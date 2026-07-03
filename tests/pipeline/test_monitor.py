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
from pipeline.monitor import WindowConfig, plan_next, run_monitor

NY = ZoneInfo("America/New_York")
WINDOW = WindowConfig(start=time(9, 0), end=time(12, 0), tz="America/New_York", interval_s=300)


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


# ── plan_next ─────────────────────────────────────────────────────────
def test_plan_next_within() -> None:
    now = datetime(2026, 6, 15, 10, 15, tzinfo=NY)
    plan = plan_next(now, WINDOW)
    assert plan.state == "within"
    assert plan.sleep_seconds == 300.0


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
    assert slept == [300.0, 300.0]    # durmió el intervalo entre corridas


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

    w = WindowConfig.from_settings(FakeSettings())
    assert w.start == time(10, 0)
    assert w.end == time(12, 0)
    assert w.interval_s == 120
