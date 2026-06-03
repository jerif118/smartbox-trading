"""CRUD de equity snapshots."""

from __future__ import annotations

from datetime import UTC, datetime

from infrastructure.persistence.sqlite.db import get_db
from infrastructure.persistence.sqlite.models import EquitySnapshot


def _now() -> str:
    return datetime.now(UTC).isoformat()


def insert_snapshot(
    balance: float,
    equity: float,
    daily_pnl: float | None = None,
    total_pnl: float | None = None,
    open_positions: int = 0,
    source: str = "computed",
    run_id: str | None = None,
) -> int:
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO equity_snapshots (
                ts, balance, equity, daily_pnl, total_pnl,
                open_positions, source, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (_now(), balance, equity, daily_pnl, total_pnl, open_positions, source, run_id),
        )
    return int(cur.lastrowid)


def list_snapshots(limit: int = 500) -> list[EquitySnapshot]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM equity_snapshots ORDER BY ts ASC LIMIT ?", (limit,)
        ).fetchall()
    return [EquitySnapshot(**dict(r)) for r in rows]


def latest_snapshot() -> EquitySnapshot | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM equity_snapshots ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    return EquitySnapshot(**dict(row))
