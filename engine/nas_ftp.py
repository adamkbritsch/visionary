"""The NAS over FTP, for the Dolby Vision profile 7 -> 8.1 lane: transfers that RESUME, and a swap
that REPLACES A FILE IN PLACE.

User, 2026-10-01: "it shouldn't be going over ssh at all, it should use ftp". The lane now uses the
same FTP as the rest of Visionary (transfer.connect: the NAS network setting's LAN route first,
Tailscale away from home), so it works wherever the pipeline does — SSH to the NAS answers only on
its own network. The NAS's FTP server (smbftpd) was proven on everything this job needs
(2026-10-01): REST before RETR and before STOR resumes a transfer at a byte offset, with the bytes
intact; RNFR/RNTO onto an existing file replaces it in one rename; MDTM answers in UTC to the
second; SITE CHMOD sets a mode. Two limits it has: MLST and a listing of a folder whose name has
[brackets] glob them (release names are full of brackets), so a file is stat'ed with SIZE + MDTM
and a mode is read from the parent's MLSD listing when it can be; and FTP cannot chown, so a
replaced movie belongs to the FTP user (1000:10). Its mode is carried over, and the libraries are
mode 777, so Plex and Radarr read it as before.

A transfer is a sequence of resumable legs paced by `limit()`: the caller's current cap in bytes
per second (None = full speed); the lane caps it while anyone is watching Plex. A leg that dies
keeps its bytes; the next one resumes from exactly there (REST). A late block from a killed upload
leg can only land at its own offset and rewrite the identical bytes, because REST + STOR writes at
the offset given. Every completed transfer is verified by size AND by reading sampled ranges back
from the NAS — the head, the tail and evenly spaced middles — because a replace-in-place with no
backup should not rest on a size match.

THE SWAP is one rename on the NAS, so there is never a moment without a file under that name.

PURE (unit-tested): ftp_to_host, host_to_ftp, host_to_plex, share_root, stage_path_for,
sample_ranges, mdtm_epoch, link_key, wire. Paths go on the wire as their UTF-8 bytes (wire()), and a
short exchange is retried after a connection blip — never the swap's rename. The link to the NAS is chosen in nas_link.py.
"""
from __future__ import annotations

import calendar
import ftplib
import hashlib
import ipaddress
import os
import re
import subprocess
import sys
import tempfile
import time

CHUNK = 8 * 1024 * 1024
STAGE_DIR = "_claude-tmp"               # beside the library, same filesystem, never scanned
STAGE_EXT = ".part"                     # not a video extension: Plex and Visionary never list it
LIMIT_POLL_SECS = 2.0                   # how often a leg re-reads its cap and the abort flag
MAX_FAILURES = 12                       # dropped legs a transfer survives before it fails
FAIL_PAUSE_SECS = 10
SAMPLES = 8                             # ranges read back to verify a finished transfer
SAMPLE_SPAN = 1024 * 1024
HEAD_BYTES = 8 * 1024 * 1024            # enough of an MKV for ffprobe to read its DV profile
TRANSFER_TIMEOUT = 300
FFPROBE = "/opt/homebrew/bin/ffprobe"
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


class Stopped(Exception):
    """The run was stopped."""


class NoLink(Exception):
    """Settings say Ethernet only and no wired link reaches the NAS: wait, never fall back. `note`
    is a reason for the panel when it is not the cable (a configuration Ethernet only cannot pin)."""

    def __init__(self, msg="", note=None):
        super().__init__(msg)
        self.note = note


class _Relink(Exception):
    """The chosen link changed under a running leg (the setting, a cable, coming home): end the
    leg; the next one resumes from the bytes already moved, on the new route."""


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


def host_to_ftp(host_path: str):
    """The inverse: `/volume3/MediaVolume3/Movies/x.mkv` -> `/MediaVolume3/Movies/x.mkv`. None
    if the path is not under a share on its own volume."""
    parts = [p for p in str(host_path or "").split("/") if p]
    if len(parts) < 3:
        return None
    m = re.fullmatch(r"volume(\d+)", parts[0])
    if not m or _vol_of_share(parts[1]) != int(m.group(1)):
        return None
    return "/" + "/".join(parts[1:])


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
    """Where a movie's new file is staged on the NAS (a host path, keyed by the original's)."""
    key = hashlib.sha1(host_path.encode()).hexdigest()[:12]
    return f"{share_root(host_path)}/{STAGE_DIR}/{key}{STAGE_EXT}"


