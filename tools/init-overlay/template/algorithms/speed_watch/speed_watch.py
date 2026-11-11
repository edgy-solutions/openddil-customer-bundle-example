"""A template unit: flag an asset that holds above a speed for several
samples in a row.

Copy it and replace `step`. A unit is one class with one method. `step` gets
the sample (a dict of the roles the declaration names; an optional role can
be absent or None) and this asset's state (a dict of the declared fields).
It changes the state in place and returns {kind: payload} or None.

What it may not do: import anything outside the allowlist in README.md, open
a file or a socket, read the clock, or set severity, labels or provenance.
The time it uses is the sample's own, so a replay gives the same answer.
"""
from __future__ import annotations

from algorithm_runtime.contract import Unit


class SpeedWatch(Unit):
    def step(self, sample, state):
        speed = sample.get("speed_mps")
        if speed is None:
            return None

        if speed <= self.params["speed_limit_mps"]:
            state["over_limit_count"] = 0
            return None

        state["over_limit_count"] += 1
        # Fires once per excursion: on the sample that reaches the count.
        if state["over_limit_count"] != self.params["min_samples"]:
            return None
        return {"anomaly": {
            "summary": f"Speed above {self.params['speed_limit_mps']:.1f} m/s "
                       f"for {self.params['min_samples']} samples",
            "evidence": {"speed_mps": speed, "at_s": sample["sample_time_s"]},
        }}
