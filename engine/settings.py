"""User-adjustable settings + per-show Topaz preset selection.

Two JSON files under ~/.topaz-pipeline/ (atomic writes):
  settings.json       — global knobs (power policy, disk floor, quiet mode, …)
  show_profiles.json  — { "<show name>": "<preset key>" }

Topaz presets are a FIXED CATALOG — all SDR ProRes 4444 XQ. They differ ONLY by
what the footage needs (the camera / the kind of content), NOT by output format:
DaVinci Resolve adds HDR + Dolby Vision afterward in every scenario. A show is
assigned ONE preset (you only CHOOSE, never hand-tune params in the app); the
Topaz stage looks it up by series name. `scale` is NOT part of a preset — it's
decided by the input plan (2× for 1080p, 1× for already-4K). See plan.py.
"""
from __future__ import annotations
import json
import re
import os
import threading

# The dashboard is a ThreadingHTTPServer: two handlers can hit set_settings at once (e.g.
# a Settings toggle save racing Activate/Deactivate). The read-modify-write must be atomic
# or a stale copy resurrects the old `activated` value.
_WRITE_LOCK = threading.Lock()

CONFIG_DIR = os.path.expanduser("~/.topaz-pipeline")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
PROFILES_FILE = os.path.join(CONFIG_DIR, "show_profiles.json")

# HARD ceiling on the round-robin, independent of the `max_active_shows` setting. Reads of the
# active list truncate at THIS, never at the setting: lowering the setting while shows are
# already running must not silently drop one (see series.max_active / get_active_series).
MAX_ACTIVE_CEILING = 4

# Longest Screen Control may be switched off for. It is a TIMED pause, never a latch:
# while it is off, items hold before Resolve and their ~190 GiB Topaz intermediates pile up
# against the min_free_gb floor, so a forgotten "off" stalls the run on low disk instead of
# doing anything useful. Four hours is about where the buffer runs out (2-3 items at
# 1-2 h of Topaz each), so that is the ceiling — and every pause self-cancels.
MAX_QUIET_SECONDS = 4 * 3600
MIN_QUIET_SECONDS = 60


