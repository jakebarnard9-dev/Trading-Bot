"""News/headline aggregation. Every item is treated as untrusted data downstream."""
from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import quote_plus

import httpx

from ..models import Headline

log = logging.getLogger(__name__)
UA = {"User-Agent": "Mozilla/5.0 (compatible; earnings-bot/0.1; +https://example.invalid)"}


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


class GoogleNewsRSS:
    name = "google_rss"

    async def fetch(self, symbol: str, company: str, days: int) -> list[Headline]:
        q = f'"{company}" OR {symbol} stock earnings when:{days}d' if company else f"{symbol} stock earnings when:{days}d"
        url = f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"
        out: list[Headline] = []
        try:
            async with httpx.AsyncClient(timeout=15, headers=UA, follow_redirects=True) as client:
                r = await client.get(url)
                r.raise_for_status()
            root = ET.fromstring(r.text)
            for item in root.iter("item"):
                title = _clean(item.findtext("title") or "")
                src = item.find("source")
                pub = item.findtext("pubDate")
                try:
                    dt = parsedate_to_datetime(pub) if pub else None
                except Exception:  # noqa: BLE001
                    dt = None
                if title:
                    out.append(Headline(title, _clean(src.text if src is not None else "google"), dt, item.findtext("link") or ""))
        except Exception as e:  # noqa: BLE001
            log.warning("google rss %s failed: %s", symbol, e)
        return out


class YFinanceNews:
    name = "yfinance"

    async def fetch(self, symbol: str, company: str, days: int) -> list[Headline]:
        def fn():
            import yfinance as yf

            return yf.Ticker(symbol).get_news(count=20) or []

        out: list[Headline] = []
        try:
            items = await asyncio.to_thread(fn)
            for it in items:
                c = it.get("content") or it
                title = _clean(c.get("title") or "")
                pub = c.get("pubDate") or c.get("providerPublishTime")
                dt: Optional[datetime] = None
                if isinstance(pub, (int, float)):
                    dt = datetime.fromtimestamp(pub, tz=timezone.utc)
                elif isinstance(pub, str):
                    try:
                        dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                    except ValueError:
                        dt = None
                prov = (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else c.get("publisher")
                url = ((c.get("canonicalUrl") or {}).get("url") if isinstance(c.get("canonicalUrl"), dict) else c.get("link")) or ""
                if title:
                    out.append(Headline(title, _clean(prov or "yahoo"), dt, url))
        except Exception as e:  # noqa: BLE001
            log.warning("yfinance news %s failed: %s", symbol, e)
        return out


class FinnhubNews:
    name = "finnhub"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def fetch(self, symbol: str, company: str, days: int) -> list[Headline]:
        if not self.api_key:
            return []
        out: list[Headline] = []
        try:
            to, frm = date.today(), date.today() - timedelta(days=days)
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get("https://finnhub.io/api/v1/company-news",
                                     params={"symbol": symbol, "from": frm.isoformat(), "to": to.isoformat(), "token": self.api_key})
                r.raise_for_status()
                for it in r.json() or []:
                    title = _clean(it.get("headline") or "")
                    if title:
                        out.append(Headline(title, _clean(it.get("source") or "finnhub"),
                                            datetime.fromtimestamp(it.get("datetime", 0), tz=timezone.utc), it.get("url") or ""))
        except Exception as e:  # noqa: BLE001
            log.warning("finnhub news %s failed: %s", symbol, e)
        return out


class FMPNews:
    name = "fmp"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def fetch(self, symbol: str, company: str, days: int) -> list[Headline]:
        if not self.api_key:
            return []
        out: list[Headline] = []
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get("https://financialmodelingprep.com/stable/news/stock",
                                     params={"symbols": symbol, "limit": 30, "apikey": self.api_key})
                r.raise_for_status()
                for it in r.json() or []:
                    title = _clean(it.get("title") or "")
                    pub = it.get("publishedDate")
                    dt = None
                    if pub:
                        try:
                            dt = datetime.fromisoformat(str(pub).replace("Z", "+00:00"))
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                        except ValueError:
                            dt = None
                    if title:
                        out.append(Headline(title, _clean(it.get("publisher") or it.get("site") or "fmp"), dt, it.get("url") or ""))
        except Exception as e:  # noqa: BLE001
            log.warning("fmp news %s failed: %s", symbol, e)
        return out


class TavilyNews:
    name = "tavily"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def fetch(self, symbol: str, company: str, days: int) -> list[Headline]:
        if not self.api_key:
            return []
        out: list[Headline] = []
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post("https://api.tavily.com/search", json={
                    "api_key": self.api_key, "query": f"{company or symbol} ({symbol}) earnings", "topic": "news",
                    "days": days, "max_results": 10, "search_depth": "basic"})
                r.raise_for_status()
                for it in (r.json() or {}).get("results", []):
                    title = _clean(it.get("title") or "")
                    pub = it.get("published_date")
                    dt = None
                    if pub:
                        try:
                            dt = parsedate_to_datetime(pub)
                        except Exception:  # noqa: BLE001
                            try:
                                dt = datetime.fromisoformat(str(pub).replace("Z", "+00:00"))
                            except ValueError:
                                dt = None
                    if title:
                        out.append(Headline(title, "tavily", dt, it.get("url") or ""))
        except Exception as e:  # noqa: BLE001
            log.warning("tavily news %s failed: %s", symbol, e)
        return out


def dedupe(headlines: list[Headline], days: int, limit: int) -> list[Headline]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    seen: set[str] = set()
    out: list[Headline] = []
    for h in sorted(headlines, key=lambda h: h.published or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True):
        if h.published and h.published.tzinfo is None:
            h.published = h.published.replace(tzinfo=timezone.utc)
        if h.published and h.published < cutoff:
            continue
        k = re.sub(r"[^a-z0-9]", "", h.title.lower())[:80]
        if k in seen or not k:
            continue
        seen.add(k)
        out.append(h)
        if len(out) >= limit:
            break
    return out


class NewsAggregator:
    def __init__(self, sources: list, days: int = 7, limit: int = 25):
        self.sources = sources
        self.days = days
        self.limit = limit

    async def headlines(self, symbol: str, company: str = "") -> list[Headline]:
        results = await asyncio.gather(*(s.fetch(symbol, company, self.days) for s in self.sources), return_exceptions=True)
        allh: list[Headline] = []
        for s, r in zip(self.sources, results):
            if isinstance(r, Exception):
                log.warning("news source %s raised: %s", getattr(s, "name", s), r)
            else:
                allh.extend(r)
        return dedupe(allh, self.days, self.limit)
