"""Run/backoff policy for the engine.

Persistence over giving up: when the power supply proves inadequate (battery
draining under load), the engine does NOT quit for the night — it backs off and
retries every 30 minutes, re-running the battery-detection process each time, so
it resumes and makes real progress the moment conditions allow (cooler machine,
a beefier adapter plugged in, lighter background load, etc.).
"""
from __future__ import annotations

RETRY_INTERVAL_S = 30 * 60  # 30 minutes


def retry_on_drain(attempt, sleep, interval_s: int = RETRY_INTERVAL_S,
                   keep_going=lambda: True):
    """Call attempt() repeatedly to make progress.

    attempt() returns:
      - "drain": power was inadequate (battery drained under load) -> back off
        `interval_s` and retry.
      - anything else (e.g. "done"): progress made / finished -> return it.

    Stops early and returns "stopped" if keep_going() becomes false (e.g. the
    8 PM-9 AM window closed or the adapter was unplugged).
    """
    while keep_going():
        outcome = attempt()
        if outcome == "drain":
            sleep(interval_s)
            continue
        return outcome
    return "stopped"
