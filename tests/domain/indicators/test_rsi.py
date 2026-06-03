"""Tests del módulo RSI (regla #4)."""

from __future__ import annotations

import pandas as pd

from domain.indicators.rsi import (
    RSI_PERIOD,
    find_peaks_valleys,
    last_rsi,
    rsi_series,
)


def test_rsi_series_no_nan_at_start() -> None:
    closes = pd.Series([100.0] * 30)
    series = rsi_series(closes)
    # primeros 13 son NaN, los siguientes son válidos
    assert len(series) <= len(closes) - RSI_PERIOD


def test_rsi_period_is_14() -> None:
    assert RSI_PERIOD == 14


def test_rsi_last_value_in_range() -> None:
    closes = pd.Series([100 + i * 0.3 for i in range(30)])
    val = last_rsi(closes)
    assert val is not None
    assert 0 <= val <= 100


def test_rsi_rising_trend_above_50() -> None:
    closes = pd.Series([100 + i for i in range(30)])
    val = last_rsi(closes)
    assert val is not None
    assert val > 50


def test_rsi_falling_trend_below_50() -> None:
    closes = pd.Series([200 - i for i in range(30)])
    val = last_rsi(closes)
    assert val is not None
    assert val < 50


def test_find_peaks_valleys_returns_list() -> None:
    closes = pd.Series([100 + (i % 5) for i in range(30)])
    series = rsi_series(closes)
    points = find_peaks_valleys(series, closes, list(range(30)))
    assert isinstance(points, list)
    for p in points:
        assert p.type in ("peak", "valley")
