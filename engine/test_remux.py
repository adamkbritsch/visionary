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
        # -fix_sub_duration guards the ORIGINAL input (S09E08: a negative-duration .ass cue
        # otherwise aborts the whole mux) — it must sit BEFORE the second -i, as an input option.
        fix = cmd.index("-fix_sub_duration")
        second_i = [i for i, x in enumerate(cmd) if x == "-i"][1]
        self.assertEqual(fix, second_i - 1)

    def test_extract_without_subs_drops_all_sub_flags(self):
        # Last-resort retry: an unconvertible subtitle track ships the master without subs
        # instead of parking the episode.
        cmd = build_extract_command("/ff", "/cfr.mp4", "/orig.mkv", "/t.mp4", include_subs=False)
        self.assertNotIn("1:s?", cmd)
        self.assertNotIn("mov_text", cmd)
        self.assertIn("0:a", cmd)                      # audio untouched
        self.assertIn("-fix_sub_duration", cmd)        # harmless to keep; input opt only

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


class PeakRepairLadder(unittest.TestCase):
    """A peak-gate miss re-encodes ONLY the offending segments at a tighter cap and re-gates,
    instead of failing forever on identical retries (user-caught: a movie parked at
    58.6 > 50 five times, shipped nothing)."""

    def _run(self, tmp, buckets_seq, reencode_calls):
        import os
        out = os.path.join(tmp, "master.mkv")             # MKV path: no audio machinery to mock
        info = {"frames": 100, "fps": "24000/1001", "master_display": None, "max_cll": None}
        ran = type("R", (), {"returncode": 0, "stderr": ""})()
        def fake_reencode(dv, rpu, segdir, idx, tight, **kw):
            reencode_calls.append((list(idx), tight))
            return True, "ok"
        with mock.patch.object(remux.dvcap, "probe_video", return_value=info), \
             mock.patch.object(remux.dvcap, "ensure_segdir", return_value="fresh"), \
             mock.patch.object(remux.dvcap, "extract_rpu", return_value=(True, "ok")), \
             mock.patch.object(remux.dvcap, "rpu_frame_count", return_value=100), \
             mock.patch.object(remux.dvcap, "encode_capped_segmented", return_value=(True, 100, "ok")), \
             mock.patch.object(remux.dvcap, "reencode_segments_tighter", side_effect=fake_reencode), \
             mock.patch.object(remux.dvcap, "video_peak_buckets", side_effect=list(buckets_seq)), \
             mock.patch.object(remux, "_verify",
                               side_effect=lambda o, fp: remux.RemuxResult(True, o, "8.1", 1, 1,
                                                                           "DV 8.1 · 1 audio · 1 sub")), \
             mock.patch.object(remux.subprocess, "run", return_value=ran):
            return remux.remux(os.path.join(tmp, "dv.mov"), "cfr.mp4", "orig.mkv", out)

    def test_clean_first_pass_never_repairs(self):
        import tempfile
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            res = self._run(tmp, [{1: 45.0}], calls)
        self.assertTrue(res.ok)
        self.assertEqual(calls, [])                       # under the gate → no repair rung ran
        self.assertNotIn("repair", res.reason)
        self.assertIn("peak 45.0 ≤ 50", res.reason)

    def test_over_gate_repairs_offending_segment_then_ships(self):
        import tempfile
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            res = self._run(tmp, [{1: 58.6}, {1: 40.0}], calls)   # over → repaired → clean
        self.assertTrue(res.ok)
        self.assertEqual(calls, [([0], 42)])              # only the hot segment, at 85% of 50
        self.assertIn("peak repair: 1 seg(s) re-capped @ 42 Mbps", res.reason)
        self.assertIn("peak 40.0 ≤ 50", res.reason)

    def test_ladder_exhaustion_ships_nothing(self):
        import tempfile
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            res = self._run(tmp, [{1: 58.6}] * 3, calls)  # never comes under the gate
        self.assertFalse(res.ok)
        self.assertEqual(calls, [([0], 42), ([0], 35)])   # both rungs tried (85%, then 70%)
        self.assertIn("peak still over cap after encode + repair: 58.6", res.reason)


