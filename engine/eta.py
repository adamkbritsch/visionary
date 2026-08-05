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
    with the other lane playing the part Topaz usually plays."""
    if other_lane_live:
        return CONTENDED
    if run_active and run_stage in ("download", "topaz"):
        return CONTENDED
    return SOLO


def correction_applies(*, run_stage: str | None, run_active: bool) -> bool:
    """Should the two-phase speed-up be applied at all?

    NO while the run thread is in `resolve`: that stage PREEMPTS the lane entirely
    (`_resolve_active` -> `should_pause`), so the remux's next regime is rate ZERO, not a
    speed-up. Predicting a speed-up there would be wrong in the dangerous direction, so the
    estimate falls back to today's conservative formula."""
    return not (run_active and run_stage == "resolve")
