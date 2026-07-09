"""
Monitoreo dentro de la ventana operativa.

Corrige el bug de "arranque a destiempo": el bot ya NO depende de una hora de
inicio exacta. Depende de un RANGO [start, end] en la zona del mercado:

- Arranca ANTES de la ventana  → espera hasta el inicio.
- Arranca DENTRO de la ventana  → monitoriza inmediatamente (no espera al día
  siguiente, no corre una sola vez).
- Arranca DESPUÉS de la ventana → no opera hoy; espera la próxima ventana.
- Dentro de la ventana          → re-corre el pipeline ALINEADO al cierre de
  vela: en el próximo múltiplo de `interval_s` (epoch) + `grace_s`, no cada
  N segundos desde el arranque. Así la señal se evalúa segundos después del
  cierre, no hasta 5 min tarde.
- Con `wake_event` (socket de Capital.com): el cierre de vela despierta el
  loop al instante; el reloj alineado queda como fallback si el socket calla.
- Al llegar el fin de la ventana → detiene el monitoreo hasta la próxima.

La lógica de tiempo está separada (`plan_next`/`seconds_to_next_bar`) para
poder testearla sin dormir. El loop acepta `now_fn`/`sleep_fn`/`max_runs`/
`max_waits` inyectables.
"""

from __future__ import annotations

import threading
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
    grace_s: float = 5.0

    @classmethod
    def from_settings(cls, settings: Any) -> WindowConfig:
        return cls(
            start=parse_hhmm(settings.operate_start),
            end=parse_hhmm(settings.operate_end),
            tz=settings.market_tz,
            interval_s=settings.monitor_interval_s,
            grace_s=getattr(settings, "monitor_grace_s", 5.0),
        )


def seconds_to_next_bar(now: datetime, interval_s: int, grace_s: float) -> float:
    """Segundos hasta el próximo cierre de vela + margen de gracia.

    Las velas cierran en múltiplos de `interval_s` en epoch UTC (una vela de
    5 min cierra a :00, :05, :10...). Correr `grace_s` después del cierre
    garantiza que la vela ya esté disponible por REST.
    """
    epoch = now.timestamp()
    next_close = (int(epoch // interval_s) + 1) * interval_s
    return max(0.0, next_close + grace_s - epoch)


@dataclass(frozen=True)
class SleepPlan:
    state: Literal["within", "outside"]
    sleep_seconds: float
    next_start: datetime | None


def plan_next(now: datetime, window: WindowConfig) -> SleepPlan:
    """Decide qué hacer en este instante (sin efectos secundarios)."""
    if is_within_window(now, window.start, window.end):
        return SleepPlan(
            "within", seconds_to_next_bar(now, window.interval_s, window.grace_s), None
        )
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
    wake_event: threading.Event | None = None,
) -> int:
    """Bucle de monitoreo. Retorna cuántas veces corrió el pipeline.

    `max_runs`/`max_waits` son cortes para tests; en producción quedan en None
    y el loop vive indefinidamente (RUN_MODE=monitor).

    `wake_event` (opcional): lo setea el stream de Capital.com al cerrar una
    vela → la espera termina al instante. Sin evento (o socket caído) la
    espera vence sola en el próximo cierre de vela por reloj (fallback).
    """
    import time as _time

    now_fn = now_fn or (lambda: now_in_tz(window.tz))
    sleep_fn = sleep_fn or _time.sleep

    log.info(
        "Monitor iniciado | ventana %s–%s (%s) | intervalo %ds | gatillo: %s",
        window.start.strftime("%H:%M"), window.end.strftime("%H:%M"),
        window.tz, window.interval_s,
        "socket+reloj" if wake_event is not None else "reloj",
    )

    def _wait(seconds: float) -> None:
        if wake_event is None:
            sleep_fn(seconds)
            return
        woke = wake_event.wait(timeout=seconds)
        wake_event.clear()
        if woke:
            log.info("Monitor: despertado por cierre de vela (socket)")
            # El quote cruza la frontera <1s tras el cierre; dar un margen
            # para que la vela cerrada ya esté disponible por REST.
            sleep_fn(window.grace_s)

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
                _wait(seconds_to_next_bar(now_fn(), window.interval_s, window.grace_s))
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
