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
  shownApprovals: new Set(),
  sanctionNodes: {},
  streamApproval: false,
  shownNotes: new Set(),
  notesReady: false,
  camNode: null,
  camTimer: null,
  camBusy: false,
  camLast: '',
  camPrevPix: null,
  sandbox: {},
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
/* Иконки файлов — тонкая линейная графика, каждый тип со своим сдержанным
   цветом (класс c-*). Никаких эмодзи: они выглядят по-детски и по-разному
   рисуются в разных системах. */
const F_SVG = {
  img: '<rect x="3" y="4.5" width="18" height="15" rx="2.4"/><circle cx="8.5" cy="10" r="1.6"/><path d="M4 17l4.8-4.6 3.4 3.1 3-2.6L20 17"/>',
  vid: '<rect x="2.8" y="5.5" width="12.6" height="13" rx="2.2"/><path d="M15.4 11l5.5-3v8l-5.5-3z"/>',
  aud: '<path d="M9 17.5V5.6l10-2v11.4"/><circle cx="6.6" cy="17.6" r="2.6"/><circle cx="16.6" cy="14.6" r="2.6"/>',
  zip: '<path d="M6 3.2h9.2L20 8v12.8H6z"/><path d="M15 3.2V8h5"/><path d="M10.5 4v2M10.5 8v2M10.5 12v2"/>',
  pdf: '<path d="M6 3.2h8.2L19 8v12.8H6z"/><path d="M14 3.2V8h5"/><path d="M9 15.5c3-.6 4.6-4 4-5.4-.6-1.4-2 .3-1.4 2.6.6 2.3 2.4 4 4.4 4.2"/>',
  tab: '<rect x="3.4" y="4.4" width="17.2" height="15.2" rx="2.2"/><path d="M3.4 9.4h17.2M9 9.4v10.2M15 9.4v10.2"/>',
  doc: '<path d="M6 3.2h8.2L19 8v12.8H6z"/><path d="M14 3.2V8h5"/><path d="M9 12.6h7M9 16h5"/>',
  code: '<path d="M9 8.4L4.6 12 9 15.6"/><path d="M15 8.4L19.4 12 15 15.6"/><path d="M13.2 5.6l-2.4 12.8"/>',
  any: '<path d="M6 3.2h8.2L19 8v12.8H6z"/><path d="M14 3.2V8h5"/>',
};
function fsvg(kind, cls) {
  return '<span class="fico ' + cls + '"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">' + F_SVG[kind] + '</svg></span>';
}
function fileIcon(name) {
  const n = String(name || '').toLowerCase();
  if (/\.(png|jpe?g|gif|webp|svg|bmp)$/.test(n)) return fsvg('img', 'c-img');
  if (/\.(mp4|mov|avi|mkv|webm)$/.test(n)) return fsvg('vid', 'c-vid');
  if (/\.(mp3|wav|ogg|m4a|opus|flac)$/.test(n)) return fsvg('aud', 'c-aud');
  if (/\.(zip|tar|gz|rar|7z)$/.test(n)) return fsvg('zip', 'c-zip');
  if (/\.(pdf)$/.test(n)) return fsvg('pdf', 'c-pdf');
  if (/\.(xlsx?|csv)$/.test(n)) return fsvg('tab', 'c-tab');
  if (/\.(docx?|txt|md|rtf)$/.test(n)) return fsvg('doc', 'c-doc');
  if (/\.(py|js|ts|html|css|json|sh|go|rs|java|yml|yaml|xml)$/.test(n)) return fsvg('code', 'c-code');
  return fsvg('any', 'c-any');
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
/* сворачивание бокового меню.
   На узком экране меню выезжает поверх (nav-open),
   на широком — схлопывается колонка сетки (collapsed). */
function isNarrow() { return window.matchMedia('(max-width:900px)').matches; }
function toggleSidebar() {
  const app = $('#app');
  if (isNarrow()) { app.classList.toggle('nav-open'); return; }
  app.classList.remove('nav-open');
  const collapsed = app.classList.toggle('collapsed');
  try { localStorage.setItem('jarvis.sidebar', collapsed ? 'collapsed' : 'open'); } catch (e) {}
}
$('#collapseBtn').addEventListener('click', toggleSidebar);
try {
  if (localStorage.getItem('jarvis.sidebar') === 'collapsed' && !isNarrow()) {
    $('#app').classList.add('collapsed');
  }
} catch (e) {}

/* Правой панели больше нет: уведомления, санкции и камера живут прямо в чате
   (см. разделы «камера в диалоге» и «санкции / уведомления в диалоге» ниже). */

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
  if (S.cameraOn) startCam(); else stopCam();
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
    item.innerHTML = '<span>' + esc(c.title || 'Диалог') + '</span>' +
      '<span class="chat-acts"><i class="chat-r" title="Переименовать">✎</i>' +
      '<i class="chat-x" title="Удалить">✕</i></span>';

    item.querySelector('.chat-r').addEventListener('click', (e) => {
      e.stopPropagation();
      startRenameChat(item, c);
    });
    item.querySelector('.chat-x').addEventListener('click', (e) => {
      e.stopPropagation();
      confirmBox('Удалить диалог?',
        'Диалог «' + esc(c.title || 'Диалог') + '» и его песочница будут удалены безвозвратно.', () => {
          api('/api/chats/delete', { chat_id: c.id }).then(() => {
            toast('Диалог удалён', 'success');
            if (S.chatId === c.id) newChat(); else loadChats();
          });
        });
    });
    item.addEventListener('click', () => openChat(c.id));
    list.appendChild(item);
  });
}
/* переименование диалога прямо в списке: поле вместо названия */
function startRenameChat(item, c) {
  const label = item.querySelector('span');
  if (!label || item.querySelector('.chat-edit')) return;
  const inp = el('input', 'chat-edit');
  inp.value = c.title || '';
  label.replaceWith(inp);
  inp.focus(); inp.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const val = inp.value.trim();
    if (save && val && val !== c.title) {
      const r = await api('/api/chats/rename', { chat_id: c.id, title: val });
      if (r.ok) toast('Название изменено', 'success');
    }
    loadChats();
  };
  inp.addEventListener('click', (e) => e.stopPropagation());
  inp.addEventListener('blur', () => finish(true));
  inp.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') { finish(false); }
  });
}

