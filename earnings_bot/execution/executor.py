"""Limit-order execution with a bounded chase: never pays above the ask on entry, walks toward the bid on exit."""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

from ..config import ExecutionCfg
from ..models import Fill, OptionKey, OptionQuote, OrderState, StructurePlan
from ..pricing import default_tick

log = logging.getLogger(__name__)


def _ceil_tick(p: float, tick: float) -> float:
    return round(math.ceil(p / tick - 1e-9) * tick, 4)


def _floor_tick(p: float, tick: float) -> float:
    return round(math.floor(p / tick + 1e-9) * tick, 4)


class Executor:
    def __init__(self, broker, store, cfg: ExecutionCfg, clock, commission: float,
                 sleep: Optional[Callable[[float], Awaitable[None]]] = None):
        self.broker = broker
        self.store = store
        self.cfg = cfg
        self.clock = clock
        self.commission = commission
        self.sleep = sleep or asyncio.sleep

    async def _quote(self, key: OptionKey) -> Optional[OptionQuote]:
        try:
            return (await self.broker.option_quotes([key])).get(key)
        except Exception as e:  # noqa: BLE001
            log.warning("quote %s failed: %s", key, e)
            return None

    async def _await_terminal(self, oid: str, last: OrderState, tries: int = 10) -> OrderState:
        """After a cancel, poll until the broker reports a terminal state so late fills are not lost."""
        st = last
        for _ in range(tries):
            try:
                st = await self.broker.order(oid)
            except Exception as e:  # noqa: BLE001
                log.warning("order status %s failed: %s", oid, e)
                break
            if st.status in ("filled", "cancelled", "rejected"):
                return st
            await self.sleep(0.5)
        return st

    async def _abort(self, oid: str, last: OrderState) -> OrderState:
        try:
            await self.broker.cancel(oid)
        except Exception as e:  # noqa: BLE001
            log.warning("cancel %s failed: %s", oid, e)
        return await self._await_terminal(oid, last)

    # ------------------------------------------------------------------ entries
    async def buy_leg(self, position_id: str, key: OptionKey, qty: int, deadline: datetime, max_price: float) -> Optional[Fill]:
        q = await self._quote(key)
        if not q or not q.has_market:
            log.warning("%s: no live market, not buying", key)
            return None
        tick = default_tick(q.mid)
        spread = max(q.ask - q.bid, tick)
        price = min(_ceil_tick(q.mid, tick), q.ask, max_price)
        if price <= 0:
            return None
        oid = await self.broker.place_limit(key, "BUY", qty, price)
        self.store.record_order(oid, position_id, key, "BUY", qty, price)
        st = OrderState(oid, "open", 0, 0.0)
        while True:
            try:
                await self.sleep(self.cfg.chase_interval_seconds)
                st = await self.broker.order(oid)
                if st.status in ("filled", "cancelled", "rejected"):
                    break
                if self.clock.now() >= deadline:
                    st = await self._abort(oid, st)
                    break
                q = await self._quote(key) or q
                spread = max(q.ask - q.bid, tick)
                cap = min(max_price, q.bid + self.cfg.max_pay_pct_of_spread * spread if q.ask > 0 else max_price)
                cap = min(cap, q.ask) if q.ask > 0 else cap
                new_price = min(cap, price + max(self.cfg.chase_step_pct_of_spread * spread, tick))
                new_price = _ceil_tick(new_price, tick)
                if new_price > price + 1e-9:
                    price = new_price
                    new_oid = await self.broker.replace_limit(oid, price)
                    if new_oid != oid:
                        self.store.update_order(oid, "cancelled", st.filled_qty, st.avg_fill_price)
                        oid = new_oid
                    self.store.record_order(oid, position_id, key, "BUY", qty, price)
            except Exception as e:  # noqa: BLE001
                log.error("buy chase %s failed (%s); cancelling and settling", key, e)
                st = await self._abort(oid, st)
                break
        self.store.update_order(oid, st.status, st.filled_qty, st.avg_fill_price)
        if st.filled_qty > 0:
            f = Fill(key, "BUY", st.filled_qty, st.avg_fill_price, self.commission * st.filled_qty, oid, self.clock.now())
            self.store.record_fill(f, position_id)
            log.info("BUY filled %s x%d @ %.2f (%s)", key, f.qty, f.price, st.status)
            return f
        log.info("BUY %s not filled (%s %s)", key, st.status, st.message)
        return None

    async def buy_structure(self, position_id: str, plan: StructurePlan, deadline: datetime, price_buffer: float = 1.08) -> dict[OptionKey, Fill]:
        tasks = [self.buy_leg(position_id, l.key, l.qty * plan.units, deadline, max_price=l.ref_ask * price_buffer) for l in plan.legs]
        res = await asyncio.gather(*tasks, return_exceptions=True)
        out: dict[OptionKey, Fill] = {}
        for l, r in zip(plan.legs, res):
            if isinstance(r, Exception):
                log.error("buy leg %s raised: %s", l.key, r)
            elif r:
                out[l.key] = r
        return out

    # ------------------------------------------------------------------ exits
    async def sell_leg(self, position_id: str, key: OptionKey, qty: int, deadline: datetime, urgency: str = "normal") -> Optional[Fill]:
        q = await self._quote(key)
        if not q or q.bid <= 0:
            log.warning("%s: no bid, cannot sell yet", key)
            return None
        tick = default_tick(q.mid)
        price = max(_floor_tick(q.mid, tick), q.bid)
        oid = await self.broker.place_limit(key, "SELL", qty, price)
        self.store.record_order(oid, position_id, key, "SELL", qty, price)
        steps = 0
        st = OrderState(oid, "open", 0, 0.0)
        while True:
            try:
                await self.sleep(self.cfg.exit_chase_interval_seconds)
                st = await self.broker.order(oid)
                if st.status in ("filled", "cancelled", "rejected"):
                    break
                if self.clock.now() >= deadline:
                    st = await self._abort(oid, st)
                    break
                q = await self._quote(key) or q
                steps += 1
                spread = max(q.ask - q.bid, tick)
                if steps <= 3:
                    target = max(q.bid, price - max(spread / 3.0, tick))
                else:
                    target = q.bid if urgency == "normal" or q.bid <= tick else max(q.bid - tick, tick)
                target = _floor_tick(target, tick)
                if target < price - 1e-9 and target > 0:
                    price = target
                    new_oid = await self.broker.replace_limit(oid, price)
                    if new_oid != oid:
                        self.store.update_order(oid, "cancelled", st.filled_qty, st.avg_fill_price)
                        oid = new_oid
                    self.store.record_order(oid, position_id, key, "SELL", qty, price)
            except Exception as e:  # noqa: BLE001
                log.error("sell chase %s failed (%s); cancelling and settling", key, e)
                st = await self._abort(oid, st)
                break
        self.store.update_order(oid, st.status, st.filled_qty, st.avg_fill_price)
        if st.filled_qty > 0:
            f = Fill(key, "SELL", st.filled_qty, st.avg_fill_price, self.commission * st.filled_qty, oid, self.clock.now())
            self.store.record_fill(f, position_id)
            log.info("SELL filled %s x%d @ %.2f (%s)", key, f.qty, f.price, st.status)
            return f
        log.info("SELL %s not filled (%s %s)", key, st.status, st.message)
        return None
