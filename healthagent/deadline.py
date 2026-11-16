"""Run blocking calls with a hard deadline, without leaking threads.

Some calls cannot be interrupted from Python: `getaddrinfo` on a dead resolver,
`statvfs` on a stale NFS mount, `connect` while the kernel retries SYNs. We run them
on a small pool and stop waiting after the deadline. The worker may stay blocked,
so we remember it: while a call for the same key is still stuck we do not start a
second one. That bounds the number of stuck threads to one per key instead of one
per tick, which is how agents usually die slowly on a flaky network.
"""

from __future__ import annotations

import concurrent.futures as cf
import threading
from collections.abc import Callable
from typing import Any


class Hung(Exception):
    """The previous call for this key has not returned yet."""


class DeadlineRunner:
    def __init__(self, max_workers: int, name: str) -> None:
        self._pool = cf.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=name)
        self._inflight: dict[str, cf.Future[Any]] = {}
        self._lock = threading.Lock()

    def submit(self, key: str, fn: Callable[..., Any], *args: Any) -> cf.Future[Any]:
        with self._lock:
            prev = self._inflight.get(key)
            if prev is not None and not prev.done():
                fut: cf.Future[Any] = cf.Future()
                fut.set_exception(Hung(f"{key}: previous call still running"))
                return fut
            fut = self._pool.submit(fn, *args)
            self._inflight[key] = fut
            return fut

    def run(self, key: str, fn: Callable[..., Any], *args: Any, timeout: float) -> Any:
        """Call fn(*args); raise TimeoutError after `timeout`, Hung if still stuck."""
        return self.submit(key, fn, *args).result(timeout=timeout)

    def stuck(self) -> list[str]:
        with self._lock:
            return sorted(k for k, f in self._inflight.items() if not f.done())

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
