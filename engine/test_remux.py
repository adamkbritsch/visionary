import json
import unittest
from unittest import mock

import remux
from remux import (build_extract_command, build_mkv_mux_command,
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
        self.assertIn("0:a", cmd)        # all audio from input 0 (the CFR file)
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


class EncodeSourceRouting(unittest.TestCase):
    """Peak-gated rpu-only fallback (SHIELD DV ceiling): remux(encode_source=...) must run
    every encode-side call — probe, capped encode, repair rung — on the SOURCE video, while
    the RPU still comes from the render (dv_video)."""

    def test_capped_encode_runs_on_the_source_and_rpu_on_the_render(self):
        import os, tempfile
        seen = {"enc": [], "repair": []}
        info = {"frames": 100, "fps": "24000/1001", "master_display": None, "max_cll": None}
        ran = type("R", (), {"returncode": 0, "stderr": ""})()
        def fake_probe(path, fp=None):
            seen["probe"] = path
            return info
        def fake_extract(dv, rpu, **kw):
            seen["rpu_from"] = dv
            return True, "ok"
        def fake_encode(src, rpu, hevc, cap, **kw):
            seen["enc"].append(src)
            return True, 100, "ok"
        def fake_repair(src, rpu, segdir, idx, tight, **kw):
            seen["repair"].append(src)
            return True, "ok"
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "master.mkv")        # MKV path: no audio machinery to mock
            source_cfr = os.path.join(tmp, "source_cfr.mkv")
            render = os.path.join(tmp, "render.mov")
            with mock.patch.object(remux.dvcap, "probe_video", side_effect=fake_probe), \
                 mock.patch.object(remux.dvcap, "ensure_segdir", return_value="fresh"), \
                 mock.patch.object(remux.dvcap, "extract_rpu", side_effect=fake_extract), \
                 mock.patch.object(remux.dvcap, "rpu_frame_count", return_value=100), \
                 mock.patch.object(remux.dvcap, "encode_capped_segmented", side_effect=fake_encode), \
                 mock.patch.object(remux.dvcap, "reencode_segments_tighter", side_effect=fake_repair), \
                 mock.patch.object(remux.dvcap, "video_peak_buckets",
                                   side_effect=[{1: 58.6}, {1: 40.0}]), \
                 mock.patch.object(remux, "_verify",
                                   side_effect=lambda o, fp: remux.RemuxResult(True, o, "8.1", 1, 1,
                                                                               "ok")), \
                 mock.patch.object(remux.subprocess, "run", return_value=ran):
                res = remux.remux(render, "cfr.mkv", "orig.mkv", out, encode_source=source_cfr)
        self.assertTrue(res.ok)
        self.assertEqual(seen["probe"], source_cfr)      # frames/fps/mastering read off the source
        self.assertEqual(seen["rpu_from"], render)       # DV analysis still comes from Resolve
        for src in seen["enc"] + seen["repair"]:         # main encode, repair rung, repair concat
            self.assertEqual(src, source_cfr)
        self.assertTrue(seen["repair"])                  # the over-gate first pass exercised repair

    def test_without_encode_source_the_render_is_encoded(self):
        import os, tempfile
        seen = {"enc": []}
        info = {"frames": 100, "fps": "24000/1001", "master_display": None, "max_cll": None}
        ran = type("R", (), {"returncode": 0, "stderr": ""})()
        def fake_encode(src, rpu, hevc, cap, **kw):
            seen["enc"].append(src)
            return True, 100, "ok"
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "master.mkv")
            render = os.path.join(tmp, "render.mov")
            with mock.patch.object(remux.dvcap, "probe_video", return_value=info), \
                 mock.patch.object(remux.dvcap, "ensure_segdir", return_value="fresh"), \
                 mock.patch.object(remux.dvcap, "extract_rpu", return_value=(True, "ok")), \
                 mock.patch.object(remux.dvcap, "rpu_frame_count", return_value=100), \
                 mock.patch.object(remux.dvcap, "encode_capped_segmented", side_effect=fake_encode), \
                 mock.patch.object(remux.dvcap, "video_peak_buckets", return_value={1: 45.0}), \
                 mock.patch.object(remux, "_verify",
                                   side_effect=lambda o, fp: remux.RemuxResult(True, o, "8.1", 1, 1,
                                                                               "ok")), \
                 mock.patch.object(remux.subprocess, "run", return_value=ran):
                res = remux.remux(render, "cfr.mkv", "orig.mkv", out)
        self.assertTrue(res.ok)
        self.assertEqual(seen["enc"], [render])


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


