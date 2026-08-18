"""Season-scoped loudness decisions for TV.

Every item used to be measured on its own, so two episodes of the SAME season could land
on different gains and the volume would step between them mid-binge. A season from one
source is one mastering job, so it gets ONE decision (user-dictated 2026-08-18): the first
episode of that season to be remuxed measures and records the gain, and every later episode
from the same source reuses it instead of re-measuring.

"Same source" is judged from the FILENAME, because that is the only evidence available
before the file is opened: resolution, source, video codec, audio codec and release group.
Any difference there means a different master — a WEB-DL filler episode dropped into a
BluRay season, a re-grab from another group — and that episode is measured on its own
rather than inheriting a gain that was never meant for it. No evidence of a difference
means the same source: an unlabelled season shares one decision, which is the common case.

TV only. Movies are one item each (nothing to share with) and YouTube videos are unrelated
to one another, so both keep measuring per item.
"""
from __future__ import annotations
import json
import os
import re
import threading
import time

BOOK_FILE = os.path.expanduser("~/.topaz-pipeline/audio_gain.json")
_LOCK = threading.Lock()

_SEASON = re.compile(r"[Ss](\d{1,3})[Ee]\d{1,3}")
_BRACKET = re.compile(r"[\(\[]([^\)\]]+)[\)\]]")

# Tokens that genuinely indicate a different MASTER. Deliberately narrow: a token family
# not listed here (episode titles, years, "PROPER", "REPACK") must not split a season.
_TOKENS = (
    r"2160p|1080p|720p|576p|480p|\b4k\b",                            # resolution
    r"blu-?ray|bdrip|brrip|web-?dl|webrip|\bweb\b|hdtv|dvdrip|\bdvd\b|remux|\buhd\b",   # source
    r"x264|x265|h\.?264|h\.?265|hevc|\bavc\b|xvid|divx|\bav1\b",     # video codec
    r"truehd|dts-?hd|\bdts\b|\bddp\b|\beac3\b|\bac3\b|\baac\b|flac|opus|atmos",         # audio codec
    r"\b7\.1\b|\b5\.1\b|\b2\.0\b",                                   # channel layout
)
_TOKEN_RE = re.compile("|".join(_TOKENS), re.I)


def season_of(ep: str) -> str:
    """'S04E12' -> 'S04'. '' when the id carries no season (a special, a movie)."""
    m = _SEASON.search(str(ep or ""))
    return "S%02d" % int(m.group(1)) if m else ""


def release_group(name: str) -> str:
    """The release group, when the filename states one: a trailing '-GROUP' before the
    extension, else the last word of the final bracketed block if it isn't a tech token
    (Silence in '… (1080p BluRay x265 Silence)'). '' when there is no evidence."""
    stem = os.path.splitext(os.path.basename(str(name or "")))[0].strip()
    if not stem or not _TOKEN_RE.search(stem):
        # No tech tokens anywhere -> this is a plain title, not a scene name, and nothing in
        # it is evidence of a source. Without this guard "The Office (US)" reads US as a
        # group and "Spider-Man" reads Man, either of which would split a season on nothing.
        return ""
    m = re.search(r"-([A-Za-z0-9]{2,20})$", stem)
    if m and not _TOKEN_RE.fullmatch(m.group(1)):
        return m.group(1).lower()
    blocks = [b for b in _BRACKET.findall(stem) if _TOKEN_RE.search(b)]
    if blocks:
        words = blocks[-1].split()
        if words:
            last = words[-1]
            # "H.264-NTb" is a codec glued to a group: keep only the group half.
            if "-" in last:
                head, _, tail = last.rpartition("-")
                if tail and _TOKEN_RE.fullmatch(head):
                    return tail.lower()
            if not _TOKEN_RE.fullmatch(last):
                return last.lower()
    return ""


def source_signature(name: str) -> str:
    """A stable fingerprint of the tokens that mean 'different master'. Two filenames with
    the same signature are treated as the same source. An EMPTY signature means the name
    carries no such evidence — those group together, which is the point: only a CLEAR
    indicator splits a season."""
    stem = os.path.splitext(os.path.basename(str(name or "")))[0]
    found = {t.group(0).lower().replace("-", "").replace(".", "")
             for t in _TOKEN_RE.finditer(stem)}
    grp = release_group(name)
    if grp:
        found.add("grp:" + grp)
    return "|".join(sorted(found))


def key_for(series: str, ep: str, name: str) -> str:
    """The book key: show + season + source. '' when this isn't a seasoned TV episode, in
    which case the caller measures per item exactly as before."""
    season = season_of(ep)
    if not (series and season):
        return ""
    return "%s|%s|%s" % (str(series).strip(), season, source_signature(name))


def _read() -> dict:
    try:
        with open(BOOK_FILE) as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _write(book: dict) -> None:
    try:
        os.makedirs(os.path.dirname(BOOK_FILE), exist_ok=True)
        tmp = BOOK_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(book, f, indent=1, sort_keys=True)
        os.replace(tmp, BOOK_FILE)
    except OSError:
        pass


def remembered(key: str):
    """The season's decided gain in dB, or None if this season/source hasn't been measured."""
    if not key:
        return None
    e = _read().get(key)
    if not isinstance(e, dict):
        return None
    g = e.get("gain")
    return float(g) if isinstance(g, (int, float)) else None


def remember(key: str, gain: float, measured, name: str, target=None) -> None:
    """Record the FIRST episode's decision for this season+source. Written before the long
    remux runs, so a failed remux that retries reuses the same gain rather than re-deciding."""
    if not key:
        return
    with _LOCK:
        book = _read()
        if key in book:
            return                      # first episode wins; later ones never overwrite it
        book[key] = {"gain": round(float(gain), 2),
                     "measured": (round(float(measured), 2) if measured is not None else None),
                     "target": target, "from": os.path.basename(str(name or "")),
                     "at": int(time.time())}
        _write(book)


def forget(series: str = "") -> int:
    """Drop a show's remembered seasons (or the whole book when series is ''), so the next
    episode re-measures. Returns how many entries went."""
    with _LOCK:
        book = _read()
        if not series:
            n = len(book)
            _write({})
            return n
        pre = str(series).strip() + "|"
        kept = {k: v for k, v in book.items() if not k.startswith(pre)}
        n = len(book) - len(kept)
        if n:
            _write(kept)
        return n


def view(series: str = "") -> list:
    """The book as rows for the UI/logs: [{key, series, season, signature, gain, from}]."""
    out = []
    for k, v in sorted(_read().items()):
        parts = k.split("|", 2)
        if len(parts) != 3 or (series and parts[0] != str(series).strip()):
            continue
        out.append({"key": k, "series": parts[0], "season": parts[1], "signature": parts[2],
                    "gain": (v or {}).get("gain"), "from": (v or {}).get("from"),
                    "measured": (v or {}).get("measured")})
    return out
