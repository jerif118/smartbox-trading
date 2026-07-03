"""
Utilidades de tiempo de mercado.

box_window_unix migrada VERBATIM desde tools_bot.time_now (legacy) para que
el pipeline no dependa de código legacy. La implementación con pandas se
conserva idéntica a propósito: maneja DST igual que siempre.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd


def box_window_unix(date_str: str, start_h: str, end_h: str, tz_name: str) -> tuple[int, int]:
    """Ventana de la caja interpretada en la zona horaria del mercado."""
    tz = ZoneInfo(tz_name)
    start = pd.Timestamp(f"{date_str} {start_h}:00", tz=tz)
    end = pd.Timestamp(f"{date_str} {end_h}:00", tz=tz)
    return (int(start.timestamp()), int(end.timestamp()))


# ── Ventana operativa (monitoreo) ─────────────────────────────────────
def parse_hhmm(value: str) -> time:
    """'10:15' → time(10, 15). Acepta también 'HH:MM:SS'."""
    parts = [int(p) for p in value.strip().split(":")]
    h = parts[0]
    m = parts[1] if len(parts) > 1 else 0
    s = parts[2] if len(parts) > 2 else 0
    return time(hour=h, minute=m, second=s)


def now_in_tz(tz_name: str) -> datetime:
    """Hora actual, timezone-aware, en la zona del mercado."""
    return datetime.now(ZoneInfo(tz_name))


def is_within_window(now: datetime | time, start: time, end: time) -> bool:
    """True si `now` cae dentro de [start, end] (inclusive).

    Acepta un datetime (se usa su .time()) o un time directamente. La
    comparación es por hora-del-día; no cruza medianoche (start <= end).
    """
    t = now.time() if isinstance(now, datetime) else now
    return start <= t <= end


def compute_next_start(now: datetime, start: time) -> datetime:
    """Próximo inicio de ventana a partir de `now` (mismo tz que `now`).

    - Si aún no llegó la hora de inicio de hoy → hoy a `start`.
    - Si ya pasó → mañana a `start`.
    """
    candidate = now.replace(
        hour=start.hour, minute=start.minute, second=start.second, microsecond=0
    )
    if now <= candidate:
        return candidate
    return candidate + timedelta(days=1)


def seconds_until(target: datetime, now: datetime) -> float:
    """Segundos (no negativos) desde `now` hasta `target`."""
    delta = (target - now).total_seconds()
    return max(0.0, delta)
