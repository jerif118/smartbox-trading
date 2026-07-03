"""
Símbolos permitidos del sistema.

Regla de negocio dura: el bot SOLO opera US500 y US100. Cualquier otro
símbolo (DE40, etc.) que entre por configuración o por un input externo se
ignora de forma segura y se loguea. Este módulo es la única fuente de verdad
del whitelist; todo el pipeline filtra a través de `filter_symbols`.
"""

from __future__ import annotations

from collections.abc import Iterable

from utils.logger import get_logger

log = get_logger(__name__)

# Whitelist canónico. Inmutable a propósito.
ALLOWED_SYMBOLS: tuple[str, ...] = ("US500", "US100")


def is_allowed(symbol: str) -> bool:
    """True si el símbolo (normalizado) está en el whitelist."""
    return symbol.strip().upper() in ALLOWED_SYMBOLS


def filter_symbols(symbols: Iterable[str], *, context: str = "") -> list[str]:
    """Filtra una colección de símbolos dejando solo los permitidos.

    - Normaliza (strip + upper).
    - Elimina duplicados preservando orden.
    - Ignora cualquier símbolo fuera del whitelist y lo loguea como warning.

    `context` se incluye en el log para rastrear de dónde vino el filtrado.
    """
    allowed: list[str] = []
    ignored: list[str] = []
    seen: set[str] = set()

    for raw in symbols:
        sym = (raw or "").strip().upper()
        if not sym:
            continue
        if sym in ALLOWED_SYMBOLS:
            if sym not in seen:
                allowed.append(sym)
                seen.add(sym)
        else:
            ignored.append(sym)

    if ignored:
        where = f" [{context}]" if context else ""
        log.warning(
            "Símbolos ignorados (fuera del whitelist %s)%s: %s",
            list(ALLOWED_SYMBOLS), where, ignored,
        )

    return allowed


def resolve_primary(primary: str, allowed: list[str]) -> str:
    """Devuelve un símbolo primario válido.

    Si el primario configurado no está dentro de los símbolos permitidos
    activos, cae al primero de la lista permitida (y lo loguea).
    """
    p = (primary or "").strip().upper()
    if p in allowed:
        return p
    fallback = allowed[0] if allowed else (ALLOWED_SYMBOLS[0])
    if p:
        log.warning(
            "PRIMARY_SYMBOL %r no está entre los símbolos activos %s → usando %r",
            p, allowed, fallback,
        )
    return fallback
