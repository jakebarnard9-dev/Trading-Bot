import json
from datetime import date, datetime, timedelta

import httpx

from earnings_bot.clock import ET
from earnings_bot.config import load_config
from earnings_bot.data.calendar_sources import NasdaqCalendar, merge_events
from earnings_bot.analysis.scoring import final_score, liquidity_ok, llm_points, quant_score
from earnings_bot.models import Candidate, EarningsEvent, OptionKey, OptionQuote, Position, PositionLeg, TradeThesis
from earnings_bot.strategy.exits import apply_fill_to_position, evaluate_exits

cfg = load_config("tests/fixture_config.yaml")


def q(key, bid, ask, oi=5000, vol=500):
    return OptionQuote(key, bid, ask, (bid + ask) / 2, vol, oi, 0.9)


def test_liquidity_gates():
    k = OptionKey("X", date(2026, 11, 13), 100.0, "C")
    assert liquidity_ok(q(k, 2.00, 2.20), cfg.options)[0]
    assert not liquidity_ok(q(k, 2.00, 2.60), cfg.options)[0]          # 26% spread
    assert not liquidity_ok(q(k, 2.00, 2.20, oi=50), cfg.options)[0]
    assert not liquidity_ok(q(k, 0.05, 0.10), cfg.options)[0]          # below min price
    assert not liquidity_ok(q(k, 0.0, 2.20), cfg.options)[0]


def _cand(ratio, n=8, timing="amc"):
    ev = EarningsEvent("X", date(2026, 11, 12), timing, "X", 1e10, 1.0, True, ["nasdaq"])
    c = Candidate(ev, spot=100, implied_move_pct=5.0)
    c.hist_stats = {"n": n, "ratio_median": ratio, "big_beat_rate": 0.5 if ratio > 1 else 0.1, "median_abs": 5 * ratio}
    c.liquidity = {"quality": 0.8}
    return c


def test_quant_score_monotonic_and_llm_veto():
    lo, _ = quant_score(_cand(0.7), cfg.scoring)
    hi, _ = quant_score(_cand(1.5), cfg.scoring)
    assert hi > lo + 30
    unk, _ = quant_score(_cand(1.5, timing="unknown"), cfg.scoring)
    assert unk == hi - cfg.scoring.unknown_timing_penalty
    c = _cand(1.5)
    c.thesis = TradeThesis(symbol="X", event_still_expected=False, direction="neutral", direction_confidence=0.5, expected_abs_move_vs_implied=1.2,
                           big_move_probability=0.5, recommended_structure="straddle", key_catalysts=[], key_risks=[], data_quality="good", rationale="r")
    s, bd = final_score(c, cfg.scoring)
    assert s == 0 and bd["llm_veto"]
    c.thesis.event_still_expected = True
    s2, _ = final_score(c, cfg.scoring)
    assert s2 > hi


def _straddle_pos(cost_c=3.0, cost_p=2.0, qty=2):
    kc, kp = OptionKey("X", date(2026, 11, 13), 100.0, "C"), OptionKey("X", date(2026, 11, 13), 100.0, "P")
    cost = (cost_c + cost_p) * 100 * qty + 0.65 * 2 * qty
    return Position("X-1", "X", "earnings", "straddle", [PositionLeg(kc, qty, cost_c, qty), PositionLeg(kp, qty, cost_p, qty)],
                    datetime(2026, 11, 12, 15, 40, tzinfo=ET), date(2026, 11, 13), date(2026, 11, 13), cost), kc, kp


