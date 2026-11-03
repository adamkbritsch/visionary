#!/usr/bin/env bash
# install.sh -- install Visionary's helper worker on the helper (any Linux machine on
# Ethernet). Run this ON THE MAC.
#
#   engine/helper_worker/install.sh --host <host> --user <user> --key <ssh private key>
#   engine/helper_worker/install.sh <host> <user> <key>                 (same, positional)
#
# What it does, over ssh only (no scp, no rsync):
#   1. cat vworker.py -> ~/visionary-worker/bin/vworker on the helper, checked by sha256,
#      chmod 755; creates ~/visionary-scratch/jobs (mode 700).
#   2. installs a systemd --user timer that runs "vworker sweep" every 5 minutes
#      (visionary-worker-sweep.timer / .service) and enables it. The timer is only a backstop
#      (every vworker call sweeps first), so a helper without a systemd user manager goes
#      without it: a warning, and the install still succeeds.
#   3. prints "vworker status" so you can see what the helper reports.
# Safe to run again: every step overwrites or is a no-op.
#
# x265 is built separately, on the helper itself, by build-x265.sh, at the version this Mac's
# `x265 --version` prints after "HEVC encoder version" (Setup's Build button does this):
#   ssh -T -i <key> <user>@<host> 'bash -s -- --version <token>' < engine/helper_worker/build-x265.sh
#
# This script runs on macOS's bash 3.2, so it avoids bash 4 features.
set -euo pipefail

usage() {
  echo "usage: $0 --host HOST --user USER --key KEY    (or: $0 HOST USER KEY)" >&2
  exit 2
}

host="" user="" key=""
if [ $# -eq 3 ] && [ "${1#-}" = "$1" ]; then
  host=$1 user=$2 key=$3
else
  while [ $# -gt 0 ]; do
    case "$1" in
      --host) [ $# -ge 2 ] || usage; host=$2; shift 2 ;;
      --user) [ $# -ge 2 ] || usage; user=$2; shift 2 ;;
      --key)  [ $# -ge 2 ] || usage; key=$2;  shift 2 ;;
      *) usage ;;
    esac
  done
fi
[ -n "$host" ] && [ -n "$user" ] && [ -n "$key" ] || usage
case "$host" in -*|*[!A-Za-z0-9._:-]*) echo "install.sh: bad host: $host" >&2; exit 2 ;; esac
case "$user" in -*|*[!A-Za-z0-9._-]*) echo "install.sh: bad user: $user" >&2; exit 2 ;; esac
[ -r "$key" ] || { echo "install.sh: ssh key not readable: $key" >&2; exit 1; }

here=$(cd "$(dirname "$0")" && pwd)
src="$here/vworker.py"
[ -f "$src" ] || { echo "install.sh: $src not found" >&2; exit 1; }

# never ship a file that does not even parse
python3 - "$src" <<'PY'
import ast, sys
ast.parse(open(sys.argv[1], encoding="utf-8").read(), sys.argv[1])
PY

sha256_of() {
  python3 -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$1"
}

# only the given key is offered (IdentitiesOnly), as the app's own ssh does: an agent holding
# many keys could otherwise use up the helper's MaxAuthTries before this one is tried. Nothing
# from the user's ssh config can multiplex, jump or allocate a tty either: with a tty forced
# (RequestTTY force) the remote end never sees the end of what is sent, so the copy's `cat` and
# the install's `bash -s` would wait for good.
ssh_helper() {
  ssh -i "$key" -o IdentitiesOnly=yes -o ControlMaster=no -o ControlPath=none \
      -o ProxyJump=none -o RequestTTY=no -o BatchMode=yes -o ConnectTimeout=8 \
      -o ServerAliveInterval=5 -o ServerAliveCountMax=3 "$user@$host" "$@"
}

echo "install.sh: copying vworker to $user@$host" >&2
want=$(sha256_of "$src")
# the helper's shell may print rc-file noise around the hash, so look for the hash in what
# comes back instead of comparing all of it
out=$(ssh_helper 'set -e; umask 077
  mkdir -p "$HOME/visionary-worker/bin" "$HOME/visionary-scratch/jobs"
  cat > "$HOME/visionary-worker/bin/.vworker.new"
  python3 -c "import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], \"rb\").read()).hexdigest())" \
    "$HOME/visionary-worker/bin/.vworker.new"' < "$src" | tr -d '\r')
case "$out" in
  *"$want"*) ok=1 ;;
  *) ok=0 ;;
esac
if [ "$ok" != 1 ]; then
  got=$(printf '%s\n' "$out" | grep -Eo '[0-9a-f]{64}' | tail -n 1 || true)
  echo "install.sh: copy is corrupt (sha256 ${got:-missing}, want $want); nothing installed" >&2
  ssh_helper 'rm -f "$HOME/visionary-worker/bin/.vworker.new"' </dev/null || true
  exit 1
fi

echo "install.sh: installing vworker and the sweep timer" >&2
ssh_helper 'bash -s' <<'REMOTE'
set -euo pipefail
bin="$HOME/visionary-worker/bin"
chmod 755 "$bin/.vworker.new"
mv -f "$bin/.vworker.new" "$bin/vworker"
chmod 700 "$HOME/visionary-scratch" "$HOME/visionary-scratch/jobs"
timer=0
if command -v systemctl >/dev/null 2>&1 &&
   systemctl --user show-environment >/dev/null 2>&1 </dev/null; then
  units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$units"
  cat > "$units/visionary-worker-sweep.service" <<'EOF'
[Unit]
Description=Visionary helper worker: delete abandoned job scratch

[Service]
Type=oneshot
ExecStart=%h/visionary-worker/bin/vworker sweep
Nice=10
EOF
  cat > "$units/visionary-worker-sweep.timer" <<'EOF'
[Unit]
Description=Visionary helper worker: sweep abandoned job scratch every 5 minutes

[Timer]
OnStartupSec=1min
OnUnitActiveSec=5min
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF
  if systemctl --user daemon-reload </dev/null &&
     systemctl --user enable --now visionary-worker-sweep.timer </dev/null; then
    timer=1
  fi
fi
[ "$timer" = 1 ] || echo "install.sh: warning: the 5-minute cleanup timer was not installed;" \
  "vworker still deletes its job files itself (this helper has no working systemd user manager)" >&2
echo "installed: $bin/vworker"
"$bin/vworker" status </dev/null || true
REMOTE
