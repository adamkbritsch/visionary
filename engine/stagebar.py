"""The lettered stages inside one pipeline step (the app's "Stage A · Measuring every shot").

A step whose work runs as a sequence of distinct jobs — the Dolby Vision step measures, then
encodes, then finishes; a fast remux copies, injects, muxes, verifies — shows each job as a STAGE
with its own bar, lettered A, B, C in the order they run, and the step card's hover view lays
every stage out side by side (finished ones full, the current one filling, the rest empty).

These fields are DISPLAY ONLY. They ride along on the progress dicts the steps already publish,
and never replace the step's own `pct` / `notches` / `seg_done`: those stay the whole step's
numbers, which the ETA clocks, the header readout and the Dock icon read.

    stages          [str]   every stage of this step, in order (the app letters them A, B, C)
    stage_idx       int     the stage running now (0-based)
    stage_pct       float   that stage's own 0..100; absent = a stage with no honest number
                            (the app then shows its label alone, never an empty bar)
    stage_notches   [float] that stage's segment boundaries as 0..1 fractions of the stage
    stage_seg_done  int     how many of those segments are finished
    stage_sub       str     what the stage is doing right now ("attempt 2", "muxing")
"""
from __future__ import annotations

FIELDS = ("stages", "stage_idx", "stage_pct", "stage_notches", "stage_seg_done", "stage_sub")


def fields(stages, idx: int, pct=None, *, notches=None, seg_done=None, sub=None) -> dict:
    """PURE. The stage fields for a progress dict: stage `idx` of `stages`, at `pct` of its own
    work (None = no number), with its own segment `notches` / `seg_done` and a `sub` line."""
    d = {"stages": list(stages), "stage_idx": int(idx)}
    if pct is not None:
        d["stage_pct"] = round(max(0.0, min(100.0, float(pct))), 1)
    if notches:
        d["stage_notches"] = [round(float(n), 4) for n in notches]
    if seg_done is not None:
        d["stage_seg_done"] = int(seg_done)
    if sub:
        d["stage_sub"] = str(sub)
    return d


class Steps:
    """The stages of a step whose plan is only known as it runs (the fast remuxes): `lead` is
    what the caller knows will run first, plan() announces the rest once the callee knows them,
    and at() reports a stage by its label. A label not in the plan is inserted after the stage
    running now, so the letter of a stage already shown never changes."""

    def __init__(self, lead=()):
        self.labels = list(lead)
        self.idx = None

    def plan(self, labels) -> None:
        """The stages still to come after the current one (replacing any announced before)."""
        keep = self.labels[:self.idx + 1] if self.idx is not None else []
        self.labels = keep + [s for s in labels if s not in keep]

    def at(self, label: str, pct=None, **kw) -> dict:
        """Stage `label` is running (at `pct` of its own work): its fields() dict."""
        start = self.idx or 0
        try:
            self.idx = self.labels.index(label, start)
        except ValueError:
            pos = start + 1 if self.idx is not None else 0
            self.labels.insert(pos, label)
            self.idx = pos
        return fields(self.labels, self.idx, pct, **kw)

    @property
    def active(self) -> bool:
        return self.idx is not None

