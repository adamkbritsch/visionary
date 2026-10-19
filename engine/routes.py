"""Which ROUTE an item takes to its Dolby Vision master — a name for every outcome of plan.py
and companion.py. The orchestrator logs that name once per item after the CFR stage, and
stages.native_route reads it to plan the native render ('dvrender') of an item the chunk lane
(nativelane) does not take.

classify() picks no engine itself. It only NAMES what the choices made elsewhere
(plan.choose_plan, companion.build_verdict, nativelane.eligible, the per-item output mode)
already do.

  route              the item                                          its Dolby Vision
  -----------------  ------------------------------------------------  ----------------------------
  tv_sdr_topaz       TV, BT.709-era SDR (720p+ bucket), Topaz upscale  the chunk lane, else the
                                                                       render from the chunks
  tv_disk_fallback   ...one the chunk lane declined for disk           the render from the chunks
  sd_sdr_topaz       TV, 480p/576p bucket (BT.601-era SD), Topaz       the chunk lane (601 front
                                                                       end), else the render
  movie_sdr_topaz    a sub-4K SDR movie, Topaz upscale                 the render from the chunks
  hdr_topaz          sub-4K PQ or HLG (TV or movie), Topaz upscale     the render, no LUT
  rpu_only           4K HDR10 HEVC Main10 at a constant frame rate     analysed as it is; the RPU
                                                                       rides the original bits
  hdr_reencode       PQ that cannot carry an RPU (not HEVC, not        the render, converted
                     10-bit, or a variable frame rate), and every
                     other YouTube PQ
  hlg                4K HLG, and any YouTube HLG                       the render, HLG -> PQ
  sdr4k              4K SDR (not YouTube), no upscale                  the render through the LUT
  yt_native4k        YouTube SDR, landscape, >= 1700 lines             ...at 14 Mbps
  yt_1080            YouTube SDR, 900-1200 lines                       ...a 2x Lanczos fit, 20 Mbps
  yt_plain           YouTube SDR, everything else (480p, 720p,         ...a plain fit, 20 or 14 Mbps
                     1440p, vertical, unprobed)
  combine_rpu        companion combine, either copy has real DV        no DV step (the real RPU)
  combine_nodv       companion combine, neither copy has DV            the render analyses the winner
  sdr_out            any item pinned to SDR                            the SDR render (no DV)
  already_dv         the source is already Dolby Vision                refused; no DV step
  dvp7               DV profile 7 (the dvlane conversion)              no DV step (its own lane)

ORDER MATTERS, and it is the stages' own order: a combine's verdict outranks the plan's
already-DV skip; an output-mode PIN outranks the kind of item for every rendered route; rpu-only
outranks YouTube (stages._remux tests it first); and HDR outranks the yt_* split, because every
yt_* render runs the BT.709 SDR LUT — a YouTube video in PQ or HLG (none so far) must never be
named into one.

THE 2000-NIT MODE IS RETIRED (user decision 2026-10-15, built 2026-10-18): there is no dv2000
route any more. settings reads a profile still pinned to "dv2000" as "dv1000", and
normalize_mode does the same with anything it is handed — so such an item is named by its kind,
exactly like an unpinned one.
"""
from __future__ import annotations
import os

# The route ids, in the table's order, each with the one line the log prints beside it.
ROUTES = {
    "tv_sdr_topaz": "TV, 720p+ SDR, Topaz upscale, dv1000",
    "tv_disk_fallback": "TV lane declined for disk (otherwise tv_sdr_topaz)",
    "sd_sdr_topaz": "TV, 480p/576p SD, Topaz upscale",
    "movie_sdr_topaz": "sub-4K SDR movie, Topaz upscale",
    "hdr_topaz": "sub-4K HDR, Topaz upscale (adds DV)",
    "rpu_only": "4K HDR10 HEVC Main10 CFR, RPU added to the original stream",
    "hdr_reencode": "PQ that cannot carry an RPU as it is, converted",
    "hlg": "HLG, converted to PQ",
    "sdr4k": "4K SDR, no upscale (adds HDR + DV)",
    "yt_native4k": "YouTube, native 4K landscape (no scaling)",
    "yt_1080": "YouTube, 900-1200 lines (2x Lanczos)",
    "yt_plain": "YouTube, plain fit (480p/720p/1440p/vertical)",
    "combine_rpu": "companion combine with a real DV RPU (no DV step)",
    "combine_nodv": "companion combine, neither copy has DV",
    "sdr_out": "pinned to the SDR output",
    "already_dv": "already Dolby Vision (refused, no DV step)",
    "dvp7": "DV profile 7 -> 8.1 lane (no DV step)",
}
UNKNOWN = "unknown"             # the facts do not name a route (no plan, no combine verdict)
NO_DV_STEP = frozenset(("combine_rpu", "already_dv", "dvp7"))   # never rendered, on any setting

