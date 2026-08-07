import io
import json
import os
import tempfile
import unittest
from unittest import mock

import companion


def _probe(width=3840, height=2160, kbps=50000, dv=None, transfer="smpte2084",
           audio=None, duration=7200.0, fps="24000/1001"):
    return {"width": width, "height": height, "video_codec": "hevc",
            "video_kbps": kbps, "duration": duration, "fps": fps,
            "transfer": transfer, "dv_profile": dv,
            "audio": audio if audio is not None else
            [{"codec": "ac3", "profile": "", "channels": 6, "atmos": False, "lang": "eng"}],
            "subs": 2}


TRUEHD_ATMOS = {"codec": "truehd", "profile": "Dolby TrueHD + Dolby Atmos",
                "channels": 8, "atmos": True, "lang": "eng"}
TRUEHD = {"codec": "truehd", "profile": "Dolby TrueHD", "channels": 8,
          "atmos": False, "lang": "eng"}
DTS_HD_MA = {"codec": "dts", "profile": "DTS-HD MA", "channels": 8,
             "atmos": False, "lang": "eng"}
EAC3_ATMOS = {"codec": "eac3", "profile": "Dolby Digital Plus + Dolby Atmos",
              "channels": 8, "atmos": True, "lang": "eng"}
AC3 = {"codec": "ac3", "profile": "", "channels": 6, "atmos": False, "lang": "eng"}


class AudioLadder(unittest.TestCase):
    """The user-dictated ranking: TrueHD Atmos > TrueHD > DTS-HD MA > EAC3 Atmos
    > DTS > EAC3 > AC3 > AAC."""

    def test_ladder_order(self):
        ladder = [TRUEHD_ATMOS, TRUEHD, DTS_HD_MA, EAC3_ATMOS,
                  {"codec": "dts", "profile": "", "channels": 6, "atmos": False},
                  {"codec": "eac3", "profile": "", "channels": 6, "atmos": False},
                  AC3,
                  {"codec": "aac", "profile": "", "channels": 2, "atmos": False}]
        ranks = [companion.audio_rank(t) for t in ladder]
        self.assertEqual(ranks, sorted(ranks, reverse=True))

    def test_channels_break_ties(self):
        a = dict(TRUEHD_ATMOS, channels=8)
        b = dict(TRUEHD_ATMOS, channels=6)
        self.assertGreater(companion.audio_rank(a), companion.audio_rank(b))


class PedigreeRank(unittest.TestCase):
    def test_order(self):
        self.assertEqual(companion.pedigree_rank("Movie 2160p BluRay REMUX.mkv"), 3)
        self.assertEqual(companion.pedigree_rank("Movie 2160p BluRay x265.mkv"), 2)
        self.assertEqual(companion.pedigree_rank("Movie 2160p WEB-DL.mkv"), 1)
        self.assertEqual(companion.pedigree_rank("Movie.mkv"), 0)


