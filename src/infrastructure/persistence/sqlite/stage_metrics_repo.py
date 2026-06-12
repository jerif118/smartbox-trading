"""CRUD de stage_metrics (duración y resultado de cada stage por run)."""

from __future__ import annotations

from typing import Any

from infrastructure.persistence.sqlite.db import get_db


def insert_stage_metric(
    run_id: str,
    stage: str,
    status: str,
    started_at: str,
    duration_ms: int,
    error_type: str | None = None,
    error_msg: str | None = None,
) -> int:
    """Inserta una métrica de stage. status: ok | error | timeout."""
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO stage_metrics (
                run_id, stage, status, started_at, duration_ms, error_type, error_msg
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, stage, status, started_at, duration_ms, error_type, error_msg),
        )
    return int(cur.lastrowid)


def list_stage_metrics(run_id: str) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM stage_metrics WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
    return [dict(r) for r in rows]
