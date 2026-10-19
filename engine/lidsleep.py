"""Keep a MacBook awake with its lid CLOSED while the pipeline is activated on mains power
(user-dictated 2026-10-19: "make it so it doesn't make the mac sleep when the pipeline is
activated with power").

macOS sleeps a laptop the moment its lid closes unless an external display is attached, and no
app-level power assertion overrides that: caffeinate's assertions only stop IDLE sleep, and
IOPMLib's PreventSystemSleep assertion has been unsupported since 10.9. The one switch is
`pmset -a disablesleep 1`, and it needs root. So a one-time sudoers rule (RULE_FILE, written by
the user with install_command()) lets this user run exactly `pmset -a disablesleep 1` and
`pmset -a disablesleep 0` without a password, and nothing else.

A Mac that cannot sleep, shut in a bag, cooks. So:
  * it is held only while the pipeline is activated AND the Mac draws from AC, and only on a Mac
    with a lid (an internal battery; a desktop has none and caffeinate already covers it);
  * the engine releases it on deactivate and on a power pause (Orchestrator._stop_caffeinate);
  * a detached GUARD process (this file, --guard) polls every GUARD_POLL_SECONDS and releases it
    the moment the engine's process is gone or the Mac is on battery — a crash, a kill -9 or a
    pulled cable never leaves it set. A new engine also releases a dead engine's hold at start;
  * a release restores what was set before the hold (HOLD_FILE keeps it), so a user who turned
    sleep off themselves keeps it off.

CLI:  python3 engine/lidsleep.py --status | --install [--dry-run] | --release | --guard
"""
from __future__ import annotations
import fcntl
import json
import os
import re
import subprocess
import sys
import time

PMSET = "/usr/bin/pmset"
SUDO = "/usr/bin/sudo"
RULE_FILE = "/etc/sudoers.d/visionary-lidsleep"
HOLD_FILE = os.path.expanduser("~/.topaz-pipeline/lidsleep.json")
LOCK_FILE = os.path.expanduser("~/.topaz-pipeline/lidsleep.lock")
GUARD_POLL_SECONDS = 5.0
ALLOWED_CACHE_SECONDS = 300.0
_UNDER_TEST = "unittest" in sys.modules      # tests never touch sudo or pmset (they patch _run)
if _UNDER_TEST:                              # ...nor the live hold book
    import tempfile
    _TEST_DIR = tempfile.mkdtemp(prefix="visionary-lidsleep-")
    HOLD_FILE = os.path.join(_TEST_DIR, "lidsleep.json")
    LOCK_FILE = os.path.join(_TEST_DIR, "lidsleep.lock")
_allowed_at = [0.0, None]                    # (checked at, answer)


def _run(argv, timeout=10.0):
    """(returncode, stdout) — (None, "") when it could not run at all."""
    if _UNDER_TEST and _run is _RUN_REAL:
        return None, ""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return None, ""


_RUN_REAL = _run


def _batt() -> str:
    return _run([PMSET, "-g", "batt"])[1]


def has_lid(batt: str | None = None) -> bool:
    """A laptop: pmset lists an internal battery."""
    return "InternalBattery" in (_batt() if batt is None else batt)


def on_ac(batt: str | None = None) -> bool:
    """Drawing from the adapter ("Now drawing from 'AC Power'")."""
    return "'AC Power'" in (_batt() if batt is None else batt)


def sleep_disabled() -> bool:
    """Is `pmset disablesleep` on right now (pmset -g says "SleepDisabled 1")?"""
    return re.search(r"SleepDisabled\s+1\b", _run([PMSET, "-g"])[1]) is not None


def allowed(fresh: bool = False) -> bool:
    """May this user run `pmset -a disablesleep 1` without a password (the rule is in place)?
    Cached ALLOWED_CACHE_SECONDS: the answer only changes when the user installs the rule."""
    now = time.monotonic()
    if not fresh and _allowed_at[1] is not None and now - _allowed_at[0] < ALLOWED_CACHE_SECONDS:
        return _allowed_at[1]
    # `sudo -l` would say yes for any admin (whose sudo asks a password); the exact test is to
    # run it: set the switch to what it already is — a no-op — with no password allowed
    ok = _set(sleep_disabled())
    _allowed_at[0], _allowed_at[1] = now, ok
    return ok


def _set(on: bool) -> bool:
    rc, _out = _run([SUDO, "-n", PMSET, "-a", "disablesleep", "1" if on else "0"])
    return rc == 0


def rule_line(user: str | None = None) -> str:
    user = user or os.environ.get("USER") or "user"
    return f"{user} ALL=(root) NOPASSWD: {PMSET} -a disablesleep 1, {PMSET} -a disablesleep 0"


