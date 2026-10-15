import json
import os
import shutil
import subprocess
import tempfile
import unittest

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
    def test_lut_then_rgb_area_downscale_then_444_ffv1_tagged_pq(self):
        c = dvnative.build_analysis_intermediate_command("/ff", "/seg_0001.mov", "/c.mkv", "/l/rcm.cube")
        vf = c[c.index("-vf") + 1]
        self.assertLess(vf.index("lut3d"), vf.index("scale=1920:1080:flags=area"))
        self.assertLess(vf.index("scale=1920:1080"), vf.index("matrix=2020_ncl"))   # downscale in RGB
        self.assertTrue(vf.endswith("format=yuv444p10le"))
        self.assertNotIn("yuv420", vf)                       # never through 4:2:0 on the way
        self.assertEqual(c[c.index("-color_trc") + 1], "smpte2084")
        self.assertEqual(c[c.index("-c:v") + 1], "ffv1")
        self.assertEqual(c[-1], "/c.mkv")

    def test_already_pq_input_needs_no_lut(self):
        c = dvnative.build_analysis_intermediate_command("/ff", "/x.mov", "/c.mkv", None)
        self.assertEqual(c[c.index("-vf") + 1], "scale=1920:1080:flags=area,format=yuv444p10le")


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


if __name__ == "__main__":
    unittest.main()
