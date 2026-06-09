"""Central logging configuration: rotating file + console, UTC timestamps.

Call ``setup_logging()`` once at process start (orchestrator, scripts, tests).
All timestamps are UTC to match the bot's 24/7 UTC-anchored data model.
"""

from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Project root = parent of the crypto_bot package directory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = _PROJECT_ROOT / "logs"
_LOG_FILE = _LOG_DIR / "crypto_bot.log"

_LOG_FORMAT = "%(asctime)s UTC | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure root logging once (idempotent). Returns the root logger.

    - Console handler (stderr) + rotating file handler (5 MB x 5 backups).
    - UTC timestamps via ``logging.Formatter.converter = time.gmtime``.
    """
    global _configured

    root = logging.getLogger()
    if _configured:
        return root

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
    formatter.converter = time.gmtime  # force UTC

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        _LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)

    _configured = True
    root.debug("Logging configured (UTC, console + %s)", _LOG_FILE)
    return root


def get_logger(name: str) -> logging.Logger:
    """Return a module logger, ensuring logging is configured first."""
    if not _configured:
        setup_logging()
    return logging.getLogger(name)
