#!/usr/bin/env bash
# build-x265.sh -- reproducible x265 for Visionary's helper worker. Run this ON THE HELPER'S HOST
# (the Linux machine itself, not inside a box):
#     bash build-x265.sh [--force]
# or from the Mac:
#     ssh -i <key> <user>@<host> 'bash -s' < engine/helper_worker/build-x265.sh
#     ssh -i <key> <user>@<host> 'bash -s -- --force' < engine/helper_worker/build-x265.sh
#
# Builds x265 at commit 1d117be -- the commit the Mac's Homebrew x265 4.1 reports -- as an
# 8/10/12-bit multilib CLI (the layout of Homebrew's formula and of x265's own
# build/linux/multilib.sh: 12-bit and 10-bit static libs linked into the 8-bit CLI) inside a
# Fedora distrobox named "visionary-worker", and installs it to ~/visionary-worker/bin/x265.
#
# Version string: x265's source/cmake/Version.cmake prefers `git describe` when a .git is
# present and otherwise reads x265Version.txt at the repo root. The build removes .git and
# writes that file (releasetag 4.1, releasetagdistance 1, repositorychangeset 1d117be), so the
# binary reports "4.1+1-1d117be", exactly what the Mac's `x265 --version` prints.
#
# Runs on the host: the CLI links libstdc++/libgcc statically and leaves libnuma out
# (ENABLE_LIBNUMA=OFF; NUMA only places threads, it never changes the bitstream), and the box
# uses the host's own Fedora release, so the glibc it links against is the host's.
# The result is verified ON THE HOST before it replaces the installed binary.
#
# Env overrides: X265_GIT_URL (source repo), X265_BOX_IMAGE (box image).
set -euo pipefail

COMMIT=1d117be
WANT_VERSION="4.1+1-1d117be"
BOX=visionary-worker
REPO_URL=${X265_GIT_URL:-https://bitbucket.org/multicoreware/x265_git.git}
ROOT="$HOME/visionary-worker"
WORK="$ROOT/build"
DEST="$ROOT/bin/x265"

say() { printf 'build-x265: %s\n' "$*" >&2; }
die() { say "ERROR: $*"; exit 1; }

force=0
[ "${1:-}" = "--force" ] && force=1

if [ -f /run/.containerenv ] || [ -f /.dockerenv ]; then
  die "run this on the helper's host, not inside a container (it creates its own box)"
fi
if [ "$force" = 0 ] && [ -x "$DEST" ] && "$DEST" --version 2>&1 | grep -qF "$WANT_VERSION"; then
  say "already installed: $("$DEST" --version 2>&1 | grep -F "$WANT_VERSION" | head -n 1)"
  say "(pass --force to rebuild)"
  exit 0
fi

command -v distrobox >/dev/null 2>&1 || die "distrobox not found (Bazzite ships it)"

if [ -n "${X265_BOX_IMAGE:-}" ]; then
  IMAGE=$X265_BOX_IMAGE
else
  fedora=$(. /etc/os-release 2>/dev/null; printf '%s' "${VERSION_ID:-}")
  case "$fedora" in
    ''|*[!0-9]*) die "cannot read the host's Fedora release (VERSION_ID='$fedora'); set X265_BOX_IMAGE" ;;
  esac
  IMAGE="registry.fedoraproject.org/fedora-toolbox:$fedora"
fi

box_exists() {
  if command -v podman >/dev/null 2>&1; then
    podman container exists "$BOX"
  else
    distrobox list --no-color 2>/dev/null |
      awk -F'|' -v b="$BOX" 'NR > 1 { gsub(/ /, "", $2); if ($2 == b) f = 1 } END { exit !f }'
  fi
}

mkdir -p "$WORK" "$ROOT/bin"
if ! box_exists; then
  say "creating distrobox $BOX from $IMAGE"
  distrobox create --yes --name "$BOX" --image "$IMAGE"
