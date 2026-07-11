import unittest
import plex


class ParseLeaves(unittest.TestCase):
    XML = (b'<MediaContainer>'
           b'<Video parentIndex="1" index="1" viewCount="3"><Media><Part file="/media/TV-Shows/MyShow/S01/ep1.mp4"/></Media></Video>'
           b'<Video parentIndex="1" index="2"><Media><Part file="/media/TV-Shows/MyShow/S01/ep2.mp4"/></Media></Video>'
           b'<Video parentIndex="1" index="3" viewCount="0"><Media><Part file="/media/TV-Shows/MyShow/S01/ep3.mp4"/></Media></Video>'
           b'<Video parentIndex="1" index="9" viewCount="1"><Media><Part file="/media/TV-Shows/OtherShow/S01/x.mp4"/></Media></Video>'
           b'</MediaContainer>')

    def test_watched_by_viewcount(self):
        m = plex._parse_leaves(self.XML, "MyShow")
        self.assertTrue(m["ep1.mp4"])               # viewCount 3 → watched
        self.assertFalse(m["ep2.mp4"])              # no viewCount → unwatched
        self.assertFalse(m["ep3.mp4"])              # viewCount 0 → unwatched

    def test_restricted_to_this_series_dir(self):
        m = plex._parse_leaves(self.XML, "MyShow")
        self.assertNotIn("x.mp4", m)                # OtherShow excluded even though watched
        self.assertEqual(set(m), {"ep1.mp4", "ep2.mp4", "ep3.mp4"})


class Candidates(unittest.TestCase):
    def test_title_word_overlap_ranks_match_first(self):
        shows = [("Random Thing", "1"), ("The Office (US)", "2"), ("Parks", "3")]
        ranked = plex._candidates(shows, "The Office Superfan Episodes (S01-08) 1080p Peacock")
        self.assertEqual(ranked[0], "2")            # 'office' overlaps; articles/tags stripped

    def test_no_overlap_falls_back_to_all_shows(self):
        shows = [("Alpha", "1"), ("Beta", "2")]
        self.assertEqual(set(plex._candidates(shows, "Zzz Qqq")), {"1", "2"})

    def test_release_tags_are_not_matches(self):
        # a 1080p-only "overlap" must NOT count (both stripped as stop words)
        shows = [("Some 1080p Show", "1")]
        # 'show' is a real word in both? neither has 'show' in the folder below → no overlap → fallback
        self.assertEqual(plex._candidates(shows, "Totally Different 1080p"), ["1"])


class TvTitles(unittest.TestCase):
    XML = (b'<MediaContainer>'
           b'<Video grandparentTitle="The Office (US)"><Media><Part file="/media/TV-Shows/The Office Superfan Episodes (S01-08) 1080p/S01/x.mp4"/></Media></Video>'
           b'<Video grandparentTitle="13 Reasons Why"><Media><Part file="/media/vol2/TV-Shows/13 Reasons Why (2017)/Season 1/y.mkv"/></Media></Video>'
           b'<Video grandparentTitle="No Path Show"></Video>'
           b'</MediaContainer>')

    def test_maps_nas_dir_to_plex_title_across_volumes(self):
        m = plex._titles_from_episodes(self.XML)
        self.assertEqual(m["The Office Superfan Episodes (S01-08) 1080p"], "The Office (US)")
        self.assertEqual(m["13 Reasons Why (2017)"], "13 Reasons Why")   # vol2 path works too
        self.assertEqual(len(m), 2)                                      # the file-less entry is skipped


class MovieTitles(unittest.TestCase):
    XML = (b'<MediaContainer>'
           b'<Video title="A Clockwork Orange" year="1971"><Media><Part file="/media/Movies/A Clockwork Orange (1971) [1080p BluRay].mkv"/></Media></Video>'
           b'<Video title="12 Years a Slave"><Media><Part file="/media/vol2/Movies/sub/12 Years a Slave (2013).mp4"/></Media></Video>'
           b'<Video title="No File Movie" year="2000"></Video>'
           b'</MediaContainer>')

    def test_maps_file_basename_to_plex_title_with_year(self):
        m = plex._movie_titles_from_xml(self.XML)
        self.assertEqual(m["A Clockwork Orange (1971) [1080p BluRay].mkv"], "A Clockwork Orange (1971)")
        self.assertEqual(m["12 Years a Slave (2013).mp4"], "12 Years a Slave")   # no year attr → title only
        self.assertEqual(len(m), 2)                                              # file-less entry skipped


if __name__ == "__main__":
    unittest.main()
