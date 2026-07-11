"""Plex watched-state lookup — drives queue ordering (unwatched episodes first).

The pipeline now processes UNWATCHED episodes before watched ones, so an episode is in
Dolby Vision before the user gets to it. Watched state lives in Plex; this module queries
the Plex API (READ-ONLY) for a series' episodes and returns {basename: watched_bool}.

Mac-direct: the Mac reaches Plex over Tailscale → LAN (mirrors transfer.ftp_hosts). Token +
URL come from env (TOPAZ_PLEX_*) or ~/.topaz-pipeline/config.json — the USER supplies the
token; it is never hardcoded, and it goes in the X-Plex-Token HEADER, never the URL. Token
sent ONLY to the user's own Plex server (never observed-content-derived hosts).

EVERYTHING degrades gracefully: no token / unreachable / show-not-found → returns None and
the queue falls back to plain numeric order (the prior behaviour).

The series→show ratingKey map is STABLE, so it's cached to ~/.topaz-pipeline/plex_shows.json.
Discovery CONFIRMS a show by its folder Location == the series dir (the NAS directory name is
embedded in every Plex file path), so the Plex show TITLE need not match the NAS folder name
(e.g. NAS "The Office Superfan Episodes …" ↔ Plex "The Office (US)").
"""
from __future__ import annotations
import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET

from transfer import _config, nas_hosts

RK_CACHE_FILE = os.path.expanduser("~/.topaz-pipeline/plex_shows.json")
# Words dropped before scoring title↔folder overlap, so generic articles + release tags don't
# match every show. Real show words (office, simpsons, …) survive; the match is only used to
# NARROW candidates — the folder Location confirms the winner — so over/under-stripping is safe.
_STOP = {"the", "a", "an", "of", "and", "s", "season", "complete", "series",
         "1080p", "2160p", "4k", "uhd", "720p", "480p", "x264", "x265", "h264", "h265", "hevc",
         "web", "webdl", "webrip", "dl", "bluray", "bdrip", "remux", "peacock", "amzn", "nf",
         "aac", "ac3", "dd", "ddp", "atmos", "extended", "cut", "hdr", "hdr10", "dv", "episodes"}


# ---- config ----------------------------------------------------------------

def plex_token() -> str:
    return os.environ.get("TOPAZ_PLEX_TOKEN") or _config().get("plex_token") or ""


def plex_base_urls() -> list:
    """Plex API roots to try IN ORDER, mirroring transfer.nas_hosts. A single TOPAZ_PLEX_URL /
    config `plex_url` overrides; config `plex_urls` sets the list; otherwise derived from the
    configured NAS host(s) on Plex's default port."""
    forced = os.environ.get("TOPAZ_PLEX_URL") or _config().get("plex_url")
    if forced:
        return [forced.rstrip("/")]
    cfg = _config().get("plex_urls")
    if cfg:
        return [u.rstrip("/") for u in cfg]
    return [f"http://{h}:32400" for h in nas_hosts()]


