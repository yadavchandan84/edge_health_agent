"""SQLite storage: metric history, 1-minute rollups, alerts.

Layout
  series      (id, name, subject, unit)          one row per metric+subject
  samples     (series_id, ts, value)             raw 5 s points, kept raw_hours
  samples_1m  (series_id, ts, avg, min, max, n)  1-minute rollups, kept rollup_days
  alerts      (...)                              open + closed alerts
  meta        (key, value)                       rollup watermark, schema version

`samples` and `samples_1m` are WITHOUT ROWID tables keyed by (series_id, ts): rows are
stored in key order, so a range query for one series is a single index scan and there
is no separate rowid b-tree to pay for. Retention deletes per series through the same
key. Freed pages are reused, so the file plateaus instead of growing.

One writer connection guarded by a lock; readers (HTTP threads) each get their own
connection. WAL mode lets readers and the writer work at the same time.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from .models import Metric

log = logging.getLogger(__name__)
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS series (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    unit    TEXT NOT NULL DEFAULT '',
    UNIQUE (name, subject)
);
CREATE TABLE IF NOT EXISTS samples (
    series_id INTEGER NOT NULL,
    ts        INTEGER NOT NULL,
    value     REAL NOT NULL,
    PRIMARY KEY (series_id, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS samples_1m (
    series_id INTEGER NOT NULL,
    ts        INTEGER NOT NULL,
    avg       REAL NOT NULL,
    min       REAL NOT NULL,
    max       REAL NOT NULL,
    n         INTEGER NOT NULL,
    PRIMARY KEY (series_id, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY,
    rule         TEXT NOT NULL,
    key          TEXT NOT NULL,
    subject      TEXT NOT NULL,
    severity     TEXT NOT NULL,
    message      TEXT NOT NULL DEFAULT '',
    unit         TEXT NOT NULL DEFAULT '',
    opened_at    REAL NOT NULL,
    closed_at    REAL,
    peak_value   REAL,
    last_value   REAL,
    close_reason TEXT
);
-- The database itself guarantees "an alert opens once": at most one open alert per
-- (rule, key), no matter what the code above it does.
CREATE UNIQUE INDEX IF NOT EXISTS alerts_one_open ON alerts (rule, key) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS alerts_opened ON alerts (opened_at);
-- Notifications waiting to leave the box. Written in the same transaction as the
-- alert change they describe (transactional outbox), delivered in id order.
-- AUTOINCREMENT: ids must never be reused once the queue drains, or delivery order
-- breaks. Receivers deduplicate on uid, a random id that is unique per event.
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    uid        TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    event      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
"""


