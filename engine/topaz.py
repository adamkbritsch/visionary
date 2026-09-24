"""Topaz stage: headless 1080p -> 4K upscale via Topaz Video AI's bundled ffmpeg.

Matches the user's real Topaz preset exactly:
"1080p to 4K SDR XQ - DIGITAL (Resolve HDR+DoVi)"
  - enhance: model prob-4 (Proteus), recoverOriginalDetail 45, compress 8,
    dehalo 5, detail 2, denoise 0, sharpen 0; HDR off (Hyperion happens later
    in Resolve).
  - encoder: prores-422-xq-osx  ->  exact ffmpegOpts from Topaz's
    video-encoders.json (ProRes 4444 XQ, p416le). PCM audio, .mov.

Runs against a LOCAL file on scratch — never off the NAS.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass

from versions import TOPAZ_APP as APP   # exact-version pin (versions.py) — preflight gates on it
FFMPEG = f"{APP}/Contents/MacOS/ffmpeg"
FFPROBE = f"{APP}/Contents/MacOS/ffprobe"
MODELS = f"{APP}/Contents/Resources/models"
FFMPEG_HB = "/opt/homebrew/bin/ffmpeg"    # plain ffmpeg for scene detection + lossless concat
FFPROBE_HB = "/opt/homebrew/bin/ffprobe"
# Resumable-encode tuning. Proteus is recurrent (lr_prev/hr_prev) but RESETS at scene
# cuts, so a chunk that cold-starts on a strong scene cut is frame-identical to a
# continuous encode (validated: SSIM ≥0.9994 across the seam). We checkpoint there.
SEGMENT_TARGET_SECONDS = 90               # group scene-cut chunks to ~this length
SCENE_STRONG_SCORE = 0.4                  # only checkpoint at strong cuts (clean reset)
CFR_CRF = 14                              # near-lossless x264 for the VFR→CFR source pass
# Above this height the software x264 pass stops being viable. A 4K VFR source (YouTube ships
# 2160p AV1) at crf 14 is tens of thousands of frames of near-lossless 4K x264 — on the
# prefetcher's background QoS that is HOURS, and because an interrupted convert is unusable
# (no moov atom → is_cfr_ready False) every restart begins again from zero. Live-caught
# 2026-08-18: an 18-minute 4K AV1 video failed its CFR five times in a day and produced 250
# bytes. Hardware HEVC is ~20-50x faster and this file is a temporary intermediate that
# cleanup deletes, so the extra bitrate costs nothing.
CFR_HW_MIN_HEIGHT = 1440
CFR_HW_KBPS = 80000

# ProRes 422 HQ, 10-bit 4:2:2 (p210le) — the upscale intermediate. Was ProRes XQ 16-bit (p416le), but
# the DV master out of Resolve is 10-bit, so XQ's 12/16-bit precision is unused on an 8-bit-sourced
# upscale; HQ 10-bit still exceeds the source's real information (banding-safe in the grade) at ~1/3 the
# size (~76 vs ~229 GB/episode) → less scratch, faster Resolve read + cleanup, room to prefetch the queue.
XQ_ENCODER = ["-c:v", "prores_videotoolbox", "-profile:v", "hq",
              "-color_range", "tv", "-pix_fmt", "p210le", "-allow_sw", "1"]


def build_filter(model="prob-4", scale=2, device=-2,
                 compression=0.08, details=0.02, halo=0.05, blend=0.45, fit_height=None) -> str:
    """tvai_up filter matching the SDR XQ - DIGITAL preset's Proteus settings. When
    `fit_height` is set, a final lanczos `scale` is chained so the output lands EXACTLY on
    that height (4K) — tvai's AI scale is {1,2,4} only, so 720p (4×→2880) fits down and 480p
    (4×→1920) fits up; aspect is preserved (-2 width). 1080p ×2 hits 2160, so it omits it."""
    vf = (f"tvai_up=model={model}:scale={scale}:device={device}"
          f":compression={compression}:details={details}:halo={halo}:blend={blend}")
    if fit_height:
        vf += f",scale=-2:{int(fit_height)}:flags=lanczos"
    return vf


def build_filter_from_profile(profile: dict, scale=2, device=-2, fit_height=None) -> str:
    """Build the tvai_up filter from a per-show preset dict (settings.py) at the plan's
    `scale` (the AI factor) + optional `fit_height` (exact 4K fit). Missing keys fall back
    to the DIGITAL defaults, so a partial preset is always valid."""
    p = profile or {}
    return build_filter(
        model=str(p.get("model", "prob-4")), scale=int(scale), device=device,
        compression=float(p.get("compression", 0.08)), details=float(p.get("details", 0.02)),
        halo=float(p.get("halo", 0.05)), blend=float(p.get("blend", 0.45)), fit_height=fit_height)


def build_env(models_dir: str) -> dict:
    return {"TVAI_MODEL_DIR": models_dir, "TVAI_MODEL_DATA_DIR": models_dir}


def source_color(path: str, ffprobe: str = FFPROBE) -> dict:
    """The source's color tags. Topaz NEVER tone-maps (no Hyperion), so to keep the
    range identical (SDR→SDR, HDR→HDR) we just carry the source's primaries/transfer/
    space onto the ProRes output."""
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-of", "json",
             "-show_entries", "stream=color_primaries,color_transfer,color_space", path],
            capture_output=True, text=True, timeout=30).stdout
        v = (json.loads(out).get("streams") or [{}])[0]
    except Exception:
        return {}
    return {"primaries": v.get("color_primaries"), "transfer": v.get("color_transfer"),
            "space": v.get("color_space")}


def color_flags(color: dict) -> list:
    """ffmpeg flags tagging the output with the source color. Unspecified values are
    skipped (ffmpeg then keeps the input's), so an SDR source stays SDR and a
    bt2020/smpte2084 (HDR) source stays HDR."""
    c, out = color or {}, []
    for flag, key in (("-color_primaries", "primaries"), ("-color_trc", "transfer"),
                      ("-colorspace", "space")):
        v = c.get(key)
        if v and v not in ("unknown", "unspecified", "reserved"):
            out += [flag, v]
    return out


# PRIORITY (user-dictated, 2026-07-07 — the remux governor + topaz nice were REMOVED): topaz and
# the finisher's x265 remux both run at NORMAL priority and freely contend. topaz is the pipeline
# BOTTLENECK (~2.5 h vs remux ~1.75 h), so throttling it to protect the remux (the old governor +
# nice-10) slowed the bottleneck and cost ~half the overlap's throughput. Segmented remux made the
# remux resumable, so it no longer needs protecting. Only the background prefetch CFR stays niced.


def build_command(ffmpeg: str, input_path: str, output_path: str, vf: str, color: dict = None) -> list:
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-y",
        "-progress", "pipe:1", "-nostats",
        "-i", input_path,
        "-vf", vf,
        *XQ_ENCODER,
        *color_flags(color),
        "-c:a", "pcm_s24le",
        output_path,
    ]


def summarize(probe_json: str) -> dict:
    data = json.loads(probe_json)
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            return {"codec": s.get("codec_name"), "profile": s.get("profile"),
                    "width": s.get("width"), "height": s.get("height")}
    return {}


def is_valid_upscale(summary: dict) -> bool:
    """A real, 4K-class ProRes stream — the encoder's output. Deliberately NOT pinned to
    exactly 3840x2160 or the 'XQ' profile string: this is a GENERAL upscaler (any source
    aspect ratio, SDR or HDR, even already-4K intake), and ffprobe reports the ProRes
    4444 XQ profile as bare 'XQ'. The old exact-match read a perfectly good encode as
    'invalid', so upscale() returned ok=False and the Topaz stage re-ran forever. The
    caller already gates on ffmpeg rc==0 + not-aborted; here we just confirm the output
    is genuine ProRes frames at 4K-class width (>=2000 px, i.e. it actually upscaled)."""
    try:
        w = int(summary.get("width") or 0)
        h = int(summary.get("height") or 0)
    except (TypeError, ValueError):
        return False
    return str(summary.get("codec", "")).startswith("prores") and w >= 2000 and h > 0


@dataclass
class UpscaleResult:
    ok: bool
    returncode: int
    frames: int
    output: str
    error_tail: str = ""
    summary: dict = None


def probe(path: str, ffprobe: str = FFPROBE) -> dict:
    r = subprocess.run(
        [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", path],
        capture_output=True, text=True,
    )
    return summarize(r.stdout) if r.returncode == 0 else {}


def media_timing(path: str, ffprobe: str = FFPROBE) -> tuple:
    """(fps, duration_seconds) for a clip — fast, no decode. (0.0, 0.0) if unknown.
    Used to turn a frame count into 'minutes of the episode processed' + an ETA."""
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-of", "json",
             "-show_entries", "stream=r_frame_rate:format=duration", path],
            capture_output=True, text=True, timeout=30).stdout
        d = json.loads(out)
        dur = float(d.get("format", {}).get("duration") or 0)
        rate = (d.get("streams") or [{}])[0].get("r_frame_rate", "0/1")
        num, den = rate.split("/")
        fps = float(num) / float(den) if float(den) else 0.0
        return (fps, dur)
    except Exception:
        return (0.0, 0.0)


# ProRes footprint per minute of FINISHED (4K) content — MEASURED 2026-08-17 from a live
# segdir: 61.5 GiB over 21,412 frames of Lost S04E09 = 4.13 GiB/content-min, which
# reproduces the long-documented ~190 GiB for a 42-min episode. The rate is per OUTPUT
# minute, so it holds for every Topaz path (480p x4, 1080p x2 and the already-4K clean
# pass all write 4K ProRes) and depends only on RUNTIME.
PRORES_GIB_PER_MIN = 4.2


def projected_prores_gib(seconds: float) -> int:
    """How much ProRes the Topaz stage will write for `seconds` of content. The whole point
    is that this scales with RUNTIME: 42-min episode ~173 GiB, 2-hour film ~500 GiB, a
    4 h 19 m cut ~1.1 TiB. Anything that gates disk on a flat floor is episode-sized
    thinking (see orchestrator._projected_item_gb)."""
    return int(max(0.0, float(seconds or 0.0)) / 60.0 * PRORES_GIB_PER_MIN)


def total_frames(path: str, ffprobe: str = FFPROBE) -> int:
    """Total video frames = duration × fps (fast, no decode). The Topaz output has
    the same frame count as the source, so this is the denominator for live %.
    Returns 0 if unknown — callers then show a frame count without a percentage."""
    fps, dur = media_timing(path, ffprobe)
    return int(round(dur * fps)) if dur and fps else 0


# ---- constant-frame-rate source pass -------------------------------------
# A variable frame rate is what made the frame counts drift downstream: ffprobe's header
# count, the actual decoded count, and Resolve's clip length disagreed by a few frames,
# which broke the Topaz last-chunk count and the Resolve timeline-length guard. We convert
# the verified download to CFR at its OWN rate (no cadence change) so every count is exact
# end to end, and Topaz/Resolve/remux all read THAT file.

@dataclass
class CfrResult:
    ok: bool
    frames: int
    rate: str
    error_tail: str
    capped_secs: float = 0.0     # >0 when the source's container outran its own picture
                                 # and the pass was bounded (see cfr_duration_cap)


def _fps_fraction(path, ffprobe=FFPROBE_HB):
    """The source's frame-rate fraction string (e.g. '24000/1001') for an EXACT -r,
    avoiding the precision loss of a float (23.976023976…). None if unreadable — the
    convert then omits -r and lets -fps_mode cfr fall back to the input rate itself."""
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        return None
    if re.fullmatch(r"\d+/\d+", out) and not out.startswith("0/"):
        return out
    return out if (out.isdigit() and out != "0") else None


def _cfr_pix_fmt(path, ffprobe=FFPROBE_HB):
    """Match the source's bit depth — a 10-bit (HDR) source stays 10-bit, SDR stays
    8-bit — normalized to 4:2:0 (delivery + x264-safe; the upscaler works in 4:2:0)."""
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=pix_fmt", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        out = ""
    return "yuv420p10le" if "10" in out else "yuv420p"


def _cfr_height(path, ffprobe=FFPROBE_HB) -> int:
    """The source's height, to decide software vs hardware for the CFR pass. 0 when unknown —
    which keeps the long-standing software path, never the newer one, on a bad probe."""
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=height", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return int(out.splitlines()[0]) if out else 0
    except Exception:
        return 0


# How far the container may outrun the VIDEO before we cap the CFR's length, and the slack
# left above the video when we do. Don't Look Up's source carries ~5 minutes of audio past
# the end of its picture; Resolve took its TIMELINE length from the container, so it rendered
# 206,200 frames for a 199,144-frame movie — five minutes of nothing on the end, which then
# could not align to the RPU and parked the movie at the remux (live-caught 2026-08-21).
# The slack is deliberately a whole second: `-t` must never shave a real trailing frame, and
# a second of overshoot is nowhere near enough to matter to anything downstream.
CFR_TAIL_SLOP_SECS = 2.0
CFR_TAIL_KEEP_SECS = 1.0


def video_duration(path, ffprobe=FFPROBE_HB, *, rate=None):
    """Length of the VIDEO stream in seconds — frames / rate where both are known (exact),
    else the stream's own duration tag. None when neither is readable. `rate` overrides the
    declared one when a caller has proven it wrong (declared_rate_mismatch): frames over a
    declared rate that is too HIGH comes out short of the real picture."""
    from fractions import Fraction
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=nb_frames,duration,r_frame_rate", "-of", "json", path],
            capture_output=True, text=True, timeout=60).stdout
        st = (json.loads(out).get("streams") or [{}])[0]
    except Exception:
        return None
    try:
        n, r = int(st.get("nb_frames") or 0), Fraction(rate or st.get("r_frame_rate") or "0/0")
        if n > 0 and r > 0:
            return n / float(r)
    except Exception:
        pass
    try:
        d = float(st.get("duration"))
        if d > 0:
            return d
    except (TypeError, ValueError):
        pass
    return _last_video_pts(path, ffprobe)


def _last_video_pts(path, ffprobe=FFPROBE_HB):
    """Where the picture actually ENDS, for containers that publish neither a frame count
    nor a stream duration — Matroska routinely publishes neither. Seeks to just before the
    container's end and reads what remains: seeking PAST the end of a short video stream
    lands on its last keyframe, so the tail is read either way, and it costs one seek
    (~0.3 s on a 19 GB file) rather than a full decode."""
    box = _container_duration(path, ffprobe)
    if not box:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-read_intervals", f"{max(0.0, box - 60):.3f}%",
             "-show_entries", "packet=pts_time,duration_time", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=180).stdout
    except Exception:
        return None
    last, step = None, 0.0
    for ln in out.splitlines():
        parts = ln.split(",")
        try:
            last = float(parts[0])
        except (ValueError, IndexError):
            continue
        try:
            step = float(parts[1])
        except (ValueError, IndexError):
            pass
    # the last frame is still ON SCREEN for its own duration, so the picture ends after it
    return (last + step) if last is not None else None


def _container_duration(path, ffprobe=FFPROBE_HB):
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of",
             "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=60).stdout
        d = float((out.splitlines() or ["0"])[0].strip())
        return d if d > 0 else None
    except Exception:
        return None


def cfr_duration_cap(source, ffprobe=FFPROBE_HB, *, rate=None):
    """The `-t` to give the CFR pass so its container cannot outrun its picture, or None
    when the source is already honest (the overwhelmingly common case). Only ever LONGER
    than the video, so it can never truncate one — given the rate the frames really run at,
    which is why to_cfr passes the one it encodes at."""
    vid = video_duration(source, ffprobe, rate=rate)
    box = _container_duration(source, ffprobe)
    if not vid or not box or box <= vid + CFR_TAIL_SLOP_SECS:
        return None
    return vid + CFR_TAIL_KEEP_SECS


def build_cfr_command(ffmpeg, src, dst, *, rate, pix, color=None, low_prio=False,
                     height=0, duration_cap=None):
    """ffmpeg args for a VFR→CFR re-encode at `rate` (the source's OWN rate — same
    cadence, just constant). `-r <rate>` + `-fps_mode cfr` is the canonical recipe;
    near-lossless crf keeps the upscaler's input detail; bit depth + color tags are
    preserved (HDR stays HDR). Video + audio ONLY — subtitles are deliberately NOT
    carried: they don't need frame-rate re-timing, copying them through `-fps_mode cfr`
    is what risked a mux abort on some sources, and they can't live in an MP4 CFR anyway
    (bitmap PGS). The remux re-attaches subs from the ORIGINAL download instead.
    `-progress pipe:1 -nostats` makes _run_ffmpeg's frame= parser report progress."""
    rate_flags = ["-r", rate] if rate else []
    # low_prio (the PREFETCHER's CFRs): QoS-clamp to BACKGROUND (E-cores only on Apple
    # Silicon) + capped threads, so a background x264 can never starve the in-flight Topaz
    # encode of CPU. `nice` alone was NOT enough — a niced x264 still ran at ~730% CPU on
    # P-cores and measurably slowed the live encode ~24% (6.7 → ~5.1 fps).
    prio = ["/usr/sbin/taskpolicy", "-c", "background"] if low_prio else []
    threads = ["-threads", "4"] if low_prio else []
    # 4K → hardware HEVC on the media engine (see CFR_HW_MIN_HEIGHT). Same timing contract:
    # the -r/-fps_mode cfr flags and the colour tags below are shared by both paths, so what
    # Resolve imports is identical apart from the codec. `-allow_sw 1` keeps it working if
    # the media engine is unavailable rather than failing the stage outright.
    # Hardware DECODE matters as much as the encode: the source is 4K AV1, and software
    # decoding that under the background QoS clamp is what actually stalls it (measured on a
    # saturated machine: this pass held 37% CPU while the live x265 held 1060%). With both
    # halves on the MEDIA ENGINE — which neither x265 (CPU) nor Topaz (GPU) contends for —
    # it stops competing with the live encode instead of merely being throttled behind it.
    # ffmpeg falls back to software decode by itself if the hwaccel cannot initialise.
    accel = ["-hwaccel", "videotoolbox"] if (height or 0) >= CFR_HW_MIN_HEIGHT else []
    hw = (["-c:v", "hevc_videotoolbox", "-b:v", f"{CFR_HW_KBPS}k",
           "-pix_fmt", ("p010le" if "10" in (pix or "") else "nv12"),
           "-tag:v", "hvc1", "-allow_sw", "1"]
          if (height or 0) >= CFR_HW_MIN_HEIGHT else [])
    return [
        *prio,
        ffmpeg, "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats",
        *accel,
        "-i", src,
        "-map", "0:v:0", "-map", "0:a?",
        # `veryfast`: at a fixed crf the preset trades encode-speed for file SIZE only, not quality —
        # Topaz ingests identical pixels and this CFR file is deleted at cleanup, so a bigger temp is
        # irrelevant. ~3-5x faster than `medium`, so the prefetcher fills its buffer sooner.
        *(hw if hw else ["-c:v", "libx264", "-crf", str(CFR_CRF), "-preset", "veryfast",
                         "-pix_fmt", pix, *threads]),
        *rate_flags, "-fps_mode", "cfr",
        *color_flags(color),
        "-c:a", "copy",
        *(["-t", f"{duration_cap:.3f}"] if duration_cap else []),
        dst,
    ]


def is_cfr_ready(path) -> bool:
    """A previously-made CFR file is reusable on resume only if it's present and decodes
    to a positive frame count. An interrupted convert leaves an mp4 with no moov atom
    (it's written last) → unreadable → treated as absent → re-encoded."""
    return os.path.exists(path) and _frame_count(path) > 0


def _frac(s):
    """Parse a ffprobe 'num/den' (or bare int) rational to (num, den); None if unparseable."""
    if not s or s in ("0/0", "N/A"):
        return None
    try:
        if "/" in s:
            n, d = s.split("/", 1)
            n, d = int(n), int(d)
        else:
            n, d = int(s), 1
    except (ValueError, TypeError):
        return None
    if n <= 0 or d <= 0:
        return None
    return (n, d)


def _period_exact_in_timebase(r_frame_rate: str, time_base: str) -> bool:
    """Can one frame's duration be represented EXACTLY as a whole number of container
    time_base ticks? A frame lasts 1/fps = fps_den/fps_num seconds; in ticks of
    time_base tb_num/tb_den that is (fps_den * tb_den) / (fps_num * tb_num). If that
    isn't an integer, the muxer must round each frame's timestamp — over an episode the
    rounding wobbles the cadence (avg_frame_rate drifts off r_frame_rate) even though the
    source is nominally CFR. A stream-COPY carries that jitter into the MP4, and the
    upscaler then duplicates frames to fill it → the render grows by ~1 frame per minute
    and the audio steadily leads the picture. This is exactly the matroska case: its
    1/1000 (millisecond) timebase can't represent a 1001/24000 s (41.708 ms) NTSC frame,
    while an MP4 written at 1/24000 can. Return False on any doubt → force the re-encode,
    which regenerates uniform PTS (what MP4 sources already get, and never drift)."""
    fps = _frac(r_frame_rate)
    tb = _frac(time_base)
    if not fps or not tb:
        return False
    fps_num, fps_den = fps
    tb_num, tb_den = tb
    return (fps_den * tb_den) % (fps_num * tb_num) == 0


# ---- the frames' own rate vs the rate the container declares ------------------------------
# A container's DECLARED frame rate can be wrong while agreeing with itself. Matroska hands
# ffprobe both r_frame_rate and avg_frame_rate from the track's DefaultDuration, and some
# release muxers store that rounded to whole milliseconds. The Adventures of Sharkboy and
# Lavagirl (EDGE2020) declares 42 ms = 500/21 fps, yet its 133,624 frames carry 24000/1001
# timestamps (deltas alternate 42/41 ms). avg == r, 10-bit 4:2:0, and 42 ms sits exactly on
# the 1 ms timebase, so the CFR pass stream-COPIED it. Its PGS subtitles made that CFR a
# Matroska file, which re-declares the same 42 ms (an MP4 copy re-derives the rate from the
# timestamps and would have been right), and the lie reached Topaz intact. There every
# segment seeked on the wrong clock while the MOV muxer's CFR output dropped 1 frame in 144
# to hold 500/21. The middle segments still reached their -frames:v count, so nothing noticed
# until the LAST segment, which reads to EOF: 11,899 of 12,826 frames after nine hours of
# upscaling, failing identically on every ~44-minute retry (live 2026-09-06). The frames are
# the truth: an exact frame count over the span their own timestamps cover.
STANDARD_FRAME_RATES = ("24000/1001", "24/1", "25/1", "30000/1001", "30/1", "48000/1001",
                        "48/1", "50/1", "60000/1001", "60/1", "100/1", "120000/1001", "120/1",
                        "12000/1001", "12/1", "15000/1001", "15/1")
RATE_SNAP_TOLERANCE = 2e-4    # how close a measured cadence must sit to a standard rate. NTSC
                              # and whole rates are 1e-3 apart, so a snap never confuses them
RATE_MIN_SPAN_SECS = 10.0     # shorter, a millisecond of timestamp rounding outweighs the check
RATE_LIE_FRAMES = 2           # the declared rate must mispredict the frame count by this much
RATE_MISMATCH = "frame-rate mismatch:"   # upscale_resumable's refusal; stages._topaz rebuilds on it


def _hms_seconds(s):
    """'01:32:53.234000000' (a Matroska DURATION statistics tag) → 5573.234; None if unparseable."""
    try:
        h, m, sec = str(s).strip().split(":")
        total = int(h) * 3600 + int(m) * 60 + float(sec)
    except (ValueError, TypeError):
        return None
    return total if total > 0 else None


def _video_span(path, ffprobe=FFPROBE_HB):
    """Seconds of picture the video stream's OWN timestamps cover, first frame on screen to
    last frame off. Never derived from the declared rate, because that is what it is checked
    against. MP4/MOV publish it as the track duration, and mkvmerge/ffmpeg Matroska as a
    DURATION statistics tag. Anything else is read off the last packets with one seek.
    None when unknown."""
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=duration,start_time:stream_tags",
                              "-of", "json", path],
                             capture_output=True, text=True, timeout=60).stdout
        st = (json.loads(out).get("streams") or [{}])[0]
    except Exception:
        return None
    try:
        d = float(st.get("duration"))
        if d > 0:
            return d
    except (TypeError, ValueError):
        pass
    for key, val in (st.get("tags") or {}).items():
        if key.upper() == "DURATION" or key.upper().startswith("DURATION-"):
            secs = _hms_seconds(val)
            if secs:
                return secs
    end = _last_video_pts(path, ffprobe)
    try:
        start = max(0.0, float(st.get("start_time")))
    except (TypeError, ValueError):
        start = 0.0
    return (end - start) if end and end > start else None


