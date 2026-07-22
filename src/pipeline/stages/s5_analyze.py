"""
Stage 5: Analyze — corre el análisis con CrewAI, concurrente y aislado por símbolo.

Arquitectura (concurrencia segura, sin volver todo secuencial):

    US500: Trader_US500 -> Risk_US500 ─┐
                                        ├─► Desk_Manager (consolidación
    US100: Trader_US100 -> Risk_US100 ─┘     determinista) ─► AnalyzeOutput

- Cada símbolo corre en su PROPIO thread, con su PROPIA instancia de Trader,
  Risk, Tasks y Crew. Nunca se comparte un Agent entre ramas concurrentes
  → desaparece "Executor is already running ... concurrently".
- Dentro de cada símbolo el proceso es SEQUENTIAL (Trader → Risk). No hay
  manager hierarchical ni delegación → desaparece "coworker mentioned not
  found".
- El Risk recibe el resultado COMPLETO del Trader (vía context de la task),
  no solo APPROVE/RR.
- La consolidación final es DETERMINISTA (Desk_Manager): mapea risk_decision
  a la acción final con la regla de seguridad "ante la duda, NO_OPERAR". No
  delega y produce JSON estricto vía Pydantic.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from crewai import Crew, Process, Task
from pydantic import BaseModel, ValidationError

from application.agents.agents import build_risk_agent, build_trader_agent
from application.agents.tools import (
    DrawdownGuardTool,
    analyze_multi_timeframe,
)
from domain.signals.confluence import compute_confluence_score
from domain.signals.mtf import mtf_alignment
from domain.strategy.decision import Action, RiskMode
from domain.symbols import is_allowed
from infrastructure.config.settings import get_settings
from infrastructure.persistence.sqlite import event_repo
from pipeline.contracts import (
    AnalyzeInput,
    AnalyzeOutput,
    DecisionContract,
    RiskAssessment,
    SymbolCrewData,
    SymbolResult,
    TraderAssessment,
)
from utils.logger import bind_log_context, get_logger

log = get_logger(__name__)

# Umbral conservador por defecto; el perfil ACTIVE lo baja mediante settings.
MIN_CONFLUENCE = 60


def stage_analyze(input_data: AnalyzeInput, run_id: str) -> AnalyzeOutput:
    """Corre una rama Trader→Risk por símbolo en paralelo y consolida."""
    settings = get_settings()

    # Filtro defensivo: aunque el orquestador ya filtra, nunca dejamos pasar
    # un símbolo fuera del whitelist hasta el crew.
    active: list[SymbolCrewData] = [s for s in input_data.symbols if is_allowed(s.symbol)]
    ignored = [s.symbol for s in input_data.symbols if not is_allowed(s.symbol)]
    if ignored:
        log.warning("[analyze] símbolos ignorados (no permitidos): %s", ignored)
        event_repo.log_event(
            run_id=run_id,
            agent="Desk_Manager",
            event_type="SYSTEM",
            payload={"event": "symbols_ignored", "ignored": ignored},
        )

    symbol_names = [s.symbol for s in active]
    log.info("[analyze] símbolos activos: %s", symbol_names)
    event_repo.log_event(
        run_id=run_id,
        agent="Desk_Manager",
        event_type="SYSTEM",
        payload={"stage": "analyze", "symbols": symbol_names},
    )

    if not active:
        # Nada que analizar tras filtrar → NO_OPERAR para lo que vino.
        return _all_no_operar(input_data.symbols, "sin símbolos permitidos")

    # ── Ramas concurrentes: una por símbolo, agentes/crew aislados ─────
    results: list[SymbolResult] = []
    with ThreadPoolExecutor(max_workers=len(active)) as pool:
        futures = {pool.submit(_analyze_symbol_branch, sd, settings, run_id): sd for sd in active}
        for fut in as_completed(futures):
            sd = futures[fut]
            try:
                results.append(fut.result())
            except Exception as e:  # noqa: BLE001 — barrera de la rama
                log.error("[analyze] rama %s falló: %s", sd.symbol, e, exc_info=True)
                event_repo.log_event(
                    run_id=run_id,
                    agent=f"Trader_{sd.symbol}",
                    event_type="SYSTEM",
                    payload={"event": "branch_error", "error": str(e)[:300]},
                )
                # Seguridad: una rama caída → NEED_DATA → NO_OPERAR.
                results.append(_fallback_symbol_result(sd, f"rama falló: {e}"))

    # ── Consolidación determinista (Desk_Manager, sin delegación) ──────
    output = consolidate(
        results,
        run_id,
        min_confluence=settings.effective_min_confluence,
        full_risk_confluence=settings.full_risk_confluence,
    )

    event_repo.log_event(
        run_id=run_id,
        agent="Desk_Manager",
        event_type="DECISION",
        payload={"n_decisions": len(output.decisions)},
    )
    return output


# ── Rama por símbolo ──────────────────────────────────────────────────
def _analyze_symbol_branch(sd: SymbolCrewData, settings: Any, run_id: str) -> SymbolResult:
    """Trader→Risk para UN símbolo, en su propio crew aislado."""
    symbol = sd.symbol
    # Hilo del pool por símbolo: siembra el contexto de logs (run_id + símbolo).
    bind_log_context(run_id=run_id, symbol=symbol)
    log.info("[analyze:%s] iniciando rama Trader→Risk", symbol)

    # El Trader NO usa tools: las señales técnicas (confluence, MTF) se calculan
    # deterministas y se le inyectan. El Risk sí usa drawdown_guard.
    risk_tools = [DrawdownGuardTool()]
    trader = build_trader_agent(symbol, settings.llm, [])
    risk = build_risk_agent(symbol, settings.llm, risk_tools)

    sd_json = json.dumps(sd.model_dump(), default=str, ensure_ascii=False)
    strategy_levels = _strategy_levels(sd)
    strategy_levels_json = json.dumps(strategy_levels, ensure_ascii=False)

    # ── Señales técnicas deterministas (una sola descarga de MTF) ──────────
    state = (sd.breakout_signal.state or "").upper()
    candidate = "LONG" if state == "ABOVE" else "SHORT" if state == "BELOW" else "LONG"
    mtf = analyze_multi_timeframe(symbol, candidate)
    pre_conf = _confluence_from_sd(sd, candidate, settings, mtf)

    trader_task = Task(
        description=(
            f"Analiza SOLO el símbolo {symbol}. Datos de mercado (caja, breakout, "
            f"RSI, volume profile VAH/VAL/POC, macro):\n{sd_json}\n\n"
            f"Señales técnicas YA CALCULADAS (deterministas — úsalas como base):\n"
            f"- confluence_score={pre_conf['score']} "
            f"(factores: {pre_conf['factors']})\n"
            f"- mtf_alignment={mtf.get('mtf_alignment')} "
            f"(sesgos por timeframe: {mtf.get('tf_biases')})\n"
            f"- breakout={state} → dirección implícita {candidate}\n\n"
            f"Decide proposed_direction (LONG/SHORT/NO_OPERAR). Si el "
            f"confluence_score < {settings.effective_min_confluence} o el contexto "
            f"macro lo desaconseja, propon NO_OPERAR.\n"
            f"Devuelve JSON con: symbol, proposed_direction, confidence (0-100), "
            f"reasons (lista de razones de tu decisión)."
        ),
        agent=trader,
        expected_output="JSON TraderAssessment para el símbolo",
        output_pydantic=TraderAssessment,
    )

    risk_task = Task(
        description=(
            f"Valida la propuesta del Trader para {symbol}. Tienes el resultado "
            f"COMPLETO del trader en el contexto (dirección, confluence_score, RSI, "
            f"VAH/VAL/POC, breakout, macro, mtf).\n\n"
            f"Niveles explícitos de la estrategia calculados desde la caja:\n"
            f"{strategy_levels_json}\n"
            f"Para LONG usa long.entry, long.stop_loss y long.take_profit. "
            f"Para SHORT usa short.entry, short.stop_loss y short.take_profit. "
            f"No marques falta de stop_loss/take_profit: ya están en strategy_levels.\n\n"
            f"Calcula R:R con "
            f"min_rr={settings.min_rr_ratio} y usa drawdown_guard con "
            f"max_daily_loss={settings.max_daily_loss}.\n\n"
            f"Emite risk_decision EXACTO de: APPROVE_TRADE, APPROVE_NO_TRADE, "
            f"MODIFY, NEED_DATA, VETO. Reglas:\n"
            f"- trader propone NO_OPERAR → APPROVE_NO_TRADE\n"
            f"- faltan datos críticos → NEED_DATA\n"
            f"- macro_risk HIGH dentro del blackout / drawdown excedido / "
            f"R:R < min / dirección contra breakout → VETO\n"
            f"- macro MEDIUM, provider DEGRADED, MTF contrario o primary_confirmed=false "
            f"→ MODIFY (medio tamaño), no VETO\n"
            f"- trader propone LONG/SHORT y todo OK → APPROVE_TRADE\n"
            f"- operar pero con ajuste de niveles → MODIFY (incluye suggested_stop_loss/"
            f"suggested_take_profit).\n"
            f"Devuelve JSON con: symbol, risk_decision, rr_ratio, reasons (lista), "
            f"suggested_stop_loss, suggested_take_profit."
        ),
        agent=risk,
        expected_output="JSON RiskAssessment para el símbolo",
        context=[trader_task],  # el Risk recibe TODO el output del trader
        output_pydantic=RiskAssessment,
    )

    crew = Crew(
        agents=[trader, risk],
        tasks=[trader_task, risk_task],
        process=Process.sequential,  # nada de hierarchical/delegación
        verbose=True,
    )
    crew.kickoff()

    trader_res = _parse_task_output(trader_task, TraderAssessment)
    risk_res = _parse_task_output(risk_task, RiskAssessment)

    if trader_res is None:
        log.error("[analyze:%s] Trader no produjo JSON parseable", symbol)
        return _fallback_symbol_result(sd, "trader sin JSON válido")

    # ── Integridad: TODOS los campos que S6 usa provienen de la fuente
    #    determinista (stage 2/4 + dominio), NO del echo del LLM. El LLM solo
    #    aporta proposed_direction, confidence y reasons. ───────────────────
    trader_res.symbol = symbol
    trader_res.breakout_state = sd.breakout_signal.state  # ← S6 valida dirección con esto
    trader_res.rsi = sd.rsi.get("last") if isinstance(sd.rsi, dict) else None
    if isinstance(sd.vp, dict):
        trader_res.vah = sd.vp.get("vah")
        trader_res.val = sd.vp.get("val")
        trader_res.poc = sd.vp.get("poc")
    if isinstance(sd.macro, dict):
        trader_res.macro_risk = sd.macro.get("risk")
        trader_res.macro_provider_status = sd.macro.get("provider_status")
    if trader_res.proposed_direction in ("LONG", "SHORT"):
        # Alineación MTF y confluence para la dirección propuesta, reutilizando
        # los sesgos ya descargados (sin segunda llamada a Capital).
        biases = mtf.get("tf_biases") or {}
        trader_res.mtf_alignment = (
            mtf_alignment(biases, trader_res.proposed_direction)
            if biases
            else mtf.get("mtf_alignment")
        )
        trader_res.confluence_score = _authoritative_confluence(
            sd, trader_res.proposed_direction, settings, mtf=mtf
        )
    else:
        trader_res.mtf_alignment = mtf.get("mtf_alignment")

    if risk_res is None:
        log.error("[analyze:%s] Risk no produjo JSON parseable → NEED_DATA", symbol)
        risk_res = RiskAssessment(
            symbol=symbol,
            risk_decision="NEED_DATA",
            reasons=["risk sin JSON válido → NO_OPERAR por seguridad"],
        )
    risk_res.symbol = symbol

    log.info(
        "[analyze:%s] trader=%s score=%d | risk=%s rr=%s",
        symbol,
        trader_res.proposed_direction,
        trader_res.confluence_score,
        risk_res.risk_decision,
        risk_res.rr_ratio,
    )
    event_repo.log_event(
        run_id=run_id,
        agent=f"Risk_{symbol}",
        event_type="DECISION",
        payload={
            "proposed_direction": trader_res.proposed_direction,
            "confluence_score": trader_res.confluence_score,
            "risk_decision": risk_res.risk_decision,
            "rr_ratio": risk_res.rr_ratio,
        },
    )
    return SymbolResult(symbol=symbol, trader=trader_res, risk=risk_res)


# ── Consolidación determinista (Desk_Manager) ─────────────────────────
def consolidate(
    results: list[SymbolResult],
    run_id: str,
    *,
    min_confluence: int = MIN_CONFLUENCE,
    full_risk_confluence: int = 70,
) -> AnalyzeOutput:
    """Combina trader+risk de cada símbolo en decisiones finales.

    Determinista y conservadora: ante cualquier duda, NO_OPERAR.
    """
    decisions: list[DecisionContract] = []
    for res in results:
        decisions.append(_to_decision(res, min_confluence, full_risk_confluence))
    return AnalyzeOutput(decisions=decisions)


def map_final_action(
    proposed_direction: str,
    confluence_score: int,
    risk_decision: str,
    min_confluence: int = MIN_CONFLUENCE,
) -> Action:
    """Mapeo seguro risk_decision → acción final. Fuente de verdad del bot.

    Prioridad #1: seguridad. Solo se opera con APPROVE_TRADE/MODIFY, dirección
    válida y confluencia suficiente. Todo lo demás → NO_OPERAR.
    """
    if proposed_direction == "NO_OPERAR":
        return Action.NO_OPERAR
    if confluence_score < min_confluence:
        return Action.NO_OPERAR
    if risk_decision in ("VETO", "NEED_DATA", "APPROVE_NO_TRADE"):
        return Action.NO_OPERAR
    if risk_decision in ("APPROVE_TRADE", "MODIFY"):
        return Action.LONG if proposed_direction == "LONG" else Action.SHORT
    # risk_decision desconocido → seguridad.
    return Action.NO_OPERAR


def _to_decision(
    res: SymbolResult, min_confluence: int = MIN_CONFLUENCE, full_risk_confluence: int = 70
) -> DecisionContract:
    t, r = res.trader, res.risk
    action = map_final_action(
        t.proposed_direction, t.confluence_score, r.risk_decision, min_confluence
    )

    # Riesgo MEDIO si hubo ajuste o macro alto; COMPLETO en caso normal.
    risk_mode = (
        RiskMode.MEDIO
        if (
            r.risk_decision == "MODIFY"
            or (t.macro_risk or "").upper() in ("MEDIUM", "HIGH")
            or (t.mtf_alignment or "").upper() in ("MIXED", "COUNTER")
            or (t.macro_provider_status or "").upper() == "DEGRADED"
            or t.confluence_score < full_risk_confluence
        )
        else RiskMode.COMPLETO
    )

    # confidence == confluence determinista (fuente única, no el número del LLM).
    confidence = t.confluence_score
    if action == Action.NO_OPERAR:
        # Confianza alta en la decisión de no operar.
        confidence = max(confidence, 50)

    reasons = [
        f"trader={t.proposed_direction} confluence={t.confluence_score}",
        f"risk={r.risk_decision} rr={r.rr_ratio}",
    ]
    reasons += [f"trader: {x}" for x in t.reasons]
    reasons += [f"risk: {x}" for x in r.reasons]
    if not reasons:
        reasons = ["sin razones explícitas"]

    # key_levels/signal preservan TODO el contexto del trader (no se pierde nada).
    key_levels = {
        "vah": t.vah,
        "val": t.val,
        "poc": t.poc,
        "suggested_stop_loss": r.suggested_stop_loss,
        "suggested_take_profit": r.suggested_take_profit,
    }
    signal = {
        "proposed_direction": t.proposed_direction,
        "confluence_score": t.confluence_score,
        "rsi": t.rsi,
        "breakout_state": t.breakout_state,
        "macro_risk": t.macro_risk,
        "macro_provider_status": t.macro_provider_status,
        "mtf_alignment": t.mtf_alignment,
        "risk_decision": r.risk_decision,
        "rr_ratio": r.rr_ratio,
    }

    return DecisionContract(
        symbol=res.symbol,
        action=action,
        risk=risk_mode,
        confidence=confidence,
        reasons=reasons,
        key_levels=key_levels,
        signal=signal,
        team_consensus=f"{t.proposed_direction}/{r.risk_decision}→{action.value}",
    )


# ── Helpers ───────────────────────────────────────────────────────────
def _confluence_from_sd(
    sd: SymbolCrewData, direction: str, settings: Any, mtf: dict | None = None
) -> dict:
    """Confluence determinista (dict con score/recommendation/factors) desde sd.

    Deriva los inputs de sd (RSI, POC vs mid de la caja, breakout) + el MTF real.
    ``mtf`` se puede pasar ya descargado para NO volver a llamar a Capital; la
    alineación se recalcula desde sus sesgos para la ``direction`` pedida.
    """
    rsi_value = sd.rsi.get("last") if isinstance(sd.rsi, dict) else None
    poc = sd.vp.get("poc") if isinstance(sd.vp, dict) else None
    # Sin POC no se puede verificar la alineación → tratar como NO alineado.
    poc_above_mid = (direction != "LONG") if poc is None else (poc > sd.caja.mid)

    state = (sd.breakout_signal.state or "").upper()
    breakout_aligned = (state == "ABOVE" and direction == "LONG") or (
        state == "BELOW" and direction == "SHORT"
    )

    if mtf is None:
        mtf = analyze_multi_timeframe(sd.symbol, direction)
    biases = mtf.get("tf_biases") or {}
    alignment = mtf_alignment(biases, direction) if biases else mtf.get("mtf_alignment", "MIXED")
    mtf_aligned = alignment == "ALIGNED"

    return compute_confluence_score(
        direction=direction,
        rsi_value=rsi_value,
        poc_above_mid=poc_above_mid,
        breakout_aligned=breakout_aligned,
        mtf_aligned=mtf_aligned,
        proceed_threshold=settings.effective_min_confluence,
    )


def _authoritative_confluence(
    sd: SymbolCrewData, direction: str, settings: Any, mtf: dict | None = None
) -> int:
    """Score de confluencia autoritativo (int) para la dirección dada."""
    return int(_confluence_from_sd(sd, direction, settings, mtf)["score"])


def _parse_task_output(task: Task, model: type[BaseModel]) -> BaseModel | None:
    """Extrae el modelo Pydantic del output de una task (pydantic o raw JSON)."""
    out = getattr(task, "output", None)
    if out is None:
        return None
    pyd = getattr(out, "pydantic", None)
    if isinstance(pyd, model):
        return pyd
    raw = getattr(out, "raw", None) or str(out)
    return _parse_json_into(raw, model)


def _parse_json_into(raw: str, model: type[BaseModel]) -> BaseModel | None:
    candidates: list[str] = []
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    start, end = raw.find("{"), raw.rfind("}") + 1
    if start >= 0 and end > start:
        candidates.append(raw[start:end])
    for cand in candidates:
        try:
            return model.model_validate(json.loads(cand))
        except (json.JSONDecodeError, ValidationError) as e:
            log.warning("JSON del crew no cumple %s: %s", model.__name__, e)
    return None


def _strategy_levels(sd: SymbolCrewData) -> dict[str, Any]:
    """Niveles deterministas que Stage 6 usará para entry/SL/TP."""
    high = float(sd.caja.high)
    low = float(sd.caja.low)
    box_range = high - low

    long_entry = round(high, 1)
    long_stop = round(low, 1)
    long_tp = round(high + box_range, 1)
    short_entry = round(low, 1)
    short_stop = round(high, 1)
    short_tp = round(low - box_range, 1)

    return {
        "box": {
            "high": high,
            "low": low,
            "range": round(box_range, 1),
            "amp_pct": sd.caja.amp_pct,
        },
        "long": {
            "entry": long_entry,
            "stop_loss": long_stop,
            "take_profit": long_tp,
            "rr_ratio": _rr(long_entry, long_stop, long_tp),
        },
        "short": {
            "entry": short_entry,
            "stop_loss": short_stop,
            "take_profit": short_tp,
            "rr_ratio": _rr(short_entry, short_stop, short_tp),
        },
    }


def _rr(entry: float, stop_loss: float, take_profit: float) -> float:
    risk = abs(entry - stop_loss)
    if risk <= 0:
        return 0.0
    return round(abs(take_profit - entry) / risk, 2)


def _fallback_symbol_result(sd: SymbolCrewData, reason: str) -> SymbolResult:
    """Resultado seguro cuando una rama falla: NEED_DATA → NO_OPERAR."""
    return SymbolResult(
        symbol=sd.symbol,
        trader=TraderAssessment(
            symbol=sd.symbol,
            proposed_direction="NO_OPERAR",
            confluence_score=0,
            reasons=[reason],
        ),
        risk=RiskAssessment(
            symbol=sd.symbol,
            risk_decision="NEED_DATA",
            reasons=[reason],
        ),
    )


def _all_no_operar(symbols: list[SymbolCrewData], reason: str) -> AnalyzeOutput:
    return AnalyzeOutput(
        decisions=[
            DecisionContract(
                symbol=s.symbol,
                action=Action.NO_OPERAR,
                risk=RiskMode.MEDIO,
                confidence=50,
                reasons=[reason],
                key_levels={},
                signal={},
                team_consensus="no_op",
            )
            for s in symbols
        ]
    )
