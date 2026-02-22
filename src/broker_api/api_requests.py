import requests
import pandas as pd
from utils.logger import get_logger
from utils.retry import retry

log = get_logger(__name__)

SIMPLE_BASE = "https://rest.simplefx.com"
SIMPLE_URL = "https://candles-core.simplefx.com" 
CAPITAL_URL = "https://api-capital.backend-capital.com/"


@retry(max_retries=3, backoff=2.0, exceptions=(requests.RequestException,))
def login_simple(client: str, api_key: str):
    url = f"{SIMPLE_BASE}/api/v3/auth/key"
    body = {
        "clientId": client,
        "clientSecret": api_key,
    }
    resp = requests.post(url, json=body, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    log.info("Login SimpleFX exitoso")
    return data


@retry(max_retries=5, backoff=1.5, exceptions=(requests.RequestException,))
def price_simple(symbol: str, timeframe: int, start: int | None = None, end: int | None = None) -> pd.DataFrame | None:
    url = f"{SIMPLE_URL}/api/v3/candles"
    params = {
        "symbol": symbol,
        "cPeriod": timeframe,
    }
    if start is not None:
        params["timeFrom"] = start
    if end is not None:
        params["timeTo"] = end

    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    df = pd.DataFrame(payload["data"])
    return df


@retry(max_retries=3, backoff=2.0, exceptions=(requests.RequestException,))
def login_capital(email: str, pwd: str, api_key: str) -> dict:
    payload = {
        "identifier": email,
        "password": pwd,
    }
    headers = {
        "X-CAP-API-KEY": api_key,
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"{CAPITAL_URL}/api/v1/session",
        json=payload, headers=headers, timeout=20,
    )
    resp.raise_for_status()

    cst = resp.headers.get("CST")
    xst = resp.headers.get("X-SECURITY-TOKEN")

    if not cst or not xst:
        raise RuntimeError(
            f"Login Capital.com: respuesta sin tokens (CST={cst}, XST={xst})"
        )

    log.info("Login Capital.com exitoso")
    return {"CST": cst, "X-SECURITY-TOKEN": xst}


@retry(max_retries=20, backoff=1.5, initial_delay=0.5, exceptions=(requests.RequestException,))
def price_capital(
    symbol: str, time_resolution: str, from_date: str, to_date: str,
    max_number: str, toke_c: str, cst_token: str,
) -> pd.DataFrame | None:
    url = f"{CAPITAL_URL}/api/v1/prices/{symbol}"
    headers = {
        "X-SECURITY-TOKEN": toke_c,
        "CST": cst_token,
    }
    params = {
        "resolution": time_resolution,
        "max": max_number,
        "from": from_date,
        "to": to_date,
    }

    resp = requests.get(url, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    df = pd.DataFrame(payload["prices"])
    return df
