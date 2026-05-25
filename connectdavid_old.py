import re
import os
import ollama
from langchain_ollama import OllamaEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
from datetime import datetime

# --- Path Configuration ---
BASE_DIR = r"C:\david"
BOOK_PATH = os.path.join(BASE_DIR, "book")
CHART_PATH = os.path.join(BASE_DIR, "chart")
LESSON_PATH = os.path.join(BASE_DIR, "wei_lesson")
JOURNAL_PATH = os.path.join(BASE_DIR, "journal")
BRAIN_PATH = os.path.join(BASE_DIR, "brain")
MODEL_NAME = "qwen3.5:9b"
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

content_images_list: list[str] = []
total_input_tokens = 0
total_output_tokens =  0

for p in [BOOK_PATH, CHART_PATH, LESSON_PATH, JOURNAL_PATH, BRAIN_PATH]:
    os.makedirs(p, exist_ok=True)

def update_brain(vectorstore):
    global content_images_list
    global total_input_tokens
    global total_output_tokens

    # ตรวจสอบไฟล์ใน /chart และ /wei_lesson
    chart_file = {os.path.splitext(f)[0]: f for f in os.listdir(CHART_PATH) if f.endswith(('.png', '.jpg'))}
    lesson_file = {os.path.splitext(f)[0] for f in os.listdir(LESSON_PATH) if f.endswith('.txt')}
    
    # จับคู่ไฟล์ที่มีชื่อเหมือนกัน
    pairs = sorted(list(chart_file.keys() & lesson_file))
    
    # ตรวจสอบไฟล์ที่เคย Process ไปแล้ว (ป้องกันการทำซ้ำ)
    log_file = os.path.join(BASE_DIR, "processed_file_log.txt")
    processed = set()
    
    if os.path.exists(log_file):
        with open(log_file, "r", encoding="utf-8") as f:
            processed = {line.strip() for line in f}
    
    new_items = [pair for pair in pairs if pair not in processed]
    
    if not new_items:
        print("No new item found.\n")
        return

    print(f"New item(s) found: {len(new_items)} unit(s).")
    
    for unit in new_items:
        chart_path = os.path.join(CHART_PATH, chart_file[unit])
        with open(os.path.join(LESSON_PATH, f"{unit}.txt"), "r", encoding="utf-8") as f:
            lesson_text = f.read()
        print(f"[Item: {unit}]")
        
        experience_content = get_experience_content(vectorstore, lesson_text, 10)
        if experience_content.strip():
            print("Experience contents was found.\n")      # print(f"\n[EXPERIENCE_FOUND]:\n{experience_content}\n")
        else:
            print("Experience contents could not be found.\n")
        
        # David เริ่มวิเคราะห์ครั้งแรก
        print(f"David is analyzing [Item: {unit}]...\n", flush=True)
        
        found_images, system_note = get_content_images(lesson_text, offset=1)
        
        temp_images_list = [chart_path] + found_images
        
        combined_content = f"[EXPERIENCE_FROM_MEMORY]:\n{experience_content}\n\n"
        combined_content += f"(System Note: Image 1 is {unit})\n\n"
        combined_content += f"[EXPLANATION_OF_Image 1]:\n{lesson_text}\n\n"
        combined_content += system_note
        combined_content += "David, do you understand it? If yes, please explain."
        initial_prompt = [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': combined_content, 'images': temp_images_list}
        ]    
        item_stream = ollama.chat(model=MODEL_NAME, messages=initial_prompt, stream=True, options=options_config)
        
        print("\n[DAVID_INITIAL_ANALYSIS]:\n", end="", flush=True)
        first_analysis = ""
        
        for chunk in item_stream:                                             # พิมพ์ Reasoning ออกมาทีละชิ้นทันที
            if 'message' in chunk and 'content' in chunk['message']:
                content = chunk['message']['content']
                print(content, end="", flush=True)
                first_analysis += content
        print("\n" + "-"*30)
        
        if chunk:
            input_tokens = chunk.get('prompt_eval_count', 0)
            total_input_tokens += input_tokens
            output_tokens = chunk.get('eval_count', 0)
            total_output_tokens += output_tokens
            total_session_tokens = input_tokens + output_tokens
            print(f"[SESSION TOKEN USAGE] Input: {input_tokens} | Output: {output_tokens} | Total: {total_session_tokens}\n")
            
            update_token_history(total_input_tokens, total_output_tokens)
        else:
            print(f"[SESSION TOKEN USAGE ERROR]: Problem with Ollama.\n")
        
        
        if not first_analysis.strip():
            print("\nError: Empty Response\n")
            continue
        
        content_images_list = list(dict.fromkeys(content_images_list + temp_images_list))
        print(f"\n{len(temp_images_list)} image(s) realized by David.")
        print(f"Image: {[os.path.splitext(os.path.basename(path))[0] for path in content_images_list]}\n")
        
        full_history = f"[LESSON_BY_WEI]:\n{lesson_text}\n\n"
        full_history += f"[DAVID_INITIAL_ANALYSIS]:\n{first_analysis}\n\n"
        
        # Loop โต้ตอบ จนกว่า Wei จะบอกว่า "ถูกต้อง"
        while True:
            feedback = input("Is it correct (y/n/quit)? > ").strip()
   
            if feedback == "y":
                full_history += "\n[FINAL_CONSENSUS]: Verified as correct by Wei."
                save_to_brain(unit, full_history, vectorstore)
                
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(unit + "\n")
                
                print(f"\n[Item: {unit}] finished.\n")
                print(f"[TOTAL TOKEN USAGE] Input: {total_input_tokens} | Output: {total_output_tokens} | Total: {total_input_tokens+total_output_tokens}\n")
                
                total_input_tokens = 0
                total_output_tokens = 0
                content_images_list.clear()
                break
            
            if feedback == "n":
                correction = input("Wei, please explain: ").strip()
                full_history += f"[WEI_CORRECTION]:\n{correction}\n\n"
                
                found_images, system_note = get_content_images(correction, offset=0)
                
                temp_images_list = content_images_list + found_images
                
                refined_content = full_history + system_note
                refined_content += "\n\nDavid, you're wrong about it. Look at the chart and rethink again.\n"
                refined_content += "Do you understand it? If yes, please explain."
                refined_prompt = [
                    {'role': 'system', 'content': SYSTEM_PROMPT},
                    {'role': 'user', 'content': refined_content, 'images': temp_images_list}
                ]           
                
                refined_stream = ollama.chat(model=MODEL_NAME, messages=refined_prompt, stream=True, options=options_config)
                
                print("\nDavid is refining his thought...\n", flush=True)
                refined_analysis = ""
                
                for chunk in refined_stream:                                             # พิมพ์ Reasoning ออกมาทีละชิ้นทันที
                    if 'message' in chunk and 'content' in chunk['message']:
                        content = chunk['message']['content']
                        print(content, end="", flush=True)
                        refined_analysis += content
                print("\n" + "-"*30)
                
                if chunk:
                    input_tokens = chunk.get('prompt_eval_count', 0)
                    total_input_tokens += input_tokens
                    output_tokens = chunk.get('eval_count', 0)
                    total_output_tokens += output_tokens
                    total_session_tokens = input_tokens + output_tokens
                    print(f"[SESSION TOKEN USAGE] Input: {input_tokens} | Output: {output_tokens} | Total: {total_session_tokens}\n")
                    
                    update_token_history(total_input_tokens, total_output_tokens)
                else:
                    print(f"[SESSION TOKEN USAGE ERROR]: Problem with Ollama.\n")
                
                if not refined_analysis.strip():
                    print("\nError: Empty Response\n")
                    continue
                
                content_images_list = list(dict.fromkeys(content_images_list + found_images))
                print(f"\nTotal {len(content_images_list)} image(s) realized by David.")
                print(f"Image: {[os.path.splitext(os.path.basename(path))[0] for path in content_images_list]}\n")
                
                full_history += f"[DAVID_REFINEMENT]:\n{refined_analysis}\n\n"
            
            if feedback == "quit":
                print(f"\nExit while not finish item: {unit}")
                print(f"[TOTAL TOKEN USAGE] Input: {total_input_tokens} | Output: {total_output_tokens} | Total: {total_input_tokens+total_output_tokens}\n")
                
                total_input_tokens = 0
                total_output_tokens = 0
                
                content_images_list.clear()
                break