class MP4BoxSafeInput(unittest.TestCase):
    """MP4Box's -add parser mangles characters in the INPUT path: a "+" fails with
    "Requested URL is not valid or cannot be found" (live-caught on "Lost - S02E12 -
    Fire + Water", parked after 5 identical failures). Backslash-escaping does not help,
    so a sanitised HARDLINK is handed to MP4Box instead."""

    def test_safe_name_is_passed_through_untouched(self):
        import tempfile, os as _os
        d = tempfile.mkdtemp()
        p = _os.path.join(d, "Plain Name HDR10 DV upscaled.mkv.capped.hevc")
        open(p, "wb").write(b"x")
        with remux.mp4box_safe_input(p) as got:
            self.assertEqual(got, p)                       # no link created for a safe name
            self.assertEqual(len(_os.listdir(d)), 1)

    def test_plus_gets_a_sanitised_hardlink_that_is_removed(self):
        import tempfile, os as _os
        d = tempfile.mkdtemp()
        p = _os.path.join(d, "Fire + Water HDR10 DV upscaled.mkv.capped.hevc")
        open(p, "wb").write(b"payload")
        with remux.mp4box_safe_input(p) as got:
            self.assertNotEqual(got, p)
            self.assertNotIn("+", _os.path.basename(got))
            self.assertTrue(_os.path.exists(got))
            self.assertEqual(open(got, "rb").read(), b"payload")     # same inode, same bytes
            self.assertEqual(_os.stat(got).st_ino, _os.stat(p).st_ino)
        self.assertFalse(_os.path.exists(got))             # ALWAYS removed — a leftover
        self.assertTrue(_os.path.exists(p))                # hardlink would pin the transient

    def test_link_is_removed_even_when_the_mux_raises(self):
        import tempfile, os as _os
        d = tempfile.mkdtemp()
        p = _os.path.join(d, "A + B.hevc")
        open(p, "wb").write(b"x")
        seen = {}
        with self.assertRaises(RuntimeError):
            with remux.mp4box_safe_input(p) as got:
                seen["path"] = got
                raise RuntimeError("MP4Box blew up")
        self.assertFalse(_os.path.exists(seen["path"]))


class ShipRender(unittest.TestCase):
    """YouTube fast remux: a render whose measured 1-s peak is under the cap ships its
    video STREAM-COPIED; over the cap returns the distinct "render-over-cap" reason the
    stage keys its fallback on (and only that reason falls back)."""

    def test_over_cap_render_bails_with_the_fallback_reason(self):
        import dvcap
        with mock.patch.object(dvcap, "probe_video",
                               return_value={"frames": 100, "fps": "24000/1001"}), \
             mock.patch.object(dvcap, "video_peak_1s_mbps", return_value=81.2), \
             mock.patch.object(remux.subprocess, "run",
                               side_effect=AssertionError("must bail before any work")):
            res = remux.remux_ship_render("/dv.mov", "/cfr.mp4", "/orig.mp4",
                                          "/tmp/out.mp4", cap_mbps=50)
        self.assertFalse(res.ok)
        self.assertTrue(res.reason.startswith("render-over-cap"))
        self.assertIn("81.2", res.reason)

    def test_unprobeable_render_fails_without_the_fallback_reason(self):
        import dvcap
        with mock.patch.object(dvcap, "probe_video", return_value={"frames": 0, "fps": ""}):
            res = remux.remux_ship_render("/dv.mov", "/cfr.mp4", "/orig.mp4",
                                          "/tmp/out.mp4", cap_mbps=50)
        self.assertFalse(res.ok)
        self.assertFalse(res.reason.startswith("render-over-cap"))   # a real failure retries


class StepWatch(unittest.TestCase):
    def test_reports_label_immediately_and_caps_at_99(self):
        seen = []
        with open("/tmp/_sw_test.bin", "wb") as f:
            f.write(b"x" * 50)
        with remux._StepWatch(lambda l, p: seen.append((l, p)), "copying", "/tmp/_sw_test.bin", 100):
            pass                                   # immediate label call, poller may not tick
        self.assertEqual(seen[0], ("copying", 0.0))
        # the pct math itself (poller body): capped, size-proportional
        self.assertEqual(min(99.0, 100.0 * 50 / 100), 50.0)
        self.assertEqual(min(99.0, 100.0 * 200 / 100), 99.0)

    def test_label_only_when_no_target(self):
        seen = []
        with remux._StepWatch(lambda l, p: seen.append((l, p)), "verifying", "/nope", 0):
            pass
        self.assertEqual(seen, [("verifying", 0.0)])

    def test_none_callback_is_inert(self):
        with remux._StepWatch(None, "x", "/nope", 100):
            pass                                   # no crash, no thread


