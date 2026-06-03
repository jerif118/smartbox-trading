"""Tests de Decision, position_sizer y budget (reglas #11, #16, #18)."""

from __future__ import annotations

import pytest

from domain.errors import BudgetExceededError, ConfidenceTooLowError, DirectionMismatchError
from domain.signals.breakout import BreakoutState
from domain.strategy.budget import ORDERS_PER_SYMBOL, DailyOrderBudget
from domain.strategy.decision import Action, Decision, RiskMode
from domain.strategy.position_sizer import size_position, split_for_risk


def test_decision_creation() -> None:
    d = Decision(
        symbol="US500", action=Action.LONG, risk=RiskMode.COMPLETO,
        confidence=80, reasons=("test reason",),
    )
    assert d.is_tradeable


def test_decision_no_op_not_tradeable() -> None:
    d = Decision(
        symbol="US500", action=Action.NO_OPERAR, risk=RiskMode.COMPLETO,
        confidence=80, reasons=("skip",),
    )
    assert not d.is_tradeable


def test_decision_confidence_out_of_range() -> None:
    with pytest.raises(ValueError):
        Decision(
            symbol="X", action=Action.LONG, risk=RiskMode.COMPLETO,
            confidence=150, reasons=("x",),
        )


def test_decision_requires_reason() -> None:
    with pytest.raises(ValueError):
        Decision(
            symbol="X", action=Action.LONG, risk=RiskMode.COMPLETO,
            confidence=80, reasons=(),
        )


def test_decision_validates_confidence() -> None:
    d = Decision(
        symbol="X", action=Action.LONG, risk=RiskMode.COMPLETO,
        confidence=30, reasons=("x",),
    )
    d.validate_confidence(min_confidence=20)
    with pytest.raises(ConfidenceTooLowError):
        d.validate_confidence(min_confidence=60)


def test_decision_validates_direction() -> None:
    long_d = Decision(
        symbol="X", action=Action.LONG, risk=RiskMode.COMPLETO,
        confidence=80, reasons=("x",),
    )
    short_d = Decision(
        symbol="X", action=Action.SHORT, risk=RiskMode.COMPLETO,
        confidence=80, reasons=("x",),
    )
    long_d.validate_direction(BreakoutState.ABOVE)
    short_d.validate_direction(BreakoutState.BELOW)
    with pytest.raises(DirectionMismatchError):
        long_d.validate_direction(BreakoutState.BELOW)
    with pytest.raises(DirectionMismatchError):
        short_d.validate_direction(BreakoutState.ABOVE)


def test_split_for_risk_completo() -> None:
    assert split_for_risk(1.0, RiskMode.COMPLETO) == (0.5, 0.5)


def test_split_for_risk_medio() -> None:
    assert split_for_risk(1.0, RiskMode.MEDIO) == (0.25, 0.25)


def test_split_for_risk_invalid_volume() -> None:
    with pytest.raises(ValueError):
        split_for_risk(0, RiskMode.COMPLETO)
    with pytest.raises(ValueError):
        split_for_risk(-1, RiskMode.COMPLETO)


def test_size_position_returns_sized() -> None:
    pos = size_position(2.0, RiskMode.COMPLETO)
    assert pos.primary == 1.0
    assert pos.runner == 1.0
    assert pos.total == 2.0


def test_budget_initial_state() -> None:
    b = DailyOrderBudget(max_orders=4)
    assert b.remaining == 4
    assert b.used == 0


def test_budget_can_send() -> None:
    b = DailyOrderBudget(max_orders=4)
    assert b.can_send(2)  # 0+2 <= 4
    b.consume(2)  # used=2
    assert b.can_send(2)  # 2+2 <= 4 (justo al límite)
    b.consume(2)  # used=4
    assert not b.can_send(2)  # 4+2 > 4
    assert b.can_send(0)  # 0 siempre OK


def test_budget_consume_increments() -> None:
    b = DailyOrderBudget(max_orders=4)
    b.consume(2)
    assert b.used == 2
    b.consume(2)
    assert b.used == 4
    assert b.remaining == 0


def test_budget_exceeded_raises() -> None:
    b = DailyOrderBudget(max_orders=4)
    b.consume(2)
    b.consume(2)
    with pytest.raises(BudgetExceededError):
        b.consume(2)


def test_budget_try_consume_doesnt_raise() -> None:
    b = DailyOrderBudget(max_orders=2)
    assert b.try_consume(2) is True
    assert b.try_consume(1) is False
    assert b.used == 2


def test_orders_per_symbol_constant() -> None:
    assert ORDERS_PER_SYMBOL == 2
