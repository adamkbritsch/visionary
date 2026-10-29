"""Peak-bitrate-capped Dolby Vision re-encode — the stage that makes masters ACTUALLY playable.

WHY: the DV render uses the VideoToolbox hardware encoder, which has no real peak
control — S05E23 measured 27.5 Mbps average with a 138.9 Mbps single-second spike, which
underruns a SHIELD's link and glitches playback. VideoToolbox cannot fix this; x265 can.

HOW (the industry-standard conformant-DV path):
  1. `dovi_tool extract-rpu` pulls the per-frame Dolby Vision RPU out of the render.
  2. x265 re-encodes the video in its NATIVE Dolby Vision mode (`--dolby-vision-profile 8.1
     --dolby-vision-rpu`), which *mandates* VBV (`--vbv-maxrate/--vbv-bufsize`) because DV
     certification requires HRD conformance — a hard ceiling on any 1-second window. x265
     interleaves the RPU NALs itself; no separate inject step.
  3. The capped HEVC ES is muxed by the remux stage (mkvmerge `--default-duration 0:<fps>p`;
     it writes the DV 8.1 record from the RPU NALs itself).

CONTRACT (user-dictated): there is NO uncapped fallback. If any step fails — RPU missing,
encode error, frame-count mismatch, DV lost, or the measured peak still over the cap — the
remux stage FAILS and the episode parks. An uncapped file IS the broken file; never ship it.

Quality: CRF-driven (quality-first) with the VBV ceiling only clipping the spikes; the cap
default (50 Mbps) is ~2x the measured average, so only the pathological seconds change.
"""
from __future__ import annotations
import collections
import json
import math
import os
import re
import shutil
import signal
import threading
import subprocess
import time
from fractions import Fraction

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"
X265 = "/opt/homebrew/bin/x265"
DOVI_TOOL = "/opt/homebrew/bin/dovi_tool"

DEFAULT_PEAK_MBPS = 50          # hard ceiling for any 1-second window (settings: max_peak_mbps)
X265_PRESET = "fast"            # 4K10 on the M3 Max; quality carried by CRF + generous VBV
X265_CRF = 16
PEAK_TOLERANCE = 1.15           # measured-peak gate: cap * this (container/NAL framing overhead)
SEG_SECONDS = 300               # ~5-min resumable x265 segments (worst-case kill loss, was ~75 min)
DV_STREAM_CEILING_MBPS = 80     # SHIELD DV playback ceiling (user-dictated 2026-08-06): with
                                # single-layer Dolby Vision engaged, playback stutters above
                                # ~80 Mbps. The ceiling is DV-specific and WHOLE-STREAM — the
                                # same box direct-plays non-DV at 121 Mbps, and a bare HDR10
                                # source plays fine until the injected RPU arms DV decoding.
TRUEHD_HEADROOM_MBPS = 8        # lossless audio rides above the video: TrueHD Atmos peaks at
                                # ~4-8 Mbps, enough to tip a borderline file over the ceiling
                                # on its own (Spider-Man NWH: 115 Mbps peaks + TrueHD =
                                # unplayable; re-confirmed by Doctor Strange). EVERY output is
                                # budgeted as if it carries TrueHD (user-dictated) — reserve
                                # the worst case, whatever audio actually ships.
RPU_SHIP_VIDEO_GATE_MBPS = DV_STREAM_CEILING_MBPS - TRUEHD_HEADROOM_MBPS   # = 72
                                # rpu-only ship gate: a source whose 1-second VIDEO peaks
                                # exceed this may NOT ship as a stream copy — it takes the
                                # enforced-VBV capped re-encode instead (an uncapped CRF pass
                                # can't promise the ceiling; the cap must bind). The x265
                                # path honours the same budget through the max_peak_mbps
                                # LIMIT: cap * PEAK_TOLERANCE + TRUEHD_HEADROOM <= CEILING.

# The 1000-nit master's "P3, D65, ST.2084" mastering display, in x265's units
# (chromaticities in 0.00002, luminance in 0.0001 nit) — used when the render carries no
# mastering-display side data.
FALLBACK_MASTER_DISPLAY = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)"


# ---- probing ---------------------------------------------------------------------------

def probe_video(path: str, ffprobe: str = FFPROBE) -> dict:
    """{frames, fps ('24000/1001'), master_display (x265 string|None), max_cll ('a,b'|None)}."""
    r = subprocess.run([ffprobe, "-v", "quiet", "-select_streams", "v:0", "-print_format", "json",
                        "-show_streams", "-show_frames", "-read_intervals", "%+#1", path],
                       capture_output=True, text=True)
    if r.returncode != 0:               # a transient probe failure must NOT read as frames=0 (which
        return {"frames": 0, "fps": "24000/1001", "start_time": 0.0,   # would poison the resume
                "master_display": None, "max_cll": None}               # manifest — see remux's guard)
    data = json.loads(r.stdout or "{}")
    streams = data.get("streams") or [{}]
    s = streams[0]
    frames = int(s.get("nb_frames") or 0)
    if frames <= 0:
        # nb_frames is MP4/MOV-only — MATROSKA NEVER CARRIES IT (not even with mkvmerge's
        # NUMBER_OF_FRAMES statistics tag, which ffprobe does not surface as nb_frames). An
        # MKV therefore reported frames=0, and every caller guarding on `frames <= 0` refused
        # to run before doing any work: the peak-gated rpu-only fallback (whose encode_source
        # is the .mkv CFR whenever TrueHD/DTS-HD/PGS forced that container — i.e. exactly the
        # UHD remuxes that exceed the peak gate) and every companion combine grafting a REAL
        # DV RPU (a P7 donor is Matroska by construction). Both branches were deterministically
        # dead, failing 5x with a reason that blamed a render never involved.
        # count_hevc_frames is the exact packet count the callers ultimately compare against.
        # Same root cause as the topaz._frame_count tagless-MKV bug fixed in 8bc83fd.
        frames = max(0, count_hevc_frames(path, ffprobe))
    fps = snap_fps(s.get("r_frame_rate")) or "24000/1001"
    try:
        start = float(s.get("start_time") or 0.0)   # container offset for the frame-exact seek
    except (TypeError, ValueError):
        start = 0.0
    md, cll = _hdr10_of_frames(data.get("frames", []))
    return {"frames": frames, "fps": fps, "start_time": start,
            "master_display": md, "max_cll": cll}


def _hdr10_of_frames(frames: list) -> tuple:
    """(master_display x265 string|None, 'cll,fall'|None) from ffprobe's first frame. Mastering
    metadata rides on FRAME side data (the in-band SEI — more reliable than the stream entry)."""
    md, cll = None, None
    for f in frames[:1]:
        for sd in f.get("side_data_list", []):
            t = sd.get("side_data_type", "")
            if "Mastering display" in t:
                md = mastering_to_x265(sd)
            elif "Content light" in t:
                cll = "%d,%d" % (int(sd.get("max_content", 0)), int(sd.get("max_average", 0)))
    return md, cll


def first_frame_hdr10(path: str, ffprobe: str = FFPROBE) -> tuple:
    """(master_display, max_cll) of `path`'s first frame — a raw ES included — WITHOUT
    probe_video's frame count (a full packet scan of a multi-GB file). (None, None) when
    unreadable or absent."""
    try:
        r = subprocess.run([ffprobe, "-v", "quiet", "-select_streams", "v:0", "-print_format",
                            "json", "-show_frames", "-read_intervals", "%+#1", path],
                           capture_output=True, text=True, timeout=120)
        return _hdr10_of_frames(json.loads(r.stdout or "{}").get("frames", []))
    except Exception:  # noqa: BLE001
        return None, None


# The frame rates real video uses. A Matroska file CANNOT carry most NTSC multiples exactly: its
# DefaultDuration is whole nanoseconds, and ffprobe reads 60000/1001 back as 19001/317 (48000/1001
# as 7001/146, 120000/1001 as 29011/242 — measured, mkvmerge v99 + ffmpeg 8). Every master is
# Matroska now, so an exact Fraction compare of an MKV against an MP4/MOV of the SAME rate failed.
# A rate this close to a standard one IS that one (the snap tolerance is topaz's: NTSC and whole
# rates are 1e-3 apart, so a snap never confuses them). topaz.py's declared-rate check uses the same
# list (it imports these: dvcap stays importable without topaz's pins).
STANDARD_FPS = ("24000/1001", "24/1", "25/1", "30000/1001", "30/1", "48000/1001", "48/1", "50/1",
                "60000/1001", "60/1", "100/1", "120000/1001", "120/1", "12000/1001", "12/1",
                "15000/1001", "15/1")
FPS_SNAP_TOLERANCE = 2e-4


def snap_fps_fields(d):
    """PURE. A copy of the dict/list `d` with every "fps" value snapped (snap_fps) — so a resume
    identity recorded with a Matroska readback rate still matches its standard rate."""
    if isinstance(d, dict):
        return {k: (snap_fps(v) or v) if k == "fps" and isinstance(v, str) else snap_fps_fields(v)
                for k, v in d.items()}
    if isinstance(d, list):
        return [snap_fps_fields(x) for x in d]
    return d


def snap_fps(rate):
    """PURE. A frame rate ('19001/317', Fraction, '25') -> 'num/den' in lowest terms, snapped to
    the standard rate it is within FPS_SNAP_TOLERANCE of; None if unknown or not positive."""
    try:
        f = Fraction(str(rate).strip())
    except (ValueError, ZeroDivisionError):
        return None
    if f <= 0:
        return None
    best = min((Fraction(r) for r in STANDARD_FPS), key=lambda r: abs(f / r - 1))
    if abs(f / best - 1) <= FPS_SNAP_TOLERANCE:
        f = best
    return f"{f.numerator}/{f.denominator}"


