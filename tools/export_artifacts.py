#!/usr/bin/env python3
"""MAINTAINER-ONLY: export the bundled Resolve/Topaz artifacts into bundle/.

Produces (committed to the repo, imported on a new user's machine by setup/import_resolve.py):
  bundle/resolve/Visionary SDR.drp        — the SDR persistent project, exported EMPTY
  bundle/resolve/Visionary HDR.drp        — the HDR persistent project, exported EMPTY
  bundle/resolve/OvernightDV.element.xml  — the global DV render preset <Element> (scrubbed)
  bundle/topaz/*.json                     — the Topaz GUI presets (reference only)

Run with /usr/bin/python3 (fusionscript needs the system Python <=3.11). Requires the live
pipeline to be at a SAFE point: it refuses while the run thread is in/near its resolve stage
(this tool drives the same Resolve instance). Projects are exported EMPTY — _clear_project
(the pipeline's own per-run reset) runs first so no media paths/episode names embed in the .drp.

The rename round-trip (temp folder → import → SetName "Visionary *" → re-export) ships the
projects under the NEW names while the maintainer's live library keeps its legacy names
(resolve_pipeline's candidate lists accept both).
"""
import json
import os
import re
import shutil
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "engine"))

import resolve_pipeline as rp  # noqa: E402

BUNDLE_RESOLVE = os.path.join(REPO, "bundle", "resolve")
BUNDLE_TOPAZ = os.path.join(REPO, "bundle", "topaz")
DELIVER_PRESETS = os.path.expanduser(
    "~/Library/Application Support/Blackmagic Design/DaVinci Resolve/"
    "Resolve Project Library/Resolve Projects/Settings/DeliverPresetList.xml")
TOPAZ_BACKUP = os.path.expanduser("~/Documents/Topaz Presets Backup")
TMP_FOLDER = "VisionaryExportTmp"
API = "http://127.0.0.1:8765"


def pipeline_safe() -> tuple:
    """(safe, reason). Safe = the run thread is NOWHERE NEAR its resolve stage: this tool
    loads/clears/saves projects in the same Resolve the pipeline drives, so it must never
    overlap that stage. The finisher (remux/upload) never touches Resolve — it doesn't gate.
    Safe when: server down, orchestrator disabled, or stage is download/idle, or topaz with
    >=3 segments still to go (>=~15 min — the export takes ~2)."""
    try:
        with urllib.request.urlopen(API + "/api/state", timeout=4) as r:
            d = json.load(r)
    except Exception:
        return True, "dashboard server not running — pipeline is down"
    o = d.get("orchestrator") or {}
    if not o.get("enabled"):
        return True, "orchestrator disabled"
    stage = o.get("stage")
    if stage in (None, "download", "cleanup"):
        return True, f"run thread at {stage or 'idle'}"
    if stage == "topaz":
        pr = o.get("progress") or {}
        done, total = pr.get("seg_done"), pr.get("seg_total")
        if done is not None and total and total - done >= 3:
            return True, f"topaz at segment {done}/{total} — resolve is >=3 segments away"
        return False, f"topaz nearly done ({done}/{total}) — resolve is imminent"
    return False, f"run thread is in {stage}"


def _strip_stills(drp_path):
    """Remove gallery still payloads (stills/*) from a .drp zip. A still is a JPEG of graded
    LIBRARY CONTENT — a copyright + privacy leak. The GyStillRef left in Gallery.xml is just a
    UUID (no leak); Resolve tolerates the missing payload (verified by the re-import below).
    (The scripting API's DeleteStills claims success but doesn't persist in 18.6 — hence zip
    surgery instead.)"""
    import zipfile
    stripped = 0
    tmp = drp_path + ".tmp"
    with zipfile.ZipFile(drp_path) as zin, \
         zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename.startswith("stills/"):
                stripped += 1
                continue
            zout.writestr(item, zin.read(item.filename))
    os.replace(tmp, drp_path)
    return stripped


