"""
Stage 4: Signal — detecta breakout post-caja.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
            signal_age_minutes=None,
        )
    signal_dt = datetime.fromisoformat(signal.signal_time.replace("Z", "+00:00"))
    latest_ts = int(input_data.df_candles["time"].max())
    latest_dt = datetime.fromtimestamp(latest_ts, tz=UTC)
    age_minutes = max(0.0, (latest_dt - signal_dt).total_seconds() / 60)
    if age_minutes > input_data.max_age_minutes:
        return SignalOutput(
            symbol=input_data.symbol,
            has_breakout=False,
            breakout_state=None,
            candle_close=None,
            signal_time=signal.signal_time,
            signal_age_minutes=round(age_minutes, 2),
        )
    return SignalOutput(
        symbol=input_data.symbol,
        has_breakout=True,
        breakout_state=signal.state.value,
        candle_close=signal.candle_close,
        signal_time=signal.signal_time,
        signal_age_minutes=round(age_minutes, 2),
    )
