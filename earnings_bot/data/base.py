"""Protocols for market data and calendar sources (so yfinance, brokers and fixtures are interchangeable)."""
from __future__ import annotations

from datetime import date
from typing import Optional, Protocol

import pandas as pd

from ..models import EarningsEvent, Headline, OptionChain


class MarketData(Protocol):
    async def spot(self, symbol: str) -> float: ...
    async def expiries(self, symbol: str) -> list[date]: ...
    async def chain(self, symbol: str, expiry: date) -> Optional[OptionChain]: ...
    async def daily_bars(self, symbol: str, days: int = 400) -> pd.DataFrame: ...
    async def past_earnings_dates(self, symbol: str, limit: int = 16) -> list[date]: ...
    async def profile(self, symbol: str) -> dict: ...
    async def estimates(self, symbol: str) -> dict: ...


class CalendarSource(Protocol):
    name: str

    async def fetch(self, start: date, end: date) -> list[EarningsEvent]: ...


class NewsSource(Protocol):
    name: str

    async def fetch(self, symbol: str, company: str, days: int) -> list[Headline]: ...
