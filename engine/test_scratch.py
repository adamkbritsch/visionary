import os
import tempfile
import unittest
import scratch


class FakeOps:
    """In-memory stand-in for diskutil + filesystem, records calls."""
    def __init__(self, mount_point=None, writable=True,
                 unmount_ok=True, mount_ok=True, unmount_raises=False):
        self._mp = mount_point
        self._writable = writable
        self.unmount_ok = unmount_ok
        self.mount_ok = mount_ok
        self.unmount_raises = unmount_raises
        self.calls = []
        self.dirs = []

    def unmount(self, name):
        self.calls.append(("unmount", name))
        if self.unmount_raises:
            raise RuntimeError("volume busy / ghosted")
        return self.unmount_ok

    def mount(self, name):
        self.calls.append(("mount", name))
        return self.mount_ok

    def mount_point(self, name):
        return self._mp

    def writable(self, path):
        return self._writable

    def ensure_dir(self, path):
        self.dirs.append(path)
        return path


SUB = "topaz-scratch"
FB = "/tmp/fallback-downloads"


def mgr(ops):
    return scratch.ScratchManager("2TB SSD", SUB, FB, ops=ops)


class Prepare(unittest.TestCase):
    def test_uses_external_when_mounted_and_writable(self):
        r = mgr(FakeOps(mount_point="/Volumes/2TB SSD", writable=True)).prepare()
        self.assertEqual(r.source, "external")
        self.assertEqual(r.path, "/Volumes/2TB SSD/topaz-scratch")

    def test_disconnects_and_reconnects_before_checking(self):
        ops = FakeOps(mount_point="/Volumes/2TB SSD", writable=True)
        mgr(ops).prepare()
        self.assertEqual(ops.calls[0], ("unmount", "2TB SSD"))
        self.assertEqual(ops.calls[1], ("mount", "2TB SSD"))

    def test_falls_back_when_not_mounted(self):
        r = mgr(FakeOps(mount_point=None)).prepare()
        self.assertEqual(r.source, "fallback")
        self.assertEqual(r.path, FB)

    def test_falls_back_when_mounted_but_not_writable(self):
        # e.g. drive came back as read-only NTFS
        r = mgr(FakeOps(mount_point="/Volumes/2TB SSD", writable=False)).prepare()
        self.assertEqual(r.source, "fallback")

    def test_unmount_failure_does_not_abort_cycle(self):
        # ghosted volume: unmount errors, but remount succeeds -> still usable
        ops = FakeOps(mount_point="/Volumes/2TB SSD", writable=True, unmount_raises=True)
        r = mgr(ops).prepare()
        self.assertEqual(r.source, "external")
        self.assertIn(("mount", "2TB SSD"), ops.calls)


class FolderPreview(unittest.TestCase):
    def _write(self, path, n):
        with open(path, "wb") as f:
            f.write(b"x" * n)

    def test_lists_top_level_biggest_first_with_folder_totals(self):
        d = tempfile.mkdtemp()
        self._write(os.path.join(d, "small.txt"), 100)
        seg = os.path.join(d, "big.segments")
        os.makedirs(os.path.join(seg, "nested"))
        self._write(os.path.join(seg, "a.mov"), 3000)
        self._write(os.path.join(seg, "nested", "b.mov"), 2000)     # folder total = 5000
        self._write(os.path.join(d, ".DS_Store"), 999)              # hidden → skipped
        out = scratch.folder_preview(d)
        self.assertEqual([(i["name"], i["bytes"], i["is_dir"]) for i in out],
                         [("big.segments", 5000, True), ("small.txt", 100, False)])

    def test_missing_dir_is_empty_list(self):
        self.assertEqual(scratch.folder_preview("/no/such/scratch/here"), [])

    def test_physical_free_is_raw_and_below_available(self):
        d = tempfile.mkdtemp()
        self._write(os.path.join(d, "big.bin"), 5_000_000)
        phys = scratch.physical_free_gb(d)
        self.assertIsInstance(phys, int)
        # available_gb ADDS the scratch footprint back; physical_free does NOT — so the prefetch gate
        # (physical_free) can't count the buffer it's filling as available to itself.
        self.assertGreaterEqual(scratch.available_gb(d), phys)

    def test_prefetch_subfolder_is_hidden_from_the_preview(self):
        d = tempfile.mkdtemp()
        self._write(os.path.join(d, "current.mp4"), 100)
        pf = os.path.join(d, scratch.PREFETCH_SUBDIR)                # the prefetch buffer
        os.makedirs(pf)
        self._write(os.path.join(pf, "upcoming.mp4"), 9000)         # big, but must NOT show
        out = scratch.folder_preview(d)
        self.assertEqual([i["name"] for i in out], ["current.mp4"])  # buffer excluded


if __name__ == "__main__":
    unittest.main()
