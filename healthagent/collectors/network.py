"""Per-interface bandwidth, drops and errors; TCP states; listening ports.

On Linux, sockets are read straight from /proc/net/{tcp,tcp6,udp,udp6}. That is far
cheaper than psutil.net_connections(), which walks every /proc/<pid>/fd on every
call. Mapping listening sockets to processes does need that walk, so it is done
at most once a minute and only for listening sockets.
"""

from __future__ import annotations

import os
import socket
import struct
import sys
import time

import psutil

from ..config import CollectConfig
from ..models import Metric
from ..rates import RateTracker

TCP_STATES = {
    "01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1",
    "05": "FIN_WAIT2", "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT",
    "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING",
}
REPORTED_STATES = ("ESTABLISHED", "TIME_WAIT", "CLOSE_WAIT", "SYN_SENT", "LISTEN")
OWNER_REFRESH_S = 60.0


def _decode_addr(hexaddr: str) -> tuple[str, int]:
    ip_hex, port_hex = hexaddr.split(":")
    raw = bytes.fromhex(ip_hex)
    if len(raw) == 4:
        ip = socket.inet_ntop(socket.AF_INET, raw[::-1])
    else:
        # IPv6 is stored as four 32-bit words, each in host (little-endian) order.
        words = struct.unpack("<4I", raw)
        ip = socket.inet_ntop(socket.AF_INET6, struct.pack(">4I", *words))
    return ip, int(port_hex, 16)


