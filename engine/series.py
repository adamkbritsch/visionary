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
import threading

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


def is_featurette(ep_key: str) -> bool:
    """Season 00 = specials / featurettes / mobisodes (Lost's "Missing Pieces", cast
    interviews, behind-the-scenes). They are real, correctly-named SxxExx files — and
    because "S00" sorts before "S01" they would otherwise be upscaled BEFORE the show
    itself, which is never what anyone wants."""
    return str(ep_key or "").upper().startswith("S00")


def build_queue(names, dv_map=None, watched_map=None, skip=(), featurettes_last=True) -> dict:
    """Queue = episodes with a non-DV source and no DV anywhere yet. Ordered UNWATCHED-FIRST
    then watched, numeric within each group (do the episodes the user hasn't seen yet before
    the ones they have). With no watched_map it's plain numeric order. `skip` excludes ep keys
    from `next` (e.g. episodes the orchestrator PARKED after repeated failures)."""
    eps = parse_episodes(names, dv_map, watched_map)
    remaining = [e for e in eps if e["has_source"] and not e["has_dv"]]
    # Sort is STABLE, so numeric order survives inside every group. Featurettes-last
    # dominates (they belong after the whole show); unwatched-first applies within each.
    remaining.sort(key=lambda e: ((1 if (featurettes_last and is_featurette(e["ep"])) else 0),
                                  1 if e.get("watched") else 0))
    nextable = [e for e in remaining if e["ep"] not in skip]
    return {
        "next": nextable[0] if nextable else None,
        "remaining": [e["ep"] for e in remaining],
        # ordered processing list (parked excluded) with titles, for the "up next" preview
        "remaining_items": [{"ep": e["ep"], "source_name": e["source_name"]} for e in nextable],
        "remaining_count": len(remaining),
        "unwatched_count": sum(1 for e in remaining if not e.get("watched")),
        "done_count": sum(1 for e in eps if e["has_dv"]),
        # >0 means the show HAS specials, which is what makes the UI toggle relevant
        "featurette_count": sum(1 for e in eps if is_featurette(e["ep"])),
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
        global _SERIES_ROOTS
        fresh, a_root_failed = {}, False
        for root in NAS_FTP_TV_ROOTS:
            try:
                for n in ftp_listdir(ftp, root):
                    if not n.startswith("."):
                        fresh.setdefault(n, root)      # vol1 priority WITHIN this pass
            except ftplib.all_errors:
                a_root_failed = True                   # keep what we already knew for THAT volume
                continue
        # REBUILD rather than setdefault-forever. The old code never updated an existing
        # entry, so a show MOVED between volumes (or one cached from a pass where its real
        # volume failed to list) kept a wrong root permanently: the episode walk found
        # nothing and the run reported "NAS unreachable" forever, with a restart the only
        # cure (live-caught 2026-08-01 on Lost (2004) after it moved to MediaVolume3).
        if fresh:
            merged = dict(_SERIES_ROOTS) if a_root_failed else {}
            merged.update(fresh)                       # this pass always WINS over stale data
            _SERIES_ROOTS = merged                     # rebind (atomic for readers)
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


# ---- real season directories -------------------------------------------------------
# Season folders are NOT reliably named `S01` — a real library carries `Season 1`,
# `Arrested Development Season 2 S02 1080p BluRay x264-BiA`, etc. The queue always found
# those episodes (the walk recurses), but the DOWNLOAD path used to be synthesized as
# `<show>/S{NN}`, which 550s on any show that doesn't use that convention (live-caught
# 2026-07-30, the first non-`SNN` show to reach the pipeline). So remember where each
# source file actually lives, learned for free during the same walk, and persist it so a
# relaunch (or a finisher resuming an upload) still knows the real directory.
EPISODE_DIRS_FILE = os.path.expanduser("~/.topaz-pipeline/episode_dirs.json")
_EP_DIRS = None
_EP_DIRS_LOCK = threading.Lock()


def _episode_dirs() -> dict:
    global _EP_DIRS
    if _EP_DIRS is None:
        try:
            with open(EPISODE_DIRS_FILE) as f:
                d = json.load(f)
            _EP_DIRS = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            _EP_DIRS = {}
    return _EP_DIRS


def remember_episode_dirs(series, pairs) -> None:
    """Record {basename: containing FTP dir} for a series from a (dir, name) walk."""
    if not series or not pairs:
        return
    with _EP_DIRS_LOCK:
        d = _episode_dirs()
        d[series] = {name: dirpath for dirpath, name in pairs}
        try:
            os.makedirs(os.path.dirname(EPISODE_DIRS_FILE), exist_ok=True)
            tmp = EPISODE_DIRS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.replace(tmp, EPISODE_DIRS_FILE)
        except OSError:
            pass


def episode_nas_dir(series, basename):
    """The FTP dir a source file ACTUALLY lives in, or None if not learned yet (the
    caller then falls back to the `S{NN}` convention)."""
    if not series or not basename:
        return None
    with _EP_DIRS_LOCK:
        return (_episode_dirs().get(series) or {}).get(basename)


def list_episode_files(series, *, timeout=40) -> list:
    """All video basenames under a series dir on its volume (FTP, recurses into seasons).
    Also records each file's REAL directory (see episode_nas_dir) — same walk, no extra I/O."""
    root = series_root(series)                 # resolve the volume first (own connection if it re-lists)
    try:
        ftp = ftp_connect(timeout=timeout)
    except ftplib.all_errors:
        return []
    try:
        pairs = ftp_walk_files(ftp, root.rstrip("/") + "/" + series, with_dirs=True)
        remember_episode_dirs(series, pairs)
        return [name for _d, name in pairs]
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
    try:
        import settings
        feat_last = settings.get_show_featurettes_last(series)
    except Exception:
        feat_last = True
    return build_queue(list_episode_files(series), load_dv_manifest(series),
                       watched_map=wm, skip=skip, featurettes_last=feat_last)


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


def _write_selection_file(d: dict) -> dict:
    os.makedirs(os.path.dirname(SELECTION_FILE), exist_ok=True)
    with open(SELECTION_FILE, "w") as f:
        json.dump(d, f)
    return d


def _write_active(active, rotation=None) -> list:
    active = [s for s in active if s][:MAX_ACTIVE]
    d = _read_selection_file()
    d["active"] = active
    d["series"] = active[0] if active else None      # keep the legacy field in sync
    rot = d.get("rotation", 0) if rotation is None else rotation
    d["rotation"] = (rot % len(active)) if active else 0
    _write_selection_file(d)
    return active


# ---- per-slot "Up next" (a show queued to take over the slot) ---------------
# A slot is ONE show at a time. `next_up` maps a currently-active show -> the show that
# takes its slot the moment it finishes (CLEAN HANDOFF, user-dictated: no interleaving —
# the successor starts only once the old show has no episodes left). The 10%-remaining
# mark is when the successor is ARMED: locked in and prefetch-eligible, so its first
# sources are already on disk when the handoff happens and the slot never stalls.
# Keyed by SHOW NAME (not slot index) so it survives slot reordering/removal.

NEXT_UP_ARM_FRACTION = 0.10


def get_next_up_map() -> dict:
    m = _read_selection_file().get("next_up")
    if not isinstance(m, dict):
        return {}
    return {k: v for k, v in m.items()
            if isinstance(k, str) and isinstance(v, str) and k and v}


def get_next_up(show):
    return get_next_up_map().get(show or "")


def set_next_up(show, nxt) -> dict:
    """Queue `nxt` to take over `show`'s slot when `show` finishes. Falsy `nxt` clears it.
    A show can't follow itself, and an ALREADY-ACTIVE show is rejected (it's running in
    its own slot — queueing it here would duplicate it on promotion)."""
    m = get_next_up_map()
    if not show:
        return m
    if nxt and nxt != show and nxt not in get_active_series():
        m[show] = nxt
    else:
        m.pop(show, None)
    d = _read_selection_file()
    d["next_up"] = m
    _write_selection_file(d)
    return m


def slot_progress(show) -> tuple:
    """(remaining, total, fraction_left) for a show, from the CACHED queue (no NAS I/O).
    fraction_left is 1.0 when nothing is known yet, so an unreachable NAS never reads as
    'finished' and can't trigger a promotion."""
    q = cached_queue(show) or {}
    rem = int(q.get("remaining_count") or 0)
    total = rem + int(q.get("done_count") or 0)
    return rem, total, (rem / total if total else 1.0)


def near_done(show) -> bool:
    """`show` is into the last NEXT_UP_ARM_FRACTION of its episodes — i.e. ≥90% done.
    The ONE definition of that threshold: the UI only offers "queue a follow-up" here
    (it's inert noise earlier), and it's also when a queued successor arms."""
    _rem, total, frac = slot_progress(show)
    return total > 0 and frac < NEXT_UP_ARM_FRACTION


def next_up_armed(show) -> bool:
    """The successor is locked in: `show` is ≥90% done and has one queued."""
    return bool(get_next_up(show)) and near_done(show)


def promote_finished_slots() -> list:
    """Swap every active show that has NO episodes left for its queued follow-up, IN PLACE
    (slot order preserved), and clear the mapping. Returns [(old, new)]. Guarded on
    total > 0 so an empty/unreachable listing can't promote a show that isn't really done.
    Cheap: returns immediately when nothing is queued."""
    m = get_next_up_map()
    active = get_active_series()
    if not m or not active:
        return []
    slots, promos = list(active), []
    for i, s in enumerate(slots):
        nxt = m.get(s)
        if not nxt or nxt in slots:
            continue
        rem, total, _frac = slot_progress(s)
        if total > 0 and rem == 0:
            slots[i] = nxt
            promos.append((s, nxt))
    if promos:
        for old, _new in promos:
            m.pop(old, None)
        d = _read_selection_file()
        d["next_up"] = m
        _write_selection_file(d)
        _write_active(slots)
        for _old, new in promos:
            try: refresh_queue(new)
            except Exception: pass
    return promos


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
