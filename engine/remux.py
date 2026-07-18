"""Remux stage: peak-cap the DV video, then put the original audio + subtitles back onto it.

Resolve renders a mute Dolby Vision .mov via VideoToolbox, whose VBR has NO peak control —
measured 139 Mbps single-second spikes on a 27.5 Mbps average, which underruns players
(S05E23 SHIELD glitching). So the remux stage now ALWAYS re-encodes through dvcap.py:
  0. dovi_tool extracts the per-frame RPU from Resolve's render; x265 re-encodes the video
     in its native Dolby Vision mode with a HARD VBV ceiling (settings `max_peak_mbps`,
     default 50) and interleaves the RPU itself. NO UNCAPPED FALLBACK (user-dictated):
     if capping fails in any way, the stage FAILS — an uncapped file IS the broken file.
     (This supersedes the Subler-optimize step: the x265 ES + fresh MP4Box mux has none of
     VideoToolbox's malformed layout; optimize_dv is kept only for reference/manual use.)
  1. ffmpeg extracts every audio + subtitle track from the source (its `-map`
     is reliable; subs -> mov_text) into a temp track file.
  2. MP4Box muxes the capped ES (`:dvp=8.1:xps_inband:fps=`) + those tracks into the final
     container, writing the Profile 8.1 dvcC box.
A verification gate then confirms DV 8.1 + tracks survived AND re-measures the actual
1-second peak of the shipped file (must be <= cap * tolerance) — this is where DV or the
cap silently dies, so a miss parks the episode instead of shipping a broken file.

The output container is MP4 by default, MKV only when the source has content MP4
can't hold — lossless audio (TrueHD/DTS-HD MA/PCM/FLAC) or bitmap subtitles (PGS/
VOBSUB). `container_ext()` decides; `remux()` dispatches on the output extension.
The MKV path wraps the capped ES in a video-only MP4 first (MP4Box writes the dvcC),
then a single ffmpeg copy muxes it with audio + all subs: ffmpeg's **Matroska** muxer
(unlike its mp4 one) PRESERVES the DV config record on copy. Audio is taken from
the CFR file; subtitles from the ORIGINAL download (the CFR pass no longer carries
them — they don't need frame-rate re-timing).
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction

import dvcap

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"
MP4BOX = "/opt/homebrew/bin/MP4Box"
SUBLERCLI = "/opt/homebrew/bin/SublerCLI"   # `brew install --cask sublercli` (x86_64 → needs Rosetta;
                                            # clear the Gatekeeper quarantine once: xattr -dr com.apple.quarantine)
_DOVI_RECORD = "DOVI configuration record"

# --- container choice: MP4 by default, MKV only when the content can't live in an MP4 -------
# Audio MP4 can't carry losslessly (must go to MKV so the master keeps the original track):
_LOSSLESS_AUDIO = {"truehd", "mlp", "flac", "alac"}    # + any pcm_*, + DTS-HD MA (checked below)
# Bitmap subtitles MP4 can't hold at all (and can't convert to mov_text) → MKV to preserve them:
_BITMAP_SUBS = {"hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub", "dvb_subtitle", "xsub"}


def _is_lossless_audio(codec: str, profile: str) -> bool:
    c = (codec or "").lower()
    if c in _LOSSLESS_AUDIO or c.startswith("pcm_"):
        return True
    # ffprobe reports DTS-HD Master Audio (lossless) as codec 'dts', profile 'DTS-HD MA'; the
    # lossy DTS variants (core, 'DTS-HD HRA', 'DTS Express') stay in MP4.
    return c == "dts" and "ma" in (profile or "").lower().split()


def needs_mkv(probe_json: str) -> bool:
    """True if the source has any stream MP4 can't hold — lossless audio or bitmap subtitles."""
    for s in json.loads(probe_json or "{}").get("streams", []):
        t = s.get("codec_type")
        if t == "audio" and _is_lossless_audio(s.get("codec_name"), s.get("profile")):
            return True
        if t == "subtitle" and (s.get("codec_name") or "").lower() in _BITMAP_SUBS:
            return True
    return False


def container_ext(source: str, ffprobe: str = FFPROBE) -> str:
    """'.mkv' if the source needs it (lossless audio / bitmap subs), else '.mp4'. Falls back to
    '.mp4' when the source can't be probed (e.g. not downloaded yet) — a safe, common-case default."""
    return ".mkv" if needs_mkv(_probe(source, ffprobe)) else ".mp4"


