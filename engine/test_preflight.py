import os
import unittest
from unittest import mock

import preflight
import versions


class Pins(unittest.TestCase):
    """versions.py is the single source of truth for the pinned builds."""


    def test_pin_values(self):
        # the exact Topaz build this repo ships params for
        self.assertEqual(versions.TOPAZ_VERSION, "7.0.1")
        for gone in ("RESOLVE_APP", "RESOLVE_VERSION", "SUPPORTED_DISPLAYS", "REQUIRED_BACKING_SCALE"):
            self.assertFalse(hasattr(versions, gone), gone)        # no Resolve, no display rule

    def test_native_dv_pin_values(self):
        # the Dolby Vision toolchain the blind A/B was judged on
        self.assertEqual(versions.DOVI_TOOL_VERSION, "2.3.0")
        self.assertEqual(versions.HDR_ANALYZER_COMMIT, "383b0ad")
        self.assertEqual(versions.HDR_ANALYZER_SHA256,
                         "41727312f4a960b27d59cb0c0f24c2922dc82768da3503f083e4842a87fc099e")
        self.assertEqual(versions.NATIVE_LUT_SHA256,
                         "e410e5b7b899a07096a77e2f65bdeb6a4ab3cc60126358a81c8050251d8c59b0")
        self.assertEqual(versions.NATIVE_FFMPEG_MAJOR, 8)
        self.assertEqual(versions.HDR_ANALYZER_PATH,
                         os.path.expanduser("~/.topaz-pipeline/bin/hdr_analyzer_mvp"))
        self.assertEqual(versions.NATIVE_LUT_PATH,
                         os.path.expanduser("~/.topaz-pipeline/luts/rcm_sdr709_to_pq1000.cube"))


class VersionChecks(unittest.TestCase):
    def test_wrong_version_fails_exactly(self):
        with mock.patch.object(preflight, "_bundle_version", return_value=("7.0.2", "7.0.2")):
            self.assertFalse(preflight.check_topaz_version()["ok"])   # point builds refuse too

    def test_the_cheap_poll_is_the_topaz_pin_alone(self):
        self.assertEqual([c["id"] for c in preflight.run_cheap()], ["topaz_version"])


class PowerRule(unittest.TestCase):
    """Sustained 140 W. A desktop Mac has no battery and is mains-powered by definition;
    a laptop is judged by MODEL, because a 140 W brick in a 96 W-max machine still reports
    140 W. An unrecognised laptop falls back to the live reading rather than being blocked."""

    def _check(self, *, battery, model, watts):
        import power
        with mock.patch.object(power, "has_battery", return_value=battery), \
             mock.patch.object(power, "model_id", return_value=model), \
             mock.patch.object(power, "adapter_watts_sustained", return_value=watts):
            return preflight.check_power_adapter()

    def test_desktop_passes_with_no_adapter_reading(self):
        c = self._check(battery=False, model="Mac16,11", watts=None)
        self.assertTrue(c["ok"]); self.assertIn("mains-powered", c["detail"])

    def test_known_140w_laptop_passes(self):
        c = self._check(battery=True, model="Mac15,11", watts=140)
        self.assertTrue(c["ok"])

    def test_known_sub_140w_laptop_hard_fails(self):
        c = self._check(battery=True, model="MacBookPro18,3", watts=140)   # 14" M1 Pro, 96 W max
        self.assertFalse(c["ok"])
        self.assertEqual(c["severity"], "fail")     # a brick reporting 140 W must NOT save it

    def test_unknown_laptop_falls_back_to_live_wattage(self):
        ok = self._check(battery=True, model="Mac99,9", watts=140)
        self.assertTrue(ok["ok"])                   # future Mac: not blocked
        self.assertIn("isn't in the known", ok["detail"])
        bad = self._check(battery=True, model="Mac99,9", watts=96)
        self.assertFalse(bad["ok"])                 # but a small brick still fails


