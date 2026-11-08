"""Ops agent: end-of-day journal with issues and proposals. Proposals are for the human; nothing is applied automatically."""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Optional

from ..models import OpsReport, _json_default
from .base import ClaudeAgentClient

log = logging.getLogger(__name__)

OPS_SYSTEM = """You are the operations lead of an automated options program. From the day's data, write a factual journal:
what was considered, what was traded and why, what was skipped and why, fills versus plan, exits and their reasons, P/L,
and any anomalies (errors, stale data, unfilled orders, reconcile warnings). List concrete issues the owner must fix.
Then list proposals: specific parameter or process changes with the evidence from today's data. Do not restate limits the
owner set on purpose. Be concise and specific; no generic advice."""


class OpsAgent:
    def __init__(self, client: ClaudeAgentClient, journal_dir: Path):
        self.client = client
        self.journal_dir = Path(journal_dir)
        self.enabled = client.enabled

    async def daily(self, day: date, payload: dict) -> Optional[OpsReport]:
        if not self.enabled:
            return None
        report = await self.client.parse(OPS_SYSTEM, json.dumps(payload, default=_json_default, indent=1), OpsReport)
        if report is not None:
            try:
                self.journal_dir.mkdir(parents=True, exist_ok=True)
                md = [f"# {day.isoformat()}", "", report.summary, ""]
                if report.issues:
                    md += ["## Issues", *[f"- {i}" for i in report.issues], ""]
                if report.proposals:
                    md += ["## Proposals (not applied)", *[f"- {p}" for p in report.proposals], ""]
                (self.journal_dir / f"{day.isoformat()}.md").write_text("\n".join(md))
            except Exception as e:  # noqa: BLE001
                log.warning("journal write failed: %s", e)
        return report
