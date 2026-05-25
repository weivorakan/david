import os
import ollama
import torch
import pandas as pd
import sys
import re
from langchain_ollama import OllamaEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
from datetime import datetime
from PIL import Image
from transformers import AutoTokenizer
from sentence_transformers import SentenceTransformer
from tvdatafeed import TvDatafeed, Interval

# --- Path Configuration ---
BASE_DIR = r"C:\david"
BOOK_PATH = os.path.join(BASE_DIR, "book")
KNOWLEDGE_PATH = os.path.join(BASE_DIR, "knowledge")
OHLC_PART = os.path.join(BASE_DIR, "ohlc")
LESSON_PATH = os.path.join(BASE_DIR, "wei_lesson")
JOURNAL_PATH = os.path.join(BASE_DIR, "journal")
BRAIN_PATH = os.path.join(BASE_DIR, "brain")
TEXT_BRAIN_PATH = os.path.join(BRAIN_PATH, "text_index")
IMAGE_BRAIN_PATH = os.path.join(BRAIN_PATH, "image_index") 
MODEL_NAME = "gemma-4-26B-A4B-it-UD-Q3_K_M"
TEXT_EMBEDDING_MODEL = "nomic-ai/nomic-embed-text-v1.5"
IMAGE_EMBEDDING_MODEL = "clip-ViT-B-32"

text_embed = SentenceTransformer(TEXT_EMBEDDING_MODEL, device='cpu')
image_embed = SentenceTransformer(IMAGE_EMBEDDING_MODEL, device='cpu')

# สร้าง Class เพื่อให้ FAISS (LangChain) เรียกใช้ EMBEDDING MODELS ได้
class HybridEmbeddings:
    def embed_documents(self, texts):
        return text_embed.encode(texts).tolist()
    def embed_query(self, text):
        return text_embed.encode([text])[0].tolist()

embeddings = HybridEmbeddings()

SYSTEM_PROMPT = """You are David, an AI assistant specialized in Al Brooks Price Action.
Your role is to learn Price Action from a user, Wei. You use three books of Al Brooks as references on trading terminology and methodology.
Wei will provide you more charts with explanation to learn from time to time in order to teach you.
You must learn to memorize many important patterns to evaluate the meaning of any chart I will ask you in the future.
Wei will test you to see your development until you are ready to be his co-pilot in trading.
Moreover, before giving the final answer(output), please provide a brief internal monologue or step-by-step reasoning explaining how you interpreted the request and which parts of the retrieved context you are using to formulate your response.
Finally, when you receive specific command 'David, provide the <final_summary> for your brain now.', you must wrap your ultimate, correct conclusion in <final_summary> tags.
CRITICAL RULE: You must answer something based on your found context and logic before getting the advise from Wei. Even if it is wrong, Wei will explain to you later. Give the best answer you can!"""

# --- David's Brain & Reasoning Configuration ---
options_config = {
    'temperature': 0.1,           # คงความแม่นยำในการตอบ
    'num_ctx': 16384,             # ขนาดสมองรวม Total Tokens/Input (จำได้ยาวขึ้น)
    'num_predict': 4096,          # เพดานคำพูด Output Tokens (ถ้าไม่เกินก็ไม่ให้โดนตัดบทดื้อๆ)
    'repeat_penalty': 1.2,        # ป้องกันการพูดวนไปวนมา (แก้ปัญหาการ Loop)
    'top_k': 20,                  # จำกัดทางเลือกคำให้คมขึ้น
    'top_p': 0.5,                 # เน้นคำตอบที่มีความน่าเชื่อถือสูง
    'num_thread': 8               # บังคับใช้ CPU Cores ตามความแรงเครื่อง (ปรับตามจำนวน core จริงของคุณ)
}

total_images_list: list[str] = []
total_input_tokens = 0
total_output_tokens =  0

tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-7B-Instruct")

for p in [BOOK_PATH, CHART_PATH, LESSON_PATH, JOURNAL_PATH, BRAIN_PATH, TEXT_BRAIN_PATH, IMAGE_BRAIN_PATH]:
    os.makedirs(p, exist_ok=True)

