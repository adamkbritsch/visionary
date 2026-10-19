"""The 'dvrender' engine: write the item's Dolby Vision render, `p.dv_render`.

WHY: everything after the DV step reads ONE file, `p.dv_render` — HEVC Main10 PQ with an in-band
Dolby Vision 8.1 RPU and the HDR10 static metadata, the whole source, at the source's frame rate.
stage_done('dv'), remux(), remux_ship_render(), remux_inject(), combine(), the chunk drop at
hand-off and the disk accounting all key on that file, and this module writes it for every route
the chunk lane (nativelane) does not take.

HOW, render(p, spec):
  1. analyse  dvnative.analyze_ranges over the source in ranges (one file), or
              dvnative.analyze_chunks over Topaz's chunks, through the same front end and LUT
              the picture gets; then dvnative.generate_rpu with the spec's MasterSpec. The RPU
              must cover exactly the spec's frames. A range is the scene plan's segment, CAPPED
              at ANALYSIS_RANGE_MAX_FRAMES (RenderSpec.analysis_plan): a long one is split at a
              strong scene cut when the scan found one, else evenly, and an even split is
              joined back into one shot in the RPU (dvnative.merge_sidecars' `joins`)
  2. encode   ffmpeg: pre_vf + lut_vf (the validated TV chain) -> hevc_videotoolbox Main10
              p010le at the attempt's bitrate, tagged BT.2020 / PQ, every frame once
              (-fps_mode passthrough), straight to an Annex B ES (no container to copy out of)
  3. finish   hdr10sei puts the HDR10 mastering-display + content-light-level SEI into every
              keyframe, exactly as the x265 path writes them -> dovi_tool inject-rpu -> MP4Box
              `:dvp=8.1:fps=` into <dv_render>.part: the file that ships, video for video
  4. peak     dvcap.video_peak_1s_mbps OF THAT FILE (RPU and SEI bytes included — the very
              number remux_ship_render re-measures) against cap x 1.15 (dvcap.peak_ok). Over it,
              peakplan's loop encodes and finishes again at a fitted target, at most
              MAX_REEXPORTS more times, keeping the FIRST finished render as the best and
              putting it back when the gamble is lost, so the remux's x265 fallback starts
              from the best render, never a half-bitrate one
  5. publish  the kept render passes the orchestrator's own gates first (_is_dv81,
              render_is_complete, _fps_matches, an exact frame count, the HDR10 metadata), THEN
              it is renamed to p.dv_render — last and atomic, so a half-made render never sits
              where stage_done looks

HDR INTAKE (2026-10-18): an HDR10 source keeps its own mastering — RenderSpec.master carries its
mastering display and RenderSpec.light its content light level, declared as they are in L6 and in
the SEI the finish writes (dvnative.source_mastering) — and is measured with no LUT (HLG through
dvnative.hlg_to_pq_vf first). A keep_video spec (rpu_only) skips steps 2 and 4 altogether: the
render is the source's own coded video, copied out as Annex B, with the RPU injected and the DV
mux (step 3 without the SEI, which the source carries already) — exactly what the remux's inject
path ships, so the original picture is never re-encoded. A source whose light level says 0,0
("unknown") gets light None from its route (stages.declared_light): the level is measured, as
the reference masters do, and step 3 writes it into the SEI too, so L6 and the HDR10 SEI always
agree.

Every child runs through dvnative.run_step at nice 10 in its own process group: abort and
should_pause kill it within a second ("aborted (run stopped)" / "paused: ..."), a wedged one
times out. Analysis resumes range by range; an encode attempt restarts from scratch. Every
step that writes something big asks for room first (the native lane's 150 GiB floor): the
analysis per range, each encode for its estimated size plus its finish, the finish for its
transients — "paused: low disk ..." (a hold, never a failure), or a plain failure when even an
empty volume could not hold it.

SDR OUTPUT (2026-10-18, route sdr_out: an item PINNED to SDR): a `sdr` spec writes the SDR
render — HEVC Main10 BT.709 limited, NO RPU and no HDR10 SEI — so step 1 is
skipped (nothing to measure), step 2 tags BT.709, step 3 is the plain mux (no SEI, no inject, no
DV box), and steps 4-5 are the same peak loop and publish against an SDR self-check. Its proof is
books/sdr.json (the published render's size and mtime, written just before the rename): the
orchestrator's stage_done and the remux accept an SDR render ONLY when it vouches for it
(sdr_render_ok), so nothing that was accepted before changes.

THE BOOKS live in <dv_render>.dvrender/ — never in segdir/native/, so nativelane's marker logic
(_native_claimed) stays false and the hand-off code drops the item's chunks like any rendered
item's:
  spec.json        what the analysis and the encode were made from (a change clears them)
  analysis/        per-range (or per-chunk) sidecars and L4: the analysis' resume state
  sidecar.json     the merged sidecar; rpu.bin (+ .generate.json) the RPU generated from it
  encode.hevc      the attempt's VideoToolbox ES (deleted once the RPU is in)
  render.best.mov  the FIRST finished render, while a re-encode gambles
  render.mov       the finished render the loop kept, until it is published
  encode.json      every attempt's target and shipped peak, and the render that was kept
  cpu.json         os.wait4 CPU per analysis unit and per encode attempt
  sdr.json         an SDR render's proof (sdr specs only): its size and mtime, as published
The finish step's transients (<dv_render>.dv.hevc, .part) sit beside the render, where remux
keeps its own, so MP4Box's path parsing sees the scratch directory, not a show name.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from fractions import Fraction

import dvcap
import dvnative
import logbook
import nativelane
import peakplan
import remux

FFMPEG = dvnative.FFMPEG
FFPROBE = dvcap.FFPROBE
DOVI_TOOL = dvnative.DOVI_TOOL
MP4BOX = remux.MP4BOX
GiB = 1024 ** 3

BOOKS_SUFFIX = ".dvrender"
SPEC_BOOK = "spec.json"
ANALYSIS_DIR = "analysis"
RPU = "rpu.bin"
SIDECAR = "sidecar.json"
ENCODE = "encode.hevc"
SEI_TMP = "encode.sei.hevc"
BEST = "render.best.mov"
KEPT = "render.mov"
ENCODE_BOOK = "encode.json"
PROGRESS = "encode.progress"
CONCAT = "inputs.ffconcat"
CPU_LEDGER = "cpu.json"
SDR_PROOF = "sdr.json"
PART, ES_DV = ".part", ".dv.hevc"                       # suffixes on p.dv_render
SPEC_VERSION = 2                # 2: capped ranges + joins, Annex B encode, HDR10 SEI, shipped-file peak

ANALYSIS_RANGE_MAX_FRAMES = 3600
                                # longest analysis range (2026-10-15, review-caught: the scene plan
                                # has no cap — a quiet 135,050-frame stretch planned as ONE segment
                                # would need ~840 GiB of intermediate and wait at the disk floor
                                # forever). 3600 x nativelane.INTERMEDIATE_BYTES_PER_FRAME (6.5 MiB)
                                # is ~23 GiB, two in flight ~46 GiB: 150 s at 24 fps, 60 s at 60
SPLIT_AT_CUT_FROM = 0.5         # a long range is split at a strong scene cut in the back half of the
                                # cap's window (never a runt); none there -> an even split, joined
ENCODE_BASE_SECONDS = 600       # encode timeout = this + frames / ENCODE_FRAMES_PER_SECOND:
ENCODE_FRAMES_PER_SECOND = 2    # far below any healthy 4K LUT encode, so only a wedged one trips
ENCODE_SIZE_MARGIN = 1.25       # an encode's size guessed from its bitrate: VideoToolbox runs over
                                # its average target a little, and the guess only gates the start
FINISH_HEADROOM = 1.10          # the finish's transient need, x the encode: at any moment one more
                                # file of the encode's size (the SEI copy, the injected ES, the mux)
DISK_FLOOR_GB = nativelane.MID_ANALYSIS_FLOOR_GB    # the native lane's floor, for every big write
NICE = dvnative.NICE
PHASE_SPAN = {"analysis": (0, 50), "encode": (50, 95), "finish": (95, 100)}   # stage pct per phase
_FRAME_RE = re.compile(rb"frame=\s*(\d+)")


# ---- the spec ----------------------------------------------------------------------------

@dataclass(frozen=True)
class RenderSpec:
    """What to render. `inputs` is [[first_frame, path], ..., [total, None]] — Topaz's chunks, or
    one file as [[0, src], [total, None]] — the same shape as remux's native bundle. `bounds`
    (one file only) are the scene plan's segment END frames (stages' bounds book), and `cuts`
    the scene scan's strong cut frames (the plan's own input, scenes.json): together they say
    where the file may be split for analysis without a seam (analysis_plan). `lut` None = an
    already-PQ picture; `pre_vf` a front end (dvnative.front_end); `cap_mbps` 0 = no peak gate
    (a route whose remux re-encodes anyway). The analysis knobs default to the TV lane's.

    HDR intake (2026-10-18): `light` (MaxCLL, MaxFALL) is declared exactly as given in the RPU's
    L6 and in the HDR10 SEI the render carries — an HDR source's own content light level
    (dvnative.source_mastering, with `master` keeping its mastering display) — instead of the
    measured, clamped values; None (the SDR routes) keeps the measured ones. `keep_video` makes
    the render out of the SOURCE'S OWN coded video (rpu_only): no encode, no SEI written (the
    source's own HDR10 SEI rides along; with light None the measured level is written into it,
    as_source), no peak gate — its Annex B stream with the generated RPU
    injected, muxed as DV 8.1. It needs one file already PQ, no front end and cap_mbps 0.

    SDR output (2026-10-18): `sdr` renders the SDR master's picture instead — no LUT, no RPU, no
    HDR10 metadata: the front end (BT.709 limited) straight into a BT.709-tagged encode. Nothing
    is analysed, so the analysis knobs, `light`, `master` and keep_video do not apply."""
    inputs: tuple
    fps: str
    kbps: int
    cap_mbps: int = dvcap.DEFAULT_PEAK_MBPS
    lut: str | None = None
    pre_vf: str | None = None
    bounds: tuple | None = None
    cuts: tuple | None = None
    master: dvnative.MasterSpec = dvnative.DEFAULT_MASTER
    avg_floor: str = nativelane.AVG_FLOOR
    with_l4: bool = nativelane.WITH_L4
    peak_estimator: str = nativelane.PEAK_ESTIMATOR
    light: tuple | None = None
    keep_video: bool = False
    sdr: bool = False

    def __post_init__(self):
        object.__setattr__(self, "inputs", tuple((int(a), p) for a, p in self.inputs))
        if self.bounds is not None:
            object.__setattr__(self, "bounds", tuple(int(b) for b in self.bounds))
        if self.cuts is not None:
            object.__setattr__(self, "cuts", tuple(sorted({int(c) for c in self.cuts})))
        if self.light is not None:
            object.__setattr__(self, "light", tuple(self.light))

    @property
    def total(self) -> int:
        return int(self.inputs[-1][0]) if self.inputs else 0

    @property
    def single(self) -> bool:
        """One source file (vs Topaz chunks)."""
        return len(self.inputs) == 2

    def problems(self) -> list:
        """Every way this spec cannot be rendered ([] when it can)."""
        out = []
        ins = self.inputs
        if len(ins) < 2 or ins[-1][1] is not None:
            out.append("inputs must end with their [total, None] sentinel")
        elif ins[0][0] != 0 or any(b <= a for (a, _p), (b, _q) in zip(ins, ins[1:])):
            out.append("inputs must start at frame 0 and grow")
        elif not all(isinstance(path, str) and path for _a, path in ins[:-1]):
            out.append("every input but the sentinel needs a path")
        try:
            if Fraction(str(self.fps)) <= 0:
                raise ValueError
        except (ValueError, ZeroDivisionError):
            out.append(f"fps {self.fps!r} is not a frame rate")
        try:
            if int(self.kbps) <= 0 or int(self.cap_mbps) < 0:
                raise ValueError
        except (TypeError, ValueError):
            out.append(f"bitrate {self.kbps!r} kb/s / cap {self.cap_mbps!r} Mbps")
        try:
            if self.lut:
                dvnative.lut_vf(self.lut)
                if not os.path.isfile(self.lut):
                    out.append(f"the LUT is missing at {self.lut}")
            dvnative.front_end(self.pre_vf)
        except ValueError as e:
            out.append(str(e))
        if self.avg_floor not in dvnative.AVG_FLOORS:
            out.append(f"avg_floor {self.avg_floor!r}")
        if (self.bounds is not None or self.cuts is not None) and not self.single:
            out.append("bounds and cuts are for one source file; chunks are their own ranges")
        if self.light is not None:
            try:
                ok = (len(self.light) == 2 and all(isinstance(v, int) and not isinstance(v, bool)
                                                   and 0 <= v <= 65535 for v in self.light))
            except TypeError:
                ok = False
            if not ok:
                out.append(f"light {self.light!r} is not (MaxCLL, MaxFALL) in whole nits")
        if self.keep_video and (not self.single or self.lut or self.pre_vf or int(self.cap_mbps or 0)):
            out.append("keep_video ships one source file's own coded picture: no LUT, no front end, "
                       "no peak gate")
        if self.sdr and (self.lut or self.keep_video or self.light is not None
                         or self.master != dvnative.DEFAULT_MASTER):
            out.append("an SDR render has no LUT, RPU or HDR metadata: no lut, keep_video, light or "
                       "master")
        return out

    def light_text(self) -> str | None:
        """`light` as the "MaxCLL,MaxFALL" string the HDR10 SEI is written from, or None."""
        return f"{self.light[0]},{self.light[1]}" if self.light is not None else None

    def analysis_plan(self) -> tuple:
        """(ranges, joins) for one file: [(a, b), ...] from frame 0, none longer than
        ANALYSIS_RANGE_MAX_FRAMES, and the range starts that are NOT scene cuts. (None, ()) for
        chunks — each is its own unit, and Topaz cut every one of them at a strong cut.

        The scene plan's segments (the bounds) start on strong cuts, but the plan has no upper
        length: a stretch with no strong cut stays one segment however long it runs. Such a
        segment is split until every piece fits — at the LATEST strong cut in the back half of
        the cap's window when the scan found one there (a real shot boundary, so no join), else
        evenly (no runt at the end) with a join, so the shot either side stays one shot in the
        RPU. Without bounds the whole file is one stretch, split the same way."""
        if not self.single:
            return None, ()
        total, cap = self.total, max(1, int(ANALYSIS_RANGE_MAX_FRAMES))
        base = (dvcap.plan_segments(total, self.fps, boundaries=list(self.bounds)) if self.bounds
                else [(0, total)])
        cuts, ranges, joins = list(self.cuts or ()), [], []
        for a, b in base:
            while b - a > cap:
                lo = a + max(1, int(cap * SPLIT_AT_CUT_FROM))
                inside = [c for c in cuts if lo <= c <= a + cap]
                if inside:
                    c = inside[-1]
                else:
                    pieces = -(-(b - a) // cap)
                    c = a + -(-(b - a) // pieces)
                    joins.append(c)
                ranges.append((a, c))
                a = c
            ranges.append((a, b))
        return ranges, tuple(joins)

    def analysis_ranges(self):
        """[(a, b), ...] the one file is measured in (None for chunks: each is its own unit)."""
        return self.analysis_plan()[0]

    def encode_vf(self) -> str:
        """The picture chain: front end, then the TV lane's LUT chain (or, already PQ, just the
        10-bit 4:2:0 the encoder takes)."""
        tail = dvnative.lut_vf(self.lut) if self.lut else "format=yuv420p10le"
        return dvnative.front_end(self.pre_vf) + tail

    def analysis_vf(self) -> str:
        return dvnative.analysis_vf(self.lut, self.pre_vf)


# ---- paths and small books ---------------------------------------------------------------

def books_dir(p) -> str:
    return p.dv_render + BOOKS_SUFFIX


def _book(p, name: str) -> str:
    return os.path.join(books_dir(p), name)


def _read_json(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path: str, data, indent=1) -> None:
    """Atomic: .part, then os.replace — a kill never leaves a half-written book."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".part", "w") as f:
        json.dump(data, f, indent=indent)
    os.replace(path + ".part", path)


