"""SQLite storage. One row per day, source and metric, so data from several sources sits side by side."""
from __future__ import annotations

import os
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings(
    date TEXT, source TEXT, metric TEXT, value REAL, cost REAL,
    PRIMARY KEY(date, source, metric));
CREATE TABLE IF NOT EXISTS rates(
    date TEXT, fuel TEXT, unit_rate REAL, standing REAL, source TEXT,
    PRIMARY KEY(date, fuel, source));
CREATE TABLE IF NOT EXISTS weather(
    date TEXT PRIMARY KEY, temp_mean REAL, temp_min REAL, temp_max REAL);
CREATE TABLE IF NOT EXISTS sync_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, ok INTEGER, message TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def _exec(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall()
            self._conn.commit()
            return rows

    def _many(self, sql, rows):
        rows = list(rows)
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()
        return len(rows)

    # ---- writes
    def upsert_readings(self, rows):
        """rows: (date, source, metric, value, cost)"""
        return self._many("INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?)", rows)

    def upsert_rates(self, rows):
        """rows: (date, fuel, unit_rate, standing, source)"""
        return self._many("INSERT OR REPLACE INTO rates VALUES (?,?,?,?,?)", rows)

    def upsert_weather(self, rows):
        """rows: (date, temp_mean, temp_min, temp_max)"""
        return self._many("INSERT OR REPLACE INTO weather VALUES (?,?,?,?)", rows)

    def log(self, source: str, ok: bool, message: str):
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._exec("INSERT INTO sync_log(ts, source, ok, message) VALUES (?,?,?,?)",
                   (ts, source, int(ok), str(message)[:500]))
        self._exec("DELETE FROM sync_log WHERE id NOT IN (SELECT id FROM sync_log ORDER BY id DESC LIMIT 200)")

    def set_kv(self, key, value):
        self._exec("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, value))

    def get_kv(self, key, default=None):
        rows = self._exec("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0][0] if rows else default

    # ---- reads
    def readings(self) -> dict:
        """{date: {metric: {source: value}}}"""
        out: dict = defaultdict(lambda: defaultdict(dict))
        for d, src, metric, value in self._exec("SELECT date, source, metric, value FROM readings"):
            out[d][metric][src] = value
        return out

    def rates(self) -> dict:
        """{fuel: {date: (unit_rate, standing)}} — OVO-reported rates."""
        out: dict = defaultdict(dict)
        for d, fuel, unit, standing in self._exec("SELECT date, fuel, unit_rate, standing FROM rates"):
            out[fuel][d] = (unit, standing)
        return out

    def weather(self) -> dict:
        return {d: t for d, t in self._exec("SELECT date, temp_mean FROM weather") if t is not None}

    def source_summary(self):
        return [
            {"source": s, "metric": m, "days": n, "first": a, "last": b}
            for s, m, n, a, b in self._exec(
                "SELECT source, metric, COUNT(*), MIN(date), MAX(date) FROM readings "
                "GROUP BY source, metric ORDER BY metric, source")
        ]

    def recent_log(self, limit=20):
        return [
            {"ts": ts, "source": s, "ok": bool(ok), "message": m}
            for ts, s, ok, m in self._exec(
                "SELECT ts, source, ok, message FROM sync_log ORDER BY id DESC LIMIT ?", (limit,))
        ]