class Semantics(unittest.TestCase):
    def test_hard_ok_ignores_warn_checks(self):
        fails = [{"id": "a", "ok": True, "severity": "fail", "detail": "", "fix": ""}]
        warns = [{"id": "b", "ok": False, "severity": "warn", "detail": "", "fix": ""}]
        with mock.patch.object(preflight, "run_cheap", return_value=fails), \
             mock.patch.object(preflight, "check_power_adapter", return_value=warns[0]), \
             mock.patch.object(preflight, "check_brew_tools", return_value=fails[0]), \
             mock.patch.object(preflight, "check_python_deps", return_value=fails[0]), \
             mock.patch.object(preflight, "check_config", return_value=warns[0]), \
             mock.patch.object(preflight, "check_native_dv", return_value=fails[0]):
            r = preflight.run_checks()
        self.assertTrue(r["hard_ok"])            # warn failures don't gate arming
        self.assertFalse(r["ok"])                # but strict ok reflects them


    def test_cli_exit_codes(self):
        allpass = {"ok": True, "hard_ok": True, "checks": []}
        hardfail = {"ok": False, "hard_ok": False, "checks": []}
        warnonly = {"ok": False, "hard_ok": True, "checks": []}
        with mock.patch.object(preflight, "run_checks", return_value=allpass):
            self.assertEqual(preflight.main(["--json"]), 0)
        with mock.patch.object(preflight, "run_checks", return_value=hardfail):
            self.assertEqual(preflight.main(["--json"]), 1)
        with mock.patch.object(preflight, "run_checks", return_value=warnonly):
            self.assertEqual(preflight.main(["--json"]), 2)


class LiveOnReferenceMachine(unittest.TestCase):
    """On the maintainer's machine (the reference), the real hard checks must pass —
    skipped automatically anywhere the pinned apps aren't installed."""

    def test_reference_machine_hard_checks(self):
        if preflight._bundle_version(versions.TOPAZ_APP)[0] != versions.TOPAZ_VERSION:
            self.skipTest("pinned Topaz not installed — not the reference machine")
        r = preflight.run_cheap()
        self.assertTrue(all(c["ok"] for c in r), [c for c in r if not c["ok"]])


if __name__ == "__main__":
    unittest.main()


class ConfigCheck(unittest.TestCase):
    """Plex is OPTIONAL (README 'Configuration'): a blank plex_token must neither fail the
    config check nor block the FTP probe — it used to do both, so a Plex-less setup could
    never pass setup steps 9-10 (fact-check-caught)."""

    def _mocks(self, token, connect=None):
        import transfer, plex
        ms = [mock.patch.object(transfer, "nas_hosts", return_value=["10.0.0.2"]),
              mock.patch.object(transfer, "ftp_settings", return_value={"user": "u", "passwd": "p"}),
              mock.patch.object(plex, "plex_token", return_value=token)]
        if connect is not None:
            ms.append(mock.patch.object(transfer, "connect", return_value=connect))
        return ms

    class _FTP:
        def quit(self): pass

    def test_blank_plex_token_is_not_required(self):
        import contextlib
        with contextlib.ExitStack() as es:
            for m in self._mocks(""):
                es.enter_context(m)
            c = preflight.check_config(network=False)
        self.assertTrue(c["ok"])
        self.assertIn("Plex not configured (optional)", c["detail"])

    def test_blank_plex_token_still_probes_ftp_and_skips_plex(self):
        import contextlib
        with contextlib.ExitStack() as es:
            for m in self._mocks("", connect=self._FTP()):
                es.enter_context(m)
            c = preflight.check_config(network=True)
        self.assertTrue(c["ok"])                            # all-green is reachable Plex-less now
        self.assertIn("FTP: connected", c["detail"])        # the FTP probe actually ran
        self.assertIn("Plex: not configured (optional)", c["detail"])

    def test_missing_ftp_keys_still_fail_without_naming_plex(self):
        import contextlib, transfer
        with contextlib.ExitStack() as es:
            for m in self._mocks(""):
                es.enter_context(m)
            es.enter_context(mock.patch.object(transfer, "ftp_settings",
                                               return_value={"user": "", "passwd": ""}))
            c = preflight.check_config(network=False)
        self.assertFalse(c["ok"])
        self.assertIn("ftp_user", c["detail"])
        self.assertNotIn("plex_token", c["detail"])         # never demanded anymore

    def test_configured_plex_is_still_probed_and_must_answer(self):
        import contextlib, plex
        with contextlib.ExitStack() as es:
            for m in self._mocks("tok", connect=self._FTP()):
                es.enter_context(m)
            es.enter_context(mock.patch.object(plex, "plex_base_urls",
                                               return_value=["http://nas:32400"]))
            es.enter_context(mock.patch("urllib.request.urlopen", side_effect=OSError("refused")))
            c = preflight.check_config(network=True)
        self.assertFalse(c["ok"])                           # configured but unreachable = real failure
        self.assertIn("Plex:", c["detail"])


