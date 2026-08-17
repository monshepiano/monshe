/* =========================================================================
   J.A.R.V.I.S. — клиентская логика
   ========================================================================= */

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const State = {
  ws: null,
  connected: false,
  session: localStorage.getItem('jarvis_session') || 'default',
  attachments: [],
  agentMode: false,
  busy: false,
  currentBubble: null,
  currentPlanCard: null,
  recorder: null,
  recording: false,
  camStream: null,
  config: null,
};

/* ---------------------------------------------------------------- утилиты */
const time = () => new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
const timeSec = () => new Date().toLocaleTimeString('ru-RU', { hour12: false });
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function toast(title, text, isErr = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (isErr ? ' err' : '');
  el.innerHTML = `<div class="toast-title">${esc(title)}</div><div>${esc(text)}</div>`;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 400); }, 5200);
}

/* Компактный Markdown -> HTML */
function md(src) {
  let t = esc(src || '');
  const blocks = [];
  t = t.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push(`<pre><code>${code.replace(/\n$/, '')}</code></pre>`);
    return `\u0000B${blocks.length - 1}\u0000`;
  });
  t = t.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  t = t.replace(/^### (.+)$/gm, '<h3>$1</h3>')
       .replace(/^## (.+)$/gm, '<h2>$1</h2>')
       .replace(/^# (.+)$/gm, '<h1>$1</h1>');
  t = t.replace(/^\s*&gt; (.+)$/gm, '<blockquote>$1</blockquote>');
  t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
       .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  // таблицы
  t = t.replace(/^\|(.+)\|\s*\n\|[\s:|-]+\|\s*\n((?:\|.*\|\s*\n?)*)/gm, (_, head, body) => {
    const th = head.split('|').map(s => s.trim()).filter(Boolean).map(s => `<th>${s}</th>`).join('');
    const rows = body.trim().split('\n').map(r =>
      '<tr>' + r.split('|').map(s => s.trim()).filter((_, i, a) => i > 0 && i < a.length - 1)
        .map(s => `<td>${s}</td>`).join('') + '</tr>').join('');
    return `<table><thead><tr>${th}</tr></thead><tbody>${rows}</tbody></table>`;
  });
  // списки
  t = t.replace(/(?:^[-*+] .+\n?)+/gm, (m) =>
    '<ul>' + m.trim().split('\n').map(l => `<li>${l.replace(/^[-*+] /, '')}</li>`).join('') + '</ul>');
  t = t.replace(/(?:^\d+\. .+\n?)+/gm, (m) =>
    '<ol>' + m.trim().split('\n').map(l => `<li>${l.replace(/^\d+\. /, '')}</li>`).join('') + '</ol>');
  t = t.split(/\n{2,}/).map(p =>
    /^<(h\d|ul|ol|pre|table|blockquote)/.test(p.trim()) ? p : `<p>${p.replace(/\n/g, '<br>')}</p>`
  ).join('');
  t = t.replace(/\u0000B(\d+)\u0000/g, (_, i) => blocks[+i]);
  return t;
}

/* ------------------------------------------------------------ WebSocket */
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  State.ws = new WebSocket(`${proto}://${location.host}/ws`);

  State.ws.onopen = () => {
    State.connected = true;
    $('#statDot').className = 'dot';
    $('#statConn').textContent = 'В сети';
  };
  State.ws.onclose = () => {
    State.connected = false;
    $('#statDot').className = 'dot off';
    $('#statConn').textContent = 'Нет связи';
    setTimeout(connect, 2500);
  };
  State.ws.onmessage = (e) => {
    try { handleEvent(JSON.parse(e.data)); } catch (_) {}
  };
}

function send(obj) {
  if (State.ws && State.ws.readyState === 1) State.ws.send(JSON.stringify(obj));
  else toast('Нет связи', 'Джарвис недоступен. Переподключаюсь…', true);
}

/* --------------------------------------------------- обработка событий */
function handleEvent(ev) {
  switch (ev.type) {
    case 'hello':
      if (!ev.has_llm) {
        toast('Нужен ключ', 'Откройте Настройки и вставьте API-ключ Cloud.ru.', true);
        setTimeout(openSettings, 900);
      }
      break;

    case 'mode':
      setBusy(true, ev.mode === 'agent' ? 'Работаю над задачей' : 'Думаю');
      break;

    case 'status':
      term(ev.text, 'thought'); setThinkingText(ev.text);
      break;

    case 'task_started':
      term('Задача принята: ' + ev.goal, 'tool');
      break;

    case 'plan':
      renderPlan(ev.plan);
      break;

    case 'thinking':
      setThinkingText(`Шаг ${ev.step} · размышляю`);
      break;

    case 'model':
      $('#statModel').textContent = shortModel(ev.model);
      term(`Модель: ${shortModel(ev.model)} — ${ev.reason}`, '');
      break;

    case 'thought':
      term(ev.text, 'thought');
      break;

    case 'tool_start':
      term('▸ ' + ev.human, 'tool');
      setThinkingText(ev.human.slice(0, 70));
      break;

    case 'tool_result': {
      const ok = ev.result && ev.result.ok !== false;
      term(`  ${ok ? '✓' : '✗'} ${ev.tool}${ev.elapsed ? ' · ' + ev.elapsed + 'с' : ''}` +
           (ok ? '' : ' — ' + (ev.result.error || '')), ok ? 'ok' : 'err');
      if (ev.result && ev.result.results) {
        ev.result.results.slice(0, 4).forEach(r => term('    · ' + r.title, ''));
      }
      if (ev.result && ev.result.stdout) {
        String(ev.result.stdout).trim().split('\n').slice(0, 6)
          .forEach(l => term('    ' + l, ''));
      }
      refreshFiles();
      break;
    }

    case 'approval_request':
      renderApproval(ev);
      term('⏸ Жду вашего разрешения: ' + ev.human, 'warn');
      break;

    case 'approval_granted':
      term('✓ Разрешено: ' + ev.tool, 'ok');
      break;

    case 'answer_start':
      State.currentBubble = addMessage('jarvis', '');
      break;

    case 'delta':
      if (!State.currentBubble) State.currentBubble = addMessage('jarvis', '');
      State.currentBubble.dataset.raw = (State.currentBubble.dataset.raw || '') + ev.text;
      State.currentBubble.innerHTML = md(State.currentBubble.dataset.raw);
      scrollDown();
      break;

    case 'answer_end':
    case 'done':
      State.currentBubble = null;
      setBusy(false);
      break;

    case 'task_done': {
      removeThinking();
      const b = addMessage('jarvis', ev.text);
      if (ev.files && ev.files.length) {
        const box = document.createElement('div');
        box.style.marginTop = '12px';
        box.innerHTML = '<div style="font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--cyan);margin-bottom:7px">Файлы</div>' +
          ev.files.map(f => `<a class="mini-btn" href="/api/files/download/${encodeURIComponent(f.path)}" target="_blank">⤓ ${esc(f.path)}</a>`).join(' ');
        b.appendChild(box);
      }
      setBusy(false);
      refreshFiles(); refreshTasks();
      break;
    }

    case 'error':
      removeThinking();
      addMessage('jarvis', '⚠️ ' + ev.text);
      term(ev.text, 'err');
      setBusy(false);
      break;

    case 'proactive':
      toast('Джарвис предлагает', ev.text);
      term('💡 ' + ev.text, 'warn');
      break;

    case 'background_started':
      toast('Фоновая задача', ev.title);
      break;
  }
}

const shortModel = (m) => String(m || '').split('/').pop().slice(0, 22);

/* -------------------------------------------------------- вывод в чат */
function hideHero() { const h = $('#hero'); if (h) h.remove(); }

function addMessage(who, text) {
  hideHero();
  removeThinking();
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + who;
  wrap.innerHTML = `
    <div class="msg-head">
      <span class="msg-who">${who === 'user' ? 'Вы' : 'Джарвис'}</span>
      <span class="msg-time">${time()}</span>
    </div>
    <div class="bubble"></div>`;
  const bubble = wrap.querySelector('.bubble');
  bubble.dataset.raw = text || '';
  bubble.innerHTML = md(text || '');
  $('#stream').appendChild(wrap);
  scrollDown();
  return bubble;
}

function scrollDown() {
  const s = $('#stream');
  s.scrollTop = s.scrollHeight;
}

function setThinkingText(text) {
  let el = $('#thinkingRow');
  if (!el) {
    hideHero();
    el = document.createElement('div');
    el.id = 'thinkingRow';
    el.className = 'thinking-row';
    el.innerHTML = `<div class="spinner"></div><div class="thinking-text"></div>`;
    $('#stream').appendChild(el);
  }
  el.querySelector('.thinking-text').textContent = text;
  scrollDown();
}
function removeThinking() { const el = $('#thinkingRow'); if (el) el.remove(); }

function renderPlan(plan) {
  hideHero();
  if (!State.currentPlanCard) {
    State.currentPlanCard = document.createElement('div');
    State.currentPlanCard.className = 'plan-card';
    const th = $('#thinkingRow');
    if (th) $('#stream').insertBefore(State.currentPlanCard, th);
    else $('#stream').appendChild(State.currentPlanCard);
  }
  State.currentPlanCard.innerHTML =
    `<div class="plan-title"><span class="dot busy"></span>План выполнения</div>` +
    plan.map(s => `
      <div class="plan-step ${s.status || 'pending'}">
        <div class="plan-num"><span>${s.step}</span></div>
        <div class="plan-body">
          <div class="plan-step-title">${esc(s.title)}</div>
          ${s.detail ? `<div class="plan-step-detail">${esc(s.detail)}</div>` : ''}
        </div>
      </div>`).join('');
  scrollDown();
}

function renderApproval(ev) {
  hideHero();
  const el = document.createElement('div');
  el.className = 'approval';
  el.innerHTML = `
    <div class="approval-head">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/>
        <path d="M12 9v4M12 17h.01"/></svg>
      Требуется подтверждение
    </div>
    <div class="approval-action">${esc(ev.human)}</div>
    <div class="approval-btns">
      <button class="btn primary" data-d="approve">Разрешить</button>
      <button class="btn" data-d="always">Всегда разрешать «${esc(ev.tool)}»</button>
      <button class="btn danger" data-d="reject">Отклонить</button>
    </div>`;
  el.querySelectorAll('button').forEach(b => b.onclick = () => {
    if (ev.approval_id !== 'demo') {
      send({ type: 'approval', approval_id: ev.approval_id, decision: b.dataset.d });
    }
    el.querySelector('.approval-btns').innerHTML =
      `<span style="font-size:12.5px;color:${b.dataset.d === 'reject' ? 'var(--red)' : 'var(--green)'}">
        ${b.dataset.d === 'reject' ? 'Отклонено' : 'Разрешено'}</span>`;
  });
  $('#stream').appendChild(el);
  scrollDown();
}

/* --------------------------------------------------- терминал (панель) */
function term(text, cls = '') {
  if (!text) return;
  const empty = $('#termEmpty'); if (empty) empty.style.display = 'none';
  const el = document.createElement('div');
  el.className = 'term-line ' + cls;
  el.innerHTML = `<span class="term-time">${timeSec()}</span>${esc(String(text).slice(0, 400))}`;
  const box = $('#term');
  box.appendChild(el);
  while (box.children.length > 300) box.removeChild(box.firstChild);
  box.parentElement.scrollTop = box.parentElement.scrollHeight;
}

function setBusy(on, label = 'Думаю') {
  State.busy = on;
  $('#reactor')?.classList.toggle('thinking', on);
  $('#statDot').className = 'dot' + (on ? ' busy' : (State.connected ? '' : ' off'));
  if (on) setThinkingText(label);
  else { removeThinking(); State.currentPlanCard = null; }
}

/* ------------------------------------------------------------ отправка */
async function sendMessage() {
  const input = $('#input');
  const text = input.value.trim();
  if (!text && !State.attachments.length) return;

  addMessage('user', text || '(файлы)');
  send({
    type: 'chat', text, session: State.session,
    agent: State.agentMode,
    attachments: State.attachments.map(a => a.id),
  });
  input.value = '';
  input.style.height = 'auto';
  State.attachments = [];
  renderAttachments();
  $('#btnSend').disabled = true;
  setBusy(true);
}

/* --------------------------------------------------------- вложения */
function renderAttachments() {
  $('#attachRow').innerHTML = State.attachments.map((a, i) =>
    `<span class="attach-pill">${a.kind === 'image' ? '🖼' : '📄'} ${esc(a.name)}
      <button data-i="${i}">×</button></span>`).join('');
  $$('#attachRow button').forEach(b => b.onclick = () => {
    State.attachments.splice(+b.dataset.i, 1); renderAttachments();
  });
}

async function uploadFiles(files) {
  for (const f of files) {
    const fd = new FormData();
    fd.append('file', f);
    try {
      const r = await (await fetch('/api/upload', { method: 'POST', body: fd })).json();
      if (r.ok) { State.attachments.push(r); renderAttachments(); }
    } catch (_) { toast('Ошибка', 'Не удалось загрузить ' + f.name, true); }
  }
  $('#btnSend').disabled = !$('#input').value.trim() && !State.attachments.length;
}

/* ------------------------------------------------------------- голос */
async function toggleMic() {
  if (State.recording) { State.recorder?.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const chunks = [];
    const rec = new MediaRecorder(stream);
    rec.ondataavailable = e => chunks.push(e.data);
    rec.onstop = async () => {
      stream.getTracks().forEach(t => t.stop());
      State.recording = false;
      $('#btnMic').classList.remove('on');
      $('#reactor')?.classList.remove('listening');
      const blob = new Blob(chunks, { type: 'audio/webm' });
      if (blob.size < 1200) return;
      setThinkingText('Распознаю речь');
      const fd = new FormData();
      fd.append('file', blob, 'voice.webm');
      try {
        const r = await (await fetch('/api/voice', { method: 'POST', body: fd })).json();
        removeThinking();
        if (r.ok && r.text) {
          $('#input').value = r.text;
          $('#btnSend').disabled = false;
          sendMessage();
        } else toast('Голос', r.error || 'Не расслышал', true);
      } catch (e) { removeThinking(); toast('Голос', String(e), true); }
    };
    rec.start();
    State.recorder = rec;
    State.recording = true;
    $('#btnMic').classList.add('on');
    $('#reactor')?.classList.add('listening');
  } catch (e) {
    toast('Микрофон', 'Нет доступа к микрофону', true);
  }
}

/* ------------------------------------------------------------ камера */
async function camStart() {
  try {
    State.camStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: 'environment', width: { ideal: 1280 } },
    });
    $('#camVideo').srcObject = State.camStream;
  } catch (e) { toast('Камера', 'Нет доступа к камере', true); }
}
function camStop() {
  State.camStream?.getTracks().forEach(t => t.stop());
  State.camStream = null;
  $('#camVideo').srcObject = null;
}
async function camShot() {
  const v = $('#camVideo');
  if (!v.videoWidth) { toast('Камера', 'Сначала включите камеру', true); return; }
  const c = document.createElement('canvas');
  c.width = v.videoWidth; c.height = v.videoHeight;
  c.getContext('2d').drawImage(v, 0, 0);
  const blob = await new Promise(res => c.toBlob(res, 'image/jpeg', 0.88));
  const fd = new FormData();
  fd.append('file', blob, 'frame.jpg');
  fd.append('prompt', $('#camPrompt').value);
  $('#camResult').innerHTML = '<div class="thinking-row"><div class="spinner"></div><div class="thinking-text">Смотрю</div></div>';
  try {
    const r = await (await fetch('/api/vision', { method: 'POST', body: fd })).json();
    $('#camResult').innerHTML = r.ok
      ? `<div class="bubble">${md(r.text)}</div>`
      : `<div class="bubble" style="border-color:rgba(255,92,108,.4)">⚠️ ${esc(r.error)}</div>`;
  } catch (e) {
    $('#camResult').innerHTML = `<div class="bubble">⚠️ ${esc(String(e))}</div>`;
  }
}

