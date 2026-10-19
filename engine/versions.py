"""The EXACT external-app + hardware pins Visionary is built for — single source of truth.

Read by engine/preflight.py (the setup/runtime gate), the dashboard server and the tests. The
pins are LOAD-BEARING, not advisory:

- The Topaz stage invokes THIS Topaz build's bundled ffmpeg (`tvai_up` filter) with the
  parameter schema and `prob-4` model that build ships.
- The Dolby Vision step runs THIS toolchain (dovi_tool, the hdr_analyzer_mvp build, the pinned
  SDR->PQ LUT, Homebrew ffmpeg 8): it is what every master's metadata and colour was judged on.

Do NOT edit these to make a mismatched machine pass preflight — install the exact builds
instead (see README "Requirements").
"""
import os

# Topaz Video AI — provides the bundled ffmpeg + models the pipeline invokes headlessly.
TOPAZ_APP = "/Applications/Topaz Video AI.app"
TOPAZ_VERSION = "7.0.1"        # CFBundleShortVersionString — the gate (exact match)

# Hardware — sustained 140 W: the Topaz stage runs the GPU flat-out for hours. A DESKTOP Mac is
# mains-powered by definition and always satisfies this; a LAPTOP needs a 140 W-class machine +
# brick, or the battery drains mid-encode and the power gate holds forever.
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

# THE DOLBY VISION TOOLCHAIN (engine/dvnative.py, nativerender.py). Part of the arm gate
# (preflight.check_native_dv): without it no item can get its Dolby Vision. They pin the
# toolchain the blind A/B on the TV was judged on.
DOVI_TOOL_VERSION = "2.3.0"         # `dovi_tool --version`. The `generate` config the lane writes
                                    # (CM v4.0, l1_avg_pq_cm_version) is version-sensitive.
HDR_ANALYZER_PATH = os.path.expanduser("~/.topaz-pipeline/bin/hdr_analyzer_mvp")
HDR_ANALYZER_COMMIT = "383b0ad"     # tinof/hdr-analyze, built from source (cargo) at this commit
HDR_ANALYZER_SHA256 = "41727312f4a960b27d59cb0c0f24c2922dc82768da3503f083e4842a87fc099e"
                                    # ...of THAT build. A hash cannot see a `brew upgrade ffmpeg`
                                    # breaking its libav*.62 links, so preflight also RUNS it.
NATIVE_LUT_PATH = os.path.expanduser("~/.topaz-pipeline/luts/rcm_sdr709_to_pq1000.cube")
NATIVE_LUT_SHA256 = "e410e5b7b899a07096a77e2f65bdeb6a4ab3cc60126358a81c8050251d8c59b0"
                                    # the captured SDR->PQ conversion (archived with its sha256
                                    # manifest); any other file changes the picture
NATIVE_FFMPEG_MAJOR = 8             # Homebrew ffmpeg: the LUT's colour was measured on 8.0, and
                                    # the analyzer links ffmpeg 8's major-versioned libraries

# THE MASTER MUX (remux.py, nativerender.py; 2026-10-18 — every master is Matroska). mkvmerge writes
# each master's Dolby Vision configuration record itself, from the RPU NALs and the HEVC VUI, so it
# has a FLOOR, not a pin: the mux is lossless and every master is verified after it (DV 8.1, the
# exact frame count, the RPU). 90.0 is the first release with every fix the mux relies on — Dolby
# Vision from an Annex B ES (58.0), unspec62/63 NALs never missed (69.0), HEVC POC handling (90.0).
MKVMERGE_MIN_VERSION = (90, 0)      # `mkvmerge --version`: "mkvmerge v99.0 ('Buka') 64-bit"
