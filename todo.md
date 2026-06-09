# Crypto Swing-Trading Bot — Build TODO (Phase by Phase)

> Work **top to bottom**. Finish and verify a phase (its "Done when" box checked) before starting
> the next. Read `summary.md` first — it holds the full spec, schema, and parameter defaults.
> Default to Alpaca's **PAPER** endpoint everywhere until Phase 14.

**Legend:** `[ ]` todo · `[x]` done
**Global definition of done (every phase):** code committed, has at least one test or a manual
verification note, no secrets hard-coded in source, and it runs without errors against the DB.

---

## Phase 0 — Project setup & scaffolding
**Goal:** a runnable skeleton with DB connectivity and logging.
**Depends on:** nothing.

- [ ] Init git repo, `pyproject.toml`/`requirements.txt`, Python 3.11 venv
- [ ] Add deps: `alpaca-py`, `pyodbc`, `sqlalchemy`, `pandas`, `numpy`, `pandas-ta`, `python-telegram-bot`, `apscheduler`, `pytest`, `python-dotenv`
- [ ] Create the module layout from `summary.md` §4 (empty stubs)
- [ ] `.env` holds **only** the SQL Server connection string; add `.env` to `.gitignore`
- [ ] `db.py`: SQLAlchemy engine + session factory; test the connection
- [ ] Central logging (rotating file + console), UTC timestamps
- [ ] `README.md` with setup steps

**Done when:** `python -c "from crypto_bot.db import test_connection; test_connection()"` prints success.

---

## Phase 1 — Database schema + config_store
**Goal:** all tables exist; credentials/config read through one isolated module.
**Depends on:** Phase 0.

- [ ] Define all tables from `summary.md` §6 (ORM models in `models.py`)
- [ ] Migration/create script (idempotent: create-if-not-exists)
- [ ] `config_store.py`: `get(key)` / `set(key, value, is_secret=False)` — **the only** code that touches `app_config`. (This is the seam that Phase 15 will encrypt.)
- [ ] Seed `app_config` with Alpaca keys, base URL (paper), Telegram token/chat id, and all params from `summary.md` §7
- [ ] Seed `watchlist` with starter crypto pairs (e.g. BTC/USD, ETH/USD, SOL/USD)
- [ ] Add indexes on `(symbol, timeframe, ts)` for `market_bars`/`indicators`

**Done when:** schema creates cleanly on a fresh DB; `config_store.get('ALPACA_BASE_URL')` returns the paper URL; watchlist query returns the seeded symbols.

---

## Phase 2 — Market-data ingestion
**Goal:** pull crypto OHLCV from Alpaca into `market_bars`.
**Depends on:** Phase 1.

- [ ] Alpaca crypto data client built from `config_store` keys
- [ ] Fetch historical bars for each active watchlist symbol (1H, 4H, 1D)
- [ ] Upsert into `market_bars` (dedupe on the unique key; UTC timestamps)
- [ ] Incremental fetch (only bars newer than the latest stored)
- [ ] Handle rate limits, retries, and empty responses gracefully

**Done when:** `market_bars` is populated for all symbols/timeframes and re-running adds no duplicates.

---

## Phase 3 — Indicator engine
**Goal:** compute every indicator the strategy needs.
**Depends on:** Phase 2.

- [ ] EMAs: 8/10/20 and 21/34/55 (EMA, not SMA)
- [ ] ATR(14), RSI(14), ADX(14), 20-bar volume average
- [ ] Return a tidy DataFrame keyed by `(symbol, timeframe, ts)`; optionally cache to `indicators`
- [ ] Unit test EMA/RSI/ATR against a tiny known fixture

**Done when:** indicator values match a hand-computed fixture within rounding tolerance.

---

## Phase 4 — Support/Resistance detection
**Goal:** produce ranked S/R levels per symbol.
**Depends on:** Phase 3.

- [ ] Swing-pivot detection (strength `PIVOT_STRENGTH`, confirmed-only — no repaint)
- [ ] Cluster pivots into zones within `CLUSTER_TOLERANCE_PCT`; track touch_count + span
- [ ] Volume Profile: POC, VAH, VAL
- [ ] Rank levels by strength; persist nearest support + resistance to `sr_levels`
- [ ] Mark stale levels inactive

**Done when:** on a known chart, detected levels visually line up with obvious highs/lows and POC; levels are stable run-to-run.

---

