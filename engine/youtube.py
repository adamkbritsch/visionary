"""YouTube mode — Visionary is youtarr's frontend + the upscaler.

The user connects their YouTube account (OAuth, see ytdata.py), queues any of their SUBSCRIBED
channels (each with a per-channel scope), and Visionary makes youtarr's subscription set match the
queue — youtarr (always on) then autonomously downloads exactly those channels into the STAGING
library (/Media/YouTube-raw), which is NOT a Plex library. Visionary doesn't pick individual videos;
instead it LIMITS WHAT IT UPSCALES:

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
    _drop_priority(vid)                # a send-to-Visionary one-off is retired however it finished


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


# How long a pause may last before its "come back to THIS video" pin is forgotten. The pin
# exists for the overnight case — pause mid-video, resume tomorrow, get that video back. A
# pause measured in WEEKS means the opposite: the user wants the newest video, not the one
# they walked away from (user-dictated 2026-08-17).
RESUME_FIRST_MAX_AGE = 3 * 86400


def set_resume_first(folder, vid) -> None:
    """Remember the video a channel PAUSE interrupted (keyed by channel FOLDER), so channel_pending
    serves it FIRST once the channel is unpaused — the user paused mid-video and wants THAT exact
    video back before anything else. Persisted (with the time it was set, so it can EXPIRE) so it
    survives the gap until they resume."""
    if not (folder and vid):
        return
    with _RESUME_LOCK:
        m = _resume_first_map(); m[folder] = {"vid": vid, "at": int(time.time())}
        os.makedirs(os.path.dirname(RESUME_FIRST_FILE), exist_ok=True)
        with open(RESUME_FIRST_FILE, "w") as f:
            json.dump(m, f)


def resume_first(folder):
    """The vid to serve first for this channel folder, or None — EXPIRED pins return None.
    A legacy bare-string entry (no timestamp) is treated as expired: it can only have been
    written before this file learned to age, so it is exactly the stale kind."""
    e = _resume_first_map().get(folder or "")
    if not isinstance(e, dict):
        return None                       # legacy/unknown-age -> newest-first wins
    vid, at = e.get("vid"), e.get("at") or 0
    if not vid or (time.time() - float(at)) > RESUME_FIRST_MAX_AGE:
        return None
    return vid


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
    """DEPRECATED no-op flag (the length cap was removed 2026-08-17). Still stored so an
    existing queue file round-trips, but nothing reads it when choosing videos.
    Historical: on → only upscale videos ≤ max_youtube_minutes; off (default)
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


# ---- FETCH-AHEAD: keep youtarr's staging folder stocked ------------------------------
# Visionary subscribes youtarr to the queued channels and then WAITS for youtarr's own
# schedule to fetch anything — nothing ever asked for a specific video (download_videos
# existed only for the companion-app push). So the upscale queue could run dry purely
# because nothing had been downloaded yet, with the pipeline idle and the NAS quiet.
# This tops the staging buffer up to a target per channel, newest-first.
_FETCH_LOCK = threading.Lock()
_fetch_at = 0.0
FETCH_GAP_SECONDS = 300.0        # don't hammer youtarr's trigger endpoint


def _fetch_ahead_target() -> int:
    try:
        import settings
        return max(0, int(settings.get_settings().get("youtube_fetch_ahead", 0)))
    except Exception:
        return 0


