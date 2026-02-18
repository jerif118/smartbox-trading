import pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Lima")
FECHA_NOW = pd.Timestamp.now(TZ).strftime("%Y-%m-%d")

def unix_time_now( start_h: str, end_h:str, tz=TZ):
    start= pd.Timestamp(f"{FECHA_NOW} {start_h}", tz=tz)
    end = pd.Timestamp(f"{FECHA_NOW} {end_h}",tz=tz)
    return (int(start.timestamp()),int(end.timestamp()))

def unix_time(start_:str, end_: str):
    start = pd.to_datetime(start_, utc=True)
    end = pd.to_datetime(end_, utc=True)
    return(int(start.timestamp()),int(end.timestamp()))

def _unix_to_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def check_time():
    return datetime.now().time()


def utc_time(ts: int):
    #return datetime.fromtimestamp(ts,tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return datetime.fromtimestamp(ts,tz=timezone.utc)