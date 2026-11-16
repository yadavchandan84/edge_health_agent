"""Network probes: DNS, TCP connect, ping (gateway + internet), HTTP endpoint.

Every probe runs on a worker of a DeadlineRunner and the prober stops waiting after
the probe's timeout. A lookup that is still hung on the next round is not started
again (it reports "still hung"), so a dead resolver costs one stuck thread, not one
per round. The prober has its own thread, so none of this can delay the 5 s sampler.
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import __version__
from .config import ProbeConfig
from .deadline import DeadlineRunner, Hung
from .models import Metric, ProbeResult, ProbeRound

log = logging.getLogger(__name__)
WINDOWS = sys.platform.startswith("win")


# -- default gateway -----------------------------------------------------------
def parse_proc_route(text: str) -> str | None:
    """Default gateway from /proc/net/route: destination 0.0.0.0 with the RTF_GATEWAY
    flag, lowest metric wins. Addresses are little-endian hex."""
    best: tuple[int, str] | None = None
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 8 or f[1] != "00000000" or not int(f[3], 16) & 0x2:
            continue
        gw = socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
        metric = int(f[6])
        if best is None or metric < best[0]:
            best = (metric, gw)
    return best[1] if best else None


def default_gateway() -> str | None:
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/net/route") as fh:
                return parse_proc_route(fh.read())
        except OSError:
            return None
    try:
        if WINDOWS:
            out = subprocess.run(["route", "print", "-4", "0.0.0.0"], capture_output=True,
                                 text=True, timeout=3).stdout
            m = re.search(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)", out, re.M)
        else:
            out = subprocess.run(["route", "-n", "get", "default"], capture_output=True,
                                 text=True, timeout=3).stdout
            m = re.search(r"gateway:\s*(\S+)", out)
        return m.group(1) if m else None
    except (OSError, subprocess.SubprocessError):
        return None


# -- individual probes (run on worker threads) ---------------------------------
def _resolve(host: str, port: int) -> tuple[float, str, int]:
    t0 = time.perf_counter()
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ms = (time.perf_counter() - t0) * 1000
    family, _, _, _, addr = infos[0]
    return ms, addr[0], family


def _tcp_connect(ip: str, family: int, port: int, timeout: float) -> float:
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        t0 = time.perf_counter()
        s.connect((ip, port))
        return (time.perf_counter() - t0) * 1000


def _ping(host: str, count: int, timeout: float) -> tuple[float, float | None]:
    """Return (loss %, avg rtt ms). Uses the system ping so no root is needed."""
    if WINDOWS:
        cmd = ["ping", "-n", str(count), "-w", str(int(timeout * 1000)), host]
    else:
        cmd = ["ping", "-n", "-q", "-c", str(count), "-i", "0.2", "-W", str(max(1, int(timeout))), host]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=count * (timeout + 0.5) + 2)
    return parse_ping(proc.stdout, count, WINDOWS)


def parse_ping(out: str, count: int, windows: bool) -> tuple[float, float | None]:
    """(loss %, avg rtt ms) from ping output. No output at all (e.g. "Network is
    unreachable" goes to stderr) means nothing came back: 100 % loss."""
    if windows:
        # Windows counts "Destination host unreachable" as a reply; count real echoes.
        received = len(re.findall(r"TTL=", out, re.I))
        m = re.search(r"Average = (\d+)ms", out)
        rtt = float(m.group(1)) if m and received else None
    else:
        m = re.search(r"(\d+) (?:packets )?received", out)
        received = int(m.group(1)) if m else 0
        m = re.search(r"= [\d.]+/([\d.]+)/", out)
        rtt = float(m.group(1)) if m and received else None
    received = min(received, count)
    return (count - received) / count * 100.0, rtt


def _http(url: str, timeout: float) -> tuple[float, int]:
    req = urllib.request.Request(url, headers={"User-Agent": f"zeroforg-healthagent/{__version__}"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(1024)
            status = resp.status
    except urllib.error.HTTPError as e:  # 4xx/5xx still means the endpoint answered
        status = e.code
    return (time.perf_counter() - t0) * 1000, status


def _err(exc: BaseException, timeout: float) -> str:
    if isinstance(exc, (cf.TimeoutError, TimeoutError, socket.timeout, subprocess.TimeoutExpired)):
        return f"timeout after {timeout:g}s"
    if isinstance(exc, Hung):
        return "previous attempt still hung"
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    return str(exc) or type(exc).__name__


class Prober:
    def __init__(self, cfg: ProbeConfig) -> None:
        self.cfg = cfg
        self.runner = DeadlineRunner(max_workers=8, name="probe")
        self._last_ip: tuple[str, int] | None = None
        self._gateway: str | None = None
        self._has_ping = shutil.which("ping") is not None
        if not self._has_ping:
            log.error("ping not found: gateway and internet probes will report failure")

    def gateway(self) -> str | None:
        if self.cfg.gateway != "auto":
            return self.cfg.gateway
        gw = default_gateway()
        # When the link drops the default route vanishes too. Keep probing the last
        # known gateway so the outage is measured instead of silently skipped.
        if gw:
            self._gateway = gw
        return self._gateway

    def run_round(self) -> ProbeRound:
        c = self.cfg
        rnd = ProbeRound(ts=time.time())
        t = c.timeout
        ping_budget = c.ping_count * (t + 0.5) + 2

        dns_f = self.runner.submit("dns", _resolve, c.target_host, c.tcp_port)
        gw = self.gateway()
        futures: dict[str, tuple[cf.Future, float, str]] = {}
        if self._has_ping:
            if gw:
                futures["gateway"] = (self.runner.submit("gateway", _ping, gw, c.ping_count, t), ping_budget, gw)
            futures["internet"] = (self.runner.submit("internet", _ping, c.internet_host, c.ping_count, t),
                                   ping_budget, c.internet_host)
        if c.http_url:
            futures["http"] = (self.runner.submit("http", _http, c.http_url, t), t + 1, c.http_url)

        deadline = time.monotonic()

        # DNS first, so TCP can connect to the address it just resolved.
        try:
            ms, ip, family = dns_f.result(timeout=t)
            self._last_ip = (ip, family)
            rnd.results["dns"] = ProbeResult("dns", c.target_host, True, value_ms=ms)
        except Exception as exc:  # every failure becomes a probe error, never a crash
            rnd.results["dns"] = ProbeResult("dns", c.target_host, False, error=_err(exc, t))

        # TCP uses the last good address if DNS failed: that separates "DNS is down"
        # from "the network is down" on the dashboard.
        tcp_target = f"{c.target_host}:{c.tcp_port}"
        if self._last_ip:
            ip, family = self._last_ip
            try:
                ms = self.runner.run("tcp", _tcp_connect, ip, family, c.tcp_port, t, timeout=t + 0.5)
                rnd.results["tcp"] = ProbeResult("tcp", tcp_target, True, value_ms=ms)
            except Exception as exc:
                rnd.results["tcp"] = ProbeResult("tcp", tcp_target, False, error=_err(exc, t))
        else:
            rnd.results["tcp"] = ProbeResult("tcp", tcp_target, False, error="no address resolved yet")

        for name, (fut, budget, target) in futures.items():
            remaining = max(0.0, deadline + budget - time.monotonic())
            try:
                if name == "http":
                    ms, status = fut.result(timeout=remaining)
                    rnd.results[name] = ProbeResult(name, target, status < 500, value_ms=ms, status=status,
                                                    error=None if status < 500 else f"HTTP {status}")
                else:
                    loss, rtt = fut.result(timeout=remaining)
                    rnd.results[name] = ProbeResult(name, target, loss < 100.0, value_ms=rtt, loss_pct=loss,
                                                    error=None if loss < 100.0 else "no reply")
            except Exception as exc:
                rnd.results[name] = ProbeResult(name, target, False, error=_err(exc, budget))
        if "gateway" not in rnd.results:
            why = "no default gateway" if self._has_ping else "ping not installed"
            rnd.results["gateway"] = ProbeResult("gateway", gw or "unknown", False, error=why)
        if "internet" not in rnd.results:
            rnd.results["internet"] = ProbeResult("internet", c.internet_host, False, error="ping not installed")
        return rnd

    def close(self) -> None:
        self.runner.shutdown()


def probe_metrics(rnd: ProbeRound) -> list[Metric]:
    out: list[Metric] = []
    for name, r in rnd.results.items():
        out.append(Metric("probe.up", name, 1.0 if r.ok else 0.0))
        if name == "dns" and r.ok:
            out.append(Metric("probe.dns_ms", r.target, r.value_ms, "ms"))
        elif name == "tcp" and r.ok:
            out.append(Metric("probe.tcp_ms", r.target, r.value_ms, "ms"))
        elif name in ("gateway", "internet"):
            if r.loss_pct is not None:
                out.append(Metric("probe.loss_pct", name, r.loss_pct, "%"))
            elif not r.ok:
                out.append(Metric("probe.loss_pct", name, 100.0, "%"))
            if r.value_ms is not None:
                out.append(Metric("probe.rtt_ms", name, r.value_ms, "ms"))
        elif name == "http":
            if r.value_ms is not None:
                out.append(Metric("probe.http_ms", r.target, r.value_ms, "ms"))
            if r.status is not None:
                out.append(Metric("probe.http_status", r.target, float(r.status)))
    return out
