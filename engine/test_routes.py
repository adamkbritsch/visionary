"""routes.classify — the golden matrix. Every source shape plan.choose_plan knows, as a TV
episode, a movie and a YouTube video, in every output mode, plus the companion combine, the
DV lanes and the TV lane's disk fallback. Each row's plan comes from the REAL choose_plan, and
each row re-asks the engine choices that exist today (the plan itself, nativelane.eligible,
stages' YouTube SuperScale and bitrate rules), so the names provably sit on top of unchanged
decisions. classify is pure and inert: it reads nothing, writes nothing and never raises."""
import builtins
import inspect
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

import nativelane
import plan
import routes
import settings
import stages


def info(w, h, transfer=None, codec="h264", pix_fmt="yuv420p", cfr=True, dv=False):
    """A plan.probe_input result."""
    return {"width": w, "height": h, "sar": "", "is_4k": w >= 3840 or h >= 2160,
            "is_hdr": transfer in plan.HDR_TRANSFERS, "is_dv": dv, "codec": codec,
            "transfer": transfer, "pix_fmt": pix_fmt, "is_cfr": cfr, "video_kbps": 8000}


PQ, HLG, SDR = "smpte2084", "arib-std-b67", "bt709"

# name -> (probe, the (topaz, resolve) choose_plan gives it TODAY)
SOURCES = {
    "480p_sdr": (info(720, 480), ("upscale", "add_hdr_dv")),
    "576p_sdr": (info(720, 576), ("upscale", "add_hdr_dv")),
    "720p_sdr": (info(1280, 720), ("upscale", "add_hdr_dv")),
    "1080p_sdr": (info(1920, 1080), ("upscale", "add_hdr_dv")),
    "1200_sdr": (info(1920, 1200), ("upscale", "add_hdr_dv")),
    "1440p_sdr": (info(2560, 1440), ("upscale", "add_hdr_dv")),
    "unprobed": (info(0, 0, codec=None, pix_fmt=None, cfr=False), ("upscale", "add_hdr_dv")),
    "vertical_608": (info(608, 1080), ("upscale", "add_hdr_dv")),
    "vertical_1080": (info(1080, 1920), ("upscale", "add_hdr_dv")),
    "1080p_pq": (info(1920, 1080, PQ, "hevc", "yuv420p10le"), ("upscale", "add_dv")),
    "1080p_hlg": (info(1920, 1080, HLG, "hevc", "yuv420p10le"), ("upscale", "add_dv")),
    "4k_hdr10": (info(3840, 2160, PQ, "hevc", "yuv420p10le"), ("rpu-only", "add_dv")),
    "4k_hdr10_vfr": (info(3840, 2160, PQ, "hevc", "yuv420p10le", cfr=False),
                     ("resolve-only", "add_dv")),
    "4k_pq_av1": (info(3840, 2160, PQ, "av1", "yuv420p10le"), ("resolve-only", "add_dv")),
    "4k_pq_8bit": (info(3840, 2160, PQ, "hevc", "yuv420p"), ("resolve-only", "add_dv")),
    "4k_hlg": (info(3840, 2160, HLG, "hevc", "yuv420p10le"), ("resolve-only", "add_dv")),
    "4k_sdr": (info(3840, 2160, SDR, "av1"), ("resolve-only", "add_hdr_dv")),
    "4k_sdr_vfr": (info(3840, 2160, SDR, "h264", cfr=False), ("resolve-only", "add_hdr_dv")),
    "dci_4k_sdr": (info(4096, 2160, SDR, "vp9"), ("resolve-only", "add_hdr_dv")),
    "scope_4k_sdr": (info(3840, 1600, SDR, "av1"), ("resolve-only", "add_hdr_dv")),
    "vertical_4k": (info(2160, 3840, SDR, "vp9"), ("resolve-only", "add_hdr_dv")),
    "dv": (info(3840, 2160, PQ, "hevc", "yuv420p10le", dv=True), ("skip", "skip")),
}