/* ------------------------------------------------------- боковые данные */
async function refreshFiles() {
  try {
    const r = await (await fetch('/api/files')).json();
    const files = (r.files || []).filter(f => !f.path.startsWith('_run_'));
    $('#fileList').innerHTML = files.length ? files.slice().reverse().map(f => `
      <div class="card">
        <div class="card-title">📄 ${esc(f.path)}</div>
        <div class="card-meta">${(f.size / 1024).toFixed(1)} КБ</div>
        <div class="card-actions">
          <a class="mini-btn" href="/api/files/download/${encodeURIComponent(f.path)}" target="_blank">Скачать</a>
        </div>
      </div>`).join('')
      : '<div class="empty-note">Файлы, которые создаст Джарвис,<br>появятся здесь.</div>';
  } catch (_) {}
}

async function refreshTasks() {
  try {
    const r = await (await fetch('/api/tasks')).json();
    const tasks = r.tasks || [];
    $('#statTasks').textContent = tasks.filter(t => ['running', 'planning', 'waiting_approval'].includes(t.status)).length;
    $('#taskList').innerHTML = tasks.length ? tasks.map(t => `
      <div class="card">
        <div class="card-title">${esc(t.title)}
          <span class="status-pill ${t.status}">${statusRu(t.status)}</span></div>
        <div class="card-meta">${new Date(t.updated * 1000).toLocaleString('ru-RU')}</div>
        ${t.result ? `<div style="font-size:12.5px;color:var(--text-dim);margin-top:7px;line-height:1.55">${esc(t.result.slice(0, 220))}…</div>` : ''}
      </div>`).join('') : '<div class="empty-note">Задач пока нет.</div>';
  } catch (_) {}
}

