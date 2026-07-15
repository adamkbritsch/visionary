"""Series selection + work queue.

The user picks ONE TV series in the app. The pipeline then walks that series in
order, taking the first 1080p source episode that has no Dolby Vision master yet,
and keeps going down the series until a different series is selected.

Pure logic (episode parsing + queue building) is unit-tested. The NAS listing is
SSH glue (the same shell transport as transfer.py) and the selection persists to
a small JSON file the dashboard and the (future) orchestrator both read.
"""
from __future__ import annotations
import ftplib
import json
import os
import re

from transfer import connect as ftp_connect, ftp_listdir, ftp_walk_files, NAS_FTP_TV_ROOT, NAS_FTP_TV_ROOTS

SELECTION_FILE = os.path.expanduser("~/.topaz-pipeline/selection.json")
_EP = re.compile(r"[sS](\d{1,2})[eE](\d{1,3})")
_EPX = re.compile(r"\b(\d{1,2})x(\d{2,3})\b")   # the '9x01' naming convention ('The Office (US)
                                                # - 9x01 - New Guys.mkv'). Word-bounded on both
                                                # numbers so resolution tokens (1920x1080) can
                                                # never match. Checked AFTER SxxExx.
# Any container ffmpeg can decode is fine — the pipeline re-encodes to a CFR intermediate first,
# so support all common video inputs, not just the mp4/mkv the library mostly holds.
_VID = (".mp4", ".mkv", ".mov", ".m4v", ".ts", ".m2ts", ".mts", ".avi", ".wmv",
        ".webm", ".mpg", ".mpeg", ".vob", ".flv", ".ogv", ".m2v", ".divx", ".mpv")
_DV_MARK = "hdr10 dv"   # the finished-master naming convention


# ---- pure logic (unit-tested) ---------------------------------------------

def parse_episodes(names, dv_map=None, watched_map=None) -> list:
    """Video basenames in a series dir -> ordered list of per-episode dicts:
    {ep:'S02E11', has_source, has_dv, source_name, watched}. SxxExx keys are zero-padded
    so they sort numerically (E2 before E10).

    A file 'has DV' if the NAS-probed `dv_map` ({basename: 0/1}) says so OR its name
    carries the master mark (covers a master we just made, before the next probe). The
    goal is Dolby Vision for ALL content, so ANY non-DV file is a source to process —
    not just 1080p; an episode whose only file is already DV has nothing to do.

    `watched` (from Plex via `watched_map` {basename: bool}) flags whether the user has
    already watched the SOURCE file — the queue processes unwatched episodes first. Absent
    a watched_map (Plex unavailable) every episode is `watched=False` → plain numeric order."""
    eps = {}
    for n in names:
        if not n.lower().endswith(_VID):
            continue
        m = _EP.search(n) or _EPX.search(n)
        if not m:
            continue
        key = f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"
        e = eps.setdefault(key, {"ep": key, "has_source": False,
                                 "has_dv": False, "source_name": None, "watched": False})
        file_has_dv = (_DV_MARK in n.lower())
        if dv_map is not None:
            file_has_dv = file_has_dv or bool(dv_map.get(n))
        if file_has_dv:
            e["has_dv"] = True
        else:
            e["has_source"] = True
            e["source_name"] = n
            if watched_map is not None:
                e["watched"] = bool(watched_map.get(n))
    return [eps[k] for k in sorted(eps)]


def build_queue(names, dv_map=None, watched_map=None, skip=()) -> dict:
    """Queue = episodes with a non-DV source and no DV anywhere yet. Ordered UNWATCHED-FIRST
    then watched, numeric within each group (do the episodes the user hasn't seen yet before
    the ones they have). With no watched_map it's plain numeric order. `skip` excludes ep keys
    from `next` (e.g. episodes the orchestrator PARKED after repeated failures)."""
    eps = parse_episodes(names, dv_map, watched_map)
    remaining = [e for e in eps if e["has_source"] and not e["has_dv"]]
    remaining.sort(key=lambda e: 1 if e.get("watched") else 0)   # stable → unwatched, then watched
    nextable = [e for e in remaining if e["ep"] not in skip]
    return {
        "next": nextable[0] if nextable else None,
        "remaining": [e["ep"] for e in remaining],
        # ordered processing list (parked excluded) with titles, for the "up next" preview
        "remaining_items": [{"ep": e["ep"], "source_name": e["source_name"]} for e in nextable],
        "remaining_count": len(remaining),
        "unwatched_count": sum(1 for e in remaining if not e.get("watched")),
        "done_count": sum(1 for e in eps if e["has_dv"]),
        "source_count": sum(1 for e in eps if e["has_source"]),
    }