# name -> the route as (TV episode, movie, YouTube video), output mode auto/dv1000
GOLDEN = {
    "480p_sdr": ("sd_sdr_topaz", "movie_sdr_topaz", "yt_plain"),
    "576p_sdr": ("sd_sdr_topaz", "movie_sdr_topaz", "yt_plain"),
    "720p_sdr": ("tv_sdr_topaz", "movie_sdr_topaz", "yt_plain"),
    "1080p_sdr": ("tv_sdr_topaz", "movie_sdr_topaz", "yt_1080"),
    "1200_sdr": ("tv_sdr_topaz", "movie_sdr_topaz", "yt_1080"),
    "1440p_sdr": ("tv_sdr_topaz", "movie_sdr_topaz", "yt_plain"),
    "unprobed": ("tv_sdr_topaz", "movie_sdr_topaz", "yt_plain"),
    "vertical_608": ("tv_sdr_topaz", "movie_sdr_topaz", "yt_1080"),
    "vertical_1080": ("tv_sdr_topaz", "movie_sdr_topaz", "yt_plain"),
    "1080p_pq": ("hdr_topaz", "hdr_topaz", "hdr_reencode"),
    "1080p_hlg": ("hdr_topaz", "hdr_topaz", "hlg"),
    "4k_hdr10": ("rpu_only", "rpu_only", "rpu_only"),
    "4k_hdr10_vfr": ("hdr_reencode", "hdr_reencode", "hdr_reencode"),
    "4k_pq_av1": ("hdr_reencode", "hdr_reencode", "hdr_reencode"),
    "4k_pq_8bit": ("hdr_reencode", "hdr_reencode", "hdr_reencode"),
    "4k_hlg": ("hlg", "hlg", "hlg"),
    "4k_sdr": ("sdr4k", "sdr4k", "yt_native4k"),
    "4k_sdr_vfr": ("sdr4k", "sdr4k", "yt_native4k"),
    "dci_4k_sdr": ("sdr4k", "sdr4k", "yt_native4k"),
    "scope_4k_sdr": ("sdr4k", "sdr4k", "yt_plain"),
    "vertical_4k": ("sdr4k", "sdr4k", "yt_plain"),
    "dv": ("already_dv", "already_dv", "already_dv"),
}
KINDS = ("tv", "movie", "youtube")


def item(kind, combine=False):
    return types.SimpleNamespace(movie=kind in ("movie", "combine"), youtube=kind == "youtube",
                                 combine=combine or kind == "combine", series="Show",
                                 source="/nowhere/src.mkv", source_basename="src.mkv")


def plan_of(name):
    """The REAL plan.plan_for result for the source, without the probe (or its cache)."""
    pl = plan.choose_plan(SOURCES[name][0])
    pl["input"] = SOURCES[name][0]
    return pl


def matrix():
    """Every (kind, source, mode) row with the route it must classify as."""
    for name, routes_by_kind in GOLDEN.items():
        for kind, want in zip(KINDS, routes_by_kind):
            # "dv2000" is RETIRED (2026-10-18): a stale pin names the item exactly as "auto" does
            for mode in ("auto", "dv1000", None, "junk", "dv2000", "sdr"):
                if mode == "sdr" and want not in routes.RESOLVE_FREE:
                    want_m = "sdr_out"
                else:
                    want_m = want
                yield kind, name, mode, want_m


