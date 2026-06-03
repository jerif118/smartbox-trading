"""
Stage 4: Signal — detecta breakout post-caja.
"""

from __future__ import annotations

from domain.signals.breakout import detect_breakout
from pipeline.contracts import SignalInput, SignalOutput


def stage_signal(input_data: SignalInput) -> SignalOutput:
    """Detecta breakout. Si no, retorna has_breakout=False."""
    signal = detect_breakout(input_data.df_candles, input_data.box)
    if signal is None or signal.state.value in ("INSIDE", "NONE"):
        return SignalOutput(
            symbol=input_data.symbol,
            has_breakout=False,
            breakout_state=None,
            candle_close=None,
            signal_time=None,
        )
    return SignalOutput(
        symbol=input_data.symbol,
        has_breakout=True,
        breakout_state=signal.state.value,
        candle_close=signal.candle_close,
        signal_time=signal.signal_time,
    )
