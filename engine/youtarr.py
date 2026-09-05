"""Youtarr API client — transparent auto-login so the YouTube picker can list the user's
SUBSCRIBED channels, including ones youtarr hasn't downloaded any videos from yet (a freshly-saved
channel has no folder on disk, so the filesystem scan alone can't see it).

Creds live in ~/.topaz-pipeline/config.json (`youtarr_user` / `youtarr_pass`, optional
`youtarr_url`), same chmod-600 file + pattern as the FTP/Plex creds. The app logs in on its own
(POST /auth/login → session token, cached in-process, re-login on expiry/401) — the user never sees
a prompt. Everything degrades gracefully: no creds / unreachable / auth fail → None, and
youtube.list_channels falls back to the on-disk folders.
"""
from __future__ import annotations
import json
import os
import time
import urllib.error
import urllib.request

from transfer import _config, nas_hosts

_TOKEN = {"token": None}          # cached session token (in-process)
# youtarr's /auth/login is rate-limited to 5 attempts per 15-minute window per IP. After a
# 429, no login is attempted until this lapses — retrying sooner only burns the window's
# remaining attempts. 5 minutes recovers well inside one window without hammering it.
LOGIN_BACKOFF_SECONDS = 300.0
_LOGIN_BLOCKED_UNTIL = [0.0]      # epoch; list so tests and _login can reset it in place


def _creds():
    c = _config()
    return (os.environ.get("TOPAZ_YOUTARR_USER") or c.get("youtarr_user") or "",
            os.environ.get("TOPAZ_YOUTARR_PASS") or c.get("youtarr_pass") or "")


def base_urls() -> list:
    """Youtarr API roots to try IN ORDER, mirroring plex/ftp host resolution.
    A single TOPAZ_YOUTARR_URL / config `youtarr_url` overrides; otherwise derived from
    the configured NAS host(s) on Youtarr's default port."""
    forced = os.environ.get("TOPAZ_YOUTARR_URL") or _config().get("youtarr_url")
    if forced:
        return [forced.rstrip("/")]
    return [f"http://{h}:3087" for h in nas_hosts()]


