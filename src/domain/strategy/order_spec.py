"""
Order specification: calcula entry/SL/TP y valida coherencia.

Reglas codificadas:
- #9: R:R mínimo 1.5:1
- #10: 2 órdenes — primary (con SL+TP) + runner (solo SL)
- #13: LONG: entry=box_high, SL=box_low, TP=box_high+amp
- #14: SHORT: entry=box_low, SL=box_high, TP=box_low-amp
- #15: Coherencia (LONG: stop<entry<tp; SHORT: tp<entry<stop)
- #17: risk_pts > 0
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from domain.errors import CoherenceError, InsufficientRRError
from domain.strategy.box import Box
from domain.strategy.decision import Action
from domain.strategy.position_sizer import SizedPosition

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class OrderSpec:
    """Especificación de una orden individual (sin ejecutar)."""

    symbol: str
    side: Side
    volume: float
    entry_price: float
    stop_loss: float
    take_profit: float | None
    is_runner: bool = False
    decision_action: Action = Action.NO_OPERAR

    @property
    def risk_points(self) -> float:
        return abs(self.entry_price - self.stop_loss)

    @property
    def reward_points(self) -> float:
        if self.take_profit is None:
            return 0.0
        return abs(self.take_profit - self.entry_price)

    @property
    def rr_ratio(self) -> float:
        if self.risk_points <= 0:
            return 0.0
        return self.reward_points / self.risk_points

    def validate_levels(self) -> None:
        """Regla #15 + #17: niveles coherentes y risk > 0."""
        if self.risk_points <= 0:
            raise CoherenceError(
                f"{self.symbol}: risk_points <= 0 (entry={self.entry_price}, "
                f"SL={self.stop_loss})"
            )
        if self.side == "BUY":
            if not (self.stop_loss < self.entry_price):
                raise CoherenceError(
                    f"{self.symbol}: BUY requiere SL < entry "
                    f"(SL={self.stop_loss}, entry={self.entry_price})"
                )
            if self.take_profit is not None and not (self.entry_price < self.take_profit):
                raise CoherenceError(
                    f"{self.symbol}: BUY requiere entry < TP "
                    f"(entry={self.entry_price}, TP={self.take_profit})"
                )
        elif self.side == "SELL":
            if not (self.stop_loss > self.entry_price):
                raise CoherenceError(
                    f"{self.symbol}: SELL requiere SL > entry "
                    f"(SL={self.stop_loss}, entry={self.entry_price})"
                )
            if self.take_profit is not None and not (self.take_profit < self.entry_price):
                raise CoherenceError(
                    f"{self.symbol}: SELL requiere TP < entry "
                    f"(TP={self.take_profit}, entry={self.entry_price})"
                )

    def validate_rr(self, min_rr: float) -> None:
        """Regla #9: R:R >= min_rr."""
        if self.take_profit is None:
            return  # runner no requiere TP
        if self.rr_ratio + 1e-9 < min_rr:
            raise InsufficientRRError(
                f"{self.symbol}: R:R={self.rr_ratio:.2f} < min {min_rr}"
            )


@dataclass(frozen=True)
class ExecutionPlan:
    """Plan completo de ejecución: 1 decisión → 1 o 2 órdenes."""

    decision_action: Action
    primary: OrderSpec
    runner: OrderSpec

    @property
    def orders(self) -> list[OrderSpec]:
        return [self.primary, self.runner]

    def validate(self, min_rr: float) -> None:
        for order in self.orders:
            order.validate_levels()
            order.validate_rr(min_rr)


def build_long_plan(
    symbol: str,
    box: Box,
    sized: SizedPosition,
    decision_action: Action = Action.LONG,
) -> ExecutionPlan:
    """Regla #13: LONG: entry=box_high, SL=box_low, TP=box_high+amp."""
    entry = round(box.high, 1)
    stop = round(box.low, 1)
    tp = round(box.high + box.range, 1)
    return ExecutionPlan(
        decision_action=decision_action,
        primary=OrderSpec(
            symbol=symbol,
            side="BUY",
            volume=sized.primary,
            entry_price=entry,
            stop_loss=stop,
            take_profit=tp,
            is_runner=False,
            decision_action=decision_action,
        ),
        runner=OrderSpec(
            symbol=symbol,
            side="BUY",
            volume=sized.runner,
            entry_price=entry,
            stop_loss=stop,
            take_profit=None,  # runner sin TP
            is_runner=True,
            decision_action=decision_action,
        ),
    )


def build_short_plan(
    symbol: str,
    box: Box,
    sized: SizedPosition,
    decision_action: Action = Action.SHORT,
) -> ExecutionPlan:
    """Regla #14: SHORT: entry=box_low, SL=box_high, TP=box_low-amp."""
    entry = round(box.low, 1)
    stop = round(box.high, 1)
    tp = round(box.low - box.range, 1)
    return ExecutionPlan(
        decision_action=decision_action,
        primary=OrderSpec(
            symbol=symbol,
            side="SELL",
            volume=sized.primary,
            entry_price=entry,
            stop_loss=stop,
            take_profit=tp,
            is_runner=False,
            decision_action=decision_action,
        ),
        runner=OrderSpec(
            symbol=symbol,
            side="SELL",
            volume=sized.runner,
            entry_price=entry,
            stop_loss=stop,
            take_profit=None,
            is_runner=True,
            decision_action=decision_action,
        ),
    )


def build_execution_plan(
    symbol: str,
    box: Box,
    sized: SizedPosition,
    action: Action,
) -> ExecutionPlan:
    """Factory: LONG → build_long_plan, SHORT → build_short_plan."""
    if action == Action.LONG:
        return build_long_plan(symbol, box, sized, decision_action=action)
    if action == Action.SHORT:
        return build_short_plan(symbol, box, sized, decision_action=action)
    raise ValueError(f"Cannot build plan for action {action}")
