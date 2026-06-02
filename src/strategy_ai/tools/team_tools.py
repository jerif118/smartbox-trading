"""
Team Communication Tools - Context summarization for agents
============================================================
These tools help agents organize and reduce context for better decisions.
Real inter-agent communication happens via CrewAI task delegation.
"""

import json
from typing import Literal
from crewai.tools import BaseTool


class SummarizeContextTool(BaseTool):
    name: str = "summarize_context"
    description: str = """
    Resume outputs de otros agentes en format digestible.
    Reduce tokens mientras preserva información clave.
    Útil para pasar contexto entre tasks sin enviar datos completos.
    """

    def _run(self, data_to_summarize: str, target_agent: str = "decision_maker", focus: str = "all") -> str:
        focus_instructions = {
            "trader": "Enfatiza: levels clave, patrón de caja, breakout signal, VP confluence",
            "risk_analyst": "Enfatiza: macro_risk, proposed_direction, confidence score, reasons",
            "decision_maker": "Enfatiza: team consensus, disagreements, final recommendation, confidence"
        }
        instruction = focus_instructions.get(target_agent, focus_instructions["decision_maker"])

        prompt = f"""Resume para agente {target_agent}.
Focus: {instruction}

Datos:
{data_to_summarize}

Output JSON:
{{
    "summary": "resumen conciso 2-3 líneas",
    "key_metrics": {{
        "metric_1": "valor",
        "metric_2": "valor"
    }},
    "actionable": "qué puede hacer el agente con esta info",
    "confidence_preserved": 0-100
}}"""
        return prompt


class AnalyzeBoxTool(BaseTool):
    name: str = "analyze_box"
    description: str = """
    Analiza patrón de caja: amplitude, forma, niveles clave.
    Return: análisis estructurado de la caja para decisión.
    """

    def _run(self, box_data: str) -> str:
        try:
            data = json.loads(box_data) if isinstance(box_data, str) else box_data

            bh = data.get("high", 0)
            bl = data.get("low", 0)
            amp = data.get("amplitud", 0)
            candles = data.get("candles", [])

            analysis = {
                "amplitude_ok": amp < 1.0,
                "amplitude_pct": amp,
                "box_range": bh - bl if bh and bl else 0,
                "candle_count": len(candles),
                "form": "wide" if amp > 0.7 else "narrow" if amp < 0.4 else "normal",
                "recommendation": "PROCEED" if amp < 1.0 else "SKIP"
            }
            return json.dumps(analysis, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})