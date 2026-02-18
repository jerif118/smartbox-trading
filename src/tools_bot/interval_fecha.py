import pandas as pd
#from api_requests import price_capital
from tools_bot.time_now import utc_time

star_time = 1764130200
end_time = 1767203400 

#time = 60 
#block = 500*60
#cont = 0
#price = []
def date_ranges(star_time: int, end_time: int, time = 60, values = 500 ):
    rangos = []
    block = values*time
    while star_time < end_time: 
        interval = min(star_time+ block,end_time)
        #cont+=1
        rangos.append((utc_time(star_time), utc_time(interval)))
        #print(interval,cont)
        star_time = interval
    #print("cantidad inicial:",len(rangos))
    #print(rangos)
    #for x, y in rangos:
    #    if x.weekday() < 5 or y.weekday() < 5 :
    #        cont+=1
    #print(len(rangos))
            
    rangos = [(x, y)  for x, y in rangos if x.weekday() < 5 or y.weekday() < 5]
    rangos = [(x.strftime("%Y-%m-%dT%H:%M:%S"), y.strftime("%Y-%m-%dT%H:%M:%S")) for x, y in rangos]
    #print(len(rangos))
    return rangos
#for from_ ,to_ in rangos:
    #cont+=1
    #p = price_capital("US500","MINUTE",from_,to_,1000,"CUkgno0sPOwCPM75VZRBn6li4hT5LeV","e7NSWhXxFyR7zo8z0L4ePF2Y")
    #price.append(p)
#print(from_,to_)

#print(price)

#df = pd.concat([df_ for df_ in price if df_ is not None ], ignore_index=True)

#df_unico = df.drop_duplicates(subset=['snapshotTimeUTC'])
#df_unico

#from standar_data import standar_data
#df_normalice = standar_data(df_unico)





