import unittest

import orchestrator as orch


class UnplugDecisionTest(unittest.TestCase):
    """Mid-stage AC-unplug grace: announce a countdown, pause only if it expires."""

    def dec(self, **kw):
        base = dict(on_ac=False, stage_active=True, unplug_since=None, now=1000.0, grace=60)
        base.update(kw)
        return orch.unplug_decision(**base)

    def test_no_active_stage_clears(self):
        self.assertEqual(self.dec(stage_active=False, unplug_since=1000.0), ("clear", None, 0))

    def test_on_ac_clears(self):
        self.assertEqual(self.dec(on_ac=True, unplug_since=1000.0), ("clear", None, 0))

    def test_first_unplug_starts_countdown(self):
        self.assertEqual(self.dec(unplug_since=None, now=1000.0), ("count", 1000.0, 60))

    def test_counting_down_announces_remaining(self):
        self.assertEqual(self.dec(unplug_since=1000.0, now=1030.0), ("count", 1000.0, 30))
        self.assertEqual(self.dec(unplug_since=1000.0, now=1059.0), ("count", 1000.0, 1))

    def test_grace_expired_pauses(self):
        self.assertEqual(self.dec(unplug_since=1000.0, now=1060.0), ("pause", None, 0))
        self.assertEqual(self.dec(unplug_since=1000.0, now=1075.0), ("pause", None, 0))

    def test_default_grace_is_60s(self):
        self.assertEqual(orch.UNPLUG_GRACE_SECONDS, 60)


class RemuxNoSpecialGraceTest(unittest.TestCase):
    """The remux is now segmented + on the durable finisher work-list, so a kill loses ≤1 short
    segment and resumes — it NO LONGER earns the old 30-min unplug cushion. Every stage, remux
    included, pauses at the same 60 s cutoff as topaz (user-dictated)."""

    def test_special_grace_removed(self):
        self.assertFalse(hasattr(orch, "REMUX_UNPLUG_GRACE_SECONDS"))
        self.assertFalse(hasattr(orch, "unplug_grace_for"))

    def test_remux_pauses_at_60s_like_topaz(self):
        # 2 min unplugged (any stage, incl. remux) at the one grace → PAUSED, no 30-min leeway
        action, _, _ = orch.unplug_decision(
            on_ac=False, stage_active=True, unplug_since=1000.0, now=1120.0,
            grace=orch.UNPLUG_GRACE_SECONDS)
        self.assertEqual(action, "pause")

    def test_grace_label_formats(self):
        self.assertEqual(orch.grace_label(42), "42s")
        self.assertEqual(orch.grace_label(119), "119s")
        self.assertEqual(orch.grace_label(1680), "28m")   # generic formatter still handles minutes


if __name__ == "__main__":
    unittest.main()
