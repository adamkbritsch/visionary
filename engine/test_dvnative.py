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
    STREAMS = 1                          # one pipeline: these tests pin its exact order

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
        kw.setdefault("streams", self.STREAMS)
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
                                        ffmpeg="/ff", python="/py", streams=1)
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
                                        ffmpeg="/ff", python="/py", streams=1)
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


class AnalyzeChunksTwoStreams(AnalyzeChunks):
    """The same fakes, two pipelines (the default): chunks 0, 2, 4 ... and 1, 3, 5 ..."""

    FRAMES = (24, 30, 18, 21, 27)
    STREAMS = 2

    # the single-stream ordering tests do not apply to two streams
    test_every_chunk_measured_and_merged = None
    test_resume_skips_published_sidecars = None
    test_should_pause_between_chunks_gives_paused_and_resumes_there = None
    test_before_chunk_hook_pauses_with_its_reason_before_any_step = None
    test_next_intermediate_builds_while_this_chunk_is_measured = None
    test_cpu_is_reported_for_a_chunk_that_failed = None
    test_a_failed_step_kills_its_sibling_and_reports_its_own_error = None

    def test_default_stays_one_stream_until_two_measure_faster(self):
        self.assertEqual(dvnative.STREAMS, 1)

    def test_same_result_as_one_stream_and_every_chunk_once(self):
        two = self.analyze()
        self.assert_chunk_order(range(5))                       # each step exactly once
        shutil.rmtree(self.work)
        self.calls.clear()
        one = self.analyze(streams=1)
        self.assertEqual(two, one)
        self.assertEqual(dvnative.stream_frames(two), self.total)
        self.assertEqual(self._leftovers(), [])

    def test_both_streams_build_at_once(self):
        other_started = threading.Event()
        together = []
        self.hooks[(1, "intermediate")] = other_started.set
        self.hooks[(0, "intermediate")] = lambda: together.append(other_started.wait(5))
        self.analyze()
        self.assertEqual(together, [True])

    def test_disk_floor_counts_the_other_streams_unwritten_intermediate(self):
        release, building_1 = threading.Event(), threading.Event()
        asked = {}

        def chunk_1_build():                                          # chunk 1 still being written
            building_1.set()
            release.wait(5)
        self.hooks[(1, "intermediate")] = chunk_1_build
        self.hooks[(0, "intermediate")] = lambda: building_1.wait(5)  # chunk 2 is gated after this

        def floor(k, frames):
            asked[k] = frames
            if k == 2:
                release.set()
            return None
        self.analyze(before_chunk=floor)
        self.assertEqual(asked[2], self.FRAMES[2] + self.FRAMES[1])
        self.assertEqual(asked[0], self.FRAMES[0])

    def test_a_refusal_lets_the_other_stream_finish_its_chunk_and_starts_nothing_new(self):
        release, building_1 = threading.Event(), threading.Event()

        def chunk_1_build():                                          # stream 1 mid-chunk
            building_1.set()
            release.wait(5)
        self.hooks[(1, "intermediate")] = chunk_1_build
        self.hooks[(0, "intermediate")] = lambda: building_1.wait(5)  # chunk 2 is gated after this

        def floor(k, frames):
            if k == 2:
                release.set()
                return "low disk"
            return None
        with self.assertRaises(dvnative.Paused) as cm:
            self.analyze(before_chunk=floor)
        self.assertEqual(str(cm.exception), "paused: low disk")
        for k in (0, 1):                                              # both in-flight chunks published
            self.assertTrue(os.path.exists(os.path.join(self.work, f"chunk_{k:04d}.bin.l1.json")))
        self.assertFalse({2, 3, 4} & {k for k, _s in self.calls})     # nothing new started
        self.assertEqual(self._leftovers(), [])
        self.calls.clear()
        self.analyze()
        self.assert_chunk_order([2, 3, 4])

    def test_a_failure_in_one_stream_kills_the_other_and_reports_its_own_error(self):
        killed = []
        real_step = self.fake_step

        def step(cmd, **kw):
            if "chunk 1 analyzer" in kw.get("what", ""):          # stream 1: a long analyzer
                guard = kw["abort"]
                for _ in range(250):
                    if guard.is_set():
                        killed.append(True)
                        raise dvnative.Aborted("aborted (run stopped) during chunk 1 analyzer")
                    time.sleep(0.02)
            return real_step(cmd, **kw)
        self.fail[(0, "l4")] = 1                                   # stream 0 fails
        with mock.patch.object(dvnative, "run_step", side_effect=step):
            with self.assertRaisesRegex(RuntimeError, "chunk 0: L4 failed"):
                dvnative.analyze_chunks(self.chunks, self.work, cube=None, analyzer="/an",
                                        ffmpeg="/ff", python="/py", streams=2)
        self.assertEqual(killed, [True])
        self.assertEqual(self._leftovers(), [])

    def test_should_pause_stops_both_streams_and_resume_finishes_the_rest_once(self):
        asked = threading.Event()
        with self.assertRaises(dvnative.Paused):
            self.analyze(should_pause=asked.is_set,
                         on_chunk=lambda done, of: asked.set())
        self.assertEqual(self._leftovers(), [])
        done = {k for k in range(5) if os.path.exists(os.path.join(self.work, f"chunk_{k:04d}.bin.l1.json"))}
        self.assertTrue(done)
        self.calls.clear()
        sc = self.analyze()
        self.assert_chunk_order(sorted(set(range(5)) - done))
        self.assertEqual(dvnative.stream_frames(sc), self.total)


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


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_fixtures")
E2E_SIDECAR = os.path.join(FIXTURES, "e2e_s06e05_sidecar.json.gz")
# What the pre-MasterSpec code wrote from that sidecar in the S06E05 E2E run (native.json's
# rpu_sha256, and the sha256 of the rpu.bin.generate.json beside it), with dovi_tool 2.3.0.
E2E_RPU_SHA256 = "7fa6836940ae67787964c1509c29c80eaf076d76945b07ddfba514055faba3f1"
E2E_CONFIG_SHA256 = "03ce22b9e4a49bd5aa6c6bbe0b007ccb8b1c2129bf75b02472dfc8fe877e62ac"


def _e2e_sidecar() -> dict:
    import gzip
    with gzip.open(E2E_SIDECAR) as f:
        return json.load(f)


