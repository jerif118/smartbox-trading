import os
import sys
import json
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

warnings.filterwarnings("ignore", category=SyntaxWarning, module="pysbd")
load_dotenv()

from utils.logger import get_logger
from utils.env_validator import validate_env
from preprocess.process_pipeline import preprocess_data
from preprocess.breakout_monitor import monitor_breakout
from tools_bot.time_now import unix_time
from tools_bot.interval_fecha import is_trading_day
from strategy_ai.crew import StrategyAi

log = get_logger(__name__)

# ── Config ────────────────────────────────────────────────────────────
SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", "US500").split(",")]
PRIMARY_SYMBOL = os.getenv("PRIMARY_SYMBOL", "US500")
MARKET = os.getenv("MARKET", "S&P 500 / Forex")
TZ_STR = os.getenv("TIMEZONE", "America/New_York")
MARKET_TZ = os.getenv("MARKET_TZ", "America/New_York")
TZ = ZoneInfo(TZ_STR)
BOX_END_HOUR = os.getenv("BOX_END", "09:55")
BOX_START_HOUR = os.getenv("BOX_START", "08:00")
MIN_CONFLUENCE_SCORE = 60


def _box_date() -> str:
    bd = os.getenv("BOX_DATE")
    return bd if bd else datetime.now(TZ).strftime("%Y-%m-%d")


def _is_primary(symbol: str) -> bool:
    return symbol.upper().replace("-", "_") == PRIMARY_SYMBOL.upper().replace("-", "_")


def _get_box_times(box_date: str):
    start_str = f"{box_date}T{BOX_START_HOUR}:00"
    end_str = f"{box_date}T{BOX_END_HOUR}:00"
    return unix_time(start_str, end_str)


def _monitor_sym(sym: str, tradeable: dict, box_date: str):
    result = tradeable[sym]
    box = result.features.get("box", {})
    bh, bl = box.get("high"), box.get("low")
    if bh is None or bl is None:
        return sym, None
    _, box_end_unix = _get_box_times(box_date)
    return sym, monitor_breakout(sym, bh, bl, box_end_unix)


# ══════════════════════════════════════════════════════════════════════
#  FLUJO PRINCIPAL - Agentes expertos evalúan dinámicamente
# ══════════════════════════════════════════════════════════════════════

