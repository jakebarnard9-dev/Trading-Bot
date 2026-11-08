"""Structure, strike and expiry selection driven by a payoff simulation over realistic move scenarios."""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Optional

from ..analysis.scoring import liquidity_ok, shrink
from ..clock import ET
from ..config import Config
from ..models import Candidate, Leg, OptionChain, OptionQuote, StructurePlan
from ..pricing import (
    bs_delta,
    build_move_scenarios,
    direction_to_p_up,
    implied_vol,
    quote_iv,
    simulate_structure,
    years_until,
)

log = logging.getLogger(__name__)


def pick_expiry(expiries: list[date], reaction: date, max_days_after: int) -> Optional[date]:
    """Nearest listed expiry on/after the reaction session (0 DTE on the reaction day when it exists)."""
    cands = sorted(e for e in expiries if e >= reaction and (e - reaction).days <= max_days_after)
    return cands[0] if cands else None


def expected_abs_vs_implied(c: Candidate) -> float:
    st = c.hist_stats or {}
    n = int(st.get("n", 0))
    hist = shrink(float(st.get("ratio_median", 1.0)), n, 1.0, 4.0) if n else 1.0
    hist = max(0.6, min(hist, 1.6))
    llm = 1.0
    if c.thesis is not None:
        llm = shrink(float(c.thesis.expected_abs_move_vs_implied), 1, 1.0, 1.0)  # 50% shrink toward 1
    return max(0.6, min(0.95 * hist * (llm ** 0.5), 1.5))


def _leg(q: OptionQuote, qty: int, spot: float, T: float) -> Leg:
    iv = quote_iv(q, spot, T)
    delta = q.delta if q.delta is not None else (bs_delta(spot, q.key.strike, T, iv, q.key.right) if iv else None)
    return Leg(q.key, qty, q.bid, q.ask, q.mid, iv, delta)


def _unit_cost(legs: list[Leg]) -> float:
    return sum(l.ref_ask * l.qty for l in legs)


def choose_structure(c: Candidate, chain: OptionChain, cfg: Config, now: datetime) -> Optional[StructurePlan]:
    """Evaluate straddle / strangle / directional candidates and return the best by expected log growth."""
    S = chain.spot
    im = c.implied_move_pct or 0.0
    if S <= 0 or im <= 0 or c.reaction_date is None:
        return None
    T_now = max(years_until(chain.expiry, now), 1e-6)
    exit_dt = datetime.combine(c.reaction_date, time(10, 0), tzinfo=ET)
    T_exit = max(years_until(chain.expiry, exit_dt), 0.0)
    atm_iv = c.liquidity.get("atm_iv")
    if not atm_iv:
        ck, pk = chain.call(c.atm_strike), chain.put(c.atm_strike)
        ivs = [v for v in (quote_iv(ck, S, T_now) if ck else None, quote_iv(pk, S, T_now) if pk else None) if v]
        atm_iv = sum(ivs) / len(ivs) if ivs else 0.8
    post_iv = max(atm_iv * cfg.structures.iv_crush_factor, 0.15)
    th = c.thesis
    p_up = direction_to_p_up(th.direction, th.direction_confidence) if th else 0.5
    scen = build_move_scenarios([m.cc_pct for m in c.history], im, expected_abs_vs_implied(c), p_up)
    f = cfg.risk.per_trade_pct
    options_cfg = cfg.options
    results: list[tuple[float, StructurePlan]] = []

    def evaluate(kind: str, quotes: list[OptionQuote], rationale: str):
        for q in quotes:
            ok, why = liquidity_ok(q, options_cfg)
            if not ok:
                return None
        legs = [_leg(q, 1, S, T_now) for q in quotes]
        cost = _unit_cost(legs)
        if cost <= 0:
            return None
        sim = simulate_structure([(l.key.right, l.key.strike, l.qty, l.ref_ask) for l in legs], S, scen, T_exit, post_iv, f)
        plan = StructurePlan(c.symbol, kind, legs, cost, sim=sim.as_dict(), rationale=rationale)
        results.append((sim.growth, plan))
        return plan

    strikes = chain.strikes()
    below = [k for k in strikes if k <= S]
    above = [k for k in strikes if k >= S]
    atm_choices = sorted({k for k in ([max(below)] if below else []) + ([min(above)] if above else [])})
    for k in atm_choices:
        cq, pq = chain.call(k), chain.put(k)
        if cq and pq:
            evaluate("straddle", [cq, pq], f"ATM straddle @ {k:g}")
    for w in (0.25, cfg.structures.strangle_max_width_vs_implied):
        if w <= 0:
            continue
        up = [k for k in strikes if k > S and (k / S - 1) * 100 <= w * im]
        dn = [k for k in strikes if k < S and (1 - k / S) * 100 <= w * im]
        if up and dn:
            kc, kp = max(up), min(dn)
            cq, pq = chain.call(kc), chain.put(kp)
            if cq and pq and kc != kp:
                evaluate("strangle", [cq, pq], f"strangle {kp:g}P/{kc:g}C (wings within {w:.0%} of implied move)")
    if th and th.direction != "neutral" and th.direction_confidence >= cfg.structures.directional_min_confidence:
        right = "C" if th.direction == "bullish" else "P"
        pool = chain.calls if right == "C" else chain.puts
        target = cfg.structures.directional_target_delta
        scored = []
        for q in pool:
            iv = quote_iv(q, S, T_now) or atm_iv
            d = abs(q.delta) if q.delta is not None else abs(bs_delta(S, q.key.strike, T_now, iv, right))
            scored.append((abs(d - target), q))
        scored.sort(key=lambda x: x[0])
        for _, q in scored[:2]:
            evaluate("call" if right == "C" else "put", [q], f"{th.direction} {right} @ {q.key.strike:g} (delta~{target}), confidence {th.direction_confidence:.2f}")
    if not results:
        return None
    results.sort(key=lambda x: x[0], reverse=True)
    best_growth, best = results[0]
    if best.sim.get("expected_return", -1) <= 0.03 or best_growth <= 0:
        log.info("%s: best structure %s has non-positive edge (ER %.1f%%, growth %.4f) -> no trade", c.symbol, best.kind, 100 * best.sim.get("expected_return", 0), best_growth)
        return None
    return best


def skew_snapshot(chain: OptionChain, spot: float, T: float) -> dict:
    """25-delta call vs put IV and put/call volume, for the analyst packet."""
    def nearest(pool, target):
        best, bd = None, 9
        for q in pool:
            iv = quote_iv(q, spot, T)
            if not iv:
                continue
            d = abs(bs_delta(spot, q.key.strike, T, iv, q.key.right))
            if abs(d - target) < bd:
                best, bd = (q, iv), abs(d - target)
        return best

    c25, p25 = nearest(chain.calls, 0.25), nearest(chain.puts, 0.25)
    cv = sum(q.volume for q in chain.calls)
    pv = sum(q.volume for q in chain.puts)
    return {
        "call_25d_iv": round(c25[1], 3) if c25 else None,
        "put_25d_iv": round(p25[1], 3) if p25 else None,
        "put_call_volume": round(pv / cv, 2) if cv else None,
        "call_volume": cv, "put_volume": pv,
    }
