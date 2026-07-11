"""YouTube mode — Visionary is youtarr's frontend + the upscaler.

The user connects their YouTube account (OAuth, see ytdata.py), queues any of their SUBSCRIBED
channels (each with a per-channel scope), and Visionary makes youtarr's subscription set match the
queue — youtarr (always on) then autonomously downloads exactly those channels into the STAGING
library (/Media/YouTube-raw), which is NOT a Plex library. Visionary doesn't pick individual videos;
instead it LIMITS WHAT IT UPSCALES:

  * per-channel length cap (OFF by default): when a channel's `capped` toggle is on, never upscale
    a video longer than `max_youtube_minutes` (settings, default 20); off → any length upscales;
  * per-channel scope: 'popular' → only the channel's top-viewed videos; 'all' → everything downloaded.

FOLDER-SPLIT: the finished 4K DV master is PUBLISHED into the Plex "YouTube" library (/Media/YouTube)
at the mirrored path (same channel/video folder + same stem, so youtarr's .nfo/thumbnail/subs still
match), and the staging source is purged — so ONLY upscaled masters ever appear in Plex. 'done' is a
local set of video ids (youtube_done.json). The user can also DELETE a downloaded (staging) video
(removes it + marks it ignored in youtarr so it isn't re-downloaded). YouTube channels interleave
with TV series in the round-robin.
Everything degrades gracefully (not connected / NAS or youtarr down → empty, never crashes a poll).
"""
from __future__ import annotations
import ftplib
import json
import os
import re
import threading
import time

from transfer import (connect as ftp_connect, ftp_listdir, remote_mtime,
                      NAS_FTP_YOUTUBE_STAGING, NAS_FTP_YOUTUBE_ROOT)

_VID = (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".ts", ".m2ts", ".mts", ".avi", ".mpv")
_VIDEO_ID = re.compile(r"\[([0-9A-Za-z_-]{11})\]")   # the [VIDEOID] youtarr embeds in the filename
YOUTUBE_PRESET = "youtube"                            # channels default to this Topaz preset

QUEUE_FILE = os.path.expanduser("~/.topaz-pipeline/youtube_queue.json")
DONE_FILE = os.path.expanduser("~/.topaz-pipeline/youtube_done.json")


# ---- pure helpers (unit-tested) -------------------------------------------

def _is_video(name: str) -> bool:
    return name.lower().endswith(_VID)


def video_id(name: str) -> str:
    """The 11-char YouTube id from the filename's `[VIDEOID]`, else the stem (stable done-key)."""
    m = _VIDEO_ID.search(name or "")
    return m.group(1) if m else os.path.splitext(name or "")[0]


def video_title(name: str, channel: str = None) -> str:
    """Clean DISPLAY title: drop the extension + trailing ` [VIDEOID]` + the leading
    `<channel> - `, then decode the FTP wire encoding (GB18030-in-latin-1 → real UTF-8,
    e.g. '£¢wizard£¢' → '＂wizard＂'). Display only — never a path/key."""
    import transfer
    stem = os.path.splitext(name)[0]
    stem = re.sub(r"\s*\[[0-9A-Za-z_-]{11}\]\s*$", "", stem)
    if channel and stem.lower().startswith(channel.lower() + " - "):
        stem = stem[len(channel) + 3:]
    return transfer.display_name(stem.strip() or os.path.splitext(name)[0])


def _channel_base(folder):
    # youtarr's raw downloads live in the STAGING library (not the Plex "YouTube" lib) — that's
    # what Visionary scans for videos to upscale. Masters are published to the Plex lib on finish.
    return NAS_FTP_YOUTUBE_STAGING.rstrip("/") + "/" + folder


