"""Tests de la reconstrucción de resultados desde velas (domain/strategy/outcome.py)."""

from __future__ import annotations

import pandas as pd

from domain.strategy.outcome import simulate_trade


def _bars(rows: list[tuple[int, float, float]]) -> pd.DataFrame:
    """rows = [(time, high, low), ...]"""
    return pd.DataFrame(rows, columns=["time", "high", "low"])


# ── Activación de la orden pendiente ──────────────────────────────────
def test_order_never_activated_expires() -> None:
    # BUY stop en 101: el precio nunca llega.
    out = simulate_trade(
        _bars([(1, 100.0, 99.0), (2, 100.5, 99.5)]),
        side="BUY",
        entry=101.0,
        stop_loss=99.0,
        take_profit=103.0,
    )
    assert out.status == "EXPIRED"
    assert out.activated is False


def test_activation_and_take_profit() -> None:
    out = simulate_trade(
        _bars([(1, 101.2, 100.0), (2, 102.0, 101.0), (3, 103.5, 102.0)]),
        side="BUY",
        entry=101.0,
        stop_loss=99.0,
        take_profit=103.0,
    )
    assert out.status == "CLOSED_TP"
    assert out.exit_price == 103.0
    assert out.exit_time == 3
    assert out.r_multiple == 1.0  # TP a 1R


def test_stop_loss_short() -> None:
    # SELL stop en 99 (ruptura bajista); SL 101, TP 97.
    out = simulate_trade(
        _bars([(1, 100.0, 98.5), (2, 101.5, 99.0)]),
        side="SELL",
        entry=99.0,
        stop_loss=101.0,
        take_profit=97.0,
    )
    assert out.status == "CLOSED_SL"
    assert out.exit_price == 101.0
    assert out.r_multiple == -1.0


def test_same_bar_touching_both_resolves_pessimistic() -> None:
    # La vela toca SL (99) y TP (103) a la vez → se asume SL.
    out = simulate_trade(
        _bars([(1, 103.5, 98.5)]),
        side="BUY",
        entry=101.0,
        stop_loss=99.0,
        take_profit=103.0,
    )
    assert out.status == "CLOSED_SL"
    assert out.r_multiple == -1.0


def test_still_open_when_nothing_hit() -> None:
    out = simulate_trade(
        _bars([(1, 101.5, 100.5), (2, 102.0, 101.2)]),
        side="BUY",
        entry=101.0,
        stop_loss=99.0,
        take_profit=103.0,
    )
    assert out.status == "OPEN"
    assert out.activated is True
    assert out.max_favorable_price == 102.0


# ── Runner: breakeven en 1R y trailing en 2R ──────────────────────────
def test_runner_moves_to_breakeven_after_1r() -> None:
    # entry 101, SL 99 → 1R = 2 puntos. Llega a 103.2 (>=1R) y luego cae.
    out = simulate_trade(
        _bars([(1, 101.5, 100.8), (2, 103.2, 102.0), (3, 102.5, 100.0)]),
        side="BUY",
        entry=101.0,
        stop_loss=99.0,
        take_profit=None,
        is_runner=True,
    )
    assert out.status == "CLOSED_SL"
    assert out.exit_price == 101.0  # breakeven, no el stop original
    assert out.r_multiple == 0.0


def test_runner_trails_one_r_behind_high_water_after_2r() -> None:
    # Pico en 105.5 (2.25R) → SL trailing a 105.5 - 2 = 103.5.
    out = simulate_trade(
        _bars([(1, 101.5, 100.8), (2, 105.5, 102.0), (3, 105.0, 103.0)]),
        side="BUY",
        entry=101.0,
        stop_loss=99.0,
        take_profit=None,
        is_runner=True,
    )
    assert out.status == "CLOSED_SL"
    assert out.exit_price == 103.5
    assert out.r_multiple == 1.25


def test_runner_stop_is_monotonic() -> None:
    # Pico 105.5 → SL 103.5. Luego el precio retrocede a 103.6 (no toca) y el
    # nuevo máximo es menor: el stop NO puede aflojarse.
    out = simulate_trade(
        _bars([(1, 101.5, 100.8), (2, 105.5, 102.0), (3, 104.0, 103.6), (4, 104.0, 103.4)]),
        side="BUY",
        entry=101.0,
        stop_loss=99.0,
        take_profit=None,
        is_runner=True,
    )
    assert out.status == "CLOSED_SL"
    assert out.exit_price == 103.5


