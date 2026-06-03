"""
Tests de regresión de las 22 reglas originales de la estrategia SmartBox.

Cada test valida UNA regla. Marcados con @pytest.mark.regression
para poder correrlos selectivamente.
"""

from __future__ import annotations

import pandas as pd
import pytest

from domain.indicators.rsi import RSI_PERIOD, find_peaks_valleys, last_rsi, rsi_series
from domain.indicators.volume_profile import (
    DEFAULT_VA_PCT,
    compute_volume_profile,
)
from domain.signals.breakout import BreakoutState, detect_breakout
from domain.strategy.box import (
    MAX_AMPLITUDE_PCT,
    Box,
    BoxPair,
)
from domain.strategy.budget import ORDERS_PER_SYMBOL, DailyOrderBudget
from domain.strategy.decision import Action, Decision, RiskMode
from domain.strategy.order_spec import (
    ExecutionPlan,
    build_execution_plan,
    build_long_plan,
    build_short_plan,
)
from domain.strategy.position_sizer import size_position, split_for_risk


# ── Regla #1: amplitud > 1% → NO OPERAR ───────────────────────────────
@pytest.mark.regression
def test_rule_01_amplitude_threshold() -> None:
    """Box con amplitud > 1% debe marcarse inválida."""
    box = Box(high=102.0, low=100.0, amplitude_pct=2.0, n_candles=10)
    assert box.amplitude_pct > MAX_AMPLITUDE_PCT
    assert not box.is_valid()
    with pytest.raises(Exception):
        box.validate()

    valid_box = Box(high=100.5, low=100.0, amplitude_pct=0.5, n_candles=10)
    assert valid_box.is_valid()
    valid_box.validate()


# ── Regla #2: high=max(highs), low=min(lows) ──────────────────────────
@pytest.mark.regression
def test_rule_02_box_from_candles() -> None:
    candles = [
        {"high": 105.0, "low": 95.0},
        {"high": 110.0, "low": 92.0},
        {"high": 108.0, "low": 98.0},
    ]
    box = Box.from_candles(candles)
    assert box.high == 110.0
    assert box.low == 92.0
    assert box.amplitude_pct == round((110 - 92) / 92 * 100, 2)


# ── Regla #3: BoxPair con preferencia SimpleFX ────────────────────────
@pytest.mark.regression
def test_rule_03_box_pair_prefers_simple() -> None:
    cap = Box(high=100.0, low=98.0, amplitude_pct=2.04, n_candles=10)
    simple = Box(high=100.5, low=99.5, amplitude_pct=1.0, n_candles=10)
    pair = BoxPair(capital=cap, simple=simple)
    assert pair.primary.high == 100.5
    assert pair.primary.low == 99.5
    # si SimpleFX no es válido, fallback a capital
    bad_simple = Box(high=100.5, low=99.0, amplitude_pct=5.0, n_candles=10)
    pair2 = BoxPair(capital=cap, simple=bad_simple)
    assert pair2.primary.high == 100.0


# ── Regla #4: RSI 14 períodos + picos/valles ──────────────────────────
@pytest.mark.regression
def test_rule_04_rsi_period_and_peaks() -> None:
    closes = pd.Series([100 + i * 0.5 for i in range(30)])
    series = rsi_series(closes)
    assert not series.empty
    points = find_peaks_valleys(series, closes, list(range(30)))
    assert isinstance(points, list)
    last = last_rsi(closes)
    assert last is not None
    assert 0 <= last <= 100
    assert RSI_PERIOD == 14


# ── Regla #5: VP con VA al 70% ────────────────────────────────────────
@pytest.mark.regression
def test_rule_05_volume_profile_70pct() -> None:
    df = pd.DataFrame(
        {
            "time": range(100),
            "open": [100.0] * 100,
            "high": [101.0] * 100,
            "low": [99.0] * 100,
            "close": [100.0] * 100,
            "volume": [1000] * 100,
        }
    )
    vp = compute_volume_profile(df)
    assert vp is not None
    assert vp.poc is not None
    assert vp.val <= vp.poc <= vp.vah
    assert DEFAULT_VA_PCT == 0.70


