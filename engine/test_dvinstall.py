import hashlib
import os
import shutil
import tempfile
import unittest
from unittest import mock

import dvinstall


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Install(unittest.TestCase):
    """dvinstall copies the bundled, pinned toolchain files: ensure() only what is missing,
    repair() also what is not the pinned file (keeping it), and never a bundle that is wrong."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.res = os.path.join(self.d, "res")
        os.makedirs(os.path.join(self.res, "dv"))
        self.good = {"an": b"analyzer-bytes", "lut": b"LUT_3D_SIZE 2\n"}
        self.dest = {"an": os.path.join(self.d, "home", "bin", "hdr_analyzer_mvp"),
                     "lut": os.path.join(self.d, "home", "luts", "rcm_sdr709_to_pq1000.cube")}
        files = (("hdr_analyzer_mvp", self.dest["an"], _sha(self.good["an"]), 0o755),
                 ("rcm_sdr709_to_pq1000.cube", self.dest["lut"], _sha(self.good["lut"]), 0o644))
        p = mock.patch.object(dvinstall, "FILES", files)
        p.start()
        self.addCleanup(p.stop)
        self._bundle()

    def _bundle(self, an=None, lut=None):
        for name, data in (("hdr_analyzer_mvp", an or self.good["an"]),
                           ("rcm_sdr709_to_pq1000.cube", lut or self.good["lut"])):
            with open(os.path.join(self.res, "dv", name), "wb") as f:
                f.write(data)

    def _read(self, k):
        with open(self.dest[k], "rb") as f:
            return f.read()

    def test_a_fresh_machine_gets_both_files_with_their_modes(self):
        out = dvinstall.ensure(self.res)
        self.assertEqual((self._read("an"), self._read("lut")), (self.good["an"], self.good["lut"]))
        self.assertTrue(os.access(self.dest["an"], os.X_OK))
        self.assertEqual(os.stat(self.dest["lut"]).st_mode & 0o777, 0o644)
        self.assertTrue(all("installed to" in line for line in out), out)
        self.assertTrue(all("already installed" in l for l in dvinstall.ensure(self.res)))

    def test_ensure_never_replaces_a_file_that_is_there(self):
        os.makedirs(os.path.dirname(self.dest["lut"]))
        with open(self.dest["lut"], "wb") as f:
            f.write(b"someone else's LUT")
        out = dvinstall.ensure(self.res)
        self.assertEqual(self._read("lut"), b"someone else's LUT")
        self.assertTrue(any("left as it is" in line for line in out), out)

    def test_repair_replaces_a_wrong_file_and_keeps_it(self):
        os.makedirs(os.path.dirname(self.dest["lut"]))
        with open(self.dest["lut"], "wb") as f:
            f.write(b"someone else's LUT")
        dvinstall.repair(self.res)
        self.assertEqual(self._read("lut"), self.good["lut"])
        kept = [n for n in os.listdir(os.path.dirname(self.dest["lut"])) if ".replaced-" in n]
        self.assertEqual(len(kept), 1)

    def test_a_damaged_bundle_is_never_copied(self):
        self._bundle(an=b"truncated")
        out = dvinstall.repair(self.res)
        self.assertFalse(os.path.exists(self.dest["an"]))
        self.assertTrue(any("NOT installed" in line for line in out), out)
        self.assertEqual(self._read("lut"), self.good["lut"])          # the other one still goes in


class TheRepoBundleIsThePinnedPair(unittest.TestCase):
    """dv/ in the repo carries exactly the files versions.py pins."""

    def test_hashes(self):
        import versions
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dv")
        self.assertEqual(dvinstall._sha256(os.path.join(root, "hdr_analyzer_mvp")),
                         versions.HDR_ANALYZER_SHA256)
        self.assertEqual(dvinstall._sha256(os.path.join(root, "rcm_sdr709_to_pq1000.cube")),
                         versions.NATIVE_LUT_SHA256)
        self.assertTrue(os.access(os.path.join(root, "hdr_analyzer_mvp"), os.X_OK))


if __name__ == "__main__":
    unittest.main()
