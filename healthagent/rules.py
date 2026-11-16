"""Deterministic alert rules.

Each rule looks at one sample (every 5 s) or one probe round (every 15 s) and returns
a Signal for every subject it currently cares about:

  FIRING  the condition holds: open an alert, or raise the peak of the open one
  HOLD    no change: keep an open alert open, do not open a new one
          (inside the hysteresis band, or still counting towards a duration)
  CLEAR   the condition is over: close the open alert

Subjects a rule stops mentioning (the process exited, the mount went away) have
their open alert closed by the AlertManager. Rules never read the clock; `now` is
passed in, so a recorded sequence of samples always produces the same alerts.
"""

from __future__ import annotations

import itertools
from collections import OrderedDict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

import psutil

from .config import Config, FailureRule, MemoryClimbRule, ThresholdRule, WatchEntry
from .models import ProbeRound, ProcInfo, Sample

MB = 1024 * 1024


class State(Enum):
    FIRING = "firing"
    HOLD = "hold"
    CLEAR = "clear"


@dataclass(slots=True)
class Signal:
    key: str
    subject: str
    state: State
    value: float | None
    message: str = ""


class Rule:
    name = ""
    source = "sample"  # or "probe"
    unit = ""

    def __init__(self, severity: str) -> None:
        self.severity = severity

    def evaluate(self, data, now: float) -> list[Signal]:  # pragma: no cover - interface
        raise NotImplementedError

    def adopt(self, key: str, subject: str) -> None:
        """An alert for `key` was open when the agent (re)started."""


# -- building block: threshold with duration and hysteresis --------------------
@dataclass(slots=True)
class _Track:
    above_since: float | None = None
    below_since: float | None = None
    last_ts: float | None = None
    firing: bool = False


class Sustained:
    """Open when value > open_above on every sample for open_for seconds.
    Close when value < clear_below on every sample for clear_for seconds.
    Values in between change nothing, which is what stops flapping at the threshold.
    A missing sample (gap > max_gap) breaks both streaks: "without a break" means
    we must have actually seen the value the whole time."""

    def __init__(self, cfg: ThresholdRule, max_gap: float) -> None:
        self.open_above = cfg.open_above
        self.clear_below = cfg.clear_below
        self.open_for = cfg.open_for
        self.clear_for = cfg.clear_for
        self.max_gap = max_gap
        self.tracks: dict[str, _Track] = {}

    def adopt(self, key: str) -> None:
        self.tracks.setdefault(key, _Track()).firing = True

    def tracked(self, key: str) -> bool:
        return key in self.tracks

    def update(self, key: str, value: float, now: float) -> State:
        t = self.tracks.get(key)
        if t is None:
            t = self.tracks[key] = _Track()
        if t.last_ts is not None and now - t.last_ts > self.max_gap:
            t.above_since = t.below_since = None
        t.last_ts = now

        if value > self.open_above:
            t.below_since = None
            if t.above_since is None:
                t.above_since = now
            if t.firing or now - t.above_since >= self.open_for:
                t.firing = True
                return State.FIRING
            return State.HOLD
        if value < self.clear_below:
            t.above_since = None
            if t.below_since is None:
                t.below_since = now
            if t.firing and now - t.below_since < self.clear_for:
                return State.HOLD
            t.firing = False
            del self.tracks[key]  # idle again: nothing to remember
            return State.CLEAR
        # Hysteresis band: an "above" streak is broken, a "below" streak never started.
        t.above_since = t.below_since = None
        return State.HOLD

    def forget_missing(self, live: set[str]) -> None:
        for k in [k for k in self.tracks if k not in live]:
            del self.tracks[k]


class _ThresholdRule(Rule):
    def __init__(self, cfg: ThresholdRule, max_gap: float) -> None:
        super().__init__(cfg.severity)
        self.cfg = cfg
        self.track = Sustained(cfg, max_gap)

    def adopt(self, key: str, subject: str) -> None:
        self.track.adopt(key)

    def _sig(self, key: str, subject: str, state: State, what: str, value: float, extra: str = "") -> Signal:
        if state is State.CLEAR:
            dur = f" for {self.cfg.clear_for:g}s" if self.cfg.clear_for else ""
            msg = f"{what} back to {value:.1f}{self.unit} (< {self.cfg.clear_below:g}{self.unit}{dur})"
        else:
            dur = f" for {self.cfg.open_for:g}s" if self.cfg.open_for else ""
            msg = f"{what} at {value:.1f}{self.unit} (> {self.cfg.open_above:g}{self.unit}{dur}){extra}"
        return Signal(key, subject, state, value, msg)