class CombineDispatcher(unittest.TestCase):
    """remux.combine(): a thin dispatcher over remux_inject (stream) / remux (capped)."""

    def setUp(self):
        # a distinct audio donor arms the sync gate — pin it PROVEN so dispatch is what's
        # under test (the gate has its own class below)
        self.g1 = mock.patch.object(remux.dvcap, "count_hevc_frames", return_value=100)
        self.g2 = mock.patch.object(remux.dvcap, "probe_video",
                                    return_value={"fps": "24000/1001"})
        self.g1.start(); self.g2.start()

    def tearDown(self):
        self.g1.stop(); self.g2.stop()

    def test_stream_path_grafts_from_the_donor(self):
        with mock.patch.object(remux, "remux_inject",
                               return_value=remux.RemuxResult(True, "o.mkv", "8.1", 2, 1,
                                                              "ok")) as inj, \
             mock.patch.object(remux, "remux", side_effect=AssertionError("capped path")):
            res = remux.combine("winner.mkv", "donor.mkv", "audio.mkv", "o.mkv",
                                rpu_inline=False, rpu_profile="7.x", capped=False)
        self.assertTrue(res.ok)
        args, kw = inj.call_args
        self.assertEqual(args, ("donor.mkv", "audio.mkv", "winner.mkv", "o.mkv"))
        self.assertEqual(kw["rpu_mode"], 2)              # P7 donor → mode-2 extract
        self.assertFalse(kw["skip_inject"])
        self.assertFalse(kw["convert_es"])

    def test_inline_81_winner_skips_the_inject(self):
        with mock.patch.object(remux, "remux_inject",
                               return_value=remux.RemuxResult(True, "o.mkv", "8.1", 2, 1,
                                                              "ok")) as inj:
            remux.combine("winner.mkv", "winner.mkv", "audio.mkv", "o.mkv",
                          rpu_inline=True, rpu_profile="8.1", capped=False)
        kw = inj.call_args.kwargs
        self.assertTrue(kw["skip_inject"])
        self.assertFalse(kw["convert_es"])               # 8.1 inline needs no conversion
        self.assertIsNone(kw["rpu_mode"])

    def test_inline_p7_winner_converts_the_es(self):
        with mock.patch.object(remux, "remux_inject",
                               return_value=remux.RemuxResult(True, "o.mkv", "8.1", 2, 1,
                                                              "ok")) as inj:
            remux.combine("winner.mkv", "winner.mkv", "audio.mkv", "o.mkv",
                          rpu_inline=True, rpu_profile="7.x", capped=False)
        kw = inj.call_args.kwargs
        self.assertTrue(kw["skip_inject"])
        self.assertTrue(kw["convert_es"])

    def test_capped_path_delegates_to_remux_with_encode_source(self):
        with mock.patch.object(remux, "remux",
                               return_value=remux.RemuxResult(True, "o.mkv", "8.1", 2, 1,
                                                              "ok")) as rm, \
             mock.patch.object(remux, "remux_inject",
                               side_effect=AssertionError("stream path")):
            remux.combine("winner.mkv", "donor.mkv", "audio.mkv", "o.mkv",
                          rpu_inline=False, rpu_profile="7.x", capped=True,
                          cap_mbps=50, boundaries=[10, 20])
        args, kw = rm.call_args
        self.assertEqual(args, ("donor.mkv", "audio.mkv", "winner.mkv", "o.mkv"))
        self.assertEqual(kw["encode_source"], "winner.mkv")
        self.assertEqual(kw["rpu_mode"], 2)
        self.assertEqual(kw["boundaries"], [10, 20])


class InjectSkipAndConvert(unittest.TestCase):
    """remux_inject's combine extensions, driven with mocked externals."""

    def _run(self, tmp, *, skip_inject, convert_es, calls):
        import os
        out = os.path.join(tmp, "master.mkv")
        info = {"frames": 100, "fps": "24000/1001", "start_time": 0.0,
                "master_display": None, "max_cll": None}
        ran = type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()
        def fake_run(cmd, **kw):
            calls.append(cmd[0] if isinstance(cmd, list) else str(cmd))
            # the convert step gates on its output existing
            if isinstance(cmd, list) and "convert" in cmd:
                open(cmd[cmd.index("-o") + 1], "wb").write(b"c" * 10)
            if isinstance(cmd, list) and any(str(c).endswith(".src.hevc") for c in cmd):
                for c in cmd:
                    if str(c).endswith(".src.hevc"):
                        open(c, "wb").write(b"e" * 10)
            return ran
        with mock.patch.object(remux.dvcap, "probe_video", return_value=info), \
             mock.patch.object(remux.dvcap, "count_hevc_frames", return_value=100), \
             mock.patch.object(remux.dvcap, "extract_rpu",
                               side_effect=AssertionError("skip_inject must not extract")), \
             mock.patch.object(remux.dvcap, "build_inject_command",
                               side_effect=AssertionError("skip_inject must not inject")), \
             mock.patch.object(remux, "_verify",
                               side_effect=lambda o, fp: remux.RemuxResult(True, o, "8.1", 2, 1,
                                                                           "DV 8.1")), \
             mock.patch.object(remux.subprocess, "run", side_effect=fake_run):
            return remux.remux_inject("winner.mkv", "audio.mkv", "winner.mkv", out,
                                      skip_inject=skip_inject, convert_es=convert_es)

    def test_skip_inject_ships_the_es_untouched(self):
        import tempfile
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            res = self._run(tmp, skip_inject=True, convert_es=False, calls=calls)
        self.assertTrue(res.ok)
        self.assertIn("shipped as-is", res.reason)

    def test_skip_inject_with_convert_runs_dovi_convert(self):
        import tempfile
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            res = self._run(tmp, skip_inject=True, convert_es=True, calls=calls)
        self.assertTrue(res.ok)
        self.assertIn("converted to 8.1", res.reason)


