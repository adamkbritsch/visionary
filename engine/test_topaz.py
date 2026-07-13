import unittest
from unittest import mock
import topaz
from topaz import build_filter, build_command, build_env, summarize, is_valid_upscale


class BuildFilter(unittest.TestCase):
    def test_matches_sdr_xq_digital_preset(self):
        # recoverOriginalDetail 45 -> blend .45, compress 8 -> .08, detail 2 -> .02, dehalo 5 -> .05
        self.assertEqual(
            build_filter(),
            "tvai_up=model=prob-4:scale=2:device=-2:compression=0.08:details=0.02:halo=0.05:blend=0.45",
        )

    def test_overrides(self):
        self.assertIn("blend=0.5", build_filter(blend=0.5))

    def test_fit_height_chains_lanczos_scale_to_exact_4k(self):
        vf = build_filter(scale=4, fit_height=2160)
        self.assertTrue(vf.startswith("tvai_up="))
        self.assertIn(",scale=-2:2160:flags=lanczos", vf)   # final exact-4K fit, aspect preserved

    def test_no_fit_height_is_just_tvai(self):
        self.assertNotIn(",scale=", build_filter(scale=2))   # 1080p×2 already 2160 → no fit


class BuildEnv(unittest.TestCase):
    def test_points_both_model_vars_at_models_dir(self):
        env = build_env("/m")
        self.assertEqual(env["TVAI_MODEL_DIR"], "/m")
        self.assertEqual(env["TVAI_MODEL_DATA_DIR"], "/m")


class BuildCommand(unittest.TestCase):
    def test_uses_hq_10bit_encoder(self):
        cmd = build_command("/ff", "/in.mkv", "/out.mov", "tvai_up=model=prob-4")
        self.assertEqual(cmd[0], "/ff")                            # normal priority (topaz nice removed)
        self.assertEqual(cmd[cmd.index("-i") + 1], "/in.mkv")
        self.assertEqual(cmd[cmd.index("-vf") + 1], "tvai_up=model=prob-4")
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "prores_videotoolbox")
        self.assertEqual(cmd[cmd.index("-profile:v") + 1], "hq")      # 422 HQ (was xq)
        self.assertEqual(cmd[cmd.index("-pix_fmt") + 1], "p210le")    # 10-bit 4:2:2 (was p416le 16-bit)
        self.assertEqual(cmd[cmd.index("-allow_sw") + 1], "1")
        self.assertEqual(cmd[-1], "/out.mov")


class BuildCfrCommand(unittest.TestCase):
    def test_constant_frame_rate_at_source_rate(self):
        cmd = topaz.build_cfr_command("/ff", "/in.mp4", "/out.mp4",
                                      rate="24000/1001", pix="yuv420p")
        self.assertEqual(cmd[0], "/ff")
        self.assertEqual(cmd[cmd.index("-i") + 1], "/in.mp4")
        self.assertEqual(cmd[-1], "/out.mp4")
        self.assertEqual(cmd[cmd.index("-r") + 1], "24000/1001")   # the source's OWN rate
        self.assertEqual(cmd[cmd.index("-fps_mode") + 1], "cfr")   # constant, not VFR
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        self.assertEqual(cmd[cmd.index("-pix_fmt") + 1], "yuv420p")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")       # audio untouched
        # Subtitles are DELIBERATELY not carried through CFR (re-attached from the original at
        # remux) — no subtitle map, no -c:s. Copying them through -fps_mode cfr risked a mux abort
        # and can't land in an MP4 CFR (bitmap PGS) anyway.
        self.assertNotIn("-c:s", cmd)
        self.assertNotIn("0:s?", cmd)
        self.assertNotIn("0:s", cmd)

    def test_ten_bit_and_color_preserved_for_hdr(self):
        cmd = topaz.build_cfr_command("/ff", "/in.mkv", "/out.mkv", rate="24000/1001",
                                      pix="yuv420p10le",
                                      color={"transfer": "smpte2084", "primaries": "bt2020",
                                             "space": "bt2020nc"})
        self.assertEqual(cmd[cmd.index("-pix_fmt") + 1], "yuv420p10le")   # stays 10-bit
        self.assertIn("smpte2084", cmd)                                   # stays HDR
        self.assertIn("bt2020", cmd)

    def test_unknown_rate_omits_r_flag(self):
        cmd = topaz.build_cfr_command("/ff", "/in.mp4", "/out.mp4", rate=None, pix="yuv420p")
        self.assertNotIn("-r", cmd)                 # let -fps_mode cfr use the input rate
        self.assertEqual(cmd[cmd.index("-fps_mode") + 1], "cfr")


