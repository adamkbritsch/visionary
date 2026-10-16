import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import dvnative


def _scene(start, end, mn, avg, mx):
    return {"start": start, "end": end, "min_pq_12bit": mn, "avg_luma_pq_12bit": avg - 20,
            "avg_max_rgb_pq_12bit": avg, "max_pq_12bit": mx}


def _sidecar(scenes, stream_frames=None, leading=None, cll=None, fall=None):
    sc = {"version": 5, "scenes": scenes}
    if stream_frames is not None or leading is not None:
        sc["source"] = {"file_name": "x.hevc", "size_bytes": 1, "width": 3840, "height": 2160,
                        "stream_frames": stream_frames, "leading_skipped_frames": leading}
    if cll:
        sc["light_level"] = {"max_cll_nits": cll, "max_fall_nits": fall}
    return sc


class PqCodes(unittest.TestCase):
    """The codes Resolve's RPUs carry (read from a real Resolve master)."""

    def test_resolve_constants(self):
        self.assertEqual(dvnative.nits_to_pq12(1000), 3079)     # source_max_pq / L2 1000-nit
        self.assertEqual(dvnative.nits_to_pq12(600), 2851)      # L2 600-nit target
        self.assertEqual(dvnative.nits_to_pq12(100), 2081)      # L2 100-nit target
        self.assertEqual(dvnative.nits_to_pq12(0.0001), 7)      # source_min_pq

    def test_clamped(self):
        self.assertEqual(dvnative.nits_to_pq12(20000), 4095)
        self.assertEqual(dvnative.nits_to_pq12(-1), 0)


class Shots(unittest.TestCase):
    """Ported from hdr-analyze mkvdovi tests (l1_sidecar_scenes_become_source_honest_shots,
    undecodable_leading_pictures_shift_the_shots_and_lengthen_the_rpu)."""

    def test_scenes_become_source_honest_shots(self):
        sc = _sidecar([_scene(0, 213, 0, 614, 2437), _scene(214, 333, 1, 462, 3416)])
        cfg = dvnative.rpu_config(sc, max_cll=997, max_fall=91)
        self.assertEqual(cfg["length"], 334)
        self.assertNotIn("l1_avg_pq_cm_version", cfg)       # CM v4.0 floor (1229), like Resolve
        s0, s1 = cfg["shots"]
        self.assertEqual((s0["start"], s0["duration"]), (0, 214))
        self.assertEqual(s0["metadata_blocks"][0]["Level1"], {"min_pq": 0, "max_pq": 2437, "avg_pq": 614})
        self.assertEqual((s1["start"], s1["duration"]), (214, 120))
        self.assertEqual(s1["metadata_blocks"][0]["Level1"]["avg_pq"], 462)   # max-RGB avg, not luma

    def test_leading_pictures_shift_the_shots_and_lengthen_the_rpu(self):
        sc = _sidecar([_scene(0, 9, 1, 500, 2400), _scene(10, 19, 2, 600, 2500)],
                      stream_frames=22, leading=2)
        cfg = dvnative.rpu_config(sc, max_cll=997, max_fall=91)
        self.assertEqual(cfg["length"], 22)
        self.assertEqual([(s["start"], s["duration"]) for s in cfg["shots"]], [(0, 12), (12, 10)])
        self.assertEqual(cfg["shots"][1]["metadata_blocks"][0]["Level1"]["max_pq"], 2500)


class StaticBlocksMatchResolve(unittest.TestCase):
    def setUp(self):
        self.cfg = dvnative.rpu_config(_sidecar([_scene(0, 9, 1, 500, 2400)], cll=640, fall=210))

    def test_profile_cm_and_source_range(self):
        c = self.cfg
        self.assertEqual((c["profile"], c["cm_version"]), ("8.1", "V40"))
        self.assertEqual((c["source_min_pq"], c["source_max_pq"]), (7, 3079))

    def test_l5_zero_and_l6_mastering(self):
        self.assertEqual(set(self.cfg["level5"].values()), {0})
        l6 = self.cfg["level6"]
        self.assertEqual((l6["max_display_mastering_luminance"], l6["min_display_mastering_luminance"]), (1000, 1))
        self.assertEqual((l6["max_content_light_level"], l6["max_frame_average_light_level"]), (640, 210))

    def test_l2_is_neutral_at_resolves_three_targets(self):
        l2 = [b["Level2"] for b in self.cfg["default_metadata_blocks"] if "Level2" in b]
        self.assertEqual([b["target_max_pq"] for b in l2], [2081, 2851, 3079])
        for b in l2:
            self.assertEqual({v for k, v in b.items() if k != "target_max_pq"}, {2048})

    def test_l9_bt2020_and_l11_like_resolve(self):
        blocks = {k: v for b in self.cfg["default_metadata_blocks"] for k, v in b.items() if k != "Level2"}
        self.assertEqual(blocks["Level9"]["source_primary_index"], 2)
        self.assertEqual(blocks["Level11"], {"content_type": 0, "whitepoint": 0, "reference_mode_flag": False})

    def test_light_level_falls_back_to_the_mastering_peak(self):
        cfg = dvnative.rpu_config(_sidecar([_scene(0, 9, 1, 500, 2400)]))
        self.assertEqual(cfg["level6"]["max_content_light_level"], 1000)


