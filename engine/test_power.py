import unittest
import power
from power import normalize_amperage, PowerReading, is_draining_on_ac, DrainMonitor


class NormalizeAmperage(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(normalize_amperage(0), 0)

    def test_positive_charging_stays_positive(self):
        self.assertEqual(normalize_amperage(1200), 1200)

    def test_unsigned_64bit_negative_is_reinterpreted_signed(self):
        # macOS prints discharging current as unsigned 2^64 - x
        self.assertEqual(normalize_amperage(18446744073709540983), -10633)


def reading(ext=True, amp=0, cap=95, charging=False):
    return PowerReading(external_connected=ext, is_charging=charging, capacity=cap, amperage=amp)


class IsDrainingOnAc(unittest.TestCase):
    def test_draining_while_plugged_in_is_true(self):
        self.assertTrue(is_draining_on_ac(reading(ext=True, amp=-1500)))

    def test_charging_while_plugged_in_is_false(self):
        self.assertFalse(is_draining_on_ac(reading(ext=True, amp=1500, charging=True)))

    def test_tiny_negative_within_noise_floor_is_false(self):
        self.assertFalse(is_draining_on_ac(reading(ext=True, amp=-20)))

    def test_on_battery_is_false(self):
        # not plugged in at all -> handled by the separate on-AC gate, not "inadequate supply"
        self.assertFalse(is_draining_on_ac(reading(ext=False, amp=-1500)))


class Monitor(unittest.TestCase):
    def test_sustained_drain_is_inadequate(self):
        m = DrainMonitor(min_consecutive=3)
        for _ in range(3):
            m.add(reading(amp=-2000))
        self.assertTrue(m.inadequate())

    def test_recovery_resets_streak(self):
        m = DrainMonitor(min_consecutive=3)
        m.add(reading(amp=-2000)); m.add(reading(amp=-2000)); m.add(reading(amp=900))
        self.assertFalse(m.inadequate())

    def test_not_enough_samples_yet(self):
        m = DrainMonitor(min_consecutive=3)
        m.add(reading(amp=-2000)); m.add(reading(amp=-2000))
        self.assertFalse(m.inadequate())


if __name__ == "__main__":
    unittest.main()
