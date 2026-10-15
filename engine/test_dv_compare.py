"""tools/dv_compare.py: gate Q2, the native L1 that ships against pixel ground truth (never
against Resolve, whose CM v4 L1 is a mapping-oriented value, not a measurement). Pure pieces
are tested without media; a real dovi_tool round trip and two tiny lossless-HEVC clips (frame
exactness of the truth decode, and the whole gate end to end) skip when the tools are absent."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest

import dvcap
import dvnative


def _load_compare():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "dv_compare.py")
    spec = importlib.util.spec_from_file_location("dv_compare", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dc = _load_compare()

HAVE_DOVI = os.path.exists(dvnative.DOVI_TOOL)
HAVE_FFMPEG = os.path.exists(dvcap.FFMPEG) and os.path.exists(dvcap.FFPROBE)


def _frame(w, h, ys, cb=512, cr=512):
    """yuv444p10le bytes: luma list (row-major), flat chroma."""
    n = w * h
    return struct.pack("<%dH" % (3 * n), *(list(ys) + [cb] * n + [cr] * n))


def _code(y):
    """12-bit PQ code of a neutral pixel with 10-bit limited luma `y`."""
    return (y - 64) / 876 * 4095


def _nat(shots, l6=(1000, 1, 1000, 1000), l4=None):
    """rpu_frames-shaped native: shots = [(frames, (min, max, avg)), ...]."""
    l1 = [l for n, l in shots for _ in range(n)]
    return {"frames": len(l1), "l1": l1, "l3": [None] * len(l1), "v4": l1, "l4": l4,
            "l6": [l6] if l6 else [], "source_max_pq": [3079]}


def _truth_for(nat, per_shot):
    """Sampled shots + truth: per_shot = [(mean, p9999), ...] one per native shot."""
    sampled, truth = [], {}
    for (a, b), (mean, p) in zip(dc.shots_from_l1(nat["l1"]), per_shot):
        frames = [a + o for o in dc.sample_offsets(b - a, 4)]
        for f in frames:
            truth[f] = {"mean": mean, "p9999": p, "min": 0.0, "std": 100.0}
        sampled.append((a, b, frames))
    return sampled, truth


class FrameTruth(unittest.TestCase):
    def test_white_and_black_halves(self):
        t = dc.frame_truth(_frame(4, 2, [940] * 4 + [64] * 4), 4, 2)
        self.assertAlmostEqual(t["mean"], 4095 / 2, delta=0.01)    # mean is NOT clamped
        self.assertEqual(t["p9999"], 3079)                          # the declared source max
        self.assertAlmostEqual(t["min"], 0, delta=0.01)
        self.assertAlmostEqual(t["std"], 4095 / 2, delta=0.01)

    def test_max_rgb_takes_the_brightest_channel(self):
        # Y 0.5, Cr +100: R = 0.5 + 1.4746 * 100/896 is the largest channel
        t = dc.frame_truth(_frame(2, 2, [502] * 4, cr=612), 2, 2)
        self.assertAlmostEqual(t["mean"], (0.5 + 1.4746 * 100 / 896) * 4095, delta=0.05)

    def test_p9999_ignores_a_lone_hot_pixel_and_p01_is_the_min(self):
        grey = _code(500)
        t = dc.frame_truth(_frame(100, 100, [940] + [500] * 9999), 100, 100)
        self.assertAlmostEqual(t["p9999"], grey, delta=1)           # a true max would say 3079
        self.assertAlmostEqual(dc.frame_truth(_frame(100, 100, [64] * 5 + [500] * 9995), 100, 100)["min"],
                               grey, delta=0.01)                    # 0.05% black: above p0.1
        self.assertAlmostEqual(dc.frame_truth(_frame(100, 100, [64] * 20 + [500] * 9980), 100, 100)["min"],
                               0, delta=0.01)                       # 0.2% black: p0.1 is black

    def test_shot_truth(self):
        s = dc.shot_truth([{"mean": 1000, "p9999": 2500, "min": 3}, {"mean": 1200, "p9999": 2900, "min": 1}])
        self.assertEqual(s, {"mean": 1100, "max": 2900, "min": 1})


class ShotsAndSampling(unittest.TestCase):
    def test_shots_are_runs_of_identical_l1(self):
        A, B = (0, 2900, 1500), (1, 3000, 1600)
        self.assertEqual(dc.shots_from_l1([A, A, B, B, B, A]), [(0, 2), (2, 5), (5, 6)])
        self.assertEqual(dc.shots_from_l1([]), [])

    def test_every_shot_when_few(self):
        shots = [(i, i + 1) for i in range(12)]
        l1 = [(0, 2500, 1300 + i) for i in range(12)]
        self.assertEqual(dc.sample_shots(shots, l1, n=30), list(range(12)))

    def test_stratified_extremes_plus_seeded_random(self):
        shots = [(i, i + 1) for i in range(100)]
        l1 = [(0, 2500, 1300 + (i * 37) % 100) for i in range(100)]   # avg is a permutation
        pick = dc.sample_shots(shots, l1, n=30, extremes=5, seed=0)
        self.assertEqual(len(pick), 30)
        by_avg = sorted(range(100), key=lambda i: l1[i][2])
        self.assertTrue(set(by_avg[:5]) <= set(pick) and set(by_avg[-5:]) <= set(pick))
        self.assertEqual(pick, dc.sample_shots(shots, l1, n=30, extremes=5, seed=0))   # reproducible
        self.assertNotEqual(pick, dc.sample_shots(shots, l1, n=30, extremes=5, seed=1))

    def test_black_shots_are_not_the_darkest_stratum(self):
        shots = [(i, i + 1) for i in range(60)]
        l1 = [(0, 2081, 1229)] * 10 + [(0, 2100 + 10 * i, 1229) for i in range(10)] + \
             [(0, 3079, 1300 + i) for i in range(40)]
        pick = dc.sample_shots(shots, l1, n=12, extremes=5, seed=0)
        self.assertTrue(set(range(10, 15)) <= set(pick))      # dimmest with a real max, not black

    def test_frames_without_l1_are_left_out_not_a_crash(self):
        shots = [(i, i + 1) for i in range(40)]
        l1 = [(0, 2500, 1300 + i) for i in range(37)] + [None] * 3
        pick = dc.sample_shots(shots, l1, n=30, extremes=5, seed=0)
        self.assertEqual(len(pick), 30)
        self.assertFalse(set(pick) & {37, 38, 39})

    def test_never_more_than_n_even_with_wide_extremes(self):
        shots = [(i, i + 1) for i in range(100)]
        l1 = [(0, 2500, 1300 + i) for i in range(100)]
        self.assertEqual(len(dc.sample_shots(shots, l1, n=3, extremes=5, seed=0)), 3)

    def test_offsets_spread_over_the_shot(self):
        self.assertEqual(dc.sample_offsets(3, 6), [0, 1, 2])
        self.assertEqual(dc.sample_offsets(60, 6), [5, 15, 25, 35, 45, 55])
        o = dc.sample_offsets(7, 6)
        self.assertEqual((len(o), min(o) >= 0, max(o) <= 6), (6, True, True))


class DeliveredClamps(unittest.TestCase):
    """What `dovi_tool generate` 2.3.0 carries for a given L1 (measured by round trip; the
    real-tool class below re-checks these against the installed binary)."""

    def test_pinned_round_trip_cases(self):
        self.assertEqual(dc.deliverable(40, 3400, 700), (12, 3079, 1229))     # source clamp + floors
        self.assertEqual(dc.deliverable(5, 2000, 1500), (5, 2081, 1500))      # max floor 2081
        self.assertEqual(dc.deliverable(0, 2500, 2600), (0, 2500, 2499))      # avg < max
        self.assertEqual(dc.deliverable(0, 1500, 500, 819), (0, 2081, 819))   # the v2.9 floor
        self.assertEqual(dc.deliverable(*dc.deliverable(40, 3400, 700)), (12, 3079, 1229))   # idempotent

    def test_avg_floor_follows_the_config(self):
        self.assertEqual(dc.avg_floor_of({"cm_version": "V40"}), 1229)
        self.assertEqual(dc.avg_floor_of({"cm_version": "V40", "l1_avg_pq_cm_version": "V29"}), 819)
        sc = {"version": 5, "scenes": [{"start": 0, "end": 9, "min_pq_12bit": 1,
                                         "max_pq_12bit": 2900, "avg_max_rgb_pq_12bit": 1500}]}
        self.assertEqual(dc.avg_floor_of(dvnative.rpu_config(sc)), 1229)       # what native ships


class V4View(unittest.TestCase):
    def test_missing_l3_is_neutral(self):
        self.assertEqual(dc.v4_view((0, 3079, 2048), None), (0, 3079, 2048))
        self.assertIsNone(dc.v4_view(None, (2048, 2048, 1434)))

    def test_offsets_apply_and_clamp(self):
        # Resolve's typical frame: L1 avg pinned at 2048, the real adjustment in L3
        self.assertEqual(dc.v4_view((0, 3079, 2048), (2048, 2048, 1434)), (0, 3079, 1434))
        self.assertEqual(dc.v4_view((10, 4000, 100), (0, 3000, 2048)), (0, 4095, 100))


class Gate(unittest.TestCase):
    TRUTH = [(1500.0, 2400.0), (1800.0, 2700.0), (2100.0, 2900.0), (1650.0, 3079.0)]

    def _accurate(self, d_avg=0, d_max=0):
        shots = [(10 + i, (5, int(p) + d_max, int(m) + d_avg)) for i, (m, p) in enumerate(self.TRUTH)]
        return _nat(shots)

    def test_an_accurate_measurement_passes(self):
        nat = self._accurate()
        sampled, truth = _truth_for(nat, self.TRUTH)
        rep = dc.gate(nat, sampled, truth, expected_frames=nat["frames"])
        v = dc.verdict(rep)
        self.assertTrue(v["pass"], v)
        self.assertEqual((rep["truth"]["avg"]["median"], rep["truth"]["max"]["median"]), (0, 0))
        self.assertEqual(rep["truth"]["shots_sampled"], 4)

    def test_thresholds(self):
        for d_avg, d_max, ok in ((15, 0, True), (16, 0, False), (0, 30, True), (0, 31, False)):
            shots = [(10 + i, (5, min(int(p) + d_max, 3079), int(m) + d_avg))
                     for i, (m, p) in enumerate(self.TRUTH[:3])]          # p + 31 stays <= 3079
            nat = _nat(shots)
            sampled, truth = _truth_for(nat, self.TRUTH[:3])
            v = dc.verdict(dc.gate(nat, sampled, truth, expected_frames=nat["frames"]))
            self.assertEqual(v["pass"], ok, (d_avg, d_max, v))

    def test_an_unmeasured_constant_rpu_fails(self):
        # Resolve-like constant L1 (0, 3079, 2048): one "shot" spanning everything
        nat = _nat([(40, (0, 3079, 2048))])
        frames = [5, 15, 25, 35]
        truth = {f: {"mean": m, "p9999": 2500.0, "min": 0.0, "std": 1.0}
                 for f, m in zip(frames, (1300.0, 1500.0, 1700.0, 1900.0))}
        v = dc.verdict(dc.gate(nat, [(0, 40, frames)], truth, expected_frames=40))
        self.assertFalse(v["pass"])
        self.assertFalse(v["checks"]["median_davg"])
        self.assertFalse(v["checks"]["median_dmax"])

    def test_a_shot_darker_than_the_floors_is_not_an_error(self):
        nat = _nat([(10, (12, 2081, 1229)), (10, (5, 2900, 1800))])
        sampled, truth = _truth_for(nat, [(600.0, 1500.0), (1800.0, 2900.0)])
        rep = dc.gate(nat, sampled, truth, expected_frames=20)
        self.assertTrue(dc.verdict(rep)["pass"])
        row = rep["truth"]["shots"][0]
        self.assertEqual((row["d_avg"], row["d_max"], row["avg_bound"], row["max_bound"]), (0, 0, True, True))
        self.assertEqual((rep["truth"]["avg"]["bound_shots"], rep["truth"]["avg"]["n"]), (1, 1))
        self.assertGreater(rep["truth"]["avg"]["median_raw"], 0)     # the raw gap is still reported

    def test_a_bad_measurement_cannot_hide_behind_floored_shots(self):
        # 4 dark shots the floors decide (right by construction) + 3 bright ones read 40 hot
        shots = [(10, (i, 2081, 1229)) for i in range(4)]             # distinct L1: 4 shots
        shots += [(10, (5, 2600 + 100 * i, 1640 + 100 * i)) for i in range(3)]
        nat = _nat(shots)
        per = [(500.0, 1500.0)] * 4 + [(1600.0 + 100 * i, 2600.0 + 100 * i) for i in range(3)]
        sampled, truth = _truth_for(nat, per)
        rep = dc.gate(nat, sampled, truth, expected_frames=70)
        self.assertEqual(rep["truth"]["avg"]["median_all"], 0)        # the plain median: a pass
        self.assertEqual((rep["truth"]["avg"]["median"], rep["truth"]["avg"]["n"]), (40, 3))
        self.assertFalse(dc.verdict(rep)["checks"]["median_davg"])

    def test_max_on_the_ceiling_is_bound_and_all_bound_passes(self):
        nat = _nat([(10, (0, 3079, 1229)), (10, (0, 2081, 1229))])
        sampled, truth = _truth_for(nat, [(900.0, 3079.0), (300.0, 1200.0)])
        rep = dc.gate(nat, sampled, truth, expected_frames=20)
        self.assertEqual([r["max_bound"] for r in rep["truth"]["shots"]], [True, True])
        self.assertEqual((rep["truth"]["avg"]["n"], rep["truth"]["max"]["n"]), (0, 0))
        self.assertTrue(dc.verdict(rep)["pass"])        # nothing measurable, nothing wrong

    def test_format_failures(self):
        good = (5, 2900, 1500)
        holes = _nat([(20, good)])
        holes["l1"] = holes["l1"][:19] + [None]
        cases = [
            ("l1_max_le_source", _nat([(10, good), (10, (5, 3400, 1800))])),
            ("l6_max_cll", _nat([(20, good)], l6=(1000, 1, 2053, 1709))),
            ("l6_max_cll", _nat([(20, good)], l6=None)),                 # a missing L6 fails too
            ("l1_min_avg_max", _nat([(10, good), (10, (1700, 2900, 1600))])),
            ("l1_every_frame", holes),
        ]
        truth = {f: {"mean": 1500.0, "p9999": 2900.0, "min": 0.0, "std": 1.0} for f in (2, 7)}
        for check, nat in cases:
            v = dc.verdict(dc.gate(nat, [(0, 10, [2, 7])], truth, expected_frames=20))
            self.assertTrue(v["checks"]["median_davg"] and v["checks"]["median_dmax"], check)
            self.assertFalse(v["pass"], check)
            self.assertFalse(v["checks"][check], (check, v))

    def test_length_mismatch_is_an_error_not_a_truncation(self):
        nat = self._accurate()
        sampled, truth = _truth_for(nat, self.TRUTH)
        with self.assertRaises(ValueError):
            dc.gate(nat, sampled, truth, expected_frames=nat["frames"] + 1)

    def test_l4_is_compared_to_truth_informationally(self):
        nat = _nat([(10, (5, 2900, 1500))], l4=[(1510, 95)] * 10)
        sampled, truth = _truth_for(nat, [(1500.0, 2900.0)])
        rep = dc.gate(nat, sampled, truth, expected_frames=10)
        self.assertEqual(rep["truth"]["l4"]["anchor_pq"]["median"], 10)
        self.assertEqual(rep["truth"]["l4"]["anchor_power"]["bias"], -5)


class ResolveIsInformational(unittest.TestCase):
    def _rep(self):
        nat = _nat([(10, (5, 2900, 1434)), (10, (5, 3000, 1600))])
        sampled, truth = _truth_for(nat, [(1434.0, 2900.0), (1600.0, 3000.0)])
        return nat, truth, dc.gate(nat, sampled, truth, expected_frames=20)

    def test_resolve_shaped_input_never_changes_the_verdict(self):
        nat, truth, rep = self._rep()
        before = dc.verdict(rep)
        # Resolve: a refresh (and its own L1) on every frame, avg pinned at 2048, L3 carrying
        # the adjustment, L4 on every frame
        l1 = [(0, 3079, 2048)] * 20
        l3 = [(2048, 2048, 1434)] * 20
        res = {"frames": 20, "l1": l1, "l3": l3, "v4": [dc.v4_view(a, b) for a, b in zip(l1, l3)],
               "l4": [(1500, 400)] * 20, "l6": [(1000, 1, 994, 553)], "source_max_pq": [3079]}
        rep["resolve"] = dc.resolve_info(nat, res, truth)
        self.assertEqual(dc.verdict(rep), before)
        self.assertEqual(rep["resolve"]["l3_frames"], 20)
        self.assertEqual(rep["resolve"]["v29"]["avg"]["worst"], 614)      # 1434 against ~2048
        self.assertEqual(rep["resolve"]["v4"]["avg"]["median"], 83)       # (0 + 166) / 2: L1 + L3
        self.assertIsNone(rep["resolve"]["l4"])                           # native has no L4 here
        self.assertIn("v4_avg", rep["resolve"]["vs_truth"])

    def test_an_unreadable_resolve_rpu_is_an_entry_not_an_aborted_gate(self):
        nat, truth, rep = self._rep()
        before = dc.verdict(rep)
        rep["resolve"] = dc.resolve_block(nat, "/nonexistent/resolve.bin", truth)
        self.assertIn("error", rep["resolve"])
        self.assertEqual(dc.verdict(rep), before)

    def test_length_mismatch_is_reported_not_truncated(self):
        nat, truth, rep = self._rep()
        res = {"frames": 25, "l1": [(0, 3079, 2048)] * 25, "l3": [None] * 25, "v4": None, "l4": None}
        info = dc.resolve_info(nat, res, truth)
        self.assertIn("error", info)
        rep["resolve"] = info
        self.assertTrue(dc.verdict(rep)["pass"])


class JsonStream(unittest.TestCase):
    def _parse(self, text, chunk):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "a.json")
            with open(p, "w") as f:
                f.write(text)
            with open(p) as f:
                return list(dc._iter_json_array(f, chunk=chunk))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_elements_across_tiny_chunks(self):
        items = [{"vdr_dm_data": {"i": i, "s": "x" * i}} for i in range(40)]
        for text in (json.dumps(items), json.dumps(items, indent=2)):
            for chunk in (3, 7, 64, 1 << 20):
                self.assertEqual(self._parse(text, chunk), items)
        self.assertEqual(self._parse(" [ ] ", 2), [])

    def test_truncated_or_not_an_array(self):
        with self.assertRaises(ValueError):
            self._parse('[{"a": 1}, {"b": ', 4)
        with self.assertRaises(ValueError):
            self._parse('{"a": 1}', 4)


class TruthDecodeCommand(unittest.TestCase):
    def test_frame_exact_seek_and_select(self):
        cmd = dc.build_truth_decode_command("ff", "m.mp4", 100, [0, 8, 16], "24000/1001")
        self.assertEqual(cmd[cmd.index("-ss") + 1], "%.6f" % dvcap.seg_seek_seconds(100, "24000/1001"))
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))                       # input seek
        self.assertEqual(cmd[cmd.index("-vf") + 1], "select='eq(n,0)+eq(n,8)+eq(n,16)'")
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "3")
        self.assertEqual(cmd[-5:], ["-pix_fmt", "yuv444p10le", "-f", "rawvideo", "-"])

    def test_frame_zero_has_no_seek(self):
        self.assertNotIn("-ss", dc.build_truth_decode_command("ff", "m.mp4", 0, [0], "24000/1001"))


@unittest.skipUnless(HAVE_DOVI, "dovi_tool not installed")
class RealDoviRoundTrip(unittest.TestCase):
    """Generate a native RPU with dvnative, read it back through rpu_frames: the per-frame L1
    must be exactly what `deliverable` predicts, i.e. the gate's clamp model matches the
    installed dovi_tool."""

    def test_generate_then_read_back(self):
        scenes = [(0, 2, 40, 3400, 700), (3, 4, 5, 2000, 1500), (5, 6, 0, 2500, 2600)]
        sc = {"version": 5, "scenes": [{"start": a, "end": b, "min_pq_12bit": mn, "max_pq_12bit": mx,
                                        "avg_max_rgb_pq_12bit": avg} for a, b, mn, mx, avg in scenes],
              "light_level": {"max_cll_nits": 2053, "max_fall_nits": 1709},
              "l4": [[900 + i, 400 + i] for i in range(7)]}
        with tempfile.TemporaryDirectory() as d:
            rpu = os.path.join(d, "n.bin")
            dvnative.generate_rpu(sc, rpu)
            fr = dc.rpu_frames(rpu)
        want = []
        for a, b, mn, mx, avg in scenes:
            want += [tuple(int(v) for v in dc.deliverable(mn, mx, avg))] * (b - a + 1)
        self.assertEqual(fr["frames"], 7)
        self.assertEqual(fr["l1"], want)
        self.assertEqual(fr["l3"], [None] * 7)
        self.assertIs(fr["v4"], fr["l1"])                     # no L3: the two views are one
        self.assertEqual(fr["l4"], [(900 + i, 400 + i) for i in range(7)])
        self.assertEqual(fr["l6"], [(1000, 1, 1000, 1000)])   # MaxCLL 2053 shipped as 1000
        self.assertEqual(fr["source_max_pq"], [3079])
        self.assertEqual(dc.shots_from_l1(fr["l1"]), [(0, 3), (3, 5), (5, 7)])

    def test_dovi_tool_failure_keeps_its_reason(self):
        with self.assertRaises(RuntimeError) as cm:
            dc.rpu_frames("/nonexistent/x.bin")
        self.assertIn("No such file", str(cm.exception))


def _make_clip(path, lum_expr, frames):
    """A tiny lossless HEVC clip (keyint 24, so seeks cross GOPs and B-frames) whose luma
    follows `lum_expr` (geq; N = frame number)."""
    subprocess.run([dvcap.FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=black:s=64x64:r=24000/1001", "-frames:v", str(frames),
                    "-vf", f"format=yuv420p10le,geq=lum={lum_expr}:cb=512:cr=512",
                    "-c:v", "libx265", "-x265-params", "lossless=1:keyint=24:min-keyint=24:log-level=error",
                    "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
                    path], check=True, capture_output=True)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class TruthDecodeIsFrameExact(unittest.TestCase):
    def test_each_sampled_frame_is_the_frame_asked_for(self):
        with tempfile.TemporaryDirectory() as d:
            clip = os.path.join(d, "idx.mp4")
            _make_clip(clip, "64+8*N", 100)                    # frame N has luma 64 + 8N
            pm = dc.probe_master(clip)
            self.assertEqual((pm["frames"], pm["width"]), (100, 64))
            runs = [(0, [0, 5]), (23, [0, 1, 2]), (37, [0, 11, 40]), (97, [0, 2])]
            got = dc.pixel_truth(clip, runs, fps_str=pm["fps"], width=64, height=64)
        for first, offs in runs:
            for o in offs:
                self.assertAlmostEqual(got[first + o]["mean"], _code(64 + 8 * (first + o)), delta=0.01,
                                       msg=f"frame {first + o}")

    def test_a_run_past_the_end_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            clip = os.path.join(d, "idx.mp4")
            _make_clip(clip, "64+8*N", 30)
            with self.assertRaises(RuntimeError):
                dc.pixel_truth(clip, [(25, [0, 10])], fps_str="24000/1001", width=64, height=64)


@unittest.skipUnless(HAVE_FFMPEG and HAVE_DOVI, "ffmpeg/dovi_tool not installed")
class GateEndToEnd(unittest.TestCase):
    """A 2-shot PQ clip (flat luma 500, then 700) and a sidecar that describes it."""

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        cls.clip = os.path.join(cls.d, "two.mp4")
        _make_clip(cls.clip, "if(lt(N\\,48)\\,500\\,700)", 96)
        cls.a, cls.b = int(_code(500)), int(_code(700))       # 2038, 2973

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, ignore_errors=True)

    def _sidecar(self, avg_b=None, end=95):
        sc = {"version": 5, "scenes": [
            {"start": 0, "end": 47, "min_pq_12bit": self.a, "max_pq_12bit": self.a, "avg_max_rgb_pq_12bit": self.a},
            {"start": 48, "end": end, "min_pq_12bit": self.b, "max_pq_12bit": self.b,
             "avg_max_rgb_pq_12bit": avg_b or self.b}],
            "light_level": {"max_cll_nits": 900, "max_fall_nits": 400}}
        p = os.path.join(self.d, f"s_{avg_b}_{end}.l1.json")
        with open(p, "w") as f:
            json.dump(sc, f)
        return p

    def test_a_true_sidecar_passes_in_both_modes(self):
        rep = dc.run(master=self.clip, native_sidecar=self._sidecar())
        self.assertTrue(rep["verdict"]["pass"], rep["verdict"])
        self.assertEqual((rep["native"]["shots"], rep["native"]["scenes"], rep["truth"]["frames_sampled"]), (2, 2, 48))
        self.assertLess(rep["truth"]["avg"]["median"], 1)
        rpu = os.path.join(self.d, "native.bin")
        dvnative.generate_rpu(self._sidecar(), rpu)
        rep2 = dc.run(master=self.clip, native_rpu=rpu, resolve_rpu=rpu)
        self.assertTrue(rep2["verdict"]["pass"])
        self.assertEqual(rep2["truth"]["avg"], rep["truth"]["avg"])         # one code path
        self.assertEqual(rep2["resolve"]["v29"]["avg"]["worst"], 0)

    def test_a_wrong_avg_fails(self):
        rep = dc.run(master=self.clip, native_sidecar=self._sidecar(avg_b=2700))
        self.assertFalse(rep["verdict"]["pass"])
        self.assertFalse(rep["verdict"]["checks"]["median_davg"])

    def test_frames_flag_must_agree_with_the_master(self):
        with self.assertRaises(ValueError):
            dc.run(master=self.clip, native_sidecar=self._sidecar(end=90), expected_frames=91)

    def test_scenes_with_identical_delivered_l1_stay_separate_shots(self):
        # two scenes whose L1 clamps to the same delivered values must still be two shots
        sc = {"version": 5, "scenes": [
            {"start": 0, "end": 47, "min_pq_12bit": self.b, "max_pq_12bit": 3500, "avg_max_rgb_pq_12bit": self.b},
            {"start": 48, "end": 95, "min_pq_12bit": self.b, "max_pq_12bit": 3600, "avg_max_rgb_pq_12bit": self.b}]}
        p = os.path.join(self.d, "clamped.l1.json")
        with open(p, "w") as f:
            json.dump(sc, f)
        rep = dc.run(master=self.clip, native_sidecar=p)
        self.assertEqual((rep["native"]["scenes"], rep["native"]["shots"]), (2, 2))

    def test_a_short_rpu_is_an_error_and_the_cli_exits_2(self):
        short = self._sidecar(end=90)
        with self.assertRaises(ValueError):
            dc.run(master=self.clip, native_sidecar=short)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(dc.main(["--native-sidecar", short, "--master", self.clip]), 2)
            self.assertEqual(dc.main(["--native-sidecar", self._sidecar(), "--master", self.clip, "--json"]), 0)
            self.assertEqual(dc.main(["--native-sidecar", self._sidecar(avg_b=2700), "--master", self.clip]), 1)
        self.assertIn("could not compare", err.getvalue())
        self.assertIn('"verdict"', out.getvalue())
        self.assertIn("FAIL", out.getvalue())


if __name__ == "__main__":
    unittest.main()