def clamp_quiet_seconds(v) -> int:
    """Seconds a Screen Control pause may last: 0 (turn it straight back on) or a value
    inside [MIN_QUIET_SECONDS, MAX_QUIET_SECONDS]. Junk becomes the 1 h default."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 3600
    if n <= 0:
        return 0
    return max(MIN_QUIET_SECONDS, min(MAX_QUIET_SECONDS, n))

# Global, user-adjustable — everything here is UNIVERSAL (per-show options live in
# show_profiles.json). The block after `youtube_every_tv_episodes` are SCHEDULING/CAPACITY
# knobs that used to be hardcoded constants in orchestrator.py / series.py; each defaults to
# the exact constant it replaced, so an untouched install behaves identically. They decide
# what runs and how much at once — never how a file is encoded.
DEFAULT_SETTINGS = {
    "activated": False,         # APPLIANCE mode: persisted arm state. While True the app runs
                                # whenever it can — the server re-enables the orchestrator on
                                # launch (see server._rearm_loop). A run ends only on a manual stop.
    "quiet_until": 0,           # SCREEN CONTROL is only ever turned off TEMPORARILY: this is the epoch
                                # second it comes back on by itself (0 = not currently timed). Holding
                                # items before Resolve piles up ~190 GiB ProRes intermediates against the
                                # min_free_gb floor, so an indefinite "off" would quietly stall the run
                                # on low disk — hence MAX_QUIET_SECONDS. The expiry is enforced in the
                                # ENGINE (orchestrator._quiet_mode), so it survives an app relaunch.
    "quiet_mode": False,        # QUIET MODE: keep download+topaz running but DEFER each item before the
                                # screen-invasive Resolve stage, so the laptop stays usable. Items pile up
                                # (no drain to remux/upload/cleanup) → the run pauses on low disk until off.
    "min_adapter_watts": 140,   # power SUFFICIENCY = the brick: >= this wattage adapter → run;
                                # anything less (hub/monitor PD, battery) → full passive pause
    "poll_minutes": 30,
    "dim_after_minutes": 15,    # AUTO-DIM: after this many minutes with no user input (while a run holds
                                # the display caffeinated), drop the backlight to 0 to save the panel.
                                # Does NOT auto-restore on activity — tap the brightness key to bring it
                                # back. 0 = Off (never dim). See orchestrator._dimmer / brightness.dim_tick.
    "audio_target_lufs": -16,   # SMART LOUDNESS BOOST target: the remux stage measures each item's
                                # integrated LUFS and boosts (never attenuates) to this, limiter at
                                # -2 dB. MP4/AAC path only (MKV = lossless audio, never transcoded).
                                # 0 = off. Derived from the Office pilot (-23 LUFS measured).
    "max_peak_mbps": 50,        # PEAK BITRATE CAP for every shipped master: the remux stage re-encodes
                                # the Resolve DV render through x265 with a hard VBV ceiling at this
                                # rate (dvcap.py). Resolve's VideoToolbox export spikes to ~139 Mbps on
                                # a ~27 Mbps average, which glitches players; ~2x the average only clips
                                # the pathological seconds. NO uncapped fallback — cap fails => stage fails.
                                # LIMIT capped at 62 so cap + gate tolerance + TrueHD headroom stays
                                # under the SHIELD's ~80 Mbps whole-stream DV ceiling (dvcap constants).
    "passthrough_min_mbps": 12, # HIGH-BITRATE 4K FAST PATH: a 3840x2160 HEVC 10-bit CFR source whose
                                # VIDEO bitrate is at/above this skips Topaz entirely. HDR10 (PQ) intake
                                # keeps its ORIGINAL stream and gets Resolve's Dolby Vision RPU injected
                                # (no re-encode); SDR intake ships Resolve's HDR+DV conversion through
                                # the normal capped remux. Sized so WWDITS-tier 4K web-DLs (~15 Mbps)
                                # qualify while starved 4K still gets the full Topaz cleanup. 0 = off.
    "resolve_share_remuxes": 0, # When Resolve runs it is the ONLY thing running (user-dictated
                                # 2026-08-06, reversing the same-day sharing experiment): 0 = never
                                # share, every Resolve takes the whole machine (SIGSTOP). Raising it
                                # opts back in: up to this many remux lanes keep encoding beside a
                                # FAST-PATH (rpu-only/resolve-only) item's Resolve pass only.
    "max_youtube_minutes": 20,  # YouTube: the per-channel length-cap threshold (applied only to channels
                                # whose 'capped' toggle is on).
    "youtube_every_tv_episodes": 2,  # YouTube CADENCE: serve exactly 1 YouTube video after every N TV
                                # episodes (was: a ~max_youtube_minutes batch every turn). Throttles the
                                # slow 4K-SDR YouTube upscales so they don't crowd out TV. If TV runs out,
                                # YouTube drains freely regardless of N.

    # --- scheduling / capacity (was: hardcoded constants; read via tunable()) ---
    "max_active_shows": 3,      # ROUND-ROBIN WIDTH (was series.MAX_ACTIVE): how many shows share the
                                # rotation, one episode taken from each in turn. 1 = finish a show before
                                # starting the next. Governs only ADDING a show — see series.max_active().
    "finisher_lanes": 2,        # PARALLEL REMUXES (was orchestrator.FINISHER_LANES): how many topaz-done
                                # items may remux at once. 1 = a quieter machine and the whole GPU for
                                # Topaz; 2 = the current throughput (Resolve gets 2 items ahead).
    "min_free_gb": 400,         # DISK FLOOR (was orchestrator.MIN_FREE_GB): free space that must remain
                                # before an item may START, and the base of the prefetch gate. A Topaz
                                # ProRes working set is ~190 GiB per episode (re-measured 2026-08-05), ~245 GB for a feature.
    "prefetch_cap_gb": 100,     # DOWNLOAD-AHEAD BUFFER (was orchestrator.PREFETCH_HARD_CAP_GB): hard
                                # ceiling on the total size of pre-staged sources. 0 = off (fetch each
                                # item only when it's needed).
    "max_episode_fails": 5,     # GIVE-UP THRESHOLD (was orchestrator.MAX_EPISODE_FAILS): consecutive
                                # genuine failures of ONE episode before it's parked and the run moves on.
    "seg_eta_after_minutes": 15,  # SEGMENT-ETA GATE (was a hardcoded 900 s in the app): Topaz shows
                                # the CURRENT segment's eta beside the segment counter only while
                                # THAT segment still has longer than this left to run — judged on
                                # the segment itself, not on a projected average across segments.
                                # The stage eta alone reads as "hours away", so this is the
                                # near-term number; a segment about to finish doesn't need one.
                                # Purely a readout threshold — changes nothing the pipeline does.
    "unplug_grace_seconds": 60, # POWER-BLIP GRACE (was orchestrator.UNPLUG_GRACE_SECONDS): unplugged
                                # mid-stage, wait this long for the power to come back before abandoning
                                # the stage. 0 = abandon immediately.

    # --- WHICH SCREEN RESOLVE RUNS ON (all default to today's behaviour) -------------
    "resolve_host_pinning": False,   # MASTER SWITCH. Off = Resolve runs wherever it opens (the
                                # main display), exactly as it always has. On = it is moved to the
                                # highest-priority attached display that is eligible AND has a
                                # recorded template-smoke pass.
    "resolve_host_displays": [],  # ordered display keys, highest priority first. The ONLY
                                # non-scalar setting — see VALIDATORS.
    "resolve_host_fallback_main": False,  # host unavailable -> fall back to the main display?
                                # Off (default) is the FAIL-SAFE: the item defers instead, so a
                                # yanked cable stalls the run rather than seizing the screen the
                                # user was told would be left alone.
    "resolve_takeover_warn": True,   # show a notice on the main display just before the pipeline
                                # takes the screen and mouse. NOT a countdown and NOT a delay: it
                                # fires from run_dv_ui, where ~10 s of real setup work (Color page,
                                # activate, placement, full screen, screenshot) still has to run
                                # before the first click. A timer was tried and removed — it had to
                                # start at the top of the stage, but the takeover lands whenever
                                # setup() finishes, so it hit zero and sat at "now..." for minutes.
}

# Clamp table — the ONE source of truth for every numeric setting's range, used on write
# (set_settings) AND on read (tunable). Keeping them together is what stops a hand-edited
# settings.json from feeding the engine a value the UI could never produce.
# key -> (lo, hi); a key in ZERO_IS_OFF may additionally be exactly 0.
LIMITS = {
    "poll_minutes": (1, 1440),
    "dim_after_minutes": (0, 240),          # 0 = Off (never dim)
    "max_peak_mbps": (20, 62),  # 62 * 1.15 gate tolerance + 8 TrueHD headroom = 79.3, the most
                                # the SHIELD's ~80 Mbps DV ceiling allows (dvcap constants) —
                                # EVERY output is budgeted as if it carries TrueHD (user-dictated)
    "audio_target_lufs": (-24, -10),
    "min_adapter_watts": (1, 500),
    "passthrough_min_mbps": (5, 200),
    "resolve_share_remuxes": (0, 2),
    "max_youtube_minutes": (1, 600),
    "youtube_every_tv_episodes": (1, 50),
    "max_active_shows": (1, MAX_ACTIVE_CEILING),
    "finisher_lanes": (1, 2),
    "min_free_gb": (200, 2000),
    "prefetch_cap_gb": (25, 500),
    "max_episode_fails": (1, 20),
    "unplug_grace_seconds": (0, 600),
    "seg_eta_after_minutes": (1, 120),
                                         # in under a minute); high = effectively never
}
ZERO_IS_OFF = {"audio_target_lufs", "passthrough_min_mbps", "prefetch_cap_gb"}

# --- the Topaz preset catalog: ALL SDR ProRes; content-type × resolution ----
# Each parent preset (content type) carries a tuned param set for EACH source resolution.
# The goal is a 4K upscale for every source — the user picks only the content type; the
# pipeline auto-detects the source resolution and applies that resolution's variant + the
# right scale-to-4K (computed in plan.py — a variant is PARAMS only). Lower-res sources are
# blockier/softer, so they get heavier compression cleanup, a touch more detail recovery,
# and LOWER blend (= trust the AI more, since the original is worse); 1080p keeps the
# lightest touch (the original values). Proteus (prob-4); blend = recover-original-detail.
#
# RULE (future-proofing): every parent preset MUST define a variant for EVERY RES_BUCKET.
# `test_settings.test_every_preset_has_all_resolution_variants` enforces it — so when a new
# parent preset is added here, give it a 480p / 720p / 1080p variant too.
RES_BUCKETS = ("480p", "720p", "1080p")

TOPAZ_PRESETS = {
    "digital": {
        "label": "Live-Action · Digital",
        "desc": "Digitally-shot live action (most modern TV).",
        "by_res": {
            "1080p": {"model": "prob-4", "compression": 0.08, "details": 0.02, "halo": 0.05, "blend": 0.45},
            "720p":  {"model": "prob-4", "compression": 0.16, "details": 0.04, "halo": 0.06, "blend": 0.35},
            "480p":  {"model": "prob-4", "compression": 0.28, "details": 0.06, "halo": 0.08, "blend": 0.25},
        },
    },
    "film": {
        "label": "Live-Action · Film",
        "desc": "Film-originated live action — preserves grain, less denoise.",
        "by_res": {
            "1080p": {"model": "prob-4", "compression": 0.04, "details": 0.05, "halo": 0.05, "blend": 0.60},
            "720p":  {"model": "prob-4", "compression": 0.10, "details": 0.06, "halo": 0.06, "blend": 0.50},
            "480p":  {"model": "prob-4", "compression": 0.20, "details": 0.08, "halo": 0.07, "blend": 0.40},
        },
    },
    "animation2d": {
        "label": "2D Animation",
        "desc": "Flat-colour 2D cartoons (Rick and Morty, Phineas and Ferb). Cleans "
                "compression banding, keeps lines crisp, no grain/detail synthesis.",
        "by_res": {
            "1080p": {"model": "prob-4", "compression": 0.30, "details": 0.00, "halo": 0.10, "blend": 0.10},
            "720p":  {"model": "prob-4", "compression": 0.45, "details": 0.00, "halo": 0.12, "blend": 0.08},
            "480p":  {"model": "prob-4", "compression": 0.60, "details": 0.00, "halo": 0.15, "blend": 0.05},
        },
    },
    "animation3d": {
        "label": "3D Animation (CGI)",
        "desc": "Computer-rendered 3D animation (Pixar, DreamWorks, Illumination). Clean source — "
                "moderate compression cleanup + light detail; NOT the flat-2D treatment, NOT film grain.",
        "by_res": {   # between 'digital' and 'animation2d' — starting values, tune here
            "1080p": {"model": "prob-4", "compression": 0.15, "details": 0.03, "halo": 0.06, "blend": 0.35},
            "720p":  {"model": "prob-4", "compression": 0.25, "details": 0.05, "halo": 0.07, "blend": 0.28},
            "480p":  {"model": "prob-4", "compression": 0.38, "details": 0.07, "halo": 0.09, "blend": 0.20},
        },
    },
    "youtube": {
        "label": "YouTube",
        "desc": "Streaming-compressed YouTube video (heavier codec artifacts than broadcast digital). "
                "Stronger compression cleanup + light detail recovery; the default for YouTube channels.",
        "by_res": {   # between 'digital' and 'animation3d' — YouTube compresses harder; tune here
            "1080p": {"model": "prob-4", "compression": 0.20, "details": 0.03, "halo": 0.06, "blend": 0.35},
            "720p":  {"model": "prob-4", "compression": 0.32, "details": 0.05, "halo": 0.07, "blend": 0.28},
            "480p":  {"model": "prob-4", "compression": 0.45, "details": 0.07, "halo": 0.09, "blend": 0.20},
        },
    },
}
DEFAULT_PRESET = "digital"
DEFAULT_RES = "1080p"   # fallback variant; also what a 4K-clean pass uses (lightest cleanup)


def _load(path: str, default):
    try:
        with open(path) as f:
            data = json.load(f)
        return {**default, **data} if isinstance(default, dict) else data
    except (OSError, json.JSONDecodeError):
        return dict(default) if isinstance(default, dict) else default


def _save(path: str, data) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)            # atomic


# ---- global settings ------------------------------------------------------

def get_settings() -> dict:
    return _load(SETTINGS_FILE, DEFAULT_SETTINGS)


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _is_zero(v) -> bool:
    """True only for a real numeric zero — a bad type ('', None, 'off') is NOT 'off', it's
    garbage, and must fall through to the clamp's default instead of disabling a feature."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == 0


