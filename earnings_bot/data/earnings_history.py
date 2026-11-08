"""Historical earnings reactions and simple price-action features from daily bars."""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from ..models import EarningsMove


def _bar_dates(bars: pd.DataFrame) -> list[date]:
    return [pd.Timestamp(i).date() for i in bars.index]


def _window(bars: pd.DataFrame, dates: list[date], d: date, timing: str) -> Optional[EarningsMove]:
    """Reaction window for a past report date d given the timing convention."""
    if timing == "amc":
        before = [x for x in dates if x <= d]
        after = [x for x in dates if x > d]
    else:  # bmo
        before = [x for x in dates if x < d]
        after = [x for x in dates if x >= d]
    if not before or not after:
        return None
    b, a = before[-1], after[0]
    if (a - b).days > 6:  # stale/missing data
        return None
    rb, ra = bars.loc[pd.Timestamp(b)], bars.loc[pd.Timestamp(a)]
    cb, oa, ca = float(rb["Close"]), float(ra["Open"]), float(ra["Close"])
    if cb <= 0 or oa <= 0 or ca <= 0:
        return None
    return EarningsMove(d, cb, oa, ca)


def compute_moves(bars: pd.DataFrame, past_dates: list[date], timing: str, limit: int = 8) -> list[EarningsMove]:
    if bars is None or len(bars) == 0 or not past_dates:
        return []
    bars = bars.copy()
    bars.index = pd.to_datetime(bars.index).normalize()
    dates = _bar_dates(bars)
    out: list[EarningsMove] = []
    seen: set[date] = set()
    for d in sorted(set(past_dates), reverse=True):
        if d in seen or any(abs((d - s).days) < 20 for s in seen):
            continue
        if timing in ("amc", "bmo"):
            m = _window(bars, dates, d, timing)
        else:
            m1, m2 = _window(bars, dates, d, "amc"), _window(bars, dates, d, "bmo")
            cands = [m for m in (m1, m2) if m]
            m = max(cands, key=lambda m: abs(m.gap_pct)) if cands else None
        if m:
            out.append(m)
            seen.add(d)
        if len(out) >= limit:
            break
    return out


def history_stats(moves: list[EarningsMove], implied_pct: Optional[float]) -> dict:
    if not moves:
        return {"n": 0}
    gaps = np.array([abs(m.gap_pct) for m in moves])
    ccs = np.array([abs(m.cc_pct) for m in moves])
    signed = np.array([m.cc_pct for m in moves])
    use = np.maximum(gaps, ccs * 0.9)  # the tradable move is roughly the larger of the gap and the day's close-to-close
    out = {
        "n": int(len(moves)),
        "mean_abs": float(use.mean()),
        "median_abs": float(np.median(use)),
        "max_abs": float(use.max()),
        "mean_gap_abs": float(gaps.mean()),
        "up_rate": float((signed > 0).mean()),
        "mean_signed": float(signed.mean()),
        "last_moves": [round(float(m.cc_pct), 2) for m in moves[:8]],
    }
    if implied_pct and implied_pct > 0:
        out["ratio_median"] = out["median_abs"] / implied_pct
        out["ratio_mean"] = out["mean_abs"] / implied_pct
        out["beat_rate"] = float((use > implied_pct).mean())
        out["big_beat_rate"] = float((use > 1.25 * implied_pct).mean())
    return out


def price_features(bars: pd.DataFrame) -> dict:
    if bars is None or len(bars) < 25:
        return {}
    c = bars["Close"].astype(float)
    r = np.log(c).diff().dropna()
    last = float(c.iloc[-1])
    hi252, lo252 = float(c.tail(252).max()), float(c.tail(252).min())
    return {
        "ret_5d": float(last / c.iloc[-6] - 1) * 100 if len(c) > 6 else 0.0,
        "ret_20d": float(last / c.iloc[-21] - 1) * 100 if len(c) > 21 else 0.0,
        "rv_20d_ann": float(r.tail(20).std() * math.sqrt(252)) * 100,
        "pos_52w": float((last - lo252) / (hi252 - lo252)) if hi252 > lo252 else 0.5,
        "avg_dollar_vol_20d": float((bars["Close"].tail(20) * bars["Volume"].tail(20)).mean()),
    }
