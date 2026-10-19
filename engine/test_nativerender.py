import collections
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

import dvcap
import dvnative
import nativerender as NR
import peakplan
import remux

TOTAL = 48
FPS = "24000/1001"


def _sidecar(ranges, l4=True):
    """A merged sidecar with one scene per range (what analyze_ranges returns)."""
    scenes = [{"start": a, "end": b - 1, "min_pq_12bit": 1, "max_pq_12bit": 2900,
               "avg_max_rgb_pq_12bit": 1500} for a, b in ranges]
    sc = {"version": 5, "scenes": scenes,
          "source": {"stream_frames": ranges[-1][1], "leading_skipped_frames": 0},
          "light_level": {"max_cll_nits": 900, "max_fall_nits": 200}}
    if l4:
        sc["l4"] = [[900, 300]] * ranges[-1][1]
    return sc


class _Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()                   # no spaces: lut_vf needs a plain path
        self.src = os.path.join(self.root, "src.mp4")
        with open(self.src, "wb") as f:
            f.write(b"src" * 100)
        self.lut = os.path.join(self.root, "rcm.cube")
        with open(self.lut, "w") as f:
            f.write("LUT_3D_SIZE 2\n")
        self.p = types.SimpleNamespace(ep="S01E01", dv_render=os.path.join(self.root, "Show S01E01_dv.mov"),
                                       source_cfr=self.src, combine=False,
                                       segdir=os.path.join(self.root, "Show S01E01.segments"))
        self.spec = NR.RenderSpec(inputs=[[0, self.src], [TOTAL, None]], fps=FPS, kbps=14000, cap_mbps=50,
                                  lut=self.lut, bounds=[20, TOTAL])

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)


class Spec(_Base):
    def test_a_good_spec_has_no_problems(self):
        self.assertEqual(self.spec.problems(), [])
        self.assertTrue(self.spec.single)
        self.assertEqual(self.spec.total, TOTAL)
        self.assertEqual(self.spec.inputs, ((0, self.src), (TOTAL, None)))

    def test_problems_name_what_is_wrong(self):
        bad = {"inputs": [[0, self.src]], "fps": "x", "kbps": 0, "lut": "/a b/x.cube",
               "pre_vf": "split;scale", "avg_floor": "V30"}
        for field, value in bad.items():
            with self.subTest(field=field):
                self.assertTrue(dataclasses.replace(self.spec, **{field: value}).problems())
        chunks = NR.RenderSpec(inputs=[[0, self.src], [10, self.src], [TOTAL, None]], fps=FPS, kbps=1,
                               bounds=[20])
        self.assertIn("bounds and cuts are for one source file", " ".join(chunks.problems()))
        cuts = NR.RenderSpec(inputs=[[0, self.src], [10, self.src], [TOTAL, None]], fps=FPS, kbps=1,
                             cuts=[5])
        self.assertIn("bounds and cuts are for one source file", " ".join(cuts.problems()))
        self.assertTrue(NR.RenderSpec(inputs=[[0, self.src], [0, None]], fps=FPS, kbps=1).problems())
        gone = dataclasses.replace(self.spec, lut=os.path.join(self.root, "gone.cube"))
        self.assertEqual(gone.problems(), [f"the LUT is missing at {gone.lut}"])

    def _plan(self, total, bounds=None, cuts=None):
        spec = dataclasses.replace(self.spec, inputs=[[0, self.src], [total, None]], bounds=bounds,
                                   cuts=cuts)
        ranges, joins = spec.analysis_plan()
        self.assertEqual(ranges, spec.analysis_ranges())
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], total)
        self.assertTrue(all(a == pb for (a, _b), (_pa, pb) in zip(ranges[1:], ranges)))   # contiguous
        self.assertTrue(all(0 < b - a <= NR.ANALYSIS_RANGE_MAX_FRAMES for a, b in ranges))
        self.assertTrue(set(joins) <= {a for a, _b in ranges[1:]})
        return ranges, joins

    def test_short_scene_plan_ranges_are_the_plan_itself(self):
        self.assertEqual(self.spec.analysis_plan(), ([(0, 20), (20, TOTAL)], ()))
        chunks = NR.RenderSpec(inputs=[[0, "/a.mov"], [10, "/b.mov"], [TOTAL, None]], fps=FPS, kbps=1)
        self.assertEqual(chunks.analysis_plan(), (None, ()))
        self.assertIsNone(chunks.analysis_ranges())

    def test_no_range_is_ever_longer_than_the_cap(self):
        """Review-caught: the scene plan has no cap. A whole-file plan (no strong cut anywhere) and
        the live book's 135,050-frame Kurzgesagt segment must both come out in bounded ranges."""
        cap = NR.ANALYSIS_RANGE_MAX_FRAMES
        for total, bounds in ((108000, [108000]), (135050, [135050]), (5000, None),
                              (140000, [2000, 137050, 140000])):
            with self.subTest(total=total, bounds=bounds):
                ranges, joins = self._plan(total, bounds)
                # every forced split is a join; the scene plan's own bounds never are
                self.assertEqual(set(joins), {a for a, _b in ranges[1:]} - set(bounds or ()))
                self.assertLessEqual(max(b - a for a, b in ranges), cap)
        ranges, joins = self._plan(5000)                     # an even split, never a runt
        self.assertEqual((ranges, joins), ([(0, 2500), (2500, 5000)], (2500,)))
        self.assertLess(max(b - a for a, b in self._plan(135050, [135050])[0])
                        - min(b - a for a, b in self._plan(135050, [135050])[0]), 2)

    def test_a_long_range_splits_at_a_strong_cut_when_one_is_in_reach(self):
        # cuts at 1000 (too early: a runt) and 3000 (in the back half of the cap's window)
        ranges, joins = self._plan(8000, [8000], cuts=[1000, 3000, 7990])
        self.assertEqual(ranges[0], (0, 3000))              # a real shot boundary: no join
        self.assertNotIn(3000, joins)
        self.assertEqual(ranges[1:], [(3000, 5500), (5500, 8000)])   # nothing in reach: even + join
        self.assertEqual(joins, (5500,))

    def test_the_picture_chain_is_front_end_then_the_tv_lut_chain(self):
        self.assertEqual(self.spec.encode_vf(), dvnative.lut_vf(self.lut))
        fe = dataclasses.replace(self.spec, pre_vf="scale=3840:2160")
        self.assertEqual(fe.encode_vf(), "scale=3840:2160," + dvnative.lut_vf(self.lut))
        self.assertEqual(fe.analysis_vf(), dvnative.analysis_vf(self.lut, "scale=3840:2160"))
        self.assertEqual(dataclasses.replace(self.spec, lut=None).encode_vf(), "format=yuv420p10le")


