"""tools/route_cpu.py: the P2g measurement tool. Pure helpers (argv builders, parsers, the disk
floor, the budget) are tested without media; the probes run on tiny lavfi clips made in a temp
dir, and skip when ffmpeg, VideoToolbox, libx265 or the analyzer is absent."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import dvcap
import dvnative
import versions


def _load_route_cpu():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "route_cpu.py")
    spec = importlib.util.spec_from_file_location("route_cpu", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rc = _load_route_cpu()

CUBE = "/l/rcm.cube"

# Real ffmpeg 8.0 trace_headers output (addresses zeroed): a libx265 HDR10 clip in MP4 — the
# extradata and the first packet both carry SEI 144, 137 and 5 (user data unregistered, whose
# SIZE is long enough to need ff_bytes) — then hevc_videotoolbox's raw HEVC of it: SEI 5 only.
TRACE_X265 = """\
[trace_headers @ 0x0] Extradata
[trace_headers @ 0x0] Video Parameter Set
[trace_headers @ 0x0] Prefix Supplemental Enhancement Information
[trace_headers @ 0x0] 16          last_payload_type_byte                               10010000 = 144
[trace_headers @ 0x0] 24          last_payload_size_byte                               00000100 = 4
[trace_headers @ 0x0] Prefix Supplemental Enhancement Information
[trace_headers @ 0x0] 16          last_payload_type_byte                               10001001 = 137
[trace_headers @ 0x0] 24          last_payload_size_byte                               00011000 = 24
[trace_headers @ 0x0] Prefix Supplemental Enhancement Information
[trace_headers @ 0x0] 16          last_payload_type_byte                               00000101 = 5
[trace_headers @ 0x0] 24          ff_byte                                              11111111 = 255
[trace_headers @ 0x0] 32          ff_byte                                              11111111 = 255
[trace_headers @ 0x0] 40          last_payload_size_byte                               00010011 = 19
[trace_headers @ 0x0] Packet: 5179 bytes, key frame, pts 0, dts -2002, duration 1001.
[trace_headers @ 0x0] Prefix Supplemental Enhancement Information
[trace_headers @ 0x0] 16          last_payload_type_byte                               10010000 = 144
[trace_headers @ 0x0] 24          last_payload_size_byte                               00000100 = 4
[trace_headers @ 0x0] Prefix Supplemental Enhancement Information
[trace_headers @ 0x0] 16          last_payload_type_byte                               10001001 = 137
[trace_headers @ 0x0] 24          last_payload_size_byte                               00011000 = 24
"""
TRACE_VT_RAW = """\
[trace_headers @ 0x0] Extradata
[trace_headers @ 0x0] Video Parameter Set
[trace_headers @ 0x0] Sequence Parameter Set
[trace_headers @ 0x0] Picture Parameter Set
[trace_headers @ 0x0] Packet: 6909 bytes, key frame, no pts, dts 0, duration 48000.
[trace_headers @ 0x0] Prefix Supplemental Enhancement Information
[trace_headers @ 0x0] 16          last_payload_type_byte                               00000101 = 5
[trace_headers @ 0x0] Packet: 5207 bytes, no pts, dts 48000, duration 48000.
"""
HW_LOG = ("[vist#0:0/av1 @ 0x0] Selecting decoder 'av1' because of requested hwaccel method videotoolbox\n"
          "[graph -1 input from stream 0:0 @ 0x0] w:256 h:144 pixfmt:videotoolbox_vld tb:1/24000 "
          "fr:24000/1001 sar:1/1 csp:unknown range:tv\n")
HELP_VT = """\
hevc_videotoolbox AVOptions:
  -profile           <int>        E..V....... Profile (from -99 to INT_MAX) (default -99)
     main            1            E..V....... Main Profile
     main10          2            E..V....... Main10 Profile
  -constant_bit_rate <boolean>    E..V....... Require constant bit rate (macOS 13 or newer) (default false)
  -allow_sw          <boolean>    E..V....... Allow software encoding (default false)
