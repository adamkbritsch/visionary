"""One-at-a-time installer jobs for the in-app Setup section.

Each target is a FIXED command string — the API's `what` only SELECTS one; nothing
user-supplied is ever interpolated into a shell. Output streams into a bounded ring
(the UI shows a live tail). One job at a time keeps the UI honest and matches brew's
own self-serialization.

Homebrew itself is never installed from here: its installer needs an admin password
and interactivity, so a missing brew is a REFUSAL carrying the copyable install
command (the row shows it copy-only).

`import_resolve` is the odd one out — argv computed by the caller (the bundled
setup/import_resolve.py, run as a subprocess so fusionscript can never wedge the
server) — and its trailing --json payload is parsed into `steps` for the UI.
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
    "brew_tools": {"cmd": f"{BREW} install ffmpeg x265 dovi_tool gpac cliclick",
                   "needs_brew": True},
    "sublercli": {"cmd": f"{BREW} install --cask sublercli", "needs_brew": True},
    "rosetta": {"cmd": "/usr/sbin/softwareupdate --install-rosetta --agree-to-license"},
    "python_deps": {"cmd": "/usr/bin/python3 -m pip install --user opencv-python || "
                           "(/usr/bin/python3 -m ensurepip --user && "
                           "/usr/bin/python3 -m pip install --user opencv-python)"},
    "import_resolve": {"cmd": None},       # argv supplied by the caller
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
               "tail": collections.deque(maxlen=TAIL_LINES), "steps": None}
        _JOBS[what] = job
        _ACTIVE[0] = what
    threading.Thread(target=_run, args=(job, t["cmd"], argv), daemon=True,
                     name=f"setup-job-{what}").start()
    return _snapshot(job)


def _run(job, cmd, argv):
    try:
        # brew's auto-update makes a 5-second install take minutes — skip it
        env = {**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1"}
        spec = argv if argv else ["/bin/sh", "-c", cmd]
        p = subprocess.Popen(spec, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, env=env)
        for line in p.stdout:
            job["tail"].append(line.rstrip("\n"))
        rc = p.wait()
        job["rc"] = rc
        job["state"] = "ok" if rc == 0 else "failed"
        if job["what"] == "import_resolve":
            job["steps"] = _parse_steps(job["tail"])
            if job["steps"] is not None and not all(s.get("ok") for s in job["steps"]):
                job["state"] = "failed"      # --json exits 0 even when a step failed
    except Exception as e:
        job["tail"].append(f"job error: {e}")
        job["state"] = "failed"
        job["rc"] = -1
    finally:
        job["finished"] = time.time()
        with _LOCK:
            _ACTIVE[0] = None


def _parse_steps(tail) -> list | None:
    """import_resolve --json prints one JSON object at the end — fish it out of the
    output tail (Resolve chatter can precede it)."""
    buf = "\n".join(tail)
    i = buf.rfind('{"ok"')
    if i < 0:
        i = buf.rfind("{")
    if i < 0:
        return None
    try:
        return (json.loads(buf[i:]) or {}).get("steps")
    except ValueError:
        return None


def status() -> dict:
    with _LOCK:
        return {"active": _ACTIVE[0],
                "jobs": {k: _snapshot(j) for k, j in _JOBS.items()}}


def _snapshot(job) -> dict:
    return {"what": job["what"], "state": job["state"], "rc": job["rc"],
            "started": job["started"], "finished": job["finished"],
            "tail": list(job["tail"]), "steps": job["steps"]}
