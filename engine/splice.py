"""Re-encode ONLY the over-gate stretches of an HDR10 source and splice them into its original
bitstream (user-dictated 2026-10-20, Deadpool 2: "figure 1 out" — then "make sure this mechanism
works for all inputs").

WHY: a source whose 1-second video peaks pass dvcap.RPU_SHIP_VIDEO_GATE_MBPS (72) ships its OWN
bits with the RPU injected (remux.remux_inject). One that does not used to be re-encoded WHOLE
under the x265 cap — twelve hours for a 4K film whose peaks sit in ~8% of its seconds, and a
generation of loss over the other 92%. This module re-encodes the hot stretches alone (x265,
closed GOPs, the same cap and CRF as the capped path) and splices them between the original
access units, so every other frame is the source's own, bit for bit.

WHAT MAKES A SPLICE CORRECT, FOR ANY GOP STRUCTURE. Everything is decided from the bitstream
(decode order of the access units, and the display order a decoder derives from the POCs and the
coded-video-sequence boundaries), never from container timestamps — a release can carry blocks
without a picture (nalfix's Profile 7 quirk), so a container's packets are only a cross-check.
  * A piece may START at an IRAP S whose decode prefix is a display prefix: every frame decoded
    before S is displayed before every frame decoded from S on.
  * The original may RESUME at an IRAP R. Its RASL pictures (leading pictures that reference the
    group before R) are dropped — their display slots belong to the piece, which re-encodes them
    — and a CRA becomes a BLA (BLA_W_RADL / BLA_N_LP), the H.265 splice point type: it starts a
    new coded video sequence, so the decoder outputs the whole piece first and may switch to the
    original's parameter sets. Every resume IRAP has no_output_of_prior_pics_flag cleared (set,
    it would discard the piece's not-yet-output frames), and gets the original's parameter sets
    in-band when it does not carry them (x265 reuses ids 0).
  * The frames a piece replaces are decoded from the original access units around them (from an
    IRAP whose decode needs nothing earlier) and trimmed to exactly the replaced display range.
  * HDR10+ and every other registered user-data SEI (payload 4) of a replaced frame is copied
    onto its re-encoded frame, so the dynamic metadata never has a hole.

PROOF, EVERY ITEM (any failure falls back to the whole-file capped re-encode, never ships):
the spliced stream's own display order keeps every original frame in its slot and fills the rest
with exactly the piece frames; every seam decodes pixel-exact (original frames == the original's
decode, piece frames == the piece's); the predicted per-second peaks are under the gate before
anything is muxed; the RPU of every access unit is the one for its display slot (remux); the
master's timestamps put every access unit in its slot and its measured peaks pass the gate (remux).
"""
from __future__ import annotations
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from array import array
from fractions import Fraction

import dvcap

FFMPEG = dvcap.FFMPEG
FFPROBE = dvcap.FFPROBE
X265 = dvcap.X265

# ---- HEVC NAL unit types (H.265 Table 7-1) ------------------------------------------------------
TRAIL_N, TRAIL_R = 0, 1
RADL_N, RADL_R, RASL_N, RASL_R = 6, 7, 8, 9
BLA_W_LP, BLA_W_RADL, BLA_N_LP, IDR_W_RADL, IDR_N_LP, CRA_NUT = 16, 17, 18, 19, 20, 21
VPS_NUT, SPS_NUT, PPS_NUT, AUD_NUT, EOS_NUT, EOB_NUT, FD_NUT = 32, 33, 34, 35, 36, 37, 38
PREFIX_SEI, SUFFIX_SEI = 39, 40
RPU_NUT, EL_NUT = 62, 63              # Dolby Vision: the RPU, the enhancement layer
PARAM_SETS = (VPS_NUT, SPS_NUT, PPS_NUT)
LEADING = (RADL_N, RADL_R, RASL_N, RASL_R)
RASL = (RASL_N, RASL_R)
RADL = (RADL_N, RADL_R)
IDR = (IDR_W_RADL, IDR_N_LP)
BLA = (BLA_W_LP, BLA_W_RADL, BLA_N_LP)
T35 = 4                               # SEI user_data_registered_itu_t_t35 (HDR10+, A/53 captions)
MDCV, CLL = 137, 144                  # SEI mastering display colour volume, content light level
_SC = b"\x00\x00\x01"


def is_vcl(t: int) -> bool:
    return t < 32


def is_irap(t: int) -> bool:
    return 16 <= t <= 23


def _sub_layer_non_ref(t: int) -> bool:
    """TRAIL_N, TSA_N, STSA_N, RADL_N, RASL_N and the reserved non-reference types (even, < 16)."""
    return t <= 14 and t % 2 == 0


def _starts_au(t: int) -> bool:
    """A NAL of this type, after a picture's VCL NALs, begins the NEXT access unit (H.265
    7.4.2.4.4): AUD, parameter sets, prefix SEI, and the reserved 41..44 / 48..55."""
    return t in (AUD_NUT, VPS_NUT, SPS_NUT, PPS_NUT, PREFIX_SEI) or 41 <= t <= 44 or 48 <= t <= 55


# ---- RBSP <-> NAL payload --------------------------------------------------------------------------

def unescape(ebsp: bytes) -> bytes:
    """PURE. NAL payload -> RBSP: drop the emulation-prevention 0x03 of every 00 00 03."""
    if b"\x00\x00\x03" not in ebsp:
        return bytes(ebsp)
    out, zeros = bytearray(), 0
    for b in ebsp:
        if zeros >= 2 and b == 3:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def escape(rbsp: bytes) -> bytes:
    """PURE. RBSP -> NAL payload: a 0x03 after every 00 00 that a byte <= 03 would follow, and
    after a trailing 00 00 (cabac_zero_words aside, a payload must not END in 00)."""
    out, zeros = bytearray(), 0
    for b in rbsp:
        if zeros >= 2 and b <= 3:
            out.append(3)
            zeros = 0
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    if zeros >= 2:
        out.append(3)
    return bytes(out)


