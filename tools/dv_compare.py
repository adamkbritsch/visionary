#!/usr/bin/env python3
"""Quality gate Q2 for native Dolby Vision: the L1 that SHIPS against pixel ground truth.

WHY NOT AGAINST RESOLVE: Resolve writes Dolby's CM v4 *mapping-oriented* L1 — an anchored
near-constant average (~2048 on 75% of S06E01's frames whatever the pixels do), a peak
floored at 2081, plus L3 offsets, L4 and L2 trims — and flags a scene refresh on EVERY
frame. That L1 is not a measurement, so agreeing with it is definitional, not a quality
measure (an unmeasured constant (0, 3079, 2048) scored better against it than a real
measurement did). Native L1 IS a measurement, so it is checked against the picture.

WHAT DECIDES THE VERDICT (revised plan, 2026-10-14):
  L1 vs truth, per SHOT (L1 is a per-shot value). Native shots are the runs of identical L1
  in the native RPU. ~30 shots are sampled (the 5 brightest and 5 darkest by avg, the rest
  seeded-random) and several frames of each are decoded from the PQ master at full
  resolution. Truth per frame is max(R,G,B) in 12-bit PQ codes: mean, p99.99 clamped to the
  1000-nit source range (3079), p0.1 as the min. Per shot: avg = mean of the frame means,
  max = max of the frame p99.99s. The truth goes through the same clamps the delivered RPU
  carries (max 2081..3079, avg >= the CM floor and < max) before it is compared, so a shot
  darker than the floor is not "wrong" for being floored. A shot where a bound set BOTH
  values is right by construction and tells nothing about the measurement, so the verdict
  medians are over the remaining, measurable shots; the all-shots and raw (unclamped)
  medians are reported beside them.
    pass: median |avg - truth| <= 15 codes and median |max - truth| <= 30
  Format: RPU frame count == the master's (a mismatch is an ERROR, never truncated), L1 on
  every frame, no L1 max above 3079, L6 MaxCLL <= 1000, min <= avg <= max.

The native RPU is always READ BACK through `dovi_tool export`: --native-sidecar first
generates it with dvnative.generate_rpu (into a temporary directory), so both modes judge
exactly what ships. --resolve-rpu adds an INFORMATIONAL comparison (never in the verdict):
per-frame deltas against Resolve's L1 (the CM v2.9 view) and L1 + L3 (the CM v4 view a v4
display applies), plus L4.

  python3 tools/dv_compare.py --native-rpu native.bin --master native.mp4
  python3 tools/dv_compare.py --native-sidecar m.bin.l1.json --master e01.mp4 \\
      [--resolve-rpu resolve.bin] [--frames N] [--shots 30] [--per-shot 6] [--json]

Exit status 0 = pass, 1 = fail, 2 = could not compare.
"""
from __future__ import annotations
import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "engine"))
import dvcap  # noqa: E402
import dvnative  # noqa: E402

GATE = {"median_davg": 15, "median_dmax": 30}
SHOTS, EXTREMES, PER_SHOT = 30, 5, 24   # PER_SHOT caps the frames decoded per shot: a
                                        # sparse sample biases the truth PEAK low (false fails)

SOURCE_MAX_PQ = dvnative.SOURCE_MAX_PQ            # 3079: the declared 1000-nit source range
PQ12_MAX = dvnative.PQ12_MAX
NEUTRAL_L3 = 2048                                 # an L3 offset of 2048 means "no offset"
# What `dovi_tool generate` (2.3.0) does to the L1 it is given — measured with a generate ->
# export round trip: min capped at 12, max floored at 2081 (100 nits), avg kept between the
# CM floor and max - 1. dvnative writes CM v4.0 without l1_avg_pq_cm_version, so 1229.
DOVI_MIN_CAP = 12
DOVI_MAX_FLOOR = dvnative.nits_to_pq12(100)       # 2081
AVG_FLOOR = {"V29": 819, "V40": 1229}


# ---- reading an RPU ------------------------------------------------------------------------

_WS = " \t\r\n"


