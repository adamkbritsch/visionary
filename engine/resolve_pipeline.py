"""Mechanical DaVinci Resolve pipeline runner — drives the scriptable backbone
of the Resolve stage from a CLOSED Resolve, end to end.

The two UI-only steps (Dolby Vision "Analyze All Shots" and the DV export-profile
dropdown) are done by the dv_shim between phases:

  python3 resolve_pipeline.py setup  <source.mov>   # launch -> import -> fps -> color -> timeline -> scene cuts
  # <DV Analyze All Shots via dv_shim>
  # <set DV Profile 8.1 on the Deliver page via dv_shim>
  python3 resolve_pipeline.py render <out.mov>      # render entire timeline + verify (fps + DV)

  Color management is INHERITED from the persistent per-mode project (the user
  configures each ONCE in Resolve; this pipeline never sets it — see resolve.py).
Frame rate is set from the SOURCE before the timeline exists (no conform).
"""
from __future__ import annotations
import importlib.util
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from versions import RESOLVE_APP  # noqa: E402 — exact-version pin (versions.py); preflight gates on it
LIB = RESOLVE_APP + "/Contents/Libraries/Fusion/fusionscript.so"
FFPROBE = "/opt/homebrew/bin/ffprobe"


def _analysis_budget(path) -> float:
    """DV "Analyze All Shots" wall-clock cap, scaled with CONTENT length. The flat hour
    is right for episodes and ordinary films; a 4-hour fan cut (Ep III Siege of
    Mandalore) can honestly need more. Analysis runs several-times realtime, so half of
    runtime is a generous ceiling; unprobeable input keeps the proven flat hour."""
    try:
        out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        dur = float(out or 0)
    except Exception:
        dur = 0.0
    return max(3600.0, dur * 0.5) if dur > 0 else 3600.0
# Candidate project names per OUTPUT MODE, in preference order. We match whatever the
# project is ACTUALLY named in Resolve (the API can't rename), so a rename — e.g. to the
# new "Visionary …" app name — won't break the pipeline. First existing one wins.
# Three persistent projects, named for what they PRODUCE. The old names described the INTAKE
# range on two of them ("SDR"/"HDR") and the output on the third, which made them unreadable:
# an "SDR" project that emitted Dolby Vision. Legacy names stay in each list as fallbacks so a
# machine that has not re-imported the bundle keeps working. First existing one wins.
# The lists live in versions.py (single source of truth — import_resolve and preflight's
# artifacts check consume the SAME tuples, so they can never drift apart again).
import versions as _versions
DV1000_PROJECTS = list(_versions.RESOLVE_PROJECTS_DV1000)
DV2000_PROJECTS = list(_versions.RESOLVE_PROJECTS_DV2000)
SDR_OUT_PROJECTS = list(_versions.RESOLVE_PROJECTS_SDR_OUT)

# Output modes, named the same way. These are the values that travel between stages.py and this
# module, and they match the per-item `output_mode` setting one-for-one.
MODE_DV1000, MODE_DV2000, MODE_SDR = "dv1000", "dv2000", "sdr"


def is_dv_mode(mode) -> bool:
    """False only for the true-SDR output. This is what makes that mode headless: with no DV
    there is no Analyze All, so dv_shim (and the screen, and cliclick) is never touched."""
    return mode != MODE_SDR


def target_nits(mode) -> int:
    return 2000 if mode == MODE_DV2000 else 1000


DV_PRESET = "OvernightDV"        # global render preset carrying DV Profile 8.1 (survives Resolve quits)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import resolve as R  # noqa: E402


def project_for(pm, mode):
    """Pick the persistent project for the OUTPUT MODE, matching whatever it's ACTUALLY
    named in Resolve. 'dv2000' → the 2000-nit DV project, 'sdr' → the true-Rec.709 one,
    anything else → 1000-nit DV (the default, and what an unpinned SDR intake gets).
    Tries the candidates in order and returns the first that exists, so renaming the
    projects in Resolve doesn't break anything. Returns (name, exists) — name is the
    preferred candidate when none exist (for the 'missing project' message)."""
    projects = pm.GetProjectListInCurrentFolder() or []
    candidates = (SDR_OUT_PROJECTS if mode == MODE_SDR
                  else DV2000_PROJECTS if mode == MODE_DV2000 else DV1000_PROJECTS)
    for name in candidates:
        if name in projects:
            return name, True
    return candidates[0], False


