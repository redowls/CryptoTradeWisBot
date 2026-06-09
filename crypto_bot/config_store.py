"""The ONLY module that reads/writes ``app_config`` (credentials + tunable params).

Everything else in the bot reads config through ``get`` / ``get_*`` here. This is
the seam Phase 15 will use to transparently encrypt/decrypt ``is_secret`` rows —
no other module should ever touch the ``app_config`` table directly.

Values are stored as strings; typed accessors parse on read. Lists (e.g.
``EMA_SHORT = "8,10,20"``) are comma-separated.
"""

from __future__ import annotations

from sqlalchemy import select

from crypto_bot.db import get_session
from crypto_bot.logging_setup import get_logger
from crypto_bot.models import AppConfig

log = get_logger(__name__)

_MISSING = object()


class ConfigError(KeyError):
    """Raised when a required config key is absent."""


# --- Phase 15 hooks (currently no-ops) --------------------------------------
def _decrypt(value: str, is_secret: bool) -> str:
    """Decrypt a stored value. Plaintext for now (Phase 15 will implement)."""
    return value


def _encrypt(value: str, is_secret: bool) -> str:
    """Encrypt a value before storing. Plaintext for now (Phase 15)."""
    return value


# --- Core read/write --------------------------------------------------------
def get(key: str, default=_MISSING) -> str | None:
    """Return the string value for ``key``; ``default`` if absent (else raise)."""
    with get_session() as session:
        row = session.get(AppConfig, key)
        if row is None:
            if default is _MISSING:
                raise ConfigError(f"Config key not found: {key!r}")
            return default
        return _decrypt(row.config_value, row.is_secret)


def set(key: str, value, is_secret: bool = False) -> None:
    """Upsert a config key. ``value`` is stringified before storing."""
    str_value = str(value)
    with get_session() as session:
        row = session.get(AppConfig, key)
        if row is None:
            row = AppConfig(config_key=key)
            session.add(row)
        row.config_value = _encrypt(str_value, is_secret)
        row.is_secret = is_secret
        session.commit()
    log.debug("config set: %s (is_secret=%s)", key, is_secret)


def exists(key: str) -> bool:
    with get_session() as session:
        return session.get(AppConfig, key) is not None


def all_keys() -> list[str]:
    with get_session() as session:
        return list(session.scalars(select(AppConfig.config_key)).all())


# --- Typed accessors --------------------------------------------------------
def get_int(key: str, default=_MISSING) -> int:
    return int(get(key, default))


def get_float(key: str, default=_MISSING) -> float:
    return float(get(key, default))


def get_bool(key: str, default=_MISSING) -> bool:
    """Parse common truthy strings: true/1/yes/on (case-insensitive)."""
    val = get(key, default)
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in {"true", "1", "yes", "on"}


def get_list(key: str, default=_MISSING, cast=str) -> list:
    """Parse a comma-separated value into a list, casting each element."""
    val = get(key, default)
    if not val:
        return []
    return [cast(p.strip()) for p in str(val).split(",") if p.strip()]


def get_int_list(key: str, default=_MISSING) -> list[int]:
    return get_list(key, default, cast=int)