def declared_rate_mismatch(path, ffprobe=FFPROBE_HB, *, frames=None):
    """`(declared, actual)` frame-rate strings when `path`'s frames prove its declared rate
    wrong, else None. Wrong means two things at once: the frames run at a DIFFERENT standard
    rate, and the declared rate mispredicts the frame count by RATE_LIE_FRAMES or more. So
    timestamp rounding, a short clip, or an odd spelling of the same rate ('2997/125') never
    qualifies. CONSERVATIVE the other way from _is_already_cfr: any doubt (unreadable count or
    span, a cadence matching no standard rate) → None, which keeps the behaviour this check
    predates. `frames` passes in an EXACT count the caller already holds: on a Matroska file
    without a count tag, counting means a packet scan of the whole file."""
    from fractions import Fraction
    declared_s = _fps_fraction(path, ffprobe)
    try:
        declared = Fraction(declared_s or "")
    except (ValueError, ZeroDivisionError):
        return None
    if declared <= 0:
        return None
    if not frames or frames <= 0:
        frames = _frame_count(path, ffprobe, decode=False)
    span = _video_span(path, ffprobe)
    if frames <= 0 or not span or span < RATE_MIN_SPAN_SECS:
        return None
    raw = frames / span
    actual = min((Fraction(r) for r in STANDARD_FRAME_RATES), key=lambda r: abs(raw / r - 1))
    if abs(raw / actual - 1) > RATE_SNAP_TOLERANCE:
        return None                                   # no standard rate fits → no verdict
    if abs(declared / actual - 1) <= RATE_SNAP_TOLERANCE:
        return None                                   # the same rate, however it is spelled
    if abs(float(declared) * span - frames) < RATE_LIE_FRAMES:
        return None                                   # too short for the difference to cost a frame
    return (declared_s, f"{actual.numerator}/{actual.denominator}")


