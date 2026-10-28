from conftest import populate
from netmonitor.config import Config
from netmonitor.reclassify import reclassify

NOW = 1_800_000_000 // 3600 * 3600


def test_reclassify_rebuilds_history_with_home_dns_and_web_rules(db):
    populate(db, NOW, hours=3)                                       # todo "ok" con las reglas viejas
    t_deg = (NOW - 7200, NOW - 7200 + 300)                           # DNS de casa caído 5 min; 1.1.1.1 sigue bien -> degradado
    t_dns = (NOW - 5400, NOW - 5400 + 300)                           # no responde ningún DNS 5 min -> caída
    t_web = (NOW - 3600, NOW - 3600 + 600)                           # webs caídas 10 min
    with db.tx() as c:
        c.execute("UPDATE dns SET ok=0, ms=NULL WHERE ts>=? AND ts<?", t_deg)
        c.executemany("INSERT INTO dns VALUES(?,?,?,?,?)", [(ts, "1.1.1.1", "google.com", 20.0, 1) for ts in range(t_deg[0], t_deg[1], 30)])
        c.execute("UPDATE dns SET ok=0, ms=NULL WHERE ts>=? AND ts<?", t_dns)
        c.executemany("INSERT INTO dns VALUES(?,?,?,?,?)", [(ts, "1.1.1.1", "google.com", None, 0) for ts in range(t_dns[0], t_dns[1], 30)])
        c.execute("UPDATE http SET ok=0, ms=NULL, status=NULL WHERE ts>=? AND ts<?", t_web)
    res = reclassify(db, Config(token="t", chat_id=1))
    inc = db.query("SELECT type,start_ts,end_ts FROM incidents ORDER BY start_ts")
    assert [r["type"] for r in inc] == ["solo_dns", "web_caida"]
    assert (inc[0]["start_ts"], inc[0]["end_ts"]) == (t_dns[0], t_dns[1])
    assert (inc[1]["start_ts"], inc[1]["end_ts"]) == (t_web[0], t_web[1])
    assert db.one("SELECT COUNT(*) n FROM cycles WHERE state='dns_degradado'")["n"] == 10
    assert res["changed"] == 10 + 10 + 20 and res["incidents"] == 2


def test_reclassify_is_idempotent_and_does_not_invent_outages_over_gaps(db):
    populate(db, NOW, hours=2)
    db.execute("DELETE FROM cycles WHERE ts>=? AND ts<?", (NOW - 3600, NOW - 1800))      # monitor parado 30 min
    a = reclassify(db, Config(token="t", chat_id=1))
    b = reclassify(db, Config(token="t", chat_id=1))
    assert a["incidents"] == 0 and b["changed"] == 0
