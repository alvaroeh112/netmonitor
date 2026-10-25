import time

import pytest

from conftest import populate
from netmonitor import retention, stats
from netmonitor.conclusion import conclusion
from netmonitor.outage import ROUTER, SIN_INTERNET

NOW = 1_800_000_000 // 3600 * 3600 + 1800


def test_percentile_and_mean():
    assert stats.percentile([1, 2, 3, 4, 5], 50) == 3
    assert stats.percentile([1, 2, 3, 4], 50) == 2.5
    assert stats.percentile(list(range(1, 101)), 95) == pytest.approx(95.05)
    assert stats.percentile([], 95) is None
    assert stats.percentile([7], 95) == 7
    assert stats.mean([1, None, 3]) == 2
    assert stats.mean([]) is None


def test_parse_period():
    assert stats.parse_period("24h") == ("24h", 86400)
    assert stats.parse_period("7D") == ("7d", 7 * 86400)
    assert stats.parse_period("12h")[1] == 12 * 3600
    assert stats.parse_period(None, "7d")[0] == "7d"
    for bad in ("90d", "abc", "0h", "7"):
        with pytest.raises(ValueError):
            stats.parse_period(bad)


def test_availability_exact(db):
    # 24 h de datos con una caída de exactamente 1 h -> 95,83 %
    populate(db, NOW, hours=24, outage=(NOW - 10 * 3600, NOW - 9 * 3600, SIN_INTERNET))
    s = stats.summary(db, 24 * 3600, NOW)
    assert s["downtime"] == 3600
    assert s["incidents"] == 1
    assert s["availability"] == pytest.approx(100 * (1 - 3600 / s["span"]), abs=1e-6)
    assert 95.5 < s["availability"] < 96.2


def test_summary_values_and_ranges(db):
    populate(db, NOW, hours=48)
    s = stats.summary(db, 24 * 3600, NOW)
    assert s["availability"] == 100.0 and s["incidents"] == 0
    assert 15 <= s["lat_avg"] <= 20 and s["lat_p95"] >= s["lat_avg"]
    assert s["loss_avg"] == 0 and s["jitter_avg"] == 1.0
    assert s["speed_n"] > 5 and s["down_min"] <= s["down_avg"] <= s["down_max"]
    assert s["dns_fail_pct"] == 0 and s["http_fail_pct"] == 0


def test_summary_clipped_to_data_start(db):
    populate(db, NOW, hours=2)
    s = stats.summary(db, 30 * 86400, NOW)
    assert s["span"] <= 2 * 3600 + 60


def test_open_incident_counts_until_now(db):
    populate(db, NOW, hours=3)
    db.execute("INSERT INTO incidents(start_ts,end_ts,type) VALUES(?,NULL,?)", (NOW - 600, ROUTER))
    s = stats.summary(db, 3 * 3600, NOW)
    assert s["downtime"] == 600


def test_worst_hours_detects_evening_peak(db):
    tz = __import__("zoneinfo").ZoneInfo("Europe/Madrid")
    prof = [{"hour": h, "lat": 15.0, "loss": 0.0, "fail_pct": 0.0, "n": 100} for h in range(24)]
    prof[21]["lat"] = 90.0
    bad = stats.worst_hours(prof)
    assert [p["hour"] for p in bad] == [21]
    assert stats.worst_hours([{"hour": h, "lat": 15.0, "loss": 0, "fail_pct": 0, "n": 1} for h in range(24)]) == []


def test_hourly_profile_shape(db):
    populate(db, NOW, hours=48)
    prof = stats.hourly_profile(db, 48 * 3600, __import__("zoneinfo").ZoneInfo("Europe/Madrid"), NOW)
    assert len(prof) == 24 and all(p["n"] > 0 for p in prof)


