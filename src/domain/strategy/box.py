"""
Box (caja) de la estrategia SmartBox.

Reglas codificadas:
- #1: Si amplitud > 1% → NO OPERAR
- #2: high = max(highs), low = min(lows)
- #3: Box SimpleFX (parallel) se prefiere como entrada
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import pandas as pd

from domain.errors import InvalidBoxError

MAX_AMPLITUDE_PCT = 1.0  # Regla #1


@dataclass(frozen=True)
class Box:
    """Caja de precios para un símbolo y ventana temporal.

    Atributos:
        high:       máximo de la ventana
        low:        mínimo de la ventana
        amplitude_pct: ((high - low) / low) * 100
        n_candles:  cantidad de velas consideradas
    """

    high: float
    low: float
    amplitude_pct: float
    n_candles: int

    @property
    def mid(self) -> float:
        return round((self.high + self.low) / 2, 2)

    @property
    def range(self) -> float:
        return self.high - self.low

    def is_valid(self) -> bool:
        """Regla #1: amplitud <= 1%."""
        return self.amplitude_pct is not None and self.amplitude_pct <= MAX_AMPLITUDE_PCT

    def validate(self) -> None:
        """Lanza InvalidBoxError si no cumple las reglas."""
        if self.high is None or self.low is None:
            raise InvalidBoxError(f"Box sin niveles (high={self.high}, low={self.low})")
        if self.high <= self.low:
            raise InvalidBoxError(
                f"Box inválida: high ({self.high}) <= low ({self.low})"
            )
        if self.amplitude_pct is None:
            raise InvalidBoxError("Box sin amplitud calculada")
        if self.amplitude_pct > MAX_AMPLITUDE_PCT:
            raise InvalidBoxError(
                f"Amplitud {self.amplitude_pct}% > {MAX_AMPLITUDE_PCT}% (regla #1)"
            )
        if self.n_candles <= 0:
            raise InvalidBoxError(f"Box con {self.n_candles} velas")

    @classmethod
    def from_candles(
        cls,
        candles: pd.DataFrame | Sequence[dict],
        time_col: str = "time",
        high_col: str = "high",
        low_col: str = "low",
    ) -> Box:
        """Regla #2: high = max(highs), low = min(lows)."""
        if isinstance(candles, pd.DataFrame):
            if candles.empty:
                raise InvalidBoxError("DataFrame de velas vacío")
            high_price = float(candles[high_col].max())
            low_price = float(candles[low_col].min())
            n = len(candles)
        else:
            rows = list(candles)
            if not rows:
                raise InvalidBoxError("Lista de velas vacía")
            high_price = float(max(r[high_col] for r in rows))
            low_price = float(min(r[low_col] for r in rows))
            n = len(rows)

        if low_price == 0:
            amplitude = None
        else:
            amplitude = round((high_price - low_price) / low_price * 100, 2)

        return cls(
            high=high_price,
            low=low_price,
            amplitude_pct=amplitude if amplitude is not None else 0.0,
            n_candles=n,
        )


@dataclass(frozen=True)
class BoxPair:
    """Regla #3: par de boxes (Capital.com + SimpleFX) con preferencia SimpleFX.

    `primary` es la que se usa para entry/SL/TP; `secondary` es fallback.
    """

    capital: Box
    simple: Box | None = None

    @property
    def primary(self) -> Box:
        if self.simple is not None and self.simple.is_valid():
            return self.simple
        return self.capital

    @property
    def high(self) -> float:
        return self.primary.high

    @property
    def low(self) -> float:
        return self.primary.low

    @property
    def amplitude(self) -> float:
        return self.primary.amplitude_pct

    def is_valid(self) -> bool:
        return self.primary.is_valid()


def compute_box_from_df(
    df: pd.DataFrame,
    time_from: int,
    time_to: int,
) -> Box | None:
    """Filtra df por rango unix y calcula Box. None si no hay velas."""
    if df is None or df.empty:
        return None
    window = df[(df["time"] >= time_from) & (df["time"] <= time_to)]
    if window.empty:
        return None
    return Box.from_candles(window)


def select_valid_boxes(boxes: Iterable[Box]) -> list[Box]:
    """Filtra boxes que cumplen la regla #1 (amplitude <= 1%)."""
    return [b for b in boxes if b.is_valid()]
