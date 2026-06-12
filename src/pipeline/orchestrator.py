"""
Pipeline orchestrator: ejecuta el flujo completo end-to-end.

Flujo:
1. Init DB
2. Crea run_id
3. Position Manager (gestiona trades abiertos)
4. Por cada símbolo: ingest → preprocess → signal
5. Context (macro)
6. Analyze (crew)
7. Por cada decisión: execute
8. Equity snapshot
9. Marca run como success/failed
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Any

from application.agents.agents import build_all_agents
from domain.strategy.budget import DailyOrderBudget
from infrastructure.broker.capital.adapter import CapitalAdapter
from infrastructure.broker.simplefx.adapter import SimpleFXAdapter
from infrastructure.config.settings import get_settings
from infrastructure.persistence.sqlite import db, run_repo, trade_repo
from infrastructure.persistence.sqlite.equity_repo import insert_snapshot
from pipeline.contracts import (
    AnalyzeInput,
    ContextInput,
    ExecuteInput,
    IngestInput,
    ManageInput,
    PreprocessInput,
    RunResult,
    SignalInput,
)
from pipeline.stages.s1_ingest import stage_ingest
from pipeline.stages.s2_preprocess import stage_preprocess
from pipeline.stages.s3_context import stage_context
from pipeline.stages.s4_signal import stage_signal
from pipeline.stages.s5_analyze import stage_analyze
from pipeline.stages.s6_execute import stage_execute
from pipeline.stages.s7_manage import stage_manage
from tools_bot.time_now import box_window_unix
from utils.logger import get_logger

log = get_logger(__name__)

# Regla #6: el breakout se busca solo en las 2h posteriores al cierre de la caja
BREAKOUT_WINDOW_SECONDS = 2 * 3600


def _is_weekend(date_str: str) -> bool:
    from datetime import datetime as dt
    try:
        d = dt.strptime(date_str[:10], "%Y-%m-%d")
        return d.weekday() >= 5
    except Exception:
        return False


def _today_str(tz: str = "America/New_York") -> str:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")


def run_pipeline() -> RunResult:
    """Ejecuta el pipeline completo. Retorna RunResult."""
    settings = get_settings()
    db.init_db()

    run_id = str(uuid.uuid4())
    started_at = datetime.now(UTC).isoformat()
    errors: list[str] = []
    orders_sent = 0
    decisions_count = 0
    status = "running"

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

        # ── Init adapters y agentes ────────────────────────────────────
        broker = SimpleFXAdapter(settings)
        market_data = CapitalAdapter(settings)
        agents = build_all_agents(settings.llm)

        # ── STAGE 7: Manage open positions (siempre primero) ───────────
        log.info("STAGE 7 · Position Manager (open trades)")
        open_trades_dicts = [
            {**t.to_dict(), "id": t.id, "side": t.side, "is_runner": t.is_runner}
            for t in trade_repo.list_open_trades()
        ]
        if open_trades_dicts:
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
            log.info("PM: %d acciones sobre trades abiertos", len(manage_out.actions))
        else:
            log.info("PM: sin trades abiertos")

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
                ingest_out = stage_ingest(
                    IngestInput(
                        symbol=sym,
                        start_iso=start_iso,
                        end_iso=end_iso,
                        timeframe=settings.timeframe,
                    )
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
                pp_out = stage_preprocess(pp_in, ingest_out.df_candles)
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
                sig_out = stage_signal(sig_in)

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
            except Exception as e:
                log.error("[pipeline] %s: ERROR → %s", sym, e, exc_info=True)
                errors.append(f"{sym}: {e}")
                return None

        with ThreadPoolExecutor(max_workers=len(settings.symbol_list)) as pool:
            futures = {pool.submit(process_symbol, s): s for s in settings.symbol_list}
            for fut in as_completed(futures):
                result = futures[fut]
                try:
                    data = fut.result()
                    if data is not None:
                        symbols_data.append(data)
                except Exception as e:
                    log.error("[pipeline] %s: %s", result, e)
                    errors.append(f"{result}: {e}")

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
        ctx_out = stage_context(ContextInput(date_str=today, market_tz=settings.market_tz))
        log.info("Macro risk: %s (%d eventos HIGH)", ctx_out.macro_risk, len(ctx_out.high_impact_events))

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
        analyze_out = stage_analyze(analyze_in, run_id, agents)
        decisions_count = len(analyze_out.decisions)
        log.info("Crew produjo %d decisiones", decisions_count)

        # ── STAGE 6: Execute ───────────────────────────────────────────
        log.info("STAGE 6 · Execute")
        budget = DailyOrderBudget(max_orders=settings.max_orders_per_day)
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
            )
            exec_out = stage_execute(exec_in, run_id, budget, broker)
            orders_sent += len(exec_out.orders)
            if exec_out.errors:
                errors.extend(exec_out.errors)

        # ── Equity snapshot ────────────────────────────────────────────
        try:
            insert_snapshot(
                balance=0.0,  # no tenemos endpoint para esto; placeholder
                equity=0.0,
                open_positions=len(trade_repo.list_open_trades()),
                source="computed",
                run_id=run_id,
            )
        except Exception as e:
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

    except Exception as e:
        log.critical("Error en run: %s", e, exc_info=True)
        run_repo.finish_run(run_id, "failed", str(e))
        return RunResult(
            run_id=run_id,
            started_at=started_at,
            finished_at=datetime.now(UTC).isoformat(),
            status="failed",
            errors=[str(e)],
        )
