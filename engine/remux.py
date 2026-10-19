"""Remux stage: peak-cap the DV video, then put the original audio + subtitles back onto it.

The native render writes a mute Dolby Vision .mov via VideoToolbox, whose VBR has NO peak control —
measured 139 Mbps single-second spikes on a 27.5 Mbps average, which underruns players
(S05E23 SHIELD glitching). So the remux stage now ALWAYS re-encodes through dvcap.py:
  0. dovi_tool extracts the per-frame RPU from the render; x265 re-encodes the video
     in its native Dolby Vision mode with a HARD VBV ceiling (settings `max_peak_mbps`,
     default 50) and interleaves the RPU itself. NO UNCAPPED FALLBACK (user-dictated):
     if capping fails in any way, the stage FAILS — an uncapped file IS the broken file.
     A chunk-lane item (`remux(native=)`) has no render: the RPU is
     dvnative's generated one and x265 reads Topaz's ProRes chunks through the SDR->PQ LUT.
  1. ffmpeg builds the TRACKS file (`<output>.tracks.mka`, Matroska): every audio track from
     the CFR file (loudness-boosted or stream-copied — see build_tracks_command) plus the
     subtitles, chapters and font attachments of the ORIGINAL download (the CFR pass no longer
     carries subtitles — they don't need frame-rate re-timing).
  2. mkvmerge muxes the capped ES (`--default-duration 0:<fps>p`, the timing a raw ES does not
     carry) + the tracks file into `<output>.part`. It writes the Dolby Vision configuration
     record (BlockAdditionMapping dvvC) ITSELF from the RPU NALs and the HEVC VUI, and the HDR10
     colour description / mastering display / light level from mkv_colour_args.
A verification gate then confirms DV 8.1 (profile 8, compat 1), the tracks and the exact frame
count survived AND re-measures the actual 1-second peak of the shipped file (must be <= cap *
tolerance) — this is where DV or the cap silently dies, so a miss parks the episode instead of
shipping a broken file. Only a master that passed every gate is renamed from `.part` onto
`output` (atomic_ship): a killed mkvmerge leaves a Matroska file whose header already says DV
8.1 + audio, so a partial master must never sit at the name the resume logic trusts.

EVERY master is Matroska (user decision 2026-10-18: no MP4Box, every output MKV); `output` must
end in .mkv. Matroska holds every source's audio (TrueHD/DTS-HD MA/PCM/FLAC included) and its
subtitles as they are (SRT/ASS/PGS/VobSub), converting only what it has no codec for (mov_text).
The CFR intermediate keeps its own container (cfr_container_ext): it is not an output.
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
MKVMERGE = "/opt/homebrew/bin/mkvmerge"
_DOVI_RECORD = "DOVI configuration record"

# --- THE MASTER IS ALWAYS MATROSKA ----------------------------------------------------------
MASTER_EXT = ".mkv"


def is_master_container(output: str) -> bool:
    """Every master is .mkv. A '.mp4' output can only be a pre-2026-10-18 resume: mkvmerge writes
    Matroska whatever the name says, so muxing it would ship Matroska bytes under an MP4 name."""
    return str(output).lower().endswith(MASTER_EXT)


def _not_mkv(output: str) -> "RemuxResult":
    return RemuxResult(False, output, reason=f"every master is Matroska: {os.path.basename(output)!r} "
                                             f"does not end in {MASTER_EXT} (shipped nothing)")


# --- the CFR INTERMEDIATE's container (not the master's) -------------------------------------
# The CFR copy (<stem>_cfr.*) carries the AUDIO the master gets, so it must hold the source's audio
# as it is: MP4 unless the source has lossless audio (or bitmap subtitles, the old rule kept so a
# CFR never changes container under an in-flight item). It stays MP4 by default ON PURPOSE:
# Matroska's 1 ms timestamps re-declare a rounded rate on a stream copy, which once cost Topaz a
# nine-hour run (topaz.py declared_rate_mismatch). It never decides the master's container.
_LOSSLESS_AUDIO = {"truehd", "mlp", "flac", "alac"}    # + any pcm_*, + DTS-HD MA (checked below)
_BITMAP_SUBS = {"hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub", "dvb_subtitle", "xsub"}


def _is_lossless_audio(codec: str, profile: str) -> bool:
    c = (codec or "").lower()
    if c in _LOSSLESS_AUDIO or c.startswith("pcm_"):
        return True
    # ffprobe reports DTS-HD Master Audio (lossless) as codec 'dts', profile 'DTS-HD MA'; the
    # lossy DTS variants (core, 'DTS-HD HRA', 'DTS Express') fit an MP4.
    return c == "dts" and "ma" in (profile or "").lower().split()


def needs_mkv(probe_json: str) -> bool:
    """True if the source has any stream an MP4 CFR copy can't hold — lossless audio or bitmap
    subtitles."""
    for s in json.loads(probe_json or "{}").get("streams", []):
        t = s.get("codec_type")
        if t == "audio" and _is_lossless_audio(s.get("codec_name"), s.get("profile")):
            return True
        if t == "subtitle" and (s.get("codec_name") or "").lower() in _BITMAP_SUBS:
            return True
    return False


def cfr_container_ext(source: str, ffprobe: str = FFPROBE) -> str:
    """The CFR INTERMEDIATE's extension: '.mkv' if the source needs it (lossless audio / bitmap
    subs), else '.mp4'. Falls back to '.mp4' when the source can't be probed (not downloaded
    yet). The master is always MASTER_EXT, whatever this says."""
    return ".mkv" if needs_mkv(_probe(source, ffprobe)) else ".mp4"


# --- SUBTITLES INTO MATROSKA ------------------------------------------------------------------
# Matroska holds these as they are (ffmpeg's matroska muxer has a codec ID for each):
SUBS_COPY = {"subrip", "ass", "ssa", "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}
# ...and has NO codec ID for these, so they are converted to SRT (S_TEXT/UTF8, which every Plex
# client reads). mov_text is the common one: MP4 originals and youtarr's embedded YouTube subtitles
# carry it, and an ffmpeg `-c copy` of it into Matroska fails the whole file ("Subtitle codec
# mov_text is not supported" — tested). WebVTT is converted too: Plex's Matroska WebVTT support is
# unproven, SRT is not.
SUBS_TO_SRT = {"mov_text", "webvtt", "text", "subviewer", "subviewer1", "microdvd", "mpl2",
               "realtext", "sami", "vplayer", "pjs", "jacosub", "stl"}
# Anything else (eia_608 captions, dvb_teletext, arib_caption, hdmv_text_subtitle) is left out —
# a track the container cannot carry must not cost the master its other subtitles.


def subtitle_plan(orig_source: str, ffprobe: str = FFPROBE) -> list:
    """[(n, action)] for the n-th subtitle stream of `orig_source`: 'copy' or 'srt', in source
    order; streams Matroska cannot carry are left out. [] when it has none or can't be probed
    (the tracks file then carries no subtitles rather than failing)."""
    plan = []
    try:
        streams = json.loads(_probe(orig_source, ffprobe) or "{}").get("streams", [])
    except ValueError:
        return []
    n = 0
    for s in streams:
        if s.get("codec_type") != "subtitle":
            continue
        codec = (s.get("codec_name") or "").lower()
        if codec in SUBS_COPY:
            plan.append((n, "copy"))
        elif codec in SUBS_TO_SRT:
            plan.append((n, "srt"))
        n += 1
    return plan


def subtitle_args(plan: list, input_idx: int = 1) -> list:
    """PURE. ffmpeg -map / per-output-stream codec args for `plan` (subtitle_plan), reading the
    subtitles from input `input_idx`."""
    args = []
    for k, (n, action) in enumerate(plan):
        args += ["-map", f"{input_idx}:s:{n}", f"-c:s:{k}", "copy" if action == "copy" else "srt"]
    return args


# ---- SMART LOUDNESS BOOST -------------------------------------------------------------------
# Web 5.1 tracks ship quiet (The Office measured -23 LUFS; streaming target is -16). Each item
# is measured individually (ebur128 integrated) and gained to `audio_target_lufs` with a -2 dB
# limiter for the rare stings — dialogue dynamics untouched. Only BOOSTS (never attenuates), so
# already-normalized sources pass through bit-exact on the copy path. The rule is about the
# CODEC, not the container: LOSSLESS audio (TrueHD/DTS-HD MA/PCM/FLAC) is never transcoded — a
# boosted AAC copy of it leads and the untouched original follows (boost_keeping_original_args).
AUDIO_MAX_GAIN_DB = 12.0
AUDIO_MIN_GAIN_DB = 0.5           # under this, not worth a lossy AAC re-encode
# How much of the REQUESTED gain a pass has to actually deliver to be believed. The alimiter
# shaves the top off a wide-range film mix, so landing short is normal and expected; landing
# almost nowhere means the filter did not do its job, and shipping a lossy re-encode for that
# is a bad trade. Half is generous to the limiter and still catches a broken pass.
ACHIEVED_FRACTION = 0.5
AUDIO_LIMITER = "alimiter=limit=0.794:attack=5:release=80:level=false"   # -2 dB ceiling


def build_loudness_probe_command(ffmpeg: str, src: str) -> list:
    return [ffmpeg, "-hide_banner", "-nostdin", "-i", src, "-map", "0:a:0",
            "-af", "ebur128=framelog=quiet", "-f", "null", "-"]


def parse_integrated_lufs(ebur_stderr: str):
    m = re.search(r"I:\s+(-?[\d.]+)\s+LUFS", ebur_stderr or "")
    return float(m.group(1)) if m else None


def _unboosted_note(measured, landed, target) -> str:
    """The remux line when a boost is measured and then declined. Says the NUMBERS: "landing
    off target" with nothing to check it against is unfalsifiable from the log, and what it
    measured and where it went is exactly what you want when a file ships quiet."""
    def db(v):
        try:
            return f"{float(v):.1f}"
        except (TypeError, ValueError):
            return "?"
    return (f" · audio unboosted ({db(measured)} → {db(landed)} LUFS, "
            f"no closer to {db(target)} — kept original)")


def landing_ok(landed, target, gain, measured=None, tol: float = 1.5) -> bool:
    """Is the boosted audio BETTER than what we started with?

    That is the whole question, and the previous two answers both got it wrong by asking
    something narrower.

    First it asked "did it land on target", which is unreachable whenever the gain hits
    AUDIO_MAX_GAIN_DB: a -30 LUFS mix boosted the full +12 lands at -18, misses a +/-1.5
    window around -16, and the good result is discarded — so the file ships at -30, the
    quietest possible outcome (live-caught 2026-08-19).

    Then it asked "did the level move by the amount asked for" for capped boosts only. That
    still failed the ordinary case, because the alimiter pulls peaks down and a wide-range
    film mix therefore lands SHORT of the arithmetic prediction without being capped at all.
    A.I. Artificial Intelligence shipped unboosted for exactly that reason (user-caught
    2026-08-24) — with "normalize audio" ON and a plain AAC track, which is the case the
    feature exists for.

    So: keep the boost when it got CLOSER to the target. On target within `tol` is success;
    otherwise it has to have closed at least AUDIO_MIN_GAIN_DB of the gap, which is the same
    threshold that decides a boost was worth doing at all. Anything that landed no nearer —
    or overshot past the target by more than it started away from it — is not an improvement
    and the original ships.
    """
    if landed is None:
        return False
    landed, target = float(landed), float(target)
    if landed > target + tol:
        return False                      # LOUDER than asked is not normalising, however
                                          # much "closer" it is — that way lies clipping
    if abs(landed - target) <= tol:
        return True                       # on target: the ordinary success
    if measured is None:
        return False                      # nothing to compare against — stay strict
    measured = float(measured)
    achieved = landed - measured          # what the pass actually moved it by
    if achieved < AUDIO_MIN_GAIN_DB:
        return False                      # it did nothing; not worth a lossy re-encode
    if gain and achieved < ACHIEVED_FRACTION * float(gain):
        return False                      # asked for +12 and got +0.5 — that is a broken
                                          # pass, not a limiter shaving the top off
    return abs(landed - target) <= abs(measured - target) - AUDIO_MIN_GAIN_DB



def boost_gain_db(measured, target, max_gain: float = AUDIO_MAX_GAIN_DB) -> float:
    """Gain to reach `target` LUFS — boost-only, clamped, 0.0 when unknown/off/negligible."""
    if measured is None or not target:
        return 0.0
    gain = min(float(max_gain), float(target) - float(measured))
    return round(gain, 2) if gain >= AUDIO_MIN_GAIN_DB else 0.0


# Channel layouts aac_at is TRUSTED with. AudioToolbox mangles exotic layouts instead of
# refusing them: fed 6.1(back) it returns a file ~8 dB QUIETER than its input — at any
# bitrate — so a +8.8 dB boost measured -1.9 after encode and every landing check rightly
# refused it (A.I. Artificial Intelligence, isolated 2026-08-24: volume-only +8.4, +limiter
# +6.1, +aac_at -1.9; the same chain on 5.1 lands perfectly). Anything else is folded to an
# EXPLICIT safe layout before the encoder sees it: a lossy re-encode is happening
# regardless, and a correct 5.1 beats a broken 6.1.
#
# The target must be a single explicit layout, chosen per source. Handing aformat a LIST and
# letting ffmpeg negotiate picked MONO for the 6.1 film — whatever the list's order — and
# the constraint has to sit AHEAD of the boost: downmixing after the limiter measured
# 4.7 dB worse on the same input.
AAC_AT_SAFE_LAYOUTS = ("mono", "stereo", "5.1", "5.1(side)")


def aac_at_target_layout(src, ffprobe=FFPROBE):
    """The explicit layout to fold `src`'s first audio track to before aac_at — or None when
    it is already one the encoder handles (the overwhelmingly common case: nothing moves).
    Unreadable probes return None too: the old behaviour, never a surprise downmix."""
    if not src:
        return None
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a:0",
                              "-show_entries", "stream=channels,channel_layout",
                              "-of", "csv=p=0", str(src)],
                             capture_output=True, text=True, errors="replace", timeout=60).stdout.strip()
        parts = (out.splitlines() or [""])[0].split(",")
        channels = int(parts[0] or 0)
        layout = parts[1].strip() if len(parts) > 1 else ""
    except Exception:
        return None
    if not channels or layout in AAC_AT_SAFE_LAYOUTS:
        return None
    return "5.1" if channels >= 5 else "stereo"


def build_audio_boost_filter(gain_db: float, src=None) -> str:
    """The boost chain. `src` = the file whose audio will be encoded — when its layout is one
    aac_at mishandles, an explicit downmix is prepended (see aac_at_target_layout)."""
    fold = aac_at_target_layout(src)
    prefix = f"aformat=channel_layouts={fold}," if fold else ""
    return f"{prefix}volume={gain_db:.2f}dB,{AUDIO_LIMITER}"


def measure_lufs(src: str, ffmpeg=FFMPEG, timeout=300):
    try:
        r = subprocess.run(build_loudness_probe_command(ffmpeg, src),
                           capture_output=True, text=True, errors="replace", timeout=timeout)
        return parse_integrated_lufs(r.stderr)
    except Exception:
        return None


def build_tracks_command(ffmpeg: str, cfr_source: str, orig_source: str, tracks_out: str,
                         gain_db: float = 0.0, subs=None, keep_original_audio: int = 0,
                         atmos_lead=None, n_audio: int = 0, fix_sub_duration: bool = False) -> list:
    """The master's TRACKS file (Matroska): every audio track from the CFR file (input 0) + the
    subtitles, chapters and font attachments of the ORIGINAL (input 1). mkvmerge then muxes it
    with the capped/injected/render ES (build_master_mux_command). Subtitles come from the
    original because the CFR pass no longer carries them (they don't need frame-rate re-timing).
    `subs` = subtitle_plan(orig_source): each stream copied as it is or converted to SRT ([] or
    None = no subtitles: the last-resort retry ships the master without them rather than park
    the item over a subtitle track). Font attachments ride along only with subtitles (they exist
    for ASS).
    With `gain_db` > 0 the audio is loudness-boosted (volume + limiter -> aac_at 384k) instead of
    stream-copied; `keep_original_audio` (the source's audio track count, set when any of it is
    LOSSLESS) makes the boosted copy of track 0 LEAD and every original track follow untouched
    (boost_keeping_original_args) — lossless audio is never transcoded away.
    `fix_sub_duration` (-fix_sub_duration on the original): a corrupt text cue with a NEGATIVE
    duration (S09E08's .ass had end < start, wrapping to 4294967213000 ms) once aborted the whole
    MP4 track file. It is a RETRY only (_prepare_tracks), never the default: it holds every
    DECODED cue until the next one arrives, so the LAST line of each converted track never comes
    out (measured 2026-10-19: 34 of 36 lines — every MP4 master shipped before this lost its last
    subtitle line). Copied tracks are not decoded, and the ASS reader already mends such a cue.
    NO SUBTITLE IS DEFAULT: Matroska players switch a default subtitle track ON, and an MP4
    source marks its first subtitle "enabled", which ffmpeg reads as default (live-caught in the
    real-mux test, 2026-10-19). `-disposition:s -default` clears that flag and keeps `forced` (the
    foreign-dialogue lines a player shows on its own); `-default_mode infer_no_subs` stops the
    muxer inferring one. `-map_metadata:g -1`: no GLOBAL tags, so no release name rides into the
    master's title — global only: a bare `-map_metadata -1` also strips every stream's and chapter's
    tags (the languages Plex picks tracks by, track and chapter names, and the filename/mimetype
    of font attachments, without which the whole file fails — review-caught 2026-10-19)."""
    # ATMOS IS NEVER TOUCHED, and it LEADS. The boost re-encodes every audio track to AAC,
    # which silently discarded the Atmos metadata on any title that carried it — Don't Look
    # Up shipped with its 'Dolby Digital Plus + Dolby Atmos' track flattened to AAC LC
    # (user-caught 2026-08-22). When an Atmos track exists nothing is re-encoded at all, and
    # it is mapped first so it is the track a player picks.
    if atmos_lead is not None:
        amaps = audio_map_args(0, n_audio, atmos_lead)
        audio = lead_track_disposition_args(n_audio)
    elif gain_db > 0 and keep_original_audio > 0:
        amaps = ["-map", "0:a:0", "-map", "0:a"]      # boosted copy first, then the originals
        audio = boost_keeping_original_args(gain_db, keep_original_audio, src=cfr_source)
    elif gain_db > 0:
        amaps = ["-map", "0:a"]            # ALL audio tracks from CFR, in their own order
        audio = ["-filter:a", build_audio_boost_filter(gain_db, src=cfr_source),
                 "-c:a", "aac_at", "-b:a", "384k"]
    else:
        amaps, audio = ["-map", "0:a"], []
    sub_args = subtitle_args(subs or [], input_idx=1)
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-y",
        "-i", cfr_source, *(["-fix_sub_duration"] if fix_sub_duration else []), "-i", orig_source,
        *amaps,                             # audio from the CFR...
        "-c", "copy", *audio,
        *sub_args,                          # ...subtitles (copied / -> SRT) from the original...
        *(["-map", "1:t?"] if subs else []),   # ...and the fonts those subtitles render with
        *(["-disposition:s", "-default"] if subs else []),
        "-map_metadata:g", "-1", "-default_mode", "infer_no_subs",
        "-f", "matroska", tracks_out,
    ]


LOSSLESS_AUDIO = ("truehd", "mlp", "flac", "alac")


def is_lossless_audio_codec(codec: str, profile: str = "") -> bool:
    """Audio we will never transcode. Plain DTS core is lossy; DTS-HD MA is not."""
    c, p = (codec or "").lower(), (profile or "").lower()
    if any(c.startswith(x) for x in LOSSLESS_AUDIO) or c.startswith("pcm"):
        return True
    return c.startswith("dts") and ("ma" in p or "lossless" in p)


def has_lossless_audio(path: str, ffprobe=FFPROBE) -> bool:
    """True if ANY audio track is lossless — then none of them are touched. Unknown counts as
    lossless: refusing to boost is recoverable (the Finished list can revise it later),
    transcoding a TrueHD track is not."""
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a",
                              "-show_entries", "stream=codec_name,profile",
                              "-of", "csv=p=0", path],
                             capture_output=True, text=True, errors="replace", timeout=60).stdout.strip()
    except Exception:
        return True
    if not out:
        return True
    for line in out.splitlines():
        parts = line.split(",")
        if is_lossless_audio_codec(parts[0], parts[1] if len(parts) > 1 else ""):
            return True
    return False


MAX_INTERLEAVE_GAP_MB = 8.0     # a master past this stalls players that stream it


def interleave_gap_mb(path: str, ffprobe=FFPROBE, at=(60, 300, 1200, 3600)) -> float:
    """Worst distance IN THE FILE between video and its own audio at the same instant, in MB.
    A well-interleaved master keeps this well under a megabyte; the value grows with the
    interleave window, so this is the number that says whether a player can stream it without
    holding the gap in memory. -1.0 when it cannot be measured (never fail a master on a
    probe that did not run)."""
    def _pos(sel, t):
        try:
            out = subprocess.run([ffprobe, "-v", "error", "-select_streams", sel,
                                  # read PAST the seek keyframe up to t: the window must
                                  # cover a whole GOP (x265 keyint 250 = 10.4 s at 23.976),
                                  # or the packet at t is never printed and the probe reads
                                  # as "unmeasurable" — %+1 only ever showed the keyframe
                                  "-read_intervals", "%d%%+15" % t,
                                  "-show_entries", "packet=pts_time,pos",
                                  "-of", "csv=p=0", path],
                                 capture_output=True, text=True, errors="replace", timeout=120).stdout
        except Exception:
            return None
        # THE FIRST PACKET AT OR AFTER t — not the first packet ffprobe prints. A video seek
        # lands on the preceding KEYFRAME and ffprobe reports from there, while an audio seek
        # is exact, so taking the first line compared a keyframe-aligned video position with
        # a time-exact audio one: the "gap" was the previous GOP's length in bytes. x265's
        # default keyint is 250 frames = 10.4 s at 23.976, and a talking-heads YouTube video
        # at ~8 Mbps measured exactly 10.4 MB, failed "badly interleaved" eight times, and
        # held the DV doorstep shut for 15 hours (live-caught 2026-09-05). What a
        # player must have read by time t is the packet it decodes AT t, for both streams.
        for ln in out.splitlines():
            part = ln.split(",")
            try:
                pts, pos = float(part[0]), int(part[1])
            except (ValueError, IndexError):
                continue
            if pts + 1e-6 >= t:
                return pos
        return None

    worst = -1.0
    for t in at:
        pv, pa = _pos("v:0", t), _pos("a:0", t)
        if pv is None or pa is None:
            continue                      # past the end, or unreadable — not a failure
        worst = max(worst, abs(pv - pa) / 1e6)
    return worst


# SUBTITLES MUST SIT WITH THEIR PICTURE TOO (live-caught 2026-10-18). MP4Box's -inter 100 kept
# audio beside its video, but wrote every mov_text sample of a 28-minute episode inside the first
# ~150 MB: the subtitle for 8:20 sat at byte 42 MB while its picture sat at 706 MB. The SHIELD's
# player reads tracks in file order, so it jumped back for every subtitle, opening a new range
# request that streamed megabytes each time, and the picture stuttered (Community S06E08, every
# S06 WEB-DL master and It's Always Sunny S01-S02). interleave_gap_mb above only compares AUDIO
# with video, so it passed them all. Measured on the shipped files: worst 1.2-2.9 GB, p99 the
# same; after an ffmpeg -c copy re-interleave: p99 0.5-1.0 MB, worst ~20-24 MB.
MAX_SUBTITLE_GAP_MB = 8.0       # p99 of subtitle-to-picture distance (the audio gate's own limit)
MAX_SUBTITLE_WORST_MB = 32.0    # no single subtitle further (~20 s of picture; the SHIELD buffers 50 MB)


def subtitle_gaps(video: list, subs: list) -> tuple:
    """PURE. (worst, p99) distance in MB between each subtitle sample and the video sample at its
    instant. `video` = [(pts, pos)], `subs` = [(pts, pos, size)]. A trailing EMPTY sample (<= 2
    bytes, the end marker ffmpeg writes at the very end of the file — never displayed) does not
    count. (0.0, 0.0) with no subtitles; (-1.0, -1.0) with no video to compare against."""
    import bisect
    if not subs:
        return 0.0, 0.0
    v = sorted(video)
    if not v:
        return -1.0, -1.0
    last = max(subs, key=lambda x: x[1])
    if last[2] <= 2:
        subs = [x for x in subs if x is not last]
    if not subs:
        return 0.0, 0.0
    times = [t for t, _p in v]
    gaps = []
    for t, pos, _size in subs:
        k = min(bisect.bisect_left(times, t), len(v) - 1)
        gaps.append(abs(pos - v[k][1]) / 1e6)
    gaps.sort()
    return round(gaps[-1], 1), round(gaps[min(len(gaps) - 1, int(len(gaps) * 0.99))], 1)


def _packets(path: str, sel: str, ffprobe=FFPROBE, size=False) -> list:
    """[(pts, pos[, size])] of the stream(s) `sel` selects; [] when none or unreadable."""
    # KEYED output: ffprobe prints packet fields in ITS order (pts_time, size, pos), not the order
    # asked for, so positional csv read size as pos the moment size was added.
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-select_streams", sel, "-show_entries",
                              "packet=pts_time,pos,size", "-of", "compact=p=0", path],
                             capture_output=True, text=True, errors="replace", timeout=600).stdout
    except Exception:
        return []
    rows = []
    for ln in out.splitlines():
        kv = dict(f.split("=", 1) for f in ln.split("|") if "=" in f)
        try:
            row = (float(kv["pts_time"]), int(kv["pos"])) + ((int(kv["size"]),) if size else ())
        except (KeyError, ValueError):
            continue
        rows.append(row)
    return rows


def subtitle_gap_mb(path: str, ffprobe=FFPROBE) -> tuple:
    """(worst, p99) MB between each subtitle and its picture in the file at `path`."""
    subs = _packets(path, "s", ffprobe, size=True)
    if not subs:
        return 0.0, 0.0
    return subtitle_gaps(_packets(path, "v:0", ffprobe), subs)


def _gate_interleave(res, path: str, ffprobe=FFPROBE):
    """Refuse a master whose audio or subtitles sit too far from their own video. Verified on the
    file that will SHIP (`path`, the muxed .part), not on what we meant to write: an MP4Box mux
    once measured 115 MB by 20 minutes in (audio, -inter 500) and 1.2-2.9 GB (every mov_text
    sample written at the start of the file — the SHIELD stuttered). mkvmerge writes the blocks
    of every track in timestamp order inside its clusters, so a Matroska master should never fail
    this; it stays as the measurement that would catch a muxer that does. There is no fixer: a
    master that fails is refused (and deleted), and the stage retries/parks it."""
    gap = interleave_gap_mb(path, ffprobe)
    if gap > MAX_INTERLEAVE_GAP_MB:
        res.ok = False
        res.reason = (f"badly interleaved: audio sits {gap:.1f} MB from its own video "
                      f"(max {MAX_INTERLEAVE_GAP_MB:.0f}) — players stall on this")
        _rm(path)
        return res
    worst, p99 = subtitle_gap_mb(path, ffprobe)
    if worst >= 0 and (p99 > MAX_SUBTITLE_GAP_MB or worst > MAX_SUBTITLE_WORST_MB):
        res.ok = False
        res.reason = (f"badly interleaved: subtitles sit {worst:.0f} MB (p99 {p99:.1f}) from their "
                      f"picture — players stall on this")
        _rm(path)
    return res


def is_atmos_audio(profile: str = "") -> bool:
    """Atmos rides in ffprobe's PROFILE string — 'Dolby Digital Plus + Dolby Atmos' for
    E-AC-3 JOC, 'Dolby TrueHD + Dolby Atmos' for a TrueHD bed. There is no codec_name that
    says it, which is why this reads the profile and nothing else."""
    return "atmos" in (profile or "").lower()


def atmos_audio_index(path: str, ffprobe=FFPROBE):
    """Position of the first Atmos track AMONG THE AUDIO TRACKS (0-based), or None. That
    position is what -map 0:a:<i> takes, which is rarely the stream index: on Don't Look Up
    the Atmos track is stream 3 but audio track 2."""
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a",
                              "-show_entries", "stream=profile", "-of", "json", path],
                             capture_output=True, text=True, errors="replace", timeout=60).stdout
        streams = json.loads(out).get("streams") or []
    except Exception:
        return None
    for i, st in enumerate(streams):
        if is_atmos_audio(st.get("profile") or ""):
            return i
    return None


def audio_map_args(input_idx: int, n_audio: int, lead: int = 0) -> list:
    """-map args putting track `lead` FIRST and keeping the rest in their original order.
    Every track is named explicitly — mapping the lead and then `:a` as a group would carry
    the lead twice."""
    if n_audio <= 0:
        return ["-map", f"{input_idx}:a"]
    order = [lead] + [i for i in range(n_audio) if i != lead]
    args = []
    for i in order:
        args += ["-map", f"{input_idx}:a:{i}"]
    return args


def lead_track_disposition_args(n_audio: int) -> list:
    """First audio track is the default, every other one is not. Written per track rather
    than as a blanket clear plus an override — when two per-stream options match the same
    stream, which wins is ffmpeg trivia."""
    args = ["-disposition:a:0", "default"]
    for i in range(1, max(0, n_audio)):
        args += [f"-disposition:a:{i}", "0"]
    return args


def audio_track_count(path: str, ffprobe=FFPROBE) -> int:
    """How many audio tracks the file carries (0 when it can't be read)."""
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a",
                              "-show_entries", "stream=index", "-of", "csv=p=0", path],
                             capture_output=True, text=True, errors="replace", timeout=60).stdout
    except Exception:
        return 0
    return len([ln for ln in out.splitlines() if ln.strip()])


def boost_keeping_original_args(gain_db: float, n_audio: int, bitrate: str = "384k",
                                src=None) -> list:
    """Encoder args for LOSSLESS sources: a boosted lossy copy of the first track leads, and
    every original track rides along untouched behind it, marked non-default.

    The old rule refused lossless outright, so a quiet DTS-HD MA or TrueHD master simply
    stayed quiet and the Finished list could only say "never re-encoded" (user-dictated
    change, 2026-08-21: boost it, but never at the cost of the lossless track). Nothing is
    lost — the original bits are still in the file, one track over — and players that pick
    the default get the normalized one.

    Output audio is laid out [boosted, original 0, original 1, ...]; the caller's -map order
    has to match. `-c copy` covers the originals, so only track 0 names an encoder."""
    args = ["-c:a:0", "aac_at", "-b:a:0", bitrate,
            "-filter:a:0", build_audio_boost_filter(gain_db, src=src),
            "-metadata:s:a:0", "title=Normalized",
            "-disposition:a:0", "default"]
    # Explicit per-track dispositions rather than a blanket "-disposition:a 0" followed by an
    # override: when two per-stream options both match a stream, which one wins is ffmpeg
    # trivia, and a master whose lossless track claims `default` is the failure this exists
    # to avoid.
    for i in range(1, n_audio + 1):
        args += [f"-disposition:a:{i}", "0"]
    return args


# ---- THE MKVMERGE MUX ----------------------------------------------------------------------
# mkvmerge muxes every master (and the native render): it reads a raw HEVC ES, writes the Dolby
# Vision configuration record ITSELF from the RPU NALs + the SPS VUI (profile 8, compat 1 for a
# BT.2020/PQ base — verified in _verify), carries every NAL (RPU, AUD, HDR10 SEI) inside its
# picture's block, and orders the blocks of all tracks by timestamp inside its clusters.
MKVMERGE_OK = (0, 1)    # 1 = finished with WARNINGS (normal on many retail streams), 2 = error


def mkv_colour_args(sdr: bool = False, master_display=None, max_cll=None, tid: int = 0) -> list:
    """PURE. mkvmerge flags for track `tid`'s Matroska Colour element. mkvmerge writes none from
    a raw ES (tested: no Colour, MasteringMetadata or MaxCLL from the VUI or the SEI), and MP4Box
    wrote the mdcv/clli boxes, so an HDR10-fallback player that reads container metadata keeps it.
    DV/HDR10: BT.2020 primaries + non-constant matrix (9), PQ (16), limited range (1), plus the
    mastering display (`master_display`, dvcap's x265 string) and light level (`max_cll`, 'a,b')
    when known — the SAME values the in-band SEI carries; nothing is invented when they are
    absent. SDR: BT.709 (1/1/1), limited range."""
    t = int(tid)
    if sdr:
        return ["--colour-matrix-coefficients", f"{t}:1", "--colour-range", f"{t}:1",
                "--colour-transfer-characteristics", f"{t}:1", "--colour-primaries", f"{t}:1"]
    args = ["--colour-matrix-coefficients", f"{t}:9", "--colour-range", f"{t}:1",
            "--colour-transfer-characteristics", f"{t}:16", "--colour-primaries", f"{t}:9"]
    md = parse_x265_master_display(master_display)
    if md:
        (gx, gy), (bx, by), (rx, ry), (wx, wy), (lmax, lmin) = md
        args += ["--chromaticity-coordinates", f"{t}:{rx:g},{ry:g},{gx:g},{gy:g},{bx:g},{by:g}",
                 "--white-colour-coordinates", f"{t}:{wx:g},{wy:g}",
                 "--max-luminance", f"{t}:{lmax:g}", "--min-luminance", f"{t}:{lmin:g}"]
    try:
        cll, fall = (int(v) for v in str(max_cll).split(","))
        if cll > 0:
            args += ["--max-content-light", f"{t}:{cll}", "--max-frame-light", f"{t}:{fall}"]
    except (TypeError, ValueError):
        pass
    return args


def parse_x265_master_display(md):
    """PURE. x265's 'G(gx,gy)B(bx,by)R(rx,ry)WP(wx,wy)L(max,min)' (chromaticity in 0.00002,
    luminance in 0.0001 cd/m², dvcap.mastering_to_x265's units) -> ((gx,gy),(bx,by),(rx,ry),
    (wx,wy),(max,min)) as floats, or None when absent or malformed."""
    m = re.fullmatch(r"G\((\d+),(\d+)\)B\((\d+),(\d+)\)R\((\d+),(\d+)\)WP\((\d+),(\d+)\)"
                     r"L\((\d+),(\d+)\)", str(md or "").strip())
    if not m:
        return None
    v = [int(x) for x in m.groups()]
    c = [x / 50000 for x in v[:8]]
    return (c[0], c[1]), (c[2], c[3]), (c[4], c[5]), (c[6], c[7]), (v[8] / 10000, v[9] / 10000)


def build_master_mux_command(mkvmerge: str, es: str, fps: str, output: str, tracks=None,
                             colour=(), seed=None, sync_ms: int = 0) -> list:
    """mkvmerge: the raw HEVC `es` (+ the TRACKS file, build_tracks_command) -> `output`.
    `--default-duration 0:<fps>p` is REQUIRED: a raw ES carries no container timing (a
    VideoToolbox ES carries none at all — mkvmerge then assumes 25 fps), and mkvmerge also derives
    the Dolby Vision LEVEL from it. `fps` must be the exact rational ('24000/1001'), the rate the
    encode was planned at (dvcap.snap_fps). There is no DV flag to pass (MP4Box's `:dvp=8.1`):
    mkvmerge builds the record from the stream, and _verify demands 8.1 of the result.
    `--title ""`: no release name in the master's title. `seed` -> `--deterministic`: the same
    inputs mux to the same bytes, so a resumed upload can never splice two different muxes (passed
    as a NUMBER — mkvmerge reads any argument starting with '@' as an option file, a name too).
    `sync_ms` (tracks_sync_ms): the shift ffmpeg applied to the tracks file, undone — positive
    delays the video (`--sync 0:` on the ES), negative delays the tracks file (`--sync -1:` on it).
    Every path is absolute: mkvmerge reads an argument starting with '+', '@', '=' or '(' as
    syntax, never as a file name."""
    import zlib
    s = int(sync_ms or 0)
    return [mkvmerge, "-q",
            *(["--deterministic", str(zlib.crc32(str(seed).encode()))] if seed else []),
            "-o", os.path.abspath(output), "--title", "",
            "--default-duration", f"0:{fps}p", *colour,
            *(["--sync", f"0:{s}"] if s > 0 else []), os.path.abspath(es),
            *((["--sync", f"-1:{-s}"] if s < 0 else []) + [os.path.abspath(tracks)] if tracks else [])]


def run_mux(cmd: list, timeout=None) -> tuple:
    """(ok, detail) for an mkvmerge run; a warning-only run (rc 1) is a success."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except Exception as e:  # noqa: BLE001 — a missing binary / timeout is a failed mux
        return False, str(e)
    if r.returncode in MKVMERGE_OK:
        return True, _tail(r.stdout) if r.returncode == 1 else ""
    return False, _tail(r.stdout or r.stderr)


def part_path(output: str) -> str:
    """Where a master is muxed before it has passed every gate (atomic_ship)."""
    return output + ".part"


def atomic_ship(part: str, output: str) -> None:
    """Put a master that passed EVERY gate (verify, interleave, peak) at its real name."""
    os.replace(part, output)


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
    counts = {"video": 0, "audio": 0, "subtitle": 0, "dovi_profile": None, "dovi": None}
    for s in streams:
        t = s.get("codec_type")
        if t in counts:
            counts[t] += 1
        if t == "video" and counts["dovi"] is None and has_dolby_vision(s):
            counts["dovi_profile"] = dolby_vision_profile(s)
            sd = next(d for d in s.get("side_data_list", []) if d.get("side_data_type") == _DOVI_RECORD)
            counts["dovi"] = {k: sd.get(k) for k in ("dv_profile", "dv_level", "rpu_present_flag",
                                                     "el_present_flag", "bl_present_flag",
                                                     "dv_bl_signal_compatibility_id")}
    return counts


def verify_remux(probe_json: str, min_audio: int = 1, require_dv: bool = True):
    """A DV master must carry EXACTLY Dolby Vision 8.1: profile 8, BL compatibility 1 (the HDR10
    base every non-DV player falls back to), the RPU and the base layer present and NO enhancement
    layer. MP4Box's `:dvp=8.1` used to FORCE that record whatever the stream held; mkvmerge DERIVES
    it from the RPU and the HEVC VUI (a base without BT.2020/PQ VUI would come out 8.0 or 8.2), so
    the result is checked rather than trusted. `require_dv=False` — an item PINNED to SDR output —
    asks the opposite: no DOVI record at all (an SDR picture must never claim Dolby Vision).
    Audio presence applies to both; the frame count and the peak are checked at the call site."""
    s = parse_streams(probe_json)
    dv = s["dovi"]
    if require_dv:
        if not dv:
            return False, "no Dolby Vision RPU in output (DOVI configuration record missing)"
        if s["dovi_profile"] != "8.1" or dv.get("el_present_flag") or not dv.get("rpu_present_flag") \
                or not dv.get("bl_present_flag"):
            return False, (f"Dolby Vision is not 8.1 single-layer: profile {dv.get('dv_profile')} "
                           f"compat {dv.get('dv_bl_signal_compatibility_id')} rpu "
                           f"{dv.get('rpu_present_flag')} bl {dv.get('bl_present_flag')} el "
                           f"{dv.get('el_present_flag')}")
    elif dv:
        return False, f"an SDR master claims Dolby Vision {s['dovi_profile']}"
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
                       capture_output=True, text=True, errors="replace")
    return r.stdout if r.returncode == 0 else "{}"


def _tail(text: str, n: int = 12) -> str:
    return "\n".join((text or "").splitlines()[-n:])


def _verify(path: str, ffprobe: str, require_dv: bool = True, frames=None,
            output=None) -> RemuxResult:
    """Verify the muxed file at `path` (the .part); the result names `output` (the master's
    real name). `frames`: the exact frame count the master must hold — the mux must neither drop
    nor invent a picture (mkvmerge parses access units itself; a split one would add a frame)."""
    probe_json = _probe(path, ffprobe)
    s = parse_streams(probe_json)
    ok, reason = verify_remux(probe_json, require_dv=require_dv)
    res = RemuxResult(ok, output or path, s["dovi_profile"], s["audio"], s["subtitle"], reason)
    if ok and frames:
        got = dvcap.count_hevc_frames(path, ffprobe)
        if got != int(frames):
            res.ok = False
            res.reason = f"frame count changed in the mux: {got} != {frames} (shipped nothing)"
    return res


def _copy_native_rpu(src: str, rpu_out: str):
    """Native DV: the generated RPU -> segdir/rpu.bin, in place of dvcap.extract_rpu. ATOMIC
    like extract_rpu (.part, then os.replace): a kill mid-copy must never leave a truncated
    rpu.bin, which the segmented resume would reuse verbatim. A COPY, never a move or link:
    ensure_segdir wipes segdir on any manifest mismatch, and the canonical RPU (the one the
    native marker hashes) lives outside it and must survive that. Returns (ok, reason)."""
    tmp = rpu_out + ".part"
    _rm(tmp)
    try:
        shutil.copyfile(src, tmp)
        size = os.path.getsize(tmp)
        if size <= 0:
            raise OSError("the generated RPU is empty")
        os.replace(tmp, rpu_out)          # atomic publish
    except OSError as e:
        _rm(tmp)
        return False, f"native RPU copy failed: {e}"
    return True, "native rpu copied (%d bytes)" % size


def _prepare_tracks(cfr_source: str, orig_source: str, tracks: str, *, audio_target_lufs=None,
                    audio_gain_db=None, ffmpeg=FFMPEG, ffprobe=FFPROBE, timeout=None) -> tuple:
    """Build the master's TRACKS file once -> (ok, audio_note, reason, sync_ms). The SAME rules on every
    route (the capped encode, the RPU inject, the shipped render, the companion combine) — they
    used to differ by container, and the routes that took the MKV branch shipped without the
    boost, the Atmos lead or the no-subtitles retry:
      ATMOS: nothing re-encoded, the Atmos track leads (user-dictated 2026-08-22).
      BOOST: to `audio_target_lufs` (or the season's `audio_gain_db`, stages/audiogain) — LOSSLESS
        sources keep every original track behind the boosted copy (2026-08-21). The landing is
        measured on this cheap file BEFORE the mux; a boost that misses (or that the encoder
        refuses — aac_at and an exotic 7.1 bed) falls back to a bit-exact copy, never a park.
      SUBTITLES: copied / converted to SRT (subtitle_plan). A build that fails retries with
        -fix_sub_duration when it converts any (corrupt cue durations; that costs each converted
        track its last line, so it is never the default), and only then drops the subtitles
        rather than parking the item over them (the last-resort retry, said in the note).
      SYNC: sync_ms for the mux (tracks_sync_ms) — Matroska has no edit list, so an MP4 CFR's
        AAC priming (its audio starting at -21 ms) comes out of ffmpeg shifted to 0, and every
        track of the tracks file with it; the mux undoes exactly the shift measured."""
    atmos_lead = atmos_audio_index(cfr_source, ffprobe)
    n_audio = audio_track_count(cfr_source, ffprobe) if atmos_lead is not None else 0
    note, measured, gain, keep = "", None, 0.0, 0
    if atmos_lead is not None:
        note = " · audio untouched (Atmos, now the main track)"
    elif audio_target_lufs:
        measured = measure_lufs(cfr_source, ffmpeg)
        gain = (round(float(audio_gain_db), 2) if audio_gain_db is not None
                else boost_gain_db(measured, audio_target_lufs))
        if gain > 0 and has_lossless_audio(cfr_source, ffprobe):
            keep = audio_track_count(cfr_source, ffprobe)
    plan, subs_note = subtitle_plan(orig_source, ffprobe), ""

    def build(g, subs, fix=False):
        cmd = build_tracks_command(ffmpeg, cfr_source, orig_source, tracks, gain_db=g, subs=subs,
                                   keep_original_audio=(keep if g > 0 else 0),
                                   atmos_lead=atmos_lead, n_audio=n_audio, fix_sub_duration=fix)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as e:
            return False, f"timed out: {e}"
        return r.returncode == 0, _tail(r.stderr)

    fix = False
    shipped = 0.0
    for attempt in ([gain, 0.0] if gain > 0 else [0.0]):
        shipped = attempt
        ok, err = build(attempt, plan, fix)
        if not ok and plan and not fix and any(a == "srt" for _n, a in plan):
            ok, _e = build(attempt, plan, True)
            if ok:
                fix, subs_note = True, " · subtitle durations repaired"
        if not ok and plan:
            ok, err2 = build(attempt, [])
            if ok:
                plan, subs_note = [], " · subs dropped (unconvertible track)"
            else:
                err = err2 or err
        if not ok:
            if attempt > 0:
                note = " · audio unboosted (encoder refused the track)"
                continue
            return False, "", "tracks failed: " + err, 0
        if attempt <= 0:
            break
        landed = measure_lufs(tracks, ffmpeg)
        if landing_ok(landed, float(audio_target_lufs), attempt, measured=measured):
            note = (f" · audio +{attempt:.1f}dB → {landed:.1f} LUFS"
                    + (" · original lossless kept" if keep else ""))
            break
        # SAY THE NUMBERS. "landing off target" with nothing to check it against is
        # unfalsifiable from the log — the one thing you want when a file ships quiet is what
        # it measured and where it went.
        note = _unboosted_note(measured, landed, audio_target_lufs)
    # a STREAM-COPIED audio track of the tracks file and the CFR track it was copied from: track 0
    # on a copy (the Atmos lead's own CFR index), track 1 when a boosted copy leads the originals.
    # A boost without kept originals copies no audio — re-encodes carry their own delay.
    if shipped <= 0:
        pair = (0, atmos_lead if atmos_lead is not None else 0)
    elif keep:
        pair = (1, 0)
    else:
        pair = None
    sync = tracks_sync_ms(cfr_source, tracks, pair, ffprobe) if pair else 0
    if sync:
        note += f" · audio start realigned ({sync:+d} ms)"
    return True, note + subs_note, "", sync


def _first_pts(path: str, sel: str, ffprobe: str = FFPROBE):
    """The first packet's pts (seconds) of stream `sel` in `path`, or None."""
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-select_streams", sel, "-read_intervals",
                              "%+#1", "-show_entries", "packet=pts_time", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=120).stdout
        tok = (out.splitlines() or [""])[0].split(",")[0].strip()
        return float(tok)
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


def tracks_sync_ms(cfr_source: str, tracks: str, pair, ffprobe: str = FFPROBE) -> int:
    """How far ffmpeg moved the tracks file against the picture, in ms (0 when it did not, or it
    cannot be measured). `pair` = (audio index in the tracks file, audio index in the CFR) of a
    stream-COPIED track. In the CFR the picture starts at V and that track at A (an MP4's AAC
    priming: A = -0.021, skipped by its edit list); in the tracks file the track starts at T. The
    master's ES starts at 0, so keeping A - V means delaying the picture by T - A + V."""
    t = _first_pts(tracks, f"a:{pair[0]}", ffprobe)
    a = _first_pts(cfr_source, f"a:{pair[1]}", ffprobe)
    v = _first_pts(cfr_source, "v:0", ffprobe)
    if t is None or a is None:
        return 0
    ms = round((t - a + (v or 0.0)) * 1000)
    return ms if abs(ms) >= 1 else 0


def _mux_verified(es: str, fps: str, tracks, output: str, *, require_dv: bool = True,
                  frames=None, colour=(), mkvmerge=MKVMERGE, ffprobe=FFPROBE, timeout=None,
                  on_step=None, step_size=0, sync_ms: int = 0) -> RemuxResult:
    """mkvmerge `es` + `tracks` into part_path(output), then verify it (DV 8.1 / no DV for SDR,
    audio, the exact frame count) and gate its interleaving. ok -> the .part passed and is
    waiting for the caller's last gate (the peak) and atomic_ship; not ok -> the .part is gone."""
    part = part_path(output)
    _rm(part)
    cmd = build_master_mux_command(mkvmerge, es, fps, part, tracks=tracks, colour=colour,
                                   seed=os.path.basename(output), sync_ms=sync_ms)
    with _StepWatch(on_step, "muxing the master", part, step_size):
        ok, detail = run_mux(cmd, timeout=timeout)
    if not ok or not os.path.exists(part):
        _rm(part)
        return RemuxResult(False, output, reason="mkv mux failed: " + detail)
    if on_step:
        on_step("verifying the master", None)
    res = _verify(part, ffprobe, require_dv=require_dv, frames=frames, output=output)
    if not res.ok:
        _rm(part)
        return res
    return _gate_interleave(res, part, ffprobe)


def remux(dv_video: str, cfr_source: str, orig_source: str, output: str, *,
          cap_mbps: int = dvcap.DEFAULT_PEAK_MBPS, audio_target_lufs=None,
          audio_gain_db=None, boundaries=None,
          abort=None, on_progress=None, on_plan=None, should_pause=None, on_repair=None,
          encode_source=None, rpu_mode=None, native=None, sdr=False,
          ffmpeg=FFMPEG, mkvmerge=MKVMERGE, ffprobe=FFPROBE, timeout=None) -> RemuxResult:
    """Peak-cap the DV render's video (dvcap: RPU extract -> x265 native-DV VBV re-encode), then
    put the original audio + subtitles back onto it. HARD GATE, no uncapped fallback: any
    failure (RPU, encode, frame mismatch, DV lost, measured peak over cap) fails the stage.
    Audio comes from the CFR file (same timing as the pipeline video); subs from the ORIGINAL.
    `output` must be a .mkv (every master is Matroska); the mux goes to <output>.part and only a
    master that passed every gate is renamed onto `output`.
    `should_pause`: polled between x265 segments — yields a benign "paused:" result so the
    finisher can hold this remux while another stage has the whole machine (a sibling's lease,
    AI outpainting); every finished segment is kept and the retry resumes here.
    `encode_source`: the peak-gated rpu-only fallback (SHIELD DV ceiling) — the RPU still
    comes from `dv_video` (the render's analysis), but the video that gets the capped re-encode
    is THIS file (the original HDR10 stream; frame-aligned because the render was made from the
    same CFR copy) instead of the render, whose floor-bitrate video was never meant to ship.
    `native` (a chunk-lane item; None = the render path above, unchanged): the
    nativelane bundle {chunks, vf, rpu, fps, max_cll, total, bounds, id}. There is NO render:
    every segment decodes its own Topaz ProRes chunk through the LUT (`decode`, at the main
    encode, the repair rung AND the re-concat — a missing one would encode the wrong file),
    the RPU is dvnative's generated one (copied, not extracted), frames/fps/max_cll come from
    the bundle (nothing to probe), the segment plan is the chunk ends (`bounds`, overriding
    `boundaries`), and the mastering display is dvcap's 1000-nit P3 fallback — what the
    generated RPU's L6 declares. `dv_video` is unused: chunk 0 stands in as the nominal
    source, which the decode chunks override segment by segment.
    `sdr` (2026-10-18, an item PINNED to SDR output, rendered natively: stages._remux passes it
    only for a render nativerender.sdr_render_ok vouches for) makes the TRUE SDR master from
    `dv_video` (an SDR render): no RPU anywhere — nothing extracted, the frame count is the
    render's own packets — the Rec.709 x265 under the same VBV cap (dvcap dv=False), no DV box
    in the mux, and the verify asks for no DOVI record. The peak gate and its repair ladder, the
    audio and the resume rules are the DV path's; the resume manifest says "sdr" so the two
    never share segments. Without it every call below is exactly the DV path's."""
    if not is_master_container(output):
        return _not_mkv(output)
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    nat_kw, dec_kw = {}, {}   # render path: NO extra kwargs — every dvcap call is the plain call
    sdr_kw = {}               # ...and none for SDR unless asked: the DV calls stay byte-identical
    if sdr:
        if native is not None or encode_source is not None or rpu_mode is not None:
            return RemuxResult(False, output,
                               reason="an SDR master has no RPU: no native bundle, encode source "
                                      "or RPU mode applies (shipped nothing)")
        nat_kw, sdr_kw = {"sdr": True}, {"dv": False}
    if native is not None:
        # GUARD BEFORE touching the resume state, like the probe guard below: a bundle whose
        # chunk list does not end at its total would plan (and wipe/resume) a different episode.
        chunks = native.get("chunks") or []
        total = int(native.get("total") or 0)
        if (total <= 0 or len(chunks) < 2 or chunks[-1][0] != total or not chunks[0][1]
                or not native.get("id")):
            return RemuxResult(False, output,
                               reason=f"native bundle is malformed (total {total}, "
                                      f"{max(0, len(chunks) - 1)} chunk(s)) — resume state left intact")
        if encode_source is not None or rpu_mode is not None:
            return RemuxResult(False, output,
                               reason="native DV has no render to swap the encode source or "
                                      "RPU mode of (shipped nothing)")
        enc = chunks[0][1]                # NOMINAL only: decode["chunks"] picks each segment's chunk
        info = {"frames": total, "fps": str(native["fps"]),
                "master_display": None,   # -> dvcap.FALLBACK_MASTER_DISPLAY (1000-nit P3 D65)
                "max_cll": native.get("max_cll")}
        boundaries = [int(b) for b in native["bounds"]]
        nat_kw = {"native_id": native["id"]}
        dec_kw = {"decode": {"chunks": chunks, "vf": native["vf"]}}
    else:
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
        encode_source=encode_source, rpu_mode=rpu_mode, **nat_kw))
    rpu = os.path.join(segdir, "rpu.bin")
    hevc = output + ".capped.hevc"          # transient: the concat of the segments, rebuilt each attempt
    tracks = None
    try:
        if sdr:
            pass                              # no RPU at all: the frame count below is the render's
        elif not (os.path.exists(rpu) and os.path.getsize(rpu) > 0):   # resume keeps the extracted RPU
            if native is not None:
                ok, why = _copy_native_rpu(native["rpu"], rpu)
            else:
                ok, why = dvcap.extract_rpu(dv_video, rpu, mode=rpu_mode, ffmpeg=ffmpeg,
                                            timeout=timeout)
            if not ok:
                return RemuxResult(False, output, reason=why)
        # RPU frame count is GROUND TRUTH (one per coded frame). The container nb_frames HEADER
        # can over-report a VideoToolbox .mov's decodable tail; planning/slicing the RPU against
        # the header instead → out-of-range dovi_tool remove → permanent park (review-caught).
        real_frames = dvcap.count_hevc_frames(enc, ffprobe) if sdr else dvcap.rpu_frame_count(rpu)
        if real_frames <= 0:
            return RemuxResult(False, output, reason=("could not count the SDR render's frames" if sdr
                                                      else "could not read RPU frame count")
                                                     + " — resume state intact")
        if native is not None and real_frames != info["frames"]:
            # FAIL FAST before the hours-long encode: the chunks ARE the video, so an RPU of any
            # other length is not their analysis. A shorter one would QUIETLY plan a shorter
            # episode (the last chunk's tail dropped, against the CFR's audio); a longer one
            # pads a tail no chunk holds. Drop the copy so a retry re-copies the canonical.
            _rm(rpu)
            return RemuxResult(False, output,
                               reason=f"native RPU/chunk frame mismatch: rpu {real_frames} != "
                                      f"chunks {info['frames']} (shipped nothing)")
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
            should_pause=should_pause, ffmpeg=ffmpeg, **dec_kw, **sdr_kw)
        if not ok:
            if why.startswith("paused:"):
                return RemuxResult(False, output, reason=why)   # benign hold, not a failure
            return RemuxResult(False, output, reason="cap encode: " + why)
        if frames != real_frames:
            return RemuxResult(False, output,
                               reason=f"frame count changed by cap encode: {frames} != {real_frames}")
        # The TRACKS file (audio from the CFR + the original's subtitles), built ONCE — it is
        # video-independent, so every peak-repair rung below muxes against the same file.
        # SMART LOUDNESS BOOST, Atmos, lossless and the subtitle retry: _prepare_tracks.
        # `audio_gain_db` = a gain already decided for this item (TV: the SEASON's, set by its
        # first episode — see audiogain); measured here only when nobody decided for us.
        tracks = output + ".tracks.mka"   # temp, next to output (on scratch)
        ok, audio_note, why, sync = _prepare_tracks(cfr_source, orig_source, tracks,
                                              audio_target_lufs=audio_target_lufs,
                                              audio_gain_db=audio_gain_db, ffmpeg=ffmpeg,
                                              ffprobe=ffprobe, timeout=timeout)
        if not ok:
            return RemuxResult(False, output, reason=why)
        # ---- mux + verify + PEAK GATE, with a tightening ladder on a peak miss ------------------
        # VBV bufsize == maxrate legally allows a 1-second burst past cap × tolerance, and an
        # identical retry reuses the identical segments — it can never pass (user-caught: a movie
        # parked at 58.6 > 50 five times, shipped nothing). On a miss, LOCALIZE the burst to its
        # segment(s) and re-encode only those at 85% then 70% of the cap, then re-gate.
        segs_plan = dvcap.plan_segments(real_frames, info["fps"], dvcap.SEG_SECONDS,
                                        boundaries=boundaries)
        # the container's HDR10 description = what the x265 SEI says (its fallback included)
        colour = mkv_colour_args(sdr, None if sdr else (info["master_display"]
                                                        or dvcap.FALLBACK_MASTER_DISPLAY),
                                 None if sdr else info["max_cll"])
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
                    abort=abort, on_repair=on_repair, ffmpeg=ffmpeg, **dec_kw, **sdr_kw)
                if not ok:
                    return RemuxResult(False, output, reason="peak repair: " + why)
                # every segment is complete now → this call just re-verifies counts and re-concats
                # (it still needs the native decode: a segment found missing here is re-encoded)
                ok, frames, why = dvcap.encode_capped_segmented(
                    enc, rpu, hevc, cap_mbps, segdir=segdir,
                    total_frames=real_frames, fps=info["fps"], boundaries=boundaries,
                    master_display=info["master_display"], max_cll=info["max_cll"],
                    abort=abort, ffmpeg=ffmpeg, **dec_kw, **sdr_kw)
                if not ok or frames != real_frames:
                    return RemuxResult(False, output, reason="peak repair concat: " + why)
                repair_note = f" · peak repair: {len(offenders)} seg(s) re-capped @ {tight} Mbps"
            res = _mux_verified(hevc, info["fps"], tracks, output, require_dv=not sdr,
                                frames=real_frames, colour=colour, mkvmerge=mkvmerge,
                                ffprobe=ffprobe, timeout=timeout, sync_ms=sync)
            if not res.ok:
                return res
            part = part_path(output)
            buckets = dvcap.video_peak_buckets(part, ffprobe)   # re-measure the file that SHIPS
            peak = max(buckets.values()) if buckets else 0.0
            if dvcap.peak_ok(peak, cap_mbps):
                res.reason += f" · peak {peak:.1f} ≤ {cap_mbps} Mbps cap" + repair_note + audio_note
                atomic_ship(part, output)
                shutil.rmtree(segdir, ignore_errors=True)  # SUCCESS: the segments won't be needed again
                return res
            _rm(part)                                      # never leave an over-peak master around
        return RemuxResult(False, output,
                           reason=f"peak still over cap after encode + repair: "
                                  f"{peak:.1f} Mbps > {cap_mbps} (shipped nothing)")
    finally:
        # transient only — segdir is KEPT on any non-success return so the next attempt resumes
        _rm(hevc); _rm(tracks); _rm(part_path(output))


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
                 ffmpeg=FFMPEG, mkvmerge=MKVMERGE, ffprobe=FFPROBE, timeout=None) -> RemuxResult:
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
    copied ES first, single-layer-ifying an inline-P7 winner (through nalfix first: a P7
    release's split access units would otherwise reach mkvmerge as picture-less frames)."""
    if not is_master_container(output):
        return _not_mkv(output)
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
    tracks = None
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
                                capture_output=True, text=True, errors="replace", timeout=timeout)
        if ex.returncode != 0 or not (os.path.exists(src_es) and os.path.getsize(src_es) > 0):
            return RemuxResult(False, output, reason="source ES extract failed: " + _tail(ex.stderr))
        # a DV 8.1 base must SAY BT.2020/PQ in its VUI: mkvmerge derives the record from it
        ok, vui_note = dvcap.ensure_pq_vui(src_es, ffmpeg, ffprobe, timeout=timeout)
        if not ok:
            return RemuxResult(False, output, reason=vui_note + " (shipped nothing)")
        if abort is not None and abort.is_set():
            return RemuxResult(False, output, reason="aborted")
        if skip_inject:
            if convert_es:
                # inline-P7 winner: hand split-off DV NALs back to their picture (nalfix — a
                # no-op on a normal stream), then drop the EL, rewrite the RPU to 8.1, in one pass
                import nalfix
                fixed = output + ".fix.hevc"
                try:
                    with open(src_es, "rb") as a, open(fixed, "wb") as b:
                        nalfix.run(a, b)
                    os.replace(fixed, src_es)
                except OSError as e:
                    _rm(fixed)
                    return RemuxResult(False, output, reason=f"nalfix failed: {e}")
                with _StepWatch(on_step, "converting DV to profile 8.1", inj_es, _fsize(src_es)):
                    cv = subprocess.run(dvcap.build_dovi_convert_command(dvcap.DOVI_TOOL,
                                                                         src_es, inj_es),
                                        capture_output=True, text=True, errors="replace", timeout=timeout)
                if cv.returncode != 0 or not (os.path.exists(inj_es) and os.path.getsize(inj_es) > 0):
                    return RemuxResult(False, output,
                                       reason="DV convert failed: " + _tail(cv.stderr or cv.stdout))
            else:
                inj_es = src_es              # the winner's own bits, RPU already inline
        else:
            with _StepWatch(on_step, "injecting DV metadata", inj_es, _fsize(src_es)):
                ij = subprocess.run(dvcap.build_inject_command(dvcap.DOVI_TOOL, src_es, rpu, inj_es),
                                    capture_output=True, text=True, errors="replace", timeout=timeout)
            if ij.returncode != 0 or not (os.path.exists(inj_es) and os.path.getsize(inj_es) > 0):
                return RemuxResult(False, output,
                                   reason="RPU inject failed: " + _tail(ij.stderr or ij.stdout))
        n_inj = dvcap.count_hevc_frames(inj_es, ffprobe)    # AUDs → 1 packet/frame
        if n_inj != n_src:
            return RemuxResult(False, output,
                               reason=f"processed ES frame count changed: {n_inj} != {n_src} "
                                      f"(shipped nothing)")
        # same audio machinery as every route (boost validated, falls back to a copy); the CFR
        # gate guarantees cfr audio is a bit-exact stream copy of the source's
        if on_step:
            on_step("preparing audio", None)
        tracks = output + ".tracks.mka"
        ok, audio_note, why, sync = _prepare_tracks(cfr_source, orig_source, tracks,
                                              audio_target_lufs=audio_target_lufs, ffmpeg=ffmpeg,
                                              ffprobe=ffprobe, timeout=timeout)
        if not ok:
            return RemuxResult(False, output, reason=why)
        # the container's HDR10 description = the shipped stream's own SEI
        res = _mux_verified(inj_es, info["fps"], tracks, output, frames=n_src,
                            colour=mkv_colour_args(False, src_info.get("master_display"),
                                                   src_info.get("max_cll")),
                            mkvmerge=mkvmerge, ffprobe=ffprobe, timeout=timeout,
                            on_step=on_step, step_size=_fsize(inj_es), sync_ms=sync)
        if not res.ok:
            return res
        # NO peak gate here: the caller gated the source's peaks before choosing this path.
        if skip_inject and convert_es:
            note = " · original stream, DV converted to 8.1 (no re-encode)"
        elif skip_inject:
            note = " · original DV stream shipped as-is (no re-encode)"
        else:
            note = " · original stream + injected RPU (no re-encode)"
        res.reason += note + audio_note + vui_note
        atomic_ship(part_path(output), output)
        if segdir:
            shutil.rmtree(segdir, ignore_errors=True)  # SUCCESS: the RPU won't be needed again
        return res
    finally:
        # the two big ESes are transient EVERY attempt (recreated in minutes); only the small
        # rpu.bin persists (in segdir) for resume — segdir is KEPT on any non-success return
        _rm(src_es); _rm(inj_es); _rm(tracks); _rm(part_path(output))


