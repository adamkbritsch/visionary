#!/usr/bin/env python3
"""What the native YouTube routes would cost on this Mac — P2g of the native-routes plan. READ-ONLY on its inputs: every file it makes lives in a fresh work dir that
it removes at the end (--keep keeps it), and it never writes beside --source or --master.

Each answer is one section of a JSON report (stdout, and --out):

  analysis    CPU-s/frame of dvnative's analysis chain on --source: dvnative's own FFVHuff
              intermediate command (1080p area -> captured LUT -> 4:4:4, dvnative.analysis_vf),
              hdr_analyzer_mvp, and the per-frame L4 child (at the loop's own 960x540), over
              --frames frames from --start.
              The range is read with an input -ss and -frames:v, as a range analysis reads one, so
              the seek's discarded decode is in the figure. Also the intermediate's bytes/frame.
  encode      VideoToolbox HEVC Main10 (p010le) through dvnative.lut_vf at 3840x2160, at the
              YouTube render bitrate (stages.youtube_render_kbps): fps, CPU-s/frame, and the
              clip's 1-s peak against the 57.5 Mbps gate. A sub-4K source is fitted and padded
              first (the plain-size route's picture). Without --source it runs on a synthetic
              4K pattern, flagged "synthetic" (the chain's floor, not a real item's cost).
  cbr         the same encode with -constant_bit_rate 1: does it run, and its bitrate and peak
              beside the plain (VBR) encode's.
  av1_hwdec   VideoToolbox AV1 hardware decode: VTIsHardwareDecodeSupported('av01'), and whether
              ffmpeg really gets VideoToolbox frames (it falls back to software SILENTLY, so the
              check keeps the frames on the GPU until hwdownload, which fails on a fallback).
              With an AV1 --source, also decode CPU-s/frame in software vs hardware.
  master_hdr  does --master (a shipped master; probed, never written) carry MDCV / CLL: ffprobe's
              first-frame side data, the stream's (container) side data, and the in-band SEI
              payload types read by trace_headers (137 mastering display, 144 content light).
  vt_hdr      can ffmpeg's hevc_videotoolbox write them: a tiny libx265 HDR10 clip with known
              MDCV/CLL re-encoded through VideoToolbox to MP4 and to raw HEVC, probed the same
              way. ffmpeg 8 ALSO fills first-frame side data from container boxes (mp4 mdcv/clli),
              so only the SEI payload types prove the metadata is in the bitstream — and only
              in-band SEI survives the raw-HEVC extraction dovi_tool inject-rpu works on.

Every measured child runs through dvnative.run_step at nice 10 (--nice): in its own process
group, reaped with os.wait4, so CPU-s are user+sys of the child and every descendant it waited
for. CPU-s barely depend on what else is running (wall time does); the load averages at start
and end are recorded beside them. --pre-vf puts a front end (a BT.601 matrix or full->limited
range conversion, a scale) ahead of both chains, to measure what it adds; --hwdec repeats the
intermediate and the encode with VideoToolbox decode. The exit status is 0 when every probe
that ran reached an answer (a "no" is an answer), 1 when one failed.

  python3 tools/route_cpu.py --source SRC [--master MASTER] [--frames 600] [--start 60]
      [--only analysis,encode,cbr,av1_hwdec,master_hdr,vt_hdr] [--hwdec] [--pre-vf FILTERS]
      [--lut CUBE|none] [--encode-size 3840x2160] [--kbps N] [--work DIR] [--keep] [--out FILE]
"""
from __future__ import annotations
import argparse
import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from fractions import Fraction

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "engine"))
import dvcap  # noqa: E402
import dvnative  # noqa: E402
import versions  # noqa: E402

FF = dvnative.FFMPEG
FFPROBE = dvcap.FFPROBE
REPORT_VERSION = 1
PROBES = ("analysis", "encode", "cbr", "av1_hwdec", "master_hdr", "vt_hdr")
NEEDS = {"analysis": "source", "master_hdr": "master"}     # probes that cannot run without an input
GiB = 1024 ** 3

DEFAULT_FRAMES = 600            # 25 s at 23.976: past every encoder's and analyser's warm-up
DEFAULT_START = 60.0            # past a video's intro/fade (cheap black frames understate it)
ENCODE_SIZE = (3840, 2160)
SYNTHETIC_RATE = "24000/1001"
NATIVE_4K_MIN_HEIGHT = 1700     # = stages.YOUTUBE_NATIVE_4K_MIN_HEIGHT (test-pinned)
KBPS_4K = 14000                 # = stages.YOUTUBE_RENDER_KBPS_4K: a native 4K source
KBPS_SUB4K = 20000              # = stages.YOUTUBE_RENDER_KBPS: every smaller source
CPU_BUDGET = 0.66               # the plan's YouTube budget: CPU-s/frame, weighted by volume
INTERMEDIATE_BYTES_PER_FRAME = int(6.5 * 1024 ** 2)
                                # = nativelane.INTERMEDIATE_BYTES_PER_FRAME (test-pinned): FFVHuff
                                # 1080p 4:4:4 10-bit, the grainy-chunk worst case rounded up
MIN_FREE_GB = 150               # = nativelane.MID_ANALYSIS_FLOOR_GB: free - intermediate >= this
TIMEOUT_FACTOR = 4              # x dvnative.step_timeout: a nice-10 child on a machine the live
                                # pipeline saturates is slow, not wedged — only a hang may trip it
