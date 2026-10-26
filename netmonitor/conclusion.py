"""Conclusión automática y lenguaje sencillo para el informe (lógica pura, probada con tests)."""
from __future__ import annotations

from .messages import fmt_dur, num
from .outage import LABELS, ROUTER, SIN_ENLACE, SIN_INTERNET, SOLO_DNS, WEB

CAUSE_TEXT = {
    ROUTER: ("Las caídas coinciden con el router inalcanzable: el equipo de medida deja de ver al router. "
             "Conviene revisar el propio router (reinicios, sobrecalentamiento, alimentación), el cable y el puerto."),
    SIN_INTERNET: ("El router responde, pero no hay salida a internet: el fallo está después del router, "
                   "es decir, en la línea o en la red del operador. Es motivo para abrir una incidencia con el operador."),
    SIN_ENLACE: ("Las caídas coinciden con la pérdida del enlace Ethernet: apunta al cable, a los conectores "
                 "o al puerto del router o de la Raspberry."),
    WEB: ("Hay conexión con los servidores, pero las webs de prueba no cargan: suele indicar bloqueos o cortes "
          "hacia destinos concretos (rutas del operador) o problemas de DNS. Conviene reclamar al operador."),
    SOLO_DNS: ("Internet funciona, pero deja de resolverse el nombre de las webs (DNS) en el DNS que usa la casa. "
               "Suele arreglarse cambiando el DNS del router a 1.1.1.1 / 8.8.8.8 o revisando el DNS del operador."),
}


def downtime_by_type(incs: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in incs:
        d = out.setdefault(i["type"], {"count": 0, "seconds": 0})
        d["count"] += 1
        d["seconds"] += i["duration"]
    return out


def conclusion(s: dict, incs: list[dict], max_temp: float | None = None) -> list[str]:
    """Lista de frases orientativas. Siempre honesta sobre el alcance de la medida (solo cable)."""
    out: list[str] = []
    by = downtime_by_type(incs)
    if not incs:
        out.append("No se ha detectado ninguna caída en el periodo: por cable, la conexión ha estado disponible "
                   f"el {num(s['availability'], 2, '%')} del tiempo.")
    else:
        dom = max(by, key=lambda t: by[t]["seconds"])
        total = sum(v["seconds"] for v in by.values())
        out.append(f"Se han registrado {len(incs)} caídas que suman {fmt_dur(total)} sin conexión "
                   f"({num(s['availability'], 2, '%')} de disponibilidad).")
        out.append(f"Tipo predominante: {LABELS[dom]} ({by[dom]['count']} caídas, {fmt_dur(by[dom]['seconds'])}).")
        out.append(CAUSE_TEXT[dom])
        if len(by) > 1:
            others = ", ".join(f"{LABELS[t]}: {v['count']}" for t, v in by.items() if t != dom)
            out.append(f"Otros tipos observados: {others}.")
    lbt = s.get("loss_by_target") or {}
    if len(lbt) > 1 and max(lbt.values()) >= 20 and min(lbt.values()) < 5:
        bad = ", ".join(f"{t} ({v:.0f} %)" for t, v in lbt.items() if v >= 20)
        good = ", ".join(t for t, v in lbt.items() if v < 5)
        out.append(f"La pérdida es selectiva: {bad} pierden paquetes y {good} no. Con el router y otros destinos bien, "
                   "apunta a un problema de ruta del operador hacia esos servidores o a una limitación de ICMP, "
                   "no a una caída general.")
    dns = s.get("dns_by_resolver") or {}
    dead = [r for r, v in dns.items() if v >= 90]
    if dead and len(dead) < len(dns):
        out.append(f"Estos servidores DNS casi no responden: {', '.join(dead)}, mientras otros sí funcionan. "
                   "Si el DNS que usan los dispositivos es uno de ellos, las webs tardarán en cargar o fallarán aunque haya internet.")
    elif any(v >= 20 for v in dns.values()):
        flaky = ", ".join(f"{r} ({v:.0f} %)" for r, v in dns.items() if v >= 20)
        out.append(f"Hay fallos intermitentes de DNS en: {flaky}.")
    if s["loss_avg"] is not None and s["loss_avg"] >= 2 and not (len(lbt) > 1 and max(lbt.values()) >= 20 and min(lbt.values()) < 5):
        out.append(f"Hay pérdida de paquetes apreciable ({num(s['loss_avg'], 1, '%')}), que degrada llamadas y juegos.")
    if s["lat_p95"] is not None and s["lat_p95"] > 100:
        out.append(f"La latencia sube a más de {num(s['lat_p95'], 0, 'ms')} en el 5 % de las mediciones.")
    if s["jitter_avg"] is not None and s["jitter_avg"] > 30:
        out.append(f"El jitter es alto ({num(s['jitter_avg'], 0, 'ms')}): la conexión es poco estable para videollamadas.")
    if s["speed_n"] >= 3 and s["down_avg"] and s["down_min"] < 0.5 * s["down_avg"]:
        out.append(f"La velocidad de bajada es muy variable (de {num(s['down_min'], 0)} a {num(s['down_max'], 0)} Mbps).")
    if max_temp is not None and max_temp >= 75:
        out.append(f"La Raspberry llegó a {num(max_temp, 0, '°C')}: podría estar afectando a las medidas.")
    elif max_temp is not None:
        out.append(f"La Raspberry no ha tenido problemas de temperatura (máx. {num(max_temp, 0, '°C')}), "
                   "así que no es la causa de los fallos.")
    out.append("Limitación: las medidas se toman por cable; no se puede afirmar nada sobre el wifi.")
    return out


def plain_summary(s: dict, incs: list[dict]) -> str:
    """Párrafo para quien no sabe de redes."""
    if s["cycles"] == 0:
        return "Todavía no hay datos suficientes para valorar la conexión."
    if not incs and (s["loss_avg"] or 0) < 1:
        return ("Durante el periodo la conexión por cable ha funcionado sin cortes y sin pérdidas importantes. "
                "Si se notan problemas, es probable que no vengan de la línea.")
    return (f"Durante el periodo la conexión ha estado disponible el {num(s['availability'], 2, '%')} del tiempo. "
            f"Se han producido {len(incs)} cortes; entre todos, la conexión estuvo {fmt_dur(s['downtime'])} caída. "
            "Un corte significa que el equipo de medida no pudo comunicarse con internet durante varios intentos seguidos.")
