"""Tests del módulo Volume Profile (regla #5)."""

from __future__ import annotations

import pandas as pd

from domain.indicators.volume_profile import compute_volume_profile


def test_vp_poc_in_range() -> None:
    df = pd.DataFrame(
        {
            "time": range(100),
            "open": [100.0] * 100,
            "high": [101.0] * 100,
            "low": [99.0] * 100,
            "close": [100.0] * 100,
            "volume": [1000] * 100,
        }
    )
    vp = compute_volume_profile(df)
    assert vp is not None
    assert 99.0 <= vp.poc <= 101.0


def test_vp_value_area_brackets_poc() -> None:
    df = pd.DataFrame(
        {
            "time": range(200),
            "open": [50.0] * 200,
            "high": [52.0] * 200,
            "low": [48.0] * 200,
            "close": [50.0] * 200,
            "volume": [1500] * 200,
        }
    )
    vp = compute_volume_profile(df)
    assert vp is not None
    assert vp.val <= vp.poc <= vp.vah


def test_vp_empty_df_returns_none() -> None:
    df = pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    assert compute_volume_profile(df) is None


def test_vp_no_volume_returns_none() -> None:
    df = pd.DataFrame(
        {
            "time": range(10),
            "open": [100.0] * 10,
            "high": [101.0] * 10,
            "low": [99.0] * 10,
            "close": [100.0] * 10,
            "volume": [0] * 10,
        }
    )
    assert compute_volume_profile(df) is None
