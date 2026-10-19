"""Preflight — the exact-version gate Visionary refuses to run without.

Why this exists: the Topaz stage invokes Topaz Video AI 7.0.1's bundled ffmpeg with that
build's `tvai_up` schema, and the Dolby Vision step runs a pinned toolchain (dovi_tool, the
hdr_analyzer_mvp build, the SDR->PQ LUT, Homebrew ffmpeg 8) that every master was judged on. A
different version doesn't degrade gracefully, so the pins in engine/versions.py are gated here,
hard.

Two surfaces, one implementation:
- CLI (setup/onboarding):  python3 engine/preflight.py [--json] [--network]
    exit 0 = all pass · 1 = a HARD check failed · 2 = warnings only
- In-app: the dashboard server imports run_cheap() into /api/selftest (12 s poll) and refuses
  to arm the pipeline (POST /api/automation -> 409) while run_arm_gate() has a failure.

Severity semantics: "fail" checks gate hard_ok (the app refuses to arm); "warn" checks are
setup-progress items (config not filled in yet) — they block a RUN from succeeding but not
intentionally, so they only fail the strict exit code, not hard_ok.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import versions  # noqa: E402

BREW = "/opt/homebrew/bin"
BREW_TOOLS = ("ffmpeg", "ffprobe", "x265", "dovi_tool", "MP4Box")


def _check(cid, ok, severity, detail, fix=""):
    # `fix` is kept even when the check PASSES (it used to be blanked): the in-app Setup
    # section shows each row's remediation/command regardless of current state, and the
    # CLI printer guards on `ok` itself.
    return {"id": cid, "ok": bool(ok), "severity": severity, "detail": detail, "fix": fix}


def _bundle_version(app_path):
    """(short_version, build) from an app bundle's Info.plist, or (None, None)."""
    try:
        with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as f:
            p = plistlib.load(f)
        return p.get("CFBundleShortVersionString"), p.get("CFBundleVersion")
    except (OSError, plistlib.InvalidFileException):
        return None, None


def check_topaz_version():
    short, _ = _bundle_version(versions.TOPAZ_APP)
    if short is None:
        return _check("topaz_version", False, "fail",
                      f"Topaz Video AI not found at {versions.TOPAZ_APP}",
                      f"Install Topaz Video AI {versions.TOPAZ_VERSION} from Topaz's release "
                      f"archive, log in once in the app to activate (headless use works after), "
                      f"and disable auto-updates in its preferences.")
    if short != versions.TOPAZ_VERSION:
        return _check("topaz_version", False, "fail",
                      f"found {short}; require exactly {versions.TOPAZ_VERSION}",
                      f"Replace Topaz Video AI {short} with {versions.TOPAZ_VERSION} from Topaz's "
                      f"release archive (updates change the tvai_up model/parameter schema the "
                      f"pipeline is tuned for), and disable auto-updates.")
    ffmpeg = os.path.join(versions.TOPAZ_APP, "Contents", "MacOS", "ffmpeg")
    models = os.path.join(versions.TOPAZ_APP, "Contents", "Resources", "models")
    ok = os.path.exists(ffmpeg) and os.path.isdir(models)
    return _check("topaz_version", ok, "fail",
                  f"{versions.TOPAZ_VERSION} present; bundled ffmpeg={os.path.exists(ffmpeg)} "
                  f"models={os.path.isdir(models)}",
                  "Launch Topaz Video AI once and log in — that activates the license and "
                  "downloads the models the headless pipeline uses.")


