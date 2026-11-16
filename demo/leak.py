#!/usr/bin/env python3
"""Scenario 2b: a process that leaks slowly. memory_climbing must name its PID.

    python3 demo/leak.py --mb 8 --every 10      # pairs with config.demo.toml (10 s samples)
    python3 demo/leak.py --mb 8 --every 60      # production config (60 s samples, ~10 min)

8 MB per sample over 10 samples = 80 MB, comfortably over the 50 MB threshold.
"""

import argparse
import os
import time


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mb", type=float, default=8.0)
    ap.add_argument("--every", type=float, default=10.0)
    ap.add_argument("--max-mb", type=float, default=600.0)
    a = ap.parse_args()
    hoard: list[bytearray] = []
    print(f"[{time.strftime('%H:%M:%S')}] leak.py pid {os.getpid()}: +{a.mb:g} MB every {a.every:g}s", flush=True)
    while len(hoard) * a.mb < a.max_mb:
        b = bytearray(int(a.mb * 1024 * 1024))
        for i in range(0, len(b), 4096):
            b[i] = 1
        hoard.append(b)
        print(f"[{time.strftime('%H:%M:%S')}] holding {len(hoard) * a.mb:.0f} MB", flush=True)
        time.sleep(a.every)
    print("reached --max-mb, holding steady (the alert should close after 3 flat samples)", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
