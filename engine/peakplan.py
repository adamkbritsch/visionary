"""The peak plan: what bitrate to render again at when a VideoToolbox render's 1-s peak is over
the cap. Pure arithmetic, no Resolve.

Moved here verbatim from resolve_pipeline.py (which imports it back, unchanged in behaviour)
so the native render (nativerender.py) can use the same rules without importing a module
that pins and launches Resolve at import time.

THE YOUTUBE GAMBLE, WITH A SECOND THROW. A YouTube render targets a cap-safe bitrate so the
remux can SHIP it stream-copied (stages.YOUTUBE_RENDER_KBPS). VideoToolbox has no peak
control, so the 1-second peak is a gamble on the burst ratio, and a lost gamble used to cost
the whole hour-class x265 re-encode the fast path exists to avoid. User-dictated 2026-09-14:
"instead of remuxing them the slow way, re-export them at a lower bitrate you mathematically
calculate would work." So the peak is measured while the render's inputs are still at hand,
and an over-cap render is simply rendered again at a target scaled by the burst ratio it just
measured — minutes, no analysis, no x265.
"""
from __future__ import annotations

REEXPORT_MARGIN = 0.9            # the SAFETY FACTOR on the gate (cap * dvcap.PEAK_TOLERANCE),
                                 # not on the bare cap: the burst ratio shifts a little between
                                 # renders, so aim just inside the bar rather than at it. Reading
                                 # this as 0.9 * cap is what mis-diagnosed the floor for two weeks
REEXPORT_FLOOR_KBPS = 8000       # under this the picture loses more than the hour saves:
                                 # the remux's capped re-encode (it caps only the PEAKS) takes it
MAX_REEXPORTS = 2                # a third miss means the ratio is not stable — stop guessing


def reexport_plan(kbps, peak_mbps, cap_mbps, *, margin=REEXPORT_MARGIN,
                  floor=REEXPORT_FLOOR_KBPS, tolerance=None, prev=None):
    """PURE. `(target, why_not)` — the target to re-export at, or None and WHICH no it is.

    The three noes are different problems and a census that cannot tell them apart cannot tell a
    floor problem from a content one. That distinction is exactly what found the aim-point bug:
    all three declines in the log said "under the floor", none said "out of attempts", which is
    what pointed at the arithmetic rather than at the retry budget."""
    import dvcap
    if tolerance is None:
        tolerance = dvcap.PEAK_TOLERANCE
    try:
        kbps, peak, cap = float(kbps), float(peak_mbps), float(cap_mbps)
    except (TypeError, ValueError):
        return None, "unreadable"
    if kbps <= 0 or peak <= 0 or cap <= 0:
        return None, "unreadable"
    gate = cap * float(tolerance) * margin
    want = None
    if prev:
        try:
            t0, p0 = float(prev[0]), float(prev[1])
        except (TypeError, ValueError, IndexError, KeyError):
            t0 = p0 = 0.0
        slope = ((peak - p0) / (kbps - t0)) if (t0 > 0 and p0 > 0 and kbps != t0) else 0.0
        if slope > 0:
            base = peak - slope * kbps          # what the content costs at any bitrate
            # Judged against the AIM, not the bare gate, on purpose: a target that only clears
            # 57.5 with no cushion left is a coin flip, and losing it costs a whole 4K export.
            if base >= gate:
                return None, "content-floor"    # no target reaches the aim — stop guessing
            want = int((gate - base) / slope)
    if want is None:
        want = int(kbps * gate / peak)
    if want >= kbps:
        return None, "not-over"                 # nothing to lower
    return (want, "") if want >= floor else (None, "under-floor")


def reexport_kbps(kbps, peak_mbps, cap_mbps, *, margin=REEXPORT_MARGIN,
                  floor=REEXPORT_FLOOR_KBPS, tolerance=None, prev=None):
    """PURE. The target to re-export at so the peak clears the gate, or None. The target half
    of reexport_plan, which is where the reasoning (and the reason for a None) lives.

    AIM AT THE GATE THAT DECIDES. The render is accepted by dvcap.peak_ok, which passes a peak
    up to cap * PEAK_TOLERANCE — 57.5 Mbps at the 50 cap, the same bar every TV master clears,
    and the same function the remux re-applies to the shipped bits. Aiming at the bare cap threw
    away that 15% before the safety margin even applied, so a high-burst video computed a target
    under the floor and lost the whole fast path: live 2026-09-26, 14000 kb/s peaked at 79.7, the
    old aim asked for 7905 (declined, then 4 h 36 m of x265), the gate-based aim asks for 9090.

    ONE MEASUREMENT: assume the peak scales with the target. TWO: fit the line through them,
    because it does not — live 2026-09-25, 14000 kb/s peaked at 75.4 and its 8358 kb/s re-export
    still peaked at 61.7, a burst ratio that GREW as the target fell."""
    return reexport_plan(kbps, peak_mbps, cap_mbps, margin=margin, floor=floor,
                         tolerance=tolerance, prev=prev)[0]
