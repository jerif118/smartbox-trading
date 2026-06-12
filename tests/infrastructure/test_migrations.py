"""Tests del runner de migraciones de schema (PRAGMA user_version)."""

from __future__ import annotations

import sqlite3

import pytest

from infrastructure.config.settings import reset_settings_cache
from infrastructure.persistence.sqlite import db, run_repo, stage_metrics_repo, trade_repo

# Schema v0: el estado de las DBs creadas ANTES del sistema de migraciones
# (sin client_order_id, sin stage_metrics). Recorte mínimo para los tests.
_SCHEMA_V0 = """
CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  error TEXT,
  config_snapshot TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  symbol TEXT NOT NULL,
  action TEXT NOT NULL,
  risk TEXT, confidence INTEGER, reasons TEXT, team_consensus TEXT,
  crew_raw_output TEXT, key_levels TEXT, signal TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE TABLE trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  decision_id INTEGER,
  ts_open TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  volume REAL NOT NULL,
  entry_price REAL NOT NULL,
  stop_loss REAL, take_profit REAL,
  is_runner INTEGER NOT NULL DEFAULT 0,
  broker_order_id TEXT,
  status TEXT NOT NULL,
  ts_close TEXT, exit_price REAL, pnl REAL, r_multiple REAL, reason TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (run_id) REFERENCES runs(id)
);
"""


@pytest.fixture(autouse=True)
def temp_env(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    reset_settings_cache()
    db.reset_db(db_path)
    yield db_path
    db.reset_db(db_path)


def _make_v0_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA_V0)
    conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES ('old-run', '2026-01-01', 'success')"
    )
    conn.execute(
        "INSERT INTO trades (run_id, ts_open, symbol, side, volume, entry_price, status) "
        "VALUES ('old-run', '2026-01-01', 'US500', 'BUY', 1.0, 5000.0, 'OPEN')"
    )
    conn.commit()
    conn.close()


def test_fresh_db_gets_latest_version(temp_env):
    db.init_db(temp_env)
    with db.get_db() as conn:
        assert db.get_schema_version(conn) == db.LATEST_SCHEMA_VERSION
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
        assert "client_order_id" in cols
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "stage_metrics" in tables


def test_v0_db_migrates_preserving_data(temp_env):
    _make_v0_db(temp_env)
    db.init_db(temp_env)
    with db.get_db() as conn:
        assert db.get_schema_version(conn) == db.LATEST_SCHEMA_VERSION
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
        assert "client_order_id" in cols
        # datos previos intactos
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        trade = conn.execute("SELECT * FROM trades").fetchone()
        assert trade["symbol"] == "US500"
        assert trade["client_order_id"] is None


def test_init_db_twice_is_idempotent(temp_env):
    _make_v0_db(temp_env)
    db.init_db(temp_env)
    db._INITIALIZED.discard(temp_env)  # simula otro proceso
    db.init_db(temp_env)
    with db.get_db() as conn:
        assert db.get_schema_version(conn) == db.LATEST_SCHEMA_VERSION


def test_partial_unique_index_blocks_active_duplicates(temp_env):
    db.init_db(temp_env)
    run_repo.start_run("r1")
    coid = "2026-06-12:US500:BUY:P"
    trade_repo.insert_trade(
        "r1", "US500", "BUY", 0.5, 5000.0, 4990.0, 5010.0,
        is_runner=False, status="PENDING", client_order_id=coid,
    )
    with pytest.raises(sqlite3.IntegrityError):
        trade_repo.insert_trade(
            "r1", "US500", "BUY", 0.5, 5000.0, 4990.0, 5010.0,
            is_runner=False, status="PENDING", client_order_id=coid,
        )


def test_partial_unique_index_allows_retry_after_rejected(temp_env):
    db.init_db(temp_env)
    run_repo.start_run("r1")
    coid = "2026-06-12:US500:BUY:P"
    tid = trade_repo.insert_trade(
        "r1", "US500", "BUY", 0.5, 5000.0, 4990.0, 5010.0,
        is_runner=False, status="PENDING", client_order_id=coid,
    )
    trade_repo.update_status(tid, "REJECTED")
    # tras REJECTED el mismo coid puede reintentarse
    tid2 = trade_repo.insert_trade(
        "r1", "US500", "BUY", 0.5, 5000.0, 4990.0, 5010.0,
        is_runner=False, status="PENDING", client_order_id=coid,
    )
    assert tid2 != tid


def test_find_active_by_client_order_id(temp_env):
    db.init_db(temp_env)
    run_repo.start_run("r1")
    coid = "2026-06-12:US100:SELL:R"
    assert trade_repo.find_active_by_client_order_id(coid) is None
    tid = trade_repo.insert_trade(
        "r1", "US100", "SELL", 0.5, 20000.0, 20050.0, 19950.0,
        is_runner=True, status="PENDING", client_order_id=coid,
    )
    found = trade_repo.find_active_by_client_order_id(coid)
    assert found is not None and found.id == tid
    trade_repo.update_status(tid, "REJECTED")
    assert trade_repo.find_active_by_client_order_id(coid) is None


def test_count_orders_today_excludes_rejected(temp_env):
    db.init_db(temp_env)
    run_repo.start_run("r1")
    t1 = trade_repo.insert_trade(
        "r1", "US500", "BUY", 0.5, 5000.0, None, None, is_runner=False, status="OPEN"
    )
    trade_repo.insert_trade(
        "r1", "US500", "BUY", 0.5, 5000.0, None, None, is_runner=True, status="PENDING"
    )
    rejected = trade_repo.insert_trade(
        "r1", "US100", "SELL", 0.5, 20000.0, None, None, is_runner=False, status="REJECTED"
    )
    assert t1 != rejected
    assert trade_repo.count_orders_today() == 2


def test_fail_stale_runs(temp_env):
    db.init_db(temp_env)
    run_repo.start_run("zombie-1")
    run_repo.start_run("zombie-2")
    run_repo.start_run("done")
    run_repo.finish_run("done", "success")

    repaired = run_repo.fail_stale_runs()
    assert repaired == 2
    for rid in ("zombie-1", "zombie-2"):
        r = run_repo.get_run(rid)
        assert r.status == "failed"
        assert "stale" in r.error
    assert run_repo.get_run("done").status == "success"


def test_stage_metrics_roundtrip(temp_env):
    db.init_db(temp_env)
    run_repo.start_run("r1")
    stage_metrics_repo.insert_stage_metric(
        "r1", "s1_ingest:US500", "ok", "2026-06-12T12:00:00+00:00", 1234
    )
    stage_metrics_repo.insert_stage_metric(
        "r1", "s5_analyze", "timeout", "2026-06-12T12:01:00+00:00", 300000,
        error_type="StageTimeoutError", error_msg="timeout tras 300s",
    )
    metrics = stage_metrics_repo.list_stage_metrics("r1")
    assert len(metrics) == 2
    assert metrics[0]["status"] == "ok"
    assert metrics[1]["error_type"] == "StageTimeoutError"
