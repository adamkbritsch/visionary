"""Persistence for the learned ETA model — engine-owned derived state, not user settings.

Lives beside orch_cadence.json / topaz_segbounds.json rather than in settings.json: nothing here
is a knob anyone sets, it is measured. One value matters today — the solo/contended speed-up `k`
of the remux — which is worth persisting because a fresh process would otherwise spend the first
part of every remux with no idea how much the machine speeds up when Topaz ends.

Failure is always non-fatal: an unreadable or corrupt file degrades to the prior, which degrades
to today's estimate. This must never be able to stop a run.
"""
from __future__ import annotations
import json
import os
import threading

MODEL_FILE = os.path.expanduser("~/.topaz-pipeline/eta_model.json")
MAX_SAMPLES = 30           # matches eta.K_SAMPLE_CAP; the median only needs a modest window
_LOCK = threading.Lock()   # the two finisher lanes can finish an item at the same moment


def load() -> dict:
    try:
        with open(MODEL_FILE) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(d: dict) -> None:
    """Atomic tmp+replace, mirroring _write_topaz_bounds / _save_cadence."""
    try:
        os.makedirs(os.path.dirname(MODEL_FILE), exist_ok=True)
        tmp = MODEL_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh, indent=2)
        os.replace(tmp, MODEL_FILE)
    except OSError:
        pass          # a model we can't persist is a model we relearn — never fatal


def add_k_sample(k: float) -> None:
    """Record one observed solo/contended ratio, keeping the most recent MAX_SAMPLES."""
    with _LOCK:
        d = load()
        xs = [x for x in (d.get("k_samples") or []) if isinstance(x, (int, float)) and x > 0]
        xs.append(round(float(k), 4))
        d["k_samples"] = xs[-MAX_SAMPLES:]
        d["version"] = 1
        _save(d)
