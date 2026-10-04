"""Dolby Vision profile 7 -> profile 8.1, with NO video re-encode.

A DV profile 7 movie has already proven it can carry a Dolby Vision layer — it is one. So the
HDR10 base layer is copied bit for bit and only the Dolby Vision metadata is rewritten: dovi_tool
mode 2 turns each RPU into profile 8.1 and discards the enhancement layer (dropping a full
enhancement layer is user-approved), and mkvmerge puts the new video back beside every other track
of the original. No Topaz, no Resolve, no x265, and no peak cap: nothing about the picture changes,
so there is nothing for a cap to protect (user-dictated 2026-09-27/29).

The recipe and every verification are ported one for one from the user's reference driver
(~/dv-p7-to-p81/convert_batch.py), which converted Project Hail Mary and The Social Network — plus
what the quirks below added: nalfix, the mkvextract fallback, the original's frame times when they
are uneven, and the refusal of a cut-short original:
  single track:  ffmpeg -map BL -c copy -bsf hevc_mp4toannexb | nalfix | dovi_tool -m 2 convert
                 --discard   (nalfix is Visionary's own addition, below)
  dual track:    extract the 4K base layer and the 1080p EL+RPU track; dovi_tool -m 2 extract-rpu
                 from the EL; dovi_tool inject-rpu into the base layer (both old tracks dropped)
  either way:    when ffmpeg reports a packet it dropped (it still exits 0), the layers are pulled
                 again with mkvextract, which copies it (a single track's then goes nalfix <
                 bl.hevc | dovi_tool)
  mkvmerge:      the new video, then every other track, chapter and attachment of the original, with
                 the video track's language, name, default/forced flags and default duration copied,
                 its start carried over with --sync (or every frame time, with --timestamps),
                 and "P7" in the title renamed to "P8". mkvmerge writes the DV configuration itself.
Verified on the new file: DV side data profile 8 / compatibility 1 / no EL, an RPU that reads
profile 8, the same track count (one fewer for dual track), chapters and attachments, duration
within 100 ms, the same video frame count, and the same first timestamp for the video and the first
audio track. Anything else raises, and the caller keeps the original.

Release quirks the build and the checks allow for, proven on the NAS originals (2026-10-01/03):
  - Some remuxes put the LAST frame's enhancement layer and RPU in a block of their own after it
    (Joker, Last Night in Soho, Mamma Mia! Here We Go Again, Rogue Nation: EL picture + RPU + EL
    end-of-stream, no base-layer picture, a duplicate timestamp). Discarding the EL leaves no
    picture there, so the new file rightly has one frame fewer than the original has blocks.
    Some put such blocks in the MIDDLE too (Gladiator, 2026-10-03: 21 of them, each a second
    block on a timestamp a picture block already has, holding ten EL NALs and an RPU). When the
    count comes up short, every block that repeats a timestamp is read and only the ones holding
    no base-layer picture are allowed for (dv_only_blocks) — a lost picture still fails.
  - Some start the video track a millisecond or more after the audio (Mission: Impossible, 1 ms).
    A raw HEVC stream carries no timestamps, so the new track would start at 0 and shift against
    the audio: the original's start — its first PRESENTED frame, not its first block, which can be
    a keyframe shown after leading pictures — is carried over with mkvmerge --sync.
  - Some carry a malformed NAL: Risky Business (2026-10-03) has one block whose end-of-sequence
    NAL has a 1-byte length prefix. ffmpeg's hevc_mp4toannexb refuses the whole block (a picture
    and its RPU), prints an error and still exits 0. That error sends the layers through
    mkvextract instead, which copies the block, so every frame survives (decoded frames proven
    identical to the original's around it) and the frame check still applies in full.
  - Some split the last picture of every group of pictures in two (Star Trek: Nemesis, 2026-10-04:
    9325 times): the picture's block holds only base-layer slices, and its EL and RPU sit in the
    next block (on or within a millisecond of a picture's timestamp), opened by a copy of the
    next keyframe's parameter sets — which made each one a
    picture-less frame of its own in the rebuilt stream. nalfix hands those NALs back to their
    picture, and the frame check takes nalfix's count of the original's pictures.
  - Some have uneven frame times (The Mandalorian and Grogu, 2026-10-04: three gaps, a frame in
    all). The original's own times are then carried over with a mkvmerge timestamp file instead
    of a default duration, which would put every frame after a gap early against the audio.
  - Some are cut short (The Boy and the Heron, 2026-10-04: the video stops at 1:55:13 of its
    header's 2:03:56). Those are refused before the conversion starts, saying where they stop:
    they need a complete copy.
Because only the start is carried over (unless the times are uneven), the video's LAST timestamp is
checked as well: a jump in the
original's timestamps mid-film would otherwise come out as a drift the other checks cannot see.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"
DOVI = "/opt/homebrew/bin/dovi_tool"
MKVMERGE = "/opt/homebrew/bin/mkvmerge"
MKVEXTRACT = "/opt/homebrew/bin/mkvextract"
NALFIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nalfix.py")
ENDED_EARLY = "File ended prematurely"          # ffprobe, on a file cut short of its own header
TRUNCATED_SLACK_SECS = 1.0      # a video track may stop this far short of the header's duration
BSF_DROPPED = "Error applying bitstream filters"   # ffmpeg dropped a packet it could not convert,
                                                    # and still exits 0
DURATION_SLACK_NS = 100e6       # the new file's duration may differ by at most 100 ms
DV_NALS = {62, 63}              # Dolby Vision NAL types: the RPU, and an enhancement-layer NAL
TAIL_SECS = 1.0                 # how much of a stream's end is read for picture-less blocks
END_SLACK_MS = 5                # the new video's last timestamp may differ by at most this (ms
                                # rounding on both sides: Planes, Trains & Automobiles is 1 ms off)
NICE = 10                       # below the pipeline's own x265 remux, which shares these cores


class AlreadyP8(Exception):
    """The file is already Dolby Vision profile 8 — nothing to convert."""


class NotP7(Exception):
    """The file is not a Dolby Vision profile 7 movie — this route does not apply."""


class Aborted(Exception):
    """The run was stopped mid-step."""


def tools_missing():
    """The tools this route needs that are not installed — so the item can say which, plainly."""
    return [os.path.basename(t) for t in (FFMPEG, FFPROBE, DOVI, MKVMERGE, MKVEXTRACT)
            if not os.path.exists(t)]


def _run(cmd, *, abort=None, timeout=None, shell=False):
    """subprocess.run that dies within a second of `abort` being set. (rc, stdout, stderr)"""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         shell=shell, executable=("/bin/bash" if shell else None),
                         start_new_session=True)
    try:        # the whole new process group — a pipeline's later children inherit it
        os.setpriority(os.PRIO_PGRP, p.pid, NICE)
    except OSError:
        pass
    t0 = time.time()
    while True:
        try:
            out, err = p.communicate(timeout=1.0)
            return p.returncode, out, err
        except subprocess.TimeoutExpired:
            if (abort is not None and abort.is_set()) or (timeout and time.time() - t0 > timeout):
                try:
                    os.killpg(p.pid, 9)          # the whole pipeline, not just the shell
                except OSError:
                    pass
                p.communicate()
                if abort is not None and abort.is_set():
                    raise Aborted("stopped")
                raise RuntimeError(f"timed out after {timeout}s: {str(cmd)[:120]}")


def mkv_info(path):
    rc, out, err = _run([MKVMERGE, "-J", path], timeout=900)
    if rc > 1:
        raise RuntimeError(f"mkvmerge -J: {(out + err)[-300:]}")
    return json.loads(out)


def rpu_profile(path, stream_sel, work):
    """(profile, enhancement-layer type) from the RPUs of a stream's first frames."""
    rpu = os.path.join(work, "probe_rpu.bin")
    cmd = (f"rm -f {shlex.quote(rpu)}; {FFMPEG} -nostdin -loglevel error -i {shlex.quote(path)} "
           f"-map {stream_sel} -c:v copy -bsf:v hevc_mp4toannexb -frames:v 24 -f hevc - "
           f"| {DOVI} extract-rpu - -o {shlex.quote(rpu)} >/dev/null 2>&1; "
           f"{DOVI} info -i {shlex.quote(rpu)} --summary")
    _rc, out, _err = _run(cmd, shell=True, timeout=900)
    try:
        os.remove(rpu)
    except OSError:
        pass
    m = re.search(r"Profile: (\d+)(?: \((\w+)\))?", out)
    return (int(m.group(1)), m.group(2)) if m else (None, None)


