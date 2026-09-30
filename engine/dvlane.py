"""The Dolby Vision profile 7 -> 8.1 lane: queued movies converted and replaced IN PLACE on the NAS.

A profile 7 movie has already proven it can carry Dolby Vision — it IS Dolby Vision — so it never
goes near Topaz, Resolve or the capped x265 re-encode (user-dictated 2026-09-29: "NONE of them
should have to re-encode their hdr video"). dvp7.py copies the HDR10 video bit for bit and rewrites
only the RPU. That makes this a transfer job, not a GPU job, so it runs BESIDE the TV pipeline in
its own two threads instead of taking the run thread's turn:

  fetch:  pull the original over SSH (resumable) -> dvp7.convert (inspect, build, verify)
  ship:   push the new file to a staging folder on the SAME NAS volume -> the NAS's own ffprobe
          must read profile 8 there -> one rename over the original (same name, owner and mode;
          refused if the original changed since it was read) -> Plex rescans the folder and
          re-analyzes the item

so the next movie downloads while the last one uploads. No backup is kept (user-dictated
2026-09-27); the original is only replaced after the new file has been verified twice, on the Mac
and again where it sits on the NAS.

THE NAS COMES FIRST while anyone is watching Plex (user rule 2026-09-27: "every single precaution
should be taken on the nas so that the cpu doesn't spike"). Every transfer is throttled to
THROTTLE_BPS whenever Plex has ANY session open (a paused viewer resumes without warning, and an
unreachable Plex counts as someone watching); a file is never renamed while someone is playing
THAT file; and the Plex rescan waits until nobody is streaming.

THE MAC'S DISK belongs to the TV pipeline first. A movie is only fetched when its whole local
footprint (source + bare video + new file, SIZE_FACTOR x its size) fits ABOVE Visionary's own
free-space floor (the min_free_gb setting), so a conversion can never be what pauses an upscale for
disk. The working files live in WORK_ROOT, outside topaz-scratch, because the pipeline counts
everything in scratch as its own reclaimable space.

Every step is resumable: the phase is on disk (dvbook), a transfer resumes from its bytes, a
conversion restarts, and a swap that happened just before a crash is recognized from the sizes.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time

import dvbook
import dvp7
import logbook
import nas_ssh
import plex

WORK_ROOT = (os.path.join(tempfile.gettempdir(), "visionary-test-dvlane")
             if "unittest" in sys.modules else os.path.expanduser("~/topaz-dv-convert"))
THROTTLE_BPS = 25_000_000       # per transfer while anyone has a Plex session open
SIZE_FACTOR = 2.8               # peak local footprint: the source + its bare video + the new file
IDLE_POLL_SECS = 30
PLEX_CACHE_SECS = 5             # both threads and every leg ask; Plex is asked at most this often
SHIP_BACKLOG = 1                # converted files allowed to wait for the ship thread


def work_dir(host: str) -> str:
    return os.path.join(WORK_ROOT, hashlib.sha1(host.encode()).hexdigest()[:12])


def local_bytes(host: str) -> int:
    """What this movie already has on the Mac's disk (its part-downloaded source, a new file)."""
    total = 0
    for root, _dirs, files in os.walk(work_dir(host)):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def disk_need(size: int, have: int) -> int:
    """PURE: bytes a movie of `size` still needs on the Mac, given `have` already there."""
    return max(0, int(SIZE_FACTOR * int(size or 0)) - int(have or 0))


def fits(size: int, have: int, free: int, floor: int) -> bool:
    """PURE: may a movie start (or resume) without pushing free space below the pipeline's floor?"""
    return free - disk_need(size, have) >= floor


def throttle_for(detail):
    """PURE: the transfer cap for a Plex session snapshot. None (Plex unreachable) counts as someone
    watching — the safe answer when the NAS may be serving a stream."""
    if detail is None or detail.get("count", 0) > 0:
        return THROTTLE_BPS
    return None


def swapped_already(nas_size, stage_exists: bool, size_in: int, size_out: int) -> bool:
    """PURE: after a crash in the swap phase — did the rename already happen? Only when the staged
    file is gone AND the NAS file now has the new file's size (the sizes always differ: the
    conversion rewrites every RPU and drops the enhancement layer)."""
    return (not stage_exists) and nas_size == size_out and size_out != size_in


