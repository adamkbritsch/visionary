import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import dvnative
import nativelane
import topaz
import versions

GiB = 1024 ** 3
REAL_TOOLCHAIN_OK = nativelane.toolchain_ok      # for tests whose fixture mocks it
FRAMES = (240, 300, 180)
TOTAL = sum(FRAMES)
FPS = "24000/1001"


def _append(path, data):
    with open(path, "ab") as f:
        f.write(data)


def _plan(**kw):
    pl = {"topaz": "upscale", "resolve": "add_hdr_dv", "res": "1080p", "scale": 2,
          "input": {"height": 1080}}
    pl.update(kw)
    return pl


def _item(segdir="/s/x.segments", **kw):
    p = types.SimpleNamespace(segdir=segdir, source="/s/x.mkv", source_cfr="/s/x_cfr.mkv",
                              series="Show", ep="S01E01", movie=False, youtube=False, combine=False)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def _sc(total=TOTAL, cll=640, fall=120):
    return {"version": 5,
            "scenes": [{"start": 0, "end": total - 1, "min_pq_12bit": 1,
                        "avg_max_rgb_pq_12bit": 1500, "max_pq_12bit": 2900}],
            "source": {"stream_frames": total, "leading_skipped_frames": 0},
            "light_level": {"max_cll_nits": cll, "max_fall_nits": fall},
            "l4": [[900, 400]] * total}


