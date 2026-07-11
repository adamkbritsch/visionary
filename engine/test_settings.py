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