class Bits:
    """Big-endian bit reader over RBSP bytes, with Exp-Golomb."""

    def __init__(self, data: bytes, pos: int = 0):
        self.d, self.p = data, pos

    def u(self, n: int) -> int:
        v = 0
        for _ in range(n):
            byte = self.d[self.p >> 3] if (self.p >> 3) < len(self.d) else 0
            v = (v << 1) | ((byte >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v

    def ue(self) -> int:
        z = 0
        while self.u(1) == 0:
            z += 1
            if z > 31:
                raise ValueError("Exp-Golomb code too long")
        return (1 << z) - 1 + self.u(z)

    def se(self) -> int:
        k = self.ue()
        return (k + 1) // 2 if k % 2 else -(k // 2)


# ---- parameter sets and slice headers (just what POC needs, plus the traits x265 follows) --------

def _profile_tier_level(b: Bits, max_sub_layers_minus1: int) -> dict:
    """H.265 7.3.3, profilePresentFlag = 1."""
    b.u(2)                                       # general_profile_space
    tier = b.u(1)
    profile = b.u(5)
    b.u(32); b.u(4); b.u(43); b.u(1)            # compatibility flags, source flags, constraints
    level = b.u(8)
    sub_prof, sub_lvl = [], []
    for _ in range(max_sub_layers_minus1):
        sub_prof.append(b.u(1)); sub_lvl.append(b.u(1))
    if max_sub_layers_minus1 > 0:
        for _ in range(max_sub_layers_minus1, 8):
            b.u(2)
    for i in range(max_sub_layers_minus1):
        if sub_prof[i]:
            b.u(88)
        if sub_lvl[i]:
            b.u(8)
    return {"tier": tier, "profile": profile, "level": level}


def parse_sps(rbsp: bytes) -> dict:
    """PURE. SPS RBSP (after the 2-byte NAL header) -> the fields this module needs."""
    b = Bits(rbsp)
    vps_id = b.u(4)
    msl = b.u(3)
    b.u(1)                                       # sps_temporal_id_nesting_flag
    ptl = _profile_tier_level(b, msl)
    sps_id = b.ue()
    chroma = b.ue()
    sep = b.u(1) if chroma == 3 else 0
    width, height = b.ue(), b.ue()
    crop = [0, 0, 0, 0]
    if b.u(1):                                   # conformance_window_flag
        crop = [b.ue(), b.ue(), b.ue(), b.ue()]  # left, right, top, bottom (chroma units)
    sub_w = 2 if chroma in (1, 2) else 1
    sub_h = 2 if chroma == 1 else 1
    bd_luma = b.ue() + 8
    bd_chroma = b.ue() + 8
    log2_poc = b.ue() + 4
    if not 4 <= log2_poc <= 16:
        raise ValueError(f"log2_max_pic_order_cnt_lsb {log2_poc} out of range")
    present = b.u(1)
    dpb = reorder = 0
    for _ in range(0 if present else msl, msl + 1):
        dpb = b.ue() + 1
        reorder = b.ue()
        b.ue()                                   # sps_max_latency_increase_plus1
    log2_min_cb = b.ue() + 3                     # log2_min_luma_coding_block_size_minus3
    min_cb = 1 << log2_min_cb
    ctb = 1 << (log2_min_cb + b.ue())            # log2_diff_max_min_luma_coding_block_size
    return {"vps_id": vps_id, "sps_id": sps_id, "chroma": chroma, "separate_planes": sep,
            "width": width, "height": height,
            "display_width": width - sub_w * (crop[0] + crop[1]),
            "display_height": height - sub_h * (crop[2] + crop[3]),
            "min_cb": min_cb, "ctb": ctb, "bit_depth": bd_luma, "bit_depth_chroma": bd_chroma,
            "log2_poc": log2_poc, "dpb": dpb, "reorder": reorder, "max_sub_layers": msl + 1, **ptl}


def parse_pps(rbsp: bytes) -> dict:
    """PURE. PPS RBSP -> its id, its SPS and the flags the slice header depends on."""
    b = Bits(rbsp)
    pps_id = b.ue()
    sps_id = b.ue()
    dep = b.u(1)
    out_flag = b.u(1)
    extra = b.u(3)
    return {"pps_id": pps_id, "sps_id": sps_id, "dependent_slices": dep,
            "output_flag_present": out_flag, "extra_bits": extra}


def parse_slice_head(rbsp: bytes, t: int, pps_by_id: dict, sps_by_id: dict) -> dict:
    """PURE. The first slice segment of a picture -> its PPS id, no_output_of_prior_pics_flag,
    pic_output_flag and POC lsb (0 for an IDR)."""
    b = Bits(rbsp)
    first = b.u(1)
    if not first:
        raise ValueError("not the first slice segment of a picture")
    no_output = b.u(1) if is_irap(t) else 0
    pps_id = b.ue()
    pps = pps_by_id.get(pps_id)
    if pps is None:
        raise ValueError(f"slice refers to PPS {pps_id}, which the stream never sent")
    sps = sps_by_id.get(pps["sps_id"])
    if sps is None:
        raise ValueError(f"PPS {pps_id} refers to SPS {pps['sps_id']}, which the stream never sent")
    b.u(pps["extra_bits"])
    b.ue()                                       # slice_type
    pic_output = b.u(1) if pps["output_flag_present"] else 1
    if sps["separate_planes"]:
        b.u(2)
    lsb = 0 if t in IDR else b.u(sps["log2_poc"])
    return {"pps_id": pps_id, "sps_id": pps["sps_id"], "no_output_of_prior_pics": no_output,
            "pic_output": pic_output, "poc_lsb": lsb}


def param_id(t: int, rbsp: bytes) -> int:
    """PURE. The id a VPS / SPS / PPS defines."""
    if t == VPS_NUT:
        return rbsp[0] >> 4
    if t == SPS_NUT:
        return parse_sps(rbsp)["sps_id"]
    return Bits(rbsp).ue()


# ---- SEI -------------------------------------------------------------------------------------------

def parse_sei(rbsp: bytes) -> list:
    """PURE. SEI RBSP (after the NAL header) -> [(payload_type, payload_bytes)]."""
    msgs, i, n = [], 0, len(rbsp)
    while i < n:
        if rbsp[i] == 0x80 and all(x == 0 for x in rbsp[i + 1:]):
            break                                # rbsp_trailing_bits
        pt = 0
        while i < n and rbsp[i] == 0xFF:
            pt += 255; i += 1
        if i >= n:
            raise ValueError("SEI payload type runs past the NAL")
        pt += rbsp[i]; i += 1
        ps = 0
        while i < n and rbsp[i] == 0xFF:
            ps += 255; i += 1
        if i >= n:
            raise ValueError("SEI payload size runs past the NAL")
        ps += rbsp[i]; i += 1
        if i + ps > n:
            raise ValueError("SEI payload runs past the NAL")
        msgs.append((pt, rbsp[i:i + ps]))
        i += ps
    return msgs


def _ff_coded(v: int) -> bytes:
    return b"\xff" * (v // 255) + bytes([v % 255])


def build_sei_nal(msgs: list, temporal_id: int = 0, suffix: bool = False) -> bytes:
    """PURE. [(payload_type, payload)] -> one SEI NAL, 4-byte start code included."""
    body = b"".join(_ff_coded(pt) + _ff_coded(len(p)) + bytes(p) for pt, p in msgs) + b"\x80"
    t = SUFFIX_SEI if suffix else PREFIX_SEI
    return b"\x00\x00\x00\x01" + bytes([t << 1, temporal_id + 1]) + escape(body)


# ---- splitting a buffer into NAL units ---------------------------------------------------------------

def nal_spans(buf: bytes, base: int = 0) -> list:
    """PURE. [(start, header, end, type)] of every NAL in an Annex-B buffer. `start` includes a
    4-byte start code's zero_byte; `end` is where the next NAL's start code begins (trailing zero
    bytes stay with the NAL before them — they belong to no picture)."""
    spans = []
    p = buf.find(_SC)
    while p != -1:
        h = p + 3
        s = p - 1 if p > 0 and buf[p - 1] == 0 else p
        q = buf.find(_SC, h)
        e = len(buf) if q == -1 else (q - 1 if buf[q - 1] == 0 else q)
        if h < len(buf):
            spans.append((base + s, base + h, base + e, (buf[h] >> 1) & 0x3F))
        p = q
    return spans


# ---- the index: one record per access unit, decode order ---------------------------------------------

class Index:
    """Decode-order access units of an Annex-B HEVC stream with the display order a decoder
    derives (POC within each coded video sequence, sequences in decode order).

    Per AU: off/end (bytes), t (NAL type of its first slice), tid, poc, cvs (its sequence number),
    ps (carries a VPS/SPS/PPS), no_out (no_output_of_prior_pics_flag), out (pic_output_flag), and
    the derived rank (display index). `t35` maps AU -> [(offset, length)] of its prefix SEI NALs
    that hold payload-4 messages; `rpu` (when captured) maps AU -> its RPU NAL bytes. `params`
    maps each AU that carried parameter sets -> [(type, id, offset, end)] of them: a resume
    point without its own gets the latest of each (type, id) from there (params_before).
    `hidden` lists AUs a decoder never outputs (pic_output_flag 0, or a RASL picture of an IRAP
    that starts a sequence) — a source with any is not spliced. `hdr` maps AU -> [(offset,
    length)] of its prefix SEI NALs carrying the HDR10 static metadata (MDCV 137 / CLL 144);
    `eos` lists the AUs an end-of-sequence / end-of-bitstream NAL closes; `layered` counts NALs
    of a layer above 0 (MV-HEVC / SHVC — never spliced). `sps` / `pps` are the parsed sets
    by id (the last of each); `first_sps` the first one a picture activated."""

    def __init__(self):
        self.off, self.end = array("q"), array("q")
        self.t, self.tid, self.cvs = array("b"), array("b"), array("i")
        self.poc = array("i")
        self.ps, self.no_out, self.out = bytearray(), bytearray(), bytearray()
        self.t35, self.rpu, self.params, self.hdr = {}, {}, {}, {}
        self.hidden, self.eos, self.layered = [], [], 0
        self.misplaced = []          # AUs whose Dolby Vision NALs are not one RPU, last (capture_rpu)
        self.sps, self.pps, self.vps_ids = {}, {}, set()
        self.first_sps = None
        self.rank = None
        self.notes = []

    def __len__(self):
        return len(self.off)

    _ARRAYS = ("off", "end", "t", "tid", "cvs", "poc", "rank")

    def save(self, path: str, identity: str) -> None:
        """Write the index next to the stream it describes (`identity`: the stream's size:mtime),
        atomically — a resumed splice reloads it instead of reading 49 GB again."""
        d = {"identity": identity, "n": len(self),
             "arrays": {k: [getattr(self, k).typecode, getattr(self, k).tobytes().hex()]
                        for k in self._ARRAYS},
             "bytes": {k: bytes(getattr(self, k)).hex() for k in ("ps", "no_out", "out")},
             "t35": {str(k): v for k, v in self.t35.items()},
             "hdr": {str(k): v for k, v in self.hdr.items()},
             "params": {str(k): v for k, v in self.params.items()},
             "hidden": self.hidden, "eos": self.eos, "layered": self.layered,
             "sps": {str(k): v for k, v in self.sps.items()},
             "pps": {str(k): v for k, v in self.pps.items()},
             "vps_ids": sorted(self.vps_ids), "first_sps": self.first_sps, "notes": self.notes}
        tmp = path + ".part"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str, identity: str):
        """The saved index of a stream whose identity still matches, else None."""
        try:
            with open(path) as f:
                d = json.load(f)
            if d.get("identity") != identity:
                return None
            ix = cls()
            for k, (tc, hx) in d["arrays"].items():
                a = array(tc)
                a.frombytes(bytes.fromhex(hx))
                setattr(ix, k, a)
            for k, hx in d["bytes"].items():
                setattr(ix, k, bytearray(bytes.fromhex(hx)))
            ix.t35 = {int(k): [tuple(x) for x in v] for k, v in d["t35"].items()}
            ix.hdr = {int(k): [tuple(x) for x in v] for k, v in d["hdr"].items()}
            ix.params = {int(k): [tuple(x) for x in v] for k, v in d["params"].items()}
            ix.hidden, ix.eos, ix.layered = d["hidden"], d["eos"], d["layered"]
            ix.sps = {int(k): v for k, v in d["sps"].items()}
            ix.pps = {int(k): v for k, v in d["pps"].items()}
            ix.vps_ids, ix.first_sps, ix.notes = set(d["vps_ids"]), d["first_sps"], d["notes"]
            if len(ix) != d["n"] or len(ix.rank) != d["n"]:
                return None
            return ix
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def irap_list(self) -> list:
        """The IRAP access units, decode order (cached): bisect lookups instead of scans."""
        if getattr(self, "_iraps", None) is None or len(self._iraps_of) != len(self):
            self._iraps = [i for i in range(len(self)) if is_irap(self.t[i])]
            self._iraps_of = range(len(self))
        return self._iraps

    def derive_ranks(self):
        """Display order: sequences in decode order, POC ascending inside each."""
        order = sorted(range(len(self)), key=lambda i: (self.cvs[i], self.poc[i]))
        rank = array("i", [0]) * len(self)
        for r, i in enumerate(order):
            rank[i] = r
        self.rank = rank
        prev = None
        for i in order:
            key = (self.cvs[i], self.poc[i])
            if key == prev:
                raise ValueError(f"two pictures share POC {self.poc[i]} in one sequence")
            prev = key


def scan(path: str, *, capture_rpu: bool = False, chunk: int = 64 << 20, on_progress=None,
         stop=None) -> Index:
    """Read an Annex-B HEVC file once and index its access units (see Index). Raises ValueError on
    a stream this module cannot reason about (a slice whose PPS/SPS never came, duplicate POCs, a
    picture with no slice). `stop()` -> True aborts with InterruptedError."""
    ix = Index()
    total = os.path.getsize(path)
    au = None                             # the open AU: {off, vcl, t, tid, ps, t35}
    prev_tid0 = None                      # (poc) of the previous TemporalId-0 reference picture
    cvs = -1
    after_eos = True                      # stream start counts as "after an end of sequence"
    irap_no_rasl = False                  # the latest IRAP started a sequence: its RASL are never shown

    def close(end_off):
        nonlocal au
        if au is None:
            return
        if not au["vcl"]:
            raise ValueError(f"an access unit at byte {au['off']} carries no picture")
        i = len(ix.off)
        ix.off.append(au["off"]); ix.end.append(end_off)
        ix.t.append(au["t"]); ix.tid.append(au["tid"]); ix.cvs.append(au["cvs"])
        ix.poc.append(au["poc"])
        ix.ps.append(1 if au["ps"] else 0)
        ix.no_out.append(au["no_out"]); ix.out.append(au["out"])
        if au["t35"]:
            ix.t35[i] = au["t35"]
        if au.get("rpu") is not None:
            ix.rpu[i] = au["rpu"]
        if capture_rpu and (au.get("rpu_n", 0) > 1 or au.get("el") or au.get("after_rpu")):
            ix.misplaced.append(i)
        if au.get("params"):
            ix.params[i] = au["params"]
        if au.get("hdr"):
            ix.hdr[i] = au["hdr"]
        if au.get("eos"):
            ix.eos.append(i)
        if not au["out"] or au.get("hidden"):
            ix.hidden.append(i)
        au = None

    with open(path, "rb") as f:
        carry, carry_off, done = b"", 0, 0
        while True:
            data = f.read(chunk)
            last = not data
            buf = carry + data
            base = carry_off
            spans = nal_spans(buf, base)
            # the final NAL of this buffer may continue into the next read: keep it back
            keep_from = None
            if not last and spans:
                keep_from = spans[-1][0]
                spans = spans[:-1]
            for (s, h, e, t) in spans:
                lo, hi = s - base, e - base
                hb = h - base
                tid = (buf[hb + 1] & 7) - 1 if hb + 1 < len(buf) else 0
                if hb + 1 < len(buf) and ((buf[hb] & 1) << 5 | buf[hb + 1] >> 3):
                    ix.layered += 1                  # nuh_layer_id > 0
                if is_vcl(t):
                    first = (buf[hb + 2] & 0x80) if hb + 2 < hi else 0
                    if au is not None and au.get("rpu_n") and not first:
                        au["after_rpu"] = True       # a slice of the SAME picture after its RPU
                    if first and au is not None and au["vcl"]:
                        close(s)
                    if au is None:
                        au = {"off": s, "vcl": False, "ps": False, "t35": [], "t": t}
                    if first:
                        rb = unescape(buf[hb + 2:min(hi, hb + 2 + 96)])
                        sh = parse_slice_head(rb, t, ix.pps, ix.sps)
                        if ix.first_sps is None:
                            ix.first_sps = dict(ix.sps[sh["sps_id"]])
                        sps = ix.sps[sh["sps_id"]]
                        max_lsb = 1 << sps["log2_poc"]
                        irap = is_irap(t)
                        no_rasl = irap and (t in IDR or t in BLA or after_eos)
                        if irap and no_rasl:
                            cvs += 1
                            msb = 0
                        else:
                            if prev_tid0 is None:
                                raise ValueError("the stream does not start with an IRAP picture")
                            p_lsb, p_msb = prev_tid0
                            lsb = sh["poc_lsb"]
                            if lsb < p_lsb and p_lsb - lsb >= max_lsb // 2:
                                msb = p_msb + max_lsb
                            elif lsb > p_lsb and lsb - p_lsb > max_lsb // 2:
                                msb = p_msb - max_lsb
                            else:
                                msb = p_msb
                        after_eos = False
                        if irap:
                            irap_no_rasl = no_rasl
                        poc = msb + sh["poc_lsb"]
                        if tid == 0 and t not in LEADING and not _sub_layer_non_ref(t):
                            prev_tid0 = (sh["poc_lsb"], msb)
                        au.update(vcl=True, t=t, tid=tid, cvs=cvs, poc=poc,
                                  no_out=sh["no_output_of_prior_pics"], out=sh["pic_output"],
                                  hidden=(t in RASL and irap_no_rasl))
                    elif au is None or not au["vcl"]:
                        raise ValueError(f"a picture at byte {s} starts with a dependent slice")
                    continue
                if au is not None and au["vcl"] and _starts_au(t):
                    close(s)
                if au is not None and au.get("rpu_n") and t not in (EOS_NUT, EOB_NUT, RPU_NUT):
                    au["after_rpu"] = True           # only EOS / EOB may follow an RPU
                if au is not None and au.get("eos") and t not in (EOB_NUT,) and capture_rpu:
                    au["after_rpu"] = True           # nothing but EOB after an EOS (7.4.2.4.4)
                if t in (EOS_NUT, EOB_NUT):
                    after_eos = True
                    if au is not None:
                        au["eos"] = True
                    continue                     # it ends the AU it follows (it stays inside it)
                if au is None:
                    if t in (SUFFIX_SEI, RPU_NUT, EL_NUT, FD_NUT):
                        raise ValueError(f"a NAL of type {t} at byte {s} precedes every picture")
                    au = {"off": s, "vcl": False, "ps": False, "t35": [], "t": None}
                if t in PARAM_SETS:
                    au["ps"] = True
                    rb = unescape(buf[hb + 2:hi])
                    if t == SPS_NUT:
                        sp = parse_sps(rb)
                        ix.sps[sp["sps_id"]] = sp
                        pid = sp["sps_id"]
                    elif t == PPS_NUT:
                        pp = parse_pps(rb)
                        ix.pps[pp["pps_id"]] = pp
                        pid = pp["pps_id"]
                    else:
                        pid = rb[0] >> 4
                        ix.vps_ids.add(pid)
                    au.setdefault("params", []).append((t, pid, s, e))
                elif t == PREFIX_SEI and not au["vcl"]:
                    try:
                        kinds = {pt for pt, _p in parse_sei(unescape(buf[hb + 2:hi]))}
                    except ValueError:
                        kinds = None
                        ix.notes.append(f"unreadable SEI at byte {s}")
                    if kinds is None or T35 in kinds:
                        au["t35"].append((s, e - s))    # (unreadable: checked again at use)
                    if kinds and kinds & {MDCV, CLL}:
                        au.setdefault("hdr", []).append((s, e - s))
                elif t == RPU_NUT and capture_rpu:
                    au["rpu"] = bytes(buf[lo:hi])
                    au["rpu_n"] = au.get("rpu_n", 0) + 1
                elif t == EL_NUT and capture_rpu:
                    au["el"] = True
            if last:
                close(base + len(buf))
                break
            carry = buf[keep_from - base:] if keep_from is not None else b""
            carry_off = keep_from if keep_from is not None else base + len(buf)
            done += len(data)
            if on_progress:
                on_progress(done, total)
            if stop is not None and stop():
                raise InterruptedError("stopped")
    if not len(ix):
        raise ValueError("no pictures in the stream")
    ix.derive_ranks()
    return ix


def params_before(ix: Index, i: int) -> list:
    """[(type, id, offset, end)] — the latest parameter-set NAL of every (type, id) the stream sent
    before access unit `i` (in VPS, SPS, PPS order): what a resume point that carries none of its
    own must re-send, since the piece before it overwrote the ids it used."""
    seen, out = set(), []
    for j in sorted((k for k in ix.params if k < i), reverse=True):
        for (t, pid, s, e) in reversed(ix.params[j]):
            if (t, pid) not in seen:
                seen.add((t, pid))
                out.append((t, pid, s, e))
    return sorted(out, key=lambda x: (x[0], x[1]))


# ---- the plan: which display ranges to re-encode, cut where ----------------------------------------

SPLICE_MARGIN_SECONDS = 1.0     # every hot stretch grows by this much each way, then out to cuts
SPLICE_MERGE_SECONDS = 2.0      # two pieces closer than this become one (fewer seams)
SPLICE_MAX_SHARE = 0.5          # past half the film, the whole-file capped encode is the cleaner job
SPLICE_ROUNDS = 3               # predicted-peak repair rounds before giving up on the splice


class NotSpliceable(Exception):
    """The source cannot be spliced (structure, share, an unverifiable seam): the caller takes the
    whole-file capped re-encode instead. Never a reason to ship nothing."""


def leading_of(t, c: int) -> range:
    """PURE. The leading pictures of IRAP `c`: they follow it directly in decode order."""
    j = c + 1
    while j < len(t) and t[j] in LEADING:
        j += 1
    return range(c + 1, j)


def ms_of_rank(rank: int, fps: Fraction) -> int:
    """PURE. The master's timestamp of display frame `rank` in ms: mkvmerge stamps frame r at
    r / fps (from --default-duration) on its 1 ms timestamp scale, rounded to nearest."""
    num, den = fps.numerator, fps.denominator
    return (rank * 1000 * den * 2 + num) // (2 * num)


def second_buckets(sizes_by_rank, fps: Fraction) -> dict:
    """PURE. {second: Mbps} of the master: frame r's bytes land in second ms_of_rank(r) // 1000 —
    the bucketing dvcap.peak_buckets_from_packets applies to the shipped file."""
    out = {}
    for r, sz in enumerate(sizes_by_rank):
        s = ms_of_rank(r, fps) // 1000
        out[s] = out.get(s, 0) + sz
    return {s: b * 8 / 1e6 for s, b in out.items()}


def cut_points(t, rank) -> tuple:
    """PURE. Where pieces may start and the original may resume, in DISPLAY (rank) space.
    -> (starts, resumes): `starts` = sorted ranks S where a piece may begin (S is also the decode
    index of that IRAP — its decode prefix is its display prefix); `resumes` = {De: R}: the
    original can resume at IRAP R (its RASL dropped) and a piece then ends at display rank De.
    De == N -> R == N (the piece runs to the end)."""
    n = len(t)
    pm, m = [0] * (n + 1), -1                     # pm[i] = max(rank[0..i))
    pm[0] = -1
    for i in range(n):
        m = max(m, rank[i])
        pm[i + 1] = m
    starts, resumes = [], {n: n}
    for c in range(n):
        if t[c] not in (IDR_W_RADL, IDR_N_LP, CRA_NUT):
            continue                               # BLA in a source: never cut there
        lead = leading_of(t, c)
        rasl = [j for j in lead if t[j] in RASL]
        if pm[c] == c - 1 and (not rasl or any(is_irap(t[k]) for k in range(c))):
            starts.append(c)                       # (a RASL start needs an earlier IRAP to decode from)
        mq = max((rank[j] for j in rasl), default=-1)
        if max(pm[c], mq) == c + len(rasl) - 1:
            resumes[c + len(rasl)] = c
    return starts, resumes


def window_len(fps: Fraction) -> int:
    """PURE. Frames in the 1-second window the gate is applied to: ceil(fps), so every fixed
    1-second bucket of the master (dvcap.video_peak_buckets) lies inside some window — a stream
    whose every window passes, passes the fixed ruler too."""
    return int(math.ceil(float(fps)))


def windows_over(sizes_by_rank, fps: Fraction, gate_mbps: float) -> list:
    """PURE. [(first_rank, mbps)] of every SLIDING window of window_len(fps) consecutive display
    frames whose bytes pass the gate (Mb per second). Fixed 1-second buckets miss the windows
    that straddle two buckets: the review measured 190 such windows at up to 79 Mbps left in
    Deadpool 2 by a fixed-bucket plan."""
    n, w = len(sizes_by_rank), window_len(fps)
    acc = [0] * (n + 1)
    for r, sz in enumerate(sizes_by_rank):
        acc[r + 1] = acc[r] + sz
    limit = gate_mbps * 1e6 / 8
    out = []
    for a in range(0, max(1, n - w + 1)):
        b = min(n, a + w)
        if acc[b] - acc[a] > limit:
            out.append((a, (acc[b] - acc[a]) * 8 / 1e6))
    return out


def window_peak_mbps_ranked(sizes_by_rank, fps: Fraction) -> float:
    """PURE. The largest sliding window (Mb per second) of a display-ordered size list."""
    n, w = len(sizes_by_rank), window_len(fps)
    acc = [0] * (n + 1)
    for r, sz in enumerate(sizes_by_rank):
        acc[r + 1] = acc[r] + sz
    return max(((acc[min(n, a + w)] - acc[a]) * 8 / 1e6 for a in range(0, max(1, n - w + 1))),
               default=0.0)


def hot_ranks(sizes_by_rank, fps: Fraction, gate_mbps: float) -> list:
    """PURE. Display ranks of every frame inside a sliding window over the gate."""
    w = window_len(fps)
    mark = bytearray(len(sizes_by_rank) + 1)
    for a, _m in windows_over(sizes_by_rank, fps, gate_mbps):
        for r in range(a, min(len(sizes_by_rank), a + w)):
            mark[r] = 1
    return [r for r in range(len(sizes_by_rank)) if mark[r]]


def plan_runs(t, rank, sizes_by_rank, fps: Fraction, gate_mbps: float, *,
              margin_seconds: float = SPLICE_MARGIN_SECONDS,
              merge_seconds: float = SPLICE_MERGE_SECONDS, extra_ranks=()) -> list:
    """PURE. [(Ds, De)] display ranges to re-encode: every frame of every over-gate second (and
    `extra_ranks`, the repair rounds' additions), widened by `margin_seconds` each way — the
    seconds either side of a seam mix original and re-encoded bytes — then out to the nearest
    valid piece start / resume, merged when closer than `merge_seconds`. A margin in seconds, not
    in groups of pictures: a source with 5-second GOPs would otherwise re-encode 15 seconds around
    every hot one (measured in the matrix)."""
    import bisect
    starts, resumes = cut_points(t, rank)
    ends = sorted(resumes)
    n = len(t)
    if not starts or starts[0] != 0:
        raise NotSpliceable("the stream does not start at a point a piece could start at")
    want = sorted(set(hot_ranks(sizes_by_rank, fps, gate_mbps)) | set(extra_ranks))
    m = int(round(float(fps) * margin_seconds))
    groups = []
    for r in want:
        if groups and r <= groups[-1][1] + 1:
            groups[-1][1] = r
        else:
            groups.append([r, r])
    spans = []
    for g0, g1 in groups:
        lo, hi = max(0, g0 - m), min(n, g1 + 1 + m)
        a = starts[bisect.bisect_right(starts, lo) - 1]
        b = ends[bisect.bisect_left(ends, hi)]
        spans.append((a, b))
    gap = int(round(float(fps) * merge_seconds))
    merged = []
    for a, b in sorted(spans):
        if merged and a - merged[-1][1] < gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


SPLICE_MAX_PIECE_SECONDS = dvcap.SEG_SECONDS   # a piece is at most this long (split at cuts): a
                                              # lease, a stop or a deploy waits at most one piece,
                                              # as the capped path waits one segment


def split_long(spans: list, starts: list, resumes: dict, max_frames: int) -> list:
    """PURE. Each (Ds, De) longer than `max_frames`, cut into abutting pieces at points c that a
    piece may start at AND the stream may resume at with nothing dropped (resumes[c] == c: an IDR,
    or a CRA without RASL), so one piece ends exactly where the next begins."""
    import bisect
    ok = [c for c in starts if resumes.get(c) == c]
    out = []
    for a, b in spans:
        while b - a > max_frames:
            k = bisect.bisect_right(ok, a + max_frames) - 1
            if k < 0 or ok[k] <= a:
                k = bisect.bisect_right(ok, a)       # none in reach: the first one after
                if k >= len(ok) or ok[k] >= b:
                    break                            # no clean cut inside: one long piece
            out.append((a, ok[k]))
            a = ok[k]
        out.append((a, b))
    return out


def run_geometry(t, rank, ds: int, de: int, resumes: dict, iraps=None) -> dict:
    """PURE. Everything about re-encoding display range [ds, de):
    S/R (decode indices: the piece replaces AUs [S, R) and R's RASL), drop (those RASL), bla
    (the NAL type R's slices become, or None), W/E (the original AUs [W, E) decoded to make the
    piece's frames), k (decoded frames before ds, trimmed), n (= de - ds)."""
    n_all = len(t)
    S, R = ds, resumes[de]
    drop = [j for j in leading_of(t, R) if t[j] in RASL] if R < n_all else []
    bla = None
    if R < n_all and t[R] == CRA_NUT:
        radl = any(t[j] in RADL for j in leading_of(t, R))
        bla = BLA_W_RADL if radl else BLA_N_LP
    W = S
    if S < n_all and any(t[j] in RASL for j in leading_of(t, S)):
        if iraps is not None:
            import bisect
            W = iraps[bisect.bisect_left(iraps, S) - 1]
        else:
            W = max(k for k in range(S) if is_irap(t[k]))
    E = (R + 1 + len(leading_of(t, R))) if R < n_all else n_all
    skipped = set()
    if t[W] == CRA_NUT or t[W] in BLA:            # decoding starts here: its RASL are never output
        skipped = {j for j in leading_of(t, W) if t[j] in RASL}
    outs = [i for i in range(W, E) if i not in skipped]
    k = sum(1 for i in outs if rank[i] < ds)
    got = sum(1 for i in outs if ds <= rank[i] < de)
    if got != de - ds:
        raise NotSpliceable(f"frames {ds}-{de - 1}: decoding AUs {W}-{E - 1} yields {got} of them")
    return {"ds": ds, "de": de, "S": S, "R": R, "drop": drop, "bla": bla, "W": W, "E": E,
            "k": k, "n": de - ds}


# ---- the pieces: x265 following the source ---------------------------------------------------------

# Main-tier maximum bitrate (kbps, NAL HRD) per general_level_idc — a cap over it needs High tier
_MAIN_TIER_KBPS = {30: 141, 60: 1650, 63: 3300, 90: 6600, 93: 11000, 120: 13200, 123: 22000,
                   150: 27500, 153: 44000, 156: 66000, 180: 66000, 183: 132000, 186: 264000}
HIGH_TIER_FROM_LEVEL = 120      # H.265 Table A.8: levels below 4 have a Main tier only
_CHROMA_LOC = {"left": 0, "center": 1, "topleft": 2, "top": 3, "bottomleft": 4, "bottom": 5}
PIECE_SETTINGS_VERSION = 2      # bump when the piece encode changes: resumed pieces are re-made


def x265_dpb(bframes: int, ref: int, pyramid: bool) -> tuple:
    """PURE. (sps_max_dec_pic_buffering, sps_max_num_reorder_pics) x265 4.1 writes for a GOP
    structure (Encoder::initVPS: reorder = 2 with a B-pyramid of >1 B-frames, else 1 per B-frame
    run; dpb = max(reorder + 2, ref) + 1, at most 16)."""
    reorder = 2 if (pyramid and bframes > 1) else (1 if bframes else 0)
    return min(16, max(reorder + 2, ref) + 1), reorder


def piece_structure(src_dpb: int, src_reorder: int, log2_poc: int = 16):
    """PURE. (bframes, ref, pyramid) whose decoded-picture buffer EQUALS the source's and whose
    reorder depth fits inside it, or None. Equal, not merely smaller: H.265 C.5.2.2 lets a decoder
    infer no_output_of_prior_pics_flag = 1 — and drop the frames still waiting — at a sequence
    whose sps_max_dec_pic_buffering differs from the one before (review, 2026-10-20)."""
    for reorder, b, pyr in ((2, 3, True), (1, 1, False), (0, 0, False)):
        if reorder > src_reorder:
            continue
        if pyr and log2_poc < 6:
            continue        # x265 4.1 widens a 4/5-bit POC to 6 for a B-pyramid (measured): the
                            # piece would no longer match the source's POC width
        ref = src_dpb - 1
        if ref >= max(1, reorder + 2) and ref <= 16 and x265_dpb(b, ref, pyr) == (src_dpb, reorder):
            return b, ref, pyr
    return None


def source_traits(ix: Index, base_container: str, ffprobe: str = FFPROBE) -> dict:
    """What the pieces must match: the first SPS's profile / tier / level / bit depth / DPB, the
    container stream's colour range, chroma siting and sample aspect ratio, the HDR10 mastering
    metadata, and the longest GOP (the pieces' keyframe interval)."""
    sp = ix.first_sps or {}
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=color_range,chroma_location,sample_aspect_ratio,field_order",
                              "-of", "json", base_container], capture_output=True, text=True,
                             timeout=120).stdout
        st = (json.loads(out or "{}").get("streams") or [{}])[0]
    except (OSError, ValueError, subprocess.SubprocessError):
        st = {}
    iraps = ix.irap_list()
    gop = max((b - a for a, b in zip(iraps, iraps[1:] + [len(ix)])), default=len(ix))
    sar = st.get("sample_aspect_ratio") or "1:1"
    return {"profile": sp.get("profile"), "tier": sp.get("tier", 1), "level": sp.get("level"),
            "bit_depth": sp.get("bit_depth"), "chroma": sp.get("chroma"),
            "ctb": sp.get("ctb"), "min_cb": sp.get("min_cb"),
            "dpb": sp.get("dpb", 5), "reorder": sp.get("reorder", 2),
            "width": sp.get("width"), "height": sp.get("height"),
            "display_width": sp.get("display_width"), "display_height": sp.get("display_height"),
            "log2_poc": sp.get("log2_poc"),
            "range": "full" if st.get("color_range") == "pc" else "limited",
            "chromaloc": _CHROMA_LOC.get(st.get("chroma_location") or ""),
            "sar": None if sar in ("1:1", "0:1", "N/A") else sar,
            "field_order": st.get("field_order") or "",
            "keyint": max(1, min(gop, 600))}


