"""CRUD de trades. Source of truth para el Position Manager."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from infrastructure.persistence.sqlite.db import get_db
from infrastructure.persistence.sqlite.models import Trade, TradeStatus


def _now() -> str:
    return datetime.now(UTC).isoformat()


def insert_trade(
    run_id: str,
    symbol: str,
    side: str,
    volume: float,
    entry_price: float,
    stop_loss: float | None,
    take_profit: float | None,
    is_runner: bool,
    decision_id: int | None = None,
    broker_order_id: str | None = None,
    status: str = TradeStatus.PENDING.value,
) -> int:
    """Inserta un trade. Retorna el id."""
    ts = _now()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO trades (
                run_id, decision_id, ts_open, symbol, side, volume,
                entry_price, stop_loss, take_profit, is_runner,
                broker_order_id, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, decision_id, ts, symbol, side, volume,
                entry_price, stop_loss, take_profit, 1 if is_runner else 0,
                broker_order_id, status,
            ),
        )
    return int(cur.lastrowid)


def update_status(
    trade_id: int,
    status: str,
    broker_order_id: str | None = None,
) -> None:
    fields = ["status = ?", "updated_at = ?"]
    values: list[Any] = [status, _now()]
    if broker_order_id is not None:
        fields.append("broker_order_id = ?")
        values.append(broker_order_id)
    values.append(trade_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE trades SET {', '.join(fields)} WHERE id = ?",
            values,
        )


def close_trade(
    trade_id: int,
    status: str,
    exit_price: float,
    pnl: float,
    r_multiple: float | None = None,
    reason: str | None = None,
) -> None:
    """Marca un trade como cerrado con P&L."""
    with get_db() as conn:
        conn.execute(
            """
            UPDATE trades SET
                status = ?, ts_close = ?, exit_price = ?, pnl = ?,
                r_multiple = ?, reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, _now(), exit_price, pnl, r_multiple, reason, _now(), trade_id),
        )


def modify_sl_tp(
    trade_id: int,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> None:
    """Modifica SL/TP de un trade abierto (usado por Position Manager)."""
    fields = ["updated_at = ?"]
    values: list[Any] = [_now()]
    if stop_loss is not None:
        fields.append("stop_loss = ?")
        values.append(stop_loss)
    if take_profit is not None:
        fields.append("take_profit = ?")
        values.append(take_profit)
    values.append(trade_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE trades SET {', '.join(fields)} WHERE id = ?",
            values,
        )


def get_trade(trade_id: int) -> Trade | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["is_runner"] = bool(d.get("is_runner", 0))
    return Trade(**d)


def list_trades(
    symbol: str | None = None,
    status: str | None = None,
    limit: int = 100,
    order: str = "ts_open DESC",
) -> list[Trade]:
    sql = "SELECT * FROM trades WHERE 1=1"
    args: list[Any] = []
    if symbol:
        sql += " AND symbol = ?"
        args.append(symbol)
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += f" ORDER BY {order} LIMIT ?"
    args.append(limit)
    with get_db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [Trade(**{**dict(r), "is_runner": bool(r["is_runner"])}) for r in rows]


def list_open_trades() -> list[Trade]:
    return list_trades(status=TradeStatus.OPEN.value, limit=500)


def list_pending_trades() -> list[Trade]:
    return list_trades(status=TradeStatus.PENDING.value, limit=500)


def realized_pnl_today() -> float:
    """P&L realizado del día (UTC). Para el freno duro de MAX_DAILY_LOSS."""
    today = datetime.now(UTC).date().isoformat()
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades "
            "WHERE pnl IS NOT NULL AND DATE(ts_close) = ?",
            (today,),
        ).fetchone()
    return float(row[0] or 0.0)


def compute_stats() -> dict[str, Any]:
    """Métricas agregadas para el dashboard."""
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        closed = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status IN ('CLOSED_TP', 'CLOSED_SL', 'CLOSED_MANUAL')"
        ).fetchone()[0]
        wins = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status = 'CLOSED_TP'"
        ).fetchone()[0]
        losses = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status = 'CLOSED_SL'"
        ).fetchone()[0]
        total_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE pnl IS NOT NULL"
        ).fetchone()[0]
        avg_r = conn.execute(
            "SELECT COALESCE(AVG(r_multiple), 0) FROM trades WHERE r_multiple IS NOT NULL"
        ).fetchone()[0]

    win_rate = (wins / closed * 100) if closed else 0
    return {
        "total_trades": total,
        "closed_trades": closed,
        "open_trades": total - closed,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate, 2),
        "total_pnl": round(total_pnl, 2),
        "avg_r_multiple": round(avg_r, 3),
    }
