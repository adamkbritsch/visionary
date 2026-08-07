"""Companion combine: pair a NAS movie with its seedbox copy and decide, track by
track, what to keep — the goal is ONE best-of MKV per film: the genuinely better
HDR10 video, a REAL (studio) Dolby Vision RPU whenever either copy carries one
(Resolve's analysis is only the no-RPU fallback), and the best lossless audio
(TrueHD Atmos), peak-budgeted for the SHIELD like every other output.

Three parts, all stdlib:
  - a read-only client for the Shuttle relay (seedbox search / manifest / fetch).
    The relay streams seedbox bytes straight through rclone — nothing is staged
    on the NAS. The NAS copy itself is probed and pulled over Visionary's own
    FTP (transfer.py), so the relay is used EXCLUSIVELY for /seedbox paths.
  - a durable book (~/.topaz-pipeline/companions.json) that carries a pairing
    from search -> candidates -> probing -> verdict -> user confirm, with async
    daemon workers for the slow steps (relay search, dual head-probe).
  - a PURE verdict engine (unit-tested): given two normalized probes it names
    the video winner, the RPU source and graft direction, the audio donor and
    whether a capped re-encode is expected — with a human "why" per choice that
    the app's verdict card shows verbatim. NOTHING runs until the user confirms
    the card (user-dictated).

Seedbox files are NEVER modified or deleted — they seed. The relay endpoints
used here are read-only by construction.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import logbook

FFPROBE = "/opt/homebrew/bin/ffprobe"
CONFIG_FILE = os.path.expanduser("~/.topaz-pipeline/config.json")
BOOK_FILE = os.path.expanduser("~/.topaz-pipeline/companions.json")
# Shuttle's own token file (0600, written by the Shuttle app) — the same bearer
# token as the relay's .env. Read at request time, NEVER logged or echoed.
SHUTTLE_TOKEN_FILE = os.path.expanduser("~/Library/Application Support/Shuttle/token")

HEAD_BYTES = 48 * 1024 * 1024   # MKV headers (tracks, codecs, DV config, duration) live at
                                # the front; 48 MB covers real remuxes with room to spare
FETCH_BUSY_WAIT = 15.0          # relay /v1/fetch has a small slot pool → 503 when busy
FETCH_BUSY_TRIES = 20
SEARCH_LIMIT = 12


class RelayError(Exception):
    """Base for relay failures. str(err) is always SAFE to log/show (no token)."""


class RelayAuthError(RelayError):
    pass


class RelayBusyError(RelayError):
    pass


class RelayDownError(RelayError):
    pass


# ---------------------------------------------------------------- relay client

def _config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


SHUTTLE_RELAY_FILE = os.path.expanduser("~/Library/Application Support/Shuttle/relay.json")


def relay_base() -> str:
    """The relay's base URL: config ('shuttle_relay_url') first, else Shuttle's own
    relay.json beside its token file (the Shuttle app writes {"base_url": ...} there so
    the integration is zero-config); '' = unconfigured."""
    url = str(_config().get("shuttle_relay_url") or "").rstrip("/")
    if url:
        return url
    try:
        with open(SHUTTLE_RELAY_FILE) as f:
            return str((json.load(f) or {}).get("base_url") or "").rstrip("/")
    except (OSError, ValueError):
        return ""


def relay_token() -> str:
    """Shuttle's token file first (the canonical copy), config fallback."""
    try:
        with open(SHUTTLE_TOKEN_FILE) as f:
            t = f.read().strip()
        if t:
            return t
    except OSError:
        pass
    return str(_config().get("shuttle_relay_token") or "").strip()


def configured() -> bool:
    return bool(relay_base() and relay_token())


def _request(path: str, params: dict | None = None, timeout: float = 30.0):
    """An opened urllib response for a relay GET. Caller must close (or use with).
    Raises the Relay* family — every message is token-free by construction."""
    base = relay_base()
    if not base:
        raise RelayDownError("Shuttle relay is not configured (shuttle_relay_url)")
    token = relay_token()
    if not token:
        raise RelayAuthError("Shuttle relay token missing (Shuttle app not set up?)")
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(500).decode("utf-8", "replace")
        except Exception:
            pass
        if e.code == 401:
            raise RelayAuthError("relay rejected the token") from None
        if e.code == 503:
            raise RelayBusyError("relay busy") from None
        raise RelayError(f"relay HTTP {e.code}: {body[:200]}") from None
    except OSError as e:
        raise RelayDownError(f"relay unreachable: {getattr(e, 'reason', e)}") from None


