import json
import unittest
from unittest import mock

import remux
from remux import (build_extract_command, build_mux_command, build_mkv_mux_command,
                   parse_streams, has_dolby_vision, dolby_vision_profile, verify_remux,
                   needs_mkv, container_ext)

# Real ffprobe shape (captured from a dovi_tool-built Profile 8.1 file).
DOVI = {"side_data_type": "DOVI configuration record", "dv_profile": 8,
        "dv_bl_signal_compatibility_id": 1, "rpu_present_flag": 1,
        "bl_present_flag": 1, "el_present_flag": 0}
SAMPLE = json.dumps({"streams": [
    {"codec_type": "video", "codec_name": "hevc", "side_data_list": [DOVI]},
    {"codec_type": "audio", "codec_name": "eac3"},
    {"codec_type": "audio", "codec_name": "aac"},
    {"codec_type": "subtitle", "codec_name": "mov_text"},
]})


class BuildCommands(unittest.TestCase):
    def test_extract_audio_from_cfr_subs_from_original(self):
        # MP4 path: audio comes from the CFR file (input 0), subs from the ORIGINAL (input 1).
        cmd = build_extract_command("/ff", "/cfr.mp4", "/orig.mkv", "/t.mp4")
        inputs = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-i"]
        self.assertEqual(inputs, ["/cfr.mp4", "/orig.mkv"])
        self.assertIn("0:a", cmd)        # audio from input 0 (the CFR file)
        self.assertIn("1:s?", cmd)       # subs from input 1 (the original)
        self.assertIn("mov_text", cmd)   # text subs -> mp4 timed text
        self.assertNotIn("0:v", cmd)     # never copies video
        self.assertEqual(cmd[-1], "/t.mp4")

    def test_mkv_mux_copies_dv_video_audio_subs(self):
        # MKV path: one ffmpeg copy — DV video (in0) + audio (CFR, in1) + all subs (original, in2).
        cmd = build_mkv_mux_command("/ff", "/dv.mov", "/cfr.mkv", "/orig.mkv", "/out.mkv")
        self.assertEqual(cmd[0], "/ff")
        inputs = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-i"]
        self.assertEqual(inputs, ["/dv.mov", "/cfr.mkv", "/orig.mkv"])
        self.assertIn("0:v:0", cmd)      # DV video
        self.assertIn("1:a", cmd)        # audio from the CFR file
        self.assertIn("2:s?", cmd)       # ALL subs (incl. bitmap PGS) from the original
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertEqual(cmd[-1], "/out.mkv")

    def test_mux_uses_mp4box_to_preserve_dv(self):
        # ffmpeg drops the DV box; MP4Box keeps it. DV video first, then tracks.
        cmd = build_mux_command("/MP4Box", "/dv.mov", "/t.mp4", "/out.mp4")
        self.assertEqual(cmd[0], "/MP4Box")
        adds = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-add"]
        self.assertEqual(adds, ["/dv.mov", "/t.mp4"])
        self.assertEqual(cmd[cmd.index("-new") + 1], "/out.mp4")

    def test_mux_interleaves_for_playback(self):
        # Subler-style "Optimize": interleave so players don't seek per-track.
        cmd = build_mux_command("/MP4Box", "/dv.mov", "/t.mp4", "/out.mp4")
        self.assertEqual(cmd[cmd.index("-inter") + 1], "500")