def main():
    global content_images_list
    global total_input_tokens
    global total_output_tokens

    embeddings = OllamaEmbeddings(model="nomic-embed-text")
    
    # Check if brain exists
    if os.path.exists(os.path.join(BRAIN_PATH, "index.faiss")):
        vectorstore = FAISS.load_local(BRAIN_PATH, embeddings, allow_dangerous_deserialization=True)
        update_brain(vectorstore)
    else:
        vectorstore = create_brain()  
    
    while True:
        try:        
            print("--- David is online ---  **To give knowledge, start with 'k:'")
            user_input = input("Wei: ").strip()
            
            if user_input.lower() in ['exit', 'quit', 'bye']:
                break
            
            # --- โหมดป้อน Knowledge ---
            if user_input.lower().startswith("k:"):
                user_input = user_input[2:].strip()
                experience_content = get_experience_content(vectorstore, user_input, 10)
                if experience_content.strip():
                    print("Experience contents was found.\n")      # print(f"\n[EXPERIENCE_FOUND]:\n{experience_content}\n")
                else:
                    print("Experience contents could not be found.\n")
                
                print("David is absorbing your knowledge...\n")
                
                found_images, system_note = get_content_images(user_input, offset=0)
                
                combined_content = f"[EXPERIENCE_FROM_MEMORY]:\n{experience_content}\n\n[KNOWLEDGE_FROM_WEI]:\n{user_input}\n\n"
                combined_content += system_note
                combined_content += "\n\nDavid, acknowledge this knowledge which is derived from Wei's experiences and explain briefly if you understand it."
                initial_knowledge_prompt = [
                    {'role': 'system', 'content': SYSTEM_PROMPT},
                    {'role': 'user', 'content': combined_content, 'images': found_images}
                ]         
                knowledge_stream = ollama.chat(model=MODEL_NAME, messages=initial_knowledge_prompt, stream=True, options=options_config)
                
                print("\n[DAVID_FIRST_ACKNOWLEDGEMENT]:\n", end="", flush=True)
                first_acknowledgement = ""
        
                for chunk in knowledge_stream:                                             # พิมพ์ Reasoning ออกมาทีละชิ้นทันที
                    if 'message' in chunk and 'content' in chunk['message']:
                        content = chunk['message']['content']
                        print(content, end="", flush=True)
                        first_acknowledgement += content
                print("\n" + "-"*30)
                
                if chunk:
                    input_tokens = chunk.get('prompt_eval_count', 0)
                    total_input_tokens += input_tokens
                    output_tokens = chunk.get('eval_count', 0)
                    total_output_tokens += output_tokens
                    total_session_tokens = input_tokens + output_tokens
                    print(f"[SESSION TOKEN USAGE] Input: {input_tokens} | Output: {output_tokens} | Total: {total_session_tokens}\n")
                    
                    update_token_history(total_input_tokens, total_output_tokens)
                else:
                    print(f"[SESSION TOKEN USAGE ERROR]: Problem with Ollama.\n")
                
                if not first_acknowledgement.strip():
                    print("\nError: Empty Response\n")
                    continue
                
                content_images_list = list(dict.fromkeys(content_images_list + found_images))
                print(f"\n{len(found_images)} image(s) realized by David.")
                print(f"Image: {[os.path.splitext(os.path.basename(path))[0] for path in content_images_list]}\n")
                
                full_history = f"[KNOWLEDGE_FROM_WEI]:\n{user_input}\n\n"
                full_history += f"[DAVID_FIRST_ACKNOWLEDGEMENT]:\n{first_acknowledgement}\n\n"

                # Loop โต้ตอบ จนกว่า Wei จะบอกว่า "ถูกต้อง"
                while True:
                    feedback = input("Do I understand correctly (y/n/quit)? > ").strip()
            
                    if feedback == "y":
                        full_history += "\n[FINAL_CONSENSUS]: Verified as correct by Wei."                       
                        current_time_string = datetime.now().strftime("%Y%m%d_%H%M%S")    # ใช้ชื่อเวลาที่ให้ Knowledge เป็นชื่อความรู้
                        save_to_brain(current_time_string, full_history, vectorstore)
                        
                        print("\nKnowledge has been acknowledged correctly.")
                        print(f"[TOTAL TOKEN USAGE] Input: {total_input_tokens} | Output: {total_output_tokens} | Total: {total_input_tokens+total_output_tokens}\n")
                
                        total_input_tokens = 0
                        total_output_tokens = 0
                        
                        content_images_list.clear()
                        break
                    
                    if feedback == "n":
                        correction = input("Wei, please explain again: ").strip()
                        full_history += f"[WEI_CORRECTION]:\n{correction}\n\n"
                        
                        found_images, system_note = get_content_images(correction, offset=0)
                
                        temp_images_list = content_images_list + found_images
                        
                        refined_content = full_history + system_note
                        refined_content += "\n\nDavid, you didn't understand it precisely. Please read my correction and tell me again if you understand more."
                        refined_prompt = [
                            {'role': 'system', 'content': SYSTEM_PROMPT},
                            {'role': 'user', 'content': refined_content, 'images': temp_images_list}
                        ]           
                
                        refined_stream = ollama.chat(model=MODEL_NAME, messages=refined_prompt, stream=True, options=options_config)
                
                        print("\nDavid is refining his thought...\n", flush=True)
                        refined_analysis = ""
                
                        for chunk in refined_stream:                                             # พิมพ์ Reasoning ออกมาทีละชิ้นทันที
                            if 'message' in chunk and 'content' in chunk['message']:
                                content = chunk['message']['content']
                                print(content, end="", flush=True)
                                refined_analysis += content
                        print("\n" + "-"*30)
                        
                        if chunk:
                            input_tokens = chunk.get('prompt_eval_count', 0)
                            total_input_tokens += input_tokens
                            output_tokens = chunk.get('eval_count', 0)
                            total_output_tokens += output_tokens
                            total_session_tokens = input_tokens + output_tokens
                            print(f"[SESSION TOKEN USAGE] Input: {input_tokens} | Output: {output_tokens} | Total: {total_session_tokens}\n")

                            update_token_history(total_input_tokens, total_output_tokens)
                        else:
                            print(f"[SESSION TOKEN USAGE ERROR]: Problem with Ollama.\n")
                        
                        if not refined_analysis.strip():
                            print("\nError: Empty Response\n")
                            continue
                        
                        content_images_list = list(dict.fromkeys(content_images_list + found_images))
                        print(f"\nTotal {len(content_images_list)} image(s) realized by David.")
                        print(f"Image: {[os.path.splitext(os.path.basename(path))[0] for path in content_images_list]}\n")
                        
                        full_history += f"[DAVID_REFINEMENT]:\n{refined_analysis}\n\n"
                        continue
                    
                    if feedback == "quit":
                        print("\nExit while not finish session.")
                        print(f"[TOTAL TOKEN USAGE] Input: {total_input_tokens} | Output: {total_output_tokens} | Total: {total_input_tokens+total_output_tokens}\n")
                
                        total_input_tokens = 0
                        total_output_tokens = 0
                        
                        content_images_list.clear()
                        break
                continue
            
            # --- โหมดคำถามเพื่อทดสอบ ---
            experience_content = get_experience_content(vectorstore, user_input, 10)
            if experience_content.strip():
                print("Experience contents was found.\n")      # print(f"\n[EXPERIENCE_FOUND]:\n{experience_content}\n")
            else:
                print("Experience contents could not be found.\n")
            
            print("David is thinking...\n", end=" ", flush=True)
            
            found_images, system_note = get_content_images(user_input, offset=0)
            
            combined_content = f"[EXPERIENCE_FROM_MEMORY]:\n{experience_content}\n\n"
            combined_content += system_note
            combined_content += f"\n\n[QUESTION]:\n{user_input}."
            initial_question_prompt = [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': combined_content, 'images': found_images}
            ]
            question_stream = ollama.chat(model=MODEL_NAME, messages=initial_question_prompt, stream=True, options=options_config)
            
            print("[DAVID_FIRST_ANALYSIS]:\n", end="", flush=True)
            first_analysis = ""
        
            for chunk in question_stream:                                             # พิมพ์ Reasoning ออกมาทีละชิ้นทันที
                if 'message' in chunk and 'content' in chunk['message']:
                    content = chunk['message']['content']
                    print(content, end="", flush=True)
                    first_analysis += content
            print("\n" + "-"*30)
            
            if chunk:
                input_tokens = chunk.get('prompt_eval_count', 0)
                total_input_tokens += input_tokens
                output_tokens = chunk.get('eval_count', 0)
                total_output_tokens += output_tokens
                total_session_tokens = input_tokens + output_tokens
                print(f"[SESSION TOKEN USAGE] Input: {input_tokens} | Output: {output_tokens} | Total: {total_session_tokens}\n")
                
                update_token_history(total_input_tokens, total_output_tokens)
            else:
                print(f"[SESSION TOKEN USAGE ERROR]: Problem with Ollama.\n")
            
            if not first_analysis.strip():
                print("\nError: Empty Response\n")
                continue
            
            content_images_list = list(dict.fromkeys(content_images_list + found_images))
            print(f"\n{len(found_images)} image(s) realized by David.")
            print(f"Image: {[os.path.splitext(os.path.basename(path))[0] for path in content_images_list]}\n")
            
            full_history = f"[QUESTION]:\n{user_input}\n\n"
            full_history += f"[DAVID_FIRST_ANALYSIS]:\n{first_analysis}\n\n"

            # Loop โต้ตอบ จนกว่า Wei จะบอกว่า "ถูกต้อง"
            while True:
                feedback = input("Is it correct (y/n/quit)? > ").strip()
            
                if feedback == "y":
                    full_history += "\n[FINAL_CONSENSUS]: Verified as correct by Wei."                       
                    current_time_string = datetime.now().strftime("%Y%m%d_%H%M%S")    # ใช้ชื่อเวลาที่ให้ Knowledge เป็นชื่อความรู้
                    save_to_brain(current_time_string, full_history, vectorstore)
                    
                    print("\nCorrected answers were recorded successfully.")
                    print(f"[TOTAL TOKEN USAGE] Input: {total_input_tokens} | Output: {total_output_tokens} | Total: {total_input_tokens+total_output_tokens}\n")
                
                    total_input_tokens = 0
                    total_output_tokens = 0
                    
                    content_images_list.clear()              
                    break
                
                if feedback == "n":
                    correction = input("Wei, please explain again: ").strip()
                    full_history += f"[WEI_CORRECTION]:\n{correction}\n\n"
                    
                    found_images, system_note = get_content_images(correction, offset=0)
                
                    temp_images_list = content_images_list + found_images
                    
                    refined_content = full_history + system_note
                    refined_content += "\n\nDavid, you didn't understand it precisely. Please read my correction and tell me again if you understand more."
                    refined_prompt = [
                        {'role': 'system', 'content': SYSTEM_PROMPT},
                        {'role': 'user', 'content': refined_content, 'images': temp_images_list}
                    ]           
                    
                    refined_stream = ollama.chat(model=MODEL_NAME, messages=refined_prompt, stream=True, options=options_config)
                    
                    print("\nDavid is refining his thought...\n", flush=True)
                    refined_analysis = ""
                    
                    for chunk in refined_stream:                                             # พิมพ์ Reasoning ออกมาทีละชิ้นทันที
                        if 'message' in chunk and 'content' in chunk['message']:
                            content = chunk['message']['content']
                            print(content, end="", flush=True)
                            refined_analysis += content
                    print("\n" + "-"*30)
                    
                    if chunk:
                        input_tokens = chunk.get('prompt_eval_count', 0)
                        total_input_tokens += input_tokens
                        output_tokens = chunk.get('eval_count', 0)
                        total_output_tokens += output_tokens
                        total_session_tokens = input_tokens + output_tokens
                        print(f"[SESSION TOKEN USAGE] Input: {input_tokens} | Output: {output_tokens} | Total: {total_session_tokens}\n")
                        
                        update_token_history(total_input_tokens, total_output_tokens)
                    else:
                        print(f"[SESSION TOKEN USAGE ERROR]: Problem with Ollama.\n")
                    
                    content_images_list = list(dict.fromkeys(content_images_list + found_images))
                    print(f"\nTotal {len(content_images_list)} image(s) realized by David.")
                    print(f"Image: {[os.path.splitext(os.path.basename(path))[0] for path in content_images_list]}\n")
                    
                    full_history += f"[DAVID_REFINEMENT]:\n{refined_analysis}\n\n"
                    continue
                
                if feedback == "quit":
                    print("\nExit while not finish session.")
                    print(f"[TOTAL TOKEN USAGE] Input: {total_input_tokens} | Output: {total_output_tokens} | Total: {total_input_tokens+total_output_tokens}\n")
                
                    total_input_tokens = 0
                    total_output_tokens = 0
                    
                    content_images_list.clear()
                    break
                
        except Exception as e:
            print(f"Error: {e}")


