import unittest
from unittest import mock

import youtarr


class Client(unittest.TestCase):
    def test_no_creds_returns_none_without_network(self):
        with mock.patch.object(youtarr, "_creds", return_value=("", "")), \
             mock.patch.object(youtarr, "_post", side_effect=AssertionError("no network")):
            self.assertIsNone(youtarr.subscribed_channels())

    def test_login_then_getchannels_returns_uploader_names(self):
        youtarr._TOKEN["token"] = None
        with mock.patch.object(youtarr, "_creds", return_value=("u", "p")), \
             mock.patch.object(youtarr, "base_urls", return_value=["http://x:3087"]), \
             mock.patch.object(youtarr, "_post", return_value={"token": "TK"}) as lg, \
             mock.patch.object(youtarr, "_get",
                               return_value=[{"uploader": "Wizards with Guns"}, {"uploader": "al jokes"}]):
            names = youtarr.subscribed_channels()
        self.assertEqual(names, ["Wizards with Guns", "al jokes"])   # sorted uploaders
        lg.assert_called_once()                                      # logged in once

    def test_dict_response_and_dedup(self):
        youtarr._TOKEN["token"] = "cached"
        with mock.patch.object(youtarr, "_creds", return_value=("u", "p")), \
             mock.patch.object(youtarr, "base_urls", return_value=["http://x:3087"]), \
             mock.patch.object(youtarr, "_get",
                               return_value={"channels": [{"uploader": "A"}, {"uploader": "A"}, {"title": "B"}]}):
            self.assertEqual(youtarr.subscribed_channels(), ["A", "B"])


class Forget(unittest.TestCase):
    class FakeFTP:
        def __init__(self, content): self.content = content.encode(); self.stored = None
        def size(self, path): return len(self.content)
        def retrbinary(self, cmd, cb): cb(self.content)
        def storbinary(self, cmd, fp): self.stored = fp.read()
        def delete(self, path): pass
        def rename(self, a, b): pass
        def quit(self): pass

    def test_strips_only_the_given_ids(self):
        archive = "youtube aaaaaaaaaa1\nyoutube bbbbbbbbbb2\nyoutube ccccccccc33\n"
        f = self.FakeFTP(archive)
        with mock.patch("transfer.connect", return_value=f):
            removed = youtarr.forget_downloads(["aaaaaaaaaa1", "ccccccccc33", "zzzznotthere"])
        self.assertEqual(removed, 2)
        self.assertEqual(f.stored.decode(), "youtube bbbbbbbbbb2\n")   # only the untouched line remains

    def test_no_ids_never_touches_the_archive(self):
        with mock.patch("transfer.connect", side_effect=AssertionError("should not connect")):
            self.assertEqual(youtarr.forget_downloads([]), 0)

    def test_channel_video_ids_parses_both_key_shapes(self):
        with mock.patch.object(youtarr, "_call",
                               return_value={"videos": [{"youtube_id": "aaaaaaaaaa1"},
                                                        {"youtubeId": "bbbbbbbbbb2"}, {"nope": 1}]}):
            self.assertEqual(youtarr.channel_video_ids("UCx"), ["aaaaaaaaaa1", "bbbbbbbbbb2"])


if __name__ == "__main__":
    unittest.main()
