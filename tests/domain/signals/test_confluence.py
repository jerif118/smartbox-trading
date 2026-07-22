"""Tests del confluence score determinista."""

from __future__ import annotations

from domain.signals.confluence import (
    compute_confluence_score,
    rsi_direction_from_value,
)


def test_rsi_direction_thresholds():
    assert rsi_direction_from_value(None) == "neutral"
    assert rsi_direction_from_value(30) == "oversold"
    assert rsi_direction_from_value(35) == "oversold"
    assert rsi_direction_from_value(50) == "neutral"
    assert rsi_direction_from_value(65) == "overbought"
    assert rsi_direction_from_value(80) == "overbought"


def test_full_confluence_long():
    # RSI neutral(20) + POC alineado(25) + breakout(30) + MTF(25) = 100
    r = compute_confluence_score(
        direction="LONG",
        rsi_value=50,
        poc_above_mid=True,
        breakout_aligned=True,
        mtf_aligned=True,
    )
    assert r["score"] == 100
    assert r["recommendation"] == "PROCEED"


def test_low_confluence_below_threshold():
    # RSI neutral(20) + POC contrario(10) + sin breakout + sin MTF = 30
    r = compute_confluence_score(
        direction="LONG",
        rsi_value=50,
        poc_above_mid=False,
        breakout_aligned=False,
        mtf_aligned=False,
    )
    assert r["score"] == 30
    assert r["recommendation"] == "NO_OPERAR"


def test_poc_alignment_flips_by_direction():
    long_r = compute_confluence_score(
        direction="LONG", rsi_value=50, poc_above_mid=True,
        breakout_aligned=False, mtf_aligned=False,
    )
    short_r = compute_confluence_score(
        direction="SHORT", rsi_value=50, poc_above_mid=True,
        breakout_aligned=False, mtf_aligned=False,
    )
    # POC arriba favorece LONG, no SHORT
    assert long_r["score"] > short_r["score"]


def test_none_rsi_treated_neutral():
    r = compute_confluence_score(
        direction="LONG", rsi_value=None, poc_above_mid=True,
        breakout_aligned=True, mtf_aligned=False,
    )
    # neutral(20) + POC(25) + breakout(30) = 75
    assert r["score"] == 75
