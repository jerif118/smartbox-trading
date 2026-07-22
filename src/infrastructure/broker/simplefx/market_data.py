"""
Velas OHLC de SimpleFX (candles-core).

SimpleFX y Capital.com cotizan el mismo instrumento (US500/US100) con precios
distintos (hay un offset entre feeds). La caja para colocar órdenes debe
calcularse con los precios del broker donde se opera —SimpleFX—, mientras que
Capital.com sigue siendo el gráfico de referencia (ruptura, VP, RSI).

Endpoint (público, sin token): GET https://candles-core.simplefx.com/api/v3/candles
Portado del legacy `broker_api.api_requests.price_simple`.
"""

from __future__ import annotations

import pandas as pd
import requests

from utils.logger import get_logger
from utils.retry import retry

log = get_logger(__name__)

CANDLES_BASE = "https://candles-core.simplefx.com"

# Nombres alternativos que puede traer el payload → nombre canónico.
_COLUMN_ALIASES = {
    "t": "time",
    "timestamp": "time",
    "o": "open",
    "openprice": "open",
    "h": "high",
    "highprice": "high",
    "l": "low",
    "lowprice": "low",
    "c": "close",
    "closeprice": "close",
    "v": "volume",
}

_REQUIRED = ("time", "high", "low")


@retry(max_retries=3, backoff=2.0, exceptions=(requests.RequestException,))
def _fetch_candles(symbol: str, period_seconds: int, time_from: int, time_to: int) -> list:
    url = f"{CANDLES_BASE}/api/v3/candles"
    params: dict = {"symbol": symbol, "cPeriod": period_seconds}
    if time_from is not None:
        params["timeFrom"] = time_from
    if time_to is not None:
        params["timeTo"] = time_to
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data") if isinstance(payload, dict) else payload
    return data or []


def _normalize(data: list) -> pd.DataFrame:
    """Normaliza el payload a columnas canónicas (time en segundos)."""
    df = pd.DataFrame(data)
    if df.empty:
        return df
    df = df.rename(columns={c: _COLUMN_ALIASES.get(str(c).lower(), str(c).lower()) for c in df.columns})
    missing = [c for c in _REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(
            f"SimpleFX candles: faltan columnas {missing} en el payload "
            f"(columnas recibidas: {list(df.columns)})"
        )
    df["time"] = pd.to_numeric(df["time"], errors="coerce")
    df = df.dropna(subset=["time"])
    # Normaliza a segundos si el feed devuelve milisegundos.
    if not df.empty and df["time"].max() > 1e12:
        df["time"] = df["time"] // 1000
    df["time"] = df["time"].astype("int64")
    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("time").reset_index(drop=True)


class SimpleFXMarketData:
    """Feed de velas de SimpleFX para calcular la caja del broker de ejecución."""

    def get_candles(
        self,
        symbol: str,
        period_seconds: int,
        time_from: int,
        time_to: int,
    ) -> pd.DataFrame:
        """Devuelve OHLC de SimpleFX en [time_from, time_to] (unix segundos).

        DataFrame vacío si el feed no devuelve datos. Lanza si el payload no
        tiene la forma esperada (falla ruidosa, no caja silenciosamente mala).
        """
        data = _fetch_candles(symbol, period_seconds, time_from, time_to)
        df = _normalize(data)
        log.info("SimpleFX candles %s: %d velas [%s, %s]", symbol, len(df), time_from, time_to)
        return df
