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
import time

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
    # `fix` is kept even when the check PASSES (it used to be blanked): the in-app Setup
    # section shows each row's remediation/command regardless of current state, and the
    # CLI printer guards on `ok` itself.
    return {"id": cid, "ok": bool(ok), "severity": severity, "detail": detail, "fix": fix}


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


def match_display(px_w, px_h, scale, builtin):
    """PURE (unit-tested). A descriptor for this main display if it can run the Resolve
    automation, else None.

    The rule is the BACKING SCALE, not the geometry: dv_shim derives every click from a
    template match, so a template lands whenever the UI renders at the same pixel size.
    A named SUPPORTED_DISPLAYS entry is returned when the geometry is one we've actually
    smoke-tested (nicer message); anything else at 2x passes with a generic name.

    `scale is None` means the system_profiler fallback couldn't read it — the invariant is
    then UNVERIFIABLE, so only a known-good geometry is accepted (unchanged behaviour)."""
    for cfg in versions.SUPPORTED_DISPLAYS:      # verified config → name it
        if ((px_w, px_h) == tuple(cfg["backing"]) and bool(builtin) == bool(cfg["builtin"])
                and (scale is None or abs(scale - cfg["scale"]) < 0.01)):
            return cfg
    if scale is None:
        return None                              # can't confirm the invariant → refuse
    if abs(scale - versions.REQUIRED_BACKING_SCALE) >= 0.01:
        return None
    min_w, min_h = versions.MIN_LOGICAL_SIZE     # sanity: Resolve's UI must actually fit
    if (px_w / scale) < min_w or (px_h / scale) < min_h:
        return None
    kind = "built-in Retina display" if builtin else (
        "4K display" if (px_w, px_h) == (3840, 2160) else "external display")
    return {"name": f"{kind} @{versions.REQUIRED_BACKING_SCALE:g}x ({px_w}x{px_h})",
            "backing": (px_w, px_h), "scale": scale, "builtin": bool(builtin),
            "verified": False}


_UNSET = object()


def check_display(host=_UNSET):
    """`host` is injectable so a unit test can ask about a specific display without the
    answer depending on what this machine happens to have pinned right now. Left alone it
    resolves the pinned host itself."""
    verified = " or ".join(f"{c['name']} ({c['backing'][0]}x{c['backing'][1]})"
                           for c in versions.SUPPORTED_DISPLAYS)
    fix = (f"Visionary's Resolve automation needs the MAIN display to render at "
           f"{versions.REQUIRED_BACKING_SCALE:g}x backing scale (Retina/HiDPI) — its screen "
           f"templates only match at that pixel size; the display's SIZE doesn't matter. A "
           f"built-in Retina panel, a 4K display in its default HiDPI mode, or a 4K dummy "
           f"HDMI plug (for lid-closed running) all qualify. If your 4K display is set to a "
           f"1x/'More Space'-style mode, switch it to the default (scaled) mode. "
           f"Smoke-tested so far: {verified}.")
    try:
        px_w, px_h, scale, builtin = _display_via_coregraphics()
        via = "CoreGraphics"
    except Exception:
        try:
            px_w, px_h, scale, builtin = _display_via_system_profiler()
            via = "system_profiler"
        except Exception as e:
            return _check("display", False, "fail", f"could not read display geometry: {e}", fix)
    cfg = match_display(px_w, px_h, scale, builtin)
    which = "main display"
    # Judge the display that will ACTUALLY be driven. With Resolve pinned to another
    # screen, main's verdict is the wrong question in BOTH directions: a 1x main would
    # refuse to arm a rig whose host is perfectly good, and a 2x main would happily arm
    # one whose host is not. Unpinned, or when the pinned display is not attached, this
    # is exactly the old check on main.
    if host is _UNSET:
        try:
            host, _why = chosen_host()
        except Exception:
            host = None
    if host:
        px_w, px_h = host["backing"]
        scale, builtin = host["scale"], host["builtin"]
        cfg = match_display(px_w, px_h, scale, builtin)
        which = "host display %s" % host.get("name", host.get("key"))
        via += " (pinned)"
    note = ""
    if cfg:
        note = f"matched: {cfg['name']}"
        if not cfg.get("verified", True):        # admitted by the scale rule, not smoke-tested
            note += " — run `preflight.py --smoke` once to confirm the templates match here"
    else:
        note = f"require {versions.REQUIRED_BACKING_SCALE:g}x backing scale (verified: {verified})"
    return _check("display", cfg is not None, "fail",
                  f"{which} {px_w}x{px_h} builtin={builtin}"
                  + (f" scale={scale:g}" if scale is not None else "") + f" (via {via}); "
                  + note, fix)


