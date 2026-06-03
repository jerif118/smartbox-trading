"""
Position sizer. Regla #16:
- COMPLETO → vol/2 cada orden
- MEDIO → vol/4 cada orden
"""

from __future__ import annotations

from dataclasses import dataclass

from domain.strategy.decision import RiskMode


def split_for_risk(base_volume: float, risk: RiskMode) -> tuple[float, float]:
    """Retorna (primary_volume, runner_volume).

    Cada una es la mitad del volumen asignado al risk mode.
    """
    if base_volume <= 0:
        raise ValueError(f"base_volume must be > 0, got {base_volume}")

    if risk == RiskMode.COMPLETO:
        per_order = base_volume / 2
    elif risk == RiskMode.MEDIO:
        per_order = base_volume / 4
    else:
        raise ValueError(f"Unknown risk mode: {risk}")

    return (round(per_order, 4), round(per_order, 4))


@dataclass(frozen=True)
class SizedPosition:
    primary: float
    runner: float

    @property
    def total(self) -> float:
        return self.primary + self.runner


def size_position(base_volume: float, risk: RiskMode) -> SizedPosition:
    primary, runner = split_for_risk(base_volume, risk)
    return SizedPosition(primary=primary, runner=runner)
