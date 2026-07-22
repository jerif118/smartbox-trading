"""
Stage 7: Manage — Position Manager gestiona trades abiertos.

Trabaja contra SQLite como source of truth. Reglas:
- La orden primary conserva su SL/TP original
- Runner en >= 1R a favor → mover SL a breakeven
- Runner en >= 2R → trailing stop 1R detrás del máximo favorable (high-water)

El trailing y los gatillos (1R/2R) se basan en la MÁXIMA excursión favorable
alcanzada por el trade (max_favorable_price), no en el precio del momento del
run. Como el pipeline corre por ventanas, si el precio retrocede entre runs un
trailing sobre el precio actual quedaría más flojo; anclarlo al high-water evita
devolver ganancia ya conseguida y hace el SL monótono (solo sube en BUY / baja
en SELL).
"""

from __future__ import annotations

from infrastructure.broker.simplefx.adapter import SimpleFXAdapter
from infrastructure.persistence.sqlite import event_repo, trade_repo
from pipeline.contracts import ManageAction, ManageInput, ManageOutput, OpenTradeContract
from utils.logger import get_logger

log = get_logger(__name__)


def _compute_r_multiple(trade: OpenTradeContract, current_price: float) -> float | None:
    """Calcula R-multiple actual del trade."""
    entry = trade.entry_price
    sl = trade.initial_stop_loss or trade.stop_loss
    if not entry or not sl or entry == sl:
        return None
    if trade.side == "BUY":
        return (current_price - entry) / (entry - sl)
    return (entry - current_price) / (sl - entry)


def stage_manage(
    input_data: ManageInput,
    run_id: str,
    broker: SimpleFXAdapter,
) -> ManageOutput:
    """Gestiona runners abiertos: SL a BE en +1R y trailing en +2R."""
    actions: list[ManageAction] = []
    for trade in input_data.open_trades:
        symbol = trade.symbol
        current = input_data.current_prices.get(symbol)
        if current is None:
            continue

        # R del precio actual (solo informativo) y R del máximo favorable (gatillo).
        r_now = _compute_r_multiple(trade, current)
        if r_now is None:
            continue

        entry = trade.entry_price
        sl = trade.initial_stop_loss or trade.stop_loss
        side = trade.side
        is_runner = trade.is_runner
        trade_id = trade.id

        # ── High-water: máxima excursión favorable alcanzada ──────────────
        prev_mfe = trade.max_favorable_price
        if side == "BUY":
            mfe = current if prev_mfe is None else max(prev_mfe, current)
        else:
            mfe = current if prev_mfe is None else min(prev_mfe, current)
        if mfe != prev_mfe:
            trade_repo.update_max_favorable(trade_id, mfe)

        # El gatillo 1R/2R y el trailing se anclan al mejor precio, no al actual.
        r_peak = _compute_r_multiple(trade, mfe)
        r_peak = r_now if r_peak is None else r_peak

        action = ManageAction(
            trade_id=trade_id,
            action="HOLD",
            reason=f"R actual: {r_now:.2f} (pico: {r_peak:.2f})",
        )

        # Regla: trailing / BE (sobre el high-water)
        new_sl = None
        if r_peak >= 2.0 and is_runner:
            # Trailing: SL a 1R detrás del máximo favorable
            if side == "BUY":
                new_sl = mfe - (entry - sl)
            else:
                new_sl = mfe + (sl - entry)
            action.action = "MODIFY_SL"
            action.new_sl = round(new_sl, 2)
            action.reason = f"R pico={r_peak:.2f} >= 2.0, trailing SL a {new_sl:.2f}"
        elif r_peak >= 1.0 and is_runner:
            # BE: SL a entry
            new_sl = entry
            action.action = "MODIFY_SL"
            action.new_sl = round(new_sl, 2)
            action.reason = f"R pico={r_peak:.2f} >= 1.0, moved to breakeven"

        if action.action != "HOLD" and new_sl is not None:
            try:
                # Solo modificar si el nuevo SL es MEJOR que el actual
                current_sl = trade.stop_loss or 0
                if (side == "BUY" and new_sl > current_sl) or (
                    side == "SELL" and new_sl < current_sl
                ):
                    broker.modify_order(
                        trade.broker_order_id,
                        stop_loss=new_sl,
                    )
                    trade_repo.modify_sl_tp(trade_id, stop_loss=new_sl)
                    event_repo.log_event(
                        run_id=run_id,
                        agent="position_manager",
                        event_type="TOOL_CALL",
                        payload={
                            "tool": "modify_order",
                            "trade_id": trade_id,
                            "new_sl": new_sl,
                            "reason": action.reason,
                        },
                    )
                else:
                    action.action = "HOLD"
                    action.reason = f"new SL no mejora el actual ({new_sl} vs {current_sl})"
            except Exception as e:
                log.error("PM: error modifying %s: %s", trade_id, e)
                action.action = "HOLD"
                action.reason = f"error: {e}"

        actions.append(action)

    return ManageOutput(actions=actions)
