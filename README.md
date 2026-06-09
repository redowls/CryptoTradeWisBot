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
