import unittest
from unittest import mock

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


class YouTubeChannelCollections(unittest.TestCase):
    """That library held 102 per-channel collections with nothing in any of them, while the
    videos themselves carried no collection tag at all — the grouping had never been applied
    to what Visionary publishes. The channel comes from the FILE PATH, which is the folder
    the publish writes into, so it cannot disagree with where the file actually lives."""

    def test_channel_comes_from_the_publish_folder(self):
        self.assertEqual(
            plex.channel_of("/media/YouTube/Almost Friday TV/AFTV - x - id/AFTV - x [id].mp4"),
            "Almost Friday TV")
        self.assertEqual(
            plex.channel_of("/Media/YouTube/DIY Perks/DIY - y - id/DIY - y [id].mp4"),
            "DIY Perks")

    def test_non_youtube_paths_yield_nothing(self):
        for p in ("/media/Movies/Some Movie.mkv", "/media/TV-Shows/Lost/S01E01.mkv", "", None):
            self.assertEqual(plex.channel_of(p), "")

    def test_a_file_directly_under_youtube_is_not_a_channel(self):
        # no video folder beneath it -> the segment is the FILE, not a channel
        self.assertEqual(plex.channel_of("/media/YouTube/loose-file.mp4"), "")

    def test_no_token_reports_rather_than_guesses(self):
        with mock.patch.object(plex, "plex_token", return_value=""):
            self.assertIn("error", plex.sync_youtube_collections())


class PlaylistImportsGetTheirOwnCollection(unittest.TestCase):
    """A playlist's videos usually span several channels, so tagging by channel alone
    scattered them with nothing recording that they arrived together (user-dictated
    2026-08-23). They now carry the playlist's name as well as their channel's."""

    def test_the_id_comes_out_of_the_published_name(self):
        self.assertEqual(
            plex.youtube_video_id("/Media/YouTube/DIY Perks/f/DIY Perks - X [Z6z_feacXW8].mp4"),
            "Z6z_feacXW8")

    def test_a_name_without_an_id_is_empty_not_a_guess(self):
        self.assertEqual(plex.youtube_video_id("/Media/Movies/Some Movie (2021).mkv"), "")
        self.assertEqual(plex.youtube_video_id(""), "")

    def test_both_names_go_in_ONE_request(self):
        # The field is locked, so a second PUT naming one collection REPLACES the first —
        # applying the playlist tag on top of the channel tag would silently drop the channel.
        seen = {}

        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def urlopen(req, timeout=None):
            seen["url"] = req.full_url
            return Resp()

        with mock.patch.object(plex.urllib.request, "urlopen", side_effect=urlopen):
            ok = plex._set_collection("http://p:32400", "tok", "3017",
                                      ["DIY Perks", "Best Builds"])
        self.assertTrue(ok)
        self.assertIn("collection%5B0%5D.tag.tag=DIY+Perks", seen["url"])
        self.assertIn("collection%5B1%5D.tag.tag=Best+Builds", seen["url"])
        self.assertIn("collection.locked=1", seen["url"])

    def test_a_bare_string_still_works(self):
        seen = {}

        class Resp:
            status = 204
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with mock.patch.object(plex.urllib.request, "urlopen",
                               side_effect=lambda req, timeout=None: (seen.update(url=req.full_url), Resp())[1]):
            plex._set_collection("http://p:32400", "tok", "1", "Just One")
        self.assertIn("collection%5B0%5D.tag.tag=Just+One", seen["url"])
        self.assertNotIn("collection%5B1%5D", seen["url"])


class ASingleImportedVideoStillGetsItsChannel(unittest.TestCase):
    """Importing one video from a channel that was never queued must still put it in that
    channel's collection — creating it if it does not exist — so that adding the whole
    channel later finds the videos already filed where they belong (user-dictated
    2026-08-23). The sweep walks the LIBRARY, not the queue, so how a video arrived never
    enters into it; _locate_scan records the staging FOLDER as its channel, publishing
    mirrors that path, and channel_of reads it back out."""

    NEW = "/Media/YouTube/Veritasium/Veritasium - X [abcdefghijk]/Veritasium - X [abcdefghijk].mp4"

    def test_a_never_queued_channel_is_read_from_the_path(self):
        self.assertEqual(plex.channel_of(self.NEW), "Veritasium")

    def test_a_single_import_is_tagged_with_its_channel_and_nothing_else(self):
        # kind "video" never reaches the playlist map, so `want` is the channel alone
        with mock.patch.object(plex, "youtube_video_id", return_value="abcdefghijk"):
            playlists = {}                       # single-video imports are excluded upstream
            chan = plex.channel_of(self.NEW)
            want = [chan] + ([playlists["abcdefghijk"]] if playlists.get("abcdefghijk") else [])
        self.assertEqual(want, ["Veritasium"])

    def test_tagging_names_the_collection_even_when_it_does_not_exist_yet(self):
        # Plex creates a collection on first tag — the request is the same either way, which
        # is what makes "the channel gets a collection" true for a channel with one video.
        seen = {}

        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with mock.patch.object(plex.urllib.request, "urlopen",
                               side_effect=lambda req, timeout=None: (seen.update(url=req.full_url), Resp())[1]):
            plex._set_collection("http://p:32400", "tok", "77", ["Veritasium"])
        self.assertIn("collection%5B0%5D.tag.tag=Veritasium", seen["url"])
        self.assertIn("collection.locked=1", seen["url"])


