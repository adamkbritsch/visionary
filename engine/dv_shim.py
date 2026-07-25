"""DV UI shim — drives DaVinci Resolve's Color page to run "Analyze All Shots",
the one Dolby Vision step that has NO scripting API.

WHAT THIS DOES (and, deliberately, what it does NOT):
  The persistent app project is configured ONCE, by hand — HDR PQ color, the
  Dolby Vision Profile 8.1 export flag, and the 1000-nit ST.2084 Target Display.
  The pipeline INHERITS all of that and never changes it. So per episode there is
  exactly ONE UI-only action with no API: **Analyze All Shots**. This shim opens
  the Dolby Vision palette and clicks Analyze "All", then waits for it to finish.

  It does NOT touch hardware acceleration (USER: never uncheck it — the DV Profile
  8.1 dropdown only shows with HW off, but the value is already saved on the
  project, so we render with HW ON and inherit it). It does NOT set the Target
  Display — it only VERIFIES the project still has the 1000-nit one (guarding the
  100-nit-SDR-default regression), and refuses to analyze against the wrong target.

WHY FULL-SCREEN FIRST: palette/button positions depend on window size, so the
shim forces a deterministic full-screen layout, then locates everything by
TEMPLATE MATCHING (cv2) — never fixed coordinates.

DEPLOYMENT (one-time, System Settings > Privacy & Security): the process that
RUNS this shim (the orchestrator's python) needs **Screen Recording** (for
screencapture) and **Accessibility** (for cliclick clicks + AXFullScreen). Claude's
computer-use grant does NOT transfer to a standalone process. Also `brew install
cliclick`. Templates in dv_shim_templates/ are captured at screencapture (Retina)
resolution on the target machine — see capture_reference().
"""
from __future__ import annotations
import os
import shutil
import subprocess
import time

try:
    import cv2
except ImportError:  # keep the module importable where cv2 isn't installed
    cv2 = None

TEMPLATES = os.path.join(os.path.dirname(__file__), "dv_shim_templates")

