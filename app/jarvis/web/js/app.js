/* ============================================================
   JARVIS — клиентская логика (без зависимостей)
   ============================================================ */
'use strict';

const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

const S = {
  chatId: null,
  chats: [],
  agentMode: false,
  cameraOn: false,
  computerUse: false,
  streaming: false,
  abort: null,
  attachments: [],
  config: {},
  tasks: [],
  approvals: [],
  notifications: [],
  unread: 0,
  camStream: null,
  recorder: null,
  recChunks: [],
  pendingApprovalNode: null,
};

/* ============================ утилиты ============================ */
function api(path, body) {
  const opt = body
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    : {};
  return fetch(path, opt).then((r) => r.json()).catch((e) => ({ ok: false, error: String(e) }));
}

function fmtSize(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + ' Б';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' КБ';
  return (n / 1048576).toFixed(1) + ' МБ';
}
function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const today = new Date();
  const t = d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  if (d.toDateString() === today.toDateString()) return t;
  return d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' }) + ' ' + t;
}
function fileIcon(name) {
  const n = String(name || '').toLowerCase();
  if (/\.(png|jpe?g|gif|webp|svg)$/.test(n)) return '🖼';
  if (/\.(mp4|mov|avi|mkv|webm)$/.test(n)) return '🎬';
  if (/\.(mp3|wav|ogg|m4a|opus)$/.test(n)) return '🎵';
  if (/\.(zip|tar|gz|rar|7z)$/.test(n)) return '🗜';
  if (/\.(pdf)$/.test(n)) return '📕';
  if (/\.(xlsx?|csv)$/.test(n)) return '📊';
  if (/\.(docx?|txt|md|rtf)$/.test(n)) return '📄';
  if (/\.(py|js|ts|html|css|json|sh|go|rs|java)$/.test(n)) return '⌨';
  return '📎';
}
function isImg(name) { return /\.(png|jpe?g|gif|webp)$/i.test(String(name || '')); }

function toast(text, kind, title) {
  const icons = { success: '✓', error: '✕', warn: '⚠', info: '◆' };
  const t = el('div', 'toast ' + (kind || 'info'));
  t.innerHTML = '<div class="ti">' + (icons[kind] || '◆') + '</div><div>' +
    (title ? '<div style="font-weight:600;margin-bottom:2px">' + esc(title) + '</div>' : '') +
    '<div>' + esc(text) + '</div></div>';
  $('#toasts').appendChild(t);
  setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 400); }, 5200);
}

function beep(freq, dur) {
  if (!S.config.ui || S.config.ui.sound === false) return;
  try {
    const ctx = beep.ctx || (beep.ctx = new (window.AudioContext || window.webkitAudioContext)());
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.type = 'sine'; o.frequency.value = freq || 660;
    g.gain.setValueAtTime(0.05, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + (dur || 0.16));
    o.connect(g); g.connect(ctx.destination); o.start(); o.stop(ctx.currentTime + (dur || 0.16));
  } catch (e) { /* тишина */ }
}

function modal(html, onMount) {
  const m = $('#modal');
  m.innerHTML = html;
  $('#modalBack').classList.add('open');
  if (onMount) onMount(m);
}
function closeModal() { $('#modalBack').classList.remove('open'); }
$('#modalBack').addEventListener('click', (e) => { if (e.target.id === 'modalBack') closeModal(); });

function lightbox(src) {
  const lb = el('div', 'lightbox', '<img src="' + esc(src) + '">');
  lb.addEventListener('click', () => lb.remove());
  document.body.appendChild(lb);
}

/* ============================ частицы ============================ */
(function particles() {
  const cv = $('#particles'); if (!cv) return;
  const ctx = cv.getContext('2d');
  let w, h, dots = [];
  function resize() {
    w = cv.width = window.innerWidth; h = cv.height = window.innerHeight;
    dots = Array.from({ length: Math.min(70, Math.round(w / 22)) }, () => ({
      x: Math.random() * w, y: Math.random() * h,
      vx: (Math.random() - 0.5) * 0.22, vy: (Math.random() - 0.5) * 0.22,
      r: Math.random() * 1.5 + 0.4, a: Math.random() * 0.4 + 0.12,
    }));
  }
  function loop() {
    ctx.clearRect(0, 0, w, h);
    for (const d of dots) {
      d.x += d.vx; d.y += d.vy;
      if (d.x < 0 || d.x > w) d.vx *= -1;
      if (d.y < 0 || d.y > h) d.vy *= -1;
      ctx.beginPath(); ctx.arc(d.x, d.y, d.r, 0, 6.283);
      ctx.fillStyle = 'rgba(0,212,255,' + d.a + ')'; ctx.fill();
    }
    requestAnimationFrame(loop);
  }
  resize(); window.addEventListener('resize', resize); loop();
})();

/* ============================ загрузка ============================ */
const BOOT_LINES = [
  'инициализация ядра……… <b>ok</b>',
  'подключение к моделям…… <b>ok</b>',
  'проверка инструментов…… <b>30 модулей</b>',
  'контур AUTO…………… <b>активен</b>',
  'протоколы безопасности… <b>ok</b>',
];
(function boot() {
  const log = $('#bootLog');
  let i = 0;
  const tick = () => {
    if (i < BOOT_LINES.length) {
      const line = el('div', '', BOOT_LINES[i]);
      log.appendChild(line); i++; beep(520 + i * 60, 0.05);
      setTimeout(tick, 210);
    } else {
      setTimeout(() => {
        $('#boot').classList.add('hide');
        $('#app').classList.add('ready');
        beep(880, 0.22);
      }, 320);
    }
  };
  setTimeout(tick, 380);
})();

/* ============================ навигация ============================ */
function showView(name) {
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === 'view-' + name));
  $$('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === name));
  const titles = { chat: 'Диалог', auto: 'AUTO · фоновые задачи', files: 'Песочница', memory: 'Память', settings: 'Настройки' };
  $('#topTitle').textContent = titles[name] || '';
  $('#app').classList.remove('nav-open');
  if (name === 'auto') loadTasks();
  if (name === 'files') loadFiles();
  if (name === 'memory') loadMemory();
  if (name === 'settings') renderSettings();
}
$$('.nav-item').forEach((b) => b.addEventListener('click', () => showView(b.dataset.view)));
$('#menuToggle').addEventListener('click', () => $('#app').classList.toggle('nav-open'));
$('#collapseBtn').addEventListener('click', () => $('#app').classList.toggle('collapsed'));

