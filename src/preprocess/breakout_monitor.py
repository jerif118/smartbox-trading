import time as time_mod
from datetime import datetime, timezone

from broker_api.login import sesion_capitalcom
from broker_api.api_requests import price_capital
from tools_bot.standar_data import standar_data
from tools_bot.time_now import _unix_to_iso
from utils.logger import get_logger

log = get_logger(__name__)

def _check_candles(df, box_high: float, box_low: float) -> dict | None:
    for _, row in df.iterrows():
        close = float(row["close"])
        if close > box_high:
            return {
                "breakout_state": "ABOVE",
                "candle_close": close,
                "signal_time": int(row["time"]),
            }
        elif close < box_low:
            return {
                "breakout_state": "BELOW",
                "candle_close": close,
                "signal_time": int(row["time"]),
            }
    return None


def _fetch_5min(symbol: str, from_unix: int, to_unix: int,
                sec_token: str, cst: str):
    from_str = _unix_to_iso(from_unix)
    to_str = _unix_to_iso(to_unix)
    df = price_capital(symbol, "MINUTE_5", from_str, to_str,
                       "500", sec_token, cst)
    if df is None or df.empty:
        return None
    df = standar_data(df)
    return df.sort_values("time").reset_index(drop=True)


# ── función principal ─────────────────────────────────────────────────

def monitor_breakout(
    symbol: str,
    box_high: float,
    box_low: float,
    box_end_unix: int,
    window_seconds: int = 7200,
    poll_interval: int = 60,
) -> dict | None:
    
    monitor_end = box_end_unix + window_seconds
    now = int(datetime.now(timezone.utc).timestamp())

    security_token, cst = sesion_capitalcom()

    # ── Modo histórico ──────────────────────────────────────────────
    if monitor_end <= now:
        log.info("[monitor] %s: modo HISTÓRICO (%s → %s)",
                 symbol, _unix_to_iso(box_end_unix), _unix_to_iso(monitor_end))

        df = _fetch_5min(symbol, box_end_unix, monitor_end, security_token, cst)
        if df is None:
            log.warning("[monitor] %s: sin velas 5 min en ventana histórica", symbol)
            return None

        result = _check_candles(df, box_high, box_low)
        if result:
            log.info("[monitor] %s: BREAKOUT %s close=%.2f @ %s",
                     symbol, result['breakout_state'], result['candle_close'],
                     _unix_to_iso(result['signal_time']))
        else:
            log.info("[monitor] %s: sin breakout en ventana de 2 h", symbol)
        return result

    # ── Modo live ───────────────────────────────────────────────────
    log.info("[monitor] %s: modo LIVE, vigilando hasta %s (máx %d min)",
             symbol, _unix_to_iso(monitor_end), window_seconds // 60)

    last_checked = box_end_unix
    # Renovar sesión cada 25 min para evitar expiración de token
    token_age = 0
    TOKEN_REFRESH = 25 * 60  # 25 minutos

    while True:
        current = int(datetime.now(timezone.utc).timestamp())
        if current >= monitor_end:
            log.info("[monitor] %s: ventana de 2 h expirada → sin breakout", symbol)
            return None

        # Renovar token si lleva mucho tiempo
        token_age += poll_interval
        if token_age >= TOKEN_REFRESH:
            try:
                security_token, cst = sesion_capitalcom()
                token_age = 0
                log.info("[monitor] %s: sesión Capital.com renovada", symbol)
            except Exception as e:
                log.error("[monitor] %s: error renovando sesión → %s", symbol, e)

        try:
            df = _fetch_5min(symbol, last_checked, current,
                             security_token, cst)
        except Exception as e:
            log.warning("[monitor] %s: error API → %s, reintentando...", symbol, e)
            time_mod.sleep(poll_interval)
            continue

        if df is not None and not df.empty:
            result = _check_candles(df, box_high, box_low)
            if result:
                log.info("[monitor] %s: BREAKOUT %s close=%.2f",
                         symbol, result['breakout_state'], result['candle_close'])
                return result
            # Avanzar puntero para no reprocesar velas
            last_checked = int(df["time"].max())

        remaining = (monitor_end - current) // 60
        log.debug("[monitor] %s: sin breakout | quedan %d min | próximo check en %ds",
                  symbol, remaining, poll_interval)
        time_mod.sleep(poll_interval)
