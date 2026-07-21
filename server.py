from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import queue
import asyncio
import uuid
import json
import re
import ast

app = FastAPI(title="Qwen God-Mode Bridge", version="5.7")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

API_KEY = "sk-qwen-bridge-key"

MODEL_MAP = {
    "qwen/qwen3.7-plus": "Qwen3.7-Plus", "qwen/qwen3.7-max": "Qwen3.7-Max","qwen/generic": "Qwen3.7-Plus",
    "qwen/qwen3.8-max": "Qwen3.8-Max-Preview",
}

command_queue = queue.Queue()
response_queue: asyncio.Queue | None = None
response_lock = asyncio.Lock()
is_processing = False
current_ui_model = "Qwen3.7-Plus"

# ✅ NOUVEAU : état du mode raisonnement (assume Think au démarrage, ajuste si ton UI démarre en Fast)
current_reasoning_mode = "Think"

TOOL_SYSTEM_PROMPT = """
You are a precise tool-calling engine.
When you need to use a tool, you MUST respond with one or more <tool_call> blocks.
Format:  <tool_call>{"name": "tool_name", "arguments": {"param": "value"}}</tool_call>
""".strip()

def _parse_json_robust(s: str):
    s = s.strip()
    if not s: return None
    try: return json.loads(s)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(s)
            if isinstance(parsed, dict): return parsed
        except Exception: pass
    return None

def _process_candidate(candidate, allowed_tools, result_list, seen_names):
    name = str(candidate.get("name") or candidate.get("tool") or "").strip()
    args = candidate.get("arguments") or candidate.get("args") or {}
    if not name or (allowed_tools and name not in allowed_tools) or name in seen_names: return
    try:
        if isinstance(args, str):
            parsed_args = _parse_json_robust(args)
            args_str = json.dumps(parsed_args if parsed_args else {"raw": args}, ensure_ascii=False)
        else:
            args_str = json.dumps(args, ensure_ascii=False)
    except Exception:
        args_str = "{}"
    result_list.append({
        "index": len(result_list),
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {"name": name, "arguments": args_str}
    })
    seen_names.add(name)

def convert_llm_output_to_openai_tool_calls(text: str, allowed_tools: list = None):
    openai_tool_calls, seen_names = [], set()
    for pattern in [r"<tool_call>\s*(.*?)\s*</tool_call>", r"<tool_use>\s*(.*?)\s*</tool_use>"]:
        for match in re.finditer(pattern, text, re.DOTALL | re.IGNORECASE):
            parsed = _parse_json_robust(match.group(1))
            if parsed: _process_candidate(parsed, allowed_tools, openai_tool_calls, seen_names)
    return openai_tool_calls