const statusRu = (s) => ({
  planning: 'планирую', running: 'выполняю', waiting_approval: 'жду вас',
  done: 'готово', failed: 'ошибка', cancelled: 'отменено',
}[s] || s);

async function refreshEvents() {
  try {
    const r = await (await fetch('/api/events')).json();
    $('#eventList').innerHTML = (r.events || []).length ? r.events.map(e => `
      <div class="card">
        <div class="card-title">${esc(e.title)}</div>
        <div class="card-meta">${new Date(e.ts * 1000).toLocaleString('ru-RU')}</div>
        ${e.body ? `<div style="font-size:12.5px;color:var(--text-dim);margin-top:6px;line-height:1.55">${esc(e.body.slice(0, 300))}</div>` : ''}
      </div>`).join('') : '<div class="empty-note">Журнал событий пуст.</div>';
  } catch (_) {}
}

async function refreshMemory() {
  try {
    const r = await (await fetch('/api/memory')).json();
    $('#memList').innerHTML = (r.facts || []).length ? r.facts.map(f => `
      <div class="card">
        <div class="card-title">${esc(f.key)}</div>
        <div class="card-meta">${esc(f.value)}</div>
        <div class="card-actions"><button class="mini-btn" data-k="${esc(f.key)}">Забыть</button></div>
      </div>`).join('') : '<div class="empty-note">Пока ничего не запомнил.</div>';
    $$('#memList button').forEach(b => b.onclick = async () => {
      await fetch('/api/memory/' + encodeURIComponent(b.dataset.k), { method: 'DELETE' });
      refreshMemory();
    });
  } catch (_) {}
}

