"""Tests de los contratos tipados del pipeline (Fase 5)."""

from __future__ import annotations

import pandas as pd
import pytest
from pydantic import ValidationError

from pipeline.contracts import (
    AnalyzeInput,
    IngestOutput,
    ManageInput,
    OpenTradeContract,
    SymbolCrewData,
)


def _crew_symbol_dict() -> dict:
    return {
        "symbol": "US500",
        "is_primary": True,
        "breakout_signal": {"state": "ABOVE", "close": 100.8, "time": "2026-06-11T15:00:00"},
        "caja": {"high": 100.5, "low": 100.0, "mid": 100.25, "amp_pct": 0.5},
        "vp": {}, "rsi": {"last": 55.0}, "macro": {"risk": "LOW"},
    }


def test_analyze_input_coerces_dicts_to_models():
    inp = AnalyzeInput(symbols=[_crew_symbol_dict()])
    assert isinstance(inp.symbols[0], SymbolCrewData)
    assert inp.symbols[0].caja.high == 100.5
    assert inp.symbols[0].breakout_signal.state == "ABOVE"


def test_analyze_input_rejects_bad_breakout_state():
    bad = _crew_symbol_dict()
    bad["breakout_signal"]["state"] = "SIDEWAYS"
    with pytest.raises(ValidationError):
        AnalyzeInput(symbols=[bad])


def test_analyze_input_rejects_missing_box_levels():
    bad = _crew_symbol_dict()
    del bad["caja"]["high"]
    with pytest.raises(ValidationError):
        AnalyzeInput(symbols=[bad])


def test_ingest_output_requires_dataframe():
    df = pd.DataFrame({"time": [1], "close": [100.0]})
    out = IngestOutput(symbol="US500", df_candles=df, n_candles=1)
    assert out.df_candles is df
    with pytest.raises(ValidationError):
        IngestOutput(symbol="US500", df_candles=[{"time": 1}], n_candles=1)


def test_manage_input_coerces_sqlite_rows():
    row = {
        "id": 7, "symbol": "US500", "side": "BUY", "entry_price": 100.0,
        "stop_loss": 98.0, "take_profit": 102.0, "is_runner": 0,
        "broker_order_id": "BR-1", "run_id": "extra-col", "pnl": None,
    }
    inp = ManageInput(open_trades=[row], current_prices={"US500": 101.0})
    trade = inp.open_trades[0]
    assert isinstance(trade, OpenTradeContract)
    assert trade.is_runner is False  # 0 → False
    assert trade.id == 7


def test_open_trade_contract_rejects_invalid_side():
    with pytest.raises(ValidationError):
        OpenTradeContract(id=1, symbol="US500", side="HOLD", entry_price=100.0)
