/* JARVIS — клиентская логика: голос, камера, чат, санкции. */
(() => {
  'use strict';

  const $ = (s) => document.querySelector(s);

  const chatLog = $('#chat');
  const msgInput = $('#msgInput');
  const sendBtn = $('#sendBtn');
  const micBtn = $('#micBtn');
  const voiceBtn = $('#voiceBtn');
  const camBtn = $('#camBtn');
  const scanBtn = $('#scanBtn');
  const camVideo = $('#camVideo');
  const camOverlay = $('#camOverlay');
  const actionsList = $('#actionsList');
  const brainLabel = $('#brainLabel');
  const modeLabel = $('#modeLabel');
  const tierLabel = $('#tierLabel');
  const modelLabel = $('#modelLabel');
  const latencyLabel = $('#latencyLabel');
  const arcReactor = $('#arcReactor');

  const state = {
    config: null,
    history: [],          // текстовые сообщения для контекста
    lastImage: null,      // base64 кадра для следующей команды
    lastImageMeta: null,  // {w, h}
    voiceOn: false,
    listening: false,
    stream: null,
    mode: 'auto',
  };

  /* ---------------- boot ---------------- */
  async function boot() {
    try {
      const r = await fetch('/api/config');
      state.config = await r.json();
      renderConfig();
    } catch (e) {
      brainLabel.textContent = 'НЕТ СВЯЗИ';
      modeLabel.textContent = '—';
    }
  }

  function renderConfig() {
    const c = state.config;
    const fast = c.tiers.fast;
    const smart = c.tiers.smart;
    const vision = c.tiers.vision;
    const hasAnyKey = Object.values(c.providers).some(Boolean);

    if (fast.ready) {
      brainLabel.textContent = (fast.provider || '').toUpperCase();
      modeLabel.textContent = 'LIVE';
      modeLabel.style.color = 'var(--ok)';
    } else {
      brainLabel.textContent = 'ДЕМО';
      modeLabel.textContent = 'MOCK';
      modeLabel.style.color = 'var(--amber)';
    }
    if (!vision.ready) {
      /* покажем подсказку про зрение один раз */
    }
    void smart; void hasAnyKey;
  }

  /* ---------------- сообщения ---------------- */
  function addMessage(role, text, meta) {
    const div = document.createElement('div');
    div.className = 'msg ' + role;
    div.innerHTML =
      `<div class="avatar">${role === 'jarvis' ? 'J' : 'ВЫ'}</div>` +
      `<div class="bubble"></div>`;
    const bubble = div.querySelector('.bubble');
    bubble.textContent = text;
    if (meta) {
      const m = document.createElement('span');
      m.className = 'meta';
      m.textContent = meta;
      bubble.appendChild(m);
    }
    chatLog.appendChild(div);
    chatLog.scrollTop = chatLog.scrollHeight;
    return div;
  }

  function typingIndicator() {
    const div = document.createElement('div');
    div.className = 'msg jarvis typing';
    div.innerHTML = '<div class="avatar">J</div><div class="bubble"></div>';
    chatLog.appendChild(div);
    chatLog.scrollTop = chatLog.scrollHeight;
    return div;
  }

  /* ---------------- чат ---------------- */
  async function send(text) {
    text = (text || '').trim();
    if (!text && !state.lastImage) return;
    msgInput.value = '';

    addMessage('user', text || '📸 [сканирование кадра]');
    const typing = typingIndicator();
    arcReactor.classList.add('active');

    const payload = {
      message: text,
      image: state.lastImage,
      mode: state.mode,
      history: state.history.slice(-14),
    };
    // кадр используем один раз
    state.lastImage = null;
    state.lastImageMeta = null;
    scanBtn.classList.remove('ready');

    try {
      const r = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await r.json();
      typing.remove();

      let meta = `${(data.tier || '').toUpperCase()}`;
      if (data.provider && data.model) meta += ` · ${data.provider}/${data.model}`;
      if (data.latency_ms != null) meta += ` · ${data.latency_ms}ms`;
      if (data.mode === 'demo') meta += ' · ДЕМО';

      addMessage('jarvis', data.reply || '(пусто)', meta);

      tierLabel.textContent = (data.tier || '—').toUpperCase();
      modelLabel.textContent = data.model ? `${data.provider}/${data.model}` : '—';
      latencyLabel.textContent = data.latency_ms != null ? data.latency_ms + ' ms' : '—';

      state.history.push({ role: 'user', content: text || 'сканирование кадра' });
      state.history.push({ role: 'assistant', content: data.reply || '' });

      if (data.actions && data.actions.length) renderActions(data.actions);
      if (state.voiceOn) speak(data.reply);
    } catch (e) {
      typing.remove();
      addMessage('jarvis', '⚠️ Нет связи с сервером JARVIS.', 'ERROR');
    } finally {
      arcReactor.classList.remove('active');
    }
  }

  sendBtn.addEventListener('click', () => send(msgInput.value));
  msgInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') send(msgInput.value);
  });

  /* ---------------- санкции ---------------- */
  function renderActions(actions) {
    const pending = actions.filter((a) => a.status === 'pending');
    actionsList.innerHTML = '';
    if (!pending.length) {
      actionsList.innerHTML = '<div class="empty">Нет ожидающих действий</div>';
      return;
    }
    for (const a of pending) {
      const card = document.createElement('div');
      card.className = 'action-card';
      card.innerHTML =
        `<span class="risk ${a.risk}">${a.risk.toUpperCase()}</span>` +
        `<h4>${escapeHtml(a.title)}</h4>` +
        `<p>${escapeHtml(a.description || '')}</p>` +
        `<div class="row">` +
        `<button class="btn approve" data-act="approve">✓ ОДОБРИТЬ</button>` +
        `<button class="btn deny" data-act="deny">✕ ОТКЛОНИТЬ</button>` +
        `</div>`;
      card.querySelector('.approve').onclick = () => act(a.id, 'approve', card);
      card.querySelector('.deny').onclick = () => act(a.id, 'deny', card);
      actionsList.appendChild(card);
    }
  }

  async function act(id, verb, card) {
    const r = await fetch(`/api/actions/${id}/${verb}`, { method: 'POST' });
    const data = await r.json();
    if (data.ok) {
      addMessage('jarvis', data.action.result || 'Готово.', 'САНКЦИЯ');
      renderActions([]);
      if (state.voiceOn) speak(data.action.result);
    } else {
      card.querySelector('p').textContent = '⚠️ ' + (data.error || 'ошибка');
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  /* ---------------- камера ---------------- */
  camBtn.addEventListener('click', async () => {
    if (state.stream) {
      state.stream.getTracks().forEach((t) => t.stop());
      state.stream = null;
      camVideo.srcObject = null;
      camOverlay.classList.remove('hidden');
      camBtn.textContent = '▶ ВКЛ. КАМЕРУ';
      scanBtn.disabled = true;
      return;
    }
    try {
      state.stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 } },
      });
      camVideo.srcObject = state.stream;
      camOverlay.classList.add('hidden');
      camBtn.textContent = '■ ВЫКЛ. КАМЕРУ';
      scanBtn.disabled = false;
    } catch (e) {
      addMessage('jarvis', '⚠️ Нет доступа к камере (или браузер его не дал). Проверьте разрешения.', 'ОПТИКА');
    }
  });

  scanBtn.addEventListener('click', () => {
    if (!state.stream) return;
    const w = camVideo.videoWidth || 640;
    const h = camVideo.videoHeight || 480;
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    canvas.getContext('2d').drawImage(camVideo, 0, 0, w, h);
    state.lastImage = canvas.toDataURL('image/jpeg', 0.85);
    state.lastImageMeta = { w, h };
    scanBtn.classList.add('ready');
    addMessage('jarvis', '📸 Кадр захвачен. Произнесите или введите команду — я передам его «мозгу» со зрением.', 'ОПТИКА');
  });

  /* ---------------- голос: распознавание (STT) ---------------- */
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  let rec = null;
  if (SR) {
    rec = new SR();
    rec.lang = 'ru-RU';
    rec.interimResults = false;
    rec.maxAlternatives = 1;
    rec.onresult = (e) => {
      const t = e.results[0][0].transcript;
      msgInput.value = t;
      send(t);
    };
    rec.onend = () => setListening(false);
    rec.onerror = (e) => {
      setListening(false);
      if (e.error === 'not-allowed') {
        addMessage('jarvis', '⚠️ Нет доступа к микрофону. Разрешите его в браузере.', 'АУДИО');
      }
    };
  }

  micBtn.addEventListener('click', () => {
    if (!rec) {
      addMessage('jarvis', '⚠️ Распознавание речи не поддерживается этим браузером — используйте Chrome.', 'АУДИО');
      return;
    }
    if (state.listening) {
      rec.stop();
      setListening(false);
      return;
    }
    try {
      rec.start();
      setListening(true);
    } catch (e) { /* already started */ }
  });

  function setListening(on) {
    state.listening = on;
    micBtn.classList.toggle('rec', on);
    const hint = $('#micHint');
    hint.textContent = on ? '● Слушаю… говорите команду' : '';
  }

  /* ---------------- голос: озвучка (TTS) ---------------- */
  function speak(text) {
    if (!('speechSynthesis' in window)) return;
    try {
      window.speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(text);
      u.lang = 'ru-RU';
      u.rate = 1.05;
      const voices = window.speechSynthesis.getVoices();
      const ru = voices.find((v) => v.lang && v.lang.toLowerCase().startsWith('ru'));
      if (ru) u.voice = ru;
      window.speechSynthesis.speak(u);
    } catch (e) { /* тишина */ }
  }

  voiceBtn.addEventListener('click', () => {
    state.voiceOn = !state.voiceOn;
    voiceBtn.textContent = state.voiceOn ? '🔊' : '🔇';
    if (!state.voiceOn) window.speechSynthesis && window.speechSynthesis.cancel();
    if (state.voiceOn) speak('Голосовой ответ включён, сэр.');
  });

  /* ---------------- настройки ---------------- */
  const settingsModal = $('#settingsModal');
  $('#settingsBtn').onclick = () => settingsModal.classList.remove('hidden');
  $('#closeSettings').onclick = () => settingsModal.classList.add('hidden');

  async function saveKeys() {
    const pairs = [
      ['deepseek', $('#keyDeepseek').value],
      ['gemini', $('#keyGemini').value],
      ['openai', $('#keyOpenai').value],
      ['anthropic', $('#keyAnthropic').value],
    ];
    state.mode = $('#modeSelect').value;
    let done = 0;
    for (const [provider, key] of pairs) {
      if (!key.trim()) continue;
      const r = await fetch('/api/config/key', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider, key }),
      });
      const d = await r.json();
      if (d.ok) done++;
    }
    const st = $('#settingsStatus');
    st.textContent = done ? `✓ Принято ключей: ${done}. Мозг обновлён.` : 'Введите хотя бы один ключ.';
    await boot();
    renderConfig();
  }
  $('#saveSettings').onclick = saveKeys;

  /* ---------------- документация ---------------- */
  const docsModal = $('#docsModal');
  const docsContent = $('#docsContent');

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  function inlineMd(s) {
    s = esc(s);
    s = s.replace(/`([^`]+)`/g, '<code class="doc-inline">$1</code>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
    return s;
  }

  function mdToHtml(src) {
    const blocks = [];
    src = src.replace(/```[a-z]*\n([\s\S]*?)```/g, (m, code) => {
      blocks.push('<pre class="doc-code">' + esc(code.replace(/\n$/, '')) + '</pre>');
      return '\u0000' + (blocks.length - 1) + '\u0000';
    });
    let html = '';
    let listOpen = false;
    for (const raw of src.split('\n')) {
      const line = raw.replace(/\u0000(\d+)\u0000/g, (m, i) => blocks[+i]);
      const blockOnly = raw.match(/^\u0000(\d+)\u0000$/);
      if (blockOnly) {
        if (listOpen) { html += '</ul>'; listOpen = false; }
        html += blocks[+blockOnly[1]];
        continue;
      }
      const h = line.match(/^(#{1,4})\s+(.*)/);
      if (h) {
        if (listOpen) { html += '</ul>'; listOpen = false; }
        html += `<h${h[1].length} class="doc-h">${inlineMd(h[2])}</h${h[1].length}>`;
        continue;
      }
      const li = line.match(/^\s*[-*]\s+(.*)/);
      if (li) {
        if (!listOpen) { html += '<ul class="doc-list">'; listOpen = true; }
        html += `<li>${inlineMd(li[1])}</li>`;
        continue;
      }
      if (listOpen) { html += '</ul>'; listOpen = false; }
      if (!line.trim()) continue;
      html += `<p class="doc-p">${inlineMd(line)}</p>`;
    }
    if (listOpen) html += '</ul>';
    return html;
  }

  $('#docsBtn').onclick = () => window.open('/run-on-pc', '_blank');

  /* ---------------- старт ---------------- */
  boot();
})();
