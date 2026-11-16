"""Turn monotonically increasing kernel counters into per-second rates.

Kernel counters (bytes sent, sectors written, packets dropped) only go up, until
they don't: an interface is re-created, a driver reloads, a 32-bit counter wraps,
or a USB disk is re-plugged. When a counter goes backwards we cannot know how much
happened in that interval, so we drop that one interval and reseed the baseline.
Reporting nothing is better than reporting a huge negative or a fake spike.
"""

from __future__ import annotations

from collections.abc import Hashable


class RateTracker:
    def __init__(self) -> None:
        self._last: dict[Hashable, tuple[float, float]] = {}
        self._touched: set[Hashable] = set()
        self.resets = 0

    def rate(self, key: Hashable, value: float, ts: float) -> float | None:
        """Per-second rate since the previous call for `key`, or None."""
        self._touched.add(key)
        prev = self._last.get(key)
        self._last[key] = (value, ts)
        if prev is None:
            return None
        prev_value, prev_ts = prev
        dt = ts - prev_ts
        if dt <= 0:
            return None
        if value < prev_value:
            self.resets += 1
            return None
        return (value - prev_value) / dt

    def delta(self, key: Hashable, value: float, ts: float) -> float | None:
        """Raw increase since the previous call (for drop / error counts)."""
        self._touched.add(key)
        prev = self._last.get(key)
        self._last[key] = (value, ts)
        if prev is None:
            return None
        if value < prev[0]:
            self.resets += 1
            return None
        return value - prev[0]

    def sweep(self) -> None:
        """Call once per tick: forget devices not seen this tick, so memory stays bounded
        and a device that comes back starts from a fresh baseline."""
        for key in [k for k in self._last if k not in self._touched]:
            del self._last[key]
        self._touched = set()
