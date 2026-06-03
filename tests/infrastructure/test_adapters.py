"""Tests de los adapters de broker y LLM.

Mockeamos `requests` directamente (los adapters usan requests, no httpx).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from infrastructure.broker.simplefx.adapter import SimpleFXAdapter
from infrastructure.broker.capital.adapter import CapitalAdapter
from infrastructure.config.settings import get_settings, reset_settings_cache
from infrastructure.llm.provider import (
    LLMHealthChecker,
    build_litellm_model_string,
    parse_model_string,
)


@pytest.fixture(autouse=True)
def setup_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ID", "test-client-id")
    monkeypatch.setenv("KEY", "test-client-key")
    monkeypatch.setenv("SIMPLE_ACCOUNT", "12345")
    reset_settings_cache()
    yield
    reset_settings_cache()


# ── LLM provider string parsing ───────────────────────────────────────
def test_parse_model_string_openai() -> None:
    p, m = parse_model_string("openai/gpt-4o-mini")
    assert p == "openai"
    assert m == "gpt-4o-mini"


def test_parse_model_string_ollama() -> None:
    p, m = parse_model_string("ollama/llama3.1")
    assert p == "ollama"
    assert m == "llama3.1"


def test_parse_model_string_invalid() -> None:
    with pytest.raises(ValueError):
        parse_model_string("gpt-4o-mini")


def test_build_litellm_string_openai() -> None:
    assert build_litellm_model_string("openai/gpt-4o-mini") == "openai/gpt-4o-mini"


def test_build_litellm_string_ollama() -> None:
    assert build_litellm_model_string("ollama/llama3.1") == "ollama/llama3.1"


def test_build_litellm_string_lm_studio() -> None:
    assert build_litellm_model_string("lm_studio/qwen2.5") == "openai/qwen2.5"


def test_build_litellm_string_openai_compatible() -> None:
    assert build_litellm_model_string("openai_compatible/custom") == "openai/custom"


# ── LLM health check ──────────────────────────────────────────────────
def test_ollama_health_check_ok() -> None:
    with patch("infrastructure.llm.provider.httpx.get") as mock_get:
        mock_get.return_value = MagicMock(status_code=200)
        assert LLMHealthChecker.check_ollama("http://localhost:11434") is True


def test_ollama_health_check_fail() -> None:
    with patch("infrastructure.llm.provider.httpx.get") as mock_get:
        mock_get.return_value = MagicMock(status_code=500)
        assert LLMHealthChecker.check_ollama("http://localhost:11434") is False


def test_ollama_health_check_connection_error() -> None:
    with patch("infrastructure.llm.provider.httpx.get") as mock_get:
        mock_get.side_effect = Exception("connection refused")
        assert LLMHealthChecker.check_ollama("http://localhost:11434") is False


# ── SimpleFX adapter (DRY_RUN) ─────────────────────────────────────────
def test_simplefx_dry_run_place_order() -> None:
    get_settings().dry_run = True
    adapter = SimpleFXAdapter()
    order_id = adapter.place_order(
        symbol="US500", side="BUY", volume=0.5,
        entry_price=5000.0, stop_loss=4990.0, take_profit=5010.0,
    )
    assert order_id.startswith("DRY-")


def test_simplefx_modify_dry_run() -> None:
    get_settings().dry_run = True
    adapter = SimpleFXAdapter()
    # No debe lanzar excepción
    adapter.modify_order("DRY-123", stop_loss=4995.0, take_profit=5015.0)


def test_simplefx_real_place_order() -> None:
    """Mockeando requests.post, simulamos un login + order real."""
    get_settings().dry_run = False
    adapter = SimpleFXAdapter()

    with patch("infrastructure.broker.simplefx.adapter.requests") as mock_requests:
        # Login response
        login_resp = MagicMock()
        login_resp.json.return_value = {"data": {"token": "fake-token"}}
        login_resp.raise_for_status = MagicMock()
        login_resp.status_code = 200
        # Order response
        order_resp = MagicMock()
        order_resp.json.return_value = {"data": {"id": 99999}}
        order_resp.raise_for_status = MagicMock()
        order_resp.status_code = 200

        mock_requests.post.side_effect = [login_resp, order_resp]
        mock_requests.put.return_value = order_resp

        order_id = adapter.place_order(
            symbol="US500", side="BUY", volume=0.5,
            entry_price=5000.0, stop_loss=4990.0, take_profit=5010.0,
        )
        assert order_id == "99999"
        assert mock_requests.post.call_count == 2  # login + order


# ── Capital adapter ───────────────────────────────────────────────────
def test_capital_login_and_fetch() -> None:
    """Mockeando requests, validamos el flujo completo."""
    adapter = CapitalAdapter()

    with patch("infrastructure.broker.capital.adapter.requests") as mock_requests:
        # Login
        login_resp = MagicMock()
        login_resp.headers = {"CST": "fake-cst", "X-SECURITY-TOKEN": "fake-xst"}
        login_resp.json.return_value = {"accountId": "x"}
        login_resp.raise_for_status = MagicMock()

        # Prices
        prices_resp = MagicMock()
        prices_resp.json.return_value = {
            "prices": [
                {
                    "snapshotTime": "2026-06-01T14:55:00",
                    "snapshotTimeUTC": "2026-06-01T18:55:00",
                    "openPrice": {"bid": 5000.0, "ask": 5000.5},
                    "closePrice": {"bid": 5005.0, "ask": 5005.5},
                    "highPrice": {"bid": 5010.0, "ask": 5010.5},
                    "lowPrice": {"bid": 4995.0, "ask": 4995.5},
                    "lastTradedVolume": 100,
                }
            ]
        }
        prices_resp.raise_for_status = MagicMock()

        mock_requests.post.return_value = login_resp
        mock_requests.get.return_value = prices_resp

        import pandas as pd
        from_ts = int(pd.Timestamp("2026-06-01T18:00:00", tz="UTC").timestamp())
        to_ts = int(pd.Timestamp("2026-06-01T19:00:00", tz="UTC").timestamp())
        df = adapter.get_candles("US500", "MINUTE_5", from_ts, to_ts)
        assert len(df) == 1
        assert "close" in df.columns
        assert df["close"].iloc[0] == 5005.25
