"""Never leave DaVinci Resolve opening on the pinned display.

Resolve keeps, per main-screen layout, which screen its window belongs on (Workspace > Primary
Display: PrimaryScreenIdx, with SecondaryScreenIdx its dual-screen partner) and where its
Project Manager opens (ProjectManagerScreenKey), in UI.preset — a Qt QDataStream written
hex-encoded on the line after "UI_Persistence": {"__CurrentPreset": {"LastUsedResolution": ..,
"1728x1117": <QByteArray>, "1920x1080": <QByteArray>, ...}}. It saves that file from whatever
screen its window is on, WHILE IT RUNS as well as when it quits (live 2026-10-04: the Resolve
Visionary launched for S04E02 rewrote it six minutes in, still running).

Visionary puts Resolve's window on the pinned display (the 4K dummy plug) for every DV pass, so
every pass saved the dummy as Resolve's primary display — and from then on the user's own
Resolve opened there, invisible, and snapped back when they tried to move it; the Primary
Display menu is greyed out until a project is open, so they could not undo it from Resolve
(Project Manager drawn at x -1512, off every screen).

So whenever Resolve is NOT running, any of those keys that points at the pinned display is
pointed back at the main one (Primary and Secondary swapped back as a pair). Nothing else in
the file changes: each value is a 4-byte int rewritten in place, inside the layout it belongs
to (found by parsing the map, not by searching), with the file's mode kept. A screen the user
chose that is not the pinned display — a real second monitor — is never touched.
"""
import os
import struct
import subprocess
import sys

PRESET = os.path.expanduser(
    "~/Library/Preferences/Blackmagic Design/DaVinci Resolve/UI.preset")
PRIMARY, SECONDARY, PROJECTS = "PrimaryScreenIdx", "SecondaryScreenIdx", "ProjectManagerScreenKey"
KEYS = (PRIMARY, SECONDARY, PROJECTS)
QT_INT, QT_STRING, QT_BYTES = 2, 10, 12          # QMetaType ids
RESOLVE_PGREP = ["pgrep", "-f", "DaVinci Resolve.app/Contents/MacOS/Resolve"]


def _path(p):
    """The file to use NOW (the module value, so a test's patch applies) — and never the real
    Resolve preferences from inside a test run, the same rule as nas_ftp._connect."""
    p = p or PRESET
    if "unittest" in sys.modules and p == os.path.expanduser(
            "~/Library/Preferences/Blackmagic Design/DaVinci Resolve/UI.preset"):
        raise RuntimeError("a test reached the real Resolve preferences — patch resolve_prefs.PRESET")
    return p


def _hex_line(raw: bytes):
    lines = raw.split(b"\n")
    return lines, max(range(len(lines)), key=lambda k: len(lines[k]))


def layouts(blob: bytes):
    """PURE: [(layout, start, end)] — each layout's QByteArray byte range, from an exact parse
    of the map's top level. None when the shape is not the one known, so nothing is written."""
    try:
        o = 0

        def u32():
            nonlocal o
            v = struct.unpack(">I", blob[o:o + 4])[0]
            o += 4
            return v

        def qstr():
            nonlocal o
            n = u32()
            if n == 0xFFFFFFFF or o + n > len(blob):
                raise ValueError("bad string")
            s = blob[o:o + n].decode("utf-16-be")
            o += n
            return s

        u32(), u32()                                    # stream header (2, 1)
        if qstr() != "__CurrentPreset":
            return None
        out = []
        for _ in range(u32()):
            key, typ = qstr(), u32()
            o += 1                                      # null flag
            if typ not in (QT_STRING, QT_BYTES):
                return None
            n = u32()
            if o + n > len(blob):
                return None
            if typ == QT_BYTES:
                out.append((key, o, o + n))
            o += n
        return out if o == len(blob) else None
    except (struct.error, ValueError, UnicodeDecodeError):
        return None


def fields(blob: bytes) -> list:
    """PURE: [(layout, key, offset, value)] — every screen key inside each layout: a QString
    key (4-byte length, UTF-16BE) followed by an int QVariant (type, null flag, 4-byte int)."""
    out = []
    for layout, start, end in layouts(blob) or []:
        for key in KEYS:
            enc = key.encode("utf-16-be")
            at = blob.find(enc, start, end)
            while at != -1:
                e = at + len(enc)
                if (at - 4 >= start and struct.unpack(">I", blob[at - 4:at])[0] == len(enc)
                        and e + 9 <= end and struct.unpack(">I", blob[e:e + 4])[0] == QT_INT
                        and blob[e + 4] == 0):
                    out.append((layout, key, e + 5, struct.unpack(">i", blob[e + 5:e + 9])[0]))
                at = blob.find(enc, e, end)
    return out