MODES = ("sdr", "dv1000")             # = settings.OUTPUT_MODES without "auto" (test-pinned)
HLG = "arib-std-b67"                  # plan.HDR_TRANSFERS' HLG member (test-pinned)
TV_LANE_RES = ("720p", "1080p")       # = nativelane.ELIGIBLE_RES (test-pinned)
SD_RES = "480p"                       # plan.resolution_bucket's SD bucket (480p and 576p)
YT_NATIVE_4K_MIN_HEIGHT = 1700        # = stages.YOUTUBE_NATIVE_4K_MIN_HEIGHT (test-pinned)
YT_1080_MIN_HEIGHT = 900              # the yt_1080 band: `if 900 <= h <= 1200`
YT_1080_MAX_HEIGHT = 1200


def describe(route) -> str:
    """The route's one-line description for the log ('' for an id this table does not know)."""
    if route == UNKNOWN:
        return "not enough facts to name a route"
    return ROUTES.get(route, "")


def normalize_mode(mode) -> str:
    """The output mode an item is made in: a real mode as given, anything else
    ("auto", None, junk, the retired "dv2000") dv1000 — the same rule as
    settings.effective_output_mode."""
    return mode if mode in MODES else "dv1000"


def _dv_major(profile):
    """A DV profile as its major number: 7 / "7" / "7.6" / "7.x" -> 7, "8.1" -> 8; else None.
    companion.probe_media reports strings ("8.1", "7.x"), movies.py ints."""
    if isinstance(profile, bool):
        return None
    if isinstance(profile, int):
        return profile
    head = str(profile or "").strip().split(".")[0]
    return int(head) if head.isdigit() else None


def _geometry(plan: dict, facts: dict) -> tuple:
    """(width, height) of the source: the plan's probe, 0 when absent or unreadable. facts may override either."""
    info = plan.get("input") if isinstance(plan.get("input"), dict) else {}
    out = []
    for key in ("width", "height"):
        try:
            out.append(int(facts.get(key, info.get(key)) or 0))
        except (TypeError, ValueError):
            out.append(0)
    return tuple(out)


def _transfer(plan: dict, facts: dict):
    info = plan.get("input") if isinstance(plan.get("input"), dict) else {}
    return facts.get("transfer", info.get("transfer"))


def _youtube(plan: dict, facts: dict) -> str:
    """The SDR YouTube split. yt_1080 is the 900-1200-line band (any orientation: the rule reads
    the height alone), a plain 2x Lanczos. yt_native4k is the landscape half of the 14 Mbps
    bucket — the frames the LUT alone can master; a vertical video in that bucket needs a fit and
    a pad, so it is plain. Everything else (480p, 720p, 1201-1699 lines, unprobed) is plain too."""
    width, height = _geometry(plan, facts)
    if YT_1080_MIN_HEIGHT <= height <= YT_1080_MAX_HEIGHT:
        return "yt_1080"
    if height >= YT_NATIVE_4K_MIN_HEIGHT and width >= height:
        return "yt_native4k"
    return "yt_plain"


def classify(p, plan, mode, facts=None) -> str:
    """PURE: the route id for item `p` (its movie / youtube / combine flags), `plan`
    (plan.choose_plan's dict for the ORIGINAL source, with plan_for's "input" probe when
    there is one), `mode` (the item's output-mode setting; normalized here) and `facts`:
      verdict     combine only: companion.confirmed_verdict(...)["verdict"] (None = lost)
      dv_profile  a DV source's profile (7 -> dvp7)
      disk_fits   False when the native lane declined a tv_sdr_topaz item for disk (its
                  nativelane.disk_fits verdict); absent or None when it was never asked
      width, height, transfer   override the plan's probe
    Every outcome is one of ROUTES, or UNKNOWN when the facts cannot name one. It reads no
    file, setting or clock, writes nothing and never raises (garbage in -> UNKNOWN)."""
    try:
        return _classify(p, plan, mode, facts)
    except Exception:  # noqa: BLE001 — a name for a log line must never cost the item
        return UNKNOWN


