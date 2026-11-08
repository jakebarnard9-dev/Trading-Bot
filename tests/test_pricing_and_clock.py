import math
from datetime import date, datetime

import numpy as np

from earnings_bot.clock import ET, is_trading_day, reaction_and_entry_dates, session_times
from earnings_bot.data.fixtures import FixtureMarketData
from earnings_bot.pricing import (
    bs_price, build_move_scenarios, direction_to_p_up, implied_move, implied_vol, simulate_structure, years_until,
)


def test_put_call_parity_and_iv_roundtrip():
    S, K, T, r, sig = 100.0, 95.0, 10 / 365, 0.04, 0.9
    c, p = bs_price(S, K, T, sig, "C", r), bs_price(S, K, T, sig, "P", r)
    assert abs((c - p) - (S - K * math.exp(-r * T))) < 1e-6
    assert abs(implied_vol(c, S, K, T, "C", r) - sig) < 1e-4
    assert implied_vol(0.0, S, K, T, "C") is None


def test_trading_calendar_2026():
    assert is_trading_day(date(2026, 11, 11))          # Veterans Day: NYSE open
    assert not is_trading_day(date(2026, 11, 26))      # Thanksgiving
    o, c = session_times(date(2026, 11, 27))           # half day
    assert c.hour == 13
    assert reaction_and_entry_dates(date(2026, 11, 12), "amc") == (date(2026, 11, 12), date(2026, 11, 13))
    assert reaction_and_entry_dates(date(2026, 11, 10), "bmo") == (date(2026, 11, 9), date(2026, 11, 10))
    assert reaction_and_entry_dates(date(2026, 11, 27), "amc") == (date(2026, 11, 27), date(2026, 11, 30))


async def test_implied_move_matches_bs_expectation():
    now = datetime(2026, 11, 12, 15, 30, tzinfo=ET)
    md = FixtureMarketData(lambda: now, {"X": dict(spot=100.0, iv=0.8, step=2.5, expiries=[date(2026, 11, 20)], past_moves=[])})
    ch = await md.chain("X", date(2026, 11, 20))
    im = implied_move(ch)
    T = years_until(date(2026, 11, 20), now)
    expected = 100 * 0.8 * math.sqrt(T) * math.sqrt(2 / math.pi)  # E|X| for a normal ~ 0.8 sigma
    assert abs(im.pct - expected) / expected < 0.08
    assert im.atm_strike == 100.0


def test_scenarios_anchor_to_target_and_direction():
    sc = build_move_scenarios([8, -11, 6, -4, 13, -9, 7, 10], 6.0, 1.2, p_up=0.5, n=20000)
    assert abs(np.abs(sc).mean() - 7.2) < 0.3
    sc2 = build_move_scenarios([], 6.0, 1.0, p_up=direction_to_p_up("bullish", 0.8), n=20000)
    assert 0.63 < (sc2 > 0).mean() < 0.73
    assert abs(np.abs(sc2).mean() - 6.0) < 0.3


def test_simulation_edge_sign():
    S, K, iv = 100.0, 100.0, 1.0
    T_entry, T_exit = 1.5 / 365, 0.25 / 365
    c, p = bs_price(S, K, T_entry, iv, "C"), bs_price(S, K, T_entry, iv, "P")
    im = 100 * (c + p) / S
    fair = simulate_structure([("C", K, 1, c), ("P", K, 1, p)], S, build_move_scenarios([], im, 1.0, 0.5, n=30000), T_exit, 0.5)
    rich = simulate_structure([("C", K, 1, c), ("P", K, 1, p)], S, build_move_scenarios([], im, 1.5, 0.5, n=30000), T_exit, 0.5)
    assert -0.15 < fair.expected_return < 0.15
    assert rich.expected_return > 0.25 and rich.growth > fair.growth
