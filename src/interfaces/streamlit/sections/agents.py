"""Sección 3: Acciones e interacciones de agentes."""

from __future__ import annotations

import json

import streamlit as st

from interfaces.streamlit.data_access import get_events_for_run, get_recent_runs

AGENT_ICONS = {
    "decision_maker": "👑",
    "trader": "📊",
    "risk_analyst": "🛡️",
    "mtfa": "🔭",
    "position_manager": "🛠️",
    "system": "⚙️",
}

EVENT_ICONS = {
    "THOUGHT": "💭",
    "TOOL_CALL": "🔧",
    "TOOL_RESULT": "📦",
    "MESSAGE": "💬",
    "DECISION": "🎯",
    "SYSTEM": "⚙️",
}

EVENT_COLORS = {
    "THOUGHT": "#888",
    "TOOL_CALL": "#FFA500",
    "TOOL_RESULT": "#00BFFF",
    "MESSAGE": "#90EE90",
    "DECISION": "#FFD700",
    "DECISION_LARGO": "#00C805",  # LONG
    "DECISION_CORTO": "#FF4136",  # SHORT
    "DECISION_NO": "#888",
    "SYSTEM": "#444",
}


def render() -> None:
    st.header("🤖 Acciones e interacciones de agentes")

    runs_df = get_recent_runs(limit=20)
    if runs_df.empty:
        st.info("Sin runs aún.")
        return

    # ── Selector de run ───────────────────────────────────────────────
    runs_df["label"] = runs_df.apply(
        lambda r: f"[{r['status']:8s}] {r['id'][:8]} {r['started_at'][:19]}", axis=1
    )
    run_label = st.selectbox("Selecciona un run", runs_df["label"].tolist())
    run_id = runs_df[runs_df["label"] == run_label]["id"].iloc[0]

    st.markdown("---")

    # ── Filtros ───────────────────────────────────────────────────────
    col1, col2 = st.columns(2)
    with col1:
        agents_filter = st.multiselect(
            "Filtrar por agente",
            ["decision_maker", "trader", "risk_analyst", "mtfa", "position_manager"],
            default=[],
        )
    with col2:
        events_filter = st.multiselect(
            "Filtrar por tipo de evento",
            ["THOUGHT", "TOOL_CALL", "TOOL_RESULT", "MESSAGE", "DECISION", "SYSTEM"],
            default=[],
        )

    events_df = get_events_for_run(run_id)
    if events_df.empty:
        st.info(f"Sin eventos para el run {run_id[:8]}")
        return

    if agents_filter:
        events_df = events_df[events_df["agent"].isin(agents_filter)]
    if events_filter:
        events_df = events_df[events_df["event_type"].isin(events_filter)]

    st.markdown(f"### {len(events_df)} eventos")

    # ── Timeline ──────────────────────────────────────────────────────
    for _, ev in events_df.iterrows():
        agent_icon = AGENT_ICONS.get(ev["agent"], "🤖")
        event_icon = EVENT_ICONS.get(ev["event_type"], "•")

        # Color de borde según tipo
        color = EVENT_COLORS.get(ev["event_type"], "#888")

        with st.container():
            cols = st.columns([1, 3, 5])
            with cols[0]:
                st.markdown(
                    f"<div style='color:{color};font-size:1.5em;text-align:center'>{event_icon}</div>",
                    unsafe_allow_html=True,
                )
            with cols[1]:
                st.markdown(
                    f"**{agent_icon} {ev['agent']}**  \n"
                    f"`{ev['event_type']}`  \n"
                    f"<small>{ev['ts'][:19]}</small>",
                    unsafe_allow_html=True,
                )
            with cols[2]:
                if ev["payload"]:
                    try:
                        payload_dict = json.loads(ev["payload"])
                        st.json(payload_dict)
                    except (json.JSONDecodeError, TypeError):
                        st.text(str(ev["payload"])[:300])
                if ev["duration_ms"]:
                    st.caption(f"⏱ {ev['duration_ms']}ms")
            st.markdown("---")
