import unittest
from unittest import mock

import power

_PEAKS_PATCH = None


def _fresh_book():
    """A brand-new empty book for ONE test. The book is real durable state
    (~/.topaz-pipeline/adapter_peaks.json) and it LIFTS sustained readings, so both a live
    seed and a previous TEST's write leak forward unless every test starts blank."""
    import tempfile, os as _os
    return _os.path.join(tempfile.mkdtemp(), "peaks.json")


def setUpModule():
    global _PEAKS_PATCH
    _PEAKS_PATCH = mock.patch.object(power, "PEAKS_FILE", _fresh_book())
    _PEAKS_PATCH.start()


def tearDownModule():
    if _PEAKS_PATCH is not None:
        _PEAKS_PATCH.stop()

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
        p = mock.patch.object(power, "PEAKS_FILE", _fresh_book())
        p.start(); self.addCleanup(p.stop)

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


class TheChargersIdentityOutlivesItsNegotiation(unittest.TestCase):
    """The UGREEN 140 W brick does worse than dip to 120 — it can NEGOTIATE 120 and settle
    there for a whole session, and every deploy restarts the process that was remembering
    the 140. Two minutes of samples on 2026-08-27: solid 120, never a flip. The user's
    standing rule is that this IS the 140 W charger; a durable per-family book now carries
    the best wattage each adapter has ever advertised across restarts."""

    def setUp(self):
        self._orig = power.PEAKS_FILE
        power.PEAKS_FILE = _fresh_book()
        power.reset_adapter_peak()

    def tearDown(self):
        power.PEAKS_FILE = self._orig
        power.reset_adapter_peak()

    def _sustained(self, watts, family="0xe000400a"):
        with mock.patch.object(power, "adapter_report",
                               return_value={"watts": watts, "family": family}):
            return power.adapter_watts_sustained()

    def test_a_settled_120_reads_as_140_once_the_family_is_known_for_140(self):
        self.assertEqual(self._sustained(140), 140)     # witnessed once...
        power.reset_adapter_peak()                      # ...process restarts (deploy)
        self.assertEqual(self._sustained(120), 140)     # settled at 120 -> still 140

    def test_a_genuinely_weaker_charger_never_inherits(self):
        self._sustained(140, family="0xe000400a")
        self.assertEqual(self._sustained(96, family="0xdeadbeef"), 96)

    def test_the_book_only_ever_rises(self):
        self._sustained(140)
        self._sustained(120)
        self.assertEqual(power._peaks_book().get("0xe000400a"), 140)

    def test_unplugging_forgets_the_session_but_not_the_book(self):
        self._sustained(140)
        with mock.patch.object(power, "adapter_report",
                               return_value={"watts": None, "family": ""}):
            self.assertIsNone(power.adapter_watts_sustained())
        self.assertEqual(self._sustained(120), 140)     # replugged, settled low -> lifted

    def test_an_unwritable_book_degrades_to_the_session_peak(self):
        power.PEAKS_FILE = "/nonexistent/dir/peaks.json"
        self.assertEqual(self._sustained(140), 140)     # in-memory peak still serves
        self.assertEqual(self._sustained(120), 140)
