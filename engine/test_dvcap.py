import unittest
from unittest import mock

import dvcap
import remux


class TrailingComma(unittest.TestCase):
    def test_a_packet_count_past_ffprobe_8s_trailing_comma(self):
        import os, tempfile
        f = os.path.join(tempfile.mkdtemp(), "seg.hevc")
        with open(f, "wb") as fh:
            fh.write(b"x")
        with mock.patch.object(dvcap.subprocess, "run", return_value=mock.Mock(stdout="219,\n")):
            self.assertEqual(dvcap.count_hevc_frames(f), 219)


class X265Command(unittest.TestCase):
    def cmd(self, **kw):
        return dvcap.build_x265_command("/x265", "/r.bin", "/out.hevc", 50, **kw)

    def test_native_dv_mode_with_rpu(self):
        c = self.cmd()
        self.assertIn("--dolby-vision-profile", c)
        self.assertEqual(c[c.index("--dolby-vision-profile") + 1], "8.1")
        self.assertEqual(c[c.index("--dolby-vision-rpu") + 1], "/r.bin")

    def test_hard_vbv_ceiling_from_cap(self):
        c = self.cmd()
        self.assertEqual(c[c.index("--vbv-maxrate") + 1], "50000")   # 50 Mbps -> kbps
        self.assertEqual(c[c.index("--vbv-bufsize") + 1], "50000")   # 1-second window

    def test_dv_conformance_flags_present(self):
        c = self.cmd()
        for flag in ("--repeat-headers", "--aud", "--hrd", "--y4m"):
            self.assertIn(flag, c)

    def test_hdr10_signaling(self):
        c = self.cmd()
        self.assertEqual(c[c.index("--transfer") + 1], "smpte2084")
        self.assertEqual(c[c.index("--colorprim") + 1], "bt2020")
        self.assertEqual(c[c.index("--colormatrix") + 1], "bt2020nc")
        self.assertEqual(c[c.index("--output-depth") + 1], "10")

    def test_master_display_falls_back_to_resolve_constant(self):
        c = self.cmd()
        self.assertEqual(c[c.index("--master-display") + 1], dvcap.FALLBACK_MASTER_DISPLAY)
        c2 = self.cmd(master_display="G(1,2)B(3,4)R(5,6)WP(7,8)L(9,10)")
        self.assertEqual(c2[c2.index("--master-display") + 1], "G(1,2)B(3,4)R(5,6)WP(7,8)L(9,10)")

    def test_max_cll_only_when_known(self):
        self.assertNotIn("--max-cll", self.cmd())
        c = self.cmd(max_cll="1000,400")
        self.assertEqual(c[c.index("--max-cll") + 1], "1000,400")


class MasteringConversion(unittest.TestCase):
    def test_p3d65_fractions_to_x265_units(self):
        sd = {"green_x": "13250/50000", "green_y": "34500/50000",
              "blue_x": "7500/50000", "blue_y": "3000/50000",
              "red_x": "34000/50000", "red_y": "16000/50000",
              "white_point_x": "15635/50000", "white_point_y": "16450/50000",
              "max_luminance": "1000/1", "min_luminance": "1/10000"}
        self.assertEqual(dvcap.mastering_to_x265(sd),
                         "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)")

    def test_partial_side_data_returns_none(self):
        self.assertIsNone(dvcap.mastering_to_x265({"green_x": "13250/50000"}))


class ProgressParse(unittest.TestCase):
    def test_progress_line(self):
        self.assertEqual(dvcap.parse_x265_progress("123 frames: 9.87 fps, 24000.11 kb/s"), 123)
        self.assertIsNone(dvcap.parse_x265_progress("x265 [info]: HEVC encoder"))

    def test_final_summary(self):
        self.assertEqual(dvcap.parse_x265_encoded(
            "encoded 40257 frames in 3200.11s (12.58 fps), 26123.44 kb/s, Avg QP:18.11"), 40257)
        self.assertIsNone(dvcap.parse_x265_encoded("no summary here"))


class PeakMeasurement(unittest.TestCase):
    def test_buckets_per_second_and_reports_max(self):
        csv = "\n".join(["packet,0.000000,1000000", "packet,0.500000,1000000",   # sec 0: 2 MB
                         "packet,1.100000,500000",                                # sec 1: 0.5 MB
                         "packet,2.000000,3000000"])                              # sec 2: 3 MB = 24 Mbps
        self.assertAlmostEqual(dvcap.peak_1s_mbps_from_packets(csv), 24.0)

    def test_skips_na_pts_rows(self):
        csv = "packet,N/A,999999999\npacket,0.0,125000"
        self.assertAlmostEqual(dvcap.peak_1s_mbps_from_packets(csv), 1.0)

    def test_empty_is_zero(self):
        self.assertEqual(dvcap.peak_1s_mbps_from_packets(""), 0.0)

    def test_peak_gate(self):
        self.assertTrue(dvcap.peak_ok(52.0, 50))      # within 15% tolerance
        self.assertFalse(dvcap.peak_ok(58.0, 50))     # over it
        self.assertFalse(dvcap.peak_ok(0.0, 50))      # unmeasurable = NOT ok (never ship blind)


class CappedMuxCommands(unittest.TestCase):
    def test_mp4_mux_signals_dv_and_fps(self):
        c = remux.build_capped_mux_command("/MP4Box", "/v.hevc", "24000/1001", "/t.mp4", "/o.mp4")
        self.assertEqual(c[c.index("-add") + 1], "/v.hevc:dvp=8.1:fps=24000/1001")
        self.assertIn("-inter", c)          # playback interleave kept
        self.assertEqual(c[c.index("-new") + 1], "/o.mp4")

    def test_no_xps_inband_ever(self):
        # xps_inband => hev1 sample entry => SHIELD refuses to direct-play (S05E24 regression)
        for c in (remux.build_capped_mux_command("/MP4Box", "/v.hevc", "24/1", "/t.mp4", "/o.mp4"),
                  remux.build_capped_video_mux_command("/MP4Box", "/v.hevc", "24/1", "/o.dv.mp4")):
            self.assertNotIn("xps_inband", " ".join(c))

    def test_video_only_wrap_for_mkv_path(self):
        c = remux.build_capped_video_mux_command("/MP4Box", "/v.hevc", "24000/1001", "/o.dv.mp4")
        self.assertEqual(c, ["/MP4Box", "-add", "/v.hevc:dvp=8.1:fps=24000/1001",
                             "-new", "/o.dv.mp4"])

    def test_parse_streams_reports_video_tag(self):
        pj = '{"streams": [{"codec_type": "video", "codec_tag_string": "hev1"}]}'
        self.assertEqual(remux.parse_streams(pj)["video_tag"], "hev1")


if __name__ == "__main__":
    unittest.main()


class SmartLoudnessBoost(unittest.TestCase):
    """Remux-stage loudness normalization: per-item measured gain to the target, boost-only,
    limiter-capped, MP4 path only (MKV = lossless audio, never transcoded)."""

    def test_parse_integrated_lufs(self):
        err = "  Integrated loudness:\n    I:         -23.2 LUFS\n    Threshold: -34.6 LUFS"
        self.assertEqual(remux.parse_integrated_lufs(err), -23.2)
        self.assertIsNone(remux.parse_integrated_lufs("no loudness here"))

    def test_gain_boost_only_and_clamped(self):
        self.assertEqual(remux.boost_gain_db(-23.2, -16), 7.2)     # the Office pilot case
        self.assertEqual(remux.boost_gain_db(-14.0, -16), 0.0)     # louder than target → NEVER attenuate
        self.assertEqual(remux.boost_gain_db(-32.0, -16), 12.0)    # clamped to max
        self.assertEqual(remux.boost_gain_db(-16.2, -16), 0.0)     # negligible → skip re-encode
        self.assertEqual(remux.boost_gain_db(None, -16), 0.0)      # unmeasurable → untouched
        self.assertEqual(remux.boost_gain_db(-23.0, None), 0.0)    # target off → untouched
        self.assertEqual(remux.boost_gain_db(-23.0, 0), 0.0)

    def test_filter_is_gain_plus_limiter(self):
        f = remux.build_audio_boost_filter(7.2)
        self.assertTrue(f.startswith("volume=7.20dB,alimiter="))
        self.assertIn("limit=0.794", f)                            # -2 dB ceiling for the stings

    def test_extract_boosted_reencodes_audio_only(self):
        c = remux.build_extract_command("/ff", "/cfr.mp4", "/orig.mp4", "/t.mp4", gain_db=7.2)
        self.assertIn("-filter:a", c)
        self.assertEqual(c[c.index("-c:a") + 1], "aac_at")
        self.assertEqual(c[c.index("-c:s") + 1], "mov_text")       # subs untouched

    def test_extract_unboosted_is_pure_copy(self):
        c = remux.build_extract_command("/ff", "/cfr.mp4", "/orig.mp4", "/t.mp4")
        self.assertNotIn("-filter:a", c)
        self.assertNotIn("aac_at", c)                              # bit-exact copy path preserved

    def test_mkv_mux_never_boosts(self):
        c = remux.build_mkv_mux_command("/ff", "/dv.mp4", "/cfr.mkv", "/orig.mkv", "/o.mkv")
        self.assertNotIn("-filter:a", " ".join(c))                 # lossless audio is sacred
        self.assertIn("copy", c)


