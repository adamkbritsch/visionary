import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import remux
from remux import (build_tracks_command, build_master_mux_command,
                   parse_streams, has_dolby_vision, dolby_vision_profile, verify_remux,
                   needs_mkv, cfr_container_ext)

# Real ffprobe shape (captured from a dovi_tool-built Profile 8.1 file).
DOVI = {"side_data_type": "DOVI configuration record", "dv_profile": 8,
        "dv_bl_signal_compatibility_id": 1, "rpu_present_flag": 1,
        "bl_present_flag": 1, "el_present_flag": 0}
SAMPLE = json.dumps({"streams": [
    {"codec_type": "video", "codec_name": "hevc", "side_data_list": [DOVI]},
    {"codec_type": "audio", "codec_name": "eac3"},
    {"codec_type": "audio", "codec_name": "aac"},
    {"codec_type": "subtitle", "codec_name": "subrip"},
]})


def fake_mux(seen=None, reason="DV 8.1 · 1 audio · 1 sub"):
    """remux._mux_verified stand-in: the .part appears (so atomic_ship renames it for real) and
    passes. `seen` collects each call's (es, fps, tracks, kwargs)."""
    def run(es, fps, tracks, output, **kw):
        if seen is not None:
            seen.append(dict(kw, es=es, fps=fps, tracks=tracks))
        with open(remux.part_path(output), "w") as f:
            f.write("mkv")
        return remux.RemuxResult(True, output, "8.1", 1, 1, reason)
    return run


TRACKS_OK = (True, "", "", 0)   # remux._prepare_tracks: built, no audio note, no failure, no sync


class BuildCommands(unittest.TestCase):
    def test_tracks_take_audio_from_cfr_subs_from_original(self):
        # Audio comes from the CFR file (input 0), subs from the ORIGINAL (input 1), into a
        # MATROSKA tracks file — never any video.
        cmd = build_tracks_command("/ff", "/cfr.mp4", "/orig.mkv", "/t.mka",
                                   subs=[(0, "copy"), (2, "srt")])
        inputs = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-i"]
        self.assertEqual(inputs, ["/cfr.mp4", "/orig.mkv"])
        self.assertIn("0:a", cmd)        # all audio from input 0 (the CFR file)
        self.assertNotIn("0:v", cmd)     # never copies video
        self.assertEqual(cmd[-3:], ["-f", "matroska", "/t.mka"])
        # -fix_sub_duration is a RETRY only: it swallows the LAST line of every decoded track
        self.assertNotIn("-fix_sub_duration", cmd)
        fixed = build_tracks_command("/ff", "/cfr.mp4", "/orig.mkv", "/t.mka",
                                     subs=[(0, "srt")], fix_sub_duration=True)
        # ...and when used it guards the ORIGINAL input: BEFORE the second -i, an input option
        second_i = [i for i, x in enumerate(fixed) if x == "-i"][1]
        self.assertEqual(fixed.index("-fix_sub_duration"), second_i - 1)

    def test_each_subtitle_is_mapped_with_its_own_codec(self):
        # the n-th SOURCE subtitle, copied or converted per output stream: mov_text has no
        # Matroska codec ID, so it becomes SRT while PGS/ASS/SRT stay as they are
        cmd = build_tracks_command("/ff", "/c", "/o", "/t.mka", subs=[(0, "copy"), (2, "srt")])
        j = " ".join(cmd)
        self.assertIn("-map 1:s:0 -c:s:0 copy", j)
        self.assertIn("-map 1:s:2 -c:s:1 srt", j)
        self.assertIn("-map 1:t?", j)                  # the fonts those subtitles render with
        # the per-stream subtitle codecs come AFTER the blanket -c copy, so they win
        self.assertGreater(cmd.index("-c:s:1"), cmd.index("-c"))

    def test_no_subtitles_means_no_subtitle_or_font_maps(self):
        # Last-resort retry: an unconvertible subtitle track ships the master without subs
        # instead of parking the episode.
        for subs in (None, []):
            cmd = build_tracks_command("/ff", "/cfr.mp4", "/orig.mkv", "/t.mka", subs=subs)
            self.assertFalse([x for x in cmd if x.startswith("1:s") or x == "1:t?"])
            self.assertIn("0:a", cmd)                  # audio untouched

    def test_no_default_subtitle_and_no_release_title(self):
        cmd = build_tracks_command("/ff", "/c", "/o", "/t.mka", subs=[(0, "copy")])
        self.assertEqual(cmd[cmd.index("-disposition:s") + 1], "-default")   # forced is kept
        self.assertEqual(cmd[cmd.index("-default_mode") + 1], "infer_no_subs")
        # GLOBAL only: a bare -map_metadata -1 also strips stream/chapter tags (languages,
        # names, a font attachment's filename — without which the whole file fails)
        self.assertEqual(cmd[cmd.index("-map_metadata:g") + 1], "-1")
        self.assertNotIn("-map_metadata", cmd)

    def test_master_mux_is_mkvmerge_with_the_exact_rate(self):
        cmd = build_master_mux_command("/mkvmerge", "/s/v.hevc", "24000/1001", "/s/o.mkv.part",
                                       tracks="/s/t.mka", colour=["--colour-primaries", "0:9"],
                                       seed="o.mkv")
        self.assertEqual(cmd[0], "/mkvmerge")
        self.assertEqual(cmd[cmd.index("-o") + 1], "/s/o.mkv.part")
        self.assertEqual(cmd[cmd.index("--default-duration") + 1], "0:24000/1001p")
        self.assertEqual(cmd[cmd.index("--title") + 1], "")
        import zlib
        self.assertEqual(cmd[cmd.index("--deterministic") + 1], str(zlib.crc32(b"o.mkv")))
        self.assertEqual(cmd[-2:], ["/s/v.hevc", "/s/t.mka"])   # the ES is track 0, then the tracks
        self.assertIn("--colour-primaries", cmd)
        self.assertNotIn("dvp", " ".join(cmd))                  # no forced DV record: from the stream

    def test_master_mux_passes_every_path_absolute(self):
        # mkvmerge reads an argument starting with '+', '@', '=' or '(' as syntax: a relative
        # path like "+ Fire.hevc" would never be opened as a file
        cmd = build_master_mux_command("/m", "+ Fire.hevc", "25", "(x).mkv", tracks="@t.mka")
        for path in (cmd[cmd.index("-o") + 1], cmd[-2], cmd[-1]):
            self.assertTrue(os.path.isabs(path), path)

    def test_a_video_only_mux_has_no_tracks_input(self):
        cmd = build_master_mux_command("/m", "/v.hevc", "25/1", "/r.part")
        self.assertEqual(cmd[-1], "/v.hevc")

    def test_warnings_are_a_successful_mux(self):
        for rc, ok in ((0, True), (1, True), (2, False)):
            with mock.patch("subprocess.run", return_value=mock.Mock(returncode=rc, stdout="w",
                                                                     stderr="")):
                self.assertEqual(remux.run_mux(["/m"])[0], ok, rc)


class MuxSeedAndSync(unittest.TestCase):
    def test_a_name_starting_with_at_never_reaches_mkvmerge_as_an_option_file(self):
        cmd = build_master_mux_command("/m", "/v.hevc", "25", "/o.part", seed="@midnight S01E01.mkv")
        self.assertTrue(cmd[cmd.index("--deterministic") + 1].isdigit())

    def test_a_positive_shift_delays_the_video_a_negative_one_the_tracks(self):
        late = build_master_mux_command("/m", "/v.hevc", "25", "/o.part", tracks="/t.mka", sync_ms=21)
        self.assertEqual(late[late.index("--sync") + 1], "0:21")
        self.assertLess(late.index("--sync"), late.index("/v.hevc"))      # applies to the ES
        early = build_master_mux_command("/m", "/v.hevc", "25", "/o.part", tracks="/t.mka", sync_ms=-7)
        self.assertEqual(early[early.index("--sync") + 1], "-1:7")
        self.assertEqual(early[-1], "/t.mka")
        self.assertGreater(early.index("--sync"), early.index("/v.hevc"))  # applies to the tracks
        self.assertNotIn("--sync", build_master_mux_command("/m", "/v", "25", "/o", tracks="/t"))

    def test_the_shift_is_measured_on_a_copied_track_against_the_picture(self):
        pts = {("/t.mka", "a:0"): 0.0, ("/c.mp4", "a:0"): -0.021333, ("/c.mp4", "v:0"): 0.0}
        with mock.patch.object(remux, "_first_pts", side_effect=lambda p, sel, fp=None: pts[(p, sel)]):
            self.assertEqual(remux.tracks_sync_ms("/c.mp4", "/t.mka", (0, 0)), 21)
        pts[("/t.mka", "a:0")] = -0.021333                  # nothing shifted: nothing to undo
        with mock.patch.object(remux, "_first_pts", side_effect=lambda p, sel, fp=None: pts[(p, sel)]):
            self.assertEqual(remux.tracks_sync_ms("/c.mp4", "/t.mka", (0, 0)), 0)
        with mock.patch.object(remux, "_first_pts", return_value=None):
            self.assertEqual(remux.tracks_sync_ms("/c.mp4", "/t.mka", (0, 0)), 0)


class SubtitlePlan(unittest.TestCase):
    def _plan(self, codecs):
        js = json.dumps({"streams": [{"codec_type": "video"}] + [
            {"codec_type": "subtitle", "codec_name": c} for c in codecs]})
        with mock.patch.object(remux, "_probe", return_value=js):
            return remux.subtitle_plan("/orig")

    def test_copy_what_matroska_holds_convert_mov_text_drop_the_rest(self):
        self.assertEqual(self._plan(["subrip", "mov_text", "eia_608", "hdmv_pgs_subtitle",
                                     "webvtt", "ass"]),
                         [(0, "copy"), (1, "srt"), (3, "copy"), (4, "srt"), (5, "copy")])

    def test_none_or_unreadable(self):
        self.assertEqual(self._plan([]), [])
        with mock.patch.object(remux, "_probe", return_value="{}"):
            self.assertEqual(remux.subtitle_plan("/x"), [])


class TracksRetryLadder(unittest.TestCase):
    """_prepare_tracks: subtitles as planned; a failure that converts any retries with
    -fix_sub_duration (it costs each converted track its last line, so never first); then the
    master ships without subtitles rather than parking."""

    def _go(self, outcomes, plan):
        calls = []
        def run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=outcomes[len(calls) - 1], stderr="boom")
        with mock.patch.object(remux, "atmos_audio_index", return_value=None), \
             mock.patch.object(remux, "subtitle_plan", return_value=plan), \
             mock.patch.object(remux, "tracks_sync_ms", return_value=0), \
             mock.patch.object(remux, "cut_overlap_packets", return_value=([], 0.0)), \
             mock.patch.object(remux.subprocess, "run", side_effect=run):
            res = remux._prepare_tracks("/c", "/o", "/t.mka")
        return res, calls

    def test_a_clean_build_is_one_pass_without_the_fix(self):
        res, calls = self._go([0], [(0, "srt")])
        self.assertEqual(res, (True, "", "", 0))
        self.assertNotIn("-fix_sub_duration", calls[0])

    def test_a_failed_conversion_retries_with_the_fix_then_ships_with_subs(self):
        res, calls = self._go([1, 0], [(0, "srt")])
        self.assertTrue(res[0])
        self.assertIn("durations repaired", res[1])
        self.assertIn("-fix_sub_duration", calls[1])
        self.assertIn("1:s:0", calls[1])

    def test_then_without_subtitles(self):
        res, calls = self._go([1, 1, 0], [(0, "srt")])
        self.assertTrue(res[0])
        self.assertIn("subs dropped", res[1])
        self.assertFalse([x for x in calls[2] if x.startswith("1:s")])

    def test_copied_tracks_skip_the_fix_rung(self):
        res, calls = self._go([1, 0], [(0, "copy")])
        self.assertEqual(len(calls), 2)
        self.assertNotIn("-fix_sub_duration", calls[1])
        self.assertIn("subs dropped", res[1])

    def test_nothing_works_is_a_failure(self):
        res, _ = self._go([1, 1, 1], [(0, "srt")])
        self.assertFalse(res[0])
        self.assertIn("tracks failed", res[2])


