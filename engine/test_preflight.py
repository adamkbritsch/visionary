import os
import unittest
from unittest import mock

import preflight
import versions


class Pins(unittest.TestCase):
    """versions.py is the single source of truth — the shim's hardcoded geometry must agree
    with it (a drifted edit to either would silently break clicks on the pinned hardware)."""

    def test_shim_scale_matches_pin(self):
        import dv_shim
        self.assertEqual(dv_shim.retina_scale(), versions.RETINA_SCALE)

    def test_every_supported_display_shares_the_invariant_scale(self):
        # The templates only match when the UI renders at the same BACKING-PIXEL size —
        # i.e. the same scale. Screen SIZE may differ (clicks are template-derived).
        for cfg in versions.SUPPORTED_DISPLAYS:
            self.assertEqual(cfg["scale"], versions.RETINA_SCALE, cfg["name"])

    def test_pin_values(self):
        # the exact builds this repo ships templates/params for — bump ONLY with new templates
        self.assertEqual(versions.RESOLVE_VERSION, "18.6.0")
        self.assertEqual(versions.TOPAZ_VERSION, "7.0.1")
        self.assertEqual(versions.DISPLAY_PIXELS, (3456, 2234))
        # the verified allow-list: built-in + the 4K dummy plug used for clamshell
        self.assertEqual([c["backing"] for c in versions.SUPPORTED_DISPLAYS],
                         [(3456, 2234), (3840, 2160)])

    def test_native_dv_pin_values(self):
        # ADDITIVE pins for the native DV lane: the toolchain the blind A/B was judged on
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
    def test_missing_app_fails_with_install_fix(self):
        with mock.patch.object(preflight, "_bundle_version", return_value=(None, None)):
            c = preflight.check_resolve_version()
        self.assertFalse(c["ok"]); self.assertEqual(c["severity"], "fail")
        self.assertIn("STUDIO", c["fix"])

    def test_wrong_version_fails_exactly(self):
        with mock.patch.object(preflight, "_bundle_version", return_value=("18.6.1", "x")):
            self.assertFalse(preflight.check_resolve_version()["ok"])   # point builds refuse too
        with mock.patch.object(preflight, "_bundle_version", return_value=("7.0.2", "7.0.2")):
            self.assertFalse(preflight.check_topaz_version()["ok"])

    def test_exact_version_passes(self):
        with mock.patch.object(preflight, "_bundle_version",
                               return_value=(versions.RESOLVE_VERSION, versions.RESOLVE_BUILD)):
            self.assertTrue(preflight.check_resolve_version()["ok"])