def combine(winner: str, rpu_source: str, audio_source: str, output: str, *,
            rpu_inline=False, rpu_profile="", capped=False,
            cap_mbps: int = dvcap.DEFAULT_PEAK_MBPS, boundaries=None,
            audio_target_lufs=None, abort=None, on_step=None, on_progress=None,
            on_plan=None, should_pause=None, on_repair=None,
            ffmpeg=FFMPEG, mkvmerge=MKVMERGE, ffprobe=FFPROBE, timeout=None) -> RemuxResult:
    """COMPANION COMBINE (user-dictated): one best-of master from two copies of a film.
    `winner` = the video that ships (the genuinely better HDR10 base, or the real-DV file
    itself); `rpu_source` = where the Dolby Vision metadata comes from (the real-DV copy,
    or the native render when neither copy has an RPU); `audio_source` = the file whose
    ENTIRE audio ships (best track wins the whole file — its compat AC-3 rides along).
    Subtitles ship from the winner. Output is always MKV (TrueHD-class audio).

    `capped=False` → stream path: the winner's own bits ship (RPU grafted/converted as
    needed) — the caller peak-gates first. `capped=True` → the enforced-VBV x265
    native-DV re-encode of the winner with the SAME RPU (real DV survives a re-encode —
    prioritized over generated DV either way, user-dictated).

    A thin dispatcher: both paths are the proven remux_inject / remux machinery — the
    frame-count and fps hard gates in there are exactly the different-cut gates a
    cross-release graft needs (a mismatch ships NOTHING; the stage parks it).

    AUDIO SYNC: audio is stream-copied, never retimed — alignment is guaranteed by
    proving the donor is the SAME CUT as the shipped video. Two donor configurations
    carry that proof already (audio from the winner itself is trivial; audio from the
    real-RPU donor is transitively proven by the RPU-vs-winner frame gate). The
    remaining one — audio from the other copy while the RPU comes from elsewhere
    (the no-RPU render, or an inline-DV winner borrowing audio) — is gated HERE: the
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
                     ffmpeg=ffmpeg, mkvmerge=mkvmerge, ffprobe=ffprobe, timeout=timeout)
    return remux_inject(rpu_source, audio_source, winner, output,
                        audio_target_lufs=audio_target_lufs, abort=abort, on_step=on_step,
                        rpu_mode=mode, skip_inject=rpu_inline,
                        convert_es=(rpu_inline and p7),
                        ffmpeg=ffmpeg, mkvmerge=mkvmerge, ffprobe=ffprobe, timeout=timeout)


def remux_ship_render(dv_video: str, cfr_source: str, orig_source: str, output: str, *,
                      cap_mbps: int = dvcap.DEFAULT_PEAK_MBPS, audio_target_lufs=None,
                      abort=None, on_step=None, ffmpeg=FFMPEG, mkvmerge=MKVMERGE, ffprobe=FFPROBE,
                      timeout=None, sdr: bool = False) -> RemuxResult:
    """YOUTUBE FAST REMUX (user-asked 2026-08-06: videos should move through fast). The
    native render IS already a DV 8.1 HEVC file — the hour-class x265 pass exists only
    to force video under the SHIELD peak cap, and a render encoded at the YouTube target
    bitrate (stages caps it well under the cap) normally measures safe already. So: GATE
    the render's measured 1-s peak; under → ship its video STREAM-COPIED (ES copy + mux +
    audio — minutes), over → reason "render-over-cap: ..." and the caller falls back to
    the normal capped re-encode, ladder and all. Same mux/audio/verify tail as
    remux_inject; the render↔CFR frame alignment was already gated when the DV step
    accepted the render (render_is_complete). No resume dir — a kill just redoes minutes.
    `sdr` (2026-10-18): a native SDR render (an item pinned to SDR output) — shipped the same
    way, with no DV box in the mux and a verify that asks for no DOVI record."""
    if not is_master_container(output):
        return _not_mkv(output)
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
    tracks = None
    try:
        if abort is not None and abort.is_set():
            return RemuxResult(False, output, reason="aborted")
        with _StepWatch(on_step, "copying the render's video", es, _fsize(dv_video)):
            ex = subprocess.run(dvcap.build_annexb_file_command(ffmpeg, dv_video, es),
                                capture_output=True, text=True, errors="replace", timeout=timeout)
        if ex.returncode != 0 or not (os.path.exists(es) and os.path.getsize(es) > 0):
            return RemuxResult(False, output, reason="render ES extract failed: " + _tail(ex.stderr))
        if not sdr:                       # a DV base must say BT.2020/PQ (see remux_inject)
            ok, _vui = dvcap.ensure_pq_vui(es, ffmpeg, ffprobe, timeout=timeout)
            if not ok:
                return RemuxResult(False, output, reason=_vui + " (shipped nothing)")
        if on_step:
            on_step("preparing audio", None)
        tracks = output + ".tracks.mka"
        ok, audio_note, why, sync = _prepare_tracks(cfr_source, orig_source, tracks,
                                              audio_target_lufs=audio_target_lufs, ffmpeg=ffmpeg,
                                              ffprobe=ffprobe, timeout=timeout)
        if not ok:
            return RemuxResult(False, output, reason=why)
        # the container's HDR10 description = the render's own SEI (nativerender wrote it)
        res = _mux_verified(es, info["fps"], tracks, output, require_dv=not sdr,
                            frames=info["frames"],
                            colour=mkv_colour_args(sdr, info.get("master_display"),
                                                   info.get("max_cll")),
                            mkvmerge=mkvmerge, ffprobe=ffprobe, timeout=timeout,
                            on_step=on_step, step_size=_fsize(es), sync_ms=sync)
        if not res.ok:
            return res
        part = part_path(output)
        shipped = dvcap.video_peak_1s_mbps(part, ffprobe)   # belt: same bits, re-measured
        if not dvcap.peak_ok(shipped, cap_mbps):
            _rm(part)
            return RemuxResult(False, output,
                               reason=f"render-over-cap: shipped file measured {shipped:.1f} Mbps "
                                      f"> {cap_mbps} — using the capped re-encode")
        res.reason += (f" · render shipped as-is (peak {mbps:.1f} ≤ {cap_mbps} Mbps, "
                       f"no re-encode)") + audio_note
        atomic_ship(part, output)
        return res
    finally:
        _rm(es); _rm(tracks); _rm(part_path(output))


def main(argv=None):
    import argparse, sys
    ap = argparse.ArgumentParser(description="Remux original audio+subs onto the mute DV video.")
    ap.add_argument("dv_video", help="mute Dolby Vision video (the native render)")
    ap.add_argument("cfr_source", help="the CFR file (audio donor)")
    ap.add_argument("orig_source", help="the original download (subtitle donor)")
    ap.add_argument("output", help="the master (.mkv)")
    args = ap.parse_args(argv)
    res = remux(args.dv_video, args.cfr_source, args.orig_source, args.output)
    print(("OK: " if res.ok else "FAILED: ") + res.reason)
    return 0 if res.ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
