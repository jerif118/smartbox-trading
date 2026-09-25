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


def _crew_data(
    state: str,
    poc: float,
    rsi: float | None = 50.0,
    penetration_pct: float = 20.0,
    rsi_divergence: str | None = None,
) -> SymbolCrewData:
    return AnalyzeInput(
        symbols=[
            {
                "symbol": "US500",
                "breakout_signal": {
                    "state": state,
                    "close": 7488.0,
                    "penetration_pct": penetration_pct,
                },
                "caja": {"high": 7482.4, "low": 7449.5, "mid": 7465.95, "amp_pct": 0.44},
                "vp": {"poc": poc},
                "rsi": {"last": rsi, "divergence": rsi_divergence},
            }
        ]
    ).symbols[0]


def _settings(**over) -> SimpleNamespace:
    base = {"effective_min_confluence": 55, "min_breakout_penetration_pct": 10.0}
    return SimpleNamespace(**{**base, **over})


def test_authoritative_confluence_ignores_llm_and_uses_real_data() -> None:
    """El score se deriva de los datos reales, no del número del LLM.

    Contexto favorable (4h a favor) → score por encima del umbral, calculado en
    Python, con independencia de lo que reportara el LLM.
    """
    sd = _crew_data(state="ABOVE", poc=7470.0)
    mtf = {"htf_bias": "BULLISH", "mtf_alignment": "ALIGNED"}
    settings = _settings()

    score = _authoritative_confluence(sd, "LONG", settings, mtf)

    assert score > settings.effective_min_confluence


def test_authoritative_confluence_depends_on_the_4h_trend() -> None:
    """La tendencia de 4h es el factor principal: cambiarla cambia el veredicto."""
    sd = _crew_data(state="ABOVE", poc=7470.0)
    settings = _settings()

    a_favor = _authoritative_confluence(
        sd, "LONG", settings, {"htf_bias": "BULLISH", "mtf_alignment": "ALIGNED"}
    )
    en_contra = _authoritative_confluence(
        sd, "LONG", settings, {"htf_bias": "BEARISH", "mtf_alignment": "COUNTER"}
    )

    assert a_favor > settings.effective_min_confluence
    assert en_contra < settings.effective_min_confluence


def test_authoritative_confluence_no_longer_depends_on_penetration() -> None:
    """La penetración de la caja dejó de mover el score."""
    settings = _settings()
    mtf = {"htf_bias": "BULLISH", "mtf_alignment": "ALIGNED"}

    marginal = _crew_data(state="ABOVE", poc=7470.0, penetration_pct=0.6)
    decidida = _crew_data(state="ABOVE", poc=7470.0, penetration_pct=25.0)

    assert _authoritative_confluence(marginal, "LONG", settings, mtf) == (
        _authoritative_confluence(decidida, "LONG", settings, mtf)
    )


