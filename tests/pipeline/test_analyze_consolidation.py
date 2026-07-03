"""Tests de la consolidación determinista (Desk_Manager) y estados de riesgo.

No invocan al LLM: prueban el mapeo risk_decision → acción final y la regla
de seguridad "ante la duda, NO_OPERAR".
"""

from __future__ import annotations

import pytest

from domain.strategy.decision import Action, RiskMode
from pipeline.contracts import RiskAssessment, SymbolResult, TraderAssessment
from pipeline.contracts import AnalyzeInput, SymbolCrewData
from pipeline.stages.s5_analyze import consolidate, map_final_action, _strategy_levels


def _result(
    *, direction: str, score: int, risk_decision: str, symbol: str = "US500"
) -> SymbolResult:
    return SymbolResult(
        symbol=symbol,
        trader=TraderAssessment(
            symbol=symbol, proposed_direction=direction, confluence_score=score,
            confidence=score, reasons=["t"],
        ),
        risk=RiskAssessment(symbol=symbol, risk_decision=risk_decision, reasons=["r"]),
    )


# ── map_final_action ──────────────────────────────────────────────────
def test_trader_no_operar_maps_no_operar() -> None:
    assert map_final_action("NO_OPERAR", 90, "APPROVE_NO_TRADE") == Action.NO_OPERAR


def test_confluence_below_60_forces_no_operar() -> None:
    # Aunque el risk apruebe, score < 60 → NO_OPERAR.
    assert map_final_action("LONG", 59, "APPROVE_TRADE") == Action.NO_OPERAR


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
            symbol="US100", proposed_direction="LONG", confluence_score=72,
            confidence=72, rsi=48.0, vah=100.5, val=99.0, poc=99.8,
            breakout_state="ABOVE", macro_risk="LOW", mtf_alignment="ALIGNED",
            reasons=["RSI neutral", "POC soporte"],
        ),
        risk=RiskAssessment(
            symbol="US100", risk_decision="APPROVE_TRADE", rr_ratio=2.1,
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
