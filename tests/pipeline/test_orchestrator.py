"""Tests end-to-end del orchestrator (con stages y adapters mockeados).

Cubre las garantías de robustez de la Fase 4:
- happy path con métricas por stage y run cerrado en success
- fallo del crew → run failed (nunca queda 'running')
- SystemExit a mitad de run → finally cierra el run
- runs zombi de corridas anteriores se reparan al arrancar
- budget sembrado con órdenes de hoy corta el run antes del crew
- fin de semana → skipped
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

from domain.strategy.box import Box
from domain.strategy.decision import Action, RiskMode
from infrastructure.config.settings import reset_settings_cache
from infrastructure.persistence.sqlite import db, run_repo, stage_metrics_repo, trade_repo
from pipeline import orchestrator
from pipeline.contracts import (
    AnalyzeOutput,
    ContextOutput,
    DecisionContract,
    ExecuteOutput,
    IngestOutput,
    OrderContract,
    PreprocessOutput,
    SignalOutput,
)

WEEKDAY = "2026-06-11"  # jueves
SATURDAY = "2026-06-13"


@pytest.fixture(autouse=True)
def temp_env(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("SYMBOLS", "US500")
    monkeypatch.setenv("PRIMARY_SYMBOL", "US500")
    monkeypatch.setenv("DRY_RUN", "true")
    reset_settings_cache()
    db.reset_db(db_path)
    db.init_db(db_path)
    yield db_path
    db.reset_db(db_path)
    reset_settings_cache()


def _mock_stages(monkeypatch, analyze_side_effect=None):
    """Mockea todos los stages y adapters del orchestrator para un happy path."""
    box = Box(high=100.5, low=100.0, amplitude_pct=0.5, n_candles=10)
    df = pd.DataFrame(
        {
            "time": [1500, 1800, 2100],
            "open": [100.0] * 3,
            "high": [101.0] * 3,
            "low": [99.5] * 3,
            "close": [100.8] * 3,
            "volume": [100] * 3,
        }
    )

    monkeypatch.setattr(orchestrator, "_today_str", lambda tz=None: WEEKDAY)
    monkeypatch.setattr(orchestrator, "box_window_unix", lambda *a: (0, 1000))
    monkeypatch.setattr(orchestrator, "SimpleFXAdapter", MagicMock())
    monkeypatch.setattr(orchestrator, "CapitalAdapter", MagicMock())
    # Sin red real al feed SimpleFX; la caja de ejecución la fija el mock de s2.
    monkeypatch.setattr(orchestrator, "_fetch_simplefx_candles", lambda *a, **k: None)
    monkeypatch.setattr(
        orchestrator,
        "stage_ingest",
        lambda inp: IngestOutput(symbol=inp.symbol, df_candles=df, n_candles=len(df)),
    )
    monkeypatch.setattr(
        orchestrator,
        "stage_preprocess",
        lambda inp, df_c, df_s=None: PreprocessOutput(
            symbol=inp.symbol,
            box=box,
            box_simple=box,
            rsi_last=55.0,
            volume_profile=None,
            box_candles=[],
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "stage_signal",
        lambda inp: SignalOutput(
            symbol=inp.symbol,
            has_breakout=True,
            breakout_state="ABOVE",
            candle_close=100.8,
            signal_time="2026-06-11T15:00:00",
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "stage_context",
        lambda inp: ContextOutput(macro_risk="LOW", high_impact_events=[]),
    )

    decision = DecisionContract(
        symbol="US500",
        action=Action.LONG,
        risk=RiskMode.COMPLETO,
        confidence=80,
        reasons=["test"],
        key_levels={"high": 100.5, "low": 100.0},
        signal={"state": "ABOVE"},
        team_consensus="unánime",
    )
    if analyze_side_effect is not None:
        analyze_mock = MagicMock(side_effect=analyze_side_effect)
    else:
        analyze_mock = MagicMock(return_value=AnalyzeOutput(decisions=[decision]))
    monkeypatch.setattr(orchestrator, "stage_analyze", analyze_mock)

    exec_mock = MagicMock(
        return_value=ExecuteOutput(
            decision_id=1,
            orders=[
                OrderContract(
                    symbol="US500",
                    side="BUY",
                    volume=0.5,
                    entry_price=100.5,
                    stop_loss=100.0,
                    take_profit=101.0,
                    is_runner=False,
                ),
                OrderContract(
                    symbol="US500",
                    side="BUY",
                    volume=0.5,
                    entry_price=100.5,
                    stop_loss=100.0,
                    take_profit=None,
                    is_runner=True,
                ),
            ],
            errors=[],
        )
    )
    monkeypatch.setattr(orchestrator, "stage_execute", exec_mock)
    return analyze_mock, exec_mock


def test_happy_path_success_with_metrics(monkeypatch):
    _mock_stages(monkeypatch)
    result = orchestrator.run_pipeline()

    assert result.status == "success"
    assert result.orders_sent == 2
    assert result.decisions_count == 1

    run = run_repo.get_run(result.run_id)
    assert run.status == "success"
    assert run.finished_at is not None

    stages = {m["stage"]: m["status"] for m in stage_metrics_repo.list_stage_metrics(result.run_id)}
    assert stages.get("s1_ingest:US500") == "ok"
    assert stages.get("s5_analyze") == "ok"
    assert stages.get("s6_execute:US500") == "ok"


def test_analyze_failure_marks_run_failed(monkeypatch):
    _mock_stages(monkeypatch, analyze_side_effect=RuntimeError("crew explotó"))
    result = orchestrator.run_pipeline()

    assert result.status == "failed"
    run = run_repo.get_run(result.run_id)
    assert run.status == "failed"
    metrics = {
        m["stage"]: m["status"] for m in stage_metrics_repo.list_stage_metrics(result.run_id)
    }
    assert metrics.get("s5_analyze") == "error"


def test_system_exit_does_not_leave_running(monkeypatch):
    _mock_stages(monkeypatch)
    monkeypatch.setattr(
        orchestrator, "reconcile_pending_trades", MagicMock(side_effect=SystemExit(1))
    )
    with pytest.raises(SystemExit):
        orchestrator.run_pipeline()

    runs = run_repo.list_runs(limit=5)
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert "aborted" in (runs[0].error or "")


def test_zombie_runs_repaired_on_start(monkeypatch):
    run_repo.start_run("zombie-anterior")  # quedó 'running' de otra corrida
    _mock_stages(monkeypatch)
    result = orchestrator.run_pipeline()

    assert result.status == "success"
    zombie = run_repo.get_run("zombie-anterior")
    assert zombie.status == "failed"
    assert "stale" in zombie.error


def test_budget_seeded_from_today_blocks_analyze(monkeypatch):
    monkeypatch.setenv("MAX_ORDERS_PER_DAY", "4")
    reset_settings_cache()
    analyze_mock, exec_mock = _mock_stages(monkeypatch)
    # 4 órdenes ya enviadas hoy (otro run): budget agotado antes de empezar
    run_repo.start_run("run-previo")
    run_repo.finish_run("run-previo", "success")
    for i in range(4):
        trade_repo.insert_trade(
            "run-previo",
            "US500",
            "BUY",
            0.5,
            5000.0,
            None,
            None,
            is_runner=bool(i % 2),
            status="OPEN",
        )

    result = orchestrator.run_pipeline()

    assert result.status == "success"
    assert result.orders_sent == 0
    analyze_mock.assert_not_called()  # ni siquiera gasta tokens del crew
    exec_mock.assert_not_called()
    run = run_repo.get_run(result.run_id)
    assert "budget" in (run.error or "")


def test_weekend_skips(monkeypatch):
    _mock_stages(monkeypatch)
    monkeypatch.setattr(orchestrator, "_today_str", lambda tz=None: SATURDAY)
    result = orchestrator.run_pipeline()

    assert result.status == "skipped"
    run = run_repo.get_run(result.run_id)
    assert run.status == "skipped"


def test_high_amplitude_symbol_skips_without_failed_run(monkeypatch):
    analyze_mock, exec_mock = _mock_stages(monkeypatch)
    wide_box = Box(high=101.03, low=100.0, amplitude_pct=1.03, n_candles=10)
    signal_mock = MagicMock()

    monkeypatch.setattr(
        orchestrator,
        "stage_preprocess",
        lambda inp, df_c, df_s=None: PreprocessOutput(
            symbol=inp.symbol,
            box=wide_box,
            box_simple=None,
            rsi_last=55.0,
            volume_profile=None,
            box_candles=[],
        ),
    )
    monkeypatch.setattr(orchestrator, "stage_signal", signal_mock)

    result = orchestrator.run_pipeline()

    assert result.status == "success"
    assert result.errors == []
    signal_mock.assert_not_called()
    analyze_mock.assert_not_called()
    exec_mock.assert_not_called()
    run = run_repo.get_run(result.run_id)
    assert run.status == "success"


def test_symbol_error_without_signals_marks_failed(monkeypatch):
    _mock_stages(monkeypatch)
    monkeypatch.setattr(
        orchestrator,
        "stage_ingest",
        MagicMock(side_effect=ValueError("API caída")),
    )
    result = orchestrator.run_pipeline()

    assert result.status == "failed"
    assert any("API caída" in e for e in result.errors)
    run = run_repo.get_run(result.run_id)
    assert run.status == "failed"


def test_primary_policy_can_reduce_instead_of_blocking() -> None:
    signals = [{"symbol": "US100", "is_primary": False}]
    active, confirmed = orchestrator.apply_primary_policy(signals, "US500", "size_reduction")
    assert confirmed is False
    assert active[0]["primary_confirmed"] is False

    blocked, confirmed = orchestrator.apply_primary_policy(signals, "US500", "required")
    assert confirmed is False
    assert blocked == []


def test_primary_policy_detects_primary_breakout() -> None:
    signals = [
        {"symbol": "US500", "is_primary": True},
        {"symbol": "US100", "is_primary": False},
    ]
    active, confirmed = orchestrator.apply_primary_policy(signals, "US500", "size_reduction")
    assert confirmed is True
    assert all(item["primary_confirmed"] for item in active)


def test_translate_suggested_levels_shifts_by_box_offset():
    """MODIFY: los niveles sugeridos (Capital) se trasladan al espacio SimpleFX."""
    from types import SimpleNamespace
    cap = Box(high=7513.8, low=7468.1, amplitude_pct=0.61, n_candles=24)
    sfx = Box(high=7549.6, low=7503.9, amplitude_pct=0.61, n_candles=24)  # ~+35.8
    offset = round(sfx.mid - cap.mid, 2)
    decision = SimpleNamespace(
        action=Action.LONG,
        key_levels={"suggested_stop_loss": 7468.1, "suggested_take_profit": 7559.5},
    )
    orchestrator._translate_suggested_levels(decision, cap, sfx)
    assert decision.key_levels["suggested_stop_loss"] == round(7468.1 + offset, 1)
    assert decision.key_levels["suggested_take_profit"] == round(7559.5 + offset, 1)


def test_translate_suggested_levels_noop_when_same_box():
    """Sin caja SimpleFX (fallback) offset=0 → no traslada."""
    from types import SimpleNamespace
    cap = Box(high=7513.8, low=7468.1, amplitude_pct=0.61, n_candles=24)
    decision = SimpleNamespace(
        action=Action.LONG,
        key_levels={"suggested_stop_loss": 7468.1, "suggested_take_profit": None},
    )
    orchestrator._translate_suggested_levels(decision, cap, cap)
    assert decision.key_levels["suggested_stop_loss"] == 7468.1
