"""Heavy stage runners for the orchestrator. Each returns (ok, message).

download / topaz / remux / upload / cleanup run fully unattended. The resolve
stage runs setup + render via the scripting API; its two UI-only steps (DV Analyze
All Shots @ 1000-nit, and the DV Profile 8.1 dropdown) go through dv_shim, which
needs this process to have macOS Screen-Recording + Accessibility grants and
`cliclick`. Until that's set up, the resolve stage returns not-ok and the episode
stays parked at this stage (resumable) — it's the remaining piece for FULL
automation; everything around it is done.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import logbook
import transfer
from orchestrator import _is_dv81, _vstream, _has_audio, _first_video, _remote_size

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
# Whole resolve stage budget (setup + DV "Analyze All Shots" + render). MUST exceed the
# DV analysis cap (dv_shim.wait_for_analysis max_seconds = 3600 = 60 min) PLUS render +
# Resolve launch, or a long episode gets killed mid-analysis and fail-loops. Was 45 min
# (< the 60-min analysis cap alone) — the exact recipe for a stuck resolve stage.
RESOLVE_TIMEOUT = 120 * 60
FFPROBE = "/opt/homebrew/bin/ffprobe"
EXPORT_BITRATE_FLOOR_KBPS = 60000   # render preset's default; the export matches the intake above this

# TOPAZ→REMUX segment BOUNDARIES: the remux re-encodes at the SAME scene-cut segment boundaries this
# episode's topaz encode used — the two stages' segments line up (not evenly spaced). Topaz's segdir
# (which knows the boundaries) is dropped at hand-off, so the cumulative segment-END frames are stashed
# here (keyed by source basename) the moment topaz plans, and read back when the remux runs — durable
# across the hand-off AND a relaunch. Absent → the remux falls back to its ~5-min (SEG_SECONDS) plan.
_SEGBOUNDS_FILE = os.path.expanduser("~/.topaz-pipeline/topaz_segbounds.json")
_SEGBOUNDS_LOCK = threading.Lock()


def _read_topaz_bounds(basename: str) -> list:
    try:
        with _SEGBOUNDS_LOCK:
            with open(_SEGBOUNDS_FILE) as f:
                v = (json.load(f) or {}).get(basename)
        return [int(x) for x in v] if isinstance(v, list) else []
    except (OSError, ValueError, TypeError):
        return []


def _write_topaz_bounds(basename: str, bounds: list) -> None:
    if not basename or not bounds:
        return
    try:
        with _SEGBOUNDS_LOCK:
            try:
                with open(_SEGBOUNDS_FILE) as f:
                    d = json.load(f)
                if not isinstance(d, dict):
                    d = {}
            except (OSError, ValueError):
                d = {}
            b = [int(x) for x in bounds]
            if d.get(basename) == b:
                return                              # unchanged → no rewrite (topaz plans are stable)
            d[basename] = b
            os.makedirs(os.path.dirname(_SEGBOUNDS_FILE), exist_ok=True)
            tmp = _SEGBOUNDS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.replace(tmp, _SEGBOUNDS_FILE)
    except OSError:
        pass


def _source_video_kbps(path):
    """The intake's video-stream bitrate in Kb/s (fallback: overall file bitrate); 0 if
    unknown. Lets a high-bitrate source export at its own bitrate instead of being
    re-compressed below it."""
    for args in (["-select_streams", "v:0", "-show_entries", "stream=bit_rate"],
                 ["-show_entries", "format=bit_rate"]):
        try:
            out = subprocess.run([FFPROBE, "-v", "error", *args, "-of", "csv=p=0", path],
                                 capture_output=True, text=True, timeout=30).stdout.strip()
            if out and out not in ("N/A", "0"):
                return int(int(out) / 1000)
        except Exception:
            pass
    return 0


def run_stage(stage, p, *, abort=None, progress=None, low_prio=False, should_pause=None):
    fn = {
        "download": _download, "topaz": _topaz, "resolve": _resolve,
        "remux": _remux, "upload": _upload, "cleanup": _cleanup,
    }.get(stage, lambda *_a, **_k: (False, f"unknown stage {stage}"))
    ep = getattr(p, "ep", "?")
    try:
        # `should_pause` = hold topaz at its next segment boundary (two remuxes have the
        # machine); only the topaz stage can honor it mid-stage.
        if stage == "topaz":
            ok, msg = fn(p, abort, progress, should_pause)
        elif stage == "download":
            ok, msg = fn(p, abort, progress, low_prio)
        else:
            ok, msg = fn(p, abort, progress)
    except Exception as e:                       # any stage bug is logged, never silent
        logbook.exception(f"{stage} {ep}", e)
        return False, f"{stage} crashed: {e.__class__.__name__}: {e}"
    # A clean segment-boundary pause is NOT a failure — log it as an event so it never
    # shows up in the red "Recent issues" banner.
    benign = (not ok) and str(msg).startswith("paused:")
    (logbook.event if (ok or benign) else logbook.failure)(f"{stage} {ep}: {msg}")
    return ok, msg


def _download(p, abort, progress=None, low_prio=False):
    """NAS source -> scratch (FTP), THEN a constant-frame-rate pass.

    Reuse an on-disk source ONLY if it's verified complete (size == the NAS file). A
    partial left by a stopped/interrupted download is deleted and re-pulled — never
    reused, since a short file surfaces later as a corrupt 'moov atom not found'. And if
    THIS download is aborted (run stopped mid-transfer) or fails, the partial is removed
    so the next pass starts clean instead of treating the stub as a finished source.

    Then convert the verified source to CONSTANT frame rate at its own rate (topaz.to_cfr)
    into p.source_cfr — a variable frame rate is what made the downstream frame counts
    drift (header vs decoder vs Resolve disagreeing), breaking the Topaz last-chunk count
    and the Resolve timeline-length guard. p.source_cfr is what Topaz, Resolve and the
    remux all read; the original stays so the size check still proves the download finished.
    stage_done('download') requires BOTH, so an interrupted CFR pass just resumes here."""
    have_source = False
    if os.path.exists(p.source):
        if _source_complete(p) is not False:   # complete, OR can't verify → keep & reuse
            have_source = True
        else:
            try: os.remove(p.source)           # verified INCOMPLETE → re-pull clean
            except OSError: pass
    if not have_source:
        on_prog = None
        if progress:
            def on_prog(done, total):          # the pull is the first ~20% of the stage bar
                progress({"stage": "download", "ep": p.ep, "pct": round(done / total * 20)})
        ok, _local, reason = transfer.download(p.nas_source, os.path.dirname(p.source),
                                               on_progress=on_prog, abort=abort)
        # Anything at p.source after a stopped/failed transfer is a partial stub — drop it so
        # it can never be mistaken for a finished source (the exact break: quitting mid-download).
        if not ok:
            if os.path.exists(p.source):
                try: os.remove(p.source)
                except OSError: pass
            return ok, reason
    return _ensure_cfr(p, abort, progress, low_prio=low_prio)


def _ensure_cfr(p, abort, progress=None, low_prio=False):
    """Make p.source_cfr (constant frame rate, same rate) from the verified p.source.
    Skipped if a valid CFR file is already present (resume-safe). Reports the re-encode as
    the back end of the download bar (20→99%) since it's the longer half of the stage."""
    import topaz
    import orchestrator
    orchestrator.apply_container(p)     # source is on disk now → lock the container (mkv vs mp4)
    if topaz.is_cfr_ready(p.source_cfr):
        return True, "source + CFR already on disk (reused)"
    on_prog = None
    if progress:
        total = topaz.total_frames(p.source) or 0
        def on_prog(frames):
            pct = 20 + round(frames / total * 79) if total else None
            progress({"stage": "download", "ep": p.ep, "pct": min(99, pct) if pct else None})
    res = topaz.to_cfr(p.source, p.source_cfr, abort=abort, on_progress=on_prog, low_prio=low_prio)
    if not res.ok:
        return False, f"CFR convert failed: {_err_tail(res.error_tail)}"
    return True, f"downloaded + CFR @ {res.rate} ({res.frames} frames)"