class SidecarValidation(unittest.TestCase):
    def _load(self, sc):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(sc, f)
        try:
            return dvnative.load_sidecar(f.name)
        finally:
            os.unlink(f.name)

    def test_contiguous_sidecar_loads(self):
        self.assertEqual(len(self._load(_sidecar([_scene(0, 9, 1, 5, 9), _scene(10, 12, 1, 5, 9)]))["scenes"]), 2)

    def test_gap_between_scenes_refused(self):
        with self.assertRaises(ValueError):
            self._load(_sidecar([_scene(0, 9, 1, 5, 9), _scene(11, 12, 1, 5, 9)]))

    def test_not_starting_at_zero_refused(self):
        with self.assertRaises(ValueError):
            self._load(_sidecar([_scene(1, 9, 1, 5, 9)]))

    def test_missing_l1_field_refused(self):
        s = _scene(0, 9, 1, 5, 9); del s["max_pq_12bit"]
        with self.assertRaises(ValueError):
            self._load(_sidecar([s]))

    def test_empty_refused(self):
        with self.assertRaises(ValueError):
            self._load({"version": 5, "scenes": []})


class AnalyzerCommand(unittest.TestCase):
    def test_defaults_force_pq_full_frame_full_res(self):
        c = dvnative.build_analyzer_command("/a", "/in.mkv", "/m.bin")
        self.assertEqual(c[:5], ["/a", "-i", "/in.mkv", "-o", "/m.bin"])
        self.assertEqual(c[c.index("--transfer") + 1], "pq")
        self.assertIn("--no-crop", c)                       # Resolve writes L5 = 0
        self.assertEqual(c[c.index("--downscale") + 1], "1")
        self.assertNotIn("--peak-source", c)

    def test_robust_estimator_selects_the_direct_peak_domain(self):
        c = dvnative.build_analyzer_command("/a", "/in", "/m.bin", peak_estimator="robust", threads=6,
                                            frame_stats="/s.csv")
        self.assertEqual(c[c.index("--peak-source") + 1], "max")
        self.assertEqual(c[c.index("--analysis-threads") + 1], "6")
        self.assertEqual(c[c.index("--dump-frame-stats") + 1], "/s.csv")

    def test_sidecar_path(self):
        self.assertEqual(dvnative.sidecar_path("/x/m.bin"), "/x/m.bin.l1.json")


class ChunkMerge(unittest.TestCase):
    def test_scenes_are_offset_by_their_chunk_start(self):
        a = _sidecar([_scene(0, 9, 1, 500, 2400), _scene(10, 19, 2, 600, 2500)], stream_frames=20, cll=700, fall=90)
        b = _sidecar([_scene(0, 14, 3, 700, 2600)], stream_frames=15, cll=900, fall=80)
        m = dvnative.merge_sidecars([(0, a), (20, b)])
        self.assertEqual([(s["start"], s["end"]) for s in m["scenes"]], [(0, 9), (10, 19), (20, 34)])
        self.assertEqual(dvnative.stream_frames(m), 35)
        self.assertEqual(dvnative.light_levels(m), (900, 90))   # max over chunks
        self.assertEqual(dvnative.rpu_config(m)["length"], 35)

    def test_merged_length_must_equal_the_expected_total(self):
        a = _sidecar([_scene(0, 9, 1, 500, 2400)], stream_frames=10)
        with self.assertRaises(ValueError):
            dvnative.merge_sidecars([(0, a)], total=11)
        self.assertEqual(dvnative.stream_frames(dvnative.merge_sidecars([(0, a)], total=10)), 10)

    def test_l4_lists_concatenate_only_when_every_chunk_has_one(self):
        a = _sidecar([_scene(0, 1, 1, 500, 2400)], stream_frames=2); a["l4"] = [[1, 2], [3, 4]]
        b = _sidecar([_scene(0, 0, 1, 500, 2400)], stream_frames=1); b["l4"] = [[5, 6]]
        self.assertEqual(dvnative.merge_sidecars([(0, a), (2, b)])["l4"], [[1, 2], [3, 4], [5, 6]])
        del b["l4"]
        self.assertNotIn("l4", dvnative.merge_sidecars([(0, a), (2, b)]))

    def test_a_gap_between_chunks_is_refused(self):
        a = _sidecar([_scene(0, 9, 1, 500, 2400)], stream_frames=10)
        with self.assertRaises(ValueError):
            dvnative.merge_sidecars([(0, a), (11, a)])

    def test_leading_pictures_inside_a_chunk_are_refused(self):
        a = _sidecar([_scene(0, 9, 1, 500, 2400)], stream_frames=12, leading=2)
        with self.assertRaises(ValueError):
            dvnative.merge_sidecars([(0, a)])


