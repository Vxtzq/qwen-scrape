## ⚠️⚠️ DISCLAIMER - might be against Qwen Studio ToS, Use at your own risk ⚠️⚠️

# Qwen Studio OpenAI API Bridge

A local OpenAI-compatible API bridge that allows AI coding agents (Cline, Roo Code, Continue.dev, Cursor, and custom Python agents) to use Qwen Studio models through a local network endpoint.

The bridge exposes an OpenAI-style API (`/v1/chat/completions`, `/v1/models`) and forwards requests to Qwen Studio by controlling an authenticated browser session via lightweight DOM/Network automation.

## Features

* ✅ Fully OpenAI-compatible API endpoint
* ✅ Works seamlessly with coding agents supporting custom OpenAI providers
* ✅ Supports dynamic Qwen Studio web model switching (e.g., Qwen3.7-Max, Qwen3.7-Plus)
* ✅ Real-time streaming response support
* ✅ Local-network architecture (accessible via `192.168.x.x`)
* ✅ No local model hosting or GPU required
* ✅ Compatible with OpenAI SDK / LangChain style clients

## Architecture

```text
Coding Agent (VS Code, Python, etc.)
     |
     | OpenAI API format (HTTP)
     v
Bridge Server (http://127.0.0.1:8000/v1)
     |
     | Command Queue & Stream Relay
     v
Browser Automation (Tampermonkey / Console)
     |
     | Native DOM Injection & Network Interception
     v
Qwen Studio Web Interface (Authenticated Session)
```

## Requirements

* Python 3.10+
* A valid, logged-in Qwen Studio session in a Chromium-based browser (Chrome, Edge, Brave)
* Tampermonkey extension (Recommended) or manual Console access

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/your-user/qwen-openai-bridge.git
   cd qwen-openai-bridge
   ```

2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

### 1. Start the Bridge Server

```bash
python server.py
```
You should see:
```text
🟢 GOD-MODE BRIDGE ACTIVE.
🔑 API Key: sk-qwen-bridge-key
```

### 2. Connect the Browser Session (Recommended: Tampermonkey)

Instead of pasting code into the console every time, automate it:

1. Install the [Tampermonkey](https://www.tampermonkey.net/) extension in your browser.
2. Click the Tampermonkey icon → **Create a new script**.
3. Paste the contents of `browser_script.user.js` (provided in this repo).
4. Save (`Ctrl+S` / `Cmd+S`).
5. Open Qwen Studio. The script will automatically activate and connect to your local server. Keep this tab open.

*(Fallback: If you don't use Tampermonkey, open DevTools `F12 → Console` in the Qwen tab and paste the raw JavaScript code manually).*

## API Configuration

The bridge exposes its API on your local network. Find your local IP (e.g., `192.168.1.38`) and configure your agents as follows:

| Setting  | Value                                |
| -------- | ------------------------------------ |
| Base URL | `http://127.0.0.1:8000/v1` or `http://your_ip/v1`      |
| API Key  | `sk-qwen-bridge-key`                 |
| Model    | `qwen/qwen3.7-max` or `qwen/qwen3.7-plus` or `qwen/qwen3.6-plus`|

## Supported Clients

### Cline / Roo Code (VS Code)
1. Open Cline Settings → **API Provider**.
2. Select **OpenAI Compatible**.
3. Enter the Base URL, API Key, and Model ID from the table above.
4. Click **Verify Connection**.

### Continue.dev
Add to your `config.json`:
```json
{
  "models": [
    {
      "title": "Qwen Bridge (Max)",
      "provider": "openai",
      "model": "qwen/qwen3.7-max",
      "apiBase": "http://192.168.1.38:8000/v1",
      "apiKey": "sk-qwen-bridge-key"
    }
  ]
}
```

### Cursor
1. Go to **Cursor Settings** → **Models**.
2. Click **Add OpenAI Compatible Model**.
3. Fill in the Name, Base URL, API Key, and Model Name.

## Python Example

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://192.168.1.38:8000/v1",
    api_key="sk-qwen-bridge-key"
)

response = client.chat.completions.create(
    model="qwen/qwen3.7-max",
    messages=[{"role": "user", "content": "Write a Python script to parse JSON."}],
    stream=True
)

for chunk in response:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

## Troubleshooting

### Connection Failed
* Ensure `server.py` is running.
* Ensure the Qwen browser tab is open and the Tampermonkey script is active.
* Verify your Base URL includes `/v1` (e.g., `http://192.168.1.38:8000/v1`).

### Model Not Found
* Test the discovery endpoint in your browser: `http://192.168.1.38:8000/v1/models`
* Ensure the `model` string in your agent exactly matches one of the returned `id` values (e.g., `qwen/qwen3.7-max`).

## Security Notes

⚠️ **This project is intended for local, trusted network use only.** 
Do not expose port `8000` to the public internet. Your browser session contains authenticated access to your Qwen account. 

## Disclaimer

This project interacts with a third-party web interface through browser automation and network interception. Use responsibly and respect the Terms of Service of the platforms you connect to. This is an unofficial community tool.

## License

MIT License
