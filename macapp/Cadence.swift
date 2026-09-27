// The YouTube cadence dial's arithmetic, deliberately kept OUT of the view.
//
// It lives alone, with no SwiftUI or AppKit import, so engine/test_cadence_dial.py can compile
// this one file with swiftc and actually RUN it. That test exists because the dial shipped twice
// with arithmetic nobody had executed: first the zero stop replaced "1 video per TV episode"
// instead of sitting below it, then the save path clamped the burst to 1 and swallowed the zero,
// so the stepper looked stuck at one-per-episode (both user-caught 2026-09-27).
//
// ONE DIAL, TWO KNOBS. The engine counts in whole numbers in two directions
// (user-dictated 2026-08-17): K videos per episode is (every: 1, burst: K); 1 video every N
// episodes is (every: N, burst: 1); and no videos at all is burst 0. A single Int position maps
// onto that pair, and UP IS MORE YOUTUBE (user-dictated 2026-08-20).
//
// Reading down from the top:
//     10 videos per TV episode        position 10
//      ...
//      2 videos per TV episode        position 2
//      1 video per TV episode         position 1     <- its own stop, never folded into the zero
//      no YouTube videos              position 0     <- the switchover
//      1 video every 2 TV episodes    position -1
//      ...
//      1 video every 50 TV episodes   position -49
//
// The axis is NOT monotonic at the zero, which is the price of putting it in the middle rather
// than at the bottom (user-dictated 2026-09-27).
enum CadenceDial {
    static let minPos = -49          // DOWN: 1 video per 50 episodes
    static let maxPos = 10           // UP:   10 videos per episode
    static let maxBurst = 10
    static let maxEvery = 50

    /// Where the stepper sits for the engine's stored pair.
    static func position(every: Int, burst: Int) -> Int {
        if burst == 0 { return 0 }
        if every <= 1 { return min(maxBurst, max(1, burst)) }
        return -(min(maxEvery, every) - 1)
    }

    /// The engine's pair for a stepper position.
    static func knobs(_ pos: Int) -> (every: Int, burst: Int) {
        if pos == 0 { return (1, 0) }                    // the cadence serves nothing
        if pos >= 1 { return (1, min(maxBurst, pos)) }   // K videos per episode
        return (min(maxEvery, -pos + 1), 1)              // 1 video every N episodes
    }

    /// What gets written to the engine. ZERO MUST SURVIVE: clamping the burst up to 1 here is
    /// what made the dial look stuck one stop above the zero.
    static func clampForSave(every: Int, burst: Int) -> (every: Int, burst: Int) {
        (max(1, min(maxEvery, every)), max(0, min(maxBurst, burst)))
    }

    /// The line under "YouTube cadence", read from the stored pair rather than the position, so
    /// a value saved before a stop existed still describes itself truthfully.
    static func summary(every: Int, burst: Int) -> String {
        if burst == 0 { return "no YouTube videos" }
        if burst > 1 { return "\(burst) videos per TV episode" }
        return every == 1 ? "1 video per TV episode" : "1 video every \(every) TV episodes"
    }
}
