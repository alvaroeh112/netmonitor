import logging
import os
import subprocess
import sys
import textwrap
import time

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import filters

from conftest import populate
from netmonitor import export, messages as msg
from netmonitor.db import Database
from netmonitor.main import RedactingFormatter

ROOT = os.path.dirname(os.path.dirname(__file__))
NOW = 1_800_000_000


def test_wal_and_full_sync(db):
    assert db.one("PRAGMA journal_mode")[0] == "wal"
    assert db.one("PRAGMA synchronous")[0] == 2           # FULL
    assert db.integrity_ok()


def test_transaction_rolls_back_atomically(db):
    with pytest.raises(RuntimeError):
        with db.tx() as c:
            c.execute("INSERT INTO cycles VALUES(1,'ok',1,1,1,1)")
            raise RuntimeError("fallo a mitad")
    assert db.one("SELECT COUNT(*) n FROM cycles")["n"] == 0


def test_survives_kill_9_during_writes(tmp_path):
    """Un proceso escribe sin parar y lo matamos con SIGKILL: la BD debe quedar íntegra y con lo confirmado."""
    if sys.platform == "win32":
        pytest.skip("SIGKILL real solo en Linux")
    path = str(tmp_path / "k.db")
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {ROOT!r})
        from netmonitor.db import Database
        d = Database({path!r})
        i = 0
        while True:
            d.execute("INSERT INTO cycles VALUES(?,?,?,?,?,?)", (i, 'ok', 1, 1, 1, 1))
            i += 1
            if i == 50: print("listo", flush=True)
    """)
    p = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE)
    assert p.stdout.readline().strip() == b"listo"
    time.sleep(0.3)
    p.kill()
    p.wait()
    d = Database(path)
    assert d.integrity_ok()
    n = d.one("SELECT COUNT(*) n FROM cycles")["n"]
    assert n >= 50
    assert d.one("SELECT MAX(ts) m FROM cycles")["m"] == n - 1          # sin huecos: lo confirmado está entero
    d.close()


def test_no_impossible_values_in_synthetic_data(db):
    populate(db, NOW, hours=6)
    assert db.one("SELECT COUNT(*) n FROM ping WHERE rtt_avg<0 OR rtt_min<0 OR loss<0 OR loss>100")["n"] == 0
    ts = [r["ts"] for r in db.query("SELECT ts FROM cycles ORDER BY ts")]
    assert max(b - a for a, b in zip(ts, ts[1:])) == 30


# ---------------------------------------------------------------- seguridad
def _update(chat_id: int) -> Update:
    chat = Chat(chat_id, "private")
    m = Message(1, __import__("datetime").datetime.now(), chat, from_user=User(chat_id, "x", False), text="/estado")
    return Update(1, message=m)


def test_only_authorized_chat_passes_filter():
    auth = filters.Chat(chat_id=12345)
    assert auth.check_update(_update(12345))
    assert not auth.check_update(_update(99999))


def test_token_is_redacted_from_logs():
    token = "123456789:" + "A" * 35
    fmt = RedactingFormatter(token, "%(message)s")
    rec = logging.LogRecord("x", logging.INFO, "", 0, "GET https://api.telegram.org/bot%s/getMe", (token,), None)
    assert token not in fmt.format(rec)
    try:
        raise RuntimeError(f"fallo en bot{token}")
    except RuntimeError:
        rec2 = logging.LogRecord("x", logging.ERROR, "", 0, "error", (), sys.exc_info())
    assert token not in fmt.format(rec2)


def test_repo_has_no_token_and_env_is_ignored():
    gi = open(os.path.join(ROOT, ".gitignore")).read()
    assert ".env" in gi.splitlines()
    import re
    pat = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}")
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in {".venv", ".git", "__pycache__", ".pytest_cache"}]
        for fn in files:
            if fn in (".env",) or fn.endswith((".db", ".pyc", ".png", ".pdf", ".secret")):
                continue
            try:
                txt = open(os.path.join(base, fn), encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            assert not pat.search(txt), f"posible token en {fn}"


# ---------------------------------------------------------------- exportación
def test_export_zip_contents(db):
    import io
    import zipfile
    from zoneinfo import ZoneInfo
    populate(db, NOW, hours=3, outage=(NOW - 3000, NOW - 2000, "router_inalcanzable"))
    z = zipfile.ZipFile(io.BytesIO(export.build_zip(db, 3 * 3600, ZoneInfo("Europe/Madrid"), NOW)))
    names = set(z.namelist())
    assert {"caidas.csv", "mediciones_ping.csv", "velocidad.csv", "ciclos.csv"} <= names
    lines = z.read("caidas.csv").decode().strip().splitlines()
    assert len(lines) == 2 and lines[1].endswith(",1000,router_inalcanzable")
    assert z.read("ciclos.csv").decode().splitlines()[0].startswith("fecha_utc,fecha_madrid,state")


async def test_pinned_transport_connects_by_ip_keeping_host_and_sni():
    import httpx
    from netmonitor.tgnet import PinnedTransport
    seen = {}

    def handler(request):
        seen["host"], seen["hdr"], seen["sni"] = request.url.host, request.headers["Host"], request.extensions.get("sni_hostname")
        return httpx.Response(200, json={"ok": True})

    async def resolver(host):
        return "149.154.166.110"

    t = PinnedTransport(inner=httpx.MockTransport(handler), resolver=resolver)
    async with httpx.AsyncClient(transport=t) as c:
        r = await c.get("https://api.telegram.org/botX/getMe")
        await c.get("https://example.org/")                 # otros dominios no se tocan
    assert r.status_code == 200
    assert seen["host"] == "example.org"
    t2 = PinnedTransport(inner=httpx.MockTransport(handler), resolver=resolver)
    async with httpx.AsyncClient(transport=t2) as c:
        await c.get("https://api.telegram.org/botX/getMe")
    assert (seen["host"], seen["hdr"], seen["sni"]) == ("149.154.166.110", "api.telegram.org", "api.telegram.org")


async def test_pinned_transport_falls_back_when_resolution_fails():
    import httpx
    from netmonitor.tgnet import PinnedTransport
    seen = {}

    async def none(host):
        return None

    t = PinnedTransport(inner=httpx.MockTransport(lambda r: seen.update(host=r.url.host) or httpx.Response(200)), resolver=none)
    async with httpx.AsyncClient(transport=t) as c:
        await c.get("https://api.telegram.org/x")
    assert seen["host"] == "api.telegram.org"


async def test_retry_send_retries_then_succeeds_and_gives_up():
    from telegram.error import TimedOut
    from netmonitor import tgnet
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimedOut("x")
        return "ok"

    async def fast_sleep(_):
        return None
    orig, tgnet.asyncio.sleep = tgnet.asyncio.sleep, fast_sleep
    try:
        assert await tgnet.retry_send(flaky) == "ok" and calls["n"] == 3
        async def dead():
            raise TimedOut("x")
        with pytest.raises(TimedOut):
            await tgnet.retry_send(dead, tries=2)
    finally:
        tgnet.asyncio.sleep = orig