def clamp_setting(key: str, v):
    """Coerce one setting to its legal value: the LIMITS range, plus an exact 0 for the
    ZERO_IS_OFF keys. Anything unparseable becomes the default."""
    lo, hi = LIMITS[key]
    if key in ZERO_IS_OFF and _is_zero(v):
        return 0
    return _clamp(v, lo, hi, DEFAULT_SETTINGS[key])


def tunable(key: str) -> int:
    """READ side of a numeric setting — the live value, clamped, with the shipped default as
    the fallback. Engine code calls this at USE time (never at import) so a change in the UI
    takes effect on the next decision rather than the next relaunch."""
    return clamp_setting(key, get_settings().get(key, DEFAULT_SETTINGS[key]))


def _valid_display_list(v):
    """A hand-edited settings.json must not be able to feed the engine garbage. LIMITS
    covers numbers; this covers the one list-valued setting."""
    if not isinstance(v, list):
        return []
    out = []
    for item in v:
        if isinstance(item, str) and 0 < len(item) <= 128 and item not in out:
            out.append(item)
    return out[:8]


# Non-numeric settings need their own validation — LIMITS is a numeric clamp table and
# silently passes anything it has no entry for.
def _valid_quiet_until(v):
    """The 4-hour Screen Control cap, enforced at the PERSISTENCE layer. The app's own
    path (/api/quiet-mode) already clamps via clamp_quiet_seconds, but a raw client
    writing quiet_until through /api/settings — or a hand-edited settings.json — could
    park the pipeline for days, which is exactly the stall MAX_QUIET_SECONDS exists to
    prevent. 0/junk = not paused; a future epoch is capped at now + MAX_QUIET_SECONDS."""
    import time as _t
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return 0 if n <= 0 else min(n, int(_t.time()) + MAX_QUIET_SECONDS)


