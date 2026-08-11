"""Heavy stage runners for the orchestrator. Each returns (ok, message).

download / topaz / remux / upload / cleanup run fully unattended. The resolve
stage runs setup + render via the scripting API; its two UI-only steps (DV Analyze
All Shots @ 1000-nit, and the DV Profile 8.1 dropdown) go through dv_shim, which
needs this process to have macOS Screen-Recording + Accessibility grants and
`cliclick`. Missing grants surface in preflight/Setup (check_tcc_grants); if they
are missing at run time the resolve stage returns not-ok and the episode stays
parked at this stage (resumable).
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
YOUTUBE_RENDER_KBPS = 35000         # YouTube renders target THIS instead of the floor: comfortably
                                    # transparent for upscaled web video, and its 1-s peaks land under
                                    # the 50 Mbps cap — which lets the remux SHIP the render as-is
                                    # (remux_ship_render) instead of an hour-class x265 re-encode.
                                    # SHIELD context: single-layer DV stutters above ~60 Mbps.

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


_PEAKGATE_FILE = os.path.expanduser("~/.topaz-pipeline/source_peaks.json")
_PEAKGATE_LOCK = threading.Lock()


def _source_peak_mbps(p):
    """Measured 1-second VIDEO peak of the source (Mb/s), cached by basename — the packet
    sweep reads the whole file (~minutes on a movie), so retries and relaunches reuse the
    book. Measures the CFR file: for fast-path items it is a stream copy (identical bits),
    and it is the exact stream the RPU is frame-aligned with and that a revoked stream copy
    would re-encode. Returns None when unmeasurable (the caller fails the attempt — never
    ship OR re-encode unverified); an unmeasurable result is never cached."""
    return _peak_of(p.source_cfr, p.source_basename)


def _peak_of(path, key):
    """The cached-peak machinery on an ARBITRARY file (companion combine measures the
    verdict WINNER, which may be the fetched seedbox copy). `key` is the peak book's
    entry name."""
    import dvcap
    try:
        with _PEAKGATE_LOCK:
            with open(_PEAKGATE_FILE) as f:
                v = (json.load(f) or {}).get(key)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    except (OSError, ValueError, TypeError):
        pass
    mbps = dvcap.video_peak_1s_mbps(path)
    if mbps <= 0:
        return None
    try:
        with _PEAKGATE_LOCK:
            try:
                with open(_PEAKGATE_FILE) as f:
                    d = json.load(f)
                if not isinstance(d, dict):
                    d = {}
            except (OSError, ValueError):
                d = {}
            d[key] = round(mbps, 1)
            os.makedirs(os.path.dirname(_PEAKGATE_FILE), exist_ok=True)
            tmp = _PEAKGATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.replace(tmp, _PEAKGATE_FILE)
    except OSError:
        pass
    return mbps


def _plan_fast_path_bounds(p, progress=None, src=None) -> str:
    """RESOLVE-ONLY fast path: no upscale, but the capped remux still re-encodes — so run
    topaz's PLANNING front half (scene detect + ~90 s grouping, the exact same utilities)
    and stash the cumulative end-frames, so the remux segments at scene cuts like the
    topaz path (user-dictated). Best-effort: any failure just leaves the remux on its
    flat ~SEG_SECONDS plan. Returns a short note for the stage message. The scan is
    cached twice over — bounds in _SEGBOUNDS_FILE (checked first, so retries are free)
    and raw cut times in segdir/scenes.json (swept at cleanup like any segdir).
    `src` overrides WHICH file gets scanned (companion combine scans the verdict winner;
    the bounds book still keys on the item's own basename)."""
    import topaz
    src = src or p.source_cfr
    if _read_topaz_bounds(p.source_basename):        # resume: already planned — no re-scan
        return " (scene plan cached)"
    try:
        if progress:
            progress({"stage": "topaz", "ep": p.ep, "pct": None})
        total = topaz.total_frames(src)
        fps, _dur = topaz.media_timing(src)
        if not total or not fps:
            return " (no scene plan — flat remux segments)"
        os.makedirs(p.segdir, exist_ok=True)         # scenes.json cache lives here
        cuts = topaz._cached_scene_frames(src, p.segdir, fps)
        segs = topaz.plan_segments(total, fps, cuts)
        if segs:
            _write_topaz_bounds(p.source_basename, [b for (_a, b) in segs])
            return f" — {len(segs)} scene-cut segments planned for the remux"
    except Exception:
        pass
    return " (no scene plan — flat remux segments)"


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
        "download": _download, "extend": _extend, "topaz": _topaz, "resolve": _resolve,
        "remux": _remux, "upload": _upload, "cleanup": _cleanup,
    }.get(stage, lambda *_a, **_k: (False, f"unknown stage {stage}"))
    ep = getattr(p, "ep", "?")
    try:
        # `should_pause` = yield at the next segment boundary: topaz yields to two live
        # remuxes; a remux yields to an active Resolve (Resolve gets the whole machine).
        # Both are segmented, so a pause costs nothing — the retry resumes in place.
        if stage in ("topaz", "remux"):
            ok, msg = fn(p, abort, progress, should_pause)
        elif stage == "download":
            ok, msg = fn(p, abort, progress, low_prio)
        else:
            ok, msg = fn(p, abort, progress)
    except Exception as e:                       # any stage bug is logged, never silent
        logbook.exception(f"{stage} {ep}", e)
        return False, f"{stage} crashed: {e.__class__.__name__}: {e}"
    # A clean segment-boundary pause is NOT a failure — log it as an event so it never
    # shows up in the red "Recent issues" banner. Same for a "permanent:" refusal
    # (already-DV source): expected behavior, and the park logs its own single line.
    benign = (not ok) and (str(msg).startswith("paused:") or str(msg).startswith("permanent:"))
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
    if p.combine:
        return _download_combine(p, abort, progress)
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
    # Refuse an already-Dolby-Vision source HERE, before the expensive CFR re-encode.
    # Queue building already excludes DV items (NAS dv-manifest / name mark); this catches
    # the slip-throughs (manifest gap, unmarked name) the moment the file can be probed.
    # "permanent:" tells the run loop the condition can never heal — park once, durably,
    # instead of the 60s-retry ladder. Probe the ORIGINAL: the CFR pass strips DV side data.
    # probe_input fails open (is_dv False on any probe error) → the topaz guard backstops.
    import plan
    info = plan.probe_input(p.source)
    if info.get("is_dv"):
        return False, "permanent: source is already Dolby Vision — nothing to upscale"
    # Show-aspect book (drives the per-show "Extend borders" row's visibility): every TV
    # source probe records its show's aspect, so the row appears the first time any
    # episode of a 4:3 show passes through. Best-effort — never blocks the download.
    if not (p.movie or p.youtube):
        try:
            import borders
            label = borders.aspect_label(info.get("width"), info.get("height"),
                                         info.get("sar"))
            if label:
                borders.record_show_aspect(p.series, label)
        except Exception:
            pass
    return _ensure_cfr(p, abort, progress, low_prio=low_prio)


