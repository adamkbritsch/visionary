"""User-adjustable settings + per-show Topaz preset selection.

Two JSON files under ~/.topaz-pipeline/ (atomic writes):
  settings.json       — global knobs (stop time, power policy, …)
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
import os
import threading

# The dashboard is a ThreadingHTTPServer: two handlers can hit set_settings at once (e.g.
# a Settings toggle save racing Activate/Deactivate). The read-modify-write must be atomic
# or a stale copy resurrects the old `activated` value.
_WRITE_LOCK = threading.Lock()

CONFIG_DIR = os.path.expanduser("~/.topaz-pipeline")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
PROFILES_FILE = os.path.join(CONFIG_DIR, "show_profiles.json")

# Global, user-adjustable. pause_on_battery_drain = pause if the battery drains >5% on AC.
DEFAULT_SETTINGS = {
    "activated": False,         # APPLIANCE mode: persisted arm state. While True the app runs
                                # whenever it can — the server re-enables the orchestrator on
                                # launch (see server._rearm_loop). A run ends only on a manual stop.
    "quiet_mode": False,        # QUIET MODE: keep download+topaz running but DEFER each item before the
                                # screen-invasive Resolve stage, so the laptop stays usable. Items pile up
                                # (no drain to remux/upload/cleanup) → the run pauses on low disk until off.
    "pause_on_battery_drain": True,
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
    "max_youtube_minutes": 20,  # YouTube: the per-channel length-cap threshold (applied only to channels
                                # whose 'capped' toggle is on).
    "youtube_every_tv_episodes": 2,  # YouTube CADENCE: serve exactly 1 YouTube video after every N TV
                                # episodes (was: a ~max_youtube_minutes batch every turn). Throttles the
                                # slow 4K-SDR YouTube upscales so they don't crowd out TV. If TV runs out,
                                # YouTube drains freely regardless of N.
}

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
TOPAZ_PARAMS = ("model", "compression", "details", "halo", "blend")


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


def set_settings(updates: dict) -> dict:
    with _WRITE_LOCK:                    # atomic read-modify-write (see _WRITE_LOCK)
        s = get_settings()
        for k, v in (updates or {}).items():
            if k in DEFAULT_SETTINGS:
                s[k] = v
        s["poll_minutes"] = _clamp(s.get("poll_minutes"), 1, 1440, DEFAULT_SETTINGS["poll_minutes"])
        s["dim_after_minutes"] = _clamp(s.get("dim_after_minutes"), 0, 240,   # 0 = Off (never dim)
                                        DEFAULT_SETTINGS["dim_after_minutes"])
        s["max_peak_mbps"] = _clamp(s.get("max_peak_mbps"), 20, 100, DEFAULT_SETTINGS["max_peak_mbps"])
        if s.get("audio_target_lufs") != 0:   # 0 = boost off; else clamp to a sane loudness window
            s["audio_target_lufs"] = _clamp(s.get("audio_target_lufs"), -24, -10,
                                            DEFAULT_SETTINGS["audio_target_lufs"])
        s["min_adapter_watts"] = _clamp(s.get("min_adapter_watts"), 1, 500,
                                        DEFAULT_SETTINGS["min_adapter_watts"])
        s["max_youtube_minutes"] = _clamp(s.get("max_youtube_minutes"), 1, 600,
                                          DEFAULT_SETTINGS["max_youtube_minutes"])
        s["youtube_every_tv_episodes"] = _clamp(s.get("youtube_every_tv_episodes"), 1, 50,
                                                DEFAULT_SETTINGS["youtube_every_tv_episodes"])
        _save(SETTINGS_FILE, s)
        return s


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


YT_SCOPES = ("popular", "all")


def get_yt_scope(channel: str) -> str:
    """Per-channel YouTube UPSCALE scope: 'popular' (only the channel's top-viewed videos) or 'all'
    (every downloaded video within the length cap). Default 'popular'."""
    v = _show_entry(channel).get("yt_scope")
    return v if v in YT_SCOPES else "popular"


def set_yt_scope(channel: str, scope: str) -> str:
    scope = scope if scope in YT_SCOPES else "popular"
    _update_show(channel, yt_scope=scope)
    return scope


def get_show_unwatched_first(show: str) -> bool:
    """Per-show: process UNWATCHED episodes first? Default True (the prior always-on
    behavior); False = just start at the beginning of the show (numeric order)."""
    return bool(_show_entry(show).get("unwatched_first", True))


def set_show_unwatched_first(show: str, value) -> bool:
    _update_show(show, unwatched_first=bool(value))
    return bool(value)


def show_topaz_params(show: str, res: str = DEFAULT_RES) -> dict:
    """What the Topaz stage actually uses for a show: its preset's params for the source's
    resolution bucket (the bucket comes from plan.resolution_bucket(source height))."""
    return preset_params(show_preset_key(show), res)
