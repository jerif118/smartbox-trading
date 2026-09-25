"""
Score determinista de CALIDAD DEL SETUP (reglas duras).

Lógica pura, sin I/O ni LLM. Es la fuente de verdad del score que decide si un
setup se opera, de modo que el número NO puede inflarse por una alucinación del
LLM al pasar booleanos incorrectos.

Qué mide y qué NO
─────────────────
NO mide la fuerza de la ruptura. Cuánto penetró el cierre fuera de la caja ya
no puntúa: una ruptura de la caja es una ruptura, y descartarla por penetrar
poco descartaba roturas buenas por décimas. El único resto de esa idea vive en
Stage 4 (``MIN_BREAKOUT_PENETRATION_PCT``, desactivado por defecto) como
guardia contra el caso extremo de un cierre pegado literalmente al borde.

Lo que sí puntúa es el CONTEXTO en el que se opera esa ruptura:

- **Tendencia de 4h**: operar a favor del marco mayor suma; operar contra él es
  el factor que más resta. Es el filtro que pidió el usuario como principal.
- **Alineación multi-timeframe** (15min/1h/4h): matiz secundario del anterior.
- **Camino hasta el TP a través del Volume Profile**: si entre la entrada y el
  take profit hay un nivel de alto volumen, el precio tiende a frenarse ahí y
  el TP no se toca. Cuanto antes aparece el obstáculo, más resta.
- **Divergencia precio/RSI**: entrar en la dirección de la rotura con una
  divergencia en contra es entrar donde el momento se está agotando.

Aviso sobre la calibración
──────────────────────────
Los pesos de abajo NO están calibrados sobre histórico: son un punto de
partida razonado, no medido. La versión anterior de este score (RSI + POC vs
mid + breakout alineado + MTF) resultó ANTI-predictiva sobre 235 rupturas
reales: aprobaba 53.1% de acierto frente al 55.3% de no filtrar. Estos factores
no son los mismos —divergencia y obstáculo-al-TP son nuevos, y el sesgo 4h pesa
mucho más— pero el precedente obliga a medir antes de confiar.

Recalibrar con ``scripts/replay.py`` antes de dar por bueno cualquier peso.
"""

from __future__ import annotations

# Punto de partida de un setup sin nada a favor ni en contra. Coincide con el
# umbral de operación por defecto: si no hay motivo para dudar, se opera.
BASE_SCORE = 55

THRESHOLD_SCORE = 55
DEFAULT_PROCEED_THRESHOLD = THRESHOLD_SCORE

# ── Pesos por factor ──────────────────────────────────────────────────
# Tendencia de 4h: el filtro principal. Operar contra ella tumba el setup por
# sí solo (55 - 30 = 25, muy por debajo del umbral).
HTF_ALIGNED_BONUS = 15
HTF_COUNTER_PENALTY = 30

# Alineación 15min/1h/4h: matiz, no veredicto. El 4h ya pesa aparte.
MTF_ALIGNED_BONUS = 5
MTF_COUNTER_PENALTY = 10

# Camino al TP despejado de niveles de volumen.
CLEAR_PATH_BONUS = 15
BLOCKED_PATH_PENALTY = 30

# Divergencia precio/RSI en la dirección del trade.
DIVERGENCE_AGAINST_PENALTY = 25
DIVERGENCE_FAVOR_BONUS = 5


def compute_confluence_score(
    *,
    direction: str,
    breakout_aligned: bool = True,
    htf_bias: str | None = None,
    mtf_alignment: str | None = None,
    clear_path_fraction: float | None = None,
    rsi_divergence: str | None = None,
    proceed_threshold: int = DEFAULT_PROCEED_THRESHOLD,
) -> dict:
    """Score 0-100 de calidad del setup, con sus factores explicados.

    ``breakout_aligned`` es una condición de seguridad: si la dirección
    propuesta contradice el breakout, el setup no es operable y el score es 0.
    No suma puntos por cumplirse — eso era la constante de 30 que inflaba todos
    los scores en la versión anterior.

    ``clear_path_fraction`` es la fracción del recorrido entrada→TP libre de
    niveles fuertes del Volume Profile (ver ``domain.signals.vp_path``). None
    cuando no hay perfil: no puntúa ni a favor ni en contra.
    """
    direction = direction.upper()
    factors: list[str] = []

    if not breakout_aligned:
        return {
            "score": 0,
            "recommendation": "NO_OPERAR",
            "factors": [f"dirección {direction} contraria al breakout"],
        }

    score = float(BASE_SCORE)

    score += _score_htf(direction, htf_bias, factors)
    score += _score_mtf(mtf_alignment, factors)
    score += _score_path(clear_path_fraction, factors)
    score += _score_divergence(direction, rsi_divergence, factors)

    final = round(max(0.0, min(100.0, score)))
    return {
        "score": final,
        "recommendation": "PROCEED" if final >= proceed_threshold else "NO_OPERAR",
        "factors": factors,
    }


def _score_htf(direction: str, htf_bias: str | None, factors: list[str]) -> float:
    """Tendencia de 4h frente a la dirección del trade."""
    bias = (htf_bias or "").upper()
    if bias not in ("BULLISH", "BEARISH"):
        factors.append("tendencia 4h neutral o desconocida: no puntúa")
        return 0.0

    target = "BULLISH" if direction == "LONG" else "BEARISH"
    if bias == target:
        factors.append(f"a favor de la tendencia de 4h ({bias})")
        return HTF_ALIGNED_BONUS
    factors.append(f"CONTRA la tendencia de 4h ({bias}) — el trade rema en contra")
    return -HTF_COUNTER_PENALTY


def _score_mtf(mtf_alignment: str | None, factors: list[str]) -> float:
    """Alineación combinada 15min/1h/4h."""
    alignment = (mtf_alignment or "").upper()
    if alignment == "ALIGNED":
        factors.append("marcos temporales alineados")
        return MTF_ALIGNED_BONUS
    if alignment == "COUNTER":
        factors.append("marcos temporales en contra")
        return -MTF_COUNTER_PENALTY
    return 0.0


def _score_path(clear_path_fraction: float | None, factors: list[str]) -> float:
    """Obstáculos de volumen entre la entrada y el take profit."""
    if clear_path_fraction is None:
        factors.append("sin volume profile: el camino al TP no se pudo evaluar")
        return 0.0

    fraction = max(0.0, min(1.0, float(clear_path_fraction)))
    if fraction >= 1.0:
        factors.append("camino al TP despejado de niveles de volumen")
        return CLEAR_PATH_BONUS

    penalty = BLOCKED_PATH_PENALTY * (1.0 - fraction)
    factors.append(
        f"nivel fuerte de volumen tras solo el {fraction * 100:.0f}% del camino "
        f"al TP — el precio puede frenarse ahí"
    )
    return -penalty


def _score_divergence(direction: str, rsi_divergence: str | None, factors: list[str]) -> float:
    """Divergencia precio/RSI respecto a la dirección del trade."""
    divergence = (rsi_divergence or "").upper()
    if divergence not in ("BULLISH", "BEARISH"):
        return 0.0

    opposes = (direction == "LONG" and divergence == "BEARISH") or (
        direction == "SHORT" and divergence == "BULLISH"
    )
    if opposes:
        factors.append(f"divergencia {divergence} en contra: momento agotándose")
        return -DIVERGENCE_AGAINST_PENALTY
    factors.append(f"divergencia {divergence} a favor")
    return DIVERGENCE_FAVOR_BONUS
