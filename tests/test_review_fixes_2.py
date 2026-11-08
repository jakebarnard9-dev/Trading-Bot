import asyncio
import sqlite3
from datetime import date

from earnings_bot.agents.research import merge_research
from earnings_bot.agents.risk_officer import apply_review
from earnings_bot.config import load_config
from earnings_bot.models import CatalystEvent, EarningsEvent, PlanDecision, PlanReview, ResearchReport
from earnings_bot.replay import run_replay
from earnings_bot.state.store import Store
from tests.test_agents import _cand


def _ce(sym, d, timing, kind="earnings", conf=0.9, move=10.0, desc="x", url="https://ir"):
    return CatalystEvent(symbol=sym, date=d, timing=timing, kind=kind, description=desc, expected_abs_move_pct=move, confidence=conf, source_url=url)


def test_merge_matches_whole_window_and_keys_catalysts_by_kind():
    known = [EarningsEvent("FAR", date(2026, 11, 15), "amc", "Far Corp", 5e9, 1.0, False, ["nasdaq"]),      # 6 days out: still "known"
             EarningsEvent("DUAL", date(2026, 11, 12), "amc", "Dual Inc", 8e9, 0.5, True, ["nasdaq", "fmp"])]
    rep = ResearchReport(events=[_ce("FAR", "2026-11-15", "bmo", conf=0.8), _ce("DUAL", "2026-11-12", "bmo", kind="fda", move=40, conf=0.9)], notes="")
    new, corrected = merge_research(rep, date(2026, 11, 9), date(2026, 11, 16), known)
    assert [e.key for e in corrected] == ["FAR:2026-11-15"] and corrected[0].timing == "bmo" and corrected[0].company == "Far Corp"   # corrected in place, never re-created
    assert [e.key for e in new] == ["DUAL:2026-11-12:fda"] and known[1].timing == "amc" and known[1].expected_move_pct is None       # earnings row untouched


def test_merge_date_change_supersedes_old_row():
    known = [EarningsEvent("MOVE", date(2026, 11, 10), "amc", "Move Co", 3e9, None, False, ["nasdaq"])]
    rep = ResearchReport(events=[], timing_corrections=[_ce("MOVE", "2026-11-12", "amc", conf=0.95, desc="IR: Nov 12")], notes="")
    new, corrected = merge_research(rep, date(2026, 11, 9), date(2026, 11, 16), known)
    assert [e.key for e in new] == ["MOVE:2026-11-12"] and new[0].timing == "amc"
    assert corrected and corrected[0].key == "MOVE:2026-11-10" and corrected[0].timing == "unknown" and "superseded" in corrected[0].notes


def test_apply_review_units_none_is_a_veto():
    a = _cand("A", 3, 330.0)
    apply_review(PlanReview(decisions=[PlanDecision(symbol="A", action="reduce", units=None, reason="?")], overall_note=""), [a])
    assert a.skip_reason and "reduced to 0" in a.skip_reason


def test_store_upsert_merges_and_migrates(tmp_path):
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE events (key TEXT PRIMARY KEY, symbol TEXT, report_date TEXT, timing TEXT, company TEXT,
        market_cap REAL, eps_estimate REAL, confirmed INTEGER, sources TEXT, updated_at TEXT);
        INSERT INTO events VALUES ('OLD:2026-11-12','OLD','2026-11-12','amc','Old Co',1e9,1.0,1,'["nasdaq"]','x');""")
    con.commit(); con.close()
    st = Store(db)                                                     # migration adds kind / expected_move_pct / notes / confidence
    evs = st.events_between(date(2026, 11, 12), date(2026, 11, 12))
    assert evs[0].kind == "earnings" and evs[0].notes == "" and evs[0].company == "Old Co"
    st.upsert_events([EarningsEvent("OLD", date(2026, 11, 12), "bmo", "", None, None, False, ["research"], expected_move_pct=9.0, confidence=0.8, notes="n")])
    e = st.events_between(date(2026, 11, 12), date(2026, 11, 12))[0]
    assert e.timing == "bmo" and e.company == "Old Co" and e.market_cap == 1e9 and e.confirmed and sorted(e.sources) == ["nasdaq", "research"] and e.expected_move_pct == 9.0
    st.upsert_events([EarningsEvent("OLD", date(2026, 11, 12), "amc", "Old Co", 1e9, 1.0, True, ["nasdaq", "fmp"])])   # next morning's aggregator refresh
    e = st.events_between(date(2026, 11, 12), date(2026, 11, 12))[0]
    assert e.timing == "bmo" and sorted(e.sources) == ["fmp", "nasdaq", "research"]                                   # research timing survives
    st.upsert_events([EarningsEvent("OLD", date(2026, 11, 12), "bmo", "", None, None, False, ["research"], kind="fda")])
    assert len(st.events_between(date(2026, 11, 12), date(2026, 11, 12))) == 2                                          # separate key per kind


class SlowRiskOfficer:
    enabled = True

    async def review(self, plan, context):
        await asyncio.sleep(2.0)
        return PlanReview(decisions=[PlanDecision(symbol=p["symbol"], action="veto", reason="too late to matter") for p in plan], overall_note="")


async def test_slow_risk_officer_does_not_block_entries(tmp_path):
    cfg = load_config("tests/fixture_config.yaml")
    cfg.agents.risk_officer_timeout_seconds = 0.2
    r = await run_replay(cfg, gap_pct=9.0, post_iv=0.5, db_path=str(tmp_path / "r.sqlite"), risk_officer=SlowRiskOfficer())
    assert any("risk officer timed out" in m for m in r["messages"])
    assert r["opened"] and r["opened"][0].symbol == "GOOD"                                                          # plan proceeded as sized
