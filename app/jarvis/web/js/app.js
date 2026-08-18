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
  editing: null,   // {id, node} — какое сообщение правим (новая версия, не новая реплика)
  asr: null,
  asrStop: null,
  asrFallback: false,
  asrTriedBrowser: false,
  micToast: null,
  micTimer: null,
  detached: null,
  detachTimer: null,
  sandbox: {},
  // выделение в файлах: набор путей + якорь для Shift-диапазона (как в Finder)
  fsel: new Set(),
  fanchor: '',
  fselAuto: false,     // выделение поставлено самим перетаскиванием, не человеком
  frows: [],
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
/* Время сообщения. Единственный источник истины — created_at из базы: он
   приходит вместе с перепиской и одинаков во всех вкладках. У только что
   отправленной реплики его ещё нет (сервер сохранит её через миг), поэтому
   берём текущий момент — расхождение меньше секунды. */
function stampTime(node, ts) {
  if (!node) return null;
  const sec = Number(ts) || (Date.now() / 1000);
  const prev = node.querySelector('.msg-time');
  if (prev) prev.remove();
  const d = new Date(sec * 1000);
  const hhmm = d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  // Бейдж узкий — шире иконки ему быть нельзя, иначе он лезет на текст.
  // Поэтому дата (если сообщение не сегодняшнее) уходит отдельной строкой сверху.
  const sameDay = d.toDateString() === new Date().toDateString();
  const day = sameDay ? '' : d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' });
  const t = el('span', 'msg-time' + (day ? ' two' : ''),
    (day ? '<i>' + esc(day) + '</i>' : '') + esc(hhmm));
  t.title = new Date(sec * 1000).toLocaleString('ru-RU');
  // Время живёт на самой иконке сообщения: у ответа — бейджем на реакторе,
  // у своей реплики — такой же меткой рядом с пузырём. Отдельной строки под
  // сообщением больше нет, лента не растёт по высоте из-за времени.
  // у ответа — бейдж на иконке-реакторе, у своей реплики — внутри пузыря,
  // прижат к нижнему правому углу (текст обтекает его отступом)
  const av = node.querySelector(':scope > .ai-avatar');
  if (av) { av.appendChild(t); return t; }
  const bubble = node.querySelector(':scope > .bubble-user');
  if (bubble) { t.classList.add('in-bubble'); bubble.appendChild(t); }
  else node.appendChild(t);
  return t;
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
  dir: '<path d="M3.2 6.6a1.8 1.8 0 011.8-1.8h4l2 2.4h7a1.8 1.8 0 011.8 1.8v9.6a1.8 1.8 0 01-1.8 1.8H5a1.8 1.8 0 01-1.8-1.8z"/>',
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
  // клик в любое место уведомления убирает его сразу
  t.title = 'Кликни, чтобы убрать';
  t.addEventListener('click', () => {
    t.classList.add('out');
    setTimeout(() => t.remove(), 300);
  });
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

/* E. Тихий интерфейсный щелчок: очень короткий и тихий, чтобы отмечать
   действие, а не привлекать внимание. Тумблер — «Интерфейсные звуки». */
function blip(up) {
  if (!S.config.ui || S.config.ui.sound === false) return;
  try {
    const ctx = beep.ctx || (beep.ctx = new (window.AudioContext || window.webkitAudioContext)());
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.type = 'triangle';
    const t = ctx.currentTime;
    o.frequency.setValueAtTime(up ? 620 : 520, t);
    o.frequency.exponentialRampToValueAtTime(up ? 940 : 380, t + 0.05);
    g.gain.setValueAtTime(0.028, t);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.07);
    o.connect(g); g.connect(ctx.destination); o.start(t); o.stop(t + 0.08);
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
/* Заставка больше не «изображает» загрузку: строки идут своим темпом, но экран
   гаснет, как только сервер реально ответил. Раньше при медленном /api/state
   пользователь смотрел на бесконечное «соединение». */
let BOOT_DONE = null;
(function boot() {
  const log = $('#bootLog');
  let i = 0;
  let finished = false;
  const finish = () => {
    if (finished) return;
    finished = true;
    $('#boot').classList.add('hide');
    $('#app').classList.add('ready');
    beep(880, 0.22);
  };
  BOOT_DONE = finish;
  // что бы ни случилось со связью — дольше 4 секунд заставку не держим
  setTimeout(finish, 4000);
  const tick = () => {
    if (i < BOOT_LINES.length) {
      const line = el('div', '', BOOT_LINES[i]);
      log.appendChild(line); i++; beep(520 + i * 60, 0.05);
      setTimeout(tick, 180);
    } else {
      setTimeout(finish, 260);
    }
  };
  setTimeout(tick, 300);
})();

/* ============================ навигация ============================ */
function showView(name) {
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === 'view-' + name));
  $$('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === name));
  const titles = { chat: 'Диалог', auto: 'AUTO · фоновые задачи', files: 'Файлы', memory: 'Память', settings: 'Настройки' };
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
  applySilentTools(st.silent_tools);
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
  renderBalance(st.billing || {});

  renderSanctions(); renderNotes(); renderNotePanel();
  syncChatTail();
  if ($('#view-auto').classList.contains('active')) renderTasks();
}

/* ================== догрузка сообщений, пришедших извне ==================
   Фоновая задача AUTO пишет ответ прямо в диалог на сервере. Раньше он
   появлялся только после переоткрытия чата — теперь подтягиваем на лету. */
async function syncChatTail() {
  if (!S.chatId || S.streaming || S.editing) return;
  const r = await api('/api/messages?chat_id=' + encodeURIComponent(S.chatId));
  if (!r.ok || !r.messages) return;
  const known = new Set($$('[data-msg-id]', stream()).map((n) => n.dataset.msgId));
  let added = false;
  r.messages.forEach((m) => {
    if (known.has(String(m.id))) return;
    if (m.role !== 'assistant') return;
    const meta = m.meta || {};
    if (!meta.from_auto) return;          // свои ответы рисует сам стрим
    const node = addAiMsg(m.created_at);
    node.root.dataset.msgId = m.id;
    node.body.innerHTML = '<div class="md">' + MD.render(m.content) + '</div>';
    foldCodeBlocks(node.body);
    mountUiPanels(node.body);
    (meta.files || []).forEach((f) => attachFileChip(node.body, f));
    addMsgActions(node, m.content);
    added = true;
  });
  if (added) { scrollDown(); beep(660, 0.08); }
}
/* Баланс и расход аккаунта Cloud.ru в подвале сайдбара.
   Публичного метода «баланс лицевого счёта» у Cloud.ru нет, поэтому
   показываем то, что реально доступно: расход за месяц по биллинг-API,
   а если ключи биллинга не заданы — свою локальную оценку. */
function renderBalance(b) {
  const row = $('#footBalRow'), val = $('#footBalance');
  if (!row || !val) return;
  S.billing = b || {};
  row.classList.toggle('off', !b.configured);
  row.classList.remove('warn');
  if (b.account_rub != null) {
    val.textContent = Number(b.account_rub).toFixed(2) + ' ₽';
    row.querySelector('span').textContent = 'Баланс Cloud.ru';
    row.title = 'Баланс лицевого счёта Cloud.ru' +
      (b.month_rub != null ? '\nРасход за месяц: ' + Number(b.month_rub).toFixed(2) + ' ₽' : '');
    if (Number(b.account_rub) < 100) row.classList.add('warn');
    return;
  }
  if (b.month_rub != null) {
    val.textContent = Number(b.month_rub).toFixed(2) + ' ₽';
    row.querySelector('span').textContent = 'Cloud.ru за месяц';
    row.title = 'Расход по биллинг-API Cloud.ru с начала месяца.\n' +
      'Баланс лицевого счёта Cloud.ru через API не отдаёт — смотри его\n' +
      'в личном кабинете: Биллинг → Обзор.';
    return;
  }
  const local = Number(b.local_month_rub || 0);
  val.textContent = local.toFixed(2) + ' ₽';
  row.querySelector('span').textContent = 'Расход 30д';
  row.title = b.configured
    ? 'Биллинг-API недоступен' + (b.error ? ': ' + b.error : '') + '. Показан мой собственный подсчёт.'
    : 'Моя оценка расхода за 30 дней. Подключи биллинг Cloud.ru в Настройках, ' +
      'чтобы видеть данные из личного кабинета.';
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
    item.innerHTML = '<span class="chat-title">' + esc(c.title || 'Диалог') + '</span>' +
      '<span class="chat-acts"><i class="chat-r" title="Переименовать">✎</i>' +
      '<i class="chat-x" title="Удалить">✕</i></span>';

    item.querySelector('.chat-r').addEventListener('click', (e) => {
      e.stopPropagation();
      startRenameChat(item, c);
    });
    item.querySelector('.chat-x').addEventListener('click', (e) => {
      e.stopPropagation();
      confirmBox('Удалить диалог?',
        'Диалог «' + esc(c.title || 'Диалог') + '» и все его файлы будут удалены безвозвратно.', () => {
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
  const label = item.querySelector('.chat-title');
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
  S.fdir = '';
  $('#stream').innerHTML = '';
  $('#stream').appendChild(buildWelcome());
  loadChats();
  showView('chat');
  $('#input').focus();
}
$('#newChatBtn').addEventListener('click', newChat);

async function openChat(id) {
  if (S.camStream) stopCam();
  // Уходим из диалога во время ответа: генерацию НЕ обрываем — сервер доведёт
  // её до конца и сохранит в переписку. Просто отпускаем интерфейс.
  if (S.streaming && id !== S.chatId) {
    S.detached = S.chatId;
    setStreaming(false);
  }
  S.sanctionNodes = {};
  S.chatId = id;
  S.fdir = '';
  showView('chat');
  const r = await api('/api/messages?chat_id=' + encodeURIComponent(id));
  const stream = $('#stream'); stream.innerHTML = '';
  renderMessages(stream, r.messages || []);
  // Показываем ПОСЛЕДНИЙ момент разговора. Одной установки scrollTop мало:
  // картинки, блоки кода и свёрнутые карточки досчитывают свою высоту уже
  // после вставки, лента становится выше — и позиция, «низ» на момент
  // присвоения, оказывается серединой. Поэтому доводим прокрутку до низа
  // ещё и после отрисовки кадра и после загрузки картинок.
  pinToBottom(stream);
  loadChats();
  // диалог, который дописывался в фоне: тихо перечитываем, пока не появится ответ
  if (S.detached === id) watchDetached(id);
}

/* Отрисовка переписки в заданный контейнер. Вынесена из openChat, потому что
   тот же список нужно уметь перерисовать ВНУТРИ карточки камеры — иначе
   переключение версии выбрасывало разговор в основную ленту. */
function renderMessages(host, messages) {
  const prevHost = S.forceHost;
  S.forceHost = host;
  (messages || []).forEach((m) => {
    if (m.role === 'user') {
      const mt = m.meta || {};
      // выбор, отправленный панелью ```ui, в ленте не показываем — ни сейчас,
      // ни при возврате в диалог: он и был задуман бесшумным
      if (mt.silent) return;
      addUserMsg(m.content, mt.attachments || [],
        { id: m.id, versions: mt.versions || [], version: mt.version || 0, ts: m.created_at });
    }
    else if (m.role === 'assistant') {
      const node = addAiMsg(m.created_at);
      node.root.dataset.msgId = m.id;
      const meta = m.meta || {};
      // ход мыслей и действия из прошлого ответа — свёрнутыми строчками
      restoreTrace(node, meta);
      node.body.appendChild(el('div', 'md', MD.render(m.content)));
      foldCodeBlocks(node.body);
      mountUiPanels(node.body);
      if (meta.model) node.modelEl.textContent = meta.model;
      (meta.files || []).forEach((f) => attachFileChip(node.body, f));
      addMsgActions(node, m.content);
    }
  });
  S.forceHost = prevHost;
  // Варианты продолжения принадлежат последнему ответу Джарвиса. Возвращаясь
  // в диалог, пользователь должен видеть их снова — иначе они выглядели бы
  // как одноразовая мелочь, исчезающая при любом переключении.
  if (!prevHost) {
    const msgs = messages || [];
    const last = msgs[msgs.length - 1];
    const items = last && last.role === 'assistant' ? ((last.meta || {}).replies || []) : [];
    showReplies(items);
  }
}

/* Ответ дописывается на сервере, а мы уже в другом диалоге. Периодически
   перечитываем переписку: как только ассистент договорил — показываем. */
function watchDetached(id) {
  clearTimeout(S.detachTimer);
  let tries = 0;
  const tick = async () => {
    if (S.chatId !== id) return;
    const r = await api('/api/messages?chat_id=' + encodeURIComponent(id));
    const msgs = r.messages || [];
    const last = msgs[msgs.length - 1];
    if (last && last.role === 'assistant') {
      S.detached = null;
      if (!$$('.msg-ai', stream()).length || stream().lastElementChild.classList.contains('msg-user')) {
        openChat(id);
      }
      return;
    }
    if (++tries < 120) S.detachTimer = setTimeout(tick, 1500);
  };
  S.detachTimer = setTimeout(tick, 1200);
}

/* Подсказки на пустом экране. Сервер отдаёт их ГОТОВЫМИ (считает заранее в
   фоне), поэтому новый диалог открывается мгновенно. Держим последний ответ
   в памяти вкладки — тогда даже первого запроса ждать не нужно. */
const SUGGESTIONS = [
  ['Что нового?', 'Найди в интернете 5 главных новостей за сегодня и сделай сводку'],
  ['Собери отчёт', 'Собери таблицу с ценами на iPhone 17 в российских магазинах и сохрани в Excel'],
  ['Каждое утро', 'Каждый день в 9:00 присылай мне погоду и курс доллара в Telegram'],
  ['Сделай картинку', 'Нарисуй логотип для кофейни в стиле неон-минимализм'],
  ['Разбери файл', 'Я пришлю документ — вытащи главное и сделай выжимку по пунктам'],
  ['Наведи порядок', 'Загляни в мою песочницу, разложи файлы по папкам и скажи, что можно удалить'],
];
S.ideas = SUGGESTIONS.map((s) => ({ title: s[0], prompt: s[1] }));

async function loadIdeas() {
  try {
    const r = await api('/api/ideas');
    if (r.ok && (r.ideas || []).length) {
      S.ideas = r.ideas;
      // если пустой экран уже открыт — обновим карточки на месте
      const box = $('.welcome .suggestions');
      if (box) fillSuggestions(box);
    }
  } catch (e) { /* останутся встроенные */ }
}

function fillSuggestions(box) {
  box.innerHTML = '';
  S.ideas.slice(0, 6).forEach((s, i) => {
    const b = el('button', 'sugg', '<b>' + esc(s.title) + '</b>' + esc(s.prompt));
    b.style.animationDelay = (0.04 * i) + 's';
    b.addEventListener('click', () => { $('#input').value = s.prompt; autoGrow(); send(); });
    box.appendChild(b);
  });
}

function buildWelcome() {
  const w = el('div', 'welcome');
  w.innerHTML = '<div class="reactor xl"><div class="ring r1"></div><div class="ring r2"></div>' +
    '<div class="ring r3"></div><div class="core"></div></div>' +
    '<h1 class="hello">Добрый день. Я <span>JARVIS</span>.</h1>' +
    '<p class="hello-sub">Спрашивай что угодно — или включи <b>Агент</b>, и я сделаю всё сам.</p>' +
    '<div class="suggestions"></div>';
  fillSuggestions(w.querySelector('.suggestions'));
  return w;
}

/* ============================ сообщения ============================ */
function stream() { return $('#stream'); }

/* Пока камера включена, диалог идёт ВНУТРИ её вкладки: карточка не уезжает
   вверх от новых вопросов, а переписка остаётся в ней и видна, когда
   окошко разворачивают обратно. */
function msgHost() {
  // перерисовка может идти в явно заданный контейнер (например, в переписку
  // внутри карточки камеры) — тогда он важнее общих правил
  if (S.forceHost && S.forceHost.isConnected) return S.forceHost;
  const cc = $('#camChat');
  if (cc && S.camNode && S.camNode.isConnected) return cc;
  return stream();
}
function scrollDown(force) {
  // в карточке камеры прокручивается только колонка переписки: видео слева и
  // комментарий «что вижу» справа сверху закреплены и никуда не уезжают
  const cl = S.camNode && S.camNode.isConnected ? S.camNode.querySelector('.cam-chat') : null;
  if (cl) {
    const nearC = cl.scrollHeight - cl.scrollTop - cl.clientHeight < 220;
    if (nearC || force) cl.scrollTop = cl.scrollHeight;
  }
  const s = stream();
  const near = s.scrollHeight - s.scrollTop - s.clientHeight < 220;
  if (near || force) s.scrollTop = s.scrollHeight;
}
/* Приветствие с подсказками убирает ТОЛЬКО действие самого пользователя:
   отправленное сообщение или включённая камера. Раньше его сносили ещё и
   уведомления от Джарвиса с карточками подтверждения — они приходят сами,
   и подсказки исчезали из пустого диалога, хотя человек ничего не сделал. */
function killWelcome() { const w = $('.welcome'); if (w) w.remove(); }

/* Удержать ленту внизу, пока её высота ещё меняется.
   Открывая диалог, мы вставляем разметку целиком, но её итоговая высота
   известна не сразу: шрифты, картинки и свёрнутые карточки досчитываются
   позже. Один scrollTop = scrollHeight в этот момент промахивается — лента
   «улетает вверх». Держим низ несколько кадров и после загрузки картинок. */
function pinToBottom(box) {
  if (!box) return;
  const put = () => { box.scrollTop = box.scrollHeight; };
  put();
  requestAnimationFrame(put);
  [60, 180, 400].forEach((ms) => setTimeout(put, ms));
  $$('img', box).forEach((img) => {
    if (img.complete) return;
    img.addEventListener('load', put, { once: true });
    img.addEventListener('error', put, { once: true });
  });
}

/* ================== версии сообщений ==================
   Правка не создаёт новую реплику: у сообщения появляется вторая версия,
   между которыми можно переключаться стрелками ‹ 2/2 ›. */

function renderVersions(node, versions, index) {
  const old = node.querySelector(':scope > .ver-switch');
  if (old) old.remove();
  if (!versions || versions.length < 2) return;

  const box = el('div', 'ver-switch');
  const prev = el('button', 'ver-btn', '‹');
  const label = el('span', 'ver-num', (index + 1) + '/' + versions.length);
  const next = el('button', 'ver-btn', '›');
  prev.disabled = index <= 0;
  next.disabled = index >= versions.length - 1;
  prev.title = 'Предыдущая версия';
  next.title = 'Следующая версия';

  const go = async (to) => {
    const id = node.dataset.msgId;
    if (!id) return;
    const r = await api('/api/messages/version', { id, index: to });
    if (!r.ok) { toast('Не получилось переключить версию', 'error'); return; }
    // как в GPT: вместе с версией вопроса возвращается и ответ на неё.
    // Перерисовываем в ТОТ контейнер, где сообщение живёт сейчас: в карточке
    // камеры — внутрь неё, иначе разговор выпрыгивал в основную ленту.
    const host = node.closest('.cam-chat') || (S.chatId ? stream() : null);
    if (S.chatId && host) {
      const msgs = await api('/api/messages?chat_id=' + encodeURIComponent(S.chatId));
      host.innerHTML = '';
      renderMessages(host, msgs.messages || []);
      pinToBottom(host);
    } else {
      const bubble = node.querySelector('.bubble-user');
      if (bubble) bubble.textContent = versions[to];
      renderVersions(node, versions, to);
    }
    beep(600, 0.05);
  };
  prev.addEventListener('click', () => go(index - 1));
  next.addEventListener('click', () => go(index + 1));

  box.appendChild(prev); box.appendChild(label); box.appendChild(next);
  node.appendChild(box);
}

/* Чистый текст реплики из пузыря.
   В пузыре, кроме самого текста, лежат служебные узлы: бейдж времени и строки
   вложений. Раньше правка брала bubble.textContent целиком — и время «19:42»
   приклеивалось к тексту, уезжало в модель и стиралось вместе с ним при
   удалении. Берём только текстовые узлы верхнего уровня. */
function bubbleText(bubble) {
  if (!bubble) return '';
  let out = '';
  bubble.childNodes.forEach((n) => {
    if (n.nodeType === 3) out += n.nodeValue;
    else if (n.nodeType === 1 && !n.classList.contains('msg-time') &&
             !n.classList.contains('att-line')) out += n.textContent;
  });
  return out.trim();
}

/* ============ правка сообщения прямо в пузыре (как в GPT/DeepSeek) ============
   Пузырь превращается в textarea с кнопками «Отмена» и «Сохранить».
   Сохранение отправляет запрос заново и добавляет вторую версию сообщения. */
function startInlineEdit(node, text) {
  if (node.querySelector('.edit-box')) return;
  const bubble = node.querySelector('.bubble-user');
  const acts = node.querySelector('.msg-actions');
  const vers = node.querySelector('.ver-switch');
  if (bubble) bubble.style.display = 'none';
  if (acts) acts.style.display = 'none';
  if (vers) vers.style.display = 'none';

  const box = el('div', 'edit-box');
  const ta = document.createElement('textarea');
  ta.className = 'edit-ta';
  ta.value = text;
  const row = el('div', 'edit-row');
  const cancel = el('button', 'ebtn', 'Отмена');
  const save = el('button', 'ebtn primary', 'Сохранить и отправить');
  row.appendChild(cancel); row.appendChild(save);
  box.appendChild(ta); box.appendChild(row);
  node.insertBefore(box, node.firstChild);

  const grow = () => { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 320) + 'px'; };
  grow();
  ta.addEventListener('input', grow);
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);

  // Вход в правку был с анимацией, а выход — мгновенным исчезновением:
  // пузырь «прыгал» обратно рывком. Закрываем тем же движением, только
  // обратным, чтобы вход и выход выглядели как одно действие.
  const restore = () => {
    box.remove();
    if (bubble) { bubble.style.display = ''; bubble.classList.add('edit-back'); }
    if (acts) acts.style.display = '';
    if (vers) vers.style.display = '';
    if (bubble) setTimeout(() => bubble.classList.remove('edit-back'), 300);
  };
  const close = (animated) => {
    if (box.dataset.closing === '1') return;
    box.dataset.closing = '1';
    if (animated === false) { restore(); return; }
    box.classList.add('edit-out');
    setTimeout(restore, 200);       // = длительность editOut в CSS
  };
  cancel.addEventListener('click', () => close(true));
  save.addEventListener('click', () => {
    const val = ta.value.trim();
    if (!val) { toast('Пустое сообщение', 'warn'); box.dataset.closing = ''; return; }
    close(false);                   // отправка — без прощальной анимации
    submitEdit(node, val);
  });
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); close(true); }
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); save.click(); }
  });
}