class Builders(_Base):
    def test_encode_command(self):
        c = NR.build_encode_command("/ff", self.spec, "/b/encode.hevc", 14000, progress="/b/encode.progress")
        self.assertEqual(c[c.index("-i") + 1], self.src)
        self.assertEqual(c[c.index("-progress") + 1], "/b/encode.progress")
        self.assertEqual(c[c.index("-vf") + 1], "setpts=N*1001/24000/TB," + dvnative.lut_vf(self.lut))
        for flag, value in (("-fps_mode", "passthrough"), ("-c:v", "hevc_videotoolbox"),
                            ("-profile:v", "main10"), ("-pix_fmt", "p010le"), ("-b:v", "14000k"),
                            ("-color_primaries", "bt2020"),
                            ("-color_trc", "smpte2084"), ("-colorspace", "bt2020nc"), ("-color_range", "tv")):
            self.assertEqual(c[c.index(flag) + 1], value, flag)
        self.assertIn("-an", c)
        self.assertEqual(c[-3:], ["-f", "hevc", "/b/encode.hevc"])     # a raw Annex B ES
        self.assertNotIn("-tag:v", c)

    def test_sei_command_and_values(self):
        import dvcap
        c = NR.build_sei_command("/py", "/b/encode.hevc", "/b/encode.sei.hevc", dvcap.FALLBACK_MASTER_DISPLAY,
                                 "981,553")
        self.assertEqual(c, ["/py", "-m", "hdr10sei", "/b/encode.hevc", "/b/encode.sei.hevc",
                             "--mdcv", dvcap.FALLBACK_MASTER_DISPLAY, "--cll", "981,553"])
        self.assertEqual(NR.build_sei_command("/py", "/a", "/b", "X", None)[-1], "0,0")   # x265's "unknown"
        self.assertEqual(NR.hdr10_values(self.spec, {"max_cll": "981,553"}),
                         (dvcap.FALLBACK_MASTER_DISPLAY, "981,553"))
        m4000 = dvnative.MasterSpec(max_nits=4000, min_nits=0.005,
                                    mdcv="G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(40000000,50)")
        self.assertEqual(NR.hdr10_values(dataclasses.replace(self.spec, master=m4000), {})[0], m4000.mdcv)
        with self.assertRaises(ValueError):          # a non-default master must name its display
            dvnative.MasterSpec(max_nits=2000).hdr10_mdcv

    def test_encode_size_estimate(self):
        # 48 frames at 23.976 = 2.002 s; 14000 kb/s = 1.75 MB/s; x1.25 margin
        self.assertAlmostEqual(NR.encode_bytes_estimate(14000, 48, FPS), 14000 * 1000 / 8 * 48 * 1001 / 24000 * 1.25,
                               delta=1)

    def test_chunks_are_read_through_an_ffconcat_list(self):
        c = NR.build_encode_command("/ff", self.spec, "/o.mov", 9000, concat="/b/inputs.ffconcat")
        i = c.index("-i")
        self.assertEqual(c[i - 4:i + 2], ["-f", "concat", "-safe", "0", "-i", "/b/inputs.ffconcat"])
        self.assertEqual(NR.ffconcat_text(["/s/seg_0000.mov", "/s/Bob's/seg_0001.mov"]),
                         "ffconcat version 1.0\nfile '/s/seg_0000.mov'\nfile '/s/Bob'\\''s/seg_0001.mov'\n")

    def test_timeouts_and_progress_reading(self):
        self.assertEqual(NR.encode_timeout(0), 600)
        self.assertEqual(NR.encode_timeout(3), 602)
        prog = os.path.join(self.root, "p.txt")
        self.assertIsNone(NR.progress_frames(prog))
        with open(prog, "w") as f:
            f.write("frame=12\nfps=3.1\nprogress=continue\nframe=  30\nprogress=continue\n")
        self.assertEqual(NR.progress_frames(prog), 30)

    def test_the_disk_floor_speaks_of_ranges(self):
        hook = NR.floor_hook(self.root, "range", free_fn=lambda: 10 * 1024 ** 3)
        why = hook(1, 2000)
        self.assertTrue(why.startswith("paused: low disk — analysis range 2 needs"))
        self.assertIsNone(NR.floor_hook(self.root, "range", free_fn=lambda: 10 ** 15)(0, 2000))
        du = collections.namedtuple("du", "total used free")
        # what even an EMPTY volume could not hold is a plain failure, never an endless hold
        small = NR.floor_hook(self.root, "range", usage_fn=lambda: du(200 * 1024 ** 3, 0, 200 * 1024 ** 3))
        with self.assertRaisesRegex(RuntimeError, "more than this volume"):
            small(0, 20000)
        self.assertIsNone(small(0, 100))
        self.assertIsNone(NR.floor_hook(self.root, "range", usage_fn=lambda: (_ for _ in ()).throw(OSError()))(0, 9))

    def test_disk_reason(self):
        G = 1024 ** 3
        self.assertIsNone(NR.disk_reason(400 * G, 2000 * G, 100 * G, "x"))
        self.assertEqual(NR.disk_reason(200 * G, 2000 * G, 100 * G, "the DV render's finish"),
                         "paused: low disk — the DV render's finish needs ~100 GiB and would leave "
                         "100 GiB free, under the 150 GiB floor")
        with self.assertRaises(RuntimeError):
            NR.disk_reason(2000 * G, 300 * G, 200 * G, "x")
        self.assertIsNone(NR.disk_reason(400 * G, 0, 100 * G, "x"))      # size unknown: no never-fits

    def test_books_sit_beside_the_render_never_in_the_segdir(self):
        self.assertEqual(NR.books_dir(self.p), self.p.dv_render + ".dvrender")
        self.assertNotIn(self.p.segdir, NR.books_dir(self.p))


class SelfCheck(_Base):
    """self_check asks the orchestrator's own gates about the kept render, with `p` pointed at
    it, then reads its HDR10 static metadata back."""

    def setUp(self):
        super().setUp()
        import orchestrator as O
        self.O = O
        self.part = self.p.dv_render + ".part"
        self.seen = {}
        self.ok = {"dv": True, "complete": True, "fps": True}

        def complete(q):
            self.seen["complete"] = (q.dv_render, q.source_cfr, q.ep)
            return self.ok["complete"]
        self.patches = [
            mock.patch.object(dvcap, "count_hevc_frames", side_effect=lambda path, *a: self.frames),
            mock.patch.object(O, "_vstream", side_effect=lambda path: [{"path": path}]),
            mock.patch.object(O, "_is_dv81", side_effect=lambda s: self.ok["dv"]),
            mock.patch.object(O, "render_is_complete", side_effect=complete),
            mock.patch.object(O, "_fps_matches", side_effect=lambda src, r: self.ok["fps"]),
            mock.patch.object(O, "_nb_frames", return_value=TOTAL),
        ]
        self.frames = TOTAL
        self.hdr10 = {"master_display": dvcap.FALLBACK_MASTER_DISPLAY, "max_cll": "900,200"}
        self.patches.append(mock.patch.object(dvcap, "probe_video",
                                              side_effect=lambda path, *a: dict(self.hdr10, frames=TOTAL)))
        for pt in self.patches:
            pt.start()

    def tearDown(self):
        # innermost first: a subclass stacks a second patch of the same name on top of these,
        # and stopping in start order put the FIRST mock back for every later test module
        for pt in reversed(self.patches):
            pt.stop()
        super().tearDown()

    def test_all_gates_pass(self):
        ok, why = NR.self_check(self.p, self.part, TOTAL)
        self.assertTrue(ok, why)
        self.assertEqual(self.seen["complete"], (self.part, self.src, "S01E01"))   # the .part, not the render

    def test_each_gate_refuses(self):
        self.frames = TOTAL - 1
        self.assertIn("47 frames, the RPU 48", NR.self_check(self.p, self.part, TOTAL)[1])
        self.frames = TOTAL
        for gate, words in (("dv", "Dolby Vision 8.1"), ("complete", "whole source"), ("fps", "frame rate")):
            with self.subTest(gate=gate):
                self.ok = {"dv": True, "complete": True, "fps": True, gate: False}
                ok, why = NR.self_check(self.p, self.part, TOTAL)
                self.assertFalse(ok)
                self.assertIn(words, why)

    def test_the_hdr10_metadata_must_be_there_and_say_what_was_written(self):
        want = (dvcap.FALLBACK_MASTER_DISPLAY, "900,200")
        self.assertTrue(NR.self_check(self.p, self.part, TOTAL, hdr=want)[0])
        self.assertFalse(NR.self_check(self.p, self.part, TOTAL, hdr=(want[0], "1000,400"))[0])
        for missing in ("master_display", "max_cll"):
            with self.subTest(missing=missing):
                self.hdr10 = {"master_display": dvcap.FALLBACK_MASTER_DISPLAY, "max_cll": "900,200",
                              missing: None}
                ok, why = NR.self_check(self.p, self.part, TOTAL)
                self.assertFalse(ok)
                self.assertIn("HDR10", why)
        self.hdr10 = {"master_display": dvcap.FALLBACK_MASTER_DISPLAY, "max_cll": "0,0"}
        self.assertTrue(NR.self_check(self.p, self.part, TOTAL, hdr=(want[0], None))[0])   # unmeasured


