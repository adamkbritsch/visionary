"""Movie library queue — the Movie-mode counterpart to series.py.

TV mode walks a selected series episode by episode; Movie mode walks the WHOLE Movies
library (no per-title selection) — every movie that still lacks a Dolby Vision master is a
source to process, in title order, unwatched first. The NAS Movies library is mixed: most
movies are a flat file directly under /Media/Movies, some sit in a per-movie subfolder, so
each entry tracks its own FTP dir (download/upload need it; movies aren't all in one place).

Pure logic (parse + queue building) is unit-tested; the FTP listing + DV manifest are glue,
and degrade gracefully (NAS down → empty list → 'unreachable', not 'complete')."""
from __future__ import annotations
import ftplib
import io
import json
import os

from transfer import connect as ftp_connect, ftp_listdir, NAS_FTP_MOVIES_ROOT, NAS_FTP_MOVIES_ROOTS
from series import MANIFEST_DIR, _DV_MARK

# Any container ffmpeg can decode is fine — the pipeline re-encodes to a CFR intermediate first.
_VID = (".mp4", ".mkv", ".mov", ".m4v", ".ts", ".m2ts", ".mts", ".avi", ".wmv",
        ".webm", ".mpg", ".mpeg", ".vob", ".flv", ".ogv", ".m2v", ".divx", ".mpv")
# One manifest for the whole flat library (vs per-series for TV). Written by the NAS-side
# dv_probe.py (movies branch); {basename: 0/1}. Absent → fall back to the name mark.
MOVIES_MANIFEST = MANIFEST_DIR + "/__movies__.json"


# ---- pure logic (unit-tested) ---------------------------------------------

def _is_movie_video(name: str) -> bool:
    n = name.lower()
    return n.endswith(_VID) and not n.endswith(".ug-tmp")   # skip UGOS temp files


def movie_title(name: str) -> str:
    """A clean display title from a release filename: drop the extension and everything from
    the first ' [' release-tag or ' {' edition bracket. Keeps the '(year)'.
    '12 Years a Slave (2013) [1080p BluRay …].mkv' -> '12 Years a Slave (2013)'."""
    stem = os.path.splitext(name)[0]
    for sep in (" [", " {"):
        i = stem.find(sep)
        if i > 0:
            stem = stem[:i]
    return stem.strip() or os.path.splitext(name)[0]


def parse_movies(entries, dv_map=None, watched_map=None) -> list:
    """entries = [{name, dir}] -> [{name, dir, title, has_dv, watched}] sorted by title.
    A movie 'has DV' if the NAS-probed dv_map says so OR the name carries the DV mark."""
    out = []
    for e in entries:
        n = e["name"]
        has_dv = (_DV_MARK in n.lower())
        if dv_map is not None:
            has_dv = has_dv or bool(dv_map.get(n))
        out.append({"name": n, "dir": e["dir"], "title": movie_title(n), "has_dv": has_dv,
                    "watched": bool(watched_map.get(n)) if watched_map else False})
    return sorted(out, key=lambda m: m["title"].lower())


# ---- NAS listing + DV manifest (glue) -------------------------------------

def _walk_movies(ftp, root) -> list:
    """Video files under the Movies root: flat files AND one level into per-movie subfolders.
    [{name, dir}] (dir = the file's FTP parent). MLSD types when available, else NLST by ext."""
    root = root.rstrip("/")
    out = []
    try:
        entries = list(ftp.mlsd(root))
    except ftplib.all_errors:
        entries = None
    if entries is not None:
        for name, facts in entries:
            if name in (".", ".."):
                continue
            t = facts.get("type")
            if t == "file" and _is_movie_video(name):
                out.append({"name": name, "dir": root})
            elif t == "dir":
                for sub in ftp_listdir(ftp, root + "/" + name):
                    if _is_movie_video(sub):
                        out.append({"name": sub, "dir": root + "/" + name})
    else:                                          # NLST fallback: guess dir-vs-file by ext
        for name in ftp_listdir(ftp, root):
            if _is_movie_video(name):
                out.append({"name": name, "dir": root})
            elif "." not in name:                  # looks like a folder → look one level in
                for sub in ftp_listdir(ftp, root + "/" + name):
                    if _is_movie_video(sub):
                        out.append({"name": sub, "dir": root + "/" + name})
    return out


def list_movie_entries(*, timeout=60) -> list:
    """Every movie file across ALL configured Movies roots (vol1 + vol2 + vol3) as [{name, dir}];
    [] if the NAS is unreachable. Each root walks independently — a missing/errored one is skipped,
    and `dir` records the volume a movie lives on so download/upload target the right share."""
    try:
        ftp = ftp_connect(timeout=timeout)
    except ftplib.all_errors:
        return []
    try:
        out = []
        for root in NAS_FTP_MOVIES_ROOTS:
            try:
                out.extend(_walk_movies(ftp, root))
            except ftplib.all_errors:
                continue
        return out
    finally:
        try: ftp.quit()
        except ftplib.all_errors: pass


def load_movies_dv_manifest(*, timeout=20):
    """The NAS-side movie DV manifest {basename: 0/1}, or None (then name marks decide)."""
    try:
        ftp = ftp_connect(timeout=timeout)
    except ftplib.all_errors:
        return None
    try:
        buf = io.BytesIO()
        ftp.retrbinary("RETR " + MOVIES_MANIFEST, buf.write)
        return json.loads(buf.getvalue().decode("utf-8"))
    except Exception:
        return None
    finally:
        try: ftp.quit()
        except ftplib.all_errors: pass


