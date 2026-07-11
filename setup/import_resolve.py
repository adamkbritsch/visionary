#!/usr/bin/env python3
"""Import Visionary's bundled DaVinci Resolve artifacts on a NEW machine (setup step 7).

What it does, in order:
  1. MERGES the OvernightDV render preset into your global DeliverPresetList.xml.
     That preset carries the Dolby Vision 8.1 profile — the one setting Resolve's
     scripting API cannot set — so it must arrive as a file, and only while Resolve
     is QUIT (Resolve rewrites the file on exit and would clobber the merge).
  2. Launches Resolve and IMPORTS the two persistent projects (bundle/resolve/*.drp):
     "Visionary SDR" + "Visionary HDR" — skipped if you already have a matching
     project (idempotent; re-running is safe).
  3. VERIFIES: both projects present, the preset visible in GetRenderPresetList().

Run with the system python: /usr/bin/python3 setup/import_resolve.py [--json] [--dry-run]
(Resolve's fusionscript needs Python <=3.11 — /usr/bin/python3 qualifies.)

Prerequisites: preflight hard checks pass (exact Resolve/Topaz/display), Resolve has been
launched at least once (so the project library exists), and Resolve is currently QUIT.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "engine"))

BUNDLE = os.path.join(REPO, "bundle", "resolve")
ELEMENT_XML = os.path.join(BUNDLE, "OvernightDV.element.xml")
SETTINGS_DIR = os.path.expanduser(
    "~/Library/Application Support/Blackmagic Design/DaVinci Resolve/"
    "Resolve Project Library/Resolve Projects/Settings")
DELIVER_PRESETS = os.path.join(SETTINGS_DIR, "DeliverPresetList.xml")
SDR_CANDIDATES = ("Visionary SDR", "Overnight Upscaler SDR", "Overnight Upscaler")
HDR_CANDIDATES = ("Visionary HDR", "Overnight Upscaler HDR")


def _result(step, ok, detail):
    return {"step": step, "ok": bool(ok), "detail": detail}


def resolve_running() -> bool:
    return subprocess.run(["pgrep", "-x", "DaVinci Resolve"],
                          capture_output=True).returncode == 0


def merge_preset(dry_run=False):
    """Idempotently merge the bundled OvernightDV <Element> into DeliverPresetList.xml.
    Backs the file up first; regenerates the element's DbId (a fresh uuid) so it can never
    collide with an existing row; replaces any previous OvernightDV element; atomic write.
    Never touches your other presets."""
    if resolve_running():
        return _result("preset_merge", False,
                       "DaVinci Resolve is RUNNING — quit it first (it rewrites the preset "
                       "file on exit and would clobber this merge)")
    if not os.path.exists(ELEMENT_XML):
        return _result("preset_merge", False, f"bundled element missing: {ELEMENT_XML}")
    new_el = ET.parse(ELEMENT_XML).getroot()
    # fresh row id — never collide with the user's existing presets
    rec = new_el.find(".//SyRecordInfo")
    if rec is not None and "DbId" in rec.attrib:
        rec.set("DbId", str(uuid.uuid4()))     # bare UUID — Resolve's own format in this file
    if os.path.exists(DELIVER_PRESETS):
        tree = ET.parse(DELIVER_PRESETS)
        root = tree.getroot()
    else:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        root = ET.Element("SmDeliverPresetList", {"DbId": str(uuid.uuid4())})
        ET.SubElement(root, "FieldsBlob")
        ET.SubElement(root, "PresetList")
        tree = ET.ElementTree(root)
    plist = root.find("PresetList")
    if plist is None:
        plist = ET.SubElement(root, "PresetList")
    removed = 0
    for el in list(plist):
        k = el.find("DbKey")
        if k is not None and (k.text or "").strip() == "OvernightDV":
            plist.remove(el)
            removed += 1
    plist.append(new_el)
    if dry_run:
        return _result("preset_merge", True,
                       f"DRY RUN: would merge OvernightDV (replacing {removed} existing) "
                       f"into {DELIVER_PRESETS}")
    if os.path.exists(DELIVER_PRESETS):
        backup = DELIVER_PRESETS + time.strftime(".bak.%Y%m%d-%H%M%S")
        shutil.copy2(DELIVER_PRESETS, backup)
    tmp = DELIVER_PRESETS + ".tmp"
    tree.write(tmp, encoding="UTF-8", xml_declaration=True)
    os.replace(tmp, DELIVER_PRESETS)
    return _result("preset_merge", True,
                   f"merged OvernightDV (replaced {removed} prior copy) into {DELIVER_PRESETS}")


def import_projects(dry_run=False):
    import resolve_pipeline as rp
    out = []
    # Discover the bundled projects by SDR/HDR marker in the filename — robust to whichever
    # name they shipped under (the engine's candidate lists accept legacy + "Visionary *").
    all_drps = sorted(f for f in os.listdir(BUNDLE) if f.endswith(".drp")) if os.path.isdir(BUNDLE) else []
    drps = {}
    for f in all_drps:
        if "SDR" in f.upper():
            drps["SDR"] = os.path.join(BUNDLE, f)
        elif "HDR" in f.upper():
            drps["HDR"] = os.path.join(BUNDLE, f)
    missing = [m for m in ("SDR", "HDR") if m not in drps]
    if missing:
        return [_result("project_import", False,
                        f"bundled .drp missing for: {', '.join(missing)} (found: {all_drps})")]
    if dry_run:
        return [_result("project_import", True, "DRY RUN: would launch Resolve and import "
                        + ", ".join(os.path.basename(p) for p in drps.values()))]
    r = rp.connect(launch=True)
    if not r:
        return [_result("project_import", False,
                        "could not reach Resolve's scripting API — is Resolve installed and "
                        "able to launch? (External scripting must be allowed: Preferences > "
                        "System > General > External scripting using = Local)")]
    pm = r.GetProjectManager()
    pm.GotoRootFolder()
    existing = set(pm.GetProjectListInCurrentFolder() or [])
    for mode, cands in (("SDR", SDR_CANDIDATES), ("HDR", HDR_CANDIDATES)):
        have = [c for c in cands if c in existing]
        if have:
            out.append(_result(f"project_{mode.lower()}", True,
                               f"already present: {have[0]!r} — skipped (idempotent)"))
            continue
        ok = bool(pm.ImportProject(drps[mode]))
        out.append(_result(f"project_{mode.lower()}", ok,
                           f"imported {os.path.basename(drps[mode])!r}" if ok
                           else f"ImportProject failed for {drps[mode]}"))
    # verify: projects visible + the preset visible to a loaded project
    existing = set(pm.GetProjectListInCurrentFolder() or [])
    sdr = next((c for c in SDR_CANDIDATES if c in existing), None)
    hdr = next((c for c in HDR_CANDIDATES if c in existing), None)
    out.append(_result("verify_projects", bool(sdr and hdr),
                       f"SDR={sdr!r} HDR={hdr!r} (Resolve {r.GetVersionString()})"))
    if sdr:
        proj = pm.LoadProject(sdr)
        presets = (proj.GetRenderPresetList() or []) if proj else []
        out.append(_result("verify_preset", "OvernightDV" in presets,
                           "OvernightDV visible in GetRenderPresetList()" if "OvernightDV" in presets
                           else "OvernightDV NOT visible — the preset merge didn't take "
                                "(was Resolve really quit during the merge?)"))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Import Visionary's Resolve projects + DV render preset")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    results = [merge_preset(dry_run=args.dry_run)]
    if results[0]["ok"]:
        results += import_projects(dry_run=args.dry_run)
    ok = all(r["ok"] for r in results)
    if args.json:
        print(json.dumps({"ok": ok, "steps": results}, indent=2))
    else:
        for r in results:
            print(f"[{'OK' if r['ok'] else 'FAIL'}] {r['step']}: {r['detail']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
