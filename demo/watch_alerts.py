#!/usr/bin/env python3
"""Live terminal view of /health and recent alerts, for the screen recording.

    python3 demo/watch_alerts.py [--url http://127.0.0.1:8765]
"""

import argparse
import json
import time
import urllib.request

COLOR = {"ok": "\033[32m", "warn": "\033[33m", "critical": "\033[31m"}
RESET = "\033[0m"


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=3) as r:
        return json.load(r)


def hms(ts: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "   --   "


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    ap.add_argument("--limit", type=int, default=14)
    a = ap.parse_args()
    while True:
        try:
            h = get(a.url + "/health")
            al = get(f"{a.url}/alerts?limit={a.limit}&since=1h")["alerts"]
        except Exception as exc:  # noqa: BLE001
            print(f"\033[2J\033[Hagent unreachable: {exc}")
            time.sleep(2)
            continue
        s = h["summary"]
        lines = [
            f"{time.strftime('%H:%M:%S')}  health {COLOR.get(h['status'], '')}{h['status'].upper()}{RESET}   "
            f"cpu {s['cpu_pct']}%  ram {s['mem_pct']}%   agent cpu {h['agent']['cpu_pct']}%  "
            f"rss {h['agent']['rss_mb']} MB",
            "",
            f"{'state':6} {'rule':16} {'subject':38} {'opened':8}  {'closed':8}  {'peak':>8}",
        ]
        for x in al:
            state = "OPEN" if x["open"] else "closed"
            col = COLOR["critical"] if x["open"] else ""
            peak = "" if x["peak_value"] is None else f"{x['peak_value']:.1f}"
            lines.append(f"{col}{state:6} {x['rule']:16} {x['subject'][:38]:38} {hms(x['opened_at'])}  "
                         f"{hms(x['closed_at'])}  {peak:>8}{RESET}")
        print("\033[2J\033[H" + "\n".join(lines), flush=True)
        time.sleep(2)


if __name__ == "__main__":
    main()
