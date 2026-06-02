import os
import re
import json
from enum import Enum
from crewai import Agent, Crew, Process, Task
from crewai.project import CrewBase, agent, crew, task, before_kickoff, after_kickoff
from crewai.agents.agent_builder.base_agent import BaseAgent
from typing import List, Optional
from pydantic import BaseModel, Field
from broker_api.login import sesion_simple
from broker_api.make_order import orden_pending
from utils.logger import get_logger
from dotenv import load_dotenv
from strategy_ai.tools.scraper_tools import ScrapeMacroCalendarTool, SearchNewsTool
from strategy_ai.tools.team_tools import SummarizeContextTool, AnalyzeBoxTool

load_dotenv()
log = get_logger(__name__)

# ── Config desde .env ─────────────────────────────────────────────────
SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", "US500").split(",")]
VOLUME_BASE = float(os.getenv("VOLUME", "1.0"))
SIMPLE_ACCOUNT = os.getenv("SIMPLE_ACCOUNT", "")
SIMPLE_REALITY = os.getenv("SIMPLE_REALITY", "Demo")
DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() in {"1", "true", "yes", "y"}
MAX_ORDERS_PER_DAY = int(os.getenv("MAX_ORDERS_PER_DAY", "4"))
MIN_RR_RATIO = float(os.getenv("MIN_RR_RATIO", "1.5"))
MIN_CONFIDENCE = int(os.getenv("MIN_CONFIDENCE", "0"))


