"""Scheduled macro releases (CPI/PPI/FOMC/NFP) traded with index-ETF nearest-expiry straddles."""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Optional

import yaml

from ..clock import prev_trading_day, is_trading_day, next_trading_day
from ..models import EarningsEvent, MacroEvent

log = logging.getLogger(__name__)


def load_macro_events(path: str | Path) -> list[MacroEvent]:
    p = Path(path)
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text()) or {}
    out = []
    for e in raw.get("events", []):
        try:
            d = e["date"] if isinstance(e["date"], date) else date.fromisoformat(str(e["date"]))
            out.append(MacroEvent(d, str(e.get("time", "08:30")), str(e["name"]), str(e.get("impact", "high")), float(e.get("expected_move_pct", 0.9))))
        except Exception as ex:  # noqa: BLE001
            log.warning("bad macro event %s: %s", e, ex)
    return out


def macro_events_for_entry(events: list[MacroEvent], entry_day: date) -> list[MacroEvent]:
    """Pre-market releases on the next session are entered at today's close (same overnight-gap pattern as AMC earnings)."""
    nxt = next_trading_day(entry_day)
    out = []
    for e in events:
        if e.date == nxt and e.time <= "09:30" and e.impact in ("high", "medium"):
            out.append(e)
    return out


def as_pseudo_event(e: MacroEvent, symbol: str) -> EarningsEvent:
    """Represent the macro release as a BMO 'earnings' event on the ETF so the same pipeline can price it."""
    return EarningsEvent(symbol=symbol, report_date=e.date, timing="bmo", company=e.name, market_cap=1e12, confirmed=True, sources=["macro"])


def macro_edge_ok(implied_move_pct: Optional[float], e: MacroEvent) -> tuple[bool, str]:
    if implied_move_pct is None or implied_move_pct <= 0:
        return False, "no implied move"
    if implied_move_pct <= e.expected_move_pct * 0.9:
        return True, f"implied {implied_move_pct:.2f}% <= 90% of prior {e.expected_move_pct:.2f}%"
    return False, f"implied {implied_move_pct:.2f}% not cheap vs prior {e.expected_move_pct:.2f}%"
