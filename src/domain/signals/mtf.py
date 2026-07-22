"""
Multi-timeframe bias/alignment (lógica pura).

Sin I/O: recibe series de cierres ya descargadas. La orquestación (descarga de
velas por timeframe vía el broker) vive en la capa de aplicación, que reutiliza
estas funciones — igual que la tool del LLM.
"""

from __future__ import annotations

from collections.abc import Sequence

EMA_SPAN = 20
MIN_CANDLES = 20


def ema_bias(closes: Sequence[float]) -> str:
    """Sesgo BULLISH/BEARISH/NEUTRAL comparando el último cierre con su EMA20."""
    n = len(closes)
    if n < MIN_CANDLES:
        return "NEUTRAL"
    # EMA exponencial simple (equivalente a pandas ewm(span, adjust=False)).
    alpha = 2.0 / (EMA_SPAN + 1.0)
    ema = float(closes[0])
    for c in closes[1:]:
        ema = alpha * float(c) + (1.0 - alpha) * ema
    last = float(closes[-1])
    if last > ema:
        return "BULLISH"
    if last < ema:
        return "BEARISH"
    return "NEUTRAL"


def mtf_alignment(biases: dict[str, str], proposed_direction: str) -> str:
    """Combina los sesgos por timeframe en ALIGNED / COUNTER / MIXED."""
    target = "BULLISH" if proposed_direction.upper() == "LONG" else "BEARISH"
    aligned = sum(1 for b in biases.values() if b == target)
    counter = sum(1 for b in biases.values() if b != "NEUTRAL" and b != target)
    if counter >= 2:
        return "COUNTER"
    if aligned >= 2:
        return "ALIGNED"
    return "MIXED"