PIECE_CRF = 14                  # a notch under the capped path's 16: a piece sits between original
                                # frames, and grain that steps down at a seam is what shows (review)


def build_piece_x265_command(x265: str, out_hevc: str, cap_mbps: int, traits: dict, *,
                             crf: float = None, preset: str = None) -> list:
    """PURE. x265 for one piece: the capped path's VBV ceiling, BT.2020 / PQ, CLOSED GOPs (every
    keyframe an IDR, so the piece decodes on its own and the original can resume right after it),
    and everything a decoder sizes itself by, taken from the source: level and tier, decoded-
    picture buffer (piece_structure), CTB and minimum CB size (an Apple decoder stops at a CTB
    change, measured), coded size, POC width, chroma siting, range, aspect ratio. No weighted
    prediction (hevcids renumbers slice headers and refuses a pred_weight_table), no HDR10 SEI
    and no HRD of x265's own — the pieces carry the ORIGINAL's mastering metadata (write_spliced),
    and no RPU: the item's one RPU is written into every frame of the spliced stream."""
    crf = PIECE_CRF if crf is None else crf
    preset = dvcap.X265_PRESET if preset is None else preset
    kbps = int(cap_mbps) * 1000
    st = piece_structure(int(traits.get("dpb") or 0), int(traits.get("reorder") or 0),
                         int(traits.get("log2_poc") or 16))
    if st is None:
        raise NotSpliceable(f"x265 cannot match the source's decoded-picture buffer "
                            f"({traits.get('dpb')} / reorder {traits.get('reorder')})")
    b, ref, pyr = st
    cmd = [x265, "--y4m", "-", "--output", out_hevc, "--preset", preset, "--crf", str(crf),
           "--output-depth", "10", "--vbv-maxrate", str(kbps), "--vbv-bufsize", str(kbps),
           "--repeat-headers", "--aud", "--no-open-gop", "--no-weightp", "--no-weightb",
           "--keyint", str(int(traits.get("keyint") or 24)),
           "--bframes", str(b), "--ref", str(ref), "--b-pyramid" if pyr else "--no-b-pyramid",
           "--range", traits.get("range") or "limited", "--colorprim", "bt2020",
           "--transfer", "smpte2084", "--colormatrix", "bt2020nc"]
    lvl = traits.get("level")
    if lvl:
        main_ok = int(lvl) < HIGH_TIER_FROM_LEVEL or (
            int(traits.get("tier") or 0) == 0 and kbps <= _MAIN_TIER_KBPS.get(int(lvl), 0))
        cmd += ["--level-idc", f"{int(lvl) / 30:.1f}", "--no-high-tier" if main_ok else "--high-tier"]
    if traits.get("ctb") in (16, 32, 64):
        cmd += ["--ctu", str(int(traits["ctb"]))]
    if traits.get("min_cb") in (8, 16, 32):
        cmd += ["--min-cu-size", str(int(traits["min_cb"]))]
    if traits.get("log2_poc") and 4 <= int(traits["log2_poc"]) <= 16:
        # the source's own POC width: a parser that keeps the stream's FIRST SPS (dovi_tool 2.3.0
        # read x265's 8-bit POCs with a 6-bit source's and misplaced 85 RPUs, measured) still
        # reads every piece right
        cmd += ["--log2-max-poc-lsb", str(int(traits["log2_poc"]))]
    if traits.get("chromaloc") is not None:
        cmd += ["--chromaloc", str(int(traits["chromaloc"]))]
    if traits.get("sar"):
        cmd += ["--sar", str(traits["sar"])]
    return cmd


