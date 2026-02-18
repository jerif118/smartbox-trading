"""
Validación de variables de entorno requeridas.
Se ejecuta al inicio del proceso para fallar rápido si falta config.
"""

import os
import sys
from utils.logger import get_logger

log = get_logger(__name__)

REQUIRED_VARS = {
    # Capital.com
    "EMAIL":       "Email para login Capital.com",
    "PASSWORD":    "Password para login Capital.com",
    "API_KEY":     "API Key de Capital.com",
    # SimpleFX
    "ID":          "Client ID de SimpleFX",
    "KEY":         "Client Secret de SimpleFX",
    "SIMPLE_ACCOUNT": "Número de cuenta SimpleFX (no placeholder)",
    # OpenAI
    "OPENAI_API_KEY": "API Key de OpenAI para el modelo LLM",
    # Operativa
    "SYMBOLS":     "Símbolos a operar (ej: US500,US100)",
    "TIMEFRAME":   "Timeframe de velas (ej: MINUTE_5)",
    "START_VP":    "Fecha/hora inicio datos",
    "END_VP":      "Fecha/hora fin datos",
}

PLACEHOLDER_VALUES = {"123456", "your-key-here", "changeme", "xxx", "placeholder"}


def validate_env() -> bool:
    """
    Valida que todas las variables de entorno requeridas estén definidas
    y no tengan valores placeholder.
    Retorna True si todo OK, False si hay errores.
    """
    errors: list[str] = []

    for var, desc in REQUIRED_VARS.items():
        val = os.getenv(var)
        if not val:
            errors.append(f"  ✗ {var} — no definida ({desc})")
        elif val.strip().lower() in PLACEHOLDER_VALUES:
            errors.append(f"  ✗ {var} — tiene valor placeholder '{val}' ({desc})")

    if errors:
        log.error("Validación de entorno FALLIDA:")
        for e in errors:
            log.error(e)
        return False

    log.info("Validación de entorno OK — %d variables verificadas", len(REQUIRED_VARS))
    return True