def sample_ranges(size: int, n: int = SAMPLES, span: int = SAMPLE_SPAN) -> list:
    """PURE: [(offset, length)] read back to verify a transfer: the head, the tail, and evenly
    spaced ranges between — sorted, never overlapping, never past the end."""
    size = int(size or 0)
    if size <= 0:
        return []
    if n <= 1 or size <= n * span:              # small: compare all of it
        return [(o, min(span, size - o)) for o in range(0, size, span)]
    last = size - span                          # (size - span) / (n - 1) > span: never overlapping
    return [(round(i * last / (n - 1)), span) for i in range(n)]


def mdtm_epoch(reply: str):
    """PURE: epoch seconds from an MDTM reply ("213 20260614023105" — UTC), or None."""
    m = re.match(r"213\s+(\d{14})", reply or "")
    if not m:
        return None
    return calendar.timegm(time.strptime(m.group(1), "%Y%m%d%H%M%S"))


def wire(path: str) -> str:
    """PURE: a real-Unicode path as this connection's latin-1 encoding must put it on the wire — its
    UTF-8 bytes (the NAS disk is UTF-8 and every connection asks for OPTS UTF8 ON). transfer.to_wire
    leaves a name it can encode in latin-1 as it is, so "Amélie" or "Pokémon" went out with é as
    the single byte 0xE9 and the NAS found no such file (review 2026-10-01)."""
    return path.encode("utf-8").decode("latin-1")


def _ftp_path(host_path: str) -> str:
    """The wire form of a host path's FTP path. Raises for a path outside the NAS's shares."""
    p = host_to_ftp(host_path)
    if p is None:
        raise RuntimeError(f"not a path under a NAS share: {host_path}")
    return wire(p)


# ---- the connection ------------------------------------------------------------------------

_via = {"route": None}                  # how the last connection reached the NAS, for the panel
SHORT_RETRY_WAITS = (5, 20)             # a short exchange's retries after a connection blip, as the
                                        # SSH lane had (Annihilation failed on one, 2026-09-30)
_TRANSIENT = (OSError, EOFError, ftplib.error_temp, ftplib.error_reply, ftplib.error_proto)


def _lan_name():
    """The NAS's local-network NAME from the FTP host list (never an IP literal: nas_link)."""
    import nas_link
    import transfer
    for h in transfer.ftp_hosts():
        if not nas_link.is_literal(h):
            return h
    return None


def link():
    """The link the transfers use (nas_link.detect: the wired one when there is one), with how the
    last connection actually reached the NAS. Local commands only, cached."""
    import nas_link
    name = _lan_name()
    ln = dict(nas_link.detect(name)) if name else {"iface": None, "bound": False}
    ln["via"] = _via["route"]
    return nas_link.remember(ln)


def _lan_offered() -> bool:
    """Would a new connection take the LAN route right now (transfer._route_order)?"""
    try:
        import transfer
        return any(src for _h, src in transfer._route_order(transfer.ftp_hosts()))
    except Exception:  # noqa: BLE001
        return False


def _ethernet_only_reason() -> str:
    """Why Ethernet only finds no wired route — the cable, or a configuration it cannot pin."""
    import transfer
    if os.environ.get("TOPAZ_NAS_FTP_HOST") or transfer._config().get("ftp_host"):
        return "Ethernet only cannot pin an FTP host set by hand (ftp_host in the config)"
    if _lan_name() is None:
        return "Ethernet only needs the NAS's network name among the FTP hosts, not only addresses"
    return ""


def _connect(timeout=TRANSFER_TIMEOUT):
    """An FTP connection over the setting's route. Ethernet only + no wired route -> NoLink (the
    lane waits); otherwise the LAN route when it reaches the NAS, else the configured order. The
    connection carries `visionary_route`: "lan" or "tailscale"."""
    if "unittest" in sys.modules:       # same rule as scratch and dvbook: a test never reaches out
        raise RuntimeError("a test tried to reach the NAS over FTP — mock it")
    import nas_link
    import transfer
    ln = link()
    if ln.get("unavailable"):
        raise NoLink("Ethernet only, and no wired link reaches the NAS")
    lan_only = nas_link.priority_setting() == "ethernet_only"
    try:
        ftp = transfer.connect(timeout=timeout, lan_only=lan_only)
    except transfer.NoLanRoute as e:
        why = _ethernet_only_reason()
        raise NoLink(why or str(e), note=why or None)
    try:
        route = "tailscale" if ipaddress.ip_address(ftp.host) in TAILSCALE_NET else "lan"
    except ValueError:
        route = "lan"
    _via["route"] = ftp.visionary_route = route
    return ftp