def _rm(*paths) -> None:
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def transient_paths(dv_render: str) -> tuple:
    """The finish step's files beside a render at `dv_render` (orchestrator.discard_workfiles
    names them from the bare file name, with no item at hand)."""
    return tuple(dv_render + s for s in (PART, ES_DV))


def _transients(p) -> tuple:
    return transient_paths(p.dv_render)


def discard_books(p) -> None:
    """Delete this item's books and any finish-step strays (cleanup's hook; never the render)."""
    shutil.rmtree(books_dir(p), ignore_errors=True)
    _rm(*_transients(p))


def _clear(p, *, analysis: bool) -> None:
    """Drop the encodes and renders (and with analysis=True everything measured) so nothing
    stale is reused."""
    _rm(*(_book(p, n) for n in (ENCODE, SEI_TMP, BEST, KEPT, ENCODE_BOOK, PROGRESS, CONCAT)))
    if analysis:
        shutil.rmtree(_book(p, ANALYSIS_DIR), ignore_errors=True)
        _rm(*(_book(p, n) for n in (RPU, RPU + ".part", RPU + ".generate.json", SIDECAR,
                                    CPU_LEDGER)))


def record_cpu(p, phase: str, key: str, cpu_s: float, frames: int) -> None:
    """Book one measured unit into cpu.json as {phase: {key: {cpu_s, frames}}}."""
    book = _read_json(_book(p, CPU_LEDGER))
    book = book if isinstance(book, dict) else {}
    rows = book.get(phase) if isinstance(book.get(phase), dict) else {}
    rows[key] = {"cpu_s": round(float(cpu_s or 0.0), 3), "frames": int(frames or 0)}
    book[phase] = rows
    _write_json(_book(p, CPU_LEDGER), book)


