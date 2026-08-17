"""Media-folder routing: Plex library sections -> verified NAS FTP roots per kind.
All pure — the live Plex/FTP readers are injected."""
import unittest
from unittest import mock

import medialibs

# The REAL layout read off the maintainer's Plex (2026-08-17), including the two 3D
# libraries the old hardcoded roots missed and the YouTube library Plex types as `movie`.
SHARES = ["home", "Media", "docker", "MediaVolume2", "3D Movies", "MediaVolume3",
          "ROMs (Games)", "3D TV Shows"]
SECTIONS = [
    {"key": "8", "type": "movie", "title": "3D Movies",
     "locations": ["/media/vol2/3D-Movies"]},
    {"key": "2", "type": "movie", "title": "Movies",
     "locations": ["/media/Movies", "/media/vol2/Movies", "/media/vol3/Movies"]},
    {"key": "9", "type": "show", "title": "3D TV Shows",
     "locations": ["/media/vol3/3D-TV-Shows"]},
    {"key": "4", "type": "show", "title": "TV Shows",
     "locations": ["/media/TV-Shows", "/media/vol2/TV-Shows", "/media/vol3/TV-Shows"]},
    {"key": "13", "type": "artist", "title": "Audiobooks",
     "locations": ["/media/vol3/Audiobooks"]},
    {"key": "10", "type": "movie", "title": "YouTube", "locations": ["/media/YouTube"]},
]
FALLBACKS = {"tv": ["/Media/TV-Shows", "/MediaVolume2/TV-Shows", "/MediaVolume3/TV-Shows"],
             "movie": ["/Media/Movies", "/MediaVolume2/Movies", "/MediaVolume3/Movies"],
             "youtube": ["/Media/YouTube"], "youtube_staging": ["/Media/YouTube-raw"]}


class ContainerPathMapping(unittest.TestCase):
    def test_primary_and_numbered_volumes(self):
        f = lambda p: medialibs.container_to_ftp(p, SHARES)
        self.assertEqual(f("/media/TV-Shows"), "/Media/TV-Shows")
        self.assertEqual(f("/media/vol2/Movies"), "/MediaVolume2/Movies")
        self.assertEqual(f("/media/vol3/3D-TV-Shows"), "/MediaVolume3/3D-TV-Shows")
        self.assertEqual(f("/media/vol1/Movies"), "/Media/Movies")      # vol1 == primary
        self.assertEqual(f("/media/volume2/Movies"), "/MediaVolume2/Movies")

    def test_nested_paths_below_the_library_root(self):
        self.assertEqual(medialibs.container_to_ftp("/media/vol2/Movies/4K", SHARES),
                         "/MediaVolume2/Movies/4K")

    def test_unmappable_inputs_return_none_never_a_guess(self):
        for bad in ("", None, "/", "/media", "relative/path"):
            self.assertIsNone(medialibs.container_to_ftp(bad, SHARES), bad)
        # a volume with no matching share must NOT silently fall back to the primary
        self.assertIsNone(medialibs.container_to_ftp("/media/vol9/Movies",
                                                     ["Media", "MediaVolume2"]))
        self.assertIsNone(medialibs.container_to_ftp("/media/TV", ["home", "docker"]))

    def test_share_for(self):
        self.assertEqual(medialibs.share_for(None, SHARES), "Media")
        self.assertEqual(medialibs.share_for("1", SHARES), "Media")
        self.assertEqual(medialibs.share_for("3", SHARES), "MediaVolume3")
        self.assertIsNone(medialibs.share_for("7", SHARES))


class DefaultClassification(unittest.TestCase):
    def _libs(self, exists=None):
        return medialibs.detect(SECTIONS, SHARES, exists=exists)

    def test_plex_type_decides_tv_and_movie(self):
        by = {l["title"]: l["default_kind"] for l in self._libs()}
        self.assertEqual(by["TV Shows"], "tv")
        self.assertEqual(by["Movies"], "movie")

    def test_youtube_is_not_a_movie_library_despite_plexs_type(self):
        by = {l["title"]: l["default_kind"] for l in self._libs()}
        self.assertEqual(by["YouTube"], "youtube")

    def test_3d_libraries_are_offered_but_never_assumed(self):
        # Assigning them by default would silently pull a whole stereoscopic library into
        # the rotation, and the deliverable here is a 2D 4K DV master.
        by = {l["title"]: l for l in self._libs()}
        self.assertIsNone(by["3D Movies"]["default_kind"])
        self.assertIsNone(by["3D TV Shows"]["default_kind"])
        self.assertTrue(by["3D Movies"]["routable"])      # still listed and choosable

    def test_non_media_types_are_listed_but_unroutable(self):
        by = {l["title"]: l for l in self._libs()}
        self.assertIsNone(by["Audiobooks"]["default_kind"])
        self.assertFalse(by["Audiobooks"]["routable"])


