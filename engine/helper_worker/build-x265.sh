#!/usr/bin/env bash
# build-x265.sh -- reproducible x265 for Visionary's helper worker. Run this ON THE HELPER
# (the Linux machine itself, not inside a container):
#     bash build-x265.sh --version <token> [--force]
# or from the Mac (Setup's Build button runs this one, with this Mac's own token):
#     ssh -i <key> <user>@<host> 'bash -s -- --version <token>' < engine/helper_worker/build-x265.sh
#
# <token> is what the Mac's `x265 --version` prints after "HEVC encoder version": "<tag>"
# (e.g. 4.3) or "<tag>+<distance>-<changeset>" (e.g. 4.1+1-1d117be). The helper must run the
# same x265 as the Mac, so this builds exactly that source -- the changeset, or the tag when
# the token has no changeset -- as an 8/10/12-bit multilib CLI (the layout of Homebrew's
# formula and of x265's own build/linux/multilib.sh: 12-bit and 10-bit static libs linked into
# the 8-bit CLI), and installs it to ~/visionary-worker/bin/x265. When the binary there
# already reports <token> and 8bit+10bit+12bit it stops without rebuilding, unless --force.
#
# Version string: x265's source/cmake/Version.cmake prefers `git describe` when a .git is
# present and otherwise reads x265Version.txt at the repo root. A releasetagdistance of 0
# renders as the bare tag ("4.3"); anything else as tag+distance-changeset, the changeset cut
# to 9 characters ("4.1+1-1d117be"). The build removes .git and writes that file from
# <token>, so the binary reports <token> byte for byte.
#
# Where it builds:
#   - on the machine itself when it has git, cmake, make, a C and a C++ compiler, nasm (on
#     x86) and a static libstdc++;
#   - otherwise inside a distrobox named "visionary-worker", made from the toolbox image of
#     the machine's own distribution and release (Fedora, Ubuntu, Debian, Arch and the
#     distributions built on them), so the glibc it links against is the machine's; the box
#     installs the tools with its own package manager;
#   - with neither, it stops and names what to install.
# Either way the CLI links libstdc++/libgcc statically and leaves libnuma out
# (ENABLE_LIBNUMA=OFF; NUMA only places threads, it never changes the bitstream), and the
# result is verified ON THE MACHINE, outside any box, before it replaces the installed binary.
#
# Everything runs from one function, main, called on the last line: when the script arrives on
# stdin (`bash -s`) bash has then read every line before anything runs, so a child that reads
# stdin cannot swallow the rest of the script. Every such child gets </dev/null as well.
#
# Env overrides: X265_GIT_URL (source repo), X265_BOX_IMAGE (build in a box from this image,
# even when the machine has the tools).
set -euo pipefail

BOX=visionary-worker
# the GitHub repo carries every release tag (4.3 is not on the Bitbucket one) and 1d117be
REPO_URL=${X265_GIT_URL:-https://github.com/Multicorewareinc/x265.git}
ROOT="$HOME/visionary-worker"
WORK="$ROOT/build"
DEST="$ROOT/bin/x265"

say() { printf 'build-x265: %s\n' "$*" >&2; }
die() { say "ERROR: $*"; exit 1; }

usage() {
  say "usage: bash build-x265.sh --version <x265 version> [--force]"
  say "  <x265 version> is what the Mac's \`x265 --version\` prints after \"HEVC encoder version\","
  say "  for example 4.3 or 4.1+1-1d117be"
  exit 2
}

# parse_token TOKEN: sets TAG, DISTANCE and CHANGESET, or fails for anything x265 would not
# print. "4.3" -> 4.3 0 ""; "4.1+1-1d117be" -> 4.1 1 1d117be.
parse_token() {
  local t=$1 rest
  local re_tag='^[0-9]+([.][0-9]+)*$' re_dist='^[1-9][0-9]{0,5}$' re_cs='^[0-9a-f]{4,9}$'
  TAG=$t DISTANCE=0 CHANGESET=""
  case "$t" in
    *+*-*)
      TAG=${t%%+*} rest=${t#*+}
      DISTANCE=${rest%%-*} CHANGESET=${rest#*-}
      [[ $DISTANCE =~ $re_dist ]] || return 1
      [[ $CHANGESET =~ $re_cs ]] || return 1 ;;
    *+*|*-*) return 1 ;;
  esac
  [[ $TAG =~ $re_tag ]]
}

# version_txt TAG DISTANCE CHANGESET: the x265Version.txt that makes Version.cmake print the
# token (with a distance of 0 the changeset is only a record; the version is the bare tag)
version_txt() {
  printf '#Attribute:         Values\n'
  printf 'repositorychangeset: %s\n' "$3"
  printf 'releasetagdistance: %s\n' "$2"
  printf 'releasetag: %s\n' "$1"
}

# packages PM: the build tools under that package manager's names (Debian and Ubuntu's g++
# brings libstdc++.a along, and so does Arch's gcc)
packages() {
  case "$1" in
    dnf) echo "git cmake make gcc gcc-c++ nasm libstdc++-static" ;;
    apt-get) echo "git cmake make gcc g++ nasm ca-certificates" ;;
    pacman) echo "git cmake make gcc nasm" ;;
  esac
}