def connect(timeout=200, launch=False):
    if launch:
        subprocess.run(["open", "-a", "DaVinci Resolve"], check=False)
    spec = importlib.util.spec_from_file_location("fusionscript", LIB)
    fs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fs)
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = fs.scriptapp("Resolve")
            if r and r.GetProjectManager():
                return r
        except Exception:
            pass
        time.sleep(3)
    return None


# ---- RESOLVE RESUME BOOK: a killed pass must not lose finished sub-steps -----------------
# (user-dictated 2026-08-06). The PERSISTENT project still holds the imported clip(s), the
# timeline, its scene-cut splits and any completed DV analysis after a kill — only this
# process's memory is gone. Setup therefore validates the project against a durable marker
# and RESUMES (skip import + DetectSceneCuts, and skip Analyze All when it had completed)
# instead of blindly clearing. Every check must prove the state is OURS for THIS exact
# input; anything off falls back to the fresh path. Markers are written only at step
# COMPLETIONS, so a kill mid-step can never fake a finished one.
_RESUME_FILE = os.path.expanduser("~/.topaz-pipeline/resolve_resume.json")
_RESUME_KEEP = 50


def _resume_key(src, mode):
    return os.path.basename(str(src)) + "|" + str(mode)


def _src_ident(video):
    try:
        st = os.stat(video)
        return f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return "missing"


def _segdir_ident(seg_paths):
    try:
        return f"{len(seg_paths)}:{sum(os.path.getsize(p) for p in seg_paths)}"
    except OSError:
        return "missing"


def _resume_book() -> dict:
    try:
        with open(_RESUME_FILE) as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _resume_save(book) -> None:
    try:
        if len(book) > _RESUME_KEEP:
            for k in list(book)[:len(book) - _RESUME_KEEP]:
                book.pop(k, None)
        os.makedirs(os.path.dirname(_RESUME_FILE), exist_ok=True)
        tmp = _RESUME_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(book, f)
        os.replace(tmp, _RESUME_FILE)
    except OSError:
        pass


def _resume_get(src, mode):
    return _resume_book().get(_resume_key(src, mode))


def _resume_put(src, mode, **fields) -> None:
    book = _resume_book()
    book[_resume_key(src, mode)] = {**fields, "at": int(time.time())}
    _resume_save(book)


def _resume_reset(src, mode) -> None:
    book = _resume_book()
    if book.pop(_resume_key(src, mode), None) is not None:
        _resume_save(book)


def _mark_analyzed(src, mode) -> None:
    book = _resume_book()
    e = book.get(_resume_key(src, mode))
    if e:
        e["analyzed"] = True
        _resume_save(book)


def _validate_resume_single(proj, video, marker) -> bool:
    """PURE-ish (takes any objects with the Resolve API shape — unit-tested on fakes).
    True only when the project's state is OUR state for THIS exact file."""
    try:
        if not (marker and marker.get("scenes")):
            return False
        mp = proj.GetMediaPool()
        clips = mp.GetRootFolder().GetClipList() or []
        if len(clips) != 1:
            return False
        if str(clips[0].GetClipProperty("File Path")) != str(video):
            return False
        if (proj.GetTimelineCount() or 0) != 1:
            return False
        tl = proj.GetTimelineByIndex(1)
        frames = tl.GetEndFrame() - tl.GetStartFrame() + 1
        if frames != int(marker.get("frames") or -1):
            return False
        items = tl.GetItemListInTrack("video", 1) or []
        if len(items) < 2:        # scene cuts leave >=2 shots; a bare clip is not our state
            return False
        return True
    except Exception:
        return False


