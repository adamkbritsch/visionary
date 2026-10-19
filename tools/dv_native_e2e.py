#!/usr/bin/env python3
"""End-to-end check of the native Dolby Vision lane on REAL Topaz chunks (rollout step 2).

Runs the production stage functions — stages._dvnative (analysis -> RPU) and stages._remux
(native branch: chunks + captured LUT -> peak-capped x265 with the generated RPU -> mux ->
verify -> chunk release) — on a scratch copy of an episode, under a SCRATCH HOME so nothing
touches ~/.topaz-pipeline (log, settings, audio-gain book) or the live pipeline: only the
pinned analyzer and LUT are linked in. Then checks what shipped:

  1. kill mid-analysis, resume: finished chunks are reused, the rest analysed
  2. the native remux through stages._remux: ok, marker 'encoded', chunk files released
  3. format: Matroska, DV 8.1 (compat 1), PQ / BT.2020, frame count == chunks, peak <= cap
  4. the master's embedded RPU == the generated RPU, frame for frame (L1, L3, L4)
  5. audio vs video duration within one frame
  6. picture: VMAF of the master vs the lossless LUT reference
  7. forced peak repair: remux.remux(native=) at a tight cap re-reads the chunks + LUT
  8. a tampered marker (LUT sha) makes ready() refuse and bundle() return None

  python3 tools/dv_native_e2e.py --chunks DIR --cfr CFR.mp4 --work DIR
DIR holds seg_*.mov + segments.json (a contiguous prefix of an episode's Topaz output); CFR
is the episode's CFR trimmed to exactly that many frames (audio included).
"""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "..", "engine")
REAL_HOME = os.path.expanduser("~")
FF, FFPROBE = "/opt/homebrew/bin/ffmpeg", "/opt/homebrew/bin/ffprobe"
SERIES, EP = "E2E Native Show", "S06E05"
SOURCE = f"{SERIES} {EP} 1080p.mkv"


# ---- the RPU reader (what a binary RPU carries per frame; dovi_tool export, streamed) ----------
PQ12_MAX = 4095                                  # = dvnative.PQ12_MAX (12-bit PQ code ceiling)
NEUTRAL_L3 = 2048                                 # an L3 offset of 2048 means "no offset"
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


def rpu_frames(rpu_bin: str, dovi_tool: str | None = None) -> dict:
    """What a binary RPU carries per frame, from `dovi_tool export`: "l1" (min, max, avg) —
    the CM v2.9 view; "l3" offsets (None where absent); "v4" = L1 + L3 (the same list when no
    frame has L3); "l4" (anchor_pq, anchor_power) per frame, or None when no frame has L4;
    the distinct "l6" tuples (mastering max, min, MaxCLL, MaxFALL) and "source_max_pq"
    values. The export is ~3.5 KB of JSON per frame (139 MB for a 22-min episode, and a
    plain json.load of it peaked at 787 MB RSS), so it goes to a temporary file that is
    parsed one frame at a time, keeping only these fields."""
    if dovi_tool is None:
        import dvnative
        dovi_tool = dvnative.DOVI_TOOL
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


def fake_home(work: str) -> str:
    """A HOME whose ~/.topaz-pipeline holds only links to the pinned analyzer + LUT."""
    home = os.path.join(work, "home")
    tp = os.path.join(home, ".topaz-pipeline")
    os.makedirs(tp, exist_ok=True)
    for sub in ("bin", "luts"):
        link = os.path.join(tp, sub)
        if not os.path.lexists(link):
            os.symlink(os.path.join(REAL_HOME, ".topaz-pipeline", sub), link)
    os.makedirs(os.path.join(home, "topaz-scratch"), exist_ok=True)
    return home


def stage_episode(work: str, chunks: str, cfr: str, run: str):
    """Hard-link the chunks + CFR into the layout orchestrator.episode_paths expects."""
    import orchestrator
    d = os.path.join(work, run)
    os.makedirs(d, exist_ok=True)
    p = orchestrator.episode_paths(SERIES, EP, SOURCE, scratch_dir=d)
    os.makedirs(p.segdir, exist_ok=True)
    for f in sorted(os.listdir(chunks)):
        if f.startswith("seg_") and f.endswith(".mov"):
            dst = os.path.join(p.segdir, f)
            if not os.path.exists(dst):
                os.link(os.path.join(chunks, f), dst)
    shutil.copyfile(os.path.join(chunks, "segments.json"), os.path.join(p.segdir, "segments.json"))
    for dst in (p.source, p.source_cfr):
        if not os.path.exists(dst):
            os.link(cfr, dst)
    return p