class AnalysisIntermediate(unittest.TestCase):
    def test_area_downscale_then_lut_then_444_ffvhuff_tagged_pq(self):
        c = dvnative.build_analysis_intermediate_command("/ff", "/seg_0001.mov", "/c.mkv", "/l/rcm.cube")
        vf = c[c.index("-vf") + 1]
        self.assertTrue(vf.startswith("scale=1920:1080:flags=area,"))   # the LUT runs at 1080p
        self.assertLess(vf.index("scale=1920:1080"), vf.index("lut3d"))
        self.assertLess(vf.index("lut3d"), vf.index("matrix=2020_ncl"))
        self.assertTrue(vf.endswith("format=yuv444p10le"))
        self.assertNotIn("yuv420", vf)                       # never through 4:2:0 on the way
        self.assertEqual(c[c.index("-color_trc") + 1], "smpte2084")
        self.assertEqual(c[c.index("-c:v") + 1], "ffvhuff")              # lossless, cheap to decode
        self.assertEqual(c[c.index("-pix_fmt") + 1], "yuv444p10le")
        self.assertEqual(c[-1], "/c.mkv")

    def test_already_pq_input_needs_no_lut(self):
        c = dvnative.build_analysis_intermediate_command("/ff", "/x.mov", "/c.mkv", None)
        self.assertEqual(c[c.index("-vf") + 1], "scale=1920:1080:flags=area,format=yuv444p10le")

    def test_hardware_decode_by_default_software_on_request(self):
        hw = dvnative.build_analysis_intermediate_command("/ff", "/x.mov", "/c.mkv", None)
        self.assertEqual(hw[hw.index("-hwaccel") + 1], "videotoolbox")
        self.assertLess(hw.index("-hwaccel"), hw.index("-i"))           # an input option
        sw = dvnative.build_analysis_intermediate_command("/ff", "/x.mov", "/c.mkv", None, hwaccel=False)
        self.assertNotIn("-hwaccel", sw)
        self.assertEqual([a for a in hw if a not in ("-hwaccel", "videotoolbox")], sw)


class RawDecode(unittest.TestCase):
    def test_full_size_unless_a_size_is_given(self):
        full = dvnative.build_raw444_decode_command("/ff", "/c.mkv")
        self.assertNotIn("-vf", full)
        small = dvnative.build_raw444_decode_command("/ff", "/c.mkv", (960, 540))
        self.assertEqual(small[small.index("-vf") + 1], "scale=960:540:flags=area")
        self.assertLess(small.index("-vf"), small.index("-pix_fmt"))
        self.assertEqual(small[-5:], ["-pix_fmt", "yuv444p10le", "-f", "rawvideo", "-"])


class ColourChain(unittest.TestCase):
    def test_second_zscale_declares_its_input_already_pq_2020(self):
        vf = dvnative.lut_vf("/l/rcm.cube")
        first, second = vf.split("lut3d=")[0], vf.split("lut3d=")[1]
        self.assertNotIn("transfer", first)                  # YUV->RGB only: no transfer change
        self.assertNotIn("primaries", first)
        self.assertIn("transferin=smpte2084", second)        # no 709->PQ on top of the LUT
        self.assertIn("primariesin=2020", second)
        self.assertIn("matrix=2020_ncl:range=limited", second)
        self.assertTrue(vf.endswith("format=yuv420p10le"))

    def test_unsafe_lut_path_refused(self):
        for bad in ("/a b/x.cube", "/x'.cube", "/x:y.cube", ""):
            with self.assertRaises(ValueError):
                dvnative.lut_vf(bad)


class Clamps(unittest.TestCase):
    def test_l1_clamped_to_the_1000_nit_source_range(self):
        self.assertEqual(dvnative.clamp_l1(5, 4025, 3200), (5, 3079, 3079))
        self.assertEqual(dvnative.clamp_l1(5, 2900, 1500), (5, 2900, 1500))
        self.assertEqual(dvnative.clamp_l1(2000, 2900, 1500), (1500, 2900, 1500))

    def test_shots_never_exceed_source_max(self):
        cfg = dvnative.rpu_config(_sidecar([_scene(0, 9, 1, 3200, 4025)]))
        self.assertEqual(cfg["shots"][0]["metadata_blocks"][0]["Level1"], {"min_pq": 1, "max_pq": 3079, "avg_pq": 3079})

    def test_light_level_capped_at_mastering_and_fall_below_cll(self):
        self.assertEqual(dvnative.light_levels(_sidecar([_scene(0, 9, 1, 5, 9)], cll=8498, fall=994)), (1000, 994))
        self.assertEqual(dvnative.light_levels(_sidecar([_scene(0, 9, 1, 5, 9)], cll=500, fall=900)), (500, 500))
        self.assertEqual(dvnative.x265_max_cll(_sidecar([_scene(0, 9, 1, 5, 9)], cll=8498, fall=994)), "1000,994")
        l6 = dvnative.rpu_config(_sidecar([_scene(0, 9, 1, 5, 9)], cll=8498, fall=994))["level6"]
        self.assertEqual((l6["max_content_light_level"], l6["max_frame_average_light_level"]), (1000, 994))


class AvgFloor(unittest.TestCase):
    def test_default_is_the_cm_v4_floor_and_v29_is_opt_in(self):
        sc = _sidecar([_scene(0, 9, 1, 600, 2400)])
        self.assertNotIn("l1_avg_pq_cm_version", dvnative.rpu_config(sc))
        self.assertEqual(dvnative.rpu_config(sc, avg_floor="V29")["l1_avg_pq_cm_version"], "V29")
        with self.assertRaises(ValueError):
            dvnative.rpu_config(sc, avg_floor="V30")


