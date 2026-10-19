# SETUP-CLAUDE — the guided-install runbook

This mirrors the README's "Setup (manual / development path)" steps 1-10 **exactly**
(same numbering — the README is the canonical source; if they ever disagree, the README
wins). It adds what Claude needs: per-step checks, expected preflight output, and
secrets etiquette.

Note there is now a FASTER route for most steps: the app's own **Settings → Setup**
section (README "Install") holds the config fields, one-click dependency installs and the
Dolby Vision toolchain check. When guiding a user who already has the app
running, prefer pointing them at the in-app rows; this runbook remains the full manual
equivalent (and the only path on a machine where the app can't launch yet).

**The loop:** after every step, run

```bash
python3 engine/preflight.py --json
```

and read `checks[]`. Each check has `id`, `ok`, `severity` (`fail` gates the app;
`warn` is setup-progress), `detail` (what was found), and `fix` (the remediation —
relay it verbatim when the user must act). `hard_ok` false means the app will refuse
to arm. Exit codes: 0 = all pass, 1 = a hard check fails, 2 = warnings only.

**Secrets etiquette:** ask the user to paste values into `~/.topaz-pipeline/config.json`
themselves, or accept them and write them directly to that file — never repeat a token
or password back into the conversation, never commit them, never put them in logs.

---

### Step 1 — Machine check
Clone, run preflight. `power_adapter` severity depends on the machine: a DESKTOP Mac (no
battery) always passes — it is mains-powered by definition. A LAPTOP is judged by model:
known 140 W-class passes, a model known to cap lower (14-inch M1/M2 MBP, any MacBook Air) is
a HARD FAIL, and an unrecognised model falls back to the live adapter reading (a WARN when
unplugged — the run-time gate enforces it when the pipeline runs).

### Step 2 — Exact Topaz install (USER does this)
Watch `topaz_version`. It must report the exact pin (`7.0.1` — from `engine/versions.py`).
- App missing → give the archive link from the check's `fix`; the user installs, logs in
  once (that activates headless use and downloads the models), quits.
- Wrong version found (including 7.0.x point builds) → the user must replace it with the
  exact build. Never "make it work anyway."
- Remind them to disable auto-updates (the `fix` string says this too). `topaz_version`
  also verifies the bundled ffmpeg + models landed.
- OPTIONAL, only if they want the AI border extension (step 11): mention Comfy Desktop
  (https://www.comfy.org/) here so it can be installed in the same sitting — free, no
  pinned version, but it must be LAUNCHED once. Do not push it; it is unrelated to
  everything else and preflight never checks it.

### Step 3 — Brew tools
`brew_tools` check. Run the command from `fix` (it's in the README too), then
`brew pin ffmpeg dovi_tool` — the Dolby Vision step is pinned to ffmpeg 8 and dovi_tool 2.3.0.

### Step 4 — Dolby Vision toolchain
`native_dv` check (severity fail — part of the arm gate). It verifies hdr_analyzer_mvp by
sha256 AND runs it, the LUT by sha256, dovi_tool's exact version and ffmpeg's major.
- hdr_analyzer_mvp and the LUT ship in `dv/` (dv/README.md). The app installs a missing one at
  launch; from a clone run `python3 engine/dvinstall.py` (`--repair` also replaces a file that
  is not the pinned one, keeping it beside as `.replaced-<stamp>`). Never substitute another
  LUT — it IS the colour conversion.
- A `does not run` result after a `brew upgrade ffmpeg` means the analyzer's libraries moved
  (it links Homebrew's ffmpeg 8 at /opt/homebrew/opt/ffmpeg): reinstall ffmpeg 8 and pin it.

### Step 5 — Python dependency
`python_deps` probes `/usr/bin/python3` specifically (the app's engine interpreter; the
border-extension stage needs cv2 in it). Don't install into conda/homebrew pythons — it
won't count.

### Step 6 — Re-run preflight
Expected now: everything `ok` except `config` (a `warn` — step 8 clears it).

### Step 7 — Build and launch
```bash
bash macapp/setup-signing-cert.sh && bash macapp/build.sh && open Visionary.app
```
- `build.sh` must print `built:`.
- `curl -s http://127.0.0.1:8765/api/selftest` → `hard_ok` true.
Optional: the user can import `bundle/topaz/*.json` in Topaz's GUI (reference only).

### Step 8 — Configure
```bash
mkdir -p ~/.topaz-pipeline && cp config.example.json ~/.topaz-pipeline/config.json && chmod 600 ~/.topaz-pipeline/config.json
```
Walk the user through each key (README "Configuration" table): NAS host(s) (VPN IP
first, LAN name second), FTP user/pass — then the OPTIONAL extras: Plex URL + token
(only if they use Plex; link them Plex's find-your-token article), TMDb, youtarr. If
their media roots differ from `/Media/TV-Shows` etc., set the `TOPAZ_NAS_FTP_*` env
overrides. A user without Plex leaves the `plex_*` keys blank — that is a fully
supported setup (see the README's "Plex is optional" note).

### Step 9 — NAS check
```bash
python3 engine/preflight.py --json --network
```
`config` must show `FTP: connected`, plus `Plex: reachable` if a token is configured
(a Plex-less setup shows `Plex: not configured (optional)` — that's a pass, not a
failure). Real failures are network truths — VPN down, wrong host, bad token; fix
with the user, don't skip. Optional NAS extras:
`nas/dv_probe.py` (see `nas/README.md`) and youtarr (its absence just disables the
YouTube mode — say so and move on).

### Step 10 — Final verification + first run
```bash
python3 engine/preflight.py --json --network
```
Everything green → have the user open Visionary, pick a show, press **Activate**, and watch
one episode go download → topaz → dv (dvnative or dvrender) → remux → upload. Nothing takes
the screen. If the server answers a 409 on arming,
read its `checks` — that's the preflight refusing; step back to whichever check failed.

### Step 11 — AI border extension (OPTIONAL, skip by default)
Only if the user asks for 4:3 shows to be widened to 16:9. It never affects
`setup_complete`, and preflight does not check it — the readiness probe is
`GET /api/borders/status` (or `python3 -c "import sys; sys.path.insert(0,'engine'); import borders; print(borders.env_ready())"`).

It **requires the Comfy Desktop app** — https://www.comfy.org/ — not any ComfyUI. The
engine reads Comfy Desktop's `~/Library/Application Support/Comfy Desktop/settings.json`
for `installDir`, then expects ITS managed layout (`<installDir>/ComfyUI/ComfyUI/main.py`
plus a `.venv` beside it). A hand-cloned ComfyUI does not satisfy that, and the
`comfy_dir` config key only relocates the same layout.

USER does these two; you cannot:
1. Install Comfy Desktop and **launch it once** (that first launch creates the checkout,
   the venv and the models dir).
2. Install **ComfyUI-VideoHelperSuite** from Comfy Desktop's Manager — it is NOT bundled,
   and the workflow's output node (`VHS_VideoCombine`) needs it.

Then the models (~11.4 GB, four files) — either have the user press Install in
Settings → Setup → *Border extender (optional)*, or drive it:
```bash
for w in borders_vace borders_umt5 borders_causvid borders_vae; do
  curl -s -X POST http://127.0.0.1:8765/api/setup/install \
       -H 'Content-Type: application/json' -d "{\"what\":\"$w\"}"
  # one job at a time: poll /api/setup/install-status until .active is null before the next
done
```
Downloads resume in place (`curl -C -`), so a partial from an earlier attempt is fine.
`env_ready()` returning `(True, [])` is the acceptance gate; the per-show "Extends to
16:9" option then appears on 4:3 shows only.

---

Everything else about working in this repo (hard rules, tests, deploys): `CLAUDE.md`.
