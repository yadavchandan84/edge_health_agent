"""Typed configuration loaded from a TOML file.

Every field has a default, so an empty file (or no file) gives a working agent.
Unknown keys are rejected so typos do not silently fall back to defaults.
"""

from __future__ import annotations

import dataclasses
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AgentConfig:
    # Identifies this box in /health and in webhook payloads. Empty = hostname.
    name: str = ""
    sample_interval: float = 5.0
    probe_interval: float = 15.0
    db_path: str = "healthagent.db"
    log_level: str = "INFO"


@dataclass
class NotifyConfig:
    # POST every alert open/close here as JSON. Empty disables notifications.
    webhook_url: str = ""
    # If set, each request carries X-Healthagent-Signature: sha256=<HMAC of the body>.
    secret: str = ""
    timeout: float = 5.0
    # Undelivered notifications older than this are dropped (the alert itself stays).
    outbox_days: float = 7.0


@dataclass
class ApiConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    # Optional bearer token. Empty means no auth (fine when bound to localhost).
    token: str = ""


@dataclass
class RetentionConfig:
    raw_hours: float = 24.0
    rollup_days: float = 7.0
    closed_alert_days: float = 30.0


@dataclass
class CollectConfig:
    exclude_interfaces: list[str] = field(default_factory=lambda: ["lo"])
    exclude_disk_prefixes: list[str] = field(default_factory=lambda: ["loop", "ram", "zram"])
    exclude_fstypes: list[str] = field(default_factory=lambda: [
        "proc", "sysfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "pstore", "bpf",
        "tracefs", "debugfs", "securityfs", "configfs", "fusectl", "mqueue",
        "hugetlbfs", "autofs", "binfmt_misc", "squashfs", "nsfs", "ramfs",
        "efivarfs", "iso9660", "rpc_pipefs", "selinuxfs", "fuse.gvfsd-fuse",
        "fuse.portal", "overlay", "nfsd",
        "9p", "drvfs",  # host drives shared into a VM / WSL: not this box's disks
    ])
    exclude_mount_prefixes: list[str] = field(default_factory=lambda: [
        "/proc", "/sys", "/snap", "/run/user", "/run/credentials", "/run/lock", "/run/shm",
        "/var/lib/docker", "/usr/lib/wsl", "/mnt/wsl", "/mnt/wslg", "/tmp/.X11-unix", "/init",
    ])
    # Rows returned by /processes when no limit is given.
    process_limit: int = 50


@dataclass
class WatchEntry:
    name: str
    # Match against the process name exactly ("name") or a cmdline substring ("cmdline").
    match: str = ""
    by: str = "cmdline"
    # Also alert when no instance is running at all (e.g. it never came up after a
    # reboot), not only when a running instance exits.
    required: bool = False


@dataclass
class ProbeConfig:
    target_host: str = "cloudflare.com"
    tcp_port: int = 443
    internet_host: str = "1.1.1.1"
    # "auto" reads the default route; set an IP to override.
    gateway: str = "auto"
    # A 204 endpoint: tiny response, built for connectivity checks.
    http_url: str = "https://www.google.com/generate_204"
    timeout: float = 2.0
    ping_count: int = 3


@dataclass
class ThresholdRule:
    enabled: bool = True
    open_above: float = 90.0
    clear_below: float = 85.0
    open_for: float = 0.0
    clear_for: float = 0.0
    severity: str = "warn"


@dataclass
class FailureRule:
    enabled: bool = True
    failures: int = 3
    severity: str = "critical"


@dataclass
class MemoryClimbRule:
    enabled: bool = True
    sample_interval: float = 60.0
    window: int = 10
    min_rises: int = 9
    min_growth_mb: float = 50.0
    flat_samples_to_clear: int = 3
    severity: str = "warn"


@dataclass
class ZombieRule:
    enabled: bool = True
    min_age: float = 30.0
    severity: str = "warn"


@dataclass
class ProcessDiedRule:
    enabled: bool = True
    severity: str = "critical"
    # For watch entries with required = true: how long after agent start (or after
    # the entry was last seen) to wait before "not running" becomes an alert. Gives
    # the service time to come up after a reboot.
    missing_grace: float = 60.0


def _t(**kw: Any) -> Any:
    return field(default_factory=lambda: ThresholdRule(**kw))


