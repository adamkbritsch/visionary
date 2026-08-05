# Hosting Resolve on a display other than the main one

Findings from a live spike on the dev rig (2026-08-04): MacBook Pro 16" built-in +
HDP-V104 4K HDMI. Resolve 18.6.0.9. Everything below was measured, not assumed — several
of the obvious approaches silently do nothing, which is the whole reason this file exists.

## The rig

```
built-in   3456x2234 backing   2.0 scale   origin (0, 0)         uuid:37D8832A-…
HDP-V104   3840x2160 backing   2.0 scale   origin (1728, 1117)   uuid:6293C759-…
```

Both satisfy `preflight.match_display` unchanged; the HDMI plug resolves to the
`4K dummy HDMI (clamshell)` entry already in `versions.SUPPORTED_DISPLAYS`. So the
2.0-backing-scale invariant is not in play here — no pin needs relaxing.

## Moving the window: `AXPosition` DOES NOT WORK

This is the finding that matters, because everything about it looks like it works.

```
value of attribute "AXPosition"          reads correctly
settable of attribute "AXPosition"       reports TRUE
set value of attribute "AXPosition" …    SILENTLY IGNORED. osascript exits 0.
                                         The window does not move — not to another
                                         display, not even 100pt on the same one.
set value of attribute "AXSize" …        also silently ignored
```

What DOES work is the System Events **`position` property**:

```applescript
set position of w to {1768, 1157}     -- moves. verified by read-back.
```

Same object, same process, same permission — one writes and one doesn't. Use `position`.
Always read back and verify: macOS clamps out-of-range positions to the union of the
displays without reporting an error, so an unverified write is indistinguishable from a
successful one.

## Order of operations

1. **Exit full screen first.** While `AXFullScreen` is true, `AXPosition` reports
   `settable=false` and nothing can move the window. Allow ~3 s for the Space transition.
2. `set position of w to {origin.x + 40, origin.y + 40}` — inset so the titlebar is
   unambiguously on the target and not straddling a seam.
3. Read back and confirm the point is inside the target's `CGDisplayBounds`.
4. Re-enter full screen. Verified: the window then occupies **exactly** the target's
   bounds — `pos=(1728,1117) size=(1920,1080)` on the HDMI, with no further arithmetic.

## Window selection: never by index

The index moved under us three times in one session — the main window was index 2, then
index 1 after the full-screen exit, and the list was **empty** while it sat full-screen on
the other display's Space. `_FS_PICK` (select the window that owns `AXFullScreen` settably)
exists for exactly this reason; selecting by name works too. An index is never valid twice.

**`count of windows` can legitimately return 0** while Resolve is full-screen on a
non-active Space. Placement code must treat an empty window list as "look again", never as
"Resolve is gone".

## Capture

`screencapture -x -R <x>,<y>,<w>,<h>` takes **global points** and renders at the *target
display's* backing scale. Verified: `-R 1728,1117,1920,1080` produced a 3840x2160 PNG.

Prefer this over `-D <n>`, whose ordinal has no documented mapping to `CGDirectDisplayID`
and is ambiguous between two identical panels. `-R` is keyed on `CGDisplayBounds`, which
we read ourselves.

## Templates match on the HDMI

Scored against a live capture of Resolve full-screen on the HDMI, Color page, DV palette
closed:

```
dolby_vision_palette.png   0.949      (threshold 0.8 — matches)
analyze_all.png            0.549      palette was closed, correctly absent
analyze_modal.png          0.570      no analysis running, correctly absent
target_1000nit.png         0.551      only visible with the palette open
```

The one template that *should* be visible in that state matched comfortably. No second
template set is needed for this display. (Consistent with the CLAUDE.md note that this
dummy matched every template at >=0.96.)

## Placement does not persist — redo it every episode

Quit Resolve while full-screen on the HDMI, relaunch, load the same project: the window
came back at `(0, 33)` on the **main** display. Resolve stores no window frame
(`com.blackmagic-design.DaVinciResolve.plist` has four keys, none geometric; the
`<DisplayScale>-1</DisplayScale>` in `config.user.xml` is UI scaling, not position).

Two consequences:

- There is no "set it once" option. `stages.py` `pkill -9`s Resolve after every stage, so
  placement must run per episode.
- Nothing leaks. A spike, or an aborted run, cannot strand Resolve on a display the shim
  is not watching.

## What is still unverified

- Whether Resolve's Color page **lays out identically** at 3840x2160 @2x with a project
  loaded and the palette open. Only the palette icon was scored here. If the layout
  differs, per CLAUDE.md the answer is a **second** template set beside the existing one —
  never an edit to `dv_shim_templates/`.
- `screencapture -R` at **negative origins** (a display arranged left of or above main).
  The current arrangement is all-positive and cannot exercise it.
- Behaviour when the host display sleeps or is unplugged mid-analysis.
