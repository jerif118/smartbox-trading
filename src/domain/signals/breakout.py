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
    # Cuánto se alejó el cierre del borde de la caja, en % del rango de la
    # caja. Es la medida de calidad de la ruptura: un cierre 0.2 puntos fuera
    # de una caja de 32 puntos (0.6%) es ruido; uno de 4.7 puntos sobre una
    # caja de 43 (10.9%) es una rotura decidida. Sobre 235 rupturas reales de
    # US500+US100 esta métrica separa un 55.3% de acierto (todas) de un 67.0%
    # (>=10%) y un 78.8% (>=20%). Es el único factor medido que discrimina.
    penetration_pct: float = 0.0

    @property
    def is_directional(self) -> bool:
        return self.state in (BreakoutState.ABOVE, BreakoutState.BELOW)


def penetration_pct(close: float, box_high: float, box_low: float, state: BreakoutState) -> float:
    """Distancia del cierre más allá del borde roto, en % del rango de la caja."""
    box_range = box_high - box_low
    if box_range <= 0:
        return 0.0
    if state == BreakoutState.ABOVE:
        return max(0.0, (close - box_high) / box_range * 100)
    if state == BreakoutState.BELOW:
        return max(0.0, (box_low - close) / box_range * 100)
    return 0.0


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
        state = None
        if close > box.high:
            state = BreakoutState.ABOVE
        elif close < box.low:
            state = BreakoutState.BELOW
        if state is not None:
            return BreakoutSignal(
                state=state,
                candle_close=close,
                signal_time=_iso(int(row[time_col])),
                box_high=box.high,
                box_low=box.low,
                penetration_pct=penetration_pct(close, box.high, box.low, state),
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
