"""Give an x265 piece its own VPS / SPS / PPS ids, so nothing can mistake the original's parameter
sets for the piece's (splice.py; design review 2026-10-20).

WHY: a spliced stream carries two sets of parameter sets — the original's and x265's — and both
encoders number theirs 0. In-band that is legal (a new IRAP re-activates), and FFmpeg 8, mkvmerge
and AVFoundation decode it exactly. But an older remuxer — Plex's own transcoder (an FFmpeg fork
from 2022, measured on this Mac) on every Direct Stream — re-inserts the Matroska CodecPrivate's
sets (the ORIGINAL's) right before each keyframe's slices, AFTER the in-band ones, so they win: the
piece's pictures decoded with the original's SPS/PPS, and every piece came out missing or corrupt.
With ids the original never uses, those re-inserted sets are inert.

HOW: the VPS id is a fixed 4-bit field; the SPS id and the PPS's own and SPS ids are Exp-Golomb
fields early in their RBSP (re-serialised: the bits before, the new codes, the bits after). A slice
references its PPS by an Exp-Golomb code near the start of its header, and a longer code shifts
everything after it — so the WHOLE slice segment header is parsed (H.265 7.3.6.1, including the
reference picture sets, entry points and extension) to find where it ends (byte_alignment()).
The header is rebuilt bit for bit with the new id and its alignment re-padded; the slice data
after it is copied byte for byte, emulation-prevention bytes and all, so the entry-point offsets
(counted from the data's first byte) stay true. The parse is self-checking: the header must end in
exactly a 1 bit then zero bits up to the byte boundary, or the piece is refused — and splice.py's
seam proof decodes every rewritten piece against the untouched x265 piece, pixel for pixel.
Only x265 output is ever rewritten; weighted prediction is turned off for the pieces, and a
pred_weight_table is refused rather than parsed.
"""
from __future__ import annotations


class Bits:
    """Big-endian bit reader over RBSP bytes, with Exp-Golomb, that knows where it is."""

    def __init__(self, data: bytes, pos: int = 0):
        self.d, self.p = data, pos

    def u(self, n: int) -> int:
        v = 0
        for _ in range(n):
            i = self.p >> 3
            if i >= len(self.d):
                raise ValueError("read past the end of the RBSP")
            v = (v << 1) | ((self.d[i] >> (7 - (self.p & 7))) & 1)
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


