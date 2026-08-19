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


def can_revise(row) -> tuple:
    """(bool, reason). MKV masters carry LOSSLESS audio (TrueHD/DTS-HD MA) and the pipeline
    deliberately never transcodes those — normalising one would mean throwing that away, so
    it is refused rather than silently degrading the track."""
    path = (row or {}).get("nas_path") or ""
    if not path:
        return False, "no published path recorded"
    if path.lower().endswith(".mkv"):
        return False, "lossless audio (MKV) — not re-encoded by design"
    return True, ""


def _mark(nas_path, **fields) -> None:
    with _LOCK:
        rows = _read()
        for r in rows:
            if r.get("nas_path") == nas_path:
                r.update(fields)
        _write(rows)


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
    try:
        target = settings.get_settings().get("audio_target_lufs", -16) or -16
        d = scratch_dir or scratch_mod.default_scratch()
        _mark(nas_path, revising_note="downloading")
        got, work, msg = transfer.download(nas_path, d)
        if not got:
            return {"status": "download-failed", "detail": msg}

        measured = remux.measure_lufs(work)
        gain = remux.boost_gain_db(measured, target)
        if gain <= 0:
            return {"status": "already-normalized",
                    "measured": measured, "target": target}

        stem, ext = os.path.splitext(os.path.basename(nas_path))
        fixed = os.path.join(d, stem + ".revised" + ext)
        cmd = [remux.FFMPEG, "-hide_banner", "-nostdin", "-y", "-i", work,
               "-map", "0", "-c", "copy",                 # keep video + subs bit-exact (DV intact)
               "-c:a", "aac_at", "-b:a", "384k",
               "-filter:a", remux.build_audio_boost_filter(gain), fixed]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode != 0 or not os.path.exists(fixed):
            return {"status": "remux-failed", "detail": (r.stderr or "")[-300:]}

        landed = remux.measure_lufs(fixed)
        if landed is None or abs(landed - float(target)) > 1.5:
            # Same rule the remux stage uses: a bad landing is not shipped.
            return {"status": "landing-off", "measured": measured, "landed": landed,
                    "target": target}

        # NEVER write straight over the master: upload() clears a different-sized file at the
        # target before STORing, so a transfer that died there would take the only copy with
        # it. Ship beside it under a distinct name, let upload() size-verify, and swap only
        # then — the sole exposed window is a delete+rename measured in milliseconds, and a
        # failure leaves the revised file plainly named on the NAS rather than a hole.
        _mark(nas_path, revising_note="uploading")
        up, revised_remote, umsg = transfer.upload(fixed, os.path.dirname(nas_path))
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
        for f in (work, fixed):
            try:
                if f and os.path.exists(f):
                    os.remove(f)
            except OSError:
                pass
        with _REVISE_LOCK:
            _revising.discard(nas_path)