# ---- SMART LOUDNESS BOOST (remux-stage, MP4/AAC path only) --------------------------------
# Web 5.1 tracks ship quiet (The Office measured -23 LUFS; streaming target is -16). Each item
# is measured individually (ebur128 integrated) and gained to `audio_target_lufs` with a -2 dB
# limiter for the rare stings — dialogue dynamics untouched. Only BOOSTS (never attenuates), so
# already-normalized sources pass through bit-exact on the copy path. The MKV path is exempt:
# it exists to preserve LOSSLESS audio (TrueHD/DTS-HD MA), which we will not transcode.
AUDIO_MAX_GAIN_DB = 12.0
AUDIO_MIN_GAIN_DB = 0.5           # under this, not worth a lossy AAC re-encode
AUDIO_LIMITER = "alimiter=limit=0.794:attack=5:release=80:level=false"   # -2 dB ceiling


def build_loudness_probe_command(ffmpeg: str, src: str) -> list:
    return [ffmpeg, "-hide_banner", "-nostdin", "-i", src, "-map", "0:a:0",
            "-af", "ebur128=framelog=quiet", "-f", "null", "-"]


def parse_integrated_lufs(ebur_stderr: str):
    m = re.search(r"I:\s+(-?[\d.]+)\s+LUFS", ebur_stderr or "")
    return float(m.group(1)) if m else None


def boost_gain_db(measured, target, max_gain: float = AUDIO_MAX_GAIN_DB) -> float:
    """Gain to reach `target` LUFS — boost-only, clamped, 0.0 when unknown/off/negligible."""
    if measured is None or not target:
        return 0.0
    gain = min(float(max_gain), float(target) - float(measured))
    return round(gain, 2) if gain >= AUDIO_MIN_GAIN_DB else 0.0


def build_audio_boost_filter(gain_db: float) -> str:
    return f"volume={gain_db:.2f}dB,{AUDIO_LIMITER}"


def measure_lufs(src: str, ffmpeg=FFMPEG, timeout=300):
    try:
        r = subprocess.run(build_loudness_probe_command(ffmpeg, src),
                           capture_output=True, text=True, timeout=timeout)
        return parse_integrated_lufs(r.stderr)
    except Exception:
        return None


def build_extract_command(ffmpeg: str, cfr_source: str, orig_source: str, tracks_out: str,
                          gain_db: float = 0.0, include_subs: bool = True) -> list:
    """Pull audio from the CFR file (input 0) + text subtitles from the ORIGINAL (input 1) into an
    MP4 track file. Subtitles come from the original because the CFR pass no longer carries them
    (they don't need frame-rate re-timing); bitmap subs never reach the MP4 path (they force MKV).
    With `gain_db` > 0 the audio is loudness-boosted (volume + limiter -> aac_at 384k) instead of
    stream-copied; subtitles are unaffected either way.
    `-fix_sub_duration` on the original: a corrupt text cue with a NEGATIVE duration (S09E08's
    .ass had end < start, wrapping to 4294967213000 ms) is otherwise passed through by the
    mov_text encoder and the mp4 muxer aborts the WHOLE extract on it ("Error submitting a
    packet to the muxer: Invalid argument"). The flag recomputes cue durations at decode.
    `include_subs=False` is the last-resort retry: ship the master without subs rather than
    park the episode over a subtitle track."""
    audio = (["-filter:a", build_audio_boost_filter(gain_db),
              "-c:a", "aac_at", "-b:a", "384k"] if gain_db > 0 else [])
    subs = (["-map", "1:s?"] if include_subs else [])
    subs_codec = (["-c:s", "mov_text"] if include_subs else [])
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-y",
        "-i", cfr_source, "-fix_sub_duration", "-i", orig_source,
        "-map", "0:a:0", *subs,            # FIRST audio only (multi-track masters push the SHIELD into Direct Stream -> clock drift), subs (optional) from the original
        "-c", "copy", *audio, *subs_codec,  # subs -> mp4 timed text
        tracks_out,
    ]


