"""
Decisión del crew y validación contra breakout.

Reglas codificadas:
- #8: dirección de la decisión debe coincidir con el breakout
- #18: confianza >= MIN_CONFIDENCE
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from domain.errors import ConfidenceTooLowError, DirectionMismatchError
from domain.signals.breakout import BreakoutState


class Action(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NO_OPERAR = "NO_OPERAR"


class RiskMode(str, Enum):
    COMPLETO = "COMPLETO"
    MEDIO = "MEDIO"


@dataclass(frozen=True)
class Decision:
    """Decisión de trading producida por el crew."""

    symbol: str
    action: Action
    risk: RiskMode
    confidence: int
    reasons: tuple[str, ...]
    team_consensus: str = "unanimous"

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 100:
            raise ValueError(f"confidence must be 0-100, got {self.confidence}")
        if not self.reasons:
            raise ValueError("Decision must have at least one reason")

    @property
    def is_tradeable(self) -> bool:
        return self.action in (Action.LONG, Action.SHORT)

    def validate_confidence(self, min_confidence: int) -> None:
        """Regla #18: si confidence < min → skip."""
        if self.confidence < min_confidence:
            raise ConfidenceTooLowError(
                f"{self.symbol}: confianza {self.confidence} < min {min_confidence}"
            )

    def validate_direction(self, breakout_state: BreakoutState) -> None:
        """Regla #8: dirección debe coincidir con breakout."""
        if breakout_state == BreakoutState.ABOVE and self.action == Action.SHORT:
            raise DirectionMismatchError(
                f"{self.symbol}: decisión SHORT pero breakout fue ABOVE"
            )
        if breakout_state == BreakoutState.BELOW and self.action == Action.LONG:
            raise DirectionMismatchError(
                f"{self.symbol}: decisión LONG pero breakout fue BELOW"
            )


Direction = Literal["BUY", "SELL"]
