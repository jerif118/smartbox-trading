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

from crewai.tools import BaseTool
from pydantic import BaseModel

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
    """Sin parámetros a propósito.

    Una barrera de riesgo cuyos umbrales los elige el propio modelo no es una
    barrera. Antes el LLM pasaba `max_daily_loss` y `current_daily_pnl`, y
    cualquiera de los dos daba la vuelta al veredicto: bastaba con declarar un
    límite enorme o un P&L positivo para convertir un VETO en PROCEED. Ahora el
    límite sale de settings y el P&L de la DB.
    """


class DrawdownGuardTool(BaseTool):
    name: str = "drawdown_guard"
    description: str = (
        "Chequea si la pérdida realizada de hoy excede el máximo diario permitido. "
        "NO recibe parámetros: el límite sale de la configuración del sistema y el "
        "P&L de la base de datos. Retorna VETO si se excedió, PROCEED si no."
    )

    args_schema: type[BaseModel] = DrawdownGuardInput

    def _run(self, **_kwargs: object) -> str:
        """Ignora cualquier argumento que mande el LLM: los datos son del sistema."""
        from infrastructure.config.settings import get_settings
        from infrastructure.persistence.sqlite import trade_repo

        max_daily_loss = float(get_settings().max_daily_loss)
        try:
            # MISMA fuente que el freno duro de s6_execute, para que no puedan
            # divergir: si uno dice VETO, el otro también.
            realized = trade_repo.realized_pnl_today()
        except Exception as e:  # noqa: BLE001 — sin dato fiable no se certifica nada
            # Fail-closed: no poder leer el P&L no es prueba de que no haya pérdida.
            return json.dumps(
                {
                    "daily_pnl": None,
                    "max_daily_loss": max_daily_loss,
                    "exceeded": True,
                    "recommendation": "VETO",
                    "error": f"P&L del día no disponible: {e}",
                },
                ensure_ascii=False,
            )

        exceeded = realized <= -max_daily_loss
        return json.dumps(
            {
                "daily_pnl": round(realized, 2),
                "max_daily_loss": max_daily_loss,
                "exceeded": exceeded,
                "recommendation": "VETO" if exceeded else "PROCEED",
            },
            ensure_ascii=False,
        )
