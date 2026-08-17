"""Which NAS folders are TV, which are Movies, which is YouTube — decided FROM PLEX.

Before this, the media roots were hardcoded constants in transfer.py with env-var escape
hatches: `/Media/TV-Shows` + two guessed sibling volumes, and the same for Movies. Nothing
in the app showed them, and nothing let the user change them — so a library Plex knows about
was simply invisible to Visionary (this machine has "3D Movies" and "3D TV Shows" that the
hardcoded roots miss entirely), and a differently-laid-out NAS could not be onboarded at all.

Plex already knows the answer: /library/sections gives every library its TYPE (show / movie /
artist / photo) and its folder Locations. Those Locations are the PLEX CONTAINER's view
(`/media/vol2/Movies`), so they get mapped onto FTP share paths (`/MediaVolume2/Movies`) and
then VERIFIED by listing them over FTP — a mapping that doesn't resolve is reported, never
guessed at silently.

The result is a PROPOSAL, not a decision: `assignment()` merges Plex detection with the
user's overrides from config, and the Setup UI shows every library with where its assignment
came from. Type is only the default — Plex types its "YouTube" library as `movie`, and only
the user knows whether "3D Movies" should be upscaled at all.

PURE (unit-tested): container_to_ftp, share_for, classify_default, merge_assignment.
I/O lives in detect(), which takes its Plex/FTP readers as arguments.
"""
from __future__ import annotations
import re

# Plex library types we can route. Everything else (artist, photo) is listed for the UI but
# never assigned — Visionary has no pipeline for it.
ROUTABLE = ("show", "movie")
KINDS = ("tv", "movie", "youtube", "youtube_staging")


def share_for(volnum, shares):
    """Which FTP share holds volume `volnum` (None/1 = the primary). UGOS exposes one share
    per volume: `Media`, `MediaVolume2`, `MediaVolume3`. Matched by shape rather than a
    hardcoded list so a NAS naming its shares differently still resolves."""
    cands = [s for s in (shares or []) if "media" in str(s).lower()]
    if not cands:
        return None
    if volnum in (None, "", "1"):
        # the primary share carries no volume digits
        plain = [s for s in cands if not re.search(r"\d", s)]
        return plain[0] if plain else cands[0]
    for s in cands:
        if re.search(rf"{re.escape(str(volnum))}\s*$", s):
            return s
    return None


def container_to_ftp(location, shares):
    """A Plex Location (the CONTAINER's path) -> the FTP path, or None if unmappable.

    `/media/TV-Shows`      -> `/Media/TV-Shows`
    `/media/vol2/Movies`   -> `/MediaVolume2/Movies`
    `/media/vol3/3D-TV-Shows` -> `/MediaVolume3/3D-TV-Shows`
    The first component is the container's mount point (whatever it is called) and is
    dropped; a `volN` component selects the share; the rest is the path inside it."""
    loc = str(location or "")
    if not loc.startswith("/"):
        return None            # Plex Locations are absolute; a relative string is junk, and
                               # coercing it would invent a plausible-looking root
    parts = [p for p in loc.strip("/").split("/") if p]
    if len(parts) < 2:
        return None
    parts = parts[1:]                       # drop the container mount root
    volnum, tail = None, []
    for p in parts:
        m = re.fullmatch(r"vol(?:ume)?[ _-]?(\d+)", p, re.I)
        if m and volnum is None and not tail:
            volnum = m.group(1)
        else:
            tail.append(p)
    if not tail:
        return None
    share = share_for(volnum, shares)
    if not share:
        return None
    return "/" + share.strip("/") + "/" + "/".join(tail)


