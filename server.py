from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import queue
import asyncio
import uuid
import json
import re
import ast

app = FastAPI(title="Qwen God-Mode Bridge", version="4.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- CONFIGURATION ---
API_KEY = "sk-qwen-bridge-key"

MODEL_MAP = {
    # Format avec préfixe (ton format original)
    "qwen/qwen3.7-plus": "Qwen3.7-Plus",
    "qwen/qwen3.7-max": "Qwen3.7-Max",
    "qwen/qwen3.6-plus": "Qwen3.6-Plus",
    "qwen/generic": "Qwen3.7-Plus",
    
    # Format sans préfixe (ce que Qwen Code envoie probablement)
    "qwen3.7-plus": "Qwen3.7-Plus",
    "qwen3.7-max": "Qwen3.7-Max",
    "qwen3.6-plus": "Qwen3.6-Plus",
    
    # Alias alternatifs courants
    "qwen-plus": "Qwen3.7-Plus",
    "qwen-max": "Qwen3.7-Max",
    "qwen3-plus": "Qwen3.7-Plus",
    "qwen3-max": "Qwen3.7-Max",
    
    # Fallback générique
    "default": "Qwen3.7-Plus",
    "generic": "Qwen3.7-Plus"
}

# --- STATE MANAGEMENT ---
command_queue = queue.Queue()          
response_queue: asyncio.Queue | None = None  
response_lock = asyncio.Lock()         
is_processing = False
current_ui_model = "Qwen3.7-Plus"

# ==========================================
# 🧠 UNIVERSAL TOOL ABSTRACTION LAYER
# ==========================================
TOOL_SYSTEM_PROMPT = """
You are a precise tool-calling engine. 

When you need to use a tool, you MUST respond with one or more <tool_call> blocks.
Do NOT write any text before or after the <tool_call> blocks when calling tools.

Format of a tool call:
<tool_call>
{
  "name": "tool_name",
  "arguments": {
    "param1": "value1",
    "param2": 42
  }
}
</tool_call>

Strict Rules:
1. "name" must exactly match one of the available tools.
2. "arguments" must be a valid JSON object matching the tool's schema.
3. NEVER escape the "arguments" object into a string. Keep it as a raw JSON object.
4. NEVER invent "id" or "type" fields. The system adds them automatically.
5. If multiple independent tools are needed, output multiple <tool_call> blocks.
6. If no tool is needed, respond with normal text.
""".strip()

def _parse_json_robust(s: str):
    s = s.strip()
    if not s: return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(s)
            if isinstance(parsed, dict): return parsed
        except Exception:
            pass
    return None

def _process_candidate(candidate, allowed_tools, result_list, seen_names):
    name = str(candidate.get("name") or candidate.get("tool") or "").strip()
    args = candidate.get("arguments") or candidate.get("args") or candidate.get("parameters") or {}
    
    if not name or (allowed_tools and name not in allowed_tools) or name in seen_names:
        return
        
    try:
        if isinstance(args, str):
            parsed_args = _parse_json_robust(args)
            args_str = json.dumps(parsed_args if parsed_args else {"raw": args}, ensure_ascii=False)
        else:
            args_str = json.dumps(args, ensure_ascii=False)
    except Exception:
        args_str = "{}"
        
    result_list.append({
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {"name": name, "arguments": args_str}
    })
    seen_names.add(name)

def convert_llm_output_to_openai_tool_calls(text: str, allowed_tools: list = None):
    openai_tool_calls = []
    seen_names = set()
    
    # 1. Extraction via balises XML (le plus fiable)
    for pattern in [r"<tool_call>\s*(.*?)\s*</tool_call>", r"<tool_use>\s*(.*?)\s*</tool_use>"]:
        for match in re.finditer(pattern, text, re.DOTALL | re.IGNORECASE):
            parsed = _parse_json_robust(match.group(1))
            if parsed:
                _process_candidate(parsed, allowed_tools, openai_tool_calls, seen_names)
                
    # 2. Fallback : JSON brut dans le texte
    decoder = json.JSONDecoder()
    i = 0
    while i < len(text):
        start = text.find("{", i)
        if start == -1: break
        try:
            obj, end = decoder.raw_decode(text[start:])
            if isinstance(obj, dict) and ("name" in obj or "tool" in obj):
                _process_candidate(obj, allowed_tools, openai_tool_calls, seen_names)
            i = start + end
        except json.JSONDecodeError:
            i = start + 1
            
    return openai_tool_calls

def clean_text_from_tool_calls(text: str) -> str:
    text = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<tool_use>.*?</tool_use>", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()

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
    tools = data.get("tools", [])
    
    has_tools = bool(tools)
    allowed_tool_names = [t["function"]["name"] for t in tools] if has_tools else []

    # Injection du prompt outil si nécessaire
    if has_tools:
        tool_prompt = TOOL_SYSTEM_PROMPT + "\n\nAvailable tools:\n"
        for t in tools:
            fn = t.get("function", {})
            tool_prompt += f"- {fn.get('name')}: {fn.get('description')}\n"
            if fn.get('parameters'):
                tool_prompt += f"  Parameters: {json.dumps(fn.get('parameters'))}\n"
        
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] += "\n\n" + tool_prompt
        else:
            messages.insert(0, {"role": "system", "content": tool_prompt.strip()})

    prompt = "\n".join([f"{m['role']}: {m['content']}" for m in messages])
    target_ui_model = MODEL_MAP.get(requested_model, "Qwen3.7-Plus")
    task_id = str(uuid.uuid4())

    async with response_lock:
        if is_processing:
            raise HTTPException(status_code=429, detail="Bridge is busy.")
        is_processing = True
        response_queue = asyncio.Queue()

    # Switch UI model if needed
    if target_ui_model != current_ui_model:
        print(f"\n🔄 [API] Switching UI model to: {target_ui_model}...", flush=True)
        current_ui_model = target_ui_model
        command_queue.put({"action": "switch_model", "model": target_ui_model})
        await asyncio.sleep(2.0)
        
    command_queue.put({"action": "send_prompt", "prompt": prompt})
    print(f"\n📡 [API] Forwarding to browser (Model: {target_ui_model}, Tools: {len(tools)})...", flush=True)

    # Collecte de la réponse (nécessaire pour parser les tool calls de manière fiable)
    full_response = ""
    try:
        while True:
            token = await asyncio.wait_for(response_queue.get(), timeout=120.0)
            if token == "[DONE]":
                break
            full_response += token
    except asyncio.TimeoutError:
        print("⚠️ [API] Timeout 120s", flush=True)
    finally:
        async with response_lock:
            is_processing = False
            response_queue = None

    # Traitement des Tool Calls
    if has_tools:
        openai_tool_calls = convert_llm_output_to_openai_tool_calls(full_response, allowed_tools=allowed_tool_names)
        
        if openai_tool_calls:
            print(f"✅ [API] {len(openai_tool_calls)} tool call(s) detected and converted.", flush=True)
            
            if stream:
                async def tool_call_stream_generator():
                    # Chunk rôle
                    yield f"data: {json.dumps({'id': f'chatcmpl-{task_id}', 'object': 'chat.completion.chunk', 'created': 1677652288, 'model': requested_model, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n"
                    # Chunks tool calls
                    for i, tc in enumerate(openai_tool_calls):
                        yield f"data: {json.dumps({'id': f'chatcmpl-{task_id}', 'object': 'chat.completion.chunk', 'created': 1677652288, 'model': requested_model, 'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': i, 'id': tc['id'], 'type': 'function', 'function': {'name': tc['function']['name'], 'arguments': tc['function']['arguments']}}]}, 'finish_reason': None}]})}\n\n"
                    # Chunk final
                    yield f"data: {json.dumps({'id': f'chatcmpl-{task_id}', 'object': 'chat.completion.chunk', 'created': 1677652288, 'model': requested_model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]})}\n\n"
                    yield "data: [DONE]\n\n"
                return StreamingResponse(tool_call_stream_generator(), media_type="text/event-stream")
            else:
                return {
                    "id": f"chatcmpl-{task_id}", "object": "chat.completion", "created": 1677652288, "model": requested_model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": openai_tool_calls}, "finish_reason": "tool_calls"}]
                }

    # Réponse texte normale (nettoyée des artefacts tool call si le LLM a halluciné)
    clean_text = clean_text_from_tool_calls(full_response)
    
    if stream:
        async def event_generator():
            try:
                # On simule un stream à partir du texte complet collecté
                yield f"data: {json.dumps({'id': f'chatcmpl-{task_id}', 'object': 'chat.completion.chunk', 'created': 1677652288, 'model': requested_model, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]})}\n\n"
                yield f"data: {json.dumps({'id': f'chatcmpl-{task_id}', 'object': 'chat.completion.chunk', 'created': 1677652288, 'model': requested_model, 'choices': [{'index': 0, 'delta': {'content': clean_text}, 'finish_reason': None}]})}\n\n"
                yield f"data: {json.dumps({'id': f'chatcmpl-{task_id}', 'object': 'chat.completion.chunk', 'created': 1677652288, 'model': requested_model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                async with response_lock:
                    is_processing = False
                    response_queue = None
        return StreamingResponse(event_generator(), media_type="text/event-stream")
    
    else:
        return {
            "id": f"chatcmpl-{task_id}", "object": "chat.completion", "created": 1677652288, "model": requested_model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": clean_text}, "finish_reason": "stop"}]
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
            
    elif data.get("type") == "error":
        print(f"\n\n❌ [API] BROWSER ERROR: {data.get('content')}", flush=True)
        if response_queue is not None:
            await response_queue.put("[DONE]") # Force la fin pour débloquer le client
    
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
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
