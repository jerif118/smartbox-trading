"""
Stage 7: Manage — Position Manager gestiona trades abiertos.

Trabaja contra SQLite como source of truth. Reglas:
- Si trade ha recorrido >= 1R a favor → mover SL a breakeven
- Si trade ha recorrido >= 2R → trailing stop 1R detrás del high/low
"""

from __future__ import annotations

from infrastructure.broker.simplefx.adapter import SimpleFXAdapter
from infrastructure.persistence.sqlite import event_repo, trade_repo
from pipeline.contracts import ManageAction, ManageInput, ManageOutput
from utils.logger import get_logger

log = get_logger(__name__)


def _compute_r_multiple(trade: dict, current_price: float) -> float | None:
    """Calcula R-multiple actual del trade."""
    entry = trade.get("entry_price", 0)
    sl = trade.get("stop_loss", 0)
    if not entry or not sl or entry == sl:
        return None
    if trade["side"] == "BUY":
        return (current_price - entry) / (entry - sl)
    return (entry - current_price) / (sl - entry)


def stage_manage(
    input_data: ManageInput,
    run_id: str,
    broker: SimpleFXAdapter,
) -> ManageOutput:
    """Gestiona trades abiertos. SL a BE en +1R, trailing en +2R."""
    actions: list[ManageAction] = []
    for trade in input_data.open_trades:
        symbol = trade["symbol"]
        current = input_data.current_prices.get(symbol)
        if current is None:
            continue

        r = _compute_r_multiple(trade, current)
        if r is None:
            continue

        entry = trade["entry_price"]
        sl = trade["stop_loss"]
        side = trade["side"]
        is_runner = bool(trade.get("is_runner", 0))
        trade_id = trade["id"]

        action = ManageAction(
            trade_id=trade_id,
            action="HOLD",
            reason=f"R actual: {r:.2f}",
        )

        # Regla: trailing / BE
        new_sl = None
        if r >= 2.0 and not is_runner:
            # Trailing: SL a 1R detrás del high actual
            if side == "BUY":
                new_sl = current - (entry - sl)
            else:
                new_sl = current + (sl - entry)
            action.action = "MODIFY_SL"
            action.new_sl = round(new_sl, 2)
            action.reason = f"R={r:.2f} >= 2.0, trailing SL a {new_sl:.2f}"
        elif r >= 1.0 and not is_runner:
            # BE: SL a entry
            new_sl = entry
            action.action = "MODIFY_SL"
            action.new_sl = round(new_sl, 2)
            action.reason = f"R={r:.2f} >= 1.0, moved to breakeven"

        if action.action != "HOLD" and new_sl is not None:
            try:
                # Solo modificar si el nuevo SL es MEJOR que el actual
                current_sl = trade.get("stop_loss", 0)
                if (side == "BUY" and new_sl > current_sl) or (side == "SELL" and new_sl < current_sl):
                    broker.modify_order(
                        trade["broker_order_id"],
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
