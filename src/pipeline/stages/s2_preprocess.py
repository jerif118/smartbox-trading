"""
Stage 2: Preprocess — calcula Box, RSI, Volume Profile.
"""

from __future__ import annotations

from domain.indicators.rsi import last_rsi
from domain.indicators.volume_profile import compute_volume_profile
from domain.strategy.box import compute_box_from_df
from pipeline.contracts import PreprocessInput, PreprocessOutput
from tools_bot.time_now import box_window_unix


def stage_preprocess(input_data: PreprocessInput, df_candles) -> PreprocessOutput:
    """Calcula features para un símbolo."""
    box_from, box_to = box_window_unix(
        input_data.box_date,
        input_data.box_start,
        input_data.box_end,
        input_data.market_tz,
    )

    # Box calculation
    box = compute_box_from_df(df_candles, box_from, box_to)
    if box is None:
        raise ValueError(f"{input_data.symbol}: sin velas en ventana de caja")

    # RSI
    rsi_val = last_rsi(df_candles["close"]) if "close" in df_candles.columns else None

    # Volume Profile (si hay columna volume)
    vp = None
    if "volume" in df_candles.columns and df_candles["volume"].sum() > 0:
        vp_obj = compute_volume_profile(df_candles)
        if vp_obj:
            vp = {
                "poc": vp_obj.poc,
                "vah": vp_obj.vah,
                "val": vp_obj.val,
                "total_volume": vp_obj.total_volume,
                "peaks": vp_obj.peaks,
            }

    # Box candles (últimas 10)
    box_candles = []
    if not df_candles.empty:
        in_box = df_candles[(df_candles["time"] >= box_from) & (df_candles["time"] <= box_to)]
        last_10 = in_box.tail(10)
        for _, row in last_10.iterrows():
            box_candles.append(
                {
                    "time": int(row["time"]),
                    "open": round(float(row["open"]), 2),
                    "high": round(float(row["high"]), 2),
                    "low": round(float(row["low"]), 2),
                    "close": round(float(row["close"]), 2),
                    "volume": float(row.get("volume", 0)),
                }
            )

    return PreprocessOutput(
        symbol=input_data.symbol,
        box=box,
        rsi_last=rsi_val,
        volume_profile=vp,
        box_candles=box_candles,
    )
