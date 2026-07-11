#!/bin/bash
# Deploy the freshly-built Visionary.app (Dock progress bar) WITHOUT wasting pipeline work
# or corrupting anything. Waits until the current episode's Topaz encode is done, then at the
# next cheap-to-interrupt stage does a GRACEFUL stop (POST enabled:false -> orchestrator
# disable() aborts the stage + topaz.terminate_all(); no orphaned children, no truncated
# upload), re-arms the appliance, and relaunches.
#   safe stages : download | cleanup | idle   (short, local-only, clean-abortable)
#   avoided     : topaz (would re-encode a segment) | upload (direct-STOR truncation) |
#                 resolve (TCC/DoVi UI automation — never interrupt) |
#                 remux (x265 peak-cap pass is NOT resumable — killing it loses ~75 min)
ROOT="${VISIONARY_ROOT:-$(cd "$(dirname "$0")" && pwd)}"
APP="$ROOT/Visionary.app"
LOG="$ROOT/.deploy-dockbar.log"
BIN="Visionary.app/Contents/MacOS/Visionary"
API=http://127.0.0.1:8765
: > "$LOG"
say(){ echo "$(date '+%H:%M:%S') $*" >> "$LOG"; }
stage_now(){ curl -s --max-time 4 "$API/api/state" | python3 -c "import sys,json
try:
 d=json.load(sys.stdin);o=d.get('orchestrator') or {}
 f=(o.get('finishing') or {}).get('stage')
 # while the FINISHER drains a previous item (overlapped), report ITS stage — a relaunch
 # would kill its un-resumable x265/upload no matter what the run thread's stage says
 print(f or o.get('stage') or 'idle')
except Exception:
 print('down')" 2>/dev/null; }
running_now(){ curl -s --max-time 4 "$API/api/state" | python3 -c "import sys,json
try:
 d=json.load(sys.stdin);print((d.get('orchestrator') or {}).get('running'))
except Exception:
 print('down')" 2>/dev/null; }

say "monitor started; will deploy at next safe gap (download/cleanup/idle, encoder idle; never remux)"
deadline=$(( $(date +%s) + 21600 ))   # 6 h
while [ "$(date +%s)" -lt "$deadline" ]; do
  enc=$(pgrep -f tvai_up | wc -l | tr -d ' ')
  stage=$(stage_now); [ -z "$stage" ] && stage=down
  say "enc=$enc stage=$stage"
  case "$stage" in
    download|cleanup|idle)
      if [ "$enc" = "0" ]; then
        say "SAFE (stage=$stage) -> graceful stop + redeploy"
        curl -s --max-time 8 -X POST "$API/api/automation" -H 'Content-Type: application/json' -d '{"enabled":false}' >/dev/null
        for i in $(seq 1 10); do r=$(running_now); say "  post-disable running=$r"; [ "$r" = "False" ] && break; sleep 1; done
        curl -s --max-time 8 -X POST "$API/api/settings"   -H 'Content-Type: application/json' -d '{"activated":true}' >/dev/null  # re-arm on relaunch
        pkill -f tvai_up 2>/dev/null            # belt: nothing should be encoding, but never leave one behind
        pkill -f "dashboard/server.py" 2>/dev/null
        pkill -f "$BIN" 2>/dev/null
        sleep 3
        open "$APP"
        sleep 8
        vp=$(pgrep -f "$BIN" | wc -l | tr -d ' '); sp=$(pgrep -f "dashboard/server.py" | wc -l | tr -d ' ')
        say "relaunched: Visionary procs=$vp server procs=$sp"
        # confirm the appliance re-armed itself
        for i in $(seq 1 20); do r=$(running_now); [ "$r" = "True" ] && { say "re-armed (running=True)"; break; }; sleep 3; done
        say "DONE"
        exit 0
      fi ;;
  esac
  sleep 4
done
say "GAVE UP after 6 h (pipeline stayed busy) — binary is BUILT; will activate on next natural relaunch"
exit 2
