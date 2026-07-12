"""Preflight — the exact-version / exact-hardware gate Visionary refuses to run without.

Why this exists: the Resolve stage drives the REAL DaVinci Resolve UI by screen-capture
template matching + synthetic clicks (engine/dv_shim.py). The templates were cropped from
DaVinci Resolve Studio 18.6.0's Color page on the 16-inch MacBook Pro's built-in display at
native 3456x2234, and every click coordinate assumes that exact geometry. The Topaz stage
invokes Topaz Video AI 7.0.1's bundled ffmpeg with that build's `tvai_up` schema. A different
app version or display doesn't degrade gracefully — it clicks the wrong pixels. So the pins
in engine/versions.py are gated here, hard.

Two surfaces, one implementation:
- CLI (setup/onboarding):  python3 engine/preflight.py [--json] [--network] [--smoke] [--post-setup]
    exit 0 = all pass · 1 = a HARD check failed · 2 = warnings only
- In-app: the dashboard server imports run_cheap() into /api/selftest (12 s poll) and refuses
  to arm the pipeline (POST /api/automation -> 409) while hard_ok is false.

Severity semantics: "fail" checks gate hard_ok (the app refuses to arm); "warn" checks are
setup-progress items (config not filled in yet, grants not yet given) — they block a RUN from
succeeding but not intentionally, so they only fail the strict exit code, not hard_ok.
"""
from __future__ import annotations
import argparse
import json
import os
import plistlib
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import versions  # noqa: E402

RESOLVE_PROJECT_DIR = os.path.expanduser(
    "~/Library/Application Support/Blackmagic Design/DaVinci Resolve/"
    "Resolve Project Library/Resolve Projects/Users/guest/Projects")
DELIVER_PRESETS = os.path.expanduser(
    "~/Library/Application Support/Blackmagic Design/DaVinci Resolve/"
    "Resolve Project Library/Resolve Projects/Settings/DeliverPresetList.xml")
BREW = "/opt/homebrew/bin"
BREW_TOOLS = ("ffmpeg", "ffprobe", "x265", "dovi_tool", "MP4Box", "cliclick")
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dv_shim_templates")
TEMPLATES = ("dolby_vision_palette.png", "analyze_all.png", "analyze_modal.png", "target_1000nit.png")


def _check(cid, ok, severity, detail, fix=""):
    return {"id": cid, "ok": bool(ok), "severity": severity, "detail": detail, "fix": "" if ok else fix}


def _bundle_version(app_path):
    """(short_version, build) from an app bundle's Info.plist, or (None, None)."""
    try:
        with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as f:
            p = plistlib.load(f)
        return p.get("CFBundleShortVersionString"), p.get("CFBundleVersion")
    except (OSError, plistlib.InvalidFileException):
        return None, None


def check_resolve_version():
    short, build = _bundle_version(versions.RESOLVE_APP)
    if short is None:
        return _check("resolve_version", False, "fail",
                      f"DaVinci Resolve not found at {versions.RESOLVE_APP}",
                      f"Install DaVinci Resolve STUDIO {versions.RESOLVE_VERSION} (build "
                      f"{versions.RESOLVE_BUILD}) from Blackmagic's support archive — the free "
                      f"edition and any other version will not work.")
    ok = short == versions.RESOLVE_VERSION
    return _check("resolve_version", ok, "fail",
                  f"found {short} (build {build}); require exactly {versions.RESOLVE_VERSION} "
                  f"(build {versions.RESOLVE_BUILD})",
                  f"Replace DaVinci Resolve {short} with STUDIO {versions.RESOLVE_VERSION} from "
                  f"Blackmagic's support archive. Do NOT let it upgrade the project library for a "
                  f"newer version. The screen automation only matches this exact build's UI.")


def check_topaz_version():
    short, _ = _bundle_version(versions.TOPAZ_APP)
    if short is None:
        return _check("topaz_version", False, "fail",
                      f"Topaz Video AI not found at {versions.TOPAZ_APP}",
                      f"Install Topaz Video AI {versions.TOPAZ_VERSION} from Topaz's release "
                      f"archive, log in once in the app to activate (headless use works after), "
                      f"and disable auto-updates in its preferences.")
    if short != versions.TOPAZ_VERSION:
        return _check("topaz_version", False, "fail",
                      f"found {short}; require exactly {versions.TOPAZ_VERSION}",
                      f"Replace Topaz Video AI {short} with {versions.TOPAZ_VERSION} from Topaz's "
                      f"release archive (updates change the tvai_up model/parameter schema the "
                      f"pipeline is tuned for), and disable auto-updates.")
    ffmpeg = os.path.join(versions.TOPAZ_APP, "Contents", "MacOS", "ffmpeg")
    models = os.path.join(versions.TOPAZ_APP, "Contents", "Resources", "models")
    ok = os.path.exists(ffmpeg) and os.path.isdir(models)
    return _check("topaz_version", ok, "fail",
                  f"{versions.TOPAZ_VERSION} present; bundled ffmpeg={os.path.exists(ffmpeg)} "
                  f"models={os.path.isdir(models)}",
                  "Launch Topaz Video AI once and log in — that activates the license and "
                  "downloads the models the headless pipeline uses.")


