import json
import os
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
    def test_the_master_mux_carries_the_rate_a_raw_es_lacks(self):
        # MP4Box's `:fps=` is mkvmerge's --default-duration: a raw ES has no container timing
        c = remux.build_master_mux_command("/mkvmerge", "/v.hevc", "24000/1001", "/o.mkv.part",
                                           tracks="/t.mka")
        self.assertEqual(c[c.index("--default-duration") + 1], "0:24000/1001p")
        self.assertEqual(c[c.index("-o") + 1], "/o.mkv.part")
        self.assertNotIn("dvp=", " ".join(c))     # the DV record comes from the stream itself

    def test_parse_streams_reports_the_dv_record(self):
        pj = ('{"streams": [{"codec_type": "video", "side_data_list": [{"side_data_type": '
              '"DOVI configuration record", "dv_profile": 8, "dv_bl_signal_compatibility_id": 1, '
              '"rpu_present_flag": 1, "bl_present_flag": 1, "el_present_flag": 0}]}]}')
        s = remux.parse_streams(pj)
        self.assertEqual(s["dovi_profile"], "8.1")
        self.assertEqual(s["dovi"]["el_present_flag"], 0)


if __name__ == "__main__":
    unittest.main()


class SmartLoudnessBoost(unittest.TestCase):
    """Remux-stage loudness normalization: per-item measured gain to the target, boost-only,
    limiter-capped. Lossless audio is never transcoded away (its original track is kept)."""

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

    def test_tracks_boosted_reencodes_audio_only(self):
        c = remux.build_tracks_command("/ff", "/cfr.mp4", "/orig.mp4", "/t.mka", gain_db=7.2,
                                       subs=[(0, "copy")])
        self.assertIn("-filter:a", c)
        self.assertEqual(c[c.index("-c:a") + 1], "aac_at")
        self.assertEqual(c[c.index("-c:s:0") + 1], "copy")          # subs untouched

    def test_tracks_unboosted_is_pure_copy(self):
        c = remux.build_tracks_command("/ff", "/cfr.mp4", "/orig.mp4", "/t.mka")
        self.assertNotIn("-filter:a", c)
        self.assertNotIn("aac_at", c)                              # bit-exact copy path preserved
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
            res = remux.remux("/dv.mov", "/cfr.mp4", "/orig.mp4", "/out.mkv")
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

    def test_the_stream_copy_never_rewrites_the_vui_itself(self):
        cmd = dvcap.build_annexb_file_command("ffmpeg", "src.mkv", "out.hevc")
        self.assertNotIn("hevc_metadata", " ".join(cmd))      # ensure_pq_vui decides, after

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


class SdrSegmentedEncode(unittest.TestCase):
    """dv=False (an item pinned to SDR output, 2026-10-18): every segment is the Rec.709 x265 with
    no RPU — nothing is sliced — and the resume identity says "sdr" so a DV run's segments are
    never reused. dv=True (the default) stays exactly the DV call."""

    def _run(self, fn, **kw):
        import os, tempfile
        encs = []

        def fake_pipe(dec, enc, out, abort, cb):
            encs.append(enc)
            with open(out, "wb") as f:
                f.write(b"SEG")
            return 50, "ok", []
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(dvcap, "slice_rpu", side_effect=AssertionError("sliced")) as sl, \
             mock.patch.object(dvcap, "count_hevc_frames", return_value=0), \
             mock.patch.object(dvcap, "_encode_pipe", side_effect=fake_pipe):
            if fn == "encode":
                res = dvcap.encode_capped_segmented("r.mov", None, os.path.join(td, "o.hevc"), 50,
                                                    segdir=td, total_frames=100, fps="25",
                                                    boundaries=[50, 100], dv=False, **kw)
            else:
                res = dvcap.reencode_segments_tighter("r.mov", None, td, [1], 42, total_frames=100,
                                                      fps="25", boundaries=[50, 100], dv=False, **kw)
        return res, encs

    def test_the_main_encode_slices_nothing_and_writes_rec709(self):
        (ok, frames, why), encs = self._run("encode")
        self.assertTrue(ok, why)
        self.assertEqual((frames, len(encs)), (100, 2))
        for enc in encs:
            self.assertEqual(enc, dvcap.build_x265_command(dvcap.X265, None, enc[enc.index("--output") + 1],
                                                           50, dv=False))
            self.assertNotIn("--dolby-vision-rpu", enc)

    def test_the_repair_rung_is_sdr_too(self):
        (ok, why), encs = self._run("repair")
        self.assertTrue(ok, why)
        self.assertEqual(encs[0][encs[0].index("--vbv-maxrate") + 1], "42000")
        self.assertIn("bt709", encs[0])
        self.assertNotIn("--dolby-vision-rpu", encs[0])

    def test_the_resume_identity_says_sdr_only_when_it_is(self):
        dv = dvcap.resume_manifest("/x.mov", 50, 100, "25", 300)
        self.assertNotIn("sdr", dv)
        self.assertEqual(dvcap.resume_manifest("/x.mov", 50, 100, "25", 300, sdr=True),
                         dict(dv, sdr=True))


class SnapFps(unittest.TestCase):
    """A Matroska file cannot carry 60000/1001 exactly (whole-ns DefaultDuration): ffprobe reads
    it back as 19001/317. Every master is Matroska, so rates snap to the standard one they sit on."""

    def test_matroska_roundings_snap_to_their_standard_rate(self):
        for raw, want in (("19001/317", "60000/1001"), ("7001/146", "48000/1001"),
                          ("29011/242", "120000/1001"), ("24000/1001", "24000/1001"),
                          ("25", "25/1"), ("30000/1001", "30000/1001")):
            self.assertEqual(dvcap.snap_fps(raw), want, raw)

    def test_a_rate_off_every_standard_is_kept_and_garbage_is_none(self):
        self.assertEqual(dvcap.snap_fps("47/2"), "47/2")
        for bad in (None, "", "0/0", "-24", "abc"):
            self.assertIsNone(dvcap.snap_fps(bad), bad)

    def test_topaz_uses_the_same_list(self):
        import topaz
        self.assertIs(topaz.STANDARD_FRAME_RATES, dvcap.STANDARD_FPS)

    def test_probe_video_reports_the_snapped_rate(self):
        js = json.dumps({"streams": [{"nb_frames": "10", "r_frame_rate": "19001/317"}],
                         "frames": []})
        with mock.patch.object(dvcap.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout=js)):
            self.assertEqual(dvcap.probe_video("/x.mkv")["fps"], "60000/1001")


class FirstFrameHdr10(unittest.TestCase):
    def test_reads_the_first_frame_only_and_never_counts(self):
        js = json.dumps({"frames": [{"side_data_list": [
            {"side_data_type": "Content light level metadata", "max_content": 1000,
             "max_average": 400}]}]})
        with mock.patch.object(dvcap.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout=js)) as run, \
             mock.patch.object(dvcap, "count_hevc_frames",
                               side_effect=AssertionError("a full scan of a raw ES")):
            self.assertEqual(dvcap.first_frame_hdr10("/es.hevc"), (None, "1000,400"))
        self.assertIn("%+#1", run.call_args.args[0])

    def test_unreadable_is_none(self):
        with mock.patch.object(dvcap.subprocess, "run", side_effect=OSError("x")):
            self.assertEqual(dvcap.first_frame_hdr10("/x"), (None, None))


