"""Simulación de caídas de extremo a extremo (sin red real): mockea las mediciones del Monitor."""
import time

import pytest

from netmonitor import collectors as col
from netmonitor.config import Config
from netmonitor.monitor import Monitor
from netmonitor.parsers import PingResult


def good():
    return PingResult(10, 10, 0.0, 9.0, 10.0, 12.0, 1.0)


def dead():
    return PingResult(10, 0, 100.0, None, None, None, None)


class Scenario:
    """Cada llamada a _measure devuelve el siguiente escenario: (carrier, gw_ok, net_ok, dns_ok)."""

    def __init__(self, steps):
        self.steps, self.i = steps, 0

    def carrier_now(self):
        return self.steps[min(self.i, len(self.steps) - 1)][0]

    async def measure(self, carrier_unused):
        step = self.steps[min(self.i, len(self.steps) - 1)]
        carrier, gw, net, dns = step[:4]
        web = step[4] if len(step) > 4 else net
        self.i += 1
        net_res = {"1.1.1.1": good() if net else dead(), "8.8.8.8": good() if net else dead()}
        home_ok = dns is True
        pub_ok = dns is True or dns == "deg"          # "deg": solo falla el DNS de casa, el público responde
        return (good() if gw else dead(), net_res, [("gw", "google.com", 10.0 if home_ok else None, home_ok), ("1.1.1.1", "google.com", 20.0 if pub_ok else None, pub_ok)],
                [("https://x", 50.0 if web else None, 204 if web else None, web)])


@pytest.fixture
def make(db, monkeypatch):
    sent: list[str] = []
    state = {"fail_send": False}

    async def notify(text):
        if state["fail_send"]:
            raise RuntimeError("sin red")
        sent.append(text)

    def build(steps):
        cfg = Config(token="t", chat_id=1, fail_threshold=3, recover_threshold=2, ping_targets=["1.1.1.1", "8.8.8.8"])
        m = Monitor(cfg, db, notify)
        sc = Scenario(steps)
        m.gateway = "192.168.1.1"
        monkeypatch.setattr(m, "_measure", sc.measure)
        monkeypatch.setattr(col, "read_carrier", lambda iface: sc.carrier_now())
        return m, sc

    return build, sent, state


async def run_n(m, n, t0, step=30, monkeypatch=None):
    for i in range(n):
        ts = t0 + i * step
        _real = time.time
        time.time = lambda ts=ts: ts          # reloj controlado
        try:
            await m.run_cycle()
        finally:
            time.time = _real


OKS = (True, True, True, True)


@pytest.mark.parametrize("bad,tipo", [
    ((False, False, False, False), "sin_enlace"),
    ((True, False, False, False), "router_inalcanzable"),
    ((True, True, False, False), "sin_internet"),
    ((True, True, True, False), "solo_dns"),
])
async def test_outage_detected_with_type_alert_and_exact_duration(make, db, bad, tipo):
    build, sent, _ = make
    steps = [OKS, OKS] + [bad] * 5 + [OKS] * 3
    m, _sc = build(steps)
    t0 = 1_800_000_000
    await run_n(m, len(steps), t0)
    inc = db.query("SELECT * FROM incidents")
    assert len(inc) == 1 and inc[0]["type"] == tipo
    assert inc[0]["start_ts"] == t0 + 2 * 30
    assert inc[0]["end_ts"] == t0 + 7 * 30
    assert inc[0]["end_ts"] - inc[0]["start_ts"] == 150          # duración exacta: 5 ciclos de 30 s
    assert len(sent) == 2
    assert "Caída detectada" in sent[0] and "Conexión recuperada" in sent[1]
    assert "2 min 30 s" in sent[1]


async def test_no_duplicate_cycles_on_same_timestamp(make, db):
    build, _, _ = make
    m, _ = build([OKS])
    await run_n(m, 1, 1_800_000_000)
    await run_n(m, 1, 1_800_000_000)
    assert db.one("SELECT COUNT(*) n FROM cycles")["n"] == 1


