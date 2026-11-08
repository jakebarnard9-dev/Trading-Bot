"""Shared Claude client: a research turn with server-side web search/fetch, and a structured-output parse."""
from __future__ import annotations

import logging
from typing import Optional, Type, TypeVar

from pydantic import BaseModel

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


def web_tools(max_searches: int, max_fetches: int) -> list[dict]:
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max(int(max_searches), 1)}]
    if max_fetches > 0:
        tools.append({"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": int(max_fetches)})
    return tools


class ClaudeAgentClient:
    def __init__(self, api_key: str, model: str = "claude-opus-5-5", effort: str = "high", timeout: float = 600.0, fallbacks: bool = True):
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=2)
        self.model = model
        self.effort = effort
        self.enabled = bool(api_key)
        self._fallbacks = bool(fallbacks)

    async def research(self, system: str, user: str, max_searches: int = 10, max_fetches: int = 5, max_tokens: int = 24000) -> Optional[str]:
        """One agentic research turn. Returns the model's final text notes (web results are untrusted data)."""
        if not self.enabled:
            return None
        anthropic = self._anthropic
        messages = [{"role": "user", "content": user}]
        texts: list[str] = []
        try:
            for _ in range(6):
                resp = await self.client.messages.create(
                    model=self.model, max_tokens=max_tokens, system=system, messages=messages,
                    tools=web_tools(max_searches, max_fetches), output_config={"effort": self.effort},
                )
                for b in resp.content:
                    if getattr(b, "type", "") == "text" and getattr(b, "text", ""):
                        texts.append(b.text)
                if resp.stop_reason == "pause_turn":
                    messages.append({"role": "assistant", "content": resp.content})
                    continue
                if resp.stop_reason == "refusal":
                    log.warning("research turn refused")
                    return None
                break
        except anthropic.APIStatusError as e:
            log.warning("research API error: %s", e)
            return "\n".join(texts).strip() or None
        except anthropic.APIConnectionError as e:
            log.warning("research connection error: %s", e)
            return "\n".join(texts).strip() or None
        except Exception as e:  # noqa: BLE001
            log.warning("research unexpected error: %s", e)
            return "\n".join(texts).strip() or None
        return "\n".join(texts).strip() or None

    async def parse(self, system: str, user: str, schema: Type[T], max_tokens: int = 12000) -> Optional[T]:
        if not self.enabled:
            return None
        anthropic = self._anthropic
        kwargs = dict(model=self.model, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user}],
                      output_format=schema, output_config={"effort": self.effort})
        try:
            if self._fallbacks:
                try:
                    resp = await self.client.beta.messages.parse(**kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
                except (anthropic.BadRequestError, TypeError) as e:
                    log.warning("server-side fallbacks rejected (%s); continuing without", e)
                    self._fallbacks = False
                    resp = await self.client.beta.messages.parse(**kwargs)
            else:
                resp = await self.client.beta.messages.parse(**kwargs)
        except anthropic.APIStatusError as e:
            log.warning("parse API error: %s", e)
            return None
        except anthropic.APIConnectionError as e:
            log.warning("parse connection error: %s", e)
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("parse unexpected error: %s", e)
            return None
        if getattr(resp, "stop_reason", None) == "refusal":
            return None
        return getattr(resp, "parsed_output", None)
