"""Deterministic candidate scoring and option liquidity gates."""
from __future__ import annotations

import math
from statistics import mean
from typing import Optional

from ..config import OptionsCfg, ScoringCfg
from ..models import Candidate, OptionQuote, TradeThesis


def clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def shrink(value: float, n: int, prior: float, k: float = 3.0) -> float:
    """Shrink a small-sample statistic toward a prior with k pseudo-observations."""
    return (n * value + k * prior) / (n + k)


def liquidity_ok(q: OptionQuote, cfg: OptionsCfg) -> tuple[bool, str]:
    if not q.has_market:
        return False, "no two-sided market"
    if q.mid < cfg.min_option_price:
        return False, f"price {q.mid:.2f} below minimum {cfg.min_option_price:.2f}"
    max_spread = cfg.max_spread_pct if q.mid >= 0.5 else cfg.max_spread_pct_cheap
    if q.spread_pct > max_spread:
        return False, f"spread {q.spread_pct:.0%} > {max_spread:.0%}"
    if q.open_interest < cfg.min_open_interest:
        return False, f"open interest {q.open_interest} < {cfg.min_open_interest}"
    if q.volume < cfg.min_volume:
        return False, f"volume {q.volume} < {cfg.min_volume}"
    return True, "ok"


def liquidity_quality(quotes: list[OptionQuote], cfg: OptionsCfg) -> float:
    vals = []
    for q in quotes:
        if not q.has_market:
            vals.append(0.0)
            continue
        max_spread = cfg.max_spread_pct if q.mid >= 0.5 else cfg.max_spread_pct_cheap
        sp = clip01(1 - (q.spread_pct - 0.02) / max(max_spread - 0.02, 0.01))
        oi = clip01((math.log10(max(q.open_interest, 1)) - math.log10(max(cfg.min_open_interest, 1))) / 1.2)
        vals.append(0.6 * sp + 0.4 * oi)
    return mean(vals) if vals else 0.0


def catalyst_score(c: Candidate) -> float:
    f = c.catalyst or {}
    parts = [clip01(abs(f.get("ret_5d", 0.0)) / 8.0), clip01(len(c.headlines) / 10.0)]
    sp = (c.profile or {}).get("short_pct_float") or 0.0
    parts.append(clip01(float(sp) / 0.20))
    est = f.get("estimates") or {}
    parts.append(clip01((est.get("rev_up_30d", 0) + est.get("rev_down_30d", 0)) / 8.0))
    rv = f.get("rv_20d_ann")
    if rv and c.implied_move_pct:
        daily_rv = float(rv) / math.sqrt(252)
        parts.append(clip01((c.implied_move_pct / max(daily_rv, 0.1) - 2.0) / 4.0))
    return mean(parts)


def quant_score(c: Candidate, scfg: ScoringCfg) -> tuple[float, dict]:
    w = scfg.weights
    st = c.hist_stats or {}
    n = int(st.get("n", 0))
    r = shrink(st.get("ratio_median", 0.95), n, 0.95) if n else 0.95
    hist_pts = w.hist_ratio * clip01((r - 0.75) / 0.55)
    f = shrink(st.get("big_beat_rate", 0.30), n, 0.30) if n else 0.30
    big_pts = w.big_move_freq * clip01((f - 0.20) / 0.50)
    liq_pts = w.liquidity * float((c.liquidity or {}).get("quality", 0.0))
    cat = catalyst_score(c)
    cat_pts = w.catalyst * cat
    pen = scfg.unknown_timing_penalty if c.event.timing == "unknown" else 0.0
    score = hist_pts + big_pts + liq_pts + cat_pts - pen
    return score, {
        "hist_ratio_shrunk": round(r, 3), "hist_pts": round(hist_pts, 1), "big_beat_shrunk": round(f, 3), "big_pts": round(big_pts, 1),
        "liq_pts": round(liq_pts, 1), "catalyst": round(cat, 3), "cat_pts": round(cat_pts, 1), "timing_penalty": pen, "n_hist": n,
    }


def llm_points(thesis: Optional[TradeThesis], scfg: ScoringCfg) -> tuple[float, bool, str]:
    """Returns (points, veto, reason)."""
    w = scfg.weights
    if thesis is None:
        return 0.0, False, "no thesis"
    if not thesis.event_still_expected:
        return 0.0, True, "analyst: event not expected as scheduled"
    if thesis.recommended_structure == "no_trade":
        return 0.0, True, "analyst: no trade"
    a = clip01((thesis.expected_abs_move_vs_implied - 0.9) / 0.5)
    b = clip01((thesis.big_move_probability - 0.35) / 0.35)
    pts = w.llm * (0.5 * a + 0.5 * b)
    if thesis.data_quality == "poor":
        pts *= 0.5
    return pts, False, "ok"


def final_score(c: Candidate, scfg: ScoringCfg) -> tuple[float, dict]:
    q, bd = quant_score(c, scfg)
    pts, veto, reason = llm_points(c.thesis, scfg)
    bd.update({"quant": round(q, 1), "llm_pts": round(pts, 1), "llm_veto": veto, "llm_reason": reason})
    if veto:
        return 0.0, bd
    return q + pts, bd
