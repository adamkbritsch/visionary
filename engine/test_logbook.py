import os
import tempfile
import unittest
from unittest import mock

import logbook
import orchestrator as orch


class LogTail(unittest.TestCase):
    def test_tail_filters_to_problem_levels(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.multiple(logbook, LOG_DIR=d, LOG_FILE=os.path.join(d, "x.log")):
                logbook.event("all good")
                logbook.failure("stage topaz failed")
                try:
                    raise ValueError("boom")
                except ValueError as e:
                    logbook.exception("resolve S01E01", e)
                issues = logbook.tail(10)                  # FAIL + ERROR only
                self.assertTrue(any("FAIL" in l for l in issues))
                self.assertTrue(any("ERROR" in l for l in issues))
                self.assertFalse(any("all good" in l for l in issues))   # INFO filtered out
                self.assertTrue(any("all good" in l for l in logbook.tail(10, levels=None)))


if __name__ == "__main__":
    unittest.main()
