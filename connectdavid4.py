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
from langchain_core.embeddings import Embeddings
from datetime import datetime
from PIL import Image
from transformers import AutoTokenizer
from sentence_transformers import SentenceTransformer

# --- Path Configuration ---
BASE_DIR = r"C:\david"
BOOK_PATH = os.path.join(BASE_DIR, "book")
PATTERN_PATH = os.path.join(BASE_DIR, "pattern")
KNOWLEDGE_PATH = os.path.join(BASE_DIR, "knowledge")
OHLC_PATH = os.path.join(BASE_DIR, "ohlc")
JOURNAL_PATH = os.path.join(BASE_DIR, "journal")
TOKENIZER_PATH = os.path.join(BASE_DIR, "tokenizer")
TEXT_MODEL_PATH = os.path.join(BASE_DIR, "text_embeddings")
IMAGE_MODEL_PATH = os.path.join(BASE_DIR, "image_embeddings")
BRAIN_PATH = os.path.join(BASE_DIR, "brain")
MODEL_NAME = "gemma4:26b"
TEXT_EMBEDDING_MODEL = "nomic-ai/nomic-embed-text-v1.5"
IMAGE_EMBEDDING_MODEL = "clip-ViT-B-32"
 
for p in [BOOK_PATH, PATTERN_PATH, KNOWLEDGE_PATH, OHLC_PATH, JOURNAL_PATH, TOKENIZER_PATH, TEXT_MODEL_PATH, IMAGE_MODEL_PATH, BRAIN_PATH]:
    os.makedirs(p, exist_ok=True)

def load_model_local(model_name, local_path, device='cpu'):
    # ตรวจสอบว่ามีโฟลเดอร์และไฟล์โมเดลอยู่แล้วหรือไม่
    if os.path.exists(local_path) and os.listdir(local_path):
        print(f"Loading {model_name} from local directory...")
        # โหลดจากเครื่องโดยตรง ไม่ต้องเช็ค update จากเน็ต
        return SentenceTransformer(local_path, device, trust_remote_code=True)
    else:
        print(f"Downloading {model_name} from Hugging Face Hub...")
        # ดาวน์โหลดครั้งแรก
        model = SentenceTransformer(model_name, device, trust_remote_code=True)
        # บันทึกลงเครื่องเพื่อใช้ครั้งต่อไป
        model.save(local_path)
        print(f"Model saved to {local_path}")
        return model

text_embed = load_model_local(TEXT_EMBEDDING_MODEL, TEXT_MODEL_PATH, device='cpu')
image_embed = load_model_local(IMAGE_EMBEDDING_MODEL, IMAGE_MODEL_PATH, device='cpu')

# สร้าง Class เพื่อให้ FAISS (LangChain) เรียกใช้ EMBEDDING MODELS ได้
class HybridEmbeddings(Embeddings):
    def __init__(self, model):
        self.model = model
    def embed_documents(self, texts):
        return self.model.encode(texts).tolist()
    def embed_query(self, text):
        return self.model.encode([text])[0].tolist()

# สร้าง Instance แยกสำหรับ Text และ Image เพื่อป้องกันการสลับรุ่น Model
Text_Embeddings = HybridEmbeddings(text_embed)
Image_Embeddings = HybridEmbeddings(image_embed)

try:
    # พยายามโหลด tokenizer จาก local ใน TOKENIZER_PATH
    tokenizer = AutoTokenizer.from_pretrained(TOKENIZER_PATH, local_files_only=False)
except Exception:
    # ถ้าไม่มี ให้โหลดจากออนไลน์ครั้งแรกและเซฟลงเครื่อง
    tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-7B-Instruct")
    tokenizer.save_pretrained(TOKENIZER_PATH)

SYSTEM_PROMPT = """You are David, an AI assistant specialized in Al Brooks Price Action.
Your role is to learn Price Action from a user, Wei. You use three books of Al Brooks as references on trading terminology and methodology.
Wei will provide you more charts with explanation to learn from time to time in order to teach you.
You must learn to memorize many important patterns to evaluate the meaning of any chart I will ask you in the future.
Wei will test you to see your development until you are ready to be his co-pilot in trading.

Moreover, before giving the final answer(output), please provide a brief internal monologue or step-by-step reasoning explaining how you interpreted the request and which parts of the retrieved context you are using to formulate your response.

Finally, when I give the command: 'David, provide the [FINAL_SUMMARY] for your brain now.', you must provide the output strictly in the following format:

[FINAL_SUMMARY]:
[Your detailed summary conclusion here]§

Important: Do not include any introductory text or conversational filler before '[FINAL_SUMMARY]:'. The character '§' must be the very last character of your entire response.
Do not mention the string '[FINAL_SUMMARY]' anywhere in your thoughts or internal monologue. Use the term 'final conclusion' instead if you need to refer to it.
The tag '[FINAL_SUMMARY]:' must appear only once in your entire response, acting as the starting header for your final output.

CRITICAL RULE: You must answer something based on your found context and logic before getting the advise from Wei. Even if it is wrong, Wei will explain to you later. Give the best answer you can!"""

