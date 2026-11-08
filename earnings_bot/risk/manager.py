"""Hard risk limits. Nothing upstream (scoring, LLM, structure choice) can relax these."""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

from ..clock import next_trading_day, prev_trading_day
from ..config import Config
from ..models import AccountInfo, Position
from ..state.store import Store

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store

    # ------------------------------------------------------------------ kill switch
    def halt_reason(self) -> Optional[str]:
        return self.store.get("halt_reason")

    def halt(self, reason: str, now: datetime) -> None:
        log.error("HALT: %s", reason)
        self.store.set("halt_reason", reason)
        self.store.set("halt_at", now.isoformat())

    def reset_halt(self) -> None:
        self.store.delete("halt_reason")
        self.store.delete("halt_at")
        self.store.delete("paused_until")

    def equity_floor(self) -> float:
        return self.cfg.account.initial_equity_for_floor * self.cfg.risk.equity_floor_pct

    def check_floor(self, equity: float, now: datetime) -> Optional[str]:
        if equity <= self.equity_floor() and not self.halt_reason():
            self.halt(f"equity ${equity:,.0f} <= floor ${self.equity_floor():,.0f} (no new positions until `earnings-bot reset-kill`)", now)
        return self.halt_reason()

    # ------------------------------------------------------------------ gates
    @staticmethod
    def deployed(open_positions: list[Position]) -> float:
        return sum(p.remaining_cost() for p in open_positions)

    def can_open(self, account: AccountInfo, open_positions: list[Position], now: datetime) -> tuple[bool, str]:
        r = self.cfg.risk
        h = self.halt_reason() or self.check_floor(account.equity, now)
        if h:
            return False, f"halted: {h}"
        pu = self.store.get("paused_until")
        if pu and date.fromisoformat(pu) >= now.date():
            return False, f"paused through {pu} after a losing streak"
        start = self.store.start_day(now.date(), account.equity)
        if account.equity <= start * (1 - r.daily_loss_limit_pct):
            return False, f"daily loss limit: equity ${account.equity:,.0f} vs day start ${start:,.0f}"
        if len(open_positions) >= r.max_open_positions:
            return False, f"max open positions ({r.max_open_positions}) reached"
        dep = self.deployed(open_positions)
        if dep >= account.equity * r.max_deployed_pct:
            return False, f"deployment cap: ${dep:,.0f} at risk >= {r.max_deployed_pct:.0%} of equity"
        if account.cash - r.min_cash_reserve <= 0:
            return False, "no cash above the reserve"
        return True, "ok"

    def size_units(self, unit_cost_dollars: float, account: AccountInfo, open_positions: list[Position],
                   budget_factor: float = 1.0, already_planned: float = 0.0) -> tuple[int, dict]:
        """Units (1 unit = 1 contract per leg) affordable within every cap. unit_cost includes commissions."""
        r = self.cfg.risk
        budget = account.equity * r.per_trade_pct * budget_factor
        deployable = max(account.equity * r.max_deployed_pct - self.deployed(open_positions) - already_planned, 0.0)
        cash_avail = max(account.cash - r.min_cash_reserve - already_planned, 0.0)
        cap = min(budget, deployable, cash_avail)
        units = int(cap // unit_cost_dollars) if unit_cost_dollars > 0 else 0
        return max(units, 0), {"budget": round(budget, 2), "deployable": round(deployable, 2), "cash_avail": round(cash_avail, 2),
                               "cap": round(cap, 2), "unit_cost": round(unit_cost_dollars, 2)}

    def sector_ok(self, sector: str, open_sectors: list[str]) -> bool:
        if not sector or sector == "Unknown":
            return True
        return open_sectors.count(sector) < self.cfg.risk.max_per_sector

    def day_trade_allowed(self, account: AccountInfo, today: date) -> tuple[bool, str]:
        mode = self.cfg.risk.pdt_mode
        if mode == "off":
            return True, "day-trade limits disabled in config"
        if mode == "auto" and account.day_trades_remaining is not None:
            if account.day_trades_remaining < 0:
                return True, "broker reports unlimited day trades"
            return account.day_trades_remaining > 0, f"broker day trades remaining: {account.day_trades_remaining}"
        since = prev_trading_day(today, 4)
        n = self.store.day_trades_since(since)
        return n < 3, f"local day-trade count {n}/3 in the rolling 5 sessions"

    def after_close(self, day: date, equity: float, realized: float, trades: int) -> Optional[str]:
        self.store.end_day(day, equity, realized, trades)
        streak = self.store.consecutive_losing_days()
        if streak >= self.cfg.risk.max_consecutive_losing_days:
            until = day
            for _ in range(self.cfg.risk.pause_days_after_streak):
                until = next_trading_day(until)
            self.store.set("paused_until", until.isoformat())
            return f"paused: {streak} consecutive losing days -> no new positions through {until}"
        return None
