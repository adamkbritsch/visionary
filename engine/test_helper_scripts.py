"""Tests for the helper's two shell scripts, engine/helper_worker/build-x265.sh and install.sh.
They run on the Mac with no helper machine, no network and no ssh: every outside command
(distrobox, sudo, dnf, git, cmake, nproc, c++, ssh, systemctl) is a fake script on PATH, and the
fake cmake turns x265Version.txt into a version string with the same rule as x265's own
source/cmake/Version.cmake, so a build "produces" a fake x265 that reports what the real one
would.

build-x265.sh's functions are tested on their own by sourcing a copy without its last line
(`main "$@"`); the whole script is tested the way the Mac runs it on the helper, piped into
`bash -s -- --version <token>`, where a child that reads stdin used to swallow the rest of the
script (the build finished, the install step never ran, and it exited 0)."""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER_DIR = os.path.join(HERE, "helper_worker")
BUILD = os.path.join(WORKER_DIR, "build-x265.sh")
INSTALL = os.path.join(WORKER_DIR, "install.sh")
VWORKER = os.path.join(WORKER_DIR, "vworker.py")
BASH = "/bin/bash" if os.path.exists("/bin/bash") else shutil.which("bash")
SYS_PATH = "/usr/bin:/bin"

FULL_1D117BE = "1d117bed4747758b51bd2c124d738527e30392cb"
FULL_43 = "e9b88125dc21393b3fd8d68e98083bdfb89778a8"


def render_version(txt):
    """PURE. What x265's Version.cmake makes of an x265Version.txt (the branch it takes when the
    source has no .git): distance "0" -> the bare tag, anything greater -> tag+distance-changeset
    with the changeset cut to 9 characters."""
    vals = {}
    for line in txt.splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            vals[k] = v
    tag = vals.get("releasetag", "0.0")
    dist = vals.get("releasetagdistance", "0")
    rev = vals.get("repositorychangeset", "")[:9]
    if dist == "0":
        return tag
    if dist > "0":                                  # cmake's STRGREATER: a string compare
        return "%s+%s-%s" % (tag, dist, rev)
    return "unknown"


# ---- fakes ----------------------------------------------------------------------------------

# Every fake that could read stdin does, the way `podman exec --interactive` (under distrobox
# enter) and a prompting git or sudo would: before the fix that swallowed the rest of a script
# piped into `bash -s`.
FAKE_DISTROBOX = r'''#!/bin/bash
echo "distrobox $*" >> "$FAKE_LOG"
case "$1" in
  list)
    echo "ID           | NAME                 | STATUS      | IMAGE"
    [ -f "$FAKE_STATE/box" ] && echo "0123456789ab | visionary-worker     | Up 2 hours  | img"
    exit 0 ;;
  create)
    cat > /dev/null
    touch "$FAKE_STATE/box"
    exit 0 ;;
  enter)
    cat > /dev/null
    while [ $# -gt 0 ] && [ "$1" != "--" ]; do shift; done
    shift
    exec "$@" ;;
esac
exit 1
'''

FAKE_SUDO = '#!/bin/bash\necho "sudo $*" >> "$FAKE_LOG"\nexec "$@"\n'

FAKE_PKG = '#!/bin/bash\necho "$(basename "$0") $*" >> "$FAKE_LOG"\ncat > /dev/null\nexit 0\n'

FAKE_GIT = r'''#!/bin/bash
echo "git $*" >> "$FAKE_LOG"
cat > /dev/null
if [ "$1" = clone ]; then
  mkdir -p "$3/.git" "$3/source"
  exit 0
fi
[ "$1" = -C ] && shift 2
[ "$1" = -c ] && shift 2
case "$1" in
  rev-parse)
    for a; do last=$a; done
    case "$*" in *--short*) echo "e9b8812"; exit 0 ;; esac
    ref=${last%"^{commit}"}
    ref=${ref#refs/tags/}
    case "$ref" in
      1d117be) echo "''' + FULL_1D117BE + r'''" ;;
      4.3) echo "''' + FULL_43 + r'''" ;;
      *) exit 1 ;;
    esac ;;
  checkout) exit 0 ;;
  *) exit 1 ;;
esac
'''

