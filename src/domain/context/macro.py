"""
Contexto macro: eventos de calendario de alto impacto.

Regla #20: filtrar solo eventos HIGH impact.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
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
    source: str = ""

    @property
    def is_high_impact(self) -> bool:
        return self.impact.upper() == "HIGH"


def filter_high_impact(events: Iterable[MacroEvent]) -> list[MacroEvent]:
    """Regla #20: retorna solo eventos HIGH impact."""
    return [e for e in events if e.is_high_impact]


def minutes_to_nearest_event(
    high_impact_events: Sequence[MacroEvent], reference: datetime
) -> float | None:
    """Minutos firmados hasta el evento HIGH más cercano.

    Los eventos deben traer un timestamp ISO-8601 en ``time``. Los registros
    sin hora parseable se conservan para mostrar, pero no inventan un veto.
    """
    nearest: float | None = None
    for event in high_impact_events:
        try:
            event_dt = datetime.fromisoformat(event.time.replace("Z", "+00:00"))
            if event_dt.tzinfo is None:
                event_dt = event_dt.replace(tzinfo=reference.tzinfo)
            delta = (event_dt - reference).total_seconds() / 60
        except (TypeError, ValueError):
            continue
        if nearest is None or abs(delta) < abs(nearest):
            nearest = delta
    return nearest


def classify_risk(
    high_impact_events: Sequence[MacroEvent],
    reference: datetime | None = None,
    *,
    blackout_minutes: int = 15,
    caution_minutes: int = 120,
) -> MacroRisk:
    """Clasifica por proximidad; conserva la heurística vieja sin referencia.

    HIGH solo significa que hay un evento realmente dentro del blackout.
    MEDIUM reduce tamaño durante la ventana de cautela. Un evento alejado no
    bloquea una señal válida.
    """
    if not high_impact_events:
        return MacroRisk.LOW
    if reference is None:
        return MacroRisk.MEDIUM if len(high_impact_events) <= 2 else MacroRisk.HIGH
    nearest = minutes_to_nearest_event(high_impact_events, reference)
    if nearest is None:
        return MacroRisk.MEDIUM
    distance = abs(nearest)
    if distance <= blackout_minutes:
        return MacroRisk.HIGH
    if distance <= caution_minutes:
        return MacroRisk.MEDIUM
    return MacroRisk.LOW
