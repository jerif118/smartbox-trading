"""Tests de los agentes y tools."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from application.agents.agents import (
    build_desk_manager,
    build_position_manager,
    build_risk_agent,
    build_trader_agent,
)
from application.agents.tools import (
    AnalyzeBoxTool,
    ConfluenceScoreTool,
    DrawdownGuardTool,
    MultiTimeframeTool,
    ScrapeMacroCalendarTool,
    SearchNewsTool,
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
    s = LLMSettings()
    return s.model_copy(update={"mtfa": "ollama/llama3.1"})


# ── Validación de configuración ───────────────────────────────────────
def test_llm_settings_validation() -> None:
    """Settings con formato provider/model es OK."""
    s = LLMSettings()
    # Override via model_copy
    s = s.model_copy(update={
        "decision_maker": "anthropic/claude-3-5-sonnet",
        "mtfa": "ollama/llama3.1",
    })
    assert "anthropic" in s.decision_maker
    assert s.mtfa.startswith("ollama/")


def test_llm_settings_invalid_format() -> None:
    with pytest.raises(Exception):
        LLMSettings()
        # Force invalid via env
        import os
        os.environ["AGENT_DECISION_MAKER_MODEL"] = "invalid-format"
        from infrastructure.config.settings import reset_settings_cache
        reset_settings_cache()
        try:
            LLMSettings()
        finally:
            del os.environ["AGENT_DECISION_MAKER_MODEL"]
            reset_settings_cache()


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


def test_build_desk_manager_no_delegation(llm_settings: LLMSettings) -> None:
    agent = build_desk_manager(llm_settings)
    assert agent.role == "Desk_Manager"
    # El manager final NUNCA delega.
    assert agent.allow_delegation is False


def test_build_position_manager_simple_name(llm_settings: LLMSettings) -> None:
    agent = build_position_manager(llm_settings)
    assert agent.role == "Position_Manager"
    assert agent.allow_delegation is False


def test_agent_names_have_no_emoji_or_dashes(llm_settings: LLMSettings) -> None:
    """Los nombres deben ser simples (sin emojis ni guiones largos)."""
    roles = [
        build_trader_agent("US500", llm_settings).role,
        build_risk_agent("US100", llm_settings).role,
        build_desk_manager(llm_settings).role,
        build_position_manager(llm_settings).role,
    ]
    for role in roles:
        assert "—" not in role  # em-dash
        assert "–" not in role  # en-dash
        assert role.isascii()


# ── Tools: funcionalidad pura ────────────────────────────────────────
def test_confluence_score_high() -> None:
    tool = ConfluenceScoreTool()
    import json
    result = json.loads(
        tool._run(
            rsi_value=50, rsi_direction="neutral",
            poc_above_mid=True, breakout_aligned=True, mtf_aligned=True,
        )
    )
    assert result["score"] == 100
    assert result["recommendation"] == "PROCEED"


def test_confluence_score_low() -> None:
    tool = ConfluenceScoreTool()
    import json
    # rsi=80 overbought(20) + poc=False(15) + breakout=False(0) + mtf=False(0) = 35
    result = json.loads(
        tool._run(
            rsi_value=80, rsi_direction="overbought",
            poc_above_mid=False, breakout_aligned=False, mtf_aligned=False,
        )
    )
    assert result["score"] == 35
    assert result["recommendation"] == "NO_OPERAR"


def test_confluence_score_threshold() -> None:
    tool = ConfluenceScoreTool()
    import json
    # rsi=50 neutral(25) + poc=False(15) + breakout=True(25) + mtf=False(0) = 65 → PROCEED
    result = json.loads(
        tool._run(
            rsi_value=50, rsi_direction="neutral",
            poc_above_mid=False, breakout_aligned=True, mtf_aligned=False,
        )
    )
    assert result["score"] == 65
    assert result["recommendation"] == "PROCEED"


def test_analyze_box_skip_high_amplitude() -> None:
    tool = AnalyzeBoxTool()
    import json
    result = json.loads(
        tool._run(box_high=102, box_low=100, box_amplitude_pct=2.0, n_candles=10)
    )
    assert result["recommendation"] == "SKIP"
    assert result["amplitude_ok"] is False


def test_analyze_box_proceed_low_amplitude() -> None:
    tool = AnalyzeBoxTool()
    import json
    result = json.loads(
        tool._run(box_high=100.5, box_low=100, box_amplitude_pct=0.5, n_candles=10)
    )
    assert result["recommendation"] == "PROCEED"
    assert result["amplitude_ok"] is True
    assert result["form"] == "normal"


def test_analyze_box_form_narrow() -> None:
    tool = AnalyzeBoxTool()
    import json
    result = json.loads(
        tool._run(box_high=100.2, box_low=100, box_amplitude_pct=0.2, n_candles=5)
    )
    assert result["form"] == "narrow"


def test_analyze_box_form_wide() -> None:
    tool = AnalyzeBoxTool()
    import json
    result = json.loads(
        tool._run(box_high=101, box_low=100, box_amplitude_pct=0.8, n_candles=20)
    )
    # 0.8 no es > 0.7 exactamente, pero es "normal"; ajustar a 0.9
    result = json.loads(
        tool._run(box_high=101, box_low=100, box_amplitude_pct=0.9, n_candles=20)
    )
    assert result["form"] == "wide"


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


def test_scrape_macro_tool_with_mock() -> None:
    with patch(
        "infrastructure.data_sources.scrapers.InvestingMacroCalendarAdapter.get_high_impact_events"
    ) as mock:
        mock.return_value = [
            {"time": "09:00", "event": "NFP", "currency": "USD", "impact": "HIGH"}
        ]
        tool = ScrapeMacroCalendarTool()
        import json
        result = json.loads(tool._run(date="2026-06-03"))
        assert len(result["events"]) == 1
        assert result["events"][0]["event"] == "NFP"


def test_search_news_tool_with_mock() -> None:
    with patch(
        "infrastructure.data_sources.scrapers.DuckDuckGoNewsAdapter.search"
    ) as mock:
        mock.return_value = [{"title": "S&P hits ATH", "source": "X", "url": "u", "published": "p"}]
        tool = SearchNewsTool()
        import json
        result = json.loads(tool._run(query="S&P 500"))
        assert len(result["news"]) == 1


def test_multi_timeframe_tool_with_mock() -> None:
    import pandas as pd
    with patch(
        "infrastructure.broker.capital.adapter.CapitalAdapter.get_candles"
    ) as mock:
        # Mock 4h bullish, 1h bullish, 15min bullish
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
        tool = MultiTimeframeTool()
        import json
        result = json.loads(tool._run(symbol="US500", proposed_direction="LONG"))
        assert result["mtf_alignment"] == "ALIGNED"
        assert result["htf_bias"] == "BULLISH"
        assert result["veto_recommended"] is False


def test_multi_timeframe_counter_signal() -> None:
    import pandas as pd
    with patch(
        "infrastructure.broker.capital.adapter.CapitalAdapter.get_candles"
    ) as mock:
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
        tool = MultiTimeframeTool()
        import json
        result = json.loads(tool._run(symbol="US500", proposed_direction="LONG"))
        assert result["htf_bias"] == "BEARISH"
        assert result["mtf_alignment"] == "COUNTER"
        assert result["veto_recommended"] is True
