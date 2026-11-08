"""Deterministic synthetic data for tests and the replay demo: Black-Scholes chains, bars, calendars, news."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

from ..clock import ET
from ..models import EarningsEvent, Headline, OptionChain, OptionKey, OptionQuote
from ..pricing import bs_delta, bs_price, round_to_tick, years_until


class FixtureMarketData:
    """Synthetic market: per-symbol spot/IV you can change between ticks (e.g. to simulate the earnings gap)."""

    def __init__(self, now_fn, symbols: dict[str, dict]):
        """symbols: {sym: {spot, iv, step, expiries:[date], past_moves:[pct...], sector}}"""
        self.now_fn = now_fn
        self.s = {k: dict(v) for k, v in symbols.items()}

    def set(self, symbol: str, **kw) -> None:
        self.s[symbol].update(kw)

    async def spot(self, symbol: str) -> float:
        return float(self.s[symbol]["spot"])

    async def expiries(self, symbol: str) -> list[date]:
        return list(self.s[symbol]["expiries"])

    async def chain(self, symbol: str, expiry: date) -> Optional[OptionChain]:
        cfg = self.s[symbol]
        S, iv, step = float(cfg["spot"]), float(cfg["iv"]), float(cfg.get("step", 1.0))
        now = self.now_fn()
        T = max(years_until(expiry, now), 1e-6)
        center = round(S / step) * step
        strikes = [round(center + i * step, 2) for i in range(-12, 13) if center + i * step > 0]
        calls, puts = [], []
        for K in strikes:
            for right, lst in (("C", calls), ("P", puts)):
                mid = bs_price(S, K, T, iv, right)
                spread = max(0.02, 0.04 * mid)
                bid = max(round_to_tick(mid - spread / 2, 0.01), 0.01 if mid > 0.01 else 0.0)
                ask = round_to_tick(mid + spread / 2, 0.01)
                lst.append(OptionQuote(OptionKey(symbol, expiry, K, right), bid=bid, ask=max(ask, bid + 0.01), last=round(mid, 2),
                                       volume=800, open_interest=5000, iv=iv, delta=bs_delta(S, K, T, iv, right),
                                       asof=datetime.now(timezone.utc)))
        return OptionChain(symbol, expiry, S, calls, puts, asof=datetime.now(timezone.utc))

    async def daily_bars(self, symbol: str, days: int = 400) -> pd.DataFrame:
        cfg = self.s[symbol]
        today = self.now_fn().date()
        rng = np.random.default_rng(abs(hash(symbol)) % (2**32))
        idx = pd.bdate_range(end=today - timedelta(days=1), periods=days)
        moves = {d: m for d, m in zip(self._reaction_dates(today), cfg.get("past_moves", []))}
        opens, closes = [], []
        c = 100.0
        for ts in idx:
            d = ts.date()
            if d in moves:
                o = c * (1 + moves[d] / 100.0)
                c = o * (1 + rng.normal(0, 0.004))
            else:
                o = c * (1 + rng.normal(0, 0.003))
                c = o * (1 + rng.normal(0, 0.015))
            opens.append(o)
            closes.append(c)
        opens, closes = np.array(opens), np.array(closes)
        scale = float(cfg["spot"]) / float(closes[-1])
        opens, closes = opens * scale, closes * scale
        return pd.DataFrame({"Open": opens, "High": np.maximum(opens, closes) * 1.005, "Low": np.minimum(opens, closes) * 0.995,
                             "Close": closes, "Volume": 2_000_000}, index=idx)

    def _reaction_dates(self, today: date) -> list[date]:
        """Reaction sessions (the session after each synthetic AMC report), newest first, ~quarterly."""
        out = []
        for i in range(8):
            dd = today - timedelta(days=91 * (i + 1))
            while dd.weekday() >= 5:
                dd -= timedelta(days=1)
            out.append(dd)
        return out

    async def past_earnings_dates(self, symbol: str, limit: int = 16) -> list[date]:
        today = self.now_fn().date()
        out = []
        for d in self._reaction_dates(today)[: len(self.s[symbol].get("past_moves", []))]:
            prev = d - timedelta(days=1)
            while prev.weekday() >= 5:
                prev -= timedelta(days=1)
            out.append(prev)
        return out[:limit]

    async def profile(self, symbol: str) -> dict:
        cfg = self.s[symbol]
        return {"name": cfg.get("name", symbol + " Inc"), "sector": cfg.get("sector", "Technology"), "market_cap": cfg.get("market_cap", 2e10), "short_pct_float": 0.05}

    async def estimates(self, symbol: str) -> dict:
        return {"eps_avg": 1.0, "analysts": 20, "rev_up_30d": 3, "rev_down_30d": 1}


class FixtureCalendar:
    name = "fixture"

    def __init__(self, events: list[EarningsEvent]):
        self.events = events

    async def fetch(self, start: date, end: date) -> list[EarningsEvent]:
        return [e for e in self.events if start <= e.report_date <= end]


class FixtureNews:
    name = "fixture"

    async def fetch(self, symbol: str, company: str, days: int) -> list[Headline]:
        now = datetime.now(timezone.utc)
        return [Headline(f"{symbol} to report quarterly results; analysts expect strong cloud demand", "fixture-wire", now - timedelta(days=1)),
                Headline(f"Options traders brace for outsized {symbol} move", "fixture-wire", now - timedelta(days=2))]
