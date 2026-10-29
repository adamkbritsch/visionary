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
    # engine/brewdeps.py (argv computed by the caller): only the MISSING tools, never an upgrade
    # (HOMEBREW_NO_INSTALL_UPGRADE in _run) — x265, mkvtoolnix, and ffmpeg 8 (an installed 8 is
    # kept; otherwise the ffmpeg@8 formula, linked). dovi_tool is not Homebrew's: dv_toolchain.
    "brew_tools": {"cmd": None, "needs_brew": True},
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
    # The optional helper (Settings > Setup > Helper): argv computed by the caller from the SAVED
    # helper settings (engine/helper.py setup_job_argv — host, user and key judged first, each
    # passed as its own argv word): a new ssh key (never over an existing one), the worker
    # install (helper_worker/install.sh), and x265 built ON the helper to this Mac's exact
    # version (helper_worker/build-x265.sh over ssh).
    "helper_key": {"cmd": None},
    "helper_worker": {"cmd": None},
    "helper_x265": {"cmd": None},
}

TAIL_LINES = 200

_LOCK = threading.Lock()
_JOBS: dict = {}          # what -> job dict (latest attempt only)
_ACTIVE: list = [None]    # the running `what`, if any


def start(what: str, argv: list | None = None, on_done=None) -> dict:
    """Kick a job. Refusals: unknown target, another job running, brew missing for a
    brew-backed target. Returns the job snapshot (state=running) or {"error": ...}.
    `on_done()` (optional) runs once the job has finished, whatever its result."""
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
    threading.Thread(target=_run, args=(job, t["cmd"], argv, on_done), daemon=True,
                     name=f"setup-job-{what}").start()
    return _snapshot(job)


def _run(job, cmd, argv, on_done=None):
    try:
        # brew's auto-update makes a 5-second install take minutes — skip it
        env = {**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_INSTALL_UPGRADE": "1"}
        spec = argv if argv else ["/bin/sh", "-c", cmd]
        # stdin is /dev/null: no job can ever sit waiting on a prompt nobody sees (ssh-keygen's
        # "Overwrite?", a script's `read`) — it gets end-of-input and says why in its tail
        p = subprocess.Popen(spec, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, env=env)
        with p.stdout:
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
        if on_done is not None:
            try:
                on_done()
            except Exception:  # noqa: BLE001 — a follow-up never fails the job
                pass


def status() -> dict:
    with _LOCK:
        return {"active": _ACTIVE[0],
                "jobs": {k: _snapshot(j) for k, j in _JOBS.items()}}


def _snapshot(job) -> dict:
    return {"what": job["what"], "state": job["state"], "rc": job["rc"],
            "started": job["started"], "finished": job["finished"],
            "tail": list(job["tail"])}
