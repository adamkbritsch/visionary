"""HDR10 static metadata for a VideoToolbox HEVC stream: the mastering-display (SEI payload 137)
and content-light-level (payload 144) messages, written into the Annex B stream itself.

WHY (2026-10-15, review-caught): every master the x265 path makes carries both, in-band, in
EVERY keyframe's access unit (`--master-display` / `--max-cll` with `--repeat-headers`: AUD,
VPS, SPS, PPS, then SEI 144 and SEI 137, each its own prefix-SEI NAL), and HDR10 players
tone-map from them. hevc_videotoolbox writes neither (measured on ffmpeg 8.0: its raw HEVC
carries only SEI 5), so the native render ('dvrender', nativerender.py) shipped without them:
an HDR10-fallback player lost MaxCLL/MaxFALL and the mastering display, and an x265 remux of the
render read none back (dvcap.probe_video) and wrote CLL 0,0. This module puts the same two
messages, the same way, into the render's ES before the RPU is injected; MP4Box keeps them in
the samples AND writes the matching mdcv/clli boxes, and dvcap.probe_video reads them back
(measured 2026-10-15: VT encode -> this -> dovi_tool inject-rpu -> MP4Box `:dvp=8.1`).

HOW: one streaming pass, constant memory. Before the FIRST slice of every IRAP picture (NAL
types 16-23 with first_slice_segment_in_pic_flag set) it writes a prefix-SEI NAL with the CLL
message and one with the MDCV message — after whatever parameter sets and SEIs the encoder put
there, before any VCL NAL, which is where HEVC wants prefix SEI. Any MDCV or CLL SEI the stream
already carries is dropped first, so a re-run (or a future encoder that writes its own) never
leaves two that disagree: the output always says exactly what it was asked to.

Run as a child (`python3 -m hdr10sei SRC DST --mdcv ... --cll A,B`) so the pass is niced,
killable and CPU-accounted like every other render step (dvnative.run_step). No engine imports.
"""
from __future__ import annotations
import argparse
import os
import re
import sys

MDCV, CLL = 137, 144                  # SEI payload types (H.265 D.2.28 / D.2.35)
SEI_PREFIX = 39                       # PREFIX_SEI_NUT
IRAP = range(16, 24)                  # BLA_W_LP .. RSV_IRAP_VCL23
VCL = range(0, 32)
BLOCK = 16 * 1024 * 1024              # read size: the pass is I/O; this keeps memory flat
_MD_RE = re.compile(r"^G\((\d+),(\d+)\)B\((\d+),(\d+)\)R\((\d+),(\d+)\)WP\((\d+),(\d+)\)"
                    r"L\((\d+),(\d+)\)$")
_START = b"\x00\x00\x01"


def mdcv_payload(master_display: str) -> bytes:
    """PURE. An x265 --master-display string (G(x,y)B(x,y)R(x,y)WP(x,y)L(max,min): chromaticities
    in 0.00002, luminance in 0.0001 nit) -> the 24-byte MDCV payload, in x265's own order (G, B,
    R, white point, then max and min luminance)."""
    m = _MD_RE.match(master_display or "")
    if not m:
        raise ValueError(f"not an x265 master-display string: {master_display!r}")
    v = [int(x) for x in m.groups()]
    if any(x > 0xFFFF for x in v[:8]) or any(x > 0xFFFFFFFF for x in v[8:]):
        raise ValueError(f"master-display value out of range: {master_display!r}")
    return b"".join(x.to_bytes(2, "big") for x in v[:8]) + v[8].to_bytes(4, "big") + v[9].to_bytes(4, "big")


def cll_payload(max_cll: int, max_fall: int) -> bytes:
    """PURE. MaxCLL, MaxFALL (nits) -> the 4-byte CLL payload. 0,0 means "unknown" — what x265
    writes when it is given a mastering display and no --max-cll."""
    cll, fall = int(max_cll), int(max_fall)
    if not (0 <= cll <= 0xFFFF and 0 <= fall <= 0xFFFF):
        raise ValueError(f"light levels out of range: {max_cll},{max_fall}")
    return cll.to_bytes(2, "big") + fall.to_bytes(2, "big")


def parse_cll(text) -> tuple:
    """PURE. "a,b" (dvnative.x265_max_cll's form) -> (a, b); None or "" -> (0, 0)."""
    if not text:
        return (0, 0)
    a, b = (int(x) for x in str(text).split(","))
    return (a, b)


