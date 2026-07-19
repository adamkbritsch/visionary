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

    def test_4k_sdr_cleans_and_adds_hdr(self):
        pl = self._plan(is_4k=True)
        self.assertEqual((pl["topaz"], pl["scale"], pl["resolve"]), ("clean", 1, "add_hdr_dv"))

    def test_4k_hdr_cleans_keeps_hdr_adds_dv_only(self):
        pl = self._plan(is_4k=True, is_hdr=True)
        self.assertEqual((pl["topaz"], pl["scale"], pl["resolve"]), ("clean", 1, "add_dv"))

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

    def test_4k_clean_uses_1080p_variant_and_no_fit(self):
        pl = self._plan(is_4k=True)
        self.assertEqual((pl["topaz"], pl["scale"], pl["res"], pl["fit_height"]),
                         ("clean", 1, "1080p", None))

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


if __name__ == "__main__":
    unittest.main()


class FastPathGate(unittest.TestCase):
    """HIGH-BITRATE 4K FAST PATH: exactly-4K HEVC 10-bit CFR at/above the threshold skips
    Topaz — HDR10 (PQ) keeps the original stream (rpu-only), SDR/HLG ships Resolve's
    conversion (resolve-only). Every disqualifier falls through to today's plans."""

    GOOD = dict(is_4k=True, is_hdr=False, is_dv=False, codec="hevc", pix_fmt="yuv420p10le",
                width=3840, height=2160, is_cfr=True, video_kbps=15000, transfer=None)

    def _plan(self, thresh=12000, **kw):
        return plan.choose_plan({**self.GOOD, **kw}, passthrough_min_kbps=thresh)

    def test_hdr10_takes_rpu_only(self):
        pl = self._plan(transfer="smpte2084", is_hdr=True)
        self.assertEqual((pl["topaz"], pl["resolve"], pl["is_hdr"]), ("rpu-only", "add_dv", True))

    def test_sdr_takes_resolve_only(self):
        pl = self._plan()                                    # WWDITS profile: SDR 4K ~15 Mbps
        self.assertEqual((pl["topaz"], pl["resolve"]), ("resolve-only", "add_hdr_dv"))

    def test_hlg_takes_resolve_only_not_inject(self):
        pl = self._plan(transfer="arib-std-b67", is_hdr=True)  # HLG base can't carry an 8.1 RPU
        self.assertEqual((pl["topaz"], pl["resolve"]), ("resolve-only", "add_dv"))

    def test_threshold_boundary_is_inclusive(self):
        self.assertEqual(self._plan(video_kbps=12000)["topaz"], "resolve-only")   # == passes
        self.assertEqual(self._plan(video_kbps=11999)["topaz"], "clean")          # below → full path

    def test_nothing_is_categorically_excluded(self):
        # Eligibility is PURELY measured (4K + CFR + bitrate) — codec/bit-depth/geometry only
        # pick the tier (user-dictated: no carve-outs; e.g. YouTube must not be discounted).
        for kw in (dict(codec="av1"), dict(codec="h264"), dict(pix_fmt="yuv420p"),
                   dict(width=4096), dict(height=2072, width=3840)):
            self.assertEqual(self._plan(**kw)["topaz"], "resolve-only", kw)
        # a PQ source that misses an inject prerequisite still fast-paths via resolve-only
        pl = self._plan(transfer="smpte2084", is_hdr=True, codec="av1")
        self.assertEqual((pl["topaz"], pl["resolve"]), ("resolve-only", "add_dv"))

    def test_youtube_profile_qualifies_on_its_numbers(self):
        pl = self._plan(codec="vp9", pix_fmt="yuv420p", video_kbps=20000)   # typical 4K VP9
        self.assertEqual(pl["topaz"], "resolve-only")

    def test_only_measured_disqualifiers_fall_through(self):
        self.assertEqual(self._plan(is_cfr=False)["topaz"], "clean")   # VFR: timing untrustworthy
        self.assertEqual(self._plan(video_kbps=0)["topaz"], "clean")   # unknown/zero bitrate

    def test_already_dv_still_wins(self):
        self.assertEqual(self._plan(is_dv=True)["topaz"], "skip")

    def test_threshold_zero_disables_the_gate(self):
        self.assertEqual(self._plan(thresh=0)["topaz"], "clean")

    def test_settings_clamp_for_the_knob(self):
        import settings as s
        import tempfile, os as _os
        from unittest import mock
        with mock.patch.object(s, "SETTINGS_FILE", _os.path.join(tempfile.mkdtemp(), "s.json")):
            self.assertEqual(s.set_settings({"passthrough_min_mbps": 3})["passthrough_min_mbps"], 5)
            self.assertEqual(s.set_settings({"passthrough_min_mbps": 999})["passthrough_min_mbps"], 200)
            self.assertEqual(s.set_settings({"passthrough_min_mbps": 0})["passthrough_min_mbps"], 0)
            self.assertEqual(s.set_settings({"passthrough_min_mbps": 12})["passthrough_min_mbps"], 12)
