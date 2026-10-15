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
import subprocess

DOVI_TOOL = "/opt/homebrew/bin/dovi_tool"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
HDR_ANALYZER = os.path.expanduser("~/.topaz-pipeline/bin/hdr_analyzer_mvp")
HDR_ANALYZER_COMMIT = "383b0ad"     # tinof/hdr-analyze, built from source (cargo, Homebrew ffmpeg 8)

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
# overshoot in max-RGB), FFV1. ~4 MB/frame, ~9 GB per ~90 s chunk, one at a time, deleted as
# soon as it is measured (Visionary gates on 400 GB raw free, so never batch these). Topaz
# cuts its chunks at strong scene changes, so every chunk start is a scene start and per-chunk
# results join without a seam.

ANALYSIS_WIDTH, ANALYSIS_HEIGHT = 1920, 1080


def analysis_vf(cube: str | None) -> str:
    """Chain for the analysis intermediate. With a LUT: SDR -> PQ RGB -> 1080p (area, in RGB)
    -> 4:4:4. Without (an already-PQ source): area-downscale and 4:4:4."""
    scale = f"scale={ANALYSIS_WIDTH}:{ANALYSIS_HEIGHT}:flags=area"
    if cube:
        return lut_rgb_chain(cube) + "," + scale + "," + pq_rgb_to_yuv("yuv444p10le")
    return scale + ",format=yuv444p10le"


def build_analysis_intermediate_command(ffmpeg: str, chunk: str, out_mkv: str, cube: str | None) -> list:
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", chunk,
            "-map", "0:v:0", "-vf", analysis_vf(cube),
            "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
            "-color_range", "tv", "-c:v", "ffv1", "-level", "3", "-slices", "16", out_mkv]


def build_raw444_decode_command(ffmpeg: str, src: str) -> list:
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", src,
            "-map", "0:v:0", "-pix_fmt", "yuv444p10le", "-f", "rawvideo", "-"]


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
             height: int = ANALYSIS_HEIGHT) -> list:
    """Per-frame [anchor_pq, anchor_power] (rounded mean, std of PQ max-RGB) of a yuv444p10le
    PQ file — the analysis intermediate, so it costs one more decode of a file we already made."""
    size = width * height * 3 * 2
    p = subprocess.Popen(build_raw444_decode_command(ffmpeg, src), stdout=subprocess.PIPE)
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


def analyze_chunks(chunks: list, workdir: str, *, cube: str | None, analyzer: str = HDR_ANALYZER,
                   ffmpeg: str = FFMPEG, peak_estimator: str = "max", with_l4: bool = True,
                   threads: int | None = None, abort=None, on_chunk=None) -> dict:
    """Measure every chunk ([[first_frame, path], ..., [total, None]]) and return the merged
    sidecar. A finished chunk's sidecar (and its L4 list) is kept in `workdir` and reused on
    resume; the ~9 GB intermediate is always deleted."""
    os.makedirs(workdir, exist_ok=True)
    total = chunks[-1][0] if chunks and chunks[-1][1] is None else None
    real = [(a, p) for a, p in chunks if p]
    parts = []
    for k, (first, path) in enumerate(real):
        if abort is not None and abort.is_set():
            raise RuntimeError("aborted")
        out_bin = os.path.join(workdir, f"chunk_{k:04d}.bin")
        side = sidecar_path(out_bin)
        l4_path = os.path.join(workdir, f"chunk_{k:04d}.l4.json")
        if not os.path.exists(side) or (with_l4 and not os.path.exists(l4_path)):
            mkv = os.path.join(workdir, f"chunk_{k:04d}.mkv")
            try:
                r = subprocess.run(build_analysis_intermediate_command(ffmpeg, path, mkv, cube),
                                   capture_output=True, text=True, errors="replace")
                if r.returncode != 0:
                    raise RuntimeError(f"chunk {k}: intermediate failed: {r.stderr[-300:]}")
                r = subprocess.run(build_analyzer_command(analyzer, mkv, out_bin + ".part",
                                                          peak_estimator=peak_estimator, threads=threads),
                                   capture_output=True, text=True, errors="replace")
                if r.returncode != 0 or not os.path.exists(sidecar_path(out_bin + ".part")):
                    raise RuntimeError(f"chunk {k}: analyzer failed: {(r.stderr or r.stdout)[-300:]}")
                if with_l4:
                    with open(l4_path + ".part", "w") as f:
                        json.dump(frame_l4(mkv, ffmpeg=ffmpeg), f)
                    os.replace(l4_path + ".part", l4_path)
                os.replace(sidecar_path(out_bin + ".part"), side)   # publish last: resume trusts it
            finally:
                for junk in (mkv, out_bin + ".part"):
                    if os.path.exists(junk):
                        os.remove(junk)
        sc = load_sidecar(side)
        if with_l4:
            with open(l4_path) as f:
                sc["l4"] = json.load(f)
        parts.append((first, sc))
        if on_chunk:
            on_chunk(k + 1, len(real))
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
