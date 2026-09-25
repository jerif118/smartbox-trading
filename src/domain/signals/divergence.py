"""
Divergencias precio/RSI (Regla #4).

Una divergencia es la discrepancia entre lo que hace el precio y lo que hace el
momento: el precio marca un máximo más alto pero el RSI marca un máximo más
bajo (bajista), o el precio marca un mínimo más bajo y el RSI uno más alto
(alcista). Es la señal de agotamiento que interesa cuando el bot va a entrar en
la dirección de la rotura: romper al alza con divergencia bajista encima es
entrar justo donde la fuerza se está acabando.

Se compara SOLO el último par de extremos del mismo tipo. Encadenar más
extremos multiplica los falsos positivos: cualquier serie larga acaba
conteniendo dos puntos que "divergen".

Los extremos son de SWING, no locales: un máximo del RSI cuenta solo si es el
mayor de su entorno de ``SWING_WINDOW`` velas a cada lado. La alternativa
—comparar simplemente con la vela anterior y la siguiente, como hace
``find_peaks_valleys``— devuelve decenas de micro-extremos sobre datos reales
de 5 minutos, y "los dos últimos" acaban siendo dos ondulaciones de ruido
consecutivas en vez de los dos máximos que un trader vería en el gráfico.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from domain.indicators.rsi import RSI_PERIOD, rsi_series

# Velas a cada lado que un extremo debe dominar para contar como swing.
SWING_WINDOW = 5

# Separación mínima entre los dos extremos comparados, en velas. Dos swings
# pegados son la misma estructura contada dos veces.
MIN_SEPARATION_CANDLES = 3

# Diferencia mínima en el RSI para aceptar que el segundo extremo es realmente
# más bajo/alto y no la misma lectura con otro decimal.
MIN_RSI_DELTA = 1.0

# Diferencia mínima en el precio, en % del propio precio. Filtra los "máximos
# más altos" que lo son por una décima en un índice de 7.800 puntos.
MIN_PRICE_DELTA_PCT = 0.02


@dataclass(frozen=True)
class _Swing:
    position: int  # índice dentro de la serie de RSI
    close: float
    rsi: float


def detect_rsi_divergence(
    df: pd.DataFrame,
    close_col: str = "close",
    time_col: str = "time",
    period: int = RSI_PERIOD,
    window: int = SWING_WINDOW,
) -> str | None:
    """``"BEARISH"`` | ``"BULLISH"`` | ``None``.

    BEARISH: el precio hace máximo más alto y el RSI máximo más bajo.
    BULLISH: el precio hace mínimo más bajo y el RSI mínimo más alto.
    Si aparecen las dos, gana la más reciente.
    """
    if df is None or len(df) == 0 or close_col not in df.columns:
        return None

    sub = df.reset_index(drop=True)
    rsi = rsi_series(sub[close_col], period)
    if rsi.empty or len(rsi) < 2 * window + 2:
        return None

    # Cierres recortados al tramo donde el RSI existe, ambos por posición.
    closes = sub[close_col].loc[rsi.index].reset_index(drop=True).tolist()
    rsi_values = rsi.tolist()

    highs = _swings(rsi_values, closes, window, high=True)
    lows = _swings(rsi_values, closes, window, high=False)

    bearish = _last_pair_diverges(highs, bearish=True)
    bullish = _last_pair_diverges(lows, bearish=False)

    if bearish and bullish:
        # Ambas: decide la que ocurrió después.
        return "BEARISH" if bearish.position >= bullish.position else "BULLISH"
    if bearish:
        return "BEARISH"
    if bullish:
        return "BULLISH"
    return None


def _swings(
    rsi_values: list[float], closes: list[float], window: int, *, high: bool
) -> list[_Swing]:
    """Extremos del RSI que dominan su entorno de ``window`` velas a cada lado."""
    points: list[_Swing] = []
    for i in range(window, len(rsi_values) - window):
        neighbourhood = rsi_values[i - window : i + window + 1]
        current = rsi_values[i]
        dominates = current >= max(neighbourhood) if high else current <= min(neighbourhood)
        if not dominates:
            continue
        # Un tramo plano domina su entorno en todas sus posiciones; nos
        # quedamos con la primera para no contar el mismo swing N veces.
        if points and i - points[-1].position <= window:
            continue
        points.append(_Swing(position=i, close=closes[i], rsi=current))
    return points


def _last_pair_diverges(points: list[_Swing], *, bearish: bool) -> _Swing | None:
    """Devuelve el extremo más reciente si el último par diverge, si no None."""
    if len(points) < 2:
        return None
    prev, last = points[-2], points[-1]

    if last.position - prev.position < MIN_SEPARATION_CANDLES:
        return None
    if abs(last.rsi - prev.rsi) < MIN_RSI_DELTA:
        return None
    if prev.close == 0:
        return None
    price_delta_pct = abs(last.close - prev.close) / abs(prev.close) * 100
    if price_delta_pct < MIN_PRICE_DELTA_PCT:
        return None

    if bearish:
        # Precio arriba, momento abajo.
        diverges = last.close > prev.close and last.rsi < prev.rsi
    else:
        # Precio abajo, momento arriba.
        diverges = last.close < prev.close and last.rsi > prev.rsi
    return last if diverges else None


def divergence_opposes(divergence: str | None, direction: str) -> bool:
    """True si la divergencia va CONTRA la dirección que se quiere operar."""
    if not divergence:
        return False
    direction = (direction or "").upper()
    return (direction == "LONG" and divergence == "BEARISH") or (
        direction == "SHORT" and divergence == "BULLISH"
    )
