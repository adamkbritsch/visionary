#!/usr/bin/env python3
"""Shadow sample for the native-DV quality gate (Q1 picture, Q3 format, Q4 blind A/B clips).

For ONE Topaz chunk of an episode Resolve has also rendered, build two clips the way each
path would ship them, then score both against the same lossless reference:

  native   Topaz ProRes -> captured Resolve LUT -> x265 (peak cap) + native RPU
  resolve  Resolve's render (same frames)       -> x265 (peak cap) + Resolve's RPU slice
  reference  Topaz ProRes -> LUT, uncompressed (dvnative.lut_vf, the native encode's input)

Both encodes use dvcap's exact x265 command (same CRF, preset, VBV cap). The reference IS
the native path's pre-encode signal, so Q1 measures each clip's encoding fidelity to the LUT
target; how far Resolve's own colour sits from the LUT is Q0's job, measured separately.
Q3 checks the native clip's format (hvc1, PQ/BT.2020, DV profile 8 / compat 1, CM v4.0,
L6 1000 nits, peak under the cap). The exit status is 0 only when Q1 and Q3 both pass.
A chunk (~90 s) rather than a whole episode keeps the disk footprint to ~8 GB — Visionary
gates its next item at 400 GB raw free.

Reruns into the same --out reuse work only when manifest.json there matches every input
(LUT hash, filter chain, chunk, frames, render, Resolve RPU hash, cap, analysis settings)
and a reused clip still has exactly the chunk's frame count; otherwise the derived files are
rebuilt. Every output is written to .part and renamed, so a killed run never leaves a
truncated clip that a later run would score.

--blind copies the two clips to <out>/ab/A.mp4 and B.mp4 (assignment from os.urandom) with
track names, tags and creation times neutralised, and writes the key to <out>/ab_key.json.
Only ab/ goes to the viewer: report.json and the key name the paths.

  python3 tools/dv_shadow.py --chunk seg_0008.mov --first 20098 --render R.mov \\
      --resolve-rpu resolve.rpu --lut rcm.cube --out DIR [--audio audio.mp4] [--blind]
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "engine"))
import dvcap  # noqa: E402
import dvnative  # noqa: E402

FF = dvcap.FFMPEG
MP4BOX = "/opt/homebrew/bin/MP4Box"
FPS = "24000/1001"
NICE = ["nice", "-n", "10"]
TAGS = ("native", "resolve")
MANIFEST = "manifest.json"
MANIFEST_VERSION = 1
PEAK_ESTIMATOR = "max"              # dvnative.analyze_chunks' default, pinned into the manifest
WITH_L4 = True
Q1_METRICS = ("vmaf", "ssim", "xpsnr_y")
Q1_NOTE = ("reference = the native path's own pre-encode signal (Topaz ProRes -> LUT), so Q1 "
           "measures encoding fidelity to the LUT target; Resolve's colour deviation from the "
           "LUT is measured separately by Q0")
BLIND_TIME = "1/1/2000-0:0:0"       # MP4Box -time: one pinned creation time for both A/B copies

# Everything this tool writes into --out (removed when the manifest no longer matches).
DERIVED = ("native.rpu", "native.rpu.generate.json", "resolve_slice.rpu", "native.hevc",
           "resolve.hevc", "native.mp4", "resolve.mp4", "native.clip.rpu", "resolve.clip.rpu",
           "report.json", "ab_key.json", os.path.join("ab", "A.mp4"), os.path.join("ab", "B.mp4"))


def _rm(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def file_id(path: str | None):
    """Path + size + mtime: cheap identity for the multi-GB media inputs (None when absent)."""
    if not path:
        return None
    st = os.stat(path)
    return {"path": os.path.abspath(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


# ---- manifest (pure) -----------------------------------------------------------------------
# dvnative.analyze_chunks reuses a chunk's sidecar whenever it exists, and the encodes are
# expensive, so the manifest is the only thing that can tell a rerun its old outputs were built
# from a different LUT, filter chain, chunk or cap (review-caught: a fixed lut_vf rerun into the
# same --out reported the old double-converted clip and L1).

def inputs_manifest(*, lut_sha256: str, vf: str, chunk: dict, first: int, n: int, render: dict,
                    resolve_rpu_sha256: str, cap: int, audio: dict | None, cube: str) -> dict:
    """PURE. Everything a derived output depends on. Only JSON scalars, lists and dicts, so it
    compares equal after a round trip through manifest.json."""
    return {"version": MANIFEST_VERSION, "lut_sha256": lut_sha256, "vf": vf, "chunk": chunk,
            "first": int(first), "frames": int(n), "render": render,
            "resolve_rpu_sha256": resolve_rpu_sha256, "cap": int(cap), "audio": audio,
            "x265": {"crf": dvcap.X265_CRF, "preset": dvcap.X265_PRESET},
            "analysis": {"vf": dvnative.analysis_vf(cube), "with_l4": WITH_L4,
                         "peak_estimator": PEAK_ESTIMATOR,
                         "analyzer_commit": dvnative.HDR_ANALYZER_COMMIT,
                         "l1_max_pq": dvnative.SOURCE_MAX_PQ, "rpu_l4": WITH_L4}}


def manifest_matches(old, inputs: dict) -> bool:
    """PURE. True when a loaded manifest was written for exactly these inputs."""
    return isinstance(old, dict) and old.get("inputs") == inputs


def hevc_reusable(built: dict, name: str, stamp: str, frames: int, n: int) -> bool:
    """PURE. An existing clip is reused only when it was built from this exact RPU (`stamp`,
    its sha256) under the current manifest AND still has every frame."""
    return bool(stamp) and (built or {}).get(name) == stamp and frames == n


def load_manifest(out: str):
    try:
        with open(os.path.join(out, MANIFEST)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_manifest(out: str, man: dict):
    path = os.path.join(out, MANIFEST)
    with open(path + ".part", "w") as f:
        json.dump(man, f, indent=1)
    os.replace(path + ".part", path)


def reset_out(out: str):
    """Remove every derived output (and its .part) so nothing built from other inputs survives.
    Only this tool's own file names are touched."""
    for rel in DERIVED:
        _rm(os.path.join(out, rel))
        _rm(os.path.join(out, rel + ".part"))
    adir = os.path.join(out, "analysis")
    if os.path.isdir(adir):
        for name in os.listdir(adir):
            if name.startswith("chunk_"):
                _rm(os.path.join(adir, name))


