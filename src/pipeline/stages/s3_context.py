"""
Stage 3: Context — fetch macro events de alto impacto.
"""

from __future__ import annotations

from domain.context.macro import MacroEvent, classify_risk, filter_high_impact
from infrastructure.data_sources.scrapers import InvestingMacroCalendarAdapter
from pipeline.contracts import ContextInput, ContextOutput


def stage_context(input_data: ContextInput) -> ContextOutput:
    """Scrapea calendario macro y clasifica el riesgo."""
    adapter = InvestingMacroCalendarAdapter()
    raw = adapter.get_high_impact_events(input_data.date_str)

    events = [
        MacroEvent(
            time=e.get("time", ""),
            event=e.get("event", ""),
            currency=e.get("currency", ""),
            impact=e.get("impact", "HIGH"),
        )
        for e in raw
    ]
    high = filter_high_impact(events)
    risk = classify_risk(high)

    return ContextOutput(
        macro_risk=risk.value,
        high_impact_events=[
            {
                "time": e.time,
                "event": e.event,
                "currency": e.currency,
                "impact": e.impact,
            }
            for e in high
        ],
    )
