#!/usr/bin/env python3
"""OPTIONAL NAS-side helper: probe TV episodes + movies for Dolby Vision and write manifests
{basename: 0/1} that Visionary reads over FTP (at <media root>/Config/dv_manifests/) to find
titles still lacking DV. Without these manifests the app falls back to filename marks
("HDR10 DV" in the name) — which works, just less precisely for pre-existing DV content.

Per-series manifests for TV, one flat __movies__.json for Movies. Incremental — only probes
files not already recorded (a file's DV status never changes). Run it on the NAS (needs
ffprobe + direct filesystem access), e.g. nightly via cron:
    0 5 * * * /usr/bin/python3 /volume1/Media/Config/dv_probe.py all

Roots are env-overridable for non-UGOS layouts:
    DV_PROBE_TV_ROOTS=/volume1/Media/TV-Shows,/volume2/.../TV-Shows
    DV_PROBE_MOVIE_ROOTS=/volume1/Media/Movies
    DV_PROBE_OUT=/volume1/Media/Config/dv_manifests
"""
import json, os, subprocess, sys


def _roots(env, default):
    v = os.environ.get(env)
    return [p for p in v.split(",") if p] if v else default


# Defaults match a UGREEN UGOS multi-volume layout; override via env for anything else.
TV_ROOTS    = _roots("DV_PROBE_TV_ROOTS",
                     ["/volume1/Media/TV-Shows", "/volume2/MediaVolume2/TV-Shows",
                      "/volume3/MediaVolume3/TV-Shows"])
MOVIE_ROOTS = _roots("DV_PROBE_MOVIE_ROOTS",
                     ["/volume1/Media/Movies", "/volume2/MediaVolume2/Movies",
                      "/volume3/MediaVolume3/Movies"])
OUT         = os.environ.get("DV_PROBE_OUT", "/volume1/Media/Config/dv_manifests")
VID         = (".mp4", ".mkv", ".mov", ".m4v")


def probe(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream_side_data=dv_profile", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=120).stdout.strip()
        return 1 if out else 0
    except Exception:
        return 0


def _build(root_dirs, manifest_name, label):
    os.makedirs(OUT, exist_ok=True)
    mf = os.path.join(OUT, manifest_name)
    try:
        old = json.load(open(mf))
    except Exception:
        old = {}
    cur, new = {}, 0
    for root_dir in root_dirs:
        if not os.path.isdir(root_dir):
            continue
        for root, _, files in os.walk(root_dir):
            for f in files:
                if f.lower().endswith(VID) and f not in cur:
                    if f in old:
                        cur[f] = old[f]
                    else:
                        cur[f] = probe(os.path.join(root, f))
                        new += 1
    json.dump(cur, open(mf, "w"))
    print(f"{label}: {len(cur)} files ({new} new probed), {sum(cur.values())} with DV")


def do_series(series):
    dirs = [os.path.join(r, series) for r in TV_ROOTS if os.path.isdir(os.path.join(r, series))]
    if dirs:
        _build(dirs, series + ".json", series)


def do_movies():
    _build(MOVIE_ROOTS, "__movies__.json", "movies")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    if arg in ("all", "movies"):
        do_movies()
    if arg != "movies":
        if arg == "all":
            names = set()
            for r in TV_ROOTS:
                if os.path.isdir(r):
                    names.update(s for s in os.listdir(r)
                                 if os.path.isdir(os.path.join(r, s)) and not s.startswith("."))
            targets = sorted(names)
        else:
            targets = [arg]
        for s in targets:
            do_series(s)
