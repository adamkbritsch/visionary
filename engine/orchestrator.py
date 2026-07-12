"""Overnight orchestrator — runs the selected series episode-by-episode:

    download -> topaz -> resolve -> remux -> upload -> cleanup

Design guarantees (per the user's spec):
  * ONE download, REUSED. The source is fetched once to scratch; Topaz AND the
    remux both read that same local file — never re-downloaded.
  * Every stage is RESUMABLE. A stage's output is validated, so an interrupted
    stage (e.g. stopped at the stop-time mid-Topaz) simply re-runs from its start
    when restarted — it picks up at the stage it was in, not the whole pipeline.
  * Topaz -> Resolve is WIRED: the ProRes output is the Resolve stage's input.
  * Per-show Topaz PROFILE: the Topaz stage uses the selected show's saved upscale
    profile (settings.show_profile_or_default) — configured once per show in the UI.
  * CLEANUP on success. After a verified upload+replace, ALL local working files
    for the episode are deleted, so the process runs indefinitely.
  * You press START any time; it runs on adequate AC and auto-STOPS at the next
    stop-time (default 9 AM, adjustable in Settings) — no fixed start window.

The heavy stage runners call the real implementations (transfer/topaz/resolve/
remux); the pure planning logic (paths, stage-done detection, gating) is unit-tested.
"""
from __future__ import annotations
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, asdict, field

import logbook
import movies
import power
import scratch
import series
import settings
import transfer
import youtube

FFPROBE = "/opt/homebrew/bin/ffprobe"
STAGES = ["download", "topaz", "resolve", "remux", "upload", "cleanup"]
# OVERLAP SPLIT: the run thread owns the stages that need the GPU and/or the screen; the
# FINISHER thread owns the headless tail (CPU/network only), so item N's ~75-min x265 remux
# runs WHILE item N+1 downloads/upscales — the peak-cap re-encode costs ~zero wall-clock.
RUN_STAGES = ["download", "topaz", "resolve"]
FINISH_STAGES = ["remux", "upload", "cleanup"]
OVERLAP_MIN_PHYS_GB = 400    # while an item finishes in the background (remux/upload), gate the NEXT
                             # item's start on RAW physical free. Its topaz intermediate was dropped at
                             # hand-off so the finisher item is small (~10 GB), but available_gb still
                             # counts scratch as reclaimable — so gate on physical free to reserve room
                             # for the NEXT item's own intermediate. Room for one movie ProRes + margin.
STATE_FILE = os.path.expanduser("~/.topaz-pipeline/run-state.json")
DIM_IDLE_MINUTES_DEFAULT = 15   # fallback for the 'dim_after_minutes' setting (idle this long → backlight 0)
DRAIN_PCT_THRESHOLD = 5      # pause once the battery drains more than this % below where draining began
DRAIN_POLL_SECONDS = 30      # how often to re-check while paused for power
UNPLUG_GRACE_SECONDS = 60    # unplugged mid-stage (ANY stage, incl. remux): wait this long for the
                             # adapter to return before pausing. The remux used to get a 30-min
                             # cushion because its x265 pass wasn't resumable — now it's segmented +
                             # on the durable work-list, so a kill loses ≤1 short segment and resumes,
                             # exactly like topaz. So it pauses at the same 60 s cutoff (user-dictated).
YT_REFRESH_SECONDS = 300     # re-scan youtarr's staging this often mid-run → new downloads join the
                             # upscale queue live (the queue keeps growing while the user is away)
MIN_FREE_GB = 400            # floor kept free before starting an item + by the prefetcher. The topaz
                             # ProRes intermediate is now DROPPED at hand-off — right after Resolve's
                             # export (_drop_topaz_intermediates) — so only ONE item's intermediate is
                             # ever on disk at a time; the previous item, now remuxing, holds ~10 GB.
                             # Sized for that single peak: one feature-length MOVIE ProRes (~245 GB) +
                             # its source/CFR/render (~10 GB) + ~145 GB OS/FS margin ≈ 400. (A TV
                             # episode's intermediate is only ~140 GB — measured — so this is generous
                             # for the current all-TV workload.) Was 600 back when topaz lingered into
                             # the finisher and TWO intermediates could coexist during a tail-overlap.
                             # Keep >= OVERLAP_MIN_PHYS_GB (the finisher-overlap gate reuses this floor).
MOVIE_TURN_SECONDS = 90 * 60  # a movie OR a long YouTube video runs at most this long per TURN —
                              # then one TV episode runs, then it resumes (segments + completed
                              # stages survive between turns). Keeps a 5-6h feature or a slow
                              # 4K-SDR video from monopolizing the queue.
CADENCE_FILE = os.path.expanduser("~/.topaz-pipeline/orch_cadence.json")
                              # the YouTube cadence counter + movie-turn deferral PERSIST here:
                              # they're facts about processing HISTORY (episodes since the last
                              # video; items owed before a movie resumes), so a re-arm or app
                              # relaunch must NOT reset them — resetting is what starved YouTube
                              # (every deploy/self-arm zeroed the counter before it reached N).
FINISHER_FILE = os.path.expanduser("~/.topaz-pipeline/finisher_queue.json")
                              # DURABLE finisher work-list: every item HANDED OFF to the finisher
                              # (remux/upload/cleanup) is recorded here and removed only when it
                              # terminally completes (uploaded) or parks. The finisher's queue is
                              # otherwise in-memory, so a deactivate/relaunch that hit mid-remux —
                              # before a segment save state existed — used to DROP the item: only the
                              # run thread came back and it grabbed the next topaz item, forgetting the
                              # aborted remux. On re-arm we RECONCILE from this file so the finisher
                              # RESUMES that remux on its own thread, concurrently with the run thread's
                              # topaz ("resume with both") — see _finisher_reconcile.
PREFETCH_HARD_CAP_GB = 100   # HARD ceiling on the prefetch buffer's total size — never stage more than
                             # this much queued content (the soft limit — _reclaim_for_pipeline purging
                             # the buffer when the pipeline needs disk — still applies on top of it)
PREFETCH_GATE_GB = MIN_FREE_GB + 30   # prefetch a source only while free stays above this (small margin
                             # over the floor so prefetched sources never eat into the intermediate reserve)
MAX_EPISODE_FAILS = 5        # consecutive GENUINE failures of one episode → park it, move on


# ---- pure: file/path plan for one episode --------------------------------

@dataclass
class EpisodePaths:
    series: str
    ep: str                 # "S02E10"
    source_basename: str    # "...(Extended Cut).mp4"
    source: str             # local 1080p source (download out; download-complete proof)
    source_cfr: str         # source re-encoded to CONSTANT frame rate (Topaz/Resolve/remux IN)
    prores: str             # legacy single-file Topaz out name (now only the segdir stem)
    segdir: str             # Topaz output: scene-cut ProRes chunks + manifest (Resolve IN)
    dv_render: str          # Resolve mute DV .mov (remux IN)
    final: str              # remuxed master .mp4 (upload IN)
    nas_dir: str            # NAS FTP dir for this season (YouTube: the Plex-lib video folder)
    nas_source: str         # NAS FTP path of the 1080p source (YouTube: in the STAGING lib)
    nas_final: str          # NAS FTP path of the finished master (YouTube: in the Plex lib)
    youtube: bool = False   # YouTube folder-split: publish master to nas_final (keeps source stem so
                            # the .nfo matches), copy sidecars from sidecar_dir, then purge staging
    movie: bool = False     # a Movie-library item (vs a TV episode). Set at build time so post-upload
                            # bookkeeping is driven by the ITEM, not by whether it's still in the queue —
                            # a movie removed mid-pipeline must NOT be miscounted as a TV episode.
    title: str = ""         # clean display title (movies + YouTube videos) — for the "now processing" header
    sidecar_dir: str = ""   # YouTube: the STAGING video folder — sidecars copy from here, purged after

    def item_view(self) -> dict:
        """The currently-processing item as an up-next-shaped dict, so the dashboard header shows what's
        ACTUALLY running (a YouTube video as its channel+title, a movie as its title) — never inferred
        from the up-next preview, which can disagree at cadence/turn boundaries."""
        if self.youtube:
            return {"kind": "youtube", "channel": self.series, "name": self.source_basename,
                    "title": transfer.display_name(self.title or self.source_basename)}
        if self.movie:
            return {"kind": "movie", "name": self.source_basename,
                    "title": transfer.display_name(self.title or self.series)}
        return {"kind": "episode", "ep": self.ep, "series": self.series,
                "source_name": transfer.display_name(self.source_basename)}   # display-parsed only

    def working_files(self) -> list:
        """Everything created locally for this episode — deleted on cleanup."""
        return [self.source, self.source_cfr, self.prores, self.dv_render, self.final]


def episode_paths(series_name, ep, source_basename, *,
                  scratch_dir=None, nas_tv_root=None) -> EpisodePaths:
    scratch_dir = scratch_dir or scratch.default_scratch()
    nas_tv_root = nas_tv_root or transfer.NAS_FTP_TV_ROOT
    stem = os.path.splitext(source_basename)[0]          # "...(Extended Cut)"
    season = "S" + ep[1:3]                               # "S02E10" -> "S02"
    nas_dir = f"{nas_tv_root.rstrip('/')}/{series_name}/{season}"
    j = lambda n: os.path.join(scratch_dir, n)
    return EpisodePaths(
        series=series_name, ep=ep, source_basename=source_basename,
        source=j(source_basename),
        # The CFR re-encode of the source (constant frame rate, same rate) — an INTERMEDIATE,
        # not a deliverable, so no "upscaled" tag; Topaz/Resolve/remux all read this file. The
        # container here (and for final/nas_final) is a pre-download DEFAULT — orchestrator.
        # apply_container() rewrites it to .mkv/.mp4 once the downloaded source is probed.
        source_cfr=j(stem + "_cfr.mp4"),
        # Every PRODUCED file carries an "upscaled" tag. "HDR10 DV" stays in the deliverable
        # names too — the queue's done-detection (_DV_MARK) and the app's Outputs panel both
        # match on it, so it must remain.
        prores=j(stem + "_prob4_upscaled.mov"),
        segdir=j(stem + "_prob4_upscaled.segments"),   # Topaz chunks + manifest live here
        dv_render=j(stem + " HDR10 DV upscaled.mov"),
        final=j(stem + " HDR10 DV upscaled.mp4"),
        nas_dir=nas_dir,
        nas_source=f"{nas_dir}/{source_basename}",
        nas_final=f"{nas_dir}/{stem} HDR10 DV upscaled.mp4",
    )


def movie_paths(source_basename, nas_dir, title=None, *, scratch_dir=None) -> EpisodePaths:
    """Paths for a MOVIE (Movie mode). Same shape as episode_paths but FLAT: a movie's NAS
    dir is wherever it lives under /Media/Movies (a per-movie subfolder OR the root), not a
    series/season tree. series = the movie TITLE so its Topaz preset is set/read per-movie
    (settings.show_preset_key(title), just like a show); ep = the filename stem (the parking
    / fail-count / display id). The generic stages then process it exactly like an episode."""
    scratch_dir = scratch_dir or scratch.default_scratch()
    stem = os.path.splitext(source_basename)[0]
    nas_dir = nas_dir.rstrip("/")
    j = lambda n: os.path.join(scratch_dir, n)
    return EpisodePaths(
        series=(title or stem), ep=stem, source_basename=source_basename,
        source=j(source_basename),
        # container is a pre-download default; apply_container() rewrites it post-probe.
        source_cfr=j(stem + "_cfr.mp4"),
        prores=j(stem + "_prob4_upscaled.mov"),
        segdir=j(stem + "_prob4_upscaled.segments"),
        dv_render=j(stem + " HDR10 DV upscaled.mov"),
        final=j(stem + " HDR10 DV upscaled.mp4"),
        nas_dir=nas_dir,
        nas_source=nas_dir + "/" + source_basename,
        nas_final=nas_dir + "/" + stem + " HDR10 DV upscaled.mp4",
        movie=True,
        title=(title or stem),
    )


