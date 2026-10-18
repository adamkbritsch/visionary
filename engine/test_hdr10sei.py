import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

import dvcap
import hdr10sei as H

FALLBACK = dvcap.FALLBACK_MASTER_DISPLAY
START = b"\x00\x00\x01"


def nal(t: int, body: bytes = b"\x11\x22\x33", zero_byte=False) -> bytes:
    """An Annex B NAL: start code (4-byte with zero_byte), header (layer 0, tid 1), body."""
    return (b"\x00" if zero_byte else b"") + START + bytes([t << 1, 1]) + body


def slice_(t: int, first=True) -> bytes:
    return nal(t, (b"\x80" if first else b"\x40") + b"\xaa\xbb\xcc")


def au(irap: bool, extra=b"") -> bytes:
    """One access unit as an encoder lays it out: AUD, (parameter sets on a keyframe), SEI 5,
    then the slice."""
    out = nal(35, b"\x50", zero_byte=True)
    if irap:
        out += nal(32, zero_byte=True) + nal(33, zero_byte=True) + nal(34, zero_byte=True)
    out += extra + nal(39, b"\x05\x02\xab\xcd\x80")
    return out + slice_(19 if irap else 1)


def stream(pattern="KPPKPPKP", extra=b"") -> bytes:
    return b"".join(au(c == "K", extra if c == "K" else b"") for c in pattern)


def split_nals(data: bytes) -> list:
    """[(type, first payload byte, body)] of every NAL, in order."""
    out, i = [], data.find(START)
    while i != -1:
        j = data.find(START, i + 3)
        body = data[i + 3:j if j != -1 else len(data)]
        out.append(((body[0] >> 1) & 0x3F, body[2] if len(body) > 2 else None, body))
        i = j
    return out


