"""Tests de la lógica multi-timeframe pura."""

from __future__ import annotations

from domain.signals.mtf import ema_bias, mtf_alignment


def test_ema_bias_bullish_on_uptrend():
    closes = [float(i) for i in range(1, 40)]  # sube monótono
    assert ema_bias(closes) == "BULLISH"


def test_ema_bias_bearish_on_downtrend():
    closes = [float(i) for i in range(40, 1, -1)]  # baja monótono
    assert ema_bias(closes) == "BEARISH"


def test_ema_bias_neutral_when_too_few():
    assert ema_bias([1.0, 2.0, 3.0]) == "NEUTRAL"


def test_mtf_alignment_aligned():
    biases = {"15min": "BULLISH", "1h": "BULLISH", "4h": "NEUTRAL"}
    assert mtf_alignment(biases, "LONG") == "ALIGNED"


def test_mtf_alignment_counter():
    biases = {"15min": "BEARISH", "1h": "BEARISH", "4h": "BULLISH"}
    assert mtf_alignment(biases, "LONG") == "COUNTER"


def test_mtf_alignment_mixed():
    biases = {"15min": "BULLISH", "1h": "NEUTRAL", "4h": "BEARISH"}
    assert mtf_alignment(biases, "LONG") == "MIXED"