class Render(_Base):
    """render() with every tool faked: each step writes what the real one would, and an encode
    writes its target bitrate into the file so the shipped render says which encode it was."""

    def setUp(self):
        super().setUp()
        self.an = os.path.join(self.root, "hdr_analyzer_mvp")
        with open(self.an, "w") as f:
            f.write("#!/bin/sh\n")
        self.peaks, self.calls, self.events, self.logs = [], [], [], []
        self.frames = {}
        self.fail_rc, self.raise_in = {}, {}
        self.check = (True, "checked")
        self.checked = []
        self.analyses = []
        self.measured = []
        self.source_frames = TOTAL
        self.encode_size = None              # bytes a fake encode claims (sparse), else its bitrate text
        self.disk = (10 ** 15, 10 ** 16)     # (free, total) of the scratch volume
        self.patches = [
            mock.patch.object(dvnative, "run_step", side_effect=self.fake_step),
            mock.patch.object(dvnative, "analyze_ranges", side_effect=self.fake_ranges),
            mock.patch.object(dvnative, "analyze_chunks", side_effect=self.fake_chunks),
            mock.patch.object(dvnative, "generate_rpu", side_effect=self.fake_generate),
            mock.patch.object(dvcap, "rpu_frame_count", side_effect=lambda path, **k: TOTAL),
            mock.patch.object(dvcap, "count_hevc_frames", side_effect=self.fake_count),
            mock.patch.object(dvcap, "video_peak_1s_mbps", side_effect=self.fake_peak),
            mock.patch.object(NR, "self_check", side_effect=self.fake_check),
            mock.patch.object(NR, "_disk", side_effect=lambda path, usage_fn=None: self.disk),
            mock.patch.object(NR.nativelane, "analyzer_path", return_value=self.an),
            mock.patch.object(NR.logbook, "event", side_effect=self.logs.append),
            mock.patch.object(NR.dvcap, "first_frame_hdr10", return_value=self.HDR),
            mock.patch.object(NR.dvcap, "ensure_pq_vui", side_effect=self.fake_vui),
        ]
        for pt in self.patches:
            pt.start()

    def tearDown(self):
        for pt in self.patches:
            pt.stop()
        super().tearDown()

    # ---- fakes ----
    def fake_step(self, cmd, *, timeout, abort=None, should_pause=None, what="step", on_poll=None, **_kw):
        self.assertEqual(cmd[:3], ["/usr/bin/nice", "-n", "10"])
        cmd = cmd[3:]
        self.calls.append((what, cmd))
        if what in self.raise_in:
            raise self.raise_in.pop(what)
        rc = self.fail_rc.get(what, 0)
        if rc:
            return rc, f"{what} said no", 0.1
        if what.startswith("encode attempt"):
            self.assertEqual(timeout, NR.encode_timeout(TOTAL))
            with open(cmd[-1], "w") as f:
                f.write(cmd[cmd.index("-b:v") + 1])
                if self.encode_size:
                    f.truncate(self.encode_size)
            with open(cmd[cmd.index("-progress") + 1], "w") as f:
                f.write("frame=24\nprogress=continue\n")
            on_poll()
        elif what == "HDR10 metadata":
            self.assertEqual(_kw.get("cwd"), dvnative.ENGINE_DIR)
            shutil.copyfile(cmd[3], cmd[4])
        elif what == "RPU inject":
            shutil.copyfile(cmd[cmd.index("-i") + 1], cmd[cmd.index("-o") + 1])
        elif what == "DV mux":                             # mkvmerge: the ES (last) -> -o
            shutil.copyfile(cmd[-1], cmd[cmd.index("-o") + 1])
        return 0, "", 0.5

    def fake_vui(self, es, *a, **k):
        self.vui_checked = getattr(self, "vui_checked", []) + [es]
        return getattr(self, "vui_result", (True, ""))

    def fake_peak(self, path, *a, **k):
        self.measured.append(path)
        return self.peaks.pop(0)

    def fake_ranges(self, path, ranges, workdir, **kw):
        self.analyses.append(("ranges", path, list(ranges), kw))
        kw["on_range"](len(ranges), len(ranges))
        return _sidecar(ranges)

    def fake_chunks(self, chunks, workdir, **kw):
        self.analyses.append(("chunks", chunks, kw))
        ends = [a for a, _p in chunks[1:]]
        return _sidecar(list(zip([0] + ends[:-1], ends)))

    def fake_generate(self, sc, rpu, **kw):
        self.assertIs(kw["master"], self.spec.master)
        with open(rpu, "w") as f:
            f.write("rpu")
        return {}

    def fake_count(self, path, *a):
        if path == self.src:
            return self.source_frames
        return self.frames.get(os.path.basename(path), TOTAL)

    def fake_check(self, p, path, total=None, hdr=None):
        self.checked.append((path, total, hdr))
        return self.check

    def render(self, spec=None, **kw):
        return NR.render(self.p, spec or self.spec, progress=self.events.append, **kw)

    def book(self, name):
        with open(os.path.join(NR.books_dir(self.p), name)) as f:
            return json.load(f)

    def whats(self):
        return [w for w, _c in self.calls]

    def shipped(self):
        with open(self.p.dv_render) as f:
            return f.read()

    def strays(self):
        return [n for n in os.listdir(self.root) if n.startswith("Show S01E01_dv.mov.")
                and not n.endswith(NR.BOOKS_SUFFIX)]

    def kept(self):
        return os.path.join(NR.books_dir(self.p), NR.KEPT)

    HDR = (dvcap.FALLBACK_MASTER_DISPLAY, "900,200")      # the fake sidecar's light levels
    FINISH = ["HDR10 metadata", "RPU inject", "DV mux"]

    # ---- the happy path ----
    def test_publishes_the_render_and_keeps_only_the_small_books(self):
        self.peaks = [40.0]
        ok, msg = self.render()
        self.assertTrue(ok, msg)
        self.assertEqual(self.whats(), ["encode attempt 1"] + self.FINISH)
        self.assertEqual(self.shipped(), "14000k")
        self.assertEqual(self.checked, [(self.kept(), TOTAL, self.HDR)])   # checked BEFORE the rename
        self.assertEqual(self.strays(), [])
        books = sorted(os.listdir(NR.books_dir(self.p)))
        self.assertEqual(books, ["cpu.json", "encode.json", "rpu.bin", "sidecar.json", "spec.json"])
        self.assertIn("native DV render: 48 frames, 2 shots, 14000 kb/s, peak 40.0 Mbps (under-cap)", msg)
        self.assertFalse(os.path.exists(os.path.join(self.p.segdir, "native")))
        cpu = self.book("cpu.json")
        self.assertEqual(cpu["encode"]["attempt_1"], {"cpu_s": 2.0, "frames": TOTAL})   # encode + finish

    def test_the_analysis_measures_the_scene_ranges_through_the_spec_chain(self):
        self.peaks = [40.0]
        self.render(dataclasses.replace(self.spec, pre_vf="scale=3840:2160"))
        kind, path, ranges, kw = self.analyses[0]
        self.assertEqual((kind, path, ranges), ("ranges", self.src, [(0, 20), (20, TOTAL)]))
        self.assertEqual(kw["joins"], ())                   # scene-plan ranges start on real cuts
        self.assertEqual((kw["cube"], kw["pre_vf"], kw["fps"], kw["analyzer"]), (self.lut, "scale=3840:2160", FPS, self.an))
        self.assertIs(kw["master"], self.spec.master)
        self.assertTrue(callable(kw["before_range"]))

    def test_finish_argv_is_the_hdr10_sei_dvcaps_inject_and_remuxs_dv_mux(self):
        self.peaks = [40.0]
        self.render()
        cmds = dict(self.calls)
        books = NR.books_dir(self.p)
        enc, sei = os.path.join(books, "encode.hevc"), os.path.join(books, "encode.sei.hevc")
        es_dv, part = (self.p.dv_render + s for s in (".dv.hevc", ".part"))
        self.assertEqual(cmds["HDR10 metadata"], NR.build_sei_command(sys.executable, enc, sei, *self.HDR))
        self.assertEqual(cmds["RPU inject"],
                         dvcap.build_inject_command(NR.DOVI_TOOL, enc, os.path.join(books, "rpu.bin"), es_dv))
        # the container's HDR10 description is read off the injected ES's own first frame
        self.assertEqual(cmds["DV mux"], remux.build_master_mux_command(
            NR.MKVMERGE, es_dv, FPS, part, colour=remux.mkv_colour_args(False, *self.HDR)))
        self.assertEqual(cmds["encode attempt 1"][-1], enc)

    def test_the_peak_is_measured_on_the_file_that_ships(self):
        """Review-caught: the encode before the inject read ~0.05 Mbps low. The gate now reads
        the finished render (RPU + SEI in), the very bytes remux_ship_render re-measures."""
        self.peaks = [40.0]
        self.render()
        self.assertEqual(self.measured, [self.p.dv_render + ".part"])
        self.assertLess(self.whats().index("DV mux"), len(self.whats()))

    def test_progress_runs_through_the_three_phases(self):
        self.peaks = [40.0]
        self.render()
        phases = [e["phase"] for e in self.events]
        self.assertEqual(phases[0], "analysis")
        self.assertEqual(phases[-1], "finish")
        self.assertIn({"stage": "dvrender", "ep": "S01E01", "phase": "encode", "pct": 72.5, "attempt": 1,
                       "kbps": 14000}, self.events)                       # frame 24 of 48, from -progress
        self.assertEqual(self.events[-1]["pct"], 100)

    # ---- the peak loop (render_under_cap's rules) ----
    def test_a_first_encode_under_the_cap_is_the_only_one(self):
        self.peaks = [57.4]                                # the gate is 50 x 1.15 (57.49999... in floats)
        self.assertTrue(self.render()[0])
        self.assertEqual(self.whats().count("encode attempt 1"), 1)
        self.assertNotIn("encode attempt 2", self.whats())
        self.assertTrue(any("RENDER_PEAK 57.4" in l for l in self.logs))

    def test_a_won_gamble_ships_the_re_encode_and_keeps_no_stray(self):
        self.peaks = [59.9, 44.0]
        ok, _msg = self.render()
        self.assertTrue(ok)
        want = peakplan.reexport_kbps(14000, 59.9, 50)
        self.assertEqual(self.shipped(), f"{want}k")
        self.assertFalse(os.path.exists(os.path.join(NR.books_dir(self.p), NR.BEST)))
        self.assertEqual(self.whats(), (["encode attempt 1"] + self.FINISH + ["encode attempt 2"]
                                        + self.FINISH))
        self.assertTrue(any(f"re-encoding at {want} kb/s" in l for l in self.logs))
        final = self.book("encode.json")["final"]
        self.assertEqual((final["kbps"], final["verdict"], final["attempts"]), (want, "under-cap", 2))

    def test_a_lost_gamble_leaves_the_FIRST_encode_for_the_fallback(self):
        spec = dataclasses.replace(self.spec, kbps=20000)
        self.peaks = [60.0, 60.0, 60.0]                    # never under the 57.5 gate
        ok, msg = self.render(spec)
        self.assertTrue(ok, msg)                           # the remux's x265 path takes it from here
        self.assertEqual([w for w in self.whats() if w.startswith("encode")],
                         ["encode attempt 1", "encode attempt 2", "encode attempt 3"])
        second = peakplan.reexport_kbps(20000, 60.0, 50)
        third = peakplan.reexport_kbps(second, 60.0, 50, prev=(20000, 60.0))
        self.assertIsNotNone(peakplan.reexport_kbps(third, 60.0, 50, prev=(second, 60.0)))   # out of tries, not floor
        targets = [c[c.index("-b:v") + 1] for w, c in self.calls if w.startswith("encode")]
        self.assertEqual(targets, ["20000k", f"{second}k", f"{third}k"])
        self.assertEqual(self.shipped(), "20000k", "the fallback must get the best encode, not the last")
        self.assertIn("(over-cap)", msg)
        self.assertTrue(any("RENDER_OVER_CAP" in l and "no re-encodes left" in l and "first render" in l
                            for l in self.logs))
        self.assertEqual(self.book("encode.json")["final"]["kbps"], 20000)

    def test_a_decline_under_the_floor_keeps_the_first_and_says_why(self):
        self.peaks = [300.0]
        self.assertTrue(self.render()[0])
        self.assertEqual(self.whats().count("encode attempt 1"), 1)
        self.assertTrue(any("under 8000 kb/s" in l for l in self.logs))

    def test_an_unmeasured_peak_keeps_the_best_encode(self):
        self.peaks = [59.9, 0.0]
        ok, msg = self.render()
        self.assertTrue(ok)
        self.assertEqual(self.shipped(), "14000k")
        self.assertIn("(unmeasured)", msg)

    def test_no_cap_means_no_peak_measurement(self):
        ok, msg = self.render(dataclasses.replace(self.spec, cap_mbps=0))
        self.assertTrue(ok)
        self.assertIn("no peak gate", msg)                 # video_peak_1s_mbps would pop an empty list

    def test_an_encode_of_the_wrong_length_fails(self):
        self.frames["encode.hevc"] = TOTAL - 1
        ok, msg = self.render()
        self.assertFalse(ok)
        self.assertIn("the encode has 47 frames, the source 48", msg)

    # ---- atomicity ----
    def test_a_failed_self_check_publishes_nothing(self):
        self.peaks = [40.0]
        self.check = (False, "it carries no Dolby Vision 8.1 configuration record")
        ok, msg = self.render()
        self.assertFalse(ok)
        self.assertEqual(msg, "native render failed: self-check: it carries no Dolby Vision 8.1 "
                              "configuration record")
        self.assertFalse(os.path.exists(self.p.dv_render))
        self.assertEqual(self.strays(), [])

    def test_a_failed_mux_leaves_no_render_and_no_strays(self):
        self.peaks = [40.0]
        self.fail_rc["DV mux"] = 2                         # mkvmerge's error (1 = warnings: fine)
        ok, msg = self.render()
        self.assertFalse(ok)
        self.assertIn("DV mux failed (rc=2): DV mux said no", msg)
        self.assertFalse(os.path.exists(self.p.dv_render))
        self.assertEqual(self.strays(), [])

    def test_strays_from_a_killed_attempt_are_swept_first(self):
        for s in (".part", ".dv.hevc"):
            with open(self.p.dv_render + s, "w") as f:
                f.write("stale")
        self.peaks = [40.0]
        self.assertTrue(self.render()[0])
        self.assertEqual(self.strays(), [])
        self.assertEqual(self.shipped(), "14000k")

    def test_a_retry_after_a_failed_finish_re_encodes_but_never_re_analyses(self):
        # the encode is deleted once the RPU is in (two encode-sized files at most), so a finish
        # cut short costs its encode; the analysis — the long part — is kept
        self.peaks = [40.0]
        self.fail_rc["RPU inject"] = 1
        ok, msg = self.render()
        self.assertFalse(ok)
        self.assertIn("RPU inject failed (rc=1)", msg)
        self.assertEqual(self.strays(), [])
        self.calls.clear()
        del self.fail_rc["RPU inject"]
        ok, msg = self.render()
        self.assertTrue(ok, msg)
        self.assertEqual(self.whats(), ["encode attempt 1"] + self.FINISH)
        self.assertEqual(len(self.analyses), 1)

    def test_a_kept_render_survives_a_stop_before_its_publish(self):
        self.peaks = [40.0]
        with mock.patch.object(NR, "_publish", side_effect=dvnative.Aborted("stop")):
            self.assertEqual(self.render(), (False, "aborted (run stopped)"))
        self.assertTrue(os.path.exists(self.kept()))
        self.calls.clear()
        ok, msg = self.render()
        self.assertTrue(ok, msg)
        self.assertEqual(self.calls, [])                   # nothing encoded or finished again
        self.assertEqual(self.shipped(), "14000k")

    # ---- the disk floor before each big write ----
    def test_an_encode_waits_for_room_for_itself_and_its_finish(self):
        G = 1024 ** 3
        self.disk = (NR.DISK_FLOOR_GB * G + 1024 ** 2, 2000 * G)
        ok, msg = self.render()
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("paused: low disk — encode attempt 1 and its finish needs"), msg)
        self.assertNotIn("encode attempt 1", self.whats())

    def test_a_finish_waits_for_room_for_its_transients(self):
        G = 1024 ** 3
        self.encode_size = 400 * 1024 ** 2                 # a sparse 400 MiB "encode"
        self.disk = (NR.DISK_FLOOR_GB * G + 100 * 1024 ** 2, 2000 * G)
        ok, msg = self.render()
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("paused: low disk — the DV render's finish needs"), msg)
        self.assertEqual(self.whats(), ["encode attempt 1"])
        self.assertFalse(os.path.exists(self.p.dv_render))

    def test_what_no_volume_could_hold_is_a_plain_failure(self):
        G = 1024 ** 3
        self.disk = (10 * G, 100 * G)                      # smaller than the floor itself
        ok, msg = self.render()
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("native render failed:"), msg)
        self.assertIn("more than this volume", msg)

    # ---- abort and pause ----
    def test_abort_mid_encode_is_not_a_failure_and_publishes_nothing(self):
        self.raise_in["encode attempt 1"] = dvnative.Aborted("aborted (run stopped) during encode attempt 1")
        ok, msg = self.render()
        self.assertEqual((ok, msg), (False, "aborted (run stopped)"))
        self.assertFalse(os.path.exists(self.p.dv_render))

    def test_abort_between_phases(self):
        ev = threading.Event()
        real = self.fake_ranges

        def ranges_then_stop(*a, **kw):
            out = real(*a, **kw)
            ev.set()
            return out
        with mock.patch.object(dvnative, "analyze_ranges", side_effect=ranges_then_stop):
            ok, msg = self.render(abort=ev)
        self.assertEqual((ok, msg), (False, "aborted (run stopped)"))
        self.assertEqual(self.calls, [])

    def test_a_pause_mid_encode_resumes_without_re_analysing(self):
        self.raise_in["encode attempt 1"] = dvnative.Paused("paused: yielded during encode attempt 1 — it "
                                                            "restarts on resume")
        ok, msg = self.render()
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("paused: yielded during encode attempt 1"))
        self.peaks = [40.0]
        ok, msg = self.render()
        self.assertTrue(ok, msg)
        self.assertEqual(len(self.analyses), 1)            # the RPU was kept
        self.assertEqual(self.whats().count("encode attempt 1"), 2)

    def test_should_pause_yields_before_the_encode(self):
        ok, msg = self.render(should_pause=lambda: True)
        self.assertEqual((ok, msg), (False, "paused: yielded before encode attempt 1"))

    def test_a_paused_analysis_comes_back_as_paused(self):
        with mock.patch.object(dvnative, "analyze_ranges",
                               side_effect=dvnative.Paused("paused: low disk — analysis range 2 needs ~9 GiB")):
            ok, msg = self.render()
        self.assertEqual((ok, msg), (False, "paused: low disk — analysis range 2 needs ~9 GiB"))

    def test_an_analysis_failure_is_a_plain_reason(self):
        with mock.patch.object(dvnative, "analyze_ranges",
                               side_effect=RuntimeError("range 1: analyzer failed: boom")):
            ok, msg = self.render()
        self.assertEqual((ok, msg), (False, "native render failed: range 1: analyzer failed: boom"))

    # ---- what the books were made from ----
    def test_a_new_bitrate_re_encodes_but_keeps_the_analysis(self):
        self.peaks = [40.0]
        self.render()
        os.remove(self.p.dv_render)
        self.peaks = [40.0]
        ok, _msg = self.render(dataclasses.replace(self.spec, kbps=12000))
        self.assertTrue(ok)
        self.assertEqual(len(self.analyses), 1)
        self.assertEqual(self.shipped(), "12000k")

    def test_a_new_front_end_or_source_re_analyses(self):
        self.peaks = [40.0]
        self.render()
        os.remove(self.p.dv_render)
        self.peaks = [40.0]
        self.render(dataclasses.replace(self.spec, pre_vf="scale=3840:2160"))
        self.assertEqual(len(self.analyses), 2)
        os.remove(self.p.dv_render)
        os.utime(self.src, ns=(1, 1))                      # the source changed under the books
        self.peaks = [40.0]
        self.render(dataclasses.replace(self.spec, pre_vf="scale=3840:2160"))
        self.assertEqual(len(self.analyses), 3)

    # ---- refusals ----
    def test_an_unusable_spec_or_missing_source_is_refused_up_front(self):
        ok, msg = self.render(dataclasses.replace(self.spec, kbps=0))
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("native render spec is unusable"))
        os.remove(self.src)
        ok, msg = self.render()
        self.assertFalse(ok)
        self.assertIn("input is missing", msg)
        self.assertEqual(self.calls, [])

    def test_a_source_of_another_length_is_refused_before_any_work(self):
        self.source_frames = TOTAL + 5
        ok, msg = self.render()
        self.assertEqual((ok, msg), (False, "native render source has 53 frames, the spec 48"))
        self.assertEqual(self.analyses, [])

    def test_a_render_already_in_place_is_kept(self):
        with open(self.p.dv_render, "w") as f:
            f.write("rendered")
        ok, msg = self.render()
        self.assertEqual((ok, msg), (True, "native DV render already in place"))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.shipped(), "rendered")

    # ---- Topaz chunks ----
    def test_chunks_are_analysed_per_chunk_and_encoded_through_concat(self):
        a, b = os.path.join(self.root, "seg_0000.mov"), os.path.join(self.root, "seg_0001.mov")
        for path in (a, b):
            with open(path, "wb") as f:
                f.write(b"x")
        spec = NR.RenderSpec(inputs=[[0, a], [30, b], [TOTAL, None]], fps=FPS, kbps=60000, cap_mbps=0,
                             lut=self.lut)
        ok, msg = self.render(spec)
        self.assertTrue(ok, msg)
        self.assertEqual(self.analyses[0][:2], ("chunks", [[0, a], [30, b], [TOTAL, None]]))
        enc = dict(self.calls)["encode attempt 1"]
        lst = enc[enc.index("-i") + 1]
        self.assertEqual(enc[enc.index("-i") - 4:enc.index("-i")], ["-f", "concat", "-safe", "0"])
        self.assertFalse(os.path.exists(lst))              # the list goes with the encode
        self.assertEqual(self.checked, [(self.kept(), TOTAL, self.HDR)])

    def test_discard_books_removes_books_and_strays_never_the_render(self):
        self.peaks = [40.0]
        self.render()
        with open(self.p.dv_render + ".part", "w") as f:
            f.write("x")
        NR.discard_books(self.p)
        self.assertFalse(os.path.exists(NR.books_dir(self.p)))
        self.assertEqual(self.strays(), [])
        self.assertTrue(os.path.exists(self.p.dv_render))


