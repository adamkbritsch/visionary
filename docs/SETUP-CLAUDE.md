# SETUP-CLAUDE — the guided-install runbook

This mirrors README "Setup" steps 1-10 **exactly** (same numbering — the README is the
canonical source; if they ever disagree, the README wins). It adds what Claude needs:
per-step checks, expected preflight output, and secrets etiquette.

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

### Step 1 — Hardware gate
Clone, run preflight. Gate on `display`:
- `display.ok true` → continue.
- `display.ok false` → **stop the entire setup.** Tell the user plainly: Visionary only
  works on a 16-inch MacBook Pro — its built-in display (3456×2234) must be the main
  display, and its 140 W adapter is required to run (the 14-inch MBP's 96 W maximum
  can't power the Topaz stage). If they're on the right Mac but docked/mirrored, have
  them undock and re-run.
- `power_adapter` is a WARN at setup (being unplugged is fine); it just reports whether
  a ≥140 W brick is connected. The run-time power gate enforces it when the pipeline runs.

### Step 2 — Exact app installs (USER does these)
Watch `resolve_version` + `topaz_version`. Both must report the exact pins
(`18.6.0` / `7.0.1` — from `engine/versions.py`). Common cases:
- App missing → give the archive link from the check's `fix`; the user installs + enters
  their own license, launches once, quits.
- Wrong version found (including 18.6.x point builds) → the user must replace it with the
  exact build; explain WHY (pixel-exact screen automation) if they push back. Never
  "make it work anyway."
- Remind them to disable auto-updates in both apps (the `fix` strings say this too).
- Topaz: the one GUI login activates headless use; `topaz_version` also verifies the
  bundled ffmpeg + models landed.

### Step 3 — Brew tools
`brew_tools` + `sublercli` checks. Run the commands from `fix` (they're in the README
too). Rosetta is required for SublerCLI (x86_64).

### Step 4 — Python dependency
`python_deps` probes `/usr/bin/python3` specifically (the app's engine interpreter;
fusionscript needs ≤ 3.11). Don't install into conda/homebrew pythons — it won't count.

### Step 5 — Re-run preflight
Expected now: everything `ok` except `tcc_grants`, `resolve_artifacts`, `config`
(all `warn` — steps 6-8 clear them).

### Step 6 — Build, launch, TCC grants (USER clicks the toggles)
```bash
bash macapp/setup-signing-cert.sh && bash macapp/build.sh && open Visionary.app
```
- `build.sh` must print `built:`.
- The System Settings toggles are user-only. Have them use the in-app "Request
  Accessibility" button, then enable Visionary under **Screen Recording** AND
  **Accessibility**, then relaunch the app.
- Authoritative check is the APP's context, not your terminal's:
  `curl -s http://127.0.0.1:8765/api/selftest` → `screen_recording`, `accessibility`,
  and `hard_ok` all true. (The CLI's `tcc_grants` reports the terminal's context —
  informational only.)

### Step 7 — Import Resolve artifacts
Resolve must be QUIT (the tool refuses otherwise — don't bypass):
```bash
/usr/bin/python3 setup/import_resolve.py --json
```
All steps `ok` → `resolve_artifacts` flips green. If `verify_preset` fails, Resolve was
probably running during the merge — quit it and re-run (idempotent, safe).
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
python3 engine/preflight.py --json --network --post-setup
```
(`--post-setup` promotes `resolve_artifacts` to a hard check.) Everything green → have
the user open Visionary, pick a show, press **Activate**, and watch one episode go
download → topaz → resolve → remux → upload. Warn them the resolve stage takes the
screen for ~10-15 min (Screen Control defers it). If the server answers a 409 on arming,
read its `checks` — that's the preflight refusing; step back to whichever check failed.

---

Everything else about working in this repo (hard rules, tests, deploys): `CLAUDE.md`.