# Absolute cliclick path. The GUI app launches the server with a minimal PATH
# (/usr/bin:/bin:/usr/sbin:/sbin — no /opt/homebrew/bin), so a bare "cliclick" raises
# FileNotFoundError mid-Resolve (the exact bug that broke the DV-analyze clicks).
CLICLICK = shutil.which("cliclick",
                        path=(os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin")) or "/opt/homebrew/bin/cliclick"
APP = "DaVinci Resolve"

# Forensics for LID-CLOSED (clamshell) runs: nobody can see the screen, so every shim
# failure drops the full screenshot + a sidecar JSON here (ring-buffered) — see _diag().
DIAG_DIR = os.path.expanduser("~/.topaz-pipeline/diag")
DIAG_KEEP = 20


def main_display_geometry():
    """(backing_w, backing_h, scale, is_builtin) of the MAIN display, or None. ctypes
    argtypes are REQUIRED — without them CoreGraphics segfaults (CGDisplayModeRef is
    truncated to 32 bits). Mirrors preflight._display_via_coregraphics."""
    try:
        import ctypes, ctypes.util
        cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
        u32, sz, vp = ctypes.c_uint32, ctypes.c_size_t, ctypes.c_void_p
        cg.CGMainDisplayID.argtypes = []; cg.CGMainDisplayID.restype = u32
        cg.CGDisplayPixelsWide.argtypes = [u32]; cg.CGDisplayPixelsWide.restype = sz
        cg.CGDisplayIsBuiltin.argtypes = [u32]; cg.CGDisplayIsBuiltin.restype = u32
        cg.CGDisplayCopyDisplayMode.argtypes = [u32]; cg.CGDisplayCopyDisplayMode.restype = vp
        cg.CGDisplayModeGetPixelWidth.argtypes = [vp]; cg.CGDisplayModeGetPixelWidth.restype = sz
        cg.CGDisplayModeGetPixelHeight.argtypes = [vp]; cg.CGDisplayModeGetPixelHeight.restype = sz
        cg.CGDisplayModeRelease.argtypes = [vp]; cg.CGDisplayModeRelease.restype = None
        d = cg.CGMainDisplayID()
        mode = cg.CGDisplayCopyDisplayMode(d)
        bw, bh = cg.CGDisplayModeGetPixelWidth(mode), cg.CGDisplayModeGetPixelHeight(mode)
        cg.CGDisplayModeRelease(mode)
        lw = cg.CGDisplayPixelsWide(d)
        return (bw, bh, (bw / lw) if lw else None, bool(cg.CGDisplayIsBuiltin(d)))
    except Exception:
        return None


def screen_locked():
    """True if the login window/lock screen is up — then EVERY template match fails and
    the failure looks inexplicable. caffeinate -d blocks the screensaver but NOT this."""
    try:
        import ctypes, ctypes.util
        from ctypes import c_void_p, c_char_p
        cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        cg.CGSessionCopyCurrentDictionary.argtypes = []
        cg.CGSessionCopyCurrentDictionary.restype = c_void_p
        cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = c_void_p
        cf.CFDictionaryGetValue.argtypes = [c_void_p, c_void_p]
        cf.CFDictionaryGetValue.restype = c_void_p
        cf.CFBooleanGetValue.argtypes = [c_void_p]; cf.CFBooleanGetValue.restype = ctypes.c_ubyte
        cf.CFRelease.argtypes = [c_void_p]; cf.CFRelease.restype = None
        d = cg.CGSessionCopyCurrentDictionary()
        if not d:
            return None
        try:
            k = cf.CFStringCreateWithCString(None, b"CGSSessionScreenIsLocked", 0x08000100)
            v = cf.CFDictionaryGetValue(d, k)
            cf.CFRelease(k)
            return bool(cf.CFBooleanGetValue(v)) if v else False
        finally:
            cf.CFRelease(d)
    except Exception:
        return None


def retina_scale() -> float:
    """screencapture pixels per cliclick logical point — READ from the main display
    (every SUPPORTED_DISPLAYS config is 2.0; falls back to 2.0 if unreadable)."""
    g = main_display_geometry()
    return float(g[2]) if (g and g[2]) else 2.0


def _diag(what: str, shot_path: str | None = None, **facts) -> str:
    """Preserve the evidence for a shim failure: copy the screenshot + write a sidecar
    JSON (what was sought, match scores, display geometry, lock state) into DIAG_DIR,
    keeping only the newest DIAG_KEEP pairs. Returns the saved path ('' on failure).
    This is what makes a lid-closed overnight failure debuggable after the fact."""
    try:
        import json, glob, shutil as _sh
        os.makedirs(DIAG_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = os.path.join(DIAG_DIR, f"{stamp}-{what}")
        if shot_path and os.path.exists(shot_path):
            _sh.copy2(shot_path, base + ".png")
        g = main_display_geometry()
        with open(base + ".json", "w") as f:
            json.dump({"what": what, "when": stamp, "display": g,
                       "screen_locked": screen_locked(), **facts}, f, indent=1)
        olds = sorted(glob.glob(os.path.join(DIAG_DIR, "*.json")))[:-DIAG_KEEP]
        for j in olds:                      # ring-buffer: drop the oldest pairs
            for p in (j, j[:-5] + ".png"):
                try: os.remove(p)
                except OSError: pass
        return base + ".png"
    except Exception:
        return ""


def find_button(screenshot_path: str, template_path: str, *,
                threshold: float = 0.8, scale: float | None = None):
    """Locate the template's center in the screenshot.

    Returns (x, y) in LOGICAL points (ready for cliclick), or None if match
    confidence < threshold. Robust to where the button sits on screen — the
    whole reason we template-match instead of using fixed coordinates.
    """
    pos, score = match_template(screenshot_path, template_path, scale=scale)
    if pos is None or score < threshold:
        # A MISS is the failure mode nobody can see with the lid closed — preserve it.
        _diag("miss-" + os.path.splitext(os.path.basename(template_path))[0],
              screenshot_path, template=os.path.basename(template_path),
              score=round(score, 4), threshold=threshold)
        return None
    return pos


def match_template(screenshot_path: str, template_path: str, *, scale: float | None = None):
    """((x, y) logical points | None, best_score). Same matcher as find_button without
    the threshold — used by the smoke test so every template reports a SCORE, making a
    drifting match visible BEFORE it drops under the threshold mid-run."""
    shot = cv2.imread(screenshot_path)
    tmpl = cv2.imread(template_path)
    if shot is None or tmpl is None:
        return None, 0.0
    res = cv2.matchTemplate(shot, tmpl, cv2.TM_CCOEFF_NORMED)
    _minv, maxv, _minl, maxloc = cv2.minMaxLoc(res)
    h, w = tmpl.shape[:2]
    s = scale if scale is not None else retina_scale()
    return ((maxloc[0] + w / 2) / s, (maxloc[1] + h / 2) / s), float(maxv)


def found(screenshot_path: str, template_path: str, *, threshold: float = 0.8) -> bool:
    """True if the template is present at/above threshold (no coordinate needed)."""
    return find_button(screenshot_path, template_path, threshold=threshold) is not None


# --- UI primitives (each needs the runner's TCC grants) --------------------

def activate():
    subprocess.run(["osascript", "-e", f'tell application "{APP}" to activate'], check=False)
    time.sleep(1)


def enter_fullscreen():
    """Deterministic full-screen via Accessibility (no coordinate click).
    No-op-safe: if already full-screen, AXFullScreen=true does nothing."""
    subprocess.run(["osascript", "-e",
                    f'tell application "System Events" to tell process "{APP}" '
                    'to set value of attribute "AXFullScreen" of window 1 to true'],
                   check=False)
    time.sleep(2)


def screenshot(path="/tmp/_dvshim_shot.png", *, attempts=4, delay=1.5) -> str:
    """Capture the main display, RETRYING briefly on failure. `screencapture` returns
    "could not create image from display" while the display set is in transition — the
    lid closing/opening (clamshell switch) tears the old display down and a single-shot
    capture fails (live-caught 2026-07-17 the instant the lid shut). A few retries ride
    that out; a persistent failure still raises, so a real grant/lock problem is loud."""
    last = None
    for i in range(attempts):
        r = subprocess.run(["screencapture", "-x", path], capture_output=True, text=True)
        if r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
            return path
        last = (r.stderr or r.stdout or "").strip()
        if i < attempts - 1:
            time.sleep(delay)
    raise RuntimeError(f"screencapture failed after {attempts} attempts: {last}")


def click(x, y):
    subprocess.run([CLICLICK, f"c:{int(round(x))},{int(round(y))}"], check=True)


def _t(name):
    return os.path.join(TEMPLATES, name)


# --- the steps -------------------------------------------------------------

def goto_dolby_vision(resolve, *, settle: float = 1.0) -> bool:
    """From ANY page: Color page, full-screen, Dolby Vision palette OPEN.
    Idempotent — if the palette is already open (Analyze 'All' visible) it does
    NOT click the icon again (which would close it). Returns True when open."""
    resolve.OpenPage("color")        # page-independent switch to Color
    activate()
    enter_fullscreen()               # deterministic layout before matching
    if found(screenshot(), _t("analyze_all.png")):
        return True                  # palette already open
    dv = find_button(screenshot(), _t("dolby_vision_palette.png"))
    if not dv:
        raise RuntimeError("Dolby Vision palette icon not found on the Color page")
    click(*dv)
    time.sleep(settle)
    return found(screenshot(), _t("analyze_all.png"))


def verify_target_display() -> bool:
    """Guard: the DV palette's Target Display Output must be the 1000-nit ST.2084
    entry. It defaults to 100-nit BT.709 (SDR) on fresh projects and analyzing
    against it yields invalid metadata (the user caught this once). We INHERIT
    the project's value — this only refuses to proceed if it's wrong."""
    return found(screenshot(), _t("target_1000nit.png"))


def click_analyze_all():
    btn = find_button(screenshot(), _t("analyze_all.png"))
    if not btn:
        raise RuntimeError("Analyze 'All' button not found in the Dolby Vision palette")
    click(*btn)


def analyzing_in_progress() -> bool:
    """True while the 'Analyzing all clips…' modal (its Cancel button) is on screen."""
    return found(screenshot(), _t("analyze_modal.png"))


def wait_for_analysis(*, abort=None, poll: float = 10.0,
                      appear_timeout: float = 150.0, max_seconds: float = 3600.0) -> bool:
    """Block until Analyze All Shots truly finishes. Tracks the analyze MODAL: wait
    for it to APPEAR (analysis started) then DISAPPEAR (done). The old region-hash
    approach false-fired — the Min/Max/Avg strip reads 0.000 the whole analysis and
    looks 'stable' once the initial UI transient settles, so it returned in ~55 s
    while the analyze was still at 52%. Honours `abort` (stop-time)."""
    start = time.time()
    saw_modal = False
    gone = 0
    while True:
        if abort is not None and abort.is_set():
            return False
        activate()                            # keep Resolve frontmost for the screenshot
        present = analyzing_in_progress()
        elapsed = time.time() - start
        if present:
            saw_modal, gone = True, 0
        elif saw_modal:
            gone += 1
            if gone >= 2:                     # modal closed (2 polls) → analysis complete
                return True
        elif elapsed > appear_timeout:        # modal never showed — click missed / no grants
            return False
        if elapsed >= max_seconds:
            return saw_modal                  # timed out — accept only if it actually ran
        time.sleep(poll)


def run_dv_ui(abort=None, expect_nit=1000) -> bool:
    """The full per-episode UI step: open the DV palette, verify the inherited target,
    Analyze All Shots, wait for completion. Everything else (color, DV Profile 8.1,
    target display) is inherited from the project and left untouched. Returns True on
    a completed analysis.

    `expect_nit` is the project's DV mastering ceiling: the SDR-intake project uses
    1000-nit (verified against a template, guarding the 100-nit-SDR-default regression);
    the HDR-intake project uses 2000-nit, for which there's no template yet, so we trust
    the project's configured target rather than block the run. Raises on a hard failure
    so the resolve stage reports it and the episode parks (resumable)."""
    if cv2 is None:
        raise RuntimeError("cv2 not available — needed for DV template matching")
    # LID-CLOSED CONTEXT: log which display this ran against, and catch the failure mode
    # that makes every template miss look inexplicable — a locked session (caffeinate -d
    # blocks the screensaver but NOT the login-window lock).
    g = main_display_geometry()
    locked = screen_locked()
    print(f"[{time.strftime('%H:%M:%S')}] dv_ui: display={g} locked={locked}", flush=True)
    if locked:
        _diag("session-locked")
        raise RuntimeError("the session is LOCKED — screencapture only sees the lock screen, "
                           "so no template can match. Disable the screen lock for lid-closed runs.")
    import resolve
    r = resolve.connect()
    if not goto_dolby_vision(r):
        raise RuntimeError("Dolby Vision palette did not open (see ~/.topaz-pipeline/diag)")
    if expect_nit == 1000 and not verify_target_display():
        _diag("wrong-target-display", expect_nit=expect_nit)
        raise RuntimeError("Target Display Output is not the 1000-nit ST.2084 entry — "
                           "refusing to analyze against the wrong (likely 100-nit SDR) target")
    click_analyze_all()
    ok = wait_for_analysis(abort=abort)
    if not ok:
        _diag("analysis-incomplete", expect_nit=expect_nit)
    return ok


def capture_reference():
    """One-time on the target machine (Screen Recording granted): with the DV
    palette open in full-screen Resolve, save a reference screenshot to crop the
    templates from at native resolution."""
    os.makedirs(TEMPLATES, exist_ok=True)
    print("saved", screenshot(os.path.join(TEMPLATES, "_reference_fullscreen.png")))


if __name__ == "__main__":
    capture_reference()
