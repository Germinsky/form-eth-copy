"""SQLite store: wallets, signals seen, paper positions, paper trades, sim state."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address TEXT PRIMARY KEY,
    label TEXT,
    status TEXT NOT NULL DEFAULT 'parked',      -- parked | watched
    stats_json TEXT NOT NULL DEFAULT '{}',
    independence_note TEXT,
    demo INTEGER NOT NULL DEFAULT 0,
    added_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS signals_seen (
    id TEXT PRIMARY KEY,
    wallet TEXT, token TEXT, side TEXT, t INTEGER,
    wallet_passed INTEGER, outcome TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT, symbol TEXT, status TEXT,         -- open | closed
    source_wallets TEXT,
    size_usd REAL, qty REAL,
    entry_t INTEGER, entry_price REAL,
    exit_t INTEGER, exit_price REAL,
    gas_usd REAL DEFAULT 0, pnl_usd REAL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER, side TEXT, token TEXT, symbol TEXT,
    t_signal INTEGER, t_fill INTEGER,
    signal_price REAL, mid_at_fill REAL, fill_price REAL,
    lag_ms INTEGER, impact_pct REAL, size_usd REAL, qty REAL, gas_usd REAL,
    trigger_signal TEXT
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
-- live wallet-swap ingestion (app/live_signals.py). One row per tx hash = dedup key. Kept across paper resets.
CREATE TABLE IF NOT EXISTS live_txs (
    tx TEXT PRIMARY KEY,
    block INTEGER, t INTEGER, wallet TEXT, token TEXT, side TEXT,
    status TEXT NOT NULL,                        -- signal | rejected | ignored
    reason TEXT, signal_json TEXT,
    seen_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS live_txs_sig ON live_txs(status, t);
-- LIVE execution (app/live.py). Real-money history: NEVER cleared by a paper reset.
CREATE TABLE IF NOT EXISTS live_orders (
    id TEXT PRIMARY KEY,
    created_at INTEGER, day TEXT,
    kind TEXT,                                   -- swap | approve
    side TEXT, token TEXT, symbol TEXT,
    ref TEXT,                                    -- buy:<paper_position_id> | sell:<live_position_id>
    sender TEXT,
    status TEXT NOT NULL,                        -- prepared | refused | sign_requested | rejected | submitted |
                                                 -- confirmed | failed | expired | mismatch
    usd_notional REAL, tx_json TEXT, quote_json TEXT, checks_json TEXT, sim_json TEXT,
    tx_hash TEXT, verified INTEGER DEFAULT 0, receipt_json TEXT,
    gas_usd REAL DEFAULT 0, pnl_usd REAL DEFAULT 0, updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS live_orders_day ON live_orders(day, status);
CREATE TABLE IF NOT EXISTS live_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT, symbol TEXT, decimals INTEGER, owner TEXT,
    status TEXT,                                 -- open | closed
    paper_position_id INTEGER,
    qty_raw TEXT, cost_usd REAL, realized_usd REAL DEFAULT 0,
    opened_at INTEGER, closed_at INTEGER
);
CREATE TABLE IF NOT EXISTS live_state (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # -- generic -------------------------------------------------------------
    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def x(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    def x_count(self, sql: str, args: tuple = ()) -> int:
        """Like x() but returns rowcount (reliable for INSERT OR IGNORE, unlike lastrowid)."""
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.rowcount

    # -- state ---------------------------------------------------------------
    def get_state(self, key: str, default: Any = None) -> Any:
        rows = self.q("SELECT value FROM state WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_state(self, key: str, value: Any) -> None:
        self.x("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))

    # -- wallets -------------------------------------------------------------
    @staticmethod
    def _wallet_row(r: dict) -> dict:
        r["stats"] = json.loads(r.pop("stats_json") or "{}")
        r["demo"] = bool(r["demo"])
        return r

    def list_wallets(self) -> list[dict]:
        return [self._wallet_row(r) for r in self.q("SELECT * FROM wallets ORDER BY added_at, address")]

    def get_wallet(self, address: str) -> dict | None:
        rows = self.q("SELECT * FROM wallets WHERE address=?", (address.lower(),))
        return self._wallet_row(rows[0]) if rows else None

    def upsert_wallet(self, address: str, label: str | None, stats: dict, note: str | None,
                      status: str = "parked", demo: bool = False) -> None:
        self.x("""INSERT INTO wallets(address,label,status,stats_json,independence_note,demo)
                  VALUES(?,?,?,?,?,?)
                  ON CONFLICT(address) DO UPDATE SET label=excluded.label, status=excluded.status,
                  stats_json=excluded.stats_json, independence_note=excluded.independence_note""",
               (address.lower(), label, status, json.dumps(stats), note, int(demo)))

    def set_wallet_status(self, address: str, status: str) -> None:
        self.x("UPDATE wallets SET status=? WHERE address=?", (status, address.lower()))

    def delete_wallet(self, address: str) -> None:
        self.x("DELETE FROM wallets WHERE address=?", (address.lower(),))

    # -- paper reset (does NOT touch the_record or live_txs) -----------------
    # The live scan cursor lives in `state` and is cleared too; the rescan is deduped by live_txs.
    def reset_paper(self) -> None:
        with self.lock:
            for t in ("signals_seen", "positions", "trades", "state"):
                self.conn.execute(f"DELETE FROM {t}")
            self.conn.commit()
