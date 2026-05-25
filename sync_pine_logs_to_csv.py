import pandas as pd
import os
import glob
import time

# ระบุ Path ของ Pine Logs (ตัวอย่างสำหรับ Windows)
# โดยปกติจะอยู่ใน AppData/Roaming/TradingView/logs/...
LOG_PATH = os.path.expanduser(r"~\AppData\Roaming\TradingView\logs\*\*.log")

def sync_pine_logs_to_csv(output_filename):
    print("Watching for Pine Logs...")
    
    # ค้นหาไฟล์ Log ล่าสุด
    list_of_files = glob.glob(LOG_PATH)
    if not list_of_files:
        print("ไม่พบไฟล์ Pine Logs กรุณาเปิดหน้าต่าง Pine Logs ใน TradingView Desktop")
        return

    latest_log = max(list_of_files, key=os.path.getctime)
    print(f"Reading from: {latest_log}")

    data_list = []
    
    with open(latest_log, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        for line in lines:
            if "[DAVID-DATA]" in line:
                # แยกข้อมูล: 2026-03-24 15:30:00,2150.1,2151.0...
                raw_content = line.split("[DAVID-DATA],")[-1].strip()
                parts = raw_content.split(",")
                if len(parts) == 5:
                    data_list.append(parts)

    # สร้าง DataFrame และบันทึก
    df = pd.DataFrame(data_list, columns=['datetime', 'open', 'high', 'low', 'close'])
    df.set_index('datetime', inplace=True)
    df.to_csv(output_filename)
    print(f"Saved {len(df)} bars to {output_filename}")

# ตัวอย่างการใช้งาน
if __name__ == "__main__":
    sync_pine_logs_to_csv("OHLC_PART/XAUUSD_Bridge.csv")