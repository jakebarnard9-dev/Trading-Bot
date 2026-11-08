from datetime import date, datetime, timedelta

import pytest

from earnings_bot.broker.paper import PaperBroker
from earnings_bot.clock import ET, FakeClock
from earnings_bot.config import load_config
from earnings_bot.data.fixtures import FixtureMarketData
from earnings_bot.execution.executor import Executor
from earnings_bot.models import Leg, OptionKey, StructurePlan
from earnings_bot.state.store import Store
from earnings_bot.replay import run_replay


@pytest.fixture
def world(tmp_path):
    cfg = load_config("tests/fixture_config.yaml")
    clock = FakeClock(datetime(2026, 11, 12, 15, 30, tzinfo=ET))
    md = FixtureMarketData(clock.now, {"X": dict(spot=100.0, iv=1.0, step=2.5, expiries=[date(2026, 11, 13)], past_moves=[])})
    broker = PaperBroker(md, 5000, 0.65, clock)
    store = Store(tmp_path / "s.sqlite")

    async def fake_sleep(s):
        clock.advance(seconds=s)

    return cfg, clock, md, broker, store, Executor(broker, store, cfg.execution, clock, 0.65, sleep=fake_sleep)


async def test_buy_chase_fills_inside_spread_then_sell_walks_down(world):
    cfg, clock, md, broker, store, ex = world
    k = OptionKey("X", date(2026, 11, 13), 100.0, "C")
    q0 = (await broker.option_quotes([k]))[k]
    fill = await ex.buy_leg("pos", k, 2, clock.now() + timedelta(minutes=10), max_price=q0.ask * 1.08)
    assert fill and fill.qty == 2
    assert q0.bid + 0.6 * (q0.ask - q0.bid) <= fill.price <= q0.ask      # paid inside the spread, never above the ask
    assert broker.pos[k][0] == 2 and broker.cash < 5000
    sell = await ex.sell_leg("pos", k, 2, clock.now() + timedelta(minutes=5))
    assert sell and sell.qty == 2 and q0.bid <= sell.price <= q0.ask
    assert broker.pos[k][0] == 0
    orders = store._rows("SELECT status FROM orders")
    assert {r["status"] for r in orders} <= {"filled", "cancelled"}


async def test_buy_deadline_cancels(world):
    cfg, clock, md, broker, store, ex = world
    k = OptionKey("X", date(2026, 11, 13), 100.0, "P")
    q0 = (await broker.option_quotes([k]))[k]
    # cap the price far below the fillable zone -> must cancel at the deadline with no fill
    fill = await ex.buy_leg("pos", k, 1, clock.now() + timedelta(seconds=45), max_price=q0.bid)
    assert fill is None and all(o["status"] == "cancelled" for o in broker.orders.values())


async def test_structure_with_units(world):
    cfg, clock, md, broker, store, ex = world
    kc, kp = OptionKey("X", date(2026, 11, 13), 100.0, "C"), OptionKey("X", date(2026, 11, 13), 100.0, "P")
    qs = await broker.option_quotes([kc, kp])
    plan = StructurePlan("X", "straddle", [Leg(kc, 1, qs[kc].bid, qs[kc].ask, qs[kc].mid), Leg(kp, 1, qs[kp].bid, qs[kp].ask, qs[kp].mid)], qs[kc].ask + qs[kp].ask, units=3)
    fills = await ex.buy_structure("pos", plan, clock.now() + timedelta(minutes=5))
    assert set(fills) == {kc, kp} and all(f.qty == 3 for f in fills.values())


async def test_replay_winner(tmp_path):
    r = await run_replay(gap_pct=9.0, post_iv=0.5, db_path=str(tmp_path / "r.sqlite"))
    by = {c["symbol"]: c for c in r["candidates"]}
    assert "GOOD" in by and by["GOOD"]["payload"]["skip_reason"] is None
    assert "SMALL" not in by                                              # filtered by the universe market-cap floor before any data fetch
    assert by["DULL"]["payload"]["skip_reason"] is not None               # dull history -> no edge
    assert len(r["opened"]) == 1 and r["opened"][0].symbol == "GOOD" and r["opened"][0].kind == "straddle"
    pos = r["positions"][0]
    assert pos.status == "closed" and pos.realized_pnl > 0
    assert pos.cost_basis <= 5000 * 0.12 * 1.10 + 5                   # sized inside the per-trade budget
    assert r["equity"] > 5000 and r["halted"] is None
    assert any("OPENED GOOD" in m for m in r["messages"]) and any("CLOSED GOOD" in m for m in r["messages"])


async def test_replay_dud_bounds_loss(tmp_path):
    r = await run_replay(gap_pct=0.8, post_iv=0.45, db_path=str(tmp_path / "r.sqlite"))
    pos = r["positions"][0]
    assert pos.status == "closed" and pos.realized_pnl < 0
    assert -pos.realized_pnl <= pos.cost_basis                           # can never lose more than premium paid
    assert pos.cost_basis <= 5000 * 0.12 * 1.10 + 5
    assert r["equity"] >= 5000 - pos.cost_basis - 10


async def test_replay_macro_leg(tmp_path):
    r = await run_replay(gap_pct=9.0, post_iv=0.5, db_path=str(tmp_path / "r.sqlite"), macro=True)
    syms = {p.symbol for p in r["positions"]}
    assert "GOOD" in syms
    spy = [c for c in r["candidates"] if c["symbol"] == "SPY"]
    assert spy and spy[0]["play_type"] == "macro"