class DisplayCheck(unittest.TestCase):
    """The display gate is the BACKING SCALE, not a geometry list: dv_shim derives every
    click from a template match, so a template lands whenever the UI renders at the same
    pixel size (proven — the 16-inch panel's templates matched the 3840x2160 dummy at
    >=0.96 with no recalibration). Verified geometries just get a nicer name."""

    def test_verified_builtin_passes_by_name(self):
        w, h = versions.DISPLAY_PIXELS
        with mock.patch.object(preflight, "_display_via_coregraphics",
                               return_value=(w, h, versions.RETINA_SCALE, True)):
            c = preflight.check_display(host=None)   # judge MAIN, whatever is pinned
        self.assertTrue(c["ok"]); self.assertIn("built-in", c["detail"])

    def test_clamshell_dummy_passes_by_name(self):
        # LID-CLOSED: the 4K dummy plug is the main display and is NOT builtin
        with mock.patch.object(preflight, "_display_via_coregraphics",
                               return_value=(3840, 2160, 2.0, False)):
            c = preflight.check_display(host=None)   # judge MAIN, whatever is pinned
        self.assertTrue(c["ok"]); self.assertIn("clamshell", c["detail"])

    def test_other_2x_displays_pass_generically(self):
        # a 14-inch MBP panel, and a 5K external — neither smoke-tested, both valid 2x
        for geom, builtin in (((3024, 1964), True), ((5120, 2880), False), ((2560, 1600), False)):
            with mock.patch.object(preflight, "_display_via_coregraphics",
                                   return_value=(geom[0], geom[1], 2.0, builtin)):
                c = preflight.check_display(host=None)   # judge MAIN, whatever is pinned
            self.assertTrue(c["ok"], geom)
            self.assertIn("@2x", c["detail"])
            self.assertIn("--smoke", c["detail"])      # says it is unverified, run the smoke test

    def test_non_2x_scale_still_fails(self):
        # 1x renders the UI at half the templates' pixel size -> nothing would match
        for scale in (1.0, 1.5, 3.0):
            with mock.patch.object(preflight, "_display_via_coregraphics",
                                   return_value=(3840, 2160, scale, False)):
                self.assertFalse(preflight.check_display(host=None)["ok"], scale)

    def test_absurdly_small_2x_display_fails(self):
        # 1920x1080 BACKING at 2x is 960x540 points — Resolve's Color page cannot lay out
        with mock.patch.object(preflight, "_display_via_coregraphics",
                               return_value=(1920, 1080, 2.0, False)):
            self.assertFalse(preflight.check_display(host=None)["ok"])

    def test_match_display_is_pure(self):
        self.assertIsNotNone(preflight.match_display(3456, 2234, 2.0, True))
        self.assertIsNotNone(preflight.match_display(3840, 2160, 2.0, False))
        self.assertIsNotNone(preflight.match_display(3024, 1964, 2.0, True))   # unverified but 2x
        self.assertIsNone(preflight.match_display(3840, 2160, 1.0, False))     # 1x
        self.assertIsNone(preflight.match_display(1920, 1080, 2.0, False))     # too small at 2x

    def test_unreadable_scale_accepts_only_a_verified_geometry(self):
        # system_profiler can't report the scale — the invariant is unverifiable, so only a
        # known-good geometry is accepted (an unknown one must NOT be waved through).
        w, h = versions.DISPLAY_PIXELS
        with mock.patch.object(preflight, "_display_via_coregraphics",
                               side_effect=RuntimeError("no CG")), \
             mock.patch.object(preflight, "_display_via_system_profiler",
                               return_value=(w, h, None, True)):
            c = preflight.check_display(host=None)   # judge MAIN, whatever is pinned
        self.assertTrue(c["ok"]); self.assertIn("system_profiler", c["detail"])
        with mock.patch.object(preflight, "_display_via_coregraphics",
                               side_effect=RuntimeError("no CG")), \
             mock.patch.object(preflight, "_display_via_system_profiler",
                               return_value=(5120, 2880, None, False)):
            self.assertFalse(preflight.check_display(host=None)["ok"])


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
             mock.patch.object(preflight, "check_shim_templates", return_value=fails[0]), \
             mock.patch.object(preflight, "check_tcc_grants", return_value=warns[0]), \
             mock.patch.object(preflight, "check_resolve_artifacts", return_value=warns[0]), \
             mock.patch.object(preflight, "check_config", return_value=warns[0]), \
             mock.patch.object(preflight, "check_native_dv", return_value=fails[0]), \
             mock.patch.object(preflight, "_dv_engine", return_value="native"):
            r = preflight.run_checks()
        self.assertTrue(r["hard_ok"])            # warn failures don't gate arming
        self.assertFalse(r["ok"])                # but strict ok reflects them

    def test_post_setup_promotes_artifacts_to_fail(self):
        c = preflight.check_resolve_artifacts(post_setup=True)
        self.assertEqual(c["severity"], "fail")
        c = preflight.check_resolve_artifacts(post_setup=False)
        self.assertEqual(c["severity"], "warn")

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
        if preflight._bundle_version(versions.RESOLVE_APP)[0] != versions.RESOLVE_VERSION:
            self.skipTest("pinned Resolve not installed — not the reference machine")
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


class ResolveProcessMatch(unittest.TestCase):
    """`pgrep -x "DaVinci Resolve"` never matches: that is the APP name, while the
    executable is "Resolve". Every guard built on it silently reported "not running" —
    --smoke always skipped, and setup/import_resolve's never-merge-while-Resolve-is-open
    rule never fired even though Resolve rewrites the preset file on exit."""

    def test_the_pattern_matches_the_executable_not_the_app_name(self):
        pat = preflight.RESOLVE_PGREP
        self.assertEqual(pat[1], "-f", "-x matches the executable name, which is 'Resolve'")
        self.assertTrue(pat[2].endswith("/Resolve"),
                        "must match the real executable at the end of the bundle path")
        self.assertIn("DaVinci Resolve.app", pat[2],
                      "and stay anchored to the bundle so it can't match an unrelated 'Resolve'")

    def test_no_caller_still_uses_the_broken_form(self):
        import glob, os
        root = os.path.dirname(os.path.dirname(os.path.abspath(preflight.__file__)))
        bad = []
        for f in glob.glob(os.path.join(root, "**", "*.py"), recursive=True):
            # Source tree only — Visionary.app is build output and carries a copy of the
            # engine that is stale until the next build.
            if os.path.basename(f).startswith("test_") or ".app/" in f:
                continue
            with open(f) as fh:
                body = fh.read()
            if '"-x", "DaVinci Resolve"' in body:
                bad.append(os.path.relpath(f, root))
        self.assertEqual(bad, [], "these still use a pgrep pattern that can never match")