def emulation_prevent(rbsp: bytes) -> bytes:
    """PURE. RBSP -> NAL payload: a 0x03 after every 00 00 that a byte <= 03 would follow."""
    out, zeros = bytearray(), 0
    for b in rbsp:
        if zeros >= 2 and b <= 3:
            out.append(3)
            zeros = 0
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def sei_nal(payload_type: int, payload: bytes) -> bytes:
    """PURE. One prefix-SEI NAL (start code included) carrying ONE message — x265's layout:
    header 0x4E01 (type 39, layer 0, temporal id 1), type, size, payload, rbsp stop bit."""
    if payload_type > 254 or len(payload) > 254:
        raise ValueError("this writer handles one-byte payload types and sizes only")
    body = bytes([payload_type, len(payload)]) + payload + b"\x80"
    return _START + bytes([SEI_PREFIX << 1, 1]) + emulation_prevent(body)


def hdr10_nals(master_display: str, max_cll: int, max_fall: int) -> bytes:
    """PURE. The two NALs every keyframe gets, in x265's order: CLL, then MDCV."""
    return sei_nal(CLL, cll_payload(max_cll, max_fall)) + sei_nal(MDCV, mdcv_payload(master_display))


def insert(src: str, dst: str, master_display: str, max_cll: int = 0, max_fall: int = 0,
           block: int = BLOCK) -> int:
    """Stream `src` (Annex B HEVC) to `dst` with the HDR10 SEI pair before every IRAP picture's
    first slice, dropping any prefix-SEI NAL whose first message is MDCV or CLL. -> the number
    of keyframes tagged. Raises ValueError when the stream has no IRAP picture at all (nothing
    would carry the metadata).

    The buffer holds the undecided bytes: `w` is where the output has got to (everything before
    it is written or dropped), `s` where the start-code scan resumes. A NAL is looked at only
    once its header and first payload byte are in the buffer, and an SEI is dropped only once
    the start code after it is too; otherwise the loop reads on and decides then. A 4-byte start
    code's leading zero stays with the bytes before it, so dropping or inserting a NAL never
    takes the zero_byte from the NAL that follows."""
    sei = hdr10_nals(master_display, max_cll, max_fall)
    tagged = 0
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        buf, eof = b"", False
        while not eof:
            chunk = fi.read(block)
            eof = not chunk
            buf += chunk
            w = s = 0
            safe = None
            while safe is None:
                i = buf.find(_START, s)
                if i == -1:                       # keep a possibly split start code's bytes
                    safe = len(buf) if eof else max(w, len(buf) - 3)
                    break
                if i + 6 > len(buf):              # header + first payload byte not all here
                    safe = len(buf) if eof else max(w, i)
                    break
                t = (buf[i + 3] >> 1) & 0x3F
                if t in IRAP and buf[i + 5] & 0x80:           # first_slice_segment_in_pic_flag
                    fo.write(buf[w:i])
                    fo.write(sei)
                    w, s = i, i + 3
                    tagged += 1
                    continue
                if t == SEI_PREFIX and buf[i + 5] in (MDCV, CLL):
                    j = buf.find(_START, i + 3)
                    if j == -1 and not eof:
                        safe = max(w, i)          # its end is in the next read
                        break
                    fo.write(buf[w:i])
                    w = s = len(buf) if j == -1 else j
                    continue
                s = i + 3
            fo.write(buf[w:safe])
            buf = buf[max(w, safe):]
        fo.write(buf)
    if not tagged:
        raise ValueError("no IRAP picture in the stream — nothing to carry the HDR10 metadata")
    return tagged


def main(argv=None) -> int:
    """`python3 -m hdr10sei SRC DST --mdcv G(..)B(..)R(..)WP(..)L(..) [--cll A,B]`."""
    ap = argparse.ArgumentParser(prog="hdr10sei")
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--mdcv", required=True)
    ap.add_argument("--cll", default="")
    a = ap.parse_args(argv)
    try:
        cll, fall = parse_cll(a.cll)
        n = insert(a.src, a.dst, a.mdcv, cll, fall)
    except (ValueError, OSError) as e:
        print(f"hdr10sei: {e}", file=sys.stderr)
        try:
            os.remove(a.dst)
        except OSError:
            pass
        return 1
    print(f"hdr10sei: tagged {n} keyframes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