# ---- what the books were made from ---------------------------------------------------------

def input_stats(spec: RenderSpec) -> list:
    """[[first, path, size, mtime_ns], ...] of the real inputs. Raises OSError for a missing one."""
    out = []
    for a, path in spec.inputs[:-1]:
        st = os.stat(path)
        out.append([a, os.path.abspath(path), st.st_size, st.st_mtime_ns])
    return out


def fingerprints(spec: RenderSpec, stats: list) -> tuple:
    """(analysis, encode): everything each phase's results depend on. A changed analysis print
    re-measures from scratch; a changed encode print only re-encodes."""
    ranges, joins = spec.analysis_plan()
    lut_sha = nativelane.sha256_cached(spec.lut) if spec.lut else None
    analysis = {
        "version": SPEC_VERSION, "inputs": stats, "total": spec.total, "fps": str(spec.fps),
        "ranges": [list(r) for r in ranges] if ranges is not None else None,
        "joins": list(joins),
        "analysis_vf": spec.analysis_vf(), "lut_sha256": lut_sha,
        "analyzer_sha256": nativelane.sha256_cached(nativelane.analyzer_path()),
        "master": dict(asdict(spec.master), trim_targets=list(spec.master.trim_targets)),
        "avg_floor": spec.avg_floor, "with_l4": spec.with_l4,
        "peak_estimator": spec.peak_estimator,
    }
    encode = {"version": SPEC_VERSION, "inputs": stats, "total": spec.total, "fps": str(spec.fps),
              "encode_vf": spec.encode_vf(), "lut_sha256": lut_sha,
              "kbps": int(spec.kbps), "cap_mbps": int(spec.cap_mbps)}
    # The HDR intake's two knobs are booked only when set, so every SDR route's prints are exactly
    # what they were (books made before 2026-10-18 stay valid). light reaches the RPU (analysis:
    # generate is part of it) and the SEI (encode); keep_video replaces the encode altogether.
    if spec.light is not None:
        analysis["light"] = list(spec.light)
        encode["light"] = list(spec.light)
    if spec.keep_video:
        encode["keep_video"] = True
    if spec.sdr:                         # booked only when set, like the HDR knobs above
        analysis["sdr"] = True
        encode["sdr"] = True
    return analysis, encode