def _display_via_coregraphics():
    """(pixel_w, pixel_h, scale, is_builtin) of the MAIN display, via CoreGraphics.
    argtypes are declared explicitly — without them ctypes passes the 64-bit
    CGDisplayModeRef as a truncated 32-bit int and the process SEGFAULTS."""
    import ctypes, ctypes.util
    cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
    cg.CGMainDisplayID.restype = ctypes.c_uint32
    main_id = cg.CGMainDisplayID()
    cg.CGDisplayIsBuiltin.restype = ctypes.c_bool
    cg.CGDisplayIsBuiltin.argtypes = [ctypes.c_uint32]
    builtin = bool(cg.CGDisplayIsBuiltin(main_id))
    cg.CGDisplayCopyDisplayMode.restype = ctypes.c_void_p
    cg.CGDisplayCopyDisplayMode.argtypes = [ctypes.c_uint32]
    mode = cg.CGDisplayCopyDisplayMode(main_id)
    if not mode:
        raise RuntimeError("CGDisplayCopyDisplayMode returned NULL")
    try:
        cg.CGDisplayModeGetPixelWidth.restype = ctypes.c_size_t
        cg.CGDisplayModeGetPixelWidth.argtypes = [ctypes.c_void_p]
        cg.CGDisplayModeGetPixelHeight.restype = ctypes.c_size_t
        cg.CGDisplayModeGetPixelHeight.argtypes = [ctypes.c_void_p]
        px_w = int(cg.CGDisplayModeGetPixelWidth(mode))
        px_h = int(cg.CGDisplayModeGetPixelHeight(mode))
    finally:
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFRelease(mode)
    cg.CGDisplayPixelsWide.restype = ctypes.c_size_t
    cg.CGDisplayPixelsWide.argtypes = [ctypes.c_uint32]
    pt_w = int(cg.CGDisplayPixelsWide(main_id))
    scale = (px_w / pt_w) if pt_w else 0.0
    return px_w, px_h, scale, builtin


def _display_via_system_profiler():
    out = subprocess.run(["system_profiler", "SPDisplaysDataType", "-json"],
                         capture_output=True, text=True, timeout=30).stdout
    for gpu in json.loads(out).get("SPDisplaysDataType", []):
        for disp in gpu.get("spdisplays_ndrvs", []):
            if disp.get("spdisplays_main") != "spdisplays_yes":
                continue
            builtin = "internal" in (disp.get("spdisplays_connection_type") or "").lower() \
                      or "built-in" in (disp.get("_name") or "").lower()
            import re
            m = re.search(r"(\d{3,5})\s*x\s*(\d{3,5})", disp.get("_spdisplays_pixels") or "")
            if m:
                return int(m.group(1)), int(m.group(2)), None, builtin
    raise RuntimeError("main display not found in system_profiler output")


def check_display():
    want_w, want_h = versions.DISPLAY_PIXELS
    fix = (f"Visionary only works on the 16-inch MacBook Pro BUILT-IN display "
           f"({want_w}x{want_h} native) used as the MAIN display. Unplug/stop mirroring to an "
           f"external main display; if this Mac isn't a 16\" MacBook Pro, it cannot run Visionary.")
    try:
        px_w, px_h, scale, builtin = _display_via_coregraphics()
        via = "CoreGraphics"
    except Exception:
        try:
            px_w, px_h, scale, builtin = _display_via_system_profiler()
            via = "system_profiler"
        except Exception as e:
            return _check("display", False, "fail", f"could not read display geometry: {e}", fix)
    ok = (px_w, px_h) == (want_w, want_h) and builtin \
         and (scale is None or abs(scale - versions.RETINA_SCALE) < 0.01)
    return _check("display", ok, "fail",
                  f"main display {px_w}x{px_h} builtin={builtin}"
                  + (f" scale={scale:g}" if scale is not None else "") + f" (via {via}); "
                  f"require builtin {want_w}x{want_h} @ {versions.RETINA_SCALE:g}x", fix)


