import re
import os
import ollama
import threading
import tkinter as tk
from tkinter import scrolledtext
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
    'temperature': 0.1,
    'num_ctx': 32768,
    'num_predict': 4096,
    'repeat_penalty': 1.2,
    'top_k': 20,
    'top_p': 0.5,
    'num_thread': 8
}

# =============================================================================
#  ReasoningWindow — Architecture ที่ถูกต้อง
#
#  ปัญหาของ code เดิม (ทั้ง 2 version):
#    Version 1: สร้าง tk.Tk() ใน daemon thread → tkinter ต้องการ Main Thread เท่านั้น
#    Version 2: ใช้ root.update() ใน streaming loop → input() ที่อยู่ก่อน/หลัง
#               loop block Main Thread ทำให้ไม่มีโอกาส render หน้าต่าง
#
#  วิธีแก้ที่ถูกต้อง:
#    - Main Thread  → รัน tkinter mainloop() อย่างเดียว ไม่มีงานอื่น
#    - Worker Thread → รัน input(), streaming, และ logic ทั้งหมด
#    - root.after() → วิธีเดียวที่ thread-safe สำหรับสั่ง tkinter จาก Worker Thread
#    - threading.Event → sync รอให้หน้าต่างสร้างเสร็จก่อนดำเนินการต่อ
# =============================================================================

class ReasoningWindow:
    def __init__(self, root, title="David's Internal Monologue"):
        """
        root  : tk.Tk() instance ที่รันอยู่บน Main Thread
        title : ชื่อหัวหน้าต่าง
        """
        self.root = root
        self.title = title
        self.win = None
        self.text_area = None
        self._ready_event = threading.Event()  # ใช้รอให้ Main Thread สร้างหน้าต่างเสร็จ

    # ------------------------------------------------------------------
    # เรียกจาก Worker Thread — schedule การสร้างหน้าต่างบน Main Thread
    # ------------------------------------------------------------------
    def open(self):
        self._ready_event.clear()
        self.root.after(0, self._create_window)   # ส่งงานไปให้ Main Thread
        self._ready_event.wait()                   # Worker Thread รอจนหน้าต่างพร้อม

    # ------------------------------------------------------------------
    # รันบน Main Thread (ถูกเรียกโดย root.after)
    # ------------------------------------------------------------------
    def _create_window(self):
        self.win = tk.Toplevel(self.root)          # Toplevel = ลูกของ root ที่ซ่อนไว้
        self.win.title(self.title)
        self.win.geometry("600x400")
        self.win.attributes("-topmost", True)

        self.text_area = scrolledtext.ScrolledText(
            self.win, wrap=tk.WORD, font=("Consolas", 10)
        )
        self.text_area.pack(expand=True, fill='both')
        self.text_area.insert(tk.INSERT, "David is thinking...\n\n")
        self.text_area.configure(state='disabled')
        self.win.protocol("WM_DELETE_WINDOW", self._on_closing)

        self._ready_event.set()  # แจ้ง Worker Thread ว่าหน้าต่างพร้อมแล้ว

    # ------------------------------------------------------------------
    # เรียกจาก Worker Thread — thread-safe ผ่าน root.after
    # ------------------------------------------------------------------
    def append_text(self, msg):
        if self.win:
            self.root.after(0, lambda m=msg: self._insert_text(m))

    # ------------------------------------------------------------------
    # รันบน Main Thread (ถูกเรียกโดย root.after)
    # ------------------------------------------------------------------
    def _insert_text(self, msg):
        if self.win and self.text_area:
            self.text_area.configure(state='normal')
            self.text_area.insert(tk.END, msg)
            self.text_area.see(tk.END)
            self.text_area.configure(state='disabled')

    def _on_closing(self):
        if self.win:
            self.win.destroy()
            self.win = None


# =============================================================================
#  Global State
# =============================================================================
total_images_list: list[str] = []
total_input_tokens = 0
total_output_tokens = 0

tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-7B-Instruct")

for p in [BOOK_PATH, CHART_PATH, LESSON_PATH, JOURNAL_PATH, BRAIN_PATH]:
    os.makedirs(p, exist_ok=True)


