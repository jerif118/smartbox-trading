"""Tests de la capa de lectura de la UI (data_access).

Regresión: trades con campos None (pnl, stop_loss, ...) deben producir
DataFrames con TODAS las columnas — el dashboard selecciona columnas fijas
y explotaba con KeyError "['pnl'] not in index".
"""

from __future__ import annotations

import pytest

from infrastructure.config.settings import reset_settings_cache
from infrastructure.persistence.sqlite import db, run_repo, trade_repo
from interfaces.streamlit.data_access import get_trades_df

RUN_ID = "ui-run"


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    reset_settings_cache()
    db.reset_db(db_path)
    db.init_db(db_path)
    run_repo.start_run(RUN_ID)
    yield db_path
    db.reset_db(db_path)


def test_trades_df_incluye_columnas_none() -> None:
    """Trade sin pnl/SL/TP (None) → el df igual trae esas columnas."""
    trade_repo.insert_trade(
        RUN_ID, "US500", "BUY", 0.5, 5000.0, None, None, is_runner=False,
        status="EXPIRED", broker_order_id="DRY-1", client_order_id="x:US500:BUY:P",
    )

    df = get_trades_df(limit=20)

    assert not df.empty
    # la selección exacta que hace el dashboard (línea que explotaba)
    display = df[["id", "ts_open", "symbol", "side", "volume", "entry_price", "status", "pnl"]]
    assert display["pnl"].isna().all()
    open_display = df[["id", "symbol", "side", "volume", "entry_price", "stop_loss", "take_profit"]]
    assert open_display["stop_loss"].isna().all()


def test_trades_df_vacio_sin_trades() -> None:
    assert get_trades_df(limit=20).empty
