"""Plain data passed between collectors, rules, storage and the API."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Metric:
    name: str
    subject: str
    value: float
    unit: str = ""


@dataclass(slots=True)
class DiskInfo:
    mount: str
    device: str
    fstype: str
    total: int
    used: int
    free: int
    percent: float
    inodes_percent: float | None


@dataclass(slots=True)
class ProcInfo:
    pid: int
    ctime: float
    name: str
    cmdline: str
    username: str | None
    status: str
    cpu_pct: float | None  # percent of one core over the last interval
    rss: int | None
    mem_pct: float | None
    fds: int | None
    threads: int | None

    @property
    def key(self) -> str:
        # (pid, start time) survives PID reuse; a bare PID does not.
        return f"{self.pid}@{self.ctime:.2f}"

    @property
    def label(self) -> str:
        return f"pid {self.pid} ({self.name})"


@dataclass
class Sample:
    """One 5-second tick of everything the collectors saw."""

    ts: float
    metrics: list[Metric] = field(default_factory=list)
    cpu_total: float | None = None
    mem_percent: float | None = None
    mem_total: int = 0
    disks: list[DiskInfo] = field(default_factory=list)
    processes: list[ProcInfo] = field(default_factory=list)
    listening: list[dict] = field(default_factory=list)
    interfaces: list[dict] = field(default_factory=list)


@dataclass(slots=True)
class ProbeResult:
    name: str  # dns, tcp, gateway, internet, http
    target: str
    ok: bool
    value_ms: float | None = None
    loss_pct: float | None = None
    status: int | None = None
    error: str | None = None


@dataclass
class ProbeRound:
    ts: float
    results: dict[str, ProbeResult] = field(default_factory=dict)
