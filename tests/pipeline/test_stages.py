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
    inp = IngestInput(
        symbol="US500", start_iso="2026-06-01T08:00:00", end_iso="2026-06-01T10:00:00"
    )
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


def test_preprocess_computes_simplefx_box_from_df_simple() -> None:
    """Con df_simple, la caja SimpleFX se calcula en su propio espacio de precios."""
    from domain.market_time import box_window_unix

    box_from, box_to = box_window_unix("2026-06-11", "08:00", "09:55", "America/New_York")
    mid = (box_from + box_to) // 2
    # Capital ~7500, SimpleFX ~19 pts abajo (offset como en las capturas del user).
    cap = pd.DataFrame(
        {"time": [box_from, mid, box_to], "open": [7500.0] * 3,
         "high": [7513.0, 7514.0, 7512.0], "low": [7470.0, 7471.0, 7469.0],
         "close": [7500.0] * 3, "volume": [1] * 3}
    )
    simple = pd.DataFrame(
        {"time": [box_from, mid, box_to], "open": [7481.0] * 3,
         "high": [7494.0, 7495.0, 7493.0], "low": [7451.0, 7452.0, 7450.0],
         "close": [7481.0] * 3, "volume": [1] * 3}
    )
    inp = PreprocessInput(symbol="US500", start_iso="2026-06-11T08:00:00",
                          end_iso="2026-06-11T12:00:00", box_date="2026-06-11")

    out = stage_preprocess(inp, cap, simple)

    assert out.box.high == 7514.0 and out.box.low == 7469.0  # Capital
    assert out.box_simple is not None
    assert out.box_simple.high == 7495.0 and out.box_simple.low == 7450.0  # SimpleFX


def test_preprocess_box_simple_none_without_df_simple() -> None:
    """Sin df_simple (feed caído) la caja SimpleFX queda en None."""
    from domain.market_time import box_window_unix

    box_from, box_to = box_window_unix("2026-06-11", "08:00", "09:55", "America/New_York")
    cap = pd.DataFrame(
        {"time": [box_from, box_to], "open": [100.0, 100.0], "high": [101.0, 101.5],
         "low": [99.5, 99.0], "close": [100.5, 100.5], "volume": [1, 1]}
    )
    inp = PreprocessInput(symbol="US500", start_iso="2026-06-11T08:00:00",
                          end_iso="2026-06-11T12:00:00", box_date="2026-06-11")
    out = stage_preprocess(inp, cap, None)
    assert out.box_simple is None


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
            symbol="US500",
            action=Action.LONG,
            risk=RiskMode.COMPLETO,
            confidence=150,
            reasons=["x"],
            key_levels={},
            signal={},
            team_consensus="u",
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
            symbol="X",
            side="BUY",
            volume=0,
            entry_price=100,
            stop_loss=99,
            take_profit=101,
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


def test_signal_rejects_stale_first_breakout() -> None:
    box = Box(high=101.0, low=99.0, amplitude_pct=0.5, n_candles=10)
    df = pd.DataFrame(
        {
            "time": [100, 400, 1300],
            "close": [101.5, 100.5, 100.4],
        }
    )
    out = stage_signal(SignalInput(symbol="US500", df_candles=df, box=box, max_age_minutes=15))
    assert out.has_breakout is False
    assert out.signal_age_minutes == 20.0


# ── Stage 3: Context ──────────────────────────────────────────────────
def test_context_degraded_reduces_risk_instead_of_false_low(monkeypatch) -> None:
    from infrastructure.data_sources.scrapers import CalendarResult

    monkeypatch.setattr(
        "infrastructure.data_sources.scrapers.PublicUSMacroCalendarAdapter.get_calendar",
        lambda _self, _date: CalendarResult(
            events=[],
            status="DEGRADED",
            providers_ok=["BLS"],
            providers_failed=["BEA", "FED"],
        ),
    )
    out = stage_context(
        ContextInput(
            date_str="2026-07-15",
            reference_time="2026-07-15T14:00:00+00:00",
        )
    )
    assert out.macro_risk == "MEDIUM"
    assert out.provider_status == "DEGRADED"


def test_context_all_providers_down_is_not_low(monkeypatch) -> None:
    from infrastructure.data_sources.scrapers import CalendarResult

    monkeypatch.setattr(
        "infrastructure.data_sources.scrapers.PublicUSMacroCalendarAdapter.get_calendar",
        lambda _self, _date: CalendarResult(
            events=[], status="UNAVAILABLE", providers_failed=["BLS", "BEA", "FED"]
        ),
    )
    with pytest.raises(ConnectionError):
        stage_context(ContextInput(date_str="2026-07-15"))


