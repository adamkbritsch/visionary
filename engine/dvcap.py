"""Peak-bitrate-capped Dolby Vision re-encode — the stage that makes masters ACTUALLY playable.

WHY: Resolve's DV export uses the VideoToolbox hardware encoder, which has no real peak
control — S05E23 measured 27.5 Mbps average with a 138.9 Mbps single-second spike, which
underruns a SHIELD's link and glitches playback. VideoToolbox cannot fix this; x265 can.

HOW (the industry-standard conformant-DV path):
  1. `dovi_tool extract-rpu` pulls the per-frame Dolby Vision RPU out of Resolve's render.
  2. x265 re-encodes the video in its NATIVE Dolby Vision mode (`--dolby-vision-profile 8.1
     --dolby-vision-rpu`), which *mandates* VBV (`--vbv-maxrate/--vbv-bufsize`) because DV
     certification requires HRD conformance — a hard ceiling on any 1-second window. x265
     interleaves the RPU NALs itself; no separate inject step.
  3. The capped HEVC ES is muxed by the remux stage (MP4Box `:dvp=8.1:xps_inband:fps=`).

CONTRACT (user-dictated): there is NO uncapped fallback. If any step fails — RPU missing,
encode error, frame-count mismatch, DV lost, or the measured peak still over the cap — the
remux stage FAILS and the episode parks. An uncapped file IS the broken file; never ship it.

Quality: CRF-driven (quality-first) with the VBV ceiling only clipping the spikes; the cap
default (50 Mbps) is ~2x the measured average, so only the pathological seconds change.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
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

# Resolve project constant: "1000-nit, P3, D65, ST.2084" mastering display, in x265's units
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
    fps = s.get("r_frame_rate") or "24000/1001"
    try:
        start = float(s.get("start_time") or 0.0)   # container offset for the frame-exact seek
    except (TypeError, ValueError):
        start = 0.0
    md, cll = None, None
    # mastering metadata rides on frame side data (more reliable than the stream entry)
    for f in data.get("frames", [])[:1]:
        for sd in f.get("side_data_list", []):
            t = sd.get("side_data_type", "")
            if "Mastering display" in t:
                md = mastering_to_x265(sd)
            elif "Content light" in t:
                cll = "%d,%d" % (int(sd.get("max_content", 0)), int(sd.get("max_average", 0)))
    return {"frames": frames, "fps": fps, "start_time": start,
            "master_display": md, "max_cll": cll}


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


def build_annexb_file_command(ffmpeg: str, src: str, out_hevc: str) -> list:
    """Source container -> Annex-B HEVC ES FILE, stream-copied — the original bits, untouched.
    The inject path's base layer (vs build_annexb_command's stdout pipe for RPU extraction)."""
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", src,
            "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", out_hevc]


def build_inject_command(dovi_tool: str, src_es: str, rpu: str, out_es: str) -> list:
    """Interleave an extracted RPU into an existing HEVC ES — dovi_tool's standard inject
    subcommand. The ONLY re-attachment path that keeps the base layer's original bits (the
    x265 path re-encodes; this one must not)."""
    return [dovi_tool, "inject-rpu", "-i", src_es, "--rpu-in", rpu, "-o", out_es]


def build_rpu_extract_command(dovi_tool: str, rpu_out: str) -> list:
    """Reads Annex-B HEVC from stdin, writes the binary RPU."""
    return [dovi_tool, "extract-rpu", "-", "-o", rpu_out]


def build_decode_command(ffmpeg: str, src: str) -> list:
    """DV render -> 10-bit y4m on stdout (x265's input; y4m carries fps/size/format)."""
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", src,
            "-map", "0:v:0", "-pix_fmt", "yuv420p10le", "-strict", "-1",
            "-f", "yuv4mpegpipe", "-"]


