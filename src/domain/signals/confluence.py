"""
Confluence score determinista (reglas duras).

Lógica pura, sin I/O ni LLM. Es la fuente de verdad del score de confluencia:
tanto la tool que ve el LLM (ConfluenceScoreTool) como el pipeline
(s5_analyze) la usan, de modo que el número que decide NO puede inflarse por
una alucinación del LLM al pasar booleanos incorrectos.
"""

from __future__ import annotations

# Umbrales de RSI para clasificar la dirección (mismos que usaba la tool).
RSI_OVERSOLD = 35.0
RSI_OVERBOUGHT = 65.0
DEFAULT_PROCEED_THRESHOLD = 55


def rsi_direction_from_value(rsi_value: float | None) -> str:
    """Clasifica el RSI en oversold/overbought/neutral de forma determinista."""
    if rsi_value is None:
        return "neutral"
    if rsi_value <= RSI_OVERSOLD:
        return "oversold"
    if rsi_value >= RSI_OVERBOUGHT:
        return "overbought"
    return "neutral"


def compute_confluence_score(
    *,
    direction: str,
    rsi_value: float | None,
    poc_above_mid: bool,
    breakout_aligned: bool,
    mtf_aligned: bool,
    rsi_direction: str | None = None,
    proceed_threshold: int = DEFAULT_PROCEED_THRESHOLD,
) -> dict:
    """Calcula el score 0-100 de confluencia técnica por reglas duras.

    Mantiene exactamente la lógica de puntuación previa de ConfluenceScoreTool.
    ``rsi_direction`` se deriva de ``rsi_value`` si no se pasa explícitamente.
    """
    direction = direction.upper()
    if rsi_direction is None:
        rsi_direction = rsi_direction_from_value(rsi_value)
    rsi = rsi_value if rsi_value is not None else 50.0

    score = 0
    factors: list[str] = []

    if rsi_direction == "neutral":
        score += 20
        factors.append("RSI neutral (20)")
    elif direction == "LONG" and rsi_direction == "oversold" and rsi < RSI_OVERSOLD:
        score += 25
        factors.append("RSI oversold favorable a LONG (25)")
    elif direction == "SHORT" and rsi_direction == "overbought" and rsi > RSI_OVERBOUGHT:
        score += 25
        factors.append("RSI overbought favorable a SHORT (25)")
    elif rsi_direction in ("oversold", "overbought"):
        score += 5
        factors.append("RSI extremo contrario a la dirección (5)")

    poc_aligned = poc_above_mid if direction == "LONG" else not poc_above_mid
    if poc_aligned:
        score += 25
        factors.append(f"POC alineado con {direction} (25)")
    else:
        score += 10
        factors.append(f"POC contrario a {direction} (10)")

    if breakout_aligned:
        score += 30
        factors.append("Breakout en dirección (30)")

    if mtf_aligned:
        score += 25
        factors.append("MTF alineado (25)")

    score = min(score, 100)
    recommendation = "PROCEED" if score >= proceed_threshold else "NO_OPERAR"
    return {"score": score, "recommendation": recommendation, "factors": factors}