class Assignment(unittest.TestCase):
    def _libs(self, exists=None):
        return medialibs.detect(SECTIONS, SHARES, exists=exists)

    def test_detection_reproduces_the_hardcoded_layout_exactly(self):
        # The whole feature is only safe to auto-apply because this holds: on the machine
        # the old constants were written for, Plex-derived == the constants.
        a = medialibs.merge_assignment(self._libs(), {}, FALLBACKS)
        self.assertEqual(a["tv"]["roots"], FALLBACKS["tv"])
        self.assertEqual(a["movie"]["roots"], FALLBACKS["movie"])
        self.assertEqual(a["youtube"]["roots"], FALLBACKS["youtube"])
        for k in ("tv", "movie", "youtube"):
            self.assertEqual(a[k]["source"], "plex")

    def test_primary_volume_sorts_first(self):
        # Load-bearing: the walkers treat the first root as the winner of a name collision.
        a = medialibs.merge_assignment(self._libs(), {"9": "tv"}, FALLBACKS)   # add 3D TV
        self.assertEqual(a["tv"]["roots"][0], "/Media/TV-Shows")
        self.assertIn("/MediaVolume3/3D-TV-Shows", a["tv"]["roots"])

    def test_override_can_add_and_remove_a_library(self):
        a = medialibs.merge_assignment(self._libs(), {"8": "movie"}, FALLBACKS)
        self.assertIn("/MediaVolume2/3D-Movies", a["movie"]["roots"])
        self.assertEqual(a["movie"]["source"], "override")
        b = medialibs.merge_assignment(self._libs(), {"4": None}, FALLBACKS)
        self.assertEqual(b["tv"]["source"], "default")     # unassigned -> built-in fallback
        self.assertEqual(b["tv"]["roots"], FALLBACKS["tv"])

    def test_unverified_locations_never_reach_the_roots(self):
        # A path Plex claims but FTP cannot list is REPORTED, never used.
        libs = self._libs(exists=lambda p: p != "/MediaVolume3/Movies")
        a = medialibs.merge_assignment(libs, {}, FALLBACKS)
        self.assertNotIn("/MediaVolume3/Movies", a["movie"]["roots"])
        self.assertEqual(a["movie"]["roots"], ["/Media/Movies", "/MediaVolume2/Movies"])

    def test_no_plex_at_all_keeps_the_built_in_layout(self):
        a = medialibs.merge_assignment([], {}, FALLBACKS)
        for k in medialibs.KINDS:
            self.assertEqual(a[k]["roots"], FALLBACKS[k])
            self.assertEqual(a[k]["source"], "default")


class ConfigRoundTrip(unittest.TestCase):
    def test_configstore_normalizes_and_refuses_junk(self):
        import configstore
        saved = {}
        with mock.patch.object(configstore, "write", side_effect=saved.update), \
             mock.patch.object(configstore, "read_redacted", return_value={}):
            configstore.save({"media_roots": {"tv": ["/Media/TV-Shows", "relative/bad"],
                                              "bogus_kind": ["/x"]},
                              "media_lib_kinds": {"4": "tv", "9": "not_a_kind"}})
        self.assertEqual(saved["media_roots"], {"tv": ["/Media/TV-Shows"]})   # relative dropped
        self.assertEqual(saved["media_lib_kinds"], {"4": "tv"})               # junk kind dropped

    def test_structured_keys_stay_out_of_the_text_field_surface(self):
        import configstore
        r = configstore.read_redacted()
        for k in ("media_roots", "media_lib_kinds"):
            self.assertNotIn(k, r["fields"])       # Swift's [String:String] stays clean
            self.assertIn(k, configstore.ALLOWED_KEYS)   # ...but still writable


if __name__ == "__main__":
    unittest.main()