class FixStringsAlwaysPresent(unittest.TestCase):
    def test_passing_check_keeps_its_fix(self):
        # the in-app Setup rows show each check's remediation/command regardless of state
        c = preflight._check("x", True, "fail", "fine", "the fix text")
        self.assertEqual(c["fix"], "the fix text")

    def test_failing_check_keeps_it_too(self):
        c = preflight._check("x", False, "fail", "broken", "the fix text")
        self.assertEqual(c["fix"], "the fix text")


class ArmGate(unittest.TestCase):
    def test_arm_gate_is_the_pins_the_instant_deps_and_the_dv_toolchain(self):
        fake = lambda cid: {"id": cid, "ok": True, "severity": "fail", "detail": "", "fix": ""}
        with mock.patch.object(preflight, "run_cheap", return_value=[fake("topaz_version")]), \
             mock.patch.object(preflight, "check_brew_tools", return_value=fake("brew_tools")), \
             mock.patch.object(preflight, "check_python_deps", return_value=fake("python_deps")), \
             mock.patch.object(preflight, "check_native_dv", return_value=fake("native_dv")):
            ids = [c["id"] for c in preflight.run_arm_gate()]
        self.assertEqual(ids, ["topaz_version", "brew_tools", "python_deps", "native_dv"])

    def test_a_broken_dv_toolchain_refuses_to_arm(self):
        with mock.patch.object(preflight, "_native_dv_problems",
                               return_value=[("dovi_tool is 2.2.0", "install 2.3.0")]):
            row = preflight.check_native_dv()
        self.assertEqual((row["ok"], row["severity"]), (False, "fail"))
        self.assertIn("dovi_tool is 2.2.0", row["detail"])


class _NativeToolchain:
    """A fake native-DV toolchain in a temp dir: the analyzer and LUT are real files whose
    hashes ARE the pins (versions patched to them), and every tool probe is answered by
    `self.answers` — so each sub-check can be broken on its own."""

    def setUp(self):
        import hashlib, tempfile
        d = tempfile.mkdtemp()
        self.an = os.path.join(d, "hdr_analyzer_mvp")
        self.lut = os.path.join(d, "rcm.cube")
        with open(self.an, "wb") as f:
            f.write(b"analyzer build 383b0ad")
        with open(self.lut, "wb") as f:
            f.write(b"LUT_3D_SIZE 2\n")
        sha = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()
        p = mock.patch.multiple(versions, HDR_ANALYZER_PATH=self.an, NATIVE_LUT_PATH=self.lut,
                                HDR_ANALYZER_SHA256=sha(self.an), NATIVE_LUT_SHA256=sha(self.lut),
                                DOVI_TOOL_VERSION="2.3.0", NATIVE_FFMPEG_MAJOR=8)
        p.start(); self.addCleanup(p.stop)
        self.answers = {"hdr_analyzer_mvp": (0, "Usage: hdr_analyzer_mvp [OPTIONS] [INPUT]\n"),
                        "dovi_tool": (0, "dovi_tool 2.3.0\n"),
                        "ffmpeg": (0, "ffmpeg version 8.0 Copyright (c) 2000-2025\n")}
        self.probed = []

        def probe(argv, timeout=10):
            self.probed.append(list(argv))
            tool = argv[2] if argv[0] == "/usr/bin/env" else argv[0]   # dvnative.analyzer_argv
            return self.answers[os.path.basename(tool)]
        p = mock.patch.object(preflight, "_probe", side_effect=probe)
        p.start(); self.addCleanup(p.stop)
        preflight._SHA_CACHE.clear()
        self.addCleanup(preflight._SHA_CACHE.clear)

    def ran(self, name):
        # the analyzer is started through env (dvnative.analyzer_argv): its path is argv[2]
        return [a for a in self.probed
                if os.path.basename(a[2] if a[0] == "/usr/bin/env" else a[0]) == name]


