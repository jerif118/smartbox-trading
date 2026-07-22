"""
Tools y helpers del crew.

- analyze_multi_timeframe: sesgo determinista 15m/1h/4h (función reutilizada por
  el pipeline s5_analyze; no es una tool del LLM).
- DrawdownGuardTool: única tool del LLM (la usa el Risk) — chequea drawdown diario.

Las señales técnicas (confluence y MTF) se calculan deterministas en s5_analyze y
se le inyectan al Trader; el LLM no las computa (no puede alterar el número final).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from domain.signals.mtf import ema_bias, mtf_alignment


# ── Multi-timeframe (sesgo determinista, reutilizado por el pipeline) ──
def analyze_multi_timeframe(
    symbol: str, proposed_direction: str, adapter=None
) -> dict:
    """Sesgo multi-timeframe (15min/1h/4h) determinista.

    Fuente de verdad reutilizada por la tool del LLM y por el pipeline
    (s5_analyze) para derivar mtf_alignment sin depender del tool-call del LLM.
    Ante cualquier fallo devuelve un dict con ``error`` y alignment MIXED
    (conservador: no confirma alineación que no se pudo verificar).
    """
    try:
        import time

        if adapter is None:
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
            biases[tf_name] = ema_bias(df["close"].tolist())

        alignment = mtf_alignment(biases, proposed_direction)
        return {
            "symbol": symbol,
            "htf_bias": biases.get("4h", "NEUTRAL"),
            "tf_biases": biases,
            "mtf_alignment": alignment,
            "veto_recommended": alignment == "COUNTER",
            "notes": (
                "Operar contra el HTF reduce la probabilidad de éxito. "
                "Recomiendo VETO o reducir tamaño."
            ),
        }
    except Exception as e:
        return {"error": str(e), "mtf_alignment": "MIXED", "veto_recommended": False}


# ── DrawdownGuardTool ─────────────────────────────────────────────────
class DrawdownGuardInput(BaseModel):
    max_daily_loss: float = Field(..., description="Max pérdida diaria permitida")
    current_daily_pnl: float = Field(default=0.0, description="P&L del día actual")


class DrawdownGuardTool(BaseTool):
    name: str = "drawdown_guard"
    description: str = (
        "Chequea si la pérdida diaria actual excede el máximo. Si excede, retorna VETO."
    )

    args_schema: type[BaseModel] = DrawdownGuardInput

    def _run(self, max_daily_loss: float, current_daily_pnl: float = 0.0) -> str:
        import sqlite3

        from infrastructure.config.settings import get_settings

        try:
            with sqlite3.connect(get_settings().db_path) as conn:
                today = datetime.now(UTC).date().isoformat()
                row = conn.execute(
                    "SELECT COALESCE(SUM(pnl), 0) FROM trades "
                    "WHERE DATE(ts_close) = ? AND pnl IS NOT NULL",
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