def _tools_present() -> bool:
    return all(os.path.exists(t) for t in (NR.FFMPEG, NR.FFPROBE, NR.DOVI_TOOL, NR.MKVMERGE))


@unittest.skipUnless(_tools_present(), "ffmpeg/ffprobe/dovi_tool/mkvmerge not installed")
class VideoToolboxRender(unittest.TestCase):
    """REAL tools end to end (the analysis alone faked — the analyzer is pinned and tested on
    its own): a tiny noisy BT.709 clip through the LUT chain, hevc_videotoolbox, dovi_tool
    generate + inject-rpu and mkvmerge must come out as a render the orchestrator's own gates
    accept — Matroska under its .mov name — at a path whose name has characters a muxer's
    argument parsing could trip on."""

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        cls.src = os.path.join(cls.d, "src.mp4")
        r = subprocess.run([NR.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            "testsrc2=size=1280x720:rate=24000/1001,noise=alls=40:allf=t",
                            "-frames:v", str(TOTAL), "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p",
                            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                            "-color_range", "tv", cls.src], capture_output=True)
        if r.returncode != 0:
            raise unittest.SkipTest("libx264 unavailable")
        probe = subprocess.run([NR.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", cls.src,
                                "-frames:v", "1", "-c:v", "hevc_videotoolbox", "-profile:v", "main10",
                                "-pix_fmt", "p010le", "-f", "null", "-"], capture_output=True)
        if probe.returncode != 0:
            raise unittest.SkipTest("hevc_videotoolbox Main10 unavailable")
        cls.cube = os.path.join(cls.d, "id.cube")             # identity: the chain, not a look
        with open(cls.cube, "w") as f:
            f.write("LUT_3D_SIZE 2\n" + "".join(f"{r} {g} {b}\n" for b in (0, 1) for g in (0, 1) for r in (0, 1)))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, ignore_errors=True)

    def test_render_is_dv81_whole_and_at_the_source_rate(self):
        p = types.SimpleNamespace(ep="T01", dv_render=os.path.join(self.d, "Show, The + #1 S01E01_dv.mov"),
                                  source_cfr=self.src, combine=False)
        spec = NR.RenderSpec(inputs=[[0, self.src], [TOTAL, None]], fps=FPS, kbps=20000, cap_mbps=50,
                             lut=self.cube, pre_vf="scale=1920:1080", bounds=[20, TOTAL])
        seen = []

        def analysis(path, ranges, workdir, **kw):
            seen.append(list(ranges))
            return _sidecar(ranges)
        with mock.patch.object(dvnative, "analyze_ranges", side_effect=analysis):
            ok, msg = NR.render(p, spec)
        self.assertTrue(ok, msg)
        self.assertEqual(seen, [[(0, 20), (20, TOTAL)]])
        out = subprocess.run([NR.FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_packets",
                              "-show_streams", "-of", "json", p.dv_render], capture_output=True, text=True).stdout
        s = json.loads(out)["streams"][0]
        self.assertEqual((s["codec_name"], s["profile"]), ("hevc", "Main 10"))
        self.assertEqual((s["color_transfer"], s["color_primaries"], s["width"]), ("smpte2084", "bt2020", 1920))
        self.assertEqual((s["r_frame_rate"], int(s["nb_read_packets"])), (FPS, TOTAL))
        dv = [x for x in s.get("side_data_list", []) if x.get("side_data_type") == "DOVI configuration record"]
        self.assertEqual((dv[0]["dv_profile"], dv[0]["dv_bl_signal_compatibility_id"]), (8, 1))
        rpu = os.path.join(self.d, "back.rpu")
        ok_rpu, why = dvcap.extract_rpu(p.dv_render, rpu)
        self.assertTrue(ok_rpu, why)
        self.assertEqual(dvcap.rpu_frame_count(rpu), TOTAL)
        self.assertEqual(NR.self_check(p, p.dv_render, TOTAL)[0], True)
        self.assertEqual(sorted(n for n in os.listdir(self.d) if n.startswith("Show, The")),
                         ["Show, The + #1 S01E01_dv.mov", "Show, The + #1 S01E01_dv.mov.dvrender"])
        # HDR10 static metadata, as the x265 path writes it: read back the way the remux reads it
        # (first-frame side data), and in-band on EVERY keyframe (the SEI itself, not a box)
        info = dvcap.probe_video(p.dv_render)
        self.assertEqual((info["master_display"], info["max_cll"]), (dvcap.FALLBACK_MASTER_DISPLAY, "900,200"))
        trace = subprocess.run([NR.FFMPEG, "-hide_banner", "-i", p.dv_render, "-map", "0:v:0", "-c", "copy",
                                "-bsf:v", "trace_headers", "-f", "null", "-"],
                               capture_output=True, text=True).stderr
        keys = int(json.loads(subprocess.run(
            [NR.FFPROBE, "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey", "-count_frames",
             "-show_entries", "stream=nb_read_frames", "-of", "json", p.dv_render],
            capture_output=True, text=True).stdout)["streams"][0]["nb_read_frames"])
        self.assertGreaterEqual(keys, 1)
        self.assertEqual(trace.count("Mastering Display Colour Volume"), keys)
        self.assertEqual(trace.count("Content Light Level Information"), keys)



