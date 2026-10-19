# Claude instructions — Visionary

Visionary upscales TV/movies to 4K Dolby Vision overnight: Topaz Video AI → the native
Dolby Vision step (hdr_analyzer_mvp + dovi_tool + the pinned SDR→PQ LUT, no screen) →
peak-capped x265 → back to the NAS/Plex. It only runs on one exact setup — see the pins in
`engine/versions.py` and the requirements box in `README.md`.

## Setting this repo up for a user

Follow **`docs/SETUP-CLAUDE.md`** — it mirrors the README's "Setup (manual /
development path)" steps exactly and adds per-step machine-readable checks. If the app is
already running, the in-app **Settings → Setup** section covers config and dependency
installs — prefer it. The operating loop:

1. Run `python3 engine/preflight.py --json` between steps.
2. For each failing check, act on its `fix` string (or relay it verbatim to the user).
3. Steps the USER must do themselves: buying/entering licenses and the Topaz installer +
   first-launch login. Guide; don't attempt to do these for them.

## Hard rules (do NOT)

- **Never weaken the pins.** Do not edit `engine/versions.py`, `engine/preflight.py`,
  or the server's arm gate to make a mismatched Topaz build or Dolby Vision toolchain
  pass. The pins are load-bearing (the Topaz stage invokes that build's `tvai_up` schema;
  every master's colour and metadata were judged on that dovi_tool, hdr_analyzer_mvp
  build, LUT and ffmpeg major) — install the exact builds instead. NEVER relax the
  wattage rule to make a machine pass. POWER: a Mac with NO BATTERY is mains-powered and
  always passes; a laptop is judged by `hw.model` against `MODELS_140W` /
  `MODELS_BELOW_140W`, because a 140 W brick in a 96 W-max machine still REPORTS 140 W.
  Extend those lists for new Macs — never guess a model into MODELS_140W (an unknown
  laptop already falls back to the live reading).
- **Never replace the LUT** (`~/.topaz-pipeline/luts/rcm_sdr709_to_pq1000.cube`, pinned by
  sha256). It IS the SDR→PQ conversion; any other file changes the picture. Archived
  copies with a sha256 manifest live in `~/.topaz-pipeline/archive/` and on the NAS.
- **Never commit or print secrets.** `~/.topaz-pipeline/config.json` and `.env` are
  gitignored for a reason. Ask the user to fill values in; never echo tokens/passwords
  back into chat, logs, or commits.
- **Respect the live pipeline.** Before any disruptive action (kill/relaunch/rebuild),
  read `GET http://127.0.0.1:8765/api/state`: never interrupt while the run stage is
  `dvrender` in its encode/finish phase or the finisher stage is `upload`. To redeploy
  the app, use `./deploy-now.sh` — it waits for a safe segment boundary itself.

## Working on the code

- Engine tests: `cd engine && python3 -m unittest discover -p 'test_*.py'` (all green
  before any deploy). Dashboard: `cd engine/dashboard && python3 -m unittest test_server`.
- Build: `bash macapp/build.sh` (verify it prints `built:`). Deploy: `./deploy-now.sh`.
- The app runs the engine from the built bundle — repo edits do nothing until a deploy.