# ---- the LIBRARY POOL: all non-DV movies, for the searchable picker --------
# (Movie mode is curated now — you PICK movies into a queue, like picking a TV series —
# instead of auto-walking the whole library. This is the pool you pick FROM.)
_CACHE = {}


def library_list() -> list:
    """All movies WITHOUT a DV master, title-sorted — the pool the picker searches. Cached
    (NAS walk + Plex). Each = {name, dir, title, watched}."""
    if "lib" not in _CACHE:
        refresh_library()
    return _CACHE.get("lib") or []


def refresh_library() -> list:
    try:
        import plex
        wm = plex.movie_watched_map()
    except Exception:
        wm = None
    movies = parse_movies(list_movie_entries(), load_movies_dv_manifest(), watched_map=wm)
    _CACHE["lib"] = [{"name": m["name"], "dir": m["dir"], "title": m["title"], "watched": m["watched"]}
                     for m in movies if not m["has_dv"]]
    return _CACHE["lib"]


def peek_library():
    """Cached pool WITHOUT computing it (None if never built) — for fast state polls."""
    return _CACHE.get("lib")


# ---- the SELECTED QUEUE: movies the user picked to process -----------------
# Persisted, ordered. The orchestrator walks it; when it empties after a run, the mode
# reverts to TV (handled in orchestrator). Items = {name, dir, title}.
SELECTED_FILE = os.path.expanduser("~/.topaz-pipeline/movie_queue.json")


def get_selected() -> list:
    try:
        with open(SELECTED_FILE) as f:
            d = json.load(f)
            return d if isinstance(d, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_selected(items) -> None:
    os.makedirs(os.path.dirname(SELECTED_FILE), exist_ok=True)
    with open(SELECTED_FILE, "w") as f:
        json.dump(items, f)


def _pos(item) -> int:
    """A movie's queue slot = how many upcoming EPISODES process before it (0 = next, before
    any episode — the default for a freshly-added movie, preserving the priority-interrupt
    behavior). Missing/legacy entries read as 0."""
    try:
        return max(0, int(item.get("pos", 0)))
    except (TypeError, ValueError):
        return 0


def add_selected(name, nas_dir, title) -> list:
    items = get_selected()
    if not any(i.get("name") == name for i in items):
        items.append({"name": name, "dir": nas_dir, "title": title, "pos": 0})  # pos 0 = process next
        _save_selected(items)
    return items


def remove_selected(name) -> list:
    items = [i for i in get_selected() if i.get("name") != name]
    _save_selected(items)
    return items


def next_due(skip=()):
    """The movie that should process NOW, or None. A movie is 'due' when its slot reaches the
    front (pos 0); episodes process while every queued movie still has episodes ahead of it.
    Order among due movies = list (add) order. Returns {source_name, nas_dir, title}."""
    due = [i for i in get_selected() if i.get("name") not in skip and _pos(i) <= 0]
    if not due:
        return None
    nx = due[0]
    return {"source_name": nx["name"], "nas_dir": nx["dir"], "title": nx.get("title")}


def decrement_positions() -> list:
    """One episode finished, so every queued movie now has one fewer episode ahead of it.
    Call after each EPISODE upload (not movie). Floors at 0."""
    items = get_selected()
    changed = False
    for it in items:
        p = _pos(it)
        if p > 0:
            it["pos"] = p - 1
            changed = True
    if changed:
        _save_selected(items)
    return items


def move_in_queue(name, direction, ep_count) -> list:
    """Move a movie one slot in the COMBINED up-next queue (movies interleaved with episodes by
    pos). direction < 0 = up/earlier, > 0 = down/later. Moving past an EPISODE changes the
    movie's pos; moving past another MOVIE swaps their add-order. `ep_count` = remaining episodes
    (caps how far down a movie can go). No-op at the very top/bottom."""
    items = get_selected()
    idx = next((i for i, it in enumerate(items) if it.get("name") == name), None)
    if idx is None:
        return items
    m = items[idx]
    p = _pos(m)
    if direction < 0:                                       # UP
        before = [i for i in range(idx) if _pos(items[i]) == p]   # nearest same-pos movie above
        if before:
            j = before[-1]
            items[idx], items[j] = items[j], items[idx]     # swap add-order with that movie
        elif p > 0:
            m["pos"] = p - 1                                # hop above the preceding episode
    else:                                                   # DOWN
        after = [i for i in range(idx + 1, len(items)) if _pos(items[i]) == p]
        if after:
            j = after[0]
            items[idx], items[j] = items[j], items[idx]
        elif p < max(0, int(ep_count or 0)):
            m["pos"] = p + 1                                # hop below the following episode
    _save_selected(items)
    return items


def clear_selected() -> list:
    _save_selected([])
    return []


def selected_view(skip=()) -> dict:
    """The curated movie queue: ordered selected movies (each enriched with its chosen Topaz
    preset, set when it was added), the next processable one (not parked), and a count. Fast
    (small local file) — safe for state polls."""
    import settings
    items = [{**i, "preset": settings.show_preset_key(i.get("title") or "")} for i in get_selected()]
    nextable = [i for i in items if i.get("name") not in skip]
    nx = nextable[0] if nextable else None
    return {
        "items": items,
        "next": ({"source_name": nx["name"], "nas_dir": nx["dir"], "title": nx.get("title")}
                 if nx else None),
        "count": len(items),
    }
