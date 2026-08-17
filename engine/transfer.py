"""Pipeline endpoints over FTP: download a source from the NAS to local scratch
(start), upload the finished master back (end), and list NAS dirs for the picker.

WHY FTP, NOT SSH: SSH to this NAS times out unreliably over time. FTP is UGOS's
native file service (Control Panel → File Service → FTP, daemon `smbftpd` on :21,
reachable LAN + Tailscale). It also fixes two SSH pain points for free:
  * spacey/parens paths ("The Office  …(Extended Cut).mp4") work natively — FTP
    sends the path as the rest of the command line, no shell, no quoting.
  * files land owned **uid 1000 / gid 10** (the user's gid) with **umask 007**
    (group-readable) — exactly the Plex/gid-10 ownership, with NO chown step.

FTP exposes UGOS SHARES at the root: `/volume1/Media` → FTP `/Media`, so the TV
library is `/Media/TV-Shows`. Credentials come from env (`TOPAZ_NAS_FTP_*`) or
`~/.topaz-pipeline/config.json` — the USER supplies the password; it is never
hard-coded here.
"""
from __future__ import annotations
import ftplib
import json
import os

CONFIG_FILE = os.path.expanduser("~/.topaz-pipeline/config.json")
# THE BUILT-IN LAYOUT — the fallback, and the exact behavior before media folders became
# configurable. First-listed wins a name collision, so the primary volume takes priority
# (it holds the fuller copy).
DEFAULT_TV_ROOTS = ["/Media/TV-Shows", "/MediaVolume2/TV-Shows", "/MediaVolume3/TV-Shows"]
DEFAULT_MOVIE_ROOTS = ["/Media/Movies", "/MediaVolume2/Movies", "/MediaVolume3/Movies"]
DEFAULT_YOUTUBE_ROOT = "/Media/YouTube"
DEFAULT_YOUTUBE_STAGING = "/Media/YouTube-raw"


def _roots_from_config(kind):
    """APPLIED media roots for `kind` from config.json (written by Setup after Plex
    detection), or None. Read here — not imported from medialibs — so transfer stays
    import-cycle-free and works with no Plex at all."""
    try:
        v = (_config().get("media_roots") or {}).get(kind)
    except Exception:
        return None
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return None
    paths = [str(x).rstrip("/") for x in v if str(x or "").startswith("/")]
    return paths or None


def _resolve_roots(kind, env_single, env_list, defaults):
    """Precedence: explicit env override > config (Plex-detected/user-applied) > built-in.
    Env stays first so an operator can always force a layout without touching config."""
    if os.environ.get(env_list):
        return [r.strip() for r in os.environ[env_list].split(",") if r.strip()]
    if os.environ.get(env_single):
        return [os.environ[env_single].rstrip("/")]
    return _roots_from_config(kind) or list(defaults)


NAS_FTP_TV_ROOTS = _resolve_roots("tv", "TOPAZ_NAS_FTP_TV", "TOPAZ_NAS_FTP_TV_ROOTS",
                                  DEFAULT_TV_ROOTS)
NAS_FTP_TV_ROOT = NAS_FTP_TV_ROOTS[0]
NAS_FTP_MOVIES_ROOTS = _resolve_roots("movie", "TOPAZ_NAS_FTP_MOVIES",
                                      "TOPAZ_NAS_FTP_MOVIES_ROOTS", DEFAULT_MOVIE_ROOTS)
NAS_FTP_MOVIES_ROOT = NAS_FTP_MOVIES_ROOTS[0]
# FOLDER-SPLIT. The Plex "YouTube" library is _ROOT — ONLY finished 4K DV masters land here.
# youtarr's raw 1080p downloads go to _STAGING, a folder that is NOT a Plex library, so raw
# downloads never show up in Plex (youtarr's data mount points at _STAGING; see the compose .env).
# Visionary reads sources from _STAGING and publishes masters into _ROOT (mirrored path).
NAS_FTP_YOUTUBE_ROOT = _resolve_roots("youtube", "TOPAZ_NAS_FTP_YOUTUBE", "",
                                      [DEFAULT_YOUTUBE_ROOT])[0]
