"""Regression tests for the defects found in the independent review."""
from datetime import date, datetime, timedelta

from earnings_bot.broker.ibkr import increment_from_rules
from earnings_bot.broker.paper import PaperBroker
from earnings_bot.clock import ET, FakeClock
from earnings_bot.config import load_config
from earnings_bot.data.fixtures import FixtureMarketData
from earnings_bot.execution.executor import Executor
from earnings_bot.models import OptionKey, OptionQuote, OrderState, Position, PositionLeg
from earnings_bot.replay import run_replay
from earnings_bot.state.store import Store
from earnings_bot.strategy.exits import apply_fill_to_position, book_expired_leg, evaluate_exits

cfg = load_config("tests/fixture_config.yaml")


def q(key, bid, ask):
    return OptionQuote(key, bid, ask, (bid + ask) / 2, 500, 5000, 0.9)


def _straddle(cost_c=3.0, cost_p=2.0, qty=2):
    kc, kp = OptionKey("X", date(2026, 11, 13), 100.0, "C"), OptionKey("X", date(2026, 11, 13), 100.0, "P")
    cost = (cost_c + cost_p) * 100 * qty
    return Position("X-1", "X", "earnings", "straddle", [PositionLeg(kc, qty, cost_c, qty), PositionLeg(kp, qty, cost_p, qty)],
                    datetime(2026, 11, 12, 15, 40, tzinfo=ET), date(2026, 11, 13), date(2026, 11, 13), cost), kc, kp


def test_take_half_does_not_trigger_an_immediate_trail_stop():
    pos, kc, kp = _straddle()
    t = datetime(2026, 11, 13, 9, 35, tzinfo=ET)
    quotes = {kc: q(kc, 11.0, 11.2), kp: q(kp, 0.02, 0.04)}        # structure at +100%
    acts = evaluate_exits(pos, quotes, t, cfg.exits)
    assert [(a.leg_key, a.qty) for a in acts] == [(kc, 1)] and not pos.took_half
    apply_fill_to_position(pos, kc, 1, 11.0, 0.65)                 # engine applies the fill, then marks took_half
    pos.took_half = True
    assert evaluate_exits(pos, quotes, t + timedelta(seconds=10), cfg.exits) == []   # no spurious trail stop
    assert evaluate_exits(pos, quotes, t + timedelta(seconds=20), cfg.exits) == []
    acts = evaluate_exits(pos, {kc: q(kc, 8.0, 8.2), kp: q(kp, 0.02, 0.04)}, t + timedelta(seconds=30), cfg.exits)
    assert acts and acts[0].reason.startswith("trail_stop") and acts[0].qty == 1   # remainder trails from its own high water


def test_missing_quote_blocks_mark_based_rules_but_not_time_stops():
    pos, kc, kp = _straddle()
    t = datetime(2026, 11, 13, 9, 50, tzinfo=ET)
    assert evaluate_exits(pos, {kc: q(kc, 0.5, 0.6)}, t, cfg.exits) == []                 # put quote missing -> no hard stop
    assert evaluate_exits(pos, {kc: q(kc, 0.5, 0.6), kp: q(kp, 0.0, 0.0)}, t, cfg.exits) == []
    acts = evaluate_exits(pos, {kc: q(kc, 0.5, 0.6), kp: q(kp, 0.4, 0.5)}, t, cfg.exits)
    assert acts and acts[0].reason.startswith("hard_stop")
    acts = evaluate_exits(pos, {kc: q(kc, 0.5, 0.6)}, datetime(2026, 11, 13, 10, 31, tzinfo=ET), cfg.exits)
    assert acts and acts[0].reason == "time_stop"                                            # time stops still fire


def test_expired_leg_books_its_loss():
    pos, kc, kp = _straddle()
    apply_fill_to_position(pos, kc, 2, 9.0, 1.30)
    loss = book_expired_leg(pos, pos.legs[1])
    assert loss == -400.0 and pos.legs[1].remaining == 0
    assert abs(pos.realized_pnl - ((9.0 - 3.0) * 200 - 1.30 - 400.0)) < 1e-9


def test_increment_from_rules():
    rules = [(0.0, 0.01), (3.0, 0.05)]
    assert increment_from_rules(rules, 2.5) == 0.01 and increment_from_rules(rules, 3.0) == 0.05 and increment_from_rules(rules, 7.0) == 0.05
    assert increment_from_rules([(0.0, 0.05), (3.0, 0.10)], 1.0) == 0.05


class FlakyBroker(PaperBroker):
    """Raises on the first status poll of each order; cancel is asynchronous and completes after one extra poll."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._polls = {}
        self._pending_cancel = {}

    async def order(self, oid):
        n = self._polls.get(oid, 0) + 1
        self._polls[oid] = n
        if n == 1:
            raise RuntimeError("transient broker error")
        if oid in self._pending_cancel:
            self._pending_cancel[oid] -= 1
            if self._pending_cancel[oid] <= 0:
                o = self.orders[oid]
                if o["status"] == "open":
                    o["status"] = "cancelled"
                del self._pending_cancel[oid]
            else:
                return OrderState(oid, "open", self.orders[oid]["filled"], self.orders[oid]["avg"], "pending_cancel")
        return await super().order(oid)

    async def cancel(self, oid):
        self._pending_cancel[oid] = 2


async def test_executor_survives_errors_and_settles_cancels(tmp_path):
    clock = FakeClock(datetime(2026, 11, 12, 15, 30, tzinfo=ET))
    md = FixtureMarketData(clock.now, {"X": dict(spot=100.0, iv=1.0, step=2.5, expiries=[date(2026, 11, 13)], past_moves=[])})
    broker = FlakyBroker(md, 5000, 0.65, clock)

    async def fake_sleep(s):
        clock.advance(seconds=s)

    ex = Executor(broker, Store(tmp_path / "s.sqlite"), cfg.execution, clock, 0.65, sleep=fake_sleep)
    k = OptionKey("X", date(2026, 11, 13), 100.0, "C")
    q0 = (await broker.option_quotes([k]))[k]
    fill = await ex.buy_leg("pos", k, 1, clock.now() + timedelta(minutes=5), max_price=q0.ask)
    # the transient error aborts the chase; the abort waits for the asynchronous cancel to become terminal
    assert fill is None
    assert all(o["status"] == "cancelled" for o in broker.orders.values())
    assert broker.pos.get(k, [0])[0] == 0 and broker.cash == 5000


async def test_replay_accounting_reconciles_with_equity(tmp_path):
    r = await run_replay(gap_pct=9.0, post_iv=0.5, db_path=str(tmp_path / "r.sqlite"))
    pos = r["positions"][0]
    assert pos.status == "closed"
    assert abs((r["equity"] - 5000) - pos.realized_pnl) < 0.5          # written-off put + sold call == equity change
    rows = {d["day"]: d for d in r["store"].daily_rows()}
    assert rows["2026-11-13"]["end_equity"] - rows["2026-11-13"]["start_equity"] > 300   # morning gain counted in the reaction day
    import re
    eod = next(m for m in r["messages"] if "EOD 2026-11-13" in m)
    assert int(re.search(r"\(([+-][\d,]+) vs day start\)", eod).group(1).replace(",", "")) > 300   # the reaction-day gain shows up the same day
