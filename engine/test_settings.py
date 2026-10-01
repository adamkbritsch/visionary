import os
import tempfile
import unittest
from unittest import mock

import plan
import settings
import topaz


class Presets(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.multiple(
            settings, CONFIG_DIR=self.d,
            SETTINGS_FILE=os.path.join(self.d, "settings.json"),
            PROFILES_FILE=os.path.join(self.d, "show_profiles.json"))
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_catalog_has_content_type_presets_incl_2d_animation(self):
        keys = [c["key"] for c in settings.preset_catalog()]
        self.assertIn("digital", keys)
        self.assertIn("film", keys)
        self.assertIn("animation2d", keys)        # 2D animation preset exists

    def test_unconfigured_show_uses_default_preset(self):
        self.assertIsNone(settings.get_show_preset("Rick and Morty"))
        self.assertEqual(settings.show_preset_key("Rick and Morty"), "digital")

    def test_assign_and_persist_per_show(self):
        settings.set_show_preset("Rick and Morty", "animation2d")
        self.assertEqual(settings.get_show_preset("Rick and Morty"), "animation2d")
        self.assertIsNone(settings.get_show_preset("The Office"))   # other shows unaffected

    def test_unknown_preset_falls_back_to_default(self):
        self.assertEqual(settings.set_show_preset("X", "bogus"), "digital")

    def test_unwatched_first_defaults_true(self):
        self.assertTrue(settings.get_show_unwatched_first("Brand New Show"))

    def test_unwatched_first_set_persists_and_coexists_with_preset(self):
        settings.set_show_preset("S", "film")
        settings.set_show_unwatched_first("S", False)
        self.assertEqual(settings.get_show_preset("S"), "film")      # preset survives
        self.assertFalse(settings.get_show_unwatched_first("S"))
        settings.set_show_unwatched_first("S", True)
        self.assertTrue(settings.get_show_unwatched_first("S"))
        self.assertEqual(settings.get_show_preset("S"), "film")

    def test_legacy_string_entry_migrates(self):
        settings._save(settings.PROFILES_FILE, {"Old": "animation2d"})   # old preset-only form
        self.assertEqual(settings.get_show_preset("Old"), "animation2d") # still readable
        self.assertTrue(settings.get_show_unwatched_first("Old"))        # default
        settings.set_show_unwatched_first("Old", False)                  # migrates to dict
        self.assertEqual(settings.get_show_preset("Old"), "animation2d") # preset preserved
        self.assertFalse(settings.get_show_unwatched_first("Old"))

    def test_normalize_audio_defaults_true(self):
        # Works for ANY item kind — the key is a show name, movie title, or channel folder.
        self.assertTrue(settings.get_show_normalize_audio("Brand New Show"))
        self.assertTrue(settings.get_show_normalize_audio("Some Movie Title (2024)"))

    def test_normalize_audio_set_persists_and_coexists_with_preset(self):
        settings.set_show_preset("S", "film")
        settings.set_show_normalize_audio("S", False)
        self.assertEqual(settings.get_show_preset("S"), "film")          # preset survives
        self.assertFalse(settings.get_show_normalize_audio("S"))
        self.assertTrue(settings.get_show_unwatched_first("S"))          # sibling key untouched
        settings.set_show_normalize_audio("S", True)
        self.assertTrue(settings.get_show_normalize_audio("S"))
        self.assertEqual(settings.get_show_preset("S"), "film")

    def test_normalize_audio_legacy_string_entry_migrates(self):
        settings._save(settings.PROFILES_FILE, {"Old": "animation2d"})   # old preset-only form
        self.assertTrue(settings.get_show_normalize_audio("Old"))        # default
        settings.set_show_normalize_audio("Old", False)                  # migrates to dict
        self.assertEqual(settings.get_show_preset("Old"), "animation2d") # preset preserved
        self.assertFalse(settings.get_show_normalize_audio("Old"))

    def test_extend_borders_defaults_off(self):
        # Overnight-scale compute per episode — the option must be an explicit opt-in.
        self.assertFalse(settings.get_show_extend_borders("Brand New Show"))

    def test_extend_borders_set_persists_and_coexists_with_preset(self):
        settings.set_show_preset("S", "film")
        settings.set_show_extend_borders("S", True)
        self.assertEqual(settings.get_show_preset("S"), "film")          # preset survives
        self.assertTrue(settings.get_show_extend_borders("S"))
        self.assertTrue(settings.get_show_normalize_audio("S"))          # sibling key untouched
        settings.set_show_extend_borders("S", False)
        self.assertFalse(settings.get_show_extend_borders("S"))
        self.assertEqual(settings.get_show_preset("S"), "film")

    def test_extend_prompt_defaults_empty_and_persists(self):
        self.assertEqual(settings.get_show_extend_prompt("Brand New Show"), "")
        settings.set_show_extend_prompt("S", "  dark wood bar, neon signs  ")
        self.assertEqual(settings.get_show_extend_prompt("S"), "dark wood bar, neon signs")
        settings.set_show_extend_prompt("S", "")                 # back to the default
        self.assertEqual(settings.get_show_extend_prompt("S"), "")

    def test_replace_source_defaults_true(self):
        # Default = REPLACE (the output replaces its input); key is show name or movie title.
        self.assertTrue(settings.get_show_replace_source("Brand New Show"))
        self.assertTrue(settings.get_show_replace_source("Some Movie Title (2024)"))

    def test_replace_source_set_persists_and_coexists(self):
        settings.set_show_preset("S", "film")
        settings.set_show_replace_source("S", False)
        self.assertFalse(settings.get_show_replace_source("S"))
        self.assertEqual(settings.get_show_preset("S"), "film")          # preset survives
        self.assertTrue(settings.get_show_normalize_audio("S"))          # sibling key untouched
        settings.set_show_replace_source("S", True)
        self.assertTrue(settings.get_show_replace_source("S"))

    def test_replace_source_legacy_string_entry_migrates(self):
        settings._save(settings.PROFILES_FILE, {"Old": "animation2d"})   # old preset-only form
        self.assertTrue(settings.get_show_replace_source("Old"))         # default
        settings.set_show_replace_source("Old", False)                   # migrates to dict
        self.assertEqual(settings.get_show_preset("Old"), "animation2d")
        self.assertFalse(settings.get_show_replace_source("Old"))

    def test_settings_only_accepts_known_keys(self):
        s = settings.set_settings({"poll_minutes": 45, "bogus": 1})
        self.assertEqual(s["poll_minutes"], 45)
        self.assertNotIn("bogus", s)

    def test_every_preset_has_all_resolution_variants(self):
        # FUTURE-PROOFING: any new parent preset must define every resolution variant.
        for key, preset in settings.TOPAZ_PRESETS.items():
            for res in settings.RES_BUCKETS:
                self.assertIn(res, preset["by_res"], f"{key} missing {res} variant")
                for param in ("model", "compression", "details", "halo", "blend"):
                    self.assertIn(param, preset["by_res"][res], f"{key}/{res} missing {param}")

    def test_lower_resolution_gets_heavier_cleanup(self):
        for key in settings.TOPAZ_PRESETS:
            by = settings.TOPAZ_PRESETS[key]["by_res"]
            self.assertGreater(by["480p"]["compression"], by["1080p"]["compression"], key)

    def test_preset_params_picks_resolution_variant(self):
        self.assertEqual(settings.preset_params("digital", "480p")["compression"], 0.28)
        self.assertEqual(settings.preset_params("digital", "1080p")["compression"], 0.08)
        # unknown res → 1080p fallback (also the 4K-clean default)
        self.assertEqual(settings.preset_params("digital", "2160p")["compression"], 0.08)


class Plan(unittest.TestCase):
    def _plan(self, **kw):
        return plan.choose_plan({"is_4k": False, "is_hdr": False, "is_dv": False, **kw})

    def test_1080p_sdr_upscales_and_adds_hdr(self):
        pl = self._plan()
        self.assertEqual((pl["topaz"], pl["scale"], pl["resolve"]), ("upscale", 2, "add_hdr_dv"))

    def test_4k_sdr_skips_topaz_and_adds_hdr(self):
        pl = self._plan(is_4k=True, is_cfr=True)
        self.assertEqual((pl["topaz"], pl["scale"], pl["resolve"]), ("resolve-only", 1, "add_hdr_dv"))

    def test_4k_hdr_skips_topaz_keeps_hdr_adds_dv_only(self):
        pl = self._plan(is_4k=True, is_hdr=True, is_cfr=True)
        self.assertEqual((pl["topaz"], pl["scale"], pl["resolve"]), ("resolve-only", 1, "add_dv"))

    def test_4k_vfr_is_only_re_timed_never_sent_to_topaz(self):
        pl = self._plan(is_4k=True)                          # is_cfr absent = not proven CFR
        self.assertEqual((pl["topaz"], pl["source_cfr"], pl["resolve"]),
                         ("resolve-only", False, "add_hdr_dv"))
        pl = self._plan(is_4k=True, is_hdr=True)
        self.assertEqual((pl["topaz"], pl["source_cfr"], pl["resolve"]),
                         ("resolve-only", False, "add_dv"))

    def test_1080p_hdr_upscales_keeps_hdr(self):
        pl = self._plan(is_hdr=True)
        self.assertEqual((pl["topaz"], pl["scale"], pl["resolve"]), ("upscale", 2, "add_dv"))

    def test_already_dv_skips_everything(self):
        pl = self._plan(is_4k=True, is_hdr=True, is_dv=True)
        self.assertEqual((pl["topaz"], pl["resolve"]), ("skip", "skip"))

    def test_480p_upscales_4x_then_fits_up_to_4k(self):
        pl = self._plan(height=480)
        self.assertEqual((pl["topaz"], pl["scale"], pl["res"], pl["fit_height"]),
                         ("upscale", 4, "480p", 2160))      # 4× = 1920 → lanczos up to 2160

    def test_720p_upscales_4x_then_fits_down_to_4k(self):
        pl = self._plan(height=720)
        self.assertEqual((pl["scale"], pl["res"], pl["fit_height"]), (4, "720p", 2160))  # 2880 → 2160

    def test_1080p_lands_on_2160_with_no_fit(self):
        pl = self._plan(height=1080)
        self.assertEqual((pl["scale"], pl["res"], pl["fit_height"]), (2, "1080p", None))  # 1080×2 = 2160

    def test_4k_never_gets_a_topaz_pass(self):
        # No 4K "clean" route any more (2026-09-30) — the cleanup counts as upscaling.
        for cfr in (True, False):
            pl = self._plan(is_4k=True, is_cfr=cfr)
            self.assertEqual((pl["topaz"], pl["scale"], pl["res"], pl["fit_height"]),
                             ("resolve-only", 1, None, None), cfr)

    def test_odd_height_fits_to_exact_4k(self):
        pl = self._plan(height=1088)                        # 1088×2 = 2176 ≠ 2160 → fit
        self.assertEqual((pl["scale"], pl["fit_height"]), (2, 2160))


class TopazColorAndScale(unittest.TestCase):
    def test_filter_uses_plan_scale_and_preset_params(self):
        prof = settings.preset_params("animation2d")
        self.assertIn("scale=2", topaz.build_filter_from_profile(prof, scale=2))
        self.assertIn("scale=1", topaz.build_filter_from_profile(prof, scale=1))
        self.assertIn("compression=0.3", topaz.build_filter_from_profile(prof, scale=1))

    def test_color_flags_preserve_hdr_and_skip_unknown(self):
        self.assertEqual(
            topaz.color_flags({"primaries": "bt2020", "transfer": "smpte2084", "space": "bt2020nc"}),
            ["-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc"])
        self.assertEqual(topaz.color_flags({"primaries": "unknown", "transfer": None}), [])

    def test_build_command_includes_color_flags(self):
        cmd = topaz.build_command("ff", "in.mp4", "out.mov", "vf", {"transfer": "smpte2084"})
        self.assertIn("-color_trc", cmd)
        self.assertIn("smpte2084", cmd)


class NasNetwork(unittest.TestCase):
    def test_only_the_three_choices_survive(self):
        import settings as st
        self.assertEqual(st.DEFAULT_SETTINGS["nas_network"], "ethernet")
        for v in ("ethernet", "wifi", "ethernet_only"):
            self.assertEqual(st._valid_nas_network(v), v)
        for junk in ("", None, "5g", 3):
            self.assertEqual(st._valid_nas_network(junk), "ethernet")
        self.assertIs(st.VALIDATORS["nas_network"], st._valid_nas_network)


class PlexThrottle(unittest.TestCase):
    def test_on_by_default_and_only_an_explicit_off_turns_it_off(self):
        import settings as st
        self.assertIs(st.DEFAULT_SETTINGS["plex_throttle"], True)
        for v in (True, 1, "yes", None, "garbage"):
            self.assertIs(st._valid_plex_throttle(v), True, v)
        for v in (False, 0, "0", "false", "off"):
            self.assertIs(st._valid_plex_throttle(v), False, v)
        self.assertIs(st.VALIDATORS["plex_throttle"], st._valid_plex_throttle)



if __name__ == "__main__":
    unittest.main()


class FastPathGate(unittest.TestCase):
    """NO 4K SOURCE GOES THROUGH TOPAZ (user-dictated 2026-09-30: no 12 Mbps minimum, and no
    cleanup pass for a variable frame rate either — "that counts as upscaling even though it's
    the same size"). A 4K HDR10 (PQ/HEVC/Main10) CFR source is NEVER re-encoded — rpu-only at any
    bitrate and 4K geometry. Every other 4K source takes resolve-only; a VFR one is re-timed."""

    GOOD = dict(is_4k=True, is_hdr=False, is_dv=False, codec="hevc", pix_fmt="yuv420p10le",
                width=3840, height=2160, is_cfr=True, video_kbps=15000, transfer=None)

    def _plan(self, **kw):
        return plan.choose_plan({**self.GOOD, **kw})

    def test_hdr10_takes_rpu_only(self):
        pl = self._plan(transfer="smpte2084", is_hdr=True)
        self.assertEqual((pl["topaz"], pl["resolve"], pl["is_hdr"], pl["source_cfr"]),
                         ("rpu-only", "add_dv", True, True))

    def test_sdr_takes_resolve_only(self):
        pl = self._plan()                                    # WWDITS profile: SDR 4K ~15 Mbps
        self.assertEqual((pl["topaz"], pl["resolve"]), ("resolve-only", "add_hdr_dv"))

    def test_hlg_takes_resolve_only_not_inject(self):
        pl = self._plan(transfer="arib-std-b67", is_hdr=True)  # HLG base can't carry an 8.1 RPU
        self.assertEqual((pl["topaz"], pl["resolve"]), ("resolve-only", "add_dv"))

    def test_no_bitrate_sends_a_4k_source_through_topaz(self):
        # The old 12 Mbps minimum is gone: a starved, an unknown and a huge bitrate all skip it.
        for kbps in (120000, 12000, 11999, 6000, 1, 0):
            self.assertEqual(self._plan(video_kbps=kbps)["topaz"], "resolve-only", kbps)
            pl = self._plan(transfer="smpte2084", is_hdr=True, video_kbps=kbps)
            self.assertEqual(pl["topaz"], "rpu-only", kbps)

    def test_no_4k_input_of_any_kind_reaches_topaz(self):
        import itertools
        for hdr, transfer, codec, pix, cfr, kbps in itertools.product(
                (False, True), (None, "smpte2084", "arib-std-b67"), ("hevc", "h264", "av1", "vp9"),
                ("yuv420p10le", "yuv420p"), (True, False), (0, 4000, 40000)):
            pl = self._plan(is_hdr=hdr, transfer=transfer, codec=codec, pix_fmt=pix,
                            is_cfr=cfr, video_kbps=kbps)
            self.assertIn(pl["topaz"], ("rpu-only", "resolve-only"),
                          (hdr, transfer, codec, pix, cfr, kbps))
            self.assertEqual(pl["source_cfr"], cfr)          # VFR: the CFR pass must re-time it

    def test_a_variable_frame_rate_source_is_re_timed_not_sent_to_topaz(self):
        pl = self._plan(is_cfr=False)
        self.assertEqual((pl["topaz"], pl["source_cfr"]), ("resolve-only", False))
        self.assertIn("re-timed to a constant frame rate", pl["reason"])
        # an HDR10 VFR source cannot keep its stream (the RPU needs one frame rate) — and says so
        pl = self._plan(is_cfr=False, transfer="smpte2084", is_hdr=True)
        self.assertEqual((pl["topaz"], pl["source_cfr"]), ("resolve-only", False))
        self.assertIn("re-encoded because its frame rate varies", pl["reason"])

    def test_hdr10_takes_rpu_only_at_ANY_4k_geometry(self):
        # The tier used to require width==3840 AND height==2160 exactly, so a 2.39:1 film
        # (3840x1600) and DCI 4K were re-encoded. Geometry has no bearing on an RPU.
        for geom in (dict(width=3840, height=1600), dict(width=3840, height=1608),
                     dict(width=4096, height=2160), dict(width=3840, height=2160)):
            pl = self._plan(transfer="smpte2084", is_hdr=True, **geom)
            self.assertEqual(pl["topaz"], "rpu-only", geom)

    def test_nothing_is_categorically_excluded(self):
        for kw in (dict(codec="av1"), dict(codec="h264"), dict(pix_fmt="yuv420p"),
                   dict(width=4096), dict(height=2072, width=3840)):
            self.assertEqual(self._plan(**kw)["topaz"], "resolve-only", kw)
        pl = self._plan(transfer="smpte2084", is_hdr=True, codec="av1")
        self.assertEqual((pl["topaz"], pl["resolve"]), ("resolve-only", "add_dv"))

    def test_a_re_encode_of_HDR_always_says_why(self):
        for kw, needle in ((dict(transfer="arib-std-b67"), "not PQ"),
                           (dict(transfer="smpte2084", codec="av1"), "not HEVC"),
                           (dict(transfer="smpte2084", pix_fmt="yuv420p"), "not 10-bit"),
                           (dict(transfer="smpte2084", is_cfr=False), "frame rate varies")):
            pl = self._plan(is_hdr=True, **kw)
            self.assertEqual(pl["topaz"], "resolve-only", kw)
            self.assertIn("re-encoded because", pl["reason"], kw)
            self.assertIn(needle, pl["reason"], kw)

    def test_youtube_profile_qualifies_on_its_numbers(self):
        pl = self._plan(codec="vp9", pix_fmt="yuv420p", video_kbps=20000)   # typical 4K VP9
        self.assertEqual(pl["topaz"], "resolve-only")

    def test_already_dv_still_wins(self):
        self.assertEqual(self._plan(is_dv=True)["topaz"], "skip")

    def test_a_1080p_hdr_source_is_still_upscaled(self):
        # The no-re-encode rule is about sources that do not need upscaling. Upscaling is the
        # point of the pipeline, and it necessarily re-encodes.
        pl = self._plan(is_4k=False, width=1920, height=1080,
                        transfer="smpte2084", is_hdr=True)
        self.assertEqual(pl["topaz"], "upscale")

    def test_the_threshold_setting_is_gone(self):
        import settings as s
        self.assertNotIn("passthrough_min_mbps", s.DEFAULT_SETTINGS)
        self.assertNotIn("passthrough_min_mbps", s.LIMITS)
        self.assertFalse(hasattr(plan, "passthrough_min_kbps"))


class Tunables(unittest.TestCase):
    """The scheduling/capacity settings that replaced hardcoded engine constants.

    Two properties matter more than any individual range: an untouched install must behave
    EXACTLY as it did when these were constants, and a hand-edited settings.json must never
    hand the engine a value the UI could not have produced."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.object(settings, "SETTINGS_FILE", os.path.join(self.d, "settings.json"))
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_a_cadence_burst_of_zero_survives_the_clamp(self):
        """0 is the dial's middle stop — "no YouTube videos" — so the clamp table has to let it
        through. It used to floor at 1, which is why the dial had no way to say none."""
        self.assertEqual(settings.clamp_setting("youtube_videos_per_burst", 0), 0)
        self.assertEqual(settings.set_settings({"youtube_videos_per_burst": 0})
                         ["youtube_videos_per_burst"], 0)
        self.assertEqual(settings.clamp_setting("youtube_videos_per_burst", -2), 0)   # still floored
        self.assertEqual(settings.clamp_setting("youtube_videos_per_burst", 99), 10)
        # the OTHER knob keeps its floor of 1: zero episodes between videos is not a cadence
        self.assertEqual(settings.clamp_setting("youtube_every_tv_episodes", 0), 1)

    def test_dead_battery_drain_key_is_gone(self):
        # It had ZERO engine consumers (the adapter-wattage gate replaced it); a setting that
        # does nothing is worse than no setting.
        self.assertNotIn("pause_on_battery_drain", settings.DEFAULT_SETTINGS)
        self.assertNotIn("pause_on_battery_drain", settings.set_settings({"pause_on_battery_drain": True}))

    def test_defaults_match_the_constants_they_replaced(self):
        # THE compatibility guarantee: a fresh install runs on exactly the old numbers.
        import orchestrator as o
        import series
        self.assertEqual(settings.DEFAULT_SETTINGS["min_free_gb"], o.MIN_FREE_GB)
        self.assertEqual(settings.DEFAULT_SETTINGS["prefetch_cap_gb"], o.PREFETCH_HARD_CAP_GB)
        self.assertEqual(settings.DEFAULT_SETTINGS["finisher_lanes"], o.FINISHER_LANES)
        self.assertEqual(settings.DEFAULT_SETTINGS["max_episode_fails"], o.MAX_EPISODE_FAILS)
        self.assertEqual(settings.DEFAULT_SETTINGS["unplug_grace_seconds"], o.UNPLUG_GRACE_SECONDS)
        self.assertEqual(settings.DEFAULT_SETTINGS["max_active_shows"], series.MAX_ACTIVE)

    def test_tunable_returns_the_default_when_the_key_is_absent(self):
        for key, default in settings.DEFAULT_SETTINGS.items():
            if key in settings.LIMITS:
                self.assertEqual(settings.tunable(key), default, key)

    def test_tunable_clamps_a_hand_edited_file(self):
        for key, (lo, hi) in settings.LIMITS.items():
            settings.set_settings({})                       # materialize the file
            with mock.patch.object(settings, "get_settings", return_value={key: hi + 10_000}):
                self.assertEqual(settings.tunable(key), hi, key)
            with mock.patch.object(settings, "get_settings", return_value={key: lo - 10_000}):
                self.assertEqual(settings.tunable(key), lo, key)
            with mock.patch.object(settings, "get_settings", return_value={key: "garbage"}):
                self.assertEqual(settings.tunable(key), settings.DEFAULT_SETTINGS[key], key)

    def test_zero_is_off_only_where_it_means_off(self):
        # 0 disables the feature for these; everywhere else it must clamp UP to the floor
        # rather than quietly turning something off.
        self.assertEqual(settings.set_settings({"prefetch_cap_gb": 0})["prefetch_cap_gb"], 0)
        self.assertEqual(settings.set_settings({"min_free_gb": 0})["min_free_gb"], 200)
        self.assertEqual(settings.set_settings({"finisher_lanes": 0})["finisher_lanes"], 1)

    def test_false_is_not_a_numeric_zero(self):
        # bool is an int subclass — without the _is_zero guard, a stray False would read as a
        # deliberate "off" for a ZERO_IS_OFF key. It must clamp like any other junk number
        # instead, so prefetching stays ON.
        self.assertEqual(settings.set_settings({"prefetch_cap_gb": False})["prefetch_cap_gb"],
                         settings.LIMITS["prefetch_cap_gb"][0])

    def test_new_keys_round_trip_in_range(self):
        s = settings.set_settings({"max_active_shows": 1, "finisher_lanes": 1, "min_free_gb": 250,
                                   "prefetch_cap_gb": 50, "max_episode_fails": 2,
                                   "unplug_grace_seconds": 0})
        self.assertEqual((s["max_active_shows"], s["finisher_lanes"], s["min_free_gb"],
                          s["prefetch_cap_gb"], s["max_episode_fails"], s["unplug_grace_seconds"]),
                         (1, 1, 250, 50, 2, 0))

    def test_segment_eta_gate_range_allows_always_but_not_zero(self):
        # The gate is "show the segment ETA once segments average longer than N minutes".
        # 1 is the floor and means effectively-always (a Topaz segment never encodes in under
        # a minute); 0 is NOT offered, because a zero here would read as "off" like the other
        # ZERO_IS_OFF keys while actually meaning the opposite.
        self.assertEqual(settings.DEFAULT_SETTINGS["seg_eta_after_minutes"], 15)
        self.assertNotIn("seg_eta_after_minutes", settings.ZERO_IS_OFF)
        self.assertEqual(settings.set_settings({"seg_eta_after_minutes": 0})["seg_eta_after_minutes"], 1)
        self.assertEqual(settings.set_settings({"seg_eta_after_minutes": 999})["seg_eta_after_minutes"], 120)

    def test_max_active_cannot_exceed_the_hard_ceiling(self):
        self.assertEqual(settings.set_settings({"max_active_shows": 99})["max_active_shows"],
                         settings.MAX_ACTIVE_CEILING)


class HdrFilenameGuess(unittest.TestCase):
    """The pre-download suggestion. It is a GUESS — plan.probe_input's color transfer is the
    real answer and only exists once the file is local — but it must not be wrong on the
    common case, because it is what the user reads and may pin."""

    def test_a_uhd_disc_source_reads_as_hdr_without_saying_so(self):
        # THE reported bug. DTS-HD and TrueHD contain "HD", not "HDR", so the token search
        # found nothing and a certainly-HDR10 film suggested 1000 nits.
        import settings as s
        self.assertTrue(s.looks_hdr(
            "Doctor.Strange.in.the.Multiverse.of.Madness.2022.2160p.BluRay.REMUX.HEVC."
            "DTS-HD.MA.TrueHD.7.1.Atmos-FGT.mkv"))

    def test_an_explicit_token_still_wins(self):
        import settings as s
        for n in ("M.2019.2160p.UHD.BluRay.HDR10.x265.mkv", "M.2005.2160p.UHD.BluRay.DV.mkv"):
            self.assertTrue(s.looks_hdr(n), n)

    def test_an_explicit_SDR_token_overrides_the_inference(self):
        import settings as s
        self.assertFalse(s.looks_hdr("M.2019.2160p.BluRay.REMUX.SDR.mkv"))

    def test_it_does_not_over_reach(self):
        import settings as s
        for n in ("Show.S01E01.1080p.BluRay.x265.mkv",      # not 4K
                  "M.2019.1080p.BluRay.REMUX.AVC.mkv",      # 1080p remux
                  "M.2019.2160p.WEB-DL.DDP5.1.H.265.mkv",   # 4K but not disc-sourced
                  "Some.Movie.2019.1080p.DVDRip.x264.mkv"):
            self.assertFalse(s.looks_hdr(n), n)

    def test_DVDRip_is_still_not_dolby_vision(self):
        import settings as s
        self.assertFalse(s.looks_hdr("Movie.2019.1080p.DVDRip.x264.mkv"))


class ProbeBeatsTheFilename(unittest.TestCase):
    """The display used to read only the filename, so a mislabelled source disagreed with the
    engine FOREVER. That is not merely cosmetic: a wrong suggestion can tempt a wrong pin, and
    a pin does take effect (stages.py picks the override over the source range)."""

    LIAR = "Some.Movie.2019.2160p.UHD.BluRay.HDR10.REMUX.HEVC.mkv"   # named HDR, actually SDR
    QUIET = "Movie.2022.2160p.BluRay.REMUX.HEVC.DTS-HD.MA.TrueHD.mkv"  # says nothing, is HDR

    def setUp(self):
        import plan, tempfile, os
        self._real = plan.PROBE_CACHE
        plan.PROBE_CACHE = os.path.join(tempfile.mkdtemp(), "probe_cache.json")

    def tearDown(self):
        import plan
        plan.PROBE_CACHE = self._real

    def test_without_a_probe_it_falls_back_to_the_name(self):
        import settings as s
        self.assertTrue(s.source_is_hdr(self.LIAR))      # the guess, and it is wrong
        self.assertTrue(s.source_is_hdr(self.QUIET))     # the UHD-disc inference

    def test_the_probe_overrides_a_filename_that_lies_HDR(self):
        import plan, settings as s
        plan._remember_probe("/x/" + self.LIAR, {"is_hdr": False, "transfer": "bt709"})
        self.assertFalse(s.source_is_hdr(self.LIAR))
        self.assertEqual(s.effective_output_mode("unpinned", s.source_is_hdr(self.LIAR)),
                         "dv1000")

    def test_the_probe_overrides_a_filename_that_lies_SDR(self):
        import plan, settings as s
        name = "Movie.2019.2160p.WEB-DL.SDR.mkv"
        self.assertFalse(s.source_is_hdr(name))
        plan._remember_probe("/x/" + name, {"is_hdr": True, "transfer": "smpte2084"})
        self.assertTrue(s.source_is_hdr(name))

    def test_an_unreadable_probe_is_never_recorded_as_a_guess(self):
        # transfer missing means ffprobe told us nothing; recording False there would turn a
        # non-answer into a confident wrong answer that outranks the filename.
        import plan, settings as s
        plan._remember_probe("/x/" + self.QUIET, {"is_hdr": False, "transfer": None})
        self.assertIsNone(plan.probed_is_hdr(self.QUIET))
        self.assertTrue(s.source_is_hdr(self.QUIET))     # still the filename inference

    def test_a_missing_cache_never_raises(self):
        import plan
        plan.PROBE_CACHE = "/nonexistent/dir/probe_cache.json"
        self.assertIsNone(plan.probed_is_hdr("anything.mkv"))
        plan._remember_probe("/x/a.mkv", {"is_hdr": True, "transfer": "smpte2084"})  # no raise


class QuietUntilValidator(unittest.TestCase):
    """The 4-hour Screen Control cap, enforced at the PERSISTENCE layer — the app's
    /api/quiet-mode path already clamps, but /api/settings and a hand-edited file must
    be held to the same rule (a days-long pause fills the disk and stalls the run)."""

    def test_cap_junk_and_in_range(self):
        import time as t
        with mock.patch.object(settings, "SETTINGS_FILE",
                               os.path.join(tempfile.mkdtemp(), "s.json")):
            far = int(t.time()) + 10 * 24 * 3600
            got = settings.set_settings({"quiet_until": far})["quiet_until"]
            self.assertLessEqual(got, int(t.time()) + settings.MAX_QUIET_SECONDS + 5)
            self.assertGreater(got, int(t.time()))          # capped, not zeroed
            self.assertEqual(settings.set_settings({"quiet_until": "junk"})["quiet_until"], 0)
            self.assertEqual(settings.set_settings({"quiet_until": -5})["quiet_until"], 0)
            ok = int(t.time()) + 600
            self.assertEqual(settings.set_settings({"quiet_until": ok})["quiet_until"], ok)


class ManualOnly2000Nits(unittest.TestCase):
    def test_auto_never_resolves_to_2000(self):
        # 2000-nit DV is MANUAL-ONLY (user-dictated 2026-08-09): auto = 1000 nits
        # whatever the intake range; the pin still wins.
        import settings as s
        self.assertEqual(s.effective_output_mode("unpinned-title", True), "dv1000")
        self.assertEqual(s.effective_output_mode("unpinned-title", False), "dv1000")


class DoFeaturettesReplacesFeaturettesLast(unittest.TestCase):
    """The per-show toggle changed meaning (user-dictated 2026-09-05): it used to choose the
    ORDER of season-00 specials, it now chooses whether they are upscaled at all. The old
    `featurettes_last` value must NOT carry over — BOTH of its states meant "process them",
    so neither maps onto "skip them", and inheriting False would silently stop upscaling
    specials for any show that had picked numeric order."""

    def setUp(self):
        # HERMETIC: per-show settings live in show_profiles.json (PROFILES_FILE), NOT in
        # SETTINGS_FILE — redirecting the latter is not enough, and without this these
        # tests write fake shows into the real profile store (caught doing exactly that).
        import tempfile, os as _os
        p = mock.patch.object(settings, "PROFILES_FILE",
                              _os.path.join(tempfile.mkdtemp(), "show_profiles.json"))
        p.start(); self.addCleanup(p.stop)
        settings.all_profiles.cache_clear() if hasattr(settings.all_profiles, "cache_clear") else None

    def test_it_defaults_to_on(self):
        self.assertTrue(settings.get_show_do_featurettes("Some Show"))

    def test_an_old_featurettes_last_false_does_not_become_skip(self):
        settings._update_show("Legacy Show", featurettes_last=False)
        self.assertTrue(settings.get_show_do_featurettes("Legacy Show"))

    def test_it_round_trips(self):
        settings.set_show_do_featurettes("Some Show", False)
        self.assertFalse(settings.get_show_do_featurettes("Some Show"))
        settings.set_show_do_featurettes("Some Show", True)
        self.assertTrue(settings.get_show_do_featurettes("Some Show"))

    def test_it_is_per_show(self):
        settings.set_show_do_featurettes("A", False)
        self.assertFalse(settings.get_show_do_featurettes("A"))
        self.assertTrue(settings.get_show_do_featurettes("B"))