def _download_combine(p, abort, progress=None):
    """COMPANION COMBINE download: BOTH copies land on scratch — the NAS copy over
    Visionary's own FTP (same reuse/verify/partial rules as the classic path) and the
    seedbox companion streamed through the Shuttle relay (rclone-cat; nothing staged on
    the NAS). NO Dolby Vision refusal (a DV copy is the point here) and NO CFR pass
    (combine never feeds Topaz; the remux and Resolve read the originals directly)."""
    import companion as companion_mod
    import scratch as scratch_mod
    if not p.companion_virtual or p.companion_size <= 0:
        # book lost/unpaired after the item entered the pipeline — never guess tracks
        return False, "permanent: combine verdict lost — re-pair the companion"
    # Free-space precheck: both copies + the winner's two ES transients + the master —
    # roughly 4x the larger copy when both still need to land. Short → retryable hold
    # (space frees as other items finish), never a park.
    nas_size = _remote_size(p.nas_source, None) or 0
    pending = ((0 if os.path.exists(p.source) else nas_size)
               + (0 if os.path.exists(p.companion_src) else p.companion_size))
    need_gb = (pending + 3 * max(nas_size, p.companion_size)) / 1e9
    avail = scratch_mod.available_gb()
    if avail is not None and nas_size and avail < need_gb:
        return False, f"holding: combine needs ~{need_gb:.0f} GB free ({avail:.0f} GB available)"
    # -- NAS copy (FTP, verified) ---------------------------------------------------
    have_source = os.path.exists(p.source) and _source_complete(p) is not False
    if os.path.exists(p.source) and not have_source:
        try: os.remove(p.source)               # verified INCOMPLETE → re-pull clean
        except OSError: pass
    if not have_source:
        on_prog = None
        if progress:
            def on_prog(done, total):          # NAS pull = the front half of the bar
                progress({"stage": "download", "ep": p.ep, "pct": round(done / total * 50)})
        ok, _local, reason = transfer.download(p.nas_source, os.path.dirname(p.source),
                                               on_progress=on_prog, abort=abort)
        if not ok:
            if os.path.exists(p.source):
                try: os.remove(p.source)
                except OSError: pass
            return ok, reason
    # -- companion (relay-streamed from the seedbox) --------------------------------
    if progress:
        progress({"stage": "download", "ep": p.ep, "pct": 50, "step": "fetching companion"})
    try:
        m = companion_mod.manifest(p.companion_virtual)
        if not (m.get("files") or []):
            companion_mod.mark(p.source_basename, "vanished")
            return False, "permanent: companion no longer on the seedbox — re-pair"
    except companion_mod.RelayError as e:
        return False, f"companion manifest: {e}"     # relay down/busy → retryable
    on_cprog = None
    if progress:
        def on_cprog(done, total):                   # companion = the back half
            progress({"stage": "download", "ep": p.ep,
                      "pct": 50 + round(done / total * 50), "step": "fetching companion"})
    ok, reason = companion_mod.fetch_to_file(p.companion_virtual, p.companion_src,
                                             p.companion_size,
                                             on_progress=on_cprog, abort=abort)
    if not ok:
        return False, "companion fetch: " + reason
    return True, f"both copies on scratch ({reason})"


def _ensure_cfr(p, abort, progress=None, low_prio=False):
    """Make p.source_cfr (constant frame rate, same rate) from the verified p.source.
    Skipped if a valid CFR file is already present (resume-safe). Reports the re-encode as
    the back end of the download bar (20→99%) since it's the longer half of the stage."""
    import topaz
    import orchestrator
    import plan
    orchestrator.apply_container(p)     # source is on disk now → lock the container (mkv vs mp4)
    if topaz.is_cfr_ready(p.source_cfr):
        return True, "source + CFR already on disk (reused)"
    # FAST-PATH items (rpu-only / resolve-only) skip Topaz, so they don't need the true-CFR
    # re-encode that keeps Topaz's frame counts stable — this file only carries their AUDIO
    # (a bit-copy of the original's either way) and decodable frames for scene-cut planning.
    # Stream-copy instead: minutes and disk-bound rather than hours of libx264 on a 4K
    # movie whose video bytes nothing downstream reads (live-caught: a 60 GB REMUX).
    fast = plan.plan_for(p.source).get("topaz") in ("rpu-only", "resolve-only")
    on_prog = None
    if progress:
        total = topaz.total_frames(p.source) or 0
        def on_prog(frames):
            pct = 20 + round(frames / total * 79) if total else None
            progress({"stage": "download", "ep": p.ep, "pct": min(99, pct) if pct else None})
    res = topaz.to_cfr(p.source, p.source_cfr, abort=abort, on_progress=on_prog,
                       low_prio=low_prio, copy_only=fast)
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


