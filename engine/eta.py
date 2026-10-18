"""PURE ETA math for the OVERLAPPED pipeline (unit-tested; no I/O, no orchestrator import).

The problem this solves: the remux's remaining time spans TWO regimes, and the old estimator
extrapolated the whole remainder at the rate of whichever one it happened to be in.

    Phase 1  the next item's Topaz runs alongside  -> the remux encodes SLOW (contended)
    Phase 2  the run thread parks at the Resolve gate (`resolve_must_wait` holds the next
             Resolve until THIS remux finishes) -> the remux has the machine (solo)

Measured on the 16" M3 Max: contended 3.12 fps with ~55k frames left, i.e. a displayed 4h46m —
a correct extrapolation of the wrong regime, since roughly 70% of that work actually runs solo.

THE EQUATION, in ETA space rather than frame space (better conditioned, and the crossing test
becomes a comparison of two like quantities instead of a product that drifts between ticks):

    E_self  = remaining / rate_now      the contended ETA of the job being predicted
    E_other = the other job's contended ETA (its own published estimate)
    k       = solo / contended speedup of the job being predicted

    ETA = min(E_self, E_other) + max(0, E_self - E_other) / k

No fixed point is needed even though both inputs are measured under mutual contention. The
system is TRIANGULAR: whichever job finishes first is contended for its entire remaining life,
so its contended ETA is unbiased by construction, and the loser's correction is a one-step
function of the winner's uncorrected value. `argmin` is preserved because the map is monotone
in E_self, so the ordering can never flip.

At k = 1 this reduces exactly to the old formula, which is why the estimate can never be worse:
removing a competing load cannot slow x265, so k >= 1 physically, and therefore the old ETA is a
provable UPPER BOUND on the truth. Clamping k also bounds how optimistic the correction can get.
"""
from __future__ import annotations
import math

# x265 --preset fast on 4K 10-bit never approaches this. The resume replay burst does: on every
# restart `dvcap.encode_capped_segmented` re-fires one progress callback per already-finished
# segment (~1 s apart), so frames leap by thousands per second. The old estimator swallowed that
# burst into its rate window and produced an ETA ~45x too low for hours. This gate is what stops
# it, and it matters more than the contention correction itself.
R_MAX_FPS = 30.0

# A tick with no frames is only evidence of a slow encode if it is SHORT. A long one is a hold
# (Resolve preemption, power pause) and must not be banked as rate evidence.
MAX_IDLE_TICK = 120.0

# Enough of a regime before its rate is trustworthy. Deliberately stricter than the display gate
# in the old code (15 s / 30 frames): inter-SEGMENT rate scatter is ~11% here, so a window has to
# span real work before its ratio means anything.
MIN_RATE_FRAMES = 300
MIN_RATE_SECONDS = 120.0

# Learning k. The prior is a physical estimate, not a guess: x265 was measured at ~548% CPU while
# contended on a 12P+4E M3 Max, and --preset fast at 4K10 realistically saturates ~9-11 cores
# solo, giving k in [1.6, 2.0]. 1.5 sits just under that because the clamp floor is the safety
# net and 1.5 already captures most of the available correction.
K_PRIOR = 1.5
K_PRIOR_WEIGHT = 3      # in units of observations; one real sample moves 1.5 -> ~1.6
K_MIN, K_MAX = 1.0, 2.5
K_SAMPLE_CAP = 30

CONTENDED = "contended"
SOLO = "solo"

# After the last frame is encoded the remux is FAR from finished: concat of the whole elementary
# stream, LUFS measure, audio/subtitle extract, MP4Box mux, verify, and a full packet-level peak
# measurement of the shipped 4K file. All of it is invisible to progress, so the countdown used to
# reach 0 with many minutes left. Every one of those terms is linear in output size, so the tail
# is modelled per 1000 frames and learned from what actually happens.
TAIL_SECS_PER_KFRAME = 4.0        # prior: ~4.5 min on a ~67k-frame episode
TAIL_MIN_SECS = 20.0              # never show 0 while a process is alive


def tail_estimate(total_frames, secs_per_kframe: float = TAIL_SECS_PER_KFRAME) -> float:
    """Seconds of post-encode work still owed once the last frame lands."""
    try:
        t = max(0.0, float(total_frames or 0))
    except (TypeError, ValueError):
        return TAIL_MIN_SECS
    return max(TAIL_MIN_SECS, (t / 1000.0) * float(secs_per_kframe))


