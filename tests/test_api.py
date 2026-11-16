"""The HTTP API, served by a real Agent that has taken one real sample of this machine."""

from __future__ import annotations

import gzip
import json
import socket
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest

from healthagent.__main__ import main
from healthagent.agent import Agent
from healthagent.api import BadRequest, parse_time, serve
from healthagent.config import load_config


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    cfg = load_config(None)
    cfg.agent.db_path = str(tmp_path_factory.mktemp("api") / "a.db")
    cfg.api.token = "t0ken"
    agent = Agent(cfg)
    agent.sample_once()
    time.sleep(0.2)
    agent.sample_once()  # second sample: per-process CPU now has a delta
    server = serve(agent, "127.0.0.1", 0)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    agent.db.close()


def get(base: str, path: str, token: str | None = "t0ken", **headers) -> tuple[int, dict, bytes]:
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def get_json(base: str, path: str) -> dict:
    status, _, body = get(base, path)
    assert status == 200, body
    return json.loads(body)


def test_health(base) -> None:
    h = get_json(base, "/health")
    assert h["status"] in ("ok", "warn", "critical")
    assert h["agent"]["rss_mb"] > 0 and h["checked_at_iso"]
    assert isinstance(h["open_alerts"], list)


def test_metrics_list_and_query(base) -> None:
    names = {s["name"] for s in get_json(base, "/metrics")["series"]}
    assert {"cpu.total", "cpu.core", "mem.percent", "disk.percent", "proc.count", "agent.rss_mb"} <= names
    m = get_json(base, "/metrics?name=cpu.total,mem.percent&since=5m")
    assert {s["name"] for s in m["series"]} == {"cpu.total", "mem.percent"}
    assert all(s["points"] for s in m["series"])


def test_alerts_and_processes(base) -> None:
    alerts = get_json(base, "/alerts?open=true")["alerts"]
    assert all(a["open"] and a["closed_at"] is None for a in alerts)
    p = get_json(base, "/processes?sort=cpu&limit=5")
    assert 1 <= len(p["processes"]) <= 5 and p["total"] >= len(p["processes"])
    cpus = [x["cpu_pct"] or 0 for x in p["processes"]]
    assert cpus == sorted(cpus, reverse=True)
    by_mem = get_json(base, "/processes?sort=mem&limit=5")["processes"]
    rss = [x["rss_mb"] or 0 for x in by_mem]
    assert rss == sorted(rss, reverse=True)


def test_system_snapshot(base) -> None:
    s = get_json(base, "/system")
    assert s["disks"] and "cpu.total" in s["metrics"]


@pytest.mark.parametrize("path,code", [
    ("/metrics?name=cpu.total&since=yesterday-ish", 400),
    ("/metrics?name=cpu.total&agg=median", 400),
    ("/metrics?name=cpu.total&since=1h&until=2h", 400),
    ("/processes?sort=evil", 400),
    ("/alerts?open=maybe", 400),
    ("/nope", 404),
])
def test_bad_requests(base, path: str, code: int) -> None:
    status, _, body = get(base, path)
    assert status == code and "error" in json.loads(body)


def test_token_is_required_for_the_api_but_not_the_page(base) -> None:
    assert get(base, "/health", token=None)[0] == 401
    assert get(base, "/health", token="wrong")[0] == 401
    assert get(base, "/health?token=t0ken", token=None)[0] == 200
    assert get(base, "/", token=None)[0] == 200


def test_read_only_and_gzip(base) -> None:
    req = urllib.request.Request(base + "/health", method="POST", data=b"{}")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 405
    status, headers, body = get(base, "/", token=None, **{"Accept-Encoding": "gzip"})
    assert headers.get("Content-Encoding") == "gzip" and b"Edge health" in gzip.decompress(body)


def test_parse_time() -> None:
    now = 1_800_000_000.0
    assert parse_time("1h", now, 0) == now - 3600
    assert parse_time("15m", now, 0) == now - 900
    assert parse_time("7d", now, 0) == now - 7 * 86400
    assert parse_time("1763290000", now, 0) == 1763290000
    assert parse_time("1763290000000", now, 0) == 1763290000, "milliseconds"
    assert parse_time("2026-11-16T10:00:00Z", now, 0) == datetime(2026, 11, 16, 10, tzinfo=UTC).timestamp()
    assert parse_time(None, now, 42) == 42
    with pytest.raises(BadRequest):
        parse_time("soon", now, 0)


def test_second_agent_on_a_busy_port_exits_with_a_clear_message(tmp_path, capsys) -> None:
    holder = socket.socket()  # stands in for an agent that is already running
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    port = holder.getsockname()[1]
    try:
        code = main(["--db", str(tmp_path / "second.db"), "--host", "127.0.0.1", "--port", str(port)])
    finally:
        holder.close()
    assert code == 1
    err = capsys.readouterr().err
    assert f"port {port}" in err and "already in use" in err and "Traceback" not in err
