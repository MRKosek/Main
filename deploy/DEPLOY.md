# Deploying on a VPS (24/7, paper)

Any small Linux VPS works: 1 vCPU / 1 GB RAM is plenty (Hetzner, DigitalOcean, Linode,
Lightsail, ...). These steps assume Ubuntu 24.04. Pick a region in the US east (closer to the
exchange) if you can, though for daily bars it barely matters.

## 1. Harden the box (10 min)

```sh
# on your laptop
ssh root@YOUR_VPS_IP

# on the VPS
adduser bot && usermod -aG sudo bot
mkdir -p /home/bot/.ssh && cp ~/.ssh/authorized_keys /home/bot/.ssh/ && chown -R bot:bot /home/bot/.ssh
# SSH keys only, no root login
sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/; s/^#\?PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl restart ssh
ufw allow OpenSSH && ufw enable          # only SSH is open; the dashboard is never exposed
apt update && apt -y upgrade && apt -y install unattended-upgrades docker.io docker-compose-v2 git
usermod -aG docker bot
timedatectl set-timezone America/New_York
```

Log back in as `bot` from now on: `ssh bot@YOUR_VPS_IP`.

## 2. Create the Telegram bot (BotFather)

1. In Telegram, open **@BotFather** → `/newbot` → pick a name and a username ending in `bot`.
2. BotFather replies with a token like `123456:ABC...`. Put it in `.env` as `TELEGRAM_BOT_TOKEN`.
3. Open your new bot in Telegram and send it any message (e.g. `hi`).
4. Run `docker compose -f deploy/docker-compose.yml run --rm bot python -m bot.notify` (after step 3). It prints `TELEGRAM_CHAT_ID=...`. Put that in `.env`.
5. Only that chat can control the bot. Messages from anyone else are ignored.

Commands the bot understands: `/status` · `/approve <id>` · `/reject <id>` · `/kill` · `/resume`.

You'll get a message for: every fill, every order error, every approval request, every Jev veto,
every kill-switch event, and the daily report at 16:15 ET.

## 3. Install and configure

```sh
git clone <your repo url> ~/trading-bot && cd ~/trading-bot
cp .env.example .env && chmod 600 .env && nano .env    # paper keys, Jev key, Telegram, DASHBOARD_TOKEN
mkdir -p data reports && sudo chown -R 10001:10001 data reports strategy.md   # the container runs as uid 10001
```

## 4. Backtest, then start

```sh
docker compose -f deploy/docker-compose.yml run --rm bot python -m bot.backtest --write
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f bot          # you should get "Bot started (PAPER)" on Telegram
docker compose -f deploy/docker-compose.yml run --rm bot python -m bot.runner --test-kill-switch
```

`restart: unless-stopped` brings both containers back after crashes and reboots.

## 5. Open the dashboard (SSH tunnel, never public)

```sh
# on your laptop
ssh -N -L 8000:localhost:8000 bot@YOUR_VPS_IP
# then browse to
http://localhost:8000/?token=YOUR_DASHBOARD_TOKEN
```

The "dashboard link" is that URL. It only works through your tunnel. If you want it on your
phone without a tunnel, put it behind Tailscale or Cloudflare Access rather than opening a port.

## Operations

| Task | Command |
|---|---|
| Emergency stop (any of these) | `/kill` on Telegram · dashboard button · `touch data/KILL` on the VPS |
| Resume after a kill | `rm -f data/KILL`, then `/resume` on Telegram |
| Change rules | edit `strategy.md`, then `docker compose -f deploy/docker-compose.yml restart` |
| Update code | `git pull && docker compose -f deploy/docker-compose.yml up -d --build` |
| Back up state | `cp data/bot.sqlite3 ~/backup-$(date +%F).sqlite3` |
| Daily reports | `reports/daily-YYYY-MM-DD.json` (also sent to Telegram) |

Without Docker: create a venv, `pip install -r requirements.txt`, and use
`deploy/trading-bot.service` and `deploy/trading-dashboard.service` with systemd.
