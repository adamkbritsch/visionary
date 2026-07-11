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

# Hardware: the 16-inch MacBook Pro — required for TWO independent reasons:
#  1) Its BUILT-IN display (Liquid Retina XDR, used as the MAIN display, native pixels):
#     dv_shim's capture region + click math assume exactly this geometry.
#  2) Its 140 W power adapter: the Topaz stage runs the GPU flat-out for hours and needs the
#     full 140 W envelope. The 14-inch MBP maxes out at a 96 W brick — the power gate would
#     hold the pipeline forever (and on a lesser brick the battery drains mid-encode).
DISPLAY_PIXELS = (3456, 2234)
RETINA_SCALE = 2.0
REQUIRED_ADAPTER_WATTS = 140   # the run-time power gate (settings min_adapter_watts) defaults to this