def begin(p, spec: RenderSpec, stats: list) -> dict:
    """Write spec.json for this spec. Results made from other inputs are deleted first (all of
    them when the analysis print changed, only the encode when just the encode print did); with
    the same inputs everything finished is kept and the work resumes."""
    analysis, encode = fingerprints(spec, stats)
    old = _read_json(_book(p, SPEC_BOOK))
    old = old if isinstance(old, dict) else {}
    same_analysis = old.get("analysis") == analysis
    if not same_analysis:
        _clear(p, analysis=True)
    elif old.get("encode") != encode:
        _clear(p, analysis=False)
    state = {"analysis": analysis, "encode": encode}
    if same_analysis:
        for key in ("rpu", "source_frames"):
            if old.get(key) is not None:
                state[key] = old[key]
    _write_json(_book(p, SPEC_BOOK), state)
    return state


# ---- command builders (pure, unit-tested) --------------------------------------------------

def ffconcat_text(paths: list) -> str:
    """PURE. An ffconcat list of `paths`, each single-quoted ('\\'' for a quote inside)."""
    return "ffconcat version 1.0\n" + "".join(
        "file '" + str(path).replace("'", "'\\''") + "'\n" for path in paths)


def build_encode_command(ffmpeg: str, spec: RenderSpec, out: str, kbps: int, *,
                         concat: str | None = None, progress: str | None = None) -> list:
    """ffmpeg: the spec's picture chain -> VideoToolbox HEVC Main10 (p010le) at `kbps`, tagged
    BT.2020 / PQ / limited, video only, written as a raw Annex B ES (`-f hevc`: parameter sets
    in-band on every keyframe — what hdr10sei, dovi_tool and MP4Box all read). One file is read
    directly; chunks through an ffconcat list. `-fps_mode passthrough` encodes every decoded
    frame exactly once, re-stamped at N / fps (dvnative.uniform_pts — the range intermediates'
    guard: a millisecond-timebase source can carry irregular or repeated stamps, and the
    encoder's rate control should see real seconds whatever the source's were); MP4Box sets the
    final timing from the same fps. `-progress` feeds the stage's progress bar. An `sdr` spec is
    tagged BT.709 instead (2026-10-18): the SDR master's colour."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    if progress:
        cmd += ["-progress", progress, "-nostats"]
    if concat:
        cmd += ["-f", "concat", "-safe", "0", "-i", concat]
    else:
        cmd += ["-i", spec.inputs[0][1]]
    return cmd + ["-map", "0:v:0", "-an", "-sn", "-dn",
                  "-vf", dvnative.uniform_pts(spec.fps) + "," + spec.encode_vf(),
                  "-fps_mode", "passthrough",
                  "-c:v", "hevc_videotoolbox", "-profile:v", "main10", "-pix_fmt", "p010le",
                  "-b:v", f"{int(kbps)}k", "-allow_sw", "1",
                  *(("-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709")
                    if spec.sdr else
                    ("-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc")),
                  "-color_range", "tv", "-f", "hevc", out]


def build_sei_command(python: str, src_es: str, out_es: str, master_display: str, max_cll) -> list:
    """`python3 -m hdr10sei SRC DST --mdcv ... --cll A,B` (run with cwd = ENGINE_DIR): the HDR10
    mastering-display and content-light-level SEI into every keyframe of `src_es`, the way the
    x265 path writes them. `max_cll` None (unmeasured) writes 0,0, as x265 does without
    --max-cll."""
    return [python, "-m", "hdr10sei", src_es, out_es, "--mdcv", master_display,
            "--cll", max_cll or "0,0"]


def encode_timeout(frames: int) -> int:
    """PURE. Seconds one encode attempt may take: 600 + frames / 2, rounded up."""
    return ENCODE_BASE_SECONDS + -(-max(0, int(frames or 0)) // ENCODE_FRAMES_PER_SECOND)


def encode_bytes_estimate(kbps, frames, fps) -> int:
    """PURE. The size an encode of `frames` at `kbps` is planned for (ENCODE_SIZE_MARGIN over
    the average target): what the disk gate before an encode reserves."""
    seconds = int(frames or 0) / float(Fraction(str(fps)))
    return int(int(kbps) * 1000 / 8 * seconds * ENCODE_SIZE_MARGIN)


def progress_frames(path: str):
    """The last `frame=N` ffmpeg's -progress wrote to `path`, or None."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            hits = _FRAME_RE.findall(f.read())
    except OSError:
        return None
    return int(hits[-1]) if hits else None


def disk_reason(free_bytes: int, total_bytes: int, need_bytes: int, what: str) -> str | None:
    """PURE. The disk floor for one big write: None when `need_bytes` still leaves DISK_FLOOR_GB
    free, else a "paused: low disk ..." reason (a hold: room comes back as remuxes pass). A need
    that even an EMPTY volume could not meet above the floor raises RuntimeError instead — a
    plain, counted failure, so the fail ladder parks the item rather than holding it forever
    (the unbounded-range livelock, review-caught 2026-10-15)."""
    floor = DISK_FLOOR_GB * GiB
    if total_bytes and need_bytes + floor > total_bytes:
        raise RuntimeError(f"{what} needs ~{need_bytes / GiB:.0f} GiB — more than this volume "
                           f"({total_bytes / GiB:.0f} GiB) can ever hold above the "
                           f"{DISK_FLOOR_GB} GiB floor")
    left = int(free_bytes) - int(need_bytes)
    if left >= floor:
        return None
    return (f"paused: low disk — {what} needs ~{need_bytes / GiB:.0f} GiB and would leave "
            f"{max(0, left) / GiB:.0f} GiB free, under the {DISK_FLOOR_GB} GiB floor")


def _disk(path: str, usage_fn=None):
    """(free, total) bytes of `path`'s volume, or None when it cannot be read (the step itself
    then fails, and is counted — an unreadable volume never pauses)."""
    try:
        u = usage_fn() if usage_fn else shutil.disk_usage(path)
    except OSError:
        return None
    return int(u.free), int(u.total)


def floor_hook(path: str, unit: str, free_fn=None, usage_fn=None):
    """The analysis' before-unit disk check: the native lane's 150 GiB mid-analysis floor against
    live free space on `path`'s volume, for the unit's intermediate (nativelane's bytes per
    frame — `frames` already counts every intermediate still being written beside it), worded
    for `unit`. `free_fn` (free bytes only: the volume's size is then not checked) is the old,
    test-facing form of `usage_fn` (shutil.disk_usage's shape)."""
    def before(k, frames):
        if free_fn is not None:
            try:
                du = (int(free_fn()), 0)
            except OSError:
                return None
        else:
            du = _disk(path, usage_fn)
            if du is None:
                return None
        need = int(frames) * nativelane.INTERMEDIATE_BYTES_PER_FRAME
        return disk_reason(du[0], du[1], need, f"analysis {unit} {k + 1}")
    return before


