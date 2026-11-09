# START HERE (3 steps)

## 1. Install (one double-click)
Double-click **INSTALL.bat**. It installs Python and Git if needed, puts the bot in `C:\catalyst-bot`,
installs dependencies, runs the checks and asks you for the key (step 2).

## 2. THE API KEY GOES HERE
**File:** `C:\catalyst-bot\.env`   (INSTALL.bat creates it; or copy `.env.example` to `.env` yourself)
**Line:** the first one:

```
ANTHROPIC_API_KEY=sk-ant-api03-sk-ant-usr-1HZ...3wAA
```

- Paste the key straight after the `=` sign: no spaces, no quotes.
- A real key starts with `sk-ant-api03-`. Get one at console.anthropic.com → API Keys → Create Key.
- Never paste the key into chat, email or GitHub. `.env` is ignored by git on purpose.
- The installer asks for it at a hidden prompt (right-click to paste, then Enter); that writes this same line for you.

Also in `.env`, this line is the live-trading switch:
```
LIVE_TRADING_ACK=I_UNDERSTAND_THE_RISK
```

## 3. Connect TWS and start
TWS → File → Global Configuration → API → Settings: enable ActiveX/Socket Clients, port **7496**, untick Read-Only,
trusted IP `127.0.0.1`; API → Precautions: Bypass Order Precautions for API Orders. Then double-click
`deploy\windows-start.bat` (or answer `y` when INSTALL.bat asks to start). Full detail: `SETUP.md`, `GO_LIVE.md`.