def build_x265_command(x265: str, rpu: str, out_hevc: str, cap_mbps: int,
                       master_display: str = None, max_cll: str = None,
                       crf: float = X265_CRF, preset: str = X265_PRESET) -> list:
    """x265 native-DV encode: CRF quality + a HARD VBV ceiling; x265 interleaves the RPU NALs.
    repeat-headers/aud/hrd are DV conformance requirements (x265 enforces them with a DV
    profile, listed explicitly so the intent is visible)."""
    cap_kbps = int(cap_mbps) * 1000
    cmd = [x265, "--y4m", "-", "--output", out_hevc,
           "--preset", preset, "--crf", str(crf), "--output-depth", "10",
           "--vbv-maxrate", str(cap_kbps), "--vbv-bufsize", str(cap_kbps),
           "--dolby-vision-profile", "8.1", "--dolby-vision-rpu", rpu,
           "--repeat-headers", "--aud", "--hrd",
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

def extract_rpu(dv_video: str, rpu_out: str, *, ffmpeg=FFMPEG, dovi_tool=DOVI_TOOL,
                timeout=None):
    """Resolve render -> binary RPU file. Returns (ok, reason). ATOMIC: writes a temp then
    os.replace, so a hard kill mid-extract can never leave a TRUNCATED rpu.bin at the real
    path (which the segmented resume reuses verbatim → wrong DV slices)."""
    tmp = rpu_out + ".part"
    _rm(tmp)
    p1 = subprocess.Popen(build_annexb_command(ffmpeg, dv_video),
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        p2 = subprocess.run(build_rpu_extract_command(dovi_tool, tmp),
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


def encode_capped(dv_video: str, rpu: str, out_hevc: str, cap_mbps: int, *,
                  master_display=None, max_cll=None, total_frames=0,
                  abort=None, on_progress=None,
                  ffmpeg=FFMPEG, x265=X265):
    """Decode | x265-DV pipe with the VBV ceiling. Polls `abort` (kills both procs) and
    reports frames via on_progress(frames, total). Returns (ok, frames_encoded, reason)."""
    dec = subprocess.Popen(build_decode_command(ffmpeg, dv_video),
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    enc = subprocess.Popen(build_x265_command(x265, rpu, out_hevc, cap_mbps,
                                              master_display, max_cll),
                           stdin=dec.stdout, stderr=subprocess.PIPE, text=True)
    dec.stdout.close()                       # enc owns the pipe; let SIGPIPE reach the decoder
    tail = []
    try:
        for line in enc.stderr:              # x265 writes progress to stderr (\r-terminated)
            for chunk in line.replace("\r", "\n").splitlines():
                tail.append(chunk)
                if len(tail) > 20:
                    tail.pop(0)
                n = parse_x265_progress(chunk)
                if n is not None and on_progress:
                    on_progress(n, total_frames)
            if abort is not None and abort.is_set():
                dec.kill(); enc.kill()
                return False, 0, "aborted"
        enc.wait(); dec.wait()
    except Exception as e:
        dec.kill(); enc.kill()
        return False, 0, f"encode pipe error: {e}"
    if enc.returncode != 0 or not os.path.exists(out_hevc) or os.path.getsize(out_hevc) == 0:
        return False, 0, "x265 failed (rc=%s): %s" % (enc.returncode, " / ".join(tail[-4:]))
    frames = parse_x265_encoded("\n".join(tail)) or 0
    return True, frames, "encoded %d frames" % frames


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
                    boundaries: list | None = None) -> dict:
    """Identity of a segmented-encode run. If ANY of this changes between attempts, the
    persisted segments/RPU describe a DIFFERENT encode and must not be resumed: a re-rendered
    dv_video would pass the frame-count check yet concat WRONG CONTENT into the master. `segb`
    is included so switching the plan (topaz-aligned vs ~seg_seconds) also wipes stale segments."""
    try:
        st = os.stat(dv_video)
        src_id = f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        src_id = "missing"
    m = {"src": src_id, "cap": int(cap_mbps), "frames": int(total_frames),
         "fps": str(fps), "seg": int(seg_seconds), "crf": float(crf), "preset": preset}
    if boundaries:                          # only when a topaz-aligned plan is in force — so a fallback
        m["segb"] = [int(b) for b in boundaries]   # (~seg_seconds) remux still matches a pre-segb manifest
    return m                                # and resumes instead of wiping on the schema change


def ensure_segdir(segdir: str, manifest: dict) -> str:
    """Create/validate the resume dir against `manifest`. A mismatch (or unreadable manifest)
    WIPES the dir — stale segments must never survive a source/params change. Returns
    'resume' when the previous attempt's state is safe to reuse, else 'fresh'."""
    mf = os.path.join(segdir, "manifest.json")
    if os.path.isdir(segdir):
        try:
            with open(mf) as f:
                if json.load(f) == manifest:
                    return "resume"
        except (OSError, ValueError):
            pass
        shutil.rmtree(segdir, ignore_errors=True)
    os.makedirs(segdir, exist_ok=True)
    with open(mf, "w") as f:
        json.dump(manifest, f)
    return "fresh"


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


def build_seg_decode_command(ffmpeg: str, src: str, a: int, n: int, fps_str: str) -> list:
    """DV render frames [a, a+n) -> 10-bit y4m on stdout. Input `-ss` (fast keyframe seek +
    decode-to-exact-frame) at the midpoint timestamp; `-frames:v n` bounds the count."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
    ss = seg_seek_seconds(a, fps_str)
    if ss is not None:
        cmd += ["-ss", f"{ss:.6f}"]
    cmd += ["-i", src, "-map", "0:v:0", "-frames:v", str(n),
            "-pix_fmt", "yuv420p10le", "-strict", "-1", "-f", "yuv4mpegpipe", "-"]
    return cmd


_RPU_FRAMES_RE = re.compile(r"Frames:\s*(\d+)")


def rpu_frame_count(rpu_path: str, *, dovi_tool=DOVI_TOOL) -> int:
    """Real coded-frame count from the RPU summary — GROUND TRUTH (one RPU per coded picture).
    A VideoToolbox .mov's nb_frames HEADER can over-report vs the decodable/RPU tail (documented
    for these renders: topaz.py / resolve_pipeline.py). Planning + slicing against the header
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
        return int((r.stdout or "0").strip() or 0)
    except ValueError:
        return 0


def _encode_pipe(dec_cmd, enc_cmd, out_hevc, abort, on_frame):
    """Decode|x265 pipe for ONE segment. Returns (frames|None, reason, tail)."""
    dec = subprocess.Popen(dec_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    enc = subprocess.Popen(enc_cmd, stdin=dec.stdout, stderr=subprocess.PIPE, text=True)
    dec.stdout.close()
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
                            abort=None, on_progress=None, on_plan=None,
                            ffmpeg=FFMPEG, x265=X265, ffprobe=FFPROBE, dovi_tool=DOVI_TOOL):
    """Resumable peak-cap encode: per-segment native-DV x265 (own RPU slice) → concat.
    Segments persist in `segdir`; a completed segment is skipped on the next attempt.
    Returns (ok, frames_encoded, reason). Reports cumulative frames via on_progress, and fires
    on_plan([segment_end_frame, ...], total_frames) ONCE after planning so the dashboard can draw
    the same notched segment bar Topaz uses (seg_done is derived from cumulative frames ≥ each end)."""
    if total_frames <= 0:
        return False, 0, "unknown total frame count"
    os.makedirs(segdir, exist_ok=True)
    segs = plan_segments(total_frames, fps, seg_seconds, boundaries=boundaries)
    if on_plan:
        on_plan([b for (_a, b) in segs], total_frames)
    base, seg_files = 0, []
    for i, (a, b) in enumerate(segs):
        n = b - a
        sf = os.path.join(segdir, f"seg_{i:04d}.hevc")
        seg_files.append(sf)
        if abort is not None and abort.is_set():
            return False, base, "aborted"
        if count_hevc_frames(sf, ffprobe) == n:          # already encoded (resume) → skip
            base += n
            if on_progress:
                on_progress(base, total_frames)
            continue
        _rm(sf)                                          # partial/stale → redo clean
        stmp = sf + ".part"                              # ATOMIC: encode to temp, publish sf only on
        _rm(stmp)                                        # a clean got==n — a hard kill never leaves a
        rslice = os.path.join(segdir, f"seg_{i:04d}.rpu")   # truncated seg_*.hevc that resume would bless
        ok, why = slice_rpu(rpu, a, b, total_frames, rslice, dovi_tool=dovi_tool)
        if not ok:
            return False, base, why
        dec = build_seg_decode_command(ffmpeg, dv_video, a, n, fps)
        enc = build_x265_command(x265, rslice, stmp, cap_mbps, master_display, max_cll)
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
    ok, why = concat_segments(seg_files, out_hevc)
    if not ok:
        return False, base, why
    return True, base, "encoded %d frames in %d segments" % (base, len(segs))


def reencode_segments_tighter(dv_video: str, rpu: str, segdir: str, indices: list,
                              tight_cap_mbps: int, *, total_frames: int, fps: str,
                              master_display=None, max_cll=None, seg_seconds: int = SEG_SECONDS,
                              boundaries: list | None = None, abort=None,
                              ffmpeg=FFMPEG, x265=X265, dovi_tool=DOVI_TOOL):
    """PEAK REPAIR: delete and re-encode ONLY `indices` segments at a TIGHTER cap. The full-cap
    encode can measure over the gate — VBV bufsize == maxrate legally allows a 1-second burst
    up to ~2× cap while the gate is cap × PEAK_TOLERANCE — and re-running the identical encode
    reuses the identical segments, so it can never pass (user-caught: a movie parked at
    58.6 > 50 five times, shipped nothing). The resume manifest is deliberately unchanged
    (same source, same GLOBAL cap): a tighter segment is strictly safer to resume, and a
    mid-repair kill just resumes with the already-tightened segments. Returns (ok, reason)."""
    segs = plan_segments(total_frames, fps, seg_seconds, boundaries=boundaries)
    for i in indices:
        if abort is not None and abort.is_set():
            return False, "aborted"
        if not (0 <= i < len(segs)):
            return False, f"repair segment index {i} out of range (plan has {len(segs)})"
        a, b = segs[i]
        n = b - a
        sf = os.path.join(segdir, f"seg_{i:04d}.hevc")
        stmp = sf + ".part"
        _rm(sf); _rm(stmp)                               # the over-peak segment is what we're replacing
        rslice = os.path.join(segdir, f"seg_{i:04d}.rpu")
        ok, why = slice_rpu(rpu, a, b, total_frames, rslice, dovi_tool=dovi_tool)
        if not ok:
            return False, why
        dec = build_seg_decode_command(ffmpeg, dv_video, a, n, fps)
        enc = build_x265_command(x265, rslice, stmp, tight_cap_mbps, master_display, max_cll)
        got, why, _t = _encode_pipe(dec, enc, stmp, abort, None)
        _rm(rslice)
        if why == "aborted":
            _rm(stmp)
            return False, "aborted"
        if got is None:
            _rm(stmp)
            return False, f"repair segment {i}: {why}"
        if got != n:                                     # same drift guard as the main encode
            _rm(stmp)
            return False, f"repair segment {i} frame mismatch: {got} != {n}"
        os.replace(stmp, sf)                             # atomic publish, like the main encode
    return True, "re-capped %d segment(s) @ %d Mbps" % (len(indices), tight_cap_mbps)