def clean_text_from_tool_calls(text: str) -> str:
    text = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL | re.IGNORECASE)
    return re.sub(r"<tool_use>.*?</tool_use>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()

# ✅ NOUVEAU : résolution du mode Fast/Think à partir de la requête OpenAI-like
def resolve_reasoning_mode(data: dict, requested_model: str) -> str:
    # 1. Convention OpenAI o-series : reasoning_effort = "none" -> Fast, sinon Think
    effort = data.get("reasoning_effort")
    if effort is not None:
        return "Fast" if str(effort).lower() in ("none", "off", "minimal", "disabled") else "Think"

    # 2. Convention custom : reasoning=True/False, ou objet {"type": "enabled"/"disabled"} (façon Anthropic)
    reasoning_flag = data.get("reasoning")
    if isinstance(reasoning_flag, bool):
        return "Think" if reasoning_flag else "Fast"
    if isinstance(reasoning_flag, dict):
        return "Fast" if reasoning_flag.get("type") == "disabled" else "Think"

    # 3. Fallback : suffixe dans le nom du modèle, ex: "qwen/generic-fast" ou "qwen/generic:thinking"
    lower_model = (requested_model or "").lower()
    if "fast" in lower_model:
        return "Fast"
    if "think" in lower_model or "reasoning" in lower_model:
        return "Think"

    # 4. Défaut rétrocompatible : on ne change rien, reasoning reste actif
    return "Think"

@app.get("/v1/models")
async def list_models():
    models = [{"id": k, "object": "model", "created": 1677652288, "owned_by": "qwen-bridge"} for k in MODEL_MAP.keys()]
    return {"object": "list", "data": models}

@app.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: str = Header(None)):
    global is_processing, response_queue, current_ui_model, current_reasoning_mode

    if authorization != f"Bearer {API_KEY}":
        raise HTTPException(status_code=401, detail="Invalid API Key")

    data = await request.json()
    messages = data.get("messages", [])
    stream = data.get("stream", True)
    requested_model = data.get("model", "qwen/generic")
    tools = data.get("tools", [])

    has_tools = bool(tools)
    allowed_tool_names = [t["function"]["name"] for t in tools] if has_tools else []

    if has_tools:
        tool_prompt = TOOL_SYSTEM_PROMPT + "\n\nAvailable tools:\n"
        for t in tools:
            fn = t.get("function", {})
            tool_prompt += f"- {fn.get('name')}: {fn.get('description')}\n"
        if messages and messages[0].get("role") == "system": messages[0]["content"] += "\n\n" + tool_prompt
        else: messages.insert(0, {"role": "system", "content": tool_prompt.strip()})

    prompt = "\n".join([f"{m['role']}: {m['content']}" for m in messages])
    target_ui_model = MODEL_MAP.get(requested_model, "Qwen3.7-Plus")
    target_reasoning_mode = resolve_reasoning_mode(data, requested_model)
    task_id = str(uuid.uuid4())

    async with response_lock:
        if is_processing: raise HTTPException(status_code=429, detail="Bridge is busy.")
        is_processing = True
        response_queue = asyncio.Queue()

    if target_ui_model != current_ui_model:
        print(f"\n🔄 [API] Switching UI model to: {target_ui_model}...", flush=True)
        current_ui_model = target_ui_model
        command_queue.put({"action": "switch_model", "model": target_ui_model})
        await asyncio.sleep(2.0)

    # ✅ NOUVEAU : switch Fast/Think si nécessaire, avant l'envoi du prompt
    if target_reasoning_mode != current_reasoning_mode:
        print(f"🧠 [API] Switching reasoning mode to: {target_reasoning_mode}...", flush=True)
        current_reasoning_mode = target_reasoning_mode
        command_queue.put({"action": "switch_reasoning_mode", "mode": target_reasoning_mode})
        await asyncio.sleep(1.0)

    command_queue.put({"action": "send_prompt", "prompt": prompt})
    print(f"\n📡 [API] Forwarding to browser (Model: {target_ui_model}, Reasoning: {target_reasoning_mode}, Stream: {stream}, HasTools: {has_tools})...", flush=True)

    # 🎯 MODE STREAMING
    if stream:
        async def event_generator():
            global response_queue, is_processing

            content_buffer = ""
            has_function_call = False
            current_tool_call_id = None

            try:
                yield f"data: {json.dumps({'id': f'chatcmpl-{task_id}', 'object': 'chat.completion.chunk', 'created': 1677652288, 'model': requested_model, 'choices': [{'index': 0, 'delta': {'reasoning_content': ' '}, 'finish_reason': None}]})}\n\n"
                await asyncio.sleep(0)

                while True:
                    token_data = await asyncio.wait_for(response_queue.get(), timeout=120.0)
                    if token_data.get("type") == "done":
                        break

                    if token_data.get("type") == "reasoning":
                        text = token_data["text"]
                        yield f"data: {json.dumps({'id': f'chatcmpl-{task_id}', 'object': 'chat.completion.chunk', 'created': 1677652288, 'model': requested_model, 'choices': [{'index': 0, 'delta': {'reasoning_content': text}, 'finish_reason': None}]})}\n\n"

                    elif token_data.get("type") == "content":
                        text = token_data["text"]
                        content_buffer += text
                        yield f"data: {json.dumps({'id': f'chatcmpl-{task_id}', 'object': 'chat.completion.chunk', 'created': 1677652288, 'model': requested_model, 'choices': [{'index': 0, 'delta': {'content': text}, 'finish_reason': None}]})}\n\n"

                    elif token_data.get("type") == "function_call":
                        has_function_call = True
                        name = token_data.get("name", "")
                        args = token_data.get("arguments", "")

                        if name and not current_tool_call_id:
                            current_tool_call_id = f"call_{uuid.uuid4().hex[:24]}"
                            tool_call_chunk = {
                                "index": 0,
                                "id": current_tool_call_id,
                                "type": "function",
                                "function": {"name": name, "arguments": args or ""}
                            }
                        else:
                            tool_call_chunk = {
                                "index": 0,
                                "function": {"arguments": args}
                            }

                        yield f"data: {json.dumps({'id': f'chatcmpl-{task_id}', 'object': 'chat.completion.chunk', 'created': 1677652288, 'model': requested_model, 'choices': [{'index': 0, 'delta': {'content': None, 'tool_calls': [tool_call_chunk]}, 'finish_reason': None}]})}\n\n"

                if has_function_call:
                    yield f"data: {json.dumps({'id': f'chatcmpl-{task_id}', 'object': 'chat.completion.chunk', 'created': 1677652288, 'model': requested_model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]})}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                if has_tools:
                    openai_tool_calls = convert_llm_output_to_openai_tool_calls(content_buffer, allowed_tools=allowed_tool_names)
                    if openai_tool_calls:
                        print(f"✅ [API] {len(openai_tool_calls)} tool call(s) detected (text fallback).", flush=True)
                        yield f"data: {json.dumps({'id': f'chatcmpl-{task_id}', 'object': 'chat.completion.chunk', 'created': 1677652288, 'model': requested_model, 'choices': [{'index': 0, 'delta': {'content': None, 'tool_calls': openai_tool_calls}, 'finish_reason': 'tool_calls'}]})}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                yield f"data: {json.dumps({'id': f'chatcmpl-{task_id}', 'object': 'chat.completion.chunk', 'created': 1677652288, 'model': requested_model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
                yield "data: [DONE]\n\n"

            except asyncio.TimeoutError:
                print("⚠️ [API] Timeout 120s", flush=True)
                yield "data: [DONE]\n\n"
            finally:
                async with response_lock:
                    is_processing = False
                    response_queue = None

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # 🎯 MODE NON-STREAMING
    else:
        full_response = ""
        try:
            while True:
                token_data = await asyncio.wait_for(response_queue.get(), timeout=120.0)
                if token_data.get("type") == "done": break
                if token_data.get("type") in ["content", "reasoning"]:
                    full_response += token_data["text"]
        except asyncio.TimeoutError:
            print("⚠️ [API] Timeout 120s", flush=True)
        finally:
            async with response_lock:
                is_processing = False
                response_queue = None

        if has_tools:
            openai_tool_calls = convert_llm_output_to_openai_tool_calls(full_response, allowed_tools=allowed_tool_names)
            if openai_tool_calls:
                return {
                    "id": f"chatcmpl-{task_id}", "object": "chat.completion", "created": 1677652288, "model": requested_model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": openai_tool_calls}, "finish_reason": "tool_calls"}]
                }

        clean_text = clean_text_from_tool_calls(full_response)
        return {
            "id": f"chatcmpl-{task_id}", "object": "chat.completion", "created": 1677652288, "model": requested_model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": clean_text}, "finish_reason": "stop"}]
        }

@app.post("/qwen-stream")
async def receive_stream(request: Request):
    global response_queue
    data = await request.json()

    if data.get("type") == "stream":
        content = data.get("content", "")
        reasoning = data.get("reasoning", "")
        if response_queue is not None:
            if reasoning:
                print(f"🧠 [Server] Received reasoning ({len(reasoning)} chars)", flush=True)
                await response_queue.put({"type": "reasoning", "text": reasoning})
            if content:
                print(f"📝 [Server] Received content ({len(content)} chars)", flush=True)
                await response_queue.put({"type": "content", "text": content})

    elif data.get("type") == "function_call":
        name = data.get("name", "")
        args = data.get("arguments", "")
        if response_queue is not None:
            print(f"🔧 [Server] Received function_call: {name}", flush=True)
            await response_queue.put({"type": "function_call", "name": name, "arguments": args})

    elif data.get("type") == "done":
        print("\n" + "="*60, flush=True)
        print("✅ QWEN FINISHED!", flush=True)
        print("="*60 + "\n", flush=True)
        if response_queue is not None:
            await response_queue.put({"type": "done"})

    elif data.get("type") == "error":
        print(f"\n❌ [API] BROWSER ERROR: {data.get('content')}", flush=True)
        if response_queue is not None:
            await response_queue.put({"type": "done"})

    return {"status": "ok"}

@app.get("/pending-command")
async def get_command():
    try: return command_queue.get_nowait()
    except queue.Empty: return {"action": None}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
