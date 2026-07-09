"""
Streaming OHLC de Capital.com (WebSocket) — gatillo de baja latencia.

NO reemplaza al REST: s1_ingest sigue descargando las velas oficiales por
REST (fuente de verdad). El socket solo detecta "cerró una vela" para que el
monitor corra el pipeline al instante en vez de esperar al próximo tick de
reloj. Si el socket se cae, el monitor sigue funcionando por reloj alineado
al cierre de velas (fallback automático — no hay dependencia dura).

Mensajes reales (verificados en vivo el 2026-07-08):
- Suscripción:
    {"destination": "OHLCMarketData.subscribe", "correlationId": "1",
     "cst": <CST>, "securityToken": <X-SECURITY-TOKEN>,
     "payload": {"epics": ["US500"], "resolutions": ["MINUTE_5"],
                 "type": "classic"}}
  Respuesta: payload.subscriptions {"US500:MINUTE_5:classic": "PROCESSED"}
- Evento de vela (updates de la vela EN CURSO; t = inicio de vela en ms):
    {"status": "OK", "destination": "ohlc.event",
     "payload": {"resolution": "MINUTE_5", "epic": "US500", "type": "classic",
                 "priceType": "bid", "t": 1783522200000,
                 "h": .., "l": .., "o": .., "c": ..}}
  Una vela se considera CERRADA cuando llega el primer evento con un t mayor.
  OJO: los ohlc.event llegan con POCA frecuencia (~1 por 45s, verificado) →
  no sirven como gatillo rápido por sí solos.
- Cotizaciones (marketData.subscribe → destination "quote"): fluyen constante
  (~2/s con mercado abierto). El gatillo rápido real es el PRIMER quote cuyo
  timestamp cruza el múltiplo del intervalo: la vela anterior acaba de cerrar.
    {"status": "OK", "destination": "quote",
     "payload": {"epic": "US500", "bid": .., "ofr": .., "timestamp": <ms>}}
- El servidor corta la conexión tras ~60s SIN TRÁFICO (verificado en vivo;
  la doc menciona 10 min pero empíricamente es 1 min) → ping de APLICACIÓN
  cada 30s:
    {"destination": "ping", "correlationId": ..., "cst": ..., "securityToken": ...}
  El ping protocolar de websocket NO cuenta como actividad para el server.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass

from infrastructure.broker.capital.adapter import TIMEFRAME_SECONDS, CapitalAdapter
from utils.logger import get_logger

log = get_logger(__name__)

STREAM_URL = "wss://api-streaming-capital.backend-capital.com/connect"
APP_PING_INTERVAL_S = 30  # el server corta a ~60s sin tráfico (verificado)
RECONNECT_BACKOFF_S = (5, 10, 30, 60)  # escalera; se repite el último


@dataclass(frozen=True)
class OHLCBar:
    """Vela recibida por el socket. t_open_s = inicio de vela (epoch, s)."""

    epic: str
    resolution: str
    t_open_s: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class BarBoundary:
    """Cierre de vela detectado por cruce de frontera en quotes (sin OHLC)."""

    epic: str
    t_open_s: int


def parse_ohlc_event(message: str | dict) -> OHLCBar | None:
    """Extrae la vela de un mensaje del socket. None si no es ohlc.event."""
    try:
        data = json.loads(message) if isinstance(message, str) else message
    except (json.JSONDecodeError, TypeError):
        return None
    if data.get("destination") != "ohlc.event":
        return None
    p = data.get("payload") or {}
    try:
        return OHLCBar(
            epic=str(p["epic"]),
            resolution=str(p["resolution"]),
            t_open_s=int(p["t"]) // 1000,
            open=float(p["o"]),
            high=float(p["h"]),
            low=float(p["l"]),
            close=float(p["c"]),
        )
    except (KeyError, ValueError, TypeError):
        log.warning("ohlc.event con payload inesperado: %s", str(p)[:200])
        return None


def parse_quote_event(message: str | dict) -> tuple[str, int] | None:
    """Extrae (epic, timestamp_ms) de un mensaje `quote`. None si no lo es."""
    try:
        data = json.loads(message) if isinstance(message, str) else message
    except (json.JSONDecodeError, TypeError):
        return None
    if data.get("destination") != "quote":
        return None
    p = data.get("payload") or {}
    try:
        return (str(p["epic"]), int(p["timestamp"]))
    except (KeyError, ValueError, TypeError):
        return None


class BarCloseDetector:
    """Detecta cierres de vela: el socket manda updates de la vela EN CURSO;
    cuando llega un evento con t mayor, la vela anterior quedó cerrada."""

    def __init__(self) -> None:
        self._current: dict[tuple[str, str], OHLCBar] = {}

    def on_bar(self, bar: OHLCBar) -> OHLCBar | None:
        """Procesa un update. Retorna la vela CERRADA si este update abre
        una vela nueva; None mientras siga la misma vela."""
        key = (bar.epic, bar.resolution)
        prev = self._current.get(key)
        self._current[key] = bar
        if prev is not None and bar.t_open_s > prev.t_open_s:
            return prev
        return None


class QuoteBoundaryDetector:
    """Detecta el cierre de vela por CRUCE DE FRONTERA en los quotes.

    Los quotes fluyen ~2/s; el primer quote con timestamp en un intervalo
    nuevo (múltiplo de interval_s en epoch) implica que la vela anterior
    acaba de cerrar → gatillo con latencia < 1s. Devuelve el epoch de INICIO
    de la vela cerrada, o None si el quote sigue en la misma vela."""

    def __init__(self, interval_s: int) -> None:
        self._interval_s = interval_s
        self._current_bar: dict[str, int] = {}  # epic → índice de vela

    def on_quote(self, epic: str, timestamp_ms: int) -> int | None:
        bar_idx = (timestamp_ms // 1000) // self._interval_s
        prev = self._current_bar.get(epic)
        self._current_bar[epic] = bar_idx
        if prev is not None and bar_idx > prev:
            return prev * self._interval_s  # inicio de la vela cerrada
        return None


class CapitalOHLCStream:
    """Cliente WebSocket con reconexión y ping. Llama `on_bar_close(...)`
    (desde el thread del socket) cada vez que cierra una vela.

    Gatillo primario: quotes cruzando la frontera del intervalo (< 1s de
    latencia). Los ohlc.event se mantienen como confirmación/log, pero llegan
    demasiado espaciados para ser el gatillo. Un mismo cierre se emite UNA
    sola vez aunque lo detecten varios epics/vías (dedupe por frontera)."""

    def __init__(
        self,
        epics: list[str],
        resolution: str,
        on_bar_close: Callable[[OHLCBar | BarBoundary], None],
        settings=None,
    ) -> None:
        self._epics = list(epics)
        self._resolution = resolution
        self._interval_s = TIMEFRAME_SECONDS.get(resolution, 300)
        self._on_bar_close = on_bar_close
        self._settings = settings
        self._detector = BarCloseDetector()
        self._quote_detector = QuoteBoundaryDetector(self._interval_s)
        self._last_boundary = 0  # última frontera ya emitida (dedupe)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws = None
        self._connected = threading.Event()

    # ── API pública ─────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="capital-ohlc-stream", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):  # cierre best-effort
                ws.close()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    # ── Loop interno ────────────────────────────────────────────────────
    def _run_loop(self) -> None:
        import websocket

        attempt = 0
        while not self._stop.is_set():
            try:
                tokens = CapitalAdapter(self._settings)._get_tokens()
                ws = websocket.WebSocketApp(
                    STREAM_URL,
                    on_open=lambda w, tk=tokens: self._subscribe(w, tk),
                    on_message=lambda w, m: self._handle_message(m),
                    on_error=lambda w, e: log.warning("stream error: %s", e),
                    on_close=lambda w, c, r: self._connected.clear(),
                )
                self._ws = ws
                attempt = 0
                # keep-alive: ping de aplicación (ver _start_ping); el ping
                # protocolar no cuenta como actividad para el server.
                ws.run_forever()
            except Exception as e:  # barrera del thread — nunca debe morir
                log.warning("stream caído: %s", e)
            finally:
                self._connected.clear()
                self._ws = None

            if self._stop.is_set():
                return
            # Token pudo expirar → forzar re-login en la próxima vuelta
            CapitalAdapter.reset_token_cache()
            backoff = RECONNECT_BACKOFF_S[min(attempt, len(RECONNECT_BACKOFF_S) - 1)]
            attempt += 1
            log.info("stream: reintentando conexión en %ds", backoff)
            self._stop.wait(backoff)

    def _subscribe(self, ws, tokens: dict[str, str]) -> None:
        sub_ohlc = {
            "destination": "OHLCMarketData.subscribe",
            "correlationId": "smartbox-1",
            "cst": tokens["CST"],
            "securityToken": tokens["X-SECURITY-TOKEN"],
            "payload": {
                "epics": self._epics,
                "resolutions": [self._resolution],
                "type": "classic",
            },
        }
        ws.send(json.dumps(sub_ohlc))
        # Quotes: gatillo rápido (fluyen ~2/s) + mantienen viva la conexión
        sub_quotes = {
            "destination": "marketData.subscribe",
            "correlationId": "smartbox-2",
            "cst": tokens["CST"],
            "securityToken": tokens["X-SECURITY-TOKEN"],
            "payload": {"epics": self._epics},
        }
        ws.send(json.dumps(sub_quotes))
        self._connected.set()
        self._start_ping(ws, tokens)
        log.info(
            "stream conectado: %s %s (gatillo de cierre de vela)",
            self._epics, self._resolution,
        )

    def _start_ping(self, ws, tokens: dict[str, str]) -> None:
        """Ping de aplicación cada APP_PING_INTERVAL_S para que el server no
        corte la conexión por inactividad. Muere solo al caer ws o al stop."""

        def _loop() -> None:
            ping = json.dumps({
                "destination": "ping",
                "correlationId": "smartbox-ping",
                "cst": tokens["CST"],
                "securityToken": tokens["X-SECURITY-TOKEN"],
            })
            while not self._stop.wait(APP_PING_INTERVAL_S):
                if self._ws is not ws:  # hubo reconexión: este ws ya no vale
                    return
                try:
                    ws.send(ping)
                except Exception:  # conexión caída: run_forever reconectará
                    return

        threading.Thread(target=_loop, name="capital-stream-ping", daemon=True).start()

    def _handle_message(self, message: str) -> None:
        quote = parse_quote_event(message)
        if quote is not None:
            epic, ts_ms = quote
            closed_t = self._quote_detector.on_quote(epic, ts_ms)
            if closed_t is not None and closed_t > self._last_boundary:
                self._last_boundary = closed_t
                log.info(
                    "stream: vela de %ds cerrada (t=%d, quote de %s cruzó frontera) "
                    "→ despertar monitor", self._interval_s, closed_t, epic,
                )
                self._emit(BarBoundary(epic=epic, t_open_s=closed_t))
            return

        bar = parse_ohlc_event(message)
        if bar is None:
            return
        closed = self._detector.on_bar(bar)
        if closed is None:
            return
        # Normalmente el quote ya emitió esta frontera (dedupe); esto es la
        # red de seguridad por si los quotes no fluyen.
        if closed.t_open_s > self._last_boundary:
            self._last_boundary = closed.t_open_s
            log.info(
                "stream: vela %s cerrada (t=%d close=%.2f, vía ohlc.event) "
                "→ despertar monitor", closed.epic, closed.t_open_s, closed.close,
            )
            self._emit(closed)

    def _emit(self, event: OHLCBar | BarBoundary) -> None:
        try:
            self._on_bar_close(event)
        except Exception as e:  # el callback no debe tumbar el socket
            log.error("on_bar_close falló: %s", e)
