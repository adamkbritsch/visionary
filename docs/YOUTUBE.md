<sub>[← Visionary README](../README.md) · [Setup](SETUP.md) · [How it works](HOW-IT-WORKS.md) · [YouTube](YOUTUBE.md) · [Configuration](CONFIGURATION.md) · [Known limitations](LIMITATIONS.md)</sub>

# YouTube

Optional, and entirely driven through [youtarr](https://github.com/DialmasterOrg/Youtarr) on
the NAS. Without youtarr the YouTube tab simply stays off.

A YouTube video is by far the quickest and lightest thing Visionary processes. It skips
Topaz, and its Dolby Vision™ render usually ships without a re-encode, so it takes minutes and a
few gigabytes where an episode takes hours and a large share of the scratch disk:

| | typical length | typical time to finish | per minute of content | estimated peak scratch space |
|---|---|---|---|---|
| YouTube video | 8 min | **13 min** | 1.4 min | **3–7 GB** |
| TV episode | 32 min | **3 h 25 min** | 6.7 min | **~160 GB** |
| Movie, 1080p upscaled to 4K | 1 h 50 min | **8 h 10 min** | 5.0 min | **~550 GB** |

<sub>Medians from Visionary's own log for everything finished between 15 July and 16 September
2026: 351 videos, 309 episodes and 8 movies. Time runs from an item's first GPU stage to its
master landing on the NAS, minus any time the pipeline was stopped. These are real-world
figures, so they include waiting on other items, which is why they run longer than the
dedicated-machine figures quoted in [How it works](HOW-IT-WORKS.md).
4K fast-path movies are too few so far to quote.</sub>

<sub>Peak space is estimated from measured rates and scales with runtime. An episode or movie
peaks at the end of its Dolby Vision™ step, while Topaz's ProRes intermediate (about 4.4 GB per
minute of content) and the Dolby Vision™ render (about 0.45 GB per minute) exist together; the
intermediate is deleted right after, so the remux needs only tens of GB. A YouTube video writes
no ProRes. It peaks at the final mux, when the source, its frame-rate copy, the render, a
stream copy of it and the master all exist at once. The range depends on whether the frame-rate
copy had to be re-encoded, which grows it to about 0.55 GB per minute. A long video grows the
same way: an 88-minute 4K interview needs about 80 GB. A 4K fast-path movie measured 318 GB.</sub>

