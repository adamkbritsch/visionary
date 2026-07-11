# Overnight Topaz → Resolve Dolby Vision Upscaling Pipeline — Design

- **Date:** 2026-06-18
- **Status:** Approved design, pre-implementation (target hardware not yet acquired)
- **Author:** Adam Britsch (design captured by Claude)
- **Target platform:** macOS / Apple Silicon (runs on the current M3 Max laptop today; scales to a future Mac Studio-class desktop)

---

## 1. Goal

Automate the existing manual pipeline that upscales 1080p live-action TV to 4K Dolby Vision, so it can run unattended across an entire library. The system runs opportunistically overnight on battery-powered hardware (laptop on AC) and full-time on a future always-on desktop, with a GUI for control and visibility.

Per-episode pipeline (today, done by hand):

1. **Topaz Video AI** — upscale 1080p → 4K, output **SDR ProRes 422 XQ** (Proteus-only, high recover-detail, no Hyperion HDR).
2. **DaVinci Resolve Studio** — import ProRes → DaVinci YRGB Color Managed (Rec.2020 ST2084, 1000-nit) → **Detect Scene Cuts** ("separate by shot") → **Dolby Vision Analyze All Shots** → render a **mute** H.265 Main10 Dolby Vision Profile 8.1 `.mov`.
3. **Subler** — mux the original audio + subtitle tracks (from the 1080p source) back onto the mute DV video.

The automation reproduces this exactly, unattended, queue-driven, resumable.

## 2. Scope

**In scope:** unattended batch processing of a library; power/time gating; a GUI (control + status); safe RAM reclamation by closing idle apps; resumability and failure isolation.

**Non-goals (YAGNI):**
- Per-title preset tuning / classification — explicitly rejected in favour of one universal preset (see Decision D2).
- Manual color grading per episode — relies on RCM + DoVi analysis only.
- Cross-platform (Windows/Linux) support — Mac only for now; OS-specific bits are isolated so a port is possible later.
- Auto-relaunch of closed apps in the morning — optional, default off.

## 3. Key decisions (with rationale)

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | Target **macOS / Apple Silicon**, likely a future Mac Studio | Matches the proven toolchain; usable on the current laptop now. |
| D2 | **One universal Topaz preset** for the whole library | User choice for predictability/simplicity. Accepted tradeoff: pristine sources slightly over-recovered, very-blocky rips stay blocky. No classifier, no manifest gate. |
| D3 | "Divide into shots" = Resolve **`DetectSceneCuts()`** + **DoVi Analyze All Shots** (per-shot trim metadata, one file out) — **not** file splitting | Matches the validated manual workflow. |
| D4 | Bridge the un-scriptable Analyze-All-Shots step with **Approach A: a UI-automation shim** (one simulated action per episode), made robust via template-match + completion detection | User wants fully hands-off end-to-end. Brittleness is mitigated, not eliminated (see Risks). |
| D5 | **Hybrid GUI**: rumps menu-bar app + native 5-min countdown popup + localhost web dashboard | Plays to user's web-design strength for the rich view; native popup is a stronger always-on interrupt. SVG icons, no emoji (project rule). |
| D6 | **Headless remux** via SublerCLI (primary) / MP4Box-GPAC (fallback), not the Subler GUI | Required for hands-off operation; both are DoVi-aware. |
| D7 | Power/time gating is a **configurable policy layer** | Same engine runs opportunistically on a laptop (require-AC + window) and full-time on a desktop (require-AC off / 24h window). |

## 4. Verified feasibility facts

DaVinci Resolve scripting API (verified against the v20.x reference, 2026-06-18):

