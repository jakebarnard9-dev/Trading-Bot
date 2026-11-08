"""Market data via yfinance (free, 15-minute delayed options quotes; fine for screening, never for execution)."""
from __future__ import annotations

import asyncio
import logging
import math
import time as _time
from datetime import date, datetime, timezone
from typing import Any, Optional

import pandas as pd

from ..models import OptionChain, OptionKey, OptionQuote

log = logging.getLogger(__name__)


def _f(x, default=0.0) -> float:
    try:
        v = float(x)
        return default if (v != v) else v  # NaN -> default
    except (TypeError, ValueError):
        return default


class YFMarketData:
    def __init__(self, max_concurrency: int = 4, ttl_seconds: int = 60):
        self._sem = asyncio.Semaphore(max_concurrency)
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._ttl = ttl_seconds

    async def _run(self, key: tuple, fn, ttl: Optional[int] = None):
        ttl = self._ttl if ttl is None else ttl
        hit = self._cache.get(key)
        if hit and _time.time() - hit[0] < ttl:
            return hit[1]
        async with self._sem:
            val = await asyncio.to_thread(fn)
        self._cache[key] = (_time.time(), val)
        return val

    def _ticker(self, symbol: str):
        import yfinance as yf

        return yf.Ticker(symbol)

    async def spot(self, symbol: str) -> float:
        def fn():
            t = self._ticker(symbol)
            fi = t.fast_info
            for k in ("last_price", "lastPrice", "regular_market_price", "regularMarketPrice"):
                try:
                    v = fi[k]
                    if v and not math.isnan(float(v)):
                        return float(v)
                except Exception:  # noqa: BLE001
                    continue
            h = t.history(period="5d", interval="1d", auto_adjust=False)
            return float(h["Close"].dropna().iloc[-1]) if len(h) else 0.0

        return await self._run(("spot", symbol), fn, ttl=30)

    async def expiries(self, symbol: str) -> list[date]:
        def fn():
            return [date.fromisoformat(s) for s in (self._ticker(symbol).options or ())]

        return await self._run(("expiries", symbol), fn, ttl=3600)

    async def chain(self, symbol: str, expiry: date) -> Optional[OptionChain]:
        def fn():
            t = self._ticker(symbol)
            oc = t.option_chain(expiry.isoformat())
            calls_df = getattr(oc, "calls", None)
            puts_df = getattr(oc, "puts", None)
            if calls_df is None and isinstance(oc, (tuple, list)):
                calls_df, puts_df = oc[0], oc[1]
            spot = 0.0
            und = getattr(oc, "underlying", None)
            if isinstance(und, dict):
                spot = _f(und.get("regularMarketPrice") or und.get("postMarketPrice") or 0.0)
            return calls_df, puts_df, spot

        try:
            calls_df, puts_df, spot = await self._run(("chain", symbol, expiry), fn, ttl=45)
        except Exception as e:  # noqa: BLE001
            log.warning("yf chain %s %s failed: %s", symbol, expiry, e)
            return None
        if spot <= 0:
            spot = await self.spot(symbol)
        now = datetime.now(timezone.utc)

        def rows(df, right):
            out = []
            if df is None or len(df) == 0:
                return out
            for _, r in df.iterrows():
                strike = _f(r.get("strike"))
                if strike <= 0:
                    continue
                out.append(OptionQuote(
                    key=OptionKey(symbol, expiry, strike, right),
                    bid=_f(r.get("bid")), ask=_f(r.get("ask")), last=_f(r.get("lastPrice")),
                    volume=int(_f(r.get("volume"))), open_interest=int(_f(r.get("openInterest"))),
                    iv=(_f(r.get("impliedVolatility")) or None), asof=now,
                ))
            return out

        return OptionChain(symbol, expiry, spot, rows(calls_df, "C"), rows(puts_df, "P"), asof=now)

    async def daily_bars(self, symbol: str, days: int = 400) -> pd.DataFrame:
        def fn():
            period = "2y" if days > 300 else "1y"
            h = self._ticker(symbol).history(period=period, interval="1d", auto_adjust=False)
            if h is None or len(h) == 0:
                return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
            h = h.copy()
            h.index = pd.to_datetime(h.index).tz_localize(None) if getattr(h.index, "tz", None) is None else pd.to_datetime(h.index).tz_convert("America/New_York").tz_localize(None)
            return h[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])

        return await self._run(("bars", symbol, days), fn, ttl=1800)

    async def past_earnings_dates(self, symbol: str, limit: int = 16) -> list[date]:
        def fn():
            t = self._ticker(symbol)
            out: list[date] = []
            try:
                df = t.get_earnings_dates(limit=max(limit + 4, 12))
                if df is not None and len(df):
                    for ts in df.index:
                        d = ts.date() if hasattr(ts, "date") else date.fromisoformat(str(ts)[:10])
                        out.append(d)
            except Exception as e:  # noqa: BLE001
                log.debug("earnings dates %s: %s", symbol, e)
            today = date.today()
            past = sorted({d for d in out if d < today}, reverse=True)
            return past[:limit]

        return await self._run(("edates", symbol), fn, ttl=6 * 3600)

    async def profile(self, symbol: str) -> dict:
        def fn():
            t = self._ticker(symbol)
            info = {}
            try:
                info = t.info or {}
            except Exception as e:  # noqa: BLE001
                log.debug("info %s: %s", symbol, e)
            return {
                "name": info.get("shortName") or info.get("longName") or symbol,
                "sector": info.get("sector") or "Unknown",
                "industry": info.get("industry") or "",
                "market_cap": info.get("marketCap"),
                "short_pct_float": info.get("shortPercentOfFloat"),
                "avg_volume": info.get("averageVolume"),
                "beta": info.get("beta"),
            }

        return await self._run(("profile", symbol), fn, ttl=24 * 3600)

    async def estimates(self, symbol: str) -> dict:
        def fn():
            t = self._ticker(symbol)
            out: dict = {}
            try:
                ee = t.earnings_estimate
                if ee is not None and len(ee):
                    row = ee.iloc[0]
                    out["eps_avg"] = _f(row.get("avg"), None)
                    out["eps_low"] = _f(row.get("low"), None)
                    out["eps_high"] = _f(row.get("high"), None)
                    out["analysts"] = int(_f(row.get("numberOfAnalysts")))
                    out["eps_year_ago"] = _f(row.get("yearAgoEps"), None)
            except Exception as e:  # noqa: BLE001
                log.debug("estimates %s: %s", symbol, e)
            try:
                rv = t.eps_revisions
                if rv is not None and len(rv):
                    row = rv.iloc[0]
                    out["rev_up_30d"] = int(_f(row.get("upLast30days")))
                    out["rev_down_30d"] = int(_f(row.get("downLast30days")))
            except Exception as e:  # noqa: BLE001
                log.debug("revisions %s: %s", symbol, e)
            return out

        return await self._run(("estimates", symbol), fn, ttl=6 * 3600)
