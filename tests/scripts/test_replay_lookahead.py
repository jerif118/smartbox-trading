"""El replay no puede usar velas de marcos mayores que aún no han cerrado."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd

_spec = importlib.util.spec_from_file_location(
    "replay", Path(__file__).resolve().parents[2] / "scripts" / "replay.py"
)
replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay)


def test_biases_ignore_htf_candle_still_open_at_cutoff() -> None:
    # 25 velas de 4h bajistas y una última (abierta al corte) que se dispara.
    # Si el replay la usara, el sesgo saldría BULLISH con información futura.
    period = 4 * 3600
    closes = [100.0 - i for i in range(25)] + [500.0]
    df = pd.DataFrame({"time": [i * period for i in range(26)], "close": closes})
    cutoff = 25 * period + 3600  # 1h dentro de la última vela de 4h

    biases = replay._biases_at({"4h": df}, cutoff)

    assert biases["4h"] == "BEARISH"
