// ==UserScript==
// @name         Qwen OpenAI Bridge + Reasoning Extractor
// @namespace    http://tampermonkey.net/
// @version      4.6
// @description  Separates reasoning_content and content, handles function_call, no double-stream
// @match        https://chat.qwen.ai/*
// @match        https://qwenlm.ai/*
// @match        https://chat.qwenlm.ai/*
// @grant        none
// @run-at       document-start
// ==/UserScript==

(() => {
  console.log("🌌🧠 [Tampermonkey] Bridge v4.6 Active");
  const SERVER_URL = "http://127.0.0.1:8000";

  let isProcessingStream = false;
  let currentGenerationId = 0;

  // Buffers envoyés au serveur
  let contentBuffer = "";
  let reasoningBuffer = "";

  let flushInterval = null;
  let isSending = false;
  let watchdog = null;

  function startWatchdog() {
    stopWatchdog();
    watchdog = setInterval(() => {
      if (isProcessingStream) return;
      console.error("❌ WATCHDOG: No stream activity.");
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
      if (data.action === "send_prompt") {
        startWatchdog();
        typeAndSend(data.prompt);
      }
    } catch (e) {}
  }, 1000);

  function startFlushing() {
    if (flushInterval) return;
    flushInterval = setInterval(() => {
      if (contentBuffer.length > 0 || reasoningBuffer.length > 0) {
        const payloadContent = contentBuffer; contentBuffer = "";
        const payloadReasoning = reasoningBuffer; reasoningBuffer = "";

        fetch(`${SERVER_URL}/qwen-stream`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            type: "stream",
            content: payloadContent,
            reasoning: payloadReasoning
          })
        }).catch(() => {});
      }
    }, 100);
  }

  function stopFlushing() {
    if (flushInterval) { clearInterval(flushInterval); flushInterval = null; }

    if (contentBuffer.length > 0 || reasoningBuffer.length > 0) {
      fetch(`${SERVER_URL}/qwen-stream`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          type: "stream",
          content: contentBuffer,
          reasoning: reasoningBuffer
        })
      }).catch(() => {});
      contentBuffer = "";
      reasoningBuffer = "";
    }

    fetch(`${SERVER_URL}/qwen-stream`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: "done", content: "" })
    }).catch(() => {});
  }

  const originalFetch = window.fetch;

  window.fetch = async function (...args) {
    const response = await originalFetch.apply(this, args);
    const url = (typeof args[0] === 'string') ? args[0] : args[0]?.url || '';

    if (url.includes('/chat/completions') || url.includes('/api/chat') || url.includes('/v1/chat')) {
      const clone = response.clone();
      const myGenId = ++currentGenerationId;
      console.log(`🆕 [JS] New stream intercepted, genId=${myGenId}`);
      processAndForwardStream(clone, myGenId);
    }
    return response;
  };

  async function processAndForwardStream(response, genId) {
    if (genId !== currentGenerationId) {
      console.log(`🚫 [JS] genId=${genId} obsolete before start, ignoring`);
      return;
    }

    isProcessingStream = true;
    contentBuffer = "";
    reasoningBuffer = "";

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let isActive = true;

    // --- état reasoning ---
    let isThinking = false;
    let lastReasoningIndex = 0;
    let lastReasoningDelta = "";
    let lastReasoningString = "";

    // --- état content normal (cumulatif ou pas) ---
    let lastUpstreamContent = "";
    let isCumulative = null;

    // --- état function_call (mode agent / write_file) ---
    let lastFnCallArgsStr = "";

    startFlushing();

    try {
      while (isActive) {
        if (genId !== currentGenerationId) {
          console.log(`🚫 [JS] genId=${genId} superseded mid-stream, aborting`);
          break;
        }

        const { done, value } = await reader.read();
        if (done) { isActive = false; break; }

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (genId !== currentGenerationId) { isActive = false; break; }

          if (line.startsWith('data: ')) {
            const jsonData = line.substring(6).trim();
            if (jsonData === '[DONE]') { isActive = false; break; }

            try {
              const parsed = JSON.parse(jsonData);
              const delta = parsed.choices?.[0]?.delta;
              if (!delta) continue;

              // 🧠 1. REASONING
              if (delta?.phase === "thinking_summary") {
                isThinking = true;

                if (delta?.extra?.summary_thought?.content) {
                  const thoughts = delta.extra.summary_thought.content;

                  if (Array.isArray(thoughts)) {
                    if (thoughts.length > lastReasoningIndex) {
                      const newFragments = thoughts.slice(lastReasoningIndex);
                      reasoningBuffer += newFragments.join("");
                      lastReasoningIndex = thoughts.length;
                    } else if (thoughts.length > 0) {
                      const currentJoin = thoughts.join("");
                      if (currentJoin && currentJoin !== lastReasoningDelta) {
                        reasoningBuffer += currentJoin;
                        lastReasoningDelta = currentJoin;
                      }
                    }
                  } else if (typeof thoughts === 'string') {
                    let deltaThought = thoughts;
                    if (lastReasoningString && thoughts.startsWith(lastReasoningString)) {
                      deltaThought = thoughts.slice(lastReasoningString.length);
                    }
                    if (deltaThought) {
                      reasoningBuffer += deltaThought;
                      lastReasoningString = thoughts;
                    }
                  }
                }
              }

              // 🔧 2. FUNCTION_CALL (mode agent / Qwen Code / write_file)
              const fnCall = delta?.function_call;
              if (fnCall && typeof fnCall.arguments === 'string') {
                isThinking = false;

                const argsStr = fnCall.arguments;
                let rawDelta = argsStr;
                if (argsStr.startsWith(lastFnCallArgsStr)) {
                  rawDelta = argsStr.slice(lastFnCallArgsStr.length);
                }
                lastFnCallArgsStr = argsStr;

                if (rawDelta) {
                  const decoded = rawDelta
                    .replace(/\\n/g, '\n')
                    .replace(/\\t/g, '\t')
                    .replace(/\\"/g, '"')
                    .replace(/\\\\/g, '\\');
                  contentBuffer += decoded;
                }
              }

              // 🎯 3. FIN DE PHASE THINKING (si le stream le signale explicitement)
              if (isThinking && delta?.phase === "answer") {
                isThinking = false;
              }

              // 📝 4. CONTENU NORMAL
              const upstreamContent = delta?.content;
              if (upstreamContent && typeof upstreamContent === 'string' && !isThinking && !fnCall) {
                if (isCumulative === null && lastUpstreamContent.length > 0) {
                  isCumulative = upstreamContent.startsWith(lastUpstreamContent);
                }

                let trueDelta = upstreamContent;
                if (isCumulative === true) {
                  trueDelta = upstreamContent.slice(lastUpstreamContent.length);
                }

                if (trueDelta) {
                  contentBuffer += trueDelta;
                }

                if (isCumulative === true || isCumulative === null) {
                  lastUpstreamContent = upstreamContent;
                }
              }
            } catch (e) {
              console.error("⚠️ [JS] Parse error on chunk:", e, jsonData);
            }
          }
        }
      }
    } catch (e) {
      console.error("💥 [JS] Stream error:", e);
    } finally {
      if (genId === currentGenerationId) {
        isProcessingStream = false;
        stopFlushing();
      }
    }
  }

  console.log("✅ Bridge v4.6 ready.");
})();
