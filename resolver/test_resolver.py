import unittest
import resolver
from resolver import container_to_host, UnmappedPathError, build_targets


class ContainerToHost(unittest.TestCase):
    def test_vol1_media_default(self):
        self.assertEqual(
            container_to_host("/media/TV-Shows/Brooklyn Nine-Nine (2013)/Season 1/ep.mkv"),
            "/volume1/Media/TV-Shows/Brooklyn Nine-Nine (2013)/Season 1/ep.mkv",
        )

    def test_vol2(self):
        self.assertEqual(
            container_to_host("/media/vol2/TV-Shows/Show/Season 1/ep.mkv"),
            "/volume2/MediaVolume2/TV-Shows/Show/Season 1/ep.mkv",
        )

    def test_vol3(self):
        self.assertEqual(
            container_to_host("/media/vol3/3D-TV-Shows/Show/ep.mkv"),
            "/volume3/MediaVolume3/3D-TV-Shows/Show/ep.mkv",
        )

    def test_vol4_is_unmapped(self):
        with self.assertRaises(UnmappedPathError):
            container_to_host("/media/vol4/TV-Shows/Show/ep.mkv")

    def test_non_media_path_is_unmapped(self):
        with self.assertRaises(UnmappedPathError):
            container_to_host("/data/TV-Shows/Show/ep.mkv")


class BuildTargets(unittest.TestCase):
    def _ep(self, s, e, title, file):
        return {"season": s, "episode": e, "title": title, "file": file}

    def test_orders_by_season_then_episode(self):
        eps = [
            self._ep(2, 1, "B", "/media/TV-Shows/X/Season 2/2x01.mkv"),
            self._ep(1, 2, "A2", "/media/TV-Shows/X/Season 1/1x02.mkv"),
            self._ep(1, 1, "A1", "/media/TV-Shows/X/Season 1/1x01.mkv"),
        ]
        targets = build_targets(eps)
        self.assertEqual([(t.season, t.episode) for t in targets], [(1, 1), (1, 2), (2, 1)])

    def test_translates_container_path_to_host(self):
        eps = [self._ep(1, 1, "Pilot", "/media/TV-Shows/X/Season 1/1x01.mkv")]
        t = build_targets(eps)[0]
        self.assertEqual(t.host_path, "/volume1/Media/TV-Shows/X/Season 1/1x01.mkv")
        self.assertEqual(t.state, "PENDING")


class PickShow(unittest.TestCase):
    class _Show:
        def __init__(self, title): self.title = title

    def test_exact_case_insensitive_match(self):
        cands = [self._Show("Brooklyn Nine-Nine"), self._Show("Brooklyn")]
        self.assertIs(resolver.pick_show(cands, "brooklyn nine-nine"), cands[0])

    def test_single_fuzzy_candidate_returned(self):
        cands = [self._Show("The Office (US)")]
        self.assertIs(resolver.pick_show(cands, "the office"), cands[0])

    def test_no_candidates_raises_not_found(self):
        with self.assertRaises(resolver.ShowNotFoundError):
            resolver.pick_show([], "nope")

    def test_ambiguous_raises(self):
        cands = [self._Show("Sherlock"), self._Show("Sherlock Holmes")]
        with self.assertRaises(resolver.AmbiguousShowError):
            resolver.pick_show(cands, "sher")


class VerifyTargets(unittest.TestCase):
    def test_marks_located_and_missing(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "real.mkv")
            open(real, "w").close()
            targets = [
                resolver.EpisodeTarget(1, 1, "real", "/media/x", real),
                resolver.EpisodeTarget(1, 2, "gone", "/media/y", os.path.join(d, "missing.mkv")),
            ]
            resolver.verify_targets(targets)
            self.assertEqual(targets[0].state, "LOCATED")
            self.assertEqual(targets[1].state, "MISSING")


if __name__ == "__main__":
    unittest.main()
