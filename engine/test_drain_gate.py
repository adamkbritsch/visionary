import unittest

import orchestrator as orch


class DrainGateTest(unittest.TestCase):
    """Pause only after the battery drains >5% below where draining began, on AC.
    Charging/holding (incl. the 80% smart-charge cap) resets the baseline."""

    def gate(self, **kw):
        base = dict(on_ac=True, draining=True, battery_pct=80, baseline=None, pause_on_drain=True)
        base.update(kw)
        return orch.drain_gate(**base)

    def test_off_ac_pauses(self):
        self.assertEqual(self.gate(on_ac=False, baseline=80), ("pause", None))

    def test_not_draining_runs_and_resets_baseline(self):
        # charging or just holding steady → baseline tracks current
        self.assertEqual(self.gate(draining=False, battery_pct=80, baseline=100), ("run", 80))

    def test_smart_charge_hold_at_80_is_not_a_drain(self):
        # held at 80% by optimized charging (not discharging) → run, baseline 80
        self.assertEqual(self.gate(draining=False, battery_pct=80, baseline=None), ("run", 80))

    def test_drain_pausing_disabled_runs(self):
        self.assertEqual(self.gate(pause_on_drain=False, battery_pct=70, baseline=80), ("run", 70))

    def test_first_drain_sample_sets_baseline_and_runs(self):
        self.assertEqual(self.gate(battery_pct=80, baseline=None), ("run", 80))

    def test_small_drain_keeps_running(self):
        self.assertEqual(self.gate(battery_pct=78, baseline=80), ("run", 80))    # 2% drop

    def test_exactly_5pct_still_runs(self):
        self.assertEqual(self.gate(battery_pct=75, baseline=80), ("run", 80))    # 5% is not > 5

    def test_over_5pct_pauses(self):
        self.assertEqual(self.gate(battery_pct=74, baseline=80), ("pause", 80))  # 6% drop
        self.assertEqual(self.gate(battery_pct=60, baseline=80), ("pause", 80))

    def test_default_threshold_is_5(self):
        self.assertEqual(orch.DRAIN_PCT_THRESHOLD, 5)


if __name__ == "__main__":
    unittest.main()
