"""Display enumeration — every attached screen, not just the main one.

Read-only. Nothing here decides policy; `preflight.match_display` remains the single
eligibility rule and `versions.REQUIRED_BACKING_SCALE` remains the invariant. This module
exists because three separate places (preflight, dv_shim, the smoke test) each grew their
own partial copy of the CoreGraphics dance, and all three could only ever see the MAIN
display — which is the whole obstacle to hosting Resolve on a second screen.

ctypes hazard, learned the hard way in preflight.py: EVERY function needs explicit
`argtypes`/`restype`. Without them ctypes truncates 64-bit pointers (CGDisplayModeRef,
CFStringRef) to 32-bit ints and the process SEGFAULTS rather than raising.
"""

import ctypes
import ctypes.util

MAX_DISPLAYS = 16

# A display's identity has to survive unplug/replug and reboot, so the priority list can
# name a screen that is not currently attached. CGDirectDisplayID does NOT survive (it is
# 1 and 5 on this machine today and is reassigned freely), so it is never persisted.
# CGDisplayCreateUUIDFromDisplayID lives in ColorSync, NOT CoreGraphics — binding it against
# CoreGraphics silently returns NULL, which is why it looks like it does not exist.
_COLORSYNC = "/System/Library/Frameworks/ColorSync.framework/ColorSync"


def _cg():
    lib = ctypes.util.find_library("CoreGraphics")
    if not lib:
        raise RuntimeError("CoreGraphics not found")
    cg = ctypes.CDLL(lib)
    cg.CGMainDisplayID.restype = ctypes.c_uint32
    cg.CGGetActiveDisplayList.restype = ctypes.c_int32
    cg.CGGetActiveDisplayList.argtypes = [ctypes.c_uint32,
                                          ctypes.POINTER(ctypes.c_uint32),
                                          ctypes.POINTER(ctypes.c_uint32)]
    cg.CGDisplayIsBuiltin.restype = ctypes.c_bool
    cg.CGDisplayIsBuiltin.argtypes = [ctypes.c_uint32]
    cg.CGDisplayIsInMirrorSet.restype = ctypes.c_bool
    cg.CGDisplayIsInMirrorSet.argtypes = [ctypes.c_uint32]
    cg.CGDisplayMirrorsDisplay.restype = ctypes.c_uint32
    cg.CGDisplayMirrorsDisplay.argtypes = [ctypes.c_uint32]
    cg.CGDisplayPixelsWide.restype = ctypes.c_size_t
    cg.CGDisplayPixelsWide.argtypes = [ctypes.c_uint32]
    cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t
    cg.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]
    cg.CGDisplayCopyDisplayMode.restype = ctypes.c_void_p
    cg.CGDisplayCopyDisplayMode.argtypes = [ctypes.c_uint32]
    cg.CGDisplayModeGetPixelWidth.restype = ctypes.c_size_t
    cg.CGDisplayModeGetPixelWidth.argtypes = [ctypes.c_void_p]
    cg.CGDisplayModeGetPixelHeight.restype = ctypes.c_size_t
    cg.CGDisplayModeGetPixelHeight.argtypes = [ctypes.c_void_p]
    cg.CGDisplayBounds.restype = _CGRect
    cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
    cg.CGDisplayVendorNumber.restype = ctypes.c_uint32
    cg.CGDisplayVendorNumber.argtypes = [ctypes.c_uint32]
    cg.CGDisplayModelNumber.restype = ctypes.c_uint32
    cg.CGDisplayModelNumber.argtypes = [ctypes.c_uint32]
    cg.CGDisplaySerialNumber.restype = ctypes.c_uint32
    cg.CGDisplaySerialNumber.argtypes = [ctypes.c_uint32]
    return cg


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _CGRect(ctypes.Structure):
    _fields_ = [("origin", _CGPoint), ("size", _CGSize)]


