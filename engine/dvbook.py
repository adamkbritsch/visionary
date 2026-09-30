"""What Visionary knows about each movie's Dolby Vision PROFILE, and the queue of profile 7 movies
waiting to become profile 8.1.

Nothing else in Visionary knows a profile. The DV manifest is a 0/1 per name, and a release name
reads the same "DV" for profile 7 and 8.1, so neither can drive a filter — and after a conversion
the file keeps its exact name, so anything keyed on the name alone would keep calling it profile 7
forever and let it be queued again. Every entry here is keyed on the name AND the size: converting
drops the enhancement layer, so the size changes, and a stale entry simply stops matching.

Sources, most trusted first:
  "rpu"   the RPU of the file's own first frames (dvp7.inspect, or the other session's classifier)
  "probe" the header side data the companion head-probe already reads for every DV file
The two files: PROFILES (name -> {size, profile, el, src, rk, section, host}) and QUEUE (the lane's
work, in the order it was added). Both are written atomically under one lock; the queue is not the
movie queue, because a profile 7 conversion needs no GPU and must never pre-empt a TV episode.

NAMES are the REAL filenames (as on the NAS disk and in Plex), never the FTP wire form the library
listing carries: the server converts a library row's name with transfer.display_name exactly once,
at the boundary. (Applying display_name to a real name can mangle it — a latin-1 "é" followed by a
letter decodes as a GB18030 character.)

A queue entry moves through PHASES (dvlane.py): download -> convert -> converted -> upload -> swap,
then state DONE (plex_pending until Plex has been told). `state` is what the UI counts; `phase` is
where a restart resumes.
"""
from __future__ import annotations

import json
import os
import threading
import time

import sys as _sys
import tempfile as _tempfile

# A test run must never write the live books — pollution of ~/.topaz-pipeline has happened five
# separate times in this repo. Under unittest the books live in a throwaway folder unless a test
# points them somewhere itself.
BOOK_DIR = (os.path.join(_tempfile.gettempdir(), "visionary-test-dvbook")
            if "unittest" in _sys.modules else os.path.expanduser("~/.topaz-pipeline"))
PROFILES_FILE = os.path.join(BOOK_DIR, "dv_profiles.json")
QUEUE_FILE = os.path.join(BOOK_DIR, "dv_queue.json")
_LOCK = threading.RLock()

# queue entry states
PENDING, ACTIVE, DONE, FAILED = "pending", "active", "done", "failed"


def _load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


# ---- profiles ------------------------------------------------------------------------------

def profile_of(name: str, size=None):
    """The known profile entry for a movie file, or None. When a size is given it must match the
    recorded one — a file that changed size is not the file that was probed."""
    with _LOCK:
        e = _load(PROFILES_FILE, {}).get(name)
    if not e:
        return None
    if size is not None and e.get("size") not in (None, int(size)):
        return None
    return e


def is_p7(name: str, size=None) -> bool:
    e = profile_of(name, size)
    return bool(e and e.get("profile") == 7)


def record_profile(name: str, size, profile, *, el=None, src="probe", **extra):
    """Remember a file's profile. A weaker source never overwrites a stronger one for the same size."""
    rank = {"probe": 1, "rpu": 2}
    with _LOCK:
        book = _load(PROFILES_FILE, {})
        cur = book.get(name)
        same = cur and cur.get("size") == (int(size) if size is not None else None)
        if same and rank.get(cur.get("src"), 0) > rank.get(src, 0):
            return
        e = {"size": int(size) if size is not None else None, "profile": profile, "src": src,
             "at": int(time.time())}
        if el:
            e["el"] = el
        keep = {k: (cur or {}).get(k) for k in ("rk", "section", "host") if (cur or {}).get(k)}
        e.update(keep)
        e.update({k: v for k, v in extra.items() if v is not None})
        book[name] = e
        _save(PROFILES_FILE, book)


