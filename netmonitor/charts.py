"""Gráficas con matplotlib (PNG en memoria). Misma fuente de datos que /resumen."""
from __future__ import annotations

import io
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from .db import Database  # noqa: E402
from .outage import LABELS  # noqa: E402
from .stats import first_cycle_ts, incidents_between  # noqa: E402

BLUE, ORANGE, TEAL, PURPLE, RED, GREY = "#2563eb", "#ea580c", "#0d9488", "#7c3aed", "#dc2626", "#6b7280"


def _window(db: Database, seconds: int, now: int | None) -> tuple[int, int]:
    now = now or int(time.time())
    start = now - seconds
    first = first_cycle_ts(db)
    return (max(start, first) if first else start), now


def _bucket(span: int) -> int:
    return max(60, span // 240 // 60 * 60)


def _dt(ts_list, tz):
    return [datetime.fromtimestamp(t, tz) for t in ts_list]


def _finish(fig, ax, tz: ZoneInfo, title: str, ylabel: str, span: int) -> bytes:
    ax.set_title(title, loc="left", fontsize=12, fontweight="bold")
    ax.set_ylabel(ylabel)
    ax.grid(True, alpha=0.25)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fmt = "%H:%M" if span <= 2 * 86400 else "%d/%m"
    ax.xaxis.set_major_formatter(mdates.DateFormatter(fmt, tz=tz))
    fig.autofmt_xdate()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def _shade_incidents(ax, db, start, end, tz):
    for i in incidents_between(db, start, end, end):
        ax.axvspan(datetime.fromtimestamp(max(i["start"], start), tz),
                   datetime.fromtimestamp(min(i["end"] or end, end), tz), color=RED, alpha=0.18, lw=0)


def _empty(title: str) -> bytes:
    fig, ax = plt.subplots(figsize=(8, 2.5))
    ax.text(0.5, 0.5, "Sin datos todavía", ha="center", va="center", color=GREY, fontsize=12)
    ax.set_title(title, loc="left", fontsize=12, fontweight="bold")
    ax.axis("off")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def latency_chart(db: Database, seconds: int, tz: ZoneInfo, label: str, now: int | None = None) -> bytes:
    start, end = _window(db, seconds, now)
    b = _bucket(end - start)
    rows = db.query("SELECT ts/?*? t, AVG(rtt_avg) a, MAX(rtt_max) m FROM ping WHERE kind='internet' AND ts BETWEEN ? AND ? "
                    "AND rtt_avg IS NOT NULL GROUP BY t ORDER BY t", (b, b, start, end))
    gw = db.query("SELECT ts/?*? t, AVG(rtt_avg) a FROM ping WHERE kind='gateway' AND ts BETWEEN ? AND ? "
                  "AND rtt_avg IS NOT NULL GROUP BY t ORDER BY t", (b, b, start, end))
    if not rows:
        return _empty(f"Latencia ({label})")
    fig, ax = plt.subplots(figsize=(8, 3.2))
    _shade_incidents(ax, db, start, end, tz)
    ax.fill_between(_dt([r["t"] for r in rows], tz), [r["a"] for r in rows], [r["m"] for r in rows],
                    color=BLUE, alpha=0.12, label="máxima")
    ax.plot(_dt([r["t"] for r in rows], tz), [r["a"] for r in rows], color=BLUE, lw=1.6, label="internet (media)")
    if gw:
        ax.plot(_dt([r["t"] for r in gw], tz), [r["a"] for r in gw], color=TEAL, lw=1.2, label="router")
    ax.legend(loc="upper right", fontsize=8, frameon=False)
    ax.set_ylim(bottom=0)
    return _finish(fig, ax, tz, f"Latencia ({label}) · zonas rojas = caídas", "ms", end - start)


def loss_chart(db: Database, seconds: int, tz: ZoneInfo, label: str, now: int | None = None) -> bytes:
    start, end = _window(db, seconds, now)
    b = _bucket(end - start)
    rows = db.query("SELECT ts/?*? t, AVG(loss) l FROM ping WHERE kind='internet' AND ts BETWEEN ? AND ? GROUP BY t ORDER BY t",
                    (b, b, start, end))
    if not rows:
        return _empty(f"Pérdida de paquetes ({label})")
    fig, ax = plt.subplots(figsize=(8, 2.8))
    _shade_incidents(ax, db, start, end, tz)
    ax.bar(_dt([r["t"] for r in rows], tz), [r["l"] for r in rows], width=b / 86400 * 0.9, color=ORANGE)
    ax.set_ylim(0, min(105, max(5, max(r["l"] for r in rows) * 1.2)))
    return _finish(fig, ax, tz, f"Pérdida de paquetes ({label})", "%", end - start)


def speed_chart(db: Database, seconds: int, tz: ZoneInfo, label: str, now: int | None = None) -> bytes:
    start, end = _window(db, seconds, now)
    rows = db.query("SELECT ts, down_mbps d, up_mbps u FROM speed WHERE ok=1 AND ts BETWEEN ? AND ? ORDER BY ts", (start, end))
    if not rows:
        return _empty(f"Velocidad ({label})")
    fig, ax = plt.subplots(figsize=(8, 3))
    t = _dt([r["ts"] for r in rows], tz)
    ax.plot(t, [r["d"] for r in rows], color=TEAL, marker="o", ms=3, lw=1.4, label="descarga")
    ax.plot(t, [r["u"] for r in rows], color=PURPLE, marker="o", ms=3, lw=1.4, label="subida")
    ax.legend(loc="upper right", fontsize=8, frameon=False)
    ax.set_ylim(bottom=0)
    return _finish(fig, ax, tz, f"Velocidad ({label})", "Mbps", end - start)


def incidents_chart(db: Database, seconds: int, tz: ZoneInfo, label: str, now: int | None = None) -> bytes:
    start, end = _window(db, seconds, now)
    incs = incidents_between(db, start, end, end)
    if not incs:
        return _empty(f"Caídas ({label})")
    order = list(LABELS)
    fig, ax = plt.subplots(figsize=(8, 2.4))
    for i in incs:
        y = order.index(i["type"])
        s = datetime.fromtimestamp(i["start"], tz)
        e = datetime.fromtimestamp(i["end"] or end, tz)
        # barra mínima visible aunque la caída dure segundos
        width = max((e - s).total_seconds(), (end - start) / 300) / 86400
        ax.barh(y, width, left=mdates.date2num(s), color=RED, height=0.55)
    ax.set_yticks(range(len(order)), [LABELS[t].split(" (")[0] for t in order], fontsize=8)
    ax.set_xlim(mdates.date2num(datetime.fromtimestamp(start, tz)), mdates.date2num(datetime.fromtimestamp(end, tz)))
    ax.xaxis_date(tz)
    return _finish(fig, ax, tz, f"Caídas por tipo ({label})", "", end - start)


def hours_chart(profile: list[dict], label: str) -> bytes:
    hrs = [p["hour"] for p in profile if p["lat"] is not None]
    if not hrs:
        return _empty(f"Latencia por hora ({label})")
    fig, ax = plt.subplots(figsize=(8, 3))
    lat = [p["lat"] for p in profile]
    ax.bar([p["hour"] for p in profile], [v or 0 for v in lat], color=BLUE)
    fails = [(p["hour"], p["fail_pct"]) for p in profile if p["fail_pct"]]
    if fails:
        ax2 = ax.twinx()
        ax2.plot([h for h, _ in fails], [f for _, f in fails], color=RED, marker="o", ms=4, lw=1.2)
        ax2.set_ylabel("% ciclos con fallo", color=RED)
    ax.set_xticks(range(0, 24, 2))
    ax.set_xlabel("hora del día (hora local)")
    ax.set_title(f"Latencia media por hora ({label})", loc="left", fontsize=12, fontweight="bold")
    ax.set_ylabel("ms")
    ax.grid(True, axis="y", alpha=0.25)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()