async function refreshSchedules() {
  try {
    const r = await (await fetch('/api/schedules')).json();
    $('#schedList').innerHTML = (r.schedules || []).length ? r.schedules.map(s => `
      <div class="card">
        <div class="card-title">⏱ ${esc(s.title)}</div>
        <div class="card-meta">${s.every_minutes ? 'каждые ' + s.every_minutes + ' мин' : 'разово'} ·
          след.: ${s.next_run ? new Date(s.next_run * 1000).toLocaleString('ru-RU') : '—'}</div>
        <div class="card-actions"><button class="mini-btn" data-s="${s.id}">Удалить</button></div>
      </div>`).join('') : '<div class="empty-note">Расписаний нет.</div>';
    $$('#schedList button').forEach(b => b.onclick = async () => {
      await fetch('/api/schedules/' + b.dataset.s, { method: 'DELETE' });
      refreshSchedules();
    });
  } catch (_) {}
}

/* --------------------------------------------------------- настройки */
const SAFE_TOOLS = {
  run_shell: 'Команды в терминале',
  browser_act: 'Действия в браузере',
  send_telegram: 'Отправка в Telegram',
  delete_file: 'Удаление файлов',
  http_request: 'Запросы к внешним API',
  self_edit: 'Изменение своего кода',
};