# ── Stage 6: Execute ──────────────────────────────────────────────────
def test_execute_no_operar() -> None:
    from infrastructure.persistence.sqlite import run_repo

    run_repo.start_run("exec-test")
    box = Box(high=101.0, low=99.0, amplitude_pct=2.02, n_candles=10)
    decision = DecisionContract(
        symbol="US500",
        action=Action.NO_OPERAR,
        risk=RiskMode.COMPLETO,
        confidence=80,
        reasons=["test"],
        key_levels={},
        signal={},
        team_consensus="u",
    )
    inp = ExecuteInput(decision=decision, symbol="US500", box=box, base_volume=1.0, min_rr=1.0)
    out = stage_execute(inp, "exec-test", budget=MagicMock(), broker=MagicMock())
    assert len(out.orders) == 0


def test_execute_long_sends_two_orders() -> None:
    from infrastructure.persistence.sqlite import run_repo, trade_repo

    run_repo.start_run("exec-long")
    box = Box(high=100.5, low=100.0, amplitude_pct=0.5, n_candles=10)  # valid: < 1%
    decision = DecisionContract(
        symbol="US500",
        action=Action.LONG,
        risk=RiskMode.COMPLETO,
        confidence=80,
        reasons=["test"],
        key_levels={"high": 100.5, "low": 100.0},
        signal={"state": "ABOVE"},
        team_consensus="u",
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
        symbol="US500",
        action=Action.LONG,
        risk=RiskMode.COMPLETO,
        confidence=80,
        reasons=["test"],
        key_levels={},
        signal={},
        team_consensus="u",
    )
    inp = ExecuteInput(decision=decision, symbol="US500", box=box, base_volume=1.0, min_rr=1.0)
    from domain.strategy.budget import DailyOrderBudget

    budget = DailyOrderBudget(max_orders=0)  # agotado
    broker = MagicMock()
    out = stage_execute(inp, "exec-budget", budget=budget, broker=broker)
    assert len(out.orders) == 0
    assert "budget" in str(out.errors).lower() or len(out.errors) > 0


def test_execute_rejects_direction_against_breakout() -> None:
    box = Box(high=100.5, low=100.0, amplitude_pct=0.5, n_candles=10)
    decision = DecisionContract(
        symbol="US500",
        action=Action.SHORT,
        risk=RiskMode.MEDIO,
        confidence=80,
        reasons=["test"],
        key_levels={},
        signal={"breakout_state": "ABOVE"},
        team_consensus="u",
    )
    inp = ExecuteInput(decision=decision, symbol="US500", box=box, base_volume=1.0)
    broker = MagicMock()
    out = stage_execute(inp, "exec-test", budget=MagicMock(), broker=broker)
    assert any("direction mismatch" in error for error in out.errors)
    broker.place_order.assert_not_called()


def test_execute_applies_modify_levels() -> None:
    from domain.strategy.budget import DailyOrderBudget
    from infrastructure.persistence.sqlite import run_repo

    run_repo.start_run("exec-modify")
    box = Box(high=100.5, low=100.0, amplitude_pct=0.5, n_candles=10)
    decision = DecisionContract(
        symbol="US500",
        action=Action.LONG,
        risk=RiskMode.MEDIO,
        confidence=70,
        reasons=["ajuste"],
        key_levels={"suggested_stop_loss": 100.1, "suggested_take_profit": 101.3},
        signal={"breakout_state": "ABOVE", "risk_decision": "MODIFY"},
        team_consensus="u",
    )
    broker = MagicMock()
    broker.place_order.return_value = "BR-MOD"
    out = stage_execute(
        ExecuteInput(decision=decision, symbol="US500", box=box, base_volume=1.0),
        "exec-modify",
        DailyOrderBudget(max_orders=4),
        broker,
    )
    assert len(out.orders) == 2
    assert all(order.stop_loss == 100.1 for order in out.orders)
    assert out.orders[0].take_profit == 101.3


# ── Stage 7: Manage ───────────────────────────────────────────────────
def test_manage_moves_sl_to_breakeven() -> None:
    from infrastructure.persistence.sqlite import run_repo, trade_repo

    run_repo.start_run("pm-test")
    tid = trade_repo.insert_trade(
        run_id="pm-test",
        symbol="US500",
        side="BUY",
        volume=0.5,
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=None,
        is_runner=True,
    )
    trade_repo.update_status(tid, "OPEN", broker_order_id="BR-99")

    inp = ManageInput(
        open_trades=[
            {
                "id": tid,
                "symbol": "US500",
                "side": "BUY",
                "entry_price": 100.0,
                "stop_loss": 98.0,
                "take_profit": None,
                "is_runner": 1,
                "broker_order_id": "BR-99",
            }
        ],
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
        run_id="pm-hold",
        symbol="US500",
        side="BUY",
        volume=0.5,
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=102.0,
        is_runner=False,
    )
    trade_repo.update_status(tid, "OPEN", broker_order_id="BR-99")
    inp = ManageInput(
        open_trades=[
            {
                "id": tid,
                "symbol": "US500",
                "side": "BUY",
                "entry_price": 100.0,
                "stop_loss": 98.0,
                "take_profit": 102.0,
                "is_runner": 0,
                "broker_order_id": "BR-99",
            }
        ],
        current_prices={"US500": 99.0},  # R = -0.5
    )
    out = stage_manage(inp, "pm-hold", broker=MagicMock())
    assert all(a.action == "HOLD" for a in out.actions)


