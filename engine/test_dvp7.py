"""Dolby Vision profile 7 -> 8.1 (engine/dvp7.py): the parts that decide whether a real movie gets
replaced. Every verification is exercised in the direction that matters — a bad file must be
REFUSED — because the caller replaces the NAS original, with no backup, the moment verify() returns.
"""
import json
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
             pts_src=None, pts_out=None, recount=(1000, 1000), bare=0, end_src=6999.958,
             end_out=6999.958, vs_src=0.0, vs_out=0.0, dups=(), dv_only=0, pictures=None):
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
             mock.patch.object(dvp7, "tail_packets", side_effect=lambda path, *a, **k: [
                 {"pts_time": str(end_src if path == "/src.mkv" else end_out)}]), \
             mock.patch.object(dvp7, "dv_only_tail", return_value=bare), \
             mock.patch.object(dvp7, "repeated_pts", return_value=list(dups)), \
             mock.patch.object(dvp7, "dv_only_blocks", return_value=dv_only), \
             mock.patch.object(dvp7, "video_start",
                               side_effect=lambda path, tid: vs_src if path == "/src.mkv" else vs_out), \
             mock.patch.object(dvp7, "first_pts", side_effect=[pts_src or {"0": 0.0, "1": 0.0},
                                                                pts_out or {"0": 0.0, "1": 0.0}]):
            return dvp7.verify("/src.mkv", out, insp, d, pictures=pictures)

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

    def test_a_first_block_a_millisecond_off_is_not_a_shifted_start(self):
        # an open-GOP source: earliest frame 811 ms on both, first blocks 937 vs 936 from rounding
        self.assertEqual(self._run(vs_src=0.811, vs_out=0.811, pts_src={"0": 0.937, "1": 0.0},
                                   pts_out={"0": 0.936, "1": 0.0})["frames"], 1000)

    def test_a_trailing_dolby_vision_only_block_is_not_a_lost_frame(self):
        # Joker and three more (2026-10-01): the last block holds only the EL and RPU of the final
        # frame. Dropping the EL leaves no picture there, so 999 frames from 1000 blocks is right.
        self.assertEqual(self._run(out_frames="999", bare=1)["frames"], 999)

    def test_picture_less_blocks_in_the_middle_are_allowed_for_once_read(self):
        # Gladiator (2026-10-03): 21 such blocks mid-film plus the last one — 22 fewer frames
        self.assertEqual(self._run(out_frames="978", recount=(1000, 978), bare=1,
                                   dups=list(range(22)), dv_only=22)["frames"], 978)

    def test_repeated_blocks_that_hold_pictures_are_not_excused(self):
        with self.assertRaisesRegex(RuntimeError, "frame count 978"):
            self._run(out_frames="978", recount=(1000, 978), bare=1, dups=list(range(22)), dv_only=1)

    def test_too_few_repeats_to_explain_the_shortfall_are_not_even_read(self):
        with mock.patch.object(dvp7, "dv_only_blocks", side_effect=AssertionError("read")):
            with self.assertRaisesRegex(RuntimeError, "frame count"):
                self._run(out_frames="978", recount=(1000, 978), bare=1, dups=list(range(5)))

    def test_a_real_loss_beside_the_quirk_is_still_refused(self):
        with self.assertRaisesRegex(RuntimeError, r"frame count 998 != the original's 999 \(1000 blocks"):
            self._run(out_frames="998", recount=(1000, 998), bare=1)

    def test_the_quirk_never_excuses_a_frame_too_many(self):
        # had the leftover RPU become a picture of its own, the new file would be wrong too
        with self.assertRaisesRegex(RuntimeError, "frame count"):
            self._run(out_frames="1000", recount=(1000, 1000), bare=1)

    def test_a_video_that_ends_elsewhere_is_refused(self):
        # Only the start is carried over to a raw stream: a mid-film timestamp jump in the original
        # would come out as a drift that frame count, start and duration all miss (review).
        self.assertEqual(self._run(end_out=6999.957)["frames"], 1000)      # ms rounding: fine
        with self.assertRaisesRegex(RuntimeError, "the video ends at"):
            self._run(end_out=6999.458)
        with self.assertRaisesRegex(RuntimeError, "the video ends at"):
            self._run(end_out=7000.000)

    def test_a_shifted_start_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "start timestamps"):
            self._run(vs_out=0.042)
        with self.assertRaisesRegex(RuntimeError, "start timestamps"):
            self._run(pts_out={"0": 0.0, "1": 0.021})              # audio moved