# ---- NAS listing (SSH glue) -----------------------------------------------

# Which volume each show lives on: {series dir name: FTP TV root}. Populated by list_series;
# series_root() consults it (or re-lists) so episode listing + paths target the right volume.
_SERIES_ROOTS = {}


def list_series(*, timeout=20) -> list:
    """Series dir names across EVERY TV volume (FTP), de-duplicated; [] if unreachable. Records
    each show's volume in _SERIES_ROOTS (first root wins a name collision → vol1 priority)."""
    try:
        ftp = ftp_connect(timeout=timeout)
    except ftplib.all_errors:
        return []
    try:
        for root in NAS_FTP_TV_ROOTS:
            try:
                for n in ftp_listdir(ftp, root):
                    if not n.startswith("."):
                        _SERIES_ROOTS.setdefault(n, root)
            except ftplib.all_errors:
                continue
        return sorted(_SERIES_ROOTS)
    finally:
        try: ftp.quit()
        except ftplib.all_errors: pass


def series_root(series) -> str:
    """The FTP TV root (volume) a show lives on — cached from list_series, re-listed if unknown,
    defaulting to vol1. Used to build the show's episode/download/upload paths on the right volume."""
    if series in _SERIES_ROOTS:
        return _SERIES_ROOTS[series]
    list_series()                          # (re)populate the map, then look again
    return _SERIES_ROOTS.get(series, NAS_FTP_TV_ROOT)


def list_episode_files(series, *, timeout=40) -> list:
    """All video basenames under a series dir on its volume (FTP, recurses into seasons)."""
    root = series_root(series)                 # resolve the volume first (own connection if it re-lists)
    try:
        ftp = ftp_connect(timeout=timeout)
    except ftplib.all_errors:
        return []
    try:
        return ftp_walk_files(ftp, root.rstrip("/") + "/" + series)
    except ftplib.all_errors:
        return []
    finally:
        try: ftp.quit()
        except ftplib.all_errors: pass


# DV manifests live alongside the TV library on the NAS, under the Media share's
# Config dir (FTP: /Media/Config/dv_manifests/<series>.json).
MANIFEST_DIR = os.path.dirname(NAS_FTP_TV_ROOT.rstrip("/")) + "/Config/dv_manifests"


def load_dv_manifest(series, *, timeout=20):
    """Download the NAS-side DV manifest {basename: 0/1} for a series, or None.
    Written by /volume1/Media/Config/dv_probe.py; if absent we fall back to name marks."""
    import io
    path = MANIFEST_DIR + "/" + series + ".json"
    try:
        ftp = ftp_connect(timeout=timeout)
    except ftplib.all_errors:
        return None
    try:
        buf = io.BytesIO()
        ftp.retrbinary("RETR " + path, buf.write)
        return json.loads(buf.getvalue().decode("utf-8"))
    except Exception:
        return None
    finally:
        try: ftp.quit()
        except ftplib.all_errors: pass


def episode_queue(series, skip=()) -> dict:
    # Per-show toggle: unwatched-first (default) consults the Plex watched map; OFF → just
    # numeric order from the start. Plex is best-effort either way (any failure → numeric).
    wm = None
    try:
        import settings
        if settings.get_show_unwatched_first(series):
            import plex
            wm = plex.watched_map(series)
    except Exception:
        wm = None
    return build_queue(list_episode_files(series), load_dv_manifest(series),
                       watched_map=wm, skip=skip)


# ---- queue cache (so /api/state polling never hits the NAS) ---------------
_QUEUE_CACHE = {}


def cached_queue(series_name):
    """The series' queue from cache (no NAS I/O — fast for state polling). Computes +
    caches on first request."""
    if not series_name:
        return None
    if series_name not in _QUEUE_CACHE:
        _QUEUE_CACHE[series_name] = episode_queue(series_name)
    return _QUEUE_CACHE.get(series_name)


def refresh_queue(series_name):
    """Recompute the queue from the NAS and update the cache. Called when the picker
    opens, a series is selected, and after each upload finishes (so 'done' / 'next up'
    advance live instead of only when the run stops)."""
    if not series_name:
        return None
    q = episode_queue(series_name)
    _QUEUE_CACHE[series_name] = q
    return q


