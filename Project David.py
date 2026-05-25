import os
import time
import pyttsx3
import google.generativeai as genai
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- 1. ตั้งค่า API Key และ Model ---
# ไปเอา API Key ที่ https://aistudio.google.com/
genai.configure(api_key="AIzaSyAfhezfz2BOxvZ38La8cL39jVljx1Uh-lg")

# เลือกใช้ Gemini 1.5 Flash เพราะเร็วที่สุดสำหรับ Real-time
model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    system_instruction="คุณคือ Co-pilot นักเทรด วิเคราะห์รูปกราฟ MT4 และตอบสั้นๆ ไม่เกิน 15 คำ เพื่อให้ระบบอ่านออกเสียงได้ทันใจ"
)

# --- 2. ตั้งค่าระบบเสียง (Text-to-Speech) ---
engine = pyttsx3.init()
def speak(text):
    print(f"Gemini: {text}")
    engine.say(text)
    engine.runAndWait()

# --- 3. ฟังก์ชันส่งรูปไปให้ Gemini ---
def analyze_chart(image_path):
    try:
        # อัปโหลดไฟล์รูป
        sample_file = genai.upload_file(path=image_path)
        
        # ส่ง Prompt ถาม (คุณสามารถแก้ Prompt ตรงนี้ให้ตรงใจคุณได้)
        response = model.generate_content([sample_file, "วิเคราะห์กราฟนี้ตาม Pattern ที่ผมเทรนคุณไว้ มีจุดน่ากังวลหรือโอกาสเทรดไหม?"])
        
        # พูดคำตอบออกมา
        speak(response.text)

        # --- เพิ่มบรรทัดนี้เพื่อลบทิ้งหลังใช้งานเสร็จ ---
        os.remove(image_path) 
        print(f"ลบไฟล์ {image_path} เรียบร้อย เพื่อประหยัดพื้นที่")
        
    except Exception as e:
        print(f"Error: {e}")

# --- 4. ระบบตรวจจับไฟล์ใหม่ใน Folder (Watchdog) ---
class ChartHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(('.png', '.jpg')):
            print(f"พบรูปใหม่: {os.path.basename(event.src_path)}")
            # หน่วงเวลาเล็กน้อยเพื่อให้ MT4 เขียนไฟล์เสร็จสมบูรณ์
            time.sleep(1) 
            analyze_chart(event.src_path)

if __name__ == "__main__":
    # ระบุ Path ของ Folder 'Files' ใน MT4 ของคุณ
    # ปกติจะอยู่ที่: C:/Users/.../AppData/Roaming/MetaQuotes/Terminal/[ID]/MQL4/Files
    path_to_watch = "./mt4_screenshots" 
    
    if not os.path.exists(path_to_watch):
        os.makedirs(path_to_watch)

    event_handler = ChartHandler()
    observer = Observer()
    observer.schedule(event_handler, path_to_watch, recursive=False)
    
    print(f"--- TradePilot Bridge เริ่มทำงานแล้ว (กำลังเฝ้าดู Folder: {path_to_watch}) ---")
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()