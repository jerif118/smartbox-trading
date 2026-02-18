import os
import pandas as pd
from dataclasses import dataclass
from broker_api.login import sesion_capitalcom
from broker_api.api_requests import price_capital, price_simple
from tools_bot.interval_fecha import date_ranges
from tools_bot.time_now import unix_time
from tools_bot.box import box_strategy
from tools_bot.utils_trading_rsi import rsi
from tools_bot.utils_trading_vp import vp_features_compose
from tools_bot.standar_data import standar_data
from utils.logger import get_logger
from dotenv import load_dotenv

load_dotenv()
log = get_logger(__name__)

# Valores por defecto desde .env (se usan si no se pasan argumentos)
DEFAULT_SYMBOL = os.getenv("SYMBOL", "US500")
DEFAULT_TIMEFRAME = os.getenv("TIMEFRAME", "MINUTE")
DEFAULT_START = os.getenv("START_VP")
DEFAULT_END = os.getenv("END_VP")
DEFAULT_BOX_DATE = os.getenv("BOX_DATE")  # "YYYY-MM-DD", si no se pasa usa end_date
DEFAULT_BOX_START = os.getenv("BOX_START", "08:00")
DEFAULT_BOX_END = os.getenv("BOX_END", "09:55")

DATA_LOADER_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data_loader"
)
VP_LOADER_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data_loader", "vp"
)

# Mapeo de timeframe Capital.com -> segundos por vela (para date_ranges)
TIMEFRAME_SECONDS = {
    "MINUTE":    60,
    "MINUTE_5":  300,
    "MINUTE_15": 900,
    "MINUTE_30": 1800,
    "HOUR":      3600,
    "HOUR_4":    14400,
    "DAY":       86400,
    "WEEK":      604800,
}


@dataclass
class PreprocessResult:
    symbols: str
    tf: str
    procesed_path: str
    last_ts: str
    features: dict


def loader_file(symb: str, start: int, end: int, path: str = DATA_LOADER_PATH):
    """
    Carga el parquet de un símbolo y retorna solo las filas dentro de [start, end].
    Retorna (df_in_range, df_full) o (None, None) si no existe archivo.
    """
    file = os.path.join(path, f"{symb}.parquet")
    if not os.path.exists(file):
        return None, None
    try:
        df = pd.read_parquet(file, engine="pyarrow")
    except Exception:
        return None, None
    if "time" not in df.columns:
        return None, None
    df = df.sort_values("time").reset_index(drop=True)
    df_range = df[(df["time"] >= start) & (df["time"] <= end)].copy()
    return df_range, df


def save_parquet(df: pd.DataFrame, symb: str, path: str = DATA_LOADER_PATH):
    """Guarda el DataFrame procesado como parquet para reutilización futura."""
    os.makedirs(path, exist_ok=True)
    file = os.path.join(path, f"{symb}.parquet")
    df.to_parquet(file, engine="pyarrow", index=False)
    return file


def fetch_from_api(symbol, timeframe, start_unix, end_unix, max_candles):
    """Descarga velas desde la API de Capital.com en el rango unix dado."""
    security_token, cst = sesion_capitalcom()
    tf_seconds = TIMEFRAME_SECONDS.get(timeframe, 60)
    intervalos = date_ranges(start_unix, end_unix, time=tf_seconds)

    dataframe = []
    for from_, to_ in intervalos:
        price = price_capital(
            symbol, timeframe, from_, to_, max_candles, security_token, cst
        )
        dataframe.append(price)

    valid = [df_ for df_ in dataframe if df_ is not None]
    if not valid:
        return pd.DataFrame()

    df = pd.concat(valid, ignore_index=True)
    df_norm = standar_data(df)
    return df_norm.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)


def merge_and_deduplicate(old_df: pd.DataFrame | None, new_df: pd.DataFrame) -> pd.DataFrame:
    """Combina datos existentes con nuevos, elimina duplicados y ordena por time."""
    if old_df is not None and not old_df.empty:
        combined = pd.concat([old_df, new_df], ignore_index=True)
    else:
        combined = new_df.copy()
    return combined.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)