def build_piece_decode_command(ffmpeg: str, es: str, fps: str, k: int, n: int) -> list:
    """PURE. The original access units around a piece -> exactly its n frames as 10-bit y4m:
    decode (display order) and drop the k frames before it (trim counts decoded frames, so the
    selection is exact — no timestamp seek)."""
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-r", str(fps), "-f", "hevc",
            "-i", es, "-map", "0:v:0", "-vf", f"trim=start_frame={int(k)}:end_frame={int(k) + int(n)}",
            "-fps_mode", "passthrough", "-pix_fmt", "yuv420p10le", "-strict", "-1",
            "-f", "yuv4mpegpipe", "-"]


def _read(path: str, a: int, b: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(a)
        return f.read(b - a)


def _copy_range(src: str, a: int, b: int, dst, block: int = 32 << 20) -> None:
    with open(src, "rb") as f:
        f.seek(a)
        left = b - a
        while left > 0:
            chunk = f.read(min(block, left))
            if not chunk:
                raise OSError(f"{os.path.basename(src)} ended before byte {b}")
            dst.write(chunk)
            left -= len(chunk)


def missing_params(path: str, ix: Index, i: int) -> list:
    """[(type, NAL bytes)] — every parameter set (type, id) defined before AU `i` that `i` does not
    carry itself, latest definition, VPS/SPS/PPS order: what a decode starting at `i` needs. Per
    id, never all-or-nothing (a stream that repeats only its PPS still needs the VPS and SPS of its
    start; one with two PPS ids needs both, review)."""
    have = {(t, pid) for (t, pid, _s, _e) in ix.params.get(i, ())}
    return [(t, _read(path, s, e)) for (t, pid, s, e) in params_before(ix, i)
            if (t, pid) not in have]


def with_params(au: bytes, missing: list) -> bytes:
    """`au` with the `missing` parameter sets put where H.265 wants them: after an AUD, every VPS
    before every SPS before every PPS — a missing PPS after the AU's own SPS (FFmpeg drops a PPS
    whose SPS it has not seen yet), the AU's own NALs otherwise in their order."""
    if not missing:
        return au
    spans = nal_spans(au)
    lead = au[:spans[0][0]] if spans else b""
    nals = [(t, au[s:e]) for (s, _h, e, t) in spans]
    head = [x for x in nals[:1] if x[0] == AUD_NUT]
    body = nals[len(head):]
    out = [lead] + [n for _t, n in head]
    for ptype in PARAM_SETS:
        out += [n for t, n in missing if t == ptype]
        out += [n for t, n in body if t == ptype]
    out += [n for t, n in body if t not in PARAM_SETS]
    return b"".join(out)


def params_bytes(path: str, ix: Index, i: int) -> bytes:
    """The missing parameter sets of AU `i` as bytes, in VPS/SPS/PPS order (missing_params)."""
    return b"".join(n for _t, n in missing_params(path, ix, i))


def activated_ids(path: str, ix: Index, i: int) -> tuple:
    """(vps, sps, pps) ids access unit i's first slice activates."""
    import hevcids
    raw = _read(path, ix.off[i], ix.end[i])
    s_, h, e, t = next(sp for sp in nal_spans(raw) if is_vcl(sp[3]))
    rb = hevcids.unescape_map(raw[h + 2:min(e, h + 66)])[0]
    pps_id = hevcids.Bits(rb, 2 if is_irap(t) else 1).ue()
    sps_id = ix.pps[pps_id]["sps_id"]
    return ix.sps[sps_id]["vps_id"], sps_id, pps_id


def write_region(path: str, ix: Index, a: int, b: int, out_path: str) -> None:
    """AUs [a, b) of `path` as a standalone Annex-B file: its parameter sets first if AU `a`
    carries none, and without the RASL pictures of `a` when it is a CRA — a decode that starts
    there never outputs them, and skipping a multi-slice one makes FFmpeg log "PPS changed between
    slices" (measured), which the decode gates rightly take for an error."""
    end = ix.off[b] if b < len(ix) else os.path.getsize(path)
    rasl = [j for j in leading_of(ix.t, a) if ix.t[j] in RASL and j < b] \
        if ix.t[a] == CRA_NUT or ix.t[a] in BLA else []
    with open(out_path, "wb") as w:
        w.write(with_params(_read(path, ix.off[a], ix.end[a]), missing_params(path, ix, a)))
        cur = ix.end[a]
        for j in rasl:
            _copy_range(path, cur, ix.off[j], w)
            cur = ix.end[j]
        _copy_range(path, cur, end, w)


def check_piece(path: str, n: int, traits: dict) -> Index:
    """A finished piece must be n pictures of closed-GOP x265 a decoder sized by the source can
    take without a reconfiguration: the same coded and displayed size, CTB and minimum CB, bit
    depth and chroma format, POC width and decoded-picture buffer, a reorder depth inside the
    source's, no weighted prediction. -> its Index; raises ValueError otherwise."""
    px = scan(path)
    if len(px) != n:
        raise ValueError(f"piece has {len(px)} pictures, {n} expected")
    if px.t[0] not in IDR or px.hidden:
        raise ValueError("piece does not start with an IDR, or hides pictures")
    if any(px.t[i] in LEADING or px.t[i] == CRA_NUT for i in range(len(px))):
        raise ValueError("piece is not closed-GOP")
    sp = px.first_sps or {}
    want = {"width": traits.get("width"), "height": traits.get("height"),
            "display_width": traits.get("display_width"),
            "display_height": traits.get("display_height"), "ctb": traits.get("ctb"),
            "min_cb": traits.get("min_cb"), "bit_depth": traits.get("bit_depth"),
            "chroma": traits.get("chroma"), "log2_poc": traits.get("log2_poc"),
            "dpb": traits.get("dpb")}
    bad = {k: (sp.get(k), v) for k, v in want.items() if v is not None and sp.get(k) != v}
    if sp.get("reorder", 99) > int(traits.get("reorder") or 0):
        bad["reorder"] = (sp.get("reorder"), traits.get("reorder"))
    if any(p.get("weighted_pred") or p.get("weighted_bipred") for p in _pps_full(path, px)):
        bad["weighted prediction"] = (1, 0)
    vui = piece_vui(path)
    want_vui = ("bt2020", "smpte2084", "bt2020nc",
                "pc" if traits.get("range") == "full" else "tv")
    if vui[:4] != want_vui:
        bad["colour description"] = (vui[:4], want_vui)
    loc = {v: k for k, v in _CHROMA_LOC.items()}.get(traits.get("chromaloc"))
    if loc and vui[4] != loc:
        bad["chroma siting"] = (vui[4], loc)
    named = _sei_kinds(path, px, 0) & {0, 129}
    if named:
        # buffering_period (0) / active_parameter_sets (129) name SPS / VPS ids: the renumbering
        # would leave them pointing at the ORIGINAL's sets
        bad["SEI naming parameter-set ids"] = (sorted(named), [])
    if bad:
        raise ValueError("piece does not match the source: " +
                         ", ".join(f"{k} {a} vs {b}" for k, (a, b) in bad.items()))
    return px


def piece_vui(path: str, ffprobe: str = FFPROBE) -> tuple:
    """(primaries, transfer, matrix, range, chroma location) a stream's SPS says."""
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=color_primaries,color_transfer,color_space,color_range,"
                            "chroma_location", "-of", "json", path],
                           capture_output=True, text=True, timeout=120)
        st = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
    except (OSError, ValueError, subprocess.SubprocessError):
        st = {}
    return (st.get("color_primaries"), st.get("color_transfer"), st.get("color_space"),
            st.get("color_range"), st.get("chroma_location"))


def _sei_kinds(path: str, ix: Index, i: int) -> set:
    """The prefix-SEI payload types in access unit i."""
    raw = _read(path, ix.off[i], ix.end[i])
    kinds = set()
    for (s_, h, e, t) in nal_spans(raw):
        if t == PREFIX_SEI:
            try:
                kinds |= {pt for pt, _p in parse_sei(unescape(raw[h + 2:e]))}
            except ValueError:
                kinds.add(-1)
    return kinds


def _pps_full(path: str, px: Index) -> list:
    import hevcids
    out = []
    for j in sorted(px.params)[:1]:
        for (t, _pid, s, e) in px.params[j]:
            if t == PPS_NUT:
                raw = _read(path, s, e)
                sp = nal_spans(raw)[0]
                out.append(hevcids.parse_pps_full(hevcids.unescape_map(raw[sp[1] - sp[0] + 2:])[0]))
    return out


import re as _re
_EPIPE = _re.compile(r"Broken pipe|Error closing file|Error writing trailer|Error muxing")
PIECE_STALL_SECONDS = 600       # x265 that shows no progress this long is killed (x265 4.1 has been
                                # seen to hang at 0% CPU on some option mixes, review + measured)


def piece_recipe(g: dict, cap_mbps: int, traits: dict) -> dict:
    """PURE. What a finished piece was made from — its sidecar: a piece is reused only when this
    matches exactly (a frame count alone would bless a piece of another range of the same length,
    review), the decode included."""
    cmd = build_piece_x265_command("x265", "-", cap_mbps, traits)
    dec = build_piece_decode_command("ffmpeg", "-", "fps", g["k"], g["n"])
    return {"W": g["W"], "E": g["E"], "k": g["k"], "n": g["n"], "ds": g["ds"], "de": g["de"],
            "cap": int(cap_mbps), "x265": hashlib.sha1(" ".join(cmd).encode()).hexdigest(),
            "decode": hashlib.sha1(" ".join(dec).encode()).hexdigest(), "v": PIECE_SETTINGS_VERSION}


def fed_path(piece: str) -> str:
    """Where the framecrc of the frames x265 was fed is kept, beside the piece."""
    return piece + ".fed"


def read_fed(piece: str) -> list:
    try:
        with open(fed_path(piece)) as f:
            return [ln.split(",")[-1].strip() for ln in f.read().splitlines()
                    if ln and not ln.startswith("#")]
    except OSError:
        return []


def piece_ok(path: str, recipe: dict, traits: dict):
    """The finished piece at `path` when its sidecar says it was made exactly so and it checks out,
    else None (and anything stale is removed)."""
    try:
        with open(path + ".json") as f:
            if json.load(f) != recipe:
                raise ValueError("another recipe")
        if len(read_fed(path)) != recipe["n"]:
            raise ValueError("no record of what x265 was fed")
        return check_piece(path, recipe["n"], traits)
    except (OSError, ValueError):
        for f in (path, path + ".json", fed_path(path)):
            dvcap._rm(f)
        return None


def _watch_piece(tmp: str, abort, last: list, done: threading.Event):
    """Kill the piece's decode|x265 pair on an abort (x265 only notices one between its own log
    lines) or when it has made no progress for PIECE_STALL_SECONDS (a suspended pair — another
    stage has the machine — is not stalled)."""
    while not done.wait(0.5):
        stop = abort is not None and abort.is_set()
        if dvcap.encoders_suspended():
            last[0] = time.monotonic()
        stalled = time.monotonic() - last[0] > PIECE_STALL_SECONDS
        if stop or stalled:
            with dvcap._ENCODER_LOCK:
                pairs = [pr for pr in dvcap._LIVE_ENCODERS if tmp in (pr[1].args or ())]
            for dec, enc in pairs:
                for proc in (dec, enc):
                    try:
                        proc.kill()
                    except OSError:
                        pass
            last[1] = "aborted" if stop else "stalled"
            return


