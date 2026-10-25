"""Carga de configuración desde variables de entorno / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

_DEFAULT_HTTP = [
    "https://www.google.com/generate_204",
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://www.wikipedia.org",
]


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    token: str
    chat_id: int
    tz_name: str = "Europe/Madrid"
    db_path: str = "/var/lib/netmonitor/netmonitor.db"
    router_label: str = "Router"
    iface: str = ""
    gateway: str = ""
    cycle_seconds: int = 30
    speedtest_minutes: int = 90
    speedtest_backend: str = "cloudflare"     # cloudflare | cli
    link_seconds: int = 300
    trace_hours: int = 6
    fail_threshold: int = 3
    recover_threshold: int = 2
    ping_targets: list[str] = field(default_factory=lambda: ["1.1.1.1", "8.8.8.8", "9.9.9.9"])
    dns_domains: list[str] = field(default_factory=lambda: ["google.com", "wikipedia.org", "cloudflare.com"])
    dns_public: list[str] = field(default_factory=lambda: ["1.1.1.1", "8.8.8.8"])
    http_urls: list[str] = field(default_factory=lambda: list(_DEFAULT_HTTP))
    trace_target: str = "1.1.1.1"
    retention_days: int = 30
    agg_retention_days: int = 365
    max_db_mb: int = 500

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)


def load_config() -> Config:
    """Lee el entorno. Lanza ValueError con un mensaje claro si falta algo esencial."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        raise ValueError("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID en el archivo .env")
    try:
        chat_id = int(chat)
    except ValueError as exc:
        raise ValueError("TELEGRAM_CHAT_ID debe ser un número") from exc
    return Config(
        token=token,
        chat_id=chat_id,
        tz_name=os.environ.get("TZ_DISPLAY", "Europe/Madrid"),
        db_path=os.environ.get("DB_PATH", "/var/lib/netmonitor/netmonitor.db"),
        router_label=os.environ.get("ROUTER_LABEL", "Router"),
        iface=os.environ.get("IFACE", "").strip(),
        gateway=os.environ.get("GATEWAY", "").strip(),
        cycle_seconds=max(10, _int("CYCLE_SECONDS", 30)),
        speedtest_minutes=max(10, _int("SPEEDTEST_MINUTES", 90)),
        speedtest_backend=os.environ.get("SPEEDTEST_BACKEND", "cloudflare").strip().lower(),
        link_seconds=max(30, _int("LINK_SECONDS", 300)),
        trace_hours=max(1, _int("TRACE_HOURS", 6)),
        fail_threshold=max(1, _int("FAIL_THRESHOLD", 3)),
        recover_threshold=max(1, _int("RECOVER_THRESHOLD", 2)),
        ping_targets=_list("PING_TARGETS", "1.1.1.1,8.8.8.8,9.9.9.9"),
        dns_domains=_list("DNS_DOMAINS", "google.com,wikipedia.org,cloudflare.com"),
        dns_public=_list("DNS_RESOLVERS_PUBLIC", "1.1.1.1,8.8.8.8"),
        http_urls=_list("HTTP_URLS", ",".join(_DEFAULT_HTTP)),
        trace_target=os.environ.get("TRACE_TARGET", "1.1.1.1"),
        retention_days=max(3, _int("RETENTION_DAYS", 30)),
        agg_retention_days=max(30, _int("AGG_RETENTION_DAYS", 365)),
        max_db_mb=max(50, _int("MAX_DB_MB", 500)),
    )
