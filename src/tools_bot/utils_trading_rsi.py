#import os
import pandas as pd
import pandas_ta as ta
#from dotenv import load_dotenv
#from api_requests import api_price
#load_dotenv()
#SYMBOL = os.getenv("SYMBOL")

#price = api_price(SYMBOL,300)
def rsi(df):
    rsi = ta.rsi(df["close"], length=14)
    rsi = rsi.dropna()
    return rsi

#rsi_def = rsi(price)

#print(rsi_def)

