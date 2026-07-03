"""
CLI entrypoint con subcomandos.

Uso:
    python -m interfaces.cli.main run
    python -m interfaces.cli.main run --dry-run
    python -m interfaces.cli.main doctor
    python -m interfaces.cli.main status
    python -m interfaces.cli.main trades
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from infrastructure.config.settings import get_settings, has_openai_key
from infrastructure.llm.provider import LLMHealthChecker
from infrastructure.persistence.sqlite import (
    db,
    equity_repo,
    run_repo,
    trade_repo,
)
from utils.logger import get_logger

log = get_logger(__name__)


def _preflight(settings) -> bool:
    """Chequeos comunes antes de operar. False si algo falla."""
    if not has_openai_key():
        print("✗ OPENAI_API_KEY no configurada o es placeholder", file=sys.stderr)
        print("  Edita tu .env o usa `doctor` para diagnosticar", file=sys.stderr)
        return False
    from utils.env_validator import validate_env

    if not validate_env():
        print("✗ Validación de entorno fallida — revisa los errores arriba", file=sys.stderr)
        return False
    return True


def cmd_run(args: argparse.Namespace) -> int:
    """Ejecuta el pipeline. RUN_MODE/--mode decide once vs monitor."""
    from pipeline.orchestrator import run_pipeline

    settings = get_settings()
    if args.dry_run:
        settings.dry_run = True
    mode = args.mode or settings.run_mode
    if not _preflight(settings):
        return 1

    print(f"Símbolos activos (filtrados): {settings.symbol_list}")

    if mode == "monitor":
        from pipeline.monitor import WindowConfig, run_monitor

        window = WindowConfig.from_settings(settings)
        print(
            f"RUN_MODE=monitor | ventana {settings.operate_start}-{settings.operate_end} "
            f"({settings.market_tz}) cada {settings.monitor_interval_s}s. Ctrl+C para salir."
        )
        try:
            n = run_monitor(run_pipeline, window)
        except KeyboardInterrupt:
            print("\nMonitor detenido por el usuario.")
            return 0
        print(f"Monitor finalizado tras {n} corrida(s).")
        return 0

    # mode == "once"
    print(f"RUN_MODE=once (DRY_RUN={settings.dry_run})")
    result = run_pipeline()
    print(json.dumps(result.model_dump(), indent=2, default=str))
    return 0 if result.status in ("success", "skipped") else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    """Diagnóstico: chequea providers LLM, DB, settings."""
    settings = get_settings()
    print("=" * 60)
    print("SmartBox v2 — Doctor")
    print("=" * 60)
    print(f"  • OPENAI_API_KEY: {'✓' if has_openai_key() else '✗ FALTA'}")
    print(f"  • Símbolos: {settings.symbol_list}")
    print(f"  • Primary: {settings.primary_symbol}")
    print(f"  • DRY_RUN: {settings.dry_run}")
    start_iso, end_iso = settings.vp_window()
    print(f"  • Ventana datos (UTC): {start_iso} → {end_iso}")
    print(f"  • Caja: {settings.box_start}–{settings.box_end} ({settings.market_tz})")
    print(f"  • DB path: {settings.db_path}")

    db.init_db()
    if Path(settings.db_path).exists():
        print(f"  • DB: ✓ existe ({Path(settings.db_path).stat().st_size} bytes)")
    else:
        print("  • DB: ✗ no existe")

    print()
    print("LLM Providers:")
    for agent, model in [
        ("decision_maker", settings.llm.decision_maker),
        ("trader", settings.llm.trader),
        ("risk_analyst", settings.llm.risk_analyst),
        ("mtfa", settings.llm.mtfa),
        ("position_manager", settings.llm.position_manager),
    ]:
        print(f"  • {agent:20s} {model}")

    print()
    print("Credenciales:")
    problems = settings.validate_credentials()
    if problems:
        for p in problems:
            print(f"  • ✗ {p}")
    else:
        print("  • ✓ sin problemas detectados")

    print()
    print("Local providers health:")
    health = LLMHealthChecker.health_check_all(settings.llm)
    for name, ok in health.items():
        print(f"  • {name}: {'✓' if ok else '✗ (no responde)'}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Muestra estado de runs recientes + stats."""
    db.init_db()
    runs = run_repo.list_runs(limit=10)
    stats = trade_repo.compute_stats()
    latest_eq = equity_repo.latest_snapshot()

    print("=" * 60)
    print("SmartBox v2 — Status")
    print("=" * 60)
    print(f"Total trades: {stats['total_trades']} | Open: {stats['open_trades']} | Cerrados: {stats['closed_trades']}")
    print(f"Wins: {stats['wins']} | Losses: {stats['losses']} | Win rate: {stats['win_rate_pct']}%")
    print(f"Total P&L: {stats['total_pnl']} | Avg R: {stats['avg_r_multiple']}")
    if latest_eq:
        print(f"Último balance: {latest_eq.balance} | Equity: {latest_eq.equity}")
    print()
    print("Últimos 10 runs:")
    for r in runs:
        err = f" ({r.error[:50]}...)" if r.error else ""
        print(f"  [{r.status:8s}] {r.id[:8]} {r.started_at} → {r.finished_at or 'running'}{err}")
    return 0


def cmd_trades(args: argparse.Namespace) -> int:
    """Lista los últimos N trades."""
    db.init_db()
    trades = trade_repo.list_trades(limit=args.limit or 20)
    if not trades:
        print("Sin trades registrados.")
        return 0
    print(f"{'ID':<6} {'DATE':<20} {'SYM':<8} {'SIDE':<5} {'VOL':<6} {'ENTRY':<10} {'STATUS':<12} {'P&L':<10}")
    print("-" * 90)
    for t in trades:
        pnl = f"{t.pnl:.2f}" if t.pnl is not None else "-"
        print(f"{t.id:<6} {t.ts_open:<20} {t.symbol:<8} {t.side:<5} {t.volume:<6} {t.entry_price:<10} {t.status:<12} {pnl:<10}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smartbox", description="SmartBox Trading v2")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Ejecuta el pipeline (once o monitor)")
    p_run.add_argument("--dry-run", action="store_true", help="No envía órdenes al broker")
    p_run.add_argument(
        "--mode", choices=["once", "monitor"], default=None,
        help="Sobrescribe RUN_MODE: once (una vez) o monitor (loop en ventana)",
    )
    p_run.set_defaults(func=cmd_run)

    p_monitor = sub.add_parser("monitor", help="Atajo de `run --mode monitor`")
    p_monitor.add_argument("--dry-run", action="store_true", help="No envía órdenes al broker")
    p_monitor.set_defaults(func=cmd_run, mode="monitor")

    p_doctor = sub.add_parser("doctor", help="Diagnóstico del sistema")
    p_doctor.set_defaults(func=cmd_doctor)

    p_status = sub.add_parser("status", help="Estado y stats")
    p_status.set_defaults(func=cmd_status)

    p_trades = sub.add_parser("trades", help="Lista trades")
    p_trades.add_argument("--limit", type=int, default=20, help="Cantidad máxima")
    p_trades.set_defaults(func=cmd_trades)

    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
