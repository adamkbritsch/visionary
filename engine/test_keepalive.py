import unittest
import keepalive


class Clock:
    """Controllable internal clock for deterministic tests."""
    def __init__(self, t=1000.0):
        self.t = t
    def __call__(self):
        return self.t
    def advance(self, seconds):
        self.t += seconds


class ScratchKeepalive(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.reconnects = []
        self.ka = keepalive.ScratchKeepalive(
            reconnect=lambda: self.reconnects.append(self.clock()),
            now=self.clock, interval_s=3600)   # 1 hour

    def test_not_due_when_fresh(self):
        self.assertFalse(self.ka.due())
        self.assertFalse(self.ka.tick())
        self.assertEqual(self.reconnects, [])

    def test_reconnects_after_an_hour_idle(self):
        self.clock.advance(3600)
        self.assertTrue(self.ka.due())
        self.assertTrue(self.ka.tick())
        self.assertEqual(len(self.reconnects), 1)

    def test_tick_resets_clock_after_reconnect(self):
        self.clock.advance(3700)
        self.ka.tick()
        self.assertFalse(self.ka.due())            # reset; reconnect counts as activity
        self.clock.advance(100)
        self.assertFalse(self.ka.tick())

    def test_note_activity_resets_idle_timer(self):
        self.clock.advance(1800)
        self.ka.note_activity()
        self.clock.advance(1800)                   # 1800s since the activity, < 3600
        self.assertFalse(self.ka.due())

    def test_active_work_never_triggers_reconnect(self):
        # heartbeating activity every 10 min (e.g. render-poll) must keep it idle-free
        for _ in range(12):
            self.clock.advance(600)
            self.ka.note_activity()
            self.assertFalse(self.ka.tick())
        self.assertEqual(self.reconnects, [])


if __name__ == "__main__":
    unittest.main()
