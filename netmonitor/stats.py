"""Cálculo de estadísticas sobre los datos guardados. Fuente única de cifras para /resumen, /informe y gráficas."""
from __future__ import annotations

import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .db import Database

PERIODS = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}


def parse_period(text: str | None, default: str = "24h") -> tuple[str, int]:
    """'24h'/'7d'/'30d' (también '3d', '12h') -> (etiqueta normalizada, segundos). Lanza ValueError si no es válido."""
    t = (text or default).strip().lower()
    if t in PERIODS:
        return t, PERIODS[t]
    if len(t) >= 2 and t[:-1].isdigit() and t[-1] in "hd":
        n = int(t[:-1])
        secs = n * (3600 if t[-1] == "h" else 86400)
        if 0 < secs <= 30 * 86400:
            return t, secs
    raise ValueError("Periodo no válido. Usa por ejemplo 24h, 7d o 30d (máximo 30d).")


def percentile(values: list[float], p: float) -> float | None:
    """Percentil con interpolación lineal (p entre 0 y 100)."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def mean(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def first_cycle_ts(db: Database) -> int | None:
    row = db.one("SELECT MIN(ts) AS t FROM cycles")
    return row["t"] if row and row["t"] is not None else None


def incidents_between(db: Database, start: int, end: int, now: int | None = None) -> list[dict]:
    """Incidentes que se solapan con [start, end]; los abiertos se cierran virtualmente en `now`."""
    now = now or int(time.time())
    rows = db.query(
        "SELECT id,start_ts,end_ts,type FROM incidents WHERE start_ts<=? AND COALESCE(end_ts,?)>=? ORDER BY start_ts",
        (end, now, start))
    out = []
    for r in rows:
        e = r["end_ts"]
        out.append({"id": r["id"], "start": r["start_ts"], "end": e, "type": r["type"],
                    "open": e is None, "duration": (e if e is not None else now) - r["start_ts"]})
    return out


def downtime_seconds(incs: list[dict], start: int, end: int, now: int) -> int:
    """Segundos sin conexión dentro de [start, end] (recorta los incidentes a la ventana)."""
    total = 0
    for i in incs:
        s = max(i["start"], start)
        e = min(i["end"] if i["end"] is not None else now, end)
        if e > s:
            total += e - s
    return total


def degraded_runs(db: Database, start: int, end: int, min_cycles: int = 3) -> tuple[int, int, int]:
    """Ciclos con DNS degradado y rachas de >= `min_cycles` ciclos seguidos (hueco máx. 90 s):
    (ciclos, episodios, racha más larga en segundos)."""
    rows = db.query("SELECT ts, state FROM cycles WHERE ts>=? AND ts<=? ORDER BY ts", (start, end))
    total, eps, longest = 0, 0, 0
    run, run_start, prev = 0, 0, 0

    def close():
        nonlocal eps, longest, run
        if run >= min_cycles:
            eps += 1
            longest = max(longest, prev - run_start + 30)
        run = 0

    for r in rows:
        if r["state"] == "dns_degradado":
            total += 1
            if run and r["ts"] - prev > 90:
                close()
            if not run:
                run_start = r["ts"]
            run += 1
            prev = r["ts"]
        elif run:
            close()
    if run:
        close()
    return total, eps, longest


def summary(db: Database, seconds: int, now: int | None = None) -> dict:
    """Todas las cifras de un periodo. El periodo se recorta al inicio real de los datos."""
    now = now or int(time.time())
    start = now - seconds
    first = first_cycle_ts(db)
    if first is not None:
        start = max(start, first)
    span = max(now - start, 1)

    incs = incidents_between(db, start, now, now)
    down = downtime_seconds(incs, start, now, now)

    net = db.query("SELECT rtt_avg, loss, jitter FROM ping WHERE kind='internet' AND ts>=? AND ts<=?", (start, now))
    lat = [r["rtt_avg"] for r in net if r["rtt_avg"] is not None]
    gw = db.query("SELECT rtt_avg, loss FROM ping WHERE kind='gateway' AND ts>=? AND ts<=?", (start, now))
    dns = db.one("SELECT COUNT(*) n, SUM(1-ok) f FROM dns WHERE ts>=? AND ts<=?", (start, now))
    slow = db.one("SELECT SUM(ok=1 AND ms>=1500) s FROM dns WHERE ts>=? AND ts<=?", (start, now))
    http = db.one("SELECT COUNT(*) n, SUM(1-ok) f FROM http WHERE ts>=? AND ts<=?", (start, now))
    by_target = {r["target"]: r["l"] for r in db.query(
        "SELECT target, AVG(loss) l FROM ping WHERE kind='internet' AND ts>=? AND ts<=? GROUP BY target ORDER BY target", (start, now))}
    by_resolver = {r["resolver"]: 100.0 * r["f"] / r["n"] for r in db.query(
        "SELECT resolver, COUNT(*) n, SUM(1-ok) f FROM dns WHERE ts>=? AND ts<=? GROUP BY resolver ORDER BY resolver", (start, now))}
    sp = db.query("SELECT down_mbps, up_mbps, ping_ms FROM speed WHERE ok=1 AND ts>=? AND ts<=?", (start, now))
    downs = [r["down_mbps"] for r in sp]
    ups = [r["up_mbps"] for r in sp]
    ncyc = db.one("SELECT COUNT(*) n FROM cycles WHERE ts>=? AND ts<=?", (start, now))["n"]
    deg_cycles, deg_eps, deg_longest = degraded_runs(db, start, now)

    return {
        "start": start, "end": now, "span": span, "cycles": ncyc,
        "availability": max(0.0, min(100.0, 100.0 * (1 - down / span))),
        "downtime": down, "incidents": len(incs),
        "lat_avg": mean(lat), "lat_p95": percentile(lat, 95),
        "loss_avg": mean([r["loss"] for r in net]),
        "jitter_avg": mean([r["jitter"] for r in net]),
        "gw_lat_avg": mean([r["rtt_avg"] for r in gw]), "gw_loss_avg": mean([r["loss"] for r in gw]),
        "dns_fail_pct": (100.0 * dns["f"] / dns["n"]) if dns["n"] else None,
        "dns_slow_pct": (100.0 * (slow["s"] or 0) / dns["n"]) if dns["n"] else None,
        "dns_degraded_pct": (100.0 * deg_cycles / ncyc) if ncyc else 0.0,
        "dns_degraded_episodes": deg_eps, "dns_degraded_longest": deg_longest,
        "http_fail_pct": (100.0 * http["f"] / http["n"]) if http["n"] else None,
        "loss_by_target": by_target, "dns_by_resolver": by_resolver,
        "speed_n": len(sp),
        "down_avg": mean(downs), "down_min": min(downs) if downs else None, "down_max": max(downs) if downs else None,
        "up_avg": mean(ups), "up_min": min(ups) if ups else None, "up_max": max(ups) if ups else None,
        "speed_ping_avg": mean([r["ping_ms"] for r in sp]),
    }


def hourly_profile(db: Database, seconds: int, tz: ZoneInfo, now: int | None = None) -> list[dict]:
    """Perfil por hora del día (0-23, hora local): latencia, pérdida y % de ciclos con fallo."""
    now = now or int(time.time())
    start = now - seconds
    buckets = {h: {"lat": [], "loss": [], "bad": 0, "deg": 0, "n": 0} for h in range(24)}
    for r in db.query("SELECT ts, rtt_avg, loss FROM ping WHERE kind='internet' AND ts>=? AND ts<=?", (start, now)):
        h = datetime.fromtimestamp(r["ts"], tz).hour
        if r["rtt_avg"] is not None:
            buckets[h]["lat"].append(r["rtt_avg"])
        buckets[h]["loss"].append(r["loss"])
    for r in db.query("SELECT ts, state FROM cycles WHERE ts>=? AND ts<=?", (start, now)):
        h = datetime.fromtimestamp(r["ts"], tz).hour
        buckets[h]["n"] += 1
        buckets[h]["bad"] += 0 if r["state"] in ("ok", "dns_degradado") else 1       # el DNS degradado no es un fallo de conexión
        buckets[h]["deg"] += r["state"] == "dns_degradado"
    out = []
    for h in range(24):
        b = buckets[h]
        out.append({"hour": h, "lat": mean(b["lat"]), "loss": mean(b["loss"]),
                    "fail_pct": (100.0 * b["bad"] / b["n"]) if b["n"] else None,
                    "deg_pct": (100.0 * b["deg"] / b["n"]) if b["n"] else None, "n": b["n"]})
    return out


def worst_hours(profile: list[dict], top: int = 3) -> list[dict]:
    """Horas con peor latencia respecto a la mediana del día, solo si la diferencia es apreciable."""
    lats = [p["lat"] for p in profile if p["lat"] is not None]
    if len(lats) < 6:
        return []
    med = percentile(lats, 50)
    bad = [p for p in profile if p["lat"] is not None and p["lat"] > med * 1.5 and p["lat"] - med > 10]
    bad += [p for p in profile if p["fail_pct"] and p["fail_pct"] > 2 and p not in bad]
    return sorted(bad, key=lambda p: -(p["lat"] or 0))[:top]


def latest_status(db: Database, now: int | None = None, public_dns: tuple[str, ...] = ()) -> dict:
    """Datos para /estado: último ciclo, últimas medidas y caída actual."""
    now = now or int(time.time())
    cyc = db.one("SELECT * FROM cycles ORDER BY ts DESC LIMIT 1")
    gw = db.one("SELECT * FROM ping WHERE kind='gateway' ORDER BY ts DESC LIMIT 1")
    net = db.query("SELECT * FROM ping WHERE kind='internet' AND ts=?", (cyc["ts"],)) if cyc else []
    sysi = db.one("SELECT * FROM sysinfo ORDER BY ts DESC LIMIT 1")
    link = db.one("SELECT * FROM link ORDER BY ts DESC LIMIT 1")
    spd = db.one("SELECT * FROM speed WHERE ok=1 ORDER BY ts DESC LIMIT 1")
    inc = db.one("SELECT * FROM incidents WHERE end_ts IS NULL ORDER BY start_ts DESC LIMIT 1")
    web = db.one("SELECT COUNT(*) n, SUM(ok) o FROM http WHERE ts=?", (cyc["ts"],)) if cyc else None
    dns_rows = db.query("SELECT resolver, ok FROM dns WHERE ts=?", (cyc["ts"],)) if cyc else []
    home = [r["ok"] for r in dns_rows if r["resolver"] not in public_dns] or [r["ok"] for r in dns_rows]
    since = now - 3600
    h1 = db.one("SELECT COUNT(*) n, SUM(state NOT IN ('ok','dns_degradado')) b FROM cycles WHERE ts>=?", (since,))
    return {"cycle": cyc, "gw": gw, "net": net, "sys": sysi, "link": link, "speed": spd, "open_incident": inc,
            "web_all_failed": bool(web and web["n"] and not web["o"]),
            "web_ok": (web["o"] or 0) if web else 0, "web_n": web["n"] if web else 0,
            "dns_ok": sum(home), "dns_n": len(home),
            "bad_last_hour": (h1["b"] or 0) if h1 else 0, "n_last_hour": h1["n"] if h1 else 0, "now": now}
