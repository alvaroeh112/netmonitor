"""Funciones puras que interpretan la salida de ping, mtr, ethtool y speedtest."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass
class PingResult:
    sent: int
    received: int
    loss: float                 # %
    rtt_min: float | None       # ms
    rtt_avg: float | None
    rtt_max: float | None
    jitter: float | None        # ms: media de |rtt[i] - rtt[i-1]|


def compute_jitter(rtts: list[float]) -> float | None:
    """Jitter como media de las diferencias absolutas entre respuestas consecutivas."""
    if not rtts:
        return None
    if len(rtts) < 2:
        return 0.0
    return sum(abs(b - a) for a, b in zip(rtts, rtts[1:])) / (len(rtts) - 1)


def parse_ping(output: str, sent_fallback: int = 0) -> PingResult:
    """Interpreta la salida de `ping` de Linux (iputils)."""
    rtts = [float(x) for x in re.findall(r"time[=<]([\d.]+)\s*ms", output)]
    m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", output)
    sent = int(m.group(1)) if m else sent_fallback
    received = int(m.group(2)) if m else len(rtts)
    received = min(received, sent) if sent else received
    loss = 100.0 if sent == 0 else max(0.0, min(100.0, (sent - received) / sent * 100.0))
    if not rtts:
        return PingResult(sent, received, loss, None, None, None, None)
    return PingResult(sent, received, loss, min(rtts), sum(rtts) / len(rtts), max(rtts), compute_jitter(rtts))


def parse_speedtest(text: str) -> dict:
    """Devuelve {down_mbps, up_mbps, ping_ms, server}. Soporta speedtest-cli (sivel) y Ookla."""
    data = json.loads(text)
    server = ""
    if isinstance(data.get("download"), dict):          # CLI oficial de Ookla (bytes/s)
        down = data["download"]["bandwidth"] * 8 / 1e6
        up = data["upload"]["bandwidth"] * 8 / 1e6
        ping = data.get("ping", {}).get("latency")
        s = data.get("server", {})
        server = f"{s.get('name', '')} ({s.get('location', '')})".strip()
    else:                                                # speedtest-cli (bits/s)
        down = data["download"] / 1e6
        up = data["upload"] / 1e6
        ping = data.get("ping")
        s = data.get("server", {})
        server = f"{s.get('sponsor', '')} ({s.get('name', '')})".strip()
    return {"down_mbps": float(down), "up_mbps": float(up),
            "ping_ms": float(ping) if ping is not None else None, "server": server}


def parse_ethtool(text: str) -> dict:
    """Extrae velocidad (Mbps), dúplex y enlace de `ethtool <iface>`."""
    speed = re.search(r"Speed:\s*(\d+)Mb/s", text)
    duplex = re.search(r"Duplex:\s*(\w+)", text)
    link = re.search(r"Link detected:\s*(yes|no)", text)
    return {
        "speed_mbps": int(speed.group(1)) if speed else None,
        "duplex": duplex.group(1).lower() if duplex else None,
        "carrier": (link.group(1) == "yes") if link else None,
    }


def parse_mtr_json(text: str) -> list[dict]:
    """Lista de saltos [{hop, host, loss, avg, best, worst}] desde `mtr --json`."""
    data = json.loads(text)
    hubs = data["report"]["hubs"]
    return [{
        "hop": int(h["count"]),
        "host": h["host"],
        "loss": float(h["Loss%"]),
        "avg": float(h["Avg"]),
        "best": float(h["Best"]),
        "worst": float(h["Wrst"]),
    } for h in hubs]


def analyze_trace(hops: list[dict]) -> tuple[int | None, str]:
    """Localiza el salto donde empieza la pérdida real o el salto de latencia.

    Una pérdida solo es real si persiste hasta el destino final; muchos routers
    limitan las respuestas ICMP y muestran pérdida en saltos intermedios sin que afecte al tráfico.
    Devuelve (salto_problemático | None, resumen en español).
    """
    if not hops:
        return None, "Sin datos de ruta."
    final = hops[-1]
    if final["loss"] >= 5:
        for h in hops:
            if h["loss"] >= max(5.0, final["loss"] * 0.5) and all(
                    x["loss"] >= max(5.0, final["loss"] * 0.5) for x in hops[hops.index(h):]):
                return h["hop"], f"La pérdida ({final['loss']:.0f} %) empieza en el salto {h['hop']} ({h['host']})."
    prev = 0.0
    for h in hops:
        if h["avg"] - prev >= 40 and h["avg"] > 0:
            return h["hop"], f"La latencia sube {h['avg'] - prev:.0f} ms en el salto {h['hop']} ({h['host']})."
        prev = max(prev, h["avg"])
    return None, f"Ruta sin problemas: {len(hops)} saltos, destino a {final['avg']:.0f} ms, pérdida {final['loss']:.0f} %."