def _get(base, path, token, timeout=10) -> bytes:
    """One read-only GET; token in the header (never the URL/query)."""
    req = urllib.request.Request(base + path,
                                 headers={"X-Plex-Token": token, "Accept": "application/xml"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def sessions_playing(xml_bytes) -> bool:
    """PURE (unit-tested): does /status/sessions XML have ANY actively-playing client? Counts a
    session as live when its <Player> state is playing OR buffering; a purely paused/stopped
    session is not. No sessions → False."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return False
    return any((pl.get("state") or "").lower() in ("playing", "buffering")
               for pl in root.iter("Player"))


def is_playing(*, timeout=6):
    """FAILSAFE probe for the prefetcher: True if any Plex client is streaming right now, False
    if none, None if Plex can't be reached / no token is set (caller decides how to treat
    'unknown' — the prefetcher tolerates a couple of Nones, then assumes idle). Read-only, token
    in the header. Naturally inert (always None) when no Plex token is configured."""
    token = plex_token()
    if not token:
        return None
    for base in plex_base_urls():
        try:
            xml = _get(base, "/status/sessions", token, timeout=timeout)
        except Exception:
            continue                       # try the next base URL
        return sessions_playing(xml)       # reached Plex → authoritative answer
    return None                            # no base reachable


# ---- pure helpers (unit-tested) -------------------------------------------

def _parse_leaves(xml_bytes, series) -> dict:
    """{basename: watched_bool} from a show's /allLeaves response, restricted to episodes
    whose file path is under THIS series dir (defensive — a show could have other folders).
    Watched = viewCount > 0 (a partially-watched episode has viewCount 0 → counts UNWATCHED)."""
    out = {}
    needle = "/" + series + "/"
    for v in ET.fromstring(xml_bytes).findall("Video"):
        part = v.find(".//Part")
        f = part.get("file") if part is not None else None
        if not f or needle not in f:
            continue
        try:
            watched = int(v.get("viewCount") or 0) > 0
        except (TypeError, ValueError):
            watched = False
        out[os.path.basename(f)] = watched
    return out


def _norm(s) -> set:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower())) - _STOP


def _candidates(shows, series) -> list:
    """shows = [(title, ratingKey)]; ratingKeys whose title shares a content word with the
    series dir, most-overlap first. Falls back to ALL shows if nothing overlaps (the folder
    Location still confirms the real match, so a weak title is never fatal)."""
    sw = _norm(series)
    scored = sorted(((len(_norm(t) & sw), rk) for (t, rk) in shows), key=lambda x: x[0], reverse=True)
    hit = [rk for (n, rk) in scored if n > 0]
    return hit or [rk for (_t, rk) in shows]


# ---- ratingKey cache (series→show is stable) ------------------------------

def _load_rk_cache() -> dict:
    try:
        with open(RK_CACHE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_rk(series, rk) -> None:
    c = _load_rk_cache()
    c[series] = rk
    os.makedirs(os.path.dirname(RK_CACHE_FILE), exist_ok=True)
    with open(RK_CACHE_FILE, "w") as f:
        json.dump(c, f)


def _forget_rk(series) -> None:
    c = _load_rk_cache()
    if c.pop(series, None) is not None:
        with open(RK_CACHE_FILE, "w") as f:
            json.dump(c, f)


# ---- show discovery + watched lookup --------------------------------------

def _show_section_keys(base, token) -> list:
    cfg = os.environ.get("TOPAZ_PLEX_SECTION") or _config().get("plex_tv_section")
    if cfg:
        return [str(cfg)]
    return [s.get("key") for s in ET.fromstring(_get(base, "/library/sections", token))
            if s.get("type") == "show"]


def _discover_rk(series, base, token):
    """Find the show whose folder Location IS this series dir (title-independent)."""
    shows = []
    for k in _show_section_keys(base, token):
        for d in ET.fromstring(_get(base, f"/library/sections/{k}/all", token)).findall("Directory"):
            shows.append((d.get("title") or "", d.get("ratingKey")))
    for rk in _candidates(shows, series):
        meta = ET.fromstring(_get(base, f"/library/metadata/{rk}", token))
        for d in meta.findall("Directory"):
            for loc in d.findall("Location"):
                if (loc.get("path") or "").rstrip("/").endswith("/" + series):
                    return rk
    return None


def _leaves_for(base, rk, token, series, timeout):
    """Watched map for one ratingKey, or None if it can't be fetched (e.g. a 404 after a
    library rescan changed the RK) — caller then re-discovers."""
    try:
        return _parse_leaves(_get(base, f"/library/metadata/{rk}/allLeaves", token, timeout=timeout), series)
    except Exception:
        return None


def watched_map(series, *, timeout=10):
    """{basename: watched_bool} for a series' episodes, or None on any failure (→ numeric
    order). Uses the cached ratingKey when present; SELF-HEALS a stale cache — a library
    rescan changes a show's RK, so the cached one 404s OR returns no matching episodes; either
    way we drop it and re-discover by the show's folder Location."""
    token = plex_token()
    if not (series and token):
        return None
    for base in plex_base_urls():
        try:
            cached = _load_rk_cache().get(series)
            if cached:
                m = _leaves_for(base, cached, token, series, timeout)   # None on 404, {} on no-match
                if m:
                    return m
                _forget_rk(series)                                      # stale → re-discover below
            rk = _discover_rk(series, base, token)
            if not rk:
                continue
            m = _leaves_for(base, rk, token, series, timeout)
            if m:
                _save_rk(series, rk)
                return m
        except Exception:
            continue                         # try the next base URL, else fall through to None
    return None


# ---- movies: flat library, no per-title discovery -------------------------

def _movie_section_keys(base, token) -> list:
    cfg = os.environ.get("TOPAZ_PLEX_MOVIE_SECTION") or _config().get("plex_movie_section")
    if cfg:
        return [str(cfg)]
    return [s.get("key") for s in ET.fromstring(_get(base, "/library/sections", token))
            if s.get("type") == "movie"]


def movie_watched_map(*, timeout=15):
    """{basename: watched_bool} for the WHOLE movie library, or None on failure (→ title
    order). Flat: every movie section's leaves keyed by file basename — no ratingKey
    discovery (a movie is its own leaf, unlike a show's episodes)."""
    token = plex_token()
    if not token:
        return None
    for base in plex_base_urls():
        try:
            out = {}
            for k in _movie_section_keys(base, token):
                xml = _get(base, f"/library/sections/{k}/all?type=1", token, timeout=timeout)
                for v in ET.fromstring(xml).findall("Video"):
                    part = v.find(".//Part")
                    f = part.get("file") if part is not None else None
                    if not f:
                        continue
                    try:
                        watched = int(v.get("viewCount") or 0) > 0
                    except (TypeError, ValueError):
                        watched = False
                    out[os.path.basename(f)] = watched
            if out:
                return out
        except Exception:
            continue
    return None


# ---- TV show display titles: {nas_dir: plex_title} -------------------------
# The picker should show shows by their PLEX title, not the messy NAS folder name. Plex's
# section listing has titles but no folders, and per-show metadata is 149 round-trips — but
# the episode dump (type=4) carries grandparentTitle + the Part path (which contains the NAS
# folder), so ONE query builds the whole map. ~23 MB / ~6 s, so it's CACHED + background-warmed
# (peek_titles for fast state polls; refresh_titles for the on-demand Refresh button).
_TITLE_CACHE = {}
_TITLE_WARMING = __import__("threading").Event()


