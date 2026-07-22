"""
Scrapers para calendario macro y noticias.

Implementa `MacroCalendarProvider` y `NewsProvider`.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import requests
from bs4 import BeautifulSoup

from utils.logger import get_logger
from utils.retry import retry

log = get_logger(__name__)

BLS_CALENDAR_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
BEA_CALENDAR_URL = "https://apps.bea.gov/API/signup/release_dates.json"
FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

_HIGH_IMPACT_TERMS = (
    "consumer price",
    "cpi",
    "employment situation",
    "nonfarm",
    "payroll",
    "producer price",
    "ppi",
    "job openings",
    "jolts",
    "gross domestic product",
    "gdp",
    "personal income and outlays",
    "pce",
    "international trade",
)


@dataclass(frozen=True)
class CalendarResult:
    events: list[dict]
    status: str  # OK | DEGRADED | UNAVAILABLE
    providers_ok: list[str] = field(default_factory=list)
    providers_failed: list[str] = field(default_factory=list)


def _is_high_impact(name: str) -> bool:
    lowered = name.lower()
    return any(term in lowered for term in _HIGH_IMPACT_TERMS)


def _unfold_ical(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_ical_datetime(value: str) -> str | None:
    value = value.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            parsed = datetime.strptime(value, fmt)
            if value.endswith("Z"):
                parsed = parsed.replace(tzinfo=UTC)
            else:
                # BLS publica sus horas en Eastern. Mantener el offset correcto,
                # incluido DST, y normalizar a UTC.
                from zoneinfo import ZoneInfo

                parsed = parsed.replace(tzinfo=ZoneInfo("America/New_York"))
            return parsed.astimezone(UTC).isoformat()
        except ValueError:
            continue
    return None


def _parse_bls_ics(text: str, date_str: str) -> list[dict]:
    events: list[dict] = []
    current: dict[str, str] | None = None
    for line in _unfold_ical(text):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            name = current.get("SUMMARY", "").replace("\\,", ",")
            timestamp = _parse_ical_datetime(current.get("DTSTART", ""))
            if timestamp and timestamp[:10] == date_str and _is_high_impact(name):
                events.append(
                    {
                        "time": timestamp,
                        "event": name,
                        "currency": "USD",
                        "impact": "HIGH",
                        "source": "BLS",
                    }
                )
            current = None
        elif current is not None and ":" in line:
            key, value = line.split(":", 1)
            current[key.split(";", 1)[0]] = value
    return events


def _parse_bea_json(payload: dict, date_str: str) -> list[dict]:
    events: list[dict] = []
    for name, value in payload.items():
        if name == "file_last_updated" or not isinstance(value, dict):
            continue
        if not _is_high_impact(name):
            continue
        for timestamp in value.get("release_dates", []):
            if str(timestamp)[:10] == date_str:
                events.append(
                    {
                        "time": timestamp,
                        "event": name,
                        "currency": "USD",
                        "impact": "HIGH",
                        "source": "BEA",
                    }
                )
    return events


def _parse_fomc_html(text: str, date_str: str) -> list[dict]:
    """Extrae el día de decisión del FOMC para la fecha solicitada."""
    from calendar import month_name
    from zoneinfo import ZoneInfo

    target = datetime.strptime(date_str, "%Y-%m-%d")
    soup = BeautifulSoup(text, "html.parser")
    year_anchor = soup.find(
        lambda tag: (
            tag.name in ("a", "h4")
            and f"{target.year} FOMC Meetings" in tag.get_text(" ", strip=True)
        )
    )
    if year_anchor is None:
        return []
    panel = year_anchor.find_parent("div", class_="panel")
    if panel is None:
        return []

    for meeting in panel.select(".fomc-meeting"):
        month_el = meeting.select_one(".fomc-meeting__month")
        date_el = meeting.select_one(".fomc-meeting__date")
        if month_el is None or date_el is None:
            continue
        # En reuniones Apr/May o Jan/Feb, la decisión cae en el último mes.
        final_month = month_el.get_text(" ", strip=True).split("/")[-1]
        month_number = next(
            (i for i, name in enumerate(month_name) if name.startswith(final_month)),
            0,
        )
        digits = [int(value) for value in re.findall(r"\d+", date_el.get_text())]
        if not month_number or not digits:
            continue
        decision_day = digits[-1]
        if (target.month, target.day) != (month_number, decision_day):
            continue
        local = datetime(
            target.year,
            target.month,
            target.day,
            14,
            0,
            tzinfo=ZoneInfo("America/New_York"),
        )
        return [
            {
                "time": local.astimezone(UTC).isoformat(),
                "event": "FOMC Rate Decision",
                "currency": "USD",
                "impact": "HIGH",
                "source": "FED",
            }
        ]
    return []


class PublicUSMacroCalendarAdapter:
    """Calendario gratuito: combina BLS, BEA y FOMC oficiales.

    Una fuente vacía y válida significa "sin eventos". Una fuente caída queda
    registrada como DEGRADED; si todas caen el resultado es UNAVAILABLE para
    impedir que un error de red se convierta silenciosamente en riesgo LOW.
    """

    _cache: ClassVar[dict[str, tuple[float, CalendarResult]]] = {}
    _cache_lock: ClassVar[threading.Lock] = threading.Lock()
    _cache_ttl_s: ClassVar[int] = 30 * 60

    def __init__(self, *, use_cache: bool = True) -> None:
        self.use_cache = use_cache

    @classmethod
    def clear_cache(cls) -> None:
        with cls._cache_lock:
            cls._cache.clear()

    def get_calendar(self, date_str: str) -> CalendarResult:
        if self.use_cache:
            with self._cache_lock:
                cached = self._cache.get(date_str)
            if cached and time.monotonic() - cached[0] < self._cache_ttl_s:
                return cached[1]

        events: list[dict] = []
        ok: list[str] = []
        failed: list[str] = []

        try:
            resp = requests.get(BLS_CALENDAR_URL, timeout=15)
            resp.raise_for_status()
            events.extend(_parse_bls_ics(resp.text, date_str))
            ok.append("BLS")
        except requests.RequestException as exc:
            failed.append("BLS")
            log.warning("Calendario BLS no disponible: %s", exc)

        try:
            resp = requests.get(BEA_CALENDAR_URL, timeout=15)
            resp.raise_for_status()
            events.extend(_parse_bea_json(resp.json(), date_str))
            ok.append("BEA")
        except (requests.RequestException, ValueError) as exc:
            failed.append("BEA")
            log.warning("Calendario BEA no disponible: %s", exc)

        try:
            resp = requests.get(FOMC_CALENDAR_URL, timeout=15)
            resp.raise_for_status()
            events.extend(_parse_fomc_html(resp.text, date_str))
            ok.append("FED")
        except requests.RequestException as exc:
            failed.append("FED")
            log.warning("Calendario FOMC no disponible: %s", exc)

        # Deduplicar por instante/nombre; algunas publicaciones comparten fuente.
        deduped = list({(e["time"], e["event"]): e for e in events}.values())
        status = "OK" if len(ok) == 3 else "DEGRADED" if ok else "UNAVAILABLE"
        result = CalendarResult(deduped, status, ok, failed)
        if self.use_cache:
            with self._cache_lock:
                self._cache[date_str] = (time.monotonic(), result)
        return result


@retry(max_retries=2, backoff=2.0, exceptions=(requests.RequestException,))
def _scrape_investing_widget(target_date: str, time_zone: str = "8") -> dict[str, Any]:
    """Scrape del widget de Investing.com (más estable que la página completa)."""
    url = "https://sslecal2.investing.com"
    params = {
        "columns": "exc_flags,exc_currency,exc_importance,exc_actual,exc_forecast,exc_previous",
        "features": "datepicker,timezone",
        "calType": "day",
        "timeZone": str(time_zone),
        "lang": "1",
        "dateFrom": target_date,
        "dateTo": target_date,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("Calendario macro (%s) no disponible: %s", url, e)
        return {"ok": False, "error": str(e), "events": []}

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if not table:
        return {"ok": False, "error": "no_table", "events": []}

    events = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        time_text = None
        currency = None
        impact = None
        event_name = None
        for td in tds:
            cls = " ".join(td.get("class", []))
            txt = td.get_text(" ", strip=True)
            if "time" in cls:
                time_text = txt or None
            elif "currency" in cls:
                currency = txt or None
            elif "sentiment" in cls or "importance" in cls:
                bulls = td.find_all("i", class_=lambda x: x and "grayFullBullishIcon" in x)
                if bulls:
                    impact = f"{len(bulls)}/3"
                else:
                    impact = txt or None
            elif "event" in cls:
                a = td.find("a")
                event_name = (a.get_text(" ", strip=True) if a else txt) or None
        if not (event_name or currency):
            continue
        events.append(
            {
                "time": time_text,
                "event": event_name,
                "currency": currency,
                "impact": impact,
            }
        )
    return {"ok": True, "events": events}


class InvestingMacroCalendarAdapter:
    """MacroCalendarProvider basado en scraping de Investing.com."""

    def get_high_impact_events(self, date_str: str) -> list[dict]:
        result = _scrape_investing_widget(date_str)
        if not result.get("ok"):
            return []
        events = result.get("events", [])
        # Filtrar HIGH impact (3/3 o "HIGH")
        high = []
        for e in events:
            impact = e.get("impact", "")
            if impact == "3/3" or "HIGH" in str(impact).upper():
                high.append({**e, "impact": "HIGH"})
        return high


class DuckDuckGoNewsAdapter:
    """NewsProvider usando DuckDuckGo HTML (sin API key)."""

    def search(self, query: str, days_back: int = 1) -> list[dict]:
        try:
            url = "https://duckduckgo.com/html/"
            params = {"q": query, "kl": "us-en"}
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            }
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning("DuckDuckGo search (%s) failed: %s", url, e)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        for item in soup.find_all("a", class_="result__a")[:5]:
            title = item.get_text(strip=True)
            url_href = item.get("href", "")
            snippet_el = (
                item.find_parent("li").find("a", class_="result__snippet")
                if item.find_parent("li")
                else None
            )
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            results.append(
                {
                    "title": title,
                    "source": "DuckDuckGo",
                    "url": url_href,
                    "snippet": snippet,
                }
            )
        return results


class NewsAPIAdapter:
    """NewsProvider usando NewsAPI.org (requiere NEWS_API_KEY)."""

    def search(self, query: str, days_back: int = 1) -> list[dict]:
        key = os.getenv("NEWS_API_KEY", "")
        if not key:
            return []
        try:
            date_from = (datetime.now(UTC) - timedelta(days=days_back)).strftime("%Y-%m-%d")
            resp = requests.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": query,
                    "from": date_from,
                    "language": "en",
                    "sortBy": "publishedAt",
                    "apiKey": key,
                    "pageSize": 5,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("NewsAPI (newsapi.org) failed: %s", e)
            return []
        return [
            {
                "title": a.get("title", ""),
                "source": a.get("source", {}).get("name", ""),
                "url": a.get("url", ""),
                "published": a.get("publishedAt", ""),
            }
            for a in data.get("articles", [])[:5]
        ]