def wanted_ids(entry, target) -> list:
    """Video ids this channel SHOULD have on staging but doesn't: candidates newest-first,
    minus what is already downloaded, done, or out of scope/age. Never raises."""
    folder = entry.get("folder_name")
    if not folder or entry.get("paused"):
        return []
    have = {v.get("vid") for v in cached_videos(folder)}
    done = get_done()
    scope = entry.get("scope", "popular")
    popular = (_META.get(entry.get("channelId")) or {}).get("popular") or set()
    pubs, max_age, now = _published(), _max_age_secs(entry), time.time()
    # CANDIDATES. _META only ever holds the `popular` set, and a scope="all" channel has no
    # set at all — so the ids come from youtarr, which already indexes every video it knows
    # for a channel. Ordered newest-first by publish date where we know it (unknown dates
    # sort last rather than jumping the queue on a 0).
    try:
        import youtarr
        cands = [v if isinstance(v, str) else (v or {}).get("youtube_id") or (v or {}).get("id")
                 for v in youtarr.channel_video_ids(entry["channelId"])]
        cands = [v for v in cands if v]
    except Exception:
        cands = []
    if not cands:
        cands = list(popular)
    cands.sort(key=lambda v: -(pubs.get(v) or 0))
    out = []
    for vid in cands:
        if vid in have or vid in done:
            continue
        if scope == "popular" and popular and vid not in popular:
            continue
        pub = pubs.get(vid)
        if max_age and pub and (now - pub) > max_age:
            continue
        out.append(vid)
        if len(out) >= target:
            break
    return out


def fetch_ahead(force=False) -> dict:
    """Ask youtarr to download whatever the queue is short of, so the upscale stream never
    starves waiting on youtarr's own cadence. {channel folder: [ids requested]}; {} when the
    feature is off, rate-limited, or nothing is missing. Best-effort — never raises."""
    global _fetch_at
    target = _fetch_ahead_target()
    if target <= 0:
        return {}
    with _FETCH_LOCK:
        now = time.time()
        if not force and (now - _fetch_at) < FETCH_GAP_SECONDS:
            return {}
        _fetch_at = now
    import youtarr
    asked = {}
    for e in get_queue():
        if e.get("paused") or not e.get("channelId"):
            continue
        folder = e.get("folder_name") or ""
        try:
            have = len([v for v in cached_videos(folder)
                        if v.get("vid") not in get_done()]) if folder else 0
            short = target - have
            if short <= 0:
                continue
            ids = wanted_ids(e, short)
            if ids and youtarr.download_videos(ids):
                asked[folder or e["channelId"]] = ids
        except Exception:
            continue
    return asked


# ---- youtarr's own settings: the contract Visionary silently depends on ---------------
# These were set BY HAND in youtarr's UI and nothing verified them. Get one wrong and the
# failure is quiet and confusing: raw 1080p downloads showing up in the Plex library
# (output directory pointing at the Plex root instead of staging), masters shipping with no
# .nfo/poster/subtitle sidecars because youtarr never wrote them, or nothing to upscale at
# all because youtarr's channel auto-download was off.

def _youtarr_host_prefix(current_dir) -> str | None:
    """The prefix youtarr's HOST paths carry in front of the FTP path, OBSERVED from its own
    current setting — never guessed.

    The same folder has three names: youtarr writes `/volume1/Media/YouTube-raw`, Visionary
    reads `/Media/YouTube-raw` over FTP, Plex sees `/media/YouTube`. Nothing can compute
    that mapping from first principles, but youtarr's existing value reveals it: match its
    TAIL against a media path we already know and whatever precedes it is the prefix.
    Returns None when no known path matches — then the directory is reported, not rewritten,
    because inventing an absolute path would strand every future download."""
    import transfer
    cur = str(current_dir or "").rstrip("/")
    if not cur:
        return None
    known = [transfer.NAS_FTP_YOUTUBE_STAGING, transfer.NAS_FTP_YOUTUBE_ROOT,
             *transfer.NAS_FTP_TV_ROOTS, *transfer.NAS_FTP_MOVIES_ROOTS]
    for p in sorted({k.rstrip("/") for k in known if k}, key=len, reverse=True):
        if cur.endswith(p):
            return cur[: len(cur) - len(p)]        # "" is valid (youtarr uses FTP-style paths)
    return None


def youtarr_desired_output_dir(cfg) -> str | None:
    """Where youtarr SHOULD write, in youtarr's own path space, or None if unknowable."""
    import transfer
    prefix = _youtarr_host_prefix((cfg or {}).get("youtubeOutputDirectory"))
    if prefix is None:
        return None
    return prefix + transfer.NAS_FTP_YOUTUBE_STAGING.rstrip("/")