def _validate_resume_episode(proj, marker) -> bool:
    """Episode variant: the media pool holds exactly the manifest's chunk count, one
    timeline, the recorded frame total, and at least chunk-count items (cuts only add)."""
    try:
        if not (marker and marker.get("scenes")):
            return False
        want_clips = int(marker.get("clips") or 0)
        if want_clips < 1:
            return False
        mp = proj.GetMediaPool()
        clips = mp.GetRootFolder().GetClipList() or []
        if len(clips) != want_clips:
            return False
        if (proj.GetTimelineCount() or 0) != 1:
            return False
        tl = proj.GetTimelineByIndex(1)
        frames = tl.GetEndFrame() - tl.GetStartFrame() + 1
        if frames != int(marker.get("frames") or -1):
            return False
        items = tl.GetItemListInTrack("video", 1) or []
        if len(items) < want_clips:
            return False
        return True
    except Exception:
        return False


def _resume_setup(resolve, proj, tl_label, marker_extra="") -> None:
    """Common resume tail: our timeline current, Color page, the same SETUP_DONE the
    fresh path prints (the shim keys on it)."""
    tl = proj.GetTimelineByIndex(1)
    try:
        proj.SetCurrentTimeline(tl)
    except Exception:
        pass
    resolve.OpenPage("color")
    print(f"[{time.strftime('%H:%M:%S')}] RESUME: project intact — {tl_label}"
          + marker_extra, flush=True)
    print("SETUP_DONE — Resolve on Color page, ready for DV Analyze All Shots (shim)", flush=True)


def _clear_project(proj):
    """Empty the persistent project for a new episode WITHOUT touching its
    settings (color management, etc. are the user's — left exactly as-is)."""
    mp = proj.GetMediaPool()
    try:
        tls = [proj.GetTimelineByIndex(i) for i in range(1, (proj.GetTimelineCount() or 0) + 1)]
        if tls:
            mp.DeleteTimelines(tls)
    except Exception:
        pass
    try:
        clips = mp.GetRootFolder().GetClipList()
        if clips:
            mp.DeleteClips(clips)
    except Exception:
        pass


def _place_now():
    """Move Resolve to the pinned display, if one is set. Idempotent, and non-fatal HERE:
    goto_dolby_vision re-asserts it before any template is matched, and that call is the
    one allowed to fail the stage. Failing setup on a placement hiccup would throw away
    the whole import for a window-position problem."""
    try:
        import dv_shim
        host = dv_shim.get_host()
        if not host:
            return
        dv_shim.place_on_host(host)
        print(f"[{time.strftime('%H:%M:%S')}] Resolve moved to {host.get('key')}", flush=True)
        # The launch stole focus on the MAIN display; Resolve's window now lives on the
        # pinned one, so hand the main display back to the app (user-dictated 2026-08-06).
        # Safe for the automation: goto_dolby_vision re-activates Resolve before any
        # click, and the analysis watcher re-activates it whenever it leaves its screen.
        # Host-pinned case ONLY — with Resolve on the main display, raising Visionary
        # would cover the very window the templates need to see.
        subprocess.run(["osascript", "-e", 'tell application "Visionary" to activate'],
                       check=False, timeout=10)
    except Exception as e:
        print(f"placement at setup deferred ({e.__class__.__name__}: {e}) — "
              "the DV step will retry", flush=True)


