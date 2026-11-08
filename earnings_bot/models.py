"""Domain models shared across the bot."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Timing = Literal["bmo", "amc", "unknown"]
Right = Literal["C", "P"]
StructureKind = Literal["straddle", "strangle", "call", "put"]
PlayType = Literal["earnings", "macro", "catalyst"]
EventKind = Literal["earnings", "fda", "regulatory", "product", "index", "macro", "legal", "other"]


def _json_default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if hasattr(o, "__dict__"):
        return o.__dict__
    return str(o)


def to_json(obj) -> str:
    if hasattr(obj, "model_dump"):
        return json.dumps(obj.model_dump(mode="json"), default=_json_default)
    if hasattr(obj, "__dataclass_fields__"):
        return json.dumps(asdict(obj), default=_json_default)
    return json.dumps(obj, default=_json_default)


@dataclass
class EarningsEvent:
    symbol: str
    report_date: date
    timing: Timing = "unknown"
    company: str = ""
    market_cap: Optional[float] = None
    eps_estimate: Optional[float] = None
    confirmed: bool = False
    sources: list[str] = field(default_factory=list)
    kind: str = "earnings"                      # EventKind; anything but 'earnings' is a researched catalyst
    expected_move_pct: Optional[float] = None   # researcher's estimate of the absolute move (percent)
    confidence: Optional[float] = None          # researcher's confidence in date/timing (0-1)
    notes: str = ""

    @property
    def key(self) -> str:
        base = f"{self.symbol}:{self.report_date.isoformat()}"
        return base if (self.kind or "earnings") == "earnings" else f"{base}:{self.kind}"


@dataclass
class MacroEvent:
    date: date
    time: str
    name: str
    impact: str = "high"
    expected_move_pct: float = 0.9  # percent, e.g. 0.9 = 0.9%


@dataclass(frozen=True)
class OptionKey:
    symbol: str
    expiry: date
    strike: float
    right: str  # "C" | "P"

    def occ(self) -> str:
        """OCC option symbol, e.g. AAPL241220C00150000."""
        return f"{self.symbol}{self.expiry.strftime('%y%m%d')}{self.right}{int(round(self.strike * 1000)):08d}"

    @staticmethod
    def from_occ(occ: str) -> "OptionKey":
        # root is everything before the 6-digit date; strike is the last 8 digits
        body = occ.strip().upper()
        strike = int(body[-8:]) / 1000.0
        right = body[-9]
        yymmdd = body[-15:-9]
        symbol = body[:-15]
        return OptionKey(symbol, datetime.strptime(yymmdd, "%y%m%d").date(), strike, right)

    def __str__(self) -> str:
        return f"{self.symbol} {self.expiry.isoformat()} {self.strike:g}{self.right}"


@dataclass
class OptionQuote:
    key: OptionKey
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    volume: int = 0
    open_interest: int = 0
    iv: Optional[float] = None
    delta: Optional[float] = None
    asof: Optional[datetime] = None

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        if self.ask > 0:
            return self.ask
        return max(self.last, 0.0)

    @property
    def spread(self) -> float:
        return max(self.ask - self.bid, 0.0) if (self.bid > 0 and self.ask > 0) else float("inf")

    @property
    def spread_pct(self) -> float:
        m = self.mid
        return self.spread / m if m > 0 else float("inf")

    @property
    def has_market(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid


@dataclass
class OptionChain:
    symbol: str
    expiry: date
    spot: float
    calls: list[OptionQuote]
    puts: list[OptionQuote]
    asof: Optional[datetime] = None

    def strikes(self) -> list[float]:
        return sorted({q.key.strike for q in self.calls} | {q.key.strike for q in self.puts})

    def call(self, strike: float) -> Optional[OptionQuote]:
        return next((q for q in self.calls if abs(q.key.strike - strike) < 1e-6), None)

    def put(self, strike: float) -> Optional[OptionQuote]:
        return next((q for q in self.puts if abs(q.key.strike - strike) < 1e-6), None)


@dataclass
class EarningsMove:
    date: date
    close_before: float
    open_after: float
    close_after: float

    @property
    def gap_pct(self) -> float:
        return (self.open_after / self.close_before - 1.0) * 100.0

    @property
    def cc_pct(self) -> float:
        return (self.close_after / self.close_before - 1.0) * 100.0


@dataclass
class Headline:
    title: str
    source: str
    published: Optional[datetime] = None
    url: str = ""


class TradeThesis(BaseModel):
    """Structured output of the LLM analyst. The LLM never sizes positions."""

    symbol: str
    event_still_expected: bool = Field(description="False if headlines show the report already happened, was moved, or the stock is halted/being acquired")
    direction: Literal["bullish", "bearish", "neutral"]
    direction_confidence: float = Field(ge=0.0, le=1.0, description="0.5 = coin flip, 1.0 = certain")
    expected_abs_move_vs_implied: float = Field(ge=0.3, le=3.0, description="1.0 = move about the size the straddle implies; 1.3 = 30% larger")
    big_move_probability: float = Field(ge=0.0, le=1.0, description="Probability the absolute move exceeds the implied move")
    recommended_structure: Literal["straddle", "strangle", "call", "put", "no_trade"]
    key_catalysts: list[str] = Field(max_length=6)
    key_risks: list[str] = Field(max_length=6)
    data_quality: Literal["good", "mixed", "poor"]
    rationale: str = Field(max_length=900)


class CatalystEvent(BaseModel):
    """One researched scheduled event (output of the research agent)."""

    symbol: str = Field(max_length=8)
    date: str = Field(description="YYYY-MM-DD of the announcement / decision")
    timing: Literal["bmo", "amc", "unknown"]
    kind: EventKind
    description: str = Field(max_length=300)
    expected_abs_move_pct: float = Field(ge=0.0, le=100.0, description="typical absolute stock move on this kind of event for this name, percent")
    confidence: float = Field(ge=0.0, le=1.0, description="confidence the event happens on that date with that timing")
    source_url: str = ""


class ResearchReport(BaseModel):
    events: list[CatalystEvent] = Field(max_length=40)
    timing_corrections: list[CatalystEvent] = Field(default_factory=list, max_length=40, description="known earnings events whose timing sources contradict")
    notes: str = Field(max_length=1500)


class PlanDecision(BaseModel):
    symbol: str
    action: Literal["approve", "reduce", "veto"]
    units: Optional[int] = Field(default=None, ge=0, description="new unit count when action == reduce")
    reason: str = Field(max_length=300)


class PlanReview(BaseModel):
    decisions: list[PlanDecision]
    overall_note: str = Field(max_length=600)


class OpsReport(BaseModel):
    summary: str = Field(max_length=1500)
    issues: list[str] = Field(max_length=10)
    proposals: list[str] = Field(max_length=10, description="config or process changes for the human to consider; never applied automatically")


@dataclass
class Leg:
    key: OptionKey
    qty: int  # per unit of structure (positive = long)
    ref_bid: float
    ref_ask: float
    ref_mid: float
    iv: Optional[float] = None
    delta: Optional[float] = None


@dataclass
class StructurePlan:
    symbol: str
    kind: StructureKind
    legs: list[Leg]
    unit_cost: float  # $ per share for one unit (sum of asks, conservative)
    units: int = 0
    est_total_cost: float = 0.0  # dollars incl. commissions
    sim: dict = field(default_factory=dict)
    rationale: str = ""

    @property
    def unit_mid_cost(self) -> float:
        return sum(l.ref_mid * l.qty for l in self.legs)


@dataclass
class Candidate:
    event: EarningsEvent
    play_type: PlayType = "earnings"
    spot: float = 0.0
    expiry: Optional[date] = None
    reaction_date: Optional[date] = None
    implied_move_pct: Optional[float] = None  # percent
    atm_strike: Optional[float] = None
    straddle_mid: Optional[float] = None
    history: list[EarningsMove] = field(default_factory=list)
    hist_stats: dict = field(default_factory=dict)
    liquidity: dict = field(default_factory=dict)
    catalyst: dict = field(default_factory=dict)
    headlines: list[Headline] = field(default_factory=list)
    profile: dict = field(default_factory=dict)
    thesis: Optional[TradeThesis] = None
    quant_score: float = 0.0
    score: float = 0.0
    score_breakdown: dict = field(default_factory=dict)
    plan: Optional[StructurePlan] = None
    skip_reason: Optional[str] = None
    chain: Optional[OptionChain] = field(default=None, repr=False)

    @property
    def symbol(self) -> str:
        return self.event.symbol


@dataclass
class PositionLeg:
    key: OptionKey
    qty: int  # contracts originally filled (long)
    avg_cost: float  # $ per share
    remaining: int
    realized: float = 0.0  # dollars


@dataclass
class Position:
    id: str
    symbol: str
    play_type: PlayType
    kind: StructureKind
    legs: list[PositionLeg]
    opened_at: datetime
    reaction_date: date
    expiry: date
    cost_basis: float  # dollars incl. commissions
    status: Literal["open", "closed"] = "open"
    high_water_value: float = 0.0
    took_half: bool = False
    realized_pnl: float = 0.0
    closed_at: Optional[datetime] = None
    notes: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def open_legs(self) -> list[PositionLeg]:
        return [l for l in self.legs if l.remaining > 0]

    def remaining_cost(self) -> float:
        """Cost basis still at risk (dollars) for legs not yet sold."""
        return sum(l.avg_cost * 100 * l.remaining for l in self.legs)


@dataclass
class AccountInfo:
    equity: float
    cash: float
    buying_power: float
    day_trades_remaining: Optional[int] = None  # None unknown, -1 unlimited
    is_margin: Optional[bool] = None
    raw: dict = field(default_factory=dict)


@dataclass
class BrokerPosition:
    key: OptionKey
    qty: int
    avg_cost: float  # $ per share


@dataclass
class OrderState:
    order_id: str
    status: Literal["open", "filled", "cancelled", "rejected"]
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    message: str = ""


@dataclass
class Fill:
    key: OptionKey
    side: str
    qty: int
    price: float
    commission: float
    order_id: str
    at: datetime


@dataclass
class ExitAction:
    leg_key: OptionKey
    qty: int
    reason: str
    urgency: Literal["normal", "high"] = "normal"
