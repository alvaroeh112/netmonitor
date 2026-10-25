"""Punto de entrada: arranca el monitor y el bot. El monitor funciona aunque Telegram no esté disponible."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time

from dotenv import load_dotenv
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application
from telegram.request import HTTPXRequest

from .bot import BotApp
from .config import load_config
from .db import Database
from .monitor import Monitor
from .tgnet import PinnedTransport, retry_send

log = logging.getLogger("netmonitor")


class RedactingFormatter(logging.Formatter):
    """Red de seguridad: el token nunca llega a un log, ni dentro de una URL ni de una traza de error."""

    def __init__(self, token: str, *a, **kw):
        super().__init__(*a, **kw)
        self.token = token

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).replace(self.token, "***")


def setup_logging(token: str) -> None:
    handler = logging.StreamHandler(sys.stdout)       # systemd/journald recoge la salida
    handler.setFormatter(RedactingFormatter(token, "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    for noisy in ("httpx", "httpcore", "telegram", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)   # httpx a nivel INFO imprime la URL con el token


async def run() -> None:
    load_dotenv(os.environ.get("NETMONITOR_ENV", ".env"))
    try:
        cfg = load_config()
    except ValueError as exc:
        print(f"Configuración incorrecta: {exc}", file=sys.stderr)
        sys.exit(2)
    setup_logging(cfg.token)
    os.makedirs(os.path.dirname(cfg.db_path) or ".", exist_ok=True)
    db = Database(cfg.db_path)

    holder: dict = {"app": None}

    async def notify(text: str) -> None:
        app = holder["app"]
        if app is None:
            raise RuntimeError("Telegram aún no está listo")
        await retry_send(lambda: app.bot.send_message(cfg.chat_id, text), tries=3)

    monitor = Monitor(cfg, db, notify)
    await monitor.start()
    bot_app = BotApp(cfg, db, monitor)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:      # Windows
            pass

    app: Application | None = None
    try:
        while not stop.is_set():
            try:
                pinned = PinnedTransport()
                app = (Application.builder().token(cfg.token)
                       .request(HTTPXRequest(connection_pool_size=8, connect_timeout=5, read_timeout=20, write_timeout=20,
                                             pool_timeout=10, httpx_kwargs={"transport": pinned}))
                       .get_updates_request(HTTPXRequest(connection_pool_size=2, connect_timeout=5, read_timeout=30,
                                                         write_timeout=20, pool_timeout=10,
                                                         httpx_kwargs={"transport": PinnedTransport()}))
                       .build())
                bot_app.register(app)
                await app.initialize()
                await bot_app.set_commands(app)
                await app.start()
                await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
                holder["app"] = app
                log.info("Bot de Telegram en marcha")
                last = db.get_setting("last_start_notice", "0")
                if time.time() - float(last or 0) > 600:
                    db.set_setting("last_start_notice", str(time.time()))
                    try:
                        await notify("🟢 NetMonitor en marcha. Escribe /ayuda para ver los comandos.")
                    except TelegramError:
                        pass
                await stop.wait()
            except TelegramError as exc:
                log.warning("Telegram no disponible (%s); reintento en 20 s", type(exc).__name__)
                holder["app"] = None
                if app is not None:
                    try:
                        await app.shutdown()
                    except Exception:  # noqa: BLE001
                        pass
                    app = None
                try:
                    await asyncio.wait_for(stop.wait(), 20)
                except asyncio.TimeoutError:
                    pass
    finally:
        log.info("Apagando…")
        holder["app"] = None
        if app is not None:
            try:
                if app.updater and app.updater.running:
                    await app.updater.stop()
                if app.running:
                    await app.stop()
                await app.shutdown()
            except Exception:  # noqa: BLE001
                pass
        await monitor.stop()
        db.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
