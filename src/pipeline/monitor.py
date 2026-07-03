"""
Monitoreo dentro de la ventana operativa.

Corrige el bug de "arranque a destiempo": el bot ya NO depende de una hora de
inicio exacta. Depende de un RANGO [start, end] en la zona del mercado:

- Arranca ANTES de la ventana  → espera hasta el inicio.
- Arranca DENTRO de la ventana  → monitoriza inmediatamente (no espera al día
  siguiente, no corre una sola vez).
- Arranca DESPUÉS de la ventana → no opera hoy; espera la próxima ventana.
- Dentro de la ventana          → re-corre el pipeline cada `interval_s`.
- Al llegar el fin de la ventana → detiene el monitoreo hasta la próxima.

La lógica de tiempo está separada (`plan_next`) para poder testearla sin
dormir. El loop acepta `now_fn`/`sleep_fn`/`max_runs`/`max_waits` inyectables.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Literal

from domain.market_time import (
    compute_next_start,
    is_within_window,
    now_in_tz,
    parse_hhmm,
    seconds_until,
)
from utils.logger import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class WindowConfig:
    """Ventana operativa + cadencia de monitoreo."""

    start: time
    end: time
    tz: str
    interval_s: int

    @classmethod
    def from_settings(cls, settings: Any) -> WindowConfig:
        return cls(
            start=parse_hhmm(settings.operate_start),
            end=parse_hhmm(settings.operate_end),
            tz=settings.market_tz,
            interval_s=settings.monitor_interval_s,
        )


@dataclass(frozen=True)
class SleepPlan:
    state: Literal["within", "outside"]
    sleep_seconds: float
    next_start: datetime | None


def plan_next(now: datetime, window: WindowConfig) -> SleepPlan:
    """Decide qué hacer en este instante (sin efectos secundarios)."""
    if is_within_window(now, window.start, window.end):
        return SleepPlan("within", float(window.interval_s), None)
    nxt = compute_next_start(now, window.start)
    return SleepPlan("outside", seconds_until(nxt, now), nxt)


def run_monitor(
    run_once: Callable[[], Any],
    window: WindowConfig,
    *,
    now_fn: Callable[[], datetime] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    max_runs: int | None = None,
    max_waits: int | None = None,
) -> int:
    """Bucle de monitoreo. Retorna cuántas veces corrió el pipeline.

    `max_runs`/`max_waits` son cortes para tests; en producción quedan en None
    y el loop vive indefinidamente (RUN_MODE=monitor).
    """
    import time as _time

    now_fn = now_fn or (lambda: now_in_tz(window.tz))
    sleep_fn = sleep_fn or _time.sleep

    log.info(
        "Monitor iniciado | ventana %s–%s (%s) | intervalo %ds",
        window.start.strftime("%H:%M"), window.end.strftime("%H:%M"),
        window.tz, window.interval_s,
    )

    runs = 0
    waits = 0
    while True:
        now = now_fn()
        plan = plan_next(now, window)

        if plan.state == "within":
            log.info("Bot iniciado DENTRO de ventana. Entrando a modo monitorización.")
            while is_within_window(now_fn(), window.start, window.end):
                runs += 1
                log.info("Monitor: corrida #%d del pipeline", runs)
                run_once()
                if max_runs is not None and runs >= max_runs:
                    return runs
                sleep_fn(float(window.interval_s))
            log.info("Saliendo de ventana operativa. Monitoreo detenido hasta la próxima.")
        else:
            log.info(
                "Bot iniciado FUERA de ventana. Próxima ventana: %s (en %.0fs).",
                plan.next_start, plan.sleep_seconds,
            )
            waits += 1
            sleep_fn(plan.sleep_seconds)
            if max_waits is not None and waits >= max_waits:
                return runs