def export_projects():
    r = rp.connect(launch=True)
    if not r:
        raise SystemExit("could not connect to DaVinci Resolve's scripting API")
    pm = r.GetProjectManager()
    print(f"connected: Resolve {r.GetVersionString()}")
    pm.GotoRootFolder()
    os.makedirs(BUNDLE_RESOLVE, exist_ok=True)
    tmpdir = os.path.join(BUNDLE_RESOLVE, ".tmp")
    os.makedirs(tmpdir, exist_ok=True)
    for mode, new_name in (("sdr", "Visionary SDR"), ("hdr", "Visionary HDR")):
        legacy, exists = rp.project_for(pm, mode)
        if not exists:
            raise SystemExit(f"{mode.upper()} project not found (looked for candidates ending "
                             f"with '{legacy}') — nothing to export")
        print(f"[{mode}] source project: {legacy!r}")
        proj = pm.LoadProject(legacy)
        if not proj:
            raise SystemExit(f"could not load {legacy!r}")
        rp._clear_project(proj)                    # export EMPTY: no media paths in the .drp
        pm.SaveProject()
        tmp_drp = os.path.join(tmpdir, f"{legacy}.drp")
        if os.path.exists(tmp_drp):
            os.remove(tmp_drp)
        if not pm.ExportProject(legacy, tmp_drp):
            raise SystemExit(f"ExportProject({legacy!r}) failed")
        print(f"[{mode}] exported empty project → {tmp_drp}")
        # rename round-trip in a TEMP FOLDER (no name clash with the live project):
        pm.GotoRootFolder()
        folders = pm.GetFolderListInCurrentFolder() or []
        if TMP_FOLDER not in folders:
            pm.CreateFolder(TMP_FOLDER)
        if not pm.OpenFolder(TMP_FOLDER):
            raise SystemExit(f"could not open temp folder {TMP_FOLDER}")
        try:
            # clear leftovers from a previous (refused/crashed) run — else ImportProject clashes
            for stale in set(pm.GetProjectListInCurrentFolder() or []) & {legacy, new_name}:
                if pm.DeleteProject(stale):
                    print(f"[{mode}] cleared stale temp copy {stale!r}")
            if not pm.ImportProject(tmp_drp):
                raise SystemExit(f"ImportProject({tmp_drp}) failed — round-trip broken")
            proj2 = pm.LoadProject(legacy)
            if not proj2:
                raise SystemExit(f"re-import produced no {legacy!r} in {TMP_FOLDER}")
            # Wipe gallery stills from the SHIPPED copy only (the maintainer's live project keeps
            # its stills): a still is a JPEG of graded LIBRARY CONTENT — both a copyright and a
            # privacy leak, and 1+ MB of dead weight in the .drp.
            try:
                gal = proj2.GetGallery()
                for album in (gal.GetGalleryStillAlbums() or []):
                    stills = album.GetStills() or []
                    if stills:
                        album.DeleteStills(stills)
                        print(f"[{mode}] wiped {len(stills)} gallery still(s) from the shipped copy")
            except Exception as e:
                print(f"[{mode}] WARN: gallery wipe failed ({e}) — check the .drp for stills/")
            renamed = False
            try:
                renamed = bool(proj2.SetName(new_name))
            except Exception:
                pass
            ship_name = new_name if renamed else legacy
            if not renamed:
                print(f"[{mode}] WARN: SetName unavailable — shipping under legacy name "
                      f"{legacy!r} (the candidate lists accept it)")
            pm.SaveProject()
            out = os.path.join(BUNDLE_RESOLVE, f"{ship_name}.drp")
            if os.path.exists(out):
                os.remove(out)
            if not pm.ExportProject(ship_name, out):
                raise SystemExit(f"ExportProject({ship_name!r}) failed")
            n = _strip_stills(out)
            if n:
                print(f"[{mode}] stripped {n} gallery-still file(s) from the shipped .drp")
            print(f"[{mode}] shipped: {out}")
            try:
                pm.CloseProject(proj2)     # DeleteProject refuses on the CURRENT project
            except Exception:
                pass
            if not pm.DeleteProject(ship_name):
                print(f"[{mode}] note: temp copy {ship_name!r} left in folder "
                      f"{TMP_FOLDER!r} (harmless — delete by hand if you like)")
            # ROUND-TRIP VERIFY the shipped artifact:
            #  - the ZIP must contain no stills/ payload (THE leak test — a dangling UUID ref
            #    in Gallery.xml is fine and Resolve tolerates it),
            #  - it must import cleanly,
            #  - the color config must survive (settings live in binary blobs — reading them
            #    back through the API is the only real proof).
            import zipfile
            with zipfile.ZipFile(out) as z:
                leaked = [f for f in z.namelist() if f.startswith("stills/")]
            if leaked:
                raise SystemExit(f"VERIFY: stills payload survived in {out}: {leaked}")
            if not pm.ImportProject(out):
                raise SystemExit(f"VERIFY: ImportProject({out}) failed — shipped .drp is broken")
            vproj = pm.LoadProject(ship_name)
            if not vproj:
                raise SystemExit(f"VERIFY: shipped .drp did not import as {ship_name!r}")
            color_out = vproj.GetSetting("colorSpaceOutput")
            print(f"[{mode}] VERIFY: imports OK, colorSpaceOutput={color_out!r}, no stills payload")
            if not color_out:
                raise SystemExit(f"VERIFY: {ship_name!r} imported with NO color config — refusing")
            try:
                pm.CloseProject(vproj)
            except Exception:
                pass
            pm.DeleteProject(ship_name)
        finally:
            pm.GotoRootFolder()
    # temp folder cleanup (best effort — the API can't delete folders in some builds)
    try:
        pm.DeleteFolder(TMP_FOLDER)
    except Exception:
        pass
    shutil.rmtree(tmpdir, ignore_errors=True)


