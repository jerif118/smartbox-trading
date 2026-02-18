import os
import sys
import json
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

warnings.filterwarnings("ignore", category=SyntaxWarning, module="pysbd")
load_dotenv()

from utils.logger import get_logger                               # noqa: E402
from utils.env_validator import validate_env                      # noqa: E402
from preprocess.process_pipeline import preprocess_data           # noqa: E402
from preprocess.breakout_monitor import monitor_breakout          # noqa: E402
from tools_bot.time_now import unix_time                          # noqa: E402
from strategy_ai.crew import StrategyAi                           # noqa: E402

log = get_logger(__name__)

# ── Config ────────────────────────────────────────────────────────────
SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", "US500").split(",")]
MARKET  = os.getenv("MARKET", "S&P 500 / Forex")
TZ      = ZoneInfo("America/Lima")
BOX_END_HOUR = os.getenv("BOX_END", "09:55")


def _box_date() -> str:
    """Devuelve BOX_DATE del .env o la fecha de hoy (hora Lima)."""
    bd = os.getenv("BOX_DATE")
    return bd if bd else datetime.now(TZ).strftime("%Y-%m-%d")


# ══════════════════════════════════════════════════════════════════════
#  FLUJO PRINCIPAL
# ══════════════════════════════════════════════════════════════════════

def run():
    """Ejecuta el flujo completo de la estrategia de la caja."""

    # ═══ Validación de entorno ════════════════════════════════════
    if not validate_env():
        log.error("Proceso abortado: variables de entorno incompletas")
        sys.exit(1)

    box_date = _box_date()

    # ═══ ETAPA 1 · Preprocesamiento ══════════════════════════════════
    log.info("═" * 60)
    log.info("  ETAPA 1 · Preprocesamiento  (%d símbolo(s))", len(SYMBOLS))
    log.info("  Fecha caja : %s", box_date)
    log.info("═" * 60)

    preprocessed: dict = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(preprocess_data, symbol=s): s for s in SYMBOLS}
        for future in as_completed(futures):
            sym = futures[future]
            try:
                preprocessed[sym] = future.result()
            except Exception as e:
                log.error("[preprocess] %s: ERROR → %s", sym, e, exc_info=True)
                preprocessed[sym] = None

    # ═══ ETAPA 2 · Filtro de amplitud ════════════════════════════════
    tradeable: dict = {}
    for sym, result in preprocessed.items():
        if result is None:
            log.warning("[filtro] %s: amplitud > 1%% o sin datos → NO se consulta la IA", sym)
            continue
        tradeable[sym] = result

    if not tradeable:
        log.info("[FIN] Ningún símbolo pasó el filtro de amplitud. Proceso detenido.")
        return

    # ═══ ETAPA 3 · Monitoreo breakout 5 min (máx 2 h) ═══════════════
    log.info("═" * 60)
    log.info("  ETAPA 3 · Monitoreo breakout 5 min  (%d símbolo(s))", len(tradeable))
    log.info("  Ventana máxima : 2 horas post cierre de caja")
    log.info("═" * 60)

    breakouts: dict = {}

    def _monitor_sym(sym: str):
        result = tradeable[sym]
        box = result.features.get("box", {})
        bh, bl = box.get("high"), box.get("low")
        if bh is None or bl is None:
            return sym, None
        _, box_end_unix = unix_time(
            f"{box_date}T{BOX_END_HOUR}:00",
            f"{box_date}T{BOX_END_HOUR}:00",
        )
        log.info("[monitor] %s: caja %.2f–%.2f, vigilando 5 min post caja …", sym, bl, bh)
        return sym, monitor_breakout(sym, bh, bl, box_end_unix)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(_monitor_sym, s): s for s in tradeable}
        for future in as_completed(futures):
            sym_key = futures[future]
            try:
                symbol, signal = future.result()
                if signal:
                    breakouts[symbol] = signal
                    log.info("[BREAKOUT] %s: %s close=%s", symbol,
                             signal['breakout_state'], signal['candle_close'])
                else:
                    log.info("[monitor] %s: sin breakout en 2 h → NO se consulta IA", sym_key)
            except Exception as e:
                log.error("[monitor] %s: error → %s", sym_key, e, exc_info=True)

    if not breakouts:
        log.info("[FIN] Ningún breakout detectado en 2 h. Proceso detenido sin consultar IA.")
        return

    # ═══ ETAPA 4 · Construir datos y lanzar IA ═══════════════════════
    log.info("═" * 60)
    log.info("  ETAPA 4 · Consulta IA  (%d breakout(s) detectados)", len(breakouts))
    log.info("═" * 60)

    symbols_data  = []
    execution_data = {}

    for sym, signal in breakouts.items():
        result   = tradeable[sym]
        features = result.features
        box = features.get("box", {})
        vp  = features.get("volume_profile") or {}

        bh, bl = box.get("high"), box.get("low")
        box_mid = round((bh + bl) / 2, 2) if bh and bl else None

        # Datos para ejecutar la orden (SimpleFX / fallback Capital)
        execution_data[sym] = {
            "box_high":         bh,
            "box_low":          bl,
            "amplitud":         box.get("amplitud"),
            "high_simple":      box.get("high_simple"),
            "low_simple":       box.get("low_simple"),
            "amplitud_simple":  box.get("amplitud_simple"),
        }

        # JSON que recibe la IA (sin datos SimpleFX)
        symbols_data.append({
            "symbol": sym,
            "breakout_signal": signal,
            "caja": {
                "high": bh,
                "low":  bl,
                "mid":  box_mid,
                "amp_pct": box.get("amplitud"),
                "hour_range": box.get("hour_range"),
            },
            "vp": {
                "poc":   vp.get("poc"),
                "hva":   vp.get("vah"),
                "lva":   vp.get("val"),
                "peaks": vp.get("peaks", []),
                "total_volume": vp.get("total_volume"),
            },
            "rsi": {
                "last":   features.get("rsi_last"),
                "points": features.get("rsi_points", []),
            },
        })

    inputs = {
        "symbols_data": json.dumps(symbols_data, indent=2, default=str),
        "market": MARKET,
    }

    # Crear crew e inyectar datos de ejecución para after_kickoff
    strategy = StrategyAi()
    strategy._execution_data = execution_data

    try:
        strategy.crew().kickoff(inputs=inputs)
        log.info("[FIN] Crew finalizado correctamente.")
    except Exception as e:
        log.critical("Error ejecutando el crew: %s", e, exc_info=True)
        raise