class ContainerDecision(unittest.TestCase):
    """MP4 by default; MKV only for content MP4 can't hold (lossless audio / bitmap subs)."""
    def _probe(self, streams):
        return json.dumps({"streams": streams})

    def test_lossless_audio_forces_mkv(self):
        for codec, prof in [("truehd", ""), ("mlp", ""), ("flac", ""), ("alac", ""),
                            ("pcm_s24le", ""), ("pcm_bluray", ""), ("dts", "DTS-HD MA")]:
            self.assertTrue(needs_mkv(self._probe(
                [{"codec_type": "audio", "codec_name": codec, "profile": prof}])), codec)

    def test_bitmap_subs_force_mkv(self):
        for sub in ["hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"]:
            self.assertTrue(needs_mkv(self._probe(
                [{"codec_type": "subtitle", "codec_name": sub}])), sub)

    def test_lossy_audio_and_text_subs_stay_mp4(self):
        # AAC / AC3 / E-AC3 / DTS-core / DTS-HD HRA (lossy) + text subs → MP4.
        streams = [{"codec_type": "audio", "codec_name": "aac", "profile": "LC"},
                   {"codec_type": "audio", "codec_name": "ac3"},
                   {"codec_type": "audio", "codec_name": "eac3"},
                   {"codec_type": "audio", "codec_name": "dts", "profile": "DTS"},
                   {"codec_type": "audio", "codec_name": "dts", "profile": "DTS-HD HRA"},
                   {"codec_type": "subtitle", "codec_name": "subrip"}]
        self.assertFalse(needs_mkv(self._probe(streams)))

    def test_pgs_with_aac_forces_mkv(self):
        # The real Hairspray case: AAC audio + PGS subs → MKV (to keep the subtitles).
        streams = [{"codec_type": "audio", "codec_name": "aac", "profile": "LC"},
                   {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"}]
        self.assertTrue(needs_mkv(self._probe(streams)))

    def test_container_ext_defaults_mp4_when_unprobeable(self):
        # A missing/unprobeable source (e.g. not downloaded yet) → safe '.mp4' default.
        self.assertEqual(container_ext("/no/such/file.mkv"), ".mp4")


class DolbyVision(unittest.TestCase):
    def test_detects_dovi_record(self):
        vstream = json.loads(SAMPLE)["streams"][0]
        self.assertTrue(has_dolby_vision(vstream))

    def test_profile_8_1_from_compat_id(self):
        vstream = json.loads(SAMPLE)["streams"][0]
        self.assertEqual(dolby_vision_profile(vstream), "8.1")

    def test_no_dovi_when_side_data_absent(self):
        self.assertFalse(has_dolby_vision({"codec_type": "video", "codec_name": "hevc"}))


class ParseAndVerify(unittest.TestCase):
    def test_counts_tracks(self):
        s = parse_streams(SAMPLE)
        self.assertEqual(s["video"], 1)
        self.assertEqual(s["audio"], 2)
        self.assertEqual(s["subtitle"], 1)
        self.assertEqual(s["dovi_profile"], "8.1")

    def test_verify_ok_with_dv_and_audio(self):
        ok, _ = verify_remux(SAMPLE)
        self.assertTrue(ok)

    def test_verify_fails_without_dolby_vision(self):
        stripped = json.dumps({"streams": [
            {"codec_type": "video", "codec_name": "hevc"},
            {"codec_type": "audio", "codec_name": "aac"},
        ]})
        ok, reason = verify_remux(stripped)
        self.assertFalse(ok)
        self.assertIn("dolby", reason.lower())

    def test_verify_fails_without_audio(self):
        no_audio = json.dumps({"streams": [
            {"codec_type": "video", "codec_name": "hevc", "side_data_list": [DOVI]},
        ]})
        ok, reason = verify_remux(no_audio)
        self.assertFalse(ok)
        self.assertIn("audio", reason.lower())


class SublerOptimize(unittest.TestCase):
    NO_DV = json.dumps({"streams": [{"codec_type": "video", "codec_name": "hevc"}]})

    def _ran(self, rc):
        m = mock.Mock(); m.returncode = rc; m.stderr = ""
        return m

    def test_optimize_command_is_sublercli_optimize(self):
        self.assertEqual(remux.build_optimize_command("/SublerCLI", "/dv.mov", "/dv.opt.mp4"),
                         ["/SublerCLI", "-source", "/dv.mov", "-dest", "/dv.opt.mp4", "-optimize"])

    def test_uses_optimized_temp_when_dv_survives(self):
        with mock.patch.object(remux.subprocess, "run", return_value=self._ran(0)), \
             mock.patch.object(remux.os.path, "exists", return_value=True), \
             mock.patch.object(remux.os.path, "getsize", return_value=1234), \
             mock.patch.object(remux, "_probe", return_value=SAMPLE):   # SAMPLE carries DV
            out, is_temp = remux.optimize_dv("/dv.mov", "/scratch/out.mp4")
        self.assertEqual(out, "/scratch/out.mp4.dvopt.mp4")
        self.assertTrue(is_temp)

    def test_falls_back_to_original_when_optimize_drops_dv(self):
        with mock.patch.object(remux.subprocess, "run", return_value=self._ran(0)), \
             mock.patch.object(remux.os.path, "exists", return_value=True), \
             mock.patch.object(remux.os.path, "getsize", return_value=1234), \
             mock.patch.object(remux, "_probe", return_value=self.NO_DV), \
             mock.patch.object(remux, "_rm"):
            out, is_temp = remux.optimize_dv("/dv.mov", "/scratch/out.mp4")
        self.assertEqual(out, "/dv.mov")   # never ship a DV-less video
        self.assertFalse(is_temp)

    def test_falls_back_when_sublercli_errors(self):
        with mock.patch.object(remux.subprocess, "run", return_value=self._ran(1)), \
             mock.patch.object(remux, "_rm"):
            out, is_temp = remux.optimize_dv("/dv.mov", "/scratch/out.mp4")
        self.assertEqual((out, is_temp), ("/dv.mov", False))

    def test_falls_back_when_sublercli_missing(self):
        with mock.patch.object(remux.subprocess, "run", side_effect=FileNotFoundError):
            out, is_temp = remux.optimize_dv("/dv.mov", "/scratch/out.mp4")
        self.assertEqual((out, is_temp), ("/dv.mov", False))

    def test_verify_reason_records_optimize_status(self):
        # the remux stage msg (= RemuxResult.reason) must reveal whether Subler engaged
        with mock.patch.object(remux, "_probe", return_value=SAMPLE):
            self.assertTrue(remux._verify("/o.mp4", "/ff", optimized=True).reason.endswith("· optimized"))
            self.assertTrue(remux._verify("/o.mp4", "/ff", optimized=False).reason.endswith("· un-optimized"))
            self.assertNotIn("optimized", remux._verify("/o.mp4", "/ff").reason)   # None → no marker


if __name__ == "__main__":
    unittest.main()