class Level4(unittest.TestCase):
    def test_every_frame_gets_its_own_l4_and_refresh_every_frame(self):
        sc = _sidecar([_scene(0, 2, 1, 500, 2400), _scene(3, 4, 2, 600, 2500)])
        sc["l4"] = [[900, 400], [910, 401], [920, 402], [1000, 500], [1010, 501]]
        cfg = dvnative.rpu_config(sc)
        self.assertTrue(cfg["long_play_mode"])
        e1 = cfg["shots"][1]["frame_edits"]
        self.assertEqual([e["edit_offset"] for e in e1], [0, 1])
        self.assertEqual(e1[1]["metadata_blocks"][0]["Level4"], {"anchor_pq": 1010, "anchor_power": 501})
        self.assertEqual(len(cfg["shots"][0]["frame_edits"]), 3)

    def test_l4_off_or_absent_writes_no_frame_edits(self):
        sc = _sidecar([_scene(0, 2, 1, 500, 2400)]); sc["l4"] = [[1, 1]] * 3
        self.assertNotIn("frame_edits", dvnative.rpu_config(sc, l4=False)["shots"][0])
        self.assertNotIn("frame_edits", dvnative.rpu_config(_sidecar([_scene(0, 2, 1, 500, 2400)]))["shots"][0])

    def test_l4_length_mismatch_refused(self):
        sc = _sidecar([_scene(0, 2, 1, 500, 2400)]); sc["l4"] = [[1, 1]] * 2
        with self.assertRaises(ValueError):
            dvnative.rpu_config(sc)

    def test_maxrgb_stats_on_a_synthetic_frame(self):
        import struct
        w, h = 4, 2
        # top row: reference white (Y 940, neutral chroma); bottom row: black (Y 64)
        y = [940] * 4 + [64] * 4
        frame = struct.pack("<%dH" % (3 * w * h), *(y + [512] * 8 + [512] * 8))
        mean, std = dvnative.maxrgb_pq12_stats(frame, w, h)
        self.assertAlmostEqual(mean, 3079 / 2, delta=1)     # white clamps to the 1000-nit code
        self.assertAlmostEqual(std, 3079 / 2, delta=1)


@unittest.skipUnless(os.path.exists(dvnative.DOVI_TOOL), "dovi_tool not installed")
class GenerateRoundTrip(unittest.TestCase):
    """The config must be one the installed dovi_tool accepts, and the RPU it writes must
    carry what we asked for (CM v4.0, the shots, L6) — read back with `info --summary`."""

    def test_generate_and_read_back(self):
        d = tempfile.mkdtemp()
        try:
            sc_path = os.path.join(d, "m.bin.l1.json")
            with open(sc_path, "w") as f:
                json.dump(_sidecar([_scene(0, 23, 1, 1500, 2900), _scene(24, 59, 3, 1700, 3079)],
                                   cll=980, fall=300), f)
            rpu = os.path.join(d, "rpu.bin")
            dvnative.generate_rpu(sc_path, rpu)
            self.assertFalse(os.path.exists(rpu + ".part"))
            info = subprocess.run([dvnative.DOVI_TOOL, "info", "-i", rpu, "--summary"],
                                  capture_output=True, text=True).stdout
            self.assertIn("Frames: 60", info)
            self.assertIn("Profile: 8", info)
            self.assertIn("CM v4.0", info)
            self.assertIn("0.0001/1000 nits", info)
            self.assertIn("Scene/shot count: 60", info)      # long_play_mode: refresh every frame
        finally:
            shutil.rmtree(d, ignore_errors=True)


PY = sys.executable


def _read(path) -> str:
    with open(path) as f:
        return f.read()


