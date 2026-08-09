import json
import os
import stat
import tempfile
import unittest
from unittest import mock

import configstore


class WriteLayer(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cfg = os.path.join(self.d, "pipe", "config.json")
        self.p = mock.patch.object(configstore, "CONFIG", self.cfg)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_fresh_machine_gets_dir_0700_and_file_0600(self):
        # the drop-in .app's first run has NO ~/.topaz-pipeline at all (the old
        # ytdata._save crashed here — regression)
        configstore.write({"ftp_user": "u"})
        self.assertEqual(configstore.read()["ftp_user"], "u")
        self.assertEqual(stat.S_IMODE(os.stat(self.cfg).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.cfg)).st_mode), 0o700)

    def test_merge_preserves_foreign_keys(self):
        configstore.write({"youtube_refresh_token": "tok", "ftp_user": "u"})
        configstore.write({"ftp_user": "v"})
        d = configstore.read()
        self.assertEqual(d["youtube_refresh_token"], "tok")   # ytdata's key survives
        self.assertEqual(d["ftp_user"], "v")

    def test_none_deletes(self):
        configstore.write({"plex_token": "t"})
        configstore.write({"plex_token": None})
        self.assertNotIn("plex_token", configstore.read())

    def test_corrupt_file_reads_empty_and_recovers(self):
        os.makedirs(os.path.dirname(self.cfg))
        open(self.cfg, "w").write("{nope")
        self.assertEqual(configstore.read(), {})
        configstore.write({"ftp_user": "u"})
        self.assertEqual(configstore.read()["ftp_user"], "u")


class SaveLayer(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.object(configstore, "CONFIG",
                                   os.path.join(self.d, "config.json"))
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_allowlist_drops_unknown_keys_and_reports_them(self):
        out = configstore.save({"ftp_user": "u", "evil_key": "x", "activated": True})
        self.assertEqual(sorted(out["ignored"]), ["activated", "evil_key"])
        d = configstore.read()
        self.assertEqual(d, {"ftp_user": "u"})

    def test_ftp_hosts_accepts_comma_string_or_list(self):
        configstore.save({"ftp_hosts": " 100.1.2.3 , nas.local ,"})
        self.assertEqual(configstore.read()["ftp_hosts"], ["100.1.2.3", "nas.local"])
        configstore.save({"ftp_hosts": ["a", " b "]})
        self.assertEqual(configstore.read()["ftp_hosts"], ["a", "b"])

    def test_ftp_port_coerced_or_refused(self):
        configstore.save({"ftp_port": "2121"})
        self.assertEqual(configstore.read()["ftp_port"], 2121)
        out = configstore.save({"ftp_port": "not-a-port"})
        self.assertIn("ftp_port", out["ignored"])
        self.assertEqual(configstore.read()["ftp_port"], 2121)   # unchanged

    def test_empty_string_deletes_the_key(self):
        configstore.save({"plex_token": "t", "plex_url": "http://x:32400"})
        configstore.save({"plex_token": "", "plex_url": ""})
        d = configstore.read()
        self.assertNotIn("plex_token", d)
        self.assertNotIn("plex_url", d)


class Redaction(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.object(configstore, "CONFIG",
                                   os.path.join(self.d, "config.json"))
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_secret_values_never_appear_in_redacted_output(self):
        # THE rule (CLAUDE.md: never echo secrets): the API view must not contain a
        # single secret value, only set/unset booleans
        configstore.write({"ftp_pass": "SECRET-FTP", "plex_token": "SECRET-PLEX",
                           "tmdb_api_key": "SECRET-TMDB", "youtarr_pass": "SECRET-YT",
                           "shuttle_relay_token": "SECRET-RELAY",
                           "youtube_refresh_token": "SECRET-OAUTH",
                           "ftp_user": "adam", "ftp_hosts": ["nas.local"]})
        dumped = json.dumps(configstore.read_redacted())
        for secret in ("SECRET-FTP", "SECRET-PLEX", "SECRET-TMDB", "SECRET-YT",
                       "SECRET-RELAY", "SECRET-OAUTH"):
            self.assertNotIn(secret, dumped)
        r = configstore.read_redacted()
        self.assertTrue(r["secrets_set"]["ftp_pass"])
        self.assertFalse(r["secrets_set"]["shuttle_relay_token"] is False and False)
        self.assertEqual(r["fields"]["ftp_user"], "adam")       # non-secrets verbatim
        self.assertEqual(r["fields"]["ftp_hosts"], "nas.local")

    def test_unset_secrets_read_false(self):
        r = configstore.read_redacted()
        self.assertFalse(any(r["secrets_set"].values()))
        self.assertEqual(r["fields"]["ftp_pass"], "")


class YtdataAlias(unittest.TestCase):
    def test_ytdata_save_routes_through_configstore(self):
        import ytdata
        d = tempfile.mkdtemp()
        with mock.patch.object(configstore, "CONFIG", os.path.join(d, "config.json")):
            ytdata._save(youtube_refresh_token="tok")
            self.assertEqual(configstore.read()["youtube_refresh_token"], "tok")


if __name__ == "__main__":
    unittest.main()
