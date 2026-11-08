"""SQLite persistence: events, candidates, positions, orders/fills, equity, day trades, key-value state."""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ..models import (
    EarningsEvent,
    Fill,
    OptionKey,
    Position,
    PositionLeg,
    _json_default,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  key TEXT PRIMARY KEY, symbol TEXT, report_date TEXT, timing TEXT, company TEXT,
  market_cap REAL, eps_estimate REAL, confirmed INTEGER, sources TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS candidates (
  day TEXT, symbol TEXT, play_type TEXT, score REAL, quant_score REAL, payload TEXT, created_at TEXT,
  PRIMARY KEY (day, symbol, play_type));
CREATE TABLE IF NOT EXISTS positions (
  id TEXT PRIMARY KEY, symbol TEXT, play_type TEXT, kind TEXT, status TEXT, opened_at TEXT, closed_at TEXT,
  reaction_date TEXT, expiry TEXT, cost_basis REAL, realized_pnl REAL, payload TEXT);
CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY, position_id TEXT, symbol TEXT, occ TEXT, side TEXT, qty INTEGER,
  limit_price REAL, status TEXT, filled_qty INTEGER, avg_fill REAL, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, position_id TEXT, occ TEXT, side TEXT, qty INTEGER,
  price REAL, commission REAL, at TEXT);
