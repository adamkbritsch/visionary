"""Power-adequacy gate for the upscaling engine.

macOS cannot read mains voltage (the adapter hides it behind low-voltage DC),
so "only run on adequate wall power" is detected behaviorally instead: if the
Mac is plugged in (ExternalConnected) yet the battery is still *draining* under
load, the adapter can't keep up with Topaz+Resolve and the run quits for the
night.

Gotcha handled below: macOS reports a discharging current as an unsigned
64-bit integer (2^64 - x), so a naive read sees a draining battery as charging
hard. `normalize_amperage` reinterprets it as signed.
"""
from __future__ import annotations
import re
import subprocess
from dataclasses import dataclass

_U64 = 1 << 64
_S63 = 1 << 63


def normalize_amperage(raw) -> int:
    raw = int(raw)
    return raw - _U64 if raw >= _S63 else raw


@dataclass
class PowerReading:
    external_connected: bool
    is_charging: bool
    capacity: int          # percent, -1 if unknown
    amperage: int          # normalized mA; negative = discharging


def is_draining_on_ac(reading: PowerReading, noise_floor: int = -50) -> bool:
    """True only when plugged in AND net-discharging beyond sensor noise."""
    return reading.external_connected and reading.amperage < noise_floor


class DrainMonitor:
    """Trips only on *sustained* drain, to ignore momentary load spikes."""

    def __init__(self, min_consecutive: int = 5, noise_floor: int = -50):
        self.min_consecutive = min_consecutive
        self.noise_floor = noise_floor
        self._streak = 0

    def add(self, reading: PowerReading) -> int:
        if is_draining_on_ac(reading, self.noise_floor):
            self._streak += 1
        else:
            self._streak = 0
        return self._streak

    def inadequate(self) -> bool:
        return self._streak >= self.min_consecutive


# --------------------------------------------------------------------------
# Real reader (macOS ioreg). Pure logic above is unit-tested; this is the thin
# I/O glue, validated by a live read.
# --------------------------------------------------------------------------

def _grab(text: str, key: str):
    m = re.search(r'"%s"\s*=\s*(Yes|No|-?\d+)' % re.escape(key), text)
    return m.group(1) if m else None


def read_power() -> PowerReading:
    try:
        out = subprocess.run(["ioreg", "-rc", "AppleSmartBattery"],
                             capture_output=True, text=True, timeout=10).stdout
    except (subprocess.TimeoutExpired, OSError):
        # ioreg hung/failed — don't wedge the power monitor or the /api/state poll.
        # Report "on AC, not draining" so a transient read glitch doesn't pause a run.
        return PowerReading(external_connected=True, is_charging=False, capacity=-1, amperage=0)
    ext = _grab(out, "ExternalConnected")
    chg = _grab(out, "IsCharging")
    cap = _grab(out, "CurrentCapacity")
    amp = _grab(out, "InstantAmperage")
    if amp is None:
        amp = _grab(out, "Amperage")
    return PowerReading(
        external_connected=(ext == "Yes"),
        is_charging=(chg == "Yes"),
        capacity=int(cap) if cap and cap.lstrip("-").isdigit() else -1,
        amperage=normalize_amperage(amp) if amp is not None else 0,
    )


def power_adequate_now() -> bool:
    """Single-sample check: True only on wall power that isn't draining. (Must require
    AC — is_draining_on_ac is False on battery, so without the AC check this wrongly
    reported 'adequate' while unplugged.)"""
    r = read_power()
    return bool(r.external_connected) and not is_draining_on_ac(r)


_HAS_BATTERY = None            # a Mac never grows a battery — probe once, reuse (this is
                               # polled every 2 s by the power monitor)


def has_battery() -> bool:
    """Does this Mac have an internal battery? The laptop/desktop discriminator.

    LOAD-BEARING: every power rule here is about a battery draining under a hours-long
    GPU load. A desktop Mac (mini/Studio/iMac) is mains-powered by definition, and it has
    no AppleSmartBattery — so `read_power()` reports external_connected=False and the run
    gate would otherwise pause it FOREVER "waiting for the 140 W adapter". Fails SAFE:
    if the probe itself errors we assume a battery (the stricter laptop path)."""
    global _HAS_BATTERY
    if _HAS_BATTERY is not None:
        return _HAS_BATTERY
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True,
                             text=True, timeout=10).stdout
    except (subprocess.TimeoutExpired, OSError):
        return True                       # probe failed → assume the STRICTER laptop path,
                                          # and don't cache a guess
    _HAS_BATTERY = "InternalBattery" in out
    return _HAS_BATTERY


def model_id() -> str:
    """This Mac's hardware identifier, e.g. 'Mac15,11' (16-inch M3 Max). '' if unreadable."""
    try:
        return subprocess.run(["sysctl", "-n", "hw.model"], capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def power_class() -> tuple:
    """(kind, model, watts) — ONE place both preflight and the run gate consult, so the
    setup check and the live check can never disagree. kind is 'desktop' (no battery →
    mains-powered, no wattage rule) or 'laptop' (the 140 W rule applies)."""
    return ("laptop" if has_battery() else "desktop", model_id(), adapter_watts())


def adapter_watts():
    """The connected power adapter's wattage (int), or None if on battery / unknown.
    The Topaz stage needs >= ~140 W or an M3-Max encode drains the battery."""
    try:
        out = subprocess.run(["pmset", "-g", "adapter"], capture_output=True, text=True).stdout
        m = re.search(r"Wattage\s*=\s*(\d+)", out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


if __name__ == "__main__":
    r = read_power()
    print(f"external={r.external_connected} charging={r.is_charging} "
          f"capacity={r.capacity}% amperage={r.amperage}mA")
    print("draining-on-AC:", is_draining_on_ac(r), "| adequate:", power_adequate_now())