def _source_complete(p):
    """Three states: True (local size matches the NAS file), False (verified mismatch →
    a partial), or None (can't verify, e.g. NAS unreachable — don't destroy the file)."""
    remote = _remote_size(p.nas_source, None)
    if remote is None:
        return None
    try:
        return os.path.getsize(p.source) == remote
    except OSError:
        return False


def _topaz(p, abort, progress=None, should_pause=None):
    """source -> ProRes 4444 XQ. Uses the SHOW's chosen preset + the input plan
    (upscale 1080p 2×, clean already-4K 1×; range PRESERVED, never SDR<->HDR).
    Reports live frame progress to the dashboard (Topaz is headless — the app is
    the only UI). `abort` lets the watchdog kill it mid-encode."""
    import topaz, settings, plan
    # Plan from the ORIGINAL source: the CFR re-encode (libx264) strips Dolby Vision side
    # data, so an already-DV source must be detected here, not on the CFR file. Resolution/
    # HDR are identical in both. We then upscale the CFR file (exact, constant frame counts).
    pl = plan.plan_for(p.source)
    if pl["topaz"] == "skip":
        return False, "source is already Dolby Vision — nothing to upscale"
    if pl["topaz"] in ("rpu-only", "resolve-only"):
        # HIGH-BITRATE 4K FAST PATH: the source picture IS the deliverable — no upscale.
        # Succeed as a no-op so the run loop proceeds straight to the Resolve stage.
        return True, pl["reason"] + " — skipping upscale"
    # Per-resolution preset variant: the plan's bucket (from the source height) picks which
    # tuned param set to use; the source can be ANY of 480p/720p/1080p and still reach 4K.
    params = settings.show_topaz_params(p.series, pl.get("res") or "1080p")
    key = settings.show_preset_key(p.series)
    total = topaz.total_frames(p.source_cfr)
    # Segment plan (fires once, from upscale_resumable): cumulative end-frames + EXACT total.
    # Feeds the dashboard's notched progress bar — `notches` = each segment's end as a 0..1
    # fraction, `seg_done` = how many segments are fully encoded (drives the little flash).
    seg_plan = {"ends": None, "total": None}
    def on_plan(ends, exact_total):
        seg_plan["ends"], seg_plan["total"] = ends, exact_total
        _write_topaz_bounds(p.source_basename, ends)   # the remux re-encodes at THESE exact boundaries
    on_progress = None
    if progress:
        def on_progress(frames):
            # prefer the plan's EXACT frame count; the duration×fps estimate is the fallback.
            # Cap at 100 either way so a slight under-estimate never shows ">100%".
            t = seg_plan["total"] or total
            d = {"stage": "topaz", "ep": p.ep,
                 "pct": min(100, round(frames / t * 100)) if t else None}
            if seg_plan["ends"] and t:
                ends = seg_plan["ends"]
                done_segs = sum(1 for e in ends if frames >= e)
                d["notches"] = [round(e / t, 4) for e in ends]
                d["seg_done"] = done_segs
                d["seg_total"] = len(ends)
                if done_segs < len(ends):
                    # how much of the WHOLE stage the current segment's remainder is —
                    # the orchestrator's rate window turns this into a per-segment ETA
                    d["seg_rem_pct"] = max(0.0, (ends[done_segs] - frames) / t * 100)
            progress(d)
    # RESUMABLE + SEAMLESS, NO CONCAT: encode scene-cut-aligned chunks into the per-episode
    # segdir (+ a manifest). An interruption only loses the current chunk — a resume
    # re-encodes just that one (from its scene-cut start, frame-identical) and skips the
    # rest. We do NOT stitch the chunks into one giant file (that needed ~238 GB twice and
    # is what got stuck); the chunks STAY as separate files and the Resolve stage assembles
    # them on its timeline. The segdir is the topaz OUTPUT + the resume checkpoint — KEPT on
    # interruption, removed only at cleanup. stage_done("topaz") checks the manifest.
    res = topaz.upscale_resumable(p.source_cfr, segdir=p.segdir, profile=params, scale=pl["scale"],
                                  fit_height=pl.get("fit_height"), on_progress=on_progress, abort=abort,
                                  on_plan=on_plan, should_pause=should_pause)
    if not res.ok and str(res.error_tail).startswith("paused:"):
        return False, res.error_tail            # NOT a failure — held cleanly at a segment boundary
    return res.ok, (f"[{key} · {pl.get('res') or '?'} {pl['topaz']} {pl['scale']}× → 4K] "
                    f"{res.frames} frames → segments"
                    if res.ok else _err_tail(res.error_tail))


