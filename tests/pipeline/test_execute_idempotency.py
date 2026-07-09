"""Tests de idempotencia de stage_execute (W8/W9/W10 de la auditoría).

Garantías cubiertas:
- Re-ejecutar la misma decisión NO duplica órdenes en el broker.
- Si el broker acepta pero la confirmación en DB falla, ningún re-run reenvía.
- Validaciones fallidas no dejan registros fantasma (decisions ni trades).
- Un REJECTED libera el client_order_id para reintentar.
- reconcile_pending_trades expira PENDING viejos y advierte sobre los de hoy.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from domain.strategy.box import Box
from domain.strategy.budget import DailyOrderBudget
from domain.strategy.decision import Action, RiskMode
from infrastructure.config.settings import reset_settings_cache
from infrastructure.persistence.sqlite import db, run_repo, trade_repo
from pipeline.contracts import DecisionContract, ExecuteInput
from pipeline.stages import s6_execute
from pipeline.stages.s6_execute import reconcile_pending_trades, stage_execute

RUN_ID = "idem-run"
TRADE_DATE = "2026-06-12"


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    reset_settings_cache()
    db.reset_db(db_path)
    db.init_db(db_path)
    run_repo.start_run(RUN_ID)
    yield db_path
    db.reset_db(db_path)


def _long_input() -> ExecuteInput:
    box = Box(high=100.5, low=100.0, amplitude_pct=0.5, n_candles=10)
    decision = DecisionContract(
        symbol="US500", action=Action.LONG, risk=RiskMode.COMPLETO,
        confidence=80, reasons=["test"], key_levels={"high": 100.5, "low": 100.0},
        signal={"state": "ABOVE"}, team_consensus="unánime",
    )
    return ExecuteInput(
        decision=decision, symbol="US500", box=box, base_volume=1.0,
        min_rr=1.0, trade_date=TRADE_DATE,
    )


def _broker(order_id: str = "BR-1") -> MagicMock:
    broker = MagicMock()
    broker.place_order.return_value = order_id
    return broker


def test_rerun_does_not_duplicate_broker_calls():
    broker = _broker()
    out1 = stage_execute(_long_input(), RUN_ID, DailyOrderBudget(max_orders=10), broker)
    assert len(out1.orders) == 2
    assert broker.place_order.call_count == 2

    # mismo día, misma decisión (re-run tras crash): no debe reenviar nada
    out2 = stage_execute(_long_input(), RUN_ID, DailyOrderBudget(max_orders=10), broker)
    assert len(out2.orders) == 0
    assert len(out2.skipped) == 2
    assert broker.place_order.call_count == 2  # sin llamadas nuevas
    assert len(trade_repo.list_trades(symbol="US500")) == 2  # sin filas nuevas


def test_db_failure_after_place_blocks_resend(monkeypatch):
    """Broker acepta, update a OPEN falla → PENDING bloquea el re-run."""
    broker = _broker()
    monkeypatch.setattr(
        s6_execute, "_confirm_open",
        MagicMock(side_effect=sqlite3.OperationalError("disco lleno")),
    )
    out1 = stage_execute(_long_input(), RUN_ID, DailyOrderBudget(max_orders=10), broker)
    assert len(out1.orders) == 0
    assert broker.place_order.call_count == 2
    assert any("sin confirmar" in e for e in out1.errors)
    # ambos trades quedaron PENDING con su coid
    pending = trade_repo.list_pending_trades()
    assert len(pending) == 2

    # re-run con la DB sana: NO debe llamar al broker de nuevo
    monkeypatch.undo()
    broker2 = _broker("BR-2")
    out2 = stage_execute(_long_input(), RUN_ID, DailyOrderBudget(max_orders=10), broker2)
    assert broker2.place_order.call_count == 0
    assert len(out2.skipped) == 2


def test_failed_validation_leaves_no_rows():
    """Confianza insuficiente → ni decisión ni trades en DB."""
    inp = _long_input()
    inp.decision.confidence = 10
    import os
    os.environ["MIN_CONFIDENCE"] = "60"
    reset_settings_cache()
    try:
        broker = _broker()
        out = stage_execute(inp, RUN_ID, DailyOrderBudget(max_orders=10), broker)
        assert out.orders == []
        assert out.decision_id == 0
        broker.place_order.assert_not_called()
        with db.get_db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    finally:
        os.environ.pop("MIN_CONFIDENCE", None)
        reset_settings_cache()


def test_budget_exhausted_leaves_no_rows():
    broker = _broker()
    out = stage_execute(_long_input(), RUN_ID, DailyOrderBudget(max_orders=0), broker)
    assert out.orders == []
    assert out.decision_id == 0
    broker.place_order.assert_not_called()
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0


def test_rejected_order_can_be_retried():
    """Si el broker rechaza, el coid queda libre y el re-run reintenta."""
    broker_fail = MagicMock()
    broker_fail.place_order.side_effect = RuntimeError("rechazada")
    out1 = stage_execute(_long_input(), RUN_ID, DailyOrderBudget(max_orders=10), broker_fail)
    assert len(out1.orders) == 0
    assert len(out1.errors) == 2
    rejected = trade_repo.list_trades(status="REJECTED")
    assert len(rejected) == 2

    broker_ok = _broker("BR-RETRY")
    out2 = stage_execute(_long_input(), RUN_ID, DailyOrderBudget(max_orders=10), broker_ok)
    assert len(out2.orders) == 2
    assert broker_ok.place_order.call_count == 2


def test_reconcile_expires_old_pending_and_warns_today():
    # PENDING de ayer (ts_open viejo se fuerza con SQL directo)
    old_id = trade_repo.insert_trade(
        RUN_ID, "US500", "BUY", 0.5, 5000.0, None, None, is_runner=False,
        status="PENDING", client_order_id="2026-06-11:US500:BUY:P",
    )
    with db.get_db() as conn:
        conn.execute(
            "UPDATE trades SET ts_open = '2026-06-11T12:00:00+00:00' WHERE id = ?",
            (old_id,),
        )
    # PENDING de hoy
    today_id = trade_repo.insert_trade(
        RUN_ID, "US100", "SELL", 0.5, 20000.0, None, None, is_runner=False,
        status="PENDING", client_order_id="hoy:US100:SELL:P",
    )

    warnings = reconcile_pending_trades(RUN_ID)

    assert trade_repo.get_trade(old_id).status == "EXPIRED"
    assert trade_repo.get_trade(today_id).status == "PENDING"
    assert len(warnings) == 1
    assert "no se reenviará" in warnings[0]


def test_reconcile_expires_old_dry_open_trades():
    """OPEN simulados (DRY-*) de días previos → EXPIRED; los reales y los de
    hoy no se tocan."""
    dry_old = trade_repo.insert_trade(
        RUN_ID, "US500", "BUY", 0.5, 5000.0, None, None, is_runner=False,
        status="OPEN", broker_order_id="DRY-1", client_order_id="2026-06-11:US500:BUY:P",
    )
    real_old = trade_repo.insert_trade(
        RUN_ID, "US500", "BUY", 0.5, 5000.0, None, None, is_runner=True,
        status="OPEN", broker_order_id="123456", client_order_id="2026-06-11:US500:BUY:R",
    )
    dry_today = trade_repo.insert_trade(
        RUN_ID, "US100", "SELL", 0.5, 20000.0, None, None, is_runner=False,
        status="OPEN", broker_order_id="DRY-2", client_order_id="hoy:US100:SELL:P",
    )
    with db.get_db() as conn:
        conn.execute(
            "UPDATE trades SET ts_open = '2026-06-11T12:00:00+00:00' WHERE id IN (?, ?)",
            (dry_old, real_old),
        )

    reconcile_pending_trades(RUN_ID)

    assert trade_repo.get_trade(dry_old).status == "EXPIRED"
    assert trade_repo.get_trade(real_old).status == "OPEN"
    assert trade_repo.get_trade(dry_today).status == "OPEN"
