"""
Utilidades de tiempo de mercado.

box_window_unix migrada VERBATIM desde tools_bot.time_now (legacy) para que
el pipeline no dependa de código legacy. La implementación con pandas se
conserva idéntica a propósito: maneja DST igual que siempre.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pandas as pd


def box_window_unix(date_str: str, start_h: str, end_h: str, tz_name: str) -> tuple[int, int]:
    """Ventana de la caja interpretada en la zona horaria del mercado."""
    tz = ZoneInfo(tz_name)
    start = pd.Timestamp(f"{date_str} {start_h}:00", tz=tz)
    end = pd.Timestamp(f"{date_str} {end_h}:00", tz=tz)
    return (int(start.timestamp()), int(end.timestamp()))