# ---- HDR intake (2026-10-18): an HDR source's own light and mastering; keep_video ------------

P3_4000 = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(40000000,50)"
HAMILTON_MDCV = "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)"


def _own_tests_only(cls):
    """Class decorator: `cls` runs only the tests it defines itself. Inheriting Render (or
    SelfCheck) brings its set-up and fakes, and every one of its tests too, which would re-run
    under the subclass's spec; those are blanked (unittest collects only callable test_*)."""
    for base in cls.__mro__[1:]:
        for name in vars(base):
            if name.startswith("test") and name not in vars(cls):
                setattr(cls, name, None)
    return cls


def _hdr_spec(src, **kw):
    master, light = dvnative.source_mastering(P3_4000, "850,210")
    base = dict(inputs=[[0, src], [TOTAL, None]], fps=FPS, kbps=60000, cap_mbps=0, lut=None,
                master=master, light=light)
    base.update(kw)
    return NR.RenderSpec(**base)


class HdrSpec(_Base):
    def test_light_and_keep_video_are_checked(self):
        self.assertEqual(_hdr_spec(self.src).problems(), [])
        self.assertEqual(_hdr_spec(self.src).light_text(), "850,210")
        self.assertEqual(_hdr_spec(self.src, light=[0, 0]).light, (0, 0))   # a list becomes a tuple
        self.assertIsNone(self.spec.light_text())
        for light in ((1,), (1, -1), (1.5, 2), (True, 1), (70000, 1), "1,2"):
            with self.subTest(light=light):
                self.assertIn("light", " ".join(_hdr_spec(self.src, light=light).problems()))
        self.assertEqual(_hdr_spec(self.src, keep_video=True).problems(), [])
        for kw in ({"lut": self.lut}, {"pre_vf": "scale=1920:1080"}, {"cap_mbps": 50},
                   {"inputs": [[0, self.src], [20, self.src], [TOTAL, None]]}):
            with self.subTest(kw=kw):
                self.assertIn("keep_video", " ".join(_hdr_spec(self.src, keep_video=True, **kw).problems()))

    def test_the_hdr_knobs_are_booked_only_when_set(self):
        stats = NR.input_stats(self.spec)
        sdr_a, sdr_e = NR.fingerprints(self.spec, stats)
        self.assertFalse({"light", "keep_video"} & (set(sdr_a) | set(sdr_e)))   # SDR prints unchanged
        a, e = NR.fingerprints(_hdr_spec(self.src, keep_video=True), stats)
        self.assertEqual((a["light"], e["light"], e["keep_video"]), ([850, 210], [850, 210], True))
        self.assertEqual(a["master"]["mdcv"], P3_4000)
        a2, e2 = NR.fingerprints(_hdr_spec(self.src, light=(0, 0)), stats)
        self.assertNotEqual(a, a2)                        # other light -> another RPU: re-analysed
        self.assertNotIn("keep_video", e2)


