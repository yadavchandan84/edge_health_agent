"""Per-process CPU, memory, file descriptors and state.

CPU % is computed from cpu_times deltas between our own ticks, as percent of one
core, so it is deterministic and does not depend on psutil's hidden per-object state.
"""

from __future__ import annotations

import time

import psutil

from ..models import Metric, ProcInfo

ATTRS = ["pid", "name", "status", "cpu_times", "memory_info", "create_time", "num_threads", "username"]
if psutil.WINDOWS:
    # Windows is only a dev platform here. There, status is always "running" and both
    # status and num_threads cost a full system process snapshot per process.
    # Linux reads them from /proc/<pid>/stat cheaply.
    ATTRS = [a for a in ATTRS if a not in ("status", "num_threads")]
# Open file descriptors are a POSIX notion; Windows handle counts are too costly to poll.
FD_ATTR = "num_fds" if hasattr(psutil.Process, "num_fds") else None


class ProcessCollector:
    def __init__(self) -> None:
        self._prev: dict[int, tuple[float, float, float]] = {}  # pid -> (ctime, cpu_s, mono ts)
        self._cmdline: dict[tuple[int, float], str] = {}
        self._attrs = ATTRS + ([FD_ATTR] if FD_ATTR else [])

    def collect(self, mem_total: int, out: list[Metric]) -> list[ProcInfo]:
        procs: list[ProcInfo] = []
        prev, nxt = self._prev, {}
        zombies = threads = fds_total = 0
        live_keys: set[tuple[int, float]] = set()
        ts = time.monotonic()  # wall clock can jump (NTP); deltas use monotonic time
        for p in psutil.process_iter(self._attrs, ad_value=None):
            info = p.info
            pid = info["pid"]
            if pid == 0:  # kernel idle task / "System Idle Process": not a real process
                continue
            ctime = info["create_time"] or 0.0
            status = info.get("status") or psutil.STATUS_RUNNING
            cpu_pct = None
            ct = info["cpu_times"]
            if ct is not None:
                cpu_s = ct.user + ct.system
                last = prev.get(pid)
                if last and last[0] == ctime and ts > last[2]:
                    cpu_pct = max(0.0, (cpu_s - last[1]) / (ts - last[2]) * 100.0)
                nxt[pid] = (ctime, cpu_s, ts)
            mi = info["memory_info"]
            rss = mi.rss if mi is not None else None
            key = (pid, ctime)
            live_keys.add(key)
            cmd = self._cmdline.get(key)
            if cmd is None:  # cmdline never changes for a process: read it once
                cmd = _cmdline(p)
                self._cmdline[key] = cmd
            fds = info.get(FD_ATTR) if FD_ATTR else None
            nthreads = info.get("num_threads")
            if status == psutil.STATUS_ZOMBIE:
                zombies += 1
            threads += nthreads or 0
            fds_total += fds or 0
            procs.append(ProcInfo(
                pid=pid, ctime=ctime, name=info["name"] or "?", cmdline=cmd,
                username=info["username"], status=status,
                cpu_pct=round(cpu_pct, 2) if cpu_pct is not None else None,
                rss=rss, mem_pct=round(rss / mem_total * 100, 2) if rss is not None and mem_total else None,
                fds=fds, threads=nthreads,
            ))
        self._prev = nxt
        if len(self._cmdline) > len(live_keys):  # keep the cache bounded by live processes
            self._cmdline = {k: v for k, v in self._cmdline.items() if k in live_keys}
        out += [
            Metric("proc.count", "", float(len(procs))),
            Metric("proc.zombies", "", float(zombies)),
        ]
        if "num_threads" in self._attrs:
            out.append(Metric("proc.threads", "", float(threads)))
        if FD_ATTR:
            out.append(Metric("proc.fds", "", float(fds_total)))
        return procs


def _cmdline(p: psutil.Process) -> str:
    try:
        parts = p.cmdline()
    except psutil.Error:
        return ""
    return " ".join(parts)[:500]
