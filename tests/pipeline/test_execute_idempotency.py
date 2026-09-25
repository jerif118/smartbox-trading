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


def test_failed_validation_records_decision_but_no_trades():
    """Confianza insuficiente → la decisión SE REGISTRA (como REJECTED), pero
    no hay trades ni llamadas al broker.

    Registrar los rechazos es lo que permite medir después cuántos setups se
    frenaron y si se frenaron bien; lo que nunca debe quedar es una orden.
    """
    inp = _long_input()
    inp.decision.confidence = 10
    import os
    os.environ["MIN_CONFIDENCE"] = "60"
    reset_settings_cache()
    try:
        broker = _broker()
        out = stage_execute(inp, RUN_ID, DailyOrderBudget(max_orders=10), broker)
        assert out.orders == []
        assert out.decision_id > 0
        broker.place_order.assert_not_called()
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT execution_status, reasons FROM decisions"
            ).fetchone()
            assert row["execution_status"] == "REJECTED"
            assert "confidence 10 < min 60" in row["reasons"]
            assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    finally:
        os.environ.pop("MIN_CONFIDENCE", None)
        reset_settings_cache()


def test_budget_exhausted_records_decision_but_no_trades():
    broker = _broker()
    out = stage_execute(_long_input(), RUN_ID, DailyOrderBudget(max_orders=0), broker)
    assert out.orders == []
    assert out.decision_id > 0
    broker.place_order.assert_not_called()
    with db.get_db() as conn:
        row = conn.execute("SELECT execution_status, reasons FROM decisions").fetchone()
        assert row["execution_status"] == "REJECTED"
        assert "daily budget exhausted" in row["reasons"]
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_no_operar_is_recorded():
    """Un NO_OPERAR deja fila propia: es la mitad de la muestra a analizar."""
    inp = _long_input()
    inp.decision.action = Action.NO_OPERAR
    inp.decision.reasons = ["confluence 45 < 55", "RSI extremo contrario"]
    broker = _broker()

    out = stage_execute(inp, RUN_ID, DailyOrderBudget(max_orders=10), broker)

    assert out.orders == []
    assert out.errors == []
    assert out.decision_id > 0
    broker.place_order.assert_not_called()
    with db.get_db() as conn:
        row = conn.execute("SELECT action, execution_status, reasons FROM decisions").fetchone()
        assert row["action"] == "NO_OPERAR"
        assert row["execution_status"] == "NO_OPERAR"
        assert "confluence 45 < 55" in row["reasons"]


def test_duplicates_do_not_consume_daily_budget():
    """Un re-run no debe quemar cupo con órdenes que ya están activas.

    Antes, try_consume() corría antes del chequeo de idempotencia: el re-run
    de US500 consumía 2 cupos sin enviar nada y dejaba a US100 sin budget.
    """
    broker = _broker()
    budget = DailyOrderBudget(max_orders=4)
    out1 = stage_execute(_long_input(), RUN_ID, budget, broker)
    assert len(out1.orders) == 2
    assert budget.used == 2

    # Re-run del mismo día: budget sembrado desde DB, US500 ya está enviada.
    budget2 = DailyOrderBudget(max_orders=4, used=trade_repo.count_orders_today())
    out_dup = stage_execute(_long_input(), RUN_ID, budget2, broker)
    assert len(out_dup.orders) == 0
    assert len(out_dup.skipped) == 2
    assert budget2.used == 2  # no consumió por los duplicados
    # la decisión del re-run queda registrada, marcada como duplicada
    assert out_dup.decision_id > 0
    with db.get_db() as conn:
        statuses = [
            r["execution_status"]
            for r in conn.execute("SELECT execution_status FROM decisions ORDER BY id").fetchall()
        ]
    assert statuses == ["EXECUTED", "SKIPPED_DUPLICATE"]

    # US100 todavía tiene sus 2 cupos.
    inp = _long_input()
    inp.decision.symbol = "US100"
    inp.symbol = "US100"
    out_other = stage_execute(inp, RUN_ID, budget2, broker)
    assert len(out_other.orders) == 2
    assert budget2.used == 4


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


