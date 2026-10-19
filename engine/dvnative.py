"""Native Dolby Vision metadata: measure the picture ourselves.

WHAT IT DOES: (1) the SDR->PQ conversion, (2) shot detection, (3) Dolby Vision analysis. (1) is
the one captured LUT (the reference 1000-nit conversion, archived with its sha256 manifest),
applied by ffmpeg (`lut_vf`); (2) and (3) are below.

HOW:
  1. hdr-analyze's `hdr_analyzer_mvp` (MIT, github.com/tinof/hdr-analyze, built from source at
     a pinned commit — HDR_ANALYZER_COMMIT) measures the PQ picture: scene cuts plus per-scene
     L1 (min / max-RGB average / max, 12-bit PQ codes) into a JSON sidecar.
  2. `frame_l4()` measures, per frame, the mean and standard deviation of PQ max(R,G,B) —
     the two values the reference masters' L4 (anchor_pq / anchor_power) track
     (black-box: r = 0.999 and 0.97 against pixel truth on S06E01). Plain measurement; the
     meaning of L4 comes from a public Dolby patent (US 12573352), so patent risk is noted.
  3. `rpu_config()` turns both into a `dovi_tool generate` config. It started as a port of
     hdr-analyze's mkvdovi/src/metadata/rpu_config.rs::generate_extra_json (MIT, Copyright
     (c) 2025 HDR-Analyze Contributors), with the static blocks set to what the reference
     masters carry
     (CM v4.0, source range 7..3079, L9 BT.2020, L11 content type 0, L5 zero, a scene refresh
     on every frame) and L1 clamped to the declared 1000-nit source range.
  4. `dovi_tool generate` writes the binary RPU, which dvcap's x265 path interleaves exactly
     like an RPU extracted from a render.
  The TV lane measures Topaz's chunks (`analyze_chunks`); nativerender measures one file in
  scene ranges (`analyze_ranges`), optionally behind a geometry/matrix front end (`pre_vf`)
  and with another mastering (`MasterSpec`). Both run the same overlapped loop and default to
  exactly the chain and RPU above.

WHAT IT DOES NOT DO: Dolby's CM v4 analysis. A licensed analyzer's L1 average is Dolby's
"anchored" value (not a mean), and its L2 trims and L3 offsets come from Dolby's algorithm;
none of that is reproduced. L1 here is a true measurement (average floored at the CM v4
minimum 1229, the reference masters' own minimum) and L2 is neutral (2048).
"""
from __future__ import annotations
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from fractions import Fraction

import dvcap
import versions as _versions

DOVI_TOOL = "/opt/homebrew/bin/dovi_tool"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
# The analyzer binary and its commit are pinned in versions.py (the native lane's toolchain
# pins). Read with defaults so this module still imports beside an older versions.py.
HDR_ANALYZER = os.path.expanduser(getattr(_versions, "HDR_ANALYZER_PATH",
                                          "~/.topaz-pipeline/bin/hdr_analyzer_mvp"))
HDR_ANALYZER_COMMIT = getattr(_versions, "HDR_ANALYZER_COMMIT", "383b0ad")
                                    # tinof/hdr-analyze, built from source (cargo, Homebrew ffmpeg 8)
ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))

# What the reference 1000-nit DV masters carry (read from their RPUs with
# `dovi_tool export`): mastering 0.0001..1000 nits, BT.2020 source primaries (L9 index 2),
# L11 content type 0 / whitepoint 0 / reference mode off, L5 all zero, CM v4.0, and a scene
# refresh flagged on EVERY frame (L1 held constant within a shot).
MASTER_MIN_NITS = 0.0001
MASTER_MAX_NITS = 1000
SOURCE_PRIMARY_BT2020 = 2
L11_CONTENT_TYPE = 0
TRIM_TARGETS_NITS = (100, 600, 1000)
NEUTRAL = 2048                  # L2 trim field value meaning "no trim"
PQ12_MAX = 4095


def nits_to_pq12(nits: float) -> int:
    """PURE. Absolute luminance -> 12-bit PQ code (SMPTE ST 2084), rounded. 1000 nits -> 3079,
    100 -> 2081, 600 -> 2851, 0.0001 -> 7 (the codes the reference RPUs carry)."""
    m1, m2 = 2610.0 / 16384.0, 2523.0 / 32.0
    c1, c2, c3 = 3424.0 / 4096.0, 2413.0 / 128.0, 2392.0 / 128.0
    y = max(0.0, min(float(nits), 10000.0)) / 10000.0
    ym = y ** m1
    return int(round(((c1 + c2 * ym) / (1.0 + c3 * ym)) ** m2 * PQ12_MAX))


SOURCE_MAX_PQ = nits_to_pq12(MASTER_MAX_NITS)      # 3079
SOURCE_MIN_PQ = nits_to_pq12(MASTER_MIN_NITS)      # 7


# ---- mastering: what one RPU declares ---------------------------------------------------
# The constants above are the reference 1000-nit DV master, and every SDR route renders through
# it. A route whose master is something else (an HDR source keeping its own mastering, a
# 2000-nit project) passes a MasterSpec instead; the DEFAULT is exactly those constants, so a
# caller that passes nothing generates the same RPU byte for byte (test_dvnative pins that on
# the stored E2E sidecar).

_MDCV_RE = re.compile(r"^G\(\d+,\d+\)B\(\d+,\d+\)R\(\d+,\d+\)WP\(\d+,\d+\)L\((\d+),(\d+)\)$")


@dataclass(frozen=True)
class MasterSpec:
    """The mastering an RPU declares: L6 mastering range, the source range L1/L4 are clamped
    to (source_min_pq / source_max_pq), MaxCLL's ceiling and the L2 trim targets. `mdcv` is
    the matching HDR10 mastering-display string in x265's units (dvcap.probe_video's
    "master_display"), carried for the routes that pass a source's own mastering through;
    when given, its L(max,min) must say the same nits as max_nits/min_nits, so the HDR10 SEI
    and the RPU can never disagree. Nothing in the RPU reads it; the native render writes it
    as the HDR10 mastering-display SEI (hdr10sei; None -> dvcap.FALLBACK_MASTER_DISPLAY, the
    1000-nit P3 D65 display the x265 path writes). A minimum of 0 nits is allowed only WITH an
    mdcv (2026-10-18): it is what some HDR10 sources' own mastering display says, and an RPU
    that keeps that source's mastering must say the same (L6 min 0, source_min_pq 0)."""
    max_nits: int = MASTER_MAX_NITS
    min_nits: float = MASTER_MIN_NITS
    trim_targets: tuple = TRIM_TARGETS_NITS
    mdcv: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "trim_targets", tuple(int(t) for t in self.trim_targets))
        lowest_ok = float(self.min_nits) > 0 or (self.mdcv is not None and float(self.min_nits) == 0)
        if not (lowest_ok and float(self.min_nits) < int(self.max_nits) <= 10000):
            raise ValueError(f"mastering range must be 0 < min < max <= 10000 nits, got "
                             f"{self.min_nits}..{self.max_nits}")
        t = self.trim_targets
        if not t or list(t) != sorted(set(t)) or t[0] <= 0 or t[-1] > 10000:
            raise ValueError(f"trim targets must be ascending nits in (0, 10000], got {t}")
        if self.mdcv is not None:
            m = _MDCV_RE.match(self.mdcv)
            if not m:
                raise ValueError(f"mdcv is not an x265 master-display string: {self.mdcv!r}")
            if (int(m.group(1)), int(m.group(2))) != (int(self.max_nits) * 10000, self.l6_min):
                raise ValueError(f"mdcv says L({m.group(1)},{m.group(2)}), the spec "
                                 f"{self.max_nits}/{self.min_nits} nits")

    @property
    def source_max_pq(self) -> int:
        return nits_to_pq12(self.max_nits)

    @property
    def source_min_pq(self) -> int:
        return nits_to_pq12(self.min_nits)

    @property
    def l6_min(self) -> int:
        """L6's min_display_mastering_luminance, in 0.0001-nit units (0.0001 nits -> 1)."""
        return int(round(float(self.min_nits) * 10000))

    @property
    def hdr10_mdcv(self) -> str:
        """The HDR10 mastering-display string a render of this master carries: `mdcv`, or the
        x265 path's own fallback (the 1000-nit P3 D65 display) for the default 1000-nit
        master. A non-default master must name its display — there is no fallback for it."""
        if self.mdcv:
            return self.mdcv
        if (int(self.max_nits), self.l6_min) == (MASTER_MAX_NITS, int(round(MASTER_MIN_NITS * 10000))):
            return dvcap.FALLBACK_MASTER_DISPLAY
        raise ValueError(f"a {self.max_nits}-nit master needs its own mdcv string")


DEFAULT_MASTER = MasterSpec()      # the reference 1000-nit master: today's RPU, unchanged

# An HDR10 source KEEPS its own mastering (2026-10-18, the HDR intake routes rpu_only /
# hdr_reencode): its mastering-display SEI becomes the RPU's L6 mastering range and the clamp for
# L1/L4, and its content-light-level SEI is L6's MaxCLL/MaxFALL as it stands — never measured,
# never clamped — so the RPU and the HDR10 metadata in the same stream can never disagree. That
# is Dolby's rule for L6, and it is the rpu_only route's whole contract: the original stream ships
# with its own SEI, so only the RPU can be made to match. (A source whose SEI says 0,0 —
# "unknown" — is measured instead, in L6 and the SEI alike: stages.declared_light.)
SOURCE_TRIM_TARGETS = TRIM_TARGETS_NITS     # the L2 targets a source's master keeps, minus any above its peak
SOURCE_MIN_PEAK_NITS = 100                  # a mastering display under this is not an HDR one (bad SEI)