class CpuHigh(_ThresholdRule):
    name, unit = "cpu_high", "%"

    def evaluate(self, s: Sample, now: float) -> list[Signal]:
        if s.cpu_total is None:
            return [Signal("total", "cpu total", State.HOLD, None)]
        st = self.track.update("total", s.cpu_total, now)
        return [self._sig("total", "cpu total", st, "Total CPU", s.cpu_total)]


class RamHigh(_ThresholdRule):
    name, unit = "ram_high", "%"

    def evaluate(self, s: Sample, now: float) -> list[Signal]:
        if s.mem_percent is None:
            return [Signal("ram", "ram", State.HOLD, None)]
        st = self.track.update("ram", s.mem_percent, now)
        return [self._sig("ram", "ram", st, "RAM used", s.mem_percent)]


class DiskHigh(_ThresholdRule):
    name, unit = "disk_high", "%"

    def evaluate(self, s: Sample, now: float) -> list[Signal]:
        out = []
        for d in s.disks:
            st = self.track.update(d.mount, d.percent, now)
            out.append(self._sig(d.mount, d.mount, st, f"{d.mount} ({d.fstype}) usage", d.percent,
                                 f", {d.free / MB:.0f} MB free"))
        self.track.forget_missing({d.mount for d in s.disks})
        return out


class DiskInodesHigh(_ThresholdRule):
    name, unit = "disk_inodes_high", "%"

    def evaluate(self, s: Sample, now: float) -> list[Signal]:
        out = []
        live = set()
        for d in s.disks:
            if d.inodes_percent is None:
                continue
            live.add(d.mount)
            st = self.track.update(d.mount, d.inodes_percent, now)
            out.append(self._sig(d.mount, d.mount, st, f"{d.mount} inodes used", d.inodes_percent))
        self.track.forget_missing(live)
        return out


class _ProcThreshold(_ThresholdRule):
    """Per-process threshold. Only processes near or over the threshold are tracked,
    so cost does not grow with the number of idle processes."""

    attr = ""
    what = ""

    def evaluate(self, s: Sample, now: float) -> list[Signal]:
        out = []
        live = set()
        for p in s.processes:
            v = getattr(p, self.attr)
            if v is None or p.status == psutil.STATUS_ZOMBIE:
                continue
            key = p.key
            live.add(key)
            if v <= self.cfg.clear_below and not self.track.tracked(key):
                continue
            st = self.track.update(key, v, now)
            out.append(self._sig(key, p.label, st, f"{p.name} (pid {p.pid}) {self.what}", v))
        self.track.forget_missing(live)
        return out


class ProcCpuHigh(_ProcThreshold):
    name, unit, attr, what = "proc_cpu_high", "%", "cpu_pct", "CPU of one core"


class ProcMemHigh(_ProcThreshold):
    name, unit, attr, what = "proc_mem_high", "%", "mem_pct", "RSS of total RAM"


# -- consecutive probe failures -------------------------------------------------
class ConsecutiveFailures(Rule):
    source = "probe"
    unit = "fails"

    def __init__(self, name: str, cfg: FailureRule, probes: Iterable[str]) -> None:
        super().__init__(cfg.severity)
        self.name = name
        self.needed = cfg.failures
        self.probes = tuple(probes)
        self.count: dict[str, int] = {}

    def adopt(self, key: str, subject: str) -> None:
        # Still failing after a restart keeps it open; the first success closes it.
        self.count[key] = max(self.count.get(key, 0), self.needed)

    def evaluate(self, rnd: ProbeRound, now: float) -> list[Signal]:
        out = []
        for name in self.probes:
            r = rnd.results.get(name)
            if r is None:
                continue
            subject = f"{name} {r.target}"
            if r.ok:
                self.count[name] = 0
                out.append(Signal(name, subject, State.CLEAR, 0, f"{name} {r.target} answering again"))
                continue
            n = self.count.get(name, 0) + 1
            self.count[name] = n
            st = State.FIRING if n >= self.needed else State.HOLD
            out.append(Signal(name, subject, st, float(n),
                              f"{name} {r.target} failed {n} probes in a row: {r.error}"))
        return out


# -- memory climbing -------------------------------------------------------------
@dataclass(slots=True)
class _Climb:
    history: deque
    last_t: float
    firing: bool = False