SEI_MDCV, SEI_CLL = 137, 144    # H.265 SEI payload types: mastering display, content light level
HDR_PROBE_FRAMES = 12           # frames in the tiny vt_hdr / synthetic AV1 clips and the hw check
VT_HDR_CLL = "1000,400"         # the vt_hdr clip's MaxCLL,MaxFALL (its MDCV: dvcap's fallback)
AV1_ENCODERS = ("libsvtav1", "libaom-av1", "librav1e")    # any one makes the synthetic AV1 clip
QUICK_TIMEOUT = 120             # seconds for an unmeasured probe child (ffprobe, the tiny clips)
MEASURE_POLL = 0.05             # run_step's poll for a measured child: its wall time is known to
                                # this resolution (the engine's 0.5 s would blur a short step)
LUT_DEFAULT = getattr(versions, "NATIVE_LUT_PATH", "~/.topaz-pipeline/luts/rcm_sdr709_to_pq1000.cube")
PIPELINE_HOME = os.path.expanduser("~/.topaz-pipeline")
VT_FRAMEWORK = "/System/Library/Frameworks/VideoToolbox.framework/VideoToolbox"


# ---- pure helpers -----------------------------------------------------------------------

def render_kbps(height) -> int:
    """PURE. The YouTube render bitrate for a source `height` lines tall (as
    stages.youtube_render_kbps): 14000 from 1700 lines up, 20000 below."""
    return KBPS_4K if (height or 0) >= NATIVE_4K_MIN_HEIGHT else KBPS_SUB4K


def fourcc(code: str) -> int:
    """PURE. A four-character code as a CMVideoCodecType: 'av01' -> 0x61763031."""
    b = (code or "").encode("ascii")
    if len(b) != 4:
        raise ValueError(f"a fourcc has 4 characters: {code!r}")
    return int.from_bytes(b, "big")


def per_frame(cpu, frames):
    """PURE. CPU-s per frame to 4 places; None without CPU or frames."""
    return round(cpu / frames, 4) if cpu is not None and frames else None


def rate(fps) -> float:
    """PURE. '24000/1001' / '30' / 29.97 -> float; 0.0 when unreadable."""
    try:
        return float(Fraction(str(fps)))
    except (ValueError, ZeroDivisionError):
        return 0.0


def parse_size(text: str) -> tuple:
    """PURE. '3840x2160' -> (3840, 2160). Even, positive sizes only (4:2:0)."""
    m = re.fullmatch(r"(\d+)x(\d+)", (text or "").strip())
    if not m:
        raise ValueError(f"size must be WxH: {text!r}")
    w, h = int(m.group(1)), int(m.group(2))
    if w <= 0 or h <= 0 or w % 2 or h % 2:
        raise ValueError(f"size must be even and positive: {text!r}")
    return w, h


def parse_only(text) -> tuple:
    """PURE. '--only' list -> probe names in report order (all of them when empty)."""
    if not text:
        return PROBES
    names = [n.strip() for n in str(text).split(",") if n.strip()]
    bad = [n for n in names if n not in PROBES]
    if bad:
        raise ValueError(f"unknown probe(s) {', '.join(bad)}; choose from {', '.join(PROBES)}")
    return tuple(n for n in PROBES if n in names)


def chain(*parts) -> str:
    """PURE. Filter chain of the non-empty parts."""
    return ",".join(p for p in parts if p)


def clamp_start(start: float, duration, frames: int, fps) -> float:
    """PURE. Move `start` back so `frames` frames fit before the end (to 0 when they never do);
    unchanged when the duration or rate is unknown."""
    start = max(0.0, float(start or 0.0))
    fr = rate(fps)
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        return start
    if duration <= 0 or fr <= 0:
        return start
    latest = duration - frames / fr - 1.0      # a second short of the end: the last GOP can be short
    return round(max(0.0, min(start, latest)), 3)


def input_args(src: str, start: float = 0.0, hwdec: bool = False) -> list:
    """PURE. ffmpeg input options for `src`: VideoToolbox decode (when asked — it must come
    before -i), then an input seek. The seek is frame-exact: ffmpeg decodes from the keyframe
    before `start` and drops what precedes it, a cost a range analysis pays too."""
    out = ["-hwaccel", "videotoolbox"] if hwdec else []
    if start and float(start) > 0:
        out += ["-ss", f"{float(start):.3f}"]
    return out + ["-i", src]


def synthetic_input(size, fps: str = SYNTHETIC_RATE) -> list:
    """PURE. A BT.709-tagged limited 4:2:0 test pattern at `size` — what a YouTube SDR source
    decodes to. Its content is not a real item's: what it measures is the chain's floor."""
    w, h = size
    return ["-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r={fps},format=yuv420p,"
            "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"]


def with_input(cmd: list, inp: list, frames=None, pre_vf=None) -> list:
    """PURE. `cmd` (one `-i X`, its output path last) reading `inp` instead, with `pre_vf` in
    front of its -vf chain and `-frames:v N` before the output. Nothing else changes, so the
    picture chain stays the one the engine builds."""
    i = cmd.index("-i")
    out = cmd[:i] + list(inp) + cmd[i + 2:-1]
    if pre_vf:
        j = out.index("-vf")
        out[j + 1] = chain(pre_vf, out[j + 1])
    if frames:
        out += ["-frames:v", str(int(frames))]
    return out + cmd[-1:]


def analysis_chain(cube, pre_vf=None) -> str:
    """PURE. The analysis picture: `pre_vf`, then dvnative.analysis_vf."""
    return chain(pre_vf, dvnative.analysis_vf(cube))


