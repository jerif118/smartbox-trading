"""
Stage 6: Execute — envía órdenes y persiste en SQLite, con idempotencia.

Reglas (preservadas del original):
- Cada símbolo manda 2 órdenes (primary + runner)
- MAX_ORDERS_PER_DAY hard cap
- Validación de dirección vs breakout
- R:R mínimo
- Coherencia de niveles

Orden de operaciones (anti-duplicados, anti-fantasmas):
1. TODAS las validaciones, sin escribir nada en DB.
2. Persistir la decisión (ya validada).
3. Por cada orden: chequear client_order_id activo (idempotencia) →
   insertar trade PENDING → enviar al broker → actualizar a OPEN.
   - Si el broker rechaza: REJECTED (el coid queda libre para reintentar).
   - Si el update a OPEN falla tras enviar: el trade queda PENDING con su
     coid, lo que BLOQUEA cualquier reenvío en re-runs (log CRITICAL con
     el broker_order_id para reconciliación manual).
"""

from __future__ import annotations

import sqlite3

from domain.errors import (
    CoherenceError,
    InsufficientRRError,
    InvalidBoxError,
)
from domain.strategy.budget import DailyOrderBudget
from domain.strategy.decision import Action
from domain.strategy.order_spec import build_execution_plan, make_client_order_id
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
from utils.logger import get_logger
from utils.retry import retry

log = get_logger(__name__)


@retry(max_retries=3, initial_delay=0.5, exceptions=(sqlite3.Error,))
def _confirm_open(trade_id: int, broker_order_id: str) -> None:
    trade_repo.update_status(trade_id, "OPEN", broker_order_id=broker_order_id)


def reconcile_pending_trades(run_id: str) -> list[str]:
    """Repara trades PENDING huérfanos de corridas anteriores.

    - PENDING de días previos → EXPIRED (la oportunidad ya pasó).
    - PENDING de hoy → se deja como está y se advierte: pudo haberse enviado
      al broker sin confirmación. Su client_order_id sigue bloqueando
      reenvíos; la reconciliación contra el broker es manual (SimpleFX no
      expone listado de órdenes).
    - OPEN simulados (broker_order_id DRY-*) de días previos → EXPIRED:
      nunca tuvieron orden real que cerrar y el Position Manager los
      gestionaría para siempre.
    Retorna mensajes de advertencia.
    """
    from datetime import UTC, datetime

    today = datetime.now(UTC).date().isoformat()
    warnings: list[str] = []

    for trade in trade_repo.list_open_trades():
        trade_day = (trade.ts_open or "")[:10]
        is_dry = str(trade.broker_order_id or "").startswith("DRY-")
        if is_dry and trade_day < today:
            trade_repo.update_status(trade.id, "EXPIRED")
            log.info(
                "Trade OPEN simulado %s (%s) del %s → EXPIRED",
                trade.id, trade.broker_order_id, trade_day,
            )
            event_repo.log_event(
                run_id=run_id,
                agent="decision_maker",
                event_type="SYSTEM",
                payload={"reconcile": "dry_open_expired", "trade_id": trade.id},
            )

    for trade in trade_repo.list_pending_trades():
        trade_day = (trade.ts_open or "")[:10]
        if trade_day < today:
            trade_repo.update_status(trade.id, "EXPIRED")
            log.info("Trade PENDING %s del %s → EXPIRED", trade.id, trade_day)
        else:
            msg = (
                f"trade {trade.id} ({trade.symbol} {trade.side}) PENDING de hoy: "
                "posible orden enviada sin confirmar; no se reenviará "
                f"(client_order_id={trade.client_order_id})"
            )
            warnings.append(msg)
            log.warning("%s", msg)
            event_repo.log_event(
                run_id=run_id,
                agent="decision_maker",
                event_type="SYSTEM",
                payload={"reconcile": "pending_today", "trade_id": trade.id},
            )
    return warnings


