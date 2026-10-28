"""Recolectores: ejecutan ping, DNS, HTTP, speedtest, mtr y leen el estado del enlace. Solo Linux."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time

import dns.asyncresolver
import dns.exception
import httpx

from .parsers import (PingResult, parse_ethtool, parse_mtr_json, parse_ping, parse_speedtest)

log = logging.getLogger(__name__)


async def run_cmd(cmd: list[str], timeout: float) -> tuple[int, str]:
    """Ejecuta un comando sin shell. Devuelve (código, stdout+stderr). Mata el proceso si excede el tiempo."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError:
        return 127, f"comando no encontrado: {cmd[0]}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "tiempo agotado"
    return proc.returncode or 0, out.decode(errors="replace")


# ---------------------------------------------------------------- red básica
async def detect_default_route() -> tuple[str, str]:
    """(interfaz, gateway) de la ruta por defecto, o ('', '') si no hay."""
    rc, out = await run_cmd(["ip", "-4", "route", "show", "default"], 5)
    m = re.search(r"default via (\S+) dev (\S+)", out)
    return (m.group(2), m.group(1)) if m else ("", "")


async def ping_host(host: str, count: int = 10, interval: float = 0.2) -> PingResult:
    rc, out = await run_cmd(["ping", "-n", "-c", str(count), "-i", str(interval), "-W", "2", host],
                            timeout=count * (interval + 1) + 5)
    return parse_ping(out, sent_fallback=count)


async def dns_query(resolver: str, domain: str, timeout: float = 3.0, attempts: int = 2) -> tuple[float | None, bool]:
    """Resuelve `domain` contra un servidor DNS concreto. Devuelve (ms | None, ok).

    Como hace cualquier cliente real, reintenta: `attempts` envíos que se reparten el `timeout` total. Una respuesta
    que llega tras el reintento tarda >= timeout/attempts, así que se distingue de una consulta fallida."""
    per_try = timeout / attempts
    t0 = time.perf_counter()
    for _ in range(attempts):
        r = dns.asyncresolver.Resolver(configure=False)
        r.nameservers = [resolver]
        r.lifetime = per_try
        r.timeout = per_try
        try:
            await r.resolve(domain, "A")
            return (time.perf_counter() - t0) * 1000, True
        except (dns.exception.DNSException, OSError):
            continue
    return None, False


async def http_check(client: httpx.AsyncClient, url: str) -> tuple[float | None, int | None, bool]:
    t0 = time.perf_counter()
    try:
        resp = await client.get(url, follow_redirects=True)
        ms = (time.perf_counter() - t0) * 1000
        return ms, resp.status_code, resp.status_code < 400
    except (httpx.HTTPError, OSError):
        return None, None, False


async def public_ip(client: httpx.AsyncClient) -> str | None:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            resp = await client.get(url, timeout=8)
            ip = resp.text.strip()
            if re.fullmatch(r"[0-9a-fA-F:.]{7,45}", ip):
                return ip
        except (httpx.HTTPError, OSError):
            continue
    return None


# ---------------------------------------------------------------- enlace y sistema
def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def system_dns() -> list[str]:
    """Servidores DNS reales del sistema (los que da el operador por DHCP), IPv4 y sin el stub local."""
    for path in ("/run/systemd/resolve/resolv.conf", "/etc/resolv.conf"):
        text = _read(path)
        if not text:
            continue
        found = [m for m in re.findall(r"^nameserver\s+(\d+\.\d+\.\d+\.\d+)", text, re.M) if not m.startswith("127.")]
        if found:
            return found[:2]
    return []


def read_carrier(iface: str) -> bool:
    """True si hay portadora. Si no se puede leer (p. ej. interfaz sin cable) devuelve False."""
    return _read(f"/sys/class/net/{iface}/carrier") == "1"


def _int(v: str | None) -> int | None:
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


async def read_link(iface: str) -> dict:
    base = f"/sys/class/net/{iface}"
    speed = _int(_read(f"{base}/speed"))
    duplex = _read(f"{base}/duplex")
    carrier = _read(f"{base}/carrier") == "1"
    if carrier and (speed is None or speed <= 0 or duplex in (None, "unknown")):
        rc, out = await run_cmd(["ethtool", iface], 5)       # respaldo con ethtool
        info = parse_ethtool(out)
        speed = info["speed_mbps"] or speed
        duplex = info["duplex"] or duplex
    stat = lambda n: _int(_read(f"{base}/statistics/{n}"))   # noqa: E731
    return {
        "iface": iface, "speed_mbps": speed if speed and speed > 0 else None, "duplex": duplex,
        "carrier": carrier, "rx_errors": stat("rx_errors"), "tx_errors": stat("tx_errors"),
        "rx_dropped": stat("rx_dropped"), "tx_dropped": stat("tx_dropped"),
        "carrier_changes": _int(_read(f"{base}/carrier_changes")),
    }


def read_sysinfo() -> dict:
    temp = _int(_read("/sys/class/thermal/thermal_zone0/temp"))
    mem_pct = None
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0])
        mem_pct = 100.0 * (1 - info["MemAvailable"] / info["MemTotal"])
    except (OSError, KeyError, ValueError):
        pass
    return {"temp_c": temp / 1000 if temp is not None else None, "load1": os.getloadavg()[0], "mem_pct": mem_pct}