NAS_FTP_YOUTUBE_STAGING = _resolve_roots("youtube_staging", "TOPAZ_NAS_FTP_YOUTUBE_STAGING",
                                         "", [DEFAULT_YOUTUBE_STAGING])[0]
MEDIA_OWNER = "1000:10"   # what FTP yields automatically (user gid 10 + umask 007)
# A connection's timeout also becomes the per-block read/write timeout for RETR/STOR. The
# 15 s default (fine for quick listings) would abort a multi-GB transfer on any brief
# stall, so downloads/uploads use a generous one.
TRANSFER_TIMEOUT = 300


def _config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def nas_hosts() -> list:
    """The NAS host(s) to try IN ORDER — from env/config ONLY (no baked-in defaults).
    Typical setup: a VPN/Tailscale IP first (works home + away), then a LAN .local name.
    `TOPAZ_NAS_FTP_HOST` env / config `ftp_host` force a single host; config `ftp_hosts`
    sets the ordered list. Shared by FTP, Plex, and Youtarr URL resolution."""
    forced = os.environ.get("TOPAZ_NAS_FTP_HOST") or _config().get("ftp_host")
    if forced:
        return [forced]
    cfg = _config().get("ftp_hosts")
    if cfg:
        return list(cfg)
    return []


def ftp_settings() -> dict:
    """Port/user/passwd from env first, then ~/.topaz-pipeline/config.json."""
    c = _config()
    return {
        "port": int(os.environ.get("TOPAZ_NAS_FTP_PORT") or c.get("ftp_port") or 21),
        "user": os.environ.get("TOPAZ_NAS_FTP_USER") or c.get("ftp_user") or "",
        "passwd": os.environ.get("TOPAZ_NAS_FTP_PASS") or c.get("ftp_pass") or "",
    }


def ftp_hosts() -> list:
    """Hosts to try IN ORDER (see nas_hosts)."""
    return nas_hosts()


def connect(timeout=15):
    """Open an FTP connection, trying each host in order."""
    s = ftp_settings()
    hosts = ftp_hosts()
    if not hosts:
        raise ftplib.error_perm(
            "no NAS FTP host configured — set `ftp_host` (or `ftp_hosts`) in "
            "~/.topaz-pipeline/config.json, or export TOPAZ_NAS_FTP_HOST")
    last = None
    for host in hosts:
        try:
            ftp = ftplib.FTP()
            # latin-1 decodes ANY byte 0-255 without error and round-trips bytes
            # exactly, so a filename with a stray non-UTF-8 byte (e.g. 0xa1 '¡' in
            # an episode title) can't crash mlsd()/listings — and RETR/STOR/SIZE
            # still send the original bytes back. (Default utf-8 raises on 0xa1.)
            ftp.encoding = "latin-1"
            ftp.connect(host, s["port"], timeout=timeout)
            ftp.login(s["user"], s["passwd"])
            ftp.set_pasv(True)   # passive (smbftpd PassiveModePortRange 40000-50000)
            return ftp
        except ftplib.all_errors as e:
            last = e
    raise last or ftplib.error_temp("no FTP host reachable")