# ---- encode / mux (atomic) -----------------------------------------------------------------

def encode(dec_cmd: list, rpu: str, out_hevc: str, cap: int, master_display, max_cll, n: int) -> int:
    """x265 to <out>.part, published only when exactly `n` frames came out (dvcap's rule: a
    short encode means seek/decode drift, and the RPU would misalign)."""
    part = out_hevc + ".part"
    _rm(part)
    enc = dvcap.build_x265_command(dvcap.X265, rpu, part, cap, master_display, max_cll)
    try:
        got, why, _tail = dvcap._encode_pipe(NICE + dec_cmd, NICE + enc, part, None, None)
        if got is None:
            raise RuntimeError(f"encode failed: {why}")
        if got != n:
            raise RuntimeError(f"encode frame mismatch: {got} != {n}")
        os.replace(part, out_hevc)
    finally:
        _rm(part)
    return got


def mux(hevc: str, out_mp4: str, audio: str | None):
    part = out_mp4 + ".part"
    _rm(part)
    cmd = [MP4BOX, "-add", f"{hevc}:dvp=8.1:fps={FPS}"]
    if audio:
        cmd += ["-add", f"{audio}#audio"]
    try:
        subprocess.run(cmd + ["-new", part], check=True, capture_output=True)
        os.replace(part, out_mp4)
    finally:
        _rm(part)