# ---- what DaVinci Resolve can actually host ------------------------------------------------
# Resolve puts a timeline on a fixed set of rates. Ask for one it does not know and the setting
# is IGNORED — `proj.SetSetting("timelineFrameRate", "23")` leaves the project at 23.976 — so
# resolve_pipeline's conform guard refuses to render, correctly, because a conform would drop or
# duplicate frames behind our back. The CFR is the file Resolve imports, so the constraint
# belongs here: a source at a rate Resolve cannot host is RE-TIMED on the way in. Live: Rhett &
# Link's "Christmas Face" is a genuine 23.000 fps upload, and from 2026-09-14 to 2026-09-24 it
# failed Resolve five times a day, every day, with nothing to show for it.
RESOLVE_TIMELINE_RATES = ("16/1", "18/1", "24000/1001", "24/1", "25/1", "30000/1001", "30/1",
                          "48000/1001", "48/1", "50/1", "60000/1001", "60/1", "72/1",
                          "96000/1001", "96/1", "100/1", "120000/1001", "120/1")
HOSTABLE_RATE_TOLERANCE = 1e-4   # 2997/125 IS Resolve's 23.976 — do not re-encode over a rounding


def resolve_hostable_rate(rate):
    """The rate to encode at so Resolve can host the result: `rate` itself when Resolve knows it,
    otherwise the next one UP.

    Up, never down: duplicating frames to reach a hostable rate keeps every picture in the source
    and its running time, while rounding down would throw pictures away to shrink a file that is a
    temporary intermediate anyway. Beyond the top of the list it clamps. An unreadable rate is
    returned untouched — the caller's existing behaviour, which is to let ffmpeg decide."""
    from fractions import Fraction
    if not rate:
        return rate
    try:
        want = Fraction(rate)
    except (ValueError, ZeroDivisionError):
        return rate
    if want <= 0:
        return rate
    known = sorted(Fraction(r) for r in RESOLVE_TIMELINE_RATES)
    for r in known:
        if abs(float(want) / float(r) - 1) <= HOSTABLE_RATE_TOLERANCE:
            return rate                      # already one of Resolve's, however it is spelled
    for r in known:
        if r > want:
            return "%d/%d" % (r.numerator, r.denominator)
    top = known[-1]
    return "%d/%d" % (top.numerator, top.denominator)