def list_video_files(folder, *, timeout=40) -> list:
    """Every video in a channel FOLDER as [{name, dir, path, mtime, vid}], NEWEST-first by mtime.
    Walks the youtarr nesting (folder/<video-folder>/<video>.mp4); poster.jpg + channel-level files
    are skipped. [] if unreachable / folder absent."""
    if not folder:
        return []
    base = _channel_base(folder)
    try:
        ftp = ftp_connect(timeout=timeout)
    except ftplib.all_errors:
        return []
    try:
        out = []
        for sub in ftp_listdir(ftp, base):
            if sub.startswith(".") or sub == "poster.jpg" or _is_video(sub):
                continue
            vdir = base + "/" + sub
            for f in ftp_listdir(ftp, vdir):
                if _is_video(f):
                    path = vdir + "/" + f
                    out.append({"name": f, "dir": vdir, "path": path,
                                "mtime": remote_mtime(ftp, path) or 0, "vid": video_id(f)})
                    break
        out.sort(key=lambda v: v["mtime"], reverse=True)
        return out
    except ftplib.all_errors:
        return []
    finally:
        try: ftp.quit()
        except ftplib.all_errors: pass


# ---- caches (state polls never hit the NAS / YouTube) ---------------------
_VIDEO_CACHE = {}    # folder -> [videos on disk]
_META = {}           # channelId -> {"popular": set(ids)}  (the top-viewed set for 'popular' scope)

# Video DURATIONS are PERSISTED to disk (a video's length never changes) — so batching survives a
# relaunch and never re-hits the API for a length it already knows. Without this, _META was wiped on
# every relaunch → durations unknown → pending_batches lumps everything into ONE batch instead of
# ~length-cap groups (the "forgot to group" bug). DEFAULT_YT_SECS is the fallback a video counts as
# while its true length isn't known yet, so grouping still forms (never one giant blob).
DURATIONS_FILE = os.path.expanduser("~/.topaz-pipeline/youtube_durations.json")
DEFAULT_YT_SECS = 300          # assume ~5 min for an as-yet-unmeasured video (batching only)
_DURATIONS = None              # lazy-loaded {vid: seconds}


def _durations() -> dict:
    global _DURATIONS
    if _DURATIONS is None:
        try:
            with open(DURATIONS_FILE) as f:
                d = json.load(f)
            _DURATIONS = {k: int(v) for k, v in d.items()} if isinstance(d, dict) else {}
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            _DURATIONS = {}
    return _DURATIONS


def remember_durations(secs) -> None:
    """Persist fetched video durations {vid: seconds} (permanent — lengths don't change). Only writes
    when something new/changed, so it's cheap to call on every refresh."""
    if not secs:
        return
    d = _durations()
    changed = False
    for k, v in secs.items():
        try:
            iv = int(v)
        except (ValueError, TypeError):
            continue
        if iv and d.get(k) != iv:
            d[k] = iv
            changed = True
    if changed:
        try:
            os.makedirs(os.path.dirname(DURATIONS_FILE), exist_ok=True)
            tmp = DURATIONS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.replace(tmp, DURATIONS_FILE)
        except OSError:
            pass


# Video PUBLISH timestamps — persisted like durations (a publish date never changes), for the
# per-channel max-age filter (download then delete videos older than the limit).
PUBLISHED_FILE = os.path.expanduser("~/.topaz-pipeline/youtube_published.json")
_PUBLISHED = None              # lazy-loaded {vid: publish_unix_ts}


def _published() -> dict:
    global _PUBLISHED
    if _PUBLISHED is None:
        try:
            with open(PUBLISHED_FILE) as f:
                d = json.load(f)
            _PUBLISHED = {k: int(v) for k, v in d.items()} if isinstance(d, dict) else {}
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            _PUBLISHED = {}
    return _PUBLISHED


def remember_published(pubs) -> None:
    """Persist video publish timestamps {vid: unix_ts} (permanent). Skips 0/unknown."""
    if not pubs:
        return
    d = _published()
    changed = False
    for k, v in pubs.items():
        try:
            iv = int(v)
        except (ValueError, TypeError):
            continue
        if iv and d.get(k) != iv:
            d[k] = iv
            changed = True
    if changed:
        try:
            os.makedirs(os.path.dirname(PUBLISHED_FILE), exist_ok=True)
            tmp = PUBLISHED_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.replace(tmp, PUBLISHED_FILE)
        except OSError:
            pass


