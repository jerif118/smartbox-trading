"""CRUD de runs (corridas del bot)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from infrastructure.persistence.sqlite.db import get_db
from infrastructure.persistence.sqlite.models import Run, RunStatus


def _now() -> str:
    return datetime.now(UTC).isoformat()


def start_run(run_id: str, config_snapshot: dict | None = None) -> Run:
    """Crea un run nuevo con status='running'."""
    run = Run(
        id=run_id,
        started_at=_now(),
        status=RunStatus.RUNNING.value,
        config_snapshot=json.dumps(config_snapshot) if config_snapshot else None,
    )
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO runs (id, started_at, status, config_snapshot)
            VALUES (?, ?, ?, ?)
            """,
            (run.id, run.started_at, run.status, run.config_snapshot),
        )
    return run


def finish_run(run_id: str, status: str, error: str | None = None) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, error = ? WHERE id = ?",
            (_now(), status, error, run_id),
        )


def fail_stale_runs() -> int:
    """Cierra runs zombi (status='running' de corridas que crashearon).

    Se invoca al inicio de cada run: cualquier run todavía 'running' en ese
    momento es de un proceso anterior que murió sin cerrar su registro.
    Retorna cuántos runs se repararon.
    """
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, error = ? WHERE status = ?",
            (_now(), RunStatus.FAILED.value, "stale: proceso anterior no cerró el run",
             RunStatus.RUNNING.value),
        )
    return cur.rowcount


def get_run(run_id: str) -> Run | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    return Run(**dict(row))


def list_runs(limit: int = 50) -> list[Run]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [Run(**dict(r)) for r in rows]
