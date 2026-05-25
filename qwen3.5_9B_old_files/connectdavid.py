import re
import os
import ollama
from langchain_ollama import OllamaEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
from datetime import datetime
from PIL import Image
from transformers import AutoTokenizer

# --- Path Configuration ---
BASE_DIR = r"C:\david"
BOOK_PATH = os.path.join(BASE_DIR, "book")
CHART_PATH = os.path.join(BASE_DIR, "chart")
LESSON_PATH = os.path.join(BASE_DIR, "wei_lesson")
JOURNAL_PATH = os.path.join(BASE_DIR, "journal")
BRAIN_PATH = os.path.join(BASE_DIR, "brain")
MODEL_NAME = "qwen3.5:9b"
EMBEDDING_MODEL = "nomic-embed-text"

SYSTEM_PROMPT = """You are David, an AI assistant specialized in Al Brooks Price Action.
Your role is to learn Price Action from a user, Wei. You use three books of Al Brooks as references on trading terminology and methodology.
Wei will provide you more charts to learn from time to time in order to teach you. You must learn what is "Correct" or "Wrong" about charts reading.
He will test you to see your development until you are ready to be his co-pilot in trading.
Moreover, before giving the final answer(output), please provide a brief internal monologue or step-by-step reasoning explaining how you interpreted the request and which parts of the retrieved context you are using to formulate your response.
CRITICAL RULE: You must answer something based on your found context and logic before getting the advise from Wei. Even if it is wrong, Wei will explain to you later. Give the best answer you can!"""

# --- David's Brain & Reasoning Configuration ---
options_config = {
    'temperature': 0.1,           # คงความแม่นยำ (ที่คุณต้องการ)
    'num_ctx': 32768,             # ขยายขนาดสมองรวม (จำได้ยาวขึ้น 4 เท่า)
    'num_predict': 4096,          # เพิ่มเพดานคำพูดให้ยาวขึ้นเป็น 4096 tokens (ไม่ให้โดนตัดบทดื้อๆ)
    'repeat_penalty': 1.2,        # ป้องกันการพูดวนไปวนมา (แก้ปัญหาการ Loop)
    'top_k': 20,                  # จำกัดทางเลือกคำให้คมขึ้น
    'top_p': 0.5,                 # เน้นคำตอบที่มีความน่าเชื่อถือสูง
    'num_thread': 8               # บังคับใช้ CPU Cores ตามความแรงเครื่อง (ปรับตามจำนวน core จริงของคุณ)
}

total_images_list: list[str] = []
total_input_tokens = 0
total_output_tokens =  0

tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-7B-Instruct")

for p in [BOOK_PATH, CHART_PATH, LESSON_PATH, JOURNAL_PATH, BRAIN_PATH]:
    os.makedirs(p, exist_ok=True)

