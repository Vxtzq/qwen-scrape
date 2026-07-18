# Qwen Studio OpenAI API Bridge

A local OpenAI-compatible API bridge that allows AI coding agents (Cline, Roo Code, Continue.dev, Cursor, custom agents, etc.) to use Qwen Studio models through a local endpoint.

The bridge exposes an OpenAI-style API (`/v1/chat/completions`, `/v1/models`) and forwards requests to Qwen Studio by controlling an authenticated browser session.

## Features

* ✅ OpenAI-compatible API endpoint
* ✅ Works with coding agents supporting custom OpenAI providers
* ✅ Supports Qwen Studio web models
* ✅ Streaming responses support
* ✅ Local-only architecture
* ✅ No model hosting required
* ✅ Compatible with OpenAI SDK / LangChain style clients

## Architecture

```
Coding Agent
     |
     | OpenAI API format
     |
     v
http://127.0.0.1:8000/v1
     |
     | Bridge Server
     |
     v
Browser Automation
     |
     v
Qwen Studio Web Interface
```

## Requirements

* Python 3.10+
* A Qwen Studio account
* Chromium-based browser
* Logged-in Qwen Studio session

## Installation

Clone the repository:

```bash
git clone https://github.com/your-user/qwen-openai-bridge.git
cd qwen-openai-bridge
```

Install dependencies:

```bash
pip install -r requirements.txt
```

## Usage

### 1. Start the bridge server

```bash
python server.py
```

You should see:

```
🟢 Qwen OpenAI Bridge ACTIVE
Running on http://127.0.0.1:8000
```

### 2. Connect the browser session

Open Qwen Studio in your browser.

Open Developer Tools:

```
F12 → Console
```

Paste the provided browser automation script.

Keep the tab open while using the API.

## API Configuration

The bridge exposes:

```
http://127.0.0.1:8000/v1
```

Example configuration:

| Setting  | Value                      |
| -------- | -------------------------- |
| Base URL | `http://127.0.0.1:8000/v1` |
| API Key  | `sk-qwen-bridge-key`       |
| Model    | `qwen/qwen3.7-max`         |

## Supported Clients

### Cline / Roo Code

Provider:

```
OpenAI Compatible
```

Configuration:

```
Base URL:
http://127.0.0.1:8000/v1

API Key:
sk-qwen-bridge-key

Model:
qwen/qwen3.7-max
```

---

### Continue.dev

Add a custom OpenAI-compatible model:

```json
{
  "models": [
    {
      "title": "Qwen Bridge",
      "provider": "openai",
      "model": "qwen/qwen3.7-max",
      "apiBase": "http://127.0.0.1:8000/v1",
      "apiKey": "sk-qwen-bridge-key"
    }
  ]
}
```

---

### Cursor

Add a custom OpenAI-compatible model:

```
Name:
Qwen Bridge

Base URL:
http://127.0.0.1:8000/v1

API Key:
sk-qwen-bridge-key

Model:
qwen/qwen3.7-max
```

---

## Python Example

Using the OpenAI SDK:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="sk-qwen-bridge-key"
)

response = client.chat.completions.create(
    model="qwen/qwen3.7-max",
    messages=[
        {
            "role": "user",
            "content": "Explain this code"
        }
    ]
)

print(response.choices[0].message.content)
```

## Troubleshooting

### Connection failed

Check that:

* `server.py` is running
* The Qwen browser tab is open
* The automation script is active
* The API URL includes `/v1`

Correct:

```
http://127.0.0.1:8000/v1
```

Incorrect:

```
http://127.0.0.1:8000
```

---

### Model not found

Check:

```
GET http://127.0.0.1:8000/v1/models
```

The configured model name must match the returned ID.

Example:

```
qwen/qwen3.7-max
```

## Security Notes

This project is intended for local use.

Do not expose the bridge publicly without adding:

* authentication
* rate limiting
* request validation
* access controls

Your browser session gives access to your Qwen account.

## Disclaimer

This project interacts with a third-party web interface through browser automation.

Use responsibly and respect the terms of service of the services you connect to.

## License

MIT License
