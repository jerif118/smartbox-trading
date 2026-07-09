"""
Tools que usan los agentes.

- ScrapeMacroCalendarTool: extrae eventos macro
- SearchNewsTool: busca noticias
- MultiTimeframeTool: análisis multi-timeframe
- ConfluenceScoreTool: score cuantitativo de confluencia
- DrawdownGuard: chequea drawdown diario
- AnalyzeBoxTool: análisis de la caja
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from crewai.tools import BaseTool
from pydantic import BaseModel, Field


# ── ScrapeMacroCalendarTool ───────────────────────────────────────────
class ScrapeMacroCalendarInput(BaseModel):
    date: str = Field(default="today", description="YYYY-MM-DD o 'today'")
    time_zone: str = Field(default="America/New_York", description="IANA TZ")


class ScrapeMacroCalendarTool(BaseTool):
    name: str = "scrape_macro_calendar"
    description: str = "Scrapea calendario macro. Filtra eventos HIGH impact."

    args_schema: type[BaseModel] = ScrapeMacroCalendarInput

    def _run(self, date: str = "today", time_zone: str = "America/New_York") -> str:
        from infrastructure.data_sources.scrapers import InvestingMacroCalendarAdapter

        adapter = InvestingMacroCalendarAdapter()
        events = adapter.get_high_impact_events(date)
        return json.dumps({"date": date, "events": events[:10]}, ensure_ascii=False)


# ── SearchNewsTool ────────────────────────────────────────────────────
class SearchNewsInput(BaseModel):
    query: str = Field(..., description="Query de búsqueda")
    days_back: int = Field(default=1, description="Días hacia atrás")


class SearchNewsTool(BaseTool):
    name: str = "search_financial_news"
    description: str = "Busca noticias financieras via DuckDuckGo."

    args_schema: type[BaseModel] = SearchNewsInput

    def _run(self, query: str, days_back: int = 1) -> str:
        from infrastructure.data_sources.scrapers import DuckDuckGoNewsAdapter

        adapter = DuckDuckGoNewsAdapter()
        results = adapter.search(query, days_back)
        return json.dumps({"query": query, "news": results}, ensure_ascii=False)


# ── MultiTimeframeTool ────────────────────────────────────────────────
class MultiTimeframeInput(BaseModel):
    symbol: str = Field(..., description="Símbolo a analizar")
    proposed_direction: str = Field(..., description="LONG o SHORT")


class MultiTimeframeTool(BaseTool):
    name: str = "multi_timeframe_analysis"
    description: str = (
        "Analiza el sesgo del símbolo en 15min, 1h, 4h. "
        "Retorna: htf_bias, mtf_alignment (ALIGNED/COUNTER/MIXED), "
        "y recomendación de veto si la dirección está contra HTF."
    )

    args_schema: type[BaseModel] = MultiTimeframeInput

    def _run(self, symbol: str, proposed_direction: str) -> str:
        try:
            import time

            from infrastructure.broker.capital.adapter import CapitalAdapter

            adapter = CapitalAdapter()
            now = int(time.time())
            tfs = {
                "15min": ("MINUTE_15", 15 * 86400),
                "1h": ("HOUR", 30 * 86400),
                "4h": ("HOUR_4", 60 * 86400),
            }
            biases: dict[str, str] = {}
            for tf_name, (resolution, lookback) in tfs.items():
                df = adapter.get_candles(symbol, resolution, now - lookback, now, max_candles=200)
                if df.empty or len(df) < 20:
                    biases[tf_name] = "NEUTRAL"
                    continue
                closes = df["close"]
                ema20 = closes.ewm(span=20, adjust=False).mean()
                if closes.iloc[-1] > ema20.iloc[-1]:
                    biases[tf_name] = "BULLISH"
                elif closes.iloc[-1] < ema20.iloc[-1]:
                    biases[tf_name] = "BEARISH"
                else:
                    biases[tf_name] = "NEUTRAL"

            htf = biases.get("4h", "NEUTRAL")
            target = "BULLISH" if proposed_direction == "LONG" else "BEARISH"
            aligned_count = sum(1 for b in biases.values() if b == target)
            counter_count = sum(1 for b in biases.values() if b != "NEUTRAL" and b != target)

            if counter_count >= 2:
                alignment = "COUNTER"
            elif aligned_count >= 2:
                alignment = "ALIGNED"
            else:
                alignment = "MIXED"

            return json.dumps(
                {
                    "symbol": symbol,
                    "htf_bias": htf,
                    "tf_biases": biases,
                    "mtf_alignment": alignment,
                    "veto_recommended": alignment == "COUNTER",
                    "notes": (
                        "Operar contra el HTF reduce la probabilidad de éxito. "
                        "Recomiendo VETO o reducir tamaño."
                    ),
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": str(e), "veto_recommended": False})


# ── ConfluenceScoreTool ───────────────────────────────────────────────
class ConfluenceScoreInput(BaseModel):
    rsi_value: float = Field(..., description="Último RSI")
    rsi_direction: str = Field(..., description="oversold/overbought/neutral")
    poc_above_mid: bool = Field(..., description="POC > box mid?")
    breakout_aligned: bool = Field(..., description="Breakout en dirección propuesta?")
    mtf_aligned: bool = Field(..., description="MTF alineado?")


class ConfluenceScoreTool(BaseTool):
    name: str = "confluence_score"
    description: str = (
        "Calcula score 0-100 de confluencia técnica basado en reglas duras. "
        "Si score < 60 → NO_OPERAR."
    )

    args_schema: type[BaseModel] = ConfluenceScoreInput

    def _run(
        self,
        rsi_value: float,
        rsi_direction: str,
        poc_above_mid: bool,
        breakout_aligned: bool,
        mtf_aligned: bool,
    ) -> str:
        score = 0
        factors: list[str] = []

        if rsi_direction == "neutral":
            score += 25
            factors.append("RSI neutral (25)")
        elif rsi_direction == "oversold" and rsi_value < 35:
            score += 20
            factors.append("RSI oversold (20)")
        elif rsi_direction == "overbought" and rsi_value > 65:
            score += 20
            factors.append("RSI overbought (20)")

        if poc_above_mid:
            score += 25
            factors.append("POC above mid - soporte (25)")
        else:
            score += 15
            factors.append("POC below mid (15)")

        if breakout_aligned:
            score += 25
            factors.append("Breakout en dirección (25)")

        if mtf_aligned:
            score += 25
            factors.append("MTF alineado (25)")

        recommendation = "PROCEED" if score >= 60 else "NO_OPERAR"
        return json.dumps(
            {"score": score, "recommendation": recommendation, "factors": factors},
            ensure_ascii=False,
        )


# ── DrawdownGuardTool ─────────────────────────────────────────────────
class DrawdownGuardInput(BaseModel):
    max_daily_loss: float = Field(..., description="Max pérdida diaria permitida")
    current_daily_pnl: float = Field(default=0.0, description="P&L del día actual")


class DrawdownGuardTool(BaseTool):
    name: str = "drawdown_guard"
    description: str = (
        "Chequea si la pérdida diaria actual excede el máximo. "
        "Si excede, retorna VETO."
    )

    args_schema: type[BaseModel] = DrawdownGuardInput

    def _run(self, max_daily_loss: float, current_daily_pnl: float = 0.0) -> str:
        import sqlite3

        from infrastructure.config.settings import get_settings

        try:
            with sqlite3.connect(get_settings().db_path) as conn:
                today = datetime.now(UTC).date().isoformat()
                row = conn.execute(
                    "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE DATE(ts_close) = ? AND pnl IS NOT NULL",
                    (today,),
                ).fetchone()
        except Exception:
            row = (0,)

        realized = float(row[0] or 0)
        total_daily = current_daily_pnl + realized
        exceeded = abs(min(0.0, total_daily)) >= max_daily_loss
        return json.dumps(
            {
                "daily_pnl": round(total_daily, 2),
                "max_daily_loss": max_daily_loss,
                "exceeded": exceeded,
                "recommendation": "VETO" if exceeded else "PROCEED",
            },
            ensure_ascii=False,
        )


# ── AnalyzeBoxTool ────────────────────────────────────────────────────
class AnalyzeBoxInput(BaseModel):
    box_high: float
    box_low: float
    box_amplitude_pct: float
    n_candles: int = 0


class AnalyzeBoxTool(BaseTool):
    name: str = "analyze_box"
    description: str = "Analiza amplitud y forma de la caja. Retorna SKIP si amplitud > 1%."

    args_schema: type[BaseModel] = AnalyzeBoxInput

    def _run(
        self,
        box_high: float,
        box_low: float,
        box_amplitude_pct: float,
        n_candles: int = 0,
    ) -> str:
        try:
            amp_ok = box_amplitude_pct < 1.0
            box_range = box_high - box_low
            form = "wide" if box_amplitude_pct > 0.7 else "narrow" if box_amplitude_pct < 0.4 else "normal"
            recommendation = "PROCEED" if amp_ok else "SKIP"
            return json.dumps(
                {
                    "amplitude_ok": amp_ok,
                    "amplitude_pct": box_amplitude_pct,
                    "box_range": box_range,
                    "candle_count": n_candles,
                    "form": form,
                    "recommendation": recommendation,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": str(e)})