# Joker (2019) [2160p UHD BluRay REMUX ... NAHOM]: its last three video blocks, read from the NAS
# original on 2026-10-01 — an ordinary frame (picture, then its EL and RPU), the final picture WITHOUT
# its EL and RPU, then those on their own (EL SEI, EL picture, RPU, EL end-of-stream).
JOKER_TAIL = [bytes.fromhex(h) for h in (
    "00000003460150000000074e010102150780000000af0201eb1a523803c20d6f90ef7827cb9a055f80000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003002a60000000097e014e010102150780000000277e010201eb1a523c03c21ebcbbcf4000000300000300000300000947772d5c31000003000044c0000000bc7c011908090840613650af003ff801ffc00ffc001fffa000001000000d0000030080000068000004000003000040000020000020000003000400000302000003020000030000400000200000200000392b300001af512b37cfe758e12b3226500000080000030040000003004000000300e1b112180c3052f1847028a000000d31f2d7fff800000300000300000300030103ee700a8a300800410999810100000300040280000003000003000009060fa0003203e00078357edaf880",
    "00000003460150000000074e010102160380000000af0201ea9bc8e803c20d6f90ef7827cb9a055f80000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003000003002a60",
    "000000097e014e010102160380000000277e010201ea9bc8ec03c2092f5095e000000300000300000300000edb159ab86200000300008980000000bc7c011908090840613650af003ff801ffc00ffc001fffa000001000000d0000030080000068000004000003000040000020000020000003000400000302000003020000030000400000200000200000392b300001af512b37cfe758e12b3226500000080000030040000003004000000300e1b112180c3052f1847028a000000d31f2d7fff800000300000300000300030103ee700a8a300800410999810100000300040280000003000003000009060fa0003203e00078357edaf880000000047e014801",
)]