def _fr(v, scale) -> int:
    """ffprobe fraction string ('13250/50000') -> integer in x265 units."""
    return round(float(Fraction(str(v))) * scale)


def mastering_to_x265(sd: dict):
    """ffprobe mastering-display side data -> x265 --master-display string, or None if partial."""
    try:
        return ("G(%d,%d)B(%d,%d)R(%d,%d)WP(%d,%d)L(%d,%d)" % (
            _fr(sd["green_x"], 50000), _fr(sd["green_y"], 50000),
            _fr(sd["blue_x"], 50000), _fr(sd["blue_y"], 50000),
            _fr(sd["red_x"], 50000), _fr(sd["red_y"], 50000),
            _fr(sd["white_point_x"], 50000), _fr(sd["white_point_y"], 50000),
            _fr(sd["max_luminance"], 10000), _fr(sd["min_luminance"], 10000)))
    except (KeyError, ValueError, ZeroDivisionError):
        return None


# ---- command builders (pure, unit-tested) ----------------------------------------------

def build_annexb_command(ffmpeg: str, src: str) -> list:
    """DV render -> Annex-B HEVC on stdout (dovi_tool's input for extract-rpu)."""
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", src,
            "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"]


# A Dolby Vision 8.1 base IS HDR10: BT.2020 primaries, PQ transfer, BT.2020 non-constant matrix.
# mkvmerge DERIVES the DV record's compatibility id from the HEVC VUI (MP4Box forced 8.1), so a
# source whose VUI leaves primaries/transfer "unknown" (an HDR10 file that signals it only in its
# container — tested: compat 2) would mux as 8.2. hevc_metadata writes the colour description into
# the SPS VUI and touches no coded picture (VCL bytes identical, measured).
PQ_VUI_BSF = "hevc_metadata=colour_primaries=9:transfer_characteristics=16:matrix_coefficients=9"


def build_annexb_file_command(ffmpeg: str, src: str, out_hevc: str) -> list:
    """Source container -> Annex-B HEVC ES FILE, stream-copied — the original bits, untouched.
    The inject path's base layer (vs build_annexb_command's stdout pipe for RPU extraction)."""
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", src,
            "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", out_hevc]


def es_says_pq(es: str, ffprobe: str = FFPROBE) -> bool:
    """The Annex-B ES's OWN VUI (not a container's Colour element, which can stand in for a missing
    one) declares BT.2020 primaries and the PQ transfer."""
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=color_primaries,color_transfer", "-of", "json", es],
                             capture_output=True, text=True, timeout=120).stdout
        st = (json.loads(out or "{}").get("streams") or [{}])[0]
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return st.get("color_primaries") == "bt2020" and st.get("color_transfer") == "smpte2084"


def ensure_pq_vui(es: str, ffmpeg: str = FFMPEG, ffprobe: str = FFPROBE, timeout=None) -> tuple:
    """(ok, note) — make a Dolby Vision base ES SAY it is BT.2020/PQ, in its VUI. Most HDR10 sources
    already do, and are left exactly as they are (no second pass). Otherwise hevc_metadata writes
    the colour description into the SPS (PQ_VUI_BSF; no coded picture changes) — accepted only
    when it drops no packet and logs no filter error: FFmpeg's parser rejects SEI it cannot read
    and DROPS that access unit with exit code 0 (review-caught 2026-10-19). ok=False says why."""
    if es_says_pq(es, ffprobe):
        return True, ""
    tmp = es + ".vui"
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                            "-i", es, "-c:v", "copy", "-bsf:v", PQ_VUI_BSF, "-f", "hevc", tmp],
                           capture_output=True, text=True, errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        _rm_quiet(tmp)
        return False, f"could not state BT.2020/PQ in the stream's VUI: {e}"
    made = os.path.isfile(tmp)
    before = count_hevc_frames(es, ffprobe)
    after = count_hevc_frames(tmp, ffprobe) if made else 0
    err = (r.stderr or "")
    if (r.returncode != 0 or not made or "Error applying bitstream filters" in err
            or after != before or before <= 0):
        _rm_quiet(tmp)
        return False, (f"the stream's VUI does not say BT.2020/PQ and rewriting it failed "
                       f"({before} -> {after} frames; {err.strip().splitlines()[-1:] or ['no error']})")
    os.replace(tmp, es)
    return True, " · VUI set to BT.2020/PQ"


def _rm_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def build_inject_command(dovi_tool: str, src_es: str, rpu: str, out_es: str) -> list:
    """Interleave an extracted RPU into an existing HEVC ES — dovi_tool's standard inject
    subcommand. The ONLY re-attachment path that keeps the base layer's original bits (the
    x265 path re-encodes; this one must not)."""
    return [dovi_tool, "inject-rpu", "-i", src_es, "--rpu-in", rpu, "-o", out_es]


def build_rpu_extract_command(dovi_tool: str, rpu_out: str, mode: int | None = None) -> list:
    """Reads Annex-B HEVC from stdin, writes the binary RPU. `mode` is dovi_tool's
    GLOBAL -m flag (before the subcommand — verified on the pinned 2.3.0): mode 2
    rewrites a Profile 7 (Blu-ray dual-layer) RPU to be 8.1-compatible during the
    extract, which is how a REAL studio RPU from a P7 companion gets grafted onto
    an HDR10 base (companion combine)."""
    head = [dovi_tool] + (["-m", str(mode)] if mode is not None else [])
    return head + ["extract-rpu", "-", "-o", rpu_out]


def build_dovi_convert_command(dovi_tool: str, in_es: str, out_es: str) -> list:
    """Single-layer-ify an ES that carries P7 DV INLINE: `-m 2 convert --discard`
    drops the enhancement layer and rewrites the RPU to 8.1 in one pass (verified
    on the pinned 2.3.0). Used when the combine's video WINNER is itself the P7
    file — its own base layer ships, converted."""
    return [dovi_tool, "-m", "2", "convert", "--discard", "-i", in_es, "-o", out_es]


def build_decode_command(ffmpeg: str, src: str) -> list:
    """DV render -> 10-bit y4m on stdout (x265's input; y4m carries fps/size/format)."""
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", src,
            "-map", "0:v:0", "-pix_fmt", "yuv420p10le", "-strict", "-1",
            "-f", "yuv4mpegpipe", "-"]


def build_x265_command(x265: str, rpu: str, out_hevc: str, cap_mbps: int,
                       master_display: str = None, max_cll: str = None,
                       crf: float = X265_CRF, preset: str = X265_PRESET, dv: bool = True) -> list:
    """x265 native-DV encode: CRF quality + a HARD VBV ceiling; x265 interleaves the RPU NALs.
    repeat-headers/aud/hrd are DV conformance requirements (x265 enforces them with a DV
    profile, listed explicitly so the intent is visible)."""
    cap_kbps = int(cap_mbps) * 1000
    cmd = [x265, "--y4m", "-", "--output", out_hevc,
           "--preset", preset, "--crf", str(crf), "--output-depth", "10",
           "--vbv-maxrate", str(cap_kbps), "--vbv-bufsize", str(cap_kbps),
           "--repeat-headers", "--aud", "--hrd"]
    if not dv:
        # TRUE SDR master: no RPU, no PQ, no mastering-display metadata — Rec.709 throughout.
        # The VBV cap stays: it is a player-glitch guard on the peak bitrate, nothing to do
        # with Dolby Vision. --aud stays too; count_hevc_frames relies on access-unit
        # delimiters to count coded frames.
        return cmd + ["--range", "limited", "--colorprim", "bt709",
                      "--transfer", "bt709", "--colormatrix", "bt709"]
    cmd += ["--dolby-vision-profile", "8.1", "--dolby-vision-rpu", rpu,
            "--range", "limited", "--colorprim", "bt2020",
            "--transfer", "smpte2084", "--colormatrix", "bt2020nc",
            "--master-display", master_display or FALLBACK_MASTER_DISPLAY]
    if max_cll:
        cmd += ["--max-cll", max_cll]
    return cmd


_PROGRESS_RE = re.compile(r"^\s*\[?\s*(\d+)\s*(?:frames|/)")


def parse_x265_progress(line: str):
    """x265 stderr progress ('123 frames: 9.87 fps, ...') -> frames encoded, or None."""
    m = _PROGRESS_RE.match(line or "")
    return int(m.group(1)) if m else None


_ENCODED_RE = re.compile(r"encoded (\d+) frames")


def parse_x265_encoded(text: str):
    """x265 final summary -> total frames encoded, or None."""
    m = _ENCODED_RE.search(text or "")
    return int(m.group(1)) if m else None


# ---- peak measurement (the verification gate's ruler) ----------------------------------

def peak_buckets_from_packets(csv_text: str) -> dict:
    """Per-second video bitrate {sec: mbps} from ffprobe packet CSV ('packet,<pts_time>,<size>'
    rows). Pure — unit-tested; the same bucketing used to diagnose the S05E23 spikes. The full
    map (not just the max) is what lets the peak-repair ladder LOCALIZE an over-cap burst to
    the segment(s) that produced it."""
    buckets = {}
    for line in (csv_text or "").splitlines():
        parts = line.split(",")
        if len(parts) < 3 or parts[0] != "packet":
            continue
        try:
            sec = int(float(parts[1]))
            buckets[sec] = buckets.get(sec, 0) + int(parts[2])
        except ValueError:
            continue                      # N/A pts on some packets — skip, don't crash
    return {sec: byts * 8 / 1e6 for sec, byts in buckets.items()}


