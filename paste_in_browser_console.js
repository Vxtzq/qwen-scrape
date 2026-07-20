// ==UserScript==
// @name         Qwen OpenAI Bridge + Reasoning Extractor
// @namespace    http://tampermonkey.net/
// @version      4.9
// @description  Immediate thinking, clean tool_call streaming, no content leak
// @match        https://chat.qwen.ai/*
// @match        https://qwenlm.ai/*
// @match        https://chat.qwenlm.ai/*
// @grant        none
// @run-at       document-start
// ==/UserScript==

(() => {
  console.log("🌌🧠 [Tampermonkey] Bridge v4.9 Active");
  const SERVER_URL = "http://127.0.0.1:8000";

  let isProcessingStream = false;
  let currentGenerationId = 0;
  let contentBuffer = "";
  let reasoningBuffer = "";
  let flushInterval = null;
  let isSending = false;
  let watchdog = null;

  function startWatchdog() {
    stopWatchdog();
    watchdog = setInterval(() => {
      if (isProcessingStream) return;
      fetch(`${SERVER_URL}/qwen-stream`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: "error", content: "Watchdog timeout" })
      }).catch(() => {});
      stopWatchdog();
    }, 25000);
  }

  function stopWatchdog() {
    if (watchdog) { clearInterval(watchdog); watchdog = null; }
  }

  function typeAndSend(text) {
    if (isSending) return;
    isSending = true;
    const textarea = document.querySelector('textarea.message-input-textarea') || document.querySelector('textarea');
    if (!textarea) { isSending = false; return; }
    textarea.focus();
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    nativeSetter.call(textarea, text);
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    setTimeout(() => {
      const sendBtn = document.querySelector('button[aria-label="Send"]') ||
                      document.querySelector('button.send-button') ||
                      Array.from(document.querySelectorAll('button')).find(b => !b.disabled && b.querySelector('svg'));
      if (sendBtn && !sendBtn.disabled) sendBtn.click();
      else textarea.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
      setTimeout(() => { isSending = false; }, 1500);
    }, 100);
  }

  setInterval(async () => {
    try {
      const res = await fetch(`${SERVER_URL}/pending-command`);
      const data = await res.json();
      if (data.action === "send_prompt") { startWatchdog(); typeAndSend(data.prompt); }
    } catch (e) {}
  }, 1000);

  function startFlushing() {
    if (flushInterval) return;
    flushInterval = setInterval(() => {
      if (contentBuffer.length > 0 || reasoningBuffer.length > 0) {
        const pc = contentBuffer; contentBuffer = "";
        const pr = reasoningBuffer; reasoningBuffer = "";
        fetch(`${SERVER_URL}/qwen-stream`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ type: "stream", content: pc, reasoning: pr })
        }).catch(() => {});
      }
    }, 100);
  }

  function stopFlushing() {
    if (flushInterval) { clearInterval(flushInterval); flushInterval = null; }
    if (contentBuffer.length > 0 || reasoningBuffer.length > 0) {
      fetch(`${SERVER_URL}/qwen-stream`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: "stream", content: contentBuffer, reasoning: reasoningBuffer })
      }).catch(() => {});
      contentBuffer = ""; reasoningBuffer = "";
    }
    fetch(`${SERVER_URL}/qwen-stream`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: "done" })
    }).catch(() => {});
  }

  const originalFetch = window.fetch;
  window.fetch = async function (...args) {
    const response = await originalFetch.apply(this, args);
    const url = (typeof args[0] === 'string') ? args[0] : args[0]?.url || '';
    if (url.includes('/chat/completions') || url.includes('/api/chat') || url.includes('/v1/chat')) {
      const clone = response.clone();
      const myGenId = ++currentGenerationId;
      console.log(`🆕 [JS] Stream intercepted, genId=${myGenId}`);

      // ✅ THINKING INSTANTANÉ : envoyer un reasoning visible AVANT de lire le stream
      fetch(`${SERVER_URL}/qwen-stream`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: "reasoning", text: "⏳" })
      }).catch(() => {});

      processAndForwardStream(clone, myGenId);
    }
    return response;
  };

  async function processAndForwardStream(response, genId) {
    if (genId !== currentGenerationId) return;

    isProcessingStream = true;
    contentBuffer = ""; reasoningBuffer = "";

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let isActive = true;
    let isThinking = false;
    let lastReasoningIndex = 0;
    let lastReasoningDelta = "";
    let lastReasoningString = "";
    let lastUpstreamContent = "";
    let isCumulative = null;

    // ✅ Flag tool call : une fois vrai, on IGNORE tout contenu texte (c'est le JSON du tool call qui fuit)
    let toolCallDetected = false;

    startFlushing();

    try {
      while (isActive) {
        if (genId !== currentGenerationId) break;
        const { done, value } = await reader.read();
        if (done) { isActive = false; break; }

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (genId !== currentGenerationId) { isActive = false; break; }
          if (!line.startsWith('data: ')) continue;

          const jsonData = line.substring(6).trim();
          if (jsonData === '[DONE]') { isActive = false; break; }

          try {
            const parsed = JSON.parse(jsonData);
            const delta = parsed.choices?.[0]?.delta;
            if (!delta) continue;

            // 🧠 REASONING
            if (delta?.phase === "thinking_summary") {
              isThinking = true;
              if (delta?.extra?.summary_thought?.content) {
                const thoughts = delta.extra.summary_thought.content;
                if (Array.isArray(thoughts)) {
                  if (thoughts.length > lastReasoningIndex) {
                    reasoningBuffer += thoughts.slice(lastReasoningIndex).join("");
                    lastReasoningIndex = thoughts.length;
                  } else if (thoughts.length > 0) {
                    const cj = thoughts.join("");
                    if (cj && cj !== lastReasoningDelta) { reasoningBuffer += cj; lastReasoningDelta = cj; }
                  }
                } else if (typeof thoughts === 'string') {
                  let dt = thoughts;
                  if (lastReasoningString && thoughts.startsWith(lastReasoningString)) dt = thoughts.slice(lastReasoningString.length);
                  if (dt) { reasoningBuffer += dt; lastReasoningString = thoughts; }
                }
              }
            }

            // 🔧 TOOL CALLS NATIFS (tool_calls[] ou function_call)
            const toolCalls = delta?.tool_calls;
            const fnCall = delta?.function_call;

            if ((toolCalls && Array.isArray(toolCalls) && toolCalls.length > 0) || fnCall) {
              isThinking = false;

              // ✅ Flush le contenu texte AVANT le tool call (texte introductif du modèle)
              if (contentBuffer.length > 0) {
                fetch(`${SERVER_URL}/qwen-stream`, {
                  method: "POST", headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ type: "stream", content: contentBuffer, reasoning: "" })
                }).catch(() => {});
                contentBuffer = "";
              }

              toolCallDetected = true;

              let fnName = "", argsStr = "";
              if (toolCalls && toolCalls.length > 0) {
                fnName = toolCalls[0]?.function?.name || "";
                argsStr = toolCalls[0]?.function?.arguments || "";
              } else if (fnCall) {
                fnName = fnCall.name || "";
                argsStr = fnCall.arguments || "";
              }

              fetch(`${SERVER_URL}/qwen-stream`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ type: "function_call", name: fnName, arguments: argsStr })
              }).catch(() => {});
            }

            if (isThinking && delta?.phase === "answer") isThinking = false;

            // 📝 CONTENU NORMAL — IGNORÉ si un tool call a été détecté (c'est le JSON qui fuit)
            const upstreamContent = delta?.content;
            if (upstreamContent && typeof upstreamContent === 'string' && !isThinking && !toolCallDetected) {
              if (isCumulative === null && lastUpstreamContent.length > 0) {
                isCumulative = upstreamContent.startsWith(lastUpstreamContent);
              }
              let trueDelta = upstreamContent;
              if (isCumulative === true) trueDelta = upstreamContent.slice(lastUpstreamContent.length);
              if (trueDelta) contentBuffer += trueDelta;
              if (isCumulative === true || isCumulative === null) lastUpstreamContent = upstreamContent;
            }
          } catch (e) {}
        }
      }
    } catch (e) {
      console.error("💥 [JS] Stream error:", e);
    } finally {
      if (genId === currentGenerationId) { isProcessingStream = false; stopFlushing(); }
    }
  }

  console.log("✅ Bridge v4.9 ready.");
})();
