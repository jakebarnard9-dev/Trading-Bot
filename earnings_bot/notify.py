"""Telegram / Discord notifications (best effort; failures never stop trading logic)."""
from __future__ import annotations

import asyncio
import logging

import httpx

from .config import NotifyCfg, Secrets

log = logging.getLogger("earnings_bot.notify")


class Notifier:
    def __init__(self, cfg: NotifyCfg, secrets: Secrets, prefix: str = "[earnings-bot]"):
        self.cfg = cfg
        self.s = secrets
        self.prefix = prefix
        self.sent: list[str] = []

    async def send(self, text: str, level: str = "info") -> None:
        msg = f"{self.prefix} {text}"
        getattr(log, "error" if level == "error" else "warning" if level == "warn" else "info")(text)
        self.sent.append(msg)
        tasks = []
        if self.cfg.telegram and self.s.telegram_bot_token and self.s.telegram_chat_id:
            tasks.append(self._telegram(msg))
        if self.cfg.discord and self.s.discord_webhook_url:
            tasks.append(self._discord(msg))
        if tasks:
            res = await asyncio.gather(*tasks, return_exceptions=True)
            for r in res:
                if isinstance(r, Exception):
                    log.warning("notification failed: %s", r)

    async def _telegram(self, msg: str) -> None:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage",
                             json={"chat_id": self.s.telegram_chat_id, "text": msg[:4000], "disable_web_page_preview": True})
            r.raise_for_status()

    async def _discord(self, msg: str) -> None:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(self.s.discord_webhook_url, json={"content": msg[:1900]})
            r.raise_for_status()