def youtube_paths(channel, video_path, title=None, *, scratch_dir=None) -> EpisodePaths:
    """Paths for a YOUTUBE video (FOLDER-SPLIT). `video_path` is youtarr's raw mp4 in the STAGING
    library (…/YouTube-raw/<channel>/<video-folder>/<file>). The 4K DV master is PUBLISHED into the
    Plex "YouTube" library at the MIRRORED path (…/YouTube/<channel>/<video-folder>/<same stem>) —
    keeping youtarr's folder + stem so the copied .nfo/.jpg/.srt still match — and the staging folder
    is purged on cleanup, so only finished masters ever land in Plex. `series` = the channel (for its
    Topaz preset), `ep` = the video stem (parking/display id)."""
    from transfer import NAS_FTP_YOUTUBE_STAGING, NAS_FTP_YOUTUBE_ROOT
    scratch_dir = scratch_dir or scratch.default_scratch()
    source_basename = os.path.basename(video_path)
    stem = os.path.splitext(source_basename)[0]
    src_dir = os.path.dirname(video_path)          # the video's STAGING subfolder
    staging_root = NAS_FTP_YOUTUBE_STAGING.rstrip("/")
    rel = src_dir[len(staging_root):].lstrip("/") if src_dir.startswith(staging_root) else os.path.basename(src_dir)
    plex_dir = NAS_FTP_YOUTUBE_ROOT.rstrip("/") + "/" + rel   # mirror under the Plex library root
    j = lambda n: os.path.join(scratch_dir, n)
    return EpisodePaths(
        series=channel, ep=stem, source_basename=source_basename,
        source=j(source_basename),
        source_cfr=j(stem + "_cfr.mp4"),
        prores=j(stem + "_prob4_upscaled.mov"),
        segdir=j(stem + "_prob4_upscaled.segments"),
        dv_render=j(stem + " HDR10 DV upscaled.mov"),
        final=j(stem + " HDR10 DV upscaled.mp4"),   # LOCAL master name
        nas_dir=plex_dir,                           # publish INTO the Plex library
        nas_source=video_path,                      # raw source in STAGING
        nas_final=plex_dir + "/" + source_basename, # keep youtarr's stem (ext locked in apply_container)
        youtube=True,
        title=(title or stem),
        sidecar_dir=src_dir,                        # copy sidecars from here + purge staging on cleanup
    )


class _AnyEvent:
    """Read-only OR over threading.Events — `is_set()` is True if ANY is set. The download +
    CFR stages only poll `.is_set()` on their abort, so this lets a PREFETCH pull honor both the
    run-abort and the Plex-playing abort at once, without touching the foreground download path."""
    def __init__(self, *events):
        self._events = events
    def is_set(self) -> bool:
        return any(e.is_set() for e in self._events)


def _buffer_names(source_basename: str) -> set:
    """The prefetch-buffer filenames that belong to ONE item: its source + its CFR (either container).
    EXACT names (not a `startswith` prefix) so one item's stem can never match another item's file —
    the CFR is always `<stem>_cfr.mp4` or `<stem>_cfr.mkv` (remux.container_ext yields only those)."""
    stem = os.path.splitext(source_basename)[0]
    return {source_basename, stem + "_cfr.mp4", stem + "_cfr.mkv"}


def discard_workfiles(source_basename: str) -> None:
    """Delete every LOCAL working file of an item REMOVED from its queue (e.g. a movie taken
    out mid-deferral between its 90-min turns) — without this, a part-processed feature
    orphans 100+ GB on scratch FOREVER (the cleanup stage only runs as a processed item's
    terminal stage, and next_due never serves a removed item again). SKIPS the currently-
    processing item — its own pipeline owns those files. EXACT names only (both container
    variants), never a stem-prefix match."""
    cur = ORCH.state.get("current") or {}
    if source_basename in (cur.get("name"), cur.get("source_name")):
        return                                   # mid-pipeline: its own cleanup stage handles it
    stem = os.path.splitext(source_basename)[0]
    names = _buffer_names(source_basename) | {
        stem + "_prob4_upscaled.mov",
        stem + " HDR10 DV upscaled.mov",
        stem + " HDR10 DV upscaled.mp4",
        stem + " HDR10 DV upscaled.mkv",
    }
    main = scratch.default_scratch()
    for d in (main, scratch.prefetch_dir()):
        for n in names:
            try:
                os.remove(os.path.join(d, n))
            except OSError:
                pass
    shutil.rmtree(os.path.join(main, stem + "_prob4_upscaled.segments"), ignore_errors=True)


def apply_container(p: EpisodePaths) -> EpisodePaths:
    """Lock the working container to what the LOCAL source needs — MKV for lossless audio (TrueHD/
    DTS-HD MA/PCM/FLAC) or bitmap subtitles (PGS/VOBSUB), else MP4 — by probing p.source, and
    rewrite the three container-dependent paths (source_cfr, final, nas_final) in place. This is
    resume-safe: paths are built (before download) with a '.mp4' default that is never consumed
    until the source + CFR exist, and once the source is on disk the decision is deterministic and
    stable (the source persists until cleanup). No-op until the source exists. Idempotent — called
    at the top of _process (resume: source already present) and inside the download stage (first
    run: source appears mid-stage, before the CFR is written)."""
    if not os.path.exists(p.source):
        return p
    import remux
    return relabel_container(p, remux.container_ext(p.source))


def relabel_container(p: EpisodePaths, ext: str) -> EpisodePaths:
    """Rewrite the three container-dependent paths (source_cfr, final, nas_final) to `ext` in place.
    Split out of apply_container so the FINISHER's durable-resume path can re-apply the SAME container
    the run thread chose (persisted as container_ext) WITHOUT re-probing the source — otherwise a
    resumed remux rebuilds an MKV item as .mp4, silently downgrading lossless audio / dropping bitmap
    subs and orphaning the real .mkv partial + '.mkv.remuxsegs' resume dir (review-caught HIGH)."""
    stem = os.path.splitext(p.source_basename)[0]
    d = os.path.dirname(p.source)
    p.source_cfr = os.path.join(d, stem + "_cfr" + ext)
    p.final = os.path.join(d, stem + " HDR10 DV upscaled" + ext)
    if p.youtube:                                   # keep youtarr's stem so the copied .nfo matches
        p.nas_final = f"{p.nas_dir}/{stem}{ext}"
    else:
        p.nas_final = f"{p.nas_dir}/{stem} HDR10 DV upscaled{ext}"
    return p


# ---- pure: gating + stop-time --------------------------------------------

def gate_state(reading, now_time=None, *, watts=None, min_watts=140) -> dict:
    """Power gating only (pure). Sufficiency = THE POWER BRICK: connected to an adapter of
    at least `min_watts` (the 140 W MacBook brick) → sufficient, full stop — battery drain
    on a big brick is normal under load and does NOT pause. A lesser brick (hub/monitor
    USB-PD) or battery → insufficient. The time bound is the stop-time, handled per-run."""
    on_ac = bool(reading.external_connected)
    adequate = on_ac and (watts or 0) >= min_watts
    return {"on_ac": on_ac, "adequate": adequate, "runnable": adequate}


def drain_gate(*, on_ac, draining, battery_pct, baseline, pause_on_drain=True,
               threshold=DRAIN_PCT_THRESHOLD):
    """PURE (unit-tested). Pause only after the battery drains MORE than `threshold`
    percent (default 5) below the level where the current drain began, while on AC.
    Charging — or merely holding steady, INCLUDING macOS smart-charging capping at
    80% — resets the baseline to the current level, so sitting at the 80% cap is never
    read as a drain. `draining` is the amperage-based discharge flag; `battery_pct` is
    the current charge. Returns (status, baseline): status 'run' or 'pause'; baseline
    is the charge level the current drain episode started from (None when not draining)."""
    if not on_ac:
        return "pause", None
    if not draining or not pause_on_drain:
        return "run", battery_pct          # charging / holding (e.g. 80% cap) → reset baseline
    base = battery_pct if baseline is None else baseline
    if base - battery_pct > threshold:
        return "pause", base
    return "run", base                      # draining but ≤ threshold so far — keep running


def unplug_decision(*, on_ac, stage_active, unplug_since, now,
                    grace=UNPLUG_GRACE_SECONDS):
    """PURE (unit-tested). Mid-stage AC-unplug grace: don't pause the instant the power
    is pulled — give a countdown so a brief unplug/re-plug doesn't kill the stage.
    Returns (action, unplug_since, remaining):
      'clear' — no active stage, or still/again on AC: reset the countdown
      'count' — unplugged with a stage running, within grace: announce `remaining` s
      'pause' — grace expired while unplugged: abort the stage so the run pauses"""
    if not stage_active or on_ac:
        return "clear", None, 0
    started = now if unplug_since is None else unplug_since
    remaining = int(round(grace - (now - started)))
    if remaining > 0:
        return "count", started, remaining
    return "pause", None, 0


def resolve_must_wait(finishing, queued: int) -> bool:
    """PURE. The next item may NOT start its Resolve while the previous item's remux is in
    flight (finisher stage == remux) or still queued to start (user-dictated): Resolve is the
    one stage that can't be paced or interrupted — its render+DV analyze would crater the
    un-resumable x265 far worse than topaz does. Upload/cleanup are light: no need to wait."""
    return (finishing or {}).get("stage") == "remux" or queued > 0


def grace_label(seconds: int) -> str:
    """'42s' under 2 min, else '29m' — the unplug countdown (now ≤60 s for every stage)."""
    return f"{seconds // 60}m" if seconds >= 120 else f"{seconds}s"


# ---- ffprobe validation (stage-done detection) ---------------------------

def _vstream(path):
    if not (path and os.path.exists(path) and os.path.getsize(path) > 1_000_000):
        return None
    try:
        out = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-of", "json", path],
                             capture_output=True, text=True, timeout=60).stdout
        streams = json.loads(out).get("streams", [])
    except Exception:
        return None
    return streams or None


def _is_dv81(streams) -> bool:
    for s in streams or []:
        if s.get("codec_type") == "video":
            for sd in s.get("side_data_list", []):
                if sd.get("side_data_type") == "DOVI configuration record":
                    return sd.get("dv_profile") == 8 and sd.get("dv_bl_signal_compatibility_id") == 1
    return False


def _has_audio(streams) -> bool:
    return any(s.get("codec_type") == "audio" for s in streams or [])


# ---- stage-done detection (resume) ---------------------------------------

def stage_done(stage, p: EpisodePaths, *, ftp=None) -> bool:
    if stage == "download":
        # Done = the original is verified complete (size == the NAS file) AND its
        # constant-frame-rate re-encode is present (that's what every later stage reads).
        import topaz
        return (os.path.exists(p.source)
                and os.path.getsize(p.source) == _remote_size(p.nas_source, ftp)
                and topaz.is_cfr_ready(p.source_cfr))
    if stage == "topaz":
        # Done = every scene-cut chunk is present with its exact frame count (per the
        # manifest) — OR a VALID DV RENDER already exists: the segments are DROPPED at
        # hand-off to free ~120-300 GB, so an item whose remux later aborts must resume at
        # remux, not re-upscale for 2 h (live-hit: S06E03, 2026-07-06 14:56).
        import topaz
        if _is_dv81(_vstream(p.dv_render)):
            return True
        return topaz.segments_complete(p.segdir)
    if stage == "resolve":
        return _is_dv81(_vstream(p.dv_render))
    if stage == "remux":
        s = _vstream(p.final)
        return _is_dv81(s) and _has_audio(s)
    if stage == "upload":
        return os.path.exists(p.final) and _remote_size(p.nas_final, ftp) == os.path.getsize(p.final)
    if stage == "cleanup":
        return not any(os.path.exists(f) for f in p.working_files())
    return False


def _first_video(streams):
    for s in streams or []:
        if s.get("codec_type") == "video":
            return {"codec": s.get("codec_name"), "profile": s.get("profile"),
                    "width": s.get("width"), "height": s.get("height")}
    return {}