class MemoryClimbing(Rule):
    """RSS sampled once per `sample_interval` per process. Fires when RSS rose in at
    least `min_rises` of the last `window` intervals and by at least `min_growth_mb`
    over the window. Closes when the process exits or RSS has not grown over the
    last `flat_samples_to_clear` intervals."""

    name, unit = "memory_climbing", "MB"

    def __init__(self, cfg: MemoryClimbRule, tick: float) -> None:
        super().__init__(cfg.severity)
        self.cfg = cfg
        # Accept a sample up to half a tick early so a 60 s period on a 5 s
        # sampler stays at 60 s instead of drifting to 65 s.
        self.tolerance = tick / 2
        self.procs: dict[str, _Climb] = {}
        self._adopted: set[str] = set()

    def adopt(self, key: str, subject: str) -> None:
        self._adopted.add(key)

    def evaluate(self, s: Sample, now: float) -> list[Signal]:
        c = self.cfg
        out = []
        live = set()
        for p in s.processes:
            if p.rss is None or p.status == psutil.STATUS_ZOMBIE:
                continue
            key = p.key
            live.add(key)
            st = self.procs.get(key)
            if st is None:
                st = self.procs[key] = _Climb(deque([p.rss], maxlen=c.window + 1), now,
                                              firing=key in self._adopted)
                self._adopted.discard(key)
            elif now - st.last_t >= c.sample_interval - self.tolerance:
                st.history.append(p.rss)
                st.last_t = now
            h = st.history
            rises = sum(1 for a, b in itertools.pairwise(h) if b > a)
            growth_mb = (h[-1] - h[0]) / MB
            climbing = len(h) == c.window + 1 and rises >= c.min_rises and growth_mb >= c.min_growth_mb
            if climbing:
                st.firing = True
                msg = (f"{p.name} (pid {p.pid}) RSS +{growth_mb:.0f} MB over the last {c.window} samples, "
                       f"rising in {rises}/{c.window}; now {p.rss / MB:.0f} MB")
                out.append(Signal(key, p.label, State.FIRING, round(growth_mb, 1), msg))
            elif st.firing:
                k = c.flat_samples_to_clear
                flat = len(h) > k and h[-1] <= h[-1 - k]
                if flat:
                    st.firing = False
                    out.append(Signal(key, p.label, State.CLEAR, round(growth_mb, 1),
                                      f"RSS flat for {k} samples"))
                else:
                    out.append(Signal(key, p.label, State.HOLD, round(growth_mb, 1)))
        for k in [k for k in self.procs if k not in live]:
            del self.procs[k]
        return out


# -- zombies --------------------------------------------------------------------
class Zombie(Rule):
    name, unit = "zombie", "s"

    def __init__(self, min_age: float, severity: str) -> None:
        super().__init__(severity)
        self.min_age = min_age
        self.first_seen: dict[str, float] = {}

    def adopt(self, key: str, subject: str) -> None:
        self.first_seen.setdefault(key, float("-inf"))

    def evaluate(self, s: Sample, now: float) -> list[Signal]:
        out = []
        live = set()
        for p in s.processes:
            if p.status != psutil.STATUS_ZOMBIE:
                continue
            key = p.key
            live.add(key)
            first = self.first_seen.setdefault(key, now)
            age = now - first if first != float("-inf") else self.min_age + 1
            st = State.FIRING if age > self.min_age else State.HOLD
            out.append(Signal(key, p.label, st, round(age, 1),
                              f"{p.name} (pid {p.pid}) zombie for {age:.0f}s; parent has not reaped it"))
        for k in [k for k in self.first_seen if k not in live]:
            del self.first_seen[k]
        return out


