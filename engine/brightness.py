"""Display brightness (private DisplayServices API — works on Apple-Silicon built-in
displays) + user-idle time, for the orchestrator's auto-dimmer.

While a run is going and the display is caffeinated (kept logically ON so
screencapture works), drop the backlight to 0 after the user has been idle a while
— screencapture reads the framebuffer, so the resolve stage still works in the dark.
We deliberately do NOT auto-restore on activity (that poll-based restore felt laggy):
the screen stays dark and the user taps the brightness key to bring it back (native,
instant). We only restore when the run stops, or step aside if the user raises it first.
"""
from __future__ import annotations
import ctypes
import ctypes.util
import re
import subprocess

_DS_PATH = "/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices"


def _api():
    cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
    ds = ctypes.CDLL(_DS_PATH)
    cg.CGMainDisplayID.restype = ctypes.c_uint32
    ds.DisplayServicesGetBrightness.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_float)]
    ds.DisplayServicesSetBrightness.argtypes = [ctypes.c_uint32, ctypes.c_float]
    return cg, ds, cg.CGMainDisplayID()


def get_brightness():
    """Main-display brightness 0.0–1.0, or None if unavailable."""
    try:
        cg, ds, disp = _api()
        v = ctypes.c_float(0)
        if ds.DisplayServicesGetBrightness(disp, ctypes.byref(v)) == 0:
            return float(v.value)
    except Exception:
        pass
    return None


def set_brightness(level: float) -> bool:
    try:
        cg, ds, disp = _api()
        return ds.DisplayServicesSetBrightness(disp, ctypes.c_float(max(0.0, min(1.0, float(level))))) == 0
    except Exception:
        return False


def idle_seconds():
    """Seconds since the last user input (HID), or None if unreadable."""
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem"],
                             capture_output=True, text=True, timeout=10).stdout
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
        return int(m.group(1)) / 1e9 if m else None
    except Exception:
        return None


def dim_tick(idle, threshold, cur, dimmed_by_us: bool) -> str:
    """PURE (unit-tested): what the dimmer should do this tick. No auto-restore-on-activity —
    once dark it STAYS dark (the user taps the brightness key to bring it back).
      'release' — WE dropped the backlight but the user has since raised it themselves → hands off
                  (so we won't fight them, and won't override their level when the run ends)
      'dim'     — not our-dark, idle past the threshold, screen currently lit → drop to 0
      'hold'    — no change
    threshold <= 0 disables dimming entirely (settings 'Off').
    """
    if dimmed_by_us:
        return "release" if (cur is not None and cur > 0.05) else "hold"
    if threshold > 0 and idle is not None and idle >= threshold and cur is not None and cur > 0.05:
        return "dim"
    return "hold"