VALIDATORS = {"resolve_host_displays": _valid_display_list,
              "quiet_until": _valid_quiet_until}


def set_settings(updates: dict) -> dict:
    with _WRITE_LOCK:                    # atomic read-modify-write (see _WRITE_LOCK)
        s = get_settings()
        for k, v in (updates or {}).items():
            if k in DEFAULT_SETTINGS:
                s[k] = v
        for k in LIMITS:                 # one table, applied on write and on read (tunable)
            s[k] = clamp_setting(k, s.get(k))
        for k, fn in VALIDATORS.items():
            s[k] = fn(s.get(k, DEFAULT_SETTINGS[k]))
        _save(SETTINGS_FILE, s)
        return s


def get_display_priority() -> list:
    return _valid_display_list(get_settings().get("resolve_host_displays", []))


# ---- Topaz preset catalog -------------------------------------------------

def preset_catalog() -> list:
    """[{key, label, desc}] for the UI dropdown."""
    return [{"key": k, "label": v["label"], "desc": v["desc"]} for k, v in TOPAZ_PRESETS.items()]


def preset_params(key: str, res: str = DEFAULT_RES) -> dict:
    """The Topaz tvai_up params for a preset key + resolution bucket ('480p'/'720p'/'1080p').
    Falls back to the default preset and the 1080p variant (also what a 4K-clean pass uses),
    so an unknown key/res — or a future preset shaped oddly — is always valid."""
    preset = TOPAZ_PRESETS.get(key) or TOPAZ_PRESETS[DEFAULT_PRESET]
    by = preset.get("by_res") or TOPAZ_PRESETS[DEFAULT_PRESET]["by_res"]
    return dict(by.get(res) or by.get(DEFAULT_RES) or next(iter(by.values())))


