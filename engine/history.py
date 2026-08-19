"""What Visionary has finished, and the ability to send one back through for its audio.

The pipeline kept no record of completed work: the log carries an "upload" line but not the
published path, so nothing could be pointed at afterwards. This book records each verified
upload with the one thing a later fix needs — where the master actually landed.

REVISING AUDIO IS IN PLACE, not a re-run. By the time an item finishes, its source is
usually gone (replace_source deletes the superseded original once the master verifies, and a
YouTube staging folder is purged at cleanup), so there is nothing left to re-process. The
master itself is the only copy — so a revision re-measures ITS loudness, applies the gain to
the audio track alone, and puts it back. Video and subtitles are stream-copied, which keeps
Dolby Vision intact.
"""
from __future__ import annotations
import json
import os
import subprocess
import threading
import time

import logbook

BOOK_FILE = os.path.expanduser("~/.topaz-pipeline/history.json")
MAX_ENTRIES = 200
_LOCK = threading.Lock()

# One revision at a time, and never a second one for the same entry.
_REVISE_LOCK = threading.Lock()
_revising = set()


def _read() -> list:
    try:
        with open(BOOK_FILE) as f:
            v = json.load(f)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _write(rows) -> None:
    try:
        os.makedirs(os.path.dirname(BOOK_FILE), exist_ok=True)
        tmp = BOOK_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rows[-MAX_ENTRIES:], f, indent=1)
        os.replace(tmp, BOOK_FILE)
    except OSError:
        pass


def entry_id(nas_path: str) -> str:
    """Stable id for a published master — its path. The UI round-trips this, so it must not
    depend on position in the book."""
    return str(nas_path or "")


def record(*, nas_path, kind, title, series="", ep="", gain=None, note="") -> None:
    """Called once a master is verified on the NAS. Newest last; the book self-trims."""
    if not nas_path:
        return
    with _LOCK:
        rows = [r for r in _read() if r.get("nas_path") != nas_path]
        rows.append({"nas_path": nas_path, "kind": kind, "title": title, "series": series,
                     "ep": ep, "gain": gain, "note": note, "at": int(time.time()),
                     "revised": 0})
        _write(rows)


def view(limit: int = 60) -> list:
    """Newest first, with the live revision state folded in so the UI can disable a row."""
    rows = list(reversed(_read()))[:max(1, int(limit))]
    for r in rows:
        r["revising"] = r.get("nas_path") in _revising
        r["can_revise"] = can_revise(r)[0]
        r["why"] = can_revise(r)[1]
    return rows


# The pipeline never transcodes LOSSLESS audio, so a revision must not either — re-encoding
# TrueHD or DTS-HD MA to AAC to make it louder would throw away the thing that made it worth
# keeping. Judged by the actual codec, NOT by the container: an MKV routinely carries lossy
# AAC (live-caught 2026-08-18 — a 5.1 AAC master was refused purely for being .mkv, and it
# was exactly the file whose audio needed fixing).
LOSSLESS = ("truehd", "mlp", "flac", "alac")


def is_lossless(codec: str, profile: str = "") -> bool:
    c, p = (codec or "").lower(), (profile or "").lower()
    if any(c.startswith(x) for x in LOSSLESS) or c.startswith("pcm"):
        return True
    return c.startswith("dts") and ("ma" in p or "lossless" in p)   # DTS-HD MA, not DTS core


def can_revise(row) -> tuple:
    """(bool, reason). Unknown audio is allowed through: the revision downloads the file
    anyway and re-checks there, which is authoritative — refusing on a guess is what went
    wrong before."""
    row = row or {}
    if not row.get("nas_path"):
        return False, "no published path recorded"
    if is_lossless(row.get("audio") or "", row.get("audio_profile") or ""):
        return False, "lossless audio — never re-encoded"
    return True, ""