def probe_dv(path):
    _rc, out, _err = _run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream_side_data=dv_profile,dv_bl_signal_compatibility_id,el_present_flag",
                           "-of", "json", path], timeout=300)
    sd = [x for s in json.loads(out or "{}").get("streams", [])
          for x in s.get("side_data_list", []) if "dv_profile" in x]
    return sd[0] if sd else {}


def count_packets(path, sel, abort=None):
    _rc, out, _err = _run([FFPROBE, "-v", "error", "-select_streams", sel, "-count_packets",
                           "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path],
                          abort=abort, timeout=6 * 3600)
    return int(out.strip().split(",")[0])


def nal_types(pkt: bytes):
    """PURE: the NAL unit types of one Matroska HEVC block (4-byte length prefixes), in order; None
    when it does not parse as one."""
    out, i = [], 0
    while i < len(pkt):
        if i + 4 > len(pkt):
            return None
        n = int.from_bytes(pkt[i:i + 4], "big")
        i += 4
        if n < 2 or i + n > len(pkt):
            return None
        out.append((pkt[i] >> 1) & 0x3F)
        i += n
    return out


def blocks_of(packets) -> list:
    """PURE: ffprobe's -show_data packets as bytes, in file order. Each dump line is a 10-character
    offset, a 40-character hex field, then the ASCII column."""
    return [bytes.fromhex("".join(line[10:50].replace(" ", "")
                                  for line in (p.get("data") or "").splitlines()))
            for p in packets]


def dumped_packets(text: str) -> list:
    """PURE: the blocks of `ffprobe -show_packets -show_data -of json` text."""
    return blocks_of(json.loads(text or "{}").get("packets", []))


def last_pts(packets):
    """PURE: the latest presentation timestamp among ffprobe packets, or None."""
    ts = [float(p["pts_time"]) for p in packets if p.get("pts_time") not in (None, "", "N/A")]
    return max(ts) if ts else None


def dv_only_tail(blocks) -> int:
    """PURE: how many blocks at the very end hold nothing but Dolby Vision data (EL NALs and RPUs)
    — no base-layer picture. Only the trailing run counts: a block like that anywhere else is not
    the known quirk, and the frame check must still refuse it."""
    n = 0
    for pkt in reversed(blocks):
        t = nal_types(pkt)
        if not t or not set(t) <= DV_NALS:
            break
        n += 1
    return n


def repeated_pts(path, track_id, abort=None) -> list:
    """The timestamps (in the stream's time base) of every block after the first that repeats one —
    where a picture-less Dolby Vision block can sit. One pass over the packet index."""
    rc, out, err = _run([FFPROBE, "-v", "error", "-select_streams", str(track_id), "-show_entries",
                         "packet=pts", "-of", "csv=p=0", path], abort=abort, timeout=6 * 3600)
    if rc:
        raise RuntimeError(f"reading the video track's timestamps: {err[-300:]}")
    seen, dups = set(), []
    for line in out.splitlines():
        t = line.split(",")[0].strip()
        if not t.lstrip("-").isdigit():
            continue
        v = int(t)
        if v in seen:
            dups.append(v)
        else:
            seen.add(v)
    return dups


def time_base(path, track_id) -> float:
    """Seconds per timestamp unit of a track (Matroska: 1/1000)."""
    _rc, out, _err = _run([FFPROBE, "-v", "error", "-select_streams", str(track_id), "-show_entries",
                           "stream=time_base", "-of", "csv=p=0", path], timeout=300)
    num, _, den = first_field_of(out).partition("/")
    try:
        return int(num) / int(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.001


def first_field_of(out) -> str:
    """PURE: the first value of ffprobe csv output (ffprobe 8 can trail a comma after it)."""
    return ((out or "").strip().splitlines() or [""])[0].split(",")[0].strip()


def dv_only_blocks(path, track_id, pts_list, abort=None) -> int:
    """How many of the repeated timestamps carry a block holding nothing but Dolby Vision data —
    each one READ, never assumed from the repeat alone."""
    tb = time_base(path, track_id)
    found = 0
    for pts in pts_list:
        t = pts * tb
        rc, out, _err = _run([FFPROBE, "-v", "error", "-select_streams", str(track_id),
                              "-read_intervals", f"{max(0.0, t - 3):.3f}%{t + 1:.3f}", "-show_packets",
                              "-show_data", "-of", "json", path], abort=abort, timeout=900)
        if rc:
            continue
        same = [pk for pk in json.loads(out or "{}").get("packets", [])
                if str(pk.get("pts")).lstrip("-").isdigit() and int(pk["pts"]) == pts]
        if any(dv_only_tail([b]) for b in blocks_of(same)):
            found += 1
    return found


def tail_packets(path, track_id, duration_ns, abort=None, data=False) -> list:
    """The video track's packets over its last TAIL_SECS, from ffprobe; with their bytes if `data`."""
    start = max(0.0, duration_ns / 1e9 - TAIL_SECS)
    rc, out, err = _run([FFPROBE, "-v", "error", "-select_streams", str(track_id), "-read_intervals",
                         f"{start:.3f}%", "-show_packets"] + (["-show_data"] if data else [])
                        + ["-of", "json", path], abort=abort, timeout=900)
    if rc:
        raise RuntimeError(f"reading the end of the video track: {err[-300:]}")
    return json.loads(out or "{}").get("packets", [])


def video_start(path, track_id) -> float:
    """The video track's first PRESENTED timestamp: the smallest in its first 2 s. A file can open
    on a keyframe that is shown after the leading pictures decoded behind it, so its first block's
    timestamp is not the one a rebuilt stream's first picture must be given (review 2026-10-01)."""
    _rc, out, _err = _run([FFPROBE, "-v", "error", "-select_streams", str(track_id), "-read_intervals",
                           "%+2", "-show_entries", "packet=pts_time", "-of", "csv=p=0", path],
                          timeout=300)
    ts = []
    for line in out.splitlines():
        t = line.split(",")[0].strip()
        try:
            ts.append(float(t))
        except ValueError:
            pass
    return min(ts) if ts else 0.0


def video_end(path, track_id, duration_ns):
    """(the video track's last timestamp in s, True when the file stops short of its own header) —
    read from the header's last second, which ffprobe cannot reach in a file that was cut short."""
    start = max(0.0, duration_ns / 1e9 - TAIL_SECS)
    _rc, out, err = _run([FFPROBE, "-v", "error", "-select_streams", str(track_id), "-read_intervals",
                          f"{start:.3f}%", "-show_entries", "packet=pts_time", "-of", "csv=p=0", path],
                         timeout=900)
    ts = [float(t) for t in (first_field_of(line) for line in out.splitlines())
          if t.replace(".", "", 1).isdigit()]
    return (max(ts) if ts else None), ENDED_EARLY in err


def hms(secs) -> str:
    """PURE: 6913.1 -> "1:55:13"."""
    s = int(secs)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def block_pts(path, track_id, abort=None) -> list:
    """Every block's timestamp on a track, in the stream's time base (Matroska: ms), file order."""
    rc, out, err = _run([FFPROBE, "-v", "error", "-select_streams", str(track_id), "-show_entries",
                         "packet=pts", "-of", "csv=p=0", path], abort=abort, timeout=6 * 3600)
    if rc:
        raise RuntimeError(f"reading the video track's timestamps: {err[-300:]}")
    return [int(t) for t in (first_field_of(line) for line in out.splitlines())
            if t.lstrip("-").isdigit()]


def picture_times(pts, frame_ms) -> list:
    """PURE: the presentation timestamps of the pictures, from every block's: sorted, and without
    the blocks that sit on (or within half a frame of) a picture's timestamp — the blocks of Dolby
    Vision data with no picture, which a decoder skips and the rebuilt stream does not have."""
    out = []
    for t in sorted(pts):
        if out and t - out[-1] < frame_ms / 2:
            continue
        out.append(t)
    return out


def ms_text(t) -> str:
    """PURE: a timestamp file's millisecond value: 7925459.0 -> "7925459", 41.7083 -> "41.708"."""
    r = round(t, 3)
    return str(int(r)) if r == int(r) else f"{r:.3f}".rstrip("0")


def even(times, frame_ms) -> bool:
    """PURE: whether frame k sits at times[0] + k frames, to the millisecond the file rounds to —
    what a rebuilt stream with a default duration and the original's start reproduces."""
    t0 = times[0] if times else 0
    return all(abs(t - (t0 + k * frame_ms)) <= 1 for k, t in enumerate(times))


def frames_tag(info, track_id):
    """mkvmerge's NUMBER_OF_FRAMES statistics tag for a track, or None."""
    for t in info.get("tracks", []):
        if t["id"] == track_id:
            v = (t.get("properties") or {}).get("tag_number_of_frames")
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None
    return None


def first_pts(path):
    _rc, out, _err = _run([FFPROBE, "-v", "error", "-read_intervals", "%+2", "-show_entries",
                           "packet=stream_index,pts_time", "-of", "csv=p=0", path], timeout=300)
    seen = {}
    for line in out.splitlines():
        idx, _, t = line.partition(",")
        if idx not in seen and t not in ("", "N/A"):
            seen[idx] = round(float(t), 3)
    return seen


def default_duration(ns):
    """mkvmerge's --default-duration spelling for a frame duration in nanoseconds."""
    for exact, spec in ((1e9 * 1001 / 24000, "24000/1001p"), (1e9 / 24, "24p"), (1e9 / 25, "25p"),
                        (1e9 * 1001 / 30000, "30000/1001p"), (1e9 / 30, "30p"), (1e9 / 50, "50p"),
                        (1e9 * 1001 / 60000, "60000/1001p"), (1e9 / 60, "60p")):
        if abs(ns - exact) < 2:
            return spec
    return f"{int(ns)}ns"


def layout(info):
    """(base-layer track, EL track or None, dual) from mkvmerge -J. Raises on anything else.

    Dual track = two HEVC tracks where one is 1920 wide: the 1080p track carries the EL and RPU."""
    hevc = [t for t in info["tracks"] if t["type"] == "video" and "HEVC" in t["codec"]]
    dual = (len(hevc) == 2 and any((t["properties"].get("pixel_dimensions") or "")
                                   .startswith("1920") for t in hevc))
    if len(hevc) != 1 and not dual:
        raise RuntimeError("unexpected video layout: " + ", ".join(
            f"{t['codec']} {t['properties'].get('pixel_dimensions')}" for t in hevc))
    bl = max(hevc, key=lambda t: int((t["properties"].get("pixel_dimensions") or "0x0").split("x")[0]))
    el = [t for t in hevc if t is not bl][0] if dual else None
    return bl, el, dual


def inspect(path, work):
    """Everything the conversion needs to know about the source. Raises AlreadyP8 or NotP7 when
    the route does not apply, RuntimeError when the file is not a shape the recipe was proven on."""
    info = mkv_info(path)
    bl, el, dual = layout(info)
    for t in [bl] + ([el] if el else []):       # mkvmerge ids must be ffmpeg's stream indexes
        _rc, c, _e = _run([FFPROBE, "-v", "error", "-select_streams", str(t["id"]), "-show_entries",
                           "stream=codec_name,width", "-of", "csv=p=0", path], timeout=300)
        c = c.strip()
        want_w = (t["properties"].get("pixel_dimensions") or "x").split("x")[0]
        if not c.startswith("hevc,") or c.split(",")[1] != want_w:
            raise RuntimeError(f"track {t['id']} is not the expected HEVC stream in ffmpeg ({c})")
    dur = info["container"]["properties"].get("duration", 0)
    end, early = video_end(path, bl["id"], dur)
    if early and end is not None and dur / 1e9 - end > TRUNCATED_SLACK_SECS:
        # The Boy and the Heron (2026-10-04): 78.9 GB on the NAS, its video stopping at 1:55:13 of
        # the 2:03:57 its header gives. Nothing can bring the rest back; it needs a complete copy.
        raise RuntimeError(f"the original is incomplete: its video stops at {hms(end)} of the "
                           f"{hms(dur / 1e9)} its header gives (the file was cut short) — "
                           "replace it with a complete copy")
    prof, el_type = rpu_profile(path, f"0:{(el or bl)['id']}", work)
    if prof == 8:
        raise AlreadyP8("already Dolby Vision profile 8")
    if prof != 7:
        raise NotP7(f"its Dolby Vision RPU reads profile {prof}, not 7")
    props = bl["properties"]
    dd = props.get("default_duration")
    if not dd:
        _rc, fr, _e = _run([FFPROBE, "-v", "error", "-select_streams", str(bl["id"]), "-show_entries",
                            "stream=r_frame_rate", "-of", "csv=p=0", path], timeout=300)
        num, _, den = fr.strip().partition("/")
        dd = 1e9 * int(den or 1) / int(num)
    start = video_start(path, bl["id"])
    return {"info": info, "bl": bl, "el": el, "dual": dual, "el_type": el_type,
            "default_duration": dd, "video_start_ms": max(0, round(start * 1000))}


def _nalfix_counts(err):
    """PURE: (pictures, moved) from nalfix's stderr line, or (None, 0)."""
    m = re.search(r"nalfix pictures=(\d+) moved=(\d+)", err or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, 0)


def _bare_video(src, v, work, insp, tool, *, abort=None):
    """Write the profile 8.1 video stream, with no container, to `v`, its layers pulled out of `src`
    by `tool` ("ffmpeg" or "mkvextract"). Returns False when ffmpeg dropped a packet it could not
    convert to a raw stream: it says so on stderr and still exits 0, and the movie would come out a
    frame short and drift against its audio after that point. mkvextract copies such a packet.
    Otherwise returns (pictures, moved) from nalfix, which sits before dovi_tool on a single track
    (None, 0 on a dual track, which has no Dolby Vision NALs in its base layer)."""
    bl, el, dual = insp["bl"], insp["el"], insp["dual"]
    tmp_bl, tmp_el, rpu = (os.path.join(work, n) for n in ("bl.hevc", "el.hevc", "rpu81.bin"))
    fix = f"{shlex.quote(sys.executable)} {shlex.quote(NALFIX)}"
    if tool == "ffmpeg" and not dual:
        rc, _o, err = _run(
            f"set -o pipefail; {FFMPEG} -nostdin -loglevel error -i {shlex.quote(src)} "
            f"-map 0:{bl['id']} -c:v copy -bsf:v hevc_mp4toannexb -f hevc - "
            f"| {fix} | {DOVI} -m 2 convert --discard - -o {shlex.quote(v)}", shell=True, abort=abort)
        if rc:
            raise RuntimeError(f"convert: {err[-300:]}")
        return False if BSF_DROPPED in err else _nalfix_counts(err)
    layers = [(bl["id"], tmp_bl)] + ([(el["id"], tmp_el)] if dual else [])
    if tool == "ffmpeg":
        for tid, dst in layers:
            rc, _o, err = _run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-i", src, "-map",
                                f"0:{tid}", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f",
                                "hevc", dst], abort=abort)
            if rc:
                raise RuntimeError(f"extracting track {tid}: {err[-300:]}")
            if BSF_DROPPED in err:
                return False
    else:
        rc, o, err = _run([MKVEXTRACT, "-q", src, "tracks"] + [f"{tid}:{dst}" for tid, dst in layers],
                          abort=abort, timeout=6 * 3600)
        if rc < 0 or rc > 1:                 # 1 = warnings, as with mkvmerge; < 0 = killed
            raise RuntimeError(f"mkvextract {rc}: {(o + err)[-300:]}")
    if dual:
        for cmd in ([DOVI, "-m", "2", "extract-rpu", tmp_el, "-o", rpu],
                    [DOVI, "inject-rpu", "-i", tmp_bl, "--rpu-in", rpu, "-o", v]):
            rc, _o, err = _run(cmd, abort=abort)
            if rc:
                raise RuntimeError(f"dovi_tool {cmd[1] if cmd[1] != '-m' else cmd[3]}: {err[-300:]}")
        counts = (None, 0)
    else:
        rc, _o, err = _run(f"set -o pipefail; {fix} < {shlex.quote(tmp_bl)} "
                           f"| {DOVI} -m 2 convert --discard - -o {shlex.quote(v)}",
                           shell=True, abort=abort)
        if rc:
            raise RuntimeError(f"convert: {err[-300:]}")
        counts = _nalfix_counts(err)
    for f in (tmp_bl, tmp_el, rpu):                  # ~a movie's worth of disk: free it before the mux
        if os.path.exists(f):
            os.remove(f)
    return counts