def main():
    global total_images_list
    global total_input_tokens
    global total_output_tokens

    embeddings = OllamaEmbeddings(model=EMBEDDING_MODEL)
    
    # เช็คว่ามีไฟล์สมอง index.faiss อยู่แล้วหรือไม่ ถ้ามีอยู่แล้วให้ update ภาพและความรู้เพิ่มเติม (ถ้ามี)
    if os.path.exists(os.path.join(BRAIN_PATH, "index.faiss")):
        vectorstore = FAISS.load_local(BRAIN_PATH, embeddings, allow_dangerous_deserialization=True)
    # ถ้าไม่มีก็ให้สร้างไฟล์สมอง index.faiss
    else:
        vectorstore = create_brain()
    
    while True:
        try:        
            full_history = ""
            mode_text = ""
            found_images = []
            system_note = ""
            response_text = ""
            added_text = ""
            initial_analysis = ""
            
            print("\n--- David is online ---")
            print("** Asking any questions by using '?' **")
            print("** Type 'file' for loading a file **")
            print("** Type 'exit'/'quit'/'bye' to terminate **\n")
            user_input = input("Wei: ").strip()
            
            if user_input.lower() in ['exit', 'quit', 'bye']:
                break  
            
            # ตรวจสอบดูว่าต้องการให้ input ด้วย lesson file หรือไม่
            if "file" in user_input.lower():
                file_name = input("Enter file name: ").strip()
                    
                if not file_name.lower().endswith(".txt"):
                    file_name += ".txt"
                
                if os.path.exists(os.path.join(LESSON_PATH, file_name)):
                    with open(os.path.join(LESSON_PATH, file_name), "r", encoding="utf-8") as f:
                        user_input = f.read()
                        if user_input == "":
                            continue
                        
                    full_history = f"[LESSON_FROM_WEI]:\n{user_input}\n\n"
                    added_text = "David, acknowledge this lesson which is derived from Wei's experiences and explain briefly if you understand it."
                    mode_text = "David is absorbing your lesson...\n"
                    response_text += "[DAVID_ACKNOWLEDGEMENT]:\n"
                else:
                    print("Error: File name was not found.")
                    continue
            else:
                # ตรวจสอบดูว่าเป็นชุดคำถามหรือไม่
                if "?" in user_input:
                    full_history = f"[QUESTION_FROM_WEI]:\n{user_input}\n\n"
                    mode_text = "David is thinking...\n"
                    response_text += "[DAVID_ANALYSIS]:\n"
                # หรือเป็นการให้ความรู้
                else:
                    full_history = f"[KNOWLEDGE_FROM_WEI]:\n{user_input}\n\n"
                    added_text = "David, acknowledge this knowledge which is derived from Wei's experiences and explain briefly if you understand it."
                    mode_text = "David is absorbing your knowledge...\n"
                    response_text += "[DAVID_ACKNOWLEDGEMENT]:\n"
            
            # ค้นหาภาพ(ถ้ามี)ใน user_input เพื่อนำมาประกอบใน initial_content
            found_images, system_note = get_content_images(user_input)
            if found_images:
                full_history += system_note
            
            # ดึงข้อมูลจากประสบการณ์มาประกอบใน Content
            experience_content = get_experience_content(vectorstore, user_input, 10)
            if experience_content.strip():
                print("Experience contents was found.\n")      # print(f"\n[EXPERIENCE_FOUND]:\n{experience_content}\n")
            else:
                print("Experience contents could not be found.\n")
            
            initial_content = f"[EXPERIENCE_FROM_MEMORY]:\n{experience_content}\n\n"
            initial_content += full_history + added_text
            initial_prompt = [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': initial_content, 'images': found_images}
            ]
            
            # ประเมินปริมาณ Tokens ทั้งหมดที่ต้องใช้ในการส่งให้ข้อความและภาพให้ David ผ่าน prompt
            text_tokens, image_tokens = get_token_count(initial_prompt)
            print(f"[TOKEN ESTIMATION]: Text input = {text_tokens} | Image input = {image_tokens} | Total input = {text_tokens + image_tokens}\n")
                
            # David เริ่มกระบวนการคิด... (ใช้เวลา)
            chunk = None
            print(mode_text)
                
            stream = ollama.chat(model=MODEL_NAME, messages=initial_prompt, stream=True, options=options_config)
            
            # David เริ่มแสดงความคิดภายใน (Internal Monologue) โดยพิมพ์ Reasoning ออกมาทีละชิ้นทันที 
            is_first_chunk = True
            for chunk in stream:
                if is_first_chunk and 'message' in chunk and 'content' in chunk['message']:
                    content = response_text + chunk['message']['content']
                    print(content, end="", flush=True)
                    initial_analysis += content
                    is_first_chunk = False
                elif 'message' in chunk and 'content' in chunk['message']:
                    content = chunk['message']['content']
                    print(content, end="", flush=True)
                    initial_analysis += content
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
            
            # ถ้าเกิด ollama.chat เกิด Crash หรือทำงานไม่สำเร็จ หรือ ollama ตัดการทำงานของ David ให้กลับไปลูปถาม Input ใหม่
            if not initial_analysis.strip():
                print("\nError: Empty Response\n")
                continue
            
            # เมื่อการวิเคราะห์ของ David สำเร็จ ให้บันทึกภาพ(ถ้ามี)จาก Input เพิ่มเข้าไปใน total_images_list ซึ่งเป็น Global Variable โดยภาพจะไม่ซ้ำกัน
            total_images_list = list(dict.fromkeys(total_images_list + found_images))
            print(f"{len(total_images_list)} image(s) realized by David.")
            print(f"Image: {[os.path.splitext(os.path.basename(path))[0] for path in total_images_list]}\n")
                
            full_history += f"\n{initial_analysis}\n\n"

            # Loop โต้ตอบ จนกว่า Wei จะบอกว่า "ถูกต้อง"
            while True:
                try:
                    next_found_images = []
                    next_system_note = ""
                    refinement_text = "[DAVID_REFINEMENT_ANALYSIS]:\n"
                    refined_analysis = ""
                    
                    feedback = input("Do I understand correctly (y/n/quit)? > ").strip()
            
                    if feedback == "y":
                        # เมื่อตกลงว่าถูกต้องแล้ว ให้บันทึกลงในคลังสมอง index.faiss ของ David
                        full_history += "[FINAL_CONSENSUS]: Verified as correct by Wei."
                        # ใช้เวลาที่ให้ Knowledge บันทึกเป็นชื่อไฟล์ความรู้
                        current_time_string = datetime.now().strftime("%y%m%d_%H%M")
                        save_to_brain(current_time_string, full_history, vectorstore)
                    
                        print("\nKnowledge was memorized successfully.")
                        print(f"[TOTAL TOKEN USAGE] Input: {total_input_tokens} | Output: {total_output_tokens} | Total: {total_input_tokens+total_output_tokens}\n")
                
                        total_input_tokens = 0
                        total_output_tokens = 0
                        
                        total_images_list.clear()
                        break
                
                    elif feedback == "n":
                        correction_input = input("Wei, please explain: ").strip()
                        full_history += f"[WEI_CORRECTION]:\n{correction_input}\n\n"
                        
                        temp_images_list = []
                        
                        # ค้นหาภาพเพิ่มเติม(ถ้ามี)ใน correction input เพื่อนำมาประกอบใน refined_content
                        next_found_images, next_system_note = get_content_images(correction_input)
                        if next_found_images:
                            # เพิ่มไฟล์ภาพเพิ่มเติม(ถ้ามี)ใน correction input โดยสร้างรวมกับภาพเดิมที่เคยคุยกันใน session แรกด้วย
                            temp_images_list += total_images_list + next_found_images
                            full_history += next_system_note
                        else:
                            temp_images_list = total_images_list
                        
                        refined_content = full_history + "David, you didn't understand it precisely. Read my correction and analyze it again.\n\n"
                        refined_prompt = [
                            {'role': 'system', 'content': SYSTEM_PROMPT},
                            {'role': 'user', 'content': refined_content, 'images': temp_images_list}
                        ]           
                
                        # ประเมินปริมาณ Tokens ที่ต้องใช้ในส่วน Refinement
                        text_tokens, image_tokens = get_token_count(refined_prompt)
                        print(f"\n[TOKEN ESTIMATION]: Text input = {text_tokens} | Image input = {image_tokens} | Total input = {text_tokens + image_tokens}\n")
                
                        # David เริ่มการวิเคราะห์เพื่อปรับปรุงความคิด
                        chunk = None
                        print("David is refining his thought...\n", flush=True)
                
                        refined_stream = ollama.chat(model=MODEL_NAME, messages=refined_prompt, stream=True, options=options_config)
                        
                        # David เริ่มแสดงความคิดภายใน (Internal Monologue) โดยพิมพ์ Reasoning ออกมาทีละชิ้นทันที 
                        is_first_chunk = True
                        for chunk in refined_stream:
                            if is_first_chunk and 'message' in chunk and 'content' in chunk['message']:
                                content = refinement_text + chunk['message']['content']
                                print(content, end="", flush=True)
                                refined_analysis += content
                                is_first_chunk = False
                            elif 'message' in chunk and 'content' in chunk['message']:
                                content = chunk['message']['content']
                                print(content, end="", flush=True)
                                refined_analysis += content
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
                        
                        # ถ้าเกิด ollama.chat เกิด Crash หรือทำงานไม่สำเร็จ หรือ ollama ตัดการทำงานของ David ให้กลับไปลูปถาม Input ใหม่
                        if not refined_analysis.strip():
                            print("\nError: Empty Response\n")
                            continue
                        
                        # เมื่อการวิเคราะห์ของ David สำเร็จ ให้บันทึกภาพ(ถ้ามี)จาก Input เพิ่มเข้าไปใน total_images_list ซึ่งเป็น Global Variable โดยภาพจะไม่ซ้ำกัน
                        total_images_list = list(dict.fromkeys(total_images_list + next_found_images))
                        print(f"Total {len(total_images_list)} image(s) realized by David.")
                        print(f"Image: {[os.path.splitext(os.path.basename(path))[0] for path in total_images_list]}\n")
                    
                        full_history += f"{refined_analysis}\n\n"
                        
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


