"""
Excepciones de dominio. Jerarquía clara para que cada capa sepa qué
catchear sin acoplarse a detalles de infraestructura.
"""

from __future__ import annotations


class DomainError(Exception):
    """Error base del dominio. Las capas externas lo capturan genéricamente."""


class InvalidBoxError(DomainError):
    """La caja no cumple las reglas (amplitud > 1%, velas insuficientes, etc.)."""


class InsufficientRRError(DomainError):
    """El ratio R:R está por debajo del mínimo configurado."""


class CoherenceError(DomainError):
    """Los niveles entry/SL/TP son incoherentes para la dirección."""


class BudgetExceededError(DomainError):
    """Se alcanzó MAX_ORDERS_PER_DAY."""


class DirectionMismatchError(DomainError):
    """La dirección de la decisión no concuerda con el breakout detectado."""


class ConfidenceTooLowError(DomainError):
    """La confianza del agente está por debajo de MIN_CONFIDENCE."""


class MissingLevelsError(DomainError):
    """Faltan niveles clave (box_high/box_low) para construir la orden."""