class PictureLessTail(unittest.TestCase):
    def test_reads_the_nal_types_of_a_block(self):
        self.assertEqual(dvp7.nal_types(JOKER_TAIL[0]), [35, 39, 1, 63, 63, 62])  # AUD SEI pic EL EL RPU
        self.assertEqual(dvp7.nal_types(JOKER_TAIL[1]), [35, 39, 1])              # the bare last picture
        self.assertEqual(dvp7.nal_types(JOKER_TAIL[-1]), [63, 63, 62, 63])         # EL, EL, RPU, EL

    def test_a_block_that_does_not_parse_is_none(self):
        self.assertIsNone(dvp7.nal_types(b"\x00\x00\x00\x09\x7e\x01"))       # length runs past the end
        self.assertIsNone(dvp7.nal_types(b"\x00\x00"))

    def test_joker_ends_with_one_block_and_no_picture(self):
        self.assertEqual(dvp7.dv_only_tail(JOKER_TAIL), 1)

    def test_only_the_trailing_run_counts(self):
        # the same block anywhere but the end is not the known quirk: the frame check must decide
        self.assertEqual(dvp7.dv_only_tail([JOKER_TAIL[-1], JOKER_TAIL[0]]), 0)
        self.assertEqual(dvp7.dv_only_tail(JOKER_TAIL[:2]), 0)
        self.assertEqual(dvp7.dv_only_tail([]), 0)

    def test_a_block_with_anything_but_dolby_vision_data_is_a_picture_block(self):
        eos = b"\x00\x00\x00\x02\x48\x01"                                  # a bare end-of-sequence
        self.assertEqual(dvp7.dv_only_tail([JOKER_TAIL[0], eos]), 0)

    def test_reads_ffprobes_dump_including_a_short_last_line(self):
        dump = ("\n00000000: 0000 0019 0001 e5c2 2d49 fdc6 1411 a6a0  ........-I......\n"
                "00000010: 953c cc26 64f7 4009 0b10 0012 f0         .<.&d.@......\n")
        got = dvp7.dumped_packets('{"packets": [{"data": %s}]}' % __import__("json").dumps(dump))
        self.assertEqual(got, [bytes.fromhex("00000019 0001e5c2 2d49fdc6 1411a6a0 953ccc26 64f74009 0b100012 f0"
                                             .replace(" ", ""))])

    def test_the_end_is_the_latest_timestamp_not_the_last_block(self):
        # Joker's last blocks in file order: its picture-less block repeats an EARLIER timestamp, so
        # "the last block's pts" would put the end 0.9 s early and fail every one of these films.
        pk = [{"pts_time": t} for t in ("7309.052000", "7308.927000", "7308.968000", "N/A", "7308.134000")]
        self.assertEqual(dvp7.last_pts(pk), 7309.052)
        self.assertIsNone(dvp7.last_pts([{"pts_time": "N/A"}]))
        self.assertIsNone(dvp7.last_pts([]))

    def test_a_dual_track_base_layer_is_not_read(self):
        # its EL is another track: the base layer cannot end in an EL-only block
        insp = Verify()._insp()
        insp["dual"] = True
        reads = []
        def tail(path, tid, dur, abort=None, data=False):
            reads.append((path, data))
            return [{"pts_time": "6999.958"}]
        with mock.patch.object(dvp7, "tail_packets", side_effect=tail), \
             mock.patch.object(dvp7, "dv_only_tail", side_effect=AssertionError("read")), \
             mock.patch.object(dvp7, "probe_dv", return_value={
                 "dv_profile": 8, "dv_bl_signal_compatibility_id": 1, "el_present_flag": 0}), \
             mock.patch.object(dvp7, "rpu_profile", return_value=(8, None)), \
             mock.patch.object(dvp7, "mkv_info", return_value=info(
                 [track(0, tag_number_of_frames="1000"), track(1, "audio")])), \
             mock.patch.object(dvp7, "first_pts", return_value={"0": 0.0, "1": 0.0}):
            d = tempfile.mkdtemp()
            out = os.path.join(d, "o.mkv")
            open(out, "wb").close()
            self.assertEqual(dvp7.verify("/src.mkv", out, insp, d)["frames"], 1000)
        self.assertIn(("/src.mkv", False), reads)              # its end is still checked, bytes not read


def _dump(b: bytes) -> str:
    """bytes in ffprobe's -show_data layout."""
    lines = []
    for off in range(0, len(b), 16):
        chunk = b[off:off + 16]
        hexs = " ".join(chunk[i:i + 2].hex() for i in range(0, len(chunk), 2))
        lines.append(f"{off:08x}: {hexs:<40}  " + "".join(chr(c) if 32 <= c < 127 else "." for c in chunk))
    return "\n" + "\n".join(lines) + "\n"


def _nal(t: int, payload=b"\x01\x02\x03") -> bytes:
    body = bytes([(t << 1) & 0x7E, 0x01]) + payload
    return len(body).to_bytes(4, "big") + body


class MidFilmBlocks(unittest.TestCase):
    def test_repeated_timestamps_are_found_in_one_pass(self):
        out = "0,\n41\n83,\n41\n125\n83\nN/A\n"
        with mock.patch.object(dvp7, "_run", return_value=(0, out, "")):
            self.assertEqual(dvp7.repeated_pts("/src.mkv", 0), [41, 83])

    def test_only_a_block_with_no_picture_counts(self):
        picture = _nal(35) + _nal(1) + _nal(63) + _nal(62)        # AUD, slice, EL, RPU
        dv_only = _nal(63) + _nal(63) + _nal(62) + _nal(63)       # Gladiator's shape
        def run(cmd, **kw):
            if "stream=time_base" in cmd:
                return 0, "1/1000\n", ""
            at = cmd[cmd.index("-read_intervals") + 1]
            pts = 982651 if at.startswith("979") else 4305012
            second = dv_only if pts == 982651 else picture          # the 2nd one: two pictures
            return 0, json.dumps({"packets": [{"pts": pts, "data": _dump(picture)},
                                              {"pts": pts, "data": _dump(second)},
                                              {"pts": pts + 42, "data": _dump(dv_only)}]}), ""
        with mock.patch.object(dvp7, "_run", side_effect=run):
            self.assertEqual(dvp7.dv_only_blocks("/src.mkv", 0, [982651, 4305012]), 1)


