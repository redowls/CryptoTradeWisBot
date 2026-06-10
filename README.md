# CryptoTradeWisBot

A Python bot that swing-trades **crypto** on **Alpaca** (paper by default) using a
**confluence engine**: algorithmic support/resistance + confirmed breakouts + a
triple-moving-average trend filter, fused into a **0–100 confidence score** that
drives conviction-scaled position sizing. State is persisted to **Microsoft SQL
Server**; alerts go to **Telegram**; it runs unattended on a **VPS**.

> **Read `summary.md` first** — it is the single source of truth (full spec, DB
> schema, parameter defaults). Build the project by working through `todo.md` one
> phase at a time. `research.md` documents the strategy rationale.

**Status:** Phase 0 (scaffolding) complete. Default to Alpaca's **PAPER** endpoint
until Phase 14. Credentials and tunable parameters live in the DB (`app_config`),
read exclusively through `config_store` — **the only secret in `.env` is the SQL
Server connection string.**

---

## Requirements

- **Python 3.11+**
- **Microsoft ODBC Driver 18 for SQL Server** (required by `pyodbc`)
  - Debian/Ubuntu: install Microsoft's `msodbcsql18` package (see Microsoft's
    "Install the ODBC driver" docs). Verify with `odbcinst -q -d`.
- A reachable **Microsoft SQL Server** instance (local, remote, or container).

## Setup

```bash
# 1. Clone, then create and activate a virtualenv
python3.11 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure the database connection (the ONLY thing .env holds)
cp .env.example .env
#   then edit .env and set DATABASE_URL — see ".env" comments for the exact format
```

### Configure the database

`.env` must contain a single `DATABASE_URL`, a SQLAlchemy URL for SQL Server via
pyodbc + ODBC Driver 18, for example:

```
DATABASE_URL=mssql+pyodbc://sa:YourStrong%40Passw0rd@localhost:1433/CryptoBot?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=yes
```

URL-encode special characters in the password (`@` → `%40`). `Encrypt=yes` is
required by Driver 18; `TrustServerCertificate=yes` is for self-signed dev certs
only — use a proper certificate in production.

## Verify (Phase 0 "Done when")

With `.env` configured and the venv active:

```bash
python -c "from crypto_bot.db import test_connection; test_connection()"
```

Expected output:

```
OK: connected to mssql+pyodbc://sa:***@localhost:1433/CryptoBot?...
    Microsoft SQL Server 2022 ...
```

Run the (DB-free) smoke tests anytime:

```bash
pytest
```

## Initialize the database (Phase 1)

Create all tables and seed config + the starter watchlist (idempotent — safe to
re-run; it never overwrites existing values):

```bash
python -m crypto_bot.schema
```

This seeds all tunable parameters (summary.md §7) and the credential keys as
**placeholders** (`REPLACE_ME`). No secrets live in source. Set the real values
through `config_store` (the only module that touches `app_config`):

```python
from crypto_bot import config_store as cs
cs.set("ALPACA_API_KEY", "your-paper-key", is_secret=True)
cs.set("ALPACA_SECRET",  "your-paper-secret", is_secret=True)
cs.set("TELEGRAM_BOT_TOKEN", "your-bot-token", is_secret=True)
cs.set("TELEGRAM_CHAT_ID", "your-chat-id")
```

`ALPACA_BASE_URL` defaults to the **paper** endpoint and `LIVE_TRADING_ENABLED`
to `false` — both stay that way until Phase 14.

## Ingest market data (Phase 2)

Fetch 1H/4H/1D crypto OHLCV for every active watchlist symbol into `market_bars`
(incremental + idempotent — re-running refreshes the latest bar and adds no
duplicates):

```bash
python -m crypto_bot.data.ingest
```

Historical crypto bars are a public Alpaca endpoint, so this works without keys;
when real keys are configured they're used automatically.

## Compute indicators (Phase 3)

Compute EMAs (8/10/20 + 21/34/55), ATR(14), RSI(14), ADX(14) and the 20-bar
volume average from `market_bars`, caching warmed rows to `indicators`:

```bash
python -m crypto_bot.indicators.engine
```

