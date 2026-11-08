"""Risk officer: an integrity check on the sized plan. It can approve, reduce or veto; it can never add size."""
from __future__ import annotations

import json
import logging
from typing import Optional

from ..models import PlanReview, _json_default
from .base import ClaudeAgentClient

log = logging.getLogger(__name__)

RISK_SYSTEM = """You are the risk officer of an automated short-dated options program. The owner has deliberately chosen
aggressive size limits; you do NOT second-guess the limits themselves. Your job is integrity: catch mistakes before money moves.
Veto or reduce only for concrete reasons, for example:
- the event date or timing looks wrong, already happened, or the stock is halted / being acquired;
- the option expiry falls before the reaction session, or strikes sit far from spot for no stated reason;
- the structure contradicts the thesis (e.g. a call on a bearish thesis), or the thesis says no_trade;
- quotes look stale or one-sided, spreads are extreme, or liquidity is far below the program's gates;
- two picks are the same bet (same company or the same catalyst) and together exceed what one pick may risk;
- the plan cannot be funded from available cash, or today's realized loss already exceeds the daily limit;
- anything that violates the owner's written instructions in the context.
Otherwise approve. Never increase a size. Give exactly one decision per plan symbol; 'reduce' must include the new unit count."""


class RiskOfficer:
    def __init__(self, client: ClaudeAgentClient):
        self.client = client
        self.enabled = client.enabled

    async def review(self, plan: list[dict], context: dict) -> Optional[PlanReview]:
        if not self.enabled or not plan:
            return None
        user = "PLAN:\n" + json.dumps(plan, default=_json_default, indent=1) + "\n\nCONTEXT:\n" + json.dumps(context, default=_json_default, indent=1)
        return await self.client.parse(RISK_SYSTEM, user, PlanReview)


def apply_review(review: Optional[PlanReview], candidates: list) -> list[tuple[str, str]]:
    """Mutates candidate plans in place. Returns [(symbol, note)] for the log. Unknown symbols are ignored."""
    notes: list[tuple[str, str]] = []
    if review is None:
        return notes
    by_symbol = {d.symbol.upper(): d for d in review.decisions}
    for c in candidates:
        d = by_symbol.get(c.symbol.upper())
        if d is None or c.plan is None:
            continue
        if d.action == "veto":
            c.skip_reason = f"risk officer veto: {d.reason}"
            notes.append((c.symbol, c.skip_reason))
        elif d.action == "reduce":
            new_units = d.units if d.units is not None else 0
            if new_units < 1:
                c.skip_reason = f"risk officer veto (reduced to 0): {d.reason}"
            elif new_units < c.plan.units:
                unit_cost = c.plan.est_total_cost / c.plan.units if c.plan.units else 0.0
                c.plan.units = new_units
                c.plan.est_total_cost = unit_cost * new_units
                notes.append((c.symbol, f"risk officer reduced to {new_units} unit(s): {d.reason}"))
            else:
                notes.append((c.symbol, f"risk officer: approved ({d.reason})"))
        else:
            notes.append((c.symbol, f"risk officer: approved ({d.reason})"))
    return notes
