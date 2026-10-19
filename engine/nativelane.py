"""The native Dolby Vision lane: which TV episodes take it, and the on-disk record that says so.

settings `dv_engine` = 'native' sends an ELIGIBLE TV episode's DV analysis through dvnative
(hdr_analyzer_mvp + the pinned SDR->PQ LUT, no screen takeover) instead of Resolve. With the
default 'resolve' nothing here is consulted for a new item, so every path stays as it was.
dv_routes {"sd_sdr_topaz": "native"} (2026-10-18) opens the lane to 480p/576p episodes as well,
with a BT.601 -> BT.709 chunk front end (see "the SD route" below); its own switch, not dv_engine's.
This module is the lane's bookkeeping; the run thread and the stages call it:

  decide(p)        after Topaz: ('native' | 'resolve', why) for this item
  run_analysis(p)  the 'dvnative' stage body: chunks -> analysis -> RPU -> ready marker
  ready(p)         the analysis is published AND still matches what is on disk
  bundle(p)        what remux.remux(native=) needs
  disk_fits(p)     the throughput guard: room to keep this item's chunks through its remux
  held_native_bytes() / held_native_segdirs()   chunks other native items still hold

THE MARKER (segdir/native/native.json) is the source of truth for the engine choice — not a
finisher_queue field and not the live setting. Once an item has one it stays native whatever
the setting says now (a fallback.json beside it is the only way back to Resolve). It records
every input the analysis depends on (manifest, chunk size+mtime, LUT and analyzer hashes, the
filter chain, dovi_tool version, avg floor, fps, the CFR's frames/fps/size/mtime), is
'analyzing' while measuring and becomes 'ready' only after the RPU is generated, checked and
hashed — the ready write is published last, atomically.

Everything lives inside the Topaz segdir, so the hand-off drop and cleanup that rmtree it take
the lane's files with it:
  segdir/native/native.json    the marker
  segdir/native/work/          analyze_chunks' per-chunk sidecars (its resume state)
  segdir/native/rpu.bin        the generated RPU (+ .generate.json, the dovi_tool config)
  segdir/native/sidecar.json   the merged sidecar it was generated from
  segdir/native/failures.json  failed native attempts (MAX_FAILURES -> fallback)
  segdir/native/fallback.json  this item goes through Resolve, whatever the setting says
  segdir/native/cpu.json       CPU ledger: os.wait4 user+sys per analysis chunk / remux segment
"""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import threading
import time
from fractions import Fraction

import dvcap
import dvnative
import logbook
import scratch
import settings
import topaz
import versions

GiB = 1024 ** 3

NATIVE_DIR = "native"
MARKER = "native.json"
WORK_DIR = "work"
RPU = "rpu.bin"
SIDECAR = "sidecar.json"
FAILURES = "failures.json"
FALLBACK = "fallback.json"
CPU_LEDGER = "cpu.json"

MARKER_VERSION = 1
AVG_FLOOR = "V40"               # CM v4.0 L1 floor: the blind-tested native clip's setting
PEAK_ESTIMATOR = "max"          # dvnative.analyze_chunks' default, pinned into the marker
WITH_L4 = True

MAX_FAILURES = 2                # the 2nd failed native attempt sends the item to Resolve for good
FALLBACK_PREFIX = "native-fallback:"   # a stage reason that falls back at once (no retry helps)

TOOLCHAIN_CACHE_SECONDS = 600   # toolchain_ok() re-checks at most this often
CLAIM_TOOLCHAIN_SECONDS = 60    # ...but a NEW claim needs a result at most this old (decide, then
                                # run_analysis's first pass); a sibling's lease re-picks every 10 s
QUICK_TIMEOUT = 10              # seconds for each `--help` / `--version` probe

LAST_CHUNK_SHORT_MAX = 8        # Topaz's own acceptance for the last chunk (topaz.py): it reads to
LAST_CHUNK_LONG_MAX = 1         # EOF, so it may land n-8..n+1 frames against the planned total
CFR_SHORT_TOLERANCE = 0.995     # = orchestrator.RENDER_SHORT/LONG_TOLERANCE: the chunks must be
CFR_LONG_TOLERANCE = 1.005      # the CFR's frames (its audio rides on them), as a render must be

ELIGIBLE_RES = ("720p", "1080p")   # plan buckets; 480p/576p sources are BT.601-era SD
UNKNOWN_TAGS = (None, "", "unknown", "unspecified", "reserved")
MIN_CHUNK_HEIGHT = 720

# ---- the SD route (2026-10-18) -----------------------------------------------------------
# sd_sdr_topaz — a TV episode from the 480p/576p bucket (BT.601-era SD) — takes this lane too, with
# dvnative.chunk_front_end in front of the LUT (BT.601 -> BT.709 by the standard conversion; square
# pixels for an anamorphic DVD). Its switch is settings dv_routes {"sd_sdr_topaz": "native"} —
# NEVER dv_engine, which owns the 720p+ episodes (and is already 'native' live): flipping one never
# moves the other's items. The front end is decided once, at the claim, from chunk 0's tags and the
# source's frame, and lives in the marker ("pre_vf"; vf and analysis_vf carry it), so a claimed SD
# item resumes, remuxes and is checked with exactly the chain it was measured with whatever the
# switch says later. A marker without "pre_vf" is byte for byte the TV lane's.
SD_ROUTE = "sd_sdr_topaz"          # = routes.py's id (test-pinned)
SD_RES = "480p"                    # = routes.SD_RES: plan.resolution_bucket's SD bucket (480p and 576p)
_SD_CLAIMS = {}                    # segdir -> the front end decide() approved for an SD claim, which
_SD_LOCK = threading.Lock()        # run_analysis' first pass (the next call, same pass) writes into the
                                   # marker; a TV claim is never in it, so the TV path reads nothing new

# ---- disk (GiB, like every gate in the engine) -------------------------------------------
# U2, user-decided "Allow below 400 GB": while a finisher-owned native item holds its ProRes,
# the NEXT item may start when raw free covers ITS OWN projected need (orchestrator.
# _projected_item_gb: ProRes estimate + ITEM_TAIL_GB), never less than NEXT_ITEM_MIN_GB —
# instead of the flat min(OVERLAP_MIN_PHYS_GB, min_free_gb). disk_fits() asks the same question
# before an item goes native, with this item's own ProRes size standing in for the next one's.
ITEM_TAIL_GB = 120              # = orchestrator.ITEM_TAIL_GB (test-pinned): the two must agree
NEXT_ITEM_MIN_GB = 150          # the hard minimum of that need
DISK_MARGIN_GB = 20             # held back from the projection (remux transients, rounding)
MID_ANALYSIS_FLOOR_GB = 150     # before each analysis intermediate: free - its size >= this
INTERMEDIATE_BYTES_PER_FRAME = int(6.5 * 1024 ** 2)
                                # FFVHuff 1080p 4:4:4 10-bit: 2.3 MB/frame on S06E06, ~1.5x the
                                # FFV1 it replaced; FFV1 measured ~4.1 MB/frame on S06E05's grainy
                                # 7,058-frame chunk, so ~6.3 there — rounded up to 6.5 MiB

