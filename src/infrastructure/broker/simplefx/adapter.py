"""
Adaptador SimpleFX: implementa `BrokerGateway`.

Solo usa endpoints que YA EXISTEN en el código actual (api_requests.py, make_order.py):
- POST /api/v3/auth/key          (login)
- POST /api/v3/trading/orders/pending  (place_order)
- PUT  /api/v3/trading/orders/market   (modify_order)
- GET  /api/v3/candles                 (precio)

NO añade endpoints nuevos (list_positions, get_account, etc.) — la fuente de
verdad para el Position Manager es SQLite, no el broker.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from infrastructure.config.settings import get_settings
from utils.logger import get_logger
from utils.retry import retry

log = get_logger(__name__)

SIMPLE_BASE = "https://rest.simplefx.com"


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
    log.info(
        "SimpleFX: %s %s vol=%.2f @ %.2f SL=%.2f TP=%s",
        side, symbol, volume, entry_price, stop_loss, take_profit,
    )
    resp = requests.post(url, headers=headers, json=body, timeout=20)
    if resp.status_code >= 400:
        log.error("SimpleFX %d: %s", resp.status_code, resp.text)
    resp.raise_for_status()
    return resp.json()


@retry(max_retries=2, backoff=2.0, exceptions=(requests.RequestException,))
def _modify_order(
    token: str,
    account: str,
    reality: str,
    id_trade: int,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> dict[str, Any]:
    url = f"{SIMPLE_BASE}/api/v3/trading/orders/market"
    headers = {"Authorization": f"Bearer {token}"}
    body: dict[str, Any] = {
        "Login": account,
        "Reality": reality,
        "Id": id_trade,
    }
    if take_profit is not None:
        body["TakeProfit"] = take_profit
    if stop_loss is not None:
        body["StopLoss"] = stop_loss
    log.info("Modify %d: SL=%s TP=%s", id_trade, stop_loss, take_profit)
    resp = requests.put(url, headers=headers, json=body, timeout=20)
    resp.raise_for_status()
    return resp.json()


class SimpleFXAdapter:
    """Implementa BrokerGateway usando solo endpoints existentes."""

    def __init__(self, settings=None):
        self._settings = settings or get_settings()
        self._token: str | None = None
        self._token_ts: float = 0.0

    def _get_token(self) -> str:
        if self._token and (time.time() - self._token_ts) < 1500:  # 25 min
            return self._token
        if not self._settings.sf_id or not self._settings.sf_key:
            raise RuntimeError("SimpleFX credentials (ID, KEY) no configuradas")
        self._token = _login(self._settings.sf_id, self._settings.sf_key)
        self._token_ts = time.time()
        log.info("SimpleFX login OK")
        return self._token

    # ── BrokerGateway interface ────────────────────────────────────────
    def login(self) -> str:
        return self._get_token()

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
        )
        # Estructura típica: {"data": {"id": 12345, ...}}
        order_id = (
            result.get("data", {}).get("id")
            or result.get("id")
            or str(result.get("orderId", "unknown"))
        )
        return str(order_id)

    def modify_order(
        self,
        broker_order_id: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> None:
        if self._settings.dry_run:
            log.info("DRY_RUN: simulación modify %s SL=%s TP=%s", broker_order_id, stop_loss, take_profit)
            return
        token = self._get_token()
        _modify_order(
            token=token,
            account=self._settings.simple_account,
            reality=self._settings.simple_reality,
            id_trade=int(broker_order_id),
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