class Writer:
    """Big-endian bit writer."""

    def __init__(self):
        self.v, self.n = 0, 0

    def put(self, value: int, nbits: int):
        if nbits:
            self.v = (self.v << nbits) | (value & ((1 << nbits) - 1))
            self.n += nbits

    def ue(self, value: int):
        x = value + 1
        k = x.bit_length() - 1
        self.put(0, k)
        self.put(x, k + 1)

    def copy(self, data: bytes, a: int, b: int):
        """Bits [a, b) of `data`."""
        r = Bits(data, a)
        left = b - a
        while left > 0:
            k = min(32, left)
            self.put(r.u(k), k)
            left -= k

    def trailing(self):
        """rbsp_trailing_bits / byte_alignment: a 1, then zeros to the byte boundary."""
        self.put(1, 1)
        self.put(0, (-self.n) % 8)

    def bytes(self) -> bytes:
        if self.n % 8:
            raise ValueError("not byte aligned")
        return self.v.to_bytes(self.n // 8, "big") if self.n else b""


def ue_len(value: int) -> int:
    return 2 * (value + 1).bit_length() - 1


def stop_bit(rbsp: bytes) -> int:
    """Bit position of rbsp_stop_one_bit: the last 1 bit of the RBSP."""
    i = len(rbsp) - 1
    while i >= 0 and rbsp[i] == 0:
        i -= 1
    if i < 0:
        raise ValueError("RBSP has no stop bit")
    b = rbsp[i]
    k = 0
    while not (b >> k) & 1:
        k += 1
    return i * 8 + (7 - k)


# ---- SPS ------------------------------------------------------------------------------------------

def _ptl(b: Bits, max_sub_layers_minus1: int):
    b.u(2); b.u(1); b.u(5); b.u(32); b.u(4); b.u(43); b.u(1); b.u(8)
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


def _scaling_list_data(b: Bits):
    for size_id in range(4):
        for _matrix in range(0, 6, 3 if size_id == 3 else 1):
            if not b.u(1):                       # scaling_list_pred_mode_flag
                b.ue()                           # scaling_list_pred_matrix_id_delta
            else:
                coef = min(64, 1 << (4 + (size_id << 1)))
                if size_id > 1:
                    b.se()
                for _ in range(coef):
                    b.se()


def st_ref_pic_set(b: Bits, idx: int, num_sets: int, sets: list) -> dict:
    """H.265 7.3.7 + 7.4.8: one short-term RPS -> {s0: [(delta, used)], s1: [(delta, used)]}."""
    inter = b.u(1) if idx != 0 else 0
    if inter:
        delta_idx = b.ue() + 1 if idx == num_sets else 1
        ref = sets[idx - delta_idx]
        sign = b.u(1)
        delta_rps = (1 - 2 * sign) * (b.ue() + 1)
        n_ref = len(ref["s0"]) + len(ref["s1"])
        used, use_delta = [], []
        for _ in range(n_ref + 1):
            u = b.u(1)
            used.append(u)
            use_delta.append(b.u(1) if not u else 1)
        s0, s1 = [], []
        n0, n1 = len(ref["s0"]), len(ref["s1"])
        for j in range(n1 - 1, -1, -1):
            d = ref["s1"][j][0] + delta_rps
            if d < 0 and use_delta[n0 + j]:
                s0.append((d, used[n0 + j]))
        if delta_rps < 0 and use_delta[n_ref]:
            s0.append((delta_rps, used[n_ref]))
        for j in range(n0):
            d = ref["s0"][j][0] + delta_rps
            if d < 0 and use_delta[j]:
                s0.append((d, used[j]))
        for j in range(n0 - 1, -1, -1):
            d = ref["s0"][j][0] + delta_rps
            if d > 0 and use_delta[j]:
                s1.append((d, used[j]))
        if delta_rps > 0 and use_delta[n_ref]:
            s1.append((delta_rps, used[n_ref]))
        for j in range(n1):
            d = ref["s1"][j][0] + delta_rps
            if d > 0 and use_delta[n0 + j]:
                s1.append((d, used[n0 + j]))
        return {"s0": s0, "s1": s1}
    neg, pos = b.ue(), b.ue()
    s0, s1, acc = [], [], 0
    for _ in range(neg):
        acc -= b.ue() + 1
        s0.append((acc, b.u(1)))
    acc = 0
    for _ in range(pos):
        acc += b.ue() + 1
        s1.append((acc, b.u(1)))
    return {"s0": s0, "s1": s1}


def parse_sps_full(rbsp: bytes) -> dict:
    """Every SPS field a slice header depends on, plus the bit positions of the two ids."""
    b = Bits(rbsp)
    vps_id = b.u(4)
    msl = b.u(3)
    b.u(1)
    _ptl(b, msl)
    id_at = b.p
    sps_id = b.ue()
    id_end = b.p
    chroma = b.ue()
    sep = b.u(1) if chroma == 3 else 0
    w, h = b.ue(), b.ue()
    if b.u(1):
        b.ue(); b.ue(); b.ue(); b.ue()
    b.ue(); b.ue()                               # bit depths
    log2_poc = b.ue() + 4
    present = b.u(1)
    for _ in range(0 if present else msl, msl + 1):
        b.ue(); b.ue(); b.ue()
    log2_min_cb = b.ue() + 3
    log2_ctb = log2_min_cb + b.ue()
    b.ue(); b.ue(); b.ue(); b.ue()               # transform sizes and depths
    if b.u(1) and b.u(1):                        # scaling_list_enabled, sps_scaling_list_data_present
        _scaling_list_data(b)
    b.u(1)                                       # amp
    sao = b.u(1)
    if b.u(1):                                   # pcm
        b.u(4); b.u(4); b.ue(); b.ue(); b.u(1)
    n_sets = b.ue()
    sets = []
    for i in range(n_sets):
        sets.append(st_ref_pic_set(b, i, n_sets, sets))
    lt_present = b.u(1)
    lt_sps = []
    if lt_present:
        for _ in range(b.ue()):
            lt_sps.append((b.u(log2_poc), b.u(1)))
    tmvp = b.u(1)
    ctb = 1 << log2_ctb
    pic_ctbs = (-(-w // ctb)) * (-(-h // ctb))
    return {"vps_id": vps_id, "sps_id": sps_id, "id_at": id_at, "id_end": id_end,
            "chroma_array_type": 0 if sep else chroma, "separate_planes": sep,
            "log2_poc": log2_poc, "sao": sao, "sets": sets, "lt_present": lt_present,
            "lt_sps": lt_sps, "tmvp": tmvp, "pic_ctbs": pic_ctbs, "log2_ctb": log2_ctb,
            "log2_min_cb": log2_min_cb}


# ---- PPS ------------------------------------------------------------------------------------------

def parse_pps_full(rbsp: bytes) -> dict:
    """Every PPS field a slice header depends on, plus the bit positions of the two ids."""
    b = Bits(rbsp)
    pps_id = b.ue()
    sps_at = b.p
    sps_id = b.ue()
    sps_end = b.p
    d = {"pps_id": pps_id, "sps_id": sps_id, "sps_at": sps_at, "sps_end": sps_end}
    d["dependent_slices"] = b.u(1)
    d["output_flag_present"] = b.u(1)
    d["extra_bits"] = b.u(3)
    b.u(1)                                       # sign_data_hiding
    d["cabac_init_present"] = b.u(1)
    d["l0_default"] = b.ue() + 1
    d["l1_default"] = b.ue() + 1
    b.se()                                       # init_qp_minus26
    b.u(1)                                       # constrained_intra_pred
    ts = b.u(1)                                  # transform_skip_enabled
    if b.u(1):                                   # cu_qp_delta_enabled
        b.ue()
    b.se(); b.se()
    d["slice_chroma_qp_offsets"] = b.u(1)
    d["weighted_pred"] = b.u(1)
    d["weighted_bipred"] = b.u(1)
    b.u(1)                                       # transquant_bypass
    d["tiles"] = b.u(1)
    d["wpp"] = b.u(1)
    if d["tiles"]:
        cols, rows = b.ue(), b.ue()
        if not b.u(1):                           # uniform_spacing
            for _ in range(cols):
                b.ue()
            for _ in range(rows):
                b.ue()
        b.u(1)
    d["lf_across_slices"] = b.u(1)
    d["deblock_override_enabled"] = 0
    d["deblock_disabled"] = 0
    if b.u(1):                                   # deblocking_filter_control_present
        d["deblock_override_enabled"] = b.u(1)
        d["deblock_disabled"] = b.u(1)
        if not d["deblock_disabled"]:
            b.se(); b.se()
    if b.u(1):                                   # pps_scaling_list_data_present
        _scaling_list_data(b)
    d["lists_modification"] = b.u(1)
    b.ue()                                       # log2_parallel_merge_level
    d["header_extension"] = b.u(1)
    d["chroma_qp_offset_list"] = 0
    if b.u(1):                                   # pps_extension_present
        rng = b.u(1)
        other = b.u(1), b.u(1), b.u(1)
        b.u(4)
        if any(other):
            raise ValueError("PPS multilayer / 3D / SCC extensions are not supported")
        if rng:
            if ts:
                b.ue()
            b.u(1)                               # cross_component_prediction
            d["chroma_qp_offset_list"] = b.u(1)
            if d["chroma_qp_offset_list"]:
                b.ue()
                for _ in range(b.ue() + 1):
                    b.se(); b.se()
            b.ue(); b.ue()
    return d


# ---- the slice segment header ----------------------------------------------------------------------

def _ceil_log2(x: int) -> int:
    return max(0, (x - 1).bit_length())


def slice_header_span(rbsp: bytes, t: int, pps: dict, sps: dict) -> dict:
    """H.265 7.3.6.1 parsed to its end. -> {"pps_at", "pps_end": the bit span of
    slice_pic_parameter_set_id, "end": the bit position where byte_alignment() starts,
    "data": the byte where the slice data starts}. Raises ValueError when the alignment is not
    exactly a 1 then zeros (a misparse never silently passes)."""
    b = Bits(rbsp)
    first = b.u(1)
    if 16 <= t <= 23:
        b.u(1)                                   # no_output_of_prior_pics_flag
    pps_at = b.p
    b.ue()
    pps_end = b.p
    dependent = 0
    if not first:
        if pps["dependent_slices"]:
            dependent = b.u(1)
        b.u(_ceil_log2(sps["pic_ctbs"]))         # slice_segment_address
    if not dependent:
        b.u(pps["extra_bits"])
        st = b.ue()                              # slice_type: 0 B, 1 P, 2 I
        if pps["output_flag_present"]:
            b.u(1)
        if sps["separate_planes"]:
            b.u(2)
        total_curr = 0
        tmvp_on = 0
        if t not in (19, 20):                    # not IDR
            b.u(sps["log2_poc"])
            if not b.u(1):                       # short_term_ref_pic_set_sps_flag
                rps = st_ref_pic_set(b, len(sps["sets"]), len(sps["sets"]), sps["sets"])
            else:
                idx = b.u(_ceil_log2(len(sps["sets"]))) if len(sps["sets"]) > 1 else 0
                rps = sps["sets"][idx]
            total_curr = sum(u for _d, u in rps["s0"]) + sum(u for _d, u in rps["s1"])
            if sps["lt_present"]:
                n_lt_sps = b.ue() if sps["lt_sps"] else 0
                n_lt = b.ue()
                for i in range(n_lt_sps + n_lt):
                    if i < n_lt_sps:
                        k = b.u(_ceil_log2(len(sps["lt_sps"]))) if len(sps["lt_sps"]) > 1 else 0
                        total_curr += sps["lt_sps"][k][1]
                    else:
                        b.u(sps["log2_poc"])
                        total_curr += b.u(1)
                    if b.u(1):                   # delta_poc_msb_present_flag
                        b.ue()
            if sps["tmvp"]:
                tmvp_on = b.u(1)
        sao_l = sao_c = 0
        if sps["sao"]:
            sao_l = b.u(1)
            if sps["chroma_array_type"] != 0:
                sao_c = b.u(1)
        if st in (0, 1):
            l0, l1 = pps["l0_default"], pps["l1_default"] if st == 0 else 0
            if b.u(1):                           # num_ref_idx_active_override_flag
                l0 = b.ue() + 1
                if st == 0:
                    l1 = b.ue() + 1
            if pps["lists_modification"] and total_curr > 1:
                bits = _ceil_log2(total_curr)
                if b.u(1):
                    for _ in range(l0):
                        b.u(bits)
                if st == 0 and b.u(1):
                    for _ in range(l1):
                        b.u(bits)
            if st == 0:
                b.u(1)                           # mvd_l1_zero_flag
            if pps["cabac_init_present"]:
                b.u(1)
            if tmvp_on:
                from_l0 = b.u(1) if st == 0 else 1
                if (from_l0 and l0 > 1) or (not from_l0 and l1 > 1):
                    b.ue()
            if (pps["weighted_pred"] and st == 1) or (pps["weighted_bipred"] and st == 0):
                raise ValueError("pred_weight_table is not supported (pieces encode --no-weightp)")
            b.ue()                               # five_minus_max_num_merge_cand
        b.se()                                   # slice_qp_delta
        if pps["slice_chroma_qp_offsets"]:
            b.se(); b.se()
        if pps["chroma_qp_offset_list"]:
            b.u(1)
        override = b.u(1) if pps["deblock_override_enabled"] else 0
        disabled = pps["deblock_disabled"]
        if override:
            disabled = b.u(1)
            if not disabled:
                b.se(); b.se()
        if pps["lf_across_slices"] and (sao_l or sao_c or not disabled):
            b.u(1)
    if pps["tiles"] or pps["wpp"]:
        n = b.ue()
        if n:
            ln = b.ue() + 1
            for _ in range(n):
                b.u(ln)
    if pps["header_extension"]:
        for _ in range(b.ue()):
            b.u(8)
    end = b.p
    if b.u(1) != 1:
        raise ValueError("slice header does not end in byte_alignment() — misparsed")
    while b.p % 8:
        if b.u(1):
            raise ValueError("slice header alignment bits are not zero — misparsed")
    return {"pps_at": pps_at, "pps_end": pps_end, "end": end, "data": b.p // 8}


# ---- rewriting ------------------------------------------------------------------------------------

def unescape_map(ebsp: bytes) -> tuple:
    """NAL payload -> (RBSP, ebsp_of): ebsp_of(k) is the payload offset of RBSP byte k."""
    out, removed, zeros = bytearray(), [], 0
    for i, x in enumerate(ebsp):
        if zeros >= 2 and x == 3:
            removed.append(i)
            zeros = 0
            continue
        out.append(x)
        zeros = zeros + 1 if x == 0 else 0

    def ebsp_of(k: int) -> int:
        j = k
        for r in removed:
            if r <= j:
                j += 1
            else:
                break
        return j
    return bytes(out), ebsp_of


def escape(rbsp: bytes) -> bytes:
    out, zeros = bytearray(), 0
    for x in rbsp:
        if zeros >= 2 and x <= 3:
            out.append(3)
            zeros = 0
        out.append(x)
        zeros = zeros + 1 if x == 0 else 0
    if zeros >= 2:
        out.append(3)
    return bytes(out)


def _rebuild(rbsp: bytes, spans: list) -> bytes:
    """`rbsp` (a whole parameter set) with each (bit_from, bit_to, new_ue) span replaced, its
    trailing bits re-made."""
    w = Writer()
    cur = 0
    for a, b_, v in sorted(spans):
        w.copy(rbsp, cur, a)
        w.ue(v)
        cur = b_
    w.copy(rbsp, cur, stop_bit(rbsp))
    w.trailing()
    return w.bytes()


def renumber_vps(payload: bytes, vps_id: int) -> bytes:
    """A VPS NAL payload (after the 2-byte header) with vps_video_parameter_set_id = vps_id: the
    top 4 bits of its first byte (emulation prevention cannot be involved: a 4-bit field of a
    byte that follows a non-zero NAL header)."""
    return bytes([(payload[0] & 0x0F) | (vps_id << 4)]) + payload[1:]


def renumber_sps(payload: bytes, vps_id: int, sps_id: int) -> bytes:
    rb, _m = unescape_map(payload)
    s = parse_sps_full(rb)
    out = _rebuild(rb, [(s["id_at"], s["id_end"], sps_id)])
    out = bytes([(out[0] & 0x0F) | (vps_id << 4)]) + out[1:]
    return escape(out)


def renumber_pps(payload: bytes, pps_id: int, sps_id: int) -> bytes:
    rb, _m = unescape_map(payload)
    p = parse_pps_full(rb)
    b = Bits(rb)
    b.ue()
    return escape(_rebuild(rb, [(0, b.p, pps_id), (p["sps_at"], p["sps_end"], sps_id)]))


def renumber_slice(payload: bytes, t: int, pps: dict, sps: dict, pps_id: int) -> bytes:
    """A slice segment NAL payload with slice_pic_parameter_set_id = pps_id: the header rebuilt
    and re-aligned, the slice data copied as it is (escaped bytes included). Only the header is
    unescaped — a prefix, widened when the parse runs past it: a 4K slice is ~250 KB, and a
    byte-by-byte pass over every piece's slice data would cost minutes."""
    cut = 512
    while True:
        rb, ebsp_of = unescape_map(payload[:cut])
        try:
            h = slice_header_span(rb, t, pps, sps)
            if h["data"] <= len(rb) - 1 or cut >= len(payload):
                break
        except ValueError as e:
            if "past the end" not in str(e) or cut >= len(payload):
                raise
        cut = min(len(payload), cut * 8)
    w = Writer()
    w.copy(rb, 0, h["pps_at"])
    w.ue(pps_id)
    w.copy(rb, h["pps_end"], h["end"])
    w.trailing()
    return escape(w.bytes()) + payload[ebsp_of(h["data"]):]
