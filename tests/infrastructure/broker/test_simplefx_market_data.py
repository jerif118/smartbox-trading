"""Tests del normalizador de velas SimpleFX (candles-core)."""

from __future__ import annotations

import pytest

from infrastructure.broker.simplefx.market_data import _normalize


def test_normalize_named_columns_seconds() -> None:
    data = [
        {"time": 1000, "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5},
        {"time": 1300, "open": 10.5, "high": 12.0, "low": 10.0, "close": 11.5},
    ]
    df = _normalize(data)
    assert list(df["time"]) == [1000, 1300]
    assert df["high"].max() == 12.0
    assert df["low"].min() == 9.0


def test_normalize_short_aliases_and_millis() -> None:
    """Acepta claves cortas (t/o/h/l/c) y convierte ms → s."""
    data = [
        {"t": 1_700_000_000_000, "o": 10, "h": 11, "l": 9, "c": 10.5},
    ]
    df = _normalize(data)
    assert int(df["time"].iloc[0]) == 1_700_000_000  # ms → s
    assert df["high"].iloc[0] == 11


def test_normalize_missing_required_column_raises() -> None:
    with pytest.raises(ValueError, match="faltan columnas"):
        _normalize([{"time": 1000, "open": 10.0}])  # sin high/low


def test_normalize_empty() -> None:
    assert _normalize([]).empty
