import unittest
from unittest import mock

import preset_detect
import tmdb


def info(genres=(), keywords=(), companies=(), lang=""):
    return {"genres": list(genres), "keywords": list(keywords),
            "companies": list(companies), "lang": lang}


class DetectPreset(unittest.TestCase):
    def _run(self, tmdb_info, shot):
        with mock.patch.object(tmdb, "lookup", return_value=tmdb_info), \
             mock.patch("shotonwhat.film_or_digital", return_value=shot):
            return preset_detect.detect_preset("Some Title", "2020", "movie")

    def test_animation_2d(self):
        self.assertEqual(self._run(info(genres=["Animation"], keywords=["anime"]), None), "animation2d")

    def test_animation_3d_cgi(self):
        self.assertEqual(self._run(info(genres=["Animation"], companies=["Pixar"]), None), "animation3d")

    def test_animation_stop_motion_is_digital(self):
        self.assertEqual(self._run(info(genres=["Animation"], keywords=["stop motion"]), None), "digital")

    def test_animation_unclear_is_none_not_shotonwhat(self):
        # Unclear animation must NOT fall through to shotonwhat (which would mislabel it).
        self.assertIsNone(self._run(info(genres=["Animation"], keywords=["family"]), "film"))

    def test_live_action_uses_shotonwhat(self):
        self.assertEqual(self._run(info(genres=["Drama"]), "film"), "film")
        self.assertEqual(self._run(info(genres=["Comedy"]), "digital"), "digital")

    def test_no_tmdb_falls_back_to_shotonwhat(self):
        self.assertEqual(self._run(None, "digital"), "digital")

    def test_nothing_resolves_is_none(self):
        self.assertIsNone(self._run(None, None))

    def test_empty_title_is_none(self):
        self.assertIsNone(preset_detect.detect_preset("", "2020", "movie"))


if __name__ == "__main__":
    unittest.main()
