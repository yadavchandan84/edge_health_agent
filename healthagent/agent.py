"""The agent: three loops on their own threads plus the HTTP server.

  sampler      every sample_interval (5 s): collect -> rules -> one DB transaction
  prober       every probe_interval (15 s): network probes -> rules -> DB
  maintenance  every 60 s: roll raw data up into 1-minute buckets; every 10 min prune

The loops never share a blocking call, so a hung DNS lookup in the prober cannot
delay a sample. Each loop is scheduled on the monotonic clock against a fixed grid,
so ticks do not drift, and an overrun skips ticks instead of bunching them up.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from typing import Any

import psutil

from . import __version__
from .alerts import AlertManager
from .collectors.network import NetworkCollector
from .collectors.processes import ProcessCollector
from .collectors.system import SystemCollector
from .config import Config
from .models import Metric, ProbeRound, Sample
from .notify import WebhookNotifier
from .probes import Prober, probe_metrics
from .rules import build_rules
from .storage import Storage

log = logging.getLogger("healthagent")
MB = 1024 * 1024


class Agent:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.started = time.time()
        self.stop_event = threading.Event()
        self.db = Storage(cfg.agent.db_path)
        notify = bool(cfg.notify.webhook_url)
        self.alerts = AlertManager(self.db, source=cfg.agent.name, outbox=notify)
        self.notifier = WebhookNotifier(cfg.notify, self.db) if notify else None
        self.rules = build_rules(cfg, os.getpid())
        self.sample_rules = [r for r in self.rules if r.source == "sample"]
        self.probe_rules = [r for r in self.rules if r.source == "probe"]
        self.alerts.adopt_into(self.rules)

        self.system = SystemCollector(cfg.collect)
        self.network = NetworkCollector(cfg.collect)
        self.processes = ProcessCollector()
        self.prober = Prober(cfg.probes)

        self.me = psutil.Process()
        self.me.cpu_percent()
        self.latest: Sample | None = None
        self.latest_probes: ProbeRound | None = None
        self.last_sample_mono: float | None = None
        self.last_probe_mono: float | None = None
        self.stats: dict[str, Any] = {"cpu_pct": 0.0, "rss_mb": 0.0, "loop_ms": 0.0, "db_mb": 0.0,
                                      "overruns": 0, "errors": 0}
        self._threads: list[threading.Thread] = []
        self._last_prune = 0.0

    # -- loops -----------------------------------------------------------------
    def _every(self, interval: float, fn: Callable[[], None], name: str) -> None:
        next_t = time.monotonic()
        while not self.stop_event.is_set():
            try:
                fn()
            except Exception:  # keep the agent alive; the error is logged and counted
                self.stats["errors"] += 1
                log.exception("%s tick failed", name)
            next_t += interval
            delay = next_t - time.monotonic()
            if delay < 0:
                skipped = int(-delay // interval) + 1
                self.stats["overruns"] += skipped
                log.warning("%s overran by %.1fs, skipping %d tick(s)", name, -delay, skipped)
                next_t += skipped * interval
                delay = next_t - time.monotonic()
            self.stop_event.wait(max(0.0, delay))

    def sample_once(self) -> Sample:
        t0 = time.perf_counter()
        ts = time.time()
        metrics: list[Metric] = []
        cpu, mem, mem_total, disks = self.system.collect(ts, metrics)
        listening, interfaces = self.network.collect(ts, metrics)
        procs = self.processes.collect(mem_total, metrics)
        sample = Sample(ts=ts, metrics=metrics, cpu_total=cpu, mem_percent=mem, mem_total=mem_total,
                        disks=disks, processes=procs, listening=listening, interfaces=interfaces)
        for rule in self.sample_rules:
            self.alerts.apply(rule, rule.evaluate(sample, ts), ts)

        # Self-monitoring: the budget is 5 % of one core and 150 MB RSS.
        st = self.stats
        cpu_pct = self.me.cpu_percent()  # percent of one core since the last tick
        st["rss_mb"] = self.me.memory_info().rss / MB
        st["db_mb"] = self.db.size_bytes() / MB
        st["loop_ms"] = (time.perf_counter() - t0) * 1000
        if self.last_sample_mono is not None:
            # The first reading covers startup (imports, schema, first full process
            # scan); it says nothing about steady-state cost, so it is not recorded.
            st["cpu_pct"] = cpu_pct
            metrics.append(Metric("agent.cpu_pct", "", cpu_pct, "%"))
        metrics += [
            Metric("agent.rss_mb", "", st["rss_mb"], "MB"),
            Metric("agent.loop_ms", "", st["loop_ms"], "ms"),
            Metric("agent.db_mb", "", st["db_mb"], "MB"),
            Metric("agent.threads", "", float(threading.active_count())),
            Metric("alerts.open", "", float(len(self.alerts.open))),
        ]
        if self.notifier:
            metrics.append(Metric("notify.pending", "", float(self.db.outbox_stats()[0])))
        self.db.write_metrics(ts, metrics)
        self.latest = sample
        self.last_sample_mono = time.monotonic()
        return sample

    def probe_once(self) -> ProbeRound:
        rnd = self.prober.run_round()
        for rule in self.probe_rules:
            self.alerts.apply(rule, rule.evaluate(rnd, rnd.ts), rnd.ts)
        inet = rnd.results.get("internet")
        if self.notifier and inet is not None:
            prev = self.latest_probes.results.get("internet") if self.latest_probes else None
            if inet.ok and prev is not None and not prev.ok:
                self.notifier.retry_now()
        metrics = probe_metrics(rnd)
        metrics.append(Metric("agent.stuck_probes", "", float(len(self.prober.runner.stuck()))))
        self.db.write_metrics(rnd.ts, metrics)
        self.latest_probes = rnd
        self.last_probe_mono = time.monotonic()
        failed = [f"{n}: {r.error}" for n, r in rnd.results.items() if not r.ok]
        if failed:
            log.info("probe failures: %s", "; ".join(failed))
        return rnd

    def maintain_once(self) -> None:
        now = time.time()
        self.db.rollup(now)
        if now - self._last_prune >= 600:
            r = self.cfg.retention
            removed = self.db.prune(now, r.raw_hours * 3600, r.rollup_days * 86400, r.closed_alert_days * 86400,
                                    self.cfg.notify.outbox_days * 86400)
            self._last_prune = now
            if any(removed.values()):
                log.info("retention: removed %s", removed)

    # -- health ----------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        now = time.time()
        mono = time.monotonic()
        open_alerts = self.alerts.snapshot()
        status = self.alerts.status()
        problems = []
        sample_age = mono - self.last_sample_mono if self.last_sample_mono else None
        probe_age = mono - self.last_probe_mono if self.last_probe_mono else None
        if sample_age is None or sample_age > self.cfg.agent.sample_interval * 3:
            if now - self.started > self.cfg.agent.sample_interval * 3:
                problems.append("sampler stalled")
                status = "critical"
        if probe_age is not None and probe_age > self.cfg.agent.probe_interval * 3:
            problems.append("prober stalled")
            if status == "ok":
                status = "warn"
        s = self.latest
        return {
            "status": status,
            "checked_at": now,
            "problems": problems,
            "open_alerts": open_alerts,
            "summary": {
                "cpu_pct": s.cpu_total if s else None,
                "mem_pct": s.mem_percent if s else None,
                "disks": {d.mount: d.percent for d in s.disks} if s else {},
                "network": {n: r.ok for n, r in self.latest_probes.results.items()} if self.latest_probes else {},
            },
            "agent": {
                "name": self.cfg.agent.name,
                "version": __version__,
                "pid": os.getpid(),
                "uptime_s": round(now - self.started, 1),
                "cpu_pct": round(self.stats["cpu_pct"], 2),
                "rss_mb": round(self.stats["rss_mb"], 1),
                "db_mb": round(self.stats["db_mb"], 2),
                "loop_ms": round(self.stats["loop_ms"], 1),
                "last_sample_age_s": round(sample_age, 1) if sample_age is not None else None,
                "last_probe_age_s": round(probe_age, 1) if probe_age is not None else None,
                "stuck_probes": self.prober.runner.stuck(),
                "overruns": self.stats["overruns"],
                "errors": self.stats["errors"],
                "notify": self.notifier.status(now) if self.notifier else {"enabled": False},
            },
        }

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        a = self.cfg.agent
        loops: list[tuple[str, float, Callable[[], Any]]] = [
            ("sampler", a.sample_interval, self.sample_once),
            ("prober", a.probe_interval, self.probe_once),
            ("maintenance", 60.0, self.maintain_once),
        ]
        if self.notifier:
            loops.append(("notifier", 5.0, self.notifier.tick))
        for name, interval, fn in loops:
            t = threading.Thread(target=self._every, args=(interval, fn, name), name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def run_forever(self) -> None:
        from .api import serve  # local import keeps the agent usable without the API

        # Bind first: if the port is taken (another agent is running), fail before
        # touching signal handlers or starting any thread.
        server = serve(self, self.cfg.api.host, self.cfg.api.port)

        def _stop(signum: int, _frame: Any) -> None:
            log.info("signal %s: shutting down", signum)
            self.stop_event.set()

        signal.signal(signal.SIGINT, _stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _stop)

        log.info("healthagent %s pid %d: dashboard on http://%s:%d/  db=%s  rules=%s",
                 __version__, os.getpid(), self.cfg.api.host, self.cfg.api.port, self.db.path,
                 ",".join(r.name for r in self.rules))
        self.start()
        try:
            while not self.stop_event.wait(1.0):
                pass
        finally:
            server.shutdown()
            for t in self._threads:
                t.join(timeout=self.cfg.agent.probe_interval)
            self.prober.close()
            self.system.close()
            if self.notifier:
                self.notifier.close()
            self.db.close()
            log.info("stopped")
