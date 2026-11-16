"""Alert rules, replayed with a fake clock through the real AlertManager and SQLite.

Each test names the requirement from the brief it pins down."""

from __future__ import annotations

import pytest

from healthagent.config import WatchEntry
from tests.conftest import Harness, disk, probes, proc, sample


def run_cpu(h: Harness, values: dict[int, float]) -> None:
    for t, v in sorted(values.items()):
        h.feed("cpu_high", sample(t, cpu=v), t)


# -- CPU high: "above 85 % for 60 seconds without a break. Not at second 5." ------
class TestCpuHigh:
    def test_opens_at_60s_not_before(self, h: Harness) -> None:
        for t in range(0, 56, 5):
            h.feed("cpu_high", sample(t, cpu=95), t)
        assert h.all("cpu_high") == [], "must not open before 60 s"
        h.feed("cpu_high", sample(60, cpu=95), 60)
        [a] = h.all("cpu_high")
        assert a["opened_at"] == 60 and a["subject"] == "cpu total"

    def test_not_at_second_5(self, h: Harness) -> None:
        run_cpu(h, {0: 100, 5: 100})
        assert h.all("cpu_high") == []

    def test_a_single_dip_restarts_the_window(self, h: Harness) -> None:
        run_cpu(h, {t: 90 for t in range(0, 56, 5)} | {60: 50} | {t: 90 for t in range(65, 121, 5)})
        assert h.all("cpu_high") == [], "dip at 60 s: the new window started at 65 s"
        h.feed("cpu_high", sample(125, cpu=90), 125)
        assert h.all("cpu_high")[0]["opened_at"] == 125

    def test_dip_into_hysteresis_band_also_breaks_the_window(self, h: Harness) -> None:
        run_cpu(h, {t: 90 for t in range(0, 31, 5)} | {35: 83} | {t: 90 for t in range(40, 61, 5)})
        assert h.all("cpu_high") == []

    def test_missing_samples_count_as_a_break(self, h: Harness) -> None:
        # 30 s with no samples (agent paused, box swapping): we did not see CPU the whole time.
        run_cpu(h, {t: 99 for t in range(0, 31, 5)} | {t: 99 for t in range(60, 91, 5)})
        assert h.all("cpu_high") == []
        run_cpu(h, {t: 99 for t in range(95, 121, 5)})
        assert h.all("cpu_high")[0]["opened_at"] == 120

    def test_no_flapping_at_the_threshold(self, h: Harness) -> None:
        run_cpu(h, {t: 95 for t in range(0, 61, 5)})
        run_cpu(h, {t: (84 if i % 2 else 86) for i, t in enumerate(range(65, 600, 5))})
        [a] = h.all("cpu_high")
        assert a["closed_at"] is None, "84/86 % stays inside the hysteresis band"

    def test_closes_after_10s_below_80_and_keeps_peak(self, h: Harness) -> None:
        run_cpu(h, {t: 95 for t in range(0, 61, 5)} | {65: 99.5})
        run_cpu(h, {70: 30, 75: 30})
        assert h.open("cpu_high"), "below 80 % for only 5 s"
        h.feed("cpu_high", sample(80, cpu=30), 80)
        [a] = h.all("cpu_high")
        assert a["closed_at"] == 80 and a["peak_value"] == 99.5
        assert "back to 30.0%" in a["close_reason"]

    def test_opens_once_while_condition_holds(self, h: Harness) -> None:
        run_cpu(h, {t: 95 for t in range(0, 3600, 5)})
        assert len(h.all("cpu_high")) == 1


