"""Turns rule signals into persisted alerts: open once, track the peak, close once.

When notifications are enabled, every open and close also writes an outbox row in
the same SQLite transaction as the alert change (transactional outbox). A crash or
power cut can therefore never record an alert without also queueing its
notification, or the other way round. notify.py delivers the outbox.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from .rules import Rule, Signal, State
from .storage import Storage

log = logging.getLogger("healthagent.alerts")
SEVERITY_ORDER = {"ok": 0, "warn": 1, "critical": 2}


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).astimezone().isoformat(timespec="seconds")


def alert_view(a: dict[str, Any], now: float) -> dict[str, Any]:
    """An alert row as the API and webhooks present it."""
    out = dict(a)
    out["open"] = a["closed_at"] is None
    out["opened_at_iso"] = iso(a["opened_at"])
    out["closed_at_iso"] = iso(a["closed_at"])
    out["duration_s"] = round((a["closed_at"] or now) - a["opened_at"], 1)
    return out


class AlertManager:
    def __init__(self, storage: Storage, source: str = "", outbox: bool = False) -> None:
        self.db = storage
        self.source = source
        self.outbox = outbox
        self._lock = threading.Lock()
        self.open: dict[tuple[str, str], dict[str, Any]] = {
            (a["rule"], a["key"]): a for a in storage.open_alerts()
        }

    def adopt_into(self, rules: list[Rule]) -> None:
        """Tell rules which of their alerts were already open before a restart, so a
        restart neither duplicates them nor closes them just because state was lost."""
        by_name = {r.name: r for r in rules}
        for (rule, key), a in self.open.items():
            r = by_name.get(rule)
            if r is not None:
                r.adopt(key, a["subject"])
                log.info("resuming open alert #%s %s %s", a["id"], rule, a["subject"])

    def apply(self, rule: Rule, signals: list[Signal], now: float) -> None:
        with self._lock:
            mentioned = set()
            for s in signals:
                mentioned.add(s.key)
                a = self.open.get((rule.name, s.key))
                if s.state is State.FIRING:
                    if a is None:
                        self._open(rule, s, now)
                    else:
                        self._update(a, s.value)
                elif s.state is State.HOLD:
                    if a is not None:
                        self._update(a, s.value)
                elif a is not None:
                    self._close(a, now, s.message or "condition cleared")
            for (rname, key), a in list(self.open.items()):
                if rname == rule.name and key not in mentioned:
                    self._close(a, now, "subject gone (process exited or device removed)")

    def _enqueue(self, event: str, a: dict[str, Any], now: float) -> None:
        if not self.outbox:
            return
        uid = uuid.uuid4().hex
        payload = {"id": uid, "event": event, "source": self.source, "queued_at": now,
                   "queued_at_iso": iso(now), "alert": alert_view(a, now)}
        self.db.outbox_add(uid, event, json.dumps(payload, separators=(",", ":")), now)

    def _open(self, rule: Rule, s: Signal, now: float) -> None:
        try:
            with self.db.transaction():
                cur = self.db.execute_write(
                    """INSERT INTO alerts (rule, key, subject, severity, message, unit, opened_at,
                                           peak_value, last_value)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (rule.name, s.key, s.subject, rule.severity, s.message, rule.unit, now, s.value, s.value))
                a = {
                    "id": cur.lastrowid, "rule": rule.name, "key": s.key, "subject": s.subject,
                    "severity": rule.severity, "message": s.message, "unit": rule.unit, "opened_at": now,
                    "closed_at": None, "peak_value": s.value, "last_value": s.value, "close_reason": None,
                }
                self._enqueue("alert.opened", a, now)
        except sqlite3.IntegrityError:
            # Another open row exists (should be impossible): adopt it instead of duplicating.
            row = self.db.execute_write(
                "SELECT * FROM alerts WHERE rule=? AND key=? AND closed_at IS NULL", (rule.name, s.key)).fetchone()
            self.open[(rule.name, s.key)] = dict(row)
            return
        self.open[(rule.name, s.key)] = a
        log.warning("ALERT OPEN  #%s %-16s %-28s %s", a["id"], rule.name, s.subject, s.message)

    def _update(self, a: dict[str, Any], value: float | None) -> None:
        if value is None or value == a["last_value"]:
            return
        a["last_value"] = value
        if a["peak_value"] is None or value > a["peak_value"]:
            a["peak_value"] = value
        self.db.execute_write("UPDATE alerts SET last_value=?, peak_value=? WHERE id=?",
                              (value, a["peak_value"], a["id"]))

    def _close(self, a: dict[str, Any], now: float, reason: str) -> None:
        with self.db.transaction():
            self.db.execute_write("UPDATE alerts SET closed_at=?, close_reason=? WHERE id=?",
                                  (now, reason, a["id"]))
            self._enqueue("alert.closed", {**a, "closed_at": now, "close_reason": reason}, now)
        a["closed_at"], a["close_reason"] = now, reason
        del self.open[(a["rule"], a["key"])]
        log.warning("ALERT CLOSE #%s %-16s %-28s after %.0fs: %s",
                    a["id"], a["rule"], a["subject"], now - a["opened_at"], reason)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted((dict(a) for a in self.open.values()),
                          key=lambda a: (-SEVERITY_ORDER.get(a["severity"], 0), a["opened_at"]))

    def status(self) -> str:
        worst = "ok"
        for a in self.snapshot():
            if SEVERITY_ORDER.get(a["severity"], 0) > SEVERITY_ORDER[worst]:
                worst = a["severity"]
        return worst
