"""End-to-end dry run on synthetic data: entry day -> reaction day -> expiry cleanup, with the paper broker.
Used by `earnings-bot replay` and by tests/test_engine_replay.py."""
from __future__ import annotations


import asyncio
import copy
import logging
import tempfile
from datetime import date, datetime
from pathlib import Path

from earnings_bot.analysis.llm import NullAnalyst
from earnings_bot.app import build_engine
from earnings_bot.broker.paper import PaperBroker
from earnings_bot.clock import ET, FakeClock
from earnings_bot.config import Config, load_config
from earnings_bot.data.fixtures import FixtureCalendar, FixtureMarketData, FixtureNews
from earnings_bot.data.news import NewsAggregator
from earnings_bot.models import EarningsEvent, MacroEvent
from earnings_bot.notify import Notifier
from earnings_bot.state.store import Store


def replay_config(base: Config | None = None, **risk_overrides) -> Config:
    cfg = copy.deepcopy(base) if base else load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    cfg.mode, cfg.broker = "paper", "paper"
    cfg.account.starting_equity = cfg.account.initial_equity_for_floor = 5000.0
    cfg.risk.profile, cfg.risk.per_trade_pct, cfg.risk.max_deployed_pct, cfg.risk.max_open_positions, cfg.risk.daily_loss_limit_pct = "aggressive", 0.12, 0.60, 4, 0.25
    cfg.universe.min_market_cap = 1.5e9
    cfg.llm.enabled = False
    cfg.notify.telegram = cfg.notify.discord = False
    cfg.macro.enabled = False
    for k, v in risk_overrides.items():
        setattr(cfg.risk, k, v)
    return cfg


async def run_replay(cfg: Config | None = None, gap_pct: float = 9.0, post_iv: float = 0.5, db_path: str | None = None,
                     analyst=None, macro: bool = False, verbose: bool = False) -> dict:
    cfg = replay_config(cfg)
    if macro:
        cfg.macro.enabled = True
    entry_day, reaction_day, after = date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 16)
    clock = FakeClock(datetime(2026, 11, 12, 7, 50, tzinfo=ET))
    md = FixtureMarketData(clock.now, {
        "GOOD": dict(spot=100.0, iv=1.10, step=2.5, expiries=[date(2026, 11, 13), date(2026, 11, 20)], past_moves=[8, -11, 6, -4, 13, -9, 7, 10], sector="Technology", market_cap=2e10, name="Good Corp"),
        "DULL": dict(spot=50.0, iv=0.60, step=1.0, expiries=[date(2026, 11, 13), date(2026, 11, 20)], past_moves=[1, -2, 1.5, -1, 2, -1.5, 1, 0.5], sector="Utilities", market_cap=3e10, name="Dull Utility"),
        "SMALL": dict(spot=20.0, iv=1.50, step=0.5, expiries=[date(2026, 11, 13)], past_moves=[20, -25, 18], sector="Healthcare", market_cap=5e8, name="Small Bio"),
        "SPY": dict(spot=600.0, iv=0.14, step=1.0, expiries=[date(2026, 11, 13), date(2026, 11, 16)], past_moves=[], sector="Index", market_cap=1e12, name="SPDR S&P 500"),
    })
    cal = FixtureCalendar([
        EarningsEvent("GOOD", entry_day, "amc", "Good Corp", 2e10, 1.0, True, ["nasdaq", "fmp"]),
        EarningsEvent("DULL", entry_day, "amc", "Dull Utility", 3e10, 0.5, True, ["nasdaq"]),
        EarningsEvent("SMALL", entry_day, "amc", "Small Bio", 5e8, -0.3, False, ["nasdaq"]),
        EarningsEvent("LATER", date(2026, 11, 17), "bmo", "Later Inc", 5e10, 1.0, True, ["nasdaq"]),
    ])
    db = db_path or str(Path(tempfile.mkdtemp()) / "replay.sqlite")
    store = Store(db)
    broker = PaperBroker(md, cfg.account.starting_equity, cfg.risk.commission_per_contract, clock)
    notifier = Notifier(cfg.notify, cfg.secrets)

    async def fake_sleep(s: float):
        clock.advance(seconds=s)

    macro_events = [MacroEvent(reaction_day, "08:30", "US CPI (test)", "high", 0.95)] if macro else []
    eng = build_engine(cfg, clock, overrides=dict(store=store, md=md, calendar_sources=[cal], news=NewsAggregator([FixtureNews()], 7, 25),
                                                 analyst=analyst or NullAnalyst(), broker=broker, notifier=notifier, sleep=fake_sleep, macro_events=macro_events))
    await eng.start()
    for hhmm in ("07:50", "12:31", "15:21", "15:31", "16:06"):
        clock.set(datetime.combine(entry_day, datetime.strptime(hhmm, "%H:%M").time(), tzinfo=ET))
        await eng.tick()
    opened = store.open_positions()
    # reaction day: pre-market tick on last night's marks, then the gap prints at the open and IV collapses
    clock.set(datetime.combine(reaction_day, datetime.strptime("07:50", "%H:%M").time(), tzinfo=ET))
    await eng.tick()
    md.set("GOOD", spot=100.0 * (1 + gap_pct / 100.0), iv=post_iv)
    md.set("SPY", spot=600.0 * (1 + 0.012), iv=0.12)
    for hhmm in ("09:31", "09:46", "10:01", "10:16", "10:31", "11:31", "15:31", "16:06"):
        clock.set(datetime.combine(reaction_day, datetime.strptime(hhmm, "%H:%M").time(), tzinfo=ET))
        await eng.tick()
    for hhmm in ("07:50", "09:31", "16:06"):
        clock.set(datetime.combine(after, datetime.strptime(hhmm, "%H:%M").time(), tzinfo=ET))
        await eng.tick()
    acct = await broker.account()
    result = dict(opened=opened, positions=store.all_positions(), equity=acct.equity, cash=acct.cash, start=cfg.account.starting_equity, messages=list(notifier.sent),
                  candidates=store.candidates_for(entry_day), halted=eng.risk.halt_reason(), store=store, engine=eng)
    if verbose:
        for m in notifier.sent:
            print(m)
        print(f"\nfinal equity ${acct.equity:,.2f} (start ${cfg.account.starting_equity:,.0f})")
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run_replay(verbose=True))