def source_mastering(master_display, max_cll) -> tuple:
    """PURE. A source's HDR10 static metadata as dvcap.probe_video reads it — `master_display`
    (x265's "G(..)B(..)R(..)WP(..)L(max,min)" in 0.0001-nit luminance units, or None) and
    `max_cll` ("MaxCLL,MaxFALL", or None) — -> (MasterSpec, (MaxCLL, MaxFALL)): the master
    that keeps the source's own mastering (its display as the mdcv, its peak and minimum as the
    range, the L2 targets at or under its peak) and the light levels L6 must declare as given.
    No mastering display -> DEFAULT_MASTER (the 1000-nit master every SDR route writes); no
    content light level -> (0, 0), HDR10's own "unknown", which is what such a stream says.
    Raises ValueError (the reason) for a mastering display no RPU can carry exactly: a peak that
    is not a whole number of nits, under SOURCE_MIN_PEAK_NITS or over 10000, or a minimum that
    is not under the peak."""
    light = (0, 0)
    if max_cll:
        try:
            a, b = (int(v) for v in str(max_cll).split(","))
        except ValueError:
            raise ValueError(f"the source's content light level {max_cll!r} does not read")
        if not (0 <= a <= 65535 and 0 <= b <= 65535):
            raise ValueError(f"the source's content light level {max_cll!r} is out of range")
        light = (a, b)
    if not master_display:
        return DEFAULT_MASTER, light
    m = _MDCV_RE.match(str(master_display))
    if not m:
        raise ValueError(f"the source's mastering display {master_display!r} does not read")
    lmax, lmin = int(m.group(1)), int(m.group(2))
    if lmax % 10000:
        raise ValueError(f"the source's mastering peak is {lmax / 10000:g} nits — an RPU's L6 "
                         "declares whole nits")
    peak = lmax // 10000
    if not SOURCE_MIN_PEAK_NITS <= peak <= 10000:
        raise ValueError(f"the source's mastering peak is {peak} nits (not an HDR mastering display)")
    targets = tuple(t for t in SOURCE_TRIM_TARGETS if t <= peak)
    try:
        return MasterSpec(max_nits=peak, min_nits=lmin / 10000, trim_targets=targets,
                          mdcv=str(master_display)), light
    except ValueError as e:
        raise ValueError(f"the source's mastering display {master_display!r}: {e}")


# ---- colour: the captured SDR->PQ conversion ---------------------------------------------
# Topaz's ProRes is BT.709 limited 4:2:2. The LUT maps BT.709 R'G'B' to BT.2020 PQ R'G'B'
# (the captured 1000-nit SDR->PQ conversion). zscale does ONLY matrix/range work around
# it: the first instance turns YUV into RGB with transfer and primaries left alone; the
# second declares its input as already PQ/BT.2020, so it converts the matrix and range and
# nothing else. (Leaving the second zscale to read the frame's stale BT.709 tags made it
# apply 709->PQ and 709->2020 again on top of the LUT — review-caught.)

_SAFE_PATH = re.compile(r"^[A-Za-z0-9_./+-]+$")


def _lut_arg(cube: str) -> str:
    if not _SAFE_PATH.match(cube or ""):
        raise ValueError(f"LUT path must be plain (no spaces/quotes) for an ffmpeg filter: {cube!r}")
    return cube


def lut_rgb_chain(cube: str) -> str:
    """BT.709 limited YUV -> RGB float -> LUT. Output: BT.2020 PQ R'G'B' in gbrpf32le."""
    return ("zscale=matrixin=709:rangein=limited,format=gbrpf32le,"
            f"lut3d=file={_lut_arg(cube)}:interp=tetrahedral")


def pq_rgb_to_yuv(pix_fmt: str) -> str:
    """BT.2020 PQ R'G'B' -> BT.2020nc limited YUV in `pix_fmt`, with no transfer/primaries change."""
    return ("zscale=transferin=smpte2084:primariesin=2020:transfer=smpte2084:primaries=2020:"
            f"matrix=2020_ncl:range=limited,format={pix_fmt}")


def lut_vf(cube: str, pix_fmt: str = "yuv420p10le") -> str:
    """The full SDR->PQ chain for an encode: Topaz ProRes -> BT.2020 PQ YUV."""
    return lut_rgb_chain(cube) + "," + pq_rgb_to_yuv(pix_fmt)


# ---- front ends (pre_vf) -----------------------------------------------------------------
# A `pre_vf` is a plain, linear filter chain run BEFORE the chains above (analysis and encode
# alike, so the RPU measures the picture that ships). Its contract is the LUT chain's input:
# BT.709 limited-range Y'CbCr (with no LUT: BT.2020 PQ Y'CbCr), because lut_rgb_chain's first
# zscale reads its input as matrix 709, limited range, whatever the frame says. So a front end
# does geometry and standard matrix/range conversions only, and the one captured LUT (the
# BT.709 DV1000 conversion) stays the only colour transform. It must hand on Y'CbCr, never RGB:
# that zscale would read RGB planes as Y'CbCr.
#
# THE FIT FRONT END (fit_front_end; 2026-10-18, the single-file SDR routes whose source is not
# already the master's 3840x2160 BT.709 frame: yt_plain and yt_1080, stages.dvrender_spec).
# Standard zscale conversions only (user decision: no further LUT captures), 16-bit 4:4:4
# throughout so an 8-bit source is rounded once, at the very end:
#   1. colour  (colour_vf, only when needed) BT.601 or full-range Y'CbCr -> BT.709 limited, at
#              the SOURCE's size. SMPTE-C / EBU primaries are converted to BT.709 through linear
#              light (zimg linearises "709" with BT.1886's 2.4 gamma: display-referred, so the
#              picture keeps the colours it had on a monitor — measured 2026-10-18 within 6/65535
#              of that reference); the LUT is BT.709 only. Which conversion a source needs is
#              colour_front's rule: the tags when present, else the frame size.
#   2. fit     (fit_pad_vf) Lanczos, 3 taps (user decision: plain Lanczos for the YouTube upscale
#              — a blind A/B against MetalFX showed no difference), to the largest size that keeps
#              the display aspect inside 3840x2160 (fit_geometry: 1920x1080 is exactly 2x),
#              marked square-pixel (setsar=1: zscale would otherwise carry a near-1 sample aspect
#              such as 1215:1216 into the master), then a centred black pad.
#   3. the LUT chain, unchanged.
# WHY SCALE BEFORE THE LUT: it is the TV lane's order — Topaz scales BT.709 Y'CbCr and the LUT
# then runs on the 4K picture — so the validated chain (lut_vf) runs byte for byte on the kind of
# frame it was made for; the analysis keeps its measured scale-first form (front end, area to
# 1080p, LUT); and the pad, laid in Y'CbCr before the LUT, leaves it as EXACTLY the code source
# black does. Measured 2026-10-18 against a reference 4K render of a 1080p60 YouTube video (5
# frames): the opposite order (LUT at 1080p, Lanczos in PQ R'G'B') moved the median dE2000 by at
# most 0.02 and the p95 by at most 0.4 — so the order is chosen for exactness, not for a look.
# THE PAD'S BLACK: pad writes FULL-range black (Y 0, neutral chroma — exact at any bit depth)
# into this limited-range frame. That is below black, which the LUT clamps to its black, so the
# bars come out exactly as source black does (measured: 64/512/512 both, 10-bit). ffmpeg's own
# limited-range black is 16/255 of full scale (Y 4112, Cb/Cr 32896 at 16 bits, not 4096/32768),
# which came out ONE CODE above black (65/513/512). The analysis' 2:1 area downscale never mixes
# pad and picture either: fit_geometry keeps every size and offset even.
_PRE_VF_BAD = re.compile(r"[;\[\]\n]")


def front_end(pre_vf: str | None) -> str:
    """'' for no front end, else `pre_vf` + "," ready to prefix a chain. Refuses a filtergraph
    (';', '[label]'): a front end must be one linear chain, or the chain after it breaks."""
    if not pre_vf:
        return ""
    if _PRE_VF_BAD.search(pre_vf) or pre_vf.strip(", ") != pre_vf:
        raise ValueError(f"pre_vf must be one plain filter chain: {pre_vf!r}")
    return pre_vf + ","


FIT_FRAME = (3840, 2160)        # the master's frame: every fitted picture ends up this size
FIT_TAPS = 3                    # Lanczos taps (zimg's own default, pinned so a zimg change can't move it)
PAD_SNAP_PX = 4                 # a pad of this many pixels or fewer is closed by stretching instead:
                                # under 0.2% of either side is invisible, a hairline bar on a TV is not
SD_MAX_LINES = 576              # an UNTAGGED frame of at most 576 lines (and narrower than
HD_MIN_WIDTH = 1280             # 1280 — a cropped 2.39:1 720p is HD) is SD: BT.601. Anything else is
PAL_LINES = 576                 # BT.709. Untagged SD of exactly 576 lines is 625-line (EBU) material,
                                # every other SD height 525-line (SMPTE-C) — mpv's long-standing guess
_FRONT_MATRIX = {"bt709": "709", "smpte170m": "170m", "bt470bg": "470bg"}      # ffprobe -> zscale
_FRONT_PRIMARIES = {"bt709": "709", "smpte170m": "170m", "bt470bg": "470bg"}   # (170m and 470bg
                                # share one matrix; their primaries differ)
SDR_TRANSFERS = ("bt709", "smpte170m", "bt470bg")   # BT.709's curve under its three common tags
                                # (601's is the same curve; "bt470bg" is how PAL encoders tag it)
_UNTAGGED = (None, "", "unknown", "unspecified", "reserved")   # = nativelane.UNKNOWN_TAGS


