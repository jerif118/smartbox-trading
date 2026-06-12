"""
Wrapper de ejecución de stages: timeout, métricas y traducción de errores.

Uso:
    from pipeline.runner import run_stage

    out = run_stage("s1_ingest:US500", run_id, stage_ingest, ingest_input,
                    timeout_s=settings.stage_timeout_s)

- Mide la duración y la persiste en stage_metrics (ok | error | timeout).
- Si el stage excede timeout_s lanza StageTimeoutError. El thread del stage
  es daemon: un stage colgado no impide que el proceso del cron termine.
- Traduce excepciones crudas a la taxonomía de pipeline.errors para que el
  orquestador nunca necesite `except Exception`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

import requests
from pydantic import ValidationError

from domain.errors import DomainError
from infrastructure.persistence.sqlite import stage_metrics_repo
from pipeline.errors import (
    StageDataError,
    StageError,
    StageNetworkError,
    StageTimeoutError,
)
from utils.logger import get_logger

log = get_logger(__name__)

T = TypeVar("T")


def translate_exception(stage: str, exc: Exception) -> StageError:
    """Traduce una excepción cruda a la taxonomía de StageError."""
    if isinstance(exc, StageError):
        return exc
    if isinstance(exc, requests.RequestException | ConnectionError | TimeoutError):
        return StageNetworkError(stage, str(exc), cause=exc)
    if isinstance(exc, ValidationError | DomainError | ValueError | KeyError | TypeError):
        return StageDataError(stage, str(exc), cause=exc)
    return StageError(stage, f"{type(exc).__name__}: {exc}", cause=exc)


def _persist_metric(
    run_id: str,
    stage: str,
    status: str,
    started_at: str,
    duration_ms: int,
    error_type: str | None = None,
    error_msg: str | None = None,
) -> None:
    # La métrica nunca debe tumbar el stage que la genera.
    try:
        stage_metrics_repo.insert_stage_metric(
            run_id, stage, status, started_at, duration_ms,
            error_type=error_type, error_msg=error_msg[:500] if error_msg else None,
        )
    except Exception as e:  # noqa: BLE001 — best-effort por diseño
        log.warning("No se pudo persistir métrica de %s: %s", stage, e)


def run_stage(
    name: str,
    run_id: str,
    fn: Callable[..., T],
    *args: Any,
    timeout_s: float | None = None,
    **kwargs: Any,
) -> T:
    """Ejecuta un stage con timeout y métricas. Lanza StageError tipado al fallar."""
    started_at = datetime.now(UTC).isoformat()
    t0 = time.monotonic()
    outcome: dict[str, Any] = {}

    def _target() -> None:
        try:
            outcome["value"] = fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 — se traduce y re-lanza en el caller
            outcome["error"] = e

    worker = threading.Thread(target=_target, name=f"stage-{name}", daemon=True)
    worker.start()
    worker.join(timeout_s)
    duration_ms = int((time.monotonic() - t0) * 1000)

    if worker.is_alive():
        msg = f"timeout tras {timeout_s:.0f}s"
        log.error("Stage %s: %s", name, msg)
        _persist_metric(run_id, name, "timeout", started_at, duration_ms,
                        "StageTimeoutError", msg)
        raise StageTimeoutError(name, msg)

    if "error" in outcome:
        err = translate_exception(name, outcome["error"])
        log.error("Stage %s falló (%s): %s", name, type(err).__name__, err,
                  exc_info=outcome["error"])
        _persist_metric(run_id, name, "error", started_at, duration_ms,
                        type(err).__name__, str(err))
        raise err

    _persist_metric(run_id, name, "ok", started_at, duration_ms)
    log.debug("Stage %s OK en %dms", name, duration_ms)
    return outcome["value"]
