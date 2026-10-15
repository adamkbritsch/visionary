#!/usr/bin/env python3
"""Capture Resolve's SDR->PQ conversion as a 3D LUT, from Resolve's own renders (gate Q0).

Resolve's colour-managed project turns Topaz's BT.709 SDR ProRes into BT.2020 PQ. The native
DV path needs the SAME picture without Resolve, so this tool learns the conversion from frame
pairs Visionary already has — the Topaz ProRes frame and Resolve's render of the same frame
index — then writes a 65^3 .cube for ffmpeg (dvnative.lut_vf) and checks it end to end through
the exact ffmpeg chain the encoder uses. Black-box observation of our own renders; nothing
of Resolve is read or copied.

Model (fitted, then verified): R'G'B' 709 -> linear by a power `gamma` -> scale so SDR white is
`white` nits -> BT.709->BT.2020 primaries -> PQ. Only flat image areas are used for the fit
(chroma subsampling and Resolve's own compression blur edges).

  python3 tools/resolve_lut.py fit   --pairs DIR --out rcm.json
  python3 tools/resolve_lut.py write --model rcm.json --cube ~/.topaz-pipeline/luts/rcm_sdr709_to_pq1000.cube
  python3 tools/resolve_lut.py check --pairs DIR --cube CUBE

DIR holds plan.json ({"frames": [{"global": i, ...}]}), src/fNNNNNN.yuv (yuv422p10le, Topaz)
and res/fNNNNNN.yuv (yuv420p10le, Resolve), 3840x2160.
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "engine"))
import dvnative  # noqa: E402

W, H = 3840, 2160
M709_2020 = np.array([[0.6274, 0.3293, 0.0433], [0.0691, 0.9195, 0.0114], [0.0164, 0.0880, 0.8956]])
K709, K2020 = (0.2126, 0.0722), (0.2627, 0.0593)
CUBE_SIZE = 65


# ---- colour maths (pure) ----------------------------------------------------------------

def ycc_to_rgb(y, cb, cr, k):
    kr, kb = k
    yy, pb, pr = (y - 64) / 876, (cb - 512) / 896, (cr - 512) / 896
    r = yy + 2 * (1 - kr) * pr
    b = yy + 2 * (1 - kb) * pb
    g = (yy - kr * r - kb * b) / (1 - kr - kb)
    return np.stack([r, g, b], -1)


def rgb_to_ycc(rgb, k):
    kr, kb = k
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    y = kr * r + (1 - kr - kb) * g + kb * b
    return np.stack([64 + 876 * y, 512 + 896 * (b - y) / (2 * (1 - kb)),
                     512 + 896 * (r - y) / (2 * (1 - kr))], -1)


def pq_encode(nits):
    m1, m2, c1, c2, c3 = 2610 / 16384, 2523 / 32, 3424 / 4096, 2413 / 128, 2392 / 128
    yv = np.clip(nits / 10000.0, 0, 1) ** m1
    return ((c1 + c2 * yv) / (1 + c3 * yv)) ** m2


def model_rgb(rgb709, white: float, gamma: float):
    """R'G'B' 709 (0..1) -> R'G'B' BT.2020 PQ (0..1)."""
    lin = np.clip(rgb709, 0, 1) ** gamma
    return pq_encode(white * lin @ M709_2020.T)


# ---- frame pairs ------------------------------------------------------------------------

def load422(p):
    a = np.fromfile(p, dtype="<u2").astype(np.float64)
    n = W * H
    return a[:n].reshape(H, W), a[n:n + n // 2].reshape(H, W // 2), a[n + n // 2:].reshape(H, W // 2)


def load420(p):
    a = np.fromfile(p, dtype="<u2").astype(np.float64)
    n, q = W * H, W * H // 4
    return a[:n].reshape(H, W), a[n:n + q].reshape(H // 2, W // 2), a[n + q:].reshape(H // 2, W // 2)


def _blocks(y):
    b = y.reshape(H // 2, 2, W // 2, 2)
    return b.mean(axis=(1, 3)), b.max(axis=(1, 3)) - b.min(axis=(1, 3))


def flat_samples(pairs_dir: str, stride: int = 7):
    """(src YCbCr, res YCbCr) on the 1920x1080 chroma grid, flat areas only."""
    plan = json.load(open(os.path.join(pairs_dir, "plan.json")))
    xs, ys = [], []
    for fr in plan["frames"]:
        g = fr["global"]
        rp = os.path.join(pairs_dir, "res", f"f{g:06d}.yuv")
        if not os.path.exists(rp):
            continue
        sy, su, sv = load422(os.path.join(pairs_dir, "src", f"f{g:06d}.yuv"))
        ry, ru, rv = load420(rp)
        sy2, srng = _blocks(sy)
        ry2, rrng = _blocks(ry)
        su2 = su.reshape(H // 2, 2, W // 2).mean(1)
        sv2 = sv.reshape(H // 2, 2, W // 2).mean(1)
        flat = (srng <= 3) & (rrng <= 6)
        flat[:, 1:] &= (np.abs(np.diff(su2, axis=1)) <= 3) & (np.abs(np.diff(sv2, axis=1)) <= 3)
        idx = np.flatnonzero(flat)[::stride]
        xs.append(np.stack([sy2.ravel()[idx], su2.ravel()[idx], sv2.ravel()[idx]], -1))
        ys.append(np.stack([ry2.ravel()[idx], ru.ravel()[idx], rv.ravel()[idx]], -1))
    if not xs:
        raise SystemExit("no frame pairs found")
    return np.concatenate(xs), np.concatenate(ys)


def residuals(src_ycc, res_ycc, white, gamma):
    pred = rgb_to_ycc(model_rgb(ycc_to_rgb(*src_ycc.T, K709), white, gamma), K2020)
    return pred - res_ycc


SEARCH_SAMPLES = 200_000          # the grid search runs on a fixed random subset (seeded);
                                  # the reported residuals use every sample


def fit(pairs_dir: str) -> dict:
    x_all, y_all = flat_samples(pairs_dir)
    pick = np.random.default_rng(0).permutation(len(x_all))[:SEARCH_SAMPLES]
    x, y = x_all[pick], y_all[pick]
    best = None
    for gamma in np.arange(1.8, 2.81, 0.05):
        for white in (100, 150, 203, 250, 300, 400, 500, 600, 700, 800, 900, 1000):
            e = np.median(np.abs(residuals(x, y, white, gamma)[:, 0]))
            if best is None or e < best[0]:
                best = (e, gamma, white)
    _, g0, w0 = best
    for gamma in np.arange(g0 - 0.05, g0 + 0.0501, 0.005):
        for white in np.arange(max(50, w0 - 60), w0 + 60.1, 2):
            e = np.median(np.abs(residuals(x, y, white, gamma)[:, 0]))
            if e < best[0]:
                best = (e, gamma, white)
    _, gamma, white = best
    x, y = x_all, y_all
    d = np.abs(residuals(x, y, white, gamma))
    stats = {ch: {"median": float(np.median(d[:, i])), "p95": float(np.percentile(d[:, i], 95)),
                  "p99": float(np.percentile(d[:, i], 99))} for i, ch in enumerate(("Y", "Cb", "Cr"))}
    # neutral transfer as data, so a non-power curve (a soft clip, a tone map) is visible
    grey = (np.abs(x[:, 1] - 512) < 4) & (np.abs(x[:, 2] - 512) < 4)
    curve = []
    for lo in range(64, 960, 32):
        m = grey & (x[:, 0] >= lo) & (x[:, 0] < lo + 32)
        if m.sum() >= 50:
            curve.append([lo + 16, float(np.median(y[m, 0])), int(m.sum())])
    return {"gamma": float(gamma), "white": float(white), "samples": int(len(x)),
            "residual_codes": stats, "neutral_curve": curve}


# ---- .cube ------------------------------------------------------------------------------

def write_cube(path: str, white: float, gamma: float, size: int = CUBE_SIZE):
    """Adobe .cube, red fastest (ffmpeg lut3d's order). Input domain 0..1 R'G'B' 709."""
    g = np.linspace(0.0, 1.0, size)
    b, gg, r = np.meshgrid(g, g, g, indexing="ij")              # r varies fastest
    rgb = np.stack([r.ravel(), gg.ravel(), b.ravel()], -1)
    out = np.clip(model_rgb(rgb, white, gamma), 0, 1)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    part = path + ".part"
    with open(part, "w") as f:
        f.write(f"TITLE \"Visionary: Resolve SDR709->PQ1000 (gamma {gamma:.3f}, white {white:.0f} nits)\"\n")
        f.write(f"LUT_3D_SIZE {size}\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n")
        np.savetxt(f, out, fmt="%.6f")
    os.replace(part, path)


# ---- end-to-end check through the real ffmpeg chain -------------------------------------

def build_chain_command(src_raw: str, cube: str) -> list:
    """A saved raw Topaz frame (yuv422p10le, 3840x2160) through dvnative.lut_vf -> yuv420p10le
    on stdout: the exact filter chain the native encode uses."""
    return [dvnative.FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "yuv422p10le", "-s", f"{W}x{H}", "-i", src_raw,
            "-vf", dvnative.lut_vf(cube), "-f", "rawvideo", "-pix_fmt", "yuv420p10le", "-"]


def chain_frame(src_raw: str, cube: str) -> np.ndarray:
    r = subprocess.run(build_chain_command(src_raw, cube), capture_output=True, check=True)
    return np.frombuffer(r.stdout, dtype="<u2").astype(np.float64)


def block_means(plane, bs=8):
    h, w = plane.shape
    return plane[:h - h % bs, :w - w % bs].reshape(h // bs, bs, w // bs, bs).mean(axis=(1, 3))


def check(pairs_dir: str, cube: str) -> dict:
    """Gate Q0: our chain vs Resolve's render on 8x8 block means (cancels both encoders' noise,
    keeps any real colour shift). Pass: |dY| p99 <= 2, |dCb|,|dCr| p99 <= 2, medians <= 1."""
    plan = json.load(open(os.path.join(pairs_dir, "plan.json")))
    acc = {"Y": [], "Cb": [], "Cr": []}
    for fr in plan["frames"]:
        g = fr["global"]
        rp = os.path.join(pairs_dir, "res", f"f{g:06d}.yuv")
        if not os.path.exists(rp):
            continue
        ours = chain_frame(os.path.join(pairs_dir, "src", f"f{g:06d}.yuv"), cube)
        ry, ru, rv = load420(rp)
        n, q = W * H, W * H // 4
        oy, ou, ov = ours[:n].reshape(H, W), ours[n:n + q].reshape(H // 2, W // 2), ours[n + q:].reshape(H // 2, W // 2)
        acc["Y"].append((block_means(oy) - block_means(ry)).ravel())
        acc["Cb"].append((block_means(ou, 4) - block_means(ru, 4)).ravel())
        acc["Cr"].append((block_means(ov, 4) - block_means(rv, 4)).ravel())
    if not acc["Y"]:
        raise SystemExit("no checkable frames (need src and res frames)")
    rep = {}
    for ch, parts in acc.items():
        d = np.abs(np.concatenate(parts))
        rep[ch] = {"median": float(np.median(d)), "p99": float(np.percentile(d, 99)),
                   "bias": float(np.mean(np.concatenate(parts)))}
    rep["frames"] = len(acc["Y"])
    rep["pass"] = all(rep[c]["p99"] <= 2 and rep[c]["median"] <= 1 for c in ("Y", "Cb", "Cr"))
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fit"); f.add_argument("--pairs", required=True); f.add_argument("--out", required=True)
    w = sub.add_parser("write"); w.add_argument("--model", required=True); w.add_argument("--cube", required=True)
    c = sub.add_parser("check"); c.add_argument("--pairs", required=True); c.add_argument("--cube", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "fit":
        m = fit(a.pairs)
        json.dump(m, open(a.out, "w"), indent=1)
        print(json.dumps({k: m[k] for k in ("gamma", "white", "samples", "residual_codes")}, indent=1))
        return 0
    if a.cmd == "write":
        m = json.load(open(a.model))
        write_cube(os.path.expanduser(a.cube), m["white"], m["gamma"])
        print("wrote", a.cube)
        return 0
    rep = check(a.pairs, os.path.expanduser(a.cube))
    print(json.dumps(rep, indent=1))
    return 0 if rep["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