# ---- per-show preset selection (the user picks a key, never tunes params) --

def all_profiles() -> dict:
    return _load(PROFILES_FILE, {})


def _show_entry(show: str) -> dict:
    """Per-show settings as a dict, normalizing the LEGACY string form (= preset key only)
    so old show_profiles.json entries keep working."""
    v = all_profiles().get(show)
    if isinstance(v, dict):
        return dict(v)
    if isinstance(v, str):
        return {"preset": v}
    return {}


def _update_show(show: str, **kw) -> dict:
    p = all_profiles()
    e = _show_entry(show)          # migrates a legacy string to a dict on write
    e.update(kw)
    p[show] = e
    _save(PROFILES_FILE, p)
    return e


def get_show_preset(show: str):
    """The show's chosen preset key, or None if it was never configured."""
    return _show_entry(show).get("preset")


def set_show_preset(show: str, key: str) -> str:
    key = key if key in TOPAZ_PRESETS else DEFAULT_PRESET
    _update_show(show, preset=key)
    return key


def show_preset_key(show: str) -> str:
    return get_show_preset(show) or DEFAULT_PRESET



def get_show_unwatched_first(show: str) -> bool:
    """Per-show: process UNWATCHED episodes first? Default True (the prior always-on
    behavior); False = just start at the beginning of the show (numeric order)."""
    return bool(_show_entry(show).get("unwatched_first", True))


