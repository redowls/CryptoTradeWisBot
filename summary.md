# Crypto Swing-Trading Bot — Project Summary & Specification

> **This file is the single source of truth.** Read it at the start of every Claude Code
> session before writing code. Build the project by working through `todo.md` one phase at a
> time. Do not skip ahead — each phase depends on the one before it.

---

## 1. Overview & Goal

A Python bot that trades **crypto** on a **swing timeframe** (hours to a few days) using the
**Alpaca** API. It identifies **support/resistance breakouts** and confirms them with a
**triple moving-average** trend system, fuses everything into a single **0–100 confidence
score**, and **scales position size with conviction** (more confidence → more capital, within
hard risk caps). It stores everything in **Microsoft SQL Server**, sends **Telegram** alerts,
and runs unattended on a **VPS**.

The bot must be able to answer, from the database alone:
- Which symbols are we analyzing? (`watchlist`)
- What did we buy/sell today, and what was the % profit/loss? (`daily_summary`)
- *Why* did we buy — breakout, moving averages, or both — and how confident were we? (`signals` + `trades`)

---

## 2. Scope & Key Decisions

| Decision | Choice | Notes |
|---|---|---|
| Asset class | **Crypto only** | `watchlist` is asset-agnostic, seeded with crypto pairs. Equities can be added later without schema changes. |
| Timeframe | **Swing**: 1H entry bars, 4H/1D trend filter | Configurable. "Fast" here means hours-to-days, not sub-minute HFT. |
| Language / DB | **Python + Microsoft SQL Server** | Connect via `pyodbc` + `SQLAlchemy`. |
| Execution venue | **Alpaca** (crypto endpoints) | **Default to the PAPER endpoint.** Only switch to live after Phase 14. |
| Credentials | **Stored in the DB** (`app_config`) | Plaintext for now, per project owner's instruction. Access is funneled through one `config_store` module so it can be encrypted in Phase 15 without touching the rest of the code. |
| Alerts | **Telegram** | Signals, entries, exits, daily summary, errors. |
| Hosting | **VPS**, unattended scheduler | systemd / supervisor / pm2. |

> ⚠️ **Security is deliberately deferred to Phase 15, not skipped.** Plaintext API keys in a
> database is a real risk: anyone with DB read access can drain the trading account. The design
> isolates this so it is trivial to fix later. Treat live-key activation (Phase 14) and security
> hardening (Phase 15) as linked — ideally encrypt before the account ever holds meaningful funds.

---

## 3. Tech Stack

- **Python 3.11+**
- **Database:** Microsoft SQL Server (via `pyodbc` + `SQLAlchemy`; ODBC Driver 18)
- **Market data + execution:** `alpaca-py` (official Alpaca SDK)
- **Indicators:** `pandas`, `numpy`, `pandas-ta` (or `ta`) — or hand-rolled for full control
- **Alerts:** `python-telegram-bot` (or raw Bot API via `requests`)
- **Scheduling:** APScheduler in-process, or system cron/systemd timer
- **Config:** `.env` for the DB connection string only; everything else (including Alpaca/Telegram keys) in `app_config`
- **Testing:** `pytest`
- **Backtesting:** custom event-driven harness (Phase 8); `vectorbt` or `backtesting.py` optional

---

## 4. High-Level Architecture

**Data flow (one cycle, runs every bar close):**

```
Scheduler (hourly)
   └─> Data Ingestion ──> store OHLCV in market_bars
          └─> Indicator Engine ──> EMAs, ATR, RSI, ADX, volume avg
                 └─> S/R Detection ──> swing pivots → clusters → volume profile → sr_levels
                        └─> Signal Engine
                               ├─ Breakout check (close beyond level + volume + buffer)
                               ├─ Overextension filter (distance / RSI / extension / R:R)
                               └─ Triple-MA check (long set = filter, short set = trigger)
                        └─> Confluence Scorer ──> 0–100 confidence + breakdown → signals
                               └─> Risk & Sizing ──> qty, stop, target (only if score ≥ threshold)
                                      └─> Execution (Alpaca, PAPER default) ──> trades
                                             └─> Exit Manager ──> stops / targets / trailing / time
                        └─> Daily Summary (end of day) ──> daily_summary
   └─> Telegram alerts at every meaningful step
```

