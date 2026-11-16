# Demo runbook (fits a 10-minute recording)

This script covers all five required scenarios. It also shows the zombie rule, a
required process that never started, and alerts leaving the box over a webhook that
survives the receiver being down. Several waits overlap (the "not running" check
happens during the CPU wait, and the leak runs in the background during the disk and
kill -9 cases), so the whole run takes about 9 minutes.

## Setup (before you hit record)

Any Linux machine you own: a VM, a Raspberry Pi, or WSL2 Ubuntu. You need root only
for the tmpfs mount and the network toggle.

```bash
cd zeroforg-agent
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
rm -f demo.db*                                  # start with a clean history
```

Open five terminals and a browser:

| Pane | Command |
|---|---|
| T1 agent | `.venv/bin/python -m healthagent -c config.demo.toml` |
| T2 live alerts | `.venv/bin/python demo/watch_alerts.py` |
| T3 scenarios | commands below |
| T4 watched service | *leave empty at first*; `python3 demo/watched_service.py` at 1:10 |
| T5 central receiver | `python3 demo/webhook_receiver.py --secret demo-secret` |
| Browser | `http://localhost:8765/` |

`config.demo.toml` keeps every threshold from the brief. It changes one number:
memory climbing takes a sample every 10 s instead of every 60 s, so a leak is caught
in about 100 s instead of about 10 minutes. The rule logic is the same. It also turns
on the webhook (to T5) and marks `camera-sim` as `required`.

## Script

Times are from the start of the recording. Start T1, T2 and T5 at 0:00. Every open
and close below also shows up in T5, signed and in order.

| Time | Do (T3 unless noted) | Say / show | Expect |
|---|---|---|---|
| 0:00 | Show dashboard, then `curl -s localhost:8765/health \| python3 -m json.tool` | Badge OK, agent CPU and RSS in the header, probe tiles green, `outbox 0` | `"status": "ok"` |
| 0:30 | `python3 demo/cpu_burn.py --seconds 90` | It prints the earliest time `cpu_high` may open. Point at T2: nothing at 0:35, nothing at 1:00 | `proc_cpu_high` per burner and `cpu_high` 60-70 s after start, not before |
| ~1:05 | (burn running) | `camera-sim` is on the watch list as required but was never started | `process_died` "camera-sim (not running)" after the 60 s grace; T5 prints `alert.opened` |
| 1:10 | T4: `python3 demo/watched_service.py` | | "not running" closes |
| 1:40 | (burn still running) | `cpu_high` row in T2, red band on the CPU chart | `opened_at` >= start + 60 s |
| 2:00 | Burn ends | Band closes ~10 s after CPU drops below 80 % | `closed_at` set, peak 100 |
| 2:10 | `.venv/bin/python demo/mem_pressure.py --target 93 --hold 30` | Memory chart crosses the 90 % line | `ram_high` opens within one sample (5 s) |
| 2:50 | It releases memory | | `ram_high` closes on the first sample < 85 % |
| 2:55 | `python3 demo/leak.py --mb 8 --every 10 &` | Note the PID it prints. It keeps running in the background | |
| 3:05 | `sudo bash demo/fill_disk.sh up` then `sudo bash demo/fill_disk.sh fill 95` | Small tmpfs, not the real disk. Filesystems table shows it at 97 % | `disk_high` on `/mnt/zf-disk-test` within 5 s |
| 3:40 | `sudo bash demo/fill_disk.sh clean` | | `disk_high` closes |
| 3:45 | T5: Ctrl-C | "The central service is unreachable now" | |
| 3:50 | `kill -9 $(pgrep -f watched_service.py)` | Header shows `outbox 1 (retrying)` | `process_died` "camera-sim pid N (python3)" within 5 s |
| 4:15 | T4: `python3 demo/watched_service.py` | A restart answers the death | `process_died` closes: "running again as pid M"; `outbox 2` |
| 4:25 | T5: start the receiver again | Both events arrive in order, marked "queued Ns on the box" | outbox back to 0 |
| ~4:40 | (leak still running) | T2 / Open alerts name the leak PID | `memory_climbing` "pid N (python3) RSS +80 MB over the last 10 samples" |
| 4:50 | `kill %1` (the leak) | | `memory_climbing` closes: subject gone |
| 5:00 | `sudo bash demo/net_toggle.sh 75` | Probe tiles turn red. TCP stops; DNS stops on a real box | `network_lost` for gateway and internet after the 3rd failed round (30-45 s in) |
| 6:15 | Toggle restores the network | DNS & TCP chart shows the gap, loss chart shows the 100 % block | `network_lost` closes on the first good round |
| 6:30 | `python3 demo/zombie.py --seconds 45` | | `zombie` opens after > 30 s, closes when reaped |
| 7:30 | Click "Last 24 h", then show the alert history table | Every alert has opened, closed, duration and peak | |
| 7:50 | `curl -s 'localhost:8765/alerts?open=false&limit=20' \| python3 -m json.tool \| less` | The same data over the API, with ISO timestamps | |
| 8:10 | Point at the "Agent self" charts | CPU ~1 % of one core, RSS ~34 MB | Under 5 % / 150 MB |
| 8:20 | `.venv/bin/pytest` | Every rule above is also pinned down by a test with a fake clock | `98 passed` in ~6 s |

## Network toggle modes

- `MODE=firewall` (default when `iptables` exists) drops all outbound traffic except
  loopback and the dashboard port. The browser keeps working and it is safe over SSH.
- `MODE=link IFACE=eth0` takes the interface down. Use it on a local VM or WSL2,
  never over SSH. On WSL2 the host answers DNS through its own tunnel, so DNS keeps
  resolving while TCP, ping and HTTP fail. On a real box all five probes fail.

## If something does not fire

- `curl -s localhost:8765/system | python3 -m json.tool | less` shows what the agent
  sees right now: disks, interfaces, probe results with error text.
- `/health` lists `problems` (sampler or prober stalled), `stuck_probes`, and the
  outbox state under `agent.notify`.
- T1 logs every `ALERT OPEN` and `ALERT CLOSE` with its reason, and when webhook
  delivery starts failing or recovers.