class InjectPath(unittest.TestCase):
    """FAST-PATH remux (rpu-only): the ORIGINAL stream ships with Resolve's RPU injected —
    no re-encode, no peak gate; strict frame/fps alignment gates ship-nothing on mismatch."""

    R_INFO = {"frames": 100, "fps": "24000/1001", "start_time": 0.0,
              "master_display": None, "max_cll": None}
    S_INFO = {"frames": 100, "fps": "24000/1001", "start_time": 0.0,
              "master_display": None, "max_cll": None}

    def _fake_run(self, cmd, **kw):
        R = type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()
        if "-bsf:v" in cmd:                                # source ES extract → must exist
            with open(cmd[-1], "wb") as f:
                f.write(b"ES")
        elif len(cmd) > 1 and cmd[1] == "inject-rpu":      # inject → must exist
            with open(cmd[cmd.index("-o") + 1], "wb") as f:
                f.write(b"INJECTED")
        return R

    def test_success_ships_injected_stream_with_no_peak_gate(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "master.mkv")          # MKV path: no audio machinery
            with mock.patch.object(remux.dvcap, "probe_video",
                                   side_effect=[self.R_INFO, self.S_INFO]), \
                 mock.patch.object(remux.dvcap, "count_hevc_frames", side_effect=[100, 100]), \
                 mock.patch.object(remux.dvcap, "extract_rpu", return_value=(True, "ok")), \
                 mock.patch.object(remux.dvcap, "rpu_frame_count", return_value=100), \
                 mock.patch.object(remux.dvcap, "video_peak_buckets") as peak, \
                 mock.patch.object(remux, "_verify",
                                   side_effect=lambda o, fp: remux.RemuxResult(True, o, "8.1", 2, 1,
                                                                               "DV 8.1 · 2 audio · 1 sub")), \
                 mock.patch.object(remux.subprocess, "run", side_effect=self._fake_run):
                res = remux.remux_inject(os.path.join(tmp, "dv.mov"), "cfr.mkv", "orig.mkv", out)
            self.assertTrue(res.ok)
            self.assertIn("original stream + injected RPU", res.reason)
            peak.assert_not_called()                        # NO peak measurement in this mode
            self.assertFalse(os.path.exists(out + ".remuxsegs"))   # success → RPU dir gone
            self.assertFalse(os.path.exists(out + ".src.hevc"))    # transients swept
            self.assertFalse(os.path.exists(out + ".inject.hevc"))

    def test_rpu_source_frame_mismatch_ships_nothing(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "master.mkv")
            with mock.patch.object(remux.dvcap, "probe_video",
                                   side_effect=[self.R_INFO, self.S_INFO]), \
                 mock.patch.object(remux.dvcap, "count_hevc_frames", return_value=100), \
                 mock.patch.object(remux.dvcap, "extract_rpu", return_value=(True, "ok")), \
                 mock.patch.object(remux.dvcap, "rpu_frame_count", return_value=99), \
                 mock.patch.object(remux.subprocess, "run",
                                   side_effect=AssertionError("must fail BEFORE any mux work")):
                res = remux.remux_inject(os.path.join(tmp, "dv.mov"), "cfr.mkv", "orig.mkv", out)
            self.assertFalse(res.ok)
            self.assertIn("RPU/source frame mismatch", res.reason)
            self.assertTrue(os.path.isdir(out + ".remuxsegs"))  # RPU dir KEPT for the retry

    def test_fps_mismatch_fails_before_any_work(self):
        import os, tempfile
        s = dict(self.S_INFO, fps="25/1")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "master.mkv")
            with mock.patch.object(remux.dvcap, "probe_video", side_effect=[self.R_INFO, s]), \
                 mock.patch.object(remux.dvcap, "count_hevc_frames", return_value=100), \
                 mock.patch.object(remux.dvcap, "extract_rpu",
                                   side_effect=AssertionError("no extract on fps mismatch")):
                res = remux.remux_inject(os.path.join(tmp, "dv.mov"), "cfr.mkv", "orig.mkv", out)
        self.assertFalse(res.ok)
        self.assertIn("fps mismatch", res.reason)

    def test_injected_count_change_ships_nothing(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "master.mkv")
            with mock.patch.object(remux.dvcap, "probe_video",
                                   side_effect=[self.R_INFO, self.S_INFO]), \
                 mock.patch.object(remux.dvcap, "count_hevc_frames", side_effect=[100, 99]), \
                 mock.patch.object(remux.dvcap, "extract_rpu", return_value=(True, "ok")), \
                 mock.patch.object(remux.dvcap, "rpu_frame_count", return_value=100), \
                 mock.patch.object(remux.subprocess, "run", side_effect=self._fake_run):
                res = remux.remux_inject(os.path.join(tmp, "dv.mov"), "cfr.mkv", "orig.mkv", out)
            self.assertFalse(res.ok)
            self.assertIn("frame count changed", res.reason)
            self.assertFalse(os.path.exists(out + ".inject.hevc"))   # transients swept on failure too
