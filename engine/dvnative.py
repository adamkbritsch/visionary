"""Native Dolby Vision metadata: measure the picture ourselves, no Resolve Analyze.

WHY: Resolve's DV pass is the slowest and most fragile stage (full-screen UI automation,
template matching, ~2 min scene cuts + ~7 min Analyze + 4-14 min render), and its HEVC
render is thrown away anyway — dvcap re-encodes it with x265 under the peak cap. What Resolve
really contributes is (1) the SDR->PQ conversion, (2) shot detection, (3) DV analysis. This
module replaces (2) and (3); (1) is a LUT fitted to Resolve's own output, applied by ffmpeg
(`lut_vf`).

HOW:
  1. hdr-analyze's `hdr_analyzer_mvp` (MIT, github.com/tinof/hdr-analyze, built from source at
     a pinned commit — HDR_ANALYZER_COMMIT) measures the PQ picture: scene cuts plus per-scene
     L1 (min / max-RGB average / max, 12-bit PQ codes) into a JSON sidecar.
  2. `frame_l4()` measures, per frame, the mean and standard deviation of PQ max(R,G,B) —
     the two values Resolve's L4 (anchor_pq / anchor_power) track on our own masters
     (black-box: r = 0.999 and 0.97 against pixel truth on S06E01). Plain measurement; the
     meaning of L4 comes from a public Dolby patent (US 12573352), so patent risk is noted.
  3. `rpu_config()` turns both into a `dovi_tool generate` config. It started as a port of
     hdr-analyze's mkvdovi/src/metadata/rpu_config.rs::generate_extra_json (MIT, Copyright
     (c) 2025 HDR-Analyze Contributors), with the static blocks set to what Resolve writes
     (CM v4.0, source range 7..3079, L9 BT.2020, L11 content type 0, L5 zero, a scene refresh
     on every frame) and L1 clamped to the declared 1000-nit source range.
  4. `dovi_tool generate` writes the binary RPU, which dvcap's x265 path interleaves exactly
     like an RPU extracted from a Resolve render.

WHAT IT DOES NOT DO: Dolby's CM v4 analysis. Resolve's L1 average is Dolby's "anchored"
value (not a mean), and its L2 trims and L3 offsets come from Dolby's licensed analyzer;
none of that is reproduced. L1 here is a true measurement (average floored at the CM v4
minimum 1229, which is also Resolve's own minimum) and L2 is neutral (2048). Whether that
looks as good on a real TV is decided by eye — the blind A/B — before any native master
ships (settings `dv_engine`).
"""
from __future__ import annotations
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

import versions as _versions

DOVI_TOOL = "/opt/homebrew/bin/dovi_tool"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
# The analyzer binary and its commit are pinned in versions.py (the native lane's toolchain
# pins). Read with defaults so this module still imports beside an older versions.py.
HDR_ANALYZER = os.path.expanduser(getattr(_versions, "HDR_ANALYZER_PATH",
                                          "~/.topaz-pipeline/bin/hdr_analyzer_mvp"))
HDR_ANALYZER_COMMIT = getattr(_versions, "HDR_ANALYZER_COMMIT", "383b0ad")
                                    # tinof/hdr-analyze, built from source (cargo, Homebrew ffmpeg 8)
ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))

# What Resolve's 1000-nit DV project writes (read from Resolve masters' RPUs with
# `dovi_tool export`): mastering 0.0001..1000 nits, BT.2020 source primaries (L9 index 2),
# L11 content type 0 / whitepoint 0 / reference mode off, L5 all zero, CM v4.0, and a scene
# refresh flagged on EVERY frame (L1 held constant within a shot).
MASTER_MIN_NITS = 0.0001
MASTER_MAX_NITS = 1000
SOURCE_PRIMARY_BT2020 = 2
L11_CONTENT_TYPE = 0
TRIM_TARGETS_NITS = (100, 600, 1000)
NEUTRAL = 2048                  # L2 trim field value meaning "no trim"
PQ12_MAX = 4095


def nits_to_pq12(nits: float) -> int:
    """PURE. Absolute luminance -> 12-bit PQ code (SMPTE ST 2084), rounded. 1000 nits -> 3079,
    100 -> 2081, 600 -> 2851, 0.0001 -> 7 (the codes Resolve's RPUs carry)."""
    m1, m2 = 2610.0 / 16384.0, 2523.0 / 32.0
    c1, c2, c3 = 3424.0 / 4096.0, 2413.0 / 128.0, 2392.0 / 128.0
    y = max(0.0, min(float(nits), 10000.0)) / 10000.0
    ym = y ** m1
    return int(round(((c1 + c2 * ym) / (1.0 + c3 * ym)) ** m2 * PQ12_MAX))


SOURCE_MAX_PQ = nits_to_pq12(MASTER_MAX_NITS)      # 3079
SOURCE_MIN_PQ = nits_to_pq12(MASTER_MIN_NITS)      # 7


