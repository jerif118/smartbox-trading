import pandas as pd


#price = {"prices":[{"snapshotTime":"2022-02-23T19:00:00","snapshotTimeUTC":"2022-02-24T00:00:00","openPrice":{"bid":4221.5,"ask":4222.2},"closePrice":{"bid":4220.1,"ask":4220.8},"highPrice":{"bid":4226.1,"ask":4226.8},"lowPrice":{"bid":4218.1,"ask":4218.8},"lastTradedVolume":964},{"snapshotTime":"2022-02-23T19:05:00","snapshotTimeUTC":"2022-02-24T00:05:00","openPrice":{"bid":4220.2,"ask":4220.9},"closePrice":{"bid":4217.5,"ask":4218.2},"highPrice":{"bid":4221.6,"ask":4222.3},"lowPrice":{"bid":4215.2,"ask":4215.9},"lastTradedVolume":1061},{"snapshotTime":"2022-02-23T19:10:00","snapshotTimeUTC":"2022-02-24T00:10:00","openPrice":{"bid":4217.6,"ask":4218.3},"closePrice":{"bid":4221.3,"ask":4222.0},"highPrice":{"bid":4224.1,"ask":4224.8},"lowPrice":{"bid":4216.7,"ask":4217.4},"lastTradedVolume":1060},{"snapshotTime":"2022-02-23T19:15:00","snapshotTimeUTC":"2022-02-24T00:15:00","openPrice":{"bid":4221.2,"ask":4221.9},"closePrice":{"bid":4218.0,"ask":4218.7},"highPrice":{"bid":4221.2,"ask":4221.9},"lowPrice":{"bid":4215.6,"ask":4216.3},"lastTradedVolume":1318},{"snapshotTime":"2022-02-23T19:20:00","snapshotTimeUTC":"2022-02-24T00:20:00","openPrice":{"bid":4218.1,"ask":4218.8},"closePrice":{"bid":4217.7,"ask":4218.4},"highPrice":{"bid":4219.1,"ask":4219.8},"lowPrice":{"bid":4214.7,"ask":4215.4},"lastTradedVolume":806},{"snapshotTime":"2022-02-23T19:25:00","snapshotTimeUTC":"2022-02-24T00:25:00","openPrice":{"bid":4217.6,"ask":4218.3},"closePrice":{"bid":4212.3,"ask":4213.0},"highPrice":{"bid":4219.2,"ask":4219.9},"lowPrice":{"bid":4205.6,"ask":4206.3},"lastTradedVolume":1812},{"snapshotTime":"2022-02-23T19:30:00","snapshotTimeUTC":"2022-02-24T00:30:00","openPrice":{"bid":4212.2,"ask":4212.9},"closePrice":{"bid":4205.6,"ask":4206.3},"highPrice":{"bid":4213.8,"ask":4214.9},"lowPrice":{"bid":4204.2,"ask":4204.9},"lastTradedVolume":2608},{"snapshotTime":"2022-02-23T19:35:00","snapshotTimeUTC":"2022-02-24T00:35:00","openPrice":{"bid":4205.5,"ask":4206.2},"closePrice":{"bid":4206.1,"ask":4206.8},"highPrice":{"bid":4211.1,"ask":4211.8},"lowPrice":{"bid":4201.9,"ask":4202.6},"lastTradedVolume":1790},{"snapshotTime":"2022-02-23T19:40:00","snapshotTimeUTC":"2022-02-24T00:40:00","openPrice":{"bid":4206.2,"ask":4206.9},"closePrice":{"bid":4206.6,"ask":4207.3},"highPrice":{"bid":4209.5,"ask":4210.2},"lowPrice":{"bid":4205.1,"ask":4205.8},"lastTradedVolume":1298},{"snapshotTime":"2022-02-23T19:45:00","snapshotTimeUTC":"2022-02-24T00:45:00","openPrice":{"bid":4206.6,"ask":4207.3},"closePrice":{"bid":4206.6,"ask":4207.3},"highPrice":{"bid":4207.2,"ask":4207.9},"lowPrice":{"bid":4204.2,"ask":4204.9},"lastTradedVolume":1000}],"instrumentType":"INDICES","tickSize":0.1,"pipPosition":0}
#price = pd.DataFrame(price['prices'])

def standar_data(df):
    df = df.copy()
    df = df.rename(columns = {"openPrice":"open","closePrice": "close", "highPrice":"high", "lowPrice":"low", "lastTradedVolume": "volume", "snapshotTimeUTC":"time"})
    df["open"] = df["open"].apply(lambda d: (d['bid'] + d['ask'])/2)
    df["close"] = df["close"].apply(lambda d: (d['bid'] + d['ask'])/2)
    df["high"] = df["high"].apply(lambda d: (d['bid'] + d['ask'])/2)
    df["low"] = df["low"].apply(lambda d: (d['bid'] + d['ask'])/2)
    t = pd.to_datetime(df['time'], utc= True)      
    df['time'] = (t.astype('int64')//10**9).astype('int64')
    return df


#price_standar = standar_data(price)
#print(price_standar)
#print(price_standar.dtypes)
