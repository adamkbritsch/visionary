# Claude instructions — Visionary

Visionary upscales TV/movies to 4K Dolby Vision overnight: Topaz Video AI → DaVinci
Resolve (screen automation for DV analysis) → peak-capped x265 → back to the NAS/Plex.
It only runs on one exact setup — see the pins in `engine/versions.py` and the
requirements box in `README.md`.

## Setting this repo up for a user

Follow **`docs/SETUP-CLAUDE.md`** — it mirrors the README's "Setup (manual /
development path)" steps 1-10 exactly and adds per-step machine-readable checks. If the
app is already running, the in-app **Settings → Setup** section covers config,
dependency installs, permissions, and the Resolve import — prefer it. The operating loop:

1. Run `python3 engine/preflight.py --json` between steps.
2. For each failing check, act on its `fix` string (or relay it verbatim to the user).
3. Steps the USER must do themselves: buying/entering licenses, the Resolve/Topaz
   installers + first-launch logins, and the System Settings privacy toggles. Guide;
   don't attempt to do these for them.
4. If the `display` check fails, stop — the main display isn't at 2.0 backing scale
   (`versions.REQUIRED_BACKING_SCALE`). Say so plainly, with the fix: a built-in Retina
   panel, a 4K/5K display in its DEFAULT HiDPI mode, or a 4K dummy HDMI plug all qualify;
   a 1x / "More Space" mode does not.

## Hard rules (do NOT)

- **Never weaken the pins.** Do not edit `engine/versions.py`, `engine/preflight.py`,
  or the server's arm gate to make a mismatched Resolve/Topaz/display pass. The pins are
  load-bearing (the screen automation matches templates against the live UI) — install
  the exact builds instead. DISPLAY support is the 2.0 BACKING-SCALE invariant
  (`versions.REQUIRED_BACKING_SCALE`), not a geometry list — templates only match at the
  same UI pixel size, while screen SIZE is free because every click is template-derived
  (proven: the 16" panel's templates matched the 3840x2160 dummy at >=0.96 untouched).
  `SUPPORTED_DISPLAYS` is now only the SMOKE-TESTED list, used for a precise message. On
  a display that isn't on it yet, run the all-template smoke test
  (`python3 engine/preflight.py --smoke`, or `GET /api/shim-smoke` while it runs) and add
  the entry. NEVER relax the scale rule or the wattage rule to make a machine pass.
  POWER: a Mac with NO BATTERY is mains-powered and always passes; a laptop is judged by
  `hw.model` against `MODELS_140W` / `MODELS_BELOW_140W`, because a 140 W brick in a
  96 W-max machine still REPORTS 140 W. Extend those lists for new Macs — never guess a
  model into MODELS_140W (an unknown laptop already falls back to the live reading).
- **Never commit or print secrets.** `~/.topaz-pipeline/config.json` and `.env` are
  gitignored for a reason. Ask the user to fill values in; never echo tokens/passwords
  back into chat, logs, or commits.
- **Never edit** `engine/dv_shim_templates/` — the PNGs are calibrated to the pinned
  Resolve build. If a new display needs different crops, capture a SECOND set beside the
  existing one; never overwrite a working calibration. (`retina_scale()` now READS the
  main display, and the old `ANALYSIS_REGION` was deleted with the dead region-hash path.)
- **Never run `tools/export_artifacts.py`** — maintainer-only; it drives the
  maintainer's live Resolve library.
- **Never merge the render preset while Resolve is open** (`setup/import_resolve.py`
  guards this — don't bypass it; Resolve rewrites the preset file on exit).
- **Debugging a lid-closed (clamshell) run:** nobody can see the screen, so use
  `GET /api/screen.png` (the app process holds the Screen Recording grant — a plain
  shell's `screencapture` cannot), `GET /api/shim-smoke` for per-template match scores,
  and `~/.topaz-pipeline/diag/` where every shim failure drops a screenshot + JSON
  (template, score, display geometry, lock state). A LOCKED session makes every match
  fail — `caffeinate -d` does not prevent it.

- **Respect the live pipeline.** Before any disruptive action (kill/relaunch/rebuild),
  read `GET http://127.0.0.1:8765/api/state`: never interrupt while the run stage is
  `resolve` or the finisher stage is `upload`. To redeploy the app, use
  `./deploy-now.sh` — it waits for a safe segment boundary itself.

## Working on the code

- Engine tests: `cd engine && python3 -m unittest discover -p 'test_*.py'` (all green
  before any deploy). Dashboard: `cd engine/dashboard && python3 -m unittest test_server`.
- Build: `bash macapp/build.sh` (verify it prints `built:`). Deploy: `./deploy-now.sh`.
- The app runs the engine from the built bundle — repo edits do nothing until a deploy.