def _int0(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _sar(sar) -> Fraction:
    """A sample aspect ratio ("8:9", "1:1", Fraction, (n, d)) as a Fraction; square (1) for
    none, "N/A", "0:1" (ffprobe's "unknown") or anything unreadable."""
    try:
        if isinstance(sar, Fraction):
            f = sar
        elif isinstance(sar, (tuple, list)):
            f = Fraction(int(sar[0]), int(sar[1]))
        else:
            n, d = str(sar).split(":")
            f = Fraction(int(n), int(d))
    except (TypeError, ValueError, ZeroDivisionError, IndexError):
        return Fraction(1)
    return f if f > 0 else Fraction(1)


def _even(v: Fraction) -> int:
    """v rounded to the nearest even integer (halves up), never under 2."""
    return max(2, 2 * math.floor(Fraction(v) / 2 + Fraction(1, 2)))


def fit_geometry(width, height, sar=None, frame=FIT_FRAME) -> tuple:
    """PURE. (w, h, x, y): the size of a `width` x `height` picture fitted into `frame` and its
    top-left corner there. The largest size that keeps the DISPLAY aspect (width x sar : height)
    inside the frame, rounded to even both ways (4:2:0), centred on even offsets (so the
    analysis' 2:1 area downscale never mixes picture and pad); a pad of PAD_SNAP_PX or fewer is
    closed by stretching. 1920x1080 -> (3840, 2160, 0, 0), exactly 2x; 640x480 -> (2880, 2160,
    480, 0); 1080x1920 -> (1216, 2160, 1312, 0). Exact arithmetic (Fraction). Raises ValueError
    for a size that is not positive."""
    fw, fh = int(frame[0]), int(frame[1])
    w0, h0 = _int0(width), _int0(height)
    if w0 <= 0 or h0 <= 0 or fw <= 0 or fh <= 0:
        raise ValueError(f"cannot fit a {width}x{height} picture into {frame[0]}x{frame[1]}")
    dw = Fraction(w0) * _sar(sar)
    s = min(Fraction(fw) / dw, Fraction(fh, h0))
    w, h = min(fw, _even(dw * s)), min(fh, _even(h0 * s))
    if fw - w <= PAD_SNAP_PX:
        w = fw
    if fh - h <= PAD_SNAP_PX:
        h = fh
    return w, h, (fw - w) // 4 * 2, (fh - h) // 4 * 2


def fit_pad_vf(width, height, sar=None, frame=FIT_FRAME) -> str:
    """PURE. The fit step: Lanczos to fit_geometry's size as 16-bit 4:4:4 BT.709 limited, square
    pixels, then — when the picture does not fill the frame — the centred pad, in full-range black
    (see the section comment: below black, which the LUT clamps to exactly source black's code).
    SQUARE PIXELS (2026-10-18 review): zscale keeps the input's DISPLAY aspect by rewriting the
    output's sample aspect whenever the new size's ratio differs from the old one's — and
    fit_geometry's even rounding and hairline snap always differ a little (1080x1920 -> 1216 wide
    for 1215 is sar 1215:1216; 854x480 stretched to 3840x2160 is 1281:1280). VideoToolbox writes
    that into the HEVC VUI, mkvmerge keeps it and the y4m pipe hands it to x265, so a player that
    honours it would rescale the whole 4K frame to 3837 / 3843 wide. The fitted picture IS square
    by construction (the snap is the deliberate, sub-0.2% stretch), so the step says so: setsar=1,
    as square_vf does."""
    w, h, x, y = fit_geometry(width, height, sar, frame)
    vf = (f"zscale=w={w}:h={h}:filter=lanczos:param_a={FIT_TAPS}:rangein=limited:range=limited,"
          "format=yuv444p16le,setsar=1")
    if (w, h) != (int(frame[0]), int(frame[1])):
        vf += (f",setparams=range=pc,pad={int(frame[0])}:{int(frame[1])}:{x}:{y}:color=black,"
               "setparams=range=tv")
    return vf


def _front_tag(value, table: dict, what: str):
    """A matrix / primaries tag as its zscale name; None when untagged. Raises for a tag no
    standard front end turns into the LUT's BT.709 input."""
    if value in _UNTAGGED:
        return None
    if value not in table:
        raise ValueError(f"the source {what} are {value} (the LUT takes BT.709, and BT.601 through "
                         "the front end)")
    return table[value]


def colour_front(info: dict) -> tuple:
    """PURE. A source's ffprobe stream tags -> (matrix, primaries, full_range): what its Y'CbCr
    is converted FROM to become the LUT's input, BT.709 limited (zscale names "709", "170m",
    "470bg"). A tag says it when there is one. An untagged matrix or primaries follows the other
    one's family when that one is tagged (a smpte170m matrix with no primaries tag is SMPTE-C);
    with neither tagged the frame decides (the route plan's rule; the width guard is mpv's): SD
    (at most SD_MAX_LINES lines, narrower than HD_MIN_WIDTH) is BT.601 — EBU primaries at exactly PAL_LINES, SMPTE-C
    otherwise — and anything larger is BT.709. Range: "pc" is full, anything else limited.
    Raises ValueError (the reason) for a source no standard front end makes the LUT's input:
    unknown size, HDR, another transfer, matrix or primaries (BT.2020, RGB, ...)."""
    info = info if isinstance(info, dict) else {}
    w, h = _int0(info.get("width")), _int0(info.get("height"))
    if w <= 0 or h <= 0:
        raise ValueError("the source's size is unknown")
    trc = info.get("color_transfer")
    if trc in ("smpte2084", "arib-std-b67"):
        raise ValueError(f"the source is already HDR ({trc})")
    if trc not in _UNTAGGED and trc not in SDR_TRANSFERS:
        raise ValueError(f"the source transfer is {trc}, not BT.709's curve (the LUT's input)")
    rng = info.get("color_range")
    if rng not in _UNTAGGED and rng not in ("tv", "pc"):
        raise ValueError(f"the source range is {rng}")
    m = _front_tag(info.get("color_space"), _FRONT_MATRIX, "matrix")
    p = _front_tag(info.get("color_primaries"), _FRONT_PRIMARIES, "primaries")
    if m is None and p is None:
        sd = h <= SD_MAX_LINES and w < HD_MIN_WIDTH
        m = p = ("470bg" if h == PAL_LINES else "170m") if sd else "709"
    elif m is None:
        m = p                       # the 601 matrix is one matrix under either name
    elif p is None:
        p = m
    return m, p, rng == "pc"


def colour_vf(matrix: str, primaries: str, full: bool = False) -> str:
    """PURE. The colour step: '' when the source already is the LUT's BT.709 limited Y'CbCr, else
    one zscale to BT.709 limited 16-bit 4:4:4 at the source's size — the matrix and range always,
    and the primaries (through linear light, BT.709's curve both ways) when they are not
    BT.709's. The transfer and the primaries are named explicitly, so an untagged source is
    converted by the rule colour_front applied, never by whatever the frame says."""
    names = ("709", "170m", "470bg")
    if matrix not in names or primaries not in names:
        raise ValueError(f"no colour front end from matrix {matrix!r} / primaries {primaries!r}")
    if (matrix, primaries, bool(full)) == ("709", "709", False):
        return ""
    vf = f"zscale=matrixin={matrix}:rangein={'full' if full else 'limited'}"
    if primaries != "709":
        vf += f":transferin=709:primariesin={primaries}:transfer=709:primaries=709"
    return vf + ":matrix=709:range=limited,format=yuv444p16le"


def fit_front_end(info: dict, frame=FIT_FRAME) -> str:
    """PURE. The whole fit front end for a source with these ffprobe stream tags (width, height,
    sample_aspect_ratio, colour tags): colour (if any), then the fit — a pre_vf for the LUT
    chain. Raises ValueError (colour_front's reason) for a source it cannot take."""
    m, p, full = colour_front(info)
    parts = (colour_vf(m, p, full),
             fit_pad_vf(info.get("width"), info.get("height"), info.get("sample_aspect_ratio"), frame))
    return ",".join(part for part in parts if part)


# THE CHUNK FRONT END (chunk_front_end; 2026-10-18, the Topaz chunk routes past the TV lane's
# 720p+ BT.709 episodes: sd_sdr_topaz in the chunk lane, and the native render from chunks for
# movie_sdr_topaz — and sd_sdr_topaz when the lane declines it). Topaz upscales in the source's own
# Y'CbCr and tags each chunk with the source's colour (topaz.color_flags): a 480p DVD episode's
# chunk is BT.601 Y'CbCr tagged smpte170m, at 2880x2160 for 4:3. Measured 2026-10-18 on It's
# Always Sunny 2x06's chunk 0: its Y'CbCr, area-scaled back to 640x480, fits the source's with a
# diagonal of 0.99-1.04 and cross terms of at most 0.03 over five frames — Topaz kept the BT.601
# matrix both ways (a 601/709 mix-up shows cross terms near 0.1). The LUT takes BT.709, so:
#   1. colour  colour_vf from the CHUNK's tags, the same standard conversion the fit front end
#              uses (user decision: no further LUT captures). An UNTAGGED chunk is judged by the
#              SOURCE's frame (colour_front's SD rule) — by its own 4K frame every untagged SD
#              source would read as BT.709
#   2. square  a chunk with non-square pixels (an anamorphic DVD: Topaz keeps the sample aspect,
#              720x480 8:9 -> 3240x2160 8:9) is resampled to square pixels at its own height
#              (Lanczos, 3 taps; 8:9 -> 2880x2160, 32:27 -> 3840x2160), so the master never
#              depends on a player honouring a sample aspect. A square chunk keeps its own frame,
#              as the TV lane's 2880x2160 4:3 episodes always have (no pad: the TV pillarboxes)
#   3. the LUT chain, unchanged.
# '' when the chunk already is the LUT's input — square BT.709 limited, every TV-lane chunk.
CHUNK_MAX_WIDTH = 4096          # the widest square frame it makes (HEVC level 5.1's 4096x2176)


def square_vf(width, height, sar=None) -> str:
    """PURE. '' for square pixels; else Lanczos to (width x sar, rounded even) x height as 16-bit
    4:4:4 BT.709 limited, then sar 1. Raises ValueError for an unknown size or a square frame
    wider than CHUNK_MAX_WIDTH."""
    w0, h0 = _int0(width), _int0(height)
    if w0 <= 0 or h0 <= 0:
        raise ValueError(f"the chunk's size is unknown ({width}x{height})")
    s = _sar(sar)
    if s == 1:
        return ""
    w = _even(Fraction(w0) * s)
    if w > CHUNK_MAX_WIDTH:
        raise ValueError(f"a {w0}x{h0} chunk at sample aspect {sar} is {w} pixels wide square, "
                         f"over {CHUNK_MAX_WIDTH}")
    return (f"zscale=w={w}:h={h0}:filter=lanczos:param_a={FIT_TAPS}:rangein=limited:range=limited,"
            "format=yuv444p16le,setsar=1")


def chunk_front_end(chunk: dict, source: dict | None = None) -> str:
    """PURE. The front end for a Topaz chunk of an SDR source (section comment): `chunk` is chunk
    0's ffprobe stream tags (width, height, sample_aspect_ratio, colour), `source` the source's
    (only its width and height are read: the untagged rule). Colour (if any), then square pixels
    (if needed) — a pre_vf for the LUT chain, '' for none. Raises ValueError (the reason) for a
    chunk no standard front end makes the LUT's input."""
    chunk = chunk if isinstance(chunk, dict) else {}
    source = source if isinstance(source, dict) else {}
    judged = dict(chunk)
    if _int0(source.get("width")) > 0 and _int0(source.get("height")) > 0:
        judged.update(width=source["width"], height=source["height"])
    m, p, full = colour_front(judged)
    # PILLARBOX (2026-10-18): the reference SD masters are a full 3840x2160 frame with the
    # 4:3 picture centred (It's Always Sunny 2x06's render, measured). A Topaz chunk of an SD
    # source is 2880x2160, and shipping that bare would be a size no master has ever had on the
    # SHIELD's Dolby Vision decoder. So a chunk whose picture does not fill the frame gets the
    # same fit-and-pad step as a plain YouTube size (square pixels included). ONLY for an SD
    # source: the 720p+ lane (live) ships 4:3 HD at 2880x2160 and scope at 3840x1600 unpadded,
    # and its front end must not change under it.
    w, h = chunk.get("width"), chunk.get("height")
    sar = chunk.get("sample_aspect_ratio")
    sw, sh = _int0(source.get("width")), _int0(source.get("height"))
    sd = 0 < sh <= 576 and 0 < sw < 1280
    try:
        fills = fit_geometry(w, h, sar)[:2] == (int(FIT_FRAME[0]), int(FIT_FRAME[1]))
    except ValueError:
        fills = True
    shape = square_vf(w, h, sar)            # first: it refuses a picture too wide to square
    if sd and not fills:
        shape = fit_pad_vf(w, h, sar)
    parts = (colour_vf(m, p, full), shape)
    return ",".join(part for part in parts if part)


# ---- colour: HDR intake (no LUT) ---------------------------------------------------------
# An HDR source is already the master's colour: BT.2020 primaries, the BT.2020 non-constant-
# luminance matrix, limited range. PQ needs no front end at all — the picture is measured and
# encoded as it is. HLG is converted to PQ by the standard BT.2100 conversion (user decision
# 2026-10-15: standard conversions, no new LUT captures): zimg's HLG inverse OETF, then
# BT.2100's display-referred OOTF for a HLG_NPL-nit display (system gamma 1.2 at 1000 nits),
# then the PQ inverse EOTF — BT.2408's "HLG to PQ at 1000 cd/m2", which puts HLG's 75% reference
# white at 203 nits. zimg FIXES that display at 1000 nits: zscale's `npl` scales only SDR transfers
# and is ignored for HLG (measured 2026-10-18: npl 203 / 1000 / 4000 / unset all give the same
# codes), so the chain names no npl — HLG_NPL is the peak it converts for, which the test pins
# (E' 1.0 -> 1000 nits, 0.75 -> 203). Primaries and matrix stay BT.2020; every property is named
# explicitly, so the frame's own tags never decide anything. It runs at the source's size in
# 16-bit 4:4:4 (zimg works in float), like the SDR fit front end, and the 1000-nit master is
# exactly the display it is converted for (the RPU keeps dvnative's default mastering).
PQ, HLG = "smpte2084", "arib-std-b67"
HLG_NPL = 1000                  # the display peak zimg converts HLG for (BT.2408's reference); fixed
_BT2020_PRIMARIES = ("bt2020",)
_BT2020_MATRIX = ("bt2020nc",)  # constant-luminance BT.2020 (bt2020c) is refused: nothing makes it


def hlg_to_pq_vf() -> str:
    """PURE. The HLG -> PQ front end: BT.2020 limited Y'CbCr in, HLG -> PQ through display light
    on an HLG_NPL-nit display (BT.2100's OOTF, system gamma 1.2), BT.2020 limited 16-bit 4:4:4 out."""
    return ("zscale=matrixin=2020_ncl:transferin=arib-std-b67:primariesin=2020:rangein=limited:"
            "matrix=2020_ncl:transfer=smpte2084:primaries=2020:range=limited,format=yuv444p16le")


def hdr_front_end(info: dict):
    """PURE. An HDR source's ffprobe stream tags -> its front end: None for PQ (nothing to do),
    hlg_to_pq_vf() for HLG. Untagged primaries or matrix are read as BT.2020 (BT.2100 has no
    other); full range, another primaries / matrix tag, or a transfer that is neither PQ nor HLG
    raises ValueError (the reason), and the route refuses the item."""
    info = info if isinstance(info, dict) else {}
    trc = info.get("color_transfer")
    if trc not in (PQ, HLG):
        raise ValueError(f"the source transfer is {trc or 'untagged'}, not PQ or HLG")
    for key, label, ok in (("color_primaries", "primaries", _BT2020_PRIMARIES),
                           ("color_space", "matrix", _BT2020_MATRIX)):
        v = info.get(key)
        if v not in _UNTAGGED and v not in ok:
            raise ValueError(f"the source {label} are {v}, not BT.2020 (an HDR master's)")
    rng = info.get("color_range")
    if rng not in _UNTAGGED and rng != "tv":
        raise ValueError(f"the source is {'full' if rng == 'pc' else rng} range (an HDR master is "
                         "limited range)")
    return None if trc == PQ else hlg_to_pq_vf()


# ---- hdr-analyze ------------------------------------------------------------------------

def build_analyzer_command(analyzer: str, src: str, out_bin: str, *, peak_estimator: str = "max",
                           downscale: int = 1, threads: int | None = None,
                           frame_stats: str | None = None) -> list:
    """`src` must be PQ. --transfer pq because a raw/re-wrapped input can lose its colour
    tags; --no-crop because the reference masters carry L5 = 0 (full frame) and Topaz output is full frame."""
    cmd = [analyzer, "-i", src, "-o", out_bin, "--transfer", "pq", "--no-crop",
           "--peak-estimator", peak_estimator, "--downscale", str(int(downscale))]
    if peak_estimator != "max":
        cmd += ["--peak-source", "max"]          # the estimator acts in the direct peak domain
    if threads:
        cmd += ["--analysis-threads", str(int(threads))]
    if frame_stats:
        cmd += ["--dump-frame-stats", frame_stats]
    return cmd


def sidecar_path(out_bin: str) -> str:
    return out_bin + ".l1.json"


def load_sidecar(path: str) -> dict:
    """The analyzer's L1 sidecar, validated: contiguous scenes from frame 0, each with the
    three L1 codes. Raises ValueError on anything a generated RPU could silently get wrong."""
    with open(path) as f:
        sc = json.load(f)
    validate_sidecar(sc)
    return sc


def validate_sidecar(sc: dict) -> None:
    scenes = sc.get("scenes") or []
    if not scenes:
        raise ValueError("sidecar has no scenes")
    expect = 0
    for i, s in enumerate(scenes):
        for k in ("start", "end", "min_pq_12bit", "max_pq_12bit", "avg_max_rgb_pq_12bit"):
            if not isinstance(s.get(k), int):
                raise ValueError(f"scene {i}: missing {k}")
        if s["start"] != expect or s["end"] < s["start"]:
            raise ValueError(f"scene {i}: starts at {s['start']}, expected {expect}")
        expect = s["end"] + 1


def stream_frames(sc: dict) -> int:
    """Frames the RPU must cover: the stream's picture count when the analyzer recorded it
    (it includes undecodable leading pictures), else the measured scenes' extent."""
    src = sc.get("source") or {}
    measured = sc["scenes"][-1]["end"] + 1
    return int(src.get("stream_frames") or measured)


def leading_frames(sc: dict) -> int:
    return int((sc.get("source") or {}).get("leading_skipped_frames") or 0)


def light_levels(sc: dict, master: MasterSpec = DEFAULT_MASTER):
    """(MaxCLL, MaxFALL) in nits, clamped to the declared mastering range: MaxCLL <= the
    master's peak (1000 by default), MaxFALL <= MaxCLL. (None, None) when unmeasured. A
    measured 8498 on a 1000-nit master (the pilot) would tell HDR10 players to tone-map
    1000-nit content as 8500-nit."""
    ll = sc.get("light_level") or {}
    cll, fall = ll.get("max_cll_nits"), ll.get("max_fall_nits")
    if not cll or not fall or cll < 1 or fall < 1:
        return (None, None)
    cll = min(int(cll), int(master.max_nits))
    return (cll, min(int(fall), cll))


def x265_max_cll(sc: dict, master: MasterSpec = DEFAULT_MASTER):
    """The x265 --max-cll string for the HDR10 SEI, from the same clamped values as L6."""
    cll, fall = light_levels(sc, master)
    return f"{cll},{fall}" if cll else None


# ---- per-chunk analysis -----------------------------------------------------------------
# hdr_analyzer_mvp drops frames on a piped input (measured: a 72-frame y4m pipe read as 1
# frame, NUT as 21), so each Topaz chunk is rendered to a lossless FILE first: the PQ
# picture after the LUT, area-downscaled IN RGB to 1080p, then 4:4:4 (no chroma-subsampling
# overshoot in max-RGB), FFVHuff: lossless like the FFV1 it replaced (pixel-identical output,
# identical analyzer and L4 results), but ~5x cheaper to encode and to decode — FFV1's own
# coding was ~240 of the ~500 ms of CPU each frame cost, and it was decoded twice (analyzer, L4).
# ~2.3 MB/frame on S06E06 (FFV1: 1.5); grain costs it more than FFV1, and a chunk has no upper
# length: Topaz closes it at the first strong cut at least ~90 s in, so a quiet stretch runs
# long (S06E05's 7,058-frame chunk made a 26-28 GiB FFV1 intermediate). At most two exist,
# deleted as soon as they are measured, and never started without room: the caller's
# before_chunk hook checks free space against the chunk's frames first (the native lane's
# 150 GiB floor). Topaz cuts its chunks at strong scene changes, so every chunk start is a
# scene start and per-chunk results join without a seam.
#
# Every step is a child process run by run_step(): at nice 10 in its own process group, polled
# so an abort or should_pause kills it (and anything it started) within a second, and bounded
# by step_timeout(). The per-frame L4 pass runs in a child too (`python3 -m dvnative l4`), so
# its numpy work and memory stay out of the engine process.

ANALYSIS_WIDTH, ANALYSIS_HEIGHT = 1920, 1080
NICE = 10                       # analysis children run below x265 and Topaz
NICE_BIN = "/usr/bin/nice"
POLL_SECONDS = 0.5              # how often a running step checks abort / should_pause / time
STEP_BASE_SECONDS = 300         # per-step timeout = this + frames / STEP_FRAMES_PER_SECOND:
STEP_FRAMES_PER_SECOND = 5      # far above any healthy step, so only a wedged child trips it
OUTPUT_TAIL_BYTES = 1000        # how much of a failed child's output a reason quotes


class Paused(Exception):
    """analyze_chunks stopped cleanly: should_pause or before_chunk asked it to. The step in
    flight was killed and its partial files removed; every published chunk is kept, so the
    next call resumes at the chunk that was interrupted. str() starts with "paused:"."""


class Aborted(RuntimeError):
    """The run was stopped (abort set) mid-analysis; the step in flight was killed."""


def step_timeout(frames) -> int:
    """PURE. Seconds one analysis step of a `frames`-frame chunk may take: 300 + frames / 5,
    rounded up (7,058 frames -> 1,712 s)."""
    return STEP_BASE_SECONDS + -(-max(0, int(frames or 0)) // STEP_FRAMES_PER_SECOND)


def niced(cmd: list, nice: int = NICE) -> list:
    """PURE. `cmd` prefixed to run at `nice` (0 = unchanged). nice(1) execs the command, so
    the child keeps the pid Popen reports, and its own children inherit the priority."""
    return [NICE_BIN, "-n", str(int(nice)), *cmd] if nice else list(cmd)


def wait4_cpu(proc, block: bool = True):
    """Reap `proc` with os.wait4 and return the user+sys CPU seconds it used, including every
    descendant it waited for (the L4 child's ffmpeg). None while it is still running
    (block=False). Sets proc.returncode, as Popen.wait would, so nothing waits on the pid
    again. 0.0 when the pid was already reaped elsewhere."""
    if proc.returncode is not None:
        return 0.0
    try:
        pid, status, ru = os.wait4(proc.pid, 0 if block else os.WNOHANG)
    except ChildProcessError:
        proc.returncode = proc.returncode if proc.returncode is not None else -1
        return 0.0
    if pid == 0:
        return None
    proc.returncode = os.waitstatus_to_exitcode(status)
    return ru.ru_utime + ru.ru_stime


def _kill_group(proc) -> None:
    """SIGKILL the step's whole process group (it was started in its own session) and reap it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except OSError:
            pass
    wait4_cpu(proc, block=True)


def _tail(f) -> str:
    try:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - OUTPUT_TAIL_BYTES))
        return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def run_step(cmd: list, *, timeout: float, abort=None, should_pause=None, cwd: str | None = None,
             what: str = "step", poll: float = POLL_SECONDS, on_poll=None) -> tuple:
    """Run one analysis child to completion -> (returncode, output tail, CPU seconds).

    The child gets its own session (so a kill takes its children too), stdin closed, and its
    stdout+stderr in an anonymous temp file (a full pipe can never stall it). While it runs,
    every `poll` seconds: abort set -> kill, raise Aborted; should_pause() -> kill, raise
    Paused; past `timeout` -> kill, raise RuntimeError; then on_poll() (a progress reader —
    its errors are swallowed: a progress bug must never kill the work). Whatever ends the
    wait, a child still running on the way out is killed and reaped."""
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                cwd=cwd, start_new_session=True)
        t0 = time.monotonic()
        try:
            while True:
                cpu = wait4_cpu(proc, block=False)
                if cpu is not None:
                    break
                if abort is not None and abort.is_set():
                    raise Aborted(f"aborted (run stopped) during {what}")
                if should_pause is not None and should_pause():
                    raise Paused(f"paused: yielded during {what} — it restarts on resume")
                if time.monotonic() - t0 > timeout:
                    raise RuntimeError(f"{what} timed out after {int(timeout)} s")
                if on_poll is not None:
                    try:
                        on_poll()
                    except Exception:
                        pass
                if abort is not None:
                    abort.wait(poll)
                else:
                    time.sleep(poll)
        finally:
            if proc.returncode is None:
                _kill_group(proc)
        return proc.returncode, _tail(log), cpu


def analysis_vf(cube: str | None, pre_vf: str | None = None) -> str:
    """Chain for the analysis intermediate: area-downscale to 1080p FIRST, then (with a LUT)
    SDR -> PQ RGB -> 4:4:4; without one (an already-PQ source) straight to 4:4:4. Scaling
    before the LUT does the LUT's per-pixel float work at a quarter of the pixels: the
    intermediate builds ~2x faster, and on a real Topaz chunk the L1 it measures moved by a
    median 0 codes (peak, worst 9-10), at most 1 code (average), scene cuts identical, L4 at
    most 1 code (measured 2026-10-15 against the LUT-first chain). A `pre_vf` front end (see
    front_end) runs before all of it, so the analysis measures the picture the encode ships."""
    scale = f"scale={ANALYSIS_WIDTH}:{ANALYSIS_HEIGHT}:flags=area"
    if cube:
        return front_end(pre_vf) + scale + "," + lut_rgb_chain(cube) + "," + pq_rgb_to_yuv("yuv444p10le")
    return front_end(pre_vf) + scale + ",format=yuv444p10le"


def uniform_pts(fps: str) -> str:
    """PURE. A setpts that stamps frame N at exactly N / fps from 0 ("24000/1001" ->
    setpts=N*1001/24000/TB). For a passthrough copy: the frames stay exactly the decoded ones,
    and their timestamps become regular whatever the source's were."""
    from fractions import Fraction
    f = Fraction(str(fps))
    return f"setpts=N*{f.denominator}/{f.numerator}/TB"


def build_analysis_intermediate_command(ffmpeg: str, chunk: str, out_mkv: str, cube: str | None,
                                        hwaccel: bool = True, *, start: int | None = None,
                                        frames: int | None = None, fps: str | None = None,
                                        pre_vf: str | None = None) -> list:
    """The lossless analysis intermediate of `chunk`: the whole file, or with `frames` (and
    `fps`) only frames [start, start+frames) of it — dvcap.build_seg_decode_command's seek:
    input `-ss` at the midpoint timestamp (frame-exact; frame 0 needs none), `-frames:v` to
    bound the count and `-fps_mode passthrough` so no frame is padded in or dropped.

    `hwaccel`: decode on Apple's media engine (VideoToolbox) — the decode is what bounds this
    step once the LUT runs at 1080p; the analysis loop retries once in software if the
    hardware decode refuses a unit.

    A range is also re-stamped from 0 (uniform_pts), so it is laid out like a chunk's
    intermediate. A passthrough copy of an MKV source's millisecond timestamps (reordered by
    B-frames, offset by the seek) can repeat one, and the L4 child's decode (rawvideo, which
    pads to a constant rate) then emitted 6 frames for a 5-frame range — measured 2026-10-15;
    the count gate would have failed that range on every retry."""
    ranged = frames is not None
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    if hwaccel:
        cmd += ["-hwaccel", "videotoolbox"]
    if ranged:
        ss = dvcap.seg_seek_seconds(int(start or 0), fps)
        if ss is not None:
            cmd += ["-ss", f"{ss:.6f}"]
    cmd += ["-i", chunk, "-map", "0:v:0"]
    if ranged:
        cmd += ["-frames:v", str(int(frames)), "-vf", uniform_pts(fps) + "," + analysis_vf(cube, pre_vf),
                "-fps_mode", "passthrough"]
    else:
        cmd += ["-vf", analysis_vf(cube, pre_vf)]
    return cmd + ["-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
                  "-color_range", "tv", "-c:v", "ffvhuff", "-pix_fmt", "yuv444p10le", out_mkv]


def build_raw444_decode_command(ffmpeg: str, src: str, size: tuple | None = None) -> list:
    """Raw yuv444p10le frames of `src` on stdout; `size` (w, h) area-scales them in the decode."""
    vf = ["-vf", f"scale={int(size[0])}:{int(size[1])}:flags=area"] if size else []
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", src,
            "-map", "0:v:0", *vf, "-pix_fmt", "yuv444p10le", "-f", "rawvideo", "-"]


def maxrgb_pq12_stats(frame, width: int, height: int, max_pq: int = SOURCE_MAX_PQ):
    """PURE (numpy). One yuv444p10le BT.2020nc limited frame -> (mean, std) of PQ max(R,G,B)
    in 12-bit codes, clamped to the declared source range (`max_pq`, the master's peak)."""
    import numpy as np
    a = np.frombuffer(frame, dtype="<u2").astype(np.float32)
    n = width * height
    y = (a[:n] - 64.0) / 876.0
    cb = (a[n:2 * n] - 512.0) / 896.0
    cr = (a[2 * n:3 * n] - 512.0) / 896.0
    r = y + 1.4746 * cr
    b = y + 1.8814 * cb
    g = (y - 0.2627 * r - 0.0593 * b) / 0.6780
    m = np.clip(np.maximum(np.maximum(r, g), b), 0.0, max_pq / PQ12_MAX) * PQ12_MAX
    return float(m.mean()), float(m.std())


def frame_l4(src: str, *, ffmpeg: str = FFMPEG, width: int = ANALYSIS_WIDTH,
             height: int = ANALYSIS_HEIGHT, scale: bool = False,
             max_pq: int = SOURCE_MAX_PQ) -> list:
    """Per-frame [anchor_pq, anchor_power] (rounded mean, std of PQ max-RGB) of a yuv444p10le
    PQ file — the analysis intermediate, so it costs one more decode of a file we already made.
    `scale`: decode it area-scaled to width x height (analyze_chunks measures L4 at 960x540:
    ~2x faster than 1080p, within a median 1-2 codes of it, worst 6 — far inside the native
    vs reference L4 difference of ~6-8). analyze_chunks runs this in a child
    (build_l4_command), never in the engine process."""
    size = width * height * 3 * 2
    p = subprocess.Popen(build_raw444_decode_command(ffmpeg, src, (width, height) if scale else None),
                         stdout=subprocess.PIPE)
    out = []
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            mean, std = maxrgb_pq12_stats(buf, width, height, max_pq)
            out.append([int(round(mean)), int(round(std))])
    finally:
        p.stdout.close()
        p.wait()
    if p.returncode != 0:
        raise RuntimeError(f"L4 decode failed (rc={p.returncode})")
    return out


L4_WIDTH, L4_HEIGHT = 960, 540  # the analysis loop's L4 resolution (scaled in the decode)
STREAMS = 1                     # analyze_chunks' parallel pipelines. Two ran 33% faster on 600-frame
                                # clips but SLOWER on full chunks beside an x265 remux (24 vs 27 fps on
                                # S06E06's chunks 11-14, 2026-10-15): x265 takes the cores either way.
                                # Raise it only on a measured gain (e.g. with the machine to itself).


def build_l4_command(python: str, src: str, out_json: str, *, ffmpeg: str = FFMPEG,
                     width: int = ANALYSIS_WIDTH, height: int = ANALYSIS_HEIGHT,
                     scale: bool = False, max_pq: int | None = None) -> list:
    """`python3 -m dvnative l4 ...` (run with cwd = ENGINE_DIR): frame_l4 of `src` as JSON.
    `--max-pq` only for a master other than the 1000-nit default, so today's argv is unchanged."""
    cmd = [python, "-m", "dvnative", "l4", src, out_json, "--ffmpeg", ffmpeg,
           "--size", f"{int(width)}x{int(height)}"] + (["--scale"] if scale else [])
    if max_pq is not None and int(max_pq) != SOURCE_MAX_PQ:
        cmd += ["--max-pq", str(int(max_pq))]
    return cmd


SCENE_MIN_KEYS = ("min_pq_12bit",)                      # a joined shot keeps the lowest of these,
SCENE_MAX_KEYS = ("max_pq_12bit",)                      # the highest of these, and a frame-weighted
SCENE_AVG_KEYS = ("avg_max_rgb_pq_12bit", "avg_luma_pq_12bit")   # mean of these


def join_scenes(a: dict, b: dict) -> dict:
    """PURE. Two adjacent scenes (a ends where b starts) as ONE: min of the minimums and max of
    the maximums (exactly what one pass over the joined frames gives), and the frame-weighted
    mean of the averages (the analyzer's averages are per-frame means over its scene)."""
    na, nb = a["end"] - a["start"] + 1, b["end"] - b["start"] + 1
    out = dict(a, end=b["end"])
    for k in SCENE_MIN_KEYS:
        if isinstance(a.get(k), int) and isinstance(b.get(k), int):
            out[k] = min(a[k], b[k])
    for k in SCENE_MAX_KEYS:
        if isinstance(a.get(k), int) and isinstance(b.get(k), int):
            out[k] = max(a[k], b[k])
    for k in SCENE_AVG_KEYS:
        if isinstance(a.get(k), int) and isinstance(b.get(k), int):
            out[k] = int(round((a[k] * na + b[k] * nb) / (na + nb)))
    return out


def merge_sidecars(parts: list, total: int | None = None, joins=()) -> dict:
    """PURE. [(first_frame, sidecar), ...] in order -> one sidecar over the whole stream.
    Scenes are offset by their chunk's first frame; light level is the max over chunks; a
    per-frame "l4" list is concatenated when every chunk has one. Refuses a gap or overlap
    between chunks, and a merged length that differs from `total` (the RPU must cover every
    frame exactly once).

    `joins`: first frames of parts that do NOT start on a scene cut (a range split only to bound
    the intermediate's size — analyze_ranges). There the previous part's last scene and this
    part's first are one shot (join_scenes), so a DV display never steps its tone mapping in
    the middle of a continuous shot. Topaz's chunks start on strong cuts: they pass none."""
    joins = {int(j) for j in joins or ()}
    scenes, expect, cll, fall, l4 = [], 0, 0, 0, []
    for first, sc in parts:
        n = stream_frames(sc)
        if first != expect:
            raise ValueError(f"chunk starts at {first}, expected {expect}")
        if leading_frames(sc):
            raise ValueError("a chunk intermediate must not have undecodable leading pictures")
        for i, s in enumerate(sc["scenes"]):
            s = dict(s, start=s["start"] + first, end=s["end"] + first)
            if i == 0 and first in joins and scenes and scenes[-1]["end"] + 1 == s["start"]:
                scenes[-1] = join_scenes(scenes[-1], s)
            else:
                scenes.append(s)
        ll = sc.get("light_level") or {}
        cll, fall = max(cll, ll.get("max_cll_nits") or 0), max(fall, ll.get("max_fall_nits") or 0)
        if l4 is not None:
            if sc.get("l4") is not None and len(sc["l4"]) == n:
                l4.extend(sc["l4"])
            else:
                l4 = None
        expect = first + n
    if total is not None and expect != total:
        raise ValueError(f"analysis covers {expect} frames, expected {total}")
    out = {"version": 5, "scenes": scenes,
           "source": {"stream_frames": expect, "leading_skipped_frames": 0}}
    if cll and fall:
        out["light_level"] = {"max_cll_nits": cll, "max_fall_nits": fall}
    if l4:
        out["l4"] = l4
    return out


def _rm_quiet(*paths) -> None:
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _load_chunk(side: str, l4_path: str | None, frames: int, unit: str = "chunk") -> tuple:
    """(sidecar with its "l4" list attached, None) for a published chunk (or range) that covers
    exactly `frames` frames; (None, why) for one that cannot be used."""
    try:
        sc = load_sidecar(side)
        if stream_frames(sc) != frames:
            return None, f"analysis covers {stream_frames(sc)} frames, the {unit} has {frames}"
        if l4_path:
            with open(l4_path) as f:
                sc["l4"] = json.load(f)
            got = len(sc["l4"]) if isinstance(sc["l4"], list) else None
            if got != frames:
                return None, f"L4 has {got} frames, the {unit} has {frames}"
        return sc, None
    except (OSError, ValueError) as e:
        return None, f"unreadable result: {e}"


class _EitherSet:
    """run_step's `abort` while chunks overlap: set when the caller's abort is set OR a sibling
    step failed (so a failure in one step kills the others instead of letting them run on)."""

    def __init__(self, outer, inner):
        self.outer, self.inner = outer, inner

    def is_set(self) -> bool:
        return bool(self.inner.is_set() or (self.outer is not None and self.outer.is_set()))

    def wait(self, timeout=None) -> bool:
        end = time.monotonic() + (timeout or 0)
        while not self.is_set():
            left = end - time.monotonic()
            if left <= 0:
                return False
            self.inner.wait(min(left, 0.25))
        return True


def analyze_chunks(chunks: list, workdir: str, *, cube: str | None, analyzer: str = HDR_ANALYZER,
                   ffmpeg: str = FFMPEG, peak_estimator: str = "max", with_l4: bool = True,
                   threads: int | None = None, abort=None, on_chunk=None, should_pause=None,
                   before_chunk=None, on_cpu=None, python: str | None = None,
                   nice: int = NICE, streams: int = STREAMS, pre_vf: str | None = None,
                   master: MasterSpec = DEFAULT_MASTER) -> dict:
    """Measure every chunk ([[first_frame, path], ..., [total, None]] — the end sentinel is
    required: it gives each chunk its frame count) and return the merged sidecar.

    OVERLAPPED (2026-10-15, to match the old DV stage's time): while hdr-analyze and the L4 pass
    run side by side on chunk k's intermediate, chunk k+1's intermediate is already being
    built — a chunk costs its slowest step instead of the sum of three. `streams` such
    pipelines run side by side, each taking every streams-th chunk (0, 2, 4 ... and 1, 3, 5
    ... at 2), because one pipeline leaves most of the machine idle; at most 2 x streams
    intermediates (~2.3 MB/frame at 1080p FFVHuff) exist at once.

    A finished chunk's sidecar (and its L4 list) is kept in `workdir` and reused on resume;
    intermediates are always deleted. Before a chunk that still needs work is STARTED:
      abort set                   -> Aborted
      should_pause()              -> Paused (also polled while each step runs; the step dies)
      before_chunk(k, frames)     -> a non-empty string pauses with that reason (the caller's
                                     disk floor); it may also raise Paused itself. `frames`
                                     also counts every chunk whose intermediate another
                                     stream is still writing, so the floor sees that too
    A pause or abort found while starting a chunk lets the chunks already in flight (in every
    stream) finish and publish first, and no stream starts another. Each step runs through
    run_step at `nice`, bounded by step_timeout(frames); a failing step kills every other
    step in every stream, and its own error is what the call raises. on_chunk(done, of)
    after every published chunk (resumed ones counted first); on_cpu(k, cpu_seconds, frames)
    for each chunk worked on in THIS call (os.wait4 user+sys over its steps): when it
    publishes, or on the way out for one that failed or was cut short. A chunk whose
    results do not cover exactly its frames (sidecar or L4) is deleted and fails the call,
    so a retry measures it again instead of reusing it. `pre_vf` (a front end) and `master`
    (its peak clamps L4) default to today's chain."""
    if not chunks or chunks[-1][1] is not None:
        raise ValueError("the chunk list must end with its [total, None] sentinel")
    real = [(a, p) for a, p in chunks if p]
    ends = [int(a) for a, _p in chunks[1:]]
    units = [(int(first), ends[k] - int(first), f"chunk_{k:04d}") for k, (first, _p) in enumerate(real)]
    return _analyze_units(
        units, int(chunks[-1][0]), workdir, unit="chunk",
        build_intermediate=lambda k, mkv, hw: build_analysis_intermediate_command(
            ffmpeg, real[k][1], mkv, cube, hwaccel=hw, pre_vf=pre_vf),
        analyzer=analyzer, ffmpeg=ffmpeg, peak_estimator=peak_estimator, with_l4=with_l4,
        threads=threads, abort=abort, on_done=on_chunk, should_pause=should_pause,
        before_unit=before_chunk, on_cpu=on_cpu, python=python, nice=nice, streams=streams,
        max_pq=master.source_max_pq)


def validate_ranges(ranges) -> list:
    """PURE. [(a, b), ...] half-open frame ranges as ints (dvcap.plan_segments' shape), refusing
    an empty plan, one not starting at frame 0, an empty range, and a gap or overlap."""
    out, expect = [], 0
    for i, r in enumerate(ranges or []):
        a, b = int(r[0]), int(r[1])
        if a != expect or b <= a:
            raise ValueError(f"range {i} is [{a}, {b}), expected one starting at {expect}")
        out.append((a, b))
        expect = b
    if not out:
        raise ValueError("no analysis ranges")
    return out


def analyze_ranges(path: str, ranges: list, workdir: str, *, fps: str, cube: str | None,
                   pre_vf: str | None = None, master: MasterSpec = DEFAULT_MASTER,
                   analyzer: str = HDR_ANALYZER, ffmpeg: str = FFMPEG, peak_estimator: str = "max",
                   with_l4: bool = True, threads: int | None = None, abort=None, on_range=None,
                   should_pause=None, before_range=None, on_cpu=None, python: str | None = None,
                   nice: int = NICE, streams: int = STREAMS, joins=()) -> dict:
    """analyze_chunks for ONE file measured in frame ranges ([(a, b), ...] from frame 0;
    validate_ranges) -> the merged sidecar over [0, last b).

    WHY RANGES: the intermediate is several MiB a frame (nativelane.INTERMEDIATE_BYTES_PER_FRAME),
    so a whole file cannot be one unit — a 231k-frame livestream would need well over 1 TiB.
    Each range is decoded with dvcap's frame-exact seek (build_analysis_intermediate_command's
    range form, in hardware like a chunk's, retried once in software), measured, published and
    its intermediate deleted — the same overlapped loop as analyze_chunks, so at most two
    ranges' intermediates exist per stream. The caller bounds each range's length (nativerender
    caps them); pass the item's scene plan as the ranges, so a range that starts on a scene cut
    joins its neighbour without a seam, exactly as Topaz's chunks do. `joins` names the range
    starts that are NOT scene cuts (a range split only for its size): merge_sidecars makes the
    shot either side of them one shot again.

    Resume, pause, abort, the per-step timeout, the before_range(k, frames) disk hook,
    on_range(done, of), on_cpu(k, cpu_s, frames) and the refusal of a result that does not
    cover exactly its range all follow analyze_chunks. A range's files are named by its index,
    first frame and length, so a different plan never reuses one."""
    from fractions import Fraction
    rs = validate_ranges(ranges)
    try:
        if Fraction(str(fps)) <= 0:
            raise ValueError
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"analyze_ranges needs the file's frame rate, got {fps!r}")
    units = [(a, b - a, f"range_{k:04d}_{a}_{b - a}") for k, (a, b) in enumerate(rs)]
    return _analyze_units(
        units, rs[-1][1], workdir, unit="range",
        build_intermediate=lambda k, mkv, hw: build_analysis_intermediate_command(
            ffmpeg, path, mkv, cube, hwaccel=hw, start=units[k][0], frames=units[k][1], fps=fps,
            pre_vf=pre_vf),
        analyzer=analyzer, ffmpeg=ffmpeg, peak_estimator=peak_estimator, with_l4=with_l4,
        threads=threads, abort=abort, on_done=on_range, should_pause=should_pause,
        before_unit=before_range, on_cpu=on_cpu, python=python, nice=nice, streams=streams,
        max_pq=master.source_max_pq, joins=joins)


