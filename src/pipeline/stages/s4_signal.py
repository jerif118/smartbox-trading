"""
Stage 4: Signal — detecta breakout post-caja y filtra los marginales.
"""

from __future__ import annotations

from datetime import UTC, datetime

from domain.signals.breakout import detect_breakout
from infrastructure.config.settings import get_settings
from pipeline.contracts import SignalInput, SignalOutput
from utils.logger import get_logger

log = get_logger(__name__)


def stage_signal(input_data: SignalInput) -> SignalOutput:
    """Detecta breakout. Si no hay o no es operable, retorna has_breakout=False."""
    signal = detect_breakout(input_data.df_candles, input_data.box)
    if signal is None or not signal.is_directional:
        return SignalOutput(
            symbol=input_data.symbol,
            has_breakout=False,
            breakout_state=None,
            candle_close=None,
            signal_time=None,
            signal_age_minutes=None,
        )
    signal_dt = datetime.fromisoformat(signal.signal_time.replace("Z", "+00:00"))
    # La antigüedad se mide contra AHORA, no contra la última vela del
    # DataFrame: el orquestador ya recorta las velas a la ventana de breakout
    # (2h), así que medir contra la última vela acotaba la edad al tamaño de la
    # ventana y el gate max_age_minutes no podía dispararse nunca.
    reference_dt = (
        datetime.now(UTC)
        if input_data.now_ts is None
        else datetime.fromtimestamp(input_data.now_ts, tz=UTC)
    )
    age_minutes = max(0.0, (reference_dt - signal_dt).total_seconds() / 60)
    if age_minutes > input_data.max_age_minutes:
        return SignalOutput(
            symbol=input_data.symbol,
            has_breakout=False,
            breakout_state=None,
            candle_close=None,
            signal_time=signal.signal_time,
            signal_age_minutes=round(age_minutes, 2),
            penetration_pct=round(signal.penetration_pct, 2),
        )

    # ── Gate de calidad de ruptura ────────────────────────────────────────
    # Un cierre que apenas asoma fuera de la caja es ruido, no una rotura. En
    # 235 rupturas reales de US500+US100 las que penetraron menos del mínimo
    # acertaron ~46% frente al 67% de las que lo superaron. Se corta aquí,
    # antes de gastar red y tokens del crew.
    #
    # Desactivado con MIN_BREAKOUT_PENETRATION_PCT=0: una ruptura de la caja es
    # una ruptura, sin mínimo de penetración.
    min_penetration = get_settings().min_breakout_penetration_pct
    if signal.penetration_pct < min_penetration:
        log.info(
            "[signal] %s: ruptura %s descartada — penetró %.2f%% del rango "
            "(mínimo %.1f%%)",
            input_data.symbol,
            signal.state.value,
            signal.penetration_pct,
            min_penetration,
        )
        return SignalOutput(
            symbol=input_data.symbol,
            has_breakout=False,
            breakout_state=None,
            candle_close=signal.candle_close,
            signal_time=signal.signal_time,
            signal_age_minutes=round(age_minutes, 2),
            penetration_pct=round(signal.penetration_pct, 2),
        )

    return SignalOutput(
        symbol=input_data.symbol,
        has_breakout=True,
        breakout_state=signal.state.value,
        candle_close=signal.candle_close,
        signal_time=signal.signal_time,
        signal_age_minutes=round(age_minutes, 2),
        penetration_pct=round(signal.penetration_pct, 2),
    )
