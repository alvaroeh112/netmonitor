"""Red resistente para hablar con Telegram cuando el DNS del operador falla a ratos.

El transporte resuelve api.telegram.org con DNS públicos fiables y conecta directamente por IP
(manteniendo Host y SNI para que el certificado se valide igual). Fuerza IPv4.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

import httpx
from telegram.error import NetworkError, TimedOut

from . import collectors as col

log = logging.getLogger(__name__)

TG_HOST = "api.telegram.org"
_TTL = 900


class PinnedTransport(httpx.AsyncBaseTransport):
    def __init__(self, host: str = TG_HOST, inner: httpx.AsyncBaseTransport | None = None,
                 resolver: Callable[[str], Awaitable[str | None]] = col._resolve_reliably):
        self.host = host
        self.inner = inner or httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=2)
        self._resolver = resolver
        self._ip: str | None = None
        self._ip_ts = 0.0

    async def _target_ip(self) -> str | None:
        if self._ip and time.monotonic() - self._ip_ts < _TTL:
            return self._ip
        ip = await self._resolver(self.host)
        if ip:
            self._ip, self._ip_ts = ip, time.monotonic()
        return self._ip            # si falla la resolución, se reutiliza la última IP conocida (o el nombre)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == self.host:
            ip = await self._target_ip()
            if ip:
                request.headers["Host"] = self.host
                request.extensions = {**request.extensions, "sni_hostname": self.host}
                request.url = request.url.copy_with(host=ip)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


async def retry_send(factory: Callable[[], Awaitable], tries: int = 4):
    """Reintenta un envío a Telegram ante timeouts de red (la conexión de este hogar falla a ratos)."""
    for attempt in range(1, tries + 1):
        try:
            return await factory()
        except (TimedOut, NetworkError) as exc:
            if attempt == tries:
                raise
            log.warning("Envío a Telegram falló (%s), reintento %d/%d", type(exc).__name__, attempt, tries - 1)
            await asyncio.sleep(attempt)
