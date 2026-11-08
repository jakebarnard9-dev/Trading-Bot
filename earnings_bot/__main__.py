"""CLI: python -m earnings_bot <command>  (or `earnings-bot <command>` after pip install -e .)"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import sys
from datetime import date, datetime
from pathlib import Path

from .config import load_config


def setup_logging(root: Path, verbose: bool) -> None:
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    h1 = logging.StreamHandler(sys.stdout)
    h1.setFormatter(fmt)
    h2 = logging.handlers.RotatingFileHandler(logs / "bot.log", maxBytes=10_000_000, backupCount=10)
    h2.setFormatter(fmt)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=[h1, h2])
    for noisy in ("httpx", "httpcore", "urllib3", "yfinance", "peewee", "ib_async.client", "ib_async.wrapper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _json(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    return str(o)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="earnings-bot", description="Autonomous nearest-expiry earnings/event options bot")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the autonomous loop (foreground; use systemd/docker to keep it alive)")
    p = sub.add_parser("preview", help="show the week's earnings/macro calendar as the bot sees it")
    p.add_argument("--start", default=None, help="YYYY-MM-DD (default: today)")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--deep", action="store_true", help="also compute implied moves / history for the largest names (slower)")
    p.add_argument("--write", default=None, help="write the preview to this markdown file")
    sub.add_parser("status", help="account, open positions, recent decisions")
    f = sub.add_parser("flatten", help="sell every open position now")
    f.add_argument("--yes", action="store_true")
    sub.add_parser("doctor", help="connectivity and configuration checks")
    sub.add_parser("reset-kill", help="clear the equity-floor halt / losing-streak pause")
    sub.add_parser("replay", help="end-to-end dry run on synthetic data with the paper broker")
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    setup_logging(cfg.root, a.verbose)
    log = logging.getLogger("earnings_bot")
    if a.cmd != "run":
        cfg.ibkr.client_id += 1   # utilities use their own IBKR client id so they work while the bot is running

    if a.cmd == "run":
        cfg.validate_live()
        from .app import build_engine
        from .runner import run_forever

        if cfg.is_live:
            log.warning("LIVE MODE: real orders will be placed through %s", cfg.broker)
        asyncio.run(run_forever(build_engine(cfg)))
        return 0
    if a.cmd == "preview":
        from .preview import preview

        return asyncio.run(preview(cfg, a.start, a.days, a.deep, a.write))
    if a.cmd == "status":
        from .app import build_engine

        async def _status():
            eng = build_engine(cfg)
            await eng.broker.connect()
            try:
                print(json.dumps(await eng.status(), indent=2, default=_json))
            finally:
                await eng.broker.close()

        asyncio.run(_status())
        return 0
    if a.cmd == "flatten":
        if not a.yes:
            print("refusing without --yes")
            return 2
        from .app import build_engine

        async def _flat():
            eng = build_engine(cfg)
            await eng.broker.connect()
            try:
                await eng.flatten_all("CLI flatten")
            finally:
                await eng.broker.close()

        asyncio.run(_flat())
        return 0
    if a.cmd == "doctor":
        from .doctor import run_doctor

        return asyncio.run(run_doctor(cfg))
    if a.cmd == "reset-kill":
        from .risk.manager import RiskManager
        from .state.store import Store

        RiskManager(cfg, Store(cfg.root / cfg.state.db_path)).reset_halt()
        print("halt/pause cleared")
        return 0
    if a.cmd == "replay":
        from .replay import run_replay

        r = asyncio.run(run_replay(cfg))
        print(f"replay finished (synthetic $5k paper scenario): equity ${r['equity']:,.2f} from ${r['start']:,.0f}; positions: "
              + ", ".join(f"{p.id} {p.status} P/L ${p.realized_pnl:,.0f}" for p in r['positions']))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
