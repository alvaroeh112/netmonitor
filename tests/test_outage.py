import pytest

from netmonitor.outage import (OK, ROUTER, SIN_ENLACE, SIN_INTERNET, SOLO_DNS, WEB, AlertGate, Incident, OutageTracker,
                               classify)


@pytest.mark.parametrize("link,gw,net,dns,expected", [
    (False, False, False, False, SIN_ENLACE),
    (False, True, True, True, SIN_ENLACE),
    (True, False, False, False, ROUTER),
    (True, False, True, True, ROUTER),
    (True, True, False, False, SIN_INTERNET),
    (True, True, False, True, SIN_INTERNET),
    (True, True, True, False, SOLO_DNS),
    (True, True, True, True, OK),
])
def test_classify(link, gw, net, dns, expected):
    assert classify(link, gw, net, dns) == expected


def test_classify_web_failure_only_when_everything_else_is_fine():
    assert classify(True, True, True, True, web_ok=False) == WEB
    assert classify(True, True, True, False, web_ok=False) == SOLO_DNS      # el DNS explica que no carguen
    assert classify(True, True, False, True, web_ok=False) == SIN_INTERNET


def feed(tr, states, start=1000, step=30):
    ev = []
    for i, s in enumerate(states):
        ev += [(i, k, inc.start_ts, inc.end_ts, inc.type) for k, inc in tr.feed(start + i * step, s)]
    return ev


def test_outage_start_end_exact_duration():
    tr = OutageTracker(3, 2)
    ev = feed(tr, [OK, OK, SIN_INTERNET, SIN_INTERNET, SIN_INTERNET, SIN_INTERNET, OK, OK, OK])
    assert [(i, k) for i, k, *_ in ev] == [(4, "start"), (7, "end")]
    assert ev[0][2] == 1000 + 2 * 30            # inicio = primer fallo, no la confirmación
    assert ev[1][3] == 1000 + 6 * 30            # fin = primer acierto de la recuperación
    assert ev[1][3] - ev[1][2] == 4 * 30        # duración exacta


def test_short_blips_are_not_outages():
    tr = OutageTracker(3, 2)
    assert feed(tr, [OK, ROUTER, ROUTER, OK, ROUTER, ROUTER, OK, OK]) == []


def test_single_ok_does_not_close():
    tr = OutageTracker(2, 2)
    ev = feed(tr, [ROUTER, ROUTER, OK, ROUTER, OK, OK])
    assert [k for _, k, *_ in ev] == ["start", "end"]


def test_brief_other_type_does_not_change_incident():
    tr = OutageTracker(2, 1)
    ev = feed(tr, [SOLO_DNS, SOLO_DNS, SIN_INTERNET, SIN_ENLACE, OK])
    assert [k for _, k, *_ in ev] == ["start", "end"]
    assert ev[-1][4] == SOLO_DNS


def test_sustained_type_change_splits_incident_without_gap():
    tr = OutageTracker(2, 2)
    ev = feed(tr, [ROUTER, ROUTER, SOLO_DNS, SOLO_DNS, SOLO_DNS, OK, OK])
    assert [(k, ty) for _, k, _, _, ty in ev] == [("start", ROUTER), ("end", ROUTER), ("start", SOLO_DNS), ("end", SOLO_DNS)]
    router_end, dns_start = ev[1][3], ev[2][2]
    assert router_end == dns_start == 1000 + 2 * 30                 # el segundo empieza donde acaba el primero
    assert ev[3][3] == 1000 + 5 * 30


def test_start_type_is_worst_in_streak():
    tr = OutageTracker(3, 1)
    ev = feed(tr, [SOLO_DNS, SIN_INTERNET, SIN_INTERNET])
    assert ev[0][4] == SIN_INTERNET


def test_resume_open_incident():
    tr = OutageTracker(3, 2, open_incident=Incident(7, 500, ROUTER))
    ev = feed(tr, [ROUTER, OK, OK], start=2000)
    assert ev[-1][1] == "end" and ev[-1][2] == 500


def test_alert_gate_flapping_and_recovery():
    g = AlertGate(window=600, max_starts=3, quiet=600)
    assert g.on_start(0) == g.SEND and g.on_end(60) == g.SEND
    assert g.on_start(120) == g.SEND
    assert g.on_start(200) == g.FLAP_NOTICE          # tercer corte en < 10 min: un solo aviso agrupado
    assert g.on_end(260) == g.SUPPRESS
    assert g.on_start(300) == g.SUPPRESS
    assert not g.stable(400, in_incident=False)
    assert g.stable(300 + 601, in_incident=False)    # 10 min tranquilo: fin del periodo inestable
    assert not g.stable(2000, in_incident=False)     # solo se avisa una vez
    assert g.on_start(3000) == g.SEND


def test_alert_gate_old_starts_expire():
    g = AlertGate(window=600, max_starts=3, quiet=600)
    for t in (0, 1000, 2000, 3000):
        assert g.on_start(t) == g.SEND