def build_analysis_command(inp: list, mkv: str, cube, frames: int, pre_vf=None) -> list:
    """PURE. dvnative's own analysis-intermediate command (its -vf chain untouched), reading
    `frames` frames from `inp` instead of a whole chunk. Its software-decode form: `inp`
    (input_args' hwdec) is what picks the decoder, so the plain figure stays a software one."""
    base = dvnative.build_analysis_intermediate_command(FF, "SRC", mkv, cube, hwaccel=False)
    return with_input(base, inp, frames, pre_vf)


def fit_pad(w: int, h: int) -> str:
    """PURE. Fit inside w x h (aspect kept, lanczos) and pad to it, centred: the plain-size
    route's picture before the LUT."""
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")


def encode_vf(cube, src_size, out_size=ENCODE_SIZE, pre_vf=None) -> str:
    """PURE. The encode's picture: `pre_vf`, then fit+pad when the source is another size, then
    dvnative.lut_vf into p010le — exactly lut_vf alone for a 4K source (the 4K route is the LUT
    only). cube=None (an already-PQ source) skips the LUT."""
    w, h = out_size
    scale = fit_pad(w, h) if tuple(src_size or ()) != (w, h) else None
    return chain(pre_vf, scale, dvnative.lut_vf(cube, "p010le") if cube else "format=p010le")


def build_encode_command(inp: list, out: str, vf: str, kbps: int, frames: int, extra=()) -> list:
    """PURE. VideoToolbox HEVC Main10 of `frames` frames through `vf`, tagged BT.2020 PQ like a
    render. -allow_sw 0: a software fallback must fail, never pass as hardware throughput."""
    return [FF, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *inp,
            "-map", "0:v:0", "-an", "-frames:v", str(int(frames)), "-vf", vf,
            "-c:v", "hevc_videotoolbox", "-profile:v", "main10", "-b:v", f"{int(kbps)}k",
            "-allow_sw", "0", *extra, "-tag:v", "hvc1",
            "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
            "-color_range", "tv", out]


def build_decode_command(inp: list, frames: int) -> list:
    """PURE. Decode `frames` frames to nothing (with -hwaccel in `inp`, the frames still come
    back to system memory, as a filter chain would need them)."""
    return [FF, "-hide_banner", "-nostdin", "-loglevel", "error", *inp, "-map", "0:v:0",
            "-frames:v", str(int(frames)), "-f", "null", "-"]


def build_hwdec_check_command(path: str, frames: int = HDR_PROBE_FRAMES) -> list:
    """PURE. Decode through VideoToolbox keeping the frames on the GPU side until hwdownload.
    ffmpeg falls back to software decode without failing, but software frames make the
    hwdownload filter fail, so exit 0 here means the frames really came from VideoToolbox."""
    return [FF, "-hide_banner", "-nostdin", "-loglevel", "verbose",
            "-hwaccel", "videotoolbox", "-hwaccel_output_format", "videotoolbox_vld",
            "-i", path, "-map", "0:v:0", "-frames:v", str(int(frames)),
            "-vf", "hwdownload,format=nv12|p010le", "-f", "null", "-"]


def hw_decode_verdict(rc: int, log: str) -> tuple:
    """PURE. (hardware?, why) from build_hwdec_check_command's exit status and verbose log."""
    log = log or ""
    if rc == 0 and "pixfmt:videotoolbox_vld" in log:
        return True, "decoded to videotoolbox_vld frames"
    for line in reversed(log.splitlines()):
        low = line.lower()
        if ("hwaccel" in low or "hardware" in low) and any(
                w in low for w in ("fail", "error", "not support", "doesn't support")):
            return False, line.split("]", 1)[-1].strip()[:200]
    return False, f"ffmpeg exited {rc} without VideoToolbox frames"


def build_trace_command(path: str, packets: int = 2) -> list:
    """PURE. trace_headers over the extradata and the first `packets` video packets: a stream
    copy, nothing decoded and nothing written."""
    return [FF, "-hide_banner", "-nostdin", "-loglevel", "info", "-i", path, "-map", "0:v:0",
            "-c:v", "copy", "-bsf:v", "trace_headers", "-frames:v", str(int(packets)),
            "-f", "null", "-"]


_SEI_FIELD = re.compile(r"\b(ff_byte|last_payload_type_byte|last_payload_size_byte)\b.*=\s*(\d+)\s*$")


def parse_sei_types(log: str) -> dict:
    """PURE. trace_headers output -> {"extradata": [...], "packets": [...]}, the SEI payload
    types met in each place, sorted. Each SEI message is [ff_byte...] last_payload_type_byte
    [ff_byte...] last_payload_size_byte, and each ff_byte adds 255 to the field it precedes
    (H.265 7.3.5), so ff_bytes count toward the type only until its last byte is read."""
    out = {"extradata": set(), "packets": set()}
    where, ffs = None, 0
    for line in (log or "").splitlines():
        if "trace_headers" not in line:
            continue
        body = line.split("]", 1)[-1].strip()
        if body.startswith("Extradata"):
            where, ffs = "extradata", 0
        elif body.startswith("Packet:"):
            where, ffs = "packets", 0
        else:
            m = _SEI_FIELD.search(body)
            if not m:
                continue
            if m.group(1) == "ff_byte":
                ffs += 1
            else:
                if m.group(1) == "last_payload_type_byte" and where:
                    out[where].add(255 * ffs + int(m.group(2)))
                ffs = 0
    return {k: sorted(v) for k, v in out.items()}