def _analyze_units(units: list, total: int, workdir: str, *, unit: str, build_intermediate,
                   analyzer: str, ffmpeg: str, peak_estimator: str, with_l4: bool,
                   threads: int | None, abort, on_done, should_pause, before_unit, on_cpu,
                   python: str | None, nice: int, streams: int, max_pq: int, joins=()) -> dict:
    """The overlapped measuring loop analyze_chunks and analyze_ranges share. `units` is
    [(first_frame, frames, file_stem), ...] in order; build_intermediate(k, mkv, hwaccel) gives
    unit k's intermediate argv. Every rule in analyze_chunks' docstring is this loop's; `unit`
    ("chunk" / "range") only words its messages."""
    import concurrent.futures as cf
    os.makedirs(workdir, exist_ok=True)
    python = python or sys.executable
    n_units = len(units)

    def frames_of(k):
        return int(units[k][1])

    def names(k):
        stem = units[k][2]
        out_bin = os.path.join(workdir, stem + ".bin")
        return (out_bin, sidecar_path(out_bin), os.path.join(workdir, stem + ".l4.json"),
                os.path.join(workdir, stem + ".mkv"))

    def published(k):
        _b, side, l4_path, _m = names(k)
        return os.path.exists(side) and (not with_l4 or os.path.exists(l4_path))

    def gate(k, extra=0):
        if abort is not None and abort.is_set():
            raise Aborted("aborted (run stopped)")
        if should_pause is not None and should_pause():
            raise Paused(f"paused: yielded before analysis {unit} {k + 1} of {n_units}")
        if before_unit is not None:
            why = before_unit(k, frames_of(k) + extra)
            if why:
                why = str(why)
                raise Paused(why if why.startswith("paused:") else "paused: " + why)

    stop = threading.Event()            # a step failed: every other step dies
    no_new = threading.Event()          # a gate refused: no stream starts another unit
    guard = _EitherSet(abort, stop)
    cpu, lock, reported = {}, threading.Lock(), set()
    building = set()                    # units whose intermediate is still being written
    errors = []                         # what each stream ended on
    counts = {"done": 0}

    def step(k, cmd, what, cwd=None):
        rc, tail, used = run_step(niced(cmd, nice), timeout=step_timeout(frames_of(k)), abort=guard,
                                  should_pause=should_pause, cwd=cwd, what=f"{unit} {k + 1} {what}")
        with lock:
            cpu[k] = cpu.get(k, 0.0) + (used or 0.0)
        return rc, tail

    def run_build(k):
        mkv = names(k)[3]
        try:
            rc, tail = step(k, build_intermediate(k, mkv, True), "intermediate")
            if rc != 0:                 # the hardware decode refused it: once more in software
                _rm_quiet(mkv)
                rc, tail = step(k, build_intermediate(k, mkv, False), "intermediate (software decode)")
            if rc != 0:
                raise RuntimeError(f"{unit} {k}: intermediate failed: {tail[-300:]}")
        finally:
            with lock:                  # written (or abandoned): the disk floor sees it now
                building.discard(k)

    def run_analyzer(k):
        out_bin, _s, _l, mkv = names(k)
        rc, tail = step(k, build_analyzer_command(analyzer, mkv, out_bin + ".part",
                                                  peak_estimator=peak_estimator, threads=threads),
                        "analyzer")
        if rc != 0 or not os.path.exists(sidecar_path(out_bin + ".part")):
            raise RuntimeError(f"{unit} {k}: analyzer failed: {tail[-300:]}")

    def run_l4(k):
        _b, _s, l4_path, mkv = names(k)
        rc, tail = step(k, build_l4_command(python, mkv, l4_path + ".part", ffmpeg=ffmpeg,
                                            width=L4_WIDTH, height=L4_HEIGHT, scale=True,
                                            max_pq=max_pq),
                        "L4", cwd=ENGINE_DIR)
        if rc != 0 or not os.path.exists(l4_path + ".part"):
            raise RuntimeError(f"{unit} {k}: L4 failed: {tail[-300:]}")

    def publish(k):
        out_bin, side, l4_path, mkv = names(k)
        if with_l4:
            os.replace(l4_path + ".part", l4_path)
        load_sidecar(sidecar_path(out_bin + ".part"))       # never publish an invalid one
        os.replace(sidecar_path(out_bin + ".part"), side)   # publish last: resume trusts it
        _rm_quiet(mkv, out_bin + ".part")

    def first_error(futures):
        """Wait for all of them; the first REAL error (a sibling killed by the stop event
        only raises Aborted because of it)."""
        cf.wait(futures)
        errs = [f.exception() for f in futures if f.exception() is not None]
        real_errs = [e for e in errs if not (isinstance(e, Aborted) and stop.is_set()
                                             and not (abort is not None and abort.is_set()))]
        return (real_errs or errs or [None])[0]

    todo = [k for k in range(n_units) if not published(k)]
    counts["done"] = n_units - len(todo)
    if on_done:
        for i in range(counts["done"]):
            on_done(i + 1, n_units)

    def start(k):
        """Gate unit k and start its intermediate -> (k, future), or None when a gate
        elsewhere already refused (this stream just stops)."""
        if no_new.is_set():
            return None
        with lock:
            extra = sum(frames_of(j) for j in building)
        gate(k, extra)
        with lock:
            building.add(k)
        return k, pool.submit(run_build, k)

    def run_stream(ks):
        """One overlapped pipeline over units `ks`: build k+1 while k is measured."""
        try:
            queue = iter(ks)
            pending, deferred = None, None
            k0 = next(queue, None)
            if k0 is not None:
                pending = start(k0)
            while pending is not None:
                k, fut = pending
                pending = None
                err = first_error([fut])
                if err is not None:
                    raise err
                nk = next(queue, None)
                if nk is not None:
                    try:
                        pending = start(nk)
                    except (Paused, Aborted) as e:
                        deferred = e    # unit k (and the other streams' units) finish first
                        no_new.set()
                measure = [pool.submit(run_analyzer, k)] + ([pool.submit(run_l4, k)] if with_l4 else [])
                done, _ = cf.wait(measure, return_when=cf.FIRST_EXCEPTION)
                if any(f.exception() is not None for f in done):
                    stop.set()          # kill every other step, in every stream
                    raise first_error(measure + ([pending[1]] if pending else []))
                publish(k)
                with lock:
                    counts["done"] += 1
                    reported.add(k)
                    if on_cpu:
                        on_cpu(k, cpu.get(k, 0.0), frames_of(k))
                    if on_done:
                        on_done(counts["done"], n_units)
                if deferred is not None:
                    raise deferred
        except BaseException as e:
            with lock:
                errors.append(e)
            no_new.set()
            if not isinstance(e, (Paused, Aborted)):
                stop.set()

    width = max(1, int(streams or 1))
    lanes = [ks for ks in (todo[i::width] for i in range(width)) if ks]
    pool = cf.ThreadPoolExecutor(max_workers=3 * max(1, len(lanes)))
    try:
        workers = [threading.Thread(target=run_stream, args=(ks,), daemon=True) for ks in lanes]
        for t in workers:
            t.start()
        for t in workers:
            t.join()
        if errors:
            stop.set()                  # nothing keeps running behind a failure or a pause

            def caused_by_stop(e):      # a sibling killed because another step failed
                return isinstance(e, Aborted) and not (abort is not None and abort.is_set())
            real_errs = [e for e in errors if not isinstance(e, (Paused, Aborted))]
            raise (real_errs or [e for e in errors if isinstance(e, Aborted) and not caused_by_stop(e)]
                   or [e for e in errors if isinstance(e, Paused)] or errors)[0]
    finally:
        stop.set()
        pool.shutdown(wait=True)
        if on_cpu:                      # a unit that failed or was cut short still used CPU
            for k in sorted(set(cpu) - reported):
                on_cpu(k, cpu[k], frames_of(k))
        for k in todo:
            if not published(k):
                out_bin, _side, l4_path, mkv = names(k)
                _rm_quiet(mkv, out_bin + ".part", sidecar_path(out_bin + ".part"), l4_path + ".part")
    parts = []
    for k, (first, frames, _stem) in enumerate(units):
        _b, side, l4_path, _m = names(k)
        sc, bad = _load_chunk(side, l4_path if with_l4 else None, frames, unit)
        if bad:
            _rm_quiet(side, l4_path)            # never reuse it: the retry measures it again
            raise RuntimeError(f"{unit} {k}: {bad}")
        parts.append((first, sc))
    return merge_sidecars(parts, total=total, joins=joins)


