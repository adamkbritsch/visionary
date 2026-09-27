"""The YouTube cadence dial's arithmetic, COMPILED AND RUN — not read and hoped over.

The dial is Swift, so the engine suite could not see it, and it shipped broken twice in one
afternoon (both user-caught 2026-09-27): first the zero stop was put where "1 video per TV
episode" lived instead of below it, then the save path clamped the burst up to 1 and swallowed
the zero, which made the stepper look stuck one stop above it. Neither was a subtle bug. Both
would have died instantly against any executed assertion.

So macapp/Cadence.swift holds that arithmetic with no SwiftUI or AppKit import, and this test
compiles it with the Swift toolchain the app is built with and runs the real thing.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

MACAPP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "macapp")
CADENCE = os.path.join(MACAPP, "Cadence.swift")

MAIN = r"""
var failures: [String] = []
func check(_ ok: Bool, _ what: String) { if !ok { failures.append(what) } }

// EVERY stop round-trips: the position the dial shows for a pair must produce that same pair.
for pos in CadenceDial.minPos...CadenceDial.maxPos {
    let k = CadenceDial.knobs(pos)
    let back = CadenceDial.position(every: k.every, burst: k.burst)
    check(back == pos, "position \(pos) -> (every \(k.every), burst \(k.burst)) -> \(back)")
}

// The stops around the middle, in order, are the ones the user asked for.
check(CadenceDial.summary(every: 1, burst: 2) == "2 videos per TV episode", "2 per episode")
check(CadenceDial.summary(every: 1, burst: 1) == "1 video per TV episode", "1 per episode")
check(CadenceDial.summary(every: 1, burst: 0) == "no YouTube videos", "the zero stop")
check(CadenceDial.summary(every: 2, burst: 1) == "1 video every 2 TV episodes", "1 every 2")

// ...and stepping DOWN from 1-per-episode reaches the zero, then 1-every-2. This is the exact
// motion that did nothing when the save path clamped the burst.
let atOne = CadenceDial.position(every: 1, burst: 1)
check(atOne == 1, "1-per-episode sits at position 1, not \(atOne)")
let down1 = CadenceDial.knobs(atOne - 1)
check(down1.burst == 0, "one step down from 1-per-episode is the zero, got burst \(down1.burst)")
let down2 = CadenceDial.knobs(atOne - 2)
check(down2.every == 2 && down2.burst == 1, "two steps down is 1 video every 2 episodes")

// The save path must not undo any of it. A zero that becomes a 1 is the whole bug.
let saved = CadenceDial.clampForSave(every: down1.every, burst: down1.burst)
check(saved.burst == 0, "clampForSave turned the zero into \(saved.burst)")
check(CadenceDial.clampForSave(every: 1, burst: 99).burst == CadenceDial.maxBurst, "burst ceiling")
check(CadenceDial.clampForSave(every: 999, burst: 1).every == CadenceDial.maxEvery, "every ceiling")
check(CadenceDial.clampForSave(every: 0, burst: 1).every == 1, "every floor is 1, not 0")
check(CadenceDial.clampForSave(every: 1, burst: -5).burst == 0, "burst floor is 0")

// The ends of the dial are reachable and mean what they say.
check(CadenceDial.knobs(CadenceDial.maxPos) == (1, 10), "the top is 10 videos per episode")
check(CadenceDial.knobs(CadenceDial.minPos) == (50, 1), "the bottom is 1 video every 50")

if failures.isEmpty { print("OK") } else { for f in failures { print("FAIL: \(f)") } }
"""


@unittest.skipUnless(shutil.which("swiftc") and os.path.exists(CADENCE),
                     "needs the Swift toolchain the app is built with")
class CadenceDialArithmetic(unittest.TestCase):
    def test_the_dial_maps_every_stop_to_the_engines_knobs_and_back(self):
        d = tempfile.mkdtemp()
        try:
            main = os.path.join(d, "main.swift")
            with open(main, "w") as f:
                f.write(MAIN)
            binary = os.path.join(d, "dialtest")
            build = subprocess.run(["swiftc", "-O", CADENCE, main, "-o", binary],
                                   capture_output=True, text=True, timeout=300)
            self.assertEqual(build.returncode, 0,
                             f"the dial does not compile:\n{build.stderr[-2000:]}")
            run = subprocess.run([binary], capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr[-2000:])
            self.assertEqual(run.stdout.strip(), "OK", run.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