class GoldenMatrix(unittest.TestCase):

    def test_the_plan_under_every_row_is_todays(self):
        # The routes only NAME the plan's choices: if plan.py ever routes a source
        # differently, this table — and the names on top of it — must be revisited.
        self.assertEqual(set(SOURCES), set(GOLDEN))
        for name, (_probe, want) in SOURCES.items():
            pl = plan_of(name)
            self.assertEqual((pl["topaz"], pl["resolve"]), want, name)

    def test_every_row(self):
        for kind, name, mode, want in matrix():
            got = routes.classify(item(kind), plan_of(name), mode)
            self.assertEqual(got, want, f"{kind} {name} mode={mode}")

    def test_the_native_lane_takes_exactly_the_tv_sdr_topaz_rows(self):
        # nativelane.eligible is the one engine choice that exists for a route today; it must
        # say yes to tv_sdr_topaz and to nothing else, in every output mode.
        for kind, name, mode, want in matrix():
            p, pl = item(kind), plan_of(name)
            lane = nativelane.eligible(p, pl, routes.normalize_mode(mode))[0]
            self.assertEqual(lane, want == "tv_sdr_topaz", f"{kind} {name} mode={mode}")
            if p.movie or p.youtube:
                self.assertFalse(lane)

    def test_the_youtube_split_is_stages_own(self):
        # yt_1080 is exactly the set stages._resolve SuperScales; yt_native4k always renders
        # in the 14 Mbps bucket. (The rule and the constant are pinned to stages below.)
        for name, (probe, _plan) in SOURCES.items():
            route = GOLDEN[name][2]
            if not route.startswith("yt_"):
                continue
            h = probe["height"]
            self.assertEqual(route == "yt_1080", 900 <= h <= 1200, name)
            if route == "yt_native4k":
                self.assertEqual(stages.youtube_render_kbps(h), stages.YOUTUBE_RENDER_KBPS_4K)
            if route == "yt_1080":
                self.assertEqual(stages.youtube_render_kbps(h), stages.YOUTUBE_RENDER_KBPS)

    def test_hdr_never_lands_in_a_yt_route(self):
        # Every yt_* engine runs the BT.709 SDR LUT.
        for kind, name, mode, want in matrix():
            if SOURCES[name][0]["is_hdr"] and want.startswith("yt_"):
                self.fail(f"{kind} {name} -> {want}")

    def test_every_route_in_the_table_is_reachable(self):
        seen = {want for _k, _n, _m, want in matrix()}
        seen |= {routes.classify(item("combine"), plan_of("4k_hdr10"), "auto",
                                 {"verdict": {"rpu_from": rf}}) for rf in ("nas", "resolve")}
        seen.add(routes.classify(item("movie"), None, "auto", {"dv_profile": 7}))
        seen.add(routes.classify(item("tv"), plan_of("1080p_sdr"), "auto", {"disk_fits": False}))
        self.assertEqual(seen, set(routes.ROUTES))
        for r in routes.ROUTES:
            self.assertTrue(routes.describe(r))


class Combine(unittest.TestCase):
    """The verdict outranks the plan: a real RPU from either copy is Resolve-free whatever the
    plan or the mode says (even a DV library copy, which the plan would 'skip')."""

    def test_a_real_rpu_is_resolve_free_in_any_mode(self):
        for rf in ("nas", "remote"):
            for name in ("dv", "4k_hdr10", "4k_sdr", "1080p_sdr"):
                for mode in ("auto", "dv2000", "sdr"):
                    self.assertEqual(
                        routes.classify(item("combine"), plan_of(name), mode,
                                        {"verdict": {"rpu_from": rf, "rpu_profile": "7.6"}}),
                        "combine_rpu")

    def test_no_rpu_follows_the_pin(self):
        v = {"verdict": {"rpu_from": "resolve"}}
        for mode, want in (("auto", "combine_nodv"), ("dv1000", "combine_nodv"),
                           ("dv2000", "combine_nodv"), ("sdr", "sdr_out")):
            self.assertEqual(routes.classify(item("combine"), plan_of("4k_sdr"), mode, v), want)

    def test_a_lost_verdict_names_nothing(self):
        for facts in (None, {}, {"verdict": None}, {"verdict": {}}, {"verdict": "x"}):
            self.assertEqual(routes.classify(item("combine"), plan_of("4k_hdr10"), "auto", facts),
                             routes.UNKNOWN)

    def test_the_lane_never_takes_a_combine(self):
        self.assertFalse(nativelane.eligible(item("combine"), plan_of("1080p_sdr"), "dv1000")[0])