async function openSettings() {
  const r = await (await fetch('/api/config')).json();
  const c = r.config; State.config = c;
  const setV = (id, v) => { const el = $(id); if (el) el.value = v ?? ''; };

  setV('#cfgKey', ''); $('#cfgKey').placeholder =
    c.providers?.cloudru?.api_key_set ? 'ключ сохранён (••••)' : 'вставьте ключ';
  setV('#cfgFbUrl', c.providers?.fallback?.base_url);
  setV('#cfgFbKey', '');
  setV('#cfgMChat', c.models?.chat); setV('#cfgMSmart', c.models?.smart);
  setV('#cfgMAgent', c.models?.agent); setV('#cfgMCode', c.models?.code);
  setV('#cfgMVision', c.models?.vision); setV('#cfgMStt', c.models?.stt);
  setV('#cfgUserName', c.persona?.user_name); setV('#cfgName', c.persona?.name);
  setV('#cfgStyle', c.persona?.style);
  $('#cfgTgToken').value = '';
  $('#cfgTgToken').placeholder = c.telegram?.bot_token ? 'токен сохранён (••••)' : '123456:ABC-...';
  $('#cfgTgOn').checked = !!c.telegram?.enabled;
  $('#cfgTgNotify').checked = !!c.telegram?.notify_on_task_done;
  setV('#cfgMaxSteps', c.safety?.max_steps); setV('#cfgMaxSec', c.safety?.max_seconds);
  $('#cfgBgOn').checked = !!c.background?.enabled;
  $('#cfgProactive').checked = !!c.background?.proactive;
  setV('#cfgHeartbeat', c.background?.heartbeat_minutes);
  setV('#cfgFbApiKey', ''); setV('#cfgFbSecret', '');

  const active = c.safety?.confirm_tools || [];
  $('#safetyList').innerHTML = Object.entries(SAFE_TOOLS).map(([k, label]) =>
    `<label class="switch"><input type="checkbox" data-tool="${k}" ${active.includes(k) ? 'checked' : ''}> ${label}</label>`
  ).join('');

  $('#settingsOverlay').classList.add('open');
}

