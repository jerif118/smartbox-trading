"""Tests del monitoreo y de la lógica de ventana operativa."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from domain.market_time import (
    compute_next_start,
    is_within_window,
    parse_hhmm,
    seconds_until,
)
from pipeline.monitor import WindowConfig, plan_next, run_monitor, seconds_to_next_bar

NY = ZoneInfo("America/New_York")
WINDOW = WindowConfig(
    start=time(9, 0), end=time(12, 0), tz="America/New_York", interval_s=300, grace_s=5.0
)


# ── parse_hhmm ────────────────────────────────────────────────────────
def test_parse_hhmm() -> None:
    assert parse_hhmm("10:15") == time(10, 15)
    assert parse_hhmm("9:00") == time(9, 0)
    assert parse_hhmm("23:59:30") == time(23, 59, 30)


# ── is_within_window ──────────────────────────────────────────────────
def test_within_window_inside() -> None:
    now = datetime(2026, 6, 15, 10, 15, tzinfo=NY)
    assert is_within_window(now, time(9, 0), time(12, 0)) is True


def test_within_window_before() -> None:
    now = datetime(2026, 6, 15, 8, 30, tzinfo=NY)
    assert is_within_window(now, time(9, 0), time(12, 0)) is False


def test_within_window_after() -> None:
    now = datetime(2026, 6, 15, 13, 0, tzinfo=NY)
    assert is_within_window(now, time(9, 0), time(12, 0)) is False


def test_within_window_boundaries_inclusive() -> None:
    assert is_within_window(time(9, 0), time(9, 0), time(12, 0)) is True
    assert is_within_window(time(12, 0), time(9, 0), time(12, 0)) is True


# ── compute_next_start / seconds_until ────────────────────────────────
def test_compute_next_start_today_when_before() -> None:
    now = datetime(2026, 6, 15, 8, 0, tzinfo=NY)
    nxt = compute_next_start(now, time(9, 0))
    assert nxt == datetime(2026, 6, 15, 9, 0, tzinfo=NY)


def test_compute_next_start_tomorrow_when_after() -> None:
    now = datetime(2026, 6, 15, 13, 0, tzinfo=NY)
    nxt = compute_next_start(now, time(9, 0))
    assert nxt == datetime(2026, 6, 16, 9, 0, tzinfo=NY)


def test_seconds_until_never_negative() -> None:
    now = datetime(2026, 6, 15, 13, 0, tzinfo=NY)
    past = datetime(2026, 6, 15, 12, 0, tzinfo=NY)
    assert seconds_until(past, now) == 0.0


# ── seconds_to_next_bar ───────────────────────────────────────────────
def test_seconds_to_next_bar_aligned() -> None:
    """A las 10:15:00 exactas la próxima vela de 5min cierra a las 10:20 →
    300s + 5s de gracia."""
    now = datetime(2026, 6, 15, 10, 15, 0, tzinfo=NY)
    assert seconds_to_next_bar(now, 300, 5.0) == 305.0


def test_seconds_to_next_bar_mid_candle() -> None:
    """A las 10:17:30 faltan 150s para el cierre de las 10:20 → 150 + 5."""
    now = datetime(2026, 6, 15, 10, 17, 30, tzinfo=NY)
    assert seconds_to_next_bar(now, 300, 5.0) == 155.0


def test_seconds_to_next_bar_justo_tras_cierre() -> None:
    """A las 10:20:02 el cierre de las 10:20 ya pasó → apunta al de las
    10:25 (+5s de gracia) = 303s."""
    now = datetime(2026, 6, 15, 10, 20, 2, tzinfo=NY)
    assert seconds_to_next_bar(now, 300, 5.0) == pytest.approx(303.0)


# ── plan_next ─────────────────────────────────────────────────────────
def test_plan_next_within() -> None:
    now = datetime(2026, 6, 15, 10, 15, tzinfo=NY)
    plan = plan_next(now, WINDOW)
    assert plan.state == "within"
    # alineado al próximo cierre de vela (10:20) + gracia, no interval fijo
    assert plan.sleep_seconds == 305.0


def test_plan_next_outside_before() -> None:
    now = datetime(2026, 6, 15, 8, 0, tzinfo=NY)
    plan = plan_next(now, WINDOW)
    assert plan.state == "outside"
    assert plan.next_start == datetime(2026, 6, 15, 9, 0, tzinfo=NY)
    assert plan.sleep_seconds == 3600.0  # 1h


# ── run_monitor: arranque DENTRO de ventana ───────────────────────────
def test_monitor_starts_immediately_when_inside() -> None:
    """Arranca a las 10:15 dentro de 09:00-12:00 → monitoriza ya, no espera."""
    now = datetime(2026, 6, 15, 10, 15, tzinfo=NY)
    runs: list[int] = []
    slept: list[float] = []

    n = run_monitor(
        run_once=lambda: runs.append(1),
        window=WINDOW,
        now_fn=lambda: now,            # tiempo congelado dentro de ventana
        sleep_fn=lambda s: slept.append(s),
        max_runs=3,                    # corta tras 3 corridas
    )
    assert n == 3
    assert len(runs) == 3              # corrió varias veces, no solo una
    assert slept == [305.0, 305.0]    # durmió hasta el próximo cierre de vela + gracia


# ── run_monitor: arranque FUERA de ventana ────────────────────────────
def test_monitor_waits_when_outside_does_not_run() -> None:
    """Arranca a las 08:00 antes de la ventana → no opera, espera al inicio."""
    now = datetime(2026, 6, 15, 8, 0, tzinfo=NY)
    runs: list[int] = []
    slept: list[float] = []

    n = run_monitor(
        run_once=lambda: runs.append(1),
        window=WINDOW,
        now_fn=lambda: now,
        sleep_fn=lambda s: slept.append(s),
        max_waits=1,                   # corta tras la primera espera
    )
    assert n == 0
    assert runs == []                  # NO operó fuera de ventana
    assert slept == [3600.0]           # esperó hasta el inicio (09:00)


# ── run_monitor: fin de ventana detiene el monitoreo ──────────────────
def test_monitor_stops_at_end_of_window() -> None:
    """Dentro al inicio, luego se sale: corre una vez y detiene el monitoreo."""
    times = iter([
        datetime(2026, 6, 15, 11, 59, tzinfo=NY),  # plan_next: within
        datetime(2026, 6, 15, 11, 59, tzinfo=NY),  # inner while: within → corre
        datetime(2026, 6, 15, 11, 59, tzinfo=NY),  # cálculo de espera alineada
        datetime(2026, 6, 15, 12, 1, tzinfo=NY),   # inner while: fuera → sale
        datetime(2026, 6, 15, 12, 1, tzinfo=NY),   # outer: outside → espera
    ])
    runs: list[int] = []
    n = run_monitor(
        run_once=lambda: runs.append(1),
        window=WINDOW,
        now_fn=lambda: next(times),
        sleep_fn=lambda s: None,
        max_waits=1,
    )
    assert n == 1
    assert runs == [1]


# ── WindowConfig.from_settings ────────────────────────────────────────
def test_window_config_from_settings() -> None:
    class FakeSettings:
        operate_start = "10:00"
        operate_end = "12:00"
        market_tz = "America/New_York"
        monitor_interval_s = 120
        monitor_grace_s = 3.0

    w = WindowConfig.from_settings(FakeSettings())
    assert w.start == time(10, 0)
    assert w.end == time(12, 0)
    assert w.interval_s == 120
    assert w.grace_s == 3.0


# ── run_monitor con wake_event (gatillo por socket) ──────────────────
def test_monitor_wake_event_dispara_corrida_inmediata() -> None:
    """Con el evento seteado (vela cerrada por socket) el monitor no espera
    el timeout de reloj: corre de inmediato y limpia el evento."""
    import threading

    now = datetime(2026, 6, 15, 10, 15, tzinfo=NY)
    runs: list[int] = []
    slept: list[float] = []
    event = threading.Event()
    event.set()  # el socket ya avisó de un cierre de vela

    import time as _time
    t0 = _time.monotonic()
    n = run_monitor(
        run_once=lambda: runs.append(1),
        window=WINDOW,
        now_fn=lambda: now,
        sleep_fn=lambda s: slept.append(s),
        max_runs=2,
        wake_event=event,
    )
    elapsed = _time.monotonic() - t0

    assert n == 2
    assert elapsed < 2.0            # no durmió los 305s del reloj
    assert slept == [5.0]           # solo el margen de gracia tras el despertar
    assert not event.is_set()       # el evento quedó limpio tras despertar


def test_monitor_wake_event_timeout_actua_como_reloj() -> None:
    """Sin señales del socket, la espera vence sola (fallback por reloj)."""
    import threading

    times = iter([
        datetime(2026, 6, 15, 10, 15, tzinfo=NY),  # plan_next: within
        datetime(2026, 6, 15, 10, 15, tzinfo=NY),  # inner while → corre
        datetime(2026, 6, 15, 10, 15, 0, 500000, tzinfo=NY),  # cálculo del timeout
        datetime(2026, 6, 15, 12, 1, tzinfo=NY),   # inner while: fuera → sale
        datetime(2026, 6, 15, 12, 1, tzinfo=NY),   # outer: outside → espera
    ])
    runs: list[int] = []

    from typing import ClassVar

    class InstantEvent(threading.Event):
        """Event cuyo wait retorna al instante (simula timeout vencido)."""
        timeouts: ClassVar[list[float]] = []

        def wait(self, timeout: float | None = None) -> bool:
            InstantEvent.timeouts.append(timeout)
            return False  # False = venció el timeout, no hubo señal

    n = run_monitor(
        run_once=lambda: runs.append(1),
        window=WINDOW,
        now_fn=lambda: next(times),
        sleep_fn=lambda s: None,
        max_waits=1,
        wake_event=InstantEvent(),
    )
    assert n == 1
    assert runs == [1]
    # el timeout pedido fue hasta el próximo cierre de vela + gracia
    assert InstantEvent.timeouts[0] == pytest.approx(304.5)