def install_command(user: str | None = None) -> str:
    """The ONE command the user runs. No quotes or parentheses to survive a copy-paste (the
    first version, an echo of the rule line, reached the user's shell as an empty file)."""
    import shlex
    return f"python3 {shlex.quote(os.path.abspath(__file__))} --install"


def install(dry_run: bool = False) -> int:
    """--install: write the rule to a private temp file, check it with visudo, and only then
    `sudo install` it into RULE_FILE (that one step asks for the user's password — a malformed
    sudoers file would lock sudo out, so nothing unchecked ever goes there). Then prove it: a
    password-free no-op set. -> exit code."""
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix="visionary-lidsleep-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(rule_line() + "\n")
        if subprocess.run(["/usr/sbin/visudo", "-cqf", tmp]).returncode != 0:
            print("the rule did not pass visudo's check — nothing was installed")
            return 1
        cmd = [SUDO, "/usr/bin/install", "-m", "0440", "-o", "root", "-g", "wheel", tmp, RULE_FILE]
        if dry_run:
            print("checked:", rule_line())
            print("would run:", " ".join(cmd))
            return 0
        print("Installing", RULE_FILE, "(sudo asks for your password once)")
        if subprocess.run(cmd).returncode != 0:
            print("not installed")
            return 1
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    ok = allowed(fresh=True)
    print("done — Visionary can now keep the Mac awake with the lid closed" if ok
          else "installed, but sudo still asks for a password — tell Claude")
    return 0 if ok else 1


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _read_hold():
    try:
        with open(HOLD_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


class _Lock:
    """One holder/releaser at a time ACROSS processes (the engine and its guard)."""

    def __enter__(self):
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
        self.f = open(LOCK_FILE, "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()
        return False


def _spawn_guard() -> None:
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--guard"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def hold(owner_pid: int | None = None) -> tuple:
    """Turn lid-close sleep off for as long as `owner_pid` (the engine) lives on AC. Idempotent.
    -> (held, why): why is "held", "no lid", "on battery", "needs permission" or "failed"."""
    owner = int(owner_pid or os.getpid())
    with _Lock():
        cur = _read_hold()
        if cur and cur.get("pid") == owner:
            return True, "held"
        batt = _batt()
        if not has_lid(batt):
            return False, "no lid"
        if not on_ac(batt):
            return False, "on battery"
        if not allowed():
            return False, "needs permission"
        # a dead engine's hold that nothing released: its "before" is the true one
        prior = bool(cur.get("prior")) if cur else sleep_disabled()
        if not _set(True):
            _allowed_at[1] = None                    # the rule may have gone: ask again next time
            return False, "failed"
        tmp = HOLD_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"pid": owner, "prior": prior, "at": int(time.time())}, f)
        os.replace(tmp, HOLD_FILE)
    _spawn_guard()
    return True, "held"


def release(only_pid: int | None = None) -> bool:
    """End the hold and restore what was set before it. `only_pid`: only if that process is
    still the holder (a guard must never release a newer engine's hold). -> True if released."""
    with _Lock():
        cur = _read_hold()
        if not cur:
            return False
        if only_pid is not None and cur.get("pid") != only_pid:
            return False
        try:
            os.remove(HOLD_FILE)
        except OSError:
            pass
        if not cur.get("prior"):
            _set(False)
        return True


def recover() -> bool:
    """At engine start: release a hold whose engine is gone (its guard died with it)."""
    cur = _read_hold()
    if cur and not _pid_alive(cur.get("pid")):
        return release(only_pid=cur.get("pid"))
    return False


def reconcile(want: bool, owner_pid: int | None = None) -> tuple:
    """Make the hold match `want` (activated and running). -> hold()'s answer, or
    (False, "released"/"off")."""
    if want:
        return hold(owner_pid)
    return (False, "released") if release(only_pid=int(owner_pid or os.getpid())) else (False, "off")


def guard() -> int:
    """The watchdog: release the moment the holder is gone or the Mac leaves AC; exit when the
    hold has been released normally."""
    while True:
        cur = _read_hold()
        if not cur:
            return 0
        pid = cur.get("pid")
        if not _pid_alive(pid) or not on_ac():
            release(only_pid=pid)
            return 0
        time.sleep(GUARD_POLL_SECONDS)


def status() -> dict:
    batt = _batt()
    return {"lid": has_lid(batt), "on_ac": on_ac(batt), "allowed": allowed(fresh=True),
            "sleep_disabled": sleep_disabled(), "hold": _read_hold()}


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--status"
    if arg == "--guard":
        sys.exit(guard())
    if arg == "--install":
        sys.exit(install(dry_run="--dry-run" in sys.argv))
    if arg == "--release":
        cur = _read_hold()
        print("released" if cur and release() else "nothing held")
        sys.exit(0)
    print(json.dumps(status(), indent=2))