**Suggested module layout:**

```
crypto_bot/
├── config_store.py        # the ONLY place that reads/writes credentials & config (Phase 1/15)
├── db.py                  # SQLAlchemy engine, session, schema helpers
├── models.py              # ORM models / table definitions
├── data/ingest.py         # Alpaca crypto OHLCV → market_bars
├── indicators/engine.py   # EMA/ATR/RSI/ADX/volume
├── analysis/levels.py     # support/resistance detection
├── analysis/breakout.py   # breakout confirmation + overextension filter
├── analysis/moving_avg.py # triple-MA signal
├── analysis/scorer.py     # confluence → confidence score
├── risk/sizing.py         # position size, stops, targets, portfolio heat
├── execution/broker.py    # Alpaca order placement + position tracking
├── execution/exits.py     # stop/target/trailing/time-stop management
├── reporting/daily.py     # daily P&L summary
├── alerts/telegram.py     # Telegram notifications
├── orchestrator.py        # the main cycle / scheduler
├── backtest/engine.py     # historical simulation (Phase 8)
└── tests/
```

---

## 5. Trading Logic Specification

All numbers below are **defaults** — they live in config (Section 7) and must be tuned by
backtesting before live use.

### 5.1 Market data & timeframes
- **Trigger timeframe:** 1H bars (where entries fire).
- **Trend-filter timeframe:** 4H (and optionally 1D) — only take longs when the higher timeframe agrees.
- Crypto is 24/7: anchor "daily" calculations to **UTC 00:00**. No gaps, but expect thin weekend liquidity.

### 5.2 Indicators (per symbol, per timeframe)
- **EMAs:** short set `[8, 10, 20]`, long set `[21, 34, 55]` (Fibonacci; crypto convention). Use **EMA, not SMA** — crypto moves fast and the strategy needs responsive alignment.
- **ATR(14):** volatility, drives stops and buffers.
- **RSI(14):** momentum throttle (Wilder; overbought ≥ 70).
- **ADX(14):** trend-strength gate (trade only when ADX > 20–25; below = chop).
- **Volume average:** 20-bar SMA of volume, for breakout confirmation.

### 5.3 Support/Resistance detection
1. **Swing pivots:** bar `i` is a swing high if its high exceeds the highs of `n` bars on each side (pivot strength `n = 5`); mirror for swing lows. A pivot is only confirmed `n` bars later (acceptable lag, not repainting).
2. **Cluster into zones:** merge pivots within a tolerance (**0.4%** for crypto) into a single level carrying a **touch count** and **time span**. More touches + longer span = stronger.
3. **Volume Profile:** compute Point of Control (**POC** = highest-volume price), and Value Area (**VAH/VAL**, the 70% volume band). These are high-reliability levels.
4. **Rank** levels by strength; store the strongest above and below current price in `sr_levels`. Treat levels as **zones (±0.2–0.5%)**, not exact lines.

### 5.4 Breakout confirmation (don't trust a bare line cross)
A long breakout is **valid** only if **all** hold:
- **Candle CLOSE** above the resistance level + a buffer of **max(0.5%, 1 × ATR)** (an intrabar wick through the level does *not* count).
- **Volume ≥ 1.5 ×** the 20-bar average on the breakout bar (crypto volume is fragmented; also accept **ATR expansion** as a participation proxy).
- **Strong body:** candle body > ~70% of its range (or > 0.8 × ATR).
- **Optional retest mode:** wait for price to return to the broken level and hold it as support (rejection candle), then enter with a tighter stop. Provide both an **aggressive** (enter on confirmed close) and **conservative** (wait for retest) mode via config.

