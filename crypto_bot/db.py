"""Database access: SQLAlchemy engine + session factory for MS SQL Server.

The connection string is read from the ``DATABASE_URL`` environment variable
(loaded from ``.env``). Per the spec, ``.env`` holds ONLY this value — all other
credentials live in the ``app_config`` table and are read via ``config_store``.

Phase 0 deliverable: ``test_connection()`` proves the DB is reachable.
"""

from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from crypto_bot.logging_setup import get_logger

log = get_logger(__name__)

# Load .env from the project root (no-op if already loaded or file absent).
load_dotenv()

_ENV_VAR = "DATABASE_URL"


class DatabaseConfigError(RuntimeError):
    """Raised when the database connection string is missing or invalid."""


def get_database_url() -> str:
    """Return the SQLAlchemy URL from the environment, or raise a clear error."""
    url = os.getenv(_ENV_VAR, "").strip()
    if not url:
        raise DatabaseConfigError(
            f"{_ENV_VAR} is not set. Copy .env.example to .env and fill in the "
            "SQL Server connection string (see README, 'Configure the database')."
        )
    return url


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Create (once) and return the SQLAlchemy Engine.

    ``pool_pre_ping`` recycles dead connections — important for a long-running
    unattended bot whose DB connection may drop between hourly cycles.
    """
    url = get_database_url()
    engine = create_engine(url, pool_pre_ping=True, future=True)
    log.debug("SQLAlchemy engine created for %s", engine.url.render_as_string(hide_password=True))
    return engine


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker:
    """Return a configured session factory bound to the engine."""
    return sessionmaker(bind=get_engine(), class_=Session, expire_on_commit=False, future=True)


def get_session() -> Session:
    """Return a new ORM session. Caller is responsible for closing it.

    Prefer using as a context manager: ``with get_session() as s: ...``.
    """
    return get_session_factory()()


def test_connection() -> bool:
    """Verify the database is reachable. Prints a success line; raises on failure.

    Phase 0 'Done when' check:
        python -c "from crypto_bot.db import test_connection; test_connection()"
    """
    engine = get_engine()
    with engine.connect() as conn:
        value = conn.execute(text("SELECT 1")).scalar_one()
        if value != 1:
            raise DatabaseConfigError(f"Unexpected result from 'SELECT 1': {value!r}")
        version = conn.execute(text("SELECT @@VERSION")).scalar_one()

    safe_url = engine.url.render_as_string(hide_password=True)
    print(f"OK: connected to {safe_url}")
    print(f"    {str(version).splitlines()[0].strip()}")
    log.info("Database connection test succeeded (%s)", safe_url)
    return True


if __name__ == "__main__":
    test_connection()