def check_power_adapter():
    """The Topaz stage runs the GPU flat-out for hours and needs the 16-inch MBP's full
    140 W adapter — the 14-inch MBP's 96 W maximum can never satisfy the run-time power
    gate (the pipeline would hold forever / drain the battery mid-encode). WARN severity:
    being unplugged during setup is fine; the run-time gate enforces this when it matters."""
    import power
    w = power.adapter_watts()
    need = versions.REQUIRED_ADAPTER_WATTS
    fix = (f"Plug in the {need} W adapter (the 16-inch MacBook Pro's brick). Smaller bricks "
           f"— including the 14-inch MBP's 96 W maximum — cannot power the Topaz stage; the "
           f"pipeline will refuse to run on them.")
    if w is None:
        return _check("power_adapter", True, "warn",
                      f"no adapter connected right now — the pipeline requires >= {need} W "
                      f"to run (checked live by its power gate)", "")
    return _check("power_adapter", w >= need, "warn",
                  f"connected adapter: {w} W (require >= {need} W to run)", fix)


def check_brew_tools():
    missing = [t for t in BREW_TOOLS if not os.path.exists(os.path.join(BREW, t))]
    return _check("brew_tools", not missing, "fail",
                  ("all present: " + ", ".join(BREW_TOOLS)) if not missing
                  else "missing from /opt/homebrew/bin: " + ", ".join(missing),
                  "brew install ffmpeg x265 dovi_tool gpac cliclick")


def check_sublercli():
    if not os.path.exists(os.path.join(BREW, "SublerCLI")):
        return _check("sublercli", False, "fail", "SublerCLI missing from /opt/homebrew/bin",
                      "brew install --cask sublercli")
    try:
        rosetta = subprocess.run(["arch", "-arch", "x86_64", "/usr/bin/true"],
                                 capture_output=True, timeout=10).returncode == 0
    except Exception:
        return _check("sublercli", True, "warn",
                      "SublerCLI present; could not determine Rosetta status",
                      "softwareupdate --install-rosetta --agree-to-license")
    return _check("sublercli", rosetta, "warn",
                  f"SublerCLI present; Rosetta {'present' if rosetta else 'MISSING (SublerCLI is x86_64)'}",
                  "softwareupdate --install-rosetta --agree-to-license")


ENGINE_PYTHON = "/usr/bin/python3"   # what the app launches the engine with (macapp/main.swift)