# configure (-S src -B dir ...) records what Version.cmake would print; --build makes
# libx265.a, and in the 8-bit tree (the one linking 10 + 12 bit) also the CLI
FAKE_CMAKE = r'''#!/bin/bash
echo "cmake $*" >> "$FAKE_LOG"
cat > /dev/null
if [ "$1" = --build ]; then
  b=$2
  touch "$b/libx265.a"
  if [ -f "$b/linked" ]; then
    v=${FAKE_X265_VERSION:-$(cat "$b/version")}
    printf '#!/bin/bash\necho "x265 [info]: HEVC encoder version %s" >&2\n' "$v" > "$b/x265"
    printf 'echo "x265 [info]: build info [Linux][GCC 14.2.1][64 bit] %s" >&2\n' \
        "${FAKE_X265_DEPTHS:-8bit+10bit+12bit}" >> "$b/x265"
    [ -z "${FAKE_X265_BROKEN:-}" ] || echo 'exit 127' >> "$b/x265"
    chmod 755 "$b/x265"
  fi
  exit 0
fi
s=$2 b=$4
mkdir -p "$b"
txt="$s/../x265Version.txt"
if [ -d "$s/../.git" ]; then
  v="from-git-describe"
else
  tag=$(sed -n 's/^releasetag: //p' "$txt")
  dist=$(sed -n 's/^releasetagdistance: //p' "$txt")
  cs=$(sed -n 's/^repositorychangeset: //p' "$txt" | cut -c1-9)
  if [ "$dist" = 0 ]; then v=$tag; else v="$tag+$dist-$cs"; fi
fi
echo "$v" > "$b/version"
case "$*" in *-DLINKED_12BIT=ON*) touch "$b/linked" ;; esac
exit 0
'''

FAKE_NPROC = "#!/bin/bash\necho 4\n"


def fake_cxx(static_lib):
    """A C++ compiler whose -print-file-name=libstdc++.a names `static_lib` (or only the bare
    name, which is how gcc and clang say they have none)."""
    return ('#!/bin/bash\ncase "$1" in -print-file-name=libstdc++.a) echo %s ;; esac\nexit 0\n'
            % (static_lib or "libstdc++.a"))


def write_exe(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, 0o755)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helper-scripts-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = os.path.join(self.tmp, "bin")
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.tmp, "state")
        for d in (self.bin, self.home, self.state):
            os.makedirs(d)
        self.log = os.path.join(self.tmp, "calls.log")
        open(self.log, "w").close()

    def fake(self, name, body):
        write_exe(os.path.join(self.bin, name), body)

    def env(self, path=None, **extra):
        env = {"PATH": path or (self.bin + ":" + SYS_PATH), "HOME": self.home,
               "FAKE_LOG": self.log, "FAKE_STATE": self.state, "LC_ALL": "C"}
        env.update(extra)
        return env

    def calls(self):
        with open(self.log) as f:
            return f.read()


# ---- build-x265.sh: its parts on their own -----------------------------------------------------

