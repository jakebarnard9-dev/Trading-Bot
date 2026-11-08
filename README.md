# catalyst-bot

An autonomous, nearest-expiry (0–1 DTE where listed) **long-premium options bot** for earnings reports and scheduled macro
releases. It pulls the earnings calendar, prices the market-implied move against each name's own history and a Claude
analyst's read of the news, buys straddles / strangles / directional options into the close before the event, and
manages the exit in the first hours of the reaction session under hard, code-enforced risk limits.

Runs in **paper mode by default**. Live trading is supported through **Interactive Brokers** (TWS / IB Gateway via
`ib_async`) or **Alpaca** (REST), and is gated behind an explicit acknowledgement in `.env`.

> Read `GO_LIVE.md` (IBKR go-live on your PC with TWS, or `DEPLOY_CLOUD.md` for a headless VPS) and `THIS_WEEK.md` for the Nov 9–13, 2026 plan.
> The shipped `config.yaml` is the **aggressive, unlimited-risk** configuration the owner asked for; see §5 and §11.

---

## 1. What this bot can and cannot promise

**It can guarantee process, not profit.** Every component that touches money is deterministic and bounded:
per-trade budget, total deployment cap, daily loss limit, equity-floor kill switch, no averaging down, no selling
naked premium, no paying above the ask. The LLM analyst cannot change any of those; it only adds information to the score.

**It cannot guarantee the target.** Turning $5k into $50k in five sessions requires the whole account to compound
about +58% per day, or every position to roughly double every day at the 60% deployment cap. Long earnings premium
has a well-documented headwind: the straddle-implied move exceeds the realized move more often than not, and IV
collapses after the print. The bot's entire edge is *selectivity* — it only buys when a name's own reaction history,
option liquidity and the analyst's evidence say the market is under-pricing the move, and it will sit out days when
nothing qualifies. A realistic one-week outcome for this strategy is a wide distribution centred near zero with fat
tails on both sides; the risk limits exist so the left tail stops near the configured floor (default: 50% of
starting equity). Nobody can make the right tail certain.

---

## 2. Daily cycle (all times America/New_York)

| Time | Phase | What happens |
|---|---|---|
| 07:45 | calendar refresh | Nasdaq + FMP + Finnhub + Yahoo earnings calendars merged for the next 10 days; macro events loaded from `macro_events.yaml` |
| 09:30 → 11:30 | manage | Open positions re-quoted every 10 s; exit rules applied (salvage, take-half, trailing stop, hard stop, time stops) |
| 11:30 → 16:00 | manage | Same, every 30 s; absolute time stop 15:30 (15:00 for same-day expiries) |
| 12:30 | scan + analyze | Candidates for **today's entry session**: AMC reporters today and BMO reporters tomorrow (plus pre-market macro releases tomorrow). Implied move, 8-quarter reaction history, liquidity, news, analyst thesis, score, structure choice |
| 15:20 | finalize | Legs re-quoted with **broker** prices, sized by the risk manager, sector/deployment/cash caps applied |
| 15:30 → 15:57 | enter | Limit orders at mid, stepped toward the ask every 20 s, never above the ask; unfilled legs cancelled at 15:57 |
| 16:05 | end of day | Equity snapshot, realized P/L, losing-streak check, tomorrow's candidate count, notification |

The bot is restart-safe: each phase is recorded in SQLite and runs at most once per day; fills are persisted before
positions, and a restart re-attaches any position whose fills were recorded but whose position row was lost.

---

## 3. How a trade is chosen

1. **Universe**: names reporting with a known BMO/AMC time (unknown-time names are excluded unless configured),
   market cap ≥ $1.5B, not block-listed.
2. **Instrument**: the nearest listed expiry on/after the reaction session (true 0 DTE when the reaction day is an
   expiry day, e.g. Thursday-AMC reporters → Friday weeklies; SPY/QQQ daily expiries for macro days). Names without a
   weekly within 7 days of the reaction are skipped.
3. **Implied move**: ATM straddle mid / spot, interpolated between the strikes bracketing spot.
4. **History**: the last 8 earnings reactions (close-before → open/close-after), giving median |move|, the ratio to the
   current implied move, and how often the move exceeded 1.25× implied.
5. **Liquidity gates**: two-sided market, spread ≤ 12% of mid (20% under $0.50), OI ≥ 300, volume ≥ 50, price ≥ $0.15.
6. **Analyst** (Claude, `claude-opus-5-5` by default, structured output): direction + confidence, expected move vs
   implied, probability of exceeding implied, structure recommendation, data quality, veto if the event looks moved /
   already reported / halted. Headlines are passed inside `<headlines>` tags with an explicit instruction that they are
   untrusted data; the analyst never sizes anything.
7. **Score** (0–100): historical ratio 40, big-move frequency 20, liquidity 15, analyst 15, catalyst density 10, minus
   a penalty for unconfirmed timing. Trade threshold 55 with a thesis, 65 without one (quant-only mode).
