"""Configuración del LLM que usan los agentes."""

from __future__ import annotations

import pytest

from infrastructure.llm.provider import _needs_reasoning_off


@pytest.mark.parametrize(
    ("provider", "name", "expected"),
    [
        # gpt-5.x con tools en /chat/completions da 400 salvo reasoning_effort="none"
        ("openai", "gpt-5.6-luna", True),
        ("openai", "o3-mini", True),
        ("openai", "gpt-4o-mini", False),  # no acepta reasoning_effort
        ("ollama", "gpt-5-local", False),
    ],
)
def test_reasoning_off_only_for_openai_reasoning_models(provider, name, expected) -> None:
    assert _needs_reasoning_off(provider, name) is expected
