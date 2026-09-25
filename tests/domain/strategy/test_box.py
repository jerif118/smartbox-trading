"""Tests del módulo Box (reglas #1, #2, #3)."""

from __future__ import annotations

import pandas as pd
import pytest

from domain.errors import InvalidBoxError
from domain.strategy.box import (
    MAX_AMPLITUDE_PCT,
    Box,
    BoxPair,
    compute_box_from_df,
    max_amplitude_for,
    select_valid_boxes,
)


def test_box_creation_basic() -> None:
    box = Box(high=101.0, low=99.0, amplitude_pct=2.02, n_candles=10)
    assert box.mid == 100.0
    assert box.range == 2.0


def test_box_valid_under_1pct() -> None:
    box = Box(high=100.5, low=100.0, amplitude_pct=0.5, n_candles=10)
    assert box.is_valid()
    box.validate()


def test_box_invalid_over_1pct() -> None:
    box = Box(high=102.0, low=100.0, amplitude_pct=2.0, n_candles=10)
    assert not box.is_valid()
    with pytest.raises(InvalidBoxError):
        box.validate()


def test_max_amplitude_is_per_symbol() -> None:
    """US500 mantiene el 1%; US100 admite hasta 1.5%."""
    assert max_amplitude_for("US500") == 1.0
    assert max_amplitude_for("US100") == 1.5
    assert max_amplitude_for("us100") == 1.5  # normaliza
    # Un símbolo desconocido cae al límite más estricto, nunca al más laxo.
    assert max_amplitude_for("DE40") == MAX_AMPLITUDE_PCT
    assert max_amplitude_for("") == MAX_AMPLITUDE_PCT


def test_box_amplitude_gate_respects_symbol_limit() -> None:
    """Una caja del 1.2% es válida para US100 e inválida para US500."""
    box = Box(high=101.2, low=100.0, amplitude_pct=1.2, n_candles=10)

    assert box.is_valid(max_amplitude_for("US100")) is True
    box.validate(max_amplitude_for("US100"))

    assert box.is_valid(max_amplitude_for("US500")) is False
    with pytest.raises(InvalidBoxError):
        box.validate(max_amplitude_for("US500"))

    # Sin argumento se aplica el default (el estricto): omitirlo no afloja nada.
    assert box.is_valid() is False

    # Y por encima de 1.5% tampoco opera US100.
    too_wide = Box(high=101.6, low=100.0, amplitude_pct=1.6, n_candles=10)
    assert too_wide.is_valid(max_amplitude_for("US100")) is False


def test_box_high_less_than_low_raises() -> None:
    box = Box(high=99.0, low=101.0, amplitude_pct=-2.0, n_candles=10)
    with pytest.raises(InvalidBoxError):
        box.validate()


def test_box_from_empty_candles() -> None:
    with pytest.raises(InvalidBoxError):
        Box.from_candles([])


def test_box_from_df() -> None:
    df = pd.DataFrame(
        {"time": [1, 2, 3], "high": [10, 12, 11], "low": [9, 10, 8]}
    )
    box = Box.from_candles(df)
    assert box.high == 12
    assert box.low == 8


def test_box_pair_prefers_simple_when_valid() -> None:
    cap = Box(high=100.0, low=98.0, amplitude_pct=2.04, n_candles=10)
    simple = Box(high=100.5, low=99.5, amplitude_pct=1.0, n_candles=10)
    pair = BoxPair(capital=cap, simple=simple)
    assert pair.high == 100.5
    assert pair.low == 99.5


def test_box_pair_falls_back_to_capital() -> None:
    cap = Box(high=100.0, low=98.0, amplitude_pct=2.04, n_candles=10)
    bad = Box(high=100.5, low=99.0, amplitude_pct=5.0, n_candles=10)
    pair = BoxPair(capital=cap, simple=bad)
    assert pair.high == 100.0


def test_box_with_non_positive_low_is_invalid() -> None:
    """low <= 0 hace la amplitud incalculable: la caja no puede pasar el gate."""
    df = pd.DataFrame({"time": [1, 2], "high": [1.0, 2.0], "low": [0.0, 0.5]})
    box = Box.from_candles(df)
    assert box.is_valid() is False
    with pytest.raises(InvalidBoxError):
        box.validate()


def test_select_valid_boxes_filters() -> None:
    valid = Box(high=100.5, low=100.0, amplitude_pct=0.5, n_candles=10)
    invalid = Box(high=102.0, low=100.0, amplitude_pct=2.0, n_candles=10)
    result = select_valid_boxes([valid, invalid])
    assert len(result) == 1
    assert result[0].amplitude_pct == 0.5


def test_max_amplitude_constant() -> None:
    assert MAX_AMPLITUDE_PCT == 1.0


def test_compute_box_from_df_with_window() -> None:
    df = pd.DataFrame(
        {
            "time": [100, 200, 300, 400],
            "high": [10, 20, 15, 12],
            "low": [9, 19, 14, 11],
        }
    )
    box = compute_box_from_df(df, time_from=150, time_to=350)
    assert box is not None
    assert box.high == 20
    assert box.low == 14
