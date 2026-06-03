from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd


def is_trading_day(date_str: str) -> bool:
    dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
    return dt.weekday() < 5


def is_market_open(dt: datetime, market_tz: str = "America/New_York") -> bool:
    market = ZoneInfo(market_tz)
    market_time = dt.astimezone(market).time()
    market_open = time(9, 30)
    market_close = time(16, 0)
    return market_open <= market_time <= market_close


def validate_date_range(start_dt: datetime, end_dt: datetime, tz_str: str) -> tuple[bool, datetime, datetime]:
    tz = ZoneInfo(tz_str)
    start_dt = start_dt.astimezone(tz)
    end_dt = end_dt.astimezone(tz)

    if end_dt <= start_dt:
        return False, start_dt, end_dt

    if not is_trading_day(start_dt.strftime("%Y-%m-%d")) or not is_trading_day(end_dt.strftime("%Y-%m-%d")):
        return False, start_dt, end_dt

    return True, start_dt, end_dt


def filter_market_hours(df: pd.DataFrame, market_tz: str = "America/New_York") -> pd.DataFrame:
    if df is None or df.empty or "time" not in df.columns:
        return df

    df = df.copy()
    df["dt"] = pd.to_datetime(df["time"], unit="s", utc=True)
    mask = df["dt"].apply(lambda x: is_market_open(x, market_tz))
    filtered = df[mask].copy()
    filtered = filtered.drop(columns=["dt"])
    return filtered


def date_ranges(start_time: int, end_time: int, time=60, values=500, tz_str: str = "America/New_York"):
    rangos = []
    block = values * time
    while start_time < end_time:
        interval = min(start_time + block, end_time)
        rangos.append((start_time, interval))
        start_time = interval

    rangos = [
        (start_ts, end_ts)
        for start_ts, end_ts in rangos
        if _range_has_trading_hours(start_ts, end_ts, tz_str)
    ]

    rangos = [
        (from_ts, to_ts)
        for from_ts, to_ts in rangos
        if _is_valid_trading_range(from_ts, to_ts, tz_str)
    ]

    return rangos


def _unix_to_dt(ts: int, tz_str: str = "UTC") -> datetime:
    tz = ZoneInfo(tz_str)
    return datetime.fromtimestamp(ts, tz=tz)


def _range_has_trading_hours(from_ts: int, to_ts: int, tz_str: str = "America/New_York") -> bool:
    market_tz = ZoneInfo(tz_str)
    from_dt = datetime.fromtimestamp(from_ts, tz=market_tz)
    to_dt = datetime.fromtimestamp(to_ts, tz=market_tz)

    if from_dt.weekday() >= 5 and to_dt.weekday() >= 5:
        return False

    return True


def _is_valid_trading_range(from_ts: int, to_ts: int, tz_str: str = "America/New_York") -> bool:
    market_tz = ZoneInfo(tz_str)
    from_dt = datetime.fromtimestamp(from_ts, tz=market_tz)
    to_dt = datetime.fromtimestamp(to_ts, tz=market_tz)

    open_time = time(9, 30)
    close_time = time(16, 0)

    from_market = from_dt.time()
    to_market = to_dt.time()

    in_range = from_market < close_time and to_market > open_time
    is_weekday = from_dt.weekday() < 5 and to_dt.weekday() < 5

    return in_range and is_weekday