def peak_1s_mbps_from_packets(csv_text: str) -> float:
    b = peak_buckets_from_packets(csv_text)
    return max(b.values()) if b else 0.0


def video_peak_buckets(path: str, ffprobe: str = FFPROBE) -> dict:
    r = subprocess.run([ffprobe, "-v", "quiet", "-select_streams", "v:0",
                        "-show_entries", "packet=pts_time,size", "-of", "csv", path],
                       capture_output=True, text=True)
    return peak_buckets_from_packets(r.stdout)


def video_peak_1s_mbps(path: str, ffprobe: str = FFPROBE) -> float:
    b = video_peak_buckets(path, ffprobe)
    return max(b.values()) if b else 0.0


# ---- freeing a chunk-lane item's Topaz chunks during its remux (user, 2026-10-19) -------------
# The peak gate measures the SHIPPED master in 1-second buckets of PRESENTATION time. A segment's
# own ES has no timestamps, only its coded frames in DECODE order; a frame's decode position is
# at most REORDER_SLACK frames from its presentation position (x265's B-frame reordering is far
# shallower), and the container rounds each timestamp to the millisecond (one frame either way).
# So every frame of any 1-second bucket lies inside some run of ceil(fps) + 2*REORDER_SLACK + 2
# consecutive coded frames, and the largest such run's bytes BOUND the largest bucket the
# segment can take part in. Under the gate (with EARLY_FREE_MARGIN) the segment can never be
# flagged for a peak repair — the only thing its Topaz chunk was still kept for.
REORDER_SLACK = 16
EARLY_FREE_MARGIN = 0.97


def hevc_packet_sizes(path: str, ffprobe: str = FFPROBE) -> list:
    """The coded-frame sizes (bytes, decode order) of a raw HEVC ES — no decode, a packet
    scan. Raises RuntimeError when ffprobe reads nothing."""
    r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "packet=size", "-of", "csv=p=0", path],
                       capture_output=True, text=True, timeout=600)
    out = []
    for line in (r.stdout or "").splitlines():
        try:
            out.append(int(line.strip().strip(",")))
        except ValueError:
            continue
    if r.returncode != 0 or not out:
        raise RuntimeError(f"no packets read from {os.path.basename(path)}")
    return out


def window_frames(fps_str: str) -> int:
    """PURE. The run length (coded frames) that bounds any 1-second presentation bucket."""
    return int(math.ceil(float(Fraction(str(fps_str))))) + 2 * REORDER_SLACK + 2


def window_peak_mbps(sizes: list, fps_str: str, lo: int = 0, hi: int | None = None) -> float:
    """PURE. The largest byte sum (as Mb) over runs of window_frames(fps) consecutive entries of
    `sizes` that include at least one index in [lo, hi) — an UPPER BOUND on every 1-second
    bucket those frames take part in."""
    w = window_frames(fps_str)
    n = len(sizes)
    hi = n if hi is None else hi
    if n == 0 or lo >= hi:
        return 0.0
    first, last = max(0, lo - w + 1), min(hi - 1, n - 1)
    best, acc = 0, sum(sizes[first:first + w])
    for s in range(first, last + 1):
        if s > first:
            acc += (sizes[s + w - 1] if s + w - 1 < n else 0) - sizes[s - 1]
        best = max(best, acc)
    return best * 8 / 1e6


def build_es_decode_command(ffmpeg: str, es: str, fps_str: str) -> list:
    """A raw HEVC ES -> 10-bit y4m at `fps` (an ES carries no timing of its own: without the
    rate the y4m header — and so x265's VUI — would say 25)."""
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-r", str(fps_str),
            "-i", es, "-map", "0:v:0", "-fps_mode", "passthrough", "-pix_fmt", "yuv420p10le",
            "-strict", "-1", "-f", "yuv4mpegpipe", "-"]


def peak_ok(measured_mbps: float, cap_mbps: int, tolerance: float = PEAK_TOLERANCE) -> bool:
    return 0 < measured_mbps <= cap_mbps * tolerance


def over_gate_segments(buckets: dict, segs: list, fps_str: str, cap_mbps: int,
                       tolerance: float = PEAK_TOLERANCE) -> list:
    """PURE. Which segments produced an over-gate second? `buckets` is {sec: mbps} of the
    SHIPPED file; `segs` the plan's [a, b) frame ranges. A second [s, s+1) is charged to every
    segment whose time-range [a/fps, b/fps) overlaps it — a burst straddling a segment cut
    charges both sides. Sorted, deduped segment indices."""
    fps = float(Fraction(str(fps_str)))
    gate = cap_mbps * tolerance
    hot = [s for s, m in (buckets or {}).items() if m > gate]
    if not hot or fps <= 0:
        return []
    out = []
    for i, (a, b) in enumerate(segs):
        t0, t1 = a / fps, b / fps
        if any(s < t1 and (s + 1) > t0 for s in hot):
            out.append(i)
    return out


# ---- orchestration ---------------------------------------------------------------------

