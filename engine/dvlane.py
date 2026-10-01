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

ONE TRANSFER AT A TIME (user-dictated 2026-09-30: "it should only upload/download one thing at a
time"): every download and upload passes through one first-come-first-served gate, so the NAS link
and the NAS's disks and sshd serve a single stream. Only the local conversion overlaps a transfer —
and first-come order is what makes that overlap happen: the fetch thread queues the next download
while an upload is still running, so it goes next, and the movie just downloaded converts while the
one before it uploads. No backup is kept (user-dictated 2026-09-27); the original is only replaced
after the new file has been verified twice, on the Mac and again where it sits on the NAS.

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

import collections
import contextlib
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
SHIP_BACKLOG = 2                # converted files allowed to wait for the ship thread: with one
                                # transfer at a time the next download goes while the last movie
                                # waits its turn to upload, so one must be allowed to wait
SHIP_TRIES = 5                  # attempts a movie with a verified new file gets before its files are
                                # deleted from this Mac (user, 2026-09-30: "try 5 times before deletion")
SHIP_RETRY_WAITS = (60, 300, 900, 1800)   # seconds before attempts 2..5: a blip clears in the first,
                                          # a NAS reboot or a full share has time by the last


def work_dir(host: str) -> str:
    return os.path.join(WORK_ROOT, hashlib.sha1(host.encode()).hexdigest()[:12])


def tree_bytes(path: str) -> int:
    """Total size of the files under `path` (0 when it does not exist)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def local_bytes(host: str) -> int:
    """What this movie already has on the Mac's disk (its part-downloaded source, a new file)."""
    return tree_bytes(work_dir(host))


def disk_need(size: int, have: int) -> int:
    """PURE: bytes a movie of `size` still needs on the Mac, given `have` already there."""
    return max(0, int(SIZE_FACTOR * int(size or 0)) - int(have or 0))


def fits(size: int, have: int, free: int, floor: int) -> bool:
    """PURE: may a movie start (or resume) without pushing free space below the pipeline's floor?"""
    return free - disk_need(size, have) >= floor


def throttle_for(detail, enabled=True):
    """PURE: the transfer cap for a Plex session snapshot. None (Plex unreachable) counts as someone
    watching — the safe answer when the NAS may be serving a stream. `enabled` is the Plex-throttle
    setting: off, transfers run at full speed whoever is watching."""
    if not enabled:
        return None
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


class _NoRoom(Exception):
    """The disk budget no longer fits once the transfer's turn came — back to waiting for disk."""


KEEP_ON_FAILURE = ("converted", "upload", "swap")   # a verified new file exists: worth a retry


class TransferGate:
    """At most one NAS transfer at a time, granted in the order it was asked for.

    First-come order, not a plain Lock: with a Lock whichever thread the OS wakes wins, so an upload
    could keep finishing and re-grabbing ahead of a download that has been waiting, or the other
    way round. In order, neither side can be starved. A waiter that is stopped or cancelled leaves
    the line without ever holding the gate."""

    def __init__(self):
        self._cv = threading.Condition()
        self._line = collections.deque()
        self._busy = False
        self.holder = None                 # what is transferring now, for the waiter's note

    def acquire(self, what: str, check=None, poll: float = 1.0):
        me = object()
        with self._cv:
            self._line.append(me)
            try:
                while self._busy or self._line[0] is not me:
                    if check:
                        check()            # raises to give up the place in line
                    self._cv.wait(poll)
            except BaseException:
                self._line.remove(me)
                self._cv.notify_all()
                raise
            self._line.popleft()
            self._busy, self.holder = True, what

    def release(self):
        with self._cv:
            self._busy, self.holder = False, None
            self._cv.notify_all()


class Lane:
    def __init__(self):
        self._lock = threading.RLock()
        self._abort = threading.Event()
        self._threads = {}
        self._events = {}                 # name -> Event: set to stop work on that movie
        self._cancelled = set()
        self._plex = (0.0, None)
        self._plex_lock = threading.Lock()
        self._status = {"fetch": None, "ship": None}
        self._note = None
        self._gate = TransferGate()
        self._throttle_cache = (0.0, True)
        self._cache = None                # (scratch folder fn, evict fn) — see use_cache
        self._active = None               # the movie the fetch thread is working on

    # ---- lifecycle -----------------------------------------------------------------------

    def start(self):
        if "unittest" in sys.modules:
            return            # a test run arms the orchestrator for real; it must never reach the NAS
        with self._lock:
            self._abort.clear()
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
            self._resume_kept_failures()
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
        import nas_link
        link = nas_link.last()
        if link:                                  # how old the answer is: the UI shows it only fresh
            link = dict(link, age=int(time.time() - (link.get("at") or 0)))
        return {"running": self.running(), "note": self._note, "ship_tries": SHIP_TRIES,
                "link": link,                     # the link the last connection chose (no probing)
                "fetch": pub(self._status.get("fetch")), "ship": pub(self._status.get("ship")),
                "summary": dvbook.summary(),
                "queue": [{k: e.get(k) for k in ("name", "title", "bytes", "state", "phase", "el",
                                                 "error", "size_out", "plex_pending", "tries",
                                                 "retry_at")}
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
        """Put a failed movie back. It resumes where it failed when that is safe — a staged copy on
        the NAS (swap) or a verified new file still on the Mac (converted/upload) — and starts over
        otherwise. Annihilation failed on one SSH blip with 43 GB converted and ready (2026-09-30);
        starting it over would have moved 90 GB again for nothing."""
        e = dvbook.entry(name)
        if e and e.get("state") == dvbook.ACTIVE and e.get("retry_at"):
            dvbook.update(name, retry_at=int(time.time()))     # waiting to try again: try now
            return True
        if not e or e.get("state") != dvbook.FAILED:
            return False
        out = os.path.join(work_dir(e["host"]), "p81.mkv")
        have_out = os.path.exists(out) and os.path.getsize(out) == int(e.get("size_out") or -1)
        if e.get("phase") == "swap" or (e.get("phase") in KEEP_ON_FAILURE and have_out):
            dvbook.update(name, state=dvbook.ACTIVE, error=None, tries=0, retry_at=None)
        else:
            shutil.rmtree(work_dir(e["host"]), ignore_errors=True)
            dvbook.update(name, state=dvbook.PENDING, phase=None, error=None, tries=0, retry_at=None)
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

    def _throttle_on(self) -> bool:
        """The Plex-throttle setting, re-read at most every PLEX_CACHE_SECS — a leg asks on every
        8 MiB chunk, far too often for a settings-file read each time."""
        at, on = self._throttle_cache
        if time.monotonic() - at >= PLEX_CACHE_SECS:
            try:
                import settings
                on = settings.plex_throttle()
            except Exception:  # noqa: BLE001 — unreadable: keep protecting the stream
                on = True
            self._throttle_cache = (time.monotonic(), on)
        return on

    def _limit(self):
        if not self._throttle_on():
            return None                       # off: no need to even ask Plex who is watching
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
              "throttled": self._throttle_on() and bool(self._plex[1] is None
                                                         or (self._plex[1] or {}).get("count"))}
        # The rate is measured from the first PROGRESS of this step. A status without progress (a
        # waiting note) must not start the clock at 0 bytes, or a resumed transfer would open with
        # its already-moved bytes counted as speed.
        if (cur.get("name") == e["name"] and cur.get("phase") == phase and done is not None
                and cur.get("_b0") is not None):
            t0, b0 = cur.get("_t0"), cur.get("_b0")
            st["_t0"], st["_b0"] = t0, b0
            if t0 and time.time() - t0 > 3:
                st["rate"] = int((done - b0) / (time.time() - t0))
        else:
            st["_t0"], st["_b0"] = time.time(), done
        self._status[lane] = st

    @contextlib.contextmanager
    def _transfer(self, lane, e, phase, ev):
        """Hold the one-transfer gate for a download or an upload. While waiting, the step says
        what it is waiting behind, and a stop or a cancel gives up its place in line."""
        title = e.get("title") or e["name"]
        verb = "download" if phase == "download" else "upload"

        def check():
            self._check(e["name"], ev)
            ahead = self._gate.holder
            self._set(lane, e, phase, note=(f"waiting for the {ahead} to finish — one transfer at "
                                            f"a time" if ahead else "waiting for its turn to transfer"))

        self._gate.acquire(f"{verb} of {title}", check)
        try:
            # A stop or cancel can land while the waiter sleeps and the holder lets go in the same
            # moment — the wait then ends without another check (review 2026-09-30). Re-check
            # holding the gate, and clear the "waiting" note: from here this step IS the transfer.
            self._check(e["name"], ev)
            self._set(lane, e, phase)
            yield
        finally:
            self._gate.release()

    def _offline(self, ex) -> bool:
        """A step that failed because the NAS dropped off the network (asleep, the Mac away from
        home) is not the movie's fault: wait for the NAS instead of failing it."""
        if nas_ssh.reachable():
            return False
        def why(ex):
            return ("waiting for an Ethernet link to the NAS (Settings: NAS network is Ethernet only)"
                    if isinstance(ex, nas_ssh.NoLink)
                    else f"waiting for the NAS (SSH unreachable: {str(ex)[:120]})")
        self._note = why(ex)
        while not self._abort.is_set() and not nas_ssh.reachable():
            # The reason follows what is true NOW (a cable plugged back in while the NAS is still
            # rebooting is no longer a cable problem — review 2026-09-30).
            if (nas_ssh.link() or {}).get("unavailable"):
                self._note = why(nas_ssh.NoLink(""))
            else:
                self._note = ("waiting for the NAS (it does not answer over SSH)"
                              if isinstance(ex, nas_ssh.NoLink) else why(ex))
            if self._wait(IDLE_POLL_SECS):
                break
        self._note = None
        return True

    def _fail(self, e, why):
        """A movie's step failed. One with a verified new file (converted, uploading, staged) tries
        again, resuming from that file, up to SHIP_TRIES attempts in all, after waits that let a
        blip clear (user, 2026-09-30: "try 5 times before deletion"). After the last attempt — or
        at once for a failure before a verified file existed (a part download, a half conversion)
        — its files are deleted from this Mac, and a staged copy from the NAS, and it waits as
        FAILED for a manual retry, which starts it over. Nothing is left holding disk."""
        name, title = e["name"], e.get("title") or e["name"]
        cur = dvbook.entry(name) or e
        if cur.get("phase") == "swap":
            landed = self._swap_landed(cur)
            if landed:
                # The rename ran but the session dropped before it could say so (it is never
                # retried on the NAS side): the new file is live — finish it, never count or
                # delete it.
                logbook.event(f"DV 7->8.1 {title}: the swap reported a failure ({why}) but the "
                              f"new file is in place on the NAS")
                return self._finish(cur)
            if landed is None and int(cur.get("tries") or 0) + 1 >= SHIP_TRIES:
                # The last attempt, and the NAS cannot say whether its rename landed: never give
                # up (and delete) on an unknown. The next attempt asks again (review 2026-09-30).
                wait = SHIP_RETRY_WAITS[-1]
                dvbook.update(name, state=dvbook.ACTIVE, retry_at=int(time.time()) + wait,
                              error=f"could not check whether the swap landed ({why})"[:400])
                logbook.event(f"DV 7->8.1 {title}: the swap failed ({why}) and the NAS cannot say "
                              f"whether it landed; asking again in {wait // 60} min")
                return
        if cur.get("phase") in KEEP_ON_FAILURE:
            tries = int(cur.get("tries") or 0) + 1
            if tries < SHIP_TRIES:
                wait = SHIP_RETRY_WAITS[min(tries, len(SHIP_RETRY_WAITS)) - 1]
                dvbook.update(name, state=dvbook.ACTIVE, tries=tries, error=str(why)[:400],
                              retry_at=int(time.time()) + wait)
                logbook.event(f"DV 7->8.1 {title}: attempt {tries} of {SHIP_TRIES} failed ({why}); "
                              f"trying again in {wait // 60} min")
                return
            why = (f"gave up after {SHIP_TRIES} attempts and deleted its files from this Mac — "
                   f"the last error: {why}")
            left = not self._clean(cur)
        else:
            tries, left = cur.get("tries"), False
            shutil.rmtree(work_dir(e["host"]), ignore_errors=True)
        dvbook.update(name, state=dvbook.FAILED, phase=None, tries=tries, retry_at=None,
                      stage_left=left or None, error=str(why)[:400], failed=int(time.time()))
        logbook.failure(f"DV 7->8.1 {title}: {why}")

    def _swap_landed(self, e):
        """After a failed swap: did the rename happen anyway? None when the NAS cannot say."""
        try:
            st, staged = nas_ssh.stat(e["host"]), nas_ssh.stat(nas_ssh.stage_path_for(e["host"]))
        except Exception:  # noqa: BLE001 — unknown: the next attempt asks again
            return None
        return swapped_already(st[0] if st else None, bool(staged),
                               int(e.get("size_in") or e.get("bytes") or 0), int(e.get("size_out") or 0))

    def _resume_kept_failures(self):
        """A movie that failed before attempts were counted kept its verified new file and waited
        for a manual retry. It now gets its attempts like any other, the failure it already had
        counting as the first (user, 2026-09-30). One whose file is gone has nothing to resume:
        its leftovers go, here and on the NAS, and a retry starts it over. A staged copy a give-up
        could not delete (the NAS did not answer) is deleted now."""
        for e in dvbook.queue():
            if e.get("state") == dvbook.FAILED and e.get("stage_left") and self._clean(e):
                dvbook.update(e["name"], stage_left=None)   # the NAS answers again: it is gone
            if e.get("state") != dvbook.FAILED or e.get("phase") not in KEEP_ON_FAILURE:
                continue
            out = os.path.join(work_dir(e["host"]), "p81.mkv")
            if e.get("phase") == "swap" or (os.path.exists(out)
                                            and os.path.getsize(out) == int(e.get("size_out") or -1)):
                dvbook.update(e["name"], state=dvbook.ACTIVE, tries=1, retry_at=int(time.time()))
            else:
                self._clean(e)
                dvbook.update(e["name"], phase=None)

    def _clean(self, e) -> bool:
        """Delete a movie's files on this Mac and any staged copy on the NAS. False when a staged
        copy may be left because the NAS did not answer."""
        shutil.rmtree(work_dir(e["host"]), ignore_errors=True)
        if e.get("phase") in ("upload", "swap") or e.get("stage_left"):
            try:
                nas_ssh.discard_stage(e["host"])
            except Exception:  # noqa: BLE001 — remembered as stage_left and tried again later
                return False
        return True

    def _sweep_orphans(self):
        """Working folders of movies no longer queued (removed while the app was down). A FAILED
        movie's verified new file is not an orphan: a retry resumes from it."""
        keep = {os.path.basename(work_dir(e["host"])) for e in dvbook.queue() if e.get("host") and (
                    e.get("state") in (dvbook.PENDING, dvbook.ACTIVE)
                    or (e.get("state") == dvbook.FAILED and e.get("phase") in KEEP_ON_FAILURE))}
        try:
            names = os.listdir(WORK_ROOT)
        except OSError:
            return
        for n in names:
            if n not in keep:
                shutil.rmtree(os.path.join(WORK_ROOT, n), ignore_errors=True)

    # ---- the pipeline's cached downloads ----------------------------------------------------
    # THE DV 7 QUEUE OUTRANKS CACHED DOWNLOADS (user, 2026-09-30). The upscale pipeline downloads
    # ahead into its prefetch buffer — upcoming items' sources and CFRs, every one of which simply
    # downloads again when its turn comes. So the lane counts that buffer as room it may clear
    # (_room), and the prefetcher leaves alone the room the movie in progress will still write
    # (reserve_bytes). Nothing is held for a movie still WAITING for disk: the buffer it would keep
    # empty is clearable the moment that movie fits anyway, so holding it would only switch the
    # download-ahead off for as long as the wait lasts (review 2026-09-30). The pipeline's own
    # working files still outrank both.

    def use_cache(self, scratch, evict):
        """The orchestrator lends the lane its cached downloads. `scratch()` is the pipeline's scratch
        folder (the buffer lives inside it); `evict(nbytes, dry_run=False)` clears at least that much
        of the buffer, least imminent first, and returns the bytes freed — or, as a dry run, the bytes
        it could free right now."""
        self._cache = (scratch, evict)

    def _same_disk(self) -> bool:
        try:
            return os.stat(self._cache[0]()).st_dev == os.stat(WORK_ROOT).st_dev
        except Exception:  # noqa: BLE001 — unreadable: clearing it cannot be shown to help
            return False

    def _cache_bytes(self) -> int:
        """Bytes of cached downloads the lane may clear — only those on its own disk help it."""
        if not self._cache or not self._same_disk():
            return 0
        try:
            return int(self._cache[1](0, dry_run=True) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _room(self, e, memo=None) -> bool:
        """Does movie `e` fit above the floor — clearing cached downloads if that is what it takes?
        Never clears anything for a movie the cache could not make fit. `memo` carries the cache's
        size across one pick, so a long queue measures it once, not once per movie."""
        memo = {} if memo is None else memo
        size, have = int(e.get("bytes") or 0), local_bytes(e["host"])
        free, floor = shutil.disk_usage(WORK_ROOT).free, _floor_bytes()
        if fits(size, have, free, floor):
            return True
        if "cache" not in memo:
            memo["cache"] = self._cache_bytes()
        if not memo["cache"] or not fits(size, have, free + memo["cache"], floor):
            return False
        # The movie claims its room BEFORE anything is cleared: from here the prefetcher sees the
        # reserve and cannot start a download into the space being made (review 2026-09-30).
        prev, self._active = self._active, e
        memo.pop("cache")                        # whatever is left must be measured again
        try:
            freed = int(self._cache[1](disk_need(size, have) - (free - floor)) or 0)
        except Exception as ex:  # noqa: BLE001 — the lane waits for disk instead
            logbook.exception("dv lane: clearing cached downloads", ex)
            freed = 0
        if freed:
            logbook.event(f"DV 7->8.1 {e.get('title') or e['name']}: cleared {freed / 1e9:.1f} GB of "
                          f"cached downloads to make room (they download again in their turn)")
        ok = fits(size, have, shutil.disk_usage(WORK_ROOT).free, floor)
        if not ok:
            self._active = prev
        return ok

    def reserve_bytes(self) -> int:
        """The room the prefetcher must leave this lane: what the movie in progress will still
        write. 0 while the lane is off or between movies."""
        if not self.running():
            return 0
        e = self._active
        return disk_need(e.get("bytes") or 0, local_bytes(e["host"])) if e is not None else 0

    # ---- fetch: download + convert -------------------------------------------------------

    def _pick_fetch(self):
        """The first not-yet-converted movie whose footprint fits above the pipeline's disk floor,
        counting the pipeline's cached downloads as room it may clear. A movie already
        part-downloaded is preferred — its bytes are sunk."""
        todo = dvbook.open_entries(phases=(None, "download", "convert"),
                                   skip=self._cancelled)
        if not todo:
            self._note = None
            return None
        held = [x for x in dvbook.open_entries(phases=KEEP_ON_FAILURE)
                if x.get("phase") == "converted" or x.get("retry_at")]
        if len(held) >= SHIP_BACKLOG:
            # A movie waiting to try its upload again holds its new file on this disk like one
            # waiting its first turn: while uploads fail, more conversions would only pile up.
            self._note = ("a movie that failed to upload is waiting to try again"
                          if any(x.get("retry_at") for x in held)
                          else "a converted movie is waiting to upload")
            return None
        os.makedirs(WORK_ROOT, exist_ok=True)
        todo.sort(key=lambda e: 0 if e.get("phase") else 1)       # stable: queue order otherwise
        memo = {}
        for e in todo:
            if self._room(e, memo):
                self._note = None
                return e
        free, floor = shutil.disk_usage(WORK_ROOT).free, _floor_bytes()
        cache = memo["cache"] if "cache" in memo else self._cache_bytes()
        need = disk_need(todo[0].get("bytes") or 0, local_bytes(todo[0]["host"]))
        more = f" + {cache / 1e9:.0f} GB of cached downloads it can clear" if cache else ""
        self._note = (f"waiting for disk: {todo[0].get('title')} needs {need / 1e9:.0f} GB above "
                      f"Visionary's {floor / 1024 ** 3:.0f} GB floor ({free / 1e9:.0f} GB free{more})")
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
            self._active = e
            try:
                self._fetch(e, ev)
            except (_Cancelled, _NoRoom):
                pass                     # _pick_fetch now says what it is waiting for
            except (nas_ssh.Stopped, dvp7.Aborted):
                if self._abort.is_set():
                    return
            except Exception as ex:  # noqa: BLE001
                self._status["fetch"] = None    # the panel shows the lane's note, not a dead step
                self._active = None             # waiting out a NAS outage writes nothing: the
                if not self._offline(ex):       # prefetcher must not be held off for its length
                    self._fail(e, ex)
            finally:
                self._active = None
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
        with self._transfer("fetch", e, "download", ev):
            # The budget was judged when the movie was picked; the turn to transfer can come a whole
            # upload later, and the pipeline's own upscale writes to the same disk meanwhile.
            if not self._room(e):
                raise _NoRoom(name)
            logbook.event(f"DV 7->8.1 {e.get('title')}: downloading {size / 1e9:.1f} GB")
            nas_ssh.download(host, src, size, abort=ev, limit=self._limit,
                             on_progress=lambda b, t: self._set("fetch", e, "download", b, t))
        self._set("fetch", e, "convert", 0, 100)      # the download row must not look live now
        self._check(name, ev)
        dvbook.update(name, phase="convert")
        # A fresh conversion makes any staged copy from an earlier attempt stale: mkvmerge writes a
        # new segment UID every time, so resuming an upload onto it would splice two files.
        nas_ssh.discard_stage(host)
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
        now = time.time()
        todo = [e for e in dvbook.open_entries(phases=("converted", "upload", "swap"),
                                               skip=self._cancelled)
                if (e.get("retry_at") or 0) <= now]          # a failed one waits out its pause
        todo.sort(key=lambda e: {"swap": 0, "upload": 1}.get(e.get("phase"), 2))
        return todo[0] if todo else None

    def _ship_loop(self):
        while not self._abort.is_set():
            self._flush_plex()
            e = self._pick_ship()
            if not e:
                self._status["ship"] = None
                try:
                    nas_ssh.link()          # keep the panel's link current while idle (local only)
                except Exception:  # noqa: BLE001
                    pass
                if self._wait(IDLE_POLL_SECS):
                    return
                continue
            ev = self._event(e["name"])
            if e.get("retry_at"):
                dvbook.update(e["name"], retry_at=None)    # this IS the next attempt
            try:
                self._ship(e, ev)
            except _Cancelled:
                pass
            except (nas_ssh.Stopped, dvp7.Aborted):
                if self._abort.is_set():
                    return
            except Exception as ex:  # noqa: BLE001
                self._status["ship"] = None    # the panel shows the lane's note, not a dead step
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
                # The new file is gone: convert it again. The next file is a new file — it gets
                # its own attempts — and a staged copy of the old one is of no use to it.
                try:
                    nas_ssh.discard_stage(host)
                except Exception:  # noqa: BLE001 — the conversion discards it again before uploading
                    pass
                dvbook.update(name, phase=None, tries=None, retry_at=None)
                return
            dvbook.update(name, phase="upload")
            self._check(name, ev)
            with self._transfer("ship", e, "upload", ev):
                logbook.event(f"DV 7->8.1 {e.get('title')}: uploading {size_out / 1e9:.1f} GB")
                nas_ssh.upload(out, stage, abort=ev, limit=self._limit,
                               on_progress=lambda b, t: self._set("ship", e, "upload", b, t))
            self._set("ship", e, "swap", note="checking the uploaded copy on the NAS")
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
            try:
                nas_ssh.link()              # a long wait here must not leave the panel's link stale
            except Exception:  # noqa: BLE001
                pass
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
        dvbook.update(name, state=dvbook.DONE, phase=None, plex_pending=True, tries=None,
                      retry_at=None, error=None, finished=int(time.time()))
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
