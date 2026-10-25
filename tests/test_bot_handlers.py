"""Los comandos responden con texto claro (nunca trazas de Python) y mandan fotos/documentos cuando toca."""
import time
from types import SimpleNamespace

import pytest

from conftest import populate
from netmonitor.bot import BotApp
from netmonitor.config import Config
from netmonitor.monitor import Monitor

CHAT = 4242


class FakeBot:
    def __init__(self):
        self.messages, self.photos, self.docs = [], [], []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        assert chat_id == CHAT
        self.messages.append((text, reply_markup))

    async def send_photo(self, chat_id, photo, **kw):
        assert chat_id == CHAT
        self.photos.append(photo)

    async def send_document(self, chat_id, document, filename=None, **kw):
        assert chat_id == CHAT
        self.docs.append((filename, document.read()))

    async def get_me(self):
        return SimpleNamespace(username="x")


@pytest.fixture
def env(db, tmp_path):
    cfg = Config(token="t", chat_id=CHAT, db_path=str(tmp_path / "t.db"))
    async def notify(_): pass
    mon = Monitor(cfg, db, notify)
    now = int(time.time())
    populate(db, now, hours=30, outage=(now - 7200, now - 7000, "sin_internet"))
    mon.last_cycle_ts = now - 10
    mon._tasks = [SimpleNamespace(done=lambda: False)]
    return BotApp(cfg, db, mon), FakeBot(), mon


def ctx(bot, *args):
    return SimpleNamespace(bot=bot, args=list(args))


@pytest.mark.parametrize("name,args", [
    ("cmd_ayuda", []), ("cmd_estado", []), ("cmd_resumen", []), ("cmd_resumen", ["7d"]), ("cmd_caidas", []),
    ("cmd_velocidad", []), ("cmd_horas", []), ("cmd_alertas", []), ("cmd_alertas", ["off"]), ("cmd_alertas", ["on"]),
])
async def test_commands_reply(env, name, args):
    app, bot, _ = env
    await getattr(app, name)(None, ctx(bot, *args))
    assert bot.messages and all("Traceback" not in m[0] for m in bot.messages)
    assert bot.messages[0][1] is not None or name == "cmd_alertas"      # botones inline


async def test_bad_period_gives_clear_spanish_error(env):
    app, bot, _ = env
    await app._guard(app.cmd_resumen)(None, ctx(bot, "90d"))
    assert "Periodo no válido" in bot.messages[-1][0]


async def test_unexpected_error_is_hidden_from_user(env, monkeypatch):
    app, bot, _ = env
    monkeypatch.setattr("netmonitor.stats.summary", lambda *a, **k: 1 / 0)
    await app._guard(app.cmd_resumen)(None, ctx(bot))
    assert "ZeroDivision" not in bot.messages[-1][0] and "Algo ha fallado" in bot.messages[-1][0]


async def test_informe_pdf_and_export_zip(env):
    app, bot, _ = env
    await app.cmd_informe(None, ctx(bot, "7d"))
    name, data = bot.docs[-1]
    assert name.endswith(".pdf") and data.startswith(b"%PDF")
    with pytest.raises(ValueError):
        await app.cmd_informe(None, ctx(bot, "24h"))
    await app.cmd_exportar(None, ctx(bot, "7d"))
    assert bot.docs[-1][0].endswith(".zip") and bot.docs[-1][1][:2] == b"PK"


async def test_test_command_runs_full_measure(env, monkeypatch):
    app, bot, mon = env

    async def fake_full():
        return {"ok": True, "down_mbps": 310.0, "up_mbps": 95.0, "ping_ms": 12.0, "server": "Movistar (Madrid)"}
    monkeypatch.setattr(mon, "full_test", fake_full)
    await app.cmd_test(None, ctx(bot))
    assert "310 Mbps" in bot.messages[-1][0] and "Movistar" in bot.messages[-1][0]


async def test_alertas_toggle_persists(env):
    app, bot, _ = env
    await app.cmd_alertas(None, ctx(bot, "off"))
    assert app.db.get_setting("alerts") == "off" and "desactivadas" in bot.messages[-1][0]
    await app.cmd_alertas(None, ctx(bot, "on"))
    assert app.db.get_setting("alerts") == "on"


async def test_diagnostico_all_green(env):
    app, bot, _ = env
    await app.cmd_diagnostico(None, ctx(bot))
    txt = bot.messages[-1][0]
    for needle in ("Recolector en marcha", "Última medición", "Base de datos accesible", "Espacio libre", "Telegram responde"):
        assert needle in txt
    assert "❌" not in txt


async def test_diagnostico_detects_stale_collector(env):
    app, bot, mon = env
    mon.last_cycle_ts = int(time.time()) - 600
    await app.cmd_diagnostico(None, ctx(bot))
    assert "❌ Última medición" in bot.messages[-1][0]


async def test_button_from_other_chat_is_ignored(env):
    app, bot, _ = env
    q = SimpleNamespace(message=SimpleNamespace(chat_id=999), data="est", answer=lambda: None)
    await app.on_button(SimpleNamespace(callback_query=q), ctx(bot))
    assert bot.messages == []