8. **Structure**: ATM straddle, OTM strangle (wings within 25–50% of the implied move) and, when the analyst's
   directional confidence ≥ 0.65, a ~0.40-delta call or put. Each is **simulated** over a move distribution built
   from the name's own history (scaled to the evidence-weighted expected move) with post-event IV crush applied, and
   the structure with the best expected log-growth at the configured per-trade fraction wins. No positive edge after
   crush → no trade.
9. **Sizing**: `units = floor(min(equity × per_trade_pct, remaining deployment cap, cash − reserve) / unit cost)`
   with unit cost at the ask plus commissions and a 5% buffer. Sizes compound with current equity.

---

## 4. Exit rules (reaction session)

| Rule | Default |
|---|---|
| Hold through the event | positions are never touched before the reaction session (gap risk is the premise) |
| Salvage | sell the losing leg of a straddle/strangle once it is worth < 25% of its cost while the other leg is winning |
| Take half | at +100% on the structure, sell half and trail the rest |
| Trailing stop | once a +50% gain has printed, exit if value falls 25% from its high-water mark |
| Hard stop | from 09:45, exit if the structure is down 60% or more |
| Time stops | 10:30 for non-winners, 11:30 for winners, 15:30 for everything, 15:00 for same-day expiries |
| Dust | options bid under $0.05 on expiry day are left to expire rather than paying commission; the forced 15:00/15:30 stops sell anything with a bid so nothing is left to auto-exercise |

---

## 5. Risk model and the aggressive configuration

| Limit | conservative | **aggressive (default)** | max |
|---|---|---|---|
| premium at risk per position | 5% | **12%** | 20% |
| total premium at risk | 25% | **60%** | 80% |
| max open positions | 3 | **4** | 4 |
| stop opening after a daily loss of | 15% | **25%** | 35% |

A fourth profile, **`unlimited`** (per position 50%, 100% deployable, 3 positions, no daily stop), exists for owners
who accept losing the deployed capital; the shipped `config.yaml` uses it with the equity floor at 20%. Caps are no
longer clamped below 100%: the YAML is the owner's decision. The kill switch halts *new* entries; it deliberately does
not dump open positions at the open. `structures.objective` chooses how a structure is picked: `growth` (Kelly-like
expected log growth), `aggressive` (highest simulated expected return with a probability-of-profit floor) or `moonshot`
(highest probability of a 3x). `universe.min_implied_move_pct` restricts the universe to names already priced for an
extreme move.

**Day-trade limits.** FINRA's $25,000 pattern-day-trader rule was replaced by intraday margin standards (SEC
approval April 14, 2026; effective June 4, 2026 per broker notices), with an 18-month phase-in for brokers. Some brokers
still count day trades. `risk.pdt_mode: auto` asks the broker (IBKR `DayTradesRemaining`, Alpaca `daytrade_count`)
and falls back to a local 3-per-5-sessions counter. The core strategy (enter at the close, exit next morning) is not a
day trade; only same-day macro plays and same-day salvage of same-day entries are.

---

## 5b. Agent system

Four Claude roles run inside the daily loop (all optional, all fail-safe, model `claude-opus-5-5` by default):

| Agent | When | Tools | Authority |
|---|---|---|---|
| Catalyst researcher | 07:45 | server-side web search + fetch | adds dated non-earnings catalysts (FDA PDUFA, regulatory, product, index, legal) and corrects earnings timing from primary sources |
| Analyst | 12:30 | structured output | TradeThesis per candidate (direction, expected move vs implied, veto if the event looks wrong) |
| Risk officer | 15:20 | structured output | reviews the sized plan: approve / reduce / veto, never increase |
| Ops | 16:05 | structured output | journal in `journal/`, issues, proposals (never applied automatically) |

Researched catalysts enter the same pipeline as earnings (`play_type: catalyst`): the researcher's expected move,
discounted by its confidence, replaces the reaction history in the score and the simulation.

## 6. Quick start

```bash
git clone <this repo> earnings-bot && cd earnings-bot
make install                      # venv + deps + editable install
cp .env.example .env              # add ANTHROPIC_API_KEY (and broker / Telegram keys)
earnings-bot replay               # end-to-end dry run on synthetic data (no network needed)
earnings-bot doctor               # connectivity: broker, market data, calendars, news, LLM, notifications
earnings-bot preview --deep       # this week's calendar with implied vs historical moves
earnings-bot run                  # paper mode (default) — leave it running
```

Run `make test` to execute the 25 unit / end-to-end tests.

### Interactive Brokers

1. Use a **paper account first** (TWS paper port 7497 / Gateway 4002). Live: 7496 / 4001.
2. TWS/Gateway → Global Configuration → API → Settings: enable *ActiveX and Socket Clients*, add `127.0.0.1` to
   trusted IPs, set the socket port, untick *Read-Only API*.
