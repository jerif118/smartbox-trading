"""Tests del streaming OHLC de Capital.com (parsing y detección de cierres).

Sin red: se testean las piezas puras (parse_ohlc_event, BarCloseDetector).
El formato de los mensajes está verificado contra el socket real (2026-07-08).
"""

from __future__ import annotations

import json

from infrastructure.broker.capital.stream import (
    BarCloseDetector,
    OHLCBar,
    QuoteBoundaryDetector,
    parse_ohlc_event,
    parse_quote_event,
)

# Mensaje real capturado del socket (2026-07-08)
OHLC_MSG = {
    "status": "OK",
    "destination": "ohlc.event",
    "payload": {
        "resolution": "MINUTE_5",
        "epic": "US500",
        "type": "classic",
        "priceType": "bid",
        "t": 1783522200000,
        "h": 7452.2,
        "l": 7435.3,
        "o": 7438.8,
        "c": 7448.5,
        "lastTradedVolume": 555,
    },
}


def _bar(epic: str = "US500", t_s: int = 1783522200, close: float = 7448.5) -> OHLCBar:
    return OHLCBar(
        epic=epic, resolution="MINUTE_5", t_open_s=t_s,
        open=7438.8, high=7452.2, low=7435.3, close=close,
    )


# ── parse_ohlc_event ──────────────────────────────────────────────────
def test_parse_ohlc_event_formato_real() -> None:
    bar = parse_ohlc_event(json.dumps(OHLC_MSG))
    assert bar is not None
    assert bar.epic == "US500"
    assert bar.resolution == "MINUTE_5"
    assert bar.t_open_s == 1783522200  # ms → s
    assert bar.close == 7448.5
    assert bar.low == 7435.3


def test_parse_ignora_otros_destinos() -> None:
    quote = {
        "status": "OK", "destination": "quote",
        "payload": {"epic": "US500", "bid": 7448.6, "ofr": 7449.0},
    }
    assert parse_ohlc_event(json.dumps(quote)) is None
    ack = {"status": "OK", "destination": "OHLCMarketData.subscribe", "payload": {}}
    assert parse_ohlc_event(ack) is None


def test_parse_tolerante_a_basura() -> None:
    assert parse_ohlc_event("no es json") is None
    assert parse_ohlc_event('{"destination": "ohlc.event", "payload": {}}') is None
    assert parse_ohlc_event('{"destination": "ohlc.event"}') is None


# ── BarCloseDetector ──────────────────────────────────────────────────
def test_detector_no_emite_en_updates_de_la_misma_vela() -> None:
    det = BarCloseDetector()
    assert det.on_bar(_bar(t_s=1000, close=10.0)) is None
    assert det.on_bar(_bar(t_s=1000, close=11.0)) is None  # update, misma vela
    assert det.on_bar(_bar(t_s=1000, close=12.0)) is None


def test_detector_emite_la_vela_cerrada_al_avanzar_t() -> None:
    det = BarCloseDetector()
    det.on_bar(_bar(t_s=1000, close=10.0))
    det.on_bar(_bar(t_s=1000, close=11.5))  # último update de la vela
    closed = det.on_bar(_bar(t_s=1300, close=12.0))  # abre vela nueva
    assert closed is not None
    assert closed.t_open_s == 1000
    assert closed.close == 11.5  # el ÚLTIMO estado de la vela cerrada


def test_detector_epics_independientes() -> None:
    det = BarCloseDetector()
    det.on_bar(_bar(epic="US500", t_s=1000))
    det.on_bar(_bar(epic="US100", t_s=1000))
    closed = det.on_bar(_bar(epic="US500", t_s=1300))
    assert closed is not None and closed.epic == "US500"
    # US100 sigue en su vela: no emite
    assert det.on_bar(_bar(epic="US100", t_s=1000, close=9.9)) is None


def test_detector_primera_vela_no_emite() -> None:
    """La primera vela vista nunca se emite como cerrada (no hay anterior)."""
    det = BarCloseDetector()
    assert det.on_bar(_bar(t_s=2000)) is None


# ── parse_quote_event ─────────────────────────────────────────────────
QUOTE_MSG = {
    "status": "OK",
    "destination": "quote",
    "payload": {
        "epic": "US500", "product": "CFD",
        "bid": 7448.6, "bidQty": 20.0, "ofr": 7449.0, "ofrQty": 20.0,
        "timestamp": 1783522448446,
    },
}


def test_parse_quote_event_formato_real() -> None:
    assert parse_quote_event(json.dumps(QUOTE_MSG)) == ("US500", 1783522448446)


def test_parse_quote_ignora_otros() -> None:
    assert parse_quote_event(json.dumps(OHLC_MSG)) is None
    assert parse_quote_event('{"destination": "quote", "payload": {}}') is None


# ── QuoteBoundaryDetector ─────────────────────────────────────────────
def test_quote_boundary_emite_al_cruzar_frontera() -> None:
    """Quote a las 10:04:59 y luego 10:05:00.2 → la vela de 10:00 cerró."""
    det = QuoteBoundaryDetector(interval_s=300)
    t_1004_59 = 1783522200_000 + 299_000   # dentro de la vela [t0, t0+300)
    t_1005_00 = 1783522200_000 + 300_200   # primer quote de la vela nueva
    assert det.on_quote("US500", t_1004_59) is None
    closed = det.on_quote("US500", t_1005_00)
    assert closed == 1783522200  # inicio de la vela cerrada (epoch s)


def test_quote_boundary_no_emite_dentro_de_la_vela() -> None:
    det = QuoteBoundaryDetector(interval_s=300)
    base = 1783522200_000
    assert det.on_quote("US500", base + 1_000) is None
    assert det.on_quote("US500", base + 100_000) is None
    assert det.on_quote("US500", base + 299_900) is None


def test_quote_boundary_por_epic() -> None:
    """Cada epic lleva su propio índice de vela."""
    det = QuoteBoundaryDetector(interval_s=300)
    base = 1783522200_000
    det.on_quote("US500", base + 1_000)
    det.on_quote("US100", base + 299_000)
    assert det.on_quote("US500", base + 301_000) == 1783522200
    assert det.on_quote("US100", base + 302_000) == 1783522200  # su propio cruce


def test_quote_boundary_primer_quote_no_emite() -> None:
    det = QuoteBoundaryDetector(interval_s=300)
    assert det.on_quote("US500", 1783522500_000) is None
