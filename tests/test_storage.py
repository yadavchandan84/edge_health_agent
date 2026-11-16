"""SQLite storage: queries, rollups, retention bounds."""

from __future__ import annotations

import time

import pytest

from healthagent.models import Metric
from healthagent.storage import Storage

DAY = 86400


@pytest.fixture
def db(tmp_path) -> Storage:
    s = Storage(str(tmp_path / "s.db"))
    yield s
    s.close()


def seed(db: Storage, start: float, end: float, step: int = 5, names=("cpu.total",)) -> None:
    for t in range(int(start), int(end), step):
        db.write_metrics(t, [Metric(n, "", float(t % 100), "%") for n in names])


def test_raw_query_returns_points_in_range(db: Storage) -> None:
    now = time.time()
    seed(db, now - 600, now)
    [s] = db.query(["cpu.total"], None, now - 300, now, 5, "avg", DAY)
    assert s["name"] == "cpu.total" and s["unit"] == "%"
    assert 59 <= len(s["points"]) <= 61
    assert all(now - 305 <= t <= now for t, _ in s["points"])


def test_bucketing_and_aggregates(db: Storage) -> None:
    now = int(time.time()) // 60 * 60
    for i, t in enumerate(range(now - 600, now, 5)):
        db.write_metrics(t, [Metric("x", "", float(i % 12))])  # 0..11 in each minute
    avg = db.query(["x"], None, now - 600, now, 60, "avg", DAY)[0]["points"]
    mx = db.query(["x"], None, now - 600, now, 60, "max", DAY)[0]["points"]
    assert len(avg) == 10 and {v for _, v in avg} == {5.5}
    assert {v for _, v in mx} == {11}


def test_prefix_and_subject_filters(db: Storage) -> None:
    now = time.time()
    db.write_metrics(now, [Metric("disk.percent", "/", 10), Metric("disk.percent", "/data", 20),
                           Metric("disk.free", "/", 1), Metric("diskio.read_bps", "sda", 5)])
    names = {(s["name"], s["subject"]) for s in db.query(["disk.*"], None, now - 60, now + 1, 5, "avg", DAY)}
    assert names == {("disk.percent", "/"), ("disk.percent", "/data"), ("disk.free", "/")}
    only = db.query(["disk.percent"], "/data", now - 60, now + 1, 5, "avg", DAY)
    assert [(s["subject"], s["points"][0][1]) for s in only] == [("/data", 20)]


def test_like_wildcards_in_names_are_escaped(db: Storage) -> None:
    now = time.time()
    db.write_metrics(now, [Metric("a_b", "", 1), Metric("axb", "", 2)])
    assert [s["name"] for s in db.query(["a_*"], None, now - 60, now + 1, 5, "avg", DAY)] == ["a_b"]


def test_rollup_is_idempotent_and_feeds_old_ranges(db: Storage) -> None:
    now = int(time.time()) // 60 * 60
    seed(db, now - 3 * 3600, now)
    n1 = db.rollup(now + 30)
    assert n1 > 0 and db.rollup(now + 30) == 0
    # A range older than the raw window must come from rollups.
    [s] = db.query(["cpu.total"], None, now - 3 * 3600, now, 600, "avg", raw_retention_s=3600)
    assert 17 <= len(s["points"]) <= 19


def test_retention_bounds_history(db: Storage) -> None:
    now = time.time()
    sid = db._series_id("m", "", "")
    with db.transaction():
        db._w.executemany("INSERT INTO samples_1m VALUES (?,?,?,?,?,?)",
                          ((sid, int(t), 1, 1, 1, 12) for t in range(int(now - 9 * DAY), int(now), 60)))
        db._w.executemany("INSERT INTO samples VALUES (?,?,?)",
                          ((sid, t, 1.0) for t in range(int(now - 30 * 3600), int(now), 5)))
    removed = db.prune(now, raw_s=DAY, rollup_s=7 * DAY, alerts_s=30 * DAY)
    assert removed["samples"] > 0 and removed["samples_1m"] > 0
    with db._lock:
        raw_min = db._w.execute("SELECT min(ts) FROM samples").fetchone()[0]
        roll_min = db._w.execute("SELECT min(ts) FROM samples_1m").fetchone()[0]
    # Cut-offs are whole seconds, so allow the 1 s of truncation.
    assert now - raw_min <= DAY + 1 and now - roll_min <= 7 * DAY + 1
    assert now - raw_min > DAY - 10, "and nothing newer than the window was deleted"


def test_series_with_no_data_left_are_forgotten(db: Storage) -> None:
    now = time.time()
    db.write_metrics(now - 10 * DAY, [Metric("gone.iface", "usb0", 1)])
    db.write_metrics(now, [Metric("cpu.total", "", 1)])
    db.prune(now, raw_s=DAY, rollup_s=7 * DAY, alerts_s=30 * DAY)
    assert [s["name"] for s in db.list_series()] == ["cpu.total"]
    db.write_metrics(now + 5, [Metric("gone.iface", "usb0", 2)])  # comes back: recreated cleanly
    assert len(db.query(["gone.iface"], None, now, now + 10, 5, "avg", DAY)[0]["points"]) == 1


def test_closed_alerts_expire_open_ones_never(db: Storage) -> None:
    now = time.time()
    old = now - 40 * DAY
    db.execute_write("INSERT INTO alerts (rule, key, subject, severity, opened_at, closed_at) "
                     "VALUES ('r','a','a','warn',?,?)", (old, old + 60))
    db.execute_write("INSERT INTO alerts (rule, key, subject, severity, opened_at) VALUES ('r','b','b','warn',?)",
                     (old,))
    db.prune(now, raw_s=DAY, rollup_s=7 * DAY, alerts_s=30 * DAY)
    assert [a["key"] for a in db.alerts(None, 10)] == ["b"]


def test_transaction_rolls_back(db: Storage) -> None:
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.outbox_add("uid-1", "e", "{}", 1.0)
            raise RuntimeError("boom")
    assert db.outbox_stats()[0] == 0