- **youtarr only has to EXIST.** Run the container; Visionary does the rest — it discovers
  the URL on your NAS, and once you've entered its login (youtarr requires auth, so that one
  credential is unavoidable) it configures youtarr itself. Several of youtarr's own settings
  are a contract Visionary depends on — where downloads land (the folder split that keeps raw 1080p out of your Plex
  library), the `.nfo`/poster/thumbnail/subtitle sidecars it copies beside each master, and
  whether youtarr auto-downloads at all. A wrong one would fail quietly, so Setup applies
  them the moment youtarr connects and shows each one with a Recheck/Apply. That includes **where downloads land**: the same
  folder has three names (youtarr's host path, Visionary's FTP path, Plex's container path),
  so the prefix is *observed* from youtarr's own current value rather than guessed — a
  directory pointing at your Plex library gets retargeted to staging, the folder is created
  first if missing, and a path matching nothing recognisable is reported instead of
  overwritten with an invented one.

- **The fast route.** A video skips Topaz; the Dolby Vision™ render scales it (a Lanczos fit
  into 3840×2160 — in a blind comparison on the TV it looked the same as Apple's MetalFX
  upscaler) and renders the master at a bitrate chosen so its **one-second peaks land under
  the playback cap** — 20 Mbps, or 14 for a native 4K source, whose detail bursts harder.
  The remux can then ship that render **stream-copied** in minutes instead of spending an
  hour on the capped x265 pass. A VideoToolbox export has no peak control, so this is a
  measured gamble: the peak is checked right after the render, and a render that busts the
  cap is **re-exported at a lower target calculated from the burst it just showed** (up to
  twice, never below 8 Mbps). Only a video that still misses takes the capped x265 pass.

- **SponsorBlock cuts stay in sync.** When youtarr removes SponsorBlock segments, yt-dlp cuts
  the download without re-encoding, so each kept stretch carries the sound from its keyframe up
  to the cut, stamped on top of the join. A player follows those stamps; mkvmerge, which muxes
  every master, would count them in and play the rest of the video late by up to a few seconds.
  Visionary drops exactly those packed packets as it reads the audio (copy and loudness boost
  alike, every other packet bit-exact), and the log says how many joins it took out. Only that
  pattern is touched — jitter in DVD or old AVI audio is left to mkvmerge, which gets it right.

- **Channels.** Subscribe from your own YouTube subscriptions, or paste a channel link.
  Each channel has a **scope** (most popular or all), a **max age**, its own output mode,
  loudness setting and pause. Visionary reads a channel's whole back catalogue — videos and
  live streams, **never Shorts** (see below) — not just the 50 newest youtarr indexes on its own, and
  videos round-robin across channels. A **newly added channel gets its first two videos
  fast**: they are requested from youtarr the moment you add it and take the next YouTube
  slots (never interrupting TV), and after that it takes ordinary turns. Removing a channel
  deletes its downloads **and its finished 4K masters** from the NAS, and forgets youtarr's
  download record so re-adding it downloads fresh.

- **Shorts are never processed.** However a video arrives — a channel's most-popular picks,
  a new channel's first videos, a pasted link, a playlist, a send — Visionary asks YouTube
  once whether it is a Short (`youtube.com/shorts/<id>` serves a Short and redirects anything
  else) and keeps the answer; a Short is never requested, never upscaled, and a pasted or
  sent one says so. A video longer than three minutes is never asked about: no Short is.

- **Fetch-ahead** keeps the NAS stocked. Left to its own schedule, youtarr can leave the
  upscale queue empty while the pipeline sits idle, so Visionary asks it for whatever each
  channel is short of — newest first, respecting the channel's scope and max-age — to keep
  `youtube_fetch_ahead` (default 12) videos staged per channel. Set it to 0 to rely on
  youtarr's own schedule.

- **Links and Send to Visionary.** Paste a video, playlist or channel link into the YouTube
  tab; a confirm step shows real titles and counts first, and a `watch?v=…&list=…` link asks
  which one you meant. A companion app can do the same through `POST /api/send-to-visionary`,
  which advertises what it accepts in `/api/state`. A **sent video jumps the queue**; imported
  playlists and videos join the normal cadence, keep playlist order, and each import has
  its own output mode, loudness setting and pause, like a channel. Every video is filed under **its own uploader**, never
  under the person who compiled the playlist, so a playlist author's name only appears if
  it is part of the playlist's title.

- **Run a video now**: any pending YouTube video can jump the whole queue from its up-next
  row (the ↥ button) — cadence-exempt and ahead of due movies. The item processing
  **finishes its current segment first**, exactly like a deploy does, then yields and
  resumes afterwards — so no encoding work is thrown away. A Dolby Vision™ render that is
  already encoding runs to its end first (that pass is not resumable). A download in
  progress does stop for it, and is
  pulled again afterwards. The row says **running next** the moment you press it, and
  only rows you activated this way, or imported as single videos, ever carry that label;
  the pipeline card names what it is waiting for. Consecutive videos collapse into **one
  expandable slot** in the queue, so a burst of them doesn't bury the shows and movies
  behind it. The queue always holds **ten TV episodes** — movies and videos ride along
  between them without consuming that count, and only the episodes are numbered.

- **Cleaning up.** **Skip & delete** on any video row — subscribed, imported or sent —
  removes the download from the NAS and makes sure it is never downloaded or queued again. Dropping a
  playlist or removing a channel also clears its videos' working files from the local
  scratch disk; a background pass catches anything left behind, and only ever removes a
  file it has proven belongs to a YouTube video nothing still needs.
