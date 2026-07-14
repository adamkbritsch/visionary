"""Per-input process plan.

A source can arrive at any major resolution (480p / 720p / 1080p) or already 4K, SDR or
HDR (but not yet Dolby Vision) — the goal is a 4K upscale for ALL of them, automatically,
without the user thinking about the source resolution. Invariants the user set:
  * ProRes is the intermediate in EVERY scenario (it makes up for source data loss).
  * Topaz PRESERVES dynamic range — SDR→SDR, HDR→HDR, NEVER converts either way
    (no Hyperion/tone-map). It UPSCALES every sub-4K source to 4K; already-4K is CLEANED (1×).
  * DaVinci Resolve adds HDR ONLY when the source is SDR; it always adds Dolby Vision.

           input                topaz (keeps range)        resolve
  ----------------------------  ------------------------   ----------------------------
  480p  SDR/HDR                 upscale 4× → fit 2160      add (HDR+)DV
  720p  SDR/HDR                 upscale 4× → fit 2160      add (HDR+)DV
  1080p SDR/HDR                 upscale 2× (lands 2160)    add (HDR+)DV
  4K (not DV)                   clean 1×                   add (HDR+)DV
  already Dolby Vision          skip                       skip

tvai_up's scale is {1,2,4} only (3 fails) and h=/scale=0 cap at the model's 4× max, so each
bucket uses an explicit AI scale and — when that doesn't land exactly on 2160 — a final
lanczos fit (down for 720p's 2880, up for 480p's 1920); 1080p×2 hits 2160 exactly, no fit.
"""
from __future__ import annotations
import json
import subprocess

FFPROBE = "/opt/homebrew/bin/ffprobe"
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}   # PQ / HLG

TARGET_H = 2160                                  # 4K output height — the goal for every source
RES_BUCKETS = ("480p", "720p", "1080p")
# AI upscale factor per SOURCE bucket. 720p uses 4× (→2880) then a lanczos fit DOWN to 2160
# (denser REAL detail than 2×→up); 480p uses the 4× max (→1920) then a small fit UP; 1080p
# ×2 lands on 2160 exactly. (3× is impossible — tvai rejects scale=3.)
_AI_SCALE = {"480p": 4, "720p": 4, "1080p": 2}


def resolution_bucket(height) -> str:
    """Map a source HEIGHT to a preset/scale bucket. Sub-4K only — 4K is handled separately."""
    h = int(height or 0)
    if 0 < h < 600:
        return "480p"
    if 0 < h < 900:
        return "720p"
    return "1080p"   # 1080p (and 1440p, and unknown) → 2× toward 2160


def probe_input(path: str) -> dict:
    info = {"width": 0, "height": 0, "is_4k": False, "is_hdr": False, "is_dv": False,
            "codec": None, "transfer": None, "pix_fmt": None, "is_cfr": False, "video_kbps": 0}
    try:
        out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                              "-show_streams", "-show_format", "-of", "json", path],
                             capture_output=True, text=True, timeout=60).stdout
        d = json.loads(out)
        v = (d.get("streams") or [{}])[0]
        fmt = d.get("format") or {}
    except Exception:
        return info
    info["width"] = int(v.get("width") or 0)
    info["height"] = int(v.get("height") or 0)
    info["is_4k"] = info["width"] >= 3840 or info["height"] >= 2160
    info["is_hdr"] = v.get("color_transfer") in HDR_TRANSFERS
    info["is_dv"] = any(sd.get("side_data_type") == "DOVI configuration record"
                        for sd in (v.get("side_data_list") or []))
    info["codec"] = v.get("codec_name")
    info["transfer"] = v.get("color_transfer")
    info["pix_fmt"] = v.get("pix_fmt")
    # CFR: avg == r frame rate, both valid (mirror of topaz._is_already_cfr — no import cycle)
    avg, r = v.get("avg_frame_rate") or "", v.get("r_frame_rate") or ""
    info["is_cfr"] = bool(avg and r and avg == r and not avg.startswith("0"))
    # VIDEO bitrate in Kb/s, best source first: stream bit_rate (MP4) → MKV track-statistics
    # tag → container total (overestimates: includes audio — fine for a threshold gate).
    # plan_for runs on the fully-downloaded local file, so MKV end-of-file tags read fine.
    tags = v.get("tags") or {}
    for cand in (v.get("bit_rate"), tags.get("BPS"), tags.get("BPS-eng"), fmt.get("bit_rate")):
        try:
            if cand and int(cand) > 0:
                info["video_kbps"] = int(cand) // 1000
                break
        except (TypeError, ValueError):
            continue
    return info


