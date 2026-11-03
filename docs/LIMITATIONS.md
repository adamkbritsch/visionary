<sub>[← Visionary README](../README.md) · [Setup](SETUP.md) · [How it works](HOW-IT-WORKS.md) · [YouTube](YOUTUBE.md) · [Configuration](CONFIGURATION.md) · [Known limitations](LIMITATIONS.md)</sub>

# Known limitations

- One software target (see the requirements box in the [README](../README.md)) — by design, not laziness: the Topaz
  build and the Dolby Vision™ toolchain are pinned because the output was judged on them.

- **AI border extension is slow and exclusive.** Hours per episode on top of the normal
  run, and while it works nothing else does — remuxes are suspended (a [helper](HELPER.md)'s
  segment in flight is stopped and redone instead), uploads and cleanup wait, the prefetcher
  stands down. It is chunked and resumable, off by default, and only
  offered on shows that measure 4:3. SDR only. For continuity, chunk boundaries **snap to
  scene cuts** (a wing reset at an edit is invisible) and every chunk within a scene shares
  its seed — but a boundary that must land mid-scene can still shift the generated borders
  slightly, and invented content is never persistent across shots or episodes. A per-show
  **wing prompt** (on the show's settings card) gently biases what the wings contain.

- **An overloaded NAS slows Visionary down, it doesn't stop it or make it fail.** A NAS that
  has run out of memory still answers, but its logins can take minutes. When that happens
  Visionary says so ("NAS overloaded — a login took 138 s"), logs in **one connection at a
  time** instead of retrying from every thread, and never counts the delay against an
  episode or a movie. The download-ahead and the queue refresh wait it out, and normal
  service resumes on its own once a login is quick again. Transfers use the Ethernet even
  when the NAS's network name stops resolving: Tailscale's signed reply from the NAS shows
  that its LAN address really belongs to it.

- **Enormous working scratch.** The finished master is small (~1.4 GB), but *getting
  there* is not: Topaz's 4K ProRes intermediate is near-lossless and scales with runtime —
  about **4.1 GiB (4.4 GB) per minute of content**, so a
  43-minute episode holds roughly **180 GiB (~192 GB)** and a 2-hour film about
  **500 GiB**. That intermediate is **deleted the
  moment the Dolby Vision™ render is made** — the remux only needs the DV render plus the
  original (a TV episode whose Dolby Vision™ is analysed straight from the chunks keeps them
  until its remux passes) — so the item being remuxed alongside the next upscale carries just ~10 GB, and
  the dual pipeline never doubles the peak. A **4K fast-path movie** peaks higher than
  anything with a ProRes stage: its source, CFR copy, DV render, two stream-copy
  transients and the growing master all coexist at the final mux — **live-measured
  317.7 GB** for a 56 GB REMUX. Budget generously: the pipeline keeps a **400 GB
  free-space floor** before starting an item — enough for any episode, but **not** for the
  ~550 GB peak of an upscaled feature film — so plan for a fast SSD with ~1 TB free if you
  queue movies (a 2 TB SSD is comfortable). Estimated peaks for each kind of item are in
  the [YouTube](YOUTUBE.md) section's table.

- **The Helper has firm limits.** The optional [Linux helper](HELPER.md) is one machine, and
  its x265 must report **exactly** the Mac's version: after Homebrew upgrades the Mac's x265,
  the helper sits out until Setup's **Build** makes the matching version (the Mac notices the
  new x265 without a restart). It needs **Ethernet at both ends** and an IPv4 address (Wi-Fi
  is refused, however good, including a stream that arrives over the helper's Wi-Fi). Its GPU
  rule counts programs one by one on NVIDIA (`nvidia-smi pmon`) and on AMD and Intel i915/xe
  (the kernel's per-program counters), and there it leaves out the desktop itself and the
  Steam client. Where there is no per-program reading (nouveau, an older kernel, or `pmon`
  failing), the whole GPU counts, desktop included, so a busy desktop alone can pause it; a
  game is caught by the game rule either way. Over ssh most desktops won't let the worker
  hold off sleep, so turn off the helper's automatic sleep while it's plugged in; one that
  sleeps mid-segment just hands that segment back. Only the capped x265 re-encode is shared:
  Topaz, the Dolby Vision™ analysis and render, the over-gate splice's pieces and the peak
  repair's re-encodes always run on the Mac, which also still decodes and colour-converts
  every segment the helper encodes. At the end of an item the Mac waits for the helper's last
  segment rather than redoing it, so a helper much slower than the Mac can cost time there.

- Replaces originals **by default**: once the uploaded 4K DV master size-verifies on the
  NAS, the superseded 1080p source is deleted — but the per-show/movie **"Replaces
  source"** setting can instead keep the source beside the master (Plex merges them
  into one item with two versions and serves the 4K, and the source stays re-runnable
  by future, better upscale models). Keep backups if you want a way back regardless.