"""
ENCODERS = (" V....D libx265              libx265 H.265 / HEVC (codec hevc)\n"
            " V..... libsvtav1            SVT-AV1(Scalable Video Technology for AV1) encoder (codec av1)\n")
MDCV_SD = {"side_data_type": "Mastering display metadata",
           "red_x": "34000/50000", "red_y": "16000/50000", "green_x": "13250/50000",
           "green_y": "34500/50000", "blue_x": "7500/50000", "blue_y": "3000/50000",
           "white_point_x": "15635/50000", "white_point_y": "16450/50000",
           "min_luminance": "1/10000", "max_luminance": "10000000/10000"}
CLL_SD = {"side_data_type": "Content light level metadata", "max_content": 1000, "max_average": 400}


def _quiet_main(argv):
    """main() with its stdout/stderr swallowed -> (exit status, stdout)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = rc.main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue()


class PinnedToTheEngine(unittest.TestCase):
    """Constants the tool mirrors instead of importing (stages/nativelane pull in the engine)."""

    def test_render_bitrate_is_stages(self):
        import stages
        self.assertEqual(rc.NATIVE_4K_MIN_HEIGHT, stages.YOUTUBE_NATIVE_4K_MIN_HEIGHT)
        self.assertEqual(rc.KBPS_4K, stages.YOUTUBE_RENDER_KBPS_4K)
        self.assertEqual(rc.KBPS_SUB4K, stages.YOUTUBE_RENDER_KBPS)
        for h in (None, 0, 360, 1080, 1699, 1700, 2160):
            self.assertEqual(rc.render_kbps(h), stages.youtube_render_kbps(h), h)

    def test_disk_figures_are_nativelanes(self):
        import nativelane
        self.assertEqual(rc.INTERMEDIATE_BYTES_PER_FRAME, nativelane.INTERMEDIATE_BYTES_PER_FRAME)
        self.assertEqual(rc.MIN_FREE_GB, nativelane.MID_ANALYSIS_FLOOR_GB)

    def test_defaults(self):
        self.assertEqual(rc.LUT_DEFAULT, versions.NATIVE_LUT_PATH)
        self.assertEqual(rc.FF, dvnative.FFMPEG)
        self.assertEqual(rc.FFPROBE, dvcap.FFPROBE)