def classify_default(lib):
    """The DEFAULT kind for a detected library — a starting point the user can override.
    Plex's type decides tv vs movie, except that a library whose title/paths say YouTube is
    routed to the YouTube kind (Plex types it `movie`, and it is emphatically not one)."""
    title = (lib.get("title") or "").lower()
    paths = " ".join(p.get("ftp") or "" for p in lib.get("locations", [])).lower()
    if "youtube" in title or "youtube" in paths:
        return "youtube_staging" if ("raw" in title or "raw" in paths) else "youtube"
    # 3D libraries are UNASSIGNED by default. The pipeline's deliverable is a 2D 4K Dolby
    # Vision master, so running a stereoscopic library through it is very unlikely to be
    # wanted — and defaulting it ON would silently pull a whole new library into the
    # rotation the first time detection runs. Offered in Setup; never assumed.
    if re.search(r"(?<![a-z0-9])3d(?![a-z0-9])", title) or re.search(r"(?<![a-z0-9])3d", paths):
        return None
    if lib.get("plex_type") == "show":
        return "tv"
    if lib.get("plex_type") == "movie":
        return "movie"
    return None                             # artist/photo: listed, never routed


def detect(sections, shares, exists=None):
    """Turn Plex sections + FTP shares into detected libraries.

    `sections`: [{"key", "type", "title", "locations": [container paths]}] — exactly what
    /library/sections yields. `shares`: FTP root listing. `exists(path)` verifies a mapped
    path over FTP (default: assume it exists, for pure tests). Never raises."""
    exists = exists or (lambda _p: True)
    out = []
    for s in sections or []:
        locs = []
        for c in s.get("locations") or []:
            ftp = container_to_ftp(c, shares)
            locs.append({"container": c, "ftp": ftp,
                         "exists": bool(ftp) and bool(exists(ftp))})
        lib = {"key": str(s.get("key") or ""), "title": s.get("title") or "",
               "plex_type": s.get("type") or "", "locations": locs,
               "routable": (s.get("type") or "") in ROUTABLE}
        lib["default_kind"] = classify_default(lib)
        out.append(lib)
    return out


def _vol_order(ftp_path):
    """Sort key putting the PRIMARY volume first, then vol2, vol3... The walkers treat the
    first-listed root as the winner of a name collision (vol1 holds the fuller copy), so
    this ordering is load-bearing, not cosmetic."""
    share = str(ftp_path or "").strip("/").split("/")[0]
    m = re.search(r"(\d+)\s*$", share)
    return (int(m.group(1)) if m else 1, str(ftp_path))


def verified_roots(libs, kind, overrides=None):
    """The FTP roots assigned to `kind`, primary volume first. Only locations that VERIFIED
    over FTP are included — an unresolvable path must never reach the walkers."""
    ov = (overrides or {})
    roots = []
    for lib in libs or []:
        k = ov.get(lib["key"], lib.get("default_kind"))
        if k != kind:
            continue
        for loc in lib.get("locations") or []:
            if loc.get("ftp") and loc.get("exists") and loc["ftp"] not in roots:
                roots.append(loc["ftp"])
    return sorted(roots, key=_vol_order)


def merge_assignment(libs, overrides, fallbacks):
    """The final answer per kind: Plex-detected roots when there are any, else the built-in
    fallback (so a Plex-less install keeps working exactly as before). Returns
    {kind: {"roots": [...], "source": "plex"|"override"|"default"}} — `source` is what the
    Setup UI shows, so the user can always see WHY a folder is being used."""
    ov = overrides or {}
    out = {}
    for kind in KINDS:
        roots = verified_roots(libs, kind, ov)
        if roots:
            touched = any(ov.get(l["key"]) == kind and ov.get(l["key"]) != l.get("default_kind")
                          for l in (libs or []))
            out[kind] = {"roots": roots, "source": "override" if touched else "plex"}
        else:
            out[kind] = {"roots": list(fallbacks.get(kind) or []), "source": "default"}
    return out


# ---- live I/O (thin; everything above is pure) ---------------------------------------

CONFIG_KEY_KINDS = "media_lib_kinds"     # {plex section key: kind} — the user's overrides
CONFIG_KEY_ROOTS = "media_roots"         # {kind: [ftp roots]} — the APPLIED answer


def plex_sections():
    """[{key, type, title, locations}] from Plex, or None if Plex is unreachable/unset."""
    import xml.etree.ElementTree as ET
    import plex
    token = plex.plex_token()
    for base in plex.plex_base_urls():
        try:
            xml = plex._get(base, "/library/sections", token)
        except Exception:
            continue
        try:
            root = ET.fromstring(xml)
        except Exception:
            continue
        return [{"key": s.get("key"), "type": s.get("type"), "title": s.get("title"),
                 "locations": [l.get("path") for l in s.findall("Location") if l.get("path")]}
                for s in root]
    return None