## Phase 5 — Signal generation
**Goal:** emit BUY/HOLD/AVOID with the correct `signal_source`.
**Depends on:** Phase 4.

- [ ] **Breakout check** (`analysis/breakout.py`): close beyond level + `max(BREAKOUT_BUFFER_PCT, 1×ATR)`, volume ≥ `VOLUME_MULT`× avg (or ATR-expansion proxy), strong body. Support `aggressive` vs `retest` mode.
- [ ] **Overextension filter:** reject if distance/extension too high, RSI > `RSI_OVERBOUGHT`, or R:R to next resistance < `MIN_RR`
- [ ] **Triple-MA** (`analysis/moving_avg.py`): long set (21/34/55) on `TREND_TF` = gate; short set (8/10/20) on `TRIGGER_TF` = trigger; require `ADX_MIN`; output BUY/HOLD/AVOID
- [ ] **Combine:** BUY only if trend filter bullish AND (breakout OR MA trigger) AND overextension passed AND R:R ≥ `MIN_RR`
- [ ] Set `signal_source` = `BREAKOUT` / `MA` / `BOTH` based on what fired

**Done when:** on historical data the engine produces sensible BUYs only in confirmed uptrends and correctly tags `signal_source`.

---

## Phase 6 — Confidence scoring
**Goal:** a 0–100 score with an auditable breakdown.
**Depends on:** Phase 5.

- [ ] Implement the weighted scorer (`analysis/scorer.py`) per `summary.md` §5.8
- [ ] Gate: only `acted` candidates scoring ≥ `CONFIDENCE_THRESHOLD`
- [ ] Write each signal to `signals` with `confidence_score` + `score_breakdown` (JSON)
- [ ] Below-threshold signals still recorded (signal='HOLD'/'AVOID', acted=0) for analysis

**Done when:** every generated signal has a score and a stored per-factor breakdown that sums correctly.

---

## Phase 7 — Risk & position sizing
**Goal:** turn a qualified signal into qty + stop + target within caps.
**Depends on:** Phase 6.

- [ ] `qty = (equity × risk_pct) / (entry − stop)` using **current** equity (from `account_snapshots`)
- [ ] Conviction scaling: `tiered` (default) and `kelly` modes (capped at `MAX_RISK_PCT`)
- [ ] ATR-based stop (`ATR_STOP_MULT`); target at next resistance / measured move
- [ ] Enforce `MAX_PORTFOLIO_HEAT_PCT`, `MAX_CONCURRENT_POSITIONS`, and the drawdown brake
- [ ] Correlation guard (treat highly-correlated open positions as shared heat)

**Done when:** for sample signals the sizer returns correct qty/stop/target and refuses trades that breach any cap.

---

## Phase 8 — Backtesting harness  ⟵ validate BEFORE real orders
**Goal:** simulate the full strategy on history.
**Depends on:** Phase 7.

- [ ] Event-driven loop over historical bars feeding Phases 3–7
- [ ] Simulate fills with fees + slippage assumptions; apply exits (Phase 10 logic stub or inline)
- [ ] Metrics: win rate, avg R, profit factor, max drawdown, equity curve, trade count
- [ ] Report expectancy **by confidence bucket** (higher buckets should perform better; if not, flatten the conviction multiplier)
- [ ] Save results for comparison across parameter tweaks

**Done when:** a 100+ trade backtest runs end-to-end and produces a metrics report; results are plausible (not obviously broken).

---

## Phase 9 — Order execution + position tracking
**Goal:** place real (paper) orders and record them.
**Depends on:** Phase 8. **Paper endpoint only.**

- [ ] `execution/broker.py`: submit market/limit + attached stop via Alpaca crypto
- [ ] Record every order to `trades` (`is_paper=1`, `alpaca_order_id`, `signal_id`, `confidence_score`, `signal_source`, `risk_pct`)
- [ ] Reconcile open positions with Alpaca on startup (recover from restarts)
- [ ] Idempotency / dedupe guard so one signal can't double-fire
- [ ] **Guardrail:** a `LIVE_TRADING_ENABLED` flag in `app_config` defaulting to `false`

**Done when:** a qualified signal places a paper order, the position appears in Alpaca, and a matching `trades` row is written.

---

## Phase 10 — Exit management
**Goal:** manage open trades to completion.
**Depends on:** Phase 9.

