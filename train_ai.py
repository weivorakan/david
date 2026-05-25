import json
import base64
import requests
import os

# --- ตั้งค่าเบื้องต้น ---
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "qwen3.5:0.8b"  # หรือชื่อรุ่นที่คุณโหลดไว้
OUTPUT_FILE = "price_action_learning.jsonl"

def encode_image(image_path):
    """แปลงรูปภาพเป็น Base64 เพื่อส่งให้ AI"""
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

def collect_training_data():
    print("--- 🧠 Al Brooks Training Collector (Marathon Mode) ---")
    
    # 1. รับที่อยู่รูปภาพ
    img_path = input("\n1. ลากไฟล์รูปกราฟมาวางที่นี่ (หรือพิมพ์ Path): ").strip().replace("'", "").replace('"', '')
    
    if not os.path.exists(img_path):
        print("❌ หาไฟล์ภาพไม่เจอครับ ลองใหม่อีกครั้งนะ")
        return

    # 2. ตั้งคำถาม (Prompt)
    user_prompt = "Analyze this chart based on Al Brooks Price Action. Identify signal bars or key patterns."
    print(f"\n2. กำลังส่งภาพไปให้ {MODEL_NAME} วิเคราะห์... (อาจใช้เวลา 30-60 วินาที)")

    # 3. สื่อสารกับ Ollama
    payload = {
        "model": MODEL_NAME,
        "prompt": user_prompt,
        "images": [encode_image(img_path)],
        "stream": False
    }

    try:
        response = requests.post(OLLAMA_URL, json=payload)
        ai_response = response.json().get('response', '')
        
        print("\n--- [คำตอบจาก AI] ---")
        print(ai_response)
        print("----------------------")

        # 4. ขั้นตอนการ "สอน" (Human Feedback)
        print("\n3. ถึงตาคุณแล้ว! โปรดพิมพ์คำตอบที่ 'ถูกต้องที่สุด' ตามหลัก Al Brooks:")
        print("(ถ้าคำตอบ AI ถูกแล้ว ให้กด Enter ผ่านได้เลย หรือพิมพ์แก้ไขจุดที่ผิด)")
        final_answer = input("> ")
        
        if final_answer.strip() == "":
            final_answer = ai_response

        # 5. บันทึกลง JSONL (รูปแบบมาตรฐานโลก)
        data_entry = {
            "messages": [
                {"role": "system", "content": "You are an expert Al Brooks Price Action trader."},
                {"role": "user", "content": user_prompt, "image_path": img_path},
                {"role": "assistant", "content": final_answer}
            ]
        }

        with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(data_entry, ensure_ascii=False) + "\n")

        print(f"\n✅ บันทึกบทเรียนลงใน {OUTPUT_FILE} เรียบร้อย! สะสมแต้มมาราธอนต่อไปครับ")

    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาด: {e}")

if __name__ == "__main__":
    while True:
        collect_training_data()
        cont = input("\nต้องการสอนภาพต่อไปไหม? (y/n): ")
        if cont.lower() != 'y':
            break