class EnsurePqVui(unittest.TestCase):
    """A DV 8.1 base must SAY BT.2020/PQ (mkvmerge derives the record's compatibility id from the
    VUI); most sources already do and are left alone; a rewrite that drops a packet (FFmpeg's
    parser discards an access unit whose SEI it cannot read, exit code 0) is refused."""

    def test_a_stream_that_already_says_pq_is_left_alone(self):
        with mock.patch.object(dvcap, "es_says_pq", return_value=True), \
             mock.patch.object(dvcap.subprocess, "run", side_effect=AssertionError("no rewrite")):
            self.assertEqual(dvcap.ensure_pq_vui("/es.hevc"), (True, ""))

    def _rewrite(self, before, after, stderr="", make=True):
        import tempfile, os
        d = tempfile.mkdtemp()
        es = os.path.join(d, "es.hevc")
        open(es, "wb").write(b"es")
        def run(cmd, **kw):
            if make:
                open(cmd[-1], "wb").write(b"vui")
            return mock.Mock(returncode=0, stderr=stderr)
        counts = {es: before, es + ".vui": after}
        with mock.patch.object(dvcap, "es_says_pq", return_value=False), \
             mock.patch.object(dvcap.subprocess, "run", side_effect=run), \
             mock.patch.object(dvcap, "count_hevc_frames", side_effect=lambda p, fp=None: counts[p]):
            res = dvcap.ensure_pq_vui(es)
        return res, open(es, "rb").read(), os.path.exists(es + ".vui")

    def test_a_clean_rewrite_replaces_the_stream(self):
        (ok, note), data, left = self._rewrite(240, 240)
        self.assertTrue(ok)
        self.assertIn("BT.2020/PQ", note)
        self.assertEqual((data, left), (b"vui", False))

    def test_a_dropped_access_unit_is_refused_and_nothing_replaced(self):
        (ok, why), data, left = self._rewrite(240, 239, "Error applying bitstream filters")
        self.assertFalse(ok)
        self.assertIn("240 -> 239", why)
        self.assertEqual((data, left), (b"es", False))

    def test_no_output_is_a_failure_not_a_crash(self):
        (ok, _why), data, _left = self._rewrite(240, 0, make=False)
        self.assertFalse(ok)
        self.assertEqual(data, b"es")

    def test_for_real_on_a_stream_whose_vui_is_silent(self):
        import tempfile, os, shutil as sh, subprocess as sp
        if not os.path.exists(dvcap.FFMPEG):
            self.skipTest("no ffmpeg")
        d = tempfile.mkdtemp()
        try:
            es = os.path.join(d, "es.hevc")
            r = sp.run([dvcap.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", "testsrc2=size=320x180:rate=24", "-frames:v", "24", "-c:v", "libx265",
                        "-pix_fmt", "yuv420p10le", "-x265-params", "log-level=error", "-f", "hevc", es],
                       capture_output=True)
            if r.returncode:
                self.skipTest("no 10-bit libx265")
            self.assertFalse(dvcap.es_says_pq(es))
            ok, note = dvcap.ensure_pq_vui(es)
            self.assertTrue(ok, note)
            self.assertTrue(dvcap.es_says_pq(es))
            self.assertEqual(dvcap.count_hevc_frames(es), 24)
        finally:
            sh.rmtree(d, ignore_errors=True)


class ResumeIdentitySurvivesTheSnap(unittest.TestCase):
    """A resume dir made before snap_fps recorded a Matroska readback rate (19001/317): the same
    work as 60000/1001, so the segments are KEPT, and the manifest is rewritten with the new rate."""

    def test_a_snapped_rate_resumes(self):
        import tempfile, os
        seg = os.path.join(tempfile.mkdtemp(), "x.remuxsegs")
        os.makedirs(seg)
        with open(os.path.join(seg, "manifest.json"), "w") as f:
            json.dump({"src": "a", "fps": "19001/317", "frames": 10}, f)
        open(os.path.join(seg, "seg_0000.hevc"), "w").close()
        new = {"src": "a", "fps": "60000/1001", "frames": 10}
        self.assertEqual(dvcap.ensure_segdir(seg, new), "resume")
        self.assertTrue(os.path.exists(os.path.join(seg, "seg_0000.hevc")))
        self.assertEqual(json.load(open(os.path.join(seg, "manifest.json"))), new)

    def test_a_really_different_rate_still_wipes(self):
        import tempfile, os
        seg = os.path.join(tempfile.mkdtemp(), "x.remuxsegs")
        os.makedirs(seg)
        with open(os.path.join(seg, "manifest.json"), "w") as f:
            json.dump({"fps": "24000/1001"}, f)
        open(os.path.join(seg, "seg_0000.hevc"), "w").close()
        self.assertEqual(dvcap.ensure_segdir(seg, {"fps": "24/1"}), "fresh")
        self.assertFalse(os.path.exists(os.path.join(seg, "seg_0000.hevc")))

    def test_nested_fields_snap_too(self):
        self.assertEqual(dvcap.snap_fps_fields({"cfr": {"fps": "19001/317"}, "x": [{"fps": "25"}]}),
                         {"cfr": {"fps": "60000/1001"}, "x": [{"fps": "25/1"}]})


class ChunkFreeingBound(unittest.TestCase):
    """The chunk lane frees a Topaz chunk once its segment provably cannot be flagged by the
    peak gate (user, 2026-10-19): the largest run of window_frames() coded frames bounds every
    1-second bucket those frames take part in."""

    def test_the_window_covers_a_second_plus_reordering(self):
        self.assertEqual(dvcap.window_frames("24000/1001"), 24 + 2 * dvcap.REORDER_SLACK + 2)
        self.assertEqual(dvcap.window_frames("60"), 60 + 2 * dvcap.REORDER_SLACK + 2)

    def test_the_bound_is_the_heaviest_run_touching_the_range(self):
        w = dvcap.window_frames("25")
        sizes = [1000] * 300
        sizes[10] = 10_000_000                       # a burst far left of the range
        self.assertAlmostEqual(dvcap.window_peak_mbps(sizes, "25", 200, 300), w * 1000 * 8 / 1e6)
        self.assertGreater(dvcap.window_peak_mbps(sizes, "25", 0, 50), 80)    # the burst counts
        # a window that only STARTS before the range still counts its frames inside it
        self.assertGreater(dvcap.window_peak_mbps(sizes, "25", 10 + w - 1, 300), 80)
        self.assertLess(dvcap.window_peak_mbps(sizes, "25", 10 + w, 300), 1)

    def test_an_es_decode_carries_its_rate(self):
        cmd = dvcap.build_es_decode_command("ffmpeg", "/s.hevc", "24000/1001")
        self.assertEqual(cmd[cmd.index("-r") + 1], "24000/1001")
        self.assertLess(cmd.index("-r"), cmd.index("-i"))
        self.assertIn("passthrough", cmd)


