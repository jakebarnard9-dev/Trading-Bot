# Go-live runbook — IBKR cash account, ~$1,000 (prepared Sun Nov 8, 2026)

## What I verified from your IBKR connection

| Item | Value | Meaning |
|---|---|---|
| Net liquidation | $1,006.50 | the bot's equity base; kill-switch floor = $500 |
| Total cash / available funds / buying power | $1,006.50 | IBKR shows the deposit as spendable |
| Initial & maintenance margin | $0, leverage 0.0 | **cash account** (no margin, so no pattern-day-trade rule applies) |
| Equity with loan value | $10.00 | for cash accounts this is settled cash → ~$996 of the deposit looks unsettled; IBKR typically lets you trade an ACH deposit once it posts, so this is informational. If an order is rejected for funds on Monday, that is why |
| Connector flow | Review & Approve | orders from this chat would only be *instructions* you approve in IBKR; not autonomous |
| SPY stock quote status | FROZEN | real-time US stock data is entitled (FROZEN = last real-time print, market closed) |
| SPY Nov-10 737 call quote status | **FROZEN_DELAYED** | **options data is delayed only: no OPRA subscription yet.** The bot refuses to trade on delayed option quotes, so step 2 below is mandatory |

IBKR Autonomous Trading (the `mcp-public` endpoint placing orders without approval) requires an eligible non-margin account **and 10 approved trades of $100+ over a two-week period in Review & Approve mode first** (an eligibility test is "coming soon"). Your account is eligible by type but has not met that requirement, so the bot runs through TWS / IB Gateway's socket API instead. That is the standard, fully autonomous path and needs no approval per order.

## Only you can do these (Sunday evening)

1. **Options trading permission.** Client Portal → Settings → Account Settings → Trading Permissions → Options → United States. Request at least the level that allows buying calls and puts ("Limited"). You may have to update investment objectives (include *speculation*) and options experience. Approval is usually same-day but can take 1–2 business days. **Without it every option order is rejected** and the bot will log `rejected` and open nothing.
2. **Market data.** Client Portal → Settings → User Settings → Market Data Subscriptions → add **OPRA (US Options Exchanges) Top of Book** (about $1.50/month for non-professionals) and the **US Securities Snapshot and Futures Value Bundle** (about $10/month, waived with $30 of commissions). Without OPRA the bot receives no option quotes and refuses to trade (`no live market`).
3. **Install TWS (or IB Gateway) on a computer that stays on**, log in to the *live* account, then Global Configuration → API → Settings: tick *Enable ActiveX and Socket Clients*, socket port **7496** (Gateway: 4001), **untick** *Read-Only API*, add `127.0.0.1` to *Trusted IPs*. Under API → Precautions tick **Bypass Order Precautions for API Orders** (otherwise every API order pops a dialog and never reaches the exchange). Under Lock and Exit set *Auto restart* at a quiet hour (e.g. 23:45) so the nightly restart does not need a new login; IBKR still asks you to log in again weekly (Sunday).
4. **Install the bot** on that computer (Python 3.11+):
   - macOS/Linux: `make install`
   - Windows (PowerShell): `python -m venv .venv; .venv\Scripts\Activate.ps1; pip install -r requirements.txt; pip install -e .`
   - copy `.env.example` to `.env`, fill `ANTHROPIC_API_KEY`, Telegram token/chat id (or Discord webhook), and set `LIVE_TRADING_ACK=I_UNDERSTAND_THE_RISK` (config.yaml is already `mode: live`, `broker: ibkr`, port 7496).
5. **Check it:** `earnings-bot doctor` — every line must be PASS during market hours, including *broker option quote* (this is the OPRA + permission check). Then `earnings-bot preview --deep --start 2026-11-09`.
6. **Start it:** `earnings-bot run` in a terminal that stays open (Windows: put a shortcut to `deploy\windows-start.bat` in the Startup folder; Linux: `deploy/earnings-bot.service`). It is restart-safe and reconnects to TWS by itself after the nightly restart.

## What to expect with $1,000

- Per position: up to $200 of premium (20%); at most $500 at risk at once (50%); 3 positions max; no new entries after a 35% daily loss; permanent halt below $500 until `earnings-bot reset-kill`.
- Affordable instruments: straddles on stocks under roughly $15 (PLUG, LCID, ACHR, MARA-type names when they report), single-contract calls/puts on stocks up to about $40 when the analyst has a view. CSCO, AMAT, TTWO, TDG are out of reach; the bot logs `one unit costs $X > budget` and moves on.
- Many sessions will end with zero trades. That is the filter working, not a fault.
- Cash account mechanics: proceeds from Tuesday-morning exits settle Wednesday; the bot sizes off IBKR's *Available Funds*, so it never tries to spend money the broker will not release.
- Five sessions cannot realistically turn $1,000 into $50,000 (that is 50× in a week); the limits above are what is guaranteed, the upside is not.

## Monday timeline (ET)

| Time | What you should see |
|---|---|
| 07:45 | Telegram: calendar refreshed, candidates entering today (TTWO, SKYD, OXY, RKLB, HIMS, TTD, MARA, PLUG, ACHR, LCID … plus Tuesday-morning reporters SE, ONON, TDG, QBTS) |
| 12:30 | scan result: scores, implied vs historical moves, analyst theses, chosen structures, skip reasons |
| 15:20 | final plan with live IBKR quotes and sizes |
| 15:30–15:57 | fills (or "not filled" if the market moved) |
| 16:05 | end-of-day summary |
| Tue 09:30–11:30 | exits with P/L per leg |

## If something is wrong

- `cannot start … is TWS running` → TWS/Gateway not running, not logged in, API disabled, or wrong port.
- `no live market` on every name → OPRA subscription missing or market closed.
- orders `rejected` → options permission not yet approved, or funds not available.
- `earnings-bot status` shows account, open positions and the last decisions; `earnings-bot flatten --yes` sells everything; `logs/bot.log` has the full trace.
