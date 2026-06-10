# Deployment (VPS) — Phase 14

Runs the bot unattended on a VPS in **paper** mode under systemd, with
auto-restart and a Telegram heartbeat. Live trading stays disabled until the
go-live gate (see `GO_LIVE_CHECKLIST.md`).

## 1. Prerequisites

- Python 3.11+ and a virtualenv at `/root/CryptoTradeWisBot/.venv`
- **Microsoft ODBC Driver 18 for SQL Server** (`odbcinst -q -d` to verify)
- Reachable SQL Server; `.env` containing only `DATABASE_URL`
- Dependencies installed: `.venv/bin/pip install -r requirements.txt`
- DB initialised + seeded: `.venv/bin/python -m crypto_bot.schema`
- Alpaca **paper** keys + Telegram token/chat id set in `app_config`
  (`config_store.set(...)`; `python -m crypto_bot.alerts.telegram resolve`)

## 2. Install the service

```bash
sudo cp deploy/cryptotradewisbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cryptotradewisbot      # start now + on boot
systemctl status cryptotradewisbot --no-pager
```

Logs:

```bash
journalctl -u cryptotradewisbot -f                 # service stdout/stderr
tail -f logs/crypto_bot.log                         # app log (rotating)
```

The orchestrator runs one full cycle just after each bar close (HH:01 UTC) and
sends a Telegram heartbeat every 6 hours.

## 3. Reboot-safe

`Restart=always` + `WantedBy=multi-user.target` means the bot relaunches on
crash and on reboot. Verify with `sudo reboot`, then after boot:

```bash
systemctl is-active cryptotradewisbot
journalctl -u cryptotradewisbot --since "5 min ago"
```

## 4. Manual controls

```bash
sudo systemctl stop cryptotradewisbot
sudo systemctl restart cryptotradewisbot
.venv/bin/python -m crypto_bot.orchestrator once       # one cycle by hand
.venv/bin/python -m crypto_bot.orchestrator heartbeat  # send a heartbeat now
```

## 5. Log rotation

The app rotates its own log (RotatingFileHandler, 5 MB x 5). For OS-level
rotation instead, see `deploy/logrotate.cryptotradewisbot`.

## 6. Go-live

**Do not enable live trading until every item in `GO_LIVE_CHECKLIST.md` passes**,
including Phase 15 (encrypted keys). Then, and only then, flip
`LIVE_TRADING_ENABLED=true` and point `ALPACA_BASE_URL` at the live endpoint, and
start with the smallest possible size.