def _close(ftp, hard=False):
    if ftp is None:
        return
    try:
        if hard:
            ftp.close()
        else:
            ftp.quit()
    except Exception:  # noqa: BLE001
        try:
            ftp.close()
        except Exception:  # noqa: BLE001
            pass


def _retrying(fn, retry=True):
    """fn(), tried again after SHORT_RETRY_WAITS when the connection blips — never on an FTP
    refusal (5xx: the answer), NoLink, Stopped or anything of the caller's own."""
    waits = SHORT_RETRY_WAITS if retry else ()
    for i in range(len(waits) + 1):
        try:
            return fn()
        except _TRANSIENT:
            if i >= len(waits):
                raise
            _pause(None, waits[i])


def reachable(timeout=20) -> bool:
    ftp = None
    try:
        ftp = _connect(timeout)
        return True
    except (ftplib.all_errors + (OSError, NoLink, RuntimeError)):
        return False
    finally:
        _close(ftp)


def _size(ftp, path):
    try:
        ftp.voidcmd("TYPE I")
        return ftp.size(path)
    except ftplib.error_perm:
        return None                     # 550: not there


def _short(fn, retry=True):
    """One short FTP exchange on a fresh connection, closed after; retried after a blip unless
    `retry` is False (the swap's rename: a lost reply must not send it twice)."""
    def once():
        ftp = _connect(timeout=60)
        try:
            return fn(ftp)
        finally:
            _close(ftp)
    return _retrying(once, retry)


# ---- reads -----------------------------------------------------------------------------------

def stat(host_path: str):
    """(size, mtime) of a NAS file, or None if it is not there."""
    path = _ftp_path(host_path)
    def go(ftp):
        size = _size(ftp, path)
        if size is None:
            return None
        return size, mdtm_epoch(ftp.sendcmd("MDTM " + path))
    return _short(go)


def mode_of(host_path: str):
    """The file's mode ("0777") from its folder's MLSD listing, or None when the listing cannot
    say (a folder whose name has brackets lists empty: smbftpd globs it)."""
    folder, name = _ftp_path(host_path).rsplit("/", 1)
    def go(ftp):
        try:
            for n, facts in ftp.mlsd(folder or "/", facts=["unix.mode"]):
                if n == name:                    # both in wire form
                    return facts.get("unix.mode")
        except ftplib.error_perm:
            return None
        return None
    try:
        return _short(go)
    except ftplib.all_errors:
        return None                              # a mode is a nicety: never fail a swap on it


def read_range(host_path: str, offset: int, length: int) -> bytes:
    """`length` bytes of a NAS file from `offset` (fewer at its end)."""
    path = _ftp_path(host_path)
    def once():
        ftp = _connect(timeout=120)
        hard = True
        try:
            ftp.voidcmd("TYPE I")
            conn = ftp.transfercmd("RETR " + path, rest=int(offset))
            buf = bytearray()
            try:
                while len(buf) < length:
                    b = conn.recv(min(CHUNK, length - len(buf)))
                    if not b:
                        break
                    buf.extend(b)
            finally:
                conn.close()
            if len(buf) < length:
                ftp.voidresp()               # the file ended: the RETR completed normally
                hard = False                 # (stopping one early wedges the control connection)
            return bytes(buf[:length])
        finally:
            _close(ftp, hard=hard)
    return _retrying(once)


def remote_dv_profile(host_path: str):
    """The DV profile of a NAS file, read by ffprobe from its first HEAD_BYTES (the track header
    carries it). None when it has none or cannot be read."""
    head = read_range(host_path, 0, HEAD_BYTES)
    fd, tmp = tempfile.mkstemp(suffix=".mkv")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(head)
        r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream_side_data=dv_profile", "-of", "default=nw=1:nk=1", tmp],
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout.split() or [""])[0]
        return int(out) if out.isdigit() else None
    finally:
        os.remove(tmp)


class Mismatch(RuntimeError):
    """A copy's bytes differ from the original's: it is bad, not merely unreadable right now."""