class ColourArgs(unittest.TestCase):
    MD = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"

    def test_hdr10_from_the_same_values_the_sei_carries(self):
        a = " ".join(remux.mkv_colour_args(False, self.MD, "1000,400"))
        for want in ("--colour-primaries 0:9", "--colour-transfer-characteristics 0:16",
                     "--colour-matrix-coefficients 0:9", "--colour-range 0:1",
                     "--chromaticity-coordinates 0:0.68,0.32,0.265,0.69,0.15,0.06",
                     "--white-colour-coordinates 0:0.3127,0.329", "--max-luminance 0:1000",
                     "--min-luminance 0:0.005", "--max-content-light 0:1000",
                     "--max-frame-light 0:400"):
            self.assertIn(want, a)

    def test_nothing_invented_when_unknown(self):
        a = " ".join(remux.mkv_colour_args(False, None, None))
        self.assertIn("--colour-transfer-characteristics 0:16", a)
        for absent in ("chromaticity", "luminance", "content-light"):
            self.assertNotIn(absent, a)
        self.assertNotIn("content-light", " ".join(remux.mkv_colour_args(False, None, "0,0")))

    def test_sdr_is_bt709(self):
        a = " ".join(remux.mkv_colour_args(True, self.MD, "1000,400"))
        self.assertIn("--colour-primaries 0:1", a)
        self.assertIn("--colour-transfer-characteristics 0:1", a)
        self.assertNotIn("luminance", a)

    def test_parse_rejects_garbage(self):
        self.assertIsNone(remux.parse_x265_master_display("nope"))
        self.assertIsNone(remux.parse_x265_master_display(None))