# --- David's Brain & Reasoning Configuration ---
options_config = {
    'num_ctx': 16384,             # ขนาดสมองรวม Total Tokens/Input (จำได้ยาวขึ้น)
    'num_predict': 4096,          # เพดานคำพูด Output Tokens (ถ้าไม่เกินก็ไม่ให้โดนตัดบทดื้อๆ)
    'num_thread': 12,             # บังคับใช้ CPU Cores ตามจำนวน Performance-cores 12 threads ของ Intel(R) Core(TM) i5-14600K (3.50 GHz)
#    'num_gpu': 28,                # ใช้ GPU แค่ 28 layers จากทั้งหมด 30 layers สำหรับ Gemma4-26B
#    "kv_cache_type": "q4_0",      # ใช้ KV Cache แบบบีบขนาดเหลือ Q4 เพื่อลด VRAM
    'temperature': 0.1,           # คงความแม่นยำในการตอบ
    'repeat_penalty': 1.2,        # ป้องกันการพูดวนไปวนมา (แก้ปัญหาการ Loop)
    'top_k': 20,                  # จำกัดทางเลือกคำให้คมขึ้น
    'top_p': 0.5,                 # เน้นคำตอบที่มีความน่าเชื่อถือสูง
}

total_images_list: list[str] = []
total_input_tokens = 0
total_output_tokens =  0