class StartOffset(unittest.TestCase):
    """Mission: Impossible (1996): its video track starts 1 ms after the audio (2026-10-01)."""

    def _mux(self, start_ms):
        d = tempfile.mkdtemp()
        insp = {"bl": {"id": 0, "properties": {}}, "el": None, "dual": False,
                "info": {"container": {"properties": {}}}, "default_duration": 41708333,
                "video_start_ms": start_ms}
        seen = []
        def run(cmd, **kw):
            if isinstance(cmd, list) and cmd[0] == dvp7.MKVMERGE:
                seen.append(cmd)
            return 0, "", ""
        with mock.patch.object(dvp7, "_run", side_effect=run):
            dvp7.build("/src.mkv", os.path.join(d, "o.mkv"), d, insp)
        return seen[0]

    def test_the_original_start_is_carried_over_to_the_new_video(self):
        cmd = self._mux(1)
        i = cmd.index("--sync")
        self.assertEqual(cmd[i + 1], "0:1")
        self.assertLess(i, cmd.index("--video-tracks"))       # an option of the NEW video's file
        self.assertTrue(cmd[i + 2].endswith("video_p81.hevc"))

    def test_a_video_that_starts_at_zero_is_left_alone(self):
        self.assertNotIn("--sync", self._mux(0))

    def test_inspect_reads_the_start(self):
        src = info([track(0), track(1, "audio")])
        def run(cmd, **kw):
            return 0, "hevc,3840\n", ""
        with mock.patch.object(dvp7, "mkv_info", return_value=src), \
             mock.patch.object(dvp7, "_run", side_effect=run), \
             mock.patch.object(dvp7, "rpu_profile", return_value=(7, "MEL")), \
             mock.patch.object(dvp7, "video_start", return_value=0.001):
            src["tracks"][0]["properties"]["default_duration"] = 41708333
            self.assertEqual(dvp7.inspect("/src.mkv", "/tmp")["video_start_ms"], 1)

    def test_the_start_is_the_first_presented_frame_not_the_first_block(self):
        # A file can open on a keyframe shown after the leading pictures decoded behind it: first
        # block 0.125, earliest frame 0.000. Carrying 125 ms over would shift the video (review,
        # reproduced with a cut open-GOP x265 stream 2026-10-01).
        with mock.patch.object(dvp7, "_run", return_value=(0, "0.125000\n0.042000\n0.000000\n0.083000\n", "")):
            self.assertEqual(dvp7.video_start("/src.mkv", 0), 0.0)
        with mock.patch.object(dvp7, "_run", return_value=(0, "0.001000\n0.334000\nN/A\n0.167000\n", "")):
            self.assertEqual(dvp7.video_start("/src.mkv", 0), 0.001)
        with mock.patch.object(dvp7, "_run", return_value=(0, "", "")):
            self.assertEqual(dvp7.video_start("/src.mkv", 0), 0.0)


