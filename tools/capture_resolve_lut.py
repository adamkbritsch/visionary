#!/usr/bin/env python3
"""Capture Resolve's exact SDR->PQ conversion as a 65^3 LUT (native-DV plan, phase 0).

Fitting the conversion from episode frames got the typical pixel right (median < 1 code)
but missed the tails (p99 16 codes): Resolve's SDR->HDR mapping expands highlights and
treats saturated colours in ways a fitted curve does not pin down. So instead we show
Resolve every colour: a chart of all 65^3 BT.709 R'G'B' grid points, encoded exactly like
Topaz's chunks (prores_videotoolbox HQ, BT.709 tags), assembled and rendered through
Visionary's own persistent DV1000 project with the pipeline's own headless phases
(resolve_pipeline.py setup + render — no Analyze, no screen automation), then read back
patch by patch. Black-box observation of our own Resolve's output; nothing of Resolve is
read or copied.

SAFETY: Resolve is shared with the live pipeline. This refuses to start when Resolve is
already running, when the run thread is in (or within 30 min of) its resolve stage, and
backs up / restores resolve_resume.json around the run. It force-quits Resolve after, the
way the pipeline does (pkill -9; a graceful quit hangs), and un-pins its saved display.

  /usr/bin/python3 tools/capture_resolve_lut.py --work DIR --cube ~/.topaz-pipeline/luts/rcm_sdr709_to_pq1000.cube
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(REPO, "engine"))

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"
ENGINE_PY = "/usr/bin/python3"             # fusionscript needs the system Python (< 3.12)
BUNDLE_ENGINE = os.path.join(REPO, "Visionary.app", "Contents", "Resources", "engine")
RESUME_BOOK = os.path.expanduser("~/.topaz-pipeline/resolve_resume.json")
STATE_URL = "http://127.0.0.1:8765/api/state"

W, H = 3840, 2160
N = 65                                     # grid points per axis
PATCH = 16                                 # px; the centre 8x8 (luma) / 4x4 (chroma) is read
COLS, ROWS = W // PATCH, H // PATCH        # 240 x 135
PER_FRAME = COLS * ROWS                    # 32,400 patches
REPEAT = 6                                 # each chart frame held this long; the middle is read
READ_AT = 3
FPS = "24000/1001"
MODE = "dv1000"
KBPS = 150000
K709, K2020 = (0.2126, 0.0722), (0.2627, 0.0593)


def chart_frames() -> int:
    return -(-N ** 3 // PER_FRAME)          # ceil: 9 frames for 65^3


def grid_rgb() -> np.ndarray:
    """(N^3, 3) R'G'B' in .cube order (red fastest)."""
    g = np.linspace(0.0, 1.0, N)
    b, gg, r = np.meshgrid(g, g, g, indexing="ij")
    return np.stack([r.ravel(), gg.ravel(), b.ravel()], -1)


def rgb_to_ycc10(rgb: np.ndarray, k) -> np.ndarray:
    kr, kb = k
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    y = kr * r + (1 - kr - kb) * g + kb * b
    return np.stack([64 + 876 * y, 512 + 896 * (b - y) / (2 * (1 - kb)),
                     512 + 896 * (r - y) / (2 * (1 - kr))], -1)


def ycc10_to_rgb(ycc: np.ndarray, k) -> np.ndarray:
    kr, kb = k
    y, pb, pr = (ycc[..., 0] - 64) / 876, (ycc[..., 1] - 512) / 896, (ycc[..., 2] - 512) / 896
    r = y + 2 * (1 - kr) * pr
    b = y + 2 * (1 - kb) * pb
    g = (y - kr * r - kb * b) / (1 - kr - kb)
    return np.stack([r, g, b], -1)


def patch_slot(k: int):
    f, s = divmod(k, PER_FRAME)
    return f, s // COLS, s % COLS


