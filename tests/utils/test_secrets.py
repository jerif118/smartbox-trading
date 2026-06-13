"""Tests de endurecimiento de secretos (Fase 8)."""

from __future__ import annotations

import logging

import pytest

from infrastructure.config.settings import Settings, reset_settings_cache
from utils.logger import RedactSecretsFilter


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    reset_settings_cache()
    yield
    reset_settings_cache()


def _record(msg: str, *args) -> logging.LogRecord:
    return logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=args, exc_info=None,
    )


def test_redacts_bearer_tokens(monkeypatch):
    f = RedactSecretsFilter()
    rec = _record("Authorization: Bearer abc123XYZsecreto")
    f.filter(rec)
    assert "abc123XYZ" not in rec.getMessage()
    assert "Bearer ***REDACTED***" in rec.getMessage()


def test_redacts_env_secret_values(monkeypatch):
    monkeypatch.setenv("PASSWORD", "MiClaveSuperSecreta1")
    f = RedactSecretsFilter()
    rec = _record("login fallido con password=%s", "MiClaveSuperSecreta1")
    f.filter(rec)
    assert "MiClaveSuperSecreta1" not in rec.getMessage()
    assert "***REDACTED***" in rec.getMessage()


def test_short_values_not_redacted(monkeypatch):
    monkeypatch.setenv("KEY", "abc")  # len <= 6: no se redacta (evita falsos positivos)
    f = RedactSecretsFilter()
    rec = _record("estado abc del mercado")
    f.filter(rec)
    assert rec.getMessage() == "estado abc del mercado"


def test_validate_credentials_live_requires_simplefx(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.delenv("ID", raising=False)
    monkeypatch.delenv("KEY", raising=False)
    monkeypatch.delenv("SIMPLE_ACCOUNT", raising=False)
    reset_settings_cache()
    # _env_file=None: ignora el .env del repo (puede tener credenciales reales)
    s = Settings(_env_file=None)
    problems = s.validate_credentials()
    assert any("SimpleFX" in p for p in problems)


def test_validate_credentials_dry_run_ok(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    reset_settings_cache()
    s = Settings()
    assert s.validate_credentials() == []


def test_validate_credentials_openai_compatible_needs_url_and_key(monkeypatch):
    monkeypatch.setenv("AGENT_MTFA_MODEL", "openai_compatible/mi-modelo")
    monkeypatch.delenv("OPENAI_COMPATIBLE_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    reset_settings_cache()
    s = Settings()
    problems = s.validate_credentials()
    assert any("OPENAI_COMPATIBLE_BASE_URL" in p for p in problems)
    assert any("OPENAI_COMPATIBLE_API_KEY" in p for p in problems)


def test_validate_credentials_invalid_reality(monkeypatch):
    monkeypatch.setenv("SIMPLE_REALITY", "PRODUCCION")
    reset_settings_cache()
    s = Settings()
    assert any("SIMPLE_REALITY" in p for p in s.validate_credentials())


def test_scraper_network_error_returns_empty(monkeypatch):
    import requests as req

    from infrastructure.data_sources import scrapers

    def boom(*args, **kwargs):
        raise req.ConnectionError("sin red")

    monkeypatch.setattr(scrapers.requests, "get", boom)
    adapter = scrapers.DuckDuckGoNewsAdapter()
    assert adapter.search("sp500") == []