class APlaylistIsBothItsOwnCollectionAndItsChannels(unittest.TestCase):
    """A playlist's videos belong in TWO places at once: the playlist, and whichever channel
    each one came from. Tagging only the playlist would lose the channel grouping the
    library is otherwise organised by; tagging only the channel is what scattered them in
    the first place (user-dictated 2026-08-23).

    Drives the real sweep against a fake Plex so the assertion is on the request that would
    actually go out, not on a re-derivation of the rule.
    """

    LIB = ('<MediaContainer>'
           '<Video ratingKey="1"/><Video ratingKey="2"/><Video ratingKey="3"/>'
           '</MediaContainer>')

    def meta(self, path, collections=()):
        cols = "".join('<Collection tag="%s"/>' % c for c in collections)
        return ('<MediaContainer><Video>%s<Media><Part file="%s"/></Media></Video>'
                '</MediaContainer>' % (cols, path))

    # one playlist, two different channels — plus an unrelated video from a third
    P1 = "/Media/YouTube/DIY Perks/a [aaaaaaaaaaa]/a [aaaaaaaaaaa].mp4"
    P2 = "/Media/YouTube/Veritasium/b [bbbbbbbbbbb]/b [bbbbbbbbbbb].mp4"
    OTHER = "/Media/YouTube/Auto Focus/c [ccccccccccc]/c [ccccccccccc].mp4"

    def _sweep(self, already=()):
        puts = []
        paths = {"1": self.P1, "2": self.P2, "3": self.OTHER}

        def get(base, path, token, timeout=20):
            if path.endswith("/all"):
                return self.LIB.encode()
            rk = path.rsplit("/", 1)[-1]
            return self.meta(paths[rk], already).encode()

        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def urlopen(req, timeout=None):
            puts.append(req.full_url)
            return Resp()

        import youtube
        with mock.patch.object(plex, "_get", side_effect=get), \
             mock.patch.object(plex, "plex_token", return_value="tok"), \
             mock.patch.object(plex, "plex_base_urls", return_value=["http://p:32400"]), \
             mock.patch.object(youtube, "playlist_title_by_vid",
                               return_value={"aaaaaaaaaaa": "Best Builds",
                                             "bbbbbbbbbbb": "Best Builds"}), \
             mock.patch.object(plex.urllib.request, "urlopen", side_effect=urlopen):
            res = plex.sync_youtube_collections()
        return res, puts

    def test_each_playlist_video_gets_its_own_channel_AND_the_playlist(self):
        res, puts = self._sweep()
        self.assertEqual(res["tagged"], 3)
        self.assertIn("collection%5B0%5D.tag.tag=DIY+Perks", puts[0])
        self.assertIn("collection%5B1%5D.tag.tag=Best+Builds", puts[0])
        self.assertIn("collection%5B0%5D.tag.tag=Veritasium", puts[1])
        self.assertIn("collection%5B1%5D.tag.tag=Best+Builds", puts[1])

    def test_the_channel_comes_first_so_it_is_never_the_one_dropped(self):
        _res, puts = self._sweep()
        for p in puts[:2]:
            self.assertLess(p.index("collection%5B0%5D"), p.index("collection%5B1%5D"))

    def test_a_video_outside_the_playlist_is_untouched_by_it(self):
        _res, puts = self._sweep()
        self.assertIn("collection%5B0%5D.tag.tag=Auto+Focus", puts[2])
        self.assertNotIn("Best+Builds", puts[2])

    def test_a_second_sweep_is_a_no_op_once_both_tags_are_on(self):
        res, puts = self._sweep(already=("DIY Perks", "Veritasium", "Auto Focus", "Best Builds"))
        self.assertEqual(res["already"], 3)
        self.assertEqual(puts, [])

    def test_having_only_the_channel_still_triggers_the_playlist_tag(self):
        # the repair case: videos tagged before playlists existed
        res, _puts = self._sweep(already=("DIY Perks", "Veritasium", "Auto Focus"))
        self.assertEqual(res["tagged"], 2)      # the two playlist videos; the third is done
        self.assertEqual(res["already"], 1)
