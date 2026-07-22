"""Tests de los agentes y tools."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from application.agents.agents import (
    build_risk_agent,
    build_trader_agent,
)
from application.agents.tools import (
    DrawdownGuardTool,
    analyze_multi_timeframe,
)
from infrastructure.config.settings import LLMSettings, reset_settings_cache


@pytest.fixture(autouse=True)
def setup_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-for-agents")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def llm_settings() -> LLMSettings:
    return LLMSettings()


# ── Validación de configuración ───────────────────────────────────────
def test_llm_settings_validation() -> None:
    """Settings con formato provider/model es OK."""
    s = LLMSettings().model_copy(
        update={"trader": "anthropic/claude-3-5-sonnet", "risk_analyst": "ollama/llama3.1"}
    )
    assert "anthropic" in s.trader
    assert s.risk_analyst.startswith("ollama/")


def test_llm_settings_invalid_format(monkeypatch) -> None:
    """Un modelo sin formato provider/model debe fallar la validación."""
    monkeypatch.setenv("AGENT_TRADER_MODEL", "invalid-format")
    reset_settings_cache()
    with pytest.raises(Exception):
        LLMSettings()


# ── Construcción de agentes (nombres simples, por símbolo) ───────────
@pytest.mark.parametrize("symbol", ["US500", "US100"])
def test_build_trader_agent_simple_name(llm_settings: LLMSettings, symbol: str) -> None:
    agent = build_trader_agent(symbol, llm_settings)
    assert agent.role == f"Trader_{symbol}"
    assert agent.allow_delegation is False


@pytest.mark.parametrize("symbol", ["US500", "US100"])
def test_build_risk_agent_simple_name(llm_settings: LLMSettings, symbol: str) -> None:
    agent = build_risk_agent(symbol, llm_settings)
    assert agent.role == f"Risk_{symbol}"
    assert agent.allow_delegation is False


def test_agent_names_have_no_emoji_or_dashes(llm_settings: LLMSettings) -> None:
    """Los nombres deben ser simples (sin emojis ni guiones largos)."""
    roles = [
        build_trader_agent("US500", llm_settings).role,
        build_risk_agent("US100", llm_settings).role,
    ]
    for role in roles:
        assert "—" not in role  # em-dash
        assert "–" not in role  # en-dash
        assert role.isascii()


# ── Tools / helpers: funcionalidad pura ──────────────────────────────
# (El scoring de confluence y el MTF puro tienen tests en tests/domain/signals/;
#  aquí se cubre el helper de descarga MTF y la DrawdownGuardTool.)
def test_drawdown_guard_proceed() -> None:
    tool = DrawdownGuardTool()
    import json

    result = json.loads(tool._run(max_daily_loss=500, current_daily_pnl=-100))
    assert result["recommendation"] == "PROCEED"
    assert result["exceeded"] is False


def test_drawdown_guard_veto() -> None:
    tool = DrawdownGuardTool()
    import json

    result = json.loads(tool._run(max_daily_loss=500, current_daily_pnl=-600))
    assert result["recommendation"] == "VETO"
    assert result["exceeded"] is True


def test_drawdown_guard_lee_db_de_settings(tmp_path, monkeypatch) -> None:
    """La tool usa settings.db_path (no una ruta relativa hardcodeada):
    el PnL realizado hoy en esa DB cuenta para el drawdown."""
    import json
    import sqlite3
    from datetime import UTC, datetime

    db_file = tmp_path / "guard.db"
    monkeypatch.setenv("DB_PATH", str(db_file))
    reset_settings_cache()

    with sqlite3.connect(db_file) as conn:
        conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, ts_close TEXT, pnl REAL)")
        conn.execute(
            "INSERT INTO trades (ts_close, pnl) VALUES (?, ?)",
            (datetime.now(UTC).isoformat(), -600.0),
        )

    tool = DrawdownGuardTool()
    result = json.loads(tool._run(max_daily_loss=500, current_daily_pnl=0.0))
    assert result["daily_pnl"] == -600.0
    assert result["recommendation"] == "VETO"


def test_analyze_multi_timeframe_aligned() -> None:
    import pandas as pd

    with patch("infrastructure.broker.capital.adapter.CapitalAdapter.get_candles") as mock:
        # 4h/1h/15min todos alcistas
        mock.return_value = pd.DataFrame(
            {
                "time": range(50),
                "close": [100 + i for i in range(50)],  # siempre subiendo
                "open": [100 + i for i in range(50)],
                "high": [100 + i + 0.5 for i in range(50)],
                "low": [100 + i - 0.5 for i in range(50)],
                "volume": [100] * 50,
            }
        )
        result = analyze_multi_timeframe("US500", "LONG")
        assert result["mtf_alignment"] == "ALIGNED"
        assert result["htf_bias"] == "BULLISH"
        assert result["veto_recommended"] is False


def test_analyze_multi_timeframe_counter_signal() -> None:
    import pandas as pd

    with patch("infrastructure.broker.capital.adapter.CapitalAdapter.get_candles") as mock:
        mock.return_value = pd.DataFrame(
            {
                "time": range(50),
                "close": [100 - i for i in range(50)],  # siempre bajando
                "open": [100 - i for i in range(50)],
                "high": [100 - i + 0.5 for i in range(50)],
                "low": [100 - i - 0.5 for i in range(50)],
                "volume": [100] * 50,
            }
        )
        result = analyze_multi_timeframe("US500", "LONG")
        assert result["htf_bias"] == "BEARISH"
        assert result["mtf_alignment"] == "COUNTER"
        assert result["veto_recommended"] is True