def extract_rpu(dv_video: str, rpu_out: str, *, mode: int | None = None,
                ffmpeg=FFMPEG, dovi_tool=DOVI_TOOL, timeout=None):
    """DV video -> binary RPU file. Returns (ok, reason). `mode=2` converts a P7
    RPU to 8.1 on the way out (companion combine's graft path). ATOMIC: writes a
    temp then os.replace, so a hard kill mid-extract can never leave a TRUNCATED
    rpu.bin at the real path (which the segmented resume reuses verbatim → wrong
    DV slices)."""
    tmp = rpu_out + ".part"
    _rm(tmp)
    p1 = subprocess.Popen(build_annexb_command(ffmpeg, dv_video),
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        p2 = subprocess.run(build_rpu_extract_command(dovi_tool, tmp, mode=mode),
                            stdin=p1.stdout, capture_output=True, text=True, timeout=timeout)
    finally:
        p1.stdout.close()
        p1.wait()
    if p2.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        _rm(tmp)
        return False, "RPU extract failed: " + "\n".join((p2.stderr or "").splitlines()[-4:])
    size = os.path.getsize(tmp)
    os.replace(tmp, rpu_out)          # atomic publish
    return True, "rpu extracted (%d bytes)" % size


# PRIORITY (user-dictated, 2026-07-06): this pipe runs at NORMAL priority — it is the EARLIER
# episode and the ONE encode in the pipeline that cannot checkpoint (killed = the whole pass
# lost). Topaz yields instead (topaz.TVAI_NICE=10 — it resumes from segments), and the
# prefetcher's CFR encodes sit below both at nice 15.

# ---- SEGMENTED (resumable) encode ------------------------------------------------------
# The x265 peak-cap pass is the ONE un-resumable encode in the pipeline: killed mid-way
# (deploy / unplug / stop) it restarted from frame 0, wasting up to ~75 min (live-hit twice
# on S06E03). Split it into ~5-min segments. KEY INSIGHT: each segment is a self-contained
# native-DV x265 encode fed its OWN RPU SLICE (dovi_tool editor), so it is byte-for-byte the
# same kind of encode as the proven whole-file path — no post-hoc inject, no display/decode
# RPU-order guessing. Each segment starts with an IDR + in-band VPS/SPS/PPS (--repeat-headers),
# so the raw-HEVC elementary streams concatenate with a plain byte cat. Resume = skip segments
# whose frame count already matches; worst-case kill loss is one segment.

def _rm(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def resume_manifest(dv_video: str, cap_mbps: int, total_frames: int, fps: str,
                    seg_seconds: int, crf: float = X265_CRF, preset: str = X265_PRESET,
                    boundaries: list | None = None, encode_source: str | None = None,
                    rpu_mode: int | None = None, native_id: str | None = None,
                    sdr: bool = False, rpu_source: str | None = None) -> dict:
    """Identity of a segmented-encode run. If ANY of this changes between attempts, the
    persisted segments/RPU describe a DIFFERENT encode and must not be resumed: a re-rendered
    dv_video would pass the frame-count check yet concat WRONG CONTENT into the master. `segb`
    is included so switching the plan (topaz-aligned vs ~seg_seconds) also wipes stale segments.
    `native_id` (native DV only): there is no render to stat — the source is Topaz's chunks +
    a generated RPU + the LUT, and the native marker's id already hashes all of them (chunk
    size:mtime, RPU and LUT sha). Stat'ing the nominal chunk-0 path instead would miss a
    regenerated RPU and resume segments encoded against the old one."""
    if native_id:
        src_id = f"native:{native_id}"      # never collides with a "size:mtime" stat
    else:
        try:
            st = os.stat(dv_video)
            src_id = f"{st.st_size}:{int(st.st_mtime)}"
        except OSError:
            src_id = "missing"
    m = {"src": src_id, "cap": int(cap_mbps), "frames": int(total_frames),
         "fps": str(fps), "seg": int(seg_seconds), "crf": float(crf), "preset": preset}
    if boundaries:                          # only when a topaz-aligned plan is in force — so a fallback
        m["segb"] = [int(b) for b in boundaries]   # (~seg_seconds) remux still matches a pre-segb manifest
    if encode_source:                       # peak-gated rpu-only fallback: the ENCODED video is the
        try:                                # source, not the render — its identity must invalidate
            st = os.stat(encode_source)     # resume too (key absent on ordinary runs, like segb)
            m["enc"] = f"{st.st_size}:{int(st.st_mtime)}"
        except OSError:
            m["enc"] = "missing"
    if rpu_mode is not None:                # a mode-2 (P7→8.1) RPU is a DIFFERENT RPU: switching
        m["rpu_mode"] = int(rpu_mode)       # modes must wipe the persisted rpu.bin + segments
    if sdr:                                 # an SDR master's segments carry no RPU and no PQ: never
        m["sdr"] = True                     # resumed into a DV encode, nor the reverse (2026-10-18)
    if rpu_source:                          # a real donor read by its frame order (2026-10-20): a
        m["rpu"] = rpu_source               # segdir from before holds dovi_tool's extract, which
                                            # misorders RADL groups — never resumed into this one
    return m                                # and resumes instead of wiping on the schema change


def ensure_segdir(segdir: str, manifest: dict) -> str:
    """Create/validate the resume dir against `manifest`. A mismatch (or unreadable manifest)
    WIPES the dir — stale segments must never survive a source/params change. Returns
    'resume' when the previous attempt's state is safe to reuse, else 'fresh'."""
    mf = os.path.join(segdir, "manifest.json")
    if os.path.isdir(segdir):
        try:
            with open(mf) as f:
                # the rate compared SNAPPED: one recorded before snap_fps (a Matroska readback,
                # 19001/317) is the same identity as its standard rate — a deploy must not wipe it
                if snap_fps_fields(json.load(f)) == snap_fps_fields(manifest):
                    with open(mf, "w") as g:
                        json.dump(manifest, g)
                    return "resume"
        except (OSError, ValueError):
            pass
        shutil.rmtree(segdir, ignore_errors=True)
    os.makedirs(segdir, exist_ok=True)
    with open(mf, "w") as f:
        json.dump(manifest, f)
    return "fresh"


def _tight_caps_path(segdir: str) -> str:
    return os.path.join(segdir, "tight_caps.json")


def read_tight_caps(segdir: str) -> dict:
    """{segment index -> Mbps} recorded by the peak-repair ladder. Missing/corrupt book →
    {} — every segment then encodes at the global cap, the pre-book behavior. Lives INSIDE
    segdir so ensure_segdir's wipe-on-mismatch removes it with the segments it describes."""
    try:
        with open(_tight_caps_path(segdir)) as f:
            d = json.load(f)
        return {int(k): int(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except Exception:
        return {}


def _remember_tight_cap(segdir: str, i: int, cap_mbps: int):
    """Record BEFORE the tightened re-encode starts (atomic tmp+rename): an interrupted
    repair then RESUMES this segment at its tight cap instead of regressing to the global
    cap — which deterministic x265 guarantees would re-fail the gate and redo the whole
    rung (a deploy mid-repair cost one full-cap re-encode + re-flag per interruption,
    live-measured 2026-08-06). Best-effort: losing the note only costs that same rework."""
    try:
        book = read_tight_caps(segdir)
        book[int(i)] = int(cap_mbps)
        tmp = _tight_caps_path(segdir) + ".tmp"
        with open(tmp, "w") as f:
            json.dump({str(k): v for k, v in book.items()}, f)
        os.replace(tmp, _tight_caps_path(segdir))
    except Exception:
        pass


def plan_segments(total_frames: int, fps_str: str, seg_seconds: int = SEG_SECONDS,
                  boundaries: list | None = None) -> list:
    """PURE. Contiguous [a, b) frame ranges covering [0, total_frames). With `boundaries` (this
    episode's TOPAZ scene-cut segment-END frames), the remux segments LINE UP with the topaz ones —
    same cuts, not evenly spaced. Boundaries are clamped to [1, total_frames) and always extended to
    cover the full frame count (a minor topaz-total vs RPU-count drift just merges/pads the tail).
    Otherwise ~seg_seconds each."""
    if total_frames <= 0:
        return []
    if boundaries:
        segs, a = [], 0
        for e in boundaries:
            b = min(int(e), total_frames)               # clamp into range (drift-safe)
            if b > a:
                segs.append((a, b)); a = b
        if a < total_frames:                            # last boundary < total → pad the tail so the
            segs.append((a, total_frames))              # whole render is still covered
        return segs
    fps = float(Fraction(str(fps_str)))
    step = max(1, int(round(seg_seconds * fps)))
    segs, a = [], 0
    while a < total_frames:
        segs.append((a, min(total_frames, a + step)))
        a += step
    return segs


def seg_seek_seconds(a: int, fps_str: str):
    """PURE. Frame-EXACT input-seek timestamp for frame `a`: the MIDPOINT (a-0.5)/fps, so
    6-decimal rounding can never cross to frame a±1 (a/fps CAN round up → +1 drift, which
    would silently misalign every RPU in the segment). Frame 0 → start (no seek).
    NO container start_time term: ffmpeg's input `-ss` is ALREADY relative to the file start
    (it internally accounts for start_time), so adding it double-counts — empirically a
    start_time=2.0 render shifted every non-first segment by ~48 frames (review-caught)."""
    if a <= 0:
        return None
    fps = float(Fraction(str(fps_str)))
    return max(0.0, (a - 0.5) / fps)


def chunk_for(chunks: list, a: int, n: int):
    """PURE. (path, local_first_frame) of the chunk holding frames [a, a+n). `chunks` is
    [[first_frame, path], ...] in order; each chunk ends where the next begins (the last at
    its "end" sentinel entry [total, None]). Raises ValueError if the range is not inside
    exactly one chunk."""
    for (start, path), (nxt, _p) in zip(chunks, chunks[1:]):
        if start <= a < nxt:
            if a + n > nxt or path is None:
                raise ValueError(f"frames {a}-{a + n - 1} cross the chunk ending at {nxt - 1}")
            return path, a - start
    raise ValueError(f"frame {a} is outside the chunk list")


def plan_chunk_error(segs: list, decode: dict | None):
    """PURE. None when every [a, b) segment of the plan lies inside one decode chunk (or there
    are no chunks: the render path); otherwise the reason for the FIRST that does not. A plan
    can legitimately miss the chunks at runtime: a missing topaz_segbounds entry gives the flat
    ~5-min plan (its first segment crosses chunk 0's end), and RPU/chunk drift pads a tail past
    the last chunk. Both encoders check the WHOLE plan once, up front, so nothing is sliced,
    deleted or tight-capped for a plan that cannot work (review-caught)."""
    chunks = (decode or {}).get("chunks")
    if not chunks:
        return None
    for i, (a, b) in enumerate(segs):
        try:
            chunk_for(chunks, a, b - a)
        except ValueError as e:
            return "segment plan does not fit the decode chunks: segment %d/%d: %s" % (
                i + 1, len(segs), e)
    return None


def build_seg_decode_command(ffmpeg: str, src: str, a: int, n: int, fps_str: str,
                             decode: dict | None = None) -> list:
    """DV render frames [a, a+n) -> 10-bit y4m on stdout. Input `-ss` (fast keyframe seek +
    decode-to-exact-frame) at the midpoint timestamp; `-frames:v n` bounds the count.
    `decode` (native DV only, None = the render path, unchanged):
      {"chunks": [[first_frame, path], ...]} decodes from Topaz's own ProRes chunk holding
      frame `a`, seeking only within it (the concat demuxer decodes every earlier chunk to
      seek: ~5 min per segment, measured). A segment must not cross a chunk end — the remux
      plan's boundaries ARE the chunk ends (topaz_segbounds) — so one that does raises.
      {"vf": "..."} is the filter chain (the captured SDR->PQ LUT)."""
    decode = decode or {}
    chunks = decode.get("chunks")
    if chunks:
        src, a = chunk_for(chunks, a, n)
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
    ss = seg_seek_seconds(a, fps_str)
    if ss is not None:
        cmd += ["-ss", f"{ss:.6f}"]
    cmd += ["-i", src, "-map", "0:v:0", "-frames:v", str(n)]
    if decode.get("vf"):
        cmd += ["-vf", decode["vf"]]
    # -fps_mode passthrough: y4m is a constant-rate muxer, so ffmpeg's default ("cfr") pads
    # the output timeline. After the midpoint seek the first kept frame sits half a frame in,
    # and cfr filled the gap by emitting it TWICE — frames a, a, a+1, ... a+n-2 (measured on a
    # VideoToolbox render: 20098, 20098, 20099, 20100). Every non-first segment then ran one frame
    # late against the audio and its own RPU slice, opened on a frozen frame and lost its last
    # one, and the frame count still matched. Passthrough emits the decoded frames as they are.
    cmd += ["-fps_mode", "passthrough",
            "-pix_fmt", "yuv420p10le", "-strict", "-1", "-f", "yuv4mpegpipe", "-"]
    return cmd


_RPU_FRAMES_RE = re.compile(r"Frames:\s*(\d+)")


def rpu_frame_count(rpu_path: str, *, dovi_tool=DOVI_TOOL) -> int:
    """Real coded-frame count from the RPU summary — GROUND TRUTH (one RPU per coded picture).
    A VideoToolbox .mov's nb_frames HEADER can over-report vs the decodable/RPU tail (documented
    for these renders in topaz.py). Planning + slicing against the header
    instead of this made dovi_tool's remove-ranges go out of range → permanent park."""
    if not (rpu_path and os.path.exists(rpu_path) and os.path.getsize(rpu_path) > 0):
        return 0
    r = subprocess.run([dovi_tool, "info", "-i", rpu_path, "-s"], capture_output=True, text=True)
    m = _RPU_FRAMES_RE.search(r.stdout or "")
    return int(m.group(1)) if m else 0


def build_rpu_edit_config(a: int, b: int, total: int) -> dict:
    """PURE. dovi_tool `editor` config that KEEPS only RPU frames [a, b) by removing the
    head [0, a) and tail [b, total). Empty removes ⇒ the whole file (a==0 and b==total)."""
    remove = []
    if a > 0:
        remove.append(f"0-{a - 1}")
    if b < total:
        remove.append(f"{b}-{total - 1}")
    return {"remove": remove}


def slice_rpu(rpu_in: str, a: int, b: int, total: int, out: str, *, dovi_tool=DOVI_TOOL):
    """Write the RPU subset for frames [a, b). Whole-file ⇒ copy. Returns (ok, reason)."""
    cfg = build_rpu_edit_config(a, b, total)
    if not cfg["remove"]:
        try:
            shutil.copyfile(rpu_in, out)
            return True, "full rpu"
        except OSError as e:
            return False, f"rpu copy failed: {e}"
    js = out + ".json"
    with open(js, "w") as f:
        json.dump(cfg, f)
    r = subprocess.run([dovi_tool, "editor", "-i", rpu_in, "-j", js, "-o", out],
                       capture_output=True, text=True)
    _rm(js)
    if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        return False, "rpu slice failed: " + "\n".join((r.stderr or "").splitlines()[-3:])
    return True, "sliced"


def count_hevc_frames(path: str, ffprobe: str = FFPROBE) -> int:
    """Frame count of a raw HEVC ES (resume validation of a pre-existing segment). Counts
    PACKETS (access units), not decoded frames: our segments carry AUDs (--aud), so ffprobe's
    packetizer yields exactly one packet per frame — a parse (~1 s) instead of a full decode
    (~30-50 s per segment, which would have made every resume pay minutes of re-verification)."""
    if not (path and os.path.exists(path) and os.path.getsize(path) > 0):
        return 0
    r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
                        "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    try:
        # the first field only: ffprobe 8 trails a comma when the stream carries side data
        return int(((r.stdout or "").strip().splitlines() or [""])[0].split(",")[0].strip() or 0)
    except ValueError:
        return 0


# --- SUSPENDING A REMUX SO ANOTHER STAGE GETS THE WHOLE MACHINE ----------------------------
# Yielding at a segment boundary is not enough. A segment is 5 minutes of VIDEO but ~7
# minutes of wall clock, so an encode that is mid-segment when an exclusive stage (AI
# outpainting) starts keeps running for most of it — measured live: 4 minutes in, both lanes
# were still at the same seg_done and still burning CPU.
#
# SIGSTOP is the right tool rather than killing: the encode freezes immediately, uses no CPU
# while stopped, and resumes exactly where it was — no lost work and no segment-granularity
# rounding. Both ends of the pipe are stopped; stopping only x265 would leave ffmpeg spinning
# until the pipe filled.
_LIVE_ENCODERS = set()                  # {(dec, enc)} currently encoding
_ENCODER_LOCK = threading.Lock()
_ENCODERS_SUSPENDED = False             # so a pair spawned DURING a suspension starts stopped
_RESUMED_AT = 0.0                       # time.monotonic() of the last resume_encoders()
# THE HELPER's in-flight REMOTE pipelines (helper.py), kept apart: they are never
# SIGSTOPped. A suspension KILLS them instead — each hook kills its own local processes and
# marks its segment "suspended", which the scheduler requeues without counting or cooling down
# (the helper's own stall timer stays the safety net) — and no new one may start until resumed.
_LIVE_REMOTE = set()                    # {hook()} — called under _ENCODER_LOCK, must not block


def _signal_pair(pair, sig):
    for proc in pair:
        try:
            if proc.poll() is None:
                proc.send_signal(sig)
        except Exception:
            pass


def register_remote(hook) -> bool:
    """Register a remote pipeline's kill hook. False (NOT registered) while encoders are
    suspended: the caller must kill its pipeline at once, as a suspension would have."""
    with _ENCODER_LOCK:
        if _ENCODERS_SUSPENDED:
            return False
        _LIVE_REMOTE.add(hook)
        return True


def unregister_remote(hook) -> None:
    with _ENCODER_LOCK:
        _LIVE_REMOTE.discard(hook)


def suspend_encoders() -> int:
    """Freeze every in-flight local remux encode and KILL every remote (helper) one.
    Returns how many pipelines were stopped or killed."""
    global _ENCODERS_SUSPENDED
    with _ENCODER_LOCK:
        _ENCODERS_SUSPENDED = True
        for pair in _LIVE_ENCODERS:
            _signal_pair(pair, signal.SIGSTOP)
        for hook in list(_LIVE_REMOTE):
            try:
                hook()
            except Exception:
                pass
        return len(_LIVE_ENCODERS) + len(_LIVE_REMOTE)


def resume_encoders() -> int:
    """Unfreeze them. MUST be called from a finally — a stopped x265 left behind would
    stall its lane forever, and nothing else would ever resume it."""
    global _ENCODERS_SUSPENDED, _RESUMED_AT
    with _ENCODER_LOCK:
        _ENCODERS_SUSPENDED = False
        _RESUMED_AT = time.monotonic()
        for pair in _LIVE_ENCODERS:
            _signal_pair(pair, signal.SIGCONT)
        return len(_LIVE_ENCODERS)


def encoders_suspended() -> bool:
    return _ENCODERS_SUSPENDED


def _encode_pipe(dec_cmd, enc_cmd, out_hevc, abort, on_frame, dec_stderr=None):
    """Decode|x265 pipe for ONE segment. Returns (frames|None, reason, tail). `dec_stderr`: an
    open file the decoder's messages go to (splice.py reads it: a missing reference is only a
    log line, and the frames it conceals must not be encoded); None discards them."""
    dec = subprocess.Popen(dec_cmd, stdout=subprocess.PIPE,
                           stderr=dec_stderr if dec_stderr is not None else subprocess.DEVNULL)
    enc = subprocess.Popen(enc_cmd, stdin=dec.stdout, stderr=subprocess.PIPE, text=True)
    dec.stdout.close()
    pair = (dec, enc)
    with _ENCODER_LOCK:
        _LIVE_ENCODERS.add(pair)
        # Spawned while another stage already holds the machine — start stopped, or this
        # segment would run free for the rest of that stage.
        if _ENCODERS_SUSPENDED:
            _signal_pair(pair, signal.SIGSTOP)
    tail = []
    try:
        for line in enc.stderr:
            for chunk in line.replace("\r", "\n").splitlines():
                tail.append(chunk)
                if len(tail) > 20:
                    tail.pop(0)
                n = parse_x265_progress(chunk)
                if n is not None and on_frame:
                    on_frame(n)
            if abort is not None and abort.is_set():
                dec.kill(); enc.kill()
                return None, "aborted", tail
        enc.wait(); dec.wait()
    except Exception as e:
        dec.kill(); enc.kill()
        return None, f"pipe error: {e}", tail
    finally:
        with _ENCODER_LOCK:
            _LIVE_ENCODERS.discard(pair)
        # A process that is SIGSTOPped cannot be killed cleanly and cannot exit; make sure
        # nothing leaves this function frozen.
        _signal_pair(pair, signal.SIGCONT)
    if enc.returncode != 0 or not os.path.exists(out_hevc) or os.path.getsize(out_hevc) == 0:
        return None, "x265 rc=%s: %s" % (enc.returncode, " / ".join(tail[-4:])), tail
    return parse_x265_encoded("\n".join(tail)), "ok", tail


def concat_segments(seg_files: list, out_hevc: str) -> tuple:
    """Byte-concatenate the per-segment Annex-B HEVC ES into one stream. Valid because each
    segment begins with in-band VPS/SPS/PPS + an IDR (multiple coded video sequences)."""
    try:
        with open(out_hevc, "wb") as w:
            for sf in seg_files:
                if not (os.path.exists(sf) and os.path.getsize(sf) > 0):
                    return False, "missing segment for concat: " + os.path.basename(sf)
                with open(sf, "rb") as r:
                    shutil.copyfileobj(r, w, 1024 * 1024)
    except OSError as e:
        return False, f"concat failed: {e}"
    return (os.path.getsize(out_hevc) > 0), "concatenated %d segments" % len(seg_files)


def encode_capped_segmented(dv_video: str, rpu: str, out_hevc: str, cap_mbps: int, *,
                            segdir: str, total_frames: int, fps: str,
                            master_display=None, max_cll=None, seg_seconds: int = SEG_SECONDS,
                            boundaries: list | None = None,
                            abort=None, on_progress=None, on_plan=None, should_pause=None,
                            decode: dict | None = None, dv: bool = True, on_segment=None,
                            helper=None, on_segments=None,
                            ffmpeg=FFMPEG, x265=X265, ffprobe=FFPROBE, dovi_tool=DOVI_TOOL):
    """Resumable peak-cap encode: per-segment native-DV x265 (own RPU slice) → concat.
    `helper` (2026-10-28; helper.for_remux(), None = exactly the encode below): the helper takes
    segments from the BACK of the plan while this thread takes them from the front — see
    _encode_shared. `on_segments(info)` (only heard with a helper) is the segment bar's live
    state on every progress tick and every segment publish: {"done": [finished indices, either
    machine], "helper_done": [those the helper made], "mac": [i, frac] | None, "helper": [i,
    frac, fps] | None} (frac 0..1 within that segment).
    `on_segment(i, path)` hears each segment that is complete on disk — encoded now or found
    complete on a resume — in plan order (the chunk lane frees its Topaz chunks from it).
    `dv=False` (2026-10-18, an item pinned to SDR output): the TRUE SDR x265 (build_x265_command's
    Rec.709 branch) with no RPU at all — `rpu` is unused and nothing is sliced; everything else
    (the plan, the VBV cap, resume, pause, the frame gates) is the same.
    Segments persist in `segdir`; a completed segment is skipped on the next attempt.
    Returns (ok, frames_encoded, reason). Reports cumulative frames via on_progress, and fires
    on_plan([segment_end_frame, ...], total_frames) ONCE after planning so the dashboard can draw
    the same notched segment bar Topaz uses (seg_done is derived from cumulative frames ≥ each end;
    with a helper the frames run ahead of plan order, so _encode_shared passes the plan-order
    prefix as on_progress's `seg_done` keyword to a callback that takes one).
    `should_pause` (like topaz's): polled between segments — a True yields cleanly with a benign
    "paused:" reason (every finished segment kept; the next attempt resumes right here). Used to
    give a sibling app's lease or AI outpainting the whole machine."""
    if total_frames <= 0:
        return False, 0, "unknown total frame count"
    os.makedirs(segdir, exist_ok=True)
    segs = plan_segments(total_frames, fps, seg_seconds, boundaries=boundaries)
    bad = plan_chunk_error(segs, decode)
    if bad:
        return False, 0, bad
    if on_plan:
        on_plan([b for (_a, b) in segs], total_frames)
    tights = read_tight_caps(segdir)   # repair-tightened segments resume at THEIR cap, not the global
    if helper is not None:
        return _encode_shared(segs, helper, tights, dv_video=dv_video, rpu=rpu, out_hevc=out_hevc,
                              cap_mbps=cap_mbps, segdir=segdir, total_frames=total_frames, fps=fps,
                              master_display=master_display, max_cll=max_cll, abort=abort,
                              on_progress=on_progress, should_pause=should_pause, decode=decode,
                              dv=dv, on_segment=on_segment, on_segments=on_segments,
                              ffmpeg=ffmpeg, x265=x265, ffprobe=ffprobe, dovi_tool=dovi_tool)
    base, seg_files = 0, []
    for i, (a, b) in enumerate(segs):
        n = b - a
        sf = os.path.join(segdir, f"seg_{i:04d}.hevc")
        seg_files.append(sf)
        if abort is not None and abort.is_set():
            return False, base, "aborted"
        if should_pause is not None and should_pause():
            return False, base, "paused: another stage has the machine — this remux resumes when it finishes"
        if count_hevc_frames(sf, ffprobe) == n:          # already encoded (resume) → skip
            base += n
            if on_progress:
                on_progress(base, total_frames)
            if on_segment:
                on_segment(i, sf)
            continue
        # built BEFORE any side effect: a segment that can't be decoded deletes and slices
        # nothing, and fails with the normal (ok, frames, reason) return, not an exception
        try:
            dec = build_seg_decode_command(ffmpeg, dv_video, a, n, fps, decode)
        except ValueError as e:
            return False, base, "segment %d/%d: %s" % (i + 1, len(segs), e)
        _rm(sf)                                          # partial/stale → redo clean
        stmp = sf + ".part"                              # ATOMIC: encode to temp, publish sf only on
        _rm(stmp)                                        # a clean got==n — a hard kill never leaves a
        rslice = os.path.join(segdir, f"seg_{i:04d}.rpu")   # truncated seg_*.hevc that resume would bless
        if dv:
            ok, why = slice_rpu(rpu, a, b, total_frames, rslice, dovi_tool=dovi_tool)
            if not ok:
                return False, base, why
            enc = build_x265_command(x265, rslice, stmp, tights.get(i, cap_mbps),
                                     master_display, max_cll)
        else:
            enc = build_x265_command(x265, None, stmp, tights.get(i, cap_mbps), dv=False)
        b0 = base
        got, why, _tail = _encode_pipe(dec, enc, stmp, abort,
                                       (lambda f: on_progress(b0 + f, total_frames)) if on_progress else None)
        _rm(rslice)
        if why == "aborted":
            _rm(stmp)
            return False, base, "aborted"
        if got is None:
            _rm(stmp)
            return False, base, "segment %d/%d: %s" % (i + 1, len(segs), why)
        if got != n:                                     # seek/decode drift → RPU misalign risk: FAIL
            _rm(stmp)
            return False, base, "segment %d/%d frame mismatch: %d != %d" % (i + 1, len(segs), got, n)
        os.replace(stmp, sf)                             # atomic publish — sf is now guaranteed complete
        base += n
        if on_progress:
            on_progress(base, total_frames)
        if on_segment:
            on_segment(i, sf)
    ok, why = concat_segments(seg_files, out_hevc)
    if not ok:
        return False, base, why
    return True, base, "encoded %d frames in %d segments" % (base, len(segs))


# ---- THE HELPER: two workers over one segment plan (2026-10-28) -------------------------------
# With a helper (helper.py) the plan's pending segments form ONE queue: this thread (the Mac)
# takes from the FRONT, a remote worker thread hands segments from the BACK to the helper, only
# while helper.available() says so. The helper gets the SAME decode (the Mac decodes and applies
# the colour step — the helper's is not bit-identical) and the SAME x265 arguments, and its
# segment is published exactly like a local one once verified here: got == n AND the received
# file's own packet count == n, then os.replace. Anything else hands the segment back to the Mac.
# The helper can never fail or park an item — only the Mac's own segments can, exactly as without it.
HELPER_WAIT_SECONDS = 30        # an unavailable helper is re-checked this often
HELPER_SUSPEND_POLL = 2.0       # while encoders are suspended, how often the remote worker looks again
HELPER_MAX_FAILURES = 2         # "error"/"disconnected" remote results in one call before the helper
                                # sits it out ("yielded"/"refused" cool the helper down instead;
                                # "suspended"/"aborted" are nobody's fault and count for nothing)
HELPER_COUNTED = ("error", "disconnected")
HELPER_JOIN_SECONDS = 120       # the remote worker is joined before every return (bounded)
HELPER_RESERVE_SECONDS = 120    # a segment a suspension took off the helper stays the helper's this
                                # long after the resume (the Mac takes it only if the helper does not
                                # reclaim it by then, or the moment the helper is unavailable)
_IDLE_POLL_SECONDS = 1.0        # the Mac's poll while only the helper has (or holds) a segment
_PAUSED_REASON = "paused: another stage has the machine — this remux resumes when it finishes"


def _hosts_path(segdir: str) -> str:
    return os.path.join(segdir, "hosts.json")


def read_hosts(segdir: str) -> dict:
    """{"<segment index>": "mac"|"ally"} — who encoded each segment published with a helper
    ("ally" = the helper, whatever machine it is). Missing/corrupt -> {}. Lives in segdir, so a
    resume-identity wipe takes it along."""
    try:
        with open(_hosts_path(segdir)) as f:
            d = json.load(f)
        return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_hosts(segdir: str, hosts: dict) -> None:
    try:
        tmp = _hosts_path(segdir) + ".tmp"
        with open(tmp, "w") as f:
            json.dump({k: hosts[k] for k in sorted(hosts, key=lambda s: int(s) if s.isdigit() else -1)}, f)
        os.replace(tmp, _hosts_path(segdir))
    except Exception:  # noqa: BLE001 — bookkeeping only
        pass


def _remote_leftovers(segdir: str, i: int) -> tuple:
    """The files a remote encode of segment i writes in segdir (the received HEVC and the RPU
    slice it uploads) — stale whenever no remote encode of i is in flight."""
    return (os.path.join(segdir, f"seg_{i:04d}.hevc.remote.part"),
            os.path.join(segdir, f"seg_{i:04d}.remote.rpu"))


def _rm_remote_leftovers(segdir: str, i: int) -> None:
    for path in _remote_leftovers(segdir, i):
        _rm(path)


def _takes_kw(fn, name: str) -> bool:
    """Whether the callable `fn` accepts the keyword `name` (by name or through **kwargs)."""
    import inspect
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.kind is p.VAR_KEYWORD or (p.name == name and p.kind in (p.KEYWORD_ONLY,
                                                                         p.POSITIONAL_OR_KEYWORD))
               for p in params)


def _segments_word(k: int) -> str:
    return f"{k} segment" if k == 1 else f"{k} segments"


_HELPER_NOTE_RE = re.compile(r" \((\d+) segments? on the helper\)$")


def helper_note(why) -> str:
    """PURE. The " (K segment(s) on the helper)" suffix _encode_shared's success reason ends
    with (K > 0), or "" — what remux carries into its success note."""
    m = _HELPER_NOTE_RE.search(str(why or ""))
    return m.group(0) if m and int(m.group(1)) > 0 else ""


class _EitherSet:
    """`is_set()` of any of the events: the remote worker's abort is the caller's abort OR this
    call stopping it (a pause, a Mac-side failure, the end)."""

    def __init__(self, *events):
        self._events = [e for e in events if e is not None]

    def is_set(self) -> bool:
        return any(e.is_set() for e in self._events)


def _encode_shared(segs, helper, tights, *, dv_video, rpu, out_hevc, cap_mbps, segdir,
                   total_frames, fps, master_display, max_cll, abort, on_progress, should_pause,
                   decode, dv, on_segment, ffmpeg, x265, ffprobe, dovi_tool, on_segments=None):
    """encode_capped_segmented's body with a helper. Same returns and reasons as the Mac-only
    loop; differences, all deliberate:
      - on_segment still fires in PLAN ORDER — the longest finished prefix — because the chunk
        releaser frees a chunk only once a segment's neighbours are known;
      - on_progress = finished + both in-flight segments, never moving backwards (a segment
        the helper drops takes its in-flight frames with it; the bar holds instead of falling).
        The frame count runs AHEAD of plan order (the helper works from the back), so a callback
        that takes a `seg_done` keyword (stages' remux bar does) also hears the finished
        plan-order PREFIX as seg_done — what the notched bar, its ETA and deploy-now's segment
        boundary must count, not "how many ends the frame total has passed";
      - on_segments(info) (C1) hears the bar's live state on every progress tick and every
        publish: {"done", "helper_done", "mac": [i, frac] | None, "helper": [i, frac, fps] |
        None}. It, on_progress and the helper's own note_segments (status_summary) are called
        from both threads, serialised under one lock — on_segments BEFORE on_progress within
        one report, so a listener publishing on the progress tick is never one report stale;
        on_segments/note_segments failing never touches the encode;
      - should_pause/abort stop the remote worker (its in-flight pipeline killed) and join it
        before returning; the Mac also polls them while it waits on the helper's last segment;
      - dvcap.suspend_encoders() KILLS the helper's in-flight segment ("suspended": requeued at
        the back for the helper, never counted, no cooldown) and no remote segment is claimed
        while encoders_suspended(). That segment stays RESERVED for the helper until
        HELPER_RESERVE_SECONDS after the resume: the Mac takes it only when the helper has not
        reclaimed it by then, or at once when the helper turns out unavailable (or its worker
        is gone);
      - a segment the helper finished and that verified here is kept even when a stop lands
        after it (published while this call is still joining the worker);
      - only "error"/"disconnected" remote results count toward HELPER_MAX_FAILURES;
      - stale remote leftovers (seg_NNNN.hevc.remote.part / seg_NNNN.remote.rpu) are deleted
        on the resume and whenever the Mac takes a segment;
      - segdir/hosts.json records who encoded each segment; the success reason appends
        " (K segment(s) on the helper)" when K > 0."""
    nseg = len(segs)
    seg_files = [os.path.join(segdir, f"seg_{i:04d}.hevc") for i in range(nseg)]
    cond = threading.Condition()            # guards st, pending, done, reserved
    out_lock = threading.Lock()             # serializes on_progress / on_segments / on_segment /
                                            # hosts.json; taken BEFORE cond, never inside it
    stop = threading.Event()
    st = {"finished": 0, "mac": 0, "mac_seg": None, "ally": 0, "ally_seg": None, "ally_done": 0,
          "ally_fps": None, "ally_t0": 0.0, "reported": 0, "emitted": 0, "closed": False}
    done = [False] * nseg
    reserved = {}                           # i -> when a suspension handed it back to the helper
    hosts = read_hosts(segdir)
    pending = collections.deque()
    prog_seg_done = on_progress is not None and _takes_kw(on_progress, "seg_done")
    note = getattr(helper, "note_segments", None)
    note = note if callable(note) else None

    def frac(f, i):
        a, b = segs[i]
        return max(0.0, min(1.0, f / float(b - a))) if b > a else 0.0

    def snapshot():
        # under cond (and out_lock): the segment bar's live state
        mac = [st["mac_seg"], frac(st["mac"], st["mac_seg"])] if st["mac_seg"] is not None else None
        hlp = None
        j = st["ally_seg"]
        if j is not None:
            rate = st["ally_fps"]
            if rate is None:
                el = time.monotonic() - st["ally_t0"]
                rate = st["ally"] / el if st["ally"] > 0 and el > 0 else 0.0
            hlp = [j, frac(st["ally"], j), round(float(rate), 2)]
        finished = [k for k in range(nseg) if done[k]]
        return {"done": finished, "helper_done": [k for k in finished if hosts.get(str(k)) == "ally"],
                "mac": mac, "helper": hlp}

    def report():
        if on_progress is None and on_segments is None and note is None:
            return
        with out_lock:
            with cond:
                v = st["finished"] + st["mac"] + st["ally"]
                info = snapshot() if (on_segments is not None or note is not None) else None
            # the segments info FIRST: a listener that publishes on on_progress (stages' remux
            # bar) then carries this report's in-flight positions and fps, not the last report's
            for fn in (on_segments, note):
                if fn is not None:
                    try:
                        fn(info)
                    except Exception:  # noqa: BLE001 — a status listener never touches the encode
                        pass
            if on_progress is not None:
                v = max(st["reported"], min(v, total_frames))
                st["reported"] = v
                if prog_seg_done:
                    on_progress(v, total_frames, seg_done=st["emitted"])
                else:
                    on_progress(v, total_frames)

    def emit():
        # the plan-order prefix moves on whether or not anyone listens: seg_done is derived from it
        with out_lock:
            while st["emitted"] < nseg and done[st["emitted"]]:
                k = st["emitted"]
                st["emitted"] = k + 1
                if on_segment is not None:
                    on_segment(k, seg_files[k])

    def published(i, host):
        with out_lock:
            hosts[str(i)] = host
            _write_hosts(segdir, hosts)
        emit()
        report()

    # resume: every segment already complete on disk counts (and is heard) before any encode;
    # no remote encode is in flight yet, so every remote leftover is stale
    for i, (a, b) in enumerate(segs):
        if abort is not None and abort.is_set():
            return False, st["finished"], "aborted"
        if should_pause is not None and should_pause():
            return False, st["finished"], _PAUSED_REASON
        _rm_remote_leftovers(segdir, i)
        if count_hevc_frames(seg_files[i], ffprobe) == b - a:
            done[i] = True
            st["finished"] += b - a
            emit()
            report()
        else:
            pending.append(i)

    either = _EitherSet(abort, stop)

    def held_for_helper(i, now):
        # under cond: a suspended segment the Mac must leave to the helper for now
        t = reserved.get(i)
        if t is None:
            return False
        if encoders_suspended():
            return True
        return now < max(t, _RESUMED_AT) + HELPER_RESERVE_SECONDS

    def unreserve():
        with cond:
            if reserved:
                reserved.clear()
                cond.notify_all()

    def remote_one(i):
        a, b = segs[i]
        n = b - a
        sf = seg_files[i]
        part, rslice = _remote_leftovers(segdir, i)
        try:
            dec = build_seg_decode_command(ffmpeg, dv_video, a, n, fps, decode)
        except ValueError:
            return "error"
        _rm(sf)
        _rm(part)
        try:
            cap = tights.get(i, cap_mbps)          # the same (repair-tightened) cap as locally
            if dv:
                ok, _why = slice_rpu(rpu, a, b, total_frames, rslice, dovi_tool=dovi_tool)
                if not ok:
                    return "error"
                args = build_x265_command("X265", "{rpu}", "{out}", cap, master_display, max_cll)[1:]
            else:
                args = build_x265_command("X265", None, "{out}", cap, dv=False)[1:]

            def on_frame(f, fps=None):
                with cond:
                    st["ally"] = max(0, min(int(f), n))
                    if fps is not None:
                        st["ally_fps"] = float(fps)
                report()
            got, _why, kind = helper.encode_segment(dec, args, rslice if dv else None, n, part,
                                                    either, on_frame)
        finally:
            _rm(rslice)
        # the helper finished; a stop landing from here on does not throw its verified work away
        if kind == "ok" and got == n and count_hevc_frames(part, ffprobe) == n:
            with cond:
                if st["closed"]:                   # the call already returned: publish nothing
                    _rm(part)
                    return "aborted"
                os.replace(part, sf)               # atomic publish, exactly like a Mac segment
                done[i] = True
                st["finished"] += n
                st["ally"], st["ally_seg"], st["ally_fps"] = 0, None, None
                st["ally_done"] += 1
            published(i, "ally")
            return "ok"
        _rm(part)
        return "error" if kind == "ok" else kind  # "ok" that failed the check here is an error

    def remote_main():
        fails = 0
        claim = getattr(helper, "claim", None)
        release = getattr(helper, "release", None)
        try:
            while not either.is_set():
                with cond:
                    if not pending:
                        return                     # nothing left to take: done for this item
                if encoders_suspended():
                    # another stage has the machine: claim nothing until it is given back
                    with cond:
                        cond.wait_for(lambda: stop.is_set() or not pending,
                                      timeout=HELPER_SUSPEND_POLL)
                    continue
                paused, held = False, False
                try:
                    paused = should_pause is not None and should_pause()
                    ok = not paused and bool(helper.available()[0])
                    held = ok and (claim() if claim else True)
                except Exception:  # noqa: BLE001
                    held = False
                if not held:
                    if not paused:
                        unreserve()                # unavailable: the Mac may take its segment now
                    with cond:
                        cond.wait_for(lambda: stop.is_set() or not pending,
                                      timeout=HELPER_WAIT_SECONDS)
                    continue
                i, kind = None, "error"
                try:
                    with cond:
                        if stop.is_set() or not pending:
                            return
                        i = pending.pop()          # from the BACK; the Mac takes the front
                        reserved.pop(i, None)
                        st["ally_seg"], st["ally"], st["ally_fps"] = i, 0, None
                        st["ally_t0"] = time.monotonic()
                    report()
                    try:
                        kind = remote_one(i)
                    except Exception:  # noqa: BLE001 — the Mac takes the segment back
                        kind = "error"
                finally:
                    try:
                        if release:
                            release()
                    except Exception:  # noqa: BLE001 — the bookkeeping below must still run
                        pass
                    if i is not None:
                        with cond:
                            if st["ally_seg"] == i:
                                st["ally_seg"], st["ally"], st["ally_fps"] = None, 0, None
                            if not done[i]:
                                if kind == "suspended":
                                    pending.append(i)       # nobody's fault: the helper's again,
                                    reserved[i] = time.monotonic()   # held for it past the resume
                                else:
                                    pending.appendleft(i)   # back to the Mac, next in its line
                            cond.notify_all()
                        if not done[i]:
                            report()                        # the dropped segment's live fill goes
                if kind in HELPER_COUNTED:
                    fails += 1
                    if fails >= HELPER_MAX_FAILURES:
                        return                     # the helper sits the rest of this item out
        except Exception:  # noqa: BLE001 — the remote worker never takes the item down
            return
        finally:
            unreserve()                            # nobody left to reclaim a reserved segment

    th = None
    if pending:
        th = threading.Thread(target=remote_main, name="ally-helper", daemon=True)
        th.start()
    try:
        while True:
            if abort is not None and abort.is_set():
                return False, st["finished"], "aborted"
            if should_pause is not None and should_pause():
                return False, st["finished"], _PAUSED_REASON
            with cond:
                i, now = None, time.monotonic()
                for k, j in enumerate(pending):    # the front, skipping the helper's reserved one
                    if not held_for_helper(j, now):
                        i = j
                        del pending[k]
                        reserved.pop(j, None)
                        break
                if i is None:
                    if not pending and st["ally_seg"] is None:
                        break                      # nothing pending, nothing in flight: done
                    if th is not None and not th.is_alive():
                        # the remote worker died holding a segment: it is the Mac's now
                        j, st["ally_seg"], st["ally"] = st["ally_seg"], None, 0
                        if j is not None and not done[j]:
                            pending.appendleft(j)
                        reserved.clear()
                        continue
                    cond.wait(_IDLE_POLL_SECONDS)  # the helper's segment is running or reserved
                else:
                    st["mac_seg"], st["mac"] = i, 0
            if i is None:
                continue
            report()
            a, b = segs[i]
            n = b - a
            sf = seg_files[i]

            def mac_lost():
                with cond:
                    st["mac_seg"], st["mac"] = None, 0
            try:
                dec = build_seg_decode_command(ffmpeg, dv_video, a, n, fps, decode)
            except ValueError as e:
                mac_lost()
                return False, st["finished"], "segment %d/%d: %s" % (i + 1, nseg, e)
            _rm(sf)
            _rm_remote_leftovers(segdir, i)        # the helper is not on it: anything there is stale
            stmp = sf + ".part"
            _rm(stmp)
            rslice = os.path.join(segdir, f"seg_{i:04d}.rpu")
            if dv:
                ok, why = slice_rpu(rpu, a, b, total_frames, rslice, dovi_tool=dovi_tool)
                if not ok:
                    mac_lost()
                    return False, st["finished"], why
                enc = build_x265_command(x265, rslice, stmp, tights.get(i, cap_mbps),
                                         master_display, max_cll)
            else:
                enc = build_x265_command(x265, None, stmp, tights.get(i, cap_mbps), dv=False)

            def mac_frame(f, _n=n):
                with cond:
                    st["mac"] = max(0, min(int(f), _n))
                report()
            listening = on_progress is not None or on_segments is not None or note is not None
            got, why, _tail = _encode_pipe(dec, enc, stmp, abort,
                                           mac_frame if listening else None)
            _rm(rslice)
            if why == "aborted":
                mac_lost()
                _rm(stmp)
                return False, st["finished"], "aborted"
            if got is None:
                mac_lost()
                _rm(stmp)
                return False, st["finished"], "segment %d/%d: %s" % (i + 1, nseg, why)
            if got != n:
                mac_lost()
                _rm(stmp)
                return False, st["finished"], "segment %d/%d frame mismatch: %d != %d" % (
                    i + 1, nseg, got, n)
            with cond:
                os.replace(stmp, sf)
                done[i] = True
                st["finished"] += n
                st["mac_seg"], st["mac"] = None, 0
            published(i, "mac")
    finally:
        stop.set()
        with cond:
            cond.notify_all()
        if th is not None:
            th.join(HELPER_JOIN_SECONDS)
        with cond:
            st["closed"] = True
    if not all(done):                              # defensive: the queue can't lose a segment
        missing = [i + 1 for i, d in enumerate(done) if not d]
        return False, st["finished"], "segments never encoded: %s" % missing[:5]
    ok, why = concat_segments(seg_files, out_hevc)
    if not ok:
        return False, st["finished"], why
    k = st["ally_done"]
    return True, st["finished"], "encoded %d frames in %d segments%s" % (
        st["finished"], nseg, f" ({_segments_word(k)} on the helper)" if k else "")


def reencode_segments_tighter(dv_video: str, rpu: str, segdir: str, indices: list,
                              tight_cap_mbps: int, *, total_frames: int, fps: str,
                              master_display=None, max_cll=None, seg_seconds: int = SEG_SECONDS,
                              boundaries: list | None = None, abort=None, on_repair=None,
                              decode: dict | None = None, dv: bool = True,
                              ffmpeg=FFMPEG, x265=X265, dovi_tool=DOVI_TOOL):
    """PEAK REPAIR: delete and re-encode ONLY `indices` segments at a TIGHTER cap. The full-cap
    encode can measure over the gate — VBV bufsize == maxrate legally allows a 1-second burst
    up to ~2× cap while the gate is cap × PEAK_TOLERANCE — and re-running the identical encode
    reuses the identical segments, so it can never pass (user-caught: a movie parked at
    58.6 > 50 five times, shipped nothing). The resume manifest is deliberately unchanged
    (same source, same GLOBAL cap): a tighter segment is strictly safer to resume, and a
    mid-repair kill just resumes with the already-tightened segments. `dv=False`: the SDR
    master's segments (no RPU, Rec.709), as encode_capped_segmented. Returns (ok, reason)."""
    segs = plan_segments(total_frames, fps, seg_seconds, boundaries=boundaries)
    bad = plan_chunk_error(segs, decode)
    if bad:
        return False, bad
    for k, i in enumerate(indices, 1):
        if abort is not None and abort.is_set():
            return False, "aborted"
        if not (0 <= i < len(segs)):
            return False, f"repair segment index {i} out of range (plan has {len(segs)})"
        a, b = segs[i]
        n = b - a
        sf = os.path.join(segdir, f"seg_{i:04d}.hevc")
        stmp = sf + ".part"
        # A CHUNK-LANE segment whose Topaz chunk was already freed (the measured bound said it
        # could never be flagged — this is the rare miss) is re-capped from ITS OWN encode: one
        # more generation for this one segment instead of a fresh Topaz pass for the item.
        own = sf + ".own.hevc"
        from_own = False
        if decode and decode.get("chunks"):
            try:
                chunk, _first = chunk_for(decode["chunks"], a, n)
            except ValueError as e:
                return False, f"repair segment {i}: {e}"
            if chunk in (decode.get("freed") or ()) and not os.path.exists(chunk):
                if not os.path.exists(sf) and not os.path.exists(own):
                    return False, f"repair segment {i}: its Topaz chunk was freed and its encode is gone"
                from_own = True
        # built BEFORE the tight-cap note and the delete: a segment that can't be decoded
        # keeps its encoded file and records nothing
        try:
            dec = (build_es_decode_command(ffmpeg, own, fps) if from_own
                   else build_seg_decode_command(ffmpeg, dv_video, a, n, fps, decode))
        except ValueError as e:
            return False, f"repair segment {i}: {e}"
        _remember_tight_cap(segdir, i, tight_cap_mbps)   # BEFORE the delete: a kill from here on
                                                         # resumes this segment at the tight cap
        if from_own and not os.path.exists(own):
            os.replace(sf, own)                          # the source of this repair, kept aside
        _rm(sf); _rm(stmp)                               # the over-peak segment is what we're replacing
        rslice = os.path.join(segdir, f"seg_{i:04d}.rpu")
        if dv:
            ok, why = slice_rpu(rpu, a, b, total_frames, rslice, dovi_tool=dovi_tool)
            if not ok:
                return False, why
            enc = build_x265_command(x265, rslice, stmp, tight_cap_mbps, master_display, max_cll)
        else:
            enc = build_x265_command(x265, None, stmp, tight_cap_mbps, dv=False)
        # Surface WHICH segment is being re-capped and its LIVE frame progress — the k'th
        # of the flagged set, re-encoding frame `done` of the segment's `n`. Without this
        # the lane sat at "remux · 100%" for the whole repair pass and read as stuck
        # (user-caught 2026-08-06). Same per-frame source as the main encode's bar.
        cb = None
        if on_repair:
            of = len(indices)
            def cb(fr, _k=k, _i=i, _n=n, _of=of):
                try: on_repair(_k, _of, _i, min(fr, _n), _n)
                except Exception: pass
            cb(0)                                        # segment start: cut-out shows empty
        got, why, _t = _encode_pipe(dec, enc, stmp, abort, cb)
        _rm(rslice)
        fail = ("aborted" if why == "aborted" else
                f"repair segment {i}: {why}" if got is None else
                f"repair segment {i} frame mismatch: {got} != {n}" if got != n else None)
        if fail:
            _rm(stmp)
            if from_own and not os.path.exists(sf):
                os.replace(own, sf)                      # the segment as it was: the item stays
            return False, fail                           # whole for the next attempt
        os.replace(stmp, sf)                             # atomic publish, like the main encode
        if from_own:
            _rm(own)
        if os.path.exists(_hosts_path(segdir)):          # a helper-run segdir: the Mac made it now
            hosts = read_hosts(segdir)
            hosts[str(i)] = "mac"
            _write_hosts(segdir, hosts)
    return True, "re-capped %d segment(s) @ %d Mbps" % (len(indices), tight_cap_mbps)
