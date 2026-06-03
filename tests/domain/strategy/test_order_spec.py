"""Tests de OrderSpec y ExecutionPlan (reglas #9, #10, #13, #14, #15, #17)."""

from __future__ import annotations

import pytest

from domain.errors import CoherenceError, InsufficientRRError
from domain.strategy.box import Box
from domain.strategy.decision import Action, RiskMode
from domain.strategy.order_spec import (
    OrderSpec,
    build_execution_plan,
    build_long_plan,
    build_short_plan,
)
from domain.strategy.position_sizer import size_position


def _box() -> Box:
    return Box(high=101.0, low=99.0, amplitude_pct=2.02, n_candles=10)


def _sized() -> "SizedPosition":
    from domain.strategy.position_sizer import SizedPosition
    return SizedPosition(primary=0.5, runner=0.5)


def test_long_plan_uses_box_levels() -> None:
    plan = build_long_plan("US500", _box(), _sized(), decision_action=Action.LONG)
    assert plan.primary.entry_price == 101.0
    assert plan.primary.stop_loss == 99.0
    assert plan.primary.take_profit == 103.0
    assert plan.primary.side == "BUY"


def test_short_plan_uses_box_levels() -> None:
    plan = build_short_plan("US500", _box(), _sized(), decision_action=Action.SHORT)
    assert plan.primary.entry_price == 99.0
    assert plan.primary.stop_loss == 101.0
    assert plan.primary.take_profit == 97.0
    assert plan.primary.side == "SELL"


def test_runner_has_no_tp() -> None:
    plan = build_long_plan("US500", _box(), _sized())
    assert plan.runner.take_profit is None
    assert plan.runner.is_runner is True


def test_primary_has_tp() -> None:
    plan = build_long_plan("US500", _box(), _sized())
    assert plan.primary.take_profit is not None
    assert plan.primary.is_runner is False


def test_plan_validates_levels() -> None:
    plan = build_long_plan("US500", _box(), _sized())
    # template produce R:R=1.0
    plan.validate(min_rr=1.0)


def test_plan_rr_below_min_raises() -> None:
    plan = build_long_plan("US500", _box(), _sized())
    with pytest.raises(InsufficientRRError):
        plan.validate(min_rr=2.0)


def test_order_with_zero_risk_raises() -> None:
    bad = OrderSpec(
        symbol="X", side="BUY", volume=1.0,
        entry_price=100.0, stop_loss=100.0, take_profit=110.0,
    )
    with pytest.raises(CoherenceError):
        bad.validate_levels()


def test_long_with_wrong_sl_raises() -> None:
    bad = OrderSpec(
        symbol="X", side="BUY", volume=1.0,
        entry_price=100.0, stop_loss=105.0, take_profit=110.0,  # SL > entry
    )
    with pytest.raises(CoherenceError):
        bad.validate_levels()


def test_short_with_wrong_sl_raises() -> None:
    bad = OrderSpec(
        symbol="X", side="SELL", volume=1.0,
        entry_price=100.0, stop_loss=95.0, take_profit=90.0,  # SL < entry
    )
    with pytest.raises(CoherenceError):
        bad.validate_levels()


def test_build_execution_plan_factory() -> None:
    plan = build_execution_plan("US500", _box(), _sized(), Action.LONG)
    assert plan.primary.side == "BUY"
    plan2 = build_execution_plan("US500", _box(), _sized(), Action.SHORT)
    assert plan2.primary.side == "SELL"
    with pytest.raises(ValueError):
        build_execution_plan("US500", _box(), _sized(), Action.NO_OPERAR)


def test_rr_ratio_computation() -> None:
    plan = build_long_plan("US500", _box(), _sized())
    # risk = 2, reward = 2, rr = 1.0
    assert plan.primary.rr_ratio == pytest.approx(1.0, abs=1e-6)