def hdr_side_data(side_data) -> dict:
    """PURE. One ffprobe side_data_list -> {types, mastering_display (x265 string|None),
    max_cll ('a,b'|None)}."""
    types, md, cll = [], None, None
    for sd in side_data or []:
        t = sd.get("side_data_type", "")
        types.append(t)
        if "Mastering display" in t:
            md = dvcap.mastering_to_x265(sd)
        elif "Content light" in t:
            cll = "%d,%d" % (int(sd.get("max_content", 0)), int(sd.get("max_average", 0)))
    return {"types": types, "mastering_display": md, "max_cll": cll}


def hdr_facts(probe: dict, sei: dict) -> dict:
    """PURE. One file's MDCV/CLL picture from build_side_data_probe's JSON and parse_sei_types:
    where each one is found — SEI in the packets (in-band), SEI in the extradata (hvcC arrays:
    in-band only at the start of an extracted stream), the stream's side data (a container
    box), first-frame side data (what the plan asked of ffprobe; ffmpeg 8 fills it from either)."""
    frame = hdr_side_data(((probe.get("frames") or [{}])[0]).get("side_data_list"))
    stream = hdr_side_data(((probe.get("streams") or [{}])[0]).get("side_data_list"))
    pkt, ext = set(sei.get("packets") or ()), set(sei.get("extradata") or ())

    def where(code, key):
        return {"sei_in_packets": code in pkt, "sei_in_extradata": code in ext,
                "container": stream[key] is not None, "frame_side_data": frame[key] is not None,
                "value": frame[key] or stream[key]}

    return {"mdcv": where(SEI_MDCV, "mastering_display"), "cll": where(SEI_CLL, "max_cll"),
            "sei_payload_types": {"extradata": sorted(ext), "packets": sorted(pkt)},
            "frame_side_data_types": frame["types"], "stream_side_data_types": stream["types"]}


def encoder_options(help_text: str) -> list:
    """PURE. Option names from `ffmpeg -h encoder=X` ('  -constant_bit_rate <boolean> ...')."""
    return sorted(set(re.findall(r"^\s+-([a-z0-9_]+)\s+<", help_text or "", re.M)))


def source_facts(probe: dict, path: str) -> dict:
    """PURE. ffprobe -show_streams -show_format JSON -> what the report says about a source."""
    s = (probe.get("streams") or [{}])[0]
    fmt = probe.get("format") or {}
    try:
        duration = float(s.get("duration") or fmt.get("duration") or 0) or None
    except (TypeError, ValueError):
        duration = None
    fps = s.get("avg_frame_rate") if rate(s.get("avg_frame_rate")) > 0 else s.get("r_frame_rate")
    return {"path": path, "codec": s.get("codec_name"), "width": s.get("width"),
            "height": s.get("height"), "pix_fmt": s.get("pix_fmt"), "fps": fps,
            "duration_s": duration, "color_transfer": s.get("color_transfer"),
            "color_primaries": s.get("color_primaries"), "color_space": s.get("color_space"),
            "color_range": s.get("color_range")}


def intermediate_bytes(frames: int) -> int:
    """PURE. Disk the analysis intermediate of `frames` frames may take (nativelane's figure)."""
    return max(0, int(frames)) * INTERMEDIATE_BYTES_PER_FRAME


def disk_ok(free_bytes: int, frames: int, min_free_gb: float = MIN_FREE_GB) -> bool:
    """PURE. Room for the intermediate with `min_free_gb` GiB still free after it — the native
    lane's own mid-analysis floor, so a measurement never takes what the pipeline gates on."""
    return free_bytes - intermediate_bytes(frames) >= min_free_gb * GiB


def unsafe_path(path: str, home: str = PIPELINE_HOME) -> bool:
    """PURE (realpath aside). True for a path inside the pipeline's own state dir."""
    p, h = os.path.realpath(path), os.path.realpath(home)
    return p == h or p.startswith(h + os.sep)


def budget(report: dict) -> dict:
    """PURE. This source's analysis + encode CPU-s/frame against the plan's budget. The budget
    is a mean weighted by YouTube volume, so one source only shows where that route sits; a
    synthetic encode does not count."""
    a = (report.get("analysis") or {}).get("cpu_s_per_frame")
    enc = report.get("encode") or {}
    e = None if enc.get("synthetic") else enc.get("cpu_s_per_frame")
    total = round(a + e, 4) if a is not None and e is not None else None
    return {"budget_cpu_s_per_frame": CPU_BUDGET, "analysis": a, "encode": e,
            "analysis_plus_encode": total,
            "within_budget": (total <= CPU_BUDGET) if total is not None else None}


def write_report(path: str, report: dict) -> None:
    """Write `report` as JSON through a .part file, so a reader never sees half of it."""
    part = path + ".part"
    with open(part, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    os.replace(part, path)


# ---- running children -------------------------------------------------------------------

def _rm(*paths) -> None:
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def measure(cmd: list, *, frames: int, nice: int, what: str, cwd=None) -> dict:
    """Run one child at `nice` through dvnative.run_step (own process group, os.wait4) ->
    {rc, wall_s, cpu_s, tail}. Raises RuntimeError on a timeout."""
    t0 = time.monotonic()
    rc, tail, cpu = dvnative.run_step(dvnative.niced(cmd, nice), cwd=cwd, what=what,
                                      timeout=TIMEOUT_FACTOR * dvnative.step_timeout(frames),
                                      poll=MEASURE_POLL)
    return {"rc": rc, "wall_s": round(time.monotonic() - t0, 3), "cpu_s": round(cpu or 0.0, 3),
            "tail": tail}


def _ok(step: dict, what: str) -> dict:
    """The step's figures without its output tail; RuntimeError (quoting the tail) on failure."""
    if step["rc"] != 0:
        raise RuntimeError(f"{what} failed (rc={step['rc']}): {step['tail'][-400:]}")
    return {k: step[k] for k in ("wall_s", "cpu_s")}


def quick(cmd: list, timeout: float = QUICK_TIMEOUT):
    """An unmeasured probe child -> CompletedProcess with text output."""
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=timeout)


