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

WHO MAY HOLD ONE (user rule, 2026-10-06: "expurgate shouldn't have the machine unless it's open and
activated. Those rules shouldn't be possible to bypass"). Only Expurgate's own engine, while the
Expurgate app is open and its automation is activated — verified by `gate` (expurgate_gate.py) from
the kernel and Expurgate's own state on EVERY take and renewal, and re-checked every GUARD_SECS while
a lease is held. The request's `holder` string is a label, never a credential: until then anything on
this Mac that said "discretion" held the machine, and an orphaned training script did so all day.

WHAT A LEASE DOES NOT DO. It never interrupts `resolve` or `upload`. Resolve holds the screen and
cannot be paced; an upload half-written to the NAS is the one thing worse than a slow one. Those
stages are already uninterruptible in Visionary's own deploy discipline and a lease respects the
same boundary — which also means a lease taken during one of them is HELD but not yet effective, and
its caller has to expect that.
"""

import sys
import threading
import time
import uuid

import expurgate_gate

# A lease longer than this is refused. Long enough for a 4K masked render of a feature film (measured
# at about 35 minutes with the copy path, under 6 hours without it), short enough that a forgotten
# one costs a single night rather than a week.
MAX_SECONDS = 6 * 3600
DEFAULT_SECONDS = 900

# Stages a lease must never cut into. Kept here rather than at the call site so the rule is stated
# once, next to the reason.
PROTECTED_STAGES = ("resolve", "extend", "upload")

GUARD_SECS = 10          # how often a held lease is re-checked against Expurgate's real state
SOFT_MISSES = 2          # Expurgate not ANSWERING this many checks in a row revokes it too
REFUSAL_LOG_SECS = 600   # a refusal repeated by a renewing caller is logged once per this


def _spawn_daemon(fn):
    if "unittest" in sys.modules:          # tests drive guard_tick() themselves
        return
    threading.Thread(target=fn, daemon=True, name="yield-lease-guard").start()


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

    def __init__(self, now=None, max_seconds=MAX_SECONDS, on_change=None, gate=expurgate_gate,
                 spawn=None, sleep=None):
        self._now = now or time.time
        self._max = float(max_seconds)
        self._on_change = on_change
        self._gate = gate                   # who may hold the machine — see the module docstring
        self._spawn = spawn or _spawn_daemon
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._holder = None
        self._reason = ""
        self._id = None
        self._owner = None                  # the verified Expurgate engine pid behind the lease
        self._expires = 0.0
        self._taken_at = 0.0
        self._soft_misses = 0
        self._guarding = False
        self._guard_gen = 0                 # which guard thread owns _guarding
        self._refused_at = {}               # (holder, why) -> when that refusal was last logged

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
        self._clear()
        return "%s's lease expired after %ds — the queue resumes" % (was, held_for)

    def _clear(self):
        """Drop the lease. Call with the lock HELD."""
        self._holder, self._reason, self._id, self._expires = None, "", None, 0.0
        self._owner, self._soft_misses = None, 0

    def _refusal_locked(self, holder, why, now):
        """The log line for a refused take — once per REFUSAL_LOG_SECS for a repeated refusal, so
        a caller renewing every few minutes cannot flood the run log. Call with the lock HELD."""
        key = (holder, why)
        last = self._refused_at.get(key)
        if last is not None and 0 <= now - last < REFUSAL_LOG_SECS:
            return None
        if len(self._refused_at) >= 64:          # bounded: holder is the caller's own string
            self._refused_at = {k: t for k, t in self._refused_at.items()
                                if 0 <= now - t < REFUSAL_LOG_SECS}
        self._refused_at[key] = now
        return "%s refused: %s" % (holder, why)

    # ---- the lease -------------------------------------------------------------------------

    def take(self, holder, seconds=DEFAULT_SECONDS, reason="", peer=None):
        """(ok, detail). Grant or extend a lease. The id is in state()["id"].

        `peer` is the request's connection, (client address, our address): the gate identifies the
        program behind it and grants nothing unless it is Expurgate's own engine with Expurgate open
        and activated — on a renewal exactly as on a first take. A gate that errors refuses. The one
        allowance: a renewal from the SAME verified engine survives a check that merely could not
        complete, as many times in a row as the guard would tolerate (SOFT_MISSES).

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
        try:
            allowed, why, owner, hard = self._gate.verify_request(peer)
        except Exception as e:  # noqa: BLE001 — a check that cannot run grants nothing
            allowed, why, owner, hard = (False, "the Expurgate check failed (%s)"
                                         % e.__class__.__name__, None, False)
        now = self._now()
        start_guard = False
        with self._lock:
            # ONE decision and ONE grant under ONE lock: a renewal tolerated on a soft miss may
            # only extend the very lease it was checked against — never create one, even if the
            # guard revoked that lease a moment ago (review 2026-10-06)
            lapsed = self._expire_if_due(now)
            tolerate = (not allowed and not hard and owner is not None and self._holder == holder
                        and self._owner == owner and self._soft_misses + 1 < SOFT_MISSES)
            if not allowed and not tolerate:
                note, ok, detail = self._refusal_locked(holder, why, now), False, why
            else:
                if allowed:
                    self._soft_misses = 0
                    if self._holder == holder and self._owner != owner:
                        # the same name from a DIFFERENT verified engine: Expurgate restarted, so
                        # the old lease's engine is gone — a new lease, not an extension of it
                        self._clear()
                else:
                    self._soft_misses += 1
                if self._holder and self._holder != holder:
                    refused = ("%s holds the machine for another %d second(s)"
                               % (self._holder, int(self._expires - now)))
                    note = "%s refused: %s" % (holder, refused)
                    ok, detail = False, refused
                else:
                    extending = self._holder == holder
                    self._holder = holder
                    self._owner = owner
                    self._reason = str(reason or "")
                    # A renewal only ever moves the expiry LATER. One holder can be several tasks —
                    # Discretion runs them all as `discretion` — and a short take from one of them
                    # must not cut another's hold short: live 2026-09-24, a 900 s "Pass 1 on a.mkv"
                    # trimmed a 3600 s detection lease two minutes after it was granted.
                    asked = now + seconds
                    self._expires = max(self._expires, asked) if extending else asked
                    if not extending:
                        # A NEW lease, so a new id: the holder of the old one must not think it
                        # still has this one. An extension deliberately keeps its id — one lease.
                        self._id = uuid.uuid4().hex
                        self._taken_at = now
                    left = int(self._expires - now)
                    detail = (("extended to %d second(s)" if extending else "held for %d second(s)")
                              % left)
                    note = ("%s %s%s" % (holder, "extended its lease to %ds" % left if extending
                                         else "took the machine for %ds" % left,
                                         (" (%s)" % self._reason) if self._reason else ""))
                    ok = True
                    start_guard = not self._guarding
                    if start_guard:
                        self._guarding = True
                        self._guard_gen += 1
                        gen = self._guard_gen
        self._report(lapsed)
        self._report(note)
        if ok and start_guard:
            try:
                self._spawn(lambda: self._guard_loop(gen))
            except Exception:  # noqa: BLE001 — never fail a granted take; the next one re-arms
                with self._lock:
                    if self._guard_gen == gen:
                        self._guarding = False
        return ok, detail

    # ---- the guard -------------------------------------------------------------------------

    def guard_tick(self):
        """Re-check the held lease against Expurgate's real state: revoke it the moment Expurgate
        is closed, deactivated or replaced, and after SOFT_MISSES checks in a row it did not answer.
        True while a lease is still held afterwards (the guard keeps going), False otherwise."""
        with self._lock:
            lapsed = self._expire_if_due(self._now())
            holder, owner, lid = self._holder, self._owner, self._id
        self._report(lapsed)
        if not holder:
            return False
        try:
            ok, why, hard = self._gate.check_engine(owner)
        except Exception as e:  # noqa: BLE001
            ok, why, hard = False, "the Expurgate check failed (%s)" % e.__class__.__name__, False
        with self._lock:
            if self._id != lid:                 # replaced or released meanwhile: not ours to judge
                return bool(self._holder)
            if ok:
                self._soft_misses = 0
                return True
            if not hard:
                self._soft_misses += 1
                if self._soft_misses < SOFT_MISSES:
                    return True
            held_for = int(self._now() - self._taken_at)
            self._clear()
            note = "%s's lease revoked after %ds — %s; the queue resumes" % (holder, held_for, why)
        self._report(note)
        return False

    def _guard_loop(self, gen=None):
        """Runs while a lease is held; one at a time. Exits when none is, and re-arms if a lease
        was granted in the moment it was leaving. Whatever ends it, the next take can start one —
        and only THIS guard's own flag is ever cleared (`gen`), never a newer guard's."""
        if gen is None:
            with self._lock:
                gen = self._guard_gen
        try:
            while True:
                self._sleep(GUARD_SECS)
                if self.guard_tick():
                    continue
                with self._lock:
                    if self._holder:
                        continue
                    if self._guard_gen == gen:   # decided and cleared in ONE lock section: a take
                        self._guarding = False   # between the two found _guarding still True and
                    return                       # armed nothing (review 2026-10-06)
        finally:
            with self._lock:
                if self._guard_gen == gen:
                    self._guarding = False

    def release(self, holder=None, lease_id=None, peer=None):
        """(ok, detail). Release early. A mismatched holder is refused, not silently honoured.

        `lease_id` scopes the release to ONE lease: a release that arrives late — from a pass whose
        lease already lapsed, or from before a restart — must not free whatever holds the machine
        now. Omitting it releases whatever the holder holds, which is what an id-less caller needs.
        (There is no manual release while Expurgate holds a lease: deactivating or closing Expurgate
        is the way to take the machine back, and the guard frees it within GUARD_SECS.)

        `peer` (the request's connection) must be the lease's OWN engine: a release is a request
        too, and an orphaned script that "released discretion" after every scan would knock out
        the real Expurgate's lease mid-pass (review 2026-10-06). When the sender cannot be
        identified at all it errs toward releasing — wrongly refusing costs Visionary a lease."""
        sender, sure = (None, False)
        if peer is not None:
            try:
                sender, sure = self._gate.identify(peer)
            except Exception:  # noqa: BLE001
                sender, sure = None, False
        with self._lock:
            lapsed = self._expire_if_due(self._now())
            if (peer is not None and self._holder and self._owner is not None and sure
                    and sender != self._owner):
                note = "%s's lease kept: a release came from another program" % self._holder
                out = (False, "only the program holding the lease can release it")
            elif not self._holder:
                note, out = None, (True, "no lease was held")
            elif holder and holder != self._holder:
                note = "%s could not release %s's lease" % (holder, self._holder)
                out = (False, "%s holds the lease, not %s" % (self._holder, holder))
            elif lease_id and lease_id != self._id:
                note = "%s tried to release a lease that is no longer the live one" % self._holder
                out = (False, "that lease is gone; another lease holds the machine now")
            else:
                was = self._holder
                self._clear()
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
