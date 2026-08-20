import unittest
from unittest import mock

import power
from power import normalize_amperage, PowerReading, is_draining_on_ac



if __name__ == "__main__":
    unittest.main()


class SustainedAdapterWattage(unittest.TestCase):
    """A 140 W charger that renegotiates downward for a moment reports 120 and back
    (live-caught 2026-08-20). The run gate compares an instantaneous read against
    min_adapter_watts, so that dip read as "a weaker charger appeared" and paused a run that
    had lost nothing.

    NOT a relaxation of the wattage rule: the held value can only ever be one the adapter
    itself advertised, and it is tied to the adapter's Family Code, so an under-spec brick
    can neither reach 140 nor inherit it."""

    def setUp(self):
        power.reset_adapter_peak()
        self.addCleanup(power.reset_adapter_peak)

    def _reads(self, *reports):
        return mock.patch.object(power, "adapter_report", side_effect=list(reports))

    F = "0xe000400a"

    def test_a_dip_is_held_at_the_peak(self):
        with self._reads({"watts": 140, "family": self.F},
                         {"watts": 120, "family": self.F},
                         {"watts": 140, "family": self.F}):
            self.assertEqual(power.adapter_watts_sustained(), 140)
            self.assertEqual(power.adapter_watts_sustained(), 140)   # the dip
            self.assertEqual(power.adapter_watts_sustained(), 140)

    def test_an_under_spec_brick_never_reaches_140(self):
        with self._reads(*[{"watts": 96, "family": "0xdead"}] * 4):
            for _ in range(4):
                self.assertEqual(power.adapter_watts_sustained(), 96)

    def test_a_weaker_adapter_cannot_inherit_the_peak(self):
        with self._reads({"watts": 140, "family": self.F},
                         {"watts": 96, "family": "0xdead"},
                         {"watts": 96, "family": "0xdead"}):
            self.assertEqual(power.adapter_watts_sustained(), 140)
            self.assertEqual(power.adapter_watts_sustained(), 96)    # swapped -> its own history
            self.assertEqual(power.adapter_watts_sustained(), 96)

    def test_unplugging_forgets_the_adapter(self):
        with self._reads({"watts": 140, "family": self.F},
                         {"watts": None, "family": ""},
                         {"watts": 96, "family": "0xdead"}):
            self.assertEqual(power.adapter_watts_sustained(), 140)
            self.assertIsNone(power.adapter_watts_sustained())       # on battery
            self.assertEqual(power.adapter_watts_sustained(), 96)    # a new brick starts fresh

    def test_it_still_rises_with_the_adapter(self):
        # a brick that advertises MORE later is taken at its word
        with self._reads({"watts": 96, "family": self.F},
                         {"watts": 140, "family": self.F}):
            self.assertEqual(power.adapter_watts_sustained(), 96)
            self.assertEqual(power.adapter_watts_sustained(), 140)
