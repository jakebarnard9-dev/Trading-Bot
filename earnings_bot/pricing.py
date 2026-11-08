"""Black-Scholes utilities, implied move, scenario generation and structure payoff simulation."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Iterable, Optional, Sequence

import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm

from .clock import ET
from .models import OptionChain, OptionQuote

RISK_FREE = 0.04
MIN_T = 1.0 / (365 * 24 * 60)  # one minute, in years


def bs_price(S: float, K: float, T: float, sigma: float, right: str, r: float = RISK_FREE) -> float:
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if right == "C" else max(K - S, 0.0)
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / sq
    d2 = d1 - sq
    if right == "C":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bs_delta(S: float, K: float, T: float, sigma: float, right: str, r: float = RISK_FREE) -> float:
    if T <= 0 or sigma <= 0:
        itm = (S > K) if right == "C" else (S < K)
        return (1.0 if itm else 0.0) * (1 if right == "C" else -1)
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / sq
    return norm.cdf(d1) if right == "C" else norm.cdf(d1) - 1.0


def implied_vol(price: float, S: float, K: float, T: float, right: str, r: float = RISK_FREE) -> Optional[float]:
    intrinsic = max(S - K, 0.0) if right == "C" else max(K - S, 0.0)
    if T <= 0 or price <= intrinsic + 1e-9:
        return None
    try:
        return brentq(lambda s: bs_price(S, K, T, s, right, r) - price, 1e-4, 15.0, xtol=1e-6, maxiter=200)
    except ValueError:
        return None


def years_until(expiry: date, now: datetime, close_hhmm: time = time(16, 0)) -> float:
    exp_dt = datetime.combine(expiry, close_hhmm, tzinfo=ET)
    secs = (exp_dt - now.astimezone(ET)).total_seconds()
    return max(secs, 0.0) / (365.0 * 24 * 3600)


def round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return round(price, 2)
    return round(round(price / tick) * tick, 4)


def default_tick(price: float) -> float:
    """Typical US equity option increments: $0.01 below $3, $0.05 at/above (penny-pilot names are finer)."""
    return 0.01 if price < 3.0 else 0.05


@dataclass
class ImpliedMove:
    pct: float  # percent of spot, e.g. 8.5
    atm_strike: float
    straddle_mid: float
    lower_strike: float
    upper_strike: float
    atm_iv: Optional[float]


def implied_move(chain: OptionChain) -> Optional[ImpliedMove]:
    """Expected absolute move implied by the straddle, interpolated between the strikes bracketing spot.

    The straddle price is (to first order) the market's expectation of |S_T - K|, so straddle / spot is
    the implied expected absolute move. No 0.85 fudge factor is applied.
    """
    S = chain.spot
    strikes = [k for k in chain.strikes() if chain.call(k) and chain.put(k)]
    if S <= 0 or not strikes:
        return None

    def straddle(k: float) -> Optional[float]:
        c, p = chain.call(k), chain.put(k)
        if not c or not p or not c.has_market or not p.has_market:
            return None
        return c.mid + p.mid

    below = [k for k in strikes if k <= S]
    above = [k for k in strikes if k >= S]
    if not below or not above:
        k = min(strikes, key=lambda x: abs(x - S))
        s = straddle(k)
        if s is None:
            return None
        return ImpliedMove(100 * s / S, k, s, k, k, _iv_of(chain, k))
    k_lo, k_hi = max(below), min(above)
    s_lo, s_hi = straddle(k_lo), straddle(k_hi)
    if s_lo is None and s_hi is None:
        return None
    if k_lo == k_hi or s_lo is None or s_hi is None:
        k = k_lo if s_lo is not None else k_hi
        s = s_lo if s_lo is not None else s_hi
        return ImpliedMove(100 * s / S, k, s, k_lo, k_hi, _iv_of(chain, k))
    w = (S - k_lo) / (k_hi - k_lo)
    s = s_lo * (1 - w) + s_hi * w
    atm = k_lo if w < 0.5 else k_hi
    return ImpliedMove(100 * s / S, atm, s, k_lo, k_hi, _iv_of(chain, atm))


def _iv_of(chain: OptionChain, k: float) -> Optional[float]:
    c, p = chain.call(k), chain.put(k)
    ivs = [q.iv for q in (c, p) if q and q.iv and 0.01 < q.iv < 10]
    return float(np.mean(ivs)) if ivs else None


# ----------------------------------------------------------------------------- scenarios

def build_move_scenarios(
    hist_moves_pct: Sequence[float],
    implied_pct: float,
    expected_abs_vs_implied: float = 1.0,
    p_up: float = 0.5,
    n: int = 5000,
    seed: int = 7,
) -> np.ndarray:
    """Signed percent moves for the reaction. Magnitudes come from a scaled bootstrap of historical
    earnings moves when enough history exists, otherwise from a fat-tailed Laplace mixture. The
    expected absolute move is anchored to implied_pct * expected_abs_vs_implied; direction from p_up."""
    rng = np.random.default_rng(seed)
    target_abs = max(implied_pct * expected_abs_vs_implied, 0.05)
    hist = np.abs(np.asarray([m for m in hist_moves_pct if m is not None and np.isfinite(m)], dtype=float))
    if hist.size >= 4 and hist.mean() > 0:
        mags = rng.choice(hist, size=n, replace=True)
        mags = mags * rng.lognormal(mean=0.0, sigma=0.25, size=n)  # jitter so the bootstrap is not just 8 points
        mags = mags * (target_abs / max(mags.mean(), 1e-9))
    else:
        b = target_abs / 1.18
        core = rng.laplace(0.0, b, size=n)
        tail = rng.laplace(0.0, 2.2 * b, size=n)
        pick = rng.random(n) < 0.15
        mags = np.abs(np.where(pick, tail, core))
    signs = np.where(rng.random(n) < p_up, 1.0, -1.0)
    return np.clip(mags * signs, -90.0, 400.0)  # a stock cannot lose more than 100%; cap absurd tails


def direction_to_p_up(direction: str, confidence: float) -> float:
    """Map an analyst's view to the probability of an up move; capped so no view is treated as certain."""
    c = min(max(confidence, 0.5), 1.0)
    tilt = (c - 0.5) * 0.6  # max tilt +/- 0.30
    if direction == "bullish":
        return 0.5 + tilt
    if direction == "bearish":
        return 0.5 - tilt
    return 0.5


