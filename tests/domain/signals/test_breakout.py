"""Tests del módulo de breakout (regla #6)."""

from __future__ import annotations

import pandas as pd

from domain.signals.breakout import BreakoutState, detect_breakout
from domain.strategy.box import Box


def test_breakout_above() -> None:
    box = Box(high=101.0, low=99.0, amplitude_pct=2.02, n_candles=10)
    df = pd.DataFrame(
        {
            "time": [100, 200, 300, 400, 500],
            "close": [100.0, 100.5, 100.8, 101.5, 102.0],
        }
    )
    signal = detect_breakout(df, box)
    assert signal is not None
    assert signal.state == BreakoutState.ABOVE


def test_breakout_below() -> None:
    box = Box(high=101.0, low=99.0, amplitude_pct=2.02, n_candles=10)
    df = pd.DataFrame(
        {
            "time": [100, 200, 300, 400, 500],
            "close": [100.0, 99.5, 98.0, 97.0, 96.0],
        }
    )
    signal = detect_breakout(df, box)
    assert signal is not None
    assert signal.state == BreakoutState.BELOW


def test_no_breakout_inside() -> None:
    box = Box(high=101.0, low=99.0, amplitude_pct=2.02, n_candles=10)
    df = pd.DataFrame(
        {
            "time": [100, 200, 300],
            "close": [100.0, 100.2, 100.5],
        }
    )
    signal = detect_breakout(df, box)
    assert signal is not None
    assert signal.state == BreakoutState.INSIDE


def test_first_breakout_wins() -> None:
    box = Box(high=101.0, low=99.0, amplitude_pct=2.02, n_candles=10)
    df = pd.DataFrame(
        {
            "time": [100, 200, 300, 400],
            "close": [100.0, 102.0, 103.0, 98.0],  # primero arriba, luego abajo
        }
    )
    signal = detect_breakout(df, box)
    assert signal is not None
    assert signal.state == BreakoutState.ABOVE  # el primero


def test_empty_df_returns_inside_with_defaults() -> None:
    box = Box(high=101.0, low=99.0, amplitude_pct=2.02, n_candles=10)
    df = pd.DataFrame(columns=["time", "close"])
    signal = detect_breakout(df, box)
    assert signal is None
