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
    """Crea las tablas si no existen. Idempotente."""
    path = db_path or get_settings().db_path

    if path in _INITIALIZED:
        return

    with _LOCK:
        if path in _INITIALIZED:
            return

        conn = _connect(path)
        try:
            schema = _SCHEMA_PATH.read_text(encoding="utf-8")
            conn.executescript(schema)
            conn.commit()
            _INITIALIZED.add(path)
        finally:
            conn.close()


def reset_db(db_path: str | None = None) -> None:
    """Borra TODAS las tablas. Solo para tests."""
    path = db_path or get_settings().db_path
    if Path(path).exists():
        Path(path).unlink()
    _INITIALIZED.discard(path)


def db_exists(db_path: str | None = None) -> bool:
    path = db_path or get_settings().db_path()
    return Path(path).exists()
