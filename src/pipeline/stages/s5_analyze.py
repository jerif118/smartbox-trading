"""
Stage 5: Analyze — corre el crew de CrewAI.
"""

from __future__ import annotations

import json
from typing import Any

from crewai import Crew, Process, Task

from infrastructure.config.settings import get_settings
from infrastructure.persistence.sqlite import event_repo
from pipeline.contracts import AnalyzeInput, AnalyzeOutput, DecisionContract
from utils.logger import get_logger

log = get_logger(__name__)


def stage_analyze(
    input_data: AnalyzeInput,
    run_id: str,
    agents: dict[str, Any],
) -> AnalyzeOutput:
    """Corre el crew. Retorna decisiones estructuradas."""
    settings = get_settings()

    # Log inicio
    event_repo.log_event(
        run_id=run_id,
        agent="decision_maker",
        event_type="SYSTEM",
        payload={"stage": "analyze", "symbols": [s["symbol"] for s in input_data.symbols]},
    )

    # Tareas (simplificadas — los detalles de cada tool ya están en los agentes)
    market_str = input_data.market

    prep_task = Task(
        description=(
            f"Para los símbolos {[s['symbol'] for s in input_data.symbols]}, "
            f"scrapea el calendario macro y clasifica el riesgo. "
            f"Retorna JSON: {{'macro_risk': 'LOW|MEDIUM|HIGH', 'events': [...]}}"
        ),
        agent=agents["decision_maker"],
        expected_output="JSON con macro_risk y high_impact_events",
    )

    mtfa_task = Task(
        description=(
            f"Para cada símbolo {[s['symbol'] for s in input_data.symbols]}, "
            f"usa multi_timeframe_analysis para confirmar sesgo HTF. "
            f"Retorna JSON: {{symbol: {{htf_bias, mtf_alignment, veto_recommended}}}}"
        ),
        agent=agents["mtfa"],
        expected_output="JSON con htf_bias y mtf_alignment por símbolo",
    )

    analyze_task = Task(
        description=(
            f"Para cada símbolo en {market_str}, analiza la caja, RSI, VP, "
            f"y el resultado de MTFA. Propón LONG/SHORT/NO_OPERAR. "
            f"Usa confluence_score. Score < 60 = NO_OPERAR. "
            f"Input JSON: {json.dumps(input_data.symbols, default=str)}"
        ),
        agent=agents["trader"],
        expected_output="JSON con proposed_direction, confidence, confluence_factors",
        context=[prep_task, mtfa_task],
    )

    risk_task = Task(
        description=(
            f"Valida las propuestas del trader. Calcula R:R. "
            f"Usa drawdown_guard con max_daily_loss={settings.max_daily_loss}. "
            f"Si macro_risk=HIGH o drawdown excede → VETO. "
            f"Output: APPROVE/MODIFY/VETO por símbolo."
        ),
        agent=agents["risk_analyst"],
        expected_output="JSON con recommendation por símbolo",
        context=[analyze_task],
    )

    final_task = Task(
        description=(
            "Combina análisis del trader y risk. Emite decisión final. "
            "Si risk VETO → NO_OPERAR. Output JSON estricto: "
            '{"decisions": [{"symbol", "action", "risk", "confidence", "reasons", "key_levels", "signal", "team_consensus"}]}'
        ),
        agent=agents["decision_maker"],
        expected_output="JSON con array de decisiones (un objeto por símbolo)",
        context=[risk_task],
        output_pydantic=AnalyzeOutput,
    )

    crew = Crew(
        agents=[agents["trader"], agents["risk_analyst"], agents["mtfa"]],
        tasks=[prep_task, mtfa_task, analyze_task, risk_task, final_task],
        process=Process.hierarchical,
        manager_agent=agents["decision_maker"],
        verbose=True,
    )

    result = crew.kickoff(
        inputs={
            "symbols_data": json.dumps(input_data.symbols, default=str),
            "market": input_data.market,
        }
    )

    # Extraer decisiones del output
    output = _extract_decisions(result)
    if output is None:
        # El fallback a NO_OPERAR se mantiene, pero el fallo de parseo
        # queda visible en logs y en agent_events (antes era silencioso).
        raw = getattr(result, "raw", str(result))
        log.error(
            "Crew no produjo JSON parseable; fallback NO_OPERAR. Raw (500c): %s",
            raw[:500],
        )
        event_repo.log_event(
            run_id=run_id,
            agent="decision_maker",
            event_type="SYSTEM",
            payload={"event": "parse_error", "raw_truncated": raw[:500]},
        )
        # Fallback: todas NO_OPERAR
        output = AnalyzeOutput(
            decisions=[
                DecisionContract(
                    symbol=s["symbol"],
                    action="NO_OPERAR",
                    risk="MEDIO",
                    confidence=0,
                    reasons=["crew no produjo decisión válida"],
                    key_levels={},
                    signal={},
                    team_consensus="failed",
                )
                for s in input_data.symbols
            ]
        )

    event_repo.log_event(
        run_id=run_id,
        agent="decision_maker",
        event_type="DECISION",
        payload={"n_decisions": len(output.decisions)},
    )

    return output


def _extract_decisions(result) -> AnalyzeOutput | None:
    """Extrae AnalyzeOutput del resultado del crew."""
    import re

    from pydantic import ValidationError

    if hasattr(result, "pydantic") and result.pydantic is not None:
        return result.pydantic
    raw = getattr(result, "raw", str(result))
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL)
    candidates = []
    if fence:
        candidates.append(fence.group(1))
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start >= 0 and end > start:
        candidates.append(raw[start:end])
    for cand in candidates:
        try:
            data = json.loads(cand)
            return AnalyzeOutput.model_validate(data)
        except json.JSONDecodeError as e:
            log.warning("Candidato JSON del crew inválido: %s", e)
        except ValidationError as e:
            log.warning("JSON del crew no cumple AnalyzeOutput: %s", e)
    return None
