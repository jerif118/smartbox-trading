"""Obstáculos de volumen entre la entrada y el take profit."""

from __future__ import annotations

from domain.signals.vp_path import (
    clear_path_fraction,
    dedupe_levels,
    obstacles_between,
    strong_levels,
)


def test_strong_levels_reune_poc_vah_val_y_picos() -> None:
    vp = {"poc": 100.0, "vah": 110.0, "val": 90.0, "peaks": [(105.0, 50.0), (95.0, 30.0)]}

    assert strong_levels(vp) == [90.0, 95.0, 100.0, 105.0, 110.0]


def test_strong_levels_conserva_solo_los_picos_mas_negociados() -> None:
    """El perfil de varios días trae decenas de picos; usarlos todos bloquea todo."""
    peaks = [(float(i), float(i)) for i in range(1, 21)]  # el 20 es el de más volumen
    vp = {"peaks": peaks}

    levels = strong_levels(vp, top_peaks=3)

    assert levels == [18.0, 19.0, 20.0]


def test_strong_levels_sin_perfil_devuelve_vacio() -> None:
    assert strong_levels(None) == []
    assert strong_levels({}) == []


def test_obstacles_between_ordena_por_cercania_a_la_entrada() -> None:
    levels = [95.0, 105.0, 115.0, 130.0]

    assert obstacles_between(100.0, 120.0, levels) == [105.0, 115.0]


def test_obstacles_between_funciona_hacia_abajo() -> None:
    """Un SHORT recorre el camino al revés: el orden sigue siendo por cercanía."""
    levels = [70.0, 85.0, 95.0, 105.0]

    assert obstacles_between(100.0, 80.0, levels) == [95.0, 85.0]


def test_obstacles_between_excluye_los_extremos() -> None:
    assert obstacles_between(100.0, 120.0, [100.0, 120.0]) == []


def test_camino_despejado_es_uno() -> None:
    assert clear_path_fraction(100.0, 120.0, [130.0, 90.0]) == 1.0


def test_camino_sin_niveles_es_uno() -> None:
    assert clear_path_fraction(100.0, 120.0, []) == 1.0


def test_fraccion_es_la_distancia_al_primer_obstaculo() -> None:
    # Entrada 100, TP 120, nivel en 105 → solo el 25% del camino está libre.
    assert clear_path_fraction(100.0, 120.0, [105.0]) == 0.25


def test_fraccion_usa_el_obstaculo_mas_cercano_no_el_primero_de_la_lista() -> None:
    assert clear_path_fraction(100.0, 120.0, [118.0, 104.0]) == 0.2


def test_fraccion_en_short() -> None:
    # Entrada 100, TP 80, nivel en 95 → 25% libre.
    assert clear_path_fraction(100.0, 80.0, [95.0]) == 0.25


def test_entrada_igual_al_tp_no_divide_por_cero() -> None:
    assert clear_path_fraction(100.0, 100.0, [100.0]) == 1.0


def test_dedupe_colapsa_niveles_pegados() -> None:
    """El POC casi siempre coincide con un pico: contarlo dos veces distorsiona."""
    # Rango 100 → tolerancia 2.0 puntos.
    assert dedupe_levels([100.0, 101.0, 110.0], reference_range=100.0) == [100.0, 110.0]


def test_dedupe_respeta_niveles_separados() -> None:
    assert dedupe_levels([100.0, 105.0], reference_range=100.0) == [100.0, 105.0]