class DolbyVisionLanes(unittest.TestCase):

    def test_profile_7_is_the_dvp7_lane(self):
        for prof in (7, "7", "7.6", "7.x", 7.6):
            self.assertEqual(routes.classify(item("movie"), plan_of("dv"), "auto",
                                             {"dv_profile": prof}), "dvp7")
            self.assertEqual(routes.classify(item("movie"), None, "dv2000",
                                             {"dv_profile": prof}), "dvp7")

    def test_other_profiles_are_already_dv_in_any_mode(self):
        for prof in (None, 8, "8.1", 5):
            for mode in ("auto", "dv2000", "sdr"):
                self.assertEqual(routes.classify(item("tv"), plan_of("dv"), mode,
                                                 {"dv_profile": prof}), "already_dv")

    def test_junk_profiles_do_not_claim_dv(self):
        for prof in ("resolve", "", 0, True):
            self.assertEqual(routes.classify(item("tv"), plan_of("720p_sdr"), "auto",
                                             {"dv_profile": prof}), "tv_sdr_topaz")


class TVLaneDiskFallback(unittest.TestCase):

    def test_a_declined_tv_item_is_the_disk_fallback(self):
        for name in ("720p_sdr", "1080p_sdr", "1440p_sdr"):
            self.assertEqual(routes.classify(item("tv"), plan_of(name), "auto",
                                             {"disk_fits": False}), "tv_disk_fallback")

    def test_only_an_explicit_no(self):
        for fits in (True, None, 0, ""):
            self.assertEqual(routes.classify(item("tv"), plan_of("1080p_sdr"), "auto",
                                             {"disk_fits": fits}), "tv_sdr_topaz")

    def test_it_never_renames_another_route(self):
        no = {"disk_fits": False}
        self.assertEqual(routes.classify(item("movie"), plan_of("1080p_sdr"), "auto", no),
                         "movie_sdr_topaz")
        self.assertEqual(routes.classify(item("tv"), plan_of("480p_sdr"), "auto", no),
                         "sd_sdr_topaz")
        self.assertEqual(routes.classify(item("tv"), plan_of("1080p_sdr"), "sdr", no),
                         "sdr_out")
        self.assertEqual(routes.classify(item("tv"), plan_of("1080p_sdr"), "dv2000", no),
                         "tv_disk_fallback")                    # the retired pin is dv1000


class Facts(unittest.TestCase):

    def test_facts_override_the_probe(self):
        pl = plan_of("1080p_sdr")
        self.assertEqual(routes.classify(item("youtube"), pl, "auto", {"height": 2160,
                                                                       "width": 3840}),
                         "yt_native4k")
        pl = plan_of("4k_pq_av1")
        self.assertEqual(routes.classify(item("youtube"), pl, "auto", {"transfer": HLG}), "hlg")

    def test_a_bare_plan_without_a_probe(self):
        # The tests elsewhere stub plan_for with bare dicts; a missing probe reads as 0 lines.
        self.assertEqual(routes.classify(item("youtube"), {"topaz": "upscale",
                                                           "resolve": "add_hdr_dv"}, "auto"),
                         "yt_plain")
        self.assertEqual(routes.classify(item("tv"), {"topaz": "rpu-only"}, "auto"), "rpu_only")
        self.assertEqual(routes.classify(item("tv"), {"topaz": "resolve-only",
                                                      "resolve": "add_dv"}, "auto"),
                         "hdr_reencode")

    def test_youtube_geometry_edges(self):
        def yt(w, h):
            return routes.classify(item("youtube"),
                                   {**plan.choose_plan(info(w, h)), "input": info(w, h)}, "auto")
        self.assertEqual(yt(1600, 899), "yt_plain")
        self.assertEqual(yt(1600, 900), "yt_1080")
        self.assertEqual(yt(2134, 1200), "yt_1080")
        self.assertEqual(yt(2134, 1201), "yt_plain")
        self.assertEqual(yt(3022, 1699), "yt_plain")
        self.assertEqual(yt(3022, 1700), "yt_native4k")
        self.assertEqual(yt(1700, 1700), "yt_native4k")       # square counts as landscape
        self.assertEqual(yt(1699, 1700), "yt_plain")          # vertical: needs a fit and pad


