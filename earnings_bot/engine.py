"""The orchestrator: a tick-driven state machine that refreshes the calendar, scans and scores candidates,
asks the analyst, sizes under hard risk limits, enters near the close, and manages exits on the reaction day."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Optional

from .analysis.scoring import final_score, liquidity_ok, liquidity_quality, quant_score
from .clock import ET, at, is_trading_day, market_phase, next_trading_day, parse_hhmm, reaction_and_entry_dates
from .config import Config
from .data.calendar_sources import fetch_all
from .data.earnings_history import compute_moves, history_stats, price_features
from .models import (
    Candidate,
    EarningsEvent,
    Fill,
    Leg,
    MacroEvent,
    OptionKey,
    Position,
    PositionLeg,
    StructurePlan,
    TradeThesis,
)
from .pricing import implied_move, years_until
from .strategy.exits import apply_fill_to_position, book_expired_leg, evaluate_exits, position_value
from .strategy.macro import as_pseudo_event, macro_edge_ok, macro_events_for_entry
from .strategy.structures import choose_structure, pick_expiry, skew_snapshot

log = logging.getLogger(__name__)


def candidate_payload(c: Candidate) -> dict:
    return {
        "event": asdict(c.event), "play_type": c.play_type, "spot": c.spot, "expiry": c.expiry, "reaction_date": c.reaction_date,
        "implied_move_pct": c.implied_move_pct, "atm_strike": c.atm_strike, "straddle_mid": c.straddle_mid,
        "hist_stats": c.hist_stats, "liquidity": c.liquidity, "catalyst": c.catalyst, "profile": c.profile,
        "headlines": [{"title": h.title, "source": h.source, "published": h.published} for h in c.headlines[:25]],
        "thesis": c.thesis.model_dump() if c.thesis else None, "quant_score": c.quant_score, "score": c.score,
        "score_breakdown": c.score_breakdown, "skip_reason": c.skip_reason,
        "plan": {"kind": c.plan.kind, "legs": [{"key": str(l.key), "occ": l.key.occ(), "qty": l.qty, "bid": l.ref_bid, "ask": l.ref_ask, "delta": l.delta} for l in c.plan.legs],
                 "unit_cost": c.plan.unit_cost, "units": c.plan.units, "est_total_cost": c.plan.est_total_cost, "sim": c.plan.sim, "rationale": c.plan.rationale} if c.plan else None,
    }


class Engine:
    def __init__(self, cfg: Config, store, clock, broker, md, calendar_sources, news, analyst, notifier, risk, executor, macro_events=None):
        self.cfg = cfg
        self.store = store
        self.clock = clock
        self.broker = broker
        self.md = md
        self.calendar_sources = calendar_sources
        self.news = news
        self.analyst = analyst
        self.notifier = notifier
        self.risk = risk
        self.executor = executor
        self.macro_events = macro_events or []
        self._candidates: dict[date, list[Candidate]] = {}
        self._final: dict[date, list[Candidate]] = {}
        self._tick_lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle
    async def notify(self, text: str, level: str = "info") -> None:
        try:
            await self.notifier.send(text, level)
        except Exception as e:  # noqa: BLE001
            log.warning("notify failed: %s", e)

    async def start(self) -> None:
        await self.broker.connect()
        acct = await self.broker.account()
        await self.reconcile()
        now = self.clock.now()
        self.store.snapshot_equity(now, acct.equity, acct.cash, "start")
        r = self.cfg.risk
        await self.notify(
            f"started | mode={self.cfg.mode} broker={self.cfg.broker} equity=${acct.equity:,.0f} cash=${acct.cash:,.0f} | "
            f"profile={r.profile} per_trade={r.per_trade_pct:.0%} max_deployed={r.max_deployed_pct:.0%} max_open={r.max_open_positions} "
            f"daily_loss_limit={r.daily_loss_limit_pct:.0%} floor=${self.risk.equity_floor():,.0f}"
            + (f" | HALTED: {self.risk.halt_reason()}" if self.risk.halt_reason() else "")
        )

    async def stop(self) -> None:
        await self.broker.close()

    # ------------------------------------------------------------------ tick state machine
    def _phases(self, d: date) -> dict:
        return self.store.get(f"phases:{d.isoformat()}", {})

    def _mark(self, d: date, phase: str) -> None:
        p = self._phases(d)
        p[phase] = self.clock.now().isoformat()
        self.store.set(f"phases:{d.isoformat()}", p)

    def next_sleep_seconds(self, now: datetime) -> float:
        if market_phase(now) == "open":
            return self.cfg.schedule.manage_poll_seconds_open if now.time() < parse_hhmm("11:30") else self.cfg.schedule.manage_poll_seconds_late
        return 60.0

    async def tick(self) -> None:
        async with self._tick_lock:
            now = self.clock.now()
            d = now.date()
            if not is_trading_day(d):
                return
            sc = self.cfg.schedule
            t = now.time()
            try:
                if "dayopen" not in self._phases(d):
                    acct = await self.broker.account()
                    self.store.start_day(d, acct.equity)   # day-start equity for the daily loss limit and the streak counter
                    self.store.snapshot_equity(now, acct.equity, acct.cash, "dayopen")
                    self.risk.check_floor(acct.equity, now)
                    self._mark(d, "dayopen")
                if t >= parse_hhmm(sc.calendar_refresh) and "refresh" not in self._phases(d):
                    await self.refresh_calendar(d)
                    self._mark(d, "refresh")
                if market_phase(now) == "open":
                    await self.manage_positions(now)
                if parse_hhmm(sc.scan) <= t < parse_hhmm(sc.entry_end) and "scan" not in self._phases(d):
                    await self.scan_and_analyze(d)
                    self._mark(d, "scan")
                if parse_hhmm(sc.final_pricing) <= t < parse_hhmm(sc.entry_end) and "scan" in self._phases(d) and "final" not in self._phases(d):
                    await self.finalize_plan(d)
                    self._mark(d, "final")
                if parse_hhmm(sc.entry_start) <= t < parse_hhmm(sc.entry_end) and "final" in self._phases(d) and "entries" not in self._phases(d):
                    self._mark(d, "entries")  # mark first so a crash mid-entry never double-enters
                    await self.enter_positions(d)
                if t >= parse_hhmm(sc.eod_report) and "eod" not in self._phases(d):
                    await self.end_of_day(d)
                    self._mark(d, "eod")
            except Exception as e:  # noqa: BLE001
                log.exception("tick failed")
                await self.notify(f"ERROR in tick: {e!r}", "error")

    # ------------------------------------------------------------------ calendar
    async def refresh_calendar(self, d: date) -> None:
        events = await fetch_all(self.calendar_sources, d, d + timedelta(days=10))
        events = [e for e in events if e.symbol.isascii() and 1 <= len(e.symbol) <= 6]
        self.store.upsert_events(events)
        todays = self.events_for_entry(d)
        await self.notify(f"calendar refreshed: {len(events)} events over the next 10 days; {len(todays)} candidates enter today: "
                          + ", ".join(f"{e.symbol}({e.timing})" for e in todays[:25]))

    def events_for_entry(self, d: date) -> list[EarningsEvent]:
        u = self.cfg.universe
        out = []
        for e in self.store.events_between(d, d + timedelta(days=6)):
            if u.allowlist and e.symbol not in u.allowlist:
                continue
            if e.symbol in u.blocklist:
                continue
            if e.timing == "unknown" and not u.include_unknown_timing:
                continue
            if e.market_cap is not None and e.market_cap < u.min_market_cap:
                continue
            entry, _ = reaction_and_entry_dates(e.report_date, e.timing)
            if entry == d:
                out.append(e)
        out.sort(key=lambda e: -(e.market_cap or 0))
        return out

    # ------------------------------------------------------------------ candidate construction
    async def build_candidate(self, ev: EarningsEvent, play_type: str = "earnings", macro_event=None) -> Candidate:
        c = Candidate(ev, play_type=play_type)
        sym = ev.symbol
        try:
            _, c.reaction_date = reaction_and_entry_dates(ev.report_date, ev.timing)
            c.profile = await self.md.profile(sym)
            mcap = c.profile.get("market_cap") or ev.market_cap
            if play_type == "earnings" and mcap and mcap < self.cfg.universe.min_market_cap:
                c.skip_reason = f"market cap {mcap / 1e9:.1f}B below minimum"
                return c
            spot = await self.md.spot(sym)
            if not spot or spot <= 0:
                c.skip_reason = "no spot price"
                return c
            c.expiry = pick_expiry(await self.md.expiries(sym), c.reaction_date, self.cfg.options.max_days_to_expiry_after_reaction)
            if c.expiry is None:
                c.skip_reason = "no listed expiry within a week of the reaction"
                return c
            chain = await self.md.chain(sym, c.expiry)
            if not chain or not chain.calls or not chain.puts:
                c.skip_reason = "empty option chain"
                return c
            if chain.spot <= 0:
                chain.spot = spot
            c.spot = chain.spot
            c.chain = chain
            im = implied_move(chain)
            if im is None:
                c.skip_reason = "could not compute implied move (no two-sided ATM market)"
                return c
            c.implied_move_pct, c.atm_strike, c.straddle_mid = im.pct, im.atm_strike, im.straddle_mid
            atm = [q for q in (chain.call(im.atm_strike), chain.put(im.atm_strike)) if q]
            for q in atm:
                ok, why = liquidity_ok(q, self.cfg.options)
                if not ok:
                    c.skip_reason = f"ATM {q.key.right} illiquid: {why}"
                    return c
            T = max(years_until(c.expiry, self.clock.now()), 1e-6)
            c.liquidity = {"quality": round(liquidity_quality(atm, self.cfg.options), 3), "atm_iv": im.atm_iv, "skew": skew_snapshot(chain, c.spot, T)}
            bars = await self.md.daily_bars(sym)
            if play_type == "earnings":
                past = await self.md.past_earnings_dates(sym, self.cfg.scoring.lookback_earnings + 4)
                c.history = compute_moves(bars, past, ev.timing, self.cfg.scoring.lookback_earnings)
                c.hist_stats = history_stats(c.history, im.pct)
            c.catalyst = price_features(bars)
            if play_type == "earnings":
                c.catalyst["estimates"] = await self.md.estimates(sym)
                c.catalyst["peers"] = self._peer_reactions(c.profile.get("sector"))
                c.headlines = await self.news.headlines(sym, c.profile.get("name") or ev.company)
            else:
                c.catalyst["macro_event"] = asdict(macro_event) if macro_event else {}
        except Exception as e:  # noqa: BLE001
            log.exception("build_candidate %s", sym)
            c.skip_reason = f"data error: {e!r}"
        return c

    def _peer_reactions(self, sector: Optional[str]) -> str:
        if not sector:
            return "n/a"
        rows = []
        for p in self.store.all_positions(60):
            if p.status == "closed" and p.meta.get("sector") == sector and p.closed_at and (self.clock.now() - p.closed_at.astimezone(ET)).days <= 21:
                rows.append(f"{p.symbol}: realized move {p.meta.get('realized_move_pct', '?')}% vs implied {p.meta.get('implied_move_pct', '?')}%")
        return "; ".join(rows) if rows else "n/a"

    # ------------------------------------------------------------------ scan / analyze
    def _cached_thesis(self, d: date, symbol: str) -> Optional[TradeThesis]:
        for row in self.store.candidates_for(d):
            if row["symbol"] == symbol and row["payload"].get("thesis"):
                try:
                    return TradeThesis(**row["payload"]["thesis"])
                except Exception:  # noqa: BLE001
                    return None
        return None

    async def scan_and_analyze(self, d: date) -> list[Candidate]:
        now = self.clock.now()
        events = self.events_for_entry(d)
        sem = asyncio.Semaphore(4)

        async def build(ev, play_type="earnings", me=None):
            async with sem:
                return await self.build_candidate(ev, play_type, me)

        tasks = [build(ev) for ev in events]
        if self.cfg.macro.enabled and self.macro_events:
            for me in macro_events_for_entry(self.macro_events, d):
                for sym in self.cfg.macro.symbols:
                    tasks.append(build(as_pseudo_event(me, sym), "macro", me))
        cands: list[Candidate] = [c for c in await asyncio.gather(*tasks) if c is not None]

        # quant pre-screen
        for c in cands:
            if c.skip_reason:
                continue
            if c.play_type == "macro":
                me = c.catalyst.get("macro_event") or {}
                ok, why = macro_edge_ok(c.implied_move_pct, MacroEvent(**me)) if me else (False, "no macro event")
                c.quant_score = 70.0 if ok else 0.0
                c.score_breakdown = {"macro": why}
                if not ok:
                    c.skip_reason = f"macro: {why}"
                continue
            c.quant_score, c.score_breakdown = quant_score(c, self.cfg.scoring)
        ranked = sorted([c for c in cands if not c.skip_reason and c.play_type == "earnings"], key=lambda c: c.quant_score, reverse=True)

        # analyst on the top names only (cached per day so restarts don't re-spend)
        floor = self.cfg.scoring.trade_threshold_without_llm - 25
        to_analyze = []
        for c in ranked[:8]:
            if c.quant_score < floor:
                break
            c.thesis = self._cached_thesis(d, c.symbol)
            if c.thesis is None and getattr(self.analyst, "enabled", False):
                to_analyze.append(c)
        if to_analyze:
            llm_sem = asyncio.Semaphore(3)

            async def analyze(c):
                async with llm_sem:
                    c.thesis = await self.analyst.analyze(c)

            await asyncio.gather(*(analyze(c) for c in to_analyze), return_exceptions=True)

        # final scores, thresholds, structures
        for c in cands:
            if c.skip_reason:
                continue
            if c.play_type == "earnings":
                c.score, c.score_breakdown = final_score(c, self.cfg.scoring)
                threshold = self.cfg.scoring.trade_threshold if c.thesis else self.cfg.scoring.trade_threshold_without_llm
                if c.score < threshold:
                    c.skip_reason = f"score {c.score:.0f} < {threshold:.0f}" + (" (no analyst thesis -> higher bar)" if not c.thesis else "")
                    continue
            else:
                c.score = c.quant_score
            c.plan = choose_structure(c, c.chain, self.cfg, now) if c.chain else None
            if c.plan is None:
                c.skip_reason = "no structure with positive expected edge after IV crush"

        tradeable = sorted([c for c in cands if not c.skip_reason and c.plan], key=lambda c: c.score, reverse=True)
        selected = tradeable[: self.cfg.universe.max_new_positions_per_day]
        self._candidates[d] = selected
        for c in cands:
            self.store.save_candidate(d, c.symbol, c.play_type, c.score, c.quant_score, candidate_payload(c))
        skipped = [c for c in cands if c.skip_reason]
        lines = [f"scan {d}: {len(events)} earnings events + {len(cands) - len(events)} macro legs -> {len(selected)} selected"]
        for c in selected:
            lines.append(f"  {c.symbol} {c.play_type} score={c.score:.0f} implied={c.implied_move_pct:.1f}% hist_median={c.hist_stats.get('median_abs', 0):.1f}% "
                         f"-> {c.plan.kind} {' + '.join(str(l.key) for l in c.plan.legs)} unit=${c.plan.unit_cost * 100:,.0f} ER={c.plan.sim.get('expected_return', 0):+.0%}"
                         + (f" | {c.thesis.direction} conf={c.thesis.direction_confidence:.2f}" if c.thesis else ""))
        for c in skipped[:12]:
            lines.append(f"  skip {c.symbol}: {c.skip_reason}")
        await self.notify("\n".join(lines))
        return selected

    # ------------------------------------------------------------------ finalize sizing with live quotes
    async def finalize_plan(self, d: date) -> list[Candidate]:
        selected = self._candidates.get(d)
        if selected is None:
            selected = await self.scan_and_analyze(d)
        now = self.clock.now()
        acct = await self.broker.account()
        open_positions = self.store.open_positions()
        ok, why = self.risk.can_open(acct, open_positions, now)
        if not ok:
            await self.notify(f"no entries today: {why}", "warn")
            self._final[d] = []
            return []
        open_sectors = [p.meta.get("sector", "Unknown") for p in open_positions]
        final: list[Candidate] = []
        planned = 0.0
        for c in selected:
            if len(open_positions) + len(final) >= self.cfg.risk.max_open_positions:
                c.skip_reason = "max open positions"
                continue
            sector = c.profile.get("sector", "Unknown")
            if c.play_type == "earnings" and not self.risk.sector_ok(sector, open_sectors):
                c.skip_reason = f"sector cap ({sector})"
                continue
            try:
                quotes = await self.broker.option_quotes([l.key for l in c.plan.legs])
            except Exception as e:  # noqa: BLE001
                c.skip_reason = f"live quote failed: {e!r}"
                continue
            legs: list[Leg] = []
            bad = None
            for l in c.plan.legs:
                q = quotes.get(l.key)
                if not q or not q.has_market:
                    bad = f"no live market for {l.key}"
                    break
                lq, why = liquidity_ok(q, self.cfg.options)
                if not lq and q.spread_pct > self.cfg.options.max_spread_pct_cheap:
                    bad = f"{l.key}: {why}"
                    break
                legs.append(Leg(l.key, l.qty, q.bid, q.ask, q.mid, q.iv or l.iv, q.delta if q.delta is not None else l.delta))
            if bad:
                c.skip_reason = bad
                continue
            c.plan.legs = legs
            c.plan.unit_cost = sum(l.ref_ask * l.qty for l in legs)
            unit_dollars = c.plan.unit_cost * 100 * 1.05 + self.cfg.risk.commission_per_contract * sum(l.qty for l in legs)
            factor = self.cfg.macro.budget_pct_of_per_trade if c.play_type == "macro" else 1.0
            units, detail = self.risk.size_units(unit_dollars, acct, open_positions, factor, planned)
            if units < 1:
                c.skip_reason = f"one unit costs ${unit_dollars:,.0f} > budget ${detail['cap']:,.0f}"
                continue
            c.plan.units = units
            c.plan.est_total_cost = units * unit_dollars
            planned += c.plan.est_total_cost
            open_sectors.append(sector)
            final.append(c)
        self._final[d] = final
        for c in selected:
            self.store.save_candidate(d, c.symbol, c.play_type, c.score, c.quant_score, candidate_payload(c))
        lines = [f"final plan {d} (equity ${acct.equity:,.0f}, cash ${acct.cash:,.0f}):"]
        for c in final:
            lines.append(f"  BUY {c.plan.units}x {c.plan.kind} {c.symbol}: " + " + ".join(f"{l.key} @<= {l.ref_ask:.2f}" for l in c.plan.legs) + f" ~ ${c.plan.est_total_cost:,.0f}")
        for c in selected:
            if c.skip_reason:
                lines.append(f"  skip {c.symbol}: {c.skip_reason}")
        await self.notify("\n".join(lines))
        return final

    # ------------------------------------------------------------------ entries
    async def enter_positions(self, d: date) -> list[Position]:
        final = self._final.get(d)
        if final is None:
            final = await self.finalize_plan(d)
        if not final:
            return []
        deadline = at(d, self.cfg.schedule.entry_end)
        out: list[Position] = []

        async def one(c: Candidate) -> Optional[Position]:
            pid = f"{c.symbol}-{d.isoformat()}-{c.plan.kind}"
            if self.store.position(pid):
                return None
            fills = await self.executor.buy_structure(pid, c.plan, deadline)
            if not fills:
                await self.notify(f"{c.symbol}: no fills before {self.cfg.schedule.entry_end}, nothing opened", "warn")
                return None
            legs = [PositionLeg(f.key, f.qty, f.price + f.commission / (100 * f.qty), f.qty) for f in fills.values()]  # per-share cost incl. commission
            kind = c.plan.kind
            notes = []
            if len(fills) < len(c.plan.legs):
                kind = "call" if legs[0].key.right == "C" else "put"
                notes.append(f"one-legged fill of planned {c.plan.kind}")
            cost = sum(f.price * 100 * f.qty + f.commission for f in fills.values())
            pos = Position(pid, c.symbol, c.play_type, kind, legs, self.clock.now(), c.reaction_date, c.expiry, cost, notes=notes,
                           meta={"sector": c.profile.get("sector", "Unknown"), "implied_move_pct": c.implied_move_pct, "spot_at_entry": c.spot,
                                 "score": c.score, "thesis": c.thesis.model_dump() if c.thesis else None, "sim": c.plan.sim})
            self.store.save_position(pos)
            self.store.log_decision("entry", c.symbol, {"position": pid, "cost": cost, "legs": [asdict(l) for l in legs]})
            await self.notify(f"OPENED {pid}: " + ", ".join(f"{l.qty}x {l.key} @ {l.avg_cost:.2f}" for l in legs) + f" | cost ${cost:,.0f}" + (f" | {notes[0]}" if notes else ""))
            return pos

        res = await asyncio.gather(*(one(c) for c in final), return_exceptions=True)
        for r in res:
            if isinstance(r, Exception):
                log.error("entry failed: %s", r)
                await self.notify(f"entry error: {r!r}", "error")
            elif r:
                out.append(r)
        return out

    # ------------------------------------------------------------------ manage exits
    async def manage_positions(self, now: datetime) -> None:
        open_positions = self.store.open_positions()
        if not open_positions:
            return
        today = now.date()
        keys = [l.key for p in open_positions for l in p.open_legs()]
        try:
            quotes = await self.broker.option_quotes(keys)
        except Exception as e:  # noqa: BLE001
            log.warning("manage: quotes failed: %s", e)
            return
        for pos in open_positions:
            if pos.expiry < today and pos.open_legs():
                for l in pos.open_legs():
                    loss = book_expired_leg(pos, l)
                    pos.notes.append(f"{l.key} expired unsold, booked ${loss:,.0f}")
                    self.store.log_decision("exit", pos.symbol, {"position": pos.id, "reason": "expired", "qty": l.qty, "price": 0.0, "realized": loss}, at=now)
                pos.status, pos.closed_at = "closed", now
                self.store.save_position(pos)
                await self.notify(f"CLOSED {pos.id} (expired leg written off): total P/L ${pos.realized_pnl:,.0f} ({pos.realized_pnl / pos.cost_basis:+.0%})")
                continue
            actions = evaluate_exits(pos, quotes, now, self.cfg.exits, self.cfg.risk.commission_per_contract)
            if not actions:
                self.store.save_position(pos)  # persists high-water mark
                continue

            async def run_action(a):
                deadline = self.clock.now() + timedelta(seconds=120)
                return a, await self.executor.sell_leg(pos.id, a.leg_key, a.qty, deadline, a.urgency)

            results = await asyncio.gather(*(run_action(a) for a in actions), return_exceptions=True)
            for res in results:
                if isinstance(res, Exception):
                    log.error("exit action failed for %s: %s", pos.id, res)
                    continue
                a, fill = res
                if not fill:
                    continue
                realized = apply_fill_to_position(pos, a.leg_key, fill.qty, fill.price, fill.commission)
                if a.reason.startswith("take_half"):
                    pos.took_half = True
                pos.notes.append(f"{now.strftime('%H:%M')} sold {fill.qty}x {a.leg_key} @ {fill.price:.2f} ({a.reason}) P/L ${realized:,.0f}")
                if pos.opened_at.astimezone(ET).date() == today:
                    self.store.record_day_trade(today, pos.symbol, a.reason)
                self.store.log_decision("exit", pos.symbol, {"position": pos.id, "reason": a.reason, "qty": fill.qty, "price": fill.price, "realized": realized}, at=now)
                await self.notify(f"SOLD {fill.qty}x {a.leg_key} @ {fill.price:.2f} [{a.reason}] P/L ${realized:,.0f}")
            if not pos.open_legs():
                pos.status, pos.closed_at = "closed", now
                try:
                    spot_now = await self.broker.spot(pos.symbol)
                    if pos.meta.get("spot_at_entry"):
                        pos.meta["realized_move_pct"] = round((spot_now / pos.meta["spot_at_entry"] - 1) * 100, 2)
                except Exception:  # noqa: BLE001
                    pass
                await self.notify(f"CLOSED {pos.id}: P/L ${pos.realized_pnl:,.0f} ({pos.realized_pnl / pos.cost_basis:+.0%}) move {pos.meta.get('realized_move_pct', '?')}% vs implied {pos.meta.get('implied_move_pct')}%")
            self.store.save_position(pos)
        acct = await self.broker.account()
        self.risk.check_floor(acct.equity, now)

    # ------------------------------------------------------------------ end of day
    async def end_of_day(self, d: date) -> None:
        acct = await self.broker.account()
        now = self.clock.now()
        self.store.snapshot_equity(now, acct.equity, acct.cash, "eod")
        start = self.store.start_day(d, acct.equity)
        closed_today = [p for p in self.store.all_positions(200) if p.status == "closed" and p.closed_at and p.closed_at.astimezone(ET).date() == d]
        realized, n_exits = self.store.realized_on(d)
        pause = self.risk.after_close(d, acct.equity, realized, len(closed_today))
        opens = self.store.open_positions()
        nxt = next_trading_day(d)
        tomorrow = self.events_for_entry(nxt)
        msg = (f"EOD {d}: equity ${acct.equity:,.0f} ({acct.equity - start:+,.0f} vs day start) realized ${realized:,.0f} on {n_exits} exit fill(s), {len(closed_today)} position(s) closed | "
               f"open overnight: {', '.join(p.id for p in opens) or 'none'} | {nxt} entry candidates: {len(tomorrow)}")
        if pause:
            msg += f" | {pause}"
        if self.risk.halt_reason():
            msg += f" | HALTED: {self.risk.halt_reason()}"
        await self.notify(msg)

    # ------------------------------------------------------------------ reconcile / flatten / status
    async def reconcile(self) -> None:
        try:
            bpos = {p.key: p for p in await self.broker.positions()}
        except Exception as e:  # noqa: BLE001
            await self.notify(f"reconcile: could not read broker positions: {e!r}", "warn")
            return
        today = self.clock.today()
        tracked: set[OptionKey] = set()
        for pos in self.store.open_positions():
            for l in pos.open_legs():
                tracked.add(l.key)
                if l.key not in bpos:
                    if pos.expiry < today:
                        loss = book_expired_leg(pos, l)
                        pos.notes.append(f"reconcile: {l.key} expired/absent at broker, booked ${loss:,.0f}")
                    else:
                        pos.notes.append(f"reconcile: {l.key} missing at broker")
                        await self.notify(f"reconcile: {pos.id} leg {l.key} not found at broker; check manually", "warn")
            if not pos.open_legs():
                pos.status, pos.closed_at = "closed", self.clock.now()
            self.store.save_position(pos)
        untracked = [p for k, p in bpos.items() if k not in tracked]
        if untracked:
            adopted = self._adopt_orphan_fills(untracked)
            rest = [p for p in untracked if p.key not in adopted]
            if adopted:
                await self.notify(f"reconcile: re-attached {len(adopted)} leg(s) from fills recorded before a restart", "warn")
            if rest:
                await self.notify("reconcile: broker holds option positions this bot did not open (left untouched): " + ", ".join(f"{p.qty}x {p.key}" for p in rest), "warn")

    def _adopt_orphan_fills(self, untracked) -> set:
        """If the process died between a fill and the Position write, rebuild the position from the fills table."""
        adopted: set = set()
        by_pid: dict[str, list[dict]] = {}
        for p in untracked:
            for row in self.store._rows("SELECT * FROM fills WHERE occ=? AND side='BUY' ORDER BY at", (p.key.occ(),)):
                by_pid.setdefault(row["position_id"], []).append(row)
        for pid, rows in by_pid.items():
            if self.store.position(pid):
                continue
            legs: dict[OptionKey, PositionLeg] = {}
            cost = 0.0
            first_at = None
            for r in rows:
                key = OptionKey.from_occ(r["occ"])
                sold = sum(x["qty"] for x in self.store._rows("SELECT qty FROM fills WHERE occ=? AND side='SELL' AND position_id=?", (r["occ"], pid)))
                qty = int(r["qty"])
                cost += r["price"] * 100 * qty + (r["commission"] or 0.0)
                px = r["price"] + (r["commission"] or 0.0) / (100 * qty)
                at = datetime.fromisoformat(r["at"])
                first_at = at if first_at is None or at < first_at else first_at
                if key in legs:
                    l = legs[key]
                    l.avg_cost = (l.avg_cost * l.qty + px * qty) / (l.qty + qty)
                    l.qty += qty
                    l.remaining += qty
                else:
                    legs[key] = PositionLeg(key, qty, float(px), max(qty - sold, 0))
            if not legs:
                continue
            keys = list(legs)
            kind = "straddle" if len(keys) == 2 and keys[0].strike == keys[1].strike else "strangle" if len(keys) == 2 else ("call" if keys[0].right == "C" else "put")
            entry_day = first_at.astimezone(ET).date() if first_at else self.clock.today()
            reaction = next_trading_day(entry_day)
            pos = Position(pid, keys[0].symbol, "earnings", kind, list(legs.values()), first_at or self.clock.now(), reaction, keys[0].expiry, cost,
                           notes=["re-attached from fills after restart"], meta={"sector": "Unknown"})
            self.store.save_position(pos)
            adopted.update(keys)
        return adopted

    async def flatten_all(self, reason: str = "manual flatten") -> None:
        now = self.clock.now()
        for pos in self.store.open_positions():
            for l in pos.open_legs():
                fill = await self.executor.sell_leg(pos.id, l.key, l.remaining, now + timedelta(seconds=180), "high")
                if fill:
                    realized = apply_fill_to_position(pos, l.key, fill.qty, fill.price, fill.commission)
                    pos.notes.append(f"flatten: sold {fill.qty}x {l.key} @ {fill.price:.2f} P/L ${realized:,.0f}")
            if not pos.open_legs():
                pos.status, pos.closed_at = "closed", self.clock.now()
            self.store.save_position(pos)
            await self.notify(f"FLATTEN {pos.id} ({reason}): realized ${pos.realized_pnl:,.0f}, status {pos.status}")

    async def status(self) -> dict:
        acct = await self.broker.account()
        return {
            "now": self.clock.now().isoformat(), "mode": self.cfg.mode, "broker": self.cfg.broker,
            "equity": acct.equity, "cash": acct.cash, "halted": self.risk.halt_reason(), "paused_until": self.store.get("paused_until"),
            "open_positions": [asdict(p) for p in self.store.open_positions()],
            "recent_closed": [{"id": p.id, "pnl": p.realized_pnl, "closed_at": p.closed_at} for p in self.store.all_positions(10) if p.status == "closed"],
            "equity_series": self.store.equity_series(50), "decisions": self.store.decisions(20),
        }