class Pure(unittest.TestCase):

    def test_fourcc(self):
        self.assertEqual(rc.fourcc("av01"), 0x61763031)
        with self.assertRaises(ValueError):
            rc.fourcc("av1")

    def test_per_frame_and_rate(self):
        self.assertEqual(rc.per_frame(12.0, 24), 0.5)
        self.assertIsNone(rc.per_frame(12.0, 0))
        self.assertIsNone(rc.per_frame(None, 24))
        self.assertAlmostEqual(rc.rate("24000/1001"), 23.976, places=3)
        for bad in ("0/0", None, "x"):
            self.assertEqual(rc.rate(bad), 0.0)

    def test_parse_size_and_only(self):
        self.assertEqual(rc.parse_size("3840x2160"), (3840, 2160))
        for bad in ("3840", "3841x2160", "0x2", "axb"):
            with self.assertRaises(ValueError):
                rc.parse_size(bad)
        self.assertEqual(rc.parse_only(None), rc.PROBES)
        self.assertEqual(rc.parse_only("cbr, encode"), ("encode", "cbr"))     # report order
        with self.assertRaises(ValueError):
            rc.parse_only("encode,bogus")

    def test_clamp_start(self):
        self.assertEqual(rc.clamp_start(60, 3600.0, 600, "24000/1001"), 60.0)
        self.assertEqual(rc.clamp_start(60, 30.0, 600, "24000/1001"), 3.975)    # 30 - 25.025 - 1
        self.assertEqual(rc.clamp_start(60, 20.0, 600, "24000/1001"), 0.0)      # never fits
        self.assertEqual(rc.clamp_start(115, 120.0, 240, "24"), 109.0)          # 120 - 10 - 1
        self.assertEqual(rc.clamp_start(60, None, 600, "24"), 60.0)             # unknown: unchanged
        self.assertEqual(rc.clamp_start(-5, None, 600, "24"), 0.0)

    def test_input_args(self):
        self.assertEqual(rc.input_args("/s.mp4"), ["-i", "/s.mp4"])
        self.assertEqual(rc.input_args("/s.mp4", 1.5), ["-ss", "1.500", "-i", "/s.mp4"])
        self.assertEqual(rc.input_args("/s.mp4", 2, hwdec=True),
                         ["-hwaccel", "videotoolbox", "-ss", "2.000", "-i", "/s.mp4"])

    def test_analysis_command_is_dvnatives_with_a_range(self):
        """The intermediate is dvnative's own command: only the input and -frames:v change."""
        base = dvnative.build_analysis_intermediate_command(rc.FF, "SRC", "/w/a.mkv", CUBE, hwaccel=False)
        inp = rc.input_args("/s.mp4", 60)
        cmd = rc.build_analysis_command(inp, "/w/a.mkv", CUBE, 600)
        self.assertEqual(cmd[-1], "/w/a.mkv")
        self.assertEqual(cmd[-3:-1], ["-frames:v", "600"])
        self.assertEqual(cmd[cmd.index("-vf") + 1], dvnative.analysis_vf(CUBE))
        i = cmd.index("-ss")
        self.assertEqual(cmd[:i] + ["-i", "SRC"] + cmd[i + len(inp):-3] + cmd[-1:], base)
        self.assertNotIn("SRC", cmd)
        pre = rc.build_analysis_command(inp, "/w/a.mkv", None, 6, pre_vf="colorspace=all=bt709")
        self.assertEqual(pre[pre.index("-vf") + 1], "colorspace=all=bt709," + dvnative.analysis_vf(None))
        self.assertEqual(rc.analysis_chain(CUBE, "x=1"), "x=1," + dvnative.analysis_vf(CUBE))

    def test_encode_vf(self):
        lut = dvnative.lut_vf(CUBE, "p010le")
        self.assertEqual(rc.encode_vf(CUBE, (3840, 2160)), lut)                 # 4K: the LUT only
        vf = rc.encode_vf(CUBE, (1920, 1080))
        self.assertEqual(vf, rc.fit_pad(3840, 2160) + "," + lut)
        self.assertTrue(rc.encode_vf(CUBE, (1920, 1080), pre_vf="P").startswith("P,scale=3840:2160"))
        self.assertEqual(rc.encode_vf(None, (640, 360), (640, 360)), "format=p010le")
        self.assertNotIn("lut3d", rc.encode_vf(None, (1920, 1080)))

    def test_encode_command(self):
        cmd = rc.build_encode_command(["-i", "/s.mp4"], "/w/e.mp4", "VF", 14000, 600,
                                      extra=("-constant_bit_rate", "1"))
        self.assertEqual(cmd[0], rc.FF)
        self.assertEqual(cmd[-1], "/w/e.mp4")
        for a, b in (("-c:v", "hevc_videotoolbox"), ("-profile:v", "main10"), ("-b:v", "14000k"),
                     ("-allow_sw", "0"), ("-constant_bit_rate", "1"), ("-vf", "VF"),
                     ("-frames:v", "600"), ("-color_trc", "smpte2084"), ("-color_primaries", "bt2020")):
            self.assertEqual(cmd[cmd.index(a) + 1], b, a)
        self.assertLess(cmd.index("-i"), cmd.index("-vf"))

    def test_hwdec_check_and_verdict(self):
        cmd = rc.build_hwdec_check_command("/a.mp4")
        self.assertEqual(cmd[cmd.index("-hwaccel_output_format") + 1], "videotoolbox_vld")
        self.assertTrue(cmd[cmd.index("-vf") + 1].startswith("hwdownload,"))
        self.assertEqual(rc.hw_decode_verdict(0, HW_LOG), (True, "decoded to videotoolbox_vld frames"))
        ok, why = rc.hw_decode_verdict(1, "[av1 @ 0x0] Your platform doesn't support hardware "
                                          "accelerated AV1 decoding.\n"
                                          "[av1 @ 0x0] Failed to get pixel format.\n")
        self.assertFalse(ok)
        self.assertIn("doesn't support hardware", why)
        self.assertEqual(rc.hw_decode_verdict(234, "nothing useful\n"),
                         (False, "ffmpeg exited 234 without VideoToolbox frames"))
        self.assertFalse(rc.hw_decode_verdict(0, "software frames only\n")[0])  # rc 0 alone is not proof

    def test_parse_sei_types(self):
        self.assertEqual(rc.parse_sei_types(TRACE_X265), {"extradata": [5, 137, 144], "packets": [137, 144]})
        self.assertEqual(rc.parse_sei_types(TRACE_VT_RAW), {"extradata": [], "packets": [5]})
        multi = ("[trace_headers @ 0x0] Packet: 9 bytes\n"
                 "[trace_headers @ 0x0] 16          ff_byte                11111111 = 255\n"
                 "[trace_headers @ 0x0] 24          last_payload_type_byte 00000001 = 1\n"
                 "[trace_headers @ 0x0] 32          ff_byte                11111111 = 255\n"
                 "[trace_headers @ 0x0] 40          last_payload_size_byte 00000010 = 2\n"
                 "[trace_headers @ 0x0] 48          last_payload_type_byte 10001001 = 137\n")
        self.assertEqual(rc.parse_sei_types(multi)["packets"], [137, 256])     # size ff_bytes ignored
        self.assertEqual(rc.parse_sei_types(""), {"extradata": [], "packets": []})

    def test_hdr_facts(self):
        probe = {"frames": [{"side_data_list": [MDCV_SD, CLL_SD]}], "streams": [{"side_data_list": []}]}
        f = rc.hdr_facts(probe, rc.parse_sei_types(TRACE_X265))
        self.assertEqual(f["mdcv"], {"sei_in_packets": True, "sei_in_extradata": True, "container": False,
                                     "frame_side_data": True, "value": dvcap.FALLBACK_MASTER_DISPLAY})
        self.assertEqual(f["cll"]["value"], "1000,400")
        # container boxes only (an MP4 muxed by ffmpeg from VideoToolbox): no SEI anywhere
        probe = {"frames": [{"side_data_list": [MDCV_SD, CLL_SD]}],
                 "streams": [{"side_data_list": [MDCV_SD, CLL_SD]}]}
        f = rc.hdr_facts(probe, rc.parse_sei_types(TRACE_VT_RAW))
        self.assertTrue(f["mdcv"]["container"] and f["cll"]["container"])
        self.assertFalse(f["mdcv"]["sei_in_packets"] or f["cll"]["sei_in_extradata"])
        empty = rc.hdr_facts({}, {})
        self.assertIsNone(empty["mdcv"]["value"])
        self.assertFalse(empty["cll"]["frame_side_data"])

    def test_encoder_options_and_listing(self):
        opts = rc.encoder_options(HELP_VT)
        self.assertIn("constant_bit_rate", opts)
        self.assertIn("allow_sw", opts)
        self.assertNotIn("main10", opts)                    # a value, not an option
        self.assertTrue(rc.has_encoder("libx265", ENCODERS))
        self.assertTrue(rc.has_encoder("libsvtav1", ENCODERS))
        self.assertFalse(rc.has_encoder("libaom-av1", ENCODERS))
        self.assertFalse(rc.has_encoder("x265", ENCODERS))

    def test_source_facts(self):
        f = rc.source_facts({"streams": [{"codec_name": "av1", "width": 3840, "height": 2160,
                                          "avg_frame_rate": "0/0", "r_frame_rate": "30/1",
                                          "color_transfer": "bt709"}],
                             "format": {"duration": "612.5"}}, "/s.webm")
        self.assertEqual((f["codec"], f["width"], f["height"], f["fps"], f["duration_s"]),
                         ("av1", 3840, 2160, "30/1", 612.5))
        self.assertIsNone(rc.source_facts({}, "/x")["duration_s"])

    def test_disk_floor(self):
        need = rc.intermediate_bytes(600)
        self.assertEqual(need, 600 * rc.INTERMEDIATE_BYTES_PER_FRAME)
        self.assertTrue(rc.disk_ok(150 * rc.GiB + need, 600))
        self.assertFalse(rc.disk_ok(150 * rc.GiB + need - 1, 600))
        self.assertTrue(rc.disk_ok(need, 600, min_free_gb=0))

    def test_analysis_refuses_below_the_floor_before_running_anything(self):
        d = tempfile.mkdtemp()
        try:
            with mock.patch.object(rc, "measure") as m:
                with self.assertRaisesRegex(RuntimeError, "refused"):
                    rc.measure_analysis("/s.mp4", d, cube=None, start=0, frames=600, nice=10,
                                        analyzer=sys.executable, min_free_gb=10 ** 9)
                m.assert_not_called()
            self.assertEqual(os.listdir(d), [])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_unsafe_path(self):
        self.assertTrue(rc.unsafe_path(rc.PIPELINE_HOME))
        self.assertTrue(rc.unsafe_path(os.path.join(rc.PIPELINE_HOME, "luts", "x")))
        self.assertFalse(rc.unsafe_path(rc.PIPELINE_HOME + "-other"))
        self.assertFalse(rc.unsafe_path(tempfile.gettempdir()))

    def test_budget(self):
        b = rc.budget({"analysis": {"cpu_s_per_frame": 0.3}, "encode": {"cpu_s_per_frame": 0.2}})
        self.assertEqual((b["analysis_plus_encode"], b["within_budget"]), (0.5, True))
        self.assertEqual(b["budget_cpu_s_per_frame"], 0.66)
        b = rc.budget({"analysis": {"cpu_s_per_frame": 0.5}, "encode": {"cpu_s_per_frame": 0.2}})
        self.assertFalse(b["within_budget"])
        b = rc.budget({"analysis": {"cpu_s_per_frame": 0.3},
                       "encode": {"cpu_s_per_frame": 0.2, "synthetic": True}})
        self.assertIsNone(b["encode"])
        self.assertIsNone(b["within_budget"])
        self.assertIsNone(rc.budget({})["analysis_plus_encode"])

    def test_write_report_is_atomic(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "r.json")
            rc.write_report(path, {"a": 1})
            self.assertEqual(os.listdir(d), ["r.json"])
            with open(path) as f:
                self.assertEqual(json.load(f), {"a": 1})
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_run_probe(self):
        r = rc.run_probe(lambda: {"x": 1})
        self.assertTrue(r["ok"])
        self.assertEqual(r["x"], 1)
        self.assertIn("probe_s", r)

        def boom():
            raise RuntimeError("nope")
        r = rc.run_probe(boom)
        self.assertEqual((r["ok"], r["error"]), (False, "RuntimeError: nope"))