# ---- the self-check --------------------------------------------------------------------------

class _AtPath:
    """`p` with dv_render pointed at another file (the kept render), for the orchestrator's
    gates."""

    def __init__(self, p, dv_render: str):
        self._p = p
        self.dv_render = dv_render

    def __getattr__(self, name):
        return getattr(self._p, name)


def _sdr_streams_why(streams):
    """None when `streams` (ffprobe's) is an SDR render's video — HEVC, no Dolby Vision record,
    no PQ/HLG transfer — else why not."""
    v = next((s for s in streams or [] if s.get("codec_type") == "video"), None)
    if v is None:
        return "it has no video stream"
    if any("DOVI" in str(sd.get("side_data_type", "")) for sd in v.get("side_data_list") or []):
        return "it carries a Dolby Vision record"
    if v.get("codec_name") != "hevc":
        return f"it is {v.get('codec_name')}, not HEVC"
    if v.get("color_transfer") in ("smpte2084", "arib-std-b67"):
        return f"it is HDR ({v.get('color_transfer')})"
    return None


def self_check(p, path: str, total: int | None = None, hdr: tuple | None = None,
               as_source: bool = False, sdr: bool = False) -> tuple:
    """(ok, why): would the orchestrator accept `path` as this item's render? The exact frame
    count first (when `total` is given: the RPU covers exactly that many), then the same gates
    stage_done('dv') applies — a DV 8.1 configuration record (_is_dv81), the whole source
    and no more (render_is_complete) and the source's frame rate (_fps_matches) — and last the
    HDR10 static metadata every master carries: a mastering display and a content light level
    (dvcap.probe_video's first-frame read). `hdr` = (master_display, "cll,fall") demands those
    exact values (what the finish wrote); without it they need only be there. `as_source` (a
    keep_video render: the source's own coded picture) demands instead EXACTLY the source's own
    HDR10 metadata, whatever it is — none, when the source carries none. `sdr` (an SDR render,
    2026-10-18) demands the opposite of the DV checks: HEVC with NO Dolby Vision record and no
    PQ/HLG transfer — then the same whole-source and frame-rate gates."""
    import orchestrator as O
    if total is not None:
        got = dvcap.count_hevc_frames(path, FFPROBE)
        if got != int(total):
            return False, f"it has {got} frames, the {'source' if sdr else 'RPU'} {total}"
    if sdr:
        why = _sdr_streams_why(O._vstream(path))
        if why:
            return False, why
    elif not O._is_dv81(O._vstream(path)):
        return False, "it carries no Dolby Vision 8.1 configuration record"
    src = O.combine_winner_path(p) or getattr(p, "source_cfr", None)
    if not O.render_is_complete(_AtPath(p, path)):
        return False, (f"it is not the whole source ({O._nb_frames(path)} frames against "
                       f"{O._nb_frames(src)}, or another frame rate)")
    if not O._fps_matches(src, path):
        return False, "its frame rate is not the source's"
    if sdr:
        return True, "an SDR HEVC render (no Dolby Vision), the whole source, at its frame rate"
    info = dvcap.probe_video(path, FFPROBE)
    md, cll = info.get("master_display"), info.get("max_cll")
    if as_source:
        own = dvcap.probe_video(src, FFPROBE)
        if (md, cll) != (own.get("master_display"), own.get("max_cll")):
            return False, (f"its HDR10 metadata says {md} / {cll}, the source's own "
                           f"{own.get('master_display')} / {own.get('max_cll')}")
        return True, ("Dolby Vision 8.1 on the source's own picture and HDR10 metadata, the "
                      "whole source, at its frame rate")
    if not md or not cll:
        return False, "it carries no HDR10 mastering-display / content-light-level metadata"
    if hdr is not None and (md, cll) != (hdr[0], hdr[1] or "0,0"):
        return False, f"its HDR10 metadata says {md} / {cll}, the finish wrote {hdr[0]} / {hdr[1] or '0,0'}"
    return True, "Dolby Vision 8.1 + HDR10 metadata, the whole source, at its frame rate"


# ---- the phases ------------------------------------------------------------------------------

def _checkpoint(abort, should_pause, where: str) -> None:
    if abort is not None and abort.is_set():
        raise dvnative.Aborted(f"aborted (run stopped) before {where}")
    if should_pause is not None and should_pause():
        raise dvnative.Paused(f"paused: yielded before {where}")


def _run(cmd: list, timeout: float, abort, should_pause, what: str, on_poll=None, cwd=None) -> float:
    """One child via dvnative.run_step at NICE -> its CPU seconds. A non-zero exit raises."""
    rc, tail, cpu = dvnative.run_step(dvnative.niced(cmd, NICE), timeout=timeout, abort=abort,
                                      should_pause=should_pause, what=what, on_poll=on_poll, cwd=cwd)
    if rc != 0:
        raise RuntimeError(f"{what} failed (rc={rc}): {tail.strip()[-300:]}")
    return cpu or 0.0


def _report(progress, ep, phase: str, frac: float, **extra) -> None:
    if progress:
        lo, hi = PHASE_SPAN[phase]
        progress(dict({"stage": "dvrender", "ep": ep, "phase": phase,
                       "pct": round(lo + (hi - lo) * max(0.0, min(1.0, frac)), 1)}, **extra))


def _rpu_ready(p, state: dict) -> bool:
    r = state.get("rpu")
    if not isinstance(r, dict) or not os.path.isfile(_book(p, SIDECAR)):
        return False
    try:
        return nativelane.sha256_file(_book(p, RPU)) == r.get("sha256")
    except OSError:
        return False