def chart_yuv422(frame: int, ycc: np.ndarray) -> bytes:
    """One chart frame as yuv422p10le: patches of grid points frame*PER_FRAME.., mid grey after."""
    Y = np.full((ROWS, COLS), 502.0); U = np.full((ROWS, COLS), 512.0); V = np.full((ROWS, COLS), 512.0)
    lo, hi = frame * PER_FRAME, min(N ** 3, (frame + 1) * PER_FRAME)
    s = np.arange(hi - lo)
    Y.flat[s], U.flat[s], V.flat[s] = ycc[lo:hi, 0], ycc[lo:hi, 1], ycc[lo:hi, 2]
    y = np.repeat(np.repeat(np.rint(Y), PATCH, 0), PATCH, 1)
    u = np.repeat(np.repeat(np.rint(U), PATCH, 0), PATCH // 2, 1)
    v = np.repeat(np.repeat(np.rint(V), PATCH, 0), PATCH // 2, 1)
    return np.concatenate([y.ravel(), u.ravel(), v.ravel()]).clip(0, 1023).astype("<u2").tobytes()


def build_chart(segdir: str) -> int:
    os.makedirs(segdir, exist_ok=True)
    ycc = rgb_to_ycc10(grid_rgb(), K709)
    out = os.path.join(segdir, "seg_0000.mov")
    p = subprocess.Popen([FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                          "-f", "rawvideo", "-pix_fmt", "yuv422p10le", "-s", f"{W}x{H}", "-r", FPS,
                          # the raw chart IS limited range: untagged, ffmpeg assumed full range
                          # and squeezed every code by 876/1023 on the way in (dry-run caught)
                          "-color_range", "tv",
                          "-i", "-",
                          # tag the FRAMES (ffmpeg 8 writes the container's colour from them):
                          # Topaz's chunks carry BT.709 primaries/transfer/matrix, and Resolve's
                          # automatic colour management reads exactly these
                          "-vf", "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709",
                          "-c:v", "prores_videotoolbox", "-profile:v", "hq",
                          "-color_range", "tv", "-pix_fmt", "p210le", "-allow_sw", "1",
                          "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                          out], stdin=subprocess.PIPE)
    frames = 0
    for f in range(chart_frames()):
        buf = chart_yuv422(f, ycc)
        for _ in range(REPEAT):
            p.stdin.write(buf); frames += 1
    p.stdin.close()
    if p.wait() != 0:
        raise SystemExit("chart encode failed")
    with open(os.path.join(segdir, "segments.json"), "w") as fh:
        json.dump({"fps": 24000 / 1001, "total_frames": frames,
                   "segments": [{"file": "seg_0000.mov", "start": 0, "frames": frames}]}, fh, indent=1)
    return frames


def decode_frames(path: str, pix_fmt: str, wanted: list):
    """{index: raw bytes} for the wanted frame indices of a video (one sequential decode)."""
    size = W * H * (2 if pix_fmt == "yuv422p10le" else 1.5) * 2
    size = int(size)
    p = subprocess.Popen([FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", path,
                          "-map", "0:v:0", "-pix_fmt", pix_fmt, "-f", "rawvideo", "-"],
                         stdout=subprocess.PIPE)
    out, i, want = {}, 0, set(wanted)
    while want - set(out):
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        if i in want:
            out[i] = buf
        i += 1
    p.stdout.close(); p.kill(); p.wait()
    return out


def read_patches(raw: bytes, chroma_w: int, chroma_h: int) -> np.ndarray:
    """(ROWS*COLS, 3) centre means (luma 8x8, chroma 4x4) of every patch of one frame."""
    a = np.frombuffer(raw, dtype="<u2").astype(np.float64)
    n, q = W * H, chroma_w * chroma_h
    y = a[:n].reshape(H, W); u = a[n:n + q].reshape(chroma_h, chroma_w); v = a[n + q:n + 2 * q].reshape(chroma_h, chroma_w)
    yc = y.reshape(ROWS, PATCH, COLS, PATCH)[:, 4:12, :, 4:12].mean(axis=(1, 3))
    cy, cx = chroma_h // ROWS, chroma_w // COLS            # chroma patch size (16x8 or 8x8)
    uc = u.reshape(ROWS, cy, COLS, cx)[:, cy // 4:cy - cy // 4, :, cx // 4:cx - cx // 4].mean(axis=(1, 3))
    vc = v.reshape(ROWS, cy, COLS, cx)[:, cy // 4:cy - cy // 4, :, cx // 4:cx - cx // 4].mean(axis=(1, 3))
    return np.stack([yc.ravel(), uc.ravel(), vc.ravel()], -1)


def state() -> dict:
    with urllib.request.urlopen(STATE_URL, timeout=5) as r:
        return json.load(r)


def resolve_running() -> bool:
    return subprocess.run(["pgrep", "-f", "DaVinci Resolve.app/Contents/MacOS/Resolve"],
                          capture_output=True).returncode == 0


def safe_to_start(min_eta_s: int = 1800):
    if resolve_running():
        return False, "Resolve is already running"
    try:
        o = state().get("orchestrator") or {}
    except Exception as e:  # noqa: BLE001
        return False, f"cannot read Visionary's state: {e}"
    stage = o.get("stage")
    eta = (o.get("progress") or {}).get("eta_secs")
    if stage == "resolve":
        return False, "the run thread is in its resolve stage"
    if stage != "topaz" or not eta or eta < min_eta_s:
        return False, f"run thread stage={stage!r} eta={eta!r}: not a long enough non-Resolve window"
    return True, f"run thread in topaz for ~{int(eta / 60)} more min"


def kill_resolve():
    subprocess.run(["pkill", "-9", "-f", "DaVinci Resolve.app"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import resolve_prefs
        end = time.time() + 10
        while resolve_prefs.resolve_running() and time.time() < end:
            time.sleep(0.25)
        resolve_prefs.unpin()
    except Exception:  # noqa: BLE001
        pass


def run_resolve(segdir: str, out: str, log) -> int:
    py = os.path.join(BUNDLE_ENGINE, "resolve_pipeline.py")
    for args in (["setup", segdir, MODE], ["render", out, MODE, str(KBPS)]):
        p = subprocess.run([ENGINE_PY, py] + args, capture_output=True, text=True, errors="replace",
                           timeout=1800)
        log.write(f"$ resolve_pipeline.py {' '.join(args)}\nrc={p.returncode}\n{p.stdout}\n{p.stderr}\n")
        log.flush()
        if p.returncode != 0:
            return p.returncode
    return 0


def capture(work: str, cube: str, *, force: bool = False) -> dict:
    ok, why = safe_to_start()
    print(("OK: " if ok else "REFUSED: ") + why, flush=True)
    if not ok and not force:
        raise SystemExit(2)
    segdir = os.path.join(work, "chart")
    frames = build_chart(segdir)
    render = os.path.join(work, "chart_resolve.mov")
    if os.path.exists(render):
        os.remove(render)
    backup = os.path.join(work, "resolve_resume.json.bak")
    if os.path.exists(RESUME_BOOK):
        shutil.copyfile(RESUME_BOOK, backup)
    t0 = time.time()
    try:
        with open(os.path.join(work, "resolve.log"), "w") as log:
            rc = run_resolve(segdir, render, log)
    finally:
        kill_resolve()
        if os.path.exists(backup):
            shutil.copyfile(backup, RESUME_BOOK)
    if rc != 0 or not os.path.exists(render):
        raise SystemExit(f"Resolve pass failed (rc={rc}); see {work}/resolve.log")
    print(f"Resolve pass done in {time.time() - t0:.0f}s", flush=True)
    return read_back(segdir, render, cube, frames)


def read_back(segdir: str, render: str, cube: str, frames: int) -> dict:
    wanted = [f * REPEAT + READ_AT for f in range(chart_frames())]
    src = decode_frames(os.path.join(segdir, "seg_0000.mov"), "yuv422p10le", wanted)
    res = decode_frames(render, "yuv420p10le", wanted)
    if len(src) != len(wanted) or len(res) != len(wanted):
        raise SystemExit(f"decoded {len(src)}/{len(res)} of {len(wanted)} chart frames")
    intended = rgb_to_ycc10(grid_rgb(), K709)
    out_rgb = np.zeros((N ** 3, 3))
    in_err = []
    for f, idx in enumerate(wanted):
        lo, hi = f * PER_FRAME, min(N ** 3, (f + 1) * PER_FRAME)
        s = read_patches(src[idx], W // 2, H)[: hi - lo]
        r = read_patches(res[idx], W // 2, H // 2)[: hi - lo]
        in_err.append(np.abs(s - np.rint(intended[lo:hi])).max(axis=1))
        out_rgb[lo:hi] = ycc10_to_rgb(r, K2020)
    in_err = np.concatenate(in_err)
    out_rgb = np.clip(out_rgb, 0, 1)
    cube = os.path.expanduser(cube)
    os.makedirs(os.path.dirname(cube), exist_ok=True)
    with open(cube + ".part", "w") as fh:
        fh.write('TITLE "Visionary: Resolve DV1000 SDR709->PQ, captured from a 65^3 chart render"\n')
        fh.write(f"LUT_3D_SIZE {N}\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n")
        np.savetxt(fh, out_rgb, fmt="%.6f")
    os.replace(cube + ".part", cube)
    return {"cube": cube, "chart_frames": frames,
            "chart_input_error_codes": {"median": float(np.median(in_err)), "max": float(in_err.max())},
            "white_out_pq": out_rgb[-1].round(4).tolist(), "black_out_pq": out_rgb[0].round(4).tolist()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--cube", required=True)
    ap.add_argument("--read-only", action="store_true", help="re-read an existing render, no Resolve")
    a = ap.parse_args(argv)
    os.makedirs(a.work, exist_ok=True)
    if a.read_only:
        rep = read_back(os.path.join(a.work, "chart"), os.path.join(a.work, "chart_resolve.mov"),
                        a.cube, chart_frames() * REPEAT)
    else:
        rep = capture(a.work, a.cube)
    print(json.dumps(rep, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
