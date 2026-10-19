"""Heavy stage runners for the orchestrator. Each returns (ok, message).

Every stage runs fully unattended, with no screen. The Dolby Vision step ("dv" in the
orchestrator's stage list) is one of two: 'dvnative', the chunk lane's analysis for an eligible
TV episode (see _dvnative), or 'dvrender', the native render that writes p.dv_render for every
other route (see _dvrender).
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
import stagebar
import transfer
from orchestrator import _is_dv81, _vstream, _has_audio, _first_video, _remote_size

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))


FFPROBE = "/opt/homebrew/bin/ffprobe"
# The lettered stages (stagebar) of the steps that run as a sequence of jobs
DOWNLOAD_STAGES = ("Copying the source from the NAS", "Making the constant-frame-rate copy")
COMBINE_STAGES = ("Copying the NAS copy", "Fetching the companion from the seedbox")
PEAKS_STAGE = "measuring source peaks"          # a fast remux's labelled steps (remux.inject_stages)
PLAN_STAGE = "planning scene-cut segments"
CAPPED_STAGE = "re-encoding under the peak cap"
EXPORT_BITRATE_FLOOR_KBPS = 60000   # render preset's default; the export matches the intake above this
YOUTUBE_NATIVE_4K_MIN_HEIGHT = 1700  # above this the source IS 4K, not a 1080p upscale
YOUTUBE_RENDER_KBPS_4K = 14000       # ...and a NATIVE 4K source renders lower (user-dictated
                                     # 2026-08-18). The 20 Mbps below is tuned for the normal
                                     # case — a 1080p web source the render upscales. Real 4K
                                     # detail is far harder to encode, so at the same target
                                     # its peaks run much higher: measured live, an 18-minute
                                     # native-4K video rendered at 20 Mbps peaked at 59.9 and
                                     # lost the gate ("render-over-cap: 59.9 > 50"), costing
                                     # the whole hour the fast path exists to save. At 14 the
                                     # same ~3x burst ratio projects ~42 Mbps — inside the 50
                                     # cap with room to spare, so these ship stream-copied
                                     # like every other video instead of taking the
                                     # hour-class capped re-encode.
YOUTUBE_RENDER_KBPS = 20000         # YouTube renders target THIS instead of the floor, so the render
                                    # IS the deliverable: its 1-s peaks land under the cap, which lets
                                    # the remux SHIP its video stream-copied (remux_ship_render) in
                                    # minutes instead of an hour-class x265 re-encode.
                                    # WHY 20 and not 35 (lowered 2026-08-17, user-dictated: the
                                    # renders "should loosely be enough to function as the final
                                    # thing"): the whole scheme only pays off if the peak gate actually
                                    # PASSES, and a VideoToolbox export's peaks run several times its
                                    # target (measured ~139 Mbps on a ~27 Mbps average). At 35 the gate
                                    # was a coin flip, and losing it costs the entire hour the fast path
                                    # was meant to save. 20 Mbps of 4K HEVC is still generous for video
                                    # UPSCALED FROM 1080p web sources (YouTube skips Topaz — the render
                                    # does the scaling — so this is the only encode the picture ever
                                    # gets, and its input is already a lossy 1080p web file).
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


def _forget_topaz_bounds(basename: str) -> None:
    """Drop an item's scene-plan frame bounds (they are re-planned from the new file)."""
    try:
        with _SEGBOUNDS_LOCK:
            try:
                with open(_SEGBOUNDS_FILE) as f:
                    d = json.load(f)
            except (OSError, ValueError):
                return
            if not isinstance(d, dict) or basename not in d:
                return
            del d[basename]
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


def _settings_scope(p) -> str:
    """The key an item's settings live under. p.series for everything — TV series name,
    movie title, channel folder — EXCEPT a YouTube video that belongs to an import batch,
    whose settings are the BATCH's ("import:<id>"): an imported playlist is its own
    configurable thing, like a queued channel, and must not silently inherit whatever
    channel folder its files land in on staging (user-asked 2026-08-28). Falls back to
    p.series whenever the lookup finds nothing, so nothing else changes."""
    if getattr(p, "youtube", False):
        try:
            import youtube
            scope = youtube.settings_scope_for_vid(youtube.video_id(p.source_basename))
            if scope:
                return scope
        except Exception:
            pass
    return p.series


def _plan_fast_path_bounds(p, progress=None, src=None) -> str:
    """The DV-only fast path ("dv-only" in the plan): no upscale, but the capped remux still
    re-encodes — so run
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
                                 capture_output=True, text=True, timeout=30).stdout
            out = ((out or "").strip().splitlines() or [""])[0].split(",")[0].strip()  # ffprobe 8
            if out and out not in ("N/A", "0"):                       # trails a comma on side data
                return int(int(out) / 1000)
        except Exception:
            pass
    return 0


def run_stage(stage, p, *, abort=None, progress=None, low_prio=False, should_pause=None):
    fn = {
        "download": _download, "extend": _extend, "topaz": _topaz, "dvnative": _dvnative, "dvrender": _dvrender, "remux": _remux, "upload": _upload,
        "cleanup": _cleanup,
    }.get(stage, lambda *_a, **_k: (False, f"unknown stage {stage}"))
    ep = getattr(p, "ep", "?")
    try:
        # `should_pause` = yield at the next segment boundary: topaz yields to two live
        # remuxes; a remux yields while AI outpainting has the machine. Both are segmented, so a pause costs nothing — the retry resumes in place. The
        # native DV analysis ('dvnative', run thread only) is per-chunk resumable too: a
        # pause there loses at most the chunk in hand; the native render ('dvrender') yields
        # only inside its analysis, range by range (see _dvrender).
        if stage in ("topaz", "remux", "dvnative", "dvrender"):
            ok, msg = fn(p, abort, progress, should_pause)
        elif stage == "download":
            # download yields too — to a sent video only (see _download); the orchestrator
            # hands it a narrower predicate than topaz gets
            ok, msg = fn(p, abort, progress, low_prio, should_pause)
        else:
            ok, msg = fn(p, abort, progress)
    except Exception as e:                       # any stage bug is logged, never silent
        logbook.exception(f"{stage} {ep}", e)
        return False, f"{stage} crashed: {e.__class__.__name__}: {e}"
    # A clean segment-boundary pause is NOT a failure — log it as an event so it never
    # shows up in the red "Recent issues" banner. Same for a "permanent:" refusal
    # (already-DV source): expected behavior, and the park logs its own single line.
    # "interrupted:" is the third benign class: WE stopped the work (a stop/deploy, a Plex
    # stream starting, the run thread claiming an item the prefetcher was mid-way through).
    # Nothing is wrong with the item and the next attempt just redoes it — but it was logged
    # as a red FAIL, so one long video accumulated six "CFR convert failed" lines in an
    # evening and read like a broken file (user-caught 2026-09-04). Kept distinct from
    # "paused:", which the run loop treats as a segment-boundary YIELD and does bookkeeping for.
    benign = (not ok) and (str(msg).startswith("paused:") or str(msg).startswith("permanent:")
                           or str(msg).startswith("interrupted:"))
    (logbook.event if (ok or benign) else logbook.failure)(f"{stage} {ep}: {msg}")
    return ok, msg


def cfr_failure_message(error_tail) -> str:
    """PURE. How a failed CFR convert should be REPORTED.

    to_cfr already tells our own kill apart from a content failure (a negative rc is always
    a signal we sent — a stop/deploy, a Plex stream starting, the run thread claiming an item
    the prefetcher was part-way through). That distinction was being thrown away, so routine
    interruptions logged as red "CFR convert failed" — one long video collected six of them
    in an evening and read like a broken file (user-caught 2026-09-04).

    "interrupted:" is benign in run_stage. Deliberately NOT "paused:", which the run loop
    treats as a segment-boundary yield and does livelock bookkeeping for."""
    if str(error_tail) == "aborted":
        return "interrupted: CFR convert stopped — the next attempt redoes it"
    return f"CFR convert failed: {_err_tail(error_tail)}"


class _PauseAbort:
    """Abort stand-in for the download stage: `is_set()` is the real abort OR a throttled
    should_pause(). Both the FTP pull and to_cfr only ever poll `.is_set()`, so this slots
    in without touching them. Remembers WHICH one fired, so the stage can report a benign
    "paused:" (re-selected later, resumed) instead of an abort."""
    POLL_SECONDS = 3.0

    def __init__(self, abort, should_pause):
        self._abort, self._sp = abort, should_pause
        self._next, self.paused = 0.0, False

    def is_set(self) -> bool:
        if self._abort is not None and self._abort.is_set():
            return True
        if self.paused:
            return True
        now = time.monotonic()
        if now >= self._next:
            self._next = now + self.POLL_SECONDS
            try:
                if self._sp():
                    self.paused = True
                    return True
            except Exception:
                pass
        return False


def _download(p, abort, progress=None, low_prio=False, should_pause=None):
    """The download stage: NAS pull + CFR (see _download_body), which YIELDS to a
    send-to-Visionary video that is ready to run (user-dictated 2026-09-05: a send runs
    after the current segment, not the current episode). Neither the pull nor the CFR is
    segmented, so "the current segment" here is "now": the partial source is dropped (the
    body already does that on any stop) and the CFR is redone on the next attempt — the
    same cost as any interruption, spent deliberately. Reported as a benign "paused:" so
    the run loop holds and re-selects rather than counting a failure."""
    pa = _PauseAbort(abort, should_pause) if should_pause is not None else None
    ok, msg = _download_body(p, pa if pa is not None else abort, progress, low_prio)
    if not ok and pa is not None and pa.paused:
        return False, "paused: a video you sent is ready — this download resumes after it"
    return ok, msg


def _download_body(p, abort, progress=None, low_prio=False):
    """NAS source -> scratch (FTP), THEN a constant-frame-rate pass. (Wrapped by _download,
    which adds the yield-to-a-sent-video behaviour; this body is unchanged.)

    Reuse an on-disk source ONLY if it's verified complete (size == the NAS file). A
    partial left by a stopped/interrupted download is deleted and re-pulled — never
    reused, since a short file surfaces later as a corrupt 'moov atom not found'. And if
    THIS download is aborted (run stopped mid-transfer) or fails, the partial is removed
    so the next pass starts clean instead of treating the stub as a finished source.

    Then convert the verified source to CONSTANT frame rate at its own rate (topaz.to_cfr)
    into p.source_cfr — a variable frame rate is what made the downstream frame counts
    drift (header vs decoder disagreeing), breaking the Topaz last-chunk count and the
    render's length check. p.source_cfr is what Topaz, the DV step and the remux all read; the original stays so the size check still proves the download finished.
    stage_done('download') requires BOTH, so an interrupted CFR pass just resumes here."""
    if p.combine:
        return _download_combine(p, abort, progress)
    have_source = False
    if os.path.exists(p.source):
        state = _source_complete(p)
        if state is True:                      # size matches the NAS file → reuse it
            have_source = True
        elif state is False:
            try: os.remove(p.source)           # verified INCOMPLETE → re-pull clean
            except OSError: pass
        else:
            # UNVERIFIABLE (NAS unreachable). "Don't destroy it" was right; "reuse it" was
            # not. A hard kill (panic, power cut, forced quit) leaves a partial behind, and
            # if the next arm can't reach the NAS — Tailscale still coming up, smbftpd at
            # its connection cap — this promoted the stub to a complete source. NOTHING
            # downstream would catch it: the CFR is built from the fragment, the render is
            # judged against that same short CFR, the remux only checks internal
            # consistency, upload verifies the master against itself, and replace_source
            # (default ON) then deletes the real source. A truncated master and a deleted
            # original, every stage reporting success. Keep the file, refuse to trust it:
            # a retryable failure re-verifies the moment the NAS answers.
            return False, (f"{_nas_down_word()} — cannot verify the local source is complete; "
                           "not reusing it (it is kept for the next attempt)")
    if not have_source:
        on_prog = None
        if progress:
            def on_prog(done, total):          # the pull is the first ~20% of the stage bar
                progress(dict({"stage": "download", "ep": p.ep, "pct": round(done / total * 20)},
                              **stagebar.fields(DOWNLOAD_STAGES, 0, done / total * 100)))
        ok, _local, reason = transfer.download(p.nas_source, os.path.dirname(p.source),
                                               on_progress=on_prog, abort=abort,
                                               on_wait=_slot_note(progress, "download", p.ep,
                                                                  stagebar.fields(DOWNLOAD_STAGES,
                                                                                  0, 0.0)),
                                               # the download-ahead steps aside for the pipeline
                                               prio=(transfer.BACKGROUND if low_prio
                                                     else transfer.FOREGROUND))
        # Anything at p.source after a stopped/failed transfer is a partial stub — drop it so
        # it can never be mistaken for a finished source (the exact break: quitting mid-download).
        if not ok:
            if os.path.exists(p.source):
                try: os.remove(p.source)
                except OSError: pass
            return ok, _stopped_is_interrupted(reason)
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
    (combine never feeds Topaz; the remux and the DV render read the originals directly)."""
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
    # The classic path's rule (_download_body): reuse a local NAS copy only when it is VERIFIED
    # complete. "Not verified incomplete" let an unverifiable partial — a kill mid-pull, then an
    # unreachable or overloaded NAS — through as a finished source (review 2026-10-06).
    state = _source_complete(p) if os.path.exists(p.source) else False
    if state is None:
        return False, (f"{_nas_down_word()} — cannot verify the local NAS copy is complete; "
                       "not reusing it (it is kept for the next attempt)")
    have_source = state is True
    if os.path.exists(p.source) and not have_source:
        try: os.remove(p.source)               # verified INCOMPLETE → re-pull clean
        except OSError: pass
    if not have_source:
        on_prog = None
        if progress:
            def on_prog(done, total):          # NAS pull = the front half of the bar
                progress(dict({"stage": "download", "ep": p.ep, "pct": round(done / total * 50)},
                              **stagebar.fields(COMBINE_STAGES, 0, done / total * 100)))
        ok, _local, reason = transfer.download(p.nas_source, os.path.dirname(p.source),
                                               on_progress=on_prog, abort=abort,
                                               on_wait=_slot_note(progress, "download", p.ep,
                                                                  stagebar.fields(COMBINE_STAGES,
                                                                                  0, 0.0)))
        if not ok:
            if os.path.exists(p.source):
                try: os.remove(p.source)
                except OSError: pass
            return ok, _stopped_is_interrupted(reason)
    # -- companion (relay-streamed from the seedbox) --------------------------------
    if progress:
        progress(dict({"stage": "download", "ep": p.ep, "pct": 50, "step": "fetching companion"},
                      **stagebar.fields(COMBINE_STAGES, 1, 0.0)))
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
            progress(dict({"stage": "download", "ep": p.ep,
                           "pct": 50 + round(done / total * 50), "step": "fetching companion"},
                          **stagebar.fields(COMBINE_STAGES, 1, done / total * 100)))
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
    # FAST-PATH items (rpu-only / dv-only) skip Topaz, so they don't need the true-CFR
    # re-encode that keeps Topaz's frame counts stable — this file only carries their AUDIO
    # (a bit-copy of the original's either way) and decodable frames for scene-cut planning.
    # Stream-copy instead: minutes and disk-bound rather than hours of libx264 on a 4K
    # movie whose video bytes nothing downstream reads (live-caught: a 60 GB REMUX).
    pl = plan.plan_for(p.source)
    fast = pl.get("topaz") in ("rpu-only", "dv-only")
    # ...except a VARIABLE frame rate source, which takes the fast path too (no 4K source goes
    # through Topaz, user-dictated 2026-09-30) and so must really be RE-TIMED here: the DV render
    # reads this file and counts its frames at one rate. For 4K that is the
    # hardware HEVC re-encode at CFR_HW_KBPS — the same one the old Topaz clean pass started from.
    copy_only = fast and pl.get("source_cfr", True)
    on_prog = None
    if progress:
        total = topaz.total_frames(p.source) or 0
        def on_prog(frames):
            pct = 20 + round(frames / total * 79) if total else None
            progress(dict({"stage": "download", "ep": p.ep, "pct": min(99, pct) if pct else None},
                          **stagebar.fields(DOWNLOAD_STAGES, 1,
                                            min(99.0, frames / total * 100) if total else None)))
    res = topaz.to_cfr(p.source, p.source_cfr, abort=abort, on_progress=on_prog,
                       low_prio=low_prio, copy_only=copy_only)
    if not res.ok:
        return False, cfr_failure_message(res.error_tail)
    if res.capped_secs:
        # Worth saying out loud: the source's container ran past its own picture, so the CFR
        # was bounded to the picture. Unbounded, every later stage would count the CONTAINER's
        # length and minutes of nothing would land on the end (see topaz.cfr_duration_cap).
        logbook.event(f"download {p.ep}: container runs past the picture — "
                      f"CFR capped at {res.capped_secs:.1f}s")
    return True, f"downloaded + CFR @ {res.rate} ({res.frames} frames)"