def read(path: str = None) -> dict:
    """{(layout, key): value}; {} when the file cannot be read or is not the known shape."""
    try:
        with open(_path(path), "rb") as fh:
            raw = fh.read()
        lines, i = _hex_line(raw)
        blob = bytes.fromhex(lines[i].strip().decode())
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return {(lay, key): val for lay, key, _o, val in fields(blob)}


def unpinned(values: dict, host: int, layout: str, main: int = 0) -> dict:
    """PURE: the changes that point `layout` away from `host` (Resolve's index for the pinned
    display) and at `main`: {(layout, key): new value}. Primary and Secondary are a pair, so a
    Primary on the host takes Secondary's place and Secondary gets the host's. Only the layout
    of the screens attached NOW: a host index means nothing in a layout saved under others."""
    out = {}
    for layout in [layout]:
        p, s, m = (values.get((layout, k)) for k in KEYS)
        if p == host and p != main:
            out[(layout, PRIMARY)] = main
            if s == main:
                out[(layout, SECONDARY)] = host
        if m == host and m != main:
            out[(layout, PROJECTS)] = main
    return out


def write(changes: dict, path: str = None) -> list:
    """Apply {(layout, key): value} in place: [(layout, key, old, new)] for what changed.
    Every other byte, the hex case and the file's mode stay as they were; the new file is
    synced before it replaces the old one. Nothing is written when nothing differs."""
    path = _path(path)
    with open(path, "rb") as fh:
        raw = fh.read()
    lines, i = _hex_line(raw)
    hx = lines[i].strip()
    blob = bytearray(bytes.fromhex(hx.decode()))
    done = []
    for lay, key, off, val in fields(bytes(blob)):
        new = changes.get((lay, key))
        if new is not None and int(new) != val:
            blob[off:off + 4] = struct.pack(">i", int(new))
            done.append((lay, key, val, int(new)))
    if not done:
        return []
    out = bytes(blob).hex().encode()
    if hx != hx.lower():
        out = out.upper()
    lines[i] = lines[i].replace(hx, out)
    data = b"\n".join(lines)
    mode = os.stat(path).st_mode & 0o7777
    tmp = path + ".visionary-tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as fh:               # a buffered write loops on short writes
            os.fchmod(fh.fileno(), mode)              # the umask must not narrow it (0666 here)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        with open(tmp, "rb") as fh:                   # what lands must be exactly what was meant
            if fh.read() != data:
                raise OSError("the new UI.preset did not write out whole")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return done


def screens_now():
    """(host index or None, current layout "WxH" or None) for the screens attached now.
    Resolve numbers screens the way Qt does on macOS: the main display first, then the others
    in the system's order (its own Primary Display menu lists them so: "Built-in Retina
    Display", "HDP-V104"). The host is a display in the pinned list — whatever the pinning
    switch says: a Resolve left pointing at the dummy breaks unpinned passes on the main screen
    too. None when nothing pinned is attached, or it is the main display right now. The layout
    is the main display's size in points, which is how Resolve names the one it loads."""
    try:
        import displays
        import settings
        pinned = set(settings.get_display_priority() or [])
        screens = [d for d in displays.enumerate_displays() if not d.get("mirror_slave")]
        order = [d for d in screens if d.get("main")] + [d for d in screens if not d.get("main")]
        main = order[0] if order and order[0].get("main") else None
        layout = ("%dx%d" % tuple(int(v) for v in main["size_pt"])) if main else None
        host = next((i for i, d in enumerate(order) if d.get("key") in pinned and i > 0), None)
        return host, layout
    except Exception:  # noqa: BLE001
        return None, None


def host_index():
    return screens_now()[0]


def resolve_running() -> bool:
    try:
        return subprocess.run(RESOLVE_PGREP, capture_output=True).returncode == 0
    except OSError:
        return True                                   # unknown: never write under a live Resolve


def unpin(path: str = None) -> list:
    """Point Resolve away from the pinned display, while it is not running (it would write its
    own values back over ours). [(layout, key, old, new)] for what changed; [] otherwise."""
    host, layout = screens_now()
    if host is None or layout is None or resolve_running():
        return []
    changes = unpinned(read(path), host, layout)
    return write(changes, path) if changes else []
