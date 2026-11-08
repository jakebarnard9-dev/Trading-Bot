"""Exit rules for open positions on the reaction day. Pure functions: easy to test, no I/O."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..clock import parse_hhmm
from ..config import ExitsCfg
from ..models import ExitAction, OptionKey, OptionQuote, Position, PositionLeg


def position_value(pos: Position, quotes: dict[OptionKey, OptionQuote]) -> float:
    """Conservative liquidation value (at the bid) of the open legs, in dollars."""
    v = 0.0
    for l in pos.open_legs():
        q = quotes.get(l.key)
        if q:
            v += max(q.bid, 0.0) * 100 * l.remaining
    return v


def pnl_pct(pos: Position, value: float) -> float:
    total = pos.realized_pnl + (value - pos.remaining_cost())
    return total / pos.cost_basis if pos.cost_basis > 0 else 0.0


def _sell_all(pos: Position, quotes, reason: str, urgency="normal", cfg: Optional[ExitsCfg] = None, today=None, force: bool = False) -> list[ExitAction]:
    """force=True (final 0DTE / absolute stops): sell anything with a bid at all so nothing is left to auto-exercise."""
    acts = []
    for l in pos.open_legs():
        q = quotes.get(l.key)
        if q is not None and q.bid <= 0:
            continue  # nothing to sell into; an OTM option with no bid expires worthless
        if not force and cfg and q is not None and q.bid < cfg.dust_threshold and pos.expiry == today:
            continue  # worthless and expiring today: let it expire rather than pay commission
        acts.append(ExitAction(l.key, l.remaining, reason, urgency))
    return acts


def quotes_usable(pos: Position, quotes: dict[OptionKey, OptionQuote]) -> bool:
    """True when every open leg has a quote with at least one side (a worthless option still shows an ask)."""
    for l in pos.open_legs():
        q = quotes.get(l.key)
        if q is None or (q.bid <= 0 and q.ask <= 0):
            return False
    return True


def book_expired_leg(pos: Position, leg: PositionLeg) -> float:
    """Write off a leg that expired (or vanished at the broker) unsold; returns the loss booked (negative dollars)."""
    loss = -leg.avg_cost * 100 * leg.remaining
    leg.realized += loss
    pos.realized_pnl += loss
    leg.remaining = 0
    return loss


def evaluate_exits(pos: Position, quotes: dict[OptionKey, OptionQuote], now: datetime, cfg: ExitsCfg, commission: float = 0.65) -> list[ExitAction]:
    today = now.date()
    legs = pos.open_legs()
    if not legs:
        return []
    if today < pos.reaction_date:
        # holding into the event is the premise; only a same-day expiry would force an exit, which pick_expiry prevents
        return []
    t = now.time()
    ok = quotes_usable(pos, quotes)
    value = position_value(pos, quotes)
    p = pnl_pct(pos, value)
    if ok and value > pos.high_water_value:
        pos.high_water_value = value
        pos.meta["hw_pnl_pct"] = p
    zero_dte = pos.expiry == today

    # E) hard time stops first (they do not depend on marks)
    if t >= parse_hhmm(cfg.absolute_time_stop) or (zero_dte and t >= parse_hhmm(cfg.zero_dte_time_stop)):
        return _sell_all(pos, quotes, "time_stop_absolute", "high", cfg, today, force=True)
    if t >= parse_hhmm(cfg.winner_time_stop):
        return _sell_all(pos, quotes, "time_stop_winner_window", "high", cfg, today)
    is_winner = ok and p >= cfg.trail_floor_gain_pct
    if t >= parse_hhmm(cfg.default_time_stop) and not is_winner:
        return _sell_all(pos, quotes, "time_stop", "normal", cfg, today)
    if not ok:
        return []  # a leg has no quote this tick: never act on a mark we do not trust

    # C) trailing stop on the open legs once the position has shown a real gain (high-water re-bases after every sale)
    if pos.meta.get("hw_pnl_pct", -1.0) >= cfg.trail_floor_gain_pct and pos.high_water_value > 0 and value <= pos.high_water_value * (1 - cfg.trail_pct_from_high):
        return _sell_all(pos, quotes, f"trail_stop (hw {pos.high_water_value:.0f} -> {value:.0f})", "normal", cfg, today)

    # D) hard stop after the opening prints settle
    if t >= parse_hhmm(cfg.hard_stop_not_before) and p <= -cfg.hard_stop_loss_pct:
        return _sell_all(pos, quotes, f"hard_stop ({p:+.0%})", "normal", cfg, today)

    acts: list[ExitAction] = []
    # B) take half at the gain target (the engine marks took_half only after the sale actually fills)
    if not pos.took_half and p >= cfg.take_half_at_gain_pct and sum(l.remaining for l in legs) >= 2:
        for l in legs:
            q = quotes.get(l.key)
            if q and q.bid > cfg.dust_threshold:
                half = l.remaining // 2 if len(legs) > 1 else (l.remaining + 1) // 2
                if half > 0:
                    acts.append(ExitAction(l.key, half, f"take_half ({p:+.0%})"))
        if acts:
            return acts

    # A) salvage the losing leg of a multi-leg structure
    if len(legs) > 1:
        for l in legs:
            q = quotes.get(l.key)
            if not q:
                continue
            leg_val = q.bid * 100 * l.remaining
            leg_cost = l.avg_cost * 100 * l.remaining
            others_winning = any((quotes.get(o.key) and quotes[o.key].bid > o.avg_cost) for o in legs if o is not l)
            if leg_val < cfg.salvage_leg_below_pct * leg_cost and others_winning:
                if q.bid < cfg.dust_threshold and (pos.expiry == today or q.bid * 100 * l.remaining <= 2 * commission * l.remaining):
                    continue
                acts.append(ExitAction(l.key, l.remaining, f"salvage_leg (worth {leg_val / max(leg_cost, 1e-9):.0%} of cost)"))
    return acts


def apply_fill_to_position(pos: Position, key: OptionKey, qty: int, price: float, commission_total: float) -> float:
    """Reduce a leg after a sale; returns realized P/L dollars for that sale."""
    for l in pos.legs:
        if l.key == key and l.remaining > 0:
            q = min(qty, l.remaining)
            proceeds = price * 100 * q - commission_total
            cost = l.avg_cost * 100 * q
            l.remaining -= q
            l.realized += proceeds - cost
            pos.realized_pnl += proceeds - cost
            pos.high_water_value = 0.0          # re-base the trailing stop on what is left
            pos.meta.pop("hw_pnl_pct", None)
            return proceeds - cost
    return 0.0