@dataclass
class SimResult:
    expected_return: float  # mean P/L as a fraction of cost
    median_return: float
    p_profit: float
    p_double: float
    p_lose_half: float
    growth: float  # expected log growth at the given fraction of equity
    cost: float  # per-share cost of one unit

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def simulate_structure(
    legs: Iterable[tuple[str, float, int, float]],
    spot: float,
    scenarios: np.ndarray,
    t_exit_years: float,
    post_iv: float,
    fraction_of_equity: float = 0.1,
) -> SimResult:
    """legs: (right, strike, qty, entry_price). Value each leg after the move at post-event IV and the
    remaining time to expiry at the planned exit."""
    legs = list(legs)
    cost = sum(q * px for (_, _, q, px) in legs)
    if cost <= 0:
        return SimResult(0, 0, 0, 0, 1, -1, cost)
    S_new = np.maximum(spot * (1.0 + scenarios / 100.0), 1e-6)
    value = np.zeros_like(S_new)
    T = max(t_exit_years, 0.0)
    for right, K, qty, _ in legs:
        if T <= MIN_T:
            v = np.maximum(S_new - K, 0.0) if right == "C" else np.maximum(K - S_new, 0.0)
        else:
            v = _vec_bs(S_new, K, T, post_iv, right)
        value += qty * v
    pnl = (value - cost) / cost
    f = min(max(fraction_of_equity, 0.0), 0.95)
    growth = float(np.mean(np.log1p(np.clip(f * pnl, -0.999, None))))
    return SimResult(
        expected_return=float(pnl.mean()),
        median_return=float(np.median(pnl)),
        p_profit=float((pnl > 0).mean()),
        p_double=float((pnl >= 1.0).mean()),
        p_lose_half=float((pnl <= -0.5).mean()),
        growth=growth,
        cost=cost,
    )


def _vec_bs(S: np.ndarray, K: float, T: float, sigma: float, right: str, r: float = RISK_FREE) -> np.ndarray:
    sigma = max(sigma, 0.01)
    sq = sigma * math.sqrt(T)
    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = (np.log(S / K) + (r + 0.5 * sigma * sigma) * T) / sq
        d2 = d1 - sq
        if right == "C":
            return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def quote_iv(q: OptionQuote, spot: float, T: float) -> Optional[float]:
    if q.iv and 0.01 < q.iv < 10:
        return q.iv
    if q.mid > 0:
        return implied_vol(q.mid, spot, q.key.strike, T, q.key.right)
    return None