def _extend(p, abort, progress=None):
    """AI BORDER EXTENSION (4:3 -> 16:9): outpaint the CFR source's left/right borders
    with WAN VACE through the locally installed ComfyUI (headless, dedicated port),
    producing p.source_wide for Topaz to upscale. QUALITY PRINCIPLE: only the borders
    are AI — each chunk is outpainted at ~480p working res, then just the generated side
    strips are cropped, upscaled to source height, and hstacked around the ORIGINAL
    full-res frames. Video only — the remux takes audio from source_cfr as always.

    CHUNKED + RESUMABLE (81 frames/chunk, ~1-2 min each — an episode is overnight-scale):
    finished chunks persist in <source_wide>.segments under a dvcap.ensure_segdir
    manifest (source identity + geometry + models + sampler + seed — any change wipes);
    an abort/deploy costs at most the in-flight chunk. The Comfy backend is stopped on
    every exit path (plus killpg/atexit inside borders)."""
    try:
        import borders, dvcap, topaz
        g0 = borders.extend_gate(p)
        if not g0["needed"]:
            return True, f"no border extension — {g0['reason']}"
        geom = g0["geom"]
        env = borders.discover()
        ready, missing = borders.env_ready(env)
        if not ready:
            return False, "border extend: " + "; ".join(missing)
        total = borders.count_frames(p.source_cfr)      # EXACT (packet count): chunk
        fps, _dur = topaz.media_timing(p.source_cfr)    # indices + the final verify need it
        if not total or not fps:
            return False, "border extend: could not read the source's frame count/rate"
        chunks = borders.plan_chunks(total)
        # Resume identity: any change to the source, geometry, models, sampler, or seed
        # invalidates the persisted chunks (same contract as the remux's segdir).
        import zlib
        try:
            st = os.stat(p.source_cfr)
            src_id = f"{st.st_size}:{int(st.st_mtime)}"
        except OSError:
            src_id = "missing"
        seed = zlib.crc32(p.source_basename.encode("utf-8")) & 0x7FFFFFFF
        manifest = {"src": src_id, "chunk": borders.CHUNK_FRAMES, "geom": geom,
                    "models": sorted(borders.MODELS),
                    "sampler": [borders.STEPS, borders.CFG, borders.SAMPLER,
                                borders.SCHEDULER, borders.SHIFT],
                    "seed": seed, "prompt": borders.DEFAULT_PROMPT}
        segdir = p.source_wide + ".segments"
        dvcap.ensure_segdir(segdir, manifest)
        backend = borders.ComfyBackend(env, os.path.join(segdir, "in"),
                                       os.path.join(segdir, "out"))
    except Exception as e:
        logbook.exception(f"extend {getattr(p, 'ep', '?')}", e)
        return False, f"border extend setup crashed: {e.__class__.__name__}: {e}"

    # Notched progress, the Topaz pattern: notches = chunk ends as 0..1 fractions
    # (omitted past 48 chunks — a movie-length notch comb is visual noise; seg counts +
    # pct still tell the story), seg_done drives the flash, seg_rem_pct feeds the ETA.
    ends, cum = [], 0
    for _s, n in chunks:
        cum += n
        ends.append(cum)
    notches = [round(e / total, 4) for e in ends] if len(ends) <= 48 else None
    def report(frames_done, done_chunks):
        if not progress:
            return
        d = {"stage": "extend", "ep": p.ep,
             "pct": min(100, round(frames_done / total * 100)),
             "seg_done": done_chunks, "seg_total": len(chunks)}
        if notches:
            d["notches"] = notches
        if done_chunks < len(ends):
            d["seg_rem_pct"] = max(0.0, (ends[done_chunks] - frames_done) / total * 100)
        progress(d)

    if not backend.port_free():
        # Our dedicated port answering = a stale orphan of a hard-killed run (a graceful
        # stop kills the backend via atexit; SIGKILL can't). Reclaim it — 8189 is
        # Visionary's alone (reclaim_port refuses the Desktop app's 8188).
        borders.reclaim_port(env["port"])
        if not backend.port_free():
            return False, (f"border extend: port {env['port']} is already in use — "
                           "close whatever holds it (retried automatically)")
    backend.start()
    if not backend.wait_ready():
        tail = " ".join((backend.tail or [])[-4:])
        backend.stop()
        return False, "border extend: ComfyUI did not come up — " + (tail or "no output")
    client = borders.ComfyClient(backend.base)
    done_frames, done_chunks = 0, 0
    try:
        for k, (start, n) in enumerate(chunks):
            wide_k = os.path.join(segdir, f"wide_{k:04d}.mp4")
            if os.path.exists(wide_k) and borders.count_frames(wide_k) == n:
                done_frames += n
                done_chunks += 1
                report(done_frames, done_chunks)
                continue                                 # resume: chunk already composited
            if abort is not None and abort.is_set():
                return False, "aborted"
            t0 = time.monotonic()
            chunk_in = os.path.join(backend.in_dir, f"chunk_{k:04d}.mp4")
            r = subprocess.run(borders.chunk_extract_cmd(p.source_cfr, chunk_in, start,
                                                         n, geom),
                               capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                return False, "border extend: chunk extract failed — " + _err_tail(r.stderr)
            graph = borders.build_outpaint_graph(
                input_name=os.path.basename(chunk_in),
                canvas_w=geom["canvas_w"], canvas_h=geom["canvas_h"],
                pad_left=geom["pad_work"], pad_right=geom["pad_work"],
                length=n, fps=float(fps), seed=seed + k,
                filename_prefix=f"gen_{k:04d}")
            hist = client.wait(client.submit(graph), backend=backend, abort=abort)
            gen = borders.ComfyClient.output_file(hist, backend.out_dir)
            if not gen or not os.path.exists(gen):
                return False, f"border extend: ComfyUI produced no output for chunk {k}"
            part = wide_k + ".part.mp4"                  # atomic: rename marks the chunk done
            r = subprocess.run(borders.composite_cmd(p.source_cfr, gen, part, start, n,
                                                     geom),
                               capture_output=True, text=True, timeout=1800)
            got = borders.count_frames(part) if r.returncode == 0 else 0
            if got != n:
                try: os.remove(part)
                except OSError: pass
                return False, (f"border extend: composite failed for chunk {k} — "
                               + _err_tail(r.stderr or f"{got}/{n} frames"))
            os.replace(part, wide_k)
            for f in (chunk_in, gen):                    # per-chunk temps: keep the dir lean
                try: os.remove(f)
                except OSError: pass
            borders.add_pace_sample(time.monotonic() - t0)
            done_frames += n
            done_chunks += 1
            report(done_frames, done_chunks)
        list_file = os.path.join(segdir, "concat.txt")
        with open(list_file, "w") as f:
            for k in range(len(chunks)):
                f.write("file '%s'\n" % os.path.join(segdir, f"wide_{k:04d}.mp4"))
        r = subprocess.run(borders.concat_cmd(list_file, p.source_wide),
                           capture_output=True, text=True, timeout=1800)
        got = borders.count_frames(p.source_wide) if r.returncode == 0 else 0
        if got != total:
            try: os.remove(p.source_wide)               # a short wide file must never be
            except OSError: pass                        # mistaken for done by stage_done
            return False, (f"border extend: concat produced {got}/{total} frames — "
                           + _err_tail(r.stderr))
    except RuntimeError as e:                            # ComfyClient: node error / backend
        if str(e) == "aborted":                          # death (MPS OOM tail) / abort / timeout
            return False, "aborted"
        return False, "border extend: " + str(e)
    finally:
        backend.stop()
    return True, (f"[extend {geom['src_w']}x{geom['src_h']} -> "
                  f"{geom['final_w']}x{geom['src_h']}] {len(chunks)} chunks, "
                  f"{total} frames -> 16:9 wide source")


def _topaz(p, abort, progress=None, should_pause=None):
    """source -> ProRes 4444 XQ. Uses the SHOW's chosen preset + the input plan
    (upscale 1080p 2×, clean already-4K 1×; range PRESERVED, never SDR<->HDR).
    Reports live frame progress to the dashboard (Topaz is headless — the app is
    the only UI). `abort` lets the watchdog kill it mid-encode."""
    if p.combine:
        # COMPANION COMBINE never upscales — and this return must come BEFORE the
        # plan_for skip below, which would otherwise "permanent:"-refuse the DV copy
        # that is the whole point of the combine.
        return True, "combine — no upscale, both copies ship their own bits"
    import topaz, settings, plan
    # Plan from the ORIGINAL source: the CFR re-encode (libx264) strips Dolby Vision side
    # data, so an already-DV source must be detected here, not on the CFR file. Resolution/
    # HDR are identical in both. We then upscale the CFR file (exact, constant frame counts).
    pl = plan.plan_for(p.source)
    if pl["topaz"] == "skip":
        # "permanent:" → the run loop parks it immediately + durably (no 60s retries).
        # Reached when the download stage was already done (pre-fix scratch, reused source);
        # a fresh download refuses earlier, before the CFR pass.
        return False, "permanent: source is already Dolby Vision — nothing to upscale"
    if pl["topaz"] in ("rpu-only", "resolve-only"):
        # HIGH-BITRATE 4K FAST PATH: the source picture IS the deliverable — no upscale.
        # Succeed as a no-op so the run loop proceeds straight to the Resolve stage.
        # resolve-only still runs topaz's scene-cut PLANNING so its capped remux segments
        # at scene cuts (rpu-only plans LAZILY in the remux stage instead — it only needs
        # segments at all when the peak gate revokes its stream copy).
        note = _plan_fast_path_bounds(p, progress) if pl["topaz"] == "resolve-only" else ""
        return True, pl["reason"] + " — skipping upscale" + note
    if p.youtube:
        # YOUTUBE SKIPS TOPAZ (user-dictated 2026-08-06): Resolve does the scaling
        # instead — SuperScale 2x for 1080p sources (a scripting-API clip property, no
        # screen navigation), the 4K timeline's plain scaling otherwise. Scene-cut
        # planning still runs so the capped remux segments at real cuts.
        note = _plan_fast_path_bounds(p, progress)
        return True, "YouTube → Resolve scales it (SuperScale for 1080p) — skipping Topaz" + note
    # Per-resolution preset variant: the plan's bucket (from the source height) picks which
    # tuned param set to use; the source can be ANY of 480p/720p/1080p and still reach 4K.
    params = settings.show_topaz_params(p.series, pl.get("res") or "1080p")
    key = settings.show_preset_key(p.series)
    # The extend stage's border-extended 16:9 file supersedes the CFR source as Topaz's
    # input when it exists (same height + frame count, so the plan's res bucket and every
    # downstream frame-count check hold unchanged; audio still comes from source_cfr).
    src_in = p.source_cfr
    wide = getattr(p, "source_wide", "")
    if wide and os.path.exists(wide):
        src_in = wide
    total = topaz.total_frames(src_in)
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
    res = topaz.upscale_resumable(src_in, segdir=p.segdir, profile=params, scale=pl["scale"],
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


def _build_mezzanine(p, abort, progress=None, src=None):
    """Transcode the source -> the mezzanine via topaz's registered/killable ffmpeg runner
    (the repo-standard wrapper — no Topaz processing involved). (ok, path_or_tail).
    `src` overrides the input (YouTube passes its TRUE-CFR file so the mezzanine's frame
    count matches what the render-completeness gate counts against)."""
    import topaz
    import dvcap
    inp = src or p.source
    dst = mezzanine_path(p.source)
    # REUSE a complete mezzanine from an earlier attempt (frame-count-verified against
    # the input): a lid-close/transient killed pass used to REBUILD the ~10-minute,
    # ~50 GB encode on every retry (live-caught 2026-08-06). A partial never matches
    # (unreadable moov / short count); cleanup still sweeps strays.
    want = topaz.total_frames(inp) or 0
    if want and os.path.exists(dst) and topaz.total_frames(dst) == want:
        return True, dst
    if not want:
        return False, "could not count the input's frames"
    # SEGMENTED, dvcap-style (user-dictated 2026-08-06): ~5-min chunks with atomic
    # publish into a manifest-guarded segdir. A kill keeps every finished chunk; the
    # retry encodes only what's missing, then a stream-copy concat assembles the
    # mezzanine. ensure_segdir wipes the chunks whenever the INPUT changes.
    rate = topaz._fps_fraction(inp)
    color = topaz.source_color(inp)
    kbps = max(4 * _source_video_kbps(p.source), MEZZ_MIN_KBPS)
    segdir = dst + ".segments"
    try:
        st = os.stat(inp)
        ident = f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        ident = "missing"
    dvcap.ensure_segdir(segdir, {"mode": "mezz", "src": ident,
                                 "frames": want, "fps": str(rate), "kbps": int(kbps)})
    segs = dvcap.plan_segments(want, rate, dvcap.SEG_SECONDS)
    done = 0
    seg_files = []
    for i, (a, b) in enumerate(segs):
        if abort is not None and abort.is_set():
            return False, "aborted"
        n = b - a
        sf = os.path.join(segdir, f"seg_{i:04d}.mp4")
        seg_files.append(sf)
        if dvcap.count_hevc_frames(sf) == n:            # already encoded (resume) → skip
            done += n
            if progress:
                progress({"stage": "resolve", "ep": p.ep, "pct": min(99, round(done / want * 100))})
            continue
        part = sf + ".part"
        for f in (sf, part):                            # partial/stale → redo clean
            try: os.remove(f)
            except OSError: pass
        cmd = topaz.build_mezzanine_segment_command(topaz.FFMPEG_HB, inp, part,
                                                    start_frame=a, n=n, rate=rate,
                                                    color=color, kbps=kbps)
        on_prog = None
        if progress:
            base = done
            def on_prog(frames, _b=base):
                progress({"stage": "resolve", "ep": p.ep,
                          "pct": min(99, round((_b + frames) / want * 100))})
        rc, _frames, aborted, tail = topaz._run_ffmpeg(cmd, os.environ.copy(), abort=abort,
                                                       on_progress=on_prog, timeout=MEZZ_TIMEOUT)
        if aborted or rc != 0:
            try: os.remove(part)
            except OSError: pass
            return False, ("aborted" if aborted else (tail or "")[-180:])
        got = dvcap.count_hevc_frames(part)
        if got != n:                                    # seek/decode drift → NEVER publish it
            try: os.remove(part)
            except OSError: pass
            return False, f"mezz segment {i + 1}/{len(segs)} frame mismatch: {got} != {n}"
        os.replace(part, sf)                            # atomic publish, like dvcap's encode
        done += n
    # Stream-copy concat of the finished chunks -> the ingest MP4. Identical encodes
    # (same builder, same params) concat cleanly; the count gate below is the proof.
    lst = os.path.join(segdir, "concat.txt")
    with open(lst, "w") as f:
        for sf in seg_files:
            f.write("file '%s'\n" % sf.replace("'", "'\\''"))
    cc = subprocess.run([topaz.FFMPEG_HB, "-hide_banner", "-nostdin", "-y",
                         "-f", "concat", "-safe", "0", "-i", lst,
                         "-c", "copy", "-tag:v", "hvc1", "-movflags", "+faststart", dst],
                        capture_output=True, text=True, timeout=MEZZ_TIMEOUT)
    if cc.returncode != 0 or topaz.total_frames(dst) != want:
        try: os.remove(dst)
        except OSError: pass
        return False, "mezz concat failed: " + (cc.stderr or "")[-180:]
    shutil.rmtree(segdir, ignore_errors=True)           # SUCCESS: chunks won't be needed again
    return True, dst


def _resolve(p, abort, progress=None):
    """ProRes -> mute Dolby Vision .mov, run in a KILLABLE SUBPROCESS (setup + DV
    Analyze All + render, via resolve_pipeline.py episode). The Resolve scripting
    API (fusionscript) can hang on a socket wait HOLDING THE GIL when Resolve is
    unresponsive — in-process that wedges the entire orchestrator (the exact bug
    that froze the app mid-run). As a child process it's killable on timeout/abort,
    so a Resolve hang fails this stage cleanly and the orchestrator keeps breathing.
    The OUTPUT MODE picks the Resolve project. Automatically that is ALWAYS the
    1000-nit Dolby Vision project (user-dictated 2026-08-09); the 2000-nit DV project
    and the non-DV SDR project are reachable ONLY through explicit per-item overrides.
    When the stage finishes (any outcome) Resolve is quit and the app refocused.
    FAST PATH: if Resolve can't INGEST the original source (VP9/AV1 — the gate excludes
    nothing by codec), a lightweight HEVC mezzanine is built and the run retried once."""
    import plan
    if p.combine:
        # COMPANION COMBINE: a REAL RPU (either copy) makes this whole stage unnecessary —
        # real DV beats Resolve DV (user-dictated). Only the neither-copy-has-DV fallback
        # renders, and it analyzes the verdict WINNER. Checked BEFORE the plan skip below:
        # a DV library copy would otherwise "permanent:"-refuse the item it belongs to.
        import companion as companion_mod
        cv = companion_mod.confirmed_verdict(p.source_basename)
        if not cv:
            return False, "permanent: combine verdict lost — re-pair the companion"
        if cv["verdict"].get("rpu_from") != "resolve":
            return True, "combine: real Dolby Vision RPU present — Resolve not needed"
    pl = plan.plan_for(p.source)   # ORIGINAL: CFR re-encode strips DV side data (skip-detection)
    if not p.combine and pl.get("resolve") == "skip":
        return False, "permanent: source is already Dolby Vision — nothing for Resolve to do"
    # OUTPUT MODE. "auto" = the 1000-nit DV project, whatever the intake range
    # (user-dictated 2026-08-09; 2000-nit is manual-only). A per-item override pins it regardless of the source;
    # "sdr" is the only value that produces a non-DV master, and the only one whose Resolve
    # stage needs no screen automation at all.
    import settings as _st
    override = _st.get_show_output_mode(p.series)
    # The setting's values ARE the Resolve modes now ("sdr" / "dv1000" / "dv2000"), so there is
    # no translation table to get wrong. "auto" = ALWAYS 1000 nits (user-dictated
    # 2026-08-09): the 2000-nit project stays available as an explicit per-item override,
    # but nothing selects it automatically — HDR intake included.
    mode = override if override in ("sdr", "dv1000", "dv2000") else "dv1000"
    # WHICH SCREEN. Resolved ONCE here, before the subprocess spawns, so an unplugged or
    # unproven display is a clean stage-level decision with a log line — not a surprise
    # 200 seconds into a Resolve cold start.
    try:
        import preflight as _pf
        host, host_why = _pf.chosen_host()
    except Exception as e:
        host, host_why = None, "%s: %s" % (e.__class__.__name__, e)
    if host:
        print(f"resolve: hosting on {host.get('name')} ({host.get('key')})", flush=True)
    elif (_st.get_settings().get("resolve_host_pinning") and _st.get_display_priority()
            and host_why != "chosen display is the main one"
            and not _st.get_settings().get("resolve_host_fallback_main")):
        # A PINNED display that is gone/unproven must not silently become "drive the main
        # display" — the whole point of pinning is that the main screen belongs to the
        # user. Same "host-display:" reason as the post-spawn HOST_UNAVAILABLE path, so
        # the orchestrator's resolve-failure ladder treats it as a retryable hold (plug
        # the display back in, or set resolve_host_fallback_main to allow main instead).
        return False, "host-display: %s" % (host_why or "pinned display unavailable")
    fast = pl.get("topaz") in ("rpu-only", "resolve-only")
    # YouTube runs single-mode too (no Topaz segdir), but on the TRUE-CFR file — web
    # sources are routinely VFR, and the render-completeness gate counts frames against
    # source_cfr. SuperScale 2x only for ~1080p sources (user-dictated).
    yt = bool(p.youtube)
    single = fast or yt or p.combine
    ss = "-"
    if yt:
        try:
            h = int((pl.get("input") or {}).get("height") or 0)
        except (TypeError, ValueError):
            h = 0
        if 900 <= h <= 1200:
            ss = "2"
    # Match the ORIGINAL intake's bitrate (the real source quality), not the CFR re-encode's
    # near-lossless crf bitrate, which would inflate the export for no quality gain. In
    # rpu-only mode the render's VIDEO is discarded (only its RPU ships) — floor is plenty.
    # (A mezzanine retry keeps this same value — the inflated mezz bitrate is not quality.)
    bitrate = (EXPORT_BITRATE_FLOOR_KBPS if (pl.get("topaz") == "rpu-only" or p.combine)
               else YOUTUBE_RENDER_KBPS if yt
               else max(_source_video_kbps(p.source), EXPORT_BITRATE_FLOOR_KBPS))

    def _run(video_in):
        """One resolve_pipeline subprocess pass. (ok, out, reason) — `out` is the FULL
        output (the import-failure markers print mid-stream, not on the last line);
        reason is None on a hard kill/abort/launch failure (no retry on those)."""
        cmd = [sys.executable, os.path.join(ENGINE_DIR, "resolve_pipeline.py"),
               ("single" if single else "episode"),
               (video_in if single else p.segdir), p.dv_render, mode, str(bitrate),
               # 6th arg: the display to drive. "-" = the main display (every older
               # behaviour). 7th: SuperScale factor for single mode ("-" = none).
               # Both files deploy together, so argv lockstep is fine.
               (host.get("key") if host else "-"), ss]
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
                    # `rendering` gates the app's live screen preview OFF for the render
                    # phase (user-dictated 2026-08-06): the analysis worth watching is
                    # over, and each preview frame is a screencapture of the 4K panel
                    # while the machine renders. Carried only by these events, so any
                    # non-render event (a retry's setup) brings the preview back.
                    progress({"stage": "resolve", "ep": p.ep, "pct": int(m.group(1)),
                              "rendering": True})
                    continue
                # The screen/mouse takeover is about to happen. This marker is the ONLY
                # lead time that exists — the stage flips to "resolve" seconds before
                # Resolve launches, so the warning window is the deliberate hold the shim
                # takes, and this is what tells the app to draw the notice.
                # Three states, no timer. SOON = about to (roughly ten seconds of real
                # setup work still to run), BEGIN = the first click has landed and the mouse
                # is genuinely taken, END = handed back.
                if progress and line.startswith("SCREEN_TAKEOVER_SOON"):
                    progress({"stage": "resolve", "ep": p.ep,
                              "takeover_soon": True, "takeover_active": False})
                elif progress and line.startswith("MOUSE_IN_USE"):
                    # A timestamp, not a flag: the app shows the notice while this is fresh,
                    # so it tracks real pointer use and expires by itself. The mouse is only
                    # busy in short click bursts — the hour of wait_for_analysis that follows
                    # is screenshots only.
                    try:
                        at = int(line.strip().split()[1])
                    except (IndexError, ValueError):
                        at = int(time.time())
                    progress({"stage": "resolve", "ep": p.ep,
                              "takeover_soon": False, "mouse_at": at})
                elif progress and line.startswith("SCREEN_TAKEOVER_END"):
                    progress({"stage": "resolve", "ep": p.ep, "takeover_soon": False})
        threading.Thread(target=_reader, daemon=True).start()
        # MONOTONIC: it pauses through a real sleep on macOS, so a laptop that slept
        # mid-pass wakes with its budget intact instead of a falsely-expired deadline
        # killing the subprocess ("starting over with resolve", user-caught 2026-08-06).
        deadline = time.monotonic() + RESOLVE_TIMEOUT
        while proc.poll() is None:
            aborted = abort is not None and abort.is_set()
            if aborted or time.monotonic() > deadline:
                proc.kill()
                reason = "aborted (stop-time)" if aborted else "TIMED OUT — Resolve unresponsive"
                logbook.failure(f"resolve {p.ep}: killed subprocess — {reason}")
                return False, "", f"resolve killed — {reason}"
            time.sleep(5)
        out = "".join(out_lines)
        tail = " ".join(out.strip().splitlines()[-1:])[:180]
        dv = _is_dv81(_vstream(p.dv_render))
        # LENGTH too. A render that died partway still carries a valid DOVI record, and
        # accepting it drops the ~190 GiB ProRes that is the only way to redo the upscale.
        from orchestrator import render_is_complete, _nb_frames
        whole = render_is_complete(p) if dv else True
        ok = dv and whole
        if dv and not whole:
            logbook.failure(f"resolve {p.ep}: TRUNCATED render "
                            f"({_nb_frames(p.dv_render)} of {_nb_frames(p.source_cfr)} frames)")
            return False, out, "resolve produced a SHORT render — not accepting it"
        if not ok:
            logbook.failure(f"resolve {p.ep}: rc={proc.returncode} :: {tail}")
        return ok, out, ("rendered DV 8.1" if ok else f"resolve failed (rc={proc.returncode}): {tail}")

    try:
        from orchestrator import combine_winner_path
        video_in = (combine_winner_path(p) if p.combine
                    else p.source_cfr if yt else p.source)
        if p.combine and not video_in:
            return False, "permanent: combine verdict lost — re-pair the companion"
        ok, out, reason = _run(video_in)
        # A PINNED display that is unplugged, asleep or unmovable must not silently become
        # "drive the main display" — that is the one outcome hosting exists to prevent. The
        # item defers instead, so a yanked cable stalls the run rather than surprising the
        # user, and it does not burn one of max_episode_fails on a cable.
        if not ok and "HOST_UNAVAILABLE" in out:
            line = next((l.strip() for l in out.splitlines() if "HOST_UNAVAILABLE" in l), "")
            return False, "host-display: %s" % (line.replace("HOST_UNAVAILABLE", "").strip()
                                                or "pinned display unavailable")
        if ok or not single or not any(mk in out for mk in _MEZZ_MARKERS):
            return ok, reason
        # Fast-path ingest failure — Resolve can't decode this source (VP9/AV1). Build the
        # compat mezzanine and retry ONCE; either way the big temp is deleted before return.
        logbook.failure(f"resolve {p.ep}: source not ingestible — building HEVC compat mezzanine")
        if progress:
            progress({"stage": "resolve", "ep": p.ep, "pct": 0})
        mok, mezz = _build_mezzanine(p, abort, progress,
                                     src=(video_in if (yt or p.combine) else None))
        if not mok:
            return False, f"compat mezzanine failed: {mezz}"
        ok, _out, reason = _run(mezz)
        if ok:                          # keep it on FAILURE — the retry reuses it instead of
            try: os.remove(mezz)        # re-encoding 10 minutes; cleanup sweeps a stray
            except OSError: pass
        return ok, (reason + " (via compat mezzanine)" if ok else reason)
    finally:
        _quit_resolve_focus_app()


def _combine_result(res, real_rpu_donor):
    """Map a combine RemuxResult to the stage's (ok, reason). A frame/fps mismatch
    against a REAL companion donor means the two releases are different cuts — that can
    never heal, so it parks permanently. The same mismatch against the Resolve render is
    just a truncated render: retryable (the resolve stage redoes it). The AUDIO-donor
    sync gate compares two release files, so its mismatch is a different cut regardless
    of where the RPU came from."""
    if res.ok:
        return True, res.reason
    r = res.reason or ""
    low = r.lower()
    if "audio donor is a different cut" in low or \
            (real_rpu_donor and ("frame mismatch" in low or "fps mismatch" in low)):
        return False, "permanent: companion is a different cut — " + r
    return False, r


def _remux(p, abort, progress=None, should_pause=None):
    """mute DV video -> PEAK-CAPPED DV video (dvcap x265 re-encode, hard `max_peak_mbps`
    ceiling — see remux.py; NO uncapped fallback) + audio (CFR file) + subtitles (ORIGINAL
    download) -> final master. Audio comes from the CFR file — the DV video derives from the
    CFR timeline, so that audio is guaranteed in sync; subtitles come from the original because
    the CFR pass no longer carries them. remux() dispatches on p.final's extension: MKV
    (lossless audio / bitmap subs) or MP4 (default). The x265 pass makes this stage LONG
    (~an hour per episode) — it reports live progress and honours abort."""
    import dvcap
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
    def _on_step(label, pct):
        # LABELED STEP progress for the fast (stream-copy) remuxes (user-asked): one bar
        # at a time — the current step's own percentage where a phase has a watchable
        # output file, label-only (pct None) where it doesn't.
        if progress:
            progress({"stage": "remux", "ep": p.ep,
                      "pct": (round(pct, 1) if pct is not None else None), "step": label})

    encode_source = None   # set when rpu-only loses its stream-copy privilege (peak-gated)
    combine_call = None    # set when a COMBINE item needs the capped path (notched bar below)
    if p.combine:
        # COMPANION COMBINE: the confirmed verdict names the donors; the peak gate picks
        # the path. Under the 72 Mbps gate the winner's own bits ship (RPU grafted or
        # converted as needed — remux_inject machinery); over it, the enforced-VBV capped
        # re-encode runs with the SAME RPU (real DV survives a re-encode and still beats
        # Resolve DV, user-dictated).
        import companion as companion_mod
        from orchestrator import combine_winner_path
        cv = companion_mod.confirmed_verdict(p.source_basename)
        winner = combine_winner_path(p)
        if not cv or not winner:
            return False, "permanent: combine verdict lost — re-pair the companion"
        v = cv["verdict"]
        side = {"nas": p.source, "remote": p.companion_src}
        real_rpu = v.get("rpu_from") != "resolve"
        combine_call = {
            "winner": winner,
            "rpu_source": side[v["rpu_from"]] if real_rpu else p.dv_render,
            "audio_source": side.get(v.get("audio_from")) or winner,
            "rpu_inline": bool(v.get("rpu_inline")),
            "rpu_profile": (v.get("rpu_profile") or "") if real_rpu else "",
            "real_rpu": real_rpu,
        }
        _on_step("measuring source peaks", None)
        peak = _peak_of(winner, os.path.basename(winner))
        if peak is None:
            return False, "could not measure source peaks — retrying (never ship unverified)"
        if peak <= dvcap.RPU_SHIP_VIDEO_GATE_MBPS:
            res = remux.combine(winner, combine_call["rpu_source"],
                                combine_call["audio_source"], p.final,
                                rpu_inline=combine_call["rpu_inline"],
                                rpu_profile=combine_call["rpu_profile"], capped=False,
                                cap_mbps=cap, audio_target_lufs=lufs, abort=abort,
                                on_step=_on_step)
            return _combine_result(res, combine_call["real_rpu"])
        logbook.event(f"remux {p.ep}: winner peaks {peak:.1f} Mbps — over the "
                      f"{dvcap.RPU_SHIP_VIDEO_GATE_MBPS} Mbps DV ship gate; combine takes "
                      "the capped re-encode (real DV preserved)")
        _on_step("planning scene-cut segments", None)
        _plan_fast_path_bounds(p, src=winner)   # cached in the bounds book — retries free
    elif plan.plan_for(p.source).get("topaz") == "rpu-only":
        # FAST PATH (HDR10 keep-the-source), PEAK-GATED (user-dictated 2026-08-06): the bare
        # HDR10 source direct-played fine, but injecting the RPU arms the SHIELD's DV decode
        # ceiling — ~80 Mbps WHOLE-STREAM, and every output is budgeted as if it carries
        # TrueHD (~8 Mbps above the video; lossless audio alone tipped Spider-Man NWH over,
        # re-confirmed by Doctor Strange). Video peaks at/under the 72 Mbps gate ship as the
        # ORIGINAL stream with the RPU injected; over it, the stream-copy privilege is
        # revoked and the SOURCE video takes the same enforced-VBV x265 native-DV capped
        # re-encode as everything else (the cap must bind — an uncapped CRF pass can't
        # promise the ceiling).
        _on_step("measuring source peaks", None)
        peak = _source_peak_mbps(p)
        if peak is None:
            return False, "could not measure source peaks — retrying (never ship unverified)"
        if peak <= dvcap.RPU_SHIP_VIDEO_GATE_MBPS:
            res = remux.remux_inject(p.dv_render, p.source_cfr, p.source, p.final,
                                     audio_target_lufs=lufs, abort=abort, on_step=_on_step)
            return res.ok, res.reason
        logbook.event(f"remux {p.ep}: source peaks {peak:.1f} Mbps — over the "
                      f"{dvcap.RPU_SHIP_VIDEO_GATE_MBPS} Mbps DV ship gate "
                      f"({dvcap.DV_STREAM_CEILING_MBPS} ceiling - TrueHD headroom); "
                      "stream copy revoked, capped re-encode of the source instead")
        _on_step("planning scene-cut segments", None)
        _plan_fast_path_bounds(p)      # cached in the bounds book — retries are free
        encode_source = p.source_cfr   # RPU from the render; VIDEO re-encoded from the source
    elif p.youtube:
        # FAST YOUTUBE REMUX (user-asked): the render targets a cap-safe bitrate, so ship
        # its video untouched when the peak gate agrees — minutes instead of an hour.
        # "render-over-cap" (and only that) falls through to the normal capped re-encode.
        res = remux.remux_ship_render(p.dv_render, p.source_cfr, p.source, p.final,
                                      cap_mbps=cap, audio_target_lufs=lufs, abort=abort,
                                      on_step=_on_step)
        if res.ok or not str(res.reason).startswith("render-over-cap"):
            return res.ok, res.reason
        logbook.event(f"remux {p.ep}: {res.reason}")
    bounds = _read_topaz_bounds(p.source_basename) or None   # segment at this episode's topaz boundaries

    # Same notched segment bar as Topaz: on_plan gives the cumulative segment-end frames once,
    # then each progress tick derives notches (each end as a 0..1 fraction) + seg_done (how many
    # ends the cumulative frame count has passed — drives the little flash) + seg_total.
    seg_plan = {"ends": None, "total": None}
    def _on_plan(ends, exact_total):
        seg_plan["ends"], seg_plan["total"] = ends, exact_total

    def _prog(frames, total, **extra):
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
        d.update(extra)
        progress(d)

    def _on_repair(k, of, seg_index, done, seg_frames):
        # The peak-repair pass runs AFTER the bar hit 100% — without its own tick the
        # lane read as stuck at "remux · 100%" for the whole re-encode (user-caught).
        # Full-bar frames + which segment is being re-capped + its LIVE frame progress,
        # so the UI can cut that segment out of the bar and refill it as it re-encodes.
        # Keys self-clear on the next ordinary progress event (the setters null absent keys).
        t = seg_plan["total"] or 0
        _prog(t, t, repair_seg=seg_index + 1, repair_k=k, repair_of=of,
              repair_done=done, repair_total=seg_frames)

    if combine_call:
        res = remux.combine(combine_call["winner"], combine_call["rpu_source"],
                            combine_call["audio_source"], p.final,
                            rpu_inline=combine_call["rpu_inline"],
                            rpu_profile=combine_call["rpu_profile"], capped=True,
                            cap_mbps=cap, boundaries=bounds, audio_target_lufs=lufs,
                            abort=abort, on_progress=_prog, on_plan=_on_plan,
                            should_pause=should_pause, on_repair=_on_repair)
        return _combine_result(res, combine_call["real_rpu"])
    res = remux.remux(p.dv_render, p.source_cfr, p.source, p.final,
                      cap_mbps=cap, audio_target_lufs=lufs, boundaries=bounds, abort=abort,
                      on_progress=_prog, on_plan=_on_plan, should_pause=should_pause,
                      on_repair=_on_repair, encode_source=encode_source)
    return res.ok, res.reason


def _upload(p, abort, progress=None):
    """final master -> NAS library (FTP STOR, owner 1000:10, size-verified), then the
    per-item `replace_source` setting decides the source's fate (user-dictated,
    2026-07-17): ON (default) = delete the superseded source once the master verifies
    (transfer.replace_original guards on size); OFF = keep it beside the master (Plex
    merges both into one item with two versions and serves the 4K; the source stays as
    the re-run option for future upscale models). The 4K keeps the HDR10 DV name
    (queue done-detection keys on that mark either way)."""
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
    import settings as settings_mod
    if settings_mod.get_show_replace_source(p.series):
        rok, rmsg = transfer.replace_original(remote, p.nas_source, p.final)
        return True, f"{reason}; {rmsg}"
    return True, f"{reason}; source kept (Plex serves the 4K version)"


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
        if os.path.isdir(mezzanine_path(p.source) + ".segments"):   # its resumable chunk dir
            shutil.rmtree(mezzanine_path(p.source) + ".segments", ignore_errors=True)
        if os.path.exists(p.final + ".ship.hevc"):      # ship-render temp: the render's ES
            os.remove(p.final + ".ship.hevc")
        # A hard kill during the mux can strand remux.mp4box_safe_input's hardlink — and a
        # leftover link keeps the multi-GB transient it points at ALIVE (second reference
        # to the same inode), so sweep any that share this item's directory.
        import glob
        for stray in glob.glob(os.path.join(os.path.dirname(p.final) or ".", "_mp4box_*")):
            try: os.remove(stray)
            except OSError: pass
    except OSError:
        pass
    shutil.rmtree(p.segdir, ignore_errors=True)          # the resumable-encode chunks (topaz output)
    shutil.rmtree(p.final + ".remuxsegs", ignore_errors=True)   # the resumable REMUX segments + rpu
    if getattr(p, "source_wide", ""):                    # the extend stage's resumable chunk dir
        shutil.rmtree(p.source_wide + ".segments", ignore_errors=True)
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
