"""
Resolución determinista del resultado de un trade a partir de velas OHLC.

Por qué existe: la API pública documentada de SimpleFX cubre auth y accounts,
no listado de órdenes ni historial de cierres, así que el bot no puede
preguntarle al broker "¿cómo terminó este trade?". Pero sí tiene el feed
público de velas y conoce entry/SL/TP de cada orden que envió: con eso el
resultado es reconstruible sin ambigüedad.

Dos consumidores, una sola implementación:
- Reconciliación (s6): cerrar en DB los trades que el broker ya cerró, para que
  ``realized_pnl_today()`` y el drawdown guard dejen de leer 0.0 para siempre.
- Replay (``scripts/replay.py``): medir hit rate y expectativa en R de un
  cambio de filtro sobre histórico, antes de arriesgar dinero.

Dos convenciones deliberadas, ambas pesimistas:

1. Si una misma vela toca SL y TP, se asume SL. El feed no dice en qué orden
   ocurrió dentro de la vela, y la alternativa optimista inflaría justo la
   métrica (hit rate) que se quiere medir.
2. El SL que se evalúa en una vela es el que el broker tenía al ABRIR esa vela.
   El trailing de s7_manage solo corre una vez por intervalo de monitoreo, así
   que mover el stop dentro de la vela sería una ventaja que en vivo no existe.
   El caller debe pasar velas con la cadencia real del monitor
   (``MONITOR_INTERVAL_S``), no más finas, o el trailing saldrá optimista.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

# Gatillos del Position Manager (s7_manage): deben moverse juntos con él.
BE_AT_R = 1.0
TRAIL_AT_R = 2.0


@dataclass(frozen=True)
class TradeOutcome:
    """Resultado reconstruido de un trade.

    ``r_multiple`` se mide siempre contra el riesgo INICIAL (entry - SL de
    origen), para que sea comparable entre trades y entre símbolos.
    ``status`` usa los mismos valores que ``TradeStatus`` para poder escribirse
    directo en la DB.
    """

    status: str  # CLOSED_TP | CLOSED_SL | OPEN | EXPIRED
    exit_price: float | None = None
    exit_time: int | None = None
    r_multiple: float | None = None
    max_favorable_price: float | None = None
    activated: bool = False
    reason: str = ""

    @property
    def is_closed(self) -> bool:
        return self.status in ("CLOSED_TP", "CLOSED_SL")


def _r_multiple(side: str, entry: float, initial_sl: float, exit_price: float) -> float | None:
    risk = abs(entry - initial_sl)
    if risk <= 0:
        return None
    delta = (exit_price - entry) if side.upper() == "BUY" else (entry - exit_price)
    return round(delta / risk, 3)


def simulate_trade(
    candles: pd.DataFrame,
    *,
    side: str,
    entry: float,
    stop_loss: float,
    take_profit: float | None,
    is_runner: bool = False,
    activated: bool = False,
    reference_price: float | None = None,
    expires_at: int | None = None,
    be_at_r: float = BE_AT_R,
    trail_at_r: float = TRAIL_AT_R,
) -> TradeOutcome:
    """Camina las velas y devuelve cómo terminó (o si sigue vivo) el trade.

    ``candles`` debe traer columnas time/high/low ordenables por tiempo y
    cubrir desde el momento en que se envió la orden en adelante.

    ``activated=False`` (por defecto) modela una orden PENDING: primero tiene
    que alcanzarse ``entry`` para que exista posición. Pásalo en True para un
    trade que ya se sabe abierto.

    ``reference_price`` es el precio de mercado cuando se envió la orden y
    decide su tipo. El bot envía la orden DESPUÉS de que una vela cierre fuera
    de la caja, así que una compra en el borde superior queda POR DEBAJO del
    precio: es una orden límite que solo se llena si el precio vuelve a la
    caja (``low <= entry``). Sin ``reference_price`` se asume orden stop
    (``high >= entry``), el caso de una orden colocada antes de la ruptura.
    Con orden límite, la vela que la llena no puede además cerrar en TP: no
    se sabe si el máximo ocurrió antes o después del retroceso.

    ``expires_at`` (unix s) es la caducidad de la orden pendiente en el broker
    (``PENDING_EXPIRY``): una vela que abre a esa hora o después ya no la llena.

    El runner (``take_profit=None``) solo puede cerrar por stop, y sobre él se
    aplican las mismas reglas de breakeven y trailing que ``s7_manage``.
    """
    side = side.upper()
    is_buy = side == "BUY"

    if candles is None or candles.empty:
        return TradeOutcome(status="OPEN", activated=activated, reason="sin velas para evaluar")

    bars = candles.sort_values("time")
    initial_sl = stop_loss
    current_sl = stop_loss
    mfe: float | None = None
    # Orden límite: compra por debajo del mercado / venta por encima.
    is_limit = reference_price is not None and (
        reference_price > entry if is_buy else reference_price < entry
    )

    for _, bar in bars.iterrows():
        high = float(bar["high"])
        low = float(bar["low"])
        ts = int(bar["time"])
        fill_bar = False

        # ── 1. Activación de la orden pendiente ────────────────────────────
        if not activated:
            if expires_at is not None and ts >= expires_at:
                break  # la orden caducó en el broker sin llenarse
            if is_limit:
                reached = low <= entry if is_buy else high >= entry
            else:
                reached = high >= entry if is_buy else low <= entry
            if not reached:
                continue
            activated = True
            fill_bar = True
            # La misma vela que activa puede stopear: se sigue evaluando abajo.

        # ── 2. Salidas, con el SL que el broker tenía al abrir la vela ─────
        hit_sl = low <= current_sl if is_buy else high >= current_sl
        hit_tp = (
            take_profit is not None
            and not (fill_bar and is_limit)
            and (high >= take_profit if is_buy else low <= take_profit)
        )

        if hit_sl:
            # Pesimista a propósito: si la vela también tocó el TP, gana el SL.
            return TradeOutcome(
                status="CLOSED_SL",
                exit_price=current_sl,
                exit_time=ts,
                r_multiple=_r_multiple(side, entry, initial_sl, current_sl),
                max_favorable_price=mfe,
                activated=True,
                reason=(
                    "stop alcanzado"
                    if current_sl == initial_sl
                    else f"stop movido a {current_sl:.2f} alcanzado"
                ),
            )
        if hit_tp:
            return TradeOutcome(
                status="CLOSED_TP",
                exit_price=take_profit,
                exit_time=ts,
                r_multiple=_r_multiple(side, entry, initial_sl, float(take_profit)),
                max_favorable_price=mfe,
                activated=True,
                reason="take profit alcanzado",
            )

        # ── 3. High-water y trailing, ya cerrada la vela ───────────────────
        extreme = high if is_buy else low
        mfe = extreme if mfe is None else (max(mfe, extreme) if is_buy else min(mfe, extreme))

        if is_runner:
            r_peak = _r_multiple(side, entry, initial_sl, mfe)
            if r_peak is not None:
                risk = abs(entry - initial_sl)
                new_sl = None
                if r_peak >= trail_at_r:
                    new_sl = (mfe - risk) if is_buy else (mfe + risk)
                elif r_peak >= be_at_r:
                    new_sl = entry
                # Monotonía: el stop solo se aprieta, igual que en s7_manage.
                if new_sl is not None and (
                    (is_buy and new_sl > current_sl) or (not is_buy and new_sl < current_sl)
                ):
                    current_sl = new_sl

    if not activated:
        return TradeOutcome(
            status="EXPIRED",
            activated=False,
            reason="la orden nunca alcanzó el precio de entrada",
        )
    return TradeOutcome(
        status="OPEN",
        max_favorable_price=mfe,
        activated=True,
        reason="sin tocar SL ni TP en el rango evaluado",
    )


def resolve_trade_outcome(trade: Any, candles: pd.DataFrame) -> TradeOutcome:
    """``simulate_trade`` a partir de una fila ``Trade`` de la DB.

    Usa ``initial_stop_loss`` como unidad de R (el ``stop_loss`` actual puede
    venir ya movido a breakeven por el Position Manager).

    OPEN en DB NO implica que la orden se haya llenado: s6 marca OPEN en cuanto
    el broker ACEPTA la orden pendiente. Tratarla como activada inventaba
    trades cerrados (con P&L que alimenta el freno de pérdida diaria) para
    órdenes límite que el precio nunca alcanzó. Por eso la activación se
    reconstruye siempre desde las velas, usando la apertura de la primera vela
    como precio de referencia para saber si la orden era límite o stop.
    """
    initial_sl = trade.initial_stop_loss or trade.stop_loss
    reference = None
    if candles is not None and not candles.empty and "open" in candles.columns:
        first_open = candles.sort_values("time")["open"].dropna()
        if not first_open.empty:
            reference = float(first_open.iloc[0])
    return simulate_trade(
        candles,
        side=trade.side,
        entry=float(trade.entry_price),
        stop_loss=float(initial_sl),
        take_profit=float(trade.take_profit) if trade.take_profit is not None else None,
        is_runner=bool(trade.is_runner),
        activated=False,
        reference_price=reference,
    )