def run():
    """
    Trader humano: envía TODOS los breakouts a los agentes expertos.
    Cada agente pesa los factores según contexto político/económico del día.
    Los agentes debaten yunan consensus sobre cuál es el MEJOR setup.
    """

    if not validate_env():
        log.error("Proceso abortado: variables de entorno incompletas")
        sys.exit(1)

    box_date = _box_date()

    if not is_trading_day(box_date):
        log.warning("[skip] %s fin de semana.", box_date)
        return

    _, primary_box_end = _get_box_times(box_date)
    max_wait = primary_box_end + 7200

    log.info("═" * 60)
    log.info("  EVALUACIÓN MULTI-SÍMBOLO CON AGENTES EXPERTOS")
    log.info("  PRIMARY: %s | Box close: %s | Max wait: 2h post close", PRIMARY_SYMBOL, BOX_END_HOUR)
    log.info("═" * 60)

    # ─── ETAPA 1: Preprocesamiento parallel ────
    log.info("  ETAPA 1 · Preprocesamiento (%d símbolos)", len(SYMBOLS))

    preprocessed: dict = {}
    with ThreadPoolExecutor(max_workers=len(SYMBOLS)) as pool:
        futures = {pool.submit(preprocess_data, symbol=s): s for s in SYMBOLS}
        for future in as_completed(futures):
            sym = futures[future]
            try:
                preprocessed[sym] = future.result()
            except Exception as e:
                log.error("[preprocess] %s: ERROR → %s", sym, e, exc_info=True)
                preprocessed[sym] = None

    tradeable = {sym: r for sym, r in preprocessed.items() if r is not None}
    if not tradeable:
        log.info("[FIN] Ningún símbolo viable.")
        return

    # ─── ETAPA 2: Boxes calculadas - información lista ────
    log.info("  ETAPA 2 · Boxes calculadas (%d símbolos)", len(tradeable))
    for sym, r in tradeable.items():
        box = r.features.get("box", {})
        vp = r.features.get("volume_profile") or {}
        log.info("    %s: box %.2f-%.2f | amp=%.2f%% | RSI=%s | POC=%.2f",
                sym, box.get("low"), box.get("high"), box.get("amplitud"),
                r.features.get("rsi_last"), vp.get("poc"))
    log.info("═" * 60)

    # ─── ETAPA 3: Monitoreo - todos los breakouts ────
    log.info("  ETAPA 3 · Esperando breakouts (max 2h post close)")

    all_breakouts: dict = {}
    primary_breakout_detected = False

    with ThreadPoolExecutor(max_workers=len(tradeable)) as pool:
        futures = {
            pool.submit(_monitor_sym, sym, tradeable, box_date): sym
            for sym in tradeable
        }

        for future in as_completed(futures):
            sym_key, result_data = future.result()
            current_ts = int(datetime.now(ZoneInfo(MARKET_TZ)).timestamp())

            if result_data is None:
                log.info("  [%s] sin breakout", sym_key)
                continue

            all_breakouts[sym_key] = {
                "signal": result_data,
                "is_primary": _is_primary(sym_key),
                "features": tradeable[sym_key].features,
            }

            if _is_primary(sym_key):
                primary_breakout_detected = True

            log.info("  [%s] BREAKOUT %s @ %s | primary=%s",
                    sym_key, result_data["breakout_state"],
                    result_data["candle_close"], _is_primary(sym_key))

            # Si primary rompió y cerró, romper loop
            if _is_primary(sym_key) and current_ts >= primary_box_end:
                log.info("  [PRIMARY] cerró box - recopilando breakouts finales")
                break

    # ─── ETAPA 4: Enviar TODOS los breakouts a agentes ────
    log.info("═" * 60)
    log.info("  ETAPA 4 · Enviando %d breakout(s) a agentes expertos", len(all_breakouts))
    log.info("═" * 60)

    if not all_breakouts:
        log.info("[FIN] Sin breakouts detectados.")
        return

    # Si primary no rompió, esperar su cierre antes de decisión final
    current_ts = int(datetime.now(ZoneInfo(MARKET_TZ)).timestamp())
    if not primary_breakout_detected and current_ts < primary_box_end:
        wait_secs = primary_box_end - current_ts
        log.info("  [gate] Primary sin breakout - esperando cierre: %ds", wait_secs)
        time.sleep(min(wait_secs, 300))
    elif current_ts < primary_box_end:
        wait_secs = primary_box_end - current_ts
        log.info("  [gate] Esperando cierre primary: %ds", wait_secs)
        time.sleep(min(wait_secs, 120))

    # ─── Construir symbols_data con TODOS los breakouts ────
    symbols_data = []
    execution_data = {}

    for sym, data in all_breakouts.items():
        result = tradeable[sym]
        features = result.features
        box = features.get("box", {})
        vp = features.get("volume_profile") or {}
        signal = data["signal"]

        bh, bl = box.get("high"), box.get("low")
        box_mid = box.get("mid")

        execution_data[sym] = {
            "box_high": bh,
            "box_low": bl,
            "amplitud": box.get("amplitud"),
            "high_simple": box.get("high_simple"),
            "low_simple": box.get("low_simple"),
        }

        symbols_data.append({
            "symbol": sym,
            "is_primary": data["is_primary"],
            "market_tz": MARKET_TZ,
            "breakout_signal": {
                "state": signal.get("breakout_state"),
                "close": signal.get("candle_close"),
                "time": signal.get("signal_time"),
            },
            "caja": {
                "high": bh,
                "low": bl,
                "mid": box_mid,
                "amp_pct": box.get("amplitud"),
                "hour_range": box.get("hour_range"),
                "candles": box.get("candles", []),
            },
            "vp": {
                "poc": vp.get("poc"),
                "hva": vp.get("vah"),
                "lva": vp.get("val"),
            },
            "rsi": {"last": features.get("rsi_last")},
        })

    inputs = {
        "symbols_data": json.dumps(symbols_data, indent=2, default=str),
        "market": MARKET,
    }

    log.info("  Enviando al crew: %s", [s["symbol"] for s in symbols_data])

    strategy = StrategyAi()
    strategy._execution_data = execution_data
    strategy._breakouts = {sym: data["signal"] for sym, data in all_breakouts.items()}

    try:
        strategy.crew().kickoff(inputs=inputs)
        log.info("[FIN] Crew finalizado.")
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


# ── Entrypoint cuando se ejecuta con `python -m strategy_ai.main` ─────
if __name__ == "__main__":
    run()
