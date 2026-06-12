"""Tests de domain.market_time (migrada desde tools_bot.time_now)."""

from __future__ import annotations

from domain.market_time import box_window_unix


def test_box_window_edt():
    """Verano NY (EDT, UTC-4): 08:00 NY = 12:00 UTC."""
    start, end = box_window_unix("2026-06-11", "08:00", "09:55", "America/New_York")
    # 2026-06-11 12:00:00 UTC y 13:55:00 UTC
    assert start == 1781179200
    assert end == 1781186100
    assert end - start == 115 * 60  # 1h55m


def test_box_window_est():
    """Invierno NY (EST, UTC-5): 08:00 NY = 13:00 UTC."""
    start, end = box_window_unix("2026-01-15", "08:00", "09:55", "America/New_York")
    assert start == 1768482000
    assert end == 1768488900
    assert end - start == 115 * 60


def test_parity_with_legacy_implementation():
    """La función migrada produce exactamente lo mismo que la legacy.

    Test temporal de paridad: se puede borrar cuando legacy/ desaparezca.
    """
    import importlib.util
    from pathlib import Path

    legacy_path = (
        Path(__file__).parent.parent.parent / "legacy" / "tools_bot" / "time_now.py"
    )
    if not legacy_path.exists():
        legacy_path = (
            Path(__file__).parent.parent.parent / "src" / "tools_bot" / "time_now.py"
        )
    spec = importlib.util.spec_from_file_location("legacy_time_now", legacy_path)
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)

    for date_str in ("2026-03-08", "2026-11-01", "2026-06-11"):  # incluye cambios DST
        assert box_window_unix(
            date_str, "08:00", "09:55", "America/New_York"
        ) == legacy.box_window_unix(date_str, "08:00", "09:55", "America/New_York")
