
import os
import json
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Literal

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from crewai.tools import BaseTool


# -----------------------------------------------------------------------------
# Deterministic scrapers for the specific sources declared in agents.yaml.
# These tools fetch + parse HTML and return structured JSON.
# IMPORTANT: The agent must NOT invent data. If scraping fails, it returns ok=false.
# -----------------------------------------------------------------------------

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _http_get(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 30) -> requests.Response:
    # Some sites are sensitive to missing Referer.
    headers = dict(DEFAULT_HEADERS)
    headers["Referer"] = "https://es.investing.com/earnings-calendar"
    r = requests.get(url, params=params, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r


# ----------------------------
# Investing.com (Economic) via widget endpoint
# ----------------------------

def _scrape_investing_economic_widget(
    cal_type: Literal["day", "week"] = "day",
    time_zone: str = "8",  # widget timezone id; DO NOT convert times
    lang: str = "1",       # 1=en, 4=es sometimes varies; keep configurable
    max_rows: int = 200,
) -> Dict[str, Any]:
    """Scrape Investing economic calendar using the iframe/widget endpoint.

    This endpoint is more stable than scraping the full page UI.
    It returns an HTML table that we parse.

    Returns:
      { ok, source, url, as_of, source_tz, events:[...] }
    """
    url = "https://sslecal2.investing.com"
    params = {
        "columns": "exc_flags,exc_currency,exc_importance,exc_actual,exc_forecast,exc_previous",
        "features": "datepicker,timezone",
        "calType": cal_type,
        "timeZone": str(time_zone),
        "lang": str(lang),
    }

    try:
        r = _http_get(url, params=params)
    except Exception as e:
        return {
            "ok": False,
            "source": "investing_economic_widget",
            "url": url,
            "as_of": _utc_now_iso(),
            "error": f"http_error: {e}",
            "events": [],
        }

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    if not table:
        return {
            "ok": False,
            "source": "investing_economic_widget",
            "url": r.url,
            "as_of": _utc_now_iso(),
            "error": "parse_error: no_table_found (markup changed or blocked)",
            "events": [],
        }

    # Extract rows
    events: List[Dict[str, Any]] = []

    # The widget frequently uses date separator rows. We'll keep the last seen date label.
    current_date_label: Optional[str] = None

    for tr in table.find_all("tr"):
        # date separator rows
        if tr.get("class") and any("theDay" in c or "theDay" == c for c in tr.get("class")):  # defensive
            current_date_label = tr.get_text(" ", strip=True) or current_date_label
            continue

        tds = tr.find_all("td")
        if not tds:
            continue

        # Heuristic mapping by class names in each cell
        time_text = None
        currency = None
        impact = None
        event_name = None
        actual = None
        forecast = None
        previous = None

        for td in tds:
            cls = " ".join(td.get("class", []))
            txt = td.get_text(" ", strip=True)

            if "time" in cls:
                time_text = txt or None
            elif "currency" in cls:
                currency = txt or None
            elif "sentiment" in cls or "importance" in cls:
                # impact is often shown as icons; fallback to title/count
                title = td.get("title")
                if title:
                    impact = title
                else:
                    # count bull icons
                    bulls = td.find_all("i", class_=lambda x: x and "grayFullBullishIcon" in x)
                    if bulls:
                        impact = f"{len(bulls)}/3"
                    else:
                        impact = txt or None
            elif "event" in cls:
                # Event name may be in an <a>
                a = td.find("a")
                event_name = (a.get_text(" ", strip=True) if a else txt) or None
            elif "act" in cls:
                actual = txt or None
            elif "fore" in cls:
                forecast = txt or None
            elif "prev" in cls:
                previous = txt or None

        # Skip blank/incomplete rows
        if not (event_name or currency or time_text):
            continue

        events.append({
            "date_label": current_date_label,
            "datetime": time_text,  # EXACT as shown (no conversion)
            "event": event_name,
            "country_or_currency": currency,
            "impact": impact,
            "previous": previous,
            "forecast": forecast,
            "actual": actual,
            "category": "macro",
            "source": "https://es.investing.com/economic-calendar-/",
        })

        if len(events) >= max_rows:
            break

    return {
        "ok": True,
        "source": "investing_economic_widget",
        "url": r.url,
        "as_of": _utc_now_iso(),
        "source_tz": "as_shown_by_source",
        "events": events,
    }


# ----------------------------
# BabyPips calendar (best-effort HTML parse)
# ----------------------------

def _scrape_babypips_calendar(max_rows: int = 200) -> Dict[str, Any]:
    url = "https://www.babypips.com/economic-calendar"
    try:
        r = _http_get(url)
    except Exception as e:
        return {"ok": False, "source": "babypips_calendar", "url": url, "as_of": _utc_now_iso(), "error": f"http_error: {e}", "events": []}

    soup = BeautifulSoup(r.text, "html.parser")

    # BabyPips markup can change; we do a conservative extraction:
    # find rows that contain currency + event name.
    events: List[Dict[str, Any]] = []

    # Common pattern: table rows with data attributes
    rows = soup.find_all("tr")
    for tr in rows:
        txt = tr.get_text(" ", strip=True)
        if not txt:
            continue

        # Heuristics: must contain a currency code like USD/EUR/GBP/JPY
        if not any(cc in txt.split() for cc in ("USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD")):
            continue

        tds = tr.find_all("td")
        if len(tds) < 3:
            continue

        # Best effort mapping
        time_text = tds[0].get_text(" ", strip=True) if len(tds) > 0 else None
        currency = None
        event_name = None

        for td in tds:
            t = td.get_text(" ", strip=True)
            if t in ("USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"):
                currency = t
        # event name: choose the longest cell text
        cell_texts = [td.get_text(" ", strip=True) for td in tds]
        event_name = max(cell_texts, key=lambda s: len(s)) if cell_texts else None

        events.append({
            "datetime": time_text,
            "event": event_name,
            "country_or_currency": currency,
            "impact": None,
            "previous": None,
            "forecast": None,
            "actual": None,
            "category": "macro",
            "source": url,
        })

        if len(events) >= max_rows:
            break

    if not events:
        return {
            "ok": False,
            "source": "babypips_calendar",
            "url": r.url,
            "as_of": _utc_now_iso(),
            "error": "parse_error: no_events_extracted (markup changed or dynamic content)",
            "events": [],
        }

    return {"ok": True, "source": "babypips_calendar", "url": r.url, "as_of": _utc_now_iso(), "source_tz": "as_shown_by_source", "events": events}


# ----------------------------
# ForexFactory calendar (best-effort HTML parse)
# ----------------------------

def _scrape_forexfactory_calendar(max_rows: int = 200) -> Dict[str, Any]:
    url = "https://www.forexfactory.com/calendar"
    try:
        r = _http_get(url)
    except Exception as e:
        return {"ok": False, "source": "forexfactory_calendar", "url": url, "as_of": _utc_now_iso(), "error": f"http_error: {e}", "events": []}

    soup = BeautifulSoup(r.text, "html.parser")

    # ForexFactory often uses a table with rows having class 'calendar__row'
    rows = soup.find_all("tr", class_=lambda x: x and "calendar__row" in x)
    events: List[Dict[str, Any]] = []

    current_date_label: Optional[str] = None

    for tr in rows:
        # date label row
        date_cell = tr.find("td", class_=lambda x: x and "calendar__date" in x)
        if date_cell:
            dl = date_cell.get_text(" ", strip=True)
            if dl:
                current_date_label = dl

        time_cell = tr.find("td", class_=lambda x: x and "calendar__time" in x)
        curr_cell = tr.find("td", class_=lambda x: x and "calendar__currency" in x)
        imp_cell = tr.find("td", class_=lambda x: x and "calendar__impact" in x)
        evt_cell = tr.find("td", class_=lambda x: x and "calendar__event" in x)
        act_cell = tr.find("td", class_=lambda x: x and "calendar__actual" in x)
        for_cell = tr.find("td", class_=lambda x: x and "calendar__forecast" in x)
        prev_cell = tr.find("td", class_=lambda x: x and "calendar__previous" in x)

        event_name = evt_cell.get_text(" ", strip=True) if evt_cell else None
        currency = curr_cell.get_text(" ", strip=True) if curr_cell else None
        time_text = time_cell.get_text(" ", strip=True) if time_cell else None

        if not (event_name or currency or time_text):
            continue

        # Impact is usually icons; get title or count
        impact = None
        if imp_cell:
            impact = imp_cell.get("title") or imp_cell.get_text(" ", strip=True) or None

        events.append({
            "date_label": current_date_label,
            "datetime": time_text,
            "event": event_name,
            "country_or_currency": currency,
            "impact": impact,
            "previous": prev_cell.get_text(" ", strip=True) if prev_cell else None,
            "forecast": for_cell.get_text(" ", strip=True) if for_cell else None,
            "actual": act_cell.get_text(" ", strip=True) if act_cell else None,
            "category": "macro",
            "source": url,
        })

        if len(events) >= max_rows:
            break

    if not events:
        return {
            "ok": False,
            "source": "forexfactory_calendar",
            "url": r.url,
            "as_of": _utc_now_iso(),
            "error": "parse_error: no_events_extracted (markup changed, blocked, or dynamic)",
            "events": [],
        }

    return {"ok": True, "source": "forexfactory_calendar", "url": r.url, "as_of": _utc_now_iso(), "source_tz": "as_shown_by_source", "events": events}


# -----------------------------------------------------------------------------
# CrewAI Tool
# -----------------------------------------------------------------------------

class ScrapeMacroInput(BaseModel):
    source: Literal[
        "investing_economic",
        "babypips_calendar",
        "forexfactory_calendar",
    ] = Field(..., description="Fuente específica a scrapear")

    cal_type: Literal["day", "week"] = Field(
        "day",
        description="(Solo investing_economic) Tipo de calendario para el widget: day|week",
    )

    # Keep these configurable because widget params differ by environment.
    time_zone: str = Field(
        "8",
        description="(Solo investing_economic) ID de zona horaria del widget. NO convertir horas.",
    )
    lang: str = Field(
        "1",
        description="(Solo investing_economic) Idioma del widget (depende de Investing).",
    )

    max_rows: int = Field(200, ge=1, le=500, description="Máximo de filas/eventos a devolver")


class ScrapeMacroCalendarTool(BaseTool):
    name: str = "scrape_macro_calendar"
    description: str = (
        "Obtiene y parsea datos desde fuentes específicas de calendario macro (Investing widget / BabyPips / ForexFactory). "
        "Devuelve JSON estructurado. Si falla, devuelve ok=false y events=[] (NO inventar)."
    )
    args_schema: type = ScrapeMacroInput

    def _run(
        self,
        source: str,
        cal_type: str = "day",
        time_zone: str = "8",
        lang: str = "1",
        max_rows: int = 200,
    ) -> str:
        if source == "investing_economic":
            data = _scrape_investing_economic_widget(
                cal_type=cal_type, time_zone=time_zone, lang=lang, max_rows=max_rows
            )
        elif source == "babypips_calendar":
            data = _scrape_babypips_calendar(max_rows=max_rows)
        elif source == "forexfactory_calendar":
            data = _scrape_forexfactory_calendar(max_rows=max_rows)
        else:
            data = {
                "ok": False,
                "source": source,
                "as_of": _utc_now_iso(),
                "error": "unsupported_source",
                "events": [],
            }

        return json.dumps(data, ensure_ascii=False)

