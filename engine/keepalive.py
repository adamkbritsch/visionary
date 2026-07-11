"""Keep the flaky external scratch SSD's connection fresh.

The external "2TB SSD" connection degrades when left idle, which then makes the
next big read/write crawl (Resolve renders go ~1%/min vs finishing in ~10 min
when the drive is freshly mounted). So during idle periods the engine
force-reconnects the drive (the same unmount->mount cycle scratch.py uses) once
it has been idle past an interval (default 1 hour).

It NEVER cycles the drive while work is happening: every drive operation, and
every render-poll heartbeat, calls note_activity(), which resets the internal
clock — so a reconnect only ever fires after a genuine stretch of inactivity
(when the drive is safe to unmount).
"""
from __future__ import annotations
import time


class ScratchKeepalive:
    def __init__(self, reconnect, now=time.time, interval_s: int = 3600):
        self._reconnect = reconnect      # callable that cycles the external SSD
        self._now = now                  # internal clock (injectable for tests)
        self.interval_s = interval_s     # idle seconds before a reconnect (1 h)
        self._last = now()

    def note_activity(self) -> None:
        """Call on any drive activity (stage work, render-poll heartbeat)."""
        self._last = self._now()

    def seconds_idle(self) -> float:
        return self._now() - self._last

    def due(self) -> bool:
        return self.seconds_idle() >= self.interval_s

    def tick(self) -> bool:
        """Reconnect the SSD iff it has been idle past the interval.
        Returns True if it reconnected. Safe to call frequently."""
        if self.due():
            self._reconnect()
            self._last = self._now()     # the reconnect itself counts as activity
            return True
        return False


def default_keepalive(interval_s: int = 3600) -> "ScratchKeepalive":
    """Wire to the real external-SSD reconnect (scratch.ScratchManager.cycle)."""
    import os
    import scratch
    mgr = scratch.ScratchManager("2TB SSD", "topaz-scratch",
                                 os.path.expanduser("~/Downloads/topaz-scratch"))
    return ScratchKeepalive(reconnect=mgr.cycle, interval_s=interval_s)