def stage_execute(
    input_data: ExecuteInput,
    run_id: str,
    budget: DailyOrderBudget,
    broker: SimpleFXAdapter,
) -> ExecuteOutput:
    """Ejecuta una decisión: valida, persiste, envía al broker con idempotencia."""
    settings = get_settings()
    errors: list[str] = []
    skipped: list[str] = []

    decision = input_data.decision

    # ── 1. Validaciones — ninguna escribe en DB ────────────────────────
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
    except InvalidBoxError as e:
        errors.append(f"box invalid: {e}")
        return ExecuteOutput(decision_id=0, orders=[], errors=errors)

    # Reglas #9/#13-15/#17: plan de ejecución coherente
    sized = size_position(input_data.base_volume, decision.risk)
    try:
        plan = build_execution_plan(
            symbol=decision.symbol,
            box=input_data.box,
            sized=sized,
            action=decision.action,
        )
        plan.validate(min_rr=input_data.min_rr)
    except (InsufficientRRError, CoherenceError, ValueError) as e:
        errors.append(f"plan invalid: {e}")
        return ExecuteOutput(decision_id=0, orders=[], errors=errors)

    # Regla #11: budget diario
    if not budget.try_consume(2):
        errors.append("daily budget exhausted")
        return ExecuteOutput(decision_id=0, orders=[], errors=errors)

    # ── 2. Persistir decisión (ya validada) ────────────────────────────
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

    # ── 3. Enviar órdenes con idempotencia ─────────────────────────────
    orders: list[OrderContract] = []
    for order_spec in plan.orders:
        coid = make_client_order_id(
            input_data.trade_date, order_spec.symbol, order_spec.side, order_spec.is_runner
        )

        existing = trade_repo.find_active_by_client_order_id(coid)
        if existing is not None:
            skipped.append(coid)
            log.warning(
                "Orden %s ya activa (trade %s, status %s) — no se reenvía",
                coid, existing.id, existing.status,
            )
            event_repo.log_event(
                run_id=run_id,
                agent="decision_maker",
                event_type="SYSTEM",
                payload={"event": "ORDER_SKIPPED_DUPLICATE", "client_order_id": coid,
                         "existing_trade_id": existing.id},
            )
            continue

        try:
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
                client_order_id=coid,
            )
        except sqlite3.IntegrityError:
            # carrera con otro insert del mismo coid — tratar como duplicado
            skipped.append(coid)
            log.warning("Orden %s duplicada (índice único) — no se reenvía", coid)
            continue

        event_repo.log_event(
            run_id=run_id,
            agent="decision_maker",
            event_type="DECISION",
            payload={"trade_id": trade_id, "side": order_spec.side, "volume": order_spec.volume},
        )

        try:
            broker_order_id = broker.place_order(
                symbol=order_spec.symbol,
                side=order_spec.side,
                volume=order_spec.volume,
                entry_price=order_spec.entry_price,
                stop_loss=order_spec.stop_loss,
                take_profit=order_spec.take_profit,
            )
        except Exception as e:  # noqa: BLE001 — el broker puede fallar de mil formas
            # El broker rechazó/falló ANTES de aceptar: liberar el coid
            trade_repo.update_status(trade_id, "REJECTED")
            errors.append(f"broker error: {e}")
            event_repo.log_event(
                run_id=run_id,
                agent="decision_maker",
                event_type="TOOL_RESULT",
                payload={"tool": "place_order", "error": str(e)},
            )
            continue

        try:
            _confirm_open(trade_id, broker_order_id)
        except sqlite3.Error as e:
            # Orden YA enviada pero no confirmada en DB. El trade queda
            # PENDING con su coid → ningún re-run la reenviará (paso 3).
            log.critical(
                "Orden ENVIADA pero sin confirmar en DB: trade_id=%s "
                "broker_order_id=%s coid=%s error=%s — reconciliar manualmente",
                trade_id, broker_order_id, coid, e,
            )
            errors.append(
                f"orden enviada sin confirmar: trade {trade_id} broker {broker_order_id}"
            )
            continue

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

    return ExecuteOutput(decision_id=decision_id, orders=orders, errors=errors, skipped=skipped)
