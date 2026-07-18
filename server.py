from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import threading
import queue
import logging
import asyncio
import uuid
import json

app = FastAPI(title="Qwen God-Mode Bridge", version="3.3")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- CONFIGURATION ---
API_KEY = "sk-qwen-bridge-key"

MODEL_MAP = {
    "qwen/qwen3.7-plus": "Qwen3.7-Plus",
    "qwen/qwen3.7-max": "Qwen3.7-Max",
    "qwen/qwen3.6-plus": "Qwen3.6-Plus",
    "qwen/generic": "Qwen3.7-Plus"
}

# --- STATE MANAGEMENT ---
command_queue = queue.Queue()          
response_queue: asyncio.Queue | None = None  
response_lock = asyncio.Lock()         
is_processing = False
current_ui_model = "Qwen3.7-Plus"

# 🛡️ THE MAGIC FIX: A thread-safe event to block the CLI until generation is done
generation_done = threading.Event()
generation_done.set() # Start in "done" state so the first prompt can be entered

# --- CLI INPUT THREAD ---
def cli_input_thread():
    global current_ui_model
    while True:
        try:
            # 🛑 BLOCK HERE: Wait until the previous LLM response is 100% finished
            generation_done.wait()
            
            # Now it's safe to print the prompt
            print("🤖 [YOU] >>> ", end="", flush=True)
            user_input = input("").strip()
            
            if user_input.lower() == 'exit':
                break
                
            if user_input.lower().startswith("/model "):
                requested_model = user_input.split(" ", 1)[1].strip().lower()
                target_ui_model = MODEL_MAP.get(requested_model)
                
                if target_ui_model:
                    if target_ui_model != current_ui_model:
                        current_ui_model = target_ui_model
                        command_queue.put({"action": "switch_model", "model": target_ui_model})
                        print(f"\n🔄 Switching UI to: {target_ui_model}...", flush=True)
                    else:
                        print(f"\nℹ️ UI is already set to {target_ui_model}.", flush=True)
                else:
                    print(f"\n❌ Unknown model. Available: {', '.join(MODEL_MAP.keys())}", flush=True)
                continue

            if user_input:
                # 🛑 Mark that we are starting a new generation, so the next loop will block
                generation_done.clear()
                command_queue.put({"action": "send_prompt", "prompt": user_input})
                
        except EOFError:
            break

# Print the main banner BEFORE starting the thread to prevent uvicorn startup text overlap
print("\n" + "="*60, flush=True)
print("🟢 BRIDGE ACTIVE.", flush=True)
print(f"🔑 API Key: {API_KEY}", flush=True)
print("💡 Commands: /model <name> | Type text to prompt.", flush=True)
print("="*60 + "\n", flush=True)

# Start the CLI thread
threading.Thread(target=cli_input_thread, daemon=True).start()

# ==========================================
# 📋 OPENAI MODEL DISCOVERY ENDPOINT
# ==========================================
@app.get("/v1/models")
async def list_models():
    models = [{"id": api_name, "object": "model", "created": 1677652288, "owned_by": "qwen-bridge"} for api_name in MODEL_MAP.keys()]
    return {"object": "list", "data": models}

# ==========================================
# 🌟 OPENAI CHAT COMPLETIONS ENDPOINT
# ==========================================
@app.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: str = Header(None)):
    global is_processing, response_queue, current_ui_model
    
    if authorization != f"Bearer {API_KEY}":
        raise HTTPException(status_code=401, detail="Invalid API Key")
        
    data = await request.json()
    messages = data.get("messages", [])
    stream = data.get("stream", False)
    requested_model = data.get("model", "qwen/generic")
    
    target_ui_model = MODEL_MAP.get(requested_model, "Qwen3.7-Plus")
    prompt = "\n".join([f"{m['role']}: {m['content']}" for m in messages])

    async with response_lock:
        if is_processing:
            raise HTTPException(status_code=429, detail="Bridge is busy.")
        is_processing = True
        response_queue = asyncio.Queue()

    task_id = str(uuid.uuid4())
    
    # 🧊 FREEZE API: Wait for UI to physically switch models before proceeding
    if target_ui_model != current_ui_model:
        print(f"\n🔄 [API] Switching UI model to: {target_ui_model}... (Freezing)", flush=True)
        current_ui_model = target_ui_model
        command_queue.put({"action": "switch_model", "model": target_ui_model})
        await asyncio.sleep(2.0) # Wait for browser to click and settle
        print(f"✅ [API] Model switched. Forwarding prompt...", flush=True)
        
    # Also block the CLI thread if an API call is made
    generation_done.clear()
    command_queue.put({"action": "send_prompt", "prompt": prompt})
    print(f"\n📡 [API] Forwarding to browser (Model: {target_ui_model})...", flush=True)

    if stream:
        async def event_generator():
            global is_processing, response_queue
            try:
                while True:
                    token = await response_queue.get()
                    if token == "[DONE]":
                        yield "data: [DONE]\n\n"
                        break
                    chunk = {
                        "id": f"chatcmpl-{task_id}", "object": "chat.completion.chunk",
                        "created": 1677652288, "model": requested_model,
                        "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}]
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
            finally:
                async with response_lock:
                    is_processing = False
                    response_queue = None
        return StreamingResponse(event_generator(), media_type="text/event-stream")
    
    else:
        full_response = ""
        try:
            while True:
                token = await response_queue.get()
                if token == "[DONE]": break
                full_response += token
        finally:
            async with response_lock:
                is_processing = False
                response_queue = None
        
        return {
            "id": f"chatcmpl-{task_id}", "object": "chat.completion",
            "created": 1677652288, "model": requested_model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": full_response}, "finish_reason": "stop"}]
        }

# ==========================================
# 📥 BROWSER STREAM RECEIVER
# ==========================================
@app.post("/qwen-stream")
async def receive_stream(request: Request):
    global response_queue
    data = await request.json()
    
    if data.get("type") == "stream":
        token = data["content"]
        print(token, end="", flush=True)
        if response_queue is not None: await response_queue.put(token)
            
    elif data.get("type") == "done":
        print("\n\n" + "="*60, flush=True)
        print("✅ QWEN FINISHED!", flush=True)
        print("="*60 + "\n", flush=True)
        if response_queue is not None: await response_queue.put("[DONE]")
        
        # 🚀 UNBLOCK THE CLI THREAD: Generation is officially done!
        generation_done.set()
    
    return {"status": "ok"}

# ==========================================
# 📤 BROWSER COMMAND POLLER
# ==========================================
@app.get("/pending-command")
async def get_command():
    try:
        cmd = command_queue.get_nowait()
        return cmd
    except queue.Empty:
        return {"action": None}

if __name__ == "__main__":
    import uvicorn
    logging.getLogger("uvicorn.access").disabled = True
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