def main():
    global total_images_list
    global total_input_tokens
    global total_output_tokens
    
    text_vectorstore = None
    image_vectorstore = None

    # เช็คว่ามีไฟล์สมอง FAISS ทั้ง text และ image อยู่แล้วหรือไม่ (อย่างน้อยต้องมี text_index.faiss ที่เกิดจากการอ่าน Books ถ้าไม่มีก็ให้สร้างสมองขึ้นมาใหม่)
    if os.path.exists(os.path.join(BRAIN_PATH, "text_index.faiss")):
        text_vectorstore = FAISS.load_local(BRAIN_PATH, Text_Embeddings, allow_dangerous_deserialization=True, index_name="text_index")       
        
        if os.path.exists(os.path.join(BRAIN_PATH, "image_index.faiss")):
            image_vectorstore = FAISS.load_local(BRAIN_PATH, Image_Embeddings, allow_dangerous_deserialization=True, index_name="image_index")
    else:
        text_vectorstore, image_vectorstore = create_brain()
        if text_vectorstore is None:    # ตรวจสอบว่าอย่างน้อยต้องมี THEORY จาก Books
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
            is_knowledge_name = ""
            is_question_name = ""
            
            print("\n--- David is online ---")
            print("** Asking any questions by using '?' **")
            print("** For PATTERN, type 'p:file_name' **")
            print("** For KNOWLEDGE, type 'k:file_name' or entering text knowledge without '?' **")
            print("** Type 'exit'/'quit'/'bye' to terminate **\n")
            user_input = input("Wei: ").strip()
            
            if user_input.strip().lower() in ['exit', 'quit', 'bye']:
                break
            
            # ตรวจสอบดูว่ารูปแบบ PATTERN ที่ต้องจดจำ โดยจะ load คำสอนจาก text file หรือไม่
            if user_input.lower().startswith("p:"):
                mode_text = "David is memorizing the pattern...\n"
                response_text = "[PATTERN_ACKNOWLEDGEMENT]:\n"
                last_text = "David, memorize this pattern which is derived from Wei's experiences and explain briefly if you understand it."
                
                file_name = user_input.replace("p:", "").strip()
                if not file_name.lower().endswith('.txt'):
                    file_name += ".txt"
                
                file_path = os.path.join(PATTERN_PATH, file_name)
                if os.path.exists(file_path):
                    with open(file_path, "r", encoding="utf-8") as f:
                        header = f.readline().strip()     # อ่าน Header [category:xxx]
                        user_input = f.read().strip()     # อ่านเนื้อหาสรุปที่เหลือ
                        
                        if not header and not user_input:
                            print("!!! --- No data in text file --- !!!\n")
                            continue
                        
                        if header:
                            match = re.search(r"\[category:\s*(.*?)\]", header, re.IGNORECASE)
                            if match:
                                tmp_category = match.group(1).strip()
                            else:
                                print("!!! --- No specific name of PATTERN --- !!!")

                                while True:
                                    try:
                                        tmp_category = input("Enter the name of PATTERN: ").strip()
                                        if not tmp_category:
                                            continue
                    
                                        input_condition = input(f"Confirm the name '{tmp_category}'? (y/n): ").lower().strip()
                                        if input_condition == "y":
                                            break
                                        else:
                                            continue

                                    except Exception as e:
                                        print(f"Error: {e}")
                                        continue
                            
                                header = "[category: " + tmp_category + "]\n"
                                f.seek(0)
                                user_input = f.read().strip()       # อ่านเนื้อหาใหม่อีกครั้งหนึ่ง
                            
                                with open(file_path, "w", encoding="utf-8") as g:
                                    g.write(header + user_input)
                        
                            if user_input:
                                category = tmp_category
                                save_type = "image"
                                is_pattern = True
                            else:
                                print("!!! --- No details in text file --- !!!\n")
                                continue
                        else:
                            print("!!! --- Corrupted text file --- !!!\n")
                            continue
                
                    summary_content = f"[PATTERN_EXPLANATION]:\n{user_input}\n\n"
                else:
                    print("!!! --- Unable to find text file --- !!!\n")
                    continue

            # ตรวจสอบว่าเป็นเป็นการให้ KNOWLEDGE โดยจะ load คำสอนจาก text file หรือไม่
            elif user_input.lower().startswith("k:"):
                mode_text = "David is absorbing your knowledge...\n"
                response_text = "[KNOWLEDGE_ACKNOWLEDGEMENT]:\n"
                last_text = "David, acknowledge this knowledge which is derived from Wei's experiences and explain briefly if you understand it."

                file_name = user_input.replace("k:", "").strip()
                if not file_name.lower().endswith(".txt"):
                    file_name += ".txt"
                        
                file_path = os.path.join(KNOWLEDGE_PATH, file_name)
                if os.path.exists(file_path):
                    with open(file_path, "r", encoding="utf-8") as f:
                        user_input = f.read()
                        if user_input == "":
                            print("\n!!! --- No data in text file --- !!!\n")
                            continue
                    summary_content = f"[KNOWLEDGE_FROM_WEI]:\n{user_input}\n\n"
                else:
                    print("\n!!! --- Unable to find text file --- !!!\n")
                    continue
                
                match = re.search(r"\[image:\s*(.*?)\]", user_input)
                if match:
                    image_name = match.group(1).strip()
                    is_knowledge_name = os.path.splitext(image_name)[0]
                    save_type = "image"
                else:
                    while True:
                        try:
                            is_knowledge_name = input("Name of KNOWLEDGE: ").strip()
                            if not is_knowledge_name:
                                continue
            
                            input_condition = input(f"Confirm the name '{is_knowledge_name}'? (y/n): ").lower().strip()
                            if input_condition == "y":
                                break
                            else:
                                continue
                        
                        except Exception as e:
                            print(f"Error: {e}")
                            continue
            
            # ตรวจสอบดูว่าเป็นชุดคำถามหรือบททดสอบหรือไม่ ถ้าใช่ จะไม่มีการบันทึกลงใน vectorstore
            elif "?" in user_input:
                mode_text = "David is thinking...\n"
                response_text = "[DAVID_ANALYSIS]:\n"
                
                summary_content = f"[QUESTION_FROM_WEI]:\n{user_input}\n\n"

                is_question_name = datetime.now().strftime("%y%m%d_%H%M")
            
            # ถ้าเป็น input ทั่วไปคือ knowledge ที่จะ save ใน text_vectorstore
            else:
                mode_text = "David is absorbing your knowledge...\n"
                response_text = "[DAVID_ACKNOWLEDGEMENT]:\n"
                last_text = "David, acknowledge this knowledge which is derived from Wei's experiences and explain briefly if you understand it."
              
                summary_content = f"[KNOWLEDGE_FROM_WEI]:\n{user_input}\n\n"
                
                while True:
                    try:
                        is_knowledge_name = input("Name of KNOWLEDGE: ").strip()
                        if not is_knowledge_name:
                            continue
            
                        input_condition = input(f"Confirm the name '{is_knowledge_name}'? (y/n): ").lower().strip()
                        if input_condition == "y":
                            break
                        else:
                            continue
                        
                    except Exception as e:
                        print(f"Error: {e}")
                        continue

            # ค้นหา THEORY และ KNOWLEDGE ที่เกี่ยวข้องกับ user_input
            knowledge_content = get_matched_knowledge(text_vectorstore, user_input, 10)
            if knowledge_content.strip():
                print("\nKnowledge contents was found.")      # print(f"\n[EXPERIENCE_FOUND]:\n{knowledge_content}\n")
                prompt_content = f"[THEORY_AND_KNOWLEDGE_FROM_MEMORY]:\n{knowledge_content}\n\n"
            else:
                print("\nKnowledge contents could not be found.")
            
            # ค้นหาภาพ(ถ้ามี)ใน user_input เพื่อนำมาประกอบใน content
            found_images, system_note = find_images_in_content(user_input, is_pattern)
            if found_images:
                summary_content += system_note
                
                matched_ohlc = fetch_OHLC(found_images, start=1)
                for str_ohlc in matched_ohlc:
                    summary_content += str_ohlc

                if is_pattern == True:
                    if image_vectorstore != None:
                        # ถ้าเป็นการเรียนรู้ PATTERN ใหม่ ให้ดึงฐานข้อมูล PATTERN เดิม 3 รูปที่ใกล้เคียงที่สุด
                        prompt_content += f"Matched patterns for [IMAGE 1]: {found_images[0]}\n\n---\n\n"
                        prompt_content += find_matched_pattern(image_vectorstore, found_images[0], k=3, distance_threshold=0.3)
                        prompt_content += "\n\n---\n\n"
                else:
                    if image_vectorstore != None:
                        # ถ้าเป็นการเรียนรู้ KNOWLEDGE ที่เป็นภาพซับซ้อน หรือคำถามที่เป็นบททดสอบ ให้ดึงฐานข้อมูล PATTERN 10 รูปเพื่อให้ครอบคลุมหลากหลาย PATTERN
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
                        response_text = "[FINAL_ANALYSIS]:\n"
                
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
                            final_summary = re.search(r"(^\[FINAL_SUMMARY\]:.*?§)", final_analysis, re.DOTALL | re.IGNORECASE| re.MULTILINE)
                            if final_summary:
                                summary_content += final_summary.group(1).strip()
                            else:
                                print("No final summary from David.")
                                continue
                        
                        full_content += final_analysis

                        if save_type == "text":
                            if not is_question_name:
                                txt_file = is_knowledge_name + ".txt"
                                knowledge_path = os.path.join(KNOWLEDGE_PATH, txt_file)
                                text_vectorstore = save_text_to_brain(text_vectorstore, summary_content, full_content, knowledge_path)
                            else:
                                txt_file = is_question_name + ".txt"
                                journal_path = os.path.join(JOURNAL_PATH, txt_file)
                                with open(journal_path, "w", encoding="utf-8") as f:
                                    f.write(full_content)

                        if save_type == "image":
                            img_file = os.path.basename(total_images_list[0])
                            if is_pattern == True:
                                img_path = os.path.join(PATTERN_PATH, img_file)
                            else:
                                img_path = os.path.join(KNOWLEDGE_PATH, img_file)               
                            image_vectorstore = save_image_to_brain(image_vectorstore, category, summary_content, full_content, img_path)                           
                        
                        print("Knowledge was memorized successfully.")
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
                        r_found_images, r_system_note = find_images_in_content(correction_input, is_pattern)
                        if r_found_images:
                            # เพิ่มไฟล์ภาพเพิ่มเติม(ถ้ามี)ใน correction input โดยสร้างรวมกับภาพเดิมที่เคยคุยกันใน session แรกด้วย
                            temp_images_list += total_images_list + r_found_images
                            refined_prompt_content += r_system_note

                            matched_ohlc = fetch_OHLC(r_found_images, start=len(total_images_list)+1)
                            for str_ohlc in matched_ohlc:
                                refined_prompt_content += str_ohlc
                            
                            if is_pattern == True:
                                if image_vectorstore != None:
                                    for i, img in enumerate(r_found_images, start=len(total_images_list)+1):
                                        refined_prompt_content += f"Matched patterns for [IMAGE {i}]: {img}\n\n---\n\n"
                                        refined_prompt_content += find_matched_pattern(image_vectorstore, img, k=3, distance_threshold=0.3)
                                        refined_prompt_content += "\n\n---\n\n"
                                else:
                                    for i, img in enumerate(r_found_images, start=len(total_images_list)+1):
                                        refined_prompt_content += f"Matched patterns for [IMAGE {i}]: {img}\n\n---\n\n"
                                        refined_prompt_content += find_matched_pattern(image_vectorstore, img, k=10, distance_threshold=0.5)
                                        refined_prompt_content += "\n\n---\n\n"
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

                        refined_analysis = david_chat(refined_prompt, mode_text, response_text)
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
    global total_input_tokens
    global total_output_tokens
    
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
        ok_to_config = input("Ok to fetch OHLC for charts? (y/n): ").strip().lower()
        if ok_to_config == "y":
            break
        elif ok_to_config == "n":
            return None
        else:
            continue

    ohlc_list = []
    
    # วน Loop ตามจำนวนภาพที่พบ โดยใช้ enumerate เพื่อระบุลำดับภาพ
    for i, img in enumerate(image_list, start=start):
        image_name = os.path.basename(img)
        print(f"\n--- Configuration of [IMAGE {i}: {image_name}] ---")
        
        # รับค่า Symbol และ Exchange
        symbol = input("Symbol: ").strip()
        exchange = input("Exchange: ").strip()
    
        # รับค่า Timeframe
        timeframe = input("Timeframe (1m/5m/15m/1h/1d/1w): ").strip().lower()
    
        # รับค่า Start_time และ End_time
        date = input("Date (YYYY-MM-DD): ").strip()
        tmp_start_time = input("Start time (HH:MM): ").strip()
        tmp_end_time = input("End time (HH:MM): ").strip()
    
        start_time = f"{date} {tmp_start_time}"
        end_time = f"{date} {tmp_end_time}"

        ohlc_file = f"{symbol}-{exchange}-{timeframe}.csv"
        ohlc_path = os.path.join(OHLC_PATH, ohlc_file)
        if not os.path.exists(ohlc_path):
            print(f"Error: Unable to find OHLC data file for IMAGE {i}, Skipping...")
            continue

        print("\nFetching OHLC...")
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

        # แยกคอลัมน์ 'Message' ออกเป็น 4 คอลัมน์ (Open, High, Low, Close) และแปลงให้เป็นตัวเลข (float) ทันที
        df[['open', 'high', 'low', 'close']] = df['Message'].str.split(',', expand=True).astype(float)

        # ลบคอลัมน์ Message เดิมทิ้ง
        df.drop(columns=['Message'], inplace=True)

        # ดึงข้อมูลในช่วง start_time ถึง end_time, Pandas จะดึงข้อมูลทุกแท่งที่อยู่ในช่วงเวลานี้มาให้ทั้งหมด
        df_filtered = df.loc[start_time : end_time]

        # ตรวจสอบว่ามีข้อมูลในช่วงที่ระบุหรือไม่
        if df_filtered.empty:
            print(f"!!! No data found between {start_time} and {end_time} !!!\n")
        else:
            print(f"Successfully fetched {len(df_filtered)} bars\n")
        
        # ใช้ index=True หากเวลาของคุณอยู่ที่ Index
        ohlc_reference = df_filtered[['open', 'high', 'low', 'close']].to_string(index=True)

        # รับค่าตำแหน่ง Bar และเวลาที่สัมพันธ์กับภาพนี้
        index_bar_no = input("Choosing index bar no: ")
        tmp_time = input("Time related to index bar (HH:MM): ")
        time_related_bar = date + " " + tmp_time
                                
        # สะสมข้อมูลเข้าใน ohlc_content โดยระบุว่าอ้างอิงกับ Image ลำดับที่เท่าไหร่
        ohlc_content = f"\n[OHLC data for IMAGE {i}]:\n"
        ohlc_content += ohlc_reference + "\n"
        ohlc_content += "=" * 10 + "\n"
        ohlc_content += f"Bar No.{index_bar_no} is at {time_related_bar}. You shall count the rest of the bars by yourself.\n\n"
        ohlc_list.append(ohlc_content)
        
    return ohlc_list