### 5.5 Overextension filter — "is it a nice entry or already overpriced?"
Reject / down-weight the entry if **any** of these say price has already run:
- **Distance from breakout level** > configurable ATR multiple (chasing).
- **Extension above key EMA:** `(price − EMA) / EMA` too large, or extension z-score `> +2` std devs.
- **RSI > 70** (don't chase parabolic moves; bearish RSI divergence is a stronger warning).
- **Risk/Reward to next resistance < 2.0** — this is the decisive filter. Compute
  `R:R = (next_resistance − entry) / (entry − stop)`. If below the minimum, the easy move is gone → **skip**.

### 5.6 Triple moving-average system
- **Stacking is the core rule.** Bullish = `fast > mid > slow` **and** price above all three. Bearish = reverse. Intertwined = **no trade**. Wider spacing = stronger momentum.
- **Two roles:**
  - **Long set (21/34/55) = trend filter / gate.** Only allow longs when the long set is bullishly stacked on the trend timeframe.
  - **Short set (8/10/20) = entry trigger.** Fire only in the direction the long set permits (e.g., short-set bullish cross / stack + price > EMA8).
- **ADX > 20** required to avoid range-bound whipsaw.
- Output: **BUY / HOLD / AVOID**.

### 5.7 Combined signal (buy/hold/avoid)
A candidate **BUY** requires: long-set trend filter bullish **AND** (confirmed breakout **OR** short-set bullish trigger) **AND** overextension filter passed **AND** R:R ≥ 2.0.
Record **`signal_source`** as one of `BREAKOUT`, `MA`, or `BOTH` depending on which condition(s) fired — this is stored to the DB as requested.

### 5.8 Confidence score (0–100)
Combine **independent** signal families (not five versions of the same thing). Each contributes
weighted points; sum and normalize to 100. **Default weights:**

| Factor | Max pts | What earns points |
|---|---|---|
| Trend alignment (long set stacked + price above) | 30 | Cleaner, wider stacking on trend TF |
| Breakout quality | 20 | Decisive close beyond level + buffer; clean structure |
| Volume confirmation | 15 | ≥ 1.5× avg (full), partial credit below |
| Short-set trigger (8/10/20) | 15 | Fresh bullish cross / full stack |
| Momentum (RSI healthy 50–70, not >70) | 10 | In-band, no bearish divergence |
| Volatility regime (ATR normal band, ADX > 20) | 10 | Trending, not chop or extreme-vol |
| **Total** | **100** | |

- **Trade only if score ≥ 70.** 70–79 = good, 80–89 = prime, ≥ 90 = extreme conviction.
- Below 70 → **alert only**, no trade.
- **Store the full breakdown** (per-factor points as JSON) in `signals.score_breakdown` so every decision is auditable.

### 5.9 Position sizing
- **Risk-based formula (Van Tharp):** `qty = (equity × risk_pct) / (entry − stop)`. This controls *dollar risk*, not position value.
- **Base risk:** 1% of current equity per trade. Always size off **current** equity (shrinks after losses, grows after wins).
- **Conviction scaling (configurable, two modes):**
  - **Tiered (default, simplest):** score 70–79 → 0.75% risk; 80–89 → 1.0%; 90+ → 2.0%.
  - **Fractional-Kelly:** `f* = (b·p − q)/b`; never full Kelly. Use **quarter-Kelly (×0.25)** normally, **half-Kelly (×0.5)** for ≥ 90 scores.
- **Hard caps (never exceed):** 2% risk per trade; **6% total portfolio heat** (sum of risk across all open positions); **5** concurrent positions.
- **Drawdown brake:** if account drawdown > 10%, automatically reduce risk_pct until recovery.

> ⚠️ **Professional disagreement, made configurable:** some traders insist size should be *mechanical*
> (fixed %) and conviction should only decide *whether* to trade. Default to conservative — a small
> conviction multiplier inside a hard 2% cap.

### 5.10 Risk management & exits
- **Stop-loss:** `entry − (ATR × 2.5)` for crypto swing (range 2–3; use the wider, structurally-justified stop and size down). Never place stops *inside* the range just broken. Structure alternative: just below the broken level / retest low.
- **Targets:** next resistance / VAH / measured move (range height projected from breakout), and require ≥ 2:1 R:R.
- **Partial scale-out:** 50% at 1R → move stop to breakeven; 25% at 2R; trail the final 25%. Take partials at *logical* levels, not arbitrary ones.
- **Trailing stop:** Chandelier Exit `highest_high_since_entry − (ATR × 3)`, default params **(22, 3.0)**; only ratchets up, never down. Crypto trailing distance floor ≥ 1× ATR.
- **Time stop:** if the trade hasn't moved ≥ ~1% in favor within N bars, exit and free the capital (24/7 opportunity cost).

### 5.11 Crypto-specific adjustments
- **24/7 / UTC anchoring**; no gaps but thin weekend books → consider reduced size / wider stops over weekends, extra skepticism on weekend breakouts.
- **Higher volatility:** BTC daily ATR ~3–7%, alts far more. Wider ATR-based stops, EMA over SMA, S/R as zones.
- **Volatility/regime filter:** express ATR as a percentile of its trailing range; trade trends in the normal band (≈25th–75th pct), expect mean-reversion and cut size in the top quartile.
- **Correlation:** alts follow BTC and correlations spike toward 1.0 in selloffs — 5 "different" coins can act as one position. Cap correlated exposure for portfolio-heat purposes.

---

## 6. Database Schema

SQL Server. Prices/quantities use `DECIMAL(18,8)` (crypto precision); percentages `DECIMAL(9,4)`;
timestamps `DATETIME2` in **UTC**. Phase 1 finalizes exact constraints, indexes, and FKs — the
columns below are the required starting point.

```sql
-- 6.1 Config & credentials (ALL keys live here, per project owner; isolated via config_store)
app_config (
  config_key      NVARCHAR(100) PRIMARY KEY,   -- 'ALPACA_API_KEY', 'ALPACA_SECRET',
                                                -- 'ALPACA_BASE_URL', 'TELEGRAM_BOT_TOKEN',
                                                -- 'TELEGRAM_CHAT_ID', plus tunable params
  config_value    NVARCHAR(MAX) NOT NULL,
  is_secret       BIT DEFAULT 0,               -- flags rows to encrypt in Phase 15
  updated_at      DATETIME2 DEFAULT SYSUTCDATETIME()
)

-- 6.2 Analyzable symbols ("which ones can we analyze") — asset-agnostic, seeded with crypto
watchlist (
  symbol_id       BIGINT IDENTITY PRIMARY KEY,
  symbol          NVARCHAR(20) UNIQUE NOT NULL, -- 'BTC/USD', 'ETH/USD', ...
  asset_class     NVARCHAR(20) DEFAULT 'crypto',
  is_active       BIT DEFAULT 1,
  added_at        DATETIME2 DEFAULT SYSUTCDATETIME(),
  notes           NVARCHAR(255) NULL
)

-- 6.3 OHLCV cache
market_bars (
  bar_id          BIGINT IDENTITY PRIMARY KEY,
  symbol          NVARCHAR(20) NOT NULL,
  timeframe       NVARCHAR(10) NOT NULL,        -- '1H', '4H', '1D'
  ts              DATETIME2 NOT NULL,           -- bar open time, UTC
  open            DECIMAL(18,8), high DECIMAL(18,8), low DECIMAL(18,8), close DECIMAL(18,8),
  volume          DECIMAL(28,8),
  CONSTRAINT uq_bar UNIQUE (symbol, timeframe, ts)
)

-- 6.4 Indicator cache (optional; can also be computed on the fly)
indicators (
  symbol NVARCHAR(20), timeframe NVARCHAR(10), ts DATETIME2,
  ema8 DECIMAL(18,8), ema10 DECIMAL(18,8), ema20 DECIMAL(18,8),
  ema21 DECIMAL(18,8), ema34 DECIMAL(18,8), ema55 DECIMAL(18,8),
  atr DECIMAL(18,8), rsi DECIMAL(9,4), adx DECIMAL(9,4), vol_avg DECIMAL(28,8),
  CONSTRAINT uq_ind UNIQUE (symbol, timeframe, ts)
)

-- 6.5 Detected support/resistance levels
sr_levels (
  level_id        BIGINT IDENTITY PRIMARY KEY,
  symbol          NVARCHAR(20) NOT NULL,
  timeframe       NVARCHAR(10) NOT NULL,
  level_price     DECIMAL(18,8) NOT NULL,
  level_type      NVARCHAR(12) NOT NULL,        -- 'support' | 'resistance'
  source          NVARCHAR(20) NOT NULL,        -- 'pivot'|'cluster'|'poc'|'vah'|'val'
  touch_count     INT DEFAULT 1,
  strength        DECIMAL(9,4),
  detected_at     DATETIME2 DEFAULT SYSUTCDATETIME(),
  is_active       BIT DEFAULT 1
)

-- 6.6 Signals — the "why we acted + how confident" record
signals (
  signal_id       BIGINT IDENTITY PRIMARY KEY,
  symbol          NVARCHAR(20) NOT NULL,
  ts              DATETIME2 NOT NULL,
  signal          NVARCHAR(8) NOT NULL,         -- 'BUY' | 'HOLD' | 'AVOID'
  signal_source   NVARCHAR(10) NOT NULL,        -- 'BREAKOUT' | 'MA' | 'BOTH'  <-- as requested
  confidence_score DECIMAL(5,2) NOT NULL,       -- 0–100
  score_breakdown NVARCHAR(MAX) NULL,           -- JSON: per-factor points
  entry_price     DECIMAL(18,8), stop_price DECIMAL(18,8), target_price DECIMAL(18,8),
  rr_ratio        DECIMAL(9,4),
  acted           BIT DEFAULT 0,                -- did it become a trade?
  notes           NVARCHAR(500) NULL
)

-- 6.7 Trades / orders
trades (
  trade_id        BIGINT IDENTITY PRIMARY KEY,
  signal_id       BIGINT NULL,                  -- FK -> signals
  symbol          NVARCHAR(20) NOT NULL,
  side            NVARCHAR(4) NOT NULL,         -- 'BUY' | 'SELL'
  qty             DECIMAL(18,8) NOT NULL,
  entry_price     DECIMAL(18,8), entry_time DATETIME2,
  stop_price      DECIMAL(18,8), target_price DECIMAL(18,8),
  exit_price      DECIMAL(18,8), exit_time DATETIME2,
  realized_pnl    DECIMAL(18,8) NULL,
  realized_pnl_pct DECIMAL(9,4) NULL,
  status          NVARCHAR(10) DEFAULT 'OPEN',  -- 'OPEN'|'CLOSED'|'CANCELLED'
  confidence_score DECIMAL(5,2) NULL,           -- denormalized for easy reporting
  signal_source   NVARCHAR(10) NULL,
  risk_pct        DECIMAL(9,4) NULL,            -- % equity risked on this trade
  fees            DECIMAL(18,8) NULL,
  alpaca_order_id NVARCHAR(64) NULL,
  is_paper        BIT DEFAULT 1
)

-- 6.8 Daily summary — "what we bought/sold today and % P&L"
daily_summary (
  summary_id      BIGINT IDENTITY PRIMARY KEY,
  trade_date      DATE NOT NULL,                -- UTC date
  symbol          NVARCHAR(20) NULL,            -- NULL row = portfolio total for the day
  num_buys        INT DEFAULT 0,
  num_sells       INT DEFAULT 0,
  realized_pnl    DECIMAL(18,8) DEFAULT 0,
  realized_pnl_pct DECIMAL(9,4) DEFAULT 0,
  unrealized_pnl  DECIMAL(18,8) DEFAULT 0,
  win_count       INT DEFAULT 0,
  loss_count      INT DEFAULT 0,
  notes           NVARCHAR(500) NULL,
  CONSTRAINT uq_daily UNIQUE (trade_date, symbol)
)

-- 6.9 Account snapshots (for equity %, drawdown brake, portfolio heat)
account_snapshots (
  snapshot_id     BIGINT IDENTITY PRIMARY KEY,
  ts              DATETIME2 DEFAULT SYSUTCDATETIME(),
  equity          DECIMAL(18,2), cash DECIMAL(18,2),
  open_positions  INT, portfolio_heat_pct DECIMAL(9,4), drawdown_pct DECIMAL(9,4)
)

-- 6.10 Run log (optional but recommended for an unattended VPS bot)
bot_runs (
  run_id BIGINT IDENTITY PRIMARY KEY,
  started_at DATETIME2, finished_at DATETIME2,
  status NVARCHAR(20), symbols_scanned INT, signals_generated INT,
  error_message NVARCHAR(MAX) NULL
)
```

---

## 7. Configuration Parameters (defaults)

Stored in `app_config`. Tune via backtest before live.

| Key | Default | Meaning |
|---|---|---|
| `TRIGGER_TF` | `1H` | Entry timeframe |
| `TREND_TF` | `4H` | Trend-filter timeframe |
| `PIVOT_STRENGTH` | `5` | Bars each side for swing pivots |
| `CLUSTER_TOLERANCE_PCT` | `0.4` | Merge S/R within this % |
| `BREAKOUT_BUFFER_PCT` | `0.5` | Min close-beyond buffer (uses max with 1×ATR) |
| `VOLUME_MULT` | `1.5` | Breakout volume vs 20-bar avg |
| `RSI_PERIOD` / `RSI_OVERBOUGHT` | `14` / `70` | Momentum throttle |
| `ATR_PERIOD` | `14` | Volatility |
| `ATR_STOP_MULT` | `2.5` | Stop distance (crypto) |
| `MIN_RR` | `2.0` | Minimum reward:risk |
| `EMA_SHORT` / `EMA_LONG` | `8,10,20` / `21,34,55` | MA sets |
| `ADX_MIN` | `20` | Trend-strength gate |
| `CONFIDENCE_THRESHOLD` | `70` | Min score to trade |
| `BASE_RISK_PCT` | `1.0` | Risk per trade |
| `MAX_RISK_PCT` | `2.0` | Hard per-trade cap |
| `MAX_PORTFOLIO_HEAT_PCT` | `6.0` | Sum of open risk |
| `MAX_CONCURRENT_POSITIONS` | `5` | Position count cap |
| `SIZING_MODE` | `tiered` | `tiered` or `kelly` |
| `BREAKOUT_MODE` | `aggressive` | `aggressive` or `retest` |
| `CHANDELIER` | `22,3.0` | Trailing-stop params |
| `DRAWDOWN_BRAKE_PCT` | `10` | Cut risk above this drawdown |
| `TIME_STOP_BARS` | `24` | Exit if no ~1% move within N bars |

---

## 8. Phase Roadmap (see `todo.md` for the checklists)

0. Project setup & scaffolding
1. Database schema + `config_store`
2. Market-data ingestion (Alpaca crypto → `market_bars`)
3. Indicator engine
4. Support/resistance detection
5. Signal generation (breakout + overextension + triple-MA + combine)
6. Confidence scoring (0–100 + breakdown to DB)
7. Risk & position sizing
8. **Backtesting harness** (validate logic before any real orders)
9. Order execution + position tracking (**paper endpoint by default**)
10. Exit management (stops / targets / partials / trailing / time)
11. Daily summary & P&L
12. Telegram alerts
13. Orchestration & scheduling
14. Paper validation → VPS go-live checklist
15. **Security hardening** (encrypt keys; deferred, not skipped)

---

## 9. Caveats & Disclaimers

- **Not financial advice.** This is a software project. Trading crypto is high-risk and you can lose your full balance. Start tiny.
- **Validate before live.** Run the backtest (Phase 8) and a meaningful paper-trading period (Phase 14) before risking real money. Backtests overstate live results — they ignore slippage, fees, thin-liquidity gaps, and stop-hunting common in crypto.
- **The numbers here are industry rules of thumb, not laws.** All thresholds need tuning and walk-forward validation (ideally 100+ trades) on the exact symbols traded.
- **MA + breakout systems lag and whipsaw in chop.** Expect a modest win rate offset by larger winners; the ADX/regime filter is essential.
- **Crypto volume data is fragmented** — lean on ATR expansion and structure, not volume alone.
- **Plaintext keys are a liability.** Phase 15 exists for a reason; do it before the account holds real funds.