def _remote_size(remote_path, ftp):
    try:
        own = ftp is None
        ftp = ftp or transfer.connect()
        try:
            return transfer.remote_size(ftp, remote_path)
        finally:
            if own:
                try: ftp.quit()
                except Exception: pass
    except Exception:
        return None


def first_incomplete_stage(p: EpisodePaths, *, ftp=None) -> str | None:
    """The stage to (re)run — resume point. None = episode fully done."""
    for st in STAGES:
        if not stage_done(st, p, ftp=ftp):
            return st
    return None


# ---- per-(item, stage) elapsed clock -------------------------------------
# A stage's live "elapsed" must be the REAL wall time spent in that exact stage of that exact item,
# accumulated across however the work got split up — a stop/deactivation, a movie-turn reprioritization,
# a pause, or a server restart. So it's persisted per (source_basename | stage): resuming that stage
# LOADS the accumulated total and keeps counting, instead of restarting at 0 (which read "12s" after a
# pause near the end). Cleared when the stage finishes so a future re-run starts fresh.
ELAPSED_FILE = os.path.expanduser("~/.topaz-pipeline/elapsed.json")


def _elapsed_map() -> dict:
    try:
        with open(ELAPSED_FILE) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _elapsed_write(m) -> None:
    try:
        os.makedirs(os.path.dirname(ELAPSED_FILE), exist_ok=True)
        with open(ELAPSED_FILE, "w") as f:
            json.dump(m, f)
    except OSError:
        pass


# ELAPSED_FILE is a read-modify-write shared by TWO stage-running threads now (run + finisher) —
# serialize the RMW so one thread's checkpoint can't drop the other's key.
_ELAPSED_LOCK = threading.Lock()


# ---- orchestrator (loop + thread + live state) ---------------------------

