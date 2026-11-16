"""'Every probe has a timeout. A hung DNS lookup must never stall the sampler.'"""

from __future__ import annotations

import threading
import time

import pytest

import healthagent.probes as probes
from healthagent.config import ProbeConfig
from healthagent.deadline import DeadlineRunner, Hung


def test_deadline_runner_times_out_and_never_piles_up_threads() -> None:
    r = DeadlineRunner(max_workers=4, name="t")
    release = threading.Event()
    with pytest.raises(TimeoutError):
        r.run("dns", release.wait, timeout=0.1)
    for _ in range(20):  # 20 more rounds while the first call is still stuck
        with pytest.raises(Hung):
            r.run("dns", release.wait, timeout=0.1)
    assert r.stuck() == ["dns"]
    assert r.run("other", lambda: 42, timeout=1) == 42, "other probes are unaffected"
    release.set()
    time.sleep(0.05)
    assert r.run("dns", lambda: "ok", timeout=1) == "ok", "recovers once the call returns"
    r.shutdown()


@pytest.fixture
def hung_dns(monkeypatch):
    release = threading.Event()

    def resolve(host, port):
        release.wait(30)  # a resolver that never answers
        raise OSError("gave up")

    monkeypatch.setattr(probes, "_resolve", resolve)
    monkeypatch.setattr(probes, "_ping", lambda host, count, timeout: (0.0, 1.0))
    monkeypatch.setattr(probes, "default_gateway", lambda: "10.0.0.1")
    yield
    release.set()


def test_hung_dns_costs_one_thread_and_other_probes_still_run(hung_dns) -> None:
    p = probes.Prober(ProbeConfig(timeout=0.3, http_url=""))
    p._has_ping = True
    t = time.monotonic()
    first = p.run_round()
    assert time.monotonic() - t < 1.5
    assert first.results["dns"].error == "timeout after 0.3s"
    assert first.results["gateway"].ok and first.results["internet"].ok
    threads = threading.active_count()
    for _ in range(5):
        t = time.monotonic()
        rnd = p.run_round()
        # On a loaded CI runner the first "hung" report may come back a little slow.
        assert time.monotonic() - t < 1.5, "a still-hung lookup is not waited for again"
        assert rnd.results["dns"].error == "previous attempt still hung"
        assert rnd.results["gateway"].ok
    # Allow +2 for threads the Python runtime itself may be spinning up.
    assert threading.active_count() <= threads + 2, "no thread per round"
    p.close()


def test_tcp_uses_last_resolved_address_when_dns_fails(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(probes, "_tcp_connect", lambda ip, fam, port, t: calls.append(ip) or 7.0)
    monkeypatch.setattr(probes, "_ping", lambda host, count, timeout: (0.0, 1.0))
    monkeypatch.setattr(probes, "default_gateway", lambda: "10.0.0.1")
    p = probes.Prober(ProbeConfig(timeout=0.3, http_url=""))
    monkeypatch.setattr(probes, "_resolve", lambda h, port: (3.0, "203.0.113.7", 2))
    assert p.run_round().results["dns"].ok

    def broken(h, port):
        raise OSError("SERVFAIL")

    monkeypatch.setattr(probes, "_resolve", broken)
    rnd = p.run_round()
    assert not rnd.results["dns"].ok and rnd.results["tcp"].ok
    assert calls == ["203.0.113.7", "203.0.113.7"]
    p.close()


def test_gateway_is_remembered_when_the_route_disappears(monkeypatch) -> None:
    p = probes.Prober(ProbeConfig())
    monkeypatch.setattr(probes, "default_gateway", lambda: "192.168.1.1")
    assert p.gateway() == "192.168.1.1"
    monkeypatch.setattr(probes, "default_gateway", lambda: None)  # link down: no default route
    assert p.gateway() == "192.168.1.1", "keep probing the last known gateway"
    p.close()