def save_to_brain(unit_name, content, vectorstore):
    # 1. บันทึกข้อมูลเป็น Text File ลงโฟลเดอร์ journal
    os.makedirs(JOURNAL_PATH, exist_ok=True)
    file_path = os.path.join(JOURNAL_PATH, f"{unit_name}.txt")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    
    # 2. หั่นเนื้อหา (Split) ก่อนบันทึกลง FAISS เพื่อป้องกัน Error 400
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
    
    # 3. สร้าง Document ชั่วคราวขึ้นมาด้วย Tag 'EXPERIENCE' และหั่นออกมาเป็นชิ้นเล็กๆ (Chunks)
    temp_doc = Document(page_content=content, metadata={"category": "EXPERIENCE", "source": unit_name})
    docs = text_splitter.split_documents([temp_doc])
    
    # 4. บันทึกลง FAISS 
    vectorstore.add_documents(docs)
    vectorstore.save_local(BRAIN_PATH)


# ฟังก์ชั่นค้นหาชื่อภาพในข้อความ text_to_be_searched และคืนค่า List ของ Path รูปภาพที่พบ
def get_content_images(text_to_be_searched):
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


def get_experience_content(vectorstore, query, k=10):
    if not vectorstore:
        return ""
    
    # ค้นหาข้อมูลที่ใกล้เคียงกับคำถามที่สุด จำนวน k ลำดับ
    docs = vectorstore.similarity_search(query, k=k)
    content_list = []
    
    for doc in docs:
        # ดึง Metadata category ที่เราตั้งไว้ (เช่น 'THEORY' หรือ 'EXPERIENCE')
        category = doc.metadata.get('category', 'knowledge').upper()
        source = doc.metadata.get('source', 'unknown')
        
        # ประกอบร่างข้อความเพื่อให้ David แยกแยะที่มาได้
        content_list.append(f"[{category}] (Source: {source}):\n{doc.page_content}")
    
    return "\n---\n".join(content_list)


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


