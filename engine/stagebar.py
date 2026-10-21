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
    stage_about     [str]   one sentence per stage on what it is (ABOUT) — the app's tooltip
"""
from __future__ import annotations

FIELDS = ("stages", "stage_idx", "stage_pct", "stage_notches", "stage_seg_done", "stage_sub",
          "stage_about")

# What each stage IS, in a sentence — the tooltip on its section of the bar (user, 2026-10-20).
# Keyed by the exact label the step publishes; test_stagebar checks every label has one.
ABOUT = {
    # the Dolby Vision step (nativelane.LANE_STAGES, nativerender.render_stages)
    "Measuring every shot": "hdr-analyze measures how bright every shot of the picture is; the "
                            "Dolby Vision metadata is written from these measurements.",
    "Writing the Dolby Vision metadata": "dovi_tool turns the shot measurements into the Dolby "
                                         "Vision 8.1 metadata that goes into the master.",
    "Encoding the picture": "The picture is encoded at 4K on the Mac's media engine. A re-encode "
                            "under the peak cap starts this bar over.",
    "Copying the source's picture": "The source's own video is copied as it is, with no "
                                    "re-encode, so the Dolby Vision metadata can be added to it.",
    "Finishing the render": "The HDR10 and Dolby Vision metadata are added, the render is "
                            "packaged, and it is checked before it is handed on.",
    # download (stages.DOWNLOAD_STAGES, COMBINE_STAGES)
    "Copying the source from the NAS": "The episode or movie is copied from your NAS to the "
                                       "Mac's scratch disk.",
    "Making the constant-frame-rate copy": "A copy at one steady frame rate is made, so every "
                                           "later step counts exactly the same frames.",
    "Copying the NAS copy": "This movie's copy on your NAS is copied to the scratch disk.",
    "Fetching the companion from the seedbox": "The movie's second copy is streamed from the "
                                               "seedbox, so the best of both can be combined.",
    # the fast remuxes (stages.PEAKS_STAGE, PLAN_STAGE, CAPPED_STAGE; remux.inject_stages,
    # SHIP_STAGES, MUX_STAGES)
    "measuring source peaks": "The source's busiest moments are measured to decide whether its "
                              "own video can ship as is or must be re-encoded under the cap.",
    "planning scene-cut segments": "The video is split at scene cuts into segments that are "
                                   "encoded, and resumed, one at a time.",
    "re-encoding under the peak cap": "x265 re-encodes the video under a hard bitrate cap so it "
                                      "plays without stutter, keeping the Dolby Vision metadata.",
    "extracting DV metadata": "The Dolby Vision metadata is read out of the copy that carries it.",
    "copying the original video": "The original video is copied out bit for bit, with no "
                                  "re-encode.",
    "injecting DV metadata": "The Dolby Vision metadata is added to the original video, frame "
                             "by frame.",
    "converting DV to profile 8.1": "The video's Dolby Vision is converted to profile 8.1, which "
                                    "every player supports, with no re-encode.",
    "preparing audio": "The audio is prepared with its loudness adjustment, and the subtitles "
                       "and chapters are gathered.",
    "muxing the master": "The video, audio and subtitles are combined into the final MKV master.",
    "verifying the master": "The master is checked: Dolby Vision 8.1, every frame present, and "
                            "the bitrate under the cap.",
    "measuring the render's peaks": "The render's busiest moments are measured to see whether "
                                    "it can ship as is, with no re-encode.",
    "copying the render's video": "The render's video is copied out as it is, with no re-encode.",
    # the over-gate splice (remux.splice_stages)
    "indexing the original video": "Every frame of the original video is listed with its place "
                                   "on screen and the points where it can be cut cleanly.",
    "re-encoding the busiest stretches": "Only the seconds over the bitrate limit, and a moment "
                                         "either side, are re-encoded under the cap; the rest of "
                                         "the video stays the original.",
    "splicing them into the original": "The re-encoded stretches are put into the original video "
                                       "in place of the frames they replace.",
    "checking every splice": "Each join is decoded and compared frame by frame with the original "
                             "and the re-encoded stretch, and every frame is checked to carry its "
                             "own Dolby Vision metadata.",
}


def fields(stages, idx: int, pct=None, *, notches=None, seg_done=None, sub=None) -> dict:
    """PURE. The stage fields for a progress dict: stage `idx` of `stages`, at `pct` of its own
    work (None = no number), with its own segment `notches` / `seg_done` and a `sub` line."""
    d = {"stages": list(stages), "stage_idx": int(idx),
         "stage_about": [ABOUT.get(s, "") for s in stages]}
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

