"""Acceso a SQLite: esquema, escritura robusta (WAL + synchronous=FULL) y utilidades.

Todas las marcas de tiempo se guardan como epoch UTC (entero, segundos).
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles(          -- una fila por ciclo: base de la disponibilidad
    ts INTEGER PRIMARY KEY, state TEXT NOT NULL,
    link_ok INTEGER, gw_ok INTEGER, net_ok INTEGER, dns_ok INTEGER);
CREATE TABLE IF NOT EXISTS ping(
    ts INTEGER NOT NULL, target TEXT NOT NULL, kind TEXT NOT NULL,   -- kind: gateway | internet
    sent INTEGER, received INTEGER, loss REAL,
    rtt_min REAL, rtt_avg REAL, rtt_max REAL, jitter REAL);
CREATE INDEX IF NOT EXISTS idx_ping_ts ON ping(ts);
CREATE INDEX IF NOT EXISTS idx_ping_kind_ts ON ping(kind, ts);
CREATE TABLE IF NOT EXISTS dns(
    ts INTEGER NOT NULL, resolver TEXT NOT NULL, domain TEXT NOT NULL, ms REAL, ok INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_dns_ts ON dns(ts);
CREATE TABLE IF NOT EXISTS http(
    ts INTEGER NOT NULL, url TEXT NOT NULL, ms REAL, status INTEGER, ok INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_http_ts ON http(ts);
CREATE TABLE IF NOT EXISTS speed(
    ts INTEGER PRIMARY KEY, down_mbps REAL, up_mbps REAL, ping_ms REAL, server TEXT, ok INTEGER NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS link(
    ts INTEGER PRIMARY KEY, iface TEXT, speed_mbps INTEGER, duplex TEXT, carrier INTEGER,
    rx_errors INTEGER, tx_errors INTEGER, rx_dropped INTEGER, tx_dropped INTEGER, carrier_changes INTEGER);
CREATE TABLE IF NOT EXISTS sysinfo(
    ts INTEGER PRIMARY KEY, public_ip TEXT, gateway_ip TEXT, temp_c REAL, load1 REAL, mem_pct REAL);
CREATE TABLE IF NOT EXISTS ip_changes(ts INTEGER PRIMARY KEY, old_ip TEXT, new_ip TEXT);
CREATE TABLE IF NOT EXISTS trace(ts INTEGER PRIMARY KEY, target TEXT, hops_json TEXT, worst_hop INTEGER, summary TEXT);
CREATE TABLE IF NOT EXISTS incidents(
    id INTEGER PRIMARY KEY AUTOINCREMENT, start_ts INTEGER NOT NULL, end_ts INTEGER, type TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_inc_start ON incidents(start_ts);
CREATE TABLE IF NOT EXISTS agg_hour(
    hour_ts INTEGER NOT NULL, metric TEXT NOT NULL, n INTEGER, avg REAL, min REAL, max REAL, p95 REAL,
    PRIMARY KEY(hour_ts, metric));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, text TEXT NOT NULL);
"""


class Database:
    """Conexión única compartida y protegida con lock (las escrituras son pequeñas y rápidas)."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        # WAL + FULL: un corte de luz no corrompe la BD ni pierde transacciones confirmadas.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA wal_autocheckpoint=1000")
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        """Transacción atómica: todo o nada."""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple = ()):
        with self.tx() as c:
            return c.execute(sql, params)

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def size_mb(self) -> float:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return total / 1_048_576

    def integrity_ok(self) -> bool:
        row = self.one("PRAGMA quick_check")
        return bool(row) and row[0] == "ok"

    def close(self) -> None:
        with self._lock:
            self.conn.close()