def _pid_gone(pid, wait=3.0) -> bool:
    """True once `pid` no longer exists (a killed grandchild is reaped by launchd, not us)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < wait:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


class StepTimeouts(unittest.TestCase):
    def test_300_s_plus_frames_over_5_rounded_up(self):
        self.assertEqual(dvnative.step_timeout(0), 300)
        self.assertEqual(dvnative.step_timeout(5), 301)
        self.assertEqual(dvnative.step_timeout(6), 302)
        self.assertEqual(dvnative.step_timeout(7058), 1712)     # S06E05's long chunk

    def test_niced_prefix(self):
        self.assertEqual(dvnative.niced(["/ff", "-i", "x"]), ["/usr/bin/nice", "-n", "10", "/ff", "-i", "x"])
        self.assertEqual(dvnative.niced(["/ff"], nice=0), ["/ff"])


class RunStep(unittest.TestCase):
    """Real child processes: the poll loop must kill (and reap) what it started, never hang."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.procs = []
        real = subprocess.Popen

        def record(*a, **k):
            pr = real(*a, **k)
            self.procs.append(pr)
            return pr
        self.patch = mock.patch.object(dvnative.subprocess, "Popen", side_effect=record)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        for pr in self.procs:
            if pr.returncode is None:
                try:
                    os.killpg(pr.pid, 9)
                except OSError:
                    pass
        shutil.rmtree(self.d, ignore_errors=True)

    def _sleeper(self, secs=30):
        return [PY, "-c", f"import time; time.sleep({secs})"]

    def test_returncode_output_tail_and_cpu(self):
        rc, tail, cpu = dvnative.run_step(
            [PY, "-c", "import sys,time\nt=time.process_time()\nwhile time.process_time()-t<0.3: pass\n"
                       "print('boom'); sys.exit(3)"], timeout=30, poll=0.05)
        self.assertEqual(rc, 3)
        self.assertIn("boom", tail)
        self.assertGreater(cpu, 0.2)                     # os.wait4 user+sys of the child
        self.assertEqual(self.procs[0].returncode, 3)    # reaped: Popen never waits on it again

    def test_timeout_raises_instead_of_hanging_and_kills_the_child(self):
        t0 = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            dvnative.run_step(self._sleeper(), timeout=0.5, poll=0.05, what="chunk 1 analyzer")
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIsNotNone(self.procs[0].returncode)
        self.assertTrue(_pid_gone(self.procs[0].pid))

    def test_abort_kills_the_child_mid_step_and_its_children(self):
        pidfile = os.path.join(self.d, "grandchild.pid")
        cmd = [PY, "-c", "import subprocess,sys,time\n"
                         "g=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\n"
                         f"open({pidfile!r},'w').write(str(g.pid))\ntime.sleep(30)"]
        ev = threading.Event()

        def later():
            for _ in range(100):
                if os.path.exists(pidfile) and _read(pidfile):
                    break
                time.sleep(0.05)
            ev.set()
        threading.Thread(target=later, daemon=True).start()
        t0 = time.monotonic()
        with self.assertRaises(dvnative.Aborted):
            dvnative.run_step(cmd, timeout=60, abort=ev, poll=0.05)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertTrue(_pid_gone(self.procs[0].pid))
        self.assertTrue(_pid_gone(int(_read(pidfile))))   # the whole process group died

    def test_should_pause_kills_the_child_and_raises_paused(self):
        t0 = time.monotonic()
        with self.assertRaises(dvnative.Paused) as cm:
            dvnative.run_step(self._sleeper(), timeout=60, poll=0.05,
                              should_pause=lambda: time.monotonic() - t0 > 0.3)
        self.assertTrue(str(cm.exception).startswith("paused:"))
        self.assertTrue(_pid_gone(self.procs[0].pid))

    def test_niced_child_runs_ten_lower(self):
        out = os.path.join(self.d, "nice.txt")
        rc, _tail, _cpu = dvnative.run_step(
            dvnative.niced([PY, "-c", f"import os; open({out!r},'w').write(str(os.nice(0)))"]),
            timeout=30, poll=0.05)
        self.assertEqual(rc, 0)
        self.assertEqual(int(_read(out)), min(20, os.nice(0) + 10))


def _fake_sidecar(frames, cll=500, fall=100):
    return {"version": 5, "scenes": [_scene(0, frames - 1, 1, 600, 2400)],
            "source": {"stream_frames": frames, "leading_skipped_frames": 0},
            "light_level": {"max_cll_nits": cll, "max_fall_nits": fall}}