def test_exit_rules():
    pos, kc, kp = _straddle_pos()
    hold = evaluate_exits(pos, {kc: q(kc, 3, 3.1), kp: q(kp, 2, 2.1)}, datetime(2026, 11, 12, 15, 45, tzinfo=ET), cfg.exits)
    assert hold == []                                                    # never exits before the reaction day
    # gap up: call 9.00, put worthless (0.02 bid) on expiry day -> dust is left to expire, no salvage sale
    acts = evaluate_exits(pos, {kc: q(kc, 9.0, 9.2), kp: q(kp, 0.02, 0.04)}, datetime(2026, 11, 13, 9, 31, tzinfo=ET), cfg.exits)
    assert all(a.leg_key != kp for a in acts)
    # +100%: take half of the call (the engine marks took_half once the sale fills)
    pos2, kc, kp = _straddle_pos()
    acts = evaluate_exits(pos2, {kc: q(kc, 11.0, 11.2), kp: q(kp, 0.02, 0.04)}, datetime(2026, 11, 13, 9, 31, tzinfo=ET), cfg.exits)
    assert [(a.leg_key, a.qty) for a in acts] == [(kc, 1)] and not pos2.took_half
    apply_fill_to_position(pos2, kc, 1, 11.0, 0.65)
    pos2.took_half = True
    # trailing stop on the remainder: re-based high water 1100 (11.0 x 1) then value falls 30%
    assert evaluate_exits(pos2, {kc: q(kc, 11.0, 11.2), kp: q(kp, 0.02, 0.04)}, datetime(2026, 11, 13, 9, 32, tzinfo=ET), cfg.exits) == []
    acts = evaluate_exits(pos2, {kc: q(kc, 7.5, 7.7), kp: q(kp, 0.02, 0.04)}, datetime(2026, 11, 13, 9, 40, tzinfo=ET), cfg.exits)
    assert acts and acts[0].reason.startswith("trail_stop")
    # dud: both legs collapse -> no hard stop before 09:45, hard stop after
    pos3, kc, kp = _straddle_pos()
    quotes = {kc: q(kc, 0.9, 1.0), kp: q(kp, 0.8, 0.9)}
    assert evaluate_exits(pos3, quotes, datetime(2026, 11, 13, 9, 40, tzinfo=ET), cfg.exits) == []
    acts = evaluate_exits(pos3, quotes, datetime(2026, 11, 13, 9, 46, tzinfo=ET), cfg.exits)
    assert len(acts) == 2 and acts[0].reason.startswith("hard_stop")
    # salvage: put collapses while call wins, before the time stop
    pos4, kc, kp = _straddle_pos()
    acts = evaluate_exits(pos4, {kc: q(kc, 4.0, 4.2), kp: q(kp, 0.30, 0.35)}, datetime(2026, 11, 13, 9, 35, tzinfo=ET), cfg.exits)
    assert [a.leg_key for a in acts] == [kp] and acts[0].reason.startswith("salvage")
    # time stop at 10:30 for a non-winner, forced absolute stop sells penny bids on 0DTE
    pos5, kc, kp = _straddle_pos()
    acts = evaluate_exits(pos5, {kc: q(kc, 3.2, 3.3), kp: q(kp, 1.5, 1.6)}, datetime(2026, 11, 13, 10, 31, tzinfo=ET), cfg.exits)
    assert len(acts) == 2 and acts[0].reason == "time_stop"
    acts = evaluate_exits(pos5, {kc: q(kc, 0.01, 0.02), kp: q(kp, 0.0, 0.01)}, datetime(2026, 11, 13, 15, 1, tzinfo=ET), cfg.exits)
    assert [a.leg_key for a in acts] == [kc] and acts[0].reason == "time_stop_absolute"


def test_apply_fill_accounting():
    pos, kc, kp = _straddle_pos(cost_c=3.0, cost_p=2.0, qty=2)
    realized = apply_fill_to_position(pos, kc, 2, 9.0, 1.30)
    assert abs(realized - ((9.0 - 3.0) * 200 - 1.30)) < 1e-9
    assert pos.legs[0].remaining == 0 and pos.realized_pnl == realized and len(pos.open_legs()) == 1


def test_merge_events_conflicts():
    m = merge_events([
        [EarningsEvent("A", date(2026, 11, 12), "amc", "A", 1e10, None, False, ["nasdaq"])],
        [EarningsEvent("A", date(2026, 11, 12), "bmo", "", None, None, False, ["fmp"])],
        [EarningsEvent("B", date(2026, 11, 12), "amc", "", None, None, False, ["fmp"]), EarningsEvent("B", date(2026, 11, 12), "amc", "", 2e9, None, False, ["finnhub"])],
    ])
    by = {e.symbol: e for e in m}
    assert by["A"].timing == "amc" and by["A"].sources == ["fmp", "nasdaq"]      # nasdaq wins the conflict
    assert by["B"].timing == "amc" and by["B"].confirmed and by["B"].market_cap == 2e9


async def test_nasdaq_parser_with_recorded_shape():
    rows = [{"symbol": "CSCO", "name": "Cisco Systems, Inc.", "time": "time-after-hours", "marketCap": "$408,000,000,000", "epsForecast": "$1.15", "noOfEsts": "12"},
            {"symbol": "KKR", "name": "KKR & Co.", "time": "time-pre-market", "marketCap": "$120,000,000,000", "epsForecast": "$1.41", "noOfEsts": "9"},
            {"symbol": "NU", "name": "Nu Holdings", "time": "time-not-supplied", "marketCap": "$70,000,000,000", "epsForecast": "", "noOfEsts": "5"}]

    def handler(request: httpx.Request):
        assert request.url.params["date"] in ("2026-11-12",)
        return httpx.Response(200, json={"data": {"rows": rows}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ev = await NasdaqCalendar(client).fetch(date(2026, 11, 12), date(2026, 11, 12))
    by = {e.symbol: e for e in ev}
    assert by["CSCO"].timing == "amc" and by["CSCO"].market_cap == 4.08e11 and by["CSCO"].eps_estimate == 1.15
    assert by["KKR"].timing == "bmo" and by["NU"].timing == "unknown" and by["NU"].eps_estimate is None
