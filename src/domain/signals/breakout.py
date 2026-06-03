"""
Detección de breakout. Regla #6:
- Velas de 5min, ventana 2h post-caja
- Primer cierre fuera de la caja = breakout
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from enum import Enum
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from domain.strategy.box import Box


class BreakoutState(str, Enum):
    ABOVE = "ABOVE"
    BELOW = "BELOW"
    INSIDE = "INSIDE"
    NONE = "NONE"


@dataclass(frozen=True)
class BreakoutSignal:
    state: BreakoutState
    candle_close: float
    signal_time: str  # ISO8601
    box_high: float
    box_low: float


def detect_breakout(
    df: pd.DataFrame,
    box: Box,
    time_col: str = "time",
    close_col: str = "close",
) -> BreakoutSignal | None:
    """Itera velas en orden; devuelve el primer cierre fuera de la caja.

    Regla #6: ventana de 2h post-caja (el caller debe haberla aplicado).
    """
    if df is None or df.empty:
        return None

    candles = df.sort_values(time_col)
    for _, row in candles.iterrows():
        close = float(row[close_col])
        if close > box.high:
            return BreakoutSignal(
                state=BreakoutState.ABOVE,
                candle_close=close,
                signal_time=_iso(int(row[time_col])),
                box_high=box.high,
                box_low=box.low,
            )
        if close < box.low:
            return BreakoutSignal(
                state=BreakoutState.BELOW,
                candle_close=close,
                signal_time=_iso(int(row[time_col])),
                box_high=box.high,
                box_low=box.low,
            )

    return BreakoutSignal(
        state=BreakoutState.INSIDE,
        candle_close=float(candles[close_col].iloc[-1]),
        signal_time=_iso(int(candles[time_col].iloc[-1])),
        box_high=box.high,
        box_low=box.low,
    )


def _iso(ts: int) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