def check_power_adapter():
    """The Topaz stage runs the GPU flat-out for hours, so the machine must sustain
    ~140 W. Three cases:

    * NO BATTERY (Mac mini/Studio/iMac) → mains-powered by definition: PASS, no wattage
      rule. (Without this a desktop is blocked outright — it has no AppleSmartBattery, so
      the run gate reads "on battery" and pauses forever.)
    * A LAPTOP on a KNOWN model → its documented ceiling decides, because a 140 W brick
      plugged into a 96 W-max machine still REPORTS 140 W; wattage alone can't tell you
      what the machine draws. Known-under-140 W is a HARD FAIL.
    * A LAPTOP we don't recognise (newer than these lists) → don't block it: fall back to
      the LIVE adapter reading and name the model so versions.MODELS_140W can be extended.

    WARN severity for the wattage part — being unplugged during setup is fine; the
    run-time gate enforces it when it actually matters."""
    import power
    need = versions.REQUIRED_ADAPTER_WATTS
    kind, model = ("laptop" if power.has_battery() else "desktop"), power.model_id()
    if kind == "desktop":
        return _check("power_adapter", True, "warn",
                      f"{model or 'desktop Mac'}: mains-powered (no battery) — the "
                      f"{need} W rule doesn't apply", "")
    if model in versions.MODELS_BELOW_140W:
        return _check("power_adapter", False, "fail",
                      f"{model} tops out below {need} W — it cannot power the Topaz stage",
                      f"Visionary needs a Mac that sustains {need} W: a 140 W-class MacBook "
                      f"Pro (16-inch Apple Silicon) on its own brick, or any desktop Mac.")
    known = model in versions.MODELS_140W
    w = power.adapter_watts_sustained()
    fix = (f"Plug in the {need} W adapter. Smaller bricks cannot power the Topaz stage; the "
           f"pipeline will refuse to run on them.")
    if known and w is None:
        return _check("power_adapter", True, "warn",
                      f"{model} is a {need} W-class Mac; no adapter connected right now "
                      f"(the run-time gate checks this live)", "")
    if known:
        return _check("power_adapter", w >= need, "warn",
                      f"{model} ({need} W-class); connected adapter: {w} W", fix)
    # unknown laptop — the live reading is all we have
    if w is None:
        return _check("power_adapter", True, "warn",
                      f"{model or 'this Mac'} isn't in the known {need} W list; connect its "
                      f"adapter — the run-time gate requires >= {need} W", "")
    return _check("power_adapter", w >= need, "warn",
                  f"{model or 'this Mac'} isn't in the known {need} W list; connected "
                  f"adapter reads {w} W (require >= {need} W)", fix)


def check_brew_tools():
    missing = [t for t in BREW_TOOLS if not os.path.exists(os.path.join(BREW, t))]
    return _check("brew_tools", not missing, "fail",
                  ("all present: " + ", ".join(BREW_TOOLS)) if not missing
                  else "missing from /opt/homebrew/bin: " + ", ".join(missing),
                  "brew install ffmpeg x265 dovi_tool gpac")


_SHA_CACHE = {}     # path -> ((size, mtime_ns), sha256 hex)


def _sha256_cached(path):
    """sha256 of a file, re-hashed only when its size or mtime moves. The LUT is ~7 MB and the
    arm gate re-runs every 60 s while waiting to re-arm, so the hash is remembered — but any
    edit or replacement of the file changes one of the two and is hashed afresh."""
    st = os.stat(path)
    key = (st.st_size, st.st_mtime_ns)
    hit = _SHA_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    _SHA_CACHE[path] = (key, h.hexdigest())
    return _SHA_CACHE[path][1]


def _sha256_or_none(path):
    """_sha256_cached, or None when the file is missing or can't be read."""
    if not os.path.isfile(path):
        return None
    try:
        return _sha256_cached(path)
    except OSError:
        return None


