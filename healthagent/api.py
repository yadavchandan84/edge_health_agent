"""Read-only HTTP API and dashboard on the standard library's threaded HTTP server.

No framework: it keeps the agent's RSS and CPU low and there is nothing to install
on the box besides psutil.

  GET /                     dashboard
  GET /health               ok | warn | critical, open alerts, agent self-stats
  GET /metrics              list series; with ?name= returns points
        name=cpu.total,mem.percent   comma list, trailing * for prefix (disk.*)
        subject=/                    only this subject (mount, core, interface...)
        since=1h | 2026-11-16T10:00 | 1763290000   default 1h
        until=...                    default now
        step=60                      bucket seconds (default: auto, <= ~600 points)
        agg=avg|max|min              bucket aggregate (default avg)
  GET /alerts?open=true|false&rule=&since=&limit=
  GET /processes?sort=cpu|mem|rss|fds|threads|pid|name&limit=&q=
  GET /system               latest full snapshot (disks, interfaces, ports, probes)
"""

from __future__ import annotations

import gzip
import hmac
import json
import logging
import math
import re
import sys
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from .alerts import alert_view as _alert_view
from .alerts import iso

if TYPE_CHECKING:
    from .agent import Agent

log = logging.getLogger(__name__)
_DURATION = re.compile(r"^-?(\d+(?:\.\d+)?)\s*(s|m|h|d|w)$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
NICE_STEPS = (5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 86400)


class BadRequest(ValueError):
    pass


def parse_time(value: str | None, now: float, default: float) -> float:
    if value is None or value == "":
        return default
    v = value.strip()
    m = _DURATION.match(v)
    if m:
        return now - float(m.group(1)) * _UNITS[m.group(2)]
    try:
        f = float(v)
        if f > 1e12:  # milliseconds
            f /= 1000
        return f
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadRequest(f"cannot parse time {value!r}: use 1h, epoch seconds or ISO 8601") from exc
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive means the box's local time
    return dt.timestamp()


def _clean(obj: Any) -> Any:
    """JSON has no NaN/Infinity."""
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def _bool(v: str | None) -> bool | None:
    if v is None or v.lower() in ("", "all", "any"):
        return None
    if v.lower() in ("1", "true", "yes", "open"):
        return True
    if v.lower() in ("0", "false", "no", "closed"):
        return False
    raise BadRequest("open must be true, false or all")


def _int(v: str | None, default: int, lo: int, hi: int, name: str) -> int:
    if v is None or v == "":
        return default
    try:
        n = int(v)
    except ValueError as exc:
        raise BadRequest(f"{name} must be an integer") from exc
    return max(lo, min(hi, n))


class Api:
    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self.cfg = agent.cfg
        self._dashboard = resources.files("healthagent").joinpath("static/index.html").read_bytes()

    # -- endpoints ---------------------------------------------------------------
    def health(self, q: dict[str, str]) -> dict[str, Any]:
        h = self.agent.health()
        now = time.time()
        h["checked_at_iso"] = iso(h["checked_at"])
        h["open_alerts"] = [_alert_view(a, now) for a in h["open_alerts"]]
        return h

    def metrics(self, q: dict[str, str]) -> dict[str, Any]:
        now = time.time()
        if not q.get("name"):
            return {"series": self.agent.db.list_series(),
                    "hint": "add ?name=cpu.total&since=1h (comma list, trailing * for a prefix)"}
        names = [n.strip() for n in q["name"].split(",") if n.strip()][:20]
        since = parse_time(q.get("since"), now, now - 3600)
        until = parse_time(q.get("until"), now, now)
        if until <= since:
            raise BadRequest("until must be after since")
        span = until - since
        base = int(self.cfg.agent.sample_interval)
        if q.get("step"):
            step = _int(q.get("step"), base, base, 86400, "step")
        else:
            want = span / 600
            step = next((s for s in NICE_STEPS if s >= want and s >= base), NICE_STEPS[-1])
        step = max(step, math.ceil(span / 5000))  # hard cap on points per series
        if since < now - self.cfg.retention.raw_hours * 3600:
            step = max(step, 60)  # only 1-minute rollups exist that far back
        agg = q.get("agg", "avg")
        if agg not in ("avg", "max", "min"):
            raise BadRequest("agg must be avg, max or min")
        series = self.agent.db.query(names, q.get("subject"), since, until, step, agg,
                                     self.cfg.retention.raw_hours * 3600)
        return {"since": since, "until": until, "step": step, "agg": agg,
                "since_iso": iso(since), "until_iso": iso(until), "series": series}

    def alerts(self, q: dict[str, str]) -> dict[str, Any]:
        now = time.time()
        open_only = _bool(q.get("open"))
        limit = _int(q.get("limit"), 100, 1, 1000, "limit")
        since = parse_time(q.get("since"), now, 0) if q.get("since") else None
        rows = self.agent.db.alerts(open_only, limit, q.get("rule") or None, since)
        return {"count": len(rows), "alerts": [_alert_view(a, now) for a in rows]}

    def processes(self, q: dict[str, str]) -> dict[str, Any]:
        s = self.agent.latest
        if s is None:
            return {"ts": None, "processes": []}
        sort = q.get("sort", "cpu")
        keys = {"cpu": "cpu_pct", "mem": "rss", "rss": "rss", "fds": "fds", "threads": "threads",
                "pid": "pid", "name": "name"}
        if sort not in keys:
            raise BadRequest(f"sort must be one of {', '.join(keys)}")
        attr = keys[sort]
        limit = _int(q.get("limit"), self.cfg.collect.process_limit, 1, 5000, "limit")
        needle = (q.get("q") or "").lower()
        procs = [p for p in s.processes
                 if not needle or needle in p.name.lower() or needle in p.cmdline.lower() or needle == str(p.pid)]
        reverse = sort not in ("pid", "name")
        if attr == "name":
            procs.sort(key=lambda p: p.name.lower())
        else:
            procs.sort(key=lambda p: (getattr(p, attr) is not None, getattr(p, attr) or 0), reverse=reverse)

        flags: dict[int, set[str]] = {}
        for a in self.agent.alerts.snapshot():
            m = re.search(r"pid (\d+)", a["subject"])
            if m:
                flags.setdefault(int(m.group(1)), set()).add(a["rule"])
        watched_rule = next((r for r in self.agent.rules if r.name == "process_died"), None)
        out = []
        for p in procs[:limit]:
            f = set(flags.get(p.pid, ()))
            if p.status == "zombie":
                f.add("zombie")
            if watched_rule and any(watched_rule.matches(w, p) for w in self.cfg.watch):
                f.add("watched")
            out.append({
                "pid": p.pid, "name": p.name, "cmdline": p.cmdline, "user": p.username, "status": p.status,
                "cpu_pct": p.cpu_pct, "rss_mb": round(p.rss / 1048576, 1) if p.rss is not None else None,
                "mem_pct": p.mem_pct, "fds": p.fds, "threads": p.threads,
                "started_at": iso(p.ctime), "flags": sorted(f),
            })
        return {"ts": s.ts, "ts_iso": iso(s.ts), "sort": sort, "total": len(s.processes), "processes": out}

    def system(self, q: dict[str, str]) -> dict[str, Any]:
        s = self.agent.latest
        rnd = self.agent.latest_probes
        latest = {}
        if s:
            for m in s.metrics:
                latest.setdefault(m.name, {})[m.subject or "_"] = round(m.value, 3)
        return {
            "ts": s.ts if s else None,
            "ts_iso": iso(s.ts) if s else None,
            "metrics": latest,
            "disks": [_slots(d) for d in (s.disks if s else [])],
            "interfaces": s.interfaces if s else [],
            "listening": s.listening if s else [],
            "probes": {
                "ts": rnd.ts if rnd else None,
                "ts_iso": iso(rnd.ts) if rnd else None,
                "results": {n: _slots(r) for n, r in rnd.results.items()} if rnd else {},
            },
            "config": {
                "name": self.cfg.agent.name,
                "notify": bool(self.cfg.notify.webhook_url),
                "sample_interval": self.cfg.agent.sample_interval,
                "probe_interval": self.cfg.agent.probe_interval,
                "watch": [{"name": w.name, "match": w.match, "by": w.by, "required": w.required}
                          for w in self.cfg.watch],
                "rules": {r.name: r.severity for r in self.agent.rules},
                "thresholds": {
                    "cpu_high": self.cfg.rules.cpu_high.open_above,
                    "ram_high": self.cfg.rules.ram_high.open_above,
                    "disk_high": self.cfg.rules.disk_high.open_above,
                },
            },
        }


def _slots(obj: Any) -> dict[str, Any]:
    return {k: getattr(obj, k) for k in obj.__slots__}


def make_handler(api: Api, token: str) -> type[BaseHTTPRequestHandler]:
    routes = {"/health": api.health, "/metrics": api.metrics, "/alerts": api.alerts,
              "/processes": api.processes, "/system": api.system}

    class Handler(BaseHTTPRequestHandler):
        server_version = "healthagent"
        sys_version = ""
        timeout = 10  # a slow or stuck client cannot hold a thread forever

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            if len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
                body = gzip.compress(body, compresslevel=5)
                gz = True
            else:
                gz = False
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj: Any) -> None:
            body = json.dumps(_clean(obj), separators=(",", ":")).encode()
            self._send(status, body, "application/json")

        def _authorized(self, q: dict[str, str]) -> bool:
            if not token:
                return True
            given = q.get("token", "")
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                given = auth[7:]
            return hmac.compare_digest(given.encode(), token.encode())

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            url = urlsplit(self.path)
            q = {k: v[-1] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
            path = url.path.rstrip("/") or "/"
            try:
                if path in ("/", "/index.html"):
                    self._send(200, api._dashboard, "text/html; charset=utf-8")
                    return
                fn = routes.get(path)
                if fn is None:
                    self._json(404, {"error": "not found", "endpoints": sorted(routes)})
                    return
                if not self._authorized(q):
                    self._json(401, {"error": "missing or wrong token"})
                    return
                self._json(200, fn(q))
            except BadRequest as exc:
                self._json(400, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                log.exception("error serving %s", self.path)
                self._json(500, {"error": "internal error"})

        def _method_not_allowed(self) -> None:
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read-only API: GET only"})

        do_POST = do_PUT = do_DELETE = do_PATCH = _method_not_allowed  # noqa: N815

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, SO_REUSEADDR lets a second process bind a port that is already in
    # use, so two agents would silently share it. There, binding must fail instead.
    allow_reuse_address = not sys.platform.startswith("win")


def serve(agent: Agent, host: str, port: int) -> ThreadingHTTPServer:
    api = Api(agent)
    server = _Server((host, port), make_handler(api, agent.cfg.api.token))
    t = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    t.start()
    return server
