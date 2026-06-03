"""
Streamlit app — entrypoint principal.

Uso: streamlit run src/interfaces/streamlit/app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# Asegurar que src/ esté en el path
SRC = Path(__file__).parent.parent.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from infrastructure.config.settings import get_settings
from infrastructure.persistence.sqlite import db

st.set_page_config(
    page_title="SmartBox Trading v2",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


def main() -> None:
    settings = get_settings()
    db.init_db()

    st.title("📈 SmartBox Trading v2")
    st.caption(f"DB: `{settings.db_path}` | DRY_RUN: {settings.dry_run} | Símbolos: {', '.join(settings.symbol_list)}")

    st.sidebar.title("Navegación")
    page = st.sidebar.radio(
        "Ir a:",
        ["🏠 Dashboard", "📋 Histórico de trades", "🤖 Acciones de agentes"],
    )

    st.sidebar.markdown("---")
    st.sidebar.markdown("### Stats rápidos")
    from infrastructure.persistence.sqlite import trade_repo
    stats = trade_repo.compute_stats()
    st.sidebar.metric("Trades totales", stats["total_trades"])
    st.sidebar.metric("Win rate", f"{stats['win_rate_pct']}%")
    st.sidebar.metric("P&L total", f"{stats['total_pnl']:.2f}")

    if page == "🏠 Dashboard":
        from interfaces.streamlit.sections.dashboard import render
        render()
    elif page == "📋 Histórico de trades":
        from interfaces.streamlit.sections.trades import render
        render()
    elif page == "🤖 Acciones de agentes":
        from interfaces.streamlit.sections.agents import render
        render()


if __name__ == "__main__":
    main()