def export_preset_element():
    """Extract the OvernightDV <Element> from the global DeliverPresetList.xml, scrub the
    stale local values, keep the binary FieldsBlob byte-intact (it carries DolbyVisionProfile
    — the one setting with no scripting API)."""
    tree = ET.parse(DELIVER_PRESETS)
    root = tree.getroot()
    target = None
    for el in root.iter("Element"):
        k = el.find("DbKey")
        if k is not None and (k.text or "").strip() == "OvernightDV":
            target = el
            break
    if target is None:
        raise SystemExit(f"OvernightDV not found in {DELIVER_PRESETS}")
    for tag in ("RecordTargetDir", "RecordPrefix"):
        node = target.find(f".//{tag}")
        if node is not None and node.text:
            print(f"scrubbed {tag}: {node.text!r} -> ''")
            node.text = ""
    os.makedirs(BUNDLE_RESOLVE, exist_ok=True)
    out = os.path.join(BUNDLE_RESOLVE, "OvernightDV.element.xml")
    ET.ElementTree(target).write(out, encoding="utf-8", xml_declaration=True)
    print(f"shipped: {out}")


def export_topaz_presets():
    if not os.path.isdir(TOPAZ_BACKUP):
        print(f"WARN: {TOPAZ_BACKUP} not found — skipping Topaz GUI presets (runtime "
              f"doesn't need them; they're reference only)")
        return
    os.makedirs(BUNDLE_TOPAZ, exist_ok=True)
    n = 0
    for f in sorted(os.listdir(TOPAZ_BACKUP)):
        if f.endswith(".json"):
            shutil.copy2(os.path.join(TOPAZ_BACKUP, f), os.path.join(BUNDLE_TOPAZ, f))
            n += 1
    print(f"shipped: {n} Topaz preset JSONs → {BUNDLE_TOPAZ}")


def main():
    safe, reason = pipeline_safe()
    print(f"pipeline gate: {reason}")
    if not safe:
        raise SystemExit("REFUSING to export while the pipeline is near its resolve stage — "
                         "re-run when topaz has >=3 segments left, or disable the run")
    export_preset_element()      # pure file ops first (no Resolve needed)
    export_topaz_presets()
    export_projects()            # drives Resolve — the gated part
    print("done.")


if __name__ == "__main__":
    main()