def _iter_json_array(f, chunk: int = 1 << 20):
    """Yield the elements of a top-level JSON array of OBJECTS (dovi_tool export) one at a
    time, so memory stays at ~2 chunks however long the array is. Not for arrays of bare
    numbers: one cut at a chunk boundary would decode as two."""
    dec = json.JSONDecoder()
    buf, pos, eof = "", 0, False

    def fill():
        nonlocal buf, pos, eof
        more = f.read(chunk)
        eof = not more
        buf, pos = buf[pos:] + more, 0

    def skip(chars):
        nonlocal pos
        while True:
            while pos < len(buf) and buf[pos] in chars:
                pos += 1
            if pos < len(buf) or eof:
                return
            fill()

    skip(_WS)
    if pos >= len(buf) or buf[pos] != "[":
        raise ValueError("not a JSON array")
    pos += 1
    while True:
        skip(_WS + ",")
        if pos >= len(buf):
            raise ValueError("JSON array is truncated")
        if buf[pos] == "]":
            return
        try:
            obj, end = dec.raw_decode(buf, pos)
        except json.JSONDecodeError:
            if eof:
                raise
            fill()                  # the element runs past the buffer: read more, retry
            continue
        pos = end
        yield obj


def _frame_fields(rpu: dict):
    """PURE. (L1, L3, L4, L6, source_max_pq) of one exported RPU frame; a block it does not
    carry is None. L1/L4/L6 live in the CM v2.9 block set, L3 in the CM v4 one."""
    dm = rpu.get("vdr_dm_data") or {}
    got = {}
    for key in ("cmv29_metadata", "cmv40_metadata"):
        for blk in (dm.get(key) or {}).get("ext_metadata_blocks") or []:
            for name, v in blk.items():
                got.setdefault(name, v)
    l1, l3, l4, l6 = (got.get(k) for k in ("Level1", "Level3", "Level4", "Level6"))
    return ((l1["min_pq"], l1["max_pq"], l1["avg_pq"]) if l1 else None,
            (l3["min_pq_offset"], l3["max_pq_offset"], l3["avg_pq_offset"]) if l3 else None,
            (l4["anchor_pq"], l4["anchor_power"]) if l4 else None,
            (l6["max_display_mastering_luminance"], l6["min_display_mastering_luminance"],
             l6["max_content_light_level"], l6["max_frame_average_light_level"]) if l6 else None,
            dm.get("source_max_pq"))


def v4_view(l1, l3):
    """PURE. What a CM v4 display works from: L1 + (L3 offset - 2048) per field, clamped to
    the 12-bit range. No L3 means neutral offsets, i.e. the L1 itself."""
    if l1 is None or l3 is None:
        return l1
    return tuple(max(0, min(PQ12_MAX, v + o - NEUTRAL_L3)) for v, o in zip(l1, l3))


def rpu_frames(rpu_bin: str, dovi_tool: str = dvnative.DOVI_TOOL) -> dict:
    """What a binary RPU carries per frame, from `dovi_tool export`: "l1" (min, max, avg) —
    the CM v2.9 view; "l3" offsets (None where absent); "v4" = L1 + L3 (the same list when no
    frame has L3); "l4" (anchor_pq, anchor_power) per frame, or None when no frame has L4;
    the distinct "l6" tuples (mastering max, min, MaxCLL, MaxFALL) and "source_max_pq"
    values. The export is ~3.5 KB of JSON per frame (139 MB for a 22-min episode, and a
    plain json.load of it peaked at 787 MB RSS), so it goes to a temporary file that is
    parsed one frame at a time, keeping only these fields."""
    l1, l3, l4, l6, src = [], [], [], set(), set()
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "all.json")
        r = subprocess.run([dovi_tool, "export", "-i", rpu_bin, "-d", f"all={out}"],
                           capture_output=True, text=True, errors="replace")
        if r.returncode != 0 or not os.path.exists(out):
            why = (r.stderr or r.stdout or "").strip()[-300:]
            raise RuntimeError(f"dovi_tool export failed on {rpu_bin} (rc={r.returncode}): {why}")
        with open(out) as f:
            for rpu in _iter_json_array(f):
                a, b, c, e, s = _frame_fields(rpu)
                l1.append(a)
                l3.append(b)
                l4.append(c)
                if e:
                    l6.add(e)
                if s is not None:
                    src.add(s)
    has_l3 = any(x is not None for x in l3)
    return {"frames": len(l1), "l1": l1, "l3": l3,
            "v4": [v4_view(a, b) for a, b in zip(l1, l3)] if has_l3 else l1,
            "l4": l4 if any(x is not None for x in l4) else None,
            "l6": sorted(l6), "source_max_pq": sorted(src)}


