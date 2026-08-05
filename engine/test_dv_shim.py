import os
import tempfile
import unittest
from unittest import mock

import dv_shim

cv2 = dv_shim.cv2


@unittest.skipIf(cv2 is None, "cv2 not installed")
class FindButton(unittest.TestCase):
    """The locator is what makes the shim robust to window size/position, so it
    is the part worth testing deterministically (no Resolve/permissions needed)."""

    def _fixture(self, d):
        import numpy as np
        canvas = np.full((300, 400, 3), 20, np.uint8)
        btn = np.full((20, 48, 3), (60, 170, 90), np.uint8)
        cv2.putText(btn, "All", (6, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        canvas[150:170, 100:148] = btn          # button placed at known location
        sp = os.path.join(d, "shot.png"); tp = os.path.join(d, "tmpl.png")
        cv2.imwrite(sp, canvas); cv2.imwrite(tp, btn)
        return sp, tp

    def test_locates_center_in_logical_points(self):
        with tempfile.TemporaryDirectory() as d:
            sp, tp = self._fixture(d)
            xy = dv_shim.find_button(sp, tp, threshold=0.9, scale=1.0)
            self.assertIsNotNone(xy)
            self.assertAlmostEqual(xy[0], 124, delta=2)   # 100 + 48/2
            self.assertAlmostEqual(xy[1], 160, delta=2)   # 150 + 20/2

    def test_halves_coordinates_for_retina(self):
        with tempfile.TemporaryDirectory() as d:
            sp, tp = self._fixture(d)
            xy = dv_shim.find_button(sp, tp, threshold=0.9, scale=2.0)
            self.assertAlmostEqual(xy[0], 62, delta=1)    # 124 / 2 (Retina)

    def test_none_when_button_absent(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as d:
            sp, _ = self._fixture(d)
            other = os.path.join(d, "other.png")
            # textured pattern that genuinely does not appear in the canvas
            rng = np.random.default_rng(0)
            cv2.imwrite(other, rng.integers(0, 256, (20, 48, 3), dtype=np.uint8))
            self.assertIsNone(dv_shim.find_button(sp, other, threshold=0.9, scale=1.0))


class DisplayGeometry(unittest.TestCase):
    """retina_scale() is READ from the main display (every supported config is 2.0), so a
    second supported display (the clamshell dummy) needs no recalibration."""

    def test_scale_derived_from_geometry(self):
        with mock.patch.object(dv_shim, "main_display_geometry", return_value=(3840, 2160, 2.0, False)):
            self.assertEqual(dv_shim.retina_scale(), 2.0)
        with mock.patch.object(dv_shim, "main_display_geometry", return_value=(3456, 2234, 2.0, True)):
            self.assertEqual(dv_shim.retina_scale(), 2.0)

    def test_scale_falls_back_when_unreadable(self):
        with mock.patch.object(dv_shim, "main_display_geometry", return_value=None):
            self.assertEqual(dv_shim.retina_scale(), 2.0)


class FullScreen(unittest.TestCase):
    """enter_fullscreen must target the window that OWNS AXFullScreen and VERIFY it.
    'window 1' can be the Project Manager dialog, where the attribute is settable:false
    and the set is silently dropped (osascript still exits 0) — that left Resolve
    windowed, and a windowed layout is where the DV palette click does not register
    (live-caught lid-closed, 2026-07-17)."""

    def test_returns_true_when_already_fullscreen(self):
        with mock.patch.object(dv_shim, "fullscreen_state", return_value=(1, True, "Main")):
            self.assertTrue(dv_shim.enter_fullscreen(settle=0))

    def test_sets_then_verifies_and_retries(self):
        # windowed -> set -> verified fullscreen on the readback
        states = [(2, False, "Main"), (2, True, "Main")]
        with mock.patch.object(dv_shim, "fullscreen_state", side_effect=states), \
             mock.patch.object(dv_shim, "_osa", return_value=(0, "", "")) as osa, \
             mock.patch.object(dv_shim, "activate"):
            self.assertTrue(dv_shim.enter_fullscreen(settle=0))
        # it must address the window index that owns the attribute — NOT a hardcoded 1
        self.assertIn("window 2", osa.call_args[0][0])

    def test_raises_when_no_window_accepts_fullscreen(self):
        # e.g. only the Project Manager dialog is open
        with mock.patch.object(dv_shim, "fullscreen_state", return_value=(None, None, None)), \
             mock.patch.object(dv_shim, "_osa", return_value=(0, "Project Manager", "")), \
             mock.patch.object(dv_shim, "activate"), \
             mock.patch.object(dv_shim, "_diag") as dg:
            with self.assertRaises(RuntimeError) as e:
                dv_shim.enter_fullscreen(attempts=2, settle=0)
        self.assertIn("Project Manager", str(e.exception))
        dg.assert_called_once()

    def test_raises_when_set_never_takes(self):
        with mock.patch.object(dv_shim, "fullscreen_state", return_value=(1, False, "Main")), \
             mock.patch.object(dv_shim, "_osa", return_value=(0, "", "")), \
             mock.patch.object(dv_shim, "activate"), \
             mock.patch.object(dv_shim, "_diag") as dg:
            with self.assertRaises(RuntimeError):
                dv_shim.enter_fullscreen(attempts=2, settle=0)
        dg.assert_called_once()

    def test_fullscreen_state_parses_the_owning_window(self):
        with mock.patch.object(dv_shim, "_osa", return_value=(0, "3|true|Overnight Upscaler SDR", "")):
            self.assertEqual(dv_shim.fullscreen_state(), (3, True, "Overnight Upscaler SDR"))
        with mock.patch.object(dv_shim, "_osa", return_value=(0, "none", "")):
            self.assertEqual(dv_shim.fullscreen_state(), (None, None, None))


class Forensics(unittest.TestCase):
    """With the lid closed nobody can see the screen, so a failed match must leave
    evidence: the screenshot + a sidecar JSON, ring-buffered."""

    def test_diag_writes_screenshot_and_json(self):
        import json, tempfile, glob
        with tempfile.TemporaryDirectory() as d:
            shot = os.path.join(d, "shot.png")
            open(shot, "wb").write(b"notarealpng")
            with mock.patch.object(dv_shim, "DIAG_DIR", os.path.join(d, "diag")), \
                 mock.patch.object(dv_shim, "main_display_geometry", return_value=(3840, 2160, 2.0, False)), \
                 mock.patch.object(dv_shim, "screen_locked", return_value=False):
                out = dv_shim._diag("miss-analyze_all", shot, template="analyze_all.png", score=0.42)
            self.assertTrue(out.endswith(".png") and os.path.exists(out))
            js = glob.glob(os.path.join(d, "diag", "*.json"))[0]
            rec = json.load(open(js))
            self.assertEqual(rec["what"], "miss-analyze_all")
            self.assertEqual(rec["score"], 0.42)
            self.assertEqual(rec["display"], [3840, 2160, 2.0, False])

    def test_diag_ring_buffer_caps_growth(self):
        import tempfile, glob
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(dv_shim, "DIAG_DIR", d), \
                 mock.patch.object(dv_shim, "DIAG_KEEP", 3), \
                 mock.patch.object(dv_shim, "main_display_geometry", return_value=None), \
                 mock.patch.object(dv_shim, "screen_locked", return_value=None):
                for i in range(6):
                    dv_shim._diag(f"miss-{i}")
            self.assertLessEqual(len(glob.glob(os.path.join(d, "*.json"))), 3)

    def test_failed_match_leaves_evidence(self):
        import tempfile
        import numpy as np
        with tempfile.TemporaryDirectory() as d:
            sp = os.path.join(d, "s.png"); tp = os.path.join(d, "t.png")
            rng = np.random.default_rng(1)
            cv2.imwrite(sp, rng.integers(0, 256, (80, 120, 3), dtype=np.uint8))
            cv2.imwrite(tp, rng.integers(0, 256, (20, 40, 3), dtype=np.uint8))
            with mock.patch.object(dv_shim, "_diag") as dg:
                self.assertIsNone(dv_shim.find_button(sp, tp, threshold=0.99, scale=1.0))
            dg.assert_called_once()
            self.assertIn("score", dg.call_args.kwargs)


class RealTemplates(unittest.TestCase):
    """The captured templates must exist and be loadable — the shim is useless
    without them, and a missing/corrupt PNG should fail loudly in CI, not at 2am."""

    @unittest.skipIf(cv2 is None, "cv2 not installed")
    def test_three_templates_present_and_valid(self):
        for name in ("dolby_vision_palette.png", "analyze_all.png", "target_1000nit.png",
                     "analyze_modal.png"):
            p = os.path.join(dv_shim.TEMPLATES, name)
            self.assertTrue(os.path.exists(p), f"missing template {name}")
            self.assertIsNotNone(cv2.imread(p), f"unreadable template {name}")


class InheritNotSet(unittest.TestCase):
    """The shim VERIFIES the inherited target display; it must never SET color,
    DV profile, or target display (the user configures the project once)."""

    def test_no_setters_exist(self):
        for forbidden in ("set_target_display", "set_dv_profile", "apply_color_management"):
            self.assertFalse(hasattr(dv_shim, forbidden), f"{forbidden} must not exist")
        self.assertTrue(hasattr(dv_shim, "verify_target_display"))
        self.assertTrue(hasattr(dv_shim, "run_dv_ui"))


if __name__ == "__main__":
    unittest.main()


class WaitForAnalysisFocus(unittest.TestCase):
    """The loop used to raise Resolve on every poll — up to 360 focus steals an episode.
    It now only does so when a capture cannot see Resolve at all. The safety property that
    makes that sound: a poll with no eyes on Resolve must advance NOTHING, or switching
    Spaces reads as "analysis finished" and ships an unanalysed master."""

    MODAL = "analyze_modal.png"

    def _run(self, frames, **kw):
        """`frames` is a list of what each successive screenshot 'contains':
        'modal' | 'resolve' | 'nothing'. Returns (result, activate_count, polls_used)."""
        seq = list(frames)
        calls = {"activate": 0, "shots": 0}

        def fake_screenshot(*a, **k):
            calls["shots"] += 1
            return seq[min(calls["shots"] - 1, len(seq) - 1)]

        def fake_found(shot, template, **k):
            name = os.path.basename(template)
            if name == self.MODAL:
                return shot == "modal"
            return shot in ("modal", "resolve")      # the on-screen witnesses

        def fake_activate():
            calls["activate"] += 1
            # Raising it works: every capture after this shows Resolve.
            for i in range(calls["shots"], len(seq)):
                if seq[i] == "nothing":
                    seq[i] = "resolve"

        with mock.patch.object(dv_shim, "screenshot", fake_screenshot), \
             mock.patch.object(dv_shim, "found", fake_found), \
             mock.patch.object(dv_shim, "activate", fake_activate), \
             mock.patch.object(dv_shim.time, "sleep", lambda *_a: None):
            res = dv_shim.wait_for_analysis(poll=0, **kw)
        return res, calls["activate"], calls["shots"]

    def test_a_visible_resolve_is_never_raised(self):
        # modal appears, runs, then closes with Resolve still on screen -> done, no focus taken
        res, activates, _ = self._run(["modal", "modal", "resolve", "resolve"])
        self.assertTrue(res)
        self.assertEqual(activates, 0, "focus was taken while Resolve was plainly visible")

    def test_focus_is_taken_only_when_resolve_is_not_in_frame(self):
        res, activates, _ = self._run(["modal", "nothing", "resolve", "resolve"])
        self.assertTrue(res)
        self.assertEqual(activates, 1)

    def test_a_blind_poll_never_counts_as_completion(self):
        """THE regression this exists to prevent. The modal is out of frame only because
        Resolve is not being displayed. The OLD loop would count two such polls as
        'modal closed -> analysis complete' and return True, rendering an unanalysed
        master. The new one must keep waiting."""
        calls = {"activate": 0, "shots": 0}

        def fake_screenshot(*a, **k):
            calls["shots"] += 1
            return "modal" if calls["shots"] == 1 else "nothing"

        def fake_found(shot, template, **k):
            if os.path.basename(template) == self.MODAL:
                return shot == "modal"
            return shot in ("modal", "resolve")

        # activate() cannot help — Resolve stays out of frame no matter what.
        with mock.patch.object(dv_shim, "screenshot", fake_screenshot), \
             mock.patch.object(dv_shim, "found", fake_found), \
             mock.patch.object(dv_shim, "activate",
                               lambda: calls.__setitem__("activate", calls["activate"] + 1)), \
             mock.patch.object(dv_shim.time, "sleep", lambda *_a: None), \
             mock.patch.object(dv_shim.time, "time",
                               mock.Mock(side_effect=[0] + [i * 10 for i in range(1, 60)])):
            res = dv_shim.wait_for_analysis(poll=0, appear_timeout=1e9, max_seconds=300)

        # It ran the clock out instead of returning early off the blind polls: the old code
        # would have returned after the 3rd screenshot.
        self.assertGreater(calls["shots"], 8,
                           "it returned early — blind polls were counted as completion")
        # saw_modal was true, so the TIMEOUT (not the blind polls) accepts the analysis.
        self.assertTrue(res)

    def test_it_falls_back_to_the_old_behaviour_when_resolve_stays_invisible(self):
        # Worst case must be "what it did before", not a new hang: after a few blind polls
        # it reverts to raising Resolve every single poll.
        calls = {"activate": 0, "shots": 0}

        def fake_found(shot, template, **k):
            return False                       # nothing is ever visible

        with mock.patch.object(dv_shim, "screenshot", lambda *a, **k: "nothing"), \
             mock.patch.object(dv_shim, "found", fake_found), \
             mock.patch.object(dv_shim, "activate", lambda: calls.__setitem__("activate", calls["activate"] + 1)), \
             mock.patch.object(dv_shim.time, "sleep", lambda *_a: None), \
             mock.patch.object(dv_shim.time, "time", mock.Mock(side_effect=[0] + [i * 10 for i in range(1, 40)])):
            dv_shim.wait_for_analysis(poll=0, appear_timeout=1e9, max_seconds=200)
        self.assertGreaterEqual(calls["activate"], 3,
                                "it must revert to raising Resolve when it stays invisible")

    def test_abort_still_wins_before_anything_is_touched(self):
        ab = mock.Mock()
        ab.is_set.return_value = True
        with mock.patch.object(dv_shim, "screenshot",
                               mock.Mock(side_effect=AssertionError("must not capture"))), \
             mock.patch.object(dv_shim, "activate",
                               mock.Mock(side_effect=AssertionError("must not steal focus"))):
            self.assertFalse(dv_shim.wait_for_analysis(abort=ab, poll=0))


class HostTargeting(unittest.TestCase):
    """Unpinned behaviour must be BYTE-identical to before the host existed — that is the
    property that lets this land while the feature is off."""

    HDMI = {"key": "uuid:HDMI", "origin": (1728.0, 1117.0), "size_pt": (1920, 1080),
            "scale": 2.0}

    def tearDown(self):
        dv_shim.set_host(None)

    def test_unpinned_capture_passes_no_R_flag(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return mock.Mock(returncode=0, stderr="", stdout="")

        with mock.patch.object(dv_shim.subprocess, "run", fake_run), \
             mock.patch.object(dv_shim.os.path, "exists", return_value=True), \
             mock.patch.object(dv_shim.os.path, "getsize", return_value=99):
            dv_shim.screenshot("/tmp/x.png")
        self.assertEqual(seen["cmd"], ["screencapture", "-x", "/tmp/x.png"])

    def test_pinned_capture_selects_the_host_rect_in_global_points(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return mock.Mock(returncode=0, stderr="", stdout="")

        dv_shim.set_host(self.HDMI)
        with mock.patch.object(dv_shim, "host_view",
                               return_value=(1728.0, 1117.0, 2.0, 1920.0, 1080.0)), \
             mock.patch.object(dv_shim.subprocess, "run", fake_run), \
             mock.patch.object(dv_shim.os.path, "exists", return_value=True), \
             mock.patch.object(dv_shim.os.path, "getsize", return_value=99):
            dv_shim.screenshot("/tmp/x.png")
        self.assertIn("-R", seen["cmd"])
        self.assertEqual(seen["cmd"][seen["cmd"].index("-R") + 1], "1728,1117,1920,1080")

    @unittest.skipIf(dv_shim.cv2 is None, "cv2 not installed")
    def test_the_origin_shifts_matches_by_exactly_the_host_origin(self):
        import numpy as np, cv2, tempfile, os as _os
        d = tempfile.mkdtemp()
        shot = _os.path.join(d, "s.png"); tmpl = _os.path.join(d, "t.png")
        # The patch must have VARIANCE — a solid block has none and TM_CCOEFF_NORMED is
        # undefined on it, which silently matches at (0, 0).
        rng = np.random.default_rng(7)
        img = rng.integers(0, 60, (400, 600, 3), dtype=np.uint8)
        img[100:140, 200:260] = rng.integers(0, 255, (40, 60, 3), dtype=np.uint8)
        cv2.imwrite(shot, img)
        cv2.imwrite(tmpl, img[100:140, 200:260])
        at_main, _ = dv_shim.match_template(shot, tmpl, scale=2.0, origin=(0.0, 0.0))
        at_hdmi, _ = dv_shim.match_template(shot, tmpl, scale=2.0, origin=(1728.0, 1117.0))
        self.assertAlmostEqual(at_hdmi[0] - at_main[0], 1728.0, places=6)
        self.assertAlmostEqual(at_hdmi[1] - at_main[1], 1117.0, places=6)
        # and the unpinned answer is the pre-host formula, unchanged
        self.assertAlmostEqual(at_main[0], (200 + 60 / 2) / 2.0, places=6)

    def test_click_refuses_a_point_outside_the_driven_display(self):
        # An origin or scale bug otherwise clicks REAL coordinates on the user's screen.
        with mock.patch.object(dv_shim, "host_view",
                               return_value=(1728.0, 1117.0, 2.0, 1920.0, 1080.0)), \
             mock.patch.object(dv_shim, "_diag", lambda *a, **k: ""), \
             mock.patch.object(dv_shim.subprocess, "run",
                               mock.Mock(side_effect=AssertionError("must not click"))):
            with self.assertRaises(RuntimeError) as cm:
                dv_shim.click(300, 300)          # a main-display point while pinned to HDMI
        self.assertIn("outside the display being driven", str(cm.exception))

    def test_click_allows_a_point_on_the_driven_display_and_restores_the_pointer(self):
        seen = {}
        with mock.patch.object(dv_shim, "host_view",
                               return_value=(1728.0, 1117.0, 2.0, 1920.0, 1080.0)), \
             mock.patch.object(dv_shim.subprocess, "run",
                               lambda cmd, **kw: seen.update(cmd=cmd)):
            dv_shim.click(2000, 1500)
        self.assertIn("-r", seen["cmd"])         # pointer goes back where the user left it
        self.assertEqual(seen["cmd"][-1], "c:2000,1500")

    def test_unpinned_clicks_are_still_allowed_across_the_main_display(self):
        seen = {}
        with mock.patch.object(dv_shim, "host_view",
                               return_value=(0.0, 0.0, 2.0, 1728.0, 1117.0)), \
             mock.patch.object(dv_shim.subprocess, "run",
                               lambda cmd, **kw: seen.update(cmd=cmd)):
            dv_shim.click(864, 558)
        self.assertEqual(seen["cmd"][-1], "c:864,558")


class TakeoverCountdown(unittest.TestCase):
    """A COUNTDOWN, not a delay. The clock starts at the top of the resolve stage, while
    Resolve is still launching and the timeline is still being assembled — so in the
    normal case it has already run out by the time anything is clicked, and the episode
    is not one second longer."""

    def setUp(self):
        dv_shim._TAKEOVER_DEADLINE = None
        try:
            os.remove(dv_shim.TAKEOVER_ACK)
        except OSError:
            pass

    tearDown = setUp

    def test_arming_publishes_an_absolute_deadline(self):
        printed = []
        with mock.patch("settings.tunable", return_value=20), \
             mock.patch("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a)))), \
             mock.patch.object(dv_shim.time, "time", return_value=1000.0):
            dv_shim.arm_takeover_warning()
        self.assertEqual(dv_shim._TAKEOVER_DEADLINE, 1020.0)
        self.assertIn("SCREEN_TAKEOVER_AT 1020 IN 20", printed[0])

    def test_zero_seconds_arms_nothing(self):
        with mock.patch("settings.tunable", return_value=0):
            dv_shim.arm_takeover_warning()
        self.assertIsNone(dv_shim._TAKEOVER_DEADLINE)

    def test_a_countdown_that_elapsed_during_setup_adds_NO_delay(self):
        # THE point of the change: the stage's own work outlasted the countdown, so the
        # wait at the takeover is zero.
        dv_shim._TAKEOVER_DEADLINE = 100.0
        slept = []
        with mock.patch.object(dv_shim.time, "time", return_value=500.0), \
             mock.patch.object(dv_shim.time, "sleep", lambda s: slept.append(s)):
            dv_shim._warn_before_takeover()
        self.assertEqual(slept, [], "it must not sleep for a countdown that already ran out")

    def test_only_the_REMAINDER_is_waited_out(self):
        dv_shim._TAKEOVER_DEADLINE = 1000.0
        now = [997.0]
        slept = []

        def fake_sleep(s):
            slept.append(s)
            now[0] += s

        with mock.patch.object(dv_shim.time, "time", lambda: now[0]), \
             mock.patch.object(dv_shim.time, "sleep", fake_sleep):
            dv_shim._warn_before_takeover()
        self.assertAlmostEqual(sum(slept), 3.0, places=6)   # 3s left of a longer countdown

    def test_an_ack_ends_the_countdown_early(self):
        dv_shim._TAKEOVER_DEADLINE = 9e12
        open(dv_shim.TAKEOVER_ACK, "w").close()
        with mock.patch.object(dv_shim.time, "sleep", lambda *_a: None):
            dv_shim._warn_before_takeover()
        self.assertIsNone(dv_shim._TAKEOVER_DEADLINE)

    def test_an_abort_ends_the_countdown(self):
        dv_shim._TAKEOVER_DEADLINE = 9e12
        ab = mock.Mock(); ab.is_set.return_value = True
        with mock.patch.object(dv_shim.time, "sleep", lambda *_a: None):
            dv_shim._warn_before_takeover(abort=ab)
        self.assertIsNone(dv_shim._TAKEOVER_DEADLINE)


class PointerRelease(unittest.TestCase):
    """When a takeover ends the mouse goes back to the main screen — however long it ran,
    and however it ended (success, raise, or abort)."""

    MAIN = (0.0, 0.0, 1728.0, 1117.0)

    def test_a_pointer_left_on_the_other_screen_comes_back(self):
        moved = {}
        with mock.patch.object(dv_shim, "main_display_bounds", return_value=self.MAIN), \
             mock.patch.object(dv_shim, "pointer_position", return_value=(2500.0, 1500.0)), \
             mock.patch.object(dv_shim, "warp_pointer",
                               lambda x, y: moved.update(to=(x, y)) or True):
            self.assertTrue(dv_shim.release_pointer_to_main(saved=(400.0, 300.0)))
        self.assertEqual(moved["to"], (400.0, 300.0), "prefers where the user had it")

    def test_it_falls_back_to_the_centre_of_main(self):
        moved = {}
        with mock.patch.object(dv_shim, "main_display_bounds", return_value=self.MAIN), \
             mock.patch.object(dv_shim, "pointer_position", return_value=(2500.0, 1500.0)), \
             mock.patch.object(dv_shim, "warp_pointer",
                               lambda x, y: moved.update(to=(x, y)) or True):
            # saved position was ALSO off-main (or unknown) -> centre of main
            dv_shim.release_pointer_to_main(saved=(3000.0, 1800.0))
            self.assertEqual(moved["to"], (864.0, 558.5))
            moved.clear()
            dv_shim.release_pointer_to_main(saved=None)
            self.assertEqual(moved["to"], (864.0, 558.5))

    def test_a_pointer_already_on_main_is_left_alone(self):
        # An hour-long analysis: if the user has been working, their cursor is where they
        # want it. Yanking it to a position remembered from an hour ago is its own
        # interruption.
        with mock.patch.object(dv_shim, "main_display_bounds", return_value=self.MAIN), \
             mock.patch.object(dv_shim, "pointer_position", return_value=(900.0, 600.0)), \
             mock.patch.object(dv_shim, "warp_pointer",
                               mock.Mock(side_effect=AssertionError("must not move it"))):
            self.assertFalse(dv_shim.release_pointer_to_main(saved=(10.0, 10.0)))

    def test_it_never_raises_since_it_runs_in_a_finally(self):
        with mock.patch.object(dv_shim, "main_display_bounds",
                               mock.Mock(side_effect=RuntimeError("boom"))):
            self.assertFalse(dv_shim.release_pointer_to_main(saved=(1.0, 1.0)))

    def test_the_pointer_is_released_even_when_the_takeover_FAILS(self):
        released = {}
        with mock.patch.object(dv_shim, "_warn_before_takeover", lambda *a: None), \
             mock.patch.object(dv_shim, "pointer_position", return_value=(50.0, 50.0)), \
             mock.patch.object(dv_shim, "release_pointer_to_main",
                               lambda saved=None: released.update(saved=saved) or True), \
             mock.patch.object(dv_shim, "screen_locked", return_value=False), \
             mock.patch.object(dv_shim, "main_display_geometry", return_value=(3456, 2234, 2.0, True)), \
             mock.patch.object(dv_shim, "host_view", return_value=(0.0, 0.0, 2.0, 1728.0, 1117.0)), \
             mock.patch.dict("sys.modules", {"resolve": mock.Mock(connect=lambda: object())}), \
             mock.patch.object(dv_shim, "goto_dolby_vision", return_value=False):
            with self.assertRaises(RuntimeError):
                dv_shim.run_dv_ui()
        self.assertEqual(released.get("saved"), (50.0, 50.0),
                         "a failed takeover must still hand the mouse back")