def create_brain():    
    embeddings = OllamaEmbeddings(model="nomic-embed-text")
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


# ฟังก์ชั่นค้นหาชื่อภาพในข้อความ text_to_be_searched และคืนค่า List ของ Path รูปภาพที่พบ
def get_content_images(text_to_be_searched, offset=0):
    found_images = []       # จุดรวมชื่อภาพที่หาเจอพร้อมตำแหน่ง Directory ที่ค้นหาจาก text_to_be_searched
    system_note = ""        # ข้อความสำหรับสร้าง System Note เพื่อบอก David เกี่ยวกับลำดับภาพ
    image_count = len(content_images_list) + offset

    #  ค้นหาชื่อภาพ Symbol_YYMMDD_Day ในวงเล็บเหลี่ยม [] ที่ขึ้นต้นด้วย "image:
    #  \s* คือ User จะพิมพ์โดยมีช่องว่างหรือไม่มีช่องว่างก็ได้
    #  (.*?) ให้ return ค่าค้นหา โดยเริ่มนับคำหลังจาก "[image:" และให้จบคำนั้นหลังจากเจอ "]"

    tags = re.findall(r"\[image:\s*(.*?)\]", text_to_be_searched)                                                    #
    if not tags:
        return [], ""

    # รายชื่อไฟล์ภาพทั้งหมดในโฟลเดอร์ chart
    all_files = os.listdir(CHART_PATH)
    
    for tag in tags:
        found_file = ""
        # ค้นหาไฟล์ที่มีชื่อตรงกับ Tag (รองรับ .png, .jpg, .jpeg)
        for f in all_files:
            if os.path.splitext(f)[0].lower() == tag.lower() and f.lower().endswith(('.png', '.jpg', '.jpeg')):
                found_file = f
                break
        
        if found_file != "":
            full_path = os.path.join(CHART_PATH, found_file)
            found_images.append(full_path)           
            image_count += 1
            system_note += f" (System Note: Image {image_count} is {tag})"
    
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