class VerdictEngine(unittest.TestCase):
    """PURE decisions over two probes. Sides are 'nas'/'remote'."""

    def test_resolution_wins_first(self):
        v = companion.build_verdict(_probe(height=2160), _probe(width=1920, height=1080),
                                    "a REMUX.mkv", "b WEB.mkv")
        self.assertEqual(v["video_from"], "nas")
        self.assertIn("resolution", v["video_why"])

    def test_pedigree_breaks_resolution_tie(self):
        v = companion.build_verdict(_probe(kbps=40000), _probe(kbps=80000),
                                    "a 2160p REMUX.mkv", "b 2160p WEB-DL.mkv")
        self.assertEqual(v["video_from"], "nas")
        self.assertIn("pedigree", v["video_why"])

    def test_bitrate_breaks_the_rest(self):
        v = companion.build_verdict(_probe(kbps=40000), _probe(kbps=80000),
                                    "a 2160p REMUX.mkv", "b 2160p REMUX.mkv")
        self.assertEqual(v["video_from"], "remote")
        self.assertIn("bitrate", v["video_why"])

    def test_winner_with_real_dv_keeps_it_inline(self):
        v = companion.build_verdict(_probe(dv="8.1", kbps=60000), _probe(kbps=30000),
                                    "a 2160p REMUX.mkv", "b 2160p REMUX.mkv")
        self.assertEqual(v["rpu_from"], "nas")
        self.assertTrue(v["rpu_inline"])
        self.assertEqual(v["rpu_profile"], "8.1")

    def test_real_dv_grafts_from_the_loser_onto_the_better_base(self):
        # remote has real DV but the WORSE HDR10 -> RPU from remote onto NAS video
        v = companion.build_verdict(_probe(kbps=60000), _probe(dv="7.x", kbps=30000),
                                    "a 2160p REMUX.mkv", "b 2160p REMUX.mkv")
        self.assertEqual(v["video_from"], "nas")
        self.assertEqual(v["rpu_from"], "remote")
        self.assertFalse(v["rpu_inline"])
        self.assertEqual(v["rpu_profile"], "7.x")
        self.assertIn("grafted", v["rpu_why"])

    def test_graft_direction_reverses_with_the_better_base(self):
        # NAS has real DV but remote HDR10 is better -> RPU from nas onto remote video
        v = companion.build_verdict(_probe(dv="8.1", kbps=30000), _probe(kbps=60000),
                                    "a 2160p REMUX.mkv", "b 2160p REMUX.mkv")
        self.assertEqual(v["video_from"], "remote")
        self.assertEqual(v["rpu_from"], "nas")
        self.assertFalse(v["rpu_inline"])

    def test_no_real_dv_falls_back_to_resolve(self):
        v = companion.build_verdict(_probe(), _probe(kbps=30000),
                                    "a 2160p REMUX.mkv", "b 2160p REMUX.mkv")
        self.assertEqual(v["rpu_from"], "resolve")
        self.assertEqual(v["rpu_profile"], "resolve")

    def test_audio_donor_is_the_best_single_track(self):
        v = companion.build_verdict(_probe(audio=[AC3]),
                                    _probe(kbps=30000, audio=[TRUEHD_ATMOS, AC3]),
                                    "a.mkv", "b.mkv")
        self.assertEqual(v["audio_from"], "remote")
        self.assertEqual(v["audio_primary"]["codec"], "truehd")
        self.assertTrue(v["audio_primary"]["atmos"])
        self.assertIn("all of this copy's audio ships", v["audio_why"])

    def test_audio_tie_goes_to_the_video_winner_side(self):
        v = companion.build_verdict(_probe(audio=[TRUEHD_ATMOS]),
                                    _probe(kbps=30000, audio=[TRUEHD_ATMOS]),
                                    "a.mkv", "b.mkv")
        self.assertEqual(v["audio_from"], "nas")

    def test_reencode_measured_when_the_nas_winner_has_a_known_peak(self):
        v = companion.build_verdict(_probe(kbps=60000), _probe(kbps=30000),
                                    "a.mkv", "b.mkv", nas_peak_mbps=88.4)
        self.assertEqual(v["reencode"], {"predicted": True, "basis": "measured",
                                         "mbps": 88.4})

    def test_reencode_estimated_otherwise(self):
        v = companion.build_verdict(_probe(kbps=30000), _probe(kbps=60000),
                                    "a.mkv", "b.mkv", nas_peak_mbps=88.4)
        self.assertEqual(v["reencode"]["basis"], "estimate")   # remote won — peak unknown
        self.assertTrue(v["reencode"]["predicted"])            # 60 * 1.8 > 72

    def test_low_bitrate_estimate_predicts_no_reencode(self):
        v = companion.build_verdict(_probe(kbps=60000), _probe(kbps=15000, height=4320),
                                    "a.mkv", "b.mkv")
        self.assertFalse(v["reencode"]["predicted"])           # 15 * 1.8 = 27 <= 72

    def test_specs_lines_exist_for_the_card(self):
        v = companion.build_verdict(_probe(dv="8.1", audio=[TRUEHD_ATMOS]), _probe(),
                                    "a 2160p REMUX.mkv", "b WEB.mkv")
        self.assertIn("2160p", v["specs"]["nas"])
        self.assertIn("REMUX", v["specs"]["nas"])
        self.assertIn("TrueHD Atmos", v["specs"]["nas"])


