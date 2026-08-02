"""The EXACT external-app + hardware pins Visionary is built for — single source of truth.

Read by engine/preflight.py (the setup/runtime gate), the dashboard server, the artifact
import tooling, and the tests. The pins are LOAD-BEARING, not advisory:

- The Resolve stage drives the real DaVinci Resolve UI by screen-capture template matching
  (engine/dv_shim.py) — the template PNGs were cropped from THIS Resolve build's Color page
  at THIS display's native resolution, and the click coordinates assume THIS backing scale.
  Any other Resolve version, or any other display, moves pixels and silently breaks clicks.
- The Topaz stage invokes THIS Topaz build's bundled ffmpeg (`tvai_up` filter) with the
  parameter schema and `prob-4` model that build ships.

Do NOT edit these to make a mismatched machine pass preflight — install the exact builds
instead (see README "Requirements").
"""

# DaVinci Resolve — must be the STUDIO edition (the free edition lacks the needed features).
RESOLVE_APP = "/Applications/DaVinci Resolve/DaVinci Resolve.app"
RESOLVE_VERSION = "18.6.0"     # CFBundleShortVersionString — the gate (exact match)
RESOLVE_BUILD = "18.6.00009"   # CFBundleVersion — reported for diagnostics, not gated

# Topaz Video AI — provides the bundled ffmpeg + models the pipeline invokes headlessly.
TOPAZ_APP = "/Applications/Topaz Video AI.app"
TOPAZ_VERSION = "7.0.1"        # CFBundleShortVersionString — the gate (exact match)

# Hardware — TWO independent requirements, each with its own rule below:
#  1) A MAIN DISPLAY at 2.0 backing scale (REQUIRED_BACKING_SCALE).
#  2) Sustained 140 W: the Topaz stage runs the GPU flat-out for hours. A DESKTOP Mac is
#     mains-powered by definition and always satisfies this; a LAPTOP needs a 140 W-class
#     machine + brick, or the battery drains mid-encode and the power gate holds forever.

# THE DISPLAY RULE. dv_shim locates every button by TEMPLATE MATCHING and derives every
# click from the match — there is not one fixed coordinate in it. So a template matches
# whenever the UI renders at the same BACKING-PIXEL SIZE, i.e. the same scale; the screen's
# SIZE and shape are irrelevant. Proven 2026-07-17: with the 16-inch panel's templates
# unchanged, all four matched the 3840x2160 dummy at >=0.96 (palette 1.000). So the gate is
# the SCALE, not a geometry allow-list — that admits any Retina built-in, any 4K/5K display
# at 2x, and a 4K dummy HDMI plug for lid-closed running.
# A 1x display is still refused: the UI would render at half the template's pixel size.
REQUIRED_BACKING_SCALE = 2.0
# Sanity floor, NOT a calibration: at 2x a tiny panel is still "2x" but Resolve's Color
# page cannot lay out in it (1920x1080 BACKING is only 960x540 points). Comfortably below
# both verified configs (1728x1117 and 1920x1080 points), so it excludes only nonsense.
MIN_LOGICAL_SIZE = (1280, 720)

# Configurations actually SMOKE-TESTED end to end (preflight --smoke / GET /api/shim-smoke
# matches every dv_shim template against the live screen). Purely for a precise message —
# NOT the pass/fail list. Anything else at 2x passes too, described generically; run
# --smoke on a new display the first time to confirm the templates land.
SUPPORTED_DISPLAYS = (
    {"name": "16-inch MacBook Pro built-in", "backing": (3456, 2234), "scale": 2.0, "builtin": True},
    # Lid-CLOSED clamshell operation: a 4K dummy HDMI plug keeps a framebuffer alive for
    # screencapture while the laptop is shut (verified 2026-07-17: HDP-V104, 1920x1080 pt).
    {"name": "4K dummy HDMI (clamshell)", "backing": (3840, 2160), "scale": 2.0, "builtin": False},
)
DISPLAY_PIXELS = SUPPORTED_DISPLAYS[0]["backing"]   # the built-in (back-compat for importers)
RETINA_SCALE = REQUIRED_BACKING_SCALE               # back-compat alias for importers
REQUIRED_ADAPTER_WATTS = 140   # the run-time power gate (settings min_adapter_watts) defaults to this

# THE POWER RULE, for machines WITH a battery only (a desktop Mac has no battery and is
# mains-powered by definition — it always passes; see power.has_battery).
# `hw.model` is the check, because a 140 W brick plugged into a 96 W-max laptop still
# REPORTS 140 W — the wattage alone can't tell you what the machine can actually draw.
# Apple's identifiers do not encode screen size (a 16-inch M3 Max is "Mac15,11"), so these
# are curated lists. EXTEND them; never guess:
MODELS_140W = frozenset({          # 16-inch Apple Silicon MacBook Pro (ships with 140 W)
    "MacBookPro18,1", "MacBookPro18,2",              # M1 Pro / M1 Max
    "Mac14,6", "Mac14,10",                           # M2 Max / M2 Pro
    "Mac15,7", "Mac15,9", "Mac15,11",                # M3 Pro / M3 Max  (this machine: Mac15,11)
    "Mac16,5", "Mac16,7",                            # M4 Pro / M4 Max
})
# Known to cap BELOW 140 W → hard-fail with a named reason. Deliberately NOT exhaustive:
# only models confidently under the bar are listed. Anything unknown falls through to the
# LIVE wattage check instead (preflight.check_power_adapter), so a Mac newer than this file
# is never wrongly blocked.
MODELS_BELOW_140W = frozenset({    # 14-inch M1/M2 MacBook Pro (96 W max) + every MacBook Air
    "MacBookPro18,3", "MacBookPro18,4",              # 14-inch M1 Pro / M1 Max
    "Mac14,5", "Mac14,9",                            # 14-inch M2 Max / M2 Pro
    "MacBookAir10,1", "Mac14,2", "Mac14,15", "Mac15,12", "Mac15,13",
})