# ---- colour: the captured Resolve conversion ---------------------------------------------
# Topaz's ProRes is BT.709 limited 4:2:2. The LUT maps BT.709 R'G'B' to BT.2020 PQ R'G'B'
# (Resolve's SDR->PQ, fitted to its own renders). zscale does ONLY matrix/range work around
# it: the first instance turns YUV into RGB with transfer and primaries left alone; the
# second declares its input as already PQ/BT.2020, so it converts the matrix and range and
# nothing else. (Leaving the second zscale to read the frame's stale BT.709 tags made it
# apply 709->PQ and 709->2020 again on top of the LUT — review-caught.)

_SAFE_PATH = re.compile(r"^[A-Za-z0-9_./+-]+$")


def _lut_arg(cube: str) -> str:
    if not _SAFE_PATH.match(cube or ""):
        raise ValueError(f"LUT path must be plain (no spaces/quotes) for an ffmpeg filter: {cube!r}")
    return cube


def lut_rgb_chain(cube: str) -> str:
    """BT.709 limited YUV -> RGB float -> LUT. Output: BT.2020 PQ R'G'B' in gbrpf32le."""
    return ("zscale=matrixin=709:rangein=limited,format=gbrpf32le,"
            f"lut3d=file={_lut_arg(cube)}:interp=tetrahedral")


def pq_rgb_to_yuv(pix_fmt: str) -> str:
    """BT.2020 PQ R'G'B' -> BT.2020nc limited YUV in `pix_fmt`, with no transfer/primaries change."""
    return ("zscale=transferin=smpte2084:primariesin=2020:transfer=smpte2084:primaries=2020:"
            f"matrix=2020_ncl:range=limited,format={pix_fmt}")


def lut_vf(cube: str, pix_fmt: str = "yuv420p10le") -> str:
    """The full SDR->PQ chain for an encode: Topaz ProRes -> BT.2020 PQ YUV."""
    return lut_rgb_chain(cube) + "," + pq_rgb_to_yuv(pix_fmt)


# ---- hdr-analyze ------------------------------------------------------------------------

def build_analyzer_command(analyzer: str, src: str, out_bin: str, *, peak_estimator: str = "max",
                           downscale: int = 1, threads: int | None = None,
                           frame_stats: str | None = None) -> list:
    """`src` must be PQ. --transfer pq because a raw/re-wrapped input can lose its colour
    tags; --no-crop because Resolve writes L5 = 0 (full frame) and Topaz output is full frame."""
    cmd = [analyzer, "-i", src, "-o", out_bin, "--transfer", "pq", "--no-crop",
           "--peak-estimator", peak_estimator, "--downscale", str(int(downscale))]
    if peak_estimator != "max":
        cmd += ["--peak-source", "max"]          # the estimator acts in the direct peak domain
    if threads:
        cmd += ["--analysis-threads", str(int(threads))]
    if frame_stats:
        cmd += ["--dump-frame-stats", frame_stats]
    return cmd


def sidecar_path(out_bin: str) -> str:
    return out_bin + ".l1.json"


def load_sidecar(path: str) -> dict:
    """The analyzer's L1 sidecar, validated: contiguous scenes from frame 0, each with the
    three L1 codes. Raises ValueError on anything a generated RPU could silently get wrong."""
    with open(path) as f:
        sc = json.load(f)
    validate_sidecar(sc)
    return sc


def validate_sidecar(sc: dict) -> None:
    scenes = sc.get("scenes") or []
    if not scenes:
        raise ValueError("sidecar has no scenes")
    expect = 0
    for i, s in enumerate(scenes):
        for k in ("start", "end", "min_pq_12bit", "max_pq_12bit", "avg_max_rgb_pq_12bit"):
            if not isinstance(s.get(k), int):
                raise ValueError(f"scene {i}: missing {k}")
        if s["start"] != expect or s["end"] < s["start"]:
            raise ValueError(f"scene {i}: starts at {s['start']}, expected {expect}")
        expect = s["end"] + 1


def stream_frames(sc: dict) -> int:
    """Frames the RPU must cover: the stream's picture count when the analyzer recorded it
    (it includes undecodable leading pictures), else the measured scenes' extent."""
    src = sc.get("source") or {}
    measured = sc["scenes"][-1]["end"] + 1
    return int(src.get("stream_frames") or measured)


def leading_frames(sc: dict) -> int:
    return int((sc.get("source") or {}).get("leading_skipped_frames") or 0)


def light_levels(sc: dict):
    """(MaxCLL, MaxFALL) in nits, clamped to the declared mastering range: MaxCLL <= 1000,
    MaxFALL <= MaxCLL. (None, None) when unmeasured. A measured 8498 on a 1000-nit master
    (the pilot) would tell HDR10 players to tone-map 1000-nit content as 8500-nit."""
    ll = sc.get("light_level") or {}
    cll, fall = ll.get("max_cll_nits"), ll.get("max_fall_nits")
    if not cll or not fall or cll < 1 or fall < 1:
        return (None, None)
    cll = min(int(cll), MASTER_MAX_NITS)
    return (cll, min(int(fall), cll))