# -- RAM / disk high ---------------------------------------------------------------
class TestRamAndDisk:
    def test_ram_opens_above_90_and_closes_below_85(self, h: Harness) -> None:
        h.feed("ram_high", sample(0, mem=90.0), 0)
        assert h.all("ram_high") == [], "strictly above 90 %"
        h.feed("ram_high", sample(5, mem=90.5), 5)
        h.feed("ram_high", sample(10, mem=87), 10)
        h.feed("ram_high", sample(15, mem=93), 15)
        assert len(h.all("ram_high")) == 1
        h.feed("ram_high", sample(20, mem=84), 20)
        [a] = h.all("ram_high")
        assert (a["opened_at"], a["closed_at"], a["peak_value"]) == (5, 20, 93)

    def test_disk_alert_per_mount(self, h: Harness) -> None:
        h.feed("disk_high", sample(0, disks=[disk("/", 50), disk("/data", 95), disk("/mnt/t", 91)]), 0)
        assert sorted(a["subject"] for a in h.open("disk_high")) == ["/data", "/mnt/t"]
        h.feed("disk_high", sample(5, disks=[disk("/", 50), disk("/data", 80), disk("/mnt/t", 92)]), 5)
        assert [a["subject"] for a in h.open("disk_high")] == ["/mnt/t"]

    def test_unmounted_filesystem_closes_its_alert(self, h: Harness) -> None:
        h.feed("disk_high", sample(0, disks=[disk("/mnt/t", 97)]), 0)
        h.feed("disk_high", sample(5, disks=[]), 5)
        [a] = h.all("disk_high")
        assert a["closed_at"] == 5 and "subject gone" in a["close_reason"]

    def test_inodes(self, h: Harness) -> None:
        h.feed("disk_inodes_high", sample(0, disks=[disk("/", 10, inodes=96)]), 0)
        assert h.open("disk_inodes_high")[0]["subject"] == "/"


# -- Network lost: "fails 3 times in a row. Closes when it answers again." --------
class TestNetworkLost:
    def test_opens_on_third_consecutive_failure(self, h: Harness) -> None:
        h.feed("network_lost", probes(0, gateway=False, internet=True), 0)
        h.feed("network_lost", probes(15, gateway=False, internet=True), 15)
        assert h.all("network_lost") == []
        h.feed("network_lost", probes(30, gateway=False, internet=True), 30)
        [a] = h.all("network_lost")
        assert a["subject"] == "gateway 10.0.0.1" and a["opened_at"] == 30

    def test_a_success_resets_the_count(self, h: Harness) -> None:
        for t, ok in ((0, False), (15, False), (30, True), (45, False), (60, False)):
            h.feed("network_lost", probes(t, gateway=ok, internet=True), t)
        assert h.all("network_lost") == []

    def test_closes_on_first_answer_and_records_longest_run(self, h: Harness) -> None:
        for t in range(0, 76, 15):
            h.feed("network_lost", probes(t, gateway=False, internet=False), t)
        assert len(h.open("network_lost")) == 2
        h.feed("network_lost", probes(90, gateway=True, internet=True), 90)
        assert h.open("network_lost") == []
        assert {a["peak_value"] for a in h.all("network_lost")} == {6}

    def test_endpoint_down(self, h: Harness) -> None:
        for t in (0, 15, 30):
            h.feed("endpoint_down", probes(t, http=False), t)
        assert h.open("endpoint_down")
        h.feed("endpoint_down", probes(45, http=True), 45)
        assert h.open("endpoint_down") == []


# -- Memory climbing: ">= 9 of last 10 one-minute samples and >= 50 MB" ------------
def feed_rss(h: Harness, pid: int, rss_by_minute: list[float], start_minute: int = 0) -> None:
    """One value per minute; the sampler still ticks every 5 s in between."""
    for i, rss in enumerate(rss_by_minute):
        m = start_minute + i
        for sub in range(0, 60, 5):
            t = m * 60 + sub
            h.feed("memory_climbing", sample(t, procs=[proc(pid, rss_mb=rss)]), t)