class MalformedPacket(unittest.TestCase):
    """Risky Business (2026-10-03): one block's end-of-sequence NAL has a 1-byte length prefix.
    ffmpeg's hevc_mp4toannexb refuses the whole block — a picture and its RPU — prints an error and
    still exits 0, so the new file came out one frame short. mkvextract copies the block."""

    DROP = ("[vost#0:0/copy @ 0x9e5534000] Error applying bitstream filters to a packet: "
            "Invalid data found when processing input\n")

    def _build(self, *, dual=False, drop_on=None, mkvextract_rc=0, mkvextract_stop=False, d=None):
        d = d or tempfile.mkdtemp()
        insp = {"bl": {"id": 1 if dual else 0, "properties": {}},
                "el": {"id": 0, "properties": {}} if dual else None, "dual": dual,
                "info": {"container": {"properties": {}}}, "default_duration": 41708333}
        calls = []
        def write(path):
            with open(path, "wb") as fh:
                fh.write(b"layer")
        def run(cmd, **kw):
            calls.append(cmd)
            if isinstance(cmd, str):                     # a single-track pipe into dovi_tool
                write(os.path.join(d, "video_p81.hevc"))
                counts = "nalfix pictures=%d moved=0\n" if "nalfix.py" in cmd else "%s"
                if cmd.startswith(f"set -o pipefail; {dvp7.FFMPEG}"):
                    return 0, "", (self.DROP if drop_on == "pipe" else "") + (counts % 9 if "%d" in counts else "")
                return 0, "", counts % 10 if "%d" in counts else ""   # nalfix < bl.hevc | dovi_tool
            if cmd[0] == dvp7.FFMPEG:
                dst = cmd[cmd.index("-f") + 2]
                write(dst)
                return 0, "", (self.DROP if drop_on == os.path.basename(dst) else "")
            if cmd[0] == dvp7.MKVEXTRACT:
                for spec in cmd[cmd.index("tracks") + 1:]:
                    write(spec.split(":", 1)[1])
                if mkvextract_stop:
                    raise dvp7.Aborted("stopped")
                return mkvextract_rc, "", ("Error: boom" if mkvextract_rc else "")
            if cmd[0] == dvp7.DOVI:
                write(cmd[cmd.index("-o") + 1])
            return 0, "", ""
        with mock.patch.object(dvp7, "_run", side_effect=run):
            got = dvp7.build("/src.mkv", os.path.join(d, "o.mkv"), d, insp)
        self.assertEqual(os.listdir(d), [])              # every intermediate gone
        return got, calls, d

    def test_a_clean_extraction_stays_on_ffmpeg(self):
        got, calls, _d = self._build()
        self.assertEqual((got["extractor"], got["pictures"]), ("ffmpeg", 9))
        pipe = next(c for c in calls if isinstance(c, str) and c.startswith("set -o pipefail; " + dvp7.FFMPEG))
        self.assertLess(pipe.index("hevc_mp4toannexb"), pipe.index("nalfix.py"))   # the Nemesis fix
        self.assertLess(pipe.index("nalfix.py"), pipe.index(dvp7.DOVI))
        self.assertFalse(any(isinstance(c, list) and c[0] == dvp7.MKVEXTRACT for c in calls))

    def test_a_packet_ffmpeg_drops_sends_the_layer_through_mkvextract(self):
        got, calls, d = self._build(drop_on="pipe")
        self.assertEqual((got["extractor"], got["pictures"]), ("mkvextract", 10))
        mkvx = [c for c in calls if isinstance(c, list) and c[0] == dvp7.MKVEXTRACT]
        self.assertEqual(mkvx, [[dvp7.MKVEXTRACT, "-q", "/src.mkv", "tracks",
                                 "0:" + os.path.join(d, "bl.hevc")]])
        conv = [c for c in calls if isinstance(c, str) and "nalfix.py" in c and "< " in c]
        self.assertEqual(len(conv), 1)                   # the re-pulled layer goes through nalfix too
        self.assertIn(os.path.join(d, "bl.hevc"), conv[0])
        mux = [c for c in calls if isinstance(c, list) and c[0] == dvp7.MKVMERGE]
        self.assertEqual(len(mux), 1)                    # one mux, of the re-pulled video

    def test_a_dual_track_movie_pulls_both_layers_in_one_mkvextract_pass(self):
        got, calls, d = self._build(dual=True, drop_on="el.hevc")
        self.assertEqual((got["extractor"], got["pictures"]), ("mkvextract", None))
        mkvx = [c for c in calls if isinstance(c, list) and c[0] == dvp7.MKVEXTRACT]
        self.assertEqual(mkvx, [[dvp7.MKVEXTRACT, "-q", "/src.mkv", "tracks",
                                 "1:" + os.path.join(d, "bl.hevc"), "0:" + os.path.join(d, "el.hevc")]])
        steps = [c[1] if c[1] != "-m" else c[3] for c in calls
                 if isinstance(c, list) and c[0] == dvp7.DOVI]
        self.assertEqual(steps[-2:], ["extract-rpu", "inject-rpu"])

    def test_an_mkvextract_error_fails_the_build_and_leaves_nothing(self):
        for rc in (2, -9):                               # an error, and a kill from outside
            d = tempfile.mkdtemp()
            with self.assertRaisesRegex(RuntimeError, f"mkvextract {rc}: Error: boom"):
                self._build(drop_on="pipe", mkvextract_rc=rc, d=d)
            self.assertEqual(os.listdir(d), [])

    def test_a_stop_during_mkvextract_ends_the_build_and_leaves_nothing(self):
        d = tempfile.mkdtemp()
        with self.assertRaises(dvp7.Aborted):
            self._build(drop_on="pipe", mkvextract_stop=True, d=d)
        self.assertEqual(os.listdir(d), [])

    def test_convert_reports_which_tool_pulled_the_video(self):
        d = tempfile.mkdtemp()
        def build(src, o, work, insp, **kw):
            with open(o, "wb") as fh:
                fh.write(b"new")
            return {"extractor": "mkvextract", "pictures": 1, "moved": 0, "timestamps": False}
        with mock.patch.object(dvp7, "tools_missing", return_value=[]), \
             mock.patch.object(dvp7, "inspect", return_value={"el_type": "FEL", "dual": False}), \
             mock.patch.object(dvp7, "build", side_effect=build), \
             mock.patch.object(dvp7, "verify", return_value={"frames": 1, "size_out": 3}):
            self.assertEqual(dvp7.convert("/src.mkv", os.path.join(d, "p81.mkv"), d)["extractor"],
                             "mkvextract")

    def test_convert_hands_the_picture_count_to_verify_and_reports_the_fixes(self):
        d = tempfile.mkdtemp()
        def build(src, o, work, insp, **kw):
            with open(o, "wb") as fh:
                fh.write(b"new")
            return {"extractor": "ffmpeg", "pictures": 167546, "moved": 9325, "timestamps": True}
        with mock.patch.object(dvp7, "tools_missing", return_value=[]), \
             mock.patch.object(dvp7, "inspect", return_value={"el_type": "FEL", "dual": False}), \
             mock.patch.object(dvp7, "build", side_effect=build), \
             mock.patch.object(dvp7, "verify", return_value={"frames": 167546, "size_out": 3}) as ver:
            res = dvp7.convert("/src.mkv", os.path.join(d, "p81.mkv"), d)
        self.assertEqual(ver.call_args.kwargs["pictures"], 167546)
        self.assertEqual((res["moved"], res["timestamps"], res["frames"]), (9325, True, 167546))

    def test_mkvextract_is_a_required_tool_installed_with_mkvmerge(self):
        with mock.patch.object(dvp7, "tools_missing", return_value=["mkvmerge", "mkvextract"]):
            with self.assertRaisesRegex(RuntimeError, r"\(brew install mkvtoolnix\)$"):
                dvp7.convert("/src.mkv", "/out.mkv", tempfile.mkdtemp())
        with mock.patch.object(dvp7.os.path, "exists", side_effect=lambda p: p != dvp7.MKVEXTRACT):
            self.assertEqual(dvp7.tools_missing(), ["mkvextract"])


