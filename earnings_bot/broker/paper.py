"""In-process paper broker. Fills limit orders against the market-data quotes with a conservative rule:
a buy fills only once the limit reaches ~65% of the way from bid to ask (or the ask), a sell symmetrically."""
from __future__ import annotations

import logging
from typing import Optional

from ..models import AccountInfo, BrokerPosition, Fill, OptionKey, OptionQuote, OrderState

log = logging.getLogger(__name__)


class PaperBroker:
    name = "paper"

    def __init__(self, md, starting_cash: float, commission: float, clock, fill_aggressiveness: float = 0.65):
        self.md = md
        self.cash = float(starting_cash)
        self.commission = commission
        self.clock = clock
        self.aggr = fill_aggressiveness
        self.pos: dict[OptionKey, list] = {}
        self.orders: dict[str, dict] = {}
        self.fills: list[Fill] = []
        self._n = 0

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def _expire(self) -> None:
        """Contracts past expiry leave the account (OTM expire worthless; the engine books the write-off)."""
        today = self.clock.now().date()
        for k in list(self.pos):
            if k.expiry < today:
                del self.pos[k]

    async def account(self) -> AccountInfo:
        self._expire()
        mv = 0.0
        live = [k for k, (q, _) in self.pos.items() if q != 0]
        if live:
            quotes = await self.option_quotes(live)
            for k in live:
                q = quotes.get(k)
                mv += self.pos[k][0] * (q.mid if q else 0.0) * 100
        return AccountInfo(self.cash + mv, self.cash, self.cash, day_trades_remaining=-1, is_margin=False)

    async def positions(self) -> list[BrokerPosition]:
        self._expire()
        return [BrokerPosition(k, q, a) for k, (q, a) in self.pos.items() if q != 0]

    async def option_quotes(self, keys: list[OptionKey]) -> dict[OptionKey, OptionQuote]:
        out: dict[OptionKey, OptionQuote] = {}
        groups: dict[tuple, list[OptionKey]] = {}
        for k in keys:
            groups.setdefault((k.symbol, k.expiry), []).append(k)
        for (sym, exp), ks in groups.items():
            ch = await self.md.chain(sym, exp)
            if not ch:
                continue
            idx = {q.key: q for q in ch.calls + ch.puts}
            for k in ks:
                if k in idx:
                    out[k] = idx[k]
        return out

    async def spot(self, symbol: str) -> float:
        return await self.md.spot(symbol)

    async def place_limit(self, key: OptionKey, side: str, qty: int, limit_price: float, tif: str = "DAY") -> str:
        self._n += 1
        oid = f"P{self._n}"
        self.orders[oid] = dict(key=key, side=side, qty=int(qty), limit=float(limit_price), filled=0, avg=0.0, status="open")
        return oid

    async def replace_limit(self, order_id: str, new_price: float) -> str:
        o = self.orders[order_id]
        if o["status"] == "open":
            o["limit"] = float(new_price)
        return order_id

    async def cancel(self, order_id: str) -> None:
        o = self.orders[order_id]
        if o["status"] == "open":
            o["status"] = "cancelled"

    async def order(self, order_id: str) -> OrderState:
        o = self.orders[order_id]
        if o["status"] == "open":
            await self._try_fill(order_id, o)
        return OrderState(order_id, o["status"], o["filled"], o["avg"])

    async def _try_fill(self, oid: str, o: dict) -> None:
        q = (await self.option_quotes([o["key"]])).get(o["key"])
        if not q or not q.has_market:
            return
        remaining = o["qty"] - o["filled"]
        spread = q.ask - q.bid
        px: Optional[float] = None
        if o["side"] == "BUY":
            if o["limit"] >= q.ask:
                px = q.ask
            elif o["limit"] >= q.bid + self.aggr * spread:
                px = o["limit"]
        else:
            if o["limit"] <= q.bid:
                px = q.bid
            elif o["limit"] <= q.ask - self.aggr * spread:
                px = o["limit"]
        if px is None:
            return
        self._apply_fill(oid, o, remaining, px)

    def _apply_fill(self, oid: str, o: dict, qty: int, px: float) -> None:
        key = o["key"]
        comm = self.commission * qty
        q0, a0 = self.pos.get(key, [0, 0.0])
        if o["side"] == "BUY":
            self.cash -= px * 100 * qty + comm
            newq = q0 + qty
            self.pos[key] = [newq, (a0 * q0 + px * qty) / newq if newq else 0.0]
        else:
            self.cash += px * 100 * qty - comm
            self.pos[key] = [q0 - qty, a0]
        o["avg"] = (o["avg"] * o["filled"] + px * qty) / (o["filled"] + qty)
        o["filled"] += qty
        if o["filled"] >= o["qty"]:
            o["status"] = "filled"
        self.fills.append(Fill(key, o["side"], qty, px, comm, oid, self.clock.now()))
        log.info("PAPER FILL %s %s x%d @ %.2f (limit %.2f)", o["side"], key, qty, px, o["limit"])
