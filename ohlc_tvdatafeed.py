import os
import pandas as pd
from tvDatafeed import TvDatafeed, Interval

# --- ส่วนเตรียม Directory ---
OHLC_PATH = r"C:\david\ohlc"

# กำหนดช่วงเวลาที่ต้องการวนลูปตามความต้องการของคุณ
interval_map = {
    "1m": Interval.in_1_minute,
    "5m": Interval.in_5_minute,
    "15m": Interval.in_15_minute,
    "1h": Interval.in_1_hour,
    "1d": Interval.in_daily,
    "1w": Interval.in_weekly
}

def fetch_tvdatafeed(symbol, exchange, timeframe, n_bars=10000):
    if not os.path.exists(OHLC_PATH):
        os.makedirs(OHLC_PATH)

    try:
        # tv = TvDatafeed("weivorakan", "KxTi4p-9-qvE36Z", chromedriver_path=None)
        tv = TvDatafeed()

        if timeframe == "all":
            for label, interval_obj in interval_map.items():
                print(f"Fetching Timeframe: {label}...")
            
                df = tv.get_hist(symbol=symbol, exchange=exchange, 
                                interval=interval_obj, n_bars=n_bars)
            
                if df is not None and not df.empty:
                    # จัดการเรื่อง Timezone โดยบังคับให้ Pandas มองว่าข้อมูลดิบคือ UTC (โดยไม่สนว่าเดิมจะเป็นอะไร)
                    # หากเดิมมี tz อยู่แล้วให้ใช้ tz_convert ถ้าไม่มีให้ใช้ tz_localize
                    if df.index.tz is None:
                        df.index = df.index.tz_localize('UTC')
                    else:
                        df.index = df.index.tz_convert('UTC')
                
                    # แปลงค่าเวลาจาก UTC ไปเป็น Asia/Bangkok (+7)
                    df.index = df.index.tz_convert('Asia/Bangkok')

                    # ถอด Timezone ออก (Make it Naive) เพื่อให้เหลือแค่ตัวเลขเวลาไทย
                    # ขั้นตอนนี้สำคัญมาก เพื่อให้ David อ่านง่ายและไม่สับสนกับระบบ Timezone ของคอมพิวเตอร์
                    df.index = df.index.tz_localize(None)

                    # ลบคอลัมน์ที่ไม่จำเป็นออก (เช่น symbol) เพื่อให้เหลือแค่ OHLC
                    if 'symbol' in df.columns:
                        df = df.drop(columns=['symbol'])
                        df = df.drop(columns=['volume'])
                
                    # เรียงลำดับจากเก่าไปใหม่
                    df = df.sort_index()

                    file_name = f"{symbol}_{exchange}_{label}.csv"
                    file_path = os.path.join(OHLC_PATH, file_name)
                    df.to_csv(file_path)
                    print(f"--- Successfully saved: {file_path} ({len(df)} bar(s)) ---")
                else:
                    print(f"!!! Unable to fetch {label} !!!")
        else:
            label = timeframe
            interval_obj = interval_map.get(label)
            if interval_obj:
                print(f"Fetching Timeframe: {label}...")

                df = tv.get_hist(symbol=symbol, exchange=exchange, 
                                interval=interval_obj, n_bars=n_bars)
            
                if df is not None and not df.empty:
                    # จัดการเรื่อง Timezone โดยบังคับให้ Pandas มองว่าข้อมูลดิบคือ UTC (โดยไม่สนว่าเดิมจะเป็นอะไร)
                    # หากเดิมมี tz อยู่แล้วให้ใช้ tz_convert ถ้าไม่มีให้ใช้ tz_localize
                    if df.index.tz is None:
                        df.index = df.index.tz_localize('UTC')
                    else:
                        df.index = df.index.tz_convert('UTC')
                
                    # แปลงค่าเวลาจาก UTC ไปเป็น Asia/Bangkok (+7)
                    df.index = df.index.tz_convert('Asia/Bangkok')

                    # ถอด Timezone ออก (Make it Naive) เพื่อให้เหลือแค่ตัวเลขเวลาไทย
                    # ขั้นตอนนี้สำคัญมาก เพื่อให้ David อ่านง่ายและไม่สับสนกับระบบ Timezone ของคอมพิวเตอร์
                    df.index = df.index.tz_localize(None)

                    # ลบคอลัมน์ที่ไม่จำเป็นออก (เช่น symbol) เพื่อให้เหลือแค่ OHLC
                    if 'symbol' in df.columns:
                        df = df.drop(columns=['symbol'])
                        df = df.drop(columns=['volume'])
                
                    # เรียงลำดับจากเก่าไปใหม่
                    df = df.sort_index()

                    file_name = f"{symbol}_{exchange}_{label}.csv"
                    file_path = os.path.join(OHLC_PATH, file_name)
                    df.to_csv(file_path)
                    print(f"--- Successfully saved: {file_path} ({len(df)} bar(s)) ---")
                else:
                    print(f"!!! Unable to fetch {label} !!!")
            else:
                print(f"!!! No specific timeframe {label} !!!")

    except Exception as e:
        print(f"!!! Error in server connection: {e} !!!")


def main():
    print("--- Importing bulk data from TradingView ---")

    symbol = input("Symbol: ").strip().upper()
    exchange = input("Exchange: ").strip().upper()
    timeframe = input("Timeframe (1m/5m/15m/1h/1d/1w/all): ")
    bars_number = input("Number of bars: ").strip()
    
    fetch_tvdatafeed(symbol, exchange, timeframe, bars_number)


if __name__ == "__main__":
    main()