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
import os
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
    info = {"width": 0, "height": 0, "sar": "", "is_4k": False, "is_hdr": False,
            "is_dv": False, "codec": None, "transfer": None, "pix_fmt": None,
            "is_cfr": False, "video_kbps": 0}
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
    info["sar"] = v.get("sample_aspect_ratio") or ""   # "8:9" on anamorphic DV; ""/"0:1"/"1:1" square
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


def _no_rpu_reason(info) -> str | None:
    """Why this stream cannot take a Dolby Vision RPU directly, or None if it can.

    These three are format requirements of DV profile 8.1, not caution: the RPU is per-frame
    metadata riding alongside an HEVC PQ Main10 base layer. HLG, AV1, H.264 and 8-bit simply
    cannot carry one, so such a source has to be CONVERTED to gain Dolby Vision at all. That
    is the one case where HDR material is legitimately re-encoded, and the plan says which
    property failed so it is never silent."""
    if info.get("transfer") != "smpte2084":
        return "its transfer is %s, not PQ" % (info.get("transfer") or "unknown")
    if info.get("codec") != "hevc":
        return "it is %s, not HEVC" % (info.get("codec") or "unknown")
    if info.get("pix_fmt") != "yuv420p10le":
        return "it is %s, not 10-bit 4:2:0" % (info.get("pix_fmt") or "unknown")
    return None