def test_branch_normalizes_deterministic_fields(monkeypatch) -> None:
    """A: el branch sobrescribe con el dato REAL lo que el LLM alucina.

    El Trader devuelve breakout_state=BELOW y confluence_score=99, pero el
    breakout real (sd) es ABOVE → el resultado usa ABOVE y recalcula el score.
    """
    import pipeline.stages.s5_analyze as s5

    # breakout real ABOVE, ruptura decidida (25% del rango)
    sd = _crew_data(state="ABOVE", poc=7470.0, penetration_pct=25.0)
    settings = _settings(
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
    # score determinista desde el contexto real (4h alcista, marcos alineados),
    # no el 99 que devolvió el LLM
    assert res.trader.confluence_score != 99
    assert res.trader.confluence_score >= settings.effective_min_confluence


def test_authoritative_confluence_low_when_data_contradicts() -> None:
    """Datos que NO apoyan la dirección → score 0, aunque el LLM dijera 100."""
    settings = _settings()
    # breakout BELOW mientras la dirección propuesta es LONG
    sd = _crew_data(state="BELOW", poc=7460.0, penetration_pct=25.0)
    score = _authoritative_confluence(sd, "LONG", settings)
    assert score == 0
    assert score < settings.effective_min_confluence


def test_authoritative_confluence_rejects_trade_against_the_4h() -> None:
    """Operar contra la tendencia de 4h queda por debajo del umbral."""
    settings = _settings()
    sd = _crew_data(state="BELOW", poc=7460.0)

    score = _authoritative_confluence(
        sd, "SHORT", settings, {"htf_bias": "BULLISH", "mtf_alignment": "COUNTER"}
    )

    assert score < settings.effective_min_confluence


def test_authoritative_confluence_accepts_trade_with_the_4h() -> None:
    """A favor de la tendencia de 4h, la ruptura es operable."""
    settings = _settings()
    sd = _crew_data(state="ABOVE", poc=7460.0)

    score = _authoritative_confluence(
        sd, "LONG", settings, {"htf_bias": "BULLISH", "mtf_alignment": "ALIGNED"}
    )

    assert score >= settings.effective_min_confluence


def test_divergence_against_the_trade_downgrades_but_does_not_veto() -> None:
    """Con el 4h a favor y camino limpio, una divergencia rebaja pero no veta.

    Es deliberado: las divergencias fallan a menudo dentro de una tendencia
    fuerte. Baja del umbral de riesgo completo (70) —el trade entra a medio
    tamaño— pero no cae por debajo del mínimo para operar. Sola, sin el resto
    de factores a favor, sí tumba el setup (ver tests de confluence).
    """
    settings = _settings()
    mtf = {"htf_bias": "BULLISH", "mtf_alignment": "ALIGNED"}
    limpio = _crew_data(state="ABOVE", poc=7460.0)
    con_divergencia = _crew_data(state="ABOVE", poc=7460.0, rsi_divergence="BEARISH")

    score_limpio = _authoritative_confluence(limpio, "LONG", settings, mtf)
    score_div = _authoritative_confluence(con_divergencia, "LONG", settings, mtf)

    assert score_div < score_limpio
    assert score_div < 70  # deja de ser riesgo completo
    assert score_div >= settings.effective_min_confluence


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


@pytest.mark.parametrize("bad_state", ["INSIDE", "NONE"])
def test_branch_without_breakout_does_not_invent_a_direction(monkeypatch, bad_state) -> None:
    """Sin breakout operable la rama corta: nada de asumir LONG.

    Antes, `candidate` caía a "LONG" por defecto y se arrancaba la rama entera
    (descarga MTF + 2 llamadas al LLM) analizando una dirección inventada.
    """
    import pipeline.stages.s5_analyze as s5

    sd = _crew_data(state=bad_state, poc=7470.0)
    settings = SimpleNamespace(
        effective_min_confluence=55,
        min_rr_ratio=1.0,
        max_daily_loss=500.0,
        llm=SimpleNamespace(trader="openai/x", risk_analyst="openai/x"),
    )

    called: list[str] = []
    monkeypatch.setattr(s5, "bind_log_context", lambda **k: None)
    monkeypatch.setattr(
        s5, "analyze_multi_timeframe", lambda *a, **k: called.append("mtf") or {}
    )
    monkeypatch.setattr(
        s5, "build_trader_agent", lambda *a, **k: called.append("trader") or object()
    )
    monkeypatch.setattr(
        s5, "build_risk_agent", lambda *a, **k: called.append("risk") or object()
    )
    monkeypatch.setattr(s5, "Crew", lambda **k: called.append("crew") or SimpleNamespace())

    res = s5._analyze_symbol_branch(sd, settings, "run-x")

    assert res.trader.proposed_direction == "NO_OPERAR"
    assert res.risk.risk_decision == "NEED_DATA"
    assert res.trader.confluence_score == 0
    # Ni red ni tokens gastados en una dirección que no existe.
    assert called == []


def test_llm_failure_is_reported_as_failed_symbol_not_a_decision(monkeypatch) -> None:
    """Si la rama revienta (LLM/API caído) el NO_OPERAR resultante no es una
    decisión de estrategia: el símbolo sale en `failed_symbols` para que el
    orquestador no marque la ruptura como decidida y la reintente."""
    import pipeline.stages.s5_analyze as s5

    sd = _crew_data(state="ABOVE", poc=7470.0)

    def boom(*a, **k):
        raise RuntimeError("400 Function tools with reasoning_effort are not supported")

    monkeypatch.setattr(s5, "_analyze_symbol_branch", boom)
    monkeypatch.setattr(s5, "event_repo", SimpleNamespace(log_event=lambda **k: None))
    monkeypatch.setattr(
        s5,
        "get_settings",
        lambda: SimpleNamespace(effective_min_confluence=55, full_risk_confluence=70),
    )

    out = s5.stage_analyze(AnalyzeInput(symbols=[sd.model_dump()]), "run-x")

    assert out.failed_symbols == ["US500"]
    assert out.decisions[0].action == Action.NO_OPERAR
