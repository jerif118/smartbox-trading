"""Sección 2: Histórico de trades."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from interfaces.streamlit.data_access import get_trades_df


def render() -> None:
    st.header("📋 Histórico de trades")

    # ── Filtros ───────────────────────────────────────────────────────
    col1, col2, col3 = st.columns(3)
    with col1:
        symbol = st.text_input("Símbolo (vacío = todos)", value="")
    with col2:
        status_options = ["", "PENDING", "OPEN", "CLOSED_TP", "CLOSED_SL", "CLOSED_MANUAL", "EXPIRED", "REJECTED"]
        status = st.selectbox("Status", status_options)
    with col3:
        limit = st.number_input("Límite", min_value=10, max_value=1000, value=100, step=10)

    df = get_trades_df(
        symbol=symbol.strip() or None,
        status=status or None,
        limit=int(limit),
    )

    st.markdown("---")

    # ── Stats agregadas ───────────────────────────────────────────────
    if not df.empty:
        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("Total", len(df))
        col2.metric("Cerrados", len(df[df["status"].str.startswith("CLOSED", na=False)]))
        col3.metric("Abiertos", len(df[df["status"] == "OPEN"]))
        if "pnl" in df.columns and df["pnl"].notna().any():
            col4.metric("P&L total", f"{df['pnl'].sum():.2f}")
            if "r_multiple" in df.columns and df["r_multiple"].notna().any():
                col5.metric("Avg R", f"{df['r_multiple'].mean():.2f}")
            else:
                col5.metric("Wins", len(df[df["pnl"] > 0]))

    st.markdown("---")

    # ── Tabla ─────────────────────────────────────────────────────────
    if df.empty:
        st.info("Sin trades para los filtros aplicados.")
    else:
        # Mostrar columnas relevantes
        display_cols = [
            "id", "ts_open", "ts_close", "symbol", "side", "volume",
            "entry_price", "exit_price", "stop_loss", "take_profit",
            "status", "pnl", "r_multiple", "is_runner", "reason",
        ]
        display = df[[c for c in display_cols if c in df.columns]].copy()
        # razones expandible
        st.dataframe(
            display,
            use_container_width=True,
            hide_index=True,
            column_config={
                "pnl": st.column_config.NumberColumn(format="%.2f"),
                "r_multiple": st.column_config.NumberColumn(format="%.2f"),
            },
        )

        # ── Botón de exportar ─────────────────────────────────────────
        st.download_button(
            label="📥 Exportar CSV",
            data=df.to_csv(index=False).encode("utf-8"),
            file_name=f"trades_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
        )

        # ── Detalle expandible ────────────────────────────────────────
        st.markdown("### Detalle de un trade")
        trade_id = st.number_input(
            "ID de trade",
            min_value=1,
            value=int(df.iloc[0]["id"]) if not df.empty else 1,
            step=1,
        )
        if st.button("Ver detalle"):
            from infrastructure.persistence.sqlite import trade_repo
            t = trade_repo.get_trade(int(trade_id))
            if t:
                col1, col2 = st.columns(2)
                with col1:
                    st.json(
                        {
                            "id": t.id,
                            "symbol": t.symbol,
                            "side": t.side,
                            "volume": t.volume,
                            "entry": t.entry_price,
                            "stop_loss": t.stop_loss,
                            "take_profit": t.take_profit,
                            "is_runner": t.is_runner,
                            "status": t.status,
                        }
                    )
                with col2:
                    st.json(
                        {
                            "exit": t.exit_price,
                            "pnl": t.pnl,
                            "r_multiple": t.r_multiple,
                            "ts_open": t.ts_open,
                            "ts_close": t.ts_close,
                            "broker_order_id": t.broker_order_id,
                            "reason": t.reason,
                        }
                    )
            else:
                st.warning(f"Trade {trade_id} no encontrado")