def set_show_unwatched_first(show: str, value) -> bool:
    _update_show(show, unwatched_first=bool(value))
    return bool(value)


def get_show_normalize_audio(key: str) -> bool:
    """Per-item (TV show / movie title / channel folder — the same string the item's
    preset is keyed by): apply the SMART LOUDNESS BOOST (global audio_target_lufs) to
    this item's remuxes? Default True (the prior always-on behavior); False = keep this
    item's audio bit-exact (the remux's existing boost-off copy path)."""
    return bool(_show_entry(key).get("normalize_audio", True))


def set_show_normalize_audio(key: str, value) -> bool:
    _update_show(key, normalize_audio=bool(value))
    return bool(value)


def get_show_featurettes_last(key: str) -> bool:
    """Per-show: process season-00 specials/featurettes AFTER the whole show (True,
    default) or leave them in numeric order, where "S00" sorts ahead of "S01" and they
    would be upscaled BEFORE the show itself."""
    return bool(_show_entry(key).get("featurettes_last", True))


def set_show_featurettes_last(key: str, value) -> bool:
    _update_show(key, featurettes_last=bool(value))
    return bool(value)


# What the Resolve stage should OUTPUT for an item. "auto" is the default and ALWAYS
# resolves to 1000-nit Dolby Vision, whatever the intake range (user-dictated 2026-08-09:
# the 2000-nit target is MANUAL-ONLY). The other three pin it regardless of the source. "sdr" is the only one that produces a non-DV master,
# and the only one whose Resolve stage needs no screen automation at all (no DV analyze).
OUTPUT_MODES = ("auto", "sdr", "dv1000", "dv2000")


def get_show_output_mode(key: str) -> str:
    """Per-item (TV show / movie title / channel folder — the same key the preset uses)."""
    v = _show_entry(key).get("output_mode")
    return v if v in OUTPUT_MODES else "auto"


def set_show_output_mode(key: str, value) -> str:
    v = value if value in OUTPUT_MODES else "auto"
    _update_show(key, output_mode=v)
    return v


# Filename evidence that a SOURCE is already HDR. Token-bounded so "DVDRip" is not read as
# Dolby Vision and "SDR" is not read as HDR. This is only ever used to DISPLAY what "auto"
# will do — the real decision still comes from ffprobe's color transfer at stage time.
_HDR_TOKENS = re.compile(
    r"(?<![A-Za-z0-9])(HDR10\+?|HDR|HLG|PQ|DV|DoVi|Dolby[ ._-]?Vision)(?![A-Za-z0-9])",
    re.IGNORECASE)