def accept_sample(df, dt, max_fps: float = R_MAX_FPS) -> bool:
    """Is this (frames, seconds) interval real encoding evidence?

    Rejects: a backwards or zero clock, frames going backwards (a restart), a physically
    impossible rate (the resume replay burst), and a long stall (a hold, not slow encoding).

    THE LONG-INTERVAL RULE APPLIES WHATEVER df IS. It used to only fire when df was exactly
    zero, which missed the case that matters most: a lane SUSPENDED for Resolve stops
    reporting progress entirely, and the first callback after it resumes carries a handful of
    frames against the whole suspension — twenty minutes of wall clock for twelve frames.
    That is not slow encoding, it is a hold with an encoding sample stapled to the end of it,
    and one such tick measured a healthy 2.50 fps lane at 1.04 (a 2.4x ETA inflation). Several
    Resolve passes compounded it to the 5x seen live. Any interval this long contains a hold;
    drop it and re-anchor rather than averaging it in."""
    if dt is None or df is None:
        return False
    if dt <= 0 or df < 0:
        return False
    if dt > MAX_IDLE_TICK:
        return False
    if df == 0:
        return True
    return (df / dt) <= max_fps


class RateBook:
    """Per-regime (frames, seconds) accumulators for ONE item.

    rate = sum(frames) / sum(seconds) -- total over total. This specific form is not incidental:
    the quantity being predicted is `remaining_frames * mean_seconds_per_frame`, so the estimator
    must be the FRAMES-weighted mean of spf, equivalently the TIME-weighted mean of fps. Averaging
    fps per-frame instead is optimistic by Jensen -- at the 3x scene-complexity swing x265 shows
    here, that is a permanent 33% underestimate at every tick. `test_eta` pins this.

    Keeping a bucket PER REGIME gets the purity the old re-anchor was after (contended and solo
    rates are never blended) without throwing away an hour of measurement at every flip."""

    def __init__(self):
        self._b: dict[str, list[float]] = {}

    def tick(self, regime: str, df, dt) -> bool:
        """Bank one interval. Returns False if the sample was rejected (see accept_sample)."""
        if not accept_sample(df, dt):
            return False
        f, s = self._b.get(regime, (0.0, 0.0))
        self._b[regime] = [f + df, s + dt]
        return True

    def totals(self, regime: str):
        f, s = self._b.get(regime, (0.0, 0.0))
        return float(f), float(s)

    def rate(self, regime: str, min_frames: int = MIN_RATE_FRAMES,
             min_seconds: float = MIN_RATE_SECONDS):
        """frames/sec for a regime, or None until it has seen enough to be worth trusting."""
        f, s = self.totals(regime)
        if s <= 0 or f < min_frames or s < min_seconds:
            return None
        return f / s

    def k_observed(self):
        """This item's OWN solo/contended ratio, once both regimes are established.

        Preferring this over the persisted value is the highest-value detail in the design: the
        Resolve gate means every remux enters a solo phase before it ends, so an item measures
        its own k before it finishes -- and a same-item ratio cancels the content-complexity
        confound that makes cross-item comparisons noisy."""
        rs = self.rate(SOLO)
        rc = self.rate(CONTENDED)
        if rs is None or rc is None or rc <= 0:
            return None
        return rs / rc


def two_phase_eta(e_self, e_other, k: float):
    """ETA = min(E_self, E_other) + max(0, E_self - E_other) / k   (see the module docstring).

    Degrades to `e_self` -- today's estimate -- whenever the correction cannot be justified:
    no other job running, an unusable k, or a missing input."""
    if e_self is None or e_self <= 0:
        return e_self
    if k is None or k <= 1.0:
        return e_self
    if e_other is None or e_other <= 0:
        return e_self
    return min(e_self, e_other) + max(0.0, e_self - e_other) / k


def clamp_k(k) -> float:
    try:
        return max(K_MIN, min(K_MAX, float(k)))
    except (TypeError, ValueError):
        return K_PRIOR


