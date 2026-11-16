"""Shared helpers: build samples and probe rounds by hand and replay them through the
real rules, alert manager and SQLite storage with a fake clock."""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from healthagent.alerts import AlertManager
from healthagent.config import Config, WatchEntry, load_config
from healthagent.models import DiskInfo, ProbeResult, ProbeRound, ProcInfo, Sample
from healthagent.rules import Rule, build_rules
from healthagent.storage import Storage

MB = 1024 * 1024
TOTAL_RAM = 8000 * MB


def proc(pid: int, rss_mb: float = 100, cpu: float | None = 1.0, status: str = "running",
         cmdline: str = "x", name: str = "p", ctime: float | None = None) -> ProcInfo:
    rss = int(rss_mb * MB)
    return ProcInfo(pid=pid, ctime=1000.0 + pid if ctime is None else ctime, name=name, cmdline=cmdline,
                    username="u", status=status, cpu_pct=cpu, rss=rss, mem_pct=rss / TOTAL_RAM * 100,
                    fds=3, threads=1)


def disk(mount: str, percent: float, inodes: float | None = 1.0, fstype: str = "ext4") -> DiskInfo:
    total = 100 * MB
    used = int(total * percent / 100)
    return DiskInfo(mount, "/dev/x", fstype, total, used, total - used, percent, inodes)


def sample(t: float, cpu: float = 10, mem: float = 50, disks: Iterable[DiskInfo] = (),
           procs: Iterable[ProcInfo] = ()) -> Sample:
    return Sample(ts=t, cpu_total=cpu, mem_percent=mem, mem_total=TOTAL_RAM, disks=list(disks),
                  processes=list(procs))


def probes(t: float, **ok: bool) -> ProbeRound:
    """probes(15, gateway=False, internet=True)"""
    targets = {"gateway": "10.0.0.1", "internet": "1.1.1.1", "http": "https://api.example/health",
               "dns": "example.com", "tcp": "example.com:443"}
    rnd = ProbeRound(ts=t)
    for name, good in ok.items():
        rnd.results[name] = ProbeResult(name, targets[name], good, value_ms=5.0 if good else None,
                                        loss_pct=(0.0 if good else 100.0) if name in ("gateway", "internet") else None,
                                        status=200 if good and name == "http" else None,
                                        error=None if good else "no reply")
    return rnd


class Harness:
    """Real rules + AlertManager + Storage; feed data, read alerts back from SQLite."""

    def __init__(self, cfg: Config, db_path: str, outbox: bool = False) -> None:
        self.cfg = cfg
        self.db_path = db_path
        self.db = Storage(db_path)
        self.outbox = outbox
        self.restart()

    def restart(self) -> None:
        """Simulate an agent restart: fresh rules and manager, same database."""
        self.alerts = AlertManager(self.db, source="test-box", outbox=self.outbox)
        self.rules: dict[str, Rule] = {r.name: r for r in build_rules(self.cfg, self_pid=1)}
        self.alerts.adopt_into(list(self.rules.values()))

    def feed(self, rule: str, data: Sample | ProbeRound, t: float) -> None:
        r = self.rules[rule]
        self.alerts.apply(r, r.evaluate(data, t), t)

    def all(self, rule: str) -> list[dict]:
        return sorted(self.db.alerts(None, 1000, rule), key=lambda a: a["id"])

    def open(self, rule: str) -> list[dict]:
        return [a for a in self.all(rule) if a["closed_at"] is None]


@pytest.fixture
def cfg() -> Config:
    c = load_config(None)
    c.watch = [WatchEntry("camera-sim", "watched_service.py", "cmdline")]
    return c


@pytest.fixture
def h(cfg: Config, tmp_path) -> Harness:
    return Harness(cfg, str(tmp_path / "t.db"))