3. Account must have **options trading permissions** and a **real-time US options (OPRA) market-data subscription**.
   Without OPRA the quotes come back empty and the bot refuses to trade (`ibkr.market_data_type: 1` is required for live).
4. Keep TWS/Gateway running on the same machine (IBC or the Gateway's auto-restart handles the daily restart; weekly
   re-authentication is still manual on most setups — plan for it).
5. `config.yaml`: `broker: ibkr`, check `ibkr.port`, optionally `ibkr.account`.

### Alpaca

1. Create API keys (paper keys for paper). Options trading level 2 is sufficient (long calls/puts only).
2. `.env`: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`. `config.yaml`: `broker: alpaca`, `alpaca.paper: true|false`,
   `alpaca.options_feed: indicative` (free) or `opra` (subscription).

### Going live checklist (full runbook: `GO_LIVE.md`)

- `earnings-bot doctor` all green against the live broker.
- At least one paper session completed end to end (entries and exits) with the same config.
- `mode: live` in `config.yaml` **and** `LIVE_TRADING_ACK=I_UNDERSTAND_THE_RISK` in `.env` (the bot refuses otherwise).
- Telegram or Discord notifications working (you will want the fill/exit stream).
- Server clock in sync; process supervised (systemd unit in `deploy/`, or `docker compose up -d`).

---

## 7. Configuration reference (`config.yaml`)

| Key | Meaning |
|---|---|
| `mode`, `broker` | `paper`/`live`; `paper`/`ibkr`/`alpaca` |
| `account.starting_equity` | paper broker seed; `initial_equity_for_floor` anchors the kill switch |
| `schedule.*` | phase times; entry window `entry_start`→`entry_end` |
| `universe.*` | market-cap floor, max new positions per day, allow/block lists, unknown-timing handling |
| `options.*` | expiry distance, OI/volume/spread/price gates |
| `scoring.*` | thresholds, lookback, weights |
| `structures.*` | directional confidence bar, target delta, strangle width, IV-crush factor used in the simulation |
| `risk.*` | profile and overrides (see §5), commissions, `pdt_mode` |
| `exits.*` | all exit thresholds and times (see §4) |
| `execution.*` | chase step / interval / cap |
| `macro.*` | SPY/QQQ event plays, budget fraction, events file |
| `llm.*` | model, effort, server-side refusal fallbacks, headline count, timeout |
| `news.sources` | `google_rss`, `yfinance`, `finnhub`, `fmp`, `tavily` (keyed ones are skipped without a key) |

`macro_events.yaml` lists scheduled releases with your prior for the index move; the bot buys a SPY/QQQ straddle only
when the market-implied move is below 90% of that prior.

---

## 8. Operations

- `earnings-bot status` — account, open positions, equity series, last decisions (JSON).
- `earnings-bot flatten --yes` — sell every open position now (limit orders walked to the bid).
- `earnings-bot reset-kill` — clear the equity-floor halt / streak pause.
- Logs: `logs/bot.log` (rotating). State: `state/earnings_bot.sqlite` (tables: events, candidates, positions, orders,
  fills, equity, daily, day_trades, decisions, kv). Every candidate — traded or skipped — is stored with its score
  breakdown, thesis and skip reason, so each day is auditable.
- Notifications: `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` and/or `DISCORD_WEBHOOK_URL`.

---

## 9. What was verified, and what was not

Verified here: the full pipeline on synthetic data (calendar → scoring → structure simulation → sizing → entry chase →
overnight → reaction-day exits → expiry cleanup → reporting), the risk gates, exit rules, calendar merge, Nasdaq
parser (recorded response shape), LLM error paths (connection error, refusal, fallback-rejected retry), and the
installed-library API shapes for `ib_async` 2.1, `alpaca-py` 0.45, `yfinance` 1.8 and `anthropic` 2.6.

**Not verified** (the build sandbox has no broker or market-data access): live order flow against IBKR/Alpaca, live
yfinance / Nasdaq / news fetches, and the Claude analyst against the real API. Treat the first paper day as the
integration test and read `logs/bot.log`. Known soft spots: Yahoo's free options quotes are 15-minute delayed (used for
screening only; execution prices come from the broker), IBKR price increments are inferred from contract details, and
Alpaca option volume comes from daily bars (may read 0 early in the session).

---

## 10. Getting the code off this session

The project lives in `/home/user/catalyst-bot` (git initialised, one commit). Either let the session push it to a
private GitHub repository you approve, or copy the folder to your machine; then follow `GO_LIVE.md`.

## 11. Costs

IBKR: ~$0.65/contract + fees; OPRA data ~$1.50/month. Alpaca: $0 commission, regulatory fees only. Claude: roughly
8 analyst calls per day at `claude-opus-5-5`, on the order of a few cents each. Data feeds used by default are free.

This software is provided for your own account at your own risk; it is not investment advice.