def shrink_k(samples, prior: float = K_PRIOR, n0: int = K_PRIOR_WEIGHT) -> float:
    """Shrink observed k toward the prior, in LOG space (k is positive and multiplicative).

    There is roughly ONE clean sample per item -- one regime crossing per remux -- so an EWMA on
    a sample an hour with ~11% content noise is far too jumpy. The MEDIAN is used rather than the
    mean because the content confound is not zero-mean per sample."""
    vals = sorted(math.log(float(x)) for x in (samples or []) if x and float(x) > 0)
    if not vals:
        return clamp_k(prior)
    n = len(vals)
    med = vals[n // 2] if n % 2 else 0.5 * (vals[n // 2 - 1] + vals[n // 2])
    return clamp_k(math.exp((n * med + n0 * math.log(prior)) / (n + n0)))


def regime_of(*, run_stage: str | None, run_active: bool,
              other_lane_live: bool = False) -> str:
    """Which regime a FINISHER lane is encoding in right now.

    Keys off `stage_active`, never `stage` alone -- `stage` is stale by design (set at stage
    start, never cleared) so it names a dead stage for the whole 60 s retry after a failure, and
    a power hold keeps it set on purpose. Trusting it would label a solo machine 'contended'.

    THE OTHER REMUX LANE COUNTS AS CONTENTION. Two x265 encodes on one machine roughly halve
    each other; calling that 'solo' banks the halved rate as this lane's best case and then
    predicts no speed-up for when the other lane finishes -- so the estimate stays pessimistic
    for the whole remainder. It is the same two-phase shape the model already exists for, just
    with the other lane playing the part Topaz usually plays.

    THE NATIVE DV ANALYSIS ('dvnative') COUNTS TOO. It is CPU work at nice 10 beside this lane's
    normal-priority x265, exactly where Topaz sits, and when it ends the run thread parks at the
    native gate until this remux finishes: contended now, solo after, the same shape again.
    Banking it as solo would drag this item's own k toward 1. The stage never runs while
    dv_engine is 'resolve', so every existing stage keeps the regime it always had.

    THE NATIVE RENDER ('dvrender') IS THE SAME CASE: its analysis and LUT encode run at nice 10
    beside this lane and never suspend it. It never runs while every dv_routes entry is
    'resolve'."""
    if other_lane_live:
        return CONTENDED
    if run_active and run_stage in ("download", "extend", "topaz", "dvnative", "dvrender"):
        return CONTENDED
    return SOLO


def correction_applies(*, run_stage: str | None, run_active: bool) -> bool:
    """Should the two-phase speed-up be applied at all?

    NO while the run thread is in `resolve`: that stage PREEMPTS the lane entirely
    (`_resolve_active` -> `should_pause`), so the remux's next regime is rate ZERO, not a
    speed-up. Predicting a speed-up there would be wrong in the dangerous direction, so the
    estimate falls back to today's conservative formula.

    'dvnative' and 'dvrender' are NOT that case: neither suspends a lane, so they are ordinary
    contention and the correction applies."""
    return not (run_active and run_stage == "resolve")


# ---- the 'dvnative' stage: native Dolby Vision analysis, timed by CHUNK ----------------------
# nativelane.run_analysis publishes progress ONLY when a Topaz chunk is finished — one callback
# per chunk, minutes apart — and a resume re-fires one callback per chunk it measured before,
# within about a second (dvnative.analyze_chunks just reloads each published sidecar). Fed to a
# plain pct extrapolation, that replay is banked as work and the ETA reads several times too low
# until the stage ends: the failure the Topaz replay guard and accept_sample exist for. The clock
# below times LIVE chunks only.

# A boundary is a replay, not analysis, when it lands this soon after the previous one ...
CHUNK_REPLAY_SECS = 5.0
# ... or at a pace that would analyse the WHOLE item in under this. A real chunk runs three
# processes and writes a ~26 GiB intermediate, so neither is reachable; a replay beats both by
# an order of magnitude. Dropping a genuinely tiny last chunk is the safe error to make.
CHUNK_REPLAY_STAGE_SECS = 60.0


def chunk_is_replay(width, dt) -> bool:
    """Is a boundary that advanced `width` of the stage (0..1) in `dt` seconds a resume replay
    rather than measured analysis? Unusable input counts as a replay: it is never banked."""
    try:
        w, t = float(width), float(dt)
    except (TypeError, ValueError):
        return True
    if not (w > 0 and t > 0) or t < CHUNK_REPLAY_SECS:
        return True
    return w / t > 1.0 / CHUNK_REPLAY_STAGE_SECS


class ChunkClock:
    """The ETA of ONE item's 'dvnative' stage, from its chunk boundaries alone.

    observe() every progress dict's (seg_done, notches); estimate(now) returns the keys
    StageProgress shows — eta_secs, seg_eta_secs, seg_secs — or {} while there is nothing
    honest to say: before the first LIVE chunk lands, and once the last one has.

    `notches` are the chunk ENDS as fractions of the item's frames, so a chunk's width is its
    share of the frames and rate = banked width / banked seconds is the frames-weighted form
    RateBook uses (the Jensen test pins why). A new attempt on the same clock (seg_done going
    back, after a pause) keeps what was measured: same chunks, same machine. New notches mean a
    different item, and start over. `now` is separate from observe() so a caller can refresh
    the countdown between boundaries, which are minutes apart."""

    def __init__(self):
        self._reset(None)

    def _reset(self, notches):
        self._notches = notches   # this item's chunk ends, 0..1
        self._done = None         # chunks finished at the last boundary seen
        self._at = None           # when that boundary (or the attempt's anchor) was seen
        self._f = 0.0             # banked LIVE work, as a fraction of the stage
        self._s = 0.0             # ... and the seconds it took

    def observe(self, seg_done, notches, now) -> bool:
        """Note one progress callback. True when it banked a live chunk as rate evidence."""
        try:
            done, t = int(seg_done), float(now)
            ends = [float(x) for x in notches]
        except (TypeError, ValueError):
            return False
        if not ends or not 0 <= done <= len(ends) or any(b < a for a, b in zip(ends, ends[1:])):
            return False
        if ends != self._notches:
            self._reset(ends)
        if self._done is None or done < self._done:     # first sight, or a new attempt
            self._done, self._at = done, t
            return False
        if done == self._done:                           # the same boundary, re-published
            return False
        width = ends[done - 1] - (ends[self._done - 1] if self._done else 0.0)
        dt = t - self._at
        self._done, self._at = done, t                   # replay or not, the next chunk starts now
        if chunk_is_replay(width, dt):
            return False
        self._f += width
        self._s += dt
        return True

    def rate(self):
        """Stage fraction per second over the live chunks, or None before the first one."""
        return self._f / self._s if self._f > 0 and self._s > 0 else None

    def estimate(self, now) -> dict:
        r, ends, done = self.rate(), self._notches, self._done
        if r is None or ends is None or done is None or done >= len(ends):
            return {}
        try:
            spent = max(0.0, float(now) - self._at)
        except (TypeError, ValueError):
            spent = 0.0
        seg = (ends[done] - (ends[done - 1] if done else 0.0)) / r   # this chunk's projected total
        # An overrunning chunk holds at the floor rather than reading 0 while it still runs.
        seg_left = max(TAIL_MIN_SECS, seg - spent)
        return {"eta_secs": round(seg_left + max(0.0, ends[-1] - ends[done]) / r, 1),
                "seg_eta_secs": round(seg_left, 1), "seg_secs": round(seg, 1)}


# ---- the 'dvrender' stage: a native Dolby Vision render, timed by PHASE ---------------------
# nativerender.render reports three phases on one 0-100 bar, and they run at unrelated speeds:
#   analysis   one callback per finished analysis RANGE, minutes apart; a resume re-fires one
#              per range measured before, within about a second (the 'dvnative' replay again)
#   encode     ffmpeg's own -progress, every poll: a steady stream. A re-encode (the peak loop)
#              starts again from the phase's start
#   finish     four short steps (ES copy, RPU inject, mux, self-check), all I/O over the encode
# A plain pct extrapolation would time the analysis' rate against the encode's half of the bar,
# so each phase is timed on its own (live work only) and the ETA adds up what is left of each.
RENDER_SPANS = {"analysis": (0.0, 50.0), "encode": (50.0, 95.0),    # = nativerender.PHASE_SPAN
                "finish": (95.0, 100.0)}                             # (test-pinned)
# The encode's rate before this item's own encode has been timed: a deliberately modest guess
# for a VideoToolbox 4K Main10 encode behind the software LUT, unmeasured on real sources (the
# route_cpu tool's encode probe will measure it). It only shapes the analysis-phase ETA, and the
# item's own encode rate replaces it once MIN_RENDER_ENCODE_SECONDS of it have been seen.
RENDER_ENCODE_PRIOR_FPS = 30.0
MIN_RENDER_ENCODE_SECONDS = 10.0
RENDER_FINISH_SECS_PER_KFRAME = 1.0   # the finish steps read and write the encode a few times


def render_finish_secs(frames) -> float:
    """Seconds the finish phase takes for `frames` (never under TAIL_MIN_SECS)."""
    try:
        f = max(0.0, float(frames or 0))
    except (TypeError, ValueError):
        f = 0.0
    return max(TAIL_MIN_SECS, f / 1000.0 * RENDER_FINISH_SECS_PER_KFRAME)


class RenderClock:
    """The ETA of ONE item's 'dvrender' stage. observe() every progress dict (its phase, pct,
    attempt and the item's `frames`); estimate(now) returns {"eta_secs"} — or {} while there is
    nothing honest to say: before the first LIVE analysis range lands.

    Rates are kept per phase as banked (fraction of the phase, seconds), the frames-weighted form
    RateBook uses. An analysis boundary is banked only when it is not a resume replay
    (chunk_is_replay, measured against the analysis phase's width); an encode tick only when it is
    not a hold (MAX_IDLE_TICK). A new attempt — the phase's fraction going back, or a new encode
    attempt — re-anchors and keeps what was measured: same item, same machine. A different
    `frames` means a different item, and starts over."""

    def __init__(self):
        self._reset(None)

    def _reset(self, frames):
        self._frames = frames     # the item's frame count (its identity here)
        self._phase = None        # the last observation: phase, its 0..1 fraction, attempt, time
        self._frac = 0.0
        self._attempt = None
        self._at = None
        self._af = self._as = 0.0   # banked LIVE analysis: fraction of the phase, seconds
        self._ef = self._es = 0.0   # banked encode

    def observe(self, info, now) -> bool:
        """Note one progress dict. True when it banked live work as rate evidence."""
        try:
            phase = info.get("phase")
            lo, hi = RENDER_SPANS[phase]
            frac = min(1.0, max(0.0, (float(info.get("pct")) - lo) / (hi - lo)))
            t = float(now)
        except (AttributeError, KeyError, TypeError, ValueError):
            return False
        frames = info.get("frames")
        if frames != self._frames:
            self._reset(frames)
        attempt = info.get("attempt")
        if (phase != self._phase or self._at is None or frac < self._frac
                or (phase == "encode" and attempt != self._attempt)):
            self._phase, self._frac, self._attempt, self._at = phase, frac, attempt, t
            return False
        df, dt = frac - self._frac, t - self._at
        self._frac, self._at = frac, t
        if df <= 0 or dt <= 0:
            return False
        if phase == "analysis":
            if chunk_is_replay(df, dt):
                return False
            self._af += df
            self._as += dt
            return True
        if phase == "encode" and dt <= MAX_IDLE_TICK:
            self._ef += df
            self._es += dt
            return True
        return False

    def _encode_rate(self):
        """Fraction of one encode per second: this item's own once timed, else the prior."""
        if self._ef > 0 and self._es >= MIN_RENDER_ENCODE_SECONDS:
            return self._ef / self._es
        try:
            frames = float(self._frames)
        except (TypeError, ValueError):
            return None
        return RENDER_ENCODE_PRIOR_FPS / frames if frames > 0 else None

    def estimate(self, now) -> dict:
        ph = self._phase
        if ph is None or self._at is None:
            return {}
        try:
            spent = max(0.0, float(now) - self._at)
        except (TypeError, ValueError):
            spent = 0.0
        finish = render_finish_secs(self._frames)
        if ph == "finish":
            return {"eta_secs": round(max(TAIL_MIN_SECS, finish * (1.0 - self._frac) - spent), 1)}
        enc = self._encode_rate()
        if enc is None:
            return {}
        if ph == "encode":
            left = max(TAIL_MIN_SECS, (1.0 - self._frac) / enc - spent)
            return {"eta_secs": round(left + finish, 1)}
        if not (self._af > 0 and self._as > 0):
            return {}
        # An overrunning range holds at the floor rather than reading 0 while it still runs.
        left = max(TAIL_MIN_SECS, (1.0 - self._frac) / (self._af / self._as) - spent)
        return {"eta_secs": round(left + 1.0 / enc + finish, 1)}
