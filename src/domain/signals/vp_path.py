"""
Camino hasta el take profit a través del Volume Profile.

La pregunta que responde: entre el precio de entrada y el TP, ¿hay algún nivel
donde ya se negoció mucho volumen? Un pico de volumen es donde el mercado
"acepta" precio: la rotura tiende a frenarse ahí. Un TP al otro lado de un pico
gordo es un TP que probablemente no se toque, y el trade acaba en breakeven o
en stop después de haber ido a favor.

No mide fuerza de la ruptura — mide si el camino está despejado.
"""

from __future__ import annotations

# Dos niveles más cercanos que esto (en % del recorrido entry→TP) son el mismo
# nivel contado dos veces: el POC suele coincidir con un peak.
DEDUPE_TOLERANCE_PCT = 2.0


# Cuántos picos de volumen se consideran "niveles fuertes". El perfil se
# calcula sobre varios días de velas de 5 min, así que la lista cruda de picos
# es larga: usarla entera convierte cualquier camino en un campo de minas y el
# bot deja de operar. Un trader mira los cuatro o cinco niveles gordos, no
# treinta.
DEFAULT_TOP_PEAKS = 5


def strong_levels(vp: dict | None, top_peaks: int = DEFAULT_TOP_PEAKS) -> list[float]:
    """Niveles relevantes del perfil: POC, VAH, VAL y los picos más negociados.

    Los picos ya vienen filtrados por cuantil desde ``compute_volume_profile``;
    aquí se conservan solo los ``top_peaks`` de mayor volumen.
    """
    if not isinstance(vp, dict):
        return []

    levels: list[float] = []
    for key in ("poc", "vah", "val"):
        value = vp.get(key)
        if isinstance(value, int | float):
            levels.append(float(value))

    peaks: list[tuple[float, float]] = []
    for peak in vp.get("peaks") or []:
        # peaks son tuplas (precio, volumen); se acepta también solo el precio.
        if isinstance(peak, list | tuple) and len(peak) >= 2:
            price, volume = peak[0], peak[1]
        else:
            price, volume = peak, 0.0
        if isinstance(price, int | float) and isinstance(volume, int | float):
            peaks.append((float(price), float(volume)))

    peaks.sort(key=lambda pv: pv[1], reverse=True)
    levels.extend(price for price, _ in peaks[: max(0, top_peaks)])

    return sorted(set(levels))


def obstacles_between(entry: float, target: float, levels: list[float]) -> list[float]:
    """Niveles que quedan estrictamente entre la entrada y el objetivo.

    Ordenados por cercanía a la entrada: el primero es el que se encuentra
    antes, y por tanto el que decide si el trade respira o no.
    """
    lo, hi = (entry, target) if entry <= target else (target, entry)
    inside = [level for level in levels if lo < level < hi]
    return sorted(inside, key=lambda level: abs(level - entry))


def clear_path_fraction(entry: float, target: float, levels: list[float]) -> float:
    """Fracción del recorrido entry→TP que está libre de niveles (0.0-1.0).

    1.0 = camino despejado hasta el TP. 0.2 = el primer nivel fuerte aparece
    tras recorrer solo el 20% del camino, es decir, casi todo el beneficio
    esperado está al otro lado de una zona de aceptación.
    """
    distance = abs(target - entry)
    if distance <= 0:
        return 1.0
    blockers = obstacles_between(entry, target, levels)
    if not blockers:
        return 1.0
    return min(1.0, abs(blockers[0] - entry) / distance)


def dedupe_levels(levels: list[float], reference_range: float) -> list[float]:
    """Colapsa niveles separados por menos de DEDUPE_TOLERANCE_PCT del rango."""
    if reference_range <= 0:
        return levels
    tolerance = reference_range * DEDUPE_TOLERANCE_PCT / 100
    merged: list[float] = []
    for level in sorted(levels):
        if not merged or abs(level - merged[-1]) > tolerance:
            merged.append(level)
    return merged
