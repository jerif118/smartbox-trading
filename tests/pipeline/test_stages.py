"""Tests del pipeline (contratos + orchestrator)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from domain.strategy.box import Box
from domain.strategy.decision import Action, RiskMode
from infrastructure.config.settings import reset_settings_cache
from infrastructure.persistence.sqlite import db
from pipeline.contracts import (
    AnalyzeInput,
    AnalyzeOutput,
    ContextInput,
    DecisionContract,
    ExecuteInput,
    IngestInput,
    ManageInput,
    PreprocessInput,
    PreprocessOutput,
    SignalInput,
)
from pipeline.stages.s1_ingest import stage_ingest
from pipeline.stages.s2_preprocess import stage_preprocess
from pipeline.stages.s3_context import stage_context
from pipeline.stages.s4_signal import stage_signal
from pipeline.stages.s6_execute import stage_execute
from pipeline.stages.s7_manage import stage_manage


@pytest.fixture(autouse=True)
def setup_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("MIN_CONFIDENCE", "0")
    monkeypatch.setenv("MAX_ORDERS_PER_DAY", "4")
    reset_settings_cache()
    db.reset_db()
    db.init_db()
    yield
    reset_settings_cache()
    db.reset_db()


# ── Contratos Pydantic ────────────────────────────────────────────────
def test_ingest_input_validation() -> None:
    inp = IngestInput(symbol="US500", start_iso="2026-06-01T08:00:00", end_iso="2026-06-01T10:00:00")
    assert inp.symbol == "US500"
    assert inp.timeframe == "MINUTE_5"


def test_preprocess_output_accepts_high_amplitude_for_skip_logic() -> None:
    """Box con amplitud > 1% se calcula, pero no es tradeable."""
    bad_box = Box(high=102.0, low=100.0, amplitude_pct=2.0, n_candles=10)
    out = PreprocessOutput(
        symbol="US500", box=bad_box, rsi_last=50, volume_profile=None, box_candles=[]
    )
    assert out.box is bad_box
    assert not out.box.is_valid()


def test_preprocess_output_rejects_malformed_box() -> None:
    """El contrato sigue rechazando cajas sin niveles coherentes."""
    from pydantic import ValidationError

    bad_box = Box(high=99.0, low=100.0, amplitude_pct=-1.0, n_candles=10)
    with pytest.raises(ValidationError):
        PreprocessOutput(
            symbol="US500", box=bad_box, rsi_last=50, volume_profile=None, box_candles=[]
        )


def test_decision_contract_validation() -> None:
    d = DecisionContract(
        symbol="US500",
        action=Action.LONG,
        risk=RiskMode.COMPLETO,
        confidence=80,
        reasons=["breakout"],
        key_levels={"high": 5000, "low": 4990},
        signal={"state": "ABOVE"},
        team_consensus="unanimous",
    )
    assert d.confidence == 80
    assert d.action == Action.LONG


def test_decision_contract_rejects_invalid_confidence() -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        DecisionContract(
            symbol="US500", action=Action.LONG, risk=RiskMode.COMPLETO,
            confidence=150, reasons=["x"], key_levels={}, signal={}, team_consensus="u",
        )


def test_analyze_output_requires_min_one_decision() -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        AnalyzeOutput(decisions=[])


def test_order_contract_validates_volume() -> None:
    from pydantic import ValidationError
    from pipeline.contracts import OrderContract
    with pytest.raises(ValidationError):
        OrderContract(
            symbol="X", side="BUY", volume=0, entry_price=100, stop_loss=99, take_profit=101,
            is_runner=False,
        )


# ── Stage 4: Signal ───────────────────────────────────────────────────
def test_signal_above() -> None:
    box = Box(high=101.0, low=99.0, amplitude_pct=2.02, n_candles=10)
    df = pd.DataFrame(
        {
            "time": [100, 200, 300, 400, 500],
            "open": [100.0, 100.5, 100.8, 101.0, 101.5],
            "high": [100.5, 101.0, 101.2, 101.5, 102.0],
            "low": [99.5, 100.0, 100.5, 100.8, 101.2],
            "close": [100.0, 100.5, 100.8, 101.5, 102.0],
            "volume": [100, 100, 100, 100, 100],
        }
    )
    out = stage_signal(SignalInput(symbol="US500", df_candles=df, box=box))
    assert out.has_breakout is True
    assert out.breakout_state == "ABOVE"


def test_signal_no_breakout() -> None:
    box = Box(high=101.0, low=99.0, amplitude_pct=2.02, n_candles=10)
    df = pd.DataFrame(
        {
            "time": [100, 200, 300],
            "open": [100.0] * 3,
            "high": [100.5] * 3,
            "low": [99.5] * 3,
            "close": [100.0, 100.2, 100.4],  # todas dentro
            "volume": [100, 100, 100],
        }
    )
    out = stage_signal(SignalInput(symbol="US500", df_candles=df, box=box))
    assert out.has_breakout is False


# ── Stage 6: Execute ──────────────────────────────────────────────────
def test_execute_no_operar() -> None:
    from infrastructure.persistence.sqlite import run_repo
    run_repo.start_run("exec-test")
    box = Box(high=101.0, low=99.0, amplitude_pct=2.02, n_candles=10)
    decision = DecisionContract(
        symbol="US500", action=Action.NO_OPERAR, risk=RiskMode.COMPLETO,
        confidence=80, reasons=["test"], key_levels={}, signal={}, team_consensus="u",
    )
    inp = ExecuteInput(decision=decision, symbol="US500", box=box, base_volume=1.0, min_rr=1.0)
    out = stage_execute(inp, "exec-test", budget=MagicMock(), broker=MagicMock())
    assert len(out.orders) == 0


def test_execute_long_sends_two_orders() -> None:
    from infrastructure.persistence.sqlite import run_repo, trade_repo
    run_repo.start_run("exec-long")
    box = Box(high=100.5, low=100.0, amplitude_pct=0.5, n_candles=10)  # valid: < 1%
    decision = DecisionContract(
        symbol="US500", action=Action.LONG, risk=RiskMode.COMPLETO,
        confidence=80, reasons=["test"], key_levels={"high": 100.5, "low": 100.0},
        signal={"state": "ABOVE"}, team_consensus="u",
    )
    inp = ExecuteInput(decision=decision, symbol="US500", box=box, base_volume=1.0, min_rr=1.0)
    from domain.strategy.budget import DailyOrderBudget
    budget = DailyOrderBudget(max_orders=4)
    broker = MagicMock()
    broker.place_order.return_value = "BR-12345"
    out = stage_execute(inp, "exec-long", budget=budget, broker=broker)
    assert len(out.orders) == 2
    assert budget.used == 2
    # ambas órdenes en DB
    trades = trade_repo.list_trades(symbol="US500", limit=10)
    assert len(trades) == 2


def test_execute_respects_budget() -> None:
    """Si budget está agotado, no envía órdenes."""
    from infrastructure.persistence.sqlite import run_repo
    run_repo.start_run("exec-budget")
    box = Box(high=100.5, low=100.0, amplitude_pct=0.5, n_candles=10)
    decision = DecisionContract(
        symbol="US500", action=Action.LONG, risk=RiskMode.COMPLETO,
        confidence=80, reasons=["test"], key_levels={}, signal={}, team_consensus="u",
    )
    inp = ExecuteInput(decision=decision, symbol="US500", box=box, base_volume=1.0, min_rr=1.0)
    from domain.strategy.budget import DailyOrderBudget
    budget = DailyOrderBudget(max_orders=0)  # agotado
    broker = MagicMock()
    out = stage_execute(inp, "exec-budget", budget=budget, broker=broker)
    assert len(out.orders) == 0
    assert "budget" in str(out.errors).lower() or len(out.errors) > 0


# ── Stage 7: Manage ───────────────────────────────────────────────────
def test_manage_moves_sl_to_breakeven() -> None:
    from infrastructure.persistence.sqlite import run_repo, trade_repo
    run_repo.start_run("pm-test")
    tid = trade_repo.insert_trade(
        run_id="pm-test", symbol="US500", side="BUY", volume=0.5,
        entry_price=100.0, stop_loss=98.0, take_profit=102.0, is_runner=False,
    )
    trade_repo.update_status(tid, "OPEN", broker_order_id="BR-99")

    inp = ManageInput(
        open_trades=[{
            "id": tid, "symbol": "US500", "side": "BUY",
            "entry_price": 100.0, "stop_loss": 98.0,
            "take_profit": 102.0, "is_runner": 0, "broker_order_id": "BR-99",
        }],
        current_prices={"US500": 102.0},  # +1R exacto (risk=2, profit=2)
    )
    broker = MagicMock()
    out = stage_manage(inp, "pm-test", broker=broker)
    # debe haber modificado el SL
    assert any(a.action == "MODIFY_SL" for a in out.actions)
    modified = trade_repo.get_trade(tid)
    assert modified.stop_loss == 100.0  # BE
    broker.modify_order.assert_called_once()


def test_manage_hold_when_r_below_1() -> None:
    from infrastructure.persistence.sqlite import run_repo, trade_repo
    run_repo.start_run("pm-hold")
    tid = trade_repo.insert_trade(
        run_id="pm-hold", symbol="US500", side="BUY", volume=0.5,
        entry_price=100.0, stop_loss=98.0, take_profit=102.0, is_runner=False,
    )
    trade_repo.update_status(tid, "OPEN", broker_order_id="BR-99")
    inp = ManageInput(
        open_trades=[{
            "id": tid, "symbol": "US500", "side": "BUY",
            "entry_price": 100.0, "stop_loss": 98.0,
            "take_profit": 102.0, "is_runner": 0, "broker_order_id": "BR-99",
        }],
        current_prices={"US500": 99.0},  # R = -0.5
    )
    out = stage_manage(inp, "pm-hold", broker=MagicMock())
    assert all(a.action == "HOLD" for a in out.actions)