def ensure_staging_dir() -> bool:
    """Make sure the staging folder EXISTS on the NAS, so pointing youtarr at it can't fail
    on a missing directory. Best-effort; True when it exists afterwards."""
    import ftplib
    import transfer
    path = transfer.NAS_FTP_YOUTUBE_STAGING.rstrip("/")
    try:
        ftp = transfer.connect()
    except Exception:
        return False
    try:
        try:
            ftp.cwd(path)
            return True
        except ftplib.all_errors:
            pass
        made, cur = True, ""
        for part in [p for p in path.strip("/").split("/") if p]:
            cur += "/" + part
            try:
                ftp.cwd(cur)
            except ftplib.all_errors:
                try:
                    ftp.mkd(cur)
                except ftplib.all_errors:
                    made = False
                    break
        return made
    finally:
        try: ftp.quit()
        except Exception: pass


def youtarr_contract(cfg=None) -> list:
    """[{key, desired, current, ok, why}] — what Visionary needs from youtarr and how the
    live settings compare. `desired=None` means "any value is fine, shown for context"."""
    import transfer
    cfg = cfg if cfg is not None else {}
    staging = transfer.NAS_FTP_YOUTUBE_STAGING.rstrip("/")
    outdir = str(cfg.get("youtubeOutputDirectory") or "")
    # youtarr writes a HOST path (/volume1/Media/YouTube-raw) while Visionary reads the same
    # folder over FTP (/Media/YouTube-raw), so compare by SUFFIX rather than demanding an
    # exact match we cannot compute without knowing the share mount.
    out_ok = bool(outdir) and outdir.rstrip("/").endswith(staging)
    want_out = None if out_ok else youtarr_desired_output_dir(cfg)
    rows = [
        {"key": "youtubeOutputDirectory", "current": outdir, "desired": want_out, "ok": out_ok,
         "why": (f"must be the STAGING folder ending in {staging} — the folder split keeps "
                 f"raw downloads out of the Plex library; only finished 4K masters go there")
                + ("" if (out_ok or want_out) else
                   ". Its current value matches no known media folder, so the correct path "
                   "cannot be derived — set it in youtarr.")},
        {"key": "channelAutoDownload", "current": cfg.get("channelAutoDownload"),
         "desired": True, "ok": bool(cfg.get("channelAutoDownload")),
         "why": "off means youtarr never fetches on its own and the queue only fills when "
                "Visionary asks"},
        {"key": "writeVideoNfoFiles", "current": cfg.get("writeVideoNfoFiles"),
         "desired": True, "ok": bool(cfg.get("writeVideoNfoFiles")),
         "why": "the .nfo is copied beside the master so Plex keeps the title/description"},
        {"key": "writeVideoFanart", "current": cfg.get("writeVideoFanart"),
         "desired": True, "ok": bool(cfg.get("writeVideoFanart")),
         "why": "the thumbnail is copied beside the master"},
        {"key": "writeChannelPosters", "current": cfg.get("writeChannelPosters"),
         "desired": True, "ok": bool(cfg.get("writeChannelPosters")),
         "why": "channel poster for the Plex folder"},
        {"key": "subtitlesEnabled", "current": cfg.get("subtitlesEnabled"),
         "desired": True, "ok": bool(cfg.get("subtitlesEnabled")),
         "why": "subtitles are muxed into the master when present"},
    ]
    return rows


def youtarr_config_status() -> dict:
    """The Setup view: youtarr's live settings against the contract. Never raises."""
    import youtarr
    cfg = youtarr.get_config()
    if cfg is None:
        return {"error": "youtarr-unreachable",
                "detail": "youtarr did not answer — check its URL and credentials above.",
                "rows": [], "ok": False}
    rows = youtarr_contract(cfg)
    for r in rows:                       # the app decodes these as Strings
        for k in ("current", "desired"):
            v = r.get(k)
            r[k] = "" if v is None else ("on" if v is True else
                                         "off" if v is False else str(v))
    return {"rows": rows, "ok": all(r["ok"] for r in rows),
            "output_dir": cfg.get("youtubeOutputDirectory")}


