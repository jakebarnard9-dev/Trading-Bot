from datetime import date
from types import SimpleNamespace

import anthropic
import httpx

from earnings_bot.agents.base import ClaudeAgentClient, web_tools
from earnings_bot.agents.ops import OpsAgent
from earnings_bot.agents.research import CatalystResearcher, merge_research
from earnings_bot.agents.risk_officer import RiskOfficer, apply_review
from earnings_bot.models import (
    Candidate, CatalystEvent, EarningsEvent, Leg, OpsReport, OptionKey, PlanDecision, PlanReview, ResearchReport, StructurePlan,
)


def test_merge_research_rules():
    known = [EarningsEvent("TTWO", date(2026, 11, 9), "amc", "", 3e10, None, True, ["nasdaq"])]
    rep = ResearchReport(events=[
        CatalystEvent(symbol="xyz", date="2026-11-12", timing="amc", kind="fda", description="PDUFA", expected_abs_move_pct=35, confidence=0.9, source_url="https://fda.gov/x"),
        CatalystEvent(symbol="LOWC", date="2026-11-12", timing="bmo", kind="fda", description="low confidence", expected_abs_move_pct=20, confidence=0.3),
        CatalystEvent(symbol="OUT", date="2026-12-01", timing="bmo", kind="product", description="outside window", expected_abs_move_pct=10, confidence=0.9),
        CatalystEvent(symbol="BAD SYM", date="2026-11-12", timing="bmo", kind="other", description="bad symbol", expected_abs_move_pct=10, confidence=0.9),
        CatalystEvent(symbol="UNK", date="2026-11-12", timing="unknown", kind="earnings", description="earnings, time unknown", expected_abs_move_pct=8, confidence=0.8),
    ], timing_corrections=[
        CatalystEvent(symbol="TTWO", date="2026-11-09", timing="bmo", kind="earnings", description="IR page says before the open", expected_abs_move_pct=7, confidence=0.95, source_url="https://ir"),
    ], notes="n")
    new, corrected = merge_research(rep, date(2026, 11, 9), date(2026, 11, 16), known)
    assert [e.symbol for e in new] == ["XYZ"] and new[0].kind == "fda" and new[0].confirmed and new[0].expected_move_pct == 35 and new[0].confidence == 0.9
    assert corrected and corrected[0].symbol == "TTWO" and corrected[0].timing == "bmo" and "research" in corrected[0].sources and corrected[0].expected_move_pct == 7
    assert merge_research(None, date(2026, 11, 9), date(2026, 11, 16), known) == ([], [])


def _cand(sym, units, cost):
    ev = EarningsEvent(sym, date(2026, 11, 12), "amc", sym, 1e10, None, True, ["nasdaq"])
    c = Candidate(ev)
    k = OptionKey(sym, date(2026, 11, 13), 100.0, "C")
    c.plan = StructurePlan(sym, "call", [Leg(k, 1, 1.0, 1.1, 1.05)], 1.1, units=units, est_total_cost=cost)
    return c


def test_apply_review_only_reduces():
    a, b, c = _cand("A", 3, 330.0), _cand("B", 2, 220.0), _cand("C", 1, 110.0)
    review = PlanReview(decisions=[
        PlanDecision(symbol="a", action="reduce", units=5, reason="bigger"),
        PlanDecision(symbol="B", action="reduce", units=1, reason="duplicate exposure"),
        PlanDecision(symbol="C", action="veto", reason="event moved"),
        PlanDecision(symbol="ZZZ", action="veto", reason="unknown symbol"),
    ], overall_note="ok")
    apply_review(review, [a, b, c])
    assert a.plan.units == 3 and a.skip_reason is None                      # never increases
    assert b.plan.units == 1 and abs(b.plan.est_total_cost - 110.0) < 1e-9
    assert c.skip_reason.startswith("risk officer veto")
    assert apply_review(None, [a]) == []


async def test_client_research_handles_pause_turn_and_errors(monkeypatch):
    cl = ClaudeAgentClient("sk-test")
    calls = []

    async def create(**kw):
        calls.append(kw)
        if len(calls) == 1:
            return SimpleNamespace(stop_reason="pause_turn", content=[SimpleNamespace(type="server_tool_use"), SimpleNamespace(type="text", text="partial")])
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="final notes")])

    monkeypatch.setattr(cl.client.messages, "create", create)
    out = await cl.research("sys", "user", 3, 1)
    assert out == "partial\nfinal notes" and len(calls) == 2
    assert calls[0]["tools"][0]["type"] == "web_search_20260209" and calls[1]["messages"][-1]["role"] == "assistant"

    async def boom(**kw):
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))

    monkeypatch.setattr(cl.client.messages, "create", boom)
    assert await cl.research("sys", "user") is None
    assert web_tools(3, 0) == [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
    assert ClaudeAgentClient("").enabled is False


class FakeClient:
    enabled = True

    def __init__(self, notes="NOTES", report=None, review=None, ops=None):
        self.notes, self.report, self.review, self.ops = notes, report, review, ops
        self.last_user = ""

    async def research(self, system, user, *a, **k):
        self.last_user = user
        return self.notes

    async def parse(self, system, user, schema, **k):
        return {ResearchReport: self.report, PlanReview: self.review, OpsReport: self.ops}.get(schema)


async def test_researcher_ops_and_officer_with_fake_client(tmp_path):
    rep = ResearchReport(events=[], timing_corrections=[], notes="nothing found")
    fc = FakeClient(report=rep, ops=OpsReport(summary="quiet day", issues=["i1"], proposals=["p1"]))
    r = CatalystResearcher(fc, 5, 2)
    out = await r.run(date(2026, 11, 9), date(2026, 11, 16), [EarningsEvent("X", date(2026, 11, 10), "bmo")])
    assert out is rep and "X 2026-11-10 timing=bmo" in fc.last_user
    fc.notes = None
    assert await r.run(date(2026, 11, 9), date(2026, 11, 16), []) is None
    ops = OpsAgent(fc, tmp_path / "journal")
    rep2 = await ops.daily(date(2026, 11, 9), {"a": 1})
    assert rep2.summary == "quiet day" and "p1" in (tmp_path / "journal" / "2026-11-09.md").read_text()
    assert await RiskOfficer(fc).review([], {}) is None