class NeverRaisesAndPure(unittest.TestCase):

    def test_garbage_is_unknown(self):
        for p in (None, object(), "x", item("tv")):
            for pl in (None, {}, "plan", [], {"topaz": "weird"}, {"topaz": "upscale"},
                       {"topaz": "upscale", "res": "8k"}, {"input": "junk", "topaz": "upscale"}):
                for facts in (None, {}, "facts", {"height": "tall"}):
                    got = routes.classify(p, pl, "auto", facts)
                    self.assertIn(got, set(routes.ROUTES) | {routes.UNKNOWN})
        self.assertEqual(routes.classify(item("tv"), None, "auto"), routes.UNKNOWN)
        self.assertEqual(routes.describe(routes.UNKNOWN), "not enough facts to name a route")
        self.assertEqual(routes.describe("nope"), "")

    def test_an_internal_error_is_unknown(self):
        with mock.patch.object(routes, "_classify", side_effect=RuntimeError("bug")):
            self.assertEqual(routes.classify(item("tv"), plan_of("720p_sdr"), "auto"),
                             routes.UNKNOWN)

    def test_it_touches_no_file_process_or_setting(self):
        plans = {name: plan_of(name) for name in SOURCES}       # built before the traps
        rows = list(matrix())
        trap = AssertionError("touched")
        with mock.patch.object(builtins, "open", side_effect=trap), \
             mock.patch.object(subprocess, "run", side_effect=trap), \
             mock.patch.object(subprocess, "Popen", side_effect=trap), \
             mock.patch.object(os.path, "exists", side_effect=trap), \
             mock.patch.object(os, "stat", side_effect=trap), \
             mock.patch.object(settings, "get_settings", side_effect=trap), \
             mock.patch.object(settings, "get_show_output_mode", side_effect=trap):
            got = [routes.classify(item(k), plans[n], m) for k, n, m, _w in rows]
        self.assertEqual(got, [w for _k, _n, _m, w in rows])

    def test_it_does_not_mutate_its_inputs(self):
        pl, facts = plan_of("1080p_sdr"), {"disk_fits": False}
        before = (repr(pl), repr(facts))
        routes.classify(item("tv"), pl, "auto", facts)
        self.assertEqual((repr(pl), repr(facts)), before)


class PinnedToTheStages(unittest.TestCase):
    """The constants classify mirrors are the stages' own. If one of these fails, the stage
    changed a rule: revisit routes.py with it, never just the number."""

    def test_youtube_rules(self):
        self.assertEqual(routes.YT_NATIVE_4K_MIN_HEIGHT, stages.YOUTUBE_NATIVE_4K_MIN_HEIGHT)
        rule = (f"if {routes.YT_SUPERSCALE_MIN_HEIGHT} <= h <= "
                f"{routes.YT_SUPERSCALE_MAX_HEIGHT}:\n")
        src = inspect.getsource(stages._resolve)
        self.assertIn(rule, src)
        self.assertIn('ss = "2"', src.split(rule, 1)[1].splitlines()[0])

    def test_lane_and_plan(self):
        self.assertEqual(routes.TV_LANE_RES, nativelane.ELIGIBLE_RES)
        self.assertIn(routes.HLG, plan.HDR_TRANSFERS)
        self.assertEqual(plan.resolution_bucket(576), routes.SD_RES)
        self.assertEqual(plan.resolution_bucket(720), "720p")

    def test_modes(self):
        self.assertEqual(set(routes.MODES) | {"auto"}, set(settings.OUTPUT_MODES))
        self.assertNotIn("dv2000", routes.ROUTES)               # retired 2026-10-18: no such route
        self.assertEqual(routes.normalize_mode("dv2000"), "dv1000")
        for key in ("auto", "dv1000", "dv2000", "sdr"):
            with mock.patch.object(settings, "get_show_output_mode", return_value=key):
                self.assertEqual(routes.normalize_mode(key), settings.effective_output_mode("k"))
        self.assertEqual(routes.normalize_mode(None), "dv1000")