class BuildParts(Base):
    def setUp(self):
        super().setUp()
        with open(BUILD) as f:
            lines = f.read().rstrip("\n").split("\n")
        self.assertEqual(lines[-1], 'main "$@"')            # nothing runs before all is read
        self.lib = os.path.join(self.tmp, "build-x265-functions.sh")
        with open(self.lib, "w") as f:
            f.write("\n".join(lines[:-1]) + "\n")

    def sh(self, snippet, *args, path=None, **extra):
        r = subprocess.run([BASH, "-c", 'source "$0"; ' + snippet, self.lib] + list(args),
                           env=self.env(path=path, **extra), capture_output=True, text=True,
                           timeout=30)
        return r.returncode, r.stdout, r.stderr

    def parse(self, token):
        rc, out, _ = self.sh('parse_token "$1" && printf "%s|%s|%s" "$TAG" "$DISTANCE" '
                             '"$CHANGESET"', token)
        return out if rc == 0 else None

    def test_tokens_parse_into_tag_distance_and_changeset(self):
        self.assertEqual(self.parse("4.1+1-1d117be"), "4.1|1|1d117be")
        self.assertEqual(self.parse("4.3"), "4.3|0|")
        self.assertEqual(self.parse("3.6+12-abcdef123"), "3.6|12|abcdef123")
        self.assertEqual(self.parse("4"), "4|0|")

    def test_anything_x265_would_not_print_is_refused(self):
        for bad in ("", "4.3+", "4.3-1", "4.1+0-1d117be", "4.1+1-1D117BE", "4.1+1-1d117bed47",
                    "v4.3", "4.3 ", " 4.3", "4..3", "4.3.", "4.1+x-1d117be", "+1-1d117be",
                    "4.1+1-", "4.1+1-1d1-7be", "4.1+1-1d117be\n", "4.3;true", "4.3$(true)",
                    "4.1+01-1d117be", "3.5_RC1", "4.1+1-xyz1234"):
            self.assertIsNone(self.parse(bad), repr(bad))

    def test_version_file_makes_version_cmake_print_the_token_exactly(self):
        for token, changeset in (("4.1+1-1d117be", "1d117be"), ("4.3", "e9b8812"),
                                 ("3.6+12-abcdef123", "abcdef123")):
            rc, out, err = self.sh('parse_token "$1"; version_txt "$TAG" "$DISTANCE" "${2}"',
                                   token, changeset)
            self.assertEqual(rc, 0, err)
            self.assertEqual(render_version(out), token)
            self.assertTrue(out.startswith("#Attribute:         Values\n"))

    def test_the_old_pinned_file_is_reproduced_byte_for_byte(self):
        rc, out, _ = self.sh('version_txt 4.1 1 1d117be')
        self.assertEqual(out, "#Attribute:         Values\nrepositorychangeset: 1d117be\n"
                              "releasetagdistance: 1\nreleasetag: 4.1\n")

    def os_release(self, text):
        path = os.path.join(self.tmp, "os-release-%d" % abs(hash(text)))
        with open(path, "w") as f:
            f.write(text)
        return path

    def image(self, text):
        rc, out, _ = self.sh('box_image "$1"', self.os_release(text))
        return out if rc == 0 else None

    def test_box_image_follows_the_machines_own_distribution_and_release(self):
        cases = (
            ('NAME="Fedora Linux"\nID=fedora\nVERSION_ID=42\n',
             "registry.fedoraproject.org/fedora-toolbox:42"),
            ('NAME="Bazzite"\nID=bazzite\nID_LIKE="fedora"\nVERSION_ID=42\n',
             "registry.fedoraproject.org/fedora-toolbox:42"),
            ('ID=aurora\nID_LIKE="fedora"\nVERSION_ID="41"\n',
             "registry.fedoraproject.org/fedora-toolbox:41"),
            ('ID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\n',
             "quay.io/toolbx/ubuntu-toolbox:24.04"),
            ('ID=pop\nID_LIKE="ubuntu debian"\nVERSION_ID="22.04"\n',
             "quay.io/toolbx/ubuntu-toolbox:22.04"),
            ('ID=debian\nVERSION_ID="12"\n', "quay.io/toolbx-images/debian-toolbox:12"),
            ('ID=debian\nVERSION_ID="13.1"\n', "quay.io/toolbx-images/debian-toolbox:13"),
            ('ID=arch\nBUILD_ID=rolling\n', "quay.io/toolbx/arch-toolbox:latest"),
            ('ID=steamos\nID_LIKE=arch\nVERSION_ID=3.6\n', "quay.io/toolbx/arch-toolbox:latest"),
            ('ID=endeavouros\nID_LIKE=arch\n', "quay.io/toolbx/arch-toolbox:latest"),
        )
        for text, want in cases:
            self.assertEqual(self.image(text), want + "\n", text)

    def test_no_box_image_when_the_release_has_no_toolbox_to_match(self):
        for text in ('ID=linuxmint\nID_LIKE="ubuntu debian"\nVERSION_ID="22"\n',   # Mint's own
                     'ID="rocky"\nID_LIKE="rhel centos fedora"\nVERSION_ID="9.4"\n',
                     'ID="centos"\nID_LIKE="rhel fedora"\nVERSION_ID="9"\n',
                     'ID=debian\n',                                               # testing / sid
                     'ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n',
                     'ID=fedora\nVERSION_ID=rawhide\n',
                     ''):
            self.assertIsNone(self.image(text), text)
        rc, _, _ = self.sh('box_image "$1"', os.path.join(self.tmp, "no-such-file"))
        self.assertNotEqual(rc, 0)

    def test_install_hint_names_the_packages_for_the_family(self):
        def hint(text):
            return self.sh('install_hint "$1"', self.os_release(text))[1]
        self.assertEqual(hint("ID=fedora\nVERSION_ID=42\n"),
                         "sudo dnf install git cmake make gcc gcc-c++ nasm libstdc++-static\n")
        self.assertEqual(hint('ID=ubuntu\nVERSION_ID="24.04"\n'),
                         "sudo apt-get install git cmake make gcc g++ nasm ca-certificates\n")
        self.assertEqual(hint("ID=steamos\nID_LIKE=arch\n"),
                         "sudo pacman -S --needed git cmake make gcc nasm\n")
        self.assertEqual(hint("ID=opensuse-tumbleweed\n"), "")

    def missing(self, tools, arch="x86_64", static_lib=True):
        """native_missing with only `tools` on PATH (the fake bin dir and nothing else)."""
        self.fake("uname", '#!/bin/bash\necho %s\n' % arch)
        lib = os.path.join(self.tmp, "libstdc++.a")
        open(lib, "w").close()
        for t in tools:
            self.fake(t, fake_cxx(lib if static_lib else None) if t in ("c++", "g++")
                      else "#!/bin/bash\nexit 0\n")
        return self.sh("native_missing", path=self.bin)[1]

    def test_native_build_needs_nothing_more_when_the_tools_are_there(self):
        self.assertEqual(self.missing(["git", "cmake", "make", "cc", "c++", "nasm"]), "")

    def test_native_missing_names_each_missing_tool(self):
        self.assertEqual(self.missing([]),
                         "git, cmake, make, a C compiler, a C++ compiler, nasm")
        self.assertEqual(self.missing(["git", "make", "gcc", "g++"]), "cmake, nasm")

    def test_a_compiler_without_libstdcxx_a_is_not_enough(self):
        self.assertEqual(self.missing(["git", "cmake", "make", "cc", "c++", "nasm"],
                                      static_lib=False), "a static libstdc++")

    def test_nasm_is_only_needed_on_x86(self):
        self.assertEqual(self.missing(["git", "cmake", "make", "cc", "c++"], arch="aarch64"), "")

    def test_x265_token_reads_the_encoder_version_line(self):
        rc, out, _ = self.sh('x265_token "$1"',
                             "x265 [info]: HEVC encoder version 4.1+1-1d117be\r\n"
                             "x265 [info]: build info [Linux][GCC 14][64 bit] 8bit+10bit+12bit\n"
                             "x265 [info]: HEVC encoder version 9.9\n")
        self.assertEqual(out, "4.1+1-1d117be\n")
        self.assertEqual(self.sh('x265_token "$1"', "garbage\n")[1], "")


