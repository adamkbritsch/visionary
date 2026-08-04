# Overnight orchestrator

`orchestrator.py` + `stages.py` turn the app into an unattended runner. You
**Activate** it once (the header's Activate button / `POST /api/automation
{enabled:true}`) and that arm state is **persisted** — `settings.activated` is
appliance mode, so the server re-arms the orchestrator on launch
(`server._rearm_loop`) and it runs whenever it can. There is **no auto-stop**: a run
ends only when you stop it (`enable()` records no deadline — "started — running until
you stop it"). It **round-robins** over the active shows, taking one item from each in
turn. `max_active_shows` (default 3) is an ADMISSION gate — how many shows you may add
— not the width of the rotation: reads of the active list truncate only at the hard
`settings.MAX_ACTIVE_CEILING` (4), so lowering the setting never drops a show that is
already running; the extra slots drain away as those shows finish.

## The per-item pipeline (on local scratch)

Six stages, split between a **run thread** and the **finisher lanes**, so item N's
headless tail — chiefly its ~75-min x265 remux — runs while item N+1 **downloads and
upscales**. It does NOT overlap N+1's screen work: `resolve` and `remux` are mutually
exclusive, gated in both directions (see below).

- **run thread** — `RUN_STAGES = ["download", "topaz", "resolve"]`, taken in order for
  one item at a time. Only `resolve` is genuinely exclusive: it drives the Resolve UI
  on the screen, so while it runs both remux lanes hold. `topaz` needs the GPU but is
  MEANT to overlap the finisher — that is the whole split — and yields at a segment
  boundary only when two remux lanes are live at once. `download` needs neither the
  GPU nor the screen; it sits here because it feeds them, and the **prefetch daemon**
  runs that same stage concurrently for upcoming items (see below).
- **finisher lanes** — `FINISH_STAGES = ["remux", "upload", "cleanup"]` (CPU + network
  only). There is no lane pool: the `finisher` daemon takes exactly one item at a time,
  and a SECOND daemon, `finisher2`, opportunistically drains a backlog. Between them at
  most `finisher_lanes` remuxes run at once (default 2; set it to 1 and lane 2 never
  fires). The exclusion is two-way — `resolve` holds at its doorstep while a remux is in
  flight, and starting `resolve` makes an in-flight remux yield at its next segment.

`enable()` actually spawns seven daemons — `run`, `finisher`, `finisher2`, `prefetch`,
plus the `power_monitor`, `dimmer` and `plex_monitor` helpers — and `_ensure()` restarts
any that died, so a disable→enable can never leave the run armed without its watchdogs.

| # | stage    | in → out                              | runner |
|---|----------|---------------------------------------|--------|
| 1 | download | NAS source → `scratch/<src>`, then normalised to a CONSTANT frame rate → `<stem>_cfr.mp4` **or** `_cfr.mkv` | `transfer.download` (FTP) + `topaz.to_cfr` |
| 2 | topaz    | CFR source → `<stem>_prob4_upscaled.segments/` (scene-cut ProRes chunks + manifest) | `topaz.upscale_resumable` |
| 3 | resolve  | ProRes segments → `<stem> HDR10 DV upscaled.mov` (mute) | `resolve_pipeline` + dv_shim |
| 4 | remux    | mute DV video + audio from the CFR copy + subtitles from the original download → peak-capped `<mstem> HDR10 DV upscaled.mp4\|.mkv` | `remux.remux` (dvcap x265 + MP4Box) |
| 5 | upload   | master → NAS library                  | `transfer.upload` (FTP STOR, owner 1000:10) |
| 6 | cleanup  | delete **every** local working file   | `stages._cleanup` |

Every PRODUCED file carries an `upscaled` tag, and the deliverable keeps `HDR10 DV` in its
name because the queue's done-detection (`_DV_MARK`) and the app's Outputs panel both match
on it. `<mstem>` is the **retagged** stem: `master_stem()` rewrites the source's own labels
so the shipped file advertises what it now is — x264→x265, 1080p→2160p, 8bit→10bit,
SDR→HDR (episodes and movies only; YouTube keeps youtarr's exact stem so its copied
sidecars still match). `relabel_container` moves `source_cfr`, `final` and `nas_final` to
the chosen container together, and the finisher re-applies the SAME one on a durable resume
rather than re-probing — otherwise a resumed remux would rebuild an MKV item as `.mp4` and
silently drop its lossless audio and bitmap subs.

- **One download, reused.** The source is fetched once. The CFR copy derived from it
  feeds Topaz (2) and donates the **audio** to the remux (4); the original download
  stays around as the **subtitle** donor. Both are deleted at cleanup (6), after a
  verified upload.
- **Why CFR.** A variable frame rate is what broke downstream frame counts, so the
  source is normalised before Topaz ever sees it. This is usually **not** a re-encode:
  a source that is already CFR + 4:2:0 with an exact container timebase is
  stream-copied (the common case for modern rips). An MKV whose 1 ms timebase cannot
  represent the frame period exactly — an NTSC 1001/24000 s frame — IS re-encoded,
  because a jittery stream-copy makes Topaz duplicate frames and drift the audio.
- **The CFR container follows the source.** `remux.container_ext` picks `.mkv` when the
  source carries lossless audio (TrueHD / DTS-HD MA / PCM / FLAC) or bitmap subtitles
  (PGS / VOBSUB), else `.mp4`; `apply_container` locks it in once the file is on disk.
  The `_cfr.mp4` in the path factories is only a pre-download default, never consumed.
- **The remux is not a plain mux.** It re-encodes the Resolve render through x265
  under a hard peak-bitrate ceiling (`dvcap`, `max_peak_mbps`) and puts the original
  audio + subtitles back. HARD GATE — no uncapped fallback.
- **Cleanup frees the disk** every item, so it runs indefinitely.

## The prefetcher
A pure accelerator: a daemon that runs the SAME download stage (`low_prio=True`) for
upcoming queue items while the run thread is busy in topaz/resolve, so the GPU never
waits on a download. `low_prio` clamps the CFR **encode** to background QoS (E-cores on
Apple silicon, `taskpolicy -c background`); the FTP transfer itself runs normally. Everything it makes, the foreground download stage also makes and
detects via `stage_done`, so killing it just leaves partials the foreground re-pulls.
Bounded three ways: `prefetch_cap_gb` (buffer ceiling, default 100; **0 turns it off**),
a free-space gate a margin above `min_free_gb`, and `_reclaim_for_pipeline`, which purges
the whole buffer the moment the in-flight item needs the disk — pipeline beats queue.
It also yields to playback: while any Plex client is streaming it pauses and aborts an
in-flight pull, so precache I/O can't stutter what you're watching.

## Resumability
Each stage validates its **own output** (`orchestrator.stage_done`, via ffprobe /
FTP size); `first_incomplete_stage()` is the resume point and no partial output counts
as done (size / DV-8.1 / audio checks are strict).

Topaz and the remux go further — both are **segmented**, so an interrupted stage does
not restart from zero: a kill loses at most one short segment and picks up from the
last completed one. The finisher also keeps a **durable work-list**
(`~/.topaz-pipeline/finisher_queue.json`), so an item killed mid-remux is reconciled
on re-arm and resumes on the finisher thread while the run thread gets on with the
next item's Topaz.

## Power gating
There is no time window and no stop-time — only power.
- The live gate is **`_power_ok()`**, which compares the reported adapter wattage
  against `min_adapter_watts` (default 140 W, read via `_min_watts()`). That is the
  whole test: **battery drain on a big brick is normal under load and does NOT pause
  the run**. A lesser brick (hub / monitor USB-PD) or battery is insufficient; a Mac
  with **no battery** is mains-powered and always passes.
  *(Two pure helpers used to shadow this — `gate_state` and `drain_gate`, the latter
  implementing a >5%-battery-drain pause. Both were deleted once they had no callers:
  their rules are now asserted directly against `_power_ok`.)*
- Lose power mid-stage and a countdown starts (`unplug_grace_seconds`, default 60);
  if the adapter is back before it expires nothing is lost, otherwise the stage is
  abandoned and the run pauses. Topaz and the remux honour `abort` and resume from
  their last segment.

## Per-show profiles + settings (`settings.py`)
`~/.topaz-pipeline/settings.json` holds the UNIVERSAL settings — power
(`min_adapter_watts`, `unplug_grace_seconds`), the screen (`dim_after_minutes`,
`quiet_mode`), output (`max_peak_mbps`, `audio_target_lufs`), eligibility
(`passthrough_min_mbps`, `max_youtube_minutes`, `youtube_every_tv_episodes`) and
scheduling/capacity (`max_active_shows`, `finisher_lanes`, `min_free_gb`,
`prefetch_cap_gb`, `max_episode_fails`, `poll_minutes`). Every numeric key is clamped
on write AND on read against one table, `settings.LIMITS`.

`show_profiles.json` is PER-ITEM — keyed by the TV show name, movie title, or YouTube
channel folder (`p.series` is that key for all three) — and stores a preset **key**
plus booleans, never raw Topaz params:
`{key: {preset, unwatched_first, normalize_audio, featurettes_last, replace_source,
yt_scope}}` (`yt_scope` only applies to channels). A legacy bare-string entry, which
was the preset key alone, still loads and migrates to a dict on the next write.
The Topaz stage resolves params through `settings.show_topaz_params(p.series, res)`
(`stages.py:242`), which looks the preset up by content type AND source-resolution
bucket; unconfigured items get the DIGITAL default.

A show's preset is chosen as a **step when you select the series** (or add the movie) —
`PresetChooser` in the app, not a Settings screen. Endpoints: `GET/POST /api/settings`,
`GET/POST /api/show-profile`.

## FTP transport — configured host list, tried in order
`transfer.connect()` tries the configured hosts in order — typically **a VPN/Tailscale IP
first (works home + away) → a LAN `.local` name (fallback)**. Hosts AND credentials come
from `~/.topaz-pipeline/config.json` (or `TOPAZ_NAS_FTP_*` env) — **never hardcoded**:
```json
{ "ftp_hosts": ["100.x.y.z", "mynas.local"], "ftp_user": "<nas-user>", "ftp_pass": "…" }
```
A single `ftp_host` (or `TOPAZ_NAS_FTP_HOST`) forces one host.

## What unattended running depends on
All in place:
- **FTP password** — set in `~/.topaz-pipeline/config.json` (login verified over Tailscale). ✓
- **`cliclick`** — installed (`/opt/homebrew/bin/cliclick`). ✓
- **DV UI step** — codified in `dv_shim.run_dv_ui()`: opens the palette, verifies the
  inherited 1000-nit target, Analyze All, waits for completion. Only "Analyze All"
  is UI; color/DV-8.1/target all inherit (never uncheck HW accel). Templates captured. ✓
- **`Overnight Upscaler` project** — exists, HDR-PQ + DV configured (renamed from
  `Dolby Vision Test`). `setup()` loads it and STOPS if it's ever missing. ✓

**The one setup step that is user-only and can't be scripted:** grant the
orchestrator's python process **Screen Recording** + **Accessibility** in System
Settings → Privacy & Security. screencapture/cliclick run as that process, and
Claude's computer-use grant does NOT transfer to it. Until granted, the resolve stage
returns not-ok and the item parks there (resumable); download/Topaz/remux/upload/
cleanup/gating all run hands-off regardless.