# ฟังก์ชั่นค้นหาชื่อภาพในข้อความ text_to_be_searched และคืนค่า List ของ Path รูปภาพที่พบ
def find_images_in_content(text_to_be_searched, is_pattern):
    found_images = []       # จุดรวมชื่อภาพที่หาเจอพร้อมตำแหน่ง Directory ที่ค้นหาจาก text_to_be_searched
    system_note = "[System Note]:\n"    # ข้อความสำหรับสร้าง System Note เพื่อบอก David เกี่ยวกับลำดับภาพ
    image_count = len(total_images_list)
    IMAGE_PATH = ""

    if is_pattern == True:
        IMAGE_PATH += PATTERN_PATH
    else:
        IMAGE_PATH += KNOWLEDGE_PATH

    #  ค้นหาชื่อภาพ Symbol_YYMMDD_Day ในวงเล็บเหลี่ยม [] ที่ขึ้นต้นด้วย "image:
    #  \s* คือ User จะพิมพ์โดยมีช่องว่างหรือไม่มีช่องว่างก็ได้
    #  (.*?) ให้ return ค่าค้นหา โดยเริ่มนับคำหลังจาก "[image:" และให้จบคำนั้นหลังจากเจอ "]"
    tags = re.findall(r"\[image:\s*(.*?)\]", text_to_be_searched)                                                    #
    if not tags:
        return [], ""

    try:
        # รายชื่อไฟล์ภาพทั้งหมด
        all_files = os.listdir(IMAGE_PATH)
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
            full_path = os.path.join(IMAGE_PATH, found_file)
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

 
def save_image_to_brain(vectorstore, category, summary_content, full_content, file_path):
    image_name = os.path.splitext(os.path.basename(file_path))[0]    # ไฟล์ไม่มี ext
    
    # สร้าง Vector จากภาพด้วย CLIP (CPU)
    img_vector = image_embed.encode(Image.open(file_path)).tolist()
    
    img_metadata = {"category": category, "source": image_name, "path": file_path}
    doc = Document(page_content=summary_content, metadata=img_metadata)
    
    if os.path.exists(os.path.join(BRAIN_PATH, "image_index.faiss")):
        vectorstore.add_embeddings(zip([doc.page_content], [img_vector]), metadatas=[doc.metadata])
    else:
        vectorstore = FAISS.from_embeddings(zip([doc.page_content], [img_vector]), Image_Embeddings, metadatas=[doc.metadata])

    vectorstore.save_local(BRAIN_PATH, index_name="image_index")

    final_txt_content = ""
    # ถ้าไม่ใช่ KNOWLEDGE แสดงว่าเป็น PATTERN ให้ใส่ Header [category:xxx]
    if category.upper() != "KNOWLEDGE":
        final_txt_content = f"[category: {category}]\n{summary_content}"
    else:
        final_txt_content = summary_content

    # บันทึก Summary Content ลงในไฟล์ต้นทาง (ทับไฟล์เดิมใน pattern หรือ knowledge folder)
    txt_source_path = os.path.splitext(file_path)[0] + ".txt"
    with open(txt_source_path, "w", encoding="utf-8") as f:
        f.write(final_txt_content)
    
    # บันทึก full content ลงใน journals
    txt_journal = image_name + ".txt"
    journal_path = os.path.join(JOURNAL_PATH, txt_journal)
    with open(journal_path, "w", encoding="utf-8") as f:
        f.write(full_content)
        
    return vectorstore
    

