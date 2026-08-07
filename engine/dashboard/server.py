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


def build_state(*, power, scratch, adapter_watts, in_win, manifest,
                automation_enabled) -> dict:
    draining = _is_draining(power)   # param 'power' shadows the module; helper routes correctly
    job = None
    if manifest:
        job = {
            "show": manifest.get("show"),
            "total": manifest.get("total"),
            "located": manifest.get("located"),
            "missing": manifest.get("missing"),
        }
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
        "job": job,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


def _is_draining(reading):
    # import-safe call into the tested detector
    return power.is_draining_on_ac(reading)


# ---- real collectors (I/O glue) -------------------------------------------

def read_adapter_watts():
    try:
        out = subprocess.run(["pmset", "-g", "adapter"], capture_output=True, text=True).stdout
        m = re.search(r"Wattage\s*=\s*(\d+)", out)
        return int(m.group(1)) if m else None
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


def load_manifest():
    files = sorted(glob.glob(os.path.join(DASHBOARD_DIR, "manifests", "*.json")))
    if not files:
        return None
    try:
        with open(files[0]) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def series_info():
    """Active series (the round-robin set) + each one's cached queue + the {nas_dir: Plex title}
    map — fast (no NAS I/O) for state polling. `selected` is the primary (back-compat); `extras`
    are the additional round-robin shows; `rotation` is whose turn is next."""
    import plex
    plex.ensure_titles_warming()
    active = series.get_active_series()
    sel = active[0] if active else None
    # Uniform per-show info for ALL active shows, so the UI renders each as the same block.
    shows = []
    for nm in active:
        nu = series.get_next_up(nm) or None
        shows.append({"name": nm, "preset": settings.show_preset_key(nm),
                      "configured": settings.get_show_preset(nm) is not None,
                      "unwatched_first": settings.get_show_unwatched_first(nm),
                      "featurettes_last": settings.get_show_featurettes_last(nm),
                      "normalize_audio": settings.get_show_normalize_audio(nm),
                      "replace_source": settings.get_show_replace_source(nm),
                      "output_mode": settings.get_show_output_mode(nm),
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
    return {"selected": sel, "active": active, "rotation": series.get_rotation(),
            "queue": shows[0]["queue"] if shows else None,
            "shows": shows, "titles": plex.peek_titles()}


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
    add/remove/scope/cap/paused re-derive youtarr's config + meta. paused stops work on a channel without
    deleting its files; remove WIPES it. delete removes a single downloaded video + ignores it."""
    import youtube
    action = (body.get("action") or "").strip()
    cid = (body.get("channelId") or "").strip()
    reconfigure = False
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
    return {"youtube": youtube.queue_view(), "up_next": up_next(current=orchestrator.ORCH.snapshot().get("current"), inflight=orchestrator.ORCH.finisher_views())}


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
    return {"preset": settings.show_preset_key(name),
            "configured": settings.get_show_preset(name) is not None,
            "unwatched_first": settings.get_show_unwatched_first(name),
            "featurettes_last": settings.get_show_featurettes_last(name),
            # only shows the toggle when the show actually HAS season-00 specials
            "has_featurettes": int((series.cached_queue(name) or {}).get("featurette_count", 0)) > 0,
            "normalize_audio": settings.get_show_normalize_audio(name),
            "replace_source": settings.get_show_replace_source(name),
            "output_mode": settings.get_show_output_mode(name),
            "output_mode_effective": settings.effective_output_mode(name, _hdr_hint(name))}


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
    target = show or series.get_selection()
    saved = settings.get_show_preset(target) if target else None
    return {"show": target, "configured": saved is not None,
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
    except Exception:
        tv_since, parked = 0, set()
    mvs = sorted(movies.get_selected(), key=movies._pos)        # stable by slot, then add-order
    mvs = [m for m in mvs if m.get("name") not in mv_excl]      # in-flight movies are not "next"
    every = max(1, int(settings.get_settings().get("youtube_every_tv_episodes", 2)))
    yt_videos = list(youtube.all_pending(skip=parked))         # flat, newest-first — 1 served per `every` eps
    yt_videos = [v for v in yt_videos if v.get("source_name") not in yt_excl]   # in-flight videos not "next"
    if cur_kind == "youtube":
        tv_since = 0                                           # after this video completes, the counter resets
    elif cur_kind == "episode":
        tv_since += 1                                          # after this episode completes, it advances
    tv_since += sum(1 for c in (inflight or []) if c.get("kind") == "episode")   # finisher eps complete too
    # 'title'/'source_name' are DISPLAY fields → wire-decoded; 'name'/'ep' are ACTION KEYS
    # (remove/reorder round-trip them) → kept in exact wire form.
    movie_item = lambda m: {"kind": "movie", "name": m.get("name"),
                            "title": transfer.display_name(m.get("title"))}
    ep_item = lambda e: {"kind": "episode", "ep": e.get("ep"), "series": e.get("series"),
                         "source_name": transfer.display_name(e.get("source_name"))}
    yt_item = lambda v: {"kind": "youtube", "channel": v.get("channel"),
                         "name": v.get("source_name"), "title": v.get("title")}
    out, mi, yi, ep_count = [], 0, 0, tv_since
    def _emit_yt_one():                                        # the single-video cadence insert
        nonlocal yi, ep_count
        out.append(yt_item(yt_videos[yi])); yi += 1
        ep_count = 0                                           # restart the N-episode countdown
        return len(out) >= limit
    def _after_episode():                                      # one TV episode emitted → toward the next YT
        nonlocal ep_count
        ep_count += 1
        if ep_count >= every and yi < len(yt_videos):
            return _emit_yt_one()
        return False
    # Counter already saturated (after `current` completes) → the orchestrator's gate serves a
    # YouTube video BEFORE the TV rotation — lead with it to match.
    if ep_count >= every and yi < len(yt_videos):
        if _emit_yt_one(): return out
    for ei in range(len(eps) + 1):
        while mi < len(mvs) and movies._pos(mvs[mi]) == ei:    # movies due right before episode ei (a movie
            out.append(movie_item(mvs[mi])); mi += 1           # does NOT count toward the YouTube cadence)
            if len(out) >= limit: return out
        if ei < len(eps):
            out.append(ep_item(eps[ei]))
            if len(out) >= limit: return out
            if _after_episode(): return out
    while yi < len(yt_videos):                                 # TV exhausted → drain remaining YouTube
        if _emit_yt_one(): return out
    while mi < len(mvs):                                       # movies parked past the last episode
        out.append(movie_item(mvs[mi])); mi += 1
        if len(out) >= limit: return out
    return out


def current_state():
    orch = orchestrator.ORCH.snapshot()
    state = build_state(
        power=power.read_power(),
        scratch=collect_scratch(),
        adapter_watts=read_adapter_watts(),
        in_win=in_window(datetime.datetime.now().time(), WINDOW_START, WINDOW_END),
        manifest=load_manifest(),
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

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/state":
            self._json(current_state())
        elif path == "/api/series":
            self._json(api_series())
        elif path == "/api/movies":
            self._json(api_movies())
        elif path == "/api/channels":
            self._json(api_channels())
        elif path == "/api/settings":
            self._json({"settings": settings.get_settings(),
                        "defaults": settings.DEFAULT_SETTINGS})
        elif path == "/api/show-profile":
            show = (parse_qs(urlparse(self.path).query).get("show") or [None])[0]
            self._json(show_profile_info(show))
        elif path == "/api/log":
            self._json({"lines": logbook.tail(120, levels=None), "file": logbook.LOG_FILE,
                        "issues": logbook.tail(40)})
        elif path == "/api/selftest":
            self._json(selftest_full())
        elif path == "/api/screen.png":
            # SEE THE SCREEN WITH THE LID CLOSED. Only this process holds the Screen
            # Recording grant (a plain shell's screencapture returns "could not create
            # image from display"), so remote debugging of the DV automation has to come
            # from here. Localhost-only, like the rest of the API.
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
        elif path == "/api/request-accessibility":
            self._json(request_accessibility())
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
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(404, b"index.html missing", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
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
                    cheap = preflight.run_cheap()
                    bad = [c for c in cheap if not c["ok"]]
                    if bad:
                        self._json({"error": "preflight failed — refusing to arm",
                                    "checks": bad}, code=409)
                        return
                    settings.set_settings({"activated": True})
                    orchestrator.ORCH.enable()
                else:
                    settings.set_settings({"activated": False})
                    orchestrator.ORCH.disable()
            self._json(orchestrator.ORCH.snapshot())
        elif path == "/api/send-to-visionary":
            # The companion YouTube app's button (localhost only — the server binds
            # 127.0.0.1). youtarr grabs exactly this video; the orchestrator serves it
            # as the NEXT item once it lands on staging. Idempotent; the status string
            # is the button's feedback.
            import youtube
            self._json(youtube.send_priority((body.get("url") or body.get("id") or ""),
                                             title=body.get("title")))
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
                    if all(c["ok"] for c in preflight.run_cheap()):
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
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
