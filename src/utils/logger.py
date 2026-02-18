
""""
Uso:
    from utils.logger import get_logger
    log = get_logger(__name__)
    log.info("mensaje")
"""

import os
import sys
import logging
from pathlib import Path

_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_INITIALIZED = False


def _init_root():
    """Configura root logger una sola vez (consola + archivo)."""
    global _INITIALIZED
    if _INITIALIZED:
        return
    _INITIALIZED = True

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, _LOG_LEVEL, logging.INFO))

    # ── Consola ───────────────────────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)
    console.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    root.addHandler(console)

    # ── Archivo rotativo ──────────────────────────────────────────
    from logging.handlers import RotatingFileHandler

    file_h = RotatingFileHandler(
        _LOG_DIR / "strategy.log",
        maxBytes=5 * 1024 * 1024,   # 5 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    root.addHandler(file_h)

    # Silenciar loggers ruidosos
    for noisy in ("httpx", "httpcore", "urllib3", "openai", "litellm"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Devuelve un logger con config de producción."""
    _init_root()
    return logging.getLogger(name)
