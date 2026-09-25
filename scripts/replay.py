#!/usr/bin/env python3
"""
Replay determinista de la estrategia de caja sobre histórico.

Para qué: hoy no hay forma de saber si un cambio de filtro mejora o empeora.
Este script reconstruye, día a día, la caja, la ruptura y el resultado real
del trade, y reporta hit rate y expectativa en R con y sin filtro. Es la
herramienta con la que se calibran los umbrales — ninguno debería elegirse
a ojo.

Sin LLM a propósito: reproducible, gratis y rápido. Lo que mide es la parte
determinista (caja + ruptura + confluencia), que es donde vive el filtro.

Feed: velas públicas de SimpleFX, el broker donde las órdenes se ejecutan de
verdad. En producción la señal se calcula sobre Capital y los niveles sobre
SimpleFX; aquí se usa SimpleFX para las dos cosas, porque el resultado que se
quiere medir (¿tocó SL o TP?) ocurre en los precios de SimpleFX. La forma de
la caja es prácticamente la misma en ambos feeds: cambia el offset, no la
estructura.

Uso:
    python scripts/replay.py --symbol US500 --days 60
    python scripts/replay.py --symbol US500 --days 90 --min-confluence 55 --csv out.csv
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

# El proyecto importa por paquete raíz `src/`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pandas as pd

from domain.indicators.rsi import last_rsi
from domain.indicators.volume_profile import compute_volume_profile
from domain.market_time import box_window_unix
from domain.signals.breakout import BreakoutState, detect_breakout
from domain.signals.confluence import compute_confluence_score
from domain.signals.divergence import detect_rsi_divergence
from domain.signals.mtf import ema_bias, mtf_alignment
from domain.signals.vp_path import clear_path_fraction, dedupe_levels, strong_levels
from domain.strategy.box import compute_box_from_df, max_amplitude_for
from domain.strategy.order_spec import build_long_plan, build_short_plan
from domain.strategy.outcome import simulate_trade
from infrastructure.broker.simplefx.market_data import SimpleFXMarketData

CANDLE_PERIOD_S = 300  # 5 min: la cadencia real del monitor
BREAKOUT_WINDOW_S = 2 * 3600  # regla #6
DIRECTION_BY_STATE = {BreakoutState.ABOVE: "LONG", BreakoutState.BELOW: "SHORT"}

# Historia previa a la caja que se descarga con las velas del día. El Volume
# Profile de producción se calcula sobre la ventana de ingest (3 días), así que
# el replay necesita ese mismo colchón para que los niveles coincidan.
VP_LOOKBACK_DAYS = 3

# Timeframes del sesgo multi-timeframe, en segundos. Se descargan UNA vez para
# todo el rango del replay y se cortan por día: pedirlos día a día multiplica
# las llamadas al feed sin aportar nada.
MTF_PERIODS = {"15min": 900, "1h": 3600, "4h": 4 * 3600}

# Colchón para que la EMA20 del marco de 4h tenga velas suficientes desde el
# primer día del replay (20 velas de 4h ≈ 3.3 días, más margen de fines de
# semana y festivos).
MTF_WARMUP_DAYS = 15


def _day_candles(
    feed: SimpleFXMarketData, symbol: str, day: date, follow_days: int
) -> pd.DataFrame:
    """Velas de 5 min desde antes de la caja hasta ``follow_days`` después.

    El runner no lleva take profit: solo cierra por stop (breakeven o trailing),
    y puede pasar noches abiertas. Si el replay cortara al final del día, el
    runner quedaría casi siempre sin resolver y su aporte se subestimaría.
    """
    start = int(datetime.combine(day, datetime.min.time(), tzinfo=UTC).timestamp())
    return feed.get_candles(
        symbol,
        CANDLE_PERIOD_S,
        start - VP_LOOKBACK_DAYS * 86400,
        start + (24 * (follow_days + 1) + 2) * 3600,
    )


def _fetch_mtf_series(
    feed: SimpleFXMarketData, symbol: str, first_day: date, last_day: date
) -> dict[str, pd.DataFrame]:
    """Series 15min/1h/4h del rango completo, para derivar el sesgo por día."""
    start = int(
        datetime.combine(first_day, datetime.min.time(), tzinfo=UTC).timestamp()
    ) - MTF_WARMUP_DAYS * 86400
    end = int(datetime.combine(last_day, datetime.max.time(), tzinfo=UTC).timestamp())
    series: dict[str, pd.DataFrame] = {}
    for name, period in MTF_PERIODS.items():
        try:
            series[name] = feed.get_candles(symbol, period, start, end)
        except Exception as e:  # noqa: BLE001 — feed externo; sin MTF no se aborta
            print(f"  aviso: no se pudo descargar {name}: {e}", file=sys.stderr)
            series[name] = pd.DataFrame()
    return series


def _biases_at(mtf_series: dict[str, pd.DataFrame], cutoff_ts: int) -> dict[str, str]:
    """Sesgo EMA por timeframe usando SOLO velas cerradas antes del corte.

    El corte evita el look-ahead: al evaluar la caja de las 09:55 no se puede
    saber cómo cerró la vela de 4h que todavía está abierta.
    """
    biases: dict[str, str] = {}
    for name, df in mtf_series.items():
        if df is None or df.empty or "time" not in df.columns:
            biases[name] = "NEUTRAL"
            continue
        # `time` es la APERTURA de la vela: una vela está cerrada solo si
        # time + periodo <= corte. Filtrar por `time <= corte` incluía la vela
        # de 4h de las 08:00-12:00 NY, cuyo cierre ya contiene la ruptura: ese
        # look-ahead era lo único que hacía "funcionar" el score de confluencia.
        period = MTF_PERIODS.get(name, 0)
        past = df[df["time"] + period <= cutoff_ts]
        if len(past) < 20:
            biases[name] = "NEUTRAL"
            continue
        biases[name] = ema_bias(past["close"].tolist())
    return biases


def replay_day(
    feed: SimpleFXMarketData,
    symbol: str,
    day: date,
    *,
    box_start: str,
    box_end: str,
    market_tz: str,
    min_confluence: int,
    min_penetration_pct: float,
    follow_days: int,
    mtf_series: dict[str, pd.DataFrame],
    pending_expiry: str = "16:00",
) -> dict | None:
    """Reconstruye el día. None si no hubo caja o no hubo ruptura."""
    df = _day_candles(feed, symbol, day, follow_days)
    if df is None or df.empty:
        return None

    box_from, box_to = box_window_unix(day.isoformat(), box_start, box_end, market_tz)
    box = compute_box_from_df(df, box_from, box_to)
    if box is None:
        return None

    row: dict = {
        "date": day.isoformat(),
        "symbol": symbol,
        "box_high": box.high,
        "box_low": box.low,
        "box_amp_pct": round(box.amplitude_pct, 3),
    }

    # Regla #1: el gate de amplitud corre antes que nada.
    if not box.is_valid(max_amplitude_for(symbol)):
        return {**row, "outcome": "SIN_CAJA", "reason": "amplitud fuera de límite"}

    post_box = df[(df["time"] > box_to) & (df["time"] <= box_to + BREAKOUT_WINDOW_S)]
    signal = detect_breakout(post_box, box)
    if signal is None or signal.state not in DIRECTION_BY_STATE:
        return {**row, "outcome": "SIN_RUPTURA", "reason": "el precio no salió de la caja"}

    direction = DIRECTION_BY_STATE[signal.state]
    row["direction"] = direction
    row["breakout_close"] = signal.candle_close
    # La penetración ya NO puntúa (ver domain/signals/confluence.py). Se sigue
    # registrando en el CSV para poder comprobar a posteriori que, en efecto,
    # no discrimina — y para el gate residual de Stage 4.
    level = box.high if direction == "LONG" else box.low
    box_range = box.high - box.low
    row["penetration_pct"] = (
        round(abs(signal.candle_close - level) / box_range * 100, 2) if box_range > 0 else 0.0
    )
    below_min_penetration = row["penetration_pct"] < min_penetration_pct

    # ── Score de calidad, con los mismos inputs que el pipeline ───────────
    # Todo se calcula con velas ANTERIORES al cierre de la caja: cualquier dato
    # posterior sería look-ahead y volvería el backtest optimista.
    history = df[df["time"] <= box_to]
    rsi = last_rsi(history["close"]) if "close" in df.columns else None
    row["rsi"] = round(rsi, 2) if rsi is not None else None

    # Volume Profile sobre la misma ventana que produccion (ingest completo),
    # no solo sobre la caja: los niveles que frenan al precio están fuera de
    # ella.
    vp_dict = None
    if "volume" in history.columns and history["volume"].sum() > 0:
        vp_obj = compute_volume_profile(history)
        if vp_obj:
            vp_dict = {
                "poc": vp_obj.poc,
                "vah": vp_obj.vah,
                "val": vp_obj.val,
                "peaks": vp_obj.peaks,
            }
    row["poc"] = round(vp_dict["poc"], 2) if vp_dict else None

    biases = _biases_at(mtf_series, box_to)
    row["htf_bias"] = biases.get("4h")
    row["mtf_alignment"] = mtf_alignment(biases, direction) if biases else None

    row["rsi_divergence"] = detect_rsi_divergence(history)

    # Camino hasta el TP: los niveles se comparan contra la entrada y el TP
    # deterministas de la estrategia (borde de la caja ± un rango de caja).
    entry = box.high if direction == "LONG" else box.low
    take_profit = box.high + box_range if direction == "LONG" else box.low - box_range
    levels = dedupe_levels(strong_levels(vp_dict), box_range)
    path = clear_path_fraction(entry, take_profit, levels) if levels else None
    row["clear_path_pct"] = round(path * 100, 1) if path is not None else None

    conf = compute_confluence_score(
        direction=direction,
        breakout_aligned=True,  # la dirección se deriva del breakout
        htf_bias=row["htf_bias"],
        mtf_alignment=row["mtf_alignment"],
        clear_path_fraction=path,
        rsi_divergence=row["rsi_divergence"],
        proceed_threshold=min_confluence,
    )
    row["confluence"] = conf["score"]
    row["factors"] = " | ".join(conf["factors"])
    # El gate de penetración de Stage 4 sigue existiendo aunque no puntúe: si
    # está activo y la ruptura no lo supera, el trade no llega ni al score.
    row["would_trade"] = conf["score"] >= min_confluence and not below_min_penetration

    # ── Resultado real, opere o no el filtro ─────────────────────────────
    # Se simula SIEMPRE: así se puede medir tanto lo que se operó como lo que
    # se descartó, que es la mitad que hoy no se registra en ningún sitio.
    from domain.strategy.position_sizer import SizedPosition

    sized = SizedPosition(primary=1.0, runner=1.0)
    build = build_long_plan if direction == "LONG" else build_short_plan
    plan = build(symbol, box, sized)
    primary = next(o for o in plan.orders if not o.is_runner)
    runner = next(o for o in plan.orders if o.is_runner)

    # Igual que en vivo: la orden se envía cuando CIERRA la vela de ruptura,
    # con el precio ya fuera de la caja. Una compra en el borde superior queda
    # entonces por debajo del mercado (orden límite): solo se llena si el
    # precio vuelve a la caja. Simular desde el cierre de la caja con orden
    # stop llenaba rupturas que en vivo nunca se habrían operado.
    signal_ts = int(pd.Timestamp(signal.signal_time).timestamp())
    after_signal = df[df["time"] >= signal_ts + CANDLE_PERIOD_S]
    _, expires_at = box_window_unix(day.isoformat(), box_start, pending_expiry, market_tz)
    res_primary = simulate_trade(
        after_signal,
        side=primary.side,
        entry=primary.entry_price,
        stop_loss=primary.stop_loss,
        take_profit=primary.take_profit,
        reference_price=signal.candle_close,
        expires_at=expires_at,
    )
    res_runner = simulate_trade(
        after_signal,
        side=runner.side,
        entry=runner.entry_price,
        stop_loss=runner.stop_loss,
        take_profit=None,
        is_runner=True,
        reference_price=signal.candle_close,
        expires_at=expires_at,
    )

    row["entry"] = primary.entry_price
    row["stop_loss"] = primary.stop_loss
    row["take_profit"] = primary.take_profit
    row["outcome"] = res_primary.status
    row["r_primary"] = res_primary.r_multiple
    row["r_runner"] = res_runner.r_multiple
    # Un runner que sigue vivo al final de la ventana se cuenta como 0 R: no
    # se le puede atribuir ni ganancia ni pérdida. Se reporta aparte para que
    # se vea cuánta de la muestra queda sin resolver.
    row["runner_abierto"] = res_runner.status == "OPEN"
    row["r_total"] = round(
        (res_primary.r_multiple or 0.0) + (res_runner.r_multiple or 0.0), 3
    )
    row["reason"] = res_primary.reason
    return row


def summarize(rows: list[dict], label: str) -> dict:
    """Hit rate y expectativa en R de un conjunto de días operados."""
    closed = [r for r in rows if r.get("outcome") in ("CLOSED_TP", "CLOSED_SL")]
    if not closed:
        return {"grupo": label, "trades": 0}
    wins = [r for r in closed if r["outcome"] == "CLOSED_TP"]
    r_total = [r["r_total"] for r in closed if r.get("r_total") is not None]
    r_primary = [r["r_primary"] for r in closed if r.get("r_primary") is not None]
    return {
        "grupo": label,
        "trades": len(closed),
        "aciertos": len(wins),
        "hit_rate_%": round(len(wins) / len(closed) * 100, 1),
        "R_total": round(sum(r_total), 2),
        "R_por_trade": round(sum(r_total) / len(r_total), 3) if r_total else 0.0,
        "R_solo_primary": round(sum(r_primary) / len(r_primary), 3) if r_primary else 0.0,
        "runners_abiertos": sum(1 for r in closed if r.get("runner_abierto")),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", default="US500")
    p.add_argument("--days", type=int, default=60, help="días hacia atrás a evaluar")
    p.add_argument("--box-start", default="08:00")
    p.add_argument("--box-end", default="09:55")
    p.add_argument("--market-tz", default="America/New_York")
    p.add_argument("--min-confluence", type=int, default=55)
    p.add_argument(
        "--min-penetration",
        type=float,
        default=0.0,
        help="penetración mínima de la ruptura, en %% del rango de la caja",
    )
    p.add_argument(
        "--follow-days",
        type=int,
        default=3,
        help="días posteriores a seguir para resolver el runner (no lleva TP)",
    )
    p.add_argument("--csv", help="ruta donde volcar el detalle por día")
    args = p.parse_args()

    feed = SimpleFXMarketData()
    today = datetime.now(UTC).date()
    rows: list[dict] = []

    # Sesgos multi-timeframe: una sola descarga para todo el rango.
    mtf_series = _fetch_mtf_series(
        feed, args.symbol, today - timedelta(days=args.days), today
    )

    for offset in range(args.days, 0, -1):
        day = today - timedelta(days=offset)
        if day.weekday() >= 5:  # fin de semana: el mercado está cerrado
            continue
        try:
            row = replay_day(
                feed,
                args.symbol,
                day,
                box_start=args.box_start,
                box_end=args.box_end,
                market_tz=args.market_tz,
                min_confluence=args.min_confluence,
                min_penetration_pct=args.min_penetration,
                follow_days=args.follow_days,
                mtf_series=mtf_series,
            )
        except Exception as e:
            print(f"  {day}: error → {e}", file=sys.stderr)
            continue
        if row:
            rows.append(row)

    if not rows:
        print("Sin datos para el rango pedido.")
        return 1

    traded = [r for r in rows if r.get("direction")]
    passed = [r for r in traded if r.get("would_trade")]
    filtered = [r for r in traded if not r.get("would_trade")]

    print(f"\n{'=' * 72}")
    print(
        f"REPLAY {args.symbol} · {args.days} días · confluencia mínima "
        f"{args.min_confluence} · penetración mínima {args.min_penetration}%"
    )
    print(f"{'=' * 72}")
    print(f"Días con caja válida y ruptura: {len(traded)}")
    print(f"  aprobados por el filtro:      {len(passed)}")
    print(f"  descartados por el filtro:    {len(filtered)}")

    print("\nRendimiento (primary + runner, en R):")
    for summary in (
        summarize(traded, "TODO (sin filtro)"),
        summarize(passed, "APROBADOS"),
        summarize(filtered, "DESCARTADOS"),
    ):
        if summary.get("trades"):
            print(
                f"  {summary['grupo']:<22} n={summary['trades']:>3}  "
                f"hit={summary['hit_rate_%']:>5}%  "
                f"R total={summary['R_total']:>7}  R/trade={summary['R_por_trade']:>6}  "
                f"(solo primary {summary['R_solo_primary']:>6}, "
                f"{summary['runners_abiertos']} runners sin cerrar)"
            )
        else:
            print(f"  {summary['grupo']:<22} sin trades cerrados")

    print(
        "\nUn filtro sirve si APROBADOS supera a TODO en R/trade y DESCARTADOS "
        "queda por debajo.\nSi DESCARTADOS gana, el filtro está tirando los buenos setups."
    )

    if args.csv:
        pd.DataFrame(rows).to_csv(args.csv, index=False)
        print(f"\nDetalle por día → {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
