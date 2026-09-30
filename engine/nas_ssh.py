"""The NAS over SSH, for jobs that must RESUME and must REPLACE A FILE IN PLACE.

Everything else in Visionary moves files over FTP (transfer.py). This exists for the Dolby Vision
profile 7 -> 8.1 lane, whose files are 20-130 GB, whose transfers must STOP while anyone is
streaming from Plex (the NAS is CPU-saturated during a stream, and the user's rule is that Plex
gets the box), and whose result must land under the EXACT original filename. FTP here cannot
resume (no REST) and cannot rename atomically over an existing file. SSH can do both, and it is
the transport the user proved this job on: `ssh ... "tail -c +N PATH"` down and, up, a write at an
explicit offset (upload_leg_cmd; it was `cat >> PATH`) at 100-225 MB/s, with UGOS's SFTP root and its rsync both unusable.

So a transfer here is a sequence of resumable legs, paced by `limit()`: the caller's current cap
in bytes per second (None = full speed). The lane caps it while anyone is watching Plex — the
user's rule for this job is to THROTTLE transfers during a stream, and the NAS is CPU-starved then,
so every byte of sshd crypto counts. A leg that dies keeps its bytes; the next one resumes from
exactly there. Every completed transfer is verified by size AND a SHA-1 of its first and last
64 MiB on both ends, because a replace-in-place with no backup should not rest on a size match.

THE SWAP is one rename(2) on the NAS, so there is never a moment without a file under that name,
and it keeps the original's owner, group and mode exactly. Some originals belong to uid 911, which
the SSH user cannot chown to, so the rename runs as root in a one-second container on the NAS
(the SSH user is in the docker group), with the numeric owner and mode read beforehand: alpine's
busybox chown/chmod have no --reference. Nothing heavy runs on the NAS itself — reads and writes.

PURE (unit-tested): ftp_to_host, host_to_plex, share_root, stage_path_for, swap_argv_inner,
ssh_argv_for. The link to the NAS is chosen in nas_link.py: the wired one, never Wi-Fi when a
cable is there (user-dictated 2026-09-30).
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import sys
import time

CHUNK = 8 * 1024 * 1024
HASH_SPAN = 64 * 1024 * 1024            # SHA-1 over the first and last 64 MiB
STAGE_DIR = "_claude-tmp"               # beside the library, same filesystem, never scanned
STAGE_EXT = ".part"                     # not a video extension: Plex and Visionary never list it
SWAP_IMAGE = "alpine:latest"            # already on the NAS; the swap needs only coreutils
LIMIT_POLL_SECS = 2.0                   # how often a leg re-reads its cap and the abort flag


class Stopped(Exception):
    """The run was stopped."""


class NoLink(Exception):
    """Settings say Ethernet only and no wired link reaches the NAS: wait, never fall back."""


class _Relink(Exception):
    """The chosen link changed under a running leg (the setting, or a cable): end the leg; the
    next one resumes from the bytes already moved, on the new link."""


def link_key(ln) -> tuple:
    """What a running leg is tied to: the interface it is bound to and whether any may be used. The
    setting's name is left out on purpose — Ethernet first -> Ethernet only on the same cable is
    the same route, and a relink there would only cost a restart (review 2026-09-30)."""
    ln = ln or {}
    return (ln.get("iface") if ln.get("bound") else None, bool(ln.get("unavailable")))


# ---- paths ---------------------------------------------------------------------------------

def _vol_of_share(share: str):
    """UGOS names one share per volume: `Media` is volume 1, `MediaVolumeN` is volume N."""
    m = re.fullmatch(r"MediaVolume(\d+)", share or "")
    if m:
        return int(m.group(1))
    return 1 if share == "Media" else None


def ftp_to_host(ftp_path: str):
    """`/Media/Movies/x.mkv` -> `/volume1/Media/Movies/x.mkv`;
    `/MediaVolume3/Movies/x.mkv` -> `/volume3/MediaVolume3/Movies/x.mkv`. None if unmappable."""
    parts = [p for p in str(ftp_path or "").split("/") if p]
    if len(parts) < 2:
        return None
    vol = _vol_of_share(parts[0])
    if vol is None:
        return None
    return f"/volume{vol}/" + "/".join(parts)


def host_to_plex(host_path: str):
    """The Plex container's view of a host path: `/volume1/Media/Movies` -> `/media/Movies`,
    `/volume2/MediaVolume2/Movies` -> `/media/vol2/Movies`. None if unmappable."""
    parts = [p for p in str(host_path or "").split("/") if p]
    if len(parts) < 2:
        return None
    m = re.fullmatch(r"volume(\d+)", parts[0])
    if not m or _vol_of_share(parts[1]) != int(m.group(1)):
        return None
    vol, rest = int(m.group(1)), parts[2:]
    root = "/media" if vol == 1 else f"/media/vol{vol}"
    return "/".join([root] + rest)


def share_root(host_path: str):
    """`/volume1/Media/Movies/x.mkv` -> `/volume1/Media` — where the staging folder lives, so the
    final rename never crosses a filesystem."""
    parts = [p for p in str(host_path or "").split("/") if p]
    return "/" + "/".join(parts[:2]) if len(parts) >= 2 else None


def stage_path_for(host_path: str):
    key = hashlib.sha1(host_path.encode()).hexdigest()[:12]
    return f"{share_root(host_path)}/{STAGE_DIR}/{key}{STAGE_EXT}"


# ---- ssh -----------------------------------------------------------------------------------

def target():
    """user@host for SSH: config `nas_ssh` if set, else the FTP user at the first LAN FTP host.
    LAN first on purpose — the FTP host list leads with the Tailscale address, which is right for
    a phone away from home and wrong for moving terabytes across the living room."""
    import configstore
    try:
        c = configstore.read() or {}
    except Exception:  # noqa: BLE001 — an unreadable config falls back to the ssh alias
        c = {}
    if c.get("nas_ssh"):
        return c["nas_ssh"]
    hosts = list(c.get("ftp_hosts") or [])
    lan = [h for h in hosts if re.match(r"^(192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.)", str(h))
           or str(h).endswith(".local")]
    host = (lan or hosts or ["nas"])[0]
    user = c.get("ftp_user")
    return f"{user}@{host}" if user else host


def link():
    """The link the transfers use to reach the NAS (nas_link.detect: the wired one when there is
    one). Local commands only, cached."""
    import nas_link
    return nas_link.remember(nas_link.detect(target().rsplit("@", 1)[-1]))


def ssh_argv_for(tgt: str, ln) -> list:
    """PURE: the ssh command line for `tgt` over link `ln`. IPv4 always — the NAS's mDNS name also
    resolves to IPv6 link-local addresses, and ssh preferred one scoped to Wi-Fi (2026-09-30). On a
    chosen (bound) link: the NAS's IPv4 address, `-B <interface>` so the kernel cannot route it
    elsewhere, and a connection-sharing socket named for that interface, so a master opened on one
    link is never reused once the setting or the cable picks another. Raises NoLink when the
    setting is Ethernet only and there is no cable."""
    user, _, host = tgt.rpartition("@")
    if (ln or {}).get("unavailable"):
        raise NoLink("no Ethernet link to the NAS (Settings: NAS network — Ethernet only)")
    iface = (ln or {}).get("iface") if (ln or {}).get("bound") else None
    if (ln or {}).get("ip"):
        host = ln["ip"]
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
            "-o", "AddressFamily=inet",
            "-o", "ControlMaster=auto", "-o", "ControlPersist=120",
            "-o", f"ControlPath=/tmp/visionary-ssh-%r@%h:%p{'-' + iface if iface else ''}"]
    if iface:
        argv += ["-B", iface]
    return argv + ["-c", "aes128-gcm@openssh.com", f"{user}@{host}" if user else host]


def ssh_argv():
    if "unittest" in sys.modules:       # same rule as scratch and dvbook: a test never reaches out
        raise RuntimeError("a test tried to reach the NAS over SSH — mock it")
    return ssh_argv_for(target(), link())


SSH_FAILED = 255                        # ssh's own exit code: the connection, not the command
RETRY_WAITS = (5, 20)                   # seconds before each retry of a connection failure


def remote(cmd: str, timeout=600, retries=len(RETRY_WAITS)) -> str:
    """Run one command on the NAS. A CONNECTION failure (exit 255 — a timed-out banner exchange,
    a dropped link) is retried a couple of times before it counts: one such blip failed a movie
    whose converted file was sitting ready to upload (Annihilation, 2026-09-30). Every command here
    is safe to repeat except the swap's rename, which passes retries=0 and relies on the lane
    recognizing a finished rename instead."""
    for attempt in range(retries + 1):
        r = subprocess.run(ssh_argv() + [cmd], capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return r.stdout
        if r.returncode != SSH_FAILED or attempt >= retries:
            break
        time.sleep(RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)])
    raise RuntimeError(f"NAS command failed ({r.returncode}): {cmd[:120]} :: {r.stderr.strip()[-300:]}")


def reachable(timeout=20) -> bool:
    try:
        return subprocess.run(ssh_argv() + ["true"], capture_output=True,
                              timeout=timeout).returncode == 0
    except (subprocess.TimeoutExpired, OSError, NoLink):
        return False


def stat(host_path: str):
    """(size, mtime, uid, gid, mode_octal) of a NAS file, or None if it is not there."""
    out = remote(f"stat -c '%s %Y %u %g %a' {shlex.quote(host_path)} 2>/dev/null || true").split()
    if len(out) != 5:
        return None
    return int(out[0]), int(out[1]), int(out[2]), int(out[3]), out[4]


def remote_hash(host_path: str, size: int, tail: bool) -> str:
    q = shlex.quote(host_path)
    if tail:
        skip = max(0, (size - HASH_SPAN) // (1024 * 1024))
        cmd = f"dd if={q} bs=1M skip={skip} 2>/dev/null | sha1sum | cut -c1-40"
    else:
        cmd = f"dd if={q} bs=1M count={HASH_SPAN // (1024 * 1024)} 2>/dev/null | sha1sum | cut -c1-40"
    return remote(cmd, timeout=900).strip()


def local_hash(path: str, tail: bool) -> str:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if tail:
            fh.seek(max(0, (size - HASH_SPAN) // (1024 * 1024)) * 1024 * 1024)
            data = fh.read()
        else:
            data = fh.read(HASH_SPAN)
    return hashlib.sha1(data).hexdigest()


def _leg(src, sink, *, moved0, total, abort, limit, on_progress, relink=None):
    """Pump one leg, paced to `limit()` bytes/s. Returns bytes moved; raises Stopped on abort and
    _Relink when `relink()` says the chosen link is no longer the one this leg runs on."""
    moved, polled, cap = 0, 0.0, None
    win_t, win_b = time.monotonic(), 0             # pacing window, reset whenever the cap changes
    while True:
        now = time.monotonic()
        if now - polled >= LIMIT_POLL_SECS:
            polled = now
            if abort is not None and abort.is_set():
                raise Stopped("stopped")
            if relink is not None and relink():
                raise _Relink("the chosen link changed")
            new_cap = limit() if limit else None
            if new_cap != cap:
                cap, win_t, win_b = new_cap, now, 0
        buf = src.read(CHUNK)
        if not buf:
            return moved
        sink.write(buf)
        moved += len(buf)
        win_b += len(buf)
        if on_progress:
            on_progress(moved0 + moved, total)
        if cap:
            ahead = win_b / cap - (time.monotonic() - win_t)
            if ahead > 0:
                time.sleep(min(ahead, LIMIT_POLL_SECS))


def _kill(proc):
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def download(host_path, local, size, *, abort=None, limit=None, on_progress=None):
    """Resumable pull of a NAS file to `local`, verified by size and head/tail SHA-1. A dropped
    leg resumes from the bytes already on disk. Returns the verified local path."""
    failures = 0
    while failures < 12:
        have = os.path.getsize(local) if os.path.exists(local) else 0
        if have > size:
            os.remove(local)
            have = 0
        if have == size:
            break
        leg = link_key(link())
        p = subprocess.Popen(ssh_argv() + [f"tail -c +{have + 1} {shlex.quote(host_path)}"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            with open(local, "ab") as fh:
                _leg(p.stdout, fh, moved0=have, total=size, abort=abort, limit=limit,
                     on_progress=on_progress, relink=lambda: link_key(link()) != leg)
            if p.wait(timeout=60):
                failures += 1
                _pause(abort, 10)              # ssh itself failed (NAS asleep, network blip)
        except Stopped:
            _kill(p)
            raise
        except _Relink:
            _kill(p)                       # straight on: the next leg takes the new link
        except Exception:  # noqa: BLE001 — a dropped leg: the bytes stay, the next leg resumes
            _kill(p)
            failures += 1
            _pause(abort, 10)
    if not os.path.exists(local) or os.path.getsize(local) != size:
        raise RuntimeError("download incomplete")
    if (local_hash(local, False) != remote_hash(host_path, size, False)
            or local_hash(local, True) != remote_hash(host_path, size, True)):
        os.remove(local)
        raise RuntimeError("the downloaded copy does not match the NAS file")
    return local


def upload_leg_cmd(stage: str, have: int) -> str:
    """PURE: the NAS side of one upload leg — write the stream at byte `have` of the staged file,
    not at its end. A killed leg's last buffered bytes can still reach the NAS after the next leg
    has read the file's size; appended (the old `cat >>`), they would land twice and splice the
    file, and only the final hash check would notice. Written at their own offsets, late bytes can
    only rewrite the identical bytes already there (review 2026-09-30). GNU dd 9.1 on the NAS."""
    return (f"dd of={shlex.quote(stage)} bs=4M seek={int(have)} oflag=seek_bytes conv=notrunc "
            f"iflag=fullblock status=none")


def upload(local, stage, *, abort=None, limit=None, on_progress=None):
    """Resumable push of `local` to a NAS staging path, verified by size and head/tail SHA-1."""
    size = os.path.getsize(local)
    remote(f"mkdir -p {shlex.quote(os.path.dirname(stage))}")
    failures = 0
    while failures < 12:
        st = stat(stage)
        have = st[0] if st else 0
        if have > size:
            remote(f"rm -f {shlex.quote(stage)}")
            have = 0
        if have == size:
            break
        leg = link_key(link())
        p = subprocess.Popen(ssh_argv() + [upload_leg_cmd(stage, have)],
                             stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            with open(local, "rb") as fh:
                fh.seek(have)
                _leg(fh, p.stdin, moved0=have, total=size, abort=abort, limit=limit,
                     on_progress=on_progress, relink=lambda: link_key(link()) != leg)
            p.stdin.close()
            if p.wait(timeout=300):
                failures += 1
                _pause(abort, 10)
        except Stopped:
            _kill(p)
            raise
        except _Relink:
            _kill(p)
        except Exception:  # noqa: BLE001
            _kill(p)
            failures += 1
            _pause(abort, 10)
    st = stat(stage)
    if not st or st[0] != size:
        raise RuntimeError(f"upload incomplete ({st[0] if st else 0} of {size})")
    if (local_hash(local, False) != remote_hash(stage, size, False)
            or local_hash(local, True) != remote_hash(stage, size, True)):
        remote(f"rm -f {shlex.quote(stage)}")      # a bad copy must not be resumed onto next time
        raise RuntimeError("the uploaded copy does not match")


def _pause(abort, secs):
    if abort is not None:
        if abort.wait(secs):
            raise Stopped("stopped")
    else:
        time.sleep(secs)


def remote_dv_profile(host_path: str):
    """The DV profile the NAS's own ffprobe reads from a file's header (header only — light)."""
    out = remote(f"ffprobe -v error -select_streams v:0 -show_entries stream_side_data=dv_profile "
                 f"-of default=nw=1:nk=1 {shlex.quote(host_path)} 2>/dev/null | head -1").strip()
    return int(out) if out.isdigit() else None


def swap_argv_inner(stage: str, host_path: str, uid: int, gid: int, mode: str) -> str:
    """PURE: the root shell line the swap container runs — owner and mode first, then ONE rename."""
    q, s = shlex.quote(host_path), shlex.quote(stage)
    return f"chown {int(uid)}:{int(gid)} {s} && chmod {mode} {s} && mv -f {s} {q}"


def swap(stage: str, host_path: str, *, expect_size: int, expect_mtime: int, new_size: int):
    """Replace `host_path` with `stage` in ONE rename, keeping its owner, group and mode. Refuses if
    the original changed since it was read. Verifies the final size."""
    st = stat(host_path)
    if not st:
        raise RuntimeError("the original is gone; not swapping")
    if (st[0], st[1]) != (expect_size, expect_mtime):
        raise RuntimeError("the original changed since it was read; not replacing it")
    if not re.fullmatch(r"[0-7]{3,4}", st[4]):
        raise RuntimeError(f"unexpected mode {st[4]!r} on the original")
    if share_root(stage) != share_root(host_path):
        raise RuntimeError("the staged file is on another share; a rename would not be atomic")
    share = share_root(host_path)
    inner = swap_argv_inner(stage, host_path, st[2], st[3], st[4])
    remote(f"docker run --rm -v {shlex.quote(share)}:{shlex.quote(share)} {SWAP_IMAGE} "
           f"sh -c {shlex.quote(inner)}", timeout=300, retries=0)
    after = stat(host_path)
    if not after or after[0] != new_size:
        raise RuntimeError("after the swap the NAS file has the wrong size")
    if (after[2], after[3], after[4]) != (st[2], st[3], st[4]):
        raise RuntimeError("the swap did not keep the original's owner, group and mode")
    remote(f"rmdir {shlex.quote(os.path.dirname(stage))} 2>/dev/null || true")


def discard_stage(host_path: str):
    stage = stage_path_for(host_path)
    remote(f"rm -f {shlex.quote(stage)}; rmdir {shlex.quote(os.path.dirname(stage))} 2>/dev/null || true")