def build_mkv_mux_command(ffmpeg: str, dv_video: str, cfr_source: str,
                          orig_source: str, output: str) -> list:
    """Single-pass ffmpeg mux for the MKV master: DV video (copy) + audio (from the CFR file) +
    ALL subtitles (from the original, incl. bitmap PGS). Unlike its mp4/mov muxer — which drops the
    Dolby Vision config box (that's why the MP4 path needs MP4Box) — ffmpeg's **Matroska** muxer
    PRESERVES the DOVI configuration record on copy. Validated on a real 8.1 master: DV + AAC + PGS
    all survive. Matroska also holds lossless audio (TrueHD/DTS-HD MA/PCM/FLAC) + bitmap subs, which
    is exactly why these titles route here instead of MP4."""
    return [ffmpeg, "-hide_banner", "-nostdin", "-y",
            "-i", dv_video, "-i", cfr_source, "-i", orig_source,
            "-map", "0:v:0", "-map", "1:a:0", "-map", "2:s?",   # video / FIRST audio only (SHIELD drift) / all subs
            "-c", "copy",
            output]


def build_mux_command(mp4box: str, dv_video: str, tracks: str, output: str,
                      interleave_ms: int = 500) -> list:
    """MP4Box mux — preserves the Dolby Vision config box that ffmpeg drops. `-inter`
    interleaves audio+video samples (chunks of `interleave_ms`) for the final master, on top of
    the structural repair the Subler optimize already did to the DV video (see optimize_dv)."""
    return [mp4box, "-add", dv_video, "-add", tracks,
            "-inter", str(interleave_ms), "-new", output]


def build_capped_mux_command(mp4box: str, hevc_es: str, fps: str, tracks: str, output: str,
                             interleave_ms: int = 500) -> list:
    """MP4Box mux for the CAPPED raw HEVC ES (x265 output). `:dvp=8.1` writes the DV config box
    signaling the RPUs x265 interleaved; `:fps=` is REQUIRED — a raw ES carries no container
    timing, so MP4Box would otherwise assume 25 fps and silently desync the master. NO
    `xps_inband`: that made the sample entry `hev1`, which the SHIELD refused to direct-play
    (S05E24 "keeps loading"); without it MP4Box hoists the parameter sets into hvcC -> `hvc1`,
    matching every master that played (x265's --repeat-headers keeps in-band copies too)."""
    return [mp4box, "-add", f"{hevc_es}:dvp=8.1:fps={fps}", "-add", tracks,
            "-inter", str(interleave_ms), "-new", output]


def build_capped_video_mux_command(mp4box: str, hevc_es: str, fps: str, output: str) -> list:
    """Video-only MP4 wrap of the capped ES — the MKV path's intermediate: MP4Box writes the
    DV config box, then ffmpeg's Matroska muxer (which preserves the DOVI record on copy,
    unlike its mp4 muxer) carries it into the .mkv alongside the audio + bitmap subs.
    Same no-`xps_inband` rule as above -> `hvc1`."""
    return [mp4box, "-add", f"{hevc_es}:dvp=8.1:fps={fps}", "-new", output]


def build_optimize_command(sublercli: str, src: str, dst: str) -> list:
    """SublerCLI's real `-optimize` — Subler's 'Optimize file' pass. It rewrites the mov/mp4 sample
    layout: the Resolve/VideoToolbox DV render ships an un-interleaved, malformed-layout file that
    makes hardware players (SHIELD / Apple TV) stutter AND that mkvmerge/MP4Box mis-parse. Subler's
    parser reads it robustly and writes a clean, interleaved file while KEEPING the Dolby Vision
    metadata. Runs on the Resolve `.mov` BEFORE the audio remux, so both the MP4 and MKV masters
    inherit the repaired video (interleave-only MP4Box `-inter` was never the full optimize)."""
    return [sublercli, "-source", src, "-dest", dst, "-optimize"]