- **Scriptable:** RCM via `SetSetting('colorScienceMode','davinciYRGBColorManagedv2')` **first**, then `colorSpaceInput=Rec.709`, `colorSpaceTimeline=Rec.2020 Intermediate`, `colorSpaceOutput=Rec.2100 ST2084`, `hdrMasteringOn`, `hdrMasteringLuminanceMax=1000` (exact values read from the live project); media import; timeline build; `timeline.DetectSceneCuts()` (boolean, not parameterizable); delivery via `LoadRenderPreset` / `SetCurrentRenderFormatAndCodec` / `AddRenderJob` / `StartRendering` / `IsRenderingInProgress`; export constant `EXPORT_DOLBY_VISION_VER_5_1`.
- **NOT scriptable (the one real gap):** Dolby Vision **"Analyze All Shots"** — no API function. Handled by the UI shim (D4). The render-settings DV export flag is also not directly settable, so it is carried by a **saved render preset** (`LoadRenderPreset`).
- **Version note:** current machine runs Resolve Studio **18.6**; the scripting module breaks on Python 3.12 (`imp` removed) — use **Resolve's bundled Python** or a Py ≤ 3.11 venv with `RESOLVE_SCRIPT_API` / `RESOLVE_SCRIPT_LIB` / `PYTHONPATH` set. A future Resolve 20.x likely resolves this; the self-test (§9) catches drift.

Headless remux: the Dolby Vision Profile 8.1 RPU lives inside the HEVC elementary stream, so a **stream copy** (`-c:v copy` equivalent) preserves it; SublerCLI and MP4Box both write the container DV signaling. `dovi_tool` is a repair tool, only invoked if verification finds a mangled RPU.

## 5. Architecture overview

Two long-lived processes, both launchd-managed, talking over a small local control interface (localhost HTTP or Unix socket):

```
 ┌─────────────────────────────┐         ┌──────────────────────────────┐
 │ GUI (LaunchAgent, user)     │ control │ Engine (LaunchAgent, KeepAlive)│
 │  - rumps menu-bar           │◄───────►│  - Orchestrator / job queue    │
 │  - native countdown popup   │ status  │  - SQLite state                │
 │  - localhost web dashboard  │         │  - Scheduler / power-gate      │
 └─────────────────────────────┘         │  - Memory reclaimer            │
                                          │  - Stage runners (1→2→2.5→3)   │
                                          └──────────────────────────────┘
```

The **engine** does all work and is fully testable headless. The **GUI** is a thin client: it renders status and sends pause/resume/start/stop/skip-tonight commands. Gating policy is enforced in the engine (defense in depth) even though the countdown UI lives in the GUI.

### Per-episode state machine

```
PENDING → FETCHING → FETCHED → UPSCALING → UPSCALED → RESOLVE_IMPORT
        → SCENE_CUT → DV_ANALYZE → RENDERING → RENDERED → REMUX → VERIFIED
        → UPLOADING → UPLOADED → CLEANED
```

**Invariant — never process media off the NAS directly.** Every episode is
first **downloaded** from the NAS to local scratch (the external 2 TB SSD);
Topaz, Resolve, and the remux run *only* against local files; the finished
file is then **uploaded back** to replace the NAS original. No stage reads or
writes source/intermediate media over the network mount — the NAS is touched
only by the fetch at the start and the push-back at the end.

Any stage error → `FAILED` → `QUARANTINED` (with its log); the run continues to the next episode. Stages are **idempotent** so an interrupted episode restarts cleanly.

### Gating state machine (scheduler)

```
DISABLED → ARMED → COUNTDOWN(5:00) → RUNNING → PAUSED → (re-ARM)
```

## 6. Component specifications

### 6.1 Orchestrator / job queue
- **Source of work:** scan of the source library (NAS path) → manifest of episodes; new files can be appended.
- **State:** SQLite (`episodes` table: path, state, stage timestamps, attempt count, error, output path). Survives reboots; resume on launch.
- **Concurrency:** bounded-serial. Disk, not GPU, is the limiter (see §8). Default = 1 episode in flight.
- **Failure isolation:** per-episode try/except; quarantine + continue. Max retry count before permanent quarantine.

### 6.2 Topaz stage (headless)
- Copy one source from NAS → local NVMe scratch.
- Bundled-ffmpeg headless render with env `TVAI_MODEL_DIR` / `TVAI_MODEL_DATA_DIR` = app's `Resources/models`. `license.lic` present → no login prompt.
- **Universal preset (`engine/topaz.py`, built + tested) — matches the real "1080p to 4K SDR XQ - DIGITAL" preset verbatim:** filter `tvai_up=model=prob-4:scale=2:device=-2:compression=0.08:details=0.02:halo=0.05:blend=0.45` (Proteus, **no Hyperion** — HDR happens in Resolve); encoder taken verbatim from Topaz's `video-encoders.json` `prores-422-xq-osx` → `-c:v prores_videotoolbox -profile:v xq -color_range tv -pix_fmt p416le -allow_sw 1` (ProRes **4444 XQ**, the UI's "422 XQ"), PCM 24-bit audio, `.mov`. **Do not deviate to HQ.**
- Verify with ffprobe: codec `prores`, profile contains `XQ`, 3840×2160. Live-validated on this Mac (48-frame clip → real ProRes XQ in ~10 s). → `UPSCALED`.