def choose_plan(info: dict) -> dict:
    """Map input characteristics to the path (PURE — unit-tested). Topaz preserves range;
    Resolve adds HDR only for SDR sources. Every sub-4K source upscales to 4K. Returns
    {topaz, scale, res, fit_height, resolve, is_hdr, reason, source_cfr}: `scale` = the tvai AI
    factor, `res` = the preset/resolution bucket (which variant's params to use), `fit_height` =
    the final lanczos-fit height (2160) or None when the AI scale already lands on 2160.

    NO 4K SOURCE GOES THROUGH TOPAZ (user-dictated 2026-09-30: "get rid of the 12 mbps minimum
    for things to be able to go through topaz first, so 4k things all don't go through topaz",
    and of a variable-frame-rate source, "I don't want it to do the cleanup, that counts as
    upscaling even though it's the same size"). There used to be a bitrate threshold
    (passthrough_min_mbps, 12 Mbps) and a Topaz "clean" 1x pass for 4K under it or at a variable
    frame rate. Neither exists now. The tier is technical:
      rpu-only      an HDR10/PQ HEVC Main10 4K source at a CONSTANT frame rate: the ORIGINAL
                    stream is kept and Resolve's Dolby Vision RPU is injected — no re-encode, at
                    any bitrate or 4K geometry (user-dictated: 4K HDR10 is never re-encoded)
      resolve-only  every other 4K source: Resolve's HDR+DV conversion ships through the normal
                    capped remux. A VARIABLE frame rate source is only RE-TIMED first — the CFR
                    pass re-encodes it to a constant rate (source_cfr=False) instead of
                    stream-copying, since Resolve's timeline and the remux run at one rate. The
                    re-time is ffmpeg's, never Topaz's.
    `"skip"` stays reserved for already-DV (an abort, not a fast path)."""
    is_hdr = bool(info.get("is_hdr"))
    resolve = "add_dv" if is_hdr else "add_hdr_dv"
    rng = "HDR" if is_hdr else "SDR"
    if info.get("is_dv"):
        return {"topaz": "skip", "scale": 1, "res": None, "fit_height": None,
                "resolve": "skip", "is_hdr": is_hdr, "reason": "already Dolby Vision — nothing to do"}
    kbps = int(info.get("video_kbps") or 0)
    cfr = bool(info.get("is_cfr"))
    # Only the three properties Dolby Vision 8.1 physically requires of a base layer are tested,
    # plus a constant frame rate for the RPU to land on frame by frame. Geometry has no bearing
    # (the old exact-3840x2160 gate re-encoded every 2.39:1 blockbuster and DCI 4K).
    if (info.get("is_4k") and cfr
            and info.get("transfer") == "smpte2084"           # PQ — DV 8.1 needs an HDR10 base
            and info.get("codec") == "hevc"                   # ...an HEVC one
            and info.get("pix_fmt") == "yuv420p10le"):        # ...Main10
        return {"topaz": "rpu-only", "scale": 1, "res": None, "fit_height": None,
                "resolve": "add_dv", "is_hdr": True, "source_cfr": True,
                "reason": "4K HDR10 HEVC Main10 (%dx%d @ ~%d Mbps) — original stream kept "
                          "untouched, Resolve adds the DV layer only"
                          % (info.get("width") or 0, info.get("height") or 0, kbps // 1000)}
    if info.get("is_4k"):
        # 4K, but it cannot keep its own stream, so it is converted. Name the reason — a
        # re-encode of HDR material should never be silent.
        why = _no_rpu_reason(info) if is_hdr else None
        if is_hdr and not why and not cfr:
            why = "its frame rate varies"
        return {"topaz": "resolve-only", "scale": 1, "res": None, "fit_height": None,
                "resolve": resolve, "is_hdr": is_hdr, "source_cfr": cfr,
                "reason": "4K %s (%s) @ ~%d Mbps — no upscale, %sResolve %s%s"
                          % (rng, info.get("codec") or "?", kbps // 1000,
                             "" if cfr else "re-timed to a constant frame rate, ",
                             "adds DV" if is_hdr else "adds HDR + DV",
                             ("; re-encoded because " + why) if why else "")}
    height = int(info.get("height") or 0)
    res = resolution_bucket(height)
    scale = _AI_SCALE[res]
    fit_height = TARGET_H if (height * scale != TARGET_H) else None   # land exactly on 4K
    reason = ("%s %s → Topaz upscale %d×%s (keeps %s) → Resolve %s"
              % (res, rng, scale, " → fit 2160" if fit_height else " = 2160", rng,
                 "adds DV" if is_hdr else "adds HDR + DV"))
    return {"topaz": "upscale", "scale": scale, "res": res, "fit_height": fit_height,
            "resolve": resolve, "is_hdr": is_hdr, "reason": reason}


# What ffprobe ACTUALLY found, remembered per source basename so the app can stop guessing.
# The display reads a filename; this reads the file. A source named like HDR that is really
# SDR (or the reverse) otherwise leaves the row disagreeing with the engine forever — and a
# wrong suggestion is not harmless, because a user may PIN it, and a pin does take effect.
PROBE_CACHE = os.path.expanduser("~/.topaz-pipeline/probe_cache.json")
PROBE_CACHE_KEEP = 400


def _remember_probe(path, info):
    """Best-effort: record {basename: is_hdr}. Never raises — a cache miss just means the
    display falls back to the filename guess, which is where it started."""
    try:
        base = os.path.basename(path or "")
        if not base or info.get("transfer") in (None, ""):
            return                       # nothing was actually read; don't record a guess
        try:
            with open(PROBE_CACHE) as f:
                book = json.load(f)
            if not isinstance(book, dict):
                book = {}
        except Exception:
            book = {}
        book[base] = {"is_hdr": bool(info.get("is_hdr")),
                      "transfer": info.get("transfer")}
        if len(book) > PROBE_CACHE_KEEP:          # ring: drop the oldest insertions
            for k in list(book)[:len(book) - PROBE_CACHE_KEEP]:
                book.pop(k, None)
        os.makedirs(os.path.dirname(PROBE_CACHE), exist_ok=True)
        tmp = PROBE_CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(book, f)
        os.replace(tmp, PROBE_CACHE)
    except Exception:
        pass


def probed_is_hdr(name):
    """True/False if this basename has ever been probed, else None ('we don't know')."""
    try:
        with open(PROBE_CACHE) as f:
            book = json.load(f)
        e = book.get(os.path.basename(str(name or "")))
        return bool(e["is_hdr"]) if isinstance(e, dict) and "is_hdr" in e else None
    except Exception:
        return None


def plan_for(path: str) -> dict:
    info = probe_input(path)
    p = choose_plan(info)
    p["input"] = info
    _remember_probe(path, info)
    return p