# ---- build-x265.sh: the whole script, the way the Mac runs it ----------------------------------

class BuildRun(Base):
    def setUp(self):
        super().setUp()
        for name, body in (("distrobox", FAKE_DISTROBOX), ("sudo", FAKE_SUDO), ("dnf", FAKE_PKG),
                           ("git", FAKE_GIT), ("cmake", FAKE_CMAKE), ("nproc", FAKE_NPROC),
                           ("cc", "#!/bin/bash\nexit 0\n"), ("nasm", "#!/bin/bash\nexit 0\n")):
            self.fake(name, body)
        self.dest = os.path.join(self.home, "visionary-worker", "bin", "x265")
        with open(BUILD, "rb") as f:
            self.script = f.read()

    def native_tools(self, ok=True):
        lib = os.path.join(self.tmp, "libstdc++.a")
        open(lib, "w").close()
        self.fake("c++", fake_cxx(lib if ok else None))

    def piped(self, *args, path=None, **extra):
        """build-x265.sh piped into `bash -s -- <args>`, as `ssh ... 'bash -s -- ...' < script`."""
        r = subprocess.run([BASH, "-s", "--"] + list(args), input=self.script,
                           env=self.env(path=path, **extra), capture_output=True, timeout=60)
        return r.returncode, r.stdout.decode(), r.stderr.decode()

    def installed_version(self):
        r = subprocess.run([self.dest, "--version"], capture_output=True, text=True, timeout=10)
        return r.stderr

    def test_piped_into_bash_s_the_box_build_installs_the_binary(self):
        self.native_tools(ok=False)                        # a machine without libstdc++.a
        rc, _, err = self.piped("--version", "4.1+1-1d117be",
                                X265_BOX_IMAGE="registry.fedoraproject.org/fedora-toolbox:42")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.access(self.dest, os.X_OK), err)
        self.assertFalse(os.path.exists(self.dest + ".new"))
        self.assertIn("HEVC encoder version 4.1+1-1d117be", self.installed_version())
        self.assertIn("installed " + self.dest, err)
        calls = self.calls()
        self.assertIn("distrobox create --yes --name visionary-worker --image "
                      "registry.fedoraproject.org/fedora-toolbox:42", calls)
        self.assertIn("distrobox enter --name visionary-worker --no-tty -- bash ", calls)
        self.assertIn("dnf -y install git cmake make gcc gcc-c++ nasm libstdc++-static", calls)
        self.assertIn("rev-parse --verify --quiet 1d117be^{commit}", calls)
        self.assertIn("checkout --detach " + FULL_1D117BE, calls)
        txt = os.path.join(self.home, "visionary-worker", "build", "x265", "x265Version.txt")
        with open(txt) as f:
            self.assertEqual(render_version(f.read()), "4.1+1-1d117be")
        self.assertIn("-DLINKED_10BIT=ON -DLINKED_12BIT=ON", calls)
        self.assertIn("-DCMAKE_EXE_LINKER_FLAGS=-static-libstdc++ -static-libgcc", calls)
        self.assertIn("-DMAIN12=ON", calls)

    def test_piped_into_bash_s_the_native_build_installs_a_tag_only_version(self):
        self.native_tools(ok=True)
        rc, out, err = self.piped("--version", "4.3")
        self.assertEqual(rc, 0, err)
        self.assertIn("HEVC encoder version 4.3\n", self.installed_version())
        self.assertIn("HEVC encoder version 4.3", out)       # the version line, on stdout
        calls = self.calls()
        self.assertNotIn("distrobox", calls)
        self.assertNotIn("dnf", calls)                       # the machine's own tools, untouched
        self.assertIn("rev-parse --verify --quiet refs/tags/4.3^{commit}", calls)
        self.assertIn("checkout --detach " + FULL_43, calls)
        txt = os.path.join(self.home, "visionary-worker", "build", "x265", "x265Version.txt")
        with open(txt) as f:
            text = f.read()
        self.assertIn("releasetagdistance: 0\n", text)
        self.assertIn("repositorychangeset: e9b8812\n", text)
        self.assertEqual(render_version(text), "4.3")

    def test_an_installed_matching_build_is_kept_unless_forced(self):
        self.native_tools(ok=True)
        self.assertEqual(self.piped("--version", "4.3")[0], 0)
        open(self.log, "w").close()
        rc, _, err = self.piped("--version", "4.3")
        self.assertEqual(rc, 0, err)
        self.assertIn("already installed", err)
        self.assertNotIn("git clone", self.calls())
        rc, _, err = self.piped("--version", "4.3", "--force")
        self.assertEqual(rc, 0, err)
        self.assertIn("git clone", self.calls())

    def test_a_different_installed_version_is_rebuilt(self):
        self.native_tools(ok=True)
        self.assertEqual(self.piped("--version", "4.3")[0], 0)
        open(self.log, "w").close()
        rc, _, err = self.piped("--version", "4.1+1-1d117be")
        self.assertEqual(rc, 0, err)
        self.assertIn("git clone", self.calls())
        self.assertIn("HEVC encoder version 4.1+1-1d117be", self.installed_version())

    def test_a_build_reporting_another_version_is_refused_and_the_old_one_kept(self):
        self.native_tools(ok=True)
        self.assertEqual(self.piped("--version", "4.3")[0], 0)
        rc, _, err = self.piped("--version", "4.1+1-1d117be", FAKE_X265_VERSION="4.1")
        self.assertEqual(rc, 1)
        self.assertIn("wrong version (want 4.1+1-1d117be, got 4.1)", err)
        self.assertFalse(os.path.exists(self.dest + ".new"))
        self.assertIn("HEVC encoder version 4.3\n", self.installed_version())

    def test_a_build_without_all_three_depths_is_refused(self):
        self.native_tools(ok=True)
        rc, _, err = self.piped("--version", "4.3", FAKE_X265_DEPTHS="8bit")
        self.assertEqual(rc, 1)
        self.assertIn("not an 8/10/12-bit multilib build", err)
        self.assertFalse(os.path.exists(self.dest))

    def test_a_box_build_that_does_not_run_on_the_machine_says_how_to_recover(self):
        self.native_tools(ok=False)
        rc, _, err = self.piped("--version", "4.3", FAKE_X265_BROKEN="1",
                                X265_BOX_IMAGE="quay.io/toolbx/arch-toolbox:latest")
        self.assertEqual(rc, 1)
        self.assertIn("the new x265 does not run on this machine", err)
        self.assertIn("distrobox rm --force visionary-worker", err)
        self.assertFalse(os.path.exists(self.dest))
        self.assertFalse(os.path.exists(self.dest + ".new"))

    @unittest.skipIf(os.path.exists("/etc/os-release"), "this machine has an os-release")
    def test_missing_tools_and_no_image_to_pick_says_so(self):
        self.native_tools(ok=False)
        rc, _, err = self.piped("--version", "4.3")
        self.assertEqual(rc, 1)
        self.assertIn("this machine lacks a static libstdc++, and there is no toolbox image", err)
        self.assertIn("set X265_BOX_IMAGE", err)
        self.assertNotIn("distrobox create", self.calls())

    def test_without_tools_or_distrobox_it_names_what_to_install(self):
        empty = os.path.join(self.tmp, "empty-bin")
        os.makedirs(empty)
        write_exe(os.path.join(empty, "uname"), "#!/bin/bash\necho x86_64\n")
        rc, _, err = self.piped("--version", "4.3", path=empty)
        self.assertEqual(rc, 1)
        self.assertIn("cannot build x265 on this machine: it lacks git, cmake, make, a C "
                      "compiler, a C++ compiler, nasm, and has no distrobox", err)
        self.assertIn("install distrobox and podman", err)
        self.assertNotIn("Bazzite", err)

    def test_version_is_required_and_checked(self):
        rc, _, err = self.piped()
        self.assertEqual(rc, 2)
        self.assertIn("usage: bash build-x265.sh --version <x265 version> [--force]", err)
        rc, _, err = self.piped("--force")
        self.assertEqual(rc, 2)
        rc, _, err = self.piped("--version", "4.3;reboot")
        self.assertEqual(rc, 1)
        self.assertIn("not an x265 version", err)
        self.assertEqual(self.calls(), "")                   # nothing ran

    def test_no_maintainer_specifics_left(self):
        for path in (BUILD, INSTALL):
            with open(path) as f:
                text = f.read()
            for word in ("Bazzite ships", "ROG", "Ally", "bazzite.local"):
                self.assertNotIn(word, text, path)