def save_to_brain(unit_name, content, vectorstore):
    # พยายามดึงวันที่จาก unit_name (คาดหวังรูปแบบ YYMMDD)
    date_str_match = re.search(r'\d{6}', unit_name)
    readable_date = None
    
    if date_str_match:
        date_str = date_str_match.group()
        try:
            # แปลง YYMMDD เป็น Object วันที่ แล้วฟอร์แมตเป็น เช่น 20 March 2026
            date_obj = datetime.strptime(date_str, "%y%m%d")
            readable_date = date_obj.strftime("%d %B %Y")
        except ValueError:
            readable_date = None
    
    # สร้าง Header ใหม่ที่ระบุทั้งชื่อไฟล์และวันที่
    # ผลลัพธ์จะเป็นเช่น [REFERENCE]: Emini_260320_Friday, 20 March 2026
    header = f"[REFERENCE]: {unit_name}"
    if readable_date:
        header += f", {readable_date}"
    
    tagged_content = header + "\n" + content
    
    # 1. บันทึกข้อมูลเป็น Text File ลงโฟลเดอร์ journal
    os.makedirs(JOURNAL_PATH, exist_ok=True)
    file_path = os.path.join(JOURNAL_PATH, f"{unit_name}.txt")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(tagged_content)
    
    # 2. หั่นเนื้อหา (Split) ก่อนบันทึกลง FAISS เพื่อป้องกัน Error 400
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
    
    # 3. สร้าง Document ชั่วคราวขึ้นมาด้วย Tag 'EXPERIENCE' และหั่นออกมาเป็นชิ้นเล็กๆ (Chunks)
    temp_doc = Document(page_content=tagged_content, metadata={"category": "EXPERIENCE", "source": unit_name})
    docs = text_splitter.split_documents([temp_doc])
    
    # 4. บันทึกลง FAISS 
    vectorstore.add_documents(docs)
    vectorstore.save_local(BRAIN_PATH)

def update_token_history(new_input, new_output):
    total_tokens_file = os.path.join(BASE_DIR, "total_tokens.txt")
    month_label = datetime.now().strftime("[%B %Y]")  # เช่น [April 2026]
    
    # 1. อ่านข้อมูลเดิมจากไฟล์ (ถ้ามี)
    lines = []
    if os.path.exists(total_tokens_file):
        with open(token_file, "r", encoding="utf-8") as f:
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
    with open(token_file, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


if __name__ == "__main__":
    main()