# =============================================================================
#  main_worker — Logic ทั้งหมดย้ายมาอยู่ที่นี่ รันบน Worker Thread
#  (เดิมคือ def main() ที่รันบน Main Thread)
# =============================================================================
def main_worker(root):
    global total_images_list, total_input_tokens, total_output_tokens

    embeddings = OllamaEmbeddings(model="nomic-embed-text")

    if os.path.exists(os.path.join(BRAIN_PATH, "index.faiss")):
        vectorstore = FAISS.load_local(BRAIN_PATH, embeddings, allow_dangerous_deserialization=True)
    else:
        vectorstore = create_brain()

    while True:
        try:
            full_history = ""
            mode_text = ""
            found_images = []
            system_note = ""
            response_text = ""
            initial_analysis = ""

            print("--- David is online ---  **Asking questions by using '?'**\n(Type exit/quit/bye to terminate)")
            user_input = input("Wei: ").strip()   # input() บน Worker Thread — ไม่ block Main Thread

            if user_input.lower() in ['exit', 'quit', 'bye']:
                root.after(0, root.quit)           # สั่งปิด mainloop บน Main Thread
                break

            if "?" in user_input:
                full_history = f"[QUESTION_FROM_WEI]:\n{user_input}\n\n"
                mode_text = "David is thinking...\n"
                response_text += "[DAVID_RESPONSE]:\n"
            else:
                full_history = f"[KNOWLEDGE_FROM_WEI]:\n{user_input}\n\nDavid, acknowledge this knowledge which is derived from Wei's experiences and explain briefly if you understand it.\n\n"
                mode_text = "David is absorbing your knowledge...\n"
                response_text += "[DAVID_ACKNOWLEDGEMENT]:\n"

            found_images, system_note = get_content_images(user_input)
            if found_images:
                full_history += system_note

            experience_content = get_experience_content(vectorstore, user_input, 10)
            if experience_content.strip():
                print("\nExperience contents was found.\n")
            else:
                print("\nExperience contents could not be found.\n")

            initial_content = f"[EXPERIENCE_FROM_MEMORY]:\n{experience_content}\n\n" + full_history
            initial_prompt = [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': initial_content, 'images': found_images}
            ]

            text_tokens, image_tokens = get_token_count(initial_prompt)
            print(f"[TOKEN ESTIMATION]: Text input = {text_tokens} | Image input = {image_tokens} | Total input = {text_tokens + image_tokens}\n")

            chunk = None
            print(mode_text)

            # --- สร้างหน้าต่าง Pop-up (Worker Thread สั่ง → Main Thread สร้าง) ---
            reasoning_gui = ReasoningWindow(root, title="David's Initial Analysis")
            reasoning_gui.open()  # บล็อก Worker Thread จนกว่าหน้าต่างจะพร้อม

            stream = ollama.chat(model=MODEL_NAME, messages=initial_prompt, stream=True, options=options_config)

            is_first_chunk = True
            for chunk in stream:
                if 'message' in chunk and 'content' in chunk['message']:
                    content = chunk['message']['content']
                    reasoning_gui.append_text(content)  # thread-safe ผ่าน root.after()

                    if is_first_chunk:
                        initial_analysis += response_text + content
                        is_first_chunk = False
                    else:
                        initial_analysis += content

            print(initial_analysis)
            print("\n" + "-"*30)
            print("(Analysis finished. You can check/close the pop-up window.)")

            if chunk:
                input_tokens = chunk.get('prompt_eval_count', 0)
                output_tokens = chunk.get('eval_count', 0)
                total_input_tokens += input_tokens
                total_output_tokens += output_tokens
                print(f"\n[SESSION TOKEN USAGE] Input: {input_tokens} | Output: {output_tokens} | Total: {input_tokens + output_tokens}\n")
                update_token_history(input_tokens, output_tokens)

            if not initial_analysis.strip():
                print("\nError: Empty Response\n")
                continue

            total_images_list = list(dict.fromkeys(total_images_list + found_images))
            print(f"{len(total_images_list)} image(s) realized by David.\n")
            full_history += response_text + f"\n{initial_analysis}\n\n"

            while True:
                try:
                    next_found_images = []
                    next_system_note = ""
                    refinement_text = "[DAVID_REFINEMENT_ANALYSIS]:\n"
                    refined_analysis = ""

                    feedback = input("Do I understand correctly (y/n/quit)? > ").strip()

                    if feedback == "y":
                        full_history += "\n[FINAL_CONSENSUS]: Verified as correct by Wei."
                        current_time_string = datetime.now().strftime("%Y%m%d_%H%M%S")
                        save_to_brain(current_time_string, full_history, vectorstore)
                        print("\nKnowledge was memorized successfully.")
                        total_input_tokens = 0
                        total_output_tokens = 0
                        total_images_list.clear()
                        break

                    elif feedback == "n":
                        correction_input = input("Wei, please explain: ").strip()
                        full_history += f"[WEI_CORRECTION]:\n{correction_input}\n\n"
                        next_found_images, next_system_note = get_content_images(correction_input)

                        temp_images_list = list(dict.fromkeys(total_images_list + next_found_images))
                        if next_found_images:
                            full_history += next_system_note

                        refined_content = full_history + "David, you didn't understand it precisely. Read my correction and analyze it again.\n\n"
                        refined_prompt = [
                            {'role': 'system', 'content': SYSTEM_PROMPT},
                            {'role': 'user', 'content': refined_content, 'images': temp_images_list}
                        ]

                        text_tokens, image_tokens = get_token_count(refined_prompt)
                        print(f"\n[TOKEN ESTIMATION]: Text input = {text_tokens} | Image input = {image_tokens} | Total input = {text_tokens + image_tokens}\n")

                        # --- หน้าต่าง Refinement ---
                        refine_gui = ReasoningWindow(root, title="David's Refinement Thought")
                        refine_gui.open()

                        print("David is refining his thought (Check pop-up)...\n")
                        refined_stream = ollama.chat(model=MODEL_NAME, messages=refined_prompt, stream=True, options=options_config)

                        is_first_chunk = True
                        for chunk in refined_stream:
                            if 'message' in chunk and 'content' in chunk['message']:
                                content = chunk['message']['content']
                                refine_gui.append_text(content)  # thread-safe

                                if is_first_chunk:
                                    refined_analysis += refinement_text + content
                                    is_first_chunk = False
                                else:
                                    refined_analysis += content

                        print(refined_analysis)
                        print("\n" + "-"*30)

                        if chunk:
                            input_tokens = chunk.get('prompt_eval_count', 0)
                            output_tokens = chunk.get('eval_count', 0)
                            total_input_tokens += input_tokens
                            total_output_tokens += output_tokens
                            update_token_history(input_tokens, output_tokens)

                        total_images_list = list(dict.fromkeys(total_images_list + next_found_images))
                        full_history += refinement_text + f"{refined_analysis}\n\n"

                    elif feedback == "quit":
                        total_input_tokens = 0
                        total_output_tokens = 0
                        total_images_list.clear()
                        break

                except Exception as e:
                    print(f"Error: {e}")

        except Exception as e:
            print(f"Error: {e}")


