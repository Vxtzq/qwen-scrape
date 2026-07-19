// ==UserScript==
// @name         Qwen OpenAI Bridge Automation
// @namespace    http://tampermonkey.net/
// @version      3.2
// @description  Fixed cumulative delta + multi-response filtering
// @match        https://chat.qwenlm.ai/*
// @match        https://qwenlm.ai/*
// @match        https://chat.qwen.ai/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

(() => {
  console.log("🌌 [Tampermonkey] Bridge v3.2 Active (Multi-Response Fix)");
  const SERVER_URL = "http://127.0.0.1:8000"; 
  
  let isProcessingStream = false;
  let primaryStreamActive = false; // 🎯 NOUVEAU: Tracker le flux principal
  let tokenBuffer = "";
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
    if (!textarea) {
      console.error("❌ UI: Textarea not found.");
      fetch(`${SERVER_URL}/qwen-stream`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: "error", content: "Textarea not found" })
      }).catch(() => {});
      isSending = false;
      return;
    }

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
      if (tokenBuffer.length > 0) {
        const payload = tokenBuffer; tokenBuffer = "";
        fetch(`${SERVER_URL}/qwen-stream`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ type: "stream", content: payload })
        }).catch(() => {});
      }
    }, 100);
  }

  function stopFlushing() {
    if (flushInterval) { clearInterval(flushInterval); flushInterval = null; }
    if (tokenBuffer.length > 0) {
      fetch(`${SERVER_URL}/qwen-stream`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: "stream", content: tokenBuffer })
      }).catch(() => {}); 
      tokenBuffer = "";
    }
    fetch(`${SERVER_URL}/qwen-stream`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: "done", content: "" })
    }).catch(() => {});
  }

  const originalFetch = window.fetch;
  window.fetch = async function (...args) {
    const response = await originalFetch.apply(this, args);
    const clone = response.clone();
    const url = (typeof args[0] === 'string') ? args[0] : args[0]?.url || '';
    
    // 🎯 FIX: N'intercepter que le premier flux, ignorer les réponses alternatives
    if ((url.includes('/chat/completions') || url.includes('/api/chat') || url.includes('/v1/chat'))) {
      if (!primaryStreamActive) {
        primaryStreamActive = true; // Verrouiller pour ignorer les flux suivants
        processAndForwardStream(clone);
      } else {
        console.log("🚫 [JS] Ignoring secondary/alternative response stream");
      }
    }
    return response;
  };

  async function processAndForwardStream(response) {
    isProcessingStream = true;
    tokenBuffer = "";
    
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = ""; 
    let isActive = true;
    
    let lastUpstreamContent = "";
    let isCumulative = null;

    startFlushing();

    while (isActive) {
      const { done, value } = await reader.read();
      if (done) { 
        isActive = false; 
        isProcessingStream = false; 
        primaryStreamActive = false; // 🎯 Libérer le verrou
        stopFlushing(); 
        break; 
      }
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n'); buffer = lines.pop();

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const jsonData = line.substring(6).trim();
          if (jsonData === '[DONE]') { 
            isActive = false; 
            isProcessingStream = false; 
            primaryStreamActive = false; // 🎯 Libérer le verrou
            stopFlushing(); 
            break; 
          }
          try {
            const parsed = JSON.parse(jsonData);
            const upstreamContent = parsed.choices?.[0]?.delta?.content;
            
            if (upstreamContent && typeof upstreamContent === 'string') {
              if (isCumulative === null && lastUpstreamContent.length > 0) {
                isCumulative = upstreamContent.startsWith(lastUpstreamContent);
              }

              let trueDelta = upstreamContent;
              
              if (isCumulative === true) {
                trueDelta = upstreamContent.slice(lastUpstreamContent.length);
              }

              if (trueDelta) {
                tokenBuffer += trueDelta;
              }

              if (isCumulative === true) {
                lastUpstreamContent = upstreamContent;
              } else if (isCumulative === null) {
                lastUpstreamContent = upstreamContent;
              }
            }
          } catch (e) {}
        }
      }
    }
  }
})();
