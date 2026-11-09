"""The shipped config.yaml (aggressive mandate) exercised end to end on synthetic data."""
from earnings_bot.config import load_config
from earnings_bot.replay import run_replay


def test_shipped_config_values():
    c = load_config("config.yaml")
    assert c.mode == "live" and c.broker == "ibkr" and c.ibkr.port == 7496
    assert c.risk.profile == "unlimited" and c.risk.per_trade_pct == 0.5 and c.risk.max_deployed_pct == 1.0 and c.risk.daily_loss_limit_pct == 1.0
    assert c.risk.equity_floor_pct == 0.20 and c.risk.min_cash_reserve == 0
    assert c.structures.objective == "aggressive" and c.universe.min_implied_move_pct == 6.0 and c.options.max_days_to_expiry_after_reaction == 4
    assert c.agents.enabled and c.agents.research and c.agents.risk_officer and c.agents.ops
    assert c.exits.take_half_at_gain_pct == 1.5 and c.exits.trail_pct_from_high == 0.30


async def test_shipped_strategy_filters_and_sizes(tmp_path):
    cfg = load_config("config.yaml")
    # implied move ~4.6% (iv 1.10) is below the 6% mandate -> GOOD must be skipped
    r = await run_replay(cfg, gap_pct=9.0, db_path=str(tmp_path / "a.sqlite"), keep_strategy=True, good_iv=1.10)
    by = {c["symbol"]: c for c in r["candidates"]}
    assert "below the 6% minimum" in by["GOOD"]["payload"]["skip_reason"] and not r["opened"]
    # implied ~7% (iv 1.7) passes; unlimited profile sizes up to 50% of equity; aggressive objective picks positive-edge structure
    big = [14, -18, 12, -9, 20, -15, 11, 16]       # a name that historically moves ~2x its implied move
    r = await run_replay(cfg, gap_pct=12.0, db_path=str(tmp_path / "b.sqlite"), keep_strategy=True, good_iv=1.70, good_moves=big)
    assert r["opened"] and r["opened"][0].symbol == "GOOD"
    pos = r["positions"][0]
    assert 1500 < pos.cost_basis <= 2500 * 1.01, pos.cost_basis            # half the $5k account on the best idea
    assert pos.status == "closed" and pos.realized_pnl > 0 and r["equity"] > 6000
    assert abs((r["equity"] - 5000) - pos.realized_pnl) < 1.0
    # dud: the loss is bounded by the premium and nothing else is touched
    r = await run_replay(cfg, gap_pct=0.5, db_path=str(tmp_path / "c.sqlite"), keep_strategy=True, good_iv=1.70, good_moves=big)
    pos = r["positions"][0]
    assert pos.realized_pnl < 0 and -pos.realized_pnl <= pos.cost_basis and r["equity"] >= 5000 - pos.cost_basis - 1 and r["halted"] is None