function newChat() {
  if (S.camStream) stopCam();          // камера жила в старом диалоге — гасим
  S.sanctionNodes = {};
  S.chatId = null;
  $('#stream').innerHTML = '';
  $('#stream').appendChild(buildWelcome());
  loadChats();
  showView('chat');
  $('#input').focus();
}
$('#newChatBtn').addEventListener('click', newChat);

async function openChat(id) {
  if (S.camStream) stopCam();
  S.sanctionNodes = {};
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

  // две кнопки под своим сообщением: скопировать и редактировать
  const acts = el('div', 'msg-actions');
  const copy = el('button', 'act act-copy', ICO.copy + '<span>Скопировать</span>');
  copy.addEventListener('click', () => {
    navigator.clipboard.writeText(text).then(
      () => toast('Скопировано', 'success'),
      () => toast('Буфер обмена недоступен', 'error'));
  });
  const edit = el('button', 'act act-edit', ICO.edit + '<span>Редактировать</span>');
  edit.addEventListener('click', () => {
    const inp = $('#input');
    inp.value = text;
    autoGrow(); inp.focus();
    try { inp.setSelectionRange(text.length, text.length); } catch (e) {}
    m.querySelector('.bubble-user').classList.add('editing');
    setTimeout(() => {
      const b = m.querySelector('.bubble-user');
      if (b) b.classList.remove('editing');
    }, 1600);
    toast('Текст перенесён в поле ввода — правь и отправляй', 'info');
  });
  acts.appendChild(copy); acts.appendChild(edit);
  m.appendChild(acts);

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
  const copy = el('button', 'act act-copy', ICO.copy + '<span>Копировать</span>');
  copy.addEventListener('click', () => {
    navigator.clipboard.writeText(text).then(() => toast('Скопировано', 'success'));
  });
  const speak = el('button', 'act act-speak', ICO.speak + '<span>Озвучить</span>');
  speak.addEventListener('click', () => {
    try {
      const u = new SpeechSynthesisUtterance(text.replace(/[#*`>|\-]/g, '').slice(0, 900));
      u.lang = 'ru-RU'; u.rate = 1.03;
      speechSynthesis.cancel(); speechSynthesis.speak(u);
    } catch (e) { toast('Синтез речи недоступен', 'error'); }
  });
  const again = el('button', 'act act-again', ICO.again + '<span>Ещё раз</span>');
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
  if (!feed) return;
  // если в терминале открыт файл — освобождаем место под живой лог
  if (feed.querySelector('.term-file')) closeFileView();
  const line = el('div', 'term-line ' + (cls || ''), esc(text));
  feed.appendChild(line);
  feed.scrollTop = feed.scrollHeight;
  while (feed.children.length > 400) feed.removeChild(feed.firstChild);
}

/* ============================ иконки и миниатюры ============================ */
/* Единый набор тонких линейных иконок — без «детских» эмодзи. */
const ICO = {
  copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2.4"/><path d="M5.5 15H5a1.9 1.9 0 0 1-1.9-1.9V5A1.9 1.9 0 0 1 5 3.1h8.1A1.9 1.9 0 0 1 15 5v.5"/></svg>',
  edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h8"/><path d="M16.4 3.6a2.1 2.1 0 0 1 3 3L7.5 18.5 3.5 20l1.5-4z"/></svg>',
  speak: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9.5v5h3.5L12 18.5v-13L7.5 9.5z"/><path d="M16 8.6a4.6 4.6 0 0 1 0 6.8"/><path d="M18.6 6a8 8 0 0 1 0 12"/></svg>',
  again: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M20.5 12a8.5 8.5 0 1 1-2.6-6.1"/><path d="M20.6 4.4v4.4h-4.4"/></svg>',
  cam: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="2.8" y="6.5" width="13" height="11" rx="2.2"/><path d="M15.8 11l5.4-3v8l-5.4-3z"/></svg>',
  bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M18 9a6 6 0 0 0-12 0c0 5-2 6.5-2 6.5h16S18 14 18 9z"/><path d="M10.3 19.5a2 2 0 0 0 3.4 0"/></svg>',
  shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7.5 3v5.6c0 4.6-3.1 8-7.5 9.4-4.4-1.4-7.5-4.8-7.5-9.4V6z"/></svg>',
};

/* Свернуть блок в компактную строку-миниатюру.
   Клик по миниатюре разворачивает исходный блок обратно. */
function collapseToThumb(node, opts) {
  if (!node || !node.isConnected || node.dataset.collapsed === '1') return null;
  node.dataset.collapsed = '1';
  const thumb = el('div', 'thumb ' + (opts.cls || ''));
  const time = new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  thumb.innerHTML =
    '<span class="th-ico">' + (opts.icon || ICO.bell) + '</span>' +
    '<span class="th-t"><b>' + esc(opts.title || '') + '</b>' +
    (opts.sub ? ' · ' + esc(opts.sub) : '') + '</span>' +
    (opts.tag ? '<span class="th-tag">' + esc(opts.tag) + '</span>' : '') +
    '<span class="th-time">' + time + '</span>' +
    '<span class="th-open">›</span>';
  node.classList.add('collapsing');
  const parent = node.parentNode;
  setTimeout(() => {
    if (!parent) return;
    parent.insertBefore(thumb, node);
    node.style.display = 'none';
    node.classList.remove('collapsing');
  }, 260);
  thumb.addEventListener('click', () => {
    node.style.display = '';
    node.dataset.collapsed = '0';
    thumb.remove();
    node.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  });
  return thumb;
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

  // камера включена — молча прикладываем текущий кадр, чтобы вопрос был «про то, что вижу»
  if (S.camStream && !S.attachments.some((a) => a.fromCam)) {
    const frame = await camAttachFrame();
    if (frame) { frame.fromCam = true; S.attachments.push(frame); }
  }

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
      if (ui.statusEl) { ui.statusEl.remove(); ui.statusEl = null; }
      node.body.appendChild(el('div', 'muted', 'Остановлено.'));
    }
  } finally {
    // страховка: что бы ни случилось со стримом (обрыв, ошибка разбора,
    // закрытие сокета) — кнопка обязана вернуться в исходное состояние
    if (ui.statusEl) { ui.statusEl.remove(); ui.statusEl = null; }
    setStreaming(false);
    S.abort = null;
  }
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

    case 'chat_title': {
      // название диалога придумал сам JARVIS
      loadChats();
      const cur = S.chats.find((c) => c.id === ev.chat_id);
      if (!cur || cur.title !== ev.title) {
        toast('Диалог назван: ' + ev.title, 'info');
      }
      break;
    }

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
      // карточка уже нарисована прямо в ответе — дубль из renderSanctions не нужен
      S.streamApproval = true;
      refreshState();
      beep(340, 0.3);
      toast((ev.label || ev.tool) + ' — нужно твоё разрешение', 'warn', 'Санкция');
      scrollDown(true);
      break;
    }

    case 'approval_done':
      S.streamApproval = false;
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
      // поток завершён сервером — сразу возвращаем кнопку в «отправить»,
      // не дожидаясь фактического закрытия сокета
      setStreaming(false);
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
    const r = await api('/api/upload', { name: file.name, data: fr.result, chat_id: S.chatId || '' });
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

/* ============================ камера в диалоге ============================ */
/* Камера открывается прямо в чате. Кнопок нет: JARVIS сам смотрит трансляцию —
   раз в несколько секунд берёт кадр и, если картинка изменилась, отправляет
   его зрительной модели. Так получается «живое» распознавание видео. */
const CAM_TICK = 2500;      // как часто заглядывать в кадр, мс
const CAM_MOTION = 7;       // порог изменения сцены (0..255)

function buildCamCard() {
  const card = el('div', 'msg msg-ai cam-msg');
  card.innerHTML =
    '<div class="ai-avatar"><div class="reactor sm" style="width:34px;height:34px">' +
    '<div class="ring r1"></div><div class="ring r2"></div><div class="core"></div></div></div>' +
    '<div class="ai-body"><div class="ai-name">JARVIS<span class="ai-model"> · зрение</span></div>' +
    '<div class="ai-content">' +
      '<div class="cam-live">' +
        '<div class="cam-wrap">' +
          '<video id="cam" autoplay playsinline muted></video>' +
          '<div class="cam-scan"></div>' +
          '<div class="cam-corners"><i></i><i></i><i></i><i></i></div>' +
          '<div class="cam-hud"><span class="cam-rec"></span><span id="camState">включаю камеру…</span></div>' +
        '</div>' +
        '<div class="cam-feed" id="camFeed"></div>' +
        '<div class="cam-note muted">Трансляция идёт в реальном времени: я смотрю кадры и комментирую, ' +
        'что вижу. Спроси прямо в чате — например «что это?» или «где такое купить» — ' +
        'и я отвечу по тому, что сейчас в кадре.</div>' +
      '</div>' +
    '</div></div>';
  return card;
}

async function startCam() {
  if (S.camNode) return;
  showView('chat');
  killWelcome();
  S.camNode = buildCamCard();
  stream().appendChild(S.camNode);
  scrollDown(true);
  try {
    S.camStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: 'environment', width: { ideal: 1280 } }, audio: false,
    });
    $('#cam').srcObject = S.camStream;
    camState('трансляция · смотрю', true);
    camSay('Камера включена. Смотрю, что происходит.', 'sys');
    beep(720, 0.09);
    S.camPrevPix = null;
    S.camTimer = setInterval(camTick, CAM_TICK);
  } catch (e) {
    camState('нет доступа к камере', false);
    camSay('Не получилось включить камеру: браузер не дал доступ. Разреши камеру для этого сайта.', 'err');
    toast('Нет доступа к камере', 'error');
  }
}