def _probe(argv, timeout=10):
    """(returncode, stdout+stderr) of a short tool probe; returncode None when it could not
    run at all or did not answer within `timeout` (the text then says why)."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"no answer within {timeout} s"
    except OSError as e:
        return None, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _native_dv_problems():
    """[(problem, fix)] for the native DV lane's toolchain; empty = ready. Every sub-check
    runs, so one pass names everything that is wrong rather than the first thing."""
    out = []
    an = versions.HDR_ANALYZER_PATH
    major = versions.NATIVE_FFMPEG_MAJOR
    build = ("Install the bundled build: Settings → Setup → Dolby Vision toolchain → Install, or "
             f"python3 engine/dvinstall.py --repair (it copies dv/hdr_analyzer_mvp to {an}).")
    sha = _sha256_or_none(an)
    if sha is None:
        out.append((f"hdr_analyzer_mvp missing or unreadable at {an}", build))
    elif sha != versions.HDR_ANALYZER_SHA256:
        out.append((f"hdr_analyzer_mvp is not the pinned build (sha256 {sha[:12]}..., want "
                    f"{versions.HDR_ANALYZER_SHA256[:12]}...)", build))
    else:
        # Only the PINNED build is ever executed. The hash can't see its dylibs: it links
        # Homebrew ffmpeg's libav*.62 by major version, so a `brew upgrade ffmpeg` breaks it
        # while the file is byte-identical — running it is the only proof it still loads.
        rc, text = _probe([an, "--help"])
        if rc != 0:
            tail = (text or "").strip().splitlines()[-1:] or ["no output"]
            out.append((f"hdr_analyzer_mvp does not run (--help: "
                        f"{'exit %s' % rc if rc is not None else 'failed'}: {tail[0][:160]})",
                        f"It links Homebrew's ffmpeg {major} at /opt/homebrew/opt/ffmpeg, so its "
                        f"libraries probably moved (brew upgrade ffmpeg?): reinstall ffmpeg "
                        f"{major} and `brew pin ffmpeg`."))
    lut = versions.NATIVE_LUT_PATH
    lut_fix = ("Install the bundled LUT: Settings → Setup → Dolby Vision toolchain → Install, or "
               f"python3 engine/dvinstall.py --repair (it copies dv/{os.path.basename(lut)} to "
               f"{lut}). It IS the SDR->PQ conversion, so any other file changes the picture.")
    sha = _sha256_or_none(lut)
    if sha is None:
        out.append((f"LUT missing or unreadable at {lut}", lut_fix))
    elif sha != versions.NATIVE_LUT_SHA256:
        out.append((f"LUT is not the pinned file (sha256 {sha[:12]}...)", lut_fix))
    rc, text = _probe([os.path.join(BREW, "dovi_tool"), "--version"])
    m = re.search(r"dovi_tool\s+v?(\d[\w.+-]*)", text or "") if rc == 0 else None
    if not m or m.group(1) != versions.DOVI_TOOL_VERSION:
        got = m.group(1) if m else ("not runnable" if rc is None else "unreadable")
        out.append((f"dovi_tool is {got}; the native lane needs exactly "
                    f"{versions.DOVI_TOOL_VERSION}",
                    f"Install dovi_tool {versions.DOVI_TOOL_VERSION} at {BREW}/dovi_tool and "
                    f"`brew pin dovi_tool`: the `generate` config this lane writes is "
                    f"version-sensitive."))
    rc, text = _probe([os.path.join(BREW, "ffmpeg"), "-version"])
    m = re.match(r"\s*ffmpeg version n?(\d+)\.", text or "") if rc == 0 else None
    if not m or int(m.group(1)) != major:
        got = m.group(1) if m else ("not runnable" if rc is None else "unreadable")
        out.append((f"ffmpeg major is {got}; the native lane needs {major}",
                    f"Install Homebrew ffmpeg {major} and `brew pin ffmpeg`: the LUT's colour "
                    f"was measured on {major}, and hdr_analyzer_mvp links its libraries."))
    return out


def check_native_dv():
    """The Dolby Vision toolchain: the pinned hdr_analyzer_mvp (sha256, AND it executes), the
    pinned LUT (sha256), dovi_tool's exact version and ffmpeg's major. Part of the arm gate:
    without it no item can get its Dolby Vision."""
    problems = _native_dv_problems()
    if problems:
        detail = "; ".join(p for p, _ in problems)
        fix = " ".join(dict.fromkeys(f for _, f in problems))     # de-duplicated, in order
    else:
        detail = (f"hdr_analyzer_mvp {versions.HDR_ANALYZER_COMMIT} (pinned, runs), LUT pinned, "
                  f"dovi_tool {versions.DOVI_TOOL_VERSION}, ffmpeg {versions.NATIVE_FFMPEG_MAJOR}")
        fix = (f"Needs hdr_analyzer_mvp built at {versions.HDR_ANALYZER_COMMIT}, the pinned LUT, "
               f"dovi_tool {versions.DOVI_TOOL_VERSION} and Homebrew ffmpeg "
               f"{versions.NATIVE_FFMPEG_MAJOR}.")
    return _check("native_dv", not problems, "fail", detail, fix)


# check_sublercli was RETIRED (2026-08-09): the enforced-VBV x265 + fresh MP4Box mux
# superseded the Subler-optimize step long ago (see remux.py's header) — optimize_dv has
# no pipeline callers and is reference-only, so demanding an x86_64 tool + Rosetta from
# every fresh install was pure vestige. remux.SUBLERCLI + optimize_dv remain for manual use.


ENGINE_PYTHON = "/usr/bin/python3"   # what the app launches the engine with (macapp/main.swift)


def check_python_deps():
    """Probe the ENGINE's interpreter (/usr/bin/python3), not whatever runs this CLI — the
    app launches the engine with the system python, and the border-extension stage needs cv2
    in it. A conda/homebrew shell python is irrelevant."""
    try:
        r = subprocess.run(
            [ENGINE_PYTHON, "-c",
             "import sys, cv2; print('%d.%d' % sys.version_info[:2], cv2.__version__)"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return _check("python_deps", False, "fail", f"{ENGINE_PYTHON} probe failed: {e}",
                      "Install the Xcode Command Line Tools: xcode-select --install")
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-1:] or ["import failed"]
        return _check("python_deps", False, "fail",
                      f"{ENGINE_PYTHON}: {tail[0]}",
                      f"{ENGINE_PYTHON} -m pip install --user opencv-python  "
                      f"(if pip is missing: {ENGINE_PYTHON} -m ensurepip --user first)")
    ver, cv2_ver = r.stdout.split()
    return _check("python_deps", True, "fail", f"{ENGINE_PYTHON} is {ver} with cv2 {cv2_ver}", "")


def check_config(network=False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import transfer, plex
    cfg_path = os.path.expanduser("~/.topaz-pipeline/config.json")
    hosts = transfer.nas_hosts()
    s = transfer.ftp_settings()
    token = plex.plex_token()
    # plex_token is NOT required — Plex is an optional extra (README 'Configuration'): the
    # token is the optionality signal, so a blank one must neither fail this check nor block
    # the FTP probe below (it used to do both, so a Plex-less setup could never go all-green).
    missing = [k for k, v in (("ftp_host(s)", hosts), ("ftp_user", s["user"]),
                              ("ftp_pass", s["passwd"])) if not v]
    if missing:
        return _check("config", False, "warn",
                      f"{cfg_path}: missing/empty -> " + ", ".join(missing),
                      "cp config.example.json ~/.topaz-pipeline/config.json && chmod 600 "
                      "~/.topaz-pipeline/config.json — then fill in the NAS values (Plex is "
                      "optional; see README 'Configuration').")
    if not network:
        note = "all required keys set" + ("" if token else " · Plex not configured (optional)")
        return _check("config", True, "warn", note + " (run --network to probe live)", "")
    # live probes: FTP always; Plex/Youtarr only when configured — both optional features
    detail, ok = [], True
    try:
        ftp = transfer.connect(timeout=10)
        try: ftp.quit()
        except Exception: pass
        detail.append("FTP: connected")
    except Exception as e:
        ok = False
        detail.append(f"FTP: {e}")
    if token:
        try:
            import urllib.request
            base = plex.plex_base_urls()[0]
            req = urllib.request.Request(base + "/identity", headers={"X-Plex-Token": token})
            with urllib.request.urlopen(req, timeout=10):
                pass
            detail.append("Plex: reachable")
        except Exception as e:
            ok = False
            detail.append(f"Plex: {e}")
    else:
        detail.append("Plex: not configured (optional)")
    return _check("config", ok, "warn", "; ".join(detail),
                  "Check the NAS is reachable (VPN up? LAN name resolves?) and — if you use "
                  "Plex — the URL/token in ~/.topaz-pipeline/config.json are right.")


def run_cheap():
    """The sub-millisecond checks safe for the app's 12 s selftest poll."""
    return [check_topaz_version()]