@dataclass
class RulesConfig:
    cpu_high: ThresholdRule = _t(open_above=85, clear_below=80, open_for=60, clear_for=10, severity="warn")
    ram_high: ThresholdRule = _t(open_above=90, clear_below=85, severity="critical")
    disk_high: ThresholdRule = _t(open_above=90, clear_below=85, severity="critical")
    disk_inodes_high: ThresholdRule = _t(open_above=90, clear_below=85, severity="critical")
    proc_cpu_high: ThresholdRule = _t(open_above=90, clear_below=70, open_for=60, clear_for=10, severity="warn")
    proc_mem_high: ThresholdRule = _t(open_above=30, clear_below=25, open_for=30, severity="warn")
    network_lost: FailureRule = field(default_factory=lambda: FailureRule(failures=3, severity="critical"))
    endpoint_down: FailureRule = field(default_factory=lambda: FailureRule(failures=3, severity="warn"))
    memory_climbing: MemoryClimbRule = field(default_factory=MemoryClimbRule)
    zombie: ZombieRule = field(default_factory=ZombieRule)
    process_died: ProcessDiedRule = field(default_factory=ProcessDiedRule)


@dataclass
class Config:
    agent: AgentConfig = field(default_factory=AgentConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    collect: CollectConfig = field(default_factory=CollectConfig)
    probes: ProbeConfig = field(default_factory=ProbeConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    watch: list[WatchEntry] = field(default_factory=list)


class ConfigError(ValueError):
    pass


def _build(cls: type, data: dict[str, Any], path: str) -> Any:
    """Recursively build a dataclass, merging `data` over the defaults."""
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a table")
    obj = cls()
    known = {f.name: f for f in dataclasses.fields(cls)}
    for key, value in data.items():
        if key not in known:
            raise ConfigError(f"{path}.{key}: unknown setting")
        current = getattr(obj, key)
        if dataclasses.is_dataclass(current):
            setattr(obj, key, _build(type(current), value, f"{path}.{key}"))
        elif key == "watch":
            setattr(obj, key, [_build_watch(v, f"{path}.watch[{i}]") for i, v in enumerate(value)])
        else:
            if isinstance(current, float) and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)
            if current is not None and not isinstance(value, type(current)):
                raise ConfigError(f"{path}.{key}: expected {type(current).__name__}, got {type(value).__name__}")
            setattr(obj, key, value)
    return obj


def _build_watch(data: dict[str, Any], path: str) -> WatchEntry:
    if not isinstance(data, dict) or "name" not in data:
        raise ConfigError(f"{path}: needs at least a name")
    extra = set(data) - {"name", "match", "by", "required"}
    if extra:
        raise ConfigError(f"{path}: unknown keys {sorted(extra)}")
    required = data.get("required", False)
    if not isinstance(required, bool):
        raise ConfigError(f"{path}.required: expected true or false")
    entry = WatchEntry(name=str(data["name"]), match=str(data.get("match", "")),
                       by=str(data.get("by", "cmdline")), required=required)
    if entry.by not in ("cmdline", "name"):
        raise ConfigError(f"{path}.by: must be 'cmdline' or 'name'")
    if "|" in entry.name:
        raise ConfigError(f"{path}.name: must not contain '|'")
    if not entry.match:
        entry.match = entry.name
    return entry


def _validate(cfg: Config) -> None:
    if cfg.agent.sample_interval < 1:
        raise ConfigError("agent.sample_interval must be >= 1 s")
    if cfg.agent.probe_interval < cfg.probes.timeout * 2:
        raise ConfigError("agent.probe_interval must be at least twice probes.timeout")
    for name in ("cpu_high", "ram_high", "disk_high", "disk_inodes_high", "proc_cpu_high", "proc_mem_high"):
        r: ThresholdRule = getattr(cfg.rules, name)
        if r.clear_below > r.open_above:
            raise ConfigError(f"rules.{name}: clear_below must be <= open_above")
    for name in ("network_lost", "endpoint_down"):
        if getattr(cfg.rules, name).failures < 1:
            raise ConfigError(f"rules.{name}.failures must be >= 1")
    mc = cfg.rules.memory_climbing
    if not 1 <= mc.min_rises <= mc.window:
        raise ConfigError("rules.memory_climbing: need 1 <= min_rises <= window")
    for f in dataclasses.fields(cfg.rules):
        sev = getattr(cfg.rules, f.name).severity
        if sev not in ("warn", "critical"):
            raise ConfigError(f"rules.{f.name}.severity: must be 'warn' or 'critical', got {sev!r}")
    names = [w.name for w in cfg.watch]
    if len(names) != len(set(names)):
        raise ConfigError("watch: entry names must be unique")
    url = cfg.notify.webhook_url
    if url and not url.startswith(("http://", "https://")):
        raise ConfigError("notify.webhook_url must start with http:// or https://")


def load_config(path: str | Path | None) -> Config:
    data: dict[str, Any] = {}
    if path:
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"config file not found: {p}")
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    cfg = _build(Config, data, "config")
    _validate(cfg)
    if not cfg.agent.name:
        cfg.agent.name = socket.gethostname()
    return cfg
