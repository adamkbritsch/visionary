"""Dolby Vision profile 7 -> 8.1 (engine/dvp7.py): the parts that decide whether a real movie gets
replaced. Every verification is exercised in the direction that matters — a bad file must be
REFUSED — because the caller replaces the NAS original, with no backup, the moment verify() returns.
"""
import os
import tempfile
import unittest
from unittest import mock

import dvp7


def track(tid, kind="video", codec="HEVC/H.265/MPEG-H", dims="3840x2160", **props):
    p = {"pixel_dimensions": dims} if kind == "video" else {}
    p.update(props)
    return {"id": tid, "type": kind, "codec": codec if kind == "video" else "TrueHD Atmos",
            "properties": p}


def info(tracks, duration=7_000_000_000_000, chapters=1, attachments=0, title=""):
    return {"tracks": tracks, "chapters": [{}] * chapters, "attachments": [{}] * attachments,
            "container": {"properties": {"duration": duration, "title": title}}}


class Layout(unittest.TestCase):
    def test_single_track_p7(self):
        bl, el, dual = dvp7.layout(info([track(0), track(1, "audio")]))
        self.assertEqual((bl["id"], el, dual), (0, None, False))

    def test_dual_track_picks_the_4k_base_layer_and_the_1080p_el(self):
        bl, el, dual = dvp7.layout(info([track(0, dims="1920x1080"), track(1), track(2, "audio")]))
        self.assertEqual((bl["id"], el["id"], dual), (1, 0, True))

    def test_an_mjpeg_cover_is_not_a_second_video_layer(self):
        """Gladiator, JW3 and both Hobbits carry an MJPEG cover track Plex shows as a 2nd video."""
        cover = track(2, codec="MJPEG", dims="600x900")
        bl, el, dual = dvp7.layout(info([track(0), track(1, "audio"), cover]))
        self.assertEqual((bl["id"], dual), (0, False))

    def test_anything_else_is_refused_rather_than_guessed(self):
        with self.assertRaises(RuntimeError):
            dvp7.layout(info([track(0), track(1)]))              # two 4K HEVC tracks
        with self.assertRaises(RuntimeError):
            dvp7.layout(info([track(0, "audio")]))               # no HEVC at all


class DefaultDuration(unittest.TestCase):
    def test_the_rates_are_spelled_the_way_mkvmerge_wants(self):
        self.assertEqual(dvp7.default_duration(41708333), "24000/1001p")
        self.assertEqual(dvp7.default_duration(1e9 / 24), "24p")
        self.assertEqual(dvp7.default_duration(40000000), "25p")
        self.assertEqual(dvp7.default_duration(1e9 * 1001 / 60000), "60000/1001p")

    def test_an_odd_rate_is_passed_through_exactly(self):
        self.assertEqual(dvp7.default_duration(43478260), "43478260ns")


class FramesTag(unittest.TestCase):
    def test_reads_the_statistics_tag(self):
        i = info([track(0, tag_number_of_frames="173304")])
        self.assertEqual(dvp7.frames_tag(i, 0), 173304)

    def test_missing_or_junk_is_none_so_the_caller_recounts(self):
        self.assertIsNone(dvp7.frames_tag(info([track(0)]), 0))
        self.assertIsNone(dvp7.frames_tag(info([track(0, tag_number_of_frames="x")]), 0))


