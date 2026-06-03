"""
Stage 1: Ingest — descarga OHLCV desde Capital.com (con caché parquet).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from infrastructure.broker.capital.adapter import CapitalAdapter
from infrastructure.config.settings import get_settings
from pipeline.contracts import IngestInput, IngestOutput


def stage_ingest(input_data: IngestInput) -> IngestOutput:
    """Descarga velas. Usa caché parquet si está disponible."""
    settings = get_settings()
    Path(settings.data_loader_path).mkdir(parents=True, exist_ok=True)
    cache_file = Path(settings.data_loader_path) / f"{input_data.symbol}.parquet"

    # Try cache
    if cache_file.exists() and input_data.start_iso:
        df = pd.read_parquet(cache_file)
        start_ts = int(pd.Timestamp(input_data.start_iso, tz="UTC").timestamp())
        end_ts = int(pd.Timestamp(input_data.end_iso, tz="UTC").timestamp())
        in_range = df[(df["time"] >= start_ts) & (df["time"] <= end_ts)]
        if not in_range.empty:
            return IngestOutput(
                symbol=input_data.symbol,
                df_candles=in_range.sort_values("time").reset_index(drop=True),
                n_candles=len(in_range),
            )

    # Fetch from API
    adapter = CapitalAdapter()
    start_ts = int(pd.Timestamp(input_data.start_iso, tz="UTC").timestamp())
    end_ts = int(pd.Timestamp(input_data.end_iso, tz="UTC").timestamp())
    df = adapter.get_candles(
        input_data.symbol, input_data.timeframe, start_ts, end_ts, max_candles=500
    )
    if df.empty:
        return IngestOutput(symbol=input_data.symbol, df_candles=df, n_candles=0)

    # Update cache
    if cache_file.exists():
        old = pd.read_parquet(cache_file)
        df = pd.concat([old, df]).drop_duplicates(subset=["time"]).sort_values("time")
    df.to_parquet(cache_file, engine="pyarrow", index=False)

    return IngestOutput(
        symbol=input_data.symbol,
        df_candles=df.reset_index(drop=True),
        n_candles=len(df),
    )
