"""
Adaptador LLM multi-provider.

Soporta OpenAI, Anthropic, Google, Mistral, DeepSeek, Groq, Ollama, LM Studio,
OpenAI-compatible. Usa LiteLLM (a través de CrewAI) para unificar el acceso.
"""

from __future__ import annotations

from typing import Any

import httpx

from infrastructure.config.settings import LLMSettings, get_settings
from utils.logger import get_logger

log = get_logger(__name__)


def parse_model_string(model: str) -> tuple[str, str]:
    """'openai/gpt-4o-mini' → ('openai', 'gpt-4o-mini')"""
    if "/" not in model:
        raise ValueError(f"Model must be 'provider/model', got {model!r}")
    provider, name = model.split("/", 1)
    return provider, name


def build_litellm_model_string(model: str, settings: LLMSettings | None = None) -> str:
    """Adapta strings custom a lo que LiteLLM espera.

    - ollama/llama3.1 → ollama/llama3.1 (con base_url)
    - lm_studio/qwen2.5 → openai/qwen2.5 (LM Studio es OpenAI-compatible)
    - openai_compatible/... → openai/... (con base_url)
    """
    provider, name = parse_model_string(model)

    if provider == "ollama":
        return f"ollama/{name}"
    if provider == "lm_studio":
        return f"openai/{name}"
    if provider == "openai_compatible":
        return f"openai/{name}"
    return model


def get_provider_api_key(provider: str, settings=None) -> str:
    """Obtiene la API key del env correspondiente al provider.

    Lee del entorno directamente para que funcione con cualquier Settings
    (LLMSettings no tiene todas las keys, solo el Settings raíz).
    """
    import os

    s = settings or get_settings()
    # Si el settings tiene el atributo, úsalo
    if hasattr(s, "openai_api_key"):
        mapping = {
            "openai": getattr(s, "openai_api_key", ""),
            "anthropic": getattr(s, "anthropic_api_key", ""),
            "google": getattr(s, "google_api_key", ""),
            "mistral": getattr(s, "mistral_api_key", ""),
            "deepseek": getattr(s, "deepseek_api_key", ""),
            "groq": getattr(s, "groq_api_key", ""),
        }
        if provider in mapping:
            return mapping[provider] or ""
    # Fallback a env directo
    return os.getenv(f"{provider.upper()}_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")


def build_extra_llm_kwargs(model: str, settings: LLMSettings | None = None) -> dict[str, Any]:
    """Config extra para providers con base_url custom (ollama, lm_studio, etc.)."""
    s = settings or get_settings()
    provider, _ = parse_model_string(model)

    if provider == "ollama":
        return {"api_base": s.ollama_base_url}
    if provider == "lm_studio":
        return {"api_base": s.lm_studio_base_url, "api_key": "lm-studio"}
    if provider == "openai_compatible":
        return {
            "api_base": s.openai_compatible_base_url,
            "api_key": s.openai_compatible_api_key or "dummy",
        }
    return {}


class LLMHealthChecker:
    """Ping a providers locales antes del run."""

    @staticmethod
    def check_ollama(base_url: str, timeout: float = 2.0) -> bool:
        try:
            r = httpx.get(f"{base_url}/api/tags", timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False

    @staticmethod
    def check_lm_studio(base_url: str, timeout: float = 2.0) -> bool:
        try:
            r = httpx.get(f"{base_url}/models", timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False

    @classmethod
    def health_check_all(cls, settings: LLMSettings | None = None) -> dict[str, bool]:
        s = settings or get_settings()
        return {
            "ollama": cls.check_ollama(s.ollama_base_url),
            "lm_studio": cls.check_lm_studio(s.lm_studio_base_url),
        }


def get_crewai_llm(model: str, settings: LLMSettings | None = None):
    """Factory: retorna un LLM de CrewAI listo para usar.

    CrewAI acepta strings de modelo tipo 'openai/gpt-4o-mini' directamente
    (los enruta a LiteLLM). Para providers locales, hay que pasar
    `base_url` y `api_key` como kwargs.
    """
    try:
        from crewai import LLM
    except ImportError as e:
        raise ImportError("crewai no instalado. `pip install crewai[tools]>=1.9.3`") from e

    s = settings or get_settings()
    provider, _name = parse_model_string(model)
    api_key = get_provider_api_key(provider, s) or "dummy"
    extra = build_extra_llm_kwargs(model, s)

    litellm_model = build_litellm_model_string(model, s)

    log.info("LLM: model=%s provider=%s", litellm_model, provider)
    return LLM(model=litellm_model, api_key=api_key, **extra)