def x265_max_cll(sc: dict):
    """The x265 --max-cll string for the HDR10 SEI, from the same clamped values as L6."""
    cll, fall = light_levels(sc)
    return f"{cll},{fall}" if cll else None


# ---- per-chunk analysis -----------------------------------------------------------------
# hdr_analyzer_mvp drops frames on a piped input (measured: a 72-frame y4m pipe read as 1
# frame, NUT as 21), so each Topaz chunk is rendered to a lossless FILE first: the PQ
# picture after the LUT, area-downscaled IN RGB to 1080p, then 4:4:4 (no chroma-subsampling
# overshoot in max-RGB), FFVHuff: lossless like the FFV1 it replaced (pixel-identical output,
# identical analyzer and L4 results), but ~5x cheaper to encode and to decode — FFV1's own
# coding was ~240 of the ~500 ms of CPU each frame cost, and it was decoded twice (analyzer, L4).
# ~2.3 MB/frame on S06E06 (FFV1: 1.5); grain costs it more than FFV1, and a chunk has no upper
# length: Topaz closes it at the first strong cut at least ~90 s in, so a quiet stretch runs
# long (S06E05's 7,058-frame chunk made a 26-28 GiB FFV1 intermediate). At most two exist,
# deleted as soon as they are measured, and never started without room: the caller's
# before_chunk hook checks free space against the chunk's frames first (the native lane's
# 150 GiB floor). Topaz cuts its chunks at strong scene changes, so every chunk start is a
# scene start and per-chunk results join without a seam.
#
# Every step is a child process run by run_step(): at nice 10 in its own process group, polled
# so an abort or should_pause kills it (and anything it started) within a second, and bounded
# by step_timeout(). The per-frame L4 pass runs in a child too (`python3 -m dvnative l4`), so
# its numpy work and memory stay out of the engine process.

ANALYSIS_WIDTH, ANALYSIS_HEIGHT = 1920, 1080
NICE = 10                       # analysis children run below x265 and Topaz
NICE_BIN = "/usr/bin/nice"
POLL_SECONDS = 0.5              # how often a running step checks abort / should_pause / time
STEP_BASE_SECONDS = 300         # per-step timeout = this + frames / STEP_FRAMES_PER_SECOND:
STEP_FRAMES_PER_SECOND = 5      # far above any healthy step, so only a wedged child trips it
OUTPUT_TAIL_BYTES = 1000        # how much of a failed child's output a reason quotes


class Paused(Exception):
    """analyze_chunks stopped cleanly: should_pause or before_chunk asked it to. The step in
    flight was killed and its partial files removed; every published chunk is kept, so the
    next call resumes at the chunk that was interrupted. str() starts with "paused:"."""


class Aborted(RuntimeError):
    """The run was stopped (abort set) mid-analysis; the step in flight was killed."""


