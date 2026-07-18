// ==UserScript==
// @name         Qwen OpenAI Bridge Automation
// @namespace    http://tampermonkey.net/
// @version      1.0
// @description  Auto-connects Qwen Studio to the local OpenAI Bridge server
// @match        https://chat.qwenlm.ai/*
// @match        https://qwenlm.ai/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

(() => {
  console.log("🌌 [Tampermonkey] Initializing God-Mode Browser Puppeteer...");
  
  // CHANGE THIS IF YOUR SERVER IS ON A DIFFERENT IP
  const SERVER_URL = "http://192.168.1.38:8000"; 
  
  let isProcessingStream = false;
  let tokenBuffer = "";
  let flushInterval = null;
  let isSending = false;

  async function switchModelInUI(targetModelName) {
    console.log(`🔄 UI: Attempting to switch model to: ${targetModelName}`);
    const trigger = document.querySelector('[aria-label="Select Model"]');
    if (!trigger) return console.error("❌ UI: Model selector trigger not found!");
    
    trigger.click();
    await new Promise(r => setTimeout(r, 600));

    const options = document.querySelectorAll('[role="option"]');
    let targetOption = null;
    for (const opt of options) {
      if (opt.innerText.includes(targetModelName)) {
        targetOption = opt;
        break;
      }
    }

    if (targetOption) {
      targetOption.click();
      console.log(`✅ UI: Successfully switched to ${targetModelName}`);
    } else {
      console.error(`❌ UI: Model "${targetModelName}" not found in dropdown.`);
      trigger.click();
    }
    await new Promise(r => setTimeout(r, 800));
  }

  function typeAndSend(text) {
    if (isSending) {
      console.warn("⚠️ UI: Already sending, ignoring duplicate command.");
      return;
    }
    isSending = true;

    const textarea = document.querySelector('textarea.message-input-textarea');
    if (!textarea) {
      console.error("❌ UI: Textarea not found!");
      isSending = false;
      return;
    }

    console.log(`⌨️ UI: Injecting prompt...`);
    textarea.focus();
    
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    nativeSetter.call(textarea, text);
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    
    setTimeout(() => {
      const sendBtn = document.querySelector('button[aria-label="Send"]') || 
                      document.querySelector('button.send-button') ||
                      Array.from(document.querySelectorAll('button')).find(b => 
                        !b.disabled && b.querySelector('svg') && b.getBoundingClientRect().width > 0
                      );
                      
      if (sendBtn && !sendBtn.disabled) {
        sendBtn.click();
        console.log("✅ UI: Send button clicked.");
      } else {
        console.warn("⚠️ UI: Send button not found. Fallback to Enter key.");
        textarea.dispatchEvent(new KeyboardEvent('keydown', {
          key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true, cancelable: true
        }));
      }
      setTimeout(() => { isSending = false; }, 1500);
    }, 100);
  }

  setInterval(async () => {
    try {
      const res = await fetch(`${SERVER_URL}/pending-command`);
      const data = await res.json();
      
      if (data.action === "switch_model") {
        await switchModelInUI(data.model);
      } else if (data.action === "send_prompt") {
        typeAndSend(data.prompt);
      }
    } catch (e) { /* Ignore network hiccups */ }
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
      }); tokenBuffer = "";
    }
    fetch(`${SERVER_URL}/qwen-stream`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: "done", content: "" })
    });
  }

  const originalFetch = window.fetch;
  window.fetch = async function (...args) {
    const response = await originalFetch.apply(this, args);
    const clone = response.clone();
    const url = (typeof args[0] === 'string') ? args[0] : args[0]?.url || '';
    
    if ((url.includes('/chat/completions') || url.includes('/api/chat')) && !isProcessingStream) {
      processAndForwardStream(clone);
    }
    return response;
  };

  async function processAndForwardStream(response) {
    isProcessingStream = true;
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = ""; let isActive = true;
    startFlushing();

    while (isActive) {
      const { done, value } = await reader.read();
      if (done) { isActive = false; isProcessingStream = false; stopFlushing(); break; }
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n'); buffer = lines.pop();

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const jsonData = line.substring(6).trim();
          if (jsonData === '[DONE]') { isActive = false; isProcessingStream = false; stopFlushing(); break; }
          try {
            const parsed = JSON.parse(jsonData);
            const delta = parsed.choices?.[0]?.delta?.content;
            if (delta) tokenBuffer += delta;
          } catch (e) {}
        }
      }
    }
  }

  console.log("✅ God-Mode Puppeteer Active. Ready for commands & API calls.");
})();