def setup(segdir, mode=MODE_DV1000):
    print(f"[{time.strftime('%H:%M:%S')}] launching Resolve… (mode={mode})", flush=True)
    resolve = connect(launch=True)
    if not resolve:
        print("CONNECT FAILED"); return 1
    print(f"[{time.strftime('%H:%M:%S')}] connected: {resolve.GetVersionString()}", flush=True)
    pm = resolve.GetProjectManager()
    # Use the app's PERSISTENT project for this mode. The user configures each ONCE (color
    # management, DV Profile 8.1, and its Target Display ceiling — 1000 or 2000 nits; the
    # SDR one carries Rec.709 Gamma 2.4 and no Dolby Vision at all) and the pipeline
    # INHERITS all of it. Both DV projects map SDR *and* HDR intake to HDR PQ output — the
    # intake range only chooses BETWEEN them, and never selects the SDR project, which is
    # reachable solely through an explicit per-item override.
    # We NEVER recreate a project (that would
    # wipe the setup), NEVER set color ourselves, and NEVER create a fresh one
    # silently — a blank project renders SDR with no Dolby Vision, and DV Profile 8.1
    # can't be re-set via the API. If it's missing, STOP and tell the user.
    want, exists = project_for(pm, mode)
    if not exists:
        print(f"MISSING PROJECT '{want}' (mode={mode}): configure it ONCE in Resolve "
              f"(color management to HDR PQ + DV Profile 8.1 + "
              f"{target_nits(mode)}-nit Target Display), then re-run. "
              f"The pipeline inherits those and will not create a blank project.")
        return 1
    pm.LoadProject(want)
    proj = pm.GetCurrentProject()
    # ONTO THE PINNED DISPLAY NOW, not when the DV step starts. Everything below —
    # clearing the project, importing the chunks, building and validating the timeline —
    # is a minute or more of Resolve sitting in front of whatever the user is doing. This
    # is also the EARLIEST it CAN move: the Project Manager window refuses to be
    # positioned at all, so there is nothing to move until a project is open.
    _place_now()
    # Topaz now outputs SCENE-CUT CHUNKS (+ a manifest), not one giant file. Assemble them
    # on the timeline IN ORDER, back-to-back — that's the "concatenation", with no second
    # ~238 GB file. Resolve's own scene detection still runs below (chunks span multiple
    # shots). Frame-accurate placement keeps the remuxed audio in sync.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import topaz
    man = topaz.read_manifest(segdir)
    if not man or not man.get("segments"):
        print("MISSING/EMPTY topaz manifest in", segdir); return 1
    seg_paths = [os.path.join(segdir, s["file"]) for s in man["segments"]]
    fps = man.get("fps")
    m = _resume_get(segdir, mode)
    if m and m.get("ident") == _segdir_ident(seg_paths) and _validate_resume_episode(proj, m):
        _resume_setup(resolve, proj, "chunk import + scene cuts skipped",
                      ", analysis already complete" if m.get("analyzed") else "")
        return 0
    _resume_reset(segdir, mode)         # entering the fresh path: no stale step may survive
    _clear_project(proj)
    mp = proj.GetMediaPool()
    proj.SetSetting("timelineFrameRate", str(fps))   # match the source BEFORE the timeline exists
    clips = mp.ImportMedia(seg_paths)
    if not clips or len(clips) < len(seg_paths):
        print(f"IMPORT FAILED: {len(clips) if clips else 0}/{len(seg_paths)} chunks"); return 1
    # ImportMedia's return order isn't guaranteed — map each clip back to its file and
    # rebuild the manifest order so the chunks land in the right timeline positions.
    by_name = {}
    for c in clips:
        try: by_name[os.path.basename(str(c.GetClipProperty("File Path")))] = c
        except Exception: pass
    ordered = [by_name.get(s["file"]) for s in man["segments"]]
    if any(c is None for c in ordered):
        print("CHUNK ORDER FAILED — couldn't match imported clips to the manifest"); return 1
    print(f"[{time.strftime('%H:%M:%S')}] assembling {len(ordered)} chunks @ {fps}fps | "
          f"INHERITED color out={proj.GetSetting('colorSpaceOutput')!r}", flush=True)
    tl = mp.CreateTimelineFromClips("_tl", ordered)   # appended in manifest order, back-to-back
    tl_fps = tl.GetSetting("timelineFrameRate")
    if abs(float(tl_fps) - float(fps)) > 0.01:   # float compare (string precision varies)
        print(f"FRAME-RATE CONFORM! tl={tl_fps!r} src={fps!r}"); return 1
    # Integrity guard: EVERY chunk must be on the timeline, in order, back-to-back.
    # CreateTimelineFromClips appends them contiguously, so the clip COUNT matching the
    # manifest is the real check (a lost/duplicated chunk is what would drift the audio).
    # The exact frame total is NOT compared — Resolve's decode count differs by a few
    # frames from ffprobe's header nb_frames, which is harmless for a contiguous timeline.
    try:
        items = tl.GetItemListInTrack("video", 1) or []
        got = tl.GetEndFrame() - tl.GetStartFrame() + 1
        if len(items) != len(ordered):
            print(f"TIMELINE ASSEMBLY WRONG: {len(items)} clips on the timeline, expected "
                  f"{len(ordered)} chunks"); return 1
        print(f"[{time.strftime('%H:%M:%S')}] timeline = {len(items)} chunks, {got} frames "
              f"(~{man.get('total_frames')} expected) — contiguous", flush=True)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] (timeline integrity unverifiable: {e})", flush=True)
    resolve.OpenPage("color")
    t = time.time()
    ok = tl.DetectSceneCuts()
    shots = tl.GetItemListInTrack("video", 1)
    print(f"[{time.strftime('%H:%M:%S')}] DetectSceneCuts={ok} ({(time.time()-t)/60:.1f}min) "
          f"shots={len(shots) if shots else 0}", flush=True)
    try:                        # step COMPLETE → durable: a killed pass resumes past it
        frames = tl.GetEndFrame() - tl.GetStartFrame() + 1
        _resume_put(segdir, mode, ident=_segdir_ident(seg_paths), frames=int(frames),
                    clips=len(ordered), scenes=True, analyzed=False)
    except Exception:
        pass
    print("SETUP_DONE — Resolve on Color page, ready for DV Analyze All Shots (shim)", flush=True)
    return 0