class NativeDvCheck(_NativeToolchain, unittest.TestCase):
    """check_native_dv: analyzer exists + sha256 + EXECUTES, LUT sha256, dovi_tool's exact
    version, ffmpeg's major. One row; every failing sub-check is named in it."""

    def test_a_pinned_working_toolchain_passes(self):
        c = preflight.check_native_dv()
        self.assertTrue(c["ok"], c["detail"])
        self.assertEqual((c["id"], c["severity"]), ("native_dv", "fail"))
        self.assertIn("383b0ad", c["detail"])
        self.assertTrue(c["fix"])                        # fix is kept even on a pass
        import dvnative
        self.assertEqual(self.ran("hdr_analyzer_mvp"), [dvnative.analyzer_argv(self.an) + ["--help"]])

    def test_a_missing_analyzer_fails_and_is_not_run(self):
        os.remove(self.an)
        c = preflight.check_native_dv()
        self.assertFalse(c["ok"])
        self.assertIn("hdr_analyzer_mvp missing", c["detail"])
        self.assertIn("dvinstall.py --repair", c["fix"])   # says how to install the bundled build
        self.assertEqual(self.ran("hdr_analyzer_mvp"), [])

    def test_a_different_analyzer_build_fails_and_is_never_executed(self):
        with open(self.an, "ab") as f:
            f.write(b" patched")
        c = preflight.check_native_dv()
        self.assertFalse(c["ok"])
        self.assertIn("not the pinned build", c["detail"])
        self.assertEqual(self.ran("hdr_analyzer_mvp"), [])   # only the PINNED build runs

    def test_a_pinned_analyzer_that_will_not_load_fails(self):
        # the brew-upgrade case: the file is byte-identical, its libav*.62 links are gone
        self.answers["hdr_analyzer_mvp"] = (
            -6, "dyld[1]: Library not loaded: /opt/homebrew/opt/ffmpeg/lib/libavformat.62.dylib\n")
        c = preflight.check_native_dv()
        self.assertFalse(c["ok"])
        self.assertIn("does not run", c["detail"])
        self.assertIn("libavformat.62", c["detail"])
        self.assertIn("ffmpeg@8", c["fix"])                  # Setup links the keg-only formula

    def test_an_analyzer_that_hangs_fails(self):
        self.answers["hdr_analyzer_mvp"] = (None, "no answer within 10 s")
        c = preflight.check_native_dv()
        self.assertFalse(c["ok"])
        self.assertIn("no answer within 10 s", c["detail"])

    def test_a_missing_or_changed_lut_fails(self):
        with open(self.lut, "ab") as f:
            f.write(b"# edited\n")
        c = preflight.check_native_dv()
        self.assertFalse(c["ok"])
        self.assertIn("LUT is not the pinned file", c["detail"])
        os.remove(self.lut)
        c = preflight.check_native_dv()
        self.assertFalse(c["ok"])
        self.assertIn("LUT missing", c["detail"])

    def test_dovi_tool_must_be_the_exact_version(self):
        for answer, shown in (((0, "dovi_tool 2.2.0\n"), "2.2.0"),
                              ((0, "dovi_tool 2.3.1\n"), "2.3.1"),
                              ((None, "No such file or directory"), "not runnable"),
                              ((1, "error: unexpected argument\n"), "unreadable")):
            self.answers["dovi_tool"] = answer
            c = preflight.check_native_dv()
            self.assertFalse(c["ok"], answer)
            self.assertIn("dovi_tool is " + shown, c["detail"])
            self.assertIn("2.3.0", c["fix"])

    def test_ffmpeg_must_be_the_pinned_major(self):
        self.answers["ffmpeg"] = (0, "ffmpeg version 8.0.1 Copyright\n")
        self.assertTrue(preflight.check_native_dv()["ok"])         # a point release is fine
        for answer, shown in (((0, "ffmpeg version 7.1.1 Copyright\n"), "7"),
                              ((0, "ffmpeg version 9.0 Copyright\n"), "9"),
                              ((0, "ffmpeg version N-120000-gabc Copyright\n"), "unreadable"),
                              ((None, "No such file or directory"), "not runnable")):
            self.answers["ffmpeg"] = answer
            c = preflight.check_native_dv()
            self.assertFalse(c["ok"], answer)
            self.assertIn("ffmpeg major is " + shown, c["detail"])

    def test_every_failure_is_named_in_one_row(self):
        os.remove(self.an)
        os.remove(self.lut)
        self.answers["dovi_tool"] = (0, "dovi_tool 2.2.0\n")
        self.answers["ffmpeg"] = (0, "ffmpeg version 7.1 Copyright\n")
        c = preflight.check_native_dv()
        self.assertFalse(c["ok"])
        for part in ("hdr_analyzer_mvp missing", "LUT missing", "dovi_tool is 2.2.0",
                     "ffmpeg major is 7"):
            self.assertIn(part, c["detail"])


    def test_the_lut_hash_is_cached_by_size_and_mtime(self):
        import hashlib
        real = hashlib.sha256
        with mock.patch.object(preflight.hashlib, "sha256", side_effect=real) as h:
            first = preflight._sha256_cached(self.lut)
            self.assertEqual(preflight._sha256_cached(self.lut), first)
            self.assertEqual(h.call_count, 1)            # unchanged file -> not re-hashed
            st = os.stat(self.lut)
            with open(self.lut, "r+b") as f:            # same SIZE, different bytes...
                f.write(b"X")
            os.utime(self.lut, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))   # ...new mtime
            self.assertNotEqual(preflight._sha256_cached(self.lut), first)
            self.assertEqual(h.call_count, 2)