class TruncatedOriginal(unittest.TestCase):
    """The Boy and the Heron (2026-10-04): the NAS file stops at 1:55:13 of its header's 2:03:57."""

    def _inspect(self, out, err):
        src = info([track(0), track(1, "audio")])
        src["container"]["properties"]["duration"] = 7436960000000
        def run(cmd, **kw):
            if "-read_intervals" in cmd and "packet=pts_time" in cmd:
                return 0, out, err
            return 0, "hevc,3840\n", ""
        with mock.patch.object(dvp7, "mkv_info", return_value=src), \
             mock.patch.object(dvp7, "_run", side_effect=run), \
             mock.patch.object(dvp7, "rpu_profile", return_value=(7, "FEL")), \
             mock.patch.object(dvp7, "video_start", return_value=0.0):
            src["tracks"][0]["properties"]["default_duration"] = 41708333
            return dvp7.inspect("/src.mkv", "/tmp")

    def test_a_file_cut_short_is_refused_before_any_work_and_says_why(self):
        with self.assertRaisesRegex(RuntimeError, r"incomplete: its video stops at 1:55:13 of the "
                                                  r"2:03:56 .*replace it with a complete copy"):
            self._inspect("6913.115000\n", "[matroska,webm @ 0x1] File ended prematurely\n")

    def test_a_whole_file_goes_on(self):
        self.assertEqual(self._inspect("7436.875000,\n7436.917000\n", "")["el_type"], "FEL")

    def test_a_video_well_short_of_the_header_without_the_warning_is_not_a_cut(self):
        # audio or subtitles running long make the header's duration exceed the video's
        self.assertEqual(self._inspect("7430.000000\n", "")["el_type"], "FEL")

    def test_audio_running_a_little_past_the_video_is_not_a_cut(self):
        # the demuxer's warning alone is not enough: the video must stop well short of the header
        self.assertEqual(self._inspect("7436.400000\n", "File ended prematurely\n")["el_type"], "FEL")