class Orchestrator:
    def __init__(self):
        self._enabled = False
        self._thread = None
        self._lock = threading.Lock()
        self._abort = threading.Event()        # set when the stop-time hits mid-stage
        self._caffeinate = None                # held while running: display+system awake
        self._drain_baseline = None            # battery % the current drain episode began at
        self._power_paused = False             # published by _run (single-writer): the prefetcher reads it
                                               # so its CFR encodes don't drain the battery mid-pause
        self._plex_playing = False             # FAILSAFE: True while any Plex client is streaming — the
                                               # prefetcher backs off its NAS pulls so precache I/O can't
                                               # stutter a live playback (see _plex_monitor)
        self._plex_abort = threading.Event()   # set with _plex_playing → aborts an in-flight prefetch pull
        self._prefetch_yield = threading.Event()   # the RUN thread sets this to make the prefetcher abort a
                                               # background pull of the item the run thread is about to process
                                               # itself (at normal priority) — so the overlap never waits out a
                                               # slow low-prio prefetch of its own current item (see _claim_prefetched)
        self._current_skip_key = None          # skip-key of the item the run thread is processing NOW — the
                                               # prefetcher excludes it so it never competes for the current item
        self._plex_errs = 0                    # consecutive Plex-check failures (tolerate a couple of blips)
        self._stage_active = False             # True only while a heavy stage is executing
        self._progress_key = None              # (stage, ep) the current progress belongs to
        self._progress_start = 0.0             # when the CURRENT ETA window began
        self._progress_start_pct = 0.0         # the % already done when that window began (0 on a fresh stage)
        self._run_contended = False            # was the finisher's remux running at topaz's last tick?
        self._fin_contended = False            # was the run thread's topaz running at the remux's last tick?
                                               # (both: overlap changes the shared-power rate → re-anchor
                                               #  the surviving stage's ETA window when the other ends/starts)
        # per-(item, stage) elapsed accumulator (survives stop / turn / pause / restart — see _elapsed_*)
        self._elapsed_key = None               # f"{source_basename}|{stage}" of the running stage, or None
        self._elapsed_base = 0.0               # accumulated seconds BEFORE the current interval
        self._elapsed_anchor = None            # monotonic() when the current interval began (None = paused)
        self._elapsed_last_save = 0.0          # monotonic() of the last periodic checkpoint
        # FINISHER (remux/upload/cleanup overlap thread) — its own abort + elapsed context so the
        # two stage-running threads never share single-slot state (see _finisher / FINISH_STAGES)
        self._finish_q = queue.Queue()         # EpisodePaths handed off after their resolve completes
        self._finish_abort = threading.Event() # aborts the finisher's CURRENT stage (disable / power pause)
        self._in_finisher = set()              # _skip_key(p) of queued+in-flight finisher items — excluded
        self._finisher_lock = threading.Lock() # from selection so the run thread can't re-pick them
        self._fin_el_key = None                # finisher's own elapsed slot (mirrors _elapsed_* above)
        self._fin_el_base = 0.0
        self._fin_el_anchor = None
        self._fin_el_save = 0.0
        self._in_finisher_movies = set()       # finisher items that are MOVIES (bigger resident set →
                                               # the overlap disk gate demands more physical headroom)
        self._finisher_persisted = self._load_finisher_persisted()   # cid -> descriptor; DURABLE finisher
                                               # ownership (survives deactivate/relaunch mid-remux). Seeded
                                               # back into _finish_q/_in_finisher by _finisher_reconcile.
        self._cadence_advanced = set()         # items whose HAND-OFF already advanced the cadence —
                                               # loaded below with the counters (survives restarts)
        self._fin_eta_anchor = None            # (stage, monotonic0, frames0) of THIS attempt — the
                                               # lane ETA must use the live rate, never the
                                               # accumulated elapsed (it spans killed attempts)
        self._cadence_lock = threading.Lock()  # cadence counters + CADENCE_FILE writes: the run thread
                                               # advances them at hand-off, the finisher's park path can
                                               # still reset _tv_since_yt — one lock, no torn RMW/tmp-file
        self._progress_last = 0.0              # time of the last progress update (to spot a stop/resume gap)
        self._parked = set()                   # episodes skipped after repeated failures (this run)
        self._resolve_deferred = set()         # items topaz'd but held before Resolve by QUIET MODE (in-memory;
                                               # self-heals each run — re-encountered items re-add themselves)
        self._fail_counts = {}                 # ep -> consecutive genuine-failure count
        self._rr = 0                           # round-robin pointer over the active TV series
        c = self._load_cadence()
        self._yt_wait = c["yt_wait"]           # YouTube TURN deferral (same as movies: a >90-min video
                                               # postpones, one episode runs, then it resumes)
        self._movie_wait = c["movie_wait"]     # movie TURN deferral: >0 = this many other items must
                                               # finish before a movie is due again (set to 1 when a
                                               # movie's 90-min turn ends; the movie then resumes)
        self._tv_since_yt = c["tv_since_yt"]   # TV episodes completed since the last YouTube video — the
                                               # cadence counter: at >= youtube_every_tv_episodes, 1 YT
                                               # video is served next, then this resets to 0
        self._cadence_advanced.update(c.get("advanced", []))   # once-per-item hand-off guard survives
                                               # restarts (else a relaunch re-counts the in-finisher item)
        self._yt_refresh_at = 0.0              # monotonic time of the next live staging re-scan
        self._yt_meta_done = False             # did this run do its one-time full popular/meta refresh?
        self._dl_guard = threading.Lock()      # guards _dl_locks
        self._dl_locks = {}                    # source path -> Lock: the prefetcher + the foreground never
                                               # download the same item at once (a size-verify corruption race)
        self._threads = {}                     # name -> Thread, so enable() can (re)start any that died
        self.state = {"enabled": False, "running": False, "episode": None,
                      "stage": None, "message": "idle", "ended_reason": None,
                      "progress": None, "current": None, "plex_playing": False,
                      "finishing": None}   # {"ep","stage","pct",...} while the finisher works an item

    def _elapsed_begin(self, key):
        """Start OR resume a stage's elapsed clock: load its accumulated total and anchor a fresh
        interval, so a stage that already ran (then was stopped / reprioritized / paused) picks up
        where it left off instead of restarting at 0."""
        self._elapsed_key = key
        with _ELAPSED_LOCK:   # the finisher truncate-writes this file — never read it torn
            self._elapsed_base = float(_elapsed_map().get(key, 0.0))
        self._elapsed_anchor = time.monotonic()
        self._elapsed_last_save = self._elapsed_anchor

    def _elapsed_value(self) -> float:
        if self._elapsed_anchor is None:
            return self._elapsed_base
        return self._elapsed_base + (time.monotonic() - self._elapsed_anchor)

    def _elapsed_pause(self):
        """Stage interrupted (stop / deadline / abort / fail) → fold the open interval into the base
        and persist, so the NEXT run of this stage resumes from here (not from 0)."""
        if self._elapsed_key is None or self._elapsed_anchor is None:
            return
        self._elapsed_base = self._elapsed_value()
        with _ELAPSED_LOCK:
            m = _elapsed_map(); m[self._elapsed_key] = round(self._elapsed_base, 1); _elapsed_write(m)
        self._elapsed_anchor = None

    def _elapsed_done(self, key):
        """Stage finished → drop its accumulated timer so any future re-run of it starts fresh."""
        with _ELAPSED_LOCK:
            m = _elapsed_map()
            if key in m:
                del m[key]; _elapsed_write(m)
        if self._elapsed_key == key:
            self._elapsed_key = None; self._elapsed_anchor = None

    # ---- the FINISHER's own elapsed slot (never shares the run thread's single-slot state) ----

    def _fin_elapsed_begin(self, key):
        self._fin_el_key = key
        with _ELAPSED_LOCK:
            self._fin_el_base = float(_elapsed_map().get(key, 0.0))
        self._fin_el_anchor = time.monotonic()
        self._fin_el_save = self._fin_el_anchor

    def _fin_elapsed_value(self) -> float:
        if self._fin_el_anchor is None:
            return self._fin_el_base
        return self._fin_el_base + (time.monotonic() - self._fin_el_anchor)

    def _fin_elapsed_pause(self):
        if self._fin_el_key is None or self._fin_el_anchor is None:
            return
        self._fin_el_base = self._fin_elapsed_value()
        with _ELAPSED_LOCK:
            m = _elapsed_map(); m[self._fin_el_key] = round(self._fin_el_base, 1); _elapsed_write(m)
        self._fin_el_anchor = None

    def _fin_elapsed_done(self, key):
        with _ELAPSED_LOCK:
            m = _elapsed_map()
            if key in m:
                del m[key]; _elapsed_write(m)
        if self._fin_el_key == key:
            self._fin_el_key = None; self._fin_el_anchor = None

    def _set_progress(self, info):
        """Live sub-stage progress for the dashboard, plus an ETA (seconds left). The ETA is a
        linear extrapolation over the CURRENT measurement window — `elapsed × remaining / advanced`,
        where `advanced` is the % gained SINCE the window anchored, not the absolute %. That detail
        is what keeps the ETA sane across a stop/resume: a resumable Topaz encode instantly REPLAYS
        its finished chunks on resume (pct leaps with no real-time work), and a stop leaves an idle
        gap — both would wreck a from-zero `elapsed × remaining / pct`."""
        now = time.time()
        key = (info.get("stage"), info.get("ep"))
        pct = info.get("pct") or 0
        gap = now - self._progress_last if self._progress_last else 1e9
        # (Re)anchor the window on a new stage/episode OR after a real gap (a stop/pause then
        # resume) — so `elapsed` never spans idle time and the rate isn't measured against
        # pre-resume progress.
        if key != self._progress_key or gap > 30:
            self._progress_key = key
            self._progress_start = now
            self._progress_start_pct = pct
        self._progress_last = now
        elapsed = now - self._progress_start
        advanced = pct - self._progress_start_pct
        # While Topaz is REPLAYING finished chunks on resume, progress is implausibly fast
        # (>0.5%/s ≫ a real upscale's ~0.05%/s) — keep re-anchoring to `now` so the instant leap
        # doesn't poison the rate. (A fresh stage has start_pct=0 → advanced=pct → identical to
        # the old formula; only a resume makes start_pct>0.)
        if info.get("stage") == "topaz" and advanced > 0 and elapsed > 0 and advanced / elapsed > 0.5:
            self._progress_start = now
            self._progress_start_pct = pct
            elapsed, advanced = 0.0, 0.0
        # CONTENTION-AWARE ETA: topaz shares the M3 Max's power envelope with the finisher's remux, so
        # it SPEEDS UP the moment that remux ends (and slows when one starts). Re-anchor the rate window
        # on that transition so the ETA tracks the NEW rate instead of averaging across both regimes.
        if info.get("stage") == "topaz":
            remux_on = ((self.state.get("finishing") or {}).get("stage") == "remux")
            if remux_on != self._run_contended:
                self._run_contended = remux_on
                self._progress_start, self._progress_start_pct = now, pct
                elapsed, advanced = 0.0, 0.0
        if advanced > 0 and elapsed > 8:        # let a few seconds of real progress accrue first
            info["eta_secs"] = elapsed * (100 - pct) / advanced
            # Per-SEGMENT eta (topaz): same windowed rate applied to the current segment's
            # remainder, plus the projected average time per segment — the UI shows the
            # segment eta only when segments average >15 min (slow 4K content), where the
            # stage-level eta alone reads as "hours away" with no sense of near-term motion.
            rem = info.get("seg_rem_pct")
            if rem is not None:
                info["seg_eta_secs"] = elapsed * rem / advanced
            if info.get("seg_total"):
                info["avg_seg_secs"] = (elapsed / advanced) * (100 / info["seg_total"])
        if self._elapsed_anchor is not None:    # accumulated real time in THIS stage of THIS item
            info["elapsed_secs"] = self._elapsed_value()
            mono = time.monotonic()
            if mono - self._elapsed_last_save > 30:     # checkpoint so a hard crash loses at most ~30s
                with _ELAPSED_LOCK:
                    m = _elapsed_map(); m[self._elapsed_key] = round(info["elapsed_secs"], 1); _elapsed_write(m)
                self._elapsed_last_save = mono
        self.state["progress"] = info

    def _start_caffeinate(self):
        """Keep the display + system awake for the WHOLE run (not just the resolve
        stage — by the time resolve runs the screensaver would already be active and
        its screencapture would get a black frame). -u wakes the display now, -d
        blocks display sleep + the screensaver, -i blocks idle system sleep. Held
        until disable(). This is the built-in equivalent of Amphetamine."""
        self._stop_caffeinate()
        try:
            self._caffeinate = subprocess.Popen(["caffeinate", "-d", "-i", "-u"],
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            logbook.exception("caffeinate", e)
            self._caffeinate = None

    def _stop_caffeinate(self):
        if self._caffeinate:
            try: self._caffeinate.terminate()
            except Exception: pass
            self._caffeinate = None

    # ---- control ----
    def skip_current(self, source_basename: str) -> bool:
        """Abort the in-flight item if it IS this source (a deleted YouTube video must not
        keep encoding). The loop then re-picks; a deleted/done video is never served again.
        Returns True if an abort was issued."""
        cur = self.state.get("current") or {}
        if source_basename and (cur.get("name") or "") == source_basename:
            self._abort.set()
            return True
        return False

    def reclaim_screen(self) -> bool:
        """QUIET MODE just turned ON: if an item is MID-RESOLVE (Resolve has the screen), abort it so the
        display frees immediately — the resolve subprocess dies within ~5s, quits Resolve, and refocuses
        the app. The item's Topaz output survives; it re-runs Resolve once Quiet Mode is off. Returns True
        if an abort was issued. No-op if we're not currently in the resolve stage."""
        if self._stage_active and self.state.get("stage") == "resolve":
            self._abort.set()
            return True
        return False

    def _load_cadence(self) -> dict:
        try:
            with open(CADENCE_FILE) as f:
                d = json.load(f)
            return {"tv_since_yt": max(0, int(d.get("tv_since_yt", 0))),
                    "movie_wait": max(0, int(d.get("movie_wait", 0))),
                    "yt_wait": max(0, int(d.get("yt_wait", 0))),
                    "advanced": [str(x) for x in d.get("advanced", [])]}
        except (OSError, ValueError, TypeError):
            return {"tv_since_yt": 0, "movie_wait": 0, "yt_wait": 0, "advanced": []}

    def _save_cadence(self):
        """Persist the scheduling history counters (atomic tmp+rename) — call after every
        mutation of _tv_since_yt / _movie_wait so relaunches and re-arms can't reset them."""
        try:
            os.makedirs(os.path.dirname(CADENCE_FILE), exist_ok=True)
            tmp = CADENCE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"tv_since_yt": self._tv_since_yt, "movie_wait": self._movie_wait,
                           "yt_wait": self._yt_wait,
                           "advanced": sorted(self._cadence_advanced)}, f)
            os.replace(tmp, CADENCE_FILE)
        except OSError:
            pass

    # ---- DURABLE finisher work-list (resume a mid-remux item across deactivate/relaunch) -------
    # The finisher's _finish_q / _in_finisher are in-memory. Recorded here at hand-off and cleared
    # only at a TERMINAL state (uploaded, or parked), an item survives a deactivate/relaunch that
    # struck mid-remux — before a segment save state — so _finisher_reconcile re-queues it onto the
    # finisher thread and it resumes CONCURRENTLY with the run thread's next topaz.

    @staticmethod
    def _finisher_descriptor(p: EpisodePaths) -> dict:
        """The minimal, FTP-free identity needed to rebuild an EpisodePaths on resume. For episodes
        we store the resolved nas_tv_root (recovered from p.nas_dir) so reconstruction never has to
        re-list the NAS to find the show's volume. container_ext preserves apply_container's MKV/MP4
        choice — the source's extension carries it (source_cfr/final/nas_final all share it)."""
        ext = os.path.splitext(p.final)[1] or ".mp4"          # apply_container already locked this
        if p.youtube:
            return {"kind": "youtube", "ep": p.ep, "channel": p.series,
                    "video_path": p.nas_source, "title": p.title, "container_ext": ext}
        if p.movie:
            return {"kind": "movie", "ep": p.ep, "source_basename": p.source_basename,
                    "nas_dir": p.nas_dir, "title": p.title, "container_ext": ext}
        tail = f"/{p.series}/S{p.ep[1:3]}"                     # episode_paths built nas_dir this way
        nas_tv_root = p.nas_dir[:-len(tail)] if p.nas_dir.endswith(tail) else transfer.NAS_FTP_TV_ROOT
        return {"kind": "episode", "ep": p.ep, "series": p.series, "source_basename": p.source_basename,
                "nas_tv_root": nas_tv_root, "container_ext": ext}

    @staticmethod
    def _desc_skip_key(d: dict) -> str:
        """The selection skip-key a descriptor maps to — mirrors _skip_key(p): movies key on the
        basename-with-extension, TV/YouTube on the stem (p.ep)."""
        return d.get("source_basename") if d.get("kind") == "movie" else d.get("ep")

    @classmethod
    def _desc_cid(cls, d: dict) -> str:
        """A kind-scoped id so a movie basename can never collide with an episode's 'S06E07'."""
        return str(d.get("kind")) + "|" + str(cls._desc_skip_key(d))

    def _finisher_reconstruct(self, d: dict) -> EpisodePaths | None:
        try:
            k = d.get("kind")
            if k == "youtube":
                p = youtube_paths(d["channel"], d["video_path"], d.get("title"))
            elif k == "movie":
                p = movie_paths(d["source_basename"], d["nas_dir"], d.get("title"))
            elif k == "episode":
                p = episode_paths(d["series"], d["ep"], d["source_basename"],
                                  nas_tv_root=d.get("nas_tv_root"))
            else:
                return None
        except Exception:
            return None
        # Restore the container the run thread chose (MKV for lossless audio / bitmap subs, else MP4).
        # The constructors default to .mp4; without this a resumed remux would rebuild an MKV item as
        # .mp4 and corrupt/orphan it. Prefer the persisted ext; fall back to re-probing the on-disk
        # source (present for any non-terminal item) so a legacy descriptor still resolves correctly.
        ext = d.get("container_ext")
        if ext:
            relabel_container(p, ext)
        else:
            p = apply_container(p)
        return p

    def _load_finisher_persisted(self) -> dict:
        """{cid: descriptor} from FINISHER_FILE; missing/corrupt → empty (a lost work-list just
        falls back to the run thread eventually re-handing the item off — never a crash)."""
        try:
            with open(FINISHER_FILE) as f:
                items = json.load(f)
            out = {}
            for d in items or []:
                if isinstance(d, dict) and d.get("kind") and self._desc_skip_key(d):
                    out[self._desc_cid(d)] = d
            return out
        except (OSError, ValueError, TypeError):
            return {}

    def _save_finisher_persisted_locked(self):
        """Atomic tmp+rename write of the durable work-list. Caller MUST hold _finisher_lock."""
        try:
            os.makedirs(os.path.dirname(FINISHER_FILE), exist_ok=True)
            tmp = FINISHER_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(list(self._finisher_persisted.values()), f)
            os.replace(tmp, FINISHER_FILE)
        except OSError:
            pass

    def _finisher_persist_remove(self, p: EpisodePaths):
        """Drop an item from the durable work-list — a TERMINAL transition only (uploaded / parked)."""
        with self._finisher_lock:
            if self._finisher_persisted.pop(self._desc_cid(self._finisher_descriptor(p)), None) is not None:
                self._save_finisher_persisted_locked()

    def _finisher_persisted_keys(self) -> set:
        with self._finisher_lock:
            return {self._desc_skip_key(d) for d in self._finisher_persisted.values()}

    def finisher_views(self) -> list:
        """The finisher-owned items as up-next-shaped dicts (kind/ep/name), so the dashboard's
        up_next can EXCLUDE them from the queue: their remux/upload hasn't put a DV master on the
        NAS yet, so they still look 'remaining', but they're committed to the pipeline. Built from
        the DURABLE work-list (kind-tagged, survives a relaunch)."""
        with self._finisher_lock:
            descs = list(self._finisher_persisted.values())
        out = []
        for d in descs:
            k = d.get("kind")
            if k == "movie":
                out.append({"kind": "movie", "name": d.get("source_basename"),
                            "title": transfer.display_name(d.get("title") or d.get("source_basename") or "")})
            elif k == "youtube":
                out.append({"kind": "youtube", "channel": d.get("channel"),
                            "name": os.path.basename(d.get("video_path") or ""),
                            "title": transfer.display_name(d.get("title") or "")})
            else:
                out.append({"kind": "episode", "ep": d.get("ep"), "series": d.get("series"),
                            "source_name": transfer.display_name(d.get("source_basename") or "")})
        return out

    def _finisher_reconcile(self):
        """Re-queue every durable finisher item that isn't currently owned (in-flight or queued) onto
        the finisher thread. Idempotent — an already-done item just fast-paths through stage_done and
        self-removes. Called synchronously at enable() (BEFORE the run thread starts, so selection
        already excludes these keys) and on the finisher's idle tick (self-heals the narrow
        disable→enable discard race)."""
        with self._finisher_lock:
            pending = [d for d in self._finisher_persisted.values()
                       if self._desc_skip_key(d) not in self._in_finisher]
        for d in pending:
            p = self._finisher_reconstruct(d)
            if p is None:
                continue
            key = self._skip_key(p)
            with self._finisher_lock:
                if key in self._in_finisher:      # another thread claimed it between the snapshot and now
                    continue
                self._in_finisher.add(key)
                if p.movie:
                    self._in_finisher_movies.add(key)
            self._finish_q.put(p)
            logbook.event(f"finisher resume: {transfer.display_name(p.ep)} re-queued "
                          f"(remux/upload continues on the finisher)")

    def _ensure(self, name, target):
        """(Re)start a daemon worker if it isn't alive. The old code started the helpers
        ONLY when the run thread was dead, so a disable→enable (or a helper that exited)
        could leave the run armed with no watchdog/dimmer/power-monitor — or no run loop."""
        t = self._threads.get(name)
        if not (t and t.is_alive()):
            t = threading.Thread(target=target, daemon=True, name=name)
            self._threads[name] = t
            t.start()

    def enable(self):
        with self._lock:
            self._enabled = True
            self._abort.clear()
            self._drain_baseline = None        # fresh run: never inherit a stale drain baseline
            self._parked.clear()               # a manual Start retries everything, incl. parked eps
            self._fail_counts.clear()
            self._yt_refresh_at = 0.0           # fresh run: re-scan staging + refresh popular sets at once
            self._yt_meta_done = False
            # _tv_since_yt / _movie_wait deliberately NOT reset — they're processing HISTORY
            # (see CADENCE_FILE); resetting them on every re-arm starved the YouTube cadence.
            self._start_caffeinate()           # display + system awake for the whole run
            msg = "started — running until you stop it"   # no auto-stop; ends only on a manual stop
            self.state.update(enabled=True, ended_reason=None, message=msg)
            logbook.event(msg)
            self._plex_abort.clear(); self._plex_errs = 0    # fresh run: re-probe Plex from scratch
            # Auto-dimmer: backlight → 0 after 'dim_after_minutes' idle (0 = Off). It does NOT restore on
            # activity (that felt unreliable) — the user taps the brightness key; we only restore on stop.
            # caffeinate keeps the display logically ON so the resolve stage's screencapture still works.
            self._finish_abort.clear()
            self.state["finishing"] = None
            # RESUME BOTH: re-queue any item whose remux was interrupted mid-flight (durable work-list)
            # onto the finisher BEFORE the run thread starts, so selection already excludes it and the
            # remux resumes in parallel with the next topaz — instead of the item being forgotten.
            self._finisher_reconcile()
            for name, target in (("run", self._run), ("finisher", self._finisher),
                                 ("power_monitor", self._power_monitor), ("dimmer", self._dimmer),
                                 ("prefetch", self._prefetch), ("plex_monitor", self._plex_monitor)):
                self._ensure(name, target)

    def disable(self, reason="disabled by user"):
        with self._lock:
            self._enabled = False
            self._abort.set()
            self._finish_abort.set()           # the finisher's in-flight remux/upload stops too
            self._stop_caffeinate()            # let the display sleep again
            self.state.update(enabled=False, ended_reason=reason)
        try:
            import topaz
            topaz.terminate_all()              # kill any in-flight encode at once (don't orphan it)
        except Exception:
            pass
        logbook.event(f"stopped — {reason}")

    def snapshot(self) -> dict:
        return dict(self.state)

    # ---- loop ----
    def _gate(self):
        return gate_state(power.read_power())

    def _power_ok(self):
        """Returns (status, message); status is 'run' or 'pause'. Sufficiency = the POWER
        BRICK, nothing else: on the >= min_adapter_watts (140 W) adapter → run, even if the
        battery dips under load; a lesser brick or battery → full passive pause (the run
        loop also releases caffeinate, so nothing holds the screen awake while waiting)."""
        r = power.read_power()
        if not r.external_connected:
            return "pause", "paused — on battery (waiting for the 140 W adapter)"
        need = self._min_watts()
        w = power.adapter_watts()
        if (w or 0) >= need:
            return "run", None
        return "pause", (f"paused — {w} W adapter connected, needs {need} W" if w
                         else f"paused — adapter wattage unknown, needs {need} W")

    def _min_watts(self) -> int:
        try:
            return max(1, int(settings.get_settings().get("min_adapter_watts", 140)))
        except (TypeError, ValueError):
            return 140

    def _retry_seconds(self) -> int:
        return int(settings.get_settings().get("poll_minutes", 30)) * 60

    def _dim_after_secs(self) -> int:
        """Live read of the 'dim after N minutes' setting → seconds. 0 = Off (never dim)."""
        try:
            m = int(settings.get_settings().get("dim_after_minutes", DIM_IDLE_MINUTES_DEFAULT))
        except (TypeError, ValueError):
            m = DIM_IDLE_MINUTES_DEFAULT
        return max(0, m) * 60

    def _dimmer(self):
        """While running + caffeinated, drop the display backlight to 0 once the user has been idle
        past the configured 'dim after N minutes' (panel/power saving on an unattended run). The
        display stays logically ON (caffeinate), so screencapture still reads the framebuffer and the
        resolve stage works in the dark. Does NOT auto-restore on activity — the screen stays dark and
        the user taps the brightness key to bring it back (native, instant). We only restore when the
        run ends (the `finally`), and step aside if the user raises the brightness themselves. Only
        runs while caffeinated — the loop condition."""
        import brightness
        saved = None
        try:
            while self._enabled and self._caffeinate is not None:
                cur = brightness.get_brightness()
                action = brightness.dim_tick(brightness.idle_seconds(), self._dim_after_secs(),
                                             cur, saved is not None)
                if action == "dim":
                    saved = cur
                    brightness.set_brightness(0.0)
                elif action == "release":            # user raised it themselves → forget our level
                    saved = None
                time.sleep(10)
        finally:
            if saved is not None:                    # run stopped while WE held it dark → restore
                brightness.set_brightness(saved)

    def _power_monitor(self):
        """Watch for the AC being UNPLUGGED while a stage is running. Don't pause
        instantly — announce a live countdown (UNPLUG_GRACE_SECONDS) so a brief
        unplug/re-plug doesn't abort the stage. If still unplugged when it expires,
        abort the stage so the run pauses (it never runs on battery). Reconnecting
        cancels the countdown and restores the stage's message."""
        unplug_since, held_msg = None, None
        while self._enabled:
            time.sleep(2)
            fin = self.state.get("finishing") or {}
            any_active = self._stage_active or bool(fin)   # the finisher's stage counts too
            if any_active:
                # sufficiency = the brick: unplugged OR a lesser adapter both count as
                # "no power" for the grace countdown → abort → the run pauses passively.
                sufficient = (bool(power.read_power().external_connected)
                              and (power.adapter_watts() or 0) >= self._min_watts())
            else:
                sufficient = True
            action, unplug_since, remaining = unplug_decision(
                on_ac=sufficient, stage_active=any_active,
                unplug_since=unplug_since, now=time.time(),
                grace=UNPLUG_GRACE_SECONDS)      # same 60 s cutoff for every stage, remux included
            if action == "clear":
                if held_msg is not None:
                    self.state["message"] = held_msg
                    held_msg = None
            elif action == "count":
                if held_msg is None:
                    held_msg = self.state.get("message")
                self.state["message"] = (f"Insufficient power — pausing in {grace_label(remaining)} "
                                         "unless the 140 W adapter returns")
            else:  # pause
                self.state["message"] = "paused — insufficient power (needs the 140 W adapter)"
                self._abort.set()
                self._finish_abort.set()       # never keep an x265/upload burning battery either
                held_msg = None

    def _plex_started_now(self) -> bool:
        """A FRESH Plex check at the exact moment before a prefetch pull would start — closes the gap
        where a stream began between 2s polls. True if the cached flag is set OR a live check confirms
        playing; a Plex blip (None) falls back to the cached flag (never blocks on a transient)."""
        if self._plex_playing:
            return True
        try:
            import plex
            if plex.is_playing() is True:
                self._plex_playing = True
                self.state["plex_playing"] = True
                self._plex_abort.set()
                return True
        except Exception:
            pass
        return False

    def _plex_monitor(self):
        """FAILSAFE: while any Plex client is streaming, pause the prefetcher's NAS pulls so precache
        I/O can't stutter a live playback. Sets `_plex_playing` (the prefetch-loop gate) + the
        `_plex_abort` event (aborts an in-flight background pull). Robust to a flaky Plex: a couple
        of failed checks HOLD the last state; a sustained outage decays to 'idle' so the prefetcher
        isn't stuck off when Plex is simply down. Naturally inert when no Plex token is configured
        (is_playing → None forever → treated as idle after the grace count)."""
        import plex
        while self._enabled:
            try:
                p = plex.is_playing()
            except Exception:
                p = None
            if p is None:                               # couldn't reach Plex / no token
                self._plex_errs += 1
                if self._plex_errs < 5:
                    self._sleep(2); continue            # transient blip → hold current state
                p = False                               # sustained → assume nobody's streaming
            else:
                self._plex_errs = 0
            self._plex_playing = bool(p)
            self.state["plex_playing"] = self._plex_playing
            (self._plex_abort.set if p else self._plex_abort.clear)()
            self._sleep(2)                              # fast poll: an in-flight pull aborts within ~2s

    def _run(self):
        self.state["running"] = True
        try:
            while self._enabled:
                self._abort.clear()          # fresh iteration — consume any mid-stage power abort
                self._maybe_resume_deferred()        # Quiet Mode off → resume items held before Resolve
                pstatus, pmsg = self._power_ok()
                self._power_paused = (pstatus == "pause")        # let the prefetcher back off too
                if pstatus == "pause":
                    self._stop_caffeinate()                      # don't hold the display/system awake
                    self.state["message"] = pmsg                 # all night while waiting on power
                    self._sleep(DRAIN_POLL_SECONDS); continue    # re-check soon to resume on recovery
                if self._caffeinate is None:                     # resumed from a power pause → re-hold
                    self._start_caffeinate()
                if (dmsg := self._low_disk_pause()) is not None:   # not enough room to start an item
                    self.state.update(stage=None, message=dmsg)
                    self._sleep(DRAIN_POLL_SECONDS); continue
                if self._finish_q.qsize() > 0:   # BACKPRESSURE: an item is WAITING behind the one the
                    # finisher is working — starting another would put a 3rd working set on scratch
                    fin = self.state.get("finishing") or {}
                    self.state.update(stage=None, current=None,
                        message=f"holding the next item — finisher backlog ({fin.get('ep') or 'queued'} still finishing)")
                    self._sleep(15); continue
                self._refresh_youtube()                         # pick up whatever youtarr downloaded since
                ep, why = self._next_episode()
                if ep is None:
                    self.state["current"] = None                 # nothing processing → header falls back to up-next
                    if why == "unreachable":     # NAS listing came back empty — a blip, not 'done'
                        self.state.update(episode=None, stage=None, message="NAS unreachable — retrying")
                        self._sleep(DRAIN_POLL_SECONDS)
                    elif why == "no-series":
                        self.state.update(episode=None, stage=None, message="no series selected")
                        self._sleep(self._retry_seconds())
                    else:                        # genuinely complete (incl. all-parked)
                        self.state.update(episode=None, stage=None,
                                          message="series complete — nothing left to upscale")
                        self._sleep(self._retry_seconds())
                    continue
                self._process(ep)
        except Exception as e:                       # never die silently — leave a trace
            logbook.exception("orchestrator loop", e)
            self.state["ended_reason"] = f"loop crashed: {e.__class__.__name__}: {e}"
        finally:
            self.state.update(running=False, stage=None, current=None)
            self.state["message"] = self.state.get("ended_reason") or "stopped"

    def _sleep(self, seconds):
        # interruptible wait (re-check ~every 5s so disable/window changes are quick)
        for _ in range(0, seconds, 5):
            if not self._enabled:
                break
            time.sleep(5)

    def _free_scratch_gb(self):
        # Space left for the project = physical free + what topaz-scratch already holds (its own
        # recyclable working files), so a partially-filled scratch doesn't read as 'no room'.
        return scratch.available_gb()

    def _maybe_resume_deferred(self):
        """Quiet Mode off → clear the Resolve-held set so those items re-enter selection and drain."""
        if self._resolve_deferred and not self._quiet_mode():
            self._resolve_deferred.clear()

    def _low_disk_pause(self):
        """A pause message if there isn't room to START an item, else None. In QUIET MODE the scratch
        fills with un-cleanable topaz outputs, so available_gb (which counts the scratch footprint as
        reclaimable) can't see the disk fill → gate on RAW physical free so a write can't ENOSPC/truncate."""
        if self._quiet_mode():
            phys = scratch.physical_free_gb()
            if phys is not None and phys < MIN_FREE_GB:
                return f"paused — low disk ({phys} GB): turn off Quiet Mode to drain items through Resolve"
        if self._in_finisher_keys():
            # OVERLAP: an item is finishing in the background (remux/upload). Its topaz ProRes was
            # dropped at hand-off so it's small (~10 GB), but available_gb still counts scratch as
            # reclaimable → gate the next item's start on RAW physical free (after offering the
            # prefetch buffer up) so the next item's own intermediate has guaranteed room. (Since the
            # drop, a finishing MOVIE no longer holds its ProRes, so both demands converge to the
            # floor — the fin_movie branch is a harmless legacy knob while MIN_FREE_GB == OVERLAP.)
            with self._finisher_lock:
                fin_movie = bool(self._in_finisher_movies)
            need = MIN_FREE_GB if fin_movie else OVERLAP_MIN_PHYS_GB
            self._reclaim_for_pipeline(need_gb=need)
            phys = scratch.physical_free_gb()
            if phys is not None and phys < need:
                return (f"paused — low disk ({phys} GB) while an item finishes in the background "
                        f"(frees at its cleanup)")
            return None                       # physical headroom confirmed — don't double-gate on
                                              # available_gb (it can't see the finisher's footprint)
        free = self._free_scratch_gb()
        if free is not None and free < MIN_FREE_GB:
            return f"paused — low disk: {free} GB free (need ~{MIN_FREE_GB} GB)"
        return None

    def _refresh_youtube(self):
        """Keep the YouTube upscale queue LIVE during an unattended run: youtarr keeps downloading
        while the user (and this laptop's on-time) is elsewhere, so re-scan its staging folder on a
        timer and let new videos join the round-robin. Once per run: a full popular/meta refresh
        (search.list — quota-heavy, so just once). Then every YT_REFRESH_SECONDS: a cheap re-scan
        (FTP list + durations for new ids only, no popular search). Best-effort — a NAS/API hiccup
        just means the queue updates on the next tick, never stalls the run."""
        try:
            import time as _t
            now = _t.monotonic()
            if not self._yt_meta_done:                     # run start: fresh popular sets, once
                youtube.refresh_all_meta()
                self._yt_meta_done = True
                self._yt_refresh_at = now + YT_REFRESH_SECONDS
            elif now >= self._yt_refresh_at:               # then a cheap live re-scan for new downloads
                youtube.refresh_downloads()
                self._yt_refresh_at = now + YT_REFRESH_SECONDS
        except Exception:
            pass

    def _item_lock(self, p):
        """The per-ITEM download lock (keyed by source basename, NOT full path) so the prefetcher (which
        downloads into the prefetch subfolder) and the foreground (main scratch) serialize on the SAME
        logical item across both folders — no double-download, and `_claim_prefetched` can't race a
        still-in-flight prefetch."""
        with self._dl_guard:
            return self._dl_locks.setdefault(p.source_basename, threading.Lock())

    def _download_once(self, p, *, on_progress=None, low_prio=False, extra_abort=None):
        """Run the download+CFR stage for `p` under the per-item lock so the background prefetcher and
        the foreground pipeline never download the same item at once (a size-verify race that could
        corrupt the source). Idempotent — re-checks stage_done after acquiring, so the loser is a fast
        no-op. Both the foreground `_process` download stage and `_prefetch` go through here.
        `extra_abort` (PREFETCH only) OR's an extra event onto the run-abort — the Plex-playing
        failsafe — so a background pull stops the instant a stream starts; the foreground never gets it."""
        from stages import run_stage
        abort = self._abort if extra_abort is None else _AnyEvent(self._abort, extra_abort)
        with self._item_lock(p):
            if stage_done("download", p):
                return True, "source + CFR already staged"
            return run_stage("download", p, abort=abort, progress=on_progress, low_prio=low_prio)

    def _claim_prefetched(self, p):
        """Move this item's prefetched source + CFR from the prefetch subfolder into the MAIN scratch, so
        the download stage sees them done (and they leave the prefetch buffer). We only ever move a
        COMPLETE prefetch. If the prefetcher is STILL fetching this exact item, do NOT wait it out (that
        stalls the run thread — and the topaz/remux overlap — behind a slow low-priority background pull):
        tell it to YIELD, drop its partial, and let the download stage re-pull at NORMAL priority."""
        pf_dir, main = scratch.prefetch_dir(), scratch.default_scratch()
        mine = _buffer_names(p.source_basename)                 # exact: source + <stem>_cfr.{mp4,mkv}
        lock = self._item_lock(p)
        if not lock.acquire(blocking=False):                   # the PREFETCHER holds it → mid-fetching THIS item
            self._prefetch_yield.set()                         # make it abort (releases within ~a poll)
            lock.acquire()                                     # now ours — the slow background pull is gone
            self._prefetch_yield.clear()
            try:
                for name in mine:                              # drop the aborted partial → clean fresh pull
                    try: os.remove(os.path.join(pf_dir, name))
                    except OSError: pass
            finally:
                lock.release()
            return
        try:
            for name in mine:                                  # move only THIS item's files (no prefix match)
                src, dst = os.path.join(pf_dir, name), os.path.join(main, name)
                if not os.path.exists(src):
                    continue
                try:
                    if os.path.exists(dst):
                        os.remove(src)                         # main already has it → drop the prefetch dup
                    else:
                        os.replace(src, dst)                   # atomic same-volume promote
                except OSError:
                    pass
        finally:
            lock.release()

    def _prefetch_candidates(self):
        """Upcoming items to pre-download, in PRIORITY order: newest YouTube first (so a just-arrived
        video is always ready for its turn), then queued movies, then the upcoming episodes of each
        active series. PEEK ONLY — never advances `_rr` or mutates any queue/pos. Deduped by local
        source path. Re-evaluated every prefetch round, so a newer YouTube video jumps to the front."""
        PF = scratch.prefetch_dir()          # stage into the prefetch subfolder (hidden from scratch view)
        cands, seen = [], set()
        def add(p):
            if p is not None and p.source not in seen:
                seen.add(p.source); cands.append(p)
        skip = set(self._parked)
        if self._current_skip_key:           # never prefetch the item the run thread is processing NOW —
            skip.add(self._current_skip_key) # it does that download itself; prefetching it just collides
        try:
            for v in youtube.all_pending(skip=skip):                       # 1. newest YouTube first
                add(youtube_paths(v["channel"], v["video_path"], v.get("title"), scratch_dir=PF))
        except Exception:
            pass
        try:
            for m in movies.get_selected():                                # 2. queued movies
                if m.get("name") not in skip and m.get("dir"):
                    add(movie_paths(m["name"], m["dir"], m.get("title"), scratch_dir=PF))
        except Exception:
            pass
        try:
            for s in series.get_active_series():                           # 3. upcoming TV episodes
                root = series.series_root(s)
                for it in (series.cached_queue(s) or {}).get("remaining_items", []):
                    if it.get("ep") not in skip:
                        add(episode_paths(s, it["ep"], it["source_name"], scratch_dir=PF, nas_tv_root=root))
        except Exception:
            pass
        return cands

    def _purge_prefetch_orphans(self, cands):
        """Delete prefetch-buffer files that no longer belong to any pending item (e.g. a channel wiped
        or a movie removed after it was prefetched) — keeps the buffer from accumulating dead weight.
        KEEPS the item currently being processed/claimed (state['current']) even if an external queue
        edit just dropped it from `cands`, so this (prefetch thread) can't delete a file `_claim_prefetched`
        (run thread) is mid-promoting — the claimed item is always exactly state['current']."""
        keep = set()
        for p in cands:
            keep |= _buffer_names(p.source_basename)
        cur = self.state.get("current") or {}
        cur_name = cur.get("name") or cur.get("source_name")   # the item being claimed right now
        if cur_name:
            keep |= _buffer_names(cur_name)
        pf_dir = scratch.prefetch_dir()
        try:
            names = os.listdir(pf_dir)
        except OSError:
            return
        for name in names:
            if name in keep:
                continue
            try:
                os.remove(os.path.join(pf_dir, name))
            except OSError:
                pass

    def _purge_all_prefetch(self) -> int:
        """Delete EVERY file in the prefetch buffer (the queue's staged, always-re-downloadable
        source + CFR). Returns how many were removed. Safe from `_process` after `_claim_prefetched`:
        the active item's files are already in the MAIN scratch, so only upcoming (sacrificable)
        items remain in the buffer."""
        pf = scratch.prefetch_dir()
        n = 0
        try:
            names = os.listdir(pf)
        except OSError:
            return 0
        for name in names:
            try:
                os.remove(os.path.join(pf, name)); n += 1
            except OSError:
                pass
        return n

    def _reclaim_for_pipeline(self, need_gb: int = MIN_FREE_GB):
        """PIPELINE > QUEUE: the active item's working files take priority over the prefetch buffer.
        If RAW physical free has fallen below `need_gb`, purge the whole prefetch buffer to reclaim
        real space (those queued items re-download when their turn comes). This makes available_gb()'s
        'the buffer counts as available' assumption actually true, so a full queue can never starve —
        and truncate — the in-flight ProRes segments or the DV master write. Returns free GB after."""
        phys = scratch.physical_free_gb()
        if phys is None or phys >= need_gb:
            return phys
        freed = self._purge_all_prefetch()
        phys2 = scratch.physical_free_gb()
        if freed:
            print(f"[disk] pipeline>queue: raw free {phys}<{need_gb} GB → purged {freed} prefetch "
                  f"file(s), now {phys2} GB", flush=True)
        return phys2

    def _prefetch(self):
        """Background worker: keep the upcoming queue's download+CFR staged on scratch so the GPU stage
        never waits on a download (the overnight run absorbs any NAS/network slowness). PURE ACCELERATOR
        — every file it makes, the foreground `download` stage also makes and detects via `stage_done`,
        so a kill just leaves partials the foreground re-pulls. Fetches newest YouTube first and
        re-evaluates the pool each round (a newer video jumps ahead). Stops deepening at the size-aware
        disk gate so prefetched sources never eat the in-flight/tail intermediate reserve."""
        while self._enabled:
            did = False
            if self._power_paused:                                # run is battery/power-paused → don't
                self._sleep(30); continue                         # burn CPU on CFR encodes either
            if self._plex_playing:                                # FAILSAFE: a Plex stream is live → keep the
                self._sleep(20); continue                         # NAS quiet so precache can't stutter it
            try:
                cands = self._prefetch_candidates()
                self._purge_prefetch_orphans(cands)               # drop buffer files for now-gone items
                main = scratch.default_scratch()
                for p in cands:
                    if (not self._enabled or self._abort.is_set()
                            or self._power_paused or self._plex_playing):
                        break
                    p = apply_container(p)                        # lock the CFR ext (.mkv/.mp4) to the source
                    if os.path.exists(os.path.join(main, p.source_basename)):   # so an MKV item reads as
                        continue                                  # prefetched once staged (else it busy-loops);
                    if stage_done("download", p):                 # already claimed/processing, or already
                        continue                                  # prefetched (in the buffer)
                    free = scratch.physical_free_gb()             # gate on RAW free — the buffer is what we
                    if free is None or free < PREFETCH_GATE_GB:   # fill, so it can't count as 'available' to
                        break                                     # itself (else the buffer overcommits disk)
                    if scratch.folder_used_gb(scratch.prefetch_dir()) >= PREFETCH_HARD_CAP_GB:
                        break                                     # HARD CAP: queued content ≤ 100 GB

                    if self._plex_started_now():                  # FRESH check at the decision point: never
                        break                                     # even START a pull if a stream just began
                    self._download_once(p, low_prio=True,                                 # → prefetch buffer;
                        extra_abort=_AnyEvent(self._plex_abort, self._prefetch_yield))
                                                                  # E-cores only + aborts if a Plex stream starts
                    did = True
                    break                                         # one at a time; re-evaluate the pool
            except Exception:
                pass
            self._sleep(15 if did else 60)                        # tight while working, relaxed when full/idle

    def _next_episode(self):
        """(EpisodePaths|None, reason). reason ∈ {ok, no-series, complete, unreachable}.
        Mode-aware: TV walks the selected series' episodes, Movie walks the whole Movies
        library. Skips PARKED items and distinguishes 'genuinely complete' from 'NAS listing
        came back empty' — the latter must NOT read as complete (a transient NAS blip would
        otherwise stall the run for the full retry interval)."""
        # Queued movies are interleaved into the episode stream by their slot (movies.pos =
        # episodes ahead of the movie). A movie defaults to pos 0 = process NEXT (the priority
        # interrupt), but the user can move it later so it processes BETWEEN chosen episodes. A
        # movie is taken only once it's 'due' (reached the front); otherwise the next episode
        # runs and every movie's slot decrements (see _process). Nav bar is VIEW-only.
        # A movie whose 90-min TURN just ended is DEFERRED (self._movie_wait > 0): one other
        # item runs first, then the movie resumes. If nothing else is ready, the fallbacks at
        # the bottom serve the movie anyway — a deferral never stalls the run.
        skip = (self._parked | self._resolve_deferred        # + items QUIET MODE is holding before Resolve
                | self._in_finisher_keys()                   # + items the FINISHER already owns (still
                                                             #   un-mastered on the NAS — must not re-pick)
                | self._finisher_persisted_keys())           # + DURABLE finisher items momentarily absent
                                                             #   from _in_finisher (disable→enable discard
                                                             #   race): the reconcile owns their resume, so
                                                             #   the run thread must not also grab them
        nx = movies.next_due(skip=skip) if self._movie_wait <= 0 else None
        if nx:
            return movie_paths(nx["source_name"], nx["nas_dir"], nx.get("title")), "ok"
        # YOUTUBE CADENCE: YouTube is NOT a round-robin peer (its 4K-SDR upscales are far slower than a
        # TV episode). It's a GATED single-video insert — after `youtube_every_tv_episodes` TV episodes
        # (`_tv_since_yt`), exactly ONE YouTube video runs, then the counter resets. So the stream is
        # e.g. ep, ep, 1 video, ep, ep, 1 video, … (newest video first — `next_due` order). The gate is
        # checked BEFORE the TV rotation so the video fires the moment the count is reached.
        yt = youtube.next_due(skip=skip)
        every = self._yt_every_tv()
        if yt is not None and self._tv_since_yt >= every and self._yt_wait <= 0:
            # (a turn-deferred video skips this gate — one episode runs first; the DRAIN
            # fallbacks below still serve it when nothing else is ready, so no stall)
            return youtube_paths(yt["channel"], yt["video_path"], yt.get("title")), "ok"
        # Round-robin over the ACTIVE TV SERIES (one episode each in turn, looping). `_rr` advances only
        # on completion (see _process), so a retry re-serves the same series. Per-run, reset on relaunch.
        parts = self._participants()
        if parts:
            n = len(parts)
            rot = self._rr % n
            saw_files = False
            for off in range(n):
                ref = parts[(rot + off) % n]
                q = series.episode_queue(ref, skip=skip)
                nxt = q.get("next")
                if nxt:
                    return episode_paths(ref, nxt["ep"], nxt["source_name"],
                                         nas_tv_root=series.series_root(ref)), "ok"
                if (q.get("done_count", 0) + q.get("source_count", 0)) > 0:
                    saw_files = True
            # No TV episode is ready right now. If YouTube has anything pending, DRAIN it (don't stall the
            # run waiting on a cadence that can't advance because TV is exhausted/blocked).
            if yt is not None:
                return youtube_paths(yt["channel"], yt["video_path"], yt.get("title")), "ok"
            # Nothing but a DEFERRED movie left → serve it anyway (deferral must never stall).
            nx = movies.next_due(skip=skip)
            if nx:
                return movie_paths(nx["source_name"], nx["nas_dir"], nx.get("title")), "ok"
            return None, ("complete" if saw_files else "unreachable")
        # No active TV series at all — YouTube-only: drain the queue one video after another.
        if yt is not None:
            return youtube_paths(yt["channel"], yt["video_path"], yt.get("title")), "ok"
        nx = movies.next_due(skip=skip)          # deferred movie + nothing else → resume it
        if nx:
            return movie_paths(nx["source_name"], nx["nas_dir"], nx.get("title")), "ok"
        return None, "no-series"

    def _yt_every_tv(self) -> int:
        """How many TV episodes run per 1 YouTube video (the cadence, >=1). Read live from settings."""
        try:
            return max(1, int(settings.get_settings().get("youtube_every_tv_episodes", 2)))
        except (TypeError, ValueError):
            return 2

    def _quiet_mode(self) -> bool:
        """QUIET MODE (read live): hold items before the screen-invasive Resolve stage so the laptop
        stays usable. download+topaz still run; nothing takes over the display."""
        try:
            return bool(settings.get_settings().get("quiet_mode", False))
        except (TypeError, ValueError):
            return False

    def _defer_resolve(self, p, ep_disp):
        """Hold this item at the Resolve doorstep because Screen Control is OFF (quiet mode). It
        re-enters selection and resumes before Resolve once Screen Control is turned back on
        (_maybe_resume_deferred). Checked at EVERY moment Resolve could start — including inside the
        dual-system resolve GATE, whose hold can span the whole previous remux (a live toggle there
        used to slip through and launch Resolve anyway)."""
        self._resolve_deferred.add(self._skip_key(p))
        self.state.update(stage=None, progress=None, current=None,
            message=f"{ep_disp}: Screen Control off — deferred before Resolve (screen stays yours)")

    def _skip_key(self, p) -> str:
        """The id the queue skip= sets (movies.next_due / youtube.next_due / series.episode_queue) match
        against: movies key on the basename WITH extension, TV/YouTube on p.ep (the stem)."""
        return p.source_basename if p.movie else p.ep

    def _participants(self):
        """The active TV series (round-robin peers — one episode each per rotation). YouTube is NOT
        here anymore; it's a counter-gated single-video insert (see _next_episode)."""
        return list(series.get_active_series())

    def _process(self, p: EpisodePaths):
        from stages import run_stage   # heavy runners live in stages.py
        # `current` = the item ACTUALLY processing (so the header shows a YouTube video as its
        # channel+title, not the next TV episode inferred from the up-next preview).
        # State/messages use the DISPLAY form of the id (a YouTube stem carries wire encoding).
        ep_disp = transfer.display_name(p.ep)
        self._current_skip_key = self._skip_key(p)   # the prefetcher excludes this item (we download it now)
        self.state.update(episode=ep_disp, current=p.item_view(), message=f"working {p.series} {ep_disp}")
        self._claim_prefetched(p)      # pull any prefetched source+CFR from the buffer into main scratch
        p = apply_container(p)         # resume: if the source is already on disk, lock its container
        # A movie gets a 90-min TURN, then one other item runs, then it resumes (segments +
        # completed stages survive). The budget gates the two LONG stages: topaz stops at a
        # segment boundary mid-stage; resolve simply doesn't start past the deadline (it can't
        # be interrupted). The tail (remux/upload/cleanup) belongs to the FINISHER thread —
        # this loop runs ONLY the GPU/screen stages, then hands the item off so the next
        # item's download/topaz overlaps the ~75-min x265 remux.
        deadline = (time.monotonic() + MOVIE_TURN_SECONDS) if (p.movie or p.youtube) else None
        for st in RUN_STAGES:
            if not self._enabled or self._abort.is_set():
                return
            if stage_done(st, p):
                continue
            if st == "resolve" and self._quiet_mode():     # SCREEN CONTROL OFF: hold before the screen-
                self._defer_resolve(p, ep_disp); return    # invasive Resolve stage; process other items
            if deadline is not None and st in ("topaz", "resolve") and time.monotonic() >= deadline:
                with self._cadence_lock:
                    if p.youtube: self._yt_wait = 1
                    else:         self._movie_wait = 1
                    self._save_cadence()
                self.state.update(stage=None, progress=None, current=None,
                    message=f"{ep_disp}: turn (90 min) over before {st} — one episode next, then it resumes")
                return
            while st == "resolve" and resolve_must_wait(self.state.get("finishing"),
                                                        self._finish_q.qsize()):
                # RESOLVE GATE (user-dictated): hold this item at the Resolve doorstep until the
                # previous item's remux fully completes. Side benefit: topaz is idle while we
                # hold, so that remux runs at full tilt and clears fastest.
                if not self._enabled or self._abort.is_set():
                    return
                if self._quiet_mode():                     # Screen Control turned OFF mid-hold (this gate
                    self._defer_resolve(p, ep_disp); return   # can span the whole previous remux) → defer
                if deadline is not None and time.monotonic() >= deadline:
                    with self._cadence_lock:
                        if p.youtube: self._yt_wait = 1
                        else:         self._movie_wait = 1
                        self._save_cadence()
                    self.state.update(stage=None, progress=None, current=None,
                        message=f"{ep_disp}: turn (90 min) over before {st} — one episode next, then it resumes")
                    return
                fin = self.state.get("finishing") or {}
                self.state.update(stage=None, progress=None,
                    message=f"{ep_disp}: holding before Resolve — finishing "
                            f"{fin.get('ep') or 'the previous item'}'s remux first")
                time.sleep(10)
            if st == "resolve" and self._quiet_mode():     # flipped OFF in the instant the gate cleared —
                self._defer_resolve(p, ep_disp); return    # never launch Resolve with Screen Control off
            self.state.update(stage=st, message=f"{ep_disp}: {st}", progress=None)
            self._reclaim_for_pipeline()   # pipeline > queue: purge the prefetch buffer if raw free is
                                           # low, so this stage's write can't be starved/truncated
            ekey = p.source_basename + "|" + st      # per-(item, stage) elapsed key
            self._elapsed_begin(ekey)                # RESUMES this stage's clock if it ran before
            self._stage_active = True
            if st == "download":              # per-source lock: the prefetcher may already be pulling
                ok, msg = self._download_once(p, on_progress=self._set_progress)   # this exact source
            else:
                ok, msg = run_stage(st, p, abort=self._abort, progress=self._set_progress, deadline=deadline)
            self._stage_active = False
            if ok:  self._elapsed_done(ekey)         # stage complete → reset its timer for any re-run
            else:   self._elapsed_pause()            # interrupted/failed → persist so it RESUMES from here
            # one-line message — a stage's msg can carry an ffmpeg stderr tail with newlines,
            # which would otherwise put multi-line junk in the UI status.
            self.state.update(message=" ".join(f"{ep_disp}: {st} — {msg}".split()), progress=None)
            if ok:
                self._fail_counts.pop(p.ep, None)   # any forward progress clears the fail streak
            if not ok:
                # A movie's 90-min TURN ending (clean stop at a segment boundary) is NOT a
                # failure: defer the movie behind one other item and move on — no fail count.
                if str(msg).startswith("turn-budget"):
                    with self._cadence_lock:
                        if p.youtube: self._yt_wait = 1
                        else:         self._movie_wait = 1
                        self._save_cadence()
                    self.state.update(stage=None, progress=None, current=None,
                        message=f"{ep_disp}: 90-minute turn done — one episode next, then it resumes")
                    return
                # A stop/pause abort isn't an episode FAILURE — don't count it toward parking
                # and don't sit in the 60s retry; let the loop re-evaluate (power/stop) at once.
                if self._abort.is_set() or not self._enabled:
                    return
                n = self._fail_counts.get(p.ep, 0) + 1
                self._fail_counts[p.ep] = n
                if n >= MAX_EPISODE_FAILS:
                    self._parked.add(self._skip_key(p))   # movies key on basename, not the stem (else never skips)
                    self._fail_counts.pop(p.ep, None)
                    if p.youtube:                     # a parked video SPENT its YouTube turn — restart the
                        with self._cadence_lock:      # cadence so the gate doesn't serve another video with
                            self._tv_since_yt = 0     # no intervening TV
                            self._save_cadence()
                                                      # no intervening TV (park never reached the upload reset)
                    logbook.failure(f"{p.ep}: parked after {n} failures at {st} — skipping so the "
                                    f"series keeps moving (last: {' '.join(str(msg).split())[-120:]})")
                    self.state.update(stage=None,
                        message=f"{ep_disp} parked after {n} failures at {st} — moving to the next episode")
                    self._sleep(5)
                    return
                # otherwise leave it; next pass resumes this stage from its start
                self._sleep(60)
                return
        if not self._enabled or self._abort.is_set():
            return
        self._hand_to_finisher(p)   # GPU/screen stages done → remux/upload/cleanup run in the
                                    # background while the loop moves on to the next item

    # ---- finisher: the headless tail (remux/upload/cleanup) overlapped with the next item ----

    def _in_finisher_keys(self) -> set:
        with self._finisher_lock:
            return set(self._in_finisher)

    def _advance_cadence_at_handoff(self, p: EpisodePaths):
        """Scheduling FAIRNESS (cadence counters + round-robin) advances the moment an item's
        GPU work completes — at hand-off, on the RUN thread, synchronously BEFORE the next
        selection — not ~75 min later at its upload (review-caught: upload-time advancement
        left _rr/_tv_since_yt stale for the next pick, degenerating A,B,A,B into A,A,B,B and
        lagging the YouTube cadence one item). COMPLETION side-effects (mark_done, replace,
        library refreshes) stay at the finisher's upload, where they belong."""
        with self._cadence_lock:
            try:
                if p.source_basename in self._cadence_advanced:
                    return          # this item already took its scheduling turn — a finisher retry
                                    # (fail / power requeue / disable / restart) must not re-count it
                self._cadence_advanced.add(p.source_basename)
                if p.youtube:
                    self._tv_since_yt = 0                 # cadence: restart the N-episode countdown
                    if self._movie_wait > 0:              # a non-movie item took its turn → a deferred
                        self._movie_wait -= 1             # movie's next turn is due again
                elif p.movie:
                    if self._yt_wait > 0:                 # a non-YouTube item took its turn
                        self._yt_wait -= 1
                else:                                     # a TV episode
                    self._tv_since_yt += 1                # one more TV episode toward the next video
                    if self._movie_wait > 0:
                        self._movie_wait -= 1
                    if self._yt_wait > 0:
                        self._yt_wait -= 1
                    parts = self._participants()          # active TV series (round-robin peers)
                    idx = next((i for i, r in enumerate(parts) if r == p.series), None)
                    if idx is not None and parts:
                        self._rr = (idx + 1) % len(parts)
                self._save_cadence()
            except Exception:
                pass

    def _drop_topaz_intermediates(self, p: EpisodePaths):
        """The moment an item heads to the finisher, its ProRes + topaz segment chunks are dead
        weight: resolve consumed them and produced the VERIFIED DV render that the remux
        re-encodes from (the RUN_STAGES loop just confirmed stage_done('resolve')). Pre-overlap
        they lingered ~35 min until cleanup; the finisher window is now HOURS and overlaps the
        next item's topaz writes — ~250-350 GB of scratch held for nothing (user-caught). The
        recovery ladder is unchanged: if the DV render were ever lost, resolve re-runs, which
        re-runs topaz from the KEPT source/CFR — same as after any cleanup."""
        try:
            os.remove(p.prores)
        except OSError:
            pass
        shutil.rmtree(p.segdir, ignore_errors=True)

    def _hand_to_finisher(self, p: EpisodePaths):
        self._drop_topaz_intermediates(p)   # frees ~250-350 GB the finisher never reads
        self._advance_cadence_at_handoff(p)
        big = p.movie
        if not big:
            try:      # gauge by the ACTUAL working set: a feature-length YouTube video's ProRes
                big = os.path.getsize(p.prores) > 150e9   # is movie-sized too (review-caught)
            except OSError:
                pass
        with self._finisher_lock:
            self._in_finisher.add(self._skip_key(p))
            if big:
                self._in_finisher_movies.add(self._skip_key(p))
            d = self._finisher_descriptor(p)     # record DURABLE ownership so a deactivate/relaunch
            self._finisher_persisted[self._desc_cid(d)] = d   # mid-remux RESUMES it (see _finisher_reconcile)
            self._save_finisher_persisted_locked()
        self._finish_q.put(p)
        self.state.update(stage=None, progress=None,
            message=f"{transfer.display_name(p.ep)} → finisher (remux/upload continue in the background)")

    def _set_finishing_progress(self, info):
        """The finisher's own progress surface (state['finishing']) — NEVER _set_progress, whose
        ETA window state is single-slot and belongs to the run thread."""
        f = dict(self.state.get("finishing") or {})
        f.update({"stage": info.get("stage") or f.get("stage"), "pct": info.get("pct"),
                  "frames": info.get("frames"), "total": info.get("total"),
                  # segment bar (remux is segmented like topaz); explicit so a non-segmented stage
                  # (upload) CLEARS the stale remux notches instead of inheriting them via `f`.
                  "notches": info.get("notches"), "seg_done": info.get("seg_done"),
                  "seg_total": info.get("seg_total"),
                  "elapsed_secs": round(self._fin_elapsed_value(), 1)})
        # ETA from THIS attempt's live rate. The elapsed stopwatch deliberately ACCUMULATES
        # across killed attempts (user-dictated), so elapsed×remaining/pct read ~38 h after a
        # remux restart; anchoring frames/time at attempt start gives the real number.
        # CONTENTION-AWARE ETA: the remux runs SLOW while topaz shares the machine and SPEEDS UP the
        # instant topaz ends (and vice versa). Drop the anchor on that transition so the rate window
        # re-measures from the new regime instead of blending the contended and solo rates.
        if info.get("stage") == "remux":
            topaz_on = (self.state.get("stage") == "topaz")
            if topaz_on != self._fin_contended:
                self._fin_contended = topaz_on
                self._fin_eta_anchor = None
        frames, total = info.get("frames"), info.get("total")
        if frames is not None and total:
            a = self._fin_eta_anchor
            if a is None or a[0] != f.get("stage") or frames < a[2]:
                self._fin_eta_anchor = (f.get("stage"), time.monotonic(), frames)
            else:
                span = time.monotonic() - a[1]
                donef = frames - a[2]
                if span >= 15 and donef >= 30:
                    f["eta_secs"] = round((total - frames) * span / donef, 1)
        self.state["finishing"] = f
        mono = time.monotonic()
        if self._fin_el_key and mono - self._fin_el_save > 30:   # crash-checkpoint, like the run thread's
            with _ELAPSED_LOCK:
                m = _elapsed_map(); m[self._fin_el_key] = round(self._fin_elapsed_value(), 1); _elapsed_write(m)
            self._fin_el_save = mono

    def _finisher(self):
        """Daemon worker: drains _finish_q one item at a time (so there is never more than one
        x265/upload in flight). Ownership is DURABLE: an item stays on _finisher_persisted from
        hand-off until it terminally completes (uploaded) or parks, so a dropped/failed item is
        re-queued by _finisher_reconcile (idle tick + enable), NOT by the run thread — selection
        excludes _finisher_persisted_keys(). Its stage_done fast-path skips already-finished stages.
        POWER (review-caught): a power pause sets _finish_abort but does NOT disable the run, so
        this loop must NOT blindly clear the abort and start the next item — it re-checks power
        BEFORE every item and REQUEUES (keeping ownership, so selection can't double-pick) while
        insufficient. The clear only happens right after power is confirmed sufficient."""
        from stages import run_stage
        while True:
            try:
                p = self._finish_q.get(timeout=2)
            except queue.Empty:
                if self._enabled:                # idle → re-queue any durable item that fell out of the
                    self._finisher_reconcile()   # queue (relaunch, or the disable→enable discard race)
                continue
            requeued = False
            try:
                if not self._enabled:
                    continue      # drop; the finally re-exposes it to selection for the next arm
                if self._power_paused or self._power_ok()[0] == "pause":
                    self._finish_q.put(p)          # hold WITHOUT losing ownership — never encode
                    requeued = True                # or upload on battery
                    time.sleep(10)
                    continue
                self._finish_abort.clear()         # safe: power confirmed sufficient just above
                self._finish_item(p, run_stage)
            except Exception as e:
                logbook.exception(f"finisher {p.ep}", e)
            finally:
                if not requeued:
                    with self._finisher_lock:
                        self._in_finisher.discard(self._skip_key(p))
                        self._in_finisher_movies.discard(self._skip_key(p))
                    self.state["finishing"] = None

    def _finish_item(self, p: EpisodePaths, run_stage):
        ep_disp = transfer.display_name(p.ep)
        for st in FINISH_STAGES:
            if not self._enabled or self._finish_abort.is_set():
                return
            if stage_done(st, p):
                continue
            self._reclaim_for_pipeline()   # same pipeline>queue guarantee for the finisher's writes
            self.state["finishing"] = {"ep": ep_disp, "stage": st, "pct": None}
            ekey = p.source_basename + "|" + st
            self._fin_elapsed_begin(ekey)
            ok, msg = run_stage(st, p, abort=self._finish_abort, progress=self._set_finishing_progress)
            if ok:
                self._fin_elapsed_done(ekey)
                self._fail_counts.pop(p.ep, None)     # forward progress clears the fail streak
            else:
                self._fin_elapsed_pause()
            if ok and st == "upload":   # now has a DV master → COMPLETION side-effects only
                # (cadence/round-robin already advanced at HAND-OFF on the run thread — see
                # _advance_cadence_at_handoff; re-advancing here would double-count)
                with self._cadence_lock:                      # its turn is fully spent — a future
                    self._cadence_advanced.discard(p.source_basename)   # full REDO is a NEW turn
                    self._save_cadence()
                try:
                    if p.youtube:                             # a YouTube video finished
                        vid = youtube.video_id(p.source_basename)
                        youtube.mark_done(vid)
                        youtube.clear_resume_first(p.series)  # completed → no longer 'first on resume'
                        youtube.refresh_videos(p.series)      # channel stays queued (standing sub)
                    elif p.movie:                             # a queued movie finished (keyed off the
                        movies.remove_selected(p.source_basename)   # ITEM, not queue membership — a movie
                        movies.refresh_library()                    # removed mid-pipeline is still a movie,
                                                                    # NOT a TV ep
                    else:                                     # a TV episode finished
                        series.refresh_queue(p.series)
                        movies.decrement_positions()          # an episode finished → every movie advances
                except Exception:
                    pass
            if not ok:
                if self._finish_abort.is_set() or not self._enabled:
                    return                                # stop/pause abort — not an episode failure
                n = self._fail_counts.get(p.ep, 0) + 1
                self._fail_counts[p.ep] = n
                if n >= MAX_EPISODE_FAILS:
                    self._parked.add(self._skip_key(p))
                    self._fail_counts.pop(p.ep, None)
                    with self._cadence_lock:       # finisher thread — never race the run thread
                        self._cadence_advanced.discard(p.source_basename)   # parked: turn spent, done
                        if p.youtube:
                            self._tv_since_yt = 0
                        self._save_cadence()
                    self._finisher_persist_remove(p)   # parked → terminal; stop resuming it every arm
                    logbook.failure(f"{p.ep}: parked after {n} failures at {st} — skipping so the "
                                    f"series keeps moving (last: {' '.join(str(msg).split())[-120:]})")
                else:
                    time.sleep(30)   # brief backoff; the item re-enters selection on return and
                                     # fast-paths back here through its stage_done RUN_STAGES
                return
        # Every FINISH_STAGE — remux, upload AND cleanup — completed. Only NOW is the item truly
        # terminal: drop it from the durable work-list. Removing at the earlier 'upload' boundary
        # left a window where a deactivate/relaunch between upload and cleanup dropped the item, so
        # cleanup never re-ran on resume and its ~250-350 GB working set leaked (review-caught).
        self._finisher_persist_remove(p)


# module-level singleton the dashboard server drives
ORCH = Orchestrator()