class TestMemoryClimbing:
    def test_fires_on_9_of_10_rises_and_50mb(self, h: Harness) -> None:
        rss = [100, 106, 112, 118, 124, 124, 130, 136, 142, 148, 154]  # one flat step, +54 MB
        feed_rss(h, 4242, rss[:10])
        assert h.all("memory_climbing") == [], "needs 10 intervals of history"
        feed_rss(h, 4242, rss[10:], start_minute=10)
        [a] = h.all("memory_climbing")
        assert a["subject"] == "pid 4242 (p)" and "rising in 9/10" in a["message"]
        assert a["opened_at"] == 600

    def test_8_of_10_rises_is_not_enough(self, h: Harness) -> None:
        feed_rss(h, 7, [100, 110, 120, 120, 130, 140, 140, 150, 160, 170, 180])
        assert h.all("memory_climbing") == []

    def test_steady_rise_under_50mb_is_not_enough(self, h: Harness) -> None:
        feed_rss(h, 7, [100 + 4 * i for i in range(11)])  # +40 MB
        assert h.all("memory_climbing") == []

    def test_closes_after_3_flat_samples(self, h: Harness) -> None:
        feed_rss(h, 7, [100 + 8 * i for i in range(11)] + [180, 180])
        assert h.open("memory_climbing"), "2 flat samples: still open"
        feed_rss(h, 7, [180], start_minute=13)
        [a] = h.all("memory_climbing")
        assert a["closed_at"] is not None and a["peak_value"] == 80

    def test_process_exit_closes(self, h: Harness) -> None:
        feed_rss(h, 7, [100 + 8 * i for i in range(11)])
        h.feed("memory_climbing", sample(700, procs=[]), 700)
        assert h.all("memory_climbing")[0]["closed_at"] == 700

    def test_pid_reuse_does_not_inherit_history(self, h: Harness) -> None:
        for i in range(10):
            t = i * 60
            h.feed("memory_climbing", sample(t, procs=[proc(9, rss_mb=100 + 8 * i, ctime=1.0)]), t)
        # Same PID, new process (different start time) keeps the climb going.
        h.feed("memory_climbing", sample(600, procs=[proc(9, rss_mb=500, ctime=2.0)]), 600)
        assert h.all("memory_climbing") == []


# -- Zombie: "stays in state Z for more than 30 s" -----------------------------------
class TestZombie:
    def test_opens_after_30s_and_closes_when_reaped(self, h: Harness) -> None:
        z = proc(77, status="zombie")
        for t in range(0, 31, 5):
            h.feed("zombie", sample(t, procs=[z]), t)
        assert h.all("zombie") == [], "exactly 30 s is not more than 30 s"
        h.feed("zombie", sample(35, procs=[z]), 35)
        assert h.open("zombie")[0]["subject"] == "pid 77 (p)"
        h.feed("zombie", sample(40, procs=[]), 40)
        assert h.all("zombie")[0]["closed_at"] == 40


# -- Process died: "a watch-list process disappears" --------------------------------
WATCHED = "python3 demo/watched_service.py"


class TestProcessDied:
    def test_kill_then_restart(self, h: Harness) -> None:
        h.feed("process_died", sample(0, procs=[proc(500, cmdline=WATCHED, name="python3")]), 0)
        h.feed("process_died", sample(5, procs=[]), 5)
        [a] = h.all("process_died")
        assert a["subject"] == "camera-sim pid 500 (python3)" and "disappeared" in a["message"]
        h.feed("process_died", sample(10, procs=[]), 10)
        assert len(h.all("process_died")) == 1, "opens once"
        h.feed("process_died", sample(15, procs=[proc(501, cmdline=WATCHED, name="python3")]), 15)
        [a] = h.all("process_died")
        assert a["closed_at"] == 15 and "pid 501" in a["close_reason"]
        assert a["peak_value"] == 5, "seconds down at the last sample before the restart"

    def test_turning_zombie_counts_as_dead(self, h: Harness) -> None:
        h.feed("process_died", sample(0, procs=[proc(500, cmdline=WATCHED)]), 0)
        h.feed("process_died", sample(5, procs=[proc(500, cmdline=WATCHED, status="zombie")]), 5)
        assert "became a zombie" in h.open("process_died")[0]["message"]

    def test_worker_pool_one_restart_answers_one_death(self, h: Harness) -> None:
        workers = [proc(p, cmdline=WATCHED) for p in (10, 11, 12)]
        h.feed("process_died", sample(0, procs=workers), 0)
        h.feed("process_died", sample(5, procs=workers[2:]), 5)  # 10 and 11 die
        assert len(h.open("process_died")) == 2
        h.feed("process_died", sample(10, procs=workers[2:] + [proc(13, cmdline=WATCHED)]), 10)
        assert [a["subject"] for a in h.open("process_died")] == ["camera-sim pid 11 (p)"]

    def test_unwatched_processes_are_ignored(self, h: Harness) -> None:
        h.feed("process_died", sample(0, procs=[proc(1, cmdline="nginx")]), 0)
        h.feed("process_died", sample(5, procs=[]), 5)
        assert h.all("process_died") == []

    def test_required_entry_never_started(self, cfg, tmp_path) -> None:
        cfg.watch = [WatchEntry("camera-sim", "watched_service.py", "cmdline", required=True)]
        cfg.rules.process_died.missing_grace = 60
        h = Harness(cfg, str(tmp_path / "r.db"))
        for t in range(0, 56, 5):
            h.feed("process_died", sample(t, procs=[]), t)
        assert h.all("process_died") == [], "grace period after start"
        h.feed("process_died", sample(60, procs=[]), 60)
        [a] = h.all("process_died")
        assert a["subject"] == "camera-sim (not running)" and "not seen since the agent started" in a["message"]
        h.feed("process_died", sample(65, procs=[proc(900, cmdline=WATCHED)]), 65)
        assert h.all("process_died")[0]["closed_at"] == 65

    def test_required_entry_death_is_not_double_reported(self, cfg, tmp_path) -> None:
        cfg.watch = [WatchEntry("camera-sim", "watched_service.py", "cmdline", required=True)]
        h = Harness(cfg, str(tmp_path / "r.db"))
        h.feed("process_died", sample(0, procs=[proc(500, cmdline=WATCHED)]), 0)
        for t in range(5, 300, 5):
            h.feed("process_died", sample(t, procs=[]), t)
        assert [a["subject"] for a in h.all("process_died")] == ["camera-sim pid 500 (p)"]