# -- watch list -------------------------------------------------------------------
class ProcessDied(Rule):
    """Fires when a watch-list process that we saw running disappears (or turns into a
    zombie). Each death is its own alert, named with the PID. When a new instance of
    the same watch entry starts, the oldest open death for that entry closes: one
    restart answers one death, which also works for pools of workers."""

    name, unit = "process_died", "s"

    def __init__(self, watch: list[WatchEntry], severity: str, self_pid: int, missing_grace: float = 60.0) -> None:
        super().__init__(severity)
        self.watch = watch
        self.self_pid = self_pid
        self.missing_grace = missing_grace
        self.alive: dict[str, dict[str, ProcInfo]] = {w.name: {} for w in watch}
        self.dead: dict[str, OrderedDict[str, tuple[str, float]]] = {w.name: OrderedDict() for w in watch}
        # For required entries: when an instance was last seen, and which entries
        # currently have a "not running" alert.
        self.last_seen: dict[str, float | None] = {w.name: None for w in watch}
        self.missing: set[str] = set()
        self.started: float | None = None

    def adopt(self, key: str, subject: str) -> None:
        entry, _, rest = key.partition("|")
        if rest == "missing":
            self.missing.add(entry)
        elif entry in self.dead:
            self.dead[entry][key] = (subject, float("nan"))

    @staticmethod
    def matches(w: WatchEntry, p: ProcInfo) -> bool:
        return p.name == w.match if w.by == "name" else w.match in p.cmdline

    def evaluate(self, s: Sample, now: float) -> list[Signal]:
        out = []
        for w in self.watch:
            running = {p.key: p for p in s.processes
                       if p.pid != self.self_pid and p.status != psutil.STATUS_ZOMBIE and self.matches(w, p)}
            alive, dead = self.alive[w.name], self.dead[w.name]
            # Sorted so that simultaneous deaths and restarts pair up deterministically.
            for k in sorted(alive.keys() - running.keys(), key=lambda k: alive[k].pid):
                p = alive[k]
                status = "became a zombie" if any(q.key == k for q in s.processes) else "disappeared"
                dead[f"{w.name}|{k}"] = (f"{w.name} pid {p.pid} ({p.name})", now)
                dead.move_to_end(f"{w.name}|{k}")
                out.append(Signal(f"{w.name}|{k}", f"{w.name} pid {p.pid} ({p.name})", State.FIRING, 0.0,
                                  f"watched process {w.name} (pid {p.pid}, {p.name}) {status}"))
            for k in sorted(running.keys() - alive.keys(), key=lambda k: running[k].pid):
                if dead:
                    dkey, (subject, _) = dead.popitem(last=False)
                    out.append(Signal(dkey, subject, State.CLEAR, None,
                                      f"{w.name} running again as pid {running[k].pid}"))
            self.alive[w.name] = running
            reported = {sig.key for sig in out}
            for dkey, (subject, died_at) in dead.items():
                if dkey not in reported:
                    down = now - died_at if died_at == died_at else None  # NaN when adopted
                    out.append(Signal(dkey, subject, State.FIRING, round(down, 1) if down is not None else None))
            if w.required:
                out += self._required(w, running, dead, now)
        return out

    def _required(self, w: WatchEntry, running: dict[str, ProcInfo], dead: OrderedDict, now: float) -> list[Signal]:
        """A required entry with no instance at all. Skipped while a death alert for
        the entry is open: that alert already says the process is gone."""
        if self.started is None:
            self.started = now
        key, subject = f"{w.name}|missing", f"{w.name} (not running)"
        if running:
            self.last_seen[w.name] = now
            if w.name in self.missing:
                self.missing.discard(w.name)
                pid = min(p.pid for p in running.values())
                return [Signal(key, subject, State.CLEAR, None, f"{w.name} is running as pid {pid}")]
            return []
        if dead:
            return []
        seen = self.last_seen[w.name]
        gone_for = now - (seen if seen is not None else self.started)
        if w.name not in self.missing and gone_for < self.missing_grace:
            return []
        self.missing.add(w.name)
        when = f"last seen {gone_for:.0f}s ago" if seen is not None else "not seen since the agent started"
        return [Signal(key, subject, State.FIRING, round(gone_for, 1),
                       f"required process {w.name} is not running ({w.by} matching {w.match!r}); {when}")]


def build_rules(cfg: Config, self_pid: int) -> list[Rule]:
    r = cfg.rules
    gap = cfg.agent.sample_interval * 2.5
    rules: list[Rule] = []
    if r.cpu_high.enabled:
        rules.append(CpuHigh(r.cpu_high, gap))
    if r.ram_high.enabled:
        rules.append(RamHigh(r.ram_high, gap))
    if r.disk_high.enabled:
        rules.append(DiskHigh(r.disk_high, gap))
    if r.disk_inodes_high.enabled:
        rules.append(DiskInodesHigh(r.disk_inodes_high, gap))
    if r.proc_cpu_high.enabled:
        rules.append(ProcCpuHigh(r.proc_cpu_high, gap))
    if r.proc_mem_high.enabled:
        rules.append(ProcMemHigh(r.proc_mem_high, gap))
    if r.memory_climbing.enabled:
        rules.append(MemoryClimbing(r.memory_climbing, cfg.agent.sample_interval))
    if r.zombie.enabled:
        rules.append(Zombie(r.zombie.min_age, r.zombie.severity))
    if r.process_died.enabled and cfg.watch:
        rules.append(ProcessDied(cfg.watch, r.process_died.severity, self_pid, r.process_died.missing_grace))
    if r.network_lost.enabled:
        rules.append(ConsecutiveFailures("network_lost", r.network_lost, ("gateway", "internet")))
    if r.endpoint_down.enabled and cfg.probes.http_url:
        rules.append(ConsecutiveFailures("endpoint_down", r.endpoint_down, ("http",)))
    return rules
