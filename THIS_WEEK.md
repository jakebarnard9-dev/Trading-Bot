# Week of November 9–13, 2026 — what the bot will see and do

Calendar sourced on Nov 8 from Nasdaq's earnings calendar (via StockTitan's Nov 6 compilation) cross-checked with FMP
(CSCO Nov 12 AMC confirmed, LCID Nov 9 AMC). The bot re-fetches every morning at 07:45 ET, so late changes are picked
up automatically. Macro dates: BLS schedule as reported by public calendars (CPI Tue 08:30, PPI Fri 08:30).
Wednesday Nov 11 is Veterans Day — the stock market is open; no major US data that day.

## The $50k question, plainly (account is now ~$1,000)

- 10× in five sessions needs the account to compound ≈ +58% per session.
- The account holds about $1,000, so the configured limits are 20% ($200) per position and 50% ($500) at risk at once;
  even if every position doubled every day the account would reach roughly $7,600 by Friday, not $50,000.
- Long earnings premium wins roughly 40–45% of the time; typical winners +60–150%, typical losers −50–70%.
  Selectivity (history ratio, liquidity, analyst veto, no-edge-after-crush filter) is the only lever, and it means
  some days the bot trades nothing.
- What the limits do promise: at most $500 at risk at any time, a halt below $500 of equity, and a 35% daily loss ends
  new entries for the day. Losses can never exceed the premium actually paid.

## Entry schedule (enter 15:30–15:57 ET, exit the next morning)

| Entry session | Reaction session | Expiry the bot will use | Likely candidates the screener will price (subject to liquidity + score) |
|---|---|---|---|
| **Mon Nov 9** | Tue Nov 10 | Fri Nov 13 weeklies (3 DTE at reaction) | AMC today: TTWO, SKYD, OXY, RKLB, ASTS, HIMS, TTD, MARA, PLUG, ACHR, ZETA, ONTO, QLYS, HRB, SANM, AAON, TDW, USAR, LCID. BMO tomorrow: TDG, SE, ONON, VG, VOD, QBTS, WIX, LEGN. **Macro**: CPI 08:30 Tue → SPY/QQQ 0-DTE straddle if the implied move is < 0.86% |
| **Tue Nov 10** | Wed Nov 11 | Fri Nov 13 weeklies | AMC today: CRWV, RGTI, MRCY, REZI, FLY, STUB, PBR, EC. BMO tomorrow: GIB, RUM, ICL |
| **Wed Nov 11** | Thu Nov 12 | Fri Nov 13 weeklies | AMC today: VYLR, FNV, TTEK, CTVA, CAE, ENS, DGII. BMO tomorrow: BN, NBIS, ASND, YETI, ONDS, LUNR, FIGR, CLBT, TWST, HBM |
| **Thu Nov 12** | Fri Nov 13 | **Fri Nov 13 = true 0 DTE** | AMC today: **CSCO, AMAT**, CBRS, ARWR, DLO, STN. **Macro**: PPI 08:30 Fri → SPY/QQQ only if cheap. NU is listed for Thursday with no time → excluded unless `include_unknown_timing: true` |
| Fri Nov 13 | Mon Nov 16 | Nov 20 weeklies | Nothing US-listed with a known time (Japanese banks, time not supplied) → likely no entries |

Names are illustrative of what the calendar shows; the bot only trades those that pass the liquidity gates and score
above threshold *on the day*. Expect 0–3 positions per session.

## Macro events loaded (`macro_events.yaml`)

| Date | Time ET | Event | Prior |move| | Bot behaviour |
|---|---|---|---|---|
| Tue Nov 10 | 08:30 | CPI, October 2026 | 0.95% | Mon 15:30: SPY/QQQ Nov-10 straddle at half the normal budget **only** if implied < 0.86% |
| Fri Nov 13 | 08:30 | PPI, October 2026 | 0.60% | Thu 15:30: same test at < 0.54% (usually fails; PPI rarely prices cheap) |

Fed speakers all week (Bowman, Goolsbee, Barkin, Waller, Logan, Schmid, Hammack, Barr, Daly, Williams) are not traded.
Note the context: midterms were Nov 3, AI-chip names sold off hard on Nov 5, and Nvidia/Disney report the following
week (Nov 17) — expect elevated implied moves in CRWV, NBIS, AMAT, CBRS; the history-ratio filter is what keeps the bot
from overpaying for them.

## What you should do before Monday 15:30 ET

1. `earnings-bot doctor` against your broker (paper first).
2. `earnings-bot preview --deep --start 2026-11-09` to see live implied vs historical moves for the biggest names.
3. Decide the profile (`risk.profile`) and whether unknown-timing names are allowed.
4. Start `earnings-bot run` under systemd/docker and confirm the 07:45 calendar notification arrives Monday.
