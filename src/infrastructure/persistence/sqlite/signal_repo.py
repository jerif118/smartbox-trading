"""Memoria de rupturas ya analizadas (una decisión por ruptura).

`domain.signals.breakout.detect_breakout` devuelve SIEMPRE el primer cierre
fuera de la caja dentro de la ventana de 2h. Eso significa que la MISMA ruptura
se vuelve a detectar en cada vela de 5 min hasta que caduca por edad
(`SIGNAL_MAX_AGE_MINUTES`). Sin una marca persistente el orquestador vuelve a
correr el crew sobre un símbolo que ya tuvo su decisión —quemando tokens— y el
segundo símbolo, que rompió después, se queda sin turno.

La identidad de una señal es `(symbol, signal_time)`: la vela concreta que
rompió la caja. Es estable entre corridas y entre procesos, así que la marca
sobrevive a un reinicio del bot (a diferencia de un set en memoria).
"""

from __future__ import annotations

from datetime import UTC, datetime

from infrastructure.persistence.sqlite.db import get_db


def _now() -> str:
    return datetime.now(UTC).isoformat()


def is_analyzed(symbol: str, signal_time: str | None) -> bool:
    """¿Esta ruptura concreta ya tuvo su decisión?

    Sin `signal_time` no hay identidad de señal: se responde False para no
    bloquear nunca un análisis por falta de datos.
    """
    if not signal_time:
        return False
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM analyzed_signals WHERE symbol = ? AND signal_time = ? LIMIT 1",
            (symbol, signal_time),
        ).fetchone()
    return row is not None


def mark_analyzed(
    run_id: str,
    symbol: str,
    signal_time: str | None,
    breakout_state: str | None = None,
    outcome: str | None = None,
) -> bool:
    """Marca la ruptura como decidida. Retorna True si se insertó ahora.

    Idempotente: si ya estaba marcada (re-run, carrera entre hilos) no falla ni
    duplica; conserva la marca original, que es la de la decisión real.
    """
    if not signal_time:
        return False
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO analyzed_signals
                (symbol, signal_time, breakout_state, outcome, run_id, ts)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (symbol, signal_time, breakout_state, outcome, run_id, _now()),
        )
    return cur.rowcount > 0


def get_outcome(symbol: str, signal_time: str) -> str | None:
    """Qué se decidió para esa ruptura (para logs y post-mortem)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT outcome FROM analyzed_signals WHERE symbol = ? AND signal_time = ? LIMIT 1",
            (symbol, signal_time),
        ).fetchone()
    return None if row is None else row[0]


def analyzed_today(symbol: str) -> bool:
    """¿Ese símbolo ya tuvo una ruptura decidida hoy?

    Responde "la caja del símbolo se rompió hoy y ya se resolvió", que es lo
    que necesita la política de confirmación del primario: sin esto, saltarse
    el re-análisis del primario haría parecer que nunca rompió y el secundario
    entraría a medio tamaño (o quedaría bloqueado en modo ``required``).
    """
    today = datetime.now(UTC).date().isoformat()
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM analyzed_signals "
            "WHERE symbol = ? AND substr(signal_time, 1, 10) = ? LIMIT 1",
            (symbol, today),
        ).fetchone()
    return row is not None


def list_analyzed_today() -> list[dict]:
    """Rupturas decididas hoy (UTC). Observabilidad del dashboard/diagnose."""
    today = datetime.now(UTC).date().isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM analyzed_signals WHERE substr(signal_time, 1, 10) = ? "
            "ORDER BY signal_time ASC",
            (today,),
        ).fetchall()
    return [dict(r) for r in rows]
