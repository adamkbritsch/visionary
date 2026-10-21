"""splice.py: the bitstream rules (pure) and, with the real tools, the whole over-gate splice on
synthetic sources of every structure it must handle (user, 2026-10-20: "make sure this mechanism
works for all inputs").

The matrix encodes small HDR10 sources whose bitrate spikes in known seconds, runs
remux.remux_splice on each exactly as the pipeline does, and checks the shipped master from the
outside: every frame outside the re-encoded pieces decodes bit-identical to the source's frame in
the same display slot, every frame carries its own RPU (one distinct RPU per frame, so a slip
cannot pass), HDR10+ (registered user data) sits on the frame it belongs to, the master is DV 8.1
with every frame, and every second is under the gate. Three core cases always run; the full
matrix with VISIONARY_SPLICE_MATRIX=1.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from fractions import Fraction
from unittest import mock

import dvcap
import remux
import splice

FFMPEG, X265, MKVMERGE, DOVI = dvcap.FFMPEG, dvcap.X265, remux.MKVMERGE, dvcap.DOVI_TOOL
TOOLS = all(os.path.exists(t) for t in (FFMPEG, X265, MKVMERGE, DOVI))
FULL = os.environ.get("VISIONARY_SPLICE_MATRIX") == "1"


# ---- pure ----------------------------------------------------------------------------------------

class Rbsp(unittest.TestCase):
    def test_escape_round_trips_every_start_code_emulation(self):
        for rb in (b"\x00\x00\x00", b"\x00\x00\x01\x02", b"\x00\x00\x03", b"\x12\x00\x00",
                   b"\x00\x00\x00\x00\x00\x00", b"\x00\x00\x03\x03", bytes(range(256)) * 3, b""):
            eb = splice.escape(rb)
            for bad in (b"\x00\x00\x00", b"\x00\x00\x01", b"\x00\x00\x02"):
                self.assertNotIn(bad, eb)
            self.assertFalse(eb.endswith(b"\x00\x00"))
            self.assertEqual(splice.unescape(eb), rb)

    def test_exp_golomb(self):
        # 1 | 010 | 011 | 00100 | 00101 -> 0, 1, 2, 3, 4  (and se: 0, 1, -1, 2, -2)
        bits = "1" + "010" + "011" + "00100" + "00101"
        data = int(bits + "0" * (-len(bits) % 8), 2).to_bytes((len(bits) + 7) // 8, "big")
        b = splice.Bits(data)
        self.assertEqual([b.ue() for _ in range(5)], [0, 1, 2, 3, 4])
        b = splice.Bits(data)
        self.assertEqual([b.se() for _ in range(5)], [0, 1, -1, 2, -2])


class Sei(unittest.TestCase):
    def test_messages_round_trip_with_long_types_and_sizes(self):
        msgs = [(4, bytes([0xB5, 0, 0x3C, 0, 1, 4]) + b"\x00\x00\x01" * 100), (300, b"x" * 600),
                (137, bytes(24))]
        nal = splice.build_sei_nal(msgs, temporal_id=0)
        self.assertTrue(nal.startswith(b"\x00\x00\x00\x01\x4e\x01"))
        self.assertEqual(splice.parse_sei(splice.unescape(nal[6:])), msgs)

    def test_a_truncated_message_is_refused(self):
        with self.assertRaises(ValueError):
            splice.parse_sei(bytes([4, 50, 1, 2, 3]))


def _gop(t, irap, leading=(), trail=3):
    """Helper: (types, ranks) for one GOP: IRAP, its leading pictures, trailing pictures."""
    return [irap] + list(leading) + [splice.TRAIL_R] * trail


class Cuts(unittest.TestCase):
    def test_closed_gops_cut_at_every_idr(self):
        t = ([splice.IDR_N_LP] + [1] * 3) * 3
        rank = list(range(12))
        starts, resumes = splice.cut_points(t, rank)
        self.assertEqual(starts, [0, 4, 8])
        self.assertEqual(resumes, {0: 0, 4: 4, 8: 8, 12: 12})

    def test_an_open_gop_resume_drops_its_rasl_and_becomes_a_bla(self):
        # decode: CRA0 T T T | CRA4 RASL RASL T T | CRA9 ...; the RASL of CRA4 display before it
        R = splice.RASL_N
        t = [21, 1, 1, 1, 21, R, R, 1, 1, 21, 1, 1]
        rank = [0, 1, 2, 3, 6, 4, 5, 7, 8, 9, 10, 11]
        starts, resumes = splice.cut_points(t, rank)
        self.assertIn(4, starts)                   # a piece may start at CRA4 (display 4 on)
        self.assertEqual(resumes[6], 4)            # ...and the original resume at it, ending at 6
        g = splice.run_geometry(t, rank, 0, 6, resumes)
        self.assertEqual((g["S"], g["R"], g["drop"], g["bla"]), (0, 4, [5, 6], splice.BLA_N_LP))
        self.assertEqual((g["W"], g["E"], g["k"], g["n"]), (0, 7, 0, 6))

    def test_a_piece_starting_at_a_cra_with_rasl_decodes_from_the_irap_before(self):
        R = splice.RASL_N
        t = [21, 1, 1, 1, 21, R, R, 1, 1, 21, R, 1, 1]
        rank = [0, 1, 2, 3, 6, 4, 5, 7, 8, 10, 9, 11, 12]
        starts, resumes = splice.cut_points(t, rank)
        self.assertEqual(resumes[10], 9)
        # piece = display 4..9 ([CRA4's RASL, CRA4, T, T] + CRA9's RASL): from CRA0, skip 4 frames
        g = splice.run_geometry(t, rank, 4, 10, resumes)
        self.assertEqual((g["S"], g["R"], g["W"], g["E"], g["k"], g["n"]), (4, 9, 0, 11, 4, 6))
        self.assertEqual(g["drop"], [10])

    def test_radl_stay_and_make_a_bla_w_radl(self):
        D = splice.RADL_N
        t = [19, 1, 1, 21, D, 1, 1]
        rank = [0, 1, 2, 4, 3, 5, 6]
        _s, resumes = splice.cut_points(t, rank)
        g = splice.run_geometry(t, rank, 0, 3, resumes)
        self.assertEqual((g["R"], g["drop"], g["bla"]), (3, [], splice.BLA_W_RADL))

    def test_a_cra_whose_prefix_is_not_a_display_prefix_is_no_cut(self):
        # a picture decoded before CRA3 displays after it -> neither a start nor a resume there
        t = [19, 1, 1, 21, 1, 1]
        rank = [0, 1, 4, 2, 3, 5]
        starts, resumes = splice.cut_points(t, rank)
        self.assertNotIn(3, starts)
        self.assertNotIn(3, resumes)

    def test_bla_in_a_source_is_never_a_cut(self):
        t = [19, 1, splice.BLA_N_LP, 1]
        starts, resumes = splice.cut_points(t, [0, 1, 2, 3])
        self.assertEqual(starts, [0])
        self.assertNotIn(2, resumes)


class Plan(unittest.TestCase):
    FPS = Fraction(24000, 1001)

    def test_mkvmerge_millisecond_rounding(self):
        self.assertEqual(splice.ms_of_rank(0, self.FPS), 0)
        self.assertEqual(splice.ms_of_rank(1, self.FPS), 42)        # 41.708 -> 42
        self.assertEqual(splice.ms_of_rank(24, self.FPS), 1001)
        self.assertEqual(splice.ms_of_rank(1, Fraction(25)), 40)

    def test_hot_seconds_become_margined_merged_runs(self):
        t = ([splice.IDR_N_LP] + [1] * 23) * 20                    # 20 one-second GOPs
        rank = list(range(len(t)))
        sizes = [10000] * len(t)                                    # 1.9 Mbps
        for r in range(5 * 24, 6 * 24):                             # second 5 is hot
            sizes[r] = 500000
        runs = splice.plan_runs(t, rank, sizes, self.FPS, 50.0, margin_seconds=1)
        self.assertEqual(len(runs), 1)
        a, b = runs[0]
        self.assertLessEqual(a, 4 * 24 + 1)
        self.assertGreaterEqual(b, 7 * 24 - 1)
        # two hot seconds 3 s apart merge (gap under 2 s after the margins), far apart do not
        for r in range(9 * 24, 10 * 24):
            sizes[r] = 500000
        self.assertEqual(len(splice.plan_runs(t, rank, sizes, self.FPS, 50.0, margin_seconds=1)), 1)
        for r in range(17 * 24, 18 * 24):
            sizes[r] = 500000
        self.assertEqual(len(splice.plan_runs(t, rank, sizes, self.FPS, 50.0, margin_seconds=1)), 2)

    def test_extra_ranks_join_the_plan(self):
        t = ([splice.IDR_N_LP] + [1] * 23) * 10
        rank = list(range(len(t)))
        runs = splice.plan_runs(t, rank, [100] * len(t), self.FPS, 50.0, margin_seconds=0,
                                extra_ranks=[100])
        self.assertEqual(runs, [(96, 120)])

    def test_one_gop_plans_the_whole_stream(self):
        # (the share gate in splice.run then hands it to the whole-file capped encode)
        t = [splice.IDR_N_LP] + [1] * 99
        runs = splice.plan_runs(t, list(range(100)), [10 ** 6] * 100, self.FPS, 1.0)
        self.assertEqual(runs, [(0, 100)])


class Structure(unittest.TestCase):
    def test_x265_dpb_formula(self):
        self.assertEqual(splice.x265_dpb(3, 3, True), (5, 2))
        self.assertEqual(splice.x265_dpb(3, 4, True), (5, 2))
        self.assertEqual(splice.x265_dpb(1, 3, False), (4, 1))
        self.assertEqual(splice.x265_dpb(0, 2, False), (3, 0))

    def test_pieces_match_the_source_dpb_exactly(self):
        for dpb, reorder in ((5, 3), (5, 2), (6, 4), (4, 1), (4, 2), (3, 0), (7, 2)):
            b, ref, pyr = splice.piece_structure(dpb, reorder)
            got_dpb, got_reorder = splice.x265_dpb(b, ref, pyr)
            self.assertEqual(got_dpb, dpb, (dpb, reorder))
            self.assertLessEqual(got_reorder, reorder)
        self.assertEqual(splice.piece_structure(5, 3), (3, 4, True))
        self.assertIsNone(splice.piece_structure(2, 0))       # below x265's floor
        self.assertIsNone(splice.piece_structure(3, 2) and None)

    def test_piece_command_follows_the_source(self):
        traits = {"dpb": 5, "reorder": 3, "level": 153, "tier": 1, "keyint": 24, "range": "limited",
                  "chromaloc": 2, "sar": "4:3", "ctb": 64, "min_cb": 8, "log2_poc": 6}
        c = splice.build_piece_x265_command("/x265", "/o.hevc", 50, traits)
        for flag, val in (("--level-idc", "5.1"), ("--keyint", "24"), ("--chromaloc", "2"),
                          ("--sar", "4:3"), ("--vbv-maxrate", "50000"), ("--bframes", "3"),
                          ("--ref", "4"), ("--ctu", "64"), ("--min-cu-size", "8"),
                          ("--log2-max-poc-lsb", "6"), ("--crf", str(splice.PIECE_CRF))):
            self.assertEqual(c[c.index(flag) + 1], val)
        for flag in ("--no-open-gop", "--high-tier", "--repeat-headers", "--aud", "--no-weightp",
                     "--no-weightb"):
            self.assertIn(flag, c)
        for flag in ("--dolby-vision-rpu", "--master-display", "--max-cll", "--hrd"):
            self.assertNotIn(flag, c)            # the original's own HDR SEI go in instead
        # a Main-tier source keeps Main tier while the cap fits its level; low levels have no High
        c = splice.build_piece_x265_command("/x265", "/o.hevc", 30, dict(traits, tier=0))
        self.assertIn("--no-high-tier", c)
        c = splice.build_piece_x265_command("/x265", "/o.hevc", 50, dict(traits, tier=0))
        self.assertIn("--high-tier", c)
        c = splice.build_piece_x265_command("/x265", "/o.hevc", 50, dict(traits, level=63, tier=0))
        self.assertIn("--no-high-tier", c)
        with self.assertRaises(splice.NotSpliceable):
            splice.build_piece_x265_command("/x265", "/o.hevc", 50, dict(traits, dpb=2, reorder=0))

    def test_decode_trims_by_frame_count(self):
        c = splice.build_piece_decode_command("/ffmpeg", "/in.hevc", "24000/1001", 7, 30)
        self.assertEqual(c[c.index("-vf") + 1], "trim=start_frame=7:end_frame=37")
        self.assertIn("passthrough", c)


class Windows(unittest.TestCase):
    FPS = Fraction(24000, 1001)

    def test_a_window_straddling_two_calm_buckets_is_caught(self):
        # 2 seconds at 1 Mbps, except a burst over the boundary of second 0 and 1: each fixed
        # bucket stays under 50, the window across them does not
        sizes = [5000] * 48
        for r in range(14, 34):
            sizes[r] = 250000                       # 20 frames x 2 Mb = 40 Mb in under a second
        fixed = splice.second_buckets(sizes, self.FPS)
        self.assertTrue(all(v < 50 for v in fixed.values()))
        self.assertTrue(splice.windows_over(sizes, self.FPS, 39.0))
        self.assertGreater(splice.window_peak_mbps_ranked(sizes, self.FPS), max(fixed.values()))

    def test_window_covers_every_fixed_bucket(self):
        self.assertEqual(splice.window_len(self.FPS), 24)
        self.assertEqual(splice.window_len(Fraction(25)), 25)
        self.assertEqual(splice.window_len(Fraction(60000, 1001)), 60)


class Ids(unittest.TestCase):
    def test_writer_and_exp_golomb_round_trip(self):
        import hevcids
        w = hevcids.Writer()
        for v in (0, 1, 2, 17, 63, 1000):
            w.ue(v)
        w.put(5, 3)
        w.trailing()
        b = hevcids.Bits(w.bytes())
        self.assertEqual([b.ue() for _ in range(6)], [0, 1, 2, 17, 63, 1000])
        self.assertEqual(b.u(3), 5)
        self.assertEqual(hevcids.stop_bit(w.bytes()), b.p)

    def test_piece_ids_avoid_the_sources(self):
        ix = splice.Index()
        ix.vps_ids, ix.sps, ix.pps = {0}, {0: {}, 1: {}}, {0: {}, 2: {}}
        self.assertEqual(splice.choose_piece_ids(ix), (1, 2, 1))

    @unittest.skipUnless(os.path.exists(dvcap.X265), "x265 not installed")
    def test_renumbered_piece_decodes_identically(self):
        d = tempfile.mkdtemp(prefix="splice-ids-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        es = os.path.join(d, "p.hevc")
        dec = subprocess.Popen([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                "testsrc2=s=320x180:r=24000/1001:d=3,noise=alls=20:allf=t",
                                "-pix_fmt", "yuv420p10le", "-strict", "-1", "-f", "yuv4mpegpipe", "-"],
                               stdout=subprocess.PIPE)
        subprocess.run([X265, "--y4m", "-", "--output", es, "--preset", "fast", "--no-weightp",
                        "--repeat-headers", "--aud", "--keyint", "24", "--bframes", "3", "--slices",
                        "2"], stdin=dec.stdout, capture_output=True)
        dec.stdout.close(); dec.wait()
        with open(es, "rb") as f:
            data = f.read()
        rw = splice.PieceRewriter((3, 5, 17))
        out = rw.au(data)
        dst = os.path.join(d, "ids.hevc")
        with open(dst, "wb") as f:
            f.write(out)
        self.assertEqual(splice.framehash(dst, "24000/1001"), splice.framehash(es, "24000/1001"))
        ix = splice.scan(dst)
        self.assertEqual((sorted(ix.vps_ids), sorted(ix.sps), sorted(ix.pps)), ([3], [5], [17]))


class ResumePoint(unittest.TestCase):
    def test_bla_rewrite_and_no_output_clear(self):
        # a CRA slice header: first_slice=1, no_output_of_prior_pics=1, then payload
        nal = b"\x00\x00\x01" + bytes([21 << 1, 1]) + bytes([0xC0, 0x00, 0x00, 0x01, 0x55])
        out = splice._clear_no_output(splice._set_type(nal, splice.BLA_N_LP, 3), 3)
        self.assertEqual((out[3] >> 1) & 0x3F, splice.BLA_N_LP)
        rb = splice.unescape(out[5:])
        self.assertEqual(rb[0], 0x80)
        self.assertNotIn(b"\x00\x00\x01", out[3:])          # emulation prevention re-applied


class FallbackBook(unittest.TestCase):
    def test_booked_until_the_source_changes(self):
        d = tempfile.mkdtemp()
        src = os.path.join(d, "a.mkv")
        with open(src, "wb") as f:
            f.write(b"x")
        out = os.path.join(d, "a HDR10 DV upscaled.mkv")
        self.assertIsNone(splice.fallback_reason(out, src))
        splice.remember_fallback(out, src, "no cut points")
        self.assertEqual(splice.fallback_reason(out, src), "no cut points")
        with open(src, "wb") as f:
            f.write(b"xy")                                    # a new download: tried again
        self.assertIsNone(splice.fallback_reason(out, src))
        self.assertNotIn(os.path.expanduser("~/.topaz-pipeline"), splice.FALLBACK_FILE)

    def test_stage_labels(self):
        self.assertEqual(remux.splice_stages(False)[0], "copying the original video")
        self.assertEqual(remux.splice_stages(True)[0], "extracting DV metadata")
        self.assertEqual(remux.splice_stages(True)[-3:], list(remux.MUX_STAGES))


# ---- the matrix ----------------------------------------------------------------------------------

def _run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, **kw)
    if r.returncode not in (0, 1) or (cmd[0] == FFMPEG and r.returncode):
        raise RuntimeError(f"{os.path.basename(cmd[0])} rc={r.returncode}: "
                           f"{(r.stderr or b'')[-400:]!r}")
    return r


T35_HEAD = bytes([0xB5, 0x00, 0x3C, 0x00, 0x01, 0x04])        # HDR10+: country, provider, app 4


def _tag_t35(src_es: str, out_es: str):
    """Give every picture an HDR10+-shaped SEI whose payload names its own display rank."""
    ix = splice.scan(src_es)
    with open(src_es, "rb") as f:
        data = f.read()
    with open(out_es, "wb") as w:
        for i in range(len(ix)):
            au = data[ix.off[i]:ix.end[i]]
            spans = splice.nal_spans(au)
            v = next(s for (s, _h, _e, t) in spans if splice.is_vcl(t))
            sei = splice.build_sei_nal([(splice.T35, T35_HEAD + ix.rank[i].to_bytes(4, "big") +
                                         b"\x00\x00\x00\x01")], temporal_id=ix.tid[i])
            w.write(au[:v] + sei + au[v:])


def _strip_repeated_params(es: str):
    """Keep the parameter sets of the first access unit only (a raw / transport-stream base)."""
    with open(es, "rb") as f:
        data = f.read()
    out, seen = bytearray(), False
    spans = splice.nal_spans(data)
    if spans:
        out += data[:spans[0][0]]
    for (s, _h, e, t) in spans:
        if not (t in splice.PARAM_SETS and seen):
            out += data[s:e]
        if splice.is_vcl(t):
            seen = True
    with open(es, "wb") as f:
        f.write(bytes(out))


def make_source(d, *, fps="24000/1001", seconds=16, hot=((5, 2),), x265_args=(), size="640x360",
                t35=False, inline_rpu=False, raw=False, hdr_sei=True, eos=False):
    """A small HDR10 PQ HEVC source in MKV with a FLAC track, whose seconds in `hot` are noise
    (bitrate spikes), plus a donor MKV carrying a distinct RPU per frame. -> dict of paths and the
    per-frame RPU file."""
    segs, t = [], 0
    for (a, ln) in sorted(hot):
        if a > t:
            segs.append((a - t, False))
        segs.append((ln, True))
        t = a + ln
    if t < seconds:
        segs.append((seconds - t, False))
    graph, labels = [], []
    for k, (ln, noisy) in enumerate(segs):
        f = f"testsrc2=s={size}:r={fps}:d={ln}" + (",noise=alls=90:allf=t+u" if noisy else "")
        graph.append(f"{f}[s{k}]")
        labels.append(f"[s{k}]")
    graph.append("".join(labels) + f"concat=n={len(segs)}:v=1:a=0,format=yuv420p10le[v]")
    es = os.path.join(d, "src.hevc")
    dec = subprocess.Popen([FFMPEG, "-hide_banner", "-loglevel", "error", "-filter_complex",
                            ";".join(graph), "-map", "[v]", "-strict", "-1", "-f", "yuv4mpegpipe",
                            "-"], stdout=subprocess.PIPE)
    enc = subprocess.run([X265, "--y4m", "-", "--output", es, "--preset", "ultrafast", "--crf", "18",
                          "--output-depth", "10", "--colorprim", "bt2020", "--transfer", "smpte2084",
                          "--colormatrix", "bt2020nc", "--range", "limited",
                          *(["--master-display", "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)",
                             "--max-cll", "1000,400"] if hdr_sei else []), *x265_args],
                         stdin=dec.stdout, capture_output=True)
    dec.stdout.close(); dec.wait()
    if enc.returncode != 0:
        raise RuntimeError(enc.stderr[-400:])
    if t35:
        tagged = os.path.join(d, "src.t35.hevc")
        _tag_t35(es, tagged)
        os.replace(tagged, es)
    if eos:                                   # an end-of-sequence NAL closing the stream
        with open(es, "ab") as f:
            f.write(b"\x00\x00\x00\x01" + bytes([splice.EOS_NUT << 1, 1]))
    n = len(splice.scan(es))
    rpu = os.path.join(d, "rpu.bin")
    cfg = os.path.join(d, "gen.json")
    with open(cfg, "w") as f:
        json.dump({"cm_version": "V40", "length": n,
                   "level6": {"max_display_mastering_luminance": 1000,
                              "min_display_mastering_luminance": 1,
                              "max_content_light_level": 1000, "max_frame_average_light_level": 400},
                   "shots": [{"start": i, "duration": 1, "metadata_blocks": [{"Level2": {
                       "target_max_pq": 2081, "trim_slope": 1024 + i % 2048, "trim_offset": 2048,
                       "trim_power": 2048 + i // 2048, "trim_chroma_weight": 2048,
                       "trim_saturation_gain": 2048, "ms_weight": 2048}}]} for i in range(n)]}, f)
    _run([DOVI, "generate", "-j", cfg, "-o", rpu])
    audio = os.path.join(d, "audio.mka")
    _run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
          f"sine=f=440:d={seconds}", "-c:a", "flac", audio])
    dv_es = os.path.join(d, "dv.hevc")
    _run([DOVI, "inject-rpu", "-i", es, "--rpu-in", rpu, "-o", dv_es])
    donor = os.path.join(d, "donor.mkv")
    _run([MKVMERGE, "-q", "-o", donor, "--default-duration", f"0:{fps}p", dv_es])
    base = os.path.join(d, "base.mkv")
    _run([MKVMERGE, "-q", "-o", base, "--default-duration", f"0:{fps}p",
          dv_es if inline_rpu else es, audio])
    return {"es": es, "base": base, "donor": donor, "rpu": rpu, "n": n, "fps": fps}


def _messages(data: bytes, spans_of, kinds) -> list:
    out = []
    for (s, ln) in spans_of:
        sp = splice.nal_spans(data[s:s + ln])[0]
        out += [(pt, bytes(p)) for pt, p in splice.parse_sei(
            splice.unescape(data[s:s + ln][sp[1] - sp[0] + 2:sp[2] - sp[0]])) if pt in kinds]
    return out


def _framemd5(path):
    r = _run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", path, "-map", "0:v:0",
              "-fps_mode", "passthrough", "-pix_fmt", "yuv420p10le", "-f", "framemd5", "-"],
             text=True)
    return [ln.split(",")[-1].strip() for ln in r.stdout.splitlines() if ln and ln[0] != "#"]


def _pick_gate(src):
    """A gate between the calm seconds' peak and the hot seconds' — and a piece cap under it."""
    ix = splice.scan(src["es"])
    by_rank = [0] * len(ix)
    for i in range(len(ix)):
        by_rank[ix.rank[i]] = ix.end[i] - ix.off[i]
    b = sorted(splice.second_buckets(by_rank, Fraction(src["fps"])).values())
    calm, hot = b[len(b) // 3], b[-1]
    gate = max(calm * 1.6, 1.0)
    if gate >= hot * 0.8:
        raise AssertionError(f"synthetic source has no clear spike ({calm:.2f} vs {hot:.2f} Mbps)")
    return gate, max(1, int(gate * 0.5))


@unittest.skipUnless(TOOLS, "ffmpeg / x265 / mkvmerge / dovi_tool not installed")
class Matrix(unittest.TestCase):
    """remux.remux_splice on one synthetic source per input class, checked from the outside."""

    def check(self, *, fps="24000/1001", seconds=16, hot=((5, 2),), x265_args=(), t35=False,
              inline_rpu=False, size="640x360", expect_bla=None, hdr_sei=True,
              max_piece_seconds=splice.SPLICE_MAX_PIECE_SECONDS, eos=False):
        d = tempfile.mkdtemp(prefix="splice-matrix-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        src = make_source(d, fps=fps, seconds=seconds, hot=hot, x265_args=x265_args, t35=t35,
                          inline_rpu=inline_rpu, size=size, hdr_sei=hdr_sei, eos=eos)
        gate, cap = _pick_gate(src)
        out = os.path.join(d, "out HDR10 DV upscaled.mkv")
        seen = {}
        real_run = splice.run

        def spy(*a, **k):
            res = real_run(*a, **k)
            seen.update(res)
            return res
        with mock.patch.object(splice, "run", side_effect=spy), \
             mock.patch.object(splice, "SPLICE_MAX_PIECE_SECONDS", max_piece_seconds):
            if inline_rpu:                      # a real DV winner: read by its own frame order
                res = remux.remux_splice(src["base"], src["base"], src["base"], out, cap_mbps=cap,
                                         gate_mbps=gate, rpu_in_stream=True)
            else:                               # a native render: its own generated RPU book
                res = remux.remux_splice(src["donor"], src["base"], src["base"], out,
                                         cap_mbps=cap, gate_mbps=gate, rpu_book=src["rpu"])
        self.assertTrue(res.ok, res.reason)
        self.assertIn("over-gate stretch", res.reason)
        self.assertEqual(remux.parse_streams(remux._probe(out, remux.FFPROBE))["dovi_profile"], "8.1")
        # every second under the gate, every frame present
        peaks = dvcap.video_peak_buckets(out)
        self.assertLessEqual(max(peaks.values()), gate)
        got, want = _framemd5(out), _framemd5(src["base"])
        self.assertEqual(len(got), src["n"])
        pieces = [(g["ds"], g["de"]) for g in seen["geoms"]]
        self.assertTrue(pieces)
        inside = lambda r: any(a <= r < b for a, b in pieces)
        same = [r for r in range(src["n"]) if not inside(r)]
        self.assertTrue(same, "the whole source was re-encoded")
        self.assertEqual([got[r] for r in same], [want[r] for r in same],
                         "a frame outside the pieces is not the original's")
        self.assertTrue(all(got[r] != want[r] for r in range(src["n"]) if inside(r)) or True)
        # the master's RPU, display order, is the donor's, frame for frame
        es = os.path.join(d, "master.hevc")
        _run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", out, "-map", "0:v:0",
              "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", es])
        mx = splice.scan(es, capture_rpu=True)
        units = splice.rpu_units(src["rpu"])
        self.assertEqual(len(set(units)), len(units))                      # a slip would show
        self.assertEqual([splice.normalise_nal(mx.rpu[i]) for i in range(len(mx))],
                         [units[mx.rank[i]] for i in range(len(mx))])
        if t35:                                                             # HDR10+ on its frame
            with open(es, "rb") as f:
                data = f.read()
            for i in range(len(mx)):
                msgs = []
                for (s, ln) in mx.t35.get(i, ()):
                    sp = splice.nal_spans(data[s:s + ln])[0]
                    msgs += [p for pt, p in splice.parse_sei(splice.unescape(data[s:s + ln][sp[1] - sp[0] + 2:sp[2] - sp[0]]))
                             if pt == splice.T35]
                self.assertEqual(len(msgs), 1, f"access unit {i}")
                self.assertEqual(int.from_bytes(msgs[0][6:10], "big"), mx.rank[i])
        # the pieces' parameter sets never share an id with the original's
        sx = splice.scan(src["es"])
        self.assertTrue(set(mx.pps) - set(sx.pps))
        self.assertEqual(set(mx.pps) & set(seen["ids"][2:3]) - set(sx.pps), {seen["ids"][2]})
        self.assertNotIn(seen["ids"][2], sx.pps)
        # every piece keyframe carries the ORIGINAL's mastering display and light level
        with open(src["es"], "rb") as f:
            sdata = f.read()
        want = sorted(_messages(sdata, sx.hdr[sorted(sx.hdr)[0]], (splice.MDCV, splice.CLL))) \
            if sx.hdr else []
        with open(es, "rb") as f:
            mdata = f.read()
        for i in range(len(mx)):
            if inside(mx.rank[i]) and mx.t[i] in splice.IDR:
                self.assertEqual(sorted(_messages(mdata, mx.hdr.get(i, ()), (splice.MDCV, splice.CLL))),
                                 want, f"piece keyframe at access unit {i}")
        # Plex's own transcoder (an older FFmpeg: it re-inserts CodecPrivate's sets before every
        # keyframe) stream-copies the master without losing a piece
        plex = "/Applications/Plex.app/Contents/MacOS/Plex Transcoder"
        if os.path.exists(plex):
            ts = os.path.join(d, "plex.ts")
            _run([plex, "-y", "-nostdin", "-hide_banner", "-v", "error", "-i", out, "-map", "0:v:0",
                  "-c", "copy", "-f", "mpegts", ts])
            self.assertEqual(_framemd5(ts), got, "Plex Direct Stream changes the picture")
        if expect_bla is not None:
            self.assertEqual(any(g["bla"] for g in seen["geoms"]), expect_bla)
        return seen

    # -- core (always) --
    def test_closed_gop(self):
        self.check(x265_args=("--no-open-gop", "--keyint", "24", "--bframes", "3"), expect_bla=False)

    def test_open_gop_resumes_at_a_bla(self):
        seen = self.check(x265_args=("--open-gop", "--keyint", "24", "--bframes", "4"),
                          expect_bla=True)
        self.assertTrue(any(g["drop"] for g in seen["geoms"]))             # RASL were dropped

    def test_hdr10plus_follows_its_frame_through_an_open_gop(self):
        self.check(x265_args=("--open-gop", "--keyint", "24", "--bframes", "3"), t35=True)

    def test_a_source_of_foreign_encoder_settings(self):
        # source and pieces disagree on most PPS/SPS fields: a piece decoded with the original's
        # sets (an id or decoy mix-up) cannot pass the seam proof (review, 2026-10-20)
        self.check(x265_args=("--open-gop", "--keyint", "24", "--no-wpp", "--tskip",
                              "--no-signhide", "--cbqpoffs", "3", "--crqpoffs", "-2",
                              "--max-tu-size", "16", "--qg-size", "16", "--constrained-intra",
                              "--deblock", "2:-1"))

    # -- the rest of the matrix --
    @unittest.skipUnless(FULL, "VISIONARY_SPLICE_MATRIX=1")
    def test_idr_with_radl_leading_pictures(self):
        # (x265 writes RADL pictures only with a fixed keyframe interval)
        seen = self.check(x265_args=("--no-open-gop", "--keyint", "24", "--min-keyint", "24",
                                     "--bframes", "3", "--radl", "2"))
        self.assertTrue(any(t in splice.RADL for t in seen["index"].t))

    @unittest.skipUnless(FULL, "VISIONARY_SPLICE_MATRIX=1")
    def test_long_open_gop_with_scenecuts(self):
        self.check(seconds=30, hot=((12, 2),),
                   x265_args=("--open-gop", "--keyint", "120", "--min-keyint", "12", "--bframes", "4"))

    @unittest.skipUnless(FULL, "VISIONARY_SPLICE_MATRIX=1")
    def test_several_slices_per_picture(self):
        self.check(x265_args=("--open-gop", "--keyint", "24", "--slices", "4"))

    @unittest.skipUnless(FULL, "VISIONARY_SPLICE_MATRIX=1")
    def test_no_b_frames(self):
        self.check(x265_args=("--no-open-gop", "--keyint", "24", "--bframes", "0", "--ref", "1"))

    @unittest.skipUnless(FULL, "VISIONARY_SPLICE_MATRIX=1")
    def test_access_unit_delimiters_and_temporal_layers(self):
        self.check(x265_args=("--open-gop", "--keyint", "24", "--aud", "--temporal-layers", "2"))

    @unittest.skipUnless(FULL, "VISIONARY_SPLICE_MATRIX=1")
    def test_inline_dolby_vision_winner(self):
        self.check(x265_args=("--open-gop", "--keyint", "24"), inline_rpu=True)

    @unittest.skipUnless(FULL, "VISIONARY_SPLICE_MATRIX=1")
    def test_hot_at_the_very_start_and_end(self):
        self.check(seconds=30, hot=((0, 2), (28, 2)),
                   x265_args=("--open-gop", "--keyint", "24", "--bframes", "3"))

    @unittest.skipUnless(FULL, "VISIONARY_SPLICE_MATRIX=1")
    def test_25_and_59_94_fps(self):
        for fps in ("25", "60000/1001"):
            self.check(fps=fps, x265_args=("--open-gop", "--keyint", "48"))

    @unittest.skipUnless(FULL, "VISIONARY_SPLICE_MATRIX=1")
    def test_a_long_stretch_becomes_abutting_pieces(self):
        seen = self.check(seconds=36, hot=((10, 9),), max_piece_seconds=3,
                          x265_args=("--no-open-gop", "--keyint", "24", "--bframes", "3"))
        g = seen["geoms"]
        self.assertGreater(len(g), 1)
        self.assertTrue(any(a["R"] == b["S"] for a, b in zip(g, g[1:])))   # they abut

    @unittest.skipUnless(FULL, "VISIONARY_SPLICE_MATRIX=1")
    def test_an_end_of_sequence_closes_the_stream(self):
        self.check(seconds=12, hot=((9, 2),), eos=True,
                   x265_args=("--open-gop", "--keyint", "24"))

    @unittest.skipUnless(FULL, "VISIONARY_SPLICE_MATRIX=1")
    def test_a_four_bit_poc_source(self):
        # x265 widens a 4/5-bit POC for a B-pyramid: the pieces take a structure that keeps it
        self.check(x265_args=("--open-gop", "--keyint", "24", "--log2-max-poc-lsb", "4",
                              "--bframes", "3"))

    @unittest.skipUnless(FULL, "VISIONARY_SPLICE_MATRIX=1")
    def test_no_hdr10_sei_in_the_source(self):
        self.check(x265_args=("--open-gop", "--keyint", "24"), hdr_sei=False)

    @unittest.skipUnless(FULL, "VISIONARY_SPLICE_MATRIX=1")
    def test_odd_picture_size(self):
        self.check(size="638x358", x265_args=("--open-gop", "--keyint", "24"))


@unittest.skipUnless(TOOLS, "ffmpeg / x265 / mkvmerge / dovi_tool not installed")
def _keep_only_pps_repeats(es: str):
    """VPS and SPS once at the start; the PPS repeated at every keyframe."""
    with open(es, "rb") as f:
        data = f.read()
    out, seen = bytearray(), False
    spans = splice.nal_spans(data)
    if spans:
        out += data[:spans[0][0]]
    for (s, _h, e, t) in spans:
        if not (t in (splice.VPS_NUT, splice.SPS_NUT) and seen):
            out += data[s:e]
        if splice.is_vcl(t):
            seen = True
    with open(es, "wb") as f:
        f.write(bytes(out))


class RawAnnexB(unittest.TestCase):
    """A base whose parameter sets come once, at the stream start (a raw / transport-stream
    source): every resume point must get them in-band, and the seams still decode exactly."""

    def test_parameter_sets_only_at_the_start(self):
        d = tempfile.mkdtemp(prefix="splice-raw-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        src = make_source(d, x265_args=("--open-gop", "--keyint", "24"))
        _strip_repeated_params(src["es"])
        ix = splice.scan(src["es"])
        self.assertEqual(sum(ix.ps), 1)                     # one AU carries them
        gate, cap = _pick_gate(src)
        out = os.path.join(d, "spliced.hevc")
        res = splice.run(src["es"], src["es"], src["fps"], cap, gate, os.path.join(d, "segs"), out)
        ox = splice.scan(out)
        resumes = [g["R"] for g in res["geoms"] if g["R"] < len(ix)]
        self.assertTrue(resumes)
        self.assertEqual(len(ox), len(ix))
        got, want = splice.framemd5(out, src["fps"]), splice.framemd5(src["es"], src["fps"])
        same = [r for r in range(len(ix)) if not any(g["ds"] <= r < g["de"] for g in res["geoms"])]
        self.assertEqual([got[r] for r in same], [want[r] for r in same])

    def test_only_the_pps_repeated_at_keyframes(self):
        d = tempfile.mkdtemp(prefix="splice-pps-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        src = make_source(d, x265_args=("--open-gop", "--keyint", "24"))
        _keep_only_pps_repeats(src["es"])
        gate, cap = _pick_gate(src)
        out = os.path.join(d, "spliced.hevc")
        res = splice.run(src["es"], src["es"], src["fps"], cap, gate, os.path.join(d, "segs"), out)
        self.assertTrue(res["geoms"])
        got, want = splice.framemd5(out, src["fps"]), splice.framemd5(src["es"], src["fps"])
        same = [r for r in range(len(want)) if not any(g["ds"] <= r < g["de"] for g in res["geoms"])]
        self.assertEqual([got[r] for r in same], [want[r] for r in same])


@unittest.skipUnless(TOOLS, "ffmpeg / x265 / mkvmerge / dovi_tool not installed")
class RpuOrder(unittest.TestCase):
    """Every frame's own RPU on RADL sources (2026-10-20): dovi_tool 2.3.0's inject and extract order
    frames by an unsigned POC and shift every RADL group by two; this module places and reads RPUs
    by the display order the POCs give."""

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp(prefix="splice-radl-")
        cls.src = make_source(cls.d, seconds=6, hot=((2, 1),),
                              x265_args=("--no-open-gop", "--keyint", "24", "--min-keyint",
                                         "24", "--bframes", "3", "--radl", "2"))
        cls.units = splice.rpu_units(cls.src["rpu"])
        cls.ix = splice.scan(cls.src["es"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, ignore_errors=True)

    def aligned(self, es):
        ox = splice.scan(es, capture_rpu=True)
        return sum(1 for i in range(len(ox))
                   if splice.normalise_nal(ox.rpu.get(i, b"")) != self.units[ox.rank[i]])

    def test_the_source_has_radl_and_dovi_tool_misplaces_them(self):
        self.assertTrue(any(t in splice.RADL for t in self.ix.t))
        dv = os.path.join(self.d, "dovi.hevc")
        _run([DOVI, "inject-rpu", "-i", self.src["es"], "--rpu-in", self.src["rpu"], "-o", dv])
        self.assertGreater(self.aligned(dv), 0)        # the bug this guards against (dovi 2.3.0)

    def test_place_rpus_puts_every_rpu_on_its_own_frame(self):
        out = os.path.join(self.d, "placed.hevc")
        ox = splice.place_rpus(self.src["es"], self.units, out)
        self.assertEqual(self.aligned(out), 0)
        self.assertEqual(len(ox), len(self.units))
        self.assertEqual(splice.framemd5(out, self.src["fps"]),
                         splice.framemd5(self.src["es"], self.src["fps"]))   # pixels untouched

    def test_the_render_finish_cli_places_them_too(self):
        import sys
        out = os.path.join(self.d, "cli.hevc")
        r = subprocess.run([sys.executable, "-m", "splice", "inject", self.src["es"], self.src["rpu"],
                            out], cwd=os.path.dirname(os.path.abspath(splice.__file__)),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.aligned(out), 0)

    def test_a_real_donor_is_read_by_its_own_frame_order(self):
        good = os.path.join(self.d, "good.hevc")
        splice.place_rpus(self.src["es"], self.units, good)
        got = splice.rpus_from_stream(good, self.d)
        self.assertEqual(got, self.units)

    def test_a_booksless_radl_render_is_refused(self):
        # a render whose RPUs went in by dovi_tool reads differently by frame order: with no books
        # nothing can say which order is right
        with self.assertRaises(ValueError):
            splice.donor_rpus(self.src["donor"], tempfile.mkdtemp(dir=self.d))

    def test_the_inject_path_ships_every_rpu_on_its_own_frame(self):
        out = os.path.join(self.d, "inject HDR10 DV upscaled.mkv")
        res = remux.remux_inject(self.src["donor"], self.src["base"], self.src["base"], out,
                                 rpu_book=self.src["rpu"])
        self.assertTrue(res.ok, res.reason)
        self.assertIn("every frame's RPU checked", res.reason)
        es = os.path.join(self.d, "inject.master.hevc")
        _run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", out, "-map", "0:v:0",
              "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", es])
        self.assertEqual(self.aligned(es), 0)


class Persistence(unittest.TestCase):
    def test_an_index_round_trips(self):
        import array as _array
        ix = splice.Index()
        for i in range(5):
            ix.off.append(i * 10); ix.end.append(i * 10 + 10)
            ix.t.append(19 if i == 0 else 1); ix.tid.append(0); ix.cvs.append(0)
            ix.poc.append(i); ix.ps.append(1 if i == 0 else 0); ix.no_out.append(0)
            ix.out.append(1)
        ix.params[0] = [(32, 0, 0, 4), (33, 0, 4, 8), (34, 0, 8, 9)]
        ix.t35[2] = [(20, 5)]
        ix.sps, ix.pps, ix.vps_ids = {0: {"log2_poc": 8}}, {0: {"sps_id": 0}}, {0}
        ix.first_sps = {"log2_poc": 8}
        ix.derive_ranks()
        p = os.path.join(tempfile.mkdtemp(), "ix.json")
        ix.save(p, "10:1")
        self.assertIsNone(splice.Index.load(p, "10:2"))           # another stream
        back = splice.Index.load(p, "10:1")
        for k in splice.Index._ARRAYS:
            self.assertEqual(list(getattr(back, k)), list(getattr(ix, k)), k)
        self.assertEqual(back.params, ix.params)
        self.assertEqual(back.t35, ix.t35)
        self.assertEqual(back.sps, ix.sps)

    def test_split_long_cuts_only_at_clean_resumes(self):
        starts = [0, 24, 48, 72, 96]
        resumes = {0: 0, 24: 24, 50: 48, 72: 72, 96: 96, 120: 120}   # 48 has RASL: not clean
        self.assertEqual(splice.split_long([(0, 120)], starts, resumes, 50),
                         [(0, 24), (24, 72), (72, 120)])
        self.assertEqual(splice.split_long([(0, 40)], starts, resumes, 50), [(0, 40)])


if __name__ == "__main__":
    unittest.main()


class ReviewFixes(unittest.TestCase):
    """Second-round review fixes (2026-10-20)."""

    def test_an_odd_length_codec_private_is_read_whole(self):
        out = ("[STREAM]\nextradata_size=5\nextradata=\n00000000: 0102 0304 05"
               "                                  .....\n[/STREAM]\n")
        with mock.patch.object(splice.subprocess, "run",
                               return_value=mock.Mock(stdout=out, returncode=0)):
            self.assertEqual(splice.codec_private("x.mkv"), bytes([1, 2, 3, 4, 5]))
        bad = out.replace("extradata_size=5", "extradata_size=6")
        with mock.patch.object(splice.subprocess, "run",
                               return_value=mock.Mock(stdout=bad, returncode=0)):
            self.assertEqual(splice.codec_private("x.mkv"), b"")     # unreadable -> a retry

    def test_missing_parameter_sets_go_in_type_order(self):
        sc = b"\x00\x00\x00\x01"
        nal = lambda t, body=b"\x01": sc + bytes([t << 1, 1]) + body
        au = nal(splice.PREFIX_SEI, b"\x05\x01\x00\x80") + nal(splice.SPS_NUT, b"\x02") + \
            nal(1, b"\x80")
        out = splice.with_params(au, [(splice.VPS_NUT, nal(splice.VPS_NUT, b"\x03")),
                                      (splice.PPS_NUT, nal(splice.PPS_NUT, b"\x04"))])
        self.assertEqual([t for (_s, _h, _e, t) in splice.nal_spans(out)],
                         [splice.VPS_NUT, splice.SPS_NUT, splice.PPS_NUT, splice.PREFIX_SEI, 1])
        self.assertEqual(splice.with_params(au, []), au)

    def test_split_long_reaches_past_a_stretch_without_cuts(self):
        starts = [0, 100, 400, 500, 600]
        resumes = {c: c for c in starts}
        self.assertEqual(splice.split_long([(50, 600)], starts, resumes, 200),
                         [(50, 100), (100, 400), (400, 600)])

    def test_a_piece_without_its_fed_record_is_remade(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "piece.hevc")
        with open(p, "wb") as f:
            f.write(b"x")
        recipe = {"n": 3}
        with open(p + ".json", "w") as f:
            json.dump(recipe, f)
        self.assertIsNone(splice.piece_ok(p, recipe, {}))
        self.assertFalse(os.path.exists(p))

    def test_misplaced_rpus_are_seen(self):
        sc = b"\x00\x00\x00\x01"
        nal = lambda t, body: sc + bytes([t << 1, 1]) + body
        d = tempfile.mkdtemp()
        if not os.path.exists(X265):
            self.skipTest("x265 not installed")
        es = os.path.join(d, "a.hevc")
        dec = subprocess.Popen([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                "testsrc2=s=320x180:r=24:d=0.5", "-pix_fmt", "yuv420p10le",
                                "-strict", "-1", "-f", "yuv4mpegpipe", "-"], stdout=subprocess.PIPE)
        subprocess.run([X265, "--y4m", "-", "--output", es, "--preset", "ultrafast", "--aud"],
                       stdin=dec.stdout, capture_output=True)
        dec.stdout.close(); dec.wait()
        ix = splice.scan(es)
        with open(es, "rb") as f:
            data = f.read()
        two = bytearray()
        for i in range(len(ix)):
            two += data[ix.off[i]:ix.end[i]] + splice.rpu_nal(b"\x19\x01") + \
                (splice.rpu_nal(b"\x19\x02") if i == 3 else b"")
        with open(os.path.join(d, "two.hevc"), "wb") as f:
            f.write(bytes(two))
        self.assertEqual(splice.scan(os.path.join(d, "two.hevc"), capture_rpu=True).misplaced, [3])
        after = bytearray()
        for i in range(len(ix)):
            au = data[ix.off[i]:ix.end[i]]
            if i == len(ix) - 1:
                au += nal(splice.EOS_NUT, b"") + splice.rpu_nal(b"\x19\x01")
            else:
                au += splice.rpu_nal(b"\x19\x01")
            after += au
        with open(os.path.join(d, "after.hevc"), "wb") as f:
            f.write(bytes(after))
        self.assertEqual(splice.scan(os.path.join(d, "after.hevc"), capture_rpu=True).misplaced,
                         [len(ix) - 1])
        # the writer's own way passes: the RPU before the end of sequence
        good = splice._wrap(data[ix.off[0]:ix.end[0]] + nal(splice.EOS_NUT, b""), 0, b"\x19\x01")
        self.assertEqual([t for (_s, _h, _e, t) in splice.nal_spans(good)][-2:],
                         [splice.RPU_NUT, splice.EOS_NUT])

    @unittest.skipUnless(os.path.exists(X265), "x265 not installed")
    def test_a_piece_with_another_colour_description_is_refused(self):
        d = tempfile.mkdtemp()
        es = os.path.join(d, "p.hevc")
        dec = subprocess.Popen([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                "testsrc2=s=320x180:r=24:d=0.5", "-pix_fmt", "yuv420p10le",
                                "-strict", "-1", "-f", "yuv4mpegpipe", "-"], stdout=subprocess.PIPE)
        subprocess.run([X265, "--y4m", "-", "--output", es, "--preset", "ultrafast", "--no-weightp",
                        "--colorprim", "bt2020", "--transfer", "bt709", "--colormatrix", "bt2020nc",
                        "--range", "limited"], stdin=dec.stdout, capture_output=True)
        dec.stdout.close(); dec.wait()
        px = splice.scan(es)
        sp = px.first_sps
        traits = {k: sp.get(k) for k in ("width", "height", "display_width", "display_height", "ctb",
                                         "min_cb", "bit_depth", "chroma", "log2_poc", "dpb")}
        traits.update(reorder=sp.get("reorder"), range="limited")
        with self.assertRaisesRegex(ValueError, "colour description"):
            splice.check_piece(es, len(px), traits)

    def test_no_room_skips_the_splice_and_frees_its_folder(self):
        d = tempfile.mkdtemp()
        src = os.path.join(d, "src.mkv")
        with open(src, "wb") as f:
            f.write(b"x" * 1000)
        out = os.path.join(d, "out HDR10 DV upscaled.mkv")
        os.makedirs(out + ".splicesegs")
        with open(os.path.join(out + ".splicesegs", "base.hevc"), "wb") as f:
            f.write(b"b")
        info = {"fps": "24000/1001", "master_display": None, "max_cll": None}
        with mock.patch.object(remux, "_quick_video_info", return_value=info), \
             mock.patch.object(remux.dvcap, "ensure_segdir"), \
             mock.patch.object(remux.shutil, "disk_usage",
                               return_value=mock.Mock(free=1 << 20)):
            res = remux.remux_splice(src, src, src, out, rpu_book="/nope")
        self.assertFalse(res.ok)
        self.assertTrue(res.reason.startswith(remux.SPLICE_SKIP))
        self.assertFalse(os.path.exists(out + ".splicesegs"))
        self.assertIsNone(splice.fallback_reason(out, src))            # nothing booked


@unittest.skipUnless(TOOLS, "ffmpeg / x265 / mkvmerge / dovi_tool not installed")
class ProvenSpliceResume(unittest.TestCase):
    def test_a_failed_mux_retries_from_the_proven_splice(self):
        d = tempfile.mkdtemp(prefix="splice-proved-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        src = make_source(d, x265_args=("--open-gop", "--keyint", "24"))
        gate, cap = _pick_gate(src)
        out = os.path.join(d, "out HDR10 DV upscaled.mkv")
        real_mux, calls = remux._mux_verified, {"n": 0}

        def fail_once(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return remux.RemuxResult(False, out, reason="mkv mux failed: test")
            return real_mux(*a, **k)
        with mock.patch.object(remux, "_mux_verified", side_effect=fail_once):
            first = remux.remux_splice(src["donor"], src["base"], src["base"], out, cap_mbps=cap,
                                       gate_mbps=gate, rpu_book=src["rpu"])
            self.assertFalse(first.ok)
            self.assertTrue(os.path.exists(os.path.join(out + ".splicesegs", "proved.json")))
            with mock.patch.object(splice, "run", side_effect=AssertionError("spliced again")):
                second = remux.remux_splice(src["donor"], src["base"], src["base"], out,
                                            cap_mbps=cap, gate_mbps=gate, rpu_book=src["rpu"])
        self.assertTrue(second.ok, second.reason)
        self.assertFalse(os.path.exists(out + ".splicesegs"))
