#!/usr/bin/env python3
"""Bonus: leave a zombie child for a while. zombie opens after 30 s, closes when reaped.

    python3 demo/zombie.py --seconds 60
"""

import argparse
import os
import time


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=60)
    a = ap.parse_args()
    pid = os.fork()
    if pid == 0:
        os._exit(0)  # child exits at once; parent does not wait() -> state Z
    print(f"[{time.strftime('%H:%M:%S')}] zombie child pid {pid} (parent {os.getpid()}), "
          f"reaping in {a.seconds:g}s", flush=True)
    time.sleep(a.seconds)
    os.waitpid(pid, 0)
    print(f"[{time.strftime('%H:%M:%S')}] reaped", flush=True)


if __name__ == "__main__":
    main()
