import pandas as pd
import os
import pytz
from datetime import datetime

# --- Configuration ---
OHLC_PATH = r"C:\Users\weivo\OneDrive\เอกสาร\Price Action Trading\david\ohlc"  # โฟลเดอร์เก็บข้อมูลหลัก
SUPPORTED_TIMEFRAMES = ("1m", "5m", "15m", "1h", "1d", "1w")

class OHLC_DB:
    def __init__(self, symbol, exchange, timeframe, directory=OHLC_PATH):
        self.directory = directory
        if not os.path.exists(self.directory):
            os.makedirs(self.directory)
            
        self.file_path = f"{directory}\\{symbol}-{exchange}-{timeframe}.csv"
        self.timeframe_map = {
            "1m": 1, "5m": 5, "15m": 15, "1h": 60, "1d": 1440, "1w": 10080
        }
        self.interval_min = self.timeframe_map.get(timeframe, 1)

    def load_data(self):
        """โหลดข้อมูลจาก CSV เดิมที่มีอยู่"""
        if not os.path.exists(self.file_path):
            return pd.DataFrame()
        
        df = pd.read_csv(self.file_path, index_col=0)
        df = normalize_index(df)
        
        return df.sort_index()

    def check_gap(self):
        """ตรวจสอบความต่อเนื่องของข้อมูล"""
        df = self.load_data()
        if df.empty:
            print(f"--- [!] Database {self.file_path} is empty. ---")
            return

        last_bar_time = df.index[-1]
        now = datetime.now()
        
        diff = now - last_bar_time
        missing_minutes = int(diff.total_seconds() / 60)
        missing_bars = missing_minutes // self.interval_min

        print(f"\n--- Database Status: {os.path.basename(self.file_path)} ---")
        print(f"Last bar in DB : {last_bar_time}")
        print(f"Current time    : {now.strftime('%Y-%m-%d %H:%M:%S')}")
        
        if missing_bars > 1:
            print(f"!!! GAP DETECTED: Missing approx {missing_bars} bars !!!")
            print(f"Suggestion: Press Alt+G in TradingView to fetch data from {last_bar_time}")
        else:
            print(">>> Database is up-to-date.")

    def merge_and_save(self, new_data_df):
        """รวมข้อมูลใหม่เข้าฐานข้อมูล และกำจัดแถวที่ซ้ำกัน (Deduplication)"""
        if new_data_df.empty:
            print("No new data to merge.")
            return
        
        old_df = self.load_data()

        # รวมข้อมูลเก่าและใหม่
        combined_df = pd.concat([old_df, new_data_df])
        
        # กำจัดข้อมูลซ้ำที่ Index (datetime) โดยยึดข้อมูลใหม่ล่าสุด (last)
        combined_df = combined_df[~combined_df.index.duplicated(keep='last')]
        combined_df = combined_df.sort_index()
        combined_df.index = combined_df.index.tz_localize(pytz.timezone("Asia/Bangkok"))

        # บันทึกลงไฟล์
        combined_df.to_csv(self.file_path)
        print(f"Successfully merged. Total bars in DB: {len(combined_df)}")
        return combined_df


def normalize_index(df):
    if df.empty:
        return df

    # แปลงเป็น datetime
    df.index = pd.to_datetime(df.index, errors='coerce')
    df = df[~df.index.isna()]

    # ตัด ms → align candle
    df.index = df.index.floor('min')

    # handle timezone
    if df.index.tz is not None:
        bangkok_tz = pytz.timezone("Asia/Bangkok")
        df.index = df.index.tz_convert(bangkok_tz)
        df.index = df.index.tz_localize(None)

    return df


def parse_base_filename(base_filename):
    """Return symbol, exchange, timeframe from `<symbol>-<exchange>-<timeframe>`."""
    try:
        symbol_exchange, timeframe = base_filename.rsplit("-", 1)
        symbol, exchange = symbol_exchange.split("-", 1)
    except ValueError:
        return None

    timeframe = timeframe.lower()
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return None

    return symbol, exchange, timeframe