def _post(base, path, body, timeout=8):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _get(base, path, token, timeout=10):
    req = urllib.request.Request(base + path, headers={"x-access-token": token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _login(base):
    """POST /auth/login → a session token (cached), or None. Password stays in config, never logged.

    youtarr rate-limits THIS endpoint (brute-force protection) and answers 429. That used to
    be swallowed like any other error, so every caller read "no data" with nothing to say
    why — and, worse, each subsequent call with no cached token tried to log in AGAIN,
    extending the lockout (live-caught 2026-09-05: a burst of probe processes, each with an
    empty token cache, turned one 429 into a storm that made every channel look empty).
    A 429 now arms a back-off: no login attempts until it lapses (Retry-After when the
    server sends one, else LOGIN_BACKOFF_SECONDS)."""
    user, pw = _creds()
    if not (user and pw):
        return None
    if time.time() < _LOGIN_BLOCKED_UNTIL[0]:
        return None                              # still backing off — do not extend the lockout
    try:
        tok = (_post(base, "/auth/login", {"username": user, "password": pw}) or {}).get("token")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            try:
                wait = float(e.headers.get("Retry-After") or LOGIN_BACKOFF_SECONDS)
            except (TypeError, ValueError):
                wait = LOGIN_BACKOFF_SECONDS
            _LOGIN_BLOCKED_UNTIL[0] = time.time() + max(1.0, wait)
            try:                                 # say so ONCE per back-off, never silently
                import logbook
                logbook.event("youtarr: login rate-limited (429) — backing off %ds; "
                              "channel listings read as empty until then" % int(wait))
            except Exception:
                pass
        return None
    except Exception:
        return None
    if tok:
        _LOGIN_BLOCKED_UNTIL[0] = 0.0            # a real login clears any back-off
    _TOKEN["token"] = tok
    return tok


def _request(base, method, path, token, body=None, timeout=12):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"x-access-token": token}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = r.read().decode("utf-8", "replace")
        return json.loads(txt) if txt.strip() else {}


def _call(method, path, body=None, *, timeout=12):
    """One authed request across base_urls, auto-login + a single re-login on 401/403. Returns the
    parsed JSON (dict/list) on success, or None on total failure. Used by all the control ops below."""
    user, pw = _creds()
    if not (user and pw):
        return None
    for base in base_urls():
        try:
            tok = _TOKEN["token"] or _login(base)
            if not tok:
                continue
            try:
                return _request(base, method, path, tok, body, timeout)
            except urllib.error.HTTPError as e:
                if e.code not in (401, 403):
                    raise
                tok = _login(base)
                if not tok:
                    continue
                return _request(base, method, path, tok, body, timeout)
        except Exception:
            continue
    return None


# ---- control ops (Visionary is youtarr's frontend) ------------------------

def subscribed(*, timeout=12):
    """youtarr's CURRENT subscribed channels as [{channelId, url, folder_name}] (folder_name =
    uploader = the on-disk /Media/YouTube/<folder> name), or None on failure."""
    arr = _call("GET", "/getchannels", timeout=timeout)
    if arr is None:
        return None
    if isinstance(arr, dict):
        arr = arr.get("channels") or arr.get("data") or []
    return [{"channelId": c.get("channel_id") or c.get("channelId"), "url": c.get("url"),
             "folder_name": c.get("uploader") or c.get("title")}
            for c in arr if isinstance(c, dict)]


def sync_subscriptions(desired, *, timeout=20):
    """Make youtarr's subscribed set == `desired` ([{channelId, url}]) by channelId: subscribe the
    missing (by url), unsubscribe everything else (by their url). True if in sync, None on failure."""
    cur = subscribed()
    if cur is None:
        return None
    cur_by_id = {c["channelId"]: c for c in cur if c.get("channelId")}
    desired_ids = {d["channelId"] for d in desired if d.get("channelId")}
    add = [d["url"] for d in desired if d.get("channelId") not in cur_by_id and d.get("url")]
    remove = [c["url"] for cid, c in cur_by_id.items() if cid not in desired_ids and c.get("url")]
    if not add and not remove:
        return True
    return _call("POST", "/updatechannels", {"add": add, "remove": remove}, timeout=timeout) is not None


def get_config(*, timeout=12):
    """youtarr's whole settings object (GET /getconfig), or None. Visionary depends on
    several of these — where downloads land, whether the sidecars it copies get written,
    whether youtarr auto-downloads at all — and they were previously set BY HAND in
    youtarr's own UI, with nothing checking they matched."""
    r = _call("GET", "/getconfig", timeout=timeout)
    return r if isinstance(r, dict) else None


def update_config(patch, *, timeout=20):
    """PATCH youtarr's settings (POST /updateconfig with the FULL object, as its own UI
    does — a partial post would blank everything absent). Returns True/None. Only the keys
    in `patch` change; everything else is read back and sent unmodified."""
    if not patch:
        return True
    cfg = get_config(timeout=timeout)
    if cfg is None:
        return None
    merged = dict(cfg)
    merged.update(patch)
    return _call("POST", "/updateconfig", merged, timeout=timeout) is not None


def download_videos(video_ids_or_urls, *, resolution="2160", timeout=20):
    """Download EXACTLY these videos (ids or watch URLs) via /triggerspecificdownloads — bypasses the
    download archive. Used by send-to-Visionary and every playlist/link import.

    LOAD-BEARING INVARIANT (user-dictated 2026-08-28): a playlist is always expanded to
    individual WATCH URLs before it reaches here — never sent as a playlist URL. Per-video
    downloads file each video under its OWN UPLOADER's folder on staging, publishing mirrors
    that path, and Plex's channel collections read it back — so a compiler's playlist lands
    as PewDiePie/Paint/etc., and the playlist AUTHOR's name appears nowhere unless it is
    part of the playlist's own title. Handing youtarr the playlist URL instead would file
    everything under one folder and put the author's name across youtarr and Plex."""
    urls = [v if str(v).startswith("http") else f"https://www.youtube.com/watch?v={v}"
            for v in (video_ids_or_urls or [])]
    if not urls:
        return True
    body = {"urls": urls, "overrideSettings": {"resolution": str(resolution)}}
    return _call("POST", "/triggerspecificdownloads", body, timeout=timeout) is not None


def ignore_video(channel_id, youtube_id, *, timeout=12):
    """Mark a video IGNORED in youtarr so it is never (re)downloaded — used when Visionary deletes a
    downloaded video the user doesn't want. Returns True/None."""
    if not (channel_id and youtube_id):
        return False
    return _call("POST", f"/api/channels/{channel_id}/videos/{youtube_id}/ignore",
                 {}, timeout=timeout) is not None


def channel_folder(channel_id):
    """The on-disk folder name for a channelId (from youtarr), or None."""
    for c in subscribed() or []:
        if c.get("channelId") == channel_id:
            return c.get("folder_name")
    return None


# The channel tabs Visionary COUNTS as a channel's programme. `shorts` is deliberately
# absent — see channel_video_ids.
CHANNEL_TABS = ("videos", "streams")


def channel_video_ids(channel_id, *, timeout=15, page_size=100, max_pages=400, tabs=None):
    """EVERY youtube video id youtarr knows for a channel (GET /getchannelvideos/:id), or []
    on failure.

    PAGED, because the endpoint returns 50 at a time and reports the real size in
    `totalCount`. Taking only the first page silently truncated the channel to its 50
    newest videos, and this list is the CANDIDATE POOL that fetch-ahead asks youtarr to
    download — so the older tail was never requested and those videos could never be
    upscaled (user-caught 2026-09-05: Wizards with Guns looked "finished" at 50 of 56).
    The same truncation left the forgotten tail behind on a channel wipe, so a re-added
    channel would not re-download them either.

    TABS. youtarr indexes a channel's YouTube tabs separately and this endpoint answers for
    ONE tab per call (default `videos`). `videos` and `streams` are the channel's real
    programme and are both counted; `shorts` are 60-second vertical clips and are NEVER
    requested (user-dictated 2026-09-05: "don't count shorts").

    Stops per tab on an empty/short page, on reaching that tab's totalCount, on a page that
    yields nothing new (a server ignoring `page` would repeat page 1), or at max_pages."""
    if not channel_id:
        return []
    out, seen = [], set()
    for tab in (tabs or CHANNEL_TABS):
        total, got, page = None, 0, 1
        while page <= max_pages:
            r = _call("GET", "/getchannelvideos/%s?tabType=%s&page=%d&pageSize=%d"
                      % (channel_id, tab, page, page_size), timeout=timeout)
            vids = r.get("videos") if isinstance(r, dict) else r
            if isinstance(r, dict) and total is None:
                tc = r.get("totalCount")
                try:
                    total = int(tc) if tc is not None else None
                except (TypeError, ValueError):
                    total = None
            if not isinstance(vids, list) or not vids:
                break
            new = 0
            for v in vids:
                yid = (v.get("youtube_id") or v.get("youtubeId") or v.get("id")) if isinstance(v, dict) else v
                if yid and str(yid) not in seen:
                    seen.add(str(yid))
                    out.append(str(yid))
                    new += 1
            got += len(vids)
            if not new:
                break
            if len(vids) < page_size or (total is not None and got >= total):
                break
            page += 1
    return out


# youtarr's yt-dlp download ARCHIVE (the skip list), reachable over the FTP share. Defaults to the
# UGREEN docker layout (the `[docker]` share → the docker appdata volume). Point it anywhere for other
# NAS layouts via TOPAZ_YOUTARR_ARCHIVE or config `youtarr_archive`, e.g.
# "/volume1/docker/youtarr/config/complete.list" (Synology) or "/appdata/youtarr/config/complete.list".
ARCHIVE_FTP_DEFAULT = "/docker/youtarr/config/complete.list"


def archive_ftp_path() -> str:
    """FTP path to youtarr's yt-dlp download archive (complete.list). Env/config override with the
    UGREEN default, so existing setups are unaffected and any NAS layout can point it at its own path."""
    return (os.environ.get("TOPAZ_YOUTARR_ARCHIVE")
            or _config().get("youtarr_archive") or ARCHIVE_FTP_DEFAULT)


def forget_downloads(video_ids) -> int:
    """Strip these youtube ids from youtarr's download archive (complete.list, over the FTP /docker
    share) so youtarr will RE-DOWNLOAD them on a future subscribe — the "forget it was downloaded"
    half of a channel wipe. Returns lines removed. Best-effort; a brief race with youtarr appending
    is possible but channel removal is rare, so at worst an unrelated video re-downloads once."""
    ids = {str(i) for i in (video_ids or []) if i}
    if not ids:
        return 0
    archive = archive_ftp_path()
    import io
    import transfer
    try:
        ftp = transfer.connect(timeout=30)
    except Exception:
        return 0
    try:
        # SIZE first, so a short/partial RETR can never truncate the real archive
        try:
            expect = ftp.size(archive)
        except Exception:
            expect = None
        buf = io.BytesIO()
        ftp.retrbinary("RETR " + archive, buf.write)
        raw = buf.getvalue()
        if expect is not None and len(raw) != expect:
            return 0                                # incomplete read — do NOT rewrite the archive
        lines = raw.decode("utf-8", "replace").splitlines()
        kept, removed = [], 0
        for ln in lines:
            parts = ln.split()                      # "youtube <id>"
            if parts and parts[-1] in ids:
                removed += 1
            else:
                kept.append(ln)
        if removed:
            data = ("\n".join(kept) + ("\n" if kept else "")).encode("utf-8")
            # write to a temp then atomically rename, so a mid-write failure never truncates complete.list
            tmp = archive + ".tmp"
            ftp.storbinary("STOR " + tmp, io.BytesIO(data))
            try: ftp.delete(archive)
            except Exception: pass
            ftp.rename(tmp, archive)
        return removed
    except Exception:
        return 0
    finally:
        try: ftp.quit()
        except Exception: pass

def subscribed_channels(*, timeout=10):
    """The user's SUBSCRIBED youtarr channel names (each channel's `uploader`, which == its on-disk
    folder name), or None on any failure (no creds / unreachable / auth). Auto-logs-in + caches the
    token; re-logs-in once on a 401/403 (expired token)."""
    user, pw = _creds()
    if not (user and pw):
        return None
    for base in base_urls():
        try:
            tok = _TOKEN["token"] or _login(base)
            if not tok:
                continue
            try:
                arr = _get(base, "/getchannels", tok, timeout=timeout)
            except urllib.error.HTTPError as e:
                if e.code not in (401, 403):
                    raise
                tok = _login(base)                    # token expired/invalid → re-login once
                if not tok:
                    continue
                arr = _get(base, "/getchannels", tok, timeout=timeout)
            if isinstance(arr, dict):
                arr = arr.get("channels") or arr.get("data") or []
            names = [(c.get("uploader") or c.get("title")) for c in arr if isinstance(c, dict)]
            names = [n for n in names if n]
            if names:
                return sorted(set(names))
        except Exception:
            continue
    return None
