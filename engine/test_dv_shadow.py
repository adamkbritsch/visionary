"""tools/dv_shadow.py: the shadow sample behind gates Q1 (picture), Q3 (format) and the Q4 blind
A/B clips. Pure helpers are tested without media; two small real-tool tests (an identity-cube
colour check and a 24-frame DV clip through encode -> mux -> Q3 -> blind) skip when the
Homebrew tools are absent."""
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import dvcap
import dvnative


def _load_shadow():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "dv_shadow.py")
    spec = importlib.util.spec_from_file_location("dv_shadow", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ds = _load_shadow()

# Real ffmpeg 8.0 stderr (lavfi testsrc2 pair, 10-bit), one run per filter.
VMAF_ERR = ("[Parsed_libvmaf_4 @ 0xc0f406a00] VMAF score: 98.407147\n"
            "[out#0/null @ 0xc0f405c80] video:10KiB audio:0KiB subtitle:0KiB other streams:0KiB "
            "global headers:0KiB muxing overhead: unknown\n")
SSIM_ERR = ("[Parsed_ssim_4 @ 0x801451200] SSIM Y:0.814289 (7.311628) U:0.860388 (8.550764) "
            "V:0.867149 (8.766362) All:0.830782 (7.715542)\n")
XPSNR_ERR = ("[Parsed_xpsnr_4 @ 0x79f039ec0] XPSNR  y: 32.3663  u: 32.4726  v: 32.4434  "
             "(minimum: 32.3663)\n")
# The same filters on identical inputs.
SSIM_SAME = ("[Parsed_ssim_4 @ 0xbd1405200] SSIM Y:1.000000 (inf) U:1.000000 (inf) V:1.000000 (inf) "
             "All:1.000000 (inf)\n")
XPSNR_SAME = "[Parsed_xpsnr_4 @ 0xb5b449200] XPSNR  y: inf  u: inf  v: inf  (minimum: inf)\n"


def _good_probe():
    return {"streams": [{"codec_tag_string": "hvc1", "color_transfer": "smpte2084",
                         "color_primaries": "bt2020", "side_data_list": [
                             {"side_data_type": "DOVI configuration record", "dv_profile": 8,
                              "dv_bl_signal_compatibility_id": 1},
                             {"side_data_type": "Mastering display metadata"}]}]}


def _good_rpu0():
    return {"dovi_profile": 8, "vdr_dm_data": {
        "cmv29_metadata": {"num_ext_blocks": 2, "ext_metadata_blocks": [
            {"Level1": {"min_pq": 7, "max_pq": 3079, "avg_pq": 1500}},
            {"Level6": {"max_display_mastering_luminance": 1000,
                        "min_display_mastering_luminance": 1,
                        "max_content_light_level": 1000, "max_frame_average_light_level": 400}}]},
        "cmv40_metadata": {"num_ext_blocks": 1, "ext_metadata_blocks": [
            {"Level254": {"dm_mode": 0, "dm_version_index": 2}}]}}}


def _inputs(**over):
    kw = dict(lut_sha256="a" * 64, vf=dvnative.lut_vf("/l/rcm.cube"),
              chunk={"path": "/c/seg_0008.mov", "size": 9, "mtime_ns": 1},
              first=20098, n=2160, render={"path": "/r/R.mov", "size": 7, "mtime_ns": 2},
              resolve_rpu_sha256="b" * 64, cap=50, audio=None, cube="/l/rcm.cube")
    kw.update(over)
    return ds.inputs_manifest(**kw)


class ColourChain(unittest.TestCase):
    """confirmed[6]: the shadow must use dvnative's fixed chain, not its old double conversion."""

    def test_no_local_lut_chain(self):
        self.assertFalse(hasattr(ds, "lut_vf"))

    def test_reference_is_the_native_encode_chain(self):
        g = ds.metric_graph("ssim", "/l/rcm.cube")
        self.assertIn("[1:v]" + dvnative.lut_vf("/l/rcm.cube") + ",setpts=PTS-STARTPTS[r]", g)
        self.assertIn("transferin=smpte2084:primariesin=2020", g)
        self.assertTrue(g.endswith("[d][r]ssim"))

    def test_metric_options_appended(self):
        g = ds.metric_graph("libvmaf", "/l/rcm.cube", "=model=version=vmaf_4k_v0.6.1:n_threads=8")
        self.assertTrue(g.endswith("[d][r]libvmaf=model=version=vmaf_4k_v0.6.1:n_threads=8"))

    def test_unsafe_lut_path_refused_before_any_work(self):
        with self.assertRaises(SystemExit):
            ds.main(["--chunk", "c.mov", "--first", "0", "--render", "r.mov", "--resolve-rpu",
                     "r.rpu", "--lut", "/a b/rcm.cube", "--out", "/nonexistent/never"])
        self.assertFalse(os.path.exists("/nonexistent/never"))


@unittest.skipUnless(os.path.exists(dvcap.FFMPEG), "needs ffmpeg")
class IdentityCubePassThrough(unittest.TestCase):
    """An identity cube through the reference chain must leave luma alone (the old chain took
    white 943 -> 509 and grey 505 -> 374; this one gives 940 and 505)."""

    def test_neutral_patches_keep_luma(self):
        d = tempfile.mkdtemp()
        try:
            cube = os.path.join(d, "identity.cube")
            with open(cube, "w") as f:
                f.write("LUT_3D_SIZE 2\n")
                for b in (0, 1):
                    for g in (0, 1):
                        for r in (0, 1):
                            f.write(f"{r}.0 {g}.0 {b}.0\n")
            for colour in ("0xE0E0E0", "0x808080", "0x202020"):    # legal range: lut3d clamps super-white
                src = (f"color=c={colour}:s=64x64:r=24:d=1,format=yuv422p10le,"
                       "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv")
                ys = []
                for vf in ("format=yuv420p10le", dvnative.lut_vf(cube)):
                    r = subprocess.run([dvcap.FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                                        "-i", src, "-vf", vf, "-frames:v", "1", "-f", "rawvideo",
                                        "-pix_fmt", "yuv420p10le", "-"], capture_output=True)
                    self.assertEqual(r.returncode, 0, r.stderr[-300:])
                    y = r.stdout[:64 * 64 * 2]
                    ys.append(sum(int.from_bytes(y[i:i + 2], "little") for i in range(0, len(y), 2))
                              / (64 * 64))
                self.assertLessEqual(abs(ys[0] - ys[1]), 3, (colour, ys))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class Metrics(unittest.TestCase):
    """The regexes against real ffmpeg 8.0 output."""

    def test_real_output(self):
        m = ds.parse_metrics(VMAF_ERR, SSIM_ERR, XPSNR_ERR)
        self.assertEqual(m, {"vmaf": 98.407147, "ssim": 0.830782, "xpsnr_y": 32.3663})

    def test_identical_frames_read_as_perfect_not_missing(self):
        m = ds.parse_metrics("[Parsed_libvmaf_0 @ 0x1] VMAF score: 100.000000\n", SSIM_SAME, XPSNR_SAME)
        self.assertEqual(m["ssim"], 1.0)                 # the All value, not its "(inf)" dB
        self.assertEqual(m["xpsnr_y"], float("inf"))
        self.assertEqual(m["vmaf"], 100.0)

    def test_failed_run_is_none(self):
        self.assertEqual(ds.parse_metrics("", "Error opening input", None),
                         {"vmaf": None, "ssim": None, "xpsnr_y": None})

    def test_ssim_all_only_from_the_ssim_line(self):
        self.assertIsNone(ds.parse_metrics("", "Stream mapping: All: 3\n", "")["ssim"])


class Q1Verdict(unittest.TestCase):
    N = {"vmaf": 97.0, "ssim": 0.99, "xpsnr_y": 45.0}

    def test_native_at_least_resolve_passes(self):
        self.assertTrue(ds.q1_verdict(self.N, dict(self.N)))                  # tie = no downgrade
        self.assertTrue(ds.q1_verdict(self.N, {"vmaf": 96.0, "ssim": 0.98, "xpsnr_y": 44.0}))

    def test_any_metric_lower_fails(self):
        for k in ds.Q1_METRICS:
            self.assertFalse(ds.q1_verdict(dict(self.N, **{k: self.N[k] - 0.001}), self.N), k)

    def test_missing_metric_fails(self):
        self.assertFalse(ds.q1_verdict(dict(self.N, ssim=None), self.N))
        self.assertFalse(ds.q1_verdict(self.N, dict(self.N, vmaf=None)))

    def test_infinite_xpsnr_compares(self):
        self.assertTrue(ds.q1_verdict(dict(self.N, xpsnr_y=float("inf")), self.N))
        self.assertFalse(ds.q1_verdict(self.N, dict(self.N, xpsnr_y=float("inf"))))


class Manifest(unittest.TestCase):
    """confirmed[7]: reuse only under a manifest that matches every input."""

    def test_round_trip_matches(self):
        d = tempfile.mkdtemp()
        try:
            inp = _inputs()
            ds.save_manifest(d, {"inputs": inp, "built": {"native.hevc": "x"}})
            self.assertTrue(ds.manifest_matches(ds.load_manifest(d), _inputs()))
            self.assertFalse(os.path.exists(os.path.join(d, "manifest.json.part")))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_every_input_invalidates(self):
        old = {"inputs": _inputs(), "built": {}}
        changes = dict(lut_sha256="c" * 64, vf="other", first=20099, n=2159, cap=40,
                       resolve_rpu_sha256="d" * 64, cube="/l/other.cube",
                       chunk={"path": "/c/seg_0008.mov", "size": 9, "mtime_ns": 3},
                       render={"path": "/r/R2.mov", "size": 7, "mtime_ns": 2},
                       audio={"path": "/a/a.mp4", "size": 1, "mtime_ns": 1})
        for k, v in changes.items():
            self.assertFalse(ds.manifest_matches(old, _inputs(**{k: v})), k)

    def test_dvnative_settings_are_recorded(self):
        a = _inputs()["analysis"]
        self.assertEqual(a["vf"], dvnative.analysis_vf("/l/rcm.cube"))
        self.assertEqual((a["l1_max_pq"], a["analyzer_commit"], a["with_l4"], a["peak_estimator"]),
                         (3079, dvnative.HDR_ANALYZER_COMMIT, True, "max"))

    def test_missing_or_broken_manifest_never_matches(self):
        self.assertFalse(ds.manifest_matches(None, _inputs()))
        self.assertFalse(ds.manifest_matches({"built": {}}, _inputs()))
        self.assertFalse(ds.manifest_matches([], _inputs()))
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "manifest.json"), "w") as f:
                f.write("{trunc")
            self.assertIsNone(ds.load_manifest(d))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_built_stamps_do_not_affect_the_match(self):
        self.assertTrue(ds.manifest_matches({"inputs": _inputs(), "built": {"a": 1}}, _inputs()))

    def test_hevc_reuse_needs_stamp_and_every_frame(self):
        built = {"native.hevc": "s1"}
        self.assertTrue(ds.hevc_reusable(built, "native.hevc", "s1", 2160, 2160))
        self.assertFalse(ds.hevc_reusable(built, "native.hevc", "s1", 2159, 2160))   # truncated
        self.assertFalse(ds.hevc_reusable(built, "native.hevc", "s2", 2160, 2160))   # new RPU
        self.assertFalse(ds.hevc_reusable(built, "resolve.hevc", "s1", 2160, 2160))  # never built
        self.assertFalse(ds.hevc_reusable({}, "native.hevc", "", 0, 0))

    def test_reset_removes_only_derived_outputs(self):
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "analysis"))
            os.makedirs(os.path.join(d, "ab"))
            mine = ["native.hevc", "native.hevc.part", "resolve.mp4", "native.rpu",
                    "native.rpu.generate.json", "resolve_slice.rpu.part", "report.json",
                    "ab_key.json", "ab/A.mp4", "ab/B.mp4.part",
                    "analysis/chunk_0000.bin.l1.json", "analysis/chunk_0000.l4.json"]
            theirs = ["keep.txt", "analysis/notes.txt", "ab/readme.txt", "manifest.json"]
            for rel in mine + theirs:
                with open(os.path.join(d, rel), "w") as f:
                    f.write("x")
            ds.reset_out(d)
            for rel in mine:
                self.assertFalse(os.path.exists(os.path.join(d, rel)), rel)
            for rel in theirs:
                self.assertTrue(os.path.exists(os.path.join(d, rel)), rel)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class AtomicWrites(unittest.TestCase):
    """A failed or short encode/mux must leave neither the final file nor its .part."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.out = os.path.join(self.d, "native.hevc")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _pipe(self, got, why="ok"):
        def fake(dec, enc, out_hevc, abort, on_frame):
            self.assertTrue(out_hevc.endswith(".part"))
            self.assertIn(out_hevc, enc)                  # x265 writes the .part, never the final
            with open(out_hevc, "wb") as f:
                f.write(b"\0" * 10)
            return got, why, []
        return fake

    def _encode(self, got, why="ok"):
        with mock.patch.object(dvcap, "_encode_pipe", side_effect=self._pipe(got, why)):
            return ds.encode(["ffmpeg"], "/r.rpu", self.out, 50, None, None, 24)

    def test_exact_count_publishes(self):
        self.assertEqual(self._encode(24), 24)
        self.assertTrue(os.path.exists(self.out))
        self.assertFalse(os.path.exists(self.out + ".part"))

    def test_short_encode_is_refused(self):
        with self.assertRaises(RuntimeError):
            self._encode(23)
        self.assertFalse(os.path.exists(self.out))
        self.assertFalse(os.path.exists(self.out + ".part"))

    def test_failed_encode_leaves_nothing(self):
        with self.assertRaises(RuntimeError):
            self._encode(None, "x265 rc=1")
        self.assertEqual(os.listdir(self.d), [])

    def test_stale_part_is_cleared_first(self):
        with open(self.out + ".part", "wb") as f:
            f.write(b"old")
        seen = {}

        def fake(dec, enc, out_hevc, abort, on_frame):
            seen["stale"] = os.path.exists(out_hevc)
            return None, "x265 rc=1", []
        with mock.patch.object(dvcap, "_encode_pipe", side_effect=fake):
            with self.assertRaises(RuntimeError):
                ds.encode(["ffmpeg"], "/r.rpu", self.out, 50, None, None, 24)
        self.assertFalse(seen["stale"])

    def test_failed_mux_leaves_nothing(self):
        mp4 = os.path.join(self.d, "native.mp4")

        def fake(cmd, **kw):
            self.assertEqual(cmd[-2:], ["-new", mp4 + ".part"])
            with open(mp4 + ".part", "wb") as f:
                f.write(b"partial")
            raise subprocess.CalledProcessError(1, cmd)
        with mock.patch.object(ds.subprocess, "run", side_effect=fake):
            with self.assertRaises(subprocess.CalledProcessError):
                ds.mux("/x/native.hevc", mp4, None)
        self.assertEqual(os.listdir(self.d), [])


class Q3Format(unittest.TestCase):
    """confirmed[8]: the native clip's format, from ffprobe JSON and its RPU's frame 0."""

    def _q3(self, probe=None, rpu0=None, frames=24, n=24, peak=40.0, cap=50):
        return ds.q3_evaluate(probe if probe is not None else _good_probe(),
                              rpu0 if rpu0 is not None else _good_rpu0(), frames, n, peak, cap)

    def test_good_clip_passes(self):
        q = self._q3()
        self.assertTrue(q["pass"], q)
        self.assertEqual(set(q) - {"pass"}, {"hvc1", "transfer", "primaries", "dv_profile",
                                              "dv_compat_id", "rpu_frames", "rpu_profile",
                                              "cm_v40", "l6_max_nits", "peak"})

    def test_each_stream_field_fails(self):
        for k, v in (("codec_tag_string", "hev1"), ("color_transfer", "bt709"),
                     ("color_primaries", "bt709")):
            p = _good_probe()
            p["streams"][0][k] = v
            self.assertFalse(self._q3(probe=p)["pass"], k)

    def test_dovi_record(self):
        for k, v in (("dv_profile", 5), ("dv_bl_signal_compatibility_id", 0)):
            p = _good_probe()
            p["streams"][0]["side_data_list"][0][k] = v
            self.assertFalse(self._q3(probe=p)["pass"], k)
        p = _good_probe()
        del p["streams"][0]["side_data_list"][0]
        q = self._q3(probe=p)
        self.assertFalse(q["dv_profile"]["ok"])
        self.assertEqual(ds.dovi_record({}), {})

    def test_rpu_checks(self):
        r = _good_rpu0()
        r["vdr_dm_data"]["cmv40_metadata"] = None                 # a CM v2.9 RPU
        self.assertFalse(self._q3(rpu0=r)["cm_v40"]["ok"])
        r = _good_rpu0()
        r["vdr_dm_data"]["cmv29_metadata"]["ext_metadata_blocks"][1]["Level6"][
            "max_display_mastering_luminance"] = 4000
        self.assertFalse(self._q3(rpu0=r)["l6_max_nits"]["ok"])
        r = _good_rpu0()
        r["dovi_profile"] = 7
        self.assertFalse(self._q3(rpu0=r)["pass"])
        self.assertFalse(self._q3(frames=23)["rpu_frames"]["ok"])

    def test_no_rpu_in_the_clip_fails(self):
        q = ds.q3_evaluate(_good_probe(), None, 0, 24, 40.0, 50)
        self.assertFalse(q["pass"])
        self.assertFalse(q["cm_v40"]["ok"])

    def test_peak_uses_dvcap_tolerance(self):
        self.assertTrue(self._q3(peak=50 * dvcap.PEAK_TOLERANCE)["peak"]["ok"])
        self.assertFalse(self._q3(peak=50 * dvcap.PEAK_TOLERANCE + 0.01)["pass"])
        self.assertFalse(self._q3(peak=0.0)["pass"])               # unmeasured is not a pass

    def test_rpu_json_parse(self):
        self.assertEqual(ds.parse_rpu_frame_json('Parsing RPU file...\n{"dovi_profile": 8}\n'),
                         {"dovi_profile": 8})
        self.assertIsNone(ds.parse_rpu_frame_json("Parsing RPU file...\nError: no RPU"))
        self.assertIsNone(ds.parse_rpu_frame_json("x {trunc"))
        self.assertIsNone(ds.parse_rpu_frame_json(None))


class Blind(unittest.TestCase):
    """The A/B clips must not reveal which path made them."""

    def test_assignment_is_a_fair_coin_over_one_byte(self):
        a = [ds.blind_assignment(b) for b in range(256)]
        for k in a:
            self.assertEqual(sorted(k), ["A", "B"])
            self.assertEqual(sorted(k.values()), ["native", "resolve"])
        self.assertEqual(sum(k["A"] == "native" for k in a), 128)
        self.assertEqual(ds.blind_assignment(0), {"A": "native", "B": "resolve"})
        self.assertEqual(ds.blind_assignment(1), {"A": "resolve", "B": "native"})

    def test_randomness_comes_from_urandom(self):
        self.assertIs(inspect.signature(ds.make_blind).parameters["rand"].default, os.urandom)

    def test_label_leaks(self):
        clean = {"format": {"filename": "/s/_native/ab/A.mp4", "tags": {"major_brand": "iso4"}},
                 "streams": [{"tags": {"handler_name": "Video"}}]}
        self.assertEqual(ds.label_leaks(clean), [])
        leaky = {"format": {"filename": "x", "tags": {"title": "Resolve render"}},
                 "streams": [{"tags": {"handler_name": "native_video"}}, {"codec_type": "audio"}]}
        self.assertEqual(ds.label_leaks(leaky), ["title=Resolve render", "handler_name=native_video"])
        self.assertEqual(ds.label_leaks({}), [])

    def test_neutralize_command(self):
        cmd = ds.build_neutralize_command("MP4Box", "ab/A.mp4.part", [
            {"id": "0x1", "codec_type": "video"}, {"id": "0x2", "codec_type": "audio"}, {"id": None}])
        t = ds.BLIND_TIME
        self.assertEqual(cmd, ["MP4Box", "-time", t,
                               "-time", f"1={t}", "-mtime", f"1={t}", "-name", "1=Video", "-udta", "1:type=name",
                               "-time", f"2={t}", "-mtime", f"2={t}", "-name", "2=Audio", "-udta", "2:type=name",
                               "-itags", "clear", "ab/A.mp4.part"])

    def test_report_is_strict_json(self):
        rep = {"native": {"xpsnr_y": float("inf"), "ssim": 1.0}, "l": [float("-inf"), 2], "n": float("nan")}
        text = json.dumps(ds.json_safe(rep), allow_nan=False)            # raises on Infinity/NaN
        self.assertIn('"inf"', text)
        self.assertEqual(ds.json_safe(rep)["native"]["ssim"], 1.0)


class Alignment(unittest.TestCase):
    def test_best_shift_prefers_zero_on_a_tie(self):
        self.assertEqual(ds.best_shift({-1: 30.0, 0: 45.0, 1: 31.0}), 0)
        self.assertEqual(ds.best_shift({-1: 30.0, 0: 31.0, 1: 45.0}), 1)
        self.assertEqual(ds.best_shift({-1: 40.0, 0: 40.0, 1: 40.0}), 0)

    def test_graph_shifts_the_right_input_and_bounds_both(self):
        g = ds.alignment_graph("/l/x.cube", 1, 48)
        self.assertIn("[0:v]trim=start_frame=0:end_frame=48", g)
        self.assertIn("[1:v]trim=start_frame=1:end_frame=49", g)
        self.assertTrue(g.endswith("[d][r]psnr"))
        g = ds.alignment_graph("/l/x.cube", -1, 48)
        self.assertIn("[0:v]trim=start_frame=1:end_frame=49", g)

    def test_psnr_regex_reads_real_ffmpeg_output(self):
        line = "[Parsed_psnr_6 @ 0x9f8c0] PSNR y:41.237201 u:44.1 v:43.9 average:42.0 min:39.1 max:45.2\n"
        self.assertAlmostEqual(ds._num(ds._PSNR_Y_RE, line), 41.237201)
        self.assertEqual(ds._num(ds._PSNR_Y_RE, "PSNR y:inf u:inf"), float("inf"))


_TOOLS = (dvcap.FFMPEG, dvcap.FFPROBE, dvcap.X265, dvcap.DOVI_TOOL, ds.MP4BOX)


@unittest.skipUnless(all(os.path.exists(t) for t in _TOOLS), "needs ffmpeg, x265, dovi_tool, MP4Box")
class RealClip(unittest.TestCase):
    """24 frames through the shadow's own encode -> mux -> Q3 -> blind, with real tools."""

    def test_end_to_end(self):
        d = tempfile.mkdtemp()
        try:
            sc = {"version": 5, "source": {"stream_frames": 24, "leading_skipped_frames": 0},
                  "scenes": [{"start": 0, "end": 11, "min_pq_12bit": 7, "max_pq_12bit": 3500,
                              "avg_max_rgb_pq_12bit": 1500},
                             {"start": 12, "end": 23, "min_pq_12bit": 7, "max_pq_12bit": 2900,
                              "avg_max_rgb_pq_12bit": 1300}],
                  "light_level": {"max_cll_nits": 8000, "max_fall_nits": 400}, "l4": [[1500, 300]] * 24}
            rpu = os.path.join(d, "native.rpu")
            dvnative.generate_rpu(sc, rpu)
            dec = [dvcap.FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                   "testsrc2=s=320x180:r=24000/1001:d=1.001", "-frames:v", "24",
                   "-pix_fmt", "yuv420p10le", "-strict", "-1", "-f", "yuv4mpegpipe", "-"]
            clips = {}
            for tag in ds.TAGS:
                hevc, mp4 = os.path.join(d, f"{tag}.hevc"), os.path.join(d, f"{tag}.mp4")
                self.assertEqual(ds.encode(dec, rpu, hevc, 50, None, dvnative.x265_max_cll(sc), 24), 24)
                ds.mux(hevc, mp4, None)
                clips[tag] = mp4
            hevc = os.path.join(d, "native.hevc")
            rpu0, frames = ds.clip_rpu(hevc, os.path.join(d, "native.clip.rpu"))
            peak = dvcap.video_peak_1s_mbps(clips["native"])
            q = ds.q3_evaluate(ds.probe_clip(clips["native"]), rpu0, frames, 24, peak, 50)
            self.assertTrue(q["pass"], q)
            self.assertEqual(dvcap.count_hevc_frames(clips["native"]), 24)

            res = ds.make_blind(d, clips, rand=lambda k: b"\x01")
            key_path = os.path.join(d, "ab_key.json")
            self.assertEqual(res, {"dir": os.path.join(d, "ab"), "key_file": key_path})
            with open(key_path) as f:
                self.assertEqual(json.load(f), {"A": "resolve", "B": "native"})
            self.assertEqual(sorted(os.listdir(os.path.join(d, "ab"))), ["A.mp4", "B.mp4"])
            times = set()
            for letter in ("A", "B"):
                p = ds.probe_all(os.path.join(d, "ab", f"{letter}.mp4"))
                self.assertEqual(ds.label_leaks(p), [])
                self.assertNotIn("title", p["format"].get("tags", {}))
                times.add(p["format"]["tags"].get("creation_time"))
                times.update(s["tags"].get("creation_time") for s in p["streams"])
                self.assertEqual(ds.dovi_record(ds.probe_clip(
                    os.path.join(d, "ab", f"{letter}.mp4"))).get("dv_profile"), 8)
            self.assertEqual(len(times), 1, times)        # one pinned creation time for both
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