@_own_tests_only
class HdrLightRender(Render):
    """A spec with an HDR source's own light: L6 (generate's `light`), the HDR10 SEI the finish
    writes and the self-check all carry exactly it — never the measured, clamped values."""

    def setUp(self):
        super().setUp()
        self.spec = _hdr_spec(self.src)
        self.gen_kw = []

    def fake_generate(self, sc, rpu, **kw):
        self.gen_kw.append(kw)
        return super().fake_generate(sc, rpu, **kw)

    def test_light_reaches_the_rpu_the_sei_and_the_check(self):
        ok, msg = self.render()
        self.assertTrue(ok, msg)
        self.assertEqual(self.gen_kw[0]["light"], (850, 210))
        sei = next(c for w, c in self.calls if w == "HDR10 metadata")
        self.assertEqual(sei[sei.index("--mdcv") + 1:], [P3_4000, "--cll", "850,210"])
        self.assertEqual(self.checked, [(self.kept(), TOTAL, (P3_4000, "850,210"))])
        self.assertEqual(self.book("spec.json")["rpu"]["max_cll"], "850,210")
        self.assertEqual(self.whats(), ["encode attempt 1"] + self.FINISH)     # no gate: one encode
        self.assertEqual(self.measured, [])

    def test_without_light_generate_is_called_exactly_as_before(self):
        self.spec = dataclasses.replace(self.spec, light=None, master=dvnative.DEFAULT_MASTER)
        ok, msg = self.render()
        self.assertTrue(ok, msg)
        self.assertNotIn("light", self.gen_kw[0])
        self.assertEqual(self.checked[0][2], (dvcap.FALLBACK_MASTER_DISPLAY, "900,200"))   # measured


@_own_tests_only
class KeepVideoRender(Render):
    """keep_video (rpu_only): the render is the source's own coded video — a stream copy, the RPU
    injected, the DV mux — with no encode, no SEI written, no peak gate, and a self-check that
    demands the source's own HDR10 metadata."""

    def setUp(self):
        super().setUp()
        master, light = dvnative.source_mastering(HAMILTON_MDCV, "0,0")
        self.spec = NR.RenderSpec(inputs=[[0, self.src], [TOTAL, None]], fps=FPS,
                                  kbps=60000, cap_mbps=0, master=master, light=light, keep_video=True)

    def fake_step(self, cmd, *, timeout, abort=None, should_pause=None, what="step", on_poll=None, **kw):
        if what == "stream copy":
            self.assertEqual(cmd[:3], ["/usr/bin/nice", "-n", "10"])
            self.calls.append((what, cmd[3:]))
            if what in self.raise_in:
                raise self.raise_in.pop(what)
            self.assertEqual(timeout, dvnative.step_timeout(TOTAL))
            shutil.copyfile(self.src, cmd[-1])                 # the original bits, as they are
            with open(cmd[cmd.index("-progress") + 1], "w") as f:
                f.write("frame=24\nprogress=continue\n")
            on_poll()
            return 0, "", 0.25
        return super().fake_step(cmd, timeout=timeout, abort=abort, should_pause=should_pause,
                                 what=what, on_poll=on_poll, **kw)

    def fake_check(self, p, path, total=None, hdr=None, **kw):
        self.checked.append((path, total, hdr, kw))
        return self.check

    def test_the_render_is_the_sources_own_stream_with_the_rpu(self):
        ok, msg = self.render()
        self.assertTrue(ok, msg)
        self.assertEqual(self.whats(), ["stream copy", "RPU inject", "DV mux"])
        self.assertEqual(self.shipped(), "src" * 100)
        self.assertEqual(self.measured, [])
        self.assertEqual(self.checked, [(self.kept(), TOTAL, None, {"as_source": True})])
        self.assertIn("the source's own video with the RPU injected (no encode)", msg)
        self.assertEqual(self.book("encode.json")["final"]["verdict"], "source-video")
        self.assertEqual(sorted(self.book("cpu.json")["encode"]), ["copy"])
        self.assertEqual(self.strays(), [])
        self.assertEqual(sorted(os.listdir(NR.books_dir(self.p))),
                         ["cpu.json", "encode.json", "rpu.bin", "sidecar.json", "spec.json"])
        self.assertTrue(any("source's own video (no encode), L6 MaxCLL/MaxFALL 0,0" in m for m in self.logs))
        self.assertEqual(self.analyses[0][3]["cube"], None)            # measured as it is: no LUT
        self.assertEqual(self.analyses[0][3]["pre_vf"], None)

    def test_the_copy_touches_no_picture(self):
        self.render()
        cmd = dict(self.calls)["stream copy"]
        bsf = "hevc_mp4toannexb"                  # the VUI is ensure_pq_vui's job, after the copy
        self.assertEqual(cmd[cmd.index("-i"):], ["-i", self.src, "-map", "0:v:0", "-c:v", "copy",
                                                 "-bsf:v", bsf, "-f", "hevc",
                                                 os.path.join(NR.books_dir(self.p), NR.ENCODE)])
        self.assertEqual(NR.build_copy_command("ff", "s", "o"),
                         ["ff", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", "s",
                          "-map", "0:v:0", "-c:v", "copy", "-bsf:v", bsf, "-f", "hevc", "o"])

    def test_a_copy_of_another_length_publishes_nothing(self):
        self.frames[NR.ENCODE] = TOTAL - 1
        ok, msg = self.render()
        self.assertFalse(ok)
        self.assertIn("the source's stream has 47 frames, the RPU 48", msg)
        self.assertFalse(os.path.exists(self.p.dv_render))
        self.assertEqual(self.strays(), [])

    def test_the_copy_is_checked_for_a_pq_vui_and_a_failed_rewrite_publishes_nothing(self):
        # mkvmerge derives the DV record's compatibility id from the VUI: a base that does not say
        # BT.2020/PQ would mux as 8.2. ensure_pq_vui runs on the COPY, after the frame count
        self.vui_result = (False, "the stream's VUI does not say BT.2020/PQ and rewriting it failed")
        ok, msg = self.render()
        self.assertFalse(ok)
        self.assertIn("VUI does not say BT.2020/PQ", msg)
        self.assertEqual(self.vui_checked, [os.path.join(NR.books_dir(self.p), NR.ENCODE)])
        self.assertFalse(os.path.exists(self.p.dv_render))

    def test_room_is_asked_for_the_copy_and_its_inject(self):
        size = os.path.getsize(self.src)
        floor = NR.DISK_FLOOR_GB * NR.GiB
        self.disk = (floor + int(size * 2.1) - 1, 10 ** 16)       # one byte short of copy + inject
        ok, msg = self.render()
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("paused: low disk — the source's stream copy and its RPU inject"), msg)
        self.disk = (floor + int(size * 2.1) + 10 ** 6, 10 ** 16)
        self.assertTrue(self.render()[0])

    def test_a_render_in_place_is_judged_against_the_source(self):
        with open(self.p.dv_render, "w") as f:
            f.write("done")
        ok, msg = self.render()
        self.assertEqual((ok, msg), (True, "native DV render already in place"))
        self.assertEqual(self.checked, [(self.p.dv_render, None, None, {"as_source": True})])

    def test_a_source_of_unknown_light_gets_the_measured_level_in_its_sei_too(self):
        # stages.declared_light: a 0,0 source's route passes light None — measured, as Resolve did;
        # the SEI then says the same as L6, and the self-check holds the render to that
        import dataclasses
        spec = self.spec = dataclasses.replace(self.spec, light=None)
        self.assertFalse(NR.as_source(spec))
        ok, msg = self.render(spec)
        self.assertTrue(ok, msg)
        self.assertEqual(self.whats(), ["stream copy", "HDR10 metadata", "RPU inject", "DV mux"])
        sei = next(c for w, c in self.calls if w == "HDR10 metadata")
        self.assertEqual(sei[sei.index("--mdcv"):], ["--mdcv", HAMILTON_MDCV, "--cll", "900,200"])
        self.assertEqual(self.checked, [(self.kept(), TOTAL, (HAMILTON_MDCV, "900,200"), {})])
        self.assertTrue(any("L6 MaxCLL/MaxFALL measured 900,200" in m for m in self.logs))
        self.checked.clear()
        self.assertEqual(self.render(spec), (True, "native DV render already in place"))
        self.assertEqual(self.checked, [(self.p.dv_render, None, None, {})])

    def test_a_stop_during_the_copy_publishes_nothing(self):
        self.raise_in["stream copy"] = dvnative.Aborted("stopped")
        self.assertEqual(self.render(), (False, "aborted (run stopped)"))
        self.assertFalse(os.path.exists(self.p.dv_render))