def timestamp_holes(path, ffprobe=FFPROBE_HB) -> int:
    """How many frame slots `path`'s own video timestamps SKIP — the number of frames it is short
    of the running time it advertises. 0 for a sound CFR, -1 when it cannot be read.

    A constant-rate file is supposed to have one picture per slot. When it does not, every reader
    that measures by DURATION (Resolve, sizing a timeline) disagrees with every reader that counts
    FRAMES (our own gates), and the item can never satisfy both. Live 2026-09-24: hevc_videotoolbox
    dropped 4 frames 39 minutes into a 2-hour interview and left the gap in the timestamps, so
    Resolve built a 171,235-frame timeline for a 171,230-frame file and refused it on every
    attempt, for two days. The same stretch re-encoded clean — a hiccup under load, not the file.
    Reads the packet INDEX only, no decode: measured at ~0.2 s/GB on that 71 GB CFR."""
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=time_base,r_frame_rate", "-of", "json", path],
                             capture_output=True, text=True, timeout=60).stdout
        st = (json.loads(out).get("streams") or [{}])[0]
        fps, tb = _frac(st.get("r_frame_rate")), _frac(st.get("time_base"))
        if not fps or not tb:
            return -1
        period = (fps[1] * tb[1]) / float(fps[0] * tb[0])     # one frame, in timebase ticks
        if period <= 0:
            return -1
        out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "packet=pts", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=1800).stdout
    except Exception:
        return -1
    pts = sorted(int(v) for v in out.replace(",", " ").split() if v.lstrip("-").isdigit())
    if len(pts) < 2:
        return 0                                  # nothing to be discontinuous with
    slots = int(round((pts[-1] - pts[0]) / period)) + 1
    return max(0, slots - len(pts))