class NativeDvProbe(unittest.TestCase):
    def test_probes_carry_a_10_second_timeout(self):
        import subprocess
        done = subprocess.CompletedProcess(["x"], 0, stdout="out", stderr="err")
        with mock.patch.object(preflight.subprocess, "run", return_value=done) as run:
            self.assertEqual(preflight._probe(["x", "--help"]), (0, "outerr"))
        self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_a_hung_tool_cannot_hang_the_check(self):
        rc, text = preflight._probe(["/bin/sleep", "5"], timeout=0.2)
        self.assertIsNone(rc)
        self.assertIn("no answer", text)

    def test_a_missing_tool_is_reported_not_raised(self):
        rc, text = preflight._probe(["/nonexistent/hdr_analyzer_mvp", "--help"])
        self.assertIsNone(rc)
        self.assertTrue(text)


class NativeDvLiveOnReferenceMachine(unittest.TestCase):
    """On the reference machine the real native toolchain must match its pins — skipped
    anywhere the pinned analyzer isn't installed. Read-only: hashes two files and runs
    `--help` / `--version` / `-version`."""

    def test_reference_toolchain_matches_the_pins(self):
        if not os.path.isfile(versions.HDR_ANALYZER_PATH):
            self.skipTest("hdr_analyzer_mvp not installed — not the reference machine")
        c = preflight.check_native_dv()
        self.assertTrue(c["ok"], c["detail"])


class MasterMuxTool(unittest.TestCase):
    """Every master is Matroska (2026-10-18): the arm gate checks mkvmerge, with a version floor,
    and MP4Box is gone from it."""

    def test_the_tools_and_their_installer(self):
        self.assertIn("mkvmerge", preflight.BREW_TOOLS)
        self.assertNotIn("MP4Box", preflight.BREW_TOOLS)
        self.assertIn("mkvtoolnix", preflight.BREW_FORMULAE)
        self.assertNotIn("gpac", preflight.BREW_FORMULAE)

    def test_version_parse(self):
        self.assertEqual(preflight.mkvmerge_version("mkvmerge v99.0 ('Buka') 64-bit"), (99, 0))
        self.assertIsNone(preflight.mkvmerge_version("garbage"))

    def _check(self, version_text, rc=0):
        with mock.patch.object(preflight.os.path, "exists", return_value=True), \
             mock.patch.object(preflight, "_probe", return_value=(rc, version_text)):
            return preflight.check_brew_tools()

    def test_a_current_mkvmerge_passes(self):
        self.assertTrue(self._check("mkvmerge v99.0 ('Buka') 64-bit")["ok"])

    def test_one_below_the_floor_fails_and_says_why(self):
        c = self._check("mkvmerge v89.0 ('x') 64-bit")
        self.assertFalse(c["ok"])
        self.assertIn("89.0 is older than 90.0", c["detail"])
        self.assertIn("mkvtoolnix", c["fix"])

    def test_an_unreadable_version_fails(self):
        self.assertFalse(self._check("", rc=None)["ok"])

    def test_a_missing_tool_is_named(self):
        with mock.patch.object(preflight.os.path, "exists",
                               side_effect=lambda p: not p.endswith("/mkvmerge")):
            c = preflight.check_brew_tools()
        self.assertFalse(c["ok"])
        self.assertIn("mkvmerge", c["detail"])