def main():
    global total_images_list
    global total_input_tokens
    global total_output_tokens
    
    # เช็คว่ามีไฟล์สมอง FAISS ทั้ง text และ image อยู่แล้วหรือไม่ (อย่างน้อยต้องมี text_index.faiss ที่เกิดจากการอ่าน Books ถ้าไม่มีก็ให้สร้างสมองขึ้นมาใหม่)
    if os.path.exists(os.path.join(BRAIN_PATH, "text_index.faiss")):
        text_vectorstore = FAISS.load_local(TEXT_BRAIN_PATH, HybridEmbeddings, allow_dangerous_deserialization=True)       
        
        if os.path.exists(os.path.join(BRAIN_PATH, "image_index.faiss")):
            image_vectorstore = FAISS.load_local(IMAGE_BRAIN_PATH, HybridEmbeddings, allow_dangerous_deserialization=True)
    else:
        if create_brain() == None:
            print("Error: Unable to create brain!")
            return
    
    while True:
        try:        
            summary_content = ""
            full_content = ""
            found_images = []
            system_note = ""
            mode_text = ""
            response_text = ""
            last_text = ""
            category = "KNOWLEDGE"
            save_type = "text"
            is_pattern = False
            
            print("\n--- David is online ---")
            print("** Asking any questions by using '?' **")
            print("** For PATTERN, type 'p:' or 'p:file' **")
            print("** For KNOWLEDGE, type 'k:' or 'k:file' **")
            print("** Type 'exit'/'quit'/'bye' to terminate **\n")
            user_input = input("Wei: ").strip()
            
            if user_input.strip().lower() in ['exit', 'quit', 'bye']:
                break
            
            # ตรวจสอบดูว่ารูปแบบ PATTERN ที่ต้องจดจำหรือไม่
            if user_input.lower().startswith("p:"):
                mode_text += "David is memorizing the pattern...\n"
                response_text += "[DAVID_ACKNOWLEDGEMENT]:\n"
                last_text += "David, memorize this pattern which is derived from Wei's experiences and explain briefly if you understand it."
                
                # ตรวจสอบดูว่าต้องการให้ความรู้ด้วยการนำเข้า Input ด้วย File หรือไม่
                if user_input.lower().startswith("p:file"):
                    file_name = input("Enter file name: ").strip()
                    if not file_name.lower().endswith(".txt"):
                        file_name += ".txt"
                        
                    if os.path.exists(os.path.join(LESSON_PATH, file_name)):
                        with open(os.path.join(LESSON_PATH, file_name), "r", encoding="utf-8") as f:
                            user_input = f.read()
                            if user_input == "":
                                print("\n!!! --- No data in text file --- !!!\n")
                                continue
                else:
                    user_input = user_input.replace("p:", "").strip()

                summary_content += f"[PATTERN_EXPLANATION]:\n{user_input}\n\n"

                while True:
                    try:
                        category = input("Name of pattern: ").strip()
                        if not category:
                            continue
            
                        input_condition = input(f"Confirm the name '{category}'? (y/n): ").lower().strip()
                        if input_condition == "y":
                            save_type = "image"
                            is_pattern = True
                            break
                        else:
                            continue

                    except Exception as e:
                        print(f"Error: {e}")
                        continue
            
            # ตรวจสอบว่าเป็นเป็นการให้ KNOWLEDGE หรือไม่
            elif user_input.lower().startswith("k:"):
                mode_text += "David is absorbing your knowledge...\n"
                response_text += "[DAVID_ACKNOWLEDGEMENT]:\n"
                last_text += "David, acknowledge this knowledge which is derived from Wei's experiences and explain briefly if you understand it."

                # ตรวจสอบดูว่าต้องการให้ความรู้ด้วยการนำเข้า Input ด้วย File หรือไม่
                if "k:file" in user_input.strip().lower():
                    file_name = input("Enter file name: ").strip()
                    if not file_name.lower().endswith(".txt"):
                        file_name += ".txt"
                        
                    if os.path.exists(os.path.join(LESSON_PATH, file_name)):
                        with open(os.path.join(LESSON_PATH, file_name), "r", encoding="utf-8") as f:
                            user_input = f.read()
                            if user_input == "":
                                print("\n!!! --- No data in text file --- !!!\n")
                                continue
                else:
                    user_input = user_input.replace("k:", "").strip()

                summary_content += f"[KNOWLEDGE_FROM_WEI]:\n{user_input}\n\n"

                while True:
                    try:
                        input_condition = input("Save to image vectorstore? (y/n): ").lower().strip()
                        if input_condition == "y":
                            save_type = "image"
                            break
                        elif input_condition == "n":
                            break
                        else:
                            continue
                    except Exception as e:
                        print(f"Error: {e}")
                        continue
            
            # ตรวจสอบดูว่าเป็นชุดคำถามหรือไม่
            elif "?" in user_input:
                mode_text += "David is thinking...\n"
                response_text += "[DAVID_ANALYSIS]:\n"

                summary_content += f"[QUESTION_FROM_WEI]:\n{user_input}\n\n"

                while True:
                    try:
                        input_condition = input("Save to image vectorstore? (y/n): ").lower().strip()
                        if input_condition == "y":
                            save_type = "image"
                            break
                        elif input_condition == "n":
                            break
                        else:
                            continue
                    except Exception as e:
                        print(f"Error: {e}")
                        continue
            
            else:
                continue
            
            # ค้นหา THEORY และ KNOWLEDGE ที่เกี่ยวข้องกับ user_input
            knowledge_content = get_matched_knowledge(text_vectorstore, user_input, 10)
            if knowledge_content.strip():
                print("\nKnowledge contents was found.")      # print(f"\n[EXPERIENCE_FOUND]:\n{knowledge_content}\n")
                prompt_content = f"[THEORY_AND_KNOWLEDGE_FROM_MEMORY]:\n{knowledge_content}\n\n"
            else:
                print("\nKnowledge contents could not be found.")
            
            # ค้นหาภาพ(ถ้ามี)ใน user_input เพื่อนำมาประกอบใน content
            found_images, system_note = find_images_in_content(user_input)
            if found_images:
                summary_content += system_note
                
                matched_ohlc = fetch_OHLC(found_images, start=1)
                if matched_ohlc:
                    for str_ohlc in matched_ohlc:
                        summary_content += str_ohlc
                        
                prompt_content += f"Matched patterns for [IMAGE 1]: {found_images[0]}\n\n---\n\n"
                prompt_content += find_matched_pattern(image_vectorstore, found_images[0], k=10, distance_threshold=0.5)
                prompt_content += "\n\n---\n\n"
            else:
                if is_pattern == True:
                    print("No pattern to be memorized!\n")
                    continue
            
            prompt_content += summary_content + last_text
            initial_prompt = [
                    {'role': 'system', 'content': SYSTEM_PROMPT},
                    {'role': 'user', 'content': prompt_content, 'images': found_images}
                ]

            # ประเมินปริมาณ Tokens ทั้งหมดที่ต้องใช้ในการส่งให้ข้อความและภาพให้ David ผ่าน prompt
            text_tokens, image_tokens = get_token_count(initial_prompt)
            print(f"[TOKEN ESTIMATION]: Text input = {text_tokens} | Image input = {image_tokens} | Total input = {text_tokens + image_tokens}\n")

            initial_analysis = david_chat(initial_prompt, mode_text, response_text)
            if not initial_analysis.strip():
                # ถ้าเกิด ollama.chat เกิด Crash หรือทำงานไม่สำเร็จ หรือ ollama ตัดการทำงานของ David ให้กลับไปลูปถาม Input ใหม่
                print("\nError: Empty Response\n")
                continue
            
            # เมื่อการวิเคราะห์ของ David สำเร็จ ให้บันทึกภาพ(ถ้ามี)จาก Input เพิ่มเข้าไปใน total_images_list ซึ่งเป็น Global Variable โดยภาพจะไม่ซ้ำกัน
            total_images_list = list(dict.fromkeys(total_images_list + found_images))
            print(f"{len(total_images_list)} image(s) realized by David.")
            print(f"Image: {[os.path.splitext(os.path.basename(path))[0] for path in total_images_list]}\n")

            full_content += summary_content + initial_analysis

            # Loop โต้ตอบ จนกว่า Wei จะบอกว่า "ถูกต้อง"
            while True:
                try:
                    r_found_images = []
                    r_system_note = ""
            
                    feedback = input("Do I understand correctly (y/n/quit)? > ").strip()
            
                    if feedback == "y":
                        mode_text = "David is summarizing the conclusion...\n"
                        response_text = "[FINAL_SUMMARY]:\n"
                
                        final_prompt_content = full_content
                        final_prompt_content += "David, your analysis is correct. Provide the <final_summary> for your brain now."
                        final_prompt = [
                            {'role': 'system', 'content': SYSTEM_PROMPT},
                            {'role': 'user', 'content': final_prompt_content}
                        ]

                        final_analysis = david_chat(final_prompt, mode_text, response_text)
                        if not final_analysis.strip():
                            # ถ้าเกิด ollama.chat เกิด Crash หรือทำงานไม่สำเร็จ หรือ ollama ตัดการทำงานของ David ให้กลับไปลูปถาม Input ใหม่
                            print("\nError: Empty Response\n")
                            continue
                        else:
                            final_summary = re.search(r'<final_summary>(.*?)</final_summary>', final_analysis, re.DOTALL)
                            if final_summary:
                                summary_content += final_summary.group(1).strip()
                            
                        while True:
                            try:
                                if save_type == "text":
                                    # current_time_string = datetime.now().strftime("%y%m%d_%H%M")
                                    save_text_to_brain(text_vectorstore, summary_content, total_images_list[0])
                                    break
                                elif save_type == "image":
                                    save_image_to_brain(image_vectorstore, category, summary_content, total_images_list[0])
                                    file_name = os.path.splitext(total_images_list[0])[0]
                            
                                    break
                                else:
                                    continue
                            
                            except Exception as e:
                                print(f"Error: {e}")
                                continue
                            
                        print("\nKnowledge was memorized successfully.")
                
                        print(f"[TOTAL TOKEN USAGE] Input: {total_input_tokens} | Output: {total_output_tokens} | Total: {total_input_tokens+total_output_tokens}\n")
                
                        total_input_tokens = 0
                        total_output_tokens = 0
                        
                        total_images_list.clear()
                        break
                
                    elif feedback == "n":
                        temp_images_list = []
                        mode_text = "David is thinking...\n"
                        response_text = "[DAVID_REFINEMENT_ANALYSIS]:\n"
                        last_text = "David, you didn't understand it precisely. Read my correction and analyze it again.\n\n"
                        
                        correction_input = input("Wei, please explain: ").strip()
                        refined_prompt_content = full_content + f"[WEI_CORRECTION]:\n{correction_input}\n\n"
                        
                        # ค้นหาภาพเพิ่มเติม(ถ้ามี)ใน correction input เพื่อนำมาประกอบใน refined_content
                        r_found_images, r_system_note = find_images_in_content(correction_input)
                        if r_found_images:
                            # เพิ่มไฟล์ภาพเพิ่มเติม(ถ้ามี)ใน correction input โดยสร้างรวมกับภาพเดิมที่เคยคุยกันใน session แรกด้วย
                            temp_images_list += total_images_list + r_found_images
                            refined_prompt_content += r_system_note

                            matched_ohlc = fetch_OHLC(r_found_images, start=len(total_images_list)+1)
                            if matched_ohlc:
                                for str_ohlc in matched_ohlc:
                                    refined_prompt_content += str_ohlc

                            for i, img in enumerate(r_found_images, start=len(total_images_list)+1):
                                refined_prompt_content += f"Matched patterns for [IMAGE {i}]: {img}\n\n---\n\n"
                                refined_prompt_content += find_matched_pattern(image_vectorstore, img, k=10, distance_threshold=0.5)
                                refined_prompt_content += "\n\n" + "=" * 30 + "\n\n"
                        else:
                            temp_images_list = total_images_list
                        
                        refined_prompt_content += last_text
                        refined_prompt = [
                            {'role': 'system', 'content': SYSTEM_PROMPT},
                            {'role': 'user', 'content': refined_prompt_content, 'images': temp_images_list}
                        ]           
                
                        # ประเมินปริมาณ Tokens ที่ต้องใช้ในส่วน Refinement
                        r_text_tokens, r_image_tokens = get_token_count(refined_prompt)
                        print(f"\n[TOKEN ESTIMATION]: Text input = {r_text_tokens} | Image input = {r_image_tokens} | Total input = {r_text_tokens + r_image_tokens}\n")

                        refined_analysis = david_chat(refined_prompt, mode_text, response_rtext)
                        if not refined_analysis.strip():
                            # ถ้าเกิด ollama.chat เกิด Crash หรือทำงานไม่สำเร็จ หรือ ollama ตัดการทำงานของ David ให้กลับไปลูปถาม Input ใหม่
                            print("\nError: Empty Response\n")
                            continue
                        
                        # เมื่อการวิเคราะห์ของ David สำเร็จ ให้บันทึกภาพ(ถ้ามี)จาก Input เพิ่มเข้าไปใน total_images_list ซึ่งเป็น Global Variable โดยภาพจะไม่ซ้ำกัน
                        total_images_list = list(dict.fromkeys(total_images_list + r_found_images))
                        print(f"Total {len(total_images_list)} image(s) realized by David.")
                        print(f"Image: {[os.path.splitext(os.path.basename(path))[0] for path in total_images_list]}\n")
                    
                        full_content += refined_analysis + "\n\n"
                        
                    elif feedback == "quit":
                        print("\nExit while not finish session.")
                        print(f"[TOTAL TOKEN USAGE] Input: {total_input_tokens} | Output: {total_output_tokens} | Total: {total_input_tokens+total_output_tokens}\n")
                
                        total_input_tokens = 0
                        total_output_tokens = 0
                            
                        total_images_list.clear()
                        break
                    else:
                        continue
        
                except Exception as e:
                    print(f"Error: {e}")
                
        except Exception as e:
            print(f"Error: {e}")


