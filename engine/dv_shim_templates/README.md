# DV shim templates

`dv_shim.find_button()` locates Resolve UI buttons by matching these reference
images against a full-screen screenshot (robust to window size — the reason the
shim full-screens first). Two are needed:

- `dolby_vision_palette.png` — the "DOLBY VISION" icon in the Color-page palette toolbar
- `analyze_all.png` — the **All** button in the palette's Analyze row

## Capturing them (one-time, on the target machine)

Templates are resolution-specific, so capture on the machine that will run the
pipeline, at native (Retina) resolution:

1. In Resolve, open a project with a timeline, go to the **Color** page, full-screen,
   and open the **Dolby Vision** palette (so the Analyze row is visible).
2. `python3 engine/dv_shim.py` → writes `_reference_fullscreen.png` here.
3. Crop the DOLBY VISION icon → `dolby_vision_palette.png`, and the **All** button →
   `analyze_all.png` (tight crops, a few px of padding).

## Deployment grants (required for the standalone shim)

The process that runs the shim needs, in **System Settings → Privacy & Security**:
- **Screen Recording** — for `screencapture` (a plain shell otherwise errors
  "could not create image from display")
- **Accessibility** — for `cliclick` clicks and the `AXFullScreen` call

Also: `brew install cliclick`.

Claude's computer-use grant does NOT transfer to a standalone process — the
end-to-end flow was *proven* via computer-use (2026-06-19), but the unattended
runner needs its own grants.
