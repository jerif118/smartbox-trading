"""
Score de calidad del setup.

Lo que se prueba aquí es la REGLA, no la calibración: los pesos concretos son
un punto de partida sin medir (ver el docstring del módulo), así que los tests
fijan el comportamiento cualitativo — qué sube, qué baja, qué tumba el setup —
y no números mágicos que habría que reescribir tras cada recalibración.
"""

from __future__ import annotations

from domain.signals.confluence import (
    BASE_SCORE,
    DEFAULT_PROCEED_THRESHOLD,
    compute_confluence_score,
)


def _score(**kwargs) -> int:
    base = {"direction": "LONG"}
    return compute_confluence_score(**{**base, **kwargs})["score"]


def test_setup_neutro_pasa_raspando() -> None:
    """Sin nada a favor ni en contra se opera: una ruptura es una ruptura."""
    result = compute_confluence_score(direction="LONG")

    assert result["score"] == BASE_SCORE
    assert result["recommendation"] == "PROCEED"


def test_direccion_contraria_al_breakout_es_cero() -> None:
    """Regla de seguridad: nunca se opera contra el propio breakout."""
    result = compute_confluence_score(direction="SHORT", breakout_aligned=False)

    assert result["score"] == 0
    assert result["recommendation"] == "NO_OPERAR"


def test_la_penetracion_ya_no_es_un_argumento() -> None:
    """El score no acepta penetración: dejó de ser un factor de decisión."""
    import inspect

    params = inspect.signature(compute_confluence_score).parameters
    assert "penetration_pct" not in params
    assert "min_penetration_pct" not in params


# ── Tendencia de 4h: el filtro principal ──────────────────────────────
def test_a_favor_del_4h_suma() -> None:
    assert _score(htf_bias="BULLISH") > BASE_SCORE


def test_contra_el_4h_tumba_el_setup_por_si_solo() -> None:
    result = compute_confluence_score(direction="LONG", htf_bias="BEARISH")

    assert result["score"] < DEFAULT_PROCEED_THRESHOLD
    assert result["recommendation"] == "NO_OPERAR"
    assert any("4h" in f for f in result["factors"])


def test_short_contra_el_4h_alcista_tambien_se_frena() -> None:
    assert _score(direction="SHORT", htf_bias="BULLISH") < DEFAULT_PROCEED_THRESHOLD


def test_4h_neutral_no_puntua() -> None:
    assert _score(htf_bias="NEUTRAL") == BASE_SCORE
    assert _score(htf_bias=None) == BASE_SCORE


# ── Camino hasta el TP a través del Volume Profile ────────────────────
def test_camino_despejado_suma() -> None:
    assert _score(clear_path_fraction=1.0) > BASE_SCORE


def test_obstaculo_temprano_penaliza_mas_que_uno_tardio() -> None:
    temprano = _score(clear_path_fraction=0.2)
    tardio = _score(clear_path_fraction=0.8)

    assert temprano < tardio < BASE_SCORE


def test_tp_al_otro_lado_de_un_nivel_gordo_no_se_opera() -> None:
    """Un nivel fuerte en el primer tercio del camino frena el trade."""
    result = compute_confluence_score(direction="LONG", clear_path_fraction=0.15)

    assert result["recommendation"] == "NO_OPERAR"
    assert any("TP" in f for f in result["factors"])


def test_sin_volume_profile_el_camino_no_puntua() -> None:
    assert _score(clear_path_fraction=None) == BASE_SCORE


# ── Divergencias precio/RSI ───────────────────────────────────────────
def test_divergencia_en_contra_tumba_el_setup() -> None:
    result = compute_confluence_score(direction="LONG", rsi_divergence="BEARISH")

    assert result["score"] < DEFAULT_PROCEED_THRESHOLD
    assert result["recommendation"] == "NO_OPERAR"


def test_divergencia_a_favor_suma_poco() -> None:
    a_favor = _score(rsi_divergence="BULLISH")

    assert BASE_SCORE < a_favor < _score(htf_bias="BULLISH")


def test_short_con_divergencia_alcista_se_frena() -> None:
    assert _score(direction="SHORT", rsi_divergence="BULLISH") < DEFAULT_PROCEED_THRESHOLD


# ── Combinaciones ─────────────────────────────────────────────────────
def test_el_mejor_setup_posible_llega_a_riesgo_completo() -> None:
    """4h a favor + camino limpio + divergencia a favor supera el umbral de 70."""
    score = _score(
        htf_bias="BULLISH",
        mtf_alignment="ALIGNED",
        clear_path_fraction=1.0,
        rsi_divergence="BULLISH",
    )

    assert score >= 70


def test_el_peor_setup_posible_se_queda_en_cero() -> None:
    score = _score(
        htf_bias="BEARISH",
        mtf_alignment="COUNTER",
        clear_path_fraction=0.0,
        rsi_divergence="BEARISH",
    )

    assert score == 0


def test_el_4h_a_favor_no_rescata_un_camino_muy_bloqueado() -> None:
    assert _score(htf_bias="BULLISH", clear_path_fraction=0.1) < DEFAULT_PROCEED_THRESHOLD


def test_el_score_siempre_cae_en_0_100() -> None:
    combos = [
        {"htf_bias": "BEARISH", "mtf_alignment": "COUNTER", "clear_path_fraction": 0.0},
        {"htf_bias": "BULLISH", "mtf_alignment": "ALIGNED", "clear_path_fraction": 1.0},
        {"clear_path_fraction": -5.0},
        {"clear_path_fraction": 99.0},
    ]
    for combo in combos:
        assert 0 <= _score(**combo) <= 100
