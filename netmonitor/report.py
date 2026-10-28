"""Informe PDF para el técnico del operador. Usa las mismas funciones de stats que /resumen."""
from __future__ import annotations

import io
import json
import platform
import socket
import time
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

from . import charts, stats
from .conclusion import conclusion, downtime_by_type, plain_summary
from .config import Config
from .db import Database
from .messages import fmt_dt, fmt_dur, num
from .outage import LABELS


def _read(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read().replace(b"\x00", b"").decode(errors="replace").strip()
    except OSError:
        return ""


def equipment_info(cfg: Config, db: Database) -> list[tuple[str, str]]:
    link = db.one("SELECT * FROM link ORDER BY ts DESC LIMIT 1")
    sysi = db.one("SELECT * FROM sysinfo WHERE public_ip IS NOT NULL ORDER BY ts DESC LIMIT 1")
    os_name = ""
    for line in _read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            os_name = line.split("=", 1)[1].strip('"')
    rows = [
        ("Equipo de medida", _read("/proc/device-tree/model") or platform.node()),
        ("Nombre / sistema", f"{socket.gethostname()} · {os_name or platform.system()}"),
        ("Router", cfg.router_label),
        ("Conexión del equipo", "Cable Ethernet" + (f" · {link['speed_mbps']} Mbps {link['duplex'] or ''}" if link and link["speed_mbps"] else "")),
        ("IP pública (última)", sysi["public_ip"] if sysi else "–"),
        ("Puerta de enlace", sysi["gateway_ip"] if sysi and sysi["gateway_ip"] else "–"),
    ]
    ch = db.one("SELECT COUNT(*) n FROM ip_changes")
    rows.append(("Cambios de IP pública registrados", str(ch["n"]) if ch else "0"))
    return rows


def build_report(db: Database, cfg: Config, period_label: str, seconds: int, now: int | None = None) -> bytes:
    now = now or int(time.time())
    tz = cfg.tz
    s = stats.summary(db, seconds, now)
    incs = stats.incidents_between(db, s["start"], now, now)
    temp = db.one("SELECT MAX(temp_c) m FROM sysinfo WHERE ts>=?", (s["start"],))
    concl = conclusion(s, incs, temp["m"] if temp else None)

    base = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=base["Heading1"], fontSize=18, spaceAfter=8, textColor=colors.HexColor("#111827"))
    h2 = ParagraphStyle("h2", parent=base["Heading2"], fontSize=13, spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#1d4ed8"))
    body = ParagraphStyle("body", parent=base["BodyText"], fontSize=10, leading=14)
    small = ParagraphStyle("small", parent=body, fontSize=8, textColor=colors.HexColor("#6b7280"))
    title = ParagraphStyle("title", parent=base["Title"], fontSize=26, leading=32, alignment=0, textColor=colors.HexColor("#111827"))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm,
                            title=f"Informe de conexión ({period_label})", author="NetMonitor")
    el: list = []

    # ---- Portada
    el += [Spacer(1, 3 * cm), Paragraph("Informe de calidad de la conexión a internet", title), Spacer(1, 0.5 * cm),
           Paragraph(f"Periodo analizado: <b>{fmt_dt(s['start'], tz)}</b> a <b>{fmt_dt(now, tz)}</b> ({period_label}, hora de Madrid)", body),
           Paragraph(f"Informe generado el {datetime.fromtimestamp(now, tz).strftime('%d/%m/%Y %H:%M')}", body), Spacer(1, 1 * cm)]
    t = Table([[Paragraph(f"<b>{k}</b>", body), Paragraph(str(v), body)] for k, v in equipment_info(cfg, db)], colWidths=[6 * cm, 10.5 * cm])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
                           ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f3f4f6")), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    el += [t, Spacer(1, 1 * cm),
           Paragraph("Las medidas se toman cada 30 segundos desde un equipo conectado por cable al router: "
                     "comprueban la comunicación con el router, con servidores de internet (1.1.1.1, 8.8.8.8, 9.9.9.9), "
                     "la resolución de nombres (DNS) y la carga de páginas web. No miden el wifi.", small), PageBreak()]

    # ---- Resumen sencillo
    el += [Paragraph("1. Resumen en lenguaje sencillo", h1), Paragraph(plain_summary(s, incs), body), Spacer(1, 0.3 * cm)]
    rows = [["Indicador", "Valor"],
            ["Disponibilidad", num(s["availability"], 2, "%")],
            ["Caídas / tiempo total sin conexión", f"{s['incidents']} / {fmt_dur(s['downtime'])}"],
            ["Latencia media / p95", f"{num(s['lat_avg'], 0, 'ms')} / {num(s['lat_p95'], 0, 'ms')}"],
            ["Pérdida media de paquetes", num(s["loss_avg"], 2, "%")],
            ["Jitter medio", num(s["jitter_avg"], 1, "ms")],
            ["Latencia hasta el router", num(s["gw_lat_avg"], 1, "ms")],
            ["Fallos de DNS / de carga web", f"{num(s['dns_fail_pct'], 1, '%')} / {num(s['http_fail_pct'], 1, '%')}"],
            ["DNS del operador degradado (sin ser caída)", f"{num(s.get('dns_degraded_pct'), 1, '%')} del tiempo · {s.get('dns_degraded_episodes', 0)} episodios"],
            ["Descarga media (mín - máx)", f"{num(s['down_avg'], 0)} ({num(s['down_min'], 0)} - {num(s['down_max'], 0)}) Mbps"],
            ["Subida media (mín - máx)", f"{num(s['up_avg'], 0)} ({num(s['up_min'], 0)} - {num(s['up_max'], 0)}) Mbps"],
            ["Tests de velocidad realizados", str(s["speed_n"])]]
    t = Table(rows, colWidths=[8.5 * cm, 8 * cm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1d4ed8")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                           ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")), ("FONTSIZE", (0, 0), (-1, -1), 9),
                           ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")])]))
    el += [t]
    if s["loss_by_target"] or s["dns_by_resolver"]:
        rows = [["Detalle por servidor", "Pérdida de paquetes / fallos de DNS"]]
        rows += [[f"Ping a {k}", num(v, 1, "%")] for k, v in s["loss_by_target"].items()]
        rows += [[f"DNS {k}" + (" (router)" if k == (db.one("SELECT gateway_ip g FROM sysinfo WHERE gateway_ip IS NOT NULL ORDER BY ts DESC LIMIT 1") or {"g": ""})["g"] else ""),
                  num(v, 1, "%")] for k, v in s["dns_by_resolver"].items()]
        t2 = Table(rows, colWidths=[8.5 * cm, 8 * cm])
        t2.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")), ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d1d5db")),
                                ("FONTSIZE", (0, 0), (-1, -1), 8.5)]))
        el += [Spacer(1, 0.3 * cm), t2]

    # ---- Conclusión
    el += [Paragraph("2. Conclusión orientativa", h2)]
    el += [Paragraph("• " + c, body) for c in concl]
    el += [Paragraph("Esta conclusión es automática y orientativa; no sustituye la revisión de un técnico.", small), PageBreak()]

    # ---- Gráficas
    el += [Paragraph("3. Gráficas", h1)]
    for fn in (charts.latency_chart, charts.loss_chart, charts.speed_chart, charts.incidents_chart):
        png = fn(db, seconds, tz, period_label, now)
        el += [Image(io.BytesIO(png), width=15.5 * cm, height=5.6 * cm, kind="proportional"), Spacer(1, 0.1 * cm)]
    el.append(PageBreak())

    # ---- Incidentes
    el += [Paragraph("4. Tabla de incidentes", h1)]
    if incs:
        by = downtime_by_type(incs)
        el += [Paragraph("Resumen por tipo: " + "; ".join(f"{LABELS[k]}: {v['count']} ({fmt_dur(v['seconds'])})" for k, v in by.items()), body), Spacer(1, 0.2 * cm)]
        data = [["Inicio", "Fin", "Duración", "Tipo"]]
        for i in sorted(incs, key=lambda x: x["start"])[:60]:
            data.append([fmt_dt(i["start"], tz), "en curso" if i["open"] else fmt_dt(i["end"], tz), fmt_dur(i["duration"]), LABELS[i["type"]]])
        t = Table(data, colWidths=[3.2 * cm, 3.2 * cm, 2.8 * cm, 7.3 * cm], repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1d4ed8")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                               ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")), ("FONTSIZE", (0, 0), (-1, -1), 8.5)]))
        el.append(t)
        if len(incs) > 60:
            el.append(Paragraph(f"Se muestran 60 de {len(incs)} incidentes (los datos completos están en el CSV).", small))
    else:
        el.append(Paragraph("No se han registrado incidentes en el periodo.", body))

    # ---- Ruta
    tr = db.one("SELECT * FROM trace ORDER BY ts DESC LIMIT 1")
    el += [Paragraph("5. Ruta hasta internet (mtr)", h2)]
    if tr:
        hops = json.loads(tr["hops_json"])
        el.append(Paragraph(f"Medida del {fmt_dt(tr['ts'], tz)} hacia {tr['target']}. {tr['summary']}", body))
        data = [["Salto", "Equipo", "Pérdida", "Latencia media"]] + [[h["hop"], h["host"], num(h["loss"], 0, "%"), num(h["avg"], 1, "ms")] for h in hops]
        t = Table(data, colWidths=[1.8 * cm, 7 * cm, 3 * cm, 4.5 * cm], repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")), ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d1d5db")),
                               ("FONTSIZE", (0, 0), (-1, -1), 8.5)]))
        el.append(t)
    else:
        el.append(Paragraph("Aún no hay medidas de ruta.", body))

    doc.build(el)
    return buf.getvalue()