def render(out, mode=MODE_DV1000, bitrate=60000):
    resolve = connect()
    if not resolve:
        print("CONNECT FAILED"); return 1
    pm = resolve.GetProjectManager()
    want, _ = project_for(pm, mode)
    if not pm.GetCurrentProject() or pm.GetCurrentProject().GetName() != want:
        pm.LoadProject(want)
    proj = pm.GetCurrentProject()
    out_dir = os.path.dirname(out) or os.path.expanduser("~/topaz-scratch")
    name = os.path.splitext(os.path.basename(out))[0]
    fps = proj.GetCurrentTimeline().GetSetting("timelineFrameRate")
    # Resolve FORGETS the Dolby Vision Profile when it quits (and there's no API to
    # set it), so re-apply it EVERY render from the saved GLOBAL render preset, which
    # persists across restarts. LoadRenderPreset restores DV 8.1 + H.265/Main10/quality.
    # Do NOT call SetCurrentRenderFormatAndCodec — it resets the DV Profile to None
    # (=> HDR10, no RPU; that was the first S02E12 render).
    if DV_PRESET not in (proj.GetRenderPresetList() or []):
        print(f"MISSING RENDER PRESET '{DV_PRESET}': create it once (Deliver page → set "
              f"Dolby Vision Profile to 8.1 with HW accel ON → Save As New Render Preset). "
              f"Without it the render embeds NO Dolby Vision."); return 1
    proj.LoadRenderPreset(DV_PRESET)
    proj.SetRenderSettings({"TargetDir": out_dir, "CustomName": name,
                            "ExportVideo": True, "ExportAudio": False, "FrameRate": str(fps),
                            "VideoQuality": int(bitrate)})   # Kb/s — overrides the preset's 60000 to match a higher-bitrate intake
    print(f"[{time.strftime('%H:%M:%S')}] export bitrate: {int(bitrate)} Kb/s", flush=True)
    jid = proj.AddRenderJob()
    print(f"[{time.strftime('%H:%M:%S')}] render job {jid} @ {fps}fps", flush=True)
    proj.StartRendering(jid)
    # Rendering now runs headless in Resolve — bring the app back to the front so its
    # progress is what's on screen (no more UI automation is needed past this point).
    subprocess.run(["open", "-a", "Visionary"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"[{time.strftime('%H:%M:%S')}] rendering started — refocused the app", flush=True)
    t0 = time.time(); last = -1
    while proj.IsRenderingInProgress():
        st = proj.GetRenderJobStatus(jid); p = st.get("CompletionPercentage")
        if p != last:
            print(f"[{time.strftime('%H:%M:%S')}] {st.get('JobStatus')} {p}% "
                  f"({(time.time()-t0)/60:.1f}min)", flush=True); last = p
            if isinstance(p, (int, float)):
                print(f"RENDER_PCT {int(p)}", flush=True)   # parsed by stages._resolve for the bar
        time.sleep(20)
    st = proj.GetRenderJobStatus(jid)
    print(f"[{time.strftime('%H:%M:%S')}] FINAL {st}", flush=True)
    if os.path.exists(out):
        pj = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-of", "json", out],
                            capture_output=True, text=True).stdout
        v = [s for s in json.loads(pj)["streams"] if s.get("codec_type") == "video"][0]
        dv = [x for x in v.get("side_data_list", []) if x.get("side_data_type") == "DOVI configuration record"]
        print(f"OUTPUT {os.path.getsize(out)/1e9:.1f}GB | {v.get('codec_name')}/{v.get('profile')} "
              f"{v.get('width')}x{v.get('height')} @ {v.get('r_frame_rate')} "
              f"{v.get('color_transfer')}/{v.get('color_primaries')}", flush=True)
        print(f"FRAME RATE: {v.get('r_frame_rate')} (want 24000/1001)", flush=True)
        print(f"DOLBY VISION: {('YES p'+str(dv[0]['dv_profile'])+' compat'+str(dv[0].get('dv_bl_signal_compatibility_id'))) if dv else 'NO RPU'}", flush=True)
    return 0