def choose_plan(info: dict, *, passthrough_min_kbps: int = 0) -> dict:
    """Map input characteristics to the path (PURE — unit-tested). Topaz preserves range;
    Resolve adds HDR only for SDR sources. Every sub-4K source upscales to 4K. Returns
    {topaz, scale, res, fit_height, resolve, is_hdr, reason}: `scale` = the tvai AI factor,
    `res` = the preset/resolution bucket (which variant's params to use), `fit_height` = the
    final lanczos-fit height (2160) or None when the AI scale already lands on 2160.

    HIGH-BITRATE 4K FAST PATH (`passthrough_min_kbps` > 0, user-dictated): ANY 4K CFR source
    at/above the threshold skips Topaz — its picture is already the deliverable, whatever the
    codec or provenance (no categorical exclusions). The tier split is technical, not
    eligibility: an HDR10/PQ HEVC Main10 exact-3840×2160 source → topaz "rpu-only" (keep the
    ORIGINAL stream, Resolve runs only for the DV analysis and its RPU is injected — no
    re-encode); everything else → topaz "resolve-only" (Resolve's HDR+DV conversion ships
    through the normal capped remux — an RPU alone can't sit on a non-PQ/non-HEVC base).
    `"skip"` stays reserved for already-DV (an abort, not a fast path)."""
    is_hdr = bool(info.get("is_hdr"))
    resolve = "add_dv" if is_hdr else "add_hdr_dv"
    rng = "HDR" if is_hdr else "SDR"
    if info.get("is_dv"):
        return {"topaz": "skip", "scale": 1, "res": None, "fit_height": None,
                "resolve": "skip", "is_hdr": is_hdr, "reason": "already Dolby Vision — nothing to do"}
    kbps = int(info.get("video_kbps") or 0)
    # ELIGIBILITY is purely measured — 4K + CFR + bitrate. NOTHING is categorically excluded
    # (user-dictated: no carve-outs by provenance — a 4K YouTube VP9 at threshold bitrate
    # qualifies the same as a web-DL). The stricter stream properties below only decide WHICH
    # tier, never whether the fast path applies.
    if (passthrough_min_kbps and info.get("is_4k") and info.get("is_cfr")
            and kbps >= passthrough_min_kbps):
        if (info.get("transfer") == "smpte2084"               # PQ — DV 8.1 needs an HDR10 base
                and info.get("codec") == "hevc"               # ...an HEVC one
                and info.get("pix_fmt") == "yuv420p10le"      # ...Main10
                and info.get("width") == 3840 and info.get("height") == 2160):
            return {"topaz": "rpu-only", "scale": 1, "res": None, "fit_height": None,
                    "resolve": "add_dv", "is_hdr": True,
                    "reason": "4K HDR10 HEVC @ ~%d Mbps ≥ threshold — original stream kept, "
                              "Resolve adds the DV layer only" % (kbps // 1000)}
        return {"topaz": "resolve-only", "scale": 1, "res": None, "fit_height": None,
                "resolve": resolve, "is_hdr": is_hdr,
                "reason": "4K %s (%s) @ ~%d Mbps ≥ threshold — no upscale needed, Resolve %s"
                          % (rng, info.get("codec") or "?", kbps // 1000,
                             "adds DV" if is_hdr else "adds HDR + DV")}
    if info.get("is_4k"):
        return {"topaz": "clean", "scale": 1, "res": "1080p", "fit_height": None,
                "resolve": resolve, "is_hdr": is_hdr,
                "reason": "4K %s → Topaz clean 1× (keeps %s) → Resolve %s"
                          % (rng, rng, "adds DV" if is_hdr else "adds HDR + DV")}
    height = int(info.get("height") or 0)
    res = resolution_bucket(height)
    scale = _AI_SCALE[res]
    fit_height = TARGET_H if (height * scale != TARGET_H) else None   # land exactly on 4K
    reason = ("%s %s → Topaz upscale %d×%s (keeps %s) → Resolve %s"
              % (res, rng, scale, " → fit 2160" if fit_height else " = 2160", rng,
                 "adds DV" if is_hdr else "adds HDR + DV"))
    return {"topaz": "upscale", "scale": scale, "res": res, "fit_height": fit_height,
            "resolve": resolve, "is_hdr": is_hdr, "reason": reason}


def passthrough_min_kbps() -> int:
    """The live fast-path threshold (Kb/s) from settings; 0 = feature off or unreadable."""
    try:
        import settings
        return max(0, int(settings.get_settings().get("passthrough_min_mbps", 0))) * 1000
    except Exception:
        return 0


def plan_for(path: str) -> dict:
    info = probe_input(path)
    p = choose_plan(info, passthrough_min_kbps=passthrough_min_kbps())
    p["input"] = info
    return p
