<sub>[← Visionary README](../README.md) · [Setup](SETUP.md) · [How it works](HOW-IT-WORKS.md) · [YouTube](YOUTUBE.md) · [Configuration](CONFIGURATION.md) · [Known limitations](LIMITATIONS.md)</sub>

# Configuration

`~/.topaz-pipeline/config.json` (never committed; `chmod 600`). Most keys have an env-var
override (env wins); keys marked — have no env override:

| config key | env override | what |
|---|---|---|
| `ftp_hosts` / `ftp_host` | `TOPAZ_NAS_FTP_HOST` | NAS host(s), tried in order (VPN IP first, then LAN name) |
| `ftp_port` | `TOPAZ_NAS_FTP_PORT` | FTP port (default 21) |
| `ftp_user` / `ftp_pass` | `TOPAZ_NAS_FTP_USER` / `_PASS` | FTP credentials |
| `plex_url` / `plex_urls` | `TOPAZ_PLEX_URL` | **optional** (see note below) — Plex server URL(s); defaults to the NAS hosts on :32400 |
| `plex_token` | `TOPAZ_PLEX_TOKEN` | **optional** — your X-Plex-Token |
| `plex_tv_section` / `plex_movie_section` | `TOPAZ_PLEX_SECTION` / `TOPAZ_PLEX_MOVIE_SECTION` | optional — auto-discovered when empty |
| `tmdb_api_key` | `TOPAZ_TMDB_KEY` | optional — richer show metadata |
| `youtarr_url` / `youtarr_user` / `youtarr_pass` | `TOPAZ_YOUTARR_URL` / `_USER` / `_PASS` | optional — YouTube mode |
| `youtarr_archive` | `TOPAZ_YOUTARR_ARCHIVE` | optional — FTP path to youtarr's yt-dlp download archive (`complete.list`); defaults to the UGREEN docker layout `/docker/youtarr/config/complete.list` |
| `youtube_client_id` / `_secret` / `_refresh_token` | — | optional — YouTube subscriptions picker |
| `shuttle_relay_url` | — | optional — [Shuttle](https://github.com/adamkbritsch/shuttle) relay base URL (e.g. `http://nas:8789`); enables the movie **companion combine** |
| `shuttle_relay_token` | — | optional — relay bearer token; normally read from Shuttle's own token file in `~/Library/Application Support/Shuttle/` |
| `media_lib_kinds` | — | optional — per-Plex-library routing overrides (`{section key: tv\|movie\|youtube\|youtube_staging}`); set from Settings → Setup → **Media folders** |
| `media_roots` | `TOPAZ_NAS_FTP_TV(_ROOTS)` / `_MOVIES(_ROOTS)` / `_YOUTUBE` / `_YOUTUBE_STAGING` | optional — the APPLIED media folders (`{kind: [ftp paths]}`), normally written by Plex detection in Setup; env still wins, and with neither the built-in layout applies |
| `comfy_dir` | — | optional — ComfyUI install dir for the **AI border extension**; normally auto-discovered from Comfy Desktop's own `settings.json` |
| `comfy_port` | — | optional — port for Visionary's headless ComfyUI (default **8189**; the Comfy app's own 8188 is never used) |

> **Plex is optional — you don't need it to run this.** Leave the `plex_*` keys blank and
> everything still works. Show and movie names come from your **NAS folder structure**, not
> Plex, and Plex never decides *what* gets upscaled (that's always "a 1080p file with no 4K
> Dolby Vision™ master yet"). The token only enables two best-effort extras:
>
> - **Playback failsafe** — while you're streaming from your Plex server, the background
>   prefetch of upcoming downloads pauses so it can't stutter your playback.
>
> - **Watched-first ordering** — an optional per-show toggle (on by default) processes
>   unwatched episodes before watched ones, plus a "watched" badge in the dashboard. Without
>   Plex it just falls back to plain episode order.
>
> Everything degrades gracefully: no token, or an unreachable Plex, just makes these two
> extras inert — the pipeline carries on.

**Media folders are decided from Plex** (Settings → **Setup** → *Media folders*). Each Plex
library's TYPE sets whether it is processed as TV or Movies, its folder Locations are mapped
onto NAS FTP paths, and every path is **verified over FTP** before it is used — an unresolved
folder is shown and ignored, never guessed at. Every library gets a picker, so the routing is
always visible and overridable: Plex types its own "YouTube" library as a *movie* library, and
a **3D** library is deliberately left unassigned (the deliverable here is a 2D 4K DV master —
opt in explicitly if you want one processed). Press **Use these folders** to apply; the roots
are read at launch, so it takes effect on the next start.

With no Plex — or while it is unreachable — the built-in layout applies unchanged:
`/Media/TV-Shows`, `/Media/Movies`, `/Media/YouTube` (+ `/MediaVolume2`, `/MediaVolume3`
variants for TV and Movies). `TOPAZ_NAS_FTP_TV`, `TOPAZ_NAS_FTP_MOVIES`,
`TOPAZ_NAS_FTP_YOUTUBE` (TV/Movies also take `..._ROOTS` comma-lists) still override
everything, including Plex detection.