# ฟังก์ชั่น streaming ollama.chat
def david_chat(prompt, mode_text, response_text):
    chunk = None
    analysis = ""
    
    # David เริ่มกระบวนการคิด... (ใช้เวลา)
    print(mode_text)
    
    stream = ollama.chat(model=MODEL_NAME, messages=prompt, stream=True, options=options_config)
            
    # David เริ่มแสดงความคิดภายใน (Internal Monologue) โดยพิมพ์ Reasoning ออกมาทีละชิ้นทันที 
    is_first_chunk = True
    for chunk in stream:
        if is_first_chunk and 'message' in chunk and 'content' in chunk['message']:
            content = response_text + chunk['message']['content']
            print(content, end="", flush=True)
            analysis += content
            is_first_chunk = False
        elif 'message' in chunk and 'content' in chunk['message']:
            content = chunk['message']['content']
            print(content, end="", flush=True)
            analysis += content
    print("\n" + "-"*30)
                
    # คำนวณปริมาณ Tokens ที่ใช้จริงทั้งหมด
    if chunk:
        # chunk ชุดสุดท้ายจะส่งค่า tokens ออกมา
        input_tokens = chunk.get('prompt_eval_count', 0)
        output_tokens = chunk.get('eval_count', 0)
                
        total_input_tokens += input_tokens
        total_output_tokens += output_tokens
        total_session_tokens = input_tokens + output_tokens
                
        print(f"\n[SESSION TOKEN USAGE] Input: {input_tokens} | Output: {output_tokens} | Total: {total_session_tokens}\n")    
        update_token_history(input_tokens, output_tokens)
    else:
        print("\n[SESSION TOKEN USAGE ERROR]: Problem with Ollama.\n")

    return analysis