CREATE TABLE IF NOT EXISTS equity (at TEXT PRIMARY KEY, equity REAL, cash REAL, note TEXT);
CREATE TABLE IF NOT EXISTS day_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, symbol TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, kind TEXT, symbol TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS daily (day TEXT PRIMARY KEY, start_equity REAL, end_equity REAL, realized_pnl REAL, trades INTEGER);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ helpers
    def _exec(self, sql: str, params: tuple = ()):
        with self._lock:
            return self._conn.execute(sql, params)

    def _rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    # ------------------------------------------------------------------ kv
    def set(self, k: str, v: Any) -> None:
        self._exec("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v, default=_json_default)))

    def get(self, k: str, default: Any = None) -> Any:
        rows = self._rows("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(rows[0]["v"]) if rows else default

    def delete(self, k: str) -> None:
        self._exec("DELETE FROM kv WHERE k=?", (k,))

    # ------------------------------------------------------------------ events
    def upsert_events(self, events: list[EarningsEvent]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for e in events:
                self._conn.execute(
                    """INSERT INTO events(key,symbol,report_date,timing,company,market_cap,eps_estimate,confirmed,sources,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(key) DO UPDATE SET timing=excluded.timing, company=excluded.company,
                         market_cap=excluded.market_cap, eps_estimate=excluded.eps_estimate, confirmed=excluded.confirmed,
                         sources=excluded.sources, updated_at=excluded.updated_at""",
                    (e.key, e.symbol, e.report_date.isoformat(), e.timing, e.company, e.market_cap, e.eps_estimate,
                     int(e.confirmed), json.dumps(e.sources), now),
                )

    def events_between(self, a: date, b: date) -> list[EarningsEvent]:
        rows = self._rows("SELECT * FROM events WHERE report_date BETWEEN ? AND ? ORDER BY report_date, symbol", (a.isoformat(), b.isoformat()))
        return [
            EarningsEvent(r["symbol"], date.fromisoformat(r["report_date"]), r["timing"], r["company"] or "", r["market_cap"],
                          r["eps_estimate"], bool(r["confirmed"]), json.loads(r["sources"] or "[]"))
            for r in rows
        ]

    # ------------------------------------------------------------------ candidates
    def save_candidate(self, day: date, symbol: str, play_type: str, score: float, quant_score: float, payload: dict) -> None:
        self._exec(
            """INSERT INTO candidates(day,symbol,play_type,score,quant_score,payload,created_at) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(day,symbol,play_type) DO UPDATE SET score=excluded.score, quant_score=excluded.quant_score,
               payload=excluded.payload, created_at=excluded.created_at""",
            (day.isoformat(), symbol, play_type, score, quant_score, json.dumps(payload, default=_json_default), datetime.now(timezone.utc).isoformat()),
        )

    def candidates_for(self, day: date) -> list[dict]:
        rows = self._rows("SELECT * FROM candidates WHERE day=? ORDER BY score DESC", (day.isoformat(),))
        return [dict(r, payload=json.loads(r["payload"])) for r in rows]

    # ------------------------------------------------------------------ positions
    def save_position(self, p: Position) -> None:
        payload = json.dumps(asdict(p), default=_json_default)
        self._exec(
            """INSERT INTO positions(id,symbol,play_type,kind,status,opened_at,closed_at,reaction_date,expiry,cost_basis,realized_pnl,payload)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status, closed_at=excluded.closed_at, cost_basis=excluded.cost_basis,
                 realized_pnl=excluded.realized_pnl, payload=excluded.payload""",
            (p.id, p.symbol, p.play_type, p.kind, p.status, p.opened_at.isoformat(), p.closed_at.isoformat() if p.closed_at else None,
             p.reaction_date.isoformat(), p.expiry.isoformat(), p.cost_basis, p.realized_pnl, payload),
        )

    def _row_to_position(self, r: sqlite3.Row) -> Position:
        d = json.loads(r["payload"])
        legs = [PositionLeg(OptionKey(l["key"]["symbol"], date.fromisoformat(l["key"]["expiry"]), float(l["key"]["strike"]), l["key"]["right"]),
                            int(l["qty"]), float(l["avg_cost"]), int(l["remaining"]), float(l.get("realized", 0.0))) for l in d["legs"]]
        return Position(
            id=d["id"], symbol=d["symbol"], play_type=d["play_type"], kind=d["kind"], legs=legs,
            opened_at=datetime.fromisoformat(d["opened_at"]), reaction_date=date.fromisoformat(d["reaction_date"]),
            expiry=date.fromisoformat(d["expiry"]), cost_basis=float(d["cost_basis"]), status=d["status"],
            high_water_value=float(d.get("high_water_value", 0.0)), took_half=bool(d.get("took_half", False)),
            realized_pnl=float(d.get("realized_pnl", 0.0)),
            closed_at=datetime.fromisoformat(d["closed_at"]) if d.get("closed_at") else None,
            notes=list(d.get("notes", [])), meta=dict(d.get("meta", {})),
        )

    def open_positions(self) -> list[Position]:
        return [self._row_to_position(r) for r in self._rows("SELECT * FROM positions WHERE status='open' ORDER BY opened_at")]

    def all_positions(self, limit: int = 200) -> list[Position]:
        return [self._row_to_position(r) for r in self._rows("SELECT * FROM positions ORDER BY opened_at DESC LIMIT ?", (limit,))]

    def position(self, pid: str) -> Optional[Position]:
        rows = self._rows("SELECT * FROM positions WHERE id=?", (pid,))
        return self._row_to_position(rows[0]) if rows else None

    def positions_opened_on(self, day: date) -> list[Position]:
        return [p for p in self.all_positions(500) if p.opened_at.astimezone().date() == day or p.opened_at.date() == day]

    # ------------------------------------------------------------------ orders / fills
    def record_order(self, order_id: str, position_id: str, key: OptionKey, side: str, qty: int, limit_price: float, status: str = "open") -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._exec(
            """INSERT INTO orders(order_id,position_id,symbol,occ,side,qty,limit_price,status,filled_qty,avg_fill,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,0,0,?,?) ON CONFLICT(order_id) DO UPDATE SET limit_price=excluded.limit_price, status=excluded.status, updated_at=excluded.updated_at""",
            (order_id, position_id, key.symbol, key.occ(), side, qty, limit_price, status, now, now),
        )

    def update_order(self, order_id: str, status: str, filled_qty: int, avg_fill: float) -> None:
        self._exec("UPDATE orders SET status=?, filled_qty=?, avg_fill=?, updated_at=? WHERE order_id=?",
                   (status, filled_qty, avg_fill, datetime.now(timezone.utc).isoformat(), order_id))

    def record_fill(self, f: Fill, position_id: str) -> None:
        self._exec("INSERT INTO fills(order_id,position_id,occ,side,qty,price,commission,at) VALUES(?,?,?,?,?,?,?,?)",
                   (f.order_id, position_id, f.key.occ(), f.side, f.qty, f.price, f.commission, f.at.isoformat()))

    def fills_for(self, position_id: str) -> list[dict]:
        return [dict(r) for r in self._rows("SELECT * FROM fills WHERE position_id=? ORDER BY at", (position_id,))]

    # ------------------------------------------------------------------ equity / daily
    def snapshot_equity(self, at: datetime, equity: float, cash: float, note: str = "") -> None:
        self._exec("INSERT OR REPLACE INTO equity(at,equity,cash,note) VALUES(?,?,?,?)", (at.isoformat(), equity, cash, note))

    def equity_series(self, limit: int = 1000) -> list[dict]:
        return [dict(r) for r in self._rows("SELECT * FROM equity ORDER BY at DESC LIMIT ?", (limit,))][::-1]

    def start_day(self, day: date, equity: float) -> float:
        rows = self._rows("SELECT start_equity FROM daily WHERE day=?", (day.isoformat(),))
        if rows and rows[0]["start_equity"] is not None:
            return float(rows[0]["start_equity"])
        self._exec("INSERT INTO daily(day,start_equity,end_equity,realized_pnl,trades) VALUES(?,?,NULL,0,0) ON CONFLICT(day) DO UPDATE SET start_equity=excluded.start_equity",
                   (day.isoformat(), equity))
        return equity

    def end_day(self, day: date, equity: float, realized_pnl: float, trades: int) -> None:
        self._exec("INSERT INTO daily(day,start_equity,end_equity,realized_pnl,trades) VALUES(?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET end_equity=excluded.end_equity, realized_pnl=excluded.realized_pnl, trades=excluded.trades",
                   (day.isoformat(), equity, equity, realized_pnl, trades))

    def daily_rows(self, limit: int = 60) -> list[dict]:
        return [dict(r) for r in self._rows("SELECT * FROM daily ORDER BY day DESC LIMIT ?", (limit,))][::-1]

    def consecutive_losing_days(self) -> int:
        n = 0
        for r in reversed(self.daily_rows(30)):
            if r["end_equity"] is None or r["start_equity"] is None:
                continue
            if r["end_equity"] < r["start_equity"]:
                n += 1
            else:
                break
        return n

    # ------------------------------------------------------------------ day trades
    def record_day_trade(self, day: date, symbol: str, note: str = "") -> None:
        self._exec("INSERT INTO day_trades(day,symbol,note) VALUES(?,?,?)", (day.isoformat(), symbol, note))

    def day_trades_since(self, since: date) -> int:
        return int(self._rows("SELECT COUNT(*) AS n FROM day_trades WHERE day>=?", (since.isoformat(),))[0]["n"])

    # ------------------------------------------------------------------ decisions
    def log_decision(self, kind: str, symbol: str, payload: dict | str, at: Optional[datetime] = None) -> None:
        self._exec("INSERT INTO decisions(at,kind,symbol,payload) VALUES(?,?,?,?)",
                   ((at or datetime.now(timezone.utc)).isoformat(), kind, symbol, payload if isinstance(payload, str) else json.dumps(payload, default=_json_default)))

    def realized_on(self, day: date) -> tuple[float, int]:
        """Dollars realized by exit fills on a given ET day, and the number of such fills."""
        total, n = 0.0, 0
        for r in self._rows("SELECT at, payload FROM decisions WHERE kind='exit'"):
            try:
                at = datetime.fromisoformat(r["at"])
                if at.tzinfo is not None:
                    from ..clock import ET
                    at = at.astimezone(ET)
                if at.date() != day:
                    continue
                total += float(json.loads(r["payload"]).get("realized", 0.0))
                n += 1
            except Exception:  # noqa: BLE001
                continue
        return total, n

    def decisions(self, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self._rows("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
