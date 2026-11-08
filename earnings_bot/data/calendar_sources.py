"""Earnings calendar sources (Nasdaq public API, FMP, Finnhub, yfinance) and the merge logic."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

import httpx

from ..models import EarningsEvent

log = logging.getLogger(__name__)

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}


def _money_to_float(s) -> Optional[float]:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = re.sub(r"[^0-9.\-]", "", str(s))
    if t in ("", "-", "."):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _norm_timing(raw: Optional[str]) -> str:
    if not raw:
        return "unknown"
    r = str(raw).strip().lower()
    if "pre" in r or r in ("bmo", "before market open", "before open") or r.startswith("b"):
        return "bmo"
    if "after" in r or r in ("amc", "after market close", "after close") or r.startswith("a"):
        return "amc"
    if "during" in r or r == "dmh":
        return "unknown"
    return "unknown"


class NasdaqCalendar:
    """Free public endpoint used by nasdaq.com's earnings calendar page. Needs browser-like headers."""

    name = "nasdaq"

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._client = client

    async def fetch(self, start: date, end: date) -> list[EarningsEvent]:
        out: list[EarningsEvent] = []
        async with (self._client or httpx.AsyncClient(timeout=20, headers=BROWSER_HEADERS)) as client:
            d = start
            while d <= end:
                if d.weekday() < 5:
                    try:
                        r = await client.get("https://api.nasdaq.com/api/calendar/earnings", params={"date": d.isoformat()})
                        r.raise_for_status()
                        rows = ((r.json() or {}).get("data") or {}).get("rows") or []
                        for row in rows:
                            sym = (row.get("symbol") or "").strip().upper()
                            if not sym or not re.fullmatch(r"[A-Z.\-]{1,6}", sym):
                                continue
                            out.append(EarningsEvent(
                                symbol=sym.replace(".", "-") if sym.count(".") == 1 and len(sym) <= 5 else sym,
                                report_date=d,
                                timing=_norm_timing(row.get("time")),
                                company=(row.get("name") or "").strip(),
                                market_cap=_money_to_float(row.get("marketCap")),
                                eps_estimate=_money_to_float(row.get("epsForecast")),
                                confirmed=False,
                                sources=[self.name],
                            ))
                    except Exception as e:  # noqa: BLE001
                        log.warning("nasdaq calendar %s failed: %s", d, e)
                    await asyncio.sleep(0.4)
                d += timedelta(days=1)
        return out


class FMPCalendar:
    name = "fmp"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def fetch(self, start: date, end: date) -> list[EarningsEvent]:
        if not self.api_key:
            return []
        out: list[EarningsEvent] = []
        urls = [
            ("https://financialmodelingprep.com/stable/earnings-calendar", {"from": start.isoformat(), "to": end.isoformat(), "apikey": self.api_key}),
            ("https://financialmodelingprep.com/api/v3/earning_calendar", {"from": start.isoformat(), "to": end.isoformat(), "apikey": self.api_key}),
        ]
        async with httpx.AsyncClient(timeout=20) as client:
            for url, params in urls:
                try:
                    r = await client.get(url, params=params)
                    if r.status_code != 200:
                        continue
                    data = r.json()
                    if not isinstance(data, list):
                        continue
                    for row in data:
                        sym = (row.get("symbol") or "").upper()
                        if not sym or "." in sym and len(sym) > 6:
                            continue
                        try:
                            rd = date.fromisoformat(str(row.get("date"))[:10])
                        except ValueError:
                            continue
                        if not (start <= rd <= end):
                            continue
                        out.append(EarningsEvent(sym, rd, _norm_timing(row.get("time")), "", None, row.get("epsEstimated"),
                                                 bool(row.get("confirmed", False)), [self.name]))
                    if out:
                        break
                except Exception as e:  # noqa: BLE001
                    log.warning("fmp calendar failed: %s", e)
        return out


class FinnhubCalendar:
    name = "finnhub"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def fetch(self, start: date, end: date) -> list[EarningsEvent]:
        if not self.api_key:
            return []
        out: list[EarningsEvent] = []
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get("https://finnhub.io/api/v1/calendar/earnings",
                                     params={"from": start.isoformat(), "to": end.isoformat(), "token": self.api_key})
                r.raise_for_status()
                for row in (r.json() or {}).get("earningsCalendar", []):
                    sym = (row.get("symbol") or "").upper()
                    if not sym:
                        continue
                    rd = date.fromisoformat(str(row.get("date"))[:10])
                    out.append(EarningsEvent(sym, rd, _norm_timing(row.get("hour")), "", None, row.get("epsEstimate"), False, [self.name]))
        except Exception as e:  # noqa: BLE001
            log.warning("finnhub calendar failed: %s", e)
        return out


