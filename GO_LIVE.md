# Go-live runbook — catalyst-bot on your IBKR account (prepared Sun Nov 8, 2026)

## Where things stand

| Item | Status |
|---|---|
| IBKR account | cash account, ~$1,006 now, $4,000 on the way (the bot sizes off IBKR *Available Funds*, so it uses the new money the moment IBKR releases it) |
| OPRA options data | subscribed by you (before that, the SPY option quote came back FROZEN_DELAYED; re-check with `earnings-bot doctor` during market hours: the *broker option quote* line must PASS) |
| Real-time stock data | entitled (SPY quote status FROZEN, i.e. real-time outside hours) |
| TWS | installed and logged in on your PC → the bot can run there today (Option A). A headless cloud stack is in `DEPLOY_CLOUD.md` (Option B) |
| Options trading permission | **unverified — check Client Portal → Settings → Trading Permissions → Options (US).** Without it every option order is rejected |
| The `mcp-public` connector | read/approve only until you complete 10 approved trades of $100+ over two weeks; not used by the bot |

## Option A — run it on your Windows PC next to TWS (ready today)

**1. TWS settings (once).** File → Global Configuration → API → Settings:
- tick **Enable ActiveX and Socket Clients**
- Socket port **7496** (live; paper is 7497)
- **untick Read-Only API**
- Trusted IP addresses: add `127.0.0.1` (keep "Allow connections from localhost only")
- API → Precautions: tick **Bypass Order Precautions for API Orders** (otherwise each API order pops a dialog and never reaches the exchange)
- Lock and Exit: choose **Auto restart** at a quiet time (e.g. 23:45) so the nightly restart keeps you logged in; IBKR still asks for a fresh login once a week (Sunday).
Keep the PC awake (Settings → Power → never sleep) and TWS open.

**2. Install (PowerShell, once).** Python 3.11+ from python.org with "Add python.exe to PATH" ticked, then:
```powershell
cd C:\catalyst-bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
copy .env.example .env
notepad .env
```
Fill in `.env`: `ANTHROPIC_API_KEY` (required for the analyst, researcher, risk officer and ops agents), `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (or `DISCORD_WEBHOOK_URL`), and the live interlock `LIVE_TRADING_ACK=I_UNDERSTAND_THE_RISK`. `config.yaml` is already `mode: live`, `broker: ibkr`, port 7496.

**3. Check.**
```powershell
earnings-bot doctor
earnings-bot preview --deep --start 2026-11-09
earnings-bot replay
```
Every doctor line must be PASS (run it during market hours for the option-quote line). `preview --deep` shows the week's names with implied vs historical moves.

**4. Press go.**
```powershell
earnings-bot run
```
Leave the window open (or put a shortcut to `deploy\windows-start.bat` in `shell:startup`). The bot is restart-safe and reconnects to TWS by itself after the nightly restart.

## Option B — cloud VPS with headless IB Gateway

See `DEPLOY_CLOUD.md` and `docker-compose.cloud.yml`: one `docker compose` stack runs IB Gateway (headless, auto-restart) and the bot; you approve the IB Key push on your phone at login and once a week.

## What the aggressive configuration does (config.yaml)

| Setting | Value | Effect |
|---|---|---|
| `risk.profile: unlimited` | per position 50% of equity, 100% deployable, 3 positions, no daily stop | the single best idea can take half the account; the day's picks can take all of it |
| `risk.equity_floor_pct: 0.20` | halt new entries below 20% of starting equity ($1,000 of $5,000) | the only brake left; set 0 to remove it |
| `structures.objective: aggressive` | pick the structure with the highest simulated expected return (p_profit ≥ 20%) | favours OTM strangles and 0.30–0.40-delta single legs over ATM straddles when the simulation supports it |
| `universe.min_implied_move_pct: 6` | only names the market already prices for a ≥ 6% move | "aggressive earnings reports" only |
| `options.max_days_to_expiry_after_reaction: 4` | nearest listed expiry | 0 DTE on reaction days that are expiry days (SPY/QQQ daily; Thu-AMC single stocks); Monday reporters get Friday weeklies (single stocks have no daily expiries) |
| `agents.*` | research + risk officer + ops on | PDUFA/regulatory/product/index catalysts are found and traded like earnings; the officer can only reduce or veto |

Plain statement of the odds: five sessions cannot be expected to turn $5,000 into $50,000. With full deployment the account can also be mostly gone in two bad sessions. The configuration above is what you asked for; the floor is the last protection.

## Monday timeline (ET)

| Time | What you should see on Telegram |
|---|---|
| 07:45 | calendar refreshed + research: catalysts added/corrected, today's entry candidates |
| 12:30 | scan: scores, implied vs historical moves, analyst theses, chosen structures, skip reasons |
| 15:20 | final plan with live IBKR quotes and sizes, risk-officer note |
| 15:30–15:57 | fills (or "not filled") |
| 16:05 | end-of-day summary + ops journal (`journal/2026-11-09.md`) |
| Tue 09:30–11:30 | exits with P/L per leg |

## If something is wrong

- `cannot start … is TWS running` → TWS not running / not logged in / API disabled / wrong port.
- `no live market` on every name → OPRA not active yet or market closed.
- orders `rejected` → options permission missing, or funds not yet available.
- `earnings-bot status` (account, open positions, decisions), `earnings-bot flatten --yes` (sell everything), `earnings-bot reset-kill` (clear the floor halt), `logs/bot.log`.