class Segmentation(unittest.TestCase):
    """Resumable segmented x265: planning, frame-exact seek, RPU slicing, concat."""

    def test_plan_covers_all_frames_contiguously(self):
        segs = dvcap.plan_segments(41071, "24000/1001", seg_seconds=300)
        self.assertEqual(segs[0][0], 0)
        self.assertEqual(segs[-1][1], 41071)
        for (a, b), (c, d) in zip(segs, segs[1:]):     # contiguous, no gaps/overlaps
            self.assertEqual(b, c)
        self.assertEqual(sum(b - a for a, b in segs), 41071)

    def test_plan_short_input_single_segment(self):
        self.assertEqual(dvcap.plan_segments(200, "24000/1001"), [(0, 200)])
        self.assertEqual(dvcap.plan_segments(0, "24000/1001"), [])

    def test_midpoint_seek_is_frame_exact(self):
        fps = 24000 / 1001
        self.assertIsNone(dvcap.seg_seek_seconds(0, "24000/1001"))     # frame 0 → no seek
        for a in (1, 2, 7193, 30000):
            ss = dvcap.seg_seek_seconds(a, "24000/1001")
            self.assertLess((a - 1) / fps, ss)                         # strictly after frame a-1
            self.assertLess(ss, a / fps)                               # strictly before frame a
        # the a/fps trap this defends against: frame 2 pts rounds UP at 6 decimals
        self.assertGreater(round(2 * 1001 / 24000, 6), 2 * 1001 / 24000)

    def test_seek_ignores_container_start_time(self):
        # input -ss is ALREADY file-start-relative; a start_time term would double-count and
        # shift every non-first segment (empirically start_time=2.0 → +48 frame drift)
        import inspect
        self.assertNotIn("start_time", inspect.signature(dvcap.seg_seek_seconds).parameters)
        self.assertNotIn("start_time", inspect.signature(dvcap.build_seg_decode_command).parameters)

    def test_rpu_edit_config(self):
        self.assertEqual(dvcap.build_rpu_edit_config(7200, 14400, 41071),
                         {"remove": ["0-7199", "14400-41070"]})
        self.assertEqual(dvcap.build_rpu_edit_config(0, 7200, 41071),
                         {"remove": ["7200-41070"]})              # head: only tail removed
        self.assertEqual(dvcap.build_rpu_edit_config(35000, 41071, 41071),
                         {"remove": ["0-34999"]})                 # last: only head removed
        self.assertEqual(dvcap.build_rpu_edit_config(0, 500, 500), {"remove": []})   # whole file

    def test_decode_command_has_seek_and_frame_bound(self):
        c = dvcap.build_seg_decode_command("/ff", "/r.mov", 7193, 7193, "24000/1001")
        self.assertIn("-ss", c)
        self.assertEqual(c[c.index("-frames:v") + 1], "7193")
        self.assertEqual(c[c.index("-f") + 1], "yuv4mpegpipe")
        c0 = dvcap.build_seg_decode_command("/ff", "/r.mov", 0, 100, "24000/1001")
        self.assertNotIn("-ss", c0)                               # frame 0 → no seek

    def test_render_decode_is_unchanged_without_native_options(self):
        # pinned LITERALLY (what HEAD built before `decode` existed, plus the -fps_mode
        # passthrough fix): comparing the function with itself could never catch a change
        want = ["/ff", "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", "299.987188",
                "-i", "/r.mov", "-map", "0:v:0", "-frames:v", "100",
                "-fps_mode", "passthrough", "-pix_fmt", "yuv420p10le", "-strict", "-1", "-f", "yuv4mpegpipe", "-"]
        for extra in ((), (None,), ({},), ({"chunks": [], "vf": ""},)):   # every "no native" form
            with self.subTest(decode=extra):
                self.assertEqual(dvcap.build_seg_decode_command(
                    "/ff", "/r.mov", 7193, 100, "24000/1001", *extra), want)
        self.assertEqual(dvcap.build_seg_decode_command("/ff", "/r.mov", 0, 100, "24000/1001"),
                         ["/ff", "-hide_banner", "-nostdin", "-loglevel", "error",
                          "-i", "/r.mov", "-map", "0:v:0", "-frames:v", "100",
                          "-fps_mode", "passthrough", "-pix_fmt", "yuv420p10le", "-strict", "-1", "-f", "yuv4mpegpipe", "-"])

    CHUNKS = [[0, "/c0.mov"], [2217, "/c1.mov"], [4398, "/c2.mov"], [6000, None]]

    def test_native_decode_reads_the_chunk_holding_the_segment(self):
        c = dvcap.build_seg_decode_command("/ff", "/ignored", 2217, 2181, "24000/1001",
                                           {"chunks": self.CHUNKS, "vf": "lut3d=file=/l.cube"})
        self.assertEqual(c[c.index("-i") + 1], "/c1.mov")
        self.assertNotIn("-ss", c)                                # chunk start → no seek at all
        self.assertEqual(c[c.index("-vf") + 1], "lut3d=file=/l.cube")
        self.assertLess(c.index("-vf"), c.index("-pix_fmt"))
        self.assertEqual(c[c.index("-frames:v") + 1], "2181")

    def test_native_decode_seeks_within_its_chunk_only(self):
        c = dvcap.build_seg_decode_command("/ff", "/ignored", 2300, 50, "24000/1001",
                                           {"chunks": self.CHUNKS})
        self.assertEqual(c[c.index("-i") + 1], "/c1.mov")
        self.assertEqual(c[c.index("-ss") + 1], f"{(83 - 0.5) * 1001 / 24000:.6f}")   # local 83

    def test_a_segment_crossing_a_chunk_end_is_refused(self):
        with self.assertRaises(ValueError):
            dvcap.chunk_for(self.CHUNKS, 2200, 100)               # 2200..2299 spans c0|c1
        with self.assertRaises(ValueError):
            dvcap.chunk_for(self.CHUNKS, 6000, 1)                 # past the end sentinel

    def test_concat_missing_segment_fails(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            good = os.path.join(td, "s0.hevc"); open(good, "wb").write(b"\x00\x00\x01x")
            out = os.path.join(td, "out.hevc")
            ok, why = dvcap.concat_segments([good, os.path.join(td, "missing.hevc")], out)
            self.assertFalse(ok); self.assertIn("missing", why)

    def test_concat_joins_in_order(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            a = os.path.join(td, "a"); b = os.path.join(td, "b")
            open(a, "wb").write(b"AAAA"); open(b, "wb").write(b"BBBB")
            out = os.path.join(td, "o")
            ok, _ = dvcap.concat_segments([a, b], out)
            self.assertTrue(ok)
            self.assertEqual(open(out, "rb").read(), b"AAAABBBB")

    def test_segmented_resumes_completed_segments(self):
        # a pre-existing seg with the right frame count is skipped; only missing ones encode
        import tempfile, os
        TOTAL, SS = 200, 4
        plan = dvcap.plan_segments(TOTAL, "24000/1001", SS)     # plan-aware n per segment
        n_of = {i: b - a for i, (a, b) in enumerate(plan)}
        idx = lambda p: int(os.path.basename(p).split("_")[1].split(".")[0])
        with tempfile.TemporaryDirectory() as td:
            segdir = os.path.join(td, "segs"); os.makedirs(segdir)
            open(os.path.join(segdir, "seg_0000.hevc"), "wb").write(b"x")   # seg 0 pre-done
            encoded = []
            def fake_pipe(dec, enc, out, abort, on_frame):
                encoded.append(out); open(out, "wb").write(b"y")
                return n_of[idx(out)], "ok", []                 # returns the exact planned n
            with mock.patch.object(dvcap, "count_hevc_frames",
                                   side_effect=lambda p, ffprobe=None: n_of[0] if "seg_0000" in p else 0), \
                 mock.patch.object(dvcap, "slice_rpu", return_value=(True, "ok")), \
                 mock.patch.object(dvcap, "_encode_pipe", side_effect=fake_pipe), \
                 mock.patch.object(dvcap, "concat_segments", return_value=(True, "ok")):
                ok, frames, why = dvcap.encode_capped_segmented(
                    "/r.mov", "/rpu.bin", os.path.join(td, "out.hevc"), 50,
                    segdir=segdir, total_frames=TOTAL, fps="24000/1001", seg_seconds=SS)
            self.assertTrue(ok, why)
            self.assertEqual(len(encoded), len(plan) - 1)       # every seg EXCEPT the pre-done seg 0
            self.assertNotIn("seg_0000", " ".join(encoded))
            self.assertEqual(frames, TOTAL)

    def test_segmented_frame_mismatch_fails_loudly(self):
        # a short/drifted decode (got != n) must FAIL — never silently misalign the RPU
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            segdir = os.path.join(td, "segs"); os.makedirs(segdir)
            def short_pipe(dec, enc, out, abort, on_frame):
                open(out, "wb").write(b"y"); return 5, "ok", []       # far fewer than any planned n
            with mock.patch.object(dvcap, "count_hevc_frames", return_value=0), \
                 mock.patch.object(dvcap, "slice_rpu", return_value=(True, "ok")), \
                 mock.patch.object(dvcap, "_encode_pipe", side_effect=short_pipe):
                ok, frames, why = dvcap.encode_capped_segmented(
                    "/r.mov", "/rpu.bin", os.path.join(td, "out.hevc"), 50,
                    segdir=segdir, total_frames=100, fps="24000/1001", seg_seconds=4)
            self.assertFalse(ok); self.assertIn("mismatch", why)

    def test_should_pause_yields_between_segments(self):
        # Resolve preemption (like topaz's pause): a True should_pause between segments
        # returns a benign 'paused:' — every finished segment kept, no concat, resume
        # re-enters right where it left off.
        import tempfile, os
        TOTAL, SS = 200, 4
        plan = dvcap.plan_segments(TOTAL, "24000/1001", SS)
        n_of = {i: b - a for i, (a, b) in enumerate(plan)}
        idx = lambda p: int(os.path.basename(p).split("_")[1].split(".")[0])
        with tempfile.TemporaryDirectory() as td:
            segdir = os.path.join(td, "segs"); os.makedirs(segdir)
            calls = []
            def fake_pipe(dec, enc, out, abort, on_frame):
                calls.append(out); open(out, "wb").write(b"y")
                return n_of[idx(out)], "ok", []
            with mock.patch.object(dvcap, "count_hevc_frames", return_value=0), \
                 mock.patch.object(dvcap, "slice_rpu", return_value=(True, "ok")), \
                 mock.patch.object(dvcap, "_encode_pipe", side_effect=fake_pipe), \
                 mock.patch.object(dvcap, "concat_segments",
                                   side_effect=AssertionError("paused run must not concat")):
                ok, frames, why = dvcap.encode_capped_segmented(
                    "/r.mov", "/rpu.bin", os.path.join(td, "out.hevc"), 50,
                    segdir=segdir, total_frames=TOTAL, fps="24000/1001", seg_seconds=SS,
                    should_pause=lambda: len(calls) >= 2)   # yield after 2 encoded segments
            self.assertFalse(ok)
            self.assertTrue(why.startswith("paused:"), why)
            self.assertEqual(len(calls), 2)                       # stopped BETWEEN segments
            self.assertEqual(frames, n_of[0] + n_of[1])           # finished work reported/kept

    def test_on_plan_fires_cumulative_segment_ends(self):
        # the dashboard's notched segment bar (same as topaz) needs the cumulative segment END
        # frames + the exact total, fired ONCE after planning — before any progress tick.
        import tempfile, os
        TOTAL, SS = 200, 4
        plan = dvcap.plan_segments(TOTAL, "24000/1001", SS)
        n_of = {i: b - a for i, (a, b) in enumerate(plan)}
        idx = lambda p: int(os.path.basename(p).split("_")[1].split(".")[0])
        with tempfile.TemporaryDirectory() as td:
            segdir = os.path.join(td, "segs"); os.makedirs(segdir)
            for i in range(len(plan)):                          # all pre-done → loop just skips + concats
                open(os.path.join(segdir, f"seg_{i:04d}.hevc"), "wb").write(b"x")
            got = {}
            with mock.patch.object(dvcap, "count_hevc_frames",
                                   side_effect=lambda p, ffprobe=None: n_of[idx(p)]), \
                 mock.patch.object(dvcap, "concat_segments", return_value=(True, "ok")):
                ok, frames, why = dvcap.encode_capped_segmented(
                    "/r.mov", "/rpu.bin", os.path.join(td, "out.hevc"), 50,
                    segdir=segdir, total_frames=TOTAL, fps="24000/1001", seg_seconds=SS,
                    on_plan=lambda ends, total: got.update(ends=ends, total=total))
            self.assertTrue(ok, why)
            self.assertEqual(got["total"], TOTAL)
            self.assertEqual(got["ends"], [b for (_a, b) in plan])   # cumulative ends
            self.assertEqual(got["ends"][-1], TOTAL)                  # last end == total

    def test_plan_uses_topaz_boundaries(self):
        # the remux segments LINE UP with the topaz scene-cut boundaries (NOT evenly spaced)
        segs = dvcap.plan_segments(1000, "24000/1001", boundaries=[137, 402, 651, 1000])
        self.assertEqual(segs, [(0, 137), (137, 402), (402, 651), (651, 1000)])

    def test_plan_boundaries_drift_safe(self):
        # RPU count slightly UNDER the topaz total → clamp + drop the now-empty tail
        self.assertEqual(dvcap.plan_segments(650, "24000/1001", boundaries=[137, 402, 651, 1000]),
                         [(0, 137), (137, 402), (402, 650)])
        # RPU count slightly OVER → pad the tail so the whole render is still covered
        self.assertEqual(dvcap.plan_segments(1100, "24000/1001", boundaries=[137, 402, 651, 1000]),
                         [(0, 137), (137, 402), (402, 651), (651, 1000), (1000, 1100)])
        # no boundaries → the ~seg_seconds fallback
        self.assertGreater(len(dvcap.plan_segments(48000, "24000/1001", seg_seconds=300)), 1)

    def test_manifest_segb_distinguishes_plans(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "r.mov"); open(f, "wb").write(b"x")
            m_a = dvcap.resume_manifest(f, 50, 1000, "24000/1001", 300, boundaries=[500, 1000])
            m_b = dvcap.resume_manifest(f, 50, 1000, "24000/1001", 300, boundaries=[300, 700, 1000])
            m_fb = dvcap.resume_manifest(f, 50, 1000, "24000/1001", 300, boundaries=None)
            self.assertNotEqual(m_a, m_b)                           # different cuts → wipe stale segments
            self.assertNotIn("segb", m_fb)                          # fallback → no segb (matches a pre-segb manifest)


class ResumeFingerprint(unittest.TestCase):
    """Persisted segments are resumed ONLY for the same render + params — a re-rendered
    dv_video passes the frame-count check but would concat WRONG CONTENT."""

    def test_matching_manifest_resumes(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            dv = os.path.join(td, "r.mov"); open(dv, "wb").write(b"x" * 100)
            seg = os.path.join(td, "segs")
            m = dvcap.resume_manifest(dv, 50, 41071, "24000/1001", 300)
            self.assertEqual(dvcap.ensure_segdir(seg, m), "fresh")
            open(os.path.join(seg, "seg_0000.hevc"), "wb").write(b"y")
            self.assertEqual(dvcap.ensure_segdir(seg, m), "resume")
            self.assertTrue(os.path.exists(os.path.join(seg, "seg_0000.hevc")))

    def test_rerendered_source_wipes_stale_segments(self):
        import tempfile, os, time
        with tempfile.TemporaryDirectory() as td:
            dv = os.path.join(td, "r.mov"); open(dv, "wb").write(b"x" * 100)
            seg = os.path.join(td, "segs")
            dvcap.ensure_segdir(seg, dvcap.resume_manifest(dv, 50, 41071, "24000/1001", 300))
            open(os.path.join(seg, "seg_0000.hevc"), "wb").write(b"y")
            open(dv, "wb").write(b"z" * 200)              # resolve re-rendered the source
            os.utime(dv, (time.time() + 5, time.time() + 5))
            m2 = dvcap.resume_manifest(dv, 50, 41071, "24000/1001", 300)
            self.assertEqual(dvcap.ensure_segdir(seg, m2), "fresh")
            self.assertFalse(os.path.exists(os.path.join(seg, "seg_0000.hevc")))   # wiped

    def test_cap_change_wipes(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            dv = os.path.join(td, "r.mov"); open(dv, "wb").write(b"x")
            seg = os.path.join(td, "segs")
            dvcap.ensure_segdir(seg, dvcap.resume_manifest(dv, 50, 100, "24/1", 300))
            open(os.path.join(seg, "seg_0000.hevc"), "wb").write(b"y")
            self.assertEqual(
                dvcap.ensure_segdir(seg, dvcap.resume_manifest(dv, 40, 100, "24/1", 300)), "fresh")
            self.assertFalse(os.path.exists(os.path.join(seg, "seg_0000.hevc")))

    def test_ordinary_manifest_carries_no_encode_source_key(self):
        # schema-stable: existing in-flight segdirs must keep resuming across the deploy
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            dv = os.path.join(td, "r.mov"); open(dv, "wb").write(b"x")
            self.assertNotIn("enc", dvcap.resume_manifest(dv, 50, 100, "24/1", 300))

    def test_encode_source_identity_invalidates_resume(self):
        # peak-gated rpu-only fallback: a changed SOURCE (the encoded video) must wipe
        # persisted segments even when the render (the RPU donor) is unchanged
        import tempfile, os, time
        with tempfile.TemporaryDirectory() as td:
            dv = os.path.join(td, "r.mov"); open(dv, "wb").write(b"x")
            src = os.path.join(td, "source_cfr.mkv"); open(src, "wb").write(b"s" * 100)
            seg = os.path.join(td, "segs")
            dvcap.ensure_segdir(seg, dvcap.resume_manifest(dv, 50, 100, "24/1", 300,
                                                           encode_source=src))
            open(os.path.join(seg, "seg_0000.hevc"), "wb").write(b"y")
            open(src, "wb").write(b"s" * 200)             # the source was re-fetched
            os.utime(src, (time.time() + 5, time.time() + 5))
            m2 = dvcap.resume_manifest(dv, 50, 100, "24/1", 300, encode_source=src)
            self.assertEqual(dvcap.ensure_segdir(seg, m2), "fresh")
            self.assertFalse(os.path.exists(os.path.join(seg, "seg_0000.hevc")))

    def test_whole_stream_budget_holds_at_the_settings_limit(self):
        # every output is budgeted as if it carries TrueHD (user-dictated): even the
        # MAXED max_peak_mbps setting + the measured-peak gate tolerance + the TrueHD
        # headroom must stay under the SHIELD's whole-stream DV ceiling
        import settings
        lo, hi = settings.LIMITS["max_peak_mbps"]
        self.assertLessEqual(hi * dvcap.PEAK_TOLERANCE + dvcap.TRUEHD_HEADROOM_MBPS,
                             dvcap.DV_STREAM_CEILING_MBPS)
        self.assertEqual(dvcap.RPU_SHIP_VIDEO_GATE_MBPS,
                         dvcap.DV_STREAM_CEILING_MBPS - dvcap.TRUEHD_HEADROOM_MBPS)


class NativeResumeIdentity(unittest.TestCase):
    """Native DV has no render to stat. Its resume identity is the native marker's id, which
    hashes the chunks, the generated RPU and the LUT — stat'ing the nominal chunk-0 path would
    miss a regenerated RPU and resume segments graded with the old one. Absent (every Resolve
    run), the manifest must be exactly the pre-native one, or every in-flight segdir would be
    wiped by the deploy."""

    def test_absent_native_id_is_the_pre_native_manifest(self):
        import json, os, tempfile
        with tempfile.TemporaryDirectory() as td:
            dv = os.path.join(td, "r.mov"); open(dv, "wb").write(b"x" * 100)
            st = os.stat(dv)
            pre_native = {"src": f"{st.st_size}:{int(st.st_mtime)}", "cap": 50, "frames": 100,
                          "fps": "24/1", "seg": 300, "crf": float(dvcap.X265_CRF),
                          "preset": dvcap.X265_PRESET, "segb": [50, 100]}
            seg = os.path.join(td, "segs"); os.makedirs(seg)
            with open(os.path.join(seg, "manifest.json"), "w") as f:
                json.dump(pre_native, f)                  # written by the pre-native code
            open(os.path.join(seg, "seg_0000.hevc"), "wb").write(b"y")
            for m in (dvcap.resume_manifest(dv, 50, 100, "24/1", 300, boundaries=[50, 100]),
                      dvcap.resume_manifest(dv, 50, 100, "24/1", 300, boundaries=[50, 100],
                                            native_id=None)):
                self.assertEqual(m, pre_native)           # no new key, no changed value
                self.assertEqual(dvcap.ensure_segdir(seg, m), "resume")
            self.assertTrue(os.path.exists(os.path.join(seg, "seg_0000.hevc")))

    def test_native_id_replaces_the_render_stat(self):
        import os, tempfile, time
        m = dvcap.resume_manifest("/no/such/seg_0000.mov", 50, 6000, "24000/1001", 300,
                                  boundaries=[2217, 4398, 6000], native_id="abc")
        self.assertEqual(m["src"], "native:abc")          # never "missing": the path is not stat'ed
        self.assertEqual(m["segb"], [2217, 4398, 6000])
        with tempfile.TemporaryDirectory() as td:
            c0 = os.path.join(td, "seg_0000.mov"); open(c0, "wb").write(b"x")
            m1 = dvcap.resume_manifest(c0, 50, 6000, "24000/1001", 300, native_id="abc")
            open(c0, "wb").write(b"x" * 50)               # the nominal path's stat changes...
            os.utime(c0, (time.time() + 5, time.time() + 5))
            m2 = dvcap.resume_manifest(c0, 50, 6000, "24000/1001", 300, native_id="abc")
            self.assertEqual(m1, m2)                      # ...the identity does not
            self.assertNotEqual(m1, dvcap.resume_manifest(c0, 50, 6000, "24000/1001", 300))

    def test_changed_native_id_wipes_stale_segments(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as td:
            seg = os.path.join(td, "segs")
            ma = dvcap.resume_manifest("/c0.mov", 50, 6000, "24000/1001", 300, native_id="abc")
            self.assertEqual(dvcap.ensure_segdir(seg, ma), "fresh")
            open(os.path.join(seg, "seg_0000.hevc"), "wb").write(b"y")
            self.assertEqual(dvcap.ensure_segdir(seg, ma), "resume")      # same id → resume
            mb = dvcap.resume_manifest("/c0.mov", 50, 6000, "24000/1001", 300, native_id="def")
            self.assertEqual(dvcap.ensure_segdir(seg, mb), "fresh")       # re-analysed → wipe
            self.assertFalse(os.path.exists(os.path.join(seg, "seg_0000.hevc")))


class ResumeRobustness(unittest.TestCase):
    """Kill-during-remux is the COMMON case for segmented remux — every persisted artifact must
    be atomic, and a transient probe must never destroy resume state (review-caught)."""

    def test_probe_failure_reads_as_zero_not_a_valid_frame_count(self):
        import subprocess as sp
        class R:  # simulate ffprobe crashing/timing out
            returncode = 1; stdout = ""; stderr = "boom"
        with mock.patch.object(dvcap.subprocess, "run", return_value=R()):
            info = dvcap.probe_video("/x.mov")
        self.assertEqual(info["frames"], 0)          # NOT a silent 0-that-looks-valid downstream

    def test_remux_guards_zero_probe_before_wiping_segdir(self):
        bad = {"frames": 0, "fps": "24000/1001", "start_time": 0.0, "master_display": None, "max_cll": None}
        with mock.patch.object(remux.dvcap, "probe_video", return_value=bad), \
             mock.patch.object(remux.dvcap, "ensure_segdir") as ens:
            res = remux.remux("/dv.mov", "/cfr.mp4", "/orig.mp4", "/out.mp4")
        self.assertFalse(res.ok)
        self.assertIn("probe", res.reason)
        ens.assert_not_called()                      # segdir untouched — resume survives a probe hiccup

    def test_extract_rpu_leaves_no_partial_on_failure(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "rpu.bin")
            class _Pipe:
                def close(self): pass
            class P1:
                stdout = _Pipe()
                def wait(self): return 0
            class P2:
                returncode = 1; stderr = "dovi boom"
            with mock.patch.object(dvcap.subprocess, "Popen", return_value=P1()), \
                 mock.patch.object(dvcap.subprocess, "run", return_value=P2()):
                ok, why = dvcap.extract_rpu("/dv.mov", out)
            self.assertFalse(ok)
            self.assertFalse(os.path.exists(out))                 # real path clean
            self.assertFalse(os.path.exists(out + ".part"))       # temp cleaned

    def test_segment_frame_mismatch_leaves_no_published_seg(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            segdir = os.path.join(td, "segs"); os.makedirs(segdir)
            def short_pipe(dec, enc, out, abort, on_frame):
                open(out, "wb").write(b"partial"); return 5, "ok", []   # wrote to .part, wrong count
            with mock.patch.object(dvcap, "count_hevc_frames", return_value=0), \
                 mock.patch.object(dvcap, "slice_rpu", return_value=(True, "ok")), \
                 mock.patch.object(dvcap, "_encode_pipe", side_effect=short_pipe):
                ok, frames, why = dvcap.encode_capped_segmented(
                    "/r.mov", "/rpu.bin", os.path.join(td, "out.hevc"), 50,
                    segdir=segdir, total_frames=100, fps="24000/1001", seg_seconds=4)
            self.assertFalse(ok)
            self.assertFalse(os.path.exists(os.path.join(segdir, "seg_0000.hevc")))     # not published
            self.assertFalse(os.path.exists(os.path.join(segdir, "seg_0000.hevc.part")))  # temp cleaned


class RpuGroundTruth(unittest.TestCase):
    """Frame count comes from the RPU (one per coded frame), not the container nb_frames header
    which over-reports a VideoToolbox .mov's decodable tail → out-of-range slices → park."""

    def test_parses_frame_count_from_summary(self):
        summary = "Parsing RPU file...\n\nSummary:\n  Frames: 117\n  Profile: 8\n  Scene/shot count: 1\n"
        class R: stdout = summary; stderr = ""; returncode = 0
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            rpu = os.path.join(td, "r.bin"); open(rpu, "wb").write(b"x" * 10)
            with mock.patch.object(dvcap.subprocess, "run", return_value=R()):
                self.assertEqual(dvcap.rpu_frame_count(rpu), 117)

    def test_missing_or_unparseable_is_zero(self):
        self.assertEqual(dvcap.rpu_frame_count("/nope.bin"), 0)
        class R: stdout = "no frames line here"; stderr = ""; returncode = 0
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            rpu = os.path.join(td, "r.bin"); open(rpu, "wb").write(b"x")
            with mock.patch.object(dvcap.subprocess, "run", return_value=R()):
                self.assertEqual(dvcap.rpu_frame_count(rpu), 0)   # → remux fails, segdir intact


class OverGateSegments(unittest.TestCase):
    """Peak-repair localization: map over-gate 1-second buckets to the segments that made them."""

    def test_hot_second_maps_to_its_segment(self):
        segs = [(0, 50), (50, 100)]                       # fps 25 → 0..2s and 2..4s
        self.assertEqual(dvcap.over_gate_segments({0: 60.0}, segs, "25", 50), [0])
        self.assertEqual(dvcap.over_gate_segments({3: 60.0}, segs, "25", 50), [1])

    def test_burst_straddling_a_cut_charges_both_segments(self):
        segs = [(0, 45), (45, 100)]                       # fps 25 → cut at 1.8s; second [1,2) spans it
        self.assertEqual(dvcap.over_gate_segments({1: 60.0}, segs, "25", 50), [0, 1])

    def test_under_gate_is_clean_and_boundary_matches_peak_ok(self):
        segs = [(0, 100)]
        self.assertEqual(dvcap.over_gate_segments({0: 57.4}, segs, "25", 50), [])   # under the gate
        self.assertEqual(dvcap.over_gate_segments({}, segs, "25", 50), [])
        self.assertEqual(dvcap.over_gate_segments(None, segs, "25", 50), [])
        # the localizer must agree with peak_ok about what's "over" — same floats, same verdict
        for m in (57.4, 57.5, 58.6):
            charged = bool(dvcap.over_gate_segments({0: m}, segs, "25", 50))
            self.assertEqual(charged, not dvcap.peak_ok(m, 50))

    def test_buckets_refactor_keeps_max_semantics(self):
        csv = "packet,0.0,125000\npacket,0.5,125000\npacket,1.2,250000\n"
        self.assertEqual(dvcap.peak_buckets_from_packets(csv), {0: 2.0, 1: 2.0})
        self.assertEqual(dvcap.peak_1s_mbps_from_packets(csv), 2.0)


class ReencodeTighter(unittest.TestCase):
    """Peak repair re-encodes ONLY the named segments at the tighter cap, atomically."""

    def test_replaces_the_segment_at_the_tight_cap(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as td:
            sf = os.path.join(td, "seg_0000.hevc")
            with open(sf, "wb") as f:
                f.write(b"OLD-OVER-PEAK")
            captured = {}
            def fake_pipe(dec, enc, out, abort, cb):
                captured["enc"] = enc
                with open(out, "wb") as f:
                    f.write(b"NEW-TIGHT")
                return 100, "ok", []
            with mock.patch.object(dvcap, "slice_rpu", return_value=(True, "ok")), \
                 mock.patch.object(dvcap, "_encode_pipe", side_effect=fake_pipe):
                ok, why = dvcap.reencode_segments_tighter("dv.mov", "rpu.bin", td, [0], 42,
                                                          total_frames=100, fps="25")
            self.assertTrue(ok); self.assertIn("42", why)
            with open(sf, "rb") as f:
                self.assertEqual(f.read(), b"NEW-TIGHT")            # atomically replaced
            i = captured["enc"].index("--vbv-maxrate")
            self.assertEqual(captured["enc"][i + 1], "42000")       # tight cap drives the VBV
            self.assertEqual(captured["enc"][captured["enc"].index("--vbv-bufsize") + 1], "42000")

    def test_frame_mismatch_fails_and_publishes_nothing(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as td:
            def bad_pipe(dec, enc, out, abort, cb):
                with open(out, "wb") as f:
                    f.write(b"SHORT")
                return 99, "ok", []                                  # one frame short → drift risk
            with mock.patch.object(dvcap, "slice_rpu", return_value=(True, "ok")), \
                 mock.patch.object(dvcap, "_encode_pipe", side_effect=bad_pipe):
                ok, why = dvcap.reencode_segments_tighter("dv.mov", "rpu.bin", td, [0], 42,
                                                          total_frames=100, fps="25")
            self.assertFalse(ok); self.assertIn("mismatch", why)
            self.assertFalse(os.path.exists(os.path.join(td, "seg_0000.hevc")))
            self.assertFalse(os.path.exists(os.path.join(td, "seg_0000.hevc.part")))

    def test_out_of_range_index_fails_cleanly(self):
        ok, why = dvcap.reencode_segments_tighter("dv.mov", "rpu.bin", "/nowhere", [7], 42,
                                                  total_frames=100, fps="25")
        self.assertFalse(ok); self.assertIn("out of range", why)


class NativeDecodePassThrough(unittest.TestCase):
    """`decode` (native DV: Topaz's own ProRes chunks + the captured LUT chain) must reach the
    decoder of every segment BOTH encoders run — the main encode and the peak repair. Dropping
    it at either call site would silently encode SDR pixels (or the wrong file) under PQ tags,
    and the build_seg_decode_command tests alone would never notice (review-caught). A plan
    that does not fit the chunks fails with the normal return, before anything is sliced,
    deleted or tight-capped."""

    FPS = "24000/1001"
    CHUNKS = [[0, "/c0.mov"], [2217, "/c1.mov"], [4398, "/c2.mov"], [6000, None]]
    BOUNDS = [2217, 3000, 4398]       # -> (0,2217) (2217,3000) (3000,4398) (4398,6000): seg 2
                                      #    starts MID-chunk (c1, local frame 783)
    DECODE = {"chunks": CHUNKS, "vf": "X"}
    # segment 2 with DECODE: chunk c1, seek to LOCAL frame 783, the LUT before -pix_fmt
    NATIVE_SEG2 = ["/ff", "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", "32.636771",
                   "-i", "/c1.mov", "-map", "0:v:0", "-frames:v", "1398", "-vf", "X",
                   "-fps_mode", "passthrough", "-pix_fmt", "yuv420p10le", "-strict", "-1", "-f", "yuv4mpegpipe", "-"]

    @staticmethod
    def _idx(path):
        import os
        return int(os.path.basename(path).split("_")[1].split(".")[0])

    def _encode(self, td, decode, boundaries=BOUNDS, total=6000):
        import os
        decs = {}
        def fake_pipe(dec, enc, out, abort, on_frame):
            decs[self._idx(out)] = dec
            open(out, "wb").write(b"y")
            return int(dec[dec.index("-frames:v") + 1]), "ok", []
        segdir = os.path.join(td, "segs")
        with mock.patch.object(dvcap, "count_hevc_frames", return_value=0), \
             mock.patch.object(dvcap, "slice_rpu", return_value=(True, "ok")) as sl, \
             mock.patch.object(dvcap, "_encode_pipe", side_effect=fake_pipe) as pipe, \
             mock.patch.object(dvcap, "concat_segments", return_value=(True, "ok")):
            res = dvcap.encode_capped_segmented(
                "/r.mov", "/rpu.bin", os.path.join(td, "out.hevc"), 50, segdir=segdir,
                total_frames=total, fps=self.FPS, boundaries=boundaries, decode=decode,
                ffmpeg="/ff")
        return res, decs, sl, pipe, segdir

    def _repair(self, td, indices, decode, boundaries=BOUNDS, total=6000):
        decs = {}
        def fake_pipe(dec, enc, out, abort, cb):
            decs[self._idx(out)] = dec
            open(out, "wb").write(b"NEW")
            return int(dec[dec.index("-frames:v") + 1]), "ok", []
        with mock.patch.object(dvcap, "slice_rpu", return_value=(True, "ok")) as sl, \
             mock.patch.object(dvcap, "_encode_pipe", side_effect=fake_pipe) as pipe:
            res = dvcap.reencode_segments_tighter(
                "/r.mov", "/rpu.bin", td, indices, 42, total_frames=total, fps=self.FPS,
                boundaries=boundaries, decode=decode, ffmpeg="/ff")
        return res, decs, sl, pipe

    def test_main_encode_hands_each_segment_its_chunk_and_the_lut(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (ok, frames, why), decs, sl, _p, _s = self._encode(td, self.DECODE)
        self.assertTrue(ok, why)
        self.assertEqual(frames, 6000)
        for i, src in enumerate(["/c0.mov", "/c1.mov", "/c1.mov", "/c2.mov"]):
            with self.subTest(segment=i):
                self.assertEqual(decs[i][decs[i].index("-i") + 1], src)
                self.assertEqual(decs[i][decs[i].index("-vf") + 1], "X")
        for i in (0, 1, 3):
            self.assertNotIn("-ss", decs[i])                      # chunk starts → no seek at all
        self.assertEqual(decs[2], self.NATIVE_SEG2)
        # the RPU slices stay on GLOBAL frames — only the decoder is chunk-local
        self.assertEqual([c[0][1:3] for c in sl.call_args_list],
                         [(0, 2217), (2217, 3000), (3000, 4398), (4398, 6000)])

    def test_main_encode_render_path_decode_is_the_pre_change_command(self):
        # decode=None (every production caller today) → literally the pre-native argv
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (ok, _f, why), decs, _sl, _p, _s = self._encode(td, None)
        self.assertTrue(ok, why)
        self.assertEqual(decs[0], ["/ff", "-hide_banner", "-nostdin", "-loglevel", "error",
                                   "-i", "/r.mov", "-map", "0:v:0", "-frames:v", "2217",
                                   "-fps_mode", "passthrough", "-pix_fmt", "yuv420p10le", "-strict", "-1",
                                   "-f", "yuv4mpegpipe", "-"])
        self.assertEqual(decs[2], ["/ff", "-hide_banner", "-nostdin", "-loglevel", "error",
                                   "-ss", "125.104146", "-i", "/r.mov", "-map", "0:v:0",
                                   "-frames:v", "1398", "-fps_mode", "passthrough", "-pix_fmt", "yuv420p10le",
                                   "-strict", "-1", "-f", "yuv4mpegpipe", "-"])
        for dec in decs.values():
            self.assertNotIn("-vf", dec)

    def test_repair_hands_its_segment_its_chunk_and_the_lut(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (ok, why), decs, sl, _p = self._repair(td, [2], self.DECODE)
        self.assertTrue(ok, why)
        self.assertEqual(list(decs), [2])
        self.assertEqual(decs[2], self.NATIVE_SEG2)
        self.assertEqual(sl.call_args[0][1:3], (3000, 4398))       # global RPU frames

    def test_flat_plan_across_chunks_fails_before_slicing(self):
        # no topaz_segbounds → the flat ~5-min plan: one (0, 6000) segment crossing chunk 0's end
        import os, tempfile
        with tempfile.TemporaryDirectory() as td:
            (ok, frames, why), _d, sl, pipe, segdir = self._encode(
                td, {"chunks": self.CHUNKS}, boundaries=None)
            self.assertEqual(os.listdir(segdir), [])              # no seg_*.rpu left behind
        self.assertFalse(ok)
        self.assertEqual(frames, 0)
        self.assertIn("cross the chunk ending at 2216", why)
        sl.assert_not_called()
        pipe.assert_not_called()

    def test_drift_tail_past_the_last_chunk_fails_before_slicing(self):
        # RPU count 2 over the chunk total → plan_segments pads (6000, 6002). Segments 1-3 fit,
        # but the WHOLE plan is checked first, so not even segment 1 is sliced or encoded.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (ok, frames, why), _d, sl, pipe, _s = self._encode(
                td, self.DECODE, boundaries=[2217, 4398, 6000], total=6002)
        self.assertFalse(ok)
        self.assertEqual(frames, 0)
        self.assertIn("segment 4/4", why)
        self.assertIn("outside the chunk list", why)
        sl.assert_not_called()
        pipe.assert_not_called()

    def test_repair_on_a_plan_that_misses_the_chunks_keeps_the_segment(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as td:
            sf = os.path.join(td, "seg_0000.hevc")
            open(sf, "wb").write(b"GOOD")
            (ok, why), _d, sl, pipe = self._repair(td, [0], {"chunks": self.CHUNKS},
                                                   boundaries=None)
            self.assertFalse(ok)
            self.assertIn("cross the chunk ending at 2216", why)
            self.assertEqual(open(sf, "rb").read(), b"GOOD")      # not deleted
            self.assertEqual(dvcap.read_tight_caps(td), {})       # no tight cap recorded
        sl.assert_not_called()
        pipe.assert_not_called()

    def test_repair_checks_the_whole_plan_not_just_its_segment(self):
        # segment 0 itself fits chunk c0, but the drift-padded tail does not: same plan as the
        # main encode, so the repair refuses it too, before touching segment 0
        import os, tempfile
        with tempfile.TemporaryDirectory() as td:
            sf = os.path.join(td, "seg_0000.hevc")
            open(sf, "wb").write(b"GOOD")
            (ok, why), _d, sl, pipe = self._repair(td, [0], self.DECODE,
                                                   boundaries=[2217, 4398, 6000], total=6002)
            self.assertFalse(ok)
            self.assertIn("outside the chunk list", why)
            self.assertEqual(open(sf, "rb").read(), b"GOOD")
            self.assertEqual(dvcap.read_tight_caps(td), {})
        sl.assert_not_called()
        pipe.assert_not_called()

    def test_decode_error_in_the_loop_is_a_failure_return_not_an_exception(self):
        # the per-segment backstop: a decode command that can't be built fails BEFORE the
        # RPU slice (main encode) and before the tight-cap note + delete (repair)
        import os, tempfile
        with mock.patch.object(dvcap, "build_seg_decode_command",
                               side_effect=ValueError("boom")):
            with tempfile.TemporaryDirectory() as td:
                (ok, frames, why), _d, sl, pipe, _s = self._encode(td, None)
                self.assertEqual((ok, frames, why), (False, 0, "segment 1/4: boom"))
                sl.assert_not_called(); pipe.assert_not_called()
            with tempfile.TemporaryDirectory() as td:
                sf = os.path.join(td, "seg_0002.hevc")
                open(sf, "wb").write(b"GOOD")
                (ok, why), _d, sl, pipe = self._repair(td, [2], None)
                self.assertEqual((ok, why), (False, "repair segment 2: boom"))
                self.assertEqual(open(sf, "rb").read(), b"GOOD")
                self.assertEqual(dvcap.read_tight_caps(td), {})
                sl.assert_not_called(); pipe.assert_not_called()


class InjectBuilders(unittest.TestCase):
    """FAST-PATH builders: the source ES copy and the RPU inject — never a re-encode."""

    def test_annexb_file_command_is_a_pure_stream_copy(self):
        cmd = dvcap.build_annexb_file_command("ffmpeg", "src.mkv", "out.hevc")
        self.assertIn("copy", cmd[cmd.index("-c:v") + 1])
        self.assertEqual(cmd[cmd.index("-bsf:v") + 1], "hevc_mp4toannexb")
        self.assertEqual(cmd[cmd.index("-f") + 1], "hevc")
        self.assertEqual(cmd[-1], "out.hevc")
        self.assertNotIn("-c:v libx265", " ".join(cmd))

    def test_inject_command_uses_dovi_tool_inject_rpu(self):
        cmd = dvcap.build_inject_command("/opt/homebrew/bin/dovi_tool", "src.hevc", "r.bin", "inj.hevc")
        self.assertEqual(cmd[:2], ["/opt/homebrew/bin/dovi_tool", "inject-rpu"])
        self.assertEqual(cmd[cmd.index("-i") + 1], "src.hevc")
        self.assertEqual(cmd[cmd.index("--rpu-in") + 1], "r.bin")
        self.assertEqual(cmd[cmd.index("-o") + 1], "inj.hevc")


class SdrEncode(unittest.TestCase):
    """A true-SDR master carries no Dolby Vision. The encode must drop the RPU and the PQ/2020
    metadata — but keep the VBV cap (a player-glitch guard, unrelated to DV) and --aud (which
    count_hevc_frames relies on to count coded frames)."""

    def _cmd(self, dv):
        return dvcap.build_x265_command("x265", "rpu.bin", "out.hevc", cap_mbps=50, dv=dv)

    def test_sdr_drops_every_dolby_vision_flag(self):
        c = self._cmd(False)
        self.assertNotIn("--dolby-vision-rpu", c)
        self.assertNotIn("--dolby-vision-profile", c)
        self.assertNotIn("--master-display", c)   # mastering metadata is meaningless in SDR

    def test_sdr_is_rec709_not_pq(self):
        c = self._cmd(False)
        self.assertIn("bt709", c)
        self.assertNotIn("smpte2084", c)
        self.assertNotIn("bt2020", c)

    def test_dv_is_unchanged(self):
        c = self._cmd(True)
        self.assertIn("--dolby-vision-rpu", c)
        self.assertIn("smpte2084", c)

    def test_both_keep_the_peak_cap_and_aud(self):
        for dv in (True, False):
            c = self._cmd(dv)
            self.assertIn("--vbv-maxrate", c)     # the peak cap is not a DV feature
            self.assertIn("--aud", c)             # count_hevc_frames needs access-unit delimiters


class RepairProgressSurface(unittest.TestCase):
    """The peak-repair pass reports which segment it is re-capping and its live frame
    progress (on_repair) — without it the lane sat at "remux · 100%" for the whole pass
    and read as stuck (user-caught 2026-08-06)."""

    def test_on_repair_gets_start_and_frame_ticks(self):
        import os, tempfile
        calls = []
        with tempfile.TemporaryDirectory() as td:
            def fake_pipe(dec, enc, out, abort, cb):
                cb(7)                                     # a mid-encode x265 progress line
                cb(999)                                   # over the segment count → clamped
                with open(out, "wb") as f:
                    f.write(b"NEW")
                # plan_segments(100, "25", seg) → one 100-frame segment at index 0
                return 100, "ok", []
            with mock.patch.object(dvcap, "slice_rpu", return_value=(True, "ok")), \
                 mock.patch.object(dvcap, "_encode_pipe", side_effect=fake_pipe):
                ok, _why = dvcap.reencode_segments_tighter(
                    "dv.mov", "rpu.bin", td, [0], 42, total_frames=100, fps="25",
                    on_repair=lambda *a: calls.append(a))
            self.assertTrue(ok)
        self.assertEqual(calls[0], (1, 1, 0, 0, 100))     # segment start: cut-out empty
        self.assertEqual(calls[1], (1, 1, 0, 7, 100))     # live frames flow through
        self.assertEqual(calls[2], (1, 1, 0, 100, 100))   # clamped to the segment's count

    def test_no_callback_is_fine(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            def fake_pipe(dec, enc, out, abort, cb):
                self.assertIsNone(cb)                     # no on_repair → no per-frame cb
                with open(out, "wb") as f:
                    f.write(b"NEW")
                return 100, "ok", []
            with mock.patch.object(dvcap, "slice_rpu", return_value=(True, "ok")), \
                 mock.patch.object(dvcap, "_encode_pipe", side_effect=fake_pipe):
                ok, _why = dvcap.reencode_segments_tighter(
                    "dv.mov", "rpu.bin", td, [0], 42, total_frames=100, fps="25")
            self.assertTrue(ok)


class TightCapResume(unittest.TestCase):
    """An interrupted peak-repair used to resume the killed segment at the GLOBAL cap —
    deterministic x265 re-fails the gate identically, so every interruption cost a full
    re-encode + re-flag + re-repair of that segment (live-measured 2026-08-06). The repair
    now records each flagged segment's tight cap in segdir/tight_caps.json BEFORE touching
    it; the segmented encode resumes those segments at THEIR cap."""

    def test_book_roundtrip_and_junk(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(dvcap.read_tight_caps(td), {})          # absent → global-cap behavior
            dvcap._remember_tight_cap(td, 13, 42)
            dvcap._remember_tight_cap(td, 13, 35)                    # deeper rung overwrites
            dvcap._remember_tight_cap(td, 2, 42)
            self.assertEqual(dvcap.read_tight_caps(td), {13: 35, 2: 42})
            with open(dvcap._tight_caps_path(td), "w") as f:
                f.write("not json")
            self.assertEqual(dvcap.read_tight_caps(td), {})          # corrupt → harmless

    def test_repair_records_before_the_kill_window(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            def killed_pipe(dec, enc, out, abort, cb):
                return None, "aborted", []                           # deploy landed mid-encode
            with mock.patch.object(dvcap, "slice_rpu", return_value=(True, "ok")), \
                 mock.patch.object(dvcap, "_encode_pipe", side_effect=killed_pipe):
                ok, why = dvcap.reencode_segments_tighter(
                    "dv.mov", "rpu.bin", td, [0], 42, total_frames=100, fps="25")
            self.assertFalse(ok)
            self.assertEqual(dvcap.read_tight_caps(td), {0: 42})     # the note SURVIVES the kill

    def test_resume_encodes_tightened_segments_at_their_cap(self):
        import tempfile, os
        TOTAL, SS = 200, 4
        plan = dvcap.plan_segments(TOTAL, "24000/1001", SS)
        n_of = {i: b - a for i, (a, b) in enumerate(plan)}
        idx = lambda p: int(os.path.basename(p).split("_")[1].split(".")[0])
        with tempfile.TemporaryDirectory() as td:
            segdir = os.path.join(td, "segs"); os.makedirs(segdir)
            dvcap._remember_tight_cap(segdir, 1, 42)                 # seg 1 was mid-repair when killed
            caps = {}
            def fake_pipe(dec, enc, out, abort, on_frame):
                caps[idx(out)] = enc[enc.index("--vbv-maxrate") + 1]
                open(out, "wb").write(b"y")
                return n_of[idx(out)], "ok", []
            with mock.patch.object(dvcap, "count_hevc_frames", return_value=0), \
                 mock.patch.object(dvcap, "slice_rpu", return_value=(True, "ok")), \
                 mock.patch.object(dvcap, "_encode_pipe", side_effect=fake_pipe), \
                 mock.patch.object(dvcap, "concat_segments",
                                   side_effect=AssertionError("paused run must not concat")):
                ok, _f, why = dvcap.encode_capped_segmented(
                    "/r.mov", "/rpu.bin", os.path.join(td, "out.hevc"), 50,
                    segdir=segdir, total_frames=TOTAL, fps="24000/1001", seg_seconds=SS,
                    should_pause=lambda: len(caps) >= 2)             # stop before concat
            self.assertTrue(why.startswith("paused:"), why)
            self.assertEqual(caps[0], "50000")                       # untouched → global cap
            self.assertEqual(caps[1], "42000")                       # tightened → resumes tight


class DoviModeBuilders(unittest.TestCase):
    """dovi_tool CLI shapes for the companion combine (verified against the pinned 2.3.0:
    -m is a GLOBAL flag and must precede the subcommand)."""

    def test_extract_default_has_no_mode(self):
        cmd = dvcap.build_rpu_extract_command("/dovi", "/r.bin")
        self.assertEqual(cmd, ["/dovi", "extract-rpu", "-", "-o", "/r.bin"])

    def test_extract_mode_2_precedes_the_subcommand(self):
        cmd = dvcap.build_rpu_extract_command("/dovi", "/r.bin", mode=2)
        self.assertEqual(cmd, ["/dovi", "-m", "2", "extract-rpu", "-", "-o", "/r.bin"])

    def test_convert_discards_the_el(self):
        cmd = dvcap.build_dovi_convert_command("/dovi", "/in.hevc", "/out.hevc")
        self.assertEqual(cmd, ["/dovi", "-m", "2", "convert", "--discard",
                               "-i", "/in.hevc", "-o", "/out.hevc"])

    def test_manifest_carries_rpu_mode_only_when_set(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            dv = os.path.join(td, "r.mov"); open(dv, "wb").write(b"x")
            m0 = dvcap.resume_manifest(dv, 50, 100, "24/1", 300)
            m2 = dvcap.resume_manifest(dv, 50, 100, "24/1", 300, rpu_mode=2)
            self.assertNotIn("rpu_mode", m0)             # schema-stable for existing runs
            self.assertEqual(m2["rpu_mode"], 2)
            self.assertNotEqual(m0, m2)                  # switching modes wipes stale RPU


class MkvFrameCount(unittest.TestCase):
    """nb_frames is MP4/MOV-only; Matroska never carries it. probe_video used to report
    frames=0 for every MKV, and the `frames <= 0` guards in remux()/remux_inject() then
    refused to run — killing the peak-gated rpu-only fallback (its encode_source is the
    .mkv CFR for TrueHD/PGS remuxes, exactly the ones that exceed the peak gate) and every
    companion combine grafting a real DV RPU (a P7 donor is Matroska by construction).
    Confirmed by a live reproduction on 2026-08-17."""

    def _probe(self, nb_frames, packets):
        payload = {"streams": [{"nb_frames": nb_frames, "r_frame_rate": "24000/1001"}],
                   "frames": []}
        import json as _json, types
        with mock.patch.object(dvcap.subprocess, "run",
                               return_value=types.SimpleNamespace(
                                   returncode=0, stdout=_json.dumps(payload), stderr="")), \
             mock.patch.object(dvcap, "count_hevc_frames", return_value=packets) as c:
            out = dvcap.probe_video("/x.mkv")
        return out, c

    def test_mkv_falls_back_to_the_packet_count(self):
        out, counter = self._probe(None, 61536)          # MKV: no nb_frames
        self.assertEqual(out["frames"], 61536)
        counter.assert_called_once()

    def test_mp4_header_count_wins_without_scanning(self):
        out, counter = self._probe("1234", 999)          # MP4: header present
        self.assertEqual(out["frames"], 1234)
        counter.assert_not_called()

    def test_unprobeable_still_reports_zero(self):
        out, _c = self._probe(None, 0)                   # both unavailable -> honest 0
        self.assertEqual(out["frames"], 0)


@__import__("unittest").skipUnless(__import__("os").path.exists(dvcap.FFMPEG), "needs ffmpeg")
class SeekNeverDuplicatesAFrame(__import__("unittest").TestCase):
    """Live-caught on a Resolve render: the midpoint seek + y4m's default cfr padding emitted
    the first frame twice (20098, 20098, 20099, ...) — on every segment start whose 6-decimal
    seek lands just past half a frame (about half of them), and only in a file with a
    timecode track, which Resolve always writes (01:00:00:00). This clip reproduces both:
    a tmcd track, and every frame its own luma level (16 + 4*N) so the order is readable."""

    @staticmethod
    def _levels(y4m, w=64, h=64):
        import struct
        body, out = y4m[y4m.index(b"\n") + 1:], []
        while body:
            body = body[body.index(b"\n") + 1:]
            y = struct.unpack("<%dH" % (w * h), body[:w * h * 2])
            out.append(round((sum(y) / len(y) / 4 - 16) / 4))
            body = body[w * h * 3:]
        return out

    def test_segment_decode_starts_at_a_and_keeps_every_frame(self):
        import os, subprocess, tempfile
        with tempfile.TemporaryDirectory() as td:
            clip = os.path.join(td, "n.mov")
            subprocess.run([dvcap.FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                            "nullsrc=s=64x64:r=24000/1001:d=2.2,geq=lum='16+4*N':cb=128:cr=128",
                            "-c:v", "libx265", "-x265-params", "log-level=none:bframes=0",
                            "-video_track_timescale", "24000", "-timecode", "01:00:00:00",
                            "-pix_fmt", "yuv420p", clip], check=True)
            for a in (4, 7, 10):                     # seeks that land just past half a frame
                with self.subTest(a=a):
                    out = subprocess.run(dvcap.build_seg_decode_command(dvcap.FFMPEG, clip, a, 4, "24000/1001"),
                                         capture_output=True, check=True).stdout
                    self.assertEqual(self._levels(out), [a, a + 1, a + 2, a + 3])
