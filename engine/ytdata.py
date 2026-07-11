"""YouTube Data API (OAuth) — lists the USER's real subscriptions and a channel's most-popular
videos, so Visionary (youtarr's frontend) can drive what youtarr downloads.

Reading *my* subscriptions requires OAuth2 (an API key returns 401 on mine=true). One-time setup:
the user makes a Google Cloud OAuth client (Desktop, scope youtube.readonly) and pastes
`youtube_client_id`/`youtube_client_secret` into ~/.topaz-pipeline/config.json (0600). The app then
does a loopback consent (redirect to the dashboard server's /oauth/youtube) and stores the
`youtube_refresh_token`; access tokens refresh on demand. Everything degrades to None gracefully
(not connected / error), so nothing here can crash a poll.
"""
from __future__ import annotations
import json
import os
import re
import time
import urllib.parse
import urllib.request

from transfer import _config

CONFIG = os.path.expanduser("~/.topaz-pipeline/config.json")
SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"

_TOKEN = {"access": None, "exp": 0.0}          # cached access token (in-process)
_ISO = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


# ---- config / creds -------------------------------------------------------

def _creds():
    c = _config()
    return (c.get("youtube_client_id") or "", c.get("youtube_client_secret") or "",
            c.get("youtube_refresh_token") or "")


def configured() -> bool:
    """True once the Google OAuth *client* (id + secret) is in config — i.e. Connect can actually
    do something. Distinct from connected() (which also needs a stored refresh token)."""
    cid, cs, _ = _creds()
    return bool(cid and cs)


def connected() -> bool:
    cid, cs, rt = _creds()
    return bool(cid and cs and rt)


def _save(**kw) -> None:
    """Merge keys into config.json (atomic, 0600). Used to persist the refresh token."""
    try:
        with open(CONFIG) as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        cfg = {}
    cfg.update(kw)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG)
    os.chmod(CONFIG, 0o600)


# ---- HTTP -----------------------------------------------------------------

def _get(url, token, timeout=12):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _post_form(url, data, timeout=12):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---- OAuth loopback -------------------------------------------------------

def auth_url(redirect_uri):
    """The Google consent URL to open in the browser, or None if the client isn't configured.
    `access_type=offline` + `prompt=consent` guarantee a refresh token comes back."""
    cid, _cs, _rt = _creds()
    if not cid:
        return None
    return AUTH + "?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent"})


