"""Tests de la consolidación determinista (Desk_Manager) y estados de riesgo.

No invocan al LLM: prueban el mapeo risk_decision → acción final y la regla
de seguridad "ante la duda, NO_OPERAR".
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from domain.strategy.decision import Action, RiskMode
from pipeline.contracts import (
    AnalyzeInput,
    RiskAssessment,
    SymbolCrewData,
    SymbolResult,
    TraderAssessment,
)
from pipeline.stages.s5_analyze import (
    _authoritative_confluence,
    _strategy_levels,
    consolidate,
    map_final_action,
)


def _crew_data(state: str, poc: float, rsi: float | None = 50.0) -> SymbolCrewData:
    return AnalyzeInput(
        symbols=[
            {
                "symbol": "US500",
                "breakout_signal": {"state": state, "close": 7488.0},
                "caja": {"high": 7482.4, "low": 7449.5, "mid": 7465.95, "amp_pct": 0.44},
                "vp": {"poc": poc},
                "rsi": {"last": rsi},
            }
        ]
    ).symbols[0]


def test_authoritative_confluence_ignores_llm_and_uses_real_data() -> None:
    """A: el score se deriva de los datos reales, no del número del LLM.

    Datos favorables a LONG (breakout ABOVE, POC sobre el mid, MTF ALIGNED) →
    score alto calculado en Python, con independencia de lo que reportara el LLM.
    """
    settings = SimpleNamespace(effective_min_confluence=55)
    sd = _crew_data(state="ABOVE", poc=7470.0)  # poc > mid(7465.95)
    with patch("pipeline.stages.s5_analyze.analyze_multi_timeframe",
               return_value={"mtf_alignment": "ALIGNED"}):
        score = _authoritative_confluence(sd, "LONG", settings)
    # RSI neutral(20)+POC(25)+breakout(30)+MTF(25) = 100
    assert score == 100


def test_authoritative_confluence_reuses_precomputed_mtf(monkeypatch) -> None:
    """B: si se pasa el mtf ya descargado, NO se vuelve a llamar a Capital."""
    import pipeline.stages.s5_analyze as s5

    calls = {"n": 0}

    def _fake_mtf(*_a, **_k):
        calls["n"] += 1
        return {"mtf_alignment": "ALIGNED", "tf_biases": {}}

    monkeypatch.setattr(s5, "analyze_multi_timeframe", _fake_mtf)
    sd = _crew_data(state="ABOVE", poc=7470.0)
    settings = SimpleNamespace(effective_min_confluence=55)
    precomputed = {
        "mtf_alignment": "ALIGNED",
        "tf_biases": {"15min": "BULLISH", "1h": "BULLISH", "4h": "NEUTRAL"},
    }
    s5._authoritative_confluence(sd, "LONG", settings, mtf=precomputed)
    assert calls["n"] == 0


def test_branch_normalizes_deterministic_fields(monkeypatch) -> None:
    """A: el branch sobrescribe con el dato REAL lo que el LLM alucina.

    El Trader devuelve breakout_state=BELOW y confluence_score=99, pero el
    breakout real (sd) es ABOVE → el resultado usa ABOVE y recalcula el score.
    """
    import pipeline.stages.s5_analyze as s5

    sd = _crew_data(state="ABOVE", poc=7470.0)  # breakout real ABOVE, poc > mid
    settings = SimpleNamespace(
        effective_min_confluence=55,
        min_rr_ratio=1.0,
        max_daily_loss=500.0,
        llm=SimpleNamespace(trader="openai/x", risk_analyst="openai/x"),
    )
    fake_trader = TraderAssessment(
        symbol="US500",
        proposed_direction="LONG",
        confluence_score=99,  # inflado
        breakout_state="BELOW",  # alucinado (contrario al real)
        reasons=["x"],
    )
    fake_risk = RiskAssessment(symbol="US500", risk_decision="APPROVE_TRADE", rr_ratio=1.0)

    # El branch siembra el contexto de logs del hilo (en prod el hilo del pool se
    # destruye); en el hilo de pytest hay que evitar que se filtre a otros tests.
    monkeypatch.setattr(s5, "bind_log_context", lambda **k: None)
    monkeypatch.setattr(s5, "build_trader_agent", lambda *a, **k: object())
    monkeypatch.setattr(s5, "build_risk_agent", lambda *a, **k: object())
    monkeypatch.setattr(s5, "Task", lambda **k: SimpleNamespace())
    monkeypatch.setattr(s5, "Crew", lambda **k: SimpleNamespace(kickoff=lambda: None))
    monkeypatch.setattr(
        s5,
        "_parse_task_output",
        lambda task, model: fake_trader if model is TraderAssessment else fake_risk,
    )
    monkeypatch.setattr(
        s5,
        "analyze_multi_timeframe",
        lambda *a, **k: {
            "mtf_alignment": "ALIGNED",
            "tf_biases": {"15min": "BULLISH", "1h": "BULLISH", "4h": "BULLISH"},
        },
    )
    monkeypatch.setattr(s5.event_repo, "log_event", lambda *a, **k: 0)

    res = s5._analyze_symbol_branch(sd, settings, "run-x")
    # breakout_state normalizado al REAL (ABOVE), no al alucinado (BELOW)
    assert res.trader.breakout_state == "ABOVE"
    # score determinista: RSI neutral(20)+POC(25)+breakout(30)+MTF(25) = 100 (no 99)
    assert res.trader.confluence_score == 100


def test_authoritative_confluence_low_when_data_contradicts() -> None:
    """A: datos que NO apoyan la dirección → score bajo, aunque el LLM dijera 100."""
    settings = SimpleNamespace(effective_min_confluence=55)
    # breakout BELOW mientras la dirección es LONG, POC bajo el mid, MTF no alineado
    sd = _crew_data(state="BELOW", poc=7460.0)  # poc < mid
    with patch("pipeline.stages.s5_analyze.analyze_multi_timeframe",
               return_value={"mtf_alignment": "MIXED"}):
        score = _authoritative_confluence(sd, "LONG", settings)
    # RSI neutral(20)+POC contrario(10) = 30 → por debajo del umbral
    assert score == 30
    assert score < settings.effective_min_confluence


def _result(
    *, direction: str, score: int, risk_decision: str, symbol: str = "US500"
) -> SymbolResult:
    return SymbolResult(
        symbol=symbol,
        trader=TraderAssessment(
            symbol=symbol,
            proposed_direction=direction,
            confluence_score=score,
            confidence=score,
            reasons=["t"],
        ),
        risk=RiskAssessment(symbol=symbol, risk_decision=risk_decision, reasons=["r"]),
    )


# ── map_final_action ──────────────────────────────────────────────────
def test_trader_no_operar_maps_no_operar() -> None:
    assert map_final_action("NO_OPERAR", 90, "APPROVE_NO_TRADE") == Action.NO_OPERAR


def test_confluence_below_60_forces_no_operar() -> None:
    # Aunque el risk apruebe, score < 60 → NO_OPERAR.
    assert map_final_action("LONG", 59, "APPROVE_TRADE") == Action.NO_OPERAR


def test_active_profile_accepts_55_with_medium_risk() -> None:
    assert map_final_action("LONG", 55, "APPROVE_TRADE", min_confluence=55) == Action.LONG
    result = _result(direction="LONG", score=55, risk_decision="APPROVE_TRADE")
    output = consolidate([result], run_id="active", min_confluence=55, full_risk_confluence=70)
    assert output.decisions[0].action == Action.LONG
    assert output.decisions[0].risk == RiskMode.MEDIO


def test_approve_trade_long() -> None:
    assert map_final_action("LONG", 75, "APPROVE_TRADE") == Action.LONG


def test_approve_trade_short() -> None:
    assert map_final_action("SHORT", 80, "APPROVE_TRADE") == Action.SHORT


def test_modify_still_tradeable() -> None:
    assert map_final_action("LONG", 70, "MODIFY") == Action.LONG


@pytest.mark.parametrize("rd", ["VETO", "NEED_DATA", "APPROVE_NO_TRADE"])
def test_blocking_risk_decisions_no_operar(rd: str) -> None:
    assert map_final_action("LONG", 90, rd) == Action.NO_OPERAR


def test_unknown_risk_decision_defaults_safe() -> None:
    assert map_final_action("LONG", 90, "WHATEVER") == Action.NO_OPERAR


# ── consolidate (Desk_Manager determinista) ───────────────────────────
def test_consolidate_approve_no_trade_is_no_operar() -> None:
    res = _result(direction="NO_OPERAR", score=0, risk_decision="APPROVE_NO_TRADE")
    out = consolidate([res], run_id="t")
    assert len(out.decisions) == 1
    d = out.decisions[0]
    assert d.action == Action.NO_OPERAR
    # El contexto NO se pierde: risk_decision queda en signal.
    assert d.signal["risk_decision"] == "APPROVE_NO_TRADE"


def test_consolidate_confluence_low_no_operar() -> None:
    res = _result(direction="LONG", score=55, risk_decision="APPROVE_TRADE")
    out = consolidate([res], run_id="t")
    assert out.decisions[0].action == Action.NO_OPERAR


def test_consolidate_approve_trade_operates() -> None:
    res = _result(direction="LONG", score=80, risk_decision="APPROVE_TRADE")
    out = consolidate([res], run_id="t")
    d = out.decisions[0]
    assert d.action == Action.LONG
    assert d.confidence == 80


def test_consolidate_modify_sets_medium_risk() -> None:
    res = _result(direction="SHORT", score=70, risk_decision="MODIFY")
    out = consolidate([res], run_id="t")
    d = out.decisions[0]
    assert d.action == Action.SHORT
    assert d.risk == RiskMode.MEDIO


def test_consolidate_preserves_full_trader_context() -> None:
    res = SymbolResult(
        symbol="US100",
        trader=TraderAssessment(
            symbol="US100",
            proposed_direction="LONG",
            confluence_score=72,
            confidence=72,
            rsi=48.0,
            vah=100.5,
            val=99.0,
            poc=99.8,
            breakout_state="ABOVE",
            macro_risk="LOW",
            mtf_alignment="ALIGNED",
            reasons=["RSI neutral", "POC soporte"],
        ),
        risk=RiskAssessment(
            symbol="US100",
            risk_decision="APPROVE_TRADE",
            rr_ratio=2.1,
            reasons=["R:R favorable"],
        ),
    )
    out = consolidate([res], run_id="t")
    d = out.decisions[0]
    # Todo el contexto del trader llega a la decisión final.
    assert d.signal["rsi"] == 48.0
    assert d.signal["breakout_state"] == "ABOVE"
    assert d.signal["mtf_alignment"] == "ALIGNED"
    assert d.signal["rr_ratio"] == 2.1
    assert d.key_levels["poc"] == 99.8
    assert any("RSI neutral" in r for r in d.reasons)


def test_consolidate_both_symbols() -> None:
    results = [
        _result(direction="LONG", score=80, risk_decision="APPROVE_TRADE", symbol="US500"),
        _result(direction="NO_OPERAR", score=0, risk_decision="APPROVE_NO_TRADE", symbol="US100"),
    ]
    out = consolidate(results, run_id="t")
    by_sym = {d.symbol: d for d in out.decisions}
    assert by_sym["US500"].action == Action.LONG
    assert by_sym["US100"].action == Action.NO_OPERAR


def test_strategy_levels_use_box_for_stop_loss_and_rr() -> None:
    sd = AnalyzeInput(
        symbols=[
            {
                "symbol": "US500",
                "breakout_signal": {"state": "ABOVE", "close": 7488.0},
                "caja": {"high": 7482.4, "low": 7449.5, "mid": 7465.95, "amp_pct": 0.44},
            }
        ]
    ).symbols[0]
    assert isinstance(sd, SymbolCrewData)

    levels = _strategy_levels(sd)

    assert levels["long"]["entry"] == 7482.4
    assert levels["long"]["stop_loss"] == 7449.5
    assert levels["long"]["take_profit"] == 7515.3
    assert levels["long"]["rr_ratio"] == 1.0
    assert levels["short"]["entry"] == 7449.5
    assert levels["short"]["stop_loss"] == 7482.4
    assert levels["short"]["take_profit"] == 7416.6
    assert levels["short"]["rr_ratio"] == 1.0