class OnSegmentHook(unittest.TestCase):
    def test_every_complete_segment_is_heard_in_order(self):
        import tempfile
        TOTAL, SS = 200, 4
        plan = dvcap.plan_segments(TOTAL, "24000/1001", SS)
        n_of = {i: b - a for i, (a, b) in enumerate(plan)}
        idx = lambda p: int(os.path.basename(p).split("_")[1].split(".")[0])
        heard = []
        with tempfile.TemporaryDirectory() as td:
            segdir = os.path.join(td, "segs"); os.makedirs(segdir)
            with open(os.path.join(segdir, "seg_0000.hevc"), "wb") as f:
                f.write(b"x")                                    # seg 0 done on an earlier pass
            def fake_pipe(dec, enc, out, abort, on_frame):
                with open(out, "wb") as f:
                    f.write(b"y")
                return n_of[idx(out)], "ok", []
            with mock.patch.object(dvcap, "count_hevc_frames",
                                   side_effect=lambda p, ffprobe=None: n_of[0] if "seg_0000" in p else 0), \
                 mock.patch.object(dvcap, "slice_rpu", return_value=(True, "ok")), \
                 mock.patch.object(dvcap, "_encode_pipe", side_effect=fake_pipe), \
                 mock.patch.object(dvcap, "concat_segments", return_value=(True, "ok")):
                ok, _f, why = dvcap.encode_capped_segmented(
                    "/r.mov", "/rpu.bin", os.path.join(td, "out.hevc"), 50, segdir=segdir,
                    total_frames=TOTAL, fps="24000/1001", seg_seconds=SS,
                    on_segment=lambda i, p: heard.append((i, os.path.basename(p))))
        self.assertTrue(ok, why)
        self.assertEqual([i for i, _p in heard], list(range(len(plan))))
        self.assertEqual(heard[0][1], "seg_0000.hevc")