class ItemRoute(unittest.TestCase):
    """routes.item_route — the live facts behind the orchestrator's log line. Never raises."""

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.src = os.path.join(self.td, "Show - S01E01.mkv")
        with open(self.src, "w") as f:
            f.write("x")
        cache = mock.patch.object(plan, "PROBE_CACHE", os.path.join(self.td, "probe_cache.json"))
        cache.start()                       # item_route must never write it; never the real one
        self.addCleanup(cache.stop)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _p(self, **kw):
        base = dict(movie=False, youtube=False, combine=False, series="Show", source=self.src,
                    source_basename=os.path.basename(self.src))
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_not_local_yet_probes_nothing(self):
        with mock.patch.object(plan, "probe_input", side_effect=AssertionError("probed")):
            self.assertIsNone(routes.item_route(self._p(source=self.src + ".gone")))
            self.assertIsNone(routes.item_route(self._p(source="")))

    def test_a_tv_episode(self):
        with mock.patch.object(plan, "probe_input", return_value=SOURCES["1080p_sdr"][0]) as pi, \
             mock.patch.object(plan, "_remember_probe", side_effect=AssertionError("cache")), \
             mock.patch.object(settings, "get_show_output_mode", return_value="auto") as gm:
            self.assertEqual(routes.item_route(self._p()), "tv_sdr_topaz")
        pi.assert_called_once_with(self.src)
        gm.assert_called_once_with("Show")

    def test_the_pin_is_read_under_the_stages_key(self):
        p = self._p(youtube=True, series="Chan",
                    source_basename="Chan - A [aaaaaaaaaa1].mp4")
        import youtube
        scope = {"aaaaaaaaaa1": "import:7"}
        with mock.patch.object(plan, "probe_input", return_value=SOURCES["4k_sdr"][0]), \
             mock.patch.object(youtube, "settings_scope_for_vid", side_effect=scope.get), \
             mock.patch.object(settings, "get_show_output_mode",
                               side_effect=lambda k: "sdr" if k == "import:7" else "auto"):
            self.assertEqual(routes.item_route(p), "sdr_out")
            self.assertEqual(stages._settings_scope(p), "import:7")

    def test_a_combine_reads_its_confirmed_verdict(self):
        import companion
        p = self._p(movie=True, combine=True)
        for cv, want in (({"verdict": {"rpu_from": "resolve"}, "companion": {}}, "combine_nodv"),
                         ({"verdict": {"rpu_from": "remote"}, "companion": {}}, "combine_rpu"),
                         (None, routes.UNKNOWN)):
            with mock.patch.object(plan, "probe_input", return_value=SOURCES["dv"][0]), \
                 mock.patch.object(companion, "confirmed_verdict", return_value=cv) as v, \
                 mock.patch.object(settings, "get_show_output_mode", return_value="auto"):
                self.assertEqual(routes.item_route(p), want)
            v.assert_called_once_with(p.source_basename)

    def test_it_never_raises(self):
        boom = RuntimeError("boom")
        for target, name in ((plan, "probe_input"), (plan, "choose_plan"),
                             (settings, "get_show_output_mode"), (routes, "classify")):
            with mock.patch.object(plan, "probe_input", return_value=SOURCES["720p_sdr"][0]), \
                 mock.patch.object(settings, "get_show_output_mode", return_value="auto"), \
                 mock.patch.object(target, name, side_effect=boom):
                self.assertIsNone(routes.item_route(self._p()), name)
        self.assertIsNone(routes.item_route(None))


if __name__ == "__main__":
    unittest.main()
