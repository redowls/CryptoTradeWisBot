# Go-Live Checklist

**Hard rule:** the bot stays in **paper** mode (`LIVE_TRADING_ENABLED=false`,
`ALPACA_BASE_URL` = paper) until *every* box below is checked. Flipping to live
before this is a money-losing risk. Start with the smallest size you can place.

## Strategy validation
- [ ] Backtest expectancy is positive and max drawdown is tolerable
      (current default params show **negative** expectancy — tune first).
- [ ] Confidence buckets show roughly monotonic improvement
      (higher score → better expectancy). If not, flatten the conviction multiplier.
- [ ] A meaningful **paper** period completed and reconciled against backtest
      assumptions (slippage, fills, fees).

## Risk controls (verify live values in app_config)
- [ ] Per-trade cap `MAX_RISK_PCT` = 2%
- [ ] Portfolio heat cap `MAX_PORTFOLIO_HEAT_PCT` = 6%
- [ ] `MAX_CONCURRENT_POSITIONS` = 5
- [ ] Drawdown brake `DRAWDOWN_BRAKE_PCT` active
- [ ] Correlation guard active

## Operations
- [ ] Telegram alerts confirmed working (signal/entry/exit/daily/error/heartbeat)
- [ ] systemd service `enable`d; survives reboot (`systemctl is-active`)
- [ ] Single-instance lock verified (no overlapping cycles)
- [ ] `bot_runs` shows clean cycles over the paper period (status OK)
- [ ] Disk/CPU healthy; log rotation working

## Security (Phase 15 — REQUIRED before real funds)
- [ ] `is_secret` rows in `app_config` are **encrypted** (no readable API key in the DB)
- [ ] Master key stored OUTSIDE the DB (env var / OS keyring / secret manager)
- [ ] SQL Server app user is least-privilege
- [ ] Alpaca keys **rotated** after development

## The flip (only after everything above)
1. Complete Phase 15 (encryption) and rotate keys.
2. `config_store.set("ALPACA_BASE_URL", "https://api.alpaca.markets")`
3. `config_store.set("LIVE_TRADING_ENABLED", "true")`
4. Set risk to the **minimum** and confirm the first live order is tiny.
5. `sudo systemctl restart cryptotradewisbot` and watch closely.
