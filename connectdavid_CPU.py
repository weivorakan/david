import os
import torch
import pandas as pd
import sys
import re
import json
import requests
from pathlib import Path
from typing import Optional
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
BASE_DIR = r"/home/weivo/david"
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

# สร้าง Class สำหรับอ่าน [IMAGE: ...] ใน text file, render chart images และจัดรูปแบบ OHLC References
class OHLCImageRenderer:
    """Parse inline IMAGE tags, render chart images, and format OHLC references."""

    IMAGE_TAG_PATTERN = re.compile(r"\[image:\s*(.*?)\]", re.IGNORECASE | re.DOTALL)

    def __init__(
        self,
        ohlc_dir,
        source_tz_if_naive="UTC",
        target_tz="Asia/Bangkok",
        message_col="Message",
    ):
        self.ohlc_dir = ohlc_dir
        self.source_tz_if_naive = source_tz_if_naive
        self.target_tz = target_tz
        self.message_col = message_col

    def extract_image_specs(self, text):
        return self.IMAGE_TAG_PATTERN.findall(text)

    def parse_image_spec(self, image_spec):
        config = {}
        key_map = {
            "symbol": "symbol",
            "exchange": "exchange",
            "timeframe": "timeframe",
            "startdatetime": "start_datetime",
            "enddatetime": "end_datetime",
            "windowsize": "window_size",
        }

        for part in image_spec.split(","):
            if ":" not in part:
                continue
            raw_key, raw_value = part.split(":", 1)
            normalized_key = re.sub(r"[\s_-]+", "", raw_key.strip().lower())
            key = key_map.get(normalized_key)
            if key:
                config[key] = raw_value.strip()

        required = ["symbol", "exchange", "timeframe", "start_datetime"]
        missing = [key for key in required if not config.get(key)]
        if missing:
            raise ValueError(f"IMAGE tag missing required field(s): {missing}")

        if config.get("window_size"):
            try:
                config["window_size"] = int(config["window_size"])
            except ValueError as e:
                raise ValueError("Window-size must be an integer") from e
            if config["window_size"] <= 0:
                raise ValueError("Window-size must be > 0")
        elif not config.get("end_datetime"):
            raise ValueError("IMAGE tag must include either End datetime or Window-size")

        return config

    def get_data_path(self, config):
        ohlc_file = f"{config['symbol']}-{config['exchange']}-{config['timeframe']}.csv"
        return os.path.join(self.ohlc_dir, ohlc_file)

    def load_from_config(self, config):
        data_path = self.get_data_path(config)
        if not os.path.exists(data_path):
            raise FileNotFoundError(data_path)

        return load_ohlc_file(
            input_path=data_path,
            message_col=self.message_col,
            source_tz_if_naive=self.source_tz_if_naive,
            target_tz=self.target_tz,
        )

    def slice_from_config(self, config):
        df = self.load_from_config(config)
        return slice_ohlc(
            df,
            start=config["start_datetime"],
            end=config.get("end_datetime"),
            window_size=config.get("window_size", 0),
        )

    def render_from_image_config(
        self,
        config,
        output_path,
        title=None,
        show_grid=False,
        hide_axes=False,
        dpi=160,
    ):
        chart_title = title or f"{config['symbol']} {config['timeframe']}"
        return render_ohlc_image(
            input_path=self.get_data_path(config),
            output_path=output_path,
            start=config["start_datetime"],
            end=config.get("end_datetime"),
            window_size=config.get("window_size", 0),
            title=chart_title,
            message_col=self.message_col,
            source_tz_if_naive=self.source_tz_if_naive,
            target_tz=self.target_tz,
            show_grid=show_grid,
            hide_axes=hide_axes,
            dpi=dpi,
        )

    def render_from_image_spec(
        self,
        image_spec,
        directory,
        name,
        image_ext=".png",
        show_grid=False,
        hide_axes=False,
        dpi=160,
    ):
        base_name = os.path.splitext(os.path.basename(name.strip()))[0]
        output_path = os.path.join(directory, base_name + image_ext)

        config = self.parse_image_spec(image_spec)
        df = self.render_from_image_config(
            config=config,
            output_path=output_path,
            title=base_name,
            show_grid=show_grid,
            hide_axes=hide_axes,
            dpi=dpi,
        )
        return output_path, config, df

    def filename_from_config(self, config):
        start_label = re.sub(r"\D+", "", config["start_datetime"])[:12]
        suffix = f"{config['window_size']}bars" if config.get("window_size") else re.sub(r"\D+", "", config["end_datetime"])[:12]
        raw_name = f"{config['symbol']}_{config['timeframe']}_{start_label}_{suffix}"
        return re.sub(r"[^A-Za-z0-9_-]+", "_", raw_name).strip("_")

    def describe_config(self, config):
        if config.get("window_size"):
            return (
                f"{config['symbol']} {config['exchange']} {config['timeframe']} "
                f"from {config['start_datetime']} for {config['window_size']} bars"
            )
        return (
            f"{config['symbol']} {config['exchange']} {config['timeframe']} "
            f"from {config['start_datetime']} to {config['end_datetime']}"
        )
    
    def format_reference(self, df, image_number):
        formatted_rows = []
        for dt, row in df.iterrows():
            dt_str = dt.strftime("%y-%m-%d %H:%M")
            row_str = f"{dt_str}, {row['open']:.2f}, {row['high']:.2f}, {row['low']:.2f}, {row['close']:.2f}"
            formatted_rows.append(row_str)

        ohlc_content = f"\n[OHLC_REFERENCE for IMAGE {image_number}] (Format: Datetime, O, H, L, C)\n"
        ohlc_content += "\n".join(formatted_rows) + "\n"
        ohlc_content += "=" * 10 + "\n\n"
        return ohlc_content