def build(src, out, work, insp, *, abort=None, progress=None):
    """Write the profile 8.1 file to `out`. No verification — that is verify()'s job. Returns what
    the build did: {"extractor": "ffmpeg" or "mkvextract" (ffmpeg dropped a packet), "pictures":
    the source's picture count from nalfix (None on a dual track), "moved": split Dolby Vision
    blocks handed back to their pictures, "timestamps": whether the original's own frame times
    were carried over}."""
    bl, el, dual = insp["bl"], insp["el"], insp["dual"]
    v = os.path.join(work, "video_p81.hevc")
    ts_file = os.path.join(work, "timestamps.txt")
    temps = [v, ts_file] + [os.path.join(work, n) for n in ("bl.hevc", "el.hevc", "rpu81.bin")]
    try:
        if progress:
            progress(5)
        extractor = "ffmpeg"
        counts = _bare_video(src, v, work, insp, "ffmpeg", abort=abort)
        if counts is False:
            # Risky Business (2026-10-03): one block holds an end-of-sequence NAL whose length
            # prefix says 1 byte (a NAL header is 2), so ffmpeg refused the whole block — a picture
            # and its RPU — and the frame check caught a file one frame short.
            for f in temps:
                if os.path.exists(f):
                    os.remove(f)
            extractor = "mkvextract"
            counts = _bare_video(src, v, work, insp, "mkvextract", abort=abort)
        pictures, moved = counts if isinstance(counts, tuple) else (None, 0)
        drop = f"!{bl['id']},{el['id']}" if dual else f"!{bl['id']}"
        # The new video is a raw stream, which carries no timestamps: mkvmerge gives frame k the
        # start + k frames. When the original's frames are NOT evenly spaced (The Mandalorian and
        # Grogu, 2026-10-04: three gaps 2, 12 and 27 ms long, a frame in all), its own times are
        # carried over instead — or every frame after a gap would play early against the audio.
        frame_ms = insp["default_duration"] / 1e6
        ms = time_base(src, bl["id"]) * 1000                # Matroska's default scale: 1 ms
        pts = [p * ms for p in block_pts(src, bl["id"], abort)]
        times = picture_times(pts, frame_ms)
        # a dual track's base layer has no Dolby Vision blocks: every block must be a picture
        expect = pictures if pictures is not None else len(pts)
        use_ts = bool(times) and len(times) == expect and not even(times, frame_ms)
        if use_ts:
            with open(ts_file, "w") as fh:
                fh.write("# timestamp format v2\n" + "".join(f"{ms_text(t)}\n" for t in times))
        if progress:
            progress(45)
        props = bl["properties"]
        title = re.sub(r"\bP7\b", "P8", insp["info"]["container"]["properties"].get("title") or "")
        cmd = [MKVMERGE, "-q", "-o", out]
        if title:
            cmd += ["--title", title]
        if props.get("language"):
            cmd += ["--language", f"0:{props['language']}"]
        if props.get("track_name"):
            cmd += ["--track-name", f"0:{props['track_name']}"]
        cmd += ["--default-track-flag", f"0:{'yes' if props.get('default_track') else 'no'}",
                "--forced-display-flag", f"0:{'yes' if props.get('forced_track') else 'no'}",
                "--default-duration", f"0:{default_duration(insp['default_duration'])}"]
        if use_ts:                                 # absolute times: the start comes with them
            cmd += ["--timestamps", f"0:{ts_file}"]
        elif insp.get("video_start_ms"):           # a raw stream starts at 0: keep the original's
            cmd += ["--sync", f"0:{int(insp['video_start_ms'])}"]
        cmd += [v, "--video-tracks", drop, src]
        rc, o, err = _run(cmd, abort=abort, timeout=6 * 3600)
        if rc > 1:                           # 1 = warnings, which mkvmerge prints for many releases
            raise RuntimeError(f"mkvmerge {rc}: {(o + err)[-300:]}")
        if progress:
            progress(85)
        return {"extractor": extractor, "pictures": pictures, "moved": moved, "timestamps": use_ts}
    finally:
        for f in temps:                              # every intermediate, on success AND failure
            if os.path.exists(f):
                os.remove(f)