def setup_single(video, mode=MODE_DV2000, superscale=0):
    """Single-file variant of setup() for the HIGH-BITRATE 4K FAST PATH: the ORIGINAL source
    goes on the timeline as ONE clip (no topaz segments exist — the source picture is the
    deliverable; Resolve runs only to produce the DV analysis/conversion). Same persistent
    project + inherited color management as setup(); fps comes from the clip itself and is
    set BEFORE the timeline exists (same conform rule as the segment path)."""
    print(f"[{time.strftime('%H:%M:%S')}] launching Resolve… (single, mode={mode})", flush=True)
    resolve = connect(launch=True)
    if not resolve:
        print("CONNECT FAILED"); return 1
    print(f"[{time.strftime('%H:%M:%S')}] connected: {resolve.GetVersionString()}", flush=True)
    pm = resolve.GetProjectManager()
    want, exists = project_for(pm, mode)
    if not exists:
        print(f"MISSING PROJECT '{want}' (mode={mode}): configure it ONCE in Resolve "
              f"(color management to HDR PQ + DV Profile 8.1 + "
              f"{target_nits(mode)}-nit Target Display), then re-run. "
              f"The pipeline inherits those and will not create a blank project.")
        return 1
    pm.LoadProject(want)
    proj = pm.GetCurrentProject()
    # ONTO THE PINNED DISPLAY NOW, not when the DV step starts. Everything below —
    # clearing the project, importing the chunks, building and validating the timeline —
    # is a minute or more of Resolve sitting in front of whatever the user is doing. This
    # is also the EARLIEST it CAN move: the Project Manager window refuses to be
    # positioned at all, so there is nothing to move until a project is open.
    _place_now()
    m = _resume_get(video, mode)
    if m and m.get("ident") == _src_ident(video) and _validate_resume_single(proj, video, m):
        _resume_setup(resolve, proj, "import + scene cuts skipped",
                      ", analysis already complete" if m.get("analyzed") else "")
        return 0
    _resume_reset(video, mode)          # entering the fresh path: no stale step may survive
    _clear_project(proj)
    mp = proj.GetMediaPool()
    clips = mp.ImportMedia([video])
    if not clips or len(clips) != 1:
        print(f"IMPORT FAILED: {len(clips) if clips else 0}/1 clips"); return 1
    if superscale and int(superscale) > 1:
        # SUPERSCALE — a Media Pool clip PROPERTY, set through the scripting API before
        # the timeline is built: no screen navigation involved (user-asked 2026-08-06;
        # 1080p YouTube → 2x = the clip lands native-4K on the 2160 timeline). Studio
        # feature — present here, since the DV analysis this pipeline exists for already
        # requires Studio. BEST-EFFORT: a failed set degrades to the timeline's plain
        # scaling and never fails the pass; the readback line is the proof either way.
        try:
            setok = clips[0].SetClipProperty("Super Scale", int(superscale))
            print(f"SUPERSCALE {superscale}x set={setok} "
                  f"readback={clips[0].GetClipProperty('Super Scale')!r}", flush=True)
        except Exception as e:
            print(f"SUPERSCALE UNAVAILABLE: {e.__class__.__name__}: {e}", flush=True)
    src_fps = clips[0].GetClipProperty("FPS")   # Resolve's own notion (e.g. '23.976') — no
    if not src_fps:                             # fraction-vs-decimal format mismatch possible
        print("FPS UNREADABLE from the imported clip"); return 1
    proj.SetSetting("timelineFrameRate", str(src_fps))   # BEFORE the timeline exists
    print(f"[{time.strftime('%H:%M:%S')}] single clip @ {src_fps}fps | "
          f"INHERITED color out={proj.GetSetting('colorSpaceOutput')!r}", flush=True)
    tl = mp.CreateTimelineFromClips("_tl", clips)
    tl_fps = tl.GetSetting("timelineFrameRate")
    if abs(float(tl_fps) - float(src_fps)) > 0.01:   # a conform would drop/dupe frames — the
        print(f"FRAME-RATE CONFORM! tl={tl_fps!r} src={src_fps!r}"); return 1   # RPU gate's enemy
    try:
        items = tl.GetItemListInTrack("video", 1) or []
        if len(items) != 1:
            print(f"TIMELINE ASSEMBLY WRONG: {len(items)} clips on the timeline, expected 1")
            return 1
        got = tl.GetEndFrame() - tl.GetStartFrame() + 1
        print(f"[{time.strftime('%H:%M:%S')}] timeline = 1 clip, {got} frames", flush=True)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] (timeline integrity unverifiable: {e})", flush=True)
    resolve.OpenPage("color")
    t = time.time()
    ok = tl.DetectSceneCuts()
    shots = tl.GetItemListInTrack("video", 1)
    print(f"[{time.strftime('%H:%M:%S')}] DetectSceneCuts={ok} ({(time.time()-t)/60:.1f}min) "
          f"shots={len(shots) if shots else 0}", flush=True)
    try:                        # step COMPLETE → durable: a killed pass resumes past it
        frames = tl.GetEndFrame() - tl.GetStartFrame() + 1
        _resume_put(video, mode, ident=_src_ident(video), frames=int(frames),
                    scenes=True, analyzed=False)
    except Exception:
        pass
    print("SETUP_DONE — Resolve on Color page, ready for DV Analyze All Shots (shim)", flush=True)
    return 0