def ffprobe_json(path: str, *args) -> dict:
    r = quick([FFPROBE, "-v", "error", "-select_streams", "v:0", *args, "-of", "json", path])
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {(r.stderr or '')[-300:]}")
    return json.loads(r.stdout or "{}")


def probe_source(path: str) -> dict:
    return source_facts(ffprobe_json(path, "-show_streams", "-show_format"), path)


def encoders() -> str:
    return quick([FF, "-hide_banner", "-encoders"]).stdout or ""


def has_encoder(name: str, listing: str) -> bool:
    """PURE. Is encoder `name` in `ffmpeg -encoders` output?"""
    return re.search(rf"^\s*\S{{6}}\s+{re.escape(name)}\s", listing or "", re.M) is not None


def file_hdr_facts(path: str) -> dict:
    """ffprobe (first frame + stream) and trace_headers of `path` -> hdr_facts. Read-only."""
    probe = ffprobe_json(path, "-show_streams", "-show_frames", "-read_intervals", "%+#1")
    r = quick(build_trace_command(path))
    if r.returncode != 0:
        raise RuntimeError(f"trace_headers failed on {path}: {(r.stderr or '')[-300:]}")
    return hdr_facts(probe, parse_sei_types(r.stderr))


def vt_hw_decode_supported(code: str = "av01"):
    """VTIsHardwareDecodeSupported(code), asked of the VideoToolbox framework through ctypes;
    None when it cannot be asked (no framework, no symbol)."""
    try:
        vt = ctypes.cdll.LoadLibrary(VT_FRAMEWORK)
        fn = vt.VTIsHardwareDecodeSupported
    except (OSError, AttributeError):
        return None
    fn.argtypes, fn.restype = [ctypes.c_uint32], ctypes.c_bool
    return bool(fn(fourcc(code)))