def verify(src, out, insp, work, *, abort=None, pictures=None):
    """Raise unless the new file is the same movie with profile 8.1 metadata. Returns
    {"frames": n, "size_out": bytes}."""
    dv = probe_dv(out)
    if (dv.get("dv_profile"), dv.get("dv_bl_signal_compatibility_id"), dv.get("el_present_flag")) != (8, 1, 0):
        raise RuntimeError(f"the new file's DV side data is wrong: {dv}")
    if rpu_profile(out, "0:0", work)[0] != 8:
        raise RuntimeError("the new file's RPU does not read profile 8")
    info, oinfo = insp["info"], mkv_info(out)
    want_tracks = len(info["tracks"]) - (1 if insp["dual"] else 0)
    if len(oinfo["tracks"]) != want_tracks:
        raise RuntimeError(f"track count {len(oinfo['tracks'])} != {want_tracks}")
    for k in ("chapters", "attachments"):
        if len(oinfo.get(k, [])) != len(info.get(k, [])):
            raise RuntimeError(f"{k} differ ({len(oinfo.get(k, []))} vs {len(info.get(k, []))})")
    d0 = info["container"]["properties"].get("duration", 0)
    d1 = oinfo["container"]["properties"].get("duration", 0)
    if abs(d0 - d1) > DURATION_SLACK_NS:
        raise RuntimeError(f"duration differs by {(d1 - d0) / 1e6:.0f} ms")
    # Frame counts from mkvmerge's statistics tags when both carry them — the new file's is written
    # by this mkvmerge from what it actually muxed — and a full packet count when either is missing
    # or they disagree, because a stale tag on a release must cost a recount, never a false pass.
    bl_id = insp["bl"]["id"]
    src_tail = tail_packets(src, bl_id, d0, abort, data=not insp["dual"])
    bare = 0 if insp["dual"] else dv_only_tail(blocks_of(src_tail))   # no EL in a dual track's BL
    f0, f1 = frames_tag(info, bl_id), frames_tag(oinfo, 0)
    if f0 is None or f1 is None or f0 - bare != f1:
        f0, f1 = count_packets(src, str(bl_id), abort), count_packets(out, "0", abort)
    if f0 - bare != f1 and pictures is not None and f1 == pictures:
        # The source's pictures, counted slice by slice on the way in (nalfix): every one of them
        # is in the new file, and the rest of the blocks held none (Star Trek: Nemesis, 9325). Read
        # FIRST: the per-timestamp reads below take ~9 s each, and Nemesis has 6523 repeats.
        bare = f0 - f1
    if f0 - bare != f1 and not insp["dual"] and f1 < f0:
        # Short: picture-less blocks in the MIDDLE too? Every repeated timestamp is read; only the
        # blocks holding no base-layer picture count (Gladiator: 21 + the last one).
        dups = repeated_pts(src, bl_id, abort)
        if len(dups) >= f0 - f1:
            bare = dv_only_blocks(src, bl_id, dups, abort)
    if f0 - bare != f1:
        raise RuntimeError(f"frame count {f1} != the original's {f0 - bare}"
                           + (f" ({f0} blocks, {bare} of them Dolby Vision data with no picture)"
                              if bare else ""))
    e0, e1 = last_pts(src_tail), last_pts(tail_packets(out, "0", d1, abort))
    if e0 is None or e1 is None or abs(e1 - e0) * 1000 > END_SLACK_MS:
        raise RuntimeError(f"the video ends at {e1} s, not the original's {e0} s")
    a0 = next((str(t["id"]) for t in info["tracks"] if t["type"] == "audio"), None)
    a1 = next((str(t["id"]) for t in oinfo["tracks"] if t["type"] == "audio"), None)
    # The video's start is its earliest PRESENTED frame (what --sync reproduces exactly), not its
    # first block: a source that opens on a keyframe shown after leading pictures has its blocks'
    # millisecond timestamps rounded from a finer clock, and its first block can sit 1 ms from the
    # rebuilt one's while every frame lines up (review 2026-10-01). The audio is copied untouched,
    # so its first block must match exactly.
    v0, v1 = round(video_start(src, bl_id) * 1000), round(video_start(out, 0) * 1000)
    p0, p1 = first_pts(src), first_pts(out)
    if v0 != v1 or (a0 and p0.get(a0) != p1.get(a1)):
        raise RuntimeError(f"start timestamps differ: video {v0} vs {v1} ms, first blocks {p0} vs {p1}")
    return {"frames": f1, "size_out": os.path.getsize(out)}


