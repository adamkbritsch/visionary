import unittest

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
