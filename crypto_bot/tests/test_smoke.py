"""Phase 0 smoke tests: package imports and logging work without a DB.

These run in CI / locally with no DATABASE_URL set. The live DB connection is
verified separately via ``test_connection()`` (see README, Phase 0 'Done when').
"""

import importlib

import pytest

# Every module in the layout should at least import cleanly.
MODULES = [
    "crypto_bot",
    "crypto_bot.db",
    "crypto_bot.logging_setup",
    "crypto_bot.config_store",
    "crypto_bot.models",
    "crypto_bot.orchestrator",
    "crypto_bot.data.ingest",
    "crypto_bot.indicators.engine",
    "crypto_bot.analysis.levels",
    "crypto_bot.analysis.breakout",
    "crypto_bot.analysis.moving_avg",
    "crypto_bot.analysis.scorer",
    "crypto_bot.risk.sizing",
    "crypto_bot.execution.broker",
    "crypto_bot.execution.exits",
    "crypto_bot.reporting.daily",
    "crypto_bot.alerts.telegram",
    "crypto_bot.backtest.engine",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module):
    assert importlib.import_module(module) is not None


def test_logging_configures(tmp_path, monkeypatch):
    from crypto_bot import logging_setup

    # Reset the module-level guard so setup runs fresh.
    monkeypatch.setattr(logging_setup, "_configured", False)
    log = logging_setup.setup_logging()
    assert log.handlers, "expected at least one handler"
    log2 = logging_setup.get_logger("crypto_bot.tests")
    log2.info("smoke test log line")


def test_db_missing_url_raises(monkeypatch):
    """With no DATABASE_URL, db access raises a clear, actionable error."""
    import crypto_bot.db as db

    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Defeat dotenv re-loading a real .env during the test.
    monkeypatch.setattr(db, "load_dotenv", lambda *a, **k: False)
    with pytest.raises(db.DatabaseConfigError):
        db.get_database_url()
