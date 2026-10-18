#!/usr/bin/env python3
"""End-to-end check of the native render ('dvrender') for ONE route, on a REAL source
(Resolve-free plan, P3).

Builds p.dv_render for a route through the production stage functions — the download stage's
CFR step (stages._ensure_cfr), the scene plan (stages._plan_fast_path_bounds), the route and its
spec (routes.item_route, stages.dvrender_spec) and stages._dvrender itself — then feeds the render
to the real downstream function through stages._remux (remux_ship_render for a YouTube route,
remux() for 4K SDR) and checks what came out. NOTHING IS UPLOADED: the upload stage is never
called, and no NAS path is touched.

Runs under a SCRATCH HOME, like dv_native_e2e.py, so nothing touches ~/.topaz-pipeline (log,
settings, probe cache, bounds book) or the live pipeline — only the pinned analyzer and LUT are
linked in — in a work directory that is NOT ~/topaz-scratch (deploy-now.sh's process patterns
never see it), at nice 15 (the stage's own children add their nice 10 on top).

  1. the route: routes.item_route names --route for the source (a YouTube video for a yt_*
     route, a movie for sdr4k), and stages.dvrender_spec builds its spec
  2. --resume-check: a child process is SIGKILLed once the first analysis range is published;
     the in-process run must resume without measuring that range again
  3. the render: stages._dvrender ok; the orchestrator's own gate, stage_done("resolve"), accepts
     p.dv_render (DV 8.1, the whole source, its frame rate); the frame count is the spec's
  4. the downstream: stages._remux ok; the master is DV 8.1 (compat 1), PQ / BT.2020, every
     frame, and its 1-s video peak is inside the cap
  5. the books (encode attempts, CPU per range and attempt), printed for the record

  python3 tools/route_e2e.py --source SRC --work DIR --route yt_native4k|sdr4k
                             [--seconds 90] [--resume-check] [--keep]

The work directory is deleted after a run whose every check passed, unless --keep; a failing
run is left in place to look at.
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "..", "engine")
REAL_HOME = os.path.expanduser("~")
FF, FFPROBE = "/opt/homebrew/bin/ffmpeg", "/opt/homebrew/bin/ffprobe"
NICE = 15
CHANNEL = "E2E Route Channel"
MOVIE_DIR = "/Media/Movies/E2E Route"      # a movie's NAS folder: named, never touched
KINDS = {"yt_native4k": "youtube", "sdr4k": "movie"}


def fake_home(work: str) -> str:
    """A HOME whose ~/.topaz-pipeline holds only links to the pinned analyzer + LUT."""
    home = os.path.join(work, "home")
    tp = os.path.join(home, ".topaz-pipeline")
    os.makedirs(tp, exist_ok=True)
    for sub in ("bin", "luts"):
        link = os.path.join(tp, sub)
        if not os.path.lexists(link):
            os.symlink(os.path.join(REAL_HOME, ".topaz-pipeline", sub), link)
    return home


def item_paths(work: str, source: str, kind: str):
    """The item's EpisodePaths in <work>/run — the layout the stages expect, built the way the
    orchestrator builds a YouTube video's or a movie's (deterministic: the child rebuilds them)."""
    import orchestrator
    run = os.path.join(work, "run")
    os.makedirs(run, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(source))
    # The source's own name (a live YouTube name carries its [video id], commas, '+' and '#'):
    # only what cannot be one path component is replaced.
    name = "E2E Route " + "".join("_" if c in "/:\x00" else c for c in stem)[:80] + ext
    if kind == "youtube":
        p = orchestrator.youtube_paths(CHANNEL, f"/YouTube-raw/{CHANNEL}/{name}/{name}", scratch_dir=run)
    else:
        p = orchestrator.movie_paths(name, MOVIE_DIR, scratch_dir=run)
    return orchestrator.apply_container(p)


def stage_source(source: str, dst: str, seconds) -> None:
    """The source into the item's place: hard-linked (copied across volumes), or its first
    `seconds` stream-copied — every stream, so the remux has the source's audio."""
    if os.path.exists(dst):
        return
    if seconds:
        subprocess.run([FF, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", source,
                        "-t", str(seconds), "-map", "0", "-c", "copy", dst], check=True)
        return
    try:
        os.link(source, dst)
    except OSError:
        shutil.copyfile(source, dst)