def _analyse(p, spec: RenderSpec, state: dict, abort, progress, should_pause) -> dict:
    """Phase 1 -> state["rpu"] {sha256, frames, max_cll, shots}. Resumes per range/chunk."""
    ep = getattr(p, "ep", "?")
    if _rpu_ready(p, state):
        return state["rpu"]
    total = spec.total
    work = _book(p, ANALYSIS_DIR)
    ranges, joins = spec.analysis_plan()
    ends = [b for _a, b in ranges] if ranges is not None else [a for a, _p in spec.inputs[1:]]
    unit = "range" if ranges is not None else "chunk"

    def on_done(done, of):
        _report(progress, ep, "analysis", ends[done - 1] / total, seg_done=done, seg_total=of)

    def on_cpu(k, cpu_s, frames):
        record_cpu(p, "analysis", f"{unit}_{k:04d}", cpu_s, frames)

    _report(progress, ep, "analysis", 0.0, seg_done=0, seg_total=len(ends))
    common = dict(cube=spec.lut, pre_vf=spec.pre_vf, master=spec.master,
                  analyzer=nativelane.analyzer_path(), peak_estimator=spec.peak_estimator,
                  with_l4=spec.with_l4, abort=abort, should_pause=should_pause, on_cpu=on_cpu)
    hook = floor_hook(books_dir(p), unit)
    if ranges is not None:
        sc = dvnative.analyze_ranges(spec.inputs[0][1], ranges, work, fps=spec.fps, joins=joins,
                                     on_range=on_done, before_range=hook, **common)
    else:
        sc = dvnative.analyze_chunks([list(c) for c in spec.inputs], work, on_chunk=on_done,
                                     before_chunk=hook, **common)
    rpu = _book(p, RPU)
    # An HDR source's own light level (spec.light) is declared as it stands; otherwise L6 keeps
    # the measured, clamped one — the generate call is then exactly what it always was.
    dvnative.generate_rpu(sc, rpu, avg_floor=spec.avg_floor, l4=spec.with_l4, master=spec.master,
                          **({"light": spec.light} if spec.light is not None else {}))
    got = dvcap.rpu_frame_count(rpu, dovi_tool=DOVI_TOOL)
    if got != total:
        raise RuntimeError(f"the generated RPU has {got} frames, the source {total}")
    _write_json(_book(p, SIDECAR), sc, indent=None)     # per-frame L4: tens of thousands of rows
    state["rpu"] = {"sha256": nativelane.sha256_file(rpu), "frames": got,
                    "max_cll": (spec.light_text() if spec.light is not None
                                else dvnative.x265_max_cll(sc, spec.master)),
                    "shots": len(sc.get("scenes") or [])}
    _write_json(_book(p, SPEC_BOOK), state)
    return state["rpu"]


def hdr10_values(spec: RenderSpec, rpu: dict) -> tuple:
    """(master_display, "cll,fall" | None): the HDR10 static metadata the render carries — the
    master's display (dvcap.FALLBACK_MASTER_DISPLAY for the default 1000-nit master, as the x265
    path writes it) and the light levels measured with the RPU (L6's own, clamped the same), or
    an HDR source's own (spec.light) when the route keeps them."""
    if spec.light is not None:
        return spec.master.hdr10_mdcv, spec.light_text()
    return spec.master.hdr10_mdcv, rpu.get("max_cll")


def as_source(spec: RenderSpec) -> bool:
    """PURE. Does the render carry the SOURCE'S OWN HDR10 SEI — a keep_video render that declares
    the source's own light level? A keep_video render whose light level is measured (the source
    said 0,0) has the measured level written into its SEI, so it is checked like any other."""
    return spec.keep_video and spec.light is not None


def _kept_render(p):
    """The kept render's record from encode.json, when render.mov is still that file."""
    fin = (_read_json(_book(p, ENCODE_BOOK)) or {}).get("final")
    try:
        st = os.stat(_book(p, KEPT))
    except OSError:
        return None
    if isinstance(fin, dict) and (fin.get("size"), fin.get("mtime_ns")) == (st.st_size, st.st_mtime_ns):
        return fin
    return None


def _need_room(p, need: int, what: str) -> None:
    """Raise Paused (or RuntimeError, when it can never fit) unless `need` bytes fit above the
    floor on the scratch volume the books and the render share."""
    du = _disk(books_dir(p))
    if du is None:
        return
    why = disk_reason(du[0], du[1], need, what)
    if why:
        raise dvnative.Paused(why)


def _finish_attempt(p, spec: RenderSpec, hdr: tuple, attempt: int, abort, progress,
                    sei_step: bool = True) -> float:
    """Phase 3 for one encode: books/encode.hevc -> HDR10 SEI (in place) -> RPU inject ->
    MP4Box -> <dv_render>.part, the file that ships. -> CPU seconds. The encode is deleted as
    soon as the RPU is in (its only reader), so at most two encode-sized files exist at once;
    a finish cut short re-encodes on the next pass. `sei_step` False (a keep_video render: the
    source's own stream already carries its HDR10 SEI) skips the SEI and `hdr` is unused. An
    `sdr` spec's finish is the mux alone: no SEI, no RPU, no DV box."""
    ep = getattr(p, "ep", "?")
    part, es_dv = _transients(p)
    enc, sei = _book(p, ENCODE), _book(p, SEI_TMP)
    tmo = dvnative.step_timeout(spec.total)
    _rm(part, es_dv, sei)
    _need_room(p, int(os.path.getsize(enc) * FINISH_HEADROOM), "the DV render's finish")
    cpu = 0.0
    if spec.sdr:
        _report(progress, ep, "finish", 0.6, step="mux", attempt=attempt)
        with remux.mp4box_safe_input(enc) as es_in:
            cpu += _run(remux.build_capped_video_mux_command(MP4BOX, es_in, str(spec.fps), part,
                                                             dv=False), tmo, abort, None, "SDR mux")
        _rm(enc)
        return cpu
    if sei_step:
        _report(progress, ep, "finish", 0.0, step="hdr10", attempt=attempt)
        cpu += _run(build_sei_command(sys.executable, enc, sei, hdr[0], hdr[1]), tmo, abort, None,
                    "HDR10 metadata", cwd=dvnative.ENGINE_DIR)
        os.replace(sei, enc)
    _report(progress, ep, "finish", 0.3, step="inject", attempt=attempt)
    cpu += _run(dvcap.build_inject_command(DOVI_TOOL, enc, _book(p, RPU), es_dv), tmo, abort, None,
                "RPU inject")
    _rm(enc)
    _report(progress, ep, "finish", 0.6, step="mux", attempt=attempt)
    with remux.mp4box_safe_input(es_dv) as es_in:
        cpu += _run(remux.build_capped_video_mux_command(MP4BOX, es_in, str(spec.fps), part), tmo,
                    abort, None, "DV mux")
    _rm(es_dv)
    return cpu