def create_brain():    
    embeddings = OllamaEmbeddings(model=EMBEDDING_MODEL)
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
    vectorstore = None
    
    # --- ส่วนที่ 1: รับทฤษฎี (THEORY) จากการอ่านหนังสือ (BOOKS) ---
    if os.path.exists(BOOK_PATH):
        books = [f for f in os.listdir(BOOK_PATH) if f.endswith(".pdf")]
    
        if books:
            print("David starts reading books...")
            theory_docs = []
            for i, file in enumerate(books):
                print(f"Loading No. {i+1}/{len(books)}: {file} ...")
                loader = PyPDFLoader(os.path.join(BOOK_PATH, file))
                data = loader.load()
                for d in data:
                    d.metadata["category"] = "THEORY"
                theory_docs.extend(data)
                print(f"Successfully loaded {file} (Total {len(data)} Pages)")
    
            print("Text splittering...")   
            splits = splitter.split_documents(theory_docs)
            print(f"Start embedding {len(splits)} pieces...")
            vectorstore = FAISS.from_documents(splits, embeddings)
            print("Successfully created brain from books")
        else:
            print("Error: No books found!")
    
    # --- ส่วนที่ 2: รับประสบการณ์ (EXPERIENCES) จากบันทึกบทสนทนา JOURNALS ---
    if os.path.exists(JOURNAL_PATH) and vectorstore:
        journal = [f for f in os.listdir(JOURNAL_PATH) if f.endswith(".txt")]
    
        if journal:
            print("Absorbing Experiences(Past Learning)...")
            experience_docs = []
            for file in journal:
                loader = TextLoader(os.path.join(JOURNAL_PATH, file), encoding='utf-8')
                data = loader.load()
                for d in data:
                    d.metadata["category"] = "EXPERIENCE"
                experience_docs.extend(data)
            if experience_docs:
                print("Successfully loading experiences")
                print("Text splittering...")
                splits = splitter.split_documents(experience_docs)
                print(f"   Adding {len(splits)} experience chunks to brain...")
                vectorstore.add_documents(splits)
                print("Experiences successfully absorbed.")
            else:
                print("Error in absorbing experiences.")
        else:
            print("No experiences found.")
    
    # --- บันทึกผลลัพธ์สุดท้าย ---
    if vectorstore:
        vectorstore.save_local(BRAIN_PATH)
        print("\n" + "="*50)
        print(f"Success! David's brain with experieces was created at: {BRAIN_PATH}")
        print("="*50)
        return vectorstore
    else:
        print("No data found to build brain!!")
        return None


if __name__ == "__main__":
    main()