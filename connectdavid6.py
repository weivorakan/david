import os
import torch
import ollama
import pandas as pd
import sys
import re
import json
import requests
from langchain_ollama import OllamaEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from datetime import datetime
from PIL import Image
from llama_cpp import Llama
from transformers import AutoTokenizer
from sentence_transformers import SentenceTransformer
from fastapi.responses import StreamingResponse

# กำหนด URL ของ David Server (FastAPI) โดย 127.0.0.1 คือเครื่องตัวเอง Port 8000 ตามที่เราสั่ง uvicorn
SERVER_URL = "http://127.0.0.1:8000/analyze"

# --- Path Configuration ---
BASE_DIR = r"C:\david"
BOOK_PATH = os.path.join(BASE_DIR, "book")
PATTERN_PATH = os.path.join(BASE_DIR, "pattern")
KNOWLEDGE_PATH = os.path.join(BASE_DIR, "knowledge")
TEST_PATH = os.path.join(BASE_DIR, "test")
OHLC_PATH = os.path.join(BASE_DIR, "ohlc")
JOURNAL_PATH = os.path.join(BASE_DIR, "journal")
TOKENIZER_PATH = os.path.join(BASE_DIR, "tokenizer")
TEXT_MODEL_PATH = os.path.join(BASE_DIR, "text_embeddings")
IMAGE_MODEL_PATH = os.path.join(BASE_DIR, "image_embeddings")
BRAIN_PATH = os.path.join(BASE_DIR, "brain")
TEXT_EMBEDDING_MODEL = "nomic-ai/nomic-embed-text-v1.5"
IMAGE_EMBEDDING_MODEL = "clip-ViT-B-32"
 
for p in [BOOK_PATH, PATTERN_PATH, KNOWLEDGE_PATH, TEST_PATH, OHLC_PATH, JOURNAL_PATH, TOKENIZER_PATH, TEXT_MODEL_PATH, IMAGE_MODEL_PATH, BRAIN_PATH]:
    os.makedirs(p, exist_ok=True)

def load_model_local(model_name, local_path, device='cpu'):
    # ตรวจสอบว่ามีโฟลเดอร์และไฟล์โมเดลอยู่แล้วหรือไม่
    if os.path.exists(local_path) and os.listdir(local_path):
        print(f"Loading {model_name} from local directory...")
        # โหลดจากเครื่องโดยตรง ไม่ต้องเช็ค update จากเน็ต
        return SentenceTransformer(local_path, device=device, trust_remote_code=True)
    else:
        print(f"Downloading {model_name} from Hugging Face Hub...")
        # ดาวน์โหลดครั้งแรก
        model = SentenceTransformer(model_name, device=device, trust_remote_code=True)
        # บันทึกลงเครื่องเพื่อใช้ครั้งต่อไป
        model.save(local_path)
        print(f"Model saved to {local_path}")
        return model

text_embed = load_model_local(TEXT_EMBEDDING_MODEL, TEXT_MODEL_PATH, device='cuda')
image_embed = load_model_local(IMAGE_EMBEDDING_MODEL, IMAGE_MODEL_PATH, device='cuda')

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
    if os.path.exists(TOKENIZER_PATH) and os.listdir(TOKENIZER_PATH):
        tokenizer = AutoTokenizer.from_pretrained(TOKENIZER_PATH, local_files_only=True, trust_remote_code=True)
    else:
        raise OSError("Local tokenizer files not found!")
except Exception as e:
    print(f"Local load failed: {e}. Downloading from Hugging Face...")
    # หากโหลด local ไม่สำเร็จ ให้โหลดจากออนไลน์หนึ่งครั้งแล้วบันทึกไว้
    tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-7B-Instruct", trust_remote_code=True)
    tokenizer.save_pretrained(TOKENIZER_PATH)
    print(f"Tokenizer saved to {TOKENIZER_PATH}")

SYSTEM_PROMPT = """You are David, an AI assistant specialized in Al Brooks Price Action.
Your role is to learn Price Action from a user, Wei. You use three books of Al Brooks as references on trading terminology and methodology.
Wei will provide you more patterns with explanation to learn from time to time in order to teach you.
You must learn to memorize many important patterns to evaluate the meaning of any chart I will ask you in the future.

[Instruction for Price Mapping]:
When analyzing patterns in the image, use the <Memory_Reference> as a guide.
Interpret the following notations to locate price levels in the visual chart:
Datetime = YY-MM-DD HH:MM
O(Datetime): Locate the Open price of the candlestick body at this specific time.
H(Datetime): Locate the High price (the top of the wick) at this specific time.
L(Datetime): Locate the Low price (the bottom of the wick) at this specific time.
C(Datetime): Locate the Close price of the candlestick body at this specific time.
David, you must treat the Datetime as the unique index to synchronize the provided OHLC table with the pixels in the provided chart image.

RULES FOR EVERY RESPONSE:
Before giving the final answer(output), provide a brief internal monologue or step-by-step reasoning explaining how you interpreted the request and which parts of the retrieved context you are using to formulate your response.
Start every response with your 'internal monologue', then wrap your full analysis in <summary> tags and make sure to close the tag every time.
If the current chart's OHLC shows momentum or candle characteristics that differ significantly from the <Memory_Reference> (e.g., much stronger bodies or different closing levels), you must highlight these contradictions in your internal monologue to identify any failed patterns.
"""