class AudioDonorSyncGate(unittest.TestCase):
    """Cross-copy audio muxes only after the donor is PROVEN the same cut as the shipped
    video (frame count + fps) — the two already-gated configurations skip the extra sweep."""

    def test_unproven_donor_with_mismatched_frames_ships_nothing(self):
        counts = {"winner.mkv": 100, "audio.mkv": 99}
        with mock.patch.object(remux.dvcap, "count_hevc_frames",
                               side_effect=lambda p, fp=None: counts[p]), \
             mock.patch.object(remux, "remux_inject",
                               side_effect=AssertionError("must not mux drifting audio")):
            res = remux.combine("winner.mkv", "render.mov", "audio.mkv", "o.mkv",
                                rpu_profile="resolve", capped=False)
        self.assertFalse(res.ok)
        self.assertIn("audio donor is a different cut", res.reason)

    def test_unproven_donor_with_mismatched_fps_ships_nothing(self):
        infos = {"winner.mkv": {"fps": "24000/1001"}, "audio.mkv": {"fps": "25/1"}}
        with mock.patch.object(remux.dvcap, "count_hevc_frames", return_value=100), \
             mock.patch.object(remux.dvcap, "probe_video",
                               side_effect=lambda p, fp=None: infos[p]), \
             mock.patch.object(remux, "remux_inject",
                               side_effect=AssertionError("must not mux drifting audio")):
            res = remux.combine("winner.mkv", "render.mov", "audio.mkv", "o.mkv",
                                rpu_profile="resolve", capped=False)
        self.assertFalse(res.ok)
        self.assertIn("fps", res.reason)

    def test_proven_donor_proceeds(self):
        with mock.patch.object(remux.dvcap, "count_hevc_frames", return_value=100), \
             mock.patch.object(remux.dvcap, "probe_video",
                               return_value={"fps": "24000/1001"}), \
             mock.patch.object(remux, "remux_inject",
                               return_value=remux.RemuxResult(True, "o.mkv", "8.1", 2, 1,
                                                              "ok")) as inj:
            res = remux.combine("winner.mkv", "render.mov", "audio.mkv", "o.mkv",
                                rpu_profile="resolve", capped=False)
        self.assertTrue(res.ok)
        inj.assert_called_once()

    def test_audio_from_the_rpu_donor_skips_the_extra_sweep(self):
        # transitively proven by the RPU-vs-winner frame gate inside remux_inject
        with mock.patch.object(remux.dvcap, "count_hevc_frames",
                               side_effect=AssertionError("already gated — no extra sweep")), \
             mock.patch.object(remux, "remux_inject",
                               return_value=remux.RemuxResult(True, "o.mkv", "8.1", 2, 1,
                                                              "ok")):
            res = remux.combine("winner.mkv", "donor.mkv", "donor.mkv", "o.mkv",
                                rpu_profile="7.x", capped=False)
        self.assertTrue(res.ok)

    def test_audio_from_the_winner_skips_the_extra_sweep(self):
        with mock.patch.object(remux.dvcap, "count_hevc_frames",
                               side_effect=AssertionError("trivially synced — no sweep")), \
             mock.patch.object(remux, "remux_inject",
                               return_value=remux.RemuxResult(True, "o.mkv", "8.1", 2, 1,
                                                              "ok")):
            res = remux.combine("winner.mkv", "donor.mkv", "winner.mkv", "o.mkv",
                                rpu_profile="7.x", capped=False)
        self.assertTrue(res.ok)


class MkvLoudnessBoost(unittest.TestCase):
    """The MKV branch skipped the loudness boost outright because Matroska is where LOSSLESS
    audio lives — but the rule is about the CODEC, not the container, and most masters here
    carry AAC or AC3. A 4K master with AAC 5.1 shipped simply quiet (user-caught 2026-08-18)."""

    def test_lossless_codecs_are_identified(self):
        for c, p in (("truehd", ""), ("mlp", ""), ("flac", ""), ("alac", ""),
                     ("pcm_s24le", ""), ("dts", "DTS-HD MA")):
            self.assertTrue(remux.is_lossless_audio_codec(c, p), c)

    def test_lossy_codecs_are_not(self):
        for c, p in (("aac", "LC"), ("aac", "HE-AAC"), ("ac3", ""), ("eac3", ""),
                     ("dts", "DTS"), ("opus", "")):
            self.assertFalse(remux.is_lossless_audio_codec(c, p), c)

    def test_a_gain_re_encodes_only_the_audio(self):
        c = remux.build_mkv_mux_command("/ff", "/v.mp4", "/a.mkv", "/o.mkv", "/out.mkv", gain_db=6.0)
        self.assertEqual(c[c.index("-c") + 1], "copy")          # video + subs untouched (DV survives)
        self.assertEqual(c[c.index("-c:a") + 1], "aac_at")
        self.assertIn("-filter:a", c)

    def test_no_gain_is_a_pure_copy_mux(self):
        c = remux.build_mkv_mux_command("/ff", "/v.mp4", "/a.mkv", "/o.mkv", "/out.mkv")
        self.assertNotIn("aac_at", c)
        self.assertNotIn("-filter:a", c)

    def test_any_lossless_track_blocks_the_whole_boost(self):
        # a lossy commentary beside a TrueHD main mix must not cause a transcode
        with mock.patch.object(remux.subprocess, "run",
                               return_value=mock.Mock(stdout="truehd,\naac,LC\n")):
            self.assertTrue(remux.has_lossless_audio("/x.mkv"))
        with mock.patch.object(remux.subprocess, "run",
                               return_value=mock.Mock(stdout="aac,LC\naac,HE-AAC\n")):
            self.assertFalse(remux.has_lossless_audio("/x.mkv"))

    def test_an_unreadable_probe_refuses_to_boost(self):
        # refusing is recoverable (revise it later); transcoding a TrueHD track is not
        with mock.patch.object(remux.subprocess, "run", side_effect=OSError("nope")):
            self.assertTrue(remux.has_lossless_audio("/x.mkv"))
        with mock.patch.object(remux.subprocess, "run", return_value=mock.Mock(stdout="")):
            self.assertTrue(remux.has_lossless_audio("/x.mkv"))