Indicators are hand-rolled (standard EMA + Wilder smoothing) and validated
against independent reference implementations in the test suite.

## Detect support/resistance (Phase 4)

Detect confirmed swing pivots, cluster them into zones, compute the Volume
Profile (POC/VAH/VAL), and persist the strongest supports/resistances to
`sr_levels` (re-running refreshes the active set; stale levels are deactivated):

```bash
python -m crypto_bot.analysis.levels
```

## Generate signals (Phase 5)

Combine the triple-MA trend filter, breakout confirmation, and overextension
filter into a BUY / HOLD / AVOID decision (tagged `BREAKOUT` / `MA` / `BOTH`)
for each watchlist symbol's latest bar:

```bash
python -m crypto_bot.analysis.signal_engine
```

A BUY requires: long-set (21/34/55) trend filter bullish on the trend timeframe
**AND** (a confirmed breakout **OR** the short-set 8/10/20 trigger) **AND** the
overextension filter passed (including R:R ≥ `MIN_RR`). The decision is computed
in-memory here; persisting it to `signals` with a 0–100 confidence score is
Phase 6.

## Score & store signals (Phase 6)

Score each decision 0–100 across six independent factors (trend 30 / breakout 20
/ volume 15 / trigger 15 / momentum 10 / volatility 10) and write it to the
`signals` table with the per-factor breakdown as JSON:

```bash
python -m crypto_bot.analysis.scorer
```

`acted=1` only when the signal is BUY **and** the score ≥ `CONFIDENCE_THRESHOLD`
(70). Below-threshold and non-BUY signals are still recorded (`acted=0`) for
analysis. Rows are upserted on `(symbol, ts)`, so re-running is idempotent.

## Position sizing (Phase 7)

Turn an actionable signal into a quantity within hard risk caps:

```bash
python -m crypto_bot.risk.sizing
```

`qty = (equity × risk%) / (entry − stop)`, risk% scaled by conviction (tiered
0.75/1.0/2.0% or fractional Kelly), capped at `MAX_RISK_PCT`. A trade is rejected
if it would breach `MAX_CONCURRENT_POSITIONS`, `MAX_PORTFOLIO_HEAT_PCT`, the
correlation cap, or if a position is already open in that symbol; risk is halved
while drawdown exceeds `DRAWDOWN_BRAKE_PCT`. Equity comes from the latest
`account_snapshots` row, falling back to `STARTING_EQUITY` (default 10,000).

## Backtest (Phase 8)

Event-driven walk over the TRIGGER-timeframe history, feeding Phases 3–7 with
only data up to each bar (no lookahead): indicators are causal, the trend
timeframe is attached via a backward as-of join, S/R is rebuilt periodically from
a bounded trailing window, entries fill at the next bar's open (slippage + fee),
and exits are stop / target / Chandelier trail / time-stop. Results (overall
metrics + expectancy by confidence bucket) are written to `backtest_results/`.

```bash
python -m crypto_bot.backtest.engine
```

> **Finding (default params, ~1y of 1H data across 19 crypto pairs, 102 trades):**
> negative expectancy (win rate ~23%, avg −0.35R, profit factor ~0.40) and the
> confidence buckets do **not** show higher buckets performing better. Per the
> spec this is the signal to **re-tune and flatten the conviction multiplier**
> before risking capital — exactly why this gate exists ahead of real orders.
> The defaults are industry rules of thumb, not validated optima; tune and
> walk-forward before live.

## Order execution (Phase 9) — paper only

Place crypto orders on Alpaca and record them to `trades`:

```bash
python -m crypto_bot.execution.broker     # account summary + reconcile (no orders)
```

The bot trades on the **paper** endpoint unless `LIVE_TRADING_ENABLED` is true
(default false). `place_order(...)` submits a market/limit order, records a
`trades` row (with `alpaca_order_id`, `signal_id`, score, source, `is_paper`),
and is **idempotent per signal** — a deterministic `client_order_id` plus a DB
check prevent a signal from double-firing. Fills are handled asynchronously:
`sync_fills()` populates `entry_price` once an order fills, and
`reconcile_open_positions()` re-syncs the DB with Alpaca on startup (restart
recovery). Alpaca crypto has no bracket orders, so the protective stop/target are
recorded on the trade row and enforced by the bot (exit management, Phase 10).

