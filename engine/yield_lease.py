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
import uuid

# A lease longer than this is refused. Long enough for a 4K masked render of a feature film (measured
# at about 35 minutes with the copy path, under 6 hours without it), short enough that a forgotten
# one costs a single night rather than a week.
MAX_SECONDS = 6 * 3600
DEFAULT_SECONDS = 900

# Stages a lease must never cut into. Kept here rather than at the call site so the rule is stated
# once, next to the reason.
PROTECTED_STAGES = ("resolve", "extend", "upload")


class YieldLease(object):
    """At most one lease at a time. A second request from the same holder extends it.

    Every lease carries an `id`, and the id is how a holder learns it no longer has the machine.
    The lease lives only in this process, so a deploy, a quit or a relaunch drops it (see the module
    docstring: that is deliberate, and it fails open). Without an id, the holder cannot tell that
    loss from a lease it never had: on 2026-09-23 a sibling twice read back not-held within a minute
    of a successful take, and neither side could say afterwards whether the server had been replaced,
    the lease released by the sibling's own cleanup path, or something else. A holder that keeps its
    id and compares it against /api/state sees every one of those as the same fact — this is not my
    lease any more — and can re-take.

    `on_change` is called with a one-line message on every transition (taken, extended, refused,
    released, expired), outside the lock. The orchestrator wires it to the log, so the next time a
    lease vanishes there is a record of which of those it was. A callback that raises is swallowed:
    the log is not the point."""

    def __init__(self, now=None, max_seconds=MAX_SECONDS, on_change=None):
        self._now = now or time.time
        self._max = float(max_seconds)
        self._on_change = on_change
        self._lock = threading.Lock()
        self._holder = None
        self._reason = ""
        self._id = None
        self._expires = 0.0
        self._taken_at = 0.0

    # ---- internals -------------------------------------------------------------------------

    def _report(self, msg):
        """Announce a transition. NEVER called with the lock held, and never raises."""
        if not (msg and self._on_change):
            return
        try:
            self._on_change(msg)
        except Exception:
            pass

    def _expire_if_due(self, now):
        """Clear a lapsed lease. Call with the lock HELD; returns a message to report after it.

        Expiry is evaluated on read so nothing has to run a timer, which means whichever read gets
        there first — the run thread's stage check, the remux gate, or /api/state — is the one that
        notices. Clearing here rather than in each caller is what makes it reported exactly once."""
        if not self._holder or now < self._expires:
            return None
        was, held_for = self._holder, int(now - self._taken_at)
        self._holder, self._reason, self._id, self._expires = None, "", None, 0.0
        return "%s's lease expired after %ds — the queue resumes" % (was, held_for)

    # ---- the lease -------------------------------------------------------------------------

    def take(self, holder, seconds=DEFAULT_SECONDS, reason=""):
        """(ok, detail). Grant or extend a lease. The id is in state()["id"].

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
            lapsed = self._expire_if_due(now)
            if self._holder and self._holder != holder:
                refused = ("%s holds the machine for another %d second(s)"
                           % (self._holder, int(self._expires - now)))
                note = "%s refused: %s" % (holder, refused)
                ok, detail = False, refused
            else:
                extending = self._holder == holder
                self._holder = holder
                self._reason = str(reason or "")
                self._expires = now + seconds
                if not extending:
                    # A NEW lease, so a new id: the holder of the old one must not think it still
                    # has this one. An extension deliberately keeps its id — it is one lease.
                    self._id = uuid.uuid4().hex
                    self._taken_at = now
                detail = ("extended to %d second(s)" if extending else "held for %d second(s)") \
                    % int(seconds)
                note = ("%s %s%s" % (holder, "extended its lease to %ds" % int(seconds) if extending
                                     else "took the machine for %ds" % int(seconds),
                                     (" (%s)" % self._reason) if self._reason else ""))
                ok = True
        self._report(lapsed)
        self._report(note)
        return ok, detail

    def release(self, holder=None, lease_id=None):
        """(ok, detail). Release early. A mismatched holder is refused, not silently honoured.

        `lease_id` scopes the release to ONE lease: a release that arrives late — from a pass whose
        lease already lapsed, or from before a restart — must not free whatever holds the machine
        now. Omitting it releases whatever the holder holds, which is what the operator escape and
        an id-less caller need."""
        with self._lock:
            lapsed = self._expire_if_due(self._now())
            if not self._holder:
                note, out = None, (True, "no lease was held")
            elif holder and holder != self._holder:
                note = "%s could not release %s's lease" % (holder, self._holder)
                out = (False, "%s holds the lease, not %s" % (self._holder, holder))
            elif lease_id and lease_id != self._id:
                note = "%s tried to release a lease that is no longer the live one" % self._holder
                out = (False, "that lease is gone; another lease holds the machine now")
            else:
                was = self._holder
                self._holder, self._reason, self._id, self._expires = None, "", None, 0.0
                note, out = "%s released the machine" % was, (True, "released %s" % was)
        self._report(lapsed)
        self._report(note)
        return out

    def active(self):
        """Is a lease in force right now? Expiry is evaluated on read, so nothing has to run a
        timer for a lease to lapse."""
        with self._lock:
            lapsed = self._expire_if_due(self._now())
            live = bool(self._holder)
        self._report(lapsed)
        return live

    def blocks(self, stage):
        """Should `stage` stand down for the lease?

        False for the protected stages even while a lease is live — see the module docstring."""
        if stage in PROTECTED_STAGES:
            return False
        return self.active()

    def state(self):
        """A dict for /api/state, so the UI can say WHO has the machine and for how long, and so a
        holder can check its `id` is still the live one."""
        with self._lock:
            lapsed = self._expire_if_due(self._now())
            live = bool(self._holder)
            out = {"held": live,
                   "holder": self._holder,
                   "reason": self._reason if live else "",
                   "id": self._id,
                   "seconds_left": int(max(0, self._expires - self._now())) if live else 0,
                   "held_for": int(self._now() - self._taken_at) if live else 0}
        self._report(lapsed)
        return out
