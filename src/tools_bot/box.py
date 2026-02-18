
def box_strategy(df, timefrom, timeto):
    price = df[(df["time"] >= timefrom) & (df["time"] <= timeto)].copy()
    if price.empty:
        return None, None, None
    high_price = max(price["high"])
    low_price = min(price["low"])
    if low_price == 0:
        return high_price, low_price, None
    amplitud = round((high_price - low_price) / low_price * 100, 2)
    return high_price, low_price, amplitud