def _is_already_cfr(path, ffprobe=FFPROBE_HB, *, check_rate=True) -> bool:
    """The source is ALREADY constant frame rate at a 4:2:0 pixel format AND its container
    timebase can represent that rate EXACTLY → the CFR step can stream-COPY the video instead
    of a full re-encode (identical pixels, ~seconds not minutes).
    `avg_frame_rate == r_frame_rate` (both valid) is the CFR signal — for VFR they differ; 4:2:0
    because the re-encode also normalizes chroma to what the upscaler ingests, so 4:2:2/4:4:4
    still transcode. The timebase check (see _period_exact_in_timebase) is the matroska guard:
    an MKV is nominally CFR (avg==r) but its 1 ms timebase can't hold an NTSC frame period, so a
    stream-copy inherits jitter the upscaler turns into a growing audio/video drift — such a
    source must re-encode to get uniform PTS (the path MP4 sources already take).
    Last, the declared rate must be the one the frames actually run at (declared_rate_mismatch):
    avg and r both come from the same Matroska header, so they agree even when it is wrong, and
    a copy would hand that wrong clock to Topaz. `check_rate=False` is for a caller that has
    just run that check itself (to_cfr), so a tagless file is not packet-scanned twice.
    CONSERVATIVE: any doubt (unreadable, VFR, wide chroma, lossy timebase) → False → safe re-encode."""
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=avg_frame_rate,r_frame_rate,pix_fmt,time_base",
                              "-of", "json", path], capture_output=True, text=True, timeout=30).stdout
        s = (json.loads(out).get("streams") or [{}])[0]
    except Exception:
        return False
    avg, r, pix = s.get("avg_frame_rate"), s.get("r_frame_rate"), s.get("pix_fmt")
    if not avg or not r or avg in ("0/0", "N/A") or r in ("0/0", "N/A"):
        return False
    return (avg == r and pix in ("yuv420p", "yuv420p10le")
            and _period_exact_in_timebase(r, s.get("time_base"))
            and (not check_rate or declared_rate_mismatch(path, ffprobe) is None))


def build_cfr_copy_command(ffmpeg, src, dst, *, low_prio=False, duration_cap=None, rate=None):
    """Fast path for an already-CFR 4:2:0 source (see _is_already_cfr): stream-COPY the video
    (+audio) into the CFR file — no re-encode. Subtitles stay out (PGS can't ride the CFR and
    aren't needed; the remux re-attaches them from the original). `-progress` still lets
    _run_ffmpeg surface progress + a frame count. `rate` corrects a DECLARED rate the frames do
    not run at (declared_rate_mismatch): on a stream copy `-r` rewrites only what the container
    declares (Matroska's DefaultDuration), never a packet or a timestamp."""
    prio = ["/usr/sbin/taskpolicy", "-c", "background"] if low_prio else []
    return [
        *prio,
        ffmpeg, "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats",
        "-i", src,
        "-map", "0:v:0", "-map", "0:a?",
        "-c", "copy",
        *(["-r", rate] if rate else []),
        *(["-t", f"{duration_cap:.3f}"] if duration_cap else []),
        dst,
    ]


def build_mezzanine_segment_command(ffmpeg, src, dst, *, start_frame, n, rate,
                                    color=None, kbps=0):
    """ONE segment of the Resolve-compat mezzanine (frames [start_frame, start_frame+n)):
    same encode as build_mezzanine_command, but seekable + bounded so the build is
    RESUMABLE (user-dictated 2026-08-06 — a killed pass keeps its finished chunks).
    Input `-ss` = dvcap's proven seek (keyframe seek + decode-to-exact-frame); the
    caller gates each chunk on an EXACT frame count, so a seek miss fails loudly
    instead of shipping drift. Inputs are CFR by construction (the fast path requires
    it; YouTube passes its true-CFR file), which is what makes frame->seconds exact."""
    import dvcap
    ss = dvcap.seg_seek_seconds(start_frame, rate)
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats"]
    if ss is not None:
        cmd += ["-ss", f"{ss:.6f}"]
    rate_flags = ["-r", rate] if rate else []
    return [*cmd, "-i", src, "-map", "0:v:0", "-an",
            "-frames:v", str(n),
            "-c:v", "hevc_videotoolbox", "-b:v", f"{int(kbps)}k",
            "-pix_fmt", "p010le", "-tag:v", "hvc1", "-allow_sw", "1",
            *rate_flags, "-fps_mode", "cfr",
            *color_flags(color),
            dst]