def exchange_code(code, redirect_uri) -> bool:
    """Exchange the consent `code` for tokens; persist the refresh token. Returns success."""
    cid, cs, _rt = _creds()
    if not (cid and cs and code):
        return False
    try:
        r = _post_form(TOKEN, {"client_id": cid, "client_secret": cs, "code": code,
                               "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
        rt = r.get("refresh_token")
        if not rt:
            return False
        _save(youtube_refresh_token=rt)
        _TOKEN["access"] = r.get("access_token")
        _TOKEN["exp"] = time.time() + int(r.get("expires_in") or 3600)
        return True
    except Exception:
        return False


def disconnect() -> None:
    _save(youtube_refresh_token="")
    _TOKEN["access"] = None
    _TOKEN["exp"] = 0.0


def access_token():
    """A valid access token (refreshed on demand), or None if not connected / refresh failed."""
    if _TOKEN["access"] and _TOKEN["exp"] - 30 > time.time():
        return _TOKEN["access"]
    cid, cs, rt = _creds()
    if not (cid and cs and rt):
        return None
    try:
        r = _post_form(TOKEN, {"client_id": cid, "client_secret": cs, "refresh_token": rt,
                               "grant_type": "refresh_token"})
        _TOKEN["access"] = r.get("access_token")
        _TOKEN["exp"] = time.time() + int(r.get("expires_in") or 3600)
        return _TOKEN["access"]
    except Exception:
        return None


# ---- data -----------------------------------------------------------------

def subscriptions():
    """The user's subscribed channels [{title, channelId}] (title-sorted), or None on failure."""
    tok = access_token()
    if not tok:
        return None
    out, page = [], None
    try:
        for _ in range(20):                    # up to ~1000 subs
            q = {"part": "snippet", "mine": "true", "maxResults": "50"}
            if page:
                q["pageToken"] = page
            r = _get(API + "/subscriptions?" + urllib.parse.urlencode(q), tok)
            for it in r.get("items") or []:
                sn = it.get("snippet") or {}
                cid = (sn.get("resourceId") or {}).get("channelId")
                if cid:
                    out.append({"title": sn.get("title") or cid, "channelId": cid})
            page = r.get("nextPageToken")
            if not page:
                break
        return sorted(out, key=lambda c: c["title"].lower())
    except Exception:
        return None


def _iso_secs(s) -> int:
    m = _ISO.fullmatch(s or "")
    if not m:
        return 0
    h, mn, sec = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mn * 60 + sec


def _iso_ts(s) -> int:
    """ISO-8601 UTC 'YYYY-MM-DDThh:mm:ss…' → unix timestamp (0 if unparseable). Used for the video's
    publish date (the per-channel max-age filter)."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", s or "")
    if not m:
        return 0
    import calendar
    try:
        return calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0))
    except (ValueError, OverflowError):
        return 0                                # out-of-range components → treat as unknown


def video_meta(ids):
    """{videoId: {"secs": duration, "pub": publish_unix_ts}} for the given ids (batched 50/call), or
    None on failure. ONE videos.list call returns both duration (contentDetails) + publish date
    (snippet) — for the ≤N-minute cap AND the per-channel max-age filter."""
    tok = access_token()
    if not (tok and ids):
        return {} if not ids else None
    out = {}
    try:
        ids = list(ids)
        for i in range(0, len(ids), 50):
            r = _get(API + "/videos?" + urllib.parse.urlencode({
                "part": "contentDetails,snippet", "id": ",".join(ids[i:i + 50])}), tok)
            for v in r.get("items") or []:
                out[v.get("id")] = {
                    "secs": _iso_secs((v.get("contentDetails") or {}).get("duration")),
                    "pub": _iso_ts((v.get("snippet") or {}).get("publishedAt")),
                }
        return out
    except Exception:
        return None


def video_seconds(ids):
    """{videoId: duration_seconds}, or None on failure. Thin wrapper over video_meta (kept for the
    length-cap callers that don't need publish dates)."""
    m = video_meta(ids)
    return None if m is None else {k: v["secs"] for k, v in m.items()}


def popular_videos(channel_id, max_secs=1200, n=50):
    """A channel's top videos by view count, filtered to ≤ max_secs, most-viewed first:
    [{id, secs, views}]. None on failure. Uses search(order=viewCount) → videos.list for
    duration+views (search alone gives neither)."""
    tok = access_token()
    if not (tok and channel_id):
        return None
    try:
        sr = _get(API + "/search?" + urllib.parse.urlencode({
            "part": "id", "channelId": channel_id, "order": "viewCount",
            "type": "video", "maxResults": str(min(int(n), 50))}), tok)
        ids = [(it.get("id") or {}).get("videoId") for it in sr.get("items") or []]
        ids = [i for i in ids if i]
        if not ids:
            return []
        vr = _get(API + "/videos?" + urllib.parse.urlencode({
            "part": "contentDetails,statistics", "id": ",".join(ids)}), tok)
        out = []
        for v in vr.get("items") or []:
            secs = _iso_secs((v.get("contentDetails") or {}).get("duration"))
            if secs and secs <= max_secs:
                out.append({"id": v.get("id"), "secs": secs,
                            "views": int((v.get("statistics") or {}).get("viewCount") or 0)})
        out.sort(key=lambda x: x["views"], reverse=True)
        return out
    except Exception:
        return None