### 6.3 Resolve stage (scripted API + UI shim)
1. Connect via `fusionscript.so` loaded with `importlib` (bypasses the py3.12 `imp` removal — works with system python3.12). `engine/resolve.py`, built + tested.
2. **RCM modeled on the live 'Dolby Vision Test' project's exact spaces, in Custom mode.** Critical finding (live): the project uses Automatic RCM (`isAutoColorManage=1`), but **in Automatic mode the API REFUSES to set the input/output color spaces** (`SetSetting` returns False; output stays Rec.709). So `resolve.py` uses **Custom mode (`isAutoColorManage=0`)** with the project's exact spaces — identical color transform: `colorScienceMode=davinciYRGBColorManagedv2` **first**, then input `Rec.709 Gamma 2.4`, timeline **`Rec.2020 Intermediate`**, output **`Rec.2100 ST2084`**, tone mapping None, `hdrMasteringOn`/`Max=1000`, timeline 3840×2160 HDR 1000, Dolby master display `1000-nit, P3, D65, ST.2084, Full`.
3. **Scripted pass — built + live-validated (`resolve.render_clip`):** import ProRes → apply Custom RCM → `CreateTimelineFromClips` → `timeline.DetectSceneCuts()` → `SetCurrentRenderFormatAndCodec('mov','H265')` → render. **Proven to produce a real 4K HDR10 file:** `hevc / Main 10 / yuv420p10le / bt2020nc / smpte2084 / bt2020`. Runs in a throwaway project and restores the open one.
4. **DV boundary (the remaining gap, needs the UI shim):** the scripted render gives **HDR10, not Dolby Vision** — the Profile 8.1 RPU requires **Analyze All Shots** (UI-only, no API) **plus** the "Dolby Vision" export flag. There is **no saved DV render preset** in the user's Resolve (only stock presets), so `LoadRenderPreset` can't supply the DV/Main10-embed flags either. To get true DV: (a) UI shim for Analyze All Shots, and (b) a saved `Topaz 4K DV` preset (§7) the user creates once, or the DV export toggled by the shim.
5. Output is a **mute** `.mov` (`ExportAudio=False`). → `RENDERED` (HDR10 today; DV once the shim lands).

### 6.4 Remux stage (`engine/remux.py`, built + tested) — triggered by Resolve "Trigger script at End of render job"
- **DV preservation tested on a real Profile 8.1 file (built with `dovi_tool`): ffmpeg's mp4/mov muxer STRIPS the Dolby Vision config box on copy (the verification gate caught this); MP4Box/GPAC preserves it.** SublerCLI isn't installed → deterministic **two-step**: (1) ffmpeg extracts every audio + subtitle track from the source (`-map 0:a -map 0:s?`, subs → `mov_text`) into a temp file; (2) MP4Box muxes the mute DV video + tracks (`MP4Box -add dv -add tracks -new out`), preserving the RPU box. All source audio+sub tracks carried by default.
- Audio copied as-is (AC3/EAC3/AAC); subtitles → `mov_text`. Image subs (PGS) can't convert to mov_text → flagged edge case.
- **Hard verification gate (built):** ffprobe must find a `DOVI configuration record` (`dv_profile 8` + `dv_bl_signal_compatibility_id 1` = **Profile 8.1**) **and** ≥1 audio track, else quarantine. Live-validated: real DV file → `DV 8.1 · 2 audio · 1 sub`. → `VERIFIED`.

