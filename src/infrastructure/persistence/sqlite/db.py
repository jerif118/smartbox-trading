"""
SQLite database connection + migraciones.

Uso:
    from infrastructure.persistence.sqlite.db import get_db, init_db

    init_db()                              # crea tablas si no existen
    with get_db() as conn:
        conn.execute(...)
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from infrastructure.config.settings import get_settings

_LOCK = threading.Lock()
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_INITIALIZED: set[str] = set()  # paths ya inicializados

# Versión actual del schema. schema.sql refleja SIEMPRE el estado final;
# las DBs existentes se llevan a ese estado aplicando _MIGRATIONS en orden.
LATEST_SCHEMA_VERSION = 3

_MIGRATIONS: dict[int, str] = {
    1: """
    ALTER TABLE trades ADD COLUMN client_order_id TEXT;

    CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_client_order_id_active
      ON trades(client_order_id)
      WHERE client_order_id IS NOT NULL AND status IN ('PENDING', 'OPEN');

    CREATE TABLE IF NOT EXISTS stage_metrics (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      run_id TEXT NOT NULL,
      stage TEXT NOT NULL,
      status TEXT NOT NULL,
      started_at TEXT NOT NULL,
      duration_ms INTEGER NOT NULL,
      error_type TEXT,
      error_msg TEXT,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY (run_id) REFERENCES runs(id)
    );

    CREATE INDEX IF NOT EXISTS idx_stage_metrics_run_id ON stage_metrics(run_id);
    CREATE INDEX IF NOT EXISTS idx_stage_metrics_stage ON stage_metrics(stage);
    """,
    2: """
    ALTER TABLE trades ADD COLUMN initial_stop_loss REAL;
    UPDATE trades SET initial_stop_loss = stop_loss WHERE initial_stop_loss IS NULL;
    """,
    3: """
    ALTER TABLE trades ADD COLUMN max_favorable_price REAL;
    """,
}


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_connection() -> sqlite3.Connection:
    """Obtiene una conexión. Usar dentro de un context manager."""
    settings = get_settings()
    return _connect(settings.db_path)


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    """Context manager que commit al salir, rollback en excepción."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str | None = None) -> None:
    """Crea las tablas si no existen y aplica migraciones pendientes. Idempotente."""
    path = db_path or get_settings().db_path

    if path in _INITIALIZED:
        return

    with _LOCK:
        if path in _INITIALIZED:
            return

        conn = _connect(path)
        try:
            is_fresh = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
                ).fetchone()
                is None
            )
            if is_fresh:
                schema = _SCHEMA_PATH.read_text(encoding="utf-8")
                conn.executescript(schema)
                conn.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION}")
                conn.commit()
            else:
                _migrate(conn)
            _INITIALIZED.add(path)
        finally:
            conn.close()


def get_schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _migrate(conn: sqlite3.Connection) -> None:
    """Lleva una DB existente a LATEST_SCHEMA_VERSION aplicando migraciones en orden."""
    current = get_schema_version(conn)
    for version in range(current + 1, LATEST_SCHEMA_VERSION + 1):
        conn.executescript(_MIGRATIONS[version])
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()


def reset_db(db_path: str | None = None) -> None:
    """Borra TODAS las tablas. Solo para tests."""
    path = db_path or get_settings().db_path
    if Path(path).exists():
        Path(path).unlink()
    _INITIALIZED.discard(path)


def db_exists(db_path: str | None = None) -> bool:
    path = db_path or get_settings().db_path
    return Path(path).exists()
