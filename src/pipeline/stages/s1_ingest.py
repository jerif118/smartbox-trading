"""
Stage 1: Ingest — descarga OHLCV desde Capital.com (con caché parquet).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from infrastructure.broker.capital.adapter import TIMEFRAME_SECONDS, CapitalAdapter
from infrastructure.config.settings import get_settings
from pipeline.contracts import IngestInput, IngestOutput


def stage_ingest(input_data: IngestInput) -> IngestOutput:
    """Descarga velas con caché parquet incremental.

    El caché solo se considera "fresco" si su última vela llega (casi) hasta
    el final del rango pedido; si no, se descarga únicamente la cola que falta.
    Así el bot nunca opera con datos desactualizados.
    """
    settings = get_settings()
    Path(settings.data_loader_path).mkdir(parents=True, exist_ok=True)
    cache_file = Path(settings.data_loader_path) / f"{input_data.symbol}.parquet"

    start_ts = int(pd.Timestamp(input_data.start_iso, tz="UTC").timestamp())
    end_ts = int(pd.Timestamp(input_data.end_iso, tz="UTC").timestamp())
    tf_seconds = TIMEFRAME_SECONDS.get(input_data.timeframe, 300)

    cached: pd.DataFrame | None = None
    if cache_file.exists():
        cached = pd.read_parquet(cache_file)

    fetch_from: int | None = start_ts
    if cached is not None and not cached.empty:
        last_cached = int(cached["time"].max())
        if last_cached >= end_ts - 2 * tf_seconds:
            fetch_from = None  # caché fresco: cubre el final del rango
        elif last_cached >= start_ts:
            fetch_from = last_cached + tf_seconds  # solo la cola que falta

    if fetch_from is not None:
        adapter = CapitalAdapter()
        df_new = adapter.get_candles(
            input_data.symbol, input_data.timeframe, fetch_from, end_ts, max_candles=500
        )
        if not df_new.empty:
            if cached is not None and not cached.empty:
                cached = (
                    pd.concat([cached, df_new])
                    .drop_duplicates(subset=["time"])
                    .sort_values("time")
                )
            else:
                cached = df_new
            cached.to_parquet(cache_file, engine="pyarrow", index=False)

    if cached is None or cached.empty:
        return IngestOutput(symbol=input_data.symbol, df_candles=pd.DataFrame(), n_candles=0)

    in_range = (
        cached[(cached["time"] >= start_ts) & (cached["time"] <= end_ts)]
        .sort_values("time")
        .reset_index(drop=True)
    )
    return IngestOutput(
        symbol=input_data.symbol,
        df_candles=in_range,
        n_candles=len(in_range),
    )