_PIN_DEFAULTS = {
    "DOVI_TOOL_VERSION": "2.3.0",
    "HDR_ANALYZER_PATH": "~/.topaz-pipeline/bin/hdr_analyzer_mvp",
    "HDR_ANALYZER_COMMIT": "383b0ad",
    "HDR_ANALYZER_SHA256": "41727312f4a960b27d59cb0c0f24c2922dc82768da3503f083e4842a87fc099e",
    "NATIVE_LUT_PATH": "~/.topaz-pipeline/luts/rcm_sdr709_to_pq1000.cube",
    "NATIVE_LUT_SHA256": "e410e5b7b899a07096a77e2f65bdeb6a4ab3cc60126358a81c8050251d8c59b0",
    "NATIVE_FFMPEG_MAJOR": 8,
}

wait4_cpu = dvnative.wait4_cpu  # reap a child with os.wait4 -> its user+sys CPU seconds

_LOCK = threading.Lock()        # the small JSON books (failures, CPU ledger)
_SHA_LOCK = threading.Lock()
_SHA_CACHE = {}                 # (path, size, mtime_ns) -> sha256 hex
_TOOLCHAIN = {"at": None, "result": None}


def pin(name: str):
    """A native-lane pin from versions.py, read at USE time (the defaults are the same values,
    for a versions.py that predates them). *_PATH pins come back user-expanded."""
    v = getattr(versions, name, _PIN_DEFAULTS[name])
    return os.path.expanduser(v) if name.endswith("_PATH") else v


def analyzer_path() -> str:
    return pin("HDR_ANALYZER_PATH")


def lut_path() -> str:
    return pin("NATIVE_LUT_PATH")


# ---- small file helpers ------------------------------------------------------------------

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def sha256_cached(path: str):
    """sha256 of a file, cached by (path, size, mtime_ns) so the 7 MB LUT is hashed once per
    change, not on every ready() call. None when the file cannot be read."""
    try:
        st = os.stat(path)
        key = (os.path.abspath(path), st.st_size, st.st_mtime_ns)
        with _SHA_LOCK:
            if key in _SHA_CACHE:
                return _SHA_CACHE[key]
        digest = sha256_file(path)
    except (OSError, TypeError):
        return None
    with _SHA_LOCK:
        if len(_SHA_CACHE) > 64:
            _SHA_CACHE.clear()
        _SHA_CACHE[key] = digest
    return digest


