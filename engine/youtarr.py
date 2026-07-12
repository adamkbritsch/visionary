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
import urllib.error
import urllib.request

from transfer import _config, nas_hosts

_TOKEN = {"token": None}          # cached session token (in-process)


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
    """POST /auth/login → a session token (cached), or None. Password stays in config, never logged."""
    user, pw = _creds()
    if not (user and pw):
        return None
    try:
        tok = (_post(base, "/auth/login", {"username": user, "password": pw}) or {}).get("token")
    except Exception:
        return None
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


def download_videos(video_ids_or_urls, *, resolution="2160", timeout=20):
    """Download EXACTLY these videos (ids or watch URLs) via /triggerspecificdownloads — bypasses the
    download archive. Not used by the autonomous flow (youtarr auto-downloads the subscribed channels);
    kept for an optional manual 'grab this now' affordance."""
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


def channel_video_ids(channel_id, *, timeout=15):
    """Every youtube video id youtarr knows for a channel (GET /getchannelvideos/:id), or [] on
    failure. Used to strip a wiped channel from the download archive so it can re-download later."""
    if not channel_id:
        return []
    r = _call("GET", f"/getchannelvideos/{channel_id}", timeout=timeout)
    vids = r.get("videos") if isinstance(r, dict) else r
    if not isinstance(vids, list):
        return []
    out = []
    for v in vids:
        yid = (v.get("youtube_id") or v.get("youtubeId") or v.get("id")) if isinstance(v, dict) else None
        if yid:
            out.append(str(yid))
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


def running_jobs():
    """youtarr's in-flight jobs (downloads), or [] on failure."""
    r = _call("GET", "/runningjobs")
    return r if isinstance(r, (list, dict)) else []


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
