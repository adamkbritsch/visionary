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

    def test_shim_region_fits_the_pinned_display(self):
        import dv_shim
        x0, y0, x1, y1 = dv_shim.ANALYSIS_REGION
        w, h = versions.DISPLAY_PIXELS
        self.assertTrue(0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h,
                        f"ANALYSIS_REGION {dv_shim.ANALYSIS_REGION} outside {w}x{h}")

    def test_pin_values(self):
        # the exact builds this repo ships templates/params for — bump ONLY with new templates
        self.assertEqual(versions.RESOLVE_VERSION, "18.6.0")
        self.assertEqual(versions.TOPAZ_VERSION, "7.0.1")
        self.assertEqual(versions.DISPLAY_PIXELS, (3456, 2234))


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
    def test_wrong_geometry_fails(self):
        with mock.patch.object(preflight, "_display_via_coregraphics",
                               return_value=(3024, 1964, 2.0, True)):    # a 14" MBP
            c = preflight.check_display()
        self.assertFalse(c["ok"]); self.assertEqual(c["severity"], "fail")

    def test_external_main_display_fails_even_at_native_res(self):
        w, h = versions.DISPLAY_PIXELS
        with mock.patch.object(preflight, "_display_via_coregraphics",
                               return_value=(w, h, 2.0, False)):         # builtin=False
            self.assertFalse(preflight.check_display()["ok"])

    def test_pinned_display_passes(self):
        w, h = versions.DISPLAY_PIXELS
        with mock.patch.object(preflight, "_display_via_coregraphics",
                               return_value=(w, h, versions.RETINA_SCALE, True)):
            self.assertTrue(preflight.check_display()["ok"])

    def test_coregraphics_failure_falls_back_to_system_profiler(self):
        w, h = versions.DISPLAY_PIXELS
        with mock.patch.object(preflight, "_display_via_coregraphics",
                               side_effect=RuntimeError("no CG")), \
             mock.patch.object(preflight, "_display_via_system_profiler",
                               return_value=(w, h, None, True)):
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