def probe(path: str) -> dict:
    r = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                       capture_output=True, text=True)
    return json.loads(r.stdout or "{}")


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return bool(ok)


def child_render(work: str, source: str, kind: str) -> None:
    """Run in a separate process (so it can be SIGKILLed): the render stage, from the start."""
    import threading
    import stages
    p = item_paths(work, source, kind)
    print("child:", *stages._dvrender(p, threading.Event()), flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="native render ('dvrender') end to end, one route")
    ap.add_argument("--source", required=True, help="the original source file (read only)")
    ap.add_argument("--work", required=True, help="a scratch directory (never ~/topaz-scratch)")
    ap.add_argument("--route", required=True, choices=sorted(KINDS),
                    help="the route to render (the source must classify as it)")
    ap.add_argument("--seconds", type=float, help="use only the source's first SECONDS")
    ap.add_argument("--resume-check", action="store_true", help="kill after range 1, then resume")
    ap.add_argument("--keep", action="store_true", help="keep the work directory after a pass")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    work = os.path.abspath(a.work)
    source = os.path.abspath(a.source)
    if os.path.realpath(work).startswith(os.path.realpath(os.path.join(REAL_HOME, "topaz-scratch"))) \
            or "/topaz-scratch/" in work + "/" or os.path.realpath(work).startswith(
                os.path.realpath(os.path.join(REAL_HOME, ".topaz-pipeline"))):
        print("refusing: --work must not be inside ~/topaz-scratch or ~/.topaz-pipeline "
              "(the live pipeline's own directories)", file=sys.stderr)
        return 2
    if not os.path.isfile(source):
        print(f"refusing: no source at {source}", file=sys.stderr)
        return 2
    if not a.child:
        # re-exec under the scratch HOME before any engine module reads ~
        home = fake_home(work)
        # The app's /usr/bin/python3 finds numpy/cv2 in the REAL user site (pip --user), which a
        # scratch HOME would hide — so it goes on PYTHONPATH explicitly (children inherit it).
        import site
        env = dict(os.environ, HOME=home, VISIONARY_TEST_LOG_DIR=os.path.join(work, "logs"),
                   PYTHONPATH=os.pathsep.join([os.path.abspath(ENGINE), site.getusersitepackages()]))
        if os.environ.get("HOME") != home:
            return subprocess.call([sys.executable, os.path.abspath(__file__)] + (argv or sys.argv[1:]),
                                   env=env)
        os.nice(NICE)
    sys.path.insert(0, os.path.abspath(ENGINE))
    if a.child:
        child_render(work, source, KINDS[a.route])
        return 0
    return run(a, work, source)