def step_timeout(frames) -> int:
    """PURE. Seconds one analysis step of a `frames`-frame chunk may take: 300 + frames / 5,
    rounded up (7,058 frames -> 1,712 s)."""
    return STEP_BASE_SECONDS + -(-max(0, int(frames or 0)) // STEP_FRAMES_PER_SECOND)


def niced(cmd: list, nice: int = NICE) -> list:
    """PURE. `cmd` prefixed to run at `nice` (0 = unchanged). nice(1) execs the command, so
    the child keeps the pid Popen reports, and its own children inherit the priority."""
    return [NICE_BIN, "-n", str(int(nice)), *cmd] if nice else list(cmd)


def wait4_cpu(proc, block: bool = True):
    """Reap `proc` with os.wait4 and return the user+sys CPU seconds it used, including every
    descendant it waited for (the L4 child's ffmpeg). None while it is still running
    (block=False). Sets proc.returncode, as Popen.wait would, so nothing waits on the pid
    again. 0.0 when the pid was already reaped elsewhere."""
    if proc.returncode is not None:
        return 0.0
    try:
        pid, status, ru = os.wait4(proc.pid, 0 if block else os.WNOHANG)
    except ChildProcessError:
        proc.returncode = proc.returncode if proc.returncode is not None else -1
        return 0.0
    if pid == 0:
        return None
    proc.returncode = os.waitstatus_to_exitcode(status)
    return ru.ru_utime + ru.ru_stime


def _kill_group(proc) -> None:
    """SIGKILL the step's whole process group (it was started in its own session) and reap it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except OSError:
            pass
    wait4_cpu(proc, block=True)


def _tail(f) -> str:
    try:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - OUTPUT_TAIL_BYTES))
        return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def run_step(cmd: list, *, timeout: float, abort=None, should_pause=None, cwd: str | None = None,
             what: str = "step", poll: float = POLL_SECONDS) -> tuple:
    """Run one analysis child to completion -> (returncode, output tail, CPU seconds).

    The child gets its own session (so a kill takes its children too), stdin closed, and its
    stdout+stderr in an anonymous temp file (a full pipe can never stall it). While it runs,
    every `poll` seconds: abort set -> kill, raise Aborted; should_pause() -> kill, raise
    Paused; past `timeout` -> kill, raise RuntimeError. Whatever ends the wait, a child still
    running on the way out is killed and reaped."""
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                cwd=cwd, start_new_session=True)
        t0 = time.monotonic()
        try:
            while True:
                cpu = wait4_cpu(proc, block=False)
                if cpu is not None:
                    break
                if abort is not None and abort.is_set():
                    raise Aborted(f"aborted (run stopped) during {what}")
                if should_pause is not None and should_pause():
                    raise Paused(f"paused: yielded during {what} — it restarts on resume")
                if time.monotonic() - t0 > timeout:
                    raise RuntimeError(f"{what} timed out after {int(timeout)} s")
                if abort is not None:
                    abort.wait(poll)
                else:
                    time.sleep(poll)
        finally:
            if proc.returncode is None:
                _kill_group(proc)
        return proc.returncode, _tail(log), cpu


def analysis_vf(cube: str | None) -> str:
    """Chain for the analysis intermediate: area-downscale to 1080p FIRST, then (with a LUT)
    SDR -> PQ RGB -> 4:4:4; without one (an already-PQ source) straight to 4:4:4. Scaling
    before the LUT does the LUT's per-pixel float work at a quarter of the pixels: the
    intermediate builds ~2x faster, and on a real Topaz chunk the L1 it measures moved by a
    median 0 codes (peak, worst 9-10), at most 1 code (average), scene cuts identical, L4 at
    most 1 code (measured 2026-10-15 against the LUT-first chain)."""
    scale = f"scale={ANALYSIS_WIDTH}:{ANALYSIS_HEIGHT}:flags=area"
    if cube:
        return scale + "," + lut_rgb_chain(cube) + "," + pq_rgb_to_yuv("yuv444p10le")
    return scale + ",format=yuv444p10le"


def build_analysis_intermediate_command(ffmpeg: str, chunk: str, out_mkv: str, cube: str | None,
                                        hwaccel: bool = True) -> list:
    """`hwaccel`: decode the 4K ProRes on Apple's media engine (VideoToolbox) — the decode is
    what bounds this step once the LUT runs at 1080p; analyze_chunks retries once in software
    if the hardware decode refuses a chunk."""
    head = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    if hwaccel:
        head += ["-hwaccel", "videotoolbox"]
    return head + ["-i", chunk, "-map", "0:v:0", "-vf", analysis_vf(cube),
                   "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
                   "-color_range", "tv", "-c:v", "ffvhuff", "-pix_fmt", "yuv444p10le", out_mkv]


def build_raw444_decode_command(ffmpeg: str, src: str, size: tuple | None = None) -> list:
    """Raw yuv444p10le frames of `src` on stdout; `size` (w, h) area-scales them in the decode."""
    vf = ["-vf", f"scale={int(size[0])}:{int(size[1])}:flags=area"] if size else []
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", src,
            "-map", "0:v:0", *vf, "-pix_fmt", "yuv444p10le", "-f", "rawvideo", "-"]


def maxrgb_pq12_stats(frame, width: int, height: int):
    """PURE (numpy). One yuv444p10le BT.2020nc limited frame -> (mean, std) of PQ max(R,G,B)
    in 12-bit codes, clamped to the declared source range."""
    import numpy as np
    a = np.frombuffer(frame, dtype="<u2").astype(np.float32)
    n = width * height
    y = (a[:n] - 64.0) / 876.0
    cb = (a[n:2 * n] - 512.0) / 896.0
    cr = (a[2 * n:3 * n] - 512.0) / 896.0
    r = y + 1.4746 * cr
    b = y + 1.8814 * cb
    g = (y - 0.2627 * r - 0.0593 * b) / 0.6780
    m = np.clip(np.maximum(np.maximum(r, g), b), 0.0, SOURCE_MAX_PQ / PQ12_MAX) * PQ12_MAX
    return float(m.mean()), float(m.std())


def frame_l4(src: str, *, ffmpeg: str = FFMPEG, width: int = ANALYSIS_WIDTH,
             height: int = ANALYSIS_HEIGHT, scale: bool = False) -> list:
    """Per-frame [anchor_pq, anchor_power] (rounded mean, std of PQ max-RGB) of a yuv444p10le
    PQ file — the analysis intermediate, so it costs one more decode of a file we already made.
    `scale`: decode it area-scaled to width x height (analyze_chunks measures L4 at 960x540:
    ~2x faster than 1080p, within a median 1-2 codes of it, worst 6 — far inside the native
    vs Resolve L4 difference of ~6-8). analyze_chunks runs this in a child
    (build_l4_command), never in the engine process."""
    size = width * height * 3 * 2
    p = subprocess.Popen(build_raw444_decode_command(ffmpeg, src, (width, height) if scale else None),
                         stdout=subprocess.PIPE)
    out = []
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            mean, std = maxrgb_pq12_stats(buf, width, height)
            out.append([int(round(mean)), int(round(std))])
    finally:
        p.stdout.close()
        p.wait()
    if p.returncode != 0:
        raise RuntimeError(f"L4 decode failed (rc={p.returncode})")
    return out


L4_WIDTH, L4_HEIGHT = 960, 540         # analyze_chunks' L4 resolution (scaled in the decode)


def build_l4_command(python: str, src: str, out_json: str, *, ffmpeg: str = FFMPEG,
                     width: int = ANALYSIS_WIDTH, height: int = ANALYSIS_HEIGHT,
                     scale: bool = False) -> list:
    """`python3 -m dvnative l4 ...` (run with cwd = ENGINE_DIR): frame_l4 of `src` as JSON."""
    return [python, "-m", "dvnative", "l4", src, out_json, "--ffmpeg", ffmpeg,
            "--size", f"{int(width)}x{int(height)}"] + (["--scale"] if scale else [])


def merge_sidecars(parts: list, total: int | None = None) -> dict:
    """PURE. [(first_frame, sidecar), ...] in order -> one sidecar over the whole stream.
    Scenes are offset by their chunk's first frame; light level is the max over chunks; a
    per-frame "l4" list is concatenated when every chunk has one. Refuses a gap or overlap
    between chunks, and a merged length that differs from `total` (the RPU must cover every
    frame exactly once)."""
    scenes, expect, cll, fall, l4 = [], 0, 0, 0, []
    for first, sc in parts:
        n = stream_frames(sc)
        if first != expect:
            raise ValueError(f"chunk starts at {first}, expected {expect}")
        if leading_frames(sc):
            raise ValueError("a chunk intermediate must not have undecodable leading pictures")
        for s in sc["scenes"]:
            scenes.append(dict(s, start=s["start"] + first, end=s["end"] + first))
        ll = sc.get("light_level") or {}
        cll, fall = max(cll, ll.get("max_cll_nits") or 0), max(fall, ll.get("max_fall_nits") or 0)
        if l4 is not None:
            if sc.get("l4") is not None and len(sc["l4"]) == n:
                l4.extend(sc["l4"])
            else:
                l4 = None
        expect = first + n
    if total is not None and expect != total:
        raise ValueError(f"analysis covers {expect} frames, expected {total}")
    out = {"version": 5, "scenes": scenes,
           "source": {"stream_frames": expect, "leading_skipped_frames": 0}}
    if cll and fall:
        out["light_level"] = {"max_cll_nits": cll, "max_fall_nits": fall}
    if l4:
        out["l4"] = l4
    return out


def _rm_quiet(*paths) -> None:
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _load_chunk(side: str, l4_path: str | None, frames: int) -> tuple:
    """(sidecar with its "l4" list attached, None) for a published chunk that covers exactly
    `frames` frames; (None, why) for one that cannot be used."""
    try:
        sc = load_sidecar(side)
        if stream_frames(sc) != frames:
            return None, f"analysis covers {stream_frames(sc)} frames, the chunk has {frames}"
        if l4_path:
            with open(l4_path) as f:
                sc["l4"] = json.load(f)
            got = len(sc["l4"]) if isinstance(sc["l4"], list) else None
            if got != frames:
                return None, f"L4 has {got} frames, the chunk has {frames}"
        return sc, None
    except (OSError, ValueError) as e:
        return None, f"unreadable result: {e}"


class _EitherSet:
    """run_step's `abort` while chunks overlap: set when the caller's abort is set OR a sibling
    step failed (so a failure in one step kills the others instead of letting them run on)."""

    def __init__(self, outer, inner):
        self.outer, self.inner = outer, inner

    def is_set(self) -> bool:
        return bool(self.inner.is_set() or (self.outer is not None and self.outer.is_set()))

    def wait(self, timeout=None) -> bool:
        end = time.monotonic() + (timeout or 0)
        while not self.is_set():
            left = end - time.monotonic()
            if left <= 0:
                return False
            self.inner.wait(min(left, 0.25))
        return True


def analyze_chunks(chunks: list, workdir: str, *, cube: str | None, analyzer: str = HDR_ANALYZER,
                   ffmpeg: str = FFMPEG, peak_estimator: str = "max", with_l4: bool = True,
                   threads: int | None = None, abort=None, on_chunk=None, should_pause=None,
                   before_chunk=None, on_cpu=None, python: str | None = None,
                   nice: int = NICE) -> dict:
    """Measure every chunk ([[first_frame, path], ..., [total, None]] — the end sentinel is
    required: it gives each chunk its frame count) and return the merged sidecar.

    OVERLAPPED (2026-10-15, to match Resolve's stage time): while hdr-analyze and the L4 pass
    run side by side on chunk k's intermediate, chunk k+1's intermediate is already being
    built — a chunk costs its slowest step instead of the sum of three. At most two
    intermediates (~2.3 MB/frame at 1080p FFVHuff) exist at once.

    A finished chunk's sidecar (and its L4 list) is kept in `workdir` and reused on resume;
    intermediates are always deleted. Before a chunk that still needs work is STARTED:
      abort set                   -> Aborted
      should_pause()              -> Paused (also polled while each step runs; the step dies)
      before_chunk(k, frames)     -> a non-empty string pauses with that reason (the caller's
                                     disk floor); it may also raise Paused itself
    A pause or abort found while starting chunk k+1 lets chunk k finish and publish first.
    Each step runs through run_step at `nice`, bounded by step_timeout(frames); a failing
    step kills its siblings and its own error is what the call raises. on_chunk(done, of)
    after every published chunk (resumed ones counted first); on_cpu(k, cpu_seconds, frames)
    for each chunk worked on in THIS call (os.wait4 user+sys over its steps): when it
    publishes, or on the way out for one that failed or was cut short. A chunk whose
    results do not cover exactly its frames (sidecar or L4) is deleted and fails the call,
    so a retry measures it again instead of reusing it."""
    import concurrent.futures as cf
    if not chunks or chunks[-1][1] is not None:
        raise ValueError("the chunk list must end with its [total, None] sentinel")
    os.makedirs(workdir, exist_ok=True)
    total = int(chunks[-1][0])
    python = python or sys.executable
    real = [(a, p) for a, p in chunks if p]
    ends = [int(a) for a, _p in chunks[1:]]
    n_chunks = len(real)

    def frames_of(k):
        return ends[k] - int(real[k][0])

    def names(k):
        out_bin = os.path.join(workdir, f"chunk_{k:04d}.bin")
        return (out_bin, sidecar_path(out_bin), os.path.join(workdir, f"chunk_{k:04d}.l4.json"),
                os.path.join(workdir, f"chunk_{k:04d}.mkv"))

    def published(k):
        _b, side, l4_path, _m = names(k)
        return os.path.exists(side) and (not with_l4 or os.path.exists(l4_path))

    def gate(k):
        if abort is not None and abort.is_set():
            raise Aborted("aborted (run stopped)")
        if should_pause is not None and should_pause():
            raise Paused(f"paused: yielded before analysis chunk {k + 1} of {n_chunks}")
        if before_chunk is not None:
            why = before_chunk(k, frames_of(k))
            if why:
                why = str(why)
                raise Paused(why if why.startswith("paused:") else "paused: " + why)

    stop = threading.Event()
    guard = _EitherSet(abort, stop)
    cpu, lock, reported = {}, threading.Lock(), set()

    def step(k, cmd, what, cwd=None):
        rc, tail, used = run_step(niced(cmd, nice), timeout=step_timeout(frames_of(k)), abort=guard,
                                  should_pause=should_pause, cwd=cwd, what=f"chunk {k + 1} {what}")
        with lock:
            cpu[k] = cpu.get(k, 0.0) + (used or 0.0)
        return rc, tail

    def run_build(k):
        mkv = names(k)[3]
        rc, tail = step(k, build_analysis_intermediate_command(ffmpeg, real[k][1], mkv, cube), "intermediate")
        if rc != 0:                     # the hardware decode refused it: once more in software
            _rm_quiet(mkv)
            rc, tail = step(k, build_analysis_intermediate_command(ffmpeg, real[k][1], mkv, cube,
                                                                   hwaccel=False),
                            "intermediate (software decode)")
        if rc != 0:
            raise RuntimeError(f"chunk {k}: intermediate failed: {tail[-300:]}")

    def run_analyzer(k):
        out_bin, _s, _l, mkv = names(k)
        rc, tail = step(k, build_analyzer_command(analyzer, mkv, out_bin + ".part",
                                                  peak_estimator=peak_estimator, threads=threads),
                        "analyzer")
        if rc != 0 or not os.path.exists(sidecar_path(out_bin + ".part")):
            raise RuntimeError(f"chunk {k}: analyzer failed: {tail[-300:]}")

    def run_l4(k):
        _b, _s, l4_path, mkv = names(k)
        rc, tail = step(k, build_l4_command(python, mkv, l4_path + ".part", ffmpeg=ffmpeg,
                                            width=L4_WIDTH, height=L4_HEIGHT, scale=True),
                        "L4", cwd=ENGINE_DIR)
        if rc != 0 or not os.path.exists(l4_path + ".part"):
            raise RuntimeError(f"chunk {k}: L4 failed: {tail[-300:]}")

    def publish(k):
        out_bin, side, l4_path, mkv = names(k)
        if with_l4:
            os.replace(l4_path + ".part", l4_path)
        load_sidecar(sidecar_path(out_bin + ".part"))       # never publish an invalid one
        os.replace(sidecar_path(out_bin + ".part"), side)   # publish last: resume trusts it
        _rm_quiet(mkv, out_bin + ".part")

    def first_error(futures):
        """Wait for all of them; the first REAL error (a sibling killed by the stop event
        only raises Aborted because of it)."""
        cf.wait(futures)
        errs = [f.exception() for f in futures if f.exception() is not None]
        real_errs = [e for e in errs if not (isinstance(e, Aborted) and stop.is_set()
                                             and not (abort is not None and abort.is_set()))]
        return (real_errs or errs or [None])[0]

    todo = [k for k in range(n_chunks) if not published(k)]
    done_count = n_chunks - len(todo)
    if on_chunk:
        for i in range(done_count):
            on_chunk(i + 1, n_chunks)
    pool = cf.ThreadPoolExecutor(max_workers=3)
    try:
        queue = iter(todo)
        pending = None
        k0 = next(queue, None)
        if k0 is not None:
            gate(k0)
            pending = (k0, pool.submit(run_build, k0))
        while pending is not None:
            k, fut = pending
            pending = None
            err = first_error([fut])
            if err is not None:
                raise err
            deferred = None
            nk = next(queue, None)
            if nk is not None:
                try:
                    gate(nk)
                    pending = (nk, pool.submit(run_build, nk))
                except (Paused, Aborted) as e:
                    deferred = e        # chunk k still finishes and publishes first
            measure = [pool.submit(run_analyzer, k)] + ([pool.submit(run_l4, k)] if with_l4 else [])
            done, _ = cf.wait(measure, return_when=cf.FIRST_EXCEPTION)
            if any(f.exception() is not None for f in done):
                stop.set()              # kill the other measuring step and the prefetched build
                err = first_error(measure + ([pending[1]] if pending else []))
                raise err
            publish(k)
            done_count += 1
            reported.add(k)
            if on_cpu:
                on_cpu(k, cpu.get(k, 0.0), frames_of(k))
            if on_chunk:
                on_chunk(done_count, n_chunks)
            if deferred is not None:
                raise deferred
    except BaseException:
        stop.set()                      # nothing keeps running behind a failure or a pause
        raise
    finally:
        pool.shutdown(wait=True)
        if on_cpu:                      # a chunk that failed or was cut short still used CPU
            for k in sorted(set(cpu) - reported):
                on_cpu(k, cpu[k], frames_of(k))
        for k in todo:
            if not published(k):
                out_bin, _side, l4_path, mkv = names(k)
                _rm_quiet(mkv, out_bin + ".part", sidecar_path(out_bin + ".part"), l4_path + ".part")
    parts = []
    for k, (first, _path) in enumerate(real):
        _b, side, l4_path, _m = names(k)
        sc, bad = _load_chunk(side, l4_path if with_l4 else None, frames_of(k))
        if bad:
            _rm_quiet(side, l4_path)            # never reuse it: the retry measures it again
            raise RuntimeError(f"chunk {k}: {bad}")
        parts.append((first, sc))
    return merge_sidecars(parts, total=total)


# ---- dovi_tool generate config ----------------------------------------------------------

def neutral_l2(target_nits: int) -> dict:
    return {"Level2": {"target_max_pq": nits_to_pq12(target_nits),
                       "trim_slope": NEUTRAL, "trim_offset": NEUTRAL, "trim_power": NEUTRAL,
                       "trim_chroma_weight": NEUTRAL, "trim_saturation_gain": NEUTRAL,
                       "ms_weight": NEUTRAL}}


def clamp_l1(mn: int, mx: int, avg: int):
    """PURE. L1 inside the declared source range: max <= 3079, avg <= max, min <= avg. The
    pilot measured scene peaks up to 4025 (~8500 nits) on a 1000-nit master; dovi_tool passes
    those straight through (it only floors max at 2081 and avg at the CM floor)."""
    mx = min(int(mx), SOURCE_MAX_PQ)
    avg = min(int(avg), mx)
    return min(int(mn), avg), mx, avg


AVG_FLOORS = ("V40", "V29")      # CM v4.0 floor 1229 (Resolve's own minimum; the default) or
                                 # hdr-analyze's v2.9 floor 819 (keeps the measured average)


def rpu_config(sc: dict, *, max_cll: int | None = None, max_fall: int | None = None,
               l4: bool = True, avg_floor: str = "V40") -> dict:
    """PURE. Sidecar -> `dovi_tool generate` JSON for Profile 8.1, CM v4.0.

    Shots follow the analyzer's scenes. Undecodable leading pictures come first in
    presentation order, so every scene but the first moves back by their count and the first
    also covers them (hdr-analyze's rule). The L1 average uses the CM v4.0 floor (1229, the
    generator's default and Resolve's own minimum) rather than hdr-analyze's v2.9 choice of
    819 (`avg_floor="V29"` selects that instead, for the blind A/B). `long_play_mode` flags
    a scene refresh on every frame, as Resolve does. With a per-frame "l4" list, every frame
    carries Level4 through a frame edit."""
    if avg_floor not in AVG_FLOORS:
        raise ValueError(f"avg_floor must be one of {AVG_FLOORS}")
    if max_cll is None or max_fall is None:
        m_cll, m_fall = light_levels(sc)
        max_cll = max_cll if max_cll is not None else (m_cll or MASTER_MAX_NITS)
        max_fall = max_fall if max_fall is not None else (m_fall or MASTER_MAX_NITS)
    max_cll = min(int(max_cll), MASTER_MAX_NITS)
    max_fall = min(int(max_fall), max_cll)
    lead = leading_frames(sc)
    frames_l4 = sc.get("l4") if l4 else None
    length = stream_frames(sc)
    if frames_l4 is not None and len(frames_l4) != length:
        raise ValueError(f"L4 has {len(frames_l4)} frames, the stream {length}")
    shots = []
    for i, s in enumerate(sc["scenes"]):
        start = 0 if i == 0 else s["start"] + lead
        duration = s["end"] + lead - start + 1
        mn, mx, avg = clamp_l1(s["min_pq_12bit"], s["max_pq_12bit"], s["avg_max_rgb_pq_12bit"])
        shot = {"start": start, "duration": duration,
                "metadata_blocks": [{"Level1": {"min_pq": mn, "max_pq": mx, "avg_pq": avg}}]}
        if frames_l4 is not None:
            shot["frame_edits"] = [
                {"edit_offset": k, "metadata_blocks": [{"Level4": {
                    "anchor_pq": min(int(frames_l4[start + k][0]), PQ12_MAX),
                    "anchor_power": min(int(frames_l4[start + k][1]), PQ12_MAX)}}]}
                for k in range(duration)]
        shots.append(shot)
    blocks = [neutral_l2(t) for t in TRIM_TARGETS_NITS]
    blocks.append({"Level9": {"length": 1, "source_primary_index": SOURCE_PRIMARY_BT2020}})
    blocks.append({"Level11": {"content_type": L11_CONTENT_TYPE, "whitepoint": 0,
                               "reference_mode_flag": False}})
    cfg = {
        "profile": "8.1",
        "cm_version": "V40",
        "long_play_mode": True,
        "length": length,
        "source_min_pq": SOURCE_MIN_PQ,
        "source_max_pq": SOURCE_MAX_PQ,
        "level5": {"active_area_left_offset": 0, "active_area_right_offset": 0,
                   "active_area_top_offset": 0, "active_area_bottom_offset": 0},
        "level6": {"max_display_mastering_luminance": MASTER_MAX_NITS,
                   "min_display_mastering_luminance": int(round(MASTER_MIN_NITS * 10000)),
                   "max_content_light_level": max_cll,
                   "max_frame_average_light_level": max_fall},
        "default_metadata_blocks": blocks,
        "shots": shots,
    }
    if avg_floor == "V29":
        cfg["l1_avg_pq_cm_version"] = "V29"
    return cfg


def build_generate_command(dovi_tool: str, config_json: str, rpu_out: str) -> list:
    return [dovi_tool, "generate", "-j", config_json, "-o", rpu_out]


def generate_rpu(sidecar, rpu_out: str, *, dovi_tool: str = DOVI_TOOL, l4: bool = True,
                 avg_floor: str = "V40", max_cll: int | None = None, max_fall: int | None = None,
                 timeout: int = 600) -> dict:
    """Sidecar (file path, or an already-merged dict) -> binary RPU at `rpu_out` (atomic:
    written to .part, then renamed). Returns the config used. Raises RuntimeError when
    dovi_tool refuses it."""
    sc = load_sidecar(sidecar) if isinstance(sidecar, str) else sidecar
    validate_sidecar(sc)
    cfg = rpu_config(sc, max_cll=max_cll, max_fall=max_fall, l4=l4, avg_floor=avg_floor)
    cfg_path = rpu_out + ".generate.json"
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    part = rpu_out + ".part"
    if os.path.exists(part):
        os.remove(part)
    r = subprocess.run(build_generate_command(dovi_tool, cfg_path, part),
                       capture_output=True, text=True, errors="replace", timeout=timeout)
    if r.returncode != 0 or not os.path.exists(part):
        raise RuntimeError("dovi_tool generate failed: " + (r.stderr or r.stdout)[-400:])
    os.replace(part, rpu_out)
    return cfg


# ---- child entry point ------------------------------------------------------------------

def main(argv=None) -> int:
    """`python3 -m dvnative l4 <intermediate.mkv> <out.json> [--ffmpeg PATH] [--size WxH]`:
    frame_l4 of the intermediate written as JSON. analyze_chunks runs this as a child (nice 10,
    killable, its numpy memory gone when it exits) and publishes `out` only on exit status 0."""
    import argparse
    ap = argparse.ArgumentParser(prog="dvnative")
    sub = ap.add_subparsers(dest="cmd")
    l4 = sub.add_parser("l4", help="per-frame L4 of a yuv444p10le PQ file, as JSON")
    l4.add_argument("src")
    l4.add_argument("out")
    l4.add_argument("--ffmpeg", default=FFMPEG)
    l4.add_argument("--size", default=f"{ANALYSIS_WIDTH}x{ANALYSIS_HEIGHT}")
    l4.add_argument("--scale", action="store_true", help="area-scale the decode to --size")
    a = ap.parse_args(argv)
    if a.cmd != "l4":
        ap.print_usage(sys.stderr)
        return 2
    try:
        width, height = (int(v) for v in a.size.lower().split("x"))
    except ValueError:
        print(f"--size must be WxH, got {a.size!r}", file=sys.stderr)
        return 2
    data = frame_l4(a.src, ffmpeg=a.ffmpeg, width=width, height=height, scale=a.scale)
    with open(a.out, "w") as f:
        json.dump(data, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
