"""CRUD de eventos de agentes (timeline de interacciones)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from infrastructure.persistence.sqlite.db import get_db
from infrastructure.persistence.sqlite.models import AgentEvent


def _now() -> str:
    return datetime.now(UTC).isoformat()


def log_event(
    run_id: str,
    agent: str,
    event_type: str,
    payload: dict | str | None = None,
    duration_ms: int | None = None,
) -> int:
    """Registra un evento de agente. payload puede ser dict o str."""
    if isinstance(payload, dict):
        payload = json.dumps(payload, ensure_ascii=False, default=str)
    elif payload is None:
        payload = None
    else:
        payload = str(payload)

    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO agent_events (run_id, ts, agent, event_type, payload, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, _now(), agent, event_type, payload, duration_ms),
        )
    return int(cur.lastrowid)


def list_events_by_run(run_id: str) -> list[AgentEvent]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_events WHERE run_id = ? ORDER BY ts ASC, id ASC",
            (run_id,),
        ).fetchall()
    return [AgentEvent(**dict(r)) for r in rows]


def list_recent_events(limit: int = 100) -> list[AgentEvent]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_events ORDER BY ts DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [AgentEvent(**dict(r)) for r in rows]