class Verify(unittest.TestCase):
    SAMPLE = '{"streams":[{"codec_type":"audio","codec_name":"pcm_s24le"},' \
             '{"codec_type":"video","codec_name":"prores","profile":"4444 XQ",' \
             '"width":3840,"height":2160}]}'

    def test_summarize_picks_video_stream(self):
        self.assertEqual(
            summarize(self.SAMPLE),
            {"codec": "prores", "profile": "4444 XQ", "width": 3840, "height": 2160},
        )

    def test_valid_4k_prores_xq_passes(self):
        self.assertTrue(is_valid_upscale(summarize(self.SAMPLE)))

    def test_non_prores_codec_fails(self):
        # wrong codec is rejected; profile string is intentionally NOT checked anymore
        self.assertFalse(is_valid_upscale({"codec": "h264", "width": 3840, "height": 2160}))

    def test_any_prores_profile_at_4k_passes(self):
        # general upscaler: any 4K-class ProRes profile counts (don't loop on a good encode)
        self.assertTrue(is_valid_upscale({"codec": "prores", "profile": "HQ", "width": 3840, "height": 2160}))
        self.assertTrue(is_valid_upscale({"codec": "prores", "profile": "XQ", "width": 2880, "height": 2160}))

    def test_not_upscaled_resolution_fails(self):
        # a 1080p-wide output means it didn't actually upscale toward 4K
        self.assertFalse(is_valid_upscale({"codec": "prores", "profile": "4444 XQ", "width": 1920, "height": 1080}))


class PlanSegments(unittest.TestCase):
    """Scene-cut checkpoint planning (resumable encode). Segments must cover [0,total),
    start on cuts, and be >= the target so a kill loses at most one chunk."""
    def test_no_cuts_is_one_segment(self):
        import topaz
        self.assertEqual(topaz.plan_segments(1000, 24, []), [(0, 1000)])

    def test_cuts_below_target_are_not_split(self):
        import topaz
        self.assertEqual(topaz.plan_segments(5000, 24, [500, 1000], target_seconds=90), [(0, 5000)])

    def test_splits_on_cuts_at_least_target_apart(self):
        import topaz
        # target 3s @24fps = 72 frames; cuts at 207/285(78 from 207)/341(56<72, skipped)
        self.assertEqual(topaz.plan_segments(384, 24, [207, 285, 341], target_seconds=3),
                         [(0, 207), (207, 285), (285, 384)])

    def test_segments_are_contiguous_and_cover_everything(self):
        import topaz
        segs = topaz.plan_segments(10000, 24, [2200, 4800, 7200], target_seconds=90)
        self.assertEqual(segs[0][0], 0)
        self.assertEqual(segs[-1][1], 10000)
        self.assertTrue(all(segs[i][1] == segs[i + 1][0] for i in range(len(segs) - 1)))

    def test_empty_when_no_frames(self):
        import topaz
        self.assertEqual(topaz.plan_segments(0, 24, [100]), [])