def _floor_bytes() -> int:
    try:
        import settings
        return int(settings.tunable("min_free_gb")) * 1024 ** 3
    except Exception:  # noqa: BLE001 — the shipped default
        return 400 * 1024 ** 3


class _Cancelled(Exception):
    pass


class Lane:
    def __init__(self):
        self._lock = threading.RLock()
        self._abort = threading.Event()
        self._threads = {}
        self._events = {}                 # name -> Event: set to stop work on that movie
        self._cancelled = set()
        self._failed_now = set()          # failed this run: not retried until re-added or restart
        self._plex = (0.0, None)
        self._plex_lock = threading.Lock()
        self._status = {"fetch": None, "ship": None}
        self._note = None

    # ---- lifecycle -----------------------------------------------------------------------

    def start(self):
        if "unittest" in sys.modules:
            return            # a test run arms the orchestrator for real; it must never reach the NAS
        with self._lock:
            self._abort.clear()
            self._failed_now.clear()
            for name, target in (("fetch", self._fetch_loop), ("ship", self._ship_loop)):
                t = self._threads.get(name)
                if not (t and t.is_alive()):
                    t = threading.Thread(target=self._guard, args=(name, target), daemon=True,
                                         name=f"dvlane-{name}")
                    self._threads[name] = t
                    t.start()

    def stop(self):
        with self._lock:
            self._abort.set()
            for ev in self._events.values():
                ev.set()

    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads.values()) and not self._abort.is_set()

    def _guard(self, name, target):
        try:
            self._sweep_orphans()
            target()
        except Exception as e:  # noqa: BLE001 — never die silently
            logbook.exception(f"dv lane {name}", e)
        finally:
            self._status[name] = None

    # ---- the UI's view (poll-safe: two small local files, no NAS I/O) ---------------------

    def status(self) -> dict:
        q = dvbook.queue()
        def pub(st):
            return {k: v for k, v in st.items() if not k.startswith("_")} if st else None
        return {"running": self.running(), "note": self._note,
                "fetch": pub(self._status.get("fetch")), "ship": pub(self._status.get("ship")),
                "summary": dvbook.summary(),
                "queue": [{k: e.get(k) for k in ("name", "title", "bytes", "state", "phase", "el",
                                                 "error", "size_out", "plex_pending")}
                          for e in q]}

    # ---- queue control -------------------------------------------------------------------

    def remove(self, name: str) -> bool:
        """Take a movie off the queue. Work in flight on it stops first; its local files and any
        staged copy on the NAS are deleted. The original on the NAS is never touched here."""
        e = dvbook.entry(name)
        if not e:
            return False
        if e.get("state") == dvbook.DONE:
            return dvbook.remove(name)          # finished: only the record goes
        with self._lock:
            self._cancelled.add(name)
            ev = self._events.get(name)
            if ev:
                ev.set()
        deadline = time.time() + 30
        while name in self._events and time.time() < deadline:
            time.sleep(0.5)                     # the worker notices within a couple of seconds
        if e.get("phase") == "swap" and name in self._events:
            return False                        # never pull the files out from under a rename
        self._clean(e)
        dvbook.remove(name)
        with self._lock:
            self._cancelled.discard(name)
        return True

    def retry(self, name: str) -> bool:
        e = dvbook.entry(name)
        if not e or e.get("state") != dvbook.FAILED:
            return False
        self._failed_now.discard(name)
        dvbook.update(name, state=dvbook.PENDING, phase=None, error=None)
        return True

    # ---- helpers -------------------------------------------------------------------------

    def _plex_detail(self):
        with self._plex_lock:
            at, d = self._plex
            if time.monotonic() - at < PLEX_CACHE_SECS:
                return d
            try:
                d = plex.session_detail()
            except Exception:  # noqa: BLE001
                d = None
            self._plex = (time.monotonic(), d)
            return d

    def _limit(self):
        return throttle_for(self._plex_detail())

    def _event(self, name) -> threading.Event:
        with self._lock:
            ev = threading.Event()
            if self._abort.is_set() or name in self._cancelled:
                ev.set()
            self._events[name] = ev
            return ev

    def _release(self, name):
        with self._lock:
            self._events.pop(name, None)

    def _check(self, name, ev):
        if name in self._cancelled:
            raise _Cancelled(name)
        if self._abort.is_set() or ev.is_set():
            raise nas_ssh.Stopped("stopped")

    def _wait(self, secs) -> bool:
        """Sleep, waking early on stop. True if stopping."""
        return self._abort.wait(secs)

    def _set(self, lane, e, phase, done=None, total=None, note=None):
        cur = self._status.get(lane) or {}
        st = {"name": e["name"], "title": e.get("title") or e["name"], "phase": phase,
              "done": done, "total": total, "note": note,
              "throttled": bool(self._plex[1] is None or (self._plex[1] or {}).get("count"))}
        if cur.get("name") == e["name"] and cur.get("phase") == phase and done is not None:
            t0, b0 = cur.get("_t0"), cur.get("_b0")
            st["_t0"], st["_b0"] = t0, b0
            if t0 and time.time() - t0 > 3:
                st["rate"] = int((done - b0) / (time.time() - t0))
        else:
            st["_t0"], st["_b0"] = time.time(), done or 0
        self._status[lane] = st

    def _offline(self, ex) -> bool:
        """A step that failed because the NAS dropped off the network (asleep, the Mac away from
        home) is not the movie's fault: wait for the NAS instead of failing it."""
        if nas_ssh.reachable():
            return False
        self._note = f"waiting for the NAS (SSH unreachable: {str(ex)[:120]})"
        while not self._abort.is_set() and not nas_ssh.reachable():
            if self._wait(IDLE_POLL_SECS):
                break
        self._note = None
        return True

    def _fail(self, e, why):
        self._failed_now.add(e["name"])
        dvbook.update(e["name"], state=dvbook.FAILED, error=str(why)[:400], failed=int(time.time()))
        logbook.failure(f"DV 7->8.1 {e.get('title') or e['name']}: {why}")

    def _clean(self, e):
        shutil.rmtree(work_dir(e["host"]), ignore_errors=True)
        if e.get("phase") in ("upload", "swap"):
            try:
                nas_ssh.discard_stage(e["host"])
            except Exception:  # noqa: BLE001 — a stray .part in _claude-tmp is harmless
                pass

    def _sweep_orphans(self):
        """Working folders of movies no longer queued (removed while the app was down)."""
        keep = {os.path.basename(work_dir(e["host"])) for e in dvbook.queue()
                if e.get("state") in (dvbook.PENDING, dvbook.ACTIVE) and e.get("host")}
        try:
            names = os.listdir(WORK_ROOT)
        except OSError:
            return
        for n in names:
            if n not in keep:
                shutil.rmtree(os.path.join(WORK_ROOT, n), ignore_errors=True)

    # ---- fetch: download + convert -------------------------------------------------------

    def _pick_fetch(self):
        """The first not-yet-converted movie whose footprint fits above the pipeline's disk floor.
        A movie already part-downloaded is preferred — its bytes are sunk."""
        todo = dvbook.open_entries(phases=(None, "download", "convert"),
                                   skip=self._failed_now | self._cancelled)
        if not todo:
            self._note = None
            return None
        waiting = len(dvbook.open_entries(phases=("converted",)))
        if waiting >= SHIP_BACKLOG:
            self._note = "a converted movie is waiting to upload"
            return None
        os.makedirs(WORK_ROOT, exist_ok=True)
        free, floor = shutil.disk_usage(WORK_ROOT).free, _floor_bytes()
        todo.sort(key=lambda e: 0 if e.get("phase") else 1)       # stable: queue order otherwise
        for e in todo:
            if fits(e.get("bytes") or 0, local_bytes(e["host"]), free, floor):
                self._note = None
                return e
        need = disk_need(todo[0].get("bytes") or 0, local_bytes(todo[0]["host"]))
        self._note = (f"waiting for disk: {todo[0].get('title')} needs {need / 1e9:.0f} GB above "
                      f"Visionary's {floor / 1024 ** 3:.0f} GB floor ({free / 1e9:.0f} GB free)")
        return None

    def _fetch_loop(self):
        while not self._abort.is_set():
            e = self._pick_fetch()
            if not e:
                self._status["fetch"] = None
                if self._wait(IDLE_POLL_SECS):
                    return
                continue
            ev = self._event(e["name"])
            try:
                self._fetch(e, ev)
            except _Cancelled:
                pass
            except (nas_ssh.Stopped, dvp7.Aborted):
                if self._abort.is_set():
                    return
            except Exception as ex:  # noqa: BLE001
                if not self._offline(ex):
                    self._fail(e, ex)
            finally:
                self._release(e["name"])
                self._status["fetch"] = None

    def _fetch(self, e, ev):
        name, host, size = e["name"], e["host"], int(e.get("bytes") or 0)
        d = work_dir(host)
        src, out = os.path.join(d, "source.mkv"), os.path.join(d, "p81.mkv")
        os.makedirs(d, exist_ok=True)
        self._set("fetch", e, "checking")
        st = nas_ssh.stat(host)
        if not st:
            raise RuntimeError(f"not on the NAS any more: {host}")
        if st[0] != size:
            if nas_ssh.remote_dv_profile(host) == 8:
                dvbook.record_profile(name, st[0], 8, src="probe", host=host)
                dvbook.update(name, state=dvbook.DONE, phase=None, note="already profile 8",
                              finished=int(time.time()))
                return
            raise RuntimeError(f"the NAS file is {st[0]} bytes, not the {size} that was classified")
        if e.get("expect_mtime") not in (None, st[1]):
            shutil.rmtree(d, ignore_errors=True)            # a partial of an older file: start over
            os.makedirs(d, exist_ok=True)
        dvbook.update(name, state=dvbook.ACTIVE, phase="download", expect_mtime=st[1], size_in=size,
                      started=e.get("started") or int(time.time()), error=None)
        self._check(name, ev)
        logbook.event(f"DV 7->8.1 {e.get('title')}: downloading {size / 1e9:.1f} GB")
        nas_ssh.download(host, src, size, abort=ev, limit=self._limit,
                         on_progress=lambda b, t: self._set("fetch", e, "download", b, t))
        self._check(name, ev)
        dvbook.update(name, phase="convert")
        # A fresh conversion makes any staged copy from an earlier attempt stale: mkvmerge writes a
        # new segment UID every time, so resuming an upload onto it would splice two files.
        nas_ssh.discard_stage(host)
        self._set("fetch", e, "convert", 0, 100)
        try:
            res = dvp7.convert(src, out, os.path.join(d, "work"), abort=ev,
                               progress=lambda pct: self._set("fetch", e, "convert", pct, 100))
        except dvp7.AlreadyP8:
            dvbook.record_profile(name, size, 8, src="rpu", host=host)
            dvbook.update(name, state=dvbook.DONE, phase=None, note="already profile 8",
                          finished=int(time.time()))
            shutil.rmtree(d, ignore_errors=True)
            return
        except dvp7.NotP7 as why:
            dvbook.record_profile(name, size, None, src="rpu", host=host)
            raise RuntimeError(str(why))
        # Record first, THEN drop the source: a crash between the two must not send a converted
        # movie back to be downloaded again.
        dvbook.update(name, phase="converted", size_out=res["size_out"], frames=res["frames"],
                      el=res.get("el") or e.get("el"), dual_track=res.get("dual_track"))
        os.remove(src)                     # the NAS original is still intact; the local copy is spent
        logbook.event(f"DV 7->8.1 {e.get('title')}: converted and verified "
                      f"({size / 1e9:.1f} -> {res['size_out'] / 1e9:.1f} GB)")

    # ---- ship: upload + swap + Plex ------------------------------------------------------

    def _pick_ship(self):
        todo = dvbook.open_entries(phases=("converted", "upload", "swap"),
                                   skip=self._failed_now | self._cancelled)
        todo.sort(key=lambda e: {"swap": 0, "upload": 1}.get(e.get("phase"), 2))
        return todo[0] if todo else None

    def _ship_loop(self):
        while not self._abort.is_set():
            self._flush_plex()
            e = self._pick_ship()
            if not e:
                self._status["ship"] = None
                if self._wait(IDLE_POLL_SECS):
                    return
                continue
            ev = self._event(e["name"])
            try:
                self._ship(e, ev)
            except _Cancelled:
                pass
            except (nas_ssh.Stopped, dvp7.Aborted):
                if self._abort.is_set():
                    return
            except Exception as ex:  # noqa: BLE001
                if not self._offline(ex):
                    self._fail(e, ex)
            finally:
                self._release(e["name"])
                self._status["ship"] = None

    def _ship(self, e, ev):
        name, host = e["name"], e["host"]
        out = os.path.join(work_dir(host), "p81.mkv")
        stage = nas_ssh.stage_path_for(host)
        size_in, size_out = int(e.get("size_in") or e.get("bytes") or 0), int(e.get("size_out") or 0)
        phase = e.get("phase")
        if phase == "swap":
            st, staged = nas_ssh.stat(host), nas_ssh.stat(stage)
            if swapped_already(st[0] if st else None, bool(staged), size_in, size_out):
                return self._finish(e)
            if not staged or staged[0] != size_out:
                phase = "converted"                     # the staged copy is gone: send it again
        if phase in ("converted", "upload"):
            if not (os.path.exists(out) and os.path.getsize(out) == size_out):
                dvbook.update(name, phase=None)         # the new file is gone: convert it again
                return
            dvbook.update(name, phase="upload")
            self._check(name, ev)
            logbook.event(f"DV 7->8.1 {e.get('title')}: uploading {size_out / 1e9:.1f} GB")
            nas_ssh.upload(out, stage, abort=ev, limit=self._limit,
                           on_progress=lambda b, t: self._set("ship", e, "upload", b, t))
            prof = nas_ssh.remote_dv_profile(stage)
            if prof != 8:
                nas_ssh.discard_stage(host)
                raise RuntimeError(f"the NAS reads DV profile {prof} in the staged file, not 8")
            dvbook.update(name, phase="swap")
        # The swap: never while someone is playing THIS file (their player holds the old one open,
        # and a seek after the rename would land in a different file).
        while True:
            self._check(name, ev)
            d = self._plex_detail()
            if d is not None and os.path.basename(host) not in d["files"]:
                break
            self._set("ship", e, "swap", note="waiting: this movie is playing on Plex")
            if self._wait(IDLE_POLL_SECS):
                raise nas_ssh.Stopped("stopped")
        self._set("ship", e, "swap")
        with self._lock:
            if name in self._cancelled:
                raise _Cancelled(name)
            nas_ssh.swap(stage, host, expect_size=size_in, expect_mtime=int(e["expect_mtime"]),
                         new_size=size_out)
        self._finish(e)

    def _finish(self, e):
        name, host = e["name"], e["host"]
        size_out = int(e.get("size_out") or 0)
        dvbook.record_profile(name, size_out, 8, src="rpu", host=host)
        dvbook.update(name, state=dvbook.DONE, phase=None, plex_pending=True,
                      finished=int(time.time()))
        shutil.rmtree(work_dir(host), ignore_errors=True)
        saved = int(e.get("size_in") or e.get("bytes") or 0) - size_out
        logbook.event(f"DV 7->8.1 {e.get('title')}: replaced on the NAS (same name), "
                      f"{saved / 1e9:.1f} GB smaller")
        self._flush_plex()

    def _flush_plex(self):
        """Tell Plex about every replaced file — once nobody is streaming."""
        pend = [e for e in dvbook.queue() if e.get("plex_pending")]
        if not pend:
            return
        d = self._plex_detail()
        if d is None or d["count"] > 0:
            return
        for e in pend:
            folder = nas_ssh.host_to_plex(os.path.dirname(e["host"]))
            ok = True
            if e.get("section") and folder:
                ok = plex.refresh_folder(e["section"], folder) and ok
            if e.get("rk"):
                ok = plex.analyze(e["rk"]) and ok
            if ok:
                dvbook.update(e["name"], plex_pending=False)


LANE = Lane()