class Storage:
    def __init__(self, path: str) -> None:
        self.path = str(Path(path).resolve())
        self._lock = threading.RLock()
        self._pool: queue.Queue[sqlite3.Connection] = queue.Queue(maxsize=4)
        self._w = self._connect()
        with self._lock:
            # auto_vacuum must be set before the first table exists.
            self._w.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self._w.executescript(SCHEMA)
            self._w.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            self._w.commit()
        self._series: dict[tuple[str, str], int] = {
            (n, s): i for i, n, s in self._w.execute("SELECT id, name, subject FROM series")
        }

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")  # safe with WAL; fsync on checkpoint only
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA cache_size=-4000")  # ~4 MB page cache per connection
        conn.row_factory = sqlite3.Row
        return conn

    @contextlib.contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        """Borrow a read-only connection. The HTTP server starts a thread per request,
        so a small pool is used instead of thread-locals (which would leak)."""
        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            conn = self._connect()
            conn.execute("PRAGMA query_only=1")
        try:
            yield conn
        finally:
            try:
                self._pool.put_nowait(conn)
            except queue.Full:
                conn.close()

    # -- writes --------------------------------------------------------------
    def _series_id(self, name: str, subject: str, unit: str) -> int:
        key = (name, subject)
        sid = self._series.get(key)
        if sid is None:
            self._w.execute("INSERT OR IGNORE INTO series (name, subject, unit) VALUES (?, ?, ?)",
                            (name, subject, unit))
            sid = self._w.execute("SELECT id FROM series WHERE name=? AND subject=?", key).fetchone()[0]
            self._series[key] = sid
        return sid

    def write_metrics(self, ts: float, metrics: Iterable[Metric]) -> int:
        # Floor, never round: a rounded-up timestamp lies in the future, and a query
        # "until now" made in the same second would miss the newest sample.
        t = int(ts)
        with self._lock:
            self._w.execute("BEGIN")
            try:
                rows = [(self._series_id(m.name, m.subject, m.unit), t, float(m.value))
                        for m in metrics if m.value is not None]
                self._w.executemany("INSERT OR REPLACE INTO samples VALUES (?, ?, ?)", rows)
                self._w.execute("COMMIT")
            except BaseException:
                self._w.execute("ROLLBACK")
                raise
        return len(rows)

    def execute_write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._w.execute(sql, params)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """Group several execute_write calls into one atomic commit."""
        with self._lock:
            self._w.execute("BEGIN")
            try:
                yield
            except BaseException:
                self._w.execute("ROLLBACK")
                raise
            self._w.execute("COMMIT")

    # -- outbox --------------------------------------------------------------
    def outbox_add(self, uid: str, event: str, payload: str, now: float) -> int:
        cur = self.execute_write("INSERT INTO outbox (uid, created_at, event, payload) VALUES (?, ?, ?, ?)",
                                 (uid, now, event, payload))
        return int(cur.lastrowid)

    def outbox_head(self, limit: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._w.execute("SELECT * FROM outbox ORDER BY id LIMIT ?", (limit,)).fetchall()

    def outbox_done(self, row_id: int) -> None:
        self.execute_write("DELETE FROM outbox WHERE id=?", (row_id,))

    def outbox_failed(self, row_id: int, error: str) -> None:
        self.execute_write("UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE id=?",
                           (error[:500], row_id))

    def outbox_stats(self) -> tuple[int, float | None]:
        with self._lock:
            n, oldest = self._w.execute("SELECT count(*), min(created_at) FROM outbox").fetchone()
        return int(n), oldest

    # -- maintenance ---------------------------------------------------------
    def rollup(self, now: float) -> int:
        """Aggregate complete minutes of raw data into samples_1m. Idempotent."""
        # 10 s grace so the last sample of a minute is written before we roll it up.
        end = int(now - 10) // 60 * 60
        n = 0
        with self._lock:
            row = self._w.execute("SELECT value FROM meta WHERE key='rollup_until'").fetchone()
            start = int(row[0]) if row else 0  # first run: roll up whatever raw data exists
            if start >= end:
                return 0
            self._w.execute("BEGIN")
            try:
                # One index range scan per series; filtering on ts alone would scan the table.
                for sid in list(self._series.values()):
                    n += self._w.execute(
                        """INSERT OR REPLACE INTO samples_1m
                           SELECT series_id, ts / 60 * 60, avg(value), min(value), max(value), count(*)
                           FROM samples WHERE series_id = ? AND ts >= ? AND ts < ?
                           GROUP BY ts / 60""",
                        (sid, start, end),
                    ).rowcount
                self._w.execute("INSERT OR REPLACE INTO meta VALUES ('rollup_until', ?)", (str(end),))
                self._w.execute("COMMIT")
            except BaseException:
                self._w.execute("ROLLBACK")
                raise
        return n

    def prune(self, now: float, raw_s: float, rollup_s: float, alerts_s: float,
              outbox_s: float = 7 * 86400) -> dict[str, int]:
        raw_cut, roll_cut, alert_cut = int(now - raw_s), int(now - rollup_s), now - alerts_s
        removed = {"samples": 0, "samples_1m": 0, "alerts": 0, "outbox": 0}
        with self._lock:
            ids = [r[0] for r in self._w.execute("SELECT id FROM series")]
        for sid in ids:  # one small transaction per series: the sampler never waits long
            with self._lock:
                w = self._w
                removed["samples"] += w.execute("DELETE FROM samples WHERE series_id=? AND ts<?",
                                                (sid, raw_cut)).rowcount
                removed["samples_1m"] += w.execute("DELETE FROM samples_1m WHERE series_id=? AND ts<?",
                                                   (sid, roll_cut)).rowcount
                # Forget series with no data left (an interface or disk that is long gone),
                # so the series table cannot grow forever either.
                if (w.execute("SELECT 1 FROM samples WHERE series_id=? LIMIT 1", (sid,)).fetchone() is None
                        and w.execute("SELECT 1 FROM samples_1m WHERE series_id=? LIMIT 1", (sid,)).fetchone() is None):
                    w.execute("DELETE FROM series WHERE id=?", (sid,))
                    self._series = {k: v for k, v in self._series.items() if v != sid}
                    removed["series"] = removed.get("series", 0) + 1
        with self._lock:
            removed["alerts"] = self._w.execute(
                "DELETE FROM alerts WHERE closed_at IS NOT NULL AND closed_at < ?", (alert_cut,)).rowcount
            removed["outbox"] = self._w.execute(
                "DELETE FROM outbox WHERE created_at < ?", (now - outbox_s,)).rowcount
            if removed["outbox"]:
                log.warning("dropped %d undelivered notifications older than %.0f days",
                            removed["outbox"], outbox_s / 86400)
            self._w.execute("PRAGMA incremental_vacuum(2000)")
            self._w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return removed

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(self.path + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    # -- reads ---------------------------------------------------------------
    def list_series(self) -> list[dict[str, Any]]:
        with self._reader() as conn:
            rows = conn.execute("SELECT name, subject, unit FROM series ORDER BY name, subject")
            return [dict(r) for r in rows]

    def query(self, names: list[str], subject: str | None, since: float, until: float,
              step: int, agg: str, raw_retention_s: float) -> list[dict[str, Any]]:
        with self._reader() as conn:
            return self._query(conn, names, subject, since, until, step, agg, raw_retention_s)

    def _query(self, conn: sqlite3.Connection, names: list[str], subject: str | None, since: float,
               until: float, step: int, agg: str, raw_retention_s: float) -> list[dict[str, Any]]:
        clauses, params = [], []
        name_parts = []
        for n in names:
            if n.endswith("*"):
                name_parts.append("name LIKE ? ESCAPE '\\'")
                params.append(n[:-1].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
            else:
                name_parts.append("name = ?")
                params.append(n)
        clauses.append("(" + " OR ".join(name_parts) + ")")
        if subject is not None:
            clauses.append("subject = ?")
            params.append(subject)
        series = conn.execute(
            f"SELECT id, name, subject, unit FROM series WHERE {' AND '.join(clauses)} ORDER BY name, subject",
            params).fetchall()

        # Raw points while they are still retained (always up to date, any bucket size);
        # 1-minute rollups for anything older than the raw retention window.
        use_raw = since >= time.time() - raw_retention_s
        if not use_raw:
            step = max(step, 60)
        since = since // step * step  # align so buckets do not shift between refreshes
        fn = {"avg": "avg", "max": "max", "min": "min"}[agg]  # whitelisted, never user text
        result = []
        for s in series:
            if use_raw and step <= 5:
                sql = "SELECT ts, value FROM samples WHERE series_id=? AND ts>=? AND ts<=? ORDER BY ts"
                args: tuple = (s["id"], int(since), int(until))
            elif use_raw:
                sql = (f"SELECT ts / ? * ? AS b, {fn}(value) FROM samples "
                       "WHERE series_id=? AND ts>=? AND ts<=? GROUP BY b ORDER BY b")
                args = (step, step, s["id"], int(since), int(until))
            else:
                expr = "sum(avg * n) / sum(n)" if agg == "avg" else f"{fn}({fn})"
                sql = (f"SELECT ts / ? * ? AS b, {expr} FROM samples_1m "
                       "WHERE series_id=? AND ts>=? AND ts<=? GROUP BY b ORDER BY b")
                args = (step, step, s["id"], int(since), int(until))
            points = [[r[0], round(r[1], 3)] for r in conn.execute(sql, args)]
            result.append({"name": s["name"], "subject": s["subject"], "unit": s["unit"], "points": points})
        return result

    def alerts(self, open_only: bool | None, limit: int, rule: str | None = None,
               since: float | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if open_only is True:
            clauses.append("closed_at IS NULL")
        elif open_only is False:
            clauses.append("closed_at IS NOT NULL")
        if rule:
            clauses.append("rule = ?")
            params.append(rule)
        if since is not None:
            clauses.append("(opened_at >= ? OR closed_at IS NULL OR closed_at >= ?)")
            params += [since, since]
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._reader() as conn:
            rows = conn.execute(
                f"SELECT * FROM alerts {where} ORDER BY (closed_at IS NULL) DESC, opened_at DESC LIMIT ?",
                (*params, limit))
            return [dict(r) for r in rows]

    def open_alerts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._w.execute("SELECT * FROM alerts WHERE closed_at IS NULL")]

    def close(self) -> None:
        while not self._pool.empty():
            self._pool.get_nowait().close()
        with self._lock:
            try:
                self._w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                self._w.close()
