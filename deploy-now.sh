#!/bin/bash
# Deploy the built Visionary.app at the earliest near-ZERO-LOSS moment.
# RULE (user, 2026-07-03): NEVER relaunch mid-Topaz-segment. A graceful stop kills the
# in-progress segment, which then re-encodes from scratch on resume — repeated mid-segment
# deploys thrash an episode (it keeps re-doing the same segment). So:
#   BOTH topaz and the remux are now segmented + resumable (durable finisher work-list), so we deploy
#   at the FIRST segment boundary of EITHER — whichever completes a segment first (user, 2026-07-07):
#   the other stage, if mid-segment, simply resumes from its own last segment.
#   - topaz encoding  -> watch progress.seg_done ; a boundary there → deploy.
#   - finisher = remux -> watch finishing.seg_done ; a boundary there → deploy.
#   - stage = resolve -> wait it out (screen-capture render, NOT segmented/resumable).
#   - finisher = upload/cleanup -> wait (a mid-STOR upload leaves a partial on the NAS; both are quick).
#   - nothing encoding (download/idle, no remux) -> nothing precious, deploy immediately.
# The stop waits for BOTH encoders (topaz tvai + remux x265) to exit before relaunching, so a resumed
# stage can never collide with a still-dying old one on the same segdir.
ROOT="${VISIONARY_ROOT:-$(cd "$(dirname "$0")" && pwd)}"
APP="$ROOT/Visionary.app"
LOG="$ROOT/.deploy-dockbar.log"
BIN="Visionary.app/Contents/MacOS/Visionary"
API=http://127.0.0.1:8765
: > "$LOG"
say(){ echo "$(date '+%H:%M:%S') $*" >> "$LOG"; }
is_num(){ case "$1" in ''|*[!0-9]*) return 1;; *) return 0;; esac; }
# "stage|topaz_seg|running|finisher|remux_seg" in one line. `finisher` = the FINISHER thread's
# stage (remux/upload run OVERLAPPED with topaz — state.stage alone can NOT see them); both topaz
# and the remux now report seg_done, so we can deploy at whichever hits a segment boundary FIRST.
snap(){ curl -s --max-time 4 "$API/api/state" | python3 -c "import sys,json
try:
 d=json.load(sys.stdin);o=d.get('orchestrator') or {};p=o.get('progress') or {};fi=o.get('finishing') or {}
 print('%s|%s|%s|%s|%s'%(o.get('stage') or 'idle', p.get('seg_done'), o.get('running'), fi.get('stage') or 'none', fi.get('seg_done')))
except Exception:
 print('down|None|down|none|None')" 2>/dev/null; }
running_now(){ curl -s --max-time 4 "$API/api/state" | python3 -c "import sys,json
try: print((json.load(sys.stdin).get('orchestrator') or {}).get('running'))
except Exception: print('down')" 2>/dev/null; }

# Re-arm policy: only restore the ARM STATE THE USER HAD when we finally deploy. If the user
# hits Deactivate while this script waits (it can wait hours behind a remux), re-arming would
# fight them — live-hit 14:55: the user's stop + our re-arm restarted a run they had just killed.
armed_now(){ curl -s --max-time 4 "$API/api/state" | python3 -c "import sys,json
try: print((json.load(sys.stdin).get('settings') or {}).get('activated'))
except Exception: print('down')" 2>/dev/null; }

say "deploy requested — holding for the first segment boundary (topaz OR remux; the other resumes; never resolve/upload)"
deadline=$(( $(date +%s) + 10800 ))  # 3 h ceiling. Rarely bites now — only outlasts a resolve/upload
                                     # the deploy is briefly waiting out.
tseg0=""; rseg0=""
while [ "$(date +%s)" -lt "$deadline" ]; do
  s=$(snap); IFS='|' read -r stage tseg running fin rseg <<< "$s"
  # Non-resumable / partial-risk stages → wait them out; reset the segment baselines.
  case "$fin" in
    upload|cleanup) say "waiting: finisher=$fin (short, not cleanly resumable)"; tseg0=""; rseg0=""; sleep 5; continue ;;
  esac
  case "$stage" in
    resolve|upload) say "waiting: stage=$stage (never interrupt — not resumable)"; tseg0=""; rseg0=""; sleep 5; continue ;;
  esac
  # Run thread is topaz/download/idle; finisher is remux/none. Deploy at the FIRST segment boundary of
  # EITHER encoder that's actually running — the other stage, if mid-segment, resumes from its own last.
  tenc=$(pgrep -f tvai_up | wc -l | tr -d ' ')      # topaz (Video AI)
  xenc=$(pgrep -x x265 | wc -l | tr -d ' ')          # remux (x265)
  watching=0
  if [ "$stage" = "topaz" ] && [ "$tenc" != "0" ] && is_num "$tseg"; then
    watching=1
    if [ -z "$tseg0" ]; then tseg0="$tseg"; say "watching topaz at segment_done=$tseg"
    elif [ "$tseg" -gt "$tseg0" ]; then say "TOPAZ segment boundary ($tseg0 -> $tseg) — deploying (remux, if any, resumes)"; break; fi
  fi
  if [ "$fin" = "remux" ] && [ "$xenc" != "0" ] && is_num "$rseg"; then
    watching=1
    if [ -z "$rseg0" ]; then rseg0="$rseg"; say "watching remux at segment_done=$rseg"
    elif [ "$rseg" -gt "$rseg0" ]; then say "REMUX segment boundary ($rseg0 -> $rseg) — deploying (topaz, if any, resumes)"; break; fi
  fi
  if [ "$watching" = "0" ]; then say "stage=$stage finisher=$fin — nothing segmenting; safe to deploy"; break; fi
  sleep 5
done

# deadline fall-through guard (review-caught): if we got here by TIMEOUT while the pipeline is
# still in an un-interruptible stage, GIVE UP instead of deploying into it — the built app stays
# on disk and deploys on the next natural relaunch or re-run of this script.
s=$(snap); IFS='|' read -r stage tseg running fin rseg <<< "$s"
case "$fin" in upload|cleanup) say "GAVE UP: deadline hit but finisher=$fin — NOT deploying"; exit 2 ;; esac
case "$stage" in resolve|upload) say "GAVE UP: deadline hit but stage=$stage — NOT deploying"; exit 2 ;; esac