class MainArguments(unittest.TestCase):
    """Bad arguments exit 2 before anything is made; a mocked run reports and cleans up."""

    def test_refusals(self):
        inside = os.path.join(rc.PIPELINE_HOME, "never-route-cpu")
        for argv in (["--only", "bogus"], ["--only", "master_hdr"], ["--only", "analysis"],
                     ["--encode-size", "3841x2160"], ["--frames", "0"],
                     ["--lut", "/nonexistent/never.cube"], ["--source", "/nonexistent/never.mp4"],
                     ["--work", inside], ["--out", os.path.join(inside, "r.json")]):
            code, _out = _quiet_main(argv)
            self.assertEqual(code, 2, argv)
        self.assertFalse(os.path.exists(inside))

    def test_mocked_run(self):
        d = tempfile.mkdtemp()
        try:
            master = os.path.join(d, "m.mp4")
            open(master, "w").close()
            out = os.path.join(d, "r.json")
            with mock.patch.object(rc, "host_facts", return_value={}), \
                    mock.patch.object(rc, "probe_master_hdr", return_value={"mdcv": {}}):
                code, text = _quiet_main(["--master", master, "--only", "master_hdr", "--lut", "none",
                                          "--work", d, "--out", out])
            self.assertEqual(code, 0)
            with open(out) as f:
                report = json.load(f)
            self.assertEqual(json.loads(text), report)
            self.assertTrue(report["master_hdr"]["ok"])
            self.assertIn("budget", report)
            self.assertEqual(sorted(os.listdir(d)), ["m.mp4", "r.json"])     # work dir removed
            with mock.patch.object(rc, "host_facts", return_value={}), \
                    mock.patch.object(rc, "probe_master_hdr", side_effect=RuntimeError("x")):
                code, _ = _quiet_main(["--master", master, "--only", "master_hdr", "--lut", "none",
                                       "--work", d])
            self.assertEqual(code, 1)
            # without --only, a probe whose input is missing is skipped, not failed
            with mock.patch.object(rc, "host_facts", return_value={}), \
                    mock.patch.object(rc, "probe_master_hdr", return_value={}), \
                    mock.patch.object(rc, "probe_encode", return_value={}), \
                    mock.patch.object(rc, "probe_cbr", return_value={}), \
                    mock.patch.object(rc, "probe_av1_hwdec", return_value={}), \
                    mock.patch.object(rc, "probe_vt_hdr", return_value={}):
                code, text = _quiet_main(["--master", master, "--lut", "none", "--work", d])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(text)["analysis"], {"skipped": "needs --source"})
        finally:
            shutil.rmtree(d, ignore_errors=True)


