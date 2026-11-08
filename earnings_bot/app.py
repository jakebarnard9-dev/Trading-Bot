"""Factory wiring config -> concrete components. Tests/replay pass overrides (fixtures, fake clock, paper broker)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .agents import CatalystResearcher, ClaudeAgentClient, OpsAgent, RiskOfficer
from .analysis.llm import ClaudeAnalyst, NullAnalyst
from .broker.alpaca import AlpacaBroker
from .broker.ibkr import IBKRBroker
from .broker.paper import PaperBroker
from .clock import Clock
from .config import Config
from .data.calendar_sources import FMPCalendar, FinnhubCalendar, NasdaqCalendar, YFinanceCalendar
from .data.news import FMPNews, FinnhubNews, GoogleNewsRSS, NewsAggregator, TavilyNews, YFinanceNews
from .data.yf_data import YFMarketData
from .engine import Engine
from .execution.executor import Executor
from .notify import Notifier
from .risk.manager import RiskManager
from .state.store import Store
from .strategy.macro import load_macro_events


def make_calendar_sources(cfg: Config) -> list:
    s = cfg.secrets
    avail = {"nasdaq": NasdaqCalendar(), "fmp": FMPCalendar(s.fmp_api_key), "finnhub": FinnhubCalendar(s.finnhub_api_key), "yfinance": YFinanceCalendar()}
    out = [avail[n] for n in cfg.data.calendar_sources if n in avail]
    if s.finnhub_api_key and "finnhub" not in cfg.data.calendar_sources:
        out.append(avail["finnhub"])
    return out


def make_news(cfg: Config) -> NewsAggregator:
    s = cfg.secrets
    avail = {"google_rss": GoogleNewsRSS(), "yfinance": YFinanceNews(), "finnhub": FinnhubNews(s.finnhub_api_key), "fmp": FMPNews(s.fmp_api_key), "tavily": TavilyNews(s.tavily_api_key)}
    srcs = [avail[n] for n in cfg.news.sources if n in avail]
    if s.tavily_api_key and "tavily" not in cfg.news.sources:
        srcs.append(avail["tavily"])
    return NewsAggregator(srcs, cfg.news.lookback_days, cfg.llm.max_headlines)


def make_broker(cfg: Config, md, clock):
    if cfg.broker == "paper":
        return PaperBroker(md, cfg.account.starting_equity, cfg.risk.commission_per_contract, clock)
    if cfg.broker == "ibkr":
        return IBKRBroker(cfg.ibkr, cfg.risk.commission_per_contract)
    if cfg.broker == "alpaca":
        return AlpacaBroker(cfg.alpaca, cfg.secrets.alpaca_api_key, cfg.secrets.alpaca_secret_key, 0.0)
    raise ValueError(cfg.broker)


def build_engine(cfg: Config, clock: Optional[Clock] = None, overrides: Optional[dict] = None) -> Engine:
    o = overrides or {}
    clock = clock or Clock()
    store = o.get("store") or Store(Path(cfg.root) / cfg.state.db_path)
    md = o.get("md") or YFMarketData()
    cal = o.get("calendar_sources") or make_calendar_sources(cfg)
    news = o.get("news") or make_news(cfg)
    analyst = o.get("analyst") or (ClaudeAnalyst(cfg.llm, cfg.secrets.anthropic_api_key) if cfg.llm.enabled and cfg.secrets.anthropic_api_key else NullAnalyst())
    broker = o.get("broker") or make_broker(cfg, md, clock)
    notifier = o.get("notifier") or Notifier(cfg.notify, cfg.secrets)
    risk = RiskManager(cfg, store)
    executor = Executor(broker, store, cfg.execution, clock, cfg.risk.commission_per_contract, sleep=o.get("sleep"))
    macro = o.get("macro_events")
    if macro is None:
        macro = load_macro_events(Path(cfg.root) / cfg.macro.events_file) if cfg.macro.enabled else []
    agent_client = o.get("agent_client")
    if agent_client is None and cfg.agents.enabled and cfg.secrets.anthropic_api_key:
        agent_client = ClaudeAgentClient(cfg.secrets.anthropic_api_key, cfg.agents.model, cfg.agents.effort, cfg.agents.timeout_seconds)
    researcher = o["researcher"] if "researcher" in o else (CatalystResearcher(agent_client, cfg.agents.max_searches, cfg.agents.max_fetches) if agent_client and cfg.agents.research else None)
    risk_officer = o["risk_officer"] if "risk_officer" in o else (RiskOfficer(agent_client) if agent_client and cfg.agents.risk_officer else None)
    ops = o["ops"] if "ops" in o else (OpsAgent(agent_client, Path(cfg.root) / cfg.agents.journal_dir) if agent_client and cfg.agents.ops else None)
    return Engine(cfg, store, clock, broker, md, cal, news, analyst, notifier, risk, executor, macro, researcher=researcher, risk_officer=risk_officer, ops=ops)