class Payloads(unittest.TestCase):
    def test_mdcv_payload_is_x265s_order_and_units(self):
        p = H.mdcv_payload(FALLBACK)
        self.assertEqual(len(p), 24)
        self.assertEqual(p[:4].hex(), "33c286c4")                 # G(13250,34500), as x265 writes it
        self.assertEqual(int.from_bytes(p[16:20], "big"), 10000000)
        self.assertEqual(int.from_bytes(p[20:24], "big"), 1)
        for bad in ("", "L(1,1)", "G(1,1)B(1,1)R(1,1)WP(1,1)L(1,1)x", "G(70000,1)B(1,1)R(1,1)WP(1,1)L(1,1)"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                H.mdcv_payload(bad)

    def test_cll_payload_and_parse(self):
        self.assertEqual(H.cll_payload(1000, 400).hex(), "03e80190")
        self.assertEqual(H.parse_cll("981,553"), (981, 553))
        self.assertEqual(H.parse_cll(None), (0, 0))
        with self.assertRaises(ValueError):
            H.cll_payload(70000, 1)

    def test_emulation_prevention(self):
        self.assertEqual(H.emulation_prevent(b"\x00\x00\x01"), b"\x00\x00\x03\x01")
        self.assertEqual(H.emulation_prevent(b"\x00\x00\x00\x01"), b"\x00\x00\x03\x00\x01")
        self.assertEqual(H.emulation_prevent(b"\x00\x00\x04"), b"\x00\x00\x04")
        # the fallback display's min luminance (00 00 00 01) needs one: no start code inside a NAL
        sei = H.sei_nal(H.MDCV, H.mdcv_payload(FALLBACK))
        self.assertEqual(sei.count(START), 1)
        self.assertTrue(sei.startswith(START + b"\x4e\x01\x89\x18"))   # x265's own header bytes
        self.assertTrue(sei.endswith(b"\x80"))

    def test_the_pair_is_cll_then_mdcv(self):
        types = [(b[0] >> 1 & 0x3F, b[2]) for _t, _f, b in split_nals(H.hdr10_nals(FALLBACK, 1000, 400))]
        self.assertEqual(types, [(39, H.CLL), (39, H.MDCV)])


class Insert(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def run_on(self, data: bytes, block=H.BLOCK, cll=(981, 553)) -> bytes:
        src, dst = os.path.join(self.d, "in.hevc"), os.path.join(self.d, "out.hevc")
        with open(src, "wb") as f:
            f.write(data)
        self.n = H.insert(src, dst, FALLBACK, *cll, block=block)
        with open(dst, "rb") as f:
            return f.read()

    def test_every_keyframe_gets_the_pair_right_before_its_first_slice(self):
        out = self.run_on(stream())
        self.assertEqual(self.n, 3)
        nals = split_nals(out)
        seq = [(t, f) for t, f, _b in nals]
        for k, (t, f) in enumerate(seq):
            if t == 19:                                       # an IDR slice
                self.assertEqual(seq[k - 2:k], [(39, H.CLL), (39, H.MDCV)])
        self.assertEqual(sum(1 for t, f in seq if t == 39 and f in (H.CLL, H.MDCV)), 6)
        # nothing else moved: drop what was added and the input comes back byte for byte
        added = H.hdr10_nals(FALLBACK, 981, 553)
        self.assertEqual(out.replace(added, b""), stream())

    def test_a_trailing_slice_or_a_second_slice_gets_nothing(self):
        data = au(True) + slice_(19, first=False) + au(False)
        out = self.run_on(data)
        self.assertEqual(self.n, 1)
        self.assertEqual(out.count(H.hdr10_nals(FALLBACK, 981, 553)), 1)

    def test_cra_and_bla_are_keyframes_too(self):
        data = nal(32) + nal(39, b"\x05\x01\x00\x80") + slice_(21) + slice_(1) + slice_(16)
        self.run_on(data)
        self.assertEqual(self.n, 2)

    def test_any_block_size_gives_the_same_bytes(self):
        data = stream("KPKPPPKPPKP")
        want = self.run_on(data)
        for block in (1, 2, 3, 5, 7, 13, 64, 1000):
            with self.subTest(block=block):
                self.assertEqual(self.run_on(data, block=block), want)

    def test_existing_mdcv_and_cll_are_replaced_and_a_rerun_changes_nothing(self):
        old = H.hdr10_nals("G(1,2)B(3,4)R(5,6)WP(7,8)L(5000000,50)", 4000, 100)
        data = stream("KPKP", extra=old)
        out = self.run_on(data, cll=(500, 100))
        self.assertNotIn(old, out)
        self.assertEqual(out.count(H.hdr10_nals(FALLBACK, 500, 100)), 2)
        again = self.run_on(out, cll=(500, 100))
        self.assertEqual(again, out)                          # idempotent
        for block in (3, 11, 50):                            # ...also when an SEI straddles a read
            self.assertEqual(self.run_on(data, block=block, cll=(500, 100)), out)

    def test_a_stream_with_no_keyframe_is_refused(self):
        with self.assertRaises(ValueError):
            self.run_on(au(False) + au(False))

    def test_the_child_entry_point(self):
        src, dst = os.path.join(self.d, "in.hevc"), os.path.join(self.d, "out.hevc")
        with open(src, "wb") as f:
            f.write(stream("KP"))
        r = subprocess.run([sys.executable, "-m", "hdr10sei", src, dst, "--mdcv", FALLBACK, "--cll", "981,553"],
                           cwd=os.path.dirname(os.path.abspath(H.__file__)), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("tagged 1 keyframes", r.stdout)
        bad = subprocess.run([sys.executable, "-m", "hdr10sei", src, dst, "--mdcv", "nope"],
                             cwd=os.path.dirname(os.path.abspath(H.__file__)), capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertFalse(os.path.exists(dst))                 # never a half-written output


@unittest.skipUnless(os.path.exists(dvcap.FFPROBE) and os.path.exists("/opt/homebrew/bin/ffmpeg"),
                     "ffmpeg/ffprobe not installed")
class ReadBackByFfmpeg(unittest.TestCase):
    """REAL ffmpeg: a VideoToolbox HEVC stream gets values that ffprobe reads back the way
    dvcap.probe_video does, and keeps its frame count (skips without VideoToolbox)."""

    def test_probe_video_reads_what_was_written(self):
        d = tempfile.mkdtemp()
        try:
            ff = "/opt/homebrew/bin/ffmpeg"
            raw = os.path.join(d, "raw.hevc")
            r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                                "testsrc2=s=320x180:r=24", "-frames:v", "36", "-g", "12",
                                "-pix_fmt", "p010le", "-c:v", "hevc_videotoolbox", "-profile:v", "main10",
                                "-f", "hevc", raw], capture_output=True)
            if r.returncode != 0:
                self.skipTest("hevc_videotoolbox unavailable")
            out = os.path.join(d, "hdr.hevc")
            n = H.insert(raw, out, FALLBACK, 981, 553)
            self.assertGreaterEqual(n, 3)
            info = dvcap.probe_video(out)
            self.assertEqual((info["master_display"], info["max_cll"]), (FALLBACK, "981,553"))
            self.assertEqual(dvcap.count_hevc_frames(out), dvcap.count_hevc_frames(raw))
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