def run(a, work: str, source: str) -> int:
    import threading
    import dvcap, dvnative, nativerender, orchestrator, routes, stages
    results = []
    t0 = time.time()
    print(f"HOME={os.environ['HOME']} (scratch), nice {os.nice(0)} — the live pipeline and "
          f"~/.topaz-pipeline are untouched; nothing is uploaded", flush=True)

    # 0. the item: the source, the production CFR step, the scene plan
    kind = KINDS[a.route]
    p = item_paths(work, source, kind)
    stage_source(source, p.source, a.seconds)
    p = orchestrator.apply_container(p)
    ok, msg = stages._ensure_cfr(p, threading.Event())
    results.append(check("the CFR step (stages._ensure_cfr)", ok, msg))
    if not ok:
        return summary(results, t0, work, a.keep)
    print("  scene plan:" + stages._plan_fast_path_bounds(p), flush=True)

    # 1. the route and its spec
    print("1. the route", flush=True)
    route = routes.item_route(p)
    results.append(check(f"routes.item_route names {a.route}", route == a.route,
                         f"{route} — {routes.describe(route)}"))
    spec, why = stages.dvrender_spec(p, route)
    results.append(check("stages.dvrender_spec builds its spec", spec is not None,
                         why or f"{spec.total} frames @ {spec.fps}, {spec.kbps} kb/s, cap "
                                f"{spec.cap_mbps or 'none'}, {len(spec.analysis_ranges())} "
                                f"analysis ranges"))
    if spec is None or not results[-2]:
        return summary(results, t0, work, a.keep)

    # 2. kill after the first range, then resume (optional)
    ranges = spec.analysis_ranges()
    first = os.path.join(nativerender.books_dir(p), nativerender.ANALYSIS_DIR,
                         dvnative.sidecar_path(f"range_0000_0_{ranges[0][1]}.bin"))
    m0 = None
    if a.resume_check and len(ranges) < 2:
        print("2. resume check skipped — the source is one analysis range", flush=True)
    elif a.resume_check:
        print("2. the render, killed after its first analysis range", flush=True)
        child = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--source", source,
                                  "--work", work, "--route", a.route, "--child"],
                                 env=os.environ, start_new_session=True)
        while child.poll() is None and not os.path.exists(first):
            time.sleep(2)
        killed = child.poll() is None
        if killed:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        results.append(check("child killed after range 1 was measured",
                             killed and os.path.exists(first)))
        m0 = os.path.getmtime(first) if os.path.exists(first) else None

    # 3. the render, through the production stage
    print("3. the render (stages._dvrender)", flush=True)
    tr = time.time()
    ok, msg = stages._dvrender(p, threading.Event(), progress=None)
    results.append(check("stages._dvrender ok", ok, f"{msg[:300]} ({time.time() - tr:.0f}s)"))
    if m0 is not None:
        results.append(check("range 1 was not measured again",
                             os.path.exists(first) and os.path.getmtime(first) == m0))
    if not ok:
        return summary(results, t0, work, a.keep)
    results.append(check('the orchestrator\'s gate: stage_done("resolve")',
                         orchestrator.stage_done("resolve", p)))
    frames = dvcap.count_hevc_frames(p.dv_render)
    results.append(check("render frames == the spec's", frames == spec.total,
                         f"{frames} vs {spec.total}"))
    if spec.cap_mbps:
        peak = dvcap.video_peak_1s_mbps(p.dv_render)
        print(f"  render 1-s peak {peak:.1f} Mbps against the {spec.cap_mbps} Mbps cap "
              f"({'under' if dvcap.peak_ok(peak, spec.cap_mbps) else 'OVER — the remux re-encodes'})")

    # 4. the real downstream function (no upload)
    print("4. the downstream (stages._remux)", flush=True)
    tr = time.time()
    ok, msg = stages._remux(p, threading.Event())
    results.append(check("stages._remux ok", ok, f"{str(msg)[:300]} ({time.time() - tr:.0f}s)"))
    if ok and os.path.exists(p.final):
        pr = probe(p.final)
        v = next((s for s in pr.get("streams", []) if s.get("codec_type") == "video"), {})
        dovi = next((sd for sd in v.get("side_data_list", []) if "DOVI" in sd.get("side_data_type", "")), {})
        results.append(check("master is Dolby Vision 8.1",
                             dovi.get("dv_profile") == 8 and dovi.get("dv_bl_signal_compatibility_id") == 1,
                             f"profile {dovi.get('dv_profile')} compat "
                             f"{dovi.get('dv_bl_signal_compatibility_id')}"))
        results.append(check("master is PQ / BT.2020", v.get("color_transfer") == "smpte2084"
                             and v.get("color_primaries") == "bt2020"))
        mf = dvcap.count_hevc_frames(p.final)
        results.append(check("master frames == the spec's", mf == spec.total, f"{mf} vs {spec.total}"))
        cap = 50
        try:
            import settings
            cap = int(settings.get_settings().get("max_peak_mbps", 50))
        except Exception:  # noqa: BLE001
            pass
        mp = dvcap.video_peak_1s_mbps(p.final)
        results.append(check("master 1-s video peak within the cap", dvcap.peak_ok(mp, cap),
                             f"{mp:.1f} Mbps against {cap}"))
    elif ok:
        results.append(check("master exists", False, p.final))

    # 5. the books, for the record
    books = nativerender.books_dir(p)
    for name in (nativerender.ENCODE_BOOK, nativerender.CPU_LEDGER):
        try:
            with open(os.path.join(books, name)) as f:
                print(f"  {name}: {json.dumps(json.load(f))[:600]}")
        except (OSError, ValueError):
            print(f"  {name}: (none)")
    return summary(results, t0, work, a.keep)


def summary(results, t0, work: str, keep: bool) -> int:
    n_ok = sum(results)
    passed = n_ok == len(results)
    print(f"\nroute E2E: {n_ok}/{len(results)} checks passed in {(time.time() - t0) / 60:.1f} min")
    if passed and not keep:
        shutil.rmtree(os.path.join(work, "run"), ignore_errors=True)
        print(f"(work files deleted; logs kept in {os.path.join(work, 'logs')})")
    else:
        print(f"(work files kept in {work})")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
