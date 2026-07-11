"""Resolve export stage — delivery (export) settings + helpers.

COLOR MANAGEMENT IS NOT SET HERE. The pipeline INHERITS whatever color management
the persistent "Overnight Upscaler" project already has: the user configures it
once in Resolve (HDR PQ / Dolby Vision) and the pipeline NEVER overrides it. An
earlier version flipped Custom→Automatic via the scripting API and risked
rendering SDR instead of HDR — inheriting the project's own settings is the robust
fix (set it once, it keeps doing what you set).

The delivery half below (codec / container / DV profile / mute) is the EXPORT
format, which is separate from color management — the DV Profile 8.1 dropdown is
still chosen per export via the UI shim.
"""
from __future__ import annotations
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from versions import RESOLVE_APP  # noqa: E402 — exact-version pin (versions.py); preflight gates on it
FUSIONSCRIPT = RESOLVE_APP + "/Contents/Libraries/Fusion/fusionscript.so"
APP_PROJECT = "Overnight Upscaler"

# Delivery (export format) — NOT color management. The render inherits color from
# the project; this is the codec/container/DV-profile the deliverable carries.
_RENDER_PRESET = {
    "format": "mov", "codec": "H265", "encoding_profile": "Main10",
    "bitrate_kbps": 60000, "multipass": True, "bypass_reencode": True,
    "export_hdr10_sidecar": False, "embed_hdr10": True,
    "dolby_vision_profile": "8.1", "tone_mapping": "None",
    "render_mode": "SingleClip", "export_audio": False,   # mute; audio+subs remuxed
    "width": 3840, "height": 2160, "frame_rate": "23.976",
}


def render_preset():
    """Delivery (export) settings — codec/DV/mute. NOT color management."""
    return dict(_RENDER_PRESET)


# --- integration (runs against a live Resolve project) ---------------------

def connect():
    """Connect to a running Resolve via fusionscript (bypasses py3.12 `imp`)."""
    if not os.path.exists(FUSIONSCRIPT):
        return None
    spec = importlib.util.spec_from_file_location("fusionscript", FUSIONSCRIPT)
    fs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fs)
    return fs.scriptapp("Resolve")


def set_format_codec(project) -> bool:
    rp = render_preset()
    return project.SetCurrentRenderFormatAndCodec(rp["format"], rp["codec"])


def inspect_color_management(project) -> dict:
    """READ-ONLY view of the project's CURRENT color management — we never change
    it. Use to log / verify the user's HDR setup is in place before a run."""
    keys = ("colorScienceMode", "isAutoColorManage", "rcmPresetMode",
            "colorSpaceOutput", "hdrDolbyControlsOn", "hdrDolbyMasterDisplay")
    return {k: project.GetSetting(k) for k in keys}


def is_hdr_project(project) -> bool:
    """True if the project's output is HDR PQ — a guard so a misconfigured project
    (defaulting to SDR Rec.709) doesn't silently produce an SDR master."""
    return project.GetSetting("colorSpaceOutput") == "Rec.2100 ST2084"


def hdr_summary(probe_json: str) -> dict:
    for s in json.loads(probe_json).get("streams", []):
        if s.get("codec_type") == "video":
            return {"codec": s.get("codec_name"), "profile": s.get("profile"),
                    "width": s.get("width"), "height": s.get("height"),
                    "transfer": s.get("color_transfer"), "primaries": s.get("color_primaries")}
    return {}


def is_hdr10(summary: dict) -> bool:
    """True for a 4K 10-bit Rec.2020 ST2084 stream — the render's HDR output."""
    return (summary.get("transfer") == "smpte2084"
            and summary.get("primaries") == "bt2020"
            and "Main 10" in str(summary.get("profile", ""))
            and summary.get("width") == 3840 and summary.get("height") == 2160)


def render_clip(resolve, input_path: str, out_dir: str, *, custom_name="output",
                project_name=APP_PROJECT, timeout=240):
    """Scripted pass that INHERITS the project's color management: load the app
    project (create it only the first time), clear it, import -> set fps -> timeline
    -> DetectSceneCuts -> render a mute .mov. Color/HDR/DV come from the project, so
    configure it once in Resolve first (else a fresh project renders SDR)."""
    import glob
    import time
    pm = resolve.GetProjectManager()
    if project_name in pm.GetProjectListInCurrentFolder():
        pm.LoadProject(project_name)
    else:
        pm.CreateProject(project_name)
    proj = pm.GetCurrentProject()
    mp = proj.GetMediaPool()
    try:   # clear previous contents — settings untouched
        tls = [proj.GetTimelineByIndex(i) for i in range(1, (proj.GetTimelineCount() or 0) + 1)]
        if tls:
            mp.DeleteTimelines(tls)
        old = mp.GetRootFolder().GetClipList()
        if old:
            mp.DeleteClips(old)
    except Exception:
        pass
    clips = mp.ImportMedia([input_path])
    if not clips:
        raise RuntimeError(f"ImportMedia failed for {input_path}")
    # match the timeline frame rate to the SOURCE before the timeline exists, or
    # Resolve conforms the footage to the project default (e.g. 24) — NOT color.
    src_fps = clips[0].GetClipProperty("FPS")
    if src_fps:
        proj.SetSetting("timelineFrameRate", str(src_fps))
    tl = mp.CreateTimelineFromClips("_tl", clips)
    if tl.GetSetting("timelineFrameRate") != str(src_fps):
        raise RuntimeError(f"frame-rate conform! timeline={tl.GetSetting('timelineFrameRate')} src={src_fps}")
    tl.DetectSceneCuts()
    proj.SetCurrentRenderFormatAndCodec("mov", "H265")
    proj.SetRenderSettings({"TargetDir": out_dir, "CustomName": custom_name,
                            "ExportVideo": True, "ExportAudio": False})
    jid = proj.AddRenderJob()
    proj.StartRendering(jid)
    t0 = time.time()
    while proj.IsRenderingInProgress() and time.time() - t0 < timeout:
        time.sleep(1)
    outs = sorted(glob.glob(os.path.join(out_dir, custom_name + "*")))
    return outs[0] if outs else None


if __name__ == "__main__":
    import sys
    r = connect()
    if not r:
        print("Resolve not reachable (running? external scripting = Local?)"); sys.exit(2)
    proj = r.GetProjectManager().GetCurrentProject()
    print("project:", proj.GetName())
    print("INHERITED color management (read-only):")
    for k, v in inspect_color_management(proj).items():
        print(f"  {k} = {v!r}")
    print("HDR output:", "YES" if is_hdr_project(proj)
          else "NO — project is SDR; set it to HDR PQ or the master will be SDR")
