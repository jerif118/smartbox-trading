import uuid
import requests
from utils.logger import get_logger
from utils.retry import retry

log = get_logger(__name__)

URL = "https://rest.simplefx.com"


@retry(max_retries=2, backoff=2.0, exceptions=(requests.RequestException,))
def orden_pending(
    token: str, account: str, symbol: str, side: str, reality: str,
    volumen: float, entry_price: float, stop_price: float,
    takeprofit_price: float | None = None, expirytime: int | None = None,
):
    url = f"{URL}/api/v3/trading/orders/pending"
    headers = {"Authorization": f"Bearer {token}"}
    request_id = str(uuid.uuid4())
    body = {
        "ActivationPrice": entry_price,
        "Symbol": symbol,
        "Volume": volumen,
        "StopLoss": stop_price,
        "Side": side,
        "RequestId": request_id,
        "Login": account,
        "Reality": reality,
    }
    if takeprofit_price is not None:
        body["TakeProfit"] = takeprofit_price
    if expirytime is not None:
        body["ExpiryTime"] = expirytime

    log.info(
        "Enviando orden %s %s %.2f vol @ %.2f | SL=%.2f | TP=%s | reqId=%s",
        side, symbol, volumen, entry_price, stop_price,
        takeprofit_price, request_id,
    )
    order = requests.post(url, headers=headers, json=body, timeout=20)
    order.raise_for_status()
    log.info("Orden aceptada: %s", order.json())
    return order


@retry(max_retries=2, backoff=2.0, exceptions=(requests.RequestException,))
def change_position(
    token: str, account: str, id_trade: int, reality: str,
    takeprofit_price: float | None = None, stop_price: float | None = None,
):
    url = f"{URL}/api/v3/trading/orders/market"
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "Login": account,
        "Reality": reality,
        "Id": id_trade,
    }
    if takeprofit_price is not None:
        body["TakeProfit"] = takeprofit_price
    if stop_price is not None:
        body["StopLoss"] = stop_price

    log.info("Modificando posición %d | TP=%s | SL=%s", id_trade, takeprofit_price, stop_price)
    order_change = requests.put(url, headers=headers, json=body, timeout=20)
    order_change.raise_for_status()
    log.info("Posición modificada: %s", order_change.json())
    return order_change