# ══════════════════════════════════════════════════════════════════════
#  Funciones auxiliares requeridas por pyproject.toml [project.scripts]
# ══════════════════════════════════════════════════════════════════════

def train():
    """Entrena el crew por N iteraciones."""
    inputs = {"symbols_data": "[]", "market": MARKET}
    try:
        StrategyAi().crew().train(
            n_iterations=int(sys.argv[1]),
            filename=sys.argv[2],
            inputs=inputs,
        )
    except Exception as e:
        raise Exception(f"Error durante entrenamiento: {e}")


def replay():
    """Re-ejecuta desde una tarea específica."""
    try:
        StrategyAi().crew().replay(task_id=sys.argv[1])
    except Exception as e:
        raise Exception(f"Error durante replay: {e}")


def test():
    """Ejecuta test del crew."""
    inputs = {"symbols_data": "[]", "market": MARKET}
    try:
        StrategyAi().crew().test(
            n_iterations=int(sys.argv[1]),
            eval_llm=sys.argv[2],
            inputs=inputs,
        )
    except Exception as e:
        raise Exception(f"Error durante test: {e}")


def run_with_trigger():
    """Ejecuta con payload de trigger externo."""
    if len(sys.argv) < 2:
        raise Exception("Falta payload JSON como argumento.")
    try:
        trigger = json.loads(sys.argv[1])
    except json.JSONDecodeError:
        raise Exception("Payload JSON inválido.")

    inputs = {
        "crewai_trigger_payload": trigger,
        "symbols_data": "[]",
        "market": MARKET,
    }
    try:
        return StrategyAi().crew().kickoff(inputs=inputs)
    except Exception as e:
        raise Exception(f"Error con trigger: {e}")