def relay_get_json(path: str, params: dict | None = None, timeout: float = 30.0) -> dict:
    with _request(path, params, timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def search(title: str, limit: int = SEARCH_LIMIT) -> list:
    """Seedbox name search for a movie title → candidate entries
    [{name, path, size, is_dir}, ...]. Directories are kept as candidates —
    pair() flattens the chosen one to its largest video file via the manifest."""
    q = (title or "").strip()
    if len(q) < 2:
        return []
    data = relay_get_json("/v1/search", {"q": q, "side": "seedbox", "limit": limit},
                          timeout=90.0)
    out = []
    for e in data.get("entries") or []:
        name = e.get("name") or ""
        if not e.get("is_dir") and not _is_video_name(name):
            continue
        out.append({"name": name, "path": e.get("path") or "",
                    "size": int(e.get("size") or 0), "is_dir": bool(e.get("is_dir"))})
    return out


def manifest(virtual: str) -> dict:
    """Flatten a seedbox path to its files: {"files": [{"rel","size"}], "bytes": n}."""
    return relay_get_json("/v1/manifest", {"path": virtual}, timeout=120.0)


def _is_video_name(name: str) -> bool:
    import movies
    return name.lower().endswith(movies._VID)


def resolve_candidate(virtual: str) -> dict | None:
    """A candidate (file OR release folder) → the actual video file to combine with:
    {"path", "name", "size"}. Folders resolve to their largest video file."""
    m = manifest(virtual)
    files = m.get("files") or []
    vids = [f for f in files if _is_video_name(f.get("rel") or "")]
    if not vids:
        return None
    best = max(vids, key=lambda f: int(f.get("size") or 0))
    rel = best["rel"]
    # manifest rels are relative to the asked path for folders; a plain file
    # manifests as itself (rel == basename)
    if rel == os.path.basename(virtual):
        path = virtual
    else:
        path = virtual.rstrip("/") + "/" + rel
    return {"path": path, "name": os.path.basename(rel), "size": int(best.get("size") or 0)}


def fetch_head(virtual: str, dest: str, max_bytes: int = HEAD_BYTES) -> str:
    """The first `max_bytes` of a seedbox file → `dest`. Closing the connection
    early is deliberate and safe: the relay kills its rclone pipe on hangup, so
    a head probe costs the seedbox only what we read."""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    got = 0
    with _request("/v1/fetch", {"path": virtual}, timeout=120.0) as r, open(dest, "wb") as f:
        while got < max_bytes:
            chunk = r.read(min(1 << 20, max_bytes - got))
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
    if got <= 0:
        raise RelayError("fetch returned no bytes")
    return dest


def fetch_to_file(virtual: str, dest: str, size: int, *,
                  on_progress=None, abort=None) -> tuple:
    """Full pull of one seedbox file → (ok, reason). Verified against the manifest
    size (seedbox responses are chunked — no Content-Length), written to .part and
    atomically renamed only on an exact match. No Range support upstream: a dropped
    connection deletes the partial and the CALLER retries from zero (stated in the
    reason so a flaky link is diagnosable). 503 busy → bounded backoff here."""
    if os.path.exists(dest) and os.path.getsize(dest) == size:
        return True, "already fetched"
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    part = dest + ".part"
    for attempt in range(FETCH_BUSY_TRIES):
        if abort is not None and abort.is_set():
            _rm(part)
            return False, "aborted"
        try:
            got = 0
            with _request("/v1/fetch", {"path": virtual}, timeout=300.0) as r, \
                 open(part, "wb") as f:
                while True:
                    if abort is not None and abort.is_set():
                        raise _Aborted()
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if on_progress:
                        on_progress(got, size)
            if got != size:
                _rm(part)
                return False, (f"fetch incomplete: {got} != {size} bytes "
                               "(no resume upstream — will restart)")
            os.replace(part, dest)
            return True, f"fetched {size} bytes"
        except _Aborted:
            _rm(part)
            return False, "aborted"
        except RelayBusyError:
            _rm(part)
            time.sleep(FETCH_BUSY_WAIT)
            continue
        except RelayError as e:
            _rm(part)
            return False, str(e)
        except OSError as e:
            _rm(part)
            return False, f"fetch failed: {e}"
    return False, "relay busy — download slots stayed full"


class _Aborted(Exception):
    pass


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ------------------------------------------------------------- probe + verdict

def probe_media(path: str, ffprobe: str = FFPROBE, name: str = "") -> dict:
    """One ffprobe pass → a normalized dict the verdict engine consumes.
    Works on a HEAD file (MKV puts tracks/duration up front); size should be
    passed via the caller when probing a truncated head (os.path.getsize of a
    head file is NOT the media size)."""
    import remux as remux_mod
    r = subprocess.run([ffprobe, "-v", "quiet", "-print_format", "json",
                        "-show_streams", "-show_format", path],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return {}
    data = json.loads(r.stdout or "{}")
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v:
        return {}
    try:
        duration = float(fmt.get("duration") or v.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    try:
        kbps = int(v.get("bit_rate") or 0) // 1000
    except (TypeError, ValueError):
        kbps = 0
    audio = []
    for s in streams:
        if s.get("codec_type") != "audio":
            continue
        prof = s.get("profile") or ""
        audio.append({
            "codec": s.get("codec_name") or "",
            "profile": prof,
            "channels": int(s.get("channels") or 0),
            # newer ffprobe reports "… + Dolby Atmos" in the profile; the release
            # name is the fallback for older builds
            "atmos": ("atmos" in prof.lower()) or ("atmos" in (name or "").lower()),
            "lang": ((s.get("tags") or {}).get("language") or ""),
        })
    return {
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "video_codec": v.get("codec_name") or "",
        "video_kbps": kbps,
        "duration": duration,
        "fps": v.get("r_frame_rate") or "",
        "transfer": v.get("color_transfer") or "",
        "dv_profile": remux_mod.dolby_vision_profile(v),   # "8.1"/"7.x"/... or None
        "audio": audio,
        "subs": sum(1 for s in streams if s.get("codec_type") == "subtitle"),
    }


def pedigree_rank(name: str) -> int:
    """Release pedigree from the filename: REMUX(3) > BluRay(2) > WEB(1) > other(0)."""
    n = (name or "").lower()
    if "remux" in n:
        return 3
    if "bluray" in n or "blu-ray" in n or "bdrip" in n:
        return 2
    if "web" in n:                       # WEB-DL / WEBDL / WEBRip
        return 1
    return 0


def audio_rank(track: dict) -> tuple:
    """Sortable quality of one audio track. The ladder is user-dictated:
    TrueHD Atmos > TrueHD > lossless PCM/FLAC > DTS-HD MA > EAC3 Atmos > DTS >
    EAC3 > AC3 > AAC > rest. Channels break ties."""
    codec = (track.get("codec") or "").lower()
    prof = (track.get("profile") or "").lower()
    atmos = bool(track.get("atmos"))
    if codec in ("truehd", "mlp"):
        base = 90 if atmos else 80
    elif codec == "flac" or codec.startswith("pcm"):
        base = 75
    elif codec == "dts" and "ma" in prof:
        base = 70
    elif codec == "eac3":
        base = 60 if atmos else 30
    elif codec == "dts":
        base = 40
    elif codec == "ac3":
        base = 20
    elif codec == "aac":
        base = 10
    else:
        base = 5
    return (base, int(track.get("channels") or 0))


def _audio_summary(track: dict) -> str:
    codec = (track.get("codec") or "?").upper()
    names = {"TRUEHD": "TrueHD", "EAC3": "DD+", "AC3": "AC-3", "DTS": "DTS"}
    label = names.get(codec, codec)
    if codec == "DTS" and "ma" in (track.get("profile") or "").lower():
        label = "DTS-HD MA"
    ch = track.get("channels") or 0
    chs = {8: "7.1", 6: "5.1", 2: "2.0"}.get(ch, f"{ch}ch")
    return label + (" Atmos" if track.get("atmos") else "") + " " + chs


def _side_spec(probe: dict, name: str) -> str:
    """One-line spec string for the verdict card: '2160p REMUX HEVC 58 Mbps · DV 7.x
    · TrueHD Atmos 7.1'."""
    if not probe:
        return "unreadable"
    h = probe.get("height") or 0
    res = f"{h}p" if h else "?"
    ped = {3: "REMUX", 2: "BluRay", 1: "WEB"}.get(pedigree_rank(name), "")
    rate = f"{probe.get('video_kbps', 0) / 1000:.0f} Mbps" if probe.get("video_kbps") else ""
    dv = f"DV {probe['dv_profile']}" if probe.get("dv_profile") else "HDR10" \
        if probe.get("transfer") == "smpte2084" else ""
    tracks = probe.get("audio") or []
    best = max(tracks, key=audio_rank) if tracks else None
    parts = [p for p in (res, ped, (probe.get("video_codec") or "").upper(), rate, dv) if p]
    if best:
        parts.append(_audio_summary(best))
    return " · ".join(parts)


# The re-encode prediction on the card is an ESTIMATE when the winner's 1-second
# peak hasn't been measured yet (that sweep needs the whole file): REMUX peaks
# routinely run ~2x their average, so anything whose average is within striking
# distance of the 72 Mbps ship gate is flagged. The gate itself is re-measured
# on the full file before anything ships — the card says so.
PEAK_ESTIMATE_FACTOR = 1.8


def build_verdict(nas_probe: dict, remote_probe: dict, nas_name: str,
                  remote_name: str, nas_peak_mbps: float | None = None) -> dict:
    """PURE. Decide what to keep from each copy. Sides are named "nas"/"remote";
    the pipeline maps them to actual files. Every choice carries a `why` string
    that the verdict card shows verbatim."""
    import dvcap

    # ---- video winner: pixels, then pedigree, then bitrate --------------------
    np_px = (nas_probe.get("width") or 0) * (nas_probe.get("height") or 0)
    rp_px = (remote_probe.get("width") or 0) * (remote_probe.get("height") or 0)
    n_ped, r_ped = pedigree_rank(nas_name), pedigree_rank(remote_name)
    n_kbps = nas_probe.get("video_kbps") or 0
    r_kbps = remote_probe.get("video_kbps") or 0
    if np_px != rp_px:
        video_from = "nas" if np_px > rp_px else "remote"
        video_why = "higher resolution"
    elif n_ped != r_ped:
        video_from = "nas" if n_ped > r_ped else "remote"
        video_why = "better source pedigree"
    elif n_kbps != r_kbps:
        video_from = "nas" if n_kbps > r_kbps else "remote"
        video_why = "higher video bitrate"
    else:
        video_from = "nas"
        video_why = "copies are equivalent — keeping the library file"
    winner_probe = nas_probe if video_from == "nas" else remote_probe
    loser_side = "remote" if video_from == "nas" else "nas"
    loser_probe = remote_probe if video_from == "nas" else nas_probe

    # ---- RPU source: real DV beats Resolve; graft follows the better base -----
    w_dv = winner_probe.get("dv_profile")
    l_dv = loser_probe.get("dv_profile")
    if w_dv:
        rpu_from, rpu_inline, rpu_profile = video_from, True, w_dv
        rpu_why = f"the {video_from} copy carries real Dolby Vision (P{w_dv})"
    elif l_dv:
        rpu_from, rpu_inline, rpu_profile = loser_side, False, l_dv
        rpu_why = (f"real Dolby Vision (P{l_dv}) grafted from the {loser_side} copy "
                   f"onto the better {video_from} video")
    else:
        rpu_from, rpu_inline, rpu_profile = "resolve", False, "resolve"
        rpu_why = "neither copy has real DV — Resolve analyzes the winner"

    # ---- audio donor: best single track wins the whole file's audio -----------
    n_best = max(nas_probe.get("audio") or [], key=audio_rank, default=None)
    r_best = max(remote_probe.get("audio") or [], key=audio_rank, default=None)
    if n_best is None and r_best is None:
        audio_from, audio_primary = video_from, {}
        audio_why = "no audio tracks probed"
    elif r_best is None or (n_best is not None and audio_rank(n_best) >= audio_rank(r_best)):
        audio_from, audio_primary = "nas", dict(n_best)
        audio_why = _audio_summary(n_best)
    else:
        audio_from, audio_primary = "remote", dict(r_best)
        audio_why = _audio_summary(r_best)
    audio_why += " — all of this copy's audio ships (compat tracks included)"

    # ---- re-encode prediction -------------------------------------------------
    gate = dvcap.RPU_SHIP_VIDEO_GATE_MBPS
    if video_from == "nas" and nas_peak_mbps:
        reencode = {"predicted": nas_peak_mbps > gate, "basis": "measured",
                    "mbps": round(nas_peak_mbps, 1)}
    else:
        avg = (winner_probe.get("video_kbps") or 0) / 1000.0
        reencode = {"predicted": bool(avg and avg * PEAK_ESTIMATE_FACTOR > gate),
                    "basis": "estimate", "mbps": round(avg, 1)}

    return {
        "video_from": video_from, "video_why": video_why,
        "rpu_from": rpu_from, "rpu_profile": rpu_profile, "rpu_inline": rpu_inline,
        "rpu_why": rpu_why,
        "audio_from": audio_from, "audio_why": audio_why,
        "audio_primary": {"codec": audio_primary.get("codec", ""),
                          "atmos": bool(audio_primary.get("atmos"))},
        "subs_from": video_from,
        "reencode": reencode,
        "container": ".mkv",
        "specs": {"nas": _side_spec(nas_probe, nas_name),
                  "remote": _side_spec(remote_probe, remote_name)},
    }


# ---------------------------------------------------------------------- book

_BOOK_LOCK = threading.Lock()
_INFLIGHT = set()               # basenames with a worker running (search or pair)
_INFLIGHT_LOCK = threading.Lock()


def _book() -> dict:
    try:
        with open(BOOK_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    os.makedirs(os.path.dirname(BOOK_FILE), exist_ok=True)
    tmp = BOOK_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, BOOK_FILE)


def mark(basename: str, status: str | None = None, **kw) -> None:
    with _BOOK_LOCK:
        d = _book()
        e = d.setdefault(basename, {})
        if status:
            e["status"] = status
        e.update(kw)
        _save(d)


def entry(basename: str) -> dict:
    with _BOOK_LOCK:
        return dict(_book().get(basename) or {})


def unpair(basename: str) -> None:
    with _BOOK_LOCK:
        d = _book()
        if basename in d:
            del d[basename]
            _save(d)


def confirmed_verdict(basename: str) -> dict | None:
    """The verdict + companion of a CONFIRMED pairing, or None. The pipeline's
    combine stages treat a missing/unconfirmed entry as a permanent park — never
    guess tracks."""
    e = entry(basename)
    if e.get("status") == "confirmed" and e.get("verdict") and e.get("companion"):
        return {"verdict": e["verdict"], "companion": e["companion"]}
    return None


def book_view() -> dict:
    """Poll-safe view for the app: everything the UI needs, nothing bulky."""
    with _BOOK_LOCK:
        d = _book()
    out = {}
    for name, e in d.items():
        out[name] = {
            "status": e.get("status") or "",
            "title": e.get("title") or "",
            "candidates": [{"name": c.get("name"), "size": c.get("size"),
                            "path": c.get("path"), "is_dir": c.get("is_dir", False)}
                           for c in (e.get("candidates") or [])[:8]],
            "companion": e.get("companion"),
            "verdict": e.get("verdict"),
            "error": e.get("error"),
        }
    return out


def confirm(basename: str) -> dict:
    """Flip a READY pairing to confirmed (the card's Confirm button). The caller
    (server) then queues the movie with combine=True."""
    e = entry(basename)
    if e.get("status") != "ready":
        return {"status": "error", "error": f"not ready (status={e.get('status') or 'none'})"}
    mark(basename, "confirmed", confirmed_at=time.time())
    logbook.event(f"companion {basename}: combine confirmed — "
                  f"video={e['verdict']['video_from']} rpu={e['verdict']['rpu_from']} "
                  f"audio={e['verdict']['audio_from']}")
    return {"status": "confirmed"}


# ------------------------------------------------------------- async workers

def _claim(basename: str) -> bool:
    with _INFLIGHT_LOCK:
        if basename in _INFLIGHT:
            return False
        _INFLIGHT.add(basename)
        return True


def _release(basename: str) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT.discard(basename)


def start_search(basename: str, nas_dir: str, title: str) -> dict:
    """Kick an async seedbox search for a movie's companion. Idempotent while
    one is running."""
    if not configured():
        mark(basename, "error", title=title, dir=nas_dir,
             error="Shuttle relay not configured")
        return {"status": "error", "error": "Shuttle relay not configured"}
    if not _claim(basename):
        return {"status": "busy"}
    mark(basename, "searching", title=title, dir=nas_dir, error=None,
         searched_at=time.time())

    def work():
        try:
            import movies
            cands = search(movies.movie_title(basename))
            mark(basename, "found" if cands else "error",
                 candidates=cands,
                 error=None if cands else "no seedbox match for this title")
        except RelayError as e:
            mark(basename, "error", error=str(e))
        except Exception as e:               # never leave a stuck "searching"
            mark(basename, "error", error=f"search failed: {e}")
        finally:
            _release(basename)

    threading.Thread(target=work, daemon=True, name=f"companion-search-{basename[:24]}").start()
    return {"status": "searching"}


def pair(basename: str, candidate_path: str) -> dict:
    """Pair a movie with a chosen seedbox candidate: resolve folders to the video
    file, head-probe BOTH copies, build the verdict → status 'ready' for the card."""
    if not _claim(basename):
        return {"status": "busy"}
    mark(basename, "probing", error=None)

    def work():
        import scratch
        import transfer
        headdir = os.path.join(scratch.default_scratch(), ".companion-heads")
        try:
            comp = resolve_candidate(candidate_path)
            if not comp:
                mark(basename, "error", error="no video file inside that candidate")
                return
            e = entry(basename)
            # -- remote head --------------------------------------------------
            rhead = os.path.join(headdir, "remote.bin")
            fetch_head(comp["path"], rhead)
            remote_probe = probe_media(rhead, name=comp["name"])
            # -- NAS head (over Visionary's own FTP — the relay never sees it) --
            nhead = os.path.join(headdir, "nas.bin")
            nas_remote = (e.get("dir") or "").rstrip("/") + "/" + basename
            ok, why = transfer.download_head(nas_remote, nhead, HEAD_BYTES)
            nas_probe = probe_media(nhead, name=basename) if ok else {}
            if not remote_probe or not nas_probe:
                which = "companion" if not remote_probe else "library copy"
                mark(basename, "error", companion=comp,
                     error=f"could not probe the {which}"
                           + ("" if ok else f" ({why})"))
                return
            # duration sanity: > 2 s apart usually means different cuts
            cut_note = None
            if nas_probe.get("duration") and remote_probe.get("duration"):
                if abs(nas_probe["duration"] - remote_probe["duration"]) > 2.0:
                    cut_note = "runtimes differ — likely different cuts"
            verdict = build_verdict(nas_probe, remote_probe, basename, comp["name"],
                                    nas_peak_mbps=_known_peak(basename))
            if cut_note:
                verdict["cut_warning"] = cut_note
            mark(basename, "ready", companion=comp, verdict=verdict,
                 local_probe=nas_probe, remote_probe=remote_probe, error=None)
        except RelayError as e:
            mark(basename, "error", error=str(e))
        except Exception as e:
            mark(basename, "error", error=f"pairing failed: {e}")
        finally:
            shutil.rmtree(headdir, ignore_errors=True)
            _release(basename)

    threading.Thread(target=work, daemon=True, name=f"companion-pair-{basename[:24]}").start()
    return {"status": "probing"}


def _known_peak(basename: str) -> float | None:
    """A previously-measured 1-second peak for this source, if the peak book has
    one (stages writes it after any full sweep)."""
    try:
        with open(os.path.expanduser("~/.topaz-pipeline/source_peaks.json")) as f:
            v = (json.load(f) or {}).get(basename)
        return float(v) if isinstance(v, (int, float)) and v > 0 else None
    except (OSError, ValueError, TypeError):
        return None