# os_release FILE: "ID|ID_LIKE|VERSION_ID" from an os-release file
os_release() {
  local f=$1
  case "$f" in /*) ;; *) f=./$f ;; esac
  [ -r "$f" ] || return 1
  ( set +eu; . "$f" >/dev/null 2>&1; printf '%s|%s|%s' "${ID:-}" "${ID_LIKE:-}" "${VERSION_ID:-}" )
}

# os_family FILE: fedora, ubuntu, debian or arch -- the first of ID, then ID_LIKE, that is one
os_family() {
  local rel id like word
  rel=$(os_release "$1") || return 1
  id=${rel%%|*} like=${rel#*|}
  like=${like%%|*}
  for word in $id $like; do
    case "$word" in
      fedora|fedora-atomic|bazzite) echo fedora; return 0 ;;
      ubuntu) echo ubuntu; return 0 ;;
      debian) echo debian; return 0 ;;
      arch|steamos) echo arch; return 0 ;;
    esac
  done
  return 1
}

# box_image FILE: the toolbox image of this machine's own distribution and release (so the
# box's glibc is the machine's), or fails when there is none to pick
box_image() {
  local family rel ver
  local re_num='^[0-9]+$' re_ubuntu='^[0-9][0-9][.][0-9][0-9]$'
  family=$(os_family "$1") || return 1
  rel=$(os_release "$1") || return 1
  ver=${rel##*|}
  case "$family" in
    fedora)    # RHEL and its rebuilds name fedora in ID_LIKE but number their releases 8, 9, 10
      [[ $ver =~ $re_num ]] && [ "$ver" -ge 30 ] || return 1
      echo "registry.fedoraproject.org/fedora-toolbox:$ver" ;;
    ubuntu)
      [[ $ver =~ $re_ubuntu ]] || return 1
      echo "quay.io/toolbx/ubuntu-toolbox:$ver" ;;
    debian)
      ver=${ver%%.*}
      [[ $ver =~ $re_num ]] || return 1
      echo "quay.io/toolbx-images/debian-toolbox:$ver" ;;
    arch)      # rolling; a machine far behind it fails the check on the machine at the end
      echo "quay.io/toolbx/arch-toolbox:latest" ;;
    *) return 1 ;;
  esac
}

# install_hint FILE: the command that installs the build tools on this machine ("" if unknown)
install_hint() {
  case "$(os_family "$1" 2>/dev/null || true)" in
    fedora) echo "sudo dnf install $(packages dnf)" ;;
    ubuntu|debian) echo "sudo apt-get install $(packages apt-get)" ;;
    arch) echo "sudo pacman -S --needed $(packages pacman)" ;;
  esac
}

# native_missing: what this machine lacks to build without a box, comma-separated ("" if
# nothing)
native_missing() {
  local miss="" t cxx lib
  for t in git cmake make; do
    command -v "$t" >/dev/null 2>&1 || miss="$miss, $t"
  done
  command -v cc >/dev/null 2>&1 || command -v gcc >/dev/null 2>&1 || miss="$miss, a C compiler"
  cxx=$(command -v c++ 2>/dev/null || command -v g++ 2>/dev/null || true)
  if [ -z "$cxx" ]; then
    miss="$miss, a C++ compiler"
  else
    # -static-libstdc++ needs libstdc++.a; the compiler names its full path when it has one
    lib=$("$cxx" -print-file-name=libstdc++.a 2>/dev/null </dev/null || true)
    case "$lib" in
      /*) [ -f "$lib" ] || miss="$miss, a static libstdc++" ;;
      *) miss="$miss, a static libstdc++" ;;
    esac
  fi
  case "$(uname -m)" in
    x86_64|amd64|i?86) command -v nasm >/dev/null 2>&1 || miss="$miss, nasm" ;;
  esac
  printf '%s' "${miss#, }"
}

# x265_token TEXT: the version `x265 --version` names after "HEVC encoder version" ("" if none)
x265_token() {
  printf '%s\n' "$1" | tr -d '\r' |
    awk '!done && /HEVC encoder version/ {
           for (i = 1; i < NF; i++) if ($i == "version") { print $(i + 1); done = 1; break } }'
}

# built_ok BINARY TOKEN: it runs, reports TOKEN and is the 8/10/12-bit multilib build
built_ok() {
  local ver
  ver=$("$1" --version 2>&1 </dev/null) || return 1
  [ "$(x265_token "$ver")" = "$2" ] || return 1
  case "$ver" in *8bit+10bit+12bit*) return 0 ;; esac
  return 1
}

box_exists() {
  if command -v podman >/dev/null 2>&1; then
    podman container exists "$BOX" </dev/null
  else
    distrobox list --no-color 2>/dev/null </dev/null |
      awk -F'|' -v b="$BOX" 'NR > 1 { gsub(/ /, "", $2); if ($2 == b) f = 1 } END { exit !f }'
  fi
}

# write_inner PATH: the script that does the build, on the machine or inside the box (the box
# shares $HOME). It carries its own copies of packages and version_txt.
write_inner() {
  {
    printf '#!/usr/bin/env bash\n'
    printf '# written by build-x265.sh; runs on the helper itself or inside the %s box\n' "$BOX"
    printf 'set -euo pipefail\n'
    declare -f packages version_txt
    cat <<'INNER'
mode=$1 url=$2 work=$3 out=$4 tag=$5 distance=$6 changeset=$7
log="$work/build.log"
: > "$log"
say() { printf 'build-x265 (%s): %s\n' "$mode" "$*" >&2; }
run() { "$@" </dev/null >>"$log" 2>&1 || { say "FAILED: $*"; tail -n 40 "$log" >&2; exit 1; }; }

if [ "$mode" = box ]; then
  say "installing build tools"
  if command -v dnf >/dev/null 2>&1; then
    run sudo dnf -y install $(packages dnf)
  elif command -v apt-get >/dev/null 2>&1; then
    run sudo env DEBIAN_FRONTEND=noninteractive apt-get update
    run sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        $(packages apt-get)
  elif command -v pacman >/dev/null 2>&1; then
    run sudo pacman -Syu --noconfirm --needed $(packages pacman)
  else
    say "the box has no dnf, apt-get or pacman to install the build tools with"
    exit 1
  fi
fi

src="$work/x265"
rm -rf "$src" "$work/8bit" "$work/10bit" "$work/12bit"
say "cloning $url"
run git clone "$url" "$src"
# the token's changeset, or the tag itself when it names none
if [ -n "$changeset" ]; then ref="$changeset^{commit}"; else ref="refs/tags/$tag^{commit}"; fi
full=$(git -C "$src" rev-parse --verify --quiet "$ref" </dev/null) ||
  { say "$url has no ${changeset:-tag $tag}"; exit 1; }
run git -C "$src" -c advice.detachedHead=false checkout --detach "$full"
case "$full" in
  "$changeset"*) say "source at $full" ;;
  *) say "checked out $full, not $changeset"; exit 1 ;;
esac
[ -n "$changeset" ] || changeset=$(git -C "$src" rev-parse --short HEAD </dev/null)

# Version.cmake would `git describe`; without .git it reads x265Version.txt instead
rm -rf "$src/.git"
version_txt "$tag" "$distance" "$changeset" > "$src/x265Version.txt"

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
  } > "$1"
  chmod 755 "$1"
}

main() {
  local token="" force=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --version) [ $# -ge 2 ] || usage; token=$2; shift 2 ;;
      --force) force=1; shift ;;
      *) usage ;;
    esac
  done
  [ -n "$token" ] || usage
  parse_token "$token" || die "not an x265 version: '$token' (want one like 4.3 or 4.1+1-1d117be)"
  local tag=$TAG distance=$DISTANCE changeset=$CHANGESET

  if [ -f /run/.containerenv ] || [ -f /.dockerenv ]; then
    die "run this on the helper itself, not inside a container (it makes its own box when it" \
        "needs one)"
  fi
  if [ "$force" = 0 ] && [ -x "$DEST" ] && built_ok "$DEST" "$token"; then
    say "already installed: $DEST reports x265 $token (8bit+10bit+12bit)"
    say "(pass --force to rebuild)"
    return 0
  fi

  local mode=native image="" missing hint
  missing=$(native_missing)
  if [ -n "${X265_BOX_IMAGE:-}" ]; then
    command -v distrobox >/dev/null 2>&1 ||
      die "X265_BOX_IMAGE is set but distrobox is not installed (install distrobox and podman)"
    mode=box image=$X265_BOX_IMAGE
  elif [ -n "$missing" ]; then
    hint=$(install_hint /etc/os-release)
    if ! command -v distrobox >/dev/null 2>&1; then
      die "cannot build x265 on this machine: it lacks $missing, and has no distrobox to" \
          "build in a box. Install them${hint:+ ($hint)}, or install distrobox and podman," \
          "then run this again."
    fi
    mode=box
    image=$(box_image /etc/os-release) ||
      die "this machine lacks $missing, and there is no toolbox image to pick for its" \
          "distribution and release. Install them${hint:+ ($hint)}, or set X265_BOX_IMAGE to" \
          "a toolbox image of this machine's own distribution and release."
  fi

  mkdir -p "$WORK" "$ROOT/bin"
  if [ "$mode" = box ]; then
    if ! box_exists; then
      say "creating distrobox $BOX from $image"
      distrobox create --yes --name "$BOX" --image "$image" </dev/null ||
        die "could not create the box from $image (see above); set X265_BOX_IMAGE to a toolbox" \
            "image of this machine's own distribution and release"
    elif [ -n "${X265_BOX_IMAGE:-}" ]; then
      say "using the existing box $BOX (X265_BOX_IMAGE only applies when the box is made;" \
          "distrobox rm --force $BOX makes the next run remake it)"
    fi
  fi

  local inner="$WORK/build-inside.sh"
  write_inner "$inner"
  rm -f "$DEST.new"
  if [ "$mode" = box ]; then
    say "building x265 $token inside $BOX (the box's first start installs its base packages;" \
        "be patient)"
    distrobox enter --name "$BOX" --no-tty -- \
        bash "$inner" box "$REPO_URL" "$WORK" "$DEST.new" "$tag" "$distance" "$changeset" \
        </dev/null || die "the build failed (see above; the full log is $WORK/build.log)"
  else
    say "building x265 $token on this machine"
    bash "$inner" native "$REPO_URL" "$WORK" "$DEST.new" "$tag" "$distance" "$changeset" \
        </dev/null || die "the build failed (see above; the full log is $WORK/build.log)"
  fi

  # verify on the machine itself, outside the box
  local ver got
  [ -x "$DEST.new" ] || die "the build did not produce $DEST.new"
  if ! ver=$("$DEST.new" --version 2>&1 </dev/null); then
    rm -f "$DEST.new"
    [ "$mode" = box ] || die "the new x265 does not run on this machine: $ver"
    die "the new x265 does not run on this machine: $ver -- if the box's release is newer" \
        "than this machine's, remove the box (distrobox rm --force $BOX), set X265_BOX_IMAGE" \
        "to a toolbox image of this machine's own release and run this again"
  fi
  got=$(x265_token "$ver")
  [ "$got" = "$token" ] ||
    { rm -f "$DEST.new"; die "wrong version (want $token, got ${got:-none}): $ver"; }
  case "$ver" in
    *8bit+10bit+12bit*) ;;
    *) rm -f "$DEST.new"; die "not an 8/10/12-bit multilib build: $ver" ;;
  esac
  mv -f "$DEST.new" "$DEST"
  printf '%s\n' "$ver" | grep -F "HEVC encoder version" || true
  say "installed $DEST"
}

main "$@"