/* Отправить исправленный текст: он станет новой версией того же сообщения. */
async function submitEdit(node, text) {
  if (S.streaming) await stopStream();
  S.editing = { id: node.dataset.msgId || '', node };
  $('#input').value = text;
  autoGrow();
  send();
}

function addUserMsg(text, atts, info) {
  info = info || {};
  killWelcome();
  const m = el('div', 'msg msg-user');
  let extra = '';
  (atts || []).forEach((a) => {
    if (!a || a.fromCam) return;   // автокадр камеры остаётся невидимым для глаза
    extra += '<div class="att-line" style="margin-top:6px;font-size:11.5px;opacity:.75">' +
      fileIcon(a.name) + ' ' + esc(a.name) + '</div>';
  });
  m.innerHTML = '<div class="bubble-user">' + esc(text) + extra + '</div>';
  m.dataset.msgId = info.id || '';
  renderVersions(m, info.versions || [], info.version || 0);
  stampTime(m, info.ts);

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
    // правим прямо в пузыре; результат станет новой версией этого сообщения
    const cur = m.querySelector('.bubble-user');
    startInlineEdit(m, bubbleText(cur) || text);
  });
  acts.appendChild(copy); acts.appendChild(edit);
  m.appendChild(acts);

  msgHost().appendChild(m);
  scrollDown(true);
  return m;
}

function addAiMsg(ts) {
  const m = el('div', 'msg msg-ai');
  m.innerHTML =
    '<div class="ai-avatar"><div class="reactor sm" style="width:34px;height:34px">' +
    '<div class="ring r1"></div><div class="ring r2"></div><div class="core"></div></div></div>' +
    '<div class="ai-body"><div class="ai-name">JARVIS<span class="ai-model"></span></div>' +
    '<div class="ai-content"></div></div>';
  stampTime(m, ts);
  msgHost().appendChild(m);
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
  // «Ещё раз» — это переспросить то же самое, а не завести новую ветку разговора.
  // Раньше отправлялась новая реплика и лента росла дублями. Теперь открываем
  // правку исходного вопроса: ответ станет новой версией того же сообщения.
  again.addEventListener('click', () => {
    let ask = node.root.previousElementSibling;
    while (ask && !ask.classList.contains('msg-user')) ask = ask.previousElementSibling;
    if (!ask) { $('#input').value = S.lastPrompt || ''; autoGrow(); send(); return; }
    const text = bubbleText(ask.querySelector('.bubble-user')) || S.lastPrompt || '';
    // Первое нажатие даёт шанс поправить вопрос. Но если правка уже открыта,
    // человек нажал «ещё раз» второй раз подряд — значит менять он ничего не
    // собирался и ждёт повтора. Спрашивать подтверждение второй раз — держать
    // его на пустом месте: отправляем немедленно, с тем же текстом.
    const open = ask.querySelector('.edit-box');
    if (open) {
      const ta = open.querySelector('.edit-ta');
      const val = (ta && ta.value.trim()) || text;
      open.remove();
      const bubble = ask.querySelector('.bubble-user');
      const acts2 = ask.querySelector('.msg-actions');
      const vers = ask.querySelector('.ver-switch');
      if (bubble) bubble.style.display = '';
      if (acts2) acts2.style.display = '';
      if (vers) vers.style.display = '';
      submitEdit(ask, val);
      return;
    }
    startInlineEdit(ask, text);
    ask.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  });
  acts.appendChild(copy); acts.appendChild(speak); acts.appendChild(again);
  node.body.appendChild(acts);
}

/* --- составные карточки внутри ответа --- */
/* Восстановить ход мыслей и список действий у сохранённого ответа.
   Показываем сразу свёрнутыми строчками — история не теряется, но и не мешает. */
function restoreTrace(node, meta) {
  const think = (meta.thinking || '').trim();
  if (think) {
    const card = makeCard('◇', 'Ход мыслей', 'think-card', false);
    const ts = el('div', 'think-stream');
    ts.textContent = think;
    card.inner.appendChild(ts);
    node.body.appendChild(card);
    collapseToThumb(card, { cls: 'th-think', icon: '◇', title: 'Ход мыслей',
      sub: think.slice(0, 60), tag: 'свёрнут', instant: true });
  }
  (meta.trace || []).forEach((t) => {
    if (t.kind === 'plan' && (t.steps || []).length) {
      const card = makeCard('☰', 'План · ' + t.steps.length + ' шаг(ов)', 'plan-card', false);
      const list = el('ul', 'plan-list');
      t.steps.forEach((x, i) => list.appendChild(
        el('li', '', '<span class="plan-num">' + (i + 1) + '</span><span>' + esc(x) + '</span>')));
      card.inner.appendChild(list);
      node.body.appendChild(card);
      collapseToThumb(card, { cls: 'th-plan', icon: '☰', title: 'План',
        sub: t.steps.length + ' шаг(ов)', tag: 'выполнен', instant: true });
    } else if (t.kind === 'tool') {
      const label = t.label || t.name || 'инструмент';
      const card = makeCard('⚙', label, 'tool-card', false);
      if (t.args) card.inner.appendChild(el('div', 'kv', esc(JSON.stringify(t.args).slice(0, 400))));
      node.body.appendChild(card);
      collapseToThumb(card, { cls: 'th-tool', icon: '⚙', title: label,
        tag: 'готово', instant: true });
    } else if (t.kind === 'question') {
      // заданный ранее вопрос и выбранный ответ — сразу свёрнуты в строку
      const card = questionCard(t, null);
      node.body.appendChild(card);
      collapseToThumb(card, { cls: 'th-ask', icon: '?', title: 'Вопрос',
        sub: t.question || '', tag: t.answer || 'без ответа', instant: true });
    }
  });
}