class Fixture(unittest.TestCase):
    """A scratch root with one Topaz segdir (3 chunks + segments.json), a CFR source, and the
    native pins pointed at a temp analyzer and LUT."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.segdir = os.path.join(self.root, "Show S01E01_prob4_upscaled.segments")
        self.make_segdir(self.segdir)
        self.cfr = os.path.join(self.root, "Show S01E01_cfr.mkv")
        with open(self.cfr, "wb") as f:
            f.write(b"cfr" * 10)
        self.p = _item(self.segdir, source=os.path.join(self.root, "Show S01E01.mkv"),
                       source_cfr=self.cfr)
        self.tools = tempfile.mkdtemp()                      # no spaces: lut_vf needs a plain path
        self.lut = os.path.join(self.tools, "rcm.cube")
        with open(self.lut, "w") as f:
            f.write("LUT_3D_SIZE 2\n")
        self.an = os.path.join(self.tools, "hdr_analyzer_mvp")
        with open(self.an, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(self.an, 0o755)
        self.pins = {"NATIVE_LUT_PATH": self.lut, "NATIVE_LUT_SHA256": nativelane.sha256_file(self.lut),
                     "HDR_ANALYZER_PATH": self.an, "HDR_ANALYZER_SHA256": nativelane.sha256_file(self.an),
                     "HDR_ANALYZER_COMMIT": "383b0ad", "DOVI_TOOL_VERSION": "2.3.0",
                     "NATIVE_FFMPEG_MAJOR": 8}
        self.patches = [mock.patch.object(versions, k, v, create=True) for k, v in self.pins.items()]
        for pt in self.patches:
            pt.start()

    def tearDown(self):
        for pt in self.patches:
            pt.stop()
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.tools, ignore_errors=True)

    def make_segdir(self, segdir, frames=FRAMES, sizes=None):
        os.makedirs(segdir, exist_ok=True)
        entries, a = [], 0
        for i, n in enumerate(frames):
            name = f"seg_{i:04d}.mov"
            with open(os.path.join(segdir, name), "wb") as f:
                if sizes:
                    f.truncate(sizes[i])
                else:
                    f.write(b"x" * (100 + i))
            entries.append({"file": name, "start": a, "frames": n})
            a += n
        topaz.write_manifest(segdir, 23.976, entries)
        return segdir

    def cfr_facts(self, **kw):
        st = os.stat(self.cfr)
        info = {"path": self.cfr, "frames": TOTAL, "fps": FPS, "size": st.st_size,
                "mtime_ns": st.st_mtime_ns}
        info.update(kw)
        return info

    def make_ready(self, p=None, total=TOTAL):
        """Claim the item and publish a ready marker over a fake RPU (frame count mocked)."""
        p = p or self.p
        cl = nativelane.chunks(p.segdir)
        nativelane.begin_marker(p, cl, FPS, self.cfr_facts())
        with open(nativelane.rpu_path(p), "wb") as f:
            f.write(b"rpu-bytes")
        with mock.patch.object(nativelane.dvcap, "rpu_frame_count", return_value=total):
            return nativelane.publish_ready(p, _sc(total))


# ---- eligibility -------------------------------------------------------------------------

class Eligibility(unittest.TestCase):
    def test_a_tv_sdr_upscale_at_dv1000_is_eligible(self):
        self.assertEqual(nativelane.eligible(_item(), _plan(), "dv1000"), (True, "eligible"))
        self.assertTrue(nativelane.eligible(_item(), _plan(res="720p"), "dv1000")[0])

    def test_the_matrix_of_items_that_stay_on_resolve(self):
        cases = {
            "movie": (_item(movie=True), _plan(), "dv1000"),
            "youtube": (_item(youtube=True), _plan(), "dv1000"),
            "combine": (_item(combine=True), _plan(), "dv1000"),
            "rpu-only fast path": (_item(), _plan(topaz="rpu-only", resolve="add_dv"), "dv1000"),
            "resolve-only fast path": (_item(), _plan(topaz="resolve-only"), "dv1000"),
            "already DV": (_item(), _plan(topaz="skip", resolve="skip"), "dv1000"),
            "HDR intake": (_item(), _plan(resolve="add_dv"), "dv1000"),
            "dv2000": (_item(), _plan(), "dv2000"),
            "sdr output": (_item(), _plan(), "sdr"),
            "480p": (_item(), _plan(res="480p", scale=4), "dv1000"),
            "no plan": (_item(), {}, "dv1000"),
        }
        for name, (p, pl, mode) in cases.items():
            with self.subTest(name):
                ok, why = nativelane.eligible(p, pl, mode)
                self.assertFalse(ok)
                self.assertTrue(why)

    def test_chunk_zero_colour(self):
        ok = {"height": 2160, "color_primaries": "bt709", "color_space": "bt709",
              "color_transfer": "bt709", "color_range": "tv"}
        self.assertTrue(nativelane.source_ok(ok)[0])
        self.assertTrue(nativelane.source_ok({"height": 2160})[0])            # untagged = BT.709
        self.assertTrue(nativelane.source_ok(dict(ok, color_primaries="unknown",
                                                  color_range="unknown"))[0])
        bad = {"BT.601 primaries": dict(ok, color_primaries="smpte170m"),
               "BT.601 matrix": dict(ok, color_space="bt470bg"),
               "BT.2020": dict(ok, color_primaries="bt2020"),
               "full range": dict(ok, color_range="pc"),
               "PQ": dict(ok, color_transfer="smpte2084"),
               "HLG": dict(ok, color_transfer="arib-std-b67"),
               "under 720p": dict(ok, height=480),
               "unprobed": {}}
        for name, info in bad.items():
            with self.subTest(name):
                self.assertFalse(nativelane.source_ok(info)[0])

    def test_effective_mode_mirrors_the_resolve_stage(self):
        for override, want in (("auto", "dv1000"), ("dv2000", "dv2000"), ("sdr", "sdr"),
                               ("dv1000", "dv1000"), ("junk", "dv1000")):
            with mock.patch.object(nativelane.settings, "get_show_output_mode", return_value=override):
                self.assertEqual(nativelane.effective_mode(_item()), want)

    def test_engine_setting_is_resolve_unless_exactly_native(self):
        for v, want in (("native", "native"), ("resolve", "resolve"), ("Native", "resolve"),
                        (None, "resolve")):
            with mock.patch.object(nativelane.settings, "dv_engine", return_value=v, create=True):
                self.assertEqual(nativelane.engine_setting(), want)
        with mock.patch.object(nativelane.settings, "dv_engine", side_effect=OSError, create=True):
            self.assertEqual(nativelane.engine_setting(), "resolve")


class Decide(Fixture):
    COLOUR = {"height": 2160, "color_primaries": "bt709", "color_space": "bt709",
              "color_range": "tv"}

    def decide(self, setting="native", pl=None, colour=None, tool=(True, "ok"), disk=(True, "room")):
        with mock.patch.object(nativelane, "engine_setting", return_value=setting), \
             mock.patch.object(nativelane, "probe_stream", return_value=colour or self.COLOUR), \
             mock.patch.object(nativelane, "toolchain_ok", return_value=tool), \
             mock.patch.object(nativelane, "disk_fits", return_value=disk):
            return nativelane.decide(self.p, pl=pl or _plan(), mode="dv1000")

    def test_native_when_every_check_passes(self):
        self.assertEqual(self.decide(), ("native", "eligible"))

    def test_setting_resolve_touches_nothing_else(self):
        """U1: with the default 'resolve', no probe, plan, toolchain or disk check runs."""
        boom = mock.Mock(side_effect=AssertionError("must not be called"))
        with mock.patch.object(nativelane, "engine_setting", return_value="resolve"), \
             mock.patch.object(nativelane, "probe_stream", boom), \
             mock.patch.object(nativelane, "toolchain_ok", boom), \
             mock.patch.object(nativelane, "disk_fits", boom), \
             mock.patch.object(nativelane, "chunks", boom), \
             mock.patch("plan.plan_for", boom):
            self.assertEqual(nativelane.decide(self.p)[0], "resolve")
        self.assertFalse(os.path.exists(nativelane.native_dir(self.p)))    # nothing written

    def test_each_failing_check_means_resolve(self):
        cases = {"not eligible": dict(pl=_plan(resolve="add_dv")),
                 "colour": dict(colour={"height": 2160, "color_primaries": "smpte170m"}),
                 "toolchain": dict(tool=(False, "dovi_tool is 2.2.0")),
                 "disk": dict(disk=(False, "projected 200 < 310"))}
        for name, kw in cases.items():
            with self.subTest(name):
                engine, why = self.decide(**kw)
                self.assertEqual(engine, "resolve")
                self.assertTrue(why)

    def test_unreadable_chunks_mean_resolve(self):
        os.remove(os.path.join(self.segdir, "seg_0001.mov"))
        self.assertEqual(self.decide()[0], "resolve")

    def test_a_marker_keeps_the_item_native_after_the_setting_flips_back(self):
        nativelane.begin_marker(self.p, nativelane.chunks(self.segdir), FPS, self.cfr_facts())
        self.assertEqual(self.decide(setting="resolve")[0], "native")
        self.assertEqual(self.decide(setting="resolve", tool=(False, "x"), disk=(False, "x"))[0], "native")

    def test_fallback_wins_over_marker_and_setting(self):
        nativelane.begin_marker(self.p, nativelane.chunks(self.segdir), FPS, self.cfr_facts())
        os.makedirs(nativelane.native_dir(self.p), exist_ok=True)
        with open(nativelane.fallback_path(self.p), "w") as f:
            f.write("{}")                                    # the manual per-item fallback
        self.assertEqual(self.decide()[0], "resolve")

    def test_a_broken_toolchain_is_resolve_this_time_and_writes_nothing(self):
        engine, why = self.decide(tool=(False, "dovi_tool is 2.2.0, pinned 2.3.0"))
        self.assertEqual(engine, "resolve")
        for words in ("toolchain is not ready", "Resolve takes this item this time",
                      "no fallback.json", "dovi_tool is 2.2.0, pinned 2.3.0"):
            self.assertIn(words, why)                    # the reason the orchestrator logs
        self.assertFalse(os.path.exists(nativelane.native_dir(self.p)))   # not sticky: nothing
        self.assertEqual(self.decide(), ("native", "eligible"))           # fixed -> native again

    def _decide_live_toolchain(self, problems, cached_at_age=None, cached=(True, "ok")):
        """decide() over the REAL toolchain_ok cache; toolchain_problems answers `problems`."""
        import time as _time
        nativelane._TOOLCHAIN.update(at=None, result=None)
        self.addCleanup(nativelane._TOOLCHAIN.update, at=None, result=None)
        if cached_at_age is not None:
            nativelane._TOOLCHAIN.update(at=_time.monotonic() - cached_at_age, result=cached)
        probe = mock.Mock(return_value=list(problems))
        disk = mock.Mock(return_value=(True, "room"))
        with mock.patch.object(nativelane, "engine_setting", return_value="native"), \
             mock.patch.object(nativelane, "probe_stream", return_value=self.COLOUR), \
             mock.patch.object(nativelane, "toolchain_problems", probe), \
             mock.patch.object(nativelane, "disk_fits", disk):
            return nativelane.decide(self.p, pl=_plan(), mode="dv1000"), probe, disk

    def test_a_claim_rechecks_a_pass_older_than_a_minute(self):
        # the 10-minute cache said fine, but the toolchain broke since: the claim re-checks
        (engine, why), probe, _ = self._decide_live_toolchain(
            ["dovi_tool is 2.2.0"], cached_at_age=nativelane.CLAIM_TOOLCHAIN_SECONDS + 5)
        self.assertEqual(engine, "resolve")
        self.assertIn("dovi_tool is 2.2.0", why)
        self.assertEqual(probe.call_count, 1)
        self.assertFalse(os.path.exists(nativelane.native_dir(self.p)))

    def test_a_fresh_pass_is_trusted_at_the_claim(self):
        # a sibling's lease re-picks every 10 s: no probe storm while it holds the machine
        result, probe, _ = self._decide_live_toolchain(["would fail"], cached_at_age=5)
        self.assertEqual(result, ("native", "eligible"))
        probe.assert_not_called()

    def test_a_cached_failure_answers_without_probing_or_disk_checks(self):
        (engine, why), probe, disk = self._decide_live_toolchain(
            [], cached_at_age=120, cached=(False, "ffmpeg major is 9, pinned 8"))
        self.assertEqual(engine, "resolve")
        self.assertIn("ffmpeg major is 9", why)
        probe.assert_not_called()
        disk.assert_not_called()


# ---- chunks and CFR ----------------------------------------------------------------------

class Chunks(Fixture):
    def test_decode_list_from_the_manifest(self):
        cl = nativelane.chunks(self.segdir)
        self.assertEqual([a for a, _p in cl], [0, 240, 540, 720])
        self.assertEqual(cl[-1], [720, None])
        self.assertEqual(cl[1][1], os.path.join(self.segdir, "seg_0001.mov"))

    def test_last_chunk_up_to_8_short_of_the_source_is_accepted(self):
        self.assertEqual(nativelane.chunks(self.segdir, expected=TOTAL + 8)[-1][0], TOTAL)
        self.assertEqual(nativelane.chunks(self.segdir, expected=TOTAL - 1)[-1][0], TOTAL)   # 1 long
        for expected in (TOTAL + 9, TOTAL - 2):
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                nativelane.chunks(self.segdir, expected=expected)

    def _rewrite(self, entries, total=None):
        data = {"fps": 23.976, "total_frames": total if total is not None else
                sum(e.get("frames", 0) for e in entries), "segments": entries}
        with open(os.path.join(self.segdir, topaz.MANIFEST), "w") as f:
            json.dump(data, f)

    def test_gaps_overlaps_and_bad_entries_are_refused(self):
        good = [{"file": "seg_0000.mov", "start": 0, "frames": 240},
                {"file": "seg_0001.mov", "start": 240, "frames": 300},
                {"file": "seg_0002.mov", "start": 540, "frames": 180}]
        cases = {"gap": [good[0], dict(good[1], start=241), dict(good[2], start=541)],
                 "overlap": [good[0], dict(good[1], start=239), dict(good[2], start=539)],
                 "zero frames": [good[0], dict(good[1], frames=0), dict(good[2], start=240)],
                 "missing file": [good[0], dict(good[1], file="seg_0009.mov"), good[2]],
                 "not from 0": [dict(good[0], start=5)],
                 "malformed": [{"file": "seg_0000.mov"}]}
        for name, entries in cases.items():
            with self.subTest(name):
                self._rewrite(entries)
                with self.assertRaises(ValueError):
                    nativelane.chunks(self.segdir)
        self._rewrite(good, total=721)
        with self.assertRaises(ValueError):
            nativelane.chunks(self.segdir)

    def test_no_manifest(self):
        os.remove(os.path.join(self.segdir, topaz.MANIFEST))
        with self.assertRaises(ValueError):
            nativelane.chunks(self.segdir)
        self.assertEqual(nativelane.chunk_bytes(self.segdir), 0)

    def test_chunk_bytes(self):
        self.assertEqual(nativelane.chunk_bytes(self.segdir), 100 + 101 + 102)


class Cfr(unittest.TestCase):
    def test_tolerance_and_exact_fps(self):
        self.assertTrue(nativelane.cfr_check(40000, 40000, FPS, FPS)[0])
        self.assertTrue(nativelane.cfr_check(39800, 40000, FPS, FPS)[0])      # 0.995
        self.assertTrue(nativelane.cfr_check(40200, 40000, FPS, FPS)[0])      # 1.005
        self.assertFalse(nativelane.cfr_check(39799, 40000, FPS, FPS)[0])
        self.assertFalse(nativelane.cfr_check(40201, 40000, FPS, FPS)[0])

    def test_fps_must_be_the_same_fraction(self):
        self.assertTrue(nativelane.cfr_check(100, 100, "48000/2002", "24000/1001")[0])
        ok, why = nativelane.cfr_check(100, 100, "24/1", FPS)
        self.assertFalse(ok)
        self.assertIn("24/1", why)

    def test_unknown_is_a_failure_here(self):
        self.assertFalse(nativelane.cfr_check(100, None, FPS, FPS)[0])
        self.assertFalse(nativelane.cfr_check(100, 100, None, FPS)[0])
        self.assertFalse(nativelane.cfr_check(100, 100, FPS, "0/0")[0])

    def test_normalize_fps(self):
        self.assertEqual(nativelane.normalize_fps("24000/1001"), "24000/1001")
        self.assertEqual(nativelane.normalize_fps("48000/2002"), "24000/1001")
        self.assertEqual(nativelane.normalize_fps("25"), "25/1")
        for junk in ("0/0", "N/A", "", "0/1"):
            self.assertIsNone(nativelane.normalize_fps(junk))


# ---- disk --------------------------------------------------------------------------------

class Disk(Fixture):
    def test_tail_matches_the_orchestrator(self):
        import orchestrator
        self.assertEqual(nativelane.ITEM_TAIL_GB, orchestrator.ITEM_TAIL_GB)

    def test_next_item_need_has_a_150_floor(self):
        self.assertEqual(nativelane.next_item_need_gb(100), 150)
        self.assertEqual(nativelane.next_item_need_gb(310), 310)
        self.assertEqual(nativelane.next_item_need_gb(None), 150)

    def test_verdict_arithmetic(self):
        # Community: 128 GiB ProRes -> need 248; raw 250 + prefetch 30 - 20 = 260
        self.assertEqual(nativelane.disk_verdict(250, 30, 0, 128), (True, 260, 248))
        # a 42-min episode needs 190 + 120 = 310: the same disk is short...
        self.assertEqual(nativelane.disk_verdict(250, 30, 0, 190), (False, 260, 310))
        # ...unless the previous native item's chunks (freed at its remux) are counted
        self.assertEqual(nativelane.disk_verdict(250, 30, 190, 190), (True, 450, 310))
        self.assertEqual(nativelane.disk_verdict(330, 0, 0, 190), (True, 310, 310))   # >= is enough
        self.assertEqual(nativelane.disk_verdict(169, 0, 0, 10), (False, 149, 150))  # hard minimum

    def test_disk_fits_sizes_this_items_chunks(self):
        seg = self.make_segdir(os.path.join(self.root, "Big_prob4_upscaled.segments"),
                               frames=(10, 10), sizes=(GiB, GiB + 1))      # sparse: 2 GiB + 1 byte
        p = _item(seg)
        ok, why = nativelane.disk_fits(p, phys_free_gb=170, prefetch_gb=0, held_gb=0)
        self.assertTrue(ok)                                   # 3 GiB + 120 is under the 150 minimum
        self.assertIn("this item's 3 GiB ProRes", why)
        self.assertIn(">= 150 GiB", why)
        self.assertFalse(nativelane.disk_fits(p, phys_free_gb=169, prefetch_gb=0, held_gb=0)[0])
        self.assertTrue(nativelane.disk_fits(p, phys_free_gb=100, prefetch_gb=70, held_gb=0)[0])

    def test_no_chunks_never_fits(self):
        p = _item(os.path.join(self.root, "none.segments"))
        self.assertFalse(nativelane.disk_fits(p, phys_free_gb=900, prefetch_gb=0, held_gb=0)[0])

    def test_held_native_counts_only_other_ready_items(self):
        other = self.make_segdir(os.path.join(self.root, "Show S01E02_prob4_upscaled.segments"))
        q = _item(other, source_cfr=self.cfr)
        self.make_ready(q)
        analyzing = self.make_segdir(os.path.join(self.root, "Show S01E03_prob4_upscaled.segments"))
        nativelane.begin_marker(_item(analyzing), nativelane.chunks(analyzing), FPS, self.cfr_facts())
        self.make_segdir(os.path.join(self.root, "Plain_prob4_upscaled.segments"))   # a Resolve item
        self.make_ready(self.p)
        self.assertEqual(nativelane.held_native_segdirs(self.root, exclude=self.segdir), [other])
        self.assertEqual(sorted(nativelane.held_native_segdirs(self.root)), sorted([other, self.segdir]))
        self.assertEqual(len(nativelane.held_native_segdirs(self.root, states=None)), 3)
        self.assertEqual(nativelane.held_native_bytes(self.root, exclude=self.segdir), 100 + 101 + 102)

    def test_mid_analysis_floor(self):
        frames = 7058                                          # ~28.9 GiB of intermediate
        self.assertIsNone(nativelane.floor_reason(180 * GiB, frames))
        why = nativelane.floor_reason(170 * GiB, frames, k=3)
        self.assertTrue(why.startswith("paused: low disk"))
        self.assertIn("chunk 4", why)
        hook = nativelane.disk_floor_hook(self.root, free_fn=lambda: 100 * GiB)
        self.assertTrue(hook(0, 10).startswith("paused:"))
        hook = nativelane.disk_floor_hook(self.root, free_fn=lambda: 500 * GiB)
        self.assertIsNone(hook(0, frames))


# ---- toolchain ---------------------------------------------------------------------------

class Toolchain(Fixture):
    def setUp(self):
        super().setUp()
        self.ffprobe = os.path.join(self.tools, "ffprobe")
        open(self.ffprobe, "w").close()
        self.answers = {"--help": (0, "Usage: hdr_analyzer_mvp"),
                        "--version": (0, "dovi_tool 2.3.0\n"),
                        "-version": (0, "ffmpeg version 8.0 Copyright (c) 2000-2025\n")}
        self.ran = []
        self.extra = [mock.patch.object(nativelane, "_run_quick", side_effect=self.quick),
                      mock.patch.object(nativelane.dvcap, "FFPROBE", self.ffprobe)]
        for pt in self.extra:
            pt.start()
        nativelane._TOOLCHAIN.update(at=None, result=None)

    def tearDown(self):
        for pt in self.extra:
            pt.stop()
        nativelane._TOOLCHAIN.update(at=None, result=None)
        super().tearDown()

    def quick(self, cmd):
        self.ran.append(cmd)
        return self.answers[cmd[-1]]

    def test_matching_toolchain(self):
        self.assertEqual(nativelane.toolchain_problems(), [])
        self.assertTrue(nativelane.toolchain_ok(force=True)[0])

    def test_each_mismatch_is_named(self):
        def bad_analyzer_sha():
            _append(self.an, b"# rebuilt\n")
        cases = {
            "analyzer sha": (bad_analyzer_sha, "not the pinned build"),
            "analyzer does not run": (lambda: self.answers.update({"--help": (1, "dyld: libavcodec.62")}),
                                      "does not run"),
            "analyzer missing": (lambda: os.remove(self.an), "missing"),
            "lut sha": (lambda: _append(self.lut, b"1 2 3\n"), "LUT is not the pinned"),
            "lut missing": (lambda: os.remove(self.lut), "LUT is missing"),
            "dovi_tool": (lambda: self.answers.update({"--version": (0, "dovi_tool 2.2.0")}), "2.2.0"),
            "dovi_tool missing": (lambda: self.answers.update({"--version": (None, "no such file")}),
                                  "dovi_tool is missing"),
            "ffmpeg major": (lambda: self.answers.update({"-version": (0, "ffmpeg version 7.1.1")}),
                             "ffmpeg major is 7"),
            "ffprobe": (lambda: os.remove(self.ffprobe), "ffprobe"),
        }
        for name, (breakit, words) in cases.items():
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                breakit()
                ok, why = nativelane.toolchain_ok(force=True)
                self.assertFalse(ok)
                self.assertIn(words, why)

    def test_cached_for_ten_minutes(self):
        nativelane.toolchain_ok(now=1000.0)
        n = len(self.ran)
        self.answers["--version"] = (0, "dovi_tool 9.9.9")
        self.assertTrue(nativelane.toolchain_ok(now=1000.0 + 599)[0])      # cached
        self.assertEqual(len(self.ran), n)
        self.assertFalse(nativelane.toolchain_ok(now=1000.0 + 600)[0])     # re-checked
        self.answers["--version"] = (0, "dovi_tool 2.3.0")
        self.assertTrue(nativelane.toolchain_ok(force=True, now=1000.0 + 601)[0])

    def test_max_age_narrows_the_cache_for_one_call(self):
        nativelane.toolchain_ok(now=1000.0)
        n = len(self.ran)
        self.answers["--version"] = (0, "dovi_tool 9.9.9")
        claim = nativelane.CLAIM_TOOLCHAIN_SECONDS
        self.assertTrue(nativelane.toolchain_ok(now=1000.0 + claim - 1, max_age=claim)[0])
        self.assertEqual(len(self.ran), n)                                   # still fresh
        self.assertFalse(nativelane.toolchain_ok(now=1000.0 + claim, max_age=claim)[0])
        self.assertGreater(len(self.ran), n)                                 # re-checked
        # ...and that answer now serves the ordinary 10-minute callers
        self.assertFalse(nativelane.toolchain_ok(now=1000.0 + claim + 1)[0])

    def test_version_parsers(self):
        self.assertEqual(nativelane.parse_dovi_version("dovi_tool 2.3.0\n"), "2.3.0")
        self.assertIsNone(nativelane.parse_dovi_version("command not found"))
        self.assertEqual(nativelane.parse_ffmpeg_major("ffmpeg version 8.0 Copyright"), 8)
        self.assertEqual(nativelane.parse_ffmpeg_major("ffmpeg version n8.0.1-tessus"), 8)
        self.assertEqual(nativelane.parse_ffmpeg_major("ffmpeg version 10.1"), 10)
        self.assertIsNone(nativelane.parse_ffmpeg_major("ffmpeg version N-112233-gabc"))


class ShippedPins(unittest.TestCase):
    def test_defaults_equal_versions_py(self):
        """The defaults only cover a versions.py that predates the pins; they must agree."""
        for name, want in nativelane._PIN_DEFAULTS.items():
            if hasattr(versions, name):
                got = getattr(versions, name)
                self.assertEqual(os.path.expanduser(got) if name.endswith("_PATH") else got,
                                 os.path.expanduser(want) if name.endswith("_PATH") else want, name)


# ---- the marker --------------------------------------------------------------------------

class Marker(Fixture):
    def test_lifecycle_analyzing_then_ready(self):
        cl = nativelane.chunks(self.segdir)
        m = nativelane.begin_marker(self.p, cl, FPS, self.cfr_facts())
        self.assertEqual(m["state"], "analyzing")
        self.assertNotIn("id", m)
        self.assertEqual(m["chunks"][0][:2], ["seg_0000.mov", 100])
        self.assertEqual((m["total"], m["fps"], m["avg_floor"]), (TOTAL, FPS, "V40"))
        self.assertEqual(m["lut_sha256"], self.pins["NATIVE_LUT_SHA256"])
        self.assertEqual(m["analyzer_sha256"], self.pins["HDR_ANALYZER_SHA256"])
        self.assertEqual(m["vf"], dvnative.lut_vf(self.lut))
        self.assertEqual(m["cfr"]["frames"], TOTAL)
        self.assertFalse(nativelane.ready(self.p))
        self.assertTrue(nativelane.has_marker(self.p))
        r = self.make_ready()
        self.assertEqual(r["state"], "ready")
        self.assertEqual(r["id"], nativelane.marker_id(r))
        self.assertEqual(r["max_cll"], "640,120")
        self.assertEqual(nativelane.check_ready(self.p), (True, "ready"))
        with open(nativelane.sidecar_path(self.p)) as f:
            self.assertEqual(json.load(f)["source"]["stream_frames"], TOTAL)

    def test_publish_refuses_an_rpu_of_another_length(self):
        nativelane.begin_marker(self.p, nativelane.chunks(self.segdir), FPS, self.cfr_facts())
        with open(nativelane.rpu_path(self.p), "wb") as f:
            f.write(b"rpu")
        with mock.patch.object(nativelane.dvcap, "rpu_frame_count", return_value=TOTAL - 1):
            with self.assertRaisesRegex(RuntimeError, "RPU has 719 frames"):
                nativelane.publish_ready(self.p, _sc())
        self.assertEqual(nativelane.read_marker(self.p)["state"], "analyzing")

    def test_bundle(self):
        r = self.make_ready()
        b = nativelane.bundle(self.p)
        self.assertEqual(b["chunks"], nativelane.chunks(self.segdir))
        self.assertEqual(b["bounds"], [240, 540, 720])
        self.assertEqual((b["total"], b["fps"], b["max_cll"], b["id"]), (TOTAL, FPS, "640,120", r["id"]))
        self.assertEqual(b["vf"], dvnative.lut_vf(self.lut))
        self.assertEqual(b["rpu"], os.path.join(self.segdir, "native", "rpu.bin"))

    def test_no_bundle_unless_ready(self):
        self.assertIsNone(nativelane.bundle(self.p))
        nativelane.begin_marker(self.p, nativelane.chunks(self.segdir), FPS, self.cfr_facts())
        self.assertIsNone(nativelane.bundle(self.p))

    def _assert_not_ready(self, change, words):
        self.make_ready()
        self.assertTrue(nativelane.ready(self.p))
        change()
        ok, why = nativelane.check_ready(self.p)
        self.assertFalse(ok)
        self.assertIn(words, why)
        self.assertIsNone(nativelane.bundle(self.p))

    def test_rejects_a_changed_lut(self):
        self._assert_not_ready(lambda: _append(self.lut, b"0 0 1\n"), "LUT changed")

    def test_rejects_a_changed_rpu(self):
        self._assert_not_ready(lambda: _append(nativelane.rpu_path(self.p), b"x"), "RPU changed")

    def test_rejects_a_missing_rpu(self):
        self._assert_not_ready(lambda: os.remove(nativelane.rpu_path(self.p)), "RPU changed")

    def test_rejects_a_changed_chunk(self):
        self._assert_not_ready(
            lambda: _append(os.path.join(self.segdir, "seg_0001.mov"), b"re-topaz"),
            "chunk seg_0001.mov changed")

    def test_rejects_a_changed_manifest(self):
        def change():
            m = topaz.read_manifest(self.segdir)
            m["fps"] = 25.0
            with open(os.path.join(self.segdir, topaz.MANIFEST), "w") as f:
                json.dump(m, f)
        self._assert_not_ready(change, "segments.json changed")

    def test_rejects_a_deleted_chunk_as_unreadable(self):
        self._assert_not_ready(lambda: os.remove(os.path.join(self.segdir, "seg_0002.mov")),
                               "chunks unreadable")

    def test_rejects_a_changed_or_missing_cfr(self):
        self._assert_not_ready(lambda: _append(self.cfr, b"more"), "CFR source changed")
        os.remove(self.cfr)
        self.assertEqual(nativelane.check_ready(self.p), (False, "the CFR source is missing"))

    def test_rejects_a_hand_edited_marker(self):
        def change():
            m = nativelane.read_marker(self.p)
            m["max_cll"] = "1000,1000"
            with open(nativelane.marker_path(self.p), "w") as f:
                json.dump(m, f)
        self._assert_not_ready(change, "does not hash to its id")

    def test_new_inputs_wipe_the_old_work_same_inputs_keep_it(self):
        cl = nativelane.chunks(self.segdir)
        nativelane.begin_marker(self.p, cl, FPS, self.cfr_facts())
        os.makedirs(nativelane.work_dir(self.p))
        keep = os.path.join(nativelane.work_dir(self.p), "chunk_0000.bin.l1.json")
        open(keep, "w").close()
        nativelane.begin_marker(self.p, cl, FPS, self.cfr_facts())          # a resume: same inputs
        self.assertTrue(os.path.exists(keep))
        with open(os.path.join(self.segdir, "seg_0000.mov"), "ab") as f:
            f.write(b"re-topaz'd")                                     # a different chunk 0
        nativelane.begin_marker(self.p, cl, FPS, self.cfr_facts())
        self.assertFalse(os.path.exists(keep))

    def test_a_ready_marker_that_no_longer_matches_restarts_as_analyzing(self):
        self.make_ready()
        os.remove(nativelane.rpu_path(self.p))
        nativelane.begin_marker(self.p, nativelane.chunks(self.segdir), FPS, self.cfr_facts())
        m = nativelane.read_marker(self.p)
        self.assertEqual(m["state"], "analyzing")
        self.assertNotIn("rpu_sha256", m)


# ---- failures and fallback ---------------------------------------------------------------

class Fallback(Fixture):
    def test_fallback_after_two_failures(self):
        nativelane.begin_marker(self.p, nativelane.chunks(self.segdir), FPS, self.cfr_facts())
        n = nativelane.count_failure(self.p, "native analysis failed: chunk 3 timed out")
        self.assertEqual(n, 1)
        self.assertFalse(nativelane.should_fall_back("native analysis failed: ...", n))
        n = nativelane.count_failure(self.p, "native analysis failed: again")
        self.assertEqual(n, 2)
        self.assertTrue(nativelane.should_fall_back("native analysis failed: again", n))
        nativelane.write_fallback(self.p, "2 native failures")
        self.assertTrue(nativelane.has_fallback(self.p))
        self.assertFalse(nativelane.has_marker(self.p))
        self.assertFalse(os.path.exists(nativelane.work_dir(self.p)))
        self.assertTrue(os.path.exists(os.path.join(self.segdir, "seg_0000.mov")))   # Resolve needs them
        with open(nativelane.fallback_path(self.p)) as f:
            self.assertEqual(json.load(f), {"why": "2 native failures", "failures": 2})
        with mock.patch.object(nativelane, "engine_setting", return_value="native"):
            self.assertEqual(nativelane.decide(self.p, pl=_plan(), mode="dv1000")[0], "resolve")

    def test_native_fallback_reason_falls_back_at_once(self):
        self.assertTrue(nativelane.should_fall_back("native-fallback: toolchain: x", 1))
        self.assertFalse(nativelane.should_fall_back("native-fallback-ish", 1))


# ---- CPU ledger --------------------------------------------------------------------------

class CpuLedger(Fixture):
    def test_totals_and_replacement(self):
        nativelane.record_cpu(self.p, "analysis", "chunk_0000", 12.5, 240)
        t = nativelane.record_cpu(self.p, "analysis", "chunk_0001", 17.5, 300)
        self.assertEqual(t, {"cpu_s": 30.0, "frames": 540, "units": 2, "ms_per_frame": 55.56})
        t = nativelane.record_cpu(self.p, "analysis", "chunk_0001", 7.5, 300)    # re-measured
        self.assertEqual((t["cpu_s"], t["units"]), (20.0, 2))
        nativelane.record_cpu(self.p, "remux", "seg_0000", 100.0, 240)
        self.assertEqual(nativelane.cpu_totals(self.p, "remux")["cpu_s"], 100.0)
        self.assertIn("analysis CPU 20 s over 540 frames", nativelane.cpu_line(self.p, "analysis"))
        self.assertEqual(nativelane.cpu_totals(self.p, "nothing")["ms_per_frame"], None)

    def test_wait4_cpu_measures_a_child(self):
        pr = subprocess.Popen([sys.executable, "-c", "import time\nt=time.process_time()\n"
                               "while time.process_time()-t<0.2: pass"])
        self.assertGreater(nativelane.wait4_cpu(pr), 0.1)
        self.assertEqual(pr.returncode, 0)


# ---- the stage body ----------------------------------------------------------------------

class _RunAnalysisBase(Fixture):
    """The stage body with its probes, RPU and analysis stubbed (no tests of its own)."""

    def setUp(self):
        super().setUp()
        self.calls = {}
        self.progress = []
        self.extra = [
            mock.patch.object(nativelane, "toolchain_ok", return_value=(True, "ok")),
            mock.patch.object(nativelane.topaz, "_frame_count", return_value=TOTAL),
            mock.patch.object(nativelane, "fps_fraction", return_value=FPS),
            mock.patch.object(nativelane.dvcap, "rpu_frame_count", return_value=TOTAL),
            mock.patch.object(nativelane.dvnative, "generate_rpu", side_effect=self.generate),
        ]
        for pt in self.extra:
            pt.start()

    def tearDown(self):
        for pt in self.extra:
            pt.stop()
        super().tearDown()

    def generate(self, sc, rpu, **kw):
        self.calls["generate"] = kw
        with open(rpu, "wb") as f:
            f.write(b"generated-rpu")
        return {}

    def analyze(self, chunk_list, workdir, **kw):
        self.calls["analyze"] = dict(kw, chunks=chunk_list, workdir=workdir)
        kw["on_cpu"](0, 3.0, 240)
        for i in range(1, 4):
            kw["on_chunk"](i, 3)
        return _sc()

    def run_it(self, analyze=None):
        with mock.patch.object(nativelane.dvnative, "analyze_chunks", side_effect=analyze or self.analyze):
            return nativelane.run_analysis(self.p, progress=self.progress.append,
                                           should_pause=lambda: False)


class RunAnalysis(_RunAnalysisBase):
    def test_success_publishes_a_ready_marker(self):
        ok, msg = self.run_it()
        self.assertTrue(ok, msg)
        self.assertIn("720 frames", msg)
        self.assertTrue(nativelane.ready(self.p))
        a = self.calls["analyze"]
        self.assertEqual(a["workdir"], os.path.join(self.segdir, "native", "work"))
        self.assertEqual(a["cube"], self.lut)
        self.assertEqual(a["analyzer"], self.an)
        self.assertEqual(a["chunks"][-1], [TOTAL, None])
        self.assertTrue(callable(a["before_chunk"]))
        self.assertTrue(callable(a["should_pause"]))
        self.assertEqual(self.calls["generate"]["avg_floor"], "V40")
        self.assertEqual([d["seg_done"] for d in self.progress], [0, 1, 2, 3])
        self.assertEqual(self.progress[-1]["pct"], 100)
        self.assertTrue(all(d["stage"] == "dvnative" for d in self.progress))
        self.assertEqual(nativelane.cpu_totals(self.p, "analysis")["cpu_s"], 3.0)
        self.assertEqual(self.run_it(), (True, "native DV metadata already ready"))

    def test_a_first_claim_toolchain_failure_is_never_a_fallback(self):
        tool = mock.Mock(return_value=(False, "dovi_tool is 2.2.0"))
        with mock.patch.object(nativelane, "toolchain_ok", tool):
            ok, msg = self.run_it()
        self.assertFalse(ok)
        self.assertFalse(msg.startswith("native-fallback:"), msg)
        self.assertIn("not claimed", msg)
        self.assertIn("dovi_tool is 2.2.0", msg)
        self.assertFalse(nativelane.should_fall_back(msg, 1))   # no fallback.json from this
        self.assertFalse(nativelane.has_marker(self.p))
        self.assertNotIn("analyze", self.calls)
        self.assertEqual(tool.call_args_list[0], mock.call(max_age=nativelane.CLAIM_TOOLCHAIN_SECONDS))
        self.assertEqual(tool.call_args_list[-1], mock.call(force=True))   # re-probed first

    def test_after_a_first_claim_toolchain_failure_decide_says_resolve(self):
        # the real cache: decide's pass is 2 minutes old, the toolchain broke since. The stage
        # re-checks, fails without claiming, and the next pass's decide() reads that failure.
        import time as _time
        nativelane._TOOLCHAIN.update(at=_time.monotonic() - 120, result=(True, "ok"))
        self.addCleanup(nativelane._TOOLCHAIN.update, at=None, result=None)
        probe = mock.Mock(return_value=["ffmpeg major is 9, pinned 8"])
        with mock.patch.object(nativelane, "toolchain_ok", REAL_TOOLCHAIN_OK), \
             mock.patch.object(nativelane, "toolchain_problems", probe):
            ok, msg = self.run_it()
            self.assertFalse(ok)
            self.assertFalse(msg.startswith("native-fallback:"), msg)
            with mock.patch.object(nativelane, "engine_setting", return_value="native"), \
                 mock.patch.object(nativelane, "probe_stream", return_value=Decide.COLOUR), \
                 mock.patch.object(nativelane, "disk_fits", return_value=(True, "room")):
                engine, why = nativelane.decide(self.p, pl=_plan(), mode="dv1000")
        self.assertEqual(engine, "resolve")
        self.assertIn("ffmpeg major is 9", why)
        self.assertFalse(nativelane.has_fallback(self.p))
        self.assertFalse(nativelane.has_marker(self.p))

    def test_a_first_claim_trusts_the_claim_check_decide_just_made(self):
        tool = mock.Mock(return_value=(True, "toolchain matches the pins"))
        with mock.patch.object(nativelane, "toolchain_ok", tool):
            ok, msg = self.run_it()
        self.assertTrue(ok, msg)
        self.assertEqual(tool.call_args_list, [mock.call(max_age=nativelane.CLAIM_TOOLCHAIN_SECONDS)])

    def test_cfr_mismatch_falls_back_without_claiming(self):
        with mock.patch.object(nativelane, "fps_fraction", side_effect=["24/1", FPS]):
            ok, msg = self.run_it()
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("native-fallback:"), msg)
        self.assertFalse(nativelane.has_marker(self.p))

    def test_chunks_short_of_the_cfr_fall_back(self):
        with mock.patch.object(nativelane.topaz, "_frame_count", return_value=TOTAL + 9):
            ok, msg = self.run_it()
        self.assertTrue(msg.startswith("native-fallback: chunks"), msg)

    def test_pause_and_abort_are_not_failures(self):
        def pause(*a, **k):
            raise dvnative.Paused("paused: low disk — under the floor")

        def abort(*a, **k):
            raise dvnative.Aborted("aborted (run stopped) during chunk 2 analyzer")
        self.assertEqual(self.run_it(pause), (False, "paused: low disk — under the floor"))
        self.assertEqual(nativelane.read_marker(self.p)["state"], "analyzing")   # claimed, resumes
        self.assertEqual(self.run_it(abort), (False, "aborted (run stopped)"))

    def test_analysis_and_rpu_errors_are_plain_failures(self):
        def broken(*a, **k):
            raise RuntimeError("chunk 1: analyzer failed: boom")
        ok, msg = self.run_it(broken)
        self.assertEqual(msg, "native analysis failed: chunk 1: analyzer failed: boom")
        self.assertFalse(nativelane.should_fall_back(msg, 1))
        with mock.patch.object(nativelane.dvcap, "rpu_frame_count", return_value=TOTAL - 3):
            ok, msg = self.run_it()
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("native RPU failed"), msg)
        self.assertFalse(nativelane.ready(self.p))


class RunAnalysisResume(_RunAnalysisBase):
    """A RESUME (an 'analyzing' marker from an earlier pass, chunk results in native/work): a
    probe that fails is an ORDINARY failure — counted, retried next pass, a fallback only by
    the 2-failure rule, and every measured chunk kept. Only a definitive mismatch falls back
    at once: the analyzer build or LUT changed (sha256 vs the marker), or the chunk set did."""

    def claim(self):
        """The state a yield / relaunch / 'paused: low disk' leaves: marker + 1 measured chunk."""
        nativelane.begin_marker(self.p, nativelane.chunks(self.segdir), FPS, self.cfr_facts())
        os.makedirs(nativelane.work_dir(self.p))
        self.measured = os.path.join(nativelane.work_dir(self.p), "chunk_0000.bin.l1.json")
        with open(self.measured, "w") as f:
            f.write("{}")

    def assert_ordinary(self, msg):
        self.assertFalse(msg.startswith(nativelane.FALLBACK_PREFIX), msg)
        self.assertFalse(nativelane.should_fall_back(msg, 1), msg)    # attempt 1: a retry
        self.assertTrue(nativelane.should_fall_back(msg, 2), msg)     # the 2-failure rule
        self.assertEqual(nativelane.read_marker(self.p)["state"], "analyzing")   # still claimed
        self.assertTrue(os.path.exists(self.measured))                # nothing measured is lost
        self.assertNotIn("analyze", self.calls)

    def assert_definitive(self, msg, words):
        self.assertTrue(msg.startswith(nativelane.FALLBACK_PREFIX), msg)
        self.assertTrue(nativelane.should_fall_back(msg, 1), msg)
        self.assertIn(words, msg)
        self.assertNotIn("analyze", self.calls)

    def test_a_toolchain_probe_failure_is_counted_not_a_fallback(self):
        self.claim()
        tool = mock.Mock(return_value=(False, "hdr_analyzer_mvp does not run: no answer within 10 s"))
        with mock.patch.object(nativelane, "toolchain_ok", tool):
            ok, msg = self.run_it()
        self.assertFalse(ok)
        self.assert_ordinary(msg)
        self.assertIn("while resuming", msg)
        self.assertIn("no answer within 10 s", msg)
        self.assertEqual(tool.call_args_list,                         # a cached pass is trusted,
                         [mock.call(max_age=nativelane.TOOLCHAIN_CACHE_SECONDS),
                          mock.call(force=True)])                    # a failure re-probed

    def test_dovi_tool_or_ffmpeg_drifting_is_left_to_the_2_failure_rule(self):
        self.claim()
        with mock.patch.object(nativelane, "toolchain_ok",
                               return_value=(False, "ffmpeg major is 9, pinned 8")):
            ok, msg = self.run_it()
        self.assert_ordinary(msg)

    def test_a_changed_analyzer_build_falls_back_at_once(self):
        self.claim()
        _append(self.an, b"# rebuilt\n")
        with mock.patch.object(nativelane, "toolchain_ok",
                               return_value=(False, "hdr_analyzer_mvp is not the pinned build")):
            ok, msg = self.run_it()
        self.assert_definitive(msg, "hdr_analyzer_mvp build changed since this item's analysis")

    def test_a_missing_analyzer_falls_back_at_once(self):
        self.claim()
        os.remove(self.an)
        with mock.patch.object(nativelane, "toolchain_ok",
                               return_value=(False, "hdr_analyzer_mvp is missing")):
            ok, msg = self.run_it()
        self.assert_definitive(msg, "hdr_analyzer_mvp build changed")

    def test_a_changed_lut_falls_back_at_once(self):
        self.claim()
        _append(self.lut, b"0 0 1\n")
        with mock.patch.object(nativelane, "toolchain_ok",
                               return_value=(False, "the LUT is not the pinned file")):
            ok, msg = self.run_it()
        self.assert_definitive(msg, "toolchain: the LUT changed")

    def test_an_unknown_cfr_frame_count_is_counted_not_a_fallback(self):
        self.claim()
        with mock.patch.object(nativelane.topaz, "_frame_count", return_value=None):
            ok, msg = self.run_it()
        self.assertIn("the CFR's frame count is unknown", msg)
        self.assert_ordinary(msg)

    def test_an_unknown_frame_rate_is_counted_not_a_fallback(self):
        self.claim()
        with mock.patch.object(nativelane, "fps_fraction", return_value=None):
            ok, msg = self.run_it()
        self.assertIn("frame rate unknown", msg)
        self.assert_ordinary(msg)

    def test_a_cfr_that_disagrees_with_the_same_chunks_is_counted(self):
        self.claim()
        with mock.patch.object(nativelane.topaz, "_frame_count", return_value=TOTAL + 9):
            ok, msg = self.run_it()
        self.assertTrue(msg.startswith("chunks: the chunks hold"), msg)
        self.assert_ordinary(msg)

    def test_a_deleted_chunk_falls_back_at_once(self):
        self.claim()
        os.remove(os.path.join(self.segdir, "seg_0001.mov"))
        ok, msg = self.run_it()
        self.assert_definitive(msg, "seg_0001.mov is gone since the analysis began")

    def test_a_rewritten_manifest_falls_back_at_once(self):
        self.claim()
        m = topaz.read_manifest(self.segdir)
        m["segments"][1]["start"] += 1                       # a gap: the chunk set changed
        with open(os.path.join(self.segdir, topaz.MANIFEST), "w") as f:
            json.dump(m, f)
        ok, msg = self.run_it()
        self.assert_definitive(msg, "segments.json changed since the analysis began")

    def test_the_first_claim_still_falls_back_on_a_cfr_misfit(self):
        # no marker: the item's own inputs don't fit the lane, nothing is measured to keep
        with mock.patch.object(nativelane.topaz, "_frame_count", return_value=None):
            ok, msg = self.run_it()
        self.assertTrue(msg.startswith(nativelane.FALLBACK_PREFIX), msg)
        self.assertFalse(nativelane.has_marker(self.p))

    def test_a_hiccup_then_a_clean_pass_resumes_with_the_measured_chunks(self):
        self.claim()
        with mock.patch.object(nativelane, "fps_fraction", return_value=None):
            ok, msg = self.run_it()
        n = nativelane.count_failure(self.p, msg)            # what the stage does with it
        self.assertFalse(nativelane.should_fall_back(msg, n))
        seen = {}

        def analyze(chunk_list, workdir, **kw):
            seen["kept"] = os.path.exists(self.measured)
            return _sc()
        ok, msg = self.run_it(analyze)
        self.assertTrue(ok, msg)
        self.assertTrue(seen["kept"])                         # resumed, not re-measured
        self.assertTrue(nativelane.ready(self.p))

    def test_a_second_ordinary_failure_falls_back(self):
        self.claim()
        for attempt in (1, 2):
            with mock.patch.object(nativelane, "toolchain_ok",
                                   return_value=(False, "dovi_tool is missing, pinned 2.3.0")):
                ok, msg = self.run_it()
            n = nativelane.count_failure(self.p, msg)
            self.assertEqual(n, attempt)
            self.assertEqual(nativelane.should_fall_back(msg, n), attempt == 2)


if __name__ == "__main__":
    unittest.main()
