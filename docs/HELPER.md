<sub>[← Visionary README](../README.md) · [Setup](SETUP.md) · [How it works](HOW-IT-WORKS.md) · [YouTube](YOUTUBE.md) · [Configuration](CONFIGURATION.md) · [Known limitations](LIMITATIONS.md)</sub>

# The Helper

Optional, and off by default. A Linux machine wired to your network (a desktop, or a laptop or
handheld on its charger) encodes some of the remux's segments while the Mac encodes the rest.
Without a helper, or with it switched off, the remux runs exactly as it always has.

## What it does

The remux re-encodes each item's picture with x265 under the peak-bitrate cap, in segments of
about 5 minutes. It is the pipeline's slowest step: about 3 fps on the Mac. With a helper, the
Mac takes segments from the **front** of the item and the helper takes them from the **back**,
so the remux bar fills from both ends and they meet somewhere in the middle.

- **The Mac still prepares every segment.** It decodes the picture and applies the colour step
  itself, because the helper's colour conversion is not bit-identical to the Mac's. It then
  streams the result, compressed, to the helper over ssh. The helper runs only x265, the **same
  x265 version as the Mac's with the same settings**, and streams the encoded picture straight
  back. Its segments are the same kind of segment the Mac makes.
- **Every segment it sends back is checked** for the right number of frames before it is used,
  and the finished master goes through every check a Mac-only master does.
- **Nothing is kept on the helper.** The encoded picture is never stored there. The only file a
  job owns is a small slice of Dolby Vision metadata, and the job's folder is deleted whenever
  it stops.
- **It can never fail an item.** A segment the helper drops goes back to the Mac.
- **One helper, one segment at a time.** When two remux lanes run at once, they share it.
- **Only the capped x265 re-encode is shared.** That covers upscaled episodes and movies, HDR
  and SDR conversions, and everything else that falls back to the full re-encode. Topaz, the
  Dolby Vision analysis and render, the over-gate splice's pieces and the peak repair's
  re-encodes always run on the Mac, and items that ship without a re-encode don't need it.

### Is it worth it?

Measured on the same frames: a handheld PC with an AMD Z1 Extreme encoded **8.06 fps**, while
the Mac (an M3 Max, also running Topaz at the time) managed **2.99 fps**. Ethernet carried
about **777 Mbps** between them; Wi-Fi gave 55 to 140 Mbps, which is too slow, so Wi-Fi is
refused at both ends.

It pays off best when the helper's x265 is at least about as fast as the Mac's. Keep in mind:

- The Mac still decodes, colour-converts and compresses each of the helper's segments. Only the
  x265 encode, the slow part, moves.
- At the end of an item the Mac waits for the helper's last segment rather than redoing it,
  so a much slower machine can leave the Mac idle there.
- The picture stream runs at several hundred Mbps, so use gigabit Ethernet or faster.

## What you need

**On the helper:**

| Need | What works |
|---|---|
| System | Linux on **x86-64** or **aarch64** (64-bit ARM) |
| Software | **Python 3.8** or newer, an **OpenSSH server** that is running, and **zstd** |
| Network | **Ethernet** to the same network as the Mac's wired port |
| Power | **Mains**: a laptop or handheld on its charger. A desktop with no battery counts as mains. |
| Disk | At least **5 GB free** in your home folder, plus room for the x265 build |
| Memory | About **3.5 GB free** to start a segment, or **6.5 GB** beside a game left resting: a resting game keeps its memory, and the helper leaves it 3 GB to spare. |
| To build x265 | **Either** the build tools (git, cmake, make, a C++ compiler, nasm and static libstdc++) **or** distrobox with podman, and an internet connection |
| A login | A user account on it whose password you know, for one command during setup |

**On the Mac:** Visionary with its command-line tools installed (Setup's *Command-line tools*
group), and Ethernet to the same network. The helper's x265 is built to match the Mac's own,
and everything the helper needs ships inside the app.

Before you start, on the helper:

- Plug in Ethernet (and the charger, on a laptop or handheld).
- Turn on its ssh server. On most distributions that is `sudo systemctl enable --now sshd`.
  On Ubuntu and Debian, install it first with `sudo apt install openssh-server`; the service is
  called `ssh` there.
- Check that `python3 --version` and `zstd --version` both answer. If not, install them with
  the helper's package manager.