def to_cfr(source, dst, *, abort=None, on_progress=None, low_prio=False,
           copy_only=False) -> CfrResult:
    """Give `source` a CONSTANT frame rate in `dst`. If the source is ALREADY CFR (the common
    case for modern rips), stream-COPY the video — a full re-encode of an already-CFR file is
    pure waste (~minutes for a movie). Otherwise re-encode at the source's own rate. Runs via
    _run_ffmpeg so it's registered for kill-on-stop/shutdown and dies within ~0.5 s of an
    abort. A failed/aborted/partial output is removed (never left to be reused as 'ready').

    `copy_only` (FAST-PATH items, rpu-only/resolve-only): ALWAYS stream-copy, even when the
    timebase-exactness test would force a re-encode. The true-CFR re-encode exists to keep
    TOPAZ's frame counts stable — but the fast paths skip Topaz, ship the ORIGINAL video (or
    Resolve's render of it), and read only this file's AUDIO (a bit-copy of the original's
    either way) plus decoded frames for scene-cut planning. Hours of libx264 on a 4K movie
    whose video bytes nothing reads (live-caught 2026-08-06, a 60 GB REMUX).

    A container that DECLARES a rate its frames do not run at (declared_rate_mismatch) never
    passes that rate on. A re-encode is timed at the frames' own rate, since `-r 500/21` over
    24000/1001 frames drops 1 in 144 to hold the wrong clock. A copy keeps its packets and
    declares the right rate over them, because Resolve imports this file for every item."""
    lie = declared_rate_mismatch(source)
    true_rate = lie[1] if lie else _fps_fraction(source)
    # Resolve can only host the rates it knows (resolve_hostable_rate), and re-timing is not
    # something a stream copy can do — so a source at any other rate is re-encoded, fast path or not.
    rate = resolve_hostable_rate(true_rate)
    retime = bool(rate and true_rate and rate != true_rate)
    cap = cfr_duration_cap(source, rate=true_rate)   # see CFR_TAIL_SLOP_SECS — a container longer
                                                     # than its own picture becomes Resolve's timeline
    copied = (copy_only or (not lie and _is_already_cfr(source, check_rate=False))) and not retime
    if copied:
        cmd = build_cfr_copy_command(FFMPEG_HB, source, dst, low_prio=low_prio,
                                     duration_cap=cap, rate=(rate if lie else None))
    else:
        cmd = build_cfr_command(FFMPEG_HB, source, dst, rate=rate,
                                pix=_cfr_pix_fmt(source), color=source_color(source),
                                low_prio=low_prio, height=_cfr_height(source),
                                duration_cap=cap)
    rc, frames, aborted, tail = _run_ffmpeg(cmd, os.environ.copy(),
                                            abort=abort, on_progress=on_progress)
    # A negative rc = the process was killed by a signal — that's ALWAYS our own stop/shutdown
    # (terminate_all), never a content failure. Treat it as aborted so a run stopped mid-CFR
    # isn't logged as "CFR convert failed" (with x264's close-stats masking the real cause).
    aborted = aborted or (rc is not None and rc < 0)
    ok = (rc == 0 and not aborted and is_cfr_ready(dst))
    # A re-encode that SKIPPED slots is not a constant-rate file, whatever its frame count says
    # (timestamp_holes): Resolve sizes a timeline by duration and refuses it against our own
    # frame count, for as long as the item is in the queue. The drop is a hiccup under load, so
    # throwing the file away is enough — the next attempt redoes it. Only the RE-ENCODE is judged
    # this way: a copy's gaps come from the source, copying again would reproduce them, and it is
    # `-fps_mode cfr` that fills a gap in the first place.
    holes = timestamp_holes(dst) if (ok and not copied) else 0
    if holes > 0:
        ok = False
        tail = ("the CFR came out %d frame(s) short of the running time it advertises — "
                "the encoder dropped them" % holes)
    if not ok and os.path.exists(dst):
        try: os.remove(dst)
        except OSError: pass
    if ok and not frames:            # a stream copy may not emit frame= progress → re-probe
        frames = _frame_count(dst)
    return CfrResult(ok=ok, frames=frames, rate=(rate or "source"),
                     error_tail=("aborted" if aborted else tail),
                     capped_secs=float(cap or 0.0))


# In-flight Topaz ffmpeg subprocesses, so a run-stop or app shutdown can kill them —
# an encode must NEVER be left orphaned (reparented to launchd) burning GPU after the
# run ends (which is exactly what happened: a stopped run left ffmpeg running).
_ACTIVE = set()
_ACTIVE_LOCK = threading.Lock()


def terminate_all():
    """Kill every in-flight Topaz ffmpeg. Called from orchestrator.disable() and the
    server's shutdown hook so stopping the run — or quitting the app — never leaves an
    encode running."""
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE)
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass


def _run_ffmpeg(cmd, env, *, abort=None, on_progress=None, timeout=None):
    """Run an ffmpeg command, streaming frame= progress. The proc is registered in
    _ACTIVE (killable on shutdown), and a watcher thread kills it within ~0.5 s once
    `abort` fires — INDEPENDENT of stdout, so a buffered or stalled pipe can't delay
    the stop (relying on the stdout loop alone is what let a stopped encode run on).
    Returns (returncode, frames, aborted, stderr_tail)."""
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    with _ACTIVE_LOCK:
        _ACTIVE.add(proc)
    stop = threading.Event()
    err_lines = []

    def _drain_err():
        # Drain stderr CONCURRENTLY. stderr is a pipe; if we only read stdout and let
        # stderr fill its ~64 KB buffer, ffmpeg blocks writing stderr while we block
        # reading stdout — a classic deadlock that would hang a long encode forever.
        # Keep the last ~200 lines for the failure tail.
        try:
            for ln in proc.stderr:
                err_lines.append(ln)
                if len(err_lines) > 200:
                    del err_lines[:-200]
        except Exception:
            pass
    err_t = threading.Thread(target=_drain_err, daemon=True)
    err_t.start()

    def _watch():
        while not stop.wait(0.5):
            if abort is not None and abort.is_set():
                try:
                    proc.kill()
                except Exception:
                    pass
                return
    threading.Thread(target=_watch, daemon=True).start()

    frames, aborted = 0, False
    try:
        for line in proc.stdout:
            if abort is not None and abort.is_set():
                aborted = True
                break
            m = re.match(r"frame=(\d+)", line.strip())
            if m:
                frames = int(m.group(1))
                if on_progress:
                    on_progress(frames)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            return (-1, frames, False, "timeout")
    finally:
        stop.set()
        if abort is not None and abort.is_set():
            try:
                proc.kill()
            except Exception:
                pass
            aborted = True
        with _ACTIVE_LOCK:
            _ACTIVE.discard(proc)
    err_t.join(timeout=2)
    stderr_tail = "\n".join("".join(err_lines).splitlines()[-12:])
    return (proc.returncode, frames, aborted, stderr_tail)


