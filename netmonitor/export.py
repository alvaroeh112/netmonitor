"""Exportación a CSV (ZIP con un CSV por tabla) del periodo pedido."""
from __future__ import annotations

import csv
import io
import time
import zipfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .db import Database

_TABLES = {
    "mediciones_ping.csv": ("ping", "ts,target,kind,sent,received,loss,rtt_min,rtt_avg,rtt_max,jitter"),
    "mediciones_dns.csv": ("dns", "ts,resolver,domain,ms,ok"),
    "mediciones_http.csv": ("http", "ts,url,ms,status,ok"),
    "ciclos.csv": ("cycles", "ts,state,link_ok,gw_ok,net_ok,dns_ok"),
    "velocidad.csv": ("speed", "ts,down_mbps,up_mbps,ping_ms,server,ok,error"),
    "enlace.csv": ("link", "ts,iface,speed_mbps,duplex,carrier,rx_errors,tx_errors,rx_dropped,tx_dropped,carrier_changes"),
    "sistema.csv": ("sysinfo", "ts,public_ip,gateway_ip,temp_c,load1,mem_pct"),
}


def _iso(ts: int, tz: ZoneInfo) -> tuple[str, str]:
    return (datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M:%S"))


def build_zip(db: Database, seconds: int, tz: ZoneInfo, now: int | None = None) -> bytes:
    now = now or int(time.time())
    start = now - seconds
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, (table, cols) in _TABLES.items():
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["fecha_utc", "fecha_madrid"] + cols.split(",")[1:])
            for r in db.query(f"SELECT {cols} FROM {table} WHERE ts>=? AND ts<=? ORDER BY ts", (start, now)):
                w.writerow(list(_iso(r[0], tz)) + [("" if v is None else v) for v in list(r)[1:]])
            z.writestr(name, buf.getvalue())
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["inicio_utc", "inicio_madrid", "fin_utc", "fin_madrid", "duracion_s", "tipo"])
        for r in db.query("SELECT start_ts,end_ts,type FROM incidents WHERE start_ts<=? AND COALESCE(end_ts,?)>=? ORDER BY start_ts",
                          (now, now, start)):
            s_utc, s_loc = _iso(r["start_ts"], tz)
            e_utc, e_loc = _iso(r["end_ts"], tz) if r["end_ts"] else ("", "")
            w.writerow([s_utc, s_loc, e_utc, e_loc, (r["end_ts"] or now) - r["start_ts"], r["type"]])
        z.writestr("caidas.csv", buf.getvalue())
    return out.getvalue()
