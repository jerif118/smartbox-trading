"""Calendario macro oficial BLS + BEA, sin llamadas reales de red."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import requests

from infrastructure.data_sources.scrapers import PublicUSMacroCalendarAdapter


def _response(*, text: str = "", payload: dict | None = None) -> MagicMock:
    response = MagicMock()
    response.text = text
    response.json.return_value = payload or {}
    response.raise_for_status.return_value = None
    return response


def test_public_calendar_merges_official_sources() -> None:
    ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20260715T083000
SUMMARY:Consumer Price Index
END:VEVENT
END:VCALENDAR
"""
    bea = {
        "Gross Domestic Product": {"release_dates": ["2026-07-15T14:00:00+00:00"]},
        "file_last_updated": "2026-07-14T00:00:00",
    }
    with patch(
        "infrastructure.data_sources.scrapers.requests.get",
        side_effect=[_response(text=ics), _response(payload=bea), _response(text="")],
    ):
        result = PublicUSMacroCalendarAdapter(use_cache=False).get_calendar("2026-07-15")
    assert result.status == "OK"
    assert result.providers_ok == ["BLS", "BEA", "FED"]
    assert {event["source"] for event in result.events} == {"BLS", "BEA"}


def test_public_calendar_distinguishes_empty_from_unavailable() -> None:
    with patch(
        "infrastructure.data_sources.scrapers.requests.get",
        side_effect=[
            _response(text="BEGIN:VCALENDAR\nEND:VCALENDAR"),
            _response(payload={}),
            _response(text=""),
        ],
    ):
        empty = PublicUSMacroCalendarAdapter(use_cache=False).get_calendar("2026-07-15")
    assert empty.status == "OK"
    assert empty.events == []

    with patch(
        "infrastructure.data_sources.scrapers.requests.get",
        side_effect=requests.ConnectionError("sin red"),
    ):
        unavailable = PublicUSMacroCalendarAdapter(use_cache=False).get_calendar("2026-07-15")
    assert unavailable.status == "UNAVAILABLE"
    assert unavailable.providers_failed == ["BLS", "BEA", "FED"]


def test_public_calendar_parses_fomc_decision_day() -> None:
    fed_html = """
    <div class="panel panel-default">
      <div class="panel-heading"><h4><a>2026 FOMC Meetings</a></h4></div>
      <div class="row fomc-meeting">
        <div class="fomc-meeting__month"><strong>July</strong></div>
        <div class="fomc-meeting__date">28-29</div>
      </div>
    </div>
    """
    with patch(
        "infrastructure.data_sources.scrapers.requests.get",
        side_effect=[
            _response(text="BEGIN:VCALENDAR\nEND:VCALENDAR"),
            _response(payload={}),
            _response(text=fed_html),
        ],
    ):
        result = PublicUSMacroCalendarAdapter(use_cache=False).get_calendar("2026-07-29")
    assert result.status == "OK"
    assert len(result.events) == 1
    assert result.events[0]["event"] == "FOMC Rate Decision"
    assert result.events[0]["time"] == "2026-07-29T18:00:00+00:00"
