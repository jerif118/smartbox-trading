"""
Funciones de lectura de la DB para la UI.

Capa delgada sobre los repos. Pensada para que Streamlit no importe
directamente los repos (separa concerns).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from infrastructure.persistence.sqlite import (
    equity_repo,
    event_repo,
    run_repo,
    trade_repo,
)


def get_dashboard_kpis() -> dict[str, Any]:
    """KPIs para el dashboard principal."""
    stats = trade_repo.compute_stats()
    latest_eq = equity_repo.latest_snapshot()
    open_trades = trade_repo.list_open_trades()
    return {
        "balance": latest_eq.balance if latest_eq else 0.0,
        "equity": latest_eq.equity if latest_eq else 0.0,
        "total_pnl": stats["total_pnl"],
        "win_rate_pct": stats["win_rate_pct"],
        "total_trades": stats["total_trades"],
        "open_trades": len(open_trades),
        "avg_r_multiple": stats["avg_r_multiple"],
        "wins": stats["wins"],
        "losses": stats["losses"],
    }


def get_equity_curve_df(days: int = 30) -> pd.DataFrame:
    """DataFrame de la curva de equity para graficar."""
    snaps = equity_repo.list_snapshots(limit=1000)
    if not snaps:
        return pd.DataFrame(columns=["ts", "balance", "equity"])
    rows = [
        {"ts": s.ts, "balance": s.balance, "equity": s.equity, "pnl": s.daily_pnl or 0}
        for s in snaps
    ]
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"])
    if days:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        df = df[df["ts"] >= cutoff]
    return df.sort_values("ts")


def get_trades_df(
    symbol: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> pd.DataFrame:
    """DataFrame de trades con filtros."""
    trades = trade_repo.list_trades(symbol=symbol, status=status, limit=limit)
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([t.to_dict() for t in trades])


def get_recent_runs(limit: int = 20) -> pd.DataFrame:
    """DataFrame de runs recientes."""
    runs = run_repo.list_runs(limit=limit)
    if not runs:
        return pd.DataFrame()
    return pd.DataFrame([r.__dict__ for r in runs])


def get_events_for_run(run_id: str) -> pd.DataFrame:
    """DataFrame de eventos de un run específico."""
    events = event_repo.list_events_by_run(run_id)
    if not events:
        return pd.DataFrame()
    return pd.DataFrame([e.__dict__ for e in events])
