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


def build_cfr_command(ffmpeg, src, dst, *, rate, pix, color=None, low_prio=False):
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
    return [
        *prio,
        ffmpeg, "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats",
        "-i", src,
        "-map", "0:v:0", "-map", "0:a?",
        # `veryfast`: at a fixed crf the preset trades encode-speed for file SIZE only, not quality —
        # Topaz ingests identical pixels and this CFR file is deleted at cleanup, so a bigger temp is
        # irrelevant. ~3-5x faster than `medium`, so the prefetcher fills its buffer sooner.
        "-c:v", "libx264", "-crf", str(CFR_CRF), "-preset", "veryfast", "-pix_fmt", pix,
        *threads,
        *rate_flags, "-fps_mode", "cfr",
        *color_flags(color),
        "-c:a", "copy",
        dst,
    ]


def is_cfr_ready(path) -> bool:
    """A previously-made CFR file is reusable on resume only if it's present and decodes
    to a positive frame count. An interrupted convert leaves an mp4 with no moov atom
    (it's written last) → unreadable → treated as absent → re-encoded."""
    return os.path.exists(path) and _frame_count(path) > 0


def to_cfr(source, dst, *, abort=None, on_progress=None, low_prio=False) -> CfrResult:
    """Re-encode `source` to a CONSTANT frame rate at its own rate, into `dst`. Runs via
    _run_ffmpeg so it's registered for kill-on-stop/shutdown and dies within ~0.5 s of an
    abort. A failed/aborted/partial output is removed (never left to be reused as 'ready')."""
    rate = _fps_fraction(source)
    cmd = build_cfr_command(FFMPEG_HB, source, dst, rate=rate,
                            pix=_cfr_pix_fmt(source), color=source_color(source),
                            low_prio=low_prio)
    rc, frames, aborted, tail = _run_ffmpeg(cmd, os.environ.copy(),
                                            abort=abort, on_progress=on_progress)
    # A negative rc = the process was killed by a signal — that's ALWAYS our own stop/shutdown
    # (terminate_all), never a content failure. Treat it as aborted so a run stopped mid-CFR
    # isn't logged as "CFR convert failed" (with x264's close-stats masking the real cause).
    aborted = aborted or (rc is not None and rc < 0)
    ok = (rc == 0 and not aborted and is_cfr_ready(dst))
    if not ok and os.path.exists(dst):
        try: os.remove(dst)
        except OSError: pass
    return CfrResult(ok=ok, frames=frames, rate=(rate or "source"),
                     error_tail=("aborted" if aborted else tail))


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


def _frame_count(path, ffprobe=FFPROBE_HB) -> int:
    """Frames in a file — fast header read, falling back to a full count if unknown."""
    for args in (["-show_entries", "stream=nb_frames"],
                 ["-count_frames", "-show_entries", "stream=nb_read_frames"]):
        try:
            out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0",
                                  *args, "-of", "csv=p=0", path],
                                 capture_output=True, text=True, timeout=120).stdout.strip()
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


def _concat_segments(seg_files, output, *, ffmpeg=FFMPEG_HB) -> tuple:
    """Losslessly join the ProRes segments into ONE file (`-c copy` — all-intra, so the
    joins are bit-exact, no re-encode, no seam). Returns (ok, reason)."""
    listfile = output + ".concat.txt"
    try:
        with open(listfile, "w") as f:
            for s in seg_files:
                f.write("file '%s'\n" % os.path.abspath(s).replace("'", "'\\''"))
        r = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-y", "-f", "concat",
                            "-safe", "0", "-i", listfile, "-c", "copy", output],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return False, "concat failed: " + "\n".join((r.stderr or "").splitlines()[-4:])
        return True, "concatenated %d segments" % len(seg_files)
    except Exception as e:
        return False, f"concat error: {e}"
    finally:
        try: os.remove(listfile)
        except OSError: pass


def upscale_resumable(source, *, segdir, profile=None, scale=2, device=-2, fit_height=None,
                      preserve_color=True, on_progress=None, abort=None,
                      target_seconds=SEGMENT_TARGET_SECONDS, deadline=None, on_plan=None,
                      ffmpeg=FFMPEG, models_dir=MODELS) -> UpscaleResult:
    """Topaz upscale that RESUMES where it left off. The source is split at strong scene
    cuts into ~SEGMENT_TARGET_SECONDS chunks, each encoded to its own ProRes file in
    `segdir`; finished chunks survive a kill, so a resume re-encodes only the interrupted
    chunk (from its scene-cut start — seamless) and skips the rest. We do NOT concatenate:
    the chunks stay as separate files and a manifest records their order — the Resolve
    stage assembles them on its timeline (frame-accurate), so we never write the ~238 GB
    twice. `on_progress(total_frames_done)` reports cumulative progress across chunks.
    `on_plan(seg_end_frames, exact_total)` fires once after planning (progress-bar notches).
    `deadline` (time.monotonic) stops CLEANLY at the next segment boundary — completed
    chunks stay, nothing partial is lost — returning msg='turn-budget…' (a movie's 90-min
    turn, NOT a failure)."""
    os.makedirs(segdir, exist_ok=True)
    fps, _dur = media_timing(source)
    total = _frame_count(source)          # EXACT frame count (nb_frames). A duration×fps
    if total <= 0:                        # estimate makes the LAST chunk's -frames:v overshoot
        total = total_frames(source)      # EOF → fewer frames than asked → validation fails.
    if not (fps and total):
        return UpscaleResult(False, -1, 0, segdir, "could not read source fps/frame-count")
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
        # Movie TURN BUDGET: stop cleanly at a segment boundary once the deadline passes —
        # every completed chunk stays on disk, the next turn resumes exactly here.
        if deadline is not None and time.monotonic() >= deadline:
            return UpscaleResult(False, 0, done, segdir,
                                 "turn-budget: paused at a segment boundary — resumes next movie turn")
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
            return UpscaleResult(False, rc, done + frames, segdir,
                                 (tail or "")[-200:] or f"segment {i}: got {c} frames, wanted {n}")
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
