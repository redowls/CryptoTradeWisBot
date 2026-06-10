"""The ONLY module that reads/writes ``app_config`` (credentials + tunable params).

Everything else in the bot reads config through ``get`` / ``get_*`` here. This is
the seam Phase 15 will use to transparently encrypt/decrypt ``is_secret`` rows —
no other module should ever touch the ``app_config`` table directly.

Values are stored as strings; typed accessors parse on read. Lists (e.g.
``EMA_SHORT = "8,10,20"``) are comma-separated.
"""

from __future__ import annotations

import os
from functools import lru_cache

from sqlalchemy import select

from crypto_bot.db import get_session
from crypto_bot.logging_setup import get_logger
from crypto_bot.models import AppConfig

log = get_logger(__name__)

_MISSING = object()

# Phase 15: secrets are encrypted at rest with Fernet. The master key lives in
# the CRYPTO_BOT_MASTER_KEY env var (NEVER in the DB). Encrypted values carry
# this prefix so plaintext (legacy / pre-migration) rows are still readable.
_MASTER_KEY_ENV = "CRYPTO_BOT_MASTER_KEY"
_ENC_PREFIX = "enc::"


class ConfigError(KeyError):
    """Raised when a required config key is absent."""


class EncryptionError(RuntimeError):
    """Raised when an encrypted value can't be read (missing/invalid master key)."""


# --- Phase 15: encryption (Fernet) ------------------------------------------
@lru_cache(maxsize=1)
def _fernet():
    """Return a Fernet from the env master key, or None if encryption is off."""
    key = os.getenv(_MASTER_KEY_ENV, "").strip()
    if not key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(key.encode())


def encryption_enabled() -> bool:
    return _fernet() is not None


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(_ENC_PREFIX)


def _encrypt(value: str, is_secret: bool) -> str:
    """Encrypt secret values when a master key is configured; else store plaintext."""
    if not is_secret or is_encrypted(value):
        return value
    f = _fernet()
    if f is None:
        log.warning("No %s set — storing secret in PLAINTEXT (run Phase 15 migration).",
                    _MASTER_KEY_ENV)
        return value
    return _ENC_PREFIX + f.encrypt(value.encode()).decode()


def _decrypt(value: str, is_secret: bool) -> str:
    """Decrypt prefixed values; pass through plaintext (legacy) unchanged."""
    if not is_encrypted(value):
        return value
    f = _fernet()
    if f is None:
        raise EncryptionError(
            f"Value is encrypted but {_MASTER_KEY_ENV} is not set — cannot decrypt.")
    from cryptography.fernet import InvalidToken
    try:
        return f.decrypt(value[len(_ENC_PREFIX):].encode()).decode()
    except InvalidToken as e:
        raise EncryptionError(f"Invalid master key for an encrypted value: {e}") from e


# --- Core read/write --------------------------------------------------------
def get(key: str, default=_MISSING) -> str | None:
    """Return the string value for ``key``; ``default`` if absent (else raise)."""
    with get_session() as session:
        row = session.get(AppConfig, key)
        if row is None:
            if default is _MISSING:
                raise ConfigError(f"Config key not found: {key!r}")
            return default
        if row.is_secret:
            # Audit: record credential access (key name only — never the value).
            log.info("AUDIT credential access: %s", key)
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


# --- Phase 15: key + migration helpers --------------------------------------
def generate_master_key() -> str:
    """Generate a new Fernet master key (set it as CRYPTO_BOT_MASTER_KEY)."""
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def secret_keys() -> list[str]:
    """All config keys flagged is_secret."""
    with get_session() as session:
        return list(session.scalars(
            select(AppConfig.config_key).where(AppConfig.is_secret == True)).all())  # noqa: E712


def encryption_status() -> dict[str, bool]:
    """Map each secret key -> whether its stored value is encrypted at rest."""
    with get_session() as session:
        rows = session.scalars(
            select(AppConfig).where(AppConfig.is_secret == True)).all()  # noqa: E712
        return {r.config_key: is_encrypted(r.config_value) for r in rows}


def migrate_encrypt_secrets() -> int:
    """Encrypt any plaintext is_secret rows in place. Returns count encrypted.

    Idempotent: already-encrypted rows are skipped. Requires CRYPTO_BOT_MASTER_KEY.
    """
    if not encryption_enabled():
        raise EncryptionError(f"{_MASTER_KEY_ENV} is not set — cannot migrate.")
    encrypted = 0
    with get_session() as session:
        rows = session.scalars(
            select(AppConfig).where(AppConfig.is_secret == True)).all()  # noqa: E712
        for row in rows:
            if is_encrypted(row.config_value):
                continue
            row.config_value = _encrypt(row.config_value, True)
            encrypted += 1
        session.commit()
    log.info("Encrypted %d plaintext secret(s).", encrypted)
    return encrypted


if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "genkey":
        print(generate_master_key())
        print(f"# Add to .env as {_MASTER_KEY_ENV}=...  (keep it OUT of the DB and git)",
              file=sys.stderr)
    elif cmd == "migrate":
        print(f"encrypted {migrate_encrypt_secrets()} secret(s)")
    else:  # status
        print(f"encryption_enabled={encryption_enabled()}")
        for k, enc in encryption_status().items():
            print(f"  {k:22} {'ENCRYPTED' if enc else 'PLAINTEXT'}")