def apply_youtarr_contract() -> dict:
    """Set the settings Visionary requires. The output DIRECTORY is deliberately NOT
    rewritten — only youtarr knows its own mount layout, and pointing it at the wrong
    absolute path would silently strand every future download. That one is reported for the
    user to fix in youtarr."""
    import youtarr
    cfg = youtarr.get_config()
    if cfg is None:
        return {"error": "youtarr-unreachable"}
    rows = youtarr_contract(cfg)
    patch = {r["key"]: r["desired"] for r in rows if r["desired"] is not None and not r["ok"]}
    if "youtubeOutputDirectory" in patch:
        # Point youtarr at staging only when the folder actually exists, so its next
        # download cannot fail on a missing directory.
        if not ensure_staging_dir():
            patch.pop("youtubeOutputDirectory")
    if not patch:
        return {"changed": {}, "ok": True}
    ok = youtarr.update_config(patch)
    return {"changed": patch if ok else {}, "ok": bool(ok),
            **({} if ok else {"error": "update-failed"})}


def _cap_secs():   # DEPRECATED — kept only so an old settings.json key stays readable
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
    max_secs = 10 ** 9        # no length limit — see channel_pending
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
    """Pending videos to UPSCALE for one queued channel, NEWEST-first: on disk, in scope
    ('popular' → in the top-viewed set; 'all' → any), not done, not parked. Each =
    {source_name, nas_dir, video_path, title, vid, channel(folder)}.

    NO LENGTH CAP (removed 2026-08-17, user-dictated). The cap existed because a long video
    used to cost the same hour-class Topaz + capped-x265 pass as a TV episode. It doesn't
    any more: YouTube skips Topaz entirely (Resolve scales it) and ships Resolve's render
    stream-copied (YOUTUBE_RENDER_KBPS + remux_ship_render), so a long video is now cheap
    in proportion to its length instead of catastrophically expensive."""
    if entry.get("paused"):        # paused → no upscaling work at all (keeps what it already has)
        return []
    folder = entry.get("folder_name")
    if not folder:
        return []
    max_age = _max_age_secs(entry)                       # None → no age limit
    scope = entry.get("scope", "popular")
    popular = (_META.get(entry.get("channelId")) or {}).get("popular") or set()
    durs, pubs, done, now = _durations(), _published(), get_done(), time.time()
    out = []
    for v in cached_videos(folder):
        vid, stem = v["vid"], os.path.splitext(v["name"])[0]
        if vid in done or stem in skip:
            continue
        pub = pubs.get(vid)
        if max_age and pub and (now - pub) > max_age:  # older than this channel's max age (prune deletes it)
            continue
        if scope == "popular" and popular and vid not in popular:
            continue
        out.append({"channel": folder, "source_name": v["name"], "nas_dir": v["dir"],
                    "video_path": v["path"], "title": video_title(v["name"], folder), "vid": vid,
                    "_pub": pub or v.get("mtime") or 0})
    # NEWEST-FIRST BY PUBLISH DATE, not by download time. cached_videos is ordered by the
    # file's mtime on the NAS — i.e. WHEN YOUTARR FETCHED IT — which is the same thing only
    # while a channel is being followed live. Un-pause a channel after weeks and youtarr
    # backfills the gap: those older videos land with the NEWEST mtimes, sort to the front,
    # and the pipeline grinds forward from where it paused instead of starting at the top
    # (user-reported 2026-08-17). The publish map is already loaded here for the max-age
    # filter, so the honest key costs nothing; mtime remains the fallback for a video whose
    # publish date was never fetched (both are epoch seconds, so they mix safely).
    out.sort(key=lambda e: e["_pub"], reverse=True)
    for e in out:
        e.pop("_pub", None)
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
                      # per-channel loudness-boost gate — keyed by FOLDER like the preset
                      # (= p.series at the remux stage for YouTube items)
                      "normalize_audio": settings.get_show_normalize_audio(folder or ""),
                      # per-channel output range (auto / sdr / dv1000 / dv2000), same key
                      "output_mode": settings.get_show_output_mode(folder or ""),
                      # youtarr's downloads are SDR, so auto always means the 1000-nit ceiling
                      "output_mode_effective": settings.effective_output_mode(folder or "", False),
                      "pending": len(channel_pending(e)), "downloaded": len(cached_videos(folder))})
    return {"items": items, "count": len(items), "connected": _connected()}


