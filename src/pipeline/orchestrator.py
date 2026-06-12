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

from application.agents.agents import build_all_agents
from domain.market_time import box_window_unix
from domain.strategy.budget import DailyOrderBudget
from infrastructure.broker.capital.adapter import CapitalAdapter
from infrastructure.broker.simplefx.adapter import SimpleFXAdapter
from infrastructure.config.settings import get_settings
from infrastructure.persistence.sqlite import db, run_repo, trade_repo
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
from utils.logger import get_logger

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


def run_pipeline() -> RunResult:
    """Ejecuta el pipeline completo. Retorna RunResult."""
    settings = get_settings()
    db.init_db()

    repaired = run_repo.fail_stale_runs()
    if repaired:
        log.warning("Reparados %d run(s) zombi de corridas anteriores", repaired)

    run_id = str(uuid.uuid4())
    started_at = datetime.now(UTC).isoformat()
    errors: list[str] = []
    orders_sent = 0
    decisions_count = 0

    # ── Config snapshot ────────────────────────────────────────────────
    config_snap = {
        "symbols": settings.symbol_list,
        "primary_symbol": settings.primary_symbol,
        "volume": settings.volume,
        "max_orders_per_day": settings.max_orders_per_day,
        "min_rr_ratio": settings.min_rr_ratio,
        "dry_run": settings.dry_run,
        "models": {
            "decision_maker": settings.llm.decision_maker,
            "trader": settings.llm.trader,
            "risk_analyst": settings.llm.risk_analyst,
            "mtfa": settings.llm.mtfa,
            "position_manager": settings.llm.position_manager,
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

        # ── Init adapters y agentes ────────────────────────────────────
        broker = SimpleFXAdapter(settings)
        market_data = CapitalAdapter(settings)
        agents = build_all_agents(settings.llm)

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
                "s7_manage", run_id, _manage_open_positions,
                timeout_s=settings.stage_timeout_s,
            )
            if n_actions:
                log.info("PM: %d acciones sobre trades abiertos", n_actions)
        except StageError as e:
            # Un fallo gestionando posiciones no debe impedir operar el día
            errors.append(str(e))

        # ── STAGES 1-4 por símbolo (paralelo) ──────────────────────────
        log.info("STAGES 1-4 · Ingest + Preprocess + Signal (%d símbolos)", len(settings.symbol_list))
        box_date = settings.box_date or today
        start_iso, end_iso = settings.vp_window()
        log.info("Ventana de datos (UTC): %s → %s | caja %s %s-%s (%s)",
                 start_iso, end_iso, box_date, settings.box_start, settings.box_end, settings.market_tz)
        symbols_data: list[dict[str, Any]] = []

        def process_symbol(sym: str) -> dict[str, Any] | None:
            try:
                # Stage 1
                ingest_out = run_stage(
                    f"s1_ingest:{sym}", run_id, stage_ingest,
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
                    f"s2_preprocess:{sym}", run_id, stage_preprocess,
                    pp_in, ingest_out.df_candles,
                    timeout_s=settings.stage_timeout_s,
                )
                log.info(
                    "[preprocess] %s: box=%.2f-%.2f amp=%.2f%% RSI=%s",
                    sym, pp_out.box.low, pp_out.box.high, pp_out.box.amplitude_pct, pp_out.rsi_last,
                )

                # Stage 4 — regla #6: solo velas de las 2h posteriores a la caja
                _, box_to = box_window_unix(
                    box_date, settings.box_start, settings.box_end, settings.market_tz
                )
                df = ingest_out.df_candles
                post_box = df[
                    (df["time"] > box_to)
                    & (df["time"] <= box_to + BREAKOUT_WINDOW_SECONDS)
                ]
                if post_box.empty:
                    log.info("[signal] %s: sin velas post-caja todavía", sym)
                    return None

                sig_in = SignalInput(
                    symbol=sym,
                    df_candles=post_box,
                    box=pp_out.box,
                    primary=(sym == settings.primary_symbol),
                )
                sig_out = run_stage(
                    f"s4_signal:{sym}", run_id, stage_signal, sig_in,
                    timeout_s=settings.stage_timeout_s,
                )

                if not sig_out.has_breakout:
                    log.info("[signal] %s: sin breakout", sym)
                    return None

                log.info(
                    "[signal] %s: BREAKOUT %s close=%.2f @ %s",
                    sym, sig_out.breakout_state, sig_out.candle_close, sig_out.signal_time,
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
                    log.error("[pipeline] %s: error inesperado fuera de stage: %s",
                              sym, e, exc_info=True)
                    errors.append(f"{sym}: {e}")

        if not symbols_data:
            if errors:
                log.warning("[FIN] Sin señales y con errores: %s", "; ".join(errors))
                run_repo.finish_run(run_id, "failed", "; ".join(errors)[:500])
                final_status = "failed"
            else:
                log.info("[FIN] Sin breakouts detectados")
                run_repo.finish_run(run_id, "success", "no breakouts")
                final_status = "success"
            return RunResult(
                run_id=run_id, started_at=started_at,
                finished_at=datetime.now(UTC).isoformat(),
                status=final_status,
                errors=errors,
            )

        # ── STAGE 3: Context ───────────────────────────────────────────
        log.info("STAGE 3 · Context (macro)")
        try:
            ctx_out = run_stage(
                "s3_context", run_id, stage_context,
                ContextInput(date_str=today, market_tz=settings.market_tz),
                timeout_s=settings.stage_timeout_s,
            )
        except StageError as e:
            # Fail-safe: sin calendario macro asumimos riesgo HIGH para que
            # el risk analyst lo tenga en cuenta (regla #20, conservadora).
            errors.append(str(e))
            ctx_out = ContextOutput(macro_risk="HIGH", high_impact_events=[])
            log.warning("Contexto macro no disponible → macro_risk=HIGH (fail-safe)")
        log.info("Macro risk: %s (%d eventos HIGH)", ctx_out.macro_risk, len(ctx_out.high_impact_events))

        # ── Budget diario ANTES del crew, sembrado con lo ya enviado hoy ─
        used_today = trade_repo.count_orders_today()
        budget = DailyOrderBudget(max_orders=settings.max_orders_per_day, used=used_today)
        if used_today:
            log.info("Budget diario: %d/%d órdenes ya usadas hoy",
                     used_today, settings.max_orders_per_day)
        if budget.remaining == 0:
            log.warning("[FIN] Budget diario agotado (%d órdenes hoy) — no se analiza", used_today)
            run_repo.finish_run(run_id, "success", "daily budget exhausted")
            return RunResult(
                run_id=run_id, started_at=started_at,
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
                    },
                }
            )

        analyze_in = AnalyzeInput(symbols=crew_symbols_data, market=settings.market)
        analyze_out = run_stage(
            "s5_analyze", run_id, stage_analyze, analyze_in, run_id, agents,
            timeout_s=settings.analyze_timeout_s,
        )
        decisions_count = len(analyze_out.decisions)
        log.info("Crew produjo %d decisiones", decisions_count)

        # ── STAGE 6: Execute ───────────────────────────────────────────
        log.info("STAGE 6 · Execute")
        trade_date = datetime.now(UTC).date().isoformat()
        for decision in analyze_out.decisions:
            # encontrar el Box del símbolo
            sym_data = next((s for s in symbols_data if s["symbol"] == decision.symbol), None)
            if sym_data is None:
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
                    f"s6_execute:{decision.symbol}", run_id, stage_execute,
                    exec_in, run_id, budget, broker,
                    timeout_s=settings.stage_timeout_s,
                )
            except StageError as e:
                # una decisión fallida no aborta las siguientes
                errors.append(str(e))
                continue
            orders_sent += len(exec_out.orders)
            if exec_out.errors:
                errors.extend(exec_out.errors)
            if exec_out.skipped:
                log.info("[execute] %s: %d orden(es) saltadas por idempotencia",
                         decision.symbol, len(exec_out.skipped))

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
        run_repo.finish_run(run_id, status, "; ".join(errors)[:500] if errors else None)
        log.info("═" * 60)
        log.info("RUN %s FINALIZADO — status=%s, decisions=%d, orders=%d", run_id, status, decisions_count, orders_sent)
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
