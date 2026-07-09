"""
Adaptador Capital.com: provee `MarketDataProvider` (precios OHLCV)
y autenticación para descargar velas. NO ejecuta órdenes.
"""

from __future__ import annotations

import threading
import time
from typing import Any, ClassVar

import pandas as pd
import requests

from infrastructure.config.settings import get_settings
from utils.logger import get_logger
from utils.retry import retry

log = get_logger(__name__)

CAPITAL_URL = "https://api-capital.backend-capital.com"

TIMEFRAME_SECONDS = {
    "MINUTE": 60,
    "MINUTE_5": 300,
    "MINUTE_15": 900,
    "MINUTE_30": 1800,
    "HOUR": 3600,
    "HOUR_4": 14400,
    "DAY": 86400,
    "WEEK": 604800,
}

# Capital.com usa epics propios; estos alias evitan 404 error.not-found.epic
EPIC_ALIASES = {
    "GER40": "DE40",
    "DAX40": "DE40",
}


@retry(max_retries=3, backoff=2.0, exceptions=(requests.RequestException,))
def _login(email: str, password: str, api_key: str) -> dict[str, str]:
    body = {"identifier": email, "password": password}
    headers = {"X-CAP-API-KEY": api_key, "Content-Type": "application/json"}
    resp = requests.post(f"{CAPITAL_URL}/api/v1/session", json=body, headers=headers, timeout=20)
    resp.raise_for_status()
    cst = resp.headers.get("CST")
    xst = resp.headers.get("X-SECURITY-TOKEN")
    if not cst or not xst:
        raise RuntimeError("Capital.com: respuesta sin tokens")
    return {"CST": cst, "X-SECURITY-TOKEN": xst}


@retry(max_retries=5, backoff=1.5, initial_delay=0.5, exceptions=(requests.RequestException,))
def _fetch_prices(
    symbol: str,
    resolution: str,
    from_iso: str,
    to_iso: str,
    max_n: str,
    cst: str,
    xst: str,
) -> list[dict[str, Any]]:
    url = f"{CAPITAL_URL}/api/v1/prices/{symbol}"
    headers = {"X-SECURITY-TOKEN": xst, "CST": cst}
    params = {"resolution": resolution, "max": max_n, "from": from_iso, "to": to_iso}
    resp = requests.get(url, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json().get("prices", [])


def _standardize(prices: list[dict]) -> pd.DataFrame:
    """Convierte formato Capital.com ({bid/ask}) a columnas planas."""
    if not prices:
        return pd.DataFrame()
    rows = []
    for p in prices:
        op = p.get("openPrice", {})
        cl = p.get("closePrice", {})
        hi = p.get("highPrice", {})
        lo = p.get("lowPrice", {})
        rows.append(
            {
                "time": int(
                    pd.Timestamp(p.get("snapshotTimeUTC", "")).timestamp()
                ) if p.get("snapshotTimeUTC") else None,
                "open": (op.get("bid", 0) + op.get("ask", 0)) / 2,
                "high": (hi.get("bid", 0) + hi.get("ask", 0)) / 2,
                "low": (lo.get("bid", 0) + lo.get("ask", 0)) / 2,
                "close": (cl.get("bid", 0) + cl.get("ask", 0)) / 2,
                "volume": float(p.get("lastTradedVolume", 0)),
            }
        )
    df = pd.DataFrame(rows)
    if "time" in df.columns:
        df = df.dropna(subset=["time"])
        df["time"] = df["time"].astype("int64")
        df = df.sort_values("time").reset_index(drop=True)
    return df


class CapitalAdapter:
    """Proveedor de datos OHLCV de Capital.com.

    El caché de tokens es de CLASE (compartido entre instancias, con lock):
    el pipeline y las tools de los agentes crean adapters nuevos por llamada
    y Capital.com limita la creación de sesiones (~1/s) — sin caché compartido
    cada corrida dispara varios logins concurrentes → error.too-many.requests.
    """

    _shared_tokens: ClassVar[dict[str, str]] = {}
    _shared_tokens_ts: ClassVar[float] = 0.0
    _tokens_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, settings=None):
        self._settings = settings or get_settings()

    @classmethod
    def reset_token_cache(cls) -> None:
        """Invalida el caché compartido (tests / re-login forzado)."""
        with cls._tokens_lock:
            cls._shared_tokens = {}
            cls._shared_tokens_ts = 0.0

    def _get_tokens(self) -> dict[str, str]:
        cls = CapitalAdapter
        with cls._tokens_lock:
            if cls._shared_tokens and (time.time() - cls._shared_tokens_ts) < 1500:
                return cls._shared_tokens
            if not self._settings.email or not self._settings.password or not self._settings.api_key:
                raise RuntimeError("Capital.com credentials no configuradas")
            cls._shared_tokens = _login(
                self._settings.email, self._settings.password, self._settings.api_key
            )
            cls._shared_tokens_ts = time.time()
            return cls._shared_tokens

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        from_ts: int,
        to_ts: int,
        max_candles: int = 500,
    ) -> pd.DataFrame:
        tokens = self._get_tokens()
        epic = EPIC_ALIASES.get(symbol, symbol)

        # La API rechaza ventanas de calendario > max*resolution
        # (error.invalid.max.daterange), así que descargamos por chunks.
        tf_seconds = TIMEFRAME_SECONDS.get(timeframe, 300)
        chunk_span = int(max_candles * tf_seconds * 0.9)
        frames: list[pd.DataFrame] = []
        chunk_from = from_ts
        while chunk_from < to_ts:
            chunk_to = min(chunk_from + chunk_span, to_ts)
            from_iso = pd.Timestamp(chunk_from, unit="s", tz="UTC").strftime("%Y-%m-%dT%H:%M:%S")
            to_iso = pd.Timestamp(chunk_to, unit="s", tz="UTC").strftime("%Y-%m-%dT%H:%M:%S")
            prices = _fetch_prices(
                symbol=epic,
                resolution=timeframe,
                from_iso=from_iso,
                to_iso=to_iso,
                max_n=str(max_candles),
                cst=tokens["CST"],
                xst=tokens["X-SECURITY-TOKEN"],
            )
            df = _standardize(prices)
            if not df.empty:
                frames.append(df)
            chunk_from = chunk_to

        if not frames:
            return pd.DataFrame()
        return (
            pd.concat(frames)
            .drop_duplicates(subset=["time"])
            .sort_values("time")
            .reset_index(drop=True)
        )

    def get_current_price(self, symbol: str) -> float | None:
        """Precio actual (última vela de 1min)."""
        now = int(time.time())
        df = self.get_candles(symbol, "MINUTE", now - 120, now, max_candles=5)
        if df.empty:
            return None
        return float(df["close"].iloc[-1])