def seed(entries) -> int:
    """Import verified entries: [{nas_path, size_bytes, enhancement_layer, plex_rating_key,
    plex_section}] — the shape of the other session's RPU-classified list. Returns how many."""
    n = 0
    for e in entries:
        name = os.path.basename(e["nas_path"])
        el = e.get("enhancement_layer") or None
        el = "dual" if el and el.startswith("dual") else el
        record_profile(name, e.get("size_bytes"), 7, el=el, src="rpu", host=e["nas_path"],
                       rk=str(e.get("plex_rating_key") or "") or None,
                       section=str(e.get("plex_section") or "") or None)
        n += 1
    return n


def p7_names() -> dict:
    """{name: entry} for every movie currently known to be profile 7."""
    with _LOCK:
        return {k: v for k, v in _load(PROFILES_FILE, {}).items() if v.get("profile") == 7}


# ---- the conversion queue ------------------------------------------------------------------

def queue() -> list:
    with _LOCK:
        return list(_load(QUEUE_FILE, []))


def add(items) -> int:
    """Queue movies for conversion: [{name, dir, title, bytes, host?}]. Skips ones already queued
    (and not failed) and ones not known to be profile 7. Returns how many were added. `host` (the
    NAS path) comes from the profile entry when the seed knew it, else from the caller."""
    added = 0
    with _LOCK:
        q = _load(QUEUE_FILE, [])
        have = {e["name"] for e in q if e.get("state") != FAILED}
        for it in items:
            name = it.get("name")
            if not name or name in have:
                continue
            prof = profile_of(name, it.get("bytes"))
            if not prof or prof.get("profile") != 7:
                continue
            host = prof.get("host") or it.get("host")
            if not host:
                continue                                     # nowhere to fetch it from
            q = [e for e in q if e["name"] != name]          # a failed entry is re-queued fresh
            q.append({"name": name, "dir": it.get("dir") or "", "title": it.get("title") or name,
                      "bytes": int(it.get("bytes") or prof.get("size") or 0), "el": prof.get("el"),
                      "rk": prof.get("rk"), "section": prof.get("section"),
                      "host": host, "state": PENDING, "phase": None, "added": int(time.time())})
            have.add(name)
            added += 1
        _save(QUEUE_FILE, q)
    return added


def remove(name: str) -> bool:
    """Drop a movie from the queue, whatever its state. Only the lane calls this for an ACTIVE
    entry — after it has stopped working on it and cleaned its files (dvlane.Lane.remove)."""
    with _LOCK:
        q = _load(QUEUE_FILE, [])
        keep = [e for e in q if e["name"] != name]
        if len(keep) == len(q):
            return False
        _save(QUEUE_FILE, keep)
        return True


def entry(name: str):
    with _LOCK:
        return next((dict(e) for e in _load(QUEUE_FILE, []) if e["name"] == name), None)


def update(name: str, **fields):
    with _LOCK:
        q = _load(QUEUE_FILE, [])
        for e in q:
            if e["name"] == name:
                e.update(fields)
        _save(QUEUE_FILE, q)


def open_entries(phases=None, skip=()) -> list:
    """Entries still to be worked (PENDING or ACTIVE), in queue order, optionally only those whose
    phase is in `phases` (None stands for "not started"). An entry left ACTIVE by a crash or a
    relaunch is simply resumed from its phase."""
    with _LOCK:
        return [dict(e) for e in _load(QUEUE_FILE, [])
                if e.get("state") in (PENDING, ACTIVE) and e["name"] not in skip
                and (phases is None or e.get("phase") in phases)]


def next_pending(skip=()):
    """The first entry still to be worked (queue order), or None."""
    left = open_entries(skip=skip)
    return left[0] if left else None


def summary() -> dict:
    q = queue()
    by = {}
    for e in q:
        by[e.get("state", PENDING)] = by.get(e.get("state", PENDING), 0) + 1
    saved = sum(max(0, int(e.get("size_in") or 0) - int(e.get("size_out") or 0))
                for e in q if e.get("state") == DONE and e.get("size_out"))
    left = sum(int(e.get("bytes") or 0) for e in q if e.get("state") in (PENDING, ACTIVE))
    return {"total": len(q), "by_state": by, "saved_bytes": saved, "bytes_left": left}
