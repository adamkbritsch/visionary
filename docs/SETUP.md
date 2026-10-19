<sub>[← Visionary README](../README.md) · [Setup](SETUP.md) · [How it works](HOW-IT-WORKS.md) · [YouTube](YOUTUBE.md) · [Configuration](CONFIGURATION.md) · [Known limitations](LIMITATIONS.md)</sub>

# Setup — the manual / development path

The same result from a clone — every step has a matching in-app Setup row and a check in
the preflight tool. **Prefer a guided install?** Open [Claude Code](https://claude.com/claude-code)
in your clone and say *"set this up for me"* — the repo ships instructions Claude follows
([CLAUDE.md](../CLAUDE.md) + [docs/SETUP-CLAUDE.md](SETUP-CLAUDE.md)).

```bash
python3 engine/preflight.py          # human output; add --json for machine-readable
```

## 1. Machine check (do this first)

```bash
git clone https://github.com/adamkbritsch/visionary.git
cd visionary && python3 engine/preflight.py
```

`power_adapter` tells you whether this Mac can sustain the run (see the requirements box in the [README](../README.md)). A desktop
Mac always passes; a 14-inch M1/M2 MacBook Pro or a MacBook Air is refused.

## 2. Install Topaz Video AI — exact version&nbsp;&nbsp;<picture><source media="(prefers-color-scheme: dark)" srcset="assets/pirate-ship-white-v2.png"><img src="assets/pirate-ship-v2.png" alt="" height="59" align="middle"></picture>

- **Topaz Video AI 7.0.1** — from Topaz's
  [release archive](https://community.topazlabs.com/c/video-ai/releases). Install, log in
  once in the app (that activates the license + downloads models; the pipeline runs it
  headlessly afterwards), then **disable auto-updates** in its preferences and quit.

> [!NOTE]
> **A second app, optional.** [Comfy Desktop](https://www.comfy.org/) is needed *only* for
> the [AI border extension](#11-ai-border-extension--optional-only-for-43-shows) (widening
> 4:3 shows to 16:9). It's free, there's **no pinned version**, and nothing else in
> Visionary touches it — skip it and the feature simply never appears.
>
> If you do want it, install it now and **launch it once**: that first launch is what builds
> the ComfyUI, virtual environment and models folder Visionary later drives headlessly. A
> hand-installed ComfyUI is *not* a substitute. Then carry on with setup — the node and the
> ~11.4 GB of models come later, in [step 11](#11-ai-border-extension--optional-only-for-43-shows).

## 3. Command-line tools

**Settings → Setup → Command-line tools → Install** does this step for you. By hand it is:

```bash
python3 engine/brewdeps.py
```

which installs only what is missing and upgrades nothing: **x265**, **mkvtoolnix**, and
**ffmpeg 8**. The Dolby Vision step needs ffmpeg's major version to be exactly 8 (the LUT's colour
was measured on it, and the analyzer links its libraries), and Homebrew's `ffmpeg` formula has
moved on to 9 — so an ffmpeg 8 already at `/opt/homebrew/bin/ffmpeg` is kept as it is, and
otherwise the keg-only **`ffmpeg@8`** formula is installed and linked there (`brew install
ffmpeg@8 && brew link --force ffmpeg@8`; a newer `ffmpeg` formula is unlinked first, not
uninstalled). The analyzer finds ffmpeg@8's libraries at `/opt/homebrew/opt/ffmpeg@8/lib` by
itself. If your ffmpeg 8 is the `ffmpeg` formula, `brew pin ffmpeg` keeps a later `brew upgrade`
from moving it — an upgrade fails the `native_dv` check, and the app will not arm.

**dovi_tool 2.3.0** does not come from Homebrew (it has moved past 2.3.0 too): it ships in `dv/`
— see step 4.

mkvtoolnix's **mkvmerge** muxes every master — each one is a Matroska file (`.mkv`) carrying
Dolby Vision 8.1 — and preflight wants version 90.0 or newer (any current Homebrew build is).

## 4. Dolby Vision™ toolchain

Three pieces ship with Visionary in [`dv/`](../dv/README.md), so there is nothing to build or
download:

- **hdr_analyzer_mvp** — a build of [tinof/hdr-analyze](https://github.com/tinof/hdr-analyze)
  (MIT) at commit `383b0ad`, which measures every shot of the picture.
- **The SDR→PQ LUT** — the one colour conversion every SDR source goes through (BT.709 SDR to
  BT.2020 PQ at 1000 nits).
- **dovi_tool 2.3.0** — [quietvoid/dovi_tool](https://github.com/quietvoid/dovi_tool)'s own release
  build (MIT), which writes and injects the Dolby Vision metadata.

The app copies a missing one to its place (`~/.topaz-pipeline/bin/`, `~/.topaz-pipeline/luts/`,
`/opt/homebrew/bin/dovi_tool`) when it launches; **Settings → Setup → Dolby Vision toolchain →
Install** also replaces one that isn't the pinned file (a dovi_tool of another version is kept
beside it; any 2.3.0 already there — a brew-built one included — is left alone). From a clone:

```bash
python3 engine/dvinstall.py          # add --repair to replace a mismatched file
```

Preflight's `native_dv` row checks the analyzer and the LUT by sha256 (and actually runs the
analyzer), dovi_tool's version, and ffmpeg's major from step 3.

## 5. Python dependency (for the SYSTEM python)

```bash
/usr/bin/python3 -m pip install --user opencv-python
# if pip is missing: /usr/bin/python3 -m ensurepip --user && retry
```

The app launches its engine with `/usr/bin/python3` specifically — installing into a
conda/homebrew Python won't help.

## 6. Re-run preflight

```bash
python3 engine/preflight.py
```

Everything should pass except `config` — that's step 8.

## 7. Build and launch

```bash
bash macapp/setup-signing-cert.sh   # one-time: local signing cert
bash macapp/build.sh
open Visionary.app
```

Verify: `curl -s http://127.0.0.1:8765/api/selftest` shows `hard_ok` true. Optional: import
`bundle/topaz/*.json` in Topaz's GUI (File → Import preset) — reference only; the pipeline
embeds its Topaz parameters.

## 8. Configure

```bash
mkdir -p ~/.topaz-pipeline
cp config.example.json ~/.topaz-pipeline/config.json
chmod 600 ~/.topaz-pipeline/config.json
```

Fill in ([Configuration](CONFIGURATION.md)): NAS FTP host(s) + credentials, and — all
optional — a Plex URL + token ([how to find it](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)),
a TMDb key, and youtarr.

## 9. NAS check

```bash
python3 engine/preflight.py --network
```

FTP must connect; Plex, if you configured it, must answer (it's optional). Optional NAS extras: [nas/dv_probe.py](../nas/README.md)
(precise Dolby Vision™ detection for pre-existing DV content) and
[youtarr](https://github.com/DialmasterOrg/Youtarr) (enables the YouTube mode — without it
that mode simply stays off).

## 10. Final verification + first run

```bash
python3 engine/preflight.py --network
```

All green → open Visionary, pick a show, press **Activate**, and watch one episode flow
through download → topaz → dolby vision → remux → upload.

## 11. AI border extension — OPTIONAL, only for 4:3 shows

Skip this entirely unless you want [4:3 shows widened to 16:9](HOW-IT-WORKS.md). Nothing
else depends on it, it never affects `setup_complete`, and with it absent the per-show
option simply never appears.

It **requires the [Comfy Desktop](https://www.comfy.org/) app** — not just any ComfyUI.
Visionary runs *its* bundled ComfyUI headlessly (own venv, dedicated port 8189) and finds
it through Comfy Desktop's own `settings.json`, so the install has to be the one Comfy
Desktop manages. A hand-cloned ComfyUI won't be detected; the `comfy_dir` config key only
relocates Comfy Desktop's layout, it doesn't replace it.

1. **Install Comfy Desktop and launch it once.** The first launch is what creates the
   ComfyUI checkout, its virtual environment and the models directory — Visionary reads
   all three from it. Quit it afterwards if you like; it is never used again (Visionary
   only ever talks to port 8189, so the app's own 8188 stays free for your normal work).
2. **Add ComfyUI-VideoHelperSuite**, from within Comfy Desktop's **Manager** — it is *not*
   bundled. The outpainting workflow writes its result through that node's
   `VHS_VideoCombine`.
3. **Download the models from Visionary**: Settings → **Setup** → *Border extender
   (optional)*. Four files, **~11.4 GB** total (WAN 2.1 VACE 1.3B, the UMT5-XXL text
   encoder, the CausVid LoRA and the WAN VAE). They land in Comfy Desktop's own models
   directory, one at a time, and a partial download **resumes** rather than restarting.

The group turns green when all of it is present. Only then does the per-show
**"Extends to 16:9"** option appear — and only on shows that actually measure 4:3.

## 12. Lid closed — OPTIONAL, MacBook only

macOS sleeps a MacBook the moment its lid closes unless an external display is attached, and
no app can override that on its own: the only switch is `pmset disablesleep`, which needs
root. Run this once (it asks for your password) and Visionary turns that switch on **only
while the pipeline is activated and the Mac is on its charger**:

```bash
python3 engine/lidsleep.py --install
```

It installs a sudoers rule that allows exactly `pmset -a disablesleep 1` and `... 0` — nothing
else — after checking it with `visudo`. Sleep comes back the moment you deactivate, unplug, or
the run pauses for power; a watchdog turns it back within 5 seconds if Visionary itself stops
or crashes, so the Mac can't end up awake in a bag. A desktop Mac has no lid and needs none of
this. `python3 engine/lidsleep.py --status` shows where it stands. To remove it:
`sudo rm /etc/sudoers.d/visionary-lidsleep`.

# Repo map

| path | what |
|---|---|
| `engine/` | the Python pipeline (orchestrator, stages, preflight, dashboard server) |
| `macapp/` | the SwiftUI app + build/signing scripts |
| `dv/` | the pinned Dolby Vision toolchain files: the hdr_analyzer_mvp build, the SDR→PQ LUT and dovi_tool 2.3.0 |
| `bundle/` | Topaz presets (reference only) |
| `nas/` | optional NAS-side helper (`dv_probe.py`) |
| `tools/` | maintainer-only checks (end-to-end route runs, CPU measurements) |
| `deploy-now.sh` | redeploy the app at a safe pipeline boundary |