def probe_audio(nas_path, *, bytes_=12_000_000) -> tuple:
    """(codec, profile) of the master's first audio track, from a HEAD download — the stream
    headers live at the front of both MP4 and MKV, so this costs a few MB rather than the
    whole file. ('', '') when it can't be determined."""
    import subprocess
    import tempfile
    import transfer
    tmp = os.path.join(tempfile.gettempdir(), "_vis_probe" + os.path.splitext(nas_path)[1])
    try:
        ok, _msg = transfer.download_head(nas_path, tmp, bytes_, timeout=60)
        if not ok:
            return "", ""
        out = subprocess.run(["/opt/homebrew/bin/ffprobe", "-v", "error", "-select_streams",
                              "a:0", "-show_entries", "stream=codec_name,profile",
                              "-of", "csv=p=0", tmp],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        parts = (out.splitlines() or [""])[0].split(",")
        return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")
    except Exception:
        return "", ""
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _mark(nas_path, **fields) -> None:
    with _LOCK:
        rows = _read()
        for r in rows:
            if r.get("nas_path") == nas_path:
                r.update(fields)
        _write(rows)


def _duration(path) -> float:
    """Seconds, for turning ffmpeg's out_time into a percentage. 0 when unknown — the caller
    then reports no number at all rather than a fabricated one."""
    import subprocess as sp
    try:
        out = sp.run(["/opt/homebrew/bin/ffprobe", "-v", "error", "-show_entries",
                      "format=duration", "-of", "csv=p=0", path],
                     capture_output=True, text=True, timeout=60).stdout.strip()
        return float(out or 0)
    except Exception:
        return 0.0


def _run_with_progress(cmd, total_secs, on_pct):
    """Run ffmpeg and report REAL progress from its -progress stream.

    This pass is the long pole — a full audio re-encode across a two-hour film — and it used
    to report nothing at all, so the bar sat frozen for minutes while work was happening. A
    bar that cannot move should not be drawn; the fix is to make it move.
    Returns (returncode, stderr_tail).
    """
    import subprocess as sp
    p = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.PIPE, text=True, bufsize=1)
    last = -1.0
    try:
        for line in p.stdout:
            if not line.startswith("out_time_ms=") or total_secs <= 0:
                continue
            try:
                secs = int(line.split("=", 1)[1].strip()) / 1_000_000.0
            except ValueError:
                continue
            pct = max(0.0, min(99.0, secs / total_secs * 100))
            if pct - last >= 0.5:                 # don't spam the state dict every frame
                last = pct
                on_pct(round(pct, 1))
    finally:
        p.wait()
        tail = (p.stderr.read() or "") if p.stderr else ""
    return p.returncode, tail


