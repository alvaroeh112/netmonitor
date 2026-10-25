import re
from zoneinfo import ZoneInfo

from conftest import populate
from netmonitor import messages as msg, report, stats
from netmonitor.config import Config

TZ = ZoneInfo("Europe/Madrid")
NOW = 1_800_000_000


def test_num_and_durations():
    assert msg.num(None) == "–"
    assert msg.num(12.345, 1, "ms") == "12.3 ms"
    assert msg.fmt_dur(45) == "45 s"
    assert msg.fmt_dur(150) == "2 min 30 s"
    assert msg.fmt_dur(3600) == "60 min" or msg.fmt_dur(3600) == "1 h 00 min"
    assert msg.fmt_dur(7500) == "2 h 05 min"
    assert msg.fmt_dur(90000) == "1 d 1 h"
    assert msg.fmt_dur(None) == "–"


def test_resumen_has_units_and_all_fields(db):
    populate(db, NOW, hours=30)
    s = stats.summary(db, 24 * 3600, NOW)
    txt = msg.resumen(s, "24h", TZ)
    for needle in ("Disponibilidad", "%", "ms", "Mbps", "Caídas", "p95", "jitter", "Pérdida"):
        assert needle in txt
    assert "Traceback" not in txt


def test_resumen_without_data():
    assert "Aún no hay datos" in msg.resumen({"cycles": 0}, "24h", TZ)


def test_estado_is_five_lines_with_traffic_light(db):
    populate(db, NOW, hours=2)
    st = stats.latest_status(db, NOW)
    txt = msg.estado(st, TZ)
    assert len(txt.splitlines()) == 5
    assert txt.startswith("🟢")


def test_estado_red_during_outage_and_yellow_with_recent_failures(db):
    populate(db, NOW, hours=2, outage=(NOW - 1800, NOW - 900, "sin_internet"))
    st = stats.latest_status(db, NOW)
    assert msg.semaforo(st)[0] == "🟡"                      # ya recuperada, pero con fallos en la última hora
    db.execute("INSERT INTO incidents(start_ts,end_ts,type) VALUES(?,NULL,?)", (NOW - 100, "router_inalcanzable"))
    st = stats.latest_status(db, NOW)
    assert msg.semaforo(st)[0] == "🔴" and "Router inalcanzable" in msg.semaforo(st)[1]


def test_estado_red_when_data_is_stale(db):
    populate(db, NOW - 3600, hours=1)
    st = stats.latest_status(db, NOW)
    assert msg.semaforo(st)[0] == "🔴"


def test_caidas_and_horas_formatting(db):
    populate(db, NOW, hours=5, outage=(NOW - 3000, NOW - 2850, "router_inalcanzable"))
    incs = stats.incidents_between(db, NOW - 86400, NOW, NOW)
    txt = msg.caidas(incs, "7d", TZ)
    assert "2 min 30 s" in txt and "Router inalcanzable" in txt
    assert "Sin caídas" in msg.caidas([], "7d", TZ)
    prof = stats.hourly_profile(db, 5 * 3600, TZ, NOW)
    assert "Latencia por hora" in msg.horas(prof, [], "7d")


def test_ayuda_lists_every_command_in_one_line():
    for c, _ in msg.COMMANDS:
        assert f"/{c}" in msg.AYUDA
    assert "wifi" in msg.AYUDA


def test_speed_trend():
    rows = [{"ok": 1, "down_mbps": v} for v in (200, 210, 205, 300, 310, 305)]
    assert "empeora" in msg.speed_trend(rows)
    rows = [{"ok": 1, "down_mbps": 300} for _ in range(6)]
    assert "estable" in msg.speed_trend(rows)


def test_pdf_generation_and_figures_match_resumen(db):
    populate(db, NOW, hours=24 * 8, outage=(NOW - 3 * 86400, NOW - 3 * 86400 + 600, "sin_internet"))
    cfg = Config(token="t", chat_id=1, router_label="Mi router")
    pdf = report.build_report(db, cfg, "7d", 7 * 86400, NOW)
    assert pdf.startswith(b"%PDF") and len(pdf) > 20_000
    # las cifras del informe salen de la misma función que /resumen
    s = stats.summary(db, 7 * 86400, NOW)
    assert s["incidents"] == 1 and s["downtime"] == 600
    assert re.search(r"99\.\d\d %", msg.resumen(s, "7d", TZ))