class CfrContainerDecision(unittest.TestCase):
    """The CFR INTERMEDIATE: MP4 by default, MKV only for content an MP4 can't hold (lossless
    audio / bitmap subs). The master is always MKV — this decides nothing about it."""
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
        # AAC / AC3 / E-AC3 / DTS-core / DTS-HD HRA (lossy) + text subs → an MP4 CFR.
        streams = [{"codec_type": "audio", "codec_name": "aac", "profile": "LC"},
                   {"codec_type": "audio", "codec_name": "ac3"},
                   {"codec_type": "audio", "codec_name": "eac3"},
                   {"codec_type": "audio", "codec_name": "dts", "profile": "DTS"},
                   {"codec_type": "audio", "codec_name": "dts", "profile": "DTS-HD HRA"},
                   {"codec_type": "subtitle", "codec_name": "subrip"}]
        self.assertFalse(needs_mkv(self._probe(streams)))

    def test_pgs_with_aac_forces_mkv(self):
        # The real Hairspray case: AAC audio + PGS subs → an MKV CFR.
        streams = [{"codec_type": "audio", "codec_name": "aac", "profile": "LC"},
                   {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"}]
        self.assertTrue(needs_mkv(self._probe(streams)))

    def test_cfr_ext_defaults_mp4_when_unprobeable(self):
        # A missing/unprobeable source (e.g. not downloaded yet) → safe '.mp4' default.
        self.assertEqual(cfr_container_ext("/no/such/file.mkv"), ".mp4")


class EveryMasterIsMatroska(unittest.TestCase):
    def test_a_non_mkv_output_is_refused_before_anything_runs(self):
        with mock.patch("subprocess.run") as run, mock.patch.object(remux.dvcap, "probe_video") as pv:
            for fn, args in ((remux.remux, ("/r", "/c", "/o", "/out.mp4")),
                             (remux.remux_inject, ("/r", "/c", "/o", "/out.mp4")),
                             (remux.remux_ship_render, ("/r", "/c", "/o", "/out.mp4"))):
                res = fn(*args)
                self.assertFalse(res.ok)
                self.assertIn("Matroska", res.reason)
            run.assert_not_called()
            pv.assert_not_called()


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

    def test_verify_demands_exactly_8_1_single_layer(self):
        # mkvmerge DERIVES the record (MP4Box forced it): an 8.4 / 8.2 / dual-layer result fails
        for change in ({"dv_bl_signal_compatibility_id": 4}, {"dv_bl_signal_compatibility_id": 2},
                       {"dv_profile": 7, "el_present_flag": 1}, {"el_present_flag": 1},
                       {"rpu_present_flag": 0}):
            js = json.dumps({"streams": [
                {"codec_type": "video", "side_data_list": [dict(DOVI, **change)]},
                {"codec_type": "audio", "codec_name": "aac"}]})
            ok, reason = verify_remux(js)
            self.assertFalse(ok, change)
            self.assertIn("8.1", reason)

    def test_an_sdr_master_must_not_claim_dolby_vision(self):
        ok, reason = verify_remux(SAMPLE, require_dv=False)
        self.assertFalse(ok)
        self.assertIn("SDR", reason)
        plain = json.dumps({"streams": [{"codec_type": "video", "codec_name": "hevc"},
                                        {"codec_type": "audio", "codec_name": "aac"}]})
        self.assertTrue(verify_remux(plain, require_dv=False)[0])

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
             mock.patch.object(remux, "_mux_verified", side_effect=fake_mux(reason="DV 8.1 · 1 audio · 1 sub")), \
             mock.patch.object(remux, "_prepare_tracks", return_value=TRACKS_OK), \
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
        self.assertIn("peak 45.0 ≤ 57.5 Mbps, the 50 cap's gate", res.reason)

    def test_over_gate_repairs_offending_segment_then_ships(self):
        import tempfile
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            res = self._run(tmp, [{1: 58.6}, {1: 40.0}], calls)   # over → repaired → clean
        self.assertTrue(res.ok)
        self.assertEqual(calls, [([0], 42)])              # only the hot segment, at 85% of 50
        self.assertIn("peak repair: 1 seg(s) re-capped @ 42 Mbps", res.reason)
        self.assertIn("peak 40.0 ≤ 57.5 Mbps, the 50 cap's gate", res.reason)

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
                 mock.patch.object(remux, "_mux_verified", side_effect=fake_mux(reason="ok")), \
             mock.patch.object(remux, "_prepare_tracks", return_value=TRACKS_OK), \
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
                 mock.patch.object(remux, "_mux_verified", side_effect=fake_mux(reason="ok")), \
             mock.patch.object(remux, "_prepare_tracks", return_value=TRACKS_OK), \
                 mock.patch.object(remux.subprocess, "run", return_value=ran):
                res = remux.remux(render, "cfr.mkv", "orig.mkv", out)
        self.assertTrue(res.ok)
        self.assertEqual(seen["enc"], [render])


def splice_mocks(n=100, units=None):
    """The inject path's RPU seams (splice.donor_rpus / place_rpus / container_ranks) as a test of
    the orchestration sees them: n distinct RPUs, a placement that writes the injected ES, a
    master whose timestamps put every frame in its slot."""
    import splice
    units = units if units is not None else [bytes([0x19, 1 + i % 250, i // 250]) for i in range(n)]

    def place(src, rpus, out, **kw):
        with open(out, "wb") as f:
            f.write(b"INJECTED")
        return type("Ix", (), {"rank": list(range(len(rpus)))})()
    return [mock.patch.object(splice, "donor_rpus", return_value=(units, "test")),
            mock.patch.object(splice, "place_rpus", side_effect=place),
            mock.patch.object(splice, "container_ranks", return_value=list(range(n)))]


class _Patches:
    def __init__(self, patches):
        self.p = patches

    def __enter__(self):
        for x in self.p:
            x.start()
        return self

    def __exit__(self, *exc):
        for x in self.p:
            x.stop()
        return False


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
                 mock.patch.object(remux.dvcap, "ensure_pq_vui", return_value=(True, "")), \
                 mock.patch.object(remux, "_mux_verified", side_effect=fake_mux(reason="DV 8.1 · 2 audio · 1 sub")), \
             mock.patch.object(remux, "_prepare_tracks", return_value=TRACKS_OK), \
                 mock.patch.object(remux.subprocess, "run", side_effect=self._fake_run), \
                 _Patches(splice_mocks()):
                res = remux.remux_inject(os.path.join(tmp, "dv.mov"), "cfr.mkv", "orig.mkv", out)
            self.assertTrue(res.ok)
            self.assertIn("original stream + injected RPU", res.reason)
            self.assertIn("every frame's RPU checked", res.reason)
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
                 _Patches(splice_mocks(99)), \
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
                 mock.patch.object(remux.dvcap, "ensure_pq_vui", return_value=(True, "")), \
                 mock.patch.object(remux.dvcap, "extract_rpu", return_value=(True, "ok")), \
                 mock.patch.object(remux.dvcap, "rpu_frame_count", return_value=100), \
                 _Patches(splice_mocks()), \
                 mock.patch.object(remux.subprocess, "run", side_effect=self._fake_run):
                res = remux.remux_inject(os.path.join(tmp, "dv.mov"), "cfr.mkv", "orig.mkv", out)
            self.assertFalse(res.ok)
            self.assertIn("frame count changed", res.reason)
            self.assertFalse(os.path.exists(out + ".inject.hevc"))   # transients swept on failure too


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
                                          "/tmp/out.mkv", cap_mbps=50)
        self.assertFalse(res.ok)
        self.assertTrue(res.reason.startswith("render-over-cap"))
        self.assertIn("81.2", res.reason)

    def test_the_note_names_the_gate_a_render_passed(self):
        # a 53.7 peak shipped under the 50 cap's 57.5 gate read "53.7 ≤ 50" in the log (2026-10-20)
        import tempfile, os
        ran = type("R", (), {"returncode": 0, "stderr": ""})()

        def fake_run(cmd, **kw):
            if cmd[-1].endswith(".ship.hevc"):
                with open(cmd[-1], "wb") as f:
                    f.write(b"es")
            return ran
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(remux.dvcap, "probe_video", return_value={"frames": 100, "fps": "25"}), \
             mock.patch.object(remux.dvcap, "video_peak_1s_mbps", return_value=53.7), \
             mock.patch.object(remux.dvcap, "ensure_pq_vui", return_value=(True, "")), \
             mock.patch.object(remux, "_mux_verified", side_effect=fake_mux([], reason="DV 8.1")), \
             mock.patch.object(remux, "_prepare_tracks", return_value=TRACKS_OK), \
             mock.patch.object(remux.subprocess, "run", side_effect=fake_run):
            res = remux.remux_ship_render("/r.mov", "/c.mp4", "/o.mp4", os.path.join(tmp, "o.mkv"),
                                          cap_mbps=50)
        self.assertTrue(res.ok, res.reason)
        self.assertIn("peak 53.7 ≤ 57.5 Mbps, the 50 cap's gate", res.reason)

    def test_unprobeable_render_fails_without_the_fallback_reason(self):
        import dvcap
        with mock.patch.object(dvcap, "probe_video", return_value={"frames": 0, "fps": ""}):
            res = remux.remux_ship_render("/dv.mov", "/cfr.mp4", "/orig.mp4",
                                          "/tmp/out.mkv", cap_mbps=50)
        self.assertFalse(res.ok)
        self.assertFalse(res.reason.startswith("render-over-cap"))   # a real failure retries


class StagePlans(unittest.TestCase):
    """The fast remuxes announce their lettered stages (on_stages) before the first step, and
    every step they then report is in that plan, in its order (the app letters them A, B, C)."""

    def _inject(self, tmp, steps, plans, **kw):
        import os
        out = os.path.join(tmp, "master.mkv")
        with mock.patch.object(remux.dvcap, "probe_video",
                               side_effect=[InjectPath.R_INFO, InjectPath.S_INFO]), \
             mock.patch.object(remux.dvcap, "count_hevc_frames", side_effect=[100, 100]), \
             mock.patch.object(remux.dvcap, "extract_rpu", return_value=(True, "ok")), \
             mock.patch.object(remux.dvcap, "rpu_frame_count", return_value=100), \
             mock.patch.object(remux.dvcap, "ensure_pq_vui", return_value=(True, "")), \
             mock.patch.object(remux, "_mux_verified", side_effect=fake_mux(reason="DV 8.1")), \
             mock.patch.object(remux, "_prepare_tracks", return_value=TRACKS_OK), \
             mock.patch.object(remux.subprocess, "run", side_effect=InjectPath()._fake_run), \
             _Patches(splice_mocks()):
            return remux.remux_inject(os.path.join(tmp, "dv.mov"), "cfr.mkv", "orig.mkv", out,
                                      on_step=lambda l, p: steps.append(l),
                                      on_stages=lambda ls: plans.append(list(ls)), **kw)

    def assertInPlanOrder(self, steps, plan):
        at = [plan.index(s) for s in steps]
        self.assertEqual(at, sorted(at), (steps, plan))

    def test_inject_announces_its_steps_first(self):
        import tempfile
        steps, plans = [], []
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(self._inject(tmp, steps, plans).ok)
        self.assertEqual(plans, [remux.inject_stages(True)])
        self.assertEqual(steps[0], "extracting DV metadata")
        self.assertInPlanOrder(steps, plans[0])

    def test_a_cached_rpu_is_not_a_stage(self):
        import os, tempfile
        steps, plans = [], []
        with tempfile.TemporaryDirectory() as tmp:
            seg = os.path.join(tmp, "master.mkv.remuxsegs")
            with mock.patch.object(remux.dvcap, "ensure_segdir"):
                os.makedirs(seg)
                import splice
                splice.write_rpu_file([bytes([0x19, 1 + i % 250, i // 250]) for i in range(100)],
                                      os.path.join(seg, "rpu.bin"))
                self.assertTrue(self._inject(tmp, steps, plans).ok)
        self.assertEqual(plans, [remux.inject_stages(False)])
        self.assertNotIn("extracting DV metadata", plans[0] + steps)
        self.assertEqual(plans[0][0], "copying the original video")     # Stage A

    def test_skip_and_convert_plans(self):
        self.assertEqual(remux.inject_stages(False, skip_inject=True, convert_es=True)[:2],
                         ["copying the original video", "converting DV to profile 8.1"])
        self.assertEqual(remux.inject_stages(True, skip_inject=True)[:2],
                         ["copying the original video", "preparing audio"])

    def test_the_mux_steps_are_the_ones_mux_verified_reports(self):
        import inspect
        src = inspect.getsource(remux._mux_verified) + inspect.getsource(remux.remux_inject)
        for label in remux.MUX_STAGES + ("copying the original video", "injecting DV metadata",
                                          "extracting DV metadata", "converting DV to profile 8.1"):
            self.assertIn(f'"{label}"', src)
        ship = inspect.getsource(remux.remux_ship_render)
        for label in remux.SHIP_STAGES[:2]:
            self.assertIn(f'"{label}"', ship)

    def test_ship_render_announces_then_measures(self):
        import dvcap
        steps, plans = [], []
        with mock.patch.object(dvcap, "probe_video",
                               return_value={"frames": 100, "fps": "24000/1001"}), \
             mock.patch.object(dvcap, "video_peak_1s_mbps", return_value=81.2):
            res = remux.remux_ship_render("/dv.mov", "/cfr.mp4", "/orig.mp4", "/tmp/out.mkv",
                                          cap_mbps=50, on_step=lambda l, p: steps.append(l),
                                          on_stages=lambda ls: plans.append(list(ls)))
        self.assertFalse(res.ok)
        self.assertEqual(plans, [list(remux.SHIP_STAGES)])
        self.assertEqual(steps, ["measuring the render's peaks"])


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
             mock.patch.object(remux.dvcap, "ensure_pq_vui", return_value=(True, "")), \
             mock.patch.object(remux, "_mux_verified", side_effect=fake_mux(reason="DV 8.1")), \
             mock.patch.object(remux, "_prepare_tracks", return_value=TRACKS_OK), \
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
    """The old MKV branch skipped the loudness boost outright because Matroska is where LOSSLESS
    audio lives — but the rule is about the CODEC, not the container, and most masters carry AAC
    or AC3. A 4K master with AAC 5.1 shipped simply quiet (user-caught 2026-08-18). Every master
    is Matroska now; the tracks builder boosts whatever is lossy."""

    def test_lossless_codecs_are_identified(self):
        for c, p in (("truehd", ""), ("mlp", ""), ("flac", ""), ("alac", ""),
                     ("pcm_s24le", ""), ("dts", "DTS-HD MA")):
            self.assertTrue(remux.is_lossless_audio_codec(c, p), c)

    def test_lossy_codecs_are_not(self):
        for c, p in (("aac", "LC"), ("aac", "HE-AAC"), ("ac3", ""), ("eac3", ""),
                     ("dts", "DTS"), ("opus", "")):
            self.assertFalse(remux.is_lossless_audio_codec(c, p), c)

    def test_a_gain_re_encodes_only_the_audio(self):
        c = remux.build_tracks_command("/ff", "/a.mkv", "/o.mkv", "/t.mka", gain_db=6.0)
        self.assertEqual(c[c.index("-c") + 1], "copy")          # video + subs untouched (DV survives)
        self.assertEqual(c[c.index("-c:a") + 1], "aac_at")
        self.assertIn("-filter:a", c)

    def test_no_gain_is_a_pure_copy_mux(self):
        c = remux.build_tracks_command("/ff", "/a.mkv", "/o.mkv", "/t.mka")
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
        return remux.build_tracks_command("/ff", "/cfr.mkv", "/orig.mkv", "/t.mka",
                                          gain_db=gain, keep_original_audio=keep)

    def test_the_boosted_copy_comes_first(self):
        c = self._cmd()
        self.assertLess(c.index("0:a:0"), c.index("0:a"))     # map order decides track order

    def test_every_original_track_is_still_there(self):
        self.assertIn("0:a", self._cmd())

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
        c = remux.build_tracks_command("/ff", "/c", "/o", "/t.mka", gain_db=6.0)
        self.assertIn("-c:a", c)
        self.assertNotIn("0:a:0", c)
        self.assertNotIn("-disposition:a:0", c)

    def test_no_gain_means_no_encoder_and_no_duplicate_track(self):
        c = remux.build_tracks_command("/ff", "/c", "/o", "/t.mka",
                                        gain_db=0.0, keep_original_audio=2)
        self.assertNotIn("aac_at", c)
        self.assertNotIn("0:a:0", c)

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
        c = remux.build_tracks_command("/ff", "/cfr", "/orig", "/t.mka",
                                        gain_db=10.9, atmos_lead=2, n_audio=4)
        self.assertNotIn("aac_at", c)              # nothing re-encoded
        self.assertNotIn("-filter:a", c)           # no gain applied
        self.assertEqual(c[c.index("-c") + 1], "copy")
        self.assertLess(c.index("0:a:2"), c.index("0:a:0"))       # Atmos first
        self.assertEqual(c[c.index("-disposition:a:0") + 1], "default")

    def test_atmos_outranks_keeping_a_lossless_original(self):
        # a TrueHD Atmos bed is BOTH — untouched wins over boost-and-keep
        c = remux.build_tracks_command("/ff", "/cfr", "/orig", "/t.mka",
                                        gain_db=10.9, keep_original_audio=2,
                                        atmos_lead=0, n_audio=2)
        self.assertNotIn("aac_at", c)

    def test_a_non_atmos_title_is_completely_unchanged(self):
        c = remux.build_tracks_command("/ff", "/cfr", "/orig", "/t.mka", gain_db=10.9)
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
    picture and sound together (an MP4Box mux's 500 ms interleave window, live-caught
    2026-08-22). mkvmerge orders every track's blocks by timestamp, so a Matroska master should
    never come near the limit — the gate stays, measured on the file that ships, as the check
    that would catch a muxer that does."""

    def _gap(self, positions):
        """positions: {(stream, t): byte_pos}"""
        def run(cmd, **kw):
            sel = cmd[cmd.index("-select_streams") + 1]
            t = int(cmd[cmd.index("-read_intervals") + 1].split("%")[0])
            p = positions.get((sel, t))
            return mock.Mock(stdout="" if p is None else "%d.0,%d\n" % (t, p))
        with mock.patch.object(remux.subprocess, "run", side_effect=run):
            return remux.interleave_gap_mb("/x.mkv", at=(60, 300))

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
        res = remux.RemuxResult(True, "/o.mkv")
        with mock.patch.object(remux, "interleave_gap_mb", return_value=115.6), \
             mock.patch.object(remux, "subtitle_gap_mb", return_value=(0.0, 0.0)), \
             mock.patch.object(remux, "_rm") as rm:
            out = remux._gate_interleave(res, "/o.mkv.part")
        self.assertFalse(out.ok)
        self.assertIn("115.6 MB", out.reason)
        rm.assert_called_once_with("/o.mkv.part")          # never leave a bad master on disk

    def test_the_gate_passes_a_tight_master(self):
        res = remux.RemuxResult(True, "/o.mkv")
        with mock.patch.object(remux, "interleave_gap_mb", return_value=0.03), \
             mock.patch.object(remux, "subtitle_gap_mb", return_value=(0.4, 0.2)), \
             mock.patch.object(remux, "_rm") as rm:
            self.assertTrue(remux._gate_interleave(res, "/o.mkv.part").ok)
        rm.assert_not_called()

    def test_an_unmeasurable_gap_passes_rather_than_parking_the_item(self):
        res = remux.RemuxResult(True, "/o.mkv")
        with mock.patch.object(remux, "interleave_gap_mb", return_value=-1.0), \
             mock.patch.object(remux, "subtitle_gap_mb", return_value=(-1.0, -1.0)), \
             mock.patch.object(remux, "_rm"):
            self.assertTrue(remux._gate_interleave(res, "/o.mkv.part").ok)

    def test_the_threshold_sits_between_what_shipped_and_what_works(self):
        # 8.4 MB was already stalling; 0.03 MB is the fixed mux. The gate has to split them.
        self.assertGreater(remux.MAX_INTERLEAVE_GAP_MB, 0.03)
        self.assertLessEqual(remux.MAX_INTERLEAVE_GAP_MB, 8.4)


class SubtitlesSitWithTheirPicture(unittest.TestCase):
    """2026-10-18: an MP4Box mux wrote every subtitle of an episode into the first ~150 MB while
    the picture ran to 2.5 GB; the SHIELD stuttered seeking back for each one. The audio gate
    never looked at subtitles. They are measured on the file that ships, and a master whose
    subtitles sit too far from their picture is refused (mkvmerge places them by timestamp)."""

    def test_distance_is_measured_against_the_picture_at_the_same_instant(self):
        video = [(t / 24, int(t / 24 * 1_412_000)) for t in range(0, 24 * 1001)]   # ~11 Mbps
        subs = [(1.0, 998_338, 37), (500.2, 42_000_000, 20), (999.0, 120_000_000, 54)]
        worst, p99 = remux.subtitle_gaps(video, subs)
        self.assertAlmostEqual(worst, 1290.6, delta=1.0)     # the 999 s line ~1.29 GB early
        self.assertGreater(p99, remux.MAX_SUBTITLE_GAP_MB)

    def test_an_interleaved_track_measures_small(self):
        video = [(float(t), t * 1_460_000) for t in range(0, 1500, 2)]
        subs = [(float(t) + 0.3, t * 1_460_000 + 400_000, 30) for t in range(5, 1500, 7)]
        worst, p99 = remux.subtitle_gaps(video, subs)
        self.assertLess(worst, 4.0)
        self.assertLess(p99, remux.MAX_SUBTITLE_GAP_MB)

    def test_ffmpegs_empty_end_sample_at_the_end_of_the_file_does_not_count(self):
        video = [(1400 + t / 24, 1_750_000_000 + int(t / 24 * 375_000)) for t in range(0, 24 * 75)]
        subs = [(1424.4, 1_759_150_000, 40), (1427.3, 1_778_037_298, 2)]   # S06E03's end marker at EOF-2
        worst, _p99 = remux.subtitle_gaps(video, subs)
        self.assertLess(worst, 2.0)

    def test_no_subtitles_is_zero_and_no_video_is_unmeasurable(self):
        self.assertEqual(remux.subtitle_gaps([(0.0, 1)], []), (0.0, 0.0))
        self.assertEqual(remux.subtitle_gaps([], [(1.0, 5, 10)]), (-1.0, -1.0))

    def test_the_gate_refuses_misplaced_subtitles_and_removes_the_master(self):
        res = remux.RemuxResult(True, "/o.mkv")
        with mock.patch.object(remux, "interleave_gap_mb", return_value=0.1), \
             mock.patch.object(remux, "subtitle_gap_mb", return_value=(2314.0, 1900.0)), \
             mock.patch.object(remux, "_rm") as rm:
            out = remux._gate_interleave(res, "/o.mkv.part")
        self.assertFalse(out.ok)
        self.assertIn("2314 MB", out.reason)
        rm.assert_called_once_with("/o.mkv.part")

    def test_one_far_subtitle_is_enough(self):
        res = remux.RemuxResult(True, "/o.mkv")
        with mock.patch.object(remux, "interleave_gap_mb", return_value=0.1), \
             mock.patch.object(remux, "subtitle_gap_mb",
                               return_value=(remux.MAX_SUBTITLE_WORST_MB + 1, 0.5)), \
             mock.patch.object(remux, "_rm"):
            self.assertFalse(remux._gate_interleave(res, "/o.mkv.part").ok)


@unittest.skipUnless(all(os.path.exists(t) for t in (remux.FFMPEG, remux.FFPROBE, remux.MKVMERGE)),
                     "ffmpeg/ffprobe/mkvmerge not installed")
class MkvMasterForReal(unittest.TestCase):
    """A real master built the way remux builds one — the tracks file (audio + an SRT and a
    mov_text subtitle, through build_tracks_command / subtitle_plan) muxed by mkvmerge with a raw
    HEVC ES — passes both interleave gates, keeps every subtitle line, converts the mov_text one
    to SRT, turns no subtitle on, and carries the exact frame count and rate."""

    def test_a_real_mkvmerge_master(self):
        d = tempfile.mkdtemp()
        try:
            j = lambda n: os.path.join(d, n)
            r = subprocess.run([remux.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                                "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24000/1001",
                                "-t", "40", "-c:v", "libx265", "-x265-params", "log-level=error",
                                "-b:v", "4M", "-f", "hevc", j("v.hevc")], capture_output=True)
            if r.returncode:
                self.skipTest("no libx265 here")
            with open(j("s.srt"), "w") as f:
                for i in range(1, 19):
                    f.write(f"{i}\n00:00:{i * 2:02d},000 --> 00:00:{i * 2 + 1:02d},000\nline {i}\n\n")
            # the "CFR": 40 s of audio; the "original": two subtitle tracks, one of them mov_text
            subprocess.run([remux.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                            "-i", "sine=f=440:d=40", "-c:a", "aac", j("cfr.mp4")], check=True)
            subprocess.run([remux.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                            "-i", "anullsrc=d=40", "-i", j("s.srt"), "-i", j("s.srt"), "-map", "0",
                            "-map", "1", "-map", "2", "-c:a", "aac", "-c:s", "mov_text",
                            j("orig.mp4")], check=True)
            plan = remux.subtitle_plan(j("orig.mp4"))
            self.assertEqual(plan, [(0, "srt"), (1, "srt")])
            r = subprocess.run(build_tracks_command(remux.FFMPEG, j("cfr.mp4"), j("orig.mp4"),
                                                    j("t.mka"), subs=plan), capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            ok, why = remux.run_mux(build_master_mux_command(remux.MKVMERGE, j("v.hevc"),
                                                             "24000/1001", j("o.mkv.part"),
                                                             tracks=j("t.mka"),
                                                             colour=remux.mkv_colour_args(True),
                                                             seed="o.mkv"))
            self.assertTrue(ok, why)
            res = remux._verify(j("o.mkv.part"), remux.FFPROBE, require_dv=False, frames=959,
                                output=j("o.mkv"))
            self.assertTrue(res.ok, res.reason)
            self.assertEqual(res.subtitle, 2)
            with mock.patch.object(remux, "MAX_SUBTITLE_GAP_MB", 1.0), \
                 mock.patch.object(remux, "MAX_SUBTITLE_WORST_MB", 2.0):   # a 20 MB clip, scaled limits
                self.assertTrue(remux._gate_interleave(res, j("o.mkv.part")).ok, res.reason)
            js = json.loads(subprocess.run([remux.FFPROBE, "-v", "error", "-show_streams", "-of",
                                            "json", j("o.mkv.part")], capture_output=True,
                                           text=True).stdout)
            v = [x for x in js["streams"] if x["codec_type"] == "video"][0]
            self.assertEqual(v["r_frame_rate"], "24000/1001")
            subs = [x for x in js["streams"] if x["codec_type"] == "subtitle"]
            self.assertEqual([x["codec_name"] for x in subs], ["subrip", "subrip"])
            self.assertFalse(any(x.get("disposition", {}).get("default") for x in subs))
            lines = remux._packets(j("o.mkv.part"), "s", size=True)
            self.assertEqual(len(lines), 36)                       # 18 lines x 2 tracks
        finally:
            shutil.rmtree(d, ignore_errors=True)


@unittest.skipUnless(all(os.path.exists(t) for t in (remux.FFMPEG, remux.FFPROBE, remux.MKVMERGE)),
                     "ffmpeg/ffprobe/mkvmerge not installed")
class MkvMasterKeepsWhatTheSourceSays(unittest.TestCase):
    """A real MKV original the way a WEB-DL / fansub release looks — Spanish AAC, an English FORCED
    SRT, a French ASS with its FONT attached, named chapters — through the real _prepare_tracks and
    mkvmerge: every language, the forced flag, the font, the chapter names and both subtitle tracks
    survive; no subtitle is default; no release title; and the audio keeps its place against the
    picture across the MP4 CFR's AAC priming (review-caught 2026-10-19: -map_metadata -1 dropped
    all of it, and the font failure took every subtitle with it)."""

    FONT = next((f for f in ("/System/Library/Fonts/Supplemental/Andale Mono.ttf",
                             "/Library/Fonts/AbrilFatface-Regular.ttf") if os.path.exists(f)), None)

    def test_languages_forced_font_chapters_and_sync(self):
        if not self.FONT:
            self.skipTest("no TTF font to attach")
        d = tempfile.mkdtemp()
        try:
            j = lambda n: os.path.join(d, n)
            run = lambda c: subprocess.run(c, capture_output=True, text=True, check=True)
            run([remux.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                 "-i", "testsrc2=size=320x180:rate=24000/1001", "-f", "lavfi", "-i", "sine=f=440",
                 "-t", "12", "-c:v", "libx264", "-c:a", "aac", "-shortest", j("av.mkv")])
            with open(j("en.srt"), "w") as f:
                f.write("1\n00:00:02,000 --> 00:00:03,000\nforced line\n\n"
                        "2\n00:00:06,000 --> 00:00:07,000\nsecond\n\n")
            with open(j("fr.ass"), "w") as f:
                f.write("[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\nFormat: Name, Fontname, "
                        "Fontsize\nStyle: Default,Andale Mono,20\n\n[Events]\nFormat: Layer, Start, End, "
                        "Style, Text\nDialogue: 0,0:00:04.00,0:00:05.00,Default,bonjour\n")
            with open(j("ch.txt"), "w") as f:
                f.write("CHAPTER01=00:00:00.000\nCHAPTER01NAME=Opening\n"
                        "CHAPTER02=00:00:06.000\nCHAPTER02NAME=Act One\n")
            run([remux.MKVMERGE, "-q", "-o", j("orig.mkv"), "--title", "Some.Release.1080p.WEB-DL",
                 "--chapters", j("ch.txt"), "--language", "1:spa", "--track-name", "1:Espanol 2.0",
                 j("av.mkv"), "--language", "0:eng", "--forced-display-flag", "0:yes",
                 "--default-track-flag", "0:no", j("en.srt"), "--language", "0:fre", j("fr.ass"),
                 "--attachment-mime-type", "font/ttf", "--attach-file", self.FONT])
            # the CFR: an MP4 stream copy, as topaz.build_cfr_copy_command makes it (AAC priming)
            run([remux.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", j("orig.mkv"),
                 "-map", "0:v:0", "-map", "0:a", "-c", "copy", j("cfr.mp4")])
            ok, note, why, sync = remux._prepare_tracks(j("cfr.mp4"), j("orig.mkv"), j("t.mka"))
            self.assertTrue(ok, why)
            self.assertNotIn("subs dropped", note)
            a0 = remux._first_pts(j("cfr.mp4"), "a:0")
            v0 = remux._first_pts(j("cfr.mp4"), "v:0")
            self.assertLess(a0, v0)                     # the AAC priming this test is about...
            self.assertGreater(sync, 0)                 # ...and the shift that undoes it
            run([remux.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                 "-i", "testsrc2=size=320x180:rate=24000/1001", "-t", "12", "-c:v", "libx265",
                 "-x265-params", "log-level=error", "-f", "hevc", j("v.hevc")])
            ok, why = remux.run_mux(build_master_mux_command(
                remux.MKVMERGE, j("v.hevc"), "24000/1001", j("o.mkv"), tracks=j("t.mka"),
                colour=remux.mkv_colour_args(True), seed="o.mkv", sync_ms=sync))
            self.assertTrue(ok, why)
            info = json.loads(run([remux.MKVMERGE, "-J", j("o.mkv")]).stdout)
            self.assertFalse(info["container"]["properties"].get("title"))      # no release name
            tr = {t["type"]: [] for t in info["tracks"]}
            for t in info["tracks"]:
                tr[t["type"]].append(t["properties"])
            self.assertEqual([a.get("language") for a in tr["audio"]], ["spa"])
            subs = tr["subtitles"]
            self.assertEqual([x.get("language") for x in subs], ["eng", "fre"])
            self.assertEqual([bool(x.get("forced_track")) for x in subs], [True, False])
            self.assertFalse(any(x.get("default_track") for x in subs))
            self.assertEqual([a["file_name"] for a in info["attachments"]],
                             [os.path.basename(self.FONT)])
            chapters = run([remux.FFPROBE, "-v", "error", "-show_chapters", "-of", "json",
                            j("o.mkv")]).stdout
            self.assertEqual([c["tags"]["title"] for c in json.loads(chapters)["chapters"]],
                             ["Opening", "Act One"])
            # the audio starts where it did against the picture in the CFR (a priming -21 ms)
            ma, mv = remux._first_pts(j("o.mkv"), "a:0"), remux._first_pts(j("o.mkv"), "v:0")
            self.assertAlmostEqual((ma - mv), (a0 - v0), delta=0.0015)
        finally:
            shutil.rmtree(d, ignore_errors=True)


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
        self.assertEqual(r.count("build_audio_boost_filter(gain_db, src="), 2)
        self.assertEqual(h.count("build_audio_boost_filter(gain, src=work)"), 1)
        self.assertEqual(h.count("boost_keeping_original_args(gain, keep, src=work)"), 1)


F = 1024 / 44100            # one AAC frame at 44.1 kHz


def _probe_answers(codec, pts):
    """subprocess.run stand-in for cut_overlap_packets: the codec probe, then the packet stamps."""
    out = iter([mock.Mock(stdout=codec + "\n"), mock.Mock(stdout="\n".join(f"{t:.6f}" for t in pts))])
    return lambda *a, **k: next(out)


class CutOverlapIsTheSquashOnly(unittest.TestCase):
    """cut_overlap_runs finds what yt-dlp's stream-copy cut leaves — a pile of packets stamped a tick
    apart that is MORE AUDIO THAN TIME — and nothing that merely shares a stamp: jitter of any shape
    nets out. Every shape below was measured on real cuts or real sources (review 2026-10-21): the
    first fix flagged clean VOB AC3 and every Vorbis track; the second missed 4-12 % of real joins
    whose chain swallowed a genuine packet at either end, and took mkvmerge's DVD jitter for a cut."""
    T = 1 / 44100                                   # the MP4 audio timebase tick

    def _check(self, pts, drops_expected):
        runs, frame = remux.cut_overlap_runs(pts)
        dropped = [k for a, b in runs for k in range(a, b + 1)]
        self.assertEqual(len(dropped), drops_expected, runs)
        kept = [t for k, t in enumerate(pts) if k not in set(dropped)]
        gaps = [y - x for x, y in zip(kept, kept[1:])]
        self.assertGreater(min(gaps), frame / 2)    # nothing left packed on top of anything
        return dropped

    def _shape(self, head_natural, n, u, before=20, after=20):
        """Stretch 1 ends on packet P; n pre-roll packets piled a tick apart, from one tick past P
        (head_natural=False) or from P + F (True); the next stretch's first packet at P + F + u."""
        pts = [i * F for i in range(before)]
        p = pts[-1]
        start = p + F if head_natural else p + self.T
        pts += [start + k * self.T for k in range(n)]
        pts += [p + F + u + i * F for i in range(after)]
        return pts

    def test_the_pile_after_the_earlier_stretch(self):
        d = self._check(self._shape(False, 68, 0.006), 68)
        self.assertEqual(d[0], 20)                   # starts right after the genuine last packet

    def test_the_pile_starting_a_frame_on(self):
        self._check(self._shape(True, 68, 0.006), 68)

    def test_a_long_pile_whose_ticks_add_up(self):  # 120 packets: 2.7 ms of ticks
        self._check(self._shape(False, 120, 0.004), 120)

    def test_the_next_stretch_landing_inside_the_pile(self):   # it chains in; still one join
        self._check(self._shape(True, 40, 0.0005), 40)
        self._check(self._shape(True, 40, 0.0011), 40)

    def test_matroska_millisecond_stamps(self):
        pts = [round(t, 3) for t in self._shape(True, 30, 0.006)]
        self._check(pts, 30)

    def test_a_cut_heavier_than_what_it_kept(self):  # the median of ALL gaps would be a tick
        pts = [i * F for i in range(30)]
        pts += [pts[-1] + F + k * self.T for k in range(200)] + [pts[-1] + F + 0.005 + i * F for i in range(30)]
        self._check(pts, 200)

    def test_dvd_jitter_nets_out(self):
        pts = [i * F for i in range(100)]
        pts[50] = 51 * F                             # one packet a frame late: gaps 2F, 0
        self.assertEqual(remux.cut_overlap_runs(pts)[0], [])

    def test_mkvmerges_dvd_jitter_nets_out(self):
        pts = [i * F for i in range(100)]
        pts[50], pts[51] = 51 * F, 52 * F            # gaps 2F, F, 0, F — the pair mkvmerge makes
        self.assertEqual(remux.cut_overlap_runs(pts)[0], [])
        pts = [i * F for i in range(100)]
        pts[50], pts[51], pts[52] = 51 * F, 52 * F, 53 * F   # three late in a row
        self.assertEqual(remux.cut_overlap_runs(pts)[0], [])

    def test_a_pair_is_jitter_whatever_surrounds_it(self):
        # third review (2026-10-21): each of these was taken for a cut by a looser rule
        step = [i * F for i in range(60)] + [(i - 1) * F for i in range(60, 120)]   # lasting step back
        late = [i * F for i in range(100)]
        for k in range(40, 60):                       # a stretch of 20 a frame late, then back
            late[k] += F
        alt = [i * F for i in range(100)]
        for k in range(40, 52, 2):                    # every other packet late, 12 packets
            alt[k] += F
        dts = [round(i * 512 / 48000, 3) for i in range(600)]
        dts[300] = round(301 * 512 / 48000, 3)        # DTS on whole ms, one packet a frame late
        for pts in (step, late, alt, dts):
            self.assertEqual(remux.cut_overlap_runs(pts)[0], [])

    def test_the_frame_survives_millisecond_rounding(self):
        dts = [round(i * 512 / 48000, 3) for i in range(600)]          # median gap 11, mean 10.67
        self.assertAlmostEqual(remux.cut_overlap_runs(dts)[1], 512 / 48000, delta=5e-5)

    def test_a_pile_that_ends_the_file(self):        # a last piece holding nothing but pre-roll
        self._check(self._shape(False, 30, 0.0, after=0), 30)

    def test_a_run_at_either_end_is_no_join(self):
        self.assertEqual(remux.cut_overlap_runs([0.0, 1e-5, 2e-5] + [F * i for i in range(1, 10)])[0], [])
        self.assertEqual(remux.cut_overlap_runs([F * i for i in range(10)] + [10 * F, 10 * F + 1e-5])[0], [])


def _probe_answers(codec, pts):
    """subprocess.run stand-in for cut_overlap_packets: the codec probe, then the packet stamps."""
    out = iter([mock.Mock(stdout=codec + "\n"), mock.Mock(stdout="\n".join(f"{t:.6f}" for t in pts))])
    return lambda *a, **k: next(out)


class CutOverlapReadsOnlyWhatItCanJudge(unittest.TestCase):
    """cut_overlap_packets: codecs whose frames are not uniform (Vorbis block switching, TrueHD's
    0.83 ms access units) are never judged; a missing stamp or an unreadable probe judges nothing."""

    def test_codecs_without_uniform_frames_are_not_judged(self):
        for codec in ("vorbis", "truehd", "flac", "pcm_s24le"):
            with mock.patch.object(remux.subprocess, "run",
                                   side_effect=_probe_answers(codec, [0.0, 0.0, 0.0, 1.0])):
                self.assertEqual(remux.cut_overlap_packets("/c.mp4"), ([], 0.0), codec)

    def test_a_judged_codec_is_read(self):
        pts = [i * F for i in range(20)] + [19 * F + k / 44100 for k in range(1, 9)]
        pts += [20 * F + 0.006 + i * F for i in range(20)]
        with mock.patch.object(remux.subprocess, "run", side_effect=_probe_answers("aac", pts)):
            self.assertEqual(remux.cut_overlap_packets("/c.mp4")[0], [(20, 27)])

    def test_a_missing_stamp_judges_nothing(self):
        with mock.patch.object(remux.subprocess, "run", side_effect=[
                mock.Mock(stdout="aac\n"), mock.Mock(stdout="0.0\n0.023\nN/A\n0.046\n")]):
            self.assertEqual(remux.cut_overlap_packets("/c.mp4"), ([], 0.0))

    def test_an_unreadable_probe_judges_nothing(self):
        with mock.patch.object(remux.subprocess, "run", side_effect=OSError):
            self.assertEqual(remux.cut_overlap_packets("/c.mp4"), ([], 0.0))

    def test_the_drop_args_read_the_source(self):
        self.assertEqual(remux.cut_overlap_bsf_args({1: [(5, 9)], 0: [(1, 2), (7, 8)], 2: []}),
                         ["-bsf:a:0", "noise=drop=between(n\\,1\\,2)+between(n\\,7\\,8)",
                          "-bsf:a:1", "noise=drop=between(n\\,5\\,9)"])
        self.assertEqual(remux.cut_overlap_bsf_args(None), [])
        c = build_tracks_command("/ff", "/cfr.mp4", "/orig.mp4", "/t.mka", drop={0: [(3, 4)]})
        self.assertLess(c.index("-bsf:a:0"), c.index("/cfr.mp4"))   # input side: before -i


class TracksDropACutOverlap(unittest.TestCase):
    """_prepare_tracks judges the first track, then the rest only when it found a cut; every build
    drops what it found; Atmos is never judged; the note says how much went."""

    def _go(self, runs_by_track, atmos=None, n_audio=1):
        calls, judged = [], []
        def run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0, stderr="")
        def judge(path, sel="a:0", ffprobe=None):
            judged.append(sel)
            return runs_by_track.get(sel, []), F
        with mock.patch.object(remux, "atmos_audio_index", return_value=atmos), \
             mock.patch.object(remux, "audio_track_count", return_value=n_audio), \
             mock.patch.object(remux, "subtitle_plan", return_value=[]), \
             mock.patch.object(remux, "tracks_sync_ms", return_value=0), \
             mock.patch.object(remux, "cut_overlap_packets", side_effect=judge), \
             mock.patch.object(remux.subprocess, "run", side_effect=run):
            res = remux._prepare_tracks("/c", "/o", "/t.mka")
        return res, calls, judged

    def test_a_clean_source_is_copied_as_before(self):
        res, calls, judged = self._go({}, n_audio=3)
        self.assertEqual(judged, ["a:0"])                       # the rest only after a cut
        self.assertFalse([x for x in calls[0] if x.startswith("-bsf")])
        self.assertNotIn("cut overlap", res[1])

    def test_a_cut_is_dropped_from_every_track(self):
        res, calls, judged = self._go({"a:0": [(10, 77), (500, 504)], "a:1": [(10, 77)]}, n_audio=2)
        self.assertEqual(judged, ["a:0", "a:1"])
        self.assertIn("-bsf:a:0", calls[0])
        self.assertIn("-bsf:a:1", calls[0])
        self.assertIn("cut overlap dropped from the audio (2 joins, 1.70 s)", res[1])

    def test_atmos_is_never_judged(self):
        res, calls, judged = self._go({"a:0": [(10, 77)]}, atmos=0, n_audio=2)
        self.assertEqual(judged, [])
        self.assertFalse([x for x in calls[0] if x.startswith("-bsf")])


def _beeps(path, player=True, sr=8000):
    """Onsets (s) of the 20 ms 1 kHz beeps in `path`'s first audio track. `player`: laid on the
    timestamps the way a player plays it (a gap is silence, an overlap is skipped); otherwise one
    sample after another, the way mkvmerge stamps a track."""
    import numpy as np
    af = ["-af", f"aresample=async=1:min_hard_comp=0.01,aresample={sr}"] if player else ["-ar", str(sr)]
    raw = subprocess.run([remux.FFMPEG, "-v", "error", "-i", path, "-map", "0:a:0", *af, "-ac", "1",
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    loud = np.abs(np.frombuffer(raw, np.float32)) > 0.1
    onsets, last = [], -10 ** 9
    for i in np.flatnonzero(loud):
        if i - last > sr // 2:
            onsets.append(i / sr)
        last = i
    return onsets


@unittest.skipUnless(all(os.path.exists(t) for t in (remux.FFMPEG, remux.FFPROBE, remux.MKVMERGE)),
                     "ffmpeg/ffprobe/mkvmerge not installed")
class ACutsOverlapNeverReachesTheMaster(unittest.TestCase):
    """For real: a source cut the way yt-dlp's SponsorBlock removal cuts (concat demuxer + stream
    copy, the kept stretch reaching back to its keyframe), through _prepare_tracks and mkvmerge. Left
    alone, mkvmerge counts the packed packets in and the rest of the sound plays late by them (what
    shipped The Epstein Coverup Runs Deep 1.21 s late, 2026-10-21); dropped, the master plays where a
    player put the download's sound, and every other packet is bit-exact."""

    def _src(self, d, cuts):
        j = lambda n: os.path.join(d, n)
        beep = "aevalsrc=0.5*sin(2*PI*1000*t)*between(mod(t\\,3)\\,0.25\\,0.27):s=44100"
        r = subprocess.run([remux.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24000/1001",
                            "-f", "lavfi", "-i", beep, "-t", "20", "-c:v", "libx264", "-g", "48",
                            "-keyint_min", "48", "-sc_threshold", "0", "-c:a", "aac", "-b:a", "128k",
                            j("src.mp4")], capture_output=True)
        if r.returncode:
            self.skipTest("no libx264 here")
        with open(j("list.txt"), "w") as f:
            for a, b in cuts:
                f.write(f"file 'src.mp4'\ninpoint {a}\noutpoint {b}\n")
        subprocess.run([remux.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                        "-safe", "0", "-i", j("list.txt"), "-map", "0", "-dn", "-ignore_unknown",
                        "-c", "copy", "-movflags", "+faststart", j("cut.mp4")],
                       capture_output=True, check=True)
        return j("cut.mp4")

    def _mkv(self, d, tracks, name="m.mka"):
        out = os.path.join(d, name)
        subprocess.run([remux.MKVMERGE, "-q", "-o", out, tracks], capture_output=True, check=True)
        return out

    def _md5s(self, path):
        out = subprocess.run([remux.FFMPEG, "-v", "error", "-i", path, "-map", "0:a:0", "-c", "copy",
                              "-f", "framemd5", "-"], capture_output=True, text=True).stdout
        return [ln.split(",")[-1].strip() for ln in out.splitlines() if not ln.startswith("#")]

    def test_the_copy_drops_the_overlap_and_nothing_else(self):
        d = tempfile.mkdtemp()
        try:
            # keep [0, 6) and [9.5, 20): the second stretch reaches back to the keyframe at 8.0
            src = self._src(d, [(0, 6), (9.5, 20)])
            runs, _f = remux.cut_overlap_packets(src)
            self.assertEqual(len(runs), 1)
            first = _beeps(src)[0]
            truth = [t - first for t in _beeps(src)]                 # the download, as played
            self.assertAlmostEqual(truth[2], 8.5, delta=0.01)        # source 12.25 at 6 + 2.75

            plain = os.path.join(d, "plain.mka")                     # the old copy, as mkvmerge took it
            subprocess.run(build_tracks_command(remux.FFMPEG, src, src, plain, subs=[]),
                           capture_output=True, check=True)
            late = _beeps(self._mkv(d, plain, "plain_m.mka"))
            self.assertGreater(late[-1] - late[0] - truth[-1], 1.0)   # (the cut's own beep plays too)
            self.assertGreater(remux.master_audio_drift(plain, os.path.join(d, "plain_m.mka")), 1.0)

            t = os.path.join(d, "t.mka")
            ok, note, why, sync = remux._prepare_tracks(src, src, t)
            self.assertTrue(ok, why)
            self.assertIn("cut overlap dropped from the audio (1 join,", note)
            m = self._mkv(d, t)
            for player in (True, False):                             # no reader can disagree now
                got = _beeps(m, player=player)
                self.assertEqual(len(got), len(truth), player)
                for g, want in zip(got, truth):
                    self.assertAlmostEqual(g - got[0], want, delta=0.03, msg=player)
            self.assertLess(abs(remux.master_audio_drift(t, m)), remux.AUDIO_DRIFT_TOLERANCE)
            a, b = runs[0]
            kept = self._md5s(src)
            self.assertEqual(self._md5s(t), kept[:a] + kept[b + 1:])  # bit-exact, minus the run
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_the_boost_drops_it_too(self):
        d = tempfile.mkdtemp()
        try:
            src = self._src(d, [(0, 6), (9.5, 20)])
            runs, _f = remux.cut_overlap_packets(src)
            t = os.path.join(d, "t.mka")
            subprocess.run(build_tracks_command(remux.FFMPEG, src, src, t, gain_db=3.0, subs=[],
                                                drop={0: runs}), capture_output=True, check=True)
            got = _beeps(self._mkv(d, t))
            self.assertAlmostEqual(got[2] - got[0], 8.5, delta=0.03)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_every_join_is_found_wherever_the_cut_falls(self):
        # the second review's misses (A: three joins, B: a stretch starting on its keyframe, C: the
        # 44.1k join at 9.503483), and a cut walked across one audio frame; MP4 and MKV downloads
        # (the MKV through an MP4 copy, as the CFR step makes it), 44.1 and 48 kHz
        cases = [[(0, 6.191967), (7.641151, 14.593702), (15.995802, 25.581296), (32.756552, None)],
                 [(0, 12.0), (20.02, None)], [(0, 6.0), (9.503483, None)]]
        cases += [[(0, 6.0), (9.5 + k * 0.0047, None)] for k in range(8)]
        d = tempfile.mkdtemp()
        try:
            j = lambda n: os.path.join(d, n)
            beep = "aevalsrc=0.5*sin(2*PI*1000*t)*between(mod(t\\,3)\\,0.25\\,0.27):s={}"
            for rate in (44100, 48000):
                src = j(f"src{rate}.mp4")
                r = subprocess.run([remux.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                                    "-i", "testsrc2=size=96x54:rate=24000/1001", "-f", "lavfi", "-i",
                                    beep.format(rate), "-t", "40", "-c:v", "libx264", "-g", "48",
                                    "-keyint_min", "48", "-sc_threshold", "0", "-c:a", "aac", src],
                                   capture_output=True)
                if r.returncode:
                    self.skipTest("no libx264 here")
                for ci, segs in enumerate(cases):
                    with open(j("list.txt"), "w") as f:
                        for a, b in segs:
                            f.write(f"file '{src}'\ninpoint {a}\n" + (f"outpoint {b}\n" if b else ""))
                    for kind in ("mp4", "mkv"):
                        cut = j(f"cut.{kind}")
                        subprocess.run([remux.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f",
                                        "concat", "-safe", "0", "-i", j("list.txt"), "-map", "0", "-dn",
                                        "-c", "copy", cut], capture_output=True, check=True)
                        if kind == "mkv":                        # the CFR copies it into an MP4
                            subprocess.run([remux.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                                            "-i", cut, "-map", "0", "-c", "copy", j("cfr.mp4")],
                                           capture_output=True, check=True)
                            cut = j("cfr.mp4")
                        runs, frame = remux.cut_overlap_packets(cut)
                        pts = [float(x.split(",")[0]) for x in subprocess.run(
                            [remux.FFPROBE, "-v", "error", "-select_streams", "a:0", "-show_entries",
                             "packet=pts_time", "-of", "csv=p=0", cut], capture_output=True,
                            text=True).stdout.split()]
                        gone = {k for a, b in runs for k in range(a, b + 1)}
                        kept = [t for k, t in enumerate(pts) if k not in gone]
                        what = f"{rate} {kind} case {ci}: {runs}"
                        self.assertEqual(len(runs), len(segs) - 1, what)    # every join, one run each
                        self.assertGreater(min(b - a for a, b in zip(kept, kept[1:])), frame / 2, what)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_a_last_piece_of_nothing_but_pre_roll(self):
        # yt-dlp writes a final piece from YouTube's whole-second length when the file runs longer:
        # if the sound has already ended there, that piece is keyframe pre-roll piled at the very end
        d = tempfile.mkdtemp()
        try:
            j = lambda n: os.path.join(d, n)
            beep = "aevalsrc=0.5*sin(2*PI*1000*t)*between(mod(t\\,3)\\,0.25\\,0.27):s=44100:d=39.5"
            r = subprocess.run([remux.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                                "-i", "testsrc2=size=96x54:rate=24000/1001:d=40", "-f", "lavfi", "-i", beep,
                                "-c:v", "libx264", "-g", "48", "-keyint_min", "48", "-sc_threshold", "0",
                                "-c:a", "aac", j("src.mp4")], capture_output=True)
            if r.returncode:
                self.skipTest("no libx264 here")
            with open(j("list.txt"), "w") as f:
                f.write(f"file '{j('src.mp4')}'\ninpoint 0\noutpoint 30\nfile '{j('src.mp4')}'\ninpoint 39.8\n")
            subprocess.run([remux.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                            "-safe", "0", "-i", j("list.txt"), "-map", "0", "-dn", "-c", "copy",
                            j("cut.mp4")], capture_output=True, check=True)
            runs, frame = remux.cut_overlap_packets(j("cut.mp4"))
            pts = [float(x.split(",")[0]) for x in subprocess.run(
                [remux.FFPROBE, "-v", "error", "-select_streams", "a:0", "-show_entries",
                 "packet=pts_time", "-of", "csv=p=0", j("cut.mp4")], capture_output=True,
                text=True).stdout.split()]
            self.assertEqual(len(runs), 1, runs)
            self.assertEqual(runs[0][1], len(pts) - 1)             # it runs to the end of the file
            self.assertGreater((runs[0][1] - runs[0][0] + 1) * frame, 1.0)   # ~1.46 s from 38.04
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_a_dvd_remuxed_by_mkvmerge_is_left_alone(self):
        # ffmpeg's VOB stamps one AC3 packet a frame late; mkvmerge makes a pair of it (2F, F, 0, F)
        d = tempfile.mkdtemp()
        try:
            j = lambda n: os.path.join(d, n)
            run = lambda c: subprocess.run(c, capture_output=True, check=True)
            run([remux.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=96x54:rate=25",
                 "-f", "lavfi", "-i", "anoisesrc=d=60:a=0.1", "-t", "60", "-c:v", "mpeg2video",
                 "-c:a", "ac3", "-b:a", "448k", "-f", "vob", j("dvd.vob")])
            run([remux.FFMPEG, "-v", "error", "-y", "-fflags", "+genpts", "-i", j("dvd.vob"),
                 "-map", "0:v", "-map", "0:a", "-c", "copy", j("copy.mkv")])
            run([remux.MKVMERGE, "-q", "-o", j("remux.mkv"), j("copy.mkv")])
            run([remux.FFMPEG, "-v", "error", "-y", "-i", j("remux.mkv"), "-map", "0", "-c", "copy",
                 j("cfr.mp4")])
            for f in ("dvd.vob", "copy.mkv", "remux.mkv", "cfr.mp4"):
                self.assertEqual(remux.cut_overlap_packets(j(f))[0], [], f)
            pts = [float(x.split(",")[0]) for x in subprocess.run(       # the shape is really there
                [remux.FFPROBE, "-v", "error", "-select_streams", "a:0", "-show_entries",
                 "packet=pts_time", "-of", "csv=p=0", j("remux.mkv")], capture_output=True,
                text=True).stdout.split()]
            self.assertTrue(any(b - a < 0.001 for a, b in zip(pts, pts[1:])))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_a_clean_source_and_jitter_are_left_alone(self):
        d = tempfile.mkdtemp()
        try:
            src = self._src(d, [(0, 20)])
            self.assertEqual(remux.cut_overlap_packets(src)[0], [])
            jit = os.path.join(d, "jit.mp4")                         # one packet a frame late
            subprocess.run([remux.FFMPEG, "-v", "error", "-y", "-i", src, "-map", "0:a", "-c", "copy",
                            "-bsf:a", "setts=ts=if(eq(N\\,100)\\,TS+DURATION\\,TS)", jit],
                           capture_output=True, check=True)
            self.assertEqual(remux.cut_overlap_packets(jit)[0], [])
            ps = os.path.join(d, "ps.mpg")                           # DVD-style MPEG-PS with AC3
            subprocess.run([remux.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                            "testsrc2=size=160x90:rate=25", "-f", "lavfi", "-i", "sine=f=440:d=20",
                            "-t", "20", "-c:v", "mpeg2video", "-c:a", "ac3", "-f", "vob", ps],
                           capture_output=True, check=True)
            self.assertEqual(remux.cut_overlap_packets(ps)[0], [])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TheMuxSaysWhenTheSoundDrifts(unittest.TestCase):
    """_mux_verified's backstop: a master whose sound runs longer than its tracks' timestamps span
    says so in its note — and still ships (it is a report, not a gate)."""

    def _mux(self, drift):
        res = remux.RemuxResult(True, "/o.mkv", reason="DV 8.1 · 1 audio · 0 sub")
        with mock.patch.object(remux, "run_mux", return_value=(True, "")), \
             mock.patch.object(remux.os.path, "exists", return_value=True), \
             mock.patch.object(remux, "_verify", return_value=res), \
             mock.patch.object(remux, "master_audio_drift", return_value=drift), \
             mock.patch.object(remux, "_gate_interleave", side_effect=lambda r, *a, **k: r):
            return remux._mux_verified("/es", "24000/1001", "/t.mka", "/o.mkv")

    def test_a_drift_is_said(self):
        r = self._mux(1.21)
        self.assertTrue(r.ok)
        self.assertIn("AUDIO DRIFT: the sound runs +1.21 s against its timestamps", r.reason)

    def test_no_drift_says_nothing(self):
        for d in (0.0, 0.05, None):
            self.assertNotIn("DRIFT", self._mux(d).reason, d)


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


class NoFreeMeasurementNames(unittest.TestCase):
    """Static: the audio-landing note was refactored onto one helper and called with the
    measurement variable — but the four remux paths name theirs differently, and two do not
    store it at all. Pyright flagged `measured_lufs`/`mkv_measured` as undefined or unbound
    in three functions (2026-09-05): a NameError the first time a boost lands off-target in
    the inject/ship paths every YouTube video takes. Never fired live, only by luck. This
    pins the CLASS: any load of these names inside a function must have a binding there."""

    NAMES = {"measured_lufs", "mkv_measured"}

    def test_every_load_has_a_binding_in_the_same_function(self):
        import ast, inspect
        tree = ast.parse(inspect.getsource(remux))
        problems = []
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            bound, loaded = set(), []
            for node in ast.walk(fn):
                if isinstance(node, ast.Name) and node.id in self.NAMES:
                    if isinstance(node.ctx, ast.Store):
                        bound.add(node.id)
                    else:
                        loaded.append(node)
                elif isinstance(node, ast.arg) and node.arg in self.NAMES:
                    bound.add(node.arg)
            for node in loaded:
                if node.id not in bound:
                    problems.append("%s: line %d loads %s with no binding" % (fn.name, node.lineno, node.id))
        self.assertEqual(problems, [])


class NativeBundle(unittest.TestCase):
    """remux(native=bundle): no Resolve render. Every encoder call — main encode, repair rung
    AND the re-concat — decodes Topaz's own ProRes chunks through the LUT; the RPU is the
    generated one, COPIED (atomically) into the segdir instead of extracted; frames, fps and
    MaxCLL come from the bundle (nothing is probed); the mastering display is dvcap's 1000-nit
    fallback; the segment plan is the chunk ends; and an RPU that is not exactly the chunk
    total fails before the encode."""

    CHUNKS = [[0, "/scratch/ep/seg_0000.mov"], [2217, "/scratch/ep/seg_0001.mov"],
              [4398, "/scratch/ep/seg_0002.mov"], [6000, None]]
    BOUNDS = [2217, 4398, 6000]
    VF = "scale=out_range=full,lut3d=file=/luts/x.cube,format=yuv420p10le"
    FPS = "24000/1001"
    RPU_BYTES = b"RPU" * 100

    def _bundle(self, tmp, **over):
        import os
        rpu = os.path.join(tmp, "native", "native.rpu")
        os.makedirs(os.path.dirname(rpu), exist_ok=True)
        with open(rpu, "wb") as f:
            f.write(self.RPU_BYTES)
        b = {"chunks": self.CHUNKS, "vf": self.VF, "rpu": rpu, "fps": self.FPS,
             "max_cll": "1000,400", "total": 6000, "bounds": self.BOUNDS, "id": "abc123"}
        b.update(over)
        return b

    def _run(self, tmp, bundle, buckets_seq=({1: 45.0},), rpu_frames=6000, **kw):
        """MKV output (no audio machinery to mock). The REAL resume_manifest/ensure_segdir run,
        so the segdir, manifest.json and the copied rpu.bin are real files under `tmp`."""
        import os
        out = os.path.join(tmp, "master.mkv")
        segdir = out + ".remuxsegs"
        seen = {"enc": [], "repair": [], "rpu_bytes": [], "manifest": []}
        ran = type("R", (), {"returncode": 0, "stderr": ""})()

        def snapshot(rpu):
            self.assertEqual(rpu, os.path.join(segdir, "rpu.bin"))
            with open(rpu, "rb") as f:
                seen["rpu_bytes"].append(f.read())
            with open(os.path.join(segdir, "manifest.json")) as f:
                seen["manifest"].append(json.load(f))

        def fake_encode(src, rpu, hevc, cap, **k):
            snapshot(rpu)
            seen["enc"].append((src, k))
            return True, k["total_frames"], "ok"

        def fake_repair(src, rpu, sd, idx, tight, **k):
            snapshot(rpu)
            seen["repair"].append((src, list(idx), tight, k))
            return True, "ok"

        with mock.patch.object(remux.dvcap, "probe_video",
                               side_effect=AssertionError("native mode probes nothing")), \
             mock.patch.object(remux.dvcap, "extract_rpu",
                               side_effect=AssertionError("native RPU is copied, not extracted")), \
             mock.patch.object(remux.dvcap, "rpu_frame_count", return_value=rpu_frames), \
             mock.patch.object(remux.dvcap, "encode_capped_segmented", side_effect=fake_encode) as enc, \
             mock.patch.object(remux.dvcap, "reencode_segments_tighter", side_effect=fake_repair), \
             mock.patch.object(remux.dvcap, "video_peak_buckets", side_effect=list(buckets_seq)), \
             mock.patch.object(remux, "_mux_verified",
                               side_effect=fake_mux(seen.setdefault("mux", []), reason="ok")), \
             mock.patch.object(remux, "_prepare_tracks", return_value=TRACKS_OK), \
             mock.patch.object(remux.subprocess, "run", return_value=ran) as run:
            res = remux.remux("/unused/render.mov", "cfr.mkv", "orig.mkv", out, native=bundle, **kw)
        seen["segdir"], seen["enc_mock"], seen["runs"] = segdir, enc, run.call_args_list
        return res, seen

    def test_every_encoder_call_gets_the_chunks_and_the_lut(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            res, seen = self._run(tmp, self._bundle(tmp), [{1: 58.6}, {1: 40.0}])   # over → repair
        self.assertTrue(res.ok, res.reason)
        self.assertIn("peak repair: 1 seg(s) re-capped @ 42", res.reason)
        want_decode = {"chunks": self.CHUNKS, "vf": self.VF, "freed": set()}
        calls = [k for _s, k in seen["enc"]] + [r[3] for r in seen["repair"]]
        self.assertEqual(len(seen["enc"]), 2)            # main encode + the repair re-concat
        self.assertEqual(len(seen["repair"]), 1)         # the repair rung
        for k in calls:
            self.assertEqual(k["decode"], want_decode)
            self.assertEqual(k["boundaries"], self.BOUNDS)   # the chunk ends, not topaz_segbounds
            self.assertEqual(k["total_frames"], 6000)
            self.assertEqual(k["fps"], self.FPS)
            self.assertIsNone(k["master_display"])           # → FALLBACK_MASTER_DISPLAY
            self.assertEqual(k["max_cll"], "1000,400")
        for src in [s for s, _k in seen["enc"]] + [r[0] for r in seen["repair"]]:
            self.assertEqual(src, "/scratch/ep/seg_0000.mov")   # nominal: decode overrides it
        self.assertEqual(seen["repair"][0][1:3], ([0], 42))
        # every mux (the first pass and the repaired one) carries the bundle's exact fps fraction,
        # the frame count, and the HDR10 description the x265 SEI says (dvcap's 1000-nit fallback)
        self.assertEqual(len(seen["mux"]), 2)
        for m in seen["mux"]:
            self.assertEqual(m["fps"], "24000/1001")
            self.assertEqual(m["frames"], 6000)
            self.assertTrue(m["require_dv"])
            c = " ".join(m["colour"])
            self.assertIn("--max-luminance 0:1000", c)
            self.assertIn("--max-content-light 0:1000", c)

    def test_the_generated_rpu_is_copied_into_the_segdir(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._bundle(tmp)
            res, seen = self._run(tmp, bundle)
            self.assertTrue(res.ok, res.reason)
            self.assertEqual(seen["rpu_bytes"], [self.RPU_BYTES])     # encoded from the copy
            with open(bundle["rpu"], "rb") as f:
                self.assertEqual(f.read(), self.RPU_BYTES)            # the canonical survives
            self.assertFalse(os.path.exists(seen["segdir"]))          # success removes the segdir

    def test_the_resume_identity_is_the_native_id(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            res, seen = self._run(tmp, self._bundle(tmp))
        self.assertTrue(res.ok, res.reason)
        m = seen["manifest"][0]
        self.assertEqual(m["src"], "native:abc123")       # not a stat of any render
        self.assertEqual(m["frames"], 6000)
        self.assertEqual(m["fps"], self.FPS)
        self.assertEqual(m["segb"], self.BOUNDS)
        self.assertNotIn("enc", m)
        self.assertNotIn("rpu_mode", m)

    def test_bundle_bounds_override_caller_boundaries(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            res, seen = self._run(tmp, self._bundle(tmp), boundaries=[1000, 6000])
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(seen["enc"][0][1]["boundaries"], self.BOUNDS)

    def test_rpu_chunk_frame_mismatch_fails_before_encoding(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            res, seen = self._run(tmp, self._bundle(tmp), rpu_frames=5999)
            self.assertFalse(res.ok)
            self.assertIn("native RPU/chunk frame mismatch: rpu 5999 != chunks 6000", res.reason)
            seen["enc_mock"].assert_not_called()
            self.assertFalse(os.path.exists(os.path.join(seen["segdir"], "rpu.bin")))  # re-copied next try
            self.assertTrue(os.path.exists(os.path.join(seen["segdir"], "manifest.json")))

    def test_missing_generated_rpu_fails_cleanly(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            res, seen = self._run(tmp, self._bundle(tmp, rpu=os.path.join(tmp, "gone.rpu")))
            self.assertFalse(res.ok)
            self.assertIn("native RPU copy failed", res.reason)
            seen["enc_mock"].assert_not_called()
            self.assertEqual(sorted(os.listdir(seen["segdir"])), ["manifest.json"])  # no .part left

    def test_empty_generated_rpu_is_not_published(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._bundle(tmp)
            open(bundle["rpu"], "wb").close()
            res, seen = self._run(tmp, bundle)
            self.assertFalse(res.ok)
            self.assertIn("native RPU copy failed", res.reason)
            self.assertEqual(sorted(os.listdir(seen["segdir"])), ["manifest.json"])

    def test_resume_reuses_the_copied_rpu(self):
        # same native id → ensure_segdir resumes, and the rpu.bin already there is the one used
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._bundle(tmp)
            segdir = os.path.join(tmp, "master.mkv.remuxsegs")
            remux.dvcap.ensure_segdir(segdir, remux.dvcap.resume_manifest(
                "/scratch/ep/seg_0000.mov", remux.dvcap.DEFAULT_PEAK_MBPS, 6000, self.FPS,
                remux.dvcap.SEG_SECONDS, boundaries=self.BOUNDS, native_id="abc123"))
            with open(os.path.join(segdir, "rpu.bin"), "wb") as f:
                f.write(b"KEPT")
            res, seen = self._run(tmp, bundle)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(seen["rpu_bytes"], [b"KEPT"])

    def test_malformed_bundle_leaves_the_resume_state_alone(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            good = self._bundle(tmp)
            for name, bad in (("total past the chunks", dict(good, total=6001)),
                              ("no id", dict(good, id="")),
                              ("no chunks", dict(good, chunks=[])),
                              ("zero total", dict(good, total=0))):
                with self.subTest(name), \
                     mock.patch.object(remux.dvcap, "ensure_segdir") as ens, \
                     mock.patch.object(remux.dvcap, "encode_capped_segmented") as enc:
                    res = remux.remux("/r.mov", "cfr.mkv", "orig.mkv", tmp + "/m.mkv", native=bad)
                    self.assertFalse(res.ok)
                    self.assertIn("native bundle is malformed", res.reason)
                    ens.assert_not_called()
                    enc.assert_not_called()

    def test_native_refuses_an_encode_source_or_rpu_mode(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for kw in ({"encode_source": "/src.mkv"}, {"rpu_mode": 2}):
                with self.subTest(**kw), \
                     mock.patch.object(remux.dvcap, "ensure_segdir") as ens:
                    res = remux.remux("/r.mov", "cfr.mkv", "orig.mkv", tmp + "/m.mkv",
                                      native=self._bundle(tmp), **kw)
                    self.assertFalse(res.ok)
                    ens.assert_not_called()


class ResolvePathPinned(unittest.TestCase):
    """native=None (dv_engine 'resolve', the default) must leave the Resolve remux exactly as
    it was: the same dvcap calls with the same arguments — no `decode`, no `native_id`, the
    probed mastering data — whether `native` is omitted or passed as None."""

    MD = "G(1,2)B(3,4)R(5,6)WP(7,8)L(9,10)"

    def _run(self, tmp, **kw):
        import os
        out = os.path.join(tmp, "master.mkv")
        render = os.path.join(tmp, "render.mov")
        info = {"frames": 100, "fps": "24000/1001", "start_time": 0.0,
                "master_display": self.MD, "max_cll": "900,300"}
        ran = type("R", (), {"returncode": 0, "stderr": ""})()
        with mock.patch.object(remux.dvcap, "probe_video", return_value=info) as probe, \
             mock.patch.object(remux.dvcap, "resume_manifest", return_value={"m": 1}) as man, \
             mock.patch.object(remux.dvcap, "ensure_segdir", return_value="fresh") as ens, \
             mock.patch.object(remux.dvcap, "extract_rpu", return_value=(True, "ok")) as ext, \
             mock.patch.object(remux.dvcap, "rpu_frame_count", return_value=100), \
             mock.patch.object(remux.dvcap, "encode_capped_segmented",
                               return_value=(True, 100, "ok")) as enc, \
             mock.patch.object(remux.dvcap, "reencode_segments_tighter",
                               return_value=(True, "ok")) as rep, \
             mock.patch.object(remux.dvcap, "video_peak_buckets",
                               side_effect=[{1: 58.6}, {1: 40.0}]), \
             mock.patch.object(remux, "_mux_verified", side_effect=fake_mux(reason="ok")), \
             mock.patch.object(remux, "_prepare_tracks", return_value=TRACKS_OK), \
             mock.patch.object(remux.subprocess, "run", return_value=ran):
            res = remux.remux(render, "cfr.mkv", "orig.mkv", out, boundaries=[50, 100], **kw)
        calls = {"probe": probe.call_args_list, "manifest": man.call_args_list,
                 "ensure": ens.call_args_list, "extract": ext.call_args_list,
                 "encode": enc.call_args_list, "repair": rep.call_args_list}
        return res, calls, render, out

    def test_call_arguments_are_the_pre_native_ones(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            res, calls, render, out = self._run(tmp)
        self.assertTrue(res.ok, res.reason)
        segdir = out + ".remuxsegs"
        rpu, hevc = segdir + "/rpu.bin", out + ".capped.hevc"
        fps, ff = "24000/1001", remux.FFMPEG
        self.assertEqual(calls["probe"], [mock.call(render, remux.FFPROBE)])
        self.assertEqual(calls["manifest"], [mock.call(
            render, 50, 100, fps, remux.dvcap.SEG_SECONDS, boundaries=[50, 100],
            encode_source=None, rpu_mode=None)])
        self.assertEqual(calls["ensure"], [mock.call(segdir, {"m": 1})])
        # extracted to a side file, published as rpu.bin only once its frame order checked out
        self.assertEqual(calls["extract"], [mock.call(render, rpu + ".extract", mode=None, ffmpeg=ff,
                                                      timeout=None)])
        self.assertEqual(calls["encode"], [
            mock.call(render, rpu, hevc, 50, segdir=segdir, total_frames=100, fps=fps,
                      boundaries=[50, 100], master_display=self.MD, max_cll="900,300",
                      abort=None, on_progress=None, on_plan=None, should_pause=None, ffmpeg=ff),
            mock.call(render, rpu, hevc, 50, segdir=segdir, total_frames=100, fps=fps,
                      boundaries=[50, 100], master_display=self.MD, max_cll="900,300",
                      abort=None, ffmpeg=ff)])
        self.assertEqual(calls["repair"], [
            mock.call(render, rpu, segdir, [0], 42, total_frames=100, fps=fps,
                      boundaries=[50, 100], master_display=self.MD, max_cll="900,300",
                      abort=None, on_repair=None, ffmpeg=ff)])

    def test_explicit_none_is_the_same_as_omitting_it(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            res_a, calls_a, _r, _o = self._run(tmp)
            res_b, calls_b, _r, _o = self._run(tmp, native=None)
        self.assertEqual((res_a.ok, res_a.reason), (res_b.ok, res_b.reason))
        self.assertEqual(calls_a, calls_b)


class SdrMaster(unittest.TestCase):
    """remux(sdr=True) / remux_ship_render(sdr=True) (2026-10-18, an item pinned to SDR output,
    from a native SDR render): no RPU anywhere — nothing extracted, the frame count is the render's
    packets — the Rec.709 x265 (dvcap dv=False) under the cap, no DV box in either mux, a verify
    that asks for no DOVI record, and an "sdr" resume identity. sdr=False calls stay the DV ones
    (ResolvePathPinned)."""

    def test_the_capped_sdr_master(self):
        import tempfile, os
        info = {"frames": 100, "fps": "24000/1001", "start_time": 0.0, "master_display": None,
                "max_cll": None}
        ran = type("R", (), {"returncode": 0, "stderr": ""})()
        verified = []
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(remux.dvcap, "probe_video", return_value=info), \
             mock.patch.object(remux.dvcap, "resume_manifest", return_value={"m": 1}) as man, \
             mock.patch.object(remux.dvcap, "ensure_segdir", return_value="fresh"), \
             mock.patch.object(remux.dvcap, "extract_rpu", side_effect=AssertionError("extracted")), \
             mock.patch.object(remux.dvcap, "rpu_frame_count", side_effect=AssertionError("rpu read")), \
             mock.patch.object(remux.dvcap, "count_hevc_frames", return_value=100), \
             mock.patch.object(remux.dvcap, "encode_capped_segmented",
                               return_value=(True, 100, "ok")) as enc, \
             mock.patch.object(remux.dvcap, "reencode_segments_tighter",
                               return_value=(True, "ok")) as rep, \
             mock.patch.object(remux.dvcap, "video_peak_buckets", side_effect=[{1: 58.6}, {1: 40.0}]), \
             mock.patch.object(remux, "_mux_verified",
                               side_effect=fake_mux(verified, reason="SDR · 1 audio · 1 sub")), \
             mock.patch.object(remux, "_prepare_tracks", return_value=TRACKS_OK), \
             mock.patch.object(remux.subprocess, "run", return_value=ran):
            out = os.path.join(tmp, "master SDR upscaled.mkv")
            res = remux.remux("/r/render.mov", "cfr.mkv", "orig.mkv", out, boundaries=[50, 100], sdr=True)
            self.assertTrue(os.path.exists(out))               # the gated .part was renamed onto it
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(man.call_args.kwargs.get("sdr"), True)
        for call in enc.call_args_list + rep.call_args_list:
            self.assertEqual(call.kwargs.get("dv"), False)
        self.assertEqual([m["require_dv"] for m in verified], [False, False])   # both muxes
        for m in verified:                                     # BT.709 container colour, no HDR10
            c = " ".join(m["colour"])
            self.assertIn("--colour-primaries 0:1", c)
            self.assertNotIn("luminance", c)

    def test_sdr_refuses_what_only_dv_has(self):
        for kw in ({"native": {"chunks": []}}, {"encode_source": "/s.mkv"}, {"rpu_mode": 2}):
            with self.subTest(kw=kw):
                res = remux.remux("/r.mov", "c.mkv", "o.mkv", "/tmp/x/out.mkv", sdr=True, **kw)
                self.assertFalse(res.ok)
                self.assertIn("no RPU", res.reason)

    def test_an_sdr_render_muxes_with_no_dv_and_bt709(self):
        c = " ".join(remux.build_master_mux_command("/m", "/a.hevc", "25", "/o.part",
                                                    colour=remux.mkv_colour_args(sdr=True)))
        self.assertNotIn("dvp", c)
        self.assertIn("--colour-transfer-characteristics 0:1", c)

    def test_verify_relaxes_only_the_dv_claim(self):
        sdr = json.dumps({"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]})
        self.assertFalse(remux.verify_remux(sdr)[0])
        self.assertEqual(remux.verify_remux(sdr, require_dv=False), (True, "SDR · 1 audio · 0 sub"))
        none = json.dumps({"streams": [{"codec_type": "video"}]})
        self.assertFalse(remux.verify_remux(none, require_dv=False)[0])

    def test_ship_render_sdr(self):
        import tempfile, os
        ran = type("R", (), {"returncode": 0, "stderr": ""})()
        verified = []

        def fake_run(cmd, **kw):
            if cmd[-1].endswith(".ship.hevc"):
                with open(cmd[-1], "wb") as f:
                    f.write(b"es")
            return ran
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(remux.dvcap, "probe_video", return_value={"frames": 100, "fps": "25"}), \
             mock.patch.object(remux.dvcap, "video_peak_1s_mbps", return_value=20.0), \
             mock.patch.object(remux, "_mux_verified",
                               side_effect=fake_mux(verified, reason="SDR · 1 audio · 0 sub")), \
             mock.patch.object(remux, "_prepare_tracks", return_value=TRACKS_OK), \
             mock.patch.object(remux.subprocess, "run", side_effect=fake_run):
            res = remux.remux_ship_render("/r.mov", "/c.mp4", "/o.mp4", os.path.join(tmp, "o.mkv"),
                                          cap_mbps=50, sdr=True)
            self.assertTrue(os.path.exists(os.path.join(tmp, "o.mkv")))
        self.assertTrue(res.ok, res.reason)
        self.assertEqual([m["require_dv"] for m in verified], [False])
        self.assertIn("--colour-primaries 0:1", " ".join(verified[0]["colour"]))


class ChunkReleaser(unittest.TestCase):
    """THE CHUNK LANE frees each Topaz chunk once its segments are encoded and provably under
    the peak gate (user, 2026-10-19) — one segment behind the encode, the last one at once."""

    CHUNKS = NativeBundle.CHUNKS
    SEGS = [(0, 2217), (2217, 4398), (4398, 6000)]
    FPS = "24000/1001"

    def _rel(self, sizes, freed=None, cap=50):
        self.calls = []
        def free(path):
            self.calls.append(path)
            return 1000
        native = {"chunks": self.CHUNKS, "free_chunk": free}
        rel = remux._ChunkReleaser(native, self.SEGS, self.FPS, cap, freed=freed)
        self.sizes = sizes
        return rel

    def _feed(self, rel, *indices):
        with mock.patch.object(remux.dvcap, "hevc_packet_sizes",
                               side_effect=lambda p, ff=None: self.sizes[int(p)]):
            for i in indices:
                rel.on_segment(i, str(i))

    def test_quiet_segments_free_their_chunks_one_behind(self):
        quiet = [10_000] * 2300                  # ~0.5 Mb a frame: far under 50 Mbps
        rel = self._rel({0: quiet[:2217], 1: quiet[:2181], 2: quiet[:1602]})
        self._feed(rel, 0)
        self.assertEqual(self.calls, [])         # its right neighbour is not known yet
        self._feed(rel, 1)
        self.assertEqual(self.calls, ["/scratch/ep/seg_0000.mov"])
        self._feed(rel, 2)                       # the last segment has no right neighbour
        self.assertEqual(self.calls, ["/scratch/ep/seg_0000.mov", "/scratch/ep/seg_0001.mov",
                                      "/scratch/ep/seg_0002.mov"])
        self.assertEqual(rel.freed_bytes, 3000)

    def test_a_hot_segment_keeps_its_chunk_and_so_does_its_neighbours_edge(self):
        quiet, hot = [10_000] * 2217, [10_000] * 2181
        hot[5] = 30_000_000                      # a burst at the start of segment 1
        rel = self._rel({0: quiet, 1: hot, 2: [10_000] * 1602})
        self._feed(rel, 0, 1, 2)
        # the burst sits within a window of segment 0's tail → chunk 0 stays too
        self.assertEqual(self.calls, ["/scratch/ep/seg_0002.mov"])

    def test_already_freed_chunks_are_not_freed_again(self):
        gone = {"/scratch/ep/seg_0000.mov"}
        rel = self._rel({0: [10_000] * 2217, 1: [10_000] * 2181, 2: [10_000] * 1602}, freed=gone)
        self._feed(rel, 0, 1)
        self.assertEqual(self.calls, [])
        self.assertIn("/scratch/ep/seg_0000.mov", gone)

    def test_a_scan_failure_only_keeps_the_chunk(self):
        rel = self._rel({})
        with mock.patch.object(remux.dvcap, "hevc_packet_sizes", side_effect=RuntimeError("no packets")):
            rel.on_segment(0, "0")               # never raises into the encode
        self.assertEqual(self.calls, [])

    def test_the_native_remux_wires_it_in(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            b = NativeBundle()._bundle(tmp, free_chunk=lambda path: 1 << 30)
            h = NativeBundle()
            h.assertEqual = self.assertEqual
            res, seen = h._run(tmp, b)
        self.assertTrue(res.ok, res.reason)
        main = seen["enc"][0][1]
        self.assertTrue(callable(main.get("on_segment")))
        self.assertIs(main["decode"]["freed"], seen["enc"][0][1]["decode"]["freed"])

    def test_a_rendered_item_has_no_releaser(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            res, seen = NativeBundle()._run(tmp, NativeBundle()._bundle(tmp))   # no free_chunk
        self.assertTrue(res.ok, res.reason)
        self.assertNotIn("on_segment", seen["enc"][0][1])