class FixStringsAlwaysPresent(unittest.TestCase):
    def test_passing_check_keeps_its_fix(self):
        # the in-app Setup rows show each check's remediation/command regardless of state
        c = preflight._check("x", True, "fail", "fine", "the fix text")
        self.assertEqual(c["fix"], "the fix text")

    def test_failing_check_keeps_it_too(self):
        c = preflight._check("x", False, "fail", "broken", "the fix text")
        self.assertEqual(c["fix"], "the fix text")


class ArmGate(unittest.TestCase):
    def test_arm_gate_is_cheap_plus_instant_deps(self):
        # dv_engine 'resolve' (the default): EXACTLY the list it always was — and the
        # native toolchain is not even looked at.
        from unittest import mock
        import settings
        fake = lambda cid: {"id": cid, "ok": True, "severity": "fail", "detail": "", "fix": ""}
        with mock.patch.object(preflight, "run_cheap",
                               return_value=[fake("resolve_version"), fake("topaz_version"),
                                             fake("display")]), \
             mock.patch.object(preflight, "check_brew_tools",
                               return_value=fake("brew_tools")), \
             mock.patch.object(preflight, "check_python_deps",
                               return_value=fake("python_deps")), \
             mock.patch.object(settings, "dv_engine", return_value="resolve"), \
             mock.patch.object(preflight, "check_native_dv") as ndv:
            ids = [c["id"] for c in preflight.run_arm_gate()]
        self.assertEqual(ids, ["resolve_version", "topaz_version", "display",
                               "brew_tools", "python_deps"])
        ndv.assert_not_called()


class ArtifactsAcceptNewProjectNames(unittest.TestCase):
    def test_clean_import_names_pass_the_check(self):
        # regression: preflight only knew legacy names, so a clean import of
        # "Visionary DV1000 Output" failed the artifacts check
        import os, tempfile
        from unittest import mock
        d = tempfile.mkdtemp()
        for name in ("Visionary DV1000 Output", "Visionary DV2000 Output"):
            os.makedirs(os.path.join(d, name))
        presets = os.path.join(d, "DeliverPresetList.xml")
        open(presets, "w").write("<x><DbKey>OvernightDV</DbKey></x>")
        with mock.patch.object(preflight, "RESOLVE_PROJECT_DIR", d), \
             mock.patch.object(preflight, "DELIVER_PRESETS", presets):
            c = preflight.check_resolve_artifacts()
        self.assertTrue(c["ok"], c["detail"])


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
            return self.answers[os.path.basename(argv[0])]
        p = mock.patch.object(preflight, "_probe", side_effect=probe)
        p.start(); self.addCleanup(p.stop)
        preflight._SHA_CACHE.clear()
        self.addCleanup(preflight._SHA_CACHE.clear)

    def ran(self, name):
        return [a for a in self.probed if os.path.basename(a[0]) == name]