def upscale(input_path: str, output_path: str, *, ffmpeg=FFMPEG, models_dir=MODELS,
            profile=None, scale=2, model="prob-4", blend=0.45, device=-2, fit_height=None,
            preserve_color=True, on_progress=None, timeout=None, abort=None) -> UpscaleResult:
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    # A per-show preset `profile` (settings.py) drives the Proteus params; `scale` comes from
    # the input plan (the AI factor), `fit_height` lands the output exactly on 4K when needed.
    vf = (build_filter_from_profile(profile, scale, device, fit_height) if profile
          else build_filter(model=model, scale=scale, blend=blend, device=device, fit_height=fit_height))
    # Preserve the source range: carry its color tags so SDR stays SDR, HDR stays HDR.
    color = source_color(input_path, FFPROBE) if preserve_color else None
    cmd = build_command(ffmpeg, input_path, output_path, vf, color)
    env = {**os.environ, **build_env(models_dir)}

    rc, frames, aborted, err_tail = _run_ffmpeg(cmd, env, abort=abort,
                                                on_progress=on_progress, timeout=timeout)
    if aborted:
        return UpscaleResult(False, -1, frames, output_path, "aborted (run stopped)")
    if rc != 0:
        return UpscaleResult(False, rc, frames, output_path, err_tail)
    summary = probe(output_path)
    return UpscaleResult(is_valid_upscale(summary), rc, frames, output_path, err_tail, summary)


# ---- resumable, seamless encode (scene-cut checkpointing) -----------------

def plan_segments(total_frames: int, fps: float, cut_frames, target_seconds=SEGMENT_TARGET_SECONDS):
    """PURE (unit-tested). Group strong scene-cut frame boundaries into segments of
    ~target_seconds, each STARTING on a scene cut (where Proteus resets, so a cold-started
    resume is seamless). Returns [(start_frame, end_frame)] covering [0, total_frames)."""
    if total_frames <= 0:
        return []
    target = max(1, int(round(target_seconds * (fps or 24))))
    bounds = [0]
    for c in sorted({int(f) for f in (cut_frames or [])}):
        if 0 < c < total_frames and c - bounds[-1] >= target:
            bounds.append(c)
    if bounds[-1] < total_frames:
        bounds.append(total_frames)
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def detect_scene_cuts(source, *, ffmpeg=FFMPEG_HB, strong=SCENE_STRONG_SCORE) -> list:
    """Times (seconds) of STRONG scene cuts in the source. Decode-only pass with plain
    ffmpeg (no Topaz). [] on any failure — the caller then encodes one big segment."""
    try:
        out = subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-i", source,
             "-vf", f"select='gt(scene,{strong})',metadata=print:file=-", "-an", "-f", "null", "-"],
            capture_output=True, text=True).stdout
    except Exception:
        return []
    times = []
    for line in out.splitlines():
        i = line.find("pts_time:")
        if i != -1:
            try:
                times.append(float(line[i + 9:].split()[0]))
            except (ValueError, IndexError):
                pass
    return sorted(times)


def _cached_scene_frames(source, segdir, fps) -> list:
    """Strong scene-cut FRAME numbers, cached in segdir/scenes.json so a resume doesn't
    re-scan the source."""
    cache = os.path.join(segdir, "scenes.json")
    try:
        with open(cache) as f:
            times = json.load(f)
    except (OSError, json.JSONDecodeError):
        times = detect_scene_cuts(source)
        try:
            os.makedirs(segdir, exist_ok=True)
            with open(cache, "w") as f:
                json.dump(times, f)
        except OSError:
            pass
    return [int(round(t * fps)) for t in times]


def _frame_count(path, ffprobe=FFPROBE_HB, *, decode=True) -> int:
    """Frames in a file — fast header/tag reads first; a full-decode count only as a LAST
    resort. `nb_frames` is present in MP4 but N/A in MKV, where the EXACT count instead lives
    in a NUMBER_OF_FRAMES stream tag (written by any muxer/copy) — read that before decoding.
    Critical for an already-CFR stream-COPY that keeps HEVC: a full HEVC `-count_frames` decode
    of a feature can take minutes and time out → the good CFR would be judged 'not ready' and
    re-copied forever. All three yield the SAME exact count, so segment planning is unaffected."""
    # NUMBER_OF_FRAMES is an mkvmerge STATISTICS tag — plenty of releases lack it (DTOne
    # remuxes, bare WEBRips) and ffmpeg's own muxer NEVER writes it, so a stream-copied
    # CFR of such a source has no header count at all. -count_packets is the exact
    # no-decode fallback (packets == frames for these streams; it reads the index only —
    # measured 0.7 s/GB, ~30 s for a 54 GB feature). Without it the good copy fell to the
    # full-decode count, timed out, was judged 'not ready', deleted, re-copied and
    # re-failed until the movie PARKED (live 2026-08-16: Deadpool 2 ×5, Hangover III ×5).
    for args, tmo in ((["-show_entries", "stream=nb_frames"], 120),
                      (["-show_entries", "stream_tags=NUMBER_OF_FRAMES"], 120),
                      (["-count_packets", "-show_entries", "stream=nb_read_packets"], 300),
                      *([(["-count_frames", "-show_entries", "stream=nb_read_frames"], 600)]
                        if decode else [])):   # decode=False: a CHECK must never cost a full decode
        try:
            out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0",
                                  *args, "-of", "csv=p=0", path],
                                 capture_output=True, text=True, timeout=tmo).stdout.strip()
            if out.isdigit():
                return int(out)
        except Exception:
            pass
    return -1


def _seg_valid(path, expected) -> bool:
    """A segment is reusable on resume only if it exists and has EXACTLY its frames
    (a partial/interrupted segment is re-encoded)."""
    return os.path.exists(path) and _frame_count(path) == expected


MANIFEST = "segments.json"


def write_manifest(segdir, fps, entries) -> None:
    """Record the ordered chunks (each with its ACTUAL frame count) so the Resolve stage
    assembles them on its timeline instead of us concatenating a second ~238 GB file.
    total_frames = the real assembled length (sum of actual counts) for Resolve's guard."""
    data = {"fps": fps, "total_frames": sum(e["frames"] for e in entries), "segments": entries}
    with open(os.path.join(segdir, MANIFEST), "w") as f:
        json.dump(data, f, indent=1)


def read_manifest(segdir):
    try:
        with open(os.path.join(segdir, MANIFEST)) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def segments_complete(segdir) -> bool:
    """True iff the manifest exists and EVERY listed segment is present with its exact
    frame count — the resumable Topaz stage's done-marker (replaces the concat output)."""
    m = read_manifest(segdir)
    if not m or not m.get("segments"):
        return False
    for s in m["segments"]:
        if not _seg_valid(os.path.join(segdir, s["file"]), s["frames"]):
            return False
    return True

