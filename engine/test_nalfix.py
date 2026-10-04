"""engine/nalfix.py — the base-layer pass between extraction and dovi_tool in the profile 7 lane."""
import io
import unittest

import nalfix


def nal(t, first=True, body=b"\x11\x22\x33", sc4=True):
    """One Annex B NAL: start code, 2-byte header, payload. For a slice (t < 32), `first` sets
    first_slice_segment_in_pic_flag in the payload's first bit."""
    lead = b"\x00\x00\x00\x01" if sc4 else b"\x00\x00\x01"
    payload = bytes([(0x80 if first else 0x00) | 0x05]) + body if t < 32 else body
    return lead + bytes([(t << 1) & 0x7E, 0x01]) + payload


PS = nal(32) + nal(33) + nal(34)
EL = nal(63, body=b"\x0a\x0b") + nal(63, body=b"\x0c\x0d")
RPU = nal(62, body=b"\x7c\x01\x99")


def pic(t=1, slices=3, rpu=True, el=True):
    return (nal(35) + nal(39) + b"".join(nal(t, first=(i == 0)) for i in range(slices))
            + (EL if el else b"") + (RPU if rpu else b""))


def run(data, chunk=None):
    """Through the filter in pieces of `chunk` bytes (all at once when None)."""
    out = io.BytesIO()
    f = nalfix.Fixer(out.write)
    pieces = [data] if not chunk else [data[i:i + chunk] for i in range(0, len(data), chunk)]
    for i, piece in enumerate(pieces):
        f.feed(piece, last=(i == len(pieces) - 1))
    return out.getvalue(), f


class Filter(unittest.TestCase):
    def test_a_normal_stream_goes_through_byte_for_byte(self):
        data = PS + pic(19) + pic() + pic() + PS + pic(21) + pic(0, rpu=True)
        for chunk in (None, 1, 2, 3, 5, 7, 64):
            out, f = run(data, chunk)
            self.assertEqual(out, data, chunk)
            self.assertEqual((f.pictures, f.moved), (5, 0), chunk)

    def test_nemesis_split_block_hands_the_rpu_back_to_its_picture(self):
        # the last picture of the group without its EL and RPU, then a block of the next keyframe's
        # parameter sets + that EL and RPU, then the keyframe
        bare = pic(0, rpu=False, el=False)
        split = PS + EL + RPU
        data = PS + pic(19) + pic() + bare + split + PS + pic(21)
        want = PS + pic(19) + pic() + bare + EL + RPU + PS + PS + pic(21)
        for chunk in (None, 1, 2, 3, 4, 6, 11, 50):
            out, f = run(data, chunk)
            self.assertEqual(out, want, chunk)
            self.assertEqual((f.pictures, f.moved), (4, 1), chunk)
            self.assertEqual(len(out), len(data))       # reordered, nothing dropped

    def test_three_and_four_byte_start_codes_both_parse(self):
        bare = nal(35, sc4=False) + nal(1, sc4=False) + nal(1, first=False, sc4=False)
        split = nal(32, sc4=False) + nal(33) + nal(34, sc4=False) + nal(63, sc4=False) + nal(62)
        data = PS + pic(19) + bare + split + PS + pic(21)
        out, f = run(data, 3)
        self.assertEqual(f.moved, 1)
        self.assertEqual(out, PS + pic(19) + bare + nal(63, sc4=False) + nal(62)
                         + nal(32, sc4=False) + nal(33) + nal(34, sc4=False) + PS + pic(21))

    def test_a_picture_that_has_its_rpu_keeps_the_block_where_it_is(self):
        data = PS + pic(19) + pic() + PS + EL + RPU + PS + pic(21)
        out, f = run(data, 5)
        self.assertEqual((out, f.moved), (data, 0))

    def test_a_block_that_does_not_end_in_an_rpu_is_left_alone(self):
        data = PS + pic(19) + pic(0, rpu=False, el=False) + PS + EL + PS + pic(21)
        out, f = run(data, 5)
        self.assertEqual((out, f.moved), (data, 0))

    def test_parameter_sets_then_dv_then_a_slice_is_an_ordinary_access_unit(self):
        # EL parameter sets right after the BL's, before the BL slices: no second VPS, no move
        data = PS + pic(19) + pic(0, rpu=False, el=False) + PS + EL + nal(39) + nal(21) + RPU
        out, f = run(data, 4)
        self.assertEqual((out, f.moved), (data, 0))

    def test_a_held_block_at_the_very_end_goes_out_as_it_was(self):
        data = PS + pic(19) + pic(0, rpu=False, el=False) + PS + EL + RPU
        for chunk in (None, 1, 3, 8):
            out, f = run(data, chunk)
            self.assertEqual((out, f.moved), (data, 0), chunk)

    def test_one_bare_picture_takes_one_block(self):
        bare = pic(0, rpu=False, el=False)
        data = PS + pic(19) + bare + PS + EL + RPU + PS + EL + RPU + PS + pic(21)
        out, f = run(data, 4)
        self.assertEqual(f.moved, 1)                     # the second block finds no bare picture
        self.assertEqual(out, PS + pic(19) + bare + EL + RPU + PS + PS + EL + RPU + PS + pic(21))

    def test_a_closed_pipe_ends_quietly(self):
        import subprocess
        import sys
        data = (PS + pic(19)) * 200000                   # far more than the reader takes
        p = subprocess.Popen(f"{sys.executable} {nalfix.__file__} | head -c 10 > /dev/null",
                             shell=True, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        _o, err = p.communicate(data)
        self.assertNotIn(b"Traceback", err)
        self.assertIn(b"closed the pipe", err)

    def test_running_it_twice_changes_nothing_more(self):
        data = PS + pic(19) + pic(0, rpu=False, el=False) + PS + EL + RPU + PS + pic(21)
        once, f1 = run(data, 7)
        twice, f2 = run(once, 7)
        self.assertEqual((f1.moved, f2.moved), (1, 0))
        self.assertEqual(twice, once)

    def test_the_command_line_reports_its_counts(self):
        import subprocess
        import sys
        data = PS + pic(19) + pic(0, rpu=False, el=False) + PS + EL + RPU + PS + pic(21)
        r = subprocess.run([sys.executable, nalfix.__file__], input=data, capture_output=True)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr.decode().strip(), "nalfix pictures=3 moved=1")
        self.assertEqual(len(r.stdout), len(data))


if __name__ == "__main__":
    unittest.main()