def _feed(rows: list[tuple[int, float, float, float]]) -> MagicMock:
    """Feed falso de velas. rows = [(time, high, low, close), ...]"""
    import pandas as pd

    feed = MagicMock()
    feed.get_candles.return_value = pd.DataFrame(
        rows, columns=["time", "high", "low", "close"]
    )
    return feed


def test_reconcile_closes_trade_that_hit_stop_loss():
    """El caso del 12-ago: el broker cerró por SL y la DB seguía en OPEN."""
    trade_id = trade_repo.insert_trade(
        RUN_ID, "US500", "SELL", 0.25, 7760.60, 7793.60, 7727.60,
        is_runner=False, status="OPEN", broker_order_id="222676041",
        client_order_id="2026-08-12:US500:SELL:P",
    )
    # Precio sube y toca el stop.
    feed = _feed([(1, 7770.0, 7755.0, 7765.0), (2, 7795.0, 7768.0, 7790.0)])

    closed = s6_execute.reconcile_closed_trades(RUN_ID, market_data=feed)

    assert closed == 1
    trade = trade_repo.get_trade(trade_id)
    assert trade.status == "CLOSED_SL"
    assert trade.exit_price == 7793.60
    assert trade.r_multiple == -1.0
    assert trade.pnl < 0
    # y ahora el freno de pérdida diaria por fin ve la pérdida
    assert trade_repo.realized_pnl_today() < 0


def test_reconcile_leaves_live_trade_open():
    trade_id = trade_repo.insert_trade(
        RUN_ID, "US500", "BUY", 0.25, 100.0, 98.0, 102.0,
        is_runner=False, status="OPEN", broker_order_id="999",
        client_order_id="hoy:US500:BUY:P",
    )
    feed = _feed([(1, 100.8, 99.5, 100.2), (2, 101.0, 100.0, 100.5)])

    assert s6_execute.reconcile_closed_trades(RUN_ID, market_data=feed) == 0
    assert trade_repo.get_trade(trade_id).status == "OPEN"


def test_reconcile_prices_a_trade_the_broker_closed_outside_the_bot():
    """s7 marcó CLOSED_MANUAL (INVALID_ORDER) pero el precio no tocó SL ni TP:
    se valora al último cierre para que no quede sin P&L para siempre."""
    trade_id = trade_repo.insert_trade(
        RUN_ID, "US500", "BUY", 1.0, 100.0, 98.0, 104.0,
        is_runner=False, status="OPEN", broker_order_id="777",
        client_order_id="hoy:US500:BUY:P",
    )
    trade_repo.update_status(trade_id, "CLOSED_MANUAL")
    feed = _feed([(1, 100.5, 99.5, 100.2), (2, 101.5, 100.0, 101.0)])

    assert s6_execute.reconcile_closed_trades(RUN_ID, market_data=feed) == 1
    trade = trade_repo.get_trade(trade_id)
    assert trade.status == "CLOSED_MANUAL"
    assert trade.exit_price == 101.0
    assert trade.r_multiple == 0.5  # +1 punto sobre 2 de riesgo
    assert trade.pnl is not None


def test_reconcile_ignores_simulated_trades():
    trade_id = trade_repo.insert_trade(
        RUN_ID, "US500", "BUY", 0.25, 100.0, 98.0, 102.0,
        is_runner=False, status="OPEN", broker_order_id="DRY-123",
        client_order_id="hoy:US500:BUY:P",
    )
    feed = _feed([(1, 105.0, 97.0, 104.0)])

    assert s6_execute.reconcile_closed_trades(RUN_ID, market_data=feed) == 0
    assert trade_repo.get_trade(trade_id).status == "OPEN"


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