@_own_tests_only
class SelfCheckAsSource(SelfCheck):
    """as_source: the render's HDR10 metadata must be exactly the source's own — none when it
    has none — instead of 'there, and what the finish wrote'."""

    def setUp(self):
        super().setUp()
        self.by_path = {self.part: (HAMILTON_MDCV, "0,0"), self.src: (HAMILTON_MDCV, "0,0")}
        pt = mock.patch.object(dvcap, "probe_video", side_effect=lambda path, *a: {
            "frames": TOTAL, "master_display": self.by_path[path][0], "max_cll": self.by_path[path][1]})
        pt.start()
        self.patches.append(pt)          # stopped FIRST by SelfCheck.tearDown (it stacks on its patch)

    def test_the_sources_own_metadata_passes_whatever_it_is(self):
        self.assertTrue(NR.self_check(self.p, self.part, TOTAL, as_source=True)[0])
        self.by_path = {self.part: (None, None), self.src: (None, None)}         # a source with no SEI
        ok, why = NR.self_check(self.p, self.part, TOTAL, as_source=True)
        self.assertTrue(ok, why)
        self.assertIn("source's own picture", why)
        self.assertFalse(NR.self_check(self.p, self.part, TOTAL)[0])             # the old rule refuses it

    def test_anything_else_is_refused(self):
        for render in ((None, None), (HAMILTON_MDCV, "100,10"), (dvcap.FALLBACK_MASTER_DISPLAY, "0,0")):
            with self.subTest(render=render):
                self.by_path = {self.part: render, self.src: (HAMILTON_MDCV, "0,0")}
                ok, why = NR.self_check(self.p, self.part, TOTAL, as_source=True)
                self.assertFalse(ok)
                self.assertIn("the source's own", why)


def _vcl_md5(path):
    """md5 over every VCL NAL unit of `path`'s HEVC stream (the coded picture), in order."""
    import hashlib, re
    data = subprocess.run([NR.FFMPEG, "-v", "error", "-i", path, "-map", "0:v:0", "-c", "copy",
                           "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"], capture_output=True).stdout
    h, starts = hashlib.md5(), [m.end() for m in re.finditer(b"\x00\x00\x01", data)]
    for i, a in enumerate(starts):
        nal = data[a:(starts[i + 1] - 3 if i + 1 < len(starts) else len(data))].rstrip(b"\x00")
        if nal and (nal[0] >> 1) & 0x3F < 32:
            h.update(nal)
    return h.hexdigest()


class KeepVideoRealTools(unittest.TestCase):
    """REAL tools (the analysis alone faked): a tiny HDR10 HEVC Main10 clip with its own
    mastering-display and content-light SEI through a keep_video spec must come out as a render
    the orchestrator's gates accept — DV 8.1, every frame — whose coded picture is the source's
    bit for bit, whose HDR10 SEI is still the source's own, and whose RPU declares that same
    mastering and light in L6."""

    MD = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"

    @classmethod
    def _make(cls, name, cll):
        path = os.path.join(cls.d, name)
        r = subprocess.run([NR.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            "testsrc2=size=1280x720:rate=24000/1001,noise=alls=40:allf=t",   # over 1 MB:
                            "-frames:v", str(TOTAL), "-c:v", "libx265", "-preset", "ultrafast",  # _vstream's floor
                            "-crf", "12", "-pix_fmt", "yuv420p10le",
                            "-x265-params", f"log-level=error:hdr10=1:repeat-headers=1:"
                                            f"master-display={cls.MD}:max-cll={cll}",
                            "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
                            "-color_range", "tv", "-tag:v", "hvc1", path], capture_output=True)
        return path if r.returncode == 0 else None

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        cls.src = cls._make("src.mp4", "500,100")
        cls.unknown = cls._make("unknown.mp4", "0,0")       # a WEB source that says "unknown" (Hamilton)
        if (not (cls.src and cls.unknown) or not os.path.exists(dvnative.DOVI_TOOL)
                or not os.path.exists(NR.MKVMERGE)):
            raise unittest.SkipTest("libx265 (10-bit), dovi_tool or mkvmerge unavailable")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, ignore_errors=True)

    def test_the_render_is_the_source_bit_for_bit_with_a_matching_rpu(self):
        p = types.SimpleNamespace(ep="T01", dv_render=os.path.join(self.d, "Movie, The + #1_dv.mov"),
                                  source_cfr=self.src, combine=False)
        src_info = dvcap.probe_video(self.src)
        self.assertEqual((src_info["master_display"], src_info["max_cll"]), (self.MD, "500,100"))
        master, light = dvnative.source_mastering(src_info["master_display"], src_info["max_cll"])
        spec = NR.RenderSpec(inputs=[[0, self.src], [TOTAL, None]], fps=FPS, kbps=60000, cap_mbps=0,
                             master=master, light=light, keep_video=True)
        with mock.patch.object(dvnative, "analyze_ranges",
                               side_effect=lambda path, ranges, workdir, **kw: _sidecar(ranges)):
            ok, msg = NR.render(p, spec)
        self.assertTrue(ok, msg)
        self.assertEqual(NR.self_check(p, p.dv_render, TOTAL, as_source=True)[0], True)
        self.assertEqual(_vcl_md5(p.dv_render), _vcl_md5(self.src))
        info = dvcap.probe_video(p.dv_render)
        self.assertEqual((info["master_display"], info["max_cll"]), (self.MD, "500,100"))
        out = subprocess.run([NR.FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_streams",
                              "-of", "json", p.dv_render], capture_output=True, text=True).stdout
        dv = [x for x in json.loads(out)["streams"][0].get("side_data_list", [])
              if x.get("side_data_type") == "DOVI configuration record"]
        self.assertEqual((dv[0]["dv_profile"], dv[0]["dv_bl_signal_compatibility_id"]), (8, 1))
        rpu = os.path.join(self.d, "back.rpu")
        self.assertTrue(dvcap.extract_rpu(p.dv_render, rpu)[0])
        self.assertEqual(dvcap.rpu_frame_count(rpu), TOTAL)
        txt = subprocess.run([dvnative.DOVI_TOOL, "info", "-i", rpu, "-f", "0"], capture_output=True,
                             text=True).stdout
        vdr = json.loads(txt[txt.find("{"):])["vdr_dm_data"]
        l6 = next(b["Level6"] for b in vdr["cmv29_metadata"]["ext_metadata_blocks"] if "Level6" in b)
        self.assertEqual(l6, {"max_display_mastering_luminance": 1000, "min_display_mastering_luminance": 50,
                              "max_content_light_level": 500, "max_frame_average_light_level": 100})

    def test_a_source_of_unknown_light_ships_the_measured_level_in_l6_and_its_sei(self):
        import stages
        p = types.SimpleNamespace(ep="T02", dv_render=os.path.join(self.d, "Unknown_dv.mov"),
                                  source_cfr=self.unknown, combine=False)
        src_info = dvcap.probe_video(self.unknown)
        self.assertEqual((src_info["master_display"], src_info["max_cll"]), (self.MD, "0,0"))
        master, light = dvnative.source_mastering(src_info["master_display"], src_info["max_cll"])
        spec = NR.RenderSpec(inputs=[[0, self.unknown], [TOTAL, None]], fps=FPS, kbps=60000, cap_mbps=0,
                             master=master, light=stages.declared_light(light), keep_video=True)
        self.assertIsNone(spec.light)
        with mock.patch.object(dvnative, "analyze_ranges",
                               side_effect=lambda path, ranges, workdir, **kw: _sidecar(ranges)):
            ok, msg = NR.render(p, spec)
        self.assertTrue(ok, msg)
        self.assertEqual(_vcl_md5(p.dv_render), _vcl_md5(self.unknown))     # still the source's picture
        info = dvcap.probe_video(p.dv_render)
        self.assertEqual((info["master_display"], info["max_cll"]), (self.MD, "900,200"))
        self.assertEqual(NR.self_check(p, p.dv_render, TOTAL, hdr=(self.MD, "900,200"))[0], True)
        rpu = os.path.join(self.d, "back2.rpu")
        self.assertTrue(dvcap.extract_rpu(p.dv_render, rpu)[0])
        txt = subprocess.run([dvnative.DOVI_TOOL, "info", "-i", rpu, "-f", "0"], capture_output=True,
                             text=True).stdout
        vdr = json.loads(txt[txt.find("{"):])["vdr_dm_data"]
        l6 = next(b["Level6"] for b in vdr["cmv29_metadata"]["ext_metadata_blocks"] if "Level6" in b)
        self.assertEqual((l6["max_content_light_level"], l6["max_frame_average_light_level"]), (900, 200))


if __name__ == "__main__":
    unittest.main()


# ---- SDR output (sdr_out, 2026-10-18) ------------------------------------------------------

def _sdr_spec(src, **kw):
    base = dict(inputs=[[0, src], [TOTAL, None]], fps=FPS, kbps=20000, cap_mbps=50, lut=None,
                pre_vf="zscale=w=3840:h=2160:filter=lanczos:param_a=3:rangein=limited:range=limited,"
                       "format=yuv444p16le", sdr=True)
    base.update(kw)
    return NR.RenderSpec(**base)


