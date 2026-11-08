"""Typed configuration loaded from config.yaml plus environment variables (.env)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

RISK_PROFILES = {
    # per_trade_pct, max_deployed_pct, max_open_positions, daily_loss_limit_pct
    "conservative": dict(per_trade_pct=0.05, max_deployed_pct=0.25, max_open_positions=3, daily_loss_limit_pct=0.15),
    "aggressive": dict(per_trade_pct=0.12, max_deployed_pct=0.60, max_open_positions=4, daily_loss_limit_pct=0.25),
    "max": dict(per_trade_pct=0.20, max_deployed_pct=0.80, max_open_positions=4, daily_loss_limit_pct=0.35),
    # 'unlimited': the user accepts total loss of deployed capital. Up to half the account on the single best idea,
    # everything deployable across the day's picks, no daily loss stop; only the equity floor remains as a last brake.
    "unlimited": dict(per_trade_pct=0.50, max_deployed_pct=1.0, max_open_positions=3, daily_loss_limit_pct=1.0),
}


class AccountCfg(BaseModel):
    starting_equity: float = 5000.0
    initial_equity_for_floor: float = 5000.0


class ScheduleCfg(BaseModel):
    timezone: str = "America/New_York"
    calendar_refresh: str = "07:45"
    scan: str = "12:30"
    final_pricing: str = "15:20"
    entry_start: str = "15:30"
    entry_end: str = "15:57"
    eod_report: str = "16:05"
    manage_poll_seconds_open: int = 10
    manage_poll_seconds_late: int = 30


class UniverseCfg(BaseModel):
    min_market_cap: float = 1.5e9
    min_implied_move_pct: float = 0.0        # only consider names whose market-implied move is at least this (percent)
    max_new_positions_per_day: int = 3
    include_unknown_timing: bool = False
    allowlist: list[str] = Field(default_factory=list)
    blocklist: list[str] = Field(default_factory=list)


class OptionsCfg(BaseModel):
    max_days_to_expiry_after_reaction: int = 7
    min_open_interest: int = 300
    min_volume: int = 50
    max_spread_pct: float = 0.12
    max_spread_pct_cheap: float = 0.20
    min_option_price: float = 0.15


class ScoringWeights(BaseModel):
    hist_ratio: float = 40
    big_move_freq: float = 20
    liquidity: float = 15
    llm: float = 15
    catalyst: float = 10


class ScoringCfg(BaseModel):
    trade_threshold: float = 55
    trade_threshold_without_llm: float = 65
    lookback_earnings: int = 8
    weights: ScoringWeights = ScoringWeights()
    unknown_timing_penalty: float = 10


class StructuresCfg(BaseModel):
    objective: Literal["growth", "aggressive", "moonshot"] = "growth"
    directional_min_confidence: float = 0.65
    directional_target_delta: float = 0.40
    directional_deltas: list[float] = Field(default_factory=lambda: [0.40])
    strangle_max_width_vs_implied: float = 0.50
    strangle_widths: list[float] = Field(default_factory=lambda: [0.25, 0.50])
    iv_crush_factor: float = 0.55
    min_expected_return: float = 0.03        # structure must clear this simulated expected return after IV crush
    aggressive_min_p_profit: float = 0.20    # 'aggressive'/'moonshot' objectives still require this probability of profit


class RiskCfg(BaseModel):
    profile: Literal["conservative", "aggressive", "max", "unlimited"] = "aggressive"
    per_trade_pct: Optional[float] = None
    max_deployed_pct: Optional[float] = None
    max_open_positions: Optional[int] = None
    daily_loss_limit_pct: Optional[float] = None
    equity_floor_pct: float = 0.50
    max_per_sector: int = 2
    max_consecutive_losing_days: int = 3
    pause_days_after_streak: int = 1
    commission_per_contract: float = 0.65
    min_cash_reserve: float = 200.0
    pdt_mode: Literal["auto", "enforce", "off"] = "auto"

    @model_validator(mode="after")
    def _apply_profile(self):
        prof = RISK_PROFILES[self.profile]
        for k, v in prof.items():
            if getattr(self, k) is None:
                setattr(self, k, v)
        # bounds are the physical ones only: a cash account cannot spend more than 100% of itself
        self.per_trade_pct = max(min(float(self.per_trade_pct), 1.0), 0.0)
        self.max_deployed_pct = max(min(float(self.max_deployed_pct), 1.0), 0.0)
        self.equity_floor_pct = max(min(self.equity_floor_pct, 0.95), 0.0)
        return self


class ExitsCfg(BaseModel):
    salvage_leg_below_pct: float = 0.25
    take_half_at_gain_pct: float = 1.00
    trail_pct_from_high: float = 0.25
    trail_floor_gain_pct: float = 0.50
    hard_stop_loss_pct: float = 0.60
    hard_stop_not_before: str = "09:45"
    default_time_stop: str = "10:30"
    winner_time_stop: str = "11:30"
    absolute_time_stop: str = "15:30"
    zero_dte_time_stop: str = "15:00"
    dust_threshold: float = 0.05


class ExecutionCfg(BaseModel):
    chase_step_pct_of_spread: float = 0.15
    chase_interval_seconds: int = 20
    max_pay_pct_of_spread: float = 1.0
    exit_chase_interval_seconds: int = 15


class MacroCfg(BaseModel):
    enabled: bool = True
    symbols: list[str] = Field(default_factory=lambda: ["SPY", "QQQ"])
    budget_pct_of_per_trade: float = 0.5
    events_file: str = "macro_events.yaml"


class LLMCfg(BaseModel):
    enabled: bool = True
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    server_fallbacks: bool = True
    max_headlines: int = 25
    timeout_seconds: int = 240


class NewsCfg(BaseModel):
    sources: list[str] = Field(default_factory=lambda: ["google_rss", "yfinance", "finnhub", "fmp"])
    lookback_days: int = 7


class AgentsCfg(BaseModel):
    enabled: bool = True
    research: bool = True          # catalyst researcher (server-side web search) at the morning calendar refresh
    risk_officer: bool = True      # reviews the final plan; can only approve, reduce or veto
    ops: bool = True               # daily journal + proposals at the end of day
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    max_searches: int = 10
    max_fetches: int = 5
    research_days_ahead: int = 7
    journal_dir: str = "journal"
    timeout_seconds: int = 600


class NotifyCfg(BaseModel):
    telegram: bool = True
    discord: bool = True


class DataCfg(BaseModel):
    calendar_sources: list[str] = Field(default_factory=lambda: ["nasdaq", "fmp", "yfinance"])
    cache_dir: str = ".cache"


class StateCfg(BaseModel):
    db_path: str = "state/earnings_bot.sqlite"


class IBKRCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 17
    account: str = ""
    market_data_type: int = 1


class AlpacaCfg(BaseModel):
    paper: bool = True
    options_feed: Literal["indicative", "opra"] = "indicative"


class Secrets(BaseModel):
    anthropic_api_key: str = ""
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    fmp_api_key: str = ""
    finnhub_api_key: str = ""
    tavily_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""
    live_trading_ack: str = ""


class Config(BaseModel):
    mode: Literal["paper", "live"] = "paper"
    broker: Literal["paper", "ibkr", "alpaca"] = "paper"
    account: AccountCfg = AccountCfg()
    schedule: ScheduleCfg = ScheduleCfg()
    universe: UniverseCfg = UniverseCfg()
    options: OptionsCfg = OptionsCfg()
    scoring: ScoringCfg = ScoringCfg()
    structures: StructuresCfg = StructuresCfg()
    risk: RiskCfg = RiskCfg()
    exits: ExitsCfg = ExitsCfg()
    execution: ExecutionCfg = ExecutionCfg()
    macro: MacroCfg = MacroCfg()
    llm: LLMCfg = LLMCfg()
    news: NewsCfg = NewsCfg()
    agents: AgentsCfg = AgentsCfg()
    notify: NotifyCfg = NotifyCfg()
    data: DataCfg = DataCfg()
    state: StateCfg = StateCfg()
    ibkr: IBKRCfg = IBKRCfg()
    alpaca: AlpacaCfg = AlpacaCfg()
    secrets: Secrets = Secrets()
    root: Path = Path(".")

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    def validate_live(self) -> None:
        """Refuse to run live without the explicit acknowledgement and a real broker."""
        if not self.is_live:
            return
        if self.broker == "paper":
            raise ValueError("mode=live requires broker=ibkr or broker=alpaca")
        if self.secrets.live_trading_ack != "I_UNDERSTAND_THE_RISK":
            raise ValueError("mode=live requires LIVE_TRADING_ACK=I_UNDERSTAND_THE_RISK in the environment (.env)")
        if self.broker == "ibkr" and self.ibkr.market_data_type != 1:
            raise ValueError("live IBKR trading requires ibkr.market_data_type=1 (real-time quotes)")


def load_config(path: str | Path = "config.yaml", env_file: str | Path | None = ".env") -> Config:
    path = Path(path)
    root = path.resolve().parent
    if env_file:
        load_dotenv(root / env_file if not Path(env_file).is_absolute() else env_file, override=False)
    raw = {}
    if path.exists():
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
    raw["secrets"] = {
        "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
        "alpaca_api_key": os.getenv("ALPACA_API_KEY", ""),
        "alpaca_secret_key": os.getenv("ALPACA_SECRET_KEY", ""),
        "fmp_api_key": os.getenv("FMP_API_KEY", ""),
        "finnhub_api_key": os.getenv("FINNHUB_API_KEY", ""),
        "tavily_api_key": os.getenv("TAVILY_API_KEY", ""),
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
        "discord_webhook_url": os.getenv("DISCORD_WEBHOOK_URL", ""),
        "live_trading_ack": os.getenv("LIVE_TRADING_ACK", ""),
    }
    # environment overrides for the two switches people most often need to flip without editing YAML
    if os.getenv("BOT_MODE"):
        raw["mode"] = os.getenv("BOT_MODE")
    if os.getenv("BOT_BROKER"):
        raw["broker"] = os.getenv("BOT_BROKER")
    if os.getenv("IBKR_HOST") or os.getenv("IBKR_PORT"):
        raw.setdefault("ibkr", {})
        if os.getenv("IBKR_HOST"):
            raw["ibkr"]["host"] = os.getenv("IBKR_HOST")
        if os.getenv("IBKR_PORT"):
            raw["ibkr"]["port"] = int(os.getenv("IBKR_PORT"))
    cfg = Config(**raw)
    cfg.root = root
    return cfg