class FrameTimes(unittest.TestCase):
    def test_picture_times_drop_the_blocks_with_no_picture(self):
        # Nemesis: a split block on a picture's timestamp, another 1 ms after one; Gladiator: on one
        self.assertEqual(dvp7.picture_times([0, 125, 42, 83, 42, 250, 167, 793, 792, 209], 41.708),
                         [0, 42, 83, 125, 167, 209, 250, 792])

    def test_even_is_the_default_durations_cadence_to_the_millisecond(self):
        frames = [round(k * 1001 / 24) for k in range(500)]
        self.assertTrue(dvp7.even(frames, 1001 / 24))
        self.assertTrue(dvp7.even([t + 1 for t in frames], 1001 / 24))     # a later start: --sync
        gap = frames[:300] + [t + 27 for t in frames[300:]]               # Mandalorian: +27 ms
        self.assertFalse(dvp7.even(gap, 1001 / 24))

    def test_ms_text_is_exact(self):
        self.assertEqual([dvp7.ms_text(t) for t in (7925459.0, 41.70833, 0.0, 33.5)],
                         ["7925459", "41.708", "0", "33.5"])

    def test_hms(self):
        self.assertEqual((dvp7.hms(6913.115), dvp7.hms(59.9), dvp7.hms(7436.96)),
                         ("1:55:13", "0:00:59", "2:03:56"))