def _sha256(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


class MasterSpecDefaults(unittest.TestCase):
    """The default MasterSpec IS today's constants: a caller that passes nothing gets the RPU
    the TV lane has always generated."""

    def test_defaults_are_resolves_1000_nit_project(self):
        m = dvnative.DEFAULT_MASTER
        self.assertEqual((m.max_nits, m.min_nits, m.trim_targets, m.mdcv), (1000, 0.0001, (100, 600, 1000), None))
        self.assertEqual((m.source_max_pq, m.source_min_pq, m.l6_min), (3079, 7, 1))
        self.assertEqual(dvnative.MasterSpec(), m)
        self.assertEqual(m.source_max_pq, dvnative.SOURCE_MAX_PQ)

    def test_refuses_what_an_rpu_cannot_declare(self):
        for kw in ({"max_nits": 0}, {"min_nits": 0}, {"min_nits": 2000}, {"max_nits": 20000},
                   {"trim_targets": ()}, {"trim_targets": (600, 100)}, {"trim_targets": (0, 100)},
                   {"mdcv": "L(1,1)"}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                dvnative.MasterSpec(**kw)

    def test_mdcv_must_say_the_same_nits(self):
        import dvcap
        self.assertEqual(dvnative.MasterSpec(mdcv=dvcap.FALLBACK_MASTER_DISPLAY).max_nits, 1000)
        p3_4000 = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(40000000,50)"
        with self.assertRaises(ValueError):                 # a 4000-nit SEI on a 1000-nit RPU
            dvnative.MasterSpec(mdcv=p3_4000)
        m = dvnative.MasterSpec(max_nits=4000, min_nits=0.005, mdcv=p3_4000)
        self.assertEqual((m.source_max_pq, m.l6_min), (dvnative.nits_to_pq12(4000), 50))

    def test_trim_targets_become_a_tuple(self):
        self.assertEqual(dvnative.MasterSpec(trim_targets=[100, 1000]).trim_targets, (100, 1000))


class MasterSpecThreading(unittest.TestCase):
    def setUp(self):
        self.sc = _sidecar([_scene(0, 9, 1, 3200, 3600)], cll=1800, fall=1900)
        self.m2000 = dvnative.MasterSpec(max_nits=2000, trim_targets=(100, 600, 1000, 2000))

    def test_default_master_changes_nothing(self):
        for sc in (self.sc, _e2e_sidecar()):
            self.assertEqual(dvnative.rpu_config(sc), dvnative.rpu_config(sc, master=dvnative.MasterSpec()))
        self.assertEqual(dvnative.light_levels(self.sc), dvnative.light_levels(self.sc, dvnative.MasterSpec()))
        self.assertEqual(dvnative.clamp_l1(5, 4025, 3200), dvnative.clamp_l1(5, 4025, 3200, dvnative.DEFAULT_MASTER))

    def test_a_2000_nit_master_moves_every_dependent_field(self):
        cfg = dvnative.rpu_config(self.sc, master=self.m2000)
        pq2000 = dvnative.nits_to_pq12(2000)
        self.assertEqual(cfg["source_max_pq"], pq2000)
        self.assertEqual(cfg["level6"]["max_display_mastering_luminance"], 2000)
        self.assertEqual((cfg["level6"]["max_content_light_level"],
                          cfg["level6"]["max_frame_average_light_level"]), (1800, 1800))
        self.assertEqual(cfg["shots"][0]["metadata_blocks"][0]["Level1"]["max_pq"], min(3600, pq2000))
        self.assertEqual([b["Level2"]["target_max_pq"] for b in cfg["default_metadata_blocks"] if "Level2" in b],
                         [dvnative.nits_to_pq12(t) for t in (100, 600, 1000, 2000)])
        self.assertEqual(dvnative.x265_max_cll(self.sc, self.m2000), "1800,1800")
        self.assertEqual(dvnative.x265_max_cll(self.sc), "1000,1000")     # the default still clamps


class E2ESidecarByteIdentical(unittest.TestCase):
    """The stored S06E05 E2E sidecar, through today's code with the DEFAULT MasterSpec, must give
    the exact config and RPU the pre-MasterSpec code shipped from it."""

    def test_config_is_byte_identical(self):
        cfg = dvnative.rpu_config(_e2e_sidecar(), l4=True, avg_floor="V40", master=dvnative.MasterSpec())
        self.assertEqual(_sha256(json.dumps(cfg).encode()), E2E_CONFIG_SHA256)   # json.dump as generate_rpu writes it

    @unittest.skipUnless(os.path.exists(dvnative.DOVI_TOOL), "dovi_tool not installed")
    def test_rpu_is_byte_identical(self):
        d = tempfile.mkdtemp()
        try:
            for name, kw in (("implicit", {}), ("explicit", {"master": dvnative.MasterSpec()})):
                rpu = os.path.join(d, name + ".bin")
                dvnative.generate_rpu(_e2e_sidecar(), rpu, avg_floor="V40", l4=True, **kw)
                with open(rpu, "rb") as f:
                    got = _sha256(f.read())
                with open(rpu + ".generate.json", "rb") as f:
                    self.assertEqual(_sha256(f.read()), E2E_CONFIG_SHA256)
                ver = subprocess.run([dvnative.DOVI_TOOL, "--version"], capture_output=True, text=True).stdout
                if "2.3.0" not in ver:
                    self.skipTest(f"RPU bytes pinned for dovi_tool 2.3.0, found {ver.strip()!r}")
                self.assertEqual(got, E2E_RPU_SHA256, name)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class FrontEnd(unittest.TestCase):
    def test_pre_vf_prefixes_the_analysis_chain_and_none_changes_nothing(self):
        self.assertEqual(dvnative.analysis_vf("/l/rcm.cube", None), dvnative.analysis_vf("/l/rcm.cube"))
        self.assertEqual(dvnative.analysis_vf(None, "scale=3840:2160"),
                         "scale=3840:2160,scale=1920:1080:flags=area,format=yuv444p10le")
        vf = dvnative.analysis_vf("/l/rcm.cube", "zscale=rangein=full:range=limited")
        # the front end first, then the target's scale-first chain (1080p, then the LUT)
        self.assertTrue(vf.startswith("zscale=rangein=full:range=limited,scale=1920:1080:flags=area,"
                                      "zscale=matrixin=709"))

    def test_a_filtergraph_is_not_a_front_end(self):
        for bad in ("[0:v]scale=1:1", "split;scale", "scale=1:1,", "a\nb"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                dvnative.front_end(bad)
        self.assertEqual(dvnative.front_end(""), "")


class FitGeometry(unittest.TestCase):
    """fit_geometry (2026-10-18): the largest display-aspect-true size inside 3840x2160, even both
    ways, on even offsets; 1920x1080 exactly 2x; a hairline pad closed by stretching."""

    def test_the_common_youtube_sizes(self):
        for size, want in (((1920, 1080), (3840, 2160, 0, 0)),        # exactly 2x
                           ((1280, 720), (3840, 2160, 0, 0)),         # exactly 3x
                           ((2560, 1440), (3840, 2160, 0, 0)),        # exactly 1.5x
                           ((640, 480), (2880, 2160, 480, 0)),        # 4:3 -> pillarbox
                           ((1080, 1920), (1216, 2160, 1312, 0)),     # vertical (1215 -> even 1216)
                           ((2160, 3840), (1216, 2160, 1312, 0)),     # vertical 4K: a downscale
                           ((3840, 1600), (3840, 1600, 0, 280)),      # 2.4:1: letterbox, no scale
                           ((4096, 2160), (3840, 2026, 0, 66))):      # DCI 4K: down, then letterbox
            with self.subTest(size=size):
                self.assertEqual(dvnative.fit_geometry(*size), want)

    def test_the_sample_aspect_ratio_is_the_display_shape(self):
        self.assertEqual(dvnative.fit_geometry(720, 480, "8:9"), (2880, 2160, 480, 0))   # 4:3 DVD
        self.assertEqual(dvnative.fit_geometry(720, 480, "32:27"), (3840, 2160, 0, 0))   # 16:9 DVD
        self.assertEqual(dvnative.fit_geometry(1440, 1080, "4:3"), (3840, 2160, 0, 0))   # HDV
        for square in (None, "1:1", "0:1", "N/A", "", "x"):
            self.assertEqual(dvnative.fit_geometry(640, 480, square), (2880, 2160, 480, 0), square)

    def test_a_hairline_pad_is_stretched_shut(self):
        # 854x480 fits as 3840x2158.3 -> 2158: a 2-line bar; stretching 0.09% closes it
        self.assertEqual(dvnative.fit_geometry(854, 480), (3840, 2160, 0, 0))
        w, h, x, y = dvnative.fit_geometry(3840, 2150)          # a 10-line pad is real: kept
        self.assertEqual((w, h, x, y), (3840, 2150, 0, 4))

    def test_every_size_is_even_inside_the_frame_and_keeps_its_aspect(self):
        for w0 in range(144, 4200, 37):
            for h0 in range(144, 4200, 53):
                w, h, x, y = dvnative.fit_geometry(w0, h0)
                self.assertTrue(w % 2 == h % 2 == x % 2 == y % 2 == 0, (w0, h0))
                self.assertTrue(0 < w <= 3840 and 0 < h <= 2160, (w0, h0))
                self.assertTrue(x + w <= 3840 and y + h <= 2160, (w0, h0))
                self.assertTrue(w == 3840 or h == 2160, (w0, h0))           # it fills one way
                s = min(3840 / w0, 2160 / h0)                                # aspect: within the
                self.assertLessEqual(abs(w - w0 * s), dvnative.PAD_SNAP_PX + 1, (w0, h0))   # even
                self.assertLessEqual(abs(h - h0 * s), dvnative.PAD_SNAP_PX + 1, (w0, h0))   # rounding
                self.assertLessEqual(abs((3840 - w) - 2 * x), 2, (w0, h0))   # centred
                self.assertLessEqual(abs((2160 - h) - 2 * y), 2, (w0, h0))

    def test_no_size_is_refused(self):
        for bad in ((0, 1080), (1920, 0), (None, None), ("x", 5), (-4, 3)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                dvnative.fit_geometry(*bad)


class ColourFront(unittest.TestCase):
    """colour_front: the tags when present (an untagged half follows the tagged one), else the
    frame size — SD BT.601 (EBU at 576 lines, SMPTE-C otherwise), HD BT.709."""

    def front(self, w=1920, h=1080, **tags):
        return dvnative.colour_front(dict({"width": w, "height": h}, **tags))

    def test_tags_win(self):
        self.assertEqual(self.front(color_space="bt709", color_primaries="bt709", color_transfer="bt709",
                                    color_range="tv"), ("709", "709", False))
        # the Rhett & Link samples: 640x480 tagged smpte170m matrix + primaries, bt709 transfer
        self.assertEqual(self.front(640, 480, color_space="smpte170m", color_primaries="smpte170m",
                                    color_transfer="bt709", color_range="tv"), ("170m", "170m", False))
        self.assertEqual(self.front(color_space="smpte170m", color_primaries="smpte170m"),
                         ("170m", "170m", False))                  # a 601-tagged HD frame: 601
        self.assertEqual(self.front(640, 480, color_space="bt709", color_primaries="bt709"),
                         ("709", "709", False))                    # a 709-tagged SD frame: 709
        self.assertEqual(self.front(color_space="bt470bg", color_primaries="bt470bg",
                                    color_transfer="bt470bg"), ("470bg", "470bg", False))
        self.assertEqual(self.front(color_space="bt709", color_primaries="smpte170m"),
                         ("709", "170m", False))                   # each tag for itself

    def test_an_untagged_half_follows_the_tagged_one(self):
        self.assertEqual(self.front(color_space="smpte170m"), ("170m", "170m", False))
        self.assertEqual(self.front(color_space="bt470bg"), ("470bg", "470bg", False))
        self.assertEqual(self.front(640, 480, color_primaries="bt709"), ("709", "709", False))
        self.assertEqual(self.front(color_primaries="smpte170m", color_space="unknown"),
                         ("170m", "170m", False))

    def test_untagged_the_frame_decides(self):
        for (w, h), want in (((640, 480), "170m"), ((720, 480), "170m"), ((320, 240), "170m"),
                             ((720, 576), "470bg"), ((1024, 576), "470bg"),
                             ((1280, 544), "709"), ((1280, 720), "709"), ((854, 578), "709"),
                             ((1080, 1920), "709"), ((480, 854), "709")):
            with self.subTest(size=(w, h)):
                self.assertEqual(self.front(w, h, color_space="unknown", color_primaries=None,
                                            color_transfer="unspecified"), (want, want, False))

    def test_full_range(self):
        self.assertEqual(self.front(color_range="pc"), ("709", "709", True))
        self.assertEqual(self.front(color_range="unknown"), ("709", "709", False))

    def test_what_no_standard_front_end_can_take(self):
        for tags, words in (({"color_transfer": "smpte2084"}, "already HDR (smpte2084)"),
                            ({"color_transfer": "arib-std-b67"}, "already HDR (arib-std-b67)"),
                            ({"color_transfer": "iec61966-2-1"}, "transfer is iec61966-2-1"),
                            ({"color_transfer": "linear"}, "transfer is linear"),
                            ({"color_space": "bt2020nc"}, "matrix are bt2020nc"),
                            ({"color_space": "gbr"}, "matrix are gbr"),
                            ({"color_space": "smpte240m"}, "matrix are smpte240m"),
                            ({"color_primaries": "bt2020"}, "primaries are bt2020"),
                            ({"color_range": "weird"}, "range is weird")):
            with self.subTest(tags=tags), self.assertRaises(ValueError) as cm:
                self.front(**tags)
            self.assertIn(words, str(cm.exception))
        for bad in ({}, {"width": 0, "height": 480}, None):
            with self.assertRaises(ValueError):
                dvnative.colour_front(bad)


class FitFrontEnd(unittest.TestCase):
    """colour_vf / fit_pad_vf / fit_front_end build one plain chain: colour (only when needed) at
    the source's size, then the Lanczos fit and the full-range-black pad."""

    def test_colour_vf(self):
        self.assertEqual(dvnative.colour_vf("709", "709"), "")
        self.assertEqual(dvnative.colour_vf("170m", "709"),
                         "zscale=matrixin=170m:rangein=limited:matrix=709:range=limited,format=yuv444p16le")
        self.assertEqual(dvnative.colour_vf("709", "709", full=True),
                         "zscale=matrixin=709:rangein=full:matrix=709:range=limited,format=yuv444p16le")
        self.assertEqual(dvnative.colour_vf("470bg", "470bg"),
                         "zscale=matrixin=470bg:rangein=limited:transferin=709:primariesin=470bg:"
                         "transfer=709:primaries=709:matrix=709:range=limited,format=yuv444p16le")
        for bad in (("2020_ncl", "709"), ("709", "2020"), (None, "709")):
            with self.assertRaises(ValueError):
                dvnative.colour_vf(*bad)

    def test_fit_pad_vf(self):
        self.assertEqual(dvnative.fit_pad_vf(1920, 1080),
                         "zscale=w=3840:h=2160:filter=lanczos:param_a=3:rangein=limited:range=limited,"
                         "format=yuv444p16le,setsar=1")
        self.assertEqual(dvnative.fit_pad_vf(640, 480),
                         "zscale=w=2880:h=2160:filter=lanczos:param_a=3:rangein=limited:range=limited,"
                         "format=yuv444p16le,setsar=1,setparams=range=pc,pad=3840:2160:480:0:color=black,"
                         "setparams=range=tv")

    def test_every_fit_says_square_pixels_before_the_pad(self):
        # 2026-10-18 review: zscale rewrites the sample aspect to keep the display aspect, and the
        # even rounding / hairline snap always move the ratio a little (FitFrontEndInFfmpeg runs it)
        for w, h, sar in ((1080, 1920, None), (854, 480, None), (720, 1280, None), (1920, 1080, None),
                          (640, 480, None), (720, 480, "8:9"), (3840, 2160, None)):
            with self.subTest(size=(w, h, sar)):
                vf = dvnative.fit_pad_vf(w, h, sar)
                self.assertIn("format=yuv444p16le,setsar=1", vf)
                self.assertLess(vf.index("setsar=1"), vf.index("pad=") if "pad=" in vf else len(vf))

    def test_colour_first_then_the_fit_as_one_plain_chain(self):
        info = {"width": 640, "height": 480, "color_space": "smpte170m", "color_primaries": "smpte170m",
                "color_transfer": "bt709", "color_range": "tv", "sample_aspect_ratio": "1:1"}
        vf = dvnative.fit_front_end(info)
        self.assertEqual(vf, dvnative.colour_vf("170m", "170m") + "," + dvnative.fit_pad_vf(640, 480))
        self.assertEqual(dvnative.front_end(vf), vf + ",")            # a legal pre_vf
        self.assertEqual(dvnative.fit_front_end(dict(info, color_space="bt709", color_primaries="bt709")),
                         dvnative.fit_pad_vf(640, 480))               # BT.709: the fit alone
        with self.assertRaises(ValueError):
            dvnative.fit_front_end(dict(info, color_transfer="smpte2084"))


def _identity_cube(path: str) -> str:
    with open(path, "w") as f:
        f.write("LUT_3D_SIZE 2\n" + "".join(f"{r} {g} {b}\n" for b in (0, 1) for g in (0, 1) for r in (0, 1)))
    return path


def _raw(vf: str, src_args: list, pix_fmt: str, size: tuple):
    import numpy as np
    out = subprocess.run([dvnative.FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", *src_args,
                          "-frames:v", "1", "-vf", vf, "-f", "rawvideo", "-pix_fmt", pix_fmt, "-"],
                         capture_output=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode(errors="replace"))
    return np.frombuffer(out.stdout, dtype="<u2")


@unittest.skipUnless(os.path.exists(dvnative.FFMPEG) and _have_numpy(), "ffmpeg/numpy not installed")
class FitFrontEndInFfmpeg(unittest.TestCase):
    """The fit front end run by the real ffmpeg/zimg: the BT.601 conversion is the standard one,
    the geometry lands where fit_geometry says, and the pad leaves the LUT chain as exactly the
    code source black does (an identity LUT stands in for the pinned one: only its black matters)."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_601_with_smpte_c_primaries_is_converted_to_709_display_referred(self):
        import numpy as np
        rgb = np.array([[0.75, 0.25, 0.10], [0.10, 0.80, 0.20], [0.20, 0.30, 0.90], [0.5, 0.5, 0.5]])

        def to_ycc(c, kr, kb):
            y = kr * c[:, 0] + (1 - kr - kb) * c[:, 1] + kb * c[:, 2]
            return np.stack([y * 219 * 256 + 4096, (c[:, 2] - y) / (2 - 2 * kb) * 224 * 256 + 32768,
                             (c[:, 0] - y) / (2 - 2 * kr) * 224 * 256 + 32768], 1)

        def xyz(prim):
            m = np.array([[x / y, 1, (1 - x - y) / y] for x, y in prim]).T
            return m * np.linalg.solve(m, [0.3127 / 0.3290, 1, (1 - 0.3127 - 0.3290) / 0.3290])
        to709 = np.linalg.solve(xyz([(0.64, 0.33), (0.30, 0.60), (0.15, 0.06)]),
                                xyz([(0.630, 0.340), (0.310, 0.595), (0.155, 0.070)]))   # SMPTE-C
        want = to_ycc(np.clip((to709 @ (rgb ** 2.4).T).T, 0, None) ** (1 / 2.4), 0.2126, 0.0722)
        src = os.path.join(self.d, "in.raw")
        frame = np.round(to_ycc(rgb, 0.299, 0.114)).astype("<u2").T.copy()      # planar Y, Cb, Cr
        frame.tofile(src)
        vf = ("setparams=range=tv:colorspace=smpte170m:color_primaries=smpte170m:color_trc=bt709,"
              + dvnative.colour_vf("170m", "170m"))
        got = _raw(vf, ["-f", "rawvideo", "-pix_fmt", "yuv444p16le", "-s", f"{len(rgb)}x1", "-i", src],
                   "yuv444p16le", (len(rgb), 1)).reshape(3, len(rgb)).T.astype(float)
        self.assertLess(np.abs(got - want).max(), 12)                 # of 65535: the standard conversion
        matrix_only = to_ycc(rgb, 0.2126, 0.0722)                     # the primaries left alone
        self.assertGreater(np.abs(got - matrix_only).max(), 1000)

    def test_the_pad_is_exactly_the_luts_black_and_the_picture_sits_where_the_geometry_says(self):
        import numpy as np
        cube = _identity_cube(os.path.join(self.d, "id.cube"))
        info = {"width": 64, "height": 48, "color_space": "bt709", "color_primaries": "bt709",
                "color_transfer": "bt709", "color_range": "tv"}
        frame = (96, 54)
        w, h, x, y = dvnative.fit_geometry(64, 48, frame=frame)
        self.assertEqual((w, h, x, y), (72, 54, 12, 0))
        vf = (dvnative.front_end(dvnative.fit_front_end(info, frame))
              + dvnative.lut_vf(cube, pix_fmt="yuv444p10le"))
        # a source that is black on the left half and white on the right half
        src = ["-f", "lavfi", "-i", "color=c=black:s=32x48,format=yuv420p[a];color=c=white:s=32x48,"
               "format=yuv420p[b];[a][b]hstack,setparams=range=tv:colorspace=bt709:color_primaries=bt709:"
               "color_trc=bt709"]
        a = _raw(vf, src, "yuv444p10le", frame).reshape(3, frame[1], frame[0])
        pad = np.concatenate([a[:, :, :x].reshape(3, -1), a[:, :, x + w:].reshape(3, -1)], axis=1)
        self.assertEqual({tuple(v) for v in pad.T}, {(64, 512, 512)})        # exactly PQ black
        self.assertEqual(tuple(a[:, 27, x + 4]), (64, 512, 512))             # ...as source black is
        self.assertGreater(int(a[0, 27, x + w - 1]), 900)                    # white to the last column

    def test_1080p_is_exactly_2x_and_fills_the_frame(self):
        info = {"width": 1920, "height": 1080, "color_space": "bt709", "color_primaries": "bt709",
                "color_transfer": "bt709", "color_range": "tv"}
        vf = dvnative.fit_front_end(info)
        out = subprocess.run([dvnative.FFMPEG, "-hide_banner", "-loglevel", "info", "-nostdin",
                              "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=1,format=yuv420p,"
                              "setparams=range=tv:colorspace=bt709", "-frames:v", "1", "-vf",
                              vf + ",showinfo", "-f", "null", "-"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("s:3840x2160", out.stderr)

    def test_the_whole_fit_and_lut_chain_hands_on_square_pixels(self):
        # 2026-10-18 review: without setsar=1 zscale left sar 1215:1216 on a vertical Short
        # (1080x1920 -> 1216 wide for 1215) and 1281:1280 on a snapped 854x480, and VideoToolbox /
        # MP4Box / the y4m pipe to x265 all carry it into the master
        cube = _identity_cube(os.path.join(self.d, "id.cube"))
        for w, h in ((1080, 1920), (854, 480), (720, 1280), (640, 480)):
            with self.subTest(size=(w, h)):
                info = {"width": w, "height": h, "color_space": "bt709", "color_primaries": "bt709",
                        "color_transfer": "bt709", "color_range": "tv", "sample_aspect_ratio": "1:1"}
                vf = dvnative.front_end(dvnative.fit_front_end(info)) + dvnative.lut_vf(cube)
                out = subprocess.run([dvnative.FFMPEG, "-hide_banner", "-loglevel", "info", "-nostdin",
                                      "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=1,format=yuv420p,"
                                      "setsar=1,setparams=range=tv:colorspace=bt709", "-frames:v", "1",
                                      "-vf", vf + ",showinfo", "-f", "null", "-"],
                                     capture_output=True, text=True)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertIn("sar:1/1 s:3840x2160", out.stderr)


class ChunkFrontEnd(unittest.TestCase):
    """dvnative.chunk_front_end (2026-10-18): a Topaz chunk's own tags pick the colour step, an
    untagged chunk is judged by the SOURCE's frame, non-square pixels are squared at the chunk's
    height, and a TV-lane chunk needs nothing at all."""
    SUNNY = {"width": 2880, "height": 2160, "sample_aspect_ratio": "1:1", "color_primaries": "smpte170m",
             "color_space": "smpte170m", "color_transfer": "smpte170m"}    # It's Always Sunny 2x06 chunk 0
    C601 = dvnative.colour_vf("170m", "170m")

    def test_a_tv_lane_chunk_needs_nothing(self):
        for info in ({"width": 3840, "height": 2160, "color_primaries": "bt709", "color_space": "bt709",
                      "color_transfer": "bt709", "color_range": "tv", "sample_aspect_ratio": "1:1"},
                     {"width": 2880, "height": 2160},                        # untagged 720p 4:3
                     {"width": 3840, "height": 1600, "sample_aspect_ratio": "0:1"}):
            with self.subTest(info=info):
                self.assertEqual(dvnative.chunk_front_end(info, {"width": 1280, "height": 720}), "")

    PILLAR = dvnative.fit_pad_vf(2880, 2160, "1:1")                       # 4:3 centred in 3840x2160

    def test_the_chunks_tags_decide_when_there_are_any(self):
        self.assertEqual(dvnative.chunk_front_end(self.SUNNY, {"width": 640, "height": 480}),
                         self.C601 + "," + self.PILLAR)                      # SD source: pillarboxed
        self.assertEqual(dvnative.chunk_front_end(self.SUNNY, {"width": 1920, "height": 1080}), self.C601)
        pal = dict(self.SUNNY, color_primaries="bt470bg", color_space="bt470bg", color_transfer="bt470bg")
        self.assertEqual(dvnative.chunk_front_end(pal), dvnative.colour_vf("470bg", "470bg"))
        self.assertEqual(dvnative.chunk_front_end(dict(self.SUNNY, color_range="pc")),
                         dvnative.colour_vf("170m", "170m", True))

    def test_an_untagged_chunk_is_judged_by_the_source_frame_not_its_own(self):
        bare = {"width": 2880, "height": 2160}
        self.assertEqual(dvnative.chunk_front_end(bare, {"width": 640, "height": 480}),
                         self.C601 + "," + self.PILLAR)
        self.assertEqual(dvnative.chunk_front_end(bare, {"width": 720, "height": 576}),
                         dvnative.colour_vf("470bg", "470bg") + "," + self.PILLAR)
        self.assertEqual(dvnative.chunk_front_end(bare, {"width": 960, "height": 720}), "")
        self.assertEqual(dvnative.chunk_front_end(bare), "")                     # no source: its own

    def test_non_square_pixels_are_squared_at_the_chunks_height(self):
        for sar, w in (("8:9", 2880), ("32:27", 3840), ("10:11", 2946), ("40:33", 3928)):
            with self.subTest(sar=sar):
                vf = dvnative.chunk_front_end({"width": 3240, "height": 2160, "sample_aspect_ratio": sar,
                                               "color_space": "bt709", "color_primaries": "bt709"})
                self.assertEqual(vf, f"zscale=w={w}:h=2160:filter=lanczos:param_a=3:rangein=limited:"
                                     "range=limited,format=yuv444p16le,setsar=1")
        vf = dvnative.chunk_front_end({"width": 3240, "height": 2160, "sample_aspect_ratio": "8:9"},
                                      {"width": 720, "height": 480})
        self.assertEqual(vf.split(",zscale=w=")[0], self.C601)                 # colour first
        self.assertEqual(dvnative.front_end(vf), vf + ",")                      # a legal pre_vf

    def test_an_sd_chunk_is_pillarboxed_like_resolves_masters(self):
        # Resolve shipped It's Always Sunny 2x06 as 3840x2160 with the 4:3 picture at x=480
        vf = dvnative.chunk_front_end(self.SUNNY, {"width": 640, "height": 480})
        self.assertIn("pad=3840:2160:480:0", vf)
        self.assertIn("setsar=1", vf)
        self.assertEqual(dvnative.front_end(vf), vf + ",")                      # a legal pre_vf
        hd = dvnative.chunk_front_end({"width": 2880, "height": 2160}, {"width": 1440, "height": 1080})
        self.assertNotIn("pad=", hd)                                            # 4:3 HD stays as it was

    def test_what_it_refuses(self):
        cases = {"no size": {"color_space": "bt709"},
                 "PQ": dict(self.SUNNY, color_transfer="smpte2084"),
                 "BT.2020": dict(self.SUNNY, color_primaries="bt2020", color_space="bt2020nc"),
                 "RGB": dict(self.SUNNY, color_space="gbr"),
                 "too wide square": {"width": 4096, "height": 2160, "sample_aspect_ratio": "4:3"}}
        for name, info in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                dvnative.chunk_front_end(info, {"width": 640, "height": 480})


@unittest.skipUnless(os.path.exists(dvnative.FFMPEG) and _have_numpy(), "ffmpeg/numpy not installed")
class ChunkFrontEndInFfmpeg(unittest.TestCase):
    """The chunk front end run by the real ffmpeg/zimg: an anamorphic chunk comes out square at
    its height (display aspect kept), a BT.601 chunk's colour is the same standard conversion
    the fit front end makes (FitFrontEndInFfmpeg pins that one against BT.1886), and the whole
    pre_vf + LUT chain runs."""

    def test_an_anamorphic_chunk_comes_out_square_at_its_height(self):
        vf = dvnative.chunk_front_end({"width": 360, "height": 240, "sample_aspect_ratio": "8:9",
                                       "color_space": "bt709", "color_primaries": "bt709"})
        out = subprocess.run([dvnative.FFMPEG, "-hide_banner", "-loglevel", "info", "-nostdin",
                              "-f", "lavfi", "-i", "testsrc2=size=360x240:rate=1,format=yuv420p,"
                              "setsar=8/9,setparams=range=tv:colorspace=bt709", "-frames:v", "1",
                              "-vf", vf + ",showinfo", "-f", "null", "-"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("s:320x240", out.stderr)
        self.assertIn("sar:1/1", out.stderr)

    def test_the_601_chunk_chain_runs_into_the_lut(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        cube = _identity_cube(os.path.join(d, "id.cube"))
        info = {"width": 64, "height": 48, "color_space": "smpte170m", "color_primaries": "smpte170m",
                "color_transfer": "smpte170m"}
        pre = dvnative.chunk_front_end(info, {"width": 1280, "height": 720})   # colour only, no pillarbox
        self.assertEqual(pre, dvnative.colour_vf("170m", "170m"))
        a = _raw(dvnative.front_end(pre) + dvnative.lut_vf(cube, pix_fmt="yuv444p10le"),
                 ["-f", "lavfi", "-i", "color=c=black:s=64x48,format=yuv420p,setparams=range=tv:"
                  "colorspace=smpte170m:color_primaries=smpte170m:color_trc=smpte170m"],
                 "yuv444p10le", (64, 48)).reshape(3, 48, 64)
        self.assertEqual({tuple(v) for v in a.reshape(3, -1).T}, {(64, 512, 512)})   # black stays black


@unittest.skipUnless(os.path.exists(dvnative.FFMPEG) and _have_numpy(), "ffmpeg/numpy not installed")
class SdPillarboxInFfmpeg(unittest.TestCase):
    def test_the_bars_are_exactly_black_and_the_picture_sits_at_x480(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        cube = _identity_cube(os.path.join(d, "id.cube"))
        info = {"width": 2880, "height": 2160, "color_space": "smpte170m", "color_primaries": "smpte170m",
                "color_transfer": "smpte170m", "sample_aspect_ratio": "1:1"}
        pre = dvnative.chunk_front_end(info, {"width": 640, "height": 480})
        a = _raw(dvnative.front_end(pre) + dvnative.lut_vf(cube, pix_fmt="yuv444p10le"),
                 ["-f", "lavfi", "-i", "color=c=white:s=2880x2160,format=yuv420p,setparams=range=tv:"
                  "colorspace=smpte170m:color_primaries=smpte170m:color_trc=smpte170m"],
                 "yuv444p10le", (3840, 2160)).reshape(3, 2160, 3840)
        self.assertEqual({tuple(v) for v in a[:, :, 8:472].reshape(3, -1).T}, {(64, 512, 512)})   # bars
        self.assertEqual({tuple(v) for v in a[:, :, 3368:3832].reshape(3, -1).T}, {(64, 512, 512)})
        self.assertGreater(a[0, 1080, 1920], 600)                                            # picture


class RangeIntermediate(unittest.TestCase):
    # The TV lane's whole-chunk argv (hardware decode, FFVHuff 4:4:4): the range keywords and
    # pre_vf default to it exactly.
    OLD = ["/ff", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-hwaccel", "videotoolbox",
           "-i", "/seg.mov", "-map", "0:v:0", "-vf", None, "-color_primaries", "bt2020",
           "-color_trc", "smpte2084", "-colorspace", "bt2020nc", "-color_range", "tv",
           "-c:v", "ffvhuff", "-pix_fmt", "yuv444p10le", "/c.mkv"]

    def test_whole_chunk_argv_is_unchanged(self):
        want = list(self.OLD)
        want[want.index(None)] = dvnative.analysis_vf("/l/rcm.cube")
        self.assertEqual(dvnative.build_analysis_intermediate_command("/ff", "/seg.mov", "/c.mkv", "/l/rcm.cube"),
                         want)
        self.assertEqual(dvnative.build_analysis_intermediate_command(
            "/ff", "/seg.mov", "/c.mkv", "/l/rcm.cube", hwaccel=False),
            [a for a in want if a not in ("-hwaccel", "videotoolbox")])

    def test_a_range_decodes_in_hardware_before_its_seek(self):
        c = dvnative.build_analysis_intermediate_command("/ff", "/src.mp4", "/c.mkv", None, start=240,
                                                         frames=96, fps="24000/1001")
        self.assertLess(c.index("-hwaccel"), c.index("-ss"))
        self.assertEqual(c[c.index("-c:v") + 1], "ffvhuff")
        self.assertNotIn("-hwaccel", dvnative.build_analysis_intermediate_command(
            "/ff", "/src.mp4", "/c.mkv", None, False, start=240, frames=96, fps="24000/1001"))

    def test_a_range_seeks_to_the_midpoint_bounds_the_count_and_passes_frames_through(self):
        c = dvnative.build_analysis_intermediate_command("/ff", "/src.mp4", "/c.mkv", None, start=240,
                                                         frames=96, fps="24000/1001", pre_vf="scale=3840:2160")
        self.assertEqual(c[c.index("-ss") + 1], f"{(240 - 0.5) / (24000 / 1001):.6f}")
        self.assertLess(c.index("-ss"), c.index("-i"))                      # input seek
        self.assertEqual(c[c.index("-frames:v") + 1], "96")
        self.assertEqual(c[c.index("-fps_mode") + 1], "passthrough")
        self.assertTrue(c[c.index("-vf") + 1].startswith(
            "setpts=N*1001/24000/TB,scale=3840:2160,scale=1920:1080:flags=area"))   # re-stamped from 0
        self.assertEqual(c[-1], "/c.mkv")

    def test_uniform_pts_from_the_rate(self):
        self.assertEqual(dvnative.uniform_pts("24000/1001"), "setpts=N*1001/24000/TB")
        self.assertEqual(dvnative.uniform_pts("25"), "setpts=N*1/25/TB")

    def test_frame_zero_needs_no_seek(self):
        c = dvnative.build_analysis_intermediate_command("/ff", "/src.mp4", "/c.mkv", None, start=0,
                                                         frames=10, fps="24000/1001")
        self.assertNotIn("-ss", c)
        self.assertIn("-frames:v", c)


class RangeValidation(unittest.TestCase):
    def test_contiguous_from_zero(self):
        self.assertEqual(dvnative.validate_ranges([[0, 5], (5, 9)]), [(0, 5), (5, 9)])
        for bad in ([], [(1, 5)], [(0, 5), (6, 9)], [(0, 5), (4, 9)], [(0, 0)]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                dvnative.validate_ranges(bad)


class L4Clamp(unittest.TestCase):
    def test_default_argv_unchanged_and_max_pq_only_when_it_differs(self):
        base = dvnative.build_l4_command("/py", "/c.mkv", "/o.json", ffmpeg="/ff")
        self.assertEqual(dvnative.build_l4_command("/py", "/c.mkv", "/o.json", ffmpeg="/ff", max_pq=3079), base)
        self.assertNotIn("--max-pq", base)
        self.assertEqual(dvnative.build_l4_command("/py", "/c.mkv", "/o.json", ffmpeg="/ff", max_pq=3388)[-2:],
                         ["--max-pq", "3388"])

    def test_the_child_refuses_a_code_outside_12_bits(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dvnative.main(["l4", "/x.mkv", "/o.json", "--max-pq", "5000"]), 2)

    @unittest.skipUnless(_have_numpy(), "numpy not installed")
    def test_white_clamps_to_the_masters_peak(self):
        import struct
        frame = struct.pack("<%dH" % 3, 940, 512, 512)              # one reference-white pixel
        self.assertAlmostEqual(dvnative.maxrgb_pq12_stats(frame, 1, 1)[0], 3079, delta=1)
        self.assertAlmostEqual(dvnative.maxrgb_pq12_stats(frame, 1, 1, max_pq=4095)[0], 4095, delta=1)


class RunStepPoll(unittest.TestCase):
    def test_on_poll_runs_while_the_child_does_and_its_errors_are_swallowed(self):
        calls = []

        def poll():
            calls.append(1)
            raise RuntimeError("a progress bug")
        rc, _tail, _cpu = dvnative.run_step([PY, "-c", "import time; time.sleep(0.4)"], timeout=30,
                                            poll=0.05, on_poll=poll)
        self.assertEqual(rc, 0)
        self.assertGreater(len(calls), 2)


class AnalyzeRanges(unittest.TestCase):
    """analyze_ranges with run_step faked, as AnalyzeChunks does."""

    RANGES = [(0, 24), (24, 54), (54, 72)]

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.work = os.path.join(self.d, "work")
        self.src = os.path.join(self.d, "src.mp4")
        open(self.src, "wb").close()
        self.calls, self.cmds = [], []
        self.short = {}                          # range index -> frames the analyzer claims

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _k(self, path):
        return int(os.path.basename(path).split("_")[1].split(".")[0])

    def fake_step(self, cmd, *, timeout, abort=None, should_pause=None, cwd=None, what="step", poll=0.5,
                  on_poll=None):
        cmd = cmd[3:]
        if cmd[0] == "/ff":
            step, k = "intermediate", self._k(cmd[-1])
            self.cmds.append(cmd)
            open(cmd[-1], "wb").close()
        elif cmd[0] == "/an":
            step, k = "analyzer", self._k(cmd[2])
            a, b = self.RANGES[k]
            with open(dvnative.sidecar_path(cmd[cmd.index("-o") + 1]), "w") as f:
                json.dump(_fake_sidecar(self.short.get(k, b - a)), f)
        else:
            step, k = "l4", self._k(cmd[4])
            a, b = self.RANGES[k]
            with open(cmd[5], "w") as f:
                json.dump([[k, 1]] * (b - a), f)
        self.assertEqual(timeout, dvnative.step_timeout(self.RANGES[k][1] - self.RANGES[k][0]))
        self.assertTrue(what.startswith(f"range {k + 1} "))
        self.calls.append((k, step))
        return 0, "", 0.5

    def analyze(self, **kw):
        kw.setdefault("cube", None)
        with mock.patch.object(dvnative, "run_step", side_effect=self.fake_step):
            return dvnative.analyze_ranges(self.src, self.RANGES, self.work, fps="24000/1001",
                                           analyzer="/an", ffmpeg="/ff", python="/py", **kw)

    def test_every_range_measured_seeked_and_merged(self):
        sc = self.analyze(pre_vf="scale=3840:2160")
        self.assertEqual(dvnative.stream_frames(sc), 72)
        self.assertEqual([s["start"] for s in sc["scenes"]], [0, 24, 54])
        self.assertEqual(len(sc["l4"]), 72)
        self.assertNotIn("-ss", self.cmds[0])
        for cmd, (a, b) in zip(self.cmds, self.RANGES):
            self.assertEqual(cmd[cmd.index("-frames:v") + 1], str(b - a))
            self.assertEqual(cmd[cmd.index("-i") + 1], self.src)
            self.assertTrue(cmd[cmd.index("-vf") + 1].startswith("setpts=N*1001/24000/TB,scale=3840:2160,"))
        self.assertEqual(self.cmds[1][self.cmds[1].index("-ss") + 1], dvcap_ss(24))
        self.assertEqual(sorted(n for n in os.listdir(self.work) if n.endswith((".mkv", ".part"))), [])

    def test_resume_measures_only_the_unfinished_range(self):
        self.analyze()
        self.calls.clear()
        os.remove(os.path.join(self.work, "range_0001_24_30.bin.l1.json"))
        done = []
        self.analyze(on_range=lambda d, of: done.append((d, of)))
        # the analyzer and the L4 pass run side by side on the intermediate (the overlapped loop)
        self.assertEqual(self.calls[0], (1, "intermediate"))
        self.assertEqual(sorted(self.calls[1:]), [(1, "analyzer"), (1, "l4")])
        self.assertEqual(done, [(1, 3), (2, 3), (3, 3)])

    def test_a_different_plan_never_reuses_a_range(self):
        self.analyze()
        self.calls.clear()
        self.RANGES = [(0, 30), (30, 54), (54, 72)]
        self.analyze()
        self.assertEqual({k for k, _s in self.calls}, {0, 1})             # range 2 is the same range

    def test_before_range_pauses_with_its_reason(self):
        with self.assertRaises(dvnative.Paused) as cm:
            self.analyze(before_range=lambda k, n: "low disk" if k == 2 else None)
        self.assertEqual(str(cm.exception), "paused: low disk")
        self.assertNotIn(2, {k for k, _s in self.calls})

    def test_should_pause_names_the_range(self):
        with self.assertRaises(dvnative.Paused) as cm:
            self.analyze(should_pause=lambda: True)
        self.assertEqual(str(cm.exception), "paused: yielded before analysis range 1 of 3")

    def test_a_short_range_is_refused_and_never_reused(self):
        self.short[1] = 29
        with self.assertRaisesRegex(RuntimeError, "range 1: analysis covers 29 frames, the range has 30"):
            self.analyze()
        self.assertFalse(os.path.exists(os.path.join(self.work, "range_0001_24_30.bin.l1.json")))

    def test_master_peak_reaches_the_l4_child(self):
        seen = []
        real = self.fake_step

        def spy(cmd, **kw):
            seen.append(cmd)
            return real(cmd, **kw)
        with mock.patch.object(dvnative, "run_step", side_effect=spy):
            dvnative.analyze_ranges(self.src, self.RANGES, self.work, fps="24000/1001", cube=None,
                                    analyzer="/an", ffmpeg="/ff", python="/py",
                                    master=dvnative.MasterSpec(max_nits=4000))
        l4 = [c for c in seen if "l4" in c]
        self.assertTrue(l4 and all(c[-2:] == ["--max-pq", str(dvnative.nits_to_pq12(4000))] for c in l4))

    def test_the_frame_rate_is_required(self):
        for fps in (None, "0/1", "x"):
            with self.subTest(fps=fps), self.assertRaises(ValueError):
                dvnative.analyze_ranges(self.src, self.RANGES, self.work, fps=fps, cube=None)


def dvcap_ss(a, fps="24000/1001"):
    import dvcap
    return f"{dvcap.seg_seek_seconds(a, fps):.6f}"


@unittest.skipUnless(os.path.exists(dvnative.FFMPEG), "ffmpeg not installed")
class RangeIntermediateKeepsExactlyItsFrames(unittest.TestCase):
    """REAL ffmpeg: a range's intermediate must hold exactly frames [a, a+n) of the source —
    not a, a, a+1 (the duplicated-first-frame bug dvcap and the mezzanine both had), not a
    frame late. Every source frame has its own grey level; the range's frames are identified
    against the WHOLE file's intermediate (same chain, so the levels compare like for like).
    Checked on an MP4 (exact timebase) and an MKV (1 ms timebase)."""

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        ff = dvnative.FFMPEG
        cls.mp4 = os.path.join(cls.d, "levels.mp4")
        r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            "nullsrc=s=64x36:r=24000/1001:d=1,geq=lum='60+7*N':cb=128:cr=128",
                            "-frames:v", "24", "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv444p",
                            cls.mp4], capture_output=True)
        if r.returncode != 0:
            raise unittest.SkipTest("libx264 unavailable")
        cls.mkv = os.path.join(cls.d, "levels.mkv")
        subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", cls.mp4, "-c", "copy", cls.mkv],
                       check=True)
        # 4:2:0 High profile: what VideoToolbox decodes IN HARDWARE (the 4:4:4 lossless files above
        # fall back to software), so the hwaccel range read is checked frame-exact too.
        cls.hw = os.path.join(cls.d, "levels420.mp4")
        subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "nullsrc=s=64x36:r=24000/1001:d=1,geq=lum='60+7*N':cb=128:cr=128",
                        "-frames:v", "24", "-c:v", "libx264", "-qp", "2", "-pix_fmt", "yuv420p",
                        "-profile:v", "high", "-bf", "2", cls.hw], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, ignore_errors=True)

    @staticmethod
    def _levels(path):
        # passthrough: rawvideo is a constant-rate muxer, and its default cfr would pad this
        # decode itself (the very bug under test), hiding or faking a shift
        out = subprocess.run([dvnative.FFMPEG, "-hide_banner", "-loglevel", "error", "-i", path,
                              "-fps_mode", "passthrough", "-vf", "scale=8:8", "-pix_fmt", "gray",
                              "-f", "rawvideo", "-"], capture_output=True).stdout
        return [sum(out[i * 64:(i + 1) * 64]) / 64 for i in range(len(out) // 64)]

    def test_each_range_is_exactly_its_frames(self):
        for src, hw in ((self.mp4, True), (self.mkv, True), (self.hw, True), (self.hw, False)):
            whole = os.path.join(self.d, "whole.mkv")
            subprocess.run(dvnative.build_analysis_intermediate_command(dvnative.FFMPEG, src, whole, None,
                                                                        hwaccel=False),
                           capture_output=True, check=True)
            table = self._levels(whole)
            self.assertEqual(len(table), 24)
            self.assertEqual(len(set(round(v) for v in table)), 24)     # every frame tells apart
            for a, n in ((0, 5), (7, 6), (19, 5)):       # no seek, a mid seek, the tail
                with self.subTest(src=os.path.basename(src), hwaccel=hw, a=a):
                    out = os.path.join(self.d, f"r_{a}.mkv")
                    r = subprocess.run(dvnative.build_analysis_intermediate_command(
                        dvnative.FFMPEG, src, out, None, hw, start=a, frames=n, fps="24000/1001"),
                        capture_output=True, text=True)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    got = [min(range(len(table)), key=lambda k: abs(table[k] - m)) for m in self._levels(out)]
                    self.assertEqual(got, list(range(a, a + n)))
                    # ...and the L4 child's own decode (no -fps_mode) sees those frames, unpadded
                    l4 = subprocess.run(dvnative.build_raw444_decode_command(dvnative.FFMPEG, out),
                                        capture_output=True).stdout
                    exact = subprocess.run(dvnative.build_raw444_decode_command(dvnative.FFMPEG, out)[:-1]
                                           + ["-fps_mode", "passthrough", "-"], capture_output=True).stdout
                    self.assertEqual(len(l4), n * 1920 * 1080 * 3 * 2)
                    self.assertEqual(l4, exact)



# ---- HDR intake (2026-10-18): a source's own mastering, exact L6 light, the HDR front end -------

HAMILTON_MDCV = "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)"   # the real SEI
P3_4000 = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(40000000,50)"


class SourceMastering(unittest.TestCase):
    """An HDR10 source's SEI -> the MasterSpec that keeps its mastering and the L6 light levels
    declared as they stand."""

    def test_the_hamilton_sei_keeps_its_bt2020_display_and_unknown_light(self):
        m, light = dvnative.source_mastering(HAMILTON_MDCV, "0,0")
        self.assertEqual((m.max_nits, m.min_nits, m.trim_targets, m.mdcv), (1000, 0.0001, (100, 600, 1000),
                                                                             HAMILTON_MDCV))
        self.assertEqual((m.source_max_pq, m.source_min_pq, m.l6_min, m.hdr10_mdcv), (3079, 7, 1, HAMILTON_MDCV))
        self.assertEqual(light, (0, 0))

    def test_a_4000_nit_p3_master_moves_every_range_and_keeps_its_light(self):
        m, light = dvnative.source_mastering(P3_4000, "850,210")
        self.assertEqual((m.max_nits, m.l6_min, m.source_max_pq, m.source_min_pq),
                         (4000, 50, dvnative.nits_to_pq12(4000), dvnative.nits_to_pq12(0.005)))
        self.assertEqual(m.trim_targets, (100, 600, 1000))
        self.assertEqual(light, (850, 210))

    def test_trim_targets_above_the_peak_are_dropped(self):
        md = HAMILTON_MDCV.replace("L(10000000,1)", "L(6000000,1)")
        self.assertEqual(dvnative.source_mastering(md, None)[0].trim_targets, (100, 600))

    def test_missing_metadata_is_the_default_master_and_unknown_light(self):
        self.assertEqual(dvnative.source_mastering(None, None), (dvnative.DEFAULT_MASTER, (0, 0)))
        self.assertEqual(dvnative.source_mastering("", "1200,300"), (dvnative.DEFAULT_MASTER, (1200, 300)))

    def test_a_zero_minimum_is_kept_when_the_source_says_so(self):
        md = HAMILTON_MDCV.replace("L(10000000,1)", "L(10000000,0)")
        m, _light = dvnative.source_mastering(md, "0,0")
        self.assertEqual((m.min_nits, m.l6_min, m.source_min_pq), (0, 0, 0))
        with self.assertRaises(ValueError):          # ...but never on a master with no display of its own
            dvnative.MasterSpec(min_nits=0)

    def test_every_refusal_says_why(self):
        for md, cll, words in (
                (HAMILTON_MDCV.replace("L(10000000,1)", "L(10005000,1)"), None, "1000.5 nits"),
                (HAMILTON_MDCV.replace("L(10000000,1)", "L(500000,1)"), None, "50 nits"),
                (HAMILTON_MDCV.replace("L(10000000,1)", "L(100010000,1)"), None, "10001 nits"),
                (HAMILTON_MDCV.replace("L(10000000,1)", "L(10000000,10000000)"), None, "mastering range"),
                ("L(10000000,1)", None, "does not read"),
                (None, "lots", "does not read"),
                (None, "70000,1", "out of range")):
            with self.subTest(md=md, cll=cll), self.assertRaises(ValueError) as cm:
                dvnative.source_mastering(md, cll)
            self.assertIn(words, str(cm.exception))


class ExactLight(unittest.TestCase):
    """rpu_config(light=...) declares an HDR source's own MaxCLL/MaxFALL in L6 exactly as given:
    not measured, not clamped. Without it nothing changes."""

    def setUp(self):
        self.sc = _sidecar([_scene(0, 9, 1, 1500, 3000)], cll=900, fall=200)

    def test_light_is_declared_as_given_even_out_of_spec(self):
        for light in ((0, 0), (1500, 1600), (850, 210)):
            cfg = dvnative.rpu_config(self.sc, light=light, max_cll=1, max_fall=1)
            self.assertEqual((cfg["level6"]["max_content_light_level"],
                              cfg["level6"]["max_frame_average_light_level"]), light)

    def test_no_light_is_exactly_the_old_rule(self):
        for sc in (self.sc, _e2e_sidecar()):
            self.assertEqual(dvnative.rpu_config(sc), dvnative.rpu_config(sc, light=None))
        self.assertEqual(dvnative.rpu_config(self.sc)["level6"]["max_content_light_level"], 900)

    def test_generate_passes_it_through(self):
        d = tempfile.mkdtemp()
        try:
            with mock.patch.object(dvnative, "rpu_config", wraps=dvnative.rpu_config) as cfg, \
                 mock.patch.object(dvnative.subprocess, "run", side_effect=OSError("no tool here")):
                with self.assertRaises(OSError):
                    dvnative.generate_rpu(self.sc, os.path.join(d, "r.bin"), light=(0, 0))
            self.assertEqual(cfg.call_args.kwargs["light"], (0, 0))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    @unittest.skipUnless(os.path.exists(dvnative.DOVI_TOOL), "dovi_tool not installed")
    def test_dovi_tool_writes_a_source_master_and_zero_light(self):
        d = tempfile.mkdtemp()
        try:
            m, light = dvnative.source_mastering(P3_4000, "0,0")
            rpu = os.path.join(d, "rpu.bin")
            dvnative.generate_rpu(_sidecar([_scene(0, 23, 1, 1500, 3500)], cll=900, fall=200), rpu,
                                  master=m, light=light)
            out = subprocess.run([dvnative.DOVI_TOOL, "info", "-i", rpu, "-f", "0"],
                                 capture_output=True, text=True).stdout
            vdr = json.loads(out[out.find("{"):])["vdr_dm_data"]
            l6 = next(b["Level6"] for b in vdr["cmv29_metadata"]["ext_metadata_blocks"] if "Level6" in b)
            self.assertEqual(l6, {"max_display_mastering_luminance": 4000, "min_display_mastering_luminance": 50,
                                  "max_content_light_level": 0, "max_frame_average_light_level": 0})
            self.assertEqual((vdr["source_min_pq"], vdr["source_max_pq"]), (m.source_min_pq, m.source_max_pq))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class HdrFrontEnd(unittest.TestCase):
    PQ = {"color_transfer": "smpte2084", "color_primaries": "bt2020", "color_space": "bt2020nc",
          "color_range": "tv"}

    def test_pq_needs_none_and_hlg_is_converted(self):
        self.assertIsNone(dvnative.hdr_front_end(self.PQ))
        self.assertIsNone(dvnative.hdr_front_end({"color_transfer": "smpte2084"}))      # BT.2100 = BT.2020
        hlg = dict(self.PQ, color_transfer="arib-std-b67")
        self.assertEqual(dvnative.hdr_front_end(hlg), dvnative.hlg_to_pq_vf())
        self.assertEqual(dvnative.hlg_to_pq_vf(),
                         "zscale=matrixin=2020_ncl:transferin=arib-std-b67:primariesin=2020:rangein=limited:"
                         "matrix=2020_ncl:transfer=smpte2084:primaries=2020:range=limited,"
                         "format=yuv444p16le")
        self.assertEqual(dvnative.front_end(dvnative.hlg_to_pq_vf()), dvnative.hlg_to_pq_vf() + ",")

    def test_every_refusal_says_why(self):
        for info, words in ((dict(self.PQ, color_transfer="bt709"), "bt709, not PQ or HLG"),
                            (dict(self.PQ, color_transfer=None), "untagged, not PQ or HLG"),
                            (dict(self.PQ, color_primaries="bt709"), "primaries are bt709"),
                            (dict(self.PQ, color_space="bt2020c"), "matrix are bt2020c"),
                            (dict(self.PQ, color_range="pc"), "full range")):
            with self.subTest(info=info), self.assertRaises(ValueError) as cm:
                dvnative.hdr_front_end(info)
            self.assertIn(words, str(cm.exception))


def _hlg_inverse_oetf(e: float) -> float:
    import math
    a = 0.17883277
    b, c = 1 - 4 * a, 0.5 - a * math.log(4 * a)
    return e * e / 3 if e <= 0.5 else (math.exp((e - c) / a) + b) / 12


def _pq_signal(nits: float) -> float:
    m1, m2, c1, c2, c3 = 2610 / 16384, 2523 / 32, 3424 / 4096, 2413 / 128, 2392 / 128
    y = max(0.0, nits) / 10000
    return ((c1 + c2 * y ** m1) / (1 + c3 * y ** m1)) ** m2


@unittest.skipUnless(os.path.exists(dvnative.FFMPEG), "ffmpeg not installed")
class HlgToPqInFfmpeg(unittest.TestCase):
    """The real zscale against BT.2100 itself: grey HLG levels -> display light through the
    OOTF at 1000 nits (gamma 1.2) -> PQ, as 10-bit limited codes. Measured 2026-10-18: within
    0.025 of a code once the reference uses the same 10-bit input codes; HLG's 75% reference white
    lands on 203 nits (BT.2408), 100% on exactly 1000; neutral chroma stays neutral."""

    def test_grey_levels_match_bt2100(self):
        import numpy as np
        w, h, levels = 64, 8, (0.0, 0.1, 0.25, 0.5, 0.6, 0.75, 0.9, 1.0)
        codes = [int(round(64 + e * 876)) for e in levels]
        y = np.zeros((h, w), np.uint16)
        for i, c in enumerate(codes):
            y[:, i * 8:(i + 1) * 8] = c << 6
        cbcr = np.full((h, w), 512 << 6, np.uint16)
        r = subprocess.run([dvnative.FFMPEG, "-v", "error", "-f", "rawvideo", "-pix_fmt", "yuv444p16le",
                            "-s", f"{w}x{h}", "-i", "-", "-vf", dvnative.hlg_to_pq_vf(), "-f", "rawvideo",
                            "-pix_fmt", "yuv444p16le", "-"],
                           input=y.tobytes() + cbcr.tobytes() + cbcr.tobytes(), capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        out = np.frombuffer(r.stdout, "<u2").reshape(3, h, w).astype(float) / 64
        for i, c in enumerate(codes):
            e = (c - 64) / 876
            nits = dvnative.HLG_NPL * _hlg_inverse_oetf(e) ** 1.2   # grey: the OOTF's Ys is the level
            want = 64 + _pq_signal(nits) * 876
            with self.subTest(level=levels[i]):
                self.assertAlmostEqual(out[0, :, i * 8:(i + 1) * 8].mean(), want, delta=0.1)
                self.assertAlmostEqual(out[1, :, i * 8 + 4].mean(), 512, delta=0.05)
                self.assertAlmostEqual(out[2, :, i * 8 + 4].mean(), 512, delta=0.05)
        self.assertAlmostEqual(1000 * _hlg_inverse_oetf(0.75) ** 1.2, 203, delta=0.5)   # BT.2408 white
        self.assertEqual(dvnative.HLG_NPL, 1000)
        self.assertAlmostEqual(out[0, :, 56:64].mean(), 64 + _pq_signal(1000) * 876, delta=0.05)   # E' 1 -> 1000


if __name__ == "__main__":
    unittest.main()
