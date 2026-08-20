"""Dashboard backend — serves the UI and a /api/state endpoint of REAL state.

Read-only and inert: AUTOMATION_ENABLED is False, so the dashboard only
observes (power, scratch, window). Nothing auto-starts, no apps are closed, the
drive is never cycled from here, and no Topaz/Resolve work is launched. The UI
reflects truth from the actual components; the automation stays off until the
app is finished.
"""
from __future__ import annotations
import atexit
import datetime
import glob
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINE_DIR = os.path.dirname(DASHBOARD_DIR)
sys.path.insert(0, ENGINE_DIR)

import logbook          # noqa: E402
import power            # noqa: E402
import preflight        # noqa: E402
import scratch          # noqa: E402
import series           # noqa: E402
import settings         # noqa: E402
import transfer         # noqa: E402
import orchestrator     # noqa: E402

# The app launches us with a minimal PATH (no /opt/homebrew/bin) — augment it so the
# selftest and every spawned subprocess (notably the resolve stage's cliclick) can
# find Homebrew-installed tools. Inherited by child processes.
os.environ["PATH"] = (os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin")

# Automation is controlled by the orchestrator's enable toggle (POST /api/automation).
# APPLIANCE mode: the toggle persists (settings.activated) — while activated, _rearm_loop
# re-enables the orchestrator on launch and when the overnight window reopens after an
# auto-stop, so the app "runs whenever it can" without the user re-arming it.
AUTOMATION_ENABLED = False

# the selected series' queue is cached in series.py (so /api/state polling never hits
# the NAS); it's refreshed on picker-open, series-select, and after each upload.

SCRATCH_VOLUME = "2TB SSD"
SCRATCH_SUBDIR = "topaz-scratch"
WINDOW_START = "20:00"
WINDOW_END = "09:00"


# ---- pure logic (unit-tested) ---------------------------------------------

def in_window(now_time: datetime.time, start: str, end: str) -> bool:
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    s = datetime.time(sh, sm)
    e = datetime.time(eh, em)
    if s <= e:
        return s <= now_time < e
    return now_time >= s or now_time < e   # overnight window


def should_rearm(*, activated: bool, enabled: bool) -> bool:
    """APPLIANCE mode: should the orchestrator be (re)enabled right now? While ACTIVATED the app
    runs whenever it can, so any time it finds itself disabled (e.g. a fresh launch at login) it
    re-arms. A run ends only on a manual Deactivate (activated=False) — there is no auto-stop."""
    return activated and not enabled


def build_state(*, power, scratch, adapter_watts, in_win,
                automation_enabled) -> dict:
    draining = _is_draining(power)   # param 'power' shadows the module; helper routes correctly
    return {
        "automation_enabled": automation_enabled,
        "status": "disabled" if not automation_enabled else "armed",
        "power": {
            "external_connected": power.external_connected,
            "charging": power.is_charging,
            "capacity": power.capacity,
            "amperage_ma": power.amperage,
            "adapter_watts": adapter_watts,
            "draining_on_ac": draining,
            # sufficiency = the BRICK: >= min_adapter_watts (140 W) connected → adequate,
            # regardless of momentary battery drain under load
            "adequate": bool(power.external_connected) and (adapter_watts or 0)
                        >= int(settings.get_settings().get("min_adapter_watts", 140)),
        },
        "scratch": scratch,
        "window": {"start": WINDOW_START, "end": WINDOW_END, "in_window": in_win},
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


def _is_draining(reading):
    # import-safe call into the tested detector
    return power.is_draining_on_ac(reading)


# ---- real collectors (I/O glue) -------------------------------------------

def read_adapter_watts():
    """The SAME number the run gate judges by (power.adapter_watts_sustained) — a second copy
    of the pmset parse used to live here, so a charger that dips to 120 and back made the
    header disagree with the gate and read as though power had failed."""
    try:
        return power.adapter_watts_sustained()
    except Exception:
        return None


def collect_scratch():
    # The pipeline now works on the always-mounted internal SSD (~/topaz-scratch);
    # the external 2TB SSD is cold storage. Report the internal scratch as the
    # active working volume — it is the location Topaz/Resolve actually read+write.
    path = scratch.default_scratch()
    # Space left for the project = physical free + topaz-scratch's own usage (its recyclable
    # working files), so a partially-filled scratch isn't counted against the pipeline.
    free_gb = scratch.available_gb(path)
    return {"name": "Internal SSD", "connected": True, "path": path,
            "free_gb": free_gb, "source": "internal"}


def series_info():
    """Active series (the round-robin set) + each one's cached queue + the {nas_dir: Plex title}
    map — fast (no NAS I/O) for state polling. `selected` is the primary (back-compat); `extras`
    are the additional round-robin shows; `rotation` is whose turn is next."""
    import borders, plex
    plex.ensure_titles_warming()
    active = series.get_active_series()
    sel = active[0] if active else None
    # Uniform per-show info for ALL active shows, so the UI renders each as the same block.
    shows = []
    for nm in active:
        nu = series.get_next_up(nm) or None
        if borders.show_aspect(nm) is None:
            _kick_aspect_probe(nm)       # eager fill; the row can't render until it lands
        shows.append({"name": nm, "preset": settings.show_preset_key(nm),
                      "configured": settings.get_show_preset(nm) is not None,
                      "unwatched_first": settings.get_show_unwatched_first(nm),
                      "featurettes_last": settings.get_show_featurettes_last(nm),
                      "normalize_audio": settings.get_show_normalize_audio(nm),
                      "replace_source": settings.get_show_replace_source(nm),
                      "output_mode": settings.get_show_output_mode(nm),
                      # AI border extension: the row renders ONLY when aspect == "4:3"
                      # AND the top-level borders_ready is true (hide-inert-UI).
                      "extend_borders": settings.get_show_extend_borders(nm),
                      "extend_prompt": settings.get_show_extend_prompt(nm),
                      "extend_sets": borders.set_count(nm),
                      "aspect": borders.show_aspect(nm),
                      # what it will ACTUALLY master as — the app shows this, not "auto"
                      "output_mode_effective": settings.effective_output_mode(
                          nm, _hdr_hint(nm)),
                      "next_up": nu,
                      "next_up_armed": series.next_up_armed(nm),
                      # ≥90% done — the UI only offers "queue a follow-up" from here
                      "near_done": series.near_done(nm),
                      # The QUEUED show's own settings, so they can be configured BEFORE it
                      # is promoted (they're keyed by show name in show_profiles.json, so
                      # they already persist for a show that isn't active yet).
                      "next_up_profile": show_settings_view(nu) if nu else None,
                      "queue": series.cached_queue(nm)})
    ready = False
    try:
        ready = borders.env_ready()[0]     # ComfyUI + VideoHelperSuite + every model
    except Exception:
        pass
    return {"selected": sel, "active": active, "rotation": series.get_rotation(),
            "queue": shows[0]["queue"] if shows else None,
            "shows": shows, "titles": plex.peek_titles(),
            # Comfy + all models installed — half of the ExtendBordersRow's visibility
            # gate (a few file stats per poll; nothing network).
            "borders_ready": ready}


_ASPECT_PROBES = set()          # shows whose eager head-probe already ran this process
_ASPECT_LOCK = threading.Lock()


def _kick_aspect_probe(name):
    """EAGER show-aspect fill: the per-show "Extend borders" row can only appear once the
    show's aspect is KNOWN, and a show freshly added to the rotation has never been
    probed. Pull the first queued episode's first ~3 MB over FTP and probe THAT (an MKV
    puts its track geometry up front; an MP4 whose moov sits at the end just stays
    unknown until the first real download probes it — the authoritative fill). Once per
    show per process; fully best-effort, never blocks the poll."""
    with _ASPECT_LOCK:
        if name in _ASPECT_PROBES:
            return
        _ASPECT_PROBES.add(name)

    def work():
        try:
            import tempfile
            import borders, plan, transfer
            q = series.cached_queue(name) or {}
            item = q.get("next") or (q.get("remaining_items") or [None])[0] or {}
            base = item.get("source_name")
            if not base:
                return
            ep = item.get("ep") or ""
            nas_dir = series.episode_nas_dir(name, base)
            if not nas_dir and len(ep) >= 3:      # the S{NN} convention, like episode_paths
                nas_dir = f"{transfer.NAS_FTP_TV_ROOT.rstrip('/')}/{name}/S{ep[1:3]}"
            if not nas_dir:
                return
            head = os.path.join(tempfile.gettempdir(),
                                f"_aspect_head_{abs(hash(name))}.bin")
            ok, _r = transfer.download_head(f"{nas_dir}/{base}", head, 3 * 1024 * 1024)
            if ok:
                info = plan.probe_input(head)
                label = borders.aspect_label(info.get("width"), info.get("height"),
                                             info.get("sar"))
                if label:
                    borders.record_show_aspect(name, label)
            try:
                os.remove(head)
            except OSError:
                pass
        except Exception:
            pass

    threading.Thread(target=work, daemon=True, name="aspect-probe").start()


def api_borders_status():
    """The border-extender environment for the Setup group: where ComfyUI lives, each
    model's install state (ok/truncated/missing — truncated resumes via curl -C -),
    overall readiness, and the learned seconds-per-chunk for the row's projection."""
    import borders
    env = borders.discover()
    models = borders.model_status(env["models_dir"]) if env.get("models_dir") else {}
    ready, missing = borders.env_ready(env)
    return {"env": env, "models": models,
            "ready": bool(env.get("ok")) and ready, "missing": missing,
            "sec_per_chunk": borders.avg_sec_per_chunk(),
            "chunk_frames": borders.CHUNK_FRAMES}


def api_refresh_library():
    """Refresh button: ask Plex to rescan the TV section(s) (new/renamed shows) AND re-pull the
    current Plex titles now, then return the freshly-listed series."""
    import plex
    plex.trigger_tv_scan()      # async on Plex's side — picks up brand-new folders for next time
    plex.refresh_titles()       # re-pull titles Plex already knows (renames, scanned shows) now
    return api_series()


def movies_info():
    """The curated movie queue (fast — a small local file) for state polling, plus the cached
    library pool if it's been built, plus the {basename: Plex title} map (background-warmed).
    No NAS walk here."""
    import companion, movies, plex
    plex.ensure_movie_titles_warming()
    return {"selected": movies.selected_view(), "library": movies.peek_library(),
            "titles": plex.peek_movie_titles(),
            "companions": companion.book_view()}   # pairing/verdict states (small local file)


def api_movies():
    """Movie picker payload: the searchable LIBRARY pool (all movies still lacking DV) + the
    SELECTED queue. Hits the NAS + Plex for the pool, so it's on demand (entering Movie mode
    / manual refresh), not on poll."""
    import movies, plex
    lib = movies.refresh_library()
    plex.refresh_movie_titles()      # pull Plex movie titles now (on-demand, like the TV refresh)
    return {"library": lib, "selected": movies.selected_view(), "reachable": bool(lib),
            "titles": plex.peek_movie_titles()}


def api_movie_queue(body):
    """Add / remove / clear movies in the curated queue. `add` carries the preset chosen in
    the add step (saved per-movie, keyed by title — so each queued movie can differ)."""
    import movies
    action = (body.get("action") or "").strip()
    if action == "add":
        title = (body.get("title") or "").strip()
        name = (body.get("name") or "").strip()
        # DV-badged movies are COMBINE-ONLY (user-dictated): a plain add would send an
        # already-DV file into the pipeline just to be permanent-parked at download.
        lib = movies.peek_library() or []
        if any(m.get("name") == name and m.get("has_dv") for m in lib):
            return {"error": "already Dolby Vision — pair a companion copy instead",
                    "selected": movies.selected_view()}
        movies.add_selected(name, (body.get("dir") or "").strip(), title)
        preset = (body.get("preset") or "").strip()
        if preset and title:
            settings.set_show_preset(title, preset)
    elif action == "remove":
        nm = (body.get("name") or "").strip()
        movies.remove_selected(nm)           # membership FIRST, so selection can't re-pick it
        try:
            orchestrator.ORCH.abandon_movie(nm)   # IN-FLIGHT too (user-dictated): abort the
        except Exception:                         # run item / lanes, drop the durable entry,
            pass                                  # sweep scratch once the encoders die
        orchestrator.discard_workfiles(nm)   # a part-processed (turn-deferred) movie's scratch
        try:
            import companion
            if companion.entry(nm).get("status") == "confirmed":
                companion.mark(nm, "ready")  # keep the pairing, re-arm the confirm gate
        except Exception:
            pass
    elif action == "clear":                  # files would otherwise be orphaned forever
        for it in movies.get_selected():
            if it.get("name"):
                try: orchestrator.ORCH.abandon_movie(it["name"])
                except Exception: pass
                orchestrator.discard_workfiles(it["name"])
        movies.clear_selected()
    return {"selected": movies.selected_view()}


def api_companion(body):
    """COMPANION COMBINE control (all localhost): pair a NAS movie with its seedbox copy.
    Actions: `search` (async relay search → candidates), `pair` (async dual head-probe →
    verdict, status `ready`), `confirm` (the verdict card's button — flips the book AND
    queues the movie as a combine item), `unpair`/`dismiss` (drop the pairing; a queued
    combine item is removed like any movie). Slow steps run in daemon workers — the app
    watches status via the state poll's `companions` map."""
    import companion, movies
    action = (body.get("action") or "").strip()
    name = (body.get("name") or "").strip()
    if not name:
        return {"error": "name required"}
    if action == "search":
        return companion.start_search(name, (body.get("dir") or "").strip(),
                                      (body.get("title") or "").strip())
    if action == "pair":
        path = (body.get("path") or "").strip()
        if not path:
            return {"error": "path required"}
        return companion.pair(name, path)
    if action == "confirm":
        res = companion.confirm(name)
        if res.get("status") == "confirmed":
            e = companion.entry(name)
            movies.add_selected(name, (body.get("dir") or e.get("dir") or "").strip(),
                                (body.get("title") or e.get("title") or "").strip(),
                                combine=True)
        res["selected"] = movies.selected_view()
        return res
    if action in ("unpair", "dismiss"):
        was_queued = any(i.get("name") == name and i.get("combine")
                         for i in movies.get_selected())
        companion.unpair(name)
        if was_queued:
            movies.remove_selected(name)
            try:
                orchestrator.ORCH.abandon_movie(name)
            except Exception:
                pass
            orchestrator.discard_workfiles(name)
        return {"status": "unpaired", "selected": movies.selected_view()}
    return {"error": f"unknown action {action!r}"}


def api_queue_action(body):
    """Manage the up-next queue — MOVIES only (episodes are auto-generated and not manipulable).
    action: remove | up | down. remove drops the movie from the curated queue; up/down move it
    one slot in the COMBINED queue (past episodes too — see movies.move_in_queue)."""
    import movies
    action = (body.get("action") or "").strip()
    name = (body.get("name") or "").strip()
    if (body.get("kind") or "").strip() == "movie" and name:
        if action == "remove":
            movies.remove_selected(name)
            orchestrator.discard_workfiles(name)   # don't orphan a turn-deferred movie's scratch
        elif action in ("up", "down"):
            sel = series.get_selection()
            q = series.cached_queue(sel) if sel else None
            ep_count = len((q or {}).get("remaining_items", []))
            movies.move_in_queue(name, -1 if action == "up" else 1, ep_count)
    return {"up_next": up_next(current=orchestrator.ORCH.snapshot().get("current"), inflight=orchestrator.ORCH.finisher_views())}


def youtube_info():
    """The curated YouTube channel queue (fast — cache + local files) for state polling: each
    queued channel with its preset + pending/total video counts, and the next processable video."""
    import youtube
    return youtube.queue_view()


YT_REDIRECT = "http://localhost:8765/oauth/youtube"


def api_channels():
    """YouTube picker payload: the user's real YouTube subscriptions (OAuth) + the current queue."""
    import youtube, ytdata
    channels = youtube.list_channels()          # [{channelId, title}] from ytdata.subscriptions()
    return {"channels": channels, "queue": youtube.queue_view(),
            "connected": ytdata.connected(), "configured": ytdata.configured()}


def api_youtube_connect(body):
    """Start / query / drop the YouTube OAuth connection. action: start → the Google consent URL for
    the app to open; disconnect → forget the token; (default) → connection status + subscription count."""
    import ytdata, youtube
    action = (body.get("action") or "").strip()
    if action == "disconnect":
        ytdata.disconnect()
    return {"connected": ytdata.connected(), "configured": ytdata.configured(),
            "auth_url": ytdata.auth_url(YT_REDIRECT) if action == "start" else None,
            "subscriptions": (len(youtube.list_channels()) if action != "start" else None)}


def api_youtube_queue(body):
    """Manage the YouTube channel queue (unlimited standing subscriptions; no priority — videos
    round-robin across channels). action: add | remove | scope | cap | paused | clear | preset | delete.
    resolve_link/import_link/drop_import handle a PASTED YouTube URL (playlist, video or
    channel) — resolve first for the confirm step, then commit.
    add/remove/scope/cap/paused re-derive youtarr's config + meta. paused stops work on a channel without
    deleting its files; remove WIPES it. delete removes a single downloaded video + ignores it."""
    import youtube
    action = (body.get("action") or "").strip()
    cid = (body.get("channelId") or "").strip()
    reconfigure = False
    imported = None
    if action == "add" and cid:
        youtube.add_channel(cid, (body.get("title") or "").strip() or cid,
                            (body.get("scope") or "popular").strip())
        reconfigure = True
    elif action == "remove" and cid:
        # WIPE on remove (user-confirmed in the UI): drop from queue NOW (UI updates instantly), then
        # in the background collect its ids, delete its staging + 4K masters, forget its archive, and
        # unsubscribe it LAST (so its ids are still known when collected). No sync reconfigure here —
        # wipe_channel does the youtarr sync itself. FTP deletes can be slow; don't block the handler.
        folder = next((e.get("folder_name") for e in youtube.get_queue() if e.get("channelId") == cid), None)
        youtube.remove_channel(cid)
        threading.Thread(target=youtube.wipe_channel, args=(cid, folder), daemon=True).start()
    elif action == "scope" and cid:
        youtube.set_scope(cid, (body.get("scope") or "popular").strip()); reconfigure = True
    elif action == "cap" and cid:                # per-channel length-limit toggle
        youtube.set_capped(cid, bool(body.get("capped"))); reconfigure = True
    elif action == "paused" and cid:             # per-channel pause: stop work, keep the files
        on = bool(body.get("paused"))
        youtube.set_paused(cid, on); reconfigure = True
        if on:   # pausing → if a video FROM THIS channel is mid-flight, SKIP it now; it comes back
            folder = next((e.get("folder_name") for e in youtube.get_queue()   # FIRST when resumed
                           if e.get("channelId") == cid), None)
            cur = orchestrator.ORCH.snapshot().get("current") or {}
            if folder and cur.get("kind") == "youtube" and cur.get("channel") == folder:
                youtube.set_resume_first(folder, youtube.video_id(cur.get("name") or ""))
                orchestrator.ORCH.skip_current(cur.get("name") or "")
    elif action == "max_age" and cid:            # per-channel max age (days; 0 = no limit)
        youtube.set_max_age(cid, body.get("max_age_days") or 0); reconfigure = True
    elif action == "clear":
        youtube.clear_queue(); reconfigure = True
    elif action == "preset":                    # preset keyed by the channel FOLDER name
        settings.set_show_preset((body.get("folder") or "").strip(), (body.get("preset") or "").strip())
    elif action == "resolve_link":
        # PHASE 1 of a link import: say WHAT the URL points at — real titles, real counts,
        # and whether a watch?v=…&list=… is ambiguous — with NO side effects, so the user
        # confirms against names instead of against a bare URL.
        return youtube.resolve_link((body.get("url") or "").strip())
    elif action == "import_link":
        # PHASE 2: commit the chosen reading. `choice` ("video"/"playlist") settles an
        # ambiguous link. Falls through so the refreshed queue + up-next come back with it.
        imported = youtube.import_link((body.get("url") or "").strip(),
                                       (body.get("choice") or "").strip() or None)
        if imported.get("status") == "channel-queued":
            reconfigure = True                  # a newly queued channel needs youtarr synced
    elif action == "drop_import":
        imported = youtube.drop_import((body.get("batch") or body.get("id") or "").strip())
    elif action == "prioritize":
        # "Run this video now": promote it to the priority book (cadence-exempt, ahead of
        # due movies) and ask the in-flight item to YIELD at its next safe boundary — a
        # Topaz segment boundary, so at most one ~90 s segment is redone. A download is
        # left to finish (aborting throws away GB) and Resolve is never interrupted, so
        # the response says when it will actually start.
        vid = (body.get("vid") or body.get("id") or "").strip()
        if not vid:
            name = (body.get("name") or "").strip()
            vid = youtube.video_id(name) if name else ""
        if not vid:
            return {"error": "no-video", "detail": "need a video id or filename"}
        out = youtube.prioritize_pending(vid)
        if out.get("status") in ("queued", "already-first"):
            o = orchestrator.ORCH.snapshot()
            stage = (o.get("stage") or "") if o.get("running") else ""
            out["current_stage"] = stage
            out["starts"] = {"topaz": "at the current segment boundary (about a minute)",
                             "resolve": "after the current Dolby Vision pass finishes",
                             "download": "after the current download finishes",
                             }.get(stage, "next")
        return out
    elif action == "delete":
        # per-video skip/delete: works from the queue rows (channel folder) OR the
        # currently-processing header. Resolves the channelId from the folder when the
        # caller only knows the folder (up-next rows carry folder, not id).
        name = (body.get("name") or "").strip()
        if not cid:
            folder = (body.get("channel") or "").strip()
            cid = next((e.get("channelId") for e in youtube.get_queue()
                        if e.get("folder_name") == folder), None)
        if cid and name:
            was_current = orchestrator.ORCH.skip_current(name)   # abort in-flight work on it
            youtube.delete_video(cid, name)                      # staging + youtarr ignore + done
            def _discard_later():
                import time as _t
                _t.sleep(8 if was_current else 0)   # let the aborted stage die + the loop move on
                orchestrator.discard_workfiles(name)             # then drop its scratch leftovers
            threading.Thread(target=_discard_later, daemon=True).start()
    if reconfigure:
        try: youtube.configure_youtarr()        # sync youtarr's subs + refresh scope/duration meta
        except Exception: pass
    out = {"youtube": youtube.queue_view(), "up_next": up_next(current=orchestrator.ORCH.snapshot().get("current"), inflight=orchestrator.ORCH.finisher_views())}
    if imported is not None:
        out["import"] = imported
    return out


def api_mode(mode):
    """Switch TV / Movie / YouTube mode (the nav bar VIEW — the movie + youtube queues still
    process regardless). Warms the pool on entering Movie/YouTube mode."""
    import movies, youtube
    m = series.set_mode(mode)
    out = {"mode": m, "movies": {"selected": movies.selected_view()}}
    if m == "movie":
        out["movies"]["library"] = movies.refresh_library()
    elif m == "youtube":
        out["youtube"] = youtube.queue_view()
    return out


def _hdr_hint(name) -> bool:
    """Filename evidence that an item's SOURCES are HDR — used only to display which ceiling
    "auto" will pick.

    Works for a TV show OR a movie. It used to be TV-only and was called for movies too,
    where it FTP-walked /Media/TV-Shows/<movie title> — a directory that cannot exist. That
    made it a guaranteed False (so every movie displayed 1000 nits), while still doing NAS
    I/O and caching an empty queue under a movie key. A movie has no episode list, so its own
    filename is the evidence; a show takes the majority of its remaining sources so one
    oddly-named file can't flip the whole show."""
    known = None
    try:
        import plan as _plan
        known = _plan.probed_is_hdr(name)
    except Exception:
        pass
    if known is not None:
        return known                       # ffprobe beat the filename to it
    if name and settings.looks_hdr(name):
        return True                        # the item's own name already says so (movie case)
    try:
        q = series.cached_queue(name) or {}
        names = [i.get("source_name") for i in (q.get("remaining_items") or [])][:40]
        nxt = (q.get("next") or {}).get("source_name")
        if nxt and nxt not in names:
            names.append(nxt)
        names = [n for n in names if n]
        if not names:
            return False
        hdr = sum(1 for n in names if settings.looks_hdr(n))
        return hdr * 2 > len(names)
    except Exception:
        return False


# --- RESOLVE SCREEN PREVIEW ----------------------------------------------------------------
# One capture at a time, shared by every request. A capture of the 4K host measured 1.9-9.9 s
# under load, so the endpoint must never do it inline.
_PREVIEW = {"jpg": None, "big": None, "at": 0.0, "busy": False}
_PREVIEW_LOCK = threading.Lock()
PREVIEW_MAX_AGE = 1.0        # refresh if the newest frame is older than this
PREVIEW_STALE = 15.0         # ...and stop serving one this old entirely


def _preview_capture():
    """Capture + downscale + JPEG-encode one frame of the display the resolve stage drives."""
    try:
        import tempfile, cv2, dv_shim
        png = os.path.join(tempfile.gettempdir(), "_api_preview.png")
        host, _why = preflight.chosen_host()
        prev = dv_shim.get_host()
        try:
            dv_shim.set_host(host)
            dv_shim.screenshot(png, attempts=1)
        finally:
            dv_shim.set_host(prev)
        img = cv2.imread(png)
        if img is None:
            return
        h, w = img.shape[:2]
        # TWO variants from the ONE capture (the screenshot is the expensive part): the
        # card's 420w tile, and a 1080p-class frame for the full-width overlay — 420
        # stretched across the window was mush (user-caught 2026-08-06).
        out = {}
        for key, want_w in (("jpg", 420), ("big", 1920)):
            scale = want_w / float(w)
            sized = img if scale >= 1.0 else cv2.resize(
                img, (want_w, max(1, int(round(h * scale)))), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", sized, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
            if ok:
                out[key] = buf.tobytes()
        if out:
            with _PREVIEW_LOCK:
                _PREVIEW.update(out)
                _PREVIEW["at"] = time.time()
    except Exception:
        pass
    finally:
        with _PREVIEW_LOCK:
            _PREVIEW["busy"] = False


def _preview_frame(big=False):
    """The newest frame (420w tile, or the 1080p-class `big` for the enlarged view),
    kicking off a refresh when it is getting old. Never blocks."""
    now = time.time()
    with _PREVIEW_LOCK:
        jpg = _PREVIEW["big" if big else "jpg"]
        at, busy = _PREVIEW["at"], _PREVIEW["busy"]
        due = (now - at) > PREVIEW_MAX_AGE
        if due and not busy:
            _PREVIEW["busy"] = True
            start = True
        else:
            start = False
    if start:
        threading.Thread(target=_preview_capture, daemon=True).start()
    return jpg if (jpg is not None and (now - at) < PREVIEW_STALE) else None


def displays_view() -> dict:
    """Every attached screen, whether it can host Resolve, and the saved priority."""
    import displays as _dsp        # noqa: F401  (imported for the side of a clear error)
    host, why = preflight.chosen_host()
    return {"displays": preflight.eligible_displays(),
            "priority": settings.get_display_priority(),
            "enabled": bool(settings.get_settings().get("resolve_host_pinning")),
            "fallback_main": bool(settings.get_settings().get("resolve_host_fallback_main")),
            "warn_takeover": bool(settings.get_settings().get("resolve_takeover_warn")),
            "host": host, "host_reason": why}


def show_settings_view(name) -> dict:
    """The per-show settings that can be set for ANY show — active or merely queued as a
    slot's follow-up (all three live in show_profiles.json keyed by show name)."""
    import borders
    return {"preset": settings.show_preset_key(name),
            "configured": settings.get_show_preset(name) is not None,
            "unwatched_first": settings.get_show_unwatched_first(name),
            "featurettes_last": settings.get_show_featurettes_last(name),
            # only shows the toggle when the show actually HAS season-00 specials
            "has_featurettes": int((series.cached_queue(name) or {}).get("featurette_count", 0)) > 0,
            "normalize_audio": settings.get_show_normalize_audio(name),
            "replace_source": settings.get_show_replace_source(name),
            "output_mode": settings.get_show_output_mode(name),
            "output_mode_effective": settings.effective_output_mode(name, _hdr_hint(name)),
            "extend_borders": settings.get_show_extend_borders(name),
            "extend_prompt": settings.get_show_extend_prompt(name),
            "extend_sets": borders.set_count(name),
            "aspect": borders.show_aspect(name)}


def api_series():
    """Picker payload: available series (NAS), current selection, and its queue.
    Hits the NAS over FTP, so it's called on demand (opening the picker), not on poll."""
    sel = series.get_selection()
    available = series.list_series()
    try: series.promote_finished_slots()   # hand a finished slot to its follow-up even while STOPPED
    except Exception: pass
    sel = series.get_selection()           # a promotion may have just changed the primary
    queue = series.refresh_queue(sel) if sel else None
    return {"series": available, "selected": sel, "queue": queue,
            "reachable": bool(available)}


def api_select(name, action="set", index=0):
    """Pick a series. action: 'at' = put it in round-robin slot `index` (replace or append — the
    per-slot picker); 'set' = make it the SOLE series (resets the round-robin); 'add' = append;
    'remove' = drop it."""
    before = set(series.get_active_series())
    if action == "at":
        series.set_series_at(index, name); series.refresh_queue(name)
    elif action == "add":
        series.add_series(name); series.refresh_queue(name)
    elif action == "remove":
        series.remove_series(name)
    else:
        series.set_selection(name); series.refresh_queue(name)
    # Bailing on a show must switch COMPLETELY: drop its in-flight item, everything queued
    # behind the finisher, and its durable entries — otherwise the finisher keeps grinding
    # the old show's ~1 h remux while the newly-picked show waits (user-caught).
    for gone in (before - set(series.get_active_series())):
        try: orchestrator.ORCH.abandon_series(gone)
        except Exception: pass
    sel = series.get_selection()
    return {"selected": sel, "active": series.get_active_series(),
            "queue": series.cached_queue(sel) if sel else None}


def show_profile_info(show=None):
    """The chosen Topaz preset for a show (TV) OR a movie title + the catalog for the picker.
    `show` overrides the default target — Movie mode passes the current movie's title so the
    Settings card edits that movie's preset. The user only PICKS a preset (no per-param
    tuning); unconfigured targets show the default until set."""
    import borders
    target = show or series.get_selection()
    saved = settings.get_show_preset(target) if target else None
    return {"show": target, "configured": saved is not None,
            "extend_borders": (settings.get_show_extend_borders(target)
                               if target else False),
            "extend_prompt": settings.get_show_extend_prompt(target) if target else "",
            "extend_sets": borders.set_count(target) if target else 0,
            "aspect": borders.show_aspect(target) if target else None,
            "preset": settings.show_preset_key(target) if target else settings.DEFAULT_PRESET,
            "unwatched_first": settings.get_show_unwatched_first(target) if target else True,
            "normalize_audio": settings.get_show_normalize_audio(target) if target else True,
            "replace_source": settings.get_show_replace_source(target) if target else True,
            "output_mode": settings.get_show_output_mode(target) if target else "auto",
            "output_mode_effective": (settings.effective_output_mode(target, _hdr_hint(target))
                                      if target else "dv1000"),
            "catalog": settings.preset_catalog()}


_YEAR_RE = re.compile(r"\((?:19|20)\d\d\)")


def _year_from(s):
    """The '(YYYY)' year embedded in a title/folder name, or None. Matches Barry (2018),
    ignores qualifiers like (US)."""
    m = _YEAR_RE.search(s or "")
    return m.group(0)[1:-1] if m else None


def _clean_title(s):
    """Drop every '(...)' qualifier — '(2018)', '(US)' — for a cleaner search title. shotonwhat
    strips parens anyway, but TMDb's query is exact-ish, so a bare title matches better."""
    return re.sub(r"\s*\([^)]*\)", "", s or "").strip()


def api_detect_preset(kind, show, name=None, title=None):
    """Auto-detect the Topaz preset for a title on the unconfigured-add path → {"preset": key|null}.
    Resolves (title, year) from the cached Plex maps (movie basename → 'Title (Year)'; TV nas_dir →
    Plex title, year from the folder), then asks preset_detect. NEVER raises — any failure (maps not
    warmed, TMDb/shotonwhat unreachable) yields null so the caller just opens the manual picker."""
    import plex
    import preset_detect
    kind = "tv" if kind == "tv" else "movie"
    if kind == "movie":
        raw = plex.peek_movie_titles().get(name or "") or title or name or ""
    else:
        raw = plex.peek_titles().get(show or "") or title or show or ""
    year = _year_from(raw) or _year_from(show or "")
    try:
        key = preset_detect.detect_preset(_clean_title(raw), year, kind)
    except Exception:
        key = None
    return {"preset": key if key in settings.TOPAZ_PRESETS else None}


def selftest_grants():
    """Moved to engine/preflight.py (one implementation for the CLI + the app); kept as a
    thin alias for existing callers."""
    return preflight.selftest_grants()


_PREFLIGHT_CACHE = {"t": 0.0, "result": None}


def selftest_full():
    """The app's selftest: TCC grants (authoritative in THIS process's context) + the cheap
    exact-version/display gates from preflight (plist reads + CoreGraphics — sub-ms; the
    heavy --network/--smoke checks never run here). Cheap checks cached 60 s."""
    import time as _t
    r = preflight.selftest_grants()
    now = _t.monotonic()
    if _PREFLIGHT_CACHE["result"] is None or now - _PREFLIGHT_CACHE["t"] > 60:
        _PREFLIGHT_CACHE["result"] = preflight.run_cheap()
        _PREFLIGHT_CACHE["t"] = now
    cheap = _PREFLIGHT_CACHE["result"]
    by_id = {c["id"]: c for c in cheap}
    r["resolve_version_ok"] = by_id["resolve_version"]["ok"]
    r["topaz_version_ok"] = by_id["topaz_version"]["ok"]
    r["display_ok"] = by_id["display"]["ok"]
    r["hard_ok"] = all(c["ok"] for c in cheap)
    r["found"] = {c["id"]: c["detail"] for c in cheap if not c["ok"]}
    r["ok"] = r["ok"] and r["hard_ok"]
    # setup_complete drives the app's "Finish setup" card + the Setup section's
    # auto-expand. A FULL pass costs ~1.4 s, so it rides its own 300 s cache here
    # (the 5 s /api/preflight cache refreshes it whenever the Setup UI is open).
    if _FULL_PREFLIGHT["result"] is not None and now - _FULL_PREFLIGHT["t"] < 300:
        r["setup_complete"] = _FULL_PREFLIGHT["result"]["ok"]
    else:
        r["setup_complete"] = api_preflight()["setup_complete"]
    return r



def request_accessibility():
    """Fire the macOS Accessibility approval POPUP for THIS process's responsible app
    (the bundle), via AXIsProcessTrustedWithOptions(prompt=True). Manual list-adding
    can mis-attribute the grant; the popup grants the exact responsible process. Returns
    {prompted, trusted}; the popup appears when trusted is still False."""
    try:
        import ctypes, ctypes.util
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        ax = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        key = cf.CFStringCreateWithCString(None, b"AXTrustedCheckOptionPrompt", 0x08000100)
        true_val = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
        keys = (ctypes.c_void_p * 1)(key)
        vals = (ctypes.c_void_p * 1)(true_val)
        kcb = ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")
        vcb = ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks")
        cf.CFDictionaryCreate.restype = ctypes.c_void_p
        cf.CFDictionaryCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                          ctypes.POINTER(ctypes.c_void_p), ctypes.c_long,
                                          ctypes.c_void_p, ctypes.c_void_p]
        opts = cf.CFDictionaryCreate(None, keys, vals, 1, ctypes.byref(kcb), ctypes.byref(vcb))
        ax.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        ax.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        return {"prompted": True, "trusted": bool(ax.AXIsProcessTrustedWithOptions(opts))}
    except Exception as e:
        return {"prompted": False, "error": str(e)}


def request_screen_recording():
    """Fire the macOS Screen Recording approval prompt for this process's responsible
    app, via CGRequestScreenCaptureAccess. macOS shows the dialog at most once per boot;
    the grant takes effect after an app relaunch (the Setup row says both)."""
    try:
        import ctypes, ctypes.util
        cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
        cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
        return {"prompted": True, "granted": bool(cg.CGRequestScreenCaptureAccess())}
    except Exception as e:
        return {"prompted": False, "error": str(e)}


# ---- in-app Setup (onboarding) ---------------------------------------------------
# RESOURCES_DIR: Contents/Resources in the bundle, the repo root in a checkout — both
# hold engine/ beside setup/, bundle/ and nas/ (build.sh ships all four).
RESOURCES_DIR = os.path.dirname(ENGINE_DIR)

_FULL_PREFLIGHT = {"t": 0.0, "result": None}    # 5 s cache (a full offline pass ≈ 1.4 s)
_FULL_PREFLIGHT_LOCK = threading.Lock()


def _invalidate_preflight():
    """Config saved / an install finished / grants changed — force fresh answers."""
    _FULL_PREFLIGHT["result"] = None
    _PREFLIGHT_CACHE["result"] = None


def api_preflight(fresh=False):
    """The FULL check list for the Setup section (12 checks, fix strings included) —
    on demand only, never on the poll. run_checks(in_app=True): the server runs inside
    the app's TCC context, so the grants check is authoritative and severity=fail."""
    import time as _t
    with _FULL_PREFLIGHT_LOCK:
        now = _t.monotonic()
        if fresh or _FULL_PREFLIGHT["result"] is None or now - _FULL_PREFLIGHT["t"] > 5:
            _FULL_PREFLIGHT["result"] = preflight.run_checks(in_app=True)
            _FULL_PREFLIGHT["t"] = now
        r = dict(_FULL_PREFLIGHT["result"])
    r["setup_complete"] = r["ok"]
    r["brew_present"] = os.path.exists("/opt/homebrew/bin/brew")
    return r


def _discover_plex(hosts):
    """Tokenless: :32400/identity answers without auth; version parsed off the
    MediaContainer tag (a bare version= match grabs the XML declaration's 1.0)."""
    import re as _re
    import urllib.request
    for h in hosts:
        base = f"http://{h}:32400"
        try:
            with urllib.request.urlopen(base + "/identity", timeout=3) as resp:
                body = resp.read(2000).decode("utf-8", "replace")
            ver = _re.search(r'<MediaContainer[^>]*\bversion="([^"]+)"', body)
            return {"ok": True, "url": base,
                    "detail": f"found Plex {ver.group(1) if ver else ''} at {base}".strip()}
        except Exception:
            continue
    return {"ok": False, "detail": f"no Plex answered on :32400 at {', '.join(hosts[:3])}"}


def _discover_youtarr(hosts):
    """Presence only — youtarr's web app answers on :3087; the login creds stay the
    user's (there is no tokenless identity endpoint)."""
    import urllib.request
    for h in hosts:
        base = f"http://{h}:3087"
        try:
            with urllib.request.urlopen(base + "/", timeout=3) as resp:
                if resp.status == 200:
                    return {"ok": True, "url": base,
                            "detail": f"found youtarr at {base} — add its login below"}
        except Exception:
            continue
    return {"ok": False, "detail": "no youtarr on :3087 (optional)"}


def _discover_relay(hosts):
    """The Shuttle relay serves /healthz BEFORE auth by design. If Shuttle's local token
    file exists we also VERIFY it (authenticated /v1/targets) — a found+authed relay is a
    complete connection: the token never needs typing."""
    import urllib.request
    import companion
    token = companion.relay_token()
    for h in hosts:
        base = f"http://{h}:8789"
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=3) as resp:
                if resp.status != 200:
                    continue
        except Exception:
            continue
        if token:
            try:
                req = urllib.request.Request(base + "/v1/targets",
                                             headers={"Authorization": "Bearer " + token})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        return {"ok": True, "url": base, "authed": True,
                                "detail": f"found the Shuttle relay at {base} — connected "
                                          "(token from the Shuttle app)"}
            except Exception:
                pass
            return {"ok": True, "url": base, "authed": False,
                    "detail": f"found the Shuttle relay at {base} — its token was refused; "
                              "open Shuttle once to refresh it"}
        return {"ok": True, "url": base, "authed": False,
                "detail": f"found the Shuttle relay at {base} — set up the Shuttle app "
                          "for the token"}
    return {"ok": False, "detail": "no Shuttle relay on :8789 (optional)"}


def api_config_test(what):
    """Live connectivity probe for one Setup group, bounded timeouts, secrets never in
    the response. 'not configured' is a clean non-error (the UI shows it neutrally)."""
    import urllib.request
    try:
        if what == "ftp":
            if not transfer.nas_hosts():
                return {"ok": False, "detail": "not configured"}
            ftp = transfer.connect(timeout=6)
            host = ftp.host
            try:
                ftp.quit()
            except Exception:
                pass
            return {"ok": True, "detail": f"connected to {host}"}
        if what == "plex":
            import plex
            token = plex.plex_token() if hasattr(plex, "plex_token") else None
            base = (plex.plex_base_urls() or [None])[0]
            if not base or not token:
                return {"ok": False, "detail": "not configured"}
            req = urllib.request.Request(base + "/identity",
                                         headers={"X-Plex-Token": token})
            with urllib.request.urlopen(req, timeout=8) as resp:
                return {"ok": resp.status == 200, "detail": f"Plex answered at {base}"}
        if what == "plex-discover":
            hosts = transfer.nas_hosts()
            if not hosts:
                return {"ok": False, "detail": "configure the NAS first"}
            return _discover_plex(hosts)
        if what == "auto-connect":
            # ONE sweep for everything discoverable once the NAS is known (user-asked):
            # Plex :32400 (/identity is tokenless), youtarr :3087 (presence — creds stay
            # the user's), the Shuttle relay :8789 (/healthz answers before auth BY
            # DESIGN — and the relay token comes from Shuttle's own local file, so a
            # found relay is a COMPLETE connection, no typing at all).
            hosts = transfer.nas_hosts()
            if not hosts:
                return {"ok": False, "detail": "configure the NAS first"}
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=3) as ex:
                futs = {"plex": ex.submit(_discover_plex, hosts),
                        "youtarr": ex.submit(_discover_youtarr, hosts),
                        "relay": ex.submit(_discover_relay, hosts)}
                found = {k: f.result() for k, f in futs.items()}
            return {"ok": any(v.get("ok") for v in found.values()), "found": found}
        if what == "youtarr":
            import youtarr
            bases = youtarr.base_urls()
            if not bases or not all(youtarr._creds()):
                return {"ok": False, "detail": "not configured"}
            for base in bases:
                if youtarr._login(base):
                    # AUTOMATIC on connect (user-asked): youtarr's own settings are a
                    # contract Visionary depends on — where downloads land, the sidecars it
                    # copies, whether youtarr fetches at all — and they used to be hand-set
                    # with nothing checking them. Bring them into line here, in the setup
                    # step where the user is already connecting youtarr. The output
                    # DIRECTORY is never rewritten (only youtarr knows its mount layout);
                    # a wrong one is reported instead.
                    fixed = {}
                    try:
                        import youtube as _yt
                        fixed = (_yt.apply_youtarr_contract() or {}).get("changed") or {}
                    except Exception:
                        pass
                    note = (" · set " + ", ".join(sorted(fixed))) if fixed else ""
                    return {"ok": True, "detail": f"youtarr login ok at {base}{note}"}
            return {"ok": False, "detail": "youtarr login failed (check url/user/pass)"}
        if what == "relay":
            import companion
            if not companion.configured():
                return {"ok": False, "detail": "not configured"}
            companion.relay_get_json("/v1/targets", timeout=8)
            return {"ok": True, "detail": "relay answered"}
        if what == "tmdb":
            import tmdb
            key = tmdb._api_key()
            if not key:
                return {"ok": False, "detail": "not configured"}
            url = f"https://api.themoviedb.org/3/configuration?api_key={key}"
            with urllib.request.urlopen(url, timeout=8) as resp:
                return {"ok": resp.status == 200, "detail": "TMDb key accepted"}
        return {"ok": False, "detail": f"unknown test {what!r}"}
    except Exception as e:
        # exception text can carry host names but never credentials (nothing here puts
        # a secret into a URL or an error string)
        return {"ok": False, "detail": f"{e.__class__.__name__}: {e}"}