def _probe_local_audio(path) -> tuple:
    import subprocess
    try:
        out = subprocess.run(["/opt/homebrew/bin/ffprobe", "-v", "error", "-select_streams",
                              "a:0", "-show_entries", "stream=codec_name,profile",
                              "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=120).stdout.strip()
        parts = (out.splitlines() or [""])[0].split(",")
        return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")
    except Exception:
        return "", ""


def adopt(nas_path: str, *, kind="", title="", probe=True) -> dict:
    """Put an ALREADY-PUBLISHED master into the book. History only records what the pipeline
    finishes from now on, so everything upscaled before it existed was unreachable —
    including, inevitably, the file whose audio you actually want to fix."""
    if not nas_path:
        return {"status": "no-path"}
    base = os.path.basename(nas_path)
    codec, profile = probe_audio(nas_path) if probe else ("", "")
    record(nas_path=nas_path, kind=(kind or _kind_of(nas_path)),
           title=(title or os.path.splitext(base)[0]), note="adopted")
    _mark(nas_path, audio=codec, audio_profile=profile)
    return {"status": "ok", "nas_path": nas_path, "audio": codec}


# ONLY the pipeline's own output. orchestrator.DV_TAG / SDR_TAG are the full tags it appends
# to everything it produces. series.MASTER_MARKS is the SHORT form ("hdr10 dv") and is right
# where it is used — inside one show's folder, against that show's own files — but it is
# catastrophic across whole libraries: every natively-Dolby-Vision release carries "HDR10 DV"
# in its name. Scanning with the short mark adopted 200 untouched UHD remuxes, filled the
# book to its cap, and evicted the real master someone had asked for (live-hit 2026-08-18).
OUR_TAGS = ("hdr10 dv upscaled", "sdr upscaled")


def is_our_master(name: str) -> bool:
    n = (name or "").lower()
    return any(t in n for t in OUR_TAGS)


def _kind_of(nas_path: str) -> str:
    p = (nas_path or "").lower()
    if "/youtube" in p:
        return "youtube"
    return "episode" if "tv-show" in p or "/tv" in p else "movie"


def scan(limit: int = 400) -> dict:
    """Find masters the pipeline has already published and adopt any that are missing.

    Detection is the pipeline's own OUTPUT TAG in full — see is_our_master. Audio is NOT
    probed here: that is a few MB per file and this walks whole libraries; the revision
    re-checks authoritatively anyway.
    """
    import ftplib
    import transfer
    known = {r.get("nas_path") for r in _read()}
    found, added = 0, 0
    try:
        ftp = transfer.connect(timeout=30)
    except Exception as e:
        return {"status": "unreachable", "detail": str(e)}
    try:
        roots = list(transfer.NAS_FTP_MOVIES_ROOTS) + list(transfer.NAS_FTP_TV_ROOTS)
        for root in roots:
            stack = [root]
            while stack and found < limit:
                d = stack.pop()
                try:
                    entries = transfer.ftp_listdir(ftp, d)
                except ftplib.all_errors:
                    continue
                for name in entries:
                    full = d.rstrip("/") + "/" + name
                    if is_our_master(name):
                        found += 1
                        if full not in known:
                            record(nas_path=full, kind=_kind_of(full),
                                   title=os.path.splitext(name)[0], note="found on the NAS")
                            added += 1
                    elif "." not in name:                  # a directory (show / movie folder)
                        stack.append(full)
    finally:
        try: ftp.quit()
        except Exception: pass
    return {"status": "ok", "found": found, "added": added}


def _publish(info) -> None:
    """Put the running revision on the PIPELINE surface, shaped exactly like a finisher lane
    ({ep, stage, pct, step}). Both front ends already render those lanes, so a revision shows
    up where the work shows up instead of only as a badge in a list — and `None` clears it.
    Best-effort: a display failure must never affect the revision itself."""
    try:
        import orchestrator
        orchestrator.ORCH.state["revising"] = info
    except Exception:
        pass


def _step(title, stage, *, pct=None, step=None, kind="movie") -> None:
    _publish({"ep": title, "stage": stage, "pct": pct, "step": step,
              "movie": kind == "movie", "youtube": kind == "youtube", "revise": True})


def _swap_in(revised_remote: str, original_remote: str) -> tuple:
    """Put the revised file in the master's place: verify it is on the NAS at the expected
    size, then delete the original and rename over it. Verification first, because the
    original is the only copy of hours of work."""
    import ftplib
    import transfer
    try:
        ftp = transfer.connect()
    except Exception as e:
        return False, f"FTP connect failed: {e}"
    try:
        if transfer.remote_size(ftp, revised_remote) is None:
            return False, "revised file not found on the NAS"
        try:
            ftp.delete(original_remote)
        except ftplib.all_errors:
            pass                                  # already gone is fine; the rename still lands
        ftp.rename(revised_remote, original_remote)
        return True, "swapped in"
    except Exception as e:
        return False, f"swap failed: {e}"
    finally:
        try: ftp.quit()
        except Exception: pass


def revise_audio(nas_path: str, *, scratch_dir=None) -> dict:
    """Re-measure the published master's loudness and re-apply the boost IN PLACE.

    Audio only: the video and subtitle streams are stream-copied, so Dolby Vision survives
    untouched and this costs minutes rather than the hours a re-run would. Returns a
    JSON-able status; runs on the caller's thread (the server hands it to a daemon).
    """
    import remux
    import scratch as scratch_mod
    import settings
    import transfer

    row = next((r for r in _read() if r.get("nas_path") == nas_path), None)
    if not row:
        return {"status": "unknown-item"}
    ok, why = can_revise(row)
    if not ok:
        return {"status": "refused", "detail": why}
    with _REVISE_LOCK:
        if nas_path in _revising:
            return {"status": "already-running"}
        _revising.add(nas_path)
    work = ""
    fixed = ""
    label = row.get("title") or os.path.basename(nas_path)
    kind = row.get("kind") or "movie"
    try:
        target = settings.get_settings().get("audio_target_lufs", -16) or -16
        d = scratch_dir or scratch_mod.default_scratch()
        _mark(nas_path, revising_note="downloading")
        _step(label, "download", pct=0, step="fetching the master", kind=kind)
        got, work, msg = transfer.download(
            nas_path, d,
            on_progress=lambda done, total: _step(
                label, "download", pct=(round(done / total * 100, 1) if total else None),
                step="fetching the master", kind=kind))
        if not got:
            return {"status": "download-failed", "detail": msg}

        # AUTHORITATIVE lossless check — the file is here now, so stop guessing from the
        # name. Refusing here costs a download; shipping a transcoded lossless track would
        # cost the track.
        _step(label, "remux", step="checking the audio track", kind=kind)
        codec, profile = _probe_local_audio(work)
        if is_lossless(codec, profile):
            _mark(nas_path, audio=codec, audio_profile=profile)
            return {"status": "refused", "detail": f"lossless audio ({codec}) — never re-encoded"}
        _mark(nas_path, audio=codec, audio_profile=profile)

        _step(label, "remux", step="measuring loudness", kind=kind)
        measured = remux.measure_lufs(work)
        gain = remux.boost_gain_db(measured, target)
        if gain <= 0:
            return {"status": "already-normalized",
                    "measured": measured, "target": target}

        stem, ext = os.path.splitext(os.path.basename(nas_path))
        fixed = os.path.join(d, stem + ".revised" + ext)
        cmd = [remux.FFMPEG, "-hide_banner", "-nostdin", "-y",
               "-progress", "pipe:1", "-nostats", "-i", work,
               "-map", "0", "-c", "copy",                 # keep video + subs bit-exact (DV intact)
               "-c:a", "aac_at", "-b:a", "384k",
               "-filter:a", remux.build_audio_boost_filter(gain), fixed]
        _step(label, "remux", pct=0, step=f"applying +{gain:.1f} dB", kind=kind)
        rc, tail = _run_with_progress(
            cmd, _duration(work),
            lambda pct: _step(label, "remux", pct=pct, step=f"applying +{gain:.1f} dB", kind=kind))
        if rc != 0 or not os.path.exists(fixed):
            return {"status": "remux-failed", "detail": tail[-300:]}

        _step(label, "remux", step="verifying the new level", kind=kind)
        landed = remux.measure_lufs(fixed)
        if not remux.landing_ok(landed, float(target), gain, measured=measured):
            # Same rule the remux stage uses: a bad landing is not shipped.
            return {"status": "landing-off", "measured": measured, "landed": landed,
                    "target": target}

        # NEVER write straight over the master: upload() clears a different-sized file at the
        # target before STORing, so a transfer that died there would take the only copy with
        # it. Ship beside it under a distinct name, let upload() size-verify, and swap only
        # then — the sole exposed window is a delete+rename measured in milliseconds, and a
        # failure leaves the revised file plainly named on the NAS rather than a hole.
        _mark(nas_path, revising_note="uploading")
        _step(label, "upload", pct=0, step="putting it back", kind=kind)
        up, revised_remote, umsg = transfer.upload(
            fixed, os.path.dirname(nas_path),
            on_progress=lambda done, total: _step(
                label, "upload", pct=(round(done / total * 100, 1) if total else None),
                step="putting it back", kind=kind))
        if not up:
            return {"status": "upload-failed", "detail": umsg}
        sok, smsg = _swap_in(revised_remote, nas_path)
        if not sok:
            return {"status": "swap-failed", "detail": smsg, "revised_at": revised_remote}
        _mark(nas_path, gain=gain, note=f"audio revised: {measured:.1f} → {landed:.1f} LUFS",
              revised=int(time.time()), revising_note="")
        logbook.event(f"audio revised: {row.get('title') or nas_path} "
                      f"{measured:.1f} → {landed:.1f} LUFS (+{gain:.1f} dB)")
        return {"status": "ok", "gain": gain, "measured": measured, "landed": landed}
    except Exception as e:
        logbook.exception("revise_audio", e)
        return {"status": "error", "detail": f"{e.__class__.__name__}: {e}"}
    finally:
        _publish(None)                      # off the pipeline surface, whatever happened
        for f in (work, fixed):
            try:
                if f and os.path.exists(f):
                    os.remove(f)
            except OSError:
                pass
        with _REVISE_LOCK:
            _revising.discard(nas_path)