# ---- selection persistence ------------------------------------------------

def _read_selection_file() -> dict:
    try:
        with open(SELECTION_FILE) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


MAX_ACTIVE = 3   # round-robin across at most this many series at once


def get_active_series() -> list:
    """The active series (1..MAX_ACTIVE, ordered) — the round-robin set: one episode is taken
    from each in turn, looping back to the first. Index 0 is the 'primary'. Migrates the legacy
    single `series` field so old selection.json files keep working. Empty = nothing selected."""
    d = _read_selection_file()
    a = d.get("active")
    if isinstance(a, list):
        return [s for s in a if isinstance(s, str) and s][:MAX_ACTIVE]
    s = d.get("series")                              # legacy single-series field
    return [s] if isinstance(s, str) and s else []


def get_selection():
    """The PRIMARY active series, or None. Back-compat for callers that want 'the current
    series' — preset/queue/profile all key on a specific name, so the primary is the sensible
    default target."""
    a = get_active_series()
    return a[0] if a else None


def _write_active(active, rotation=None) -> list:
    active = [s for s in active if s][:MAX_ACTIVE]
    d = _read_selection_file()
    d["active"] = active
    d["series"] = active[0] if active else None      # keep the legacy field in sync
    rot = d.get("rotation", 0) if rotation is None else rotation
    d["rotation"] = (rot % len(active)) if active else 0
    os.makedirs(os.path.dirname(SELECTION_FILE), exist_ok=True)
    with open(SELECTION_FILE, "w") as f:
        json.dump(d, f)
    return active


def add_series(name) -> list:
    """Add a series to the round-robin set (no dups, capped at MAX_ACTIVE)."""
    a = get_active_series()
    if name and name not in a and len(a) < MAX_ACTIVE:
        a.append(name)
        _write_active(a)
    return a


def remove_series(name) -> list:
    """Drop a series from the round-robin set."""
    return _write_active([s for s in get_active_series() if s != name])


def set_selection(series):
    """REPLACE the set with a single series (legacy 'pick THE series'; resets the rotation)."""
    return _write_active([series] if series else [], rotation=0)


def set_series_at(index, name) -> list:
    """Put `name` in round-robin slot `index` — replace that slot if it exists, else append (an
    empty slot passes the next index). Dedup: a show can't occupy two slots. Unlike set_selection
    it does NOT reset the other slots, so each slot's picker changes just that one show."""
    a = get_active_series()
    if not name:
        return a
    if 0 <= index < len(a):
        a[index] = name
    elif len(a) < MAX_ACTIVE:
        a.append(name)
    seen, out = set(), []
    for s in a:                       # dedup, keeping the slot we just set/added
        if s not in seen:
            seen.add(s); out.append(s)
    return _write_active(out)


def get_rotation() -> int:
    """Index into get_active_series() of the show whose turn is next."""
    a = get_active_series()
    return (_read_selection_file().get("rotation", 0) % len(a)) if a else 0


def advance_rotation(served_name) -> int:
    """After an episode of `served_name` finishes, point the rotation at the NEXT active show so
    the following episode comes from a different series. No-op if it's no longer active."""
    a = get_active_series()
    if not a:
        return 0
    try:
        i = a.index(served_name)
    except ValueError:
        return get_rotation()
    rot = (i + 1) % len(a)
    _write_active(a, rotation=rot)
    return rot


# ---- TV-vs-Movie mode (the nav bar) ---------------------------------------

_MODES = ("tv", "movie", "youtube")


def get_mode() -> str:
    """'tv' (walk a selected series), 'movie' (curated movie queue), or 'youtube' (curated
    channel queue) — the nav-bar VIEW. Persists in the selection file; defaults to 'tv'.
    NOTE: this is the VIEW only — movie + youtube queues process regardless of the current view."""
    m = _read_selection_file().get("mode")
    return m if m in _MODES else "tv"


def set_mode(mode: str) -> str:
    mode = mode if mode in _MODES else "tv"
    d = _read_selection_file()
    d["mode"] = mode
    os.makedirs(os.path.dirname(SELECTION_FILE), exist_ok=True)
    with open(SELECTION_FILE, "w") as f:
        json.dump(d, f)
    return mode