def _encode(p, spec: RenderSpec, rpu: dict, abort, progress, should_pause) -> dict:
    """Phases 2-4 -> the kept render's record {kbps, peak, verdict, attempts, size, mtime_ns},
    the render itself at books/render.mov. Each attempt encodes AND finishes, and the peak is
    measured on the finished file — the bytes remux_ship_render measures again before it ships
    them, RPU and SEI included (an encode measured before the inject read ~0.05 Mbps low, so a
    render just under the gate here could fail it there: review-caught 2026-10-15). verdict:
    "under-cap", "over-cap" (the remux's capped re-encode takes it), "unmeasured" (the remux
    gates it) or "no-cap"."""
    ep = getattr(p, "ep", "?")
    done = _kept_render(p)
    if done:
        return done
    total, cap = spec.total, int(spec.cap_mbps)
    out, best, kept, prog = _book(p, ENCODE), _book(p, BEST), _book(p, KEPT), _book(p, PROGRESS)
    part = _transients(p)[0]
    _rm(out, best, kept, prog, _book(p, SEI_TMP))
    concat = None
    if not spec.single:
        concat = _book(p, CONCAT)
        with open(concat, "w") as f:
            f.write(ffconcat_text([path for _a, path in spec.inputs[:-1]]))
    hdr = hdr10_values(spec, rpu)
    kbps, prev, attempts = int(spec.kbps), None, []
    keep, verdict = part, "no-cap"       # the file that ships at the end (part, or best)

    def keep_best():
        """The first render is the best source the fallback can have: back it goes."""
        nonlocal keep
        if os.path.exists(best):
            _rm(part)
            keep = best

    for attempt in range(peakplan.MAX_REEXPORTS + 1):
        n = attempt + 1
        _checkpoint(abort, should_pause, f"encode attempt {n}")
        _need_room(p, int(encode_bytes_estimate(kbps, total, spec.fps) * (1 + FINISH_HEADROOM)),
                   f"encode attempt {n} and its finish")
        _rm(prog)

        def on_poll(n=n, k=kbps):
            got = progress_frames(prog)
            if got is not None:
                _report(progress, ep, "encode", got / total, attempt=n, kbps=k)

        _report(progress, ep, "encode", 0.0, attempt=n, kbps=kbps)
        cpu = _run(build_encode_command(FFMPEG, spec, out, kbps, concat=concat, progress=prog),
                   encode_timeout(total), abort, should_pause, f"encode attempt {n}",
                   on_poll=on_poll)
        frames = dvcap.count_hevc_frames(out, FFPROBE)
        if frames != total:
            raise RuntimeError(f"the encode has {frames} frames, the source {total}")
        cpu += _finish_attempt(p, spec, hdr, n, abort, progress)
        record_cpu(p, "encode", f"attempt_{n}", cpu, total)
        peak = 0.0
        if cap:
            try:
                peak = dvcap.video_peak_1s_mbps(part, FFPROBE)
            except Exception as e:
                logbook.event(f"dvrender {ep}: RENDER_PEAK unmeasured ({e.__class__.__name__}) — "
                              f"the remux gates it")
        attempts.append({"kbps": kbps, "peak": round(peak, 2), "cpu_s": round(cpu, 1)})
        _write_json(_book(p, ENCODE_BOOK), {"attempts": attempts})
        kept_attempt = attempt
        keep = part
        if not cap:
            break
        if peak <= 0:
            keep_best()                  # unverified here; the remux gates the better file
            verdict = "unmeasured"
            break
        if dvcap.peak_ok(peak, cap):
            logbook.event(f"dvrender {ep}: RENDER_PEAK {peak:.1f} Mbps at {kbps} kb/s — under the "
                          f"{cap} Mbps cap")
            _rm(best)                    # this one ships; the earlier render is dead weight
            verdict = "under-cap"
            break
        nxt, why_not = peakplan.reexport_plan(kbps, peak, cap, prev=prev)
        if nxt is None or attempt >= peakplan.MAX_REEXPORTS:
            why = ("no re-encodes left" if nxt is not None else
                   {"under-floor": f"a fitting target would be under {peakplan.REEXPORT_FLOOR_KBPS} kb/s",
                    "content-floor": "the two encodes fit a line that never reaches the gate",
                    "not-over": "nothing to lower",
                    "unreadable": "the numbers did not read"}.get(why_not, why_not))
            had_best = os.path.exists(best)
            keep_best()
            logbook.event(f"dvrender {ep}: RENDER_OVER_CAP {peak:.1f} Mbps at {kbps} kb/s > {cap} "
                          f"cap — {why}; the remux's capped re-encode takes it"
                          f"{' (from the first render, not the re-encode)' if had_best else ''}")
            verdict = "over-cap"
            break
        logbook.event(f"dvrender {ep}: RENDER_REEXPORT {peak:.1f} Mbps at {kbps} kb/s > {cap} cap — "
                      f"re-encoding at {nxt} kb/s{' (fitted from two encodes)' if prev else ''}")
        if os.path.exists(best):
            _rm(part)                    # a later re-encode is never the best one
        else:
            os.replace(part, best)       # keep the first
        prev, kbps = (kbps, peak), nxt
    if keep == best:
        kept_attempt = 0
    os.replace(keep, kept)
    _rm(best, prog, part)
    st = os.stat(kept)
    final = dict(attempts[kept_attempt], verdict=verdict, attempts=len(attempts),
                 size=st.st_size, mtime_ns=st.st_mtime_ns)
    _write_json(_book(p, ENCODE_BOOK), {"attempts": attempts, "final": final})
    return final


def build_copy_command(ffmpeg: str, src: str, out: str, progress: str | None = None) -> list:
    """PURE. ffmpeg: one file's own coded video, stream-copied to a raw Annex B ES (every
    parameter set in-band — what dovi_tool and MP4Box read): the original bits, untouched.
    dvcap.build_annexb_file_command's copy (the remux inject path's base layer), plus the
    progress feed."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    if progress:
        cmd += ["-progress", progress, "-nostats"]
    return cmd + ["-i", src, "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
                  "-f", "hevc", out]


def _copy_video(p, spec: RenderSpec, rpu: dict, abort, progress) -> dict:
    """Phases 2-4 for a keep_video spec (rpu_only) -> the kept render's record, the render itself
    at books/render.mov: the source's own coded video, copied out as Annex B (no encode, no
    decode), its RPU injected, muxed as DV 8.1 — the very bytes the remux's inject path would
    ship, so there is nothing to gate (the remux measures the SOURCE's peaks before choosing
    that path, as it always has). Room is asked first for two source-sized files: the copy and
    the injected stream exist at once, then the injected stream and the mux."""
    ep = getattr(p, "ep", "?")
    done = _kept_render(p)
    if done:
        return done
    total, src = spec.total, spec.inputs[0][1]
    out, kept, prog = _book(p, ENCODE), _book(p, KEPT), _book(p, PROGRESS)
    part = _transients(p)[0]
    _rm(out, kept, prog, _book(p, SEI_TMP), _book(p, BEST))
    _checkpoint(abort, None, "the stream copy")
    _need_room(p, int(os.path.getsize(src) * (1 + FINISH_HEADROOM)),
               "the source's stream copy and its RPU inject")

    def on_poll():
        got = progress_frames(prog)
        if got is not None:
            _report(progress, ep, "encode", got / total, step="copy")

    _report(progress, ep, "encode", 0.0, step="copy")
    cpu = _run(build_copy_command(FFMPEG, src, out, prog), dvnative.step_timeout(total), abort,
               None, "stream copy", on_poll=on_poll)
    frames = dvcap.count_hevc_frames(out, FFPROBE)
    if frames != total:
        raise RuntimeError(f"the source's stream has {frames} frames, the RPU {total}")
    # the source's own HDR10 SEI rides along when L6 declares the source's own light level; a
    # source that says 0,0 ("unknown") has its level measured, and the SEI then says the same as L6
    cpu += _finish_attempt(p, spec, hdr10_values(spec, rpu), 1, abort, progress,
                           sei_step=spec.light is None)
    record_cpu(p, "encode", "copy", cpu, total)
    os.replace(part, kept)
    _rm(prog)
    st = os.stat(kept)
    final = {"kbps": 0, "peak": 0.0, "cpu_s": round(cpu, 1), "verdict": "source-video",
             "attempts": 0, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    _write_json(_book(p, ENCODE_BOOK), {"attempts": [], "final": final})
    return final


def _publish(p, spec: RenderSpec, rpu: dict, progress) -> str:
    """Phase 5: self-check the kept render, then rename it to p.dv_render. -> why."""
    ep = getattr(p, "ep", "?")
    kept = _book(p, KEPT)
    _report(progress, ep, "finish", 0.9, step="check")
    if as_source(spec):      # the source's own HDR10 SEI, whatever it says (the finish wrote none)
        ok, why = self_check(p, kept, spec.total, as_source=True)
    elif spec.sdr:
        ok, why = self_check(p, kept, spec.total, sdr=True)
    else:
        ok, why = self_check(p, kept, spec.total, hdr=hdr10_values(spec, rpu))
    if not ok:
        _rm(kept)
        raise RuntimeError(f"self-check: {why}")
    if spec.sdr:
        _write_sdr_proof(p, kept)       # BEFORE the rename: the rename keeps size and mtime, so
                                        # a kill between the two leaves a proof of nothing at all
    os.replace(kept, p.dv_render)       # the publish: last, atomic
    _report(progress, ep, "finish", 1.0, step="done")
    return why


def _write_sdr_proof(p, path: str) -> None:
    """books/sdr.json = `path`'s size and mtime: what sdr_render_ok later matches p.dv_render to."""
    st = os.stat(path)
    _write_json(_book(p, SDR_PROOF), {"size": st.st_size, "mtime_ns": st.st_mtime_ns})


