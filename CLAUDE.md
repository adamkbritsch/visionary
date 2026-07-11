# Claude instructions — Visionary

Visionary upscales TV/movies to 4K Dolby Vision overnight: Topaz Video AI → DaVinci
Resolve (screen automation for DV analysis) → peak-capped x265 → back to the NAS/Plex.
It only runs on one exact setup — see the pins in `engine/versions.py` and the
requirements box in `README.md`.

## Setting this repo up for a user

Follow **`docs/SETUP-CLAUDE.md`** — it mirrors README "Setup" steps 1-10 exactly and
adds per-step machine-readable checks. The operating loop:

1. Run `python3 engine/preflight.py --json` between steps.
2. For each failing check, act on its `fix` string (or relay it verbatim to the user).
3. Steps the USER must do themselves: buying/entering licenses, the Resolve/Topaz
   installers + first-launch logins, and the System Settings privacy toggles. Guide;
   don't attempt to do these for them.
4. If the `display` check fails, stop — the machine is unsupported (16" MacBook Pro
   built-in display only). Say so plainly; nothing else will fix it.

## Hard rules (do NOT)

- **Never weaken the pins.** Do not edit `engine/versions.py`, `engine/preflight.py`,
  or the server's arm gate to make a mismatched Resolve/Topaz/display pass. The pins are
  load-bearing (pixel-exact screen automation) — install the exact builds instead.
- **Never commit or print secrets.** `~/.topaz-pipeline/config.json` and `.env` are
  gitignored for a reason. Ask the user to fill values in; never echo tokens/passwords
  back into chat, logs, or commits.
- **Never edit** `engine/dv_shim_templates/` or `ANALYSIS_REGION`/`retina_scale()` in
  `engine/dv_shim.py` — they're calibrated to the pinned hardware.
- **Never run `tools/export_artifacts.py`** — maintainer-only; it drives the
  maintainer's live Resolve library.
- **Never merge the render preset while Resolve is open** (`setup/import_resolve.py`
  guards this — don't bypass it; Resolve rewrites the preset file on exit).
- **Respect the live pipeline.** Before any disruptive action (kill/relaunch/rebuild),
  read `GET http://127.0.0.1:8765/api/state`: never interrupt while the run stage is
  `resolve` or the finisher stage is `upload`. To redeploy the app, use
  `./deploy-now.sh` — it waits for a safe segment boundary itself.

## Working on the code

- Engine tests: `cd engine && python3 -m unittest discover -p 'test_*.py'` (all green
  before any deploy). Dashboard: `cd engine/dashboard && python3 -m unittest test_server`.
- Build: `bash macapp/build.sh` (verify it prints `built:`). Deploy: `./deploy-now.sh`.
- The app runs the engine from the built bundle — repo edits do nothing until a deploy.