# ฟังก์ชั่นค้นหา PATTERN ที่คล้ายกับ image_path
def find_matched_pattern(vectorstore, image_path, k=10, distance_threshold=0.5):
    if not vectorstore:
        return None

    # 1. แปลงภาพปัจจุบันเป็น Vector
    query_vector = image_embed.encode(Image.open(image_path)).tolist()

    # 2. ค้นหาในสมองพร้อมค่าความห่าง (Score), ผลลัพธ์ที่ได้คือ List ของ tuple (Document, score)
    results = vectorstore.similarity_search_with_score_by_vector(query_vector, k=k)

    matched_patterns = []
    
    for doc, score in results:
        # ใน FAISS L2 Distance: ยิ่งน้อยยิ่งเหมือน (0.0 คือเหมือนเป๊ะ)
        if score <= distance_threshold:
            # ดึงข้อมูล OHLC และคำบรรยายที่เรา save ไว้ใน page_content
            pattern_info = doc.page_content
            source_file = doc.metadata.get('source', 'Unknown')
            
            matched_patterns.append(f"Match Score (L2): {score:.4f}\nSource: {source_file}\nDetails: {pattern_info}")

    if not matched_patterns:
        return None # ไม่พบรูปแบบที่มั่นใจพอ
    
    str_matched_patterns = "\n\n---\n\n".join(matched_patterns)
    return str_matched_patterns