def encode_piece(base_es: str, ix: Index, g: dict, out_hevc: str, cap_mbps: int, traits: dict,
                 fps: str, *, abort=None, on_frame=None, ffmpeg=FFMPEG, x265=X265) -> tuple:
    """Make one piece: the original AUs [W, E) -> decode -> trim to the piece's frames -> x265.
    -> (ok, reason). Atomic: the piece (and its sidecar, piece_recipe) appears at `out_hevc` only
    verified (check_piece). A decoder message (a missing reference conceals a frame) fails it —
    nothing concealed is encoded."""
    src = out_hevc + ".in.hevc"
    tmp = out_hevc + ".part"
    err = out_hevc + ".decode.log"
    for f in (src, tmp, err):
        dvcap._rm(f)
    last = [time.monotonic(), None]
    done = threading.Event()

    def tick(f):
        last[0] = time.monotonic()
        if on_frame:
            on_frame(f)
    fed = fed_path(out_hevc) + ".part"
    try:
        write_region(base_es, ix, g["W"], g["E"], src)
        dec = build_piece_decode_command(ffmpeg, src, fps, g["k"], g["n"])
        # the SAME decode's frames, hashed as x265 gets them: the content proof (check_seams)
        # compares these with the original — never a second derivation of the input (a bug in
        # this path, or a piece resumed from older code, would re-derive differently, review)
        vf = dec[dec.index("-vf") + 1]
        dec = dec + ["-map", "0:v:0", "-vf", vf, "-fps_mode", "passthrough",
                     "-pix_fmt", "yuv420p10le", "-f", "framecrc", fed]
        enc = build_piece_x265_command(x265, tmp, cap_mbps, traits)
        threading.Thread(target=_watch_piece, args=(tmp, abort, last, done), daemon=True).start()
        with open(err, "w") as ef:
            got, why, _tail = dvcap._encode_pipe(dec, enc, tmp, abort, tick, dec_stderr=ef)
        done.set()
        if why == "aborted" or last[1] == "aborted" or (abort is not None and abort.is_set()):
            return False, "aborted"
        if last[1] == "stalled":
            return False, f"x265 made no progress for {PIECE_STALL_SECONDS} s (killed)"
        msgs = ""
        if os.path.exists(err):
            with open(err, errors="replace") as ef:
                msgs = "\n".join(ln for ln in ef.read().splitlines()
                                 if ln.strip() and not _EPIPE.search(ln))
        if msgs:                         # the decoder's own complaint (a broken pipe is only its
            raise NotSpliceable(         # echo of an x265 failure, never the source's fault)
                f"decoding the original around frames {g['ds']}-{g['de'] - 1}: "
                + msgs.strip().splitlines()[-1][:200])
        if got is None:                  # x265 failed: its own words
            return False, why
        if got != g["n"]:
            return False, f"x265 encoded {got} frames, {g['n']} expected"
        try:
            check_piece(tmp, g["n"], traits)
        except ValueError as e:
            raise NotSpliceable(f"piece {g['ds']}-{g['de'] - 1}: {e}")
        with open(fed) as f:
            n_fed = sum(1 for ln in f if ln and not ln.startswith("#"))
        if n_fed != g["n"]:
            raise NotSpliceable(f"piece {g['ds']}-{g['de'] - 1}: x265 was fed {n_fed} frames")
        with open(out_hevc + ".json.tmp", "w") as f:
            json.dump(piece_recipe(g, cap_mbps, traits), f)
        os.replace(fed, fed_path(out_hevc))
        os.replace(tmp, out_hevc)
        os.replace(out_hevc + ".json.tmp", out_hevc + ".json")
        return True, "ok"
    finally:
        done.set()
        for f in (src, tmp, err, fed, out_hevc + ".json.tmp"):
            dvcap._rm(f)


# ---- writing the spliced stream ------------------------------------------------------------------

def _set_type(nal: bytes, t: int, hdr: int) -> bytes:
    """`nal` (start code included, header at `hdr`) with its NAL type set to `t`."""
    b = bytearray(nal)
    b[hdr] = (b[hdr] & 0x81) | (t << 1)
    return bytes(b)


def _clear_no_output(nal: bytes, hdr: int) -> bytes:
    """An IRAP slice segment with no_output_of_prior_pics_flag (the 2nd payload bit) cleared."""
    rb = bytearray(unescape(nal[hdr + 2:]))
    if not rb or not rb[0] & 0x40:
        return nal
    rb[0] &= ~0x40 & 0xFF
    return nal[:hdr + 2] + escape(bytes(rb))


def resume_au(base_es: str, ix: Index, R: int, bla) -> bytes:
    """Access unit R as the original resumes with it after a piece: every slice segment a BLA when
    it was a CRA (`bla` = the type), no_output_of_prior_pics_flag cleared (the piece's last frames
    are still waiting to be shown), and the original's parameter sets in front of its SEI and
    slices when it carries none (the piece's x265 overwrote ids 0)."""
    raw = with_params(_read(base_es, ix.off[R], ix.end[R]), missing_params(base_es, ix, R))
    spans = nal_spans(raw)
    out = [raw[:spans[0][0]] if spans else b""]
    for (s, h, e, t) in spans:
        nal = raw[s:e]
        hdr = h - s
        if is_vcl(t):
            if bla is not None:
                nal = _set_type(nal, bla, hdr)
            nal = _clear_no_output(nal, hdr)
        out.append(nal)
    return b"".join(out)


def _sei_messages(base_es: str, spans_of, kinds) -> list:
    """The SEI messages of `kinds` in the prefix SEI NALs at `spans_of` [(offset, length)].
    Raises ValueError on one that cannot be read (its metadata would silently go missing)."""
    msgs = []
    for (s, ln) in spans_of:
        raw = _read(base_es, s, s + ln)
        sp = nal_spans(raw)
        if not sp:
            continue
        _s, h, e, _t = sp[0]
        msgs += [(pt, p) for pt, p in parse_sei(unescape(raw[h + 2:e])) if pt in kinds]
    return msgs


def choose_piece_ids(ix: Index) -> tuple:
    """PURE-ish. (vps, sps, pps) ids the original never defines: the pieces take them, so a remuxer
    that re-inserts the CodecPrivate's (the original's) parameter sets before every keyframe —
    Plex's transcoder does, AFTER the in-band ones — cannot hand a piece the wrong ones."""
    vps = min(set(range(16)) - set(ix.vps_ids))
    sps = min(set(range(16)) - set(ix.sps))
    pps = min(set(range(64)) - set(ix.pps))
    return vps, sps, pps


class PieceRewriter:
    """A piece's NALs with the piece ids (hevcids): VPS / SPS / PPS renumbered, every slice
    segment header re-pointed at the new PPS."""

    def __init__(self, ids: tuple):
        import hevcids
        self.h = hevcids
        self.vps, self.sps, self.pps = ids
        self.sps_full, self.pps_full = {}, {}

    def nal(self, nal: bytes, hdr: int, t: int) -> bytes:
        h = self.h
        head, pay = nal[:hdr + 2], nal[hdr + 2:]
        if t == VPS_NUT:
            return head + h.renumber_vps(pay, self.vps)
        if t == SPS_NUT:
            f = h.parse_sps_full(h.unescape_map(pay)[0])
            self.sps_full[f["sps_id"]] = f
            return head + h.renumber_sps(pay, self.vps, self.sps)
        if t == PPS_NUT:
            f = h.parse_pps_full(h.unescape_map(pay)[0])
            self.pps_full[f["pps_id"]] = f
            return head + h.renumber_pps(pay, self.pps, self.sps)
        if is_vcl(t):
            rb = h.unescape_map(pay[:64])[0]
            pid = h.Bits(rb, 2 if is_irap(t) else 1).ue()
            pf = self.pps_full[pid]
            return head + h.renumber_slice(pay, t, pf, self.sps_full[pf["sps_id"]], self.pps)
        return nal

    def au(self, au: bytes) -> bytes:
        spans = nal_spans(au)
        if not spans:
            return au
        out = [au[:spans[0][0]]]
        for (s, h, e, t) in spans:
            out.append(self.nal(au[s:e], h - s, t))
        return b"".join(out)


def _t35_messages(base_es: str, ix: Index, i: int) -> list:
    return _sei_messages(base_es, ix.t35.get(i, ()), (T35,))


class _AUReader:
    """Sequential reads of access units out of a large file, through one big buffer."""

    def __init__(self, path: str, ix: Index, block: int = 64 << 20):
        self.f, self.ix, self.block = open(path, "rb"), ix, block
        self.buf, self.at = b"", 0

    def get(self, i: int) -> bytes:
        a, b = self.ix.off[i], self.ix.end[i]
        if not (self.at <= a and b <= self.at + len(self.buf)):
            self.f.seek(a)
            self.buf, self.at = self.f.read(max(b - a, self.block)), a
            if len(self.buf) < b - a:
                raise OSError(f"{self.f.name} ended inside access unit {i}")
        return self.buf[a - self.at:b - self.at]

    def close(self):
        self.f.close()


def _first_type(au: bytes):
    p = au.find(_SC)
    return (au[p + 3] >> 1) & 0x3F if p != -1 and p + 3 < len(au) else None


def aud_nal(temporal_id: int) -> bytes:
    """PURE. An access unit delimiter (pic_type 2: any slice type may follow)."""
    return b"\x00\x00\x00\x01" + bytes([AUD_NUT << 1, temporal_id + 1, 0x50])


def rpu_nal(payload: bytes) -> bytes:
    """PURE. An RPU file entry (rpu_units) as the stream's RPU NAL: UNSPEC62, layer 0, tid 0."""
    return b"\x00\x00\x00\x01" + bytes([RPU_NUT << 1, 1]) + payload


def _wrap(au: bytes, tid: int, rpu) -> bytes:
    """An AU as it ships: an AUD first when it has none (what dovi_tool's inject adds — the master
    a SHIELD already plays), then the AU, then its own RPU as its last NAL — before a trailing
    end-of-sequence / end-of-bitstream NAL, which must stay last (H.265 7.4.2.4.4)."""
    head = b"" if _first_type(au) == AUD_NUT else aud_nal(tid)
    if rpu is None:
        return head + au
    cut = len(au)
    for (s_, _h, _e, t) in reversed(nal_spans(au)):
        if t in (EOS_NUT, EOB_NUT):
            cut = s_
        else:
            break
    return head + au[:cut] + rpu_nal(rpu) + au[cut:]


def resume_ids(base_es: str, ix: Index, R: int) -> tuple:
    """(vps, sps, pps) ids the original's access unit R activates."""
    return activated_ids(base_es, ix, R)


def decoy_sets(piece_au: bytes, ids: tuple) -> bytes:
    """The piece's own VPS / SPS / PPS (x265's, from one of its keyframes) renumbered to `ids` — the
    original's. A piece that hands over to a BLA carries them once: mkvmerge v99 does not reset
    its POC count at a BLA, only when the SPS a picture activates CHANGES (measured: with the
    pieces on their own ids, every frame of the piece's last group and of the BLA's came out of
    display order), so the original's sets coming back at the BLA must change something. Nothing
    refers to these copies — the piece's slices use the piece ids — and the original's own come
    back in-band at the resume before any of its slices."""
    import hevcids
    vps, sps, pps = ids
    out = []
    for (s, h, e, t) in nal_spans(piece_au):
        if t not in PARAM_SETS:
            continue
        nal = piece_au[s:e]
        head, pay = nal[:h - s + 2], nal[h - s + 2:]
        if t == VPS_NUT:
            out.append(head + hevcids.renumber_vps(pay, vps))
        elif t == SPS_NUT:
            out.append(head + hevcids.renumber_sps(pay, vps, sps))
        else:
            out.append(head + hevcids.renumber_pps(pay, pps, sps))
    return b"".join(out)


def active_hdr_messages(base_es: str, ix: Index, S: int) -> list:
    """The original's HDR10 static metadata (MDCV / CLL messages) in force at access unit S: the
    latest AU at or before S that carried any. [] for a source with none — a piece then carries
    none either, exactly like the frames around it."""
    import bisect
    keys = sorted(ix.hdr)
    k = bisect.bisect_right(keys, S) - 1
    if k < 0:
        return []
    return _sei_messages(base_es, ix.hdr[keys[k]], (MDCV, CLL))


def write_spliced(base_es: str, ix: Index, geoms: list, pieces: list, out_es: str, *,
                  rpus=None, ids=None, on_bytes=None) -> tuple:
    """The stream that ships: original AUs, each piece in place of the AUs it replaces (and their
    RASL), every resume point a clean splice point (resume_au), payload-4 SEI copied onto the
    re-encoded frames, and — with `rpus` (rpu_units, display order) — every AU carrying the RPU
    of ITS display slot, written here from ranks the decoder's own order was checked against
    (dovi_tool's inject assigned 85 frames of a splice the wrong one: it parses with the first
    SPS). -> (expect, origin, marks): the display rank each output AU must have (decode order),
    the original AU each is (-1 inside a piece), and each piece's (first, end) output AUs."""
    n = len(ix)
    inv_rank = array("i", [0]) * n
    for i in range(n):
        inv_rank[ix.rank[i]] = i
    expect, origin, marks = array("i"), array("i"), []
    resume_of = {g["R"]: g for g in geoms if g["R"] < n}
    total = os.path.getsize(base_es)
    rd = _AUReader(base_es, ix)
    rpu = (lambda r: rpus[r]) if rpus is not None else (lambda r: None)
    try:
        with open(out_es, "wb") as w:
            def orig(a, b):
                drop = set()
                for i in range(a, b):
                    if i in drop:
                        continue
                    g = resume_of.get(i) if i == a else None
                    if g is not None:
                        au = resume_au(base_es, ix, i, g["bla"])
                        drop = set(g["drop"])
                    else:
                        au = rd.get(i)
                    w.write(_wrap(au, ix.tid[i], rpu(ix.rank[i])))
                    expect.append(ix.rank[i])
                    origin.append(i)
                    if on_bytes and i % 512 == 0:
                        on_bytes(ix.off[i], total)

            def piece(path, px, ds, S, g, abuts):
                prd = _AUReader(path, px)
                rw = PieceRewriter(ids) if ids is not None else None
                hdr = active_hdr_messages(base_es, ix, S)
                decoy_at = None
                if rw is not None and g["bla"] is not None and g["R"] < n and not abuts:
                    decoy_at = max(j for j in range(len(px)) if px.t[j] in IDR)
                    decoy_ids = resume_ids(base_es, ix, g["R"])
                try:
                    for j in range(len(px)):
                        raw = prd.get(j)
                        au = raw
                        if rw is not None:
                            au = rw.au(au)
                            if j == decoy_at:
                                d = decoy_sets(raw, decoy_ids)
                                v = next(sp[0] for sp in nal_spans(au) if is_vcl(sp[3]) or sp[3] == PREFIX_SEI)
                                au = au[:v] + d + au[v:]
                        r = ds + px.rank[j]
                        extra = b""
                        if hdr and px.t[j] in IDR:     # every piece sequence: the original's own
                            extra += build_sei_nal(hdr, temporal_id=px.tid[j])
                        msgs = _t35_messages(base_es, ix, inv_rank[r]) if ix.t35 else []
                        if msgs:
                            extra += build_sei_nal(msgs, temporal_id=px.tid[j])
                        if extra:
                            v = next(sp[0] for sp in nal_spans(au) if is_vcl(sp[3]))
                            au = au[:v] + extra + au[v:]
                        w.write(_wrap(au, px.tid[j], rpu(r)))
                        expect.append(r)
                        origin.append(-1)
                finally:
                    prd.close()

            cursor = 0
            for k, (g, (path, px)) in enumerate(zip(geoms, pieces)):
                orig(cursor, g["S"])
                first = len(expect)
                abuts = k + 1 < len(geoms) and geoms[k + 1]["S"] == g["R"]
                piece(path, px, g["ds"], g["S"], g, abuts)
                marks.append((first, len(expect)))
                cursor = g["R"]
            orig(cursor, n)
    finally:
        rd.close()
    return list(expect), list(origin), marks