def test_manage_leaves_primary_to_fixed_take_profit() -> None:
    inp = ManageInput(
        open_trades=[
            {
                "id": 88,
                "symbol": "US500",
                "side": "BUY",
                "entry_price": 100.0,
                "stop_loss": 98.0,
                "take_profit": 102.0,
                "is_runner": 0,
                "broker_order_id": "BR-P",
            }
        ],
        current_prices={"US500": 104.0},
    )
    broker = MagicMock()
    out = stage_manage(inp, "pm-primary", broker)
    assert out.actions[0].action == "HOLD"
    broker.modify_order.assert_not_called()


def test_runner_keeps_original_risk_after_breakeven() -> None:
    from infrastructure.persistence.sqlite import run_repo, trade_repo

    run_repo.start_run("pm-trailing")
    trade_id = trade_repo.insert_trade(
        "pm-trailing",
        "US500",
        "BUY",
        0.5,
        100.0,
        98.0,
        None,
        is_runner=True,
        status="OPEN",
        broker_order_id="BR-R",
    )
    broker = MagicMock()

    first = trade_repo.get_trade(trade_id)
    stage_manage(
        ManageInput(open_trades=[first.to_dict()], current_prices={"US500": 102.0}),
        "pm-trailing",
        broker,
    )
    after_be = trade_repo.get_trade(trade_id)
    assert after_be.stop_loss == 100.0
    assert after_be.initial_stop_loss == 98.0

    out = stage_manage(
        ManageInput(open_trades=[after_be.to_dict()], current_prices={"US500": 104.0}),
        "pm-trailing",
        broker,
    )
    assert out.actions[0].action == "MODIFY_SL"
    assert out.actions[0].new_sl == 102.0
    assert trade_repo.get_trade(trade_id).stop_loss == 102.0


def test_manage_trailing_uses_high_water_not_current() -> None:
    """A2: el trailing se ancla al máximo favorable, no al precio actual.

    El precio subió a un pico (2R+) y luego retrocedió por debajo de 1R. Con
    trailing sobre el precio actual sería HOLD; con high-water debe arrastrar el
    SL respecto al pico ya alcanzado.
    """
    from infrastructure.persistence.sqlite import run_repo, trade_repo

    run_repo.start_run("pm-hw")
    tid = trade_repo.insert_trade(
        "pm-hw", "US500", "BUY", 0.5, 100.0, 98.0, None,
        is_runner=True, status="OPEN", broker_order_id="BR-HW",
    )
    # El pico histórico fue 106 (=3R); el precio ahora está en 101 (=0.5R).
    trade_repo.update_max_favorable(tid, 106.0)
    trade = trade_repo.get_trade(tid)

    broker = MagicMock()
    out = stage_manage(
        ManageInput(open_trades=[trade.to_dict()], current_prices={"US500": 101.0}),
        "pm-hw",
        broker,
    )
    # Trailing 1R detrás del pico (106 - 2 = 104), pese a que el precio actual es 101.
    assert out.actions[0].action == "MODIFY_SL"
    assert out.actions[0].new_sl == 104.0
    assert trade_repo.get_trade(tid).stop_loss == 104.0


def test_manage_updates_high_water_mark() -> None:
    """A2: cada run registra el nuevo máximo favorable y no lo baja."""
    from infrastructure.persistence.sqlite import run_repo, trade_repo

    run_repo.start_run("pm-mfe")
    tid = trade_repo.insert_trade(
        "pm-mfe", "US500", "BUY", 0.5, 100.0, 98.0, None,
        is_runner=True, status="OPEN", broker_order_id="BR-MFE",
    )
    broker = MagicMock()

    # Sube a 103 → high-water = 103
    stage_manage(
        ManageInput(open_trades=[trade_repo.get_trade(tid).to_dict()],
                    current_prices={"US500": 103.0}),
        "pm-mfe", broker,
    )
    assert trade_repo.get_trade(tid).max_favorable_price == 103.0

    # Retrocede a 101 → high-water NO baja (sigue 103)
    stage_manage(
        ManageInput(open_trades=[trade_repo.get_trade(tid).to_dict()],
                    current_prices={"US500": 101.0}),
        "pm-mfe", broker,
    )
    assert trade_repo.get_trade(tid).max_favorable_price == 103.0