say "graceful stop + relaunch"
WAS_ARMED=$(armed_now)   # capture the USER's arm state at this exact moment, before OUR stop
curl -s --max-time 8 -X POST "$API/api/automation" -H 'Content-Type: application/json' -d '{"enabled":false}' >/dev/null
# Wait for the run thread to stop AND both encoders to exit — topaz (tvai_up) and the remux's x265.
# The remux abort is async (dvcap's pipe notices _finish_abort on its next check), so we must not
# relaunch until its x265 is gone, or the resumed remux would race a dying one on the same segdir.
for i in $(seq 1 30); do
  [ "$(running_now)" = "False" ] \
    && [ "$(pgrep -f tvai_up | wc -l | tr -d ' ')" = "0" ] \
    && [ "$(pgrep -x x265 | wc -l | tr -d ' ')" = "0" ] && break
  sleep 1
done
if [ "$WAS_ARMED" = "True" ]; then
  curl -s --max-time 8 -X POST "$API/api/settings" -H 'Content-Type: application/json' -d '{"activated":true}' >/dev/null
else
  say "user had the pipeline STOPPED — deploying without re-arming"
fi
pkill -f tvai_up 2>/dev/null; pkill -f "dashboard/server.py" 2>/dev/null; pkill -f "$BIN" 2>/dev/null
sleep 3; open "$APP"; sleep 8
vp=$(pgrep -f "$BIN" | wc -l | tr -d ' '); sp=$(pgrep -f "dashboard/server.py" | wc -l | tr -d ' ')
say "relaunched: Visionary=$vp server=$sp"
for i in $(seq 1 25); do [ "$(running_now)" = "True" ] && { say "re-armed"; break; }; sleep 3; done
say "DONE"
