"""Divergencias precio/RSI."""

from __future__ import annotations

import numpy as np
import pandas as pd

from domain.signals.divergence import detect_rsi_divergence, divergence_opposes

CANDLE_S = 300


def _df(closes: list[float]) -> pd.DataFrame:
    times = [1_700_000_000 + i * CANDLE_S for i in range(len(closes))]
    return pd.DataFrame(
        {
            "time": times,
            "open": closes,
            "high": [c * 1.001 for c in closes],
            "low": [c * 0.999 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        }
    )


def _swings(legs: list[tuple[float, float, int]], wobble: float = 0.35) -> list[float]:
    """Encadena tramos de precio con una oscilación pequeña encima.

    La oscilación no es decorativa: un tramo perfectamente lineal produce
    deltas idénticos, el RSI se queda plano y no forma ningún extremo. Con
    tramos rectos puros el detector no tendría nada que comparar y el test
    pasaría por el motivo equivocado.
    """
    closes: list[float] = []
    for i, (start, end, n) in enumerate(legs):
        base = np.linspace(start, end, n)
        closes.extend(base + wobble * np.sin(np.arange(n) * 1.7 + i))
    return [float(c) for c in closes]


def test_detecta_divergencia_bajista() -> None:
    """Impulso fuerte, corrección, y máximo más alto pero mucho más lento.

    El precio marca un máximo superior; el RSI, uno inferior.
    """
    closes = _swings([(100, 130, 40), (130, 118, 15), (118, 131, 45), (131, 129, 6)])

    assert detect_rsi_divergence(_df(closes)) == "BEARISH"


def test_detecta_divergencia_alcista() -> None:
    """El caso espejo: mínimo más bajo en precio, mínimo más alto en RSI."""
    closes = _swings([(130, 100, 40), (100, 112, 15), (112, 99, 45), (99, 101, 6)])

    assert detect_rsi_divergence(_df(closes)) == "BULLISH"


def test_tendencia_limpia_no_produce_divergencia() -> None:
    """Una subida sostenida no tiene dos swings que diverjan."""
    closes = _swings([(100, 200, 140)], wobble=0.05)

    assert detect_rsi_divergence(_df(closes)) is None


def test_serie_corta_devuelve_none() -> None:
    assert detect_rsi_divergence(_df([100.0, 101.0, 102.0])) is None


def test_df_vacio_devuelve_none() -> None:
    assert detect_rsi_divergence(pd.DataFrame()) is None
    assert detect_rsi_divergence(None) is None


def test_sin_columna_close_devuelve_none() -> None:
    assert detect_rsi_divergence(pd.DataFrame({"time": [1, 2, 3]})) is None


def test_divergence_opposes() -> None:
    assert divergence_opposes("BEARISH", "LONG") is True
    assert divergence_opposes("BULLISH", "SHORT") is True
    assert divergence_opposes("BULLISH", "LONG") is False
    assert divergence_opposes("BEARISH", "SHORT") is False
    assert divergence_opposes(None, "LONG") is False
