# Mechanical DaVinci Resolve workflow (Topaz ProRes → DV 8.1 4K)

Proven end-to-end on real episodes (S02E09–S02E11). Resolve 18.6 Studio. This is
the **resolve** stage of the overnight orchestrator (download → topaz →
**resolve** → remux → upload → cleanup): it's fed the Topaz ProRes and emits the
mute DV master. **Inherit-everything design:** the persistent `Overnight Upscaler`
project is configured ONCE by hand (HDR PQ color, DV Profile 8.1, 1000-nit target),
and every run reuses it. So there is exactly **one** UI-only step per episode —
Dolby Vision *Analyze All Shots* — and it's codified in `dv_shim.run_dv_ui()`.

## 1. Setup — API (`resolve_pipeline.py setup <prores.mov>`)
**Loads** the persistent **`Overnight Upscaler`** project (never creates it),
clears its timelines+clips in place, imports the ProRes, sets **23.976 from the
clip BEFORE the timeline exists** (no conform), builds the timeline, runs
DetectSceneCuts. Leaves Resolve on the Color page. ~1.6–2 min for scene cuts.

- **Everything is INHERITED, never set** — color, DV Profile 8.1, AND the
  Target Display. The pipeline READS the project's color (`inspect_color_management`,
  `is_hdr_project` → `colorSpaceOutput == Rec.2100 ST2084`) but never writes it.
  This replaced the old API color-flip whose bug left projects SDR instead of HDR.
- **If `Overnight Upscaler` is missing, `setup()` STOPS** with a message (it will
  NOT create a blank project — that renders SDR with no DV, and DV Profile 8.1
  can't be re-set via the API). Configure the project once, then runs inherit it.
  (The project began as `Dolby Vision Test`, renamed in the Project Manager.)
- Gotcha: you cannot `DeleteProject` the currently-open project — `setup()` loads
  the app project, then clears it **in place** (no delete/recreate).

## 2. Dolby Vision Analyze — UI (`dv_shim.run_dv_ui`)  ← the ONLY UI step
1. Full-screen Resolve (`AXFullScreen`) for deterministic, template-matchable layout.
2. `OpenPage("color")`, open the **DOLBY VISION** palette (idempotent — won't close
   an already-open palette). Located by cv2 template match, never fixed coords.
3. **VERIFY** Target Display Output is `1000-nit, P3, D65, ST.2084, Full (HOME)` —
   it's inherited from the project, but if it ever shows the 100-nit BT.709 SDR
   default the shim REFUSES to analyze (invalid metadata — the user caught this once).
4. Click **Analyze → All**, then `wait_for_analysis()` polls a region hash until the
   L1 values settle (changed-then-stable; ~7 min for ~390 shots). Honours `abort`.

## 3. Render + verify — API (`resolve_pipeline.py render <out.mov>`)
**Never touch hardware acceleration** (USER: don't uncheck it, ever). The DV Profile
8.1 lives in the project's render settings (set ONCE in the UI — the **Dolby Vision
Profile** dropdown at the BOTTOM of Advanced Settings, VISIBLE with HW accel ON; just
scroll the render panel down). The render INHERITS it, so `AddRenderJob` captures it.

**Do NOT call `SetCurrentRenderFormatAndCodec` in render()** — it RESETS the render
settings and wipes the DV Profile back to None (→ HDR10, no RPU; this is the exact
bug that produced the first S02E12 render). Set ONLY the output, everything else
inherits: `SetRenderSettings({TargetDir, CustomName, ExportVideo:True,
ExportAudio:False, FrameRate})` → `AddRenderJob` → `StartRendering`.

~4 min single-pass / ~14 min multi-pass for a 30-min 4K episode. Verify with ffprobe:
`dv_profile=8` + `dv_bl_signal_compatibility_id=1` (= 8.1) over `smpte2084`/`bt2020`,
`r_frame_rate=24000/1001`. Audio is mute → remux later.