function stopCam() {
  if (S.camTimer) { clearInterval(S.camTimer); S.camTimer = null; }
  if (S.camStream) { S.camStream.getTracks().forEach((t) => t.stop()); S.camStream = null; }
  const v = $('#cam');
  if (v) v.srcObject = null;
  if (S.camNode) {
    const node = S.camNode;
    node.classList.add('done');
    camState('трансляция завершена', false);
    // блок камеры отработал — сворачиваем его в компактную строку
    const seen = (node.querySelectorAll('.cam-line') || []).length;
    collapseToThumb(node, {
      cls: 'th-cam', icon: ICO.cam, title: 'Камера',
      sub: seen ? 'наблюдений: ' + seen : 'трансляция завершена', tag: 'закрыта',
    });
    S.camNode = null;
  }
  S.camPrevPix = null;
  S.cameraOn = false;
  $('#tgCamera').classList.remove('on');
}

function camState(text, live) {
  const st = $('#camState');
  if (st) st.textContent = text;
  const wrap = S.camNode && S.camNode.querySelector('.cam-live');
  if (wrap) wrap.classList.toggle('live', !!live);
}

function camSay(text, kind) {
  const feed = $('#camFeed');
  if (!feed) return;
  const line = el('div', 'cam-line ' + (kind || ''));
  line.innerHTML = '<span class="cam-t">' +
    new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) +
    '</span><span class="cam-x">' + (kind === 'sys' || kind === 'err' ? esc(text) : MD.render(text)) + '</span>';
  feed.appendChild(line);
  while (feed.children.length > 40) feed.removeChild(feed.firstChild);
  feed.scrollTop = feed.scrollHeight;
  scrollDown();
}