def api_import_resolve():
    """Run the bundled setup/import_resolve.py as a SUBPROCESS job (fusionscript can
    hang holding the GIL — it must never run inside the server). Hard guard first:
    Resolve must be closed (it rewrites DeliverPresetList.xml on exit)."""
    script = os.path.join(RESOURCES_DIR, "setup", "import_resolve.py")
    if not os.path.exists(script):
        return {"error": "missing", "detail": "setup/import_resolve.py not in this build"}
    try:
        r = subprocess.run(["pgrep", "-f", "DaVinci Resolve.app/Contents/MacOS/Resolve"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return {"error": "resolve-running",
                    "detail": "Quit DaVinci Resolve first — it rewrites the render-preset "
                              "file on exit and would undo the import."}
    except Exception:
        pass
    import setup_jobs
    return setup_jobs.start("import_resolve",
                            argv=[preflight.ENGINE_PYTHON, script, "--json"])


DV_PROBE_CRON = "0 5 * * * /usr/bin/python3 {fs_path} all"


def api_install_dv_probe():
    """Upload the optional NAS-side DV prober over the configured FTP and hand back the
    cron line to paste into the NAS scheduler. The FTP path→filesystem mapping default
    matches UGOS (/Media/... = /volume1/Media/...), both overridable in config."""
    import configstore
    local = os.path.join(RESOURCES_DIR, "nas", "dv_probe.py")
    if not os.path.exists(local):
        return {"ok": False, "detail": "nas/dv_probe.py not in this build"}
    cfg = configstore.read()
    remote_dir = str(cfg.get("dv_probe_remote_dir") or "/Media/Config").rstrip("/")
    try:
        ftp = transfer.connect(timeout=10)
        try:
            try:
                ftp.mkd(remote_dir)               # best-effort; exists = fine
            except Exception:
                pass
        finally:
            try:
                ftp.quit()
            except Exception:
                pass
        ok, remote, reason = transfer.upload(local, remote_dir)
        if not ok:
            return {"ok": False, "detail": reason}
    except Exception as e:
        return {"ok": False, "detail": f"{e.__class__.__name__}: {e}"}
    fs_path = str(cfg.get("dv_probe_fs_path") or ("/volume1" + remote_dir + "/dv_probe.py"))
    return {"ok": True, "uploaded_to": remote_dir + "/dv_probe.py",
            "cron": DV_PROBE_CRON.format(fs_path=fs_path), "optional": True}


def up_next(limit=10, current=None, inflight=None):
    """The next ≤`limit` UPCOMING items in PROCESSING order — the active series ROUND-ROBINED
    (one episode each from the rotation pointer, looping), with movies interleaved by their slot
    (movies.pos = episodes ahead). EVERYTHING already IN the pipeline is EXCLUDED in every form
    (pinned episode / still-pending video / still-queued movie): the run-thread `current` PLUS the
    finisher-owned `inflight` items (their remux/upload hasn't put a DV master on the NAS, so they
    still look 'remaining' — but they're committed and must NEVER re-appear as 'next', even if the
    queue is re-sorted underneath them). They show in the 'now processing'/'finishing' surfaces
    instead. The YouTube cadence is modelled from AFTER all of them complete. Matches
    orchestrator._next_episode (whose skip set already excludes the finisher's keys)."""
    import movies
    current = current or {}
    cur_kind = current.get("kind")
    # Items ALREADY in the pipeline (run-thread current + finisher-owned) — exclude by KEY so a
    # re-sorted queue can never float one of them into 'next'.
    committed = [c for c in ([current] + list(inflight or [])) if c]
    ep_excl = {c.get("ep")   for c in committed if c.get("kind") == "episode"}
    mv_excl = {c.get("name") for c in committed if c.get("kind") == "movie"}
    yt_excl = {c.get("name") for c in committed if c.get("kind") == "youtube"}
    active = series.get_active_series()
    rotation = series.get_rotation()
    queues = [list((series.cached_queue(name) or {}).get("remaining_items", [])) for name in active]
    eps, n = [], len(active)                                     # round-robin into one ep stream
    if n:
        ptr, total, i, taken = [0] * n, sum(len(q) for q in queues), rotation % n, 0
        while taken < total and len(eps) <= limit:
            if ptr[i] < len(queues[i]):
                it = queues[i][ptr[i]]; ptr[i] += 1; taken += 1
                eps.append({"ep": it.get("ep"), "source_name": it.get("source_name"), "series": active[i]})
            i = (i + 1) % n
    eps = [e for e in eps if e.get("ep") not in ep_excl]        # in-flight episodes are not "next"
    import youtube
    try:                                                       # live cadence position (episodes already
        import orchestrator as _orch                           # done since the last YouTube video) + the
        tv_since = max(0, int(getattr(_orch.ORCH, "_tv_since_yt", 0)))   # PARKED set (skipped like reality)
        parked = getattr(_orch.ORCH, "_parked", None) or set()
        in_burst = max(0, int(getattr(_orch.ORCH, "_yt_in_burst", 0)))   # of THIS burst, already done
    except Exception:
        tv_since, parked, in_burst = 0, set(), 0
    mvs = sorted(movies.get_selected(), key=movies._pos)        # stable by slot, then add-order
    mvs = [m for m in mvs if m.get("name") not in mv_excl]      # in-flight movies are not "next"
    _st = settings.get_settings()
    every = max(1, int(_st.get("youtube_every_tv_episodes", 2)))
    burst = max(1, int(_st.get("youtube_videos_per_burst", 1) or 1))   # videos per firing
    yt_videos = list(youtube.all_pending(skip=parked))         # flat, newest-first — 1 served per `every` eps
    yt_videos = [v for v in yt_videos if v.get("source_name") not in yt_excl]   # in-flight videos not "next"
    # WHERE THE CURRENT BURST STANDS. `_yt_in_burst` counts videos that have HANDED OFF; a
    # video on the run thread right now has not, and is excluded from yt_videos anyway, so it
    # still occupies one of the burst's slots.
    burst_done = in_burst + (1 if cur_kind == "youtube" else 0)
    if burst_done >= burst:
        # That burst finishes with the current video, so the countdown restarts (exactly what
        # _advance_cadence_at_handoff does) and the NEXT group of videos is a whole fresh burst.
        if cur_kind == "youtube":
            tv_since = 0
        first_burst = burst
    else:
        # Mid-burst: what runs before the next episode is the REMAINDER, not a fresh burst —
        # ten set, four done, six shown (user-dictated 2026-08-19). The countdown is NOT reset
        # here: resetting it after every video modelled a burst as though it were always one
        # video, so mid-burst the queue claimed TV came next when the rest of the burst does.
        first_burst = burst - burst_done
    if cur_kind == "episode":
        tv_since += 1                                          # after this episode completes, it advances
    tv_since += sum(1 for c in (inflight or []) if c.get("kind") == "episode")   # finisher eps complete too
    # 'title'/'source_name' are DISPLAY fields → wire-decoded; 'name'/'ep' are ACTION KEYS
    # (remove/reorder round-trip them) → kept in exact wire form.
    movie_item = lambda m: {"kind": "movie", "name": m.get("name"),
                            "title": transfer.display_name(m.get("title"))}
    ep_item = lambda e: {"kind": "episode", "ep": e.get("ep"), "series": e.get("series"),
                         "source_name": transfer.display_name(e.get("source_name"))}
    # A video the user pressed "run now" on carries priority=True, so the row can SAY it is
    # queued to jump. Without that the request looked inert: the pipeline only yields at the
    # next Topaz segment boundary (deliberately — see the run-now docs), which can be a
    # couple of minutes, and nothing in the UI acknowledged the press.
    try:
        import youtube as _yt
        _prio_vids = {e.get("vid") for e in _yt._priority() if e.get("vid")}
    except Exception:
        _prio_vids = set()
    yt_item = lambda v: {"kind": "youtube", "channel": v.get("channel"),
                         "name": v.get("source_name"), "title": v.get("title"),
                         "priority": bool(v.get("vid") and v.get("vid") in _prio_vids)}
    out, mi, yi, ep_count = [], 0, 0, tv_since
    # `limit` counts TV EPISODES (user-dictated): the queue always shows ten episodes of
    # actual show, with movies and YouTube videos riding along BETWEEN them rather than
    # consuming the budget. Counting entries (or drawn slots) meant a burst of videos ate
    # the list and only a couple of episodes were visible. The entry ceiling still bounds
    # the payload when a long tail of videos or movies interleaves.
    MAX_ENTRIES = max(limit * 8, 80)
    def _full() -> bool:
        return (sum(1 for o in out if o.get("kind") == "episode") >= limit
                or len(out) >= MAX_ENTRIES)
    def _emit_yt_one():                                        # the single-video cadence insert
        nonlocal yi, ep_count
        out.append(yt_item(yt_videos[yi])); yi += 1
        ep_count = 0                                           # restart the N-episode countdown
        return _full()
    led_once = False
    def _emit_yt_burst():          # first group = the remainder above; later ones = whole bursts
        nonlocal led_once
        n = burst if led_once else first_burst
        led_once = True
        for _ in range(n):
            if yi >= len(yt_videos):
                break
            if _emit_yt_one():
                return True
        return False
    def _after_episode():                                      # one TV episode emitted → toward the next YT
        nonlocal ep_count
        ep_count += 1
        if ep_count >= every and yi < len(yt_videos):
            return _emit_yt_burst()
        return False
    # Counter already saturated (after `current` completes) → the orchestrator's gate serves a
    # YouTube video BEFORE the TV rotation — lead with it to match.
    if ep_count >= every and yi < len(yt_videos):
        if _emit_yt_burst(): return out
    for ei in range(len(eps) + 1):
        while mi < len(mvs) and movies._pos(mvs[mi]) == ei:    # movies due right before episode ei (a movie
            out.append(movie_item(mvs[mi])); mi += 1           # does NOT count toward the YouTube cadence)
            if _full(): return out
        if ei < len(eps):
            out.append(ep_item(eps[ei]))
            if _full(): return out
            if _after_episode(): return out
    while yi < len(yt_videos):                                 # TV exhausted → drain remaining YouTube
        if _emit_yt_one(): return out
    while mi < len(mvs):                                       # movies parked past the last episode
        out.append(movie_item(mvs[mi])); mi += 1
        if _full(): return out
    return out


def current_state():
    orch = orchestrator.ORCH.snapshot()
    state = build_state(
        power=power.read_power(),
        scratch=collect_scratch(),
        adapter_watts=read_adapter_watts(),
        in_win=in_window(datetime.datetime.now().time(), WINDOW_START, WINDOW_END),
        automation_enabled=orch["enabled"],     # live: the orchestrator's arm state
    )
    # scratch preview: local files inherit the FTP wire name, so decode for display
    state["scratch_contents"] = [{**it, "name": transfer.display_name(it.get("name"))}
                                 for it in scratch.folder_preview()]
    state["mode"] = series.get_mode()
    state["series"] = series_info()
    state["movies"] = movies_info()
    state["youtube"] = youtube_info()
    state["up_next"] = up_next(current=orch.get("current"), inflight=orchestrator.ORCH.finisher_views())
    state["orchestrator"] = orch
    state["settings"] = settings.get_settings()
    state["show_profile"] = show_profile_info()
    state["log"] = logbook.tail(6)            # recent failures/errors for the UI
    return state


# ---- HTTP -----------------------------------------------------------------

# ---- remote access (Tailscale) -------------------------------------------
# The server used to bind 127.0.0.1, so reaching it at all meant already being on this Mac.
# It now binds every interface so the dashboard can be opened from a phone over the tailnet
# — which means the port is no longer its own protection. This API can ARM, DISARM, SKIP and
# DELETE, so everything arriving from off-machine must present a token. Loopback stays
# exempt: the Mac app talks to us there and must keep working untouched.
BIND_ADDR = os.environ.get("VISIONARY_BIND", "0.0.0.0")
TOKEN_FILE = os.path.expanduser("~/.topaz-pipeline/remote_token")
REMOTE_AWAKE_GRACE_SECS = 60 * 60      # a remote Deactivate keeps the Mac reachable this long
_LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def remote_token() -> str:
    """The shared secret, generated once and kept 0600 in its own file (NOT config.json —
    nothing that gets echoed into the UI or a bug report). Never logged: log_message is a
    no-op, so the ?k= form can't leak into an access log."""
    try:
        with open(TOKEN_FILE) as fh:
            tok = fh.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    try:
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(tok)
    except OSError:
        pass
    return tok


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _authorized(self) -> bool:
        """Loopback is trusted; anything else needs the token, via an Authorization header,
        a ?k= query (so a phone can open one link), or the cookie that link then sets.
        Compared with compare_digest — a plain == leaks the answer through timing."""
        self._token_via_query = False
        host = (self.client_address or ("",))[0]
        if host in _LOOPBACK:
            return True
        want = remote_token()
        if not want:
            return False                      # couldn't establish a secret -> refuse, never open up
        auth = self.headers.get("Authorization") or ""
        got = auth[7:].strip() if auth.startswith("Bearer ") else ""
        if not got:
            got = (parse_qs(urlparse(self.path).query).get("k") or [""])[0]
            self._token_via_query = bool(got)
        if not got:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "vk":
                    got = v
                    break
        return bool(got) and secrets.compare_digest(got, want)

    def _deny(self):
        self._send(401, b"unauthorized", "text/plain",
                   {"WWW-Authenticate": "Bearer realm=\"Visionary\""})

    def do_GET(self):
        if not self._authorized():
            return self._deny()
        path = self.path.split("?")[0]
        if path == "/api/state":
            self._json(current_state())
        elif path == "/api/series":
            self._json(api_series())
        elif path == "/api/movies":
            self._json(api_movies())
        elif path == "/api/history":
            import history
            self._json({"items": history.view()})
        elif path == "/api/channels":
            self._json(api_channels())
        elif path == "/api/settings":
            self._json({"settings": settings.get_settings(),
                        "defaults": settings.DEFAULT_SETTINGS})
        elif path == "/api/config":
            # REDACTED view only — secret values never leave configstore (Setup UI
            # learns set/unset). Deliberately NOT part of /api/state.
            import configstore
            self._json(configstore.read_redacted())
        elif path == "/api/preflight":
            fresh = (parse_qs(urlparse(self.path).query).get("fresh") or ["0"])[0] == "1"
            self._json(api_preflight(fresh=fresh))
        elif path == "/api/setup/install-status":
            import setup_jobs
            self._json(setup_jobs.status())
        elif path == "/api/borders/status":
            self._json(api_borders_status())
        elif path == "/api/media-libraries":
            # Which NAS folders are TV / Movies / YouTube, auto-decided from Plex's own
            # library sections, plus the user's overrides and what is actually in force.
            import medialibs
            self._json(medialibs.status())
        elif path == "/api/youtarr-config":
            import youtube as _yt
            self._json(_yt.youtarr_config_status())
        elif path == "/api/show-profile":
            show = (parse_qs(urlparse(self.path).query).get("show") or [None])[0]
            self._json(show_profile_info(show))
        elif path == "/api/selftest":
            self._json(selftest_full())
        elif path == "/api/screen.png":
            # SEE THE SCREEN WITH THE LID CLOSED. Only this process holds the Screen
            # Recording grant (a plain shell's screencapture returns "could not create
            # image from display"), so remote debugging of the DV automation has to come
            # from here — and from a phone, which is the whole point of the web UI. It is
            # the remote TOKEN that protects this now, not the old loopback bind.
            # ?display=<key> captures a NON-main screen. Once Resolve can be hosted
            # elsewhere, a debugger that always shows the built-in is worse than useless.
            try:
                import tempfile, dv_shim, displays
                key = (parse_qs(urlparse(self.path).query).get("display") or [None])[0]
                target = displays.find(key) if key else None
                if key and not target:
                    self._json({"error": "display not attached: %s" % key}, code=404); return
                prev = dv_shim.get_host()
                try:
                    dv_shim.set_host(target)
                    png = os.path.join(tempfile.gettempdir(), "_api_screen.png")
                    dv_shim.screenshot(png)
                finally:
                    dv_shim.set_host(prev)
                with open(png, "rb") as f:
                    self._send(200, f.read(), "image/png")
            except Exception as e:
                self._json({"error": f"screenshot failed: {e.__class__.__name__}: {e}"}, code=500)
        elif path == "/api/resolve-preview.jpg":
            # Serve the LATEST captured frame immediately and refresh in the background.
            #
            # Capturing per request does not work: measured at 1.9 s, 9.9 s and 3.7 s for one
            # frame of a 3840x2160 panel while Resolve has the machine. A 2 s client poll
            # therefore queued requests faster than they completed — every frame arrived
            # stale, and overlapping requests raced on one shared temp file. Decoupling the
            # two means the client always gets the freshest frame that EXISTS, at whatever
            # rate captures actually manage, and never waits on one.
            big = (parse_qs(urlparse(self.path).query).get("size") or [""])[0] == "big"
            frame = _preview_frame(big=big)
            if frame is None:
                self._send(204, b"", "image/jpeg")     # nothing captured yet — try again
            else:
                self._send(200, frame, "image/jpeg")
        elif path == "/api/shim-smoke":
            # Per-template match scores against the LIVE screen + display/lock context —
            # the acceptance gate for a display config and the first thing to check when
            # the Resolve stage misbehaves on a screen nobody can see.
            key = (parse_qs(urlparse(self.path).query).get("display") or [None])[0]
            self._json(preflight.shim_smoke_scores(key))
        elif path == "/api/displays":
            # Every attached screen + whether it could host Resolve + the saved priority.
            # NOT folded into /api/state: that polls every 1.5 s and re-enumerating
            # displays at that rate is pointless work.
            self._json(displays_view())
        elif path == "/oauth/youtube":
            import ytdata
            code = (parse_qs(urlparse(self.path).query).get("code") or [""])[0]
            ok = ytdata.exchange_code(code, YT_REDIRECT) if code else False
            msg = ("YouTube connected — you can close this tab and return to Visionary."
                   if ok else "YouTube connection failed. Close this tab and try Connect again.")
            self._send(200 if ok else 400,
                       f"<html><body style='font:16px -apple-system;padding:3em;text-align:center'>"
                       f"<h2>{msg}</h2></body></html>".encode(), "text/html; charset=utf-8")
        elif self.path in ("/", "/index.html"):
            try:
                with open(os.path.join(DASHBOARD_DIR, "index.html"), "rb") as f:
                    # NEVER let a browser hold the old UI: the page is redeployed under a
                    # fixed URL, so a cached copy silently survives every deploy (user-hit
                    # 2026-08-18 — the rebuilt dashboard looked unchanged until a hard reload).
                    extra = {"Cache-Control": "no-store, must-revalidate"}
                    if getattr(self, "_token_via_query", False):
                        # Trade the one-time ?k= link for a cookie so the bookmark, the
                        # history entry and any shoulder-surfer never carry the secret.
                        extra["Set-Cookie"] = ("vk=" + remote_token() + "; Path=/; Max-Age=31536000;"
                                               " HttpOnly; SameSite=Lax")
                    self._send(200, f.read(), "text/html; charset=utf-8", extra)
            except OSError:
                self._send(404, b"index.html missing", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if not self._authorized():
            return self._deny()
        path = self.path.split("?")[0]
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except (ValueError, json.JSONDecodeError) as e:
            return self._json({"error": str(e)}, 400)
        if path == "/api/select":
            name = (body.get("series") or "").strip()
            action = (body.get("action") or "set").strip()
            try:
                index = int(body.get("index", 0))
            except (TypeError, ValueError):
                index = 0
            if not name:
                return self._json({"error": "missing series"}, 400)
            self._json(api_select(name, action, index))
        elif path == "/api/mode":
            self._json(api_mode((body.get("mode") or "tv").strip()))
        elif path == "/api/movie-queue":
            self._json(api_movie_queue(body or {}))
        elif path == "/api/companion":
            self._json(api_companion(body or {}))
        elif path == "/api/queue-action":
            self._json(api_queue_action(body or {}))
        elif path == "/api/youtube-connect":
            self._json(api_youtube_connect(body or {}))
        elif path == "/api/youtube-queue":
            self._json(api_youtube_queue(body or {}))
        elif path == "/api/refresh-library":
            self._json(api_refresh_library())
        elif path == "/api/detect-preset":
            self._json(api_detect_preset(
                (body.get("kind") or "tv").strip(),
                (body.get("show") or "").strip(),
                (body.get("name") or "").strip() or None,
                (body.get("title") or "").strip() or None))
        elif path == "/api/automation":
            # Activate/Deactivate: the toggle is PERSISTED (appliance mode) — while activated,
            # the rearm daemon keeps the orchestrator enabled whenever it can run (on launch,
            # and after an auto-stop once the overnight window reopens). _ARM_LOCK serializes
            # this with the daemon so a Deactivate can't be immediately undone by a rearm that
            # already passed its check.
            with _ARM_LOCK:
                if body.get("enabled"):
                    # HARD GATE (server-side so the UI can't bypass it): never arm on a machine
                    # that isn't the exact Resolve/Topaz/display Visionary is built for — the
                    # screen automation would click the wrong pixels (see engine/versions.py).
                    # run_arm_gate = the version/display pins PLUS the instant
                    # dependency checks (brew tools, cv2) — a missing x265 used to
                    # surface HOURS into a job; now it refuses to arm, named.
                    gate = preflight.run_arm_gate()
                    bad = [c for c in gate if not c["ok"]]
                    if bad:
                        self._json({"error": "preflight failed — refusing to arm",
                                    "checks": bad}, code=409)
                        return
                    settings.set_settings({"activated": True})
                    orchestrator.ORCH.enable()
                else:
                    settings.set_settings({"activated": False})
                    # A Deactivate from OFF-MACHINE is remote by definition (the web UI is
                    # only reachable over the tailnet), and a laptop that sleeps can't be
                    # woken from away — so a remote stop would be a one-way door. Hold the
                    # screen for a grace period so it can still be re-armed; the hold
                    # refuses itself on battery (see ORCH.hold_awake).
                    remote = (self.client_address or ("",))[0] not in _LOOPBACK
                    orchestrator.ORCH.disable(
                        keep_awake_secs=REMOTE_AWAKE_GRACE_SECS if remote else 0)
            self._json(orchestrator.ORCH.snapshot())
        elif path == "/api/send-to-visionary":
            # The companion YouTube app's button. Off-machine callers need the remote
            # token (see _authorized); loopback is still open, so the app on this Mac is
            # unaffected. youtarr grabs exactly this video; the orchestrator serves it
            # as the NEXT item once it lands on staging. Idempotent; the status string
            # is the button's feedback.
            import youtube
            self._json(youtube.send_priority((body.get("url") or body.get("id") or ""),
                                             title=body.get("title")))
        elif path == "/api/config":
            # values are never logged (log_message is a no-op; keep it that way) and the
            # response is the redacted view, so a secret can't round-trip out
            import configstore
            out = configstore.save(body or {})
            _invalidate_preflight()
            self._json(out)
        elif path == "/api/config-test":
            self._json(api_config_test((body or {}).get("what") or ""))
        elif path == "/api/setup/install":
            import setup_jobs
            what = (body or {}).get("what") or ""
            argv = None
            if what.startswith("borders_"):
                # Border-extender model downloads: argv is ENGINE-computed (the
                # import_resolve escape hatch — nothing user-supplied reaches a shell)
                # and lands in Comfy Desktop's own models directory. curl -C - resumes
                # a truncated file in place.
                import borders
                env = borders.discover()
                argv = borders.model_download_argv(what, env.get("models_dir") or "")
                if not argv:
                    self._json({"error": "missing",
                                "detail": "Comfy Desktop's models directory not found — "
                                          "install and run Comfy Desktop once"})
                    return
            out = setup_jobs.start(what, argv=argv)
            _invalidate_preflight()          # a finished job re-detects on next preflight
            self._json(out, code=(409 if out.get("error") == "busy" else 200))
        elif path == "/api/media-libraries":
            # Save per-library kind overrides and/or APPLY the resulting roots. Applying
            # writes media_roots, which transfer reads at import — so it takes effect on
            # the next launch; the response says so rather than pretending otherwise.
            import configstore, medialibs
            body = body if isinstance(body, dict) else {}
            updates = {}
            if isinstance(body.get("kinds"), dict):
                updates["media_lib_kinds"] = body["kinds"]
            if updates:
                configstore.save(updates)
            if body.get("apply"):
                st = medialibs.status()
                roots = {k: v["roots"] for k, v in (st.get("proposed") or {}).items()
                         if v.get("roots")}
                if not roots:
                    return self._json({"error": "nothing-detected",
                                       "detail": st.get("detail") or
                                                 "No Plex libraries resolved to NAS folders."}, 400)
                configstore.save({"media_roots": roots})
            out = medialibs.status()
            out["restart_required"] = bool(body.get("apply")) and not out.get("matches_in_force")
            self._json(out)
        elif path == "/api/youtarr-config":
            # Apply the settings Visionary requires of youtarr (the same thing that runs
            # automatically when youtarr is connected in Setup).
            import youtube as _yt
            out = _yt.apply_youtarr_contract()
            out["status"] = _yt.youtarr_config_status()
            self._json(out, code=(200 if not out.get("error") else 502))
        elif path == "/api/borders/reset-set-book":
            # Forget a show's remembered wing inventions (set-reference book) — the
            # lever when the AI took a set a wrong direction. The next episode
            # re-invents fresh and re-registers. Never touches finished masters.
            import borders
            show = (body.get("show") if isinstance(body, dict) else "") or ""
            if not show:
                return self._json({"error": "no show given"}, 400)
            removed, ok = borders.reset_set_book(show)
            # ok=False -> the book dir would not fully delete (permissions): say so
            # instead of letting the row silently reappear after a "successful" reset.
            self._json({"show": show, "removed": removed, "ok": ok},
                       code=(200 if ok else 500))
        elif path == "/api/setup/import-resolve":
            out = api_import_resolve()
            self._json(out, code=(409 if out.get("error") == "resolve-running" else 200))
        elif path == "/api/setup/install-dv-probe":
            self._json(api_install_dv_probe())
        elif path == "/api/request-accessibility":
            # the app's button POSTs; this was GET-only and 404'd silently (live bug)
            self._json(request_accessibility())
        elif path == "/api/request-screen-recording":
            self._json(request_screen_recording())
        elif path == "/api/settings":
            new = settings.set_settings(body or {})
            if "max_youtube_minutes" in (body or {}):
                # The popular sets bake this cap in at refresh time — invalidate so the
                # next _refresh_youtube tick rebuilds them with the new value (otherwise
                # raising the cap does nothing until the next re-arm). Best-effort, like
                # the configure_youtarr call on queue edits.
                try: orchestrator.ORCH.refresh_youtube_meta()
                except Exception: pass
            self._json({"settings": new})
        elif path == "/api/displays":
            # Reorder the priority list / flip the master switch. The priority list is
            # validated in settings (VALIDATORS) rather than here, so a hand-edited
            # settings.json is held to the same rule as the UI.
            upd = {}
            for k in ("resolve_host_pinning", "resolve_host_fallback_main"):
                if k in body:
                    upd[k] = bool(body.get(k))
            if "priority" in body:
                upd["resolve_host_displays"] = body.get("priority")
            if "warn_takeover" in body:
                upd["resolve_takeover_warn"] = bool(body.get("warn_takeover"))
            if upd:
                settings.set_settings(upd)
            self._json(displays_view())
        elif path == "/api/display-smoke":
            # Score every template against ONE display and remember the result. This is
            # what a screen must pass before it is allowed to host Resolve.
            key = body.get("display")
            res = preflight.shim_smoke_scores(key)
            preflight.record_display_smoke(key, res)
            self._json({"result": res, "displays": displays_view()})
        elif path == "/api/history-scan":
            # Adopt masters the pipeline published BEFORE the history book existed. Detection
            # is the pipeline's own naming (series.is_master_name), so a source can never be
            # mistaken for a deliverable. Backgrounded: it walks whole libraries over FTP.
            import history
            threading.Thread(target=history.scan, daemon=True, name="history-scan").start()
            self._json({"status": "started"})
        elif path == "/api/revise-audio":
            # Send a FINISHED master back through for its audio only. In place, on a daemon
            # thread: it re-measures the published file and re-applies the boost, so it takes
            # minutes rather than the hours a full re-run would — and a re-run is usually
            # impossible anyway, the source having been replaced or purged.
            import history
            nas = (body.get("id") or body.get("nas_path") or "").strip()
            row = next((r for r in history.view(500) if r.get("nas_path") == nas), None)
            if not row:
                self._json({"error": "unknown item"}, code=404)
            elif not row.get("can_revise"):
                self._json({"error": row.get("why") or "cannot revise this item"}, code=400)
            else:
                threading.Thread(target=history.revise_audio, args=(nas,),
                                 daemon=True, name="revise").start()
                self._json({"status": "started", "id": nas})
        elif path == "/api/awake-hold":
            # "Give me another hour." A remote Deactivate already holds the screen so the
            # run can be re-armed from away; this extends that hold when the window is
            # running out and the decision hasn't been made yet. Same refusal rules —
            # ORCH.hold_awake declines on battery.
            secs = body.get("seconds")
            try:
                secs = int(secs) if secs is not None else REMOTE_AWAKE_GRACE_SECS
            except (TypeError, ValueError):
                secs = REMOTE_AWAKE_GRACE_SECS
            secs = max(60, min(int(secs), 4 * 3600))
            until = orchestrator.ORCH.hold_awake(secs, "extended from the web UI")
            self._json({"held": bool(until), "awake_hold_secs":
                        orchestrator.ORCH.snapshot().get("awake_hold_secs", 0)})
        elif path == "/api/quiet-mode":
            # SCREEN CONTROL. `enabled` = quiet mode ON = the pipeline stops using the screen.
            # That is only ever TEMPORARY: `seconds` (or an `until` epoch) sets when it comes
            # back by itself, clamped to settings.MAX_QUIET_SECONDS — an indefinite pause would
            # buffer items until the disk floor stalls the run. Turning it back ON clears both.
            on = bool(body.get("enabled"))
            if not on:
                settings.set_settings({"quiet_mode": False, "quiet_until": 0})
            else:
                secs = body.get("seconds")
                if secs is None and body.get("until") is not None:
                    try:
                        secs = int(body["until"]) - int(time.time())
                    except (TypeError, ValueError):
                        secs = None
                secs = settings.clamp_quiet_seconds(3600 if secs is None else secs)
                if secs <= 0:                      # a past/zero deadline means "don't pause at all"
                    settings.set_settings({"quiet_mode": False, "quiet_until": 0})
                else:
                    settings.set_settings({"quiet_mode": True,
                                           "quiet_until": int(time.time()) + secs})
                    orchestrator.ORCH.reclaim_screen()   # abort any in-flight Resolve NOW
            self._json(orchestrator.ORCH.snapshot())
        elif path == "/api/next-up":
            # Per-slot follow-up: the show that takes this slot the moment `show` finishes.
            # Empty `next` clears it. NOT arm-gated (unlike picking the slot's CURRENT show):
            # this only records a future intent, so it's safe to set mid-run — the promotion
            # itself happens on the run thread (series.promote_finished_slots).
            show = (body.get("show") or "").strip()
            if not show:
                return self._json({"error": "no show given"}, 400)
            series.set_next_up(show, (body.get("next") or "").strip())
            self._json(series_info())
        elif path == "/api/show-profile":
            show = (body.get("show") or series.get_selection() or "").strip()
            if not show:
                return self._json({"error": "no show selected"}, 400)
            if "preset" in body:
                settings.set_show_preset(show, (body.get("preset") or "").strip())
            if "unwatched_first" in body:
                settings.set_show_unwatched_first(show, bool(body.get("unwatched_first")))
                try: series.refresh_queue(show)   # re-order the queue with the new setting
                except Exception: pass
            if "normalize_audio" in body:
                # Per-item loudness-boost gate — `show` is the show_profiles key, so movies
                # (title) and YouTube channels (folder) reuse this endpoint verbatim. No
                # queue refresh: audio doesn't affect ordering.
                settings.set_show_normalize_audio(show, bool(body.get("normalize_audio")))
            if "featurettes_last" in body:
                settings.set_show_featurettes_last(show, bool(body.get("featurettes_last")))
                try: series.refresh_queue(show)      # re-order the queue with the new setting
                except Exception: pass
            if "replace_source" in body:
                # Per-item upload policy (shows + movies): replace the source with the
                # verified master (default) vs keep both. No queue refresh needed.
                settings.set_show_replace_source(show, bool(body.get("replace_source")))
            if "output_mode" in body:
                # What Resolve should OUTPUT for this item: auto (the long-standing rule) or a
                # pinned sdr / dv1000 / dv2000. Changing it changes the DELIVERABLE'S NAME too
                # (SDR masters carry a different done-mark), so it only affects items not yet
                # shipped — anything already finished keeps the name it was shipped under.
                settings.set_show_output_mode(show, body.get("output_mode"))
            if "extend_borders" in body:
                # AI border extension (4:3 -> 16:9) — the extend stage's per-show opt-in.
                # The UI only renders the row on 4:3 shows with the models installed; the
                # stage re-gates per episode. No queue refresh: ordering is unaffected.
                settings.set_show_extend_borders(show, bool(body.get("extend_borders")))
            if "extend_prompt" in body:
                # The show's WING PROMPT (continuity tier 1) — what the generated side
                # wings should contain. "" = the built-in default. Part of the extend
                # chunks' resume identity, so changing it re-generates unprocessed work.
                settings.set_show_extend_prompt(show, str(body.get("extend_prompt") or ""))
            self._json(show_profile_info(show))
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *args):
        pass


def _shutdown(*_a):
    """On server exit (app quit / SIGTERM), stop the run and kill any Topaz encode so
    nothing is left orphaned (reparented to launchd) burning GPU after we're gone."""
    try:
        import topaz
        topaz.terminate_all()
    except Exception:
        pass
    try:
        orchestrator.ORCH.disable("server shutdown")
    except Exception:
        pass


_ARM_LOCK = threading.Lock()   # serializes Activate/Deactivate with the rearm daemon


def _rearm_loop():
    """APPLIANCE mode daemon: while the user has ACTIVATED the app, keep the orchestrator
    enabled whenever it can run. Checks FIRST (so a login launch arms without waiting a
    tick), then every 60s. Deactivating (activated=False) stops all re-arming — the check
    and the enable happen under _ARM_LOCK so a concurrent Deactivate can't be undone."""
    import time as _time
    while True:
        try:
            with _ARM_LOCK:
                s = settings.get_settings()               # read INSIDE the lock (fresh)
                if should_rearm(activated=bool(s.get("activated")),
                                enabled=bool(orchestrator.ORCH.snapshot().get("enabled"))):
                    # same HARD GATE as POST /api/automation — the rearm daemon must not
                    # arm a machine that fails the exact-version/display preflight.
                    if all(c["ok"] for c in preflight.run_arm_gate()):
                        print("appliance: activated + idle → re-arming the run")
                        orchestrator.ORCH.enable()
                    else:
                        print("appliance: preflight failed — NOT re-arming (see /api/selftest)")
        except Exception:
            pass
        _time.sleep(60)


def main(port=8765):
    atexit.register(_shutdown)
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, lambda *_a: sys.exit(0))   # SystemExit unwinds → atexit runs _shutdown
        except (ValueError, OSError):
            pass
    threading.Thread(target=_rearm_loop, daemon=True, name="rearm").start()
    print(f"Dashboard (automation_enabled={AUTOMATION_ENABLED}) on http://localhost:{port}")
    ThreadingHTTPServer((BIND_ADDR, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
