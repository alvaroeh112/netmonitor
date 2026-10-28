"""Reclasifica el histórico con las reglas actuales y reconstruye los incidentes.

Útil cuando cambian los criterios de caída (p. ej. al tener en cuenta el DNS que usa la casa y las webs).
Usar con el servicio parado:  sudo systemctl stop netmonitor && python -m netmonitor.reclassify && sudo systemctl start netmonitor
"""
from __future__ import annotations

import os
import sys

from .config import Config
from .db import Database
from .monitor import is_private
from .outage import OutageTracker, classify


def reclassify(db: Database, cfg: Config) -> dict:
    cycles = db.query("SELECT ts, link_ok, gw_ok, net_ok, dns_ok, state FROM cycles ORDER BY ts")
    if not cycles:
        return {"cycles": 0, "changed": 0, "incidents": 0}
    first, last = cycles[0]["ts"], cycles[-1]["ts"]
    public = set(cfg.dns_public)

    home_dns: dict[int, bool] = {}
    pub_dns: dict[int, bool] = {}
    clean: dict[int, bool] = {}
    any_dns: dict[int, bool] = {}
    for r in db.query("SELECT ts, resolver, ok FROM dns WHERE ts>=?", (first,)):
        any_dns[r["ts"]] = any_dns.get(r["ts"], False) or bool(r["ok"])
        if r["resolver"] not in public and not is_private(r["resolver"]):
            clean[r["ts"]] = clean.get(r["ts"], True) and bool(r["ok"])
        if r["resolver"] not in public:
            home_dns[r["ts"]] = home_dns.get(r["ts"], False) or bool(r["ok"])
        else:
            pub_dns[r["ts"]] = pub_dns.get(r["ts"], False) or bool(r["ok"])
    web: dict[int, bool] = {}
    for r in db.query("SELECT ts, ok FROM http WHERE ts>=?", (first,)):
        web[r["ts"]] = web.get(r["ts"], False) or bool(r["ok"])

    gap_limit = 3 * cfg.cycle_seconds + 60
    tracker = OutageTracker(cfg.fail_threshold, cfg.recover_threshold)
    incidents: list[list] = []          # [start, end|None, type]
    open_inc: list | None = None
    updates, changed, prev = [], 0, None

    def close_open(at: int):
        nonlocal open_inc
        if open_inc is not None:
            open_inc[1] = at
            open_inc = None

    for c in cycles:
        ts = c["ts"]
        if prev is not None and ts - prev > gap_limit:       # el monitor estuvo parado: no se inventan caídas
            close_open(prev)
            tracker = OutageTracker(cfg.fail_threshold, cfg.recover_threshold)
        prev = ts
        dns_ok = home_dns[ts] if ts in home_dns else any_dns.get(ts, bool(c["dns_ok"]))
        web_ok = web.get(ts, True)
        state = classify(bool(c["link_ok"]), bool(c["gw_ok"]), bool(c["net_ok"]), dns_ok, web_ok, pub_dns.get(ts, False), clean.get(ts, True))
        if state != c["state"] or int(dns_ok) != c["dns_ok"]:
            changed += 1
            updates.append((state, int(dns_ok), ts))
        for kind, inc in tracker.feed(ts, state):
            if kind == "start":
                open_inc = [inc.start_ts, None, inc.type]
                incidents.append(open_inc)
            elif kind == "update" and open_inc is not None:
                open_inc[2] = inc.type
            elif kind == "end" and open_inc is not None:
                open_inc[1] = inc.end_ts
                open_inc = None

    with db.tx() as conn:
        conn.executemany("UPDATE cycles SET state=?, dns_ok=? WHERE ts=?", updates)
        conn.execute("DELETE FROM incidents WHERE start_ts<=? AND COALESCE(end_ts, ?)>=?", (last, last, first))
        conn.executemany("INSERT INTO incidents(start_ts,end_ts,type) VALUES(?,?,?)", incidents)
    return {"cycles": len(cycles), "changed": changed, "incidents": len(incidents)}


def main() -> None:
    from dotenv import load_dotenv

    from .config import load_config
    load_dotenv(os.environ.get("NETMONITOR_ENV", ".env"))
    cfg = load_config()
    res = reclassify(Database(cfg.db_path), cfg)
    print(f"Ciclos revisados: {res['cycles']} · reclasificados: {res['changed']} · incidentes reconstruidos: {res['incidents']}")


if __name__ == "__main__":
    sys.exit(main())