def verify_copy(local: str, host_path: str):
    """Raise Mismatch unless sampled ranges of the NAS file equal the local file's, byte for byte.
    A connection that fails while reading raises its own error: the copy may be fine."""
    size = os.path.getsize(local)
    with open(local, "rb") as fh:
        for off, ln in sample_ranges(size):
            fh.seek(off)
            if fh.read(ln) != read_range(host_path, off, ln):
                raise Mismatch(f"the NAS copy differs from this Mac's at byte {off}")


# ---- transfers -----------------------------------------------------------------------------

def _leg(read, write, *, moved0, total, abort, limit, on_progress, relink=None):
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
        buf = read(CHUNK)
        if not buf:
            return moved
        write(buf)
        moved += len(buf)
        win_b += len(buf)
        if on_progress:
            on_progress(moved0 + moved, total)
        if cap:
            ahead = win_b / cap - (time.monotonic() - win_t)
            if ahead > 0:
                time.sleep(min(ahead, LIMIT_POLL_SECS))


def _pause(abort, secs):
    if abort is not None:
        if abort.wait(secs):
            raise Stopped("stopped")
    else:
        time.sleep(secs)


def _relinker(ftp):
    """When a running leg must end so the next one takes a better route: the link changed (the
    setting, a cable), or the leg went over Tailscale because the LAN was not offered then and it
    is now (home again). A leg that fell back to Tailscale WHILE the LAN was offered stays put:
    the LAN failed it, and trying again every two seconds would only stall (review 2026-10-01)."""
    key = link_key(link())
    away = getattr(ftp, "visionary_route", None) == "tailscale" and not _lan_offered()
    return lambda: link_key(link()) != key or (away and _lan_offered())


def _legs(run_leg, have_fn, size, abort):
    """Drive legs until `have_fn()` reaches `size`. A dropped leg, a failed size check, and a leg
    that ended cleanly having moved nothing (a NAS file now shorter than expected) each cost a pause
    and count; a relink starts the next leg at once; Stopped and NoLink end the transfer."""
    failures, prev = 0, None
    _legs.last_error = None
    while failures < MAX_FAILURES:
        try:
            have = have_fn()
            if have == size:
                return
            if prev is not None and have <= prev:
                failures += 1
                _pause(abort, FAIL_PAUSE_SECS)
                if failures >= MAX_FAILURES:
                    return
            prev = None
            run_leg(have)
            prev = have
        except (Stopped, NoLink):
            raise
        except _Relink:
            continue
        except Exception as ex:  # noqa: BLE001 — a dropped leg: the bytes stay, the next resumes
            _legs.last_error = f"{type(ex).__name__}: {str(ex)[:160]}"
            failures += 1
            _pause(abort, FAIL_PAUSE_SECS)


def _last_leg_error() -> str:
    """The last dropped leg's error, for a transfer that ran out of attempts — "upload incomplete
    (0 of N)" alone hid why every first upload failed (2026-10-02)."""
    err = getattr(_legs, "last_error", None)
    return f" — last error: {err}" if err else ""


def download(host_path, local, size, *, abort=None, limit=None, on_progress=None):
    """Resumable pull of a NAS file to `local`, verified by size and sampled ranges. A dropped leg
    resumes from the bytes already on disk; a leg never reads past `size`, so a NAS file that grew
    meanwhile costs one download and a refused verify, not a dozen. Returns the local path."""
    path = _ftp_path(host_path)

    def have_fn():
        have = os.path.getsize(local) if os.path.exists(local) else 0
        if have > size:
            os.remove(local)
            have = 0
        return have

    def run_leg(have):
        ftp = _connect()
        try:
            relink = _relinker(ftp)
            ftp.voidcmd("TYPE I")
            conn = ftp.transfercmd("RETR " + path, rest=have)
            left = [size - have]

            def read(n):
                if left[0] <= 0:
                    return b""
                b = conn.recv(min(n, left[0]))
                left[0] -= len(b)
                return b
            try:
                with open(local, "ab") as fh:
                    _leg(read, fh.write, moved0=have, total=size, abort=abort, limit=limit,
                         on_progress=on_progress, relink=relink)
            finally:
                conn.close()
        finally:
            _close(ftp, hard=True)           # a RETR stopped at `size` may not have ended

    _legs(run_leg, have_fn, size, abort)
    if not os.path.exists(local) or os.path.getsize(local) != size:
        raise RuntimeError("download incomplete" + _last_leg_error())
    try:
        verify_copy(local, host_path)
    except Mismatch:
        os.remove(local)                   # a bad copy must not be resumed onto next time
        raise RuntimeError("the downloaded copy does not match the NAS file")
    return local