/* ============================ правая панель ============================ */
function openDrawer(tab) {
  $('#drawer').classList.add('open');
  if (tab) {
    $$('.dtab').forEach((t) => t.classList.toggle('active', t.dataset.dtab === tab));
    $$('.dpane').forEach((p) => p.classList.toggle('active', p.id === 'dpane-' + tab));
  }
}
$$('.dtab').forEach((t) => t.addEventListener('click', () => openDrawer(t.dataset.dtab)));
$('#drawerClose').addEventListener('click', () => $('#drawer').classList.remove('open'));
$('#approvalsBtn').addEventListener('click', () => openDrawer('sanctions'));
$('#notifyBtn').addEventListener('click', () => {
  openDrawer('notes'); api('/api/notifications/read', {}).then(refreshState);
});
$('#sandboxBtn').addEventListener('click', () => openDrawer('sandbox'));

/* ============================ переключатели ============================ */
$('#tgAgent').addEventListener('click', function () {
  S.agentMode = !S.agentMode; this.classList.toggle('on', S.agentMode);
  beep(S.agentMode ? 760 : 420, 0.1);
  $('#input').placeholder = S.agentMode
    ? 'Поставь задачу — разобью на шаги и сделаю сам…'
    : 'Сообщение для JARVIS…';
  if (S.agentMode) toast('Агентский режим включён: планирую и выполняю сам.', 'info', 'AGENT');
});
$('#tgCamera').addEventListener('click', function () {
  S.cameraOn = !S.cameraOn; this.classList.toggle('on', S.cameraOn);
  if (S.cameraOn) { openDrawer('camera'); startCam(); } else { stopCam(); }
});
$('#tgComputer').addEventListener('click', function () {
  S.computerUse = !S.computerUse; this.classList.toggle('on', S.computerUse);
  if (S.computerUse) {
    toast('Управление мышью и клавиатурой разрешено. Каждое действие спрошу отдельно.', 'warn', 'COMPUTER-USE');
    beep(300, 0.2);
  }
});

/* ============================ состояние ============================ */
async function refreshState() {
  const st = await api('/api/state');
  if (!st.ok) { setChip('#chipConn', 'err', 'нет связи'); return; }
  setChip('#chipConn', 'ok', 'связь');
  S.config = st.config || {};
  S.tasks = st.tasks || [];
  S.approvals = st.approvals || [];
  S.notifications = st.notifications || [];
  S.unread = st.unread || 0;

  const active = st.active_tasks || 0;
  const badge = $('#autoBadge');
  badge.textContent = active;
  badge.classList.toggle('hot', active > 0);
  setChip('#chipAuto', active > 0 ? 'warn live' : 'ok', active > 0 ? 'AUTO · ' + active : 'AUTO');

  setChip('#chipModel', st.providers_ready ? 'ok' : 'err',
    st.providers_ready ? 'модели готовы' : 'нет ключа');

  const ab = $('#approvalsBadge');
  ab.textContent = S.approvals.length;
  ab.classList.toggle('hidden', S.approvals.length === 0);
  $('#approvalsBtn').classList.toggle('alert', S.approvals.length > 0);

  const nb = $('#notifyBadge');
  nb.textContent = S.unread;
  nb.classList.toggle('hidden', S.unread === 0);

  const cost = ((st.usage || {}).total || {}).cost || 0;
  $('#footCost').textContent = cost.toFixed(2) + ' ₽';

  renderSanctions(); renderNotes();
  if ($('#view-auto').classList.contains('active')) renderTasks();
}
function setChip(sel, cls, text) {
  const chip = $(sel);
  chip.querySelector('.dot').className = 'dot ' + (cls || '');
  chip.querySelector('span').textContent = text;
}

/* ============================ чаты ============================ */
async function loadChats() {
  const r = await api('/api/chats');
  S.chats = r.chats || [];
  const list = $('#chatList'); list.innerHTML = '';
  S.chats.forEach((c, i) => {
    const item = el('div', 'chat-item' + (c.id === S.chatId ? ' active' : ''));
    item.style.animationDelay = (i * 0.02) + 's';
    item.innerHTML = '<span>' + esc(c.title || 'Диалог') + '</span><i class="chat-x">✕</i>';
    item.addEventListener('click', (e) => {
      if (e.target.classList.contains('chat-x')) {
        api('/api/chats/delete', { chat_id: c.id }).then(() => {
          if (S.chatId === c.id) newChat(); else loadChats();
        });
        e.stopPropagation(); return;
      }
      openChat(c.id);
    });
    list.appendChild(item);
  });
}
function newChat() {
  S.chatId = null;
  $('#stream').innerHTML = '';
  $('#stream').appendChild(buildWelcome());
  loadChats();
  showView('chat');
  $('#input').focus();
}
$('#newChatBtn').addEventListener('click', newChat);

async function openChat(id) {
  S.chatId = id;
  showView('chat');
  const r = await api('/api/messages?chat_id=' + encodeURIComponent(id));
  const stream = $('#stream'); stream.innerHTML = '';
  (r.messages || []).forEach((m) => {
    if (m.role === 'user') addUserMsg(m.content, (m.meta || {}).attachments || []);
    else if (m.role === 'assistant') {
      const node = addAiMsg();
      node.body.innerHTML = '<div class="md">' + MD.render(m.content) + '</div>';
      const meta = m.meta || {};
      if (meta.model) node.modelEl.textContent = meta.model;
      (meta.files || []).forEach((f) => attachFileChip(node.body, f));
      addMsgActions(node, m.content);
    }
  });
  stream.scrollTop = stream.scrollHeight;
  loadChats();
}

const SUGGESTIONS = [
  ['Что нового?', 'Найди в интернете 5 главных новостей за сегодня и сделай сводку'],
  ['Собери отчёт', 'Собери таблицу с ценами на iPhone 17 в российских магазинах и сохрани в Excel'],
  ['Каждое утро', 'Каждый день в 9:00 присылай мне погоду и курс доллара в Telegram'],
  ['Сделай картинку', 'Нарисуй логотип для кофейни в стиле неон-минимализм'],
];
function buildWelcome() {
  const w = el('div', 'welcome');
  w.innerHTML = '<div class="reactor xl"><div class="ring r1"></div><div class="ring r2"></div>' +
    '<div class="ring r3"></div><div class="core"></div></div>' +
    '<h1 class="hello">Добрый день. Я <span>JARVIS</span>.</h1>' +
    '<p class="hello-sub">Спрашивай что угодно — или включи <b>Агент</b>, и я сделаю всё сам.</p>' +
    '<div class="suggestions"></div>';
  const box = w.querySelector('.suggestions');
  SUGGESTIONS.forEach((s, i) => {
    const b = el('button', 'sugg', '<b>' + esc(s[0]) + '</b>' + esc(s[1]));
    b.style.animationDelay = (0.05 * i) + 's';
    b.addEventListener('click', () => { $('#input').value = s[1]; autoGrow(); send(); });
    box.appendChild(b);
  });
  return w;
}

/* ============================ сообщения ============================ */
function stream() { return $('#stream'); }
function scrollDown(force) {
  const s = stream();
  const near = s.scrollHeight - s.scrollTop - s.clientHeight < 220;
  if (near || force) s.scrollTop = s.scrollHeight;
}
function killWelcome() { const w = $('.welcome'); if (w) w.remove(); }

