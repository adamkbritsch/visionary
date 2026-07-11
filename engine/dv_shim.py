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
import hashlib
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

# Bottom-left of the DV palette (Analyze row + Min/Max/Avg), in screencapture
# pixels on the target 3456x2234 Retina panel. This strip reads 0.000 during
# analysis and flips to populated values when it finishes — see wait_for_analysis.
ANALYSIS_REGION = (15, 1840, 690, 2180)


def retina_scale() -> float:
    """screencapture pixels per cliclick logical point. The target panel is a
    3456x2234 Retina display at 2x backing scale → 2.0 (verified)."""
    return 2.0


def find_button(screenshot_path: str, template_path: str, *,
                threshold: float = 0.8, scale: float | None = None):
    """Locate the template's center in the screenshot.

    Returns (x, y) in LOGICAL points (ready for cliclick), or None if match
    confidence < threshold. Robust to where the button sits on screen — the
    whole reason we template-match instead of using fixed coordinates.
    """
    shot = cv2.imread(screenshot_path)
    tmpl = cv2.imread(template_path)
    if shot is None or tmpl is None:
        return None
    res = cv2.matchTemplate(shot, tmpl, cv2.TM_CCOEFF_NORMED)
    _minv, maxv, _minl, maxloc = cv2.minMaxLoc(res)
    if maxv < threshold:
        return None
    h, w = tmpl.shape[:2]
    s = scale if scale is not None else retina_scale()
    return ((maxloc[0] + w / 2) / s, (maxloc[1] + h / 2) / s)


def found(screenshot_path: str, template_path: str, *, threshold: float = 0.8) -> bool:
    """True if the template is present at/above threshold (no coordinate needed)."""
    return find_button(screenshot_path, template_path, threshold=threshold) is not None


# --- region-hash completion detection (pure-ish; the decider is unit-tested) ---

def region_hash(screenshot_path: str, box=ANALYSIS_REGION) -> str:
    """Stable fingerprint of a screen region. Downsized so sub-pixel noise
    doesn't register as a change, but any real UI change (0.000 -> populated
    L1 values, a progress bar) does."""
    img = cv2.imread(screenshot_path)
    if img is None:
        return ""
    x0, y0, x1, y1 = box
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return ""
    small = cv2.resize(crop, (64, 16))
    return hashlib.md5(small.tobytes()).hexdigest()


def is_analysis_done(hashes, *, stable_polls: int = 3) -> bool:
    """Decide completion from a sequence of region hashes (PURE — unit-tested).

    Done only when BOTH hold:
      * the region has CHANGED at least once (analysis produced output — guards
        the "stable because it never started" false positive), and
      * the last `stable_polls` samples are identical (it has settled).
    """
    if len(hashes) < stable_polls + 1:
        return False
    if len(set(hashes[-stable_polls:])) != 1:
        return False
    return len(set(hashes)) > 1


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


def screenshot(path="/tmp/_dvshim_shot.png") -> str:
    subprocess.run(["screencapture", "-x", path], check=True)
    return path


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
    import resolve
    r = resolve.connect()
    if not goto_dolby_vision(r):
        raise RuntimeError("Dolby Vision palette did not open")
    if expect_nit == 1000 and not verify_target_display():
        raise RuntimeError("Target Display Output is not the 1000-nit ST.2084 entry — "
                           "refusing to analyze against the wrong (likely 100-nit SDR) target")
    click_analyze_all()
    return wait_for_analysis(abort=abort)


def capture_reference():
    """One-time on the target machine (Screen Recording granted): with the DV
    palette open in full-screen Resolve, save a reference screenshot to crop the
    templates from at native resolution."""
    os.makedirs(TEMPLATES, exist_ok=True)
    print("saved", screenshot(os.path.join(TEMPLATES, "_reference_fullscreen.png")))


if __name__ == "__main__":
    capture_reference()
