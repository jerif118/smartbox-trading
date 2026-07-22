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

La consolidación final (Desk_Manager) y la gestión de posiciones son
DETERMINISTAS (ver pipeline.stages.s5_analyze.consolidate y s7_manage), no
agentes LLM. Por eso los únicos agentes LLM del flujo activo son Trader y Risk.
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


def build_trader_agent(symbol: str, llm_settings: LLMSettings, tools: list | None = None) -> Agent:
    """Trader aislado para un símbolo concreto. Instancia nueva por rama."""
    return _build_agent(
        role=f"Trader_{symbol}",
        goal=(
            f"Analizar el setup de {symbol}: patrón de caja, RSI, Volume Profile "
            f"(VAH/VAL/POC), breakout y alineación multi-timeframe. Proponer "
            f"proposed_direction (LONG/SHORT/NO_OPERAR) con un confluence_score "
            f"cuantitativo 0-100, siempre relativo a la dirección propuesta. "
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


def build_risk_agent(symbol: str, llm_settings: LLMSettings, tools: list | None = None) -> Agent:
    """Risk aislado para un símbolo concreto. Instancia nueva por rama."""
    return _build_agent(
        role=f"Risk_{symbol}",
        goal=(
            f"Validar la propuesta del Trader_{symbol}. Calcular R:R numérico y peor "
            f"escenario. Emitir un risk_decision EXACTO de este conjunto: "
            f"APPROVE_TRADE, APPROVE_NO_TRADE, MODIFY, NEED_DATA, VETO. "
            f"Reglas: si el trader propone NO_OPERAR → APPROVE_NO_TRADE. "
            f"Si faltan datos críticos → NEED_DATA. Si hay regla dura violada "
            f"(macro_risk HIGH dentro del blackout, drawdown diario excedido, "
            f"R:R < mínimo o dirección contraria al breakout) → VETO. "
            f"MTF mixto/contrario, calendario DEGRADED o falta de confirmación "
            f"primaria se resuelven con MODIFY y medio tamaño, no con veto. "
            f"Nunca uses 'APPROVE' a secas."
        ),
        backstory=(
            "Eres un gestor de riesgo práctico. Proteges contra pérdidas catastróficas, "
            "pero ante incertidumbre moderada reduces tamaño en vez de cancelar una "
            "señal válida. Eres terminal: no delegas ni preguntas a coworkers."
        ),
        llm=get_crewai_llm(llm_settings.risk_analyst, llm_settings),
        tools=tools,
    )