def probe(path: str) -> dict:
    r = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                       capture_output=True, text=True)
    return json.loads(r.stdout or "{}")


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def child_analysis(work: str, chunks: str, cfr: str):
    """Run in a separate process (so it can be SIGKILLed): the analysis stage, run1."""
    import threading
    import stages
    p = stage_episode(work, chunks, cfr, "run1")
    ok, msg = stages._dvnative(p, threading.Event())
    print("child:", ok, msg, flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", required=True)
    ap.add_argument("--cfr", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    work = os.path.abspath(a.work)
    if not a.child:
        # re-exec under the scratch HOME before any engine module reads ~
        home = fake_home(work)
        # The app's /usr/bin/python3 finds numpy/cv2 in the REAL user site (pip --user, per
        # requirements.txt; Apple's framework layout, lib/python/site-packages), which a scratch
        # HOME would hide — so it goes on PYTHONPATH explicitly (children inherit it).
        import site
        env = dict(os.environ, HOME=home, VISIONARY_TEST_LOG_DIR=os.path.join(work, "logs"),
                   PYTHONPATH=os.pathsep.join([os.path.abspath(ENGINE), site.getusersitepackages()]))
        if os.environ.get("HOME") != home:
            return subprocess.call([sys.executable, os.path.abspath(__file__)] + (argv or sys.argv[1:]),
                                   env=env)
    sys.path.insert(0, os.path.abspath(ENGINE))
    if a.child:
        child_analysis(work, os.path.abspath(a.chunks), os.path.abspath(a.cfr))
        return 0
    return run(work, os.path.abspath(a.chunks), os.path.abspath(a.cfr))


def run(work: str, chunks: str, cfr: str) -> int:
    import threading
    import nativelane, stages, remux, dvcap, dvnative
    results = []
    t0 = time.time()
    print(f"HOME={os.environ['HOME']} (scratch) — the live pipeline and ~/.topaz-pipeline are untouched")
    ok, why = nativelane.toolchain_ok(force=True)
    results.append(check("toolchain pinned and runnable", ok, why or ""))

    # 1. kill mid-analysis, then resume in-process
    print("1. analysis with a kill after the first chunk, then resume", flush=True)
    p = stage_episode(work, chunks, cfr, "run1")
    workdir = nativelane.work_dir(p)
    child = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--chunks", chunks, "--cfr", cfr,
                              "--work", work, "--child"], env=os.environ, start_new_session=True)
    first = os.path.join(workdir, dvnative.sidecar_path("chunk_0000.bin"))
    while child.poll() is None and not os.path.exists(first):
        time.sleep(2)
    killed = child.poll() is None
    if killed:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
    results.append(check("child killed after chunk 0 was measured", killed and os.path.exists(first)))
    m0 = os.path.getmtime(first) if os.path.exists(first) else None
    ta = time.time()
    ok, msg = stages._dvnative(p, threading.Event())
    results.append(check("analysis resumed and finished", ok, f"{msg} ({time.time() - ta:.0f}s)"))
    results.append(check("chunk 0 was not re-measured", m0 is not None and os.path.getmtime(first) == m0))
    results.append(check("marker ready", nativelane.ready(p)))
    b = nativelane.bundle(p)

    # 2. the native remux through the production stage
    print("2. native remux through stages._remux", flush=True)
    tr = time.time()
    ok, msg = stages._remux(p, threading.Event())
    results.append(check("stages._remux ok", ok, f"{msg[:300]} ({time.time() - tr:.0f}s)"))
    marker = nativelane.read_marker(p) or {}
    results.append(check("marker stamped 'encoded'", marker.get("state") == "encoded", str(marker.get("state"))))
    left = [f for f in os.listdir(p.segdir) if f.startswith("seg_") and f.endswith(".mov")]
    results.append(check("chunk files released", not left, f"{len(left)} left"))
    final = p.final
    if not os.path.exists(final):
        results.append(check("master exists", False, final))
        return summary(results, t0)

    # 3. format
    print("3. format", flush=True)
    pr = probe(final)
    v = next(s for s in pr["streams"] if s["codec_type"] == "video")
    dovi = next((sd for sd in v.get("side_data_list", []) if "DOVI" in sd.get("side_data_type", "")), {})
    results.append(check("Dolby Vision 8.1", dovi.get("dv_profile") == 8 and dovi.get("dv_bl_signal_compatibility_id") == 1,
                         f"profile {dovi.get('dv_profile')} compat {dovi.get('dv_bl_signal_compatibility_id')}"))
    results.append(check("Matroska master", final.endswith(".mkv") and
                         "matroska" in (pr.get("format", {}).get("format_name") or ""),
                         pr.get("format", {}).get("format_name", "")))
    results.append(check("PQ / BT.2020", v.get("color_transfer") == "smpte2084" and v.get("color_primaries") == "bt2020"))
    frames = dvcap.count_hevc_frames(final)
    results.append(check("frame count == chunks", frames == b["total"], f"{frames} vs {b['total']}"))
    peak = dvcap.video_peak_1s_mbps(final)
    cap = 50
    results.append(check("peak within the cap", dvcap.peak_ok(peak, cap), f"{peak:.1f} Mbps"))

    # 4. embedded RPU == generated RPU
    print("4. embedded RPU vs generated", flush=True)
    emb = os.path.join(work, "embedded.rpu")
    dvcap.extract_rpu(final, emb)
    gen = rpu_frames(b["rpu"])
    got = rpu_frames(emb)
    same = all(got[k] == gen[k] for k in ("l1", "l3", "l4")) and got["frames"] == gen["frames"]
    results.append(check("embedded RPU == generated, frame for frame", same, f"{got['frames']} frames"))

    # 5. A/V duration
    a_dur = max((float(s.get("duration") or 0) for s in pr["streams"] if s["codec_type"] == "audio"), default=0)
    v_dur = frames * 1001 / 24000
    results.append(check("audio vs video within one frame", abs(a_dur - v_dur) <= 1001 / 24000,
                         f"audio {a_dur:.3f}s video {v_dur:.3f}s"))

    # 6. picture vs the lossless LUT reference (first 600 frames of chunk 0)
    print("6. VMAF vs the LUT reference", flush=True)
    chunk0 = os.path.join(chunks, "seg_0000.mov")
    lut = nativelane.lut_path()
    g = (f"[0:v]trim=end_frame=600,setpts=PTS-STARTPTS,format=yuv420p10le[d];"
         f"[1:v]trim=end_frame=600,{dvnative.lut_vf(lut)},setpts=PTS-STARTPTS[r];"
         f"[d][r]libvmaf=model=version=vmaf_4k_v0.6.1:n_threads=8")
    e = subprocess.run(["nice", "-n", "10", FF, "-hide_banner", "-nostdin", "-i", final, "-i", chunk0,
                        "-filter_complex", g, "-f", "null", "-"], capture_output=True, text=True).stderr
    m = re.search(r"VMAF score:\s*([\d.]+)", e)
    vm = float(m.group(1)) if m else 0.0
    results.append(check("VMAF >= 95 vs the LUT reference", vm >= 95, f"{vm:.2f}"))

    # 7. forced peak repair from the chunks (a second, independent run)
    print("7. forced peak repair at a tight cap", flush=True)
    p2 = stage_episode(work, chunks, cfr, "run2")
    ok2, msg2 = stages._dvnative(p2, threading.Event())            # its own marker/RPU (fast: same chunks)
    b2 = nativelane.bundle(p2)
    out2 = os.path.join(work, "run2", "repair.mkv")
    tr = time.time()
    # 6 Mbps: well under what x265's VBV lets through on 4K content, so the peak gate
    # (cap x 1.15) fails and a REPAIR must run (12 Mbps passed outright on S06E05: 12.4 Mbps).
    res = remux.remux(p2.dv_render, p2.source_cfr, p2.source, out2, cap_mbps=6, native=b2)
    tight = os.path.join(out2 + ".remuxsegs", "tight_caps.json")    # kept only on failure
    results.append(check("repair ran or failed cleanly", res.ok or bool(res.reason),
                         f"ok={res.ok} {res.reason[:200]} ({time.time() - tr:.0f}s)"))
    results.append(check("a peak repair re-encoded segments from the chunks",
                         "re-capped" in (res.reason or "") or os.path.exists(tight), res.reason[-160:]))
    if res.ok:
        f2 = dvcap.count_hevc_frames(out2)
        results.append(check("repaired master keeps every frame and its DV", f2 == b2["total"] and
                             next((sd for sd in probe(out2)["streams"][0].get("side_data_list") or []
                                   if "DOVI" in sd.get("side_data_type", "")), {}).get("dv_profile") == 8,
                             f"{f2} frames"))

    # 8. tampered marker
    print("8. tampered marker", flush=True)
    mk = nativelane.marker_path(p2)
    m2 = json.load(open(mk))
    for k in list(m2):
        if "lut" in k and "sha" in k:
            m2[k] = "0" * 64
    json.dump(m2, open(mk, "w"))
    results.append(check("ready() refuses a changed LUT sha", not nativelane.ready(p2)))
    results.append(check("bundle() is None", nativelane.bundle(p2) is None))
    return summary(results, t0)


def summary(results, t0) -> int:
    n_ok = sum(results)
    print(f"\nE2E: {n_ok}/{len(results)} checks passed in {(time.time() - t0) / 60:.1f} min")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