class SdrSpec(_Base):
    def test_an_sdr_spec_has_no_lut_rpu_or_hdr_metadata(self):
        self.assertEqual(_sdr_spec(self.src).problems(), [])
        for kw in ({"lut": self.lut}, {"keep_video": True, "pre_vf": None, "cap_mbps": 0},
                   {"light": (100, 10)}, {"master": dvnative.MasterSpec(max_nits=4000)}):
            with self.subTest(kw=kw):
                self.assertIn("an SDR render", " ".join(_sdr_spec(self.src, **kw).problems()))

    def test_sdr_is_booked_only_when_set_and_the_encode_is_tagged_bt709(self):
        stats = NR.input_stats(self.spec)
        a, e = NR.fingerprints(self.spec, stats)
        self.assertNotIn("sdr", a)
        self.assertNotIn("sdr", e)                        # every DV route's books stay valid
        a2, e2 = NR.fingerprints(_sdr_spec(self.src), stats)
        self.assertEqual((a2["sdr"], e2["sdr"]), (True, True))
        dv = NR.build_encode_command("ff", self.spec, "o.hevc", 14000)
        self.assertEqual(dv[dv.index("-color_primaries"):],
                         ["-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace",
                          "bt2020nc", "-color_range", "tv", "-f", "hevc", "o.hevc"])   # unchanged
        sdr = NR.build_encode_command("ff", _sdr_spec(self.src), "o.hevc", 20000)
        self.assertEqual(sdr[sdr.index("-color_primaries"):],
                         ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                          "-color_range", "tv", "-f", "hevc", "o.hevc"])
        self.assertTrue(sdr[sdr.index("-vf") + 1].endswith(",format=yuv420p10le"))    # no LUT


@_own_tests_only
class SdrRender(Render):
    """An sdr spec: nothing analysed, the encode tagged BT.709, the plain mux (no SEI, no inject, no
    DV box), the same peak loop, an SDR self-check, and the proof written before the rename."""

    def setUp(self):
        super().setUp()
        self.spec = _sdr_spec(self.src)

    def fake_step(self, cmd, *, timeout, abort=None, should_pause=None, what="step", on_poll=None, **kw):
        if what == "SDR mux":
            self.assertEqual(cmd[:3], ["/usr/bin/nice", "-n", "10"])
            self.calls.append((what, cmd[3:]))
            shutil.copyfile(cmd[-1], cmd[cmd.index("-o") + 1])     # mkvmerge: the ES -> -o
            return 0, "", 0.5
        return super().fake_step(cmd, timeout=timeout, abort=abort, should_pause=should_pause,
                                 what=what, on_poll=on_poll, **kw)

    def fake_check(self, p, path, total=None, hdr=None, **kw):
        self.checked.append((path, total, hdr, kw))
        return self.check

    def test_the_sdr_render_is_encoded_muxed_checked_and_vouched_for(self):
        self.peaks = [30.0]
        ok, msg = self.render()
        self.assertTrue(ok, msg)
        self.assertEqual(self.whats(), ["encode attempt 1", "SDR mux"])
        self.assertEqual(self.analyses, [])                                    # nothing measured
        mux = dict(self.calls)["SDR mux"]
        self.assertNotIn("dvp", " ".join(mux))
        self.assertIn("--colour-primaries 0:1", " ".join(mux))               # BT.709 container
        self.assertEqual(self.checked, [(self.kept(), TOTAL, None, {"sdr": True})])
        self.assertIn("native SDR render: 48 frames, 20000 kb/s, peak 30.0 Mbps (under-cap)", msg)
        self.assertTrue(NR.sdr_render_ok(self.p))
        self.assertEqual(sorted(os.listdir(NR.books_dir(self.p))),
                         ["cpu.json", "encode.json", "sdr.json", "spec.json"])
        enc = dict(self.calls)["encode attempt 1"]
        self.assertEqual(enc[enc.index("-color_trc") + 1], "bt709")

    def test_the_peak_loop_is_the_same(self):
        self.peaks = [70.0, 40.0]
        ok, msg = self.render()
        self.assertTrue(ok, msg)
        self.assertEqual(self.whats(), ["encode attempt 1", "SDR mux", "encode attempt 2", "SDR mux"])
        self.assertTrue(NR.sdr_render_ok(self.p))

    def test_a_failed_check_publishes_nothing_and_vouches_for_nothing(self):
        self.peaks = [30.0]
        self.check = (False, "it carries a Dolby Vision record")
        ok, msg = self.render()
        self.assertFalse(ok)
        self.assertIn("self-check: it carries a Dolby Vision record", msg)
        self.assertFalse(os.path.exists(self.p.dv_render))
        self.assertFalse(NR.sdr_render_ok(self.p))

    def test_an_sdr_render_in_place_is_blessed_not_redone(self):
        with open(self.p.dv_render, "w") as f:
            f.write("an SDR render Resolve's SDR project made")
        self.assertFalse(NR.sdr_render_ok(self.p))
        ok, msg = self.render()
        self.assertEqual((ok, msg), (True, "native SDR render already in place"))
        self.assertEqual(self.checked, [(self.p.dv_render, None, None, {"sdr": True})])
        self.assertEqual(self.calls, [])
        self.assertTrue(NR.sdr_render_ok(self.p))
        with open(self.p.dv_render, "a") as f:
            f.write("changed")
        self.assertFalse(NR.sdr_render_ok(self.p))                             # names that file only


class SdrProof(_Base):
    def test_sdr_render_ok_never_raises_and_needs_the_exact_file(self):
        self.assertFalse(NR.sdr_render_ok(self.p))                             # no render, no proof
        self.assertFalse(NR.sdr_render_ok(types.SimpleNamespace(dv_render=None)))
        with open(self.p.dv_render, "w") as f:
            f.write("render")
        self.assertFalse(NR.sdr_render_ok(self.p))                             # a render, no proof
        os.makedirs(NR.books_dir(self.p))
        NR._write_sdr_proof(self.p, self.p.dv_render)
        self.assertTrue(NR.sdr_render_ok(self.p))
        os.utime(self.p.dv_render, ns=(1, 1))
        self.assertFalse(NR.sdr_render_ok(self.p))
        with open(os.path.join(NR.books_dir(self.p), NR.SDR_PROOF), "w") as f:
            f.write("junk")
        self.assertFalse(NR.sdr_render_ok(self.p))


@_own_tests_only
class SelfCheckSdr(SelfCheck):
    """sdr=True: HEVC with NO Dolby Vision record and no PQ/HLG transfer, then the whole-source
    and frame-rate gates; no HDR10 metadata asked for."""

    def setUp(self):
        super().setUp()
        self.video = {"codec_type": "video", "codec_name": "hevc", "color_transfer": "bt709"}
        self.vstream = mock.patch.object(self.O, "_vstream", side_effect=lambda path: [dict(self.video)])
        self.vstream.start()
        self.hdr10 = {"master_display": None, "max_cll": None}

    def tearDown(self):
        self.vstream.stop()             # before the base's patches: stopping it after them would
        super().tearDown()              # put the base's mock back for every later test

    def test_an_sdr_render_passes_without_hdr10_metadata(self):
        ok, why = NR.self_check(self.p, self.part, TOTAL, sdr=True)
        self.assertTrue(ok, why)
        self.assertIn("SDR", why)
        self.assertFalse(NR.self_check(self.p, self.part, TOTAL)[1].startswith("an SDR"))   # DV rule

    def test_what_it_refuses(self):
        cases = {"dovi": dict(self.video, side_data_list=[{"side_data_type": "DOVI configuration record"}]),
                 "pq": dict(self.video, color_transfer="smpte2084"),
                 "prores": dict(self.video, codec_name="prores")}
        for name, video in cases.items():
            with self.subTest(name):
                self.video = video
                self.assertFalse(NR.self_check(self.p, self.part, TOTAL, sdr=True)[0])
        self.video = {"codec_type": "video", "codec_name": "hevc", "color_transfer": "bt709"}
        self.frames = TOTAL - 1
        self.assertIn("47 frames, the source 48", NR.self_check(self.p, self.part, TOTAL, sdr=True)[1])
        self.frames = TOTAL
        self.ok["complete"] = False
        self.assertIn("whole source", NR.self_check(self.p, self.part, TOTAL, sdr=True)[1])


class BooksSurviveTheFpsSnap(unittest.TestCase):
    """spec.json written before dvcap.snap_fps recorded a Matroska readback rate (19001/317 for
    60000/1001): the same work, so a deploy keeps the analysis instead of re-measuring hours."""

    def test_an_old_readback_rate_keeps_the_analysis(self):
        d = tempfile.mkdtemp()
        try:
            p = types.SimpleNamespace(ep="X", dv_render=os.path.join(d, "X HDR10 DV upscaled.mov"))
            spec = NR.RenderSpec(inputs=[[0, os.path.join(d, "s.mkv")], [10, None]],
                                 fps="60000/1001", kbps=20000, cap_mbps=50)
            analysis, encode = NR.fingerprints(spec, [])
            old = {"analysis": dict(analysis, fps="19001/317"), "encode": dict(encode, fps="19001/317"),
                   "rpu": {"frames": 10}}
            os.makedirs(NR.books_dir(p), exist_ok=True)
            with open(os.path.join(NR.books_dir(p), NR.SPEC_BOOK), "w") as f:
                json.dump(old, f)
            with mock.patch.object(NR, "_clear", side_effect=AssertionError("cleared")):
                state = NR.begin(p, spec, [])
            self.assertEqual(state.get("rpu"), {"frames": 10})
        finally:
            shutil.rmtree(d, ignore_errors=True)
