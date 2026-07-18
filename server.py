from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import queue
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
    
    # Switch UI model if needed
    if target_ui_model != current_ui_model:
        print(f"\n🔄 [API] Switching UI model to: {target_ui_model}... (Freezing)", flush=True)
        current_ui_model = target_ui_model
        command_queue.put({"action": "switch_model", "model": target_ui_model})
        await asyncio.sleep(2.0)  # Wait for browser to apply the switch
        print(f"✅ [API] Model switched. Forwarding prompt...", flush=True)
        
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
        if response_queue is not None:
            await response_queue.put(token)
            
    elif data.get("type") == "done":
        print("\n\n" + "="*60, flush=True)
        print("✅ QWEN FINISHED!", flush=True)
        print("="*60 + "\n", flush=True)
        if response_queue is not None:
            await response_queue.put("[DONE]")
    
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
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