def shots_from_l1(l1: list) -> list:
    """PURE. [(start, end_exclusive), ...]: runs of identical L1. With a scene refresh on
    every frame (long_play_mode, as Resolve does) the L1 change points ARE the shot starts;
    two adjacent shots that happen to share an L1 read as one, which changes nothing here."""
    shots, a = [], 0
    for i in range(1, len(l1) + 1):
        if i == len(l1) or l1[i] != l1[a]:
            shots.append((a, i))
            a = i
    return shots


# ---- sampling ------------------------------------------------------------------------------

def sample_shots(shots: list, l1: list, n: int = SHOTS, extremes: int = EXTREMES, seed: int = 0,
                 avg_floor: int = AVG_FLOOR["V40"]) -> list:
    """PURE. Indices into `shots` (sorted): every shot when there are at most `n`, else the
    `extremes` brightest and darkest by delivered (avg, max) — where a bad measurement
    shows most — plus seeded-random others up to `n` (reproducible run to run). "Darkest"
    skips shots sitting on both floors (black: avg at the CM floor, max at 2081) — the
    floors decide their L1, so they say nothing about the measurement (they stay eligible
    for the random picks)."""
    idx = [i for i in range(len(shots)) if l1[shots[i][0]] is not None]   # no L1: a format
    if len(idx) <= n:                                                    # fail, not a sample
        return idx
    extremes = min(extremes, n // 2)
    by_level = sorted(idx, key=lambda i: (l1[shots[i][0]][2], l1[shots[i][0]][1], i))
    dark = [i for i in by_level if not (l1[shots[i][0]][2] <= avg_floor and l1[shots[i][0]][1] <= DOVI_MAX_FLOOR)]
    pick = set(dark[:extremes]) | set(by_level[len(by_level) - extremes:])
    rest = [i for i in idx if i not in pick]
    pick |= set(random.Random(seed).sample(rest, max(0, min(len(rest), n - len(pick)))))
    return sorted(pick)


def sample_offsets(length: int, k: int = PER_SHOT) -> list:
    """PURE. Up to `k` frame offsets spread evenly over a shot of `length` frames (the centre
    of each k-th), or every frame of a shorter shot."""
    if length <= k:
        return list(range(length))
    return sorted(set(int((j + 0.5) * length / k) for j in range(k)))


# ---- pixel ground truth --------------------------------------------------------------------

def frame_truth(frame, width: int, height: int) -> dict:
    """PURE (numpy). One yuv444p10le BT.2020nc limited-range PQ frame -> the L1 statistics of
    its max(R,G,B) in 12-bit PQ codes: "mean", "p9999" (p99.99, clamped to the 1000-nit
    source range 3079 — the max an RPU may declare), "min" (p0.1) and "std" (what L4's
    anchor_power tracks). float32 keeps a 4K frame to ~200 MB of temporaries."""
    import numpy as np
    a = np.frombuffer(frame, dtype="<u2")
    n = width * height
    y = (a[:n].astype(np.float32) - 64.0) / 876.0
    cb = (a[n:2 * n].astype(np.float32) - 512.0) / 896.0
    cr = (a[2 * n:3 * n].astype(np.float32) - 512.0) / 896.0
    r = y + 1.4746 * cr
    b = y + 1.8814 * cb
    del cr
    g = (y - 0.2627 * r - 0.0593 * b) / 0.6780
    del y, cb
    m = np.maximum(r, g)
    np.maximum(m, b, out=m)
    np.clip(m, 0.0, 1.0, out=m)
    m *= PQ12_MAX
    lo, hi = np.percentile(m, [0.1, 99.99])
    return {"mean": float(m.mean(dtype=np.float64)), "p9999": float(min(hi, SOURCE_MAX_PQ)),
            "min": float(lo), "std": float(m.std(dtype=np.float64))}


def build_truth_decode_command(ffmpeg: str, src: str, first: int, offsets: list, fps_str: str) -> list:
    """Frames first+o (o in `offsets`, ascending, the first 0) of a video as raw
    yuv444p10le, from ONE decode. The seek is dvcap's frame-exact rule (input -ss at the
    MIDPOINT timestamp, dvcap.seg_seek_seconds, so the first frame out is exactly `first`);
    `select` then keeps the wanted frames, counted from there."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
    ss = dvcap.seg_seek_seconds(first, fps_str)
    if ss is not None:
        cmd += ["-ss", f"{ss:.6f}"]
    expr = "+".join(f"eq(n,{int(o)})" for o in offsets)
    return cmd + ["-i", src, "-map", "0:v:0", "-vf", f"select='{expr}'", "-fps_mode", "passthrough",
                  "-frames:v", str(len(offsets)), "-pix_fmt", "yuv444p10le", "-f", "rawvideo", "-"]


def pixel_truth(master: str, runs: list, *, fps_str: str, width: int, height: int,
                ffmpeg: str = dvcap.FFMPEG) -> dict:
    """{frame index: frame_truth(...)} for each run (first, offsets) — one niced decode per
    run (a shot), streamed a frame at a time (a 4K yuv444p10le frame is ~50 MB)."""
    size = width * height * 3 * 2
    out = {}
    for first, offsets in runs:
        cmd = ["nice", "-n", "10"] + build_truth_decode_command(ffmpeg, master, first, offsets, fps_str)
        with tempfile.TemporaryFile() as err:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
            got = 0
            try:
                for o in offsets:
                    buf = p.stdout.read(size)
                    if len(buf) < size:
                        break
                    out[first + o] = frame_truth(buf, width, height)
                    got += 1
            finally:
                p.stdout.close()
                p.wait()
            if p.returncode != 0 or got != len(offsets):
                err.seek(0)
                why = err.read().decode(errors="replace").strip()[-300:]
                raise RuntimeError(f"truth decode at frame {first}: {got}/{len(offsets)} frames "
                                   f"(rc={p.returncode}) {why}")
    return out


def shot_truth(stats: list) -> dict:
    """PURE. A shot's truth from its sampled frames: mean of the frame means, max of the frame
    p99.99s (already clamped to 3079), min of the frame p0.1s."""
    return {"mean": statistics.mean(s["mean"] for s in stats),
            "max": max(s["p9999"] for s in stats),
            "min": min(s["min"] for s in stats)}


def deliverable(mn: float, mx: float, avg: float, avg_floor: int = AVG_FLOOR["V40"]):
    """PURE. The (min, max, avg) a delivered RPU can carry for a measured one: the source
    clamp (max <= 3079, dvnative.clamp_l1) and then dovi_tool's own (min <= 12, max >= 2081,
    avg in [CM floor, max - 1]). Kept in floats for truth; idempotent."""
    mx = min(max(float(mx), DOVI_MAX_FLOOR), SOURCE_MAX_PQ)
    avg = min(max(float(avg), avg_floor), mx - 1)
    return min(float(mn), avg, DOVI_MIN_CAP), mx, avg


def avg_floor_of(cfg: dict) -> int:
    """The L1 avg floor dovi_tool applies for a generate config (V40's 1229 unless the
    config asks for the v2.9 floor)."""
    return AVG_FLOOR.get(cfg.get("l1_avg_pq_cm_version") or cfg.get("cm_version") or "V40", AVG_FLOOR["V40"])


# ---- the gate ------------------------------------------------------------------------------

def _dstats(signed: list):
    """PURE. Signed deltas -> median / p95 / worst of |d|, plus the mean signed bias."""
    if not signed:
        return None
    ab = sorted(abs(x) for x in signed)
    return {"n": len(ab), "median": statistics.median(ab),
            "p95": ab[min(len(ab) - 1, int(0.95 * len(ab)))], "worst": ab[-1],
            "bias": statistics.mean(signed)}


def gate(nat: dict, sampled: list, truth: dict, *, expected_frames: int,
         avg_floor: int = AVG_FLOOR["V40"]) -> dict:
    """PURE. The Q2 report for a native RPU (rpu_frames' shape), its sampled shots
    [(start, end_exclusive, [frame, ...]), ...] and their pixel truth {frame: frame_truth}.
    Raises ValueError when the RPU does not cover exactly `expected_frames` frames — every
    delta would be measured against the wrong pictures."""
    n = nat["frames"]
    if n != expected_frames:
        raise ValueError(f"the native RPU covers {n} frames, the master has {expected_frames}: "
                         "an RPU must carry exactly one entry per frame")
    l1 = nat["l1"]
    present = [x for x in l1 if x]
    l6 = nat.get("l6") or []
    fmt = {"frames_without_l1": n - len(present),
           "l1_max_over_source": sum(1 for x in present if x[1] > SOURCE_MAX_PQ),
           "l1_worst_max": max((x[1] for x in present), default=None),
           "l1_order_bad": sum(1 for x in present if not x[0] <= x[2] <= x[1]),
           "l6_max_cll": max((x[2] for x in l6), default=None),
           "l6_max_fall": max((x[3] for x in l6), default=None),
           "source_max_pq": nat.get("source_max_pq")}
    rows, d_min, pq, power = [], [], [], []
    l4 = nat.get("l4")
    for a, b, frames in sampled:
        stats = [truth[f] for f in frames if f in truth]
        if not stats or not l1[a]:
            continue
        t = shot_truth(stats)
        mn, mx, avg = l1[a]
        tmn, tmx, tavg = deliverable(t["min"], t["max"], t["mean"], avg_floor)
        d_min.append(mn - tmn)
        # A field is "bound" when a format bound set BOTH the delivered value and the
        # deliverable truth (avg on the CM floor; max on the 2081 floor or the 3079 ceiling):
        # right by construction, so it says nothing about the measurement. The verdict
        # medians skip bound shots — otherwise a dark episode (S06E01: 22 of 30 sampled
        # shots under the avg floor) passes on the floors alone and a bad measurement on
        # the measurable shots hides behind them.
        # mean_spread (informational): how far the sampled frames' means differ inside one
        # native shot — a large one hints at a missed cut (or adjacent shots whose L1 came
        # out identical), which a median would hide.
        rows.append({"start": a, "frames": b - a, "sampled": len(stats), "l1": [mn, mx, avg],
                     "truth": {k: round(v, 1) for k, v in t.items()},
                     "d_avg": avg - tavg, "d_max": mx - tmx,
                     "d_avg_raw": avg - t["mean"], "d_max_raw": mx - t["max"],
                     "avg_bound": avg == avg_floor and tavg == avg_floor,
                     "max_bound": (mx == tmx == DOVI_MAX_FLOOR) or (mx == tmx == SOURCE_MAX_PQ),
                     "mean_spread": max(s["mean"] for s in stats) - min(s["mean"] for s in stats)})
        for f in frames:
            if l4 and l4[f] and f in truth:
                pq.append(l4[f][0] - truth[f]["mean"])
                power.append(l4[f][1] - truth[f]["std"])
    if not rows:
        raise ValueError("no sampled shot has pixel truth")
    summary = {}
    for k in ("avg", "max"):
        s = _dstats([r["d_" + k] for r in rows if not r[k + "_bound"]]) or \
            {"n": 0, "median": 0.0, "p95": 0.0, "worst": 0.0, "bias": 0.0}
        s.update(median_all=_dstats([r["d_" + k] for r in rows])["median"],
                 median_raw=_dstats([r["d_" + k + "_raw"] for r in rows])["median"],
                 bound_shots=sum(r[k + "_bound"] for r in rows))
        summary[k] = s
    for r in rows:
        for k in ("d_avg", "d_max", "d_avg_raw", "d_max_raw", "mean_spread"):
            r[k] = round(r[k], 1)
    return {"frames": n, "expected_frames": expected_frames, "avg_floor": avg_floor, "format": fmt,
            "truth": {"shots_sampled": len(rows), "frames_sampled": sum(r["sampled"] for r in rows),
                      "avg": summary["avg"], "max": summary["max"], "min": _dstats(d_min),
                      "worst_mean_spread": max(r["mean_spread"] for r in rows),
                      "l4": {"anchor_pq": _dstats(pq), "anchor_power": _dstats(power)} if pq else None,
                      "shots": rows}}


def verdict(rep: dict) -> dict:
    """PURE. Native against the picture and the format only — a "resolve" block never counts."""
    f, t = rep["format"], rep["truth"]
    checks = {
        "median_davg": t["avg"]["median"] <= GATE["median_davg"],
        "median_dmax": t["max"]["median"] <= GATE["median_dmax"],
        "frame_count": rep["frames"] == rep["expected_frames"],
        "l1_every_frame": f["frames_without_l1"] == 0,
        "l1_max_le_source": f["l1_max_over_source"] == 0,
        "l1_min_avg_max": f["l1_order_bad"] == 0,
        "l6_max_cll": f["l6_max_cll"] is not None and f["l6_max_cll"] <= dvnative.MASTER_MAX_NITS,
    }
    return {"pass": all(checks.values()), "checks": checks}


def resolve_info(nat: dict, res: dict, truth: dict | None = None) -> dict:
    """PURE. INFORMATIONAL ONLY: native against Resolve per frame, in the CM v2.9 view (L1)
    and the CM v4 view (L1 + L3), plus L4, and Resolve's own avg against the sampled truth.
    Resolve's L1 is Dolby's mapping-oriented value, so large deltas here are expected and
    say nothing about native quality. A length mismatch is reported, not truncated."""
    if res["frames"] != nat["frames"]:
        return {"error": f"Resolve RPU has {res['frames']} frames, native {nat['frames']}: not compared"}
    out = {"frames": res["frames"], "l3_frames": sum(1 for x in res["l3"] if x is not None)}
    for view in ("l1", "v4"):
        pairs = [(q, r) for q, r in zip(nat[view], res[view]) if q and r]
        out["v29" if view == "l1" else "v4"] = {
            k: _dstats([q[j] - r[j] for q, r in pairs]) for j, k in enumerate(("min", "max", "avg"))}
    if nat.get("l4") and res.get("l4"):
        pairs = [(q, r) for q, r in zip(nat["l4"], res["l4"]) if q and r]
        out["l4"] = {k: _dstats([q[j] - r[j] for q, r in pairs])
                     for j, k in enumerate(("anchor_pq", "anchor_power"))}
    else:
        out["l4"] = None
    if truth:
        fs = [f for f in sorted(truth) if f < res["frames"] and res["l1"][f]]
        out["vs_truth"] = {"v29_avg": _dstats([res["l1"][f][2] - truth[f]["mean"] for f in fs]),
                           "v4_avg": _dstats([res["v4"][f][2] - truth[f]["mean"] for f in fs])}
    return out


def resolve_block(nat: dict, resolve_rpu: str, truth: dict, dovi_tool: str = dvnative.DOVI_TOOL) -> dict:
    """The informational Resolve block. Informational means it can never cost the verdict —
    an unreadable Resolve RPU becomes an "error" entry, not an aborted gate."""
    try:
        return resolve_info(nat, rpu_frames(resolve_rpu, dovi_tool), truth)
    except Exception as e:  # noqa: BLE001
        return {"error": f"Resolve RPU not compared: {e}"}


# ---- orchestration -------------------------------------------------------------------------

def probe_master(path: str, ffprobe: str = dvcap.FFPROBE) -> dict:
    """{frames, fps, width, height} of the PQ master. Refuses a video tagged with a
    non-PQ transfer (an untagged one is trusted): its "truth" would be meaningless."""
    r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,color_transfer", "-of", "json", path],
                       capture_output=True, text=True, errors="replace")
    s = (json.loads(r.stdout or "{}").get("streams") or [{}])[0] if r.returncode == 0 else {}
    if not s.get("width"):
        raise RuntimeError(f"could not probe {path}: {(r.stderr or '').strip()[-300:]}")
    trc = s.get("color_transfer")
    if trc and trc not in ("smpte2084", "unknown"):
        raise ValueError(f"{path} is tagged {trc}, not PQ (smpte2084)")
    pv = dvcap.probe_video(path, ffprobe)
    return {"frames": pv["frames"], "fps": pv["fps"], "width": int(s["width"]), "height": int(s["height"])}


def run(*, master: str, native_rpu: str | None = None, native_sidecar: str | None = None,
        resolve_rpu: str | None = None, expected_frames: int | None = None, shots: int = SHOTS,
        per_shot: int = PER_SHOT, seed: int = 0, ffmpeg: str = dvcap.FFMPEG,
        dovi_tool: str = dvnative.DOVI_TOOL) -> dict:
    """The full Q2 report (with "verdict") for one native RPU or sidecar against `master`."""
    pm = probe_master(master)
    if expected_frames and pm["frames"] and expected_frames != pm["frames"]:
        raise ValueError(f"--frames {expected_frames} but {master} has {pm['frames']} frames: "
                         "the truth would come from pictures the RPU does not describe")
    expected = expected_frames or pm["frames"]
    if not expected:
        raise RuntimeError(f"could not count the frames of {master} (pass --frames)")
    native = {"source": "sidecar" if native_sidecar else "rpu"}
    if native_sidecar:
        with tempfile.TemporaryDirectory() as d:
            rpu = os.path.join(d, "native.bin")
            cfg = dvnative.generate_rpu(native_sidecar, rpu, dovi_tool=dovi_tool)
            nat = rpu_frames(rpu, dovi_tool)
        floor = avg_floor_of(cfg)
        native["scenes"] = len(cfg["shots"])
        # The real scene list: runs of identical DELIVERED L1 merge neighbours that the floors
        # and the 3079 ceiling made equal (S06E01: 423 scenes read as 160), hiding peak errors.
        shot_list = [(sh["start"], sh["start"] + sh["duration"]) for sh in cfg["shots"]]
    else:
        nat = rpu_frames(native_rpu, dovi_tool)
        floor = AVG_FLOOR["V40"]          # dvnative's config: CM v4.0, no v2.9 avg floor
        shot_list = shots_from_l1(nat["l1"])
    if nat["frames"] != expected:         # before the expensive decode (gate() checks it too)
        raise ValueError(f"the native RPU covers {nat['frames']} frames, the master has {expected}")
    native["shots"] = len(shot_list)
    sampled = []
    for i in sample_shots(shot_list, nat["l1"], n=shots, seed=seed, avg_floor=floor):
        a, b = shot_list[i]
        sampled.append((a, b, [a + o for o in sample_offsets(b - a, per_shot)]))
    truth = pixel_truth(master, [(fr[0], [f - fr[0] for f in fr]) for _, _, fr in sampled],
                        fps_str=pm["fps"], width=pm["width"], height=pm["height"], ffmpeg=ffmpeg)
    rep = gate(nat, sampled, truth, expected_frames=expected, avg_floor=floor)
    rep["native"] = native
    rep["master"] = {"path": master, "width": pm["width"], "height": pm["height"], "fps": pm["fps"]}
    if resolve_rpu:
        rep["resolve"] = resolve_block(nat, resolve_rpu, truth, dovi_tool)
    rep["verdict"] = verdict(rep)
    return rep


def _fmt(s, key="median"):
    return "n/a" if not s else f"{s[key]:.1f}"


def print_report(rep: dict) -> None:
    t, f, v = rep["truth"], rep["format"], rep["verdict"]
    nat = rep["native"]
    print(f"native {nat['source']}: {rep['frames']} frames (master {rep['expected_frames']}), "
          f"{nat['shots']} shots" + (f" from {nat['scenes']} scenes" if "scenes" in nat else "")
          + f"  |  truth: {t['shots_sampled']} shots, {t['frames_sampled']} frames at "
          f"{rep['master']['width']}x{rep['master']['height']}")
    for k, gate_key, bounds in (("avg", "median_davg", f"the {rep['avg_floor']} floor"),
                                ("max", "median_dmax", f"{DOVI_MAX_FLOOR}/{SOURCE_MAX_PQ}")):
        s = t[k]
        print(f"  L1 {k} vs truth: median |d| {s['median']:.1f} over {s['n']} measurable shot(s)"
              f" (gate <= {GATE[gate_key]})  p95 {s['p95']:.1f}  worst {s['worst']:.1f}  bias {s['bias']:+.1f}"
              f"  |  {s['bound_shots']} set by {bounds}; all shots {s['median_all']:.1f}, raw {s['median_raw']:.1f}")
    if t["l4"]:
        print(f"  L4 vs truth (info): anchor_pq median |d| {_fmt(t['l4']['anchor_pq'])}"
              f"  anchor_power {_fmt(t['l4']['anchor_power'])}")
    print(f"  format: L1 max over {SOURCE_MAX_PQ} on {f['l1_max_over_source']} frame(s) (worst {f['l1_worst_max']})"
          f"  |  L6 MaxCLL {f['l6_max_cll']} / MaxFALL {f['l6_max_fall']}"
          f"  |  min<=avg<=max broken on {f['l1_order_bad']}  |  no L1 on {f['frames_without_l1']}")
    res = rep.get("resolve")
    if res:
        if "error" in res:
            print(f"  Resolve (informational): {res['error']}")
        else:
            print(f"  Resolve (informational, not gated): avg median |d| v2.9 {_fmt(res['v29']['avg'])}"
                  f" / v4 (L1+L3) {_fmt(res['v4']['avg'])}; max v2.9 {_fmt(res['v29']['max'])}"
                  + (f"; L4 anchor_pq {_fmt(res['l4']['anchor_pq'])}, anchor_power {_fmt(res['l4']['anchor_power'])}"
                     if res.get("l4") else "; L4 n/a")
                  + (f"; Resolve avg vs truth v2.9 {_fmt(res['vs_truth']['v29_avg'])}"
                     f" / v4 {_fmt(res['vs_truth']['v4_avg'])}" if res.get("vs_truth") else ""))
    print("PASS" if v["pass"] else "FAIL", {k: ("ok" if ok else "FAIL") for k, ok in v["checks"].items()})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--native-rpu", help="the native binary RPU")
    g.add_argument("--native-sidecar", help="an hdr-analyze sidecar; the RPU is generated from it first")
    ap.add_argument("--master", required=True, help="the PQ video the native L1 describes (pixel truth)")
    ap.add_argument("--resolve-rpu", help="Resolve's RPU, for an informational comparison only")
    ap.add_argument("--frames", type=int, help="expected frame count (default: counted from --master)")
    ap.add_argument("--shots", type=int, default=SHOTS, help="shots to sample for truth")
    ap.add_argument("--per-shot", type=int, default=PER_SHOT, help="frames decoded per sampled shot")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        rep = run(master=a.master, native_rpu=a.native_rpu, native_sidecar=a.native_sidecar,
                  resolve_rpu=a.resolve_rpu, expected_frames=a.frames, shots=a.shots,
                  per_shot=a.per_shot, seed=a.seed)
    except Exception as e:  # noqa: BLE001 — a CLI reports, it does not trace
        print(f"could not compare: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(rep, indent=1))
    else:
        print_report(rep)
    return 0 if rep["verdict"]["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