class RunsToCompletion(unittest.TestCase):
    """upscale_resumable has NO mid-run deadline (the 90-min turn system is gone —
    user-dictated): once started, it plans and encodes straight through."""
    def _run(self, tmp):
        import topaz
        from unittest import mock
        with mock.patch.object(topaz, "media_timing", return_value=(24.0, 100.0)), \
             mock.patch.object(topaz, "_frame_count", return_value=0), \
             mock.patch.object(topaz, "total_frames", return_value=2400), \
             mock.patch.object(topaz, "_cached_scene_frames", return_value=[1200]), \
             mock.patch.object(topaz, "source_color", return_value=None), \
             mock.patch.object(topaz, "_run_ffmpeg") as rf:
            rf.return_value = (1, 0, False, "encode error (harness)")
            plan_seen = {}
            res = topaz.upscale_resumable("/in.mp4", segdir=tmp,
                                          on_plan=lambda ends, tot: plan_seen.update(ends=ends, tot=tot),
                                          target_seconds=10)
            return res, rf, plan_seen

    def test_plans_and_starts_encoding_immediately(self):
        import tempfile
        res, rf, plan = self._run(tempfile.mkdtemp())
        rf.assert_called()                                # attempted the first segment at once
        self.assertEqual(plan["tot"], 2400)               # on_plan fired with the exact total
        self.assertEqual(plan["ends"], [1200, 2400])      # cumulative segment ends

    def test_no_deadline_parameter_anymore(self):
        import inspect, topaz
        self.assertNotIn("deadline", inspect.signature(topaz.upscale_resumable).parameters)


class SegmentManifest(unittest.TestCase):
    """No-concat: Topaz writes a manifest; stage_done = every chunk present + exact frames."""
    def test_roundtrip_and_completeness(self):
        import topaz, tempfile, os
        from unittest import mock
        d = tempfile.mkdtemp()
        entries = [{"file": "seg_0000.mov", "start": 0, "frames": 100},
                   {"file": "seg_0001.mov", "start": 100, "frames": 150}]
        topaz.write_manifest(d, 23.976, entries)
        m = topaz.read_manifest(d)
        self.assertEqual(m["total_frames"], 250)        # sum of ACTUAL counts
        self.assertEqual([s["frames"] for s in m["segments"]], [100, 150])
        for s in m["segments"]:
            open(os.path.join(d, s["file"]), "w").close()
        with mock.patch.object(topaz, "_frame_count", side_effect=lambda p: 100 if p.endswith("seg_0000.mov") else 150):
            self.assertTrue(topaz.segments_complete(d))      # all chunks valid
        os.remove(os.path.join(d, "seg_0001.mov"))
        with mock.patch.object(topaz, "_frame_count", return_value=100):
            self.assertFalse(topaz.segments_complete(d))     # a missing chunk → not done

    def test_no_manifest_is_incomplete(self):
        import topaz, tempfile
        self.assertFalse(topaz.segments_complete(tempfile.mkdtemp()))


if __name__ == "__main__":
    unittest.main()


class LowPrioCfr(unittest.TestCase):
    """The PREFETCHER's CFR encodes run niced + thread-capped so they can never starve the
    in-flight Topaz encode of CPU (full-core background x264 slowed episodes ~15-20%)."""
    def test_low_prio_clamps_qos_and_threads(self):
        cmd = topaz.build_cfr_command("/ff", "/in.mp4", "/out.mp4",
                                      rate="24000/1001", pix="yuv420p", low_prio=True)
        self.assertEqual(cmd[:3], ["/usr/sbin/taskpolicy", "-c", "background"])  # E-cores only
        self.assertEqual(cmd[cmd.index("-threads") + 1], "4")

    def test_foreground_cfr_stays_full_speed(self):
        cmd = topaz.build_cfr_command("/ff", "/in.mp4", "/out.mp4",
                                      rate="24000/1001", pix="yuv420p")
        self.assertNotIn("nice", cmd)
        self.assertNotIn("-threads", cmd)