## Exit management (Phase 10)

Manage open longs to completion:

```bash
python -m crypto_bot.execution.exits     # replay bars since entry, close exited trades
```

Exit rules (summary.md §5.10): hard stop at the initial stop; **partial
scale-outs** — 50% at 1R (then move stop to breakeven), 25% at 2R, the final 25%
trails; a **Chandelier** trailing stop (ratchet-only); a structural take-profit
at the target; and a **time stop** if price hasn't moved ~1% in favor within
`TIME_STOP_BARS`. On close the `trades` row gets the effective blended exit price,
realized P&L + %, and `status='CLOSED'`. The manager is stateless — it replays
bars since entry each cycle, so it recovers cleanly from restarts.

## Daily summary & P&L (Phase 11)

Aggregate `trades` into `daily_summary` (per-symbol rows + a portfolio-total row
with `symbol` NULL) and write an `account_snapshots` row:

```bash
python -m crypto_bot.reporting.daily
```

Per symbol/day: buys (entries), sells (exits), realized P&L + % (return on the
day's deployed capital), unrealized P&L on open positions (marked to the latest
close), and win/loss counts. The snapshot records equity, cash, open positions,
portfolio heat, and drawdown vs the running equity peak. Rows upsert on
`(trade_date, symbol)`, so re-running the day is idempotent.

## Telegram alerts (Phase 12)

Send formatted alerts (new signal, entry, exit, daily summary, errors) to the
configured chat:

```bash
python -m crypto_bot.alerts.telegram resolve   # discover & store chat id (message the bot first)
python -m crypto_bot.alerts.telegram test      # send a test alert
```

Uses the raw Bot API over `requests` (synchronous, loop-safe). Token/chat id come
from `config_store`. **Alerting never crashes the trading loop** — every send is
wrapped and failures are logged and swallowed (returns False). A dedup throttle
suppresses identical messages within a short window. To enable delivery, message
**@CryptoTradeWisBot** once, then run `... telegram resolve` to store your
`TELEGRAM_CHAT_ID`.

---

## Project layout

```
crypto_bot/
├── config_store.py        # ONLY place that reads/writes credentials & config   (Phase 1/15)
├── db.py                  # SQLAlchemy engine, session factory, test_connection  (Phase 0)
├── logging_setup.py       # rotating file + console logging, UTC                 (Phase 0)
├── models.py              # ORM models / table definitions                       (Phase 1)
├── data/ingest.py         # Alpaca crypto OHLCV -> market_bars                    (Phase 2)
├── indicators/engine.py   # EMA/ATR/RSI/ADX/volume                               (Phase 3)
├── analysis/levels.py     # support/resistance detection                         (Phase 4)
├── analysis/breakout.py   # breakout confirmation + overextension filter         (Phase 5)
├── analysis/moving_avg.py # triple-MA signal                                     (Phase 5)
├── analysis/scorer.py     # confluence -> confidence score                       (Phase 6)
├── risk/sizing.py         # position size, stops, targets, portfolio heat        (Phase 7)
├── execution/broker.py    # Alpaca order placement + position tracking           (Phase 9)
├── execution/exits.py     # stop/target/trailing/time-stop management            (Phase 10)
├── reporting/daily.py     # daily P&L summary                                    (Phase 11)
├── alerts/telegram.py     # Telegram notifications                               (Phase 12)
├── orchestrator.py        # the main cycle / scheduler                           (Phase 13)
├── backtest/engine.py     # historical simulation                                (Phase 8)
└── tests/                 # pytest suite
```

## Roadmap

16 phases (0–15) tracked in `todo.md`, built strictly in order. Highlights:
backtest (Phase 8) **before** any real orders; paper execution (Phase 9);
paper validation → VPS go-live (Phase 14); security hardening / key encryption
(Phase 15, required before live funds).

## Disclaimer

Not financial advice. Trading crypto is high-risk; you can lose your full
balance. Validate with the backtest and a meaningful paper period before risking
real money, and start with the smallest possible size.
