"""
Adaptador SimpleFX: implementa `BrokerGateway`.

Endpoints usados (verificados contra el swagger oficial, oas3-3.1.json):
- POST /api/v3/auth/key                (login)
- POST /api/v3/trading/orders/pending  (place_order)
- PUT  /api/v3/trading/orders/pending  (modify de orden aún no activada)
- PUT  /api/v3/trading/orders/market   (modify de posición ya activada)

NO añade endpoints nuevos (list_positions, get_account, etc.) — la fuente de
verdad para el Position Manager es SQLite, no el broker.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from domain.market_time import parse_hhmm
from infrastructure.broker.simplefx.market_data import SimpleFXMarketData
from infrastructure.config.settings import get_settings
from utils.logger import get_logger
from utils.retry import retry

log = get_logger(__name__)

SIMPLE_BASE = "https://rest.simplefx.com"

# Precio actual = cierre de la última vela de 1 min del feed público de
# SimpleFX. Se mira 1h hacia atrás para tolerar huecos de liquidez.
PRICE_PERIOD_SECONDS = 60
PRICE_LOOKBACK_SECONDS = 3600


@retry(max_retries=3, backoff=2.0, exceptions=(requests.RequestException,))
def _login(client_id: str, api_key: str) -> str:
    url = f"{SIMPLE_BASE}/api/v3/auth/key"
    body = {"clientId": client_id, "clientSecret": api_key}
    resp = requests.post(url, json=body, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data["data"]["token"]


@retry(max_retries=2, backoff=2.0, exceptions=(requests.RequestException,))
def _place_order(
    token: str,
    account: str,
    symbol: str,
    side: str,
    volume: float,
    entry_price: float,
    stop_loss: float,
    take_profit: float | None,
    reality: str,
    expiry_ms: int | None = None,
) -> dict[str, Any]:
    url = f"{SIMPLE_BASE}/api/v3/trading/orders/pending"
    headers = {"Authorization": f"Bearer {token}"}
    body: dict[str, Any] = {
        "ActivationPrice": entry_price,
        "Symbol": symbol,
        "Volume": volume,
        "StopLoss": stop_loss,
        "Side": side.upper(),
        "Login": int(account),
        "Reality": reality.upper(),
    }
    if take_profit is not None:
        body["TakeProfit"] = take_profit
    if expiry_ms is not None:
        body["ExpiryTime"] = expiry_ms
    log.info(
        "SimpleFX: %s %s vol=%.2f @ %.2f SL=%.2f TP=%s",
        side, symbol, volume, entry_price, stop_loss, take_profit,
    )
    resp = requests.post(url, headers=headers, json=body, timeout=20)
    if resp.status_code >= 400:
        # truncado: el body de error puede contener datos sensibles del server
        log.error("SimpleFX %d: %s", resp.status_code, resp.text[:300])
    resp.raise_for_status()
    return resp.json()


def _modify_at(
    endpoint: str,
    token: str,
    account: str,
    reality: str,
    id_trade: int,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> dict[str, Any]:
    url = f"{SIMPLE_BASE}/api/v3/trading/orders/{endpoint}"
    headers = {"Authorization": f"Bearer {token}"}
    body: dict[str, Any] = {
        "Login": int(account),
        "Reality": reality.upper(),
        "Id": id_trade,
    }
    if take_profit is not None:
        body["TakeProfit"] = take_profit
    if stop_loss is not None:
        body["StopLoss"] = stop_loss
    log.info("Modify %s %d: SL=%s TP=%s", endpoint, id_trade, stop_loss, take_profit)
    resp = requests.put(url, headers=headers, json=body, timeout=20)
    if resp.status_code >= 400:
        log.warning("SimpleFX modify %s %d: %s", endpoint, resp.status_code, resp.text[:300])
    resp.raise_for_status()
    return resp.json()


@retry(max_retries=2, backoff=2.0, exceptions=(requests.ConnectionError, requests.Timeout))
def _modify_order(
    token: str,
    account: str,
    reality: str,
    id_trade: int,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> dict[str, Any]:
    """Modifica SL/TP. El bot coloca órdenes PENDING; mientras no se activan
    el id pertenece a una pending order (PUT /orders/pending). Si la orden ya
    se activó, ese PUT falla y se reintenta contra la posición de mercado
    (PUT /orders/market)."""
    try:
        return _modify_at(
            "pending", token, account, reality, id_trade,
            stop_loss=stop_loss, take_profit=take_profit,
        )
    except requests.HTTPError:
        return _modify_at(
            "market", token, account, reality, id_trade,
            stop_loss=stop_loss, take_profit=take_profit,
        )


def _extract_order_id(result: dict[str, Any]) -> str | None:
    """Extrae el id de la orden de la respuesta real de SimpleFX.

    Forma oficial (swagger): {"data": {"pendingOrders": [{"action": ..,
    "order": {"id": ..}}], "marketOrders": [...]}, "code": .., ...}
    """
    data = result.get("data") or {}
    for key in ("pendingOrders", "marketOrders"):
        entries = data.get(key) or []
        for entry in entries:
            order = (entry or {}).get("order") or {}
            if order.get("id") is not None:
                return str(order["id"])
    # Fallbacks defensivos por si el formato cambia
    for container in (data, result):
        for key in ("id", "orderId"):
            if container.get(key) is not None:
                return str(container[key])
    return None


class SimpleFXAdapter:
    """Implementa BrokerGateway usando solo endpoints existentes."""

    def __init__(self, settings=None, market_data: SimpleFXMarketData | None = None):
        self._settings = settings or get_settings()
        self._token: str | None = None
        self._token_ts: float = 0.0
        self._market_data = market_data or SimpleFXMarketData()

    def _get_token(self) -> str:
        if self._token and (time.time() - self._token_ts) < 1500:  # 25 min
            return self._token
        if not self._settings.sf_id or not self._settings.sf_key:
            raise RuntimeError("SimpleFX credentials (ID, KEY) no configuradas")
        self._token = _login(self._settings.sf_id, self._settings.sf_key)
        self._token_ts = time.time()
        log.info("SimpleFX login OK")
        return self._token

    def _pending_expiry_ms(self, now: datetime | None = None) -> int | None:
        """Caducidad de hoy (PENDING_EXPIRY en market_tz) en ms unix.

        None si ya pasó esa hora: una caducidad en el pasado haría que el
        broker rechazara o cancelara la orden al instante.
        """
        tz = ZoneInfo(self._settings.market_tz)
        now = now or datetime.now(tz)
        expiry = datetime.combine(
            now.astimezone(tz).date(), parse_hhmm(self._settings.pending_expiry), tzinfo=tz
        )
        if expiry <= now:
            return None
        return int(expiry.timestamp() * 1000)

    # ── BrokerGateway interface ────────────────────────────────────────
    def login(self) -> str:
        return self._get_token()

    def get_current_price(self, symbol: str) -> float | None:
        """Último precio de SimpleFX, o None si el feed no responde.

        El Position Manager gestiona trades que viven en SimpleFX, así que su
        precio de referencia tiene que salir de SimpleFX: Capital.com cotiza el
        mismo instrumento con un offset de decenas de puntos y mezclarlos
        falsea el R-múltiple (y con él los gatillos de breakeven y trailing).
        """
        now = int(time.time())
        try:
            df = self._market_data.get_candles(
                symbol, PRICE_PERIOD_SECONDS, now - PRICE_LOOKBACK_SECONDS, now
            )
        except Exception as e:  # noqa: BLE001 — feed externo, degradación controlada
            log.warning("SimpleFX: sin precio actual para %s: %s", symbol, e)
            return None
        if df is None or df.empty or "close" not in df.columns:
            log.warning("SimpleFX: feed sin cierres para %s", symbol)
            return None
        closes = df["close"].dropna()
        if closes.empty:
            log.warning("SimpleFX: feed sin cierres válidos para %s", symbol)
            return None
        return float(closes.iloc[-1])

    def place_order(
        self,
        symbol: str,
        side: str,
        volume: float,
        entry_price: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> str:
        if self._settings.dry_run:
            log.info("DRY_RUN: simulación de orden %s %s vol=%.2f @ %.2f", side, symbol, volume, entry_price)
            return f"DRY-{int(time.time() * 1000)}"

        token = self._get_token()
        result = _place_order(
            token=token,
            account=self._settings.simple_account,
            symbol=symbol,
            side=side,
            volume=volume,
            entry_price=entry_price,
            stop_loss=stop_loss or entry_price,  # SimpleFX requiere SL
            take_profit=take_profit,
            reality=self._settings.simple_reality,
            expiry_ms=self._pending_expiry_ms(),
        )
        order_id = _extract_order_id(result)
        if order_id is None:
            # La orden YA fue aceptada por el broker: no lanzar (un raise haría
            # que s6 la marque REJECTED y libere el coid → orden duplicada).
            # Se conserva el trade con id "unknown" y se pide reconciliación.
            log.critical(
                "SimpleFX aceptó la orden pero la respuesta no trae id "
                "(reconciliar manualmente): %s", str(result)[:300],
            )
            return "unknown"
        return order_id

    def modify_order(
        self,
        broker_order_id: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> None:
        if self._settings.dry_run:
            log.info("DRY_RUN: simulación modify %s SL=%s TP=%s", broker_order_id, stop_loss, take_profit)
            return
        if not str(broker_order_id).isdigit():
            raise ValueError(
                f"broker_order_id no numérico ({broker_order_id!r}): "
                "orden sin id real, reconciliar manualmente contra el broker"
            )
        token = self._get_token()
        _modify_order(
            token=token,
            account=self._settings.simple_account,
            reality=self._settings.simple_reality,
            id_trade=int(broker_order_id),
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