# --- David's Brain & Reasoning Configuration ---
options_config = {
    'num_predict': 4096,          # เพดานคำพูด Output Tokens (แต่รวมกันต้องไม่เกิน num_ctx ที่ตั้งไว้ใน server)
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
            prompt_content = ""
            found_images = []
            system_note = ""
            modified_content = ""
            mode_text = ""
            response_text = ""
            last_text = ""
            category = ""
            is_pattern_name = []
            is_knowledge_name = []
            is_question_name = []
            IMAGE_PATH = ""
            
            print("\n--- David is online ---")
            print("** Asking any questions by using '?' **")
            print("** To input PATTERN, type 'p:file_name' | KNOWLEDGE, type 'k:file_name' **")
            print("** Type 'exit'/'quit'/'bye' to terminate **\n")
            user_input = input("Wei: ").strip()
            
            if user_input.strip().lower() in ['exit', 'quit', 'bye']:
                break
            
            # ตรวจสอบดูว่ารูปแบบ PATTERN ที่ต้องจดจำ โดยจะ load คำสอนจาก text file หรือไม่
            if user_input.lower().startswith("p:"):
                mode_text = "David is memorizing the pattern..."
                response_text = "[PATTERN_ACKNOWLEDGEMENT]:\n"
                last_text = "David, memorize this pattern which is derived from Wei's experiences and explain briefly if you understand it."
                IMAGE_PATH = PATTERN_PATH

                input_name = os.path.splitext(user_input.replace("p:", "").strip())[0]
                if not input_name:
                    print("!!! --- No file name --- !!!")
                    continue
                else:
                    text_filename = input_name + ".txt"
                
                full_txtfilename = os.path.join(PATTERN_PATH, text_filename)
                if os.path.exists(full_txtfilename):
                    try:
                        all_image_files = [f for f in os.listdir(PATTERN_PATH) if f.lower().endswith(('.png', '.jpg', '.jpeg')) and os.path.isfile(os.path.join(PATTERN_PATH, f))]
                        for img in all_image_files:
                            if input_name.lower() == os.path.splitext(os.path.basename(img))[0]:
                                is_pattern_name = [input_name, os.path.splitext(img)[1]]
                                found_images.append(os.path.join(PATTERN_PATH,img))
                                system_note = f"<System Note: IMAGE 1 is a pattern of {is_pattern_name[0]}{is_pattern_name[1]}>\n"
                                break
                        if not is_pattern_name:
                            print("No pattern image found!")
                            continue                            # PATTERN ต้องมีภาพเท่านั้น ถ้าไม่มีให้เริ่ม input ใหม่
                    except Exception as e:
                        print(f"Error: {e}")
                        continue
                    
                    with open(full_txtfilename, "r", encoding="utf-8") as f:
                        pattern_name = f.readline().strip()     # อ่าน [PATTERN NAME]: xxx
                        tmp_category = f.readline().strip()     # อ่าน [CATEGORY]: xxx
                        user_input = f.read().strip()           # อ่านเนื้อหาที่เหลือ
                        
                    if pattern_name.lower().startswith("[pattern name]:") and tmp_category.lower().startswith("[category]:") and user_input != "":
                        category = pattern_name + ", " + tmp_category
                        summary_content = pattern_name + "\n" + tmp_category + "\n" + user_input + "\n\n" + system_note

                        tmp_ohlc_list = [os.path.join(PATTERN_PATH, is_pattern_name[0] + "_ohlc.txt")]
                        input_ohlc_reference = fetch_OHLC(tmp_ohlc_list, start=1)
                        if input_ohlc_reference:
                            summary_content += input_ohlc_reference[0]
                    else:
                        print("!!! --- Unusable text file --- !!!\n")
                        continue
                else:
                    print("!!! --- Unable to find text file --- !!!\n")
                    continue

            # ตรวจสอบว่าเป็นเป็นการให้ KNOWLEDGE โดยจะ load คำสอนจาก text file หรือไม่
            elif user_input.lower().startswith("k:"):
                mode_text = "David is absorbing your knowledge..."
                response_text = "[KNOWLEDGE_ACKNOWLEDGEMENT]:\n"
                last_text = "David, acknowledge this knowledge which is derived from Wei's experiences and explain briefly if you understand it."
                IMAGE_PATH = KNOWLEDGE_PATH

                input_name = os.path.splitext(user_input.replace("k:", "").strip())[0]
                if not input_name:
                    print("!!! --- No file name --- !!!")
                    continue
                else:
                    text_filename = input_name + ".txt"
                        
                full_txtfilename = os.path.join(KNOWLEDGE_PATH, text_filename)
                if os.path.exists(full_txtfilename):
                    try:
                        all_image_files = [f for f in os.listdir(KNOWLEDGE_PATH) if f.lower().endswith(('.png', '.jpg', '.jpeg')) and os.path.isfile(os.path.join(KNOWLEDGE_PATH, f))]
                        for img in all_image_files:
                            if input_name.lower() == os.path.splitext(os.path.basename(img))[0]:
                                is_knowledge_name = [input_name, os.path.splitext(img)[1]]     # ถ้าจะบันทึก KNOWLEDGE แบบ image ให้ is_knowledge_name[1] เป็นนามสกุลของภาพ     
                                found_images, system_note, modified_input_name = find_images_in_content(f"[image:{input_name}]", KNOWLEDGE_PATH)
                                break
                        if not is_knowledge_name:
                            is_knowledge_name = [input_name, ".txt"]        # ถ้าจะบันทึก KNOWLEDGE แบบ text ให้ is_knowledge_name[1] เป็นนามสกุล ".txt"
                    except Exception as e:
                        print(f"Error: {e}")
                        continue

                    with open(full_txtfilename, "r", encoding="utf-8") as f:
                        knowledge_name = f.readline().strip()     # อ่าน [KNOWLEDGE NAME]: xxx
                        tmp_category = f.readline().strip()       # อ่าน [CATEGORY]: xxx
                        user_input = f.read().strip()             # อ่านเนื้อหาที่เหลือ

                        if knowledge_name.lower().startswith("[knowledge name]:") and tmp_category.lower().startswith("[category]:") and user_input != "":
                            category = knowledge_name + ", " + tmp_category
                            summary_content = knowledge_name + "\n" + tmp_category + "\n" + user_input + "\n\n" + system_note

                            tmp_ohlc_list = [os.path.join(KNOWLEDGE_PATH, is_knowledge_name[0] + "_ohlc.txt")]
                            input_ohlc_reference = fetch_OHLC(tmp_ohlc_list, start=1)
                            if input_ohlc_reference:
                                summary_content += input_ohlc_reference[0]
                        else:
                            print("!!! --- Unusable text file --- !!!\n")
                            continue
                else:
                    print("\n!!! --- Unable to find text file --- !!!\n")
                    continue
            
            # ตรวจสอบดูว่าเป็นชุดคำถามหรือบททดสอบหรือไม่ ถ้าใช่ จะไม่มีการบันทึกลงใน vectorstore
            elif "?" in user_input:
                mode_text = "David is thinking...\n"
                response_text = "[DAVID_ANALYSIS]:\n"
                IMAGE_PATH = TEST_PATH
                
                summary_content = f"[QUESTION]: {user_input}\n\n"

                # ค้นหาภาพสำหรับการทดสอบ (ถ้ามี)ใน user_input เพื่อนำมาประกอบใน content
                found_images, system_note, modified_content = find_images_in_content(user_input, TEST_PATH)
                if found_images:
                    user_input = modified_content
                    summary_content += system_note
                
                    _ohlc_list = [os.path.splitext(path)[0] + "_ohlc.txt" for path in found_images]
                    _matched_ohlc = fetch_OHLC(_ohlc_list, start=len(found_images)+1)
                    for str_ohlc in _matched_ohlc:
                        summary_content += str_ohlc

                    img_name = os.path.splitext(os.path.basename(found_images[0]))[0]
                    img_ext = os.path.splitext(found_images[0])[1]
                    is_question_name = [img_name, img_ext]
                else:
                    is_question_name = [datetime.now().strftime("%y%m%d_%H%M"), ".txt"]
            else:
                continue

            # ค้นหา THEORY และ KNOWLEDGE ที่เกี่ยวข้องกับ user_input
            prompt_content, matched_knowledge = get_matched_knowledge(text_vectorstore, user_input, k=3)
            if prompt_content:
                print(f"Found {matched_knowledge} knowledge contents from memory.\n")      # print(f"\n[EXPERIENCE_FOUND]:\n{prompt_content}\n")
                prompt_content = prompt_content.strip()
                prompt_content += "\n===\n\n"
            else:
                print("Knowledge contents could not be found.")
            
            if image_vectorstore != None:
                if is_pattern_name:
                    # ถ้าเป็นการเรียนรู้ PATTERN ใหม่ ให้ดึงฐานข้อมูล PATTERN เดิม 3 รูปที่ใกล้เคียงที่สุด
                    tmp_content, matched_patterns = find_matched_pattern(image_vectorstore, found_images[0], k=2, distance_threshold=0.2)
                    if matched_patterns > 0:
                        print(f"Found {matched_patterns} matched pattern(s).")
                        prompt_content += f"Matched patterns for [IMAGE 1]: {is_pattern_name[0]+is_pattern_name[1]}\n\n---\n\n"
                        prompt_content += tmp_content + "\n\n---\n\n"
                elif is_knowledge_name and is_knowledge_name[1] != ".txt":
                    # ถ้าเป็นการเรียนรู้ KNOWLEDGE ที่เป็นภาพซับซ้อน ให้ดึงฐานข้อมูล PATTERN 10 รูปเพื่อให้ครอบคลุมหลากหลาย PATTERN
                    tmp_content, matched_patterns = find_matched_pattern(image_vectorstore, found_images[0], k=3, distance_threshold=0.4)
                    if matched_patterns > 0:
                        print(f"Found {matched_patterns} matched pattern(s).")
                        prompt_content += f"Matched patterns for [IMAGE 1]: {is_knowledge_name[0]+is_knowledge_name[1]}\n\n---\n\n"
                        prompt_content += tmp_content + "\n\n---\n\n"
                elif is_question_name and is_question_name[1] != ".txt":
                    # ถ้าเป็นคำถาม QUESTION เพื่อการทดสอบ ที่เป็นภาพซับซ้อน ให้ดึงฐานข้อมูล PATTERN 10 รูปเพื่อให้ครอบคลุมหลากหลาย PATTERN
                    tmp_content, matched_patterns = find_matched_pattern(image_vectorstore, found_images[0], k=3, distance_threshold=0.4)
                    if matched_patterns > 0:
                        print(f"Found {matched_patterns} matched pattern(s).")
                        prompt_content += f"Matched patterns for [IMAGE 1]: {is_question_name}\n\n---\n\n"
                        prompt_content += tmp_content + "\n\n---\n\n"
            
            # ค้นหาภาพสำหรับ Reference (ถ้ามี)ใน user_input เพื่อนำมาประกอบใน content
            more_images, system_note, modified_content = find_images_in_content(user_input, IMAGE_PATH)
            if more_images:
                found_images = list(dict.fromkeys(found_images + more_images))   # เพิ่ม more_images เข้าไปใน found_images โดยภาพต้องไม่ซ้ำกัน
                user_input = modified_content
                summary_content += system_note
                
                ohlc_list = [os.path.splitext(path)[0] + "_ohlc.txt" for path in more_images]
                matched_ohlc = fetch_OHLC(ohlc_list, start=len(found_images)+1)
                for str_ohlc in matched_ohlc:
                    summary_content += str_ohlc

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
                print("\nError: Empty Response\n")
                continue

            print(f"{len(found_images)} image(s) realized by David.")
            print(f"Image: {[os.path.splitext(os.path.basename(path))[0] for path in found_images]}\n")

            total_images_list.extend(found_images)
            full_content = summary_content + initial_analysis

            # Loop โต้ตอบ จนกว่า Wei จะบอกว่า "ถูกต้อง"
            while True:
                try:
                    r_found_images = []
                    r_system_note = ""
                    r_modified_content = ""
            
                    feedback = input("Do I understand correctly (y/n/quit)?: ").strip()
            
                    if feedback == "y":
                        final_summary = ""
                        all_summaries = re.findall(r'<summary>(.*?)</summary>', full_content, re.DOTALL)
                        if all_summaries:
                            final_summary = all_summaries[-1].strip()

                        while True:
                            try:
                                add_final = input("Adding final conclusion to brain (y/n)?: ").strip()
                                if add_final == "y":
                                    summary_content += "[CORRECT UNDERSTANDING]:\n" + final_summary
                                    break
                                elif add_final == "n":
                                    break
                                else:
                                    continue
                            except Exception as e:
                                print(f"Error: {e}")

                        if is_pattern_name:
                            image_vectorstore = save_image_to_brain(image_vectorstore, category, summary_content, full_content, total_images_list[0])
                            print("Pattern was memorized successfully.")
                        elif is_knowledge_name:
                            save_path = os.path.join(KNOWLEDGE_PATH, is_knowledge_name[0]+is_knowledge_name[1])
                            if is_knowledge_name[1] == ".txt":
                                text_vectorstore = save_text_to_brain(text_vectorstore, category, summary_content, full_content, save_path)
                            else:
                                image_vectorstore = save_image_to_brain(image_vectorstore, category, summary_content, full_content, save_path)
                            print("Knowledge was memorized successfully.")
                        elif is_question_name:
                            save_path = os.path.join(TEST_PATH, is_question_name[0]+is_question_name[1])
                            modified_summary_content = summary_content
                            category = "KNOWLEDGE FROM QUESTION"
                            
                            while True:
                                try:
                                    input_name = input("Enter the name of this test: ").strip()
                                    if input_name == "":
                                        continue
                                    input_category = input("Enter the category of this test: ").strip()
                                    if input_category == "":
                                        continue
                    
                                    input_condition = input(f"Confirm the name: '{input_name}', and category: '{input_category}'? (y/n): ").lower().strip()
                                    if input_condition == "y":
                                        test_name_content = f"[KNOWLEDGE NAME]: {input_name}"
                                        test_category_content = f"[CATEGORY]: {input_category}"
                                        category = test_name_content + ", " + test_category_content
                                        modified_summary_content = test_name_content + "\n" + test_category_content + "\n" + summary_content
                                        break
                                    elif input_condition == "n":
                                        break
                                    else:
                                        continue
                                except Exception as e:
                                    print(f"Error: {e}")

                            if is_question_name[1] == ".txt":
                                text_vectorstore = save_text_to_brain(text_vectorstore, category, modified_summary_content, full_content, save_path)
                            else:
                                image_vectorstore = save_image_to_brain(image_vectorstore, category, modified_summary_content, full_content, save_path)
                            print("Knowledge from question was memorized successfully.")

                        
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
                        r_found_images, r_system_note, r_modified_content = find_images_in_content(correction_input, IMAGE_PATH)
                        if r_found_images:
                            # เพิ่มไฟล์ภาพเพิ่มเติม(ถ้ามี)ใน correction input โดยสร้างรวมกับภาพเดิมที่เคยคุยกันใน session แรกด้วย
                            temp_images_list += total_images_list + r_found_images
                            correction_input = r_modified_content
                            refined_prompt_content += r_system_note

                            r_ohlc_list = [os.path.splitext(path)[0] + "_ohlc.txt" for path in r_found_images]
                            r_matched_ohlc = fetch_OHLC(r_found_images, start=len(total_images_list)+1)
                            for str_ohlc in r_matched_ohlc:
                                refined_prompt_content += str_ohlc
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
                            print("\nError: Empty Response\n")
                            continue
                        
                        print(f"Total {len(total_images_list)} image(s) realized by David.")
                        print(f"Image: {[os.path.splitext(os.path.basename(path))[0] for path in total_images_list]}\n")
                    
                        # เมื่อการวิเคราะห์ของ David สำเร็จ ให้บันทึกภาพ(ถ้ามี)จาก Input เพิ่มเข้าไปใน total_images_list ซึ่งเป็น Global Variable โดยภาพจะไม่ซ้ำกัน
                        total_images_list = list(dict.fromkeys(total_images_list + r_found_images))
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

 
def save_image_to_brain(vectorstore, category, summary_content, full_content, file_path):
    image_name = os.path.splitext(os.path.basename(file_path))[0]    # ไฟล์ไม่มี ext
    
    # สร้าง Vector จากภาพด้วย CLIP (CPU)
    img_vector = image_embed.encode(Image.open(file_path)).tolist()
    
    img_metadata = {"category": category, "source": image_name, "path": file_path}
    doc = Document(page_content=summary_content, metadata=img_metadata)
    
    if os.path.exists(os.path.join(BRAIN_PATH, "image_index.faiss")) and vectorstore is not None:
        vectorstore.add_embeddings(zip([doc.page_content], [img_vector]), metadatas=[doc.metadata])
    else:
        vectorstore = FAISS.from_embeddings(zip([doc.page_content], [img_vector]), Image_Embeddings, metadatas=[doc.metadata])

    vectorstore.save_local(BRAIN_PATH, index_name="image_index")

    # บันทึก Summary Content ลงในไฟล์ต้นทาง (ทับไฟล์เดิมใน pattern หรือ knowledge folder)
    txt_source_path = os.path.splitext(file_path)[0] + ".txt"
    with open(txt_source_path, "w", encoding="utf-8") as f:
        f.write(summary_content)
    
    # บันทึก full content ลงใน journals
    txt_journal = image_name + ".txt"
    journal_path = os.path.join(JOURNAL_PATH, txt_journal)
    with open(journal_path, "w", encoding="utf-8") as f:
        f.write(full_content)
        
    return vectorstore
    

def save_text_to_brain(vectorstore, category, summary_content, full_content, file_path):
    txt_name = os.path.splitext(os.path.basename(file_path))[0]
    
    # 1. บันทึกข้อมูลเป็น Text File ลงโฟลเดอร์ journal
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(summary_content)
    
    # 2. หั่นเนื้อหา (Split) ก่อนบันทึกลง FAISS เพื่อป้องกัน Error 400
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
    
    txt_metadata = {"category": category, "source": txt_name}
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


def fetch_OHLC(ohlc_path, start=1):
    ohlc_list = []

    while True:
        try:
            ok_to_config = input("Ok to fetch OHLC for charts? (y/n): ").strip().lower()
            if ok_to_config == "y":
                break
            elif ok_to_config == "n":
                return ohlc_list
            else:
                continue
        except Exception as e:
            print(f"Error: {e}")
    
    # วน Loop ตามจำนวนภาพที่พบ โดยใช้ enumerate เพื่อระบุลำดับภาพ
    for i, img in enumerate(ohlc_path, start=start):
        base_name = os.path.basename(img)
        ohlc_config_file = base_name.replace("_ohlc.txt", "")
        
        with open(img, "r", encoding="utf-8") as f:
            symbol = f.readline().strip()       # อ่านบรรทัดแรก "Symbol: XAUUSD"
            if symbol.lower().startswith("symbol:"):
                symbol = re.sub(r'^symbol:\s*', '', symbol, flags=re.IGNORECASE)     # ตัดคำว่า Symbol: ออก เหลือแค่ XAUUSD
            else:
                print(f"!!! --- Unusable OHLC config file: {ohlc_config_file} --- !!!")
                continue

            exchange = f.readline().strip()     # อ่านบรรทัดที่สอง "Exchange: GO Markets"
            if exchange.lower().startswith("exchange:"):
                exchange = re.sub(r'^exchange:\s*', '', exchange, flags=re.IGNORECASE)     # ตัดคำว่า Exchange: ออก เหลือแค่ GO Markets
            else:
                print(f"!!! --- Unusable OHLC config file: {ohlc_config_file} --- !!!")
                continue

            timeframe = f.readline().strip()    # อ่านบรรทัดที่สาม "Timeframe: (1m/5m/15m/1h/1d)"
            if timeframe.lower().startswith("timeframe:"):
                timeframe = re.sub(r'^timeframe:\s*', '', timeframe, flags=re.IGNORECASE)     # ตัดคำว่า Timeframe: ออก เหลือแค่ 1m/5m/15m/1h/1d
            else:
                print(f"!!! --- Unusable OHLC config file: {ohlc_config_file} --- !!!")
                continue

            start_datetime = f.readline().strip()    # อ่านบรรทัดที่สี่ "Start Datetime: (YYYY-MM-DD HH:MM)"
            if start_datetime.lower().startswith("start datetime:"):
                start_datetime = re.sub(r'^start datetime:\s*', '', start_datetime, flags=re.IGNORECASE)     # ตัดคำว่า Start_datetime: ออก เหลือแค่ (YYYY-MM-DD HH:MM)
            else:
                print(f"!!! --- Unusable OHLC config file: {ohlc_config_file} --- !!!")
                continue

            end_datetime = f.readline().strip()    # อ่านบรรทัดที่ห้า "End Datetime: (YYYY-MM-DD HH:MM)"
            if end_datetime.lower().startswith("end datetime:"):
                end_datetime = re.sub(r'^end datetime:\s*', '', end_datetime, flags=re.IGNORECASE)     # ตัดคำว่า End_datetime: ออก เหลือแค่ (YYYY-MM-DD HH:MM)
            else:
                print(f"!!! --- Unusable OHLC config file: {ohlc_config_file} --- !!!")
                continue

        ohlc_file = f"{symbol}-{exchange}-{timeframe}.csv"
        ohlc_path = os.path.join(OHLC_PATH, ohlc_file)
        if not os.path.exists(ohlc_path):
            print(f"Error: Unable to find OHLC data file for IMAGE {i}, Skipping...")
            continue

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
        
        df.index = df.index.tz_localize(None)     # ถอด Timezone ออกให้เหลือแค่ "ตัวเลขเวลา" (Naive) เพื่อให้ง่ายต่อการอ้างอิง
        df = df.sort_index(ascending=True)        # เรียงลำดับเพื่อให้การ Slice ช่วงเวลาถูกต้อง

        # แยกคอลัมน์ 'Message' ออกเป็น 4 คอลัมน์ (Open, High, Low, Close) และแปลงให้เป็นตัวเลข (float) ทันที
        df[['open', 'high', 'low', 'close']] = df['Message'].str.split(',', expand=True).astype(float)
        df.drop(columns=['Message'], inplace=True)     # ลบคอลัมน์ Message เดิมทิ้ง

        # ดึงข้อมูลในช่วง start_datetime ถึง end_datetime, Pandas จะดึงข้อมูลทุกแท่งที่อยู่ในช่วงเวลานี้มาให้ทั้งหมด
        df_filtered = df.loc[start_datetime : end_datetime].copy()
        if df_filtered.empty:         # ตรวจสอบว่ามีข้อมูลในช่วงที่ระบุหรือไม่
            print(f"!!! No data found between {start_datetime} and {end_datetime} !!!")
        else:
            print(f"Successfully fetched {len(df_filtered)} bars")
        
        # --- สร้าง List และแปลงข้อมูลทีละแถวให้เป็น String แล้วใส่ลงไปใน List ในรูปแบบ Datetime, O, H, L, C ---
        formatted_rows = []
        for dt, row in df_filtered.iterrows():
            dt_str = dt.strftime('%y-%m-%d %H:%M')       # แปลง Datetime ให้เป็นรูปแบบ (yy-mm-dd HH:MM)
            # สร้าง Format: Datetime, O, H, L, C
            row_str = f"{dt_str}, {row['open']:.2f}, {row['high']:.2f}, {row['low']:.2f}, {row['close']:.2f}"
            formatted_rows.append(row_str)

        ohlc_reference = "\n".join(formatted_rows)
                                
        # สะสมข้อมูลเข้าใน ohlc_content โดยระบุว่าอ้างอิงกับ Image ลำดับที่เท่าไหร่
        ohlc_content = f"\n[OHLC_REFERENCE for IMAGE {i}] (Format: Datetime, O, H, L, C)\n"
        ohlc_content += ohlc_reference + "\n"
        ohlc_content += "=" * 10 + "\n\n"
        
        ohlc_list.append(ohlc_content)
        
    return ohlc_list


# ฟังก์ชั่นค้นหา PATTERN ที่คล้ายกับ image_path
def find_matched_pattern(vectorstore, image_path, k=10, distance_threshold=0.5):
    if not vectorstore:
        return "", 0

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
        return "", 0             # ไม่พบรูปแบบที่มั่นใจพอ
    
    # รวมข้อมูลแล้วครอบด้วย <Memory_Reference>
    joined_patterns = "\n\n---\n\n".join(matched_patterns)
    str_matched_patterns = f"<Memory_Reference>\n{joined_patterns}\n</Memory_Reference>\n"
    no_of_matched_patterns = len(matched_patterns)

    return str_matched_patterns, no_of_matched_patterns


# ฟังก์ชั่นดึง THEORY และ KNOWLEDGE จาก MEMORY
def get_matched_knowledge(vectorstore, query, k=5):
    if not vectorstore:
        return "", 0
    
    # ค้นหาข้อมูลที่ใกล้เคียงกับคำถามที่สุด จำนวน k ลำดับ
    results = vectorstore.similarity_search(query, k=k)
    matched_knowledge = []
    
    for doc in results:
        # ดึง Metadata category ที่เราตั้งไว้ (เช่น 'THEORY' หรือ 'KNOWLEDGE')
        category = doc.metadata.get('category', 'KNOWLEDGE').upper()
        source = doc.metadata.get('source', 'unknown')
        
        # ประกอบร่างข้อความเพื่อให้ David แยกแยะที่มาได้
        matched_knowledge.append(f"[{category}] (Source: {source}):\n{doc.page_content}")
    
    if not matched_knowledge:
        return "", 0

    # รวมข้อมูลแล้วครอบด้วย tag <Memory_Reference>
    joined_knowledge = "\n---\n".join(matched_knowledge)
    str_matched_knowledge = f"<Memory_Reference>\n{joined_knowledge}\n</Memory_Reference>\n"
    no_of_matched_knowledge = len(matched_knowledge)

    return str_matched_knowledge, no_of_matched_knowledge


# ฟังก์ชั่นค้นหาชื่อภาพในข้อความ text_to_be_searched และคืนค่า List ของ Path รูปภาพที่พบ
def find_images_in_content(text_to_be_searched, image_path):
    found_images = []       # จุดรวมชื่อภาพที่หาเจอพร้อมตำแหน่ง Directory ที่ค้นหาจาก text_to_be_searched
    system_note = "\n<System Note: "    # ข้อความสำหรับสร้าง System Note เพื่อบอก David เกี่ยวกับลำดับภาพ
    content_text = text_to_be_searched
    image_count = len(total_images_list)

    #  ค้นหาชื่อภาพ Symbol_YYMMDD_Day ในวงเล็บเหลี่ยม [] ที่ขึ้นต้นด้วย "image:
    #  \s* คือ User จะพิมพ์โดยมีช่องว่างหรือไม่มีช่องว่างก็ได้
    #  (.*?) ให้ return ค่าค้นหา โดยเริ่มนับคำหลังจาก "[image:" และให้จบคำนั้นหลังจากเจอ "]"
    image_list = re.findall(r"\[image:\s*(.*?)\]", content_text)
    if not image_list:
        return [], "", content_text

    try:
        # รายชื่อไฟล์ภาพทั้งหมด
        all_image_files = {
            os.path.splitext(f)[0].lower(): f 
            for f in os.listdir(image_path) 
            if f.lower().endswith(('.png', '.jpg', '.jpeg')) and os.path.isfile(os.path.join(image_path, f))
        }
    except Exception as e:
        print(f"Error: {e}")
        return [], "", content_text

    final_replacements = []

    for img in image_list:
        img_name = img.strip().lower()

        if img_name in all_image_files:
            found_file = all_image_files[img_name]
            full_path = os.path.join(image_path, found_file)
            found_images.append(full_path)           
            image_count += 1
            
            # กระบวนการเปลี่ยนชื่อไฟล์จากเช่น Emini_260320_Friday ให้เป็น Emini_Friday 20 March 2026 เพื่อให้ David อ่านแล้วเข้าใจ
            tmp_name = os.path.splitext(found_file)[0]
            
            # ลบวันด้านหลังของชื่อออก ให้เหลือเฉพาะแต่ชื่อด้านหน้าเช่น Emini_260320
            for day in ["_Monday", "_Tuesday", "_Wednesday", "_Thursday", "_Friday", "_Saturday", "_Sunday"]:
                if day in tmp_name:
                    tmp_name = tmp_name.replace(day, "")
            
            # ดึงชื่อส่วนหน้า (Base Name) ออกมา โดยใช้ Regex ค้นหาตำแหน่งแรกที่เจอตัวเลข แล้วตัดเอาข้อความก่อนหน้านั้น
            # เช่น "Connected_Emini_260328-260329" ไปเป็น "Connected_Emini"
            base_name_match = re.search(r'^(.*?)(?=_?\d{6})', tmp_name)
            base_name = base_name_match.group(1).rstrip('_') if base_name_match else tmp_name
            
            # ดึงวันที่ทั้งหมด
            date_matches = re.findall(r'\d{6}', tmp_name)

            formatted_dates = []
            for date_str in date_matches:
                try:
                    # แปลง YYMMDD เป็น Object วันที่
                    date_obj = datetime.strptime(date_str, "%y%m%d")
                    # ฟอร์แมตวันที่เป็น %A (ชื่อวันเต็ม) %d (ตัวเลขวันที่) %B (ชื่อเดือนแบบเต็ม) %Y (ชื่อปีแบบเต็ม) เช่น Friday 20 March 2026
                    formatted_dates.append(date_obj.strftime("%A %d %B %Y"))
                except ValueError:
                    continue            # ถ้าไม่ใช่รูปแบบวันที่ที่ถูกต้อง ให้ใช้ชื่อเดิม
            
            # สร้างข้อความชุดใหม่ที่จะเอาไปวางแทนที่เดิม
            if len(formatted_dates) >= 2:
                # ถ้าเจอ 2 วันที่ เช่น จาก Connected_Emini_260319-260320 จะกลายเป็น Connected_Emini from Thursday 19 March 2026 to Friday 20 March 2026
                transformed_name = f"{base_name} from {formatted_dates[0]} to {formatted_dates[1]}"
            elif len(formatted_dates) == 1:
                # กรณีเจอวันที่เดียว เช่น Emini_260320 จะกลายเป็น Emini on Friday 20 March 2026
                transformed_name = f"{base_name} on {formatted_dates[0]}"
            else:
                transformed_name = tmp_name
            
            final_replacements.append(transformed_name)

            system_note += f"IMAGE {image_count} is a chart of {found_file} which is {transformed_name}, "
        else:
            final_replacements.append(f"[image:{img}]")
    
    # จบท้ายด้วยการแทนที่ใน content_text ตามลำดับที่หาเจอ โดยใช้ iterator เพื่อให้ re.sub ดึงค่าจาก final_replacements มาวางทีละตัวตามลำดับ
    replacement_iter = iter(final_replacements)
    content_text = re.sub(r"\[image:\s*.*?\]", lambda m: next(replacement_iter), content_text)

    system_note += ">\n"
    print(f"\n{len(found_images)} image(s) found from input.")
    print(f"IMAGE: {[os.path.splitext(os.path.basename(path))[0] for path in found_images]}\n")
    # ส่งค่ากลับไป 2 อย่าง: 
    # 1. รายชื่อไฟล์ภาพพร้อม Path 
    # 2. ข้อความ System Note เพื่อบอกลำดับภาพให้ David   
    return found_images, system_note, content_text


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


def david_chat(prompt, mode_text, response_text):
    global total_input_tokens
    global total_output_tokens
    
    analysis = ""
    last_chunk = None
    
    # David เริ่มกระบวนการคิด... (ใช้เวลา)
    print(mode_text)
    
    # เตรียมข้อมูลส่งให้ FastAPI โดยส่งไปทั้ง List (รวม System Prompt) แบบ messages
    payload = {"messages": prompt, "temperature": 0.1}

    try:
        # เริ่มการเชื่อมต่อแบบ Stream
        with requests.post(SERVER_URL, json=payload, stream=True) as response:
            if response.status_code != 200:
                print(f"Server Error: {response.status_code}")
                return ""

            is_first_chunk = True
            
            # อ่านข้อมูลที่ส่งมาจาก FastAPI ทีละบรรทัด (Chunks)
            for line in response.iter_lines():
                if line:
                    # แปลงจาก JSON String กลับเป็น Dictionary
                    chunk = json.loads(line.decode("utf-8"))
                    last_chunk = chunk
                    
                    # ตรวจสอบว่ามีเนื้อหา (Content) ส่งมาหรือไม่
                    if 'choices' in chunk and len(chunk['choices']) > 0:
                        delta = chunk['choices'][0].get('delta', {})
                        content = delta.get('content', '')
                        
                        if content:
                            if is_first_chunk:
                                full_content = response_text + content
                                print(full_content, end="", flush=True)
                                analysis += full_content
                                is_first_chunk = False
                            else:
                                print(content, end="", flush=True)
                                analysis += content

            print("\n" + "-"*30)

            # คำนวณปริมาณ Tokens (llama-cpp-python จะส่งมาในก้อนสุดท้ายหรือแยกส่วน)
            # ในกรณีที่เราใช้ StreamingResponse ทั่วไป เราอาจต้องให้ Server คำนวณและส่งมา
            if last_chunk and 'usage' in last_chunk and last_chunk['usage'] is not None:
                input_tokens = last_chunk['usage'].get('prompt_tokens', 0)
                output_tokens = last_chunk['usage'].get('completion_tokens', 0)
            else:
                # ถ้า Server ไม่ได้ส่งมา เราสามารถใช้การนับคร่าวๆ หรือให้ Server ส่ง JSON พิเศษมาปิดท้าย
                input_tokens = 0 
                output_tokens = 0

            total_input_tokens += input_tokens
            total_output_tokens += output_tokens
            total_session_tokens = input_tokens + output_tokens
            
            print(f"\n[SESSION TOKEN USAGE] Input: {input_tokens} | Output: {output_tokens} | Total: {total_session_tokens}\n")

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
                
                    updated_input = old_input + input_tokens
                    updated_output = old_output + output_tokens
                
                    new_lines.append(f"{month_label}: Total Input Tokens = {updated_input}, Total Output Tokens = {updated_output}, Total Tokens = {updated_input + updated_output}\n")
                    found = True
                else:
                    new_lines.append(line)     # เก็บเดือนอื่นไว้เหมือนเดิม
    
            # 3. ถ้ายังไม่มีเดือนนี้เลย ให้สร้างบรรทัดแรกของเดือน
            if not found:
                new_lines.append(f"{month_label}: Total Input Tokens = {input_tokens}, Total Output Tokens = {output_tokens}, Total Tokens = {input_tokens + output_tokens}\n")
    
            # 4. เขียนข้อมูลทั้งหมดกลับลงไฟล์
            with open(total_tokens_file, "w", encoding="utf-8") as f:
                f.writelines(new_lines)

    except requests.exceptions.ConnectionError:
        print("\nError: David Server is not running!\n")

    return analysis


def create_brain():    
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
    text_vectorstore = None
    image_vectorstore = None
    category = ""
    
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
                pattern_name = f.readline().strip()     # อ่าน [PATTERN NAME]: xxx
                tmp_category = f.readline().strip()     # อ่าน [CATEGORY]: xxx
                content_text = f.read().strip()         # อ่านเนื้อหาที่เหลือ
            
            if pattern_name.lower().startswith("[pattern name]:") and tmp_category.lower().startswith("[category]:") and content_text != "":
                category = pattern_name + ", " + tmp_category
                full_content = category + "\n" + content_text + "\n"
            else:
                print(f"!!! --- Unusable text file: {txt_name}, Skipping... --- !!!\n")
                continue
            
            # แปลงภาพเป็น Vector โดยใช้ CLIP
            pattern_vector = image_embed.encode(Image.open(pattern_path)).tolist()
               
            # เตรียม Metadata สำหรับ PATTERN
            pattern_metadata = {"category": category, "source": unit, "path": pattern_path}
            # สร้าง Document หลอกเพื่อเก็บ Vector และ Metadata ของภาพลงใน FAISS
            temp_doc = Document(page_content=full_content, metadata=pattern_metadata)

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
        if not not_pairs:
            print("No text knowledge files found.")
        else:
            print(f"Absorbing text knowledge... [{len(not_pairs)} unit(s)]")
            # เริ่มดึงฐานข้อมูลส่วน KNOWLEDGE ที่ไม่มีภาพประกอบก่อน
            for unit in not_pairs:
                txt_name = unit + ".txt"
                txt_path = os.path.join(KNOWLEDGE_PATH, txt_name)
                category = ""
            
                with open(txt_path, "r", encoding="utf-8") as f:
                    knowledge_name = f.readline().strip()   # อ่าน [KNOWLEDGE NAME]: xxx
                    tmp_category = f.readline().strip()     # อ่าน [CATEGORY]: xxx
                    content_text = f.read().strip()         # อ่านเนื้อหาที่เหลือ
            
                if knowledge_name.lower().startswith("[knowledge name]:") and tmp_category.lower().startswith("[category]:") and content_text != "":
                    category = knowledge_name + ", " + tmp_category
                    full_content = category + "\n" + content_text + "\n"
                else:
                    print(f"!!! --- Unusable text file: {txt_name}, Skipping... --- !!!\n")
                    continue
                
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
            print(f"Absorbing image knowledge... [{len(pairs)} unit(s)]")
            # และดึงฐานข้อมูลส่วน KNOWLEDGE ที่มีภาพประกอบด้วย
            for unit in pairs:
                txt_name = unit + ".txt"
                txt_path = os.path.join(KNOWLEDGE_PATH, txt_name)
                chart_name =  chart_files[unit]
                chart_path = os.path.join(KNOWLEDGE_PATH, chart_name)
                category = ""
            
                with open(txt_path, "r", encoding="utf-8") as f:
                    knowledge_name = f.readline().strip()   # อ่าน [KNOWLEDGE NAME]: xxx
                    tmp_category = f.readline().strip()     # อ่าน [CATEGORY]: xxx
                    content_text = f.read().strip()         # อ่านเนื้อหาที่เหลือ
            
                if knowledge_name.lower().startswith("[knowledge name]:") and tmp_category.lower().startswith("[category]:") and content_text != "":
                    category = knowledge_name + ", " + tmp_category
                    full_content = category + "\n" + content_text + "\n"
                else:
                    print(f"!!! --- Unusable text file: {txt_name}, Skipping... --- !!!\n")
                    continue

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


def ask_david(prompt_text):
    # 2. เตรียมข้อมูลในรูปแบบ JSON ตามที่ AnalysisRequest ใน server ต้องการ
    payload = {
        "prompt": prompt_text,
        "temperature": 0.1
    }

    try:
        # 3. ยิง Request ไปที่ Server
        response = requests.post(SERVER_URL, json=payload)
        
        # 4. รับคำตอบกลับมา
        if response.status_code == 200:
            result = response.json()
            return result["analysis"]
        else:
            return f"Error: {response.status_code} - {response.text}"
            
    except requests.exceptions.ConnectionError:
        return "David Server is not running! (กรุณาไปรัน david_server.py ในอีกหน้าจอก่อน)"


if __name__ == "__main__":
    main()