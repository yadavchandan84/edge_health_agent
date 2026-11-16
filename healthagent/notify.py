"""Deliver alert notifications off the box, surviving uplink outages and restarts.

The outbox lives in SQLite, so a notification queued while the uplink is down (or
just before a power cut) is delivered once the box can reach the receiver again.

Delivery is at-least-once and in order:
  * rows are sent in id order; on a transient failure the whole queue waits
    (exponential backoff, 5 s doubling to 5 min) so a "closed" never overtakes its
    "opened";
  * each request carries X-Healthagent-Delivery: <random event id> (also "id" in the
    body), so the receiver can drop duplicates (a response lost on the way back
    means a resend);
  * a 4xx other than 408/429 means the receiver rejects this event for good, so it
    is dropped instead of blocking the queue forever.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from . import __version__
from .config import NotifyConfig
from .deadline import DeadlineRunner, Hung
from .storage import Storage

log = logging.getLogger("healthagent.notify")
BACKOFF_BASE = 5.0
BACKOFF_MAX = 300.0
BATCH = 20


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class WebhookNotifier:
    def __init__(self, cfg: NotifyConfig, db: Storage) -> None:
        self.cfg = cfg
        self.db = db
        self.runner = DeadlineRunner(max_workers=1, name="notify")
        self.failures = 0
        self.retry_at = 0.0
        self.delivered = 0
        self.dropped = 0
        self.last_error: str | None = None
        self.last_ok_at: float | None = None

    def tick(self, now: float | None = None) -> int:
        """Send what is due. Returns the number of notifications delivered."""
        now = time.time() if now is None else now
        if now < self.retry_at:
            return 0
        sent = 0
        for row in self.db.outbox_head(BATCH):
            try:
                status = self.runner.run("post", self._post, row, timeout=self.cfg.timeout + 1)
            except Exception as exc:  # network down, DNS, timeout, still hung from last time
                self._failed(row, _describe(exc, self.cfg.timeout), now)
                break
            if 200 <= status < 300:
                self.db.outbox_done(row["id"])
                sent += 1
                self.delivered += 1
                if self.failures:
                    log.info("webhook reachable again after %d failed attempt(s)", self.failures)
                self.failures = 0
                self.last_ok_at = now
            elif status in (408, 429) or status >= 500:
                self._failed(row, f"HTTP {status}", now)
                break
            else:
                log.error("webhook rejected %s #%s with HTTP %s; dropping it", row["event"], row["id"], status)
                self.db.outbox_done(row["id"])
                self.dropped += 1
        return sent

    def retry_now(self) -> None:
        """The uplink just came back: flush the backlog now instead of waiting out a
        backoff that may be minutes long."""
        if self.failures:
            self.retry_at = 0.0

    def _post(self, row: Any) -> int:
        body = row["payload"].encode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"edge-health-agent/{__version__}",
            "X-Healthagent-Event": row["event"],
            "X-Healthagent-Delivery": row["uid"],
        }
        if self.cfg.secret:
            headers["X-Healthagent-Signature"] = sign(self.cfg.secret, body)
        req = urllib.request.Request(self.cfg.webhook_url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                resp.read(1024)
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def _failed(self, row: Any, error: str, now: float) -> None:
        self.db.outbox_failed(row["id"], error)
        self.failures += 1
        self.retry_at = now + min(BACKOFF_MAX, BACKOFF_BASE * 2 ** (self.failures - 1))
        self.last_error = error
        if self.failures == 1:
            log.warning("webhook delivery failing (%s); notifications stay queued and are retried", error)

    def status(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        pending, oldest = self.db.outbox_stats()
        return {
            "enabled": True,
            "pending": pending,
            "oldest_pending_s": round(now - oldest, 1) if oldest else None,
            "delivered": self.delivered,
            "dropped": self.dropped,
            "last_error": self.last_error if self.failures else None,
            "retry_in_s": round(self.retry_at - now, 1) if self.retry_at > now else 0,
            "last_delivered_at": self.last_ok_at,
        }

    def close(self) -> None:
        self.runner.shutdown()


def _describe(exc: BaseException, timeout: float) -> str:
    if isinstance(exc, TimeoutError):
        return f"timeout after {timeout:g}s"
    if isinstance(exc, Hung):
        return "previous attempt still hung"
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    return str(exc) or type(exc).__name__