def load_or_fetch_vp(symbol: str, start_unix: int, end_unix: int, max_candles: int = 1000) -> pd.DataFrame:
    """
    Carga o descarga datos de 1 minuto exclusivos para Volume Profile.
    Usa su propio parquet en VP_LOADER_PATH con el mismo flujo de caché
    (detecta huecos al inicio/final y descarga solo lo faltante).
    Siempre timeframe=MINUTE (60s) independiente del timeframe principal.
    """
    vp_symb = f"{symbol}_vp"  # nombre separado para no colisionar

    df_in_range, df_full = loader_file(symb=vp_symb, start=start_unix, end=end_unix, path=VP_LOADER_PATH)

    if df_in_range is not None and not df_in_range.empty:
        cached_min = int(df_in_range["time"].min())
        cached_max = int(df_in_range["time"].max())

        missing = []
        if cached_min > start_unix:
            missing.append((start_unix, cached_min - 1))
        if cached_max < end_unix:
            missing.append((cached_max + 1, end_unix))

        if not missing:
            log.info("[vp-cache] %s: rango completo (%d filas 1min)", symbol, len(df_in_range))
            return df_in_range

        log.info("[vp-cache] %s: %d filas, descargando %d rango(s)",
                 symbol, len(df_in_range), len(missing))
        parts = [df_in_range]
        for gap_start, gap_end in missing:
            log.debug("  -> vp descargando ts=%d..%d", gap_start, gap_end)
            df_gap = fetch_from_api(symbol, "MINUTE", gap_start, gap_end, max_candles)
            if not df_gap.empty:
                parts.append(df_gap)

        df_vp = merge_and_deduplicate(None, pd.concat(parts, ignore_index=True))
        df_full_updated = merge_and_deduplicate(df_full, df_vp)
        save_parquet(df_full_updated, vp_symb, path=VP_LOADER_PATH)
        log.info("[vp-update] %s: parquet VP actualizado -> %d filas", symbol, len(df_full_updated))

        return df_vp[
            (df_vp["time"] >= start_unix) & (df_vp["time"] <= end_unix)
        ].reset_index(drop=True)

    # Sin caché o parquet vacío en rango -> descargar completo
    if df_full is not None:
        log.info("[vp-cache] %s: parquet VP existe pero sin datos en rango", symbol)
    else:
        log.info("[vp-api] %s: sin parquet VP, descargando completo 1min", symbol)

    df_new = fetch_from_api(symbol, "MINUTE", start_unix, end_unix, max_candles)
    if df_new.empty:
        log.warning("[vp-warn] %s: no se obtuvieron datos VP de 1min", symbol)
        return pd.DataFrame()

    if df_full is not None:
        df_full_updated = merge_and_deduplicate(df_full, df_new)
    else:
        df_full_updated = df_new

    save_parquet(df_full_updated, vp_symb, path=VP_LOADER_PATH)
    log.info("[vp-save] %s: %d filas VP -> %s/%s.parquet",
             symbol, len(df_full_updated), VP_LOADER_PATH, vp_symb)
    return df_new


