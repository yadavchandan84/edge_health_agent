#!/usr/bin/env python3
"""Stand-in for the central alert service: prints every webhook the agent sends.

    python3 demo/webhook_receiver.py                      # listen on 127.0.0.1:9999
    python3 demo/webhook_receiver.py --secret s3cret      # also verify signatures
    python3 demo/webhook_receiver.py --fail-for 60        # answer 503 for 60 s first,
                                                          # to watch the agent queue and retry

Drops duplicates by X-Healthagent-Delivery, as a real receiver should
(delivery is at-least-once).
"""

import argparse
import hashlib
import hmac
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COLOR = {"alert.opened": "\033[31m", "alert.closed": "\033[32m"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--secret", default="")
    ap.add_argument("--fail-for", type=float, default=0, help="return 503 for this many seconds")
    a = ap.parse_args()
    fail_until = time.time() + a.fail_for
    seen: set[str] = set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            stamp = time.strftime("%H:%M:%S")
            if time.time() < fail_until:
                delivery = (self.headers.get("X-Healthagent-Delivery") or "")[:8]
                print(f"[{stamp}] 503 (simulated outage) for delivery {delivery}", flush=True)
                self.send_response(503)
                self.end_headers()
                return
            if a.secret:
                want = "sha256=" + hmac.new(a.secret.encode(), body, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(want, self.headers.get("X-Healthagent-Signature", "")):
                    print(f"[{stamp}] 401 bad signature", flush=True)
                    self.send_response(401)
                    self.end_headers()
                    return
            delivery = self.headers.get("X-Healthagent-Delivery", "")
            self.send_response(204)
            self.end_headers()
            if delivery in seen:
                print(f"[{stamp}] duplicate delivery {delivery[:8]} ignored", flush=True)
                return
            seen.add(delivery)
            ev = json.loads(body)
            al = ev["alert"]
            lag = time.time() - ev["queued_at"]
            when = al["closed_at_iso"] if ev["event"] == "alert.closed" else al["opened_at_iso"]
            print(f"[{stamp}] {COLOR.get(ev['event'], '')}{ev['event']:13}\033[0m {delivery[:8]} "
                  f"{ev['source']:<12} {al['rule']:16} {al['subject'][:34]:34} at {when}"
                  f"{f'  (queued {lag:.0f}s on the box)' if lag > 10 else ''}", flush=True)

    print(f"receiving on http://{a.host}:{a.port}/  (any path)", flush=True)
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