function makeCard(icon, title, cls, openByDefault) {
  const card = el('div', 'panel-card ' + (cls || ''));
  card.innerHTML =
    '<div class="card-head' + (openByDefault ? ' open' : '') + '">' +
    '<span class="k">' + icon + '</span><span class="t">' + esc(title) + '</span>' +
    '<span class="chev">›</span></div>' +
    '<div class="card-body' + (openByDefault ? ' open' : '') + '"><div class="card-inner"></div></div>';
  const head = card.querySelector('.card-head');
  const body = card.querySelector('.card-body');
  const toggle = () => { head.classList.toggle('open'); body.classList.toggle('open'); };
  head.addEventListener('click', toggle);
  // Свернуть можно кликом в ЛЮБОМ месте карточки — так просил пользователь.
  // Исключение ровно одно и закрытое: интерактивное содержимое (ссылки, поля,
  // кнопки) и выделение текста, иначе карточка схлопывалась бы при попытке
  // скопировать её содержимое или нажать кнопку внутри.
  body.addEventListener('click', (e) => {
    if (window.getSelection && String(window.getSelection()).length) return;
    if (e.target.closest('a,button,input,textarea,select,label,.file-chip,.img-out')) return;
    toggle();
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
  code: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9 17.5L3.5 12 9 6.5"/><path d="M15 6.5L20.5 12 15 17.5"/></svg>',
  think: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.2l2.6 5.6 6 .7-4.5 4.1 1.3 6-5.4-3-5.4 3 1.3-6L3.4 9.5l6-.7z"/></svg>',
  shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7.5 3v5.6c0 4.6-3.1 8-7.5 9.4-4.4-1.4-7.5-4.8-7.5-9.4V6z"/></svg>',
};

/* Свернуть блок в компактную строку-миниатюру.
   Клик по миниатюре разворачивает исходный блок обратно. */
function collapseToThumb(node, opts) {
  if (!node || !node.isConnected || node.dataset.collapsed === '1') return null;
  opts = opts || {};
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
  // Сворачивается ЦЕЛОЕ сообщение JARVIS (например, окно камеры)? Тогда
  // миниатюра обязана остаться сообщением JARVIS: с его иконкой и на той же
  // вертикали, что и остальные ответы. Раньше на месте карточки появлялась
  // голая строка — она прижималась к левому краю и теряла аватар, из-за чего
  // свёрнутая камера «уезжала влево».
  const asMsg = node.classList.contains('msg-ai');
  let holder = thumb;
  if (asMsg) {
    holder = el('div', 'msg msg-ai thumb-msg');
    holder.innerHTML =
      '<div class="ai-avatar"><div class="reactor sm" style="width:34px;height:34px">' +
      '<div class="ring r1"></div><div class="ring r2"></div><div class="core"></div></div></div>' +
      '<div class="ai-body"></div>';
    holder.querySelector('.ai-body').appendChild(thumb);
  }
  const put = () => {
    if (!node.parentNode) return;
    node.parentNode.insertBefore(holder, node);
    node.style.display = 'none';
    node.classList.remove('collapsing', 'shrinking');
  };
  if (opts.instant) { put(); } else {
    // 400 мс = длительность shrinkClose с паузой посередине (см. CSS).
    // Снимем класс раньше — и «подвисание» оборвётся на полукадре.
    node.classList.add('shrinking');
    setTimeout(() => { node.classList.remove('shrinking'); put(); }, 400);
  }
  thumb.addEventListener('click', () => {
    node.style.display = '';
    node.dataset.collapsed = '0';
    holder.remove();
    // ПРИЧИНА «текст не появляется»: карточку сворачивали в миниатюру, когда её
    // тело было закрыто (max-height:0). Разворачивая миниатюру, мы возвращали
    // карточку как есть — с закрытым телом, — и пользователь видел один
    // заголовок. Текст был на месте, но требовал второго клика. Теперь
    // разворачивание миниатюры сразу раскрывает и содержимое.
    const head = node.querySelector(':scope > .card-head');
    const body = node.querySelector(':scope > .card-body');
    if (head && body) { head.classList.add('open'); body.classList.add('open'); }
    // рост из строки в полноразмерный блок — тот же объект, а не подмена
    node.classList.remove('shrinking');
    node.classList.add('unfolding');
    setTimeout(() => node.classList.remove('unfolding'), 520);   // = growOpen
    addFoldButton(node, opts);              // развернули — даём чем свернуть обратно
    node.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  });
  return thumb;
}

/* Кнопка «свернуть» в углу развёрнутого блока: любую миниатюру
   можно закрыть обратно, а не только развернуть. */
function addFoldButton(node, opts) {
  if (!node || node.querySelector(':scope > .th-fold')) return;
  node.classList.add('foldable');
  const b = el('i', 'th-fold');
  b.title = 'Свернуть (или кликни по пустому месту)';
  b.textContent = '⌃';
  const fold = (e) => {
    if (e) e.stopPropagation();
    b.remove();
    node.removeEventListener('click', bgFold);
    collapseToThumb(node, opts);
  };
  b.addEventListener('click', fold);
  // Клик в ЛЮБОМ месте области сворачивает её в миниатюру — целиться в мелкую
  // стрелку не нужно. Список исключений закрытый и короткий: то, с чем реально
  // взаимодействуют (ссылки, кнопки, поля, видео, картинки), плюс выделение
  // текста. Всё остальное — фон, по которому и сворачиваем.
  const bgOk = (t) => !t.closest('a,button,input,textarea,select,label,video,canvas,' +
    '.file-chip,.img-out,.msg-actions,.ver-switch');
  function bgFold(e) {
    if (window.getSelection && String(window.getSelection()).length) return;
    if (!bgOk(e.target)) return;
    fold();
  }
  node.addEventListener('click', bgFold);
  node.appendChild(b);
}

/* Крупные блоки кода из ответа сразу прячем в миниатюру. */
/* ========== D. Живые элементы управления прямо в ответе ==========
   Джарвис может вернуть блок ```ui ... ``` — набор строк вида
     slider Громкость 0..100 = 40
     toggle Тёмная тема = on
     tiles Куда едем: Москва | Питер | Казань
     button Запустить сборку
   Блок превращается в настоящие слайдеры/тумблеры/плитки. Значения
   пользователь крутит вживую, а «Отправить» одним сообщением возвращает
   Джарвису итог — так ответ становится инструментом, а не картинкой. */
function parseUiSpec(src) {
  const items = [];
  String(src || '').split('\n').forEach((raw) => {
    const ln = raw.trim();
    if (!ln) return;
    let m;
    if ((m = ln.match(/^slider\s+(.+?)\s+(-?\d+(?:\.\d+)?)\.\.(-?\d+(?:\.\d+)?)(?:\s+step\s+(\d+(?:\.\d+)?))?(?:\s*=\s*(-?\d+(?:\.\d+)?))?$/i))) {
      const min = parseFloat(m[2]), max = parseFloat(m[3]);
      items.push({ t: 'slider', label: m[1], min, max,
                   step: m[4] ? parseFloat(m[4]) : ((max - min) % 1 ? 0.1 : 1),
                   val: m[5] != null ? parseFloat(m[5]) : min });
    } else if ((m = ln.match(/^toggle\s+(.+?)(?:\s*=\s*(on|off|да|нет|true|false))?$/i))) {
      items.push({ t: 'toggle', label: m[1],
                   val: /^(on|да|true)$/i.test(m[2] || '') });
    } else if ((m = ln.match(/^tiles\s+(.+?)\s*:\s*(.+)$/i))) {
      items.push({ t: 'tiles', label: m[1],
                   opts: m[2].split('|').map((x) => x.trim()).filter(Boolean), val: null });
    } else if ((m = ln.match(/^button\s+(.+)$/i))) {
      items.push({ t: 'button', label: m[1] });
    }
  });
  return items;
}

function mountUiPanels(root) {
  if (!root) return;
  $$('.ui-panel', root).forEach((box) => {
    if (box.dataset.live === '1') return;
    const items = parseUiSpec(box.dataset.ui || '');
    if (!items.length) { box.remove(); return; }
    box.dataset.live = '1';
    box.innerHTML = '';

    // Каждому элементу — свой цвет по порядку. Раньше вся панель была одного
    // бирюзового оттенка и читалась как одна большая деталь; разные тона
    // сразу показывают, что это отдельные независимые органы управления.
    const HUES = ['c1', 'c2', 'c3', 'c4', 'c5'];
    items.forEach((it, idx) => {
      const row = el('div', 'ui-row ui-' + it.t + ' ' + HUES[idx % HUES.length]);
      row.style.animationDelay = (idx * 70) + 'ms';
      if (it.t === 'slider') {
        row.innerHTML = '<div class="ui-lab"><span>' + esc(it.label) +
          '</span><b class="ui-val">' + it.val + '</b></div>';
        const inp = el('input', 'ui-range');
        inp.type = 'range'; inp.min = it.min; inp.max = it.max;
        inp.step = it.step; inp.value = it.val;
        const out = row.querySelector('.ui-val');
        const paint = () => {
          const pct = ((inp.value - it.min) / (it.max - it.min || 1)) * 100;
          inp.style.setProperty('--fill', pct + '%');
        };
        inp.addEventListener('input', () => {
          it.val = parseFloat(inp.value); out.textContent = inp.value; paint();
          out.classList.remove('bump'); void out.offsetWidth; out.classList.add('bump');
        });
        paint();
        row.appendChild(inp);
      } else if (it.t === 'toggle') {
        row.innerHTML = '<span class="ui-lab-t">' + esc(it.label) + '</span>';
        const sw = el('button', 'ui-sw' + (it.val ? ' on' : ''));
        sw.innerHTML = '<i></i>';
        sw.addEventListener('click', () => {
          it.val = !it.val; sw.classList.toggle('on', it.val); blip(it.val);
        });
        row.appendChild(sw);
      } else if (it.t === 'tiles') {
        row.innerHTML = '<div class="ui-lab"><span>' + esc(it.label) + '</span></div>';
        const grid = el('div', 'ui-tiles');
        it.opts.forEach((o) => {
          const t = el('button', 'ui-tile', o);
          t.addEventListener('click', () => {
            it.val = o;
            $$('.ui-tile', grid).forEach((x) => x.classList.remove('on'));
            t.classList.add('on'); blip(true);
          });
          grid.appendChild(t);
        });
        row.appendChild(grid);
      } else {
        const b = el('button', 'ui-btn', it.label);
        b.addEventListener('click', () => {
          $('#input').value = it.label; autoGrow(); send({ silent: true });
        });
        row.appendChild(b);
      }
      box.appendChild(row);
    });

    // одна кнопка на всю панель — итог уходит Джарвису человеческим текстом
    if (items.some((x) => x.t !== 'button')) {
      const go = el('button', 'ui-send', 'Отправить выбор');
      go.addEventListener('click', () => {
        const parts = items.filter((x) => x.t !== 'button').map((x) => {
          if (x.t === 'slider') return x.label + ': ' + x.val;
          if (x.t === 'toggle') return x.label + ': ' + (x.val ? 'да' : 'нет');
          return x.label + ': ' + (x.val || '—');
        });
        $('#input').value = parts.join('\n'); autoGrow(); send({ silent: true });
        go.disabled = true; go.textContent = 'Принято';
        box.classList.add('ui-sent');
      });
      box.appendChild(go);
    }
  });
}

function foldCodeBlocks(root) {
  if (!root) return;
  $$('pre', root).forEach((pre) => {
    if (pre.dataset.folded === '1' || pre.closest('.code-block')) return;
    pre.dataset.folded = '1';
    const code = pre.textContent || '';
    const lines = code.split('\n').length;
    if (lines < 4 && code.length < 200) return;      // короткие сниппеты оставляем как есть
    const lang = pre.getAttribute('data-lang') || '';
    const wrap = el('div', 'code-block');
    pre.parentNode.insertBefore(wrap, pre);
    wrap.appendChild(pre);
    const bar = el('div', 'code-bar');
    bar.appendChild(el('span', 'cb-lang', lang || 'код'));
    const cp = el('button', 'cb-copy', 'Копировать');
    cp.addEventListener('click', (e) => {
      e.stopPropagation();
      navigator.clipboard.writeText(code).then(() => toast('Код скопирован', 'success'));
    });
    bar.appendChild(cp);
    wrap.insertBefore(bar, pre);
    collapseToThumb(wrap, {
      instant: true, cls: 'th-code inline-thumb', icon: ICO.code,
      title: lang ? 'Код · ' + lang : 'Код',
      sub: lines + ' стр. · ' + fmtSize(code.length), tag: 'развернуть',
    });
  });
}

/* ============================ отправка ============================ */
function autoGrow() {
  const t = $('#input');
  t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight, 190) + 'px';
  updateSendBtn();
}
$('#input').addEventListener('input', autoGrow);
$('#input').addEventListener('focus', () => $('#composer').classList.add('focus'));
$('#input').addEventListener('blur', () => $('#composer').classList.remove('focus'));
$('#input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
$('#sendBtn').addEventListener('click', () => {
  // стоп — только когда поле пустое; если текст набран, отправляем (прервав старый поток)
  if (S.streaming && !$('#input').value.trim() && !S.attachments.length) {
    if (S.abort) S.abort.abort();
    setStreaming(false);
    return;
  }
  send();
});

/* Прервать текущий поток и дождаться, пока интерфейс освободится. */
function stopStream() {
  return new Promise((resolve) => {
    if (!S.streaming) { resolve(); return; }
    try { if (S.abort) S.abort.abort(); } catch (e) { /* уже закрыт */ }
    let waited = 0;
    const t = setInterval(() => {
      waited += 60;
      if (!S.streaming || waited > 1800) {
        clearInterval(t);
        setStreaming(false);
        resolve();
      }
    }, 60);
  });
}

/* Вид кнопки зависит и от потока, и от того, набран ли текст. */
function updateSendBtn() {
  const btn = $('#sendBtn');
  if (!btn) return;
  const hasText = !!$('#input').value.trim() || S.attachments.length > 0;
  const stopMode = S.streaming && !hasText;
  btn.classList.toggle('stop', stopMode);
  btn.title = stopMode ? 'Остановить' : 'Отправить';
  btn.innerHTML = stopMode
    ? '<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>'
    : '<svg viewBox="0 0 24 24"><path d="M3 20l18-8L3 4v6l12 2-12 2z"/></svg>';
}

/* opts.silent — отправить, не показывая пузырь пользователя.
   Так уходит выбор из интерактивной панели ```ui: человек уже видит, что
   накрутил в самих слайдерах и плитках, а служебная простыня
   «Громкость: 40 / Тема: тёмная» в ленте — шум, разрывающий ответ. */
async function send(opts) {
  opts = opts || {};
  const input = $('#input');
  const text = input.value.trim();
  if (!text && !S.attachments.length) return;
  // предыдущий ответ ещё идёт — аккуратно прерываем и отправляем новый
  if (S.streaming) { await stopStream(); }
  if (S.streaming) return;
  S.lastPrompt = text;
  // старые варианты ответа относились к прошлой реплике — убираем сразу
  const rb = $('#replyBar');
  if (rb) { rb.hidden = true; rb.innerHTML = ''; }
  S.replyTicket = (S.replyTicket || 0) + 1;   // аннулируем незавершённый заказ подсказок
  foldAllNotes();   // диалог ожил — уведомления уплывают наверх, в ленту

  // камера включена — молча прикладываем текущий кадр, чтобы вопрос был «про то, что вижу»
  if (S.camStream && !S.attachments.some((a) => a.fromCam)) {
    const frame = await camAttachFrame();
    if (frame) { frame.fromCam = true; S.attachments.push(frame); }
  }

  // правка: подменяем текст на месте и убираем устаревший ответ ниже
  const editing = S.editing && S.editing.id ? S.editing : null;
  S.editing = null;
  if (editing && editing.node && editing.node.isConnected) {
    const bubble = editing.node.querySelector('.bubble-user');
    // подменяем ТОЛЬКО текст: бейдж времени и строки вложений — служебные узлы,
    // и присваивание textContent стирало их вместе с текстом
    if (bubble) {
      const keep = Array.from(bubble.childNodes).filter(
        (n) => n.nodeType === 1 && (n.classList.contains('msg-time') || n.classList.contains('att-line')));
      bubble.textContent = text;
      keep.forEach((n) => bubble.appendChild(n));
    }
    let sib = editing.node.nextElementSibling;
    while (sib) { const nx = sib.nextElementSibling; sib.remove(); sib = nx; }
  } else if (!opts.silent) {
    addUserMsg(text, S.attachments);
  }
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
    verbose: true,
    mdEl: null,
    buffer: '',
    shown: '',
    typer: null,
    onTyped: null,
    tools: {},
    silent: {},
    files: [],
  };
  ui.statusEl = el('div', 'thinking-line');
  node.body.appendChild(ui.statusEl);
  thinkMode(ui, 'Соединяюсь');

  S.abort = new AbortController();
  try {
    const res = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: S.abort.signal,
      body: JSON.stringify({
        chat_id: S.chatId, text,
        edit_of: editing ? editing.id : '',
        agent_mode: S.agentMode,
        computer_use: S.computerUse,
        silent: !!opts.silent,
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
      typerStop(ui);
      if (ui.mdEl) { ui.mdEl.classList.remove('typing'); ui.mdEl.innerHTML = MD.render(ui.shown || ui.buffer); }
      dropStatus(ui);
      node.body.appendChild(el('div', 'muted', 'Остановлено.'));
    }
  } finally {
    // страховка: что бы ни случилось со стримом (обрыв, ошибка разбора,
    // закрытие сокета) — кнопка обязана вернуться в исходное состояние
    dropStatus(ui);
    setStreaming(false);
    S.abort = null;
  }
  refreshState();
  loadChats();
  fetchReplies();   // подсказки — уже после того, как ответ закрыт
}

/* Варианты продолжения тянем отдельным запросом. Пока их считают, полоса
   показывает мерцающие заглушки: пусто было бы похоже на «ничего не будет». */
async function fetchReplies() {
  const box = $('#replyBar');
  if (!box || !S.chatId) return;
  const chat = S.chatId;
  // Пока подсказки считаются, пользователь может отправить своё сообщение.
  // Без метки заказа опоздавший ответ всплыл бы поверх нового разговора.
  const ticket = (S.replyTicket = (S.replyTicket || 0) + 1);
  box.hidden = false;
  box.innerHTML = '<span class="reply-skel"></span><span class="reply-skel"></span>' +
                  '<span class="reply-skel"></span>';
  // Заглушки не должны светиться дольше, чем это выглядит осмысленно.
  // Сервер ждёт модель максимум 20 с; если она молчит — убираем полосу сами,
  // не оставляя мигать «вечную загрузку».
  const bail = setTimeout(() => {
    if (S.replyTicket === ticket) showReplies([]);
  }, 22000);
  try {
    const r = await api('/api/replies', { chat_id: chat });
    if (S.replyTicket !== ticket || S.chatId !== chat) return;
    showReplies(r.items || []);
  } catch (e) {
    if (S.replyTicket === ticket) showReplies([]);
  } finally {
    clearTimeout(bail);
  }
}

/* ============ состояние «думаю» и остальные статусы ============
   Пока JARVIS молчит перед первым словом, крутилка не нужна: вместо неё
   мигает курсор — тот самый, что через мгновение поедет вместе с текстом.
   Справа от него очень мелкая подпись, которая сама себя подменяет, создавая
   ощущение, что внутри действительно что-то происходит.
   Во ВСЕХ остальных состояниях (инструмент, ожидание, шаг плана) крутилка
   возвращается: там она честно показывает работу, а не пустое ожидание. */
const THINK_QUIPS = [
  'думаю', 'взвешиваю', 'соображаю', 'подбираю слова', 'листаю память',
  'связываю мысли', 'проверяю себя', 'ищу формулировку', 'считаю варианты',
  'собираю ответ', 'сверяюсь с фактами', 'почти нашёл', 'ещё секунду',
  'кручу шестерёнки', 'раскладываю по полочкам', 'прикидываю', 'уточняю детали',
];

function thinkMode(ui, first) {
  const box = ui.statusEl;
  if (!box) return;
  stopQuips(ui);
  box.className = 'thinking-line think-wait';
  box.innerHTML = '<span class="tw-caret"></span><span class="tw-quip"></span>';
  const q = box.querySelector('.tw-quip');
  // подписи не повторяются подряд: одинаковый текст дважды выглядит как зависание
  let last = -1;
  const swap = (txt) => {
    q.classList.remove('in'); void q.offsetWidth;
    q.textContent = txt; q.classList.add('in');
  };
  swap(first || THINK_QUIPS[0]);
  ui.quipTimer = setInterval(() => {
    let i = Math.floor(Math.random() * THINK_QUIPS.length);
    if (i === last) i = (i + 1) % THINK_QUIPS.length;
    last = i;
    swap(THINK_QUIPS[i]);
  }, 2200);
}

/* Снять строку статуса — ВСЕГДА через это место: иначе таймер подписей
   продолжит тикать в фоне на удалённом узле. */
function dropStatus(ui) {
  stopQuips(ui);
  if (ui && ui.statusEl) { ui.statusEl.remove(); ui.statusEl = null; }
}

function stopQuips(ui) {
  if (ui && ui.quipTimer) { clearInterval(ui.quipTimer); ui.quipTimer = null; }
}

/* Обычный статус: крутилка + текст. Возвращена везде, кроме «думаю». */
function busyMode(ui, text) {
  const box = ui.statusEl;
  if (!box) return;
  stopQuips(ui);
  box.className = 'thinking-line';
  box.innerHTML = '<span class="spinner"></span><span>' + esc(text) + '</span>';
}

function setStreaming(on) {
  S.streaming = on;
  updateSendBtn();
  $('#composer').classList.toggle('busy', on);
  reactor(on ? 'busy' : 'idle');
}

/* A. Живое ядро: один визуальный индикатор состояния на весь интерфейс.
   'busy' — работает, 'wait' — ждёт человека, 'ok'/'err' — короткая вспышка,
   'idle' — спокойное дыхание. */
function reactor(state) {
  const b = document.body;
  const r = $('#brandReactor');
  if (state === 'ok' || state === 'err') {
    if (!r) return;
    const cls = state === 'ok' ? 'jv-ok' : 'jv-flash';
    r.classList.remove('jv-ok', 'jv-flash');
    void r.offsetWidth;                 // перезапуск анимации
    r.classList.add(cls);
    setTimeout(() => r.classList.remove(cls), 900);
    return;
  }
  b.classList.toggle('jv-busy', state === 'busy');
  b.classList.toggle('jv-wait', state === 'wait');
}

function showError(ui, msg) {
  reactor('err');
  dropStatus(ui);
  const c = el('div', 'panel-card', '<div class="card-inner" style="padding:12px 13px;color:#ffb3c1">⚠ ' + esc(msg) + '</div>');
  ui.node.body.appendChild(c);
  toast(msg, 'error', 'Ошибка');
}

/* ================== плавная печать ответа ==================
   Сервер шлёт текст кусками, а показываем мы его по буквам:
   отдельный таймер догоняет буфер со скоростью, зависящей от отставания. */
/* Такт печати. Был 16 мс со ступенчатым ускорением до «step = left/40» —
   на обычном ответе это 200+ символов в секунду, то есть текст появлялся
   блоками, а курсор не успевал прорисоваться ни разу. Теперь такт чаще, но
   шаг мельче и с потолком: печать видно как печать, а не как вспышку. */
const TYPE_MS = 12;          // такт печати

function typeInto(ui, chunk) {
  ui.buffer += chunk;
  if (ui.shown == null) ui.shown = '';
  typerStart(ui);
}

/* B. Печать с характером: на знаках препинания печать на миг замирает, как
   будто собеседник переводит дыхание. Пауза действует, только когда мы не
   отстаём — иначе догоняем ровно, паузы не должны копить задержку. */
const PAUSE_AFTER = { '.': 7, '!': 7, '?': 7, ',': 3, ';': 4, ':': 4, '\n': 5, '—': 3 };

function typerStart(ui) {
  if (ui.typer) return;
  ui.hold = 0;
  ui.typer = setInterval(() => {
    const left = ui.buffer.length - ui.shown.length;
    if (left <= 0) {
      clearInterval(ui.typer); ui.typer = null;
      if (ui.mdEl) ui.mdEl.classList.remove('typing');
      if (ui.onTyped) { const cb = ui.onTyped; ui.onTyped = null; cb(); }
      return;
    }
    if (ui.hold > 0) { ui.hold--; return; }
    // Шаг растёт с отставанием, но ограничен сверху: даже на очень длинном
    // ответе печать остаётся печатью. 4 символа за 12 мс ≈ 330 зн/с — быстро,
    // однако буквы всё ещё появляются по одной, и курсор виден.
    let step = 1;
    if (left > 1200) step = 4;
    else if (left > 400) step = 3;
    else if (left > 120) step = 2;
    ui.shown = ui.buffer.slice(0, ui.shown.length + step);
    if (ui.mdEl) {
      ui.mdEl.classList.add('typing');
      ui.mdEl.innerHTML = MD.render(ui.shown);
    }
    if (step === 1 && left < 400) {
      const last = ui.shown[ui.shown.length - 1];
      ui.hold = PAUSE_AFTER[last] || 0;
    }
    scrollDown();
  }, TYPE_MS);
}

/* дописать всё, что осталось (в конце ответа) */
function typerFlush(ui) {
  if (ui.shown == null) ui.shown = '';
  typerStart(ui);
}

function typerStop(ui) {
  if (ui.typer) { clearInterval(ui.typer); ui.typer = null; }
  ui.onTyped = null;
}

/* ================== голос Джарвиса ================== */
function voiceOn() { return localStorage.getItem('jarvisVoice') === '1'; }

/* Единственная точка переключения голоса: и кнопка в шапке, и тумблер в
   настройках зовут её — иначе два источника истины разъезжаются. */
function setVoice(on) {
  localStorage.setItem('jarvisVoice', on ? '1' : '0');
  syncVoiceBtn();
  if (on) {
    toast('Голос включён — буду озвучивать ответы', 'success', 'Голос');
    speakReply('Голос включён, сэр. Я на связи.');
  } else {
    try { speechSynthesis.cancel(); } catch (e) {}
    toast('Голос выключен', 'info', 'Голос');
  }
  api('/api/config/update', { patch: { ui: { voice_reply: on } } });
}

function syncVoiceBtn() {
  const b = $('#voiceBtn');
  if (!b) return;
  const on = voiceOn();
  b.classList.toggle('on', on);
  b.classList.toggle('off', !on);
  b.title = on ? 'Голос включён — я озвучиваю ответы' : 'Голос выключен';
}

function speakReply(text) {
  if (!voiceOn() || !text) return;
  try {
    const clean = String(text)
      .replace(/```[\s\S]*?```/g, ' ... код ... ')
      .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .replace(/https?:\/\/\S+/g, ' ссылка ')
      .replace(/[#*`>_|]/g, ' ')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 1200);
    if (!clean) return;
    const u = new SpeechSynthesisUtterance(clean);
    u.lang = 'ru-RU'; u.rate = 1.04; u.pitch = 0.95;
    const btn = $('#voiceBtn');
    if (btn) {
      btn.classList.add('speaking');
      u.onend = u.onerror = () => btn.classList.remove('speaking');
    }
    speechSynthesis.cancel();
    speechSynthesis.speak(u);
  } catch (e) { /* браузер без синтеза — молчим */ }
}

if ($('#voiceBtn')) {
  $('#voiceBtn').addEventListener('click', () => setVoice(!voiceOn()));
}

/* колокольчик в углу: открыть/закрыть центр уведомлений */
if ($('#bellBtn')) {
  $('#bellBtn').addEventListener('click', (e) => { e.stopPropagation(); toggleNotePanel(); });
  const npc = $('#npClear');
  if (npc) {
    npc.addEventListener('click', async () => {
      S.notifications = [];
      renderNotePanel();
      await api('/api/notifications/clear', {});
    });
  }
  // клик мимо панели закрывает её — как любое всплывающее меню
  document.addEventListener('click', (e) => {
    const panel = $('#notePanel');
    if (!panel || panel.hidden) return;
    if (e.target.closest('#notePanel') || e.target.closest('#bellBtn')) return;
    toggleNotePanel(false);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') toggleNotePanel(false);
  });
}

const TIER_LABEL = { nano: 'экономный', base: 'базовый', smart: 'усиленный', coder: 'кодовый', vision: 'зрение' };

function handleEvent(ev, ui) {
  const node = ui.node;
  switch (ev.type) {
    case 'chat':
      S.chatId = ev.chat_id; break;

    case 'user_msg': {
      // сервер сообщил id только что сохранённой реплики — привязываем к пузырю,
      // иначе «Редактировать» не сможет создать вторую версию
      const mine = $$('.msg-user', stream());
      const last = mine[mine.length - 1];
      if (last && !last.dataset.msgId) last.dataset.msgId = ev.id;
      break;
    }

    case 'chat_title': {
      // название диалога придумал сам JARVIS
      loadChats();
      const cur = S.chats.find((c) => c.id === ev.chat_id);
      if (!cur || cur.title !== ev.title) {
        toast('Диалог назван: ' + ev.title, 'info');
      }
      break;
    }

    case 'edited': {
      // сервер подтвердил: правка сохранена как ещё одна версия
      const target = $$('.msg-user', stream()).find((n) => n.dataset.msgId === ev.id);
      if (target) renderVersions(target, ev.versions || [], ev.version || 0);
      break;
    }

    case 'route': {
      const hint = $('#routeHint');
      hint.textContent = 'маршрут: ' + (TIER_LABEL[ev.tier] || ev.tier) + ' · ' + (ev.reason || '');
      hint.classList.add('show');
      // Кухню показываем только на сложных задачах: на «привет» и короткий
      // вопрос пользователь ждёт ответ, а не ход мыслей и терминал.
      ui.verbose = ev.verbose !== false;
      break;
    }

    case 'model':
      node.modelEl.textContent = ' · ' + (ev.model || '');
      $('#footModel').textContent = ev.model || '—';
      break;

    case 'status':
      // Состояние приходит с сервера полем phase, а не угадывается по тексту:
      // "думаю" — мигающий курсор с меняющейся подписью, всё прочее — крутилка.
      if (ev.phase === 'think') thinkMode(ui);
      else busyMode(ui, ev.text);
      break;

    case 'thinking': {
      if (!ui.verbose) break;
      if (!ui.thinkCard) {
        // карточка раскрыта сразу: мысли должны бежать на глазах, как в терминале
        ui.thinkCard = makeCard('◇', 'Ход мыслей', 'think-card live', true);
        ui.thinkCard.inner.appendChild(el('div', 'think-stream'));
        node.body.insertBefore(ui.thinkCard, ui.statusEl);
      }
      const ts = ui.thinkCard.querySelector('.think-stream');
      ts.textContent += ev.text;
      // автопрокрутка — только если пользователь сам не отлистал вверх
      const atEnd = ts.scrollHeight - ts.scrollTop - ts.clientHeight < 60;
      if (atEnd) ts.scrollTop = ts.scrollHeight;
      ui.thinkCard.setTitle('Ход мыслей <span class="muted" style="font-size:10.5px">· думаю…</span>');
      scrollDown();
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
        busyMode(ui, 'Готовлю инструмент: ' + ev.name);
      }
      break;

    case 'tool_start': {
      // «Глаза» агента (снимок экрана, параметры экрана) — служебные шаги.
      // Пользователю их видеть незачем: он просил результат, а не отчёт
      // о каждом кадре. Тихо запоминаем и показываем только в терминале.
      if (SILENT_TOOLS[ev.name]) {
        ui.silent[ev.id || ev.name] = true;
        if (ui.statusEl) {
          busyMode(ui, 'смотрю на экран…');
        }
        termLine('$ ' + ev.name, 'cmd');
        break;
      }
      const card = makeCard('⚙', ev.label || ev.name, 'tool-card live', true);
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
      reactor('wait');
      busyMode(ui, 'Жду твоего решения…');
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
        // решение принято — карточка сразу сворачивается в миниатюру
        collapseToThumb(card, {
          instant: true, cls: d === 'approved' ? 'th-ok' : 'th-no', icon: ICO.shield,
          title: 'Санкция · ' + (ev.label || ev.tool || ''),
          tag: d === 'approved' ? 'разрешено' : 'отклонено',
        });
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
      reactor('busy');
      S.streamApproval = false;
      refreshState(); break;

    // Уточняющий вопрос с готовыми вариантами. Джарвис останавливается и ждёт,
    // пока нажмут кнопку: лучше один вопрос, чем неверная догадка.
    case 'question': {
      reactor('wait');
      busyMode(ui, 'Жду твоего ответа…');
      const card = questionCard(ev, (choice) => {
        api('/api/questions/answer', { id: ev.id, answer: choice });
      });
      node.body.insertBefore(card, ui.statusEl);
      beep(520, 0.16);
      scrollDown(true);
      break;
    }

    case 'replies':          // старый путь, оставлен для совместимости
      showReplies(ev.items || []);
      break;

    case 'tool_result': {
      if (ui.silent[ev.id || ev.name]) {
        delete ui.silent[ev.id || ev.name];
        termLine((ev.result && ev.result.ok !== false ? '✓ ' : '✕ ') + ev.name, 'sys');
        break;
      }
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
        card.classList.remove('live');
        // отработал — сворачиваем в миниатюру, чтобы диалог шёл дальше
        collapseToThumb(card, {
          cls: ok ? 'th-ok' : 'th-no', icon: ICO.code,
          title: ev.label || ev.name,
          sub: ev.elapsed != null ? ev.elapsed + 'с' : '',
          tag: ok ? 'готово' : 'ошибка',
        });
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
      flyToFiles(wrap.lastElementChild, ev.name);
      beep(820, 0.1);
      break;
    }

    case 'background': {
      const when = ev.when || ev.schedule || '';
      toast('Задача «' + ev.title + '» ушла в фон' + (when ? ' · ' + when : ''), 'info', 'AUTO');
      dropStatus(ui);
      const bg = el('div', 'panel-card bg-card');
      bg.innerHTML = '<div class="card-inner" style="padding:12px 14px">' +
        '<b style="color:var(--teal)">В фоне: ' + esc(ev.title || 'задача') + '</b>' +
        (when ? '<div class="muted" style="margin-top:5px">Когда: ' + esc(when) + '</div>' : '') +
        (ev.reason ? '<div class="muted" style="margin-top:3px">' + esc(ev.reason) + '</div>' : '') +
        '<div class="muted" style="margin-top:6px">Результат придёт уведомлением и во вкладке AUTO.</div>' +
        '</div>';
      node.body.appendChild(bg);
      refreshState();
      scrollDown();
      break;
    }

    case 'delta': {
      if (!ui.mdEl) {
        dropStatus(ui);
        if (ui.thinkCard) ui.thinkCard.classList.remove('live');
        // пошёл ответ — ход мыслей сразу убираем в миниатюру, чтобы не мешал читать
        if (ui.thinkCard && ui.thinkCard.isConnected) {
          const ts0 = ui.thinkCard.querySelector('.think-stream');
          collapseToThumb(ui.thinkCard, {
            instant: true, cls: 'th-think', icon: ICO.think, title: 'Ход мыслей',
            sub: ts0 ? fmtSize((ts0.textContent || '').length) : '', tag: 'развернуть',
          });
        }
        ui.mdEl = el('div', 'md typing');
        node.body.appendChild(ui.mdEl);
      }
      typeInto(ui, ev.text);
      break;
    }

    case 'reset': {
      // сервер понял, что модель напечатала вызов инструмента текстом,
      // и просит стереть уже показанное — начинаем ответ заново
      typerStop(ui);
      ui.buffer = ''; ui.shown = '';
      if (ui.mdEl) { ui.mdEl.remove(); ui.mdEl = null; }
      if (!ui.statusEl) {
        ui.statusEl = el('div', 'thinking-line');
        node.body.appendChild(ui.statusEl);
        busyMode(ui, 'Переигрываю: беру инструмент…');
      }
      break;
    }

    case 'done': {
      dropStatus(ui);
      const content = ev.content || ui.buffer;
      if (!ui.mdEl) { ui.mdEl = el('div', 'md'); node.body.appendChild(ui.mdEl); }
      // догоняем печать: остаток дописываем плавно, финальную отделку делаем в конце
      ui.buffer = content;
      ui.onTyped = () => {
        ui.mdEl.classList.remove('typing');
        ui.mdEl.innerHTML = MD.render(content);
        foldCodeBlocks(ui.mdEl);
        mountUiPanels(ui.mdEl);
        $$('.img-out', ui.mdEl).forEach((im) => im.addEventListener('click', () => lightbox(im.src)));
        // ход мыслей отработал — прячем в миниатюру
        if (ui.thinkCard && ui.thinkCard.isConnected) {
          const ts = ui.thinkCard.querySelector('.think-stream');
          collapseToThumb(ui.thinkCard, {
            instant: true, cls: 'th-think', icon: ICO.think, title: 'Ход мыслей',
            sub: ts ? fmtSize((ts.textContent || '').length) : '', tag: 'развернуть',
          });
        }
        if (ui.planCard && ui.planCard.isConnected) {
          collapseToThumb(ui.planCard, {
            instant: true, cls: 'th-plan', icon: ICO.think,
            title: 'План · ' + ui.planItems.length + ' шаг(ов)', tag: 'выполнен',
          });
        }
        addMsgActions(node, content);
        speakReply(content);
        scrollDown();
      };
      typerFlush(ui);
      ui.planItems.forEach((li) => li.classList.add('done'));
      beep(760, 0.13);
      $('#routeHint').classList.remove('show');
      scrollDown();
      break;
    }

    case 'error':
      showError(ui, ev.error || 'неизвестная ошибка');
      break;

    case 'end':
      dropStatus(ui);
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
  // Кадр с камеры прикладывается сам и в списке вложений не показывается:
  // пользователь и так видит себя в окне камеры, а плашка «camera_….jpg»
  // появлялась при каждой реплике и только мозолила глаза.
  S.attachments.filter((a) => !a.fromCam).forEach((a) => {
    const i = S.attachments.indexOf(a);
    const n = el('div', 'att');
    n.innerHTML = (a.kind === 'image' && a.data ? '<img src="' + a.data + '">' : '<span>' + fileIcon(a.name) + '</span>') +
      '<b>' + esc(a.name) + '</b><small style="color:var(--tx3)">' + fmtSize(a.size) + '</small><i class="x">✕</i>';
    n.querySelector('.x').addEventListener('click', () => { S.attachments.splice(i, 1); renderAttachments(); });
    box.appendChild(n);
  });
  updateSendBtn();
}

/* ============================ голос ============================ */
/* ---------------------------------------------------------------- микрофон
   Два пути: распознавание прямо в браузере (мгновенно, без интернета к нам)
   и запись с отправкой на сервер. Браузерный путь основной — он работает
   всегда; серверный включается, если браузер не умеет слушать сам. */

function browserASR(btn) {
  const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Rec) return false;
  if (S.asr) {
    if (S.asrStop) S.asrStop(); else { try { S.asr.stop(); } catch (e) {} }
    S.asr = null; S.asrStop = null;
    return true;
  }

  const rec = new Rec();
  rec.lang = 'ru-RU';
  rec.continuous = true;
  rec.interimResults = true;
  S.asr = rec;

  const input = $('#input');
  const basis = input.value ? input.value.replace(/\s+$/, '') + ' ' : '';
  let settled = '';

  rec.onresult = (ev) => {
    let live = '';
    for (let k = ev.resultIndex; k < ev.results.length; k++) {
      const chunk = ev.results[k][0].transcript;
      if (ev.results[k].isFinal) settled += chunk + ' ';
      else live += chunk;
    }
    input.value = (basis + settled + live).replace(/\s+/g, ' ').trimStart();
    autoGrow();
  };
  // Браузер шлёт no-speech уже через пару секунд тишины, а Safari к тому же
  // сам обрывает распознавание. Раньше это мгновенно превращалось в «не
  // расслышал» и в остановку записи. Теперь молчание — не ошибка: слушаем
  // дальше, пока пользователь сам не выключит микрофон.
  let stopping = false;
  let heard = false;
  let netFails = 0;
  rec.addEventListener('result', () => { heard = true; netFails = 0; });
  rec.onerror = (ev) => {
    if (ev.error === 'not-allowed' || ev.error === 'service-not-allowed') {
      stopping = true;
      toast('Разреши доступ к микрофону в настройках браузера', 'error');
    } else if (ev.error === 'audio-capture') {
      stopping = true;
      toast('Микрофон не найден', 'error');
    } else if (ev.error === 'network') {
      // Распознавание Chrome ходит на серверы Google. Из России они часто
      // недоступны, и тогда микрофон «слушает», но не слышит ничего.
      // Не крутим пустой цикл — молча уходим на запись с распознаванием
      // на нашей стороне. Тихо, без уведомления: для человека это один
      // непрерывный сеанс записи, а не два разных механизма.
      netFails++;
      if (netFails >= 2 && !heard) { stopping = true; S.asrFallback = true; }
    }
    /* no-speech, aborted — молча продолжаем слушать */
  };
  rec.onend = () => {
    // сам оборвался, а пользователь не просил — поднимаем заново
    if (!stopping) {
      try { rec.start(); return; } catch (e) { /* поднять не вышло — выходим */ }
    }
    S.asr = null;
    btn.classList.remove('rec');
    input.value = input.value.trim();
    autoGrow();
    if (input.value) input.focus();
    updateSendBtn();
    if (S.asrFallback) {
      S.asrFallback = false;
      S.asrTriedBrowser = true;   // чтобы сервер не отправил нас обратно
      serverASR(btn, true);   // запасной путь: пишем звук и распознаём у себя
    }
  };

  S.asrStop = () => { stopping = true; try { rec.stop(); } catch (e) {} };
  try { rec.start(); } catch (e) { S.asr = null; return false; }
  btn.classList.add('rec');
  beep(560, 0.1);
  micHint('Слушаю… нажми ещё раз, чтобы закончить');
  return true;
}

/* Подсказка микрофона — ОДНА на весь сеанс записи.
   Раньше каждый внутренний переход (браузер → сервер → распознавание) сыпал
   свой тост, и на экране вырастала лавина уведомлений об одном и том же
   действии. Теперь это одна строка, которая просто меняет текст. */
function micHint(text, kind) {
  let t = S.micToast;
  if (!t || !t.isConnected) {
    t = el('div', 'toast info');
    t.innerHTML = '<div class="ti">◆</div><div><div style="font-weight:600;margin-bottom:2px">Микрофон</div>' +
      '<div class="mic-msg"></div></div>';
    t.title = 'Кликни, чтобы убрать';
    t.addEventListener('click', () => micHintOff());
    $('#toasts').appendChild(t);
    S.micToast = t;
  }
  clearTimeout(S.micTimer);
  t.className = 'toast ' + (kind || 'info');
  t.querySelector('.ti').textContent = kind === 'error' ? '✕' : (kind === 'success' ? '✓' : '◆');
  t.querySelector('.mic-msg').textContent = text;
  if (kind) S.micTimer = setTimeout(micHintOff, 3200);
}
function micHintOff() {
  const t = S.micToast;
  clearTimeout(S.micTimer);
  S.micToast = null;
  if (!t || !t.isConnected) return;
  t.classList.add('out');
  setTimeout(() => t.remove(), 300);
}

async function serverASR(btn, silentStart) {
  if (S.recorder && S.recorder.state === 'recording') { S.recorder.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    S.recChunks = [];
    const rec = new MediaRecorder(stream);
    S.recorder = rec;
    rec.ondataavailable = (e) => S.recChunks.push(e.data);
    rec.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      S.recorder = null;
      btn.classList.remove('rec');
      const blob = new Blob(S.recChunks, { type: 'audio/webm' });
      const fr = new FileReader();
      fr.onload = async () => {
        micHint('Распознаю речь…');
        const r = await api('/api/transcribe', { audio: fr.result, language: 'ru' });
        // Ответ сервера окончательный: он больше не отправляет нас обратно в
        // браузер. Один запрос — один результат, никакого пинг-понга.
        if (r.ok && r.text) {
          $('#input').value = ($('#input').value + ' ' + r.text).trim();
          autoGrow(); $('#input').focus(); updateSendBtn();
          micHintOff();
          beep(760, 0.08);
        } else {
          micHint(r.error || 'Не удалось распознать', 'error');
        }
      };
      fr.readAsDataURL(blob);
    };
    rec.start();
    btn.classList.add('rec');
    if (!silentStart) beep(560, 0.1);
    micHint('Говори… нажми ещё раз, чтобы остановить');
  } catch (e) {
    micHint('Нет доступа к микрофону', 'error');
  }
}

$('#micBtn').addEventListener('click', function () {
  // Повторное нажатие всегда ЗАКАНЧИВАЕТ сеанс, каким бы путём он ни шёл.
  if (S.asr || (S.recorder && S.recorder.state === 'recording')) {
    if (S.asr) browserASR(this); else serverASR(this);
    return;
  }
  if (browserASR(this)) return;   // основной путь
  serverASR(this);                // запасной
});

/* ============================ камера в диалоге ============================ */
/* Камера открывается прямо в чате. Кнопок нет: JARVIS сам смотрит трансляцию —
   раз в несколько секунд берёт кадр и, если картинка изменилась, отправляет
   его зрительной модели. Так получается «живое» распознавание видео. */
const CAM_TICK = 2500;      // как часто заглядывать в кадр, мс
const CAM_MOTION = 7;       // порог изменения сцены (0..255)

// Служебные шаги computer-use: агенту нужны, пользователю — нет.
// Служебные шаги приходят с сервера (/api/state.silent_tools) — единый
// источник истины в реестре инструментов. Значения ниже нужны только до
// первого ответа сервера.
let SILENT_TOOLS = { screenshot: 1, screen_info: 1 };
function applySilentTools(list) {
  if (!Array.isArray(list) || !list.length) return;
  const next = {};
  list.forEach(n => { next[n] = 1; });
  SILENT_TOOLS = next;
}

function buildCamCard() {
  const card = el('div', 'msg msg-ai cam-msg');
  card.innerHTML =
    '<div class="ai-avatar"><div class="reactor sm" style="width:34px;height:34px">' +
    '<div class="ring r1"></div><div class="ring r2"></div><div class="core"></div></div></div>' +
    '<div class="ai-body"><div class="ai-name">JARVIS<span class="ai-model"> · зрение</span></div>' +
    '<div class="ai-content">' +
      '<div class="cam-live">' +
        '<div class="cam-col-left">' +
          '<div class="cam-wrap">' +
            '<video id="cam" autoplay playsinline muted></video>' +
            '<div class="cam-scan"></div>' +
            '<div class="cam-corners"><i></i><i></i><i></i><i></i></div>' +
            '<div class="cam-hud"><span class="cam-rec"></span><span id="camState">включаю камеру…</span></div>' +
          '</div>' +
          '<div class="cam-note muted">Смотрю трансляцию и комментирую справа. ' +
          'Спроси прямо в чате — «что это?», «где купить» — отвечу по тому, что сейчас в кадре.</div>' +
        '</div>' +
        '<div class="cam-col-right">' +
          '<div class="cam-feed" id="camFeed"></div>' +
          '<div class="cam-chat" id="camChat"></div>' +
        '</div>' +
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
  // окно камеры сворачивается кликом по любому пустому месту (не по видео)
  addFoldButton(S.camNode, { cls: 'th-cam', icon: ICO.cam, title: 'Камера', tag: 'свёрнута' });
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
  // «Что вижу» — это состояние кадра, а не переписка: каждое новое описание
  // ЗАМЕНЯЕТ предыдущее, иначе лента растёт и выдавливает картинку из вида.
  // Сообщения системы и ошибки — отдельная короткая строка, тоже одна.
  const slot = kind === 'sys' || kind === 'err' ? 'camNote' : 'camView';
  line.dataset.slot = slot;
  const old = feed.querySelector('[data-slot="' + slot + '"]');
  if (old) feed.replaceChild(line, old); else feed.appendChild(line);
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
  collapseToThumb(card, {
    instant: true, cls: ok ? 'th-ok' : 'th-no', icon: ICO.shield,
    title: 'Санкция' + (tool ? ' · ' + tool : ''),
    sub: text.replace(/[✓✕]\s*/, ''), tag: ok ? 'разрешено' : 'отклонено',
  });
}

function renderSanctions() {
  S.approvals.forEach((a) => {
    const key = String(a.id);
    if (S.shownApprovals.has(key)) return;
    // подтверждение по ходу стрима рисуется внутри ответа — второй раз не показываем
    if (S.streamApproval) { S.shownApprovals.add(key); return; }
    S.shownApprovals.add(key);
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

/* Карточка уточняющего вопроса: текст + кнопки вариантов.
   Первый вариант зелёный, последний красный, если это пара вида «да / нет» —
   такие ответы читаются мгновенно, без чтения подписей. */
function questionCard(ev, onPick) {
  const opts = ev.options || [];
  const card = el('div', 'panel-card ask-card');
  const yesNo = opts.length === 2;
  card.innerHTML =
    '<div class="ask-h"><span class="ask-i">?</span>' + esc(ev.question || '') + '</div>' +
    '<div class="ask-opts"></div>';
  const box = card.querySelector('.ask-opts');
  opts.forEach((o, i) => {
    const tone = yesNo ? (i === 0 ? ' good' : ' bad') : '';
    const b = el('button', 'ask-opt' + tone, esc(o));
    b.addEventListener('click', () => {
      if (card.dataset.done === '1') return;
      card.dataset.done = '1';
      box.innerHTML = '<span class="ask-picked">✓ ' + esc(o) + '</span>';
      reactor('busy');            // ответ получен — Джарвис снова за работой
      if (onPick) onPick(o);
      collapseToThumb(card, { cls: 'th-ask', icon: '?', title: 'Вопрос',
                              sub: ev.question || '', tag: o });
    });
    box.appendChild(b);
  });
  // если ответ уже был дан раньше (перечитываем историю) — показываем выбор
  if (ev.answer) {
    card.dataset.done = '1';
    box.innerHTML = '<span class="ask-picked">✓ ' + esc(ev.answer) + '</span>';
  }
  return card;
}

/* Варианты продолжения над полем ввода.
   Обычный клик отправляет реплику сразу, клик по «карандашу» кладёт её в поле
   ввода — можно дописать своё. Готовый вариант почти всегда хочется поправить,
   и без этого подсказки превращались бы в жёсткое меню из трёх пунктов. */
function showReplies(items) {
  const box = $('#replyBar');
  if (!box) return;
  box.innerHTML = '';
  if (!items.length) { box.hidden = true; return; }
  items.forEach((t, i) => {
    const chip = el('span', 'reply-chip');
    chip.style.animationDelay = (i * 60) + 'ms';
    const go = el('button', 'rc-go', esc(t));
    const ed = el('button', 'rc-ed', '✎');
    ed.title = 'Вставить в поле ввода и дописать';
    go.addEventListener('click', () => {
      box.hidden = true;
      $('#input').value = t;
      autoGrow();
      send();
    });
    ed.addEventListener('click', (e) => {
      e.stopPropagation();
      box.hidden = true;
      const input = $('#input');
      input.value = t;
      autoGrow();
      input.focus();
      input.setSelectionRange(t.length, t.length);
    });
    chip.appendChild(go); chip.appendChild(ed);
    box.appendChild(chip);
  });
  box.hidden = false;
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
    foldNote(card, n);
  });
  return card;
}

/* Уведомление живёт в доке над полем ввода, а свернувшись — возвращается
   обычной строкой в конец ленты, на своё привычное место. Перелёт делаем
   вручную по фактическим координатам: анимация «из воздуха» выглядела бы
   как исчезновение и появление двух разных вещей. */
function foldNote(card, n) {
  if (card.dataset.folding === '1') return;
  card.dataset.folding = '1';
  api('/api/notifications/read', {});

  const from = card.getBoundingClientRect();
  const host = stream();
  const thumb = el('div', 'thumb th-note');
  const time = new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  thumb.innerHTML =
    '<span class="th-ico">' + ICO.bell + '</span>' +
    '<span class="th-t"><b>' + esc(n.title || 'Уведомление') + '</b>' +
    ((n.body || '') ? ' · ' + esc((n.body || '').slice(0, 70)) : '') + '</span>' +
    '<span class="th-tag">прочитано</span>' +
    '<span class="th-time">' + time + '</span>';
  thumb.style.visibility = 'hidden';
  host.appendChild(thumb);
  const to = thumb.getBoundingClientRect();

  // карточка летит из дока на место миниатюры, уменьшаясь до её размера
  const ghost = card.cloneNode(true);
  ghost.classList.add('note-fly');
  ghost.style.cssText += ';position:fixed;left:' + from.left + 'px;top:' + from.top +
    'px;width:' + from.width + 'px;margin:0;z-index:120;pointer-events:none';
  document.body.appendChild(ghost);
  card.remove();
  requestAnimationFrame(() => {
    ghost.style.transform = 'translate(' + (to.left - from.left) + 'px,' +
      (to.top - from.top) + 'px) scale(' + Math.max(0.35, to.width / from.width) + ')';
    ghost.style.opacity = '0';
  });
  setTimeout(() => { ghost.remove(); thumb.style.visibility = ''; scrollDown(); }, 380);
}

function noteDock() { return $('#noteDock'); }

/* C. Готовый файл летит к пункту «Файлы» в меню и там растворяется — не
   приземляется, а тает: пользователь понимает, куда файл ушёл, но экран не
   получает лишнего объекта. Летит копия, оригинальная плашка остаётся в чате. */
function flyToFiles(chip, name) {
  const target = document.querySelector('.nav-item[data-view="files"]');
  if (!chip || !target || !chip.getBoundingClientRect) { toast((name || 'файл') + ' готов', 'success', 'Файл'); return; }
  const a = chip.getBoundingClientRect();
  const b = target.getBoundingClientRect();
  if (!a.width || !b.width) { toast((name || 'файл') + ' готов', 'success', 'Файл'); return; }

  const fly = el('div', 'file-fly');
  fly.textContent = '📄 ' + (name || 'файл');
  fly.style.left = a.left + 'px';
  fly.style.top = a.top + 'px';
  fly.style.width = Math.min(a.width, 260) + 'px';
  document.body.appendChild(fly);

  // дуга: сначала вверх и вбок, потом к цели — прямой перелёт выглядит мёртвым
  const dx = (b.left + b.width / 2) - (a.left + Math.min(a.width, 260) / 2);
  const dy = (b.top + b.height / 2) - (a.top + a.height / 2);
  // Файл не срывается с места мгновенно: сначала он еле заметно приподнимается
  // над диалогом (отрыв), на миг зависает — и только потом уходит по дуге.
  // Без этой паузы движение выглядело так, будто плашку выдернули.
  fly.animate([
    { transform: 'translate(0,0) scale(1)', opacity: 1, offset: 0 },
    // отрыв: приподнялся и чуть подрос
    { transform: 'translate(0,-7px) scale(1.05)', opacity: 1, offset: .13,
      easing: 'cubic-bezier(.2,.9,.3,1)' },
    // задержка: тот же кадр повторён — висит неподвижно
    { transform: 'translate(0,-7px) scale(1.05)', opacity: 1, offset: .28,
      easing: 'cubic-bezier(.4,0,.2,1)' },
    { transform: 'translate(' + (dx * 0.45) + 'px,' + (dy * 0.35 - 46) + 'px) scale(.78)', opacity: .95, offset: .62 },
    { transform: 'translate(' + (dx * 0.92) + 'px,' + (dy * 0.92) + 'px) scale(.42)', opacity: .5, offset: .88 },
    { transform: 'translate(' + dx + 'px,' + dy + 'px) scale(.2)', opacity: 0, offset: 1 }
  ], { duration: 1180, easing: 'cubic-bezier(.3,.7,.3,1)' }).onfinish = () => {
    fly.remove();
    target.classList.add('nav-lit');
    setTimeout(() => target.classList.remove('nav-lit'), 900);
  };
}

/* Пользователь заговорил — уведомления не должны загораживать разговор:
   сворачиваем их все, они уплывают наверх в ленту. */
function foldAllNotes() {
  $$('#noteDock .note-item').forEach((c) => {
    const n = c._note || {};
    foldNote(c, n);
  });
}

/* ============ центр уведомлений в правом верхнем углу ============
   Док над полем ввода показывает только СВЕЖЕЕ и сразу уплывает в ленту.
   История же нужна отдельно и под рукой — в углу, как на телефоне: список,
   смахивание вбок для удаления и «Очистить все». */
function renderNotePanel() {
  const list = $('#npList');
  const dot = $('#bellDot');
  if (!list) return;
  const items = S.notifications || [];
  if (dot) dot.hidden = !(S.unread > 0);
  const bell = $('#bellBtn');
  if (bell) bell.classList.toggle('has-new', S.unread > 0);
  // не перерисовываем открытый список без нужды — иначе смахивание дёргается
  const sig = items.map((n) => n.id).join(',');
  if (list.dataset.sig === sig) return;
  list.dataset.sig = sig;
  list.innerHTML = '';
  if (!items.length) {
    list.appendChild(el('div', 'np-empty', 'Пока пусто'));
    return;
  }
  items.forEach((n) => list.appendChild(noteRow(n)));
}

/* Одна строка уведомления. Тянем влево — из-под неё выезжает «Удалить»,
   отпустили за половину ширины — улетает совсем, как в iOS. */
function noteRow(n) {
  const kind = n.level === 'error' ? 'error' : (n.level === 'success' ? 'success' : 'info');
  const row = el('div', 'np-item ' + kind + (n.read ? '' : ' fresh'));
  row.innerHTML =
    '<div class="np-del">Удалить</div>' +
    '<div class="np-face">' +
      '<div class="np-t">' + esc(n.title || 'Уведомление') + '</div>' +
      '<div class="np-b">' + esc((n.body || '').slice(0, 260)) + '</div>' +
      '<div class="np-time">' + fmtTime(n.created_at) + '</div>' +
    '</div>';
  const face = row.querySelector('.np-face');
  const kill = async () => {
    row.classList.add('np-gone');
    setTimeout(() => {
      row.remove();
      const list = $('#npList');
      if (list && !list.querySelector('.np-item')) {
        list.appendChild(el('div', 'np-empty', 'Пока пусто'));
      }
    }, 240);
    S.notifications = (S.notifications || []).filter((x) => String(x.id) !== String(n.id));
    await api('/api/notifications/delete', { id: n.id });
  };

  let x0 = null, dx = 0;
  const start = (e) => {
    x0 = (e.touches ? e.touches[0].clientX : e.clientX);
    dx = 0;
    face.style.transition = 'none';
  };
  const move = (e) => {
    if (x0 == null) return;
    const x = (e.touches ? e.touches[0].clientX : e.clientX);
    dx = Math.min(0, x - x0);                 // тянем только влево
    face.style.transform = 'translateX(' + dx + 'px)';
    row.classList.toggle('np-armed', dx < -row.offsetWidth * 0.45);
  };
  const end = () => {
    if (x0 == null) return;
    x0 = null;
    face.style.transition = '';
    if (dx < -row.offsetWidth * 0.45) { face.style.transform = 'translateX(-110%)'; kill(); }
    else { face.style.transform = ''; row.classList.remove('np-armed'); }
  };
  face.addEventListener('mousedown', start);
  window.addEventListener('mousemove', move);
  window.addEventListener('mouseup', end);
  face.addEventListener('touchstart', start, { passive: true });
  face.addEventListener('touchmove', move, { passive: true });
  face.addEventListener('touchend', end);
  row.querySelector('.np-del').addEventListener('click', kill);
  return row;
}

function toggleNotePanel(force) {
  const panel = $('#notePanel');
  if (!panel) return;
  const open = force != null ? force : panel.hidden;
  panel.hidden = !open;
  if (open) {
    renderNotePanel();
    // открыл — значит увидел: гасим счётчик непрочитанного
    api('/api/notifications/read', {}).then(() => { S.unread = 0; renderNotePanel(); });
  }
}

function renderNotes() {
  // при первой загрузке старые уведомления не сыплем в диалог
  if (!S.notesReady) {
    S.notifications.forEach((n) => S.shownNotes.add(String(n.id)));
    S.notesReady = true;
    return;
  }
  const dock = noteDock();
  if (!dock) return;
  const fresh = S.notifications.filter((n) => !S.shownNotes.has(String(n.id)));
  fresh.reverse().forEach((n) => {
    S.shownNotes.add(String(n.id));
    const card = noteCard(n);
    card._note = n;
    dock.appendChild(card);
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

/* ============================ ФАЙЛЫ (как Finder) ============================
   У каждого диалога — своя рабочая папка. Здесь можно ходить по папкам,
   перетаскивать файлы мышью, переименовывать, создавать папки и удалять. */

async function loadFiles(dir) {
  if (dir != null) S.fdir = dir;
  const q = '/api/files/browse?dir=' + encodeURIComponent(S.fdir || '') +
    (S.chatId ? '&chat_id=' + encodeURIComponent(S.chatId) : '');
  const r = await api(q);
  const grid = $('#fileGrid');
  if (!r.ok) { grid.innerHTML = '<div class="file-empty">' + esc(r.error || 'не удалось открыть папку') + '</div>'; return; }
  S.fdir = r.cwd || '';
  S.fparent = r.parent || '';
  renderSbxBar(r.sandbox || {}, r.entries || []);
  renderCrumbs(S.fdir);
  const entries = r.entries || [];
  grid.innerHTML = '';
  // список того, что сейчас на экране — по нему считается диапазон Shift,
  // и из выделения выпадает всё, чего в этой папке уже нет
  S.frows = entries.map((f) => f.path);
  S.fsel = new Set([...S.fsel].filter((p) => S.frows.indexOf(p) >= 0));
  if (!entries.length) {
    grid.innerHTML = '<div class="file-empty">Пусто.<br>Перетащи сюда файлы с компьютера или ' +
      'нажми «Загрузить». Здесь же появятся файлы, которые я создам.</div>';
    syncSelection();
    return;
  }
  entries.forEach((f, i) => grid.appendChild(fileCard(f, i)));
  syncSelection();
}

/* ======================= выделение файлов (как в Finder) =======================
   Один источник истины — множество путей S.fsel. Классы на карточках, панель
   действий и перетаскивание читают только его; никакой второй «памяти» о том,
   что выделено, в интерфейсе нет.
     клик            — выделить один (и открыть предпросмотр/папку),
     Cmd/Ctrl+клик   — добавить или убрать один,
     Shift+клик      — выделить диапазон от предыдущего клика,
     Cmd/Ctrl+A      — выделить всё, Escape — снять, Delete — удалить. */
function selInfo() {
  const paths = [...S.fsel];
  return { paths, count: paths.length };
}

function syncSelection() {
  $$('#fileGrid .fcard').forEach((c) => {
    c.classList.toggle('selected', S.fsel.has(c.dataset.path));
  });
  const bar = $('#selBar'), label = $('#selCount');
  if (!bar) return;
  const n = S.fsel.size;
  bar.hidden = n === 0;
  if (label) label.textContent = 'Выделено: ' + n + ' из ' + S.frows.length;
}

function selectOnly(path) {
  S.fsel = new Set(path ? [path] : []);
  S.fanchor = path || '';
  syncSelection();
}

function selectToggle(path) {
  if (S.fsel.has(path)) S.fsel.delete(path); else S.fsel.add(path);
  S.fanchor = path;
  syncSelection();
}

function selectRange(path) {
  const rows = S.frows;
  const to = rows.indexOf(path);
  let from = rows.indexOf(S.fanchor);
  if (from < 0) from = to;
  if (to < 0) return;
  const [a, b] = from <= to ? [from, to] : [to, from];
  for (let i = a; i <= b; i++) S.fsel.add(rows[i]);
  syncSelection();
}

function clearSelection() {
  if (!S.fsel.size) return;
  S.fsel.clear();
  syncSelection();
}

async function deleteSelection() {
  const { paths, count } = selInfo();
  if (!count) return;
  confirmBox(count === 1 ? 'Удалить объект?' : 'Удалить ' + count + ' объекта(ов)?',
    'Выделенное будет удалено безвозвратно.', async () => {
      const r = await api('/api/sandbox/delete_many', { paths, chat_id: S.chatId || '' });
      if (r.removed) toast('Удалено: ' + r.removed, 'success');
      if ((r.errors || []).length) toast('Не удалось удалить: ' + r.errors.length, 'error');
      clearSelection(); closeFileView(); loadFiles();
    });
}

/* Рамка выделения («лассо»), как на рабочем столе Finder.
   Тянуть можно только с пустого места сетки: на карточках висит родной
   drag-and-drop переноса файлов, и перехватывать его нельзя — иначе сломается
   перетаскивание в папки. Пока рамка растянута, считаем пересечение с
   прямоугольниками карточек: попал — выделен. Cmd/Ctrl и Shift добавляют к
   тому, что уже выделено, обычное протягивание начинает выбор заново. */
function initLasso(grid) {
  let box = null, sx = 0, sy = 0, base = null, active = false;

  const rectOf = (x1, y1, x2, y2) => ({
    left: Math.min(x1, x2), top: Math.min(y1, y2),
    right: Math.max(x1, x2), bottom: Math.max(y1, y2),
  });

  const apply = (r) => {
    const g = grid.getBoundingClientRect();
    const next = new Set(base);
    $$('#fileGrid .fcard').forEach((c) => {
      const b = c.getBoundingClientRect();
      // координаты карточки приводим к системе отсчёта сетки с учётом прокрутки
      const cl = b.left - g.left + grid.scrollLeft, ct = b.top - g.top + grid.scrollTop;
      const hit = cl < r.right && cl + b.width > r.left && ct < r.bottom && ct + b.height > r.top;
      if (hit) next.add(c.dataset.path);
    });
    S.fsel = next;
    syncSelection();
  };

  let last = null, timer = 0;

  // тянем к нижнему/верхнему краю — сетка едет сама, как в Finder
  const autoScroll = () => {
    if (!active || !last) return;
    const g = grid.getBoundingClientRect();
    const edge = 40;
    let d = 0;
    if (last.clientY > g.bottom - edge) d = Math.min(18, (last.clientY - (g.bottom - edge)) / 2);
    else if (last.clientY < g.top + edge) d = -Math.min(18, ((g.top + edge) - last.clientY) / 2);
    if (d) { grid.scrollTop += d; draw(last); }
  };

  const draw = (e) => {
    const g = grid.getBoundingClientRect();
    const x = e.clientX - g.left + grid.scrollLeft;
    const y = e.clientY - g.top + grid.scrollTop;
    const r = rectOf(sx, sy, x, y);
    box.style.left = r.left + 'px';
    box.style.top = r.top + 'px';
    box.style.width = (r.right - r.left) + 'px';
    box.style.height = (r.bottom - r.top) + 'px';
    apply(r);
  };

  const onMove = (e) => {
    const g = grid.getBoundingClientRect();
    const x = e.clientX - g.left + grid.scrollLeft;
    const y = e.clientY - g.top + grid.scrollTop;
    if (!active) {
      if (Math.abs(x - sx) < 5 && Math.abs(y - sy) < 5) return;  // это просто клик
      active = true;
      box = el('div', 'lasso');
      grid.appendChild(box);
      grid.classList.add('lassoing');
      timer = setInterval(autoScroll, 50);
    }
    last = { clientX: e.clientX, clientY: e.clientY };
    draw(e);
    e.preventDefault();
  };

  const onUp = () => {
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
    if (timer) { clearInterval(timer); timer = 0; }
    last = null;
    if (box) box.remove();
    box = null;
    grid.classList.remove('lassoing');
    // после протягивания браузер ещё пришлёт click по пустому месту — он не
    // должен сбросить только что набранное выделение
    if (active) grid.dataset.lasso = '1';
    active = false;
  };

  grid.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    if (e.target.closest('.fcard')) return;   // с карточки начинается перенос, не рамка
    const g = grid.getBoundingClientRect();
    sx = e.clientX - g.left + grid.scrollLeft;
    sy = e.clientY - g.top + grid.scrollTop;
    base = (e.metaKey || e.ctrlKey || e.shiftKey) ? new Set(S.fsel) : new Set();
    if (!base.size && S.fsel.size) { S.fsel = new Set(); syncSelection(); }
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  });
}

function downloadSelection() {
  const cards = $$('#fileGrid .fcard.selected');
  let n = 0;
  cards.forEach((c) => {
    if (c.classList.contains('dir')) return;      // папку одним файлом не скачать
    const url = c.dataset.dl;
    if (!url) return;
    n++;
    setTimeout(() => window.open(url, '_blank'), n * 250);
  });
  if (!n) toast('Папки скачиваются только по одной — открой её и выбери файлы', 'warn');
}

function renderCrumbs(dir) {
  const box = $('#crumbs');
  if (!box) return;
  box.innerHTML = '';
  const parts = (dir || '').split('/').filter(Boolean);
  // В корне «хлебные крошки» не нужны вовсе: заголовок раздела и так называется
  // «Файлы», а одинокая подсвеченная плашка выглядела лишней табличкой.
  if (!parts.length) return;
  const mk = (label, path, here) => {
    const b = el('span', 'cr' + (here ? ' here' : ''), esc(label));
    if (!here) b.addEventListener('click', () => loadFiles(path));
    // на «хлебную крошку» тоже можно бросить файл — это перенос вверх по дереву
    b.addEventListener('dragover', (e) => { e.preventDefault(); b.classList.add('drop-on'); });
    b.addEventListener('dragleave', () => b.classList.remove('drop-on'));
    b.addEventListener('drop', async (e) => {
      e.preventDefault(); e.stopPropagation();
      b.classList.remove('drop-on');
      await dropOnto(e, path);
    });
    box.appendChild(b);
  };
  // Корень называется просто «Файлы». Имя песочницы («Песочница диалога»)
  // сюда больше не подставляется: это была та самая жёлтая табличка — она
  // жила не в панели управления, а в первой «хлебной крошке», поэтому
  // прошлые правки панели её и не задевали.
  mk('Файлы', '', !parts.length);
  let acc = '';
  parts.forEach((p, i) => {
    acc = acc ? acc + '/' + p : p;
    box.appendChild(el('span', 'sep', '›'));
    mk(p, acc, i === parts.length - 1);
  });
}

function fileCard(f, i) {
  const c = el('div', 'fcard' + (f.is_dir ? ' dir' : ''));
  c.style.animationDelay = (i * 0.02) + 's';
  c.draggable = true;
  c.dataset.path = f.path;
  if (!f.is_dir) c.dataset.dl = f.download_url || '';
  const icon = f.is_dir
    ? '<div class="fi">' + fsvg('dir', 'c-any') + '</div>'
    : (isImg(f.name) ? '<img src="' + f.download_url + '" loading="lazy">'
                     : '<div class="fi">' + fileIcon(f.name) + '</div>');
  c.innerHTML = icon +
    '<div class="fn">' + esc(f.name) + '</div>' +
    '<div class="fs">' + (f.is_dir ? (f.items || 0) + ' объект(ов)' : fmtSize(f.size)) + '</div>' +
    '<div class="fx-bar"><i class="r" title="Переименовать">✎</i>' +
    (f.is_dir ? '' : '<i class="dl" title="Скачать">↓</i>') +
    '<i class="d" title="Удалить">✕</i></div>';

  c.querySelector('.r').addEventListener('click', (e) => { e.stopPropagation(); startRenameFile(c, f); });
  const dlBtn = c.querySelector('.dl');
  if (dlBtn) dlBtn.addEventListener('click', (e) => { e.stopPropagation(); window.open(f.download_url, '_blank'); });
  c.querySelector('.d').addEventListener('click', (e) => {
    e.stopPropagation();
    confirmBox(f.is_dir ? 'Удалить папку?' : 'Удалить файл?',
      '«' + esc(f.name) + '» будет удалён' + (f.is_dir ? 'а вместе со всем содержимым' : '') + ' безвозвратно.',
      async () => {
        const res = await api('/api/sandbox/delete_file', { name: f.path, chat_id: S.chatId || '' });
        if (res.ok) { toast('Удалено', 'success'); closeFileView(); loadFiles(); }
        else toast(res.error || 'не удалось удалить', 'error');
      });
  });

  c.addEventListener('click', (e) => {
    // Cmd/Ctrl и Shift — только выделение, без открытия: в Finder так же
    if (e.metaKey || e.ctrlKey) { e.preventDefault(); selectToggle(f.path); return; }
    if (e.shiftKey) { e.preventDefault(); selectRange(f.path); return; }
    selectOnly(f.path);
    if (f.is_dir) { closeFileView(); loadFiles(f.path); }
    else viewFile(f, c);
  });
  c.addEventListener('dblclick', () => { if (!f.is_dir) window.open(f.download_url, '_blank'); });

  // перетаскивание внутри песочницы: тянем всё выделенное, а не одну карточку
  c.addEventListener('dragstart', (e) => {
    // Выделение, сделанное САМИМ перетаскиванием, — служебное: пользователь
    // не ставил галочку, он просто взял файл. Запоминаем это, чтобы вернуть
    // всё как было, если перенос ничем не кончился (бросок в ту же папку).
    S.fselAuto = !S.fsel.has(f.path);
    if (S.fselAuto) selectOnly(f.path);
    const paths = [...S.fsel];
    const cards = $$('#fileGrid .fcard.selected');
    cards.forEach((n) => n.classList.add('dragging'));
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/jarvis-path', f.path);
    e.dataTransfer.setData('text/jarvis-paths', JSON.stringify(paths));
    e.dataTransfer.setData('text/plain', paths.length > 1 ? paths.length + ' объекта(ов)' : f.name);
    // Браузер рисует снимок ОДНОЙ карточки — той, за которую взялись. Поэтому
    // «оживала» одна из группы. Гасим штатный снимок и ведём свой шлейф.
    startDragGhosts(e, cards, c);
  });
  c.addEventListener('dragend', () => {
    $$('#fileGrid .fcard.dragging').forEach((n) => n.classList.remove('dragging'));
    clearDropMarks();
    stopDragGhosts();
    // перенос закончился ничем — снимаем служебную галочку, которую поставили
    // ради самого перетаскивания
    if (S.fselAuto) { S.fselAuto = false; clearSelection(); }
  });

  if (f.is_dir) {
    c.addEventListener('dragover', (e) => { e.preventDefault(); c.classList.add('drop-on'); });
    c.addEventListener('dragleave', () => c.classList.remove('drop-on'));
    c.addEventListener('drop', async (e) => {
      e.preventDefault(); e.stopPropagation();
      c.classList.remove('drop-on');
      await dropOnto(e, f.path);
    });
  }
  return c;
}

/* ============ шлейф перетаскивания: миниатюры «висят» на курсоре ============
   Штатный drag-снимок браузера показывает только карточку-источник, поэтому
   группа из нескольких файлов выглядела так, будто едет один. Прячем его
   прозрачной картинкой 1×1 и рисуем свой слой: каждая выделенная карточка
   уменьшается в миниатюру и догоняет курсор с небольшой задержкой — чем
   дальше миниатюра в стопке, тем ленивее, отсюда ощущение инерции. */
const DRAG = { ghosts: [], x: 0, y: 0, raf: 0, move: null };
const BLANK_IMG = (() => {
  const i = new Image();
  i.src = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
  return i;
})();

function startDragGhosts(e, cards, source) {
  stopDragGhosts();
  try { e.dataTransfer.setDragImage(BLANK_IMG, 0, 0); } catch (err) { /* старый браузер */ }
  const list = (cards && cards.length ? [...cards] : [source]);
  // карточка, за которую взялись, летит первой — она ближе всего к курсору
  list.sort((a, b) => (a === source ? -1 : b === source ? 1 : 0));
  const layer = el('div', 'drag-layer');
  DRAG.x = e.clientX; DRAG.y = e.clientY;

  // Стопка, как пачка фотографий в руке: верхняя ровно на курсоре, остальные
  // выглядывают из-под неё веером — сразу видно, что их несколько.
  const FAN = [
    { dx: 0, dy: 0, rot: 0 },
    { dx: -7, dy: 6, rot: -5 },
    { dx: 7, dy: 9, rot: 5 },
    { dx: -12, dy: 13, rot: -8 },
    { dx: 12, dy: 16, rot: 8 },
  ];
  DRAG.ghosts = list.slice(0, 5).map((card, i) => {
    const box = card.getBoundingClientRect();
    const g = el('div', 'drag-ghost');
    g.innerHTML = card.innerHTML;
    g.style.width = box.width + 'px';
    g.style.height = box.height + 'px';
    // верхняя карточка рисуется поверх остальных
    g.style.zIndex = String(50 - i);
    layer.appendChild(g);
    const fan = FAN[i] || FAN[FAN.length - 1];
    // стартуем из настоящего положения карточки — миниатюра «взлетает» с места
    return {
      node: g, x: box.left + box.width / 2, y: box.top + box.height / 2, vx: 0, vy: 0,
      // Пружина подобрана численно. Прежняя (k .26 / damp .78) давала на
      // рывке 140 px перелёт 62 px и успокоение за 0.72 с — размашисто.
      // Теперь 51 px и 0.55 с: шатание читается, но не мотает миниатюры.
      k: 0.32 - i * 0.02, damp: 0.72 + i * 0.01,
      dx: fan.dx, dy: fan.dy, rot: fan.rot, scale: 0.3 - i * 0.012,
    };
  });
  if (list.length > 5) {
    const more = el('div', 'drag-more', '+' + (list.length - 5));
    layer.appendChild(more);
    DRAG.ghosts.push({ node: more, x: DRAG.x, y: DRAG.y, vx: 0, vy: 0,
                       k: 0.34, damp: 0.72, dx: 0, dy: 34, rot: 0, scale: 1, plain: true });
  }
  document.body.appendChild(layer);
  DRAG.layer = layer;

  // dragover — единственное событие, где во время перетаскивания есть курсор
  DRAG.move = (ev) => { if (ev.clientX || ev.clientY) { DRAG.x = ev.clientX; DRAG.y = ev.clientY; } };
  document.addEventListener('dragover', DRAG.move, true);
  document.addEventListener('drag', DRAG.move, true);

  // Пружина со скоростью и затуханием вместо простого «догоняния». Резко
  // остановил курсор — накопленная скорость проносит миниатюру дальше и
  // раскачивает обратно: она заметно шатается, а не просто приезжает.
  const tick = () => {
    for (let i = 0; i < DRAG.ghosts.length; i++) {
      const g = DRAG.ghosts[i];
      g.vx = (g.vx + (DRAG.x + g.dx - g.x) * g.k) * g.damp;
      g.vy = (g.vy + (DRAG.y + g.dy - g.y) * g.k) * g.damp;
      g.x += g.vx;
      g.y += g.vy;
      if (g.plain) {
        g.node.style.transform = 'translate3d(' + (g.x - 12) + 'px,' + g.y + 'px,0)';
        continue;
      }
      // Наклон по скорости. Вместе с амплитудой уменьшен и крен: 20° при
      // множителе 0.8 выглядели как заваливание набок, 13° при 0.55 — как
      // естественная реакция на движение руки.
      const tilt = Math.max(-13, Math.min(13, g.vx * 0.55)) + g.rot;
      g.node.style.transform =
        'translate3d(' + g.x + 'px,' + g.y + 'px,0) translate(-50%,-50%) ' +
        'rotate(' + tilt.toFixed(2) + 'deg) scale(' + g.scale + ')';
    }
    DRAG.raf = requestAnimationFrame(tick);
  };
  DRAG.raf = requestAnimationFrame(tick);
}

/* Единая уборка подсветки. Раньше зелёную рамку снимало только событие на
   самой сетке, но бросок на папку гасит всплытие (stopPropagation) — до сетки
   оно не доходило, и подсветка залипала. Теперь метки снимаются в одном месте
   и всегда: на dragend, drop и Escape, где бы они ни случились. */
function clearDropMarks() {
  $$('.drop-root').forEach((n) => n.classList.remove('drop-root'));
  $$('.drop-on').forEach((n) => n.classList.remove('drop-on'));
}
document.addEventListener('dragend', clearDropMarks, true);
document.addEventListener('drop', clearDropMarks, true);
window.addEventListener('blur', () => { clearDropMarks(); stopDragGhosts(); });

function stopDragGhosts() {
  if (DRAG.raf) cancelAnimationFrame(DRAG.raf);
  DRAG.raf = 0;
  if (DRAG.move) {
    document.removeEventListener('dragover', DRAG.move, true);
    document.removeEventListener('drag', DRAG.move, true);
    DRAG.move = null;
  }
  if (DRAG.layer) { DRAG.layer.remove(); DRAG.layer = null; }
  DRAG.ghosts = [];
}

/* Обработка броска: либо перенос внутри песочницы, либо загрузка с компьютера. */
async function dropOnto(e, destDir) {
  const inner = e.dataTransfer.getData('text/jarvis-path');
  if (inner) {
    let paths = [inner];
    try {
      const many = JSON.parse(e.dataTransfer.getData('text/jarvis-paths') || '[]');
      if (Array.isArray(many) && many.length) paths = many;
    } catch (err) { /* тянули одну карточку */ }
    // Отбрасываем не только саму папку-приёмник, но и файлы, которые УЖЕ лежат
    // в ней: бросок «туда же» — это не перемещение, и рапортовать об успехе,
    // когда ничего не изменилось, значит врать пользователю.
    const parentOf = (p) => {
      const i = String(p).lastIndexOf('/');
      return i <= 0 ? '' : String(p).slice(0, i);
    };
    const dest = String(destDir || '').replace(/\/+$/, '');
    paths = paths.filter((p) => p && p !== dest && parentOf(p) !== dest);
    if (!paths.length) { if (S.fselAuto) { S.fselAuto = false; clearSelection(); } return; }
    S.fselAuto = false;
    const r = await api('/api/sandbox/move_many', { paths, dest: destDir, chat_id: S.chatId || '' });
    if (r.moved) toast('Перемещено: ' + r.moved, 'success');
    if ((r.errors || []).length) toast(r.errors[0].error || 'не удалось переместить', 'error');
    clearSelection();
    loadFiles();
    return;
  }
  const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
  if (files.length) await uploadToSandbox(files, destDir);
}

function startRenameFile(card, f) {
  const label = card.querySelector('.fn');
  if (!label || card.querySelector('.f-edit')) return;
  const inp = el('input', 'f-edit');
  inp.value = f.name;
  label.replaceWith(inp);
  inp.focus();
  const dot = f.is_dir ? -1 : f.name.lastIndexOf('.');
  try { inp.setSelectionRange(0, dot > 0 ? dot : f.name.length); } catch (err) {}
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const val = inp.value.trim();
    if (save && val && val !== f.name) {
      const r = await api('/api/sandbox/rename_file', { path: f.path, name: val, chat_id: S.chatId || '' });
      if (r.ok) toast('Переименовано', 'success');
      else toast(r.error || 'не удалось', 'error');
    }
    loadFiles();
  };
  inp.addEventListener('click', (e) => e.stopPropagation());
  inp.addEventListener('blur', () => finish(true));
  inp.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') finish(false);
  });
}

/* загрузка файлов с компьютера прямо в текущую папку */
async function uploadToSandbox(files, destDir) {
  let done = 0;
  for (const file of files) {
    if (file.size > 50 * 1024 * 1024) { toast(file.name + ' больше 50 МБ', 'error'); continue; }
    const data = await new Promise((res) => {
      const fr = new FileReader();
      fr.onload = () => res(fr.result);
      fr.onerror = () => res(null);
      fr.readAsDataURL(file);
    });
    if (!data) continue;
    const r = await api('/api/upload', { name: file.name, data, chat_id: S.chatId || '' });
    if (!r.ok) { toast(r.error || 'не загрузилось: ' + file.name, 'error'); continue; }
    const target = destDir != null ? destDir : (S.fdir || '');
    if (target) await api('/api/sandbox/move', { path: r.name, dest: target, chat_id: S.chatId || '' });
    done++;
  }
  if (done) toast('Загружено файлов: ' + done, 'success');
  loadFiles();
}

function renderSbxBar(info, entries) {
  const box = $('#sbxStats');
  if (!box) return;
  S.sandbox = info || {};
  // в шапке остаются только характеристики: сколько файлов, сколько места,
  // сколько объектов в текущей папке. Ярлык с названием убран.
  const stats = [
    ['файлов', String(info.files || 0)],
    ['занято', fmtSize(info.size || 0)],
  ];
  if (S.fdir) stats.push(['в этой папке', String((entries || []).length)]);
  box.innerHTML = stats.map(([k, v]) =>
    '<span class="sbx-stat"><i>' + esc(k) + '</i><b>' + esc(v) + '</b></span>').join('');
}

async function viewFile(f, card) {
  $$('.fcard.viewing').forEach((n) => n.classList.remove('viewing'));
  if (card) card.classList.add('viewing');
  const feed = $('#termFeed');
  const title = $('#termTitle');
  feed.classList.add('big');
  feed.innerHTML = '<div class="muted">открываю ' + esc(f.name) + '…</div>';
  title.textContent = 'ФАЙЛ · ' + String(f.name).toUpperCase();
  const dlBtn = $('#termDownload'), closeBtn = $('#termClose');
  dlBtn.hidden = false; closeBtn.hidden = false;
  dlBtn.onclick = () => window.open(f.download_url, '_blank');
  closeBtn.onclick = closeFileView;

  const q = '/api/files/view?name=' + encodeURIComponent(f.path || f.name) +
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
  if (!feed) return;
  feed.classList.remove('big');
  feed.innerHTML = '';
  $('#termTitle').textContent = 'ТЕРМИНАЛ';
  $('#termDownload').hidden = true;
  $('#termClose').hidden = true;
  $$('.fcard.viewing').forEach((n) => n.classList.remove('viewing'));
}

$('#refreshFiles').addEventListener('click', () => loadFiles());

$('#newFolderBtn').addEventListener('click', () => {
  promptBox('Название новой папки', 'Новая папка', async (val) => {
    const r = await api('/api/sandbox/mkdir', { name: val, parent: S.fdir || '', chat_id: S.chatId || '' });
    if (r.ok) { toast('Папка создана', 'success'); loadFiles(); }
    else toast(r.error || 'не удалось', 'error');
  });
});

$('#uploadHere').addEventListener('click', () => $('#filesUpload').click());
$('#filesUpload').addEventListener('change', (e) => {
  const files = Array.from(e.target.files || []);
  e.target.value = '';
  if (files.length) uploadToSandbox(files, S.fdir || '');
});

/* кнопки панели выделения */
if ($('#selAll')) {
  $('#selAll').addEventListener('click', () => { S.fsel = new Set(S.frows); syncSelection(); });
  $('#selDelete').addEventListener('click', deleteSelection);
  $('#selDownload').addEventListener('click', downloadSelection);
}

/* Клавиши работают, когда открыта вкладка «Файлы» и курсор не в поле ввода:
   Cmd/Ctrl+A — выделить всё, Escape — снять, Delete/Backspace — удалить. */
window.addEventListener('keydown', (e) => {
  const view = $('#view-files');
  if (!view || !view.classList.contains('active')) return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
  if ((e.metaKey || e.ctrlKey) && (e.key === 'a' || e.key === 'A')) {
    e.preventDefault(); S.fsel = new Set(S.frows); syncSelection(); return;
  }
  if (e.key === 'Escape' && S.fsel.size) { e.preventDefault(); clearSelection(); return; }
  if ((e.key === 'Delete' || e.key === 'Backspace') && S.fsel.size) {
    e.preventDefault(); deleteSelection();
  }
});

/* бросок в пустое место сетки = положить в текущую папку */
(function initGridDnd() {
  const grid = $('#fileGrid');
  if (!grid) return;
  // клик по пустому месту снимает выделение — как по рабочему столу в Finder
  grid.addEventListener('click', (e) => {
    if (grid.dataset.lasso) { delete grid.dataset.lasso; return; }
    if (e.target === grid) clearSelection();
  });
  initLasso(grid);
  grid.addEventListener('dragover', (e) => {
    e.preventDefault();
    grid.classList.add('drop-root');
  });
  grid.addEventListener('dragleave', (e) => {
    if (e.target === grid) grid.classList.remove('drop-root');
  });
  grid.addEventListener('drop', async (e) => {
    e.preventDefault();
    grid.classList.remove('drop-root');
    await dropOnto(e, S.fdir || '');
  });
})();

$('#sbxWipe').addEventListener('click', () => {
  confirmBox('Очистить рабочую папку?',
    'Все файлы этого диалога будут удалены безвозвратно. Действие нельзя отменить.', async () => {
      const r = await api('/api/sandbox/clear', { chat_id: S.chatId || '' });
      if (r.ok) { toast('Удалено файлов: ' + (r.removed || 0), 'success'); closeFileView(); loadFiles(''); }
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
      '<div class="mem-val">' + esc(m.value) + '</div>' +
      '<div class="mem-acts"><i class="mem-edit" title="Изменить">✎</i>' +
      '<i class="mem-del" title="Удалить">✕</i></div>';
    c.querySelector('.mem-del').addEventListener('click', async () => {
      await api('/api/memory/delete', { id: m.id }); loadMemory();
    });
    c.querySelector('.mem-edit').addEventListener('click', () => editMemory(m));
    grid.appendChild(c);
  });
}
/* правка факта: то же окно, что и при добавлении, но с заполненными полями */
function editMemory(m) {
  modal('<h3>Изменить факт</h3><div class="md-sub">Название и значение можно поправить.</div>' +
    '<div class="field"><label>Что</label><input id="mk"></div>' +
    '<div class="field"><label>Значение</label><textarea id="mv" rows="3"></textarea></div>' +
    '<div class="modal-acts"><button class="btn ghost" id="mc">Отмена</button>' +
    '<button class="btn primary" id="mo">Сохранить</button></div>',
    (box) => {
      const k = $('#mk', box), v = $('#mv', box);
      k.value = m.key; v.value = m.value;
      v.focus();
      $('#mc', box).addEventListener('click', closeModal);
      $('#mo', box).addEventListener('click', async () => {
        const key = k.value.trim(), val = v.value.trim();
        if (!key || !val) { toast('Заполни оба поля', 'warn'); return; }
        const r = await api('/api/memory/update', { id: m.id, key, value: val });
        if (!r.ok) { toast('Не получилось сохранить', 'error'); return; }
        closeModal(); loadMemory(); toast('Изменено', 'success');
      });
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
        sw.classList.toggle('on'); blip(sw.classList.contains('on'));
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
      sw.classList.toggle('on'); blip(sw.classList.contains('on'));
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

  // Биллинг Cloud.ru — данные из личного кабинета
  const bl = el('div', 'sset');
  const bc = c.billing || {};
  const bs = S.billing || {};
  bl.innerHTML = '<h3>Биллинг Cloud.ru</h3>' +
    '<div class="sd">Важно: Cloud.ru <b>не отдаёт баланс лицевого счёта</b> через API — ' +
    'такого метода просто нет. Доступен только <b>расход</b> за период. Сам баланс ' +
    'смотри в личном кабинете: <b>Биллинг → Обзор</b> на <b>cloud.ru</b>.</div>' +
    '<div class="sd" style="margin-top:8px">Чтобы я показывал реальный расход вместо своей ' +
    'оценки, дай ключ сервисного аккаунта. В консоли Cloud.ru: <b>Пользователи → ' +
    'Сервисные аккаунты</b> → создай аккаунт (или открой готовый) → в карточке блок ' +
    '<b>Учетные данные доступа</b> → вкладка <b>Ключи доступа</b> → <b>Создать ключ</b>. ' +
    'Key Secret показывается один раз — скопируй сразу. Аккаунту нужна роль ' +
    '<b>Администратор расходов</b> (platform.customer.expense-admin) или Администратор ' +
    'проекта, иначе придёт ошибка доступа.</div>' +
    '<div class="switch"><span>Показывать баланс и расход</span>' +
    '<div class="sw' + (bc.enabled ? ' on' : '') + '" id="bEn"></div></div>' +
    '<div class="field"><label>Key ID</label><input id="bKid" placeholder="' +
      esc(bc.key_id || 'например 1a2b3c4d-…') + '"></div>' +
    '<div class="field"><label>Секрет ключа</label><input id="bSec" type="password" placeholder="' +
      (bc.has_secret ? '••••••••  (сохранён)' : 'секрет сервисного аккаунта') + '"></div>' +
    '<div class="field"><label>ID договора (необязательно)</label><input id="bAgr" placeholder="' +
      esc(bc.agreement_id || 'agreement_id из личного кабинета') + '"></div>' +
    '<div class="sd" style="margin-top:6px">Состояние: <b style="color:' +
      (bs.account_rub != null || bs.month_rub != null ? 'var(--green)' : 'var(--tx2)') + '">' +
      esc(bs.account_rub != null ? 'баланс ' + Number(bs.account_rub).toFixed(2) + ' ₽'
          : bs.month_rub != null ? 'расход за месяц ' + Number(bs.month_rub).toFixed(2) + ' ₽'
          : bs.error ? 'ошибка: ' + bs.error
          : 'показываю собственный подсчёт') + '</b></div>' +
    '<button class="btn primary" id="saveBill">Сохранить</button> ' +
    '<button class="btn" id="testBill">Проверить</button>';
  grid.appendChild(bl);
  $('#bEn', bl).addEventListener('click', () => $('#bEn', bl).classList.toggle('on'));
  $('#saveBill', bl).addEventListener('click', async () => {
    const patch = { billing: { enabled: $('#bEn', bl).classList.contains('on') } };
    const kid = $('#bKid', bl).value.trim();
    const sec = $('#bSec', bl).value.trim();
    const agr = $('#bAgr', bl).value.trim();
    if (kid) patch.billing.key_id = kid;
    if (sec) patch.billing.key_secret = sec;
    if (agr) patch.billing.agreement_id = agr;
    const r = await api('/api/config/update', { patch });
    S.config = r.config || S.config;
    toast('Биллинг сохранён', 'success');
    await api('/api/billing?refresh=1');
    refreshState(); renderSettings();
  });
  $('#testBill', bl).addEventListener('click', async () => {
    toast('Спрашиваю Cloud.ru…', 'info');
    const r = await api('/api/billing?refresh=1');
    const b = r.billing || r;
    if (b.error) toast('Cloud.ru: ' + b.error, 'error', 'Биллинг');
    else if (b.month_rub != null) toast('Расход за месяц: ' + Number(b.month_rub).toFixed(2) + ' ₽', 'success', 'Биллинг');
    else toast('Данные не пришли — проверь ключ и роль аккаунта', 'warn', 'Биллинг');
    refreshState(); renderSettings();
  });

  // E. Интерфейс и звук
  const ui = el('div', 'sset');
  const uic = c.ui || {};
  ui.innerHTML = '<h3>Интерфейс</h3>' +
    '<div class="sd">Тихие звуки подтверждают действия, не отвлекая: щелчок переключателя, ' +
    'мягкий тон при готовом файле, низкий — при ошибке.</div>';
  const soundRow = el('div', 'switch', '<span>Интерфейсные звуки</span>');
  const soundSw = el('div', 'sw' + (uic.sound !== false ? ' on' : ''));
  soundSw.addEventListener('click', async () => {
    soundSw.classList.toggle('on');
    const on = soundSw.classList.contains('on');
    const r = await api('/api/config/update', { patch: { ui: { sound: on } } });
    S.config = r.config || S.config;
    if (on) blip(true);                      // сразу слышно, что включилось
  });
  soundRow.appendChild(soundSw); ui.appendChild(soundRow);

  const voiceRow = el('div', 'switch', '<span>Читать ответы вслух</span>');
  const voiceSw = el('div', 'sw' + (voiceOn() ? ' on' : ''));
  voiceSw.addEventListener('click', () => {
    setVoice(!voiceOn());
    voiceSw.classList.toggle('on', voiceOn());
  });
  voiceRow.appendChild(voiceSw); ui.appendChild(voiceRow);
  grid.appendChild(ui);

  // Расходы
  const us = el('div', 'sset');
  us.innerHTML = '<h3>Мой подсчёт расходов</h3><div class="sd">Сколько я сам насчитал по токенам за 24 часа.</div><div id="usageBox" class="muted">загрузка…</div>';
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
  syncVoiceBtn();
  $('#stream').appendChild(buildWelcome());
  // состояние и список диалогов тянем параллельно, а не гуськом.
  // Подсказки не ждём вовсе: экран уже показан со встроенными, а личные
  // подменятся, как только придут.
  loadIdeas();
  await Promise.all([refreshState(), loadChats()]);
  if (BOOT_DONE) BOOT_DONE();
  setInterval(refreshState, 4000);
  if (!(S.config.providers || {}).cloudru || !S.config.providers.cloudru.has_key) {
    setTimeout(() => {
      toast('Открой Настройки и вставь API-ключ, чтобы я заработал.', 'warn', 'Нужен ключ');
    }, 2600);
  }
  $('#input').focus();
})();
