"""CPU, load, uptime, memory, swap, disk usage and disk I/O."""

from __future__ import annotations

import logging
import os

import psutil

from ..config import CollectConfig
from ..deadline import DeadlineRunner
from ..models import DiskInfo, Metric
from ..rates import RateTracker

log = logging.getLogger(__name__)

STATVFS_TIMEOUT = 1.0


class SystemCollector:
    def __init__(self, cfg: CollectConfig) -> None:
        self.cfg = cfg
        self.rates = RateTracker()
        self._boot = psutil.boot_time()
        self._statvfs = DeadlineRunner(max_workers=2, name="statvfs")
        self._hung_mounts: set[str] = set()
        # psutil's cpu_percent(interval=None) measures since the previous call;
        # prime it so the first real sample covers one full interval.
        psutil.cpu_percent(percpu=True)
        psutil.cpu_percent()
        psutil.cpu_times_percent()
        if not hasattr(os, "getloadavg"):
            psutil.getloadavg()  # starts the Windows load emulation thread

    def collect(self, ts: float, out: list[Metric]) -> tuple[float, float, int, list[DiskInfo]]:
        cpu_total = self._cpu(out)
        mem_percent, mem_total = self._memory(out)
        disks = self._disks(out)
        self._disk_io(ts, out)
        self.rates.sweep()
        out.append(Metric("system.uptime", "", ts - self._boot, "s"))
        return cpu_total, mem_percent, mem_total, disks

    # -- CPU -----------------------------------------------------------------
    def _cpu(self, out: list[Metric]) -> float:
        for i, pct in enumerate(psutil.cpu_percent(percpu=True)):
            out.append(Metric("cpu.core", str(i), pct, "%"))
        total = psutil.cpu_percent()
        out.append(Metric("cpu.total", "", total, "%"))
        times = psutil.cpu_times_percent()
        for field in ("iowait", "steal"):
            if hasattr(times, field):
                out.append(Metric(f"cpu.{field}", "", getattr(times, field), "%"))
        try:
            l1, l5, l15 = os.getloadavg() if hasattr(os, "getloadavg") else psutil.getloadavg()
            out += [Metric("load.1", "", l1), Metric("load.5", "", l5), Metric("load.15", "", l15)]
        except OSError:
            pass
        return total

    # -- Memory --------------------------------------------------------------
    def _memory(self, out: list[Metric]) -> tuple[float, int]:
        vm = psutil.virtual_memory()
        # psutil's percent is (total - available) / total: page cache the kernel can
        # reclaim is not counted as used, which is what matters for OOM risk.
        out += [
            Metric("mem.percent", "", vm.percent, "%"),
            Metric("mem.used", "", float(vm.total - vm.available), "B"),
            Metric("mem.available", "", float(vm.available), "B"),
        ]
        sw = psutil.swap_memory()
        out += [Metric("swap.percent", "", sw.percent, "%"), Metric("swap.used", "", float(sw.used), "B")]
        return vm.percent, vm.total

    # -- Disk usage ----------------------------------------------------------
    def _mounts(self) -> list[tuple[str, str, str]]:
        seen: dict[str, tuple[str, str, str]] = {}
        for p in psutil.disk_partitions(all=True):
            if p.fstype in self.cfg.exclude_fstypes or not p.fstype:
                continue
            if "cdrom" in p.opts:
                continue
            mp = p.mountpoint
            if any(mp == pre or mp.startswith(pre.rstrip("/") + "/") for pre in self.cfg.exclude_mount_prefixes):
                continue
            seen[mp] = (mp, p.device, p.fstype)  # later mounts shadow earlier ones
        return list(seen.values())

    def _disks(self, out: list[Metric]) -> list[DiskInfo]:
        disks: list[DiskInfo] = []
        for mount, device, fstype in self._mounts():
            try:
                info = self._statvfs.run(mount, _usage, mount, device, fstype, timeout=STATVFS_TIMEOUT)
            except Exception as exc:  # TimeoutError, Hung, PermissionError, OSError
                if mount not in self._hung_mounts:
                    log.warning("disk usage for %s unavailable: %s", mount, str(exc) or type(exc).__name__)
                    self._hung_mounts.add(mount)
                continue
            self._hung_mounts.discard(mount)
            if info is None:
                continue
            disks.append(info)
            out += [
                Metric("disk.percent", mount, info.percent, "%"),
                Metric("disk.used", mount, float(info.used), "B"),
                Metric("disk.free", mount, float(info.free), "B"),
            ]
            if info.inodes_percent is not None:
                out.append(Metric("disk.inodes_percent", mount, info.inodes_percent, "%"))
        return disks

    # -- Disk I/O ------------------------------------------------------------
    def _disk_io(self, ts: float, out: list[Metric]) -> None:
        try:
            counters = psutil.disk_io_counters(perdisk=True, nowrap=False) or {}
        except Exception:  # no /proc/diskstats in some containers
            return
        for dev, c in counters.items():
            if any(dev.startswith(p) for p in self.cfg.exclude_disk_prefixes):
                continue
            r = self.rates
            for name, value, unit in (
                ("diskio.read_bps", c.read_bytes, "B/s"),
                ("diskio.write_bps", c.write_bytes, "B/s"),
                ("diskio.read_iops", c.read_count, "/s"),
                ("diskio.write_iops", c.write_count, "/s"),
            ):
                v = r.rate((name, dev), value, ts)
                if v is not None:
                    out.append(Metric(name, dev, v, unit))
            busy = getattr(c, "busy_time", None)  # ms, Linux only
            if busy is not None:
                v = r.rate(("diskio.busy", dev), busy, ts)
                if v is not None:
                    out.append(Metric("diskio.busy_pct", dev, min(100.0, v / 10.0), "%"))

    def close(self) -> None:
        self._statvfs.shutdown()


def _usage(mount: str, device: str, fstype: str) -> DiskInfo | None:
    if hasattr(os, "statvfs"):
        st = os.statvfs(mount)
        if st.f_blocks == 0:
            return None  # pseudo filesystem with no size
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        # Same formula as df: blocks reserved for root count as neither used nor free.
        percent = used / (used + free) * 100 if used + free else 0.0
        # Some filesystems (btrfs, 9p, FUSE) report no or nonsensical inode counts.
        inodes = (round((st.f_files - st.f_ffree) / st.f_files * 100, 2)
                  if st.f_files and 0 <= st.f_ffree <= st.f_files else None)
        return DiskInfo(mount, device, fstype, total, used, free, round(percent, 2), inodes)
    u = psutil.disk_usage(mount)
    if u.total == 0:
        return None
    return DiskInfo(mount, device, fstype, u.total, u.used, u.free, u.percent, None)
