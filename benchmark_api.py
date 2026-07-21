import requests
import json

URL = "http://127.0.0.1:8000/v1/chat/completions"
HEADERS = {
    "Authorization": "Bearer sk-qwen-bridge-key",
    "Content-Type": "application/json"
}


def run_test(label, model, reasoning_effort, prompt):
    print(f"\n{'='*70}")
    print(f"🧪 TEST: {label}")
    print(f"   model={model} | reasoning_effort={reasoning_effort}")
    print(f"{'='*70}\n")

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "reasoning_effort": reasoning_effort
    }

    saw_reasoning = False
    saw_content = False

    with requests.post(URL, headers=HEADERS, json=payload, stream=True) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            decoded = line.decode("utf-8")
            if not decoded.startswith("data: "):
                continue
            data_str = decoded[len("data: "):].strip()
            if data_str == "[DONE]":
                print("\n\n✅ DONE")
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            delta = chunk.get("choices", [{}])[0].get("delta", {})

            if delta.get("reasoning_content"):
                saw_reasoning = True
                print(f"\033[90m{delta['reasoning_content']}\033[0m", end="", flush=True)

            if delta.get("content"):
                saw_content = True
                print(delta["content"], end="", flush=True)

            if delta.get("tool_calls"):
                print(f"\n🔧 TOOL CALL: {json.dumps(delta['tool_calls'], indent=2)}")

    print(f"\n\n📊 Summary: reasoning_seen={saw_reasoning} | content_seen={saw_content}")


if __name__ == "__main__":
    # Test 1
    run_test(
        label="Qwen3.8-Max — Fast (no reasoning)",
        model="qwen/qwen3.8-max",
        reasoning_effort="none",
        prompt="What is the capital of France? Answer in one sentence."
    )

    # Test 2
    run_test(
        label="Qwen3.7-Plus — Think (reasoning enabled)",
        model="qwen/qwen3.7-plus",
        reasoning_effort="high",
        prompt="If a train leaves at 3pm going 60mph and another leaves at 4pm going 90mph in the same direction, when does the second train catch up? Think step by step."
    )