# ---------------------------------------------------------------- speedtest y ruta
_CF_HOST = "speed.cloudflare.com"


async def _resolve_reliably(host: str) -> str | None:
    """IPv4 de `host` usando DNS públicos (el DNS del operador puede fallar a ratos y falsear la medida de velocidad)."""
    for ns in ("1.1.1.1", "8.8.8.8", "9.9.9.9"):
        r = dns.asyncresolver.Resolver(configure=False)
        r.nameservers = [ns]
        r.lifetime = 4
        try:
            return str((await r.resolve(host, "A"))[0])
        except (dns.exception.DNSException, OSError):
            continue
    return None


async def cloudflare_speedtest(seconds: float = 6.0, streams: int = 4) -> dict:
    """Test de velocidad con los endpoints públicos de speed.cloudflare.com (sin instalar nada).

    Latencia = mediana de peticiones HTTPS con la conexión ya abierta. Descarga y subida = bytes / tiempo
    con varios flujos en paralelo durante `seconds` s. Se conecta por IP (con SNI) para no depender del DNS local.
    """
    ip = await _resolve_reliably(_CF_HOST)
    if not ip:
        return {"ok": False, "error": "No se pudo resolver el servidor de pruebas"}
    base, hdr, ext = f"https://{ip}", {"Host": _CF_HOST}, {"sni_hostname": _CF_HOST}
    limits = httpx.Limits(max_connections=streams + 2)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10), limits=limits,
                                     transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0", limits=limits, retries=3)) as c:
            pings, colo = [], ""
            for i in range(6):
                t0 = time.perf_counter()
                resp = await c.get(f"{base}/__down?bytes=0", headers=hdr, extensions=ext)
                if i:                                   # la primera incluye el establecimiento de la conexión
                    pings.append((time.perf_counter() - t0) * 1000)
                colo = resp.headers.get("cf-ray", "").rsplit("-", 1)[-1]
            pings.sort()

            async def down(deadline: float) -> int:
                total = 0
                refused = 0
                try:
                    while time.perf_counter() < deadline and refused < 5:
                        # 5 MB por petición: con tamaños mayores Cloudflare responde 429 (límite de ritmo)
                        async with c.stream("GET", f"{base}/__down?bytes=5000000", headers=hdr, extensions=ext) as r:
                            if r.status_code != 200:
                                refused += 1
                                await asyncio.sleep(0.5)
                                continue
                            async for chunk in r.aiter_bytes():
                                total += len(chunk)
                                if time.perf_counter() >= deadline:
                                    return total
                except (httpx.HTTPError, OSError):
                    pass                                  # un flujo caído no invalida el test: cuenta lo recibido
                return total

            t0 = time.perf_counter()
            got = sum(await asyncio.gather(*[down(t0 + seconds) for _ in range(streams)]))
            if got == 0:
                return {"ok": False, "error": "No llegaron datos de descarga (conexión inestable)"}
            down_mbps = got * 8 / 1e6 / (time.perf_counter() - t0)

            payload = b"\0" * 2_000_000

            async def up(deadline: float) -> int:
                total = 0
                try:
                    refused = 0
                    while time.perf_counter() < deadline and refused < 5:
                        r = await c.post(f"{base}/__up", content=payload, headers=hdr, extensions=ext)
                        if r.status_code != 200:
                            refused += 1
                            await asyncio.sleep(0.5)
                            continue
                        total += len(payload)
                except (httpx.HTTPError, OSError):
                    pass
                return total

            t0 = time.perf_counter()
            sent = sum(await asyncio.gather(*[up(t0 + seconds) for _ in range(streams)]))
            if sent == 0:
                return {"ok": False, "error": "No se pudo enviar datos de subida (conexión inestable)"}
            up_mbps = sent * 8 / 1e6 / (time.perf_counter() - t0)
    except (httpx.HTTPError, OSError) as exc:
        return {"ok": False, "error": f"fallo de red durante el test ({type(exc).__name__})"}
    return {"ok": True, "down_mbps": down_mbps, "up_mbps": up_mbps, "ping_ms": pings[len(pings) // 2] if pings else None,
            "server": f"Cloudflare ({colo or 'desconocido'})"}


async def run_speedtest(backend: str = "cloudflare") -> dict:
    """Ejecuta un test de velocidad. Devuelve dict con ok=True y las cifras, o ok=False y error."""
    if backend == "cloudflare":
        return await cloudflare_speedtest()
    if shutil.which("speedtest-cli"):
        cmd = ["speedtest-cli", "--json"]
    elif shutil.which("speedtest"):
        cmd = ["speedtest", "--format=json", "--accept-license", "--accept-gdpr"]
    else:
        return {"ok": False, "error": "No hay speedtest instalado"}
    rc, out = await run_cmd(cmd, 100)
    if rc != 0:
        return {"ok": False, "error": out.strip().splitlines()[-1][:200] if out.strip() else f"código {rc}"}
    try:
        start = out.index("{")
        return {"ok": True, **parse_speedtest(out[start:])}
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"respuesta no válida ({type(exc).__name__})"}


async def run_trace(target: str) -> list[dict] | None:
    rc, out = await run_cmd(["mtr", "-n", "-r", "-c", "10", "--json", target], 120)
    if rc != 0:
        return None
    try:
        return parse_mtr_json(out[out.index("{"):])
    except (ValueError, KeyError, json.JSONDecodeError):
        return None