def video_published(vid) -> int:
    """A video's publish time (unix ts) from the persisted cache, or 0 if not yet known."""
    return _published().get(vid, 0)


def cached_videos(folder) -> list:
    if folder and folder not in _VIDEO_CACHE:
        _VIDEO_CACHE[folder] = list_video_files(folder)
    return _VIDEO_CACHE.get(folder) or []


def refresh_videos(folder) -> list:
    if folder:
        _VIDEO_CACHE[folder] = list_video_files(folder)
    return _VIDEO_CACHE.get(folder) or []


def refresh_downloads() -> None:
    """LIVE re-scan across ALL queued channels — cheap enough to run on a timer during a run so
    youtarr's fresh downloads enter the upscale queue in real time (even while the user is away).
    Re-lists each channel's staging folder (FTP, NO API quota) and fetches durations for any NEW
    video ids only (videos.list = 1 quota unit / 50 ids). Deliberately does NOT re-run the 'popular'
    search (quota-heavy + near-static — that's refreshed at run start / on queue edits). So new
    downloads on 'all' channels become pending immediately; on 'popular' channels as soon as they're
    in the (persisted) top-viewed set."""
    import ytdata
    durs, pubs = _durations(), _published()
    for e in get_queue():
        folder = e.get("folder_name")
        if not folder or e.get("paused"):                   # skip paused channels (no work on them)
            continue
        refresh_videos(folder)                              # FTP re-scan (no quota)
        # fetch for anything missing a duration OR a publish date (a video whose duration was cached
        # via popular_videos never got a date — without this it could never be age-pruned)
        missing = [v["vid"] for v in cached_videos(folder) if v["vid"] not in durs or v["vid"] not in pubs]
        if missing:
            meta = ytdata.video_meta(missing) or {}         # videos.list — 1 unit/50: duration + date
            remember_durations({k: m.get("secs") for k, m in meta.items()})
            remember_published({k: m.get("pub") for k, m in meta.items()})
        prune_old(e)                                        # delete videos older than this channel's max age


def refresh_all_meta() -> None:
    """Full metadata refresh for every ACTIVE queued channel (popular set via search.list + durations).
    Quota-heavier — called ONCE at run start + on queue edits, never on the live timer. Skips paused."""
    for e in get_queue():
        if not e.get("paused"):
            refresh_meta(e)


# ---- done-set -------------------------------------------------------------

def get_done() -> set:
    try:
        with open(DONE_FILE) as f:
            d = json.load(f)
            return set(d) if isinstance(d, list) else set()
    except (OSError, json.JSONDecodeError):
        return set()


_DONE_LOCK = threading.Lock()          # serialize the done-set read-modify-write (orchestrator + wipe)


def _save_done(done) -> None:
    os.makedirs(os.path.dirname(DONE_FILE), exist_ok=True)
    with open(DONE_FILE, "w") as f:
        json.dump(sorted(done), f)


def mark_done(vid) -> None:
    if not vid:
        return
    with _DONE_LOCK:                   # atomic vs a concurrent wipe dropping ids from the same file
        done = get_done()
        done.add(vid)
        _save_done(done)


# ---- resume-first: a channel PAUSE interrupted this video → serve it FIRST when the channel resumes -------

RESUME_FIRST_FILE = os.path.expanduser("~/.topaz-pipeline/youtube_resume_first.json")
_RESUME_LOCK = threading.Lock()


def _resume_first_map() -> dict:
    try:
        with open(RESUME_FIRST_FILE) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def set_resume_first(folder, vid) -> None:
    """Remember the video a channel PAUSE interrupted (keyed by channel FOLDER), so channel_pending
    serves it FIRST once the channel is unpaused — the user paused mid-video and wants THAT exact
    video back before anything else. Persisted so it survives the gap until they resume."""
    if not (folder and vid):
        return
    with _RESUME_LOCK:
        m = _resume_first_map(); m[folder] = vid
        os.makedirs(os.path.dirname(RESUME_FIRST_FILE), exist_ok=True)
        with open(RESUME_FIRST_FILE, "w") as f:
            json.dump(m, f)