async def test_alert_queued_when_send_fails_then_flushed(make, db):
    build, sent, state = make
    steps = [OKS] + [(True, False, False, False)] * 3 + [OKS] * 3
    m, _ = build(steps)
    state["fail_send"] = True                       # sin internet no se puede avisar
    await run_n(m, 4, 1_800_000_000)
    assert db.one("SELECT COUNT(*) n FROM outbox")["n"] == 1 and sent == []
    state["fail_send"] = False                      # vuelve la red: se envía lo pendiente y la recuperación
    await run_n(m, 3, 1_800_000_000 + 4 * 30)
    assert db.one("SELECT COUNT(*) n FROM outbox")["n"] == 0
    assert "Caída detectada" in sent[0] and "recuperada" in sent[-1]


async def test_alerts_off_records_but_does_not_send(make, db):
    build, sent, _ = make
    db.set_setting("alerts", "off")
    steps = [OKS] + [(True, False, False, False)] * 3 + [OKS] * 3
    m, _ = build(steps)
    await run_n(m, len(steps), 1_800_000_000)
    assert sent == [] and db.one("SELECT COUNT(*) n FROM incidents")["n"] == 1


async def test_flapping_sends_single_grouped_notice(make, db):
    build, sent, _ = make
    bad = (True, False, False, False)
    steps = ([bad] * 3 + [OKS] * 2) * 4
    m, _ = build(steps)
    await run_n(m, len(steps), 1_800_000_000)
    assert db.one("SELECT COUNT(*) n FROM incidents")["n"] == 4
    assert sum("inestable" in s for s in sent) == 1
    assert len(sent) < 8                                     # sin spam


async def test_restart_closes_stale_incident_and_resumes_recent(db, make):
    build, _, _ = make
    old = int(time.time()) - 7200
    db.execute("INSERT INTO cycles VALUES(?,?,?,?,?,?)", (old, "router_inalcanzable", 1, 0, 0, 0))
    db.execute("INSERT INTO incidents(start_ts,end_ts,type) VALUES(?,NULL,?)", (old - 100, "router_inalcanzable"))
    m, _ = build([OKS])
    m._recover_open_incident()
    row = db.one("SELECT * FROM incidents")
    assert row["end_ts"] == old                              # cerrada en la última medición conocida
    assert m.tracker.current is None


async def test_only_home_dns_failing_is_degraded_not_an_outage(make, db):
    """1.1.1.1 resuelve pero el DNS de casa no: internet funciona, no es caída (se ve en los ciclos, sin incidente)."""
    build, sent, _ = make
    steps = [OKS] + [(True, True, True, "deg")] * 6 + [OKS] * 3
    m, _ = build(steps)
    await run_n(m, len(steps), 1_800_000_000)
    assert db.query("SELECT type FROM incidents") == []
    assert db.one("SELECT COUNT(*) n FROM cycles WHERE state='dns_degradado'")["n"] == 6
    assert sent == []                                         # una racha corta no avisa


async def test_long_degraded_dns_alerts_once_and_reports_end(make, db):
    build, sent, _ = make
    steps = [OKS] + [(True, True, True, "deg")] * 40 + [OKS] * 5
    m, _ = build(steps)                                       # 15 min = 30 ciclos de 30 s
    await run_n(m, len(steps), 1_800_000_000)
    assert db.query("SELECT type FROM incidents") == []
    assert len(sent) == 2 and "DNS del operador degradado" in sent[0] and "vuelve a responder" in sent[1]


async def test_degraded_dns_does_not_hide_a_real_outage(make, db):
    build, _, _ = make
    steps = [OKS] + [(True, True, True, "deg")] * 3 + [(True, True, False, False)] * 4 + [OKS] * 3
    m, _ = build(steps)
    await run_n(m, len(steps), 1_800_000_000)
    assert [r["type"] for r in db.query("SELECT type FROM incidents")] == ["sin_internet"]


async def test_all_webs_failing_is_an_outage_even_if_pings_and_dns_work(make, db):
    build, sent, _ = make
    steps = [OKS] + [(True, True, True, True, False)] * 3 + [OKS] * 3
    m, _ = build(steps)
    await run_n(m, len(steps), 1_800_000_000)
    assert [r["type"] for r in db.query("SELECT type FROM incidents")] == ["web_caida"]
    assert any("webs no cargan" in t for t in sent)
