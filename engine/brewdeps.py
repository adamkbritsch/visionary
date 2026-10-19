"""Install Visionary's Homebrew dependencies at the versions it is pinned to — Settings → Setup →
Command-line tools → Install (setup_jobs "brew_tools"), or `python3 engine/brewdeps.py` from a clone.

  x265, mkvtoolnix   whatever Homebrew has (mkvmerge has a floor, not a pin: preflight checks it)
  ffmpeg             the engine runs /opt/homebrew/bin/ffmpeg and needs major 8 (versions.
                     NATIVE_FFMPEG_MAJOR). An ffmpeg 8 already there is KEPT, untouched (a pinned
                     `ffmpeg` formula at 8.x). Otherwise the keg-only `ffmpeg@8` formula is installed
                     and linked into /opt/homebrew — a newer `ffmpeg` formula's links are removed
                     first (it stays installed; anything built against it keeps its own libraries
                     at /opt/homebrew/opt/ffmpeg). The pinned analyzer then finds ffmpeg@8's
                     libraries through dvnative.analyzer_argv.
  dovi_tool          NOT from Homebrew, which has moved past 2.3.0: the bundled 2.3.0 (dvinstall).

Only what is missing is installed, and nothing is upgraded (setup_jobs runs this with
HOMEBREW_NO_INSTALL_UPGRADE / HOMEBREW_NO_AUTO_UPDATE). Prints each step; exit 0 when ffmpeg is
major 8 at the end, else 1.
"""
from __future__ import annotations
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import versions  # noqa: E402

BREW = "/opt/homebrew/bin/brew"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFMPEG8 = f"ffmpeg@{versions.NATIVE_FFMPEG_MAJOR}"
ALWAYS = ("x265", "mkvtoolnix")


def ffmpeg_major(path: str = FFMPEG):
    """The major version `ffmpeg -version` reports at `path`, or None."""
    try:
        r = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"\s*ffmpeg version n?(\d+)\.", r.stdout or "")
    return int(m.group(1)) if r.returncode == 0 and m else None


def installed(formula: str, brew: str = BREW) -> bool:
    try:
        return subprocess.run([brew, "list", "--versions", formula], capture_output=True,
                              timeout=120).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def links_ffmpeg(path: str = FFMPEG) -> bool:
    """/opt/homebrew/bin/ffmpeg is the unversioned `ffmpeg` formula's link (…/Cellar/ffmpeg/…)."""
    try:
        return "/Cellar/ffmpeg/" in os.path.realpath(path)
    except OSError:
        return False


def plan(state: dict) -> list:
    """PURE. The brew commands (argument lists, without `brew`) for `state`:
    {"installed": {formula: bool}, "ffmpeg_major": int|None, "ffmpeg_linked": bool} where
    ffmpeg_linked = /opt/homebrew/bin/ffmpeg belongs to the unversioned `ffmpeg` formula."""
    have = state.get("installed") or {}
    steps = [["install", f] for f in ALWAYS if not have.get(f)]
    if state.get("ffmpeg_major") != versions.NATIVE_FFMPEG_MAJOR:
        if not have.get(FFMPEG8):
            steps.append(["install", FFMPEG8])
        if state.get("ffmpeg_linked"):
            steps.append(["unlink", "ffmpeg"])
        steps.append(["link", "--force", FFMPEG8])
    return steps


def current_state() -> dict:
    return {"installed": {f: installed(f) for f in ALWAYS + (FFMPEG8,)},
            "ffmpeg_major": ffmpeg_major(), "ffmpeg_linked": links_ffmpeg()}


def main() -> int:
    if not os.path.exists(BREW):
        print(f"Homebrew is not installed ({BREW} is missing)")
        return 1
    state = current_state()
    if state["ffmpeg_major"] == versions.NATIVE_FFMPEG_MAJOR:
        print(f"ffmpeg {state['ffmpeg_major']} at {FFMPEG}: kept as it is")
    for step in plan(state):
        print("$ brew " + " ".join(step), flush=True)
        rc = subprocess.run([BREW] + step).returncode
        if rc != 0:
            print(f"brew {' '.join(step)} failed (exit {rc})")
            return 1
    got = ffmpeg_major()
    if got != versions.NATIVE_FFMPEG_MAJOR:
        print(f"{FFMPEG} is ffmpeg {got}, not {versions.NATIVE_FFMPEG_MAJOR}")
        return 1
    print(f"ffmpeg {got}, x265 and mkvtoolnix are ready; dovi_tool {versions.DOVI_TOOL_VERSION} "
          f"comes with the Dolby Vision toolchain (dv/)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