def single(video, out, mode=MODE_DV2000, bitrate=60000, superscale=0):
    """The whole FAST-PATH resolve stage in one process: single-file setup -> DV Analyze All
    (UI shim) -> render. Mirrors episode(); run as a killable subprocess the same way."""
    rc = setup_single(video, mode, superscale)
    if rc != 0:
        return rc
    if not is_dv_mode(mode):
        print("SDR output — skipping DV analyze (headless render)", flush=True)
        return render(out, mode, bitrate)
    m = _resume_get(video, mode)
    if m and m.get("analyzed") and m.get("ident") == _src_ident(video):
        # The kill happened AFTER Analyze All completed (during render): the trims are in
        # the persistent project — only the render needs redoing. The marker can only say
        # analyzed on the RESUME path (a fresh setup resets it first).
        print("RESUME: DV analysis already complete — skipping Analyze All Shots", flush=True)
        return render(out, mode, bitrate)
    import dv_shim
    try:
        if not dv_shim.run_dv_ui(expect_nit=target_nits(mode),
                                 analysis_max_seconds=_analysis_budget(video)):
            print("DV_UI_INCOMPLETE — analyze did not finish (grants/cliclick?)", flush=True)
            return 2
    except Exception as e:
        if e.__class__.__name__ == "HostUnavailable":
            # NOT a render failure: the pinned display could not be used. The stage turns
            # this into a deferral rather than falling back to seizing the main screen.
            print(f"HOST_UNAVAILABLE {e}", flush=True)
            return 3
        print(f"DV_UI_EXC: {e.__class__.__name__}: {e}", flush=True)
        return 2
    _mark_analyzed(video, mode)         # step COMPLETE → a kill during render resumes past it
    return render(out, mode, bitrate)


