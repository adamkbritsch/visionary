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
#      (visionary-worker-sweep.timer / .service) and enables it.
#   3. prints "vworker status" so you can see what the helper reports.
# Safe to run again: every step overwrites or is a no-op.
#
# x265 is built separately, on the helper itself, by build-x265.sh:
#   ssh -i <key> <user>@<host> 'bash -s' < engine/helper_worker/build-x265.sh
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

ssh_helper() {
  ssh -i "$key" -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=5 \
      -o ServerAliveCountMax=3 "$user@$host" "$@"
}

echo "install.sh: copying vworker to $user@$host" >&2
want=$(sha256_of "$src")
got=$(ssh_helper 'set -e; umask 077
  mkdir -p "$HOME/visionary-worker/bin" "$HOME/visionary-scratch/jobs"
  cat > "$HOME/visionary-worker/bin/.vworker.new"
  python3 -c "import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], \"rb\").read()).hexdigest())" \
    "$HOME/visionary-worker/bin/.vworker.new"' < "$src")
if [ "$got" != "$want" ]; then
  echo "install.sh: copy is corrupt (sha256 $got, want $want); nothing installed" >&2
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
systemctl --user daemon-reload
systemctl --user enable --now visionary-worker-sweep.timer
echo "installed: $bin/vworker"
"$bin/vworker" status || true
REMOTE