- Turn off its automatic sleep while it's plugged in. Setup's *Sleep* check later tells you
  whether it can stay awake by itself (see [Keeping it awake](#keeping-it-awake)).
- Note its address: a name such as `helper.local`, or its IPv4 address such as
  `192.168.1.50` (`ip -4 addr` on the helper shows it). The Mac connects over IPv4 only.

## Setting it up

Everything happens in the app, under **gear → Settings → Setup → Helper**. Each step has a
check beside it, and **Test** runs the checks again.

1. **Address, Login and SSH key.** Enter the helper's address (`helper.local` or
   `192.168.1.50`) and your login on it. Leave the key at its default,
   `~/.ssh/id_ed25519_visionary_helper`, unless you already made one for this. Then press
   **Save**: the check rows, **Test**, **Create key** and the **Helper** switch in Settings
   appear once an address and a login are saved. A saved value that can't be used shows its
   reason in the *Signs in* row, for example *settings: bad host*; fix it and press **Save**
   again.
2. **Create key**, on the *Signs in* row, if there is no key yet. It makes a new key with no
   passphrase, because Visionary logs in unattended. It never overwrites a file that already
   exists.
3. **Authorise the key, once.** Setup shows a command such as

   ```bash
   ssh-copy-id -i ~/.ssh/id_ed25519_visionary_helper.pub you@helper.local
   ```

   Run it in Terminal. It asks you to accept the helper's host key (type `yes`) and then for
   your password on the helper. This is the only time a password is used: from then on the
   Mac logs in with the key. Press **Test** and the *Signs in* check turns green.
4. **Install** puts the worker on the helper (`~/visionary-worker/bin/vworker`, checked by
   sha256 after the copy) and makes its job folder (`~/visionary-scratch/jobs`). Where the
   helper runs systemd for your user, it also adds a timer that clears abandoned job files
   every 5 minutes; the worker does the same each time it runs, so the timer is a backstop.
5. **Build** makes x265 on the helper from exactly the source of this Mac's x265 version, and
   makes it report exactly that version. It uses the helper's own build tools when they are
   installed. Otherwise it builds inside a distrobox box chosen for the helper's distribution:
   Fedora or Bazzite, Ubuntu, Debian, or Arch or SteamOS. On any other distribution, install
   the build tools first (see [Building x265 by hand](#building-x265-by-hand)). The first build
   takes the longest, since it fetches the source and a new box installs its packages; the
   output shows live in Setup. The result is checked on the helper itself before it replaces
   anything, and lands in `~/visionary-worker/bin/x265`.
6. **Switch it on:** gear → Settings → **Helper**. The switch appears once an address and a
   login are saved. Within about a minute the header shows **Helper ready**.

### What the checks mean

| Check | Green | Otherwise |
|---|---|---|
| **Signs in** | The Mac logs in with the key, unattended (*logged in as you*). | *this Mac hasn't met the helper yet* or *the key isn't authorised*: run the command Setup shows. *no answer from helper.local*: is it on, is the address right, is its ssh server running? *no SSH key yet*: press **Create key**. *settings: bad host* (or user, or key): the saved value can't be used; fix it and press **Save**. |
| **Ethernet** | The Mac reaches it over Ethernet, and names the port. | *the Mac reaches it over Wi-Fi*: plug both into Ethernet (see [Troubleshooting](#troubleshooting)). |
| **Worker** | *installed*, and the same worker as this app's. | *not installed*, or *an older worker* after you update Visionary: press **Install**. |
| **x265** | The same version as this Mac's. | *not built yet*, or two different versions named (after Homebrew upgrades the Mac's x265): press **Build**. *install the command-line tools first*: this Mac has no x265 yet. |
| **Sleep** | *it stays awake while it encodes* | A gray dot: never a failure, and it never holds back *Ready*. *it can't keep itself awake over ssh · turn off automatic sleep while it's plugged in*: see [Keeping it awake](#keeping-it-awake). No words when it can't be told (no systemd-inhibit on the helper, no up-to-date worker yet, or no answer) or when a rest manager runs there, which looks after sleep itself. |
| **Status** | *ready · waiting for the next segment* | The same words as the status line below, for example *paused · a game is running on it*. Switched off, it reads *off · switched off* with a gray dot. |

## What the app shows

- **In the header**, beside the power pill: **Helper ready** (steel), **Helper working**
  (bright) or **Helper paused** (a dim, disabled-looking gray: it answers but won't take work
  right now). Nothing at all when it is switched off, off for a reason below, or not connected.
  Hover for the reason and the machine's name.
- **On the remux card**: the bar fills from both ends, the Mac's segments from the left and the
  helper's from the right, each machine's segment in progress drawn dim in its slot. Under the
  bar, while the helper is switched on, one line says what it is doing (hover it for the reason
  and the address it uses).
- **In the log**: a finished remux's note says how many segments the helper made, for example
  *(12 segments on the helper)*.

| Status line | What it means |
|---|---|
| Helper connected · on seg 9 · 7.9 fps · did 10–11 | It is encoding segment 9 of this item at 7.9 fps and has finished segments 10 and 11. The header reads *Helper working*. |
| Helper ready · waiting for the next segment | Free and allowed: it takes the next segment from the back. |
| Helper paused · a game is running on it | It stepped aside for a game: one running, just started, waking up, resting for less than a minute, or (where a rest manager runs) frozen only behind its quick menu or home. |
| Helper paused · it's busy | Something heavy is running on it, or it already has a job. |
| Helper paused · it's low on memory | Less than about 3.5 GB of memory free, or less than 6.5 GB beside a resting game (a big game at rest can hold 9 GB). Also when a game rests and the helper can't fence off its own memory. |
| Helper paused · it's low on disk space | Less than 5 GB free. |
| Helper off · the Mac is on Wi-Fi | The Mac's route to it is Wi-Fi, not Ethernet. |
| Helper off · it's not on Ethernet | The helper has no Ethernet link up, or its Wi-Fi is on the same network and the stream arrives over it (the tooltip then says *the stream is arriving over its Wi-Fi · turn its Wi-Fi off*). |
| Helper off · it's on battery | A laptop or handheld off its charger. |
| Helper off · switched off | The Settings switch is off. |
| Helper off · not set up | No address or login saved in Setup. |
| Helper off · settings: bad host | A saved address (or login, or key path) that can't be used. Fix it in Setup, where the *Signs in* row says the same. |
| Helper not connected | Anything else: no answer, a name that doesn't resolve, a missing or different x265, a missing worker, no zstd on the helper. The tooltip and Setup's checks say which. |

## When it steps aside, and what it deletes

The helper polices itself, whatever the Mac thinks.

- **Before each segment** it refuses at once during a game, on battery, without Ethernet, with
  less than 5 GB of disk or too little memory free (see below), or while it already has a job.
  A heavy reading alone is sampled again for a few seconds before it refuses, so one busy
  second doesn't count.
- **While encoding** it checks every 2 seconds, and stops **at once** when a game starts, when
  the charger is unplugged, when Ethernet goes down, or when the Mac goes away (the connection
  breaks, or no data arrives for 5 minutes). It also stops when free memory stays too low for
  two checks in a row.
- **A game that starts or wakes up is caught within a quarter of a second.** Four times a
  second the helper looks at the programs started since its last look (only those), at a
  resting game, and at a rest manager's wake marker, and steps aside the moment a game starts,
  a resting game wakes up, or the rest manager begins waking one.
- **It stops after about 10 seconds of anything heavy:** other programs using more than 1.5
  cores of CPU, or more than 30 % of the GPU. A browser, a file manager or Steam's menus stay
  below that.
- **The GPU is read program by program** on NVIDIA (`nvidia-smi pmon`) and on AMD and Intel
  (i915, xe) from the kernel's per-program counters. There the desktop's own drawing and the
  Steam client are not counted: GNOME, KDE, Cinnamon, COSMIC, Pantheon, Budgie, Xfce, MATE and
  the wlroots compositors such as Sway. Where there is no per-program reading (an older
  kernel, or `nvidia-smi pmon` failing), the whole GPU counts, desktop included, so a busy
  desktop alone can pause it. The open-source **nouveau** driver gives no GPU reading at all,
  so on it only the CPU rule and the game rule apply.
- **A game** is any Steam game (Proton included), any Windows program under Wine or Proton,
  and a launch from Lutris or Heroic. A non-Steam shortcut running a native Linux program is
  judged by the CPU and GPU rule only.
- **A resting game doesn't count, once it has rested for a minute.** A game frozen by a rest
  mode (with the cgroup freezer) or stopped with SIGSTOP isn't running, so the helper can work
  while it rests. It counts as resting only after a minute of rest in a row, so a game paused
  for a moment in its menu still counts as running. The whole game has to rest, not one of its
  processes: a running game with one stopped helper process is a running game, and anything
  that can't be told counts as running. A game quit or killed while it rests is no wake-up.
- **Memory beside a resting game.** A resting game keeps all of its memory, so beside one the
  helper keeps at least 3 GB free the whole time: it starts a segment only with 6.5 GB free,
  runs it in a systemd scope of its own capped at what is left above 3.5 GB, and steps aside
  when free memory stays below 3 GB for two checks. Where it can't start such a scope over
  ssh, it doesn't work beside a resting game (*Helper paused · it's low on memory*). With no
  game resting it needs 3.5 GB to start and steps aside below 0.75 GB. If memory ever ran out,
  the helper's own processes are the first the kernel would stop, never the game.
- **A rest manager.** On a machine whose rest manager leaves a status file (in your runtime
  folder), only a **real rest** counts: the console itself at rest and the game held at
  *rest*. A game frozen only behind its quick menu or its home is the console in use, so the
  helper doesn't start there, and a segment already running stops within a quarter of a second
  when the game leaves the rest that way. It also refuses, or steps aside, while the file's
  wake marker (when the manager began waking a game) is fresh. It only ever reads that file,
  and on such a machine it takes no keep-awake hold at all (see
  [Keeping it awake](#keeping-it-awake)).
- **Its x265 runs at the lowest CPU priority** (SCHED_IDLE, or nice 19 where that is not
  available) and the idle I/O priority (`ionice -c 3`, where that works). Where the helper's
  systemd can start a scope of its own (a transient user scope), each segment also runs in one
  at **idle CPU weight** (`CPUWeight=idle`): Linux shares the CPU between groups of programs
  before it looks at their priorities, and this makes the whole group take only CPU nobody
  else wants, so the desktop, a game and Steam's own updates and shader pre-caching always
  come first.
- **On every stop** it kills its zstd and x265 and deletes that job's folder. The Mac deletes
  the partial segment it received and encodes that segment itself.
- After the helper steps aside for a game, a heavy program or low memory, Visionary **leaves it
  alone for 5 minutes** before asking again. After two lost connections or errors in one item,
  it sits out the rest of that item.
- When the AI border extension takes the Mac's whole machine, the Mac's own remux is frozen
  and loses nothing, but the helper's segment in flight is stopped and redone later, by either
  machine.
- Switching the helper off stops new segments; one already in flight finishes.

Visionary logs in with the configured key only, never a password. For its work it connects to
the IPv4 address its Ethernet check found, bound to the Mac's Ethernet port, so a pulled cable
ends the session instead of moving it to Wi-Fi. Apart from Setup's Install and Build, it runs
nothing on the helper but its worker.

### Keeping it awake

While it encodes, the worker asks the system to hold off sleep (`systemd-inhibit`). It never
does beside a resting game, and takes no keep-awake hold at all on a machine with a rest
manager, which decides about sleep itself. Over ssh most desktops won't allow it anyway, and
Setup's *Sleep* row says so. Then turn off automatic sleep while it's plugged in:

- **GNOME**: Settings → Power.
- **KDE**: System Settings → Power Management.
- Or add a polkit rule that allows `org.freedesktop.login1.inhibit-block-sleep` for your user.
  That needs root, so it is up to you; Visionary never changes it.

A helper that sleeps mid-segment just hands that segment back to the Mac.

## Troubleshooting

Hover the status line or the header readout for the reason, or open **Setup → Helper** and
press **Test**.

- **"this Mac hasn't met the helper yet"** (ssh's *Host key verification failed*): run the
  `ssh-copy-id` command Setup shows and answer `yes`. The host key is remembered under the
  address exactly as you typed it, so if you change the address (from `helper.local` to
  `192.168.1.50`, say), run the command again.
- **"the key isn't authorised"** (ssh's *Permission denied*): run the command Setup shows, and
  check that the login is your user name on the helper.
- **"no answer from helper.local"** or **"cannot resolve helper.local to an IPv4 address"**:
  check that the helper is on and its ssh server is running. If the name doesn't resolve, enter
  its IPv4 address instead.
- **Helper off · the Mac is on Wi-Fi**: plug the Mac into Ethernet. With both connected, put
  Ethernet above Wi-Fi (System Settings → Network → the **…** menu → *Set Service Order*), or
  turn Wi-Fi off.
- **Helper off · it's not on Ethernet**: plug the helper into Ethernet. If the tooltip says
  *the stream is arriving over its Wi-Fi · turn its Wi-Fi off*, its Wi-Fi is on the same
  network as its Ethernet and the stream comes in that way: turn its Wi-Fi off.
- **Two different x265 versions** in the x265 check: this happens after Homebrew upgrades the
  Mac's x265 (a `brew upgrade`), because the helper must match it exactly. Visionary notices
  the new x265 without a restart, and the helper sits out until you press **Build**, which
  makes the matching version.
- **"an older worker"**: press **Install** after updating Visionary.
- **Helper not connected, with *zstd missing* in the tooltip**: install zstd on the helper.
- **Helper paused · it's busy, all the time**: something on the helper keeps using more than
  1.5 cores or 30 % of the GPU. `vworker status` (below) names it in its `why` field, for
  example *busy: GPU card0 35% busy*. `top` shows CPU use; for the GPU, `nvidia-smi` on
  NVIDIA, or `cat /sys/class/drm/card*/device/gpu_busy_percent` on AMD. Where the GPU has no
  per-program reading, the desktop's own drawing counts too (see
  [When it steps aside](#when-it-steps-aside-and-what-it-deletes)).
- **Helper paused · it's low on memory**: less than about 3.5 GB of memory is free on the
  helper, or less than 6.5 GB beside a game left resting there (a resting game keeps all of
  its memory: a big one can hold 9 GB and leave under 3 GB). Close that game or other big
  programs. Beside a resting game it also reads so when the helper can't start a
  memory-capped scope of its own: `systemd-run --user --scope true` has to work over ssh. In
  `vworker status`, `mem_available_gb` is the free memory and `resting_game` names a game that
  rests.
- **Helper paused · a game is running on it** right after you put a game to rest: the helper
  waits until the game has rested for a minute, and Visionary asks it again every minute.
- **It falls asleep overnight**, or the *Sleep* row says *it can't keep itself awake over
  ssh*: turn off its automatic sleep while it's plugged in (see
  [Keeping it awake](#keeping-it-awake)). In `vworker status`, `sleep_block` is then `false`.
- **Build fails**: Setup shows the build's output. The usual causes are no build tools and no
  distrobox, a distribution without a box, or no internet connection on the helper.

To read the status by hand (all read-only):

```bash
curl -s 'http://127.0.0.1:8765/api/helper/setup?fresh=1' | python3 -m json.tool      # Setup's checks
curl -s http://127.0.0.1:8765/api/state | python3 -c 'import json,sys; print(json.load(sys.stdin).get("helper"))'
ssh -T -i ~/.ssh/id_ed25519_visionary_helper you@helper.local '~/visionary-worker/bin/vworker status'
```

## Setting it up by hand

Setup's buttons run these scripts, which ship inside the app. Use them if you prefer Terminal.
On the Mac:

```bash
H=/Applications/Visionary.app/Contents/Resources/engine/helper_worker
KEY=~/.ssh/id_ed25519_visionary_helper
```

1. **Key** (skip if you have one): `mkdir -p -m 700 ~/.ssh` and
   `ssh-keygen -t ed25519 -N "" -C visionary-helper -f "$KEY"`, then
   `ssh-copy-id -i "$KEY.pub" you@helper.local`.
2. **Worker:** `bash "$H/install.sh" --host helper.local --user you --key "$KEY"`. It copies the
   worker, checks it by sha256, adds the cleanup timer where it can, and ends by printing the
   helper's `vworker status`.
3. **x265**, see below.
4. **Settings:** enter the address, login and key in Setup → Helper and press **Save** (or see
   [Configuration](CONFIGURATION.md#settingsjson--the-helper)), then switch **Helper** on.

### Building x265 by hand

`build-x265.sh --version <token> [--force]` runs **on the helper**. `<token>` is what the
Mac's x265 prints after *HEVC encoder version*: a release tag such as `4.3`, or a tag, a
distance and a commit such as `4.1+1-1d117be`. The script builds exactly that source, makes
the binary report exactly that token, and does nothing if a matching x265 is already
installed (`--force` rebuilds it). From the Mac:

```bash
tok=$(/opt/homebrew/bin/x265 --version 2>&1 | sed -n 's/.*HEVC encoder version \([^ ]*\).*/\1/p' | head -n 1)
ssh -T -i "$KEY" you@helper.local "bash -s -- --version $tok" < "$H/build-x265.sh"
```

Without distrobox, install the build tools on the helper first. CMake 4 works too, for every
x265 version the script builds.

| Distribution | Command |
|---|---|
| Fedora | `sudo dnf install git cmake make gcc-c++ nasm libstdc++-static` |
| Ubuntu, Debian | `sudo apt install git cmake make g++ nasm` |
| Arch | `sudo pacman -S --needed git cmake make gcc nasm` |

## Removing it

On the Mac, switch **Helper** off, then clear the address and login in Setup and press
**Save**. Then, on the helper:

```bash
systemctl --user disable --now visionary-worker-sweep.timer     # if Install added the timer
rm -f ~/.config/systemd/user/visionary-worker-sweep.service ~/.config/systemd/user/visionary-worker-sweep.timer
rm -rf ~/visionary-worker ~/visionary-scratch                   # the worker, its x265 and build files, job folders
distrobox list                                                  # if Build used a box: distrobox rm <its name>
```

Finally remove the Mac's key from `~/.ssh/authorized_keys` on the helper (the line ending in
`visionary-helper`), and delete `~/.ssh/id_ed25519_visionary_helper` and its `.pub` on the
Mac if nothing else uses them.