def building_path(out):
    """Where the new file is written until it has passed verify(). `out` itself only ever appears
    by one rename of a VERIFIED file, so a file under that name is proof — even after a crash."""
    stem, ext = os.path.splitext(out)
    return f"{stem}.building{ext or '.mkv'}"


def convert(src, out, work, *, abort=None, progress=None):
    """inspect -> build -> verify, then rename into `out`. The result dict on success; raises
    otherwise, and never leaves a half-written file behind under either name."""
    os.makedirs(work, exist_ok=True)
    missing = tools_missing()
    if missing:
        raise RuntimeError("not installed: " + ", ".join(missing)
                           + " (brew install " + " ".join(dict.fromkeys(
                               "mkvtoolnix" if m.startswith("mkv") else m for m in missing)) + ")")
    tmp = building_path(out)
    try:
        for f in (tmp, out):
            if os.path.exists(f):
                os.remove(f)
        insp = inspect(src, work)
        made = build(src, tmp, work, insp, abort=abort, progress=progress) or {}
        res = verify(src, tmp, insp, work, abort=abort, pictures=made.get("pictures"))
        os.replace(tmp, out)
        if progress:
            progress(100)
        return {"el": insp["el_type"], "dual_track": insp["dual"],
                "extractor": made.get("extractor") or "ffmpeg", "moved": made.get("moved") or 0,
                "timestamps": bool(made.get("timestamps")), **res}
    except BaseException:
        for f in (tmp, out):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
        raise