# ---- dovi_tool generate config ----------------------------------------------------------

def neutral_l2(target_nits: int) -> dict:
    return {"Level2": {"target_max_pq": nits_to_pq12(target_nits),
                       "trim_slope": NEUTRAL, "trim_offset": NEUTRAL, "trim_power": NEUTRAL,
                       "trim_chroma_weight": NEUTRAL, "trim_saturation_gain": NEUTRAL,
                       "ms_weight": NEUTRAL}}


def clamp_l1(mn: int, mx: int, avg: int, master: MasterSpec = DEFAULT_MASTER):
    """PURE. L1 inside the declared source range: max <= the master's peak code (3079 for the
    default 1000 nits), avg <= max, min <= avg. The pilot measured scene peaks up to 4025
    (~8500 nits) on a 1000-nit master; dovi_tool passes those straight through (it only floors
    max at 2081 and avg at the CM floor)."""
    mx = min(int(mx), master.source_max_pq)
    avg = min(int(avg), mx)
    return min(int(mn), avg), mx, avg


AVG_FLOORS = ("V40", "V29")      # CM v4.0 floor 1229 (the reference minimum; the default) or
                                 # hdr-analyze's v2.9 floor 819 (keeps the measured average)


def rpu_config(sc: dict, *, max_cll: int | None = None, max_fall: int | None = None,
               l4: bool = True, avg_floor: str = "V40", master: MasterSpec = DEFAULT_MASTER,
               light: tuple | None = None) -> dict:
    """PURE. Sidecar -> `dovi_tool generate` JSON for Profile 8.1, CM v4.0.

    Shots follow the analyzer's scenes. Undecodable leading pictures come first in
    presentation order, so every scene but the first moves back by their count and the first
    also covers them (hdr-analyze's rule). The L1 average uses the CM v4.0 floor (1229, the
    generator's default and the reference masters' minimum) rather than hdr-analyze's v2.9 choice of
    819 (`avg_floor="V29"` selects that instead, for the blind A/B). `long_play_mode` flags
    a scene refresh on every frame, as the reference masters do. With a per-frame "l4" list, every frame
    carries Level4 through a frame edit. `master` sets the mastering range, the source range
    L1 is clamped to and the L2 targets; its default is the reference 1000-nit master. `light`
    (MaxCLL, MaxFALL) is declared in L6 EXACTLY as given — an HDR source's own content light
    level (source_mastering), which the stream's HDR10 SEI says too — and then max_cll /
    max_fall and the measurement are not used; None (the default) keeps the rule above."""
    if avg_floor not in AVG_FLOORS:
        raise ValueError(f"avg_floor must be one of {AVG_FLOORS}")
    peak = int(master.max_nits)
    if light is not None:
        max_cll, max_fall = int(light[0]), int(light[1])
    else:
        if max_cll is None or max_fall is None:
            m_cll, m_fall = light_levels(sc, master)
            max_cll = max_cll if max_cll is not None else (m_cll or peak)
            max_fall = max_fall if max_fall is not None else (m_fall or peak)
        max_cll = min(int(max_cll), peak)
        max_fall = min(int(max_fall), max_cll)
    lead = leading_frames(sc)
    frames_l4 = sc.get("l4") if l4 else None
    length = stream_frames(sc)
    if frames_l4 is not None and len(frames_l4) != length:
        raise ValueError(f"L4 has {len(frames_l4)} frames, the stream {length}")
    shots = []
    for i, s in enumerate(sc["scenes"]):
        start = 0 if i == 0 else s["start"] + lead
        duration = s["end"] + lead - start + 1
        mn, mx, avg = clamp_l1(s["min_pq_12bit"], s["max_pq_12bit"], s["avg_max_rgb_pq_12bit"], master)
        shot = {"start": start, "duration": duration,
                "metadata_blocks": [{"Level1": {"min_pq": mn, "max_pq": mx, "avg_pq": avg}}]}
        if frames_l4 is not None:
            shot["frame_edits"] = [
                {"edit_offset": k, "metadata_blocks": [{"Level4": {
                    "anchor_pq": min(int(frames_l4[start + k][0]), PQ12_MAX),
                    "anchor_power": min(int(frames_l4[start + k][1]), PQ12_MAX)}}]}
                for k in range(duration)]
        shots.append(shot)
    blocks = [neutral_l2(t) for t in master.trim_targets]
    blocks.append({"Level9": {"length": 1, "source_primary_index": SOURCE_PRIMARY_BT2020}})
    blocks.append({"Level11": {"content_type": L11_CONTENT_TYPE, "whitepoint": 0,
                               "reference_mode_flag": False}})
    cfg = {
        "profile": "8.1",
        "cm_version": "V40",
        "long_play_mode": True,
        "length": length,
        "source_min_pq": master.source_min_pq,
        "source_max_pq": master.source_max_pq,
        "level5": {"active_area_left_offset": 0, "active_area_right_offset": 0,
                   "active_area_top_offset": 0, "active_area_bottom_offset": 0},
        "level6": {"max_display_mastering_luminance": peak,
                   "min_display_mastering_luminance": master.l6_min,
                   "max_content_light_level": max_cll,
                   "max_frame_average_light_level": max_fall},
        "default_metadata_blocks": blocks,
        "shots": shots,
    }
    if avg_floor == "V29":
        cfg["l1_avg_pq_cm_version"] = "V29"
    return cfg


