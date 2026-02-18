import os
import json
from enum import Enum
from crewai import Agent, Crew, Process, Task
from crewai.project import CrewBase, agent, crew, task, before_kickoff, after_kickoff
from typing import List, Optional
from pydantic import BaseModel, Field
from broker_api.login import sesion_simple
from broker_api.make_order import orden_pending
from utils.logger import get_logger
from dotenv import load_dotenv

load_dotenv()
log = get_logger(__name__)

# ── Config desde .env ─────────────────────────────────────────────────
SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", "US500").split(",")]
VOLUME_BASE = float(os.getenv("VOLUME", "1.0"))
SIMPLE_ACCOUNT = os.getenv("SIMPLE_ACCOUNT", "")
SIMPLE_REALITY = os.getenv("SIMPLE_REALITY", "Demo")


# ── Modelos Pydantic para forzar salida estructurada ──────────────────
class ActionType(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NO_OPERAR = "NO_OPERAR"


class RiskType(str, Enum):
    COMPLETO = "COMPLETO"
    MEDIO = "MEDIO"


class BreakoutState(str, Enum):
    ABOVE = "ABOVE"
    BELOW = "BELOW"
    INSIDE = "INSIDE"
    NONE_ = "NONE"


class TimingInfo(BaseModel):
    box_end_time: Optional[str] = Field(None, description="ISO8601 fin de la caja")
    trade_valid_until: Optional[str] = Field(None, description="ISO8601 ventana de validez (2h post caja)")
    signal_time: Optional[str] = Field(None, description="ISO8601 timestamp de la señal de breakout")


class KeyLevels(BaseModel):
    box_high: float = Field(..., description="Parte superior de la caja")
    box_low: float = Field(..., description="Parte inferior de la caja")
    box_mid: float = Field(..., description="Punto medio de la caja")
    poc: Optional[float] = Field(None, description="Point of Control del Volume Profile")
    hva: Optional[float] = Field(None, description="High Value Area del VP")
    lva: Optional[float] = Field(None, description="Low Value Area del VP")
    peaks: List[float] = Field(default_factory=list, description="Picos de volumen relevantes")


class SignalInfo(BaseModel):
    breakout_state: BreakoutState = Field(..., description="Estado del breakout respecto a la caja")
    candle_close: Optional[float] = Field(None, description="Precio de cierre de la vela de señal")


class SymbolDecision(BaseModel):
    """Decisión de trading para un símbolo individual."""
    symbol: str = Field(..., description="Símbolo del instrumento (ej: US500, EURUSD)")
    action: ActionType = Field(..., description="LONG, SHORT o NO_OPERAR")
    risk: RiskType = Field(RiskType.COMPLETO, description="COMPLETO (vol base) o MEDIO (vol/2)")
    confidence: int = Field(..., ge=0, le=100, description="Confianza de 0 a 100")
    reasons: List[str] = Field(..., min_length=1, description="Razones que justifican la decisión")
    timing: TimingInfo = Field(default_factory=TimingInfo)
    key_levels: KeyLevels
    signal: SignalInfo


class CrewDecisionOutput(BaseModel):
    """Salida final del crew: lista de decisiones por símbolo."""
    decisions: List[SymbolDecision] = Field(
        ..., min_length=1,
        description="Una decisión por cada símbolo analizado"
    )


@CrewBase
class StrategyAi():
    """StrategyAi crew"""

    agents_config = 'config/agents.yaml'
    tasks_config = 'config/tasks.yaml'

    # ── BEFORE KICKOFF: recibir datos ya procesados desde main.py ────
    @before_kickoff
    def carga_data(self, inputs):

        if "symbols_data" not in inputs or not inputs["symbols_data"]:
            raise ValueError(
                "[before_kickoff] 'symbols_data' no encontrado en inputs. "
                "Ejecute run() desde main.py para el flujo completo."
            )

        data = json.loads(inputs["symbols_data"])
        inputs["market"] = inputs.get("market", "S&P 500 / Forex")

        log.info("[before_kickoff] %d símbolo(s) con breakout: %s",
                 len(data), [s['symbol'] for s in data])
        return inputs

    # ── AFTER KICKOFF: ejecutar órdenes en SimpleFX ───────────────────
    @after_kickoff
    def ejecutar_ordenes(self, results):
        
        # ── Parsear salida Pydantic ───────────────────────────────────
        decision_output = None

        # Si crewai devolvió el pydantic directamente
        if hasattr(results, "pydantic") and results.pydantic is not None:
            decision_output = results.pydantic
        else:
            # Fallback: parsear JSON del raw output
            raw = results.raw if hasattr(results, "raw") else str(results)
            try:
                start_idx = raw.find("{")
                end_idx = raw.rfind("}") + 1
                if start_idx >= 0 and end_idx > start_idx:
                    parsed = json.loads(raw[start_idx:end_idx])
                    decision_output = CrewDecisionOutput.model_validate(parsed)
            except Exception as e:
                log.error("[after_kickoff] Error parseando decisión: %s", e, exc_info=True)
                return results

        if decision_output is None:
            log.warning("[after_kickoff] No se obtuvo decisión válida")
            return results

        exec_data = getattr(self, "_execution_data", {})

        for decision in decision_output.decisions:
            symbol = decision.symbol
            action = decision.action.value
            risk = decision.risk.value

            if action == "NO_OPERAR":
                log.info("[order] %s: NO_OPERAR (confianza=%d%%) → skip | razones: %s",
                         symbol, decision.confidence, decision.reasons)
                continue

            box = exec_data.get(symbol)
            if not box:
                log.warning("[order] %s: sin datos de ejecución, skip", symbol)
                continue

            # Usar valores de SimpleFX; fallback a Capital
            box_high = box.get("high_simple") or box.get("box_high")
            box_low = box.get("low_simple") or box.get("box_low")

            if box_high is None or box_low is None:
                log.warning("[order] %s: faltan niveles de caja, skip", symbol)
                continue

            amp_points = box_high - box_low
            vol_half = round(VOLUME_BASE / 2, 2) if risk == "COMPLETO" else round(VOLUME_BASE / 4, 2)

            if action == "LONG":
                side = "Buy"
                entry = box_high
                stop = box_low
                tp = round(box_high + amp_points, 2)
            elif action == "SHORT":
                side = "Sell"
                entry = box_low
                stop = box_high
                tp = round(box_low - amp_points, 2)
            else:
                continue

            log.info("[order] %s: %s | Entry=%.2f | SL=%.2f | TP=%.2f | Vol=%.2f | Risk=%s | Conf=%d%%",
                     symbol, action, entry, stop, tp, vol_half * 2, risk, decision.confidence)
            log.info("[order] %s razones: %s", symbol, decision.reasons)

            try:
                token = sesion_simple()

                # ── Orden 1: mitad del volumen CON SL + TP ────────────
                order1 = orden_pending(
                    token=token,
                    account=SIMPLE_ACCOUNT,
                    symbol=symbol,
                    side=side,
                    reality=SIMPLE_REALITY,
                    volumen=vol_half,
                    entry_price=entry,
                    stop_price=stop,
                    takeprofit_price=tp,
                )
                log.info("[order1] %s: %.2f vol | SL=%.2f | TP=%.2f → %s",
                         symbol, vol_half, stop, tp, order1.json())

                # ── Orden 2: mitad del volumen SOLO SL (sin TP) ───────
                order2 = orden_pending(
                    token=token,
                    account=SIMPLE_ACCOUNT,
                    symbol=symbol,
                    side=side,
                    reality=SIMPLE_REALITY,
                    volumen=vol_half,
                    entry_price=entry,
                    stop_price=stop,
                    takeprofit_price=None,
                )
                log.info("[order2] %s: %.2f vol | SL=%.2f | TP=None (runner) → %s",
                         symbol, vol_half, stop, order2.json())

            except Exception as e:
                log.error("[order] %s: error enviando orden → %s", symbol, e, exc_info=True)

        return results

    # ── Agents ────────────────────────────────────────────────────────
    @agent
    def profesional_trader(self) -> Agent:
        return Agent(
            config=self.agents_config['professional_trader'],  # type: ignore[index]
            verbose=True,
        )

    @agent
    def macro_news_watcher(self) -> Agent:
        return Agent(
            config=self.agents_config['macro_news_watcher'],  # type: ignore[index]
            verbose=True,
        )

    # ── Tasks ─────────────────────────────────────────────────────────
    @task
    def preparar_data(self) -> Task:
        return Task(
            config=self.tasks_config['preparar_data'],  # type: ignore[index]
        )

    @task
    def reglas_trader(self) -> Task:
        return Task(
            config=self.tasks_config['reglas_trader'],  # type: ignore[index]
        )

    @task
    def filtros_risk(self) -> Task:
        return Task(
            config=self.tasks_config['filtros_risk'],  # type: ignore[index]
            output_file='',
        )

    @task
    def decision(self) -> Task:
        return Task(
            config=self.tasks_config['decision'],  # type: ignore[index]
            output_pydantic=CrewDecisionOutput,
            output_file='',
        )

    @crew
    def crew(self) -> Crew:
        """Creates the StrategyAi crew"""
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            verbose=True,
        )