# ฟังก์ชั่นดึง THEORY และ KNOWLEDGE จาก MEMORY
def get_matched_knowledge(vectorstore, query, k=10):
    if not vectorstore:
        return ""
    
    # ค้นหาข้อมูลที่ใกล้เคียงกับคำถามที่สุด จำนวน k ลำดับ
    results = vectorstore.similarity_search(query, k=k)
    content_list = []
    
    for doc in results:
        # ดึง Metadata category ที่เราตั้งไว้ (เช่น 'THEORY' หรือ 'KNOWLEDGE')
        category = doc.metadata.get('category', 'KNOWLEDGE').upper()
        source = doc.metadata.get('source', 'unknown')
        
        # ประกอบร่างข้อความเพื่อให้ David แยกแยะที่มาได้
        content_list.append(f"[{category}] (Source: {source}):\n{doc.page_content}")
    
    return "\n---\n".join(content_list)


def fetch_OHLC(image_list, start=1):
    while True:
        ok_to_config = input("\nOk to fetch OHLC for charts? (y/n): ").strip().lower()
        if ok_to_config == "y":
            break
        elif ok_to_config == "n":
            return None
        else:
            continue

    ohlc_list = []
    
    # วน Loop ตามจำนวนภาพที่พบ โดยใช้ enumerate เพื่อระบุลำดับภาพ
    for i, img in enumerate(image_list, start=start):
        print(f"\n--- Configuration of [IMAGE {i}: {img} ---")
        
        # รับค่า Symbol และ Exchange
        symbol = input("Symbol: ").strip()
        exchange = input("Exchange: ").strip()
    
        # รับค่า Timeframe
        timeframe = input("Timeframe (1m/5m/15m/1h/1d/1w): ").strip().lower()
    
        # รับค่า Start_time และ End_time
        date = input("Date (YYYY-MM-DD): ").strip()
        tmp_start_time = input("Start time (HH:MM:SS): ").strip()
        tmp_end_time = input("End time (HH:MM:SS): ").strip()
    
        start_time = f"{date} {tmp_start_time}"
        end_time = f"{date} {tmp_end_time}"

        image_name = f"{symbol}_{exchange}_{timeframe}.csv"
        ohlc_path = os.path.join(OHLC_PATH, image_name)

        print("Fetching OHLC...")
        # โหลดไฟล์และตั้งค่า Index เป็น datetime
        df = pd.read_csv(ohlc_path, index_col=0, parse_dates=True)
    
        # --- ส่วนแก้ไขเรื่อง Timezone ---
        if df.index.tz is None:
            # กรณีไม่มี Timezone: ระบุว่าเป็น UTC แล้วแปลงเป็นเวลาไทย
            df.index = df.index.tz_localize('UTC').tz_convert('Asia/Bangkok')
        else:
            # กรณีมี Timezone อยู่แล้ว (เช่น UTC): แปลงให้เป็นเวลาไทยทันที
            df.index = df.index.tz_convert('Asia/Bangkok')
    
        # ถอด Timezone ออกให้เหลือแค่ "ตัวเลขเวลา" (Naive) เพื่อให้ง่ายต่อการอ้างอิง
        df.index = df.index.tz_localize(None)

        # เรียงลำดับเพื่อให้การ Slice ช่วงเวลาถูกต้อง
        df = df.sort_index(ascending=True)

        # ดึงข้อมูลในช่วง start_time ถึง end_time, Pandas จะดึงข้อมูลทุกแท่งที่อยู่ในช่วงเวลานี้มาให้ทั้งหมด
        df_filtered = df.loc[start_time : end_time]

        # ตรวจสอบว่ามีข้อมูลในช่วงที่ระบุหรือไม่
        if df_filtered.empty:
            print(f"!!! No data found between {start_time} and {end_time} !!!")
        else:
            print(f"Successfully fetched {len(df_filtered)} bars")
        
        # ใช้ index=True หากเวลาของคุณอยู่ที่ Index
        ohlc_reference = df_filtered[['open', 'high', 'low', 'close']].to_string(index=True)

        # รับค่าตำแหน่ง Bar และเวลาที่สัมพันธ์กับภาพนี้
        index_bar_no = input("\nChoosing index bar no: ")
        tmp_time = input("Time related to index bar (HH:MM:SS): ")
        time_related_bar = date + " " + tmp_time
                                
        # สะสมข้อมูลเข้าใน ohlc_content โดยระบุว่าอ้างอิงกับ Image ลำดับที่เท่าไหร่
        ohlc_content = f"[OHLC data for IMAGE {i}]:\n"
        ohlc_content += ohlc_reference + "\n"
        ohlc_content += f"Bar {index_bar_no} is at {time_related_bar}."
        ohlc_content += "-" * 30 + "\n"
        ohlc_list.append(ohlc_content)
        
    return ohlc_list


