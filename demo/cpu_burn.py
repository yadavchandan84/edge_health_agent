#!/usr/bin/env python3
"""Scenario 1: busy-loop on every core. cpu_high must open 60-65 s after start, not before.

    python3 demo/cpu_burn.py --seconds 100
(stress-ng --cpu 0 --timeout 100s does the same if installed.)
"""

import argparse
import multiprocessing as mp
import os
import time


def burn(stop_at: float) -> None:
    x = 0
    while time.time() < stop_at:
        for _ in range(100_000):
            x = (x * 31 + 7) % 1_000_003


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=100)
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    a = ap.parse_args()
    start = time.time()
    stop_at = start + a.seconds
    print(f"[{time.strftime('%H:%M:%S')}] burning {a.workers} cores for {a.seconds:g}s "
          f"(expect cpu_high at ~{time.strftime('%H:%M:%S', time.localtime(start + 60))})", flush=True)
    procs = [mp.Process(target=burn, args=(stop_at,)) for _ in range(a.workers)]
    for p in procs:
        p.start()
    try:
        for p in procs:
            p.join()
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()
    print(f"[{time.strftime('%H:%M:%S')}] stopped after {time.time() - start:.0f}s", flush=True)


if __name__ == "__main__":
    main()
