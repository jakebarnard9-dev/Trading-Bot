"""Print (and optionally write) the week's earnings/macro calendar as the bot will see it."""
from __future__ import annotations


import asyncio
from datetime import date, timedelta
from pathlib import Path

from earnings_bot.app import make_calendar_sources
from earnings_bot.clock import Clock, is_trading_day, reaction_and_entry_dates
from earnings_bot.config import Config
from earnings_bot.data.calendar_sources import fetch_all
from earnings_bot.data.earnings_history import compute_moves, history_stats
from earnings_bot.data.yf_data import YFMarketData
from earnings_bot.pricing import implied_move
from earnings_bot.strategy.macro import load_macro_events
from earnings_bot.strategy.structures import pick_expiry


async def preview(cfg: Config, start: str | None, days: int, deep: bool, write: str | None) -> int:
    s = date.fromisoformat(start) if start else date.today()
    e = s + timedelta(days=days - 1)
    events = await fetch_all(make_calendar_sources(cfg), s, e)
    macro = [m for m in load_macro_events(Path(cfg.root) / cfg.macro.events_file) if s <= m.date <= e]
    lines = [f"# Week preview {s} .. {e}", "", f"Sources: {', '.join(src.name for src in make_calendar_sources(cfg))}; min market cap ${cfg.universe.min_market_cap / 1e9:.1f}B", ""]
    big = [ev for ev in events if (ev.market_cap or 0) >= cfg.universe.min_market_cap or ev.market_cap is None]
    d = s
    while d <= e:
        day_ev = [ev for ev in big if ev.report_date == d]
        if is_trading_day(d) or day_ev:
            lines.append(f"## {d:%a %b %d}" + ("" if is_trading_day(d) else " (market closed)"))
            for m in macro:
                if m.date == d:
                    lines.append(f"- MACRO {m.time} ET: {m.name} (impact {m.impact}, prior move {m.expected_move_pct}%)")
            for timing, label in (("bmo", "Before open"), ("amc", "After close"), ("unknown", "Time unknown")):
                sel = sorted([ev for ev in day_ev if ev.timing == timing], key=lambda x: -(x.market_cap or 0))
                if sel:
                    lines.append(f"- {label}: " + ", ".join(f"{ev.symbol}" + (f" ({ev.market_cap / 1e9:.0f}B)" if ev.market_cap else "") for ev in sel[:40]) + (f" … +{len(sel) - 40}" if len(sel) > 40 else ""))
            lines.append("")
        d += timedelta(days=1)
    lines.append("## Entry schedule (bot buys 15:30-15:57 ET on the entry session, exits on the reaction session)")
    sched: dict[date, list[str]] = {}
    for ev in big:
        if ev.timing == "unknown" and not cfg.universe.include_unknown_timing:
            continue
        entry, reaction = reaction_and_entry_dates(ev.report_date, ev.timing)
        sched.setdefault(entry, []).append(f"{ev.symbol}({ev.timing}, react {reaction:%a})")
    for k in sorted(sched):
        lines.append(f"- enter {k:%a %b %d}: {len(sched[k])} names: " + ", ".join(sched[k][:30]) + (" …" if len(sched[k]) > 30 else ""))
    if deep:
        lines += ["", "## Implied vs historical moves (largest names)"]
        md = YFMarketData()
        clock = Clock()
        top = sorted([ev for ev in big if ev.timing != "unknown"], key=lambda x: -(x.market_cap or 0))[:15]
        for ev in top:
            try:
                _, reaction = reaction_and_entry_dates(ev.report_date, ev.timing)
                exp = pick_expiry(await md.expiries(ev.symbol), reaction, cfg.options.max_days_to_expiry_after_reaction)
                if not exp:
                    lines.append(f"- {ev.symbol}: no near expiry")
                    continue
                ch = await md.chain(ev.symbol, exp)
                im = implied_move(ch) if ch else None
                bars = await md.daily_bars(ev.symbol)
                moves = compute_moves(bars, await md.past_earnings_dates(ev.symbol, 12), ev.timing, 8)
                st = history_stats(moves, im.pct if im else None)
                lines.append(f"- {ev.symbol} {ev.report_date} {ev.timing}: spot {ch.spot:.2f} expiry {exp} implied {im.pct:.1f}% | last moves {st.get('last_moves')} median {st.get('median_abs', 0):.1f}% ratio {st.get('ratio_median', 0):.2f}" if im else f"- {ev.symbol}: no implied move")
            except Exception as ex:  # noqa: BLE001
                lines.append(f"- {ev.symbol}: error {ex!r}")
    text = "\n".join(lines)
    print(text)
    if write:
        Path(write).write_text(text + "\n")
        print(f"\nwritten to {write}")
    return 0
