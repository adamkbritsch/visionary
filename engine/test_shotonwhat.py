import os
import tempfile
import unittest
from unittest import mock

import shotonwhat


class Slug(unittest.TestCase):
    def test_strips_qualifier_and_appends_year(self):
        self.assertEqual(shotonwhat._slug("The Office (US)", 2005), "the-office-2005")

    def test_year_paren_and_punctuation(self):
        self.assertEqual(shotonwhat._slug("Barry (2018)", 2018), "barry-2018")
        self.assertEqual(shotonwhat._slug("Spider-Man: No Way Home", 2021), "spider-man-no-way-home-2021")

    def test_no_year(self):
        self.assertEqual(shotonwhat._slug("Oppenheimer"), "oppenheimer")


class Parse(unittest.TestCase):
    # The real pages embed a "pagePostTerms":{...,"acquisition":[...]} JSON blob.
    def test_digital(self):
        html = '..."acquisition":["Digital Cinema"],"cameras":["ARRI ALEXA Mini"]...'
        self.assertEqual(shotonwhat._parse(html), "digital")

    def test_celluloid_is_film(self):
        html = '..."acquisition":["Celluloid"],"cameras":["Panavision"]...'
        self.assertEqual(shotonwhat._parse(html), "film")

    def test_mixed_prefers_film(self):
        # A grain-safe call: if a title mixes both, treat it as film.
        html = '..."acquisition":["Celluloid","Computer Generated (Digital)"]...'
        self.assertEqual(shotonwhat._parse(html), "film")

    def test_no_field_is_empty(self):
        self.assertEqual(shotonwhat._parse("<html>no terms here</html>"), "")
        self.assertEqual(shotonwhat._parse(""), "")


class FilmOrDigital(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.object(shotonwhat, "CACHE", os.path.join(self.d, "c.json"))
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_fetch_parsed_and_cached(self):
        with mock.patch.object(shotonwhat, "_fetch", return_value='"acquisition":["Celluloid"]') as f:
            self.assertEqual(shotonwhat.film_or_digital("Oppenheimer", 2023), "film")
            # second call hits the cache — no re-fetch
            self.assertEqual(shotonwhat.film_or_digital("Oppenheimer", 2023), "film")
            f.assert_called_once()

    def test_miss_returns_none_and_is_cached(self):
        with mock.patch.object(shotonwhat, "_fetch", return_value=None) as f:
            self.assertIsNone(shotonwhat.film_or_digital("Nonexistent Title", 1999))
            self.assertIsNone(shotonwhat.film_or_digital("Nonexistent Title", 1999))
            f.assert_called_once()


if __name__ == "__main__":
    unittest.main()
