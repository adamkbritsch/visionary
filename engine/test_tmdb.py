import os
import tempfile
import unittest
from unittest import mock

import tmdb


def info(genres=(), keywords=(), companies=(), lang=""):
    return {"genres": list(genres), "keywords": list(keywords),
            "companies": list(companies), "lang": lang}


class IsAnimation(unittest.TestCase):
    def test_true_false_none(self):
        self.assertTrue(tmdb.is_animation(info(genres=["Animation", "Comedy"])))
        self.assertFalse(tmdb.is_animation(info(genres=["Comedy"])))
        self.assertFalse(tmdb.is_animation(None))


class Technique(unittest.TestCase):
    def test_2d_keywords(self):
        for kw in ("hand-drawn animation", "traditional animation", "cel animation",
                   "2d animation", "anime"):
            self.assertEqual(tmdb.technique(info(keywords=[kw])), "animation2d", kw)

    def test_japanese_language_is_2d_anime(self):
        self.assertEqual(tmdb.technique(info(lang="ja")), "animation2d")

    def test_3d_keywords(self):
        for kw in ("cgi", "computer animation", "3d animation", "computer-animated"):
            self.assertEqual(tmdb.technique(info(keywords=[kw])), "animation3d", kw)

    def test_cgi_studio(self):
        for co in ("Pixar", "DreamWorks Animation", "Illumination", "Blue Sky Studios",
                   "Sony Pictures Animation", "Walt Disney Animation Studios"):   # WDAS = Frozen/Encanto = 3D
            self.assertEqual(tmdb.technique(info(companies=[co])), "animation3d", co)

    def test_stop_motion_is_digital(self):
        self.assertEqual(tmdb.technique(info(keywords=["stop motion"])), "digital")
        self.assertEqual(tmdb.technique(info(keywords=["claymation"])), "digital")

    def test_stop_motion_beats_incidental_cgi(self):
        # Real case (Coraline/LAIKA): stop-motion films carry an incidental CGI keyword → still digital.
        self.assertEqual(tmdb.technique(info(keywords=["stop motion", "cgi"])), "digital")

    def test_weak_2d_cartoon_and_adult_animation(self):
        # Western TV cartoons TMDb under-tags for technique (Simpsons, Rick and Morty).
        self.assertEqual(tmdb.technique(info(keywords=["cartoon"])), "animation2d")
        self.assertEqual(tmdb.technique(info(keywords=["adult animation"])), "animation2d")

    def test_weak_2d_tv_studio(self):
        # Phineas and Ferb: no technique keyword, only a 2D-TV studio.
        self.assertEqual(tmdb.technique(info(companies=["Disney Television Animation"])), "animation2d")

    def test_3d_beats_weak_2d_signal(self):
        # A CGI show tagged "cartoon" must NOT fall to the weak-2D tier — 3D is checked first.
        self.assertEqual(tmdb.technique(info(keywords=["cartoon", "computer animation"])), "animation3d")
        self.assertEqual(tmdb.technique(info(keywords=["cartoon"], companies=["Pixar"])), "animation3d")

    def test_unclear_is_none(self):
        self.assertIsNone(tmdb.technique(info(keywords=["family"])))
        self.assertIsNone(tmdb.technique(None))

    def test_2d_wins_over_3d_when_both_present(self):
        self.assertEqual(tmdb.technique(info(keywords=["anime", "cgi"])), "animation2d")


class LookupNoKey(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.ps = [mock.patch.object(tmdb, "CACHE", os.path.join(self.d, "c.json")),
                   mock.patch.object(tmdb, "CONFIG", os.path.join(self.d, "config.json")),
                   mock.patch.dict(os.environ, {}, clear=False)]
        os.environ.pop("TOPAZ_TMDB_KEY", None)
        for p in self.ps:
            p.start()

    def tearDown(self):
        for p in self.ps:
            p.stop()

    def test_no_key_returns_none_without_network(self):
        with mock.patch.object(tmdb, "_get", side_effect=AssertionError("no network")):
            self.assertIsNone(tmdb.lookup("Barry", 2018, "tv"))

    def test_env_key_used_search_then_details(self):
        os.environ["TOPAZ_TMDB_KEY"] = "k"
        calls = []

        def fake_get(path, params, timeout=8):
            calls.append(path)
            if path.startswith("/search/"):
                return {"results": [{"id": 42}]}
            return {"genres": [{"name": "Animation"}],
                    "keywords": {"results": [{"name": "anime"}]},
                    "production_companies": [{"name": "Studio Ghibli"}],
                    "original_language": "ja"}
        with mock.patch.object(tmdb, "_get", side_effect=fake_get):
            got = tmdb.lookup("Spirited Away", 2001, "movie")
        self.assertEqual(got["genres"], ["Animation"])
        self.assertEqual(got["keywords"], ["anime"])
        self.assertEqual(got["lang"], "ja")
        self.assertTrue(calls[0].startswith("/search/movie"))
        # cached — a second lookup makes no further _get calls
        with mock.patch.object(tmdb, "_get", side_effect=AssertionError("cached")):
            self.assertEqual(tmdb.lookup("Spirited Away", 2001, "movie")["lang"], "ja")

    def test_no_match_cached_as_none(self):
        os.environ["TOPAZ_TMDB_KEY"] = "k"
        with mock.patch.object(tmdb, "_get", return_value={"results": []}) as g:
            self.assertIsNone(tmdb.lookup("Zzz", 1900, "movie"))
        with mock.patch.object(tmdb, "_get", side_effect=AssertionError("cached")):
            self.assertIsNone(tmdb.lookup("Zzz", 1900, "movie"))


if __name__ == "__main__":
    unittest.main()