function addUserMsg(text, atts) {
  killWelcome();
  const m = el('div', 'msg msg-user');
  let extra = '';
  (atts || []).forEach((a) => {
    if (!a) return;
    extra += '<div style="margin-top:6px;font-size:11.5px;opacity:.75">' +
      fileIcon(a.name) + ' ' + esc(a.name) + '</div>';
  });
  m.innerHTML = '<div class="bubble-user">' + esc(text) + extra + '</div>';
  stream().appendChild(m);
  scrollDown(true);
  return m;
}

function addAiMsg() {
  killWelcome();
  const m = el('div', 'msg msg-ai');
  m.innerHTML =
    '<div class="ai-avatar"><div class="reactor sm" style="width:34px;height:34px">' +
    '<div class="ring r1"></div><div class="ring r2"></div><div class="core"></div></div></div>' +
    '<div class="ai-body"><div class="ai-name">JARVIS<span class="ai-model"></span></div>' +
    '<div class="ai-content"></div></div>';
  stream().appendChild(m);
  scrollDown(true);
  return {
    root: m,
    body: m.querySelector('.ai-content'),
    modelEl: m.querySelector('.ai-model'),
  };
}

function addMsgActions(node, text) {
  const acts = el('div', 'msg-actions');
  const copy = el('button', 'act', 'Копировать');
  copy.addEventListener('click', () => {
    navigator.clipboard.writeText(text).then(() => toast('Скопировано', 'success'));
  });
  const speak = el('button', 'act', 'Озвучить');
  speak.addEventListener('click', () => {
    try {
      const u = new SpeechSynthesisUtterance(text.replace(/[#*`>|\-]/g, '').slice(0, 900));
      u.lang = 'ru-RU'; u.rate = 1.03;
      speechSynthesis.cancel(); speechSynthesis.speak(u);
    } catch (e) { toast('Синтез речи недоступен', 'error'); }
  });
  const again = el('button', 'act', 'Ещё раз');
  again.addEventListener('click', () => { $('#input').value = S.lastPrompt || ''; autoGrow(); send(); });
  acts.appendChild(copy); acts.appendChild(speak); acts.appendChild(again);
  node.body.appendChild(acts);
}

/* --- составные карточки внутри ответа --- */
function makeCard(icon, title, cls, openByDefault) {
  const card = el('div', 'panel-card ' + (cls || ''));
  card.innerHTML =
    '<div class="card-head' + (openByDefault ? ' open' : '') + '">' +
    '<span class="k">' + icon + '</span><span class="t">' + esc(title) + '</span>' +
    '<span class="chev">›</span></div>' +
    '<div class="card-body' + (openByDefault ? ' open' : '') + '"><div class="card-inner"></div></div>';
  const head = card.querySelector('.card-head');
  const body = card.querySelector('.card-body');
  head.addEventListener('click', () => {
    head.classList.toggle('open'); body.classList.toggle('open');
  });
  card.inner = card.querySelector('.card-inner');
  card.setTitle = (t) => { card.querySelector('.t').innerHTML = t; };
  card.body = body;
  return card;
}

function attachFileChip(container, f) {
  if (isImg(f.name)) {
    const img = el('img', 'img-out');
    img.src = f.url; img.alt = f.name; img.loading = 'lazy';
    img.addEventListener('click', () => lightbox(f.url));
    container.appendChild(img);
  }
  const a = el('a', 'file-chip');
  a.href = f.url; a.target = '_blank'; a.download = '';
  a.innerHTML = '<span class="fi">' + fileIcon(f.name) + '</span><span>' + esc(f.name) +
    '</span><small>' + fmtSize(f.size) + '</small>';
  container.appendChild(a);
}

function termLine(text, cls) {
  const feed = $('#termFeed');
  const line = el('div', 'term-line ' + (cls || ''), esc(text));
  feed.appendChild(line);
  feed.scrollTop = feed.scrollHeight;
  while (feed.children.length > 400) feed.removeChild(feed.firstChild);
}

/* ============================ отправка ============================ */
function autoGrow() {
  const t = $('#input');
  t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight, 190) + 'px';
}
$('#input').addEventListener('input', autoGrow);
$('#input').addEventListener('focus', () => $('#composer').classList.add('focus'));
$('#input').addEventListener('blur', () => $('#composer').classList.remove('focus'));
$('#input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
$('#sendBtn').addEventListener('click', () => {
  if (S.streaming) { if (S.abort) S.abort.abort(); return; }
  send();
});

async function send() {
  const input = $('#input');
  const text = input.value.trim();
  if ((!text && !S.attachments.length) || S.streaming) return;
  S.lastPrompt = text;

  addUserMsg(text, S.attachments);
  input.value = ''; autoGrow();
  const atts = S.attachments.slice();
  S.attachments = []; renderAttachments();

  const node = addAiMsg();
  setStreaming(true);
  beep(700, 0.07);

  // блоки, которые появляются по ходу
  const ui = {
    node,
    statusEl: null,
    thinkCard: null,
    planCard: null,
    planItems: [],
    mdEl: null,
    buffer: '',
    tools: {},
    files: [],
  };
  ui.statusEl = el('div', 'thinking-line');
  ui.statusEl.innerHTML = '<div class="spinner"></div><span>Соединяюсь…</span>';
  node.body.appendChild(ui.statusEl);

  S.abort = new AbortController();
  try {
    const res = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: S.abort.signal,
      body: JSON.stringify({
        chat_id: S.chatId, text,
        agent_mode: S.agentMode,
        computer_use: S.computerUse,
        attachments: atts,
      }),
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop();
      for (const part of parts) {
        const line = part.split('\n').find((l) => l.startsWith('data:'));
        if (!line) continue;
        let ev; try { ev = JSON.parse(line.slice(5).trim()); } catch (e) { continue; }
        handleEvent(ev, ui);
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') {
      showError(ui, String(e.message || e));
    } else {
      if (ui.statusEl) ui.statusEl.remove();
      node.body.appendChild(el('div', 'muted', 'Остановлено.'));
    }
  }
  setStreaming(false);
  refreshState();
  loadChats();
}

function setStreaming(on) {
  S.streaming = on;
  const btn = $('#sendBtn');
  btn.classList.toggle('stop', on);
  btn.innerHTML = on
    ? '<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>'
    : '<svg viewBox="0 0 24 24"><path d="M3 20l18-8L3 4v6l12 2-12 2z"/></svg>';
  $('#composer').classList.toggle('busy', on);
}

function showError(ui, msg) {
  if (ui.statusEl) ui.statusEl.remove();
  const c = el('div', 'panel-card', '<div class="card-inner" style="padding:12px 13px;color:#ffb3c1">⚠ ' + esc(msg) + '</div>');
  ui.node.body.appendChild(c);
  toast(msg, 'error', 'Ошибка');
}

const TIER_LABEL = { nano: 'экономный', base: 'базовый', smart: 'усиленный', coder: 'кодовый', vision: 'зрение' };

function handleEvent(ev, ui) {
  const node = ui.node;
  switch (ev.type) {
    case 'chat':
      S.chatId = ev.chat_id; break;

    case 'chat_title':
      loadChats(); break;

    case 'route': {
      const hint = $('#routeHint');
      hint.textContent = 'маршрут: ' + (TIER_LABEL[ev.tier] || ev.tier) + ' · ' + (ev.reason || '');
      hint.classList.add('show');
      break;
    }

    case 'model':
      node.modelEl.textContent = ' · ' + (ev.model || '');
      $('#footModel').textContent = ev.model || '—';
      break;

    case 'status':
      if (ui.statusEl) {
        ui.statusEl.innerHTML = '<div class="spinner"></div><span>' + esc(ev.text) +
          '</span><span class="dots"><span></span><span></span><span></span></span>';
      }
      break;

    case 'thinking': {
      if (!ui.thinkCard) {
        ui.thinkCard = makeCard('◇', 'Ход мыслей', 'think-card', false);
        ui.thinkCard.inner.appendChild(el('div', 'think-stream'));
        node.body.insertBefore(ui.thinkCard, ui.statusEl);
      }
      const ts = ui.thinkCard.querySelector('.think-stream');
      ts.textContent += ev.text;
      ts.scrollTop = ts.scrollHeight;
      break;
    }

    case 'plan': {
      ui.planCard = makeCard('☰', 'План · ' + ev.steps.length + ' шаг(ов)', 'plan-card', true);
      const list = el('ul', 'plan-list');
      ev.steps.forEach((s, i) => {
        const li = el('li', '', '<span class="plan-num">' + (i + 1) + '</span><span>' + esc(s) + '</span>');
        li.style.animationDelay = (i * 0.06) + 's';
        list.appendChild(li); ui.planItems.push(li);
      });
      ui.planCard.inner.appendChild(list);
      node.body.insertBefore(ui.planCard, ui.statusEl);
      beep(620, 0.09);
      scrollDown();
      break;
    }

    case 'tool_hint':
      if (ui.statusEl) {
        ui.statusEl.innerHTML = '<div class="spinner"></div><span>Готовлю инструмент: ' +
          esc(ev.name) + '</span>';
      }
      break;

    case 'tool_start': {
      const card = makeCard('⚙', ev.label || ev.name, 'tool-card', false);
      card.querySelector('.card-head').insertBefore(el('span', 'tool-run'), card.querySelector('.chev'));
      const kv = el('div', 'kv');
      Object.keys(ev.args || {}).forEach((k) => {
        const v = String(ev.args[k]);
        kv.innerHTML += '<i>' + esc(k) + '</i><span>' + esc(v.length > 300 ? v.slice(0, 300) + '…' : v) + '</span>';
      });
      card.inner.appendChild(kv);
      node.body.insertBefore(card, ui.statusEl);
      ui.tools[ev.id || ev.name] = card;
      termLine('$ ' + ev.name + ' ' + JSON.stringify(ev.args || {}).slice(0, 300), 'cmd');
      // отметить шаг плана
      const doneCount = Object.keys(ui.tools).length;
      if (ui.planItems[doneCount - 2]) ui.planItems[doneCount - 2].classList.add('done');
      beep(520, 0.05);
      scrollDown();
      break;
    }

    case 'approval_wait': {
      if (ui.statusEl) ui.statusEl.innerHTML = '<div class="spinner"></div><span>Жду твоего решения…</span>';
      const critical = /delete|shell|payment|pay|computer|click|type_text/.test(ev.tool || '');
      const card = el('div', 'panel-card approve-card' + (critical ? ' critical' : ''));
      card.innerHTML =
        '<div class="ah">⛨ Требуется подтверждение</div>' +
        '<div class="ab"><b>' + esc(ev.label || ev.tool) + '</b><br>' + esc(ev.reason || '') +
        '<div class="s-args" style="margin-top:8px">' + esc(JSON.stringify(ev.args || {}, null, 1)) + '</div></div>' +
        '<div class="approve-actions"><button class="btn primary sm ok">Разрешить</button>' +
        '<button class="btn danger sm no">Отклонить</button>' +
        '<span class="muted" style="align-self:center;font-size:11px">или реши в панели «Санкции»</span></div>';
      node.body.insertBefore(card, ui.statusEl);
      const decide = (d) => {
        const pend = S.approvals[0];
        if (pend) api('/api/approvals/decide', { id: pend.id, decision: d });
        card.querySelector('.approve-actions').innerHTML =
          '<span class="muted">' + (d === 'approved' ? '✓ разрешено' : '✕ отклонено') + '</span>';
      };
      card.querySelector('.ok').addEventListener('click', () => decide('approved'));
      card.querySelector('.no').addEventListener('click', () => decide('rejected'));
      refreshState(); openDrawer('sanctions');
      beep(340, 0.3);
      toast((ev.label || ev.tool) + ' — нужно твоё разрешение', 'warn', 'Санкция');
      scrollDown(true);
      break;
    }

    case 'approval_done':
      refreshState(); break;

    case 'tool_result': {
      const card = ui.tools[ev.id || ev.name];
      const ok = ev.result && ev.result.ok !== false;
      if (card) {
        const run = card.querySelector('.tool-run');
        if (run) { run.style.animation = 'none'; run.style.background = ok ? 'var(--green)' : 'var(--red)'; }
        card.querySelector('.k').className = 'k ' + (ok ? 'tool-ok' : 'tool-err');
        card.querySelector('.k').textContent = ok ? '✓' : '✕';
        if (ev.elapsed != null) {
          card.querySelector('.t').innerHTML += ' <span class="muted" style="font-size:10.5px">· ' + ev.elapsed + 'с</span>';
        }
        const r = ev.result || {};
        const pre = el('pre', 'out');
        let txt = '';
        if (r.error) txt = '⚠ ' + r.error;
        else if (r.stdout != null || r.stderr != null) txt = (r.stdout || '') + (r.stderr ? '\n' + r.stderr : '');
        else if (r.content) txt = String(r.content).slice(0, 4000);
        else if (r.results) txt = (r.results || []).map((x) => '• ' + (x.title || '') + '\n  ' + (x.url || '') + '\n  ' + (x.snippet || '')).join('\n');
        else txt = JSON.stringify(r, null, 1).slice(0, 4000);
        pre.textContent = txt || '(пусто)';
        card.inner.appendChild(pre);
      }
      termLine((ok ? '✓ ' : '✕ ') + ev.name + (ev.result && ev.result.error ? ' — ' + ev.result.error : ' — ok'),
        ok ? '' : 'err');
      break;
    }

    case 'file': {
      ui.files.push(ev);
      const wrap = ui.filesBox || (ui.filesBox = el('div', ''));
      if (!wrap.parentNode) node.body.insertBefore(wrap, ui.statusEl);
      attachFileChip(wrap, ev);
      toast(ev.name + ' готов', 'success', 'Файл');
      beep(820, 0.1);
      break;
    }

    case 'background':
      toast('Задача «' + ev.title + '» ушла в фон' + (ev.schedule ? ' (' + ev.schedule + ')' : ''),
        'info', 'AUTO');
      refreshState();
      break;

    case 'delta': {
      if (!ui.mdEl) {
        if (ui.statusEl) { ui.statusEl.remove(); ui.statusEl = null; }
        ui.mdEl = el('div', 'md');
        node.body.appendChild(ui.mdEl);
      }
      ui.buffer += ev.text;
      ui.mdEl.innerHTML = MD.render(ui.buffer) + '<span class="cursor-blink"></span>';
      scrollDown();
      break;
    }

    case 'done': {
      if (ui.statusEl) { ui.statusEl.remove(); ui.statusEl = null; }
      const content = ev.content || ui.buffer;
      if (!ui.mdEl) { ui.mdEl = el('div', 'md'); node.body.appendChild(ui.mdEl); }
      ui.mdEl.innerHTML = MD.render(content);
      ui.planItems.forEach((li) => li.classList.add('done'));
      $$('.img-out', ui.mdEl).forEach((im) => im.addEventListener('click', () => lightbox(im.src)));
      addMsgActions(node, content);
      beep(760, 0.13);
      $('#routeHint').classList.remove('show');
      scrollDown();
      break;
    }

    case 'error':
      showError(ui, ev.error || 'неизвестная ошибка');
      break;

    case 'end':
      if (ui.statusEl) { ui.statusEl.remove(); ui.statusEl = null; }
      break;
  }
}

/* ============================ вложения ============================ */
$('#attachBtn').addEventListener('click', () => $('#fileInput').click());
$('#fileInput').addEventListener('change', (e) => {
  Array.from(e.target.files || []).forEach(uploadFile);
  e.target.value = '';
});
document.addEventListener('dragover', (e) => e.preventDefault());
document.addEventListener('drop', (e) => {
  e.preventDefault();
  Array.from(e.dataTransfer.files || []).forEach(uploadFile);
});
$('#input').addEventListener('paste', (e) => {
  Array.from(e.clipboardData.items || []).forEach((it) => {
    if (it.kind === 'file') { const f = it.getAsFile(); if (f) uploadFile(f); }
  });
});

function uploadFile(file) {
  if (file.size > 25 * 1024 * 1024) { toast('Файл больше 25 МБ', 'error'); return; }
  const fr = new FileReader();
  fr.onload = async () => {
    const r = await api('/api/upload', { name: file.name, data: fr.result });
    if (!r.ok) { toast(r.error || 'не загрузилось', 'error'); return; }
    if (r.kind === 'image') r.data = fr.result;
    S.attachments.push(r);
    renderAttachments();
    toast(file.name + ' прикреплён', 'success');
  };
  fr.readAsDataURL(file);
}

function renderAttachments() {
  const box = $('#attachments'); box.innerHTML = '';
  S.attachments.forEach((a, i) => {
    const n = el('div', 'att');
    n.innerHTML = (a.kind === 'image' && a.data ? '<img src="' + a.data + '">' : '<span>' + fileIcon(a.name) + '</span>') +
      '<b>' + esc(a.name) + '</b><small style="color:var(--tx3)">' + fmtSize(a.size) + '</small><i class="x">✕</i>';
    n.querySelector('.x').addEventListener('click', () => { S.attachments.splice(i, 1); renderAttachments(); });
    box.appendChild(n);
  });
}

/* ============================ голос ============================ */
$('#micBtn').addEventListener('click', async function () {
  if (S.recorder && S.recorder.state === 'recording') { S.recorder.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    S.recChunks = [];
    const rec = new MediaRecorder(stream);
    S.recorder = rec;
    rec.ondataavailable = (e) => S.recChunks.push(e.data);
    rec.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      this.classList.remove('rec');
      const blob = new Blob(S.recChunks, { type: 'audio/webm' });
      const fr = new FileReader();
      fr.onload = async () => {
        toast('Распознаю речь…', 'info');
        const r = await api('/api/transcribe', { audio: fr.result, language: 'ru' });
        if (r.ok && r.text) {
          $('#input').value = ($('#input').value + ' ' + r.text).trim();
          autoGrow(); $('#input').focus();
        } else {
          toast(r.error || 'не удалось распознать', 'error');
        }
      };
      fr.readAsDataURL(blob);
    };
    rec.start();
    this.classList.add('rec');
    beep(560, 0.1);
    toast('Говори… нажми ещё раз, чтобы остановить', 'info', 'Запись');
  } catch (e) {
    toast('Нет доступа к микрофону', 'error');
  }
});

/* ============================ камера ============================ */
async function startCam() {
  try {
    S.camStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
    $('#cam').srcObject = S.camStream;
    toast('Камера включена', 'success');
  } catch (e) { toast('Нет доступа к камере', 'error'); }
}
function stopCam() {
  if (S.camStream) { S.camStream.getTracks().forEach((t) => t.stop()); S.camStream = null; }
  $('#cam').srcObject = null;
}
function camFrame() {
  const v = $('#cam');
  if (!v.videoWidth) return null;
  const c = document.createElement('canvas');
  c.width = v.videoWidth; c.height = v.videoHeight;
  c.getContext('2d').drawImage(v, 0, 0);
  return c.toDataURL('image/jpeg', 0.85);
}
$('#camStart').addEventListener('click', startCam);
$('#camStop').addEventListener('click', stopCam);
$('#camShot').addEventListener('click', async () => {
  const data = camFrame();
  if (!data) { toast('Сначала включи камеру', 'warn'); return; }
  $('#camResult').innerHTML = '<div class="thinking-line"><div class="spinner"></div><span>Смотрю…</span></div>';
  const r = await api('/api/vision', { image: data, question: 'Что на изображении? Опиши кратко и по делу, по-русски.' });
  $('#camResult').innerHTML = r.ok ? '<div class="md">' + MD.render(r.content || r.text || '') + '</div>'
    : '<span style="color:#ffb3c1">' + esc(r.error) + '</span>';
});
$('#camBuy').addEventListener('click', async () => {
  const data = camFrame();
  if (!data) { toast('Сначала включи камеру', 'warn'); return; }
  const r = await api('/api/upload', { name: 'camera_' + Date.now() + '.jpg', data });
  if (r.ok) {
    r.data = data;
    S.attachments.push(r); renderAttachments();
    $('#input').value = 'Определи, что на фото, и найди, где это купить в России — с ценами и ссылками.';
    autoGrow(); showView('chat'); $('#drawer').classList.remove('open');
    send();
  }
});

/* ============================ санкции / уведомления ============================ */
function renderSanctions() {
  const pane = $('#dpane-sanctions');
  if (!S.approvals.length) {
    pane.innerHTML = '<div class="empty"><span class="e-ico">⛨</span>Нет запросов на подтверждение.<br>' +
      'Опасные действия — оплата, удаление, управление компьютером — я всегда спрашиваю здесь.</div>';
    return;
  }
  pane.innerHTML = '';
  S.approvals.forEach((a) => {
    let args = a.args;
    try { args = JSON.stringify(JSON.parse(a.args), null, 1); } catch (e) { /* как есть */ }
    const critical = a.risk === 'danger' || /delete|shell|pay/.test(a.tool || '');
    const card = el('div', 'sanction' + (critical ? ' critical' : ''));
    card.innerHTML =
      '<div class="s-top">⛨ ' + esc(a.tool) + '</div>' +
      '<div class="s-why">' + esc(a.reason || 'Требуется твоё разрешение.') + '</div>' +
      '<div class="s-args">' + esc(args) + '</div>' +
      '<div class="s-acts"><button class="btn primary sm">Разрешить</button>' +
      '<button class="btn danger sm">Отклонить</button></div>';
    const [okBtn, noBtn] = $$('.s-acts .btn', card);
    okBtn.addEventListener('click', () => decideApproval(a.id, 'approved', card));
    noBtn.addEventListener('click', () => decideApproval(a.id, 'rejected', card));
    pane.appendChild(card);
  });
}
async function decideApproval(id, decision, card) {
  await api('/api/approvals/decide', { id, decision });
  if (card) card.remove();
  toast(decision === 'approved' ? 'Разрешено — продолжаю' : 'Отклонено', decision === 'approved' ? 'success' : 'warn');
  refreshState();
}

function renderNotes() {
  const pane = $('#dpane-notes');
  if (!S.notifications.length) {
    pane.innerHTML = '<div class="empty"><span class="e-ico">◔</span>Пока тихо.<br>' +
      'Здесь появятся отчёты фоновых задач и мои проактивные подсказки.</div>';
    return;
  }
  pane.innerHTML = '';
  S.notifications.forEach((n) => {
    const kind = n.level === 'error' ? 'error' : (n.level === 'success' ? 'success' : '');
    const item = el('div', 'note-item ' + kind);
    item.innerHTML = '<div class="note-ico">' + (kind === 'error' ? '✕' : kind === 'success' ? '✓' : '◆') + '</div>' +
      '<div style="flex:1;min-width:0"><div class="note-t">' + esc(n.title) + '</div>' +
      '<div class="note-b">' + esc((n.body || '').slice(0, 400)) + '</div>' +
      '<div class="note-time">' + fmtTime(n.created_at) + '</div></div>';
    pane.appendChild(item);
  });
}

/* ============================ AUTO ============================ */
async function loadTasks() {
  const r = await api('/api/tasks');
  S.tasks = r.tasks || [];
  renderTasks();
}
function renderTasks() {
  const grid = $('#taskGrid');
  if (!S.tasks.length) {
    grid.innerHTML = '<div class="empty" style="grid-column:1/-1"><span class="e-ico">◎</span>' +
      'Фоновых задач нет.<br>Напиши в чат «каждый день в 9:00 присылай сводку новостей» — ' +
      'я сам заведу задачу и буду присылать результат.</div>';
    return;
  }
  grid.innerHTML = '';
  S.tasks.forEach((t, i) => {
    const st = t.status || 'queued';
    const card = el('div', 'task-card ' + st);
    card.style.animationDelay = (i * 0.03) + 's';
    const stateRu = { queued: 'в очереди', running: 'выполняется', done: 'готово', error: 'ошибка', scheduled: 'по расписанию', cancelled: 'отменена' }[st] || st;
    card.innerHTML =
      '<div class="tc-head"><div class="tc-title">' + esc(t.title) + '</div>' +
      '<div class="tc-state ' + st + '">' + stateRu + '</div></div>' +
      '<div class="tc-prompt">' + esc(t.prompt) + '</div>' +
      '<div class="tc-bar"><i style="width:' + Math.round((t.progress || 0) * 100) + '%"></i></div>' +
      (t.schedule ? '<div class="muted" style="font-size:11px;margin-bottom:6px">⟳ ' + esc(t.schedule) +
        (t.next_run ? ' · следующий запуск ' + fmtTime(t.next_run) : '') + '</div>' : '') +
      '<div class="tc-events"></div>' +
      (t.result ? '<div class="tc-result md">' + MD.render(String(t.result).slice(0, 2500)) + '</div>' : '') +
      '<div class="tc-actions"></div>';

    const evBox = card.querySelector('.tc-events');
    (t.events || []).slice(-40).forEach((e) => {
      evBox.appendChild(el('div', 'tc-ev', esc(typeof e === 'string' ? e : (e.text || JSON.stringify(e)))));
    });

    const acts = card.querySelector('.tc-actions');
    const mk = (label, cls, fn) => { const b = el('button', 'btn sm ' + (cls || ''), label); b.addEventListener('click', fn); acts.appendChild(b); };
    if ((t.events || []).length) mk('Лог', 'ghost', () => evBox.classList.toggle('open'));
    if (st !== 'running') mk('Запустить', 'primary', async () => { await api('/api/tasks/run', { task_id: t.id }); toast('Задача запущена', 'info'); setTimeout(loadTasks, 700); });
    else mk('Отменить', 'ghost', async () => { await api('/api/tasks/cancel', { task_id: t.id }); loadTasks(); });
    mk('Удалить', 'danger', async () => { await api('/api/tasks/delete', { task_id: t.id }); loadTasks(); });
    grid.appendChild(card);
  });
}

$('#addTaskBtn').addEventListener('click', () => {
  modal(
    '<h3>Новая фоновая задача</h3>' +
    '<div class="md-sub">JARVIS выполнит её сам и пришлёт результат — в уведомления и в Telegram.</div>' +
    '<div class="field"><label>Название</label><input id="mTitle" placeholder="Утренняя сводка"></div>' +
    '<div class="field"><label>Что сделать</label><textarea id="mPrompt" rows="4" placeholder="Собери главные новости про ИИ и сделай короткую сводку"></textarea></div>' +
    '<div class="field"><label>Расписание (необязательно)</label><input id="mSched" placeholder="daily 09:00  ·  every 2h  ·  every 30m"></div>' +
    '<div class="modal-acts"><button class="btn ghost" id="mCancel">Отмена</button>' +
    '<button class="btn primary" id="mOk">Создать</button></div>',
    (m) => {
      $('#mCancel', m).addEventListener('click', closeModal);
      $('#mOk', m).addEventListener('click', async () => {
        const title = $('#mTitle', m).value.trim();
        const prompt = $('#mPrompt', m).value.trim();
        if (!prompt) { toast('Опиши задачу', 'warn'); return; }
        await api('/api/tasks/new', { title: title || prompt.slice(0, 40), prompt, schedule: $('#mSched', m).value.trim() });
        closeModal(); loadTasks(); toast('Задача создана', 'success');
      });
    }
  );
});

/* ============================ песочница ============================ */
async function loadFiles() {
  const r = await api('/api/files');
  const grid = $('#fileGrid');
  const files = r.files || [];
  if (!files.length) {
    grid.innerHTML = '<div class="empty" style="grid-column:1/-1"><span class="e-ico">▤</span>' +
      'Песочница пуста.<br>Здесь появятся файлы, которые я создам: отчёты, таблицы, картинки, архивы.</div>';
    return;
  }
  grid.innerHTML = '';
  files.slice().reverse().forEach((f, i) => {
    const c = el('div', 'fcard');
    c.style.animationDelay = (i * 0.02) + 's';
    c.innerHTML = (isImg(f.name) ? '<img src="' + f.download_url + '" loading="lazy">' :
      '<div class="fi">' + fileIcon(f.name) + '</div>') +
      '<div class="fn">' + esc(f.name) + '</div><div class="fs">' + fmtSize(f.size) + '</div>';
    c.addEventListener('click', () => {
      if (isImg(f.name)) lightbox(f.download_url); else window.open(f.download_url, '_blank');
    });
    grid.appendChild(c);
  });
}
$('#refreshFiles').addEventListener('click', loadFiles);

/* ============================ память ============================ */
async function loadMemory() {
  const r = await api('/api/memory');
  const grid = $('#memGrid');
  const items = r.memory || [];
  if (!items.length) {
    grid.innerHTML = '<div class="empty" style="grid-column:1/-1"><span class="e-ico">◇</span>' +
      'Память пуста.<br>Расскажи о себе в чате — я запомню сам. Или добавь факт кнопкой выше.</div>';
    return;
  }
  grid.innerHTML = '';
  items.forEach((m, i) => {
    const c = el('div', 'mem-card');
    c.style.animationDelay = (i * 0.02) + 's';
    c.innerHTML = '<div class="mem-kind">' + esc(m.kind) + '</div>' +
      '<div class="mem-key">' + esc(m.key) + '</div>' +
      '<div class="mem-val">' + esc(m.value) + '</div><i class="mem-del">✕</i>';
    c.querySelector('.mem-del').addEventListener('click', async () => {
      await api('/api/memory/delete', { id: m.id }); loadMemory();
    });
    grid.appendChild(c);
  });
}
$('#addMemBtn').addEventListener('click', () => {
  modal('<h3>Добавить факт в память</h3><div class="md-sub">Например: «Город» → «Москва».</div>' +
    '<div class="field"><label>Что</label><input id="mk" placeholder="Город"></div>' +
    '<div class="field"><label>Значение</label><input id="mv" placeholder="Москва"></div>' +
    '<div class="modal-acts"><button class="btn ghost" id="mc">Отмена</button>' +
    '<button class="btn primary" id="mo">Запомнить</button></div>',
    (m) => {
      $('#mc', m).addEventListener('click', closeModal);
      $('#mo', m).addEventListener('click', async () => {
        const k = $('#mk', m).value.trim(), v = $('#mv', m).value.trim();
        if (!k || !v) return;
        await api('/api/memory/add', { kind: 'fact', key: k, value: v });
        closeModal(); loadMemory(); toast('Запомнил', 'success');
      });
    });
});

/* ============================ настройки ============================ */
function renderSettings() {
  const c = S.config || {};
  const p = c.providers || {};
  const grid = $('#settingsGrid');
  grid.innerHTML = '';

  // Провайдеры
  const prov = el('div', 'sset');
  prov.innerHTML = '<h3>Модели и ключи</h3>' +
    '<div class="sd">Основной провайдер — Cloud.ru (Сбер, работает из России без VPN). DeepSeek — резерв.</div>' +
    '<div class="prov-state"><span class="dot" style="width:7px;height:7px;border-radius:50%;background:' +
    ((p.cloudru || {}).has_key ? 'var(--green)' : 'var(--red)') + '"></span> Cloud.ru: ' +
    ((p.cloudru || {}).has_key ? 'ключ установлен' : 'нет ключа') + '</div>' +
    '<div class="field"><label>API-ключ Cloud.ru</label><input id="kCloud" placeholder="' +
    esc((p.cloudru || {}).api_key || 'вставь ключ') + '"></div>' +
    '<div class="prov-state"><span class="dot" style="width:7px;height:7px;border-radius:50%;background:' +
    ((p.deepseek || {}).has_key ? 'var(--green)' : 'var(--tx3)') + '"></span> DeepSeek: ' +
    ((p.deepseek || {}).has_key ? 'ключ установлен' : 'нет ключа') + '</div>' +
    '<div class="field"><label>API-ключ DeepSeek</label><input id="kDeep" placeholder="' +
    esc((p.deepseek || {}).api_key || 'вставь ключ') + '"></div>' +
    '<div class="field"><label>Уровень модели</label><select id="fTier">' +
    ['', 'nano', 'base', 'smart', 'coder'].map((t) =>
      '<option value="' + t + '"' + (((c.orchestrator || {}).force_tier || '') === t ? ' selected' : '') + '>' +
      (t ? TIER_LABEL[t] || t : 'авто — выбирает JARVIS') + '</option>').join('') +
    '</select></div>' +
    '<button class="btn primary" id="saveProv">Сохранить</button> ' +
    '<button class="btn" id="testProv">Проверить связь</button>';
  grid.appendChild(prov);
  $('#saveProv', prov).addEventListener('click', async () => {
    const patch = { providers: {}, orchestrator: { force_tier: $('#fTier', prov).value } };
    const kc = $('#kCloud', prov).value.trim(), kd = $('#kDeep', prov).value.trim();
    if (kc) patch.providers.cloudru = { api_key: kc };
    if (kd) patch.providers.deepseek = { api_key: kd };
    const r = await api('/api/config/update', { patch });
    S.config = r.config || S.config;
    toast('Настройки сохранены', 'success'); renderSettings(); refreshState();
  });
  $('#testProv', prov).addEventListener('click', async () => {
    toast('Проверяю…', 'info');
    const h = await api('/api/health');
    const lines = Object.entries(h.providers || {}).map(([k, v]) =>
      k + ': ' + (v.ok ? '✓ ' + (v.models || 0) + ' моделей' : '✕ ' + (v.error || 'нет доступа'))).join('\n');
    modal('<h3>Состояние провайдеров</h3><pre class="out">' + esc(lines || 'нет данных') + '</pre>' +
      '<div class="modal-acts"><button class="btn primary" onclick="document.getElementById(\'modalBack\').classList.remove(\'open\')">Ок</button></div>');
  });

  // Безопасность
  const saf = el('div', 'sset');
  saf.innerHTML = '<h3>Безопасность</h3><div class="sd">Что я обязан спросить перед выполнением.</div>';
  const safety = c.safety || {};
  [['confirm_payments', 'Спрашивать перед оплатой'],
   ['confirm_delete', 'Спрашивать перед удалением'],
   ['confirm_shell', 'Спрашивать перед командами терминала'],
   ['confirm_computer_use', 'Спрашивать перед управлением мышью'],
   ['confirm_send_message', 'Спрашивать перед отправкой сообщений'],
   ['auto_approve_readonly', 'Безопасное (чтение, поиск) — без вопросов']]
    .forEach(([key, label]) => {
      const row = el('div', 'switch', '<span>' + label + '</span>');
      const sw = el('div', 'sw' + (safety[key] ? ' on' : ''));
      sw.addEventListener('click', async () => {
        sw.classList.toggle('on');
        const patch = { safety: {} }; patch.safety[key] = sw.classList.contains('on');
        const r = await api('/api/config/update', { patch }); S.config = r.config || S.config;
      });
      row.appendChild(sw); saf.appendChild(row);
    });
  grid.appendChild(saf);

  // AUTO
  const au = el('div', 'sset');
  au.innerHTML = '<h3>AUTO · фоновый режим</h3><div class="sd">Работа без тебя и проактивные подсказки.</div>';
  const autoCfg = c.auto || {};
  [['enabled', 'Фоновые задачи включены'], ['proactive', 'Проактивные подсказки']].forEach(([key, label]) => {
    const row = el('div', 'switch', '<span>' + label + '</span>');
    const sw = el('div', 'sw' + (autoCfg[key] ? ' on' : ''));
    sw.addEventListener('click', async () => {
      sw.classList.toggle('on');
      const patch = { auto: {} }; patch.auto[key] = sw.classList.contains('on');
      const r = await api('/api/config/update', { patch }); S.config = r.config || S.config;
    });
    row.appendChild(sw); au.appendChild(row);
  });
  au.innerHTML += '<div class="field" style="margin-top:12px"><label>Тихие часы (не беспокоить), от и до</label>' +
    '<input id="qh" value="' + esc((autoCfg.quiet_hours || [1, 8]).join('-')) + '" placeholder="1-8"></div>' +
    '<button class="btn primary" id="saveAuto">Сохранить</button>';
  grid.appendChild(au);
  $('#saveAuto', au).addEventListener('click', async () => {
    const parts = $('#qh', au).value.split('-').map((x) => parseInt(x, 10) || 0);
    const r = await api('/api/config/update', { patch: { auto: { quiet_hours: [parts[0] || 0, parts[1] || 0] } } });
    S.config = r.config || S.config; toast('Сохранено', 'success');
  });

  // Telegram
  const tg = el('div', 'sset');
  const tgc = c.telegram || {};
  tg.innerHTML = '<h3>Telegram</h3>' +
    '<div class="sd">Уведомления и отчёты фоновых задач. Создай бота у @BotFather, вставь токен, ' +
    'напиши боту «привет» и укажи свой chat id (узнать: @userinfobot).</div>' +
    '<div class="field"><label>Токен бота</label><input id="tgTok" placeholder="' + esc(tgc.bot_token || '123456:AA...') + '"></div>' +
    '<div class="field"><label>Твой chat id</label><input id="tgChat" value="' + esc(tgc.chat_id || '') + '" placeholder="123456789"></div>' +
    '<button class="btn primary" id="saveTg">Сохранить</button> <button class="btn" id="testTg">Тест</button>';
  grid.appendChild(tg);
  $('#saveTg', tg).addEventListener('click', async () => {
    const patch = { telegram: { chat_id: $('#tgChat', tg).value.trim(), enabled: true } };
    const tok = $('#tgTok', tg).value.trim(); if (tok) patch.telegram.bot_token = tok;
    const r = await api('/api/config/update', { patch }); S.config = r.config || S.config;
    toast('Telegram настроен', 'success');
  });
  $('#testTg', tg).addEventListener('click', async () => {
    const r = await api('/api/tool', { name: 'send_telegram', args: { text: 'JARVIS на связи. Проверка уведомлений ✅' } });
    toast(r.ok ? 'Сообщение отправлено' : (r.error || 'не отправилось'), r.ok ? 'success' : 'error');
  });

  // Профиль
  const pr = el('div', 'sset');
  const u = c.user || {};
  pr.innerHTML = '<h3>Обо мне</h3><div class="sd">Чтобы я отвечал персонально.</div>' +
    '<div class="field"><label>Имя</label><input id="uName" value="' + esc(u.name || '') + '"></div>' +
    '<div class="field"><label>Город</label><input id="uCity" value="' + esc(u.city || '') + '"></div>' +
    '<div class="field"><label>Коротко о себе, интересы, стиль общения</label>' +
    '<textarea id="uAbout" rows="3">' + esc(u.about || '') + '</textarea></div>' +
    '<button class="btn primary" id="saveUser">Сохранить</button>';
  grid.appendChild(pr);
  $('#saveUser', pr).addEventListener('click', async () => {
    const r = await api('/api/config/update', {
      patch: { user: { name: $('#uName', pr).value, city: $('#uCity', pr).value, about: $('#uAbout', pr).value } },
    });
    S.config = r.config || S.config; toast('Профиль сохранён', 'success');
  });

  // Расходы
  const us = el('div', 'sset');
  us.innerHTML = '<h3>Расходы</h3><div class="sd">Сколько потрачено на модели за 24 часа.</div><div id="usageBox" class="muted">загрузка…</div>';
  grid.appendChild(us);
  api('/api/usage').then((r) => {
    const t = r.total || {};
    const rows = (r.by_model || []).map((m) =>
      '<div class="switch"><span style="font-family:var(--fm);font-size:11px">' + esc(m.model || '—') +
      '</span><b style="color:var(--cy)">' + (m.cost || 0).toFixed(2) + ' ₽ · ' + m.calls + '</b></div>').join('');
    $('#usageBox').innerHTML =
      '<div class="switch"><span>Запросов</span><b style="color:var(--cy)">' + (t.calls || 0) + '</b></div>' +
      '<div class="switch"><span>Токенов</span><b style="color:var(--cy)">' + ((t.pt || 0) + (t.ct || 0)) + '</b></div>' +
      '<div class="switch"><span>Итого</span><b style="color:var(--green)">' + (t.cost || 0).toFixed(2) + ' ₽</b></div>' +
      rows;
  });
}

/* ============================ старт ============================ */
window.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'k') { e.preventDefault(); newChat(); }
  if (e.key === 'Escape') { closeModal(); $('#drawer').classList.remove('open'); }
});

(async function init() {
  await refreshState();
  await loadChats();
  $('#stream').appendChild(buildWelcome());
  setInterval(refreshState, 4000);
  if (!(S.config.providers || {}).cloudru || !S.config.providers.cloudru.has_key) {
    setTimeout(() => {
      toast('Открой Настройки и вставь API-ключ, чтобы я заработал.', 'warn', 'Нужен ключ');
    }, 2600);
  }
  $('#input').focus();
})();