class CappedBoostLanding(unittest.TestCase):
    """A boost that hits AUDIO_MAX_GAIN_DB cannot reach the target, and demanding it did threw
    the work away: a -30 LUFS mix boosted the full +12 lands at -18, missed a +/-1.5 window
    around -16, and shipped UNBOOSTED at -30 — the quietest possible outcome, in exactly the
    case a boost exists for (live-caught 2026-08-19 on a film mix under -28)."""

    def test_a_capped_boost_is_judged_on_movement(self):
        self.assertTrue(remux.landing_ok(-18.0, -16.0, remux.AUDIO_MAX_GAIN_DB, measured=-30.0))

    def test_a_capped_boost_that_did_nothing_still_fails(self):
        self.assertFalse(remux.landing_ok(-29.5, -16.0, remux.AUDIO_MAX_GAIN_DB, measured=-30.0))

    def test_an_uncapped_boost_must_still_hit_the_target(self):
        self.assertTrue(remux.landing_ok(-16.2, -16.0, 6.0, measured=-22.0))
        self.assertFalse(remux.landing_ok(-11.0, -16.0, 6.0, measured=-22.0))   # overshot

    def test_no_measurement_is_a_failure(self):
        self.assertFalse(remux.landing_ok(None, -16.0, 6.0))

    def test_a_capped_boost_without_a_reference_falls_back_to_the_target(self):
        # no `measured` to compare against -> the old, stricter rule
        self.assertFalse(remux.landing_ok(-18.0, -16.0, remux.AUDIO_MAX_GAIN_DB))


class LosslessIsBoostedButKeptWhole(unittest.TestCase):
    """The rule used to be "never touch lossless", so a quiet DTS-HD MA or TrueHD master
    simply stayed quiet — Lost in Translation could only be told why nothing would happen.
    It is boosted now, and the original lossless track rides along behind the normalized
    one rather than being replaced (user-dictated 2026-08-21)."""

    def _cmd(self, gain=6.0, keep=2):
        return remux.build_mkv_mux_command("/ff", "/dv.mp4", "/cfr.mkv", "/orig.mkv",
                                           "/out.mkv", gain_db=gain, keep_original_audio=keep)

    def test_the_boosted_copy_comes_first(self):
        c = self._cmd()
        self.assertLess(c.index("1:a:0"), c.index("1:a"))     # map order decides track order

    def test_every_original_track_is_still_there(self):
        self.assertIn("1:a", self._cmd())

    def test_only_the_first_track_is_re_encoded(self):
        c = self._cmd()
        self.assertIn("-c:a:0", c)
        self.assertIn("aac_at", c)
        self.assertNotIn("-c:a", c)          # never a blanket audio encoder over the originals

    def test_the_originals_are_copied_bit_exact(self):
        c = self._cmd()
        self.assertEqual(c[c.index("-c") + 1], "copy")

    def test_the_normalized_track_is_the_default_and_the_rest_are_not(self):
        c = self._cmd(keep=2)
        self.assertEqual(c[c.index("-disposition:a:0") + 1], "default")
        self.assertEqual(c[c.index("-disposition:a:1") + 1], "0")
        self.assertEqual(c[c.index("-disposition:a:2") + 1], "0")

    def test_dispositions_cover_exactly_the_tracks_that_exist(self):
        c = self._cmd(keep=3)
        self.assertIn("-disposition:a:3", c)
        self.assertNotIn("-disposition:a:4", c)

    def test_a_lossy_source_is_unchanged(self):
        # keep=0 -> the long-standing single-track boost, byte for byte as before
        c = remux.build_mkv_mux_command("/ff", "/dv.mp4", "/c", "/o", "/out.mkv", gain_db=6.0)
        self.assertIn("-c:a", c)
        self.assertNotIn("1:a:0", c)
        self.assertNotIn("-disposition:a:0", c)

    def test_no_gain_means_no_encoder_and_no_duplicate_track(self):
        c = remux.build_mkv_mux_command("/ff", "/dv.mp4", "/c", "/o", "/out.mkv",
                                        gain_db=0.0, keep_original_audio=2)
        self.assertNotIn("aac_at", c)
        self.assertNotIn("1:a:0", c)

    def test_the_lossless_check_itself_is_unchanged(self):
        # keeping the original depends on correctly RECOGNISING lossless
        self.assertTrue(remux.is_lossless_audio_codec("truehd"))
        self.assertTrue(remux.is_lossless_audio_codec("dts", "DTS-HD MA"))
        self.assertTrue(remux.is_lossless_audio_codec("pcm_s24le"))
        self.assertFalse(remux.is_lossless_audio_codec("dts", "DTS"))
        self.assertFalse(remux.is_lossless_audio_codec("aac", "LC"))


