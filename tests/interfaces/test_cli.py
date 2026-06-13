"""Tests del CLI (doctor/status/trades/run) contra DB temporal."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from infrastructure.config.settings import reset_settings_cache
from infrastructure.persistence.sqlite import db, run_repo, trade_repo
from interfaces.cli import main as cli
from pipeline.contracts import RunResult


@pytest.fixture(autouse=True)
def temp_env(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("SYMBOLS", "US500")
    reset_settings_cache()
    db.reset_db(db_path)
    db.init_db(db_path)
    yield db_path
    db.reset_db(db_path)
    reset_settings_cache()


def test_status_empty_db(capsys):
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Total trades: 0" in out


def test_status_with_data(capsys):
    run_repo.start_run("r1")
    run_repo.finish_run("r1", "success")
    tid = trade_repo.insert_trade(
        "r1", "US500", "BUY", 0.5, 5000.0, 4990.0, 5010.0, is_runner=False, status="OPEN"
    )
    trade_repo.close_trade(tid, "CLOSED_TP", exit_price=5010.0, pnl=5.0, r_multiple=1.0)

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Total trades: 1" in out
    assert "Wins: 1" in out
    assert "success" in out


def test_trades_empty(capsys):
    assert cli.main(["trades"]) == 0
    assert "Sin trades" in capsys.readouterr().out


def test_trades_lists_rows(capsys):
    run_repo.start_run("r1")
    trade_repo.insert_trade(
        "r1", "US500", "BUY", 0.5, 5000.0, None, None, is_runner=False, status="OPEN"
    )
    assert cli.main(["trades", "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert "US500" in out
    assert "OPEN" in out


def test_doctor_runs(capsys):
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "Doctor" in out
    assert "US500" in out
    assert "Credenciales:" in out


def test_run_invokes_pipeline_success(capsys):
    fake = RunResult(
        run_id="x", started_at="2026-06-12T12:00:00", finished_at="2026-06-12T12:01:00",
        status="success", decisions_count=1, orders_sent=2,
    )
    with patch("pipeline.orchestrator.run_pipeline", return_value=fake) as mock_run:
        rc = cli.main(["run", "--dry-run"])
    assert rc == 0
    mock_run.assert_called_once()
    assert '"orders_sent": 2' in capsys.readouterr().out


def test_run_failed_returns_1():
    fake = RunResult(
        run_id="x", started_at="2026-06-12T12:00:00", finished_at="2026-06-12T12:01:00",
        status="failed", errors=["boom"],
    )
    with patch("pipeline.orchestrator.run_pipeline", return_value=fake):
        assert cli.main(["run", "--dry-run"]) == 1


def test_run_without_openai_key_exits_1(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "placeholder")
    reset_settings_cache()
    with patch("pipeline.orchestrator.run_pipeline", MagicMock()) as mock_run:
        assert cli.main(["run", "--dry-run"]) == 1
        mock_run.assert_not_called()
    assert "OPENAI_API_KEY" in capsys.readouterr().err