def host_facts() -> dict:
    def out(cmd):
        try:
            return (quick(cmd, 10).stdout or "").strip().splitlines()[0]
        except (OSError, subprocess.SubprocessError, IndexError):
            return None
    return {"machine": platform.machine(), "macos": platform.mac_ver()[0] or None,
            "cpu": out(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"]),
            "cpus": os.cpu_count(), "ffmpeg": out([FF, "-hide_banner", "-version"]),
            "python": platform.python_version()}


def load_avg():
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except OSError:
        return None


# ---- probes -----------------------------------------------------------------------------

def measure_analysis(src: str, work: str, *, cube, start: float, frames: int, nice: int,
                     analyzer: str, pre_vf=None, hwdec: bool = False,
                     min_free_gb: float = MIN_FREE_GB) -> dict:
    """dvnative's analysis chain over `frames` frames of `src` from `start` -> CPU-s/frame per
    step and in total. The intermediate is deleted before this returns (with hwdec, a second
    intermediate decoded by VideoToolbox is made after the first is gone)."""
    if not os.path.exists(analyzer):
        raise RuntimeError(f"no analyzer at {analyzer}")
    free = shutil.disk_usage(work).free
    if not disk_ok(free, frames, min_free_gb):
        raise RuntimeError(f"refused: {free / GiB:.0f} GiB free, the intermediate may take "
                           f"{intermediate_bytes(frames) / GiB:.1f} GiB and {min_free_gb} GiB must stay free")
    mkv = os.path.join(work, "analysis.mkv")
    out_bin = os.path.join(work, "analysis.bin")
    l4 = os.path.join(work, "analysis.l4.json")
    steps = {}
    try:
        steps["intermediate"] = _ok(measure(build_analysis_command(input_args(src, start), mkv, cube,
                                                                   frames, pre_vf),
                                            frames=frames, nice=nice, what="intermediate"),
                                    "intermediate")
        mkv_bytes = os.path.getsize(mkv)
        steps["analyzer"] = _ok(measure(dvnative.build_analyzer_command(analyzer, mkv, out_bin),
                                        frames=frames, nice=nice, what="analyzer"), "analyzer")
        sc = dvnative.load_sidecar(dvnative.sidecar_path(out_bin))
        n = dvnative.stream_frames(sc)
        steps["l4"] = _ok(measure(dvnative.build_l4_command(sys.executable, mkv, l4, ffmpeg=FF,
                                                            width=dvnative.L4_WIDTH,
                                                            height=dvnative.L4_HEIGHT, scale=True),
                                  frames=frames, nice=nice, what="L4", cwd=dvnative.ENGINE_DIR), "L4")
        with open(l4) as f:
            l4_frames = len(json.load(f))
        _rm(mkv)
        if l4_frames != n:
            raise RuntimeError(f"L4 has {l4_frames} frames, the analyzer {n}")
        if not n:
            raise RuntimeError("the analyzer measured no frames")
        if hwdec:       # an extra figure: its failure is recorded, the main result still stands
            what = "intermediate (VideoToolbox decode)"
            try:
                steps["intermediate_hwdec"] = _ok(measure(
                    build_analysis_command(input_args(src, start, hwdec=True), mkv, cube, frames,
                                           pre_vf), frames=frames, nice=nice, what=what), what)
            except RuntimeError as e:
                steps["intermediate_hwdec"] = {"error": str(e)}
    finally:
        _rm(mkv, out_bin, dvnative.sidecar_path(out_bin), l4)
    for st in steps.values():
        if "cpu_s" in st:
            st["cpu_s_per_frame"] = per_frame(st["cpu_s"], n)
    core = [steps[k] for k in ("intermediate", "analyzer", "l4")]
    cpu = sum(st["cpu_s"] for st in core)
    out = {"start_s": start, "frames_requested": frames, "frames": n, "l4_frames": l4_frames,
           "scenes": len(sc.get("scenes") or []), "lut": cube, "chain": analysis_chain(cube, pre_vf),
           "steps": steps, "cpu_s": round(cpu, 3), "cpu_s_per_frame": per_frame(cpu, n),
           "wall_s": round(sum(st["wall_s"] for st in core), 3),
           "intermediate_bytes_per_frame": int(mkv_bytes / n)}
    if "cpu_s" in steps.get("intermediate_hwdec", {}):
        hw_cpu = cpu - steps["intermediate"]["cpu_s"] + steps["intermediate_hwdec"]["cpu_s"]
        out["cpu_s_per_frame_hwdec"] = per_frame(hw_cpu, n)
    return out


def measure_encode(inp: list, work: str, *, src_size, cube, frames: int, kbps: int, out_size,
                   nice: int, pre_vf=None, extra=(), tag: str = "encode") -> dict:
    """One VideoToolbox encode through encode_vf -> its figures. rc != 0 is returned (with the
    output tail), not raised: for the cbr probe a refusal is the answer."""
    out = os.path.join(work, f"{tag}.mp4")
    vf = encode_vf(cube, src_size, out_size, pre_vf)
    try:
        st = measure(build_encode_command(inp, out, vf, kbps, frames, extra),
                     frames=frames, nice=nice, what=tag)
        res = {"rc": st["rc"], "vf": vf, "kbps": int(kbps), "size": "%dx%d" % tuple(out_size),
               "extra": list(extra), "wall_s": st["wall_s"], "cpu_s": st["cpu_s"]}
        if st["rc"] != 0 or not os.path.exists(out):
            res["error_tail"] = st["tail"][-400:]
            return res
        n = dvcap.count_hevc_frames(out)
        peak = dvcap.video_peak_1s_mbps(out)
        fmt = ffprobe_json(out, "-show_format").get("format") or {}
        try:
            mean = round(int(fmt.get("bit_rate")) / 1e6, 2)
        except (TypeError, ValueError):
            mean = None
        res.update(frames=n, fps=round(n / st["wall_s"], 2) if st["wall_s"] > 0 else None,
                   cpu_s_per_frame=per_frame(st["cpu_s"], n), mean_mbps=mean,
                   peak_1s_mbps=round(peak, 2),
                   peak_gate_mbps=round(dvcap.DEFAULT_PEAK_MBPS * dvcap.PEAK_TOLERANCE, 2),
                   under_peak_gate=dvcap.peak_ok(peak, dvcap.DEFAULT_PEAK_MBPS))
        return res
    finally:
        _rm(out)


def encode_probe(ctx: dict, extra=(), tag: str = "encode") -> dict:
    res = measure_encode(ctx["inp"], ctx["work"], src_size=ctx["src_size"], cube=ctx["cube"],
                         frames=ctx["frames"], kbps=ctx["kbps"], out_size=ctx["out_size"],
                         nice=ctx["nice"], pre_vf=ctx["pre_vf"], extra=extra, tag=tag)
    res["synthetic"] = ctx["synthetic"]
    return res


def probe_encode(ctx: dict) -> dict:
    res = encode_probe(ctx)
    if res["rc"] != 0:
        raise RuntimeError(f"VideoToolbox encode failed (rc={res['rc']}): {res.get('error_tail', '')}")
    if ctx["hwdec"] and not ctx["synthetic"]:
        hw = dict(ctx, inp=input_args(ctx["src"], ctx["start"], hwdec=True))
        h = encode_probe(hw, tag="encode_hwdec")
        res["hwdec"] = {k: h.get(k) for k in ("rc", "frames", "fps", "cpu_s", "cpu_s_per_frame",
                                              "error_tail") if k in h}
    return res


def probe_cbr(ctx: dict, vbr=None) -> dict:
    """-constant_bit_rate 1 beside the plain encode (the encode probe's result when it ran)."""
    listed = "constant_bit_rate" in encoder_options(
        quick([FF, "-hide_banner", "-h", "encoder=hevc_videotoolbox"]).stdout)
    if not vbr or vbr.get("rc") != 0:
        vbr = encode_probe(ctx, tag="vbr")
    cbr = encode_probe(ctx, extra=("-constant_bit_rate", "1"), tag="cbr")
    works = cbr["rc"] == 0 and (cbr.get("frames") or 0) > 0
    keys = ("rc", "frames", "fps", "cpu_s_per_frame", "mean_mbps", "peak_1s_mbps",
            "under_peak_gate", "error_tail")
    return {"option_listed": listed, "works": works, "kbps": ctx["kbps"],
            "synthetic": ctx["synthetic"],
            "cbr": {k: cbr.get(k) for k in keys if k in cbr},
            "vbr": {k: vbr.get(k) for k in keys if k in vbr}}


def make_av1(work: str, listing: str):
    """A tiny synthetic 8-bit AV1 clip in `work` from the first AV1 encoder ffmpeg has ->
    (path, encoder), or (None, why)."""
    enc = next((e for e in AV1_ENCODERS if has_encoder(e, listing)), None)
    if not enc:
        return None, "ffmpeg has no AV1 encoder (" + ", ".join(AV1_ENCODERS) + ")"
    out = os.path.join(work, "synthetic_av1.mp4")
    r = quick(dvnative.niced([FF, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                              *synthetic_input((256, 144)), "-frames:v", str(HDR_PROBE_FRAMES),
                              "-c:v", enc, out]))
    if r.returncode != 0 or not os.path.exists(out):
        return None, f"{enc} failed: {(r.stderr or '')[-200:]}"
    return out, enc


def probe_av1_hwdec(ctx: dict) -> dict:
    res = {"vt_api_av01": vt_hw_decode_supported("av01")}
    info = ctx.get("src_info") or {}
    real = info.get("codec") == "av1"
    if real:
        path, res["input"] = ctx["src"], "source"
    else:
        path, what = make_av1(ctx["work"], encoders())
        res["input"] = f"synthetic ({what})" if path else None
        if not path:
            res["ffmpeg_hw_frames"], res["why"] = None, what
            return res
    r = quick(build_hwdec_check_command(path))
    res["ffmpeg_hw_frames"], res["why"] = hw_decode_verdict(r.returncode, r.stderr)
    if real:
        n = ctx["frames"]
        sw = _ok(measure(build_decode_command(input_args(path, ctx["start"]), n),
                         frames=n, nice=ctx["nice"], what="software decode"), "software decode")
        res["decode_software"] = dict(sw, cpu_s_per_frame=per_frame(sw["cpu_s"], n))
        if res["ffmpeg_hw_frames"]:
            hw = _ok(measure(build_decode_command(input_args(path, ctx["start"], hwdec=True), n),
                             frames=n, nice=ctx["nice"], what="VideoToolbox decode"),
                     "VideoToolbox decode")
            res["decode_videotoolbox"] = dict(hw, cpu_s_per_frame=per_frame(hw["cpu_s"], n))
        res["decode_frames"] = n
    return res


def probe_master_hdr(master: str) -> dict:
    facts = file_hdr_facts(master)
    dv = next((t for t in facts["stream_side_data_types"] if "DOVI" in t), None)
    return dict(facts, path=master, dolby_vision_record=dv is not None)


def probe_vt_hdr(ctx: dict) -> dict:
    """A tiny libx265 HDR10 clip (dvcap's fallback MDCV, MaxCLL 1000,400) -> VideoToolbox, to
    MP4 and to raw HEVC; where the metadata ends up in each."""
    if not has_encoder("libx265", encoders()):
        raise RuntimeError("ffmpeg has no libx265 to make the HDR10 test clip")
    work = ctx["work"]
    src = os.path.join(work, "vt_hdr_src.mp4")
    params = (f"log-level=none:hdr10=1:repeat-headers=1:"
              f"master-display={dvcap.FALLBACK_MASTER_DISPLAY}:max-cll={VT_HDR_CLL}")
    r = quick(dvnative.niced([FF, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                              "-f", "lavfi", "-i", f"testsrc2=s=256x144:r={SYNTHETIC_RATE}",
                              "-frames:v", str(HDR_PROBE_FRAMES), "-pix_fmt", "yuv420p10le",
                              "-c:v", "libx265", "-x265-params", params,
                              "-color_primaries", "bt2020", "-color_trc", "smpte2084",
                              "-colorspace", "bt2020nc", src], ctx["nice"]))
    if r.returncode != 0:
        raise RuntimeError(f"libx265 test clip failed: {(r.stderr or '')[-300:]}")
    res = {"expected": {"mastering_display": dvcap.FALLBACK_MASTER_DISPLAY, "max_cll": VT_HDR_CLL},
           "source": file_hdr_facts(src)}
    for kind, name in (("mp4", "vt_hdr.mp4"), ("raw_hevc", "vt_hdr.hevc")):
        out = os.path.join(work, name)
        r = quick(dvnative.niced([FF, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                                  "-i", src, "-map", "0:v:0", "-c:v", "hevc_videotoolbox",
                                  "-profile:v", "main10", "-pix_fmt", "p010le", "-b:v", "2000k",
                                  "-allow_sw", "0", out], ctx["nice"]))
        if r.returncode != 0:
            raise RuntimeError(f"VideoToolbox encode ({kind}) failed: {(r.stderr or '')[-300:]}")
        res[kind] = file_hdr_facts(out)
    src_sei = res["source"]["mdcv"]["sei_in_packets"] and res["source"]["cll"]["sei_in_packets"]
    if not src_sei:
        raise RuntimeError("the libx265 test clip carries no in-band MDCV/CLL SEI: no control")
    raw = res["raw_hevc"]
    res["writes_in_band_sei"] = {"mdcv": raw["mdcv"]["sei_in_packets"], "cll": raw["cll"]["sei_in_packets"]}
    mp4 = res["mp4"]
    res["mp4_container_boxes"] = {"mdcv": mp4["mdcv"]["container"], "cll": mp4["cll"]["container"]}
    res["values_kept"] = (mp4["mdcv"]["value"] == dvcap.FALLBACK_MASTER_DISPLAY
                          and mp4["cll"]["value"] == VT_HDR_CLL)
    return res


def run_probe(fn) -> dict:
    """One section: fn()'s dict plus ok/probe_s; an exception becomes {"ok": False, "error"}
    so the remaining probes still run."""
    t0 = time.monotonic()
    try:
        out = fn()
        out["ok"] = True
    except Exception as e:      # a measurement tool reports a failed probe and carries on
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    out["probe_s"] = round(time.monotonic() - t0, 2)
    return out


# ---- main -------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Measure the native YouTube routes' CPU cost and "
                                             "VideoToolbox abilities (JSON report).")
    ap.add_argument("--source", help="a YouTube source (4K AV1, 1080p H.264, ...); read only")
    ap.add_argument("--master", help="a shipped master to check for MDCV/CLL; read only")
    ap.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    ap.add_argument("--start", type=float, default=DEFAULT_START,
                    help="seconds into --source (moved back when the frames would not fit)")
    ap.add_argument("--only", help="comma list of: " + ", ".join(PROBES))
    ap.add_argument("--lut", default=LUT_DEFAULT, help="the captured cube, or 'none' (a PQ source)")
    ap.add_argument("--pre-vf", help="filters in front of both chains (a 601 or range front end, "
                                     "a scale) to measure what they add")
    ap.add_argument("--hwdec", action="store_true",
                    help="also measure the analysis intermediate and the encode with VideoToolbox decode")
    ap.add_argument("--encode-size", default="%dx%d" % ENCODE_SIZE)
    ap.add_argument("--kbps", type=int, help="encode bitrate (default: the YouTube render bitrate "
                                             "for the source's height)")
    ap.add_argument("--nice", type=int, default=dvnative.NICE)
    ap.add_argument("--analyzer", default=dvnative.HDR_ANALYZER)
    ap.add_argument("--min-free-gb", type=float, default=MIN_FREE_GB)
    ap.add_argument("--work", help="parent of the work dir (default: the system temp dir)")
    ap.add_argument("--keep", action="store_true", help="keep the work dir")
    ap.add_argument("--out", help="also write the report here")
    a = ap.parse_args(argv)
    try:
        only = parse_only(a.only)
        out_size = parse_size(a.encode_size)
    except ValueError as e:
        ap.error(str(e))
    if a.frames <= 0:
        ap.error("--frames must be positive")
    cube = None if (a.lut or "").lower() == "none" else os.path.expanduser(a.lut)
    if cube and not os.path.exists(cube):
        ap.error(f"no LUT at {cube}")
    for flag, path in (("--source", a.source), ("--master", a.master)):
        if path and not os.path.isfile(path):
            ap.error(f"{flag}: no file at {path}")
    if a.only:
        for name in only:
            if name in NEEDS and not getattr(a, NEEDS[name]):
                ap.error(f"{name} needs --{NEEDS[name]}")
    for flag, path in (("--work", a.work), ("--out", a.out)):
        if path and unsafe_path(path):
            ap.error(f"{flag}: refusing a path inside {PIPELINE_HOME} (the pipeline's own state)")
    if a.work:
        os.makedirs(a.work, exist_ok=True)
    work = tempfile.mkdtemp(prefix="route_cpu.", dir=a.work or None)

    report = {"tool": "route_cpu", "version": REPORT_VERSION,
              "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "nice": a.nice,
              "host": host_facts(), "load_avg_start": load_avg()}
    try:
        info = probe_source(a.source) if a.source else None
        start = clamp_start(a.start, info.get("duration_s"), a.frames, info.get("fps")) if info else 0.0
        src_size = (info.get("width"), info.get("height")) if info else out_size
        ctx = {"src": a.source, "src_info": info, "start": start, "frames": a.frames,
               "cube": cube, "pre_vf": a.pre_vf, "hwdec": a.hwdec, "nice": a.nice,
               "work": work, "out_size": out_size, "src_size": src_size,
               "synthetic": info is None,
               "inp": input_args(a.source, start) if info else synthetic_input(out_size),
               "kbps": a.kbps or render_kbps(src_size[1] if info else out_size[1])}
        report["source"] = dict(info, start_s=start) if info else None
        runs = {
            "analysis": lambda: measure_analysis(a.source, work, cube=cube, start=start,
                                                 frames=a.frames, nice=a.nice, analyzer=a.analyzer,
                                                 pre_vf=a.pre_vf, hwdec=a.hwdec,
                                                 min_free_gb=a.min_free_gb),
            "encode": lambda: probe_encode(ctx),
            "cbr": lambda: probe_cbr(ctx, report.get("encode")),
            "av1_hwdec": lambda: probe_av1_hwdec(ctx),
            "master_hdr": lambda: probe_master_hdr(a.master),
            "vt_hdr": lambda: probe_vt_hdr(ctx),
        }
        for name in only:
            if name in NEEDS and not getattr(a, NEEDS[name]):
                report[name] = {"skipped": f"needs --{NEEDS[name]}"}
                continue
            print(f"route_cpu: {name} ...", file=sys.stderr, flush=True)
            report[name] = run_probe(runs[name])
    finally:
        if a.keep:
            print(f"route_cpu: work dir kept at {work}", file=sys.stderr)
        else:
            shutil.rmtree(work, ignore_errors=True)
    report["load_avg_end"] = load_avg()
    report["budget"] = budget(report)
    print(json.dumps(report, indent=2))
    if a.out:
        write_report(a.out, report)
    ran = [report[n] for n in only if "skipped" not in report[n]]
    return 0 if all(r.get("ok") for r in ran) else 1


if __name__ == "__main__":
    sys.exit(main())