def episode(segdir, out, mode=MODE_DV1000, bitrate=60000):
    """The WHOLE resolve stage in one process: setup (assemble the Topaz chunks on the
    timeline) -> DV Analyze All (UI shim) -> render. Run as a SUBPROCESS so a hung
    fusionscript call (Resolve unresponsive) can be killed from outside.
    `mode` ('dv1000'|'dv2000'|'sdr') selects the project + the DV analyze target ceiling
    ('sdr' has no DV stage at all, so the whole run is headless); `bitrate`
    (Kb/s) is the export bitrate (already max'd against the intake by the caller)."""
    rc = setup(segdir, mode)
    if rc != 0:
        return rc
    if not is_dv_mode(mode):
        # TRUE SDR: no Dolby Vision, so no Analyze All, so no screen automation. The whole
        # stage is headless — it never takes the display, never needs cliclick or the
        # Screen Recording grant, and cannot be blocked by Screen Control.
        print("SDR output — skipping DV analyze (headless render)", flush=True)
        return render(out, mode, bitrate)
    m = _resume_get(segdir, mode)
    if m and m.get("analyzed"):
        # Killed AFTER Analyze All completed (during render): the trims persist in the
        # project — only the render needs redoing. Only reachable via the RESUME path
        # (a fresh setup resets the marker first).
        print("RESUME: DV analysis already complete — skipping Analyze All Shots", flush=True)
        return render(out, mode, bitrate)
    import dv_shim
    try:
        # Episodes are TV-length by construction — the flat hour has never been the
        # wall for them; only single() (features, incl. 4 h fan cuts) scales.
        if not dv_shim.run_dv_ui(expect_nit=target_nits(mode)):
            print("DV_UI_INCOMPLETE — analyze did not finish (grants/cliclick?)", flush=True)
            return 2
    except Exception as e:
        if e.__class__.__name__ == "HostUnavailable":
            # NOT a render failure: the pinned display could not be used. The stage turns
            # this into a deferral rather than falling back to seizing the main screen.
            print(f"HOST_UNAVAILABLE {e}", flush=True)
            return 3
        print(f"DV_UI_EXC: {e.__class__.__name__}: {e}", flush=True)
        return 2
    _mark_analyzed(segdir, mode)        # step COMPLETE → a kill during render resumes past it
    return render(out, mode, bitrate)


def _set_host_from_argv(key):
    """Point dv_shim at the pinned display, or leave it on the main one."""
    if not key or key == "-":
        return
    try:
        import displays, dv_shim
        d = displays.find(key)
        if d is None:
            # NOT a silent fall back to main: that would seize the very screen the user
            # pinned Resolve away from. The stage turns this marker into a deferral.
            print(f"HOST_UNAVAILABLE {key} not attached", flush=True)
            return "missing"
        dv_shim.set_host(d)
        print(f"host display: {key} origin={d['origin']} size_pt={d['size_pt']}", flush=True)
    except Exception as e:
        print(f"HOST DISPLAY ERROR: {e.__class__.__name__}: {e}", flush=True)


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else ""
    a = sys.argv[2:]
    if phase == "setup":
        sys.exit(setup(a[0], a[1] if len(a) > 1 else MODE_DV1000))
    elif phase == "render":
        sys.exit(render(a[0], a[1] if len(a) > 1 else MODE_DV1000, int(a[2]) if len(a) > 2 else 60000))
    elif phase in ("episode", "single"):
        # 5th positional arg (index 4) = the display key to drive, "-" for the main one.
        # Set BEFORE any capture: dv_shim fixes its target once per process on purpose, so
        # a settings change mid-episode cannot move the screen under a running match.
        if _set_host_from_argv(a[4] if len(a) > 4 else "-") == "missing":
            sys.exit(3)                    # distinct rc: the display, not the render
        if phase == "episode":
            sys.exit(episode(a[0], a[1], a[2] if len(a) > 2 else MODE_DV1000,
                             int(a[3]) if len(a) > 3 else 60000))
        # 6th positional (index 5) = SuperScale factor for single mode; "-" = none.
        _ss = a[5] if len(a) > 5 else "-"
        sys.exit(single(a[0], a[1], a[2] if len(a) > 2 else MODE_DV2000,
                        int(a[3]) if len(a) > 3 else 60000,
                        superscale=int(_ss) if _ss.isdigit() else 0))
    else:
        print("usage: resolve_pipeline.py setup <src> [mode] | render <out> [mode] [kbps] "
              "| episode <prores> <out> [mode] [kbps] | single <video> <out> [mode] [kbps]")
        sys.exit(2)
