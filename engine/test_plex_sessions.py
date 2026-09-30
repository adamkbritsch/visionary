"""plex.sessions_playing() / is_playing() — the prefetcher's failsafe probe."""
import unittest
from unittest import mock
import plex

PLAYING = b'<MediaContainer size="1"><Video><Player state="playing"/></Video></MediaContainer>'
BUFFERING = b'<MediaContainer size="1"><Track><Player state="buffering"/></Track></MediaContainer>'
PAUSED = b'<MediaContainer size="1"><Video><Player state="paused"/></Video></MediaContainer>'
EMPTY = b'<MediaContainer size="0"></MediaContainer>'


class SessionsPlaying(unittest.TestCase):
    def test_playing(self):     self.assertTrue(plex.sessions_playing(PLAYING))
    def test_buffering(self):   self.assertTrue(plex.sessions_playing(BUFFERING))   # buffering counts (I/O active)
    def test_paused(self):      self.assertFalse(plex.sessions_playing(PAUSED))
    def test_empty(self):       self.assertFalse(plex.sessions_playing(EMPTY))
    def test_garbage(self):     self.assertFalse(plex.sessions_playing(b"<<not xml"))


class IsPlaying(unittest.TestCase):
    def test_no_token_is_none(self):
        with mock.patch.object(plex, "plex_token", return_value=""):
            self.assertIsNone(plex.is_playing())

    def test_reaches_plex_returns_bool(self):
        with mock.patch.object(plex, "plex_token", return_value="tok"), \
             mock.patch.object(plex, "plex_base_urls", return_value=["http://x:32400"]), \
             mock.patch.object(plex, "_get", return_value=PLAYING):
            self.assertTrue(plex.is_playing())

    def test_unreachable_is_none(self):
        with mock.patch.object(plex, "plex_token", return_value="tok"), \
             mock.patch.object(plex, "plex_base_urls", return_value=["http://x:32400"]), \
             mock.patch.object(plex, "_get", side_effect=OSError("refused")):
            self.assertIsNone(plex.is_playing())


if __name__ == "__main__":
    unittest.main()


class SessionsDetail(unittest.TestCase):
    """The DV lane's view: it throttles for ANY session and never renames the file being played."""
    TWO = (b'<MediaContainer size="2">'
           b'<Video><Media><Part file="/media/vol3/Movies/Drive (2011).mkv"/></Media>'
           b'<Player state="paused"/></Video>'
           b'<Track><Media><Part file="/media/Music/a.flac"/></Media><Player state="playing"/></Track>'
           b'</MediaContainer>')

    def test_counts_paused_sessions_and_names_the_files(self):
        self.assertEqual(plex.sessions_detail(self.TWO),
                         {"count": 2, "files": {"Drive (2011).mkv", "a.flac"}})

    def test_empty_and_garbage(self):
        self.assertEqual(plex.sessions_detail(EMPTY), {"count": 0, "files": set()})
        self.assertEqual(plex.sessions_detail(b"<<"), {"count": 0, "files": set()})

    def test_unreachable_is_none_not_idle(self):
        with mock.patch.object(plex, "plex_token", return_value="tok"), \
             mock.patch.object(plex, "plex_base_urls", return_value=["http://x:32400"]), \
             mock.patch.object(plex, "_get", side_effect=OSError("down")):
            self.assertIsNone(plex.session_detail())
