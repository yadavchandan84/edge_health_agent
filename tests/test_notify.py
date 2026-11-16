"""Alerts leave the box: transactional outbox + webhook delivery with retries."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from healthagent.config import NotifyConfig
from healthagent.notify import WebhookNotifier, sign
from tests.conftest import Harness, sample


class Receiver:
    """A local webhook endpoint whose response code the test controls."""

    def __init__(self) -> None:
        self.status = 204
        self.received: list[tuple[Any, dict]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if outer.status < 300:
                    # Keep the Message object: header lookup is case-insensitive there.
                    outer.received.append((self.headers, json.loads(body) | {"_raw": body.decode()}))
                self.send_response(outer.status)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def rx():
    r = Receiver()
    yield r
    r.close()


@pytest.fixture
def setup(cfg, tmp_path, rx):
    h = Harness(cfg, str(tmp_path / "n.db"), outbox=True)
    n = WebhookNotifier(NotifyConfig(webhook_url=rx.url, secret="s3cret", timeout=2), h.db)
    yield h, n
    n.close()


def open_and_close_ram(h: Harness) -> None:
    h.feed("ram_high", sample(0, mem=95), 0)
    h.feed("ram_high", sample(5, mem=50), 5)


def test_open_and_close_are_delivered_in_order_and_signed(setup, rx) -> None:
    h, n = setup
    open_and_close_ram(h)
    assert n.tick(now=10) == 2
    (h1, e1), (h2, e2) = rx.received
    assert [e1["event"], e2["event"]] == ["alert.opened", "alert.closed"]
    assert e1["alert"]["rule"] == "ram_high" and e1["source"] == "test-box"
    assert e2["alert"]["closed_at"] == 5 and e2["alert"]["close_reason"]
    assert h1["X-Healthagent-Signature"] == sign("s3cret", e1["_raw"].encode())
    assert h1["X-Healthagent-Delivery"] == e1["id"] and h2["X-Healthagent-Delivery"] == e2["id"]
    assert e1["id"] != e2["id"]
    assert h.db.outbox_stats()[0] == 0


def test_delivery_ids_are_never_reused_after_the_queue_drains(setup, rx) -> None:
    # Regression: with a plain INTEGER PRIMARY KEY, SQLite reuses id 1 once the
    # outbox is empty, and a receiver deduplicating on it dropped a real alert.
    h, n = setup
    h.feed("ram_high", sample(0, mem=95), 0)
    n.tick(now=1)
    h.feed("ram_high", sample(5, mem=50), 5)
    n.tick(now=6)
    h.feed("ram_high", sample(10, mem=95), 10)
    n.tick(now=11)
    ids = [hdr["X-Healthagent-Delivery"] for hdr, _ in rx.received]
    assert len(ids) == 3 and len(set(ids)) == 3
    with h.db._lock:
        rowids = [r[0] for r in h.db._w.execute("SELECT seq FROM sqlite_sequence WHERE name='outbox'")]
    assert rowids == [3], "row ids keep increasing, so id order is delivery order"


def test_outage_queues_then_backs_off_then_delivers(setup, rx) -> None:
    h, n = setup
    rx.status = 503
    open_and_close_ram(h)
    assert n.tick(now=10) == 0
    assert n.retry_at == pytest.approx(15, abs=1), "first retry after 5 s"
    assert n.tick(now=12) == 0 and n.status(now=12)["pending"] == 2, "waits for the backoff"
    n.tick(now=15)
    assert n.retry_at == pytest.approx(25, abs=1), "backoff doubles"
    rx.status = 204
    assert n.tick(now=25) == 2
    assert [e["event"] for _, e in rx.received] == ["alert.opened", "alert.closed"], "order kept"
    assert n.failures == 0 and n.status(now=25)["last_error"] is None


def test_uplink_recovery_skips_the_remaining_backoff(setup, rx) -> None:
    h, n = setup
    rx.status = 503
    open_and_close_ram(h)
    for t in (10, 15, 25, 45):  # backoff now 40 s
        n.tick(now=t)
    rx.status = 204
    assert n.tick(now=50) == 0, "still backing off"
    n.retry_now()  # agent saw the internet probe recover
    assert n.tick(now=50) == 2


def test_unreachable_receiver_is_retried(cfg, tmp_path) -> None:
    h = Harness(cfg, str(tmp_path / "u.db"), outbox=True)
    n = WebhookNotifier(NotifyConfig(webhook_url="http://127.0.0.1:9/nothing", timeout=1), h.db)
    open_and_close_ram(h)
    assert n.tick(now=10) == 0
    assert n.status(now=10)["pending"] == 2 and n.last_error
    n.close()


def test_queue_survives_an_agent_restart(setup, rx, tmp_path) -> None:
    h, n = setup
    rx.status = 503
    open_and_close_ram(h)
    n.tick(now=10)
    h.restart()  # power cut: new process, same database
    rx.status = 204
    n2 = WebhookNotifier(NotifyConfig(webhook_url=rx.url), h.db)
    assert n2.tick(now=20) == 2
    n2.close()


def test_permanent_rejection_is_dropped_not_retried_forever(setup, rx) -> None:
    h, n = setup
    rx.status = 400
    open_and_close_ram(h)
    n.tick(now=10)
    assert n.dropped == 2 and h.db.outbox_stats()[0] == 0


def test_no_outbox_rows_when_notifications_are_off(cfg, tmp_path) -> None:
    h = Harness(cfg, str(tmp_path / "off.db"), outbox=False)
    open_and_close_ram(h)
    assert h.db.outbox_stats()[0] == 0
