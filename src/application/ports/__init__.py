"""
Ports de la capa de aplicación. Interfaces que la infraestructura implementa.

Usan `Protocol` para dependency inversion: la aplicación define QUÉ necesita,
la infraestructura decide CÓMO lo provee.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

import pandas as pd


# ── Market data ────────────────────────────────────────────────────────
@runtime_checkable
class MarketDataProvider(Protocol):
    """Proveedor de datos OHLCV (vela source-of-truth para análisis)."""

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        from_ts: int,
        to_ts: int,
        max_candles: int = 500,
    ) -> pd.DataFrame:
        """Retorna DataFrame con columnas: time, open, high, low, close, volume."""
        ...


# ── Broker ─────────────────────────────────────────────────────────────
@runtime_checkable
class BrokerGateway(Protocol):
    """Interface unificada con el broker (SimpleFX por ahora)."""

    def login(self) -> str:
        """Retorna token de sesión."""
        ...

    def place_order(
        self,
        symbol: str,
        side: str,
        volume: float,
        entry_price: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> str:
        """Envía orden pendiente. Retorna broker_order_id."""
        ...

    def modify_order(
        self,
        broker_order_id: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> None:
        """Modifica SL/TP de una orden existente."""
        ...


# ── Calendar (macro events) ────────────────────────────────────────────
@runtime_checkable
class MacroCalendarProvider(Protocol):
    """Proveedor de eventos macro de alto impacto."""

    def get_high_impact_events(self, date_str: str) -> list[dict]:
        """Retorna lista de eventos {'time', 'event', 'currency', 'impact'}."""
        ...


# ── News ───────────────────────────────────────────────────────────────
@runtime_checkable
class NewsProvider(Protocol):
    """Proveedor de noticias financieras."""

    def search(self, query: str, days_back: int = 1) -> list[dict]:
        """Retorna lista de {'title', 'source', 'url', 'published'}."""
        ...


# ── Repositorios (side-effect free data access) ───────────────────────
@runtime_checkable
class TradeRepository(Protocol):
    def insert(self, **kwargs) -> int: ...
    def get(self, trade_id: int) -> dict | None: ...
    def list_open(self) -> list[dict]: ...
    def list_pending(self) -> list[dict]: ...
    def update_status(self, trade_id: int, status: str) -> None: ...
    def close(self, trade_id: int, status: str, exit_price: float, pnl: float, **kwargs) -> None: ...
    def modify(self, trade_id: int, **kwargs) -> None: ...


@runtime_checkable
class DecisionRepository(Protocol):
    def insert(self, **kwargs) -> int: ...
    def get(self, decision_id: int) -> dict | None: ...


@runtime_checkable
class EventRepository(Protocol):
    def log(self, run_id: str, agent: str, event_type: str, payload: dict, **kwargs) -> int: ...
    def list_by_run(self, run_id: str) -> list[dict]: ...


@runtime_checkable
class EquityRepository(Protocol):
    def insert_snapshot(self, **kwargs) -> int: ...
    def latest(self) -> dict | None: ...
    def list_all(self, limit: int = 500) -> list[dict]: ...


@runtime_checkable
class RunRepository(Protocol):
    def start(self, run_id: str, config: dict | None = None) -> None: ...
    def finish(self, run_id: str, status: str, error: str | None = None) -> None: ...
    def get(self, run_id: str) -> dict | None: ...
    def list_recent(self, limit: int = 50) -> list[dict]: ...
