"""
Constructores de agentes CrewAI.

Arquitectura por símbolo (concurrencia segura):
- Cada símbolo tiene SU PROPIA instancia de Trader y de Risk. Nunca se
  comparte una instancia de Agent entre dos ramas concurrentes (eso causaba
  "Executor is already running. Cannot invoke the same executor instance
  concurrently.").
- Nombres simples y estables (Trader_US500, Risk_US100, Desk_Manager) sin
  emojis ni guiones largos, para que CrewAI no falle al resolver coworkers.
- allow_delegation=False en TODOS los agentes: ningún agente delega. La
  delegación libre del manager hierarchical era la causa de
  "coworker mentioned not found".

El Desk_Manager final NO es un agente que delega: la consolidación final es
determinista (ver pipeline.stages.s5_analyze.consolidate). Se expone igual su
constructor por si se necesita una narrativa LLM, siempre con
allow_delegation=False.
"""

from __future__ import annotations

from crewai import Agent

from infrastructure.config.settings import LLMSettings
from infrastructure.llm.provider import get_crewai_llm


def _build_agent(role: str, goal: str, backstory: str, llm, tools: list | None = None) -> Agent:
    return Agent(
        role=role,
        goal=goal,
        backstory=backstory,
        llm=llm,
        tools=tools or [],
        verbose=True,
        # Nadie delega: especialistas y manager final son terminales.
        allow_delegation=False,
    )


def build_trader_agent(
    symbol: str, llm_settings: LLMSettings, tools: list | None = None
) -> Agent:
    """Trader aislado para un símbolo concreto. Instancia nueva por rama."""
    return _build_agent(
        role=f"Trader_{symbol}",
        goal=(
            f"Analizar el setup de {symbol}: patrón de caja, RSI, Volume Profile "
            f"(VAH/VAL/POC), breakout y alineación multi-timeframe. Proponer "
            f"proposed_direction (LONG/SHORT/NO_OPERAR) con un confluence_score "
            f"cuantitativo 0-100. Si confluence_score < 60 → NO_OPERAR. "
            f"Listar explícitamente las razones del análisis."
        ),
        backstory=(
            "15 años de price action institucional (Wyckoff, soportes/resistencias, "
            "volume profile). Esperas el setup correcto antes de proponer. No operas "
            "sin confluencia clara. Trabajas solo: no delegas ni preguntas a nadie."
        ),
        llm=get_crewai_llm(llm_settings.trader, llm_settings),
        tools=tools,
    )


def build_risk_agent(
    symbol: str, llm_settings: LLMSettings, tools: list | None = None
) -> Agent:
    """Risk aislado para un símbolo concreto. Instancia nueva por rama."""
    return _build_agent(
        role=f"Risk_{symbol}",
        goal=(
            f"Validar la propuesta del Trader_{symbol}. Calcular R:R numérico y peor "
            f"escenario. Emitir un risk_decision EXACTO de este conjunto: "
            f"APPROVE_TRADE, APPROVE_NO_TRADE, MODIFY, NEED_DATA, VETO. "
            f"Reglas: si el trader propone NO_OPERAR → APPROVE_NO_TRADE. "
            f"Si faltan datos críticos → NEED_DATA. Si hay regla dura violada "
            f"(macro_risk HIGH con evento cercano, drawdown diario excedido, "
            f"R:R < mínimo) → VETO. Nunca uses 'APPROVE' a secas."
        ),
        backstory=(
            "Vienes de un fondo donde sobreviviste drawdowns severos. Tu mantra: "
            "'¿y si estoy equivocado?'. Prefieres perder una oportunidad antes que "
            "un blowup. Eres terminal: no delegas ni preguntas a coworkers."
        ),
        llm=get_crewai_llm(llm_settings.risk_analyst, llm_settings),
        tools=tools,
    )


def build_desk_manager(llm_settings: LLMSettings, tools: list | None = None) -> Agent:
    """Manager final. allow_delegation=False — solo consolida, nunca delega."""
    return _build_agent(
        role="Desk_Manager",
        goal=(
            "Consolidar los resultados YA EXISTENTES de cada rama (Trader + Risk por "
            "símbolo) en una decisión final por símbolo. NO analizas mercados, NO "
            "delegas, NO preguntas a nadie. Solo combinas lo que ya recibiste y "
            "devuelves JSON estricto."
        ),
        backstory=(
            "Jefe de mesa con 15 años de experiencia. Tu palabra es final, pero te "
            "basas únicamente en lo que el Trader y el Risk de cada símbolo ya "
            "produjeron. Ante cualquier duda, NO_OPERAR."
        ),
        llm=get_crewai_llm(llm_settings.decision_maker, llm_settings),
        tools=tools,
    )


def build_position_manager(llm_settings: LLMSettings, tools: list | None = None) -> Agent:
    """Gestor de trades abiertos (usado fuera del crew de análisis)."""
    return _build_agent(
        role="Position_Manager",
        goal=(
            "Gestionar trades abiertos: trailing stop, mover SL a breakeven tras "
            "+1R, y registrar eventos relevantes. No delega."
        ),
        backstory=(
            "El 70% del profit viene del management, no de la entrada. No dejas que "
            "un trade ganador se vuelva perdedor ni que uno perdedor corra sin razón."
        ),
        llm=get_crewai_llm(llm_settings.position_manager, llm_settings),
        tools=tools,
    )