class AnalyzeChunks(unittest.TestCase):
    """analyze_chunks with run_step faked: each step writes what the real tool would."""

    FRAMES = (24, 30, 18)

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.work = os.path.join(self.d, "work")
        self.chunks, a = [], 0
        for i, n in enumerate(self.FRAMES):
            path = os.path.join(self.d, f"seg_{i:04d}.mov")
            open(path, "wb").close()
            self.chunks.append([a, path])
            a += n
        self.chunks.append([a, None])
        self.total = a
        self.calls = []
        self.l4_short = set()            # chunk indices whose L4 child writes one frame too few
        self.analyzer_frames = {}        # chunk index -> frames the analyzer claims
        self.fail = {}                   # (k, step) -> rc
        self.hooks = {}                  # (k, step) -> called inside that step, before it ends

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _k(self, path):
        return int(os.path.basename(path).split("_")[1].split(".")[0])

    def fake_step(self, cmd, *, timeout, abort=None, should_pause=None, cwd=None, what="step", poll=0.5):
        self.assertEqual(cmd[:3], ["/usr/bin/nice", "-n", "10"])
        cmd = cmd[3:]
        if should_pause is not None and should_pause():     # run_step's poll, at the first tick
            raise dvnative.Paused(f"paused: yielded during {what}")
        if cmd[0] == "/ff":
            step, k = "intermediate", self._k(cmd[-1])
            if "-hwaccel" not in cmd:
                step = "intermediate-sw"
            if (k, "hw-refuses") in self.fail and step == "intermediate":
                self.calls.append((k, step))
                return 1, "hardware decode refused", 0.25
            open(cmd[-1], "wb").close()
        elif cmd[0] == "/an":
            step, k = "analyzer", self._k(cmd[2])
            n = self.analyzer_frames.get(k, self.FRAMES[k])
            with open(dvnative.sidecar_path(cmd[cmd.index("-o") + 1]), "w") as f:
                json.dump(_fake_sidecar(n), f)
        else:
            step, k = "l4", self._k(cmd[4])
            self.assertEqual(cmd[1:4], ["-m", "dvnative", "l4"])
            self.assertEqual(cmd[cmd.index("--size") + 1], "960x540")      # L4 at 540p...
            self.assertEqual(cmd[-1], "--scale")                           # ...scaled in the decode
            self.assertEqual(cwd, dvnative.ENGINE_DIR)
            n = self.FRAMES[k] - (1 if k in self.l4_short else 0)
            with open(cmd[5], "w") as f:
                json.dump([[k, 1]] * n, f)
        self.assertEqual(timeout, dvnative.step_timeout(self.FRAMES[k]))
        hook = self.hooks.get((k, step))
        if hook:
            hook()
        self.calls.append((k, step))
        rc = self.fail.get((k, step), 0)
        return rc, f"{step} said no" if rc else "", 0.25

    def assert_chunk_order(self, chunks, measure=("analyzer", "l4")):
        """Every chunk ran its intermediate, then (in either order: they run side by side) its
        measuring steps; nothing else ran."""
        want = [(k, s) for k in chunks for s in ("intermediate",) + tuple(measure)]
        self.assertEqual(sorted(self.calls), sorted(want))
        for k in chunks:
            first = self.calls.index((k, "intermediate"))
            for s in measure:
                self.assertLess(first, self.calls.index((k, s)))

    def analyze(self, **kw):
        kw.setdefault("cube", None)
        with mock.patch.object(dvnative, "run_step", side_effect=self.fake_step):
            return dvnative.analyze_chunks(self.chunks, self.work, analyzer="/an", ffmpeg="/ff",
                                           python="/py", **kw)

    def _leftovers(self):
        return sorted(n for n in os.listdir(self.work) if n.endswith((".mkv", ".part")))

    def test_every_chunk_measured_and_merged(self):
        cpu = []
        sc = self.analyze(on_cpu=lambda k, s, n: cpu.append((k, s, n)))
        self.assertEqual(dvnative.stream_frames(sc), self.total)
        self.assertEqual(len(sc["l4"]), self.total)
        self.assertEqual([s["start"] for s in sc["scenes"]], [0, 24, 54])
        self.assert_chunk_order(range(3))
        self.assertEqual(cpu, [(0, 0.75, 24), (1, 0.75, 30), (2, 0.75, 18)])   # 3 steps x 0.25 s
        self.assertEqual(self._leftovers(), [])

    def test_resume_skips_published_sidecars(self):
        self.analyze()
        self.calls.clear()
        os.remove(os.path.join(self.work, "chunk_0001.bin.l1.json"))      # only chunk 1 is unfinished
        progress = []
        sc = self.analyze(on_chunk=lambda done, of: progress.append((done, of)))
        self.assert_chunk_order([1])
        self.assertEqual(progress, [(1, 3), (2, 3), (3, 3)])                 # resumed ones report too
        self.assertEqual(dvnative.stream_frames(sc), self.total)

    def test_should_pause_between_chunks_gives_paused_and_resumes_there(self):
        asked = threading.Event()
        with self.assertRaises(dvnative.Paused) as cm:
            self.analyze(should_pause=asked.is_set,
                         on_chunk=lambda done, of: asked.set() if done == 1 else None)
        self.assertTrue(str(cm.exception).startswith("paused:"))
        self.assertNotIn(2, {k for k, _s in self.calls})                   # never started
        self.assertTrue(os.path.exists(os.path.join(self.work, "chunk_0000.bin.l1.json")))
        self.assertFalse(os.path.exists(os.path.join(self.work, "chunk_0001.bin.l1.json")))
        self.assertEqual(self._leftovers(), [])
        self.calls.clear()
        self.analyze()
        self.assert_chunk_order([1, 2])

    def test_before_chunk_hook_pauses_with_its_reason_before_any_step(self):
        seen = []

        def floor(k, frames):
            seen.append((k, frames))
            return "low disk — under the floor" if k == 1 else None
        with self.assertRaises(dvnative.Paused) as cm:
            self.analyze(before_chunk=floor)
        self.assertEqual(str(cm.exception), "paused: low disk — under the floor")
        self.assertEqual(seen, [(0, 24), (1, 30)])
        self.assertNotIn(1, {k for k, _s in self.calls})            # nothing spawned for chunk 1

    def test_before_chunk_is_not_asked_for_finished_chunks(self):
        self.analyze()
        seen = []
        self.analyze(before_chunk=lambda k, n: seen.append(k))
        self.assertEqual(seen, [])

    def test_abort_before_a_chunk(self):
        ev = threading.Event()
        ev.set()
        with self.assertRaises(dvnative.Aborted):
            self.analyze(abort=ev)
        self.assertEqual(self.calls, [])

    def test_a_pause_mid_step_leaves_no_partial_files(self):
        def step(cmd, **kw):
            if "analyzer" in kw.get("what", "") and "chunk 2" in kw["what"]:
                raise dvnative.Paused("paused: yielded during chunk 2 analyzer")
            return self.fake_step(cmd, **kw)
        with mock.patch.object(dvnative, "run_step", side_effect=step):
            with self.assertRaises(dvnative.Paused):
                dvnative.analyze_chunks(self.chunks, self.work, cube=None, analyzer="/an",
                                        ffmpeg="/ff", python="/py")
        self.assertEqual(self._leftovers(), [])
        self.assertFalse(os.path.exists(os.path.join(self.work, "chunk_0001.bin.l1.json")))

    def test_next_intermediate_builds_while_this_chunk_is_measured(self):
        built_1 = threading.Event()
        overlapped = []
        self.hooks[(1, "intermediate")] = built_1.set
        self.hooks[(0, "analyzer")] = lambda: overlapped.append(built_1.wait(5))
        sc = self.analyze()
        self.assertEqual(overlapped, [True])           # chunk 1's build ran inside chunk 0's analysis
        self.assertEqual(dvnative.stream_frames(sc), self.total)

    def test_analyzer_and_l4_run_side_by_side(self):
        l4_started = threading.Event()
        together = []
        self.hooks[(0, "l4")] = l4_started.set
        self.hooks[(0, "analyzer")] = lambda: together.append(l4_started.wait(5))
        self.analyze()
        self.assertEqual(together, [True])

    def test_hardware_decode_refusal_retries_once_in_software(self):
        self.fail[(1, "hw-refuses")] = 1
        sc = self.analyze()
        self.assertEqual(dvnative.stream_frames(sc), self.total)
        self.assertIn((1, "intermediate"), self.calls)
        self.assertIn((1, "intermediate-sw"), self.calls)
        self.assertLess(self.calls.index((1, "intermediate")), self.calls.index((1, "intermediate-sw")))
        self.assertNotIn((0, "intermediate-sw"), self.calls)
        self.assertEqual(self._leftovers(), [])

    def test_software_decode_failure_fails_the_chunk(self):
        self.fail[(1, "hw-refuses")] = 1
        self.fail[(1, "intermediate-sw")] = 1
        with self.assertRaisesRegex(RuntimeError, "chunk 1: intermediate failed: intermediate-sw said no"):
            self.analyze()
        self.assertEqual(self._leftovers(), [])
        self.assertTrue(os.path.exists(os.path.join(self.work, "chunk_0000.bin.l1.json")))  # kept

    def test_a_failed_step_kills_its_sibling_and_reports_its_own_error(self):
        sibling = []

        def slow_l4():                                  # stands in for a long L4 run_step
            seen = guard_holder[0]
            for _ in range(100):
                if seen.is_set():
                    sibling.append("killed")
                    raise dvnative.Aborted("aborted (run stopped) during chunk 2 L4")
                time.sleep(0.02)
            sibling.append("ran on")
        guard_holder = []
        real_step = self.fake_step

        def step(cmd, **kw):
            if "chunk 2 L4" in kw.get("what", ""):
                guard_holder.append(kw["abort"])
                slow_l4()
            return real_step(cmd, **kw)
        self.fail[(1, "analyzer")] = 1
        with mock.patch.object(dvnative, "run_step", side_effect=step):
            with self.assertRaisesRegex(RuntimeError, "chunk 1: analyzer failed"):
                dvnative.analyze_chunks(self.chunks, self.work, cube=None, analyzer="/an",
                                        ffmpeg="/ff", python="/py")
        self.assertEqual(sibling, ["killed"])
        self.assertEqual(self._leftovers(), [])

    def test_cpu_is_reported_for_a_chunk_that_failed(self):
        self.fail[(1, "analyzer")] = 1
        cpu = []
        with self.assertRaises(RuntimeError):
            self.analyze(on_cpu=lambda k, s, n: cpu.append((k, n)))
        self.assertEqual(cpu[0], (0, 24))
        self.assertIn((1, 30), cpu)
        self.assertEqual(len(cpu), len(set(cpu)))      # once each

    def test_a_failed_step_raises_and_cleans_up(self):
        self.fail[(1, "analyzer")] = 1
        with self.assertRaisesRegex(RuntimeError, "chunk 1: analyzer failed: analyzer said no"):
            self.analyze()
        self.assertEqual(self._leftovers(), [])
        self.assertFalse(os.path.exists(os.path.join(self.work, "chunk_0001.bin.l1.json")))

    def test_short_l4_is_refused_and_never_reused(self):
        self.l4_short.add(1)
        with self.assertRaisesRegex(RuntimeError, "chunk 1: L4 has 29 frames, the chunk has 30"):
            self.analyze()
        self.assertFalse(os.path.exists(os.path.join(self.work, "chunk_0001.bin.l1.json")))
        self.l4_short.clear()
        self.calls.clear()
        self.analyze()
        self.assertIn((1, "l4"), self.calls)                          # measured again

    def test_analysis_of_the_wrong_length_is_refused(self):
        self.analyzer_frames[2] = 17
        with self.assertRaisesRegex(RuntimeError, "chunk 2: analysis covers 17 frames"):
            self.analyze()

    def test_the_end_sentinel_is_required(self):
        with self.assertRaises(ValueError):
            dvnative.analyze_chunks(self.chunks[:-1], self.work, cube=None)