class NativeDvCheck(_NativeToolchain, unittest.TestCase):
    """check_native_dv: analyzer exists + sha256 + EXECUTES, LUT sha256, dovi_tool's exact
    version, ffmpeg's major. One row; every failing sub-check is named in it."""

    def test_a_pinned_working_toolchain_passes(self):
        c = preflight.check_native_dv()
        self.assertTrue(c["ok"], c["detail"])
        self.assertEqual((c["id"], c["severity"]), ("native_dv", "fail"))
        self.assertIn("383b0ad", c["detail"])
        self.assertTrue(c["fix"])                        # fix is kept even on a pass
        self.assertEqual(self.ran("hdr_analyzer_mvp"), [[self.an, "--help"]])

    def test_a_missing_analyzer_fails_and_is_not_run(self):
        os.remove(self.an)
        c = preflight.check_native_dv()
        self.assertFalse(c["ok"])
        self.assertIn("hdr_analyzer_mvp missing", c["detail"])
        self.assertIn("383b0ad", c["fix"])               # says which commit to build
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
        self.assertIn("brew upgrade ffmpeg", c["fix"])

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

    def test_not_required_is_a_warn_that_says_items_fall_back(self):
        os.remove(self.lut)
        c = preflight.check_native_dv(required=False)
        self.assertEqual(c["severity"], "warn")
        self.assertFalse(c["ok"])                        # it still reports the real state
        self.assertTrue(c["detail"].startswith(
            "native items fall back to Resolve until this is fixed: "), c["detail"])
        self.assertIn("LUT missing", c["detail"])
        self.assertIn("Restore the pinned LUT", c["fix"])

    def test_a_passing_warn_row_reads_as_a_plain_pass(self):
        c = preflight.check_native_dv(required=False)
        self.assertTrue(c["ok"], c["detail"])
        self.assertEqual(c["severity"], "warn")
        self.assertNotIn("fall back", c["detail"])

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


