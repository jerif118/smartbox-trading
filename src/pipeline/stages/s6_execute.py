"""
Stage 6: Execute — envía órdenes y persiste en SQLite.

Reglas (preservadas del original):
- Cada símbolo manda 2 órdenes (primary + runner)
- MAX_ORDERS_PER_DAY hard cap
- Validación de dirección vs breakout
- R:R mínimo
- Coherencia de niveles
"""

from __future__ import annotations

from domain.errors import (
    CoherenceError,
    InsufficientRRError,
)
from domain.strategy.budget import DailyOrderBudget
from domain.strategy.decision import Action
from domain.strategy.order_spec import build_execution_plan
from domain.strategy.position_sizer import size_position
from infrastructure.broker.simplefx.adapter import SimpleFXAdapter
from infrastructure.config.settings import get_settings
from infrastructure.persistence.sqlite import (
    decision_repo,
    event_repo,
    trade_repo,
)
from pipeline.contracts import (
    ExecuteInput,
    ExecuteOutput,
    OrderContract,
)


def stage_execute(
    input_data: ExecuteInput,
    run_id: str,
    budget: DailyOrderBudget,
    broker: SimpleFXAdapter,
) -> ExecuteOutput:
    """Ejecuta una decisión: inserta en DB, envía al broker, actualiza estado."""
    settings = get_settings()
    errors: list[str] = []

    decision = input_data.decision

    # ── Validaciones (preservan reglas originales) ─────────────────────
    if decision.action == Action.NO_OPERAR:
        event_repo.log_event(
            run_id=run_id,
            agent="decision_maker",
            event_type="DECISION",
            payload={"symbol": decision.symbol, "action": "NO_OPERAR", "skip": True},
        )
        # NO_OPERAR es un resultado normal de la estrategia, no un error
        return ExecuteOutput(decision_id=0, orders=[], errors=[])

    # Regla #18
    if decision.confidence < settings.min_confidence:
        errors.append(f"confidence {decision.confidence} < min {settings.min_confidence}")
        return ExecuteOutput(decision_id=0, orders=[], errors=errors)

    # Freno duro de pérdida diaria (no depende del veto del LLM)
    pnl_today = trade_repo.realized_pnl_today()
    if pnl_today <= -settings.max_daily_loss:
        errors.append(
            f"daily loss limit: pnl hoy {pnl_today:.2f} <= -{settings.max_daily_loss:.2f}"
        )
        return ExecuteOutput(decision_id=0, orders=[], errors=errors)

    # Regla #1: validar box
    try:
        input_data.box.validate()
    except Exception as e:
        errors.append(f"box invalid: {e}")
        return ExecuteOutput(decision_id=0, orders=[], errors=errors)

    # ── Persistir decisión ─────────────────────────────────────────────
    decision_id = decision_repo.insert_decision(
        run_id=run_id,
        symbol=decision.symbol,
        action=decision.action.value,
        risk=decision.risk.value,
        confidence=decision.confidence,
        reasons=list(decision.reasons),
        team_consensus=decision.team_consensus,
        key_levels=decision.key_levels,
        signal=decision.signal,
    )

    # ── Construir plan de ejecución ────────────────────────────────────
    sized = size_position(input_data.base_volume, decision.risk)
    try:
        plan = build_execution_plan(
            symbol=decision.symbol,
            box=input_data.box,
            sized=sized,
            action=decision.action,
        )
        plan.validate(min_rr=input_data.min_rr)
    except (InsufficientRRError, CoherenceError) as e:
        errors.append(f"plan invalid: {e}")
        return ExecuteOutput(decision_id=decision_id, orders=[], errors=errors)

    # ── Regla #11: budget ──────────────────────────────────────────────
    if not budget.try_consume(2):
        errors.append("daily budget exhausted")
        return ExecuteOutput(decision_id=decision_id, orders=[], errors=errors)

    # ── Enviar órdenes ─────────────────────────────────────────────────
    orders: list[OrderContract] = []
    for order_spec in plan.orders:
        # Persistir ANTES de enviar (source of truth)
        trade_id = trade_repo.insert_trade(
            run_id=run_id,
            decision_id=decision_id,
            symbol=order_spec.symbol,
            side=order_spec.side,
            volume=order_spec.volume,
            entry_price=order_spec.entry_price,
            stop_loss=order_spec.stop_loss,
            take_profit=order_spec.take_profit,
            is_runner=order_spec.is_runner,
            status="PENDING",
        )
        event_repo.log_event(
            run_id=run_id,
            agent="decision_maker",
            event_type="DECISION",
            payload={"trade_id": trade_id, "side": order_spec.side, "volume": order_spec.volume},
        )

        # Enviar al broker
        try:
            broker_order_id = broker.place_order(
                symbol=order_spec.symbol,
                side=order_spec.side,
                volume=order_spec.volume,
                entry_price=order_spec.entry_price,
                stop_loss=order_spec.stop_loss,
                take_profit=order_spec.take_profit,
            )
            trade_repo.update_status(trade_id, "OPEN", broker_order_id=broker_order_id)
            event_repo.log_event(
                run_id=run_id,
                agent="decision_maker",
                event_type="TOOL_CALL",
                payload={"tool": "place_order", "result": broker_order_id},
            )
            orders.append(
                OrderContract(
                    symbol=order_spec.symbol,
                    side=order_spec.side,
                    volume=order_spec.volume,
                    entry_price=order_spec.entry_price,
                    stop_loss=order_spec.stop_loss,
                    take_profit=order_spec.take_profit,
                    is_runner=order_spec.is_runner,
                    decision_id=decision_id,
                    broker_order_id=broker_order_id,
                )
            )
        except Exception as e:
            trade_repo.update_status(trade_id, "REJECTED")
            errors.append(f"broker error: {e}")
            event_repo.log_event(
                run_id=run_id,
                agent="decision_maker",
                event_type="TOOL_RESULT",
                payload={"tool": "place_order", "error": str(e)},
            )

    return ExecuteOutput(decision_id=decision_id, orders=orders, errors=errors)
