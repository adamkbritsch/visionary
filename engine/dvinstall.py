"""Install the bundled Dolby Vision toolchain files to the paths the engine runs them from.

The repo's dv/ folder (Contents/Resources/dv in the app) carries the pinned files the native
Dolby Vision step needs and Homebrew cannot provide: the hdr_analyzer_mvp build (tinof/
hdr-analyze at versions.HDR_ANALYZER_COMMIT, MIT — dv/LICENSE.hdr-analyze), the SDR->PQ LUT, and
dovi_tool 2.3.0 (quietvoid/dovi_tool's release build, MIT — dv/LICENSE.dovi_tool).
The engine runs them from versions.HDR_ANALYZER_PATH and versions.NATIVE_LUT_PATH, and
preflight's native_dv row checks both by sha256.

  ensure(resources)   copy each file that is MISSING at its path; never touches one that is there
                      (the server calls it at start, so a fresh install needs no step at all)
  repair(resources)   also replace a file whose sha256 is not the pin, keeping the old one beside
                      it as <name>.replaced-<YYYYmmdd-HHMMSS> (Settings → Setup's Install button)

dovi_tool (dv/dovi_tool: quietvoid's own 2.3.0 release build, versions.DOVI_TOOL_BUNDLE_SHA256) is
judged by VERSION, not by hash: any dovi_tool at versions.DOVI_TOOL_PATH that reports
versions.DOVI_TOOL_VERSION is the pinned tool and is never touched (a brew-built 2.3.0 is). ensure()
installs the bundled one where there is none; repair() also replaces one of another version (a
Homebrew dovi_tool past 2.3.0), keeping it beside as for the others. /opt/homebrew/bin must exist.

A bundled file whose own sha256 is not the pin is never copied: a damaged bundle must not
overwrite anything. Each call returns one line per file saying what it did.

CLI: python3 engine/dvinstall.py [--repair] [RESOURCES_DIR]   (default: the repo root)
"""
from __future__ import annotations
import hashlib
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import versions  # noqa: E402

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RESOURCES = os.path.dirname(ENGINE_DIR)      # the repo root, or the app's Resources
BUNDLE_DIR = "dv"

# (bundled name, installed path, pinned sha256, mode)
FILES = (
    ("hdr_analyzer_mvp", versions.HDR_ANALYZER_PATH, versions.HDR_ANALYZER_SHA256, 0o755),
    ("rcm_sdr709_to_pq1000.cube", versions.NATIVE_LUT_PATH, versions.NATIVE_LUT_SHA256, 0o644),
)


def _sha256(path: str):
    """sha256 of a file, or None when it is missing or unreadable."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def _install(resources: str, replace_mismatched: bool) -> list:
    out = []
    for name, dest, pin, mode in FILES:
        src = os.path.join(resources, BUNDLE_DIR, name)
        have = _sha256(dest)
        if have == pin:
            out.append(f"{name}: already installed (pinned)")
            continue
        if have is not None and not replace_mismatched:
            out.append(f"{name}: left as it is — {dest} is not the pinned file (repair replaces it)")
            continue
        if _sha256(src) != pin:
            out.append(f"{name}: NOT installed — the bundled copy ({src}) is missing or not the "
                       f"pinned file")
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if have is not None:
            kept = f"{dest}.replaced-{time.strftime('%Y%m%d-%H%M%S')}"
            os.replace(dest, kept)
            out.append(f"{name}: the old file is kept as {kept}")
        tmp = dest + ".installing"
        shutil.copyfile(src, tmp)
        os.chmod(tmp, mode)
        if _sha256(tmp) != pin:                 # the copy itself must be the pinned file
            os.remove(tmp)
            out.append(f"{name}: NOT installed — the copy did not verify")
            continue
        os.replace(tmp, dest)
        out.append(f"{name}: installed to {dest}")
    return out


def dovi_version(path: str):
    """The version `path --version` reports ('2.3.0'), or None when it is missing or will not run."""
    import re
    import subprocess
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"dovi_tool\s+v?(\d[\w.+-]*)", (r.stdout or "") + (r.stderr or ""))
    return m.group(1) if r.returncode == 0 and m else None


def _install_dovi(resources: str, replace_mismatched: bool, dest: str = None) -> list:
    if dest is None and "unittest" in sys.modules:
        return ["dovi_tool: not touched under a test"]   # a test never replaces the real one
    dest = dest or versions.DOVI_TOOL_PATH
    want = versions.DOVI_TOOL_VERSION
    src = os.path.join(resources, BUNDLE_DIR, "dovi_tool")
    have = dovi_version(dest)
    if have == want:
        return [f"dovi_tool: {want} already at {dest}"]
    present = os.path.lexists(dest)
    if present and not replace_mismatched:
        return [f"dovi_tool: left as it is — {dest} is {have or 'not runnable'}, the pin is {want} "
                f"(repair replaces it)"]
    if not os.path.isdir(os.path.dirname(dest)):
        return [f"dovi_tool: NOT installed — {os.path.dirname(dest)} does not exist (install "
                f"Homebrew first)"]
    if _sha256(src) != versions.DOVI_TOOL_BUNDLE_SHA256:
        return [f"dovi_tool: NOT installed — the bundled copy ({src}) is missing or not the pinned file"]
    out = []
    tmp = dest + ".installing"
    shutil.copyfile(src, tmp)
    os.chmod(tmp, 0o755)
    if dovi_version(tmp) != want:               # the copy must RUN as the pinned version
        os.remove(tmp)
        return [f"dovi_tool: NOT installed — the bundled copy does not run as {want}"]
    if present:
        kept = f"{dest}.replaced-{time.strftime('%Y%m%d-%H%M%S')}"
        os.replace(dest, kept)                  # a Homebrew link is moved aside, not deleted
        out.append(f"dovi_tool: the old {have or 'unrunnable'} one is kept as {kept}")
    os.replace(tmp, dest)
    out.append(f"dovi_tool: {want} installed to {dest}")
    return out


def ensure(resources: str = DEFAULT_RESOURCES) -> list:
    """Copy each bundled file that is missing at its path; never replace one that is there."""
    return _install(resources, replace_mismatched=False) + _install_dovi(resources, False)


def repair(resources: str = DEFAULT_RESOURCES) -> list:
    """ensure(), and replace a file that is not the pinned one (the old one is kept)."""
    return _install(resources, replace_mismatched=True) + _install_dovi(resources, True)


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    fix = "--repair" in args
    rest = [a for a in args if a != "--repair"]
    resources = rest[0] if rest else DEFAULT_RESOURCES
    for line in (repair if fix else ensure)(resources):
        print(line)
    import preflight                            # what is still wrong (ffmpeg, dovi_tool, ...)
    problems = preflight._native_dv_problems()
    for problem, how in problems:
        print(f"still needed: {problem} — {how}")
    if not problems:
        print("Dolby Vision toolchain: ready")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