# ---------------------------------------------------------------- retención
def test_aggregation_and_prune(db):
    populate(db, NOW, hours=24 * 40)                       # 40 días de datos
    before = db.one("SELECT COUNT(*) n FROM ping")["n"]
    res = retention.prune_detail(db, 30, NOW)
    assert res["ping"] > 0 and db.one("SELECT COUNT(*) n FROM ping")["n"] < before
    oldest = db.one("SELECT MIN(ts) t FROM ping")["t"]
    assert oldest >= NOW - 30 * 86400
    # los datos borrados siguen existiendo como agregados por hora
    agg = db.query("SELECT * FROM agg_hour WHERE metric='ping_net' ORDER BY hour_ts")
    assert agg and agg[0]["hour_ts"] < NOW - 30 * 86400
    row = agg[0]
    assert row["n"] > 0 and row["min"] <= row["avg"] <= row["max"] and row["p95"] >= row["avg"] - 1e-9
    up = db.one("SELECT avg FROM agg_hour WHERE metric='up' LIMIT 1")
    assert up["avg"] == 1.0
    # lo reciente no se toca
    assert db.one("SELECT COUNT(*) n FROM cycles WHERE ts>=?", (NOW - 29 * 86400,))["n"] > 0


def test_prune_is_idempotent(db):
    populate(db, NOW, hours=24 * 35)
    retention.prune_detail(db, 30, NOW)
    n1 = db.one("SELECT COUNT(*) n FROM agg_hour")["n"]
    retention.prune_detail(db, 30, NOW)
    assert db.one("SELECT COUNT(*) n FROM agg_hour")["n"] == n1


def test_aggregates_expire_after_a_year(db):
    db.execute("INSERT INTO agg_hour VALUES(?,?,?,?,?,?,?)", (NOW - 400 * 86400, "up", 1, 1, 1, 1, 1))
    db.execute("INSERT INTO agg_hour VALUES(?,?,?,?,?,?,?)", (NOW - 100 * 86400, "up", 1, 1, 1, 1, 1))
    assert retention.prune_aggregates(db, 365, NOW) == 1


def test_size_limit_shrinks_retention(db):
    populate(db, NOW, hours=24 * 20)
    res = retention.run_maintenance(db, 30, 365, max_mb=1, now=NOW)       # límite absurdo -> reduce
    assert res["retention_days_effective"] < 30


# ---------------------------------------------------------------- conclusión
def _inc(t, d):
    return {"type": t, "duration": d, "start": 0, "end": d, "open": False}


def _s(**kw):
    base = {"availability": 99.0, "loss_avg": 0.0, "lat_p95": 30.0, "jitter_avg": 2.0, "speed_n": 0,
            "down_avg": None, "down_min": None, "down_max": None, "downtime": 0, "cycles": 10, "incidents": 0}
    base.update(kw)
    return base


def test_conclusion_router():
    txt = " ".join(conclusion(_s(), [_inc(ROUTER, 300), _inc(ROUTER, 200)]))
    assert "router inalcanzable" in txt.lower() and "revisar" in txt.lower()


def test_conclusion_operator():
    txt = " ".join(conclusion(_s(), [_inc(SIN_INTERNET, 300)]))
    assert "operador" in txt.lower() and "router responde" in txt.lower()


def test_conclusion_no_incidents_and_honest_limit():
    out = conclusion(_s(availability=100.0), [], max_temp=55)
    assert "ninguna caída" in out[0] and "wifi" in out[-1]
    assert any("temperatura" in x for x in out)


def test_summary_breakdowns_and_conclusion(db):
    populate(db, NOW, hours=3)
    # 8.8.8.8 pierde el 60 % de los paquetes y un DNS no responde nunca
    with db.tx() as c:
        for ts in range(NOW - 3 * 3600, NOW, 30):
            c.execute("INSERT INTO ping VALUES(?,?,?,?,?,?,?,?,?,?)", (ts, "8.8.8.8", "internet", 10, 4, 60.0, 9, 10, 12, 1))
            c.execute("INSERT INTO dns VALUES(?,?,?,?,?)", (ts, "192.168.1.1", "google.com", None, 0))
    s = stats.summary(db, 3 * 3600, NOW)
    assert s["loss_by_target"]["8.8.8.8"] == 60.0 and s["loss_by_target"]["1.1.1.1"] == 0.0
    assert s["dns_by_resolver"]["192.168.1.1"] == 100.0 and s["dns_by_resolver"]["gw"] == 0.0
    txt = " ".join(conclusion(s, []))
    assert "selectiva" in txt and "192.168.1.1" in txt
    from netmonitor import messages
    out = messages.resumen(s, "24h", __import__("zoneinfo").ZoneInfo("Europe/Madrid"))
    assert "Pérdida por servidor" in out and "DNS con fallos" in out
