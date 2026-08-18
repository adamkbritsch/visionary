import unittest

import ytlinks


class ParseLink(unittest.TestCase):
    """Every shape a pasted YouTube link can take. Anything unrecognised must come back
    'unknown' — a wrong guess would queue the wrong thing."""

    def k(self, url):
        return ytlinks.parse_link(url)["kind"]

    def test_video_urls(self):
        for u in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                  "http://youtube.com/watch?v=dQw4w9WgXcQ",
                  "https://youtu.be/dQw4w9WgXcQ",
                  "https://youtu.be/dQw4w9WgXcQ?si=Xy_z-123",
                  "https://www.youtube.com/shorts/dQw4w9WgXcQ",
                  "https://www.youtube.com/live/dQw4w9WgXcQ",
                  "https://www.youtube.com/embed/dQw4w9WgXcQ",
                  "https://m.youtube.com/watch?v=dQw4w9WgXcQ&t=42s",
                  "music.youtube.com/watch?v=dQw4w9WgXcQ",
                  "dQw4w9WgXcQ"):
            r = ytlinks.parse_link(u)
            self.assertEqual(r["kind"], "video", u)
            self.assertEqual(r["video_id"], "dQw4w9WgXcQ", u)
            self.assertFalse(r["ambiguous"], u)

    def test_playlist_urls(self):
        for u in ("https://www.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf",
                  "youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf&si=zz",
                  "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"):
            r = ytlinks.parse_link(u)
            self.assertEqual(r["kind"], "playlist", u)
            self.assertEqual(r["playlist_id"], "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf", u)
            self.assertEqual(r["video_id"], "", u)

    def test_video_inside_a_playlist_is_ambiguous_and_reports_both(self):
        # user-dictated: ASK which was meant rather than guessing either way
        r = ytlinks.parse_link("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLxy&index=3")
        self.assertEqual(r["kind"], "video")
        self.assertTrue(r["ambiguous"])
        self.assertEqual(r["video_id"], "dQw4w9WgXcQ")
        self.assertEqual(r["playlist_id"], "PLxy")

    def test_channel_and_handle(self):
        r = ytlinks.parse_link("https://www.youtube.com/channel/UCXuqSBlHAE6Xw-yeJA0Tunw")
        self.assertEqual(r["kind"], "channel")
        self.assertEqual(r["channel_id"], "UCXuqSBlHAE6Xw-yeJA0Tunw")
        self.assertEqual(ytlinks.parse_link("UCXuqSBlHAE6Xw-yeJA0Tunw")["kind"], "channel")
        for u in ("https://www.youtube.com/@LinusTechTips",
                  "youtube.com/@LinusTechTips/videos",
                  "@LinusTechTips"):
            r = ytlinks.parse_link(u)
            self.assertEqual(r["kind"], "handle", u)
            self.assertEqual(r["handle"], "LinusTechTips", u)
        # legacy /c/ and /user/ are handles too — they need the same API lookup
        self.assertEqual(ytlinks.parse_link("https://m.youtube.com/c/SomeName")["handle"], "SomeName")
        self.assertEqual(ytlinks.parse_link("https://youtube.com/user/SomeName")["handle"], "SomeName")

    def test_junk_is_unknown_never_a_guess(self):
        for u in ("", None, "   ", "https://example.com/watch?v=nope",
                  "not a link at all", "https://vimeo.com/12345678"):
            self.assertEqual(self.k(u), "unknown", repr(u))

    def test_bare_token_that_could_be_either_resolves_as_a_video(self):
        # "PLabc-123_x" is a valid 11-char video id AND looks like a playlist class. Video
        # wins (11 chars is exactly a video id); the resolve step shows the title before
        # anything is queued, so the user catches a wrong call there.
        r = ytlinks.parse_link("PLabc-123_x")
        self.assertEqual(r["kind"], "video")
        self.assertEqual(r["video_id"], "PLabc-123_x")

    def test_private_playlists_parse_so_the_api_reports_why(self):
        # WL/LM are real but inaccessible — a clear API error beats "unknown link"
        self.assertEqual(self.k("https://www.youtube.com/playlist?list=WL"), "playlist")


if __name__ == "__main__":
    unittest.main()
