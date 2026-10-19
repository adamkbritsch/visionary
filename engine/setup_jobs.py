"""One-at-a-time installer jobs for the in-app Setup section.

Each target is a FIXED command string — the API's `what` only SELECTS one; nothing
user-supplied is ever interpolated into a shell. Output streams into a bounded ring
(the UI shows a live tail). One job at a time keeps the UI honest and matches brew's
own self-serialization.

Homebrew itself is never installed from here: its installer needs an admin password
and interactivity, so a missing brew is a REFUSAL carrying the copyable install
command (the row shows it copy-only).
"""
from __future__ import annotations
import collections
import json
import os
import subprocess
import threading
import time

BREW = "/opt/homebrew/bin/brew"
BREW_INSTALL_HINT = ('/bin/bash -c "$(curl -fsSL '
                     'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"')

TARGETS = {
    # Only the MISSING tools, and never an upgrade (HOMEBREW_NO_INSTALL_UPGRADE in _run): ffmpeg
    # and dovi_tool are pinned (versions.py) — an upgrade past them would fail preflight's
    # native_dv row, which is part of the arm gate.
    "brew_tools": {"cmd": f'for f in ffmpeg x265 dovi_tool gpac; do {BREW} list --versions "$f" '
                          f'>/dev/null 2>&1 || {BREW} install "$f"; done',
                   "needs_brew": True},
    # the bundled Dolby Vision toolchain files (dv/): argv computed by the caller (engine/
    # dvinstall.py --repair with the bundle's Resources dir) — copies the pinned analyzer and LUT
    "dv_toolchain": {"cmd": None},
    "python_deps": {"cmd": "/usr/bin/python3 -m pip install --user opencv-python || "
                           "(/usr/bin/python3 -m ensurepip --user && "
                           "/usr/bin/python3 -m pip install --user opencv-python)"},
    # Border-extender model downloads (the extend stage): argv computed by the caller
    # from borders.model_download_argv (curl -fL -C - into Comfy Desktop's models dir —
    # fixed URL + engine-computed dest, nothing user-supplied).
    "borders_vace": {"cmd": None},
    "borders_umt5": {"cmd": None},
    "borders_causvid": {"cmd": None},
    "borders_vae": {"cmd": None},
}

TAIL_LINES = 200

_LOCK = threading.Lock()
_JOBS: dict = {}          # what -> job dict (latest attempt only)
_ACTIVE: list = [None]    # the running `what`, if any


def start(what: str, argv: list | None = None) -> dict:
    """Kick a job. Refusals: unknown target, another job running, brew missing for a
    brew-backed target. Returns the job snapshot (state=running) or {"error": ...}."""
    t = TARGETS.get(what)
    if t is None:
        return {"error": "unknown", "detail": f"no such install target {what!r}"}
    if t.get("needs_brew") and not os.path.exists(BREW):
        return {"error": "homebrew-missing", "hint": BREW_INSTALL_HINT}
    if t["cmd"] is None and not argv:
        return {"error": "argv-required", "detail": f"{what} needs an argv"}
    with _LOCK:
        if _ACTIVE[0] is not None:
            return {"error": "busy", "active": _ACTIVE[0]}
        job = {"what": what, "state": "running", "rc": None,
               "started": time.time(), "finished": None,
               "tail": collections.deque(maxlen=TAIL_LINES)}
        _JOBS[what] = job
        _ACTIVE[0] = what
    threading.Thread(target=_run, args=(job, t["cmd"], argv), daemon=True,
                     name=f"setup-job-{what}").start()
    return _snapshot(job)


def _run(job, cmd, argv):
    try:
        # brew's auto-update makes a 5-second install take minutes — skip it
        env = {**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_INSTALL_UPGRADE": "1"}
        spec = argv if argv else ["/bin/sh", "-c", cmd]
        p = subprocess.Popen(spec, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, env=env)
        for line in p.stdout:
            job["tail"].append(line.rstrip("\n"))
        rc = p.wait()
        job["rc"] = rc
        job["state"] = "ok" if rc == 0 else "failed"
    except Exception as e:
        job["tail"].append(f"job error: {e}")
        job["state"] = "failed"
        job["rc"] = -1
    finally:
        job["finished"] = time.time()
        with _LOCK:
            _ACTIVE[0] = None


def status() -> dict:
    with _LOCK:
        return {"active": _ACTIVE[0],
                "jobs": {k: _snapshot(j) for k, j in _JOBS.items()}}


def _snapshot(job) -> dict:
    return {"what": job["what"], "state": job["state"], "rc": job["rc"],
            "started": job["started"], "finished": job["finished"],
            "tail": list(job["tail"])}
