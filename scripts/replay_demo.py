"""Thin wrapper: python scripts/replay_demo.py  (same as `earnings-bot replay`)."""
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from earnings_bot.replay import run_replay  # noqa: E402

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run_replay(verbose=True))