def save_text_to_brain(vectorstore, summary_content, full_content, file_path):
    txt_name = os.path.splitext(os.path.basename(file_path))[0]
    
    # 1. บันทึกข้อมูลเป็น Text File ลงโฟลเดอร์ journal
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(summary_content)
    
    # 2. หั่นเนื้อหา (Split) ก่อนบันทึกลง FAISS เพื่อป้องกัน Error 400
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
    
    txt_metadata = {"category": "KNOWLEDGE", "source": txt_name}
    # 3. สร้าง Document ชั่วคราวขึ้นมาด้วย Tag 'KNOWLEDGE' และหั่นออกมาเป็นชิ้นเล็กๆ (Chunks)
    temp_doc = Document(page_content=summary_content, metadata=txt_metadata)
    docs = text_splitter.split_documents([temp_doc])
    
    # 4. บันทึกลง FAISS 
    vectorstore.add_documents(docs)
    vectorstore.save_local(BRAIN_PATH, index_name="text_index")

    # 5. บันทึก full content ลงใน journals
    txt_file = txt_name + ".txt"
    journal_path = os.path.join(JOURNAL_PATH, txt_file)
    with open(journal_path, "w", encoding="utf-8") as f:
        f.write(full_content)
        
    return vectorstore


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
                text_vectorstore = FAISS.from_documents(splits, Text_Embeddings)
                
                if text_vectorstore:
                    text_vectorstore.save_local(BRAIN_PATH, index_name="text_index")
                    print("Brain creation from books is successful.")
                else:
                    print("Failed to create brain from books.\n")
                    return text_vectorstore, image_vectorstore
        else:
            print("Error: No books found!\n")
            return text_vectorstore, image_vectorstore

    # --- ส่วนที่ 2: รับฐานข้อมูล PATTERN จากบันทึกภาพ ---
    if os.path.exists(PATTERN_PATH):
        save_img_vectorstore = False
        
        pattern_files = {os.path.splitext(f)[0]: f for f in os.listdir(PATTERN_PATH) if f.lower().endswith(('.png', '.jpg', '.jpeg'))}
        content_files = {os.path.splitext(f)[0] for f in os.listdir(PATTERN_PATH) if f.lower().endswith('.txt')}
    
        # จับคู่ไฟล์ที่มีชื่อเหมือนกัน
        pairs = sorted(list(pattern_files.keys() & content_files))
        
        pattern_docs = []
        metadatas_list = []
        print(f"Absorbing... [{len(pairs)} pattern(s)]")
        
        for unit in pairs:
            txt_name = unit + ".txt"
            txt_path = os.path.join(PATTERN_PATH, txt_name)
            pattern_path = os.path.join(PATTERN_PATH, pattern_files[unit])
            category = ""
            
            with open(txt_path, "r", encoding="utf-8") as f:
                header = f.readline().strip()     # อ่าน Header [category:xxx]
                content_text = f.read().strip()   # อ่านเนื้อหาสรุปที่เหลือ
            
                if not header and not content_text:
                    print(f"!!! No data in file: {txt_name}, Skipping... !!!\n")
                    continue
            
                if header:
                    match = re.search(r"\[category:\s*(.*?)\]", header, re.IGNORECASE)
                    if match:
                        tmp_category = match.group(1).strip()
                    else:
                        print(f"Error: No specific name of PATTERN of {unit}")
            
                        while True:
                            try:
                                tmp_category = input("Enter the name of PATTERN: ").strip()
                                if not tmp_category:
                                    continue
                    
                                input_condition = input(f"Confirm the name '{tmp_category}'? (y/n): ").lower().strip()
                                if input_condition == "y":
                                    break
                                else:
                                    continue

                            except Exception as e:
                                print(f"Error: {e}")
                                continue
                            
                        header = "[category: " + tmp_category + "]\n"
                        f.seek(0)
                        content_text = f.read().strip()       # อ่านเนื้อหาใหม่อีกครั้งหนึ่ง
                            
                        with open(txt_path, "w", encoding="utf-8") as g:
                            g.write(header + content_text)
            
                    if content_text:
                        category = tmp_category
                    else:
                        print("!!! No details in in file: {txt_name}, Skipping... !!!\n")
                        continue
                else:
                    print("!!! Corrupted file: {txt_name}, Skipping... !!!\n")
                    continue
            
            # แปลงภาพเป็น Vector โดยใช้ CLIP
            pattern_vector = image_embed.encode(Image.open(pattern_path)).tolist()
               
            # เตรียม Metadata สำหรับ PATTERN
            pattern_metadata = {"category": category, "source": unit, "path": pattern_path}
            # สร้าง Document หลอกเพื่อเก็บ Vector และ Metadata ของภาพลงใน FAISS
            temp_doc = Document(page_content=content_text, metadata=pattern_metadata)

            # เพิ่มคู่ (Document, Vector) ลงใน List เพื่อเตรียมเข้า FAISS
            pattern_docs.append((temp_doc.page_content, pattern_vector))
            metadatas_list.append(temp_doc.metadata)
            
        if pattern_docs:
            # ทำการเพิ่ม Vector ของภาพเข้าไปในคลังสมอง
            # หมายเหตุ: เราใช้เทคนิคการ Map Vector ตรงๆ เพื่อความแม่นยำของ Visual Search
            texts, vecs = zip(*pattern_docs)
            image_vectorstore = FAISS.from_embeddings(zip(texts, vecs), Image_Embeddings, metadatas=metadatas_list)
            if not image_vectorstore:
                print("Error: Failed Image Vectorization!\n")
            else:    
                image_vectorstore.save_local(BRAIN_PATH, index_name="image_index")
                print("Patterns absorption completed.\n")
        else:
            print("No patterns found.\n")

    # --- ส่วนที่ 3: รับความรู้ (KNOWLEDGE) จากบันทึกใน KNOWLEDGE_PATH ---
    if os.path.exists(KNOWLEDGE_PATH):
        save_txt_vectorstore = False
        save_img_vectorstore = False
        
        chart_files = {os.path.splitext(f)[0]: f for f in os.listdir(KNOWLEDGE_PATH) if f.lower().endswith(('.png', '.jpg', '.jpeg'))}
        content_files = {os.path.splitext(f)[0] for f in os.listdir(KNOWLEDGE_PATH) if f.lower().endswith('.txt')}

        # ดึงชื่อ text files ที่ไม่มีคู่ภาพประกอบ เพื่อเอา KNOWLEDGE บันทึกลงใน text brain อย่างเดียว
        not_pairs = sorted(list(content_files - chart_files.keys()))
        if not_pairs:
            print("No text knowledge files found.")
        else:
            category = "KNOWLEDGE"
            print(f"Absorbing text knowledge... [{len(not_pairs)} unit(s)]")
            # เริ่มดึงฐานข้อมูลส่วน KNOWLEDGE ที่ไม่มีภาพประกอบก่อน
            for unit in not_pairs:
                txt_name = unit + ".txt"
                txt_path = os.path.join(KNOWLEDGE_PATH, txt_name)

                with open(txt_path, 'r', encoding='utf-8') as f:
                    content_text = f.read().strip()
                
                if content_text == "":
                    print(f"-- !! No data in text file: {unit} !! , Skipping... --")
                    continue
                else:
                    # เตรียม Metadata สำหรับ text knowledge
                    txt_metadata = {"category": category, "source": txt_name, "path": txt_path}
                    temp_doc = Document(page_content=content_text, metadata=txt_metadata)
                    
                    # หั่นเนื้อหา (Chunking) ก่อนลง text brain
                    splits = splitter.split_documents([temp_doc])
                    # เพิ่ม text knowledge ลงไปใน text brain
                    text_vectorstore.add_documents(splits)
                    
                    print(f"   > Saved to Text Brain: {txt_name}")
                    save_txt_vectorstore = True

        # จับคู่ไฟล์ chart และ text file ที่มีชื่อเหมือนกัน เพื่อเอา KNOWLEDGE บันทึกลงใน image brain อย่างเดียว
        pairs = sorted(list(chart_files.keys() & content_files))
        if not pairs:
            print("No image knowledge files found.")
        else:
            category = "KNOWLEDGE"
            print(f"Absorbing image knowledge... [{len(pairs)} unit(s)]")
            # และดึงฐานข้อมูลส่วน KNOWLEDGE ที่มีภาพประกอบด้วย
            for unit in pairs:
                txt_name = unit + ".txt"
                txt_path = os.path.join(KNOWLEDGE_PATH, txt_name)
                chart_name =  chart_files[unit]
                chart_path = os.path.join(KNOWLEDGE_PATH, chart_name)
                
                with open(txt_path, 'r', encoding='utf-8') as f:
                    content_text = f.read().strip()
                
                if content_text == "":
                    print(f"-- !! No data in text file: {unit} !! , Skipping... --")
                    continue
                else:
                    # สร้าง Vector จากรูปภาพ
                    chart_vector = image_embed.encode(Image.open(chart_path)).tolist()
                        
                    # เตรียม Metadata สำหรับรูปภาพ
                    chart_metadata = {"category": category, "source": txt_name, "path": chart_path, "image_name": chart_name}
                    
                    if image_vectorstore is not None:
                        # บันทึกลง image brain (ใช้ content_text เป็นตัวกำกับ Vector)
                        image_vectorstore.add_embeddings(zip([content_text], [chart_vector]), metadatas=[chart_metadata])
                    else:
                        image_vectorstore = FAISS.from_embeddings(zip([content_text], [chart_vector]), Image_Embeddings, metadatas=[chart_metadata])
                    
                    print(f"   > Saved to Image Brain: {chart_name} (from {txt_name})")
                    save_img_vectorstore = True

        # บันทึกสถานะ Index ลง Disk
        if save_txt_vectorstore == True:
            text_vectorstore.save_local(BRAIN_PATH, index_name="text_index")
            print("\nText knowledge absorption completed.\n")
        if save_img_vectorstore == True:
            image_vectorstore.save_local(BRAIN_PATH, index_name="image_index")
            print("\nImage knowledge absorption completed.\n")
    
    return text_vectorstore, image_vectorstore


if __name__ == "__main__":
    main()