def test_trailing_uses_stop_from_bar_open_not_intrabar() -> None:
    # La vela 2 alcanza 2R y retrocede hasta 100.5 dentro de la MISMA vela.
    # En vivo s7 aún no había movido el stop, así que debe salir por el SL
    # original (99), no por el trailing que se calcula al cierre.
    out = simulate_trade(
        _bars([(1, 101.5, 100.8), (2, 105.5, 98.5)]),
        side="BUY",
        entry=101.0,
        stop_loss=99.0,
        take_profit=None,
        is_runner=True,
    )
    assert out.status == "CLOSED_SL"
    assert out.exit_price == 99.0
    assert out.r_multiple == -1.0


# ── Casos reales que motivaron el trabajo ─────────────────────────────
def test_real_short_2026_08_12_hits_stop() -> None:
    """SHORT del 12-ago: entry 7760.60, SL 7793.60, TP 7727.60 (caja SimpleFX)."""
    out = simulate_trade(
        _bars([
            (1, 7762.0, 7758.0),   # activa en 7760.60
            (2, 7775.0, 7759.0),
            (3, 7795.0, 7770.0),   # toca SL
        ]),
        side="SELL",
        entry=7760.60,
        stop_loss=7793.60,
        take_profit=7727.60,
    )
    assert out.status == "CLOSED_SL"
    assert out.r_multiple == -1.0


def test_already_open_trade_is_not_re_activated() -> None:
    # Trade ya OPEN en DB: se evalúa desde la primera vela sin exigir que
    # vuelva a tocar el precio de entrada.
    out = simulate_trade(
        _bars([(1, 98.0, 96.0)]),
        side="BUY",
        entry=101.0,
        stop_loss=99.0,
        take_profit=103.0,
        activated=True,
    )
    assert out.status == "CLOSED_SL"


# ── Orden límite (la que envía el bot tras la ruptura) ────────────────
def test_limit_order_fills_only_on_retest() -> None:
    # Ruptura cerró en 102 sobre una caja con techo 101: la compra en 101
    # queda por debajo del mercado. Subir más NO la llena.
    out = simulate_trade(
        _bars([(1, 104.0, 101.5), (2, 106.0, 102.0)]),
        side="BUY",
        entry=101.0,
        stop_loss=99.0,
        take_profit=103.0,
        reference_price=102.0,
    )
    assert out.status == "EXPIRED"
    assert out.activated is False


def test_limit_order_fill_bar_cannot_also_take_profit() -> None:
    # La vela que retrocede a 101 también marcó 103: no se sabe el orden, así
    # que no cuenta como TP. La siguiente vela sí lo toca.
    out = simulate_trade(
        _bars([(1, 103.5, 100.8), (2, 103.2, 101.5)]),
        side="BUY",
        entry=101.0,
        stop_loss=99.0,
        take_profit=103.0,
        reference_price=102.0,
    )
    assert out.status == "CLOSED_TP"
    assert out.exit_time == 2


def test_limit_short_fills_on_retest_up() -> None:
    out = simulate_trade(
        _bars([(1, 99.5, 97.0), (2, 100.2, 98.0), (3, 99.0, 97.5)]),
        side="SELL",
        entry=100.0,
        stop_loss=102.0,
        take_profit=98.0,
        reference_price=99.0,
    )
    assert out.status == "CLOSED_TP"
    assert out.exit_time == 3


def test_pending_order_expires_before_fill() -> None:
    out = simulate_trade(
        _bars([(1, 104.0, 101.5), (2, 102.0, 100.0)]),
        side="BUY",
        entry=101.0,
        stop_loss=99.0,
        take_profit=103.0,
        reference_price=102.0,
        expires_at=2,
    )
    assert out.status == "EXPIRED"


def test_resolve_open_trade_not_filled_is_not_closed() -> None:
    # OPEN en DB = orden ACEPTADA, no llenada. Si el precio nunca volvió a la
    # entrada, no puede inventarse un cierre por SL/TP.
    from types import SimpleNamespace

    from domain.strategy.outcome import resolve_trade_outcome

    trade = SimpleNamespace(
        side="BUY", entry_price=101.0, stop_loss=99.0, initial_stop_loss=99.0,
        take_profit=103.0, is_runner=False, status="OPEN",
    )
    candles = pd.DataFrame(
        [(1, 102.0, 104.0, 101.5), (2, 104.0, 106.0, 103.5)],
        columns=["time", "open", "high", "low"],
    )
    out = resolve_trade_outcome(trade, candles)
    assert out.activated is False
    assert not out.is_closed