_ERR_LINE = re.compile(
    r"error|invalid|could ?not|cannot|conversion failed|no such|not (?:found|supported)"
    r"|unable|denied|failed|non.?monoton|unsupported", re.I)


def _err_tail(text, n=200):
    """The ACTUAL error from an ffmpeg stderr blob. ffmpeg prints libx264's verbose close-stats
    LAST, so a plain tail shows encoder noise ('ref P L0: … kb/s:…') and hides the real failure.
    Prefer the last few lines that look like an error/failure; fall back to the tail otherwise.
    Collapsed to one readable line, capped at `n` chars."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    hits = [ln for ln in lines if _ERR_LINE.search(ln)]
    picked = " ".join((hits or lines)[-3:])
    return (" ".join(picked.split()) or "(no error output)")[-n:]


def _quit_resolve_focus_app():
    """After the resolve stage: force-quit Resolve (graceful quit hangs on its
    'cancel renders?' prompt) to free its RAM/GPU and guarantee a FRESH, responsive
    Resolve next episode (stale Resolve is what caused the original hang), then bring
    the dashboard app back to the front."""
    subprocess.run(["pkill", "-9", "-f", "DaVinci Resolve.app"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["open", "-a", "Visionary"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---- FAST-PATH RESOLVE-COMPAT MEZZANINE -----------------------------------------------
# The fast path feeds the ORIGINAL source to Resolve, and the eligibility gate excludes
# nothing by codec (user-dictated) — so a 4K VP9/AV1 source (typical 4K YouTube) can land
# on a Resolve build that can't decode it. That fails at import ("IMPORT FAILED" /
# "FPS UNREADABLE" from resolve_pipeline), NOT at render — so when that exact signature
# appears, transcode a lightweight HEVC Main10 mezzanine (hardware videotoolbox — plain
# ffmpeg, ~45 GB/hr vs ProRes' ~340) and retry ONCE. HEVC Main10 is what the fast path's
# HDR10 sources already are, so Resolve ingests it by construction. Sources Resolve
# decodes never take this path.
MEZZ_MIN_KBPS = 80_000        # floor; else 4× the source bitrate — transparent for a 12-25 Mbps intake
MEZZ_TIMEOUT = 7200           # hardware encode runs >100 fps; even a movie is well inside 2 h
_MEZZ_MARKERS = ("IMPORT FAILED", "FPS UNREADABLE")   # resolve_pipeline's ingest-failure prints


def mezzanine_path(source: str) -> str:
    """The mezzanine sits next to the source in scratch (cleanup sweeps it by this name)."""
    return os.path.splitext(source)[0] + "_mezz.mp4"


def build_mezzanine_command(ffmpeg, src, dst, *, rate, color=None, kbps=MEZZ_MIN_KBPS):
    """Resolve-compat mezzanine: video-only HEVC Main10 at ≥4× the source bitrate. CFR
    flags regenerate uniform PTS (a webm/mkv ms-timebase must not reach a frame-counted
    render); color tags carry the source's range (bt709 or PQ) so the persistent
    project's color management reads the mezzanine exactly like the original. Audio
    stays out — the remux ships it from the CFR file."""
    import topaz
    rate_flags = ["-r", rate] if rate else []
    return [ffmpeg, "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats",
            "-i", src, "-map", "0:v:0", "-an",
            "-c:v", "hevc_videotoolbox", "-b:v", f"{int(kbps)}k",
            "-pix_fmt", "p010le", "-tag:v", "hvc1", "-allow_sw", "1",
            *rate_flags, "-fps_mode", "cfr",
            *topaz.color_flags(color),
            dst]


def _build_mezzanine(p, abort, progress=None):
    """Transcode p.source -> the mezzanine via topaz's registered/killable ffmpeg runner
    (the repo-standard wrapper — no Topaz processing involved). (ok, path_or_tail)."""
    import topaz
    dst = mezzanine_path(p.source)
    kbps = max(4 * _source_video_kbps(p.source), MEZZ_MIN_KBPS)
    cmd = build_mezzanine_command(topaz.FFMPEG_HB, p.source, dst,
                                  rate=topaz._fps_fraction(p.source),
                                  color=topaz.source_color(p.source), kbps=kbps)
    total = topaz.total_frames(p.source) or 0
    on_prog = None
    if progress and total:
        def on_prog(frames):
            progress({"stage": "resolve", "ep": p.ep, "pct": min(99, round(frames / total * 100))})
    rc, _frames, aborted, tail = topaz._run_ffmpeg(cmd, os.environ.copy(), abort=abort,
                                                   on_progress=on_prog, timeout=MEZZ_TIMEOUT)
    ok = rc == 0 and not aborted and os.path.exists(dst) and os.path.getsize(dst) > 0
    if not ok:
        try: os.remove(dst)
        except OSError: pass
        return False, ("aborted" if aborted else (tail or "")[-180:])
    return True, dst


def _resolve(p, abort, progress=None):
    """ProRes -> mute Dolby Vision .mov, run in a KILLABLE SUBPROCESS (setup + DV
    Analyze All + render, via resolve_pipeline.py episode). The Resolve scripting
    API (fusionscript) can hang on a socket wait HOLDING THE GIL when Resolve is
    unresponsive — in-process that wedges the entire orchestrator (the exact bug
    that froze the app mid-run). As a child process it's killable on timeout/abort,
    so a Resolve hang fails this stage cleanly and the orchestrator keeps breathing.
    ONE project handles SDR and HDR input (the ProRes color tags pick the range).
    When the stage finishes (any outcome) Resolve is quit and the app refocused.
    FAST PATH: if Resolve can't INGEST the original source (VP9/AV1 — the gate excludes
    nothing by codec), a lightweight HEVC mezzanine is built and the run retried once."""
    import plan
    pl = plan.plan_for(p.source)   # ORIGINAL: CFR re-encode strips DV side data (skip-detection)
    if pl.get("resolve") == "skip":
        return False, "source is already Dolby Vision — nothing for Resolve to do"
    mode = "hdr" if pl.get("is_hdr") else "sdr"   # HDR intake → 2000-nit HDR project
    fast = pl.get("topaz") in ("rpu-only", "resolve-only")
    # Match the ORIGINAL intake's bitrate (the real source quality), not the CFR re-encode's
    # near-lossless crf bitrate, which would inflate the export for no quality gain. In
    # rpu-only mode the render's VIDEO is discarded (only its RPU ships) — floor is plenty.
    # (A mezzanine retry keeps this same value — the inflated mezz bitrate is not quality.)
    bitrate = (EXPORT_BITRATE_FLOOR_KBPS if pl.get("topaz") == "rpu-only"
               else max(_source_video_kbps(p.source), EXPORT_BITRATE_FLOOR_KBPS))

    def _run(video_in):
        """One resolve_pipeline subprocess pass. (ok, out, reason) — `out` is the FULL
        output (the import-failure markers print mid-stream, not on the last line);
        reason is None on a hard kill/abort/launch failure (no retry on those)."""
        cmd = [sys.executable, os.path.join(ENGINE_DIR, "resolve_pipeline.py"),
               ("single" if fast else "episode"),
               (video_in if fast else p.segdir), p.dv_render, mode, str(bitrate)]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1)
        except Exception as e:
            logbook.exception(f"resolve {p.ep}: launch", e)
            return False, "", f"resolve launch failed: {e}"
        out_lines = []

        def _reader():
            # stream output live so the RENDER part's % surfaces as a progress bar;
            # keep the lines around for the failure tail. (Reader thread so the abort/
            # timeout poll below stays responsive even between sparse render updates.)
            for line in proc.stdout:
                out_lines.append(line)
                m = re.match(r"RENDER_PCT (\d+)", line.strip())
                if m and progress:
                    progress({"stage": "resolve", "ep": p.ep, "pct": int(m.group(1))})
        threading.Thread(target=_reader, daemon=True).start()
        deadline = time.time() + RESOLVE_TIMEOUT
        while proc.poll() is None:
            aborted = abort is not None and abort.is_set()
            if aborted or time.time() > deadline:
                proc.kill()
                reason = "aborted (stop-time)" if aborted else "TIMED OUT — Resolve unresponsive"
                logbook.failure(f"resolve {p.ep}: killed subprocess — {reason}")
                return False, "", f"resolve killed — {reason}"
            time.sleep(5)
        out = "".join(out_lines)
        tail = " ".join(out.strip().splitlines()[-1:])[:180]
        ok = _is_dv81(_vstream(p.dv_render))
        if not ok:
            logbook.failure(f"resolve {p.ep}: rc={proc.returncode} :: {tail}")
        return ok, out, ("rendered DV 8.1" if ok else f"resolve failed (rc={proc.returncode}): {tail}")

    try:
        ok, out, reason = _run(p.source)
        if ok or not fast or not any(mk in out for mk in _MEZZ_MARKERS):
            return ok, reason
        # Fast-path ingest failure — Resolve can't decode this source (VP9/AV1). Build the
        # compat mezzanine and retry ONCE; either way the big temp is deleted before return.
        logbook.failure(f"resolve {p.ep}: source not ingestible — building HEVC compat mezzanine")
        if progress:
            progress({"stage": "resolve", "ep": p.ep, "pct": 0})
        mok, mezz = _build_mezzanine(p, abort, progress)
        if not mok:
            return False, f"compat mezzanine failed: {mezz}"
        try:
            ok, _out, reason = _run(mezz)
            return ok, (reason + " (via compat mezzanine)" if ok else reason)
        finally:
            try: os.remove(mezz)
            except OSError: pass
    finally:
        _quit_resolve_focus_app()


def _remux(p, abort, progress=None):
    """mute DV video -> PEAK-CAPPED DV video (dvcap x265 re-encode, hard `max_peak_mbps`
    ceiling — see remux.py; NO uncapped fallback) + audio (CFR file) + subtitles (ORIGINAL
    download) -> final master. Audio comes from the CFR file — the DV video derives from the
    CFR timeline, so that audio is guaranteed in sync; subtitles come from the original because
    the CFR pass no longer carries them. remux() dispatches on p.final's extension: MKV
    (lossless audio / bitmap subs) or MP4 (default). The x265 pass makes this stage LONG
    (~an hour per episode) — it reports live progress and honours abort."""
    import plan
    import remux
    import settings as settings_mod
    _st = settings_mod.get_settings()
    cap = int(_st.get("max_peak_mbps", 50))
    lufs = _st.get("audio_target_lufs", -16) or None   # 0/None = boost off
    # Per-item "Normalize audio" gate — p.series is the item's settings key for ALL kinds
    # (TV series name / movie title / channel folder — the same key its preset uses).
    # OFF -> None -> the boost-off bit-exact copy path remux already has.
    if lufs and p.series and not settings_mod.get_show_normalize_audio(p.series):
        lufs = None
    if plan.plan_for(p.source).get("topaz") == "rpu-only":
        # FAST PATH (HDR10 keep-the-source): no re-encode, no peak cap — the ORIGINAL stream
        # ships with Resolve's DV RPU injected (user-dictated; the source's own peaks were
        # already direct-playing before the pipeline touched it).
        res = remux.remux_inject(p.dv_render, p.source_cfr, p.source, p.final,
                                 audio_target_lufs=lufs, abort=abort)
        return res.ok, res.reason
    bounds = _read_topaz_bounds(p.source_basename) or None   # segment at this episode's topaz boundaries

    # Same notched segment bar as Topaz: on_plan gives the cumulative segment-end frames once,
    # then each progress tick derives notches (each end as a 0..1 fraction) + seg_done (how many
    # ends the cumulative frame count has passed — drives the little flash) + seg_total.
    seg_plan = {"ends": None, "total": None}
    def _on_plan(ends, exact_total):
        seg_plan["ends"], seg_plan["total"] = ends, exact_total

    def _prog(frames, total):
        if not progress:
            return
        t = seg_plan["total"] or total
        d = {"stage": "remux", "ep": p.ep, "frames": frames, "total": t,
             "pct": round(100.0 * frames / t, 1) if t else None}
        if seg_plan["ends"] and t:
            ends = seg_plan["ends"]
            d["notches"] = [round(e / t, 4) for e in ends]
            d["seg_done"] = sum(1 for e in ends if frames >= e)
            d["seg_total"] = len(ends)
        progress(d)

    res = remux.remux(p.dv_render, p.source_cfr, p.source, p.final,
                      cap_mbps=cap, audio_target_lufs=lufs, boundaries=bounds, abort=abort,
                      on_progress=_prog, on_plan=_on_plan)
    return res.ok, res.reason


def _upload(p, abort, progress=None):
    """final master -> NAS library (FTP STOR, owner 1000:10), then REPLACE: delete
    the superseded 1080p original — but only after the 4K master is verified on the
    NAS (transfer.replace_original guards on size). The 4K keeps the HDR10 DV name."""
    on_prog = None
    if progress:
        def on_prog(done, total):
            progress({"stage": "upload", "ep": p.ep, "pct": round(done / total * 100)})
    if p.youtube:                         # FOLDER-SPLIT: publish master into the Plex lib (mirrored
        # path, youtarr's stem) + copy sidecars so Plex keeps the metadata. Staging is purged in
        # cleanup (resume-safe), never here — so a re-run can always re-pull the source if needed.
        ok, _remote, reason = transfer.publish_master(
            p.final, p.nas_final, p.sidecar_dir, os.path.dirname(p.source), on_progress=on_prog)
        return ok, reason
    ok, remote, reason = transfer.upload(p.final, p.nas_dir, on_progress=on_prog)
    if not ok:
        return False, reason
    rok, rmsg = transfer.replace_original(remote, p.nas_source, p.final)
    return True, f"{reason}; {rmsg}"


def _cleanup(p, abort, progress=None):
    """Verified upload done -> PERMANENTLY delete ALL local working files. os.remove
    erases them (never Trash), so the ~300 GB/episode of ProRes can't accumulate and
    fill the disk. If ANY file can't be removed it is reported and the stage fails —
    a silently-undeleted file is exactly what would stop the pipeline indefinitely."""
    # Also sweep the intermediate TEMP files a crash could orphan (a Topaz encode renames
    # base.part.ext → prores on success; a remux writes final.tracks.mp4). They aren't in
    # working_files() (so stage_done/resume stays simple), but they'd silently eat ~hundreds
    # of GB if left behind, so cleanup removes them best-effort.
    try:
        if os.path.exists(p.final + ".tracks.mp4"):     # remux temp
            os.remove(p.final + ".tracks.mp4")
        if os.path.exists(p.final + ".capped.hevc"):    # remux temp: the concat of the segments
            os.remove(p.final + ".capped.hevc")
        if os.path.exists(p.final + ".dv.mp4"):         # remux temp: MKV-path DV video intermediate
            os.remove(p.final + ".dv.mp4")
        if os.path.exists(p.final + ".src.hevc"):       # inject temp: source Annex-B ES
            os.remove(p.final + ".src.hevc")
        if os.path.exists(p.final + ".inject.hevc"):    # inject temp: RPU-injected ES
            os.remove(p.final + ".inject.hevc")
        if os.path.exists(mezzanine_path(p.source)):    # fast-path Resolve-compat mezzanine
            os.remove(mezzanine_path(p.source))
    except OSError:
        pass
    shutil.rmtree(p.segdir, ignore_errors=True)          # the resumable-encode chunks (topaz output)
    shutil.rmtree(p.final + ".remuxsegs", ignore_errors=True)   # the resumable REMUX segments + rpu
    deleted, failed = 0, []
    for f in p.working_files():
        if not os.path.exists(f):
            continue
        try:
            os.remove(f)                 # permanent — does NOT go to Trash
            deleted += 1
        except OSError as e:
            failed.append(f"{os.path.basename(f)} ({e.strerror})")
    remaining = [f for f in p.working_files() if os.path.exists(f)]
    if os.path.isdir(p.segdir):                  # the ~238 GB chunk dir MUST be gone
        remaining.append(p.segdir)
    if remaining:
        return False, f"deleted {deleted}, but {len(remaining)} ITEM(S) REMAIN — disk will fill: {failed}"
    if p.youtube:                                # folder-split: the master is safely in the Plex lib,
        # so purge the raw STAGING copy (keeps /YouTube-raw lean) and record the video as done. Both
        # are done HERE (the terminal stage) as well so a resume that skips the post-upload block
        # still lands them; purge is best-effort (leftover raw only wastes space, never blocks).
        import youtube as _yt
        try: transfer.delete_tree(p.sidecar_dir)
        except Exception: pass
        _yt.mark_done(_yt.video_id(p.source_basename))
    return True, f"permanently deleted {deleted} local working files + chunks for {p.ep}"
