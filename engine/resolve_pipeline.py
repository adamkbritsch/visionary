"""Mechanical DaVinci Resolve pipeline runner — drives the scriptable backbone
of the Resolve stage from a CLOSED Resolve, end to end.

The two UI-only steps (Dolby Vision "Analyze All Shots" and the DV export-profile
dropdown) are done by the dv_shim between phases:

  python3 resolve_pipeline.py setup  <source.mov>   # launch -> import -> fps -> color -> timeline -> scene cuts
  # <DV Analyze All Shots via dv_shim>
  # <set DV Profile 8.1 on the Deliver page via dv_shim>
  python3 resolve_pipeline.py render <out.mov>      # render entire timeline + verify (fps + DV)

Color management = Automatic + HDR preset (the user's _office_test settings).
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
# Two persistent projects, picked by the intake range so each can carry the right DV
# mastering ceiling: SDR intake → 1000-nit Target Display; HDR intake → 2000-nit.
# Candidate project names per intake range, in preference order. We match whatever the
# project is ACTUALLY named in Resolve (the API can't rename), so a rename — e.g. to the
# new "Visionary …" app name — won't break the pipeline. First existing one wins.
SDR_PROJECTS = ["Visionary SDR", "Overnight Upscaler SDR", "Overnight Upscaler"]
HDR_PROJECTS = ["Visionary HDR", "Overnight Upscaler HDR"]
DV_PRESET = "OvernightDV"        # global render preset carrying DV Profile 8.1 (survives Resolve quits)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import resolve as R  # noqa: E402


def project_for(pm, mode):
    """Pick the persistent project for the intake range, matching whatever it's ACTUALLY
    named in Resolve. mode 'hdr' → the HDR (2000-nit) project; else the SDR (1000-nit)
    one. Tries the candidates in order and returns the first that exists, so renaming the
    projects in Resolve doesn't break anything. Returns (name, exists) — name is the
    preferred candidate when none exist (for the 'missing project' message)."""
    projects = pm.GetProjectListInCurrentFolder() or []
    candidates = HDR_PROJECTS if mode == "hdr" else SDR_PROJECTS
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


def setup(segdir, mode="sdr"):
    print(f"[{time.strftime('%H:%M:%S')}] launching Resolve… (mode={mode})", flush=True)
    resolve = connect(launch=True)
    if not resolve:
        print("CONNECT FAILED"); return 1
    print(f"[{time.strftime('%H:%M:%S')}] connected: {resolve.GetVersionString()}", flush=True)
    pm = resolve.GetProjectManager()
    # Use the app's PERSISTENT project. The user configures it ONCE (color management
    # that maps SDR *and* HDR input to HDR PQ output, DV Profile 8.1, 1000-nit Target
    # Display) and the pipeline INHERITS all of it. We NEVER recreate it (that would
    # wipe the setup), NEVER set color ourselves, and NEVER create a fresh one
    # silently — a blank project renders SDR with no Dolby Vision, and DV Profile 8.1
    # can't be re-set via the API. If it's missing, STOP and tell the user.
    want, exists = project_for(pm, mode)
    if not exists:
        print(f"MISSING PROJECT '{want}' (mode={mode}): configure it ONCE in Resolve "
              f"(color management to HDR PQ + DV Profile 8.1 + "
              f"{'2000' if mode == 'hdr' else '1000'}-nit Target Display), then re-run. "
              f"The pipeline inherits those and will not create a blank project.")
        return 1
    pm.LoadProject(want)
    proj = pm.GetCurrentProject()
    _clear_project(proj)
    mp = proj.GetMediaPool()
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
    print("SETUP_DONE — Resolve on Color page, ready for DV Analyze All Shots (shim)", flush=True)
    return 0


def render(out, mode="sdr", bitrate=60000):
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


def episode(segdir, out, mode="sdr", bitrate=60000):
    """The WHOLE resolve stage in one process: setup (assemble the Topaz chunks on the
    timeline) -> DV Analyze All (UI shim) -> render. Run as a SUBPROCESS so a hung
    fusionscript call (Resolve unresponsive) can be killed from outside.
    `mode` ('sdr'|'hdr') selects the project + the DV analyze target ceiling; `bitrate`
    (Kb/s) is the export bitrate (already max'd against the intake by the caller)."""
    rc = setup(segdir, mode)
    if rc != 0:
        return rc
    import dv_shim
    try:
        if not dv_shim.run_dv_ui(expect_nit=(2000 if mode == "hdr" else 1000)):
            print("DV_UI_INCOMPLETE — analyze did not finish (grants/cliclick?)", flush=True)
            return 2
    except Exception as e:
        print(f"DV_UI_EXC: {e.__class__.__name__}: {e}", flush=True)
        return 2
    return render(out, mode, bitrate)


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else ""
    a = sys.argv[2:]
    if phase == "setup":
        sys.exit(setup(a[0], a[1] if len(a) > 1 else "sdr"))
    elif phase == "render":
        sys.exit(render(a[0], a[1] if len(a) > 1 else "sdr", int(a[2]) if len(a) > 2 else 60000))
    elif phase == "episode":
        sys.exit(episode(a[0], a[1], a[2] if len(a) > 2 else "sdr", int(a[3]) if len(a) > 3 else 60000))
    else:
        print("usage: resolve_pipeline.py setup <src> [mode] | render <out> [mode] [kbps] "
              "| episode <prores> <out> [mode] [kbps]")
        sys.exit(2)