class L4Command(unittest.TestCase):
    def test_module_entry_with_ffmpeg_and_size(self):
        self.assertEqual(dvnative.build_l4_command("/py", "/w/c.mkv", "/w/c.l4.json.part", ffmpeg="/ff"),
                         ["/py", "-m", "dvnative", "l4", "/w/c.mkv", "/w/c.l4.json.part",
                          "--ffmpeg", "/ff", "--size", "1920x1080"])

    def test_scaled_entry(self):
        c = dvnative.build_l4_command("/py", "/w/c.mkv", "/o", ffmpeg="/ff", width=960, height=540, scale=True)
        self.assertEqual(c[-3:], ["--size", "960x540", "--scale"])

    def test_bad_size_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dvnative.main(["l4", "/x.mkv", "/o.json", "--size", "big"]), 2)
            self.assertEqual(dvnative.main([]), 2)


def _have_numpy() -> bool:
    try:
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(os.path.exists(dvnative.FFMPEG), "ffmpeg not installed")
class IntermediateIsLossless(unittest.TestCase):
    """The analysis intermediate's codec must give back exactly the 10-bit 4:4:4 PQ frames it
    was handed (FFVHuff replaced FFV1 for speed; a codec that silently dropped to 8-bit, as
    Ut Video does with yuv444p10le, would shift every measurement)."""

    def test_round_trip_is_bit_exact(self):
        d = tempfile.mkdtemp()
        try:
            src = ["-f", "lavfi", "-i", "testsrc2=size=96x54:rate=24", "-frames:v", "4",
                   "-vf", "format=yuv444p10le"]
            c = dvnative.build_analysis_intermediate_command(dvnative.FFMPEG, "IN", os.path.join(d, "x.mkv"),
                                                             None, hwaccel=False)
            enc = c[:c.index("-i")] + src + c[c.index("-color_primaries"):]
            r = subprocess.run(enc, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            # the reference: the SAME command with the codec swapped for FFV1 (lossless)
            ref = enc[:enc.index("-c:v")] + ["-c:v", "ffv1", "-level", "3", os.path.join(d, "ref.mkv")]
            r = subprocess.run(ref, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)

            def md5(path):
                return subprocess.run([dvnative.FFMPEG, "-hide_banner", "-loglevel", "error", "-i", path,
                                       "-pix_fmt", "yuv444p10le", "-f", "md5", "-"],
                                      capture_output=True, text=True).stdout.strip()
            self.assertTrue(md5(os.path.join(d, "ref.mkv")).startswith("MD5="))
            self.assertEqual(md5(os.path.join(d, "x.mkv")), md5(os.path.join(d, "ref.mkv")))
            probe = subprocess.run([dvnative.FFMPEG.replace("ffmpeg", "ffprobe"), "-v", "error",
                                    "-select_streams", "v:0", "-show_entries", "stream=pix_fmt",
                                    "-of", "csv=p=0", os.path.join(d, "x.mkv")], capture_output=True, text=True)
            self.assertEqual(probe.stdout.strip(), "yuv444p10le")     # 10-bit 4:4:4 kept (the analyzer
            #                                       is told --transfer pq, so the tags are not load-bearing)
        finally:
            shutil.rmtree(d, ignore_errors=True)


@unittest.skipUnless(os.path.exists(dvnative.FFMPEG) and _have_numpy(), "ffmpeg/numpy not installed")
class L4SubprocessMatchesInProcess(unittest.TestCase):
    """The L4 child (`python3 -m dvnative l4`, run exactly as analyze_chunks runs it) must give
    the same per-frame values as frame_l4 in-process, on a tiny synthetic PQ 4:4:4 clip."""

    def test_child_equals_in_process(self):
        d = tempfile.mkdtemp()
        try:
            mkv = os.path.join(d, "tiny.mkv")
            r = subprocess.run([dvnative.FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                                "-f", "lavfi", "-i", "testsrc2=size=64x36:rate=24", "-frames:v", "6",
                                "-pix_fmt", "yuv444p10le", "-color_primaries", "bt2020",
                                "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
                                "-color_range", "tv", "-c:v", "ffv1", mkv], capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = os.path.join(d, "tiny.l4.json")
            rc, tail, cpu = dvnative.run_step(
                dvnative.niced(dvnative.build_l4_command(PY, mkv, out, width=64, height=36)),
                timeout=dvnative.step_timeout(6), cwd=dvnative.ENGINE_DIR, poll=0.05)
            self.assertEqual(rc, 0, tail)
            with open(out) as f:
                child = json.load(f)
            here = dvnative.frame_l4(mkv, width=64, height=36)
            self.assertEqual(len(here), 6)
            self.assertEqual(child, here)
            self.assertTrue(any(v[0] > 0 for v in here))     # a real picture, not all black
            self.assertGreater(cpu, 0.0)
            # scaled in the decode (as analyze_chunks runs it): child == in-process, and close
            # to the full-size values on this smooth test pattern
            out2 = os.path.join(d, "tiny.small.l4.json")
            rc, tail, _cpu = dvnative.run_step(
                dvnative.niced(dvnative.build_l4_command(PY, mkv, out2, width=32, height=18, scale=True)),
                timeout=dvnative.step_timeout(6), cwd=dvnative.ENGINE_DIR, poll=0.05)
            self.assertEqual(rc, 0, tail)
            with open(out2) as f:
                child_small = json.load(f)
            small = dvnative.frame_l4(mkv, width=32, height=18, scale=True)
            self.assertEqual(child_small, small)
            self.assertEqual(len(small), 6)
            self.assertTrue(all(abs(a[0] - b[0]) <= 40 for a, b in zip(small, here)))
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