# ฟังก์ชั่นค้นหาชื่อภาพในข้อความ text_to_be_searched และคืนค่า List ของ Path รูปภาพที่พบ
def find_images_in_content(text_to_be_searched):
    found_images = []       # จุดรวมชื่อภาพที่หาเจอพร้อมตำแหน่ง Directory ที่ค้นหาจาก text_to_be_searched
    system_note = "[System Note]:\n"    # ข้อความสำหรับสร้าง System Note เพื่อบอก David เกี่ยวกับลำดับภาพ
    image_count = len(total_images_list)

    #  ค้นหาชื่อภาพ Symbol_YYMMDD_Day ในวงเล็บเหลี่ยม [] ที่ขึ้นต้นด้วย "image:
    #  \s* คือ User จะพิมพ์โดยมีช่องว่างหรือไม่มีช่องว่างก็ได้
    #  (.*?) ให้ return ค่าค้นหา โดยเริ่มนับคำหลังจาก "[image:" และให้จบคำนั้นหลังจากเจอ "]"
    tags = re.findall(r"\[image:\s*(.*?)\]", text_to_be_searched)                                                    #
    if not tags:
        return [], ""

    try:
        # รายชื่อไฟล์ภาพทั้งหมดในโฟลเดอร์ chart
        all_files = os.listdir(CHART_PATH)
    except Exception:
        return [], ""
    
    for tag in tags:
        found_file = ""
        # ค้นหาไฟล์ที่มีชื่อตรงกับ Tag (รองรับ .png, .jpg, .jpeg)
        for f in all_files:
            if os.path.splitext(f)[0].lower() == tag.strip().lower() and f.lower().endswith(('.png', '.jpg', '.jpeg')):
                found_file = f
                break
        
        if found_file:
            full_path = os.path.join(CHART_PATH, found_file)
            found_images.append(full_path)           
            image_count += 1
            
            # กระบวนการเปลี่ยนชื่อไฟล์จากเช่น Emini_260320_Friday ให้เป็น Emini_Friday 20 March 2026
            transformed_name = os.path.splitext(found_file)[0]
            
            # ลบวันด้านหลังของชื่อออก ให้เหลือเฉพาะแต่ชื่อด้านหน้าเช่น Emini_260320
            for day in ["_Monday", "_Tuesday", "_Wednesday", "_Thursday", "_Friday", "_Saturday", "_Sunday"]:
                if day in transformed_name:
                    transformed_name = transformed_name.replace(day, "")
            
            # ดึงวันที่จาก unit_name (คาดหวังรูปแบบ YYMMDD เป็นเลขหกตัว) และดึงตัวเลข 6 ตัวมาทุกชุด 
            date_matches = re.findall(r'\d{6}', transformed_name)
            
            for date_str in set(date_matches):
                try:
                    # แปลง YYMMDD เป็น Object วันที่
                    date_obj = datetime.strptime(date_str, "%y%m%d")
            
                    # ฟอร์แมตวันที่เป็น %A (ชื่อวันเต็ม) %d (ตัวเลขวันที่) %B (ชื่อเดือนแบบเต็ม) %Y (ชื่อปีแบบเต็ม) เช่น Friday 20 March 2026
                    formatted_date = date_obj.strftime("%A %d %B %Y")
            
                    # และสร้างข้อความชุดใหม่ที่จะเอาไปวางแทนที่เดิม เช่น ชื่อ unit_name คือ Emini_260320_Friday จะกลายเป็น Emini_Friday 20 March 2026
                    # และถ้าชื่อเป็น Connected_Emini_260319-260320 จะกลายเป็น Connected_Emini_Thursday 19 March 2026-Friday 20 March 2026
                    transformed_name = transformed_name.replace(date_str, formatted_date)
                except ValueError:
                    continue            # ถ้าไม่ใช่รูปแบบวันที่ที่ถูกต้อง ให้ใช้ชื่อเดิม
            
            system_note += f"IMAGE {image_count} is {transformed_name}\n"
    
    print(f"\n{len(found_images)} image(s) found from input.")
    print(f"Image: {[os.path.splitext(os.path.basename(path))[0] for path in found_images]}\n")
    # ส่งค่ากลับไป 2 อย่าง: 
    # 1. รายชื่อไฟล์ภาพพร้อม Path 
    # 2. ข้อความ System Note เพื่อบอกลำดับภาพให้ David   
    return found_images, system_note


