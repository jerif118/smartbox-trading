"""Tests del módulo macro (regla #20)."""

from __future__ import annotations

from datetime import UTC, datetime

from domain.context.macro import MacroEvent, classify_risk, filter_high_impact


def test_filter_high_impact() -> None:
    events = [
        MacroEvent(time="09:00", event="NFP", currency="USD", impact="HIGH"),
        MacroEvent(time="10:00", event="PMI", currency="EUR", impact="LOW"),
        MacroEvent(time="11:00", event="CPI", currency="USD", impact="HIGH"),
    ]
    high = filter_high_impact(events)
    assert len(high) == 2
    assert {e.event for e in high} == {"NFP", "CPI"}


def test_classify_risk_low() -> None:
    assert classify_risk([]) == "LOW"


def test_classify_risk_medium() -> None:
    events = [MacroEvent(time="09:00", event="X", currency="USD", impact="HIGH")]
    assert classify_risk(events) == "MEDIUM"


def test_classify_risk_high() -> None:
    events = [
        MacroEvent(time=f"{9 + i}:00", event=f"E{i}", currency="USD", impact="HIGH")
        for i in range(4)
    ]
    assert classify_risk(events) == "HIGH"


def test_macro_event_is_high_impact() -> None:
    e = MacroEvent(time="09:00", event="NFP", currency="USD", impact="HIGH")
    assert e.is_high_impact
    e2 = MacroEvent(time="09:00", event="X", currency="USD", impact="high")
    assert e2.is_high_impact  # case-insensitive
    e3 = MacroEvent(time="09:00", event="X", currency="USD", impact="low")
    assert not e3.is_high_impact


def test_classify_risk_uses_event_proximity() -> None:
    reference = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
    imminent = [
        MacroEvent(time="2026-07-15T14:10:00+00:00", event="CPI", currency="USD", impact="HIGH")
    ]
    nearby = [
        MacroEvent(time="2026-07-15T15:00:00+00:00", event="GDP", currency="USD", impact="HIGH")
    ]
    far = [MacroEvent(time="2026-07-15T18:00:00+00:00", event="GDP", currency="USD", impact="HIGH")]
    assert classify_risk(imminent, reference, blackout_minutes=15, caution_minutes=120) == "HIGH"
    assert classify_risk(nearby, reference, blackout_minutes=15, caution_minutes=120) == "MEDIUM"
    assert classify_risk(far, reference, blackout_minutes=15, caution_minutes=120) == "LOW"
