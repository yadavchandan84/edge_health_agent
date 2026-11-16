#!/usr/bin/env python3
"""Scenario 2a: push system RAM above 90 % for a while, then release it.

    python3 demo/mem_pressure.py --target 93 --hold 40

Allocates in 32 MB chunks and touches every page so the memory is really resident.
It stops growing at the target and backs off if available memory gets dangerously
low, so it will not trigger the OOM killer on your session.
"""

import argparse
import time

import psutil

CHUNK = 32 * 1024 * 1024
FLOOR = 150 * 1024 * 1024  # never leave less than this available


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=float, default=93.0, help="system RAM percent to reach")
    ap.add_argument("--hold", type=float, default=40.0, help="seconds to hold at the target")
    a = ap.parse_args()
    blocks: list[bytearray] = []
    print(f"[{time.strftime('%H:%M:%S')}] RAM at {psutil.virtual_memory().percent:.1f}%, "
          f"allocating up to {a.target:g}%", flush=True)
    while True:
        vm = psutil.virtual_memory()
        if vm.percent >= a.target or vm.available - CHUNK < FLOOR:
            break
        b = bytearray(CHUNK)
        for i in range(0, CHUNK, 4096):  # touch each page so it is resident
            b[i] = 1
        blocks.append(b)
    vm = psutil.virtual_memory()
    print(f"[{time.strftime('%H:%M:%S')}] holding {len(blocks) * 32} MB, RAM at {vm.percent:.1f}% "
          f"for {a.hold:g}s", flush=True)
    time.sleep(a.hold)
    blocks.clear()
    print(f"[{time.strftime('%H:%M:%S')}] released, RAM at {psutil.virtual_memory().percent:.1f}%", flush=True)


if __name__ == "__main__":
    main()
