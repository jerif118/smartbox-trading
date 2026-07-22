"""
Pipeline orchestrator: ejecuta el flujo completo end-to-end.

Flujo:
1. Init DB + reparar runs zombi + reconciliar trades PENDING huérfanos
2. Crea run_id
3. Position Manager (gestiona trades abiertos)
4. Por cada símbolo: ingest → preprocess → signal (paralelo, con timeout)
5. Context (macro)
6. Analyze (crew, timeout largo)
7. Por cada decisión: execute (idempotente)
8. Equity snapshot (P&L computado)
9. Marca run como success/partial/failed — nunca queda en "running"

Robustez:
- Cada stage corre vía pipeline.runner.run_stage: timeout configurable,
  métricas persistidas en stage_metrics y errores traducidos a StageError.
- El finally garantiza que el run se cierra aunque haya SystemExit o
  KeyboardInterrupt; un crash duro (kill -9) lo repara fail_stale_runs()
  en la corrida siguiente.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Any

from domain.market_time import box_window_unix
from domain.strategy.box import MAX_AMPLITUDE_PCT
from domain.strategy.budget import DailyOrderBudget
from domain.strategy.decision import Action, RiskMode
from infrastructure.broker.capital.adapter import CapitalAdapter
from infrastructure.broker.simplefx.adapter import SimpleFXAdapter
from infrastructure.config.settings import get_settings
from infrastructure.persistence.sqlite import db, event_repo, run_repo, trade_repo
from infrastructure.persistence.sqlite.equity_repo import insert_snapshot
from pipeline.contracts import (
    AnalyzeInput,
    ContextInput,
    ContextOutput,
    ExecuteInput,
    IngestInput,
    ManageInput,
    PreprocessInput,
    RunResult,
    SignalInput,
)
from pipeline.errors import StageError
from pipeline.runner import run_stage
from pipeline.stages.s1_ingest import stage_ingest
from pipeline.stages.s2_preprocess import stage_preprocess
from pipeline.stages.s3_context import stage_context
from pipeline.stages.s4_signal import stage_signal
from pipeline.stages.s5_analyze import stage_analyze
from pipeline.stages.s6_execute import reconcile_pending_trades, stage_execute
from pipeline.stages.s7_manage import stage_manage
from utils.logger import bind_log_context, get_logger, log_context, reset_log_context

log = get_logger(__name__)

# Regla #6: el breakout se busca solo en las 2h posteriores al cierre de la caja
BREAKOUT_WINDOW_SECONDS = 2 * 3600


def _is_weekend(date_str: str) -> bool:
    from datetime import datetime as dt

    try:
        d = dt.strptime(date_str[:10], "%Y-%m-%d")
        return d.weekday() >= 5
    except ValueError:
        return False


def _today_str(tz: str = "America/New_York") -> str:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")


def apply_primary_policy(
    symbols_data: list[dict[str, Any]], primary_symbol: str, mode: str
) -> tuple[list[dict[str, Any]], bool]:
    """Aplica la política de confirmación sin esconder señales secundarias.

    ``required`` bloquea; ``size_reduction`` conserva la señal y la marca para
    medio tamaño; ``independent`` solo registra el estado.
    """
    confirmed = any(
        item.get("symbol") == primary_symbol and item.get("is_primary") for item in symbols_data
    )
    enriched = [{**item, "primary_confirmed": confirmed} for item in symbols_data]
    if not confirmed and mode == "required":
        return [], False
    return enriched, confirmed


def _persist_full_errors(run_id: str, errors: list[str]) -> None:
    """Guarda la lista COMPLETA de errores como evento SYSTEM.

    ``runs.error`` se trunca a 500 chars; este evento conserva el detalle
    íntegro para el post-mortem (``smartbox diagnose <run_id>``).
    """
    if not errors:
        return
    try:
        event_repo.log_event(
            run_id=run_id,
            agent="orchestrator",
            event_type="SYSTEM",
            payload={"event": "run_errors", "count": len(errors), "errors": errors},
        )
    except Exception as e:  # noqa: BLE001 — best-effort, no debe tumbar el cierre
        log.warning("No se pudo persistir la lista completa de errores: %s", e)


def run_pipeline() -> RunResult:
    """Ejecuta el pipeline completo. Retorna RunResult."""
    settings = get_settings()
    db.init_db()

    repaired = run_repo.fail_stale_runs()
    if repaired:
        log.warning("Reparados %d run(s) zombi de corridas anteriores", repaired)

    run_id = str(uuid.uuid4())
    started_at = datetime.now(UTC).isoformat()
    _log_token = bind_log_context(run_id=run_id)
    errors: list[str] = []
    orders_sent = 0
    decisions_count = 0

    # ── Config snapshot ────────────────────────────────────────────────
    config_snap = {
        "symbols": settings.symbol_list,
        "primary_symbol": settings.primary_symbol_resolved,
        "volume": settings.volume,
        "max_orders_per_day": settings.max_orders_per_day,
        "min_rr_ratio": settings.min_rr_ratio,
        "dry_run": settings.dry_run,
        "models": {
            "trader": settings.llm.trader,
            "risk_analyst": settings.llm.risk_analyst,
        },
    }
    run_repo.start_run(run_id, config_snap)
    log.info("═" * 60)
    log.info("RUN %s — SmartBox v2", run_id)
    log.info("═" * 60)

    try:
        # ── Weekend check ──────────────────────────────────────────────
        today = _today_str(settings.market_tz)
        if _is_weekend(today):
            log.warning("[skip] %s fin de semana", today)
            run_repo.finish_run(run_id, "skipped", "weekend")
            return RunResult(
                run_id=run_id,
                started_at=started_at,
                finished_at=datetime.now(UTC).isoformat(),
                status="skipped",
            )

        # ── Reconciliación de PENDING huérfanos ────────────────────────
        pending_warnings = reconcile_pending_trades(run_id)
        errors.extend(pending_warnings)

        # ── Init adapters ──────────────────────────────────────────────
        # Los agentes del crew se construyen por símbolo DENTRO de stage_analyze
        # (instancias aisladas por rama, nunca compartidas).
        broker = SimpleFXAdapter(settings)
        market_data = CapitalAdapter(settings)

        # ── STAGE 7: Manage open positions (siempre primero) ───────────
        log.info("STAGE 7 · Position Manager (open trades)")

        def _manage_open_positions() -> int:
            open_trades_dicts = [
                {**t.to_dict(), "id": t.id, "side": t.side, "is_runner": t.is_runner}
                for t in trade_repo.list_open_trades()
            ]
            if not open_trades_dicts:
                log.info("PM: sin trades abiertos")
                return 0
            symbols_open = list({t["symbol"] for t in open_trades_dicts})
            current_prices: dict[str, float] = {}
            for sym in symbols_open:
                price = market_data.get_current_price(sym)
                if price is not None:
                    current_prices[sym] = price
            manage_in = ManageInput(
                open_trades=open_trades_dicts,
                current_prices=current_prices,
            )
            manage_out = stage_manage(manage_in, run_id, broker)
            return len(manage_out.actions)

        try:
            n_actions = run_stage(
                "s7_manage",
                run_id,
                _manage_open_positions,
                timeout_s=settings.stage_timeout_s,
                side_effecting=True,
            )
            if n_actions:
                log.info("PM: %d acciones sobre trades abiertos", n_actions)
        except StageError as e:
            # Un fallo gestionando posiciones no debe impedir operar el día
            errors.append(str(e))

        # ── STAGES 1-4 por símbolo (paralelo) ──────────────────────────
        log.info(
            "STAGES 1-4 · Ingest + Preprocess + Signal (%d símbolos)", len(settings.symbol_list)
        )
        box_date = settings.box_date or today
        start_iso, end_iso = settings.vp_window()
        log.info(
            "Ventana de datos (UTC): %s → %s | caja %s %s-%s (%s)",
            start_iso,
            end_iso,
            box_date,
            settings.box_start,
            settings.box_end,
            settings.market_tz,
        )
        symbols_data: list[dict[str, Any]] = []

        def process_symbol(sym: str) -> dict[str, Any] | None:
            # Hilo del pool por símbolo: siembra el contexto de logs. El pool se
            # cierra al terminar el run, así que no requiere reset explícito.
            bind_log_context(run_id=run_id, symbol=sym)
            try:
                # Stage 1
                ingest_out = run_stage(
                    f"s1_ingest:{sym}",
                    run_id,
                    stage_ingest,
                    IngestInput(
                        symbol=sym,
                        start_iso=start_iso,
                        end_iso=end_iso,
                        timeframe=settings.timeframe,
                    ),
                    timeout_s=settings.stage_timeout_s,
                )
                if ingest_out.n_candles == 0:
                    log.warning("[ingest] %s: 0 velas", sym)
                    errors.append(f"{sym}: ingest devolvió 0 velas")
                    return None

                # Stage 2
                pp_in = PreprocessInput(
                    symbol=sym,
                    start_iso=start_iso,
                    end_iso=end_iso,
                    box_date=box_date,
                    box_start=settings.box_start,
                    box_end=settings.box_end,
                    market_tz=settings.market_tz,
                )
                pp_out = run_stage(
                    f"s2_preprocess:{sym}",
                    run_id,
                    stage_preprocess,
                    pp_in,
                    ingest_out.df_candles,
                    timeout_s=settings.stage_timeout_s,
                )
                log.info(
                    "[preprocess] %s: box=%.2f-%.2f amp=%.2f%% RSI=%s",
                    sym,
                    pp_out.box.low,
                    pp_out.box.high,
                    pp_out.box.amplitude_pct,
                    pp_out.rsi_last,
                )
                if not pp_out.box.is_valid():
                    log.info(
                        "[preprocess] %s: amplitud %.2f%% > %.2f%% -> NO_OPERAR",
                        sym,
                        pp_out.box.amplitude_pct,
                        MAX_AMPLITUDE_PCT,
                    )
                    return None

                # Stage 4 — regla #6: solo velas de las 2h posteriores a la caja
                _, box_to = box_window_unix(
                    box_date, settings.box_start, settings.box_end, settings.market_tz
                )
                df = ingest_out.df_candles
                post_box = df[
                    (df["time"] > box_to) & (df["time"] <= box_to + BREAKOUT_WINDOW_SECONDS)
                ]
                if post_box.empty:
                    log.info("[signal] %s: sin velas post-caja todavía", sym)
                    return None

                sig_in = SignalInput(
                    symbol=sym,
                    df_candles=post_box,
                    box=pp_out.box,
                    primary=(sym == settings.primary_symbol_resolved),
                    max_age_minutes=settings.signal_max_age_minutes,
                )
                sig_out = run_stage(
                    f"s4_signal:{sym}",
                    run_id,
                    stage_signal,
                    sig_in,
                    timeout_s=settings.stage_timeout_s,
                )

                if not sig_out.has_breakout:
                    log.info(
                        "[signal] %s: sin breakout vigente (age=%s min)",
                        sym,
                        sig_out.signal_age_minutes,
                    )
                    return None

                log.info(
                    "[signal] %s: BREAKOUT %s close=%.2f @ %s",
                    sym,
                    sig_out.breakout_state,
                    sig_out.candle_close,
                    sig_out.signal_time,
                )

                return {
                    "symbol": sym,
                    "is_primary": sig_in.primary,
                    "box": pp_out.box,
                    "rsi_last": pp_out.rsi_last,
                    "volume_profile": pp_out.volume_profile,
                    "box_candles": pp_out.box_candles,
                    "breakout_state": sig_out.breakout_state,
                    "candle_close": sig_out.candle_close,
                    "signal_time": sig_out.signal_time,
                    "signal_age_minutes": sig_out.signal_age_minutes,
                }
            except StageError as e:
                # run_stage ya logueó con stack trace y persistió la métrica
                errors.append(str(e))
                return None

        with ThreadPoolExecutor(max_workers=len(settings.symbol_list)) as pool:
            futures = {pool.submit(process_symbol, s): s for s in settings.symbol_list}
            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    data = fut.result()
                    if data is not None:
                        symbols_data.append(data)
                except Exception as e:  # noqa: BLE001 — barrera del thread (bug inesperado)
                    log.error(
                        "[pipeline] %s: error inesperado fuera de stage: %s", sym, e, exc_info=True
                    )
                    errors.append(f"{sym}: {e}")

        if not symbols_data:
            if errors:
                log.warning("[FIN] Sin señales y con errores: %s", "; ".join(errors))
                _persist_full_errors(run_id, errors)
                run_repo.finish_run(run_id, "failed", "; ".join(errors)[:500])
                final_status = "failed"
            else:
                log.info("[FIN] Sin breakouts detectados")
                run_repo.finish_run(run_id, "success", "no breakouts")
                final_status = "success"
            return RunResult(
                run_id=run_id,
                started_at=started_at,
                finished_at=datetime.now(UTC).isoformat(),
                status=final_status,
                errors=errors,
            )

        symbols_data, primary_confirmed = apply_primary_policy(
            symbols_data,
            settings.primary_symbol_resolved,
            settings.primary_confirmation_mode,
        )
        if not symbols_data and settings.primary_confirmation_mode == "required":
            log.info(
                "[FIN] %s sin breakout: confirmación primaria obligatoria",
                settings.primary_symbol_resolved,
            )
            event_repo.log_event(
                run_id=run_id,
                agent="desk_manager",
                event_type="DECISION",
                payload={
                    "action": "NO_OPERAR",
                    "reason": "primary_breakout_required",
                    "primary_symbol": settings.primary_symbol_resolved,
                },
            )
            run_repo.finish_run(run_id, "success", "primary breakout required")
            return RunResult(
                run_id=run_id,
                started_at=started_at,
                finished_at=datetime.now(UTC).isoformat(),
                status="success",
                errors=errors,
            )

        # ── STAGE 3: Context ───────────────────────────────────────────
        log.info("STAGE 3 · Context (macro)")
        try:
            ctx_out = run_stage(
                "s3_context",
                run_id,
                stage_context,
                ContextInput(
                    date_str=box_date,
                    market_tz=settings.market_tz,
                    reference_time=max(
                        (s["signal_time"] for s in symbols_data if s["signal_time"]),
                        default=None,
                    ),
                ),
                timeout_s=settings.stage_timeout_s,
            )
        except StageError as e:
            # Fail-safe: sin calendario macro asumimos riesgo HIGH para que
            # el risk analyst lo tenga en cuenta (regla #20, conservadora).
            errors.append(str(e))
            ctx_out = ContextOutput(macro_risk="HIGH", high_impact_events=[])
            log.warning("Contexto macro no disponible → macro_risk=HIGH (fail-safe)")
        log.info(
            "Macro risk: %s (%d eventos HIGH)", ctx_out.macro_risk, len(ctx_out.high_impact_events)
        )

        # ── Budget diario ANTES del crew, sembrado con lo ya enviado hoy ─
        used_today = trade_repo.count_orders_today()
        budget = DailyOrderBudget(max_orders=settings.max_orders_per_day, used=used_today)
        if used_today:
            log.info(
                "Budget diario: %d/%d órdenes ya usadas hoy",
                used_today,
                settings.max_orders_per_day,
            )
        if budget.remaining == 0:
            log.warning("[FIN] Budget diario agotado (%d órdenes hoy) — no se analiza", used_today)
            run_repo.finish_run(run_id, "success", "daily budget exhausted")
            return RunResult(
                run_id=run_id,
                started_at=started_at,
                finished_at=datetime.now(UTC).isoformat(),
                status="success",
                errors=errors,
            )

        # ── STAGE 5: Analyze (crew) ────────────────────────────────────
        log.info("STAGE 5 · Analyze (crew)")
        # Convertir para el crew (no incluir objetos Box en JSON)
        crew_symbols_data = []
        for sd in symbols_data:
            crew_symbols_data.append(
                {
                    "symbol": sd["symbol"],
                    "is_primary": sd["is_primary"],
                    "primary_confirmed": sd["primary_confirmed"],
                    "market_tz": settings.market_tz,
                    "breakout_signal": {
                        "state": sd["breakout_state"],
                        "close": sd["candle_close"],
                        "time": sd["signal_time"],
                    },
                    "caja": {
                        "high": sd["box"].high,
                        "low": sd["box"].low,
                        "mid": sd["box"].mid,
                        "amp_pct": sd["box"].amplitude_pct,
                    },
                    "vp": sd["volume_profile"] or {},
                    "rsi": {"last": sd["rsi_last"]},
                    "macro": {
                        "risk": ctx_out.macro_risk,
                        "events": ctx_out.high_impact_events[:5],
                        "provider_status": ctx_out.provider_status,
                        "nearest_event_minutes": ctx_out.nearest_event_minutes,
                    },
                }
            )

        analyze_in = AnalyzeInput(symbols=crew_symbols_data, market=settings.market)
        analyze_out = run_stage(
            "s5_analyze",
            run_id,
            stage_analyze,
            analyze_in,
            run_id,
            timeout_s=settings.analyze_timeout_s,
        )
        decisions_count = len(analyze_out.decisions)
        log.info("Crew produjo %d decisiones", decisions_count)

        # ── STAGE 6: Execute ───────────────────────────────────────────
        log.info("STAGE 6 · Execute")
        trade_date = datetime.now(UTC).date().isoformat()
        ordered_decisions = sorted(
            analyze_out.decisions,
            key=lambda d: (
                d.symbol != settings.primary_symbol_resolved,
                -int(d.signal.get("confluence_score", d.confidence)),
            ),
        )
        correlated_setups = trade_repo.count_setups_today()
        for decision in ordered_decisions:
            # encontrar el Box del símbolo
            sym_data = next((s for s in symbols_data if s["symbol"] == decision.symbol), None)
            if sym_data is None:
                continue
            if decision.action != Action.NO_OPERAR:
                if ctx_out.macro_risk == "HIGH":
                    log.info(
                        "[execute] %s vetado por blackout macro (%s min)",
                        decision.symbol,
                        ctx_out.nearest_event_minutes,
                    )
                    event_repo.log_event(
                        run_id=run_id,
                        agent="desk_manager",
                        event_type="DECISION",
                        payload={
                            "symbol": decision.symbol,
                            "action": "NO_OPERAR",
                            "reason": "macro_blackout",
                            "nearest_event_minutes": ctx_out.nearest_event_minutes,
                            "provider_status": ctx_out.provider_status,
                        },
                    )
                    continue
                if ctx_out.macro_risk == "MEDIUM":
                    decision.risk = RiskMode.MEDIO
                    decision.reasons.append("contexto macro MEDIUM: medio tamaño")
                if (
                    not primary_confirmed
                    and decision.symbol != settings.primary_symbol_resolved
                    and settings.primary_confirmation_mode == "size_reduction"
                ):
                    decision.risk = RiskMode.MEDIO
                    decision.reasons.append("sin confirmación del US500: medio tamaño")
                if correlated_setups >= settings.max_correlated_setups:
                    log.info(
                        "[execute] %s omitido por límite de exposición correlacionada",
                        decision.symbol,
                    )
                    event_repo.log_event(
                        run_id=run_id,
                        agent="desk_manager",
                        event_type="DECISION",
                        payload={
                            "symbol": decision.symbol,
                            "action": "NO_OPERAR",
                            "reason": "correlated_exposure_limit",
                            "setups_today": correlated_setups,
                            "max_correlated_setups": settings.max_correlated_setups,
                        },
                    )
                    continue

            exec_in = ExecuteInput(
                decision=decision,
                symbol=decision.symbol,
                box=sym_data["box"],
                base_volume=settings.volume,
                min_rr=settings.min_rr_ratio,
                trade_date=trade_date,
            )
            try:
                exec_out = run_stage(
                    f"s6_execute:{decision.symbol}",
                    run_id,
                    stage_execute,
                    exec_in,
                    run_id,
                    budget,
                    broker,
                    timeout_s=settings.stage_timeout_s,
                    side_effecting=True,
                )
            except StageError as e:
                # una decisión fallida no aborta las siguientes
                errors.append(str(e))
                continue
            orders_sent += len(exec_out.orders)
            if exec_out.orders:
                correlated_setups += 1
            if exec_out.errors:
                log.warning("[execute] %s: %s", decision.symbol, "; ".join(exec_out.errors))
                errors.extend(exec_out.errors)
            if exec_out.skipped:
                log.info(
                    "[execute] %s: %d orden(es) saltadas por idempotencia",
                    decision.symbol,
                    len(exec_out.skipped),
                )

        # ── Equity snapshot (P&L computado desde trades; no hay endpoint
        #    de balance en el broker — source='computed') ────────────────
        try:
            stats = trade_repo.compute_stats()
            total_pnl = float(stats["total_pnl"])
            insert_snapshot(
                balance=total_pnl,
                equity=total_pnl,
                daily_pnl=trade_repo.realized_pnl_today(),
                total_pnl=total_pnl,
                open_positions=len(trade_repo.list_open_trades()),
                source="computed",
                run_id=run_id,
            )
        except Exception as e:  # noqa: BLE001 — best-effort, no tumba el run
            log.warning("No se pudo guardar equity snapshot: %s", e)

        status = "success" if not errors else "partial"
        _persist_full_errors(run_id, errors)
        run_repo.finish_run(run_id, status, "; ".join(errors)[:500] if errors else None)
        log.info("═" * 60)
        log.info(
            "RUN %s FINALIZADO — status=%s, decisions=%d, orders=%d",
            run_id,
            status,
            decisions_count,
            orders_sent,
        )
        log.info("═" * 60)

        return RunResult(
            run_id=run_id,
            started_at=started_at,
            finished_at=datetime.now(UTC).isoformat(),
            status=status,
            decisions_count=decisions_count,
            orders_sent=orders_sent,
            errors=errors,
        )

    except StageError as e:
        log.critical("Stage crítico falló: %s", e)
        _persist_full_errors(run_id, [*errors, str(e)])
        run_repo.finish_run(run_id, "failed", str(e)[:500])
        return RunResult(
            run_id=run_id,
            started_at=started_at,
            finished_at=datetime.now(UTC).isoformat(),
            status="failed",
            errors=[*errors, str(e)],
        )
    except Exception as e:  # noqa: BLE001 — última barrera: bug fuera de stages
        log.critical("Error en run: %s", e, exc_info=True)
        _persist_full_errors(run_id, [*errors, str(e)])
        run_repo.finish_run(run_id, "failed", str(e)[:500])
        return RunResult(
            run_id=run_id,
            started_at=started_at,
            finished_at=datetime.now(UTC).isoformat(),
            status="failed",
            errors=[*errors, str(e)],
        )
    finally:
        # Garantía anti-zombi: cubre SystemExit/KeyboardInterrupt y returns
        # tempranos; un kill -9 lo repara fail_stale_runs() del próximo run.
        try:
            current = run_repo.get_run(run_id)
            if current is not None and current.status == "running":
                run_repo.finish_run(
                    run_id, "failed", "aborted: el proceso terminó sin cerrar el run"
                )
                log.warning("Run %s cerrado como failed en finally (aborted)", run_id)
        except Exception as e:  # noqa: BLE001 — best-effort
            log.error("No se pudo cerrar el run en finally: %s", e)
        reset_log_context(_log_token)
