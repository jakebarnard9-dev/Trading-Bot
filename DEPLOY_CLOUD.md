# Cloud deployment: bot + headless IB Gateway on a Linux VPS

Runs the bot 24/7 next to a headless Interactive Brokers Gateway, both as Docker containers on one small VPS. No
desktop TWS, no screen. Files: `docker-compose.cloud.yml`, `deploy/cloud-setup.sh`, `.env` (gateway section in
`.env.example`). Everything in `GO_LIVE.md` about permissions, OPRA data and `config.yaml` still applies.

```
VPS (Ubuntu 22.04 / 24.04)
└── docker compose project "catalyst-bot"   private network, nothing published to the internet
    ├── ib-gateway   ghcr.io/gnzsnz/ib-gateway:stable  = IB Gateway 10.50.1f + IBC 3.24.2 + Xvfb + x11vnc + socat
    │                API inside the network: ib-gateway:4003 (live) / ib-gateway:4004 (paper)
    │                (socat relays them to the gateway's loopback-only 4001 / 4002);  VNC: 127.0.0.1:5900 on the VPS
    └── bot          built from ./Dockerfile, IBKR_HOST=ib-gateway IBKR_PORT=4003, env from .env,
                     volumes: config.yaml + macro_events.yaml (read-only), state/, logs/
```