# An explicit SDR token overrides the inference below. master_stem already treats "SDR" as
# meaningful, so a release that bothers to say it is trusted.
_SDR_TOKEN = re.compile(r"(?<![A-Za-z0-9])SDR(?![A-Za-z0-9])", re.IGNORECASE)
# 4K disc-sourced releases are HDR10 in practice — a UHD Blu-ray is HDR10 by specification,
# and a REMUX is a straight copy of one. The name that prompted this carries no HDR token at
# all ("...2022.2160p.BluRay.REMUX.HEVC.DTS-HD.MA.TrueHD.7.1.Atmos-FGT"): DTS-HD and TrueHD
# contain "HD", not "HDR", so the token search found nothing and the row suggested 1000 nits
# for a film that is certainly HDR10.
_UHD_DISC = re.compile(
    r"(?<![A-Za-z0-9])(2160p|4k|uhd)(?![A-Za-z0-9])", re.IGNORECASE)
_DISC_SOURCE = re.compile(
    r"(?<![A-Za-z0-9])(remux|bluray|blu-ray|bdrip|bdremux|uhdbd)(?![A-Za-z0-9])",
    re.IGNORECASE)


def source_is_hdr(name) -> bool:
    """Is this source HDR? Prefers what ffprobe ACTUALLY found, falling back to the filename.

    Once an item has been through plan_for, its real color transfer is on record, so the row
    stops guessing — which matters in both directions: an HDR file whose name says nothing
    (the case that started this), and an SDR file NAMED like HDR, where the guess would
    otherwise disagree with the engine forever and could tempt a wrong pin."""
    try:
        import plan
        known = plan.probed_is_hdr(name)
    except Exception:
        known = None
    return looks_hdr(name) if known is None else known


def looks_hdr(name) -> bool:
    """Best-effort HDR guess from a FILENAME. A guess is all this is — see
    effective_output_mode; the authoritative answer is ffprobe's color transfer at stage time
    (plan.probe_input), which only exists once the file is local."""
    n = str(name or "")
    if not n:
        return False
    if _HDR_TOKENS.search(n):
        return True
    if _SDR_TOKEN.search(n):
        return False
    return bool(_UHD_DISC.search(n) and _DISC_SOURCE.search(n))


def effective_output_mode(key: str, hdr_source: bool = False) -> str:
    """What the item will ACTUALLY be mastered as — a pin if there is one, otherwise the
    automatic rule. Automatic is always Dolby Vision at 1000 nits, whatever the intake
    range (user-dictated 2026-08-09: the 2000-nit target is MANUAL-ONLY — it stays a
    per-item override but nothing selects it automatically). This is what the app
    displays, so the row never shows an abstract "auto" the user has to translate.
    `hdr_source` is retained for signature compatibility; it no longer changes the
    automatic answer."""
    m = get_show_output_mode(key)
    if m in ("sdr", "dv1000", "dv2000"):
        return m
    return "dv1000"


def is_sdr_output(key: str) -> bool:
    """True only when the item is PINNED to a non-DV SDR master. 'auto' is never SDR — the
    automatic rule has always produced Dolby Vision."""
    return get_show_output_mode(key) == "sdr"


def get_show_replace_source(key: str) -> bool:
    """Per-item (TV show / movie title): after the 4K master is VERIFIED on the NAS,
    delete the superseded source (True, default — the output replaces its input) or
    keep it beside the master (False: Plex merges the two files into one item with two
    versions and serves the 4K; the source stays as the re-run option for future,
    better upscale models). Consulted by stages._upload; YouTube's folder-split is
    unaffected (its staging copy is not a Plex version)."""
    return bool(_show_entry(key).get("replace_source", True))


def set_show_replace_source(key: str, value) -> bool:
    _update_show(key, replace_source=bool(value))
    return bool(value)


def show_topaz_params(show: str, res: str = DEFAULT_RES) -> dict:
    """What the Topaz stage actually uses for a show: its preset's params for the source's
    resolution bucket (the bucket comes from plan.resolution_bucket(source height))."""
    return preset_params(show_preset_key(show), res)
