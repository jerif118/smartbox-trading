"""
Scrapers para calendario macro y noticias.

Implementa `MacroCalendarProvider` y `NewsProvider`.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import requests
from bs4 import BeautifulSoup

from utils.logger import get_logger
from utils.retry import retry

log = get_logger(__name__)


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
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
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
        except Exception as e:
            log.warning("DuckDuckGo search failed: %s", e)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        for item in soup.find_all("a", class_="result__a")[:5]:
            title = item.get_text(strip=True)
            url_href = item.get("href", "")
            snippet_el = item.find_parent("li").find("a", class_="result__snippet") if item.find_parent("li") else None
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
        except Exception as e:
            log.warning("NewsAPI failed: %s", e)
            return []

        data = resp.json()
        return [
            {
                "title": a.get("title", ""),
                "source": a.get("source", {}).get("name", ""),
                "url": a.get("url", ""),
                "published": a.get("publishedAt", ""),
            }
            for a in data.get("articles", [])[:5]
        ]
