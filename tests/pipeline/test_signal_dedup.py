"""Una ruptura, una decisión.

`detect_breakout` devuelve SIEMPRE el primer cierre fuera de la caja, así que
la misma ruptura reaparece en cada vela de 5 min hasta caducar por edad. Sin
memoria de lo ya decidido el bot:

- re-analizaba indefinidamente un símbolo que ya tenía sus órdenes puestas
  (gastando el crew en cada vela), y
- con el límite de exposición sembrado desde "símbolos operados hoy", el
  segundo símbolo —que rompe más tarde— no llegaba a operar nunca.

Estos tests fijan las dos cosas.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pandas as pd
import pytest

from domain.strategy.box import Box
from domain.strategy.decision import Action, RiskMode
from infrastructure.config.settings import reset_settings_cache
from infrastructure.persistence.sqlite import db, signal_repo, trade_repo
from pipeline import orchestrator
from pipeline.contracts import (
    AnalyzeOutput,
    ContextOutput,
    DecisionContract,
    ExecuteOutput,
    IngestOutput,
    OrderContract,
    PreprocessOutput,
    SignalOutput,
)

WEEKDAY = "2026-06-11"  # jueves: salta el check de fin de semana
# `signal_repo.analyzed_today` compara contra la fecha UTC real, así que las
# señales del test tienen que llevar la fecha de hoy.
TODAY = datetime.now(UTC).date().isoformat()
BREAKOUT_US500 = f"{TODAY}T14:05:00Z"
BREAKOUT_US100 = f"{TODAY}T14:30:00Z"

BOX = Box(high=100.5, low=100.0, amplitude_pct=0.5, n_candles=10)


@pytest.fixture(autouse=True)
def temp_env(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("SYMBOLS", "US500,US100")
    monkeypatch.setenv("PRIMARY_SYMBOL", "US500")
    monkeypatch.setenv("MAX_ORDERS_PER_DAY", "8")
    monkeypatch.setenv("DRY_RUN", "true")
    reset_settings_cache()
    db.reset_db(db_path)
    db.init_db(db_path)
    yield db_path
    db.reset_db(db_path)
    reset_settings_cache()


def _mock_stages(monkeypatch, breakouts: dict[str, str], exec_box_for=None):
    """Mockea los stages. `breakouts` = {símbolo: signal_time} con ruptura.

    `exec_box_for` (opcional) limita qué símbolos traen caja SimpleFX; los que
    falten simulan el feed caído (no hay niveles que mandar al broker).
    """
    df = pd.DataFrame(
        {
            "time": [1500, 1800, 2100],
            "open": [100.0] * 3,
            "high": [101.0] * 3,
            "low": [99.5] * 3,
            "close": [100.8] * 3,
            "volume": [100] * 3,
        }
    )

    monkeypatch.setattr(orchestrator, "_today_str", lambda tz=None: WEEKDAY)
    monkeypatch.setattr(orchestrator, "box_window_unix", lambda *a: (0, 1000))
    monkeypatch.setattr(orchestrator, "SimpleFXAdapter", MagicMock())
    monkeypatch.setattr(orchestrator, "_fetch_simplefx_candles", lambda *a, **k: None)
    monkeypatch.setattr(
        orchestrator,
        "stage_ingest",
        lambda inp: IngestOutput(symbol=inp.symbol, df_candles=df, n_candles=len(df)),
    )
    monkeypatch.setattr(
        orchestrator,
        "stage_preprocess",
        lambda inp, df_c, df_s=None: PreprocessOutput(
            symbol=inp.symbol,
            box=BOX,
            box_simple=(BOX if exec_box_for is None or inp.symbol in exec_box_for else None),
            rsi_last=55.0,
            volume_profile=None,
            box_candles=[],
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "stage_signal",
        lambda inp: SignalOutput(
            symbol=inp.symbol,
            has_breakout=inp.symbol in breakouts,
            breakout_state="ABOVE" if inp.symbol in breakouts else None,
            candle_close=100.8,
            signal_time=breakouts.get(inp.symbol),
            signal_age_minutes=1.0,
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "stage_context",
        lambda inp: ContextOutput(macro_risk="LOW", high_impact_events=[]),
    )

    def _analyze(analyze_in, run_id):
        return AnalyzeOutput(
            decisions=[
                DecisionContract(
                    symbol=s.symbol,
                    action=Action.LONG,
                    risk=RiskMode.COMPLETO,
                    confidence=80,
                    reasons=["test"],
                    key_levels={},
                    signal={"state": "ABOVE", "confluence_score": 80},
                    team_consensus="unánime",
                )
                for s in analyze_in.symbols
            ]
        )

    analyze_mock = MagicMock(side_effect=_analyze)
    monkeypatch.setattr(orchestrator, "stage_analyze", analyze_mock)

    def _execute(exec_in, run_id, budget, broker):
        return ExecuteOutput(
            decision_id=1,
            orders=[
                OrderContract(
                    symbol=exec_in.symbol,
                    side="BUY",
                    volume=0.5,
                    entry_price=100.5,
                    stop_loss=100.0,
                    take_profit=101.0,
                    is_runner=False,
                )
            ],
            errors=[],
        )

    exec_mock = MagicMock(side_effect=_execute)
    monkeypatch.setattr(orchestrator, "stage_execute", exec_mock)
    return analyze_mock, exec_mock


def _analyzed_symbols(analyze_mock) -> list[list[str]]:
    """Símbolos que llegaron al crew, corrida a corrida."""
    return [[s.symbol for s in call.args[0].symbols] for call in analyze_mock.call_args_list]


def test_misma_ruptura_no_se_reanaliza_en_la_siguiente_vela(monkeypatch):
    analyze_mock, exec_mock = _mock_stages(monkeypatch, {"US500": BREAKOUT_US500})

    first = orchestrator.run_pipeline()
    assert first.orders_sent == 1
    assert _analyzed_symbols(analyze_mock) == [["US500"]]

    # Misma vela de ruptura, siguiente corrida del monitor: no vuelve al crew.
    second = orchestrator.run_pipeline()
    assert second.status == "success"
    assert second.orders_sent == 0
    assert analyze_mock.call_count == 1
    assert exec_mock.call_count == 1

    assert signal_repo.is_analyzed("US500", BREAKOUT_US500)
    assert signal_repo.get_outcome("US500", BREAKOUT_US500) == "LONG"


def test_el_segundo_simbolo_llega_al_crew_aunque_el_primero_ya_decidio(monkeypatch):
    _mock_stages(monkeypatch, {"US500": BREAKOUT_US500})
    orchestrator.run_pipeline()

    # US100 rompe 25 min más tarde; US500 sigue devolviendo su misma ruptura.
    analyze_mock2, exec_mock2 = _mock_stages(
        monkeypatch, {"US500": BREAKOUT_US500, "US100": BREAKOUT_US100}
    )
    second = orchestrator.run_pipeline()

    # Solo el símbolo con ruptura nueva se analiza: el otro ya tuvo su decisión.
    assert _analyzed_symbols(analyze_mock2) == [["US100"]]
    assert second.decisions_count == 1
    assert [c.args[0].symbol for c in exec_mock2.call_args_list] == ["US100"]
    assert signal_repo.is_analyzed("US100", BREAKOUT_US100)


def test_primario_ya_decidido_cuenta_como_confirmacion(monkeypatch):
    """El primario rompió hoy: el secundario no entra a medio tamaño por eso."""
    _mock_stages(monkeypatch, {"US500": BREAKOUT_US500})
    orchestrator.run_pipeline()

    _, exec_mock = _mock_stages(monkeypatch, {"US500": BREAKOUT_US500, "US100": BREAKOUT_US100})
    orchestrator.run_pipeline()

    decision = exec_mock.call_args_list[0].args[0].decision
    assert decision.risk == RiskMode.COMPLETO
    assert not any("sin confirmación" in r for r in decision.reasons)


def test_fallo_de_infraestructura_no_marca_la_ruptura(monkeypatch):
    """Sin caja SimpleFX no se envió nada: la ruptura debe reintentarse."""
    _, exec_mock = _mock_stages(monkeypatch, {"US500": BREAKOUT_US500}, exec_box_for=set())
    first = orchestrator.run_pipeline()
    assert first.orders_sent == 0
    assert exec_mock.call_count == 0
    assert not signal_repo.is_analyzed("US500", BREAKOUT_US500)

    # Feed recuperado: la misma ruptura vuelve a intentarse y ahora sí opera.
    _, exec_mock2 = _mock_stages(monkeypatch, {"US500": BREAKOUT_US500})
    second = orchestrator.run_pipeline()
    assert second.orders_sent == 1
    assert exec_mock2.call_count == 1
    assert signal_repo.is_analyzed("US500", BREAKOUT_US500)


def test_exposicion_correlacionada_mide_posiciones_vivas(monkeypatch):
    """Un trade ya cerrado no bloquea al otro símbolo el resto del día."""
    monkeypatch.setenv("MAX_CORRELATED_SETUPS", "1")
    reset_settings_cache()

    from infrastructure.persistence.sqlite import run_repo

    run_repo.start_run("run-previo")
    run_repo.finish_run("run-previo", "success")
    trade_id = trade_repo.insert_trade(
        "run-previo",
        "US500",
        "BUY",
        0.5,
        100.5,
        100.0,
        101.0,
        is_runner=False,
        status="OPEN",
    )
    assert trade_repo.count_open_setups() == 1

    trade_repo.close_trade(trade_id, status="CLOSED_TP", exit_price=101.0, pnl=50.0)
    assert trade_repo.count_open_setups() == 0
    assert trade_repo.count_setups_today() == 1  # el día sí lo recuerda

    _, exec_mock = _mock_stages(monkeypatch, {"US100": BREAKOUT_US100})
    result = orchestrator.run_pipeline()

    assert result.orders_sent == 1
    assert exec_mock.call_count == 1


def test_marca_idempotente(temp_env):
    from infrastructure.persistence.sqlite import run_repo

    run_repo.start_run("run-x")
    assert signal_repo.mark_analyzed("run-x", "US500", BREAKOUT_US500, "ABOVE", "LONG")
    # Segunda marca (re-run/carrera): no duplica ni pisa el outcome original.
    assert not signal_repo.mark_analyzed("run-x", "US500", BREAKOUT_US500, "ABOVE", "OTRO")
    assert signal_repo.get_outcome("US500", BREAKOUT_US500) == "LONG"
    assert len(signal_repo.list_analyzed_today()) == 1