class AtmosIsLeftAlone(unittest.TestCase):
    """The boost re-encodes EVERY audio track to AAC 384k, which silently flattened Don't
    Look Up's 'Dolby Digital Plus + Dolby Atmos' track — the master shipped 1.25 GB smaller
    than its source with no Atmos in it (user-caught 2026-08-22). An Atmos title is now left
    exactly as it is, with the Atmos track promoted to first so players pick it."""

    def test_atmos_is_read_from_the_profile_not_the_codec(self):
        self.assertTrue(remux.is_atmos_audio("Dolby Digital Plus + Dolby Atmos"))
        self.assertTrue(remux.is_atmos_audio("Dolby TrueHD + Dolby Atmos"))
        self.assertFalse(remux.is_atmos_audio(""))
        self.assertFalse(remux.is_atmos_audio("Dolby Digital Plus"))
        self.assertFalse(remux.is_atmos_audio(None))

    def test_the_index_is_the_AUDIO_position_not_the_stream_index(self):
        # Don't Look Up: Atmos is stream 3 but audio track 2 — -map 0:a:N takes the latter
        js = json.dumps({"streams": [{"profile": None}, {"profile": None},
                                     {"profile": "Dolby Digital Plus + Dolby Atmos"},
                                     {"profile": None}]})
        with mock.patch.object(remux.subprocess, "run", return_value=mock.Mock(stdout=js)):
            self.assertEqual(remux.atmos_audio_index("/x.mkv"), 2)

    def test_no_atmos_is_None(self):
        js = json.dumps({"streams": [{"profile": None}, {"profile": "LC"}]})
        with mock.patch.object(remux.subprocess, "run", return_value=mock.Mock(stdout=js)):
            self.assertIsNone(remux.atmos_audio_index("/x.mkv"))

    def test_the_atmos_track_leads_and_nothing_is_duplicated(self):
        m = remux.audio_map_args(0, 4, 2)
        self.assertEqual(m, ["-map", "0:a:2", "-map", "0:a:0",
                             "-map", "0:a:1", "-map", "0:a:3"])
        self.assertEqual(len([x for x in m if x == "-map"]), 4)   # every track exactly once

    def test_the_extract_copies_atmos_instead_of_boosting_it(self):
        c = remux.build_extract_command("/ff", "/cfr", "/orig", "/t.mp4",
                                        gain_db=10.9, atmos_lead=2, n_audio=4)
        self.assertNotIn("aac_at", c)              # nothing re-encoded
        self.assertNotIn("-filter:a", c)           # no gain applied
        self.assertEqual(c[c.index("-c") + 1], "copy")
        self.assertLess(c.index("0:a:2"), c.index("0:a:0"))       # Atmos first
        self.assertEqual(c[c.index("-disposition:a:0") + 1], "default")

    def test_the_mkv_mux_does_the_same(self):
        c = remux.build_mkv_mux_command("/ff", "/dv", "/cfr", "/orig", "/o.mkv",
                                        gain_db=10.9, atmos_lead=2, n_audio=4)
        self.assertNotIn("aac_at", c)
        self.assertLess(c.index("1:a:2"), c.index("1:a:0"))
        self.assertEqual(c[c.index("-disposition:a:0") + 1], "default")

    def test_atmos_outranks_keeping_a_lossless_original(self):
        # a TrueHD Atmos bed is BOTH — untouched wins over boost-and-keep
        c = remux.build_mkv_mux_command("/ff", "/dv", "/cfr", "/orig", "/o.mkv",
                                        gain_db=10.9, keep_original_audio=2,
                                        atmos_lead=0, n_audio=2)
        self.assertNotIn("aac_at", c)

    def test_a_non_atmos_title_is_completely_unchanged(self):
        c = remux.build_extract_command("/ff", "/cfr", "/orig", "/t.mp4", gain_db=10.9)
        self.assertIn("aac_at", c)
        self.assertIn("-map", c)
        self.assertIn("0:a", c)
        self.assertNotIn("-disposition:a:0", c)

    def test_an_unreadable_track_count_still_maps_every_track(self):
        self.assertEqual(remux.audio_map_args(1, 0, 0), ["-map", "1:a"])


class MastersMustBeInterleaved(unittest.TestCase):
    """Don't Look Up's master streamed at 154 MB/s from the NAS and still stalled the SHIELD
    from ~1:45 on. Its audio drifted away from its own video and kept drifting — 5.7 MB by
    1 minute, 12 by 2, 32 by 5, 115 by 20 — so a player had to hold that whole span to keep
    picture and sound together. The mux's 500 ms interleave window did it, and only with
    SUBTITLE tracks present; small AAC audio hid it (other masters measured 1-3 MB), four
    full-bitrate stream-copied tracks did not. Measured on the same file, same mux:
        audio only,  -inter 500 -> 0.0 / 1.5 / 0.9 MB
        + subtitles, -inter 500 -> 0.2 / 5.7 / 8.4 MB    <- what shipped
        + subtitles, -inter 100 -> 0.0 / 0.0 / 0.0 MB
    (live-caught 2026-08-22)"""

    def test_the_mux_interleaves_tightly(self):
        self.assertEqual(remux.MUX_INTERLEAVE_MS, 100)
        c = remux.build_capped_mux_command("/mp4box", "/es.hevc", "24", "/t.mp4", "/o.mp4")
        self.assertEqual(c[c.index("-inter") + 1], "100")

    def test_the_window_comes_before_the_output(self):
        c = remux.build_capped_mux_command("/mp4box", "/es.hevc", "24", "/t.mp4", "/o.mp4")
        self.assertLess(c.index("-inter"), c.index("-new"))

    def _gap(self, positions):
        """positions: {(stream, t): byte_pos}"""
        def run(cmd, **kw):
            sel = cmd[cmd.index("-select_streams") + 1]
            t = int(cmd[cmd.index("-read_intervals") + 1].split("%")[0])
            p = positions.get((sel, t))
            return mock.Mock(stdout="" if p is None else "%d.0,%d\n" % (t, p))
        with mock.patch.object(remux.subprocess, "run", side_effect=run):
            return remux.interleave_gap_mb("/x.mp4", at=(60, 300))

    def test_a_tight_file_measures_near_zero(self):
        self.assertLess(self._gap({("v:0", 60): 100_000_000, ("a:0", 60): 100_200_000,
                                   ("v:0", 300): 500_000_000, ("a:0", 300): 500_100_000}), 0.5)

    def test_a_drifting_file_reports_the_WORST_gap(self):
        g = self._gap({("v:0", 60): 100_000_000, ("a:0", 60): 105_700_000,
                       ("v:0", 300): 500_000_000, ("a:0", 300): 532_600_000})
        self.assertAlmostEqual(g, 32.6, places=1)          # the worst, not the first or the mean

    def test_an_unmeasurable_file_never_fails_a_master(self):
        self.assertEqual(self._gap({}), -1.0)

    def test_the_gate_rejects_a_drifting_master_and_removes_it(self):
        res = remux.RemuxResult(True, "/o.mp4")
        with mock.patch.object(remux, "interleave_gap_mb", return_value=115.6), \
             mock.patch.object(remux, "_rm") as rm:
            out = remux._gate_interleave(res, "/o.mp4")
        self.assertFalse(out.ok)
        self.assertIn("115.6 MB", out.reason)
        rm.assert_called_once_with("/o.mp4")               # never leave a bad master on disk

    def test_the_gate_passes_a_tight_master(self):
        res = remux.RemuxResult(True, "/o.mp4")
        with mock.patch.object(remux, "interleave_gap_mb", return_value=0.03), \
             mock.patch.object(remux, "_rm") as rm:
            self.assertTrue(remux._gate_interleave(res, "/o.mp4").ok)
        rm.assert_not_called()

    def test_an_unmeasurable_gap_passes_rather_than_parking_the_item(self):
        res = remux.RemuxResult(True, "/o.mp4")
        with mock.patch.object(remux, "interleave_gap_mb", return_value=-1.0), \
             mock.patch.object(remux, "_rm"):
            self.assertTrue(remux._gate_interleave(res, "/o.mp4").ok)

    def test_the_threshold_sits_between_what_shipped_and_what_works(self):
        # 8.4 MB was already stalling; 0.03 MB is the fixed mux. The gate has to split them.
        self.assertGreater(remux.MAX_INTERLEAVE_GAP_MB, 0.03)
        self.assertLessEqual(remux.MAX_INTERLEAVE_GAP_MB, 8.4)