fi

inner="$WORK/build-inside.sh"
cat > "$inner" <<'INNER'
#!/usr/bin/env bash
# written by build-x265.sh; runs inside the visionary-worker box
set -euo pipefail
commit=$1 url=$2 work=$3 out=$4
log="$work/build.log"
: > "$log"
say() { printf 'build-x265 (box): %s\n' "$*" >&2; }
run() { "$@" >>"$log" 2>&1 || { say "FAILED: $*"; tail -n 40 "$log" >&2; exit 1; }; }

say "installing build tools"
run sudo dnf -y install git cmake make gcc-c++ nasm libstdc++-static

src="$work/x265"
rm -rf "$src" "$work/8bit" "$work/10bit" "$work/12bit"
say "cloning $url"
run git clone "$url" "$src"
run git -C "$src" -c advice.detachedHead=false checkout "$commit"
full=$(git -C "$src" rev-parse HEAD)
case "$full" in
  "$commit"*) say "source at $full" ;;
  *) say "checked out $full, not $commit"; exit 1 ;;
esac

# Version.cmake would `git describe`; without .git it reads x265Version.txt instead
rm -rf "$src/.git"
cat > "$src/x265Version.txt" <<'EOF'
#Attribute:         Values
repositorychangeset: 1d117be
releasetagdistance: 1
releasetag: 4.1
EOF

jobs=$(nproc)
common=(-DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DENABLE_LIBNUMA=OFF)
high=(-DHIGH_BIT_DEPTH=ON -DEXPORT_C_API=OFF -DENABLE_SHARED=OFF -DENABLE_CLI=OFF)

say "building the 12-bit library"
run cmake -S "$src/source" -B "$work/12bit" "${common[@]}" "${high[@]}" -DMAIN12=ON
run cmake --build "$work/12bit" -j "$jobs"
say "building the 10-bit library"
run cmake -S "$src/source" -B "$work/10bit" "${common[@]}" "${high[@]}"
run cmake --build "$work/10bit" -j "$jobs"

mkdir -p "$work/8bit"
cp "$work/10bit/libx265.a" "$work/8bit/libx265_main10.a"
cp "$work/12bit/libx265.a" "$work/8bit/libx265_main12.a"
say "building the 8-bit library and the CLI (linking 10 + 12 bit)"
run cmake -S "$src/source" -B "$work/8bit" "${common[@]}" \
    -DEXTRA_LIB="x265_main10.a;x265_main12.a" -DEXTRA_LINK_FLAGS=-L. \
    -DLINKED_10BIT=ON -DLINKED_12BIT=ON -DENABLE_SHARED=OFF \
    -DCMAKE_EXE_LINKER_FLAGS="-static-libstdc++ -static-libgcc"
run cmake --build "$work/8bit" -j "$jobs"

install -m 755 "$work/8bit/x265" "$out"
say "built $out"
INNER
chmod 755 "$inner"

say "building inside $BOX (the box's first start installs its base packages; be patient)"
rm -f "$DEST.new"
distrobox enter --name "$BOX" --no-tty -- bash "$inner" "$COMMIT" "$REPO_URL" "$WORK" "$DEST.new"

# verify on the HOST, outside the box
[ -x "$DEST.new" ] || die "the build did not produce $DEST.new"
ver=$("$DEST.new" --version 2>&1) || die "the new x265 does not run on the host: $ver"
printf '%s\n' "$ver" | grep -qF "$WANT_VERSION" ||
  { rm -f "$DEST.new"; die "wrong version (want $WANT_VERSION): $ver"; }
printf '%s\n' "$ver" | grep -qF "8bit+10bit+12bit" ||
  { rm -f "$DEST.new"; die "not an 8/10/12-bit multilib build: $ver"; }
mv -f "$DEST.new" "$DEST"
"$DEST" --version 2>&1 | grep -F "$WANT_VERSION"
say "installed $DEST"