def _connected() -> bool:
    try:
        import ytdata
        return ytdata.connected()
    except Exception:
        return False


# ---- SEND TO VISIONARY: priority one-offs pushed from the companion YouTube app -----------
# The app POSTs /api/send-to-visionary with a watch URL; Visionary asks youtarr to download
# EXACTLY that video (works for unsubscribed channels too — youtarr's manual grab), records
# it in a durable priority book, and the orchestrator serves it as the NEXT item the moment
# the file lands on staging — cadence-exempt, ahead of due movies (the user just pressed a
# button; that IS the priority signal). mark_done retires the entry however the video ends
# up processed (priority pick or the ordinary channel stream).

PRIORITY_FILE = os.path.expanduser("~/.topaz-pipeline/yt_priority.json")
_PRIORITY_LOCK = threading.Lock()
_PRIORITY_SCAN_GAP = 45.0            # min seconds between staging-wide FTP locate scans
_priority_scan_at = 0.0

_URL_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([0-9A-Za-z_-]{11})")
_BARE_ID = re.compile(r"^[0-9A-Za-z_-]{11}$")


def parse_video_id(text) -> str:
    """The 11-char video id from a watch/short/live/embed URL or a bare id; '' if none."""
    t = str(text or "").strip()
    if _BARE_ID.match(t):
        return t
    m = _URL_ID.search(t)
    return m.group(1) if m else ""


def _priority() -> list:
    try:
        with open(PRIORITY_FILE) as f:
            v = json.load(f)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _save_priority(items) -> None:
    try:
        os.makedirs(os.path.dirname(PRIORITY_FILE), exist_ok=True)
        tmp = PRIORITY_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(items, f)
        os.replace(tmp, PRIORITY_FILE)
    except OSError:
        pass


def send_priority(url_or_id, title=None) -> dict:
    """The /api/send-to-visionary handler body. Idempotent; every outcome is a JSON-able
    status the sending app can show on its button: queued | already-queued |
    already-upscaled | bad-url | youtarr-unreachable."""
    import youtarr
    vid = parse_video_id(url_or_id)
    if not vid:
        return {"status": "bad-url"}
    if vid in get_done():
        return {"status": "already-upscaled", "id": vid}
    with _PRIORITY_LOCK:
        book = _priority()
        if any(e.get("vid") == vid for e in book):
            return {"status": "already-queued", "id": vid}
    if not youtarr.download_videos([vid]):
        return {"status": "youtarr-unreachable", "id": vid}
    with _PRIORITY_LOCK:
        book = _priority()
        if not any(e.get("vid") == vid for e in book):     # re-check under the lock
            book.append({"vid": vid, "title": (title or "").strip() or None,
                         "sent_at": int(time.time())})
            _save_priority(book)
    return {"status": "queued", "id": vid}


def prioritize_pending(vid) -> dict:
    """JUMP AN ALREADY-DOWNLOADED video to the front of everything (user-asked 2026-08-17).

    send_priority() is for a video the companion app pushes — it triggers a youtarr
    download and waits for the file to appear. This is the other half: a video already on
    staging and already sitting in the up-next list, promoted to the priority book with its
    path filled in, so locate_priority serves it on the very next selection (cadence-exempt
    and ahead of due movies). Inserted at the FRONT: the most recent "do this now" wins.

    Status: queued | already-first | not-pending | already-upscaled | bad-id."""
    v = parse_video_id(vid) or str(vid or "").strip()
    if not v:
        return {"status": "bad-id"}
    if v in get_done():
        return {"status": "already-upscaled", "id": v}
    with _PRIORITY_LOCK:
        if any(e.get("vid") == v for e in _priority()):
            return {"status": "already-first", "id": v}
    hit = next((x for x in all_pending() if x.get("vid") == v), None)
    if not hit:
        return {"status": "not-pending", "id": v}
    with _PRIORITY_LOCK:
        book = _priority()
        if any(e.get("vid") == v for e in book):
            return {"status": "already-first", "id": v}
        book.insert(0, {"vid": v, "title": hit.get("title"),
                        "channel": hit.get("channel"), "path": hit.get("video_path"),
                        "sent_at": int(time.time())})
        _save_priority(book)
    return {"status": "queued", "id": v, "title": hit.get("title")}