def get_token_count(messages):
    """คำนวณ Token ทั้งหมดจากข้อความและรูปภาพตามมาตรฐาน Qwen"""
    if not tokenizer:
        return 0, 0
    
    text_tokens = 0
    image_tokens = 0
    
    for m in messages:
        # นับ Text Tokens (รวม System Prompt และ User Input)
        text_tokens += len(tokenizer.encode(m.get('content', "")))
        
        # นับ Image Tokens จากขนาดพิกเซลจริง
        if 'images' in m:
            for img_path in m['images']:
                try:
                    with Image.open(img_path) as img:
                        w, h = img.size
                        # สูตรประเมิน Patch ของ Qwen (28x28 pixels per patch)
                        patches = (h // 28) * (w // 28)
                        image_tokens += patches
                except:
                    image_tokens += 1000 # ค่า Default หากเปิดไฟล์ไม่ได้
                    
    return text_tokens, image_tokens


def update_token_history(new_input, new_output):
    total_tokens_file = os.path.join(BASE_DIR, "total_tokens.txt")
    month_label = datetime.now().strftime("[%B %Y]")  # เช่น [April 2026]
    
    # 1. อ่านข้อมูลเดิมจากไฟล์ (ถ้ามี)
    lines = []
    if os.path.exists(total_tokens_file):
        with open(total_tokens_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

    # 2. ตรวจสอบและอัปเดตข้อมูลเดือนปัจจุบัน
    found = False
    new_lines = []
    
    for line in lines:
        if line.startswith(month_label):
            # ใช้ Regex ดึงตัวเลขเดิมออกมาบวกเพิ่ม
            match = re.search(r"Total Input Tokens = (\d+), Total Output Tokens = (\d+)", line)
            old_input = int(match.group(1)) if match else 0
            old_output = int(match.group(2)) if match else 0
                
            updated_input = old_input + new_input
            updated_output = old_output + new_output
                
            new_lines.append(f"{month_label}: Total Input Tokens = {updated_input}, Total Output Tokens = {updated_output}, Total Tokens = {updated_input + updated_output}\n")
            found = True
        else:
            new_lines.append(line)     # เก็บเดือนอื่นไว้เหมือนเดิม
    
    # 3. ถ้ายังไม่มีเดือนนี้เลย ให้สร้างบรรทัดแรกของเดือน
    if not found:
        new_lines.append(f"{month_label}: Total Input Tokens = {new_input}, Total Output Tokens = {new_output}, Total Tokens = {new_input + new_output}\n")
    
    # 4. เขียนข้อมูลทั้งหมดกลับลงไฟล์
    with open(total_tokens_file, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

 
def save_image_to_brain(vectorstore, category, description, image_file):
    file_path = os.path.join(CHART_PATH, image_file)
    
    # สร้าง Vector จากภาพด้วย CLIP (CPU)
    img_vector = image_embed.encode(Image.open(file_path)).tolist()
    
    doc = Document(page_content=description, metadata={"category": category, "source": image_file, "path": file_path})
    
    # เตรียม Wrapper เพื่อให้ FAISS ใช้งานได้ (ใช้ค่าหลอกเพราะเราส่ง Vector ตรง)
    class SimpleEmbed:
        def embed_query(self, text): return [0] * 512
    
    if os.path.exists(os.path.join(IMAGE_BRAIN_PATH, "image_index.faiss")):
        vectorstore.add_embeddings(zip([doc.page_content], [img_vector]), metadatas=[doc.metadata])
    else:
        vectorstore = FAISS.from_embeddings(zip([doc.page_content], [img_vector]), SimpleEmbed(), metadatas=[doc.metadata])

    vectorstore.save_local(IMAGE_BRAIN_PATH)


def save_text_to_brain(vectorstore, content, text_file):
    # 1. บันทึกข้อมูลเป็น Text File ลงโฟลเดอร์ journal
    file_path = os.path.join(JOURNAL_PATH, f"{text_file}.txt")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    
    # 2. หั่นเนื้อหา (Split) ก่อนบันทึกลง FAISS เพื่อป้องกัน Error 400
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
    
    # 3. สร้าง Document ชั่วคราวขึ้นมาด้วย Tag 'KNOWLEDGE' และหั่นออกมาเป็นชิ้นเล็กๆ (Chunks)
    temp_doc = Document(page_content=content, metadata={"category": "KNOWLEDGE", "source": text_file})
    docs = text_splitter.split_documents([temp_doc])
    
    # 4. บันทึกลง FAISS 
    vectorstore.add_documents(docs)
    vectorstore.save_local(TEXT_BRAIN_PATH)
 

def create_brain():    
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
    text_vectorstore = None
    image_vectorstore = None
    
    # --- ส่วนที่ 1: รับทฤษฎี (THEORY) จากการอ่านหนังสือ (BOOKS) ---
    if os.path.exists(BOOK_PATH):
        books = [f for f in os.listdir(BOOK_PATH) if f.endswith(".pdf")]
    
        if books:
            print("David starts reading books...")
            theory_docs = []
            
            for i, file in enumerate(books):
                print(f"Loading no. {i+1}/{len(books)}: {file} ...")      
                loader = PyPDFLoader(os.path.join(BOOK_PATH, file))
                
                if loader:
                    data = loader.load()
                    
                    for d in data:
                        d.metadata["category"] = "THEORY"
                    
                    theory_docs.extend(data)
                    print(f"Successfully loaded {file} (Total {len(data)} Pages)")
                else:
                    print("Error loading no. {i+1}: {file}")
    
            if theory_docs:
                print("Text splittering...")   
                splits = splitter.split_documents(theory_docs)
                
                print(f"Start embedding {len(splits)} pieces...")
                text_vectorstore = FAISS.from_documents(splits, HybridEmbeddings)
                
                if text_vectorstore:
                    print("Brain creation from books is successful.\n")
                else
                    print("Failed to create brain from books.\n")
        else:
            print("Error: No books found!\n")
            return None
    
    # --- ส่วนที่ 2: รับประสบการณ์ (EXPERIENCES) จากบันทึกบทสนทนา text_journal ---
    if os.path.exists(JOURNAL_PATH) and text_vectorstore:
        journals = [f for f in os.listdir(JOURNAL_PATH) if f.lower().endswith(".txt")]
    
        if journals:
            print(f"Absorbing knowledge from journals... [{len(journals)} unit(s)]")
            journal_docs = []
            
            for file in journals:
                loader = TextLoader(os.path.join(JOURNAL_PATH, file), encoding='utf-8')
                data = loader.load()
                
                for d in data:
                    d.metadata["category"] = "KNOWLEDGE"
                
                journal_docs.extend(data)
            
            if journal_docs:
                print("Successfully loading text journals")
                print("Text splittering...")        
                splits = splitter.split_documents(journal_docs)
                
                print(f"   Adding {len(splits)} knowledge chunks to brain...")
                text_vectorstore.add_documents(splits)
                
                print("Knowledge successfully absorbed.\n")
            else:
                print("Error in absorbing knowledge.\n")
        else:
            print("No knowledge found.\n")
    
    # --- ส่วนที่ 4: รับฐานข้อมูล PATTERN และประสบการณ์ (KNOWLEDGE) จากบันทึกภาพ image_journal ---
    if os.path.exists(CHART_PATH) and os.path.exists(LESSON_PATH) and text_vectorstore:
        chart_file = {os.path.splitext(f)[0]: f for f in os.listdir(CHART_PATH) if f.endswith(('.png', '.jpg'))}
        content_file = {os.path.splitext(f)[0] for f in os.listdir(LESSON_PATH) if f.endswith('.txt')}
    
        # จับคู่ไฟล์ที่มีชื่อเหมือนกัน
        pairs = sorted(list(chart_file.keys() & content_file))
        
        print(f"Absorbing pattern & knowledge from charts... [{len(pairs)} unit(s)]")

        chart_docs = []
        metadatas_list = []
        
        for unit in pairs:
            chart_path = os.path.join(CHART_PATH, chart_file[unit])
            with open(os.path.join(LESSON_PATH, f"{unit}.txt"), "r", encoding="utf-8") as f:
                content_text = f.read()

            # แปลงภาพเป็น Vector โดยใช้ CLIP
            chart_vector = image_embed.encode(Image.open(chart_path)).tolist()
               
            # สร้าง Document หลอกเพื่อเก็บ Vector และ Metadata ของภาพลงใน FAISS
            doc = Document(
                page_content=content_text,
                metadata={"category": "PATTERN", "source": unit, "path": chart_path}
            )
            # เพิ่มคู่ (Document, Vector) ลงใน List เพื่อเตรียมเข้า FAISS
            chart_docs.append((doc.page_content, chart_vector))
            
            # ทำการเพิ่ม Vector ของภาพเข้าไปในคลังสมอง
            # หมายเหตุ: เราใช้เทคนิคการ Map Vector ตรงๆ เพื่อความแม่นยำของ Visual Search
            texts, vecs = zip(*chart_docs)
            
            image_vectorstore = FAISS.from_embeddings(
                zip(texts, vecs), 
                HybridEmbeddings(), # ใส่ไว้เป็น placeholder แต่เราใช้ vector โดยตรง
                metadatas=[{"path": chart_path}]
            )
            
            image_vectorstore.save_local(IMAGE_BRAIN_PATH)
            
            if not image_vectorstore:
                print("Error: Failed Image Vectorization!\n")

            

            for unit in pairs:
                chart_file, journal_file = unit
                chart_path = os.path.join(CHART_PATH, chart_file)
                journal_path = os.path.join(JOURNAL_PATH, journal_file)
                
                with open(journal_path, 'r', encoding='utf-8') as f:
                    content_text = f.read()

                # แปลงภาพเป็น Vector โดยใช้ CLIP
                chart_vector = image_embed.encode(Image.open(chart_path)).tolist()
                   
                # สร้าง Document หลอกเพื่อเก็บ Vector และ Metadata
                doc = Document(
                    page_content=content_text,
                    metadata={"category": "PATTERN", "source": unit, "path": chart_path}
                )
                
                # สะสมข้อมูลลงใน List
                chart_docs.append((doc.page_content, chart_vector))
                metadatas_list.append(doc.metadata)
                print(f"Collected: {chart_file}")

            # --- ย้ายชุดคำสั่งประมวลผลและบันทึกออกมาไว้นอก Loop ---
            if chart_docs:
                texts, vecs = zip(*chart_docs)
                
                image_vectorstore = FAISS.from_embeddings(
                    zip(texts, vecs), 
                    HybridEmbeddings(), 
                    metadatas=metadatas_list
                )
                
                image_vectorstore.save_local(IMAGE_BRAIN_PATH)
                print("Successfully updated Image Brain with all patterns.")



    
    # --- เช็คว่าอย่างน้อยต้องมี THEORY ที่ได้จาก Books ---
    if text_vectorstore:
        return text_vectorstore, image_vectorstore
    else:
        print("No data found to build brain!!")
        return None


if __name__ == "__main__":
    main()