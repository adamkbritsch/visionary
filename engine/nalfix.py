"""A pass over a raw (Annex B) HEVC base layer, between the extraction and dovi_tool, for the profile
7 lane (engine/dvp7.py). It changes nothing in a normal stream: every byte goes through in order.

It mends one release quirk, proven on Star Trek: Nemesis (2026-10-04): the muxer split the LAST
picture of every group of pictures in two. The picture's block holds only its base-layer slices, and
its enhancement layer and RPU sit in a block of their own after it, opened by a copy of the next
keyframe's parameter sets (VPS/SPS/PPS) and given a stray timestamp. A stream's access units start at
parameter sets, so that block reads as a picture-less frame of its own: mkvmerge muxed 9325 of them
as frames and the movie came out 6.5 minutes long, every frame after the first one late. Moving the
Dolby Vision NALs in front of those parameter sets hands them back to the picture they belong to
(dovi_tool then discards the enhancement layer and converts the RPU as for any other frame), and the
parameter sets stay where they were, repeated just before the keyframe's own. No byte is dropped.

It also counts the pictures it passes (slices that start a picture), which is the frame count the
new file must have, whatever blocks without a picture the original carries.

As a filter: stdin -> stdout, then one line on stderr: "nalfix pictures=N moved=M".
"""
import os
import sys

PARAM_SETS = {32, 33, 34}      # VPS, SPS, PPS
DV_NALS = {62, 63}             # the RPU and enhancement-layer NALs
CHUNK = 64 * 1024 * 1024
SC = b"\x00\x00\x01"


class Fixer:
    """Feed it the stream in pieces; it writes through `write` as soon as bytes are settled."""

    def __init__(self, write):
        self.write = write
        self.carry = b""        # from the last start code seen: a NAL not yet known to be whole
        self.group = []         # NAL segments held back: parameter sets, then DV NALs
        self.types = []         # their NAL types
        self.dv_at = None       # index in `group` where its DV NALs begin
        self.bare = False       # the last picture so far has no RPU of its own
        self.pictures = 0
        self.moved = 0

    def _flush_group(self):
        for g in self.group:
            self.write(g)
        self.group, self.types, self.dv_at = [], [], None

    def feed(self, data: bytes, last=False):
        buf = self.carry + data if self.carry else data
        mv = memoryview(buf)
        starts, heads = [], []                       # each NAL's first byte, and its header's
        p = buf.find(SC)
        while p != -1:
            starts.append(p - 1 if p > 0 and buf[p - 1] == 0 else p)
            heads.append(p + 3)
            p = buf.find(SC, p + 3)
        if not starts:
            if last:
                self._flush_group()
                self.write(buf)
                self.carry = b""
            else:
                self.carry = bytes(buf)
            return
        ends = starts[1:] + ([len(buf)] if last else [])
        run = None if self.group else 0              # start of the pass-through span not yet written
        if starts[0] > 0 and self.group:             # (only the stream's first bytes come before a
            self.group[-1] += bytes(mv[:starts[0]])  #  start code; a carry always opens with one)
        n = len(buf)
        for a, b, h in zip(starts, ends, heads):
            t = (buf[h] >> 1) & 0x3F if h < n else None
            held = t in PARAM_SETS or (t in DV_NALS and self.group)
            if not held:
                if self.group:                           # anything else closes a held group as is
                    self._flush_group()
                    run = a
                if t is not None and t < 32:
                    if h + 2 < n and buf[h + 2] & 0x80:  # first_slice_segment_in_pic_flag
                        self.pictures += 1
                        self.bare = True
                elif t == 62:
                    self.bare = False
                continue
            if run is not None:
                if a > run:
                    self.write(mv[run:a])
                run = None
            if t in PARAM_SETS and self.dv_at is not None:
                # Parameter sets, then nothing but DV NALs ending in an RPU, then parameter sets
                # again — a block with no picture. When the picture before it has no RPU, the DV
                # NALs are that picture's: they go first, and the parameter sets follow them.
                if self.bare and self.types[-1] == 62:
                    for g in self.group[self.dv_at:] + self.group[:self.dv_at]:
                        self.write(g)
                    self.moved += 1
                    self.bare = False
                    self.group, self.types, self.dv_at = [], [], None
                else:
                    self._flush_group()
            if t in DV_NALS and self.dv_at is None:
                self.dv_at = len(self.group)
            self.group.append(bytes(mv[a:b]))
            self.types.append(t)
        tail = ends[-1] if ends else starts[0]
        if run is not None and tail > run:
            self.write(mv[run:tail])
        if last:
            self._flush_group()
            self.carry = b""
        else:
            self.carry = bytes(mv[starts[-1]:])          # the last NAL may continue in the next piece


def run(src, dst) -> Fixer:
    f = Fixer(dst.write)
    prev = src.read(CHUNK)
    while prev:
        nxt = src.read(CHUNK)
        f.feed(prev, last=not nxt)
        prev = nxt
    dst.flush()
    return f


if __name__ == "__main__":
    try:
        fx = run(sys.stdin.buffer, sys.stdout.buffer)
    except BrokenPipeError:
        # dovi_tool stopped reading — its own error says why; a traceback here would push that
        # out of the tail the lane logs (review 2026-10-04)
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.stderr.write("nalfix: the next step closed the pipe\n")
        sys.exit(1)
    sys.stderr.write(f"nalfix pictures={fx.pictures} moved={fx.moved}\n")