# =============================================================================
#  Helper Functions (ไม่เปลี่ยนแปลง)
# =============================================================================

def save_to_brain(unit_name, content, vectorstore):
    date_list = re.findall(r'\d{6}', unit_name)
    transformed_name = unit_name
    for day in ["_Monday", "_Tuesday", "_Wednesday", "_Thursday", "_Friday", "_Saturday", "_Sunday"]:
        transformed_name = transformed_name.replace(day, "")
    for date_str in date_list:
        try:
            date_obj = datetime.strptime(date_str, "%y%m%d")
            formatted_date = date_obj.strftime("%A %d %B %Y")
            transformed_name = transformed_name.replace(date_str, formatted_date)
        except ValueError:
            continue
    header = f"[REFERENCE]: {transformed_name}"
    tagged_content = header + "\n" + content
    os.makedirs(JOURNAL_PATH, exist_ok=True)
    file_path = os.path.join(JOURNAL_PATH, f"{unit_name}.txt")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(tagged_content)
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
    temp_doc = Document(page_content=tagged_content, metadata={"category": "EXPERIENCE", "source": unit_name})
    docs = text_splitter.split_documents([temp_doc])
    vectorstore.add_documents(docs)
    vectorstore.save_local(BRAIN_PATH)

def get_content_images(text_to_be_searched):
    found_images = []
    system_note = "[System Note]:\n"
    image_count = len(total_images_list)
    tags = re.findall(r"\[image:\s*(.*?)\]", text_to_be_searched)
    if not tags: return [], ""
    try:
        all_files = os.listdir(CHART_PATH)
    except Exception: return [], ""
    for tag in tags:
        found_file = ""
        for f in all_files:
            if os.path.splitext(f)[0].lower() == tag.strip().lower() and f.lower().endswith(('.png', '.jpg', '.jpeg')):
                found_file = f
                break
        if found_file:
            full_path = os.path.join(CHART_PATH, found_file)
            found_images.append(full_path)
            image_count += 1
            transformed_name = os.path.splitext(found_file)[0]
            for day in ["_Monday", "_Tuesday", "_Wednesday", "_Thursday", "_Friday", "_Saturday", "_Sunday"]:
                transformed_name = transformed_name.replace(day, "")
            date_match = re.search(r'\d{6}', transformed_name)
            if date_match:
                date_str = date_match.group()
                try:
                    date_obj = datetime.strptime(date_str, "%y%m%d")
                    formatted_date = date_obj.strftime("%A %d %B %Y")
                    transformed_name = transformed_name.replace(date_str, formatted_date)
                except ValueError: pass
            system_note += f"IMAGE {image_count} is {transformed_name}\n\n"
    return found_images, system_note