def upload(local, stage_host, *, abort=None, limit=None, on_progress=None):
    """Resumable push of `local` to a NAS staging path, verified by size and sampled ranges."""
    size = os.path.getsize(local)
    path = _ftp_path(stage_host)
    folder = path.rsplit("/", 1)[0]

    def have_fn():
        def go(ftp):
            have = _size(ftp, path) or 0
            if have > size:
                ftp.delete(path)
                have = 0
            return have
        return _short(go)

    def run_leg(have):
        ftp = _connect()
        hard = True
        try:
            if abort is not None and abort.is_set():
                raise Stopped("stopped")    # a removed movie's cleanup may already have run: make
                                            # nothing (no folder, no empty .part) behind it
            relink = _relinker(ftp)
            # Every leg, on its own connection: the folder is shared by every movie on the share,
            # and clearing out another movie's stage removes it whenever it is empty — which it is
            # until this STOR creates the file. A STOR into a missing folder is "553 Permission
            # denied", and with the folder made only once every first upload that started as the
            # other thread finished a download from the same share failed all its legs (2026-10-03).
            try:
                ftp.mkd(folder)
            except ftplib.error_perm:
                pass                        # 550: it is already there
            ftp.voidcmd("TYPE I")
            conn = ftp.transfercmd("STOR " + path, rest=have)
            try:
                with open(local, "rb") as fh:
                    fh.seek(have)
                    _leg(fh.read, conn.sendall, moved0=have, total=size, abort=abort, limit=limit,
                         on_progress=on_progress, relink=relink)
            finally:
                conn.close()
            ftp.voidresp()
            hard = False
        finally:
            _close(ftp, hard=hard)

    _legs(run_leg, have_fn, size, abort)
    st = stat(stage_host)
    if not st or st[0] != size:
        raise RuntimeError(f"upload incomplete ({st[0] if st else 0} of {size})" + _last_leg_error())
    try:
        verify_copy(local, stage_host)
    except Mismatch:
        _short(lambda ftp: ftp.delete(path))  # a bad copy must not be resumed onto next time
        raise RuntimeError("the uploaded copy does not match")


# ---- the swap --------------------------------------------------------------------------------

def swap(stage_host: str, host_path: str, *, expect_size: int, expect_mtime: int, new_size: int):
    """Replace `host_path` with the staged file in ONE rename, carrying the original's mode over.
    Refuses if the original changed since it was read. Verifies the final size. The rename is never
    retried: a rename whose reply was lost is recognized by the caller (dvlane.swapped_already)."""
    if share_root(stage_host) != share_root(host_path):
        raise RuntimeError("the staged file is on another share; a rename would not be atomic")
    st = stat(host_path)
    if not st:
        raise RuntimeError("the original is gone; not swapping")
    if (st[0], st[1]) != (expect_size, expect_mtime):
        raise RuntimeError("the original changed since it was read; not replacing it")
    old_mode, new_mode = mode_of(host_path), mode_of(stage_host)
    stage, final = _ftp_path(stage_host), _ftp_path(host_path)
    if old_mode and new_mode and old_mode != new_mode:
        _short(lambda ftp: ftp.sendcmd(f"SITE CHMOD {old_mode[-3:]} {stage}"))  # no brackets in it
    _short(lambda ftp: ftp.rename(stage, final), retry=False)
    after = stat(host_path)
    if not after or after[0] != new_size:
        raise RuntimeError("after the swap the NAS file has the wrong size")
    try:
        _short(lambda ftp: ftp.rmd(stage.rsplit("/", 1)[0]), retry=False)
    except ftplib.all_errors:
        pass                                # another movie's stage is still in it


def discard_stage(host_path: str, keep_dir=False):
    """Delete a movie's staged copy on the NAS, and the staging folder once it is empty — unless
    `keep_dir`: an upload is about to write into it."""
    stage = _ftp_path(stage_path_for(host_path))

    def go(ftp):
        try:
            ftp.delete(stage)
        except ftplib.error_perm:
            pass                            # 550: nothing staged
        if keep_dir:
            return
        try:
            ftp.rmd(stage.rsplit("/", 1)[0])
        except ftplib.all_errors:
            pass                            # not empty, or the folder is already gone
    _short(go)