class CarryTimestamps(unittest.TestCase):
    """The Mandalorian and Grogu (2026-10-04): three gaps in the original's frame times (2, 12 and
    27 ms over the 42) — with a default duration the new file ended one frame early."""

    def _build(self, pts, pictures, dual=False, tb="1/1000"):
        d = tempfile.mkdtemp()
        insp = {"bl": {"id": 1 if dual else 0, "properties": {}},
                "el": {"id": 0, "properties": {}} if dual else None, "dual": dual,
                "info": {"container": {"properties": {}}}, "default_duration": 41708333,
                "video_start_ms": 5}
        seen = {}
        def write(path):
            with open(path, "wb") as fh:
                fh.write(b"v")
        def run(cmd, **kw):
            if isinstance(cmd, str):
                write(os.path.join(d, "video_p81.hevc"))
                return 0, "", f"nalfix pictures={pictures} moved=0\n"
            if cmd[0] == dvp7.FFMPEG:                    # a dual track's layer extraction
                write(cmd[cmd.index("-f") + 2])
                return 0, "", ""
            if cmd[0] == dvp7.DOVI:
                write(cmd[cmd.index("-o") + 1])
                return 0, "", ""
            if cmd[0] == dvp7.FFPROBE and "stream=time_base" in cmd:
                return 0, tb + "\n", ""
            if cmd[0] == dvp7.FFPROBE and "packet=pts" in cmd:
                return 0, "".join(f"{t},\n" for t in pts), ""
            if cmd[0] == dvp7.MKVMERGE:
                seen["cmd"] = cmd
                if "--timestamps" in cmd:
                    with open(cmd[cmd.index("--timestamps") + 1].split(":", 1)[1]) as fh:
                        seen["file"] = fh.read()
            return 0, "", ""
        with mock.patch.object(dvp7, "_run", side_effect=run):
            got = dvp7.build("/src.mkv", os.path.join(d, "o.mkv"), d, insp)
        self.assertEqual(os.listdir(d), [])
        return got, seen

    def test_uneven_frame_times_are_carried_over(self):
        even = [5 + round(k * 1001 / 24) for k in range(400)]
        pts = even[:100] + [t + 12 for t in even[100:300]] + [t + 39 for t in even[300:]]
        got, seen = self._build(pts, pictures=400)
        self.assertTrue(got["timestamps"])
        self.assertNotIn("--sync", seen["cmd"])                      # the start comes with them
        lines = seen["file"].splitlines()
        self.assertEqual(lines[0], "# timestamp format v2")
        self.assertEqual([int(x) for x in lines[1:]], sorted(pts))
        i = seen["cmd"].index("--timestamps")
        self.assertLess(i, seen["cmd"].index("--video-tracks"))     # an option of the NEW video

    def test_a_dual_track_movie_carries_uneven_times_too(self):
        even = [5 + round(k * 1001 / 24) for k in range(400)]
        pts = even[:200] + [t + 27 for t in even[200:]]
        got, seen = self._build(pts, pictures=None, dual=True)
        self.assertTrue(got["timestamps"])
        self.assertIn("--timestamps", seen["cmd"])

    def test_a_dual_track_with_a_block_that_is_not_a_picture_is_not_trusted(self):
        even = [5 + round(k * 1001 / 24) for k in range(400)]
        pts = even[:200] + [even[199]] + [t + 27 for t in even[200:]]     # a repeat in a dual BL?
        got, seen = self._build(pts, pictures=None, dual=True)
        self.assertFalse(got["timestamps"])

    def test_a_finer_time_base_is_written_in_milliseconds(self):
        even = [5 + round(k * 1001 / 24) for k in range(400)]
        ms = even[:300] + [t + 27 for t in even[300:]]
        got, seen = self._build([t * 10 for t in ms], pictures=400, tb="1/10000")
        self.assertTrue(got["timestamps"])
        self.assertEqual([int(x) for x in seen["file"].splitlines()[1:]], ms)

    def test_even_frame_times_keep_the_default_duration(self):
        got, seen = self._build([5 + round(k * 1001 / 24) for k in range(400)], pictures=400)
        self.assertFalse(got["timestamps"])
        self.assertNotIn("--timestamps", seen["cmd"])
        self.assertIn("--sync", seen["cmd"])

    def test_times_that_do_not_match_the_picture_count_are_not_trusted(self):
        even = [5 + round(k * 1001 / 24) for k in range(400)]
        pts = even[:300] + [t + 27 for t in even[300:]]
        got, seen = self._build(pts, pictures=401)                  # one picture unaccounted for
        self.assertFalse(got["timestamps"])                         # verify judges the result
        self.assertNotIn("--timestamps", seen["cmd"])


class PictureCount(unittest.TestCase):
    """Star Trek: Nemesis (2026-10-04): 176871 blocks, 167546 pictures — 9325 blocks of Dolby Vision
    data split off the picture before them, most on a timestamp 1 ms from a picture's."""

    def test_the_counted_pictures_settle_the_frame_check(self):
        v = Verify()
        v.setUp()
        try:
            res = v._run(out_frames="167546", recount=(176871, 167546),
                         pictures=167546)
        finally:
            v.tearDown()
        self.assertEqual(res["frames"], 167546)

    def test_a_lost_picture_still_fails(self):
        v = Verify()
        v.setUp()
        try:
            with self.assertRaisesRegex(RuntimeError, "frame count 167545"):
                v._run(out_frames="167545", recount=(176871, 167545),
                       pictures=167546)
        finally:
            v.tearDown()


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
