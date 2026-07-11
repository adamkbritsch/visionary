"""TMDb lookups for preset auto-detection: is a title animation, and if so what technique (flat
2D / hand-drawn / anime vs 3D-CGI vs stop-motion). shotonwhat can't tell those apart, so TMDb
(genres + keywords + production companies + original language) does. Needs a free TMDb v3 API key in
~/.topaz-pipeline/config.json ("tmdb_api_key") or env TOPAZ_TMDB_KEY; without it every call returns
None (→ the animation branch falls back to the manual picker). Cached + resilient."""
from __future__ import annotations
import json
import os
import urllib.parse
import urllib.request

CACHE = os.path.expanduser("~/.topaz-pipeline/tmdb_cache.json")
CONFIG = os.path.expanduser("~/.topaz-pipeline/config.json")

# Signals for the animation TECHNIQUE (all matched case-insensitively as substrings), tiered by
# how specific/reliable each is — see technique() for the order they're consulted in.
# STRONG 2D — unambiguous hand-drawn / flat / anime (+ Japanese original language).
_2D_STRONG  = ("anime", "hand-drawn", "hand drawn", "traditional animation", "cel animation", "2d animation")
# Stop-motion / claymation is PHYSICALLY shot (real light + texture) → the live-action digital preset.
_STOPMOTION = ("stop motion", "stop-motion", "claymation")
# 3D / CGI — computer-rendered.
_3D_KW      = ("cgi", "computer animation", "3d animation", "computer-animated")
_3D_STUDIOS = ("pixar", "dreamworks animation", "illumination", "blue sky",
               "sony pictures animation", "walt disney animation")   # theatrical WDAS = Frozen/Encanto = 3D
# WEAK 2D — western TV cartoons TMDb under-tags for technique; consulted ONLY after 3D is ruled
# out, so a CGI show never lands here. "cartoon"/"adult animation" read as flat 2D; the studios are
# 2D-TV houses (NOT the theatrical "Walt Disney Animation" above — this is "Disney TELEVISION").
_2D_WEAK_KW = ("cartoon", "adult animation")
_2D_STUDIOS = ("disney television animation", "nickelodeon animation", "cartoon network",
               "warner bros. animation", "hanna-barbera", "titmouse", "bento box")


def _api_key() -> str:
    if os.environ.get("TOPAZ_TMDB_KEY"):
        return os.environ["TOPAZ_TMDB_KEY"]
    try:
        with open(CONFIG) as f:
            return json.load(f).get("tmdb_api_key") or ""
    except (OSError, json.JSONDecodeError):
        return ""


def _load_cache() -> dict:
    try:
        with open(CACHE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(d) -> None:
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, CACHE)


def _get(path, params, timeout=8):
    url = "https://api.themoviedb.org/3" + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def lookup(title, year, kind):
    """{'genres', 'keywords', 'companies', 'lang'} for the best title match, or None (no key / no
    match / error). Cached per kind|title|year (misses cached as None to avoid re-querying)."""
    key = _api_key()
    if not key or not title:
        return None
    ck = f"{kind}|{title}|{year}"
    cache = _load_cache()
    if ck in cache:
        return cache[ck]
    typ = "tv" if kind == "tv" else "movie"
    try:
        params = {"api_key": key, "query": title}
        if year:
            params["first_air_date_year" if typ == "tv" else "year"] = str(year)
        results = (_get(f"/search/{typ}", params) or {}).get("results") or []
        if not results:
            cache[ck] = None; _save_cache(cache); return None
        det = _get(f"/{typ}/{results[0].get('id')}", {"api_key": key, "append_to_response": "keywords"})
        kw = det.get("keywords") or {}
        info = {
            "genres":    [g.get("name", "") for g in det.get("genres") or []],
            "keywords":  [k.get("name", "") for k in (kw.get("keywords") or kw.get("results") or [])],
            "companies": [c.get("name", "") for c in det.get("production_companies") or []],
            "lang":      det.get("original_language") or "",
        }
        cache[ck] = info; _save_cache(cache)
        return info
    except Exception:
        return None


def is_animation(info) -> bool:
    return bool(info) and any("animation" in g.lower() for g in info.get("genres", []))


def technique(info):
    """Preset key for an ANIMATED title, checked most-specific signal first:
      1. flat / hand-drawn / anime (or Japanese)          → animation2d
      2. stop-motion / claymation                         → digital  (physically shot — real light)
      3. CGI / computer / 3D keyword, or a CGI film studio → animation3d
      4. western TV 'cartoon' / 'adult animation', or a 2D-TV studio → animation2d
         (reached ONLY after 3D is ruled out, so a CGI show is never mislabeled here)
      5. otherwise                                        → None  (unclear → manual)
    Stop-motion is checked BEFORE 3D on purpose: LAIKA-style films carry incidental CGI keywords,
    but the defining technique is the physical shoot."""
    if not info:
        return None
    kws = " ".join(info.get("keywords", [])).lower()
    comps = " ".join(info.get("companies", [])).lower()
    if any(k in kws for k in _2D_STRONG) or info.get("lang") == "ja":
        return "animation2d"
    if any(k in kws for k in _STOPMOTION):
        return "digital"
    if any(k in kws for k in _3D_KW) or any(s in comps for s in _3D_STUDIOS):
        return "animation3d"
    if any(k in kws for k in _2D_WEAK_KW) or any(s in comps for s in _2D_STUDIOS):
        return "animation2d"
    return None