class RelayClient(unittest.TestCase):
    def _cfg(self, url="http://relay.test:8789", token="tok"):
        return mock.patch.multiple(companion,
                                   relay_base=mock.Mock(return_value=url),
                                   relay_token=mock.Mock(return_value=token))

    def test_request_sends_the_bearer_header(self):
        seen = {}
        def fake_open(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            seen["url"] = req.full_url
            return io.BytesIO(b'{"entries": []}')
        with self._cfg(), mock.patch.object(companion.urllib.request, "urlopen",
                                            side_effect=fake_open):
            companion.relay_get_json("/v1/search", {"q": "x", "side": "seedbox"})
        self.assertEqual(seen["auth"], "Bearer tok")
        self.assertIn("side=seedbox", seen["url"])

    def test_unconfigured_is_a_relay_down_error(self):
        with mock.patch.object(companion, "relay_base", return_value=""):
            with self.assertRaises(companion.RelayDownError):
                companion._request("/v1/search")

    def test_http_codes_map_to_typed_errors(self):
        import urllib.error
        for code, exc in ((401, companion.RelayAuthError),
                          (503, companion.RelayBusyError)):
            err = urllib.error.HTTPError("u", code, "m", {}, io.BytesIO(b""))
            with self._cfg(), mock.patch.object(companion.urllib.request, "urlopen",
                                                side_effect=err):
                with self.assertRaises(exc):
                    companion._request("/v1/fetch")

    def test_error_strings_never_contain_the_token(self):
        import urllib.error
        err = urllib.error.HTTPError("u", 401, "m", {}, io.BytesIO(b""))
        with self._cfg(token="SECRET-TOKEN"), \
             mock.patch.object(companion.urllib.request, "urlopen", side_effect=err):
            try:
                companion._request("/v1/fetch")
            except companion.RelayError as e:
                self.assertNotIn("SECRET-TOKEN", str(e))

    def test_fetch_verifies_the_manifest_size(self):
        d = tempfile.mkdtemp()
        dest = os.path.join(d, "movie.mkv")
        with self._cfg(), mock.patch.object(companion, "_request",
                                            return_value=io.BytesIO(b"x" * 100)):
            ok, why = companion.fetch_to_file("/seedbox/m.mkv", dest, 200)
        self.assertFalse(ok)
        self.assertIn("incomplete", why)
        self.assertFalse(os.path.exists(dest))
        self.assertFalse(os.path.exists(dest + ".part"))       # partial removed

    def test_fetch_lands_atomically_on_exact_size(self):
        d = tempfile.mkdtemp()
        dest = os.path.join(d, "movie.mkv")
        with self._cfg(), mock.patch.object(companion, "_request",
                                            return_value=io.BytesIO(b"x" * 200)):
            ok, why = companion.fetch_to_file("/seedbox/m.mkv", dest, 200)
        self.assertTrue(ok)
        self.assertEqual(os.path.getsize(dest), 200)

    def test_fetch_reuses_a_complete_file(self):
        d = tempfile.mkdtemp()
        dest = os.path.join(d, "movie.mkv")
        open(dest, "wb").write(b"x" * 200)
        with mock.patch.object(companion, "_request",
                               side_effect=AssertionError("must not re-fetch")):
            ok, why = companion.fetch_to_file("/seedbox/m.mkv", dest, 200)
        self.assertTrue(ok)
        self.assertIn("already", why)

    def test_busy_retries_then_gives_up(self):
        d = tempfile.mkdtemp()
        dest = os.path.join(d, "movie.mkv")
        calls = []
        def busy(*a, **k):
            calls.append(1)
            raise companion.RelayBusyError("relay busy")
        with self._cfg(), \
             mock.patch.object(companion, "FETCH_BUSY_WAIT", 0.0), \
             mock.patch.object(companion, "_request", side_effect=busy):
            ok, why = companion.fetch_to_file("/seedbox/m.mkv", dest, 200)
        self.assertFalse(ok)
        self.assertIn("busy", why)
        self.assertEqual(len(calls), companion.FETCH_BUSY_TRIES)

    def test_search_keeps_folders_and_video_files_only(self):
        data = {"entries": [
            {"name": "Movie.2160p.REMUX", "path": "/seedbox/a", "size": 0, "is_dir": True},
            {"name": "Movie.mkv", "path": "/seedbox/b.mkv", "size": 9, "is_dir": False},
            {"name": "Movie.nfo", "path": "/seedbox/c.nfo", "size": 1, "is_dir": False},
        ]}
        with mock.patch.object(companion, "relay_get_json", return_value=data):
            out = companion.search("Movie (2021)")
        self.assertEqual([c["name"] for c in out], ["Movie.2160p.REMUX", "Movie.mkv"])

    def test_resolve_candidate_flattens_a_folder_to_its_largest_video(self):
        m = {"files": [{"rel": "movie.2160p.mkv", "size": 900},
                       {"rel": "sample/sample.mkv", "size": 10},
                       {"rel": "cover.jpg", "size": 5}], "bytes": 915}
        with mock.patch.object(companion, "manifest", return_value=m):
            c = companion.resolve_candidate("/seedbox/Release.Folder")
        self.assertEqual(c["path"], "/seedbox/Release.Folder/movie.2160p.mkv")
        self.assertEqual(c["size"], 900)

    def test_resolve_candidate_plain_file(self):
        m = {"files": [{"rel": "movie.mkv", "size": 900}], "bytes": 900}
        with mock.patch.object(companion, "manifest", return_value=m):
            c = companion.resolve_candidate("/seedbox/movie.mkv")
        self.assertEqual(c["path"], "/seedbox/movie.mkv")


class Book(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.patch = mock.patch.object(companion, "BOOK_FILE",
                                       os.path.join(self.d, "companions.json"))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_mark_and_entry_roundtrip(self):
        companion.mark("m.mkv", "found", title="Movie", candidates=[{"name": "x"}])
        e = companion.entry("m.mkv")
        self.assertEqual(e["status"], "found")
        self.assertEqual(e["title"], "Movie")

    def test_confirm_requires_ready(self):
        companion.mark("m.mkv", "probing")
        self.assertEqual(companion.confirm("m.mkv")["status"], "error")
        companion.mark("m.mkv", "ready",
                       verdict={"video_from": "nas", "rpu_from": "remote",
                                "audio_from": "remote"},
                       companion={"path": "/seedbox/x.mkv", "name": "x.mkv", "size": 9})
        self.assertEqual(companion.confirm("m.mkv")["status"], "confirmed")
        cv = companion.confirmed_verdict("m.mkv")
        self.assertEqual(cv["companion"]["name"], "x.mkv")

    def test_confirmed_verdict_is_none_until_confirmed(self):
        companion.mark("m.mkv", "ready", verdict={"video_from": "nas"},
                       companion={"path": "p", "name": "n", "size": 1})
        self.assertIsNone(companion.confirmed_verdict("m.mkv"))

    def test_unpair_deletes_the_entry(self):
        companion.mark("m.mkv", "ready")
        companion.unpair("m.mkv")
        self.assertEqual(companion.entry("m.mkv"), {})

    def test_corrupt_book_reads_as_empty(self):
        open(companion.BOOK_FILE, "w").write("{nope")
        self.assertEqual(companion.entry("m.mkv"), {})
        companion.mark("m.mkv", "searching")        # and writes fine over it
        self.assertEqual(companion.entry("m.mkv")["status"], "searching")

    def test_book_view_trims_candidates(self):
        companion.mark("m.mkv", "found",
                       candidates=[{"name": f"c{i}", "size": i, "path": f"/s/{i}",
                                    "is_dir": False} for i in range(20)],
                       local_probe={"big": "blob"})
        v = companion.book_view()["m.mkv"]
        self.assertEqual(len(v["candidates"]), 8)
        self.assertNotIn("local_probe", v)          # bulky probes stay out of the poll


class ProbeNormalization(unittest.TestCase):
    def _ffprobe_json(self, profile="Dolby TrueHD + Dolby Atmos"):
        return json.dumps({
            "streams": [
                {"codec_type": "video", "codec_name": "hevc", "width": 3840,
                 "height": 2160, "bit_rate": "52000000", "r_frame_rate": "24000/1001",
                 "color_transfer": "smpte2084",
                 "side_data_list": [{"side_data_type": "DOVI configuration record",
                                     "dv_profile": 7}]},
                {"codec_type": "audio", "codec_name": "truehd", "profile": profile,
                 "channels": 8, "tags": {"language": "eng"}},
                {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
            ],
            "format": {"duration": "7200.5"},
        })

    def test_normalizes_video_audio_and_dv(self):
        r = mock.Mock(returncode=0, stdout=self._ffprobe_json())
        with mock.patch.object(companion.subprocess, "run", return_value=r):
            p = companion.probe_media("/x/movie.mkv")
        self.assertEqual(p["height"], 2160)
        self.assertEqual(p["video_kbps"], 52000)
        self.assertEqual(p["dv_profile"], "7.x")
        self.assertTrue(p["audio"][0]["atmos"])
        self.assertEqual(p["subs"], 1)

    def test_atmos_filename_fallback(self):
        r = mock.Mock(returncode=0, stdout=self._ffprobe_json(profile="Dolby TrueHD"))
        with mock.patch.object(companion.subprocess, "run", return_value=r):
            p = companion.probe_media("/x/m.mkv", name="Movie.TrueHD.Atmos.7.1.mkv")
        self.assertTrue(p["audio"][0]["atmos"])

    def test_failed_probe_is_empty(self):
        r = mock.Mock(returncode=1, stdout="")
        with mock.patch.object(companion.subprocess, "run", return_value=r):
            self.assertEqual(companion.probe_media("/x/m.mkv"), {})


if __name__ == "__main__":
    unittest.main()


class RelayBaseDiscovery(unittest.TestCase):
    def test_config_key_wins(self):
        with mock.patch.object(companion, "_config",
                               return_value={"shuttle_relay_url": "http://cfg:8789/"}):
            self.assertEqual(companion.relay_base(), "http://cfg:8789")

    def test_falls_back_to_shuttles_relay_json(self):
        d = tempfile.mkdtemp()
        rj = os.path.join(d, "relay.json")
        json.dump({"base_url": "http://shuttle:8789/"}, open(rj, "w"))
        with mock.patch.object(companion, "_config", return_value={}), \
             mock.patch.object(companion, "SHUTTLE_RELAY_FILE", rj):
            self.assertEqual(companion.relay_base(), "http://shuttle:8789")

    def test_unconfigured_is_empty(self):
        with mock.patch.object(companion, "_config", return_value={}), \
             mock.patch.object(companion, "SHUTTLE_RELAY_FILE", "/no/such/relay.json"):
            self.assertEqual(companion.relay_base(), "")


class CounterpartSweep(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.pb = mock.patch.object(companion, "BOOK_FILE",
                                    os.path.join(self.d, "companions.json"))
        self.pb.start()

    def tearDown(self):
        self.pb.stop()

    def _sweep(self, entries, results, atmos=False, on_update=None):
        # run the sweep synchronously: patch Thread to invoke work() inline
        class T:
            def __init__(self, target=None, **kw): self.t = target
            def start(self): self.t()
        with mock.patch.object(companion, "configured", return_value=True), \
             mock.patch.object(companion.threading, "Thread", T), \
             mock.patch.object(companion.time, "sleep", lambda s: None), \
             mock.patch.object(companion, "_probe_nas_atmos", return_value=atmos), \
             mock.patch.object(companion, "search", side_effect=results):
            companion.sweep_counterparts(entries, on_update=on_update)

    def test_sweep_caches_the_answer(self):
        self._sweep([{"name": "a.mkv", "title": "A", "dir": "/m"}],
                    [[{"name": "a REMUX.mkv", "path": "/seedbox/a", "size": 9,
                       "is_dir": False}]])
        e = companion.entry("a.mkv")
        self.assertTrue(e["counterpart"])
        self.assertEqual(len(e["candidates"]), 1)
        self.assertIsNone(e.get("status"))               # NEVER creates a panel state
        self.assertNotIn("a.mkv", companion.book_view()) # and stays out of the app's view
        self.assertTrue(companion.counterparts()["a.mkv"]["counterpart"])

    def test_no_match_caches_false(self):
        self._sweep([{"name": "b.mkv", "title": "B", "dir": "/m"}], [[]])
        self.assertFalse(companion.entry("b.mkv")["counterpart"])

    def test_fresh_answers_are_not_reswept(self):
        companion.mark("c.mkv", None, counterpart=True, counterpart_at=__import__("time").time())
        self._sweep([{"name": "c.mkv", "title": "C", "dir": "/m"}],
                    AssertionError("fresh answer must not re-search"))
        self.assertTrue(companion.entry("c.mkv")["counterpart"])

    def test_active_pairing_is_never_touched(self):
        companion.mark("d.mkv", "ready", verdict={"video_from": "nas"})
        self._sweep([{"name": "d.mkv", "title": "D", "dir": "/m"}],
                    AssertionError("active flow must not be swept"))
        self.assertEqual(companion.entry("d.mkv")["status"], "ready")

    def test_unconfigured_sweep_is_a_noop(self):
        with mock.patch.object(companion, "configured", return_value=False), \
             mock.patch.object(companion.threading, "Thread",
                               side_effect=AssertionError("no thread when unconfigured")):
            companion.sweep_counterparts([{"name": "e.mkv"}])

    def test_start_search_reuses_a_fresh_sweep_result(self):
        companion.mark("f.mkv", None, counterpart=True,
                       counterpart_at=__import__("time").time(),
                       candidates=[{"name": "x", "path": "/seedbox/x", "size": 1,
                                    "is_dir": False}])
        with mock.patch.object(companion, "configured", return_value=True), \
             mock.patch.object(companion.threading, "Thread",
                               side_effect=AssertionError("cached candidates skip the search")):
            out = companion.start_search("f.mkv", "/m", "F")
        self.assertEqual(out["status"], "found")
        self.assertEqual(companion.entry("f.mkv")["status"], "found")


class AtmosProbeInSweep(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.pb = mock.patch.object(companion, "BOOK_FILE",
                                    os.path.join(self.d, "companions.json"))
        self.pb.start()

    def tearDown(self):
        self.pb.stop()

    def _sweep(self, entries, results, atmos, on_update=None):
        class T:
            def __init__(self, target=None, **kw): self.t = target
            def start(self): self.t()
        with mock.patch.object(companion, "configured", return_value=True), \
             mock.patch.object(companion.threading, "Thread", T), \
             mock.patch.object(companion.time, "sleep", lambda s: None), \
             mock.patch.object(companion, "_probe_nas_atmos", return_value=atmos), \
             mock.patch.object(companion, "search", side_effect=results):
            companion.sweep_counterparts(entries, on_update=on_update)

    def test_atmos_movie_is_marked_and_never_searched(self):
        ticks = []
        self._sweep([{"name": "a.mkv", "title": "A", "dir": "/m"}],
                    AssertionError("Atmos movie must not burn a search"),
                    atmos=True, on_update=lambda: ticks.append(1))
        e = companion.entry("a.mkv")
        self.assertTrue(e["nas_atmos"])
        self.assertNotIn("counterpart", e)
        self.assertEqual(len(ticks), 1)          # the cache got told to re-filter
        self.assertTrue(companion.counterparts()["a.mkv"]["atmos"])

    def test_non_atmos_movie_is_probed_then_searched(self):
        ticks = []
        self._sweep([{"name": "b.mkv", "title": "B", "dir": "/m"}],
                    [[{"name": "b REMUX.mkv", "path": "/seedbox/b", "size": 9,
                       "is_dir": False}]],
                    atmos=False, on_update=lambda: ticks.append(1))
        e = companion.entry("b.mkv")
        self.assertFalse(e["nas_atmos"])
        self.assertTrue(e["counterpart"])
        self.assertEqual(len(ticks), 2)          # once per answer

    def test_probe_failure_leaves_atmos_unknown_for_retry(self):
        self._sweep([{"name": "c.mkv", "title": "C", "dir": "/m"}], [[]], atmos=None)
        e = companion.entry("c.mkv")
        self.assertNotIn("nas_atmos", e)         # unanswered → re-probed next sweep
        self.assertFalse(e["counterpart"])       # the search still ran (fail-open on probe)
