"""
Contexto macro: eventos de calendario de alto impacto.

Regla #20: filtrar solo eventos HIGH impact.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum


class MacroRisk(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass(frozen=True)
class MacroEvent:
    time: str
    event: str
    currency: str
    impact: str  # "HIGH" | "MEDIUM" | "LOW"

    @property
    def is_high_impact(self) -> bool:
        return self.impact.upper() == "HIGH"


def filter_high_impact(events: Iterable[MacroEvent]) -> list[MacroEvent]:
    """Regla #20: retorna solo eventos HIGH impact."""
    return [e for e in events if e.is_high_impact]


def classify_risk(high_impact_events: Sequence[MacroEvent]) -> MacroRisk:
    """Heurística: 0 = LOW, 1-2 = MEDIUM, 3+ = HIGH."""
    n = len(high_impact_events)
    if n == 0:
        return MacroRisk.LOW
    if n <= 2:
        return MacroRisk.MEDIUM
    return MacroRisk.HIGH
