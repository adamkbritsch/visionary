"""A TTL'd request from a sibling app for the machine, and the reason it has a TTL.

Discretion (the content-filtering sibling) needs the whole machine for a scan or a masked render,
the same way Visionary's own Resolve and outpainting stages do. It asks for it here rather than by
pausing automation, because pausing is a state a human has to undo and this is a state that must
undo itself.

THE TTL IS THE SAFETY PROPERTY, not a convenience. A lease that had to be released explicitly would
mean a crashed, killed or force-quit Discretion silently wedges the upscaling queue for as long as
nobody notices — which, for an appliance that runs overnight, is until morning. With an expiry, the
worst case is that Visionary idles for the remainder of one lease and then carries on by itself.

NOT PERSISTED, deliberately. A Visionary restart clears every lease, which fails OPEN: the queue
resumes. Persisting them would make a restart the one way to inherit a wedge, which is exactly
backwards for the failure this is guarding against.

WHAT A LEASE DOES NOT DO. It never interrupts `resolve` or `upload`. Resolve holds the screen and
cannot be paced; an upload half-written to the NAS is the one thing worse than a slow one. Those
stages are already uninterruptible in Visionary's own deploy discipline and a lease respects the
same boundary — which also means a lease taken during one of them is HELD but not yet effective, and
its caller has to expect that.
"""

import threading
import time

# A lease longer than this is refused. Long enough for a 4K masked render of a feature film (measured
# at about 35 minutes with the copy path, under 6 hours without it), short enough that a forgotten
# one costs a single night rather than a week.
MAX_SECONDS = 6 * 3600
DEFAULT_SECONDS = 900

# Stages a lease must never cut into. Kept here rather than at the call site so the rule is stated
# once, next to the reason.
PROTECTED_STAGES = ("resolve", "extend", "upload")


class YieldLease(object):
    """At most one lease at a time. A second request from the same holder extends it."""

    def __init__(self, now=None, max_seconds=MAX_SECONDS):
        self._now = now or time.time
        self._max = float(max_seconds)
        self._lock = threading.Lock()
        self._holder = None
        self._reason = ""
        self._expires = 0.0
        self._taken_at = 0.0

    def take(self, holder, seconds=DEFAULT_SECONDS, reason=""):
        """(ok, detail). Grant or extend a lease.

        A DIFFERENT holder is refused while one is live rather than queued: two siblings both
        believing they have the machine is worse than one of them waiting and knowing it."""
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            return False, "seconds must be a number"
        if seconds <= 0:
            return False, "seconds must be positive"
        if seconds > self._max:
            return False, "at most %d seconds (asked for %d)" % (int(self._max), int(seconds))
        if not holder:
            return False, "a lease needs a holder, so an expired one can be attributed"
        now = self._now()
        with self._lock:
            if self._holder and self._holder != holder and now < self._expires:
                return False, ("%s holds the machine for another %d second(s)"
                               % (self._holder, int(self._expires - now)))
            extending = self._holder == holder and now < self._expires
            self._holder = holder
            self._reason = str(reason or "")
            self._expires = now + seconds
            if not extending:
                self._taken_at = now
            return True, ("extended to %d second(s)" if extending else "held for %d second(s)") \
                % int(seconds)

    def release(self, holder=None):
        """(ok, detail). Release early. A mismatched holder is refused, not silently honoured."""
        with self._lock:
            if not self._holder:
                return True, "no lease was held"
            if holder and holder != self._holder:
                return False, "%s holds the lease, not %s" % (self._holder, holder)
            was = self._holder
            self._holder, self._reason, self._expires = None, "", 0.0
            return True, "released %s" % was

    def active(self):
        """PURE-ish. Is a lease in force right now? Expiry is evaluated on read, so nothing has to
        run a timer for a lease to lapse."""
        with self._lock:
            if not self._holder:
                return False
            if self._now() >= self._expires:
                self._holder, self._reason, self._expires = None, "", 0.0
                return False
            return True

    def blocks(self, stage):
        """Should `stage` stand down for the lease?

        False for the protected stages even while a lease is live — see the module docstring."""
        if stage in PROTECTED_STAGES:
            return False
        return self.active()

    def state(self):
        """A dict for /api/state, so the UI can say WHO has the machine and for how long."""
        with self._lock:
            live = bool(self._holder) and self._now() < self._expires
            return {"held": live,
                    "holder": self._holder if live else None,
                    "reason": self._reason if live else "",
                    "seconds_left": int(max(0, self._expires - self._now())) if live else 0,
                    "held_for": int(self._now() - self._taken_at) if live else 0}