class NetworkCollector:
    def __init__(self, cfg: CollectConfig) -> None:
        self.cfg = cfg
        self.rates = RateTracker()
        self._linux = sys.platform.startswith("linux") and os.path.exists("/proc/net/tcp")
        self._owners: dict[int, tuple[int, str]] = {}  # socket inode -> (pid, name)
        self._owners_at = 0.0

    def collect(self, ts: float, out: list[Metric]) -> tuple[list[dict], list[dict]]:
        interfaces = self._interfaces(ts, out)
        listening = self._sockets(out)
        self.rates.sweep()
        return listening, interfaces

    # -- Interfaces ----------------------------------------------------------
    def _interfaces(self, ts: float, out: list[Metric]) -> list[dict]:
        counters = psutil.net_io_counters(pernic=True, nowrap=False)
        try:
            stats = psutil.net_if_stats()
        except OSError:
            stats = {}
        result = []
        r = self.rates
        for nic, c in counters.items():
            if nic in self.cfg.exclude_interfaces:
                continue
            row: dict = {"name": nic, "up": bool(stats[nic].isup) if nic in stats else None}
            rx = r.rate((nic, "rx"), c.bytes_recv, ts)
            tx = r.rate((nic, "tx"), c.bytes_sent, ts)
            if rx is not None:
                out.append(Metric("net.rx_bps", nic, rx, "B/s"))
                row["rx_bps"] = rx
            if tx is not None:
                out.append(Metric("net.tx_bps", nic, tx, "B/s"))
                row["tx_bps"] = tx
            # Drops and errors are reported as counts per sample interval.
            for name, value in (
                ("net.rx_drops", c.dropin), ("net.tx_drops", c.dropout),
                ("net.rx_errors", c.errin), ("net.tx_errors", c.errout),
            ):
                d = r.delta((nic, name), value, ts)
                if d is not None:
                    out.append(Metric(name, nic, d, "count"))
                    row[name.split(".")[1]] = d
            row.update(drops_total=c.dropin + c.dropout, errors_total=c.errin + c.errout)
            if row["up"] is not None:
                out.append(Metric("net.up", nic, 1.0 if row["up"] else 0.0))
            result.append(row)
        return result

    # -- Sockets -------------------------------------------------------------
    def _sockets(self, out: list[Metric]) -> list[dict]:
        if self._linux:
            states, listening = self._sockets_proc()
        else:
            states, listening = self._sockets_psutil()
        for st in REPORTED_STATES:
            out.append(Metric("tcp.connections", st, float(states.get(st, 0))))
        out.append(Metric("tcp.total", "", float(sum(states.values()))))
        out.append(Metric("ports.listening", "", float(len(listening))))
        listening.sort(key=lambda x: (x["proto"], x["port"]))
        return listening

    def _sockets_proc(self) -> tuple[dict[str, int], list[dict]]:
        states: dict[str, int] = {}
        listening: list[dict] = []
        wanted: set[int] = set()
        for proto in ("tcp", "tcp6", "udp", "udp6"):
            try:
                with open(f"/proc/net/{proto}") as fh:
                    next(fh, None)
                    for line in fh:
                        parts = line.split()
                        if len(parts) < 10:
                            continue
                        st = parts[3]
                        if proto.startswith("tcp"):
                            name = TCP_STATES.get(st, st)
                            states[name] = states.get(name, 0) + 1
                            is_listen = st == "0A"
                        else:
                            is_listen = st == "07" and parts[2].endswith(":0000")  # bound, unconnected
                        if is_listen:
                            ip, port = _decode_addr(parts[1])
                            inode = int(parts[9])
                            wanted.add(inode)
                            listening.append({"proto": proto, "addr": ip, "port": port, "inode": inode})
            except FileNotFoundError:
                continue
        self._refresh_owners(wanted)
        for row in listening:
            owner = self._owners.get(row.pop("inode"))
            row["pid"], row["process"] = owner if owner else (None, None)
        return states, _dedupe(listening)

    def _refresh_owners(self, wanted: set[int]) -> None:
        # New listening socket with unknown owner: rescan at most once a minute.
        # Nothing new: rescan every 5 minutes. Without root we only see our own
        # processes' fds, so some owners stay unknown; that is expected.
        now = time.monotonic()
        age = now - self._owners_at
        known = wanted.issubset(self._owners)
        if self._owners_at and (age < OWNER_REFRESH_S or (known and age < OWNER_REFRESH_S * 5)):
            return
        self._owners_at = now
        owners: dict[int, tuple[int, str]] = {}
        for pid_s in os.listdir("/proc"):
            if not pid_s.isdigit():
                continue
            fd_dir = f"/proc/{pid_s}/fd"
            try:
                for fd in os.listdir(fd_dir):
                    try:
                        target = os.readlink(f"{fd_dir}/{fd}")
                    except OSError:
                        continue
                    if target.startswith("socket:["):
                        inode = int(target[8:-1])
                        if inode in wanted:
                            with open(f"/proc/{pid_s}/comm") as fh:
                                owners[inode] = (int(pid_s), fh.read().strip())
            except (PermissionError, FileNotFoundError, ProcessLookupError, NotADirectoryError):
                continue
        self._owners = owners

    def _sockets_psutil(self) -> tuple[dict[str, int], list[dict]]:
        states: dict[str, int] = {}
        listening: list[dict] = []
        try:
            conns = psutil.net_connections(kind="inet")
        except (psutil.AccessDenied, OSError):
            return states, listening
        names: dict[int, str] = {}
        for c in conns:
            tcp = c.type == socket.SOCK_STREAM
            if tcp:
                states[c.status] = states.get(c.status, 0) + 1
            if (tcp and c.status == psutil.CONN_LISTEN) or (not tcp and not c.raddr):
                if not c.laddr:
                    continue
                name = None
                if c.pid:
                    if c.pid not in names:
                        try:
                            names[c.pid] = psutil.Process(c.pid).name()
                        except psutil.Error:
                            names[c.pid] = "?"
                    name = names[c.pid]
                proto = ("tcp" if tcp else "udp") + ("6" if c.family == socket.AF_INET6 else "")
                listening.append({"proto": proto, "addr": c.laddr.ip, "port": c.laddr.port,
                                  "pid": c.pid, "process": name})
        return states, _dedupe(listening)


def _dedupe(rows: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for r in rows:
        k = (r["proto"], r["addr"], r["port"])
        if k not in seen:
            seen.add(k)
            result.append(r)
    return result