def upscale_resumable(source, *, segdir, profile=None, scale=2, device=-2, fit_height=None,
                      preserve_color=True, on_progress=None, abort=None,
                      target_seconds=SEGMENT_TARGET_SECONDS, on_plan=None, should_pause=None,
                      ffmpeg=FFMPEG, models_dir=MODELS) -> UpscaleResult:
    """Topaz upscale that RESUMES where it left off. The source is split at strong scene
    cuts into ~SEGMENT_TARGET_SECONDS chunks, each encoded to its own ProRes file in
    `segdir`; finished chunks survive a kill, so a resume re-encodes only the interrupted
    chunk (from its scene-cut start — seamless) and skips the rest. We do NOT concatenate:
    the chunks stay as separate files and a manifest records their order — the Resolve
    stage assembles them on its timeline (frame-accurate), so we never write the ~238 GB
    twice. `on_progress(total_frames_done)` reports cumulative progress across chunks.
    `on_plan(seg_end_frames, exact_total)` fires once after planning (progress-bar notches).
    `should_pause()` (optional callable) is polled BETWEEN segments: when it returns True the
    encode stops CLEANLY at that boundary — completed chunks stay, nothing partial is lost —
    returning msg='paused: …' (a benign hold, NOT a failure; the orchestrator re-selects the
    item and resumes here once the pause condition clears)."""
    os.makedirs(segdir, exist_ok=True)
    fps, _dur = media_timing(source)
    total = exact = _frame_count(source)  # EXACT frame count (nb_frames). A duration×fps
    if total <= 0:                        # estimate makes the LAST chunk's -frames:v overshoot
        total = total_frames(source)      # EOF → fewer frames than asked → validation fails.
    if not (fps and total):
        return UpscaleResult(False, -1, 0, segdir, "could not read source fps/frame-count")
    # Every seek below is frame / fps, and the .mov output holds the declared rate. If the
    # frames run at another rate, each middle segment silently drops frames to hold it and the
    # LAST one comes up short on every attempt (see declared_rate_mismatch). Refuse here, before
    # hours of GPU, so the caller can rebuild the input on the right clock.
    lie = declared_rate_mismatch(source, frames=exact)   # never the estimate: it assumes the rate
    if lie:
        return UpscaleResult(False, -1, 0, segdir,
                             f"{RATE_MISMATCH} the input declares {lie[0]} fps but its frames "
                             f"run at {lie[1]}")
    segs = plan_segments(total, fps, _cached_scene_frames(source, segdir, fps), target_seconds)
    if on_plan:
        try:
            on_plan([b for (_a, b) in segs], total)
        except Exception:
            pass
    vf = (build_filter_from_profile(profile, scale, device, fit_height) if profile
          else build_filter(scale=scale, device=device, fit_height=fit_height))
    color = source_color(source, FFPROBE) if preserve_color else None
    env = {**os.environ, **build_env(models_dir)}
    done, entries = 0, []
    nseg = len(segs)
    for i, (a, b) in enumerate(segs):
        sf = os.path.join(segdir, f"seg_{i:04d}.mov")
        n = b - a
        is_last = (i == nseg - 1)                   # last chunk runs to EOF (see below)
        existing = _frame_count(sf) if os.path.exists(sf) else 0
        # already encoded? a middle chunk must match its planned length EXACTLY; the last
        # chunk is bounded by EOF, and the source's true decodable tail can be a few frames
        # short of nb_frames, so accept it as long as it reached ~the planned end.
        if existing > 0 and (existing == n or (is_last and n - 8 <= existing <= n + 1)):
            entries.append({"file": os.path.basename(sf), "start": a, "frames": existing})
            done += existing
            if on_progress:
                on_progress(done)
            continue
        # Segment-boundary PAUSE (e.g. two remuxes have the machine — user-dictated): stop
        # cleanly here; every completed chunk stays on disk and the resume re-enters exactly here.
        if should_pause is not None and should_pause():
            # The REASON lives with the caller (dual remux / a gate-released fast item / a
            # "run this now" YouTube request) — naming one of them here mislabelled the others.
            return UpscaleResult(False, 0, done, segdir,
                                 "paused: yielded at a segment boundary — resumes from here")
        # Accurate input seek to the scene-cut start `a` (cold start is seamless there).
        # Middle chunks encode exactly n frames; the LAST chunk omits -frames:v and reads
        # to the source's end — so a source whose last frame won't decode can't fail it.
        bound = [] if is_last else ["-frames:v", str(n)]
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats",
               "-ss", f"{a / fps:.6f}", "-i", source, *bound,
               "-vf", vf, *XQ_ENCODER, *color_flags(color), "-an", sf]
        base = done
        prog = (lambda f: on_progress(base + f)) if on_progress else None
        rc, frames, aborted, tail = _run_ffmpeg(cmd, env, abort=abort, on_progress=prog)
        if aborted:
            try: os.remove(sf)                      # drop the partial chunk; resume re-does it
            except OSError: pass
            return UpscaleResult(False, -1, done + frames, segdir, "aborted (run stopped)")
        c = _frame_count(sf)
        good = rc == 0 and c > 0 and (c == n if not is_last else c >= n - 8)
        if not good:
            try: os.remove(sf)
            except OSError: pass
            # ffmpeg's tail explains a CRASH. After a clean exit it holds only the closing stats
            # ("muxing overhead … frame=11899 … drop=82"), and logging that hid a wrong frame
            # count through five 44-minute failures of the same segment.
            got = f"{c} frames" if c >= 0 else "an unreadable file"
            why = (f"segment {i + 1} of {nseg} came out {got}, expected {n}" if rc == 0
                   else (tail or "")[-200:] or f"segment {i + 1} of {nseg}: ffmpeg exited {rc}")
            return UpscaleResult(False, rc, done + frames, segdir, why)
        entries.append({"file": os.path.basename(sf), "start": a, "frames": c})
        done += c
    # All chunks present — record the order + ACTUAL frame counts for Resolve (no concat).
    write_manifest(segdir, fps, entries)
    summary = probe(os.path.join(segdir, entries[0]["file"])) if entries else {}
    return UpscaleResult(is_valid_upscale(summary), 0, done, segdir, "", summary)


def main(argv=None):
    import argparse, sys
    ap = argparse.ArgumentParser(description="Topaz 1080p->4K ProRes XQ upscale (headless).")
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--blend", type=float, default=0.45)
    ap.add_argument("--device", type=int, default=-2)
    args = ap.parse_args(argv)

    print(f"Upscaling {args.input} -> {args.output}")
    res = upscale(args.input, args.output, blend=args.blend, device=args.device,
                  on_progress=lambda f: print(f"\r  frame {f}", end="", flush=True))
    print()
    if res.ok:
        print(f"OK: {res.summary} ({res.frames} frames)")
        return 0
    print(f"FAILED (rc={res.returncode}): {res.error_tail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
