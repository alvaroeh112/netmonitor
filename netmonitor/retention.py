"""Agregación por horas y limpieza automática para no llenar la tarjeta SD."""
from __future__ import annotations

import logging
import time

from .db import Database
from .stats import percentile

log = logging.getLogger(__name__)

# metric -> (sql que devuelve (hour_ts, valor) )
_SOURCES = {
    "ping_gw": "SELECT ts/3600*3600 h, rtt_avg v FROM ping WHERE kind='gateway' AND rtt_avg IS NOT NULL AND ts<?",
    "ping_net": "SELECT ts/3600*3600 h, rtt_avg v FROM ping WHERE kind='internet' AND rtt_avg IS NOT NULL AND ts<?",
    "loss_net": "SELECT ts/3600*3600 h, loss v FROM ping WHERE kind='internet' AND ts<?",
    "jitter_net": "SELECT ts/3600*3600 h, jitter v FROM ping WHERE kind='internet' AND jitter IS NOT NULL AND ts<?",
    "dns_ms": "SELECT ts/3600*3600 h, ms v FROM dns WHERE ok=1 AND ts<?",
    "http_ms": "SELECT ts/3600*3600 h, ms v FROM http WHERE ok=1 AND ts<?",
    "speed_down": "SELECT ts/3600*3600 h, down_mbps v FROM speed WHERE ok=1 AND ts<?",
    "speed_up": "SELECT ts/3600*3600 h, up_mbps v FROM speed WHERE ok=1 AND ts<?",
    "up": "SELECT ts/3600*3600 h, CASE WHEN state='ok' THEN 1.0 ELSE 0.0 END v FROM cycles WHERE ts<?",
    "temp": "SELECT ts/3600*3600 h, temp_c v FROM sysinfo WHERE temp_c IS NOT NULL AND ts<?",
}


def aggregate_hours(db: Database, before_ts: int) -> int:
    """Calcula (o recalcula) las agregaciones por hora de todo lo anterior a `before_ts` (hora completa)."""
    cutoff = before_ts // 3600 * 3600
    written = 0
    for metric, sql in _SOURCES.items():
        groups: dict[int, list[float]] = {}
        for r in db.query(sql, (cutoff,)):
            groups.setdefault(r["h"], []).append(r["v"])
        rows = [(h, metric, len(v), sum(v) / len(v), min(v), max(v), percentile(v, 95)) for h, v in groups.items()]
        if rows:
            with db.tx() as c:
                c.executemany("INSERT OR REPLACE INTO agg_hour(hour_ts,metric,n,avg,min,max,p95) VALUES(?,?,?,?,?,?,?)",
                              rows)
            written += len(rows)
    return written


_DETAIL_TABLES = ["ping", "dns", "http", "cycles", "speed", "link", "sysinfo", "trace"]


def prune_detail(db: Database, retention_days: int, now: int | None = None) -> dict:
    """Agrega por horas y borra el detalle más antiguo que `retention_days`. Nunca borra sin agregar antes."""
    now = now or int(time.time())
    cutoff = now - retention_days * 86400
    aggregate_hours(db, cutoff)
    deleted = {}
    with db.tx() as c:
        for t in _DETAIL_TABLES:
            deleted[t] = c.execute(f"DELETE FROM {t} WHERE ts<?", (cutoff,)).rowcount
        # Los incidentes cerrados se conservan 1 año (son pocos y valiosos).
        deleted["incidents"] = c.execute(
            "DELETE FROM incidents WHERE end_ts IS NOT NULL AND end_ts<?", (now - 365 * 86400,)).rowcount
    return deleted


def prune_aggregates(db: Database, agg_days: int, now: int | None = None) -> int:
    now = now or int(time.time())
    return db.execute("DELETE FROM agg_hour WHERE hour_ts<?", (now - agg_days * 86400,)).rowcount


def run_maintenance(db: Database, retention_days: int, agg_days: int, max_mb: int, now: int | None = None) -> dict:
    """Mantenimiento completo: retención normal y, si la BD supera el límite, retención reducida."""
    now = now or int(time.time())
    result = {"deleted": prune_detail(db, retention_days, now), "agg_deleted": prune_aggregates(db, agg_days, now)}
    days = retention_days
    while db.size_mb() > max_mb and days > 3:
        days = max(3, days // 2)
        log.warning("BD por encima del límite (%.0f MB > %d MB): retención reducida a %d días", db.size_mb(), max_mb, days)
        prune_detail(db, days, now)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    if result["deleted"] and any(result["deleted"].values()):
        with db._lock:
            db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.conn.execute("PRAGMA incremental_vacuum")
    result["retention_days_effective"] = days
    return result