def get_experience_content(vectorstore, query, k=10):
    if not vectorstore: return ""
    docs = vectorstore.similarity_search(query, k=k)
    content_list = []
    for doc in docs:
        category = doc.metadata.get('category', 'knowledge').upper()
        source = doc.metadata.get('source', 'unknown')
        content_list.append(f"[{category}] (Source: {source}):\n{doc.page_content}")
    return "\n---\n".join(content_list)

def get_token_count(messages):
    if not tokenizer: return 0, 0
    text_tokens = 0
    image_tokens = 0
    for m in messages:
        text_tokens += len(tokenizer.encode(m.get('content', "")))
        if 'images' in m:
            for img_path in m['images']:
                try:
                    with Image.open(img_path) as img:
                        w, h = img.size
                        patches = (h // 28) * (w // 28)
                        image_tokens += patches
                except: image_tokens += 1000
    return text_tokens, image_tokens

def update_token_history(new_input, new_output):
    total_tokens_file = os.path.join(BASE_DIR, "total_tokens.txt")
    month_label = datetime.now().strftime("[%B %Y]")
    lines = []
    if os.path.exists(total_tokens_file):
        with open(total_tokens_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
    found = False
    new_lines = []
    for line in lines:
        if line.startswith(month_label):
            match = re.search(r"Total Input Tokens = (\d+), Total Output Tokens = (\d+)", line)
            old_input = int(match.group(1)) if match else 0
            old_output = int(match.group(2)) if match else 0
            updated_input = old_input + new_input
            updated_output = old_output + new_output
            new_lines.append(f"{month_label}: Total Input Tokens = {updated_input}, Total Output Tokens = {updated_output}, Total Tokens = {updated_input + updated_output}\n")
            found = True
        else: new_lines.append(line)
    if not found:
        new_lines.append(f"{month_label}: Total Input Tokens = {new_input}, Total Output Tokens = {new_output}, Total Tokens = {new_input + new_output}\n")
    with open(total_tokens_file, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

def create_brain():
    embeddings = OllamaEmbeddings(model=EMBEDDING_MODEL)
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
    vectorstore = None
    if os.path.exists(BOOK_PATH):
        books = [f for f in os.listdir(BOOK_PATH) if f.endswith(".pdf")]
        if books:
            theory_docs = []
            for i, file in enumerate(books):
                loader = PyPDFLoader(os.path.join(BOOK_PATH, file))
                data = loader.load()
                for d in data: d.metadata["category"] = "THEORY"
                theory_docs.extend(data)
            splits = splitter.split_documents(theory_docs)
            vectorstore = FAISS.from_documents(splits, embeddings)
    if os.path.exists(JOURNAL_PATH) and vectorstore:
        journal = [f for f in os.listdir(JOURNAL_PATH) if f.endswith(".txt")]
        if journal:
            experience_docs = []
            for file in journal:
                loader = TextLoader(os.path.join(JOURNAL_PATH, file), encoding='utf-8')
                data = loader.load()
                for d in data: d.metadata["category"] = "EXPERIENCE"
                experience_docs.extend(data)
            if experience_docs:
                splits = splitter.split_documents(experience_docs)
                vectorstore.add_documents(splits)
    if vectorstore:
        vectorstore.save_local(BRAIN_PATH)
        return vectorstore
    return None


# =============================================================================
#  Entry Point — Architecture ใหม่
#
#  Main Thread  : รัน tk.mainloop() อย่างเดียว ไม่มีงานอื่น
#  Worker Thread: รัน main_worker() ซึ่งมี input(), streaming, logic ทั้งหมด
# =============================================================================
if __name__ == "__main__":
    root = tk.Tk()
    root.withdraw()   # ซ่อน root window (ใช้แค่เป็น parent ของ Toplevel windows)

    worker_thread = threading.Thread(
        target=main_worker,
        args=(root,),
        daemon=True
    )
    worker_thread.start()

    root.mainloop()   # Main Thread วนลูปรับ GUI events ตลอดเวลา — ไม่ถูก block โดย input()