def run_arm_gate():
    """What Activation requires: the version pins PLUS the instant dependency checks and the
    Dolby Vision toolchain — a missing x265 or a broken hdr_analyzer_mvp used to surface as a
    stage failure HOURS into a job; now it refuses to arm with a named check. Not run_cheap():
    check_python_deps spawns /usr/bin/python3 (~0.3 s) and check_native_dv runs two tools and
    the analyzer — too heavy for the selftest poll, fine at arm time and in the 60 s re-arm
    tick (the hashes are cached)."""
    return run_cheap() + [check_brew_tools(), check_python_deps(), check_native_dv()]


def run_checks(network=False):
    checks = run_arm_gate() + [check_power_adapter(), check_config(network=network)]
    hard_ok = all(c["ok"] for c in checks if c["severity"] == "fail")
    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "hard_ok": hard_ok, "checks": checks}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Visionary preflight — exact-version gate")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--network", action="store_true", help="probe NAS FTP + Plex live")
    args = ap.parse_args(argv)
    result = run_checks(network=args.network)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for c in result["checks"]:
            mark = "PASS" if c["ok"] else ("FAIL" if c["severity"] == "fail" else "WARN")
            print(f"[{mark:4}] {c['id']}: {c['detail']}")
            if not c["ok"] and c["fix"]:       # fix is now always populated — print on failure only
                print(f"       fix: {c['fix']}")
        print(f"\nhard_ok={result['hard_ok']} ok={result['ok']}")
    return 0 if result["ok"] else (1 if not result["hard_ok"] else 2)


if __name__ == "__main__":
    sys.exit(main())