# ---- install.sh -----------------------------------------------------------------------------

# fake ssh: record the call, drop the options, run the command locally in FAKE_HOME. It prints
# rc-file noise first, as a helper whose ~/.bashrc echoes for non-interactive shells does (a
# 64-hex string among it, so only the right hash may pass); FAKE_SSH_CORRUPT drops the last
# byte of what it is sent.
FAKE_SSH = r'''#!/bin/bash
echo "ssh $*" >> "$FAKE_LOG"
while [ $# -gt 0 ]; do
  case "$1" in
    -i|-o) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
shift
cd "$FAKE_HOME"
printf 'Welcome to the helper\r\nnoise %s\n' "$(printf 'ab%.0s' $(seq 32))"
printf 'no newline after this: '
if [ -n "${FAKE_SSH_CORRUPT:-}" ]; then
  python3 -c 'import sys; d = sys.stdin.buffer.read(); sys.stdout.buffer.write(d[:-1])' |
    HOME="$FAKE_HOME" bash -c "$*"
  exit
fi
HOME="$FAKE_HOME" exec bash -c "$*"
'''


class Install(Base):
    def setUp(self):
        super().setUp()
        self.fake("ssh", FAKE_SSH)
        os.symlink(sys.executable, os.path.join(self.bin, "python3"))
        self.key = os.path.join(self.tmp, "id_helper")
        with open(self.key, "w") as f:
            f.write("not a real key\n")
        self.helper_home = os.path.join(self.tmp, "helper-home")
        os.makedirs(self.helper_home)
        self.installed = os.path.join(self.helper_home, "visionary-worker", "bin", "vworker")
        self.units = os.path.join(self.helper_home, ".config", "systemd", "user")

    def install(self, **extra):
        r = subprocess.run([BASH, INSTALL, "--host", "helper.local", "--user", "worker",
                            "--key", self.key],
                           env=self.env(FAKE_HOME=self.helper_home, VWORKER_STATUS_SAMPLE="0",
                                        **extra),
                           capture_output=True, timeout=60)
        return r.returncode, r.stdout.decode(), r.stderr.decode()

    def assert_installed(self):
        with open(self.installed, "rb") as a, open(VWORKER, "rb") as b:
            self.assertEqual(a.read(), b.read())
        self.assertTrue(os.access(self.installed, os.X_OK))
        self.assertFalse(os.path.exists(self.installed.replace("vworker", ".vworker.new")))

    def test_rc_file_noise_around_the_hash_does_not_fail_the_check(self):
        self.fake("systemctl", '#!/bin/bash\necho "systemctl $*" >> "$FAKE_LOG"\n')
        rc, out, err = self.install()
        self.assertEqual(rc, 0, err)
        self.assert_installed()
        self.assertIn('"version":1', out)                    # it printed the helper's status
        self.assertNotIn("warning", err)
        calls = self.calls()
        self.assertIn("ssh -i %s -o IdentitiesOnly=yes -o BatchMode=yes" % self.key, calls)
        self.assertIn("systemctl --user enable --now visionary-worker-sweep.timer", calls)
        self.assertTrue(os.path.exists(os.path.join(self.units, "visionary-worker-sweep.timer")))

    def test_a_corrupt_copy_is_still_refused(self):
        self.fake("systemctl", '#!/bin/bash\necho "systemctl $*" >> "$FAKE_LOG"\n')
        rc, _, err = self.install(FAKE_SSH_CORRUPT="1")
        self.assertEqual(rc, 1)
        with open(VWORKER, "rb") as f:
            bad = hashlib.sha256(f.read()[:-1]).hexdigest()
        self.assertIn("copy is corrupt (sha256 %s, want " % bad, err)
        self.assertFalse(os.path.exists(self.installed))
        self.assertFalse(os.path.exists(self.installed.replace("vworker", ".vworker.new")))
        self.assertNotIn("systemctl", self.calls())

    def test_a_helper_without_systemctl_installs_without_the_timer(self):
        rc, out, err = self.install()                        # the Mac has no systemctl
        self.assertEqual(rc, 0, err)
        self.assert_installed()
        self.assertIn("the 5-minute cleanup timer was not installed; vworker still deletes its "
                      "job files itself", err)
        self.assertFalse(os.path.exists(self.units))
        self.assertIn("installed: ", out)
        self.assertIn('"version":1', out)

    def test_a_helper_without_a_user_manager_installs_without_the_timer(self):
        self.fake("systemctl", '#!/bin/bash\necho "systemctl $*" >> "$FAKE_LOG"\n'
                               'echo "Failed to connect to bus: No medium found" >&2\nexit 1\n')
        rc, out, err = self.install()
        self.assertEqual(rc, 0, err)
        self.assert_installed()
        self.assertIn("the 5-minute cleanup timer was not installed", err)
        self.assertFalse(os.path.exists(self.units))
        self.assertNotIn("enable --now", self.calls())
        self.assertIn('"version":1', out)

    def test_a_failing_enable_is_a_warning_too(self):
        self.fake("systemctl", '#!/bin/bash\necho "systemctl $*" >> "$FAKE_LOG"\n'
                               'case "$*" in *enable*) exit 1 ;; esac\nexit 0\n')
        rc, out, err = self.install()
        self.assertEqual(rc, 0, err)
        self.assert_installed()
        self.assertIn("the 5-minute cleanup timer was not installed", err)
        self.assertIn('"version":1', out)


if __name__ == "__main__":
    unittest.main()
