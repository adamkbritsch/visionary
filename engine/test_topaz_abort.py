import os
import subprocess
import threading
import time
import unittest

import topaz


class AbortKillsEncodeTest(unittest.TestCase):
    """The bug: stopping a run left the Topaz ffmpeg running (orphaned). _run_ffmpeg
    must kill the process promptly when `abort` fires, and terminate_all() must kill
    any in-flight proc on shutdown."""

    def test_abort_kills_process_promptly(self):
        # a stand-in for Topaz ffmpeg: emits frame= lines forever until killed
        cmd = ["sh", "-c", "i=0; while :; do i=$((i+1)); echo frame=$i; sleep 0.05; done"]
        ab = threading.Event()
        seen = []
        threading.Timer(0.4, ab.set).start()         # stop the "run" after 0.4 s
        t0 = time.time()
        rc, frames, aborted, _tail = topaz._run_ffmpeg(
            cmd, {**os.environ}, abort=ab, on_progress=seen.append)
        dt = time.time() - t0
        self.assertTrue(aborted, "should report aborted")
        self.assertLess(dt, 2.0, "abort must stop the encode within ~1 s, not run on")
        self.assertGreater(frames, 0, "should have streamed some progress first")
        self.assertEqual(len(topaz._ACTIVE), 0, "proc must be deregistered")

    def test_terminate_all_kills_active(self):
        proc = subprocess.Popen(["sh", "-c", "sleep 30"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        with topaz._ACTIVE_LOCK:
            topaz._ACTIVE.add(proc)
        try:
            topaz.terminate_all()
            for _ in range(20):
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(proc.poll(), "terminate_all must kill in-flight encodes")
        finally:
            with topaz._ACTIVE_LOCK:
                topaz._ACTIVE.discard(proc)
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