def build_generate_command(dovi_tool: str, config_json: str, rpu_out: str) -> list:
    return [dovi_tool, "generate", "-j", config_json, "-o", rpu_out]


def generate_rpu(sidecar, rpu_out: str, *, dovi_tool: str = DOVI_TOOL, l4: bool = True,
                 avg_floor: str = "V40", max_cll: int | None = None, max_fall: int | None = None,
                 timeout: int = 600, master: MasterSpec = DEFAULT_MASTER,
                 light: tuple | None = None) -> dict:
    """Sidecar (file path, or an already-merged dict) -> binary RPU at `rpu_out` (atomic:
    written to .part, then renamed). Returns the config used. Raises RuntimeError when
    dovi_tool refuses it. `master` and `light` as in rpu_config (defaults: today's RPU, byte
    for byte)."""
    sc = load_sidecar(sidecar) if isinstance(sidecar, str) else sidecar
    validate_sidecar(sc)
    cfg = rpu_config(sc, max_cll=max_cll, max_fall=max_fall, l4=l4, avg_floor=avg_floor,
                     master=master, light=light)
    cfg_path = rpu_out + ".generate.json"
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    part = rpu_out + ".part"
    if os.path.exists(part):
        os.remove(part)
    r = subprocess.run(build_generate_command(dovi_tool, cfg_path, part),
                       capture_output=True, text=True, errors="replace", timeout=timeout)
    if r.returncode != 0 or not os.path.exists(part):
        raise RuntimeError("dovi_tool generate failed: " + (r.stderr or r.stdout)[-400:])
    os.replace(part, rpu_out)
    return cfg


