#!/usr/bin/env python3
"""Scenario 4: a stand-in for the camera pipeline, on the watch list in config.demo.toml.

    python3 demo/watched_service.py &
    kill -9 <pid>                       # process_died opens with name and PID
    python3 demo/watched_service.py &   # restart: the alert closes
"""

import os
import time

print(f"[{time.strftime('%H:%M:%S')}] camera-sim running as pid {os.getpid()}", flush=True)
while True:
    time.sleep(1)
