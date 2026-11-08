"""Alpaca via alpaca-py (REST). Options level 2 is enough for the long-premium structures used here."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Optional

from ..config import AlpacaCfg
from ..models import AccountInfo, BrokerPosition, OptionKey, OptionQuote, OrderState
from .base import BrokerError

log = logging.getLogger(__name__)


def _f(x, default=0.0) -> float:
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


class AlpacaBroker:
    name = "alpaca"

    def __init__(self, cfg: AlpacaCfg, api_key: str, secret_key: str, commission: float = 0.0):
        if not api_key or not secret_key:
            raise BrokerError("ALPACA_API_KEY / ALPACA_SECRET_KEY are required for broker=alpaca")
        self.cfg = cfg
        self.key, self.secret = api_key, secret_key
        self.commission = commission
        self.tc = self.odc = self.sdc = None
        self._oi_cache: dict[tuple, dict[str, int]] = {}

    async def connect(self) -> None:
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        self.tc = TradingClient(self.key, self.secret, paper=self.cfg.paper)
        self.odc = OptionHistoricalDataClient(self.key, self.secret)
        self.sdc = StockHistoricalDataClient(self.key, self.secret)
        a = await asyncio.to_thread(self.tc.get_account)
        log.info("Alpaca connected (paper=%s) status=%s options_level=%s equity=%s", self.cfg.paper, a.status, a.options_trading_level, a.equity)

    async def close(self) -> None:
        return None

    async def account(self) -> AccountInfo:
        a = await asyncio.to_thread(self.tc.get_account)
        eq = _f(a.equity)
        dtr: Optional[int]
        if a.pattern_day_trader is None:
            dtr = None
        elif eq >= 25000:
            dtr = -1
        else:
            dtr = max(3 - int(_f(a.daytrade_count)), 0)
        return AccountInfo(eq, _f(a.cash), _f(a.options_buying_power) or _f(a.buying_power), dtr, True,
                           raw={"status": str(a.status), "options_trading_level": a.options_trading_level, "daytrade_count": a.daytrade_count})

    async def positions(self) -> list[BrokerPosition]:
        out = []
        for p in await asyncio.to_thread(self.tc.get_all_positions):
            if str(getattr(p.asset_class, "value", p.asset_class)) != "us_option":
                continue
            try:
                key = OptionKey.from_occ(p.symbol)
            except Exception:  # noqa: BLE001
                continue
            out.append(BrokerPosition(key, int(_f(p.qty)), _f(p.avg_entry_price)))
        return out

    async def _open_interest(self, symbol: str, expiry: date) -> dict[str, int]:
        from alpaca.trading.requests import GetOptionContractsRequest

        ck = (symbol, expiry, date.today())
        if ck in self._oi_cache:
            return self._oi_cache[ck]
        oi: dict[str, int] = {}
        try:
            req = GetOptionContractsRequest(underlying_symbols=[symbol], expiration_date=expiry, limit=1000)
            res = await asyncio.to_thread(self.tc.get_option_contracts, req)
            for c in res.option_contracts or []:
                oi[c.symbol] = int(_f(c.open_interest))
        except Exception as e:  # noqa: BLE001
            log.debug("alpaca contracts %s %s: %s", symbol, expiry, e)
        self._oi_cache[ck] = oi
        return oi

    async def option_quotes(self, keys: list[OptionKey]) -> dict[OptionKey, OptionQuote]:
        from alpaca.data.enums import OptionsFeed
        from alpaca.data.requests import OptionBarsRequest, OptionSnapshotRequest
        from alpaca.data.timeframe import TimeFrame

        out: dict[OptionKey, OptionQuote] = {}
        if not keys:
            return out
        occs = [k.occ() for k in keys]
        feed = OptionsFeed.OPRA if self.cfg.options_feed == "opra" else OptionsFeed.INDICATIVE
        snaps = await asyncio.to_thread(self.odc.get_option_snapshot, OptionSnapshotRequest(symbol_or_symbols=occs, feed=feed))
        vols: dict[str, int] = {}
        try:
            bars = await asyncio.to_thread(self.odc.get_option_bars, OptionBarsRequest(symbol_or_symbols=occs, timeframe=TimeFrame.Day,
                                                                                     start=datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)))
            data = getattr(bars, "data", bars) or {}
            for sym, blist in data.items():
                if blist:
                    vols[sym] = int(_f(blist[-1].volume))
        except Exception as e:  # noqa: BLE001
            log.debug("alpaca option bars: %s", e)
        now = datetime.now(timezone.utc)
        for k in keys:
            s = snaps.get(k.occ())
            if s is None:
                continue
            q = s.latest_quote
            oi = (await self._open_interest(k.symbol, k.expiry)).get(k.occ(), 0)
            out[k] = OptionQuote(k, _f(q.bid_price) if q else 0.0, _f(q.ask_price) if q else 0.0,
                                 _f(s.latest_trade.price) if s.latest_trade else 0.0, vols.get(k.occ(), 0), oi,
                                 _f(s.implied_volatility) or None, _f(s.greeks.delta) if s.greeks else None, now)
        return out

    async def spot(self, symbol: str) -> float:
        from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest

        try:
            q = (await asyncio.to_thread(self.sdc.get_stock_latest_quote, StockLatestQuoteRequest(symbol_or_symbols=symbol)))[symbol]
            if _f(q.bid_price) > 0 and _f(q.ask_price) > 0:
                return (_f(q.bid_price) + _f(q.ask_price)) / 2
        except Exception as e:  # noqa: BLE001
            log.debug("alpaca quote %s: %s", symbol, e)
        t = (await asyncio.to_thread(self.sdc.get_stock_latest_trade, StockLatestTradeRequest(symbol_or_symbols=symbol)))[symbol]
        return _f(t.price)

    async def place_limit(self, key: OptionKey, side: str, qty: int, limit_price: float, tif: str = "DAY") -> str:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        req = LimitOrderRequest(symbol=key.occ(), qty=int(qty), side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                                time_in_force=TimeInForce.DAY, limit_price=round(limit_price, 2))
        o = await asyncio.to_thread(self.tc.submit_order, req)
        log.info("Alpaca order %s: %s %s x%d @ %.2f", o.id, side, key, qty, limit_price)
        return str(o.id)

    async def replace_limit(self, order_id: str, new_price: float) -> str:
        from alpaca.trading.requests import ReplaceOrderRequest

        o = await asyncio.to_thread(self.tc.replace_order_by_id, order_id, ReplaceOrderRequest(limit_price=round(new_price, 2)))
        return str(o.id)

    async def cancel(self, order_id: str) -> None:
        try:
            await asyncio.to_thread(self.tc.cancel_order_by_id, order_id)
        except Exception as e:  # noqa: BLE001
            log.debug("alpaca cancel %s: %s", order_id, e)

    async def order(self, order_id: str) -> OrderState:
        o = await asyncio.to_thread(self.tc.get_order_by_id, order_id)
        s = str(getattr(o.status, "value", o.status))
        if s == "filled":
            status = "filled"
        elif s in ("canceled", "cancelled", "expired", "done_for_day", "replaced"):
            status = "cancelled"
        elif s in ("rejected", "suspended", "stopped"):
            status = "rejected"
        else:
            status = "open"
        return OrderState(order_id, status, int(_f(o.filled_qty)), _f(o.filled_avg_price), s)
