
""""
Uso:
    from utils.logger import get_logger
    log = get_logger(__name__)
    log.info("mensaje")
"""

import logging
import os
import sys
from pathlib import Path

_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_INITIALIZED = False


def _resolve_log_dir() -> Path | None:
    """Determina dónde escribir logs sin asumir layout source-tree.

    Prioridad:
      1. LOG_DIR del entorno (explícito; usado en Docker/k8s).
      2. <cwd>/logs si es escribible (uso local: cwd == raíz del proyecto).
      3. None → el handler de archivo se desactiva, solo queda consola.
    """
    env_dir = os.getenv("LOG_DIR")
    candidates: list[Path] = []
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(Path.cwd() / "logs")

    for p in candidates:
        try:
            p.mkdir(parents=True, exist_ok=True)
            # Probar escritura (algunos volúmenes son read-only)
            test_file = p / ".write_test"
            test_file.touch()
            test_file.unlink()
            return p
        except (OSError, PermissionError):
            continue
    return None


def _init_root():
    """Configura root logger una sola vez (consola + archivo opcional)."""
    global _INITIALIZED
    if _INITIALIZED:
        return
    _INITIALIZED = True

    root = logging.getLogger()
    root.setLevel(getattr(logging, _LOG_LEVEL, logging.INFO))

    # ── Consola ───────────────────────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)
    console.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    root.addHandler(console)

    # ── Archivo rotativo (opcional, no rompe si no hay path escribible) ──
    log_dir = _resolve_log_dir()
    if log_dir is not None:
        from logging.handlers import RotatingFileHandler

        file_h = RotatingFileHandler(
            log_dir / "strategy.log",
            maxBytes=5 * 1024 * 1024,   # 5 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_h.setLevel(logging.DEBUG)
        file_h.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
        root.addHandler(file_h)
    else:
        root.warning(
            "[logger] No se encontró ruta escribible para logs (configura LOG_DIR). "
            "Solo se escribirá a stdout."
        )

    # Silenciar loggers ruidosos
    for noisy in ("httpx", "httpcore", "urllib3", "openai", "litellm"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Devuelve un logger con config de producción."""
    _init_root()
    return logging.getLogger(name)