async function saveSettings() {
  const val = (id) => $(id)?.value.trim() ?? '';
  const patch = {
    providers: {
      cloudru: {},
      fallback: { base_url: val('#cfgFbUrl') },
    },
    models: {
      chat: val('#cfgMChat'), smart: val('#cfgMSmart'), agent: val('#cfgMAgent'),
      code: val('#cfgMCode'), vision: val('#cfgMVision'), stt: val('#cfgMStt'),
    },
    persona: {
      user_name: val('#cfgUserName'), name: val('#cfgName'), style: val('#cfgStyle'),
    },
    telegram: {
      enabled: $('#cfgTgOn').checked,
      notify_on_task_done: $('#cfgTgNotify').checked,
    },
    safety: {
      max_steps: +val('#cfgMaxSteps') || 24,
      max_seconds: +val('#cfgMaxSec') || 900,
      confirm_tools: $$('#safetyList input:checked').map(i => i.dataset.tool),
    },
    background: {
      enabled: $('#cfgBgOn').checked,
      proactive: $('#cfgProactive').checked,
      heartbeat_minutes: +val('#cfgHeartbeat') || 30,
    },
    image_gen: {},
  };
  if (val('#cfgKey')) patch.providers.cloudru.api_key = val('#cfgKey');
  if (val('#cfgFbKey')) { patch.providers.fallback.api_key = val('#cfgFbKey'); patch.providers.fallback.enabled = true; }
  if (val('#cfgTgToken')) patch.telegram.bot_token = val('#cfgTgToken');
  if (val('#cfgFbApiKey')) patch.image_gen.api_key = val('#cfgFbApiKey');
  if (val('#cfgFbSecret')) patch.image_gen.secret_key = val('#cfgFbSecret');

  await fetch('/api/config', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
  $('#settingsOverlay').classList.remove('open');
  toast('Настройки', 'Сохранено. Джарвис перенастроен.');
}

/* ------------------------------------------------------------ запуск */
function init() {
  connect();

  // Ввод
  const input = $('#input');
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 180) + 'px';
    $('#btnSend').disabled = !input.value.trim() && !State.attachments.length;
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  $('#btnSend').onclick = sendMessage;
  $('#btnMic').onclick = toggleMic;
  $('#btnAttach').onclick = () => $('#fileInput').click();
  $('#fileInput').onchange = (e) => { uploadFiles([...e.target.files]); e.target.value = ''; };

  $('#toggleAgent').onclick = () => {
    State.agentMode = !State.agentMode;
    $('#toggleAgent').classList.toggle('on', State.agentMode);
    toast('Режим', State.agentMode
      ? 'Агентский режим: буду планировать и работать шагами.'
      : 'Обычный режим: отвечаю сразу.');
  };

  // Перетаскивание файлов
  document.addEventListener('dragover', e => e.preventDefault());
  document.addEventListener('drop', e => {
    e.preventDefault();
    if (e.dataTransfer.files.length) uploadFiles([...e.dataTransfer.files]);
  });
  // Вставка картинки из буфера
  document.addEventListener('paste', e => {
    const files = [...(e.clipboardData?.files || [])];
    if (files.length) uploadFiles(files);
  });

  // Подсказки
  $$('#chips .chip').forEach(c => c.onclick = () => {
    $('#input').value = c.textContent.trim();
    $('#btnSend').disabled = false;
    sendMessage();
  });

  // Навигация
  $$('.rail-btn[data-view]').forEach(b => b.onclick = () => {
    $$('.rail-btn[data-view]').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    $$('.view').forEach(v => v.classList.remove('active'));
    $('#view-' + b.dataset.view).classList.add('active');
    if (b.dataset.view === 'tasks') { refreshTasks(); refreshSchedules(); }
    if (b.dataset.view === 'memory') refreshMemory();
  });
  $$('.side-tab').forEach(t => t.onclick = () => {
    $$('.side-tab').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    $$('.side-pane').forEach(p => p.classList.remove('active'));
    $('#pane-' + t.dataset.pane).classList.add('active');
    if (t.dataset.pane === 'files') refreshFiles();
    if (t.dataset.pane === 'events') refreshEvents();
  });
  $('#btnSide').onclick = () => $('#side').classList.toggle('open');

  // Настройки
  $('#btnSettings').onclick = openSettings;
  $('#btnCloseSettings').onclick = () => $('#settingsOverlay').classList.remove('open');
  $('#btnCancelSettings').onclick = () => $('#settingsOverlay').classList.remove('open');
  $('#btnSaveSettings').onclick = saveSettings;

  $('#btnTestKey').onclick = async () => {
    const box = $('#testResult');
    box.className = 'test-result'; box.style.display = 'block';
    box.textContent = 'Проверяю…';
    const r = await (await fetch('/api/test-key', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: $('#cfgKey').value.trim() }),
    })).json();
    box.className = 'test-result ' + (r.ok ? 'ok' : 'err');
    box.textContent = r.ok
      ? `✓ Ключ работает. Доступно моделей: ${r.count}`
      : `✗ ${r.error}`;
  };

  $('#btnLoadModels').onclick = async () => {
    const r = await (await fetch('/api/models')).json();
    $('#modelsHint').innerHTML = r.models?.length
      ? 'Доступно: ' + r.models.map(m => `<code>${esc(m)}</code>`).join(', ')
      : 'Не удалось получить список — проверьте ключ.';
  };

  // Задачи
  $('#btnRunBg').onclick = async () => {
    const goal = $('#bgGoal').value.trim();
    if (!goal) return;
    const every = +$('#bgEvery').value || 0;
    if (every > 0) {
      await fetch('/api/schedules', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: $('#bgTitle').value || goal.slice(0, 40), prompt: goal, every_minutes: every }),
      });
      toast('Расписание', `Буду выполнять каждые ${every} мин.`);
      refreshSchedules();
    } else {
      await fetch('/api/tasks/run', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ goal }),
      });
      toast('Задача', 'Запущена в фоне. Слежу за ходом в панели «Мысли».');
    }
    $('#bgGoal').value = '';
    refreshTasks();
  };

  // Память
  $('#btnMemAdd').onclick = async () => {
    const key = $('#memKey').value.trim(), value = $('#memVal').value.trim();
    if (!key) return;
    await fetch('/api/memory', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key, value }),
    });
    $('#memKey').value = ''; $('#memVal').value = '';
    refreshMemory();
  };

  // Камера
  $('#btnCamStart').onclick = camStart;
  $('#btnCamStop').onclick = camStop;
  $('#btnCamShot').onclick = camShot;

  // Восстановление истории
  loadHistory();
  refreshTasks();
  setInterval(refreshTasks, 20000);
  setInterval(() => { if ($('#pane-events').classList.contains('active')) refreshEvents(); }, 25000);
}

