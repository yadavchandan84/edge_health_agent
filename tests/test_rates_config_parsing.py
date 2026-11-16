"""Counter rates, config validation, and the parsers fed by /proc and ping."""

from __future__ import annotations

from pathlib import Path

import pytest

from healthagent.collectors.network import _decode_addr
from healthagent.config import ConfigError, load_config
from healthagent.probes import parse_ping, parse_proc_route
from healthagent.rates import RateTracker

ROOT = Path(__file__).resolve().parent.parent


# -- rates ---------------------------------------------------------------------------
class TestRateTracker:
    def test_rate_from_deltas(self) -> None:
        r = RateTracker()
        assert r.rate("eth0", 1000, 0.0) is None, "first value is only a baseline"
        assert r.rate("eth0", 6000, 5.0) == 1000.0

    def test_counter_reset_drops_one_interval_then_recovers(self) -> None:
        r = RateTracker()
        r.rate("eth0", 10_000, 0)
        assert r.rate("eth0", 200, 5) is None, "went backwards: interface re-created"
        assert r.resets == 1
        assert r.rate("eth0", 700, 10) == 100.0, "new baseline after the reset"

    def test_no_division_by_zero(self) -> None:
        r = RateTracker()
        r.rate("k", 1, 5)
        assert r.rate("k", 2, 5) is None

    def test_delta_for_drop_counts(self) -> None:
        r = RateTracker()
        r.delta("drops", 7, 0)
        assert r.delta("drops", 10, 5) == 3

    def test_sweep_forgets_devices_that_disappear(self) -> None:
        r = RateTracker()
        r.rate("usb0", 100, 0)
        r.sweep()
        r.rate("eth0", 1, 5)
        r.sweep()  # usb0 not seen this tick
        assert r.rate("usb0", 5000, 10) is None, "returning device starts from a fresh baseline"


# -- config ---------------------------------------------------------------------------
def write(tmp_path, text: str):
    p = tmp_path / "c.toml"
    p.write_text(text)
    return p


class TestConfig:
    def test_defaults_match_the_brief(self) -> None:
        c = load_config(None)
        assert (c.agent.sample_interval, c.agent.probe_interval) == (5, 15)
        assert (c.rules.cpu_high.open_above, c.rules.cpu_high.open_for) == (85, 60)
        assert c.rules.ram_high.open_above == c.rules.disk_high.open_above == 90
        assert c.rules.network_lost.failures == 3
        mc = c.rules.memory_climbing
        assert (mc.sample_interval, mc.window, mc.min_rises, mc.min_growth_mb) == (60, 10, 9, 50)
        assert c.rules.zombie.min_age == 30
        assert c.retention.rollup_days == 7
        assert c.agent.name, "defaults to the hostname"

    def test_shipped_configs_are_valid(self) -> None:
        load_config(ROOT / "config.toml")
        demo = load_config(ROOT / "config.demo.toml")
        assert demo.watch[0].required and demo.rules.memory_climbing.sample_interval == 10

    @pytest.mark.parametrize("text,err", [
        ("[agent]\nsampel_interval = 5\n", "unknown setting"),
        ("[rules.cpu_high]\nopen_above = 'high'\n", "expected float"),
        ("[rules.cpu_high]\nopen_above = 80\nclear_below = 90\n", "clear_below"),
        ("[rules.zombie]\nseverity = 'warning'\n", "severity"),
        ("[[watch]]\nmatch = 'x'\n", "needs at least a name"),
        ("[[watch]]\nname = 'a'\nby = 'regex'\n", "'cmdline' or 'name'"),
        ("[[watch]]\nname = 'a'\n[[watch]]\nname = 'a'\n", "unique"),
        ("[notify]\nwebhook_url = 'ftp://x'\n", "http"),
        ("[agent]\nprobe_interval = 3\n", "twice"),
    ])
    def test_mistakes_are_rejected_at_startup(self, tmp_path, text: str, err: str) -> None:
        with pytest.raises(ConfigError, match=err):
            load_config(write(tmp_path, text))

    def test_ints_are_accepted_for_floats(self, tmp_path) -> None:
        c = load_config(write(tmp_path, "[probes]\ntimeout = 3\n"))
        assert c.probes.timeout == 3.0 and isinstance(c.probes.timeout, float)


# -- parsers ----------------------------------------------------------------------------
LINUX_PING_OK = """PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.

--- 1.1.1.1 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 402ms
rtt min/avg/max/mdev = 4.912/5.045/5.201/0.118 ms
"""
LINUX_PING_PARTIAL = """--- 10.0.0.1 ping statistics ---
3 packets transmitted, 1 received, +2 errors, 66.6667% packet loss, time 2003ms
rtt min/avg/max/mdev = 0.410/0.410/0.410/0.000 ms
"""
LINUX_PING_DOWN = """--- 10.0.0.1 ping statistics ---
3 packets transmitted, 0 received, 100% packet loss, time 2046ms
"""
BUSYBOX_PING = """--- 1.1.1.1 ping statistics ---
3 packets transmitted, 3 packets received, 0% packet loss
round-trip min/avg/max = 4.1/4.5/5.0 ms
"""
WINDOWS_PING_UNREACHABLE = """Pinging 10.0.0.1 with 32 bytes of data:
Reply from 192.168.1.5: Destination host unreachable.
Reply from 192.168.1.5: Destination host unreachable.
Reply from 192.168.1.5: Destination host unreachable.

Ping statistics for 10.0.0.1:
    Packets: Sent = 3, Received = 3, Lost = 0 (0% loss),
"""
WINDOWS_PING_OK = """Reply from 1.1.1.1: bytes=32 time=4ms TTL=57
Reply from 1.1.1.1: bytes=32 time=5ms TTL=57
Reply from 1.1.1.1: bytes=32 time=4ms TTL=57
    Minimum = 4ms, Maximum = 5ms, Average = 4ms
"""


class TestParsers:
    @pytest.mark.parametrize("out,windows,loss,rtt", [
        (LINUX_PING_OK, False, 0.0, 5.045),
        (LINUX_PING_PARTIAL, False, pytest.approx(66.67, abs=0.01), 0.41),
        (LINUX_PING_DOWN, False, 100.0, None),
        ("", False, 100.0, None),  # "connect: Network is unreachable" goes to stderr
        (BUSYBOX_PING, False, 0.0, 4.5),
        (WINDOWS_PING_UNREACHABLE, True, 100.0, None),  # Windows says 0% loss here; it is not
        (WINDOWS_PING_OK, True, 0.0, 4.0),
    ])
    def test_ping(self, out: str, windows: bool, loss, rtt) -> None:
        assert parse_ping(out, 3, windows) == (loss, rtt)

    def test_default_route(self) -> None:
        route = (
            "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
            "eth0\t0030A8C0\t00000000\t0001\t0\t0\t0\t00F0FFFF\t0\t0\t0\n"
            "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
            "eth0\t00000000\t0130A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
        )
        assert parse_proc_route(route) == "192.168.48.1", "lowest metric wins"
        assert parse_proc_route(route.splitlines()[0]) is None

    def test_proc_net_tcp_addresses(self) -> None:
        assert _decode_addr("0100007F:1F90") == ("127.0.0.1", 8080)
        assert _decode_addr("00000000000000000000000001000000:01BB") == ("::1", 443)