def sdr_render_ok(p) -> bool:
    """Is p.dv_render an SDR render this module published (or blessed) for the SDR output — its
    proof (books/sdr.json) still naming exactly that file's size and mtime? Never raises; a DV
    render, an SDR render nobody blessed, or no render at all is False."""
    try:
        proof = _read_json(_book(p, SDR_PROOF))
        st = os.stat(p.dv_render)
    except (OSError, AttributeError, TypeError):
        return False
    return (isinstance(proof, dict)
            and (proof.get("size"), proof.get("mtime_ns")) == (st.st_size, st.st_mtime_ns))


def render(p, spec: RenderSpec, abort=None, progress=None, should_pause=None) -> tuple:
    """The 'dvrender' stage body -> (ok, msg): write p.dv_render from `spec` (module docstring).

    A render already at p.dv_render that passes the self-check is kept as it is. "paused: ..."
    (should_pause, or the disk floor before a range, an encode or a finish) and "aborted (run
    stopped)" are not failures: the next call resumes the analysis range by range (an encode
    or finish in flight restarts its attempt). Anything else is a plain reason for the caller
    to count. On success the encodes are deleted and the small books (spec, sidecars, RPU,
    attempt log, CPU) kept."""
    ep = getattr(p, "ep", "?")
    bad = spec.problems()
    if bad:
        return False, "native render spec is unusable: " + "; ".join(bad)
    if spec.sdr and os.path.exists(p.dv_render) and self_check(p, p.dv_render, sdr=True)[0]:
        # an SDR render of this whole source is already there — ours (its proof just re-written)
        # or an older one of the same picture: either is what the SDR master is made from
        try:
            _write_sdr_proof(p, p.dv_render)
        except OSError as e:
            return False, f"native render failed: the SDR proof could not be written: {e}"
        return True, "native SDR render already in place"
    if not spec.sdr and os.path.exists(p.dv_render) and self_check(
            p, p.dv_render, **({"as_source": True} if as_source(spec) else {}))[0]:
        return True, "native DV render already in place"
    try:
        stats = input_stats(spec)
    except OSError as e:
        return False, f"native render input is missing: {e}"
    try:
        os.makedirs(books_dir(p), exist_ok=True)
        _rm(*_transients(p))
        state = begin(p, spec, stats)
        if spec.single:
            if state.get("source_frames") is None:      # a packet scan: once per source file
                state["source_frames"] = dvcap.count_hevc_frames(spec.inputs[0][1], FFPROBE)
                _write_json(_book(p, SPEC_BOOK), state)
            have = state["source_frames"]
            if have and have != spec.total:             # 0 = unreadable: the analysis decides
                return False, f"native render source has {have} frames, the spec {spec.total}"
        rpu = {} if spec.sdr else _analyse(p, spec, state, abort, progress, should_pause)
        enc = (_copy_video(p, spec, rpu, abort, progress) if spec.keep_video
               else _encode(p, spec, rpu, abort, progress, should_pause))
        why = _publish(p, spec, rpu, progress)
    except dvnative.Paused as e:
        msg = str(e)
        return False, msg if msg.startswith("paused:") else "paused: " + msg
    except dvnative.Aborted:
        return False, "aborted (run stopped)"
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as e:
        return False, f"native render failed: {e}"
    finally:
        _rm(*_transients(p), _book(p, PROGRESS), _book(p, SEI_TMP))
    _rm(*(_book(p, n) for n in (ENCODE, BEST, CONCAT)))
    if spec.keep_video:
        logbook.event(f"dvrender {ep}: {spec.total} frames, {rpu.get('shots')} shots, the source's "
                      f"own video (no encode), L6 MaxCLL/MaxFALL "
                      f"{spec.light_text() or 'measured ' + str(rpu.get('max_cll'))}")
        return True, (f"native DV render: {spec.total} frames, {rpu.get('shots')} shots, the "
                      f"source's own video with the RPU injected (no encode) — {why}")
    peak = f"peak {enc['peak']:.1f} Mbps ({enc['verdict']})" if spec.cap_mbps else "no peak gate"
    if spec.sdr:
        logbook.event(f"dvrender {ep}: SDR render, {spec.total} frames, {enc['kbps']} kb/s after "
                      f"{enc['attempts']} encode(s), {peak}")
        return True, (f"native SDR render: {spec.total} frames, {enc['kbps']} kb/s, {peak} — {why}")
    logbook.event(f"dvrender {ep}: {spec.total} frames, {rpu.get('shots')} shots, "
                  f"{enc['kbps']} kb/s after {enc['attempts']} encode(s), {peak}")
    return True, (f"native DV render: {spec.total} frames, {rpu.get('shots')} shots, "
                  f"{enc['kbps']} kb/s, {peak} — {why}")