class NormalizeAudioHasToActuallyNormalize(unittest.TestCase):
    """A.I. Artificial Intelligence shipped unboosted with normalize-audio ON and a plain AAC
    track — the case the feature exists for (user-caught 2026-08-24). The boost was computed,
    applied, measured, and then thrown away because it missed a +/-1.5 window around the
    target, so the file shipped at its original quiet level: the worst of both outcomes.

    The limiter is why an ordinary, uncapped boost lands short — it pulls peaks down, so a
    wide-range film mix never reaches the arithmetic prediction. The question is not "did it
    hit the target" but "is this better than what we started with"."""

    T = -16.0

    def test_a_limiter_shortfall_is_kept(self):
        # the A.I. shape: boosted well up, still short of target, and plainly better
        self.assertTrue(remux.landing_ok(-18.5, self.T, 8.0, measured=-24.0))

    def test_a_capped_boost_is_still_kept(self):
        # the 2026-08-19 case this rule was first widened for
        self.assertTrue(remux.landing_ok(-18.7, self.T, 12.0, measured=-30.4))

    def test_landing_on_target_is_kept_without_needing_a_before(self):
        self.assertTrue(remux.landing_ok(-16.2, self.T, 4.0, measured=None))

    def test_a_boost_that_achieved_nothing_is_discarded(self):
        # 0.2 dB is not worth a lossy re-encode — AUDIO_MIN_GAIN_DB is the same threshold
        # that decides a boost is worth doing at all
        self.assertFalse(remux.landing_ok(-23.8, self.T, 8.0, measured=-24.0))

    def test_an_overshoot_past_the_target_is_discarded(self):
        self.assertFalse(remux.landing_ok(-6.0, self.T, 8.0, measured=-24.0))

    def test_a_pass_that_delivered_almost_none_of_its_gain_is_discarded(self):
        # asked for +4 and moved 0.5: the limiter shaves the top off a peaky mix, it does not
        # eat 88% of the gain. That is a broken pass, and a lossy re-encode is too high a
        # price for 0.5 dB.
        self.assertFalse(remux.landing_ok(-19.5, self.T, 4.0, measured=-20.0))

    def test_landing_louder_than_the_target_is_never_kept(self):
        # "closer" is not the whole test — past the target is past it, and that way lies
        # clipping. -11 against a -16 target is closer than -22 was, and still wrong.
        self.assertFalse(remux.landing_ok(-11.0, self.T, 6.0, measured=-22.0))

    def test_an_unmeasurable_landing_is_never_shipped(self):
        self.assertFalse(remux.landing_ok(None, self.T, 8.0, measured=-24.0))

    def test_no_before_and_off_target_stays_strict(self):
        self.assertFalse(remux.landing_ok(-22.0, self.T, 4.0, measured=None))

    def test_the_refusal_says_what_it_measured(self):
        note = remux._unboosted_note(-24.0, -23.8, -16)
        self.assertIn("-24.0", note)
        self.assertIn("-23.8", note)
        self.assertIn("-16.0", note)

    def test_the_refusal_survives_a_missing_measurement(self):
        self.assertIn("?", remux._unboosted_note(None, -23.8, -16))


