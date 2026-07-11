# Overnight orchestrator

`orchestrator.py` + `stages.py` turn the app into a semi-automatic nightly runner.
You **arm it each evening** (tap the status pill / `POST /api/automation {enabled:true}`);
it processes the selected series episode-by-episode and **auto-ends at 09:00**.

## The per-episode pipeline (run in order, on local scratch)

| # | stage    | in → out                              | runner |
|---|----------|---------------------------------------|--------|
| 1 | download | NAS source → `scratch/<src>.mp4`      | `transfer.download` (FTP) |
| 2 | topaz    | source → `<stem>_prob4.mov` (ProRes)  | `topaz.upscale` |
| 3 | resolve  | ProRes → `<stem> HDR10 DV.mov` (mute) | `resolve_pipeline` + dv_shim |
| 4 | remux    | DV video + **the same source** → `<stem> HDR10 DV.mp4` | `remux.remux` (MP4Box) |
| 5 | upload   | final → NAS library (replace)         | `transfer.upload` (FTP STOR, owner 1000:10) |
| 6 | cleanup  | delete **all 4** local working files  | `stages._cleanup` |

- **One download, reused.** The source is fetched once; Topaz (2) AND remux (4)
  read that same file. It's only deleted at cleanup (6), after a verified upload.
- **Topaz → Resolve is wired:** Topaz's ProRes is the Resolve stage's input.
- **Cleanup frees the disk** every episode, so it runs indefinitely.

## Resumability
Each stage validates its **own output** (`orchestrator.stage_done`, via ffprobe /
FTP size). An interrupted stage simply re-runs from its start next time — the app
"picks up at the stage it was in." `first_incomplete_stage()` is the resume point.
No partial output counts as done (size / DV-8.1 / audio checks are strict).

## Start now → stop at the next stop-time
You press **Start** any time of day (the status pill / `POST /api/automation`); it
begins immediately and `enable()` records `stop_at = next_stop(now, stop_hour)` —
the next occurrence of the stop-time (default **9 AM**, adjustable in Settings).
- `gate_state` = on AC **and** not draining (power only — no fixed start window).
- At the stop-time → auto-disable. A **watchdog** (every 30 s) aborts a *running*
  stage when it hits — Topaz honours `abort` and is killed mid-encode (resumable),
  so a long encode can't bleed hours past the stop-time.

## Per-show Topaz profiles + settings (`settings.py`)
`~/.topaz-pipeline/settings.json` (stop time, drain-pause, poll interval) and
`show_profiles.json` ( `{show: {model, scale, compression, details, halo, blend, label}}` ).
The Topaz stage uses `settings.show_profile_or_default(p.series)` — each show is
configured ONCE in the UI Settings section and reused; unconfigured shows get the
DIGITAL default. Endpoints: `GET/POST /api/settings`, `GET/POST /api/show-profile`.

## FTP transport — configured host list, tried in order
`transfer.connect()` tries the configured hosts in order — typically **a VPN/Tailscale IP
first (works home + away) → a LAN `.local` name (fallback)**. Hosts AND credentials come
from `~/.topaz-pipeline/config.json` (or `TOPAZ_NAS_FTP_*` env) — **never hardcoded**:
```json
{ "ftp_hosts": ["100.x.y.z", "mynas.local"], "ftp_user": "<nas-user>", "ftp_pass": "…" }
```
A single `ftp_host` (or `TOPAZ_NAS_FTP_HOST`) forces one host.

## To go fully unattended (the remaining gap)
Mostly closed:
- **FTP password** — set in `~/.topaz-pipeline/config.json` (login verified over Tailscale). ✓
- **`cliclick`** — installed (`/opt/homebrew/bin/cliclick`). ✓
- **DV UI step** — codified in `dv_shim.run_dv_ui()`: opens the palette, verifies the
  inherited 1000-nit target, Analyze All, waits for completion. Only "Analyze All"
  is UI; color/DV-8.1/target all inherit (never uncheck HW accel). Templates captured. ✓
- **`Overnight Upscaler` project** — exists, HDR-PQ + DV configured (renamed from
  `Dolby Vision Test`). `setup()` loads it and STOPS if it's ever missing. ✓

**The one thing left (user-only, can't be scripted):** grant the orchestrator's
python process **Screen Recording** + **Accessibility** in System Settings → Privacy
& Security. screencapture/cliclick run as that process, and Claude's computer-use
grant does NOT transfer to it. Until granted, the resolve stage returns not-ok and
the episode parks there (resumable); download/Topaz/remux/upload/cleanup/gating run
hands-off. End-to-end DV Analyze still needs one live run on real (online) media to
tune `wait_for_analysis` timings — the configured project's media was offline during
template capture.