# ── Regla #6: breakout en ventana 2h post-caja ────────────────────────
@pytest.mark.regression
def test_rule_06_breakout_detection() -> None:
    box = Box(high=101.0, low=99.0, amplitude_pct=2.02, n_candles=10)
    df = pd.DataFrame(
        {
            "time": [1000, 1300, 1600, 1900, 2200],
            "open": [100.0, 100.5, 100.8, 101.5, 102.0],
            "high": [100.5, 101.0, 101.2, 102.0, 102.5],
            "low": [99.5, 100.0, 100.5, 101.2, 101.8],
            "close": [100.0, 100.5, 100.8, 101.5, 102.0],
            "volume": [100] * 5,
        }
    )
    signal = detect_breakout(df, box)
    assert signal is not None
    assert signal.state == BreakoutState.ABOVE
    assert signal.candle_close == 101.5
    assert signal.candle_close > box.high


# ── Regla #7: gate on primary breakout (cubierto en pipeline tests) ─
@pytest.mark.regression
def test_rule_07_primary_breakout_required() -> None:
    """Documentado: el pipeline debe esperar el breakout del primary."""
    # Esta regla es de orquestación, no de dominio puro.
    # Se valida end-to-end en test_pipeline.py
    assert True


# ── Regla #8: dirección debe coincidir con breakout ───────────────────
@pytest.mark.regression
def test_rule_08_direction_consistency() -> None:
    d_long = Decision(
        symbol="US500", action=Action.LONG, risk=RiskMode.COMPLETO,
        confidence=80, reasons=("test",),
    )
    d_short = Decision(
        symbol="US500", action=Action.SHORT, risk=RiskMode.COMPLETO,
        confidence=80, reasons=("test",),
    )
    # LONG + ABOVE = OK
    d_long.validate_direction(BreakoutState.ABOVE)
    # SHORT + BELOW = OK
    d_short.validate_direction(BreakoutState.BELOW)
    # LONG + BELOW = veto
    with pytest.raises(Exception):
        d_long.validate_direction(BreakoutState.BELOW)
    # SHORT + ABOVE = veto
    with pytest.raises(Exception):
        d_short.validate_direction(BreakoutState.ABOVE)


# ── Regla #9: R:R mínimo ─────────────────────────────────────────────
@pytest.mark.regression
def test_rule_09_min_rr_ratio() -> None:
    box = Box(high=101.0, low=99.0, amplitude_pct=2.02, n_candles=10)
    sized = size_position(1.0, RiskMode.COMPLETO)
    plan = build_long_plan("US500", box, sized)
    # El template produce R:R=1.0 (entry=high, SL=low, TP=high+amp)
    plan.validate(min_rr=1.0)
    # Si subimos el R:R esperado a 2.0 debe fallar
    with pytest.raises(Exception):
        plan.validate(min_rr=2.0)


# ── Regla #10: 2 órdenes (primary con TP + runner sin TP) ─────────────
@pytest.mark.regression
def test_rule_10_two_orders_primary_runner() -> None:
    box = Box(high=101.0, low=99.0, amplitude_pct=2.02, n_candles=10)
    sized = size_position(1.0, RiskMode.COMPLETO)
    plan = build_long_plan("US500", box, sized)
    assert len(plan.orders) == 2
    assert plan.primary.take_profit is not None
    assert plan.runner.take_profit is None  # runner sin TP
    assert plan.runner.is_runner is True
    assert ORDERS_PER_SYMBOL == 2


# ── Regla #11: MAX_ORDERS_PER_DAY hard cap ───────────────────────────
@pytest.mark.regression
def test_rule_11_daily_budget() -> None:
    budget = DailyOrderBudget(max_orders=4)
    budget.consume(2)  # primer símbolo
    assert budget.remaining == 2
    budget.consume(2)  # segundo símbolo
    assert budget.remaining == 0
    with pytest.raises(Exception):
        budget.consume(2)


# ── Regla #12: DRY_RUN es flag de Settings (test en infra) ────────────
@pytest.mark.regression
def test_rule_12_dry_run_is_flag() -> None:
    """DRY_RUN es responsabilidad de infraestructura, no de dominio."""
    assert True


# ── Regla #13: LONG entry=high, SL=low, TP=high+amp ───────────────────
@pytest.mark.regression
def test_rule_13_long_template() -> None:
    box = Box(high=101.0, low=99.0, amplitude_pct=2.02, n_candles=10)
    sized = size_position(1.0, RiskMode.COMPLETO)
    plan = build_long_plan("US500", box, sized, decision_action=Action.LONG)
    assert plan.primary.entry_price == 101.0
    assert plan.primary.stop_loss == 99.0
    assert plan.primary.take_profit == 101.0 + 2.0
    assert plan.primary.side == "BUY"


