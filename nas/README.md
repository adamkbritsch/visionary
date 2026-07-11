# NAS-side helper (optional)

## dv_probe.py — Dolby Vision manifests

Visionary decides which titles still need upscaling by checking each file for Dolby
Vision. Two sources, in order:

1. **DV manifests** (this script): `{basename: 0/1}` JSON files under
   `<media root>/Config/dv_manifests/`, written on the NAS by `dv_probe.py`
   (per-series for TV + one `__movies__.json`). Precise — actual ffprobe results.
2. **Filename marks** (automatic fallback): a file whose name contains `HDR10 DV`
   counts as done. This is what Visionary itself names its finished masters, so the
   pipeline works fine without the manifests — they only add precision for DV content
   that arrived from elsewhere (bought/downloaded already-DV files).

**Install (optional):** copy `dv_probe.py` anywhere on the NAS (e.g.
`/volume1/Media/Config/`), make sure `ffprobe` is installed there, and schedule it:

```
0 5 * * * /usr/bin/python3 /volume1/Media/Config/dv_probe.py all
```

Defaults assume a UGREEN UGOS multi-volume layout (`/volume1/Media/...`,
`/volume2/MediaVolume2/...`); override with `DV_PROBE_TV_ROOTS`,
`DV_PROBE_MOVIE_ROOTS`, `DV_PROBE_OUT` (comma-separated lists) for anything else.