class CfrFastPath(unittest.TestCase):
    """Already-CFR 4:2:0 sources stream-COPY instead of a wasteful full re-encode."""
    def _probe(self, avg, r, pix):
        js = ('{"streams":[{"avg_frame_rate":"%s","r_frame_rate":"%s","pix_fmt":"%s"}]}'
              % (avg, r, pix))
        return mock.patch.object(topaz.subprocess, "run", return_value=mock.Mock(stdout=js))

    def test_detects_constant_frame_rate_420(self):
        with self._probe("24000/1001", "24000/1001", "yuv420p10le"):
            self.assertTrue(topaz._is_already_cfr("x.mkv"))

    def test_variable_frame_rate_still_reencodes(self):
        with self._probe("23976/1000", "24000/1001", "yuv420p"):   # avg != r → VFR
            self.assertFalse(topaz._is_already_cfr("x.mkv"))

    def test_wide_chroma_still_reencodes(self):
        with self._probe("24000/1001", "24000/1001", "yuv422p10le"):  # 4:2:2 → normalize
            self.assertFalse(topaz._is_already_cfr("x.mkv"))

    def test_unreadable_falls_back(self):
        with self._probe("0/0", "0/0", "yuv420p"):
            self.assertFalse(topaz._is_already_cfr("x.mkv"))

    def test_copy_command_is_stream_copy_no_reencode(self):
        cmd = topaz.build_cfr_copy_command("/ff", "/in.mkv", "/out.mkv")
        self.assertIn("copy", cmd); self.assertNotIn("libx264", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")

    def test_to_cfr_streamcopies_when_already_cfr(self):
        captured = {}
        def fake_run(cmd, env, *, abort=None, on_progress=None):
            captured["cmd"] = cmd; return (0, 100, False, "")
        with mock.patch.object(topaz, "_is_already_cfr", return_value=True), \
             mock.patch.object(topaz, "_run_ffmpeg", side_effect=fake_run), \
             mock.patch.object(topaz, "is_cfr_ready", return_value=True), \
             mock.patch.object(topaz, "_fps_fraction", return_value="24000/1001"):
            r = topaz.to_cfr("src.mkv", "dst.mkv")
        self.assertTrue(r.ok)
        self.assertIn("copy", captured["cmd"]); self.assertNotIn("libx264", captured["cmd"])

    def test_to_cfr_reencodes_when_not_cfr(self):
        captured = {}
        def fake_run(cmd, env, *, abort=None, on_progress=None):
            captured["cmd"] = cmd; return (0, 100, False, "")
        with mock.patch.object(topaz, "_is_already_cfr", return_value=False), \
             mock.patch.object(topaz, "_run_ffmpeg", side_effect=fake_run), \
             mock.patch.object(topaz, "is_cfr_ready", return_value=True), \
             mock.patch.object(topaz, "_fps_fraction", return_value="24000/1001"), \
             mock.patch.object(topaz, "_cfr_pix_fmt", return_value="yuv420p"), \
             mock.patch.object(topaz, "source_color", return_value=None):
            topaz.to_cfr("src.mkv", "dst.mkv")
        self.assertIn("libx264", captured["cmd"])

    def test_frame_count_uses_mkv_tag_not_slow_decode(self):
        # MKV: nb_frames is N/A -> the NUMBER_OF_FRAMES tag gives the exact count with NO
        # `-count_frames` full decode (which times out on HEVC and broke the CFR fast-path).
        calls = []
        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            if "stream=nb_frames" in cmd: return mock.Mock(stdout="N/A\n")
            if "stream_tags=NUMBER_OF_FRAMES" in cmd: return mock.Mock(stdout="60961\n")
            return mock.Mock(stdout="")
        with mock.patch.object(topaz.subprocess, "run", side_effect=fake_run):
            n = topaz._frame_count("x.mkv")
        self.assertEqual(n, 60961)
        self.assertFalse(any("-count_frames" in c for c in calls))   # never decoded