def _uuid_for(display_id):
    """Stable identity, or None. Never raises — a missing UUID falls back to the
    vendor/model/serial key, which is weaker (two identical panels collide) but real."""
    try:
        cs = ctypes.CDLL(_COLORSYNC)
        cs.CGDisplayCreateUUIDFromDisplayID.restype = ctypes.c_void_p
        cs.CGDisplayCreateUUIDFromDisplayID.argtypes = [ctypes.c_uint32]
        ref = cs.CGDisplayCreateUUIDFromDisplayID(display_id)
        if not ref:
            return None
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        cf.CFUUIDCreateString.restype = ctypes.c_void_p
        cf.CFUUIDCreateString.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFStringGetCStringPtr.restype = ctypes.c_char_p
        cf.CFStringGetCStringPtr.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                          ctypes.c_long, ctypes.c_uint32]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        s = cf.CFUUIDCreateString(None, ref)
        try:
            if not s:
                return None
            kCFStringEncodingUTF8 = 0x08000100
            p = cf.CFStringGetCStringPtr(s, kCFStringEncodingUTF8)
            if p:
                return p.decode("utf-8")
            buf = ctypes.create_string_buffer(128)
            if cf.CFStringGetCString(s, buf, 128, kCFStringEncodingUTF8):
                return buf.value.decode("utf-8")
            return None
        finally:
            if s:
                cf.CFRelease(s)
            cf.CFRelease(ref)
    except Exception:
        return None


def display_key(uuid, vendor, model, serial, px_w, px_h) -> str:
    """PURE (unit-tested). The string a saved priority list stores. UUID when we have one;
    otherwise vendor/model/serial plus the backing size, which is stable across replug on
    everything except a pair of identical panels."""
    if uuid:
        return "uuid:%s" % uuid
    return "vhw:%d:%d:%d:%dx%d" % (int(vendor or 0), int(model or 0), int(serial or 0),
                                   int(px_w or 0), int(px_h or 0))


def _one(cg, did, main_id):
    mode = cg.CGDisplayCopyDisplayMode(did)
    if not mode:
        raise RuntimeError("CGDisplayCopyDisplayMode returned NULL for display %d" % did)
    try:
        px_w = int(cg.CGDisplayModeGetPixelWidth(mode))
        px_h = int(cg.CGDisplayModeGetPixelHeight(mode))
    finally:
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFRelease(mode)
    pt_w = int(cg.CGDisplayPixelsWide(did))
    pt_h = int(cg.CGDisplayPixelsHigh(did))
    r = cg.CGDisplayBounds(did)
    vendor = int(cg.CGDisplayVendorNumber(did))
    model = int(cg.CGDisplayModelNumber(did))
    serial = int(cg.CGDisplaySerialNumber(did))
    uuid = _uuid_for(did)
    # A mirror SLAVE has no independent framebuffer to full-screen onto — it just repeats
    # another display — so it can never host Resolve.
    mirror_slave = bool(cg.CGDisplayIsInMirrorSet(did)) and cg.CGDisplayMirrorsDisplay(did) != 0
    return {
        "id": int(did),
        "uuid": uuid,
        "key": display_key(uuid, vendor, model, serial, px_w, px_h),
        "vendor": vendor, "model": model, "serial": serial,
        "builtin": bool(cg.CGDisplayIsBuiltin(did)),
        "main": int(did) == int(main_id),
        "mirror_slave": mirror_slave,
        "backing": (px_w, px_h),                     # real pixels
        "size_pt": (pt_w, pt_h),                     # logical points
        "scale": (px_w / pt_w) if pt_w else 0.0,
        # GLOBAL desktop points — this is the space cliclick clicks in and the space
        # `screencapture -R` selects in. The main display's origin is (0, 0).
        "origin": (float(r.origin.x), float(r.origin.y)),
    }


def enumerate_displays():
    """Every ACTIVE display, main first. Raises only if CoreGraphics itself is unavailable;
    a single display that fails to describe is skipped rather than sinking the list."""
    cg = _cg()
    count = ctypes.c_uint32(0)
    ids = (ctypes.c_uint32 * MAX_DISPLAYS)()
    if cg.CGGetActiveDisplayList(MAX_DISPLAYS, ids, ctypes.byref(count)) != 0:
        raise RuntimeError("CGGetActiveDisplayList failed")
    main_id = cg.CGMainDisplayID()
    out = []
    for i in range(count.value):
        try:
            out.append(_one(cg, ids[i], main_id))
        except Exception:
            continue
    out.sort(key=lambda d: (not d["main"], d["id"]))
    return out


def main_display():
    """The main display's descriptor, or None."""
    for d in enumerate_displays():
        if d["main"]:
            return d
    return None


def find(key):
    """The attached display matching a saved key, or None when it is not plugged in."""
    if not key:
        return None
    for d in enumerate_displays():
        if d["key"] == key:
            return d
    return None