def check_python_deps():
    """Probe the ENGINE's interpreter (/usr/bin/python3), not whatever runs this CLI —
    the app launches the engine with the system python, and Resolve's fusionscript
    breaks on Python >=3.12 ('imp' removed). A conda/homebrew shell python is irrelevant."""
    try:
        r = subprocess.run(
            [ENGINE_PYTHON, "-c",
             "import sys, cv2; print('%d.%d' % sys.version_info[:2], cv2.__version__)"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return _check("python_deps", False, "fail", f"{ENGINE_PYTHON} probe failed: {e}",
                      "Install the Xcode Command Line Tools: xcode-select --install")
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-1:] or ["import failed"]
        return _check("python_deps", False, "fail",
                      f"{ENGINE_PYTHON}: {tail[0]}",
                      f"{ENGINE_PYTHON} -m pip install --user opencv-python  "
                      f"(if pip is missing: {ENGINE_PYTHON} -m ensurepip --user first)")
    ver, cv2_ver = r.stdout.split()
    py_ok = tuple(int(x) for x in ver.split(".")) < (3, 12)
    detail = f"{ENGINE_PYTHON} is {ver} with cv2 {cv2_ver}"
    if not py_ok:
        detail += " — Python >=3.12 breaks Resolve's fusionscript ('imp' removed)"
    return _check("python_deps", py_ok, "fail", detail,
                  "The system python at /usr/bin/python3 must be < 3.12 for Resolve's "
                  "scripting API — this macOS version ships one that is too new.")


def check_shim_templates():
    missing = [t for t in TEMPLATES if not os.path.exists(os.path.join(TEMPLATES_DIR, t))]
    if missing:
        return _check("shim_templates", False, "fail",
                      "missing template PNGs: " + ", ".join(missing),
                      "Re-clone the repo — engine/dv_shim_templates/*.png must ship with it.")
    try:
        import cv2
        bad = [t for t in TEMPLATES if cv2.imread(os.path.join(TEMPLATES_DIR, t)) is None]
        return _check("shim_templates", not bad, "fail",
                      "all 4 templates load" if not bad else "unreadable PNGs: " + ", ".join(bad),
                      "Re-clone the repo — a template PNG is corrupt.")
    except Exception:
        return _check("shim_templates", True, "warn",
                      "4 templates present (cv2 unavailable — could not validate contents)",
                      "")


def selftest_grants():
    """Verify THIS process's macOS grants for the resolve stage: Screen Recording
    (screencapture) + Accessibility (cliclick). TCC is per-app: run from the app it tests
    the app's context; run from a terminal it tests the terminal's. (Moved verbatim from
    dashboard/server.py so the CLI and the app share one implementation.)"""
    import tempfile
    r = {"screen_recording": False, "accessibility": False, "cliclick_installed": True, "detail": {}}
    try:
        import ctypes, ctypes.util
        cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        r["screen_recording"] = bool(cg.CGPreflightScreenCaptureAccess())
        r["detail"]["screen_recording_via"] = "CGPreflightScreenCaptureAccess"
    except Exception as e:                           # ancient macOS / missing symbol → old probe
        r["detail"]["preflight_err"] = str(e)
        try:
            png = os.path.join(tempfile.gettempdir(), "_grant_selftest.png")
            subprocess.run(["screencapture", "-x", png], timeout=12, check=False)
            sz = os.path.getsize(png) if os.path.exists(png) else 0
            r["screen_recording"] = sz > 10000
            r["detail"]["screencapture_bytes"] = sz
        except Exception as e2:
            r["detail"]["screencapture_err"] = str(e2)
    try:
        cc = subprocess.run(["cliclick", "p:."], capture_output=True, text=True, timeout=12)
        warned = "Accessibility privileges not enabled" in (cc.stderr or "")
        r["accessibility"] = not warned
        r["detail"]["cliclick_warned"] = warned
    except FileNotFoundError:
        r["cliclick_installed"] = False
        r["detail"]["cliclick_err"] = "cliclick not installed"
    except Exception as e:
        r["detail"]["cliclick_err"] = str(e)
    r["ok"] = r["screen_recording"] and r["accessibility"]
    return r


def check_tcc_grants(in_app=False):
    g = selftest_grants()
    return _check("tcc_grants", g["ok"], "fail" if in_app else "warn",
                  f"screen_recording={g['screen_recording']} accessibility={g['accessibility']} "
                  + ("(this is the APP's TCC context)" if in_app else
                     "(CLI runs in the TERMINAL's TCC context — the app's own selftest at "
                     "http://127.0.0.1:8765/api/selftest is authoritative)"),
                  "Launch Visionary once, then System Settings > Privacy & Security: enable "
                  "Visionary under Screen Recording AND Accessibility (the in-app card has a "
                  "'Request Accessibility' button), then relaunch it.")


def check_resolve_artifacts(post_setup=False):
    sev = "fail" if post_setup else "warn"
    fix = "Quit Resolve, then run: python3 setup/import_resolve.py  (imports the two Visionary " \
          "projects and the OvernightDV Dolby Vision render preset shipped in bundle/resolve/)."
    preset_ok = False
    try:
        with open(DELIVER_PRESETS, encoding="utf-8", errors="replace") as f:
            preset_ok = "<DbKey>OvernightDV</DbKey>" in f.read()
    except OSError:
        pass
    try:
        projects = set(os.listdir(RESOLVE_PROJECT_DIR))
    except OSError:
        projects = set()
    sdr = {"Visionary SDR", "Overnight Upscaler SDR", "Overnight Upscaler"} & projects
    hdr = {"Visionary HDR", "Overnight Upscaler HDR"} & projects
    ok = preset_ok and bool(sdr) and bool(hdr)
    return _check("resolve_artifacts", ok, sev,
                  f"OvernightDV preset={'present' if preset_ok else 'MISSING'}; "
                  f"SDR project={sorted(sdr) or 'MISSING'}; HDR project={sorted(hdr) or 'MISSING'}",
                  fix)


def check_config(network=False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import transfer, plex
    cfg_path = os.path.expanduser("~/.topaz-pipeline/config.json")
    hosts = transfer.nas_hosts()
    s = transfer.ftp_settings()
    token = plex.plex_token()
    # plex_token is NOT required — Plex is an optional extra (README 'Configuration'): the
    # token is the optionality signal, so a blank one must neither fail this check nor block
    # the FTP probe below (it used to do both, so a Plex-less setup could never go all-green).
    missing = [k for k, v in (("ftp_host(s)", hosts), ("ftp_user", s["user"]),
                              ("ftp_pass", s["passwd"])) if not v]
    if missing:
        return _check("config", False, "warn",
                      f"{cfg_path}: missing/empty -> " + ", ".join(missing),
                      "cp config.example.json ~/.topaz-pipeline/config.json && chmod 600 "
                      "~/.topaz-pipeline/config.json — then fill in the NAS values (Plex is "
                      "optional; see README 'Configuration').")
    if not network:
        note = "all required keys set" + ("" if token else " · Plex not configured (optional)")
        return _check("config", True, "warn", note + " (run --network to probe live)", "")
    # live probes: FTP always; Plex/Youtarr only when configured — both optional features
    detail, ok = [], True
    try:
        ftp = transfer.connect(timeout=10)
        try: ftp.quit()
        except Exception: pass
        detail.append("FTP: connected")
    except Exception as e:
        ok = False
        detail.append(f"FTP: {e}")
    if token:
        try:
            import urllib.request
            base = plex.plex_base_urls()[0]
            req = urllib.request.Request(base + "/identity", headers={"X-Plex-Token": token})
            with urllib.request.urlopen(req, timeout=10):
                pass
            detail.append("Plex: reachable")
        except Exception as e:
            ok = False
            detail.append(f"Plex: {e}")
    else:
        detail.append("Plex: not configured (optional)")
    return _check("config", ok, "warn", "; ".join(detail),
                  "Check the NAS is reachable (VPN up? LAN name resolves?) and — if you use "
                  "Plex — the URL/token in ~/.topaz-pipeline/config.json are right.")


def check_shim_smoke():
    """OPTIONAL (--smoke): with Resolve running, prove the full chain — screencapture works
    AND the 18.6 Color-page template actually matches this screen."""
    if subprocess.run(["pgrep", "-x", "DaVinci Resolve"], capture_output=True).returncode != 0:
        return _check("shim_smoke", True, "warn", "skipped — DaVinci Resolve is not running", "")
    try:
        import tempfile, cv2
        png = os.path.join(tempfile.gettempdir(), "_preflight_smoke.png")
        subprocess.run(["screencapture", "-x", png], timeout=15, check=True)
        shot = cv2.imread(png)
        tpl = cv2.imread(os.path.join(TEMPLATES_DIR, "dolby_vision_palette.png"))
        if shot is None or tpl is None:
            return _check("shim_smoke", False, "warn", "could not load screenshot/template",
                          "Grant Screen Recording to this process and retry.")
        score = float(cv2.minMaxLoc(cv2.matchTemplate(shot, tpl, cv2.TM_CCOEFF_NORMED))[1])
        ok = score >= 0.8
        return _check("shim_smoke", ok, "warn",
                      f"template match score {score:.3f} (need >=0.8; Resolve must be on the "
                      f"Color page, full screen)",
                      "Open Resolve full-screen on the Color page with the Dolby Vision palette "
                      "visible and re-run --smoke. A persistent low score means the UI doesn't "
                      "match the pinned Resolve build / display.")
    except Exception as e:
        return _check("shim_smoke", False, "warn", f"smoke test error: {e}",
                      "Grant Screen Recording to this process (terminal) and retry.")


def run_cheap():
    """The sub-millisecond checks safe for the app's 12 s selftest poll."""
    return [check_resolve_version(), check_topaz_version(), check_display()]


def run_checks(network=False, smoke=False, post_setup=False, in_app=False):
    checks = run_cheap() + [
        check_power_adapter(), check_brew_tools(), check_sublercli(), check_python_deps(),
        check_shim_templates(), check_tcc_grants(in_app=in_app),
        check_resolve_artifacts(post_setup=post_setup), check_config(network=network),
    ]
    if smoke:
        checks.append(check_shim_smoke())
    hard_ok = all(c["ok"] for c in checks if c["severity"] == "fail")
    return {"ok": all(c["ok"] for c in checks), "hard_ok": hard_ok, "checks": checks}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Visionary preflight — exact-version/hardware gate")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--network", action="store_true", help="probe NAS FTP + Plex live")
    ap.add_argument("--smoke", action="store_true", help="template-match against a live Resolve")
    ap.add_argument("--post-setup", action="store_true",
                    help="Resolve artifacts become a HARD requirement (run after import_resolve)")
    args = ap.parse_args(argv)
    result = run_checks(network=args.network, smoke=args.smoke, post_setup=args.post_setup)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for c in result["checks"]:
            mark = "PASS" if c["ok"] else ("FAIL" if c["severity"] == "fail" else "WARN")
            print(f"[{mark:4}] {c['id']}: {c['detail']}")
            if c["fix"]:
                print(f"       fix: {c['fix']}")
        print(f"\nhard_ok={result['hard_ok']} ok={result['ok']}")
    return 0 if result["ok"] else (1 if not result["hard_ok"] else 2)


if __name__ == "__main__":
    sys.exit(main())