def optimize_dv(dv_video: str, output: str, *, sublercli=SUBLERCLI, ffprobe=FFPROBE, timeout=None):
    """Subler-optimize the Resolve DV `.mov`. Returns (optimized_path, is_temp). On any failure —
    SublerCLI missing / not runnable / errored / dropped DV — returns the ORIGINAL dv_video so a
    Subler problem degrades to today's behaviour instead of blocking a ship (loud log either way)."""
    dst = output + ".dvopt.mp4"
    try:
        r = subprocess.run(build_optimize_command(sublercli, dv_video, dst),
                           capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        print(f"[remux] subler optimize could not run ({e}); shipping un-optimized video", flush=True)
        return dv_video, False
    if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
        print(f"[remux] subler optimize failed (rc={r.returncode}): {_tail(r.stderr)}; un-optimized", flush=True)
        _rm(dst); return dv_video, False
    if not parse_streams(_probe(dst, ffprobe)).get("dovi_profile"):   # DV must survive the optimize
        print("[remux] subler optimize dropped Dolby Vision; shipping un-optimized video", flush=True)
        _rm(dst); return dv_video, False
    return dst, True


def _rm(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def has_dolby_vision(stream: dict) -> bool:
    return any(sd.get("side_data_type") == _DOVI_RECORD
               for sd in stream.get("side_data_list", []))


def dolby_vision_profile(stream: dict):
    for sd in stream.get("side_data_list", []):
        if sd.get("side_data_type") == _DOVI_RECORD:
            p = sd.get("dv_profile")
            compat = sd.get("dv_bl_signal_compatibility_id")
            if p == 8 and compat == 1:
                return "8.1"
            if p == 8 and compat == 4:
                return "8.4"
            return f"{p}.x" if p is not None else None
    return None


def parse_streams(probe_json: str) -> dict:
    streams = json.loads(probe_json).get("streams", [])
    counts = {"video": 0, "audio": 0, "subtitle": 0, "dovi_profile": None, "video_tag": None}
    for s in streams:
        t = s.get("codec_type")
        if t in counts:
            counts[t] += 1
        if t == "video":
            counts["video_tag"] = counts["video_tag"] or s.get("codec_tag_string")
            if has_dolby_vision(s):
                counts["dovi_profile"] = dolby_vision_profile(s)
    return counts


def verify_remux(probe_json: str, min_audio: int = 1):
    s = parse_streams(probe_json)
    if not s["dovi_profile"]:
        return False, "no Dolby Vision RPU in output (DOVI configuration record missing)"
    if s["audio"] < min_audio:
        return False, f"no audio tracks (need >= {min_audio})"
    return True, f"DV {s['dovi_profile']} · {s['audio']} audio · {s['subtitle']} sub"


@dataclass
class RemuxResult:
    ok: bool
    output: str
    dovi_profile: str = None
    audio: int = 0
    subtitle: int = 0
    reason: str = ""


def _probe(path: str, ffprobe: str) -> str:
    r = subprocess.run([ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", path],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else "{}"


def _tail(text: str, n: int = 12) -> str:
    return "\n".join((text or "").splitlines()[-n:])


def _verify(output: str, ffprobe: str, optimized: bool = None) -> RemuxResult:
    probe_json = _probe(output, ffprobe)
    s = parse_streams(probe_json)
    ok, reason = verify_remux(probe_json)
    if ok and optimized is not None:            # record whether Subler -optimize engaged (else it's
        reason += " · optimized" if optimized else " · un-optimized"   # invisible in the logbook)
    return RemuxResult(ok, output, s["dovi_profile"], s["audio"], s["subtitle"], reason)


def remux(dv_video: str, cfr_source: str, orig_source: str, output: str, *,
          cap_mbps: int = dvcap.DEFAULT_PEAK_MBPS, audio_target_lufs=None, boundaries=None,
          abort=None, on_progress=None, on_plan=None, should_pause=None,
          ffmpeg=FFMPEG, mp4box=MP4BOX, ffprobe=FFPROBE, timeout=None) -> RemuxResult:
    """Peak-cap the Resolve DV video (dvcap: RPU extract -> x265 native-DV VBV re-encode), then
    put the original audio + subtitles back onto it. HARD GATE, no uncapped fallback: any
    failure (RPU, encode, frame mismatch, DV lost, measured peak over cap) fails the stage.
    Audio comes from the CFR file (same timing as the pipeline video); subs from the ORIGINAL.
    Dispatches on `output`'s extension, decided by `container_ext` upstream.
    `should_pause`: polled between x265 segments — yields a benign "paused:" result so the
    finisher can hold this remux while the run thread's Resolve is active (Resolve gets the
    whole machine, user-dictated); every finished segment is kept and the retry resumes here."""
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    info = dvcap.probe_video(dv_video, ffprobe)
    # GUARD BEFORE touching the resume state (review-caught): a transient ffprobe failure returns
    # frames=0, which would build a mismatched manifest and make ensure_segdir WIPE hours of
    # finished segments. Fail the attempt cleanly instead — the segdir is untouched, next try resumes.
    if info["frames"] <= 0:
        return RemuxResult(False, output,
                           reason="could not probe render frame count — resume state left intact")
    audio_note = ""
    # segdir PERSISTS across finisher attempts so the ~75-min x265 pass resumes from the last
    # finished ~5-min segment (RPU + segments live here) — deleted ONLY on a fully verified ship.
    segdir = output + ".remuxsegs"
    # resume ONLY if the previous attempt encoded the SAME render with the SAME params —
    # a re-rendered dv_video or changed cap would otherwise concat stale/wrong segments
    dvcap.ensure_segdir(segdir, dvcap.resume_manifest(
        dv_video, cap_mbps, info["frames"], info["fps"], dvcap.SEG_SECONDS, boundaries=boundaries))
    rpu = os.path.join(segdir, "rpu.bin")
    hevc = output + ".capped.hevc"          # transient: the concat of the segments, rebuilt each attempt
    tracks = dv_mp4 = None
    try:
        if not (os.path.exists(rpu) and os.path.getsize(rpu) > 0):   # resume keeps the extracted RPU
            ok, why = dvcap.extract_rpu(dv_video, rpu, ffmpeg=ffmpeg, timeout=timeout)
            if not ok:
                return RemuxResult(False, output, reason=why)
        # RPU frame count is GROUND TRUTH (one per coded frame). The container nb_frames HEADER
        # can over-report a VideoToolbox .mov's decodable tail; planning/slicing the RPU against
        # the header instead → out-of-range dovi_tool remove → permanent park (review-caught).
        real_frames = dvcap.rpu_frame_count(rpu)
        if real_frames <= 0:
            return RemuxResult(False, output, reason="could not read RPU frame count — resume state intact")
        ok, frames, why = dvcap.encode_capped_segmented(
            dv_video, rpu, hevc, cap_mbps, segdir=segdir,
            total_frames=real_frames, fps=info["fps"], boundaries=boundaries,
            master_display=info["master_display"], max_cll=info["max_cll"],
            abort=abort, on_progress=on_progress, on_plan=on_plan,
            should_pause=should_pause, ffmpeg=ffmpeg)
        if not ok:
            if why.startswith("paused:"):
                return RemuxResult(False, output, reason=why)   # benign hold, not a failure
            return RemuxResult(False, output, reason="cap encode: " + why)
        if frames != real_frames:
            return RemuxResult(False, output,
                               reason=f"frame count changed by cap encode: {frames} != {real_frames}")
        if not output.lower().endswith(".mkv"):
            # MP4 path: extract audio (CFR) + text subs (original) → track file, built ONCE — it's
            # video-independent, so the peak-repair rungs below reuse it.
            # SMART LOUDNESS BOOST: measure this item's integrated LUFS, gain to the target (boost-only,
            # limiter-capped). Validated on the cheap tracks file BEFORE the mux — a bad landing falls
            # back to a bit-exact copy of the original audio (never fails the 75-min x265 pass over audio).
            gain = boost_gain_db(measure_lufs(cfr_source, ffmpeg), audio_target_lufs)
            tracks = output + ".tracks.mp4"   # temp, next to output (on scratch)
            subs_note = ""
            for attempt_gain in ([gain, 0.0] if gain > 0 else [0.0]):
                ex = subprocess.run(build_extract_command(ffmpeg, cfr_source, orig_source, tracks,
                                                          gain_db=attempt_gain),
                                    capture_output=True, text=True, timeout=timeout)
                if ex.returncode != 0:
                    # LAST-RESORT RETRY, no subs: a still-broken subtitle track (even past
                    # -fix_sub_duration) must not park the episode — audio is essential,
                    # subs are not. Same gain; the landing check below still applies.
                    ex = subprocess.run(build_extract_command(ffmpeg, cfr_source, orig_source,
                                                              tracks, gain_db=attempt_gain,
                                                              include_subs=False),
                                        capture_output=True, text=True, timeout=timeout)
                    if ex.returncode != 0:
                        return RemuxResult(False, output, reason="extract failed: " + _tail(ex.stderr))
                    subs_note = " · subs dropped (unconvertible track)"
                if attempt_gain <= 0:
                    break
                landed = measure_lufs(tracks, ffmpeg)
                want = float(audio_target_lufs)
                if landed is not None and abs(landed - want) <= 1.5:
                    audio_note = f" · audio +{attempt_gain:.1f}dB → {landed:.1f} LUFS"
                    break
                audio_note = " · audio unboosted (landing off target — kept original)"
            audio_note += subs_note
        # ---- mux + verify + PEAK GATE, with a tightening ladder on a peak miss ------------------
        # VBV bufsize == maxrate legally allows a 1-second burst past cap × tolerance, and an
        # identical retry reuses the identical segments — it can never pass (user-caught: a movie
        # parked at 58.6 > 50 five times, shipped nothing). On a miss, LOCALIZE the burst to its
        # segment(s) and re-encode only those at 85% then 70% of the cap, then re-gate.
        segs_plan = dvcap.plan_segments(real_frames, info["fps"], dvcap.SEG_SECONDS,
                                        boundaries=boundaries)
        repair_note, buckets, peak = "", None, 0.0
        for tight in (None, int(cap_mbps * 0.85), int(cap_mbps * 0.70)):
            if tight is not None:
                offenders = dvcap.over_gate_segments(buckets, segs_plan, info["fps"], cap_mbps)
                if not offenders:
                    return RemuxResult(False, output,
                                       reason=f"peak over cap and burst not localizable: "
                                              f"{peak:.1f} Mbps > {cap_mbps} (shipped nothing)")
                ok, why = dvcap.reencode_segments_tighter(
                    dv_video, rpu, segdir, offenders, tight,
                    total_frames=real_frames, fps=info["fps"], boundaries=boundaries,
                    master_display=info["master_display"], max_cll=info["max_cll"],
                    abort=abort, ffmpeg=ffmpeg)
                if not ok:
                    return RemuxResult(False, output, reason="peak repair: " + why)
                # every segment is complete now → this call just re-verifies counts and re-concats
                ok, frames, why = dvcap.encode_capped_segmented(
                    dv_video, rpu, hevc, cap_mbps, segdir=segdir,
                    total_frames=real_frames, fps=info["fps"], boundaries=boundaries,
                    master_display=info["master_display"], max_cll=info["max_cll"],
                    abort=abort, ffmpeg=ffmpeg)
                if not ok or frames != real_frames:
                    return RemuxResult(False, output, reason="peak repair concat: " + why)
                repair_note = f" · peak repair: {len(offenders)} seg(s) re-capped @ {tight} Mbps"
            if output.lower().endswith(".mkv"):
                # wrap the ES in a video-only MP4 (writes the dvcC), then one ffmpeg copy-mux to MKV
                dv_mp4 = output + ".dv.mp4"
                vx = subprocess.run(build_capped_video_mux_command(mp4box, hevc, info["fps"], dv_mp4),
                                    capture_output=True, text=True, timeout=timeout)
                if vx.returncode != 0:
                    return RemuxResult(False, output, reason="dv wrap failed: " + _tail(vx.stderr))
                mx = subprocess.run(build_mkv_mux_command(ffmpeg, dv_mp4, cfr_source, orig_source, output),
                                    capture_output=True, text=True, timeout=timeout)
                if mx.returncode != 0:
                    return RemuxResult(False, output, reason="mkv mux failed: " + _tail(mx.stderr))
            else:
                mx = subprocess.run(build_capped_mux_command(mp4box, hevc, info["fps"], tracks, output),
                                    capture_output=True, text=True, timeout=timeout)
                if mx.returncode != 0:
                    return RemuxResult(False, output, reason="mux failed: " + _tail(mx.stderr))
            res = _verify(output, ffprobe)
            if not res.ok:
                return res
            if output.lower().endswith(".mp4"):
                tag = parse_streams(_probe(output, ffprobe)).get("video_tag")
                if tag != "hvc1":                          # hev1 masters DON'T direct-play (SHIELD)
                    res.ok = False
                    res.reason = f"sample entry is {tag!r}, need hvc1 (hev1 broke SHIELD direct play)"
                    _rm(output)
                    return res
            buckets = dvcap.video_peak_buckets(output, ffprobe)   # re-measure the SHIPPED file
            peak = max(buckets.values()) if buckets else 0.0
            if dvcap.peak_ok(peak, cap_mbps):
                res.reason += f" · peak {peak:.1f} ≤ {cap_mbps} Mbps cap" + repair_note + audio_note
                shutil.rmtree(segdir, ignore_errors=True)  # SUCCESS: the segments won't be needed again
                return res
            _rm(output)                                    # never leave an over-peak master around
        return RemuxResult(False, output,
                           reason=f"peak still over cap after encode + repair: "
                                  f"{peak:.1f} Mbps > {cap_mbps} (shipped nothing)")
    finally:
        # transient only — segdir is KEPT on any non-success return so the next attempt resumes
        _rm(hevc); _rm(tracks); _rm(dv_mp4)


def remux_inject(dv_video: str, cfr_source: str, orig_source: str, output: str, *,
                 audio_target_lufs=None, abort=None,
                 ffmpeg=FFMPEG, mp4box=MP4BOX, ffprobe=FFPROBE, timeout=None) -> RemuxResult:
    """HIGH-BITRATE 4K HDR10 FAST PATH (user-dictated): ship the ORIGINAL video stream with
    Resolve's Dolby Vision RPU injected — NO re-encode, NO peak cap (the source's own peaks
    were already direct-playing before the pipeline touched it). The DV render contributes
    ONLY its RPU; its video is discarded. HARD GATE: the RPU frame count must exactly equal
    the source's coded-frame count — a misaligned RPU time-shifts every frame's DV trim,
    which is worse than no DV, so a mismatch ships NOTHING."""
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    info = dvcap.probe_video(dv_video, ffprobe)
    if info["frames"] <= 0:      # transient ffprobe blip — fail cleanly, resume state intact
        return RemuxResult(False, output,
                           reason="could not probe render frame count — resume state left intact")
    n_src = dvcap.count_hevc_frames(orig_source, ffprobe)   # container packets = coded frames
    if n_src <= 0:
        return RemuxResult(False, output, reason="could not count source frames")
    src_info = dvcap.probe_video(orig_source, ffprobe)
    try:
        if Fraction(str(src_info["fps"])) != Fraction(str(info["fps"])):
            return RemuxResult(False, output,
                               reason=f"fps mismatch: source {src_info['fps']} vs render "
                                      f"{info['fps']} — RPU cannot align (shipped nothing)")
    except (ValueError, ZeroDivisionError):
        return RemuxResult(False, output, reason="unreadable fps — RPU alignment unverifiable")
    # Small resume dir: keeps ONLY the extracted RPU (tens of MB). Same name the cleanup
    # stage already sweeps; the manifest identity wipes a stale RPU if the render changed.
    segdir = output + ".remuxsegs"
    try:
        st = os.stat(dv_video)
        src_id = f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        src_id = "missing"
    dvcap.ensure_segdir(segdir, {"mode": "inject", "src": src_id,
                                 "frames": n_src, "fps": str(info["fps"])})
    rpu = os.path.join(segdir, "rpu.bin")
    src_es = output + ".src.hevc"           # transient: the source's Annex-B ES (original bits)
    inj_es = output + ".inject.hevc"        # transient: same ES with the RPU interleaved
    tracks = dv_mp4 = None
    try:
        if not (os.path.exists(rpu) and os.path.getsize(rpu) > 0):
            ok, why = dvcap.extract_rpu(dv_video, rpu, ffmpeg=ffmpeg, timeout=timeout)
            if not ok:
                return RemuxResult(False, output, reason=why)
        n_rpu = dvcap.rpu_frame_count(rpu)
        if n_rpu != n_src:
            return RemuxResult(False, output,
                               reason=f"RPU/source frame mismatch: rpu {n_rpu} != source {n_src} "
                                      f"— Resolve render is not frame-aligned with the original "
                                      f"(shipped nothing)")
        if abort is not None and abort.is_set():
            return RemuxResult(False, output, reason="aborted")
        ex = subprocess.run(dvcap.build_annexb_file_command(ffmpeg, orig_source, src_es),
                            capture_output=True, text=True, timeout=timeout)
        if ex.returncode != 0 or not (os.path.exists(src_es) and os.path.getsize(src_es) > 0):
            return RemuxResult(False, output, reason="source ES extract failed: " + _tail(ex.stderr))
        if abort is not None and abort.is_set():
            return RemuxResult(False, output, reason="aborted")
        ij = subprocess.run(dvcap.build_inject_command(dvcap.DOVI_TOOL, src_es, rpu, inj_es),
                            capture_output=True, text=True, timeout=timeout)
        if ij.returncode != 0 or not (os.path.exists(inj_es) and os.path.getsize(inj_es) > 0):
            return RemuxResult(False, output,
                               reason="RPU inject failed: " + _tail(ij.stderr or ij.stdout))
        n_inj = dvcap.count_hevc_frames(inj_es, ffprobe)    # inject writes AUDs → 1 packet/frame
        if n_inj != n_src:
            return RemuxResult(False, output,
                               reason=f"injected ES frame count changed: {n_inj} != {n_src} "
                                      f"(shipped nothing)")
        audio_note = ""
        if output.lower().endswith(".mkv"):
            dv_mp4 = output + ".dv.mp4"
            vx = subprocess.run(build_capped_video_mux_command(mp4box, inj_es, info["fps"], dv_mp4),
                                capture_output=True, text=True, timeout=timeout)
            if vx.returncode != 0:
                return RemuxResult(False, output, reason="dv wrap failed: " + _tail(vx.stderr))
            mx = subprocess.run(build_mkv_mux_command(ffmpeg, dv_mp4, cfr_source, orig_source, output),
                                capture_output=True, text=True, timeout=timeout)
            if mx.returncode != 0:
                return RemuxResult(False, output, reason="mkv mux failed: " + _tail(mx.stderr))
        else:
            # same audio machinery as the cap path (boost validated, falls back to a copy);
            # the CFR gate guarantees cfr audio is a bit-exact stream copy of the source's
            gain = boost_gain_db(measure_lufs(cfr_source, ffmpeg), audio_target_lufs)
            tracks = output + ".tracks.mp4"
            subs_note = ""
            for attempt_gain in ([gain, 0.0] if gain > 0 else [0.0]):
                ex = subprocess.run(build_extract_command(ffmpeg, cfr_source, orig_source, tracks,
                                                          gain_db=attempt_gain),
                                    capture_output=True, text=True, timeout=timeout)
                if ex.returncode != 0:
                    # LAST-RESORT RETRY, no subs (same rule as the cap path): a broken
                    # subtitle track must not park a fast-path item over nice-to-haves.
                    ex = subprocess.run(build_extract_command(ffmpeg, cfr_source, orig_source,
                                                              tracks, gain_db=attempt_gain,
                                                              include_subs=False),
                                        capture_output=True, text=True, timeout=timeout)
                    if ex.returncode != 0:
                        return RemuxResult(False, output, reason="extract failed: " + _tail(ex.stderr))
                    subs_note = " · subs dropped (unconvertible track)"
                if attempt_gain <= 0:
                    break
                landed = measure_lufs(tracks, ffmpeg)
                want = float(audio_target_lufs)
                if landed is not None and abs(landed - want) <= 1.5:
                    audio_note = f" · audio +{attempt_gain:.1f}dB → {landed:.1f} LUFS"
                    break
                audio_note = " · audio unboosted (landing off target — kept original)"
            audio_note += subs_note
            mx = subprocess.run(build_capped_mux_command(mp4box, inj_es, info["fps"], tracks, output),
                                capture_output=True, text=True, timeout=timeout)
            if mx.returncode != 0:
                return RemuxResult(False, output, reason="mux failed: " + _tail(mx.stderr))
        res = _verify(output, ffprobe)
        if not res.ok:
            return res
        if output.lower().endswith(".mp4"):
            tag = parse_streams(_probe(output, ffprobe)).get("video_tag")
            if tag != "hvc1":                          # hev1 masters DON'T direct-play (SHIELD)
                res.ok = False
                res.reason = f"sample entry is {tag!r}, need hvc1 (hev1 broke SHIELD direct play)"
                _rm(output)
                return res
        # NO peak gate here (user-dictated): the shipped video IS the source's own encode.
        res.reason += " · original stream + injected RPU (no re-encode, peak gate n/a)" + audio_note
        shutil.rmtree(segdir, ignore_errors=True)      # SUCCESS: the RPU won't be needed again
        return res
    finally:
        # the two big ESes are transient EVERY attempt (recreated in minutes); only the small
        # rpu.bin persists (in segdir) for resume — segdir is KEPT on any non-success return
        _rm(src_es); _rm(inj_es); _rm(tracks); _rm(dv_mp4)


def main(argv=None):
    import argparse, sys
    ap = argparse.ArgumentParser(description="Remux original audio+subs onto the mute DV video.")
    ap.add_argument("dv_video", help="mute Dolby Vision video from Resolve")
    ap.add_argument("cfr_source", help="the CFR file (audio donor)")
    ap.add_argument("orig_source", help="the original download (subtitle donor)")
    ap.add_argument("output", help="master; .mkv or .mp4 decides the mux path")
    args = ap.parse_args(argv)
    res = remux(args.dv_video, args.cfr_source, args.orig_source, args.output)
    print(("OK: " if res.ok else "FAILED: ") + res.reason)
    return 0 if res.ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