/* текущий кадр как data-url (для отправки модели) */
function camFrame(maxW) {
  const v = $('#cam');
  if (!v || !v.videoWidth) return null;
  const w = Math.min(maxW || 900, v.videoWidth);
  const h = Math.round(v.videoHeight * (w / v.videoWidth));
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  c.getContext('2d').drawImage(v, 0, 0, w, h);
  return c.toDataURL('image/jpeg', 0.82);
}

/* грубая оценка «что-то изменилось в кадре» — чтобы не жечь деньги впустую */
function camMotion() {
  const v = $('#cam');
  if (!v || !v.videoWidth) return 0;
  const c = document.createElement('canvas');
  c.width = 48; c.height = 36;
  const ctx = c.getContext('2d');
  ctx.drawImage(v, 0, 0, 48, 36);
  const cur = ctx.getImageData(0, 0, 48, 36).data;
  let diff = 255;
  if (S.camPrevPix) {
    let sum = 0;
    for (let i = 0; i < cur.length; i += 4) {
      const g1 = (cur[i] + cur[i + 1] + cur[i + 2]) / 3;
      const p = S.camPrevPix;
      const g2 = (p[i] + p[i + 1] + p[i + 2]) / 3;
      sum += Math.abs(g1 - g2);
    }
    diff = sum / (cur.length / 4);
  }
  S.camPrevPix = cur;
  return diff;
}

