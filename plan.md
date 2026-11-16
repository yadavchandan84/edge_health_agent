# Plan: Edge Health Agent

## 1. Goal

One Python process that runs on an unattended edge box and notices first when the
box is in trouble. It samples the machine every 5 s, probes the network every 15 s,
keeps 7 days of history in SQLite, raises deterministic alerts, and serves an HTTP
API plus a dashboard.

Hard constraints from the brief:

| Constraint | How the design meets it |
|---|---|
| Sample every 5 s, probe every 15 s | Two independent threads on drift-free monotonic schedules |
| A hung DNS lookup must never stall the sampler | Probes run on their own thread; every probe runs in a worker with a hard deadline; a probe that is still hung is not resubmitted |
| Rates from counter deltas, handle resets | `RateTracker`: per-key baseline, skips the interval when a counter goes backwards, reseeds |
| Alerts open once, close once, no flapping | Rules emit FIRING / HOLD / CLEAR; hysteresis bands; a partial unique index in SQLite makes a duplicate open alert impossible |
| < 5 % of one core, < 150 MB RAM | No web framework, no pandas; stdlib HTTP server; cheap `/proc` parsing; the agent measures and charts its own CPU and RSS |
| 7 days of history without growing forever | Raw 5 s data kept 24 h, 1-minute rollups kept 7 days, closed alerts kept 30 days; periodic pruning and WAL checkpoint |

## 2. Architecture

```
                  +-------------------- healthagent (one process) --------------------+
                  |                                                                  |
 /proc, /sys,     |  sampler thread (5 s)        prober thread (15 s)                |
 psutil  -------> |  system + network +          DNS, TCP, ping gw/internet, HTTP     |
                  |  process collectors          (each in a worker w/ deadline)       |
                  |        |                            |                            |
                  |        v                            v                            |
                  |   rule engine  <---------------------                            |
                  |   (pure, clock injected)                                         |
                  |        |  FIRING / HOLD / CLEAR signals                           |
                  |        v                                                         |
                  |   alert manager ---> SQLite (WAL) <--- maintenance thread        |
                  |                       ^   samples, 1m rollups,   (rollup, prune)  |
                  |                       |   series, alerts                          |
                  |   HTTP server --------+  /health /metrics /alerts /processes      |
                  |   (stdlib, threaded)     /system  and the dashboard at /          |
                  +------------------------------------------------------------------+
```

## 3. Modules

| Module | Responsibility |
|---|---|
| `healthagent/config.py` | Typed dataclass config loaded from TOML (stdlib `tomllib`) with validated defaults |
| `healthagent/rates.py` | Counter-delta rate tracker with reset handling |
| `healthagent/collectors/system.py` | CPU per core and total, iowait, load, uptime, RAM, swap, disk usage and inodes per mount, disk I/O rates |
| `healthagent/collectors/network.py` | Per-interface bandwidth, drops and errors; TCP states and listening ports from `/proc/net/tcp*` (psutil fallback) |
| `healthagent/collectors/processes.py` | Per-process CPU (from `cpu_times` deltas), RSS, FDs, state; cmdline cached per process lifetime |
| `healthagent/probes.py` | DNS time, TCP connect time, ping loss and RTT to gateway and internet, HTTP status and latency; default gateway discovery |
| `healthagent/rules.py` | Rule building blocks (sustained threshold with hysteresis, consecutive failures, memory trend, zombie age, watch list) |
| `healthagent/alerts.py` | Turns signals into open / update peak / close, persisted; adopts open alerts after a restart |
| `healthagent/storage.py` | SQLite schema, batched writes, range queries with bucketing, rollups, retention |
| `healthagent/api.py` | Read-only HTTP API and dashboard, optional bearer token |
| `healthagent/notify.py` | Webhook delivery of the alert outbox (phase 2) |
| `healthagent/agent.py` | Wires threads together, self-monitoring, graceful shutdown |
| `healthagent/static/index.html` | Single-file dashboard; dependency-free canvas charts so it works when the uplink is down |
| `demo/` | Scripts for the five required scenarios plus zombie and runbook |

## 4. Alert rules (decisions written down)

| Rule | Opens | Closes | Why these numbers |
|---|---|---|---|
| `cpu_high` | Total CPU > 85 % on every sample for >= 60 s; a missed sample (gap > 2.5 x interval) counts as a break | < 80 % for 10 s | 60 s is measured from the first breaching sample, so it can never open at second 5. 5-point hysteresis stops flapping at 85 % |
| `ram_high` | RAM used (total - available) > 90 % | < 85 % | `available` is what the kernel can actually give to apps; page cache does not count |
| `disk_high` | Any real mounted filesystem > 90 % (subject = mount) | < 85 % | Pseudo filesystems and always-full `squashfs` snaps are excluded; tmpfs is included so the demo works |
| `disk_inodes_high` | Inodes > 90 % on a mount | < 85 % | Millions of small files fill a disk without using bytes |
| `network_lost` | Gateway or internet ping fails 3 rounds in a row (subject = host) | First successful round | Exactly as specified. Partial loss is recorded as a metric, not an outage |
| `endpoint_down` | Configured HTTP endpoint errors or returns 5xx 3 rounds in a row | First good response | Same shape as network_lost |
| `memory_climbing` | RSS rose in >= 9 of the last 10 one-minute samples and by >= 50 MB in total (subject = PID) | Process exits, or RSS has not grown over the last 3 samples | Spec numbers. 50 MB filters normal allocator noise; the 3-sample flat window stops flapping when a leak pauses briefly |
| `process_died` | A watch-list process that was seen running disappears (subject = name + PID) | A new instance of that watch entry appears | One replacement closes one death, so worker pools are handled |
| `zombie` | A process stays in state Z for > 30 s (subject = PID) | Reaped | Spec |
| `proc_cpu_high` | One process > 90 % of one core for 60 s | < 70 % for 10 s | Names the culprit behind `cpu_high` |
| `proc_mem_high` | One process RSS > 30 % of RAM for 30 s | < 25 % | Names the culprit behind `ram_high` |