- [ ] Stop-loss + take-profit enforcement
- [ ] Partial scale-out: 50% at 1R → stop to breakeven; 25% at 2R; trail final 25%
- [ ] Chandelier trailing stop (`CHANDELIER` params), ratchet-only
- [ ] Time stop (`TIME_STOP_BARS`)
- [ ] On close, update `trades`: `exit_price`, `exit_time`, `realized_pnl`, `realized_pnl_pct`, `status='CLOSED'`

**Done when:** a simulated/paper trade hits each exit type correctly and the trade row closes with accurate P&L.

---

## Phase 11 — Daily summary & P&L
**Goal:** populate `daily_summary` from trades.
**Depends on:** Phase 10.

- [ ] End-of-day job: per-symbol rows + a portfolio-total row (symbol NULL)
- [ ] Compute num_buys/num_sells, realized_pnl + %, unrealized_pnl, win/loss counts
- [ ] Write/refresh `account_snapshots` (equity, cash, heat, drawdown)
- [ ] Idempotent on the unique `(trade_date, symbol)` key

**Done when:** querying `daily_summary` for today returns accurate buy/sell counts and % P&L.

---

## Phase 12 — Telegram alerts
**Goal:** notify on every meaningful event.
**Depends on:** Phases 9–11.

- [ ] `alerts/telegram.py` using token/chat id from `config_store`
- [ ] Alerts: new qualified signal (symbol, score, source, entry/stop/target), entry filled, exit filled (with P&L %), end-of-day summary, errors/exceptions
- [ ] Throttle/format so it isn't spammy
- [ ] Failures in alerting must never crash the trading loop

**Done when:** a test run delivers formatted messages to the configured chat for each event type.

---

## Phase 13 — Orchestration & scheduling
**Goal:** one command runs the whole cycle on a schedule.
**Depends on:** Phases 2–12.

- [ ] `orchestrator.py` runs the full pipeline once (ingest → indicators → S/R → signal → score → size → execute → manage → log to `bot_runs`)
- [ ] Schedule on bar close (e.g. top of each hour) via APScheduler or systemd timer
- [ ] Graceful error handling per symbol (one bad symbol can't kill the run)
- [ ] Single-instance lock (no overlapping runs)

**Done when:** the scheduler runs unattended for several cycles, logging each to `bot_runs`, with no crashes.

---

## Phase 14 — Paper validation → VPS go-live
**Goal:** prove it on paper, then deploy.
**Depends on:** Phase 13.

- [ ] Run **paper** for a defined period; compare live behavior (slippage, fills) to backtest assumptions
- [ ] Provision VPS; install Python + ODBC Driver 18; secure DB connectivity
- [ ] Process manager (systemd service / supervisor / pm2) with auto-restart
- [ ] Log rotation, disk/CPU monitoring, a heartbeat Telegram ping
- [ ] **Go-live gate:** only after paper results are acceptable AND Phase 15 is done — flip `LIVE_TRADING_ENABLED` to true and point `ALPACA_BASE_URL` to live. **Start with the smallest possible size.**

**Done when:** the bot runs continuously on the VPS in paper mode and survives a reboot.

---

## Phase 15 — Security hardening (deferred, NOT skipped)
**Goal:** stop storing usable secrets in plaintext.
**Depends on:** can be done any time; **required before live funds.**

- [ ] Encrypt `is_secret` rows in `app_config` (e.g. `cryptography.Fernet`) — change lives entirely inside `config_store.py`
- [ ] Move the master key OUT of the DB (env var / OS keyring / cloud secret manager)
- [ ] Restrict SQL Server permissions (least-privilege app user; no broad read on the secrets table)
- [ ] Optionally migrate secrets to a dedicated secret manager (Azure Key Vault / AWS Secrets Manager / Vault)
- [ ] Rotate the Alpaca keys after development is finished
- [ ] Audit logs for credential access

**Done when:** no readable API key exists in the database, and the bot still authenticates via `config_store`.

---

## Appendix — pre-flight checklist before flipping to live
- [ ] Backtest expectancy positive and drawdown tolerable
- [ ] Confidence buckets show monotonic-ish improvement
- [ ] Paper period completed and reconciled
- [ ] Security (Phase 15) complete; keys rotated
- [ ] Hard caps verified (per-trade 2%, heat 6%, max 5 positions, drawdown brake)
- [ ] Telegram alerts confirmed working
- [ ] VPS restart-safe; single-instance lock active
- [ ] Starting size is the smallest you can place