def _identity_cube(path):
    with open(path, "w") as f:
        f.write("LUT_3D_SIZE 2\n")
        for b in (0, 1):
            for g in (0, 1):
                for r in (0, 1):
                    f.write(f"{r}.0 {g}.0 {b}.0\n")


@unittest.skipUnless(os.path.exists(dvcap.FFMPEG) and os.path.exists(dvcap.FFPROBE),
                     "needs ffmpeg and ffprobe")
class TinyClips(unittest.TestCase):
    """The probes for real, on 2-second lavfi clips (320x180) in a temp dir."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.cube = os.path.join(cls.td, "identity.cube")
        _identity_cube(cls.cube)
        listing = rc.encoders()
        codec = "libx264" if rc.has_encoder("libx264", listing) else "mpeg4"
        cls.src = os.path.join(cls.td, "src.mp4")
        subprocess.run([rc.FF, "-hide_banner", "-loglevel", "error", "-y",
                        *rc.synthetic_input((320, 180)), "-t", "2", "-c:v", codec,
                        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                        cls.src], check=True)
        probe = os.path.join(cls.td, "vt_probe.mp4")
        r = subprocess.run([rc.FF, "-hide_banner", "-loglevel", "error", "-y", "-i", cls.src,
                            "-frames:v", "2", "-c:v", "hevc_videotoolbox", "-allow_sw", "0", probe],
                           capture_output=True)
        cls.vt = r.returncode == 0
        cls.x265 = rc.has_encoder("libx265", listing)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def setUp(self):
        self.work = tempfile.mkdtemp(dir=self.td)

    def ctx(self, **kw):
        c = {"src": self.src, "src_info": rc.probe_source(self.src), "start": 0.5, "frames": 6,
             "cube": self.cube, "pre_vf": None, "hwdec": False, "nice": 10, "work": self.work,
             "out_size": (320, 180), "src_size": (320, 180), "synthetic": False,
             "inp": rc.input_args(self.src, 0.5), "kbps": 2000}
        c.update(kw)
        return c

    def need_vt(self):
        if not self.vt:
            self.skipTest("hevc_videotoolbox unavailable")

    def test_source_probe(self):
        f = rc.probe_source(self.src)
        self.assertEqual((f["width"], f["height"]), (320, 180))
        self.assertAlmostEqual(f["duration_s"], 2.0, delta=0.1)

    def test_encode(self):
        self.need_vt()
        r = rc.probe_encode(self.ctx())
        self.assertEqual((r["rc"], r["frames"]), (0, 6))
        self.assertGreater(r["cpu_s"], 0)
        self.assertEqual(r["vf"], dvnative.lut_vf(self.cube, "p010le"))
        self.assertTrue(r["under_peak_gate"])
        self.assertEqual(os.listdir(self.work), [])                 # the clip is not kept
        up = rc.probe_encode(self.ctx(out_size=(640, 360), hwdec=True))
        self.assertEqual(up["frames"], 6)
        self.assertTrue(up["vf"].startswith(rc.fit_pad(640, 360)))
        self.assertIn("hwdec", up)

    def test_cbr(self):
        self.need_vt()
        r = rc.probe_cbr(self.ctx())
        self.assertIsInstance(r["works"], bool)
        self.assertIsInstance(r["option_listed"], bool)
        self.assertEqual(r["vbr"]["frames"], 6)
        if r["works"]:
            self.assertEqual(r["cbr"]["frames"], 6)

    def test_av1_hwdec_synthetic(self):
        r = rc.probe_av1_hwdec(self.ctx(src_info=None))
        self.assertIn(r["vt_api_av01"], (True, False, None))
        if r["input"] is None:
            self.skipTest(r["why"])
        self.assertIsInstance(r["ffmpeg_hw_frames"], bool)
        if r["vt_api_av01"] is not None:                # the API and ffmpeg must agree
            self.assertEqual(r["ffmpeg_hw_frames"], r["vt_api_av01"], r["why"])
        self.assertNotIn("decode_software", r)          # only an AV1 --source is timed

    def test_vt_hdr_and_master_hdr(self):
        self.need_vt()
        if not self.x265:
            self.skipTest("ffmpeg has no libx265")
        r = rc.probe_vt_hdr(self.ctx())
        self.assertTrue(r["source"]["mdcv"]["sei_in_packets"] and r["source"]["cll"]["sei_in_packets"])
        for key in ("writes_in_band_sei", "mp4_container_boxes"):
            self.assertEqual(set(r[key]), {"mdcv", "cll"})
            self.assertTrue(all(isinstance(v, bool) for v in r[key].values()))
        self.assertIsInstance(r["values_kept"], bool)
        m = rc.probe_master_hdr(os.path.join(self.work, "vt_hdr_src.mp4"))
        self.assertEqual(m["mdcv"]["value"], dvcap.FALLBACK_MASTER_DISPLAY)
        self.assertEqual(m["cll"]["value"], rc.VT_HDR_CLL)
        self.assertIn(rc.SEI_MDCV, m["sei_payload_types"]["packets"])
        self.assertFalse(m["dolby_vision_record"])

    @unittest.skipUnless(os.path.exists(dvnative.HDR_ANALYZER), "needs the pinned analyzer")
    def test_analysis(self):
        r = rc.measure_analysis(self.src, self.work, cube=self.cube, start=0.5, frames=6, nice=10,
                                analyzer=dvnative.HDR_ANALYZER, min_free_gb=0)
        self.assertEqual((r["frames"], r["l4_frames"]), (6, 6))
        self.assertEqual(set(r["steps"]), {"intermediate", "analyzer", "l4"})
        self.assertGreater(r["cpu_s"], 0)
        self.assertEqual(r["cpu_s_per_frame"], rc.per_frame(r["cpu_s"], 6))
        self.assertEqual(r["chain"], dvnative.analysis_vf(self.cube))
        self.assertGreater(r["intermediate_bytes_per_frame"], 0)
        self.assertEqual(os.listdir(self.work), [])                 # intermediate and results gone

    def test_main_end_to_end(self):
        self.need_vt()
        out = os.path.join(self.work, "report.json")
        code, text = _quiet_main(["--source", self.src, "--frames", "6", "--start", "0.5",
                                  "--encode-size", "320x180", "--lut", self.cube, "--kbps", "2000",
                                  "--only", "encode,cbr", "--work", self.work, "--out", out])
        self.assertEqual(code, 0, text[-500:])
        report = json.loads(text)
        self.assertTrue(report["encode"]["ok"] and report["cbr"]["ok"])
        self.assertEqual(report["source"]["start_s"], 0.5)
        self.assertNotIn("analysis", report)
        self.assertEqual(sorted(os.listdir(self.work)), ["report.json"])


if __name__ == "__main__":
    unittest.main()
