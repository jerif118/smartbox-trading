"""
Real Tools for Agent Crew
==========================
These tools perform REAL actions - web scraping, searching.
"""

import json
import os

import requests
from crewai.tools import BaseTool


class ScrapeMacroCalendarTool(BaseTool):
    name: str = "scrape_macro_calendar"
    description: str = "Scraper calendario económico de investing.com. Filtra HIGH impact events. Return: lista de eventos."

    def _run(self, date: str = "today", time_zone: str = "America/New_York") -> str:
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            target_date = date if date != "today" else datetime.now(ZoneInfo(time_zone)).strftime("%Y-%m-%d")

            url = f"https://www.investing.com/economic-calendar/-1-{target_date}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }

            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code != 200:
                return json.dumps({"error": f"HTTP {response.status_code}", "events": []})

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.text, "html.parser")
            events = []

            rows = soup.select("table.ec-table tbody tr")
            for row in rows[:15]:
                try:
                    impact_cell = row.select_one(".impact span")
                    if impact_cell and "high" in impact_cell.get("class", []):
                        time_el = row.select_one("td.time")
                        event_el = row.select_one("td.event")
                        curr_el = row.select_one("td.curr")

                        event_time = time_el.text.strip() if time_el else ""
                        event_name = event_el.text.strip() if event_el else ""
                        currency = curr_el.text.strip() if curr_el else ""

                        if event_name:
                            events.append({
                                "time": event_time,
                                "event": event_name,
                                "currency": currency,
                                "impact": "HIGH"
                            })
                except Exception:
                    continue

            return json.dumps({"date": target_date, "events": events[:10]}, indent=2)

        except Exception as e:
            return json.dumps({"error": str(e), "events": []})


class SearchNewsTool(BaseTool):
    name: str = "search_news"
    description: str = "Buscar noticias recientes via newsapi.org. Return: titulos, fuentes, timestamps."

    def _run(self, query: str, days_back: int = 1) -> str:
        try:
            news_api_key = os.getenv("NEWS_API_KEY", "")

            if not news_api_key:
                return json.dumps({
                    "query": query,
                    "error": "NEWS_API_KEY not configured",
                    "articles": []
                })

            from datetime import datetime, timedelta
            from zoneinfo import ZoneInfo

            date_from = (datetime.now(ZoneInfo("UTC")) - timedelta(days=days_back)).strftime("%Y-%m-%d")

            url = "https://newsapi.org/v2/everything"
            params = {
                "q": query,
                "from": date_from,
                "language": "en",
                "sortBy": "publishedAt",
                "apiKey": news_api_key,
                "pageSize": 5
            }

            response = requests.get(url, params=params, timeout=10)
            if response.status_code != 200:
                return json.dumps({"error": f"HTTP {response.status_code}", "articles": []})

            data = response.json()
            articles = [
                {
                    "title": a.get("title", ""),
                    "source": a.get("source", {}).get("name", ""),
                    "url": a.get("url", ""),
                    "published": a.get("publishedAt", "")
                }
                for a in data.get("articles", [])[:5]
            ]

            return json.dumps({"query": query, "articles": articles}, indent=2)

        except Exception as e:
            return json.dumps({"error": str(e), "articles": []})