def slice_resolve_rpu(rpu: str, first: int, n: int, out: str):
    total = dvcap.rpu_frame_count(rpu)
    if first + n > total:
        raise SystemExit(f"Resolve RPU has {total} frames; the chunk needs {first}..{first + n}")
    part = out + ".part"
    _rm(part)
    try:
        ok, why = dvcap.slice_rpu(rpu, first, first + n, total, part)
        if not ok:
            raise SystemExit(why)
        os.replace(part, out)
    finally:
        _rm(part)


# ---- Q1 picture ----------------------------------------------------------------------------

def metric_graph(filter_name: str, cube: str, opts: str = "") -> str:
    """PURE. Distorted clip [0] vs the lossless reference [1]: the chunk through the same
    dvnative.lut_vf chain the native encode used."""
    return (f"[0:v]setpts=PTS-STARTPTS,format=yuv420p10le[d];"
            f"[1:v]{dvnative.lut_vf(cube)},setpts=PTS-STARTPTS[r];[d][r]{filter_name}{opts}")


def metric(filter_name: str, dist: str, chunk: str, cube: str, n: int, opts: str = "") -> str:
    """Run one full-reference metric; returns ffmpeg's stderr (parse_metrics reads it)."""
    r = subprocess.run(NICE + [FF, "-hide_banner", "-nostdin", "-i", dist, "-i", chunk,
                               "-frames:v", str(n), "-filter_complex",
                               metric_graph(filter_name, cube, opts), "-f", "null", "-"],
                       capture_output=True, text=True, errors="replace")
    return r.stderr


# The summary lines ffmpeg 8.0 prints (checked on real output):
#   [Parsed_libvmaf_4 @ 0x...] VMAF score: 98.407147
#   [Parsed_ssim_4 @ 0x...] SSIM Y:0.814289 (7.311628) U:... V:... All:0.830782 (7.715542)
#   [Parsed_xpsnr_4 @ 0x...] XPSNR  y: 32.3663  u: 32.4726  v: 32.4434  (minimum: 32.3663)
# Identical frames print "XPSNR  y: inf", which the old [\d.]+ pattern read as a failure.
_NUM = r"(inf|\d+(?:\.\d+)?)"
_VMAF_RE = re.compile(r"VMAF score:\s*" + _NUM)
_SSIM_RE = re.compile(r"SSIM\s.*?All:\s*" + _NUM)
_XPSNR_RE = re.compile(r"XPSNR\s+y:\s*" + _NUM)


def _num(rx, text: str):
    m = rx.search(text or "")
    return float(m.group(1)) if m else None


def parse_metrics(vmaf_err: str, ssim_err: str, xpsnr_err: str) -> dict:
    """PURE. ffmpeg stderr of each metric run -> {vmaf, ssim, xpsnr_y}; None when absent."""
    return {"vmaf": _num(_VMAF_RE, vmaf_err), "ssim": _num(_SSIM_RE, ssim_err),
            "xpsnr_y": _num(_XPSNR_RE, xpsnr_err)}


def q1_verdict(native: dict, resolve: dict) -> bool:
    """PURE. The approved gate: native >= the Resolve path on every metric, no tolerance."""
    return all(native.get(k) is not None and resolve.get(k) is not None
               and native[k] >= resolve[k] for k in Q1_METRICS)


# ---- Q3 format -----------------------------------------------------------------------------

def probe_clip(mp4: str) -> dict:
    r = subprocess.run([dvcap.FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=codec_tag_string,color_transfer,color_primaries:stream_side_data="
                        "side_data_type,dv_profile,dv_bl_signal_compatibility_id",
                        "-of", "json", mp4], capture_output=True, text=True, errors="replace")
    try:
        return json.loads(r.stdout or "{}")
    except ValueError:
        return {}


def parse_rpu_frame_json(text: str):
    """PURE. `dovi_tool info -f N` stdout ("Parsing RPU file..." then one JSON object) -> dict."""
    i = (text or "").find("{")
    if i < 0:
        return None
    try:
        return json.loads(text[i:])
    except ValueError:
        return None


