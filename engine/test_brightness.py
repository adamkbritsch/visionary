import unittest
from unittest import mock

import brightness


class DimTickTest(unittest.TestCase):
    TH = 15 * 60

    def test_idle_past_threshold_screen_lit_dims(self):
        self.assertEqual(brightness.dim_tick(self.TH, self.TH, 0.5, dimmed_by_us=False), "dim")
        self.assertEqual(brightness.dim_tick(self.TH + 100, self.TH, 0.8, dimmed_by_us=False), "dim")

    def test_after_dimming_holds_dark(self):
        # we dimmed last tick → cur is ~0 now; nothing to do, stay dark (no auto-restore)
        self.assertEqual(brightness.dim_tick(2, self.TH, 0.0, dimmed_by_us=True), "hold")
        self.assertEqual(brightness.dim_tick(self.TH + 100, self.TH, 0.0, dimmed_by_us=True), "hold")

    def test_user_raises_brightness_while_we_hold_it_dark_release(self):
        # user tapped the brightness key → cur is back up → hands off (clear our saved level)
        self.assertEqual(brightness.dim_tick(2, self.TH, 0.6, dimmed_by_us=True), "release")

    def test_active_and_not_dimmed_holds(self):
        self.assertEqual(brightness.dim_tick(2, self.TH, 0.5, dimmed_by_us=False), "hold")

    def test_already_dark_screen_is_not_re_dimmed(self):
        # screen already at ~0 (user set it) → don't "memorise" 0 as the restore level
        self.assertEqual(brightness.dim_tick(self.TH + 100, self.TH, 0.0, dimmed_by_us=False), "hold")

    def test_idle_unknown_does_not_dim(self):
        self.assertEqual(brightness.dim_tick(None, self.TH, 0.5, dimmed_by_us=False), "hold")

    def test_threshold_zero_disables_dimming(self):
        self.assertEqual(brightness.dim_tick(self.TH + 100, 0, 0.5, dimmed_by_us=False), "hold")


if __name__ == "__main__":
    unittest.main()


class WatchingIsNotIdle(unittest.TestCase):
    """The dimmer measures HID idle, and watching a video is the one activity with no input
    at all — sit still for 15 minutes and the screen went dark mid-episode (user-caught
    2026-08-22). Every player takes the same PreventUserIdleDisplaySleep assertion macOS
    itself honours, so the dimmer reads that and never overrides what the system would have
    allowed anyway."""

    TH = 900

    def test_a_long_idle_does_NOT_dim_while_something_is_playing(self):
        self.assertEqual(brightness.dim_tick(self.TH + 9999, self.TH, 0.7,
                                             dimmed_by_us=False, others_awake=True), "hold")

    def test_the_same_idle_still_dims_when_nothing_is_playing(self):
        self.assertEqual(brightness.dim_tick(self.TH + 9999, self.TH, 0.7,
                                             dimmed_by_us=False, others_awake=False), "dim")

    def test_playback_starting_while_we_hold_it_dark_restores_it(self):
        # pressing play is HID activity, which this dimmer deliberately ignores — without
        # this the screen stayed black for the whole episode
        self.assertEqual(brightness.dim_tick(self.TH + 9999, self.TH, 0.0,
                                             dimmed_by_us=True, others_awake=True), "restore")

    def test_our_dark_screen_stays_dark_when_nothing_is_playing(self):
        self.assertEqual(brightness.dim_tick(2, self.TH, 0.0,
                                             dimmed_by_us=True, others_awake=False), "hold")

    def test_the_user_raising_it_still_wins_over_a_player(self):
        self.assertEqual(brightness.dim_tick(2, self.TH, 0.6,
                                             dimmed_by_us=True, others_awake=True), "release")

    def test_the_default_keeps_every_existing_caller_unchanged(self):
        self.assertEqual(brightness.dim_tick(self.TH + 100, self.TH, 0.8,
                                             dimmed_by_us=False), "dim")


class WhoIsHoldingTheDisplayAwake(unittest.TestCase):
    """Our own caffeinate asserts PreventUserIdleDisplaySleep for the whole run, so it must
    never read as 'something is playing'."""

    def _holder(self, text):
        run = lambda *a, **k: mock.Mock(stdout=text)
        return brightness.display_awake_holder(_run=run)

    PLAYER = ('   pid 44471(Plex HTPC): [0x0001] 0:00:12 PreventUserIdleDisplaySleep '
              'named: "tv.plex.player"\n')
    OURS = ('   pid 22597(caffeinate): [0x0002] 27:48:46 PreventUserIdleDisplaySleep '
            'named: "caffeinate command-line tool"\n')
    SYSTEM_ONLY = ('   pid 33(loginwindow): [0x0003] 1:00:00 PreventUserIdleSystemSleep '
                   'named: "something else"\n')

    def test_a_player_is_named(self):
        self.assertEqual(self._holder(self.PLAYER), "Plex HTPC")

    def test_our_own_caffeinate_is_never_it(self):
        self.assertIsNone(self._holder(self.OURS))

    def test_a_player_is_found_even_beside_our_caffeinate(self):
        self.assertEqual(self._holder(self.OURS + self.PLAYER), "Plex HTPC")

    def test_a_system_sleep_assertion_is_not_a_display_one(self):
        self.assertIsNone(self._holder(self.SYSTEM_ONLY))

    def test_nothing_at_all(self):
        self.assertIsNone(self._holder(""))

    def test_an_unreadable_probe_does_not_disable_dimming_for_the_run(self):
        def boom(*a, **k):
            raise OSError("pmset gone")
        self.assertIsNone(brightness.display_awake_holder(_run=boom))
