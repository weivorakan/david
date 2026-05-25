import os
import json
from llama_cpp import Llama, GGML_TYPE_Q4_0
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from contextlib import asynccontextmanager

model_name = "gemma-4-E4B-it-IQ4_NL.gguf"
clip_model_name = "gemma-4-E4B-it.mmproj-q8_0.gguf"
BASE_DIR = r"/home/weivo/david/ai_models"
MODEL_PATH = os.path.join(BASE_DIR, model_name)
CLIP_MODEL_PATH = os.path.join(BASE_DIR, clip_model_name)

if not os.path.exists(MODEL_PATH) and not os.path.exists(CLIP_MODEL_PATH):
    print("!!! ---Error: No model found--- !!!")


ai_model = None 

@asynccontextmanager
async def lifespan(app: FastAPI):
    global ai_model
    
    # ดึงขนาดไฟล์โมเดลเพื่อคำนวณ Model Weight
    model_file_size = os.path.getsize(MODEL_PATH) / (1024**2) # แปลงเป็น MB

    # 1. โหลดโมเดลเข้า VRAM ตอน Start Server
    # ตั้งค่า 28 จาก 30 Layers ตามสถาปัตยกรรม Gemma 4-26B[cite: 1, 2, 3]
    try:
        ai_model = Llama(
            model_path=MODEL_PATH,
            clip_model_path=CLIP_MODEL_PATH,
            n_ctx=24576,                 # กำหนดขนาด Context Window 24K (Total Tokens/Input) - 16384(16K), 20480(20K), 24576(24K), 32768(32K)
            n_gpu_layers=32,             # รันบน GPU 32 layers จากทั้งหมด 43 layers ของ gemma4-E4B
            n_thread=8,                  # บังคับใช้ CPU Cores ตามจำนวน 8 Performance Cores (16 threads) ของ AMD Ryzen 7 5700U (1900 MHz) พร้อม Integrated Radeon Graphics (AMD Radeon RX Vega 8 - Lucienne มี 8 Graphic Cores 1900 MHz) ไม่มี VRAM ในตัว
            type_k=GGML_TYPE_Q4_0,       # บีบอัด KV Cache เป็น Q4_0
            type_v=GGML_TYPE_Q4_0,
            flash_attn=True,             # เปิด Flash Attention
            verbose=True,
            n_batch=512,                 # (Logical Batch Size): คือจำนวน Token ทั้งหมดที่ Model จะพยายามประมวลผลในรอบนั้นๆ (เช่น 512 Token) ค่านี้ยิ่งมาก ยิ่งช่วยให้ Model อ่าน Prompt ยาวๆ ได้เร็วขึ้น
            n_ubatch=128                 # (Physical/Micro Batch Size): คือขนาดของ "ชิ้นงาน" ที่ส่งให้ GPU คำนวณจริงในหนึ่งจังหวะ (Execution step))
        )                                # ตัวอย่าง: หากตั้ง n_batch=512 และ n_ubatch=128 หมายความว่า Model จะรับงานมา 512 Token แต่จะซอยส่งให้ iGPU ทำทีละ 128 Token จำนวน 4 ครั้ง
        print("--- David is Ready (VRAM Assigned) ---")
        yield
    finally:
        # 2. คืน VRAM เมื่อปิด Server
        if ai_model:
            del ai_model
        print("--- David Service Stopped ---")

app = FastAPI(lifespan=lifespan)

# กำหนดรูปแบบข้อมูลขาเข้า เป็น messages แบบ list
class AnalysisRequest(BaseModel):
    messages: list
    temperature: float = 0.1

@app.post("/analyze")
async def analyze_chart(request: AnalysisRequest):
    if not ai_model:
        raise HTTPException(status_code=503, detail="Model not loaded")
    
    def stream_generator():
        # 1. นับ Input Tokens จริงๆ จากข้อความที่รับมา
        # แปลง messages เป็น string และ tokenize เพื่อนับจำนวน
        prompt_str = str(request.messages)
        input_tokens = len(ai_model.tokenize(prompt_str.encode('utf-8')))
        completion_tokens = 0

        # ตัด stream_options ออกเพื่อแก้ TypeError
        stream = ai_model.create_chat_completion(
            messages=request.messages,
            temperature=request.temperature,
            stream=True
        )

        for chunk in stream:
            # ตรวจสอบว่ามีเนื้อหาถูกส่งออกมาจริงไหม เพื่อนับเป็น Output Token
            if 'choices' in chunk and len(chunk['choices']) > 0:
                if chunk['choices'][0].get('delta', {}).get('content'):
                    completion_tokens += 1
            yield json.dumps(chunk) + "\n"

        # 2. ส่ง Chunk พิเศษปิดท้าย เพื่อส่งค่า Usage ให้ฝั่ง Client
        usage_chunk = {
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": completion_tokens
            }
        }
        yield json.dumps(usage_chunk) + "\n"       # ส่งข้อมูลออกไปในรูปแบบ JSON string
    
    return StreamingResponse(stream_generator(), media_type="application/x-ndjson")


@app.get("/")
async def root():
    return {"status": "AI model is ready to run."}


if __name__ == "__main__":
    import uvicorn
    # รัน Server ที่ Port 8000 (หรือ Port ที่คุณต้องการ)
    uvicorn.run(app, host="127.0.0.1", port=8000)