async function camTick() {
  if (S.camBusy || S.streaming || !S.camStream) return;
  const move = camMotion();
  if (move < CAM_MOTION && S.camLast) { camState('трансляция · кадр без изменений', true); return; }
  const data = camFrame();
  if (!data) return;
  S.camBusy = true;
  camState('трансляция · распознаю', true);
  try {
    const r = await api('/api/vision', {
      image: data,
      question: 'Это кадр живой видеотрансляции с камеры. Одним-двумя короткими предложениями по-русски ' +
        'скажи, что сейчас в кадре: объект, что с ним происходит, важные детали (текст, марка, состояние). ' +
        'Без вступлений и без «на изображении».',
    });
    const txt = (r.answer || r.content || r.text || '').trim();
    if (r.ok && txt && txt !== S.camLast) { S.camLast = txt; camSay(txt); }
    else if (!r.ok) camState('трансляция · ' + (r.error || 'модель молчит'), true);
    if (r.ok) camState('трансляция · смотрю', true);
  } finally {
    S.camBusy = false;
  }
}

/* если камера включена — к сообщению в чат автоматически прикладывается текущий кадр */
async function camAttachFrame() {
  if (!S.camStream) return null;
  const data = camFrame();
  if (!data) return null;
  const r = await api('/api/upload', { name: 'camera_' + Date.now() + '.jpg', data, chat_id: S.chatId || '' });
  if (!r.ok) return null;
  r.data = data;
  return r;
}

/* ============================ санкции / уведомления в диалоге ============================ */
/* Всё, что раньше жило в правой панели, теперь всплывает карточками в чате. */
function sanctionCard(a) {
  let args = a.args;
  try { args = JSON.stringify(JSON.parse(a.args), null, 1); } catch (e) { /* как есть */ }
  const critical = a.risk === 'danger' || /delete|shell|pay/.test(a.tool || '');
  const card = el('div', 'chat-card sanction' + (critical ? ' critical' : ''));
  card.innerHTML =
    '<div class="s-top">⛨ Санкция · ' + esc(a.tool) + '</div>' +
    '<div class="s-why">' + esc(a.reason || 'Требуется твоё разрешение.') + '</div>' +
    '<div class="s-args">' + esc(args) + '</div>' +
    '<div class="s-acts"><button class="btn primary sm">Разрешить</button>' +
    '<button class="btn danger sm">Отклонить</button></div>';
  const [okBtn, noBtn] = $$('.s-acts .btn', card);
  okBtn.addEventListener('click', () => decideApproval(a.id, 'approved', card, a.tool));
  noBtn.addEventListener('click', () => decideApproval(a.id, 'rejected', card, a.tool));
  return card;
}

function closeSanctionCard(card, text, tool) {
  if (!card) return;
  const acts = card.querySelector('.s-acts');
  if (acts) acts.innerHTML = '<span class="muted">' + esc(text) + '</span>';
  card.classList.add('resolved');
  // решение принято — карточка больше не нужна, оставляем компактный след
  const ok = text.indexOf('✕') === -1;
  setTimeout(() => collapseToThumb(card, {
    cls: ok ? 'th-ok' : 'th-no', icon: ICO.shield,
    title: 'Санкция' + (tool ? ' · ' + tool : ''),
    sub: text.replace(/[✓✕]\s*/, ''), tag: ok ? 'разрешено' : 'отклонено',
  }), 900);
}

function renderSanctions() {
  S.approvals.forEach((a) => {
    const key = String(a.id);
    if (S.shownApprovals.has(key)) return;
    // подтверждение по ходу стрима рисуется внутри ответа — второй раз не показываем
    if (S.streamApproval) { S.shownApprovals.add(key); return; }
    S.shownApprovals.add(key);
    killWelcome();
    const card = sanctionCard(a);
    S.sanctionNodes[key] = card;
    stream().appendChild(card);
    scrollDown(true);
    beep(340, 0.28);
  });
  // решённые где-то ещё — закрываем карточку
  const live = new Set(S.approvals.map((a) => String(a.id)));
  Object.keys(S.sanctionNodes).forEach((k) => {
    if (!live.has(k)) { closeSanctionCard(S.sanctionNodes[k], '✓ решено', ''); delete S.sanctionNodes[k]; }
  });
}

async function decideApproval(id, decision, card, tool) {
  await api('/api/approvals/decide', { id, decision });
  closeSanctionCard(card, decision === 'approved' ? '✓ разрешено' : '✕ отклонено', tool);
  toast(decision === 'approved' ? 'Разрешено — продолжаю' : 'Отклонено', decision === 'approved' ? 'success' : 'warn');
  refreshState();
}

function noteCard(n) {
  const kind = n.level === 'error' ? 'error' : (n.level === 'success' ? 'success' : '');
  const card = el('div', 'chat-card note-item ' + kind);
  card.innerHTML = '<div class="note-ico">' + (kind === 'error' ? '✕' : kind === 'success' ? '✓' : '◆') + '</div>' +
    '<div style="flex:1;min-width:0"><div class="note-t">' + esc(n.title) + '</div>' +
    '<div class="note-b">' + esc((n.body || '').slice(0, 900)) + '</div>' +
    '<div class="note-time">' + fmtTime(n.created_at) + '</div></div>' +
    '<i class="note-x" title="Свернуть">✕</i>';
  // крестик не удаляет уведомление, а сворачивает его в компактную строку
  card.querySelector('.note-x').addEventListener('click', (e) => {
    e.stopPropagation();
    collapseToThumb(card, {
      cls: 'th-note', icon: ICO.bell, title: n.title || 'Уведомление',
      sub: (n.body || '').slice(0, 70), tag: 'прочитано',
    });
    api('/api/notifications/read', {});
  });
  return card;
}

function renderNotes() {
  // при первой загрузке старые уведомления не сыплем в диалог
  if (!S.notesReady) {
    S.notifications.forEach((n) => S.shownNotes.add(String(n.id)));
    S.notesReady = true;
    return;
  }
  const fresh = S.notifications.filter((n) => !S.shownNotes.has(String(n.id)));
  fresh.reverse().forEach((n) => {
    S.shownNotes.add(String(n.id));
    killWelcome();
    stream().appendChild(noteCard(n));
    scrollDown();
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
/* У каждого диалога — своя песочница. Клик по файлу открывает его
   прямо в терминале ниже, панель сверху умеет переименовать и очистить. */
async function loadFiles() {
  const q = S.chatId ? '?chat_id=' + encodeURIComponent(S.chatId) : '';
  const r = await api('/api/files' + q);
  const grid = $('#fileGrid');
  const files = r.files || [];
  renderSbxBar(r.sandbox || {}, files);
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
      '<div class="fn">' + esc(f.name) + '</div><div class="fs">' + fmtSize(f.size) + '</div>' +
      '<i class="fx" title="Удалить файл">✕</i>';
    c.querySelector('.fx').addEventListener('click', (e) => {
      e.stopPropagation();
      confirmBox('Удалить файл?', esc(f.name) + ' будет удалён из песочницы безвозвратно.', async () => {
        const res = await api('/api/sandbox/delete_file', { name: f.name, chat_id: S.chatId || '' });
        if (res.ok) { toast('Файл удалён', 'success'); loadFiles(); }
        else toast(res.error || 'не удалось удалить', 'error');
      });
    });
    c.addEventListener('click', () => viewFile(f, c));
    grid.appendChild(c);
  });
}

function renderSbxBar(info, files) {
  const nameEl = $('#sbxName'), metaEl = $('#sbxMeta');
  if (!nameEl) return;
  S.sandbox = info || {};
  nameEl.textContent = info.name || 'Песочница';
  const total = files.reduce((a, f) => a + (f.size || 0), 0);
  metaEl.textContent = (info.shared ? 'общая · ' : 'диалог · ') +
    files.length + ' файл(ов) · ' + fmtSize(info.size != null ? info.size : total);
}

async function viewFile(f, card) {
  $$('.fcard.viewing').forEach((n) => n.classList.remove('viewing'));
  if (card) card.classList.add('viewing');
  const feed = $('#termFeed');
  const title = $('#termTitle');
  feed.classList.add('big');
  feed.innerHTML = '<div class="muted">открываю ' + esc(f.name) + '…</div>';
  title.textContent = 'ФАЙЛ · ' + f.name.toUpperCase();
  const dlBtn = $('#termDownload'), closeBtn = $('#termClose');
  dlBtn.hidden = false; closeBtn.hidden = false;
  dlBtn.onclick = () => window.open(f.download_url, '_blank');
  closeBtn.onclick = closeFileView;

  const q = '/api/files/view?name=' + encodeURIComponent(f.name) +
    (S.chatId ? '&chat_id=' + encodeURIComponent(S.chatId) : '');
  const r = await api(q);
  if (!r.ok) { feed.innerHTML = '<div class="term-line err">' + esc(r.error || 'не открылось') + '</div>'; return; }
  const head = '<div class="tf-head">' + esc(f.name) + ' · ' + fmtSize(r.size) + ' · ' +
    (r.kind === 'text' ? 'текст' : r.kind === 'image' ? 'изображение' : 'двоичный файл') + '</div>';
  if (r.kind === 'image') {
    feed.innerHTML = '<div class="term-file">' + head + '<img src="' + esc(r.download_url) + '"></div>';
    const im = feed.querySelector('img');
    if (im) im.addEventListener('click', () => lightbox(r.download_url));
  } else if (r.kind === 'text') {
    feed.innerHTML = '<div class="term-file">' + head + '<pre>' + esc(r.content || '(пусто)') + '</pre></div>';
  } else {
    feed.innerHTML = '<div class="term-file">' + head +
      '<div class="muted">Двоичный файл — показать в терминале нельзя. Нажми «Скачать».</div></div>';
  }
  feed.scrollTop = 0;
}

function closeFileView() {
  const feed = $('#termFeed');
  feed.classList.remove('big');
  feed.innerHTML = '';
  $('#termTitle').textContent = 'ТЕРМИНАЛ';
  $('#termDownload').hidden = true;
  $('#termClose').hidden = true;
  $$('.fcard.viewing').forEach((n) => n.classList.remove('viewing'));
}

$('#refreshFiles').addEventListener('click', loadFiles);

$('#sbxRename').addEventListener('click', () => {
  if (!S.chatId) { toast('Общую песочницу переименовать нельзя — открой диалог', 'warn'); return; }
  promptBox('Название песочницы', (S.sandbox || {}).name || '', async (val) => {
    const r = await api('/api/sandbox/rename', { name: val, chat_id: S.chatId });
    if (r.ok) { toast('Песочница переименована', 'success'); loadFiles(); }
    else toast(r.error || 'не удалось', 'error');
  });
});

$('#sbxWipe').addEventListener('click', () => {
  confirmBox('Очистить песочницу?',
    'Все файлы этой песочницы будут удалены безвозвратно. Действие нельзя отменить.', async () => {
      const r = await api('/api/sandbox/clear', { chat_id: S.chatId || '' });
      if (r.ok) { toast('Удалено файлов: ' + (r.removed || 0), 'success'); closeFileView(); loadFiles(); }
      else toast(r.error || 'не удалось очистить', 'error');
    });
});

/* маленькие диалоги подтверждения и ввода — на базе общего модального окна */
function confirmBox(title, text, onYes) {
  modal('<h3>' + esc(title) + '</h3><div class="md-sub">' + text + '</div>' +
    '<div class="modal-acts"><button class="btn" id="cbNo">Отмена</button>' +
    '<button class="btn danger" id="cbYes">Да, продолжить</button></div>', (m) => {
    $('#cbNo', m).addEventListener('click', closeModal);
    $('#cbYes', m).addEventListener('click', () => { closeModal(); onYes(); });
  });
}
function promptBox(title, value, onOk) {
  modal('<h3>' + esc(title) + '</h3><div class="md-sub">Коротко и по делу — так проще искать.</div>' +
    '<div class="field"><input id="pbVal" value="' + esc(value) + '"></div>' +
    '<div class="modal-acts"><button class="btn" id="pbNo">Отмена</button>' +
    '<button class="btn primary" id="pbOk">Сохранить</button></div>', (m) => {
    const inp = $('#pbVal', m);
    inp.focus(); inp.select();
    const ok = () => { const v = inp.value.trim(); if (!v) return; closeModal(); onOk(v); };
    $('#pbNo', m).addEventListener('click', closeModal);
    $('#pbOk', m).addEventListener('click', ok);
    inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') ok(); });
  });
}

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
    const r = await api('/api/tool', { name: 'send_telegram', args: { text: 'JARVIS на связи. Проверка уведомлений — всё работает.' } });
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
  if (e.key === 'Escape') closeModal();
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
