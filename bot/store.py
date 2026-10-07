"""SQLite persistence: signals, Jev decisions, approvals, fills, events, key/value state."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL,
    stop REAL,
    take_profit REAL,
    qty INTEGER,
    notional REAL,
    status TEXT NOT NULL,          -- new, blocked_jev, blocked_risk, pending_approval, rejected, expired, submitted, error
    reason TEXT,
    order_id TEXT,
    realized_pnl REAL
);
CREATE TABLE IF NOT EXISTS jev_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    signal_id INTEGER,
    symbol TEXT,
    passed INTEGER NOT NULL,
    probabilities TEXT NOT NULL,   -- JSON {question: {outcome: p}}
    gates TEXT NOT NULL,           -- JSON [{question, outcome, op, threshold, value, passed}]
    latency_ms REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL,
    created TEXT NOT NULL,
    expires TEXT NOT NULL,
    status TEXT NOT NULL,          -- pending, approved, rejected, expired, executed
    decided_by TEXT,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id TEXT PRIMARY KEY,           -- broker order id
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    client_order_id TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._conn.executescript(SCHEMA)

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # key/value
    def get(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    # events
    def event(self, kind: str, message: str, level: str = "info") -> None:
        self.execute("INSERT INTO events(ts, level, kind, message) VALUES(?,?,?,?)", (now_iso(), level, kind, message))

    # signals
    def add_signal(self, **fields: Any) -> int:
        fields.setdefault("ts", now_iso())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.execute(f"INSERT INTO signals({cols}) VALUES({marks})", tuple(fields.values()))
        return int(cur.lastrowid)

    def update_signal(self, signal_id: int, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE signals SET {sets} WHERE id=?", (*fields.values(), signal_id))

    def signal(self, signal_id: int) -> dict | None:
        rows = self.query("SELECT * FROM signals WHERE id=?", (signal_id,))
        return rows[0] if rows else None

    # fills
    def add_fill(self, order_id: str, ts: str, symbol: str, side: str, qty: float, price: float,
                 client_order_id: str | None) -> bool:
        """Returns True if this fill is new."""
        cur = self.execute(
            "INSERT OR IGNORE INTO fills(id, ts, symbol, side, qty, price, client_order_id) VALUES(?,?,?,?,?,?,?)",
            (order_id, ts, symbol, side, qty, price, client_order_id),
        )
        return cur.rowcount == 1