async function loadHistory() {
  try {
    const r = await (await fetch('/api/history?session=' + State.session)).json();
    const msgs = r.messages || [];
    if (!msgs.length) return;
    msgs.forEach(m => {
      if (m.role === 'user' || m.role === 'assistant') {
        addMessage(m.role === 'user' ? 'user' : 'jarvis', m.content);
      }
    });
  } catch (_) {}
}

/* ------------------------------------------------- демонстрация (?demo=1)
   Проигрывает записанный сценарий работы агента, чтобы посмотреть
   интерфейс без API-ключа. Ничего никуда не отправляет.                    */
const DEMO = [
  [200,  { type: 'hello', has_llm: true }],
  [400,  () => { addMessage('user', 'Собери сводку по рынку электромобилей в России за 2025 год и сохрани в файл'); }],
  [900,  { type: 'mode', mode: 'agent' }],
  [1300, { type: 'status', text: 'Составляю план…' }],
  [2100, { type: 'plan', plan: [
    { step: 1, title: 'Найти свежие данные', detail: 'продажи, доли брендов, инфраструктура', status: 'active' },
    { step: 2, title: 'Прочитать источники', detail: 'отраслевые обзоры и статистика', status: 'pending' },
    { step: 3, title: 'Свести цифры', detail: 'таблица динамики по кварталам', status: 'pending' },
    { step: 4, title: 'Собрать отчёт', detail: 'markdown-файл в песочнице', status: 'pending' },
  ]}],
  [2400, { type: 'model', model: 'openai/gpt-oss-120b', reason: 'агентский цикл' }],
  [2800, { type: 'thought', text: 'Начну с актуальной статистики продаж за 2025 год.' }],
  [3200, { type: 'tool_start', tool: 'web_search', human: 'Поиск: продажи электромобилей Россия 2025 статистика' }],
  [4600, { type: 'tool_result', tool: 'web_search', elapsed: 1.4, result: { ok: true, results: [
    { title: 'Автостат: итоги рынка электромобилей за 2025 год' },
    { title: 'Обзор: зарядная инфраструктура и господдержка' },
    { title: 'Доли брендов: Zeekr, Evolute, Москвич' },
  ]}}],
  [5000, { type: 'plan', plan: [
    { step: 1, title: 'Найти свежие данные', detail: 'продажи, доли брендов, инфраструктура', status: 'done' },
    { step: 2, title: 'Прочитать источники', detail: 'отраслевые обзоры и статистика', status: 'active' },
    { step: 3, title: 'Свести цифры', detail: 'таблица динамики по кварталам', status: 'pending' },
    { step: 4, title: 'Собрать отчёт', detail: 'markdown-файл в песочнице', status: 'pending' },
  ]}],
  [5400, { type: 'tool_start', tool: 'open_url', human: 'Читаю страницу autostat.ru' }],
  [6600, { type: 'tool_result', tool: 'open_url', elapsed: 1.1, result: { ok: true } }],
  [7000, { type: 'thought', text: 'Данные собраны. Посчитаю динамику и оформлю таблицу.' }],
  [7400, { type: 'tool_start', tool: 'run_python', human: 'Считаю квартальную динамику' }],
  [8600, { type: 'tool_result', tool: 'run_python', elapsed: 0.9, result: { ok: true,
    stdout: 'Q1 3 240\nQ2 4 115\nQ3 5 002\nQ4 5 870\nИтого: 18 227 (+34% г/г)' } }],
  [9200, { type: 'tool_start', tool: 'run_shell', human: 'Выполнить в терминале: pandoc отчёт.md -o отчёт.pdf' }],
  [9400, { type: 'approval_request', approval_id: 'demo', tool: 'run_shell',
           human: 'Выполнить в терминале: pandoc отчёт.md -o отчёт.pdf' }],
  [9800, { type: 'plan', plan: [
    { step: 1, title: 'Найти свежие данные', detail: 'продажи, доли брендов, инфраструктура', status: 'done' },
    { step: 2, title: 'Прочитать источники', detail: 'отраслевые обзоры и статистика', status: 'done' },
    { step: 3, title: 'Свести цифры', detail: 'таблица динамики по кварталам', status: 'done' },
    { step: 4, title: 'Собрать отчёт', detail: 'markdown-файл в песочнице', status: 'active' },
  ]}],
  [10600, { type: 'tool_start', tool: 'write_file', human: 'Сохраняю отчёт.md' }],
  [11400, { type: 'tool_result', tool: 'write_file', elapsed: 0.1, result: { ok: true } }],
  [12000, { type: 'task_done',
    text: `**Готово.** Сводка по рынку электромобилей в России за 2025 год.

### Ключевые цифры
| Квартал | Продажи | Динамика |
|---|---|---|
| Q1 | 3 240 | +18% |
| Q2 | 4 115 | +27% |
| Q3 | 5 002 | +31% |
| Q4 | 5 870 | +34% |

**Итого: 18 227 автомобилей**, рост 34% год к году.

- Лидеры рынка — Zeekr, Evolute и «Москвич»
- Зарядных станций стало вдвое больше, основной прирост — Москва и Санкт-Петербург
- Главный сдерживающий фактор — стоимость владения зимой

Полный отчёт с источниками сохранён в файл. Что-нибудь ещё, Сэр?`,
    files: [{ path: 'отчёт.md' }] }],
];

function runDemo() {
  $('#statModel').textContent = 'gpt-oss-120b';
  $('#side').classList.add('open');
  toast('Демонстрация', 'Показываю, как Джарвис работает. Вставьте ключ — и он заработает по-настоящему.');
  DEMO.forEach(([t, ev]) => setTimeout(() => {
    if (typeof ev === 'function') ev(); else handleEvent(ev);
  }, t));
}

document.addEventListener('DOMContentLoaded', () => {
  init();
  if (new URLSearchParams(location.search).has('demo')) setTimeout(runDemo, 500);
});
