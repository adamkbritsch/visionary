import unittest
import gate


class RetryOnDrain(unittest.TestCase):
    def test_retries_every_interval_until_progress(self):
        outcomes = iter(["drain", "drain", "done"])
        slept = []
        r = gate.retry_on_drain(lambda: next(outcomes), slept.append, interval_s=1800)
        self.assertEqual(r, "done")
        self.assertEqual(slept, [1800, 1800])   # backed off 30 min, twice, then made progress

    def test_no_drain_means_no_waiting(self):
        slept = []
        r = gate.retry_on_drain(lambda: "done", slept.append)
        self.assertEqual(r, "done")
        self.assertEqual(slept, [])

    def test_stops_when_window_closes(self):
        slept = []
        keep = iter([True, False])
        r = gate.retry_on_drain(lambda: "drain", slept.append, keep_going=lambda: next(keep))
        self.assertEqual(r, "stopped")
        self.assertEqual(slept, [1800])


if __name__ == "__main__":
    unittest.main()
