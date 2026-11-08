"""Connectivity / configuration checks: run before the first trading day."""
from __future__ import annotations

import asyncio
import time
from datetime import date, timedelta

from .app import build_engine, make_calendar_sources
from .config import Config


async def run_doctor(cfg: Config) -> int:
    fails = 0

    def report(name: str, ok: bool, detail: str = ""):
        nonlocal fails
        fails += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}")

    try:
        cfg.validate_live()
        report("config", True, f"mode={cfg.mode} broker={cfg.broker} profile={cfg.risk.profile}")
    except Exception as e:  # noqa: BLE001
        report("config", False, str(e))
    eng = build_engine(cfg)
    broker_ok = False
    try:
        await eng.broker.connect()
        a = await eng.broker.account()
        broker_ok = True
        report("broker", True, f"equity=${a.equity:,.0f} cash(available)=${a.cash:,.0f} day_trades_remaining={a.day_trades_remaining} margin={a.is_margin}")
    except Exception as e:  # noqa: BLE001
        report("broker", False, repr(e))
    if cfg.broker in ("ibkr", "alpaca") and broker_ok:
        try:
            from .clock import next_trading_day
            from .models import OptionKey

            spot = await eng.broker.spot("SPY")
            exp = next_trading_day(date.today()) if date.today().weekday() >= 5 or time.localtime().tm_hour >= 16 else date.today()
            key = OptionKey("SPY", exp, float(round(spot)), "C")
            q = (await eng.broker.option_quotes([key])).get(key)
            ok = bool(q and q.has_market)
            report("broker option quote (needs OPRA data + options permission)", ok,
                   f"{key}: bid {q.bid if q else None} ask {q.ask if q else None} iv {q.iv if q else None}" + ("" if ok else " -> no two-sided quote; outside market hours this check is inconclusive, during hours it means no OPRA subscription"))
        except Exception as e:  # noqa: BLE001
            report("broker option quote", False, repr(e))
    try:
        t0 = time.time()
        spot = await eng.md.spot("SPY")
        exps = await eng.md.expiries("SPY")
        ch = await eng.md.chain("SPY", exps[0]) if exps else None
        report("market data (yfinance)", spot > 0 and bool(exps) and ch is not None, f"SPY={spot:.2f} expiries={len(exps)} chain_rows={len(ch.calls) if ch else 0} ({time.time() - t0:.1f}s)")
    except Exception as e:  # noqa: BLE001
        report("market data (yfinance)", False, repr(e))
    for src in make_calendar_sources(cfg):
        try:
            ev = await src.fetch(date.today(), date.today() + timedelta(days=7))
            report(f"calendar source {src.name}", len(ev) > 0, f"{len(ev)} events next 7 days")
        except Exception as e:  # noqa: BLE001
            report(f"calendar source {src.name}", False, repr(e))
    try:
        hl = await eng.news.headlines("AAPL", "Apple")
        report("news", len(hl) > 0, f"{len(hl)} headlines for AAPL")
    except Exception as e:  # noqa: BLE001
        report("news", False, repr(e))
    if cfg.llm.enabled and cfg.secrets.anthropic_api_key:
        try:
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=cfg.secrets.anthropic_api_key, timeout=60)
            r = await client.messages.create(model=cfg.llm.model, max_tokens=64, messages=[{"role": "user", "content": "Reply with the single word: ready"}])
            txt = "".join(getattr(b, "text", "") for b in r.content)
            report("LLM analyst", "ready" in txt.lower() or r.stop_reason in ("end_turn", "max_tokens"), f"model={cfg.llm.model} reply={txt.strip()[:40]!r}")
        except Exception as e:  # noqa: BLE001
            report("LLM analyst", False, repr(e))
    else:
        report("LLM analyst", True, "disabled or no ANTHROPIC_API_KEY -> quant-only mode with higher score bar")
    try:
        await eng.notifier.send("doctor: notification test")
        report("notifications", True, "sent (check Telegram/Discord)" if (cfg.secrets.telegram_bot_token or cfg.secrets.discord_webhook_url) else "no channels configured (log only)")
    except Exception as e:  # noqa: BLE001
        report("notifications", False, repr(e))
    await eng.broker.close()
    print(f"\n{fails} check(s) failed" if fails else "\nall checks passed")
    return 1 if fails else 0