class NativeDvGating(unittest.TestCase):
    """dv_engine 'resolve' (the default): the native row does not exist — run_checks, --json,
    the text output and the arm gate are exactly the old ones, and nothing native is probed.
    'native': a "warn" row that never gates hard_ok, and NEVER an arm-gate row (a broken
    toolchain sends items through Resolve; only the switch to 'native' is refused, by
    POST /api/settings)."""

    ROWS = ("resolve_version", "power_adapter", "brew_tools", "python_deps", "shim_templates",
            "tcc_grants", "resolve_artifacts", "config")

    def _row(self, cid, ok=True, sev="fail"):
        return {"id": cid, "ok": ok, "severity": sev, "detail": "", "fix": ""}

    def _patched(self, es, engine, native_ok=True, calls=None):
        import settings

        def ndv(required=True):
            if calls is not None:
                calls.append(required)
            return self._row("native_dv", native_ok, "fail" if required else "warn")
        es.enter_context(mock.patch.object(settings, "dv_engine", return_value=engine))
        es.enter_context(mock.patch.object(preflight, "run_cheap",
                                           return_value=[self._row("resolve_version")]))
        for name in ("check_power_adapter", "check_brew_tools", "check_python_deps",
                     "check_shim_templates", "check_tcc_grants",
                     "check_resolve_artifacts", "check_config"):
            es.enter_context(mock.patch.object(preflight, name,
                                               return_value=self._row(name[6:])))
        es.enter_context(mock.patch.object(preflight, "check_shim_smoke",
                                           return_value=self._row("shim_smoke", sev="warn")))
        es.enter_context(mock.patch.object(preflight, "check_native_dv", side_effect=ndv))

    def _run_checks(self, engine, native_ok=True, smoke=False):
        import contextlib
        calls = []
        with contextlib.ExitStack() as es:
            self._patched(es, engine, native_ok, calls)
            return preflight.run_checks(smoke=smoke), calls

    def test_resolve_mode_has_no_native_row_and_probes_nothing(self):
        import contextlib
        trap = mock.Mock(side_effect=AssertionError("the native toolchain was looked at"))
        with contextlib.ExitStack() as es:
            self._patched(es, "resolve", native_ok=False)
            for name in ("check_native_dv", "_native_dv_problems", "_probe", "_sha256_cached"):
                es.enter_context(mock.patch.object(preflight, name, trap))
            r = preflight.run_checks()
            rs = preflight.run_checks(smoke=True)
        self.assertEqual([c["id"] for c in r["checks"]], list(self.ROWS))
        self.assertEqual([c["id"] for c in rs["checks"]], list(self.ROWS) + ["shim_smoke"])
        # ...and the verdicts are computed over exactly those rows, the way they always were
        self.assertEqual(r, {"ok": True, "hard_ok": True, "checks": r["checks"]})
        trap.assert_not_called()

    def test_resolve_mode_cli_output_has_no_native_row(self):
        import contextlib, io, json
        for argv in (["--json"], []):
            with self.subTest(argv=argv), contextlib.ExitStack() as es:
                self._patched(es, "resolve", native_ok=False)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = preflight.main(argv)
                self.assertEqual(code, 0)                 # all pass, as before the lane
                self.assertNotIn("native", out.getvalue())
        with contextlib.ExitStack() as es:
            self._patched(es, "resolve")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                preflight.main(["--json"])
        self.assertEqual([c["id"] for c in json.loads(out.getvalue())["checks"]], list(self.ROWS))

    def test_native_mode_lists_a_warn_row_that_never_gates_hard_ok(self):
        r, calls = self._run_checks("native", native_ok=False)
        self.assertEqual(calls, [False])                  # asked as the warn row
        self.assertEqual([c["id"] for c in r["checks"]], list(self.ROWS) + ["native_dv"])
        self.assertEqual(r["checks"][-1]["severity"], "warn")
        self.assertTrue(r["hard_ok"])                     # never refuses to arm
        self.assertTrue(r["ok"])                          # nor raises "Finish setup": Setup has
                                                          # no row that could fix it (API-only)
        r, _ = self._run_checks("native", native_ok=True)
        self.assertTrue(r["hard_ok"])
        self.assertTrue(r["ok"])
        r, _ = self._run_checks("native", smoke=True)
        self.assertEqual([c["id"] for c in r["checks"]][-2:], ["native_dv", "shim_smoke"])

    def test_native_mode_cli_exit_is_warnings_only(self):
        import contextlib, io
        with contextlib.ExitStack() as es:
            self._patched(es, "native", native_ok=False)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = preflight.main([])
        self.assertEqual(code, 0)                         # ok stays true (see above)...
        self.assertIn("[WARN] native_dv", out.getvalue())   # ...but the row is still reported

    def _arm_gate(self, engine, native_ok=True):
        import settings
        with mock.patch.object(preflight, "run_cheap", return_value=[self._row("resolve_version"),
                                                                     self._row("topaz_version"),
                                                                     self._row("display")]), \
             mock.patch.object(preflight, "check_brew_tools", return_value=self._row("brew_tools")), \
             mock.patch.object(preflight, "check_python_deps",
                               return_value=self._row("python_deps")), \
             mock.patch.object(settings, "dv_engine", return_value=engine), \
             mock.patch.object(preflight, "check_native_dv",
                               return_value=self._row("native_dv", native_ok)) as ndv:
            return preflight.run_arm_gate(), ndv

    def test_the_arm_gate_never_holds_the_native_toolchain(self):
        for engine in ("resolve", "native"):
            with self.subTest(engine=engine):
                gate, ndv = self._arm_gate(engine, native_ok=False)
                self.assertEqual([c["id"] for c in gate],
                                 ["resolve_version", "topaz_version", "display", "brew_tools",
                                  "python_deps"])
                ndv.assert_not_called()
                self.assertTrue(all(c["ok"] for c in gate))   # a broken toolchain still arms

    def test_the_switch_to_native_still_gets_a_hard_row(self):
        # POST /api/settings {"dv_engine": "native"} calls check_native_dv() and refuses (409)
        # on a failing row — that row is severity "fail", without the fall-back wording.
        with mock.patch.object(preflight, "_native_dv_problems",
                               return_value=[("dovi_tool is 2.2.0", "Install dovi_tool 2.3.0")]):
            c = preflight.check_native_dv()
        self.assertEqual((c["ok"], c["severity"]), (False, "fail"))
        self.assertEqual(c["detail"], "dovi_tool is 2.2.0")

    def test_an_unreadable_setting_fails_safe_to_resolve(self):
        import settings
        with mock.patch.object(settings, "dv_engine", side_effect=TypeError("bad file")):
            self.assertEqual(preflight._dv_engine(), "resolve")
        with mock.patch.object(settings, "get_settings", return_value={"dv_engine": "Native"}):
            self.assertEqual(preflight._dv_engine(), "resolve")
        with mock.patch.object(settings, "get_settings", return_value={"dv_engine": "native"}):
            self.assertEqual(preflight._dv_engine(), "native")


class NativeDvLiveOnReferenceMachine(unittest.TestCase):
    """On the reference machine the real native toolchain must match its pins — skipped
    anywhere the pinned analyzer isn't installed. Read-only: hashes two files and runs
    `--help` / `--version` / `-version`."""

    def test_reference_toolchain_matches_the_pins(self):
        if not os.path.isfile(versions.HDR_ANALYZER_PATH):
            self.skipTest("hdr_analyzer_mvp not installed — not the reference machine")
        c = preflight.check_native_dv()
        self.assertTrue(c["ok"], c["detail"])