### 6.5 Deliver + cleanup
- Move final to Plex library on NAS with Plex-correct naming; ownership **1000:10 / PGID 10** (UGOS gid-10 rule).
- Delete the ~300 GB ProRes intermediate **after render**; delete the local source copy **after remux + verify** (it is the audio/sub donor — must outlive the remux).
- Optional: trigger a Plex **section** scan only (not the full Kometa pipeline). → `CLEANED`.

### 6.6 Scheduler / power-gate
- launchd LaunchAgent (KeepAlive) — "always on when logged in."
- **ARMED** when on AC **and** clock ∈ [20:00, 09:00). Trigger on plug-in event during window, or at 20:00 if already on AC. Power state via IOKit power-source notifications / `pmset`.
- **COUNTDOWN:** 5-minute popup; untouched → auto-enable → RUNNING. Buttons: **Start now** · **Not tonight** (skip until next window, no re-nag) · **Settings**.
- **PAUSED** on: unplug, 09:00, or user. On unplug/09:00 → stop current episode immediately and re-queue it (lose partial work on that one episode only; idempotent), pause cleanly, auto-re-ARM next window.
- **Power-adequacy guard (`engine/power.py` + `engine/gate.py`, built + tested):** macOS can't read mains voltage, so "only run on adequate power" is behavioral. While RUNNING, sample battery state; if plugged in (`ExternalConnected`) yet **sustained-draining** under load (adapter can't keep up — e.g. a 65 W brick under an M3 Max), the supply is inadequate → pause and **retry every 30 minutes** (`gate.retry_on_drain`) — persistent, not "quit for the night": each cycle re-runs battery detection under load, so it resumes the moment power is adequate. Sustained = drain across N consecutive samples (ignores momentary spikes). Handles the macOS quirk where discharging current is reported as an unsigned 64-bit int.
- **Policy config:** `require_ac` (bool), `window_start`/`window_end`, `countdown_seconds`, `drain_samples`. Desktop preset: `require_ac=false`, 24h window.

### 6.7 GUI (hybrid)
- **rumps menu-bar app** (LaunchAgent, user session): live state (idle/armed/running/paused), current episode + progress, done/remaining/ETA, failures; quick pause/resume/skip-tonight.
- **Native countdown popup** (PyObjC NSAlert/NSWindow): the 5-minute interrupt, always-on-top.
- **localhost web dashboard** (engine-served): rich live status, queue, history, failures/logs, settings. HTML/CSS with **SVG icons, no emoji** (project rule); `plxIcon()`/`plxEsc()`-style helpers.
- **Built so far (`engine/dashboard/server.py` + `index.html`, tested):** real `/api/state` assembled from the live components (power, scratch, window, manifest) and a data-driven UI that renders truth. **Master switch `AUTOMATION_ENABLED = False`** until the app is finished — read-only "development mode": nothing auto-starts (no countdown, no app-closing, the drive is never cycled from the dashboard, no Topaz/Resolve runs launched).
- Thin client only — all state owned by the engine.

### 6.8 Memory reclaimer (safe app-quit)
- Runs at start of each RUNNING session (default: only when free RAM < threshold; config flag to always-run).
- **Never-close whitelist:** Firefox, UGREEN NAS, the engine + GUI, DaVinci Resolve, Topaz Video AI, Finder, system/background processes; editors/IDEs (VS Code, etc.) default-included. Editable in Settings.
- For every other user-facing app (`System Events` application processes, `background only` false): check unsaved state via `isDocumentEdited` / AXModified. **Clean → graceful `quit`. Dirty → skip + log "left open: unsaved work."** Never force-kill; a hung quit past timeout is left alone.
- Honest limitation: edited-state is reliable for document apps, fuzzy for some Electron/web apps → whitelist + "skip if unsure" is the safety net.
- Optional morning relaunch of closed apps (default off).