class Verify(unittest.TestCase):
    """verify() is the last thing between a new file and a replaced original."""

    def _insp(self, **kw):
        src = info([track(0, tag_number_of_frames="1000"), track(1, "audio"), track(2, "subtitles")],
                   **kw)
        return {"info": src, "bl": src["tracks"][0], "el": None, "dual": False, "el_type": "FEL",
                "default_duration": 41708333}

    def _run(self, *, dv=None, rpu=8, out_tracks=3, out_frames="1000", dur_ms=0, chapters=1,
             pts_src=None, pts_out=None, recount=(1000, 1000)):
        insp = self._insp()
        out_tr = [track(0, tag_number_of_frames=out_frames), track(1, "audio"), track(2, "subtitles"),
                  track(3, "audio")][:out_tracks]
        oinfo = info(out_tr, duration=7_000_000_000_000 + dur_ms * 1_000_000, chapters=chapters)
        d = tempfile.mkdtemp()
        out = os.path.join(d, "out.mkv")
        with open(out, "wb") as fh:
            fh.write(b"x" * 10)
        with mock.patch.object(dvp7, "probe_dv", return_value=dv or {
                "dv_profile": 8, "dv_bl_signal_compatibility_id": 1, "el_present_flag": 0}), \
             mock.patch.object(dvp7, "rpu_profile", return_value=(rpu, None)), \
             mock.patch.object(dvp7, "mkv_info", return_value=oinfo), \
             mock.patch.object(dvp7, "count_packets", side_effect=list(recount)), \
             mock.patch.object(dvp7, "first_pts", side_effect=[pts_src or {"0": 0.0, "1": 0.0},
                                                                pts_out or {"0": 0.0, "1": 0.0}]):
            return dvp7.verify("/src.mkv", out, insp, d)

    def test_a_faithful_file_passes(self):
        self.assertEqual(self._run()["frames"], 1000)

    def test_profile_7_side_data_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "side data"):
            self._run(dv={"dv_profile": 7, "dv_bl_signal_compatibility_id": 6, "el_present_flag": 1})

    def test_a_leftover_enhancement_layer_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "side data"):
            self._run(dv={"dv_profile": 8, "dv_bl_signal_compatibility_id": 1, "el_present_flag": 1})

    def test_an_rpu_that_does_not_read_8_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "RPU"):
            self._run(rpu=7)

    def test_a_lost_track_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "track count"):
            self._run(out_tracks=2)

    def test_lost_chapters_are_refused(self):
        with self.assertRaisesRegex(RuntimeError, "chapters"):
            self._run(chapters=0)

    def test_a_duration_off_by_more_than_100ms_is_refused(self):
        self.assertEqual(self._run(dur_ms=90)["frames"], 1000)
        with self.assertRaisesRegex(RuntimeError, "duration"):
            self._run(dur_ms=150)

    def test_a_disagreeing_tag_is_recounted_not_trusted(self):
        # tags say 999 vs 1000, the real counts agree -> passes, and it DID recount
        self.assertEqual(self._run(out_frames="999", recount=(1000, 1000))["frames"], 1000)

    def test_a_real_frame_loss_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "frame count"):
            self._run(out_frames="998", recount=(1000, 998))

    def test_a_shifted_start_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "start timestamps"):
            self._run(pts_out={"0": 0.042, "1": 0.0})
        with self.assertRaisesRegex(RuntimeError, "start timestamps"):
            self._run(pts_out={"0": 0.0, "1": 0.021})              # audio moved


class Convert(unittest.TestCase):
    def test_missing_tools_are_named_with_the_install_command(self):
        with mock.patch.object(dvp7, "tools_missing", return_value=["mkvmerge"]):
            with self.assertRaisesRegex(RuntimeError, "brew install mkvtoolnix"):
                dvp7.convert("/src.mkv", "/out.mkv", tempfile.mkdtemp())

    def test_a_failure_never_leaves_a_half_written_file(self):
        d = tempfile.mkdtemp()
        out = os.path.join(d, "out.mkv")
        def half_write(src, o, work, insp, **kw):
            with open(o, "wb") as fh:
                fh.write(b"partial")
            raise RuntimeError("mkvmerge 2: boom")
        with mock.patch.object(dvp7, "tools_missing", return_value=[]), \
             mock.patch.object(dvp7, "inspect", return_value={"el_type": "MEL", "dual": False}), \
             mock.patch.object(dvp7, "build", side_effect=half_write):
            with self.assertRaises(RuntimeError):
                dvp7.convert("/src.mkv", out, d)
        self.assertFalse(os.path.exists(out))
        self.assertFalse(os.path.exists(dvp7.building_path(out)))

    def test_the_final_name_only_ever_holds_a_verified_file(self):
        """A kill mid-mux must not leave a file under the name the lane takes as converted."""
        d = tempfile.mkdtemp()
        out = os.path.join(d, "p81.mkv")
        written = []
        def build(src, o, work, insp, **kw):
            written.append(o)
            with open(o, "wb") as fh:
                fh.write(b"new")
        def verify(src, o, insp, work, **kw):
            self.assertFalse(os.path.exists(out))            # not yet under the final name
            return {"frames": 1, "size_out": 3}
        with mock.patch.object(dvp7, "tools_missing", return_value=[]), \
             mock.patch.object(dvp7, "inspect", return_value={"el_type": "MEL", "dual": False}), \
             mock.patch.object(dvp7, "build", side_effect=build), \
             mock.patch.object(dvp7, "verify", side_effect=verify):
            dvp7.convert("/src.mkv", out, d)
        self.assertEqual(written, [dvp7.building_path(out)])
        self.assertTrue(os.path.exists(out))
        self.assertFalse(os.path.exists(dvp7.building_path(out)))

    def test_dual_track_intermediates_are_removed_when_the_build_fails(self):
        d = tempfile.mkdtemp()
        insp = {"bl": {"id": 1, "properties": {}}, "el": {"id": 0, "properties": {}}, "dual": True,
                "info": {"container": {"properties": {}}}, "default_duration": 41708333}
        def run(cmd, **kw):
            if cmd[0] == dvp7.FFMPEG:                     # extraction writes a big file...
                with open(cmd[cmd.index("-f") + 2], "wb") as fh:
                    fh.write(b"layer")
                return 0, "", ""
            return 1, "", "dovi_tool: bad RPU"            # ...then the RPU step fails
        with mock.patch.object(dvp7, "_run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "dovi_tool"):
                dvp7.build("/src.mkv", os.path.join(d, "o.mkv"), d, insp)
        self.assertEqual(os.listdir(d), [])


if __name__ == "__main__":
    unittest.main()