# สร้าง Instance แยกสำหรับ Text และ Image เพื่อป้องกันการสลับรุ่น Model
Text_Embeddings = HybridEmbeddings(text_embed)
Image_Embeddings = HybridEmbeddings(image_embed)
ohlc_image_renderer = OHLCImageRenderer(OHLC_PATH)

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

CRITICAL RULE:
Before any chart analysis, verify that the chart and OHLC are synchronized. If inconsistent, stop and report the mismatch.
If consistent, use the chart for visual structure and OHLC for exact bar validation.

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
If the current chart's OHLC shows momentum or candle characteristics that differ significantly from the <Memory_Reference>, you must highlight these contradictions in your internal monologue to identify the differences from the patterns found from <Memory_Reference>.
"""

# --- David's Brain & Reasoning Configuration ---
options_config = {
    'num_predict': -1,            # เพดานคำพูด Output Tokens (แต่รวมกันต้องไม่เกิน num_ctx ที่ตั้งไว้ใน server)
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
                    with open(full_txtfilename, "r", encoding="utf-8") as f:
                        pattern_name = f.readline().strip()     # อ่าน [PATTERN NAME]: xxx
                        tmp_category = f.readline().strip()     # อ่าน [CATEGORY]: xxx
                        user_input = f.read().strip()           # อ่านเนื้อหาที่เหลือ
                        
                    if pattern_name.lower().startswith("[pattern name]:") and tmp_category.lower().startswith("[category]:") and user_input != "":
                        found_images, system_note, modified_content = find_images_in_content(user_input, PATTERN_PATH, output_name=input_name, start=1)
                        if not found_images:
                            print("!!! --- No [IMAGE: ...] tag found for pattern chart rendering --- !!!")
                            continue

                        user_input = modified_content
                        is_pattern_name = [input_name, os.path.splitext(found_images[0])[1]]
                        category = pattern_name + ", " + tmp_category
                        summary_content = pattern_name + "\n" + tmp_category + "\n" + user_input + "\n\n" + system_note
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
                    with open(full_txtfilename, "r", encoding="utf-8") as f:
                        knowledge_name = f.readline().strip()     # อ่าน [KNOWLEDGE NAME]: xxx
                        tmp_category = f.readline().strip()       # อ่าน [CATEGORY]: xxx
                        user_input = f.read().strip()             # อ่านเนื้อหาที่เหลือ

                        if knowledge_name.lower().startswith("[knowledge name]:") and tmp_category.lower().startswith("[category]:") and user_input != "":
                            found_images, system_note, modified_content = find_images_in_content(user_input, KNOWLEDGE_PATH, output_name=input_name, start=1)
                            user_input = modified_content
                            if found_images:
                                is_knowledge_name = [input_name, os.path.splitext(found_images[0])[1]]
                            else:
                                is_knowledge_name = [input_name, ".txt"]

                            category = knowledge_name + ", " + tmp_category
                            summary_content = knowledge_name + "\n" + tmp_category + "\n" + user_input + "\n\n" + system_note
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

                    img_name = os.path.splitext(os.path.basename(found_images[0]))[0]
                    img_ext = os.path.splitext(found_images[0])[1]
                    is_question_name = [img_name, img_ext]
                else:
                    is_question_name = [datetime.now().strftime("%y%m%d_%H%M"), ".txt"]
            else:
                continue

            # ค้นหา THEORY และ KNOWLEDGE ที่เกี่ยวข้องกับ user_input
            prompt_content, matched_knowledge = get_matched_knowledge(text_vectorstore, user_input, k=6, distance_threshold=0.6)
            if prompt_content:
                print(f"Found {matched_knowledge} knowledge contents from memory.\n")      # print(f"\n[EXPERIENCE_FOUND]:\n{prompt_content}\n")
                prompt_content = prompt_content.strip()
                prompt_content += "\n===\n\n"
            else:
                print("Knowledge contents could not be found.")
            
            if image_vectorstore != None:
                if is_pattern_name:
                    # ถ้าเป็นการเรียนรู้ PATTERN ใหม่ ให้ดึงฐานข้อมูล PATTERN เดิม 3 รูปที่ใกล้เคียงที่สุด
                    tmp_content, matched_patterns = find_matched_pattern(image_vectorstore, found_images[0], k=3, distance_threshold=0.3)
                    if matched_patterns > 0:
                        print(f"Found {matched_patterns} matched pattern(s).")
                        prompt_content += f"Matched patterns for [IMAGE 1]: {is_pattern_name[0]+is_pattern_name[1]}\n\n---\n\n"
                        prompt_content += tmp_content + "\n\n---\n\n"
                elif is_knowledge_name and is_knowledge_name[1] != ".txt":
                    # ถ้าเป็นการเรียนรู้ KNOWLEDGE ที่เป็นภาพซับซ้อน ให้ดึงฐานข้อมูล PATTERN 10 รูปเพื่อให้ครอบคลุมหลากหลาย PATTERN
                    tmp_content, matched_patterns = find_matched_pattern(image_vectorstore, found_images[0], k=5, distance_threshold=0.5)
                    if matched_patterns > 0:
                        print(f"Found {matched_patterns} matched pattern(s).")
                        prompt_content += f"Matched patterns for [IMAGE 1]: {is_knowledge_name[0]+is_knowledge_name[1]}\n\n---\n\n"
                        prompt_content += tmp_content + "\n\n---\n\n"
                elif is_question_name and is_question_name[1] != ".txt":
                    # ถ้าเป็นคำถาม QUESTION เพื่อการทดสอบ ที่เป็นภาพซับซ้อน ให้ดึงฐานข้อมูล PATTERN 10 รูปเพื่อให้ครอบคลุมหลากหลาย PATTERN
                    tmp_content, matched_patterns = find_matched_pattern(image_vectorstore, found_images[0], k=5, distance_threshold=0.5)
                    if matched_patterns > 0:
                        print(f"Found {matched_patterns} matched pattern(s).")
                        prompt_content += f"Matched patterns for [IMAGE 1]: {is_question_name}\n\n---\n\n"
                        prompt_content += tmp_content + "\n\n---\n\n"
            
            # ค้นหาภาพสำหรับ Reference (ถ้ามี)ใน user_input เพื่อนำมาประกอบใน content
            more_images, system_note, modified_content = find_images_in_content(user_input, IMAGE_PATH, start=len(found_images)+1)
            if more_images:
                user_input = modified_content
                summary_content += system_note

                found_images = list(dict.fromkeys(found_images + more_images))   # เพิ่ม more_images เข้าไปใน found_images โดยภาพต้องไม่ซ้ำกัน

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
                        r_found_images, r_system_note, r_modified_content = find_images_in_content(correction_input, IMAGE_PATH, start=len(total_images_list)+1)
                        if r_found_images:
                            # เพิ่มไฟล์ภาพเพิ่มเติม(ถ้ามี)ใน correction input โดยสร้างรวมกับภาพเดิมที่เคยคุยกันใน session แรกด้วย
                            temp_images_list += total_images_list + r_found_images
                            correction_input = r_modified_content
                            refined_prompt_content += r_system_note
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
    source_content = summary_content
    if os.path.exists(txt_source_path):
        with open(txt_source_path, "r", encoding="utf-8") as f:
            existing_source_content = f.read()

        if (
            ohlc_image_renderer.extract_image_specs(existing_source_content)
            and not ohlc_image_renderer.extract_image_specs(summary_content)
        ):
            correct_understanding = re.search(r"\[CORRECT UNDERSTANDING\]:\n.*", summary_content, re.DOTALL)
            source_content = existing_source_content
            if correct_understanding:
                source_content = source_content.rstrip() + "\n\n" + correct_understanding.group(0).strip()

    with open(txt_source_path, "w", encoding="utf-8") as f:
        f.write(source_content)
    
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


# ฟังก์ชั่นค้นหา PATTERN ที่คล้ายกับ image_path
def find_matched_pattern(vectorstore, image_path, k=10, distance_threshold=0.5):
    if not vectorstore or not image_path:
        return "", 0

    # 1. แปลงภาพปัจจุบันเป็น Vector
    query_vector = image_embed.encode(Image.open(image_path)).tolist()

    # 2. ค้นหาในสมองพร้อมค่าความห่าง (Score) จำนวน k ตัว, ผลลัพธ์ที่ได้คือ List ของ tuple (Document, score)
    # ใน FAISS L2 Distance: ยิ่งน้อยยิ่งเหมือน 0.0 คือเหมือนเป๊ะ)
    results = vectorstore.similarity_search_with_score_by_vector(query_vector, k=k)

    passed_threshold = []
    fallback_pool = []
    
    # 3. แยกกลุ่มผลลัพธ์: กลุ่มที่ผ่านเกณฑ์ (High Quality) และกลุ่มสำรอง (Best Effort)
    for doc, score in results:
        if score <= distance_threshold:
            passed_threshold.append((doc, score))
        else:
            print("!!! --- Important: Found patterns in memory didn't pass 'L2 Score Search'. Try adjusting 'Distance Threshold' --- !!!")
            fallback_pool.append((doc, score))
    
    # 4. ตรวจสอบจำนวน: ถ้าตัวที่ผ่านเกณฑ์มีน้อยกว่า k/2 ให้ใช้ตรรกะ "อย่างน้อยครึ่งหนึ่ง (ปัดเศษขึ้น)" : (k + 1) // 2 และดึงจาก fallback_pool มาเติม
    min_required = (k + 1) // 2
    final_selection = passed_threshold
    
    if len(final_selection) < min_required:
        # คำนวณว่ายังขาดอีกกี่ตัวเพื่อให้ครบ (k + 1) // 2
        needed = min_required - len(final_selection)
        # ดึงภาพที่ใกล้ที่สุดจากกลุ่มสำรองมาเติมให้ครบโควตาขั้นต่ำ
        final_selection.extend(fallback_pool[:needed])

    # 5. ประกอบร่างพร้อมติด Tag คุณภาพ (MATCHED PATTERN / BEST EFFORT PATTERN)
    matched_patterns = []
    
    for doc, score in final_selection:
        # ดึงข้อมูล OHLC และคำบรรยายที่เรา save ไว้ใน page_content
        pattern_info = doc.page_content
        source_file = doc.metadata.get('source', 'Unknown')
        
        # ติด Tag เพื่อบอกระดับความมั่นใจให้ David
        label = "MATCHED PATTERN" if score <= distance_threshold else "BEST EFFORT PATTERN"
        
        matched_patterns.append(
            f"[{label}]\n"
            f"Match Score (L2): {score:.4f}\n"
            f"Source: {source_file}\n"
            f"Details: {pattern_info}"
        )

    if not matched_patterns:
        return "", 0             # ไม่พบรูปแบบที่มั่นใจพอ
    
    # รวมข้อมูลแล้วครอบด้วย <Memory_Reference>
    joined_patterns = "\n\n---\n\n".join(matched_patterns)
    str_matched_patterns = f"<Memory_Reference>\n{joined_patterns}\n</Memory_Reference>\n"
    no_of_matched_patterns = len(matched_patterns)

    return str_matched_patterns, no_of_matched_patterns


# ฟังก์ชั่นดึง THEORY และ KNOWLEDGE จาก MEMORY
def get_matched_knowledge(vectorstore, query, k=6, distance_threshold=0.6):
    if not vectorstore or query == "":
        return "", 0
    
    # 1. ค้นหาข้อมูลพร้อมคะแนนความห่าง (L2 Score) ทั้งหมด k ตัว
    # ใน FAISS L2 Distance: ยิ่งน้อยยิ่งเหมือน (แนะนำค่าประมาณ 0.5 - 0.7 สำหรับ Text)
    results = vectorstore.similarity_search_with_score(query, k=k)
    matched_knowledge = []
    
    # 2. แยกกลุ่มผลลัพธ์: กลุ่มที่ผ่านเกณฑ์ (High Quality) และกลุ่มสำรอง (Best Effort)
    passed_threshold = []
    fallback_pool = []
    
    for doc, score in results:
        if score <= distance_threshold:
            passed_threshold.append((doc, score))
        else:
            print("!!! --- Important: Found knowledge in memory didn't pass 'L2 Score Search'. Try adjusting 'Distance Threshold' --- !!!")
            fallback_pool.append((doc, score))
    
    # 3. ตรวจสอบจำนวน: ถ้าตัวที่ผ่านเกณฑ์มีน้อยกว่า k/2 ให้ใช้ตรรกะ "อย่างน้อยครึ่งหนึ่ง (ปัดเศษขึ้น)" : (k + 1) // 2 และดึงจาก fallback_pool มาเติม
    min_required = (k + 1) // 2
    final_selection = passed_threshold

    if len(final_selection) < min_required:
        # คำนวณว่ายังขาดอีกกี่ตัวเพื่อให้ครบ (k + 1) // 2
        needed = min_required - len(final_selection)
        # ดึงความรู้ตัวที่ใกล้ที่สุดจากกลุ่มสำรองมาเติมให้ครบโควตาขั้นต่ำ
        final_selection.extend(fallback_pool[:needed])
    
    # 4. ดึง Metadata category ที่เราตั้งไว้ (เช่น 'THEORY' หรือ 'KNOWLEDGE')
    for i, (doc, score) in enumerate(final_selection):
        category = doc.metadata.get('category', 'KNOWLEDGE').upper()
        source = doc.metadata.get('source', 'unknown')
        
        # ใส่ Tag เพื่อให้รู้ว่าเป็นข้อมูลระดับไหน
        label = "MATCHED" if score <= distance_threshold else "BEST EFFORT"
        matched_knowledge.append(f"[{category}] ({label} - Score: {score:.2f}, Source: {source}):\n{doc.page_content}")
    
    if not matched_knowledge:
        return "", 0

    # รวมข้อมูลแล้วครอบด้วย tag <Memory_Reference>
    joined_knowledge = "\n---\n".join(matched_knowledge)
    str_matched_knowledge = f"<Memory_Reference>\n{joined_knowledge}\n</Memory_Reference>\n"
    no_of_matched_knowledge = len(matched_knowledge)

    return str_matched_knowledge, no_of_matched_knowledge


# ฟังก์ชั่นค้นหา [IMAGE: ...] ในข้อความ แล้ว generate ภาพและ OHLC reference จาก tag นั้น
def find_images_in_content(text_to_be_searched, image_path, output_name=None, start=1):
    found_images = []
    system_note = "\n<System Note: "
    ohlc_references = ""
    content_text = text_to_be_searched
    image_specs = ohlc_image_renderer.extract_image_specs(content_text)    

    if not image_specs:
        return [], "", content_text

    final_replacements = []

    for offset, image_spec in enumerate(image_specs):
        image_number = start + offset

        try:
            config = ohlc_image_renderer.parse_image_spec(image_spec)
            if output_name:
                base_name = os.path.splitext(os.path.basename(output_name))[0]
                image_name = base_name if len(image_specs) == 1 else f"{base_name}_{offset + 1}"
            else:
                image_name = ohlc_image_renderer.filename_from_config(config)

            full_path, config, df = ohlc_image_renderer.render_from_image_spec(
                image_spec=image_spec,
                directory=image_path,
                name=image_name,
            )
            found_file = os.path.basename(full_path)
            found_images.append(full_path)
            description = ohlc_image_renderer.describe_config(config)
            final_replacements.append(description)
            system_note += f"IMAGE {image_number} is a generated chart of {description} saved as {found_file}, "
            ohlc_references += ohlc_image_renderer.format_reference(df, image_number)
            print(f"Generated chart image: {full_path}")
            print(f"Successfully fetched {len(df)} bars")
        except Exception as e:
            print(f"!!! --- Unable to render [IMAGE: {image_spec}]: {e} --- !!!")
            final_replacements.append(f"[IMAGE: {image_spec}]")
    
    replacement_iter = iter(final_replacements)
    content_text = ohlc_image_renderer.IMAGE_TAG_PATTERN.sub(lambda m: next(replacement_iter), content_text)

    if found_images:
        system_note += ">\n"
        system_note += ohlc_references
    else:
        system_note = ""

    print(f"\n{len(found_images)} image(s) generated from inline IMAGE tag.")
    print(f"IMAGE: {[os.path.splitext(os.path.basename(path))[0] for path in found_images]}\n")
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
            
            # อ่านข้อมูลที่ส่งมาจาก FastAPI ทีละบรรทัด (Chunks)
            for line in response.iter_lines():
                if not line: continue
                
                try:
                    # แปลงจาก JSON String กลับเป็น Dictionary
                    chunk = json.loads(line.decode("utf-8"))
                    
                    # ตรวจสอบว่าเป็น Chunk ข้อมูล Usage หรือไม่
                    if 'usage' in chunk:
                        input_tokens = chunk['usage'].get('prompt_tokens', 0)
                        output_tokens = chunk['usage'].get('completion_tokens', 0)
                        total_input_tokens += input_tokens
                        total_output_tokens += output_tokens
                        print(f"\n[SESSION TOKEN USAGE] Input: {input_tokens} | Output: {output_tokens} | Total: {input_tokens+output_tokens}")
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
                            continue

                    # ประมวลผลข้อความตอบกลับปกติ
                    if 'choices' in chunk and len(chunk['choices']) > 0:
                        delta = chunk['choices'][0].get('delta', {})
                        content = delta.get('content', '')
                        if content:
                            print(content, end="", flush=True)
                            analysis += content
                except json.JSONDecodeError:
                    continue

            print("\n" + "-"*30)

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
        
        content_files = {
            os.path.splitext(f)[0]
            for f in os.listdir(PATTERN_PATH)
            if f.lower().endswith(".txt") and not f.lower().endswith("_ohlc.txt")
        }
    
        # ใช้เฉพาะ text file ที่มี [IMAGE: ...] อยู่ภายใน แล้ว render chart image เอง
        pairs = sorted(list(content_files))
        
        pattern_docs = []
        metadatas_list = []
        print(f"Absorbing... [{len(pairs)} pattern(s)]")
        
        for unit in pairs:
            txt_name = unit + ".txt"
            txt_path = os.path.join(PATTERN_PATH, txt_name)
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
            
            image_specs = ohlc_image_renderer.extract_image_specs(content_text)
            if not image_specs:
                print(f"!!! --- No [IMAGE: ...] tag in {txt_name}, Skipping image brain... --- !!!\n")
                continue

            try:
                pattern_path, _, df = ohlc_image_renderer.render_from_image_spec(image_specs[0], PATTERN_PATH, unit)
                full_content += ohlc_image_renderer.format_reference(df, 1)
            except Exception as e:
                print(f"!!! --- Unable to render chart for {unit}: {e}, Skipping... --- !!!\n")
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
        
        content_files = {
            os.path.splitext(f)[0]
            for f in os.listdir(KNOWLEDGE_PATH)
            if f.lower().endswith(".txt") and not f.lower().endswith("_ohlc.txt")
        }

        image_content_files = set()
        for unit in content_files:
            txt_path = os.path.join(KNOWLEDGE_PATH, unit + ".txt")
            try:
                with open(txt_path, "r", encoding="utf-8") as f:
                    content_for_scan = f.read()
            except Exception as e:
                print(f"Error scanning {txt_path}: {e}")
                continue
            if ohlc_image_renderer.extract_image_specs(content_for_scan):
                image_content_files.add(unit)

        # ดึงชื่อ text files ที่ไม่มี [IMAGE: ...] เพื่อเอา KNOWLEDGE บันทึกลงใน text brain อย่างเดียว
        not_pairs = sorted(list(content_files - image_content_files))
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

        # ใช้ text file ที่มี [IMAGE: ...] เพื่อ render chart แล้วบันทึกลง image brain
        pairs = sorted(list(image_content_files))
        if not pairs:
            print("No image knowledge files found.")
        else:
            print(f"Absorbing image knowledge... [{len(pairs)} unit(s)]")
            # และดึงฐานข้อมูลส่วน KNOWLEDGE ที่มีภาพประกอบด้วย
            for unit in pairs:
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

                image_specs = ohlc_image_renderer.extract_image_specs(content_text)
                if not image_specs:
                    print(f"!!! --- No [IMAGE: ...] tag in {txt_name}, Skipping image brain... --- !!!\n")
                    continue

                try:
                    chart_path, _, df = ohlc_image_renderer.render_from_image_spec(image_specs[0], KNOWLEDGE_PATH, unit)
                    chart_name = os.path.basename(chart_path)
                    full_content += ohlc_image_renderer.format_reference(df, 1)
                except Exception as e:
                    print(f"!!! --- Unable to render chart for {unit}: {e}, Skipping... --- !!!\n")
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


def normalize_ohlc_columns(df: pd.DataFrame, message_col: str = "Message") -> pd.DataFrame:
    """Return dataframe with lowercase columns: open, high, low, close."""
    df = df.copy()

    if message_col in df.columns:
        ohlc = df[message_col].astype(str).str.split(",", expand=True)
        if ohlc.shape[1] < 4:
            raise ValueError(f"Column '{message_col}' must contain open,high,low,close")
        df[["open", "high", "low", "close"]] = ohlc.iloc[:, :4].astype(float)
        return df[["open", "high", "low", "close"]]

    lower_map = {str(c).strip().lower(): c for c in df.columns}
    required = ["open", "high", "low", "close"]
    missing = [c for c in required if c not in lower_map]
    if missing:
        raise ValueError(
            "Expected either Message='open,high,low,close' or columns open/high/low/close. "
            f"Missing: {missing}. Columns found: {list(df.columns)}"
        )

    out = df[[lower_map[c] for c in required]].copy()
    out.columns = required
    return out.astype(float)


def normalize_datetime_index(
    df: pd.DataFrame,
    datetime_col: Optional[str] = None,
    source_tz_if_naive: str = "UTC",
    target_tz: str = "Asia/Bangkok",
    round_to_minute: bool = False,
) -> pd.DataFrame:
    """Set DatetimeIndex, convert to target_tz, then remove timezone."""
    df = df.copy()

    if datetime_col:
        if datetime_col not in df.columns:
            raise ValueError(f"datetime_col '{datetime_col}' was not found. Columns: {list(df.columns)}")
        dt = pd.to_datetime(df[datetime_col], errors="coerce")
        df = df.drop(columns=[datetime_col])
    else:
        dt = pd.to_datetime(df.index, errors="coerce")

    if dt.isna().any():
        raise ValueError(f"Datetime parsing failed for {int(dt.isna().sum())} row(s).")

    dt_index = pd.DatetimeIndex(dt)

    if dt_index.tz is None:
        dt_index = dt_index.tz_localize(source_tz_if_naive).tz_convert(target_tz)
    else:
        dt_index = dt_index.tz_convert(target_tz)

    dt_index = dt_index.tz_localize(None)

    if round_to_minute:
        dt_index = dt_index.round("min")

    df.index = dt_index
    return df.sort_index()


def load_ohlc_file(
    input_path: str | Path,
    datetime_col: Optional[str] = None,
    message_col: str = "Message",
    source_tz_if_naive: str = "UTC",
    target_tz: str = "Asia/Bangkok",
    round_to_minute: bool = False,
) -> pd.DataFrame:
    input_path = Path(input_path)
    suffix = input_path.suffix.lower()

    if suffix == ".csv":
        raw = pd.read_csv(input_path, index_col=0 if datetime_col is None else None, parse_dates=False)
    elif suffix in [".xlsx", ".xls"]:
        raw = pd.read_excel(input_path, index_col=0 if datetime_col is None else None)
    else:
        raise ValueError("Supported input files: .csv, .xlsx, .xls")

    raw = normalize_datetime_index(
        raw,
        datetime_col=datetime_col,
        source_tz_if_naive=source_tz_if_naive,
        target_tz=target_tz,
        round_to_minute=round_to_minute,
    )
    return normalize_ohlc_columns(raw, message_col=message_col)


def slice_ohlc(
    df: pd.DataFrame,
    start: Optional[str],
    end: Optional[str],
    window_size: int = 0,
) -> pd.DataFrame:
    """Slice OHLC dataframe by either datetime range or fixed bar window."""
    if window_size < 0:
        raise ValueError("window_size must be >= 0")

    if window_size > 0:
        if start:
            anchor = pd.to_datetime(start)
            candidates = df.loc[df.index >= anchor].copy()
        else:
            candidates = df.copy()

        out = candidates.iloc[:window_size].copy()
        if out.empty:
            raise ValueError(f"No OHLC rows found from start={start} for window_size={window_size}")
        if len(out) < window_size:
            raise ValueError(
                f"Only {len(out)} bar(s) available from start={start}, "
                f"but window_size={window_size} was requested."
            )
        return out

    if start or end:
        df = df.loc[start:end].copy()
    if df.empty:
        raise ValueError(f"No OHLC rows found between start={start} and end={end}")
    return df


def render_bw_candles(
    df: pd.DataFrame,
    output_path: str | Path,
    title: str = "OHLC Chart",
    show_grid: bool = False,
    hide_axes: bool = False,
    dpi: int = 160,
) -> None:
    """Render black/white candlesticks using pure matplotlib only."""
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(14, 7))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    x = mdates.date2num(df.index.to_pydatetime())
    candle_width = (x[1] - x[0]) * 0.65 if len(x) >= 2 else 0.0005

    price_range = float(df["high"].max() - df["low"].min())
    min_body = max(price_range * 0.001, 1e-8)

    for xi, row in zip(x, df.itertuples(index=False)):
        o = float(row.open)
        h = float(row.high)
        l = float(row.low)
        c = float(row.close)

        body_bottom = min(o, c)
        body_top = max(o, c)
        body_height = body_top - body_bottom

        if body_height == 0:
            body_bottom = o - min_body / 2
            body_top = o + min_body / 2
            body_height = min_body

        face_color = "white" if c >= o else "black"

        if h > body_top:
            ax.vlines(xi, body_top, h, linewidth=1.0, color="black", zorder=1)
        if l < body_bottom:
            ax.vlines(xi, l, body_bottom, linewidth=1.0, color="black", zorder=1)

        body = Rectangle(
            (xi - candle_width / 2, body_bottom),
            candle_width,
            body_height,
            linewidth=1.0,
            edgecolor="black",
            facecolor=face_color,
            zorder=2,
        )
        ax.add_patch(body)

    ax.set_xlim(x[0] - candle_width, x[-1] + candle_width)
    y_pad = price_range * 0.05 if price_range > 0 else 1
    ax.set_ylim(float(df["low"].min()) - y_pad, float(df["high"].max()) + y_pad)

    if show_grid:
        ax.grid(True, linewidth=0.4, alpha=0.25)
    else:
        ax.grid(False)

    if hide_axes:
        ax.set_axis_off()
    else:
        ax.set_title(title)
        ax.set_ylabel("Price")
        ax.xaxis_date()
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%y-%m-%d\n%H:%M"))
        fig.autofmt_xdate()

    fig.tight_layout()
    fig.savefig(output_path, dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def render_ohlc_image(
    input_path: str | Path,
    output_path: str | Path,
    start: Optional[str] = None,
    end: Optional[str] = None,
    window_size: int = 0,
    title: str = "OHLC Chart",
    datetime_col: Optional[str] = None,
    message_col: str = "Message",
    source_tz_if_naive: str = "UTC",
    target_tz: str = "Asia/Bangkok",
    round_to_minute: bool = False,
    show_grid: bool = False,
    hide_axes: bool = False,
    dpi: int = 160,
) -> pd.DataFrame:
    df = load_ohlc_file(
        input_path=input_path,
        datetime_col=datetime_col,
        message_col=message_col,
        source_tz_if_naive=source_tz_if_naive,
        target_tz=target_tz,
        round_to_minute=round_to_minute,
    )
    df = slice_ohlc(df, start=start, end=end, window_size=window_size)
    render_bw_candles(df, output_path, title=title, show_grid=show_grid, hide_axes=hide_axes, dpi=dpi)
    return df


if __name__ == "__main__":
    main()