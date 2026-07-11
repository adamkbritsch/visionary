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