# ---- child entry point ------------------------------------------------------------------

def main(argv=None) -> int:
    """`python3 -m dvnative l4 <intermediate.mkv> <out.json> [--ffmpeg PATH] [--size WxH]
    [--scale] [--max-pq N]`: frame_l4 of the intermediate written as JSON. The analysis loop
    runs this as a child (nice 10, killable, its numpy memory gone when it exits) and
    publishes `out` only on exit status 0."""
    import argparse
    ap = argparse.ArgumentParser(prog="dvnative")
    sub = ap.add_subparsers(dest="cmd")
    l4 = sub.add_parser("l4", help="per-frame L4 of a yuv444p10le PQ file, as JSON")
    l4.add_argument("src")
    l4.add_argument("out")
    l4.add_argument("--ffmpeg", default=FFMPEG)
    l4.add_argument("--size", default=f"{ANALYSIS_WIDTH}x{ANALYSIS_HEIGHT}")
    l4.add_argument("--scale", action="store_true", help="area-scale the decode to --size")
    l4.add_argument("--max-pq", type=int, default=SOURCE_MAX_PQ)
    a = ap.parse_args(argv)
    if a.cmd != "l4":
        ap.print_usage(sys.stderr)
        return 2
    try:
        width, height = (int(v) for v in a.size.lower().split("x"))
    except ValueError:
        print(f"--size must be WxH, got {a.size!r}", file=sys.stderr)
        return 2
    if not 0 < a.max_pq <= PQ12_MAX:
        print(f"--max-pq must be a 12-bit PQ code, got {a.max_pq}", file=sys.stderr)
        return 2
    data = frame_l4(a.src, ffmpeg=a.ffmpeg, width=width, height=height, scale=a.scale,
                    max_pq=a.max_pq)
    with open(a.out, "w") as f:
        json.dump(data, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