def ftp_shares_and_checker(timeout=25):
    """(shares, exists_fn) from ONE FTP connection — the shares at the root plus a checker
    that verifies a mapped path is really a directory. (None, None) if unreachable."""
    import ftplib
    import transfer
    try:
        ftp = transfer.connect(timeout=timeout)
    except Exception:
        return None, None
    try:
        shares = transfer.ftp_listdir(ftp, "/")
    except ftplib.all_errors:
        try: ftp.quit()
        except Exception: pass
        return None, None
    seen = {}

    def exists(path):
        if path in seen:
            return seen[path]
        try:
            ftp.cwd(path)
            ok = True
        except Exception:
            ok = False
        seen[path] = ok
        return ok
    return shares, (exists, ftp)


def detect_live():
    """Detected libraries from the LIVE Plex + NAS, or {"error": ...}. Never raises."""
    sections = plex_sections()
    if sections is None:
        return {"error": "plex-unreachable",
                "detail": "Plex did not answer — set the Plex URL/token above, or leave the "
                          "folders on their defaults."}
    shares, pair = ftp_shares_and_checker()
    if shares is None:
        return {"error": "nas-unreachable",
                "detail": "Could not list the NAS over FTP to verify the folders."}
    exists, ftp = pair
    try:
        libs = detect(sections, shares, exists=exists)
    finally:
        try: ftp.quit()
        except Exception: pass
    return {"libraries": libs, "shares": shares}


def _cfg():
    import configstore
    try:
        return configstore.read() or {}
    except Exception:
        return {}


def overrides() -> dict:
    v = _cfg().get(CONFIG_KEY_KINDS)
    return {str(k): (str(x) if x else None) for k, x in v.items()} if isinstance(v, dict) else {}


def applied_roots() -> dict:
    """{kind: [roots]} the user/detection has APPLIED, or {} to fall back to the defaults."""
    v = _cfg().get(CONFIG_KEY_ROOTS)
    if not isinstance(v, dict):
        return {}
    out = {}
    for k in KINDS:
        r = v.get(k)
        if isinstance(r, str):
            r = [r]
        if isinstance(r, list):
            paths = [str(x).rstrip("/") for x in r if str(x or "").startswith("/")]
            if paths:
                out[k] = paths
    return out


def builtin_fallbacks() -> dict:
    """What transfer.py used before any of this existed — the behavior a Plex-less or
    unreachable install keeps."""
    import transfer
    return {"tv": list(transfer.DEFAULT_TV_ROOTS),
            "movie": list(transfer.DEFAULT_MOVIE_ROOTS),
            "youtube": [transfer.DEFAULT_YOUTUBE_ROOT],
            "youtube_staging": [transfer.DEFAULT_YOUTUBE_STAGING]}


def status() -> dict:
    """Everything the Setup section renders: the live detection, the user's overrides, the
    resulting assignment per kind, and what is ACTUALLY in force right now."""
    import transfer
    det = detect_live()
    libs = det.get("libraries") or []
    ov = overrides()
    fb = builtin_fallbacks()
    proposed = merge_assignment(libs, ov, fb)
    applied = applied_roots()
    in_force = {"tv": list(transfer.NAS_FTP_TV_ROOTS),
                "movie": list(transfer.NAS_FTP_MOVIES_ROOTS),
                "youtube": [transfer.NAS_FTP_YOUTUBE_ROOT],
                "youtube_staging": [transfer.NAS_FTP_YOUTUBE_STAGING]}
    return {"error": det.get("error"), "detail": det.get("detail"),
            "libraries": libs, "overrides": ov, "proposed": proposed,
            "applied": applied, "in_force": in_force,
            "matches_in_force": all(proposed.get(k, {}).get("roots", []) == in_force[k]
                                    for k in KINDS if proposed.get(k, {}).get("roots")),
            "kinds": list(KINDS)}
