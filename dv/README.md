# dv/ — the pinned Dolby Vision toolchain files

The native Dolby Vision step needs two files Homebrew can't provide. Both are pinned by sha256
in `engine/versions.py` and checked by preflight's `native_dv` row. The app installs them
itself: at launch, a missing one is copied to its path, and **Settings → Setup → Dolby Vision
toolchain → Install** also replaces one that is not the pinned file. From a clone, run
`python3 engine/dvinstall.py` (add `--repair` to replace a mismatched file).

| file | installed to | sha256 |
|---|---|---|
| `hdr_analyzer_mvp` | `~/.topaz-pipeline/bin/hdr_analyzer_mvp` | `41727312f4a960b27d59cb0c0f24c2922dc82768da3503f083e4842a87fc099e` |
| `rcm_sdr709_to_pq1000.cube` | `~/.topaz-pipeline/luts/rcm_sdr709_to_pq1000.cube` | `e410e5b7b899a07096a77e2f65bdeb6a4ab3cc60126358a81c8050251d8c59b0` |

## hdr_analyzer_mvp

A build of [tinof/hdr-analyze](https://github.com/tinof/hdr-analyze) at commit
[`383b0ad`](https://github.com/tinof/hdr-analyze/tree/383b0ad258a0a2b25bb41e77200281685c6a5068)
(`cargo build --release -p hdr_analyzer_mvp`), for Apple silicon. It measures the PQ picture
shot by shot: the per-scene brightness the Dolby Vision metadata is written from. It is MIT
licensed — see [`LICENSE.hdr-analyze`](LICENSE.hdr-analyze); the source is at the commit above.

It links Homebrew's `ffmpeg` formula, major version 8, at `/opt/homebrew/opt/ffmpeg/lib`
(libavcodec 62, libavformat 62, libavutil 60, libswscale 9). That is why ffmpeg is pinned: an
upgrade to ffmpeg 9 moves those libraries and the analyzer no longer starts.

## rcm_sdr709_to_pq1000.cube

The colour conversion every SDR source goes through: a 65×65×65 3D LUT from BT.709 SDR to
BT.2020 PQ at a 1000-nit mastering level, applied by ffmpeg (`lut3d`, tetrahedral). It was
captured once from a 1000-nit colour-managed render of an identity cube. Any other file
changes the picture, so it is never replaced by anything but this one.
