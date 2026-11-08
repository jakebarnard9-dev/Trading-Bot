"""Interactive Brokers via ib_async (TWS / IB Gateway socket API)."""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Optional

from ..config import IBKRCfg
from ..models import AccountInfo, BrokerPosition, OptionKey, OptionQuote, OrderState
from ..pricing import default_tick, round_to_tick
from .base import BrokerError

log = logging.getLogger(__name__)


def increment_from_rules(rules: list[tuple[float, float]], price: float) -> float:
    """Pick the increment whose lowEdge is the largest one not above the price."""
    inc = None
    for low, step in sorted(rules):
        if price >= low - 1e-12:
            inc = step
    return inc if inc and inc > 0 else default_tick(price)


def _num(x, default=0.0) -> float:
    try:
        v = float(x)
        return default if (v != v or v in (float("inf"), float("-inf"))) else v
    except (TypeError, ValueError):
        return default


class IBKRBroker:
    name = "ibkr"

    def __init__(self, cfg: IBKRCfg, commission: float = 0.65):
        self.cfg = cfg
        self.commission = commission
        self.ib = None
        self._contracts: dict[OptionKey, object] = {}
        self._details: dict[OptionKey, object] = {}
        self._trades: dict[str, object] = {}

    async def connect(self) -> None:
        from ib_async import IB

        if self.ib is None:
            self.ib = IB()
            self.ib.errorEvent += self._on_error
        if self.ib.isConnected():
            return
        last = None
        for attempt in range(3):
            try:
                await self.ib.connectAsync(self.cfg.host, self.cfg.port, clientId=self.cfg.client_id, timeout=30)
                break
            except Exception as e:  # noqa: BLE001
                last = e
                await asyncio.sleep(5)
        else:
            raise BrokerError(f"cannot connect to TWS/Gateway at {self.cfg.host}:{self.cfg.port} (is it running and logged in, API enabled, port right?): {last}")
        self.ib.reqMarketDataType(self.cfg.market_data_type)
        self._contracts.clear()
        self._details.clear()
        log.info("IBKR connected (%s:%s client %s), market data type %s", self.cfg.host, self.cfg.port, self.cfg.client_id, self.cfg.market_data_type)

    async def _ensure(self) -> None:
        if self.ib is None or not self.ib.isConnected():
            log.warning("IBKR not connected; reconnecting")
            await self.connect()

    def _on_error(self, reqId, errorCode, errorString, contract=None, *args):
        lvl = logging.WARNING if errorCode in (110, 201, 202, 10147, 10148, 200, 354, 10167, 10168) else logging.DEBUG
        if errorCode in (2104, 2106, 2158, 2107, 2108):  # farm connection status noise
            lvl = logging.DEBUG
        log.log(lvl, "IBKR error %s req %s: %s %s", errorCode, reqId, errorString, contract or "")

    async def close(self) -> None:
        if self.ib and self.ib.isConnected():
            self.ib.disconnect()

    # ------------------------------------------------------------------ account / positions
    async def account(self) -> AccountInfo:
        await self._ensure()
        rows = await self.ib.accountSummaryAsync(self.cfg.account or "")
        vals: dict[str, str] = {}
        for r in rows:
            if r.currency in ("USD", "", None, "BASE") or r.tag in ("DayTradesRemaining", "AccountType", "Cushion"):
                vals.setdefault(r.tag, r.value)
        equity = _num(vals.get("NetLiquidation"))
        total_cash = _num(vals.get("TotalCashValue"))
        avail = _num(vals.get("AvailableFunds"))
        # In a cash account AvailableFunds already excludes unsettled money; never size off cash the broker will not let us spend.
        cash = min(total_cash, avail) if avail > 0 else total_cash
        bp = _num(vals.get("BuyingPower")) or avail
        dtr_raw = vals.get("DayTradesRemaining")
        dtr = int(_num(dtr_raw, -1)) if dtr_raw is not None else None
        is_margin = None
        if _num(vals.get("InitMarginReq"), -1) == 0 and _num(vals.get("MaintMarginReq"), -1) == 0 and _num(vals.get("EquityWithLoanValue"), -1) >= 0:
            is_margin = False if equity < 2000 else None  # IBKR margin accounts need $2,000; below that it is a cash account
        return AccountInfo(equity, cash, bp, day_trades_remaining=dtr, is_margin=is_margin, raw=dict(vals))

    async def positions(self) -> list[BrokerPosition]:
        await self._ensure()
        out = []
        for p in self.ib.positions():
            c = p.contract
            if c.secType != "OPT" or not p.position:
                continue
            try:
                exp = datetime.strptime(c.lastTradeDateOrContractMonth[:8], "%Y%m%d").date()
            except ValueError:
                continue
            mult = _num(c.multiplier, 100.0) or 100.0
            out.append(BrokerPosition(OptionKey(c.symbol, exp, float(c.strike), c.right[0]), int(p.position), _num(p.avgCost) / mult))
        return out

    # ------------------------------------------------------------------ contracts
    async def _qualify(self, key: OptionKey):
        from ib_async import Option

        if key in self._contracts:
            return self._contracts[key]
        exp = key.expiry.strftime("%Y%m%d")
        attempts = [Option(key.symbol, exp, key.strike, key.right, "SMART", currency="USD", tradingClass=key.symbol),
                    Option(key.symbol, exp, key.strike, key.right, "SMART", currency="USD")]
        for c in attempts:
            try:
                res = await self.ib.qualifyContractsAsync(c)
                if res and getattr(res[0], "conId", 0):
                    self._contracts[key] = res[0]
                    return res[0]
            except Exception as e:  # noqa: BLE001
                log.debug("qualify %s failed: %s", key, e)
        raise BrokerError(f"could not qualify option contract {key}")

    async def _tick_for(self, key: OptionKey, price: float) -> float:
        """Price increment from the contract's market rule (exact), falling back to the penny/nickel heuristic."""
        try:
            if key not in self._details:
                c = await self._qualify(key)
                cds = await self.ib.reqContractDetailsAsync(c)
                self._details[key] = cds[0] if cds else None
            cd = self._details.get(key)
            rules = await self._market_rule(cd) if cd else None
            if rules:
                return increment_from_rules(rules, price)
            min_tick = _num(getattr(cd, "minTick", 0.01), 0.01) if cd else 0.01
        except Exception as e:  # noqa: BLE001
            log.debug("tick lookup %s: %s", key, e)
            min_tick = 0.01
        if price < 3.0:
            return max(min_tick, 0.01)
        return 0.05 if min_tick <= 0.011 else 0.10

    async def _market_rule(self, cd) -> Optional[list[tuple[float, float]]]:
        ids = [x.strip() for x in str(getattr(cd, "marketRuleIds", "") or "").split(",") if x.strip()]
        exchanges = [x.strip() for x in str(getattr(cd, "validExchanges", "") or "").split(",") if x.strip()]
        if not ids:
            return None
        idx = exchanges.index("SMART") if "SMART" in exchanges and len(exchanges) == len(ids) else 0
        rule_id = int(ids[min(idx, len(ids) - 1)])
        if not hasattr(self, "_rules"):
            self._rules: dict[int, list[tuple[float, float]]] = {}
        if rule_id not in self._rules:
            res = await self.ib.reqMarketRuleAsync(rule_id)
            if not res:
                return None
            self._rules[rule_id] = [(float(r.lowEdge), float(r.increment)) for r in res]
        return self._rules[rule_id]

    # ------------------------------------------------------------------ market data
    async def option_quotes(self, keys: list[OptionKey]) -> dict[OptionKey, OptionQuote]:
        await self._ensure()
        out: dict[OptionKey, OptionQuote] = {}
        if not keys:
            return out
        contracts = []
        for k in keys:
            try:
                contracts.append((k, await self._qualify(k)))
            except BrokerError as e:
                log.warning("%s", e)
        tickers = [(k, self.ib.reqMktData(c, "100,101,106", False, False)) for k, c in contracts]
        try:
            await asyncio.sleep(2.5)
            now = datetime.now(timezone.utc)
            for k, t in tickers:
                bid, ask, last = _num(t.bid), _num(t.ask), _num(t.last) or _num(t.close)
                bid = bid if bid > 0 else 0.0
                ask = ask if ask > 0 else 0.0
                oi = int(_num(t.callOpenInterest if k.right == "C" else t.putOpenInterest))
                vol = int(_num(t.volume))
                g = t.modelGreeks or t.lastGreeks or t.bidGreeks
                iv = _num(getattr(g, "impliedVol", None), 0.0) or None
                delta = _num(getattr(g, "delta", None), 0.0) if g else None
                out[k] = OptionQuote(k, bid, ask, last, vol, oi, iv, delta, now)
        finally:
            for _, c in contracts:
                try:
                    self.ib.cancelMktData(c)
                except Exception:  # noqa: BLE001
                    pass
        return out

    async def spot(self, symbol: str) -> float:
        await self._ensure()
        from ib_async import Stock

        c = Stock(symbol, "SMART", "USD")
        await self.ib.qualifyContractsAsync(c)
        tickers = await self.ib.reqTickersAsync(c)
        if not tickers:
            return 0.0
        t = tickers[0]
        px = t.marketPrice()
        if not px or math.isnan(px):
            px = _num(t.last) or _num(t.close)
        return _num(px)

    # ------------------------------------------------------------------ orders
    async def place_limit(self, key: OptionKey, side: str, qty: int, limit_price: float, tif: str = "DAY") -> str:
        await self._ensure()
        from ib_async import LimitOrder

        c = await self._qualify(key)
        tick = await self._tick_for(key, limit_price)
        px = round_to_tick(limit_price, tick)
        order = LimitOrder(side, int(qty), px, tif=tif, outsideRth=False)
        if self.cfg.account:
            order.account = self.cfg.account
        trade = self.ib.placeOrder(c, order)
        oid = str(order.orderId)
        self._trades[oid] = trade
        await asyncio.sleep(0.3)
        log.info("IBKR order %s: %s %s x%d @ %.2f", oid, side, key, qty, px)
        return oid

    async def replace_limit(self, order_id: str, new_price: float) -> str:
        await self._ensure()
        trade = self._trades[order_id]
        key = next((k for k, c in self._contracts.items() if c.conId == trade.contract.conId), None)
        tick = await self._tick_for(key, new_price) if key else default_tick(new_price)
        trade.order.lmtPrice = round_to_tick(new_price, tick)
        self.ib.placeOrder(trade.contract, trade.order)
        await asyncio.sleep(0.2)
        return order_id

    async def cancel(self, order_id: str) -> None:
        await self._ensure()
        trade = self._trades.get(order_id)
        if trade and not trade.isDone():
            self.ib.cancelOrder(trade.order)
            await asyncio.sleep(0.3)

    async def order(self, order_id: str) -> OrderState:
        await self._ensure()
        trade = self._trades.get(order_id)
        if trade is None:
            return OrderState(order_id, "rejected", 0, 0.0, "unknown order id")
        st = trade.orderStatus
        s = st.status
        if s == "Filled":
            status = "filled"
        elif s in ("Cancelled", "ApiCancelled"):
            status = "cancelled"
        elif s == "Inactive":
            status = "rejected"
        else:
            status = "open"
        msg = trade.log[-1].message if trade.log else ""
        return OrderState(order_id, status, int(_num(st.filled)), _num(st.avgFillPrice), msg)