def has_priority_ready(skip=()) -> bool:
    """Is a priority video sitting ready to run RIGHT NOW? Book-only — NO staging scan, so
    this is cheap enough for the run thread to poll between Topaz segments (locate_priority
    can trigger an FTP walk; this must never)."""
    done = get_done()
    for e in _priority():
        p = e.get("path")
        if not p or e.get("vid") in done:
            continue
        if os.path.splitext(os.path.basename(p))[0] in (skip or ()):
            continue
        return True
    return False


def _drop_priority(vid) -> None:
    with _PRIORITY_LOCK:
        book = _priority()
        kept = [e for e in book if e.get("vid") != vid]
        if len(kept) != len(book):
            _save_priority(kept)


def _locate_scan() -> None:
    """Find on-staging files for un-located priority entries. Two tiers: the QUEUED
    channels' caches first (no FTP), then one throttled staging-wide walk — a manual grab
    from an UNSUBSCRIBED channel lands in a folder Visionary otherwise never scans.
    Located entries cache {channel, path} in the book, so this goes quiet once found."""
    global _priority_scan_at
    with _PRIORITY_LOCK:
        book = _priority()
        want = {e["vid"] for e in book if not e.get("path")}
    if not want:
        return
    found = {}
    for e in get_queue():                                   # tier 1: cached, quota/FTP-free
        folder = e.get("folder_name")
        for v in (cached_videos(folder) if folder else []):
            if v.get("vid") in want:
                found[v["vid"]] = {"channel": folder, "path": v["path"]}
    missing = want - set(found)
    now = time.monotonic()
    if missing and now - _priority_scan_at >= _PRIORITY_SCAN_GAP:
        _priority_scan_at = now
        try:                                                # tier 2: staging-wide (throttled)
            ftp = ftp_connect(timeout=30)
        except ftplib.all_errors:
            ftp = None
        if ftp is not None:
            try:
                queued = {e.get("folder_name") for e in get_queue()}
                for folder in ftp_listdir(ftp, NAS_FTP_YOUTUBE_STAGING.rstrip("/")):
                    if folder in queued or not missing:
                        continue                            # tier 1 already covered queued folders
                    for v in list_video_files(folder):
                        if v.get("vid") in missing:
                            found[v["vid"]] = {"channel": folder, "path": v["path"]}
                            missing.discard(v["vid"])
            finally:
                try: ftp.quit()
                except ftplib.all_errors: pass
    if found:
        with _PRIORITY_LOCK:
            book = _priority()
            for e in book:
                hit = found.get(e.get("vid"))
                if hit and not e.get("path"):
                    e.update(hit)
            _save_priority(book)


def locate_priority(skip=()) -> dict | None:
    """The first sent-to-Visionary video whose FILE is already on staging (oldest send
    first), as {channel, video_path, title, vid} for youtube_paths — or None. `skip` is
    the selection skip-key set (a YouTube key is the video's file STEM)."""
    if not _priority():
        return None
    _locate_scan()
    for e in _priority():
        p = e.get("path")
        if not p:
            continue
        stem = os.path.splitext(os.path.basename(p))[0]
        if stem in skip:
            continue
        if e["vid"] in get_done():                          # finished via the ordinary stream
            _drop_priority(e["vid"])
            continue
        return {"channel": e.get("channel"), "video_path": p,
                "title": e.get("title"), "vid": e["vid"]}
    return None
