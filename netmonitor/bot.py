"""Bot de Telegram: comandos, botones inline y errores en español. Solo atiende al chat autorizado."""
from __future__ import annotations

import asyncio
import io
import logging
import shutil
import time

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

from . import charts, export, messages as msg, report, stats
from .config import Config
from .db import Database
from .monitor import Monitor
from .tgnet import retry_send

log = logging.getLogger(__name__)


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])


PERIOD_ROW = lambda prefix: [("24 h", f"{prefix}:24h"), ("7 días", f"{prefix}:7d"), ("30 días", f"{prefix}:30d")]  # noqa: E731


class BotApp:
    def __init__(self, cfg: Config, db: Database, monitor: Monitor):
        self.cfg, self.db, self.monitor = cfg, db, monitor
        self._busy: set[str] = set()

    # ------------------------------------------------------------ construcción
    def register(self, app: Application) -> None:
        auth = filters.Chat(chat_id=self.cfg.chat_id)       # el resto de chats se ignora sin respuesta
        for name, fn in {
            "start": self.cmd_ayuda, "ayuda": self.cmd_ayuda, "estado": self.cmd_estado, "resumen": self.cmd_resumen,
            "caidas": self.cmd_caidas, "velocidad": self.cmd_velocidad, "test": self.cmd_test, "horas": self.cmd_horas,
            "informe": self.cmd_informe, "exportar": self.cmd_exportar, "alertas": self.cmd_alertas,
            "diagnostico": self.cmd_diagnostico,
        }.items():
            app.add_handler(CommandHandler(name, self._guard(fn), filters=auth))
        app.add_handler(CallbackQueryHandler(self.on_button, pattern=r"^[a-z]+(:\w+)?$"))
        app.add_error_handler(self.on_error)

    async def set_commands(self, app: Application) -> None:
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in msg.COMMANDS])

    def _guard(self, fn):
        """Convierte cualquier excepción en un mensaje claro (nunca trazas de Python al usuario)."""
        async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            try:
                await fn(update, ctx)
            except ValueError as exc:
                await self.say(ctx, str(exc))
            except Exception:  # noqa: BLE001
                log.exception("Error en comando %s", fn.__name__)
                await self.say(ctx, msg.ERROR_GENERIC)
        return wrapper

    async def say(self, ctx, text: str, markup=None) -> None:
        await retry_send(lambda: ctx.bot.send_message(self.cfg.chat_id, text, reply_markup=markup))

    async def photo(self, ctx, png: bytes) -> None:
        await retry_send(lambda: ctx.bot.send_photo(self.cfg.chat_id, png))

    async def doc(self, ctx, data: bytes, filename: str, caption: str, markup=None) -> None:
        await retry_send(lambda: ctx.bot.send_document(self.cfg.chat_id, io.BytesIO(data), filename=filename,
                                                       caption=caption, reply_markup=markup))

    async def on_error(self, update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        err = ctx.error
        if isinstance(err, TelegramError):
            log.warning("Error de Telegram: %s", type(err).__name__)     # sin detalles (podrían incluir la URL)
        else:
            log.error("Error no controlado: %s", type(err).__name__)

    # ------------------------------------------------------------ botones
    async def on_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.message is None or q.message.chat_id != self.cfg.chat_id:
            return
        await q.answer()
        action, _, arg = (q.data or "").partition(":")
        table = {"est": self.cmd_estado, "res": self.cmd_resumen, "cai": self.cmd_caidas, "vel": self.cmd_velocidad,
                 "tst": self.cmd_test, "hor": self.cmd_horas, "inf": self.cmd_informe, "exp": self.cmd_exportar}
        fn = table.get(action)
        if fn is None:
            return
        ctx.args = [arg] if arg else []
        try:
            await fn(update, ctx)
        except ValueError as exc:
            await self.say(ctx, str(exc))
        except Exception:  # noqa: BLE001
            log.exception("Error en botón %s", action)
            await self.say(ctx, msg.ERROR_GENERIC)

    # ------------------------------------------------------------ comandos
    async def cmd_ayuda(self, update, ctx):
        await self.say(ctx, msg.AYUDA, kb([("📊 Estado", "est"), ("📈 Resumen", "res:24h")], [("🔴 Caídas", "cai:7d"), ("⚡ Test", "tst")]))

    async def cmd_estado(self, update, ctx):
        st = await asyncio.to_thread(stats.latest_status, self.db)
        await self.say(ctx, msg.estado(st, self.cfg.tz), kb([("📊 Resumen 24h", "res:24h"), ("🔴 Ver caídas", "cai:7d")], [("⚡ Test ahora", "tst")]))

    def _period(self, ctx, default="24h") -> tuple[str, int]:
        return stats.parse_period(ctx.args[0] if ctx.args else None, default)

    async def cmd_resumen(self, update, ctx):
        label, secs = self._period(ctx)
        s = await asyncio.to_thread(stats.summary, self.db, secs)
        text = msg.resumen(s, label, self.cfg.tz)
        await self.say(ctx, text, kb(PERIOD_ROW("res"), [("🔴 Ver caídas", f"cai:{label}"), ("📄 Generar informe", "inf:7d")]))
        if s["cycles"]:
            png = await asyncio.to_thread(charts.latency_chart, self.db, secs, self.cfg.tz, label)
            await self.photo(ctx, png)

    async def cmd_caidas(self, update, ctx):
        label, secs = self._period(ctx, "7d")
        now = int(time.time())
        incs = await asyncio.to_thread(stats.incidents_between, self.db, now - secs, now, now)
        await self.say(ctx, msg.caidas(incs, label, self.cfg.tz),
                       kb(PERIOD_ROW("cai"), [("📊 Resumen", f"res:{label}"), ("📄 Generar informe", "inf:7d")]))

    async def cmd_velocidad(self, update, ctx):
        rows = await asyncio.to_thread(self.db.query, "SELECT * FROM speed ORDER BY ts DESC LIMIT 12")
        await self.say(ctx, msg.velocidad(rows, self.cfg.tz), kb([("⚡ Medir ahora", "tst"), ("📊 Resumen 7d", "res:7d")]))
        if rows:
            png = await asyncio.to_thread(charts.speed_chart, self.db, 7 * 86400, self.cfg.tz, "7d")
            await self.photo(ctx, png)

    async def cmd_test(self, update, ctx):
        if "test" in self._busy:
            await self.say(ctx, "⏳ Ya hay un test en marcha, espera a que termine.")
            return
        self._busy.add("test")
        try:
            await self.say(ctx, "⏳ Midiendo ahora (ping, DNS, web y velocidad). Tarda cerca de un minuto…")
            res = await self.monitor.full_test()
            row = await asyncio.to_thread(self.db.one, "SELECT state FROM cycles ORDER BY ts DESC LIMIT 1")
            net = await asyncio.to_thread(self.db.query, "SELECT AVG(loss) l, AVG(rtt_avg) a FROM ping WHERE kind='internet' AND ts=(SELECT MAX(ts) FROM ping)")
            extra = f"📡 Latencia internet {msg.num(net[0]['a'], 0, 'ms')} · pérdida {msg.num(net[0]['l'], 0, '%')}" if net and net[0]["l"] is not None else ""
            await self.say(ctx, msg.test_result(res, extra), kb([("📊 Estado", "est"), ("⚡ Velocidad", "vel")]))
        finally:
            self._busy.discard("test")

    async def cmd_horas(self, update, ctx):
        label, secs = self._period(ctx, "7d")
        if secs < 3 * 86400:
            label, secs = "7d", 7 * 86400
        prof = await asyncio.to_thread(stats.hourly_profile, self.db, secs, self.cfg.tz)
        bad = stats.worst_hours(prof)
        await self.say(ctx, msg.horas(prof, bad, label), kb([("7 días", "hor:7d"), ("30 días", "hor:30d")]))
        if any(p["n"] for p in prof):
            await self.photo(ctx, charts.hours_chart(prof, label))

    async def cmd_informe(self, update, ctx):
        label, secs = self._period(ctx, "7d")
        if label not in ("7d", "30d"):
            raise ValueError("El informe admite 7d o 30d. Ejemplo: /informe 30d")
        if "informe" in self._busy:
            await self.say(ctx, "⏳ Ya estoy generando un informe.")
            return
        self._busy.add("informe")
        try:
            await self.say(ctx, "📄 Generando el informe PDF…")
            pdf = await asyncio.to_thread(report.build_report, self.db, self.cfg, label, secs)
            name = f"informe_conexion_{label}_{time.strftime('%Y%m%d')}.pdf"
            await self.doc(ctx, pdf, name, "Informe para el técnico. Mide por cable, no el wifi.",
                           kb([("7 días", "inf:7d"), ("30 días", "inf:30d")], [("📦 Exportar datos", f"exp:{label}")]))
        finally:
            self._busy.discard("informe")

    async def cmd_exportar(self, update, ctx):
        label, secs = self._period(ctx, "7d")
        data = await asyncio.to_thread(export.build_zip, self.db, secs, self.cfg.tz)
        await self.doc(ctx, data, f"netmonitor_{label}_{time.strftime('%Y%m%d')}.zip",
                       f"📦 Datos de {label} en CSV (un archivo por tabla, dentro del ZIP).")

    async def cmd_alertas(self, update, ctx):
        arg = (ctx.args[0].lower() if ctx.args else "")
        if arg in ("on", "off"):
            await asyncio.to_thread(self.db.set_setting, "alerts", arg)
            await self.say(ctx, "🔔 Alertas activadas: te avisaré de caídas y recuperaciones." if arg == "on" else "🔕 Alertas desactivadas. Las caídas se siguen registrando.")
        else:
            cur = self.db.get_setting("alerts", "on")
            await self.say(ctx, f"Las alertas están {'activadas 🔔' if cur == 'on' else 'desactivadas 🔕'}. Usa /alertas on o /alertas off.")

    async def cmd_diagnostico(self, update, ctx):
        checks: list[tuple[bool, str]] = []
        mon = self.monitor
        alive = bool(mon._tasks) and not any(t.done() for t in mon._tasks)
        checks.append((alive, "Recolector en marcha" if alive else "El recolector se ha detenido"))
        last = mon.last_cycle_ts
        age = int(time.time()) - last if last else None
        checks.append((age is not None and age < 120, f"Última medición hace {msg.fmt_dur(age)}" if age is not None else "Aún no hay mediciones"))
        try:
            ok = await asyncio.to_thread(self.db.integrity_ok)
            n = (await asyncio.to_thread(self.db.one, "SELECT COUNT(*) n FROM cycles"))["n"]
            checks.append((ok, f"Base de datos accesible e íntegra ({self.db.size_mb():.0f} MB, {n} ciclos)" if ok else "La base de datos tiene problemas de integridad"))
        except Exception:  # noqa: BLE001
            checks.append((False, "No se puede acceder a la base de datos"))
        du = shutil.disk_usage(self.cfg.db_path.rsplit("/", 1)[0] or "/")
        free_pct = 100 * du.free / du.total
        checks.append((du.free > 500 * 1024 * 1024 and free_pct > 5, f"Espacio libre: {du.free / 1e9:.1f} GB ({free_pct:.0f} %)"))
        t0 = time.perf_counter()
        try:
            await ctx.bot.get_me()
            checks.append((True, f"Telegram responde ({(time.perf_counter() - t0) * 1000:.0f} ms)"))
        except Exception:  # noqa: BLE001
            checks.append((False, "Telegram no responde"))
        if mon.last_error:
            checks.append((False, f"Último error interno: {mon.last_error[:120]}"))
        pending = (await asyncio.to_thread(self.db.one, "SELECT COUNT(*) n FROM outbox"))["n"]
        if pending:
            checks.append((False, f"{pending} avisos pendientes de enviar"))
        await self.say(ctx, msg.diagnostico(checks), kb([("📊 Estado", "est")]))
