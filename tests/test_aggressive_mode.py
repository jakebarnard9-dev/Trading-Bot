import os
from datetime import date

from earnings_bot.analysis.scoring import quant_score
from earnings_bot.config import load_config
from earnings_bot.models import Candidate, EarningsEvent, Leg, OpsReport, OptionKey, PlanDecision, PlanReview, ResearchReport, CatalystEvent, StructurePlan
from earnings_bot.replay import run_replay
from earnings_bot.strategy.structures import select_by_objective

cfg = load_config("tests/fixture_config.yaml")


def _plan(kind, er, p_profit, p_triple):
    k = OptionKey("X", date(2026, 11, 13), 100.0, "C")
    return StructurePlan("X", kind, [Leg(k, 1, 1.0, 1.1, 1.05)], 1.1, sim={"expected_return": er, "p_profit": p_profit, "p_triple": p_triple})


def test_select_by_objective():
    results = [(0.05, _plan("straddle", 0.30, 0.60, 0.05)), (0.02, _plan("strangle", 0.90, 0.25, 0.30)), (-0.01, _plan("call", 1.50, 0.15, 0.40))]
    assert select_by_objective(results, "growth", 0.03, 0.2).kind == "straddle"
    assert select_by_objective(results, "aggressive", 0.03, 0.2).kind == "strangle"      # highest ER with p_profit >= 20%
    assert select_by_objective(results, "moonshot", 0.03, 0.2).kind == "strangle"        # call fails the p_profit floor
    assert select_by_objective(results, "moonshot", 0.03, 0.1).kind == "call"
    assert select_by_objective([(0.1, _plan("straddle", 0.01, 0.6, 0.1))], "aggressive", 0.03, 0.2) is None   # no edge -> no trade


def test_catalyst_quant_score_uses_research_estimate():
    ev = EarningsEvent("BIO", date(2026, 11, 13), "bmo", "Bio", 4e9, None, True, ["research"], kind="fda", expected_move_pct=30, confidence=0.9)
    c = Candidate(ev, play_type="catalyst", spot=30, implied_move_pct=7.0)
    c.hist_stats = {"n": 0, "research_expected_move_pct": 30.0, "research_confidence": 0.9}
    c.liquidity = {"quality": 0.8}
    hi, bd = quant_score(c, cfg.scoring)
    c.hist_stats["research_confidence"] = 0.0
    lo, _ = quant_score(c, cfg.scoring)
    assert hi > 55 and hi > lo + 15 and bd["n_hist"] == 0


def test_config_env_overrides(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("ibkr:\n  host: 127.0.0.1\n  port: 7496\n")
    monkeypatch.setenv("IBKR_HOST", "ib-gateway")
    monkeypatch.setenv("IBKR_PORT", "4003")
    c = load_config(tmp_path / "config.yaml", env_file=None)
    assert c.ibkr.host == "ib-gateway" and c.ibkr.port == 4003
    monkeypatch.delenv("IBKR_HOST")
    monkeypatch.delenv("IBKR_PORT")


class FakeResearcher:
    enabled = True

    async def run(self, today, end, known):
        return ResearchReport(events=[CatalystEvent(symbol="BIO", date="2026-11-13", timing="bmo", kind="fda", description="PDUFA decision",
                                                    expected_abs_move_pct=35, confidence=0.9, source_url="https://fda.gov")], notes="one PDUFA this week")


class FakeRiskOfficer:
    enabled = True

    def __init__(self):
        self.seen = []

    async def review(self, plan, context):
        self.seen.append((plan, context))
        decisions = [PlanDecision(symbol=p["symbol"], action="reduce" if p["units"] > 1 else "approve", units=1, reason="cap test") for p in plan]
        return PlanReview(decisions=decisions, overall_note="integrity ok")


class FakeOps:
    enabled = True

    async def daily(self, day, payload):
        assert "candidates" in payload and "decisions" in payload
        return OpsReport(summary=f"journal for {day}", issues=[], proposals=["none"])


async def test_replay_with_agents(tmp_path):
    ro = FakeRiskOfficer()
    r = await run_replay(gap_pct=9.0, post_iv=0.5, db_path=str(tmp_path / "r.sqlite"), researcher=FakeResearcher(), risk_officer=ro, ops=FakeOps())
    by = {c["symbol"]: c for c in r["candidates"]}
    assert by["BIO"]["play_type"] == "catalyst" and by["BIO"]["payload"]["skip_reason"] is None
    syms = {p.symbol for p in r["positions"]}
    assert {"GOOD", "BIO"} <= syms
    bio = next(p for p in r["positions"] if p.symbol == "BIO")
    assert bio.status == "closed" and bio.realized_pnl > 0 and all(l.qty == 1 for l in bio.legs)   # +30% PDUFA gap, officer capped at 1 unit
    assert ro.seen and ro.seen[0][1]["owner_instructions"].startswith("Aggressive mandate")
    kinds = {d["kind"] for d in r["store"].decisions(200)}
    assert {"research", "risk_officer", "entry", "exit"} <= kinds
    assert any("research: 1 catalyst event(s) added" in m for m in r["messages"]) and any("ops journal: journal for 2026-11-12" in m for m in r["messages"])
    assert abs((r["equity"] - 5000) - sum(p.realized_pnl for p in r["positions"])) < 1.0
