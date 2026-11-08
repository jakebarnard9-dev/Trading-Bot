#!/usr/bin/env bash
# Fresh Ubuntu 22.04/24.04 VPS -> bot + headless IB Gateway under Docker. Safe to re-run: each step is skipped when
# already done, and the script stops (exit 1) until .env is filled in. Full walk-through: DEPLOY_CLOUD.md
#
#   curl -fsSL https://raw.githubusercontent.com/jakebarnard9-dev/catalyst-bot/main/deploy/cloud-setup.sh | bash
#   # or, from a checkout:  bash deploy/cloud-setup.sh
#
# Overrides: REPO_URL (git URL to clone), BOT_DIR (checkout directory, default ~/catalyst-bot).
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/jakebarnard9-dev/catalyst-bot.git}"
COMPOSE_FILE="docker-compose.cloud.yml"
if [ -z "${BOT_DIR:-}" ]; then
  # run from inside a checkout -> use that checkout; otherwise clone into ~/catalyst-bot
  here="$(cd "$(dirname "${BASH_SOURCE[0]:-/nonexistent}")" 2>/dev/null && pwd || true)"
  if [ -n "$here" ] && [ -f "$here/../$COMPOSE_FILE" ]; then BOT_DIR="$(cd "$here/.." && pwd)"; else BOT_DIR="$HOME/catalyst-bot"; fi
fi

say() { printf '\n==> %s\n' "$*"; }
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || { echo "ERROR: run as root or install sudo" >&2; exit 1; }
  SUDO="sudo"
fi
. /etc/os-release 2>/dev/null || true
[ "${ID:-}" = "ubuntu" ] || echo "warning: written for Ubuntu 22.04/24.04, detected: ${PRETTY_NAME:-unknown}"

# ---- 1. Docker Engine + compose plugin, from Docker's apt repository (docs.docker.com/engine/install/ubuntu)
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  say "Docker already installed: $(docker --version)"
else
  say "Installing Docker Engine and the compose plugin"
  export DEBIAN_FRONTEND=noninteractive
  $SUDO apt-get update -y
  $SUDO apt-get install -y ca-certificates curl git
  $SUDO install -m 0755 -d /etc/apt/keyrings
  $SUDO curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  $SUDO chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-${VERSION_CODENAME:-noble}} stable" \
    | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null
  $SUDO apt-get update -y
  $SUDO apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  $SUDO systemctl enable --now docker
fi
command -v git >/dev/null 2>&1 || $SUDO apt-get install -y git
me="${USER:-$(id -un)}"
if [ -n "$SUDO" ] && ! id -nG "$me" | grep -qw docker; then
  $SUDO usermod -aG docker "$me"
  echo "added $me to the docker group (effective at your next login; this run uses sudo for docker)"
fi
DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="$SUDO docker"
$SUDO timedatectl set-ntp true >/dev/null 2>&1 || true   # the bot's schedule and IBKR both need a correct clock

# ---- 2. Code
if [ -f "$BOT_DIR/$COMPOSE_FILE" ]; then
  say "Using checkout $BOT_DIR (update later with: git -C $BOT_DIR pull)"
else
  say "Cloning $REPO_URL into $BOT_DIR"
  git clone "$REPO_URL" "$BOT_DIR"
fi
cd "$BOT_DIR"
mkdir -p state logs

# ---- 3. .env
envval() {  # value of KEY in .env (last occurrence), quotes and surrounding whitespace stripped; .env is never sourced
  { grep -E "^[[:space:]]*$1=" .env || true; } | tail -n 1 | cut -d= -f2- \
    | sed -e 's/^[[:space:]]*//;s/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}
if [ ! -f .env ]; then
  cp .env.example .env && chmod 600 .env
  cat <<MSG

Created $BOT_DIR/.env from .env.example. Fill it in (nano $BOT_DIR/.env), then run this script again.
  Required
    TWS_USERID / TWS_PASSWORD   IBKR login for the gateway (live account; or your paper-trading login with TRADING_MODE=paper)
    TRADING_MODE / IBKR_PORT    live + 4003 (as shipped) or paper + 4004 -- they must match
    LIVE_TRADING_ACK            I_UNDERSTAND_THE_RISK -- config.yaml is mode: live and the bot refuses to start without it
    ANTHROPIC_API_KEY           Claude analyst (without it the bot trades on quant signals only, with a higher score bar)
  Recommended
    TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID and/or DISCORD_WEBHOOK_URL   fills, exits, errors (you are not watching a screen)
    VNC_SERVER_PASSWORD         lets you look at the gateway screen through an SSH tunnel (DEPLOY_CLOUD.md section 5)
MSG
  exit 1
fi
chmod 600 .env
missing=()
for v in TWS_USERID TWS_PASSWORD; do [ -n "$(envval "$v")" ] || missing+=("$v"); done
if grep -Eq '^mode:[[:space:]]*live' config.yaml && [ "$(envval LIVE_TRADING_ACK)" != "I_UNDERSTAND_THE_RISK" ]; then
  missing+=("LIVE_TRADING_ACK=I_UNDERSTAND_THE_RISK (config.yaml has mode: live)")
fi
if [ "${#missing[@]}" -gt 0 ]; then
  echo; echo "Fill these in $BOT_DIR/.env and run this script again:"; printf '  - %s\n' "${missing[@]}"; exit 1
fi
[ -n "$(envval ANTHROPIC_API_KEY)" ] || echo "warning: ANTHROPIC_API_KEY is empty -- running without the Claude analyst"
mode="$(envval TRADING_MODE)"; mode="${mode:-paper}"; port="$(envval IBKR_PORT)"; port="${port:-4003}"
case "$mode:$port" in
  live:4003|paper:4004) ;;
  *) echo "warning: TRADING_MODE=$mode with IBKR_PORT=$port -- expected live/4003 or paper/4004; the bot will not reach the gateway" ;;
esac

# ---- 4. Start: gateway first (you approve the IB Key push on your phone), then build + start the bot
say "Starting IB Gateway (TRADING_MODE=$mode)"
$DOCKER compose -f "$COMPOSE_FILE" up -d ib-gateway
cat <<MSG
Watch the login:   $DOCKER compose -f $COMPOSE_FILE logs -f ib-gateway
Approve the IBKR Mobile (IB Key) notification on your phone when it arrives. The next step builds the bot image and
waits (up to about 20 minutes) for the gateway to become healthy, i.e. logged in with its API port open.
MSG
say "Building and starting the bot"
if ! $DOCKER compose -f "$COMPOSE_FILE" up -d --build; then
  echo "The gateway is not healthy yet (IB Key push not approved, or wrong credentials -- check the gateway log)."
  echo "Once it is logged in, run:  $DOCKER compose -f $COMPOSE_FILE up -d"
  exit 1
fi
say "Running. Next: $DOCKER compose -f $COMPOSE_FILE ps | logs -f bot; DEPLOY_CLOUD.md section 6 covers doctor / status / flatten."
