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
2. Chequear client_order_id activo (idempotencia) y quedarse solo con las
   órdenes que faltan por enviar. Va ANTES del budget para que un duplicado
   nunca reserve cupo del día.
3. Reservar budget por esas órdenes y persistir la decisión (ya validada).
4. Por cada orden: insertar trade PENDING → enviar al broker → actualizar a
   OPEN.
   - Si el broker rechaza: REJECTED (el coid queda libre para reintentar).
   - Si el update a OPEN falla tras enviar: el trade queda PENDING con su
     coid, lo que BLOQUEA cualquier reenvío en re-runs (log CRITICAL con
     el broker_order_id para reconciliación manual).
"""

from __future__ import annotations

import sqlite3
import time

from domain.errors import (
    CoherenceError,
    InsufficientRRError,
    InvalidBoxError,
)
from domain.signals.breakout import BreakoutState
from domain.strategy.box import max_amplitude_for
from domain.strategy.budget import DailyOrderBudget
from domain.strategy.decision import Action
from domain.strategy.order_spec import OrderSpec, build_execution_plan, make_client_order_id
from domain.strategy.outcome import resolve_trade_outcome
from domain.strategy.position_sizer import size_position
from infrastructure.broker.simplefx.adapter import SimpleFXAdapter
from infrastructure.broker.simplefx.market_data import SimpleFXMarketData
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

# Granularidad para reconstruir cierres. Debe ir a la par del intervalo del
# monitor: con velas más finas el trailing simulado saldría mejor de lo que el
# Position Manager puede conseguir en vivo (ver domain/strategy/outcome.py).
RECONCILE_PERIOD_SECONDS = 300


@retry(max_retries=3, initial_delay=0.5, exceptions=(sqlite3.Error,))
def _confirm_open(trade_id: int, broker_order_id: str) -> None:
    trade_repo.update_status(trade_id, "OPEN", broker_order_id=broker_order_id)


def reconcile_closed_trades(run_id: str, market_data: SimpleFXMarketData | None = None) -> int:
    """Cierra en DB los trades que el broker ya cerró, reconstruyéndolos del feed.

    SimpleFX no expone historial de órdenes en su API pública, así que el bot
    no puede preguntar "¿cómo terminó?". Sí conoce entry/SL/TP de cada orden que
    envió y tiene el feed de velas: con eso el resultado es reconstruible
    (``domain.strategy.outcome``).

    Sin esto un trade cerrado por el broker se queda OPEN para siempre en DB:
    ``realized_pnl_today()`` devuelve 0.0 aunque el día haya sido perdedor —
    y con él el drawdown guard —, y el Position Manager reintenta modificar una
    orden inexistente en cada corrida.

    Retorna cuántos trades se cerraron.
    """
    settings = get_settings()
    feed = market_data or SimpleFXMarketData()
    closed = 0

    for trade in trade_repo.list_trades_needing_outcome():
        if str(trade.broker_order_id or "").startswith("DRY-"):
            continue  # los simulados los gestiona reconcile_pending_trades
        if not trade.entry_price or trade.initial_stop_loss is None:
            continue

        opened_at = _ts_to_unix(trade.ts_open)
        if opened_at is None:
            continue
        try:
            candles = feed.get_candles(
                trade.symbol, RECONCILE_PERIOD_SECONDS, opened_at, int(time.time())
            )
        except Exception as e:  # noqa: BLE001 — feed externo: no debe tumbar el run
            log.warning("Reconcile: sin velas para trade %s (%s): %s", trade.id, trade.symbol, e)
            continue

        outcome = resolve_trade_outcome(trade, candles)
        status, exit_price, r_multiple, reason = (
            outcome.status,
            outcome.exit_price,
            outcome.r_multiple,
            outcome.reason,
        )

        if not outcome.is_closed:
            if trade.status == "OPEN":
                continue  # sigue vivo de verdad: nada que reconciliar
            if not outcome.activated:
                # El broker ya no tiene la orden y el precio nunca la llenó:
                # era una pendiente cancelada/caducada, sin P&L.
                trade_repo.close_trade(
                    trade.id,
                    status="EXPIRED",
                    exit_price=0.0,
                    pnl=0.0,
                    r_multiple=0.0,
                    reason="reconciliado desde velas: la orden pendiente nunca se llenó",
                )
                closed += 1
                continue
            # El broker ya dijo que la orden no existe (s7: INVALID_ORDER) pero
            # el precio nunca tocó SL ni TP → lo cerraron a mercado. Se valora
            # al último cierre disponible para no dejarlo sin P&L para siempre.
            last_close = _last_close(candles)
            if last_close is None:
                continue
            status = "CLOSED_MANUAL"
            exit_price = last_close
            r_multiple = _r_from_levels(trade, last_close)
            reason = "cerrado fuera del bot (ni SL ni TP): valorado al último cierre"

        # R exacto; el dinero es una estimación vía POINT_VALUE (ver settings).
        pnl = (r_multiple or 0.0) * abs(
            trade.entry_price - trade.initial_stop_loss
        ) * trade.volume * settings.point_value
        trade_repo.close_trade(
            trade.id,
            status=status,
            exit_price=float(exit_price or 0.0),
            pnl=round(pnl, 2),
            r_multiple=r_multiple,
            reason=f"reconciliado desde velas: {reason}",
        )
        closed += 1
        log.info(
            "Trade %s (%s %s) → %s @ %.2f  R=%.2f  pnl≈%.2f",
            trade.id, trade.symbol, trade.side, status,
            exit_price or 0.0, r_multiple or 0.0, pnl,
        )
        event_repo.log_event(
            run_id=run_id,
            agent="decision_maker",
            event_type="SYSTEM",
            payload={
                "reconcile": "closed_from_candles",
                "trade_id": trade.id,
                "status": status,
                "exit_price": exit_price,
                "r_multiple": r_multiple,
                "pnl_estimated": round(pnl, 2),
            },
        )
    return closed


def _last_close(candles) -> float | None:
    """Último cierre disponible del feed, o None si no hay."""
    if candles is None or candles.empty or "close" not in candles.columns:
        return None
    closes = candles.sort_values("time")["close"].dropna()
    return float(closes.iloc[-1]) if not closes.empty else None


def _r_from_levels(trade, exit_price: float) -> float | None:
    """R-múltiple de una salida arbitraria, medida contra el riesgo inicial."""
    risk = abs(trade.entry_price - trade.initial_stop_loss)
    if risk <= 0:
        return None
    delta = (
        exit_price - trade.entry_price
        if trade.side.upper() == "BUY"
        else trade.entry_price - exit_price
    )
    return round(delta / risk, 3)


def _ts_to_unix(ts_iso: str | None) -> int | None:
    """ISO8601 de la DB → unix segundos."""
    if not ts_iso:
        return None
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(ts_iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        from datetime import UTC

        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


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
                trade.id,
                trade.broker_order_id,
                trade_day,
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

    def _record(execution_status: str, reasons: list[str] | None = None) -> int:
        """Deja constancia en `decisions` de una decisión que NO llegó al broker.

        Toda decisión se registra, se opere o no. Las descartadas son la mitad
        de la muestra que hace falta para saber si el filtro está bien
        calibrado: sin ellas no se puede responder "¿cuántos buenos setups
        rechazamos?", que es justo la pregunta de esta estrategia.
        """
        try:
            return decision_repo.insert_decision(
                run_id=run_id,
                symbol=decision.symbol,
                action=decision.action.value,
                risk=decision.risk.value,
                confidence=decision.confidence,
                reasons=list(decision.reasons) + (reasons or []),
                team_consensus=decision.team_consensus,
                key_levels=decision.key_levels,
                signal=decision.signal,
                execution_status=execution_status,
            )
        except sqlite3.Error as e:
            # Registrar es observabilidad, no puede tumbar el pipeline.
            log.error("No se pudo registrar la decisión de %s: %s", decision.symbol, e)
            return 0

    # ── 1. Validaciones — ninguna envía órdenes ────────────────────────
    if decision.action == Action.NO_OPERAR:
        decision_id = _record("NO_OPERAR")
        event_repo.log_event(
            run_id=run_id,
            agent="decision_maker",
            event_type="DECISION",
            payload={"symbol": decision.symbol, "action": "NO_OPERAR", "skip": True},
        )
        # NO_OPERAR es un resultado normal de la estrategia, no un error
        return ExecuteOutput(decision_id=decision_id, orders=[], errors=[])

    def _reject(msg: str) -> ExecuteOutput:
        errors.append(msg)
        return ExecuteOutput(decision_id=_record("REJECTED", [msg]), orders=[], errors=errors)

    if input_data.symbol != decision.symbol:
        return _reject(
            f"symbol mismatch: input={input_data.symbol} decision={decision.symbol}"
        )

    # La dirección del LLM nunca puede contradecir el breakout determinista.
    breakout_raw = str(
        decision.signal.get("breakout_state") or decision.signal.get("state") or ""
    ).upper()
    expected_action = {
        BreakoutState.ABOVE.value: Action.LONG,
        BreakoutState.BELOW.value: Action.SHORT,
    }.get(breakout_raw)
    if expected_action is None:
        return _reject(f"breakout state missing/invalid: {breakout_raw or 'empty'}")
    if decision.action != expected_action:
        return _reject(
            f"direction mismatch: breakout={breakout_raw} action={decision.action.value}"
        )

    # Regla #18
    if decision.confidence < settings.min_confidence:
        return _reject(f"confidence {decision.confidence} < min {settings.min_confidence}")

    # Freno duro de pérdida diaria (no depende del veto del LLM)
    pnl_today = trade_repo.realized_pnl_today()
    if pnl_today <= -settings.max_daily_loss:
        return _reject(
            f"daily loss limit: pnl hoy {pnl_today:.2f} <= -{settings.max_daily_loss:.2f}"
        )

    # Regla #1: validar box (límite propio del símbolo)
    try:
        input_data.box.validate(max_amplitude_for(decision.symbol))
    except InvalidBoxError as e:
        return _reject(f"box invalid: {e}")

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
        return _reject(f"plan invalid: {e}")

    # ── 2. Idempotencia ANTES del budget ───────────────────────────────
    # Una orden que ya está activa no se va a reenviar, así que no puede
    # reservar cupo del día: si lo hiciera, un re-run tras crash quemaría el
    # budget con duplicados y bloquearía símbolos que sí podían operar.
    to_send: list[tuple[OrderSpec, str]] = []
    for order_spec in plan.orders:
        coid = make_client_order_id(
            input_data.trade_date, order_spec.symbol, order_spec.side, order_spec.is_runner
        )
        existing = trade_repo.find_active_by_client_order_id(coid)
        if existing is not None:
            skipped.append(coid)
            log.warning(
                "Orden %s ya activa (trade %s, status %s) — no se reenvía",
                coid,
                existing.id,
                existing.status,
            )
            event_repo.log_event(
                run_id=run_id,
                agent="decision_maker",
                event_type="SYSTEM",
                payload={
                    "event": "ORDER_SKIPPED_DUPLICATE",
                    "client_order_id": coid,
                    "existing_trade_id": existing.id,
                },
            )
            continue
        to_send.append((order_spec, coid))

    if not to_send:
        # Todo duplicado: nada que enviar ni que reservar. Se deja constancia
        # de la decisión (el re-run la tomó igual) marcada como duplicada.
        return ExecuteOutput(
            decision_id=_record("SKIPPED_DUPLICATE"), orders=[], errors=errors, skipped=skipped
        )

    # Regla #11: budget diario — solo por las órdenes que faltan por enviar
    if not budget.try_consume(len(to_send)):
        msg = "daily budget exhausted"
        errors.append(msg)
        return ExecuteOutput(
            decision_id=_record("REJECTED", [msg]), orders=[], errors=errors, skipped=skipped
        )

    # ── 3. Persistir decisión (ya validada) ────────────────────────────
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

    # ── 4. Enviar órdenes ──────────────────────────────────────────────
    orders: list[OrderContract] = []
    for order_spec, coid in to_send:
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
            # carrera con otro insert del mismo coid — tratar como duplicado.
            # La orden no se envía, así que su cupo vuelve al budget.
            skipped.append(coid)
            budget.release(1)
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
                trade_id,
                broker_order_id,
                coid,
                e,
            )
            errors.append(f"orden enviada sin confirmar: trade {trade_id} broker {broker_order_id}")
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
