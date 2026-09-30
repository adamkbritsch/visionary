"""Dolby Vision profile 7 -> profile 8.1, with NO video re-encode.

A DV profile 7 movie has already proven it can carry a Dolby Vision layer — it is one. So the
HDR10 base layer is copied bit for bit and only the Dolby Vision metadata is rewritten: dovi_tool
mode 2 turns each RPU into profile 8.1 and discards the enhancement layer (dropping a full
enhancement layer is user-approved), and mkvmerge puts the new video back beside every other track
of the original. No Topaz, no Resolve, no x265, and no peak cap: nothing about the picture changes,
so there is nothing for a cap to protect (user-dictated 2026-09-27/29).

The recipe and every verification are ported one for one from the user's reference driver
(~/dv-p7-to-p81/convert_batch.py), which converted Project Hail Mary and The Social Network:
  single track:  ffmpeg -map BL -c copy -bsf hevc_mp4toannexb | dovi_tool -m 2 convert --discard
  dual track:    extract the 4K base layer and the 1080p EL+RPU track; dovi_tool -m 2 extract-rpu
                 from the EL; dovi_tool inject-rpu into the base layer (both old tracks dropped)
  mkvmerge:      the new video, then every other track, chapter and attachment of the original, with
                 the video track's language, name, default/forced flags and default duration copied,
                 and "P7" in the title renamed to "P8". mkvmerge writes the DV configuration itself.
Verified on the new file: DV side data profile 8 / compatibility 1 / no EL, an RPU that reads
profile 8, the same track count (one fewer for dual track), chapters and attachments, duration
within 100 ms, the same video frame count, and the same first timestamp for the video and the first
audio track. Anything else raises, and the caller keeps the original.
"""
import json
import os
import re
import shlex
import subprocess
import time

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"
DOVI = "/opt/homebrew/bin/dovi_tool"
MKVMERGE = "/opt/homebrew/bin/mkvmerge"
DURATION_SLACK_NS = 100e6       # the new file's duration may differ by at most 100 ms


class AlreadyP8(Exception):
    """The file is already Dolby Vision profile 8 — nothing to convert."""


class NotP7(Exception):
    """The file is not a Dolby Vision profile 7 movie — this route does not apply."""


class Aborted(Exception):
    """The run was stopped mid-step."""


def tools_missing():
    """The tools this route needs that are not installed — so the item can say which, plainly."""
    return [os.path.basename(t) for t in (FFMPEG, FFPROBE, DOVI, MKVMERGE) if not os.path.exists(t)]


def _run(cmd, *, abort=None, timeout=None, shell=False):
    """subprocess.run that dies within a second of `abort` being set. (rc, stdout, stderr)"""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         shell=shell, executable=("/bin/bash" if shell else None),
                         start_new_session=True)
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
    return {"info": info, "bl": bl, "el": el, "dual": dual, "el_type": el_type,
            "default_duration": dd}


def build(src, out, work, insp, *, abort=None, progress=None):
    """Write the profile 8.1 file to `out`. No verification — that is verify()'s job."""
    bl, el, dual = insp["bl"], insp["el"], insp["dual"]
    v = os.path.join(work, "video_p81.hevc")
    temps = [v] + [os.path.join(work, n) for n in ("bl.hevc", "el.hevc", "rpu81.bin")]
    try:
        if progress:
            progress(5)
        if dual:
            tmp_bl, tmp_el, rpu = (os.path.join(work, n) for n in ("bl.hevc", "el.hevc", "rpu81.bin"))
            for tid, dst in ((bl["id"], tmp_bl), (el["id"], tmp_el)):
                rc, _o, err = _run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-i", src, "-map",
                                    f"0:{tid}", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f",
                                    "hevc", dst], abort=abort)
                if rc:
                    raise RuntimeError(f"extracting track {tid}: {err[-300:]}")
            for cmd in ([DOVI, "-m", "2", "extract-rpu", tmp_el, "-o", rpu],
                        [DOVI, "inject-rpu", "-i", tmp_bl, "--rpu-in", rpu, "-o", v]):
                rc, _o, err = _run(cmd, abort=abort)
                if rc:
                    raise RuntimeError(f"dovi_tool {cmd[1] if cmd[1] != '-m' else cmd[3]}: {err[-300:]}")
            for f in (tmp_bl, tmp_el, rpu):          # ~a movie's worth of disk: free it before the mux
                os.remove(f)
            drop = f"!{bl['id']},{el['id']}"
        else:
            rc, _o, err = _run(
                f"set -o pipefail; {FFMPEG} -nostdin -loglevel error -i {shlex.quote(src)} "
                f"-map 0:{bl['id']} -c:v copy -bsf:v hevc_mp4toannexb -f hevc - "
                f"| {DOVI} -m 2 convert --discard - -o {shlex.quote(v)}", shell=True, abort=abort)
            if rc:
                raise RuntimeError(f"convert: {err[-300:]}")
            drop = f"!{bl['id']}"
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
                "--default-duration", f"0:{default_duration(insp['default_duration'])}",
                v, "--video-tracks", drop, src]
        rc, o, err = _run(cmd, abort=abort, timeout=6 * 3600)
        if rc > 1:                           # 1 = warnings, which mkvmerge prints for many releases
            raise RuntimeError(f"mkvmerge {rc}: {(o + err)[-300:]}")
        if progress:
            progress(85)
    finally:
        for f in temps:                              # every intermediate, on success AND failure
            if os.path.exists(f):
                os.remove(f)


def verify(src, out, insp, work, *, abort=None):
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
    f0, f1 = frames_tag(info, bl_id), frames_tag(oinfo, 0)
    if f0 is None or f1 is None or f0 != f1:
        f0, f1 = count_packets(src, str(bl_id), abort), count_packets(out, "0", abort)
    if f0 != f1:
        raise RuntimeError(f"frame count {f1} != the original's {f0}")
    a0 = next((str(t["id"]) for t in info["tracks"] if t["type"] == "audio"), None)
    a1 = next((str(t["id"]) for t in oinfo["tracks"] if t["type"] == "audio"), None)
    p0, p1 = first_pts(src), first_pts(out)
    if p0.get(str(bl_id)) != p1.get("0") or (a0 and p0.get(a0) != p1.get(a1)):
        raise RuntimeError(f"start timestamps differ: {p0} vs {p1}")
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
                           + " (brew install " + " ".join(
                               "mkvtoolnix" if m == "mkvmerge" else m for m in missing) + ")")
    tmp = building_path(out)
    try:
        for f in (tmp, out):
            if os.path.exists(f):
                os.remove(f)
        insp = inspect(src, work)
        build(src, tmp, work, insp, abort=abort, progress=progress)
        res = verify(src, tmp, insp, work, abort=abort)
        os.replace(tmp, out)
        if progress:
            progress(100)
        return {"el": insp["el_type"], "dual_track": insp["dual"], **res}
    except BaseException:
        for f in (tmp, out):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
        raise
