"""Scratch-volume manager for the upscaling engine.

The pipeline writes ~300 GB ProRes intermediates, so it needs the external
2 TB drive as scratch. That drive sometimes "ghosts" on macOS (device still
attached, mount dropped), so before a run we force a disconnect/reconnect
cycle. If the drive still can't be brought back writable, we fall back to a
directory on the internal disk (e.g. ~/Downloads) so the engine keeps running
(in a reduced-capacity mode the disk-watermark check is expected to respect).
"""
from __future__ import annotations
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass


@dataclass
class ScratchResult:
    path: str
    source: str  # "external" | "fallback"


class DiskutilOps:
    """Real macOS implementation via `diskutil` + the filesystem.

    Volumes are addressed by NAME, never device node — the node (disk4s2 etc.)
    changes across reconnects, the name does not.
    """

    def unmount(self, name: str) -> bool:
        r = subprocess.run(["diskutil", "unmount", name],
                           capture_output=True, text=True)
        if r.returncode != 0:  # ghosted/busy -> escalate to force
            r = subprocess.run(["diskutil", "unmount", "force", name],
                               capture_output=True, text=True)
        return r.returncode == 0

    def mount(self, name: str) -> bool:
        r = subprocess.run(["diskutil", "mount", name],
                           capture_output=True, text=True)
        return r.returncode == 0

    def mount_point(self, name: str):
        p = f"/Volumes/{name}"
        return p if os.path.ismount(p) else None

    def writable(self, path: str) -> bool:
        try:
            os.makedirs(path, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path) as f:
                f.write(b"ok")
                f.flush()
            return True
        except OSError:
            return False

    def ensure_dir(self, path: str) -> str:
        os.makedirs(path, exist_ok=True)
        return path


class ScratchManager:
    def __init__(self, volume_name: str, external_subdir: str,
                 fallback_dir: str, ops=None):
        self.volume_name = volume_name
        self.external_subdir = external_subdir
        self.fallback_dir = fallback_dir
        self.ops = ops or DiskutilOps()

    def cycle(self) -> None:
        """Disconnect then reconnect the external volume (best-effort).

        A ghosted volume can make unmount error; that must not abort the run,
        so the remount is always attempted regardless.
        """
        try:
            self.ops.unmount(self.volume_name)
        except Exception:
            pass
        try:
            self.ops.mount(self.volume_name)
        except Exception:
            pass

    def prepare(self) -> ScratchResult:
        self.cycle()
        mp = self.ops.mount_point(self.volume_name)
        if mp:
            ext = os.path.join(mp, self.external_subdir)
            if self.ops.writable(ext):
                return ScratchResult(ext, "external")
        self.ops.ensure_dir(self.fallback_dir)
        return ScratchResult(self.fallback_dir, "fallback")


# The active workflow scratch is now the INTERNAL SSD. The external "2TB SSD"
# proved UNRELIABLE for heavy live I/O — its connection degrades (a software
# unmount/remount wasn't enough; it needed a physical replug), which made
# renders crawl (~1%/min vs ~10 min when healthy). So it's relegated to cold
# storage and the reliable internal SSD hosts the per-episode working set
# (~238 GB ProRes XQ + render), kept clear by the serial + immediate-cleanup
# policy. Internal disk is always mounted, so no diskutil cycling is needed.
INTERNAL_SCRATCH = os.path.expanduser("~/topaz-scratch")
PREFETCH_SUBDIR = "prefetch"      # where the prefetcher stages upcoming downloads — a subfolder of scratch
                                  # so its disk still counts toward the pipeline's footprint, but it's kept
                                  # OUT of the "scratch contents" preview (promoted into scratch on process).


def default_scratch() -> str:
    """Reliable scratch dir on the internal SSD (always mounted, no cycling)."""
    os.makedirs(INTERNAL_SCRATCH, exist_ok=True)
    return INTERNAL_SCRATCH


def prefetch_dir() -> str:
    """Subfolder of scratch holding the prefetch buffer (upcoming items' source + CFR), hidden from the
    scratch-contents preview and promoted into the main scratch when an item starts processing."""
    d = os.path.join(default_scratch(), PREFETCH_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d


def folder_used_gb(path: str = None) -> int:
    """GB the topaz-scratch folder is currently using (sum of file sizes)."""
    base = path or default_scratch()
    total = 0
    for root, _, files in os.walk(base):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total // (1024 ** 3)


def physical_free_gb(path: str = None):
    """RAW physical free space (GB), NOT counting scratch's own footprint. The prefetcher gates on this
    (not available_gb) — the prefetch buffer IS what it's filling, so it must respect real free space or
    it would overcommit the disk (its bytes can't count as 'available' to itself). None if unreadable."""
    base = path or default_scratch()
    try:
        return shutil.disk_usage(base).free // (1024 ** 3)
    except OSError:
        return None


def available_gb(path: str = None):
    """Space 'left' for the pipeline = physical FREE space PLUS what topaz-scratch already
    holds. The scratch's current footprint is the pipeline's own working files (recycled by
    cleanup each item), so it counts as available to the project — a partially-filled scratch
    shouldn't read as 'no room'. (e.g. 945 free + 223 scratch = 1168.) None if unreadable."""
    base = path or default_scratch()
    try:
        free = shutil.disk_usage(base).free // (1024 ** 3)
    except OSError:
        return None
    return free + folder_used_gb(base)


def folder_preview(path: str = None) -> list:
    """A snapshot of the topaz-scratch folder's CURRENT top-level contents — every file/folder
    in it right now, biggest first, as [{name, bytes, is_dir}]. A folder's size is its recursive
    total (so a .segments dir reads as one line). Hidden entries (.DS_Store, …) are skipped.
    Just a 'what's in scratch' listing — NOT filtered to finished deliverables."""
    base = path or default_scratch()
    items = []
    try:
        names = os.listdir(base)
    except OSError:
        return []
    for name in names:
        if name.startswith(".") or name == PREFETCH_SUBDIR:   # hide the prefetch buffer (upcoming items)
            continue
        full = os.path.join(base, name)
        is_dir = os.path.isdir(full)
        size = 0
        if is_dir:
            for root, _, files in os.walk(full):
                for f in files:
                    try:
                        size += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
        else:
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
        items.append({"name": name, "bytes": size, "is_dir": is_dir})
    items.sort(key=lambda x: (-x["bytes"], x["name"].lower()))
    return items


def external_manager() -> ScratchManager:
    """Legacy external-SSD manager (the 2TB SSD) — cold storage now, not the
    default scratch. Kept for the unmount/remount cycle if the drive is used."""
    return ScratchManager(
        volume_name="2TB SSD",
        external_subdir="topaz-scratch",
        fallback_dir=os.path.expanduser("~/Downloads/topaz-scratch"),
    )


if __name__ == "__main__":
    print("workflow scratch (internal SSD):", default_scratch())