def _titles_from_episodes(xml_bytes) -> dict:
    """{nas_dir: grandparentTitle} from a section's episode dump (the dir = the path
    component right after '/TV-Shows/', which is exactly what series.list_series returns)."""
    out = {}
    for v in ET.fromstring(xml_bytes).findall("Video"):
        part = v.find(".//Part")
        f = part.get("file") if part is not None else None
        title = v.get("grandparentTitle")
        if not f or not title or "/TV-Shows/" not in f:
            continue
        d = f.split("/TV-Shows/", 1)[1].split("/", 1)[0]
        if d:
            out.setdefault(d, title)
    return out


def refresh_titles(timeout=40) -> dict:
    """(Re)build the {nas_dir: plex_title} map from the TV episode dump. Always sets the cache
    (to the fresh map, or keeps the previous/empty on failure — so it isn't re-fetched every poll)."""
    token = plex_token()
    result = _TITLE_CACHE.get("map") or {}
    if token:
        for base in plex_base_urls():
            try:
                m = {}
                for k in _show_section_keys(base, token):
                    m.update(_titles_from_episodes(
                        _get(base, f"/library/sections/{k}/all?type=4", token, timeout=timeout)))
                if m:
                    result = m
                    break
            except Exception:
                continue
    _TITLE_CACHE["map"] = result
    return result


def peek_titles() -> dict:
    """Cached title map WITHOUT fetching (empty until warmed) — for fast state polls."""
    return _TITLE_CACHE.get("map") or {}


def ensure_titles_warming():
    """Kick a one-time background build of the title map if it's never been built (non-blocking)."""
    if "map" not in _TITLE_CACHE and not _TITLE_WARMING.is_set():
        _TITLE_WARMING.set()
        import threading
        def _w():
            try: refresh_titles()
            finally: _TITLE_WARMING.clear()
        threading.Thread(target=_w, daemon=True).start()


def trigger_tv_scan(timeout=10) -> bool:
    """Ask Plex to rescan the TV section(s) so it picks up new/renamed shows (Plex's own
    'refresh'). Fire-and-forget — the scan runs async on the Plex side."""
    token = plex_token()
    if not token:
        return False
    for base in plex_base_urls():
        try:
            for k in _show_section_keys(base, token):
                _get(base, f"/library/sections/{k}/refresh", token, timeout=timeout)
            return True
        except Exception:
            continue
    return False


# ---- movie display titles: {file basename: plex_title} --------------------
# Same idea as the TV title map but flat: each movie is one Video in the movie section dump,
# so {basename(Part.file): title}. Cached + background-warmed like the TV one.
_MOVIE_TITLE_CACHE = {}
_MOVIE_TITLE_WARMING = __import__("threading").Event()


def _movie_titles_from_xml(xml_bytes) -> dict:
    out = {}
    for v in ET.fromstring(xml_bytes).findall("Video"):
        part = v.find(".//Part")
        f = part.get("file") if part is not None else None
        title = v.get("title")
        year = v.get("year")
        if f and title:
            out.setdefault(os.path.basename(f), f"{title} ({year})" if year else title)
    return out


def refresh_movie_titles(timeout=40) -> dict:
    """(Re)build {basename: plex_title} from the movie section dump(s). Always sets the cache."""
    token = plex_token()
    result = _MOVIE_TITLE_CACHE.get("map") or {}
    if token:
        for base in plex_base_urls():
            try:
                m = {}
                for k in _movie_section_keys(base, token):
                    m.update(_movie_titles_from_xml(
                        _get(base, f"/library/sections/{k}/all?type=1", token, timeout=timeout)))
                if m:
                    result = m
                    break
            except Exception:
                continue
    _MOVIE_TITLE_CACHE["map"] = result
    return result


def peek_movie_titles() -> dict:
    """Cached movie title map WITHOUT fetching (empty until warmed) — for fast state polls."""
    return _MOVIE_TITLE_CACHE.get("map") or {}


def ensure_movie_titles_warming():
    """One-time background build of the movie title map (non-blocking)."""
    if "map" not in _MOVIE_TITLE_CACHE and not _MOVIE_TITLE_WARMING.is_set():
        _MOVIE_TITLE_WARMING.set()
        import threading
        def _w():
            try: refresh_movie_titles()
            finally: _MOVIE_TITLE_WARMING.clear()
        threading.Thread(target=_w, daemon=True).start()


if __name__ == "__main__":   # manual check: python3 plex.py "<series dir name>"
    import sys
    s = sys.argv[1] if len(sys.argv) > 1 else ""
    m = watched_map(s)
    if m is None:
        print("watched_map: None (no token / unreachable / show not found)")
    else:
        w = sum(1 for v in m.values() if v)
        print(f"{len(m)} episodes · watched {w} · unwatched {len(m) - w}")
        for name, watched in list(m.items())[:8]:
            print(("  [x] " if watched else "  [ ] ") + name)