def resume_first(folder):
    """The vid to serve first for this channel folder, or None."""
    return _resume_first_map().get(folder or "") or None


def clear_resume_first(folder) -> None:
    with _RESUME_LOCK:
        m = _resume_first_map()
        if folder in m:
            del m[folder]
            with open(RESUME_FIRST_FILE, "w") as f:
                json.dump(m, f)


# ---- the channel QUEUE (unlimited, each {channelId, title, folder_name, scope}) ---

def get_queue() -> list:
    try:
        with open(QUEUE_FILE) as f:
            d = json.load(f)
            return d if isinstance(d, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_queue(items) -> None:
    os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
    with open(QUEUE_FILE, "w") as f:
        json.dump(items, f)


def add_channel(channel_id, title, scope="popular") -> list:
    """Queue a subscribed channel (no dups, no limit). Defaults scope 'popular' + the YouTube preset."""
    items = get_queue()
    if channel_id and not any(i.get("channelId") == channel_id for i in items):
        items.append({"channelId": channel_id, "title": title or channel_id, "folder_name": "",
                      "scope": scope if scope in ("popular", "all") else "popular",
                      "capped": False, "paused": False, "max_age_days": 0})
        _save_queue(items)
    return items


def remove_channel(channel_id) -> list:
    items = [i for i in get_queue() if i.get("channelId") != channel_id]
    _save_queue(items)
    return items


def set_scope(channel_id, scope) -> list:
    items = get_queue()
    for e in items:
        if e.get("channelId") == channel_id:
            e["scope"] = scope if scope in ("popular", "all") else "popular"
    _save_queue(items)
    return items


def set_capped(channel_id, on) -> list:
    """Per-channel length-cap toggle: on → only upscale videos ≤ max_youtube_minutes; off (default)
    → upscale any length. Purely upscale-side (doesn't change what youtarr downloads)."""
    items = get_queue()
    for e in items:
        if e.get("channelId") == channel_id:
            e["capped"] = bool(on)
    _save_queue(items)
    return items


def set_paused(channel_id, on) -> list:
    """Per-channel PAUSE toggle: on → stop ALL work on the channel (youtarr stops downloading it +
    Visionary stops upscaling it) WITHOUT deleting anything it already has; off → resume. The channel
    stays in the queue. (Caller re-runs configure_youtarr so youtarr's subscriptions follow suit.)"""
    items = get_queue()
    for e in items:
        if e.get("channelId") == channel_id:
            e["paused"] = bool(on)
    _save_queue(items)
    return items


def set_max_age(channel_id, days) -> list:
    """Per-channel MAX AGE (days; 0 = no limit): videos older than this are deleted from staging +
    not upscaled ('download them then delete the too-old ones'). Caller re-runs configure_youtarr,
    which prunes the now-too-old videos."""
    try:
        d = max(0, int(days))
    except (ValueError, TypeError):
        d = 0
    items = get_queue()
    for e in items:
        if e.get("channelId") == channel_id:
            e["max_age_days"] = d
    _save_queue(items)
    return items


def clear_queue() -> list:
    _save_queue([])
    return []


# ---- picker (the user's real YouTube subscriptions) -----------------------

def list_channels() -> list:
    """The user's SUBSCRIBED YouTube channels [{channelId, title}] (OAuth), or [] if not connected."""
    try:
        import ytdata
        return ytdata.subscriptions() or []
    except Exception:
        return []


# ---- configure youtarr (the frontend role) + upscale metadata -------------

def configure_youtarr():
    """Make youtarr's subscriptions == the queued channels (subscribe the queued, unsubscribe the rest),
    resolve each queued channel's on-disk folder_name, and refresh its scope/duration metadata.
    youtarr then autonomously downloads exactly these channels. Returns True/None."""
    import youtarr
    q = get_queue()
    # PAUSED channels are excluded from youtarr's subscription set → youtarr stops downloading them
    # (their files stay). Un-pausing re-subscribes them.
    active = [e for e in q if e.get("channelId") and not e.get("paused")]
    desired = [{"channelId": e["channelId"],
                "url": f"https://www.youtube.com/channel/{e['channelId']}"}
               for e in active]
    ok = youtarr.sync_subscriptions(desired)
    changed = False
    for e in q:
        if not e.get("folder_name") and not e.get("paused"):
            fn = youtarr.channel_folder(e["channelId"])
            if fn:
                e["folder_name"] = fn
                changed = True
    if changed:
        _save_queue(q)
    for e in active:
        refresh_meta(e)
        prune_old(e)                        # download-then-delete: drop videos past the channel's max age
    return ok


def _cap_secs():
    import settings
    return int(settings.get_settings().get("max_youtube_minutes", 20)) * 60


def _max_age_secs(entry):
    """The channel's max-age in seconds, or None when it has no age limit (0/unset)."""
    try:
        d = int(entry.get("max_age_days") or 0)
    except (ValueError, TypeError):
        d = 0
    return d * 86400 if d > 0 else None


def prune_old(entry) -> int:
    """DOWNLOAD-THEN-DELETE for the per-channel max-age limit: DELETE the channel's already-downloaded
    videos whose publish date is older than max_age_days — remove each from staging (FTP), tell youtarr
    to ignore it (so it isn't re-downloaded), and mark it done. Returns how many were pruned. No-op when
    the channel has no age limit; only touches videos whose publish date is KNOWN (never guesses)."""
    import transfer
    import youtarr
    max_age = _max_age_secs(entry)
    folder, cid = entry.get("folder_name"), entry.get("channelId")
    if not max_age or not folder:
        return 0
    now, pubs, pruned = time.time(), _published(), 0
    for v in list(cached_videos(folder)):
        pub = pubs.get(v["vid"])
        if pub and (now - pub) > max_age and transfer.delete_tree(v["dir"]):
            youtarr.ignore_video(cid, v["vid"])
            mark_done(v["vid"])
            pruned += 1
    if pruned:
        refresh_videos(folder)                          # drop the deleted ones from the cache
    return pruned


def refresh_meta(entry) -> None:
    """Pull the channel's upscale metadata from YouTube: the 'popular' id set (top-viewed, within the
    length cap) into _META, and durations for whatever's on disk into the PERSISTED duration cache
    (for the cap filter + batching)."""
    import ytdata
    cid = entry.get("channelId")
    if not cid:
        return
    # only length-limit the 'popular' set when this channel opts into the cap; else take top-viewed
    # of any length (huge ceiling).
    max_secs = _cap_secs() if entry.get("capped") else 10 ** 9
    popular = set()
    pv = ytdata.popular_videos(cid, max_secs=max_secs, n=50)
    if pv:
        popular = {v["id"] for v in pv}
        remember_durations({v["id"]: v["secs"] for v in pv})
    folder = entry.get("folder_name")
    if folder:
        durs, pubs = _durations(), _published()
        missing = [v["vid"] for v in cached_videos(folder) if v["vid"] not in durs or v["vid"] not in pubs]
        if missing:
            meta = ytdata.video_meta(missing) or {}
            remember_durations({k: m.get("secs") for k, m in meta.items()})
            remember_published({k: m.get("pub") for k, m in meta.items()})
    _META[cid] = {"popular": popular}


# ---- what to upscale (cap + scope filter) ---------------------------------

def channel_pending(entry, skip=()) -> list:
    """Pending videos to UPSCALE for one queued channel, NEWEST-first: on disk, within the length cap
    IF this channel is capped (else any length), in scope ('popular' → in the top-viewed set; 'all' →
    any), not done, not parked. Each = {source_name, nas_dir, video_path, title, vid, channel(folder)}."""
    if entry.get("paused"):        # paused → no upscaling work at all (keeps what it already has)
        return []
    folder = entry.get("folder_name")
    if not folder:
        return []
    cap = _cap_secs() if entry.get("capped") else None   # None → no length limit for this channel
    max_age = _max_age_secs(entry)                       # None → no age limit
    scope = entry.get("scope", "popular")
    popular = (_META.get(entry.get("channelId")) or {}).get("popular") or set()
    durs, pubs, done, now = _durations(), _published(), get_done(), time.time()
    out = []
    for v in cached_videos(folder):
        vid, stem = v["vid"], os.path.splitext(v["name"])[0]
        if vid in done or stem in skip:
            continue
        dur = durs.get(vid)
        if cap and dur and dur > cap:                  # capped channel + known to be over the limit
            continue
        pub = pubs.get(vid)
        if max_age and pub and (now - pub) > max_age:  # older than this channel's max age (prune deletes it)
            continue
        if scope == "popular" and popular and vid not in popular:
            continue
        out.append({"channel": folder, "source_name": v["name"], "nas_dir": v["dir"],
                    "video_path": v["path"], "title": video_title(v["name"], folder), "vid": vid})
    rf = resume_first(folder)          # a pause interrupted this video → it comes back FIRST on resume
    if rf:
        i = next((k for k, v in enumerate(out) if v["vid"] == rf), None)
        if i:                          # found AND not already at the front
            out.insert(0, out.pop(i))
    return out


def next_due(skip=()):
    """The next YouTube video to upscale — the head of the round-robin-across-channels stream (no
    per-channel priority; all channels interleave evenly), or None."""
    stream = all_pending(skip)
    return stream[0] if stream else None


def video_secs(vid) -> int:
    """A video's true duration in seconds from the PERSISTED duration cache (0 if not yet measured)."""
    return _durations().get(vid, 0)


def all_pending(skip=()) -> list:
    """Every pending-to-upscale video across the queued channels, ROUND-ROBINED across channels — one
    from each channel in turn (newest-first within a channel), so NO channel has priority and all
    channels' videos interleave evenly. Each is annotated with its duration ('secs'). This is the
    stream that gets grouped into ~length-cap batches."""
    durs = _durations()
    cols = []
    for e in get_queue():
        cols.append([{**v, "secs": durs.get(v["vid"]) or 0} for v in channel_pending(e, skip)])
    out, i = [], 0
    while any(i < len(col) for col in cols):          # take index i from every channel, then i+1, …
        for col in cols:
            if i < len(col):
                out.append(col[i])
        i += 1
    return out


def pending_batches(batch_secs, skip=()) -> list:
    """Group the pending YouTube videos into batches whose durations sum to ~`batch_secs` — so a
    YouTube 'turn' is ~a TV-episode's worth of short videos, not one. Round-robined across channels.
    A video whose length isn't measured yet counts as DEFAULT_YT_SECS so grouping still forms (never
    one giant blob); once its real length is known the grouping refines. Each batch is ≥1 video."""
    batches, cur, cur_secs = [], [], 0
    for v in all_pending(skip):
        s = v.get("secs") or DEFAULT_YT_SECS
        if cur and cur_secs + s > batch_secs:          # next video would overflow → cut here
            batches.append(cur)
            cur, cur_secs = [], 0
        cur.append(v)
        cur_secs += s
    if cur:
        batches.append(cur)
    return batches


# ---- delete a downloaded video (+ don't let youtarr re-fetch it) ----------

def delete_video(channel_id, video_basename) -> bool:
    """Remove a downloaded video's folder (FTP) AND ignore it in youtarr so it isn't re-downloaded;
    also mark it done so it's never re-queued. DESTRUCTIVE. Returns True on any success."""
    import youtarr
    import transfer
    vid = video_id(video_basename)
    entry = next((e for e in get_queue() if e.get("channelId") == channel_id), None)
    folder = (entry or {}).get("folder_name") or youtarr.channel_folder(channel_id)
    v = next((x for x in cached_videos(folder) if x["vid"] == vid), None) if folder else None
    deleted = transfer.delete_tree(v["dir"]) if v else False
    youtarr.ignore_video(channel_id, vid)
    mark_done(vid)
    if folder:
        refresh_videos(folder)
    return bool(deleted)


# ---- UI view --------------------------------------------------------------

_WIPING = set()                        # channelIds currently being wiped (dedup rapid double-removes)
_WIPE_LOCK = threading.Lock()


def _safe_folder(folder) -> str:
    """A channel folder must be a SINGLE plain path component. Reject anything that could escape its
    own folder (empty, slashes, dot-segments) — a wipe must never be able to delete outside it."""
    f = (folder or "").strip().strip("/")
    return f if f and "/" not in f and f not in (".", "..") else ""


def wipe_channel(channel_id, folder) -> dict:
    """DESTRUCTIVE, user-confirmed channel wipe (runs on a background thread; the queue entry was
    already dropped synchronously): collect the channel's ids WHILE it's still subscribed, delete its
    raw STAGING folder AND its published 4K MASTERS, strip its videos from youtarr's download archive
    (so re-adding RE-DOWNLOADS fresh), drop them from the done-set, and only THEN unsubscribe it in
    youtarr. Folder is validated so a bad name can't escape the channel dir; concurrent wipes of the
    same channel are ignored. Best-effort — any FTP/youtarr failure just leaves that piece in place."""
    import youtarr
    import transfer
    with _WIPE_LOCK:
        if channel_id in _WIPING:
            return {"skipped": "already wiping"}
        _WIPING.add(channel_id)
    try:
        folder = _safe_folder(folder or youtarr.channel_folder(channel_id))
        # ids to forget, collected BEFORE unsubscribing (youtarr still knows them): staging ∪ its list
        ids = {v["vid"] for v in cached_videos(folder)} if folder else set()
        ids |= set(youtarr.channel_video_ids(channel_id) or [])
        staged = masters = False
        if folder:
            staged = transfer.delete_tree(NAS_FTP_YOUTUBE_STAGING.rstrip("/") + "/" + folder)   # raw
            masters = transfer.delete_tree(NAS_FTP_YOUTUBE_ROOT.rstrip("/") + "/" + folder)      # 4K DV
            _VIDEO_CACHE.pop(folder, None)
        forgotten = youtarr.forget_downloads(ids)
        if ids:
            with _DONE_LOCK:
                _save_done(get_done() - ids)            # so a re-added video isn't skipped as "done"
        try:
            configure_youtarr()                         # NOW unsubscribe (queue entry already removed)
        except Exception:
            pass
        return {"folder": folder, "staging_deleted": bool(staged), "masters_deleted": bool(masters),
                "archive_forgotten": forgotten, "ids": len(ids)}
    finally:
        with _WIPE_LOCK:
            _WIPING.discard(channel_id)


def queue_view() -> dict:
    """The channel queue for the UI: each queued channel with its scope, Topaz preset, on-disk +
    pending(to-upscale) counts. Fast (caches only)."""
    import settings
    items = []
    for e in get_queue():
        folder = e.get("folder_name")
        items.append({"channelId": e.get("channelId"), "title": e.get("title"),
                      "folder_name": folder, "scope": e.get("scope", "popular"),
                      "capped": bool(e.get("capped")), "paused": bool(e.get("paused")),
                      "max_age_days": int(e.get("max_age_days") or 0),
                      "preset": settings.get_show_preset(folder or "") or YOUTUBE_PRESET,
                      "pending": len(channel_pending(e)), "downloaded": len(cached_videos(folder))})
    return {"items": items, "count": len(items), "connected": _connected()}


def _connected() -> bool:
    try:
        import ytdata
        return ytdata.connected()
    except Exception:
        return False
