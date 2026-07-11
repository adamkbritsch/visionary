import unittest
from unittest import mock

import ytdata


class Pure(unittest.TestCase):
    def test_iso_seconds(self):
        self.assertEqual(ytdata._iso_secs("PT12M30S"), 750)
        self.assertEqual(ytdata._iso_secs("PT1H2M3S"), 3723)
        self.assertEqual(ytdata._iso_secs("PT45S"), 45)
        self.assertEqual(ytdata._iso_secs(""), 0)

    def test_iso_ts(self):
        self.assertEqual(ytdata._iso_ts("2024-01-02T03:04:05Z"), 1704164645)   # UTC → unix ts
        self.assertEqual(ytdata._iso_ts(""), 0)
        self.assertEqual(ytdata._iso_ts("garbage"), 0)
        self.assertEqual(ytdata._iso_ts("2024-13-40T99:99:99Z"), 0)            # out-of-range → 0, no raise

    def test_not_connected_degrades(self):
        with mock.patch.object(ytdata, "_creds", return_value=("", "", "")):
            self.assertFalse(ytdata.connected())
            self.assertFalse(ytdata.configured())
            self.assertIsNone(ytdata.auth_url("http://x"))
            self.assertIsNone(ytdata.access_token())
            self.assertIsNone(ytdata.subscriptions())
            self.assertIsNone(ytdata.popular_videos("UCx"))

    def test_configured_needs_client_not_token(self):
        # client id+secret present but no refresh token → configured (can start OAuth), not connected.
        with mock.patch.object(ytdata, "_creds", return_value=("CID", "SEC", "")):
            self.assertTrue(ytdata.configured())
            self.assertFalse(ytdata.connected())
        with mock.patch.object(ytdata, "_creds", return_value=("CID", "SEC", "RT")):
            self.assertTrue(ytdata.configured())
            self.assertTrue(ytdata.connected())

    def test_auth_url_built_when_client_configured(self):
        with mock.patch.object(ytdata, "_creds", return_value=("CID", "SEC", "")):
            u = ytdata.auth_url("http://localhost:8765/oauth/youtube")
        self.assertIn("client_id=CID", u)
        self.assertIn("youtube.readonly", u)
        self.assertIn("access_type=offline", u)


if __name__ == "__main__":
    unittest.main()