class YFinanceCalendar:
    """Yahoo's calendar via yfinance.Calendars. Column names vary by version, so parse defensively."""

    name = "yfinance"

    async def fetch(self, start: date, end: date) -> list[EarningsEvent]:
        def _run():
            import yfinance as yf

            cal = yf.Calendars(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat())
            df = cal.get_earnings_calendar(market_cap=1e9, filter_most_active=False, limit=1000)
            return df

        try:
            df = await asyncio.to_thread(_run)
        except Exception as e:  # noqa: BLE001
            log.warning("yfinance calendar failed: %s", e)
            return []
        out: list[EarningsEvent] = []
        if df is None or len(df) == 0:
            return out
        cols = {c.lower(): c for c in df.columns}

        def col(*names):
            for n in names:
                for lc, orig in cols.items():
                    if n in lc:
                        return orig
            return None

        c_sym = col("symbol", "ticker")
        c_date = col("date", "earnings date", "startdatetime")
        c_time = col("call time", "time", "timing", "hour")
        c_cap = col("market cap", "marketcap")
        c_name = col("company", "name")
        c_eps = col("eps estimate", "epsestimate", "estimate")
        idx_is_sym = c_sym is None and df.index.dtype == object
        for i, row in df.iterrows():
            sym = str(row[c_sym] if c_sym else i).upper().strip()
            if not sym or len(sym) > 6:
                continue
            raw_d = row[c_date] if c_date else (i if hasattr(i, "year") else None)
            try:
                rd = raw_d.date() if hasattr(raw_d, "date") else date.fromisoformat(str(raw_d)[:10])
            except Exception:  # noqa: BLE001
                continue
            if not (start <= rd <= end):
                continue
            out.append(EarningsEvent(
                sym, rd, _norm_timing(str(row[c_time]) if c_time and row[c_time] is not None else None),
                str(row[c_name]) if c_name else "", _money_to_float(row[c_cap]) if c_cap else None,
                _money_to_float(row[c_eps]) if c_eps else None, False, [self.name],
            ))
        return out


def merge_events(groups: list[list[EarningsEvent]]) -> list[EarningsEvent]:
    """Merge events from several sources. Timing: agree -> that; disagree -> prefer nasdaq, else unknown.
    Market cap: max available. Sources: union. Company name: first non-empty."""
    by_key: dict[str, list[EarningsEvent]] = {}
    for g in groups:
        for e in g:
            by_key.setdefault(e.key, []).append(e)
    merged: list[EarningsEvent] = []
    for key, items in by_key.items():
        timings = {e.timing for e in items if e.timing != "unknown"}
        n_timings = len(timings)
        if n_timings == 1:
            timing = next(iter(timings))
        elif n_timings > 1:
            nas = [e.timing for e in items if "nasdaq" in e.sources and e.timing != "unknown"]
            timing = nas[0] if nas else "unknown"
        else:
            timing = "unknown"
        caps = [e.market_cap for e in items if e.market_cap]
        eps = [e.eps_estimate for e in items if e.eps_estimate is not None]
        names = [e.company for e in items if e.company]
        srcs = sorted({s for e in items for s in e.sources})
        merged.append(EarningsEvent(items[0].symbol, items[0].report_date, timing, names[0] if names else "",
                                    max(caps) if caps else None, eps[0] if eps else None,
                                    any(e.confirmed for e in items) or (n_timings == 1 and len(srcs) >= 2), srcs))
    merged.sort(key=lambda e: (e.report_date, -(e.market_cap or 0), e.symbol))
    return merged


async def fetch_all(sources: list, start: date, end: date) -> list[EarningsEvent]:
    results = await asyncio.gather(*(s.fetch(start, end) for s in sources), return_exceptions=True)
    groups = []
    for s, r in zip(sources, results):
        if isinstance(r, Exception):
            log.warning("calendar source %s raised: %s", getattr(s, "name", s), r)
        else:
            log.info("calendar source %s: %d events", getattr(s, "name", s), len(r))
            groups.append(r)
    return merge_events(groups)