def _extract_json_object(raw: str) -> dict | None:
    """Extrae el primer objeto JSON válido de un texto (tolera fences markdown)."""
    if not raw:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL)
    candidates = []
    if fence:
        candidates.append(fence.group(1))
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start >= 0 and end > start:
        candidates.append(raw[start:end])
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    return None


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
    box_high: Optional[float] = Field(None, description="Parte superior de la caja")
    box_low: Optional[float]  = Field(None, description="Parte inferior de la caja")
    box_mid: Optional[float]  = Field(None, description="Punto medio de la caja")
    poc: Optional[float] = Field(None, description="Point of Control del Volume Profile")
    hva: Optional[float] = Field(None, description="High Value Area del VP")
    lva: Optional[float] = Field(None, description="Low Value Area del VP")


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
    """3-agent crew with real debate via inter-agent tools."""
    agents: List[BaseAgent]
    tasks: List[Task]

    @before_kickoff
    def carga_data(self, inputs):
        if "symbols_data" not in inputs or not inputs["symbols_data"]:
            raise ValueError("[before_kickoff] symbols_data requerido.")
        data = json.loads(inputs["symbols_data"])
        inputs["market"] = inputs.get("market", "S&P 500 / Forex")
        log.info("[before_kickoff] %d símbolo(s): %s", len(data), [s['symbol'] for s in data])
        return inputs

    @after_kickoff
    def ejecutar_ordenes(self, results):
        decision_output = None

        if hasattr(results, "pydantic") and results.pydantic is not None:
            decision_output = results.pydantic
        else:
            raw = results.raw if hasattr(results, "raw") else str(results)
            parsed = _extract_json_object(raw)
            if parsed is not None:
                try:
                    decision_output = CrewDecisionOutput.model_validate(parsed)
                except Exception as e:
                    log.error("[after_kickoff] JSON parseado pero inválido contra schema: %s", e)

        if decision_output is None:
            log.warning("[after_kickoff] No se obtuvo decisión válida — no se ejecutan órdenes")
            return results

        exec_data = getattr(self, "_execution_data", {})
        breakouts = getattr(self, "_breakouts", {})

        if DRY_RUN:
            log.warning("[after_kickoff] DRY_RUN=true → las órdenes NO se enviarán al broker")

        token: str | None = None
        orders_sent = 0

        for decision in decision_output.decisions:
            symbol = decision.symbol
            action = decision.action.value
            risk = decision.risk.value

            if action == "NO_OPERAR":
                log.info("[order] %s: NO_OPERAR (confianza=%d%%) → skip | razones: %s",
                         symbol, decision.confidence, decision.reasons)
                continue

            if decision.confidence < MIN_CONFIDENCE:
                log.warning("[order] %s: confianza %d%% < min %d%% → skip",
                            symbol, decision.confidence, MIN_CONFIDENCE)
                continue

            box = exec_data.get(symbol)
            if not box:
                log.warning("[order] %s: sin datos de ejecución, skip", symbol)
                continue

            # ── Consistencia LONG/SHORT vs breakout real ────────────────
            breakout_state = (breakouts.get(symbol) or {}).get("breakout_state")
            if breakout_state == "ABOVE" and action == "SHORT":
                log.warning("[order] %s: IA pide SHORT pero breakout fue ABOVE → veto seguridad",
                            symbol)
                continue
            if breakout_state == "BELOW" and action == "LONG":
                log.warning("[order] %s: IA pide LONG pero breakout fue BELOW → veto seguridad",
                            symbol)
                continue

            # Usar valores de SimpleFX; fallback a Capital
            box_high = box.get("high_simple") or box.get("box_high")
            box_low = box.get("low_simple") or box.get("box_low")

            if box_high is None or box_low is None:
                log.warning("[order] %s: faltan niveles de caja, skip", symbol)
                continue

            amp_points = box_high - box_low
            if amp_points <= 0:
                log.warning("[order] %s: amplitud no positiva (%.4f), skip", symbol, amp_points)
                continue

            vol_half = round(VOLUME_BASE / 2, 2) if risk == "COMPLETO" else round(VOLUME_BASE / 4, 2)
            if vol_half <= 0:
                log.warning("[order] %s: volumen 0, skip", symbol)
                continue

            if action == "LONG":
                side = "BUY"
                entry = round(box_high, 1)
                stop = round(box_low, 1)
                tp = round(box_high + amp_points, 1)
            elif action == "SHORT":
                side = "SELL"
                entry = round(box_low, 1)
                stop = round(box_high, 1)
                tp = round(box_low - amp_points, 1)
            else:
                continue

            # ── Validar Entry / SL / TP coherentes ──────────────────────
            risk_pts = abs(entry - stop)
            reward_pts = abs(tp - entry)
            if risk_pts <= 0:
                log.warning("[order] %s: SL == entry (%.2f), skip", symbol, entry)
                continue
            rr = reward_pts / risk_pts
            if rr + 1e-9 < MIN_RR_RATIO:
                log.warning("[order] %s: R:R=%.2f < min %.2f → skip",
                            symbol, rr, MIN_RR_RATIO)
                continue

            if action == "LONG" and not (stop < entry < tp):
                log.warning("[order] %s: LONG con niveles incoherentes (entry=%.2f SL=%.2f TP=%.2f) → skip",
                            symbol, entry, stop, tp)
                continue
            if action == "SHORT" and not (tp < entry < stop):
                log.warning("[order] %s: SHORT con niveles incoherentes (entry=%.2f SL=%.2f TP=%.2f) → skip",
                            symbol, entry, stop, tp)
                continue

            # Cada símbolo manda 2 órdenes (principal + runner)
            if orders_sent + 2 > MAX_ORDERS_PER_DAY:
                log.warning("[order] %s: alcanzaría MAX_ORDERS_PER_DAY=%d → skip",
                            symbol, MAX_ORDERS_PER_DAY)
                continue

            log.info("[order] %s: %s | Entry=%.2f | SL=%.2f | TP=%.2f | R:R=%.2f | Vol=%.2f | Risk=%s | Conf=%d%%",
                     symbol, action, entry, stop, tp, rr, vol_half * 2, risk, decision.confidence)
            log.info("[order] %s razones: %s", symbol, decision.reasons)

            if DRY_RUN:
                log.info("[order] %s: DRY_RUN → no se envía al broker", symbol)
                orders_sent += 2
                continue

            try:
                if token is None:
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
                orders_sent += 1

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
                orders_sent += 1

            except Exception as e:
                log.error("[order] %s: error enviando orden → %s", symbol, e, exc_info=True)

        log.info("[after_kickoff] órdenes enviadas: %d (dry_run=%s)", orders_sent, DRY_RUN)
        return results

    # ── Agents ──────────────────────────────────────────────────────────
    @agent
    def decision_maker(self) -> Agent:
        return Agent(
            config=self.agents_config['decision_maker'],
            verbose=True,
            tools=[ScrapeMacroCalendarTool, SearchNewsTool, SummarizeContextTool],
        )

    @agent
    def trader(self) -> Agent:
        return Agent(
            config=self.agents_config['trader'],
            verbose=True,
            tools=[ScrapeMacroCalendarTool, SearchNewsTool, AnalyzeBoxTool],
        )

    @agent
    def risk_analyst(self) -> Agent:
        return Agent(
            config=self.agents_config['risk_analyst'],
            verbose=True,
            tools=[ScrapeMacroCalendarTool, SearchNewsTool, AnalyzeBoxTool],
        )

    # ── Tasks ─────────────────────────────────────────────────────────
    @task
    def prepare_macro(self) -> Task:
        return Task(config=self.tasks_config['prepare_macro'])

    @task
    def analyze_pattern(self) -> Task:
        return Task(config=self.tasks_config['analyze_pattern'])

    @task
    def evaluate_risk(self) -> Task:
        return Task(config=self.tasks_config['evaluate_risk'])

    @task
    def debate_resolution(self) -> Task:
        return Task(config=self.tasks_config['debate_resolution'])

    @task
    def final_decision(self) -> Task:
        return Task(config=self.tasks_config['final_decision'], output_pydantic=CrewDecisionOutput)

    @crew
    def crew(self) -> Crew:
        """
        3-agent crew con real debate via inter-agent tools.
        Flow: decision_maker delega → trader propone → risk_analyst evalúa → debate si hay conflicto → decision_maker decide
        """
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.hierarchical,
            manager_agent=self.decision_maker(),
            verbose=True,
            memory=True,
            max_iterations=5,
            max_execution_time=120,
        )