def display_name(s):
    """Human-readable form of an FTP wire name, for DISPLAY ONLY. The NAS disk is clean
    UTF-8, but smbftpd (UGREEN) converts filenames to GB18030 on the wire, and connect()
    deliberately reads latin-1 so any byte round-trips (RETR/STOR/SIZE need the exact wire
    bytes back — NEVER feed this function's output to a path or action key). Pure-ASCII
    names pass through untouched; wire GB18030 sequences (e.g. '£¢' = ＂, '¡¯' = ’) decode
    to their true characters; anything that doesn't decode cleanly is returned as-is."""
    if not s or not isinstance(s, str):
        return s
    try:
        b = s.encode("latin-1")     # recover the wire bytes
    except UnicodeEncodeError:
        return s                    # already real unicode (not a wire name)
    for enc in ("utf-8", "gb18030"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return s


def remote_size(ftp, path):
    try:
        ftp.voidcmd("TYPE I")
        return ftp.size(path)
    except ftplib.all_errors:
        return None


def remote_mtime(ftp, path):
    """The file's modification time as a sortable int YYYYMMDDHHMMSS (from MDTM), or None.
    Used for newest-first ordering of YouTube videos (youtarr sets it at download time)."""
    try:
        r = ftp.sendcmd("MDTM " + path)          # "213 20260630181500"
        return int(r.split()[1]) if r[:3] == "213" else None
    except (ftplib.all_errors, ValueError, IndexError):
        return None


def ftp_listdir(ftp, path) -> list:
    """Entry basenames in an FTP dir (MLSD preferred, NLST fallback)."""
    try:
        return [n for n, _ in ftp.mlsd(path) if n not in (".", "..")]
    except ftplib.all_errors:
        try:
            return [p.rsplit("/", 1)[-1] for p in ftp.nlst(path)]
        except ftplib.all_errors:
            return []


# Any container ffmpeg can decode is fine — the pipeline re-encodes to a CFR intermediate first.
_VIDEO_EXT = (".mp4", ".mkv", ".mov", ".m4v", ".ts", ".m2ts", ".mts", ".avi", ".wmv",
              ".webm", ".mpg", ".mpeg", ".vob", ".flv", ".ogv", ".m2v", ".divx", ".mpv")


def ftp_walk_files(ftp, base, depth=3, with_dirs=False) -> list:
    """Recursively collect file basenames under an FTP dir (series → season → files).
    Uses MLSD types when the server supports them; otherwise falls back to NLST and
    guesses dir-vs-file by extension (smbftpd may not implement MLSD).

    `with_dirs=True` returns (containing_dir, name) pairs instead of bare names. The
    caller needs that because season folders are NOT reliably named `S01`: real
    libraries carry things like `Season 1` or
    `Arrested Development Season 2 S02 1080p BluRay x264-BiA`. Synthesizing the season
    path from the episode number silently 550s on those shows (live-caught 2026-07-30)."""
    out = []
    if depth < 0:
        return out
    hit = (lambda d, n: out.append((d, n))) if with_dirs else (lambda d, n: out.append(n))
    base = base.rstrip("/")
    try:
        entries = list(ftp.mlsd(base))
    except ftplib.all_errors:
        entries = None
    if entries:
        for name, facts in entries:
            if name in (".", ".."):
                continue
            t = facts.get("type")
            if t == "dir":
                out.extend(ftp_walk_files(ftp, base + "/" + name, depth - 1, with_dirs))
            elif t == "file":
                hit(base, name)
    else:
        for name in ftp_listdir(ftp, base):     # NLST fallback (no types)
            if name.lower().endswith(_VIDEO_EXT):
                hit(base, name)                 # a video file
            else:
                out.extend(ftp_walk_files(ftp, base + "/" + name, depth - 1, with_dirs))
    return out


class _Aborted(Exception):
    """Raised from the retrbinary callback to interrupt a download when the run is
    stopped — so the blocking RETR returns at once instead of finishing the file."""


def download_head(remote_path, local_file, max_bytes, *, timeout=None):
    """The FIRST `max_bytes` of a NAS file → `local_file`, for header probing
    (companion combine: MKV track/codec/DV metadata lives at the front). Aborting
    RETR mid-stream is the POINT here — the truncation is deliberate, so unlike
    download() there is no size verify. Returns (ok, reason)."""
    os.makedirs(os.path.dirname(local_file) or ".", exist_ok=True)
    try:
        ftp = connect(timeout=timeout or TRANSFER_TIMEOUT)
    except ftplib.all_errors as e:
        return False, f"FTP connect/login failed: {e}"
    got = 0
    try:
        with open(local_file, "wb") as f:
            def _write(block):
                nonlocal got
                f.write(block)
                got += len(block)
                if got >= max_bytes:
                    raise _Aborted()
            try:
                ftp.retrbinary("RETR " + remote_path, _write)
            except _Aborted:
                pass                      # deliberate early stop — the head is complete
        if got <= 0:
            return False, "no bytes read"
        return True, f"read {got} bytes"
    except ftplib.all_errors as e:
        # small files can finish BEFORE max_bytes: that path returns above; here the
        # transfer itself failed
        return False, f"head read failed: {e}"
    finally:
        # the deliberate mid-RETR abort leaves the control connection wedged — close
        # hard, never quit()
        try: ftp.close()
        except Exception: pass


def download(remote_path, local_dir, *, timeout=None, on_progress=None, abort=None):
    """Pull a source from the NAS via FTP. Returns (ok, local_path, reason).
    on_progress(done_bytes, total_bytes) fires as blocks arrive, for a live % bar.
    `abort` (a threading.Event) interrupts the transfer mid-block when the run stops;
    on abort the caller deletes the partial so it can't be mistaken for a full source."""
    os.makedirs(local_dir, exist_ok=True)
    local = os.path.join(local_dir, os.path.basename(remote_path))
    try:
        ftp = connect(timeout=timeout or TRANSFER_TIMEOUT)
    except ftplib.all_errors as e:
        return False, local, f"FTP connect/login failed: {e}"
    try:
        total = remote_size(ftp, remote_path)   # the % denominator AND the verify target
        done = 0
        with open(local, "wb") as f:
            def _write(block):
                nonlocal done
                if abort is not None and abort.is_set():
                    raise _Aborted()
                f.write(block)
                done += len(block)
                if on_progress and total:
                    on_progress(done, total)
            ftp.retrbinary("RETR " + remote_path, _write)
        lsz = os.path.getsize(local)
        # REQUIRE a verified full-size match. A dropped data connection can end RETR
        # cleanly with a PARTIAL file; if we can't confirm the size we must NOT accept
        # it — an unverified/short download surfaces later as a corrupt "moov atom not
        # found" at the Topaz stage (that's exactly what happened to S02E17).
        if total is None:
            return False, local, f"size unverifiable (SIZE failed); got {lsz} bytes — rejecting"
        if total != lsz:
            return False, local, f"PARTIAL download: remote {total} != local {lsz} bytes"
        return True, local, f"downloaded {lsz} bytes (size-verified)"
    except _Aborted:
        return False, local, "aborted mid-download"
    except ftplib.all_errors as e:
        return False, local, f"download failed: {e}"
    finally:
        try: ftp.quit()
        except ftplib.all_errors: pass


def upload(local_file, remote_dir, *, timeout=None, on_progress=None):
    """Push the finished file to the NAS via FTP (spacey paths OK, no quoting).
    smbftpd writes it as uid 1000 / gid 10 (umask 007) — Plex-readable, no chown.
    on_progress(done_bytes, total_bytes) fires per block sent. Returns (ok, remote, reason)."""
    final = remote_dir.rstrip("/") + "/" + os.path.basename(local_file)
    lsz = os.path.getsize(local_file)
    try:
        ftp = connect(timeout=timeout or TRANSFER_TIMEOUT)
    except ftplib.all_errors as e:
        return False, final, f"FTP connect/login failed: {e}"
    try:
        # ALREADY SHIPPED? An upload cut AFTER its last byte (an app relaunch between the
        # transfer and the verify) leaves a complete master at the target; re-STORing it
        # can be refused outright (553 on an existing file, observed live) — and even when
        # allowed it re-sends gigabytes for nothing. An exact size match is the SAME
        # verification the fresh-STOR path requires below, so presence-at-size IS shipped.
        rs = remote_size(ftp, final)
        if rs == lsz:
            return True, final, f"already on the NAS at {lsz} bytes (size-verified) — STOR skipped"
        if rs is not None:
            # A PARTIAL from a cut transfer: clear it first, or the STOR itself may be
            # refused. Only ever this master's own filename at the target — never a source.
            try: ftp.delete(final)
            except ftplib.all_errors: pass
        done = 0
        def _sent(block):
            nonlocal done
            done += len(block)
            if on_progress and lsz:
                on_progress(done, lsz)
        with open(local_file, "rb") as f:
            ftp.storbinary("STOR " + final, f, callback=_sent)
        # REQUIRE a verified size match, same as download — never trust an unverifiable
        # upload (a truncated master we can't confirm must not be accepted as shipped).
        rs = remote_size(ftp, final)
        if rs is None:
            return False, final, f"upload size unverifiable (SIZE failed) — rejecting {lsz} bytes"
        if rs != lsz:
            return False, final, f"size mismatch after upload: remote {rs} != local {lsz}"
        return True, final, f"uploaded {lsz} bytes (FTP → owner {MEDIA_OWNER})"
    except ftplib.all_errors as e:
        return False, final, f"upload failed: {e}"
    finally:
        try: ftp.quit()
        except ftplib.all_errors: pass


def replace_original(master_remote, original_remote, local_master) -> tuple:
    """REPLACE step (per-item `replace_source` setting, default ON): permanently delete
    the superseded source `original_remote`, but ONLY after confirming the 4K
    `master_remote` is intact on the NAS (remote size == local size). The source is
    irreplaceable, so it is NEVER deleted unless the replacement is verified present
    and whole. Returns (ok, reason). (Removed 2026-07-16 for always-keep; restored
    2026-07-17 behind the per-show/movie setting.)"""
    try:
        ftp = connect()
    except ftplib.all_errors as e:
        return False, f"FTP connect/login failed: {e}"
    try:
        lsz = os.path.getsize(local_master)
        if remote_size(ftp, master_remote) != lsz:
            return False, "master NOT verified on NAS (size mismatch) — 1080p kept"
        if remote_size(ftp, original_remote) is None:
            return True, "no 1080p original present (nothing to replace)"
        ftp.delete(original_remote)
        return True, "replaced — deleted 1080p original"
    except ftplib.all_errors as e:
        return False, f"replace-delete failed: {e}"
    finally:
        try: ftp.quit()
        except ftplib.all_errors: pass


def _rmtree(ftp, path):
    path = path.rstrip("/")
    try:
        entries = list(ftp.mlsd(path))
    except ftplib.all_errors:
        entries = [(n, {}) for n in ftp_listdir(ftp, path)]
    for name, facts in entries:
        if name in (".", ".."):
            continue
        full = path + "/" + name
        t = facts.get("type")
        if t == "dir":
            _rmtree(ftp, full)
        elif t == "file":
            ftp.delete(full)
        else:                                   # NLST (no type) — try file, else recurse as a dir
            try:
                ftp.delete(full)
            except ftplib.all_errors:
                _rmtree(ftp, full)
    ftp.rmd(path)


def _safe_delete_path(remote_dir) -> bool:
    """A path is safe to recursively delete only if it's an ABSOLUTE, already-NORMALIZED path at least
    3 levels deep — so a traversal segment ('..', '.') or a near-root/relative path can NEVER slip
    through the guard and delete a library root or escape the intended folder."""
    import posixpath
    p = (remote_dir or "").rstrip("/")
    return bool(p) and p.startswith("/") and posixpath.normpath(p) == p and p.count("/") >= 2


def delete_tree(remote_dir) -> bool:
    """Recursively delete an FTP directory (its files + subdirs + itself). Returns True on success.
    Used to remove a downloaded YouTube video's/channel's folder. REFUSES traversal ('..'), relative,
    or near-root paths — a wrong recursive delete here would nuke a whole media library."""
    if not _safe_delete_path(remote_dir):
        return False
    try:
        ftp = connect(timeout=30)
    except ftplib.all_errors:
        return False
    try:
        _rmtree(ftp, remote_dir)
        return True
    except ftplib.all_errors:
        return False
    finally:
        try: ftp.quit()
        except ftplib.all_errors: pass

# ---- folder-split publish (YouTube: staging source -> Plex library master) ---

_SIDECAR_SKIP = (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".ts", ".m2ts", ".mts", ".avi", ".mpv",
                 ".part", ".upscaling", ".ytdl")   # videos + transient junk are NOT sidecars


def _makedirs(ftp, remote_dir) -> None:
    """mkdir -p over FTP: create each path component, ignoring 'already exists'."""
    cur = ""
    for part in remote_dir.strip("/").split("/"):
        cur += "/" + part
        try:
            ftp.mkd(cur)
        except ftplib.all_errors:
            pass                                   # exists (or a race) — fine


def _copy_sidecars(ftp, src_dir, dst_dir, scratch_dir) -> int:
    """Copy the SIDECAR files (everything that isn't a video/transient — .nfo/.jpg/.srt/.vtt/…)
    from src_dir to dst_dir via a scratch round-trip (FTP has no server-side copy). Best-effort;
    a sidecar that fails to copy is skipped (Plex just lacks that one asset). Returns the count."""
    if not src_dir:
        return 0
    copied = 0
    for name in ftp_listdir(ftp, src_dir):
        if name.startswith(".") or name.lower().endswith(_SIDECAR_SKIP):
            continue
        tmp = os.path.join(scratch_dir, "_sidecar_" + name)
        try:
            with open(tmp, "wb") as f:
                ftp.retrbinary("RETR " + src_dir + "/" + name, f.write)
            with open(tmp, "rb") as f:
                ftp.storbinary("STOR " + dst_dir + "/" + name, f)
            copied += 1
        except ftplib.all_errors:
            pass
        finally:
            try: os.remove(tmp)
            except OSError: pass
    return copied


def publish_master(local_master, master_remote, sidecar_src_dir, scratch_dir, *,
                   timeout=None, on_progress=None) -> tuple:
    """Publish a finished master into a NEW library path (folder-split YouTube): make the dest dir
    tree, STOR the master (size-verified — never accept an unverifiable upload), then copy the
    source folder's SIDECARS alongside it so Plex keeps youtarr's .nfo/thumbnail/subs. Does NOT
    touch the staging source (cleanup purges it). Returns (ok, master_remote, reason)."""
    lsz = os.path.getsize(local_master)
    dest_dir = os.path.dirname(master_remote)
    try:
        ftp = connect(timeout=timeout or TRANSFER_TIMEOUT)
    except ftplib.all_errors as e:
        return False, master_remote, f"FTP connect/login failed: {e}"
    try:
        _makedirs(ftp, dest_dir)
        done = 0
        def _sent(block):
            nonlocal done
            done += len(block)
            if on_progress and lsz:
                on_progress(done, lsz)
        with open(local_master, "rb") as f:
            ftp.storbinary("STOR " + master_remote, f, callback=_sent)
        rs = remote_size(ftp, master_remote)
        if rs is None:
            return False, master_remote, f"master upload unverifiable (SIZE failed) — rejecting {lsz} bytes"
        if rs != lsz:
            return False, master_remote, f"size mismatch after upload: remote {rs} != local {lsz}"
        copied = _copy_sidecars(ftp, sidecar_src_dir, dest_dir, scratch_dir)
        return True, master_remote, f"published {lsz} bytes + {copied} sidecar(s) (owner {MEDIA_OWNER})"
    except ftplib.all_errors as e:
        return False, master_remote, f"publish failed: {e}"
    finally:
        try: ftp.quit()
        except ftplib.all_errors: pass
