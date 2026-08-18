"""Remux stage: peak-cap the DV video, then put the original audio + subtitles back onto it.

Resolve renders a mute Dolby Vision .mov via VideoToolbox, whose VBR has NO peak control —
measured 139 Mbps single-second spikes on a 27.5 Mbps average, which underruns players
(S05E23 SHIELD glitching). So the remux stage now ALWAYS re-encodes through dvcap.py:
  0. dovi_tool extracts the per-frame RPU from Resolve's render; x265 re-encodes the video
     in its native Dolby Vision mode with a HARD VBV ceiling (settings `max_peak_mbps`,
     default 50) and interleaves the RPU itself. NO UNCAPPED FALLBACK (user-dictated):
     if capping fails in any way, the stage FAILS — an uncapped file IS the broken file.
  1. ffmpeg extracts every audio + subtitle track from the source (its `-map`
     is reliable; subs -> mov_text) into a temp track file.
  2. MP4Box muxes the capped ES (`:dvp=8.1:fps=`) + those tracks into the final
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
import contextlib
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
        "-map", "0:a", *subs,              # ALL audio tracks from CFR, subs (optional) from the original
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
            "-map", "0:v:0", "-map", "1:a", "-map", "2:s?",   # video / all audio / all subs
            "-c", "copy",
            output]

_MP4BOX_UNSAFE = re.compile(r"[+#:,@]")


@contextlib.contextmanager
def mp4box_safe_input(path: str):
    """Yield a path MP4Box's `-add` can definitely open — `path` itself when it's already
    safe, else a temporary hardlink beside it. The link is ALWAYS removed: a leftover
    would keep the (huge) transient alive by holding a second reference to its inode."""
    base = os.path.basename(path)
    if not _MP4BOX_UNSAFE.search(base):
        yield path
        return
    safe = os.path.join(os.path.dirname(path) or ".", "_mp4box_" + _MP4BOX_UNSAFE.sub("_", base))
    try:
        if os.path.exists(safe):
            os.remove(safe)
        os.link(path, safe)
    except OSError:
        yield path            # couldn't link — try the real path rather than fail outright
        return
    try:
        yield safe
    finally:
        try: os.remove(safe)
        except OSError: pass


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


def verify_remux(probe_json: str, min_audio: int = 1, require_dv: bool = True):
    """`require_dv=False` for an item PINNED to SDR output: that master legitimately carries no
    DOVI record, so the DV assertion would fail a perfectly good file. Everything else (audio
    presence, and the hvc1 + peak checks at the call site) still applies — only the Dolby
    Vision claim is relaxed, and only when the item asked for no Dolby Vision."""
    s = parse_streams(probe_json)
    if require_dv and not s["dovi_profile"]:
        return False, "no Dolby Vision RPU in output (DOVI configuration record missing)"
    if s["audio"] < min_audio:
        return False, f"no audio tracks (need >= {min_audio})"
    kind = f"DV {s['dovi_profile']}" if s["dovi_profile"] else "SDR"
    return True, f"{kind} · {s['audio']} audio · {s['subtitle']} sub"


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


def _verify(output: str, ffprobe: str) -> RemuxResult:
    probe_json = _probe(output, ffprobe)
    s = parse_streams(probe_json)
    ok, reason = verify_remux(probe_json)
    return RemuxResult(ok, output, s["dovi_profile"], s["audio"], s["subtitle"], reason)


def remux(dv_video: str, cfr_source: str, orig_source: str, output: str, *,
          cap_mbps: int = dvcap.DEFAULT_PEAK_MBPS, audio_target_lufs=None,
          audio_gain_db=None, boundaries=None,
          abort=None, on_progress=None, on_plan=None, should_pause=None, on_repair=None,
          encode_source=None, rpu_mode=None,
          ffmpeg=FFMPEG, mp4box=MP4BOX, ffprobe=FFPROBE, timeout=None) -> RemuxResult:
    """Peak-cap the Resolve DV video (dvcap: RPU extract -> x265 native-DV VBV re-encode), then
    put the original audio + subtitles back onto it. HARD GATE, no uncapped fallback: any
    failure (RPU, encode, frame mismatch, DV lost, measured peak over cap) fails the stage.
    Audio comes from the CFR file (same timing as the pipeline video); subs from the ORIGINAL.
    Dispatches on `output`'s extension, decided by `container_ext` upstream.
    `should_pause`: polled between x265 segments — yields a benign "paused:" result so the
    finisher can hold this remux while the run thread's Resolve is active (Resolve gets the
    whole machine, user-dictated); every finished segment is kept and the retry resumes here.
    `encode_source`: the peak-gated rpu-only fallback (SHIELD DV ceiling) — the RPU still
    comes from `dv_video` (Resolve's analysis), but the video that gets the capped re-encode
    is THIS file (the original HDR10 stream; frame-aligned because Resolve rendered from the
    same CFR copy) instead of the render, whose floor-bitrate video was never meant to ship."""
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    enc = encode_source or dv_video   # the video the x265 pass re-encodes (RPU: always dv_video)
    info = dvcap.probe_video(enc, ffprobe)
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
        dv_video, cap_mbps, info["frames"], info["fps"], dvcap.SEG_SECONDS, boundaries=boundaries,
        encode_source=encode_source, rpu_mode=rpu_mode))
    rpu = os.path.join(segdir, "rpu.bin")
    hevc = output + ".capped.hevc"          # transient: the concat of the segments, rebuilt each attempt
    tracks = dv_mp4 = None
    try:
        if not (os.path.exists(rpu) and os.path.getsize(rpu) > 0):   # resume keeps the extracted RPU
            ok, why = dvcap.extract_rpu(dv_video, rpu, mode=rpu_mode, ffmpeg=ffmpeg,
                                        timeout=timeout)
            if not ok:
                return RemuxResult(False, output, reason=why)
        # RPU frame count is GROUND TRUTH (one per coded frame). The container nb_frames HEADER
        # can over-report a VideoToolbox .mov's decodable tail; planning/slicing the RPU against
        # the header instead → out-of-range dovi_tool remove → permanent park (review-caught).
        real_frames = dvcap.rpu_frame_count(rpu)
        if real_frames <= 0:
            return RemuxResult(False, output, reason="could not read RPU frame count — resume state intact")
        if encode_source is not None:
            # FAIL FAST before the hours-long encode: when the RPU donor and the encoded
            # video are different files (rpu-only fallback / companion combine), a frame
            # mismatch means a different cut — the post-encode count check would only
            # catch it after burning the whole x265 pass.
            n_enc = dvcap.count_hevc_frames(enc, ffprobe)
            if n_enc > 0 and n_enc != real_frames:
                return RemuxResult(False, output,
                                   reason=f"RPU/source frame mismatch: rpu {real_frames} != "
                                          f"source {n_enc} — the RPU donor is not frame-aligned "
                                          f"with the encoded video (shipped nothing)")
        ok, frames, why = dvcap.encode_capped_segmented(
            enc, rpu, hevc, cap_mbps, segdir=segdir,
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
            # `audio_gain_db` = a gain already decided for this item (TV: the SEASON's, set
            # by its first episode — see audiogain). Only measure when nobody decided for us.
            gain = (round(float(audio_gain_db), 2) if audio_gain_db is not None
                    else boost_gain_db(measure_lufs(cfr_source, ffmpeg), audio_target_lufs))
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
                    enc, rpu, segdir, offenders, tight,
                    total_frames=real_frames, fps=info["fps"], boundaries=boundaries,
                    master_display=info["master_display"], max_cll=info["max_cll"],
                    abort=abort, on_repair=on_repair, ffmpeg=ffmpeg)
                if not ok:
                    return RemuxResult(False, output, reason="peak repair: " + why)
                # every segment is complete now → this call just re-verifies counts and re-concats
                ok, frames, why = dvcap.encode_capped_segmented(
                    enc, rpu, hevc, cap_mbps, segdir=segdir,
                    total_frames=real_frames, fps=info["fps"], boundaries=boundaries,
                    master_display=info["master_display"], max_cll=info["max_cll"],
                    abort=abort, ffmpeg=ffmpeg)
                if not ok or frames != real_frames:
                    return RemuxResult(False, output, reason="peak repair concat: " + why)
                repair_note = f" · peak repair: {len(offenders)} seg(s) re-capped @ {tight} Mbps"
            if output.lower().endswith(".mkv"):
                # wrap the ES in a video-only MP4 (writes the dvcC), then one ffmpeg copy-mux to MKV
                dv_mp4 = output + ".dv.mp4"
                with mp4box_safe_input(hevc) as _hevc_in:
                    vx = subprocess.run(build_capped_video_mux_command(mp4box, _hevc_in, info["fps"], dv_mp4),
                                    capture_output=True, text=True, timeout=timeout)
                if vx.returncode != 0:
                    return RemuxResult(False, output, reason="dv wrap failed: " + _tail(vx.stderr))
                mx = subprocess.run(build_mkv_mux_command(ffmpeg, dv_mp4, cfr_source, orig_source, output),
                                    capture_output=True, text=True, timeout=timeout)
                if mx.returncode != 0:
                    return RemuxResult(False, output, reason="mkv mux failed: " + _tail(mx.stderr))
            else:
                with mp4box_safe_input(hevc) as _hevc_in, mp4box_safe_input(tracks) as _tracks_in:
                    mx = subprocess.run(build_capped_mux_command(mp4box, _hevc_in, info["fps"], _tracks_in, output),
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


# ---- STEP PROGRESS for the fast (no-re-encode) remuxes -------------------------------------
# The inject/ship paths are a chain of blocking subprocess phases with no encoder progress
# to parse — but the LONG phases each write one growing file with a known target size, so a
# background size-poller yields an honest per-step percentage (user-asked 2026-08-06: a
# progress bar with labeled steps, one at a time). Label-only steps report pct=None.

class _StepWatch:
    """Context manager: poll `path`'s size toward `target` every 2 s and report
    on_step(label, pct) — capped at 99 (the phase's own exit reports completion)."""

    def __init__(self, on_step, label, path, target):
        self._on, self._label, self._path, self._target = on_step, label, path, target
        self._stop = None
        self._thread = None

    def __enter__(self):
        if self._on:
            self._on(self._label, 0.0)
            if self._target and self._target > 0:
                import threading
                self._stop = threading.Event()
                def _poll():
                    while not self._stop.wait(2.0):
                        try:
                            sz = os.path.getsize(self._path)
                        except OSError:
                            continue
                        self._on(self._label, min(99.0, 100.0 * sz / self._target))
                self._thread = threading.Thread(target=_poll, daemon=True)
                self._thread.start()
        return self

    def __exit__(self, *exc):
        if self._stop is not None:
            self._stop.set()
            self._thread.join(timeout=5)
        return False


def _fsize(path) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def remux_inject(dv_video: str, cfr_source: str, orig_source: str, output: str, *,
                 audio_target_lufs=None, abort=None, on_step=None,
                 rpu_mode=None, skip_inject=False, convert_es=False,
                 ffmpeg=FFMPEG, mp4box=MP4BOX, ffprobe=FFPROBE, timeout=None) -> RemuxResult:
    """HIGH-BITRATE 4K HDR10 FAST PATH (user-dictated): ship the ORIGINAL video stream
    (orig_source) with a Dolby Vision RPU from `dv_video` injected — no re-encode (the
    caller peak-gates before choosing this path). The RPU donor contributes ONLY its
    metadata; its video is discarded. HARD GATE: the RPU frame count must exactly equal
    the source's coded-frame count — a misaligned RPU time-shifts every frame's DV trim,
    which is worse than no DV, so a mismatch ships NOTHING.

    Companion-combine extensions (all default-off — zero change for the classic path):
    `rpu_mode=2` converts a Profile 7 donor's RPU to 8.1 during extraction;
    `skip_inject=True` means orig_source ALREADY carries the wanted RPU inline (the
    winner is itself the real-DV file) — no extraction, no inject, its own bits ship;
    `convert_es=True` (with skip_inject) runs dovi_tool `-m 2 convert --discard` on the
    copied ES first, single-layer-ifying an inline-P7 winner."""
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
                               reason=f"fps mismatch: source {src_info['fps']} vs RPU donor "
                                      f"{info['fps']} — RPU cannot align (shipped nothing)")
    except (ValueError, ZeroDivisionError):
        return RemuxResult(False, output, reason="unreadable fps — RPU alignment unverifiable")
    src_es = output + ".src.hevc"           # transient: the source's Annex-B ES (original bits)
    inj_es = output + ".inject.hevc"        # transient: same ES with the RPU interleaved
    tracks = dv_mp4 = None
    segdir = None
    try:
        if not skip_inject:
            # Small resume dir: keeps ONLY the extracted RPU (tens of MB). Same name the
            # cleanup stage sweeps; the manifest identity wipes a stale RPU if the donor
            # (or the extraction mode) changed.
            segdir = output + ".remuxsegs"
            try:
                st = os.stat(dv_video)
                src_id = f"{st.st_size}:{int(st.st_mtime)}"
            except OSError:
                src_id = "missing"
            manifest = {"mode": "inject", "src": src_id,
                        "frames": n_src, "fps": str(info["fps"])}
            if rpu_mode is not None:
                manifest["rpu_mode"] = int(rpu_mode)
            dvcap.ensure_segdir(segdir, manifest)
            rpu = os.path.join(segdir, "rpu.bin")
            if not (os.path.exists(rpu) and os.path.getsize(rpu) > 0):
                if on_step:
                    on_step("extracting DV metadata", None)   # reads the whole donor; no
                ok, why = dvcap.extract_rpu(dv_video, rpu, mode=rpu_mode,        # watchable output
                                            ffmpeg=ffmpeg, timeout=timeout)
                if not ok:
                    return RemuxResult(False, output, reason=why)
            n_rpu = dvcap.rpu_frame_count(rpu)
            if n_rpu != n_src:
                return RemuxResult(False, output,
                                   reason=f"RPU/source frame mismatch: rpu {n_rpu} != source "
                                          f"{n_src} — the RPU donor is not frame-aligned with "
                                          f"the shipped video (shipped nothing)")
        if abort is not None and abort.is_set():
            return RemuxResult(False, output, reason="aborted")
        with _StepWatch(on_step, "copying the original video", src_es, _fsize(orig_source)):
            ex = subprocess.run(dvcap.build_annexb_file_command(ffmpeg, orig_source, src_es),
                                capture_output=True, text=True, timeout=timeout)
        if ex.returncode != 0 or not (os.path.exists(src_es) and os.path.getsize(src_es) > 0):
            return RemuxResult(False, output, reason="source ES extract failed: " + _tail(ex.stderr))
        if abort is not None and abort.is_set():
            return RemuxResult(False, output, reason="aborted")
        if skip_inject:
            if convert_es:
                # inline-P7 winner: drop the EL, rewrite the RPU to 8.1, in one pass
                with _StepWatch(on_step, "converting DV to profile 8.1", inj_es, _fsize(src_es)):
                    cv = subprocess.run(dvcap.build_dovi_convert_command(dvcap.DOVI_TOOL,
                                                                         src_es, inj_es),
                                        capture_output=True, text=True, timeout=timeout)
                if cv.returncode != 0 or not (os.path.exists(inj_es) and os.path.getsize(inj_es) > 0):
                    return RemuxResult(False, output,
                                       reason="DV convert failed: " + _tail(cv.stderr or cv.stdout))
            else:
                inj_es = src_es              # the winner's own bits, RPU already inline
        else:
            with _StepWatch(on_step, "injecting DV metadata", inj_es, _fsize(src_es)):
                ij = subprocess.run(dvcap.build_inject_command(dvcap.DOVI_TOOL, src_es, rpu, inj_es),
                                    capture_output=True, text=True, timeout=timeout)
            if ij.returncode != 0 or not (os.path.exists(inj_es) and os.path.getsize(inj_es) > 0):
                return RemuxResult(False, output,
                                   reason="RPU inject failed: " + _tail(ij.stderr or ij.stdout))
        n_inj = dvcap.count_hevc_frames(inj_es, ffprobe)    # AUDs → 1 packet/frame
        if n_inj != n_src:
            return RemuxResult(False, output,
                               reason=f"processed ES frame count changed: {n_inj} != {n_src} "
                                      f"(shipped nothing)")
        audio_note = ""
        if output.lower().endswith(".mkv"):
            dv_mp4 = output + ".dv.mp4"
            with _StepWatch(on_step, "wrapping the DV video", dv_mp4, _fsize(inj_es)), \
                 mp4box_safe_input(inj_es) as _es_in:
                vx = subprocess.run(build_capped_video_mux_command(mp4box, _es_in, info["fps"], dv_mp4),
                                capture_output=True, text=True, timeout=timeout)
            if vx.returncode != 0:
                return RemuxResult(False, output, reason="dv wrap failed: " + _tail(vx.stderr))
            with _StepWatch(on_step, "muxing the master", output, _fsize(dv_mp4)):
                mx = subprocess.run(build_mkv_mux_command(ffmpeg, dv_mp4, cfr_source, orig_source, output),
                                    capture_output=True, text=True, timeout=timeout)
            if mx.returncode != 0:
                return RemuxResult(False, output, reason="mkv mux failed: " + _tail(mx.stderr))
        else:
            # same audio machinery as the cap path (boost validated, falls back to a copy);
            # the CFR gate guarantees cfr audio is a bit-exact stream copy of the source's
            if on_step:
                on_step("preparing audio", None)
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
            with _StepWatch(on_step, "muxing the master", output, _fsize(inj_es)), \
                 mp4box_safe_input(inj_es) as _es_in, mp4box_safe_input(tracks) as _tracks_in:
                mx = subprocess.run(build_capped_mux_command(mp4box, _es_in, info["fps"], _tracks_in, output),
                                capture_output=True, text=True, timeout=timeout)
            if mx.returncode != 0:
                return RemuxResult(False, output, reason="mux failed: " + _tail(mx.stderr))
        if on_step:
            on_step("verifying the master", None)
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
        # NO peak gate here: the caller gated the source's peaks before choosing this path.
        if skip_inject and convert_es:
            note = " · original stream, DV converted to 8.1 (no re-encode)"
        elif skip_inject:
            note = " · original DV stream shipped as-is (no re-encode)"
        else:
            note = " · original stream + injected RPU (no re-encode)"
        res.reason += note + audio_note
        if segdir:
            shutil.rmtree(segdir, ignore_errors=True)  # SUCCESS: the RPU won't be needed again
        return res
    finally:
        # the two big ESes are transient EVERY attempt (recreated in minutes); only the small
        # rpu.bin persists (in segdir) for resume — segdir is KEPT on any non-success return
        _rm(src_es); _rm(inj_es); _rm(tracks); _rm(dv_mp4)


def combine(winner: str, rpu_source: str, audio_source: str, output: str, *,
            rpu_inline=False, rpu_profile="", capped=False,
            cap_mbps: int = dvcap.DEFAULT_PEAK_MBPS, boundaries=None,
            audio_target_lufs=None, abort=None, on_step=None, on_progress=None,
            on_plan=None, should_pause=None, on_repair=None,
            ffmpeg=FFMPEG, mp4box=MP4BOX, ffprobe=FFPROBE, timeout=None) -> RemuxResult:
    """COMPANION COMBINE (user-dictated): one best-of master from two copies of a film.
    `winner` = the video that ships (the genuinely better HDR10 base, or the real-DV file
    itself); `rpu_source` = where the Dolby Vision metadata comes from (the real-DV copy,
    or the Resolve render when neither copy has an RPU); `audio_source` = the file whose
    ENTIRE audio ships (best track wins the whole file — its compat AC-3 rides along).
    Subtitles ship from the winner. Output is always MKV (TrueHD-class audio).

    `capped=False` → stream path: the winner's own bits ship (RPU grafted/converted as
    needed) — the caller peak-gates first. `capped=True` → the enforced-VBV x265
    native-DV re-encode of the winner with the SAME RPU (real DV survives a re-encode —
    prioritized over Resolve DV either way, user-dictated).

    A thin dispatcher: both paths are the proven remux_inject / remux machinery — the
    frame-count and fps hard gates in there are exactly the different-cut gates a
    cross-release graft needs (a mismatch ships NOTHING; the stage parks it).

    AUDIO SYNC: audio is stream-copied, never retimed — alignment is guaranteed by
    proving the donor is the SAME CUT as the shipped video. Two donor configurations
    carry that proof already (audio from the winner itself is trivial; audio from the
    real-RPU donor is transitively proven by the RPU-vs-winner frame gate). The
    remaining one — audio from the other copy while the RPU comes from elsewhere
    (Resolve fallback, or an inline-DV winner borrowing audio) — is gated HERE: the
    donor's video must match the winner frame-for-frame, else its audio would drift."""
    p7 = str(rpu_profile).startswith("7")
    mode = 2 if p7 else None
    if audio_source not in (winner, rpu_source):
        nw = dvcap.count_hevc_frames(winner, ffprobe)
        na = dvcap.count_hevc_frames(audio_source, ffprobe)
        if nw <= 0 or na <= 0:
            return RemuxResult(False, output,
                               reason="could not count frames to verify the audio donor "
                                      "— resume state intact")
        if na != nw:
            return RemuxResult(False, output,
                               reason=f"audio donor is a different cut: {na} != {nw} frames "
                                      f"— its audio would drift (shipped nothing)")
        try:
            fa = dvcap.probe_video(audio_source, ffprobe)["fps"]
            fw = dvcap.probe_video(winner, ffprobe)["fps"]
            if Fraction(str(fa)) != Fraction(str(fw)):
                return RemuxResult(False, output,
                                   reason=f"audio donor is a different cut: fps {fa} vs {fw} "
                                          f"— its audio would drift (shipped nothing)")
        except (ValueError, ZeroDivisionError):
            return RemuxResult(False, output,
                               reason="unreadable fps — audio-donor alignment unverifiable")
    if capped:
        return remux(rpu_source, audio_source, winner, output,
                     cap_mbps=cap_mbps, audio_target_lufs=audio_target_lufs,
                     boundaries=boundaries, abort=abort, on_progress=on_progress,
                     on_plan=on_plan, should_pause=should_pause, on_repair=on_repair,
                     encode_source=winner, rpu_mode=mode,
                     ffmpeg=ffmpeg, mp4box=mp4box, ffprobe=ffprobe, timeout=timeout)
    return remux_inject(rpu_source, audio_source, winner, output,
                        audio_target_lufs=audio_target_lufs, abort=abort, on_step=on_step,
                        rpu_mode=mode, skip_inject=rpu_inline,
                        convert_es=(rpu_inline and p7),
                        ffmpeg=ffmpeg, mp4box=mp4box, ffprobe=ffprobe, timeout=timeout)


def remux_ship_render(dv_video: str, cfr_source: str, orig_source: str, output: str, *,
                      cap_mbps: int = dvcap.DEFAULT_PEAK_MBPS, audio_target_lufs=None,
                      abort=None, on_step=None, ffmpeg=FFMPEG, mp4box=MP4BOX, ffprobe=FFPROBE,
                      timeout=None) -> RemuxResult:
    """YOUTUBE FAST REMUX (user-asked 2026-08-06: videos should move through fast). The
    Resolve render IS already a DV 8.1 HEVC file — the hour-class x265 pass exists only
    to force video under the SHIELD peak cap, and a render encoded at the YouTube target
    bitrate (stages caps it well under the cap) normally measures safe already. So: GATE
    the render's measured 1-s peak; under → ship its video STREAM-COPIED (ES copy + mux +
    audio — minutes), over → reason "render-over-cap: ..." and the caller falls back to
    the normal capped re-encode, ladder and all. Same mux/audio/verify tail as
    remux_inject; the render↔CFR frame alignment was already gated when the resolve stage
    accepted the render (render_is_complete). No resume dir — a kill just redoes minutes."""
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    info = dvcap.probe_video(dv_video, ffprobe)
    if info["frames"] <= 0:
        return RemuxResult(False, output, reason="could not probe render frame count")
    mbps = dvcap.video_peak_1s_mbps(dv_video, ffprobe)
    if not dvcap.peak_ok(mbps, cap_mbps):
        return RemuxResult(False, output,
                           reason=f"render-over-cap: {mbps:.1f} Mbps > {cap_mbps} cap — "
                                  f"using the capped re-encode")
    es = output + ".ship.hevc"              # transient: the render's ES, RPU already in-band
    tracks = dv_mp4 = None
    try:
        if abort is not None and abort.is_set():
            return RemuxResult(False, output, reason="aborted")
        with _StepWatch(on_step, "copying the render's video", es, _fsize(dv_video)):
            ex = subprocess.run(dvcap.build_annexb_file_command(ffmpeg, dv_video, es),
                                capture_output=True, text=True, timeout=timeout)
        if ex.returncode != 0 or not (os.path.exists(es) and os.path.getsize(es) > 0):
            return RemuxResult(False, output, reason="render ES extract failed: " + _tail(ex.stderr))
        audio_note = ""
        if output.lower().endswith(".mkv"):
            dv_mp4 = output + ".dv.mp4"
            with _StepWatch(on_step, "wrapping the DV video", dv_mp4, _fsize(es)), \
                 mp4box_safe_input(es) as _es_in:
                vx = subprocess.run(build_capped_video_mux_command(mp4box, _es_in, info["fps"], dv_mp4),
                                capture_output=True, text=True, timeout=timeout)
            if vx.returncode != 0:
                return RemuxResult(False, output, reason="dv wrap failed: " + _tail(vx.stderr))
            with _StepWatch(on_step, "muxing the master", output, _fsize(dv_mp4)):
                mx = subprocess.run(build_mkv_mux_command(ffmpeg, dv_mp4, cfr_source, orig_source, output),
                                    capture_output=True, text=True, timeout=timeout)
            if mx.returncode != 0:
                return RemuxResult(False, output, reason="mkv mux failed: " + _tail(mx.stderr))
        else:
            # same audio machinery as the cap/inject paths (boost validated, falls back to copy)
            if on_step:
                on_step("preparing audio", None)
            gain = boost_gain_db(measure_lufs(cfr_source, ffmpeg), audio_target_lufs)
            tracks = output + ".tracks.mp4"
            subs_note = ""
            for attempt_gain in ([gain, 0.0] if gain > 0 else [0.0]):
                ex = subprocess.run(build_extract_command(ffmpeg, cfr_source, orig_source, tracks,
                                                          gain_db=attempt_gain),
                                    capture_output=True, text=True, timeout=timeout)
                if ex.returncode != 0:
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
            with _StepWatch(on_step, "muxing the master", output, _fsize(es)), \
                 mp4box_safe_input(es) as _es_in, mp4box_safe_input(tracks) as _tracks_in:
                mx = subprocess.run(build_capped_mux_command(mp4box, _es_in, info["fps"], _tracks_in, output),
                                capture_output=True, text=True, timeout=timeout)
            if mx.returncode != 0:
                return RemuxResult(False, output, reason="mux failed: " + _tail(mx.stderr))
        if on_step:
            on_step("verifying the master", None)
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
        shipped = dvcap.video_peak_1s_mbps(output, ffprobe)   # belt: same bits, re-measured
        if not dvcap.peak_ok(shipped, cap_mbps):
            _rm(output)
            return RemuxResult(False, output,
                               reason=f"render-over-cap: shipped file measured {shipped:.1f} Mbps "
                                      f"> {cap_mbps} — using the capped re-encode")
        res.reason += (f" · render shipped as-is (peak {mbps:.1f} ≤ {cap_mbps} Mbps, "
                       f"no re-encode)") + audio_note
        return res
    finally:
        _rm(es); _rm(tracks); _rm(dv_mp4)


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
