"""Agent system: a shared Claude client plus three roles that run inside the daily workflow.

- CatalystResearcher (07:45): server-side web search to build/confirm the catalyst calendar.
- RiskOfficer (15:20): reviews the sized plan; may approve, reduce or veto, never increase.
- OpsAgent (16:05): daily journal, issues and proposals for the human.
The TradeThesis analyst lives in earnings_bot.analysis.llm and runs at the 12:30 scan.
"""
from .base import ClaudeAgentClient
from .ops import OpsAgent
from .research import CatalystResearcher, merge_research
from .risk_officer import RiskOfficer

__all__ = ["ClaudeAgentClient", "CatalystResearcher", "merge_research", "RiskOfficer", "OpsAgent"]
