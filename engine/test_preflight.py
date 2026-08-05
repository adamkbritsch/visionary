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
             mock.patch.object(power, "adapter_watts", return_value=watts):
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
             mock.patch.object(preflight, "check_sublercli", return_value=warns[0]), \
             mock.patch.object(preflight, "check_python_deps", return_value=fails[0]), \
             mock.patch.object(preflight, "check_shim_templates", return_value=fails[0]), \
             mock.patch.object(preflight, "check_tcc_grants", return_value=warns[0]), \
             mock.patch.object(preflight, "check_resolve_artifacts", return_value=warns[0]), \
             mock.patch.object(preflight, "check_config", return_value=warns[0]):
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
