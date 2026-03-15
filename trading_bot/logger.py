"""
logger.py
=========
Configures a module-level logger that writes both to STDOUT and to a
rotating log file.  Import ``get_logger`` in every module:

    from trading_bot.logger import get_logger
    log = get_logger(__name__)
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from trading_bot import config

_FORMATTER = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_initialised = False


def _setup_root_logger() -> None:
    global _initialised
    if _initialised:
        return

    root = logging.getLogger("trading_bot")
    root.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(_FORMATTER)
    root.addHandler(ch)

    # Rotating file handler (5 MB × 3 backups)
    log_path = Path(config.LOG_FILE)
    fh = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(_FORMATTER)
    root.addHandler(fh)

    # Suppress noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("binance").setLevel(logging.WARNING)

    _initialised = True


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``trading_bot`` namespace."""
    _setup_root_logger()
    return logging.getLogger(name if name.startswith("trading_bot") else f"trading_bot.{name}")