def _stopped_is_interrupted(reason):
    """A download WE stopped — a stop, a Plex stream starting, or the download-ahead stepping aside
    for the pipeline's own transfer (transfer.nas_slot) — is "interrupted:", the benign class
    run_stage logs as an event, not a red failure line."""
    r = str(reason or "")
    return ("interrupted: " + r) if r.startswith("aborted") else reason


def _slot_note(progress, stage, ep, extra=None):
    """`on_wait` for a pipeline transfer: put "waiting for another app's NAS transfer" on the
    stage's own surface while the shared NAS transfer slot is taken, and take it off again.
    `extra` rides along on each note (the download's stage fields)."""
    if not progress:
        return None

    def note(text):
        info = dict({"stage": stage, "ep": ep, "pct": 0}, **(extra or {}))
        if text:
            info["waiting"] = text
        progress(info)
    return note


def _nas_down_word() -> str:
    """"NAS overloaded" while transfer's login breaker is open — the orchestrator never charges an
    item for that (Orchestrator._overload_fault) — else "NAS unreachable"."""
    return "NAS overloaded" if transfer.nas_busy() else "NAS unreachable"


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
        # CONTINUITY (user-dictated 2026-08-10): the per-show WING PROMPT (tier 1 — a
        # gentle style bias at cfg 1.0) and scene-aware chunking (tier 2 — boundaries
        # snap to scene cuts so wing resets coincide with edits, and chunks within one
        # scene share a seed so their inventions converge).
        import settings as settings_mod
        try:
            eff_prompt = (settings_mod.get_show_extend_prompt(p.series) or "").strip()
        except Exception:
            eff_prompt = ""
        eff_prompt = eff_prompt or borders.DEFAULT_PROMPT
        segdir = p.source_wide + ".segments"
        os.makedirs(segdir, exist_ok=True)        # the scene cache lives inside it
        try:
            cuts = [int(c) for c in (topaz._cached_scene_frames(p.source_cfr, segdir,
                                                                fps) or [])]
        except Exception:
            cuts = []                             # no cuts → plain fixed windows
        chunks = borders.plan_chunks_snapped(total, cuts)
        # Resume identity: any change to the source, geometry, models, sampler, seed,
        # PROMPT, or the chunk PLAN itself (scene detection moved a boundary) invalidates
        # the persisted chunks (same contract as the remux's segdir).
        import zlib
        try:
            st = os.stat(p.source_cfr)
            src_id = f"{st.st_size}:{int(st.st_mtime)}"
        except OSError:
            src_id = "missing"
        seed_base = zlib.crc32(p.source_basename.encode("utf-8")) & 0x7FFFFFFF
        manifest = {"src": src_id, "chunk": borders.CHUNK_FRAMES, "geom": geom,
                    "models": sorted(borders.MODELS),
                    "sampler": [borders.STEPS, borders.CFG, borders.SAMPLER,
                                borders.SCHEDULER, borders.SHIFT],
                    "seed": seed_base, "prompt": eff_prompt,
                    "plan": [[s, n] for (s, n) in chunks]}
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
    # SET-REFERENCE BOOK (continuity tier 3): per scene, recognize the set (dHash of a
    # probe frame vs the show's persisted book) and feed its remembered widened frame
    # into WanVaceToVideo's reference_image — the model then keeps inventing THE SAME
    # wings for that set, within the episode and across every future one. An unknown
    # set generates its first chunk reference-free, REGISTERS the result, and the rest
    # of the scene (and all future matches) chain off it. Every failure path degrades
    # to reference-free generation. The book is deliberately NOT in the resume manifest:
    # it only ever grows, matching is deterministic, and completed chunks never re-run —
    # a resume reuses exactly the references the failed attempt persisted. KNOWN MINOR:
    # a resume that lands mid-scene re-probes from a LATER chunk's frame — if that frame
    # hashes far from the scene's original probe (wide shot vs closeup), the rest of the
    # scene may generate reference-free or register a second entry for the set. Rare
    # (needs a deploy/crash mid-scene), self-limiting (the book match is re-tried per
    # scene), and always fail-open.
    set_book = borders.load_set_book(p.series)
    scene_refs: dict = {}       # scene -> reference filename in in_dir (None = none yet)
    scene_hash: dict = {}       # scene -> the probe frame's dHash (for registration)

    def _scene_reference(sc, chunk_in, n):
        """Look up this scene's reference (cached). Never raises."""
        if sc in scene_refs:
            return scene_refs[sc]
        ref_name = None
        try:
            probe = os.path.join(backend.in_dir, f"probe_{sc:04d}.png")
            r = subprocess.run(borders.extract_frame_cmd(chunk_in, probe, n // 2),
                               capture_output=True, text=True, timeout=120)
            h = borders.dhash_file(probe) if r.returncode == 0 else None
            scene_hash[sc] = h
            m = borders.match_set(set_book, h)
            if m:
                # Copy FIRST, name after: the book PNG can vanish between the stage-start
                # load and this moment (a Reset mid-run, hours later) — a failed copy
                # must degrade to reference-free, never cache a phantom filename that
                # ComfyUI's LoadImage validation would reject on every chunk of the scene.
                dst = f"setref_{sc:04d}.png"
                shutil.copyfile(m["path"], os.path.join(backend.in_dir, dst))
                ref_name = dst
            try:
                os.remove(probe)
            except OSError:
                pass
        except Exception:
            scene_hash.setdefault(sc, None)
        scene_refs[sc] = ref_name
        return ref_name

    def _register_scene(sc, gen, n):
        """A new set's first chunk just generated reference-free: remember its widened
        canvas frame so the REST of the scene (and every future episode's matches)
        reproduces the same wings. Never raises."""
        nonlocal set_book
        try:
            if scene_hash.get(sc) is None:
                return
            # Re-match against the CURRENT book first: if an earlier attempt registered
            # this set but its in_dir copy failed (ENOSPC, review-caught), the entry
            # already exists — reuse it rather than appending a duplicate per chunk.
            entry = borders.match_set(borders.load_set_book(p.series), scene_hash[sc])
            canvas = os.path.join(backend.out_dir, f"canvas_{sc:04d}.png")
            if entry is None:
                r = subprocess.run(borders.extract_frame_cmd(gen, canvas, n // 2),
                                   capture_output=True, text=True, timeout=120)
                entry = (borders.register_set(p.series, scene_hash[sc], canvas)
                         if r.returncode == 0 else None)
            try:
                os.remove(canvas)
            except OSError:
                pass
            if entry:
                set_book = borders.load_set_book(p.series)
                ref_name = f"setref_{sc:04d}.png"
                shutil.copyfile(entry["path"], os.path.join(backend.in_dir, ref_name))
                scene_refs[sc] = ref_name
        except Exception:
            pass

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
                                                         n, geom, fps=float(fps)),
                               capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                return False, "border extend: chunk extract failed — " + _err_tail(r.stderr)
            sc = borders.scene_of(start, cuts)
            graph = borders.build_outpaint_graph(
                input_name=os.path.basename(chunk_in),
                canvas_w=geom["canvas_w"], canvas_h=geom["canvas_h"],
                pad_left=geom["pad_work"], pad_right=geom["pad_work"],
                length=n, fps=float(fps),
                seed=seed_base + sc,                              # per-SCENE, not per-chunk
                filename_prefix=f"gen_{k:04d}", prompt=eff_prompt,
                reference_name=_scene_reference(sc, chunk_in, n))
            hist = client.wait(client.submit(graph), backend=backend, abort=abort)
            gen = borders.ComfyClient.output_file(hist, backend.out_dir)
            if not gen or not os.path.exists(gen):
                return False, f"border extend: ComfyUI produced no output for chunk {k}"
            part = wide_k + ".part.mp4"                  # atomic: rename marks the chunk done
            r = subprocess.run(borders.composite_cmd(p.source_cfr, gen, part, start, n,
                                                     geom, fps=float(fps)),
                               capture_output=True, text=True, timeout=1800)
            got = borders.count_frames(part) if r.returncode == 0 else 0
            if got != n:
                try: os.remove(part)
                except OSError: pass
                return False, (f"border extend: composite failed for chunk {k} — "
                               + _err_tail(r.stderr or f"{got}/{n} frames"))
            os.replace(part, wide_k)
            if scene_refs.get(sc) is None:               # a NEW set: remember its wings
                _register_scene(sc, gen, n)
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


def _discard_wrong_clock_input(p, src_in, why):
    """Topaz refused its input because the file declares a frame rate its frames do not run
    at (topaz.declared_rate_mismatch). The usual origin is a CFR that an older build
    stream-copied from such a source. It can never pass, and nothing planned on its clock is
    sound, so drop the input and the segdir. The next pass finds the file missing and rebuilds
    it: the download stage re-encodes the CFR at the frames' own rate, and the extend stage
    re-makes a border-extended copy from that. Counting one failure for it is fine; looping on
    it cost The Adventures of Sharkboy and Lavagirl five 44-minute retries (2026-09-06)."""
    import shutil
    try:
        if src_in and os.path.exists(src_in):
            os.remove(src_in)
    except OSError:
        pass
    shutil.rmtree(p.segdir, ignore_errors=True)
    return f"{why} — discarded it and its segments; the next pass rebuilds it on that clock"


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
    if pl["topaz"] in ("rpu-only", "dv-only"):
        # HIGH-BITRATE 4K FAST PATH: the source picture IS the deliverable — no upscale.
        # Succeed as a no-op so the run loop proceeds straight to the DV step.
        # dv-only still runs topaz's scene-cut PLANNING so its capped remux segments
        # at scene cuts (rpu-only plans LAZILY in the remux stage instead — it only needs
        # segments at all when the peak gate revokes its stream copy).
        note = _plan_fast_path_bounds(p, progress) if pl["topaz"] == "dv-only" else ""
        return True, pl["reason"] + " — skipping upscale" + note
    if p.youtube:
        # YOUTUBE SKIPS TOPAZ (user-dictated 2026-08-06): the native DV render does the scaling
        # (the scheduler reads this same fact via orchestrator.topaz_is_noop — keep in step) —
        # a Lanczos fit into 3840x2160 (user-chosen 2026-10-18 over MetalFX in a blind A/B).
        # Scene-cut planning still runs so the capped remux segments at real cuts.
        note = _plan_fast_path_bounds(p, progress)
        route = _native_render_route(p)
        return True, f"YouTube → the native DV render ({route or 'its route'}) — skipping Topaz" + note
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
    # is what got stuck); the chunks STAY as separate files and the DV step reads them in
    # order. The segdir is the topaz OUTPUT + the resume checkpoint — KEPT on
    # interruption, removed only at cleanup. stage_done("topaz") checks the manifest.
    res = topaz.upscale_resumable(src_in, segdir=p.segdir, profile=params, scale=pl["scale"],
                                  fit_height=pl.get("fit_height"), on_progress=on_progress, abort=abort,
                                  on_plan=on_plan, should_pause=should_pause)
    if not res.ok and str(res.error_tail).startswith("paused:"):
        return False, res.error_tail            # NOT a failure — held cleanly at a segment boundary
    if not res.ok and str(res.error_tail).startswith(topaz.RATE_MISMATCH):
        return False, _discard_wrong_clock_input(p, src_in, res.error_tail)
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


def _dvnative(p, abort, progress=None, should_pause=None):
    """NATIVE DOLBY VISION — the chunk lane: an eligible TV episode's DV analysis, hdr_analyzer_mvp
    over Topaz's ProRes chunks through the pinned SDR->PQ LUT, then dovi_tool's RPU (nativelane.run_analysis: chunk list -> 'analyzing' marker ->
    per-chunk analysis -> RPU -> 'ready' marker, published last). Run thread only, and only
    for an item orchestrator._native_slot routed here — never the screen, never a SIGSTOP:
    it runs at nice 10 beside the previous item's x265 tail.

    "paused: ..." (a yield, or the 150 GiB mid-analysis disk floor) and "interrupted: ..."
    (the run stopped) are not failures: the next pass resumes chunk by chunk. Every other
    failure is booked in native/failures.json; the MAX_FAILURES-th, or one the lane says no
    retry can fix (on a first claim: chunks or CFR that do not fit; on a resume: a changed
    analyzer or LUT, or a changed chunk set), writes native/fallback.json and comes back as
    "native-fallback: ..." — the orchestrator then sends the item to the native chunk render
    (dvrender) in the same pass, on the chunks the lane never touched. A broken TOOLCHAIN is
    never sticky: it claims nothing, and the item waits until it is fixed (the render needs the
    same toolchain)."""
    import nativelane
    try:
        ok, msg = nativelane.run_analysis(p, abort=abort, progress=progress,
                                          should_pause=should_pause)
    except Exception as e:      # a lane bug costs one native attempt — it must never loop the item
        logbook.exception(f"dvnative {getattr(p, 'ep', '?')}", e)
        ok, msg = False, f"native analysis crashed: {e.__class__.__name__}: {e}"
    msg = str(msg)
    if ok or msg.startswith("paused:"):
        return ok, msg
    if msg.startswith("aborted") or (abort is not None and abort.is_set()):
        return False, "interrupted: DV analysis stopped — the next pass resumes it chunk by chunk"
    try:
        n = nativelane.count_failure(p, msg)
        if nativelane.should_fall_back(msg, n):
            nativelane.write_fallback(p, msg)
            why = msg[len(nativelane.FALLBACK_PREFIX):].strip() \
                if msg.startswith(nativelane.FALLBACK_PREFIX) else msg
            return False, f"{nativelane.FALLBACK_PREFIX} {why} — the native render takes this item"
    except Exception as e:      # the books are files on a disk that may be full: never a crash
        logbook.exception(f"dvnative {getattr(p, 'ep', '?')} bookkeeping", e)
        return False, f"{msg} (native attempt not booked: {e.__class__.__name__})"
    return False, f"{msg} (native attempt {n} of {nativelane.MAX_FAILURES})"


# ---- the native DV render ('dvrender'): every route the chunk lane does not take -------------
# The routes the stage makes. yt_native4k and sdr4k take one source already at the master's
# 3840x2160, BT.709 limited-range SDR, straight through the pinned LUT (the TV lane's validated
# chain) — no front end at all; any other size or colour of theirs is refused with the reason.
# DVRENDER_FIT_ROUTES (yt_plain, yt_1080 — every SDR YouTube video under native 4K, and every
# vertical one) put the fit front end before the LUT (dvnative.fit_front_end): BT.601 or full
# range to BT.709 limited when the tags (or, untagged, the frame size) say so, then a Lanczos fit
# into 3840x2160 with a black pad. yt_1080 is a plain 2x Lanczos (user decision 2026-10-18: a
# blind A/B against MetalFX showed no difference).
# HDR INTAKE: DVRENDER_HDR_ROUTES take an HDR source with NO LUT (_hdr_render_spec).
#   rpu_only      4K HDR10 HEVC Main10 CFR: the source is measured as it is and keeps its own
#                 mastering (its mastering-display and content-light-level SEI become L6), and the
#                 render IS its own coded video with that RPU injected (keep_video: no encode) —
#                 so the remux's rpu-only branch ships remux_inject onto the original bits under
#                 the 72 Mbps gate, the capped x265 of the source (its own mastering display,
#                 encode_source) over it
#   hdr_reencode  PQ that cannot carry an RPU as it is (AV1, H.264, 8-bit, VFR): measured and
#                 VideoToolbox-encoded as it is, keeping its own mastering, at the export floor;
#                 the remux's capped x265 then reads the render
#   hlg           HLG: dvnative.hlg_to_pq_vf (BT.2100 HLG -> PQ at 1000 nits) in front, then as
#                 PQ, on the 1000-nit master
#   hdr_topaz     sub-4K PQ or HLG upscaled by Topaz: the render is made from Topaz's chunks (the
#                 native render's chunk input — per-chunk analysis, one concat encode), no LUT,
#                 the HLG front end for HLG, the source's own mastering for PQ; after it the chunks
#                 drop at hand-off and the remux's x265 reads the render. It is NOT the chunk lane
#                 (nativelane), which stays the SDR TV lane.
# The source keeps its own frame (any 4K geometry, as rpu_only's original stream always has) —
# except a YouTube video, whose render is 3840x2160: one of another size needs an HDR fit, which
# is not built, so it is refused with the reason.
# SDR TOPAZ CHUNKS: DVRENDER_SDR_CHUNK_ROUTES render from Topaz's chunks through
# dvnative.chunk_front_end (BT.601 -> BT.709 by the standard conversion when the chunk's tags — or,
# untagged, the source's frame — say so; square pixels for an anamorphic chunk; a 4:3 SD picture
# pillarboxed into 3840x2160) and the pinned LUT, at the intake's own bitrate floored at
# EXPORT_BITRATE_FLOOR_KBPS, no gate (_sdr_chunk_render_spec). The chunks drop at hand-off and the
# remux's x265 reads the render, which is what movie_sdr_topaz must keep (orchestrator: a movie's
# ~245 GB ProRes is never held through its remux).
#   movie_sdr_topaz   a sub-4K SDR movie (any bucket: a 480p DVD film gets the 601 front end)
#   tv_sdr_topaz,     a TV episode the chunk lane (nativelane, its first choice) passed on — for
#   tv_disk_fallback, disk, say, or fell back from (fallback.json) — never one it has claimed
#   sd_sdr_topaz
# COMBINE: combine_nodv — neither copy has real DV, so the verdict's WINNER is analysed
# (_combine_render_spec), exactly as rpu_only analyses its one source; companion.build_verdict
# already refuses a winner that is not PQ, so there is no SDR winner to route.
DVRENDER_HDR_ROUTES = ("rpu_only", "hdr_reencode", "hlg", "hdr_topaz")
DVRENDER_SDR_CHUNK_ROUTES = ("movie_sdr_topaz", "tv_sdr_topaz", "tv_disk_fallback", "sd_sdr_topaz")
DVRENDER_TV_CHUNK_ROUTES = ("tv_sdr_topaz", "tv_disk_fallback", "sd_sdr_topaz")   # the chunk lane's first
DVRENDER_CHUNK_ROUTES = ("hdr_topaz",) + DVRENDER_SDR_CHUNK_ROUTES   # from Topaz's chunks; the rest
                                                                     # from one file
# SDR OUTPUT: sdr_out — any item PINNED to SDR (user decision 2026-10-18: the SDR output mode
# stays). It makes the SDR master (_sdr_out_spec): the item is named by the route it would have unpinned (routes.item_route(p,
# mode="dv1000")) and rendered as that route's picture with NO LUT — Topaz's chunks through the
# chunk front end, a YouTube video through the fit front end, 4K SDR through the colour step
# alone — into a BT.709 HEVC render with no RPU (nativerender's sdr spec, its proof books/sdr.json),
# at the export bitrate and gate that route's render has. The remux then makes the TRUE SDR master
# (x265 Rec.709 under the cap, no DV: remux(sdr=True), remux_ship_render(sdr=True) for a video).
# An HDR source pinned to SDR needs a tone map (not built) and a combine an RPU: those refuse.
DVRENDER_COMBINE_ROUTES = ("combine_nodv",)
DVRENDER_OUTPUT_ROUTES = ("sdr_out",)
SDR_OUT_CHUNK_BASES = ("tv_sdr_topaz", "tv_disk_fallback", "sd_sdr_topaz", "movie_sdr_topaz")
SDR_OUT_FIT_BASES = ("yt_native4k", "yt_plain", "yt_1080")
DVRENDER_ROUTES = (("yt_native4k", "sdr4k", "yt_plain", "yt_1080") + DVRENDER_HDR_ROUTES
                   + DVRENDER_SDR_CHUNK_ROUTES + DVRENDER_COMBINE_ROUTES + DVRENDER_OUTPUT_ROUTES)
DVRENDER_FIT_ROUTES = ("yt_plain", "yt_1080")
DVRENDER_SIZE = (3840, 2160)
TOOLCHAIN_REFUSAL = "the native toolchain is not ready"   # a refusal of the MACHINE, not the item
DVRENDER_HDR_MAX = (4096, 2176)     # the largest frame an HDR re-encode makes: HEVC level 5.1's
                                    # picture size (DV 8.1's UHD levels); larger is refused


def _int0(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def dvrender_spec(p, route) -> tuple:
    """(nativerender.RenderSpec, "") for item `p`'s native render on `route`, or (None, why) when
    the stage cannot make it (the item then fails with that reason). The spec for the route: the
    CFR source, the pinned LUT, the export bitrate (youtube_render_kbps of the source's height under the max_peak_mbps gate
    for a video — 14 Mbps for a native-4K or a vertical >= 1700-line one, 20 otherwise; for 4K
    SDR the intake's own bitrate floored at EXPORT_BITRATE_FLOOR_KBPS, no gate — its remux
    re-encodes under the cap anyway), the scene plan _plan_fast_path_bounds stashed as the
    analysis ranges, and for a fit route the fit front end its tags call for. The HDR intake,
    SDR chunk, combine and SDR-output routes have specs of their own (_hdr_render_spec,
    _sdr_chunk_render_spec, _combine_render_spec, _sdr_out_spec). Probes the CFR (stream tags,
    frame count) and asks the native toolchain check (cached); never writes anything."""
    if route not in DVRENDER_ROUTES:
        return None, f"route {route or 'unknown'} has no native render yet"
    import nativelane
    import nativerender
    import settings as _st
    if route in DVRENDER_COMBINE_ROUTES:        # a combine has no CFR: it renders the winner
        ok, why = nativelane.toolchain_ok()
        if not ok:
            return None, f"{TOOLCHAIN_REFUSAL}: {why}"
        return _combine_render_spec(p)
    src = getattr(p, "source_cfr", None)
    if not src or not os.path.exists(src):
        return None, "its CFR source is not on disk"
    ok, why = nativelane.toolchain_ok()
    if not ok:
        return None, f"{TOOLCHAIN_REFUSAL}: {why}"
    if route in DVRENDER_HDR_ROUTES:
        return _hdr_render_spec(p, route, src)
    if route in DVRENDER_SDR_CHUNK_ROUTES:
        return _sdr_chunk_render_spec(p, route)
    if route in DVRENDER_OUTPUT_ROUTES:
        return _sdr_out_spec(p, src)
    import dvnative
    info = nativelane.probe_stream(src, extra=("sample_aspect_ratio",))
    if not info:
        return None, "the source could not be probed"
    size = (_int0(info.get("width")), _int0(info.get("height")))
    pre_vf = None
    if route in DVRENDER_FIT_ROUTES or size != DVRENDER_SIZE:
        # a fit route always takes the fit front end; a native-4K one (yt_native4k, sdr4k) only
        # when its frame is not the master's — 2.39:1 scope, DCI 4K — and is then letterboxed
        # into 3840x2160, as every 4K master is
        try:
            pre_vf = dvnative.fit_front_end(info, DVRENDER_SIZE)
        except ValueError as e:
            return None, str(e)
    else:
        ok, why = nativelane.source_ok(info)
        if not ok:
            return None, why.replace("chunk 0", "the source")
    cfr = nativelane.cfr_info(p)
    if not cfr.get("frames") or not cfr.get("fps"):
        return None, "the CFR's frame count or frame rate is unknown"
    if route.startswith("yt_"):
        # the YouTube export: youtube_render_kbps(h) for the height of plan_for(p.source)'s
        # probe — the same height as the CFR's, which never changes the geometry — and its
        # cap_arg, the max_peak_mbps render gate.
        kbps, cap = youtube_render_kbps(size[1]), int(_st.get_settings().get("max_peak_mbps", 50))
    else:
        kbps, cap = max(_source_video_kbps(p.source), EXPORT_BITRATE_FLOOR_KBPS), 0
    spec = nativerender.RenderSpec(inputs=[[0, src], [cfr["frames"], None]], fps=cfr["fps"],
                                   kbps=kbps, cap_mbps=cap, lut=nativelane.lut_path(),
                                   pre_vf=pre_vf,
                                   bounds=_read_topaz_bounds(p.source_basename) or None)
    bad = spec.problems()
    if bad:
        return None, "; ".join(bad)
    return spec, ""


def declared_light(light):
    """PURE. The content light level a native HDR route declares: the source's own (MaxCLL,
    MaxFALL), or None — measure it, in L6 and in the HDR10 SEI alike — when the source says 0,0
    (HDR10's "unknown", or no level at all: dvnative.source_mastering). The reference masters
    measure it then too: Hamilton's (2026-10-18) declared 620,41 in both over a 0,0 WEB source."""
    return None if light is None or tuple(light) == (0, 0) else light


def source_hdr10(path):
    """(master_display, max_cll) of `path`'s first frame — dvcap.probe_video's own read of the
    HDR10 SEI (x265's mastering-display string, "MaxCLL,MaxFALL"; None for one it lacks) — or
    None when ffprobe cannot read the file. Only the first frame: probe_video would also count
    a Matroska file's packets (a whole-file scan) for a frame count this does not need."""
    import dvcap
    try:
        r = subprocess.run([FFPROBE, "-v", "quiet", "-select_streams", "v:0", "-print_format",
                            "json", "-show_frames", "-read_intervals", "%+#1", path],
                           capture_output=True, text=True, timeout=60)
        frames = json.loads(r.stdout or "{}").get("frames") if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not frames:
        return None
    md = cll = None
    for sd in frames[0].get("side_data_list") or []:
        t = sd.get("side_data_type", "")
        if "Mastering display" in t:
            md = dvcap.mastering_to_x265(sd)
        elif "Content light" in t:
            cll = "%d,%d" % (int(sd.get("max_content", 0)), int(sd.get("max_average", 0)))
    return md, cll


def _hdr_render_spec(p, route, src) -> tuple:
    """dvrender_spec for an HDR intake route (DVRENDER_HDR_ROUTES): no LUT, a PQ picture as it is
    and an HLG one through the HLG -> PQ front end (dvnative.hdr_front_end), BT.2020 limited only.
    The picture is the CFR source — or, for hdr_topaz, Topaz's chunks (nativelane.chunks, checked
    against the CFR they were made from, whose audio the remux takes). PQ routes keep the
    ORIGINAL's own mastering — read from p.source, whose stream the rpu_only master ships (a VFR
    source's CFR is a re-encode that need not carry the SEI) — and HLG takes the 1000-nit master
    it is converted for. The bitrate and gate: the floor for rpu_only (its
    render's picture is never re-encoded here at all — keep_video), youtube_render_kbps(h) under
    max_peak_mbps for a YouTube video, else the intake's own bitrate floored at
    EXPORT_BITRATE_FLOOR_KBPS with no gate (its remux re-encodes under the cap). Probes only;
    writes nothing."""
    import dvnative
    import nativelane
    import nativerender
    import settings as _st
    cfr = nativelane.cfr_info(p)
    if not cfr.get("frames") or not cfr.get("fps"):
        return None, "the CFR's frame count or frame rate is unknown"
    inputs = [[0, src], [cfr["frames"], None]]
    picture = src
    if route in DVRENDER_CHUNK_ROUTES:
        try:
            inputs = nativelane.chunks(p.segdir, expected=cfr["frames"])
        except ValueError as e:
            return None, f"the Topaz chunks: {e}"
        picture = inputs[0][1]
    info = nativelane.probe_stream(picture, extra=("codec_name",))
    if not info:
        return None, "the source could not be probed"
    if route in DVRENDER_CHUNK_ROUTES:
        ok, why = nativelane.cfr_check(inputs[-1][0], cfr["frames"], info.get("r_frame_rate"),
                                       cfr["fps"])
        if not ok:
            return None, why.replace("chunk 0", "Topaz's chunk 0")
    want = {"hlg": (dvnative.HLG,), "hdr_topaz": (dvnative.PQ, dvnative.HLG)}.get(route, (dvnative.PQ,))
    trc = info.get("color_transfer")
    if trc not in want:
        return None, (f"the source transfer is {trc or 'untagged'}; route {route} takes "
                      f"{' or '.join('HLG' if t == dvnative.HLG else 'PQ' for t in want)}")
    try:
        pre_vf = dvnative.hdr_front_end(info)
    except ValueError as e:
        return None, str(e)
    size = (_int0(info.get("width")), _int0(info.get("height")))
    if size[0] <= 0 or size[1] <= 0:
        return None, "the source's size is unknown"
    keep = route == "rpu_only"
    if keep and (info.get("codec_name"), info.get("pix_fmt")) != ("hevc", "yuv420p10le"):
        return None, (f"the source is {info.get('codec_name')} {info.get('pix_fmt')}, not HEVC "
                      "Main10 — its own stream cannot carry the RPU")
    if not keep and (size[0] > DVRENDER_HDR_MAX[0] or size[1] > DVRENDER_HDR_MAX[1]):
        return None, (f"the source is {size[0]}x{size[1]}, larger than "
                      f"{DVRENDER_HDR_MAX[0]}x{DVRENDER_HDR_MAX[1]} (HEVC level 5.1)")
    yt = bool(getattr(p, "youtube", False)) and not keep
    if yt and size != DVRENDER_SIZE:
        return None, (f"the source is {size[0]}x{size[1]}: a YouTube video renders at "
                      f"{DVRENDER_SIZE[0]}x{DVRENDER_SIZE[1]}, and there is no HDR fit front end yet")
    master, light = dvnative.DEFAULT_MASTER, None
    if trc == dvnative.PQ:
        hdr10 = source_hdr10(p.source)
        if hdr10 is None:
            return None, "the source's HDR10 metadata could not be read"
        try:
            master, light = dvnative.source_mastering(*hdr10)
        except ValueError as e:
            return None, str(e)
        light = declared_light(light)
    if keep:
        kbps, cap = EXPORT_BITRATE_FLOOR_KBPS, 0
    elif yt:
        kbps, cap = youtube_render_kbps(size[1]), int(_st.get_settings().get("max_peak_mbps", 50))
    else:
        kbps, cap = max(_source_video_kbps(p.source), EXPORT_BITRATE_FLOOR_KBPS), 0
    single = route not in DVRENDER_CHUNK_ROUTES
    spec = nativerender.RenderSpec(inputs=inputs, fps=cfr["fps"], kbps=kbps, cap_mbps=cap,
                                   lut=None, pre_vf=pre_vf,
                                   bounds=(_read_topaz_bounds(p.source_basename) or None) if single else None,
                                   master=master, light=light, keep_video=keep)
    bad = spec.problems()
    if bad:
        return None, "; ".join(bad)
    return spec, ""


def _sdr_chunk_render_spec(p, route, *, sdr: bool = False) -> tuple:
    """dvrender_spec for an SDR Topaz chunk route (DVRENDER_SDR_CHUNK_ROUTES): the render from
    Topaz's chunks (nativelane.chunks, checked against the CFR whose audio the remux takes, chunk
    0's rate exactly the CFR's) through dvnative.chunk_front_end — chunk 0's tags, an untagged one
    judged by the source's frame (the CFR's: it keeps the source's geometry) — and the pinned LUT,
    at the intake's own bitrate, floored at EXPORT_BITRATE_FLOOR_KBPS, no gate (its remux
    re-encodes under the cap). A TV route (DVRENDER_TV_CHUNK_ROUTES) refuses an item the chunk
    lane has claimed — the lane makes it — unless the lane fell back from it. `sdr` (sdr_out over a chunk route): the same
    chunks and front end with no LUT, into the SDR render. Probes only; writes nothing."""
    import dvnative
    import nativelane
    import nativerender
    if route in DVRENDER_TV_CHUNK_ROUTES and nativelane.has_marker(p) \
            and not nativelane.has_fallback(p):
        return None, "the native lane has claimed this item (native/native.json)"
    cfr = nativelane.cfr_info(p)
    if not cfr.get("frames") or not cfr.get("fps"):
        return None, "the CFR's frame count or frame rate is unknown"
    try:
        inputs = nativelane.chunks(p.segdir, expected=cfr["frames"])
    except ValueError as e:
        return None, f"the Topaz chunks: {e}"
    info = nativelane.probe_stream(inputs[0][1], extra=("sample_aspect_ratio",))
    if not info:
        return None, "Topaz's chunk 0 could not be probed"
    ok, why = nativelane.cfr_check(inputs[-1][0], cfr["frames"], info.get("r_frame_rate"), cfr["fps"])
    if not ok:
        return None, why.replace("chunk 0", "Topaz's chunk 0")
    try:
        pre_vf = dvnative.chunk_front_end(info, nativelane.probe_stream(p.source_cfr))
    except ValueError as e:
        return None, f"Topaz's chunk 0: {e}"
    kbps = max(_source_video_kbps(p.source), EXPORT_BITRATE_FLOOR_KBPS)
    spec = nativerender.RenderSpec(inputs=inputs, fps=cfr["fps"], kbps=kbps, cap_mbps=0,
                                   lut=None if sdr else nativelane.lut_path(), pre_vf=pre_vf or None,
                                   sdr=sdr)
    bad = spec.problems()
    if bad:
        return None, "; ".join(bad)
    return spec, ""


def _sdr_out_spec(p, src) -> tuple:
    """dvrender_spec for sdr_out (the SDR output mode, section comment above DVRENDER_ROUTES): the
    item's unpinned route says what its picture is, and it is rendered as that route's, minus the
    LUT, into the SDR render (nativerender sdr spec: BT.709 HEVC, no RPU):
      a Topaz chunk route   its chunks through the chunk front end (_sdr_chunk_render_spec)
      a YouTube route       the CFR through the fit front end to 3840x2160 at
                            youtube_render_kbps(h) under the max_peak_mbps gate
      sdr4k                 the CFR at its own frame, through the colour step alone, at the
                            intake's bitrate floored at EXPORT_BITRATE_FLOOR_KBPS, no gate
    Anything else refuses with the reason (HDR needs a tone map; a combine an RPU). Probes only."""
    import dvnative
    import nativelane
    import nativerender
    import routes
    import settings as _st
    base = routes.item_route(p, mode="dv1000")
    if base in SDR_OUT_CHUNK_BASES:
        return _sdr_chunk_render_spec(p, base, sdr=True)
    if base not in SDR_OUT_FIT_BASES + ("sdr4k",):
        why = ("an HDR source mastered to SDR needs a tone map, which is not built"
               if base in DVRENDER_HDR_ROUTES else
               "a companion combine ships Dolby Vision" if str(base).startswith("combine")
               else "no SDR render for it")
        return None, f"underneath the SDR pin it is route {base or 'unknown'}: {why}"
    info = nativelane.probe_stream(src, extra=("sample_aspect_ratio",))
    if not info:
        return None, "the source could not be probed"
    try:
        if base in SDR_OUT_FIT_BASES:
            pre_vf = dvnative.fit_front_end(info, DVRENDER_SIZE)
        else:
            m, pr, full = dvnative.colour_front(info)
            pre_vf = dvnative.colour_vf(m, pr, full)
    except ValueError as e:
        return None, str(e)
    cfr = nativelane.cfr_info(p)
    if not cfr.get("frames") or not cfr.get("fps"):
        return None, "the CFR's frame count or frame rate is unknown"
    if base in SDR_OUT_FIT_BASES:
        kbps = youtube_render_kbps(_int0(info.get("height")))
        cap = int(_st.get_settings().get("max_peak_mbps", 50))
    else:
        kbps, cap = max(_source_video_kbps(p.source), EXPORT_BITRATE_FLOOR_KBPS), 0
    spec = nativerender.RenderSpec(inputs=[[0, src], [cfr["frames"], None]], fps=cfr["fps"],
                                   kbps=kbps, cap_mbps=cap, lut=None, pre_vf=pre_vf or None,
                                   bounds=_read_topaz_bounds(p.source_basename) or None, sdr=True)
    bad = spec.problems()
    if bad:
        return None, "; ".join(bad)
    return spec, ""


def _combine_render_spec(p) -> tuple:
    """dvrender_spec for combine_nodv: a companion combine where neither copy has real Dolby
    Vision, so the RPU is measured from the verdict's WINNER (orchestrator.combine_winner_path —
    the copy that ships), exactly as rpu_only measures its one source: no LUT, the winner's own
    mastering (its HDR10 SEI -> L6 and the L1/L4 clamp, dvnative.source_mastering), and the
    render IS the winner's own coded video with the RPU injected (keep_video) when it is HEVC
    Main10 — else a VideoToolbox encode of it, at the export floor bitrate.
    Its video never ships either way: remux.combine takes only the render's RPU (rpu_source =
    p.dv_render), injected onto the winner's bits or carried by the capped x265 of the winner —
    the very calls it always made. companion.build_verdict refuses a winner that
    is not PQ, and so does this. No CFR exists for a combine: the frame count is the winner's
    own (a header read, else a packet count — never a decode) and so is the rate. Probes only."""
    import dvnative
    import nativelane
    import nativerender
    import topaz
    from orchestrator import combine_winner_path
    winner = combine_winner_path(p)
    if not winner or not os.path.exists(winner):
        return None, "the combine verdict's winner is not on disk"
    info = nativelane.probe_stream(winner, extra=("codec_name",))
    if not info:
        return None, "the winner could not be probed"
    if info.get("color_transfer") != dvnative.PQ:
        return None, (f"the winner's transfer is {info.get('color_transfer') or 'untagged'}, not PQ "
                      "(an RPU describes a PQ base)")
    try:
        if dvnative.hdr_front_end(info) is not None:
            raise ValueError("the winner needs a front end")
    except ValueError as e:
        return None, str(e)
    frames = topaz._frame_count(winner, decode=False)
    fps = nativelane.fps_fraction(winner)
    if not frames or frames <= 0 or not fps:
        return None, "the winner's frame count or frame rate is unknown"
    size = (_int0(info.get("width")), _int0(info.get("height")))
    keep = (info.get("codec_name"), info.get("pix_fmt")) == ("hevc", "yuv420p10le")
    if size[0] <= 0 or size[1] <= 0:
        return None, "the winner's size is unknown"
    if not keep and (size[0] > DVRENDER_HDR_MAX[0] or size[1] > DVRENDER_HDR_MAX[1]):
        return None, (f"the winner is {size[0]}x{size[1]}, larger than "
                      f"{DVRENDER_HDR_MAX[0]}x{DVRENDER_HDR_MAX[1]} (HEVC level 5.1)")
    hdr10 = source_hdr10(winner)
    if hdr10 is None:
        return None, "the winner's HDR10 metadata could not be read"
    try:
        master, light = dvnative.source_mastering(*hdr10)
    except ValueError as e:
        return None, str(e)
    light = declared_light(light)
    spec = nativerender.RenderSpec(inputs=[[0, winner], [frames, None]], fps=fps,
                                   kbps=EXPORT_BITRATE_FLOOR_KBPS, cap_mbps=0, lut=None,
                                   bounds=_read_topaz_bounds(p.source_basename) or None,
                                   master=master, light=light, keep_video=keep)
    bad = spec.problems()
    if bad:
        return None, "; ".join(bad)
    return spec, ""


def route_refusal(p) -> str:
    """Why this item's Dolby Vision can never be made natively whatever Topaz produces — ""
    when it can (or when that is not known yet: the source is not local). Asked at the Topaz
    doorstep, so an item no route can make is refused BEFORE hours of upscaling, not after:
      a route with no native render at all (unknown, or one this table has not got)
      sdr_out over an HDR source (needs a tone map) or a combine (ships Dolby Vision)
      an HDR YouTube video that is not 3840x2160 (needs an HDR fit, not built)
    Every other refusal depends on the chunks, the toolchain or the disk, and stays the render's.
    Never raises (a probe hiccup is "" — the render decides later)."""
    try:
        import routes
        route = routes.item_route(p)
        if route is None or route in routes.NO_DV_STEP:
            return ""
        if route not in DVRENDER_ROUTES:
            return f"route {route} has no native render"
        if route == "sdr_out" and not getattr(p, "combine", False):
            base = routes.item_route(p, mode="dv1000")
            if base in DVRENDER_HDR_ROUTES:
                return (f"underneath the SDR pin it is route {base}: an HDR source mastered to SDR "
                        "needs a tone map, which is not built")
        if getattr(p, "youtube", False) and route in DVRENDER_HDR_ROUTES and route != "rpu_only":
            import plan as _plan
            info = _plan.probe_input(p.source)
            size = (_int0(info.get("width")), _int0(info.get("height")))
            if size[0] and size != DVRENDER_SIZE:
                return (f"an HDR YouTube video at {size[0]}x{size[1]}: its render is 3840x2160 and "
                        "there is no HDR fit front end yet")
        return ""
    except Exception:  # noqa: BLE001 — a doorstep question must never cost the item
        return ""


def native_route(p) -> tuple:
    """(route, spec, why): the native render's plan for this item's Dolby Vision ('dvrender').
    `spec` is set when the stage can make it; otherwise `why` says what stops it."""
    import routes
    route = routes.item_route(p)
    if getattr(p, "combine", False):
        import companion
        if not companion.confirmed_verdict(p.source_basename):
            return route, None, "permanent: combine verdict lost — re-pair the companion"
    spec, why = dvrender_spec(p, route)
    return route, spec, why


def _native_render_route(p):
    """The route the native render will make this item on, or None — for a stage MESSAGE, so it
    never raises."""
    try:
        route, spec, _why = native_route(p)
    except Exception:  # noqa: BLE001 — a message must never cost the stage
        return None
    return route if spec is not None else None


def _dvrender(p, abort, progress=None, should_pause=None):
    """NATIVE DOLBY VISION RENDER (every route the chunk lane does not take): write p.dv_render —
    HEVC Main10 PQ with an in-band DV 8.1 RPU, the whole source at its frame rate
    (nativerender.render: the analysis in scene
    ranges through the pinned LUT, a VideoToolbox encode under the peak gate, the RPU inject, the
    mux, the self-check, the atomic publish). Run thread only, and only for an item
    orchestrator._process routed here — never the screen, never a SIGSTOP: it runs at nice 10
    beside the previous item's x265 tail, and everything after it (stage_done("dv"), the
    remux, the hand-off, cleanup) reads p.dv_render.

    should_pause is honoured during the ANALYSIS only, where a yield loses at most the range in
    hand (it resumes range by range). From the first encode on the stage runs to its end, the
    way: a yield there would throw the whole encode away (it restarts from
    scratch) — deploy-now.sh waits that phase out for the same reason. "paused: ..." and
    "interrupted: ..." are not failures; anything else is a plain failure, counted by the
    orchestrator's ordinary fail ladder."""
    import nativerender
    import routes
    route = routes.item_route(p)
    spec, why = dvrender_spec(p, route)
    if spec is None:
        return False, f"native render refused (route {route or 'unknown'}): {why}"
    total = spec.total
    phase = {"now": None}            # the last phase render() reported (None: not started yet)

    def report(info):
        phase["now"] = info.get("phase")
        if progress:
            progress(dict(info, frames=total, route=route))

    def pause():
        return phase["now"] in (None, "analysis") and bool(should_pause())

    ok, msg = nativerender.render(p, spec, abort=abort, progress=report,
                                  should_pause=pause if should_pause is not None else None)
    msg = str(msg)
    if ok or msg.startswith("paused:"):
        return ok, msg
    if msg.startswith("aborted") or (abort is not None and abort.is_set()):
        return False, ("interrupted: native DV render stopped — the next pass resumes its "
                       "analysis range by range")
    return False, msg


def _tree_gib(path) -> float:
    """GiB under a directory (0 when it is missing)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1024 ** 3


# ---- native DV remux: the wall-time ledger and the pass ----------------------------------

def _never_raise(fn, *a, **k) -> None:
    """Call a bookkeeping hook from inside dvcap's encode loop: an exception there would end
    the x265 pipe ("pipe error"), so a ledger bug must never get that far."""
    try:
        fn(*a, **k)
    except Exception:  # noqa: BLE001
        pass


class _NativeRemuxClock:
    """Wall time per LIVE-encoded segment of a native item's remux, booked into
    segdir/native/cpu.json beside the analysis CPU (design §4 'Measure'). Fed by the remux's own callbacks — nothing in
    dvcap or its processes changes:
      plan(ends, total)             on_plan: the segment ends; this attempt's clock starts
      progress(frames, total, **x)  on_progress: cumulative frames. A segment counts only when
                                    an in-segment frame was seen before its end was reached —
                                    a resumed segment jumps straight to its end and is never
                                    booked (it cost this attempt nothing).
      repair(seg, done, n)          the peak repair's per-frame callback: wall from its first
                                    frame report to its last, per re-capped segment ('remux_repair').
    Rows are {key: {"wall_s", "frames"}} under phases "remux" and "remux_repair"; a segment
    measured again (a retry) replaces its row. Booking never fails the remux."""

    def __init__(self, p, clock=None):
        self.p, self._now = p, (clock or time.monotonic)
        self.ends, self.mark, self.done, self.live = None, None, 0, False
        self._rep = None          # [seg (1-based), started, last, frames, max done] of a repair
        self._rep_sum = {}        # seg -> wall booked by the repairs of this call (rungs add up)

    def plan(self, ends, _total=None):
        try:
            self.ends = [int(e) for e in ends]
        except (TypeError, ValueError):
            self.ends = None
        self.mark, self.done, self.live = self._now(), 0, False

    def progress(self, frames, _total=None, **extra):
        if extra.get("repair_seg") is not None or not self.ends or self.mark is None:
            return
        try:
            frames = int(frames)
        except (TypeError, ValueError):
            return
        crossed = sum(1 for e in self.ends if frames >= e)
        if crossed <= self.done:
            if frames > (self.ends[self.done - 1] if self.done else 0):
                self.live = True                    # real x265 progress inside this segment
            return
        now = self._now()
        if crossed == self.done + 1 and self.live:
            k = self.done
            n = self.ends[k] - (self.ends[k - 1] if k else 0)
            self._book("remux", f"seg_{k:04d}", now - self.mark, n)
        self.done, self.mark, self.live = crossed, now, False

    def repair(self, seg, done, n):
        now = self._now()
        cur = self._rep
        if cur is None or cur[0] != seg or (done == 0 and cur[4] > 0):   # a new segment / rung
            self.finish()
            self._rep = [seg, now, now, n, 0]
            return
        cur[2], cur[4] = now, max(cur[4], done or 0)

    def finish(self):
        """Book the repair in flight (its last frame report ends it)."""
        cur, self._rep = self._rep, None
        if cur is None or cur[2] <= cur[1]:
            return
        seg = cur[0]
        self._rep_sum[seg] = self._rep_sum.get(seg, 0.0) + (cur[2] - cur[1])
        self._book("remux_repair", f"seg_{seg - 1:04d}", self._rep_sum[seg], cur[3])

    def _book(self, phase, key, wall_s, frames):
        try:
            import nativelane
            with nativelane._LOCK:
                book = nativelane._read_json(nativelane.cpu_path(self.p))
                book = book if isinstance(book, dict) else {}
                if not isinstance(book.get(phase), dict):
                    book[phase] = {}
                book[phase][key] = {"wall_s": round(float(wall_s), 1), "frames": int(frames or 0)}
                nativelane._write_json(nativelane.cpu_path(self.p), book)
        except Exception as e:  # noqa: BLE001 — a ledger row is never worth a remux
            logbook.exception(f"remux {getattr(self.p, 'ep', '?')}: native wall ledger", e)


def native_wall_line(p) -> str:
    """One line of the native remux's booked wall time (all of its segments, every attempt),
    for the log — cpu.json goes with the segdir at cleanup, the log line stays."""
    import nativelane
    book = nativelane._read_json(nativelane.cpu_path(p))
    parts = []
    for phase in ("remux", "remux_repair"):
        rows = (book or {}).get(phase) if isinstance(book, dict) else None
        rows = [r for r in rows.values() if isinstance(r, dict)] if isinstance(rows, dict) else []
        if not rows:
            continue
        wall = sum(float(r.get("wall_s") or 0) for r in rows)
        frames = sum(int(r.get("frames") or 0) for r in rows)
        per = f", {wall * 1000.0 / frames:.1f} ms/frame" if frames else ""
        parts.append(f"{phase} wall {wall:.0f} s over {frames} frames ({len(rows)} segments{per})")
    return "; ".join(parts) or "no remux segment timed (all resumed)"


def _native_master_passed(p, native: dict) -> None:
    """A NATIVE master just passed the peak gate: free its Topaz chunks — the ProRes seg_*.mov
    files, ~130-190 GiB, the very thing the next item's disk gates wait on — and keep a proof
    that survives them. The marker turns NATIVE_ENCODED (written FIRST, so a kill in between
    leaves a proof, never a chunk-less 'ready' marker) and records the master's path, size and
    mtime: orchestrator.stage_done("topaz"/"dv") accept it while that master is unchanged,
    so an item that parks at upload and is re-picked goes straight back to the upload instead
    of re-running Topaz and the analysis. The rest of the segdir (the marker, RPU, sidecar,
    cpu.json; a few MB) goes with cleanup's rmtree. If the stamp cannot be written, the whole
    segdir goes at once, exactly as before the stamp existed."""
    import glob
    import nativelane
    from orchestrator import NATIVE_ENCODED
    ep = getattr(p, "ep", "?")
    try:
        m = nativelane.read_marker(p)
        if not isinstance(m, dict):
            raise ValueError("no native marker to stamp")
        st = os.stat(p.final)
        m.update(state=NATIVE_ENCODED, ready_id=native.get("id"), final=p.final,
                 final_size=st.st_size, final_mtime_ns=st.st_mtime_ns, encoded_at=int(time.time()))
        nativelane.write_marker(p, m)
    except Exception as e:  # noqa: BLE001
        logbook.exception(f"remux {ep}: stamping the native marker", e)
        gib = _tree_gib(p.segdir)
        shutil.rmtree(p.segdir, ignore_errors=True)
        logbook.event(f"remux {ep}: native master passed the peak gate — freed {gib:.0f} GiB "
                      f"of Topaz chunks (no 'encoded' stamp: a re-pick re-runs Topaz)")
        return
    files = set(glob.glob(os.path.join(p.segdir, "seg_*.mov")))
    files.update(path for _a, path in (native.get("chunks") or []) if path)
    freed = 0
    for f in sorted(files):
        try:
            size = os.path.getsize(f)
            os.remove(f)
            freed += size
        except OSError:
            pass
    try:
        timing = f"{nativelane.cpu_line(p, 'analysis')}; {native_wall_line(p)}"
    except Exception:  # noqa: BLE001
        timing = "timing unavailable"
    logbook.event(f"remux {ep}: native master passed the peak gate — freed "
                  f"{freed / 1024 ** 3:.0f} GiB of Topaz chunks ({timing})")


def _combine_result(res, real_rpu_donor):
    """Map a combine RemuxResult to the stage's (ok, reason). A frame/fps mismatch
    against a REAL companion donor means the two releases are different cuts — that can
    never heal, so it parks permanently. The same mismatch against the native render is
    just a truncated render: retryable (the DV step redoes it). The AUDIO-donor
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


def youtube_render_kbps(height) -> int:
    """What a YouTube item's render targets. A NATIVE 4K source gets a lower target
    than a 1080p one, because the point of the number is not picture quality in isolation —
    it is landing the render's PEAK under max_peak_mbps so the remux can stream-copy it.
    A VideoToolbox export has no peak control whatsoever, so the only lever is the
    target, and a more detailed source bursts further above it."""
    return (YOUTUBE_RENDER_KBPS_4K if (height or 0) >= YOUTUBE_NATIVE_4K_MIN_HEIGHT
            else YOUTUBE_RENDER_KBPS)


def _remux(p, abort, progress=None, should_pause=None):
    """mute DV video -> PEAK-CAPPED DV video (dvcap x265 re-encode, hard `max_peak_mbps`
    ceiling — see remux.py; NO uncapped fallback) + audio (CFR file) + subtitles (ORIGINAL
    download) -> final master. Audio comes from the CFR file — the DV video derives from the
    CFR timeline, so that audio is guaranteed in sync; subtitles come from the original because
    the CFR pass no longer carries them. The master is Matroska (mkvmerge), whatever the source
    holds. The x265 pass makes this stage LONG (~an hour per episode) — it reports live progress
    and honours abort."""
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
    if lufs and p.series and not settings_mod.get_show_normalize_audio(_settings_scope(p)):
        lufs = None
    # SEASON-SCOPED LOUDNESS FOR TV (user-dictated 2026-08-18). Measuring every episode on
    # its own let two episodes of ONE season land on different gains, so the volume stepped
    # mid-binge. A season from one source is one mastering job: its FIRST episode measures
    # and records the gain, and every later episode from the same source reuses it. "Same
    # source" is read off the filename, so a WEB-DL episode dropped into a BluRay season is
    # measured on its own rather than inheriting a gain never meant for it. Movies (one item
    # each) and YouTube (unrelated videos) keep measuring per item. Every master is Matroska and
    # every route boosts (lossless audio keeps its original track), so this holds for all of them.
    audio_gain = None
    if lufs and not p.movie and not p.youtube:
        import audiogain
        gkey = audiogain.key_for(p.series, p.ep, p.source_basename)
        if gkey:
            audio_gain = audiogain.remembered(gkey)
            season = audiogain.season_of(p.ep)
            if audio_gain is None:
                measured = remux.measure_lufs(p.source_cfr)
                audio_gain = remux.boost_gain_db(measured, lufs)
                # Recorded BEFORE the ~75-min remux, so a failure that retries reuses this
                # decision instead of re-deciding from whichever episode runs next.
                audiogain.remember(gkey, audio_gain, measured, p.source_basename, target=lufs)
                logbook.event(f"audio {p.series} {season}: measured "
                              f"{'?' if measured is None else round(measured, 1)} LUFS "
                              f"-> +{audio_gain:.1f} dB (this season's decision)")
            else:
                logbook.event(f"audio {p.series} {season}: reusing +{audio_gain:.1f} dB "
                              f"from the season's first episode")
    steps = stagebar.Steps()   # the fast paths' lettered stages (remux announces its plan)

    def _on_step(label, pct):
        # LABELED STEP progress for the fast (stream-copy) remuxes (user-asked): one bar
        # at a time — the current step's own percentage where a phase has a watchable
        # output file, label-only (pct None) where it doesn't — each a lettered stage.
        if progress:
            progress(dict({"stage": "remux", "ep": p.ep,
                           "pct": (round(pct, 1) if pct is not None else None), "step": label},
                          **steps.at(label, pct)))

    encode_source = None   # set when rpu-only loses its stream-copy privilege (peak-gated)
    combine_call = None    # set when a COMBINE item needs the capped path (notched bar below)
    # SDR OUTPUT (2026-10-18, route sdr_out): an SDR render the native render published for an item
    # pinned to SDR (its proof, nativerender.sdr_render_ok — nothing else is ever taken for one)
    # becomes the TRUE SDR master: remux_ship_render / remux with sdr=True (no RPU, Rec.709 x265
    # under the same cap, no DV box). Every other item makes exactly the calls it always made.
    # ONLY while the item still ships as SDR (orchestrator.ships_as_sdr — p.final's name; 2026-10-18
    # review): the finisher runs the remux without asking stage_done("dv"), so an item unpinned
    # after its SDR render and resumed under the DV name would otherwise have made an SDR master
    # named '... HDR10 DV upscaled' and uploaded it. That render cannot make a DV master either, so
    # the remux refuses at once (no hour of x265 first); the failed pick sends the item back
    # through RUN_STAGES, where stage_done("dv") no longer counts the SDR render
    # (orchestrator._native_sdr_render asks the same question) and the item renders again as DV.
    sdr_out = False
    if not p.combine and os.path.isfile(str(p.dv_render) + ".dvrender/sdr.json"):   # one stat, else
        import nativerender                                                      # nothing at all
        if nativerender.sdr_render_ok(p):
            from orchestrator import ships_as_sdr
            if not ships_as_sdr(p):
                return False, ("the render is the SDR output's (sdr_out), but the item now ships as "
                               "Dolby Vision (its SDR pin is gone): it renders again before the remux")
            sdr_out = True
    sdr_kw = {"sdr": True} if sdr_out else {}
    if p.combine:
        # COMPANION COMBINE: the confirmed verdict names the donors; the peak gate picks
        # the path. Under the 72 Mbps gate the winner's own bits ship (RPU grafted or
        # converted as needed — remux_inject machinery); over it, the enforced-VBV capped
        # re-encode runs with the SAME RPU (real DV survives a re-encode and still beats
        # generated DV, user-dictated).
        import companion as companion_mod
        from orchestrator import combine_winner_path
        cv = companion_mod.confirmed_verdict(p.source_basename)
        winner = combine_winner_path(p)
        if not cv or not winner:
            return False, "permanent: combine verdict lost — re-pair the companion"
        v = cv["verdict"]
        side = {"nas": p.source, "remote": p.companion_src}
        real_rpu = v.get("rpu_from") != companion_mod.GENERATED_RPU
        combine_call = {
            "winner": winner,
            "rpu_source": side[v["rpu_from"]] if real_rpu else p.dv_render,
            "audio_source": side.get(v.get("audio_from")) or winner,
            "rpu_inline": bool(v.get("rpu_inline")),
            "rpu_profile": (v.get("rpu_profile") or "") if real_rpu else "",
            "real_rpu": real_rpu,
        }
        steps.plan([PEAKS_STAGE] + remux.inject_stages(True))   # remux_inject corrects the tail
        _on_step(PEAKS_STAGE, None)
        peak = _peak_of(winner, os.path.basename(winner))
        if peak is None:
            return False, "could not measure source peaks — retrying (never ship unverified)"
        if peak <= dvcap.RPU_SHIP_VIDEO_GATE_MBPS:
            res = remux.combine(winner, combine_call["rpu_source"],
                                combine_call["audio_source"], p.final,
                                rpu_inline=combine_call["rpu_inline"],
                                rpu_profile=combine_call["rpu_profile"], capped=False,
                                cap_mbps=cap, audio_target_lufs=lufs, abort=abort,
                                on_step=_on_step, on_stages=steps.plan)
            return _combine_result(res, combine_call["real_rpu"])
        logbook.event(f"remux {p.ep}: winner peaks {peak:.1f} Mbps — over the "
                      f"{dvcap.RPU_SHIP_VIDEO_GATE_MBPS} Mbps DV ship gate; combine takes "
                      "the capped re-encode (real DV preserved)")
        steps.plan([PLAN_STAGE, CAPPED_STAGE])
        _on_step(PLAN_STAGE, None)
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
        steps.plan([PEAKS_STAGE] + remux.inject_stages(True))   # remux_inject corrects the tail
        _on_step(PEAKS_STAGE, None)
        peak = _source_peak_mbps(p)
        if peak is None:
            return False, "could not measure source peaks — retrying (never ship unverified)"
        if peak <= dvcap.RPU_SHIP_VIDEO_GATE_MBPS:
            res = remux.remux_inject(p.dv_render, p.source_cfr, p.source, p.final,
                                     audio_target_lufs=lufs, abort=abort, on_step=_on_step,
                                     on_stages=steps.plan)
            return res.ok, res.reason
        logbook.event(f"remux {p.ep}: source peaks {peak:.1f} Mbps — over the "
                      f"{dvcap.RPU_SHIP_VIDEO_GATE_MBPS} Mbps DV ship gate "
                      f"({dvcap.DV_STREAM_CEILING_MBPS} ceiling - TrueHD headroom); "
                      "stream copy revoked, capped re-encode of the source instead")
        steps.plan([PLAN_STAGE, CAPPED_STAGE])
        _on_step(PLAN_STAGE, None)
        _plan_fast_path_bounds(p)      # cached in the bounds book — retries are free
        encode_source = p.source_cfr   # RPU from the render; VIDEO re-encoded from the source
    elif p.youtube:
        # FAST YOUTUBE REMUX (user-asked): the render targets a cap-safe bitrate, so ship
        # its video untouched when the peak gate agrees — minutes instead of an hour.
        # "render-over-cap" (and only that) falls through to the normal capped re-encode.
        res = remux.remux_ship_render(p.dv_render, p.source_cfr, p.source, p.final,
                                      cap_mbps=cap, audio_target_lufs=lufs, abort=abort,
                                      on_step=_on_step, on_stages=steps.plan, **sdr_kw)
        if res.ok or not str(res.reason).startswith("render-over-cap"):
            return res.ok, res.reason
        logbook.event(f"remux {p.ep}: {res.reason}")
        steps.plan([CAPPED_STAGE])
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
        if steps.active:       # a fast path that turned into the capped re-encode: its last stage
            d.update(steps.at(CAPPED_STAGE, d["pct"], notches=d.get("notches"),
                              seg_done=d.get("seg_done")))
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
    # THE CHUNK LANE (the item carries a native marker and no fallback.json): this item never
    # rendered. Its RPU is the lane's, and x265 reads Topaz's ProRes chunks through the LUT (remux.remux(
    # native=)) — for the peak repair as well, which is why the chunks were KEPT at hand-off.
    # They go the moment the master passes the peak gate, before the upload; the marker stays,
    # stamped 'encoded', as the proof of that work (_native_master_passed). An item without
    # a native marker (a rendered item: its segdir was dropped at hand-off) takes the
    # call below exactly as before — no extra keyword at all.
    native = None
    if not (p.movie or p.youtube) and encode_source is None:
        import nativelane
        if nativelane.has_marker(p) and not nativelane.has_fallback(p):
            native = nativelane.bundle(p)
            if native is None:
                return False, (f"native DV metadata no longer matches the chunks "
                               f"({nativelane.check_ready(p)[1]}) — shipped nothing")
    on_prog, on_plan, on_repair, wall = _prog, _on_plan, _on_repair, None
    if native is not None:
        # ...and only a native call is timed per segment (the trial's ledger, cpu.json): the
        # same callbacks, each also feeding the wall clock, which can never raise into dvcap.
        wall = _NativeRemuxClock(p)

        def on_prog(frames, total, **extra):
            _never_raise(wall.progress, frames, total, **extra)
            _prog(frames, total, **extra)

        def on_plan(ends, exact_total):
            _never_raise(wall.plan, ends, exact_total)
            _on_plan(ends, exact_total)

        def on_repair(k, of, seg_index, done, seg_frames):
            _never_raise(wall.repair, seg_index + 1, done, seg_frames)
            _on_repair(k, of, seg_index, done, seg_frames)
    res = remux.remux(p.dv_render, p.source_cfr, p.source, p.final,
                      cap_mbps=cap, audio_target_lufs=lufs, audio_gain_db=audio_gain,
                      boundaries=bounds, abort=abort,
                      on_progress=on_prog, on_plan=on_plan, should_pause=should_pause,
                      on_repair=on_repair, encode_source=encode_source,
                      **({"native": native} if native is not None else {}), **sdr_kw)
    if wall is not None:
        _never_raise(wall.finish)
    if res.ok and native is not None:
        _native_master_passed(p, native)
    return res.ok, res.reason


def _remember_finished(p, nas_path):
    """Record the verified master in the history book. Nothing else knows WHERE a finished
    item landed — the log only says an upload happened — so without this there is nothing to
    point a later fix at (see history.revise_audio). Best-effort: a bookkeeping failure must
    never fail an upload that actually succeeded."""
    try:
        import audiogain
        import history
        gain = audiogain.remembered(audiogain.key_for(p.series, p.ep, p.source_basename))
        title = (getattr(p, "title", "") or "").strip() or p.ep
        history.record(nas_path=nas_path, kind=("youtube" if p.youtube else
                                                "movie" if p.movie else "episode"),
                       title=title, series=(p.series or ""), ep=(p.ep or ""), gain=gain)
    except Exception:
        pass


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
            p.final, p.nas_final, p.sidecar_dir, os.path.dirname(p.source), on_progress=on_prog,
            abort=abort, on_wait=_slot_note(progress, "upload", p.ep))
        if ok:
            _remember_finished(p, p.nas_final)
            reason += _supersede_legacy(p, p.nas_final)
        return ok, (reason if ok else _stopped_is_interrupted(reason))
    ok, remote, reason = transfer.upload(p.final, p.nas_dir, on_progress=on_prog, abort=abort,
                                         on_wait=_slot_note(progress, "upload", p.ep))
    if not ok:
        return False, _stopped_is_interrupted(reason)
    _remember_finished(p, remote)
    reason += _supersede_legacy(p, remote)
    import settings as settings_mod
    if settings_mod.get_show_replace_source(p.series):
        rok, rmsg = transfer.replace_original(remote, p.nas_source, p.final)
        return True, f"{reason}; {rmsg}"
    return True, f"{reason}; source kept (Plex serves the 4K version)"


def legacy_master_candidates(p, new_remote: str) -> list:
    """PURE. The NAS paths of THIS item's earlier .mp4 master, the ones a Matroska re-run lands
    beside (every master is .mkv since 2026-10-18; upload only replaces the same name). TV/movie:
    the new master's name with .mp4, and its pre-retag spelling (<source stem><tag>.mp4) — each
    only when series.is_master_name says it IS a pipeline master. YouTube: <youtarr stem>.mp4 in
    the Plex folder the master was published to — never when the Plex library and youtarr's
    staging library are the same folder, where that name could be the source itself. Never the
    new master, never the item's source."""
    import orchestrator as O
    import series as series_mod
    if not str(new_remote).lower().endswith(".mkv"):
        return []
    base = os.path.splitext(new_remote)[0]
    out = []
    if p.youtube:
        if transfer.NAS_FTP_YOUTUBE_ROOT.rstrip("/") == transfer.NAS_FTP_YOUTUBE_STAGING.rstrip("/"):
            return []
        out = [base + ".mp4"]
    else:
        stem = os.path.splitext(p.source_basename)[0]
        tag = O.SDR_TAG if O.SDR_TAG.strip() in os.path.basename(base) else O.DV_TAG
        out = [base + ".mp4", f"{p.nas_dir.rstrip('/')}/{stem}{tag}.mp4"]
        out = [c for c in out if series_mod.is_master_name(os.path.basename(c))]
    seen, keep = set(), []
    for c in out:
        if c not in seen and c not in (new_remote, p.nas_source):
            seen.add(c)
            keep.append(c)
    return keep


def _supersede_legacy(p, new_remote: str) -> str:
    """After the new master is VERIFIED on the NAS: delete this item's earlier .mp4 master
    (legacy_master_candidates) so Plex does not show two versions, and drop its history row.
    Best-effort — a failure here never fails the upload; the note says what happened."""
    cands = legacy_master_candidates(p, new_remote)
    if not cands:
        return ""
    gone, why = transfer.supersede_masters(new_remote, p.final, cands)
    if gone:
        import history
        for g in gone:
            history.forget(g)
        logbook.event(f"upload {p.ep}: superseded the earlier master(s) "
                      f"{', '.join(os.path.basename(g) for g in gone)} with the Matroska one")
    return f"; {why}" if why else ""


def _cleanup(p, abort, progress=None):
    """Verified upload done -> PERMANENTLY delete ALL local working files. os.remove
    erases them (never Trash), so the ~300 GB/episode of ProRes can't accumulate and
    fill the disk. If ANY file can't be removed it is reported and the stage fails —
    a silently-undeleted file is exactly what would stop the pipeline indefinitely."""
    # Also sweep the intermediate TEMP files a crash could orphan (a Topaz encode renames
    # base.part.ext → prores on success; a remux writes final.tracks.mka and final.part). They
    # aren't in working_files() (so stage_done/resume stays simple), but they'd silently eat
    # ~hundreds of GB if left behind, so cleanup removes them best-effort.
    try:
        # BOTH master names around the switch to Matroska (2026-10-18): the other one's transients,
        # and the other master itself when it is not the one that shipped (an unadopted pre-switch
        # .mp4, or an .mkv attempt begun before a later resume adopted the .mp4)
        stem = os.path.splitext(p.final)[0]
        other = stem + (".mp4" if str(p.final).lower().endswith(".mkv") else ".mkv")
        if os.path.exists(other):
            os.remove(other)
        shutil.rmtree(other + ".remuxsegs", ignore_errors=True)
        for base in (p.final, other):
            for sfx in (".part",            # the mkvmerge mux, before it passed its gates
                        ".tracks.mka",      # remux temp: audio + subtitles for the mux
                        ".capped.hevc",     # remux temp: the concat of the segments
                        ".src.hevc",        # inject temp: source Annex-B ES
                        ".src.hevc.vui",    # ...its VUI rewrite (dvcap.ensure_pq_vui)
                        ".ship.hevc.vui",
                        ".inject.hevc",     # inject temp: RPU-injected ES
                        ".fix.hevc",        # inject temp: an inline-P7 winner through nalfix
                        ".ship.hevc",       # ship-render temp: the render's ES
                        ".tracks.mp4",      # the pre-Matroska build's (2026-10-18) temps, left
                        ".dv.mp4"):         # by an item in flight across that deploy
                if os.path.exists(base + sfx):
                    os.remove(base + sfx)
        if os.path.exists(p.dv_render + ".best"):       # the re-export gamble's kept first render
            os.remove(p.dv_render + ".best")            # (its own finally clears it; a kill does not)
        # The pre-Matroska build's mux hardlinked unsafe paths as _mp4box_*; a kill could strand
        # one, keeping the multi-GB transient it points at ALIVE — swept while any may remain.
        import glob
        for stray in glob.glob(os.path.join(os.path.dirname(p.final) or ".", "_mp4box_*")):
            try: os.remove(stray)
            except OSError: pass
    except OSError:
        pass
    try:                                                 # a native render's books (<dv_render>.dvrender/)
        import nativerender                              # and its finish-step strays — never the render
        nativerender.discard_books(p)                    # itself
    except Exception:  # noqa: BLE001 — best-effort, like the strays above
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