def preprocess_data(
    symbol: str | None = None,
    timeframe: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    box_date: str | None = None,
    box_start_hour: str | None = None,
    box_end_hour: str | None = None,
    max_candles: int = 1000,
    use_cache: bool = True,
):
    """
    Descarga, normaliza y calcula features para cualquier símbolo y rango.

    Parámetros
    ----------
    symbol         : str  – símbolo del instrumento (ej. "US500", "EURUSD").
                            Por defecto usa SYMBOL del .env.
    timeframe      : str  – resolución Capital.com (MINUTE, MINUTE_5, HOUR, DAY …).
                            Por defecto usa TIMEFRAME del .env.
    start_date     : str  – fecha/hora inicio "YYYY-MM-DD" o "YYYY-MM-DDTHH:MM:SS".
                            Por defecto usa START_VP del .env.
    end_date       : str  – fecha/hora fin. Por defecto usa END_VP del .env.
    box_date       : str  – fecha de la caja "YYYY-MM-DD". Por defecto usa end_date.
    box_start_hour : str  – hora inicio de la caja ("HH:MM"). Por defecto "08:00".
    box_end_hour   : str  – hora fin de la caja ("HH:MM"). Por defecto "09:55".
    max_candles    : int  – máximo de velas por petición (API limit).
    use_cache      : bool – si True, intenta cargar datos previos de parquet.

    Retorna
    -------
    PreprocessResult con la ruta del parquet y las features calculadas.
    """
    # ── Resolver valores por defecto ──────────────────────────────────
    symbol = symbol or DEFAULT_SYMBOL
    timeframe = timeframe or DEFAULT_TIMEFRAME
    start_date = start_date or DEFAULT_START
    end_date = end_date or DEFAULT_END

    box_start_hour = box_start_hour or DEFAULT_BOX_START
    box_end_hour = box_end_hour or DEFAULT_BOX_END
    # box_date: si no se pasa, usa la parte fecha de end_date ("YYYY-MM-DD")
    box_date = box_date or DEFAULT_BOX_DATE or (end_date[:10] if end_date else None)

    if not symbol or not start_date or not end_date:
        raise ValueError("Se requieren symbol, start_date y end_date.")

    # ── Convertir fechas a unix timestamps ────────────────────────────
    start_unix, end_unix = unix_time(start_date, end_date)

    # ── Flujo de caché + descarga de rangos faltantes ─────────────────
    df_unico = None
    needs_save = False

    if use_cache:
        df_in_range, df_full = loader_file(symb=symbol, start=start_unix, end=end_unix)

        if df_in_range is not None and not df_in_range.empty:
            cached_min = int(df_in_range["time"].min())
            cached_max = int(df_in_range["time"].max())

            missing_ranges = []  # lista de (from_ts, to_ts) que faltan

            # ¿Falta al inicio? (el caché empieza después de lo pedido)
            if cached_min > start_unix:
                missing_ranges.append((start_unix, cached_min - 1))

            # ¿Falta al final? (el caché termina antes de lo pedido)
            if cached_max < end_unix:
                missing_ranges.append((cached_max + 1, end_unix))

            if not missing_ranges:
                # El caché cubre todo el rango
                log.info("[cache] %s: rango completo (%d filas)", symbol, len(df_in_range))
                df_unico = df_in_range
            else:
                # Descargar solo los rangos faltantes
                log.info("[cache] %s: %d filas en caché, descargando %d rango(s) faltante(s)",
                         symbol, len(df_in_range), len(missing_ranges))
                security_token, cst = None, None
                parts = [df_in_range]

                for gap_start, gap_end in missing_ranges:
                    log.debug("  -> descargando ts=%d..%d", gap_start, gap_end)
                    df_gap = fetch_from_api(symbol, timeframe, gap_start, gap_end, max_candles)
                    if not df_gap.empty:
                        parts.append(df_gap)

                # Unir caché parcial + datos nuevos
                df_unico = merge_and_deduplicate(None, pd.concat(parts, ignore_index=True))

                # Actualizar el parquet completo (datos viejos + nuevos)
                df_full_updated = merge_and_deduplicate(df_full, df_unico)
                save_parquet(df_full_updated, symbol)
                log.info("[update] %s: parquet actualizado -> %d filas totales",
                         symbol, len(df_full_updated))
                needs_save = False  # ya se guardó

                # Filtrar al rango solicitado
                df_unico = df_unico[
                    (df_unico["time"] >= start_unix) & (df_unico["time"] <= end_unix)
                ].reset_index(drop=True)

        elif df_full is not None:
            # Existe parquet pero no tiene datos en el rango → descargar todo el rango
            log.info("[cache] %s: parquet existe pero sin datos en rango, descargando completo", symbol)
            df_new = fetch_from_api(symbol, timeframe, start_unix, end_unix, max_candles)
            if not df_new.empty:
                df_full_updated = merge_and_deduplicate(df_full, df_new)
                save_parquet(df_full_updated, symbol)
                log.info("[update] %s: parquet actualizado -> %d filas totales",
                         symbol, len(df_full_updated))
                df_unico = df_new
            else:
                df_unico = None

    # ── Descarga completa si no hay caché ─────────────────────────────
    if df_unico is None:
        log.info("[api] %s: descargando rango completo ts=%d..%d", symbol, start_unix, end_unix)
        df_new = fetch_from_api(symbol, timeframe, start_unix, end_unix, max_candles)
        if df_new.empty:
            raise RuntimeError(f"No se obtuvieron datos de la API para {symbol}")
        df_unico = df_new
        needs_save = True

    # ── Guardar parquet (solo primera carga sin caché previo) ─────────
    if needs_save:
        saved_path = save_parquet(df_unico, symbol)
        log.info("[save] %s: %d filas -> %s", symbol, len(df_unico), saved_path)
    else:
        saved_path = os.path.join(DATA_LOADER_PATH, f"{symbol}.parquet")

    # ── Calcular features ─────────────────────────────────────────────
    rsi_series = rsi(df_unico)
    last_rsi = float(rsi_series.iloc[-1]) if not rsi_series.empty else None

    # RSI con timestamps y precio close alineados para detectar divergencias
    rsi_points = []
    if not rsi_series.empty:
        rsi_df = pd.DataFrame({
            "time": df_unico.loc[rsi_series.index, "time"].values,
            "close": df_unico.loc[rsi_series.index, "close"].values,
            "rsi": rsi_series.values,
        })

        # Detectar picos y valles del RSI (máximos/mínimos locales)
        rsi_vals = rsi_df["rsi"].values
        for i in range(1, len(rsi_vals) - 1):
            is_peak = rsi_vals[i] > rsi_vals[i - 1] and rsi_vals[i] > rsi_vals[i + 1]
            is_valley = rsi_vals[i] < rsi_vals[i - 1] and rsi_vals[i] < rsi_vals[i + 1]
            if is_peak or is_valley:
                rsi_points.append({
                    "time": int(rsi_df.iloc[i]["time"]),
                    "close": float(rsi_df.iloc[i]["close"]),
                    "rsi": float(rsi_df.iloc[i]["rsi"]),
                    "type": "peak" if is_peak else "valley",
                })

    # ── Box strategy (ventana horaria variable) ───────────────────────
    box_from, box_to = unix_time(
        f"{box_date}T{box_start_hour}:00",
        f"{box_date}T{box_end_hour}:00",
    )

    # Box desde datos del parquet (Capital.com)
    high_price, low_price, amplitud = box_strategy(df_unico, box_from, box_to)

    if amplitud is not None and amplitud > 1:
        log.warning("[box] %s: amplitud=%.2f%% > 1%% -> operativa nula", symbol, amplitud)
        return None

    # Box desde SimpleFX (price_simple) para el mismo rango horario
    df_simple = price_simple(symbol, 300, box_from, box_to)
    if df_simple is not None and not df_simple.empty:
        high_simple, low_simple, amplitud_simple = box_strategy(df_simple, box_from, box_to)
    else:
        high_simple, low_simple, amplitud_simple = None, None, None

    # ── Volume Profile (datos de 1 minuto, parquet paralelo) ─────────
    df_vp_1min = load_or_fetch_vp(symbol, start_unix, end_unix, max_candles)
    if not df_vp_1min.empty:
        vp_data = vp_features_compose(df_vp_1min, start_date)
    else:
        vp_data = None

    last_ts = int(df_unico["time"].iloc[-1]) if not df_unico.empty else None

    features = {
        "rsi_last": last_rsi,
        "rsi_points": rsi_points,
        "box": {
            "hour_range": f"{box_start_hour}-{box_end_hour}",
            "high": high_price,
            "low": low_price,
            "amplitud": amplitud,
            "high_simple": high_simple,
            "low_simple": low_simple,
            "amplitud_simple": amplitud_simple,
        },
        "volume_profile": vp_data,
    }

    log.info("[features] %s | RSI=%s | Box=(%s-%s) | BoxSimple=(%s-%s) | VP=%s",
             symbol, last_rsi, low_price, high_price, low_simple, high_simple,
             'OK' if vp_data else 'None')

    return PreprocessResult(
        symbols=symbol,
        tf=timeframe,
        procesed_path=saved_path,
        last_ts=str(last_ts),
        features=features,
    )