def _classify(p, plan, mode, facts) -> str:
    facts = facts if isinstance(facts, dict) else {}
    plan = plan if isinstance(plan, dict) else {}
    mode = normalize_mode(mode)
    youtube = bool(getattr(p, "youtube", False))
    movie = bool(getattr(p, "movie", False))

    # A COMBINE is decided by its verdict before anything else: a real RPU (either copy) skips
    # the DV step whatever the plan or the mode says; the no-RPU fallback renders the winner. Without a confirmed verdict the stages refuse the item permanently.
    if getattr(p, "combine", False):
        verdict = facts.get("verdict")
        if not isinstance(verdict, dict) or not verdict.get("rpu_from"):
            return UNKNOWN
        if verdict.get("rpu_from") != "native":
            return "combine_rpu"
        return "sdr_out" if mode == "sdr" else "combine_nodv"

    # ALREADY DOLBY VISION: never rendered (a profile 7 has a lane of its own).
    major = _dv_major(facts.get("dv_profile"))
    if major == 7:
        return "dvp7"
    if major or "skip" in (plan.get("topaz"), plan.get("dv")):
        return "already_dv"

    topaz = plan.get("topaz")
    if topaz not in ("upscale", "rpu-only", "dv-only"):
        return UNKNOWN                  # no plan (or one this table does not know)

    # A PIN makes every remaining route the SDR render (SDR: the only pin left).
    if mode == "sdr":
        return "sdr_out"

    if topaz == "rpu-only":             # ahead of YouTube, as in stages._remux
        return "rpu_only"
    hdr = bool(plan.get("is_hdr")) or plan.get("dv") == "add_dv"
    if hdr:
        if topaz == "upscale" and not youtube:   # YouTube never runs Topaz (stages._topaz)
            return "hdr_topaz"
        return "hlg" if _transfer(plan, facts) == HLG else "hdr_reencode"
    if youtube:
        return _youtube(plan, facts)
    if topaz == "dv-only":
        return "sdr4k"
    # The SDR Topaz upscale.
    if movie:
        return "movie_sdr_topaz"        # a movie first: its disk model is what differs
    if plan.get("res") == SD_RES:
        return "sd_sdr_topaz"
    if plan.get("res") in TV_LANE_RES:
        return "tv_disk_fallback" if facts.get("disk_fits") is False else "tv_sdr_topaz"
    return UNKNOWN


def output_mode(p) -> str:
    """The output mode `p` is made in: its per-item override, read under the
    same key (stages._settings_scope — a YouTube import batch's own scope), normalized."""
    import settings
    import stages
    return normalize_mode(settings.get_show_output_mode(stages._settings_scope(p)))


def item_route(p, mode=None):
    """classify() for a live item whose source (and CFR) are on disk, with the facts read the
    way the stages read them: the ORIGINAL source's plan (plan_for's probe and plan, minus its
    probe-cache write — this changes nothing), the output mode, and a combine's confirmed
    verdict. None when the source is not local yet, or anything at all goes wrong: it NEVER
    raises (its caller is a log line). The disk fallback is never reported here — the lane
    asks about disk only after Topaz, long after this runs. `mode` names the item under that
    output mode instead of its own (stages' sdr_out asks "dv1000": what kind of item a pinned
    one is underneath — its route had it not been pinned)."""
    try:
        src = getattr(p, "source", None)
        if not src or not os.path.exists(src):
            return None
        import plan as plan_mod
        info = plan_mod.probe_input(src)
        pl = plan_mod.choose_plan(info)
        pl["input"] = info
        facts = {}
        if getattr(p, "combine", False):
            import companion
            cv = companion.confirmed_verdict(p.source_basename)
            facts["verdict"] = cv.get("verdict") if isinstance(cv, dict) else None
        return classify(p, pl, output_mode(p) if mode is None else mode, facts)
    except Exception:  # noqa: BLE001
        return None