### 6.9 Cross-cutting
- **Keep-awake:** `caffeinate` while RUNNING; display on, logged-in session (the shim needs Resolve frontmost and screen awake).
- **Disk watermark:** never start an episode that can't fit; pause intake below threshold.
- **Scratch volume (`engine/scratch.py`, built + tested):** scratch lives on the external **"2TB SSD"** (~300 GB ProRes intermediates). Pre-flight force-cycles it (unmount → remount) to recover macOS "ghost" disconnects, verifies writable, else falls back to `~/Downloads/topaz-scratch`. Targets by volume name (node changes on reconnect). Fallback is internal-disk (~83 GB) → reduced-capacity mode the watermark must respect.
- **Post-update self-test:** on launch, run Topaz on a ~5-sec clip and verify the shim template-match resolves; if either fails (app update wiped Topaz presets or moved Resolve's UI), **halt before touching the library**.
- **Observability:** per-episode logs, rotating; dashboard surfaces counts + ETA; optional completion/failure notification.

## 7. Proven Resolve render preset ("Topaz 4K DV")

Saved once in the UI; loaded via `LoadRenderPreset`. From the user's validated DaVinci Resolve Studio 18.6 settings:

- Custom Export · Format **QuickTime** · Codec **H.265** · Use hardware acceleration **on**
- **Export HDR10 Metadata: off** · **Embed HDR10 Metadata: on**
- Resolution **3840×2160** · Frame rate **23.976**
- Quality **Restrict to 60000 Kb/s** · Encoding Profile **Main10** · **Multi-pass encode on**
- Advanced: **Bypass re-encode when possible on**
- **Dolby Vision Profile 8.1** · Tone Mapping **None**
- Render **Single clip**
- **Audio tab: Export Audio OFF** → mute `.mov`
- (Suggested: bump bitrate to 60 Mbps — already set; could go higher for masters.)

Verified output (prior validation): HEVC Main10 10-bit 4K, smpte2084/bt2020, Dolby Vision **Profile 8.1 BL+RPU**, HDR10-compatible, P3-D65 1000-nit — confirmed via ffprobe DOVI side-data + mediainfo.

## 8. Storage math (why bounded-serial + immediate cleanup)

ProRes 422 XQ at 4K ≈ **~2 Gbps ≈ ~250 MB/s ≈ ~15 GB/min** → a 22-min episode ≈ **~300 GB** intermediate. Final HEVC at 60 Mbps ≈ ~10 GB. Therefore: process serially, render then delete the intermediate immediately, keep source only until remux-verify. (Intermediate stays ProRes 422 XQ per the preset — no HQ substitution.)

## 9. Risks & honest caveats

1. **UI shim brittleness (highest risk).** Analyze All Shots has no API; the template-match click + completion detection can break on Resolve UI/version changes. Mitigation: self-test halts the run on drift; quarantine on shim failure. This is the accepted cost of Approach A.
2. **Time at scale.** 4K HDR Topaz runs well under real-time (~1 hr per 22-min episode on M3 Max-class). A series = days; a whole library = **months of 24/7 GPU**. This is the entire reason for the overnight/always-on design.
3. **Universal preset quality ceiling** (D2) — no per-title tuning.
4. **App-close data-loss risk** — mitigated by whitelist + unsaved-skip + never-force-kill; residual fuzziness on non-document apps.
5. **Version drift** — Topaz updates wipe bundle presets (restore from `~/Documents/Topaz Presets Backup/`); Resolve updates move UI / may change scripting. Self-test guards both.
6. **DV silently dropped at remux** — mitigated by the hard verification gate.

## 10. Tech stack

- Engine: **Python** (orchestrator, stage runners, scheduler, reclaimer). SQLite for state.
- Topaz: bundled ffmpeg + `tvai_up`.
- Resolve: bundled-Python scripting API + a shim helper (`cliclick` / PyObjC / template-match via screenshot).
- Remux: SublerCLI / MP4Box (GPAC); `dovi_tool` on standby.
- GUI: **rumps** (menu bar) + **PyObjC** (countdown) + engine-served **web dashboard**.
- Process management: **launchd** LaunchAgents (engine KeepAlive; GUI user session).

## 11. Open questions / future

- Exact SublerCLI vs MP4Box flag set — pinned during implementation against a real DV sample.
- Plex output container: `.mov` (validated) vs `.mp4` — default `.mov`.
- Whether to expose ProRes HQ-vs-XQ intermediate as a per-run toggle (leaning yes).
- Future Windows/NVIDIA port (TensorRT Topaz + NVENC) — out of scope now; OS-specific code isolated to allow it.

---

*This design is the validated target. Implementation waits on hardware but the engine (stages 1–5) and gating/GUI are buildable and testable on the current laptop today.*
