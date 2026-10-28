"""Orquestador: planifica las mediciones, guarda resultados, detecta caídas y genera avisos."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
from typing import Awaitable, Callable

import httpx

from . import collectors as col
from . import messages as msg
from . import retention
from .config import Config
from .db import Database
from .outage import DNS_DEG, AlertGate, DegradeWatch, Incident, OutageTracker, classify
from .parsers import PingResult, analyze_trace

log = logging.getLogger(__name__)

Notify = Callable[[str], Awaitable[None]]


def home_dns_ok(dns_rows, public: list[str]) -> bool:
    """¿Resuelve el DNS que usan los dispositivos de casa (router y operador)? Los públicos solo sirven de contraste.
    Si no hay DNS de casa conocidos, vale cualquiera."""
    home = [ok for res, _, _, ok in dns_rows if res not in public]
    return any(home) if home else any(ok for _, _, _, ok in dns_rows)


def is_private(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_private
    except ValueError:
        return False


def home_dns_clean(dns_rows, public: list[str]) -> bool:
    """¿Respondieron TODAS las consultas al DNS de casa? Se ignoran las IP privadas (un router que no sirve DNS)."""
    home = [ok for res, _, _, ok in dns_rows if res not in public and not is_private(res)]
    return all(home)


def _failed_ping() -> PingResult:
    return PingResult(0, 0, 100.0, None, None, None, None)


class Monitor:
    def __init__(self, cfg: Config, db: Database, notify: Notify):
        self.cfg, self.db, self.notify = cfg, db, notify
        self.iface = cfg.iface
        self.gateway = cfg.gateway
        self.lock = asyncio.Lock()          # una sola medición pesada a la vez (evita que el speedtest falsee los pings)
        self.tracker = OutageTracker(cfg.fail_threshold, cfg.recover_threshold)
        self.gate = AlertGate()
        self.degrade = DegradeWatch(max(1, round(cfg.dns_degraded_alert_min * 60 / cfg.cycle_seconds)))
        self._gw_dns = False                # ¿el router sirve DNS? (muchos solo hacen de reenviador interno y no contestan)
        self.last_cycle_ts: int | None = None
        self.last_error: str | None = None
        self._tasks: list[asyncio.Task] = []
        self._http: httpx.AsyncClient | None = None
        self._last_public_ip: str | None = None

    # ------------------------------------------------------------ arranque / parada
    async def start(self) -> None:
        # IPv4 forzado: el IPv6 del hogar suele no tener salida y httpx no hace "happy eyeballs"
        self._http = httpx.AsyncClient(timeout=10, headers={"User-Agent": "netmonitor/1.0"},
                                       transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"))
        if not self.iface or not self.gateway:
            iface, gw = await col.detect_default_route()
            self.iface = self.iface or iface or "eth0"
            self.gateway = self.gateway or gw
        self._recover_open_incident()
        await self._probe_gateway_dns()
        row = self.db.one("SELECT public_ip FROM sysinfo WHERE public_ip IS NOT NULL ORDER BY ts DESC LIMIT 1")
        self._last_public_ip = row["public_ip"] if row else None
        log.info("Monitor iniciado: interfaz=%s gateway=%s", self.iface, self.gateway or "?")
        loops = [
            ("ciclo", self.cfg.cycle_seconds, self.run_cycle, 0),
            ("enlace", self.cfg.link_seconds, self.link_job, 5),
            ("speedtest", self.cfg.speedtest_minutes * 60, self.speed_job, 90),
            ("traza", self.cfg.trace_hours * 3600, self.trace_job, 240),
            ("mantenimiento", 6 * 3600, self.maintenance_job, 600),
        ]
        for name, every, fn, delay in loops:
            self._tasks.append(asyncio.create_task(self._loop(name, every, fn, delay), name=name))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._http:
            await self._http.aclose()

    async def _loop(self, name: str, every: float, fn, initial_delay: float) -> None:
        """Ejecuta `fn` cada `every` s sin acumular retraso; un error no mata el bucle."""
        await asyncio.sleep(initial_delay)
        next_run = time.monotonic()
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - el bucle nunca debe morir
                self.last_error = f"{name}: {type(exc).__name__}: {exc}"
                log.exception("Error en la tarea '%s'", name)
            next_run += every
            await asyncio.sleep(max(0.0, next_run - time.monotonic()))
            if time.monotonic() - next_run > every:       # nos hemos quedado muy atrás (p. ej. suspensión)
                next_run = time.monotonic()

    def _recover_open_incident(self) -> None:
        """Tras un reinicio: continúa una caída abierta, o la cierra si el monitor estuvo parado mucho rato."""
        row = self.db.one("SELECT id,start_ts,type FROM incidents WHERE end_ts IS NULL ORDER BY start_ts DESC LIMIT 1")
        if not row:
            return
        last = self.db.one("SELECT MAX(ts) t FROM cycles")["t"]
        if last is None or time.time() - last > 3 * self.cfg.cycle_seconds + 60:
            end = last if last and last >= row["start_ts"] else row["start_ts"]
            self.db.execute("UPDATE incidents SET end_ts=? WHERE id=?", (end, row["id"]))
            log.warning("Caída %s cerrada al arrancar (monitor parado desde %s)", row["id"], last)
            return
        self.tracker.current = Incident(row["id"], row["start_ts"], row["type"])

    # ------------------------------------------------------------ ciclo de 30 s
    async def run_cycle(self) -> None:
        async with self.lock:
            ts = int(time.time())
            carrier = col.read_carrier(self.iface)
            gw_res, net_res, dns_rows, http_rows = await self._measure(carrier)
        gw_ok = gw_res.received > 0
        net_ok = any(r.received > 0 for r in net_res.values())
        dns_ok = home_dns_ok(dns_rows, self.cfg.dns_public)
        web_ok = any(ok for _, _, _, ok in http_rows)
        public_ok = any(ok for res, _, _, ok in dns_rows if res in self.cfg.dns_public)
        state = classify(carrier, gw_ok, net_ok, dns_ok, web_ok, public_ok, home_dns_clean(dns_rows, self.cfg.dns_public))

        with self.db.tx() as c:
            c.execute("INSERT OR IGNORE INTO cycles(ts,state,link_ok,gw_ok,net_ok,dns_ok) VALUES(?,?,?,?,?,?)",
                      (ts, state, int(carrier), int(gw_ok), int(net_ok), int(dns_ok)))
            for kind, target, r in [("gateway", self.gateway or "gateway", gw_res)] + \
                                   [("internet", t, r) for t, r in net_res.items()]:
                c.execute("INSERT INTO ping(ts,target,kind,sent,received,loss,rtt_min,rtt_avg,rtt_max,jitter) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (ts, target, kind, r.sent, r.received, r.loss, r.rtt_min, r.rtt_avg, r.rtt_max, r.jitter))
            c.executemany("INSERT INTO dns(ts,resolver,domain,ms,ok) VALUES(?,?,?,?,?)",
                          [(ts, res, dom, ms, int(ok)) for res, dom, ms, ok in dns_rows])
            c.executemany("INSERT INTO http(ts,url,ms,status,ok) VALUES(?,?,?,?,?)",
                          [(ts, u, ms, st, int(ok)) for u, ms, st, ok in http_rows])
        self.last_cycle_ts = ts

        await self._handle_events(ts, self.tracker.feed(ts, state))
        await self._handle_degraded(ts, state == DNS_DEG)
        if self.gate.stable(ts, self.tracker.current is not None):
            await self._send(msg.stable_again(self.db.one("SELECT COUNT(*) n FROM incidents WHERE start_ts>=?", (ts - 3600,))["n"]))
        if net_ok:
            await self._flush_outbox()

    async def _measure(self, carrier: bool):
        cfg = self.cfg
        if not carrier:        # sin cable no merece la pena esperar timeouts
            dead = _failed_ping()
            return dead, {t: dead for t in cfg.ping_targets}, \
                [(r, d, None, False) for r in self._resolvers() for d in cfg.dns_domains], \
                [(u, None, None, False) for u in cfg.http_urls]
        gw_task = col.ping_host(self.gateway, 5) if self.gateway else asyncio.sleep(0, _failed_ping())
        # Intervalo de 1 s: a 0,2 s varios servidores limitan el ICMP y se medirían pérdidas falsas
        ping_tasks = [col.ping_host(t, 10, interval=1.0) for t in cfg.ping_targets]
        # Una tarea por servidor DNS que consulta sus dominios de uno en uno: como mucho 1 consulta a la vez por
        # servidor (en ráfaga, 15 consultas simultáneas inflaban la latencia medida)
        dns_tasks = [self._dns_one(r) for r in self._resolvers()]
        http_tasks = [col.http_check(self._http, u) for u in cfg.http_urls]
        results = await asyncio.gather(gw_task, *ping_tasks, *dns_tasks, *http_tasks)
        gw_res = results[0]
        n = len(ping_tasks)
        net = dict(zip(cfg.ping_targets, results[1:1 + n]))
        dns_rows = [row for rows in results[1 + n:1 + n + len(dns_tasks)] for row in rows]
        http_res = results[1 + n + len(dns_tasks):]
        http_rows = [(u, ms, st, ok) for u, (ms, st, ok) in zip(cfg.http_urls, http_res)]
        return gw_res, net, dns_rows, http_rows

    async def _dns_one(self, resolver: str) -> list[tuple[str, str, float | None, bool]]:
        rows = []
        for d in self.cfg.dns_domains:
            ms, ok = await col.dns_query(resolver, d)
            rows.append((resolver, d, ms, ok))
        return rows

    async def _probe_gateway_dns(self) -> None:
        """Comprueba si el router contesta DNS; si no, no se mide (un 100 % de fallo falso ensucia el informe)."""
        if self._gw_dns or not self.gateway:
            return
        _, ok = await col.dns_query(self.gateway, self.cfg.dns_domains[0])
        if ok:
            self._gw_dns = True
            log.info("El router (%s) sirve DNS: se incluye en las mediciones", self.gateway)

    def _resolvers(self) -> list[str]:
        """DNS del router (si sirve DNS), DNS que usa realmente el sistema (el del operador) y los públicos, sin repetir."""
        out: list[str] = []
        for r in ([self.gateway] if self.gateway and self._gw_dns else []) + col.system_dns() + self.cfg.dns_public:
            if r and r not in out:
                out.append(r)
        return out

    # ------------------------------------------------------------ caídas y avisos
    async def _handle_events(self, ts: int, events: list[tuple[str, Incident]]) -> None:
        for kind, inc in events:
            if kind == "start":
                cur = self.db.execute("INSERT INTO incidents(start_ts,type) VALUES(?,?)", (inc.start_ts, inc.type))
                inc.id = cur.lastrowid
                log.warning("CAÍDA %s: %s desde %s", "cambia de tipo" if inc.transition else "detectada", inc.type, inc.start_ts)
                if inc.transition:                      # misma caída, otro tipo de fallo: un aviso breve, sin contar como corte nuevo
                    if not self.gate.flapping:
                        await self._send(msg.outage_changed(inc, self.cfg.tz))
                    continue
                decision = self.gate.on_start(ts)
                if decision == AlertGate.SEND:
                    await self._send(msg.outage_start(inc, self.cfg.tz))
                elif decision == AlertGate.FLAP_NOTICE:
                    await self._send(msg.flapping(self.gate.count_recent()))
            elif kind == "update":
                self.db.execute("UPDATE incidents SET type=? WHERE id=?", (inc.type, inc.id))
            elif kind == "end":
                self.db.execute("UPDATE incidents SET end_ts=? WHERE id=?", (inc.end_ts, inc.id))
                log.warning("Caída %s: duración %s s", "cambia de tipo" if inc.transition else "recuperada", inc.duration)
                if inc.transition:
                    continue
                if self.gate.on_end(ts) == AlertGate.SEND:
                    await self._send(msg.outage_end(inc, self.cfg.tz))

    async def _handle_degraded(self, ts: int, degraded: bool) -> None:
        """DNS de casa caído pero con DNS públicos funcionando: no es caída. Solo se avisa si se prolonga."""
        ev = self.degrade.feed(ts, degraded)
        if ev is None:
            return
        kind, since = ev
        if kind == "alert":
            await self._send(msg.dns_degraded(since, self.cfg.tz))
        else:
            await self._send(msg.dns_degraded_end(since, ts, self.cfg.tz))

    async def _send(self, text: str) -> None:
        """Envía el aviso si las alertas están activas; si falla (p. ej. sin internet) lo deja en cola."""
        if self.db.get_setting("alerts", "on") != "on":
            return
        try:
            await self.notify(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudo enviar el aviso (%s); queda en cola", type(exc).__name__)
            self.db.execute("INSERT INTO outbox(ts,text) VALUES(?,?)", (int(time.time()), text))

    async def _flush_outbox(self) -> None:
        rows = self.db.query("SELECT id,text FROM outbox ORDER BY id LIMIT 20")
        for r in rows:
            try:
                await self.notify(r["text"])
            except Exception:  # noqa: BLE001
                return
            self.db.execute("DELETE FROM outbox WHERE id=?", (r["id"],))

    # ------------------------------------------------------------ tareas periódicas
    async def link_job(self) -> None:
        await self._probe_gateway_dns()
        link = await col.read_link(self.iface)
        sysi = col.read_sysinfo()
        ip = await col.public_ip(self._http)
        ts = int(time.time())
        with self.db.tx() as c:
            c.execute("INSERT OR REPLACE INTO link VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (ts, link["iface"], link["speed_mbps"], link["duplex"], int(link["carrier"]), link["rx_errors"],
                       link["tx_errors"], link["rx_dropped"], link["tx_dropped"], link["carrier_changes"]))
            c.execute("INSERT OR REPLACE INTO sysinfo VALUES(?,?,?,?,?,?)",
                      (ts, ip, self.gateway, sysi["temp_c"], sysi["load1"], sysi["mem_pct"]))
            if ip and self._last_public_ip and ip != self._last_public_ip:
                c.execute("INSERT OR REPLACE INTO ip_changes VALUES(?,?,?)", (ts, self._last_public_ip, ip))
                log.info("La IP pública ha cambiado")
        if ip:
            self._last_public_ip = ip

    async def speed_job(self) -> dict:
        async with self.lock:
            res = await col.run_speedtest(self.cfg.speedtest_backend)
        ts = int(time.time())
        self.db.execute("INSERT OR REPLACE INTO speed(ts,down_mbps,up_mbps,ping_ms,server,ok,error) VALUES(?,?,?,?,?,?,?)",
                        (ts, res.get("down_mbps"), res.get("up_mbps"), res.get("ping_ms"), res.get("server"),
                         int(res["ok"]), res.get("error")))
        return res

    async def trace_job(self) -> list[dict] | None:
        async with self.lock:
            hops = await col.run_trace(self.cfg.trace_target)
        if hops:
            worst, summary = analyze_trace(hops)
            self.db.execute("INSERT OR REPLACE INTO trace(ts,target,hops_json,worst_hop,summary) VALUES(?,?,?,?,?)",
                            (int(time.time()), self.cfg.trace_target, json.dumps(hops), worst, summary))
        return hops

    async def maintenance_job(self) -> None:
        res = await asyncio.to_thread(retention.run_maintenance, self.db, self.cfg.retention_days,
                                      self.cfg.agg_retention_days, self.cfg.max_db_mb)
        log.info("Mantenimiento: %s", res)

    # ------------------------------------------------------------ medición manual (/test)
    async def full_test(self) -> dict:
        """Ciclo completo + speedtest + traza ahora. Devuelve el resultado del speedtest."""
        await self.run_cycle()
        await self.link_job()
        return await self.speed_job()
