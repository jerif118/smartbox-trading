"""
Validación de variables de entorno requeridas.

Versión nueva: usa `Settings` de pydantic para validación tipada.
"""

from __future__ import annotations

import os

from infrastructure.config.settings import REQUIRED_FOR_LIVE, get_settings, has_openai_key
from utils.logger import get_logger

log = get_logger(__name__)

PLACEHOLDER_VALUES = {"123456", "your-key-here", "changeme", "xxx", "placeholder"}


def _is_placeholder(val: str) -> bool:
    return not val or val.strip().lower() in PLACEHOLDER_VALUES


def validate_env() -> bool:
    """
    Valida configuración mínima para correr.

    - Si DRY_RUN=true: solo exige LLM key (no broker).
    - Si DRY_RUN=false (LIVE): exige TODO (broker + LLM).
    """
    settings = get_settings()
    errors: list[str] = []

    log.info("=" * 60)
    log.info("Validando entorno (DRY_RUN=%s, SIMPLE_REALITY=%s)", settings.dry_run, settings.simple_reality)
    log.info("=" * 60)

    if not has_openai_key():
        errors.append("  ✗ OPENAI_API_KEY — no definida o es placeholder")

    if not settings.dry_run:
        for var in REQUIRED_FOR_LIVE:
            if var == "OPENAI_API_KEY":
                continue
            val = os.getenv(var, "")
            if _is_placeholder(val):
                errors.append(f"  ✗ {var} — no definida o es placeholder")

    if not settings.symbol_list:
        errors.append("  ✗ SYMBOLS — lista vacía")

    errors.extend(f"  ✗ {p}" for p in settings.validate_credentials())

    if errors:
        log.error("Validación FALLIDA:")
        for e in errors:
            log.error(e)
        return False

    log.info("✓ OPENAI_API_KEY OK")
    log.info("✓ %d símbolo(s) configurado(s): %s", len(settings.symbol_list), settings.symbol_list)
    log.info("✓ Modo: %s", "DRY_RUN" if settings.dry_run else f"LIVE ({settings.simple_reality})")
    log.info("=" * 60)
    return True