def check_power_adapter():
    """The Topaz stage runs the GPU flat-out for hours, so the machine must sustain
    ~140 W. Three cases:

    * NO BATTERY (Mac mini/Studio/iMac) → mains-powered by definition: PASS, no wattage
      rule. (Without this a desktop is blocked outright — it has no AppleSmartBattery, so
      the run gate reads "on battery" and pauses forever.)
    * A LAPTOP on a KNOWN model → its documented ceiling decides, because a 140 W brick
      plugged into a 96 W-max machine still REPORTS 140 W; wattage alone can't tell you
      what the machine draws. Known-under-140 W is a HARD FAIL.
    * A LAPTOP we don't recognise (newer than these lists) → don't block it: fall back to
      the LIVE adapter reading and name the model so versions.MODELS_140W can be extended.

    WARN severity for the wattage part — being unplugged during setup is fine; the
    run-time gate enforces it when it actually matters."""
    import power
    need = versions.REQUIRED_ADAPTER_WATTS
    kind, model = ("laptop" if power.has_battery() else "desktop"), power.model_id()
    if kind == "desktop":
        return _check("power_adapter", True, "warn",
                      f"{model or 'desktop Mac'}: mains-powered (no battery) — the "
                      f"{need} W rule doesn't apply", "")
    if model in versions.MODELS_BELOW_140W:
        return _check("power_adapter", False, "fail",
                      f"{model} tops out below {need} W — it cannot power the Topaz stage",
                      f"Visionary needs a Mac that sustains {need} W: a 140 W-class MacBook "
                      f"Pro (16-inch Apple Silicon) on its own brick, or any desktop Mac.")
    known = model in versions.MODELS_140W
    w = power.adapter_watts()
    fix = (f"Plug in the {need} W adapter. Smaller bricks cannot power the Topaz stage; the "
           f"pipeline will refuse to run on them.")
    if known and w is None:
        return _check("power_adapter", True, "warn",
                      f"{model} is a {need} W-class Mac; no adapter connected right now "
                      f"(the run-time gate checks this live)", "")
    if known:
        return _check("power_adapter", w >= need, "warn",
                      f"{model} ({need} W-class); connected adapter: {w} W", fix)
    # unknown laptop — the live reading is all we have
    if w is None:
        return _check("power_adapter", True, "warn",
                      f"{model or 'this Mac'} isn't in the known {need} W list; connect its "
                      f"adapter — the run-time gate requires >= {need} W", "")
    return _check("power_adapter", w >= need, "warn",
                  f"{model or 'this Mac'} isn't in the known {need} W list; connected "
                  f"adapter reads {w} W (require >= {need} W)", fix)


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
                      "Reinstall the app (or re-clone the repo) — engine/dv_shim_templates/*.png must ship with it.")
    try:
        import cv2
        bad = [t for t in TEMPLATES if cv2.imread(os.path.join(TEMPLATES_DIR, t)) is None]
        return _check("shim_templates", not bad, "fail",
                      "all 4 templates load" if not bad else "unreadable PNGs: " + ", ".join(bad),
                      "Reinstall the app (or re-clone the repo) — a template PNG is corrupt.")
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
    fix = "Quit Resolve, then use Settings → Setup → 'Import projects & preset' in the app " \
          "(or run: python3 setup/import_resolve.py from a clone). Imports the Visionary " \
          "projects and the OvernightDV Dolby Vision render preset shipped in bundle/resolve/."
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
    # SAME tuples import_resolve imports from (versions.py) — these drifted once: a clean
    # import of "Visionary DV1000 Output" then failed this check, which only knew the
    # legacy names.
    dv1000 = set(versions.RESOLVE_PROJECTS_DV1000) & projects
    dv2000 = set(versions.RESOLVE_PROJECTS_DV2000) & projects
    ok = preset_ok and bool(dv1000) and bool(dv2000)
    return _check("resolve_artifacts", ok, sev,
                  f"OvernightDV preset={'present' if preset_ok else 'MISSING'}; "
                  f"DV1000 project={sorted(dv1000) or 'MISSING'}; "
                  f"DV2000 project={sorted(dv2000) or 'MISSING'}",
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


# "DaVinci Resolve" is the APP; the executable is "Resolve", so `pgrep -x "DaVinci Resolve"`
# never matches and every guard built on it silently reported "not running". Match the full
# bundle path instead — unambiguous, and the same thing stages.py's pkill -f already uses.
RESOLVE_PGREP = ["pgrep", "-f", "DaVinci Resolve.app/Contents/MacOS/Resolve"]

SMOKE_FILE = os.path.expanduser("~/.topaz-pipeline/display_smoke.json")
SMOKE_PASS = 0.9      # a hosting display must clear this on at least one template


def shim_smoke_scores(display_key=None):
    """Match EVERY dv_shim template against a live screen -> {template: score} plus
    context. The acceptance gate for calling a display configuration supported: a
    template only matches when the UI renders at the same backing-pixel size, so this is
    what proves a new display (e.g. the clamshell dummy) is really usable. Also the
    remote debugging entry point (GET /api/shim-smoke) when nobody can see the screen.

    `display_key` scores a NON-main display. That is the point of the whole exercise:
    before any click is ever sent to a screen, prove the templates match on it."""
    import tempfile, cv2, dv_shim, displays as _dsp
    target = _dsp.find(display_key) if display_key else None
    out = {"scores": {}, "resolve_running": False,
           "display": dv_shim.main_display_geometry(),
           "display_key": (target or {}).get("key") if target else None,
           "display_name": None,
           "screen_locked": dv_shim.screen_locked(), "error": None}
    out["resolve_running"] = subprocess.run(RESOLVE_PGREP,
                                            capture_output=True).returncode == 0
    if display_key and not target:
        out["error"] = "display not attached: %s" % display_key
        return out
    if target:
        d = match_display(target["backing"][0], target["backing"][1],
                          target["scale"], target["builtin"])
        out["display_name"] = (d or {}).get("name")
    prev = dv_shim.get_host()
    try:
        dv_shim.set_host(target)
        png = os.path.join(tempfile.gettempdir(), "_preflight_smoke.png")
        # Through dv_shim.screenshot, not a bare screencapture: that is the ONLY capture
        # path that targets a display and that retries through a display-set transition.
        dv_shim.screenshot(png)
        for name in sorted(os.listdir(TEMPLATES_DIR)):
            if not name.endswith(".png") or name.startswith("_"):
                continue
            _pos, score = dv_shim.match_template(png, os.path.join(TEMPLATES_DIR, name))
            out["scores"][name] = round(score, 4)
    except Exception as e:
        out["error"] = f"{e.__class__.__name__}: {e}"
    finally:
        dv_shim.set_host(prev)
    out["best"] = max(out["scores"].values()) if out["scores"] else 0.0
    out["pass"] = bool(out["best"] >= SMOKE_PASS and not out["error"])
    return out


def load_display_smoke() -> dict:
    """{display_key: {best, pass, when, scores}} — which screens have been PROVEN to
    render Resolve the way the templates expect. A display cannot be chosen to host
    Resolve without a record here: driving a screen nobody is looking at raises the cost
    of a bad match from a loud failure to silent wrong clicks."""
    try:
        with open(SMOKE_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def record_display_smoke(display_key, result) -> dict:
    """Persist one display's smoke result. Returns the whole book."""
    book = load_display_smoke()
    if display_key:
        book[display_key] = {"best": result.get("best", 0.0),
                             "pass": bool(result.get("pass")),
                             "scores": result.get("scores", {}),
                             "when": time.strftime("%Y-%m-%dT%H:%M:%S")}
        try:
            os.makedirs(os.path.dirname(SMOKE_FILE), exist_ok=True)
            tmp = SMOKE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(book, f, indent=1)
            os.replace(tmp, SMOKE_FILE)
        except Exception:
            pass
    return book


def eligible_displays() -> list:
    """Every attached display, annotated with whether it could host Resolve. The
    eligibility RULE is match_display, unchanged — this only applies it to each screen
    instead of only to the main one, and adds the two host-specific exclusions."""
    import displays as _dsp
    book = load_display_smoke()
    out = []
    for d in _dsp.enumerate_displays():
        desc = match_display(d["backing"][0], d["backing"][1], d["scale"], d["builtin"])
        smoke = book.get(d["key"]) or {}
        why = None
        if d["mirror_slave"]:
            why = "mirrors another display — it has no framebuffer of its own"
        elif not desc:
            why = ("renders at %.2gx — the shim's templates only match at %gx"
                   % (d["scale"], versions.REQUIRED_BACKING_SCALE))
        out.append({**d,
                    "name": (desc or {}).get("name") or
                            ("%dx%d @%.2gx" % (d["backing"][0], d["backing"][1], d["scale"])),
                    "eligible": bool(desc) and not d["mirror_slave"],
                    "verified": bool(desc) and bool(desc.get("backing")),
                    "why_not": why,
                    "smoke_pass": bool(smoke.get("pass")),
                    "smoke_best": smoke.get("best"),
                    "smoke_when": smoke.get("when")})
    return out


def chosen_host():
    """(descriptor | None, reason). None means "drive the main display", which is what
    every code path did before this feature and what it still does unless ALL of these
    hold: pinning is on, the display is in the priority list, it is attached, it is
    eligible under match_display, and it has a recorded template-smoke pass.

    That last gate is deliberate and is not just belt-and-braces: driving a screen nobody
    is looking at turns a bad template match from a loud failure into silent wrong clicks,
    so a display has to be PROVEN before it is trusted."""
    try:
        import settings
        if not settings.get_settings().get("resolve_host_pinning"):
            return None, "pinning off"
        prio = settings.get_display_priority()
        if not prio:
            return None, "no display chosen"
        by_key = {d["key"]: d for d in eligible_displays()}
        missing = []
        for key in prio:
            d = by_key.get(key)
            if d is None:
                missing.append("not attached")
                continue
            if d["main"]:
                return None, "chosen display is the main one"
            if not d["eligible"]:
                missing.append(d.get("why_not") or "not eligible")
                continue
            if not d["smoke_pass"]:
                missing.append("templates not proven on it yet")
                continue
            return d, "pinned"
        return None, "; ".join(missing) or "no usable display"
    except Exception as e:
        return None, "%s: %s" % (e.__class__.__name__, e)


def check_shim_smoke():
    """OPTIONAL (--smoke): with Resolve running, prove the full chain — screencapture works
    AND EVERY dv_shim template matches this screen (not just the palette). The per-template
    scores are what diagnose a display/Resolve-build mismatch."""
    if subprocess.run(RESOLVE_PGREP, capture_output=True).returncode != 0:
        return _check("shim_smoke", True, "warn", "skipped — DaVinci Resolve is not running", "")
    fixit = ("Open Resolve full-screen on the Color page with the Dolby Vision palette "
             "visible and re-run --smoke. A persistently low score for a template means "
             "the UI doesn't match the pinned Resolve build / this display config — "
             "re-capture that template on THIS display (see dv_shim_templates/README.md).")
    r = shim_smoke_scores()
    if r["error"] or not r["scores"]:
        return _check("shim_smoke", False, "warn",
                      f"smoke test error: {r['error'] or 'no templates matched'}",
                      "Grant Screen Recording to this process (terminal) and retry.")
    # analyze_modal only exists WHILE analyzing, so it is reported but never gates.
    gated = {k: v for k, v in r["scores"].items() if k != "analyze_modal.png"}
    worst = min(gated.values()) if gated else 0.0
    detail = " ".join(f"{k.replace('.png','')}={v:.3f}" for k, v in sorted(r["scores"].items()))
    return _check("shim_smoke", worst >= 0.8, "warn",
                  f"template scores: {detail} (need >=0.8 each, analyze_modal excluded — "
                  f"it only exists during an analysis)", fixit)


def run_cheap():
    """The sub-millisecond checks safe for the app's 12 s selftest poll."""
    return [check_resolve_version(), check_topaz_version(), check_display()]


def run_arm_gate():
    """What Activation requires: the version/display pins PLUS the instant dependency
    checks. STRENGTHENS the gate (CLAUDE.md allows that; weakening is forbidden) — a
    missing x265 or a broken cv2 used to surface as a stage failure HOURS into a job;
    now it refuses to arm with a named check. Not run_cheap(): check_python_deps spawns
    /usr/bin/python3 (~0.3 s), too heavy for the selftest poll, fine at arm time and in
    the 60 s re-arm tick."""
    return run_cheap() + [check_brew_tools(), check_python_deps()]


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
            if not c["ok"] and c["fix"]:       # fix is now always populated — print on failure only
                print(f"       fix: {c['fix']}")
        print(f"\nhard_ok={result['hard_ok']} ok={result['ok']}")
    return 0 if result["ok"] else (1 if not result["hard_ok"] else 2)


if __name__ == "__main__":
    sys.exit(main())
