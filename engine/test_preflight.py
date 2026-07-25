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
    """The display gate is an ALLOW-LIST of verified configs (versions.SUPPORTED_DISPLAYS):
    the built-in panel, and the 4K dummy HDMI plug used for LID-CLOSED clamshell runs.
    Anything else still hard-fails — an unverified display breaks template matching."""

    def test_wrong_geometry_fails(self):
        with mock.patch.object(preflight, "_display_via_coregraphics",
                               return_value=(3024, 1964, 2.0, True)):    # a 14" MBP
            c = preflight.check_display()
        self.assertFalse(c["ok"]); self.assertEqual(c["severity"], "fail")

    def test_unverified_external_display_still_fails(self):
        with mock.patch.object(preflight, "_display_via_coregraphics",
                               return_value=(2560, 1440, 2.0, False)):   # some random monitor
            self.assertFalse(preflight.check_display()["ok"])

    def test_builtin_geometry_on_an_external_display_fails(self):
        # the builtin flag is part of the identity — a panel merely reporting the
        # built-in's pixels is not the verified config
        w, h = versions.DISPLAY_PIXELS
        with mock.patch.object(preflight, "_display_via_coregraphics",
                               return_value=(w, h, 2.0, False)):
            self.assertFalse(preflight.check_display()["ok"])

    def test_wrong_scale_fails(self):
        # 1x (or any non-2x) mode renders the UI at a different pixel size -> no match
        with mock.patch.object(preflight, "_display_via_coregraphics",
                               return_value=(3840, 2160, 1.0, False)):
            self.assertFalse(preflight.check_display()["ok"])

    def test_pinned_display_passes(self):
        w, h = versions.DISPLAY_PIXELS
        with mock.patch.object(preflight, "_display_via_coregraphics",
                               return_value=(w, h, versions.RETINA_SCALE, True)):
            c = preflight.check_display()
        self.assertTrue(c["ok"]); self.assertIn("built-in", c["detail"])

    def test_clamshell_dummy_passes(self):
        # LID-CLOSED: the 4K dummy plug is the main display and is NOT builtin
        with mock.patch.object(preflight, "_display_via_coregraphics",
                               return_value=(3840, 2160, 2.0, False)):
            c = preflight.check_display()
        self.assertTrue(c["ok"]); self.assertIn("clamshell", c["detail"])

    def test_match_display_is_pure(self):
        self.assertIsNotNone(preflight.match_display(3456, 2234, 2.0, True))
        self.assertIsNotNone(preflight.match_display(3840, 2160, 2.0, False))
        self.assertIsNone(preflight.match_display(3840, 2160, 2.0, True))    # dummy can't be builtin
        self.assertIsNone(preflight.match_display(1920, 1080, 2.0, False))   # logical, not backing

    def test_coregraphics_failure_falls_back_to_system_profiler(self):
        w, h = versions.DISPLAY_PIXELS
        with mock.patch.object(preflight, "_display_via_coregraphics",
                               side_effect=RuntimeError("no CG")), \
             mock.patch.object(preflight, "_display_via_system_profiler",
                               return_value=(w, h, None, True)):           # scale unknown
            c = preflight.check_display()
        self.assertTrue(c["ok"]); self.assertIn("system_profiler", c["detail"])


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
