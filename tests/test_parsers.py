import json

from netmonitor.parsers import (analyze_trace, compute_jitter, parse_ethtool, parse_mtr_json, parse_ping,
                                parse_speedtest)

PING_OK = """PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.
64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=10.0 ms
64 bytes from 1.1.1.1: icmp_seq=2 ttl=57 time=12.0 ms
64 bytes from 1.1.1.1: icmp_seq=3 ttl=57 time=11.0 ms
64 bytes from 1.1.1.1: icmp_seq=4 ttl=57 time=15.0 ms

--- 1.1.1.1 ping statistics ---
5 packets transmitted, 4 received, 20% packet loss, time 804ms
rtt min/avg/max/mdev = 10.000/12.000/15.000/1.870 ms
"""


def test_ping_normal():
    r = parse_ping(PING_OK)
    assert (r.sent, r.received) == (5, 4)
    assert r.loss == 20.0
    assert (r.rtt_min, r.rtt_max) == (10.0, 15.0)
    assert r.rtt_avg == 12.0
    assert r.jitter == (2 + 1 + 4) / 3


def test_ping_total_loss():
    r = parse_ping("--- x ping statistics ---\n10 packets transmitted, 0 received, 100% packet loss, time 9000ms\n")
    assert r.loss == 100.0 and r.rtt_avg is None and r.jitter is None


def test_ping_unreachable_uses_fallback():
    r = parse_ping("ping: connect: Network is unreachable", sent_fallback=5)
    assert r.sent == 5 and r.received == 0 and r.loss == 100.0


def test_ping_loss_never_out_of_range():
    r = parse_ping("3 packets transmitted, 7 received, +4 duplicates")
    assert 0 <= r.loss <= 100


def test_jitter_edge_cases():
    assert compute_jitter([]) is None
    assert compute_jitter([5.0]) == 0.0
    assert compute_jitter([1, 1, 1]) == 0


def test_speedtest_cli():
    t = json.dumps({"download": 300e6, "upload": 50e6, "ping": 11.5,
                    "server": {"sponsor": "Movistar", "name": "Madrid"}})
    r = parse_speedtest(t)
    assert round(r["down_mbps"]) == 300 and round(r["up_mbps"]) == 50 and r["ping_ms"] == 11.5
    assert "Movistar" in r["server"]


def test_speedtest_ookla():
    t = json.dumps({"download": {"bandwidth": 37500000}, "upload": {"bandwidth": 6250000},
                    "ping": {"latency": 9.0}, "server": {"name": "Orange", "location": "Madrid"}})
    r = parse_speedtest(t)
    assert round(r["down_mbps"]) == 300 and round(r["up_mbps"]) == 50


def test_ethtool():
    r = parse_ethtool("Settings for end0:\n\tSpeed: 1000Mb/s\n\tDuplex: Full\n\tLink detected: yes\n")
    assert r == {"speed_mbps": 1000, "duplex": "full", "carrier": True}
    assert parse_ethtool("Speed: Unknown!\n")["speed_mbps"] is None


def _hubs(*rows):
    return json.dumps({"report": {"hubs": [
        {"count": str(i + 1), "host": h, "Loss%": loss, "Snt": 10, "Last": a, "Avg": a, "Best": a, "Wrst": a, "StDev": 0}
        for i, (h, loss, a) in enumerate(rows)]}})


def test_mtr_and_analysis_real_loss():
    hops = parse_mtr_json(_hubs(("192.168.1.1", 0, 1), ("10.0.0.1", 30, 9), ("1.1.1.1", 30, 12)))
    assert analyze_trace(hops)[0] == 2


def test_analysis_ignores_icmp_rate_limit():
    hops = parse_mtr_json(_hubs(("192.168.1.1", 0, 1), ("10.0.0.1", 50, 9), ("1.1.1.1", 0, 12)))
    assert analyze_trace(hops)[0] is None


def test_analysis_latency_jump():
    hops = parse_mtr_json(_hubs(("192.168.1.1", 0, 1), ("10.0.0.1", 0, 5), ("1.1.1.1", 0, 120)))
    assert analyze_trace(hops)[0] == 3