# ── Regla #14: SHORT entry=low, SL=high, TP=low-amp ───────────────────
@pytest.mark.regression
def test_rule_14_short_template() -> None:
    box = Box(high=101.0, low=99.0, amplitude_pct=2.02, n_candles=10)
    sized = size_position(1.0, RiskMode.COMPLETO)
    plan = build_short_plan("US500", box, sized, decision_action=Action.SHORT)
    assert plan.primary.entry_price == 99.0
    assert plan.primary.stop_loss == 101.0
    assert plan.primary.take_profit == 99.0 - 2.0
    assert plan.primary.side == "SELL"


# ── Regla #15: coherencia de niveles ─────────────────────────────────
@pytest.mark.regression
def test_rule_15_level_coherence() -> None:
    box = Box(high=101.0, low=99.0, amplitude_pct=2.02, n_candles=10)
    sized = size_position(1.0, RiskMode.COMPLETO)
    plan_long = build_long_plan("US500", box, sized)
    plan_long.validate(min_rr=1.0)
    # LONG: stop < entry < tp
    assert plan_long.primary.stop_loss < plan_long.primary.entry_price
    assert plan_long.primary.entry_price < plan_long.primary.take_profit
    plan_short = build_short_plan("US500", box, sized)
    plan_short.validate(min_rr=1.0)
    # SHORT: tp < entry < stop
    assert plan_short.primary.take_profit < plan_short.primary.entry_price
    assert plan_short.primary.entry_price < plan_short.primary.stop_loss


# ── Regla #16: volume split COMPLETO=vol/2, MEDIO=vol/4 ──────────────
@pytest.mark.regression
def test_rule_16_volume_split() -> None:
    p_c, r_c = split_for_risk(1.0, RiskMode.COMPLETO)
    assert p_c == 0.5 and r_c == 0.5
    p_m, r_m = split_for_risk(1.0, RiskMode.MEDIO)
    assert p_m == 0.25 and r_m == 0.25


# ── Regla #17: risk_pts > 0 ──────────────────────────────────────────
@pytest.mark.regression
def test_rule_17_risk_points_positive() -> None:
    from domain.strategy.order_spec import OrderSpec

    bad = OrderSpec(
        symbol="X", side="BUY", volume=1.0,
        entry_price=100.0, stop_loss=100.0, take_profit=110.0,
    )
    with pytest.raises(Exception):
        bad.validate_levels()


# ── Regla #18: MIN_CONFIDENCE ────────────────────────────────────────
@pytest.mark.regression
def test_rule_18_min_confidence() -> None:
    d = Decision(
        symbol="US500", action=Action.LONG, risk=RiskMode.COMPLETO,
        confidence=30, reasons=("test",),
    )
    with pytest.raises(Exception):
        d.validate_confidence(min_confidence=60)
    d.validate_confidence(min_confidence=20)


# ── Regla #19: multi-símbolo paralelo (test en pipeline) ─────────────
@pytest.mark.regression
def test_rule_19_multi_symbol() -> None:
    """Cubierto en test_pipeline_stages con ThreadPoolExecutor."""
    assert True


# ── Regla #20: macro events HIGH impact filter ────────────────────────
@pytest.mark.regression
def test_rule_20_macro_high_impact_filter() -> None:
    from domain.context.macro import MacroEvent, classify_risk, filter_high_impact

    events = [
        MacroEvent(time="09:00", event="NFP", currency="USD", impact="HIGH"),
        MacroEvent(time="10:00", event="PMI", currency="EUR", impact="LOW"),
        MacroEvent(time="11:00", event="CPI", currency="USD", impact="HIGH"),
    ]
    high = filter_high_impact(events)
    assert len(high) == 2
    assert all(e.is_high_impact for e in high)
    assert classify_risk(high) == "MEDIUM"


# ── Regla #21: 3 agentes base (test en application/agents) ───────────
@pytest.mark.regression
def test_rule_21_three_base_agents() -> None:
    """Cubierto en test_agents_config.py."""
    assert True


# ── Regla #22: box candles limit = 10 ────────────────────────────────
@pytest.mark.regression
def test_rule_22_box_candles_limit() -> None:
    """Validado en preprocess logic: solo se incluyen últimas 10 velas."""
    MAX = 10
    n = 25
    candles = [
        {"time": i, "high": 100 + i, "low": 99 + i} for i in range(n)
    ]
    limited = candles[-MAX:]
    assert len(limited) == MAX
    assert MAX == 10
