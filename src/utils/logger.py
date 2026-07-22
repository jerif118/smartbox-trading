""" "
Uso:
    from utils.logger import get_logger
    log = get_logger(__name__)
    log.info("mensaje")
"""

import contextvars
import logging
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
# %(ctx)s lo rellena ContextFilter con "[run][símbolo] " (o "" si no hay contexto).
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(ctx)s%(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_INITIALIZED = False

# Contexto por hilo/tarea para correlacionar logs de un mismo run/símbolo.
# Los contextvars NO se heredan entre threads, así que se siembra en cada punto
# de entrada de hilo (run_pipeline, run_stage, ramas por símbolo).
_log_context: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "log_context", default=None
)


class ContextFilter(logging.Filter):
    """Inyecta el prefijo de contexto (run_id/símbolo) en cada record.

    Nunca descarta un record: ante cualquier fallo deja ``ctx`` vacío.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            ctx = _log_context.get() or {}
            parts = ""
            run_id = ctx.get("run_id")
            symbol = ctx.get("symbol")
            if run_id:
                parts += f"[{run_id[:8]}]"
            if symbol:
                parts += f"[{symbol}]"
            record.ctx = f"{parts} " if parts else ""
        except Exception:  # el logging nunca debe romperse por el contexto
            record.ctx = ""
        return True


def bind_log_context(**kwargs: str) -> contextvars.Token:
    """Fija claves de contexto (run_id, symbol) para el hilo/tarea actual.

    Devuelve un token para restaurar con reset_log_context(). Ignora valores None.
    """
    current = dict(_log_context.get() or {})
    current.update({k: v for k, v in kwargs.items() if v is not None})
    return _log_context.set(current)


def reset_log_context(token: contextvars.Token) -> None:
    _log_context.reset(token)


@contextmanager
def log_context(**kwargs: str) -> Iterator[None]:
    """Context manager que fija y restaura el contexto de log."""
    token = bind_log_context(**kwargs)
    try:
        yield
    finally:
        reset_log_context(token)

# Env vars cuyos VALORES nunca deben aparecer en un log
_SECRET_ENV_VARS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "MISTRAL_API_KEY",
    "DEEPSEEK_API_KEY",
    "GROQ_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "PASSWORD",
    "API_KEY",
    "KEY",
    "ID",
    "NEWS_API_KEY",
)

_BEARER_RE = re.compile(r"Bearer\s+\S+")
_REDACTED = "***REDACTED***"


class RedactSecretsFilter(logging.Filter):
    """Enmascara tokens Bearer y valores de env secrets en cada record.

    Se instala en TODOS los handlers del root logger, así cubre también
    logs de librerías de terceros que pasen por logging estándar.
    """

    def __init__(self) -> None:
        super().__init__()
        # len > 6 evita redactar valores triviales tipo "true" o ids cortos
        self._secrets = [
            val for var in _SECRET_ENV_VARS if len(val := os.getenv(var, "").strip()) > 6
        ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        redacted = _BEARER_RE.sub(f"Bearer {_REDACTED}", msg)
        for secret in self._secrets:
            if secret in redacted:
                redacted = redacted.replace(secret, _REDACTED)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def _resolve_log_dir() -> Path | None:
    """Determina dónde escribir logs sin asumir layout source-tree.

    Prioridad:
      1. LOG_DIR del entorno (explícito; usado en Docker/k8s).
      2. <cwd>/logs si es escribible (uso local: cwd == raíz del proyecto).
      3. None → el handler de archivo se desactiva, solo queda consola.
    """
    # Pytest importa el logger durante collection, antes de que existan sus
    # fixtures. No contaminar strategy.log con fallos deliberados de tests.
    if "pytest" in sys.modules:
        return None

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
    redact = RedactSecretsFilter()
    ctx_filter = ContextFilter()

    # ── Consola ───────────────────────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)
    console.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    console.addFilter(redact)
    console.addFilter(ctx_filter)
    root.addHandler(console)

    # ── Archivo rotativo (opcional, no rompe si no hay path escribible) ──
    log_dir = _resolve_log_dir()
    if log_dir is not None:
        from logging.handlers import RotatingFileHandler

        file_h = RotatingFileHandler(
            log_dir / "strategy.log",
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_h.setLevel(logging.DEBUG)
        file_h.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
        file_h.addFilter(redact)
        file_h.addFilter(ctx_filter)
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
