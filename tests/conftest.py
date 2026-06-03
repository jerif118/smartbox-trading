"""Fixtures compartidos para tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest


@pytest.fixture
def sample_candles_5min() -> pd.DataFrame:
    """Velas 5min de ejemplo (10 filas)."""
    base = int(datetime(2026, 6, 1, 14, 55, tzinfo=timezone.utc).timestamp())
    data = []
    prices = [100.0, 101.0, 100.5, 102.0, 101.5, 99.0, 100.0, 103.0, 102.5, 104.0]
    for i, p in enumerate(prices):
        ts = base + i * 300
        data.append(
            {
                "time": ts,
                "open": p - 0.5,
                "high": p + 1.0,
                "low": p - 1.0,
                "close": p,
                "volume": 1000 + i * 50,
            }
        )
    return pd.DataFrame(data)


@pytest.fixture
def sample_box() -> "Box":  # noqa: F821
    from domain.strategy.box import Box
    return Box(high=101.0, low=99.0, amplitude_pct=2.02, n_candles=10)
