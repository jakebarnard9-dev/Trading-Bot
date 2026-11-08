"""Long-running loop: tick, sleep according to market phase, exit cleanly on SIGINT/SIGTERM."""
from __future__ import annotations

import asyncio
import logging
import signal

log = logging.getLogger(__name__)


async def run_forever(engine) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    attempt = 0
    while not stop.is_set():
        try:
            await engine.start()
            break
        except Exception as e:  # noqa: BLE001
            attempt += 1
            log.error("start failed (attempt %d): %s", attempt, e)
            if attempt == 1:
                await engine.notify(f"cannot start: {e}. Retrying every 30s (is TWS/IB Gateway running and logged in?)", "error")
            try:
                await asyncio.wait_for(stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
    try:
        while not stop.is_set():
            await engine.tick()
            try:
                await asyncio.wait_for(stop.wait(), timeout=engine.next_sleep_seconds(engine.clock.now()))
            except asyncio.TimeoutError:
                pass
    finally:
        await engine.stop()
        log.info("stopped")