What was verified and what was not: the image tag, variable names, ports and the socat relay come from the image's
README (https://github.com/gnzsnz/ib-gateway-docker, `master`, read 2026-11-08) and its `run.sh`/`common.sh`; the
2FA, daily-restart and Sunday behaviour from the IBC user guide (https://github.com/IbcAlpha/IBC/blob/master/userguide.md)
and IBKR's rules. None of it was run against a real IBKR login from this sandbox, so treat the first week as the
integration test and keep an eye on the logs (section 7).

## 1. VPS

- **2 vCPU / 4 GB RAM / 20 GB disk is plenty.** The gateway's Java heap is 768 MB by default, the bot is small.
  x86-64 or arm64 (the image is multi-arch).
- Ubuntu 22.04 or 24.04, a non-root user with sudo, SSH key login. Any provider; a US-East region keeps latency to
  IBKR low. Uptime matters more than speed: no spot/preemptible instances.
- Clock: the bot schedules in America/New_York; the setup script turns NTP on. The containers carry their own TZ.

## 2. Install and configure

As your sudo user on the fresh VPS:

```bash
curl -fsSL https://raw.githubusercontent.com/jakebarnard9-dev/catalyst-bot/main/deploy/cloud-setup.sh | bash
# or: git clone https://github.com/jakebarnard9-dev/catalyst-bot.git && bash catalyst-bot/deploy/cloud-setup.sh
```

First run: installs Docker + compose plugin, clones into `~/catalyst-bot`, creates `.env` from `.env.example` and
**stops**, listing what to fill in. Edit `~/catalyst-bot/.env`:

| Variable | Value |
|---|---|
| `ANTHROPIC_API_KEY` | the Claude analyst (optional; without it the bot trades on quant signals with a higher score bar) |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` and/or `DISCORD_WEBHOOK_URL` | notifications: on a headless box this is how you see fills, exits and errors |
| `LIVE_TRADING_ACK` | `I_UNDERSTAND_THE_RISK` (`config.yaml` is `mode: live`; the bot refuses to start without it) |
| `TWS_USERID`, `TWS_PASSWORD` | the IBKR username/password the gateway logs in with |
| `TRADING_MODE` | `live` (real account). Dry run: `paper` with your paper-trading username/password |
| `IBKR_PORT` | `4003` with `live`, `4004` with `paper`; the bot uses it to reach the gateway |
| `VNC_SERVER_PASSWORD` | any password: enables the VNC view of the gateway (section 5); empty = off |
| `AUTO_RESTART_TIME`, `TIME_ZONE` | `11:45 PM`, `America/New_York`: daily restart at a quiet hour (section 4) |
| `TWOFA_TIMEOUT_ACTION`, `RELOGIN_AFTER_TWOFA_TIMEOUT` | `restart`, `yes` (section 4) |
| `READ_ONLY_API=no`, `TWS_ACCEPT_INCOMING=accept`, `BYPASS_WARNING=yes` | keep: the bot cannot trade without them |

Keys, FMP/Finnhub/Tavily and everything else are unchanged from the desktop setup. For a paper dry run without
editing `config.yaml`, add `BOT_MODE=paper` to `.env` (the bot's `mode` override) next to `TRADING_MODE=paper` /
`IBKR_PORT=4004`.

Run the script again. It starts the gateway, tells you to approve the IB Key push, builds the bot image and starts the
bot once the gateway is logged in. Later runs of the script are no-ops apart from `up -d --build`.

## 3. First start, step by step (what the script does, if you prefer to do it by hand)

```bash
cd ~/catalyst-bot
docker compose -f docker-compose.cloud.yml up -d ib-gateway      # pull + start the gateway
docker compose -f docker-compose.cloud.yml logs -f ib-gateway    # watch IBC log in
```
Within a minute IBC fills in the login form and your phone shows an **IBKR Mobile (IB Key) notification: approve it**.
The log then shows the login completing and `docker compose -f docker-compose.cloud.yml ps` reports the gateway
**healthy** (the health check is "the gateway's API port is listening", which only happens after a successful login).

```bash
docker compose -f docker-compose.cloud.yml up -d --build          # build the bot image, start the bot
docker compose -f docker-compose.cloud.yml logs -f bot
```
The bot waits for the gateway to be healthy (if you took more than ~20 minutes to approve the push, compose gives up
with "dependency failed to start": approve it and run `up -d` again). Expect
`IBKR connected (ib-gateway:4003 client 17), market data type 1` and the start-up notification on Telegram/Discord.
Then, during market hours, run the doctor (section 6): every line must be PASS, including *broker option quote*.

## 4. Two-factor authentication with a headless gateway

- Enroll the **IBKR Mobile app (IB Key)** as your second factor. SMS and security-card codes cannot be typed into a
  headless gateway; with IB Key, IBC fills in username and password and the push approval is the only manual step.
- **Daily:** at `AUTO_RESTART_TIME` (23:45 ET as configured) the gateway restarts *re-using the session*: no login,
  no push (upstream README: "does not require daily 2FA validation"). The bot logs `IBKR not connected; reconnecting` on its next
  tick and carries on; at 23:45 there is nothing to do anyway.
- **Weekly:** IBKR expires the session every Sunday (first run after 01:00 ET on Sunday needs a full login), so the
  **Sunday 23:45 restart sends an IB Key push: approve it.** That is the one recurring chore. If you miss it,
  `TWOFA_TIMEOUT_ACTION=restart` + `RELOGIN_AFTER_TWOFA_TIMEOUT=yes` make IBC start the login over (a new push) after
  IBKR's ~3-minute window, again and again, until you approve. Nothing trades meanwhile; the bot retries every 30 s and
  sends one `cannot start ... is TWS/IB Gateway running and logged in?` notification. Approve before Monday 09:30 ET if
  positions are open over the weekend, and in any case before the 12:30 scan.
- Any other gateway restart (`docker compose restart ib-gateway`, image update, VPS reboot) is a full login = a push.
- Do not log in to TWS or IB Gateway elsewhere with the same username while this runs: with
  `EXISTING_SESSION_DETECTED_ACTION=primary` the gateway's login takes over any existing session of that username, and
  a later TWS login elsewhere can bump the gateway in turn. Create a second username in Client Portal for manual use;
  Client Portal and the mobile app themselves do not interfere.

## 5. Watching the gateway (VNC over an SSH tunnel, never exposed)

Set `VNC_SERVER_PASSWORD` in `.env` (and `up -d` once). Then, from your laptop:

```bash
ssh -N -L 5900:127.0.0.1:5900 you@your-vps        # leave this running
```
and open `localhost:5900` in a VNC client (TigerVNC/RealVNC; macOS: Finder > Go > Connect to Server >
`vnc://localhost:5900`) with the VNC password. You get the gateway window: login dialog, API settings, any warning
pop-up. Port 5900 is bound to 127.0.0.1 on the VPS by `docker-compose.cloud.yml`; keep it that way.

## 6. Day-to-day commands

Run from `~/catalyst-bot`. `alias dc='docker compose -f docker-compose.cloud.yml'` saves typing.

| What | Command |
|---|---|
| containers + gateway health | `docker compose -f docker-compose.cloud.yml ps` |
| connectivity / config checks | `docker compose -f docker-compose.cloud.yml exec bot earnings-bot doctor` |
| account, positions, last decisions | `docker compose -f docker-compose.cloud.yml exec bot earnings-bot status` |
| sell every open position now | `docker compose -f docker-compose.cloud.yml exec bot earnings-bot flatten --yes` |
| clear the equity-floor halt / streak pause | `docker compose -f docker-compose.cloud.yml exec bot earnings-bot reset-kill` |
| this week's calendar as the bot sees it | `docker compose -f docker-compose.cloud.yml exec bot earnings-bot preview --deep` |
| stop / start the bot only | `docker compose -f docker-compose.cloud.yml stop bot` / `start bot` |
| restart the gateway (= new login, new push) | `docker compose -f docker-compose.cloud.yml restart ib-gateway` |
| stop everything | `docker compose -f docker-compose.cloud.yml down` |

**One API connection per client id.** IB Gateway accepts one connection per client id. The bot connects with `ibkr.client_id` (17); `doctor`, `status`, `flatten --yes` and `preview --deep` automatically use the next id (18), so they can run while the bot is up. Only a second *bot* instance would collide (ib_async then logs `Peer closed connection. clientId 17 already in use?`).

```bash
docker compose -f docker-compose.cloud.yml exec bot earnings-bot status        # while the bot container runs
docker compose -f docker-compose.cloud.yml run --rm bot earnings-bot doctor    # while the bot container is stopped
```
(`exec` needs the running container; `run --rm` uses the same image, env and volumes while it is stopped.)
`reset-kill` only touches the database and is fine at any time.

## 7. Logs and state

- `docker compose -f docker-compose.cloud.yml logs -f bot` (container stdout, rotated at 5 x 10 MB) or
  `tail -f logs/bot.log` (the bot's own rotating file, same content). `grep -E "WARNING|ERROR" logs/bot.log` for a
  quick scan; `-v` on the CLI commands for debug output.
- `docker compose -f docker-compose.cloud.yml logs -f ib-gateway`: IBC's login steps, the 2FA wait, socat start,
  the API port in use. The gateway's own files stay inside the container unless you set `TWS_SETTINGS_PATH`.
- State: `state/earnings_bot.sqlite` (every candidate, position, fill, decision). Back it up with
  `cp state/earnings_bot.sqlite backup-$(date +%F).sqlite`. The bot is restart-safe: fills are persisted before
  positions and each daily phase runs at most once.
- Optional: keep gateway settings across restarts by setting `TWS_SETTINGS_PATH=/home/ibgateway/tws_settings` and
  adding `- ./tws_settings:/home/ibgateway/tws_settings` to the `ib-gateway` volumes; the container runs as uid 1000,
  so `mkdir tws_settings && sudo chown 1000:1000 tws_settings` first.

## 8. Updating

```bash
cd ~/catalyst-bot && git pull
docker compose -f docker-compose.cloud.yml up -d --build        # rebuilds the bot image, restarts only the bot
```
`config.yaml` and `macro_events.yaml` are bind-mounted, so edits to them need only
`docker compose -f docker-compose.cloud.yml restart bot` (no rebuild). `.env` is read when a container is created,
so after editing it run `docker compose -f docker-compose.cloud.yml up -d` (`restart` does not re-read it); if you
changed gateway variables this recreates the gateway too, which means a new login and a push. To update the gateway
image:
`docker compose -f docker-compose.cloud.yml pull ib-gateway && docker compose -f docker-compose.cloud.yml up -d`;
that restarts the gateway, which means a fresh login and a push, so do it outside market hours. To pin a version,
change the tag (`stable`, `10.50`, `10.50.1f`; `latest` is the newer 10.51 channel).

## 9. Security

- **Firewall: only SSH open.** `sudo ufw allow OpenSSH && sudo ufw enable` (use your port if SSH is not on 22).
  Nothing else needs to be reachable: notifications and market data are outbound.
- **Never expose 4001/4003 (or 4002/4004) or 5900 to the internet.** The IB API is an unencrypted, unauthenticated
  TCP socket: anyone who can reach it can trade your account. `docker-compose.cloud.yml` publishes no API port at all
  (the bot talks to `ib-gateway` over the private compose network) and binds VNC to `127.0.0.1`. Keep it that way:
  Docker publishes ports by rewriting iptables directly and bypasses ufw, so a `"4003:4003"` mapping would be open
  even with the firewall on.
- SSH: keys only (`PasswordAuthentication no`), `fail2ban` and `unattended-upgrades` are cheap insurance.
- `.env` holds the IBKR password and your API keys: it is `chmod 600`, git-ignored, excluded from the Docker image by
  `.dockerignore`, and both containers read it. Do not copy it anywhere else. If you want the password out of
  `docker inspect` output, the image also accepts `TWS_PASSWORD_FILE` with a compose `secrets:` entry (see its README).
- The equity-floor kill switch, caps and limits in `config.yaml` are the real safety net; `earnings-bot flatten --yes`
  and `docker compose -f docker-compose.cloud.yml stop bot` are the manual ones. Stopping the bot never closes
  positions by itself.

## 10. If something is wrong

| Symptom | Cause / fix |
|---|---|
| gateway never becomes healthy; log loops on the login | wrong `TWS_USERID`/`TWS_PASSWORD`, or the push was not approved: watch `logs -f ib-gateway`, approve the next push; wrong credentials also show up in VNC |
| bot: `cannot connect to TWS/Gateway at ib-gateway:4003` | gateway not logged in yet (wait), or `TRADING_MODE`/`IBKR_PORT` mismatch (live/4003, paper/4004) |
| bot exits at once with `mode=live requires LIVE_TRADING_ACK` | set `LIVE_TRADING_ACK=I_UNDERSTAND_THE_RISK` in `.env`, `restart bot` |
| `no live market` on every name | no OPRA subscription, or market closed (`GO_LIVE.md` step 2) |
| orders `rejected` | options permission not approved, funds unsettled, or `READ_ONLY_API` not `no` |
| a CLI command fails with `cannot connect`, log says `clientId 17 already in use?` | the bot is connected: stop it first (section 6) |
| gateway dropped mid-day, bot reconnecting | someone logged in with the same username elsewhere (section 4), or IBKR maintenance; it re-logs in, approve the push if asked |
