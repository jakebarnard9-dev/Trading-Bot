# catalyst-bot — complete setup, step by step (Windows PC + TWS, Sunday night → Monday)

Time needed: about 45 minutes, plus waiting for IBKR approvals. Everything marked **[auto]** is done by a script;
everything marked **[you]** can only be done by you in IBKR's or Anthropic's web pages.

## Part 1 — IBKR account (Client Portal, interactivebrokers.com → Log in → Client Portal)

1. **[you] Options permission.** Menu (☰) → Settings → Account Settings → Trading → *Trading Permissions* → Options →
   United States → request. If asked for investment objectives include *Speculation*; options knowledge *Good* or better;
   years of experience ≥ 2 (be truthful). Typical approval: minutes to 24 hours. Until it shows *Approved*, every option
   order is rejected.
2. **[you] Confirm OPRA** (you added it): Settings → User Settings → Market Data Subscriptions → *OPRA (US Options
   Exchanges) Top of Book* must be listed as active. Add *US Securities Snapshot and Futures Value Bundle* too if it is not.
3. **[you] Funding.** Transfer the $4,000 (wire posts same day; ACH 1–4 business days). The bot uses whatever IBKR
   shows as *Available Funds* at 15:20 ET.
4. **[you] IB Key** on your phone for two-factor (you already log into TWS, so this exists).

## Part 2 — TWS (the desktop app you logged into)

5. **[you]** File → Global Configuration → API → **Settings**:
   - ☑ Enable ActiveX and Socket Clients
   - ☐ Read-Only API (must be **off**)
   - Socket port: **7496**
   - ☑ Allow connections from localhost only; Trusted IPs: add `127.0.0.1`
   - Master API client ID: leave blank
6. **[you]** API → **Precautions**: ☑ *Bypass Order Precautions for API Orders* (also tick *Bypass Bond warning* etc. if offered).
7. **[you]** Configuration → **Lock and Exit**: choose *Auto restart*, time **11:45 PM**. (TWS then restarts nightly
   without a new login; Sunday needs one manual login.)
8. **[you]** Windows: Settings → System → Power → Screen and sleep → *Never* for sleep on power. Keep TWS open.
9. Apply/OK. TWS shows a green "API" connection indicator once the bot connects.

## Part 3 — keys

10. **[you] Anthropic API key**: console.anthropic.com → API Keys → Create key (starts with `sk-ant-`). Add a few
    dollars of credit; the agents cost roughly $2–6 per trading day.
11. **[you] Telegram (recommended)**: in Telegram message **@BotFather** → `/newbot` → copy the token; then message
    **@userinfobot** → copy your numeric id. Open a chat with your new bot and press Start once.

## Part 4 — the code on your PC

12. **[you]** Get `catalyst-bot.zip` (download `dist/catalyst-bot.zip` from the Claude session's file view, or, if you
    approve the GitHub push in the session, `git clone https://github.com/jakebarnard9-dev/catalyst-bot.git`).
    Unzip to **C:\catalyst-bot** (so that `C:\catalyst-bot\config.yaml` exists).
13. **[you]** Install Python 3.11 or newer from python.org → Downloads → Windows; in the installer tick
    **Add python.exe to PATH**.
14. **[auto]** Open PowerShell (Start → type PowerShell → Run as administrator is not needed) and run:
    ```powershell
    cd C:\catalyst-bot
    Set-ExecutionPolicy -Scope Process Bypass -Force
    .\deploy\windows-setup.ps1
    ```
    The script creates the Python environment, installs dependencies, asks for the three keys and the live
    acknowledgement, runs the synthetic replay, runs `doctor`, prints the week's calendar, creates an autostart
    shortcut, and offers to start the bot.

## Part 5 — reading the checks

15. `doctor` lines and what to do if one fails:
    | Line | FAIL means |
    |---|---|
    | config | `.env` lacks `LIVE_TRADING_ACK=I_UNDERSTAND_THE_RISK` (live mode) |
    | broker | TWS not running / API not enabled / wrong port / Read-Only still on |
    | broker option quote | OPRA not active (during market hours) or options permission missing; outside hours inconclusive |
    | market data (yfinance) / calendar sources / news | internet or Yahoo/Nasdaq blocked; try again |
    | LLM analyst | wrong Anthropic key or no credit |
    | notifications | Telegram token/chat id wrong (check you pressed Start in the bot chat) |
16. `preview --deep --start 2026-11-09` shows every name with implied vs historical move; anything under 6% implied is
    outside this mandate and will be skipped.

## Part 6 — press go (Monday)

17. If the setup script did not start it: double-click `deploy\windows-start.bat` (it restarts the bot if it exits).
18. Expected Telegram messages: 07:45 calendar + research, 12:30 scan, 15:20 final plan + risk officer, 15:30–15:57
    fills, 16:05 summary + journal; Tuesday 09:30–11:30 exits.
19. Daily routine: keep PC and TWS up; Sunday: log into TWS again. Useful commands in PowerShell
    (`cd C:\catalyst-bot; .\.venv\Scripts\Activate.ps1` first): `earnings-bot status`, `earnings-bot flatten --yes`,
    `earnings-bot reset-kill`. Journal: `journal\2026-11-09.md`. Log: `logs\bot.log`.

## Option — cloud VPS instead of your PC

`DEPLOY_CLOUD.md`: a $6–12/month Ubuntu VPS running IB Gateway headless in Docker next to the bot; you approve the
IB Key push on your phone at start and once a week. Do not run the same IBKR username in TWS on your PC at the same time.

## What I can do for you remotely

If you enable Remote Control in the Claude desktop app on this PC and share a normal folder (not a hidden one), I can
attach to it and run Parts 4–6 myself. Parts 1–3 (IBKR permissions, keys) are account actions only you can perform.
