"""
Taxonomía de errores del pipeline.

Cada stage falla con un StageError tipado para que el orquestador pueda
discriminar la causa (datos, red, LLM, timeout) sin `except Exception`
genéricos, y para que stage_metrics registre el tipo exacto.
"""

from __future__ import annotations


class StageError(Exception):
    """Error base de un stage del pipeline."""

    def __init__(self, stage: str, message: str, cause: Exception | None = None):
        self.stage = stage
        self.cause = cause
        super().__init__(f"[{stage}] {message}")


class StageTimeoutError(StageError):
    """El stage excedió su timeout configurado."""


class StageDataError(StageError):
    """Datos inválidos o insuficientes (ValidationError, DomainError, etc.)."""


class StageNetworkError(StageError):
    """Fallo de red hablando con broker, data source o scraper."""


class StageLLMError(StageError):
    """Fallo del proveedor LLM o del crew de agentes."""
