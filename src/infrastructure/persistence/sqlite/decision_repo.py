"""CRUD de decisiones del crew."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from infrastructure.persistence.sqlite.db import get_db
from infrastructure.persistence.sqlite.models import Decision


def _now() -> str:
    return datetime.now(UTC).isoformat()


def insert_decision(
    run_id: str,
    symbol: str,
    action: str,
    risk: str | None,
    confidence: int | None,
    reasons: list[str],
    team_consensus: str | None = None,
    crew_raw_output: dict | None = None,
    key_levels: dict | None = None,
    signal: dict | None = None,
    execution_status: str = "EXECUTED",
) -> int:
    """Inserta una decisión. Retorna el id.

    ``execution_status`` distingue lo que se operó de lo que se frenó
    (NO_OPERAR, REJECTED, SKIPPED_DUPLICATE). Se registra TODO: las decisiones
    descartadas son la mitad de la muestra necesaria para saber si el filtro
    está bien calibrado.
    """
    ts = _now()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO decisions (
                run_id, ts, symbol, action, risk, confidence,
                reasons, team_consensus, crew_raw_output, key_levels, signal,
                execution_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, ts, symbol, action, risk, confidence,
                json.dumps(reasons), team_consensus,
                json.dumps(crew_raw_output) if crew_raw_output else None,
                json.dumps(key_levels) if key_levels else None,
                json.dumps(signal) if signal else None,
                execution_status,
            ),
        )
    return int(cur.lastrowid)


def get_decision(decision_id: int) -> Decision | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    if row is None:
        return None
    return Decision(**dict(row))


def list_decisions_by_run(run_id: str) -> list[Decision]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM decisions WHERE run_id = ? ORDER BY ts ASC", (run_id,)
        ).fetchall()
    return [Decision(**dict(r)) for r in rows]