Engine contract:

- A rule returns a signal per subject it tracks: FIRING, HOLD (in the hysteresis band
  or still building up) or CLEAR.
- FIRING opens the alert if none is open for `(rule, key)`, otherwise raises its peak.
- An open alert whose subject the rule no longer reports (process exited, mount gone)
  is closed with a reason.
- SQLite enforces at most one open alert per `(rule, key)` with a partial unique index.
- Time is injected, so every rule is deterministic and replayable.

## 5. Storage and retention

- `series(id, name, subject, unit)` + `samples(series_id, ts, value)` WITHOUT ROWID,
  primary key `(series_id, ts)`: compact rows and indexed range scans.
- `samples_1m(series_id, ts, avg, min, max, n)` built every minute from raw data.
- Retention: raw 24 h, rollups 7 days, closed alerts 30 days. Prune per series using
  the primary key, then checkpoint the WAL.
- Estimated steady state for ~100 series: about 40 MB raw + 35 MB rollups.
- `/metrics` picks raw or rollups by range and buckets by `step`, so the 24 h chart is
  ~300 points instead of 17,000.

## 6. Budget

- psutil `process_iter` with a fixed attribute list (one `oneshot` per process).
- TCP states parsed from `/proc/net/tcp` instead of `psutil.net_connections`, which
  walks every `/proc/<pid>/fd` and is the most expensive call in psutil.
- cmdline read once per process lifetime.
- One batched transaction per tick, `synchronous=NORMAL`.
- The agent publishes `agent.cpu_pct`, `agent.rss_mb` and `agent.loop_ms` so the budget
  is visible on the dashboard during the demo.

## 7. Demo plan (fits a 10-minute recording)

`config.demo.toml` keeps every spec threshold. It shortens only the memory-climbing
sample period from 60 s to 10 s so the leak is detected in about 2 minutes instead
of 10 (the brief allows other numbers if defended).

| # | Scenario | Script | Expected |
|---|---|---|---|
| 1 | CPU on every core | `demo/cpu_burn.py` | `cpu_high` opens 60-65 s after start, closes ~10 s after stop |
| 2 | Memory pressure, then slow leak | `demo/mem_pressure.py`, `demo/leak.py` | `ram_high` opens and closes; `memory_climbing` names the leaker PID |
| 3 | Fill a tmpfs | `demo/fill_disk.sh` | `disk_high` opens for the tmpfs mount, closes after cleanup |
| 4 | kill -9 a watched process | `demo/watched_service.py` | `process_died` names it with PID; restart closes it |
| 5 | Network down, then up | `demo/net_toggle.sh` | `network_lost` opens after 3 failed rounds, closes on recovery; DNS/TCP charts show the gap |
| + | Zombie | `demo/zombie.py` | `zombie` opens after 30 s |

## 8. Milestones

1. Config, storage, rate tracker
2. Collectors (system, network, processes)
3. Probes with deadlines and the hung-probe guard
4. Rule engine and alert manager
5. API and dashboard
6. Demo scripts and runbook
7. Verify on Linux (WSL2 Ubuntu): every scenario, budget, retention
8. README

## 9. Phase 2 (done after the first version was verified)

| Item | Why | Result |
|---|---|---|
| Test suite (pytest) | Back the "deterministic rules" claim with something a reviewer can run | 98 tests, ~6 s, Linux and Windows |
| Alerts off the box | The agent exists for failures nobody sees; an alert that never leaves the box does not help | Webhook with a transactional SQLite outbox, ordered, signed, retried with backoff, flushed when the uplink returns |
| `required` watch entries | A pipeline that never came back after a reboot was invisible | `process_died` "name (not running)" after a 60 s grace |
| CI and lint | Every push proves the tests still pass | GitHub Actions on Python 3.11-3.13: ruff, pytest, live smoke test |
| Pure parsers | ping and `/proc/net/route` parsing tested against real outputs (iputils, BusyBox, Windows) | `parse_ping`, `parse_proc_route` |

Bugs found along the way and fixed with a regression test:

- **Rounded timestamps.** Sample timestamps were rounded up, which hid the newest
  point from `until=now` queries.
- **Reused outbox ids.** SQLite reused outbox id 1 once the queue drained, so a
  receiver deduplicating on it dropped a real alert. Fixed with `AUTOINCREMENT` plus
  a random per-event `uid`.

## 10. Still out of scope

- Packaging as a .deb with the systemd unit
- Fan-out to several notification targets (belongs in the central receiver)
- Live config reload (a restart is safe: open alerts are adopted)
