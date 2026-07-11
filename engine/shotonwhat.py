"""Look a title up on shotonwhat.com to tell live-action FILM vs DIGITAL. shotonwhat is a CAMERA
database, reliable for live action via its structured `acquisition` field (Celluloid → film,
Digital → digital); animated titles often 404 there and can't be told 2D-vs-CGI apart, so those go
through tmdb.py instead. HTML-only (no API): we build the page URL from title+year and read the
`"acquisition":[...]` blob the page embeds. Cached + polite (one request per title, real UA)."""
from __future__ import annotations
import json
import os
import re
import urllib.request

CACHE = os.path.expanduser("~/.topaz-pipeline/shotonwhat_cache.json")
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) VisionaryPreset/1.0"


def _slug(title: str, year=None) -> str:
    """URL slug: lowercase, drop any '(parenthetical)' qualifier (US)/(2018)/…, punctuation → single
    hyphens, append the year. 'The Office (US)', 2005 → 'the-office-2005'."""
    t = re.sub(r"\([^)]*\)", " ", title or "")
    t = re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")
    return f"{t}-{year}" if year and t else (t or "")


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


def _fetch(slug: str, timeout=6):
    if not slug:
        return None
    try:
        req = urllib.request.Request("https://shotonwhat.com/" + slug, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


def _parse(html: str) -> str:
    """'film' | 'digital' | '' from the page's embedded `"acquisition":[ ... ]` list. Celluloid ⇒
    film (grain treatment is the safer call if a title mixes both); Digital ⇒ digital."""
    if not html:
        return ""
    m = re.search(r'"acquisition"\s*:\s*\[([^\]]*)\]', html)
    if not m:
        return ""
    acq = m.group(1).lower()          # e.g. '"digital cinema"' or '"celluloid","computer generated (digital)"'
    if "celluloid" in acq:
        return "film"
    if "digital" in acq:
        return "digital"
    return ""


def film_or_digital(title: str, year=None):
    """'film' | 'digital' | None (not found / unclear). Cached per title|year."""
    key = f"{title}|{year}"
    cache = _load_cache()
    if key in cache:
        return cache[key] or None
    result = _parse(_fetch(_slug(title, year)))
    cache[key] = result
    _save_cache(cache)
    return result or None
