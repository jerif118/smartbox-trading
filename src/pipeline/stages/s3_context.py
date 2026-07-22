"""
Stage 3: Context — fetch macro events de alto impacto.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from domain.context.macro import (
    MacroEvent,
    MacroRisk,
    classify_risk,
    filter_high_impact,
    minutes_to_nearest_event,
)
from infrastructure.config.settings import get_settings
from infrastructure.data_sources.scrapers import PublicUSMacroCalendarAdapter
from pipeline.contracts import ContextInput, ContextOutput


def stage_context(input_data: ContextInput) -> ContextOutput:
    """Scrapea calendario macro y clasifica el riesgo."""
    settings = get_settings()
    result = PublicUSMacroCalendarAdapter().get_calendar(input_data.date_str)
    if result.status == "UNAVAILABLE":
        raise ConnectionError("calendario macro no disponible (BLS, BEA y FED fallaron)")
    raw = result.events

    events = [
        MacroEvent(
            time=e.get("time", ""),
            event=e.get("event", ""),
            currency=e.get("currency", ""),
            impact=e.get("impact", "HIGH"),
            source=e.get("source", ""),
        )
        for e in raw
    ]
    high = filter_high_impact(events)
    if input_data.reference_time:
        reference = datetime.fromisoformat(
            input_data.reference_time.replace("Z", "+00:00")
        ).astimezone(ZoneInfo(input_data.market_tz))
    else:
        reference = datetime.now(ZoneInfo(input_data.market_tz))
    risk = classify_risk(
        high,
        reference,
        blackout_minutes=settings.macro_blackout_minutes,
        caution_minutes=settings.macro_caution_minutes,
    )
    if result.status == "DEGRADED" and risk.value == "LOW":
        # Una fuente oficial caída reduce tamaño, pero no bloquea todo el día.
        risk = MacroRisk.MEDIUM

    return ContextOutput(
        macro_risk=risk.value,
        high_impact_events=[
            {
                "time": e.time,
                "event": e.event,
                "currency": e.currency,
                "impact": e.impact,
                "source": e.source,
            }
            for e in high
        ],
        provider_status=result.status,
        providers_ok=result.providers_ok,
        providers_failed=result.providers_failed,
        nearest_event_minutes=minutes_to_nearest_event(high, reference),
    )