# -- Per-process CPU / RAM ---------------------------------------------------------
class TestProcessHogs:
    def test_proc_cpu_names_the_culprit(self, h: Harness) -> None:
        for t in range(0, 61, 5):
            h.feed("proc_cpu_high", sample(t, procs=[proc(42, cpu=99, name="burner"), proc(43, cpu=1)]), t)
        assert [a["subject"] for a in h.open("proc_cpu_high")] == ["pid 42 (burner)"]

    def test_proc_mem(self, h: Harness) -> None:
        big = proc(42, rss_mb=3000, name="hog")  # 37.5 % of 8000 MB
        for t in range(0, 31, 5):
            h.feed("proc_mem_high", sample(t, procs=[big]), t)
        assert h.open("proc_mem_high")[0]["subject"] == "pid 42 (hog)"


# -- Restarts: no duplicates, no lost alerts ----------------------------------------
class TestRestartAdoption:
    def test_cpu_alert_survives_restart_without_duplicating(self, h: Harness) -> None:
        run_cpu(h, {t: 95 for t in range(0, 61, 5)})
        h.restart()
        run_cpu(h, {65: 95, 70: 83})  # still high, then in the band: stays open
        assert len(h.all("cpu_high")) == 1 and h.open("cpu_high")
        run_cpu(h, {75: 20, 80: 20, 85: 20})
        assert h.open("cpu_high") == []

    @pytest.mark.parametrize("rule,data,stays", [
        ("ram_high", sample(0, mem=88), True),
        ("zombie", sample(0, procs=[proc(77, status="zombie")]), True),
        ("zombie", sample(0, procs=[]), False),
    ])
    def test_open_alerts_are_adopted(self, h: Harness, rule, data, stays) -> None:
        setup = {"ram_high": [(sample(-5, mem=95), -5)],
                 "zombie": [(sample(t, procs=[proc(77, status="zombie")]), t) for t in range(-60, -4, 5)]}[rule]
        for d, t in setup:
            h.feed(rule, d, t)
        assert h.open(rule)
        h.restart()
        h.feed(rule, data, 0)
        assert bool(h.open(rule)) is stays
        assert len(h.all(rule)) == 1

    def test_watched_process_restarted_while_agent_was_down(self, h: Harness) -> None:
        h.feed("process_died", sample(0, procs=[proc(500, cmdline=WATCHED)]), 0)
        h.feed("process_died", sample(5, procs=[]), 5)
        h.restart()
        h.feed("process_died", sample(10, procs=[proc(600, cmdline=WATCHED)]), 10)
        [a] = h.all("process_died")
        assert a["closed_at"] == 10

    def test_database_refuses_a_second_open_alert(self, h: Harness) -> None:
        import sqlite3
        run_cpu(h, {t: 95 for t in range(0, 61, 5)})
        with pytest.raises(sqlite3.IntegrityError):
            h.db.execute_write(
                "INSERT INTO alerts (rule, key, subject, severity, opened_at) VALUES ('cpu_high','total','x','warn',1)")
