"""Automatic run log — captures every stage result, failure, and exception so a
stuck/failed unattended run leaves a trail you can read after the fact.

Writes to ~/.topaz-pipeline/logs/upscaler.log (rotating). `event()` for normal
progress, `failure()` for a stage that returned not-ok, `exception()` for an
uncaught error (with traceback). `tail()` feeds the dashboard's recent-issues view.
"""
from __future__ import annotations
import datetime
import os
import threading
import traceback

LOG_DIR = os.path.expanduser("~/.topaz-pipeline/logs")
LOG_FILE = os.path.join(LOG_DIR, "upscaler.log")
MAX_BYTES = 3_000_000
_LOCK = threading.Lock()


def _write(level: str, msg: str) -> None:
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {level:5} {msg}\n"
    try:
        with _LOCK:
            os.makedirs(LOG_DIR, exist_ok=True)
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_BYTES:
                os.replace(LOG_FILE, LOG_FILE + ".1")   # one-generation rotate
            with open(LOG_FILE, "a") as f:
                f.write(line)
    except OSError:
        pass


def _oneline(msg: str) -> str:
    """Collapse whitespace/newlines so a stage message is ONE log line. A multi-line msg
    (e.g. an ffmpeg stderr tail) otherwise writes continuation lines with no timestamp/
    level — corrupting rotation, the tail() level filter, and readability."""
    return " ".join(str(msg).split())


def event(msg: str) -> None:
    _write("INFO", _oneline(msg))


def failure(msg: str) -> None:
    _write("FAIL", _oneline(msg))


def exception(where: str, exc: BaseException) -> None:
    _write("ERROR", f"{where}: {exc.__class__.__name__}: {exc}\n" + traceback.format_exc().rstrip())


def tail(n: int = 40, levels=("FAIL", "ERROR")) -> list:
    """Last `n` log lines, optionally filtered to problem levels (for the UI)."""
    try:
        with open(LOG_FILE) as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    if levels:
        lines = [ln for ln in lines if any(f" {lv} " in ln[:30] for lv in levels)]
    return lines[-n:]