def _read_json(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path: str, data, indent=1) -> None:
    """Atomic: .part, then os.replace — a kill never leaves a half-written book or marker."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".part", "w") as f:
        json.dump(data, f, indent=indent)
    os.replace(path + ".part", path)


def _rm(*paths) -> None:
    for path in paths:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _segdir(p) -> str:
    return p if isinstance(p, str) else p.segdir


def native_dir(p) -> str:
    return os.path.join(_segdir(p), NATIVE_DIR)


def marker_path(p) -> str:
    return os.path.join(native_dir(p), MARKER)


def work_dir(p) -> str:
    return os.path.join(native_dir(p), WORK_DIR)


def rpu_path(p) -> str:
    return os.path.join(native_dir(p), RPU)


def sidecar_path(p) -> str:
    return os.path.join(native_dir(p), SIDECAR)


def fallback_path(p) -> str:
    return os.path.join(native_dir(p), FALLBACK)


def failures_path(p) -> str:
    return os.path.join(native_dir(p), FAILURES)


def cpu_path(p) -> str:
    return os.path.join(native_dir(p), CPU_LEDGER)


# ---- the setting and eligibility ---------------------------------------------------------

def engine_setting() -> str:
    """The live dv_engine ('resolve' | 'native'), validated; anything unreadable is 'resolve'."""
    try:
        fn = getattr(settings, "dv_engine", None)
        v = fn() if callable(fn) else settings.get_settings().get("dv_engine")
    except Exception:  # noqa: BLE001 — a settings hiccup must never pick the new path
        return "resolve"
    return "native" if v == "native" else "resolve"


def sd_route_on() -> bool:
    """settings dv_routes sets sd_sdr_topaz 'native' (validated on read); anything unreadable is
    False — a settings hiccup must never pick the new path."""
    try:
        return settings.dv_route(SD_ROUTE) == "native"
    except Exception:  # noqa: BLE001
        return False


def lane_on() -> bool:
    """Could a NEW item take the lane at all: dv_engine 'native' (the 720p+ episodes) or the SD
    route switched on. dv_engine is read first, so with it 'native' this is the one read it
    always was."""
    return engine_setting() == "native" or sd_route_on()


def effective_mode(p) -> str:
    """The Resolve-equivalent output mode stages._resolve would use for this TV item: the
    per-show override when it is a real mode, else dv1000 ("auto" is always 1000 nits). The
    2000-nit mode is retired (user decision 2026-10-15): a show still pinned to it is dv1000."""
    try:
        override = settings.get_show_output_mode(p.series)
    except Exception:  # noqa: BLE001
        override = "auto"
    return override if override in ("sdr", "dv1000") else "dv1000"


def eligible(p, plan: dict, mode: str, *, sd: bool = False) -> tuple:
    """PURE. (True, "eligible") when the item itself qualifies for the native lane, else
    (False, why). TV only (no movie, YouTube or combine); the Topaz UPSCALE path (fast paths
    rpu-only / resolve-only and already-DV are not); SDR intake ('add_hdr_dv' — the LUT is
    BT.709 SDR -> PQ, so HDR intake 'add_dv' is out); a 720p+ source — or, with `sd` (the caller
    read the SD route's switch: on), a 480p/576p one, which then takes the chunk front end; and
    the dv1000 output mode (sdr stays on Resolve). Chunk colour, the toolchain and disk are
    decide()'s further checks."""
    if getattr(p, "movie", False):
        return False, "a movie (the native lane is TV only)"
    if getattr(p, "youtube", False):
        return False, "a YouTube video (the native lane is TV only)"
    if getattr(p, "combine", False):
        return False, "a companion combine (the native lane is TV only)"
    pl = plan or {}
    if pl.get("topaz") != "upscale":
        return False, f"plan topaz is {pl.get('topaz')!r}, not the Topaz upscale path"
    if pl.get("resolve") != "add_hdr_dv":
        return False, (f"plan resolve is {pl.get('resolve')!r}: not an SDR intake "
                       "(the LUT converts BT.709 SDR only)")
    if pl.get("res") not in ELIGIBLE_RES and not (sd and pl.get("res") == SD_RES):
        return False, f"a {pl.get('res') or 'unknown'}-bucket source (480p/BT.601 stays on Resolve)"
    if mode != "dv1000":
        return False, f"output mode is {mode}, not dv1000"
    return True, "eligible"


def sd_front(info: dict, source: dict | None) -> tuple:
    """PURE. An SD claim's chunk 0 (ffprobe tags, with sample_aspect_ratio) and the source's probe
    -> (True, pre_vf) — dvnative.chunk_front_end's chain, '' when the chunk needs none — or
    (False, why): unprobed, under MIN_CHUNK_HEIGHT lines, or a colour no standard front end makes
    the LUT's BT.709 input (HDR, BT.2020, another transfer)."""
    if not info:
        return False, "chunk 0 could not be probed"
    try:
        height = int(info.get("height") or 0)
    except (TypeError, ValueError):
        height = 0
    if height < MIN_CHUNK_HEIGHT:
        return False, f"chunk 0 is {height} lines, under {MIN_CHUNK_HEIGHT}"
    try:
        return True, dvnative.chunk_front_end(info, source)
    except ValueError as e:
        return False, f"chunk 0: {e}"


def source_ok(info: dict) -> tuple:
    """PURE. Chunk 0's stream tags (ffprobe) -> (ok, why). Topaz keeps the source's colour
    tags, and the LUT is BT.709 limited-range only: BT.709 or untagged primaries and matrix,
    limited or untagged range, an SDR transfer, at least 720 lines."""
    if not info:
        return False, "chunk 0 could not be probed"
    try:
        height = int(info.get("height") or 0)
    except (TypeError, ValueError):
        height = 0
    if height < MIN_CHUNK_HEIGHT:
        return False, f"chunk 0 is {height} lines, under {MIN_CHUNK_HEIGHT}"
    for key, label in (("color_primaries", "primaries"), ("color_space", "matrix")):
        v = info.get(key)
        if v not in UNKNOWN_TAGS and v != "bt709":
            return False, f"chunk 0 {label} are {v}, not BT.709 (the LUT is BT.709 only)"
    if info.get("color_transfer") in ("smpte2084", "arib-std-b67"):
        return False, f"chunk 0 is already HDR ({info.get('color_transfer')})"
    if info.get("color_range") == "pc":
        return False, "chunk 0 is full range (the LUT expects limited range)"
    return True, "BT.709 limited"


def probe_stream(path: str, extra: tuple = ()) -> dict:
    """Chunk 0's video stream tags; {} when unreadable. `extra` asks for more stream fields
    (the native render's fit front end wants sample_aspect_ratio); without it the probe is
    exactly the lane's own."""
    try:
        out = subprocess.run(
            [dvcap.FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,pix_fmt,r_frame_rate,color_primaries,color_space,"
             "color_transfer,color_range" + "".join("," + str(e) for e in extra), "-of", "json", path],
            capture_output=True, text=True, timeout=60).stdout
        return (json.loads(out).get("streams") or [{}])[0]
    except Exception:  # noqa: BLE001
        return {}


# ---- chunks, CFR -------------------------------------------------------------------------

def chunks(segdir: str, expected: int | None = None) -> list:
    """Topaz's chunks as dvcap's decode list, from segments.json (ACTUAL frame counts, never
    topaz_segbounds' planned ones): [[first_frame, path], ..., [total, None]]. Raises
    ValueError on a missing manifest or chunk file, a gap or overlap between chunks, or a
    manifest total that disagrees with its chunks. With `expected` (the CFR's frame count,
    which Topaz planned against) the total may land LAST_CHUNK_SHORT_MAX short or
    LAST_CHUNK_LONG_MAX long of it — Topaz's own acceptance for the EOF-bounded last chunk."""
    m = topaz.read_manifest(segdir)
    if not isinstance(m, dict) or not isinstance(m.get("segments"), list) or not m["segments"]:
        raise ValueError("no segments.json with chunks")
    out, expect = [], 0
    for i, s in enumerate(m["segments"]):
        try:
            start, frames, name = int(s["start"]), int(s["frames"]), str(s["file"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"chunk {i}: malformed manifest entry")
        if start != expect:
            raise ValueError(f"chunk {i} starts at frame {start}, expected {expect} "
                             f"({'a gap' if start > expect else 'an overlap'})")
        if frames <= 0:
            raise ValueError(f"chunk {i} has {frames} frames")
        path = os.path.join(segdir, name)
        if not os.path.isfile(path):
            raise ValueError(f"chunk {i} ({name}) is missing")
        out.append([start, path])
        expect = start + frames
    if m.get("total_frames") is not None and int(m["total_frames"]) != expect:
        raise ValueError(f"manifest total {m['total_frames']} != its chunks' {expect}")
    if expected:
        if not (int(expected) - LAST_CHUNK_SHORT_MAX <= expect <= int(expected) + LAST_CHUNK_LONG_MAX):
            raise ValueError(f"the chunks hold {expect} frames, the source {expected} "
                             f"(the last chunk may be {LAST_CHUNK_SHORT_MAX} short at most)")
    out.append([expect, None])
    return out


def chunk_bytes(segdir: str) -> int:
    """Bytes of the manifest's chunk files (0 when there is no manifest)."""
    m = topaz.read_manifest(segdir)
    total = 0
    for s in (m.get("segments") if isinstance(m, dict) else None) or []:
        try:
            total += os.path.getsize(os.path.join(segdir, str(s.get("file"))))
        except (OSError, AttributeError):
            pass
    return total


def normalize_fps(rate):
    """PURE. "24000/1001" / Fraction / "24" -> "num/den" in lowest terms; None if unknown."""
    try:
        f = Fraction(str(rate).strip())
    except (ValueError, ZeroDivisionError):
        return None
    return f"{f.numerator}/{f.denominator}" if f > 0 else None


def fps_fraction(path: str):
    """A file's r_frame_rate as "num/den", or None."""
    try:
        out = subprocess.run([dvcap.FFPROBE, "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=60).stdout
        tok = ((out or "").splitlines() or [""])[0].split(",")[0].strip()   # ffprobe 8 trails a comma
        return normalize_fps(tok) if tok and tok != "0/0" else None
    except Exception:  # noqa: BLE001
        return None


def cfr_info(p) -> dict:
    """The CFR source's facts the analysis is pinned to: frame count (topaz._frame_count —
    the count Topaz planned with, no decode), fps fraction, size and mtime."""
    path = getattr(p, "source_cfr", None)
    info = {"path": path, "frames": None, "fps": None, "size": None, "mtime_ns": None}
    try:
        st = os.stat(path)
    except (OSError, TypeError):
        return info
    n = topaz._frame_count(path, decode=False)
    info.update(frames=n if n and n > 0 else None, fps=fps_fraction(path),
                size=st.st_size, mtime_ns=st.st_mtime_ns)
    return info


def cfr_check(total: int, cfr_frames, chunk_fps, cfr_fps) -> tuple:
    """PURE. The chunks against the CFR their audio comes from: total within 0.995..1.005 of
    the CFR's frames, and chunk 0's frame rate EXACTLY the CFR's (as a fraction). Unlike the
    render check, unknown is a failure here — the remux needs both numbers, and the fallback
    (Resolve) is always available."""
    if not cfr_frames:
        return False, "the CFR's frame count is unknown"
    # Exact fractions: as a float 1.005 is 1.00499..., which refused 40,200 against 40,000.
    lo, hi = Fraction(str(CFR_SHORT_TOLERANCE)), Fraction(str(CFR_LONG_TOLERANCE))
    if not (cfr_frames * lo <= total <= cfr_frames * hi):
        return False, f"the chunks hold {total} frames, the CFR {cfr_frames}"
    a, b = normalize_fps(chunk_fps) if chunk_fps else None, normalize_fps(cfr_fps) if cfr_fps else None
    if a is None or b is None:
        return False, f"frame rate unknown (chunk {chunk_fps}, CFR {cfr_fps})"
    if a != b:
        return False, f"chunk 0 runs at {a}, the CFR at {b}"
    return True, f"{total} frames at {a}"


# ---- disk --------------------------------------------------------------------------------

def next_item_need_gb(projected_gb) -> int:
    """PURE. U2: the raw free the NEXT item needs to start while a native item holds its
    chunks — its projected need (orchestrator._projected_item_gb), never under 150 GiB."""
    try:
        return max(NEXT_ITEM_MIN_GB, int(projected_gb))
    except (TypeError, ValueError):
        return NEXT_ITEM_MIN_GB


def disk_verdict(phys_free_gb, prefetch_gb, held_gb, own_prores_gb) -> tuple:
    """PURE. (fits, projected, need). projected = raw free + the prefetch buffer (purged
    whenever the pipeline needs room) + chunks other native items hold (freed when their remux
    passes) - DISK_MARGIN_GB. need = next_item_need_gb(this item's ProRes + ITEM_TAIL_GB): the
    next item's projected need, with this item's own ProRes size as its estimate."""
    projected = int(phys_free_gb) + int(prefetch_gb or 0) + int(held_gb or 0) - DISK_MARGIN_GB
    need = next_item_need_gb(int(own_prores_gb) + ITEM_TAIL_GB)
    return projected >= need, projected, need


def held_native_segdirs(root: str | None = None, exclude: str | None = None,
                        states=("ready",)) -> list:
    """Segdirs under the scratch root that carry a native marker in one of `states` (other
    than `exclude`). 'ready' = analysed and handed off (or about to be): their chunks stay
    on disk until their remux passes. Pass states=None for every marker."""
    base = root or scratch.default_scratch()
    skip = os.path.abspath(exclude) if exclude else None
    out = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for name in names:
        d = os.path.join(base, name)
        if not (name.endswith(".segments") and os.path.isdir(d)) or os.path.abspath(d) == skip:
            continue
        m = read_marker(d)
        if m is not None and (states is None or m.get("state") in states):
            out.append(d)
    return out


def held_native_bytes(root: str | None = None, exclude: str | None = None) -> int:
    """Bytes of Topaz chunks that 'ready' native items still hold (see held_native_segdirs)."""
    return sum(chunk_bytes(d) for d in held_native_segdirs(root, exclude))


def disk_fits(p, *, phys_free_gb=None, prefetch_gb=None, held_gb=None) -> tuple:
    """(fits, why): is there room to keep this item's chunks through its remux and still
    start the next item (disk_verdict)? A throughput guard only — when it says no, the item
    simply goes through Resolve; the run-thread gates keep the disk safe either way."""
    own = chunk_bytes(p.segdir)
    if own <= 0:
        return False, "no chunks on disk to keep"
    base = os.path.dirname(os.path.abspath(p.segdir))
    if phys_free_gb is None:
        phys_free_gb = scratch.physical_free_gb(base)
        if phys_free_gb is None:
            return False, "free space is unreadable"
    if prefetch_gb is None:
        try:
            prefetch_gb = scratch.folder_used_gb(scratch.prefetch_dir())
        except OSError:
            prefetch_gb = 0
    if held_gb is None:
        held_gb = held_native_bytes(base, exclude=p.segdir) // GiB
    own_gb = -(-own // GiB)
    fits, projected, need = disk_verdict(phys_free_gb, prefetch_gb, held_gb, own_gb)
    why = (f"projected {projected} GiB free (raw {phys_free_gb} + prefetch {prefetch_gb} + "
           f"held native {held_gb} - {DISK_MARGIN_GB} margin) "
           f"{'>=' if fits else '<'} {need} GiB the next item needs (this item's "
           f"{own_gb} GiB ProRes + {ITEM_TAIL_GB})")
    return fits, why


def floor_reason(free_bytes: int, frames: int, k: int = 0) -> str | None:
    """PURE. The mid-analysis floor: None when writing chunk k's intermediate (frames x
    INTERMEDIATE_BYTES_PER_FRAME) still leaves MID_ANALYSIS_FLOOR_GB free, else a "paused:"
    reason."""
    need = int(frames) * INTERMEDIATE_BYTES_PER_FRAME
    left = int(free_bytes) - need
    if left >= MID_ANALYSIS_FLOOR_GB * GiB:
        return None
    return (f"paused: low disk — analysis chunk {k + 1} needs ~{need / GiB:.0f} GiB and would "
            f"leave {max(0, left) / GiB:.0f} GiB free, under the {MID_ANALYSIS_FLOOR_GB} GiB floor")


def disk_floor_hook(path: str, free_fn=None):
    """dvnative.analyze_chunks' before_chunk: floor_reason against live free space on `path`'s
    volume. An unreadable volume does not pause (the step itself then fails, and is counted)."""
    def before_chunk(k, frames):
        try:
            free = free_fn() if free_fn else shutil.disk_usage(path).free
        except OSError:
            return None
        return floor_reason(free, frames, k)
    return before_chunk


# ---- toolchain ---------------------------------------------------------------------------

def _run_quick(cmd: list) -> tuple:
    """(returncode, stdout+stderr) of a short probe; (None, error) when it cannot run."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=QUICK_TIMEOUT, stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)


def parse_dovi_version(text: str):
    """PURE. "dovi_tool 2.3.0" -> "2.3.0"."""
    m = re.search(r"dovi_tool\s+v?(\d+(?:\.\d+)+)", text or "")
    return m.group(1) if m else None


def parse_ffmpeg_major(text: str):
    """PURE. "ffmpeg version 8.0 Copyright ..." -> 8 (also "n8.0.1", "8.0-tessus")."""
    m = re.search(r"ffmpeg version n?(\d+)\.", text or "")
    return int(m.group(1)) if m else None


def toolchain_problems() -> list:
    """Every way the installed toolchain differs from the native pins (versions.py), [] when
    it matches: the analyzer's build (sha256) AND that it still runs (`--help`; a Homebrew
    ffmpeg upgrade breaks its libav* links without changing its hash), the LUT's sha256 and a
    path ffmpeg's filter syntax can carry, dovi_tool's version, ffmpeg's major version,
    ffprobe, and numpy for the L4 child. Independent of preflight on purpose."""
    problems = []
    an = analyzer_path()
    if not (os.path.isfile(an) and os.access(an, os.X_OK)):
        problems.append(f"hdr_analyzer_mvp is missing at {an}")
    elif sha256_cached(an) != pin("HDR_ANALYZER_SHA256"):
        problems.append(f"hdr_analyzer_mvp is not the pinned build (commit {pin('HDR_ANALYZER_COMMIT')})")
    else:
        rc, out = _run_quick([an, "--help"])
        if rc != 0:
            problems.append("hdr_analyzer_mvp does not run (was ffmpeg upgraded under it?): "
                            + (out or "").strip()[-160:])
    lut = lut_path()
    if not os.path.isfile(lut):
        problems.append(f"the LUT is missing at {lut}")
    elif sha256_cached(lut) != pin("NATIVE_LUT_SHA256"):
        problems.append("the LUT is not the pinned file (sha256 differs)")
    else:
        try:
            dvnative.lut_vf(lut)
        except ValueError as e:
            problems.append(str(e))
    rc, out = _run_quick([dvnative.DOVI_TOOL, "--version"])
    got = parse_dovi_version(out) if rc == 0 else None
    if got != pin("DOVI_TOOL_VERSION"):
        problems.append(f"dovi_tool is {got or 'missing'}, pinned {pin('DOVI_TOOL_VERSION')}")
    rc, out = _run_quick([dvnative.FFMPEG, "-version"])
    major = parse_ffmpeg_major(out) if rc == 0 else None
    if major != pin("NATIVE_FFMPEG_MAJOR"):
        problems.append(f"ffmpeg major is {major or 'missing'}, pinned {pin('NATIVE_FFMPEG_MAJOR')}")
    if not os.path.isfile(dvcap.FFPROBE):
        problems.append(f"ffprobe is missing at {dvcap.FFPROBE}")
    if importlib.util.find_spec("numpy") is None:
        problems.append("numpy is not importable (the L4 pass needs it)")
    return problems


def toolchain_ok(*, force: bool = False, now: float | None = None,
                 max_age: float = TOOLCHAIN_CACHE_SECONDS) -> tuple:
    """(ok, why), cached for TOOLCHAIN_CACHE_SECONDS: decide() asks on every eligible item,
    and the probes spawn four processes. max_age narrows the cache for one call (decide()
    wants a result at most CLAIM_TOOLCHAIN_SECONDS old before it lets an item be claimed);
    force=True always re-checks (run_analysis does before it counts a failure)."""
    now = time.monotonic() if now is None else now
    with _LOCK:
        at, result = _TOOLCHAIN["at"], _TOOLCHAIN["result"]
    if not force and at is not None and result is not None and now - at < max_age:
        return result
    problems = toolchain_problems()
    result = (not problems, "; ".join(problems) if problems else "toolchain matches the pins")
    with _LOCK:
        _TOOLCHAIN["at"], _TOOLCHAIN["result"] = now, result
    return result


# ---- the marker --------------------------------------------------------------------------

def read_marker(p):
    m = _read_json(marker_path(p))
    return m if isinstance(m, dict) else None


def has_marker(p) -> bool:
    """This segdir has been claimed by the native lane (in any state)."""
    return os.path.isfile(marker_path(p))


def marker_id(marker: dict) -> str:
    """PURE. sha256 of every field but "id" (canonical JSON): the marker's identity, which the
    remux's resume manifest keys on (native:<id>)."""
    body = {k: v for k, v in marker.items() if k != "id"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_marker(p, marker: dict) -> dict:
    """Atomically write the marker. A 'ready' marker gets its id here, so it is always the
    hash of exactly what was written."""
    m = dict(marker)
    m.pop("id", None)
    if m.get("state") == "ready":
        m["id"] = marker_id(m)
    _write_json(marker_path(p), m)
    return m


def chunk_stats(chunk_list: list) -> list:
    """[[file, size, mtime_ns], ...] of the real chunks (the "chunk size:mtime list")."""
    out = []
    for _a, path in chunk_list:
        if path:
            st = os.stat(path)
            out.append([os.path.basename(path), st.st_size, st.st_mtime_ns])
    return out


def marker_inputs(p, chunk_list: list, fps: str, cfr: dict, pre_vf: str | None = None) -> dict:
    """Everything the published analysis depends on. Any change re-measures from scratch.
    `pre_vf` (an SD claim's chunk front end) prefixes vf and analysis_vf and is recorded as
    "pre_vf" — only when there is one, so a TV marker is exactly what it always was."""
    lut, an = lut_path(), analyzer_path()
    m = {
        "version": MARKER_VERSION,
        "manifest_sha256": sha256_file(os.path.join(_segdir(p), topaz.MANIFEST)),
        "chunks": chunk_stats(chunk_list),
        "total": int(chunk_list[-1][0]),
        "fps": fps,
        "lut_path": lut, "lut_sha256": sha256_cached(lut),
        "vf": dvnative.front_end(pre_vf) + dvnative.lut_vf(lut),
        "analysis_vf": dvnative.analysis_vf(lut, pre_vf),
        "analyzer_commit": pin("HDR_ANALYZER_COMMIT"), "analyzer_sha256": sha256_cached(an),
        "peak_estimator": PEAK_ESTIMATOR, "with_l4": WITH_L4,
        "dovi_tool_version": pin("DOVI_TOOL_VERSION"),
        "avg_floor": AVG_FLOOR,
        "cfr": {"frames": cfr.get("frames"), "fps": cfr.get("fps"),
                "size": cfr.get("size"), "mtime_ns": cfr.get("mtime_ns")},
    }
    if pre_vf:
        m["pre_vf"] = pre_vf
    return m


def _clear_outputs(p, *, work: bool) -> None:
    _rm(rpu_path(p), rpu_path(p) + ".part", rpu_path(p) + ".generate.json", sidecar_path(p))
    if work:
        shutil.rmtree(work_dir(p), ignore_errors=True)


def begin_marker(p, chunk_list: list, fps: str, cfr: dict, pre_vf: str | None = None) -> dict:
    """Claim the item: write the 'analyzing' marker. When an older marker was made from other
    inputs (re-Topaz'd chunks, a new LUT or analyzer, another front end), its per-chunk results,
    RPU and sidecar are deleted first so nothing stale is reused; with the same inputs the work
    dir is kept and the analysis resumes chunk by chunk."""
    inputs = marker_inputs(p, chunk_list, fps, cfr, pre_vf)
    old = read_marker(p)
    same = (old is not None and all(old.get(k) == v for k, v in inputs.items())
            and old.get("pre_vf") == inputs.get("pre_vf"))
    _clear_outputs(p, work=not same)
    return write_marker(p, dict(inputs, state="analyzing"))


def publish_ready(p, sc: dict) -> dict:
    """After generate_rpu: the RPU must cover exactly the chunks' frames; then the merged
    sidecar is saved and the marker turns 'ready' with the RPU/sidecar hashes, MaxCLL and its
    id — the last write of the analysis."""
    m = read_marker(p)
    if not m or m.get("state") != "analyzing":
        raise RuntimeError("no 'analyzing' marker to publish")
    rpu = rpu_path(p)
    got = dvcap.rpu_frame_count(rpu, dovi_tool=dvnative.DOVI_TOOL)
    if got != m["total"]:
        raise RuntimeError(f"the generated RPU has {got} frames, the chunks {m['total']}")
    _write_json(sidecar_path(p), sc, indent=None)     # per-frame L4: tens of thousands of rows
    m.update(state="ready", rpu_sha256=sha256_file(rpu), sidecar_sha256=sha256_file(sidecar_path(p)),
             max_cll=dvnative.x265_max_cll(sc), shots=len(sc.get("scenes") or []))
    return write_marker(p, m)


def _chunk_drift(p, m: dict):
    """Why the chunks on disk are not the set marker `m` recorded (segments.json's sha256, every
    chunk's size+mtime), or None when they still are. Raises OSError when they can't be read."""
    seg = _segdir(p)
    if sha256_file(os.path.join(seg, topaz.MANIFEST)) != m.get("manifest_sha256"):
        return "segments.json changed"
    for name, size, mtime_ns in m.get("chunks") or []:
        st = os.stat(os.path.join(seg, name))
        if (st.st_size, st.st_mtime_ns) != (size, mtime_ns):
            return f"chunk {name} changed"
    return None


def check_ready(p) -> tuple:
    """(ready, why). Ready = a 'ready' marker whose id still hashes its fields AND whose inputs
    still match the disk: segments.json, every chunk's size+mtime, the LUT's sha256 and filter
    chain, the RPU's sha256, and the CFR's size+mtime (its frames/fps were checked when the
    marker was written). Anything else means the RPU is not this item's analysis."""
    m = read_marker(p)
    if not m:
        return False, "no native marker"
    if m.get("state") != "ready":
        return False, f"native marker is {m.get('state')!r}, not ready"
    if m.get("version") != MARKER_VERSION or m.get("id") != marker_id(m):
        return False, "native marker does not hash to its id"
    try:
        drift = _chunk_drift(p, m)
    except OSError as e:
        return False, f"chunks unreadable: {e}"
    if drift:
        return False, drift
    lut = m.get("lut_path") or ""
    if sha256_cached(lut) != m.get("lut_sha256"):
        return False, "the LUT changed"
    try:
        if dvnative.front_end(m.get("pre_vf")) + dvnative.lut_vf(lut) != m.get("vf"):
            return False, "the LUT filter chain changed"
    except ValueError:
        return False, "the LUT path is unusable"
    if sha256_cached(rpu_path(p)) != m.get("rpu_sha256"):
        return False, "the RPU changed"
    cfr = m.get("cfr") or {}
    try:
        st = os.stat(getattr(p, "source_cfr", "") or "")
        if (st.st_size, st.st_mtime_ns) != (cfr.get("size"), cfr.get("mtime_ns")):
            return False, "the CFR source changed"
    except OSError:
        return False, "the CFR source is missing"
    return True, "ready"


def ready(p) -> bool:
    return check_ready(p)[0]


def bundle(p):
    """remux.remux(native=) input, or None when not ready(p):
    {chunks: [[first, path], ..., [total, None]], vf: the LUT chain, rpu: path, fps: "n/d",
     max_cll: "cll,fall" | None, total, bounds: chunk END frames (the segment plan), id}."""
    if not ready(p):
        return None
    m = read_marker(p)
    try:
        chunk_list = chunks(_segdir(p))
    except ValueError:
        return None
    if chunk_list[-1][0] != m["total"]:
        return None
    return {"chunks": chunk_list, "vf": m["vf"], "rpu": rpu_path(p), "fps": m["fps"],
            "max_cll": m.get("max_cll"), "total": m["total"],
            "bounds": [a for a, _p in chunk_list[1:]], "id": m["id"]}


# ---- failures and fallback ---------------------------------------------------------------

def failure_count(p) -> int:
    book = _read_json(failures_path(p))
    try:
        return int((book or {}).get("count", 0))
    except (TypeError, ValueError, AttributeError):
        return 0


def count_failure(p, why: str) -> int:
    """Record one failed native attempt; returns the new count."""
    with _LOCK:
        n = failure_count(p) + 1
        _write_json(failures_path(p), {"count": n, "last": str(why)[:300]})
    return n


def should_fall_back(msg: str, count: int) -> bool:
    """PURE. After a failed native attempt: give up on native for this item when the stage
    said so ("native-fallback:" — a retry cannot help) or it has failed MAX_FAILURES times.
    A "paused:" result is never a failure and must not be passed here."""
    return str(msg or "").startswith(FALLBACK_PREFIX) or int(count) >= MAX_FAILURES


def has_fallback(p) -> bool:
    return os.path.isfile(fallback_path(p))


def write_fallback(p, why: str) -> None:
    """Send this item to Resolve for good: fallback.json first (decide() reads it before the
    marker, so a crash between the two writes still means Resolve), then the marker and the
    lane's outputs go. The chunks are untouched — Resolve needs them."""
    _write_json(fallback_path(p), {"why": str(why)[:300], "failures": failure_count(p)})
    _rm(marker_path(p))
    _clear_outputs(p, work=True)


# ---- CPU ledger --------------------------------------------------------------------------

def record_cpu(p, phase: str, key: str, cpu_s: float, frames: int) -> dict:
    """Book one measured unit (an analysis chunk, a remux segment) into segdir/native/cpu.json
    as {phase: {key: {cpu_s, frames}}}; a re-measured key replaces the old entry. Returns the
    phase totals (cpu_totals)."""
    with _LOCK:
        book = _read_json(cpu_path(p))
        book = book if isinstance(book, dict) else {}
        if not isinstance(book.get(phase), dict):
            book[phase] = {}
        book[phase][key] = {"cpu_s": round(float(cpu_s or 0.0), 3), "frames": int(frames or 0)}
        _write_json(cpu_path(p), book)
    return cpu_totals(p, phase)


def cpu_totals(p, phase: str) -> dict:
    book = _read_json(cpu_path(p))
    rows = (book or {}).get(phase) if isinstance(book, dict) else None
    rows = [r for r in rows.values() if isinstance(r, dict)] if isinstance(rows, dict) else []
    cpu = sum(float(r.get("cpu_s") or 0) for r in rows)
    frames = sum(int(r.get("frames") or 0) for r in rows)
    return {"cpu_s": round(cpu, 1), "frames": frames, "units": len(rows),
            "ms_per_frame": round(cpu * 1000.0 / frames, 2) if frames else None}


def cpu_line(p, phase: str) -> str:
    t = cpu_totals(p, phase)
    per = f"{t['ms_per_frame']} ms/frame" if t["ms_per_frame"] is not None else "no frames"
    return f"{phase} CPU {t['cpu_s']:.0f} s over {t['frames']} frames ({per}, {t['units']} units)"


# ---- the decision and the stage body -----------------------------------------------------

def decide(p, *, pl: dict | None = None, mode: str | None = None) -> tuple:
    """('native' | 'resolve', why) for an item whose Topaz is done. In order:
      fallback.json present            -> resolve (sticky, manual or after failures)
      a native marker present          -> native (claimed: the setting no longer matters)
      dv_engine != 'native' and the SD route off
                                       -> resolve (today's path, byte for byte)
      eligible() / chunk 0 colour / chunks readable / toolchain_ok() / disk_fits() / the
      toolchain once more, at most CLAIM_TOOLCHAIN_SECONDS old
                                       -> native only when all pass
    A 720p+ episode needs dv_engine 'native'; a 480p/576p one needs dv_routes sd_sdr_topaz
    'native' (sd_route_on, read only for an SD source while dv_engine is 'native') and passes
    sd_front instead of source_ok — its chunk front end is what run_analysis' first pass then
    writes into the marker (_SD_CLAIMS). A broken toolchain is a per-item, THIS-TIME answer: the
    item goes through Resolve and nothing is written (no fallback.json), so items take the lane
    again once it is fixed. The orchestrator logs `why` (once per reason per item). `pl`
    (plan.plan_for of the ORIGINAL source) and `mode` may be passed when the caller already has
    them."""
    if has_fallback(p):
        return "resolve", "native fell back for this item (native/fallback.json)"
    if has_marker(p):
        return "native", "claimed by the native lane (native/native.json)"
    tv_on = engine_setting() == "native"
    sd_on = None                            # the SD route's switch, read only when it can matter
    if not tv_on:
        sd_on = sd_route_on()
        if not sd_on:
            return "resolve", "dv_engine is 'resolve'"
    if pl is None:
        import plan as _plan
        pl = _plan.plan_for(p.source)
    sd = pl.get("res") == SD_RES and (sd_route_on() if sd_on is None else sd_on)
    ok, why = eligible(p, pl, effective_mode(p) if mode is None else mode, sd=sd)
    if not ok:
        return "resolve", why
    if not sd and not tv_on:
        return "resolve", (f"dv_engine is 'resolve' (route {SD_ROUTE}, switched on, takes 480p/576p "
                           "episodes only)")
    try:
        chunk_list = chunks(p.segdir)
    except ValueError as e:
        return "resolve", f"chunks: {e}"
    if sd:
        ok, front = sd_front(probe_stream(chunk_list[0][1], extra=("sample_aspect_ratio",)),
                             pl.get("input"))
        if not ok:
            return "resolve", front
    else:
        ok, why = source_ok(probe_stream(chunk_list[0][1]))
        if not ok:
            return "resolve", why
    ok, why = toolchain_ok()                # cached: a toolchain known to be broken spawns nothing
    if ok:
        ok, why = disk_fits(p)
        if not ok:
            return "resolve", f"disk: {why}"
        ok, why = toolchain_ok(max_age=CLAIM_TOOLCHAIN_SECONDS)   # fresh, before the claim
    if not ok:
        return "resolve", (f"the native toolchain is not ready, so Resolve takes this item this "
                           f"time (no fallback.json: the lane takes new items again once it is "
                           f"fixed): {why}")
    if sd:
        with _SD_LOCK:
            _SD_CLAIMS[os.path.abspath(p.segdir)] = front
        return "native", ("eligible (SD: " + ("BT.601 -> BT.709 / square-pixel front end"
                                              if front else "already square BT.709") + ")")
    return "native", "eligible"


def sd_claim(p):
    """The chunk front end decide() approved for this SD item's claim ('' = none needed), or None
    when decide() approved no SD claim for it (every TV item): a dict read, nothing else."""
    with _SD_LOCK:
        return _SD_CLAIMS.get(os.path.abspath(_segdir(p)))


def _toolchain_drift(marker: dict) -> list:
    """On a RESUME: the files the analysis began with that are no longer those files — the
    analyzer build and the LUT, by sha256 against the marker (a file that is gone counts).
    [] when both are unchanged, or the marker never recorded them (unreadable: can't prove)."""
    out = []
    for label, path, key in (("hdr_analyzer_mvp build", analyzer_path(), "analyzer_sha256"),
                             ("LUT", lut_path(), "lut_sha256")):
        if marker.get(key) and sha256_cached(path) != marker[key]:
            out.append(label)
    return out


def _resume_chunk_drift(p, marker: dict):
    """On a RESUME: _chunk_drift, where a chunk set that is GONE (segments.json or a chunk
    deleted) has changed too, but one that merely can't be read right now, or a marker that
    never recorded it, proves nothing (None)."""
    if not marker.get("manifest_sha256"):
        return None
    try:
        return _chunk_drift(p, marker)
    except FileNotFoundError as e:
        return f"{os.path.basename(e.filename or '') or 'a chunk'} is gone"
    except OSError:
        return None


def _toolchain_failure(claimed: bool, marker: dict, why: str) -> str:
    """The stage reason for a failed toolchain check (flow step a). Never a fallback.json on
    the FIRST claim: nothing is claimed yet and the failure is cached, so the next pass's
    decide() sends the item through Resolve this time only. On a RESUME only a definitive
    mismatch falls back at once — the analyzer build or LUT the analysis began with is not on
    disk any more, so no chunk measured so far is valid. Anything else (a probe that hung or
    failed, dovi_tool/ffmpeg beside them) is an ordinary counted failure: the 2-failure rule
    decides, and one hiccup throws no finished chunk away."""
    if not claimed:
        return (f"native toolchain not ready ({why}) — the item is not claimed; the next pass "
                f"sends it through Resolve (no fallback.json)")
    drift = _toolchain_drift(marker)
    if drift:
        return (f"{FALLBACK_PREFIX} toolchain: the {' and the '.join(drift)} changed since this "
                f"item's analysis began ({why})")
    return f"native toolchain check failed while resuming ({why}); the next pass checks again"


def _input_failure(claimed: bool, p, marker: dict, why: str) -> str:
    """The stage reason for a failed chunk/CFR check (flow steps b-c). The FIRST claim falls
    back at once: the item's own inputs don't fit the lane. On a RESUME they fitted when the
    marker was written, so only a chunk set that changed since then falls back at once; with
    the same chunks the failure came from a probe (ffprobe's frame count or rate — a None is
    a hiccup, not proof), so it is an ordinary counted failure and the work dir is kept."""
    if not claimed:
        return f"{FALLBACK_PREFIX} {why}"
    drift = _resume_chunk_drift(p, marker)
    if drift:
        return f"{FALLBACK_PREFIX} {why} ({drift} since the analysis began)"
    return f"{why} (while resuming; the next pass checks again)"


def run_analysis(p, abort=None, progress=None, should_pause=None) -> tuple:
    """The 'dvnative' stage body -> (ok, msg). Steps (design flow 2a-g):
      a. re-check the toolchain: a new claim trusts only a result CLAIM_TOOLCHAIN_SECONDS old
         (decide() just made one), a resume a cached pass; a failure is always re-probed
      b. chunk list from segments.json vs the CFR
      c. CFR frames (0.995..1.005) and exact fps
         a-c on the FIRST claim (no marker yet): a toolchain failure is a plain reason (the
         next decide() sends the item to Resolve, no fallback.json); a chunk/CFR misfit is
         "native-fallback: ...". On a RESUME (a marker exists) only a definitive mismatch is
         "native-fallback: ..." — the analyzer build or LUT sha256 changed, or the chunk set
         changed — and every probe failure is a plain reason, counted by the caller
         (_toolchain_failure / _input_failure)
      d. write the 'analyzing' marker (claims the item) — with an SD item's chunk front end
         (decide()'s, on the first claim; the marker's own on a resume)
      e. dvnative.analyze_chunks into segdir/native/work: nice 10, killable, per-step
         timeouts, the 150 GiB mid-analysis floor, L4 in a child, CPU booked per chunk
      f. generate_rpu (CM v4.0 floor V40); its frame count must equal the chunks'
      g. the 'ready' marker, published last
    A pause (should_pause / low disk) returns "paused: ..." and an abort "aborted (run
    stopped)": neither is a failure, and the next call resumes chunk by chunk. Any other
    failure is a plain reason: the caller counts it (count_failure / should_fall_back)."""
    ep = getattr(p, "ep", "?")
    if ready(p):
        return True, "native DV metadata already ready"
    claimed = has_marker(p)                 # a marker (any state): this pass is a RESUME
    marker = (read_marker(p) or {}) if claimed else {}
    ok, why = toolchain_ok(max_age=TOOLCHAIN_CACHE_SECONDS if claimed else CLAIM_TOOLCHAIN_SECONDS)
    if not ok:
        ok, why = toolchain_ok(force=True)  # never count a failure that is only a cached one
    if not ok:
        return False, _toolchain_failure(claimed, marker, why)
    cfr = cfr_info(p)
    try:
        chunk_list = chunks(p.segdir, expected=cfr.get("frames"))
    except ValueError as e:
        return False, _input_failure(claimed, p, marker, f"chunks: {e}")
    total = chunk_list[-1][0]
    fps = fps_fraction(chunk_list[0][1])
    ok, why = cfr_check(total, cfr.get("frames"), fps, cfr.get("fps"))
    if not ok:
        return False, _input_failure(claimed, p, marker, why)
    # The chunk front end: a claimed item's is its marker's (none for a TV one); an SD item's
    # first claim takes the one decide() approved — never re-decided, so a resume measures with
    # exactly the chain the analysis began with.
    pre_vf = (marker.get("pre_vf") if claimed else sd_claim(p)) or None
    begin_marker(p, chunk_list, fps, cfr, pre_vf)
    if not claimed:
        with _SD_LOCK:
            _SD_CLAIMS.pop(os.path.abspath(p.segdir), None)
    ends = [a for a, _p in chunk_list[1:]]

    def on_chunk(done, of):
        if progress:
            d = {"stage": "dvnative", "ep": ep, "pct": min(100, round(ends[done - 1] / total * 100)),
                 "notches": [round(e / total, 4) for e in ends], "seg_done": done, "seg_total": of}
            if done < of:
                d["seg_rem_pct"] = (ends[done] - ends[done - 1]) / total * 100
            progress(d)

    def on_cpu(k, cpu_s, frames):
        record_cpu(p, "analysis", f"chunk_{k:04d}", cpu_s, frames)

    if progress:
        progress({"stage": "dvnative", "ep": ep, "pct": 0, "seg_done": 0, "seg_total": len(ends),
                  "notches": [round(e / total, 4) for e in ends]})
    try:
        sc = dvnative.analyze_chunks(chunk_list, work_dir(p), cube=lut_path(), analyzer=analyzer_path(),
                                     peak_estimator=PEAK_ESTIMATOR, with_l4=WITH_L4, abort=abort,
                                     should_pause=should_pause, before_chunk=disk_floor_hook(p.segdir),
                                     on_chunk=on_chunk, on_cpu=on_cpu,
                                     **({"pre_vf": pre_vf} if pre_vf else {}))
    except dvnative.Paused as e:
        msg = str(e)
        return False, msg if msg.startswith("paused:") else "paused: " + msg
    except dvnative.Aborted:
        return False, "aborted (run stopped)"
    except (RuntimeError, ValueError, OSError) as e:
        return False, f"native analysis failed: {e}"
    try:
        dvnative.generate_rpu(sc, rpu_path(p), avg_floor=AVG_FLOOR, l4=WITH_L4)
        m = publish_ready(p, sc)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as e:
        return False, f"native RPU failed: {e}"
    logbook.event(f"dvnative {ep}: {cpu_line(p, 'analysis')}")
    return True, (f"native DV metadata ready: {len(ends)} chunks, {total} frames, "
                  f"{m.get('shots')} shots, MaxCLL {m.get('max_cll') or 'unmeasured'}"
                  + (" (SD chunk front end)" if pre_vf else ""))