class RepairFromItsOwnEncode(unittest.TestCase):
    """The rare miss: the gate flags a segment whose chunk was already freed. It is re-capped
    from its own encode, and put back as it was if that fails."""

    CHUNKS = None

    def _run(self, td, pipe):
        chunk = os.path.join(td, "seg_0000.mov")                  # never created: freed
        decode = {"chunks": [[0, chunk], [100, None]], "vf": "lut3d=x", "freed": {chunk}}
        with open(os.path.join(td, "seg_0000.hevc"), "wb") as f:
            f.write(b"ORIGINAL")
        seen = {}
        def fake_pipe(dec, enc, out, abort, cb):
            seen["dec"] = dec
            return pipe(out)
        with mock.patch.object(dvcap, "slice_rpu", return_value=(True, "ok")), \
             mock.patch.object(dvcap, "_encode_pipe", side_effect=fake_pipe):
            res = dvcap.reencode_segments_tighter("dv.mov", "rpu.bin", td, [0], 42,
                                                  total_frames=100, fps="25", decode=decode)
        return res, seen

    def _new(self, out):
        with open(out, "wb") as f:
            f.write(b"NEW")
        return 100, "ok", []

    def test_it_decodes_its_own_encode_and_tidies_up(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (ok, why), seen = self._run(td, self._new)
            self.assertTrue(ok, why)
            self.assertIn(os.path.join(td, "seg_0000.hevc.own.hevc"), seen["dec"])
            self.assertNotIn("lut3d=x", " ".join(seen["dec"]))   # already PQ: no LUT again
            with open(os.path.join(td, "seg_0000.hevc"), "rb") as f:
                self.assertEqual(f.read(), b"NEW")
            self.assertFalse(os.path.exists(os.path.join(td, "seg_0000.hevc.own.hevc")))

    def test_a_failed_repair_puts_the_segment_back(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (ok, _why), _seen = self._run(td, lambda out: (None, "aborted", []))
            self.assertFalse(ok)
            with open(os.path.join(td, "seg_0000.hevc"), "rb") as f:
                self.assertEqual(f.read(), b"ORIGINAL")

    def test_a_present_chunk_is_still_the_source(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            chunk = os.path.join(td, "seg_0000.mov")
            with open(chunk, "wb") as f:
                f.write(b"prores")
            with open(os.path.join(td, "seg_0000.hevc"), "wb") as f:
                f.write(b"ORIGINAL")
            seen = {}
            def fake_pipe(dec, enc, out, abort, cb):
                seen["dec"] = dec
                return self._new(out)
            with mock.patch.object(dvcap, "slice_rpu", return_value=(True, "ok")), \
                 mock.patch.object(dvcap, "_encode_pipe", side_effect=fake_pipe):
                ok, _why = dvcap.reencode_segments_tighter(
                    "dv.mov", "rpu.bin", td, [0], 42, total_frames=100, fps="25",
                    decode={"chunks": [[0, chunk], [100, None]], "vf": "lut3d=x"})
            self.assertTrue(ok)
            self.assertIn(chunk, seen["dec"])


# ---- THE HELPER: encode_capped_segmented(helper=...) (2026-10-28) ------------------------------

def _seg_idx(path):
    return int(os.path.basename(path).split("_")[1].split(".")[0])


def _size_count(path, ffprobe=None):
    return os.path.getsize(path) if path and os.path.exists(path) else 0


def _fake_slice(rpu_in, a, b, total, out, dovi_tool=None):
    with open(out, "wb") as f:
        f.write(b"R" * (b - a))
    return True, "sliced"


def _ally_threads():
    import threading
    return [t for t in threading.enumerate() if t.name == "ally-helper" and t.is_alive()]


def _wait_for(pred, timeout=10.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end and not pred():
        time.sleep(0.01)
    return pred()


_SUSP = "suspended: another stage has the machine — the segment goes back in the queue"


class FakeAlly:
    """A helper object with no processes: `script` lists the kinds its encodes return, in order
    (then "ok"); "short" claims ok but sends one byte (= frame) too few. Writes b"A" per frame."""

    def __init__(self, script=(), available=True, delay=0.0, hook=None):
        self.script, self.avail, self.delay, self.hook = list(script), available, delay, hook
        self.calls = []

    def available(self):
        ok = self.avail() if callable(self.avail) else self.avail
        return (True, "ready") if ok else (False, "test: not available")

    def encode_segment(self, dec, args, rpu, n, part, abort, on_frame):
        import time
        rpu_bytes = None
        if rpu and os.path.exists(rpu):
            with open(rpu, "rb") as f:
                rpu_bytes = f.read()
        rec = {"i": _seg_idx(part), "dec": dec, "args": args, "rpu": rpu, "n": n, "part": part,
               "rpu_bytes": rpu_bytes}
        self.calls.append(rec)
        if self.hook:
            r = self.hook(rec, abort, on_frame)
            if r is not None:
                return r
        kind = self.script.pop(0) if self.script else "ok"
        if self.delay:
            time.sleep(self.delay)
        if kind in ("ok", "short"):
            with open(part, "wb") as f:
                f.write(b"A" * (n if kind == "ok" else n - 1))
            if on_frame:
                on_frame(n)
            return n, "ok", "ok"
        return None, kind, kind


class AllyHelperScheduler(unittest.TestCase):
    """The Mac takes segments from the FRONT, the Ally from the BACK; a dropped segment goes back
    to the Mac; on_segment stays in plan order; pause/abort stop and join the remote worker."""
    N, FPS = 100, "24/1"

    def setUp(self):
        import tempfile
        self.td = tempfile.mkdtemp()
        self.segdir = os.path.join(self.td, "segs")
        os.makedirs(self.segdir)
        self.out = os.path.join(self.td, "out.hevc")
        self.mac, self.mac_delay, self.mac_hook = [], 0.0, None
        self.patches = [mock.patch.object(dvcap, "count_hevc_frames", side_effect=_size_count),
                        mock.patch.object(dvcap, "slice_rpu", side_effect=_fake_slice),
                        mock.patch.object(dvcap, "_encode_pipe", side_effect=self._mac_pipe)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        import shutil
        dvcap.resume_encoders()                            # a failed suspension test leaves none
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.td, ignore_errors=True)

    def _mac_pipe(self, dec, enc, out, abort, on_frame):
        import time
        i = _seg_idx(out)
        self.mac.append(i)
        if self.mac_hook:
            r = self.mac_hook(i, out, abort, on_frame)
            if r is not None:
                return r
        time.sleep(self.mac_delay)
        with open(out, "wb") as f:
            f.write(b"M" * self.N)
        if on_frame:
            on_frame(self.N)
        return self.N, "ok", []

    def seg(self, i):
        return os.path.join(self.segdir, f"seg_{i:04d}.hevc")

    def run_enc(self, h, segs=6, **kw):
        kw.setdefault("boundaries", [self.N * (k + 1) for k in range(segs)])
        return dvcap.encode_capped_segmented("/r.mov", "/rpu.bin", self.out, 50, segdir=self.segdir,
                                             total_frames=self.N * segs, fps=self.FPS, helper=h, **kw)

    def hosts(self):
        return dvcap.read_hosts(self.segdir)

    def test_the_mac_takes_the_front_and_the_ally_the_back(self):
        self.mac_delay = 0.05
        ally = FakeAlly(delay=0.12)
        ok, frames, why = self.run_enc(ally)
        self.assertTrue(ok, why)
        self.assertEqual(frames, 600)
        took = [c["i"] for c in ally.calls]
        self.assertEqual(self.mac[0], 0)
        self.assertEqual(took[0], 5)
        self.assertEqual(self.mac, sorted(self.mac))
        self.assertEqual(took, sorted(took, reverse=True))
        self.assertEqual(sorted(self.mac + took), list(range(6)))
        self.assertEqual(self.hosts(), {str(i): ("ally" if i in took else "mac") for i in range(6)})
        self.assertEqual(len(took), 2)
        self.assertEqual(why, "encoded 600 frames in 6 segments (2 segments on the helper)")
        with open(self.out, "rb") as f:                    # concatenated in PLAN order
            self.assertEqual(f.read(), b"".join((b"A" if i in took else b"M") * self.N
                                                for i in range(6)))
        self.assertEqual(_ally_threads(), [])

    def test_a_dropped_segment_goes_back_to_the_mac_next(self):
        self.mac_delay = 0.1
        ally = FakeAlly(script=["disconnected"], delay=0.05)
        ok, _frames, why = self.run_enc(ally)
        self.assertTrue(ok, why)
        self.assertEqual(ally.calls[0]["i"], 5)
        self.assertEqual(self.mac[:2], [0, 5])             # the Mac's very next segment
        self.assertEqual(self.hosts()["5"], "mac")
        self.assertFalse(os.path.exists(self.seg(5) + ".remote.part"))

    def test_two_failures_retire_the_ally_for_the_item(self):
        # D4: only "error" and "disconnected" count toward HELPER_MAX_FAILURES
        self.assertEqual(dvcap.HELPER_COUNTED, ("error", "disconnected"))
        self.mac_delay = 0.05
        ally = FakeAlly(script=["error", "disconnected"])
        ok, _frames, why = self.run_enc(ally)
        self.assertTrue(ok, why)
        self.assertEqual(len(ally.calls), 2)
        self.assertEqual(set(self.hosts().values()), {"mac"})
        self.assertEqual(sorted(self.mac), list(range(6)))
        self.assertEqual(why, "encoded 600 frames in 6 segments")

    def test_yields_do_not_count_as_failures(self):
        self.mac_delay = 0.1
        ally = FakeAlly(script=["yielded"] * 3)
        ok, _frames, why = self.run_enc(ally)
        self.assertTrue(ok, why)
        self.assertGreaterEqual(len(ally.calls), 4)
        self.assertIn("ally", self.hosts().values())

    def test_refusals_suspensions_and_aborts_do_not_count_either(self):
        # "refused" cools the helper down (helper.py) like a yield; "suspended" and "aborted" are
        # nobody's fault. Five of them in a row still leave the Ally in the item.
        self.mac_delay = 0.1
        ally = FakeAlly(script=["refused", "suspended", "aborted", "refused", "yielded"])
        ok, _frames, why = self.run_enc(ally)
        self.assertTrue(ok, why)
        self.assertGreaterEqual(len(ally.calls), 6)
        self.assertIn("ally", self.hosts().values())
        self.assertEqual(sorted(int(k) for k in self.hosts()), list(range(6)))

    def test_a_suspended_segment_goes_back_to_the_ally(self):
        # D3: requeued at the BACK (the Ally's end), not handed to the Mac
        self.mac_delay = 0.2
        ally = FakeAlly(script=["suspended"])
        ok, _frames, why = self.run_enc(ally)
        self.assertTrue(ok, why)
        self.assertEqual([c["i"] for c in ally.calls[:2]], [5, 5])
        self.assertEqual(self.hosts()["5"], "ally")
        self.assertNotIn(5, self.mac)

    def test_nothing_is_claimed_while_encoders_are_suspended(self):
        asked = []
        ally = FakeAlly(available=lambda: asked.append(1) or True)
        self.mac_delay = 0.05
        dvcap.suspend_encoders()
        try:
            with mock.patch.object(dvcap, "HELPER_SUSPEND_POLL", 0.02):
                ok, frames, why = self.run_enc(ally)
        finally:
            dvcap.resume_encoders()
        self.assertEqual((ok, frames, why), (True, 600, "encoded 600 frames in 6 segments"))
        self.assertEqual(ally.calls, [])
        self.assertEqual(asked, [])                        # not even asked: no ssh probe either
        self.assertEqual(self.mac, list(range(6)))
        self.assertEqual(_ally_threads(), [])

    def test_a_suspension_mid_item_pauses_the_ally_until_resumed(self):
        events = []

        def ally_hook(rec, abort, on_frame):
            events.append(("ally", rec["i"], dvcap.encoders_suspended()))
            if len(events) == 1:                           # the suspension lands on its first one
                dvcap.suspend_encoders()
                return None, "suspended", "suspended"
            return None

        def mac_hook(i, out, abort, on_frame):
            import time
            time.sleep(0.1)
            if len([e for e in events if e[0] == "mac"]) == 1 and dvcap.encoders_suspended():
                events.append(("resume",))
                dvcap.resume_encoders()
            events.append(("mac", i))
            return None
        self.mac_hook = mac_hook
        ally = FakeAlly(hook=ally_hook)
        with mock.patch.object(dvcap, "HELPER_SUSPEND_POLL", 0.02):
            ok, _frames, why = self.run_enc(ally)
        self.assertTrue(ok, why)
        self.assertIn(("resume",), events)
        r = events.index(("resume",))
        self.assertEqual([e for e in events[:r] if e[0] == "ally"], [("ally", 5, False)])
        later = [e for e in events[r:] if e[0] == "ally"]
        self.assertTrue(later)
        self.assertEqual(later[0][1], 5)                   # the suspended segment, the Ally's again
        self.assertTrue(all(not e[2] for e in later))      # never called while suspended
        self.assertEqual(self.hosts()["5"], "ally")

    def test_the_mac_takes_back_a_segment_a_dead_ally_thread_held(self):
        # 7: the remote worker died holding a segment (here: its release raises SystemExit, which
        # skips the requeue); the Mac's idle loop must take it back instead of waiting forever
        import threading

        class DyingAlly(FakeAlly):
            def claim(self):
                return True

            def release(self):
                raise SystemExit

        self.mac_delay = 0.05
        ally = DyingAlly(script=["disconnected"], delay=0.02)
        box = {}
        th = threading.Thread(target=lambda: box.update(res=self.run_enc(ally)), daemon=True)
        th.start()
        th.join(20)
        self.assertFalse(th.is_alive(), "the Mac waited forever on a dead Ally thread")
        ok, frames, why = box["res"]
        self.assertTrue(ok, why)
        self.assertEqual(frames, 600)
        self.assertEqual([c["i"] for c in ally.calls], [5])
        self.assertIn(5, self.mac)
        self.assertEqual(self.hosts()["5"], "mac")
        self.assertEqual(_ally_threads(), [])

    def test_stale_remote_leftovers_go_on_the_resume(self):
        stale = [os.path.join(self.segdir, n) for n in
                 ("seg_0002.hevc.remote.part", "seg_0002.remote.rpu", "seg_0004.remote.rpu")]
        for p in stale:
            with open(p, "wb") as f:
                f.write(b"stale")
        seen = {}

        def mac_hook(i, out, abort, on_frame):
            if i == 0:
                seen["left"] = [p for p in stale if os.path.exists(p)]
            return None
        self.mac_hook = mac_hook
        ok, _frames, why = self.run_enc(FakeAlly(available=False))
        self.assertTrue(ok, why)
        self.assertEqual(seen["left"], [])                 # gone before the first encode
        self.assertEqual([p for p in stale if os.path.exists(p)], [])

    def test_stale_remote_leftovers_go_when_the_mac_takes_the_segment(self):
        stale = [os.path.join(self.segdir, n) for n in ("seg_0003.hevc.remote.part",
                                                          "seg_0003.remote.rpu")]
        seen = {}

        def mac_hook(i, out, abort, on_frame):
            if i == 0:                                     # appears mid-run (a dead remote's)
                for p in stale:
                    with open(p, "wb") as f:
                        f.write(b"stale")
            if i == 3:
                seen["left"] = [p for p in stale if os.path.exists(p)]
            return None
        self.mac_hook = mac_hook
        ok, _frames, why = self.run_enc(FakeAlly(available=False))
        self.assertTrue(ok, why)
        self.assertEqual(seen["left"], [])

    def test_seg_done_is_the_plan_order_prefix(self):
        # 9: the Ally runs ahead from the back, so "frames passed each end" over-counts finished
        # segments; a callback that takes seg_done hears the plan-order prefix instead
        self.mac_delay = 0.3
        seen, extra = [], []
        ok, _frames, why = self.run_enc(
            FakeAlly(delay=0.02),
            on_progress=lambda f, total, seg_done: seen.append((f, seg_done)))
        self.assertTrue(ok, why)
        done = [d for _f, d in seen]
        self.assertEqual(done, sorted(done))               # never backwards
        self.assertEqual(seen[-1], (600, 6))
        self.assertTrue(any(f >= 200 and d == 0 for f, d in seen), seen)   # seg 0 still running
        ends = [self.N * (k + 1) for k in range(6)]
        for f, d in seen:                                   # never more than the frames allow
            self.assertLessEqual(d, sum(1 for e in ends if f >= e))
        ok, _frames, why = self.run_enc(FakeAlly(), on_progress=lambda f, t, **kw: extra.append(kw))
        self.assertTrue(ok, why)
        self.assertTrue(extra and all("seg_done" in kw for kw in extra))

    def test_a_peak_repair_records_the_mac_as_the_segments_host(self):
        # 9: reencode_segments_tighter re-encodes on the Mac: hosts.json must say so
        self.mac_delay = 0.05
        ok, _frames, why = self.run_enc(FakeAlly(delay=0.1))
        self.assertTrue(ok, why)
        ally_segs = sorted(int(k) for k, v in self.hosts().items() if v == "ally")
        self.assertTrue(ally_segs)
        j = ally_segs[0]
        ok, why = dvcap.reencode_segments_tighter(
            "/r.mov", "/rpu.bin", self.segdir, [j], 40, total_frames=600, fps=self.FPS,
            boundaries=[self.N * (k + 1) for k in range(6)])
        self.assertTrue(ok, why)
        hosts = self.hosts()
        self.assertEqual(hosts[str(j)], "mac")
        self.assertEqual(len(hosts), 6)                    # the others untouched

    def test_a_peak_repair_without_a_helper_writes_no_hosts_file(self):
        ok, why = dvcap.reencode_segments_tighter(
            "/r.mov", "/rpu.bin", self.segdir, [1], 40, total_frames=600, fps=self.FPS,
            boundaries=[self.N * (k + 1) for k in range(6)])
        self.assertTrue(ok, why)
        self.assertFalse(os.path.exists(os.path.join(self.segdir, "hosts.json")))

    def test_an_unavailable_ally_gets_nothing_and_holds_nothing_up(self):
        import time
        ally = FakeAlly(available=False)
        t0 = time.monotonic()
        ok, frames, why = self.run_enc(ally)
        self.assertLess(time.monotonic() - t0, 5.0)        # not one HELPER_WAIT_SECONDS
        self.assertEqual((ok, frames, why), (True, 600, "encoded 600 frames in 6 segments"))
        self.assertEqual(ally.calls, [])
        self.assertEqual(self.mac, list(range(6)))
        self.assertEqual(_ally_threads(), [])

    def test_on_segment_fires_in_plan_order(self):
        self.mac_delay = 0.1
        ally = FakeAlly()
        heard = []
        ok, _frames, why = self.run_enc(
            ally, on_segment=lambda i, p: heard.append((i, p, os.path.exists(self.seg(5)))))
        self.assertTrue(ok, why)
        self.assertEqual([(i, p) for i, p, _ in heard], [(i, self.seg(i)) for i in range(6)])
        self.assertTrue(heard[0][2])                        # the Ally's seg 5 was done first, held back

    def test_progress_counts_both_in_flight_segments(self):
        import threading
        ally_half, mac_done = threading.Event(), threading.Event()

        def ally_hook(rec, abort, on_frame):
            on_frame(50)
            ally_half.set()
            mac_done.wait(5)
            with open(rec["part"], "wb") as f:
                f.write(b"A" * rec["n"])
            on_frame(100)
            return rec["n"], "ok", "ok"

        def mac_hook(i, out, abort, on_frame):
            ally_half.wait(5)
            on_frame(30)                                    # 0 finished + 30 (Mac) + 50 (Ally)
            with open(out, "wb") as f:
                f.write(b"M" * self.N)
            on_frame(100)
            mac_done.set()
            return self.N, "ok", []
        self.mac_hook = mac_hook
        seen = []
        ok, frames, why = self.run_enc(FakeAlly(hook=ally_hook), segs=2,
                                       on_progress=lambda f, total: seen.append((f, total)))
        self.assertTrue(ok, why)
        values = [f for f, _t in seen]
        self.assertIn(80, values)
        self.assertEqual(values, sorted(values))            # never backwards
        self.assertEqual(seen[-1], (200, 200))
        self.assertEqual(why, "encoded 200 frames in 2 segments (1 segment on the helper)")

    def _blocking_ally(self, saw):
        def hook(rec, abort, on_frame):
            on_frame(10)
            saw["abort"] = _wait_for(abort.is_set)
            return None, "aborted", "aborted"
        return FakeAlly(hook=hook)

    def test_a_pause_mid_remote_segment_stops_and_joins_the_ally(self):
        self.mac_delay = 0.2
        saw = {}
        ally = self._blocking_ally(saw)
        ok, frames, why = self.run_enc(ally, segs=3, should_pause=lambda: os.path.exists(self.seg(0)))
        self.assertFalse(ok)
        self.assertTrue(why.startswith("paused:"), why)
        self.assertEqual(frames, 100)
        self.assertEqual(ally.calls[0]["i"], 2)
        self.assertTrue(saw["abort"])
        self.assertEqual(_ally_threads(), [])               # joined before the return
        self.assertEqual(self.mac, [0])
        self.assertFalse(os.path.exists(self.seg(2)))
        self.assertFalse(os.path.exists(self.seg(2) + ".remote.part"))

    def test_an_abort_mid_remote_segment_stops_and_joins_the_ally(self):
        import threading, time
        abort, saw = threading.Event(), {}

        def mac_hook(i, out, ab, on_frame):
            time.sleep(0.2)
            with open(out, "wb") as f:
                f.write(b"M" * self.N)
            abort.set()
            return self.N, "ok", []
        self.mac_hook = mac_hook
        ok, frames, why = self.run_enc(self._blocking_ally(saw), segs=3, abort=abort)
        self.assertEqual((ok, frames, why), (False, 100, "aborted"))
        self.assertTrue(saw["abort"])
        self.assertEqual(_ally_threads(), [])

    def test_a_mac_failure_stops_the_ally_and_fails_as_before(self):
        import time

        def mac_hook(i, out, ab, on_frame):
            time.sleep(0.1)
            with open(out, "wb") as f:
                f.write(b"M" * 5)
            return 5, "ok", []
        self.mac_hook = mac_hook
        saw = {}
        ok, frames, why = self.run_enc(self._blocking_ally(saw), segs=3)
        self.assertEqual((ok, frames, why), (False, 0, "segment 1/3 frame mismatch: 5 != 100"))
        self.assertTrue(saw["abort"])
        self.assertEqual(_ally_threads(), [])

    def test_the_ally_gets_the_same_decode_args_and_cap(self):
        self.mac_delay = 0.3
        with open(os.path.join(self.segdir, "tight_caps.json"), "w") as f:
            json.dump({"5": 40}, f)
        md, cll = "G(1,2)B(3,4)R(5,6)WP(7,8)L(9,10)", "1000,400"
        ally = FakeAlly()
        ok, _frames, why = self.run_enc(ally, master_display=md, max_cll=cll)
        self.assertTrue(ok, why)
        by = {c["i"]: c for c in ally.calls}
        self.assertEqual(by[5]["dec"], dvcap.build_seg_decode_command(dvcap.FFMPEG, "/r.mov", 500,
                                                                       100, self.FPS, None))
        self.assertEqual(by[5]["args"], dvcap.build_x265_command("X265", "{rpu}", "{out}", 40, md, cll)[1:])
        self.assertEqual(by[4]["args"], dvcap.build_x265_command("X265", "{rpu}", "{out}", 50, md, cll)[1:])
        self.assertEqual(by[5]["rpu"], os.path.join(self.segdir, "seg_0005.remote.rpu"))
        self.assertEqual(by[5]["rpu_bytes"], b"R" * 100)    # its own slice, [500, 600)
        self.assertFalse(os.path.exists(by[5]["rpu"]))      # and tidied away
        self.assertEqual(by[5]["n"], 100)

    def test_an_sdr_item_sends_no_rpu(self):
        self.mac_delay = 0.2
        ally = FakeAlly()
        ok, _frames, why = self.run_enc(ally, dv=False)
        self.assertTrue(ok, why)
        rec = ally.calls[0]
        self.assertIsNone(rec["rpu"])
        self.assertEqual(rec["args"], dvcap.build_x265_command("X265", None, "{out}", 50, dv=False)[1:])
        self.assertNotIn("{rpu}", rec["args"])

    def test_an_ok_that_fails_the_local_count_goes_back_to_the_mac(self):
        self.mac_delay = 0.1
        ally = FakeAlly(script=["short"], delay=0.05)
        ok, _frames, why = self.run_enc(ally)
        self.assertTrue(ok, why)
        self.assertEqual(ally.calls[0]["i"], 5)
        self.assertEqual(self.hosts()["5"], "mac")
        self.assertFalse(os.path.exists(self.seg(5) + ".remote.part"))

    def test_a_resume_skips_finished_segments_and_keeps_their_host(self):
        for i, b in ((0, b"M"), (5, b"A")):
            with open(self.seg(i), "wb") as f:
                f.write(b * self.N)
        with open(os.path.join(self.segdir, "hosts.json"), "w") as f:
            json.dump({"5": "ally"}, f)
        self.mac_delay = 0.05
        ally, heard = FakeAlly(delay=0.08), []
        ok, frames, why = self.run_enc(ally, on_segment=lambda i, p: heard.append(i))
        self.assertTrue(ok, why)
        self.assertEqual(frames, 600)
        self.assertEqual(ally.calls[0]["i"], 4)
        self.assertNotIn(0, self.mac)
        self.assertNotIn(5, [c["i"] for c in ally.calls])
        self.assertEqual(heard, list(range(6)))
        self.assertEqual(self.hosts()["5"], "ally")
        self.assertNotIn("0", self.hosts())

    def test_no_helper_is_the_mac_only_encode(self):
        ok, frames, why = self.run_enc(None)
        self.assertEqual((ok, frames, why), (True, 600, "encoded 600 frames in 6 segments"))
        self.assertEqual(self.mac, list(range(6)))
        self.assertFalse(os.path.exists(os.path.join(self.segdir, "hosts.json")))

    # -- C1: on_segments ------------------------------------------------------------------------
    def test_on_segments_is_the_bars_live_state_from_both_machines(self):
        import threading
        ally_half, mac_half = threading.Event(), threading.Event()
        infos, overlap, gate = [], [], threading.Lock()

        def serial(fn):
            def call(*a, **kw):
                if not gate.acquire(blocking=False):        # one lock: never two at once
                    overlap.append(fn.__name__)
                    gate.acquire()
                try:
                    fn(*a, **kw)
                finally:
                    gate.release()
            return call

        @serial
        def on_segments(info):
            infos.append(json.loads(json.dumps(info)))      # JSON-able, copied as heard

        @serial
        def on_progress(f, total, **_kw):
            pass

        def ally_hook(rec, abort, on_frame):
            on_frame(50, fps=7.94)
            ally_half.set()
            mac_half.wait(5)
            time.sleep(0.05)
            with open(rec["part"], "wb") as f:
                f.write(b"A" * rec["n"])
            on_frame(100, fps=8.0)
            return rec["n"], "ok", "ok"

        def mac_hook(i, out, abort, on_frame):
            ally_half.wait(5)
            on_frame(30)
            mac_half.set()
            time.sleep(0.2)
            with open(out, "wb") as f:
                f.write(b"M" * self.N)
            on_frame(100)
            return self.N, "ok", []
        import time
        self.mac_hook = mac_hook
        noted = []
        ally = FakeAlly(hook=ally_hook)
        ally.note_segments = noted.append
        ok, _frames, why = self.run_enc(ally, segs=2, on_progress=on_progress,
                                        on_segments=on_segments)
        self.assertTrue(ok, why)
        self.assertEqual(overlap, [])
        self.assertTrue(infos)
        for info in infos:
            self.assertEqual(set(info), {"done", "helper_done", "mac", "helper"})
            self.assertEqual(info["done"], sorted(info["done"]))
            for who in ("mac", "helper"):
                if info[who] is not None:
                    self.assertTrue(0.0 <= info[who][1] <= 1.0, info)
        both = [x for x in infos if x["mac"] == [0, 0.3] and x["helper"] == [1, 0.5, 7.94]]
        self.assertTrue(both, infos)                        # each in its own slot, live
        self.assertIn({"done": [1], "helper_done": [1], "mac": [0, 0.3], "helper": None}, infos)
        self.assertEqual(infos[-1], {"done": [0, 1], "helper_done": [1], "mac": None, "helper": None})
        self.assertEqual(noted, infos)                      # the helper's status hears the same

    def test_each_report_hands_on_segments_its_info_before_on_progress(self):
        # 1: a listener that publishes on the progress tick (stages' remux bar) must already
        # hold THIS report's positions and fps — never the previous report's
        import threading
        import time
        ally_half, mac_half = threading.Event(), threading.Event()
        events, last, stale = [], {}, []

        def on_segments(info):
            events.append("seg")
            last["info"] = json.loads(json.dumps(info))

        def on_progress(v, total, seg_done=None):
            # (recorded, not asserted here: the remote thread swallows a raising callback)
            events.append("prog")
            info = last.get("info")
            if info is None:
                stale.append(("no info yet", v))
                return
            frames = self.N * len(info["done"])
            for who in ("mac", "helper"):
                if info[who] is not None:
                    frames += round(info[who][1] * self.N)
            if v != frames:                                 # the info must BE this report's
                stale.append((v, info))

        def ally_hook(rec, abort, on_frame):
            on_frame(50, fps=7.94)
            ally_half.set()
            mac_half.wait(5)
            with open(rec["part"], "wb") as f:
                f.write(b"A" * rec["n"])
            on_frame(100, fps=8.0)
            return rec["n"], "ok", "ok"

        def mac_hook(i, out, abort, on_frame):
            ally_half.wait(5)
            on_frame(30)
            mac_half.set()
            time.sleep(0.1)
            with open(out, "wb") as f:
                f.write(b"M" * self.N)
            on_frame(100)
            return self.N, "ok", []
        self.mac_hook = mac_hook
        ok, _frames, why = self.run_enc(FakeAlly(hook=ally_hook), segs=2, on_progress=on_progress,
                                        on_segments=on_segments)
        self.assertTrue(ok, why)
        self.assertEqual(stale, [])
        self.assertGreaterEqual(events.count("prog"), 4)
        for k, ev in enumerate(events):                     # seg, prog, seg, prog, ...
            self.assertEqual(ev, "seg" if k % 2 == 0 else "prog", events)

    def test_helper_note_is_the_success_suffix(self):
        n = dvcap.helper_note
        self.assertEqual(n("encoded 600 frames in 6 segments (2 segments on the helper)"),
                         " (2 segments on the helper)")
        self.assertEqual(n("encoded 100 frames in 2 segments (1 segment on the helper)"),
                         " (1 segment on the helper)")
        for why in ("encoded 600 frames in 6 segments", "", None, "ok",
                    "encoded 6 frames in 1 segment (0 segments on the helper)"):
            self.assertEqual(n(why), "", why)

    def test_on_segments_without_fps_estimates_one_and_a_broken_listener_is_harmless(self):
        self.mac_delay = 0.2
        heard = []

        def on_segments(info):
            heard.append(info)
            raise RuntimeError("UI went away")
        ok, frames, why = self.run_enc(FakeAlly(delay=0.05), segs=3, on_segments=on_segments)
        self.assertEqual((ok, frames), (True, 300), why)
        rates = [x["helper"][2] for x in heard if x["helper"] is not None]
        self.assertTrue(rates and all(isinstance(r, float) and r >= 0 for r in rates), heard)
        self.assertEqual(heard[-1]["done"], [0, 1, 2])

    def test_no_on_segments_without_a_helper(self):
        heard = []
        ok, _frames, why = self.run_enc(None, segs=2, on_segments=heard.append)
        self.assertTrue(ok, why)
        self.assertEqual(heard, [])

    # -- 4: a stop after the helper finished ---------------------------------------------------
    def test_a_stop_after_the_helper_finished_keeps_its_segment(self):
        import threading
        stop_now = threading.Event()

        def ally_hook(rec, abort, on_frame):
            with open(rec["part"], "wb") as f:
                f.write(b"A" * rec["n"])
            stop_now.set()                                  # the pause lands now ...
            saw = _wait_for(abort.is_set, 5)                # ... and reaches this call first
            return (rec["n"], "ok", "ok") if saw else (None, "error", "error")
        self.mac_delay = 0.3
        ok, frames, why = self.run_enc(FakeAlly(hook=ally_hook), segs=3,
                                       should_pause=stop_now.is_set)
        self.assertFalse(ok)
        self.assertTrue(why.startswith("paused:"), why)
        self.assertEqual(os.path.getsize(self.seg(2)), self.N)   # verified, published, kept
        self.assertEqual(self.hosts()["2"], "ally")
        self.assertEqual(_ally_threads(), [])
        self.mac = []
        ok, frames, why = self.run_enc(FakeAlly(available=False), segs=3)   # the next attempt
        self.assertEqual((ok, frames), (True, 300), why)
        self.assertEqual(self.mac, [1])                     # 0 (the Mac's) and 2 (the helper's) kept

    # -- 5: a suspended segment stays the helper's past the resume -----------------------------
    def _suspend_once(self, resume_after=0.3):
        import threading
        calls = []

        def ally_hook(rec, abort, on_frame):
            calls.append(rec["i"])
            if len(calls) == 1:
                dvcap.suspend_encoders()
                threading.Timer(resume_after, dvcap.resume_encoders).start()
                return None, _SUSP, "suspended"
            return None
        return calls, ally_hook

    def _mac_waits_for_the_first_remote_call(self, calls):
        def mac_hook(i, out, abort, on_frame):
            if i == 0:
                _wait_for(lambda: calls, 5)
            return None
        return mac_hook

    def test_a_suspended_segment_waits_for_the_helper_after_the_resume(self):
        calls, hook = self._suspend_once()
        self.mac_hook = self._mac_waits_for_the_first_remote_call(calls)
        with mock.patch.object(dvcap, "HELPER_SUSPEND_POLL", 0.02):
            ok, frames, why = self.run_enc(FakeAlly(hook=hook), segs=2)
        self.assertEqual((ok, frames), (True, 200), why)
        self.assertEqual(calls, [1, 1])                     # the helper reclaimed it
        self.assertEqual(self.mac, [0])                     # the idle Mac left it alone
        self.assertEqual(self.hosts(), {"0": "mac", "1": "ally"})

    def test_the_mac_takes_it_when_the_helper_does_not_reclaim_it_in_time(self):
        import time
        calls, hook = self._suspend_once(resume_after=0.1)
        self.mac_hook = self._mac_waits_for_the_first_remote_call(calls)
        slow = lambda: (not calls) or (time.sleep(2.0) or True)   # its check hangs after the resume
        with mock.patch.object(dvcap, "HELPER_SUSPEND_POLL", 0.02), \
                mock.patch.object(dvcap, "HELPER_RESERVE_SECONDS", 0.3):
            ok, frames, why = self.run_enc(FakeAlly(hook=hook, available=slow), segs=2)
        self.assertEqual((ok, frames), (True, 200), why)
        self.assertEqual(calls, [1])
        self.assertEqual(self.mac, [0, 1])
        self.assertEqual(self.hosts(), {"0": "mac", "1": "mac"})

    def test_the_mac_takes_it_at_once_when_the_helper_is_unavailable(self):
        import threading, time
        self.assertEqual(dvcap.HELPER_RESERVE_SECONDS, 120)
        calls, hook = self._suspend_once(resume_after=0.1)
        self.mac_hook = self._mac_waits_for_the_first_remote_call(calls)
        box = {}

        def go():
            with mock.patch.object(dvcap, "HELPER_SUSPEND_POLL", 0.02):
                box["r"] = self.run_enc(FakeAlly(hook=hook, available=lambda: not calls), segs=2)
        t0 = time.monotonic()
        th = threading.Thread(target=go, daemon=True)
        th.start()
        th.join(15)
        self.assertFalse(th.is_alive(), "the Mac sat out the whole reservation")
        self.assertLess(time.monotonic() - t0, 10)
        ok, frames, why = box["r"]
        self.assertEqual((ok, frames), (True, 200), why)
        self.assertEqual(self.mac, [0, 1])
        self.assertEqual(self.hosts()["1"], "mac")

    def test_end_to_end_over_the_fake_wire(self):
        # the real Helper: y4m from a fake decoder -> real zstd -> a fake vworker (real zstd -d)
        # -> HEVC back into the part; the Mac's own segments run a fake x265 through _encode_pipe
        import test_helper
        import helper
        if not test_helper.ZSTD_OK:
            self.skipTest("needs /opt/homebrew/bin/zstd")
        self.patches[2].stop()                              # the REAL _encode_pipe for the Mac
        self.patches.pop(2)
        aud = test_helper.AUD                               # the fake helper's frame; the Mac's is b"F"

        def frames_in(path, ffprobe=None):
            if not (path and os.path.exists(path)):
                return 0
            with open(path, "rb") as f:
                data = f.read()
            return data.count(aud) if data.startswith(aud) else len(data)
        self.patches[0].stop()
        self.patches[0] = mock.patch.object(dvcap, "count_hevc_frames", side_effect=frames_in)
        self.patches[0].start()
        fakes = test_helper.write_fakes(self.td)
        root = os.path.join(self.td, "ally")
        os.makedirs(root)
        helper.reset_state()
        h = test_helper.fake_helper(fakes["vworker"], root)
        env = {"FAKE_W": "4", "FAKE_H": "2", "FAKE_DEC_SLEEP": "0.003", "FAKE_DEC_FAIL_AT": "-1"}
        try:
            with mock.patch.dict(os.environ, env):
                ok, frames, why = self.run_enc(h, segs=4, ffmpeg=fakes["ffmpeg"], x265=fakes["x265"])
        finally:
            helper.reset_state()
        self.assertTrue(ok, why)
        self.assertEqual(frames, 400)
        hosts = self.hosts()
        self.assertEqual(sorted(hosts), ["0", "1", "2", "3"])
        self.assertIn("ally", hosts.values())
        self.assertIn("mac", hosts.values())
        k = list(hosts.values()).count("ally")
        with open(self.out, "rb") as f:                     # plan order, no stream marker anywhere
            self.assertEqual(f.read(), b"".join((aud if hosts[str(i)] == "ally" else b"F") * self.N
                                                for i in range(4)))
        self.assertTrue(why.endswith(" on the helper)"), why)
        self.assertIn(f"({k} segment", why)
        self.assertEqual([x for x in os.listdir(self.segdir) if "remote" in x or x.endswith(".part")], [])
        self.assertEqual(len(dvcap._LIVE_ENCODERS), 0)
