"""Sección 1: Dashboard principal."""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from interfaces.streamlit.data_access import (
    get_dashboard_kpis,
    get_equity_curve_df,
    get_recent_runs,
    get_trades_df,
)


def render() -> None:
    st.header("🏠 Dashboard")

    kpis = get_dashboard_kpis()

    # ── KPIs ──────────────────────────────────────────────────────────
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Balance", f"{kpis['balance']:.2f}")
    col2.metric("Equity", f"{kpis['equity']:.2f}")
    col3.metric(
        "P&L Total",
        f"{kpis['total_pnl']:.2f}",
        delta=f"{kpis['total_pnl']:.2f}",
    )
    col4.metric("Trades abiertos", kpis["open_trades"])

    col5, col6, col7, col8 = st.columns(4)
    col5.metric("Total trades", kpis["total_trades"])
    col6.metric("Win rate", f"{kpis['win_rate_pct']}%")
    col7.metric("Avg R", f"{kpis['avg_r_multiple']:.2f}")
    col8.metric("Wins / Losses", f"{kpis['wins']} / {kpis['losses']}")

    st.markdown("---")

    # ── Equity curve ──────────────────────────────────────────────────
    st.subheader("Curva de equity")
    range_days = st.selectbox("Rango", [7, 30, 90, 365], index=1, format_func=lambda x: f"{x} días")
    df = get_equity_curve_df(days=range_days)
    if df.empty:
        st.info("Sin datos de equity aún. Corre el bot al menos una vez.")
    else:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df["ts"], y=df["balance"], name="Balance", line=dict(color="#00C805")))
        fig.add_trace(go.Scatter(x=df["ts"], y=df["equity"], name="Equity", line=dict(color="#0055FF")))
        fig.update_layout(
            template="plotly_dark",
            height=400,
            margin=dict(l=0, r=0, t=0, b=0),
            xaxis_title="",
            yaxis_title="USD",
        )
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")

    # ── Posiciones abiertas + últimos trades ──────────────────────────
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("Posiciones abiertas")
        open_trades = get_trades_df(status="OPEN", limit=20)
        if open_trades.empty:
            st.info("Sin posiciones abiertas")
        else:
            display = open_trades[["id", "symbol", "side", "volume", "entry_price", "stop_loss", "take_profit"]]
            st.dataframe(display, use_container_width=True, hide_index=True)

    with col_right:
        st.subheader("Últimos trades cerrados")
        closed = get_trades_df(limit=20)
        closed_sorted = closed.sort_values("ts_open", ascending=False).head(10)
        if closed_sorted.empty:
            st.info("Sin trades cerrados aún")
        else:
            display = closed_sorted[["id", "ts_open", "symbol", "side", "volume", "entry_price", "status", "pnl"]]
            st.dataframe(display, use_container_width=True, hide_index=True)

    st.markdown("---")

    # ── Runs recientes ────────────────────────────────────────────────
    st.subheader("Runs recientes")
    runs_df = get_recent_runs(limit=10)
    if runs_df.empty:
        st.info("Sin runs aún. Ejecuta `python -m interfaces.cli.main run`.")
    else:
        display = runs_df[["id", "started_at", "finished_at", "status", "error"]].copy()
        display["id"] = display["id"].str[:8]
        st.dataframe(display, use_container_width=True, hide_index=True)