# ---- proving the seams -------------------------------------------------------------------------

def run_cmd(cmd: list, *, abort=None, timeout=None, text=True) -> tuple:
    """(returncode, stdout, stderr) of a child that dies within ~0.5 s of `abort` (its whole
    process group) and at `timeout` — a long decode or copy must never outlive a stop or a
    deploy. Raises InterruptedError on abort, TimeoutError on timeout."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         text=text, errors="replace" if text else None, start_new_session=True)
    # registered like a decode|x265 pair: AI outpainting's dvcap.suspend_encoders() SIGSTOPs it
    # too (a 4K seam decode is all cores and a GB of RAM — review), resume_encoders() wakes it
    pair = (p, p)
    with dvcap._ENCODER_LOCK:
        dvcap._LIVE_ENCODERS.add(pair)
        if dvcap._ENCODERS_SUSPENDED:
            dvcap._signal_pair(pair, signal.SIGSTOP)
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        return _wait_cmd(p, abort, deadline, cmd)
    finally:
        with dvcap._ENCODER_LOCK:
            dvcap._LIVE_ENCODERS.discard(pair)
        dvcap._signal_pair(pair, signal.SIGCONT)


def _wait_cmd(p, abort, deadline, cmd) -> tuple:
    while True:
        try:
            out, err = p.communicate(timeout=0.5)
            return p.returncode, out, err
        except subprocess.TimeoutExpired:
            stop = abort is not None and abort.is_set()
            late = deadline is not None and time.monotonic() > deadline
            if stop or late:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    p.kill()
                p.communicate()
                if stop:
                    raise InterruptedError("aborted")
                raise TimeoutError(f"{os.path.basename(cmd[0])} timed out")


def framehash(path: str, fps: str, ffmpeg: str = FFMPEG, abort=None) -> list:
    """Every decoded frame's checksum (framecrc: adler32 — md5 of a 25 MB 4K frame costs more
    than its decode), display order. Any decoder message fails it (ValueError)."""
    rc, out, err = run_cmd([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-r", str(fps),
                            "-f", "hevc", "-i", path, "-map", "0:v:0", "-fps_mode", "passthrough",
                            "-pix_fmt", "yuv420p10le", "-f", "framecrc", "-"], abort=abort)
    if rc != 0 or (err or "").strip():
        raise ValueError(f"decoding {os.path.basename(path)}: "
                         f"{((err or '').strip().splitlines() or ['rc %d' % rc])[-1][:200]}")
    return [ln.split(",")[-1].strip() for ln in (out or "").splitlines()
            if ln and not ln.startswith("#")]


framemd5 = framehash      # (the tests' name)


def _irap_after(ix: Index, i: int) -> int:
    import bisect
    ir = ix.irap_list()
    k = bisect.bisect_right(ir, i)
    return ir[k] if k < len(ir) else len(ix)


def _irap_before(ix: Index, i: int) -> int:
    import bisect
    ir = ix.irap_list()
    k = bisect.bisect_left(ir, i) - 1
    return ir[k] if k >= 0 else 0


def _hold(should_pause, abort):
    """Wait out a sibling app's lease or AI outpainting between proof steps: the tail cannot
    resume, so it waits instead of yielding."""
    while should_pause is not None and should_pause():
        if abort is not None and abort.is_set():
            raise InterruptedError("aborted")
        time.sleep(2)


def check_seams(base_es: str, ix: Index, out_es: str, ox: Index, geoms: list, pieces: list,
                origin: list, marks: list, fps: str, workdir: str, *, ffmpeg=FFMPEG,
                abort=None, should_pause=None, on_check=None) -> None:
    """Prove every piece in place, frame for frame:
      * x265 was fed EXACTLY the original's frames of its display range: the frames it was fed
        (hashed from that very decode, kept beside the piece) are hash identical to the original
        decoded from the group before the piece (counted by FFmpeg's own output, never by the
        plan's arithmetic) at ds..de-1 — a wrong trim, a wrong range of the same length, a frame
        lost or repeated cannot pass;
      * the spliced stream, decoded from the original group of pictures before the piece to the
        end of the one after it, is exactly that group as the ORIGINAL decodes it, then the piece
        as x265 wrote it (so its renumbered ids, copied SEI and RPU changed no pixel), then the
        original's next group — the original flushes every frame before the piece, the piece
        decodes clean after it, and the resume point gives back the original's own pictures.
    Two pieces that abut (split at a cut inside one long stretch) have no original group
    between them; each side is proven against the other's piece. Raises ValueError."""
    n, m = len(ix), len(ox)
    tmp = os.path.join(workdir, "seam.hevc")
    try:
        for k, (g, (piece, _px), (p0, p1)) in enumerate(zip(geoms, pieces, marks)):
            if abort is not None and abort.is_set():
                raise InterruptedError("aborted")
            _hold(should_pause, abort)
            sec0 = marks[k - 1][1] if k else 0
            sec1 = marks[k + 1][0] if k + 1 < len(marks) else m
            b_out = max((i for i in range(sec0, p0) if is_irap(ox.t[i])), default=None)
            e_out = next((i for i in range(p1 + 1, sec1) if is_irap(ox.t[i])), sec1)
            b0 = origin[b_out] if b_out is not None else g["S"]
            expect = []
            if b_out is not None:
                write_region(base_es, ix, b0, g["S"], tmp)
                expect += framehash(tmp, fps, ffmpeg, abort)
            n_before = len(expect)
            # the original, decoded through the piece's whole range: frames n_before.. are ds..
            write_region(base_es, ix, b0, g["E"], tmp)
            ref = framehash(tmp, fps, ffmpeg, abort)
            fed = read_fed(piece)
            if ref[:n_before] != expect or fed != ref[n_before:n_before + g["n"]]:
                at = next((i for i, (x, y) in enumerate(zip(fed, ref[n_before:])) if x != y),
                          min(len(fed), max(0, len(ref) - n_before)))
                raise ValueError(f"piece {g['ds']}-{g['de'] - 1} was not made from the frames it "
                                 f"replaces (input frame {at} differs; {len(fed)} fed)")
            expect += framehash(piece, fps, ffmpeg, abort)
            abuts = k + 1 < len(geoms) and geoms[k + 1]["S"] == g["R"]
            if g["R"] < n and not abuts:
                base_end = (origin[e_out] if e_out < m and origin[e_out] >= 0
                            else geoms[k + 1]["S"] if k + 1 < len(geoms) and e_out == sec1 < m
                            else n)
                write_region(base_es, ix, g["R"], base_end, tmp)
                expect += framehash(tmp, fps, ffmpeg, abort)
            write_region(out_es, ox, b_out if b_out is not None else p0,
                         p1 if abuts else e_out, tmp)
            got = framehash(tmp, fps, ffmpeg, abort)
            if got != expect:
                at = next((i for i, (x, y) in enumerate(zip(got, expect)) if x != y),
                          min(len(got), len(expect)))
                raise ValueError(f"seam around frames {g['ds']}-{g['de'] - 1} does not decode as "
                                 f"the original + the piece: frame {at} of the check differs "
                                 f"({len(got)} decoded, {len(expect)} expected)")
            if on_check:
                on_check(k + 1, len(geoms))
    finally:
        dvcap._rm(tmp)


def check_t35(base_es: str, ix: Index, out_es: str, ox: Index, marks: list) -> None:
    """Every re-encoded frame — and every resume point — carries exactly the registered user data
    (HDR10+, captions: SEI payload 4) of the original frame in its display slot. Three placement
    mutants shipped without this (review). Raises ValueError."""
    if not ix.t35:
        return
    inv = array("i", [0]) * len(ix)
    for i in range(len(ix)):
        inv[ix.rank[i]] = i
    for (p0, p1) in marks:
        for j in list(range(p0, p1)) + ([p1] if p1 < len(ox) else []):
            got = sorted((pt, bytes(pl)) for pt, pl in _sei_messages(out_es, ox.t35.get(j, ()), (T35,)))
            want = sorted((pt, bytes(pl)) for pt, pl in _t35_messages(base_es, ix, inv[ox.rank[j]]))
            if got != want:
                raise ValueError(f"access unit {j} does not carry its own frame's registered "
                                 "user data (HDR10+ / captions)")


def check_static_hdr(base_es: str, ix: Index, out_es: str, ox: Index, geoms: list,
                     marks: list) -> None:
    """Every piece keyframe carries exactly the original's HDR10 static metadata in force there
    (MDCV / CLL messages, byte for byte) — or none when the original has none: an HDR10 display
    never sees the mastering display or light level change at a seam. Raises ValueError."""
    for k, (g, (p0, p1)) in enumerate(zip(geoms, marks)):
        want = sorted((pt, bytes(pl)) for pt, pl in active_hdr_messages(base_es, ix, g["S"]))
        for j in range(p0, p1):
            if ox.t[j] not in IDR:
                continue
            got = sorted((pt, bytes(pl)) for pt, pl in
                         _sei_messages(out_es, ox.hdr.get(j, ()), (MDCV, CLL)))
            if got != want:
                raise ValueError(f"piece {k + 1}'s keyframe at access unit {j} does not carry the "
                                 "original's mastering display / light level")


def lint(out_es: str, ox: Index, geoms: list, marks: list, ids: tuple) -> None:
    """The splice points as H.265 wants them — FFmpeg forgives a resume left a CRA (it decoded one
    pixel-exact, review), a hardware decoder need not: every piece starts with an IDR carrying its
    own VPS/SPS/PPS (the piece ids) and no_output_of_prior_pics_flag 0; every resume point is an
    IDR or a BLA in EVERY slice, the flag 0 in every slice segment, with no RASL left behind it.
    Raises ValueError."""
    m = len(ox)
    for k, (p0, p1) in enumerate(marks):
        if ox.t[p0] not in IDR or ox.no_out[p0]:
            raise ValueError(f"piece {k + 1} does not start with a clean IDR")
        defined = {(t, pid) for (t, pid, _s, _e) in ox.params.get(p0, ())}
        if not {(VPS_NUT, ids[0]), (SPS_NUT, ids[1]), (PPS_NUT, ids[2])} <= defined:
            raise ValueError(f"piece {k + 1} does not carry its own parameter sets")
        if p1 >= m:
            continue
        raw = _read(out_es, ox.off[p1], ox.end[p1])
        types = set()
        for (s_, h, e, t) in nal_spans(raw):
            if not is_vcl(t):
                continue
            types.add(t)
            if t not in IDR and t not in BLA:
                raise ValueError(f"resume after piece {k + 1} has a slice of type {t}")
            if unescape(raw[h + 2:min(e, h + 8)])[0] & 0x40:
                raise ValueError(f"resume after piece {k + 1} keeps no_output_of_prior_pics_flag")
        if len(types) != 1:
            raise ValueError(f"resume after piece {k + 1} mixes slice types {sorted(types)}")
        lead = [ox.t[j] for j in leading_of(ox.t, p1)]
        if any(t in RASL for t in lead):
            raise ValueError(f"resume after piece {k + 1} still has a RASL picture")
        if ox.t[p1] == BLA_N_LP and lead:
            raise ValueError(f"resume after piece {k + 1} is a {ox.t[p1]} BLA with "
                             f"{len(lead)} leading picture(s)")
    # the piece ids' own chain: SPS -> the piece VPS, PPS -> the piece SPS
    if ox.sps.get(ids[1], {}).get("vps_id") != ids[0] or \
            ox.pps.get(ids[2], {}).get("sps_id") != ids[1]:
        raise ValueError("a piece's parameter sets point at the original's")


# ---- the whole splice ----------------------------------------------------------------------------

def container_ranks(container: str, ffprobe: str = FFPROBE, timeout=None):
    """The display order the CONTAINER's timestamps give its video packets (decode order), or
    None when unreadable — a cross-check of the bitstream's own (Index.rank)."""
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "packet=pts", "-of", "csv=p=0", container],
                           capture_output=True, text=True, timeout=timeout)
        pts = [int(x.strip().strip(",")) for x in (r.stdout or "").split() if x.strip(",").lstrip("-").isdigit()]
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not pts:
        return None
    order = sorted(range(len(pts)), key=lambda i: pts[i])
    if len(set(pts)) != len(pts):
        return None
    rank = [0] * len(pts)
    for r_, i in enumerate(order):
        rank[i] = r_
    return rank


def _piece_name(segdir: str, ds: int, de: int, cap: int) -> str:
    return os.path.join(segdir, f"piece_{ds:07d}_{de:07d}_c{int(cap)}.hevc")


def _read_book(segdir: str) -> dict:
    try:
        with open(os.path.join(segdir, "plan.json")) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_book(segdir: str, book: dict) -> None:
    tmp = os.path.join(segdir, "plan.json.tmp")
    with open(tmp, "w") as f:
        json.dump(book, f)
    os.replace(tmp, os.path.join(segdir, "plan.json"))


def predicted_sizes(ix: Index, geoms: list, pxs: list, extra_by_rank=None) -> list:
    """PURE-ish. Bytes per display rank of the stream to be shipped: the original's, each piece's
    in its range, plus `extra_by_rank` (the RPU and AUD dovi_tool adds, copied payload-4 SEI)."""
    n = len(ix)
    by_rank = [0] * n
    for i in range(n):
        by_rank[ix.rank[i]] = ix.end[i] - ix.off[i]
    for g, px in zip(geoms, pxs):
        for r in range(g["ds"], g["de"]):
            by_rank[r] = 0
        for j in range(len(px)):
            by_rank[g["ds"] + px.rank[j]] = px.end[j] - px.off[j]
    if extra_by_rank is not None:
        for r in range(n):
            by_rank[r] += extra_by_rank[r]
    return by_rank


