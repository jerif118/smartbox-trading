"""
RSI. Regla #4: 14 períodos, detección de picos/valles para divergencias.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pandas as pd

RSI_PERIOD = 14


@dataclass(frozen=True)
class RSIPoint:
    time: int
    close: float
    rsi: float
    type: str  # "peak" | "valley"


def rsi_series(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Calcula RSI con pandas_ta."""
    try:
        import pandas_ta as ta
        rsi = ta.rsi(close, length=period)
        return rsi.dropna()
    except Exception:
        return _rsi_fallback(close, period)


def _rsi_fallback(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """RSI cálculo manual por si pandas_ta no está disponible."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))
    return rsi.dropna()


def find_peaks_valleys(
    rsi_values: pd.Series,
    close_values: pd.Series,
    times: Sequence[int],
) -> list[RSIPoint]:
    """Detecta picos (RSI[i] > vecinos) y valles (RSI[i] < vecinos)."""
    points: list[RSIPoint] = []
    rsi_arr = rsi_values.values
    for i in range(1, len(rsi_arr) - 1):
        is_peak = rsi_arr[i] > rsi_arr[i - 1] and rsi_arr[i] > rsi_arr[i + 1]
        is_valley = rsi_arr[i] < rsi_arr[i - 1] and rsi_arr[i] < rsi_arr[i + 1]
        if is_peak or is_valley:
            idx = rsi_values.index[i]
            points.append(
                RSIPoint(
                    time=int(times[idx]) if hasattr(times, "__getitem__") else int(idx),
                    close=float(close_values.iloc[i]),
                    rsi=float(rsi_arr[i]),
                    type="peak" if is_peak else "valley",
                )
            )
    return points


def last_rsi(close: pd.Series, period: int = RSI_PERIOD) -> float | None:
    series = rsi_series(close, period)
    if series.empty:
        return None
    return float(series.iloc[-1])