def clip_rpu(hevc: str, out_rpu: str):
    """The RPU actually interleaved into a clip (not the file handed to x265): extracted from
    the HEVC. Returns (frame-0 JSON | None, frame count)."""
    part = out_rpu + ".part"
    _rm(part)
    r = subprocess.run([dvcap.DOVI_TOOL, "extract-rpu", "-i", hevc, "-o", part],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0 or not os.path.exists(part) or os.path.getsize(part) == 0:
        _rm(part)
        return None, 0
    os.replace(part, out_rpu)
    info = subprocess.run([dvcap.DOVI_TOOL, "info", "-i", out_rpu, "-f", "0"],
                          capture_output=True, text=True, errors="replace")
    return parse_rpu_frame_json(info.stdout), dvcap.rpu_frame_count(out_rpu)


def _video_stream(probe: dict) -> dict:
    return ((probe or {}).get("streams") or [{}])[0]


def dovi_record(probe: dict) -> dict:
    """PURE. The video stream's DOVI configuration record from ffprobe JSON ({} when absent)."""
    sds = _video_stream(probe).get("side_data_list") or []
    return next((sd for sd in sds if isinstance(sd, dict) and "dv_profile" in sd), {})


def q3_evaluate(probe: dict, rpu0, rpu_frames: int, n: int, peak_mbps: float, cap: int) -> dict:
    """PURE. ffprobe JSON of the MP4 + frame 0 of its RPU -> {check: {got, want, ok}, pass}.
    CM v4.0 shows as the RPU's cmv40_metadata block (dovi_tool's "DM version: 2 (CM v4.0)");
    L6 rides in the CM v2.9 extension blocks."""
    s = _video_stream(probe)
    dovi = dovi_record(probe)
    vdr = (rpu0 or {}).get("vdr_dm_data") or {}
    blocks = (vdr.get("cmv29_metadata") or {}).get("ext_metadata_blocks") or []
    l6 = next((b["Level6"] for b in blocks if isinstance(b, dict) and "Level6" in b), {})
    want = (("hvc1", s.get("codec_tag_string"), "hvc1"),
            ("transfer", s.get("color_transfer"), "smpte2084"),
            ("primaries", s.get("color_primaries"), "bt2020"),
            ("dv_profile", dovi.get("dv_profile"), 8),
            ("dv_compat_id", dovi.get("dv_bl_signal_compatibility_id"), 1),
            ("rpu_frames", rpu_frames, n),
            ("rpu_profile", (rpu0 or {}).get("dovi_profile"), 8),
            ("cm_v40", isinstance(vdr.get("cmv40_metadata"), dict), True),
            ("l6_max_nits", l6.get("max_display_mastering_luminance"), dvnative.MASTER_MAX_NITS))
    out = {k: {"got": got, "want": w, "ok": got == w} for k, got, w in want}
    out["peak"] = {"got": peak_mbps, "max": round(cap * dvcap.PEAK_TOLERANCE, 3),
                   "ok": dvcap.peak_ok(peak_mbps, cap)}
    out["pass"] = all(v["ok"] for v in out.values())
    return out


# ---- Q4 blind A/B --------------------------------------------------------------------------

def json_safe(v):
    """PURE. Report values for strict JSON readers: a perfect XPSNR is inf, which json.dump
    writes as the non-standard token Infinity (jq and other strict readers reject it)."""
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return "nan" if v != v else ("inf" if v > 0 else "-inf")
    if isinstance(v, dict):
        return {k: json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [json_safe(x) for x in v]
    return v


_PSNR_Y_RE = re.compile(r"PSNR y:\s*([\d.]+|inf)")


def alignment_graph(cube: str, shift: int, frames: int) -> str:
    """PURE. PSNR of distorted [0] against the reference [1] moved by `shift` frames
    (+1: distorted frame i vs reference frame i+1), over `frames` frames."""
    a, b = max(0, -shift), max(0, shift)
    d = f"[0:v]trim=start_frame={a}:end_frame={a + frames},setpts=PTS-STARTPTS,format=yuv420p10le[d]"
    r = (f"[1:v]trim=start_frame={b}:end_frame={b + frames},{dvnative.lut_vf(cube)},"
         f"setpts=PTS-STARTPTS[r]")
    return f"{d};{r};[d][r]psnr"


def best_shift(psnr_by_shift: dict) -> int:
    """PURE. The shift with the highest average PSNR; 0 is preferred on a tie."""
    return max(sorted(psnr_by_shift, key=lambda k: (k != 0, k)), key=lambda k: psnr_by_shift[k])


def check_alignment(clip: str, chunk: str, cube: str, frames: int = 48) -> dict:
    """The Resolve clip must line up with the chunk frame for frame: decoded at --first, its
    frame 0 must match the reference's frame 0 better than frame 1 (or the clip's own frame 1
    the reference's frame 0). A wrong --first makes Resolve score badly against the reference,
    so Q1 would pass for the wrong reason."""
    out = {}
    for shift in (-1, 0, 1):
        r = subprocess.run(["nice", "-n", "10", FF, "-hide_banner", "-nostdin", "-i", clip, "-i", chunk,
                            "-filter_complex", alignment_graph(cube, shift, frames), "-f", "null", "-"],
                           capture_output=True, text=True, errors="replace")
        v = _num(_PSNR_Y_RE, r.stderr)
        out[shift] = v if v is not None else float("-inf")
    return {"psnr_y": out, "best_shift": best_shift(out), "aligned": best_shift(out) == 0}


def blind_assignment(byte: int) -> dict:
    """PURE. One random byte -> {"A": tag, "B": tag}. The low bit of an os.urandom byte is a
    fair coin; never derive this from the clock."""
    first, second = TAGS if (int(byte) & 1) == 0 else TAGS[::-1]
    return {"A": first, "B": second}


def label_leaks(probe: dict, labels=TAGS) -> list:
    """PURE. Every string in ffprobe's format/stream JSON that names a path ("native",
    "resolve", any case). The format's `filename` is skipped: it is the --out path the
    viewer never sees (and the plan's shadow folder is literally `_native`)."""
    hits = []

    def walk(v, key=""):
        if isinstance(v, dict):
            for k, x in v.items():
                if key == "format" and k == "filename":
                    continue
                walk(x, k)
        elif isinstance(v, list):
            for x in v:
                walk(x, key)
        elif isinstance(v, str) and any(lab in v.lower() for lab in labels):
            hits.append(f"{key}={v}")
    walk(probe or {})
    return hits


def build_neutralize_command(mp4box: str, path: str, streams: list) -> list:
    """PURE. In-place MP4Box edit for a blind copy: one pinned movie/track/media creation time
    (the clips are muxed native-first, so real times would give the order away), generic
    track names, no per-track udta "name" box (ffprobe cannot see it, MP4Box -info can: an
    --audio file titled "resolve mix" kept that title — review-caught), and no iTunes tags."""
    cmd = [mp4box, "-time", BLIND_TIME]
    for s in streams:
        try:
            tk = int(str(s.get("id")), 0)
        except (TypeError, ValueError):
            continue
        name = {"video": "Video", "audio": "Audio"}.get(s.get("codec_type"), "Track")
        cmd += ["-time", f"{tk}={BLIND_TIME}", "-mtime", f"{tk}={BLIND_TIME}",
                "-name", f"{tk}={name}", "-udta", f"{tk}:type=name"]
    return cmd + ["-itags", "clear", path]


def probe_all(mp4: str) -> dict:
    r = subprocess.run([dvcap.FFPROBE, "-v", "error", "-show_format", "-show_streams",
                        "-of", "json", mp4], capture_output=True, text=True, errors="replace")
    try:
        return json.loads(r.stdout or "{}")
    except ValueError:
        return {}


def ab_paths(out: str) -> dict:
    ab = os.path.join(out, "ab")
    return {"dir": ab, "A": os.path.join(ab, "A.mp4"), "B": os.path.join(ab, "B.mp4"),
            "key": os.path.join(out, "ab_key.json")}


def clear_blind(out: str):
    p = ab_paths(out)
    for k in ("key", "A", "B"):
        _rm(p[k])
        _rm(p[k] + ".part")


def make_blind(out: str, clips: dict, *, rand=os.urandom, mp4box: str = MP4BOX) -> dict:
    """Copy {tag: mp4} to ab/A.mp4 and ab/B.mp4 in random order and write the key beside ab/.
    The old key goes first, so a killed run leaves no key that could pair with new files."""
    p = ab_paths(out)
    clear_blind(out)
    os.makedirs(p["dir"], exist_ok=True)
    key = blind_assignment(rand(1)[0])
    for letter in ("A", "B"):              # A then B: file times follow the letters, not the tags
        part = p[letter] + ".part"
        shutil.copyfile(clips[key[letter]], part)    # copyfile, not copy2: no source mtime
        streams = probe_all(part).get("streams") or []
        subprocess.run(build_neutralize_command(mp4box, part, streams), check=True,
                       capture_output=True)
        probe = probe_all(part)
        leaks = label_leaks(probe)
        if leaks:
            _rm(part)
            raise RuntimeError(f"blind copy {letter} still names its path: {leaks}")
        if dovi_record(probe_clip(part)).get("dv_profile") != 8:
            _rm(part)
            raise RuntimeError(f"blind copy {letter} lost its Dolby Vision configuration")
    for letter in ("A", "B"):
        os.replace(p[letter] + ".part", p[letter])
    with open(p["key"] + ".part", "w") as f:
        json.dump(key, f, indent=1)
    os.replace(p["key"] + ".part", p["key"])
    return {"dir": p["dir"], "key_file": p["key"]}


# ---- main ----------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk", required=True)
    ap.add_argument("--first", type=int, required=True, help="the chunk's first frame in the episode")
    ap.add_argument("--render", required=True)
    ap.add_argument("--resolve-rpu", required=True)
    ap.add_argument("--lut", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--audio")
    ap.add_argument("--cap", type=int, default=dvcap.DEFAULT_PEAK_MBPS)
    ap.add_argument("--blind", action="store_true",
                    help="also write ab/A.mp4 + ab/B.mp4 (random assignment) and ab_key.json")
    a = ap.parse_args(argv)
    try:
        vf = dvnative.lut_vf(a.lut)
    except ValueError as e:
        raise SystemExit(str(e))
    os.makedirs(a.out, exist_ok=True)
    probe = subprocess.run([dvcap.FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=nb_frames", "-of", "csv=p=0", a.chunk],
                           capture_output=True, text=True).stdout.strip()
    try:
        n = int(probe.splitlines()[0].split(",")[0])
    except (IndexError, ValueError):
        n = 0
    if n <= 0:
        raise SystemExit(f"could not read the chunk's frame count: {a.chunk}")

    inputs = inputs_manifest(lut_sha256=sha256_file(a.lut), vf=vf, chunk=file_id(a.chunk),
                             first=a.first, n=n, render=file_id(a.render),
                             resolve_rpu_sha256=sha256_file(a.resolve_rpu), cap=a.cap,
                             audio=file_id(a.audio), cube=a.lut)
    man = load_manifest(a.out)
    reused = manifest_matches(man, inputs)
    if not reused:
        if man is not None:
            print("inputs changed since the last run in this --out: rebuilding", file=sys.stderr)
        reset_out(a.out)
        man = {"inputs": inputs, "built": {}}
        save_manifest(a.out, man)
    built = man["built"]
    rep = {"chunk": os.path.basename(a.chunk), "first": a.first, "frames": n, "manifest_reused": reused}

    # native: measure the chunk, generate its RPU, encode from ProRes through the LUT
    nat_rpu = os.path.join(a.out, "native.rpu")
    sc = dvnative.analyze_chunks([[0, a.chunk], [n, None]], os.path.join(a.out, "analysis"),
                                 cube=a.lut, with_l4=WITH_L4, peak_estimator=PEAK_ESTIMATOR)
    cfg = dvnative.generate_rpu(sc, nat_rpu, l4=WITH_L4)
    rep["native_shots"] = len(cfg["shots"])
    rep["native_max_cll"] = dvnative.x265_max_cll(sc)

    # resolve path: the same frames of Resolve's render, with Resolve's RPU slice
    res_rpu = os.path.join(a.out, "resolve_slice.rpu")
    slice_resolve_rpu(a.resolve_rpu, a.first, n, res_rpu)

    hevcs = {"native": os.path.join(a.out, "native.hevc"), "resolve": os.path.join(a.out, "resolve.hevc")}
    stamps = {"native": sha256_file(nat_rpu), "resolve": sha256_file(res_rpu)}
    fresh = set()
    for tag in TAGS:
        name, hevc = f"{tag}.hevc", hevcs[tag]
        if hevc_reusable(built, name, stamps[tag], dvcap.count_hevc_frames(hevc), n):
            continue
        _rm(hevc)
        built.pop(name, None)
        save_manifest(a.out, man)
        if tag == "native":
            dec = dvcap.build_seg_decode_command(FF, a.chunk, 0, n, FPS, {"vf": vf})
            encode(dec, nat_rpu, hevc, a.cap, None, rep["native_max_cll"], n)
        else:
            info = dvcap.probe_video(a.render)
            dec = dvcap.build_seg_decode_command(FF, a.render, a.first, n, FPS)
            encode(dec, res_rpu, hevc, a.cap, info.get("master_display"), info.get("max_cll"), n)
        built[name] = stamps[tag]
        save_manifest(a.out, man)
        fresh.add(tag)

    mp4s = {tag: os.path.join(a.out, f"{tag}.mp4") for tag in TAGS}
    for tag in TAGS:
        name, mp4 = f"{tag}.mp4", mp4s[tag]
        if tag not in fresh and built.get(name) == stamps[tag] and dvcap.count_hevc_frames(mp4) == n:
            continue
        clear_blind(a.out)              # A/B copies of an old mux must not outlive it
        _rm(mp4)
        built.pop(name, None)
        save_manifest(a.out, man)
        mux(hevcs[tag], mp4, a.audio)
        built[name] = stamps[tag]
        save_manifest(a.out, man)

    for tag in TAGS:
        mp4 = mp4s[tag]
        got = dvcap.count_hevc_frames(mp4)
        if got != n:                    # a short clip is scored against a frozen last frame
            raise SystemExit(f"{tag}.mp4 has {got} frames, expected {n}")
        rep[tag] = parse_metrics(
            metric("libvmaf", mp4, a.chunk, a.lut, n, "=model=version=vmaf_4k_v0.6.1:n_threads=8"),
            metric("ssim", mp4, a.chunk, a.lut, n),
            metric("xpsnr", mp4, a.chunk, a.lut, n))
        rep[tag]["peak_mbps"] = dvcap.video_peak_1s_mbps(mp4)
        rep[tag]["bytes"] = os.path.getsize(hevcs[tag])
        rpu0, rpu_frames = clip_rpu(hevcs[tag], os.path.join(a.out, f"{tag}.clip.rpu"))
        rep[tag]["q3"] = q3_evaluate(probe_clip(mp4), rpu0, rpu_frames, n, rep[tag]["peak_mbps"], a.cap)
    rep["alignment"] = check_alignment(mp4s["resolve"], a.chunk, a.lut)
    rep["q1_pass"] = rep["alignment"]["aligned"] and q1_verdict(rep["native"], rep["resolve"])
    rep["q1_note"] = Q1_NOTE
    rep["q3_pass"] = rep["native"]["q3"]["pass"]       # the Resolve clip's q3 is a control only
    if a.blind:
        rep["blind"] = make_blind(a.out, mp4s)
    with open(os.path.join(a.out, "report.json.part"), "w") as f:
        json.dump(json_safe(rep), f, indent=1)
    os.replace(os.path.join(a.out, "report.json.part"), os.path.join(a.out, "report.json"))
    print(json.dumps(json_safe(rep), indent=1))
    return 0 if rep["q1_pass"] and rep["q3_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
