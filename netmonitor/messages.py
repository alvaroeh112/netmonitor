"""Formato de los mensajes de Telegram (texto plano, español, números redondeados y con unidades)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .outage import LABELS

WIFI_NOTE = ("ℹ️ Las medidas se hacen por cable desde la Raspberry: no reflejan el wifi. "
             "Para medir el wifi hace falta una sonda aparte (otra Raspberry o móvil conectado por wifi "
             "ejecutando este mismo monitor).")


def num(x, decimals: int = 0, unit: str = "") -> str:
    if x is None:
        return "–"
    s = f"{x:.{decimals}f}"
    return f"{s} {unit}".strip()


def fmt_dur(seconds: float | int | None) -> str:
    if seconds is None:
        return "–"
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s:02d} s" if s else f"{m} min"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} h {m:02d} min"
    d, h = divmod(h, 24)
    return f"{d} d {h} h"


def fmt_dt(ts: int | float, tz: ZoneInfo, with_date: bool = True) -> str:
    dt = datetime.fromtimestamp(ts, tz)
    return dt.strftime("%d/%m %H:%M") if with_date else dt.strftime("%H:%M")


def fmt_ago(ts: int | float, now: int | float) -> str:
    d = max(0, now - ts)
    return "hace " + fmt_dur(d if d >= 60 else d)


# ------------------------------------------------------------------ avisos
def outage_start(inc, tz: ZoneInfo) -> str:
    return (f"🔴 Caída detectada\n{LABELS[inc.type]}\nDesde las {fmt_dt(inc.start_ts, tz, False)} "
            f"({fmt_dt(inc.start_ts, tz)}). Te aviso cuando vuelva.")


def outage_end(inc, tz: ZoneInfo) -> str:
    return (f"🟢 Conexión recuperada\nHa durado {fmt_dur(inc.duration)} "
            f"({fmt_dt(inc.start_ts, tz, False)} → {fmt_dt(inc.end_ts, tz, False)}).\nTipo: {LABELS[inc.type]}")


def flapping(count: int) -> str:
    return (f"🟠 Conexión inestable: {count} cortes en pocos minutos.\n"
            "Dejo de avisar uno a uno y te escribo cuando se estabilice.")


def stable_again(summary_count: int) -> str:
    return f"🟢 La conexión se ha estabilizado. Cortes durante el periodo inestable: {summary_count}."


# ------------------------------------------------------------------ /estado
def semaforo(st: dict) -> tuple[str, str]:
    """(emoji, titular). Rojo: caída en curso o datos obsoletos; amarillo: degradada; verde: bien."""
    cyc = st["cycle"]
    if cyc is None:
        return "⚪", "Aún no hay mediciones"
    if st["now"] - cyc["ts"] > 120:
        return "🔴", "El monitor no ha medido en los últimos 2 minutos"
    if st["open_incident"] is not None:
        return "🔴", f"Caída en curso: {LABELS[st['open_incident']['type']]}"
    if cyc["state"] != "ok":
        return "🟡", f"Posible problema: {LABELS.get(cyc['state'], cyc['state'])}"
    net = [r for r in st["net"] if r["rtt_avg"] is not None]
    lat = sum(r["rtt_avg"] for r in net) / len(net) if net else None
    loss = sum(r["loss"] for r in st["net"]) / len(st["net"]) if st["net"] else 0
    if st.get("web_all_failed"):
        return "🟡", "Hay conexión, pero las webs de prueba no cargan"
    if loss >= 2 or (lat is not None and lat > 100) or st["bad_last_hour"] > 0:
        return "🟡", "Conexión con incidencias recientes"
    return "🟢", "Conexión estable"


def estado(st: dict, tz: ZoneInfo) -> str:
    emoji, title = semaforo(st)
    if st["cycle"] is None:
        return f"{emoji} {title}"
    now = st["now"]
    net = [r for r in st["net"] if r["rtt_avg"] is not None]
    lat = sum(r["rtt_avg"] for r in net) / len(net) if net else None
    loss = sum(r["loss"] for r in st["net"]) / len(st["net"]) if st["net"] else None
    gw = st["gw"]
    lines = [f"{emoji} {title}"]
    lines.append(f"📡 Internet: {num(lat, 0, 'ms')} · pérdida {num(loss, 0, '%')} · router {num(gw['rtt_avg'] if gw else None, 1, 'ms')}")
    sp = st["speed"]
    lines.append(f"⚡ Velocidad: ⬇ {num(sp['down_mbps'], 0, 'Mbps')} ⬆ {num(sp['up_mbps'], 0, 'Mbps')} ({fmt_ago(sp['ts'], now)})"
                 if sp else "⚡ Velocidad: aún sin test (usa /test)")
    lk, sy = st["link"], st["sys"]
    if lk:
        lines.append(f"🔌 Cable: {num(lk['speed_mbps'], 0, 'Mbps')} {lk['duplex'] or ''} · IP pública {sy['public_ip'] if sy and sy['public_ip'] else '–'}")
    else:
        lines.append("🔌 Cable: sin datos aún")
    pi = f"🌡️ Raspberry: {num(sy['temp_c'], 0, '°C')} · carga {num(sy['load1'], 2)}" if sy else "🌡️ Raspberry: sin datos"
    lines.append(f"{pi} · fallos última hora: {st['bad_last_hour']}")
    return "\n".join(lines)


# ------------------------------------------------------------------ /resumen
def resumen(s: dict, label: str, tz: ZoneInfo) -> str:
    cov = ""
    if s["cycles"] == 0:
        return f"📊 Resumen {label}\nAún no hay datos suficientes."
    lines = [
        f"📊 Resumen {label} ({fmt_dt(s['start'], tz)} → {fmt_dt(s['end'], tz)}){cov}",
        f"✅ Disponibilidad: {num(s['availability'], 2, '%')}",
        f"🔴 Caídas: {s['incidents']} · sin conexión {fmt_dur(s['downtime'])}",
        f"⏱️ Latencia: media {num(s['lat_avg'], 0, 'ms')} · p95 {num(s['lat_p95'], 0, 'ms')} · jitter {num(s['jitter_avg'], 1, 'ms')}",
        f"📉 Pérdida media: {num(s['loss_avg'], 2, '%')} · router: {num(s['gw_lat_avg'], 1, 'ms')}",
        f"🌐 Fallos DNS: {num(s['dns_fail_pct'], 1, '%')} · fallos web: {num(s['http_fail_pct'], 1, '%')}",
    ]
    if s.get("loss_by_target") and len(s["loss_by_target"]) > 1:
        lines.append("🎯 Pérdida por servidor: " + " · ".join(f"{t} {num(v, 0, '%')}" for t, v in s["loss_by_target"].items()))
    bad_dns = {r: v for r, v in (s.get("dns_by_resolver") or {}).items() if v >= 20}
    if bad_dns:
        lines.append("🔎 DNS con fallos: " + " · ".join(f"{r} {num(v, 0, '%')}" for r, v in bad_dns.items()))
    if s["speed_n"]:
        lines.append(f"⬇ Descarga: media {num(s['down_avg'], 0)} (mín {num(s['down_min'], 0)}, máx {num(s['down_max'], 0)}) Mbps")
        lines.append(f"⬆ Subida: media {num(s['up_avg'], 0)} (mín {num(s['up_min'], 0)}, máx {num(s['up_max'], 0)}) Mbps · {s['speed_n']} tests")
    else:
        lines.append("⚡ Velocidad: sin tests en este periodo")
    return "\n".join(lines)


# ------------------------------------------------------------------ /caidas
def caidas(incs: list[dict], label: str, tz: ZoneInfo, limit: int = 15) -> str:
    if not incs:
        return f"✅ Sin caídas en {label}."
    total = sum(i["duration"] for i in incs)
    lines = [f"🔴 Caídas en {label}: {len(incs)} · total {fmt_dur(total)}"]
    for i in sorted(incs, key=lambda x: -x["start"])[:limit]:
        end = "en curso" if i["open"] else fmt_dt(i["end"], tz, False)
        lines.append(f"• {fmt_dt(i['start'], tz)} → {end} · {fmt_dur(i['duration'])} · {LABELS[i['type']]}")
    if len(incs) > limit:
        lines.append(f"… y {len(incs) - limit} más (usa /informe o /exportar para verlas todas)")
    return "\n".join(lines)


# ------------------------------------------------------------------ /velocidad
def speed_trend(rows: list) -> str:
    ok = [r["down_mbps"] for r in rows if r["ok"]]          # rows en orden cronológico inverso
    if len(ok) < 6:
        return "Tendencia: aún pocos tests para compararla."
    recent, older = sum(ok[:3]) / 3, sum(ok[3:6]) / 3
    if older and recent < older * 0.85:
        return f"Tendencia: ↘️ empeora (media reciente {recent:.0f} Mbps frente a {older:.0f} Mbps)."
    if older and recent > older * 1.15:
        return f"Tendencia: ↗️ mejora (media reciente {recent:.0f} Mbps frente a {older:.0f} Mbps)."
    return "Tendencia: ➡️ estable."


def velocidad(rows: list, tz: ZoneInfo, limit: int = 8) -> str:
    if not rows:
        return "⚡ Aún no hay tests de velocidad. Usa /test para hacer uno ahora."
    lines = ["⚡ Últimos tests de velocidad"]
    for r in rows[:limit]:
        if r["ok"]:
            lines.append(f"• {fmt_dt(r['ts'], tz)} · ⬇ {num(r['down_mbps'], 0)} ⬆ {num(r['up_mbps'], 0)} Mbps · ping {num(r['ping_ms'], 0, 'ms')}")
        else:
            lines.append(f"• {fmt_dt(r['ts'], tz)} · falló: {r['error'] or 'error desconocido'}")
    lines.append(speed_trend(rows))
    return "\n".join(lines)


def test_result(res: dict, loss_line: str) -> str:
    if not res.get("ok"):
        return f"⚠️ El test de velocidad no se ha podido completar: {res.get('error', 'error desconocido')}"
    return (f"⚡ Resultado\n⬇ Descarga: {num(res['down_mbps'], 0, 'Mbps')}\n⬆ Subida: {num(res['up_mbps'], 0, 'Mbps')}\n"
            f"⏱️ Ping: {num(res['ping_ms'], 0, 'ms')}\n🖧 Servidor: {res['server']}\n{loss_line}")


# ------------------------------------------------------------------ /horas
def horas(profile: list[dict], bad: list[dict], label: str) -> str:
    if not any(p["n"] for p in profile):
        return "🕒 Aún no hay datos para el mapa por horas."
    lines = [f"🕒 Latencia por hora del día ({label}, hora local)", "Hora  Latencia  Pérdida  Fallos"]
    for p in profile:
        if not p["n"]:
            continue
        flag = " ⚠️" if p in bad else ""
        lines.append(f"{p['hour']:02d}h  {num(p['lat'], 0, 'ms'):>8}  {num(p['loss'], 1, '%'):>7}  {num(p['fail_pct'], 1, '%'):>6}{flag}")
    if bad:
        hrs = ", ".join(f"{p['hour']:02d}h" for p in sorted(bad, key=lambda p: p["hour"]))
        lines.append(f"\n⚠️ Peores franjas: {hrs}")
    else:
        lines.append("\n✅ No se aprecian horas claramente peores que el resto.")
    return "\n".join(lines)


# ------------------------------------------------------------------ otros
AYUDA = """🤖 NetMonitor: vigila tu conexión 24/7
/estado – semáforo y datos actuales
/resumen [24h|7d|30d] – estadísticas del periodo
/caidas – lista de cortes con hora, duración y tipo
/velocidad – últimos tests y tendencia
/test – medición completa ahora mismo
/horas – cómo cambia la conexión según la hora
/informe [7d|30d] – informe PDF para el técnico
/exportar – datos del periodo en CSV
/alertas on|off – avisos de caída y recuperación
/diagnostico – autocomprobación del bot
/ayuda – esta lista

""" + WIFI_NOTE

COMMANDS = [
    ("estado", "Semáforo y datos actuales"),
    ("resumen", "Estadísticas: 24h, 7d o 30d"),
    ("caidas", "Lista de cortes"),
    ("velocidad", "Últimos tests de velocidad"),
    ("test", "Medición completa ahora"),
    ("horas", "Mapa por franja horaria"),
    ("informe", "Informe PDF para el técnico"),
    ("exportar", "Datos en CSV"),
    ("alertas", "Avisos on/off"),
    ("diagnostico", "Autocomprobación del bot"),
    ("ayuda", "Lista de comandos"),
]


def diagnostico(checks: list[tuple[bool, str]]) -> str:
    ok = all(c for c, _ in checks)
    head = "🩺 Diagnóstico: todo en orden" if ok else "🩺 Diagnóstico: hay problemas"
    return head + "\n" + "\n".join(f"{'✅' if c else '❌'} {t}" for c, t in checks)


ERROR_GENERIC = "⚠️ Algo ha fallado al procesar eso. Prueba otra vez en un momento; si se repite, usa /diagnostico."
