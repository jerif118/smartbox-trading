"""Tests de los repos SQLite (Fase 2)."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from infrastructure.config.settings import reset_settings_cache
from infrastructure.persistence.sqlite import db
from infrastructure.persistence.sqlite import (
    decision_repo,
    equity_repo,
    event_repo,
    run_repo,
    trade_repo,
)
from infrastructure.persistence.sqlite.models import TradeStatus


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    """Cada test usa una DB temporal."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    reset_settings_cache()
    db.reset_db(db_path)
    db.init_db(db_path)
    yield db_path
    db.reset_db(db_path)


def test_init_creates_tables(temp_db):
    with db.get_db() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    table_names = {t["name"] for t in tables}
    assert {"runs", "decisions", "trades", "agent_events", "equity_snapshots"}.issubset(table_names)


def test_run_lifecycle(temp_db):
    run = run_repo.start_run("test-run-1", {"key": "value"})
    assert run.id == "test-run-1"
    assert run.status == "running"

    run_repo.finish_run("test-run-1", "success")
    fetched = run_repo.get_run("test-run-1")
    assert fetched is not None
    assert fetched.status == "success"
    assert fetched.finished_at is not None


def test_run_with_error(temp_db):
    run_repo.start_run("fail-run")
    run_repo.finish_run("fail-run", "failed", error="API down")
    fetched = run_repo.get_run("fail-run")
    assert fetched.error == "API down"


def test_list_runs(temp_db):
    for i in range(3):
        run_repo.start_run(f"r{i}")
    runs = run_repo.list_runs(limit=10)
    assert len(runs) == 3


def test_decision_insert_and_get(temp_db):
    run_repo.start_run("d-run")
    did = decision_repo.insert_decision(
        run_id="d-run",
        symbol="US500",
        action="LONG",
        risk="COMPLETO",
        confidence=85,
        reasons=["breakout", "RSI ok"],
        team_consensus="unanimous",
        key_levels={"high": 5000, "low": 4990},
    )
    assert did > 0
    d = decision_repo.get_decision(did)
    assert d is not None
    assert d.symbol == "US500"
    assert d.action == "LONG"
    import json
    assert json.loads(d.reasons) == ["breakout", "RSI ok"]


def test_trade_insert_and_status(temp_db):
    run_repo.start_run("t-run")
    did = decision_repo.insert_decision(
        run_id="t-run", symbol="US500", action="LONG",
        risk="COMPLETO", confidence=80, reasons=["x"],
    )
    tid = trade_repo.insert_trade(
        run_id="t-run",
        decision_id=did,
        symbol="US500",
        side="BUY",
        volume=0.5,
        entry_price=5000.0,
        stop_loss=4990.0,
        take_profit=5010.0,
        is_runner=False,
    )
    trade = trade_repo.get_trade(tid)
    assert trade is not None
    assert trade.is_runner is False
    assert trade.status == TradeStatus.PENDING.value

    trade_repo.update_status(tid, TradeStatus.OPEN.value, broker_order_id="br-123")
    trade = trade_repo.get_trade(tid)
    assert trade.status == "OPEN"
    assert trade.broker_order_id == "br-123"


def test_trade_close_with_pnl(temp_db):
    run_repo.start_run("t2-run")
    tid = trade_repo.insert_trade(
        run_id="t2-run", symbol="US500", side="BUY", volume=0.5,
        entry_price=5000.0, stop_loss=4990.0, take_profit=5010.0, is_runner=False,
    )
    trade_repo.close_trade(
        tid, status=TradeStatus.CLOSED_TP.value, exit_price=5010.0,
        pnl=5.0, r_multiple=1.0, reason="TP hit",
    )
    t = trade_repo.get_trade(tid)
    assert t.status == "CLOSED_TP"
    assert t.pnl == 5.0
    assert t.exit_price == 5010.0


def test_trade_modify_sl(temp_db):
    run_repo.start_run("t3-run")
    tid = trade_repo.insert_trade(
        run_id="t3-run", symbol="US500", side="BUY", volume=0.5,
        entry_price=5000.0, stop_loss=4990.0, take_profit=5010.0, is_runner=False,
    )
    trade_repo.update_status(tid, TradeStatus.OPEN.value)
    trade_repo.modify_sl_tp(tid, stop_loss=4995.0)  # move to BE
    t = trade_repo.get_trade(tid)
    assert t.stop_loss == 4995.0


def test_list_open_trades(temp_db):
    run_repo.start_run("list-run")
    for i in range(3):
        tid = trade_repo.insert_trade(
            run_id="list-run", symbol="US500", side="BUY", volume=0.1,
            entry_price=5000.0, stop_loss=4990.0, take_profit=5010.0, is_runner=False,
        )
        trade_repo.update_status(tid, TradeStatus.OPEN.value)
    # 1 trade PENDING (no update_status)
    trade_repo.insert_trade(
        run_id="list-run", symbol="US500", side="BUY", volume=0.1,
        entry_price=5000.0, stop_loss=4990.0, take_profit=5010.0, is_runner=False,
    )
    open_trades = trade_repo.list_open_trades()
    assert len(open_trades) == 3
    pending = trade_repo.list_pending_trades()
    assert len(pending) == 1


def test_compute_stats(temp_db):
    run_repo.start_run("stats-run")
    for i, (status, pnl, r) in enumerate([
        (TradeStatus.CLOSED_TP, 10.0, 1.0),
        (TradeStatus.CLOSED_TP, 20.0, 2.0),
        (TradeStatus.CLOSED_SL, -5.0, -0.5),
    ]):
        tid = trade_repo.insert_trade(
            run_id="stats-run", symbol="US500", side="BUY", volume=0.1,
            entry_price=5000.0, stop_loss=4990.0, take_profit=5010.0, is_runner=False,
        )
        trade_repo.close_trade(tid, status=status, exit_price=5005.0, pnl=pnl, r_multiple=r)

    stats = trade_repo.compute_stats()
    assert stats["total_trades"] == 3
    assert stats["wins"] == 2
    assert stats["losses"] == 1
    assert stats["total_pnl"] == 25.0
    assert stats["win_rate_pct"] == pytest.approx(66.67, abs=0.01)


def test_agent_event_logging(temp_db):
    run_repo.start_run("evt-run")
    eid = event_repo.log_event(
        run_id="evt-run",
        agent="trader",
        event_type="TOOL_CALL",
        payload={"tool": "analyze_box", "args": {"symbol": "US500"}},
        duration_ms=120,
    )
    assert eid > 0
    events = event_repo.list_events_by_run("evt-run")
    assert len(events) == 1
    assert events[0].agent == "trader"
    assert events[0].duration_ms == 120


def test_equity_snapshot(temp_db):
    run_repo.start_run("eq-run")
    sid = equity_repo.insert_snapshot(
        balance=10000.0, equity=10050.0, daily_pnl=50.0,
        total_pnl=250.0, open_positions=2, source="broker", run_id="eq-run",
    )
    assert sid > 0
    latest = equity_repo.latest_snapshot()
    assert latest is not None
    assert latest.balance == 10000.0
    assert latest.open_positions == 2


def test_equity_snapshots_ordering(temp_db):
    for i in range(3):
        equity_repo.insert_snapshot(balance=10000 + i, equity=10050 + i, source="c")
    snaps = equity_repo.list_snapshots()
    assert len(snaps) == 3
    # ordered ASC by ts
    assert snaps[0].balance <= snaps[-1].balance
