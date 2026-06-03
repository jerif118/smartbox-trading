"""Dominio: entidades y reglas puras de la estrategia SmartBox."""

from domain.errors import (
    BudgetExceededError,
    CoherenceError,
    ConfidenceTooLowError,
    DirectionMismatchError,
    DomainError,
    InsufficientRRError,
    InvalidBoxError,
    MissingLevelsError,
)
from domain.indicators.rsi import RSI_PERIOD, RSIPoint, find_peaks_valleys, last_rsi, rsi_series
from domain.indicators.volume_profile import (
    DEFAULT_N_BINS,
    DEFAULT_VA_PCT,
    VolumeProfile,
    compute_volume_profile,
)
from domain.signals.breakout import BreakoutSignal, BreakoutState, detect_breakout
from domain.strategy.box import (
    MAX_AMPLITUDE_PCT,
    Box,
    BoxPair,
    compute_box_from_df,
    select_valid_boxes,
)
from domain.strategy.budget import ORDERS_PER_SYMBOL, DailyOrderBudget
from domain.strategy.decision import Action, Decision, RiskMode
from domain.strategy.order_spec import (
    ExecutionPlan,
    OrderSpec,
    build_execution_plan,
    build_long_plan,
    build_short_plan,
)
from domain.strategy.position_sizer import SizedPosition, size_position, split_for_risk

__all__ = [
    # box
    "Box",
    "BoxPair",
    "MAX_AMPLITUDE_PCT",
    "compute_box_from_df",
    "select_valid_boxes",
    # decision
    "Action",
    "Decision",
    "RiskMode",
    # breakout
    "BreakoutSignal",
    "BreakoutState",
    "detect_breakout",
    # order
    "OrderSpec",
    "ExecutionPlan",
    "build_long_plan",
    "build_short_plan",
    "build_execution_plan",
    # budget
    "DailyOrderBudget",
    "ORDERS_PER_SYMBOL",
    # position sizer
    "SizedPosition",
    "size_position",
    "split_for_risk",
    # indicators
    "RSIPoint",
    "RSI_PERIOD",
    "rsi_series",
    "find_peaks_valleys",
    "last_rsi",
    "VolumeProfile",
    "compute_volume_profile",
    "DEFAULT_VA_PCT",
    "DEFAULT_N_BINS",
    # errors
    "DomainError",
    "InvalidBoxError",
    "InsufficientRRError",
    "CoherenceError",
    "BudgetExceededError",
    "DirectionMismatchError",
    "ConfidenceTooLowError",
    "MissingLevelsError",
]