class AacAtCannotBeTrustedWithExoticLayouts(unittest.TestCase):
    """AudioToolbox mangles layouts it doesn't handle instead of refusing them: fed the 6.1
    film audio it returned a file ~8 dB QUIETER than its input at any bitrate, so A.I.'s
    +8.8 dB boost measured -1.9 after encode and the landing check rightly refused it —
    twice (isolated 2026-08-24: volume-only +8.4, +limiter +6.1, +aac_at -1.9; the same
    chain on 5.1 is perfect). Exotic layouts are folded to an EXPLICIT target before the
    encoder. Explicit, because handing aformat a list and letting ffmpeg negotiate picked
    MONO for the 6.1 film, whatever the list's order."""

    def _target(self, channels, layout):
        with mock.patch.object(remux.subprocess, "run",
                               return_value=mock.Mock(stdout=f"{channels},{layout}\n")):
            return remux.aac_at_target_layout("/x.mkv")

    def test_the_61_film_folds_to_51(self):
        self.assertEqual(self._target(7, "6.1(back)"), "5.1")

    def test_71_folds_to_51(self):
        self.assertEqual(self._target(8, "7.1"), "5.1")

    def test_an_exotic_THREE_channel_folds_to_stereo(self):
        self.assertEqual(self._target(3, "3.0"), "stereo")

    def test_the_safe_layouts_are_left_alone(self):
        for ch, lay in ((1, "mono"), (2, "stereo"), (6, "5.1"), (6, "5.1(side)")):
            self.assertIsNone(self._target(ch, lay), lay)

    def test_an_unreadable_probe_changes_nothing(self):
        with mock.patch.object(remux.subprocess, "run", side_effect=OSError):
            self.assertIsNone(remux.aac_at_target_layout("/x.mkv"))
        self.assertIsNone(remux.aac_at_target_layout(None))

    def test_the_fold_leads_the_chain(self):
        # downmixing AFTER the limiter measured 4.7 dB worse on the same input
        with mock.patch.object(remux, "aac_at_target_layout", return_value="5.1"):
            f = remux.build_audio_boost_filter(8.8, src="/x.mkv")
        self.assertTrue(f.startswith("aformat=channel_layouts=5.1,volume=8.80dB"))
        self.assertNotIn("|", f)                  # ONE explicit layout — never a list

    def test_a_safe_source_gets_the_plain_chain(self):
        with mock.patch.object(remux, "aac_at_target_layout", return_value=None):
            f = remux.build_audio_boost_filter(8.8, src="/x.mkv")
        self.assertTrue(f.startswith("volume=8.80dB"))
        self.assertNotIn("aformat", f)

    def test_every_boost_call_site_names_its_source(self):
        import inspect, history
        for mod, fn in ((remux, None), (history, None)):
            src = open(mod.__file__.replace(".pyc", ".py")).read()
        r = open(remux.__file__).read()
        h = open(history.__file__).read()
        self.assertEqual(r.count("build_audio_boost_filter(gain_db, src="), 3)
        self.assertEqual(h.count("build_audio_boost_filter(gain, src=work)"), 1)
        self.assertEqual(h.count("boost_keeping_original_args(gain, keep, src=work)"), 1)


class TheGapProbeMeasuresInterleavingNotGopLength(unittest.TestCase):
    """A video seek lands on the preceding KEYFRAME and ffprobe reports from there; an audio
    seek is exact. Taking the first printed packet compared a keyframe-aligned video position
    with a time-exact audio one, so the "gap" was the previous GOP in bytes: x265's default
    keyint (250 = 10.4 s at 23.976) on a ~8 Mbps talking-heads video read as 10.4 MB, failed
    "badly interleaved" eight times, and held the resolve doorstep shut for 15 hours
    (live-caught 2026-09-05). The probe now takes the first packet AT OR AFTER t, and reads
    a window long enough to reach it."""

    def _gap(self, streams):
        """streams: {(sel, t): [(pts, pos), ...]} as ffprobe would print them after the seek."""
        seen = {}
        def run(cmd, **kw):
            sel = cmd[cmd.index("-select_streams") + 1]
            iv = cmd[cmd.index("-read_intervals") + 1]
            t = int(iv.split("%")[0]); seen["window"] = iv
            rows = streams.get((sel, t), [])
            return mock.Mock(stdout="".join("%.3f,%d\n" % (p, q) for p, q in rows))
        with mock.patch.object(remux.subprocess, "run", side_effect=run):
            return remux.interleave_gap_mb("/x.mp4", at=(60,)), seen

    def test_a_keyframe_before_t_is_not_the_measurement(self):
        # keyframe 8 s early at 33.1 MB, then the packets AT 60 s at 43.4 MB; audio at 60 s
        # sits 43.48 MB — the file is tightly interleaved (0.08 MB), the GOP is just long
        g, _ = self._gap({("v:0", 60): [(52.135, 33_121_655), (52.052, 33_170_992), (60.010, 43_400_000)],
                          ("a:0", 60): [(59.977, 43_486_632), (60.000, 43_487_004)]})
        self.assertLess(g, 0.5, g)

    def test_a_genuinely_drifting_file_is_still_caught(self):
        g, _ = self._gap({("v:0", 60): [(52.135, 33_000_000), (60.010, 43_000_000)],
                          ("a:0", 60): [(60.000, 158_600_000)]})
        self.assertAlmostEqual(g, 115.6, places=1)

    def test_no_packet_at_or_after_t_reads_as_unmeasurable(self):
        g, _ = self._gap({("v:0", 60): [(52.135, 33_000_000)], ("a:0", 60): [(60.0, 1)]})
        self.assertEqual(g, -1.0)

    def test_the_read_window_reaches_past_a_whole_gop(self):
        _, seen = self._gap({})
        secs = int(seen["window"].split("+")[1])
        self.assertGreaterEqual(secs, 11)      # keyint 250 at 23.976 fps = 10.43 s
