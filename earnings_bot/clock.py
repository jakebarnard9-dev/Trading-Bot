"""Eastern-time clock, trading calendar helpers, and an injectable fake clock for tests/replay."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")[:2]
    return time(int(h), int(m))


def at(d: date, hhmm: str) -> datetime:
    return datetime.combine(d, parse_hhmm(hhmm), tzinfo=ET)


class Clock:
    def now(self) -> datetime:
        return datetime.now(tz=ET)

    def today(self) -> date:
        return self.now().date()


class FakeClock(Clock):
    def __init__(self, start: datetime):
        self._now = start if start.tzinfo else start.replace(tzinfo=ET)

    def now(self) -> datetime:
        return self._now

    def set(self, dt: datetime) -> None:
        self._now = dt if dt.tzinfo else dt.replace(tzinfo=ET)

    def advance(self, **kw) -> None:
        self._now = self._now + timedelta(**kw)


@lru_cache(maxsize=1)
def _nyse():
    import pandas_market_calendars as pmc

    return pmc.get_calendar("NYSE")


@lru_cache(maxsize=64)
def _schedule(year: int):
    cal = _nyse()
    return cal.schedule(start_date=f"{year}-01-01", end_date=f"{year}-12-31")


def session_times(d: date) -> tuple[datetime, datetime] | None:
    """(open, close) in ET for a trading day, or None if the market is closed that day."""
    sched = _schedule(d.year)
    key = d.isoformat()
    if key not in sched.index.strftime("%Y-%m-%d"):
        return None
    row = sched.loc[key]
    return row["market_open"].tz_convert(ET).to_pydatetime(), row["market_close"].tz_convert(ET).to_pydatetime()


def is_trading_day(d: date) -> bool:
    return session_times(d) is not None


def next_trading_day(d: date, n: int = 1) -> date:
    cur = d
    while n > 0:
        cur = cur + timedelta(days=1)
        if is_trading_day(cur):
            n -= 1
    return cur


def prev_trading_day(d: date, n: int = 1) -> date:
    cur = d
    while n > 0:
        cur = cur - timedelta(days=1)
        if is_trading_day(cur):
            n -= 1
    return cur


def trading_days_between(a: date, b: date) -> int:
    """Number of trading sessions strictly after a and up to and including b."""
    if b <= a:
        return 0
    n, cur = 0, a
    while cur < b:
        cur += timedelta(days=1)
        if is_trading_day(cur):
            n += 1
    return n


def reaction_and_entry_dates(report_date: date, timing: str) -> tuple[date, date]:
    """Return (entry_date, reaction_date) for an earnings event.

    AMC: enter on the report date, reaction is the next session.
    BMO/unknown: enter the session before the report date, reaction is the report date.
    """
    if timing == "amc":
        entry = report_date if is_trading_day(report_date) else prev_trading_day(report_date)
        reaction = next_trading_day(entry)
    else:
        reaction = report_date if is_trading_day(report_date) else next_trading_day(report_date)
        entry = prev_trading_day(reaction)
    return entry, reaction


def market_phase(now: datetime) -> str:
    """'closed' | 'pre' | 'open' | 'post' for the given ET datetime."""
    st = session_times(now.date())
    if st is None:
        return "closed"
    o, c = st
    if now < o:
        return "pre"
    if now >= c:
        return "post"
    return "open"