def find_sync_pairs(directory=OHLC_PATH):
    """Find matching `pine-logs-xxxx.csv` and `xxxx.csv` file pairs."""
    sync_pairs = []

    for filename in os.listdir(directory):
        if not filename.startswith("pine-logs-") or not filename.endswith(".csv"):
            continue

        base_filename = filename[len("pine-logs-"):-len(".csv")]
        parsed = parse_base_filename(base_filename)
        if parsed is None:
            print(f"Skipping unsupported or invalid log filename: {filename}")
            continue

        symbol, exchange, timeframe = parsed
        db_filename = f"{base_filename}.csv"
        db_path = os.path.join(directory, db_filename)
        log_path = os.path.join(directory, filename)

        if not os.path.exists(db_path):
            print(f"Skipping {filename}: matching DB file not found ({db_filename})")
            continue

        sync_pairs.append(
            {
                "symbol": symbol,
                "exchange": exchange,
                "timeframe": timeframe,
                "db_filename": db_filename,
                "log_filename": filename,
                "log_path": log_path,
            }
        )

    timeframe_order = {timeframe: index for index, timeframe in enumerate(SUPPORTED_TIMEFRAMES)}
    return sorted(
        sync_pairs,
        key=lambda item: (
            item["symbol"],
            item["exchange"],
            timeframe_order[item["timeframe"]],
        ),
    )


def find_missing_pine_logs(directory=OHLC_PATH):
    """Find DB files that do not yet have matching `pine-logs-xxxx.csv` files."""
    missing_logs = []

    for filename in os.listdir(directory):
        if filename.startswith("pine-logs-") or not filename.endswith(".csv"):
            continue

        base_filename = filename[:-len(".csv")]
        parsed = parse_base_filename(base_filename)
        if parsed is None:
            continue

        symbol, exchange, timeframe = parsed
        log_filename = f"pine-logs-{base_filename}.csv"
        log_path = os.path.join(directory, log_filename)

        if not os.path.exists(log_path):
            missing_logs.append(
                {
                    "symbol": symbol,
                    "exchange": exchange,
                    "timeframe": timeframe,
                    "db_filename": filename,
                    "log_filename": log_filename,
                }
            )

    timeframe_order = {timeframe: index for index, timeframe in enumerate(SUPPORTED_TIMEFRAMES)}
    return sorted(
        missing_logs,
        key=lambda item: (
            item["symbol"],
            item["exchange"],
            timeframe_order[item["timeframe"]],
        ),
    )


def print_missing_pine_log_report(missing_logs):
    if not missing_logs:
        print("\nAll DB files already have matching Pine Logs files.")
        return

    print("\n--- Missing Pine Logs Report ---")
    current_key = None
    missing_timeframes = []

    for item in missing_logs:
        key = (item["symbol"], item["exchange"])
        if key != current_key and current_key is not None:
            symbol, exchange = current_key
            print(f"{symbol}-{exchange}: missing {', '.join(missing_timeframes)}")
            missing_timeframes = []

        current_key = key
        missing_timeframes.append(item["timeframe"])

    if current_key is not None:
        symbol, exchange = current_key
        print(f"{symbol}-{exchange}: missing {', '.join(missing_timeframes)}")


def sync_pair(symbol, exchange, timeframe, log_filename, log_path, directory=OHLC_PATH):
    db = OHLC_DB(symbol, exchange, timeframe, directory=directory)

    print(f"\n--- Starting Sync Process for {symbol}-{exchange}-{timeframe} ---")
    db.check_gap()

    print("\nScanning Pine Logs for new data...")
    print(f"Reading from Log: {log_filename}")

    new_data = pd.read_csv(log_path, index_col=0, parse_dates=True)
    new_data = normalize_index(new_data)

    if new_data.empty:
        print("No new data to merge.")
        return False

    print(f"Found {len(new_data)} bars in logs.")
    db.merge_and_save(new_data)
    os.remove(log_path)
    db.check_gap()
    return True


def main():
    print("--- Update bulk OHLC data from Pine Logs ---")
    sync_pairs = find_sync_pairs()
    missing_logs = find_missing_pine_logs()
    print_missing_pine_log_report(missing_logs)

    if not sync_pairs:
        print("No matching Pine Logs / DB file pairs found.")
        return

    print(f"Found {len(sync_pairs)} matching file pair(s).")
    synced_count = 0

    for pair in sync_pairs:
        synced = sync_pair(
            pair["symbol"],
            pair["exchange"],
            pair["timeframe"],
            pair["log_filename"],
            pair["log_path"],
        )
        synced_count += int(synced)

    print(f"\n--- Sync complete: {synced_count}/{len(sync_pairs)} pair(s) updated ---")


if __name__ == "__main__":
    main()