def run(base_es: str, base_container: str, fps: str, cap_mbps: int, gate_mbps: float, segdir: str,
        out_es: str, *, rpus=None, extra_by_rank=None, expect_frames=None, abort=None,
        should_pause=None, on_step=None, on_pieces=None, log=None, ffmpeg=FFMPEG, x265=X265,
        ffprobe=FFPROBE) -> dict:
    """The splice, start to finish, onto `out_es`. Raises NotSpliceable (take the whole-file
    capped re-encode), InterruptedError (aborted / paused: every finished piece is kept in
    `segdir`, the retry resumes) or RuntimeError (a failure worth retrying as is).
    `on_pieces(done, total)` hears the pieces as they finish (deploy-now waits for one).
    -> {"expect": ranks of out_es's AUs, "pieces": k, "frames": re-encoded, "share": ...}."""
    say = log or (lambda m: None)
    step = on_step or (lambda label, pct: None)
    stop = (lambda: abort is not None and abort.is_set())
    fr = Fraction(str(fps))

    step("indexing the original video", None)
    saved = os.path.join(segdir, "base.index.json")
    ix = Index.load(saved, _identity(base_es))
    if ix is None:
        try:
            ix = scan(base_es, stop=stop,
                      on_progress=lambda d, t: step("indexing the original video", 100.0 * d / t))
        except ValueError as e:
            raise NotSpliceable(f"the stream cannot be indexed: {e}")
        os.makedirs(segdir, exist_ok=True)
        ix.save(saved, _identity(base_es))
    n = len(ix)
    if expect_frames is not None and n != int(expect_frames):
        raise RuntimeError(f"RPU/source frame mismatch: rpu {expect_frames} != {n} pictures — the "
                           "RPU donor is not frame-aligned with the shipped video (shipped nothing)")
    if extra_by_rank is not None and len(extra_by_rank) != n:
        raise RuntimeError("per-frame sizes do not match the picture count")
    # the structures this module does not splice (each one fact about the SOURCE, so a booked
    # fallback, never a retry)
    if ix.hidden:
        raise NotSpliceable(f"{len(ix.hidden)} pictures are never displayed (RASL at a sequence "
                            "start or pic_output_flag 0)")
    if ix.layered:
        raise NotSpliceable("the stream has more than one layer (MV-HEVC / SHVC)")
    if any(i < n - 1 for i in ix.eos):
        raise NotSpliceable("an end-of-sequence NAL inside the stream")
    if any(note.startswith("unreadable SEI") for note in ix.notes):
        raise NotSpliceable("an SEI message cannot be read, so it could not be carried")
    traits = source_traits(ix, base_container, ffprobe)
    if traits.get("bit_depth") != 10 or traits.get("chroma") != 1:
        raise NotSpliceable(f"not 10-bit 4:2:0 ({traits.get('bit_depth')}-bit, chroma "
                            f"{traits.get('chroma')})")
    if traits.get("field_order") not in ("", "progressive", "unknown"):
        raise NotSpliceable(f"interlaced source ({traits['field_order']})")
    if piece_structure(int(traits.get("dpb") or 0), int(traits.get("reorder") or 0),
                       int(traits.get("log2_poc") or 16)) is None:
        raise NotSpliceable(f"x265 cannot match the source's decoded-picture buffer "
                            f"({traits.get('dpb')}, reorder {traits.get('reorder')})")
    if traits.get("ctb") == 16 and int(traits.get("level") or 0) >= 150:
        raise NotSpliceable("x265 4.1 cannot encode CTU 16 at level 5 or higher (it crashes; "
                            "measured)")
    if traits.get("ctb") not in (16, 32, 64) or traits.get("min_cb") not in (8, 16, 32):
        raise NotSpliceable(f"x265 cannot match the source's CTB {traits.get('ctb')} / minimum "
                            f"CB {traits.get('min_cb')}")
    cb = traits["min_cb"]
    if (-(-traits["display_width"] // cb) * cb, -(-traits["display_height"] // cb) * cb) != \
            (traits["width"], traits["height"]):
        raise NotSpliceable(f"x265 cannot match the source's coded size {traits['width']}x"
                            f"{traits['height']}")
    cr = container_ranks(base_container, ffprobe)
    if cr is not None and len(cr) == n and list(ix.rank) != cr:
        bad = next(i for i in range(n) if ix.rank[i] != cr[i])
        raise NotSpliceable(f"the container's timestamps and the bitstream's POCs disagree on the "
                            f"display order (access unit {bad})")
    ids = choose_piece_ids(ix)
    say(f"splice: {n} frames indexed, {sum(1 for i in range(n) if is_irap(ix.t[i]))} keyframes, "
        f"container order {'matches' if cr is not None and len(cr) == n else 'unchecked'}, "
        f"pieces take parameter-set ids {ids}")

    os.makedirs(segdir, exist_ok=True)
    book = _read_book(segdir)
    # tightened caps, by DISPLAY RANGE: a piece the next round plans wider keeps the tightest cap
    # of every range it covers (an exact-span key lost the cut when a piece grew, review)
    caps = [tuple(int(x) for x in c) for c in (book.get("caps") or []) if isinstance(c, list)]
    extra = set(int(r) for r in (book.get("extra") or ()))
    starts, resumes = cut_points(ix.t, ix.rank)
    iraps = ix.irap_list()
    max_frames = int(round(float(fr) * SPLICE_MAX_PIECE_SECONDS))

    def cap_for(g):
        hit = [c for (a, b, c) in caps if a < g["de"] and g["ds"] < b]
        return min(hit) if hit else int(cap_mbps)

    sizes = predicted_sizes(ix, [], [], extra_by_rank)
    for rnd in range(1, SPLICE_ROUNDS + 1):
        spans = split_long(plan_runs(ix.t, ix.rank, sizes, fr, gate_mbps, extra_ranks=extra),
                           starts, resumes, max_frames)
        geoms = [run_geometry(ix.t, ix.rank, a, b, resumes, iraps) for a, b in spans]
        share = sum(g["n"] for g in geoms) / n
        if share > SPLICE_MAX_SHARE:
            raise NotSpliceable(f"{share:.0%} of the frames would be re-encoded")
        _write_book(segdir, {"caps": [list(c) for c in caps], "extra": sorted(extra)})
        total = sum(g["n"] for g in geoms)
        done = [0]
        pxs, pieces = [], []
        if on_pieces:
            on_pieces(0, len(geoms))
        for gi, g in enumerate(geoms):
            cap = cap_for(g)
            path = _piece_name(segdir, g["ds"], g["de"], cap)
            recipe = piece_recipe(g, cap, traits)
            px = piece_ok(path, recipe, traits) if os.path.exists(path) else None
            if px is None:
                if stop():
                    raise InterruptedError("aborted")
                if should_pause is not None and should_pause():
                    raise InterruptedError("paused: another stage has the machine — the splice "
                                           "resumes from its finished pieces")
                d0 = done[0]
                ok, why = encode_piece(
                    base_es, ix, g, path, cap, traits, fps, abort=abort, ffmpeg=ffmpeg, x265=x265,
                    on_frame=lambda f: step("re-encoding the busiest stretches",
                                            100.0 * (d0 + f) / max(1, total)))
                if why == "aborted":
                    raise InterruptedError("aborted")
                if not ok:
                    raise RuntimeError(why)
                px = check_piece(path, g["n"], traits)
            done[0] += g["n"]
            step("re-encoding the busiest stretches", 100.0 * done[0] / max(1, total))
            if on_pieces:
                on_pieces(gi + 1, len(geoms))
            pxs.append(px)
            pieces.append((path, px))
        sizes_now = predicted_sizes(ix, geoms, pxs, extra_by_rank)
        over = windows_over(sizes_now, fr, gate_mbps)
        if not over:
            break
        say(f"splice round {rnd}: {len(over)} one-second window(s) still over {gate_mbps:g} Mbps "
            f"(worst {max(m for _a, m in over):.1f}) — repairing")
        if rnd == SPLICE_ROUNDS:
            raise NotSpliceable(f"{len(over)} one-second window(s) still over the gate after "
                                f"{SPLICE_ROUNDS} rounds")
        # each piece a window touches is cut ONCE per round — by how far its worst window is over,
        # at least 20% — and the window's frames outside every piece join the next plan
        w = window_len(fr)
        owner = array("i", [-1]) * n
        for gi, g in enumerate(geoms):
            for r in range(g["ds"], g["de"]):
                owner[r] = gi
        worst = {}
        for a0, mb in over:
            for r in range(a0, min(n, a0 + w)):
                if owner[r] >= 0:
                    worst[owner[r]] = max(worst.get(owner[r], 0.0), mb)
                else:
                    extra.add(r)
        for gi, mb in worst.items():
            g = geoms[gi]
            new = max(max(1, int(cap_mbps * 0.4)),
                      int(cap_for(g) * min(0.8, float(gate_mbps) / mb)))
            caps.append((g["ds"], g["de"], new))
    say(f"splice: {len(geoms)} piece(s), {sum(g['n'] for g in geoms)} frames re-encoded "
        f"({share:.1%}); writing the spliced stream")

    step("splicing them into the original", 0.0)
    expect, origin, marks = write_spliced(
        base_es, ix, geoms, pieces, out_es, rpus=rpus, ids=ids,
        on_bytes=lambda d, t: step("splicing them into the original", min(99.0, 100.0 * d / t)))
    step("checking every splice", 0.0)
    ox = scan(out_es, stop=stop, capture_rpu=rpus is not None)
    if len(ox) != n or list(ox.rank) != list(expect) or ox.hidden:
        raise NotSpliceable("the spliced stream's display order is not the original's "
                            f"({len(ox)} of {n} frames)")
    if rpus is not None:
        bad = [j for j in range(n) if normalise_nal(ox.rpu.get(j, b"")) != rpus[ox.rank[j]]]
        if bad:
            raise NotSpliceable(f"{len(bad)} frame(s) do not carry their own RPU (first: access "
                                f"unit {bad[0]})")
        if ox.misplaced:
            raise NotSpliceable(f"access unit {ox.misplaced[0]} does not end with exactly one RPU "
                                "(and nothing after it but an end of sequence)")
    if set(ox.sps) - set(ix.sps) - {ids[1]} or set(ox.pps) - set(ix.pps) - {ids[2]}:
        raise NotSpliceable("the spliced stream defines a parameter set nobody asked for")
    for g, (p0, p1) in zip(geoms, marks):            # after every resume, the original's own sets
        if p1 < n:
            have = {(t, pid) for (t, pid, _s, _e) in ox.params.get(p1, ())}
            if not set(zip(PARAM_SETS, activated_ids(out_es, ox, p1))) <= have:
                raise NotSpliceable("a resume point does not carry every parameter set it uses")
    try:
        lint(out_es, ox, geoms, marks, ids)
        check_static_hdr(base_es, ix, out_es, ox, geoms, marks)
        check_t35(base_es, ix, out_es, ox, marks)
        check_seams(base_es, ix, out_es, ox, geoms, pieces, origin, marks, fps, segdir,
                    ffmpeg=ffmpeg, abort=abort, should_pause=should_pause,
                    on_check=lambda k, of: step("checking every splice", 100.0 * k / of))
    except ValueError as e:
        raise NotSpliceable(str(e))
    keep = {os.path.basename(p) for p, _px in pieces}
    keep |= {k + ".json" for k in keep} | {k + ".fed" for k in keep}
    for f in os.listdir(segdir):                      # superseded pieces of earlier rounds
        if f.startswith("piece_") and f not in keep:
            dvcap._rm(os.path.join(segdir, f))
    return {"expect": expect, "pieces": len(geoms), "frames": sum(g["n"] for g in geoms),
            "share": share, "geoms": geoms, "traits": traits, "index": ox, "ids": ids,
            "marks": marks}


# ---- remembering a source the splice cannot do -------------------------------------------------------
# A NotSpliceable source takes the whole-file capped re-encode, whose own resume dir (.remuxsegs)
# holds hours of segments. Trying the splice again on the next attempt would wipe nothing of that
# (separate dirs) but would cost its index-and-plan minutes every retry and could flip-flop the
# stage bar: the decision is booked per master and source identity, and a changed source (a new
# download) is tried again.
FALLBACK_FILE = os.path.expanduser("~/.topaz-pipeline/splice_fallback.json")
if "unittest" in __import__("sys").modules:      # tests never write the live book
    import tempfile as _tf
    FALLBACK_FILE = os.path.join(_tf.mkdtemp(prefix="visionary-splice-"), "splice_fallback.json")


_BOOK_LOCK = threading.Lock()


def _book_update(fn) -> None:
    """Read-modify-write FALLBACK_FILE under a lock, through a unique temp file."""
    import tempfile
    with _BOOK_LOCK:
        try:
            with open(FALLBACK_FILE) as f:
                d = json.load(f)
            if not isinstance(d, dict):
                d = {}
        except (OSError, ValueError):
            d = {}
        out = fn(d)
        os.makedirs(os.path.dirname(FALLBACK_FILE), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(FALLBACK_FILE), prefix=".splice-")
        with os.fdopen(fd, "w") as f:
            json.dump(d, f)
        os.replace(tmp, FALLBACK_FILE)
        return out


def _identity(path: str) -> str:
    try:
        st = os.stat(path)
        return f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return "missing"


def fallback_reason(output: str, base: str):
    """Why this master's splice was given up on (its source unchanged), or None."""
    try:
        with open(FALLBACK_FILE) as f:
            d = json.load(f) or {}
        e = d.get(os.path.basename(output))
        if isinstance(e, dict) and e.get("base") == _identity(base):
            return e.get("reason") or "not spliceable"
    except (OSError, ValueError, AttributeError):
        pass
    return None


def remember_fallback(output: str, base: str, reason: str) -> None:
    def put(d):
        d[os.path.basename(output)] = {"base": _identity(base), "reason": str(reason)[:300]}
    try:
        _book_update(put)
    except OSError:
        pass


SPLICE_MAX_FAILURES = 2         # the same source failing the splice this often (not a pause, not an
                                # abort) takes the whole-file encode: a deterministic fault must not
                                # park an item the capped path would have finished


def note_failure(output: str, base: str, reason: str) -> int:
    """Count one failed splice attempt of this master (its source unchanged). -> the count."""
    def bump(d):
        key = "failures:" + os.path.basename(output)
        e = d.get(key) if isinstance(d.get(key), dict) else {}
        n = (int(e.get("n") or 0) + 1) if e.get("base") == _identity(base) else 1
        d[key] = {"base": _identity(base), "n": n, "last": str(reason)[:300]}
        return n
    try:
        return _book_update(bump)
    except OSError:
        return 0


def forget(output: str) -> None:
    """A shipped splice clears its failure count."""
    try:
        _book_update(lambda d: d.pop("failures:" + os.path.basename(output), None))
    except OSError:
        pass


def _run_filter(cmd: list, src: str, dst: str, abort) -> int:
    """`cmd < src > dst` as a child that dies within ~0.5 s of `abort` (run with cwd = this
    module's folder, so `python -m nalfix` finds the engine)."""
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        p = subprocess.Popen(cmd, stdin=fi, stdout=fo, stderr=subprocess.DEVNULL,
                             cwd=os.path.dirname(os.path.abspath(__file__)), start_new_session=True)
        while True:
            try:
                return p.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                if abort is not None and abort.is_set():
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except OSError:
                        p.kill()
                    p.wait()
                    raise InterruptedError("aborted")


def rpus_from_stream(donor: str, workdir: str, *, mode=None, ffmpeg=FFMPEG, abort=None) -> list:
    """The RPU of every frame of a REAL Dolby Vision donor, in display order — read out of the
    donor's own access units by the display order its POCs give, NOT by dovi_tool extract-rpu,
    which reorders by an unsigned POC and slides every RADL group by two (review, measured
    578/600 wrong). The donor goes through nalfix first (the Profile 7 split-AU quirk hands a
    picture's EL and RPU to the next keyframe) and, for Profile 7 (`mode` 2), through dovi_tool's
    convert to 8.1, which rewrites each RPU in its own access unit. Raises ValueError."""
    es = os.path.join(workdir, "donor.hevc")
    fixed = os.path.join(workdir, "donor.fix.hevc")
    conv = os.path.join(workdir, "donor.81.hevc")
    try:
        rc, _o, err = run_cmd(dvcap.build_annexb_file_command(ffmpeg, donor, es), abort=abort)
        if rc != 0:
            raise ValueError(f"donor ES extract failed: {(err or '').strip()[-200:]}")
        rc = _run_filter([sys.executable, "-m", "nalfix"], es, fixed, abort)
        if rc != 0:
            raise ValueError("nalfix failed on the donor")
        dvcap._rm(es)
        src = fixed
        if mode == 2:
            rc, out, err = run_cmd(dvcap.build_dovi_convert_command(dvcap.DOVI_TOOL, fixed, conv),
                                   abort=abort)
            if rc != 0:
                raise ValueError(f"DV convert failed: {((err or out) or '').strip()[-200:]}")
            dvcap._rm(fixed)
            src = conv
        dx = scan(src, capture_rpu=True, stop=lambda: abort is not None and abort.is_set())
        units = [None] * len(dx)
        for i in range(len(dx)):
            r = dx.rpu.get(i)
            if r is None:
                raise ValueError(f"donor access unit {i} carries no RPU")
            units[dx.rank[i]] = normalise_nal(r)
        # a second reading of the donor's order, by its container's timestamps (one packet pass,
        # no decode); skipped only where the container counts other packets (the P7 split quirk)
        cr = container_ranks(donor)
        if cr is not None and len(cr) == len(dx):
            chk = [None] * len(dx)
            for i in range(len(dx)):
                chk[cr[i]] = normalise_nal(dx.rpu[i])
            if chk != units:
                raise ValueError("the donor's RPUs read by POC and by container timestamps "
                                 "disagree")
        return units
    finally:
        for f in (es, fixed, conv):
            dvcap._rm(f)


def hvcc_sets(extradata: bytes) -> list:
    """PURE. [(type, id, NAL bytes without start code)] of an hvcC record's parameter-set arrays."""
    out = []
    if len(extradata) < 23:
        return out
    p = 23
    for _ in range(extradata[22]):
        t = extradata[p] & 0x3F
        cnt = int.from_bytes(extradata[p + 1:p + 3], "big")
        p += 3
        for _ in range(cnt):
            ln = int.from_bytes(extradata[p:p + 2], "big")
            nal = bytes(extradata[p + 2:p + 2 + ln])
            p += 2 + ln
            if t in PARAM_SETS:
                out.append((t, param_id(t, unescape(nal[2:])), nal))
    return out


def codec_private(path: str, ffprobe: str = FFPROBE) -> bytes:
    """The video track's CodecPrivate (hvcC) of a muxed file, b"" when unreadable — or when the
    bytes read are not extradata_size long (an odd final byte prints as a lone 2-digit group:
    the first reader dropped it, review)."""
    import re
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=extradata,extradata_size", "-show_data", "-of", "default", path],
                           capture_output=True, text=True, timeout=120)
        hexs = "".join(re.findall(r"^[0-9a-f]{8}: ((?:(?:[0-9a-f]{2}){1,2} ?)+)", r.stdout or "",
                                  re.M))
        data = bytes.fromhex(hexs.replace(" ", ""))
        m = re.search(r"^extradata_size=(\d+)", r.stdout or "", re.M)
        if not m or int(m.group(1)) != len(data):
            return b""
        return data
    except (OSError, ValueError, subprocess.SubprocessError):
        return b""


def check_codec_private(extradata: bytes, out_es: str, ox: Index, ids: tuple, marks: list) -> None:
    """The master's CodecPrivate — what a player starts from, and what an old remuxer re-inserts
    before every keyframe — must hold, for the original's ids, one of the ORIGINAL's own
    definitions (never a decoy) and, for the piece ids, the pieces' own. Raises ValueError."""
    sets = hvcc_sets(extradata)
    if not sets:
        raise ValueError("the master has no parameter sets in its CodecPrivate")
    piece_ids = {(VPS_NUT, ids[0]), (SPS_NUT, ids[1]), (PPS_NUT, ids[2])}
    inside = [False] * len(ox)
    for p0, p1 in marks:
        for j in range(p0, p1):
            inside[j] = True
    want = {}
    for j in sorted(ox.params):
        for (t, pid, s_, e) in ox.params[j]:
            if inside[j] and (t, pid) not in piece_ids:
                continue                              # a decoy: the original's ids, x265's sets
            want.setdefault((t, pid), set()).add(normalise_ps(_read(out_es, s_, e)))
    for (t, pid, nal) in sets:                        # any of the stream's own definitions of the
        if nal.rstrip(b"\x00") not in want.get((t, pid), ()):   # id (mkvmerge keeps the first)
            raise ValueError(f"the master's CodecPrivate {('VPS', 'SPS', 'PPS')[t - 32]} {pid} is "
                             "not the stream's own")


def normalise_ps(nal: bytes) -> bytes:
    i = nal.find(_SC)
    return bytes(nal[i + 3:] if i != -1 else nal).rstrip(b"\x00")


# ---- placing an RPU on its own frames, without any piece (user, 2026-10-20) ---------------------
# dovi_tool 2.3.0 orders frames by an UNSIGNED POC: in a stream whose IDRs have RADL leading
# pictures (negative POCs) inject-rpu puts every GOP's RPUs two frames off (578/600 wrong,
# measured), and extract-rpu reads them back with the same mapping, so a round trip hides it. It
# also parses every slice with the stream's FIRST SPS (a spliced stream with another POC width
# came out 85 frames wrong). So every RPU this pipeline writes is written here, by the display
# order the stream's POCs give (Index.rank — checked against FFmpeg's decoder, mkvmerge and the
# container), and read back the same way.

class Unindexable(ValueError):
    """The stream is one this module cannot index (scan raised): the caller may fall back to
    dovi_tool inject-rpu, exactly the old behaviour, for this one stream."""


def write_rpu_file(units: list, path: str) -> None:
    """A dovi_tool binary RPU file (display order) from rpu_units-style entries. Atomic."""
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        for u in units:
            f.write(b"\x00\x00\x00\x01" + u)
    os.replace(tmp, path)


def place_rpus(src_es: str, rpus: list, out_es: str, *, stop=None, on_bytes=None) -> Index:
    """`src_es` with every access unit carrying its OWN display slot's RPU as its last NAL (an AUD
    added where it has none — what dovi_tool's inject adds), then PROVEN: the written stream is
    read back and every access unit's RPU compared with rpus[its display rank]. -> the written
    stream's Index. Raises Unindexable (scan could not read `src_es`) or ValueError (a frame
    count that does not match, a stream that already carries RPUs, a failed proof)."""
    try:
        ix = scan(src_es, stop=stop, capture_rpu=True)
    except ValueError as e:
        raise Unindexable(str(e))
    if len(ix) != len(rpus):
        raise ValueError(f"RPU/source frame mismatch: rpu {len(rpus)} != source {len(ix)} "
                         "pictures (shipped nothing)")
    if ix.rpu:
        raise ValueError("the stream already carries Dolby Vision RPUs")
    if ix.hidden:
        raise Unindexable(f"{len(ix.hidden)} pictures are never displayed")
    write_spliced(src_es, ix, [], [], out_es, rpus=rpus, on_bytes=on_bytes)
    ox = scan(out_es, stop=stop, capture_rpu=True)
    if len(ox) != len(ix) or list(ox.rank) != list(ix.rank):
        raise ValueError("writing the RPUs changed the stream's pictures or their order")
    bad = [j for j in range(len(ox)) if normalise_nal(ox.rpu.get(j, b"")) != rpus[ox.rank[j]]]
    if bad:
        raise ValueError(f"{len(bad)} frame(s) do not carry their own RPU (first: access unit "
                         f"{bad[0]})")
    if ox.misplaced:
        raise ValueError(f"access unit {ox.misplaced[0]} does not end with exactly one RPU")
    return ox


def donor_rpus(dv_video: str, workdir: str, *, book=None, in_stream=False, mode=None,
               cache=None, ffmpeg=FFMPEG, abort=None) -> tuple:
    """(units, how): the RPU of every frame of the item, display order, from the most trustworthy
    place there is:
      * `book` — the native render's own generated RPU (<render>.dvrender/rpu.bin, sha-checked by
        the caller): what the analysis wrote, whatever any stream does with it;
      * `in_stream` — a REAL Dolby Vision donor (a combine's companion, an inline-DV winner): read
        out of its access units by their POCs (rpus_from_stream; nalfix, mode-2 convert for P7);
      * otherwise a render with no books: dovi_tool extract-rpu (cached at `cache`), accepted only
        when the POC-order read of the same render AGREES — a render whose RPUs went in by
        dovi_tool (before 2026-10-20) and one whose RPUs went in here differ exactly on RADL
        sources, and with the books gone nothing can say which it is. Raises ValueError."""
    if book:
        units = rpu_units(book)
        if not units:
            raise ValueError("the render's RPU book is empty")
        return units, "the render's own RPU"
    if in_stream:
        return rpus_from_stream(dv_video, workdir, mode=mode, ffmpeg=ffmpeg, abort=abort), \
            "read from the donor by its own frame order"
    cache = cache or os.path.join(workdir, "rpu.bin")
    if not (os.path.exists(cache) and os.path.getsize(cache) > 0):
        ok, why = dvcap.extract_rpu(dv_video, cache, mode=mode, ffmpeg=ffmpeg)
        if not ok:
            raise ValueError(why)
    check_extract_order(dv_video, cache, workdir, mode=mode, ffmpeg=ffmpeg, abort=abort)
    return rpu_units(cache), "extracted from the render"


def check_extract_order(dv_video: str, rpu_file: str, workdir: str, *, mode=None, ffmpeg=FFMPEG,
                        abort=None) -> None:
    """A render's RPU as dovi_tool extracted it (`rpu_file`) must equal the same render read by
    its own frame order: a render whose RPUs went in by dovi_tool (before 2026-10-20) and one
    whose RPUs went in by place_rpus read differently exactly on RADL sources, and with the books
    gone nothing else can say which it is. Raises ValueError (the RPU file is removed) on a
    disagreement. A `dv_video` that is not a file has nothing to compare (the extract failed
    already, or a test's stand-in)."""
    if not os.path.isfile(dv_video):
        return
    by_poc = rpus_from_stream(dv_video, workdir, mode=mode, ffmpeg=ffmpeg, abort=abort)
    if rpu_units(rpu_file) != by_poc:
        dvcap._rm(rpu_file)
        raise ValueError("the render's RPU order cannot be told (no books, and its RPUs read "
                         "differently by dovi_tool and by frame order — a RADL source): it must "
                         "render again")


def _cli(argv=None) -> int:
    """python3 -m splice inject SRC_ES RPU_FILE OUT_ES — place_rpus as a child (the native render
    runs its finish steps niced, killable and CPU-accounted). A stream this module cannot index
    takes dovi_tool inject-rpu, the old way, with a note on stderr. Exit 0 on success."""
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 4 or args[0] != "inject":
        print("usage: python3 -m splice inject SRC_ES RPU_FILE OUT_ES", file=sys.stderr)
        return 2
    _cmd, src, rpu, out = args
    try:
        ox = place_rpus(src, rpu_units(rpu), out)
        print(f"splice inject: {len(ox)} frames, every RPU on its own frame", file=sys.stderr)
        return 0
    except Unindexable as e:
        print(f"splice inject: cannot index the stream ({e}) — dovi_tool inject-rpu instead",
              file=sys.stderr)
        r = subprocess.run(dvcap.build_inject_command(dvcap.DOVI_TOOL, src, rpu, out),
                           capture_output=True, text=True)
        sys.stderr.write(r.stderr or "")
        return r.returncode
    except (OSError, ValueError) as e:
        print(f"splice inject failed: {e}", file=sys.stderr)
        return 1


def rpu_units(rpu_path: str) -> list:
    """The RPU file's entries in display order (dovi_tool's binary RPU: one per frame, each after a
    4-byte start code, WITHOUT the 2-byte NAL header the stream's RPU NAL carries), normalised like
    a stream's (normalise_nal)."""
    with open(rpu_path, "rb") as f:
        data = f.read()
    return [normalise_nal(data[s:e]) for (s, _h, e, _t) in nal_spans(data)]


def normalise_nal(nal: bytes) -> bytes:
    """PURE. An RPU as both places hold it: no start code, no RPU NAL header (0x7C01), no trailing
    zero bytes — the payload that must match frame for frame."""
    i = nal.find(_SC)
    body = bytes(nal[i + 3:] if i != -1 else nal)
    if len(body) >= 2 and ((body[0] >> 1) & 0x3F) == RPU_NUT:
        body = body[2:]
    return body.rstrip(b"\x00")


BASE_ES_BSF = "hevc_mp4toannexb,filter_units=remove_types=62|63"


def build_base_es_command(ffmpeg: str, src: str, out_hevc: str) -> list:
    """PURE. The source's video as an Annex-B ES, its own bits, WITHOUT any Dolby Vision NALs (an
    inline RPU or Profile 7 enhancement layer of a combine winner: the item's one RPU is injected
    over the whole spliced stream instead)."""
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", src,
            "-map", "0:v:0", "-c:v", "copy", "-bsf:v", BASE_ES_BSF, "-f", "hevc", out_hevc]


if __name__ == "__main__":
    raise SystemExit(_cli())
