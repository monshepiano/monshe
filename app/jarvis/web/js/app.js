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
  streamRun: 0,
  chatOpenRun: 0,       // поздний /messages не может перерисовать уже другой чат
  taskLoadRun: 0,
  autoPaused: false,
  scenarioActive: false, // идёт выполнение сценария: индикатор даты молчит
  agentWaveRect: null,   // rect тумблера из карточки-разрешения (до свёртки)
  followUi: null,       // текущий run и явное намерение следовать за его ростом
  attachments: [],
  config: {},
  tasks: [],
  approvals: [],
  notifications: [],
  unread: 0,
  camStream: null,
  camRun: 0,          // поколение media-запроса: поздний getUserMedia не воскресит закрытую камеру
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
  detached: null,
  detachTimer: null,
  sandbox: {},
  // выделение в файлах: набор путей + якорь для Shift-диапазона (как в Finder)
  fsel: new Set(),
  fanchor: '',
  fselAuto: false,     // выделение поставлено самим перетаскиванием, не человеком
  frows: [],
  fileViewToken: 0,
};

/* ============================ утилиты ============================ */
function api(path, body, extra) {
  const opt = body
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    : {};
  if (extra) Object.assign(opt, extra);
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
/* Telegram-подобная дата имеет два слоя: один обычный разделитель в начале
   каждого календарного дня и временный badge у верхней кромки при прокрутке.
   У самих реплик дата не повторяется — возле них остаётся только время. */
function dayKey(ts) {
  const d = new Date((Number(ts) || Date.now() / 1000) * 1000);
  return [d.getFullYear(), String(d.getMonth() + 1).padStart(2, '0'),
    String(d.getDate()).padStart(2, '0')].join('-');
}

function dayLabel(ts) {
  const sec = Number(ts) || Date.now() / 1000;
  const d = new Date(sec * 1000);
  const today = new Date();
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  const that = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const days = Math.round((start - that) / 86400000);
  if (days === 0) return 'Сегодня';
  if (days === 1) return 'Вчера';
  return d.toLocaleDateString('ru-RU', {
    day: 'numeric', month: 'long', year: d.getFullYear() === today.getFullYear() ? undefined : 'numeric',
  });
}

function scrollDayMessage(host) {
  if (!host) return null;
  const messages = $$('.msg', host).filter((node) => node.dataset && node.dataset.ts);
  if (!messages.length) return null;
  const edge = host.getBoundingClientRect().top + 10;
  // Первый элемент, нижняя грань которого ещё не ушла за верх ленты, задаёт
  // текущий день. Один линейный проход только по scroll animation frame;
  // DOM не меняется и никаких дополнительных узлов на каждый день нет.
  for (const node of messages) {
    if (node.getBoundingClientRect().bottom > edge) return node;
  }
  return messages[messages.length - 1];
}

function setupScrollDate(host, badge) {
  if (!host || !badge || host._scrollDateReady) return;
  host._scrollDateReady = true;
  let frame = 0;
  let hideTimer = 0;
  const hide = () => {
    badge.classList.remove('show');
    badge.setAttribute('aria-hidden', 'true');
  };
  const paint = () => {
    frame = 0;
    // СЦЕНАРИЙ — РАЗГОВОР ОДНОГО ДНЯ: индикатор «сегодня» не будится вовсе,
    // он объясняет переход между днями, а этапы сценария — это не дни.
    if (S.scenarioActive) { clearTimeout(hideTimer); hide(); return; }
    // Внизу дата не нужна: пользователь и так находится в сегодняшнем ходе.
    if (host.scrollHeight - host.scrollTop - host.clientHeight < 18) {
      clearTimeout(hideTimer);
      hide();
      return;
    }
    const node = scrollDayMessage(host);
    if (!node) { hide(); return; }
    badge.textContent = dayLabel(Number(node.dataset.ts));
    badge.classList.add('show');
    badge.setAttribute('aria-hidden', 'false');
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hide, 1150);
  };
  host.addEventListener('scroll', () => {
    if (!frame) frame = requestAnimationFrame(paint);
  }, { passive: true });
}

/* Возле самой реплики остаётся только время. Сервер хранит Unix timestamp в
   секундах; для живого сообщения берём текущий момент. */
function stampTime(node, ts) {
  if (!node) return null;
  const sec = Number(ts) || (Date.now() / 1000);
  node.dataset.ts = String(sec);
  node.dataset.day = dayKey(sec);
  const prev = node.querySelector('.msg-time');
  if (prev) prev.remove();
  const d = new Date(sec * 1000);
  const hhmm = d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  const t = el('span', 'msg-time', esc(hhmm));
  t.title = d.toLocaleString('ru-RU');
  // У ответа время сидит на реакторе, у своей реплики — внутри пузыря,
  // прижатое к нижнему правому углу.
  const av = node.querySelector(':scope > .ai-avatar');
  if (av) { av.appendChild(t); return t; }
  const bubble = node.querySelector(':scope > .bubble-user');
  if (bubble) { t.classList.add('in-bubble'); bubble.appendChild(t); }
  else node.appendChild(t);
  return t;
}

/* Обычный разделитель дня остаётся в истории, как в Telegram. Временная дата
   при прокрутке — отдельный слой #scrollDate; поэтому одно не подменяет другое. */
function placeDaySeparator(node) {
  if (!node || !node.parentNode || !node.dataset || !node.dataset.ts) return null;
  // СЦЕНАРИЙ — РАЗГОВОР ОДНОГО ДНЯ: пока он выполняется, разделители дат
  // («Сегодня») не появляются вовсе. Они объясняют переход между днями,
  // а этапы сценария — это не дни.
  if (S.scenarioActive) return null;
  let prev = node.previousElementSibling;
  /* BF: строки инструментов (.tool-mark) — не сообщения и не дни: после
     них следующий ответ того же дня снова рисовал разделитель даты */
  while (prev && (prev.classList.contains('day-separator') ||
                  prev.classList.contains('tool-mark'))) prev = prev.previousElementSibling;
  if (prev && prev.classList.contains('msg') && prev.dataset.day === node.dataset.day) return null;
  const immediate = node.previousElementSibling;
  if (immediate && immediate.classList.contains('day-separator') &&
      immediate.dataset.day === node.dataset.day) return immediate;
  const sep = el('div', 'day-separator', esc(dayLabel(Number(node.dataset.ts))));
  sep.dataset.day = node.dataset.day;
  sep.dataset.ts = node.dataset.ts;
  sep.setAttribute('role', 'separator');
  sep.setAttribute('aria-label', 'Дата: ' + dayLabel(Number(node.dataset.ts)));
  node.parentNode.insertBefore(sep, node);
  return sep;
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

/* BE: включение/отключение инструмента — стильная строка В ЧАТЕ, а не
   всплывашка: остаётся в ленте, и видно, КОГДА режим заработал и когда
   его сняли. Иконка режима + название + состояние, без времени.
   (Камера и микрофон уже отображаются своими карточками/областью —
   здесь только агент и компьютер.) */
/* BK: КОНЕЦ ПРЕДЛОЖЕНИЯ — граница для метки режима. Ищем последнюю
   точку/вопрос/восклицание, ЗА которой идёт пробел или конец текста
   (цифры «3.14» и сокращения без пробела не считаются). Возврат —
   индекс среза: всё до него — законченные предложения */
function lastSentenceEnd(text, from) {
  const t = String(text || '');
  const re = /[.!?…]+["'»)]*(?=\s|$)/g;
  let last = -1, m;
  while ((m = re.exec(t))) {
    if (m.index + m[0].length <= from) continue;   // уже в замороженной части
    last = m.index + m[0].length;
    re.lastIndex = m.index + m[0].length;
  }
  return last;
}

function toolLine(kind, on) {
  const box = stream();
  if (!box) return;
  const agent = kind === 'agent';
  /* BH: метка режима, включённого ПОСЕРЕДИНЕ ответа, остаётся ПОСЕРЕДИНЕ
     ответа — в месте включения, а не уезжает в конец ленты. Текст,
     напечатанный до включения, замораживаем границей: метка встанет
     ровно между ним и будущим текстом (внутри печати, слотом .md-marks) */
  let liveUi = (S.followUi && S.followUi.mdEl && S.followUi.mdEl.isConnected &&
    S.followUi.node && S.followUi.node.root &&
    S.followUi.node.root.classList.contains('live')) ? S.followUi : null;
  /* BI: печать может стоять на паузе (followUi погас), но ответ ещё
     жив — уведомление всё равно должно встать в его поток */
  if (!liveUi) {
    const cand = (S.liveUi && S.liveUi !== S.followUi) ? S.liveUi :
      (S.streaming ? S.followUi : null);
    if (cand && cand.mdEl && cand.mdEl.isConnected && cand.node &&
        cand.node.root && cand.node.root.classList.contains('live')) liveUi = cand;
  }
  /* BJ: ТЕКСТА ЕЩЁ НЕТ — типичный агентский случай: разрешение спрашивают
     ДО того, как Джарвис начал печатать. Раньше метка в этот момент
     падала в самый НИЗ ленты (append в box) и оставалась под всем
     ответом. Теперь она встаёт В ТЕЛО ответа, прямо перед строкой
     статуса — будущий текст напечатается ПОД ней, ровно после метки */
  let preText = false;
  if (!liveUi) {
    const cand = (S.streaming && S.liveUi) ? S.liveUi :
      ((S.streaming && S.followUi) ? S.followUi : null);
    if (cand && cand.node && cand.node.root &&
        cand.node.root.classList.contains('live') && !cand.mdEl) {
      liveUi = cand;
      preText = true;
    }
  }
  let markHost = null;
  if (liveUi && preText) {
    if (!liveUi.marksEl || !liveUi.marksEl.isConnected) {
      liveUi.marksEl = el('div', 'md-marks');
      const body = liveUi.node.body;
      const st = (liveUi.statusEl && body.contains(liveUi.statusEl))
        ? liveUi.statusEl : null;
      if (st) body.insertBefore(liveUi.marksEl, st);
      else body.appendChild(liveUi.marksEl);
    }
    markHost = liveUi.marksEl;
  } else if (liveUi) {
    /* BK: МЕТКА МЕЖДУ ПРЕДЛОЖЕНИЯМИ. Граница не имеет права разрезать
       слово или предложение: ищем последний ЗАВЕРШЁННОЕ предложение в
       напечатанном; если первое ещё не дописано — метка ждёт (скрыта),
       и renderTyped поставит её сразу после точки */
    const curLen = liveUi.frozen ? liveUi.frozen.src.length : 0;
    const shown = String(liveUi.shown || '');
    const k = lastSentenceEnd(shown, curLen);
    try {
      if (k > curLen) {
        const head = shown.slice(0, k);
        const mathOk = (head.match(/\\\[/g) || []).length ===
                       (head.match(/\\\]/g) || []).length;
        if (!inCodeBlock(head) && mathOk) {
          liveUi.frozen = { src: head, html: MD.render(stripSteps(head)) };
          liveUi._frozenSrc = null;   // заставить renderTyped перелить границу
          /* BI: граница ЗАМОРАЖИВАЕТСЯ НАВСЕГДА: весь будущий текст будет
             хвостом ПОСЛЕ метки — метка не сползает вниз с новой печатью */
          liveUi.freezeLocked = true;
          liveUi.freezePending = false;
        }
      } else {
        /* предложение ещё не закончено — ждём точку, метку прячем */
        liveUi.freezePending = true;
      }
    } catch (e) { /* не смогли заморозить — метка просто встанет после границы */ }
    if (!liveUi.marksEl || (liveUi.mdEl && !liveUi.mdEl.contains(liveUi.marksEl))) {
      liveUi.marksEl = el('div', 'md-marks');
      const frozen = liveUi.mdEl.querySelector('.md-frozen');
      const tail = liveUi.mdEl.querySelector('.md-tail');
      if (frozen && frozen.parentNode === liveUi.mdEl) {
        frozen.after(liveUi.marksEl);
      } else if (tail && tail.parentNode === liveUi.mdEl) {
        liveUi.mdEl.insertBefore(liveUi.marksEl, tail);
      } else {
        liveUi.mdEl.appendChild(liveUi.marksEl);
      }
    }
    /* пока граница ждёт конца предложения — метки не видно */
    liveUi.marksEl.style.display = liveUi.freezePending ? 'none' : '';
    markHost = liveUi.marksEl;
  }
  /* BF: иконка агента — РОБОТ, как в проактивном предложении о включении;
     дизайн строки скопирован с предлагашек (иконка в мягкой плитке),
     но меньше и тише */
  const row = el('div', 'tool-mark ' + (on ? 'on' : 'off') + (agent ? ' ag' : ''));
  row.innerHTML = '<span class="tm-ico">' +
    (agent
      ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="8" width="14" height="11" rx="3"/><path d="M12 8V5.4"/><circle cx="12" cy="3.6" r="1.3"/><circle cx="9.2" cy="12.6" r=".9" fill="currentColor" stroke="none"/><circle cx="14.8" cy="12.6" r=".9" fill="currentColor" stroke="none"/><path d="M9.5 16h5"/></svg>'
      : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3.4" y="4.6" width="17.2" height="12.4" rx="2.2"/><path d="M9.5 20.5h5M12 17v3.5"/></svg>') +
    '</span><span class="tm-name">' + (agent ? 'Агент' : 'Компьютер') + '</span>' +
    '<span class="tm-state">' + (on ? 'включён' : 'отключён') + '</span>';
  if (markHost) markHost.appendChild(row);
  else box.appendChild(row);
  scrollDown(false);
}

/* ================== ЗВУК ==================
   Раньше каждый звук был голой синусоидой на 5% громкости: тонко, сухо и
   почти неслышно. Ухо любит не чистый тон, а НОТУ — основной тон плюс
   обертоны, с мягкой атакой и заметным хвостом. Поэтому здесь один общий
   синтезатор: он играет аккорд из нескольких голосов через фильтр, и любой
   звук интерфейса — просто набор нот. */
function audioCtx() {
  try {
    if (!beep.ctx) beep.ctx = new (window.AudioContext || window.webkitAudioContext)();
    // браузер засыпает контекст до первого клика — будим его
    if (beep.ctx.state === 'suspended') beep.ctx.resume();
    return beep.ctx;
  } catch (e) { return null; }
}

/* AF: по умолчанию звук ЕСТЬ: нет настройки — считается включённым.
   Прежняя формула считала «нет секции ui» выключенным звуком. */
function soundOn() { const ui = S.config && S.config.ui; return !(ui && ui.sound === false); }

/* Одна нота: основной тон + октава + квинта, мягкая атака, длинный хвост.
   gain здесь ощутимо выше прежнего (было 0.05) — звук должен быть сочным. */
function tone(freq, when, dur, gain, type, glide) {
  const ctx = audioCtx();
  if (!ctx) return;
  const t = ctx.currentTime + (when || 0);
  const d = dur || 0.22;
  const vol = (gain == null ? 0.09 : gain);

  // общий фильтр: срезает резкость верхов, оставляя «тёплый» тембр
  const lp = ctx.createBiquadFilter();
  lp.type = 'lowpass';
  lp.frequency.setValueAtTime(Math.max(1200, freq * 4), t);
  lp.Q.value = 0.7;

  const bus = ctx.createGain();
  bus.gain.setValueAtTime(0.0001, t);
  bus.gain.exponentialRampToValueAtTime(vol, t + 0.012);      // атака — мягкая, но быстрая
  bus.gain.exponentialRampToValueAtTime(vol * 0.55, t + d * 0.35);
  bus.gain.exponentialRampToValueAtTime(0.0001, t + d);       // длинный хвост: звук «дышит»
  lp.connect(bus); bus.connect(ctx.destination);

  // три голоса: тон, октава сверху потише, квинта — она и даёт «смачность»
  const voices = [
    { m: 1,   g: 1.0,  ty: type || 'sine' },
    { m: 2,   g: 0.34, ty: 'sine' },
    { m: 1.5, g: 0.20, ty: 'triangle' },
  ];
  voices.forEach((v) => {
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.type = v.ty;
    o.frequency.setValueAtTime(freq * v.m, t);
    if (glide) o.frequency.exponentialRampToValueAtTime(freq * v.m * glide, t + d * 0.8);
    g.gain.value = v.g;
    o.connect(g); g.connect(lp);
    o.start(t); o.stop(t + d + 0.05);
  });
}

/* Аккорд/арпеджио: ноты с небольшим сдвигом друг за другом. Именно короткий
   сдвиг (12-45 мс) превращает набор тонов в приятный «дзинь», а не в кашу. */
function chord(freqs, opts) {
  opts = opts || {};
  const gap = opts.gap == null ? 0.028 : opts.gap;
  const dur = opts.dur || 0.3;
  const gain = opts.gain == null ? 0.085 : opts.gain;
  freqs.forEach((f, i) => tone(f, i * gap, dur - i * gap * 0.3, gain * (1 - i * 0.12), opts.type, opts.glide));
}

/* Совместимость: старые вызовы beep(частота, длительность) продолжают работать,
   но звучат уже полноценной нотой, а не писком. */
function beep(freq, dur) {
  if (!soundOn()) return;
  // ОДНА нота, а не аккорд. Аккорд здесь был ошибкой: beep зовут сериями
  // (пять строк заставки подряд через 180 мс), и хвосты накладывались друг
  // на друга в гудящий кластер — тот самый «звук из хоррора» на старте.
  // Тембр остался тёплым за счёт обертонов внутри tone().
  tone(freq || 660, 0, Math.max(dur || 0.16, 0.18), 0.075);
}

/* Щелчок переключателя: вверх — светлая терция, вниз — мягкое падение. */
function blip(up) {
  if (!soundOn()) return;
  if (up) tone(880, 0, 0.2, 0.075);
  else tone(587.33, 0, 0.2, 0.065);
}

/* Именованные звуки интерфейса: одно место, где решается «как это звучит».
   Ноты подобраны по мажорному трезвучию — оно воспринимается как «хорошо». */
/* Именованные звуки. Правило: по умолчанию ОДНА нота — короткая и негромкая.
   Аккорд оставлен только там, где событие редкое и его приятно отметить:
   ответ готов, файл улетел, камера включилась. Частые события (отправка,
   щелчок по плитке, шаг инструмента) звучат одним тоном, иначе интерфейс
   превращается в гудящий орган. */
const SFX = {
  send:    () => tone(659.25, 0, 0.19, 0.07),                                          // ми
  done:    () => chord([659.25, 987.77], { dur: 0.42, gain: 0.085, gap: 0.05 }),       // редкое — можно аккордом
  ok:      () => tone(880, 0, 0.24, 0.075),
  error:   () => chord([311.13, 233.08], { dur: 0.44, gain: 0.09, gap: 0.06, type: 'triangle' }),
  warn:    () => tone(466.16, 0, 0.28, 0.08),
  pop:     () => tone(1046.5, 0, 0.16, 0.06),
  select:  () => tone(783.99, 0, 0.18, 0.07),
  fly:     () => chord([659.25, 987.77], { dur: 0.34, gain: 0.075, gap: 0.055, glide: 1.05 }),
  note:    () => tone(1174.66, 0, 0.22, 0.065),
  start:   () => chord([523.25, 783.99], { dur: 0.4, gain: 0.085, gap: 0.06 }),
  stop:    () => chord([587.33, 392], { dur: 0.4, gain: 0.075, gap: 0.06 }),
};
function sfx(name) { if (soundOn() && SFX[name]) SFX[name](); }

function modal(html, onMount, opts) {
  const m = $('#modal');
  // SOFT-окна скроллятся ВНУТРЕННИМ слоем: раньше скроллилось само окно, и
  // градиентная рамка (::before) уезжала вместе с контентом — по краям
  // проступали резкие обрезы карточки. Рамка живёт на окне и стоит на месте.
  m.innerHTML = (opts && opts.soft) ? '<div class="jw-pane">' + html + '</div>' : html;
  // soft — лёгкие диалоги Джарвиса (предложение сценария, просмотр шагов):
  // фон затемняется вполсилы, лента под ним остаётся видна.
  m.classList.toggle('jarvis-win', !!(opts && opts.soft));
  $('#modalBack').classList.toggle('soft', !!(opts && opts.soft));
  $('#modalBack').classList.add('open');
  if (onMount) onMount(m);
}
function closeModal() {
  $('#modalBack').classList.remove('open', 'soft');
  $('#modal').classList.remove('jarvis-win');
}
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
/* Заставку видно ровно столько, сколько идут строки, и не меньше.
   Локально сервер отвечает за десятки миллисекунд, BOOT_DONE прилетал почти
   сразу и срезал заставку на середине — поэтому она «мелькала». Теперь ранний
   ответ сервера не гасит экран раньше BOOT_MIN_MS, а поздний по-прежнему
   ничего не задерживает. */
const BOOT_MIN_MS = 900;
(function boot() {
  const log = $('#bootLog');
  const t0 = Date.now();
  let i = 0;
  let finished = false;
  const hide = () => {
    if (finished) return;
    finished = true;
    $('#boot').classList.add('hide');
    $('#app').classList.add('ready');
    sfx('done');
  };
  // ждём и строки, и сервер: гасим по позднему из двух, но не раньше минимума
  const finish = () => {
    const left = BOOT_MIN_MS - (Date.now() - t0);
    if (left > 0) setTimeout(hide, left); else hide();
  };
  BOOT_DONE = finish;
  // что бы ни случилось со связью — дольше 5 секунд заставку не держим
  setTimeout(hide, 5000);
  const tick = () => {
    if (i < BOOT_LINES.length) {
      const line = el('div', '', BOOT_LINES[i]);
      log.appendChild(line); i++; tone(660, 0, 0.09, 0.03);   // тихий сухой тик, без гаммы
      setTimeout(tick, 300);
    } else {
      setTimeout(finish, 420);
    }
  };
  setTimeout(tick, 340);
})();

/* ============================ навигация ============================ */
function showView(name) {
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === 'view-' + name));
  $$('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === name));
  const titles = { chat: 'Диалог', auto: 'AUTO · фоновые задачи', files: 'Файлы',
    memory: 'Память', settings: 'Настройки', scenarios: 'Сценарии' };
  $('#topTitle').textContent = titles[name] || '';
  $('#app').classList.remove('nav-open');
  if (name === 'auto') loadTasks();
  if (name === 'scenarios') loadScenarios();
  if (name === 'files') {
    // Вход во вкладку всегда начинается с широкой сетки. Терминал — не
    // постоянная нижняя панель, а прямой предпросмотр выбранного файла.
    closeFileView();
    loadFiles();
  }
  if (name === 'memory') loadMemory();
  if (name === 'settings') renderSettings();
}
$$('.nav-item').forEach((b) => b.addEventListener('click', () => showView(b.dataset.view)));
/* ВКЛАДКА «ФАЙЛЫ» — ПРИЁМНИК ПЕРЕНОСА: бросить файл из диалога (или папку
   с рабочего стола) можно прямо на вкладку: файл уедет в песочницу, вкладка
   мигнёт — тот же сигнал, что при обычной загрузке в песочницу. */
(() => {
  const tab = $('.nav-item[data-view="files"]');
  if (!tab) return;
  tab.addEventListener('dragover', (e) => {
    const types = Array.from((e.dataTransfer && e.dataTransfer.types) || []);
    if (types.includes('Files') || types.includes('text/jarvis-path')) {
      e.preventDefault();
      tab.classList.add('drop-hot');
    }
  });
  tab.addEventListener('dragleave', () => tab.classList.remove('drop-hot'));
  tab.addEventListener('drop', async (e) => {
    e.preventDefault();
    e.stopPropagation();
    tab.classList.remove('drop-hot');
    const inner = e.dataTransfer.getData('text/jarvis-path');
    if (inner) {
      // файл из диалога: тот же жест и та же логика, что в песочнице, —
      // включая ввоз по ссылке, если пути в песочнице уже нет
      await dropOnto(e, '');
      pulseNav('files', false);
      loadFiles();
      return;
    }
    const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
    if (files.length) await uploadToSandbox(files, '');
  });
})();

/* Сохранение подсвечивает назначение, а не показывает ещё один toast. Класс
   перезапускается на каждом фактическом успехе; Memory намеренно чуть ярче. */
function pulseNav(view, strong) {
  const item = $('.nav-item[data-view="' + view + '"]');
  if (!item) return;
  item.classList.remove('save-glint', 'save-glint-strong');
  void item.offsetWidth;
  item.classList.add(strong ? 'save-glint-strong' : 'save-glint');
  setTimeout(() => item.classList.remove('save-glint', 'save-glint-strong'), 1050);
}
/* сворачивание бокового меню.
   На узком экране меню выезжает поверх (nav-open). На широком панель
   ПРЕВРАЩАЕТСЯ В ДОК — двухфазно: сначала гаснут диалоги и статистика,
   затем панель morph'ится в стеклянную пилюлю (контент занимает всю
   ширину, док парит по центру высоты). Разворачивание — в обратном
   порядке, тем же темпом. */
function isNarrow() { return window.matchMedia('(max-width:900px)').matches; }
/* ЕДИНЫЙ РИСУНОК ПРЕВРАЩЕНИЯ: всё происходит ОДНОВРЕМЕННО — диалоги
   уезжают вбок ровно в тот же такт, что панель превращается в док (и
   наоборот). Никаких фаз и таймеров: одинаковая длительность .55s,
   направления различаются ТОЛЬКО кривой плавности (CSS). */
/* док держит своё смещение в --dock-y: transform плавно увозит пилюлю
   в центр высоты и так же плавно возвращает (offsetTop не зависит от
   transform — стрелки-клики не сбивают прицел) */
function dockY(on) {
  const dock = document.querySelector('.dock');
  if (!dock) return;
  if (on) {
    const dy = Math.max(0, (window.innerHeight - dock.offsetHeight) / 2 - dock.offsetTop);
    dock.style.setProperty('--dock-y', dy + 'px');
  } else {
    dock.style.setProperty('--dock-y', '0px');
  }
}
function toggleSidebar() {
  const app = $('#app');
  if (isNarrow()) { app.classList.toggle('nav-open'); return; }
  app.classList.remove('nav-open');
  const collapsing = !app.classList.contains('collapsed');
  app.classList.toggle('collapsed', collapsing);
  // диалоги уезжают/возвращаются РАЗОМ с превращением — один такт
  app.classList.toggle('side-folding', collapsing);
  dockY(collapsing);
  /* BK: состояние панели больше не хранится: каждый запуск — с доком */
}
$('#collapseBtn').addEventListener('click', toggleSidebar);
try {
  /* BK: ДЖАРВИС ВСЕГДА ОТКРЫВАЕТСЯ С ДОКОМ. Раньше из localStorage
     восстанавливалось 'open' — достаточно было один раз раскрыть панель,
     и все следующие запуски открывались с боковым меню. Панель — решение
     на ТЕКУЩУЮ сессию: перезапуск всегда возвращает док */
  localStorage.removeItem('jarvis.sidebar2');
  if (!isNarrow()) {
    $('#app').classList.add('collapsed');
    // восстановление БЕЗ анимации: пилюля сразу в центре высоты
    const dock = document.querySelector('.dock');
    if (dock) {
      dock.style.transition = 'none';
      dockY(true);
      void dock.offsetHeight;
      dock.style.transition = '';
    }
  }
} catch (e) {}
window.addEventListener('resize', () => {
  if ($('#app').classList.contains('collapsed') && !isNarrow()) dockY(true);
});

/* Правой панели больше нет: уведомления, санкции и камера живут прямо в чате
   (см. разделы «камера в диалоге» и «санкции / уведомления в диалоге» ниже). */

/* ============================ переключатели ============================ */
/* КРАСНАЯ ВОЛНА AGENT. Включение агентского режима — не смена галочки, а
   пересадка в гоночный автомобиль: от тумблера к краям экрана мягко
   расходятся красные акценты, и весь интерфейс наливается цветом режима.
   Волна — одноразовый слой поверх всего: расширяется, тает, убирается.
   Класс agent-on на body остаётся и держит красную тему, пока режим жив. */
/* КИСТЬ: волна красит интерфейс ЗА СОБОЙ. Класс темы включается сразу,
   но каждый элемент начинает перекрашиваться ровно тогда, когда фронт
   доходит до него (задержка = расстояние от тумблера / скорость фронта),
   а не весь экран разом. Градиентные фоны браузер не интерполирует — их
   держим старыми до прихода фронта и отпускаем в момент: жёсткая кромка
   кисти. После прохода всё прибирается, элементы живут своей жизнью. */
function agentPaint(cx, cy) {
  /* AO: клики больше не блокируются — быстрый ON→OFF→ON защищён флагом:
     пока кисть живёт, повторный запуск лишь переключает класс темы */
  if (agentPaint._busy) {
    document.body.classList.add('agent-on');
    return;
  }
  agentPaint._busy = true;
  const R = Math.hypot(Math.max(cx, innerWidth - cx), Math.max(cy, innerHeight - cy)) || 1;
  const VARS = ['--cy', '--cy2', '--line', '--line2', '--panel', '--panel2'];
  const cs = getComputedStyle(document.body);
  const old = {};
  VARS.forEach((v) => { old[v] = cs.getPropertyValue(v).trim(); });
  const els = new Set([document.body]);
  for (const sheet of document.styleSheets) {
    let rules; try { rules = sheet.cssRules; } catch (e) { continue; }
    for (const rule of rules) {
      const sel = rule.selectorText || '';
      if (sel.indexOf('body.agent-on') !== 0) continue;
      const rest = sel.replace(/^body\.agent-on\s*/, '').trim();
      if (!rest) continue;
      try { document.querySelectorAll(rest).forEach((x) => els.add(x)); } catch (e) {}
    }
  }
  const grads = [];
  els.forEach((e) => {
    const r = e.getBoundingClientRect();
    const d = (r.width || r.height)
      ? Math.hypot(r.left + r.width / 2 - cx, r.top + r.height / 2 - cy) : 0;
    const delay = Math.min(620, Math.round(d / R * 470));
    const save = { tr: e.style.transition, td: e.style.transitionDelay, vars: {} };
    VARS.forEach((v) => {
      save.vars[v] = e.style.getPropertyValue(v);
      e.style.setProperty(v, old[v]);
    });
    e.style.transition = 'background-color .26s ease, border-color .26s ease, color .26s ease, ' +
      'box-shadow .26s ease, fill .26s ease, --cy .26s ease, --cy2 .26s ease, ' +
      '--line .26s ease, --line2 .26s ease, --panel .26s ease, --panel2 .26s ease';
    e.style.transitionDelay = delay + 'ms';
    e._agSave = save;
    const bg = getComputedStyle(e).backgroundImage;
    if (bg && bg !== 'none' && bg.indexOf('gradient') >= 0 && e !== document.body) {
      save.bg = e.style.backgroundImage;
      e.style.backgroundImage = bg;      // старый градиент — до прихода фронта
      grads.push({ e, delay });
    }
  });
  // класс темы — СРАЗУ: каждый элемент поедет со своей задержкой
  document.body.classList.add('agent-on');
  // на следующем кадре снимаем var- overrides: у каждого элемента его
  // наследуемые цвета меняются в СВОЙ момент (transition + delay)
  requestAnimationFrame(() => {
    els.forEach((e) => {
      if (!e._agSave) return;
      VARS.forEach((v) => { if (!e._agSave.vars[v]) e.style.removeProperty(v); });
    });
  });
  grads.forEach((g) => {
    setTimeout(() => { g.e.style.backgroundImage = g.e._agSave.bg || ''; }, g.delay);
  });
  // прибираем за кистью: элементы возвращаются к обычным переходам
  setTimeout(() => {
    els.forEach((e) => {
      if (!e._agSave) return;
      const s = e._agSave;
      e.style.transition = s.tr;
      e.style.transitionDelay = s.td;
      if (s.bg !== undefined) e.style.backgroundImage = s.bg;
      VARS.forEach((v) => {
        if (s.vars[v]) e.style.setProperty(v, s.vars[v]);
        else e.style.removeProperty(v);
      });
      e._agSave = null;
    });
    agentPaint._busy = false;   // AO: кисть свободна — новый запуск снова с волной
  }, 1000);
}

function agentWave(originEl, calm) {
  const src = originEl && originEl.getBoundingClientRect ? originEl : $('#swAgent');
  let r = (src && src.getBoundingClientRect()) || null;
  // Карточка-разрешение уже свернулась — её тумблер в display:none и rect
  // нулевой. Тогда волна рождается из координат, снятых ДО свёртывания.
  if ((!r || (!r.width && !r.height)) && S.agentWaveRect) r = S.agentWaveRect;
  if (!r) r = { left: innerWidth / 2, top: innerHeight / 2, width: 0, height: 0 };
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const wave = el('div', 'agent-wave' + (calm ? ' out' : ''));
  const radius = Math.hypot(Math.max(cx, innerWidth - cx), Math.max(cy, innerHeight - cy));
  wave.style.left = cx + 'px';
  wave.style.top = cy + 'px';
  // с запасом ДАЛЕКО за экран: фронт уходит за границы и гаснет только там
  wave.style.setProperty('--aw', (radius * 2.6) + 'px');
  document.body.appendChild(wave);
  S.agentWaveRect = null;
  // волна короткая и быстрая, но ДОХОДИТ до края экрана и уходит за него
  setTimeout(() => wave.remove(), calm ? 1000 : 900);
}

$('#tgAgent').addEventListener('change', function () {
  // Настоящий checkbox-switch: состояние принадлежит самому control, а не
  // декоративному классу кнопки. Подпись лежит вне <label>, поэтому она не
  // переключает режим. После клика tooltip гаснет, даже пока указатель на track.
  S.agentMode = this.checked;
  const shell = this.closest('.agent-switch');
  if (shell) {
    shell.classList.add('tip-dismissed');
    /* AN: ВКЛ/ВЫКЛ ИГРАЕТ ВСЕГДА — ДАЖЕ ПОСЕРЕДИНЕ РЕЗИНКИ. У резинки
       наведения fill:both на transform круглёшка: пока она живёт (или
       держит последний кадр), CSS-переход включения проиграть не может —
       анимация главнее перехода, и тумблер ТЕЛЕПОРТИРОВАЛСЯ, если клик
       пришёлся на анимацию наведения. Вместо надежд на переход ведём
       круглёшок ЯВНО: снимаем его текущее положение (каким бы оно ни
       было — хоть 6px посередине резинки), гасим его анимации и ведём
       Web Animations API в конечную точку той же кривой .45с. Под этим
       вездехал обычный CSS-переход с теми же параметрами, а в конце
       WAAPI-анимация сама снимается — стык невидим, наследования нет. */
    const knob = shell.querySelector('.agent-switch-track i');
    if (knob && knob.getAnimations && knob.animate) {
      /* AP: КОРЕНЬ «ИНОГДА НЕ СРАБАТЫВАЕТ». Раньше старт брали из
         getComputedStyle ПОСЛЕ клика — и при ВЫКЛЮЧЕНИИ под курсором
         резинка мгновенно ПЕРЕЗАПУСКАЛАСЬ с нулевого кадра: код читал
         «кружок уже слева», считал, что ехать некуда, гасил переход —
         и круглёшок телепортировался. Логические точки дискретны и
         известны ЗАРАНЕЕ: до клика тумблер стоял в одном из двух
         положений. ВКЛЮЧАЕМ: старт — где круглёшок реально виден
         (матрица живой резинки, хоть 6px посередине). ВЫКЛЮЧАЕМ:
         старт — всегда правое положение (на включённом резинка не
         играет). Переход есть ВСЕГДА, «некуда ехать» больше не бывает. */
      let from;
      if (this.checked) {
        from = 'translateX(0)';
        try {
          const m = getComputedStyle(knob).transform;
          if (m && m !== 'none') from = m;
        } catch (e) { /* остаёмся на левом краю */ }
      } else {
        from = 'translateX(10px)';
      }
      knob.getAnimations().forEach((a) => { try { a.cancel(); } catch (e) {} });
      const to = this.checked ? 'translateX(10px)' : 'translateX(0)';
      if (from !== to) {
        const go = knob.animate(
          [{ transform: from }, { transform: to }],
          { duration: 450, easing: 'cubic-bezier(.3,.6,.3,1)', fill: 'both' });
        go.onfinish = () => { try { go.cancel(); } catch (e) {} };
      }
    }
    // ПЕРЕКЛЮЧЕНИЕ ГАСИТ РЕЗИНКУ ДО НОВОГО НАВЕДЕНИЯ (X): клик по тумблеру
    // — уже ответ интерфейса; резинка не должна дёргаться следом за ним.
    shell.classList.remove('ag-play');
    shell.dataset.agHold = '1';
  }
  beep(S.agentMode ? 760 : 420, 0.1);
  // ТУМБЛЕР НЕ ТРОГАЮТ, ПОКА ИГРАЕТ ПЕРЕСАДКА: волна и смена темы — единый
  // жест, двойной клик посреди него ломал бы последовательность.
  if (shell) shell.classList.add('ag-switching');
  // СНАЧАЛА ВОЛНА, ТЕМА — ЗА НЕЙ. Волна короткая и сдержанная (0.78 с),
  // расходится от тумблера; на 280-й мс, когда фронт накрывает экран, за ним
  // меняется тема — жест читается как «волна прокатилась и перекрасила».
  if (S.agentMode) {
    // ВОЛНА КРАСИТ ИНТЕРФЕЙС ЗА СОБОЙ: пока бордовый фронт катится по
    // экрану, ПЕРЕКРАШЕННЫМ становится то, что она уже накрыла. Класс
    // темы включается на 260-й мс — к этому моменту фронт накрыл центр,
    // и цвета доезжают переходами (0.5 с) ещё ПОД волной, до её ухода
    const wOrigin = S.agentWaveOrigin || $('#swAgent');
    let wr = (wOrigin && wOrigin.getBoundingClientRect && wOrigin.getBoundingClientRect()) || null;
    if ((!wr || (!wr.width && !wr.height)) && S.agentWaveRect) wr = S.agentWaveRect;
    if (!wr) wr = { left: innerWidth / 2, top: innerHeight / 2, width: 0, height: 0 };
    agentWave(wOrigin, false);
    agentPaint(wr.left + wr.width / 2, wr.top + wr.height / 2);
    setTimeout(() => { if (shell) shell.classList.remove('ag-switching'); }, 950);
  } else {
    // ВЫКЛЮЧЕНИЕ — БЕЗ ОБРАТНОЙ ВОЛНЫ: базовый переход. Цвета уезжают
    // вспять своими transition (~0.95с), тумблер гаснет до нейтрального
    // обычной анимацией переключения. Тихо и спокойно.
    document.body.classList.remove('agent-on');
    setTimeout(() => { if (shell) shell.classList.remove('ag-switching'); }, 400);
  }
  S.agentWaveOrigin = null;
  $('#input').placeholder = S.agentMode
    ? 'Поставь задачу — разобью на шаги и сделаю сам…'
    : 'Сообщение для JARVIS…';
  /* BE: строка в чате вместо всплывашки — включение и выключение
     остаются в ленте */
  toolLine('agent', S.agentMode);
});
$$('.agent-switch').forEach((sw) => sw.addEventListener('mouseleave', function () {
  this.classList.remove('tip-dismissed');
  // гашение резинки живёт ровно до ухода курсора: следующее наведение
  // играет анимацию как обычно
  delete this.dataset.agHold;
}));
/* РЕЗИНКА ТУМБЛЕРА ИГРАЕТ РОВНО ОДИН РАЗ за наведение. Раньше анимации
   висели на :hover — мелкое дрожание курсора на краю перезапускало их,
   и искры пробегали дважды. Теперь mouseevent-класс ставится один раз
   и снимается после проигрыша; повторное наведение в паузе игнорируется. */
$$('.agent-switch').forEach((sw) => {
  sw.addEventListener('mouseenter', () => {
    if (sw.classList.contains('ag-switching') || sw.dataset.agHold) return;
    // РЕСТАРТ ВСЕГДА: даже мгновенный повтор входа проигрывает резинку
    // с нуля — снять класс, принудительный reflow, поставить заново
    sw.classList.remove('ag-play');
    void sw.offsetWidth;
    sw.classList.add('ag-play');
  });
  // резинка, огонёк и свечение живут, ПОКА курсор на тумблере: уход — всё
  // тихо гаснет (анимации с both-заливкой снимаются вместе с классом)
  sw.addEventListener('mouseleave', () => {
    sw.classList.remove('ag-play');
  });
});
$('#tgCamera').addEventListener('click', function () {
  S.cameraOn = !S.cameraOn; this.classList.toggle('on', S.cameraOn);
  beep(S.cameraOn ? 760 : 420, 0.1);
  if (S.cameraOn) startCam(); else stopCam();
});
$('#tgComputer').addEventListener('click', function () {
  S.computerUse = !S.computerUse; this.classList.toggle('on', S.computerUse);
  beep(S.computerUse ? 760 : 420, 0.1);
  /* BE: отключение — сразу строка в чате (включение — после самопроверки) */
  if (!S.computerUse) { toolLine('computer', false); return; }
  // Самопроверка при включении: раньше «не работает» выглядело как молчаливое
  // бездействие агента. Теперь тумблер сразу называет конкретную причину —
  // права macOS, скриншот или зрительную модель.
  api('/api/computer/status').then((r) => {
    // формат: {ok: <запрос дошёл>, computer: {ok: <режим готов>, error: ...}}
    const st = (r && r.computer) || {};
    if (st.ok) {
      /* BE: строка в чате вместо всплывашки — остаётся в ленте */
      toolLine('computer', true);
    } else {
      S.computerUse = false;
      const t = $('#tgComputer');
      if (t) t.classList.remove('on');
      sfx('error');
      modal('<h3>COMPUTER-USE не готов</h3>' +
        '<div class="sd" style="margin-bottom:10px">Проверка на этой машине не прошла:</div>' +
        '<pre class="out" style="white-space:pre-wrap">' + esc(st.error || r.error || 'неизвестная причина') + '</pre>' +
        '<div class="sd" style="margin:4px 0 10px">Открою нужную панель настроек — включи там приложение, ' +
        'из которого запущен JARVIS (Терминал), и перезапусти JARVIS:</div>' +
        '<div class="modal-acts" style="justify-content:flex-start;flex-wrap:wrap;gap:8px">' +
        '<button class="btn sm primary" id="permAcc">Открыть «Универсальный доступ»</button>' +
        '<button class="btn sm primary" id="permScr">Открыть «Запись экрана»</button>' +
        '<button class="btn sm" onclick="document.getElementById(\'modalBack\').classList.remove(\'open\')">Закрыть</button></div>');
      const openPane = (pane) => api('/api/computer/permissions', { pane })
        .then((res) => { if (res.ok) toast('Панель открыта — включи JARVIS/Терминал и перезапусти', 'info', 'Права'); });
      const acc = $('#permAcc'), scr = $('#permScr');
      if (acc) acc.addEventListener('click', () => openPane('accessibility'));
      if (scr) scr.addEventListener('click', () => openPane('screen'));
    }
  });
});

/* ============================ состояние ============================ */
async function refreshState() {
  const st = await api('/api/state');
  if (!st.ok) { setChip('#chipConn', 'err', 'нет связи'); return; }
  setChip('#chipConn', 'ok', 'связь');
  S.config = st.config || {};
  syncSoundBtn();   // AF: конфиг приехал — кнопка звука показывает правду
  applySilentTools(st.silent_tools);
  S.tasks = st.tasks || [];
  S.autoPaused = !!st.auto_paused;
  S.approvals = st.approvals || [];
  S.notifications = st.notifications || [];
  S.unread = st.unread || 0;

  const active = st.active_tasks || 0;
  const running = st.running_tasks != null
    ? Number(st.running_tasks || 0)
    : S.tasks.filter((task) => task.status === 'running').length;
  const badge = $('#autoBadge');
  badge.textContent = active;
  badge.classList.toggle('hot', active > 0);
  // «0 задач» — не информация: без задач цифру не показываем вовсе
  badge.style.display = active > 0 ? '' : 'none';
  const autoNav = $('.nav-item[data-view="auto"]');
  if (autoNav) autoNav.classList.toggle('auto-running', running > 0);
  setChip('#chipAuto', running > 0 ? 'warn live' : (active > 0 ? 'warn' : 'ok'),
    running > 0 ? 'AUTO · выполняю ' + running : (active > 0 ? 'AUTO · ' + active : 'AUTO'));

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
function typeAutoReply(node, content, done) {
  const text = String(content || '');
  const md = el('div', 'md typing');
  node.body.appendChild(md);
  if (!text) { md.classList.remove('typing'); if (done) done(); return; }
  // AUTO использует тот же elapsed-time typer и тот же синий cursor, что
  // обычный ответ. Отложенная реплика больше не возникает целым абзацем.
  const ui = {
    node, runId: S.streamRun, mdEl: md, buffer: '', shown: '', typer: null,
    pendingReplyUi: '', replyUiSpec: '', replyLive: null, cps: 0, acc: 0,
    lastScroll: 0, floor: 0, followOutput: true,
  };
  S.followUi = ui;
  watchRunFollow(ui);
  ui.onTyped = () => {
    md.classList.remove('typing');
    clearTypingDecorations(md);
    if (S.followUi === ui) S.followUi = null;
    if (ui.stopFollowWatch) ui.stopFollowWatch();
    if (done) done(ui);
  };
  typeInto(ui, text);
}

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
    typeAutoReply(node, m.content, (ui) => {
      foldCodeBlocks(node.body);
      mountUiPanels(node.body);
      mountPlotPanels(node.body);
      (meta.files || []).forEach((f) => attachFileChip(node.body, f));
      addMsgActions(node, m.content);
      scrollDown(false, ui);
    });
    added = true;
  });
  if (added) { scrollDown(); sfx('note'); }
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
  S.chatOpenRun += 1; // инвалидируем любой ещё летящий openChat()
  if (VOICE.open) closeVoiceMode();
  if (S.camNode || S.camStream) stopCam(); // чистим и активный, и ошибочный/pending-сеанс
  // Уходим на новый диалог во время ответа: генерация НЕ обрывается — сервер
  // допишет и сохранит её в переписку (как при переключении на другой чат)
  if (S.streaming) {
    S.detached = S.chatId;
    setStreaming(false);
  }
  S.sanctionNodes = {};
  S.chatId = null;
  S.fdir = '';
  /* AZ: подсказки принадлежат диалогу — новый диалог начинается без чужих */
  S.replyTicket = (S.replyTicket || 0) + 1;
  { const rb = $('#replyBar'); if (rb) { rb.hidden = true; rb.innerHTML = ''; } }
  $('#stream').innerHTML = '';
  $('#stream').appendChild(buildWelcome());
  requestAnimationFrame(fitSuggTexts);
  // AA: ПЛАН УХОДИТ ВМЕСТЕ С ДИАЛОГОМ. Панель привязана к своему диалогу
  // (Z), но «новый диалог» её не прятал: пустой экран приветствия с чужим
  // золотым планом наверху выглядел как баг. Прячем ВСЕ доки — свой вернётся
  // при возвращении в диалог.
  $$('.plan-dock').forEach((d) => { d.style.display = 'none'; });
  loadChats();
  showView('chat');
  $('#input').focus();
}
$('#newChatBtn').addEventListener('click', newChat);

async function openChat(id) {
  const ticket = ++S.chatOpenRun;
  if (VOICE.open) closeVoiceMode();
  if (S.camNode || S.camStream) stopCam();
  // Уходим из диалога во время ответа: генерацию НЕ обрываем — сервер доведёт
  // её до конца и сохранит в переписку. Просто отпускаем интерфейс.
  if (S.streaming && id !== S.chatId) {
    S.detached = S.chatId;
    setStreaming(false);
  }
  S.sanctionNodes = {};
  S.chatId = id;
  S.fdir = '';
  /* AZ: уйдя в другой диалог, чужие подсказки гасим сразу — свои придут
     из meta последнего ответа или посчитаются заново */
  S.replyTicket = (S.replyTicket || 0) + 1;
  { const rb = $('#replyBar'); if (rb) { rb.hidden = true; rb.innerHTML = ''; } }
  showView('chat');
  const r = await api('/api/messages?chat_id=' + encodeURIComponent(id));
  // Быстрые переключения чатов могут вернуть HTTP-ответы в обратном порядке.
  if (ticket !== S.chatOpenRun || id !== S.chatId) return;
  const stream = $('#stream');
  // Скрытая render-транзакция: браузер ни на одном paint не видит историю в
  // позиции 0. В том же task строим DOM, отключаем smooth, ставим низ и только
  // затем открываем ленту. Картинки корректируют высоту лишь после их load.
  stream.classList.add('history-rendering');
  stream.innerHTML = '';
  renderMessages(stream, r.messages || []);
  // AB: ВЕРНУЛСЯ В ДИАЛОГ, ГДЕ ЕЩЁ ПИШЕТСЯ ОТВЕТ — не крошечная заглушка
  // «отвечает…», а САМ живой ответ: узел прогона пережил перерисовку ленты
  // и продолжает печататься на глазах, как никуда и не уходил. Прежнее
  // поведение (заглушка → готовый текст резко появляется целиком) и было
  // корнем «ответ пропадает при переключении диалога» — на десятый раз
  // лечим не симптом, а сам механизм.
  const live = (S.liveRuns || {})[id];
  const liveAttached = !!(live && !live.doneReceived && live.node && live.node.root);
  if (liveAttached) {
    stream.appendChild(live.node.root);
    watchRunFollow(live);
    if (S.followUi === live && !S.streaming) setStreaming(true);   // Stop снова Stop
  } else if (r.generating) {
    appendLivePlaceholder();
  }
  pinToBottom(stream);
  stream.classList.remove('history-rendering');
  // Z: ПЛАН ПРИВЯЗАН К ДИАЛОГУ: панель чужого диалога прячется, своего —
  // возвращается на экран в актуальном состоянии (шаги, прогресс)
  $$('.plan-dock').forEach((d) => {
    d.style.display = (d.dataset.chatId === id) ? '' : 'none';
  });
  loadChats();
  // диалог, который дописывался в фоне (или открыт во время генерации):
  // тихо перечитываем, пока не появится ответ. Живой прогон уже на экране —
  // ему опрос не нужен (AC).
  if (!liveAttached && (r.generating || S.detached === id)) watchDetached(id);
}

/* Отрисовка переписки в заданный контейнер. Вынесена из openChat, потому что
   тот же список нужно уметь перерисовать ВНУТРИ карточки камеры — иначе
   переключение версии выбрасывало разговор в основную ленту. */
function renderMessageInto(host, m, activePanel) {
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
    updateResponseMeta({
      node,
      routeTier: meta.tier || '',
      modelName: meta.model || '',
      routeReason: '',
      routeEl: null,
    });
    // ход мыслей и действия из прошлого ответа — свёрнутыми строчками
    restoreTrace(node, meta);
    node.body.appendChild(el('div', 'md', MD.render(m.content)));
    foldCodeBlocks(node.body);
    // AD: панели прошлого законсервированы; кликабельна только панель
    // ПОСЛЕДНЕГО ответа — старый интерактивчик больше не принимает ответы
    mountUiPanels(node.body, { inert: !activePanel });
    mountPlotPanels(node.body);
    (meta.files || []).forEach((f) => attachFileChip(node.body, f));
    addMsgActions(node, m.content);
  }
}

function renderMessages(host, messages) {
  const prevHost = S.forceHost;
  S.forceHost = host;
  // AD: активная панель — только у ПОСЛЕДНЕГО ответа Джарвиса
  const msgs = messages || [];
  let lastAiId = '';
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === 'assistant') { lastAiId = msgs[i].id; break; }
  }
  msgs.forEach((m) => renderMessageInto(host, m, m.role === 'assistant' && m.id === lastAiId));
  S.forceHost = prevHost;
  fixTables(host);
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

/* Живая строка «отвечает…» — в диалоге, где генерация ещё идёт на сервере.
   Курсор тот же, что и при обычном ответе: возвращение в диалог посреди
   генерации читается как «он всё ещё пишет», а не «ответ исчез». */
function appendLivePlaceholder() {
  const node = addAiMsg(Date.now() / 1000);
  node.root.dataset.livePlaceholder = '1';
  const st = el('div', 'thinking-line');
  node.body.appendChild(st);
  runStatus({ statusEl: st, node }, ['Джарвись отвечает…', 'дописывает ответ', 'ещё немного'],
    { caret: true, shuffle: true, every: 2600 });
  return node;
}

/* Ответ дописывается на сервере, а мы уже смотрим этот диалог. Периодически
   перечитываем переписку: как только ассистент договорил — ДОРИСОВЫВАЕМ
   только новые сообщения поверх живой ленты. Раньше здесь стоял полный
   openChat: лента мигала, скролл прыгал, готовый ответ «возникал резко». */
function watchDetached(id) {
  clearTimeout(S.detachTimer);
  let tries = 0;
  const tick = async () => {
    if (S.chatId !== id) return;
    // AC: живой прогон этого диалога ещё на экране — SSE сам дорисует ответ;
    // опрос добавил бы СОХРАНЁННУЮ копию поверх печатающейся (дубль ответа)
    if ((S.liveRuns || {})[id]) return;
    const r = await api('/api/messages?chat_id=' + encodeURIComponent(id));
    if (S.chatId !== id) return;
    const msgs = r.messages || [];
    const last = msgs[msgs.length - 1];
    if (last && last.role === 'assistant') {
      S.detached = null;
      appendFreshMessages(msgs);
      return;
    }
    // сервер уже не генерирует, а ответа всё нет — прогон умер до
    // сохранения: убираем заглушку, не морочим голову «отвечает…»
    if (!r.generating && tries > 3) {
      const ph = stream().querySelector('[data-live-placeholder]');
      if (ph) ph.remove();
      return;
    }
    if (++tries < 400) S.detachTimer = setTimeout(tick, 1500);
  };
  S.detachTimer = setTimeout(tick, 1200);
}

/* Готовый ответ приезжает ПЛАВНО: дорисовываем только то, чего в ленте ещё
   нет (по msgId), заглушку снимаем, скролл мягко едет вниз. */
function appendFreshMessages(msgs) {
  const host = stream();
  const known = new Set();
  $$('.msg', host).forEach((m) => {
    if (m.dataset && m.dataset.msgId) known.add(m.dataset.msgId);
  });
  const ph = host.querySelector('[data-live-placeholder]');
  if (ph) ph.remove();
  const fresh = (msgs || []).filter((m) => !known.has(m.id));
  if (!fresh.length) return;
  const before = host.children.length;
  const prevHost = S.forceHost;
  S.forceHost = host;
  // свежие сообщения — самые последние в ленте, их панели активны
  fresh.forEach((m) => renderMessageInto(host, m, m.role === 'assistant'));
  S.forceHost = prevHost;
  // Z: ПЛАВНЫЙ ПРИЕЗД — готовый ответ доезжает мягким проявлением,
  // а не «резко появляется» после пустого экрана
  Array.prototype.slice.call(host.children, before).forEach((c) => {
    c.style.animation = 'freshIn .32s ease both';
  });
  const lastAi = fresh[fresh.length - 1];
  if (lastAi && lastAi.role === 'assistant') {
    const cachedReplies = ((lastAi.meta || {}).replies) || [];
    showReplies(cachedReplies);
    /* BG: у последнего ответа нет сохранённых подсказок (nano не успела
       или сбой) — посчитаем фоном, чтобы чипы были после каждого ответа */
    if (!cachedReplies.length && !S.streaming) fetchReplies();
  }
  pinToBottom(host);
}

/* Подсказки на пустом экране. Сервер отдаёт их ГОТОВЫМИ (считает заранее в
   фоне), поэтому новый диалог открывается мгновенно. Держим последний ответ
   в памяти вкладки — тогда даже первого запроса ждать не нужно. */
/* AZ: ПЛИТКА = НАЗВАНИЕ + ОПИСАНИЕ, что произойдёт. Сам запрос (промпт)
   пользователь не читает заранее — он введётся в поле по клику */
const SUGGESTIONS = [
  ['Что нового?', 'Пройдусь по новостным сайтам, отберу самые важные события дня и соберу их в короткую сводку со ссылками',
   'Найди в интернете 5 главных новостей за сегодня и сделай сводку'],
  ['Собери таблицу', 'Найду в интернете цены на товар в разных магазинах, сведу их в ровную таблицу и сохраню файлом в Excel',
   'Собери таблицу с ценами на iPhone 17 в российских магазинах и сохрани в Excel'],
  ['Каждое утро', 'Настрою расписанное задание: каждое утро ровно в девять я буду присылать погоду и курс доллара',
   'Каждый день в 9:00 присылай мне погоду и курс доллара в Telegram'],
  ['Нарисую картинку', 'Придумаю несколько вариантов по описанию, нарисую самый удачный и покажу — можно сразу сохранить',
   'Нарисуй логотип для кофейни в стиле неон-минимализм'],
  ['Разберу файл', 'Прочитаю присланный документ целиком, вытащу главное из каждого раздела и соберу выжимку по пунктам',
   'Я пришлю документ — вытащи из него главное и сделай выжимку по пунктам'],
  ['Наведу порядок', 'Загляну в твою песочницу, разложу файлы по папкам, покажу структуру и подскажу, что можно удалить',
   'Загляни в мою песочницу, разложи файлы по папкам и скажи, что можно удалить'],
];
S.ideas = SUGGESTIONS.map((s) => ({ title: s[0], desc: s[1], prompt: s[2] }));

async function loadIdeas(again) {
  try {
    const r = await api('/api/ideas');
    if (r.ok && (r.ideas || []).length) {
      S.ideas = r.ideas;
      /* если пустой экран уже открыт — обновим карточки на месте,
         спокойно: без повторного «всплытия» */
      const box = $('.welcome .suggestions');
      if (box) {
        fillSuggestions(box, true);
        requestAnimationFrame(fitSuggTexts);
      }
      /* AY: ИИ ЕЩЁ ПРИДУМЫВАЕТ ПЛИТКИ — заберём живые повторным заходом */
      if (r.refreshing && !again) setTimeout(() => { loadIdeas(true); }, 2600);
    }
  } catch (e) { /* останутся встроенные */ }
}

/* AQ: ТРОЕТОЧИЕ ПЛИТОК — чистый CSS. Три итерации JS-обрезки не
   пережили реальности (плиты создаются до подключения к экрану,
   шрифты меняют метрики), а CSS-«многоточие» не работало из-за
   сетки: колонки 1fr не могут быть уже самого длинного nowrap-заголовка
   (min-content), плитки разъезжались и текст «неровнел». Теперь колонки
   minmax(0,1fr) — плитки всегда равные, а заголовок лежит в собственном
   span с min-width:0 (канонический паттерн, работает в любом браузере):
   длинный заголовок обрезается с «…» на границе плитки. */
function fillSuggestions(box, calm) {
  box.innerHTML = '';
  S.ideas.slice(0, 6).forEach((s, i) => {
    const b = el('button', 'sugg',
      '<b><span class="st">' + esc(s.title) + '</span></b><span class="sp">' + esc(s.desc || s.prompt) + '</span>');
    b.style.animationDelay = (0.04 * i) + 's';
    /* подмена плиток на живом экране — без повторного «всплытия» */
    if (calm) b.style.animation = 'none';
    b.addEventListener('click', () => { $('#input').value = s.prompt; autoGrow(); send(); });
    box.appendChild(b);
  });
}

/* AY: ТРОЕТОЧИЕ ПО-ЧЕЛОВЕЧЕСКИ. CSS-обрезка ставит «…» вплотную к знаку
   препинания («дела,…»). Меряем реальную высоту и подрезаем по словам:
   если обрезка пришлась после знака препинания, многоточие идёт после
   пробела. Полный текст хранится в data-full — подрезка идемпотентна. */
function fitSuggText(sp) {
  const full = sp.dataset.full || sp.textContent;
  if (!full) return;
  sp.dataset.full = full;
  sp.textContent = full;
  if (sp.scrollHeight <= sp.clientHeight + 1) return;
  const words = full.split(' ');
  let lo = 1, hi = words.length, keep = 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    sp.textContent = words.slice(0, mid).join(' ') + '\u00A0…';
    if (sp.scrollHeight <= sp.clientHeight + 1) { keep = mid; lo = mid + 1; }
    else { hi = mid - 1; }
  }
  const head = words.slice(0, keep).join(' ').trimEnd();
  sp.textContent = /[.,;:!?…—)]$/.test(head) ? head + '\u00A0…' : head + '…';
}
function fitSuggTexts() {
  $$('.welcome .sugg .sp').forEach(fitSuggText);
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
/* Камера — ОТДЕЛЬНЫЙ разговор, а не продолжение текущего.
   Раньше карточка камеры рисовала реплики у себя, но отправляла их в общий
   chat_id: в переписку основного диалога подмешивались «вижу кружку, вижу
   руку», а модель тащила этот мусор в ответы на обычные вопросы. Причина —
   у камеры был свой ВИД, но не было своего КОНТЕКСТА. Заводим ей собственный
   chat_id и держим единственную точку, которая отвечает на вопрос
   «в какой разговор сейчас пишем». */
function camLive() { return !!(S.camNode && S.camNode.isConnected); }

/* Камера привязана к текущему диалогу? По умолчанию НЕТ: у неё свой контекст.
   Тумблер «Видеть текущий диалог» в карточке переключает это на лету. */
function camLinked() { return camLive() && !!S.camLink; }

/* Единственная точка ответа на вопрос «в какой разговор сейчас пишем».
   Камера без привязки пишет в свой чат, с привязкой — в основной. */
function activeChatId() {
  if (camLive() && !camLinked()) return S.camChatId || null;
  return S.chatId;
}

function setActiveChat(id) {
  if (camLive() && !camLinked()) S.camChatId = id; else S.chatId = id;
}

function camPart(selector) {
  // У камеры один владелец DOM — активная S.camNode. Старые свёрнутые карточки
  // намеренно остаются в истории, поэтому глобальный querySelector выбирал
  // первую (уже скрытую) карточку и отправлял туда новые сообщения/кадры.
  // Все live-узлы ищутся только внутри карточки текущего поколения.
  const node = S.camNode;
  return node && node.isConnected ? node.querySelector(selector) : null;
}

function msgHost() {
  // перерисовка может идти в явно заданный контейнер (например, в переписку
  // внутри карточки камеры) — тогда он важнее общих правил
  if (S.forceHost && S.forceHost.isConnected) return S.forceHost;
  return camPart('.cam-chat') || stream();
}
function runScrollBox(ui) {
  if (!ui || !ui.node || !ui.node.root) return null;
  return ui.node.root.closest('.cam-chat') || stream();
}

/* BE: СКРОЛЛ ЗА ОТВЕТОМ — ПЛАВНЫЙ И ЧЕСТНЫЙ. Прилипание живёт только
   пока читатель в самом низу: любой реальный уход вверх — колесо, тач,
   клавиши, тащок скроллбара — сразу снимает его; вернулся до упора
   вниз — включается снова. Прокрутка за ответом «догоняющая»: каждый
   кадр экран проходит четверть оставшейся дистанции, поэтому движение
   плавное, но от быстрой печати кода не отстаёт. */
function watchRunFollow(ui) {
  const box = runScrollBox(ui);
  if (!box || !box.addEventListener) return;
  followLiveStream(box);
  if (box.__jarvisFollowRuns) return;
  box.__jarvisFollowRuns = true;
  const st = (box.__jarvisScroll = box.__jarvisScroll ||
    { lastTop: box.scrollTop, lastH: box.scrollHeight, autoPend: 0, chasing: false });
  let touchY = null;
  const active = () => {
    const run = S.followUi;
    return run && runScrollBox(run) === box ? run : null;
  };
  const leave = () => {
    const run = active();
    if (run) run.followOutput = false;
  };
  box.addEventListener('wheel', (e) => {
    if (Number(e.deltaY || 0) < 0) leave();
  }, { passive: true });
  box.addEventListener('touchstart', (e) => {
    touchY = e.touches && e.touches[0] ? e.touches[0].clientY : null;
  }, { passive: true });
  box.addEventListener('touchmove', (e) => {
    const y = e.touches && e.touches[0] ? e.touches[0].clientY : null;
    if (touchY != null && y != null && y > touchY + 4) leave();
    if (y != null) touchY = y;
  }, { passive: true });
  box.addEventListener('scroll', () => {
    const top = box.scrollTop;
    const h = box.scrollHeight;
    /* BF: АГЕНТСКИЙ РЕЖИМ — инструменты СВЁРТЫВАЮТСЯ, контент сжимается и
       браузер сам УМЕНЬШАЕТ scrollTop (clamp). Это не человек: уменьшение
       высоты при уменьшении scrollTop — служебная подстройка, прилипание
       не трогаем. Настоящий уход вверх (колесо/тач/клавиши/скроллбар)
       высоту не меняет */
    if (h < st.lastH - 2) {
      st.lastTop = top; st.lastH = h;
      if (st.autoPend > 0) st.autoPend -= 1;
      return;
    }
    st.lastH = h;
    // наш догоняющий кадр scrollTop уменьшить не может: уменьшение —
    // это человек (клавиши, скроллбар, инерция) — прилипание снимаем
    if (top < st.lastTop - 2) { leave(); st.lastTop = top; return; }
    st.lastTop = top;
    const run = active();
    /* BG: ПРИЛИПАНИЕ У ДНА — ПРОВЕРЯЕМ ПЕРВЫМ: раньше кадр собственного
       догона (autoPend) съедал событие «читатель доехал до низа», и
       прилипание не включалось. Доехал до низа — включаем всегда */
    if (run && h - top - box.clientHeight < 48) {
      run.followOutput = true;
      st.autoPend = 0;
      return;
    }
    if (st.autoPend > 0) { st.autoPend -= 1; return; }
  }, { passive: true });
}

/* BE: ДОГОНЯЮЩИЙ СКРОЛЛ. Вместо мгновенных прыжков — плавное сближение
   с дном: за кадр четверть остатка (и не меньше 3px). Пружинка сходится
   быстро, но зримо плавно; на время догона выключаем scroll-behavior,
   иначе браузер анимировал бы КАЖДУЮ запись. Жест «наверх» останавливает
   догон на следующем кадре. */
function chaseBottom(box, run) {
  const st = (box.__jarvisScroll = box.__jarvisScroll ||
    { lastTop: box.scrollTop, lastH: box.scrollHeight, autoPend: 0, chasing: false });
  if (st.chasing) return;
  st.chasing = true;
  box.classList.add('pin-instant');
  /* BJ: РОВНЫЙ ХОД БЕЗ РЫВКОВ. Прежняя кривая «разгонялась» к доле остатка
     (×1.22 за кадр): у большой агентской карточки скорость долетала до
     26% остатка за один-два кадра — глаз ловил бросок. Теперь скорость
     ПРЯМО ПРОПОРЦИОНАЛЬНА остатку и просто ограничена сверху: большое
     окно догоняется быстрым, но постоянным ходом, у дна ход плавно
     замирает (экспоненциальное затухание — как инерция у iOS). Ни
     разгона, ни ступенек: одна и та же плавная кривая у печати, у
     карточек и у панелей */
  const frame = () => {
    st.chasing = false;
    if (run && run.followOutput === false) { box.classList.remove('pin-instant'); return; }
    const gap = box.scrollHeight - box.scrollTop - box.clientHeight;
    if (gap <= 1) { box.classList.remove('pin-instant'); return; }
    st.autoPend += 1;
    const v = Math.min(24, Math.max(1.6, gap * 0.11));
    box.scrollTop = box.scrollTop + v;
    if (box.scrollHeight - box.scrollTop - box.clientHeight > 1) {
      st.chasing = true;
      requestAnimationFrame(frame);
    } else {
      box.classList.remove('pin-instant');
    }
  };
  requestAnimationFrame(frame);
}

/* BH: НЕПРЕРЫВНЫЙ ЖИВОЙ ДОГОН. В тихом режиме лента растёт мелкими шагами
   печати — chase вызывается часто и всё выглядит плавно. В агентском
   контент растёт ПОЗЖЕ события: строки панели входят с задержками,
   свёртки дожимаются, статус меняет высоту — разовые scrollDown это
   ловили одним поздним рывком. Пока в ленте есть живой ответ, тихий
   цикл каждый кадр ДОЕДАЕТ остаток тем же плавным ходом — как печать */
function followLiveStream(box) {
  if (!box || !box.addEventListener || !box.querySelector) return;
  if (typeof requestAnimationFrame !== 'function') return;
  const st = (box.__jarvisScroll = box.__jarvisScroll ||
    { lastTop: box.scrollTop, lastH: box.scrollHeight, autoPend: 0, chasing: false });
  if (st.liveLoop) return;
  st.liveLoop = true;
  /* BI: АГЕНТСКИЙ СКРОЛЛ = ТИХИЙ СКРОЛЛ, ДОСЛОВНО. Раньше жил собственный
     шаг 16%/кадр — по факту это ДРУГАЯ кривая движения, и глаз чувствовал
     разницу. Теперь живой цикл лишь СЛЕДИТ, что разгоняющийся chaseBottom
     всегда запущен, пока в ленте есть live-ответ: та же функция, тот же
     разгон, та же кривая, что у печати тихого режима */
  const frame = () => {
    st.liveLoop = false;
    if (!box.isConnected) return;
    if (!box.querySelector('.msg-ai.live')) return;   // ответ кончился — цикл угас
    const run = (S.followUi && runScrollBox(S.followUi) === box) ? S.followUi :
      ((S.liveUi && runScrollBox(S.liveUi) === box) ? S.liveUi : null);
    const gap = box.scrollHeight - box.scrollTop - box.clientHeight;
    if (gap > 1 && !st.chasing && !(run && run.followOutput === false)) {
      chaseBottom(box, run);
    }
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

function scrollDown(force, owner) {
  // Явный owner нужен typer-у AUTO, который не является основным SSE-run.
  const boxes = [];
  const cl = S.camNode && S.camNode.isConnected ? S.camNode.querySelector('.cam-chat') : null;
  if (cl) boxes.push(cl);
  const main = stream();
  if (main && !boxes.includes(main)) boxes.push(main);
  boxes.forEach((box) => {
    const run = owner || (S.followUi && runScrollBox(S.followUi) === box ? S.followUi : null);
    // без активного run-а следуем только из «зоны у дна»: далеко от низа
    // и без ответа — прокрутку не дёргаем
    const near = box.scrollHeight - box.scrollTop - box.clientHeight < 160;
    /* BF: force больше не всесилен: если человек ЯВНО ушёл вверх во время
       ответа (followOutput=false), агентские scrollDown(true) от карточек
       инструментов не тащат его обратно */
    const gone = !!(run && run.followOutput === false);
    if (!gone && (force || run || near)) {
      chaseBottom(box, run);
    }
  });
}

/* Controls входят строками с animation-delay. Если они принадлежат текущему
   ответу, следуем его явному intent; для остальных небольших UI сохраняем
   локальное wheel/touch cancellation. */
function followGrowingPanel(node, duration, owner) {
  if (!node || !node.isConnected) return;
  const box = node.closest('.cam-chat') || stream();
  const run = owner || (S.followUi && runScrollBox(S.followUi) === box ? S.followUi : null);
  if (!run && (!box || box.scrollHeight - box.scrollTop - box.clientHeight >= 220)) return;
  const st = (box.__jarvisScroll = box.__jarvisScroll ||
    { lastTop: box.scrollTop, lastH: box.scrollHeight, autoPend: 0, chasing: false });
  let cancelled = false;
  const cancel = () => { if (run) run.followOutput = false; else cancelled = true; cleanup(); };
  const cleanup = () => {
    box.removeEventListener('wheel', cancel);
    box.removeEventListener('touchstart', cancel);
  };
  box.addEventListener('wheel', cancel, { passive: true });
  box.addEventListener('touchstart', cancel, { passive: true });
  const until = performance.now() + (duration || 900);
  /* BI: свёртки/рост панелей догоняет ТОТ ЖЕ разгоняющийся chaseBottom —
     один механизм на всё: и печать, и панели, и агентские карточки */
  const frame = (now) => {
    if (cancelled || (run && run.followOutput === false) || !node.isConnected || now >= until) {
      cleanup(); return;
    }
    const gap = box.scrollHeight - box.scrollTop - box.clientHeight;
    if (gap > 1 && !st.chasing && !(run && run.followOutput === false)) {
      chaseBottom(box, run);
    }
    requestAnimationFrame(frame);
  };
  if (!run || run.followOutput !== false) chaseBottom(box, run);
  requestAnimationFrame(frame);
}
/* Приветствие с подсказками убирает ТОЛЬКО действие самого пользователя:
   отправленное сообщение или включённая камера. Раньше его сносили ещё и
   уведомления от Джарвиса с карточками подтверждения — они приходят сами,
   и подсказки исчезали из пустого диалога, хотя человек ничего не сделал. */
function killWelcome() {
  /* AY: приветствие, которое уже УХОДИТ своей анимацией, не убиваем —
     раньше addUserMsg срезал его на первом же кадре, и никакого
     растворения с разъездом плиток не происходило вовсе */
  const w = $('.welcome');
  if (w && w.dataset.exit !== '1') w.remove();
}

/* История открывается сразу в конечной позиции. Здесь намеренно нет каскада
   rAF/таймеров: он и был видимой поэтапной прокруткой. Поздняя картинка делает
   один мгновенный pin только в момент, когда получает реальную высоту. */
function pinToBottom(box) {
  if (!box) return;
  // CSS smooth-scroll превращал даже прямое присваивание scrollTop в видимый
  // проезд через всю историю. Каждая pin-транзакция временно и синхронно
  // выключает интерполяцию; класс снимается только после самой записи.
  const put = () => {
    box.classList.add('pin-instant');
    box.scrollTop = box.scrollHeight;
    box.classList.remove('pin-instant');
  };
  put();
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
/* Единственная длительность правки: и вход, и отмена живут ровно столько.
   Значение обязано совпадать с editIn/editOut в CSS. */
const EDIT_MS = 220;

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

  // ОДИНАКОВАЯ СКОРОСТЬ входа и выхода. Раньше вход был один шаг (220 мс),
  // а выход — два подряд: сначала уезжало поле (220 мс), потом ОТДЕЛЬНО
  // возвращался пузырь (240 мс). Итого 460 мс против 220 — отмена ощущалась
  // вдвое медленнее. Теперь оба движения идут ОДНОВРЕМЕННО: поле уходит и
  // пузырь возвращается в одни и те же EDIT_MS.
  // вернуть пузырь на место без анимации (когда правку отправляют)
  const restoreNow = () => {
    if (bubble) { bubble.style.display = ''; bubble.classList.remove('edit-back'); }
    if (acts) acts.style.display = '';
    if (vers) vers.style.display = '';
  };
  const close = (animated) => {
    if (box.dataset.closing === '1') return;
    box.dataset.closing = '1';
    if (animated === false) { box.remove(); restoreNow(); return; }
    // Замораживаем коробку РОВНО в тех размерах и координатах, что сейчас на
    // экране, и только потом вынимаем её из потока. Иначе в момент перехода
    // в absolute ширина пересчитается и поле прыгнет.
    const r = { w: box.offsetWidth, h: box.offsetHeight, t: box.offsetTop, l: box.offsetLeft };
    box.style.width = r.w + 'px';
    box.style.height = r.h + 'px';
    box.style.top = r.t + 'px';
    box.style.left = r.l + 'px';
    box.classList.add('edit-out');
    // пузырь проявляется ПАРАЛЛЕЛЬНО уходу поля, а не после него
    if (bubble) { bubble.style.display = ''; bubble.classList.add('edit-back'); }
    if (acts) acts.style.display = '';
    if (vers) vers.style.display = '';
    setTimeout(() => {
      box.remove();
      if (bubble) bubble.classList.remove('edit-back');
    }, EDIT_MS);
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

function addUserMsg(text, atts, info, hostOverride) {
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
    // Источник истины — то, что СЕЙЧАС в пузыре, а не текст, захваченный при
    // создании кнопки. Из-за замыкания на старое значение переключение версий
    // копировало исходный вариант вместо выбранного.
    const live = bubbleText(m.querySelector('.bubble-user')) || text;
    navigator.clipboard.writeText(live).then(
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

  const host = hostOverride || msgHost();
  host.appendChild(m);
  placeDaySeparator(m);
  scrollDown(true);
  return m;
}

/* AS: у ответа — КРУГЛЕШОК, ядро реактора без колец. Живой: дышит в покое,
   разгорается при печати. Рождается перелётом из большого ядра приветствия
   (первый запрос диалога). Электрической эстафеты больше нет: реактор в
   доке живёт своей жизнью и никуда не гаснет. */
const AVATAR_CORE = '<div class="ai-core" aria-hidden="true"></div>';

function welcomeExit() {
  /* AY: ЕДИНАЯ БОЛЬШАЯ АНИМАЦИЯ ОТКРЫТИЯ ДИАЛОГА. Плитки разъезжаются
     врозь и растворяются, страница тает И складывается по высоте — всё
     одной кривой cubic-bezier(.65,0,.35,1) и одной длительностью с
     полётом призраков. Корни прежнего «плитки не разъезжаются»:
     (1) входная анимация popIn .45s both держала свои ключевые кадры
     поверх инлайновых transform/opacity — снимаем её явно;
     (2) addUserMsg убивал welcome мгновенно — теперь помечаем
     dataset.exit, и killWelcome даёт анимации доиграть до конца */
  const w = document.querySelector('.welcome');
  if (!w) return;
  w.dataset.exit = '1';
  $$('.sugg', w).forEach((t, i) => {
    t.style.animation = 'none';
    t.style.transition = 'transform .62s cubic-bezier(.65,0,.35,1), opacity .62s cubic-bezier(.65,0,.35,1)';
    void t.offsetWidth;
    t.style.transform = 'translateX(' + (i % 2 ? 120 : -120) + 'px) scale(.93)';
    t.style.opacity = '0';
  });
  w.style.animation = 'none';
  const h = w.offsetHeight;
  w.style.transition = 'height .62s cubic-bezier(.65,0,.35,1), opacity .62s cubic-bezier(.65,0,.35,1), margin .62s cubic-bezier(.65,0,.35,1)';
  /* режем по ВЕРТИКАЛЬНОЙ рамке (высота складывается), а по горизонтали
     даём плиткам выехать за край — как уход за кадр */
  w.style.clipPath = 'inset(0 -300px 0 -300px)';
  w.style.height = h + 'px';
  void w.offsetWidth;
  w.style.height = '0px';
  w.style.opacity = '0';
  w.style.marginTop = '0px';
  setTimeout(() => { if (w.isConnected) w.remove(); }, 700);
}

function flyWelcomeInto(node, wf) {
  if (!wf || !node || !node.root) return;
  const core = node.root.querySelector('.ai-core');
  const name = node.root.querySelector('.ai-name');
  /* AW: до прилёта места назначения ПУСТЫ — круглешка и имени ещё нет */
  if (core && core.classList) core.classList.add('pre-flight');
  if (name && name.classList) name.classList.add('pre-flight');
  /* полёт стартует СРАЗУ — призраки созданы в момент отправки */
  if (core && wf.ghostCore) {
    const rings = $$('.gr', wf.ghostCore);
    flyGhost(wf.ghostCore, () => core.getBoundingClientRect(), () => {
      core.classList.remove('pre-flight');
    }, (p, e) => {
      /* AY: кольца тают СО СКОРОСТЬЮ ПОЛЁТА — по той же кривой, что и
         движение: не спешат впереди, к посадке остаётся круглешок */
      rings.forEach((r) => { r.style.opacity = String(Math.max(0, 1 - e)); });
    });
  }
  if (name && wf.ghostTitle) {
    /* цель — САМ ТЕКСТ «JARVIS» в имени, не flex-строка на всю колонку */
    const titleTarget = () => {
      const tn = name.firstChild;
      if (tn && tn.nodeType === 3) {
        const rg = document.createRange();
        rg.selectNode(tn);
        return rg.getBoundingClientRect();
      }
      return name.getBoundingClientRect();
    };
    const solid = wf.ghostTitle.querySelector('.gt-solid');
    flyGhost(wf.ghostTitle, titleTarget, () => {
      name.classList.remove('pre-flight');
    }, (p) => {
      /* AX: цвет надписи меняется В ПОЛЁТЕ — градиент уступает место
         цветному слою во второй половине пути */
      if (solid) solid.style.opacity = String(Math.max(0, Math.min(1, (p - .35) / .45)));
    });
  }
  /* страховка: в фоновой вкладке анимации замирают — имя и ядро всё
     равно проявятся */
  setTimeout(() => {
    if (core) core.classList.remove('pre-flight');
    if (name) name.classList.remove('pre-flight');
  }, 1600);
}

/* Полёт с ЖИВЫМ наведением: цель перемеряется каждый кадр, позиция и
   масштаб идут по мягкой S-кривой, onProgress даёт призраку менять вид
   В ПОЛЁТЕ (кольца тают, цвет перетекает). Приземление — кроссфейд. */
function flyGhost(g, targetRect, done, onProgress) {
  const start = g.getBoundingClientRect();
  const t0 = performance.now();
  const dur = 620;
  g.dataset.landed = '0';
  /* AY: ЕДИНАЯ КРИВАЯ всего ухода — численно тот же cubic-bezier
     (.65,0,.35,1), что ведёт плитки и страницу: полёт, разъезд и
     растворение — одно движение одним темпом */
  const ease = cubicBezierEase(.65, 0, .35, 1);
  let lx = 0, ly = 0;
  const tick = () => {
    const p = Math.min(1, (performance.now() - t0) / dur);
    const e = ease(p);
    if (onProgress) onProgress(p, e);
    const to = targetRect();
    const k = Math.max(0.06, to.width / Math.max(1, start.width));
    const dx = (to.left + to.width / 2) - (start.left + start.width / 2);
    const dy = (to.top + to.height / 2) - (start.top + start.height / 2);
    const x = dx * e, y = dy * e;
    /* AY/BB: КИНОШНОЕ РАЗМЫТИЕ В ДВИЖЕНИИ. Раньше filter переписывался
       КАЖДЫЙ кадр — стиль-пересчёт и перерисовка с блюром рвали FPS.
       Теперь класс-ступенька: браузер сам ведёт transition фильтра,
       JS лишь изредка переключает «в движении / встал» */
    const speed = Math.hypot(x - lx, y - ly);
    const moving = speed > 7;
    if (moving !== g._motion) {
      g._motion = moving;
      g.classList.toggle('motion', moving);
    }
    lx = x; ly = y;
    g.style.transform = 'translate(' + x + 'px,' + y + 'px) scale(' +
      (1 + (k - 1) * e) + ')';
    if (p < 1) { requestAnimationFrame(tick); return; }
    g.dataset.landed = '1';
    g._motion = false;
    g.classList.remove('motion');
    /* мягкая посадка: кроссфейд 180мс вместо мгновенной подмены */
    const fade = g.animate([{ opacity: 1 }, { opacity: 0 }],
      { duration: 180, fill: 'both' });
    fade.onfinish = () => g.remove();
    done();
  };
  requestAnimationFrame(tick);
}

/* AY: кубик-безье для JS-анимаций — то же семейство кривых, что и CSS
   transition cubic-bezier(...): полёт призраков движется строго той же
   кривой, что растворение страницы (Ньютон по x, значение по y) */
function cubicBezierEase(x1, y1, x2, y2) {
  const cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx;
  const cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
  const sampleX = (t) => ((ax * t + bx) * t + cx) * t;
  const sampleY = (t) => ((ay * t + by) * t + cy) * t;
  const slopeX = (t) => (3 * ax * t + 2 * bx) * t + cx;
  return (x) => {
    if (x <= 0) return 0;
    if (x >= 1) return 1;
    let t = x;
    for (let i = 0; i < 7; i++) {
      const err = sampleX(t) - x;
      if (Math.abs(err) < 1e-5) break;
      const d = slopeX(t);
      if (Math.abs(d) < 1e-6) break;
      t -= err / d;
    }
    return sampleY(t);
  };
}
function addAiMsg(ts, hostOverride) {
  const m = el('div', 'msg msg-ai');
  m.innerHTML =
    '<div class="ai-avatar">' + AVATAR_CORE + '</div>' +
    '<div class="ai-body"><div class="ai-name">JARVIS<span class="ai-model"></span></div>' +
    '<div class="ai-content"></div></div>';
  stampTime(m, ts);
  const host = hostOverride || msgHost();
  host.appendChild(m);
  placeDaySeparator(m);
  scrollDown(true);
  return {
    root: m,
    body: m.querySelector('.ai-content'),
    modelEl: m.querySelector('.ai-model'),
  };
}

/* Сценарий и точная модель принадлежат конкретному ответу и остаются над ним
   после завершения и после повторного открытия диалога. Оба значения живут в
   одной спокойной строке, а не конкурируют за место вокруг имени JARVIS. */
function updateResponseMeta(ui) {
  if (!ui || !ui.node || !ui.node.root) return;
  const scenario = ui.routeTier ? (TIER_LABEL[ui.routeTier] || ui.routeTier) : '';
  const model = String(ui.modelName || '').trim();
  if (!scenario && !model) return;
  if (!ui.routeEl || !ui.routeEl.isConnected) {
    ui.routeEl = el('span', 'ai-route');
    const head = ui.node.root.querySelector('.ai-name');
    if (head) head.appendChild(ui.routeEl);
  }
  const parts = [];
  // Это тихий технический паспорт ответа, не заголовок: сценарий намеренно
  // остаётся со строчной буквы, как и просил пользователь.
  if (scenario) parts.push(scenario);
  if (model) parts.push(model);
  ui.routeEl.textContent = parts.join(' · ');
  ui.routeEl.title = ui.routeReason || parts.join(' · ');
  // Старый отдельный model span сохраняется в разметке для совместимости с
  // историей beta, но новый контракт имеет один недублирующийся meta-узел.
  if (ui.node.modelEl) ui.node.modelEl.textContent = '';
}

function addMsgActions(node, text) {
  // Продолжение ответа (ui-панель) добавляет действия второй раз — старые
  // кнопки не копятся: сначала снимаем предыдущий ряд.
  if (node.body) $$('.msg-actions', node.body).forEach((a) => a.remove());
  const acts = el('div', 'msg-actions');
  // Что скопировать/озвучить, решаем в момент нажатия по живому узлу ответа:
  // если ответ перерисовали (другая версия вопроса), текст будет уже новый.
  // Продолженный ответ состоит из нескольких .md — берём их все.
  const liveText = () => {
    const parts = node.body ? $$('.md', node.body).map((md) =>
      (md.innerText || md.textContent || '').trim()) : [];
    const t = parts.join('\n\n').trim();
    return t || text;
  };
  const copy = el('button', 'act act-copy', ICO.copy + '<span>Копировать</span>');
  copy.addEventListener('click', () => {
    navigator.clipboard.writeText(liveText()).then(() => toast('Скопировано', 'success'));
  });
  const speak = el('button', 'act act-speak', ICO.speak + '<span>Озвучить</span>');
  speak.addEventListener('click', () => {
    try {
      const u = new SpeechSynthesisUtterance(liveText().replace(/[#*`>|\-]/g, '').slice(0, 900));
      u.lang = 'ru-RU'; u.rate = 1.03;
      speechSynthesis.cancel(); speechSynthesis.speak(u);
    } catch (e) { toast('Синтез речи недоступен', 'error'); }
  });
  const again = el('button', 'act act-again', ICO.again + '<span>Ещё раз</span>');
  // «Ещё раз» значит ровно одно: переспросить то же самое прямо сейчас.
  // Раньше нажатие открывало правку и ждало подтверждения — но правка уже
  // есть отдельной кнопкой под вопросом, и лишний шаг только мешал: человек
  // просил повтор, а получал форму. Отправляем немедленно, тем же текстом,
  // и ответ становится новой версией того же вопроса, а не дублем в ленте.
  again.addEventListener('click', () => {
    let ask = node.root.previousElementSibling;
    while (ask && !ask.classList.contains('msg-user')) ask = ask.previousElementSibling;
    if (!ask) { $('#input').value = S.lastPrompt || ''; autoGrow(); send(); return; }
    // если правка этого вопроса открыта — берём то, что человек уже набрал
    const open = ask.querySelector('.edit-box');
    let text = bubbleText(ask.querySelector('.bubble-user')) || S.lastPrompt || '';
    if (open) {
      const ta = open.querySelector('.edit-ta');
      if (ta && ta.value.trim()) text = ta.value.trim();
      open.remove();
      const bubble = ask.querySelector('.bubble-user');
      const acts2 = ask.querySelector('.msg-actions');
      const vers = ask.querySelector('.ver-switch');
      if (bubble) bubble.style.display = '';
      if (acts2) acts2.style.display = '';
      if (vers) vers.style.display = '';
    }
    submitEdit(ask, text);
  });
  acts.appendChild(copy); acts.appendChild(speak); acts.appendChild(again);
  node.body.appendChild(acts);
}

/* --- составные карточки внутри ответа --- */
function memoryTraceCard(facts) {
  const items = (facts || []).filter((fact) => fact && (fact.key || fact.value));
  const card = makeCard('✓', 'Запомнить', 'tool-card memory-tool', true);
  const kv = el('div', 'kv');
  items.forEach((fact) => {
    const key = el('i', ''); key.textContent = String(fact.key || 'Факт');
    const value = el('span', ''); value.textContent = String(fact.value || '');
    kv.appendChild(key); kv.appendChild(value);
  });
  card.inner.appendChild(kv);
  return card;
}

/* Восстановить ход мыслей и список действий у сохранённого ответа.
   Показываем сразу свёрнутыми строчками — история не теряется, но и не мешает. */
/* Прошлые инструменты — ТИХАЯ КУХНЯ: папки семейств со строками, точно как
   выглядит конец тихого ответа. Режим, включённый сейчас, не перекрашивает
   историю: AGENT меняет только текущую анимацию и дизайн новых карточек. */
function renderToolKitchen(node, traces) {
  if (!traces.length) return;
  const groups = new Map();
  traces.forEach((t) => {
    const g = t.group || 'base';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(t);
  });
  groups.forEach((items, g) => {
    const f = el('div', 'qt-folder');
    f.dataset.group = g;
    f._items = items.map((t) => ({
      name: t.name, label: t.label || t.name, group: g,
      args: t.args || {}, ok: true, elapsed: t.elapsed,
    }));
    f.innerHTML =
      '<div class="qt-head">' +
        '<span class="qt-ico">' + qtFamilyIcon(g, '') + '</span>' +
        '<span class="qt-name"></span>' +
        '<span class="qt-mark"></span>' +
      '</div>' +
      '<div class="qt-kids"><span class="qt-rail"></span><div class="qt-rows"></div></div>';
    f.querySelector('.qt-head').addEventListener('click', () => qtToggleFolder(f));
    qtFolderSync(f);
    node.body.appendChild(f);
  });
}

/* Y: ПРОШЛЫЕ ИНСТРУМЕНТЫ АГЕНТСКОГО ОТВЕТА — АГЕНТСКИМ ДИЗАЙНОМ: одна
   групповая карточка на семейство, свёрнутая в строку. Дизайн истории
   повторяет дизайн ЖИВОГО ответа (meta.agent), а не текущий режим:
   тихий ответ никогда не «переодевается» в агентский при возврате. */
function renderAgentTraceGroups(node, traces) {
  if (!traces.length) return;
  const groups = new Map();
  traces.forEach((t) => {
    const g = t.group || 'base';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(t);
  });
  groups.forEach((items, g) => {
    const fam = QT_FAMILY[g] || QT_FAMILY.base;
    const card = makeCard(qtFamilyIcon(g, ''), fam.label, 'tool-card ag-group', true);
    const head = card.querySelector('.k');
    if (head) head.innerHTML = esc(fam.label) + '<span class="ag-count"> × ' + items.length + '</span>';
    const rows = el('div', 'ag-rows');
    items.forEach((t) => {
      const row = el('div', 'ql-row');
      row.innerHTML =
        '<span class="ql-head">' +
          '<span class="qs-ico">' + qtFamilyIcon(t.group || g, t.name) + '</span>' +
          '<span class="ql-label">' + esc(t.label || t.name) + '</span>' +
          '<span class="qs-state">✓</span>' +
        '</span>' +
        '<pre class="ql-detail">' + esc(qtDetail(t.args || {}, t.result || {})) + '</pre>';
      row.addEventListener('click', () => {
        const det = row.querySelector('.ql-detail');
        const willOpen = !row.classList.contains('open');
        row.classList.toggle('open', willOpen);
        if (det && det.animate) {
          const h = det.scrollHeight;
          det.style.overflow = 'hidden';
          const anim = det.animate(
            willOpen
              ? [{ height: '0px', opacity: '0' }, { height: h + 'px', opacity: '1' }]
              : [{ height: h + 'px', opacity: '1' }, { height: '0px', opacity: '0' }],
            { duration: willOpen ? 440 : 380,
              easing: willOpen ? 'cubic-bezier(.22,.8,.3,1)' : 'cubic-bezier(.3,.6,.3,1)' });
          anim.finished.then(() => { det.style.overflow = ''; }).catch(() => { det.style.overflow = ''; });
        }
      });
      rows.appendChild(row);
    });
    card.inner.appendChild(rows);
    node.body.appendChild(card);
    collapseToThumb(card, {
      cls: 'th-ok', icon: ICO.code, title: fam.label + ' × ' + items.length,
      sub: '', tag: 'готово', instant: true,
    });
  });
}

function restoreTrace(node, meta) {
  const think = (meta.thinking || '').trim();
  const toolTraces = [];
  const agentAnswer = !!meta.agent;   // Y: дизайн истории = дизайн ответа
  if (think) {
    if (agentAnswer) {
      const card = makeCard('◇', 'Ход мыслей', 'think-card', false);
      const ts = el('div', 'think-stream');
      // AN: история показывает тот же живой формат, что и бегущий ответ
      ts.textContent = thinkFormat(think);
      card.inner.appendChild(ts);
      node.body.appendChild(card);
      collapseToThumb(card, { cls: 'th-think', icon: '◇', title: 'Ход мыслей',
        sub: thinkFormat(think).split('\n')[0].slice(0, 60), tag: 'свёрнут', instant: true });
    } else {
      // Y: тихий ответ — тихая строка кухни, без агентской карточки.
      // AA: строки мыслей hydrated внутрь и спрятаны — клик по строке
      // раскрывает поток, как у живого ответа (раньше строка была мёртвой)
      const row = el('div', 'qt-node qt-think');
      row.innerHTML =
        '<div class="qt-head">' +
          '<span class="qt-ico">◇</span>' +
          '<span class="qt-name">Ход мыслей</span>' +
          '<span class="qt-mark">✓ ' + fmtSize(think.length) + '</span>' +
        '</div>' +
        '<div class="qt-body" style="height:0;opacity:0;overflow:hidden"><span class="qt-rail"></span>' +
          '<div class="qt-flow"><div class="qt-flowin"></div></div></div>';
      const inner = row.querySelector('.qt-flowin');
      String(think).split(/\n+/).map((s) => s.trim()).filter(Boolean)
        .forEach((para) => {
          // длинную мысль режем по границам слов на строки потока — открытие
          // показывает тот же «бегущий» вид, что и живой ответ
          let rest = para;
          while (rest.length > 96) {
            const sp = rest.lastIndexOf(' ', 96);
            const cut = sp > 40 ? sp : 96;
            inner.appendChild(el('div', 'qt-flowline', esc(rest.slice(0, cut))));
            rest = rest.slice(cut).replace(/^\s+/, '');
          }
          if (rest) inner.appendChild(el('div', 'qt-flowline', esc(rest)));
        });
      row.querySelector('.qt-head').addEventListener('click', () => qtToggleThink(row));
      node.body.appendChild(row);
    }
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
    } else if (t.kind === 'memory') {
      const facts = t.facts || [];
      const card = memoryTraceCard(facts);
      node.body.appendChild(card);
      collapseToThumb(card, { cls: 'th-ok', icon: '✓', title: 'Запомнить',
        sub: facts.map((fact) => fact.value || '').filter(Boolean).join(', ').slice(0, 80),
        tag: 'готово', instant: true });
    } else if (t.kind === 'tool') {
      // собираем в кухню после цикла (нужны все инструменты сразу)
      toolTraces.push(t);
    } else if (t.kind === 'question') {
      // заданный ранее вопрос и выбранный ответ — сразу свёрнуты в строку
      const card = questionCard(t, null);
      node.body.appendChild(card);
      collapseToThumb(card, { cls: 'th-ask', icon: '?', title: 'Вопрос',
        sub: t.question || '', tag: t.answer || 'без ответа', instant: true });
    }
  });
  if (agentAnswer) renderAgentTraceGroups(node, toolTraces);
  else renderToolKitchen(node, toolTraces);
}

/* ============ РАСКРЫТИЕ И ЗАКРЫТИЕ ТЕЛА КАРТОЧКИ ============
   ГЛУБИННАЯ ПРИЧИНА ВСЕХ «ЛАГОВ» ПРИ РАСКРЫТИИ. Высота анимировалась через
   max-height от 0 до ВЫДУМАННОЙ константы 900px. Но max-height не равен
   высоте: пока значение больше реального содержимого, элемент уже стоит на
   месте и не движется. Считаем честно: содержимое 200px из 900 — блок стоит
   неподвижно 78% времени анимации (327 мс из 420). Вот это неподвижное
   ожидание глаз и читает как «подвисло». Никакая подгонка длительности или
   кривой это не лечит — лечится только отказом от выдуманной константы.
   Поэтому высота меряется по факту (scrollHeight) и анимируется от реальной
   к реальной: мёртвого времени не остаётся вовсе. По окончании раскрытия
   ограничение снимается совсем (max-height:none) — иначе живая карточка,
   в которую ещё текут мысли, упёрлась бы в потолок. */
const CARD_OPEN_MS = 260;

function setCardOpen(card, open, instant) {
  if (!card) return;
  const head = card.querySelector(':scope > .card-head');
  const body = card.querySelector(':scope > .card-body');
  if (!head || !body) return;
  head.classList.toggle('open', open);
  body.classList.toggle('open', open);
  if (body._ct) { clearTimeout(body._ct); body._ct = null; }

  if (instant) {
    body.style.transition = 'none';
    body.style.maxHeight = open ? 'none' : '0px';
    void body.offsetHeight;
    body.style.transition = '';
    return;
  }
  // старт — фактическая высота сейчас, финиш — фактическая высота содержимого
  const from = body.getBoundingClientRect().height;
  const to = open ? body.scrollHeight : 0;
  body.style.transition = 'none';
  body.style.maxHeight = from + 'px';
  void body.offsetHeight;                       // зафиксировать точку отсчёта
  // короткие блоки не должны ехать столько же, сколько длинные: время
  // пропорционально реальному пути, иначе маленькая карточка «тянется»
  const dur = Math.max(140, Math.min(CARD_OPEN_MS, 120 + Math.abs(to - from) * 0.35));
  body.style.transition = 'max-height ' + Math.round(dur) + 'ms cubic-bezier(.33,1,.68,1)';
  body.style.maxHeight = to + 'px';
  body._ct = setTimeout(() => {
    body._ct = null;
    body.style.transition = '';
    // раскрытая карточка живёт без потолка — содержимое может расти дальше
    body.style.maxHeight = open ? 'none' : '0px';
  }, Math.round(dur) + 20);
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
  if (openByDefault) body.style.maxHeight = 'none';
  const toggle = () => setCardOpen(card, !body.classList.contains('open'));
  head.addEventListener('click', toggle);
  // Свернуть можно кликом в ЛЮБОМ месте карточки — так просил пользователь.
  // Исключение ровно одно и закрытое: интерактивное содержимое (ссылки, поля,
  // кнопки) и выделение текста, иначе карточка схлопывалась бы при попытке
  // скопировать её содержимое или нажать кнопку внутри.
  body.addEventListener('click', (e) => {
    if (window.getSelection && String(window.getSelection()).length) return;
    if (e.target.closest('a,button,input,textarea,select,label,.file-chip,.img-out')) return;
    // строка-инструмент внутри группы — самостоятельный клик: разворачиваем
    // её, а НЕ сворачиваем группу, внутри которой она живёт
    if (e.target.closest('.ql-row')) return;
    toggle();
  });
  card.inner = card.querySelector('.card-inner');
  card.setTitle = (t) => { card.querySelector('.t').innerHTML = t; };
  card.body = body;
  return card;
}

/* =================== ПРЕДПРОСМОТР ФАЙЛА ===================
   Единственный вход — одиночный клик по файлу в переписке (см. attachFileChip).
   Ни двойной клик, ни лайтбокс, ни «открыть в новой вкладке» больше не
   участвуют: у одного действия должен быть один результат. */
let PREV_FILE = null;

function closePreview() {
  const p = $('#fprev');
  if (p) p.classList.remove('open');
  PREV_FILE = null;
}

function openPreview(f) {
  const box = $('#fprev');
  if (!box || !f) return;
  PREV_FILE = f;
  $('#fprevIco').innerHTML = fileIcon(f.name);
  $('#fprevName').textContent = f.name || 'файл';
  $('#fprevSize').textContent = f.size != null ? fmtSize(f.size) : '';
  const body = $('#fprevBody');
  const foot = $('#fprevPath'), stat = $('#fprevStat');
  // строка состояния внизу — как у терминала: где лежит файл и что с ним
  if (foot) foot.textContent = '~/JARVIS/workspace/' + (f.path || f.name || '');
  if (stat) stat.textContent = '';
  box.classList.add('open');

  if (isImg(f.name)) {
    body.innerHTML = '<img src="' + esc(f.url) + '" alt="' + esc(f.name || '') + '">';
    if (stat) stat.textContent = 'изображение';
    return;
  }
  body.innerHTML = '<div class="fprev-none">читаю файл…</div>';
  // Текст тянем через тот же /api/files/view, что и файловый менеджер:
  // второй способ читать файл означал бы второй набор ошибок.
  const q = '/api/files/view?name=' + encodeURIComponent(f.path || f.name || '') +
            '&chat_id=' + encodeURIComponent(f.chat_id || activeChatId() || '');
  api(q).then((r) => {
    if (PREV_FILE !== f) return;                 // пока читали, открыли другой
    if (!r || !r.ok) { body.innerHTML = '<div class="fprev-none">не удалось прочитать файл</div>'; return; }
    if (r.kind === 'image') { body.innerHTML = '<img src="' + esc(r.download_url) + '">'; return; }
    if (r.kind === 'text') {
      body.innerHTML = '<pre></pre>';
      const txt = r.content || '';
      body.querySelector('pre').textContent = txt;
      if (stat) stat.textContent = txt.split('\n').length + ' строк';
      return;
    }
    body.innerHTML = '<div class="fprev-none">Предпросмотр недоступен для этого типа.<br>Файл можно скачать кнопкой выше.</div>';
  }).catch(() => {
    if (PREV_FILE === f) body.innerHTML = '<div class="fprev-none">не удалось прочитать файл</div>';
  });
}

/* Кнопка скачивания в углу карточки файла. Отдельной функцией, потому что
   нужна и картинке, и «фишке» файла: один источник поведения на оба случая. */
function dlCorner(host, f) {
  const b = el('button', 'chip-dl', ICO.dl);
  b.title = 'Скачать ' + (f.name || 'файл');
  b.addEventListener('click', (e) => {
    e.preventDefault(); e.stopPropagation();
    const a = el('a'); a.href = f.url; a.download = f.name || '';
    document.body.appendChild(a); a.click(); a.remove();
    sfx('ok');
  });
  host.appendChild(b);
  return b;
}

function attachFileChip(container, f) {
  if (isImg(f.name)) {
    // обёртка нужна, чтобы кнопку можно было поставить в угол картинки
    const wrap = el('div', 'img-wrap');
    const img = el('img', 'img-out');
    img.src = f.url; img.alt = f.name; img.loading = 'lazy';
    // Одиночный клик по файлу = предпросмотр. Лайтбокс убран: у пользователя
    // должен быть ровно один способ открыть файл, иначе картинка и документ
    // ведут себя по-разному без всякой причины.
    img.addEventListener('click', () => openPreview(f));
    wrap.appendChild(img);
    dlCorner(wrap, f);
    container.appendChild(wrap);
  }
  const a = el('a', 'file-chip');
  a.href = f.url;
  a.innerHTML = '<span class="fi">' + fileIcon(f.name) + '</span><span>' + esc(f.name) +
    '</span><small>' + fmtSize(f.size) + '</small>';
  a.addEventListener('click', (e) => { e.preventDefault(); openPreview(f); });
  // файл из диалога можно ПЕРЕТАЩИТЬ в песочницу — тем же жестом и с той же
  // анимацией шлейфа, что и перенос карточек внутри «Файлов»
  a.draggable = true;
  a.dataset.path = f.path || f.name;
  a.addEventListener('dragstart', (e) => {
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/jarvis-path', a.dataset.path);
    e.dataTransfer.setData('text/jarvis-paths', JSON.stringify([a.dataset.path]));
    // ссылка и имя — страховка: если файла уже нет в песочнице диалога,
    // перенос всё равно сработает: содержимое ввезётся по ссылке
    e.dataTransfer.setData('text/jarvis-url', f.url || '');
    e.dataTransfer.setData('text/jarvis-name', f.name || '');
    e.dataTransfer.setData('text/plain', f.name);
    startDragGhosts(e, [a], a);
  });
  dlCorner(a, f);
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
  dl: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v10"/><path d="M8 11l4 4 4-4"/><path d="M5 19h14"/></svg>',
  // та же стрелка, что на кнопке отправки под полем ввода
  send: '<svg viewBox="0 0 24 24"><path d="M3 20l18-8L3 4v6l12 2-12 2z"/></svg>',
  copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2.4"/><path d="M5.5 15H5a1.9 1.9 0 0 1-1.9-1.9V5A1.9 1.9 0 0 1 5 3.1h8.1A1.9 1.9 0 0 1 15 5v.5"/></svg>',
  edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h8"/><path d="M16.4 3.6a2.1 2.1 0 0 1 3 3L7.5 18.5 3.5 20l1.5-4z"/></svg>',
  speak: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9.5v5h3.5L12 18.5v-13L7.5 9.5z"/><path d="M16 8.6a4.6 4.6 0 0 1 0 6.8"/><path d="M18.6 6a8 8 0 0 1 0 12"/></svg>',
  again: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M20.5 12a8.5 8.5 0 1 1-2.6-6.1"/><path d="M20.6 4.4v4.4h-4.4"/></svg>',
  cam: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="2.8" y="6.5" width="13" height="11" rx="2.2"/><path d="M15.8 11l5.4-3v8l-5.4-3z"/></svg>',
  bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M18 9a6 6 0 0 0-12 0c0 5-2 6.5-2 6.5h16S18 14 18 9z"/><path d="M10.3 19.5a2 2 0 0 0 3.4 0"/></svg>',
  code: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9 17.5L3.5 12 9 6.5"/><path d="M15 6.5L20.5 12 15 17.5"/></svg>',
  think: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.2l2.6 5.6 6 .7-4.5 4.1 1.3 6-5.4-3-5.4 3 1.3-6L3.4 9.5l6-.7z"/></svg>',
  shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7.5 3v5.6c0 4.6-3.1 8-7.5 9.4-4.4-1.4-7.5-4.8-7.5-9.4V6z"/></svg>',
  pause: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M8 5v14M16 5v14"/></svg>',
  play: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 5.5l10 6.5-10 6.5z"/></svg>',
  // Смысловые иконки ИНСТРУМЕНТОВ: по ним читается работа без подписи.
  // Глобус — поиск в интернете; окно — открытие страницы; мозг — память/мысли;
  // глаз — взгляд на экран; курсор — управление компьютером; терминал и
  // шестерня — код и служебные действия; документ с пером — запись файла.
  globe: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="12" cy="12" r="8.6"/><path d="M3.4 12h17.2M12 3.4c2.6 2.4 3.9 5.4 3.9 8.6s-1.3 6.2-3.9 8.6c-2.6-2.4-3.9-5.4-3.9-8.6s1.3-6.2 3.9-8.6z"/></svg>',
  win: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><rect x="3.4" y="4.4" width="17.2" height="15.2" rx="2.4"/><path d="M3.4 9.2h17.2M6.4 6.8h.01M9 6.8h.01M11.6 6.8h.01"/></svg>',
  brain: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 3.5a2.6 2.6 0 0 0-2.6 2.6c-1.4.2-2.4 1.4-2.4 2.8 0 .6.2 1.2.5 1.6-.5.5-.8 1.1-.8 1.9 0 1.2.8 2.2 1.9 2.5 0 1.6 1.3 2.9 2.9 2.9.7 0 1.3-.2 1.8-.7V3.9c-.4-.2-.8-.4-1.3-.4z"/><path d="M14.5 3.5a2.6 2.6 0 0 1 2.6 2.6c1.4.2 2.4 1.4 2.4 2.8 0 .6-.2 1.2-.5 1.6.5.5.8 1.1.8 1.9 0 1.2-.8 2.2-1.9 2.5 0 1.6-1.3 2.9-2.9 2.9-.7 0-1.3-.2-1.8-.7V3.9c.4-.2.8-.4 1.3-.4z"/><path d="M12 19.5v1.2"/></svg>',
  eye: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 12S6 5.8 12 5.8 21.5 12 21.5 12 18 18.2 12 18.2 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.9"/></svg>',
  cursor: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"><path d="M5.5 3.5l14 7.7-6.3 1.5 2.6 6.1-2.6 1.1-2.6-6.1-4.4 4.2z"/></svg>',
  term: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3.2" y="4.4" width="17.6" height="15.2" rx="2.4"/><path d="M7 9.5l3 2.8-3 2.8M12.4 15.6h4.4"/></svg>',
  gear: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.1"/><path d="M12 2.8l1 2.6 2.7-.7 1.4 2.4-1.9 2 1.9 2-1.4 2.4-2.7-.7-1 2.6h-1l-1-2.6-2.7.7-1.4-2.4 1.9-2-1.9-2 1.4-2.4 2.7.7 1-2.6z" transform="translate(0 1.2)"/></svg>',
  docpen: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3.5h8.5L18.5 7.5v6.8l-4.3 4.2H6z"/><path d="M14 3.7v4h4.3"/><path d="M12.6 17.6l5.8-5.8 2 2-5.8 5.8-2.5.5z"/></svg>',
  doc: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3.5h8.5L18.5 7.5v13H6z"/><path d="M14 3.7v4h4.3"/><path d="M9 12h6M9 15.4h6"/></svg>',
  cloud: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M7 18.5a4.2 4.2 0 0 1-.4-8.4 5.4 5.4 0 0 1 10.5 1.2 3.6 3.6 0 0 1-.6 7.2z"/></svg>',
  spark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.2l2.6 5.6 6 .7-4.5 4.1 1.3 6-5.4-3-5.4 3 1.3-6L3.4 9.5l6-.7z"/></svg>',
};

/* Свернуть блок в компактную строку-миниатюру.
   Клик по миниатюре разворачивает исходный блок обратно. */
/* Рост карточки из миниатюры: анимируем НАСТОЯЩУЮ высоту, а не transform.
   Только так содержимое под карточкой едет вместе с ней, а не прыгает на
   сотни пикселей в первом же кадре. Длительность — по реальному пути, как в
   setCardOpen: короткий блок не должен тянуться столько же, сколько длинный. */
function growHeight(node, from, to) {
  if (!node || !(to > 0) || Math.abs(to - from) < 4) return;
  if (node._gt) { clearTimeout(node._gt); node._gt = null; }
  const dur = Math.max(200, Math.min(360, 150 + Math.abs(to - from) * 0.3));
  const prev = node.style.overflow;
  node.style.overflow = 'hidden';
  node.style.transition = 'none';
  node.style.height = from + 'px';
  void node.offsetHeight;                       // зафиксировать точку отсчёта
  node.style.transition = 'height ' + Math.round(dur) + 'ms cubic-bezier(.25,.75,.3,1)';
  node.style.height = to + 'px';
  node._gt = setTimeout(() => {
    node._gt = null;
    // снимаем потолок: содержимое может расти дальше (стрим, картинки)
    node.style.transition = '';
    node.style.height = '';
    node.style.overflow = prev || '';
  }, Math.round(dur) + 20);
}

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
      '<div class="ai-avatar">' + AVATAR_CORE + '</div>' +
      '<div class="ai-body"></div>';
    holder.querySelector('.ai-body').appendChild(thumb);
  }
  const put = () => {
    if (!node.parentNode) return;
    node.parentNode.insertBefore(holder, node);
    node.style.display = 'none';
    node.classList.remove('collapsing', 'shrinking');
    // БАГ «КОД ПОСЛЕ РАЗВОРОТА — ТЁМНЫЙ И ПОЛУСВЁРНУТЫЙ»: анимация
    // сворачивания оставляла на узле inline-стили (height:0, opacity:.25,
    // overflow:hidden). Пока узел скрыт — их не видно, но при клике по
    // миниатюре они оживали: код раскрывался «со второго раза» и стоял
    // полупрозрачным, нечитаемым. Следы анимации стираем в момент укрытия.
    node.style.height = '';
    node.style.opacity = '';
    node.style.overflow = '';
    node.style.transition = '';
    // Скрытый узел с maxHeight:none опасен: при следующем показе браузер
    // сначала разложит его во всю высоту, и первый кадр анимации уедет.
    // Возвращаем телу обычное закрытое состояние заранее.
    const b = node.querySelector(':scope > .card-body');
    if (b && b._ct) { clearTimeout(b._ct); b._ct = null; }
    if (b) { b.style.transition = 'none'; b.style.maxHeight = ''; }
  };
  if (opts.instant) { put(); } else {
    // ПОЧЕМУ СВОРАЧИВАНИЕ ИНОГДА ДЁРГАЛОСЬ.
    // Оно было устроено ровно так, как когда-то разворачивание: анимация
    // shrinkClose двигала transform (scaleY), а transform вёрстку не меняет.
    // Карточка визуально сжималась, оставаясь в потоке во всю высоту, и
    // только через 170 мс исчезала разом — вся лента прыгала на её высоту за
    // один кадр. Заметно это было не всегда: если карточка низкая или лента
    // прокручена так, что прыжок за экраном, глаз ничего не ловит. Отсюда
    // «иногда минилаг». Разворачивание я так уже починил — делаю симметрично:
    // сжимаем НАСТОЯЩУЮ высоту, и лента едет вместе с карточкой.
    const h0 = node.getBoundingClientRect().height;
    node.style.overflow = 'hidden';
    node.style.transition = 'none';
    node.style.height = h0 + 'px';
    void node.offsetHeight;
    // Свёртка идёт заметно медленнее роста: раскрытие — ответ на клик, его
    // ждут, а свёртка происходит сама и должна читаться как мягкий уход.
    const dur = Math.max(240, Math.min(420, 200 + h0 * 0.4));
    node.classList.add('shrinking');
    node.style.transition = 'height ' + Math.round(dur) + 'ms cubic-bezier(.35,.55,.35,1)';
    node.style.height = '28px';        // примерно высота будущей миниатюры
    setTimeout(() => {
      node.classList.remove('shrinking');
      // вернуть карточке обычные размеры: она ещё пригодится при раскрытии
      node.style.transition = ''; node.style.height = ''; node.style.overflow = '';
      put();
    }, Math.round(dur));
  }
  thumb.addEventListener('click', () => {
    node.style.display = '';
    node.dataset.collapsed = '0';
    // AD: СВЁРНУТАЯ КАМЕРА/РАЗГОВОР РАЗВОРАЧИВАЮТСЯ ТЁМНЫМИ И НЕАКТИВНЫМИ:
    // сеанс давно завершён — кнопки мертвы, полистать диалог можно (как у камеры)
    if (node.classList.contains('cam-msg') && !S.camStream) node.classList.add('offline');
    if (node.classList.contains('voice-msg') && !VOICE.open) node.classList.add('offline');
    // ПРИЧИНА «текст не появляется»: карточку сворачивали в миниатюру, когда её
    // тело было закрыто (max-height:0). Разворачивая миниатюру, мы возвращали
    // карточку как есть — с закрытым телом, — и пользователь видел один
    // заголовок. Текст был на месте, но требовал второго клика. Теперь
    // разворачивание миниатюры сразу раскрывает и содержимое.
    // Тело раскрываем МГНОВЕННО и без собственной анимации: наружу идёт ровно
    // одно движение — рост самой карточки. Раньше здесь соревновались рост
    // блока, переход max-height и свечение шапки; теперь двигается одно.
    // ИСТИННАЯ ПРИЧИНА РЫВКА. Раньше карточка раскрывалась мгновенно
    // (setCardOpen instant), то есть её полная высота вставала в поток за ОДИН
    // кадр: строка 18px исчезала, на её место падал блок в сотни пикселей, и
    // всё содержимое ниже прыгало разом. Анимация growOpen этого не скрывала,
    // потому что transform (scale/opacity) вёрстку не двигает — он лишь
    // перерисовывает уже занятую коробку. Получалось: layout прыгнул сразу,
    // а карточка отдельно доигрывала сжатие. Это и читалось как «лаг».
    // Лечится только одним: анимировать НАСТОЯЩУЮ высоту от строки к блоку,
    // чтобы поток двигался вместе с карточкой.
    const h0 = holder.getBoundingClientRect().height;   // высота миниатюры
    holder.remove();
    setCardOpen(node, true, true);
    node.classList.remove('shrinking', 'unfolding');
    node.classList.add('grown');
    // plan-archive-target — одноразовая невидимая цель первого FLIP-полёта.
    // Раньше те же opts повторно использовались после ручного сворачивания:
    // новая миниатюра снова получала opacity:0 + pointer-events:none и потому
    // «второй раз не открывалась». Повторные миниатюры уже обычные и видимые.
    const reopenOpts = Object.assign({}, opts, { instant: false });
    reopenOpts.cls = String(reopenOpts.cls || '').replace(/\bplan-archive-target\b/g, '').trim();
    addFoldButton(node, reopenOpts);         // развернули — даём чем свернуть обратно
    // Карточка разрешения режима после ответа — приглушённая: решение принято,
    // тумблер показывает живой статус. Свернуть её по-прежнему можно.
    if (node.dataset.readonly === '1') {
      node.classList.add('mc-readonly');
      if (node._syncModeSwitch) node._syncModeSwitch();
    }
    const h1 = node.getBoundingClientRect().height;     // высота раскрытой карточки
    growHeight(node, h0, h1);
  });
  return thumb;
}

/* ПОЧЕМУ КАРТОЧКИ «НЕ ОТКРЫВАЛИСЬ». Они открывались — просто на доли
   секунды. Запись файла или короткий поиск отрабатывают за десятки
   миллисекунд, и tool_result сворачивал карточку в том же кадре, в котором
   её создал tool_start: браузер даже не успевал нарисовать раскрытое
   состояние. Ход мыслей вдобавок сворачивался с instant:true — то есть
   вообще без анимации.
   Поэтому у карточки теперь есть минимальный срок жизни: она обязана
   побыть раскрытой хотя бы CARD_MIN_MS, а потом свернуться уже на глазах.
   Мгновенные шаги превращаются в короткую заметную вспышку «открылось —
   закрылось», а долгие ведут себя как раньше. */
const CARD_MIN_MS = 620;

/* ПЛАН НАВЕРХУ, ПОКА ОН ВЫПОЛНЯЕТСЯ.
   План — единственная карточка, нужная всё время работы: по ней видно, где
   агент сейчас. В ленте она уезжает за экран через десяток строк вывода.
   Поэтому план «улетает» наверх, там сжимается в горизонтальную дорожку шагов
   и возвращается на своё место в ленте, когда всё выполнено. */

/* Один план на экране. Если пришёл новый — старый обязан уйти.
   БАГ: агент за прогон может составить план дважды (например, уточнил задачу).
   Второй dockPlan вешал вторую панель поверх первой, а undockPlan в конце
   знал только про последнюю — первая оставалась висеть наверху навсегда. */
function dropStrayDocks(keep) {
  $$('.plan-dock').forEach((d) => { if (d !== keep) d.remove(); });
}

/* Анимация плана — отдельная дорожка, но теперь она владеет event-gate ответа:
   пока план не дописан и не долетел, последующие SSE-события ждут. Таймеры
   храним у конкретного ui-прогона, чтобы Stop/смена плана не оставляли старый
   callback, который позднее поднимет неактуальную карточку наверх. */
const PLAN_FRAME_MS = 20;       // лёгкий кадровый цикл
const PLAN_CPS = 220;           // вступительный план быстрее разговорного ответа
const PLAN_ITEM_PAUSE = 300;    // короткая пауза между пунктами
const PLAN_LOOK_MS = 520;       // успеть охватить план перед красивым перелётом
const PLAN_FLY_MS = 640;        // совпадает с transition .plan-dock.fly в CSS
const PLAN_DONE_HOLD_MS = 2100; // зелёный итог не исчезает через треть секунды
const PLAN_FOLD_MS = 680;       // dock визуально превращается в архивную строку
const CURSOR_BREATHE_MS = 1050; // совпадает с cursorBreathe в CSS
const PLAN_STEP_MS = 950;       // минимальное время жизни одного шага на экране

/* Markdown-рендер пересобирает caret вместе с HTML ответа. Без общей фазы его
   CSS animation начиналась заново каждые 20 ms и фактически всегда стояла на
   первом кадре — именно поэтому «анимация курсора» визуально не работала.
   Отрицательная задержка возвращает новый DOM-узел в непрерывную фазу часов. */
function syncCursorPhase(node) {
  if (!node) return;
  node.style.animationDelay = '-' + (performance.now() % CURSOR_BREATHE_MS) + 'ms';
}

function clearPlanTimers(ui) {
  (ui.planTimers || []).forEach((t) => clearTimeout(t));
  ui.planTimers = [];
  ui.planPaintPending = null;
}

function planLater(ui, fn, ms) {
  const t = setTimeout(() => {
    ui.planTimers = (ui.planTimers || []).filter((x) => x !== t);
    fn();
  }, ms);
  (ui.planTimers || (ui.planTimers = [])).push(t);
  return t;
}

/* Пункт плана печатается быстрее разговорного ответа. Таймер может опоздать
   под нагрузкой, поэтому считаем символы по ПРОШЕДШЕМУ времени, а не «по одной
   букве за tick». На кадре меняются два узла; след ограничен 7 символами. */
function typePlanItem(ui, li, done) {
  const host = li && li.querySelector('.plan-copy');
  const chars = Array.from((li && li._planText) || '');
  if (!host) { if (done) done(); return; }
  host.textContent = '';
  const lead = document.createTextNode('');
  const trail = el('span', 'important-trail');
  const caret = el('i', 'important-caret');
  syncCursorPhase(caret);
  host.appendChild(lead);
  host.appendChild(trail);
  host.appendChild(caret);
  let at = 0;
  let carry = 0;
  let last = performance.now();
  const timer = setInterval(() => {
    if (!li.isConnected || ui.runId !== S.streamRun) {
      clearInterval(timer);
      ui.planTimers = (ui.planTimers || []).filter((x) => x !== timer);
      return;
    }
    const now = performance.now();
    // Покрываем обычные Safari stalls целиком; верхняя граница защищает лишь
    // от огромного скачка после возврата к давно скрытой вкладке.
    const elapsed = Math.max(1, Math.min(250, now - last));
    last = now;
    carry += PLAN_CPS * elapsed / 1000;
    const step = Math.floor(carry);
    if (step < 1) return;
    carry -= step;
    at = Math.min(chars.length, at + step);
    const cut = Math.max(0, at - 7);
    lead.nodeValue = chars.slice(0, cut).join('');
    trail.textContent = chars.slice(cut, at).join('');
    if (at < chars.length) return;
    clearInterval(timer);
    ui.planTimers = (ui.planTimers || []).filter((x) => x !== timer);
    caret.remove();
    // После набора обычный цвет возвращается без пересоздания всей строки.
    lead.nodeValue = chars.join('');
    trail.textContent = '';
    if (done) done();
  }, PLAN_FRAME_MS);
  (ui.planTimers || (ui.planTimers = [])).push(timer);
}

/* План — визуальный event-gate, а не независимая декорация. Пока он печатается
   и летит в dock, все следующие SSE-события лежат в очереди. Только callback
   завершённого перелёта выпускает текст/инструменты в исходном порядке. */
function beginPlanGate(ui) {
  if (ui.planGate) return;
  ui.planGate = true;
  ui.planDeferred = [];
  // Во время вступления на экране пишется только сам план. Статус не
  // удаляем и не прячем целиком: гасим ТОЛЬКО ПОДПИСЬ — курсор продолжает
  // дышать всё вступление. Если цепочка плана споткнётся, у пользователя
  // останется живой курсор, а не «исчез и завис».
  if (ui.statusEl && ui.statusEl.isConnected) {
    ui.planStatusDisplay = ui.statusEl.style.display || '';
    ui.statusEl.classList.add('gate-hold');
  }
  // СТРАЖ ЖИВУЧЕСТИ (X): показ плана — цепочка таймеров; оборвись одно
  // звено, gate держал бы ВСЕ события в очереди навсегда. Страж в любом
  // случае выпускает очередь — ответ не может «зависнуть» навечно.
  ui.planWatchdog = setTimeout(() => {
    if (ui.planGate) releasePlanGate(ui);
  }, 15000);
  ui.planIntroPromise = new Promise((resolve) => { ui.resolvePlanIntro = resolve; });
}

function releasePlanGate(ui) {
  if (!ui.planGate) return;
  ui.planGate = false;
  clearTimeout(ui.planWatchdog);
  if (ui.statusEl && ui.statusEl.isConnected) {
    ui.statusEl.style.display = ui.planStatusDisplay || '';
    ui.statusEl.classList.remove('gate-hold');
  }
  ui.planStatusDisplay = '';
  const queued = (ui.planDeferred || []).splice(0);
  const resolve = ui.resolvePlanIntro;
  ui.resolvePlanIntro = null;
  ui.planIntroPromise = null;
  queued.forEach((event) => dispatchStreamEvent(event, ui));
  if (resolve) resolve();
}

function cancelPlanGate(ui) {
  if (!ui || !ui.planGate) return;
  ui.planDeferred = [];
  releasePlanGate(ui);
}

async function waitForPlanGate(ui) {
  // Повторный план, выпущенный из первой очереди, может открыть новый gate.
  while (ui && ui.planIntroPromise) await ui.planIntroPromise;
}

function revealPlanItems(ui, at) {
  if (!ui.planCard || !ui.planCard.isConnected || ui.runId !== S.streamRun) {
    releasePlanGate(ui);
    return;
  }
  if (at >= ui.planItems.length) {
    planLater(ui, () => dockPlan(ui, () => releasePlanGate(ui)), PLAN_LOOK_MS);
    return;
  }
  const li = ui.planItems[at];
  if (!li.parentNode) ui.planList.appendChild(li);
  requestAnimationFrame(() => li.classList.remove('plan-pending'));
  /* BK: ПЛАН ЕДЕТ ПЛАВНО, КАК ТИХАЯ ПЕЧАТЬ. Вот она, разница режимов:
     каждый пункт плана раньше дёргал ленту мгновенным прыжком scrollTop
     к дну (скачок одним кадром), а в тихом режиме всё росло плавным
     догоном. Теперь план — тем же догоном: карточка растёт, лента
     плавно доедает остаток каждый кадр */
  chaseBottom(msgHost(), ui);
  typePlanItem(ui, li, () => {
    planLater(ui, () => revealPlanItems(ui, at + 1), PLAN_ITEM_PAUSE);
  });
}

function finishPlanItems(ui) {
  ui.planPaintQ = [];
  ui.planPaintPending = null;
  ui.planPainted = (ui.planItems || []).length;
  (ui.planItems || []).forEach((li) => {
    li.classList.remove('plan-pending', 'now');
    li.classList.add('done');
    const copy = li.querySelector('.plan-copy');
    if (copy) copy.textContent = li._planText || '';
    if (ui.planList && !li.parentNode) ui.planList.appendChild(li);
  });
}

/* Прогресс и задачи имеют одну и ту же сетку. Один непрерывный fill не мог
   показать, к какому пункту относится мерцание: сегменты — ровно по одному
   на пункт, поэтому активный блик всегда находится над своей подписью. */
function paintDockStep(ui) {
  const dock = ui.planDock;
  if (!dock) return;
  const total = ui.planItems.length || 1;
  const n = Math.max(1, Math.min(ui.planPainted || ui.planStep || 1, total));
  const label = dock.querySelector('.pd-step');
  if (label) label.textContent = 'шаг ' + n + ' из ' + total;
  $$('.pd-seg', dock).forEach((seg, i) => {
    seg.classList.toggle('done', i < n - 1);
    seg.classList.toggle('now', i === n - 1);
  });
  $$('.pd-s', dock).forEach((st, i) => {
    st.classList.toggle('done', i < n - 1);
    st.classList.toggle('now', i === n - 1);
  });
}

/* Шаги плана приезжают фактами от агента — иногда пачкой: параллельные
   инструменты заканчиваются почти одновременно, и события plan_step
   применяются за один кадр. visually это «план выполнился мгновенно»,
   хотя работа шла по-настоящему. Красим шаги с минимальным ритмом
   PLAN_STEP_MS: даже мгновенная работа видна как последовательный
   прогресс. Логическое состояние (ui.planStep) обновляется сразу, а
   очередь покраски сбрасывается только в finishPlanItems. */
function paintPlanStepNow(ui, n) {
  ui.planPainted = Math.max(ui.planPainted || 0, n);
  ui.planItems.forEach((li, i) => {
    li.classList.toggle('done', i < n - 1);
    li.classList.toggle('now', i === n - 1);
  });
  if (ui.planDock) paintDockStep(ui);
}

function paintPlanStepSoon(ui) {
  if (ui.planPaintPending) return;
  ui.planPaintPending = planLater(ui, () => {
    ui.planPaintPending = null;
    const queue = ui.planPaintQ || [];
    const n = queue.shift();
    if (!n) return;
    paintPlanStepNow(ui, n);
    if ((ui.planPaintQ || []).length) paintPlanStepSoon(ui);
  }, (ui.planPainted || 0) ? PLAN_STEP_MS : 0);
}

function dockPlan(ui, arrived) {
  const card = ui.planCard;
  // Фоновый ответ из уже закрытого диалога не владеет общей верхней зоной.
  if (!card || !card.isConnected || ui.planDock || ui.runId !== S.streamRun) {
    if (arrived) arrived();
    return;
  }

  // Верхняя панель — отдельный flying visual. Исходная карточка остаётся в
  // normal flow лишь на время полёта и одновременно мягко схлопывается: ни
  // фиксированной распорки, ни пустого места после неё в ленте нет.
  const box = card.getBoundingClientRect();

  const n = ui.planItems.length;
  const dock = el('div', 'plan-dock');
  // Владелец записан на самом DOM-узле. Даже если ссылка ui.planDock будет
  // потеряна при смене контейнера камеры, завершение найдёт и уберёт СВОЙ dock,
  // не задевая план более нового ответа.
  dock.dataset.runId = String(ui.runId);
  dock.dataset.chatId = ui.chatId || S.chatId || '';   // Z: план принадлежит диалогу
  dock.innerHTML =
    '<div class="pd-top">' +
      '<span class="pd-ico">☰</span>' +
      '<span class="pd-t">План выполняется</span>' +
      '<span class="pd-step">шаг 1 из ' + n + '</span>' +
    '</div>' +
    '<div class="pd-bar">' + Array.from({ length: n }, () => '<i class="pd-seg"></i>').join('') + '</div>' +
    '<div class="pd-steps"></div>' +
    '<ol class="pd-full"></ol>';

  // Шаги — кружки с номерами, равноудалённо по горизонтали, с короткой
  // подписью под каждым. Так виден ВЕСЬ план целиком и место в нём.
  const row = dock.querySelector('.pd-steps');
  ui.planItems.forEach((li, i) => {
    const full = (li.textContent || '').replace(/^\d+/, '').trim();
    const st = el('div', 'pd-s');
    st.innerHTML = '<i class="pd-dot">' + (i + 1) + '</i>' +
                   '<span class="pd-cap">' + esc(shortStep(full)) + '</span>';
    st.title = full;                       // полная формулировка — по наведению
    row.appendChild(st);
    dock.querySelector('.pd-full').appendChild(el('li', '', esc(full)));
  });
  const dockTop = dock.querySelector('.pd-top');
  dockTop.title = 'Открыть полный план';
  dockTop.setAttribute('role', 'button');
  dockTop.setAttribute('tabindex', '0');
  const toggleFull = () => dock.classList.toggle('expanded');
  dockTop.addEventListener('click', toggleFull);
  dockTop.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggleFull(); }
  });

  const holder = $('#dockZone') || stream().parentNode;
  holder.appendChild(dock);
  dropStrayDocks(dock);
  ui.planDock = dock;
  // plan_step часто успевает прийти за время вступительной анимации. Dock не
  // начинает заново с первого шага, а сразу рисует сохранённый факт.
  paintDockStep(ui);

  // ПОЛЁТ «ОБЛАЧКОМ»: панель стартует там, где карточка стоит в ленте, и
  // плавно уплывает на своё место наверху, попутно сжимаясь. Раньше она
  // просто возникала сверху — движение было незаметно.
  const to = dock.getBoundingClientRect();
  const dx = (box.left + box.width / 2) - (to.left + to.width / 2);
  const dy = box.top - to.top;
  const sx = Math.min(1.25, box.width / Math.max(1, to.width));
  dock.style.transformOrigin = 'top center';
  dock.style.transform = 'translate(' + dx + 'px,' + dy + 'px) scale(' + sx + ')';
  dock.style.opacity = '0';
  card.style.height = box.height + 'px';
  card.style.overflow = 'hidden';
  card.style.transition = 'height .54s cubic-bezier(.16,1,.3,1),margin .54s ease,opacity .38s ease,border-width .54s ease';
  requestAnimationFrame(() => {
    dock.classList.add('fly');            // .fly задаёт длинный мягкий переход
    dock.style.transform = 'none';
    dock.style.opacity = '1';
    card.style.height = '0px';
    card.style.marginTop = '0';
    card.style.marginBottom = '0';
    card.style.borderWidth = '0';
    card.style.opacity = '0';
  });
  planLater(ui, () => {
    if (card.isConnected && ui.planDock === dock) card.remove();
  }, PLAN_FLY_MS + 20);
  // Только фактическое окончание перелёта открывает gate для ответа. Таймер
  // принадлежит ui и не может выпустить события после Stop/смены диалога.
  planLater(ui, () => {
    if (dock.isConnected && ui.planDock === dock) dock.classList.add('live');
    if (arrived) arrived();
  }, PLAN_FLY_MS + 30);
}

/* Короткая подпись под кружком: первые два-три слова шага. */
function shortStep(t) {
  const words = String(t).split(/\s+/).filter(Boolean);
  let out = words.slice(0, 2).join(' ');
  if (out.length > 18) out = out.slice(0, 17) + '…';
  return out || '—';
}

/* Завершённый план не уничтожается: зелёный dock подтверждает результат,
   затем в самом сообщении остаётся компактная открываемая вкладка со всеми
   исходными формулировками. */
function archiveCompletedPlan(ui) {
  // Обычный ответ приходит сюда через общий done, но не имеет plan event.
  // Пустая «План выполнен · 0 шагов» была следствием отсутствия этого guard.
  if (!ui || !ui.agentMode || !(ui.planItems || []).length || !ui.node || !ui.node.body) return null;
  if (ui.planArchive) return ui.planArchiveThumb || null;
  const card = makeCard('☰', 'План выполнен', 'plan-card plan-complete', true);
  const list = el('ul', 'plan-list');
  (ui.planItems || []).forEach((item, index) => {
    const text = item._planText || (item.textContent || '').replace(/^\d+/, '').trim();
    list.appendChild(el('li', 'done', '<span class="plan-num">' + (index + 1) +
      '</span><span class="plan-copy">' + esc(text) + '</span>'));
  });
  card.inner.appendChild(list);
  const before = ui.mdEl && ui.mdEl.parentNode === ui.node.body ? ui.mdEl : ui.node.body.firstChild;
  ui.node.body.insertBefore(card, before || null);
  ui.planArchive = card;
  const thumb = collapseToThumb(card, {
    cls: 'th-plan plan-archive-target', icon: '☰', title: 'План выполнен',
    sub: (ui.planItems || []).length + ' шаг(ов)', tag: 'открыть', instant: true,
  });
  ui.planArchiveThumb = thumb;
  return thumb;
}

function undockPlan(ui) {
  if (!ui || ui.planFinished) return;
  // done есть у каждого ответа; normal chat не должен получать пустой архив.
  if (!ui.agentMode || !(ui.planItems || []).length) {
    ui.planFinished = true;
    releasePlanGate(ui);
    return;
  }
  ui.planFinished = true;
  clearPlanTimers(ui);
  finishPlanItems(ui);
  releasePlanGate(ui);

  const owned = $$('.plan-dock').filter((d) => d.dataset.runId === String(ui.runId));
  const dock = ui.planDock || owned[owned.length - 1] || null;
  const card = ui.planCard;
  ui.planDock = null;
  const docks = owned.length ? owned : (dock ? [dock] : []);
  const primary = dock || docks[docks.length - 1] || null;

  if (card && card.isConnected) card.remove();
  docks.forEach((ownDock) => {
    ownDock.classList.remove('live', 'expanded');
    ownDock.classList.add('done');
    const title = ownDock.querySelector('.pd-t');
    if (title) title.textContent = 'План выполнен';
    const step = ownDock.querySelector('.pd-step');
    if (step) step.textContent = 'готово';
    [...$$('.pd-seg', ownDock), ...$$('.pd-s', ownDock)].forEach((item) => {
      item.classList.remove('now'); item.classList.add('done');
    });
  });

  // Если dock по технической причине уже потерян, архив всё равно не пропадает.
  if (!primary) {
    const instantThumb = archiveCompletedPlan(ui);
    if (instantThumb) instantThumb.classList.add('plan-archive-reveal');
    return;
  }
  docks.filter((item) => item !== primary).forEach((item) => item.remove());

  // Сначала зелёный результат спокойно остаётся наверху. Затем создаём
  // конечную архивную строку, измеряем её фактическое положение и FLIP-полётом
  // превращаем dock именно в неё — не в произвольную точку экрана.
  setTimeout(() => {
    if (!primary.isConnected) {
      const fallbackThumb = archiveCompletedPlan(ui);
      if (fallbackThumb) fallbackThumb.classList.add('plan-archive-reveal');
      return;
    }
    const thumb = archiveCompletedPlan(ui);
    if (!thumb || !thumb.isConnected) { primary.remove(); return; }
    const from = primary.getBoundingClientRect();
    const to = thumb.getBoundingClientRect();
    const dx = (to.left + to.width / 2) - (from.left + from.width / 2);
    const dy = (to.top + to.height / 2) - (from.top + from.height / 2);
    const sx = Math.max(.12, Math.min(1, to.width / Math.max(1, from.width)));
    const sy = Math.max(.12, Math.min(1, to.height / Math.max(1, from.height)));
    primary.classList.add('plan-folding');
    requestAnimationFrame(() => {
      primary.style.transform = 'translate3d(' + dx + 'px,' + dy + 'px,0) scale(' + sx + ',' + sy + ')';
      primary.style.opacity = '0';
      thumb.classList.add('plan-archive-reveal');
    });
    setTimeout(() => primary.remove(), PLAN_FOLD_MS + 80);
  }, PLAN_DONE_HOLD_MS);
}

function discardPlan(ui) {
  if (!ui) return;
  clearPlanTimers(ui);
  cancelPlanGate(ui);
  if (ui.planCard && ui.planCard.isConnected) ui.planCard.remove();
  $$('.plan-dock').filter((d) => d.dataset.runId === String(ui.runId)).forEach((d) => d.remove());
  ui.planDock = null;
}

function markBorn(card) {
  if (!card) return;
  card.dataset.born = String(performance.now());
  // Класс ожидания уже стоит ДО insertion, поэтому самый первый paint содержит
  // градиент. rAF здесь ничего не запускает — только доказывает, что браузер
  // действительно показал хотя бы один кадр до быстрого tool_result.
  if (card.classList.contains('tool-wait')) {
    requestAnimationFrame(() => {
      if (card.isConnected && !card.dataset.waitPainted) {
        card.dataset.waitPainted = String(performance.now());
      }
    });
  }
}



function cancelFoldSoon(card) {
  if (!card) return;
  if (card._foldT) { clearTimeout(card._foldT); card._foldT = null; }
  card.dataset.folding = '';
}

function collapseSoon(card, opts) {
  if (!card || !card.isConnected) return;
  if (card.dataset.folding === '1') return;
  card.dataset.folding = '1';
  // осторожно: born может быть ровно 0 (первые миллисекунды жизни страницы),
  // а 0 в JS ложный — короткая запись `|| performance.now()` тут молча
  // превращала «родилась в самом начале» в «родилась только что» и добавляла
  // лишнюю задержку даже долгим шагам
  const bornRaw = parseFloat(card.dataset.born);
  const born = Number.isFinite(bornRaw) ? bornRaw : performance.now();
  const left = Math.max(0, CARD_MIN_MS - (performance.now() - born));
  card._foldT = setTimeout(() => {
    card._foldT = null;
    if (card.isConnected) collapseToThumb(card, opts);
  }, left);
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
    '.file-chip,.img-out,.msg-actions,.ver-switch,.ql-row,.qt-row,.ag-rows,.qt-kids');
  // ИСТИННЫЙ КОРЕНЬ «группа закрывается при клике по инструменту»: этот
  // слушатель висит на ВСЕЙ карточке и сворачивает её по клику в любом
  // месте. Строки-инструменты (.ql-row агента, .qt-row кухни) — сами по
  // себе интерактив: их клик разворачивает деталь, а не закрывает группу.
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
/* Синонимы типов. Модель — не парсер: она пишет «radio», «select», «choice»,
   «плитки», «checkbox». Раньше любая такая строка не распознавалась, панель
   получалась пустой и УДАЛЯЛАСЬ — пользователь видел «выбери скорость», а
   выбора не было (та самая змейка). Приводим синонимы к своим типам. */
const UI_ALIAS = {
  плитки: 'tiles', выбор: 'tiles', radio: 'tiles', select: 'tiles',
  choice: 'tiles', options: 'tiles', option: 'tiles', buttons: 'tiles',
  ползунок: 'slider', range: 'slider',
  число: 'number', счётчик: 'number', counter: 'number', spinner: 'number',
  переключатель: 'toggle', checkbox: 'toggle', switch: 'toggle', флаг: 'toggle',
  строка: 'text', input: 'text', поле: 'text',
  текст: 'area', textarea: 'area',
  кнопка: 'button',
  оценка: 'rate', stars: 'rate', звёзды: 'rate',
  несколько: 'multi', multiselect: 'multi', checklist: 'multi',
  порядок: 'rank', sort: 'rank', приоритет: 'rank',
  дата: 'date', цвет: 'color',
};

/* Привести вольную строку к канону: снять маркеры списка и нумерацию,
   развернуть синоним типа, починить диапазон «1-10» и «от 1 до 10»,
   заменить перечисление запятыми на «|». */
function normUiLine(raw) {
  let ln = String(raw).trim();
  if (!ln) return '';
  const W = 'A-Za-z\u0400-\u04FF0-9_';                 // «словесные» символы, включая кириллицу
  ln = ln.replace(/^[-*\u2022\u00b7]\s+/, '').replace(/^\d+[.)]\s+/, '');
  ln = ln.replace(new RegExp('^([' + W + '][' + W + '-]*)\\s*:\\s+(?=\\S)', 'i'), '$1 ');
  const head = ln.match(new RegExp('^([' + W + '-]+)'));
  if (head) {
    const canon = UI_ALIAS[head[1].toLowerCase()];
    if (canon) ln = canon + ln.slice(head[1].length);
  }
  // диапазоны: «от 1 до 10», «1-10» -> «1..10»  (\b здесь бесполезен: он не видит кириллицу)
  ln = ln.replace(/(^|\s)от\s+(-?\d+(?:[.,]\d+)?)\s+до\s+(-?\d+(?:[.,]\d+)?)/i, '$1$2..$3');
  ln = ln.replace(/(^|\s)(-?\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)(?=\s|$|=)/, '$1$2..$3');
  ln = ln.replace(/step(?=\d)/i, 'step ');
  return ln;
}

/* Разбор строки выбора: «tiles Метка: A | B | C» и «tiles Метка | A | B».

   ПОЧЕМУ ЗАГОЛОВОК УЕЗЖАЛ В ПЕРВЫЙ ВАРИАНТ. Шаблон требовал двоеточие между
   меткой и вариантами. Модель написала «tiles Что делать? | Сделай постер |
   ...» — вопросительный знак вместо двоеточия, — строка не совпала ни с одним
   шаблоном и доехала до последнего рубежа. А тот делит по «|» ВСЮ строку, не
   зная, что первое слово — это название типа. Получилась плитка с подписью
   «tiles Что делать?» и панель без заголовка.
   Двоеточие — не то, на чём стоит держаться. Если варианты разделены «|», то
   первый кусок и есть метка, каким бы знаком он ни кончался. */
/* AA: сломанная строка выбора превращается в вопрос да/нет — почистить
   метку, чтобы она стала нормальным вопросом («Скачать? да» → «Скачать?»,
   голое «Да» → «Подтвердить?»). */
function confirmLabel(label) {
  let q = String(label || '').replace(/\s*[?:!]+\s*$/, '').trim();
  // NB: \b в JS не знает кириллицы — границу слова строим сами
  q = q.replace(/[\s,.!:;?]+(?:да|нет)\s*$/i, '').trim();
  if (/^(?:да|нет)$/i.test(q)) q = '';
  q = q || 'Подтвердить';
  return /[?…:]$/.test(q) ? q : q + '?';
}

function matchChoice(ln, type) {
  const head = new RegExp('^' + type + '\\s+(.+)$', 'i');
  const m = ln.match(head);
  if (!m) return null;
  let rest = m[1];
  let label = '';
  const colon = rest.indexOf(':');
  const bar = rest.indexOf('|');
  if (colon >= 0 && (bar < 0 || colon < bar)) {
    label = rest.slice(0, colon).trim();
    rest = rest.slice(colon + 1);
  } else if (bar >= 0) {
    // двоеточия нет: меткой служит всё до первой черты
    label = rest.slice(0, bar).trim();
    rest = rest.slice(bar + 1);
  } else {
    return null;                       // вариантов нет — это не выбор
  }
  const opts = rest.split('|').map((x) => x.trim()).filter(Boolean);
  if (!opts.length) return null;
  return [ln, label || 'Выбери', opts];
}

/* Название типа, случайно оставшееся в начале строки. Нужно последнему
   рубежу: он делит строку по «|» вслепую и не должен принять слово «tiles»
   за часть первого варианта. */
const UI_TYPE_WORD = /^(confirm|tiles|multi|rank|slider|number|rate|toggle|text|area|date|color|button)\s+/i;

function parseUiSpec(src) {
  const items = [];
  String(src || '').split('\n').forEach((raw) => {
    const ln = normUiLine(raw);
    if (!ln) return;
    let m;
    // slider Метка 0..100 [step 5] [unit ₽] = 50
    if ((m = ln.match(/^slider\s+(.+?)\s+(-?\d+(?:\.\d+)?)\.\.(-?\d+(?:\.\d+)?)(?:\s+step\s+(\d+(?:\.\d+)?))?(?:\s+unit\s+(\S+))?(?:\s*=\s*(-?\d+(?:\.\d+)?))?$/i))) {
      const min = parseFloat(m[2]), max = parseFloat(m[3]);
      items.push({ t: 'slider', label: m[1], min, max,
                   step: m[4] ? parseFloat(m[4]) : ((max - min) % 1 ? 0.1 : 1),
                   unit: m[5] || '',
                   val: m[6] != null ? parseFloat(m[6]) : min });
    // number Метка 1..20 [step 1] [unit шт] = 3  — счётчик с кнопками ± 
    // Диапазон необязателен: «number Количество = 3» — обычный счётчик.
    // Раньше min..max требовался жёстко, строка не совпадала и панель пропадала.
    } else if ((m = ln.match(/^number\s+(.+?)(?:\s+(-?\d+(?:\.\d+)?)\.\.(-?\d+(?:\.\d+)?))?(?:\s+step\s+(\d+(?:\.\d+)?))?(?:\s+unit\s+(\S+))?(?:\s*=\s*(-?\d+(?:\.\d+)?))?$/i))) {
      const min = m[2] != null ? parseFloat(m[2]) : 0;
      const max = m[3] != null ? parseFloat(m[3]) : 999999;
      items.push({ t: 'number', label: m[1], min, max,
                   step: m[4] ? parseFloat(m[4]) : 1,
                   unit: m[5] || '',
                   val: m[6] != null ? parseFloat(m[6]) : min });
    } else if ((m = ln.match(/^toggle\s+(.+?)(?:\s*=\s*(on|off|да|нет|true|false))?$/i))) {
      items.push({ t: 'toggle', label: m[1],
                   val: /^(on|да|true)$/i.test(m[2] || '') });
    // confirm Подпись — вопрос да/нет: сноска с зелёной «Да» и красной «Нет».
    // Самый частый вопрос интерфейса: когда хватает да/нет — всегда он.
    } else if ((m = ln.match(/^confirm\s+(.+?)\s*:*$/i))) {
      items.push({ t: 'confirm', label: m[1].trim(), val: null });
    } else if ((m = matchChoice(ln, 'tiles'))) {
      // AA: ОДНА ПЛИТКА — СЛОМАННЫЙ ВЫБОР. Модель, желая спросить да/нет,
      // писала «tiles Да: нет» — и пользователь получал панель с названием
      // «Да» и единственной кнопкой «нет». Такой выбор превращается в
      // нормальный confirm: вопрос + две кнопки «Да»/«Нет».
      if (!m[2] || m[2].length < 2) {
        items.push({ t: 'confirm', label: confirmLabel(m[1]), val: null });
      } else {
        items.push({ t: 'tiles', label: m[1], opts: m[2], val: null });
      }
    // text Метка [= подсказка] — свободный ответ, когда варианты не перечислить
    } else if ((m = ln.match(/^text\s+(.+?)(?:\s*=\s*(.*))?$/i))) {
      items.push({ t: 'text', label: m[1], hint: (m[2] || '').trim(), val: '' });
    // rank Метка: A | B | C — расставить по важности (порядок и есть ответ)
    } else if ((m = matchChoice(ln, 'rank'))) {
      items.push({ t: 'rank', label: m[1], opts: m[2], val: null });
    // multi Метка: A | B | C — выбрать НЕСКОЛЬКО, а не одно
    } else if ((m = matchChoice(ln, 'multi'))) {
      items.push({ t: 'multi', label: m[1], opts: m[2], val: [] });
    // rate Метка [1..5] — оценка звёздами
    } else if ((m = ln.match(/^rate\s+(.+?)(?:\s+(\d+)\.\.(\d+))?(?:\s*=\s*(\d+))?$/i))) {
      items.push({ t: 'rate', label: m[1], max: m[3] ? parseInt(m[3], 10) : 5,
                   val: m[4] ? parseInt(m[4], 10) : 0 });
    // date Метка [= 2026-08-18] — дата
    } else if ((m = ln.match(/^date\s+(.+?)(?:\s*=\s*(\S+))?$/i))) {
      items.push({ t: 'date', label: m[1], val: m[2] || '' });
    // color Метка [= #00c8f0]
    } else if ((m = ln.match(/^color\s+(.+?)(?:\s*=\s*(#[0-9a-f]{3,8}))?$/i))) {
      items.push({ t: 'color', label: m[1], val: m[2] || '#00c8f0' });
    // area Метка [= подсказка] — длинный ответ в несколько строк
    } else if ((m = ln.match(/^area\s+(.+?)(?:\s*=\s*(.*))?$/i))) {
      items.push({ t: 'area', label: m[1], hint: (m[2] || '').trim(), val: '' });
    } else if ((m = ln.match(/^button\s+(.+)$/i))) {
      items.push({ t: 'button', label: m[1] });

    // ПОСЛЕДНИЙ РУБЕЖ. Строка не легла ни в один шаблон, но в ней есть
    // «Метка: A | B | C» или «A | B» — значит, выбор человеку предлагают,
    // и молча выбрасывать его нельзя. Лучше показать плитки не того оттенка,
    // чем не показать ничего: пустая панель удаляется, и пользователь остаётся
    // с текстом «выбери скорость» без единой кнопки.
    } else if (/\|/.test(ln)) {
      const bare = ln.replace(UI_TYPE_WORD, '');   // «tiles Что делать?» -> «Что делать?»
      // Метку отделяем тем же правилом, что и в matchChoice: двоеточие, если
      // оно раньше первой черты, иначе — всё до первой черты. Иначе вопрос
      // «Что делать? | А | Б» превращался в лишнюю плитку «Что делать?».
      const colon = bare.indexOf(':'), bar = bare.indexOf('|');
      let label = '', rest = bare;
      if (colon >= 0 && (bar < 0 || colon < bar)) {
        label = bare.slice(0, colon).trim(); rest = bare.slice(colon + 1);
      } else if (bar >= 0) {
        label = bare.slice(0, bar).trim(); rest = bare.slice(bar + 1);
      }
      const opts = rest.split('|').map((x) => x.trim()).filter(Boolean);
      if (opts.length > 1) items.push({ t: 'tiles', label: label || 'Выбери', opts, val: null });
      // единственный вариант после черты — сломанное да/нет: превращаем
      // в confirm, чтобы у человека всегда были обе кнопки
      else if (opts.length === 1) items.push({ t: 'confirm', label: confirmLabel(label), val: null });
    }
  });

  // ЛИШНЯЯ КНОПКА «СГЕНЕРИРОВАТЬ».
  // Панель сама ставит кнопку отправки там, где она нужна. Когда модель сверх
  // этого дописывает свою кнопку («Сгенерировать», «Поехали»), получается два
  // способа подтвердить выбор — причём её кнопка отправляет одну свою подпись,
  // теряя выставленные значения. Из промпта я эту привычку убрал, но полагаться
  // на послушание модели нельзя: подтверждение — дело интерфейса, поэтому
  // кнопку-действие оставляем ТОЛЬКО если ничего другого в панели нет
  // (тогда это осмысленная кнопка «сделай вот это»).
  const real = items.filter((x) => x.t !== 'button');
  return real.length ? real : items;
}

function hasMeaningfulUiItems(items) {
  // Свободный text/area дублирует основной composer. В сочетании с настоящим
  // выбором он допустим, но сам по себе отдельной панели не заслуживает.
  return (items || []).some((item) => item.t !== 'text' && item.t !== 'area');
}

function choiceKey(text) {
  return String(text || '').toLocaleLowerCase('ru-RU')
    .replace(/[^a-zа-яё0-9]+/gi, ' ').trim();
}

/* Модель может напечатать те же варианты обычным списком, а deterministic gate
   добавить плитки ниже. После появления controls убираем только зеркальную
   копию списка — вопрос и контекст остаются. Сравнение закрытое: минимум два
   пункта должны попарно совпасть с labels плиток, чужой список не трогаем. */
function stripMirroredChoiceList(box, items) {
  const tiles = (items || []).find((item) => item.t === 'tiles');
  if (!box || !tiles || !tiles.opts || tiles.opts.length < 2) return;
  const wanted = tiles.opts.map(choiceKey);
  let node = box.previousElementSibling;
  for (let distance = 0; node && distance < 4; distance += 1) {
    const prev = node.previousElementSibling;
    if (node.tagName === 'UL' || node.tagName === 'OL') {
      const listed = $$('li', node).map((li) => choiceKey(li.textContent));
      const same = listed.length >= 2 && listed.every((label) =>
        wanted.some((option) => option === label || option.startsWith(label + ' ') ||
          label.startsWith(option + ' ')));
      if (same) node.remove();
      return;
    }
    node = prev;
  }
}

/* ================== BG: ВСТРОЕННЫЙ МАТЕМАТИЧЕСКИЙ РЕЖИМ ==================
   Живые графики и чертежи прямо в диалоге: ```plot (функции 2D и
   поверхности 3D) и ```geo (геометрия). Панель панорамируется
   перетаскиванием, масштабируется колесом и кнопками, 3D вращается.
   Выражения парсятся локально (без eval): + - * / ^, скобки, sin cos
   tan asin acos atan sqrt abs exp log ln, pi, e, переменные x и y. */

/* Безопасный разбор математического выражения -> RPN -> функция */
function mathParseExpr(src) {
  const FUN = { sin: Math.sin, cos: Math.cos, tan: Math.tan, asin: Math.asin,
    acos: Math.acos, atan: Math.atan, sqrt: Math.sqrt, abs: Math.abs,
    exp: Math.exp, log: Math.log10, ln: Math.log, sinh: Math.sinh,
    cosh: Math.cosh, tanh: Math.tanh, floor: Math.floor, round: Math.round,
    sign: Math.sign };
  /* BK: модель любит типографские знаки — · × − ÷ π √ и ** для степени.
     Раньше они молча выбрасывались из формулы: график «не строился» или
     врал. Нормализуем всё в обычную запись ДО разбора */
  const text = String(src || '')
    .replace(/\s+/g, '')
    .replace(/[,;](?=\d{3}\b)/g, '')
    .replace(/[·×]/g, '*')
    .replace(/[−–—]/g, '-')
    .replace(/÷/g, '/')
    .replace(/\*\*/g, '^')
    .replace(/π/g, 'pi')
    .replace(/√/g, 'sqrt');
  const toks = [];
  const re = /\d+\.?\d*(?:e[+-]?\d+)?|[a-zA-Z]+|[()+\-*/^,]/g;
  let m;
  while ((m = re.exec(text))) toks.push(m[0]);
  const out = [], ops = [];
  const prec = (o) => (o === '+' || o === '-') ? 1 : (o === '*' || o === '/') ? 2 :
    (o === 'u-') ? 2.2 : (o === '^') ? 3 : 0;
  const right = (o) => o === '^';
  let prev = null;
  for (const tk of toks) {
    if (/^\d/.test(tk)) { out.push(parseFloat(tk)); prev = 'n'; continue; }
    if (/^[a-zA-Z]+$/.test(tk)) {
      const low = tk.toLowerCase();
      if (low === 'pi') { out.push(Math.PI); prev = 'n'; continue; }
      if (low === 'e') { out.push(Math.E); prev = 'n'; continue; }
      if (FUN[low]) { ops.push(low + '('); prev = 'f'; continue; }
      if (tk === 'x' || tk === 'y') { out.push(tk); prev = 'n'; continue; }
      /* BK: одиночная буква (t, n, u…) — это параметр: считаем её x,
         иначе «sin(t)» валил график целиком */
      if (tk.length === 1) { out.push('x'); prev = 'n'; continue; }
      throw new Error('неизвестное имя: ' + tk);
    }
    if (tk === '(') { if (prev === 'n') ops.push('*'); ops.push('('); prev = '('; continue; }
    if (tk === ')') {
      while (ops.length && ops[ops.length - 1] !== '(') out.push(ops.pop());
      if (!ops.length) throw new Error('скобки');
      ops.pop();
      if (ops.length && FUN[ops[ops.length - 1].slice(0, -1)]) out.push(ops.pop());
      prev = 'n'; continue;
    }
    if (tk === ',') {
      while (ops.length && ops[ops.length - 1] !== '(') out.push(ops.pop());
      prev = ','; continue;
    }
    if ('+-*/^'.includes(tk)) {
      if (tk === '-' && (prev === null || prev === 'o' || prev === '(' || prev === ',')) {
        ops.push('u-'); prev = 'n'; continue;
      }
      while (ops.length) {
        const top = ops[ops.length - 1];
        if (top === '(' || !prec(top)) break;
        if (prec(top) > prec(tk) || (prec(top) === prec(tk) && !right(tk))) out.push(ops.pop());
        else break;
      }
      ops.push(tk); prev = 'o'; continue;
    }
    throw new Error('символ: ' + tk);
  }
  while (ops.length) {
    const o = ops.pop();
    if (o === '(') throw new Error('скобки');
    out.push(o);
  }
  return out;
}

function mathCompile(src) {
  const rpn = mathParseExpr(src);
  return function (x, y) {
    const st = [];
    for (const tk of rpn) {
      if (typeof tk === 'number') { st.push(tk); continue; }
      if (tk === 'x') { st.push(x); continue; }
      if (tk === 'y') { st.push(y); continue; }
      if (tk === 'u-') { st.push(-st.pop()); continue; }
      if (typeof tk === 'string' && '+-*/^'.includes(tk) && tk.length === 1) {
        const b = st.pop(), a = st.pop();
        st.push(tk === '+' ? a + b : tk === '-' ? a - b : tk === '*' ? a * b :
          tk === '/' ? a / b : Math.pow(a, b));
        continue;
      }
      const arg = st.pop();
      const fn = { sin: Math.sin, cos: Math.cos, tan: Math.tan, asin: Math.asin,
        acos: Math.acos, atan: Math.atan, sqrt: Math.sqrt, abs: Math.abs,
        exp: Math.exp, log: Math.log10, ln: Math.log, sinh: Math.sinh,
        cosh: Math.cosh, tanh: Math.tanh, floor: Math.floor, round: Math.round,
        sign: Math.sign }[tk.slice(0, -1)];
      if (!fn) throw new Error('оператор: ' + tk);
      st.push(fn(arg));
    }
    const v = st.pop();
    if (st.length || typeof v !== 'number' || !isFinite(v)) return NaN;
    return v;
  };
}

function plotFmt(v) {
  if (Math.abs(v) >= 1000 || (Math.abs(v) < 0.01 && v !== 0)) return v.toExponential(1);
  return String(Math.round(v * 100) / 100);
}

/* панель графика/чертежа: canvas + тулбар; один раз оживляется */
/* BK: СПОКОЙНЫЕ ТАБЛИЦЫ. Пока ответ печатается, авто-раскладка заново
   делит ширину между колонками на каждом такте печати — таблица «резко
   масштабируется» в обе стороны. Лечение: как только у таблицы появилась
   первая строка, фиксируем ЕСТЕСТВЕННЫЕ пропорции колонок (colgroup в
   процентах от реальных ширин контента) — масштаб не «ровный казённый»,
   а ровно такой, как просится у контента, и больше не прыгает: новые
   строки наливаются в стабильную сетку */
function fixTables(root) {
  $$('table', root).forEach((t) => {
    if (t.dataset.cols === '1' || !t.isConnected) return;
    const ths = $$('thead th', t);
    if (!ths.length) return;
    const row = $$('tbody tr:first-child td', t);
    if (!row.length) return;            // header-only ещё не честен в пропорциях
    const ws = ths.map((th, i) => Math.max(th.offsetWidth, row[i] ? row[i].offsetWidth : 0) + 1);
    const total = ws.reduce((a, b) => a + b, 0) || 1;
    const cg = el('colgroup');
    ws.forEach((w) => {
      const c = el('col');
      c.style.width = (w / total * 100).toFixed(2) + '%';
      cg.appendChild(c);
    });
    t.insertBefore(cg, t.firstChild);
    t.style.tableLayout = 'fixed';
    t.dataset.cols = '1';
  });
}

/* BJ: ГРАФИК-ИНТЕРПРЕТАТОР ПОНИМАЕТ ЛЮБУЮ разумную запись. Модель иногда
   пишет plot-спеку с одинарными кавычками, голыми ключами, висячими
   запятыми, комментариями или просто парами «f=sin(x)» — раньше это
   валилось с «Не разобрать JSON» и график пропадал. Теперь: строгий
   JSON → мягкий JSON (одинарные кавычки, голые ключи, хвостовые
   запятые, // комментарии) → пары ключ=значение → голая формула.
   Плюс синонимы ключей: y/func/formula → f (кривая), поверхность — z */
function plotParseSpec(raw) {
  let src = String(raw || '').trim()
    .replace(/^```[a-zа-яё]*\s*/i, '').replace(/```\s*$/, '');
  if (!src) return {};
  /* BK: запись вида z(x,y) = sin(x)·cos(y) или f(x) = x^2 — модель так
     любит объявлять функции. Превращаем в пару «ключ = значение»:
     аргументы с y → поверхность z, иначе кривая f (граница — не буква,
     чтобы не разрезать sin(x) и т.п.) */
  src = src.replace(/(^|[^\w])([a-zA-Z])\s*\(([^)]*)\)\s*=/g,
    (mm, pre, name, args) => pre + (args.indexOf('y') >= 0 ? 'z' : 'f') + '= ');
  try { return JSON.parse(src); } catch (e) { /* дальше мягкий разбор */ }
  let s = src
    .replace(/\/\/[^\n]*/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/,\s*([}\]])/g, '$1')
    .replace(/([{,]\s*)([A-Za-z_]\w*)\s*:/g, '$1"$2":')
    .replace(/'/g, '"');
  try {
    const spec = JSON.parse(s);
    if (spec && typeof spec === 'object') return spec;
  } catch (e) { /* дальше пары ключ=значение */ }
  const out = {};
  const re = /([A-Za-z_]\w*)\s*[:=]\s*("[^"]*"|'[^']*'|\[[^\]]*\]|[^,\n\]}]+)/g;
  let m;
  while ((m = re.exec(src))) {
    const k = m[1].toLowerCase();
    let v = m[2].trim().replace(/^["']|["']$/g, '').trim();
    if (k === 'f' || k === 'z' || k === 'title') {
      if (v.startsWith('[')) {
        try { out[k] = JSON.parse(v.replace(/'/g, '"')); } catch (e) { out[k] = [v]; }
      } else out[k] = v;
    } else if (k === 'x' || k === 'range' || k === 'domain') {
      const arr = v.match(/-?\d+(?:\.\d+)?/g);
      if (arr && arr.length >= 2) out.x = [Number(arr[0]), Number(arr[1])];
    } else if (k === 'yy' || k === 'yrange' ||
               (k === 'y' && /^\[\s*-?\d/.test(v.replace(/'/g, '')))) {
      /* y=[-3,3] — это ДИАПАЗОН оси Y (поверхность), а не формула */
      const arr = v.match(/-?\d+(?:\.\d+)?/g);
      if (arr && arr.length >= 2) out.yy = [Number(arr[0]), Number(arr[1])];
    } else if (k === 'y' || k === 'func' || k === 'formula' || k === 'fn') {
      if (!out.f) out.f = v.startsWith('[') ? [v] : v;   // y=… — это кривая
    }
  }
  /* совсем без ключей? одиночная формула — это график: если в ней есть
     y — поверхность z, иначе кривая f */
  if (!out.f && !out.z && !out.title && !src.includes(':') && !src.includes('=')) {
    const t = src.replace(/^["']|["']$/g, '').trim();
    if (t && !/[\n{}]/.test(t)) out[/\by\b|[*,+\-\/].*\by\b/.test(t) ? 'z' : 'f'] = t;
  }
  return out;
}

function mountPlotPanels(root) {
  $$('.plot-panel', root).forEach((panel) => {
    if (panel.dataset.live === '1') return;
    panel.dataset.live = '1';
    panel._jarvisPlot = true;
    const spec = plotParseSpec(panel.dataset.plot);
    /* синонимы: модель пишет "y"/"func"/"formula" вместо "f" (строки —
     это кривые; числовой массив в y — диапазон оси для поверхности) */
    if (!spec.f && !spec.z && spec.y != null) {
      const ys = Array.isArray(spec.y) ? spec.y : [spec.y];
      if (ys.length && ys.every((v) => typeof v === 'string')) {
        spec.f = Array.isArray(spec.y) ? spec.y : spec.y;
        delete spec.y;
      }
    }
    if (!spec.f && (spec.func || spec.formula || spec.fn)) {
      spec.f = spec.func || spec.formula || spec.fn;
    }
    /* BK: СПАСЕНИЕ ФОРМУЛЫ. Ключи могут быть совсем нестандартными
       (equation, expr, surface…). Если f/z так и не нашлись — ищем
       среди ВСЕХ строковых значений первую, что компилируется: с y —
       поверхность, без — кривая. Раньше это валилось в «нет формул» */
    if (!spec.f && !spec.z) {
      for (const key of Object.keys(spec)) {
        const v = spec[key];
        const cand = Array.isArray(v) ? v.find((s) => typeof s === 'string') : v;
        if (typeof cand !== 'string' || !/[a-zA-Z]/.test(cand)) continue;
        try {
          const fn = mathCompile(cand);
          let ok = false;
          for (let i = 0; i <= 8 && !ok; i++) ok = isFinite(fn(-3 + i, -2 + i * .5));
          if (!ok) continue;
          if (/y/.test(cand)) spec.z = cand; else spec.f = [cand];
          break;
        } catch (e) { /* не формула — идём дальше */ }
      }
    }
    const kind = panel.dataset.kind || (spec.z ? 'plot3' : 'plot');
    try {
      if (kind === 'geo') buildGeoPanel(panel, spec);
      else if (spec.z) buildPlot3Panel(panel, spec);
      else buildPlot2Panel(panel, spec);
    } catch (e) {
      panel.innerHTML = '<div class="plot-err">' + esc(e.message || String(e)) + '</div>';
    }
  });
}

function plotShell(panel, title) {
  panel.innerHTML = '';
  const cv = el('canvas');
  const bar = el('div', 'plot-bar');
  const mkBtn = (label, fn) => {
    const b = el('button', '', label);
    b.addEventListener('click', fn);
    bar.appendChild(b);
    return b;
  };
  mkBtn('+', () => { panel._zoom(1.3); });
  mkBtn('\u2212', () => { panel._zoom(1 / 1.3); });
  mkBtn('\u27f2', () => { panel._reset(); });
  const read = el('div', 'plot-read');
  if (title) {
    const t = el('div', 'plot-title', esc(title));
    panel.appendChild(t);
  }
  panel.appendChild(cv);
  panel.appendChild(bar);
  panel.appendChild(read);
  const ctx = cv.getContext('2d');
  return { cv, ctx, read, bar };
}

function plotHiDpi(cv, ctx) {
  const r = cv.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  cv.width = Math.max(80, Math.round(r.width * dpr));
  cv.height = Math.max(80, Math.round(300 * dpr));
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { w: r.width, h: 300 };
}

/* ---------- 2D: функции y = f(x) ---------- */
/* BH: ГРАФИК КАК В МАТЕМАТИЧЕСКОМ РЕДАКТОРЕ.
   — ПАН свободный по ВСЕЙ площади (раньше уезжал только X: Y всё время
     пере-подгонялся под кривую — вид был приклеен к графику);
   — МАСШТАБ всегда: кнопки ± и колесо двигают ОБЕ оси, после первого
     касания Y перестаёт подгоняться и держится как есть;
   — ХОВЕР: точка на графике показывает, КАКОЙ функции она принадлежит
     (ближайшая кривая) и её координаты;
   — СООТНОШЕНИЕ ОСЕЙ: по умолчанию 1:1 (единичный квадрат — квадрат,
     окружность — круг), сбоку кнопка с выбором: авто, 1:1, 4:3, 3:2, 16:9. */
function buildPlot2Panel(panel, spec) {
  const fns = (Array.isArray(spec.f) ? spec.f : (spec.f ? [spec.f] : []))
    .filter((e) => e != null && String(e).trim() !== '').map((e) => mathCompile(e));
  if (!fns.length) {
    throw new Error('нет формул: для графика пиши {"f": ["sin(x)"], "x": [-6, 6]} — ' +
      'строго двойные кавычки; для поверхности 3D — {"z": "sin(x)*cos(y)", ' +
      '"x": [-3, 3], "y": [-3, 3]}');
  }
  const labels = (Array.isArray(spec.f) ? spec.f : [spec.f]).map((e) => String(e));
  const colors = ['#37d3ff', '#ffd489', '#8f86cf', '#3fbf95', '#e3798d'];
  const { cv, ctx, read, bar } = plotShell(panel, spec.title);
  const X0 = () => (spec.x && spec.x[0] != null) ? spec.x[0] : -6.28;
  const X1 = () => (spec.x && spec.x[1] != null) ? spec.x[1] : 6.28;
  let x0 = X0(), x1 = X1();
  let y0 = null, y1 = null;      // null = авто по кривым (только до первого касания)
  /* BI: СООТНОШЕНИЯ МАТЕМАТИКОВ, а не кино: единичное, изометрическое
     (бумага, √2), золотое сечение (φ), двойное и половинное */
  const ASPECTS = [
    { r: 1, label: '1:1' }, { r: Math.SQRT2, label: '1:√2' },
    { r: 1.618, label: '1:φ' }, { r: 2, label: '2:1' },
    { r: .5, label: '1:2' }, { r: 0, label: 'авто' },
  ];
  let aspect = ASPECTS[0];       // BH: по умолчанию честный 1:1
  let hover = null;

  const autoY = () => {
    let ya = Infinity, yb = -Infinity;
    for (let i = 0; i <= 160; i++) {
      const x = x0 + (x1 - x0) * i / 160;
      for (const f of fns) {
        const v = f(x);
        if (isFinite(v)) { ya = Math.min(ya, v); yb = Math.max(yb, v); }
      }
    }
    if (!isFinite(ya) || !isFinite(yb)) { ya = -1; yb = 1; }
    const pad = (yb - ya) * 0.12 + 0.5;
    return [ya - pad, yb + pad];
  };
  /* BI: «ПУСТАЯ ПЛОСКОСТЬ». Если функция определена вне заданного
     диапазона (корни, логарифмы), ищем её область определения
     расширяющимися кругами — как в 3D — и наводим окно на неё */
  const aimDomain = () => {
    for (const R of [2, 5, 10, 25, 60, 200]) {
      let xa = Infinity, xb = -Infinity, n = 0;
      for (let i = 0; i <= 40; i++) {
        const x = -R + 2 * R * i / 40;
        if (fns.some((f) => isFinite(f(x)))) {
          n++; xa = Math.min(xa, x); xb = Math.max(xb, x);
        }
      }
      if (n > 2) {
        const pad = (xb - xa) * 0.12 + 0.1;
        x0 = xa - pad; x1 = xb + pad;
        return true;
      }
    }
    return false;
  };

  const draw = () => {
    let { w, h } = plotHiDpi(cv, ctx);
    /* первый кадр: если функции нигде не определены — наводим окно */
    if (y0 === null && !fns.some((f) => {
      for (let i = 0; i <= 40; i++) {
        if (isFinite(f(x0 + (x1 - x0) * i / 40))) return true;
      }
      return false;
    })) aimDomain();
    if (y0 === null) { const a = autoY(); y0 = a[0]; y1 = a[1]; }
    if (aspect.r) {
      /* фиксированное соотношение: пикселей на единицу X и Y совпадают.
         На СТАРТЕ окно X подгоняется под высоту кривой — иначе на широкой
         области (±100) кривая сжималась в невидимую линию (»пустая
         плоскость«). Дальше соотношение держит только форму */
      if (!panel._aspectInit) {
        panel._aspectInit = true;
        const [ya, yb] = autoY();
        const spanX = (yb - ya) * (w / h) * aspect.r;
        const xc = (x0 + x1) / 2;
        x0 = xc - spanX / 2; x1 = xc + spanX / 2;
        const [na, nb] = autoY();
        y0 = na; y1 = nb;
      }
      const yc = (y0 + y1) / 2;
      const span = (x1 - x0) * h / (w * aspect.r);
      y0 = yc - span / 2; y1 = yc + span / 2;
    }
    const X = (x) => (x - x0) / (x1 - x0) * w;
    const Y = (y) => h - (y - y0) / (y1 - y0) * h;
    ctx.clearRect(0, 0, w, h);
    /* BH: ФОН СВЕТЛЕЕ — сетка и кривые читаются на светлых экранах */
    ctx.fillStyle = 'rgba(120,190,230,.05)';
    ctx.fillRect(0, 0, w, h);
    /* BI: ЗАСЕЧКИ-МАСШТАБ. Сетка идёт по КРАСИВЫМ шагам (1-2-5×10ⁿ),
         у каждой линии — подпись значения: масштаб виден на осях,
         а не только в углах */
    const niceStep = (span, target) => {
      const raw = span / Math.max(1, target);
      const pow = Math.pow(10, Math.floor(Math.log10(raw)));
      const m = raw / pow;
      return (m < 1.5 ? 1 : m < 3.5 ? 2 : m < 7.5 ? 5 : 10) * pow;
    };
    const sx = niceStep(x1 - x0, Math.round(w / 110));
    const sy = niceStep(y1 - y0, Math.round(h / 70));
    ctx.lineWidth = 1;
    ctx.font = '10px ui-monospace,monospace';
    const x0t = Math.ceil(x0 / sx) * sx, x1t = Math.floor(x1 / sx);
    for (let t = x0t; t <= x1t + 1e-9; t += sx) {
      const px = X(t);
      ctx.strokeStyle = Math.abs(t) < sx / 1e6 ? 'rgba(150,215,245,.5)' : 'rgba(120,200,235,.11)';
      ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, h); ctx.stroke();
    }
    const y0t = Math.ceil(y0 / sy) * sy, y1t = Math.floor(y1 / sy);
    for (let t = y0t; t <= y1t + 1e-9; t += sy) {
      const py = Y(t);
      ctx.strokeStyle = Math.abs(t) < sy / 1e6 ? 'rgba(150,215,245,.5)' : 'rgba(120,200,235,.11)';
      ctx.beginPath(); ctx.moveTo(0, py); ctx.lineTo(w, py); ctx.stroke();
    }
    ctx.strokeStyle = 'rgba(150,215,245,.55)';
    if (y0 < 0 && y1 > 0) { ctx.beginPath(); ctx.moveTo(0, Y(0)); ctx.lineTo(w, Y(0)); ctx.stroke(); }
    if (x0 < 0 && x1 > 0) { ctx.beginPath(); ctx.moveTo(X(0), 0); ctx.lineTo(X(0), h); ctx.stroke(); }
    /* подписи засечек: вдоль осей (или вдоль краёв, если ось вне кадра) */
    ctx.fillStyle = 'rgba(178,222,240,.9)';
    const axisY = (y0 < 0 && y1 > 0) ? Y(0) : h - 4;
    const axisX = (x0 < 0 && x1 > 0) ? X(0) : 4;
    for (let t = x0t; t <= x1t + 1e-9; t += sx) {
      if (Math.abs(t) < sx / 2) continue;
      const px = X(t);
      ctx.beginPath();
      ctx.moveTo(px, Math.max(4, Math.min(h - 2, axisY - 3)));
      ctx.lineTo(px, Math.max(4, Math.min(h - 2, axisY + 3)));
      ctx.strokeStyle = 'rgba(150,215,245,.6)'; ctx.stroke();
      const lbl = plotFmt(t);
      ctx.fillText(lbl, px - ctx.measureText(lbl).width / 2,
        Math.max(11, Math.min(h - 4, axisY + 15)));
    }
    for (let t = y0t; t <= y1t + 1e-9; t += sy) {
      if (Math.abs(t) < sy / 2) continue;
      const py = Y(t);
      ctx.beginPath();
      ctx.moveTo(Math.max(2, Math.min(w - 4, axisX - 3)), py);
      ctx.lineTo(Math.max(2, Math.min(w - 4, axisX + 3)), py);
      ctx.strokeStyle = 'rgba(150,215,245,.6)'; ctx.stroke();
      const lbl = plotFmt(t);
      ctx.fillText(lbl, Math.max(2, axisX + 6), py + 3);
    }
    fns.forEach((f, fi) => {
      ctx.strokeStyle = colors[fi % colors.length];
      ctx.lineWidth = 1.8;
      ctx.beginPath();
      let pen = false;
      for (let px = 0; px <= w; px += 1) {
        const x = x0 + (x1 - x0) * px / w;
        const y = f(x);
        if (!isFinite(y) || y < y0 - (y1 - y0) * 2 || y > y1 + (y1 - y0) * 2) { pen = false; continue; }
        const py = Y(y);
        if (!pen) { ctx.moveTo(px, py); pen = true; } else ctx.lineTo(px, py);
      }
      ctx.stroke();
    });
    /* BH: ХОВЕР — точка принадлежит БЛИЖАЙШЕЙ кривой: показываем её
       имя (формулу), цвет и координаты, на самом графике — маркер */
    if (hover) {
      const x = x0 + (x1 - x0) * hover.px / w;
      let best = null;
      fns.forEach((f, fi) => {
        const y = f(x);
        if (!isFinite(y)) return;
        const py = Y(y);
        const d = Math.abs(py - hover.py);
        if (d < (best ? best.d : 26)) best = { d, fi, y, py };
      });
      if (best) {
        ctx.beginPath();
        ctx.arc(hover.px, best.py, 4.6, 0, Math.PI * 2);
        ctx.fillStyle = colors[best.fi % colors.length];
        ctx.globalAlpha = .28; ctx.fill(); ctx.globalAlpha = 1;
        ctx.lineWidth = 1.6; ctx.strokeStyle = colors[best.fi % colors.length]; ctx.stroke();
        read.innerHTML = '<b style="color:' + colors[best.fi % colors.length] + '">' +
          'f' + (fns.length > 1 ? (best.fi + 1) : '') + ' = ' + esc(labels[best.fi]) + '</b>' +
          ' · x = ' + plotFmt(x) + ' · y = ' + plotFmt(best.y);
      } else {
        read.textContent = 'x = ' + plotFmt(x);
      }
    } else {
      read.textContent = 'тащи — двигать · колесо — масштаб';
    }
  };
  panel._zoom = (k) => {
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
    x0 = cx - (cx - x0) / k; x1 = cx + (x1 - cx) / k;
    y0 = cy - (cy - y0) / k; y1 = cy + (y1 - cy) / k;
    draw();
  };
  panel._reset = () => { x0 = X0(); x1 = X1(); y0 = y1 = null; aspect = ASPECTS[0]; syncAspect(); draw(); };
  /* кнопка соотношения осей + выезжающие параметры */
  const aspBtn = el('button', '', '1:1');
  aspBtn.title = 'Соотношение осей';
  aspBtn.style.width = 'auto';
  aspBtn.style.padding = '0 8px';
  aspBtn.style.fontSize = '11px';
  const pop = el('div', 'plot-pop');
  const syncAspect = () => {
    aspBtn.textContent = aspect.label;
    $$('.plot-opt', pop).forEach((b, i) => b.classList.toggle('on', ASPECTS[i] === aspect));
  };
  ASPECTS.forEach((a) => {
    const b = el('button', 'plot-opt', a.label);
    b.addEventListener('click', (e) => {
      e.stopPropagation();
      aspect = a; pop.classList.remove('open'); syncAspect(); draw();
    });
    pop.appendChild(b);
  });
  aspBtn.addEventListener('click', (e) => { e.stopPropagation(); pop.classList.toggle('open'); });
  bar.appendChild(aspBtn);
  panel.appendChild(pop);
  syncAspect();
  let drag = null;
  cv.addEventListener('pointerdown', (e) => {
    pop.classList.remove('open');
    drag = { x: e.clientX, y: e.clientY, x0, x1, y0, y1 };
    cv.setPointerCapture(e.pointerId);
    /* BK: печать пересобирает хвост и рвёт захват — после возврата панели
       вернём захват тому же пальцу, пан не сорвётся */
    panel._recapture = () => {
      try { cv.setPointerCapture(e.pointerId); } catch (err) { panel._recapture = null; }
    };
  });
  cv.addEventListener('pointermove', (e) => {
    const r = cv.getBoundingClientRect();
    hover = { px: e.clientX - r.left, py: e.clientY - r.top };
    if (!drag) { draw(); return; }
    /* ПАН ПО ВСЕЙ ПЛОЩАДИ: едут обе оси (Y больше не подгоняется).
       BJ: ИСПРАВЛЕННАЯ МАТЕМАТИКА — раньше сдвиг домена делился на ширину
       холста ещё раз, и пан двигал график на доли пикселя: график
       «не перемещался». Сдвиг в пикселях × единиц на пиксель — вот и всё */
    const ux = (drag.x1 - drag.x0) / r.width;
    const uy = (drag.y1 - drag.y0) / r.height;
    const dx = (drag.x - e.clientX) * ux;
    const dy = (e.clientY - drag.y) * uy;
    x0 = drag.x0 + dx;
    x1 = drag.x1 + dx;
    y0 = drag.y0 + dy;
    y1 = drag.y1 + dy;
    draw();
  });
  cv.addEventListener('pointerup', () => { drag = null; panel._recapture = null; });
  cv.addEventListener('pointercancel', () => { drag = null; panel._recapture = null; });
  cv.addEventListener('pointerleave', () => { hover = null; draw(); });
  cv.addEventListener('wheel', (e) => {
    e.preventDefault();
    const r = cv.getBoundingClientRect();
    const mx = x0 + (e.clientX - r.left) / r.width * (x1 - x0);
    const my = y1 - (e.clientY - r.top) / r.height * (y1 - y0);
    const k = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    x0 = mx - (mx - x0) / k; x1 = mx + (x1 - mx) / k;
    y0 = my - (my - y0) / k; y1 = my + (y1 - my) / k;
    draw();
  }, { passive: false });
  draw();
}

/* ---------- 3D: поверхность z = f(x,y), вращение мышью ---------- */
function buildPlot3Panel(panel, spec) {
  /* BJ: z бывает и массивом из одного элемента — берём формулу, а не
     падаем на «нет формул» */
  const zSrc = Array.isArray(spec.z) ? (spec.z[0] != null ? spec.z[0] : '') : spec.z;
  if (!String(zSrc || '').trim()) {
    throw new Error('нет формулы поверхности: пиши {"z": "sin(x)*cos(y)", ' +
      '"x": [-3, 3], "y": [-3, 3]} — строго двойные кавычки');
  }
  const fz = mathCompile(String(zSrc));
  const xr = (spec.x && spec.x.length === 2) ? spec.x : [-3, 3];
  const yr = ((spec.y && spec.y.length === 2) ? spec.y :
    ((spec.yy && spec.yy.length === 2) ? spec.yy : [-3, 3]));
  const { cv, ctx, read } = plotShell(panel, spec.title);
  let alpha = -0.65, beta = 0.6, zoom = 1;
  /* BI: ЖЕСТЫ как просил юзер: одна кнопка/палец — ВРАЩЕНИЕ,
     колесо мышки / два пальца — ПАНорамирование, ± — масштаб */
  let panX = 0, panY = 0;
  const N = 42;
  /* BH: «ПОВЕРХНОСТЬ ПУСТА». Если функция определена не везде (корни,
     логарифмы, деление), сетка из NaN дырявит всю поверхность, а на узком
     диапазоне может не поймать ни одной конечной точки. Сначала ищем
     область определения расширяющимися кругами; нашли — наводим диапазоны
     на неё; нет нигде — честно сообщаем про формулу */
  const sampleGrid = () => {
    let zmin = Infinity, zmax = -Infinity;
    const grid = [];
    for (let i = 0; i <= N; i++) {
      const row = [];
      const x = xr[0] + (xr[1] - xr[0]) * i / N;
      for (let j = 0; j <= N; j++) {
        const y = yr[0] + (yr[1] - yr[0]) * j / N;
        const z = fz(x, y);
        if (isFinite(z)) { zmin = Math.min(zmin, z); zmax = Math.max(zmax, z); }
        row.push(z);
      }
      grid.push(row);
    }
    return { grid, zmin, zmax };
  };
  let sampled = sampleGrid();
  if (!isFinite(sampled.zmin) || !isFinite(sampled.zmax)) {
    let aimed = false;
    for (const R of [2, 5, 10, 25, 60]) {
      let xa = Infinity, xb = -Infinity, ya = Infinity, yb = -Infinity, n = 0;
      for (let i = 0; i <= 24; i++) {
        for (let j = 0; j <= 24; j++) {
          const x = -R + 2 * R * i / 24, y = -R + 2 * R * j / 24;
          if (isFinite(fz(x, y))) {
            n++; xa = Math.min(xa, x); xb = Math.max(xb, x);
            ya = Math.min(ya, y); yb = Math.max(yb, y);
          }
        }
      }
      if (n > 8) {
        const px = (xb - xa) * 0.12 + 0.1, py = (yb - ya) * 0.12 + 0.1;
        xr[0] = xa - px; xr[1] = xb + px; yr[0] = ya - py; yr[1] = yb + py;
        aimed = true;
        break;
      }
    }
    if (!aimed) throw new Error('поверхность пуста: функция нигде не определена — проверь формулу z');
    sampled = sampleGrid();
    if (!isFinite(sampled.zmin) || !isFinite(sampled.zmax)) {
      throw new Error('поверхность пуста: не удалось нацелиться на область определения');
    }
  }
  const draw = () => {
    const { w, h } = plotHiDpi(cv, ctx);
    ctx.clearRect(0, 0, w, h);
    const cx = w / 2 + panX, cy = h / 2 + 14 + panY;
    const ca = Math.cos(alpha), sa = Math.sin(alpha);
    const cb = Math.cos(beta), sb = Math.sin(beta);
    const scale = Math.min(w, h) / 3.4 * zoom;
    const project = (x, y, z) => {
      const X = x * ca - y * sa;
      const Y0 = x * sa + y * ca;
      const Y = Y0 * sb - z * cb;
      const depth = Y0 * cb + z * sb;
      return { px: cx + X * scale, py: cy - Y * scale, depth };
    };
    const { grid, zmin, zmax } = sampled;
    const zr = Math.max(1e-6, zmax - zmin);
    const quads = [];
    for (let i = 0; i < N; i++) {
      for (let j = 0; j < N; j++) {
        /* BH: дырявые клетки (NaN в любом углу) просто пропускаем —
           поверхность рисуется там, где определена */
        const c00 = grid[i][j], c10 = grid[i + 1][j],
              c11 = grid[i + 1][j + 1], c01 = grid[i][j + 1];
        if (!isFinite(c00) || !isFinite(c10) || !isFinite(c11) || !isFinite(c01)) continue;
        const x1 = xr[0] + (xr[1] - xr[0]) * i / N, x2 = xr[0] + (xr[1] - xr[0]) * (i + 1) / N;
        const y1 = yr[0] + (yr[1] - yr[0]) * j / N, y2 = yr[0] + (yr[1] - yr[0]) * (j + 1) / N;
        const c = [project(x1, y1, c00), project(x2, y1, c10),
          project(x2, y2, c11), project(x1, y2, c01)];
        const zm = (c00 + c10 + c11 + c01) / 4;
        quads.push({ c, zm });
      }
    }
    quads.sort((a, b) => b.c[0].depth + b.c[2].depth - (a.c[0].depth + a.c[2].depth));
    for (const q of quads) {
      const t = (q.zm - zmin) / zr;
      const light = 0.35 + 0.65 * Math.max(0, Math.min(1, t));
      ctx.fillStyle = 'rgba(' + Math.round(40 + 120 * light) + ',' +
        Math.round(120 + 110 * light) + ',' + Math.round(190 + 60 * light) + ',.92)';
      ctx.beginPath();
      ctx.moveTo(q.c[0].px, q.c[0].py);
      for (let k = 1; k < 4; k++) ctx.lineTo(q.c[k].px, q.c[k].py);
      ctx.closePath(); ctx.fill();
      ctx.strokeStyle = 'rgba(230,250,255,.10)';
      ctx.lineWidth = .5;
      ctx.stroke();
    }
    read.textContent = 'вращай — мышью · сдвиг — колесом или двумя пальцами · ± — масштаб';
  };
  panel._zoom = (k) => { zoom *= k; draw(); };
  panel._reset = () => { alpha = -0.65; beta = 0.6; zoom = 1; panX = panY = 0; draw(); };
  /* мультитач: один указатель — вращение, два — панорамирование */
  const pts = new Map();
  let lastMid = null;
  cv.addEventListener('pointerdown', (e) => {
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    cv.setPointerCapture(e.pointerId);
    /* BK: пересборка хвоста печатью рвёт захват — вернём его панели */
    panel._recapture = () => {
      try { cv.setPointerCapture(e.pointerId); } catch (err) { panel._recapture = null; }
    };
    if (pts.size === 2) {
      const arr = Array.from(pts.values());
      lastMid = { x: (arr[0].x + arr[1].x) / 2, y: (arr[0].y + arr[1].y) / 2 };
    }
  });
  cv.addEventListener('pointermove', (e) => {
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pts.size >= 2 && lastMid) {
      const arr = Array.from(pts.values());
      const mid = { x: (arr[0].x + arr[1].x) / 2, y: (arr[0].y + arr[1].y) / 2 };
      panX += mid.x - lastMid.x;
      panY += mid.y - lastMid.y;
      lastMid = mid;
      draw();
      return;
    }
    if (pts.size !== 1) return;
    const p = pts.get(e.pointerId);
    const dx = e.clientX - p.x, dy = e.clientY - p.y;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    alpha -= dx * 0.011;
    beta = Math.max(-1.35, Math.min(1.35, beta + dy * 0.011));
    draw();
  });
  const lift = (e) => {
    pts.delete(e.pointerId);
    if (pts.size < 2) lastMid = null;
    if (!pts.size) panel._recapture = null;
  };
  cv.addEventListener('pointerup', lift);
  cv.addEventListener('pointercancel', lift);
  /* колесо — ПАН (масштаб остаётся на кнопках ±): так удобнее на тачпаде */
  cv.addEventListener('wheel', (e) => {
    e.preventDefault();
    panX -= e.deltaX;
    panY -= e.deltaY;
    draw();
  }, { passive: false });
  draw();
}

/* ---------- Геометрия: точки, отрезки, многоугольники, окружности ---------- */
function buildGeoPanel(panel, spec) {
  const { cv, ctx, read } = plotShell(panel, spec.title);
  const pts = spec.points || {};
  const names = Object.keys(pts);
  if (!names.length) throw new Error('нет точек: нужен ключ "points"');
  let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
  for (const n of names) {
    const [x, y] = pts[n];
    xmin = Math.min(xmin, x); xmax = Math.max(xmax, x);
    ymin = Math.min(ymin, y); ymax = Math.max(ymax, y);
  }
  for (const c of (spec.circles || [])) {
    const [, r] = c;
    const [cx, cy] = pts[c[0]] || [0, 0];
    xmin = Math.min(xmin, cx - r); xmax = Math.max(xmax, cx + r);
    ymin = Math.min(ymin, cy - r); ymax = Math.max(ymax, cy + r);
  }
  const view = { xmin: xmin - 1, xmax: xmax + 1, ymin: ymin - 1, ymax: ymax + 1 };
  const draw = () => {
    const { w, h } = plotHiDpi(cv, ctx);
    const sx = w / (view.xmax - view.xmin), sy = h / (view.ymax - view.ymin);
    const s = Math.min(sx, sy);
    const ox = (w - s * (view.xmax - view.xmin)) / 2;
    const oy = (h - s * (view.ymax - view.ymin)) / 2;
    const X = (x) => ox + (x - view.xmin) * s;
    const Y = (y) => h - oy - (y - view.ymin) * s;
    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = 'rgba(0,190,255,.08)';
    for (let i = 0; i <= 8; i++) {
      ctx.beginPath(); ctx.moveTo(i * w / 8, 0); ctx.lineTo(i * w / 8, h); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, i * h / 8); ctx.lineTo(w, i * h / 8); ctx.stroke();
    }
    // многоугольники — заливка
    for (const poly of (spec.polygons || [])) {
      ctx.beginPath();
      poly.forEach((n, i) => {
        const [x, y] = pts[n] || [0, 0];
        if (!i) ctx.moveTo(X(x), Y(y)); else ctx.lineTo(X(x), Y(y));
      });
      ctx.closePath();
      ctx.fillStyle = 'rgba(55,211,255,.10)';
      ctx.fill();
      ctx.strokeStyle = 'rgba(160,230,250,.9)';
      ctx.lineWidth = 1.6;
      ctx.stroke();
    }
    for (const seg of (spec.segments || [])) {
      const [a, b] = seg;
      const pa = pts[a] || [0, 0], pb = pts[b] || [0, 0];
      ctx.beginPath(); ctx.moveTo(X(pa[0]), Y(pa[1])); ctx.lineTo(X(pb[0]), Y(pb[1]));
      ctx.strokeStyle = 'rgba(160,230,250,.9)'; ctx.lineWidth = 1.6; ctx.stroke();
    }
    for (const c of (spec.circles || [])) {
      const [cn, r] = c;
      const [cx, cy] = pts[cn] || [0, 0];
      ctx.beginPath(); ctx.arc(X(cx), Y(cy), r * s, 0, Math.PI * 2);
      ctx.strokeStyle = 'rgba(160,230,250,.75)'; ctx.lineWidth = 1.4; ctx.stroke();
    }
    ctx.font = '600 11px ui-monospace,monospace';
    for (const n of names) {
      const [x, y] = pts[n];
      ctx.beginPath(); ctx.arc(X(x), Y(y), 2.6, 0, Math.PI * 2);
      ctx.fillStyle = '#eaf9ff'; ctx.fill();
      ctx.fillStyle = 'rgba(220,240,250,.95)';
      ctx.fillText(n, X(x) + 5, Y(y) - 5);
    }
    read.textContent = 'перетаскивай · колесо — масштаб';
  };
  panel._zoom = (k) => {
    const cx = (view.xmin + view.xmax) / 2, cy = (view.ymin + view.ymax) / 2;
    view.xmin = cx - (cx - view.xmin) / k; view.xmax = cx + (view.xmax - cx) / k;
    view.ymin = cy - (cy - view.ymin) / k; view.ymax = cy + (view.ymax - cy) / k;
    draw();
  };
  panel._reset = () => { view.xmin = xmin - 1; view.xmax = xmax + 1; view.ymin = ymin - 1; view.ymax = ymax + 1; draw(); };
  let drag = null;
  cv.addEventListener('pointerdown', (e) => {
    drag = { x: e.clientX, y: e.clientY, v: { ...view } };
    cv.setPointerCapture(e.pointerId);
    /* BK: пересборка хвоста печатью рвёт захват — вернём его панели */
    panel._recapture = () => {
      try { cv.setPointerCapture(e.pointerId); } catch (err) { panel._recapture = null; }
    };
  });
  cv.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const r = cv.getBoundingClientRect();
    const dx = (drag.x - e.clientX) / r.width * (drag.v.xmax - drag.v.xmin);
    const dy = (e.clientY - drag.y) / r.height * (drag.v.ymax - drag.v.ymin);
    view.xmin = drag.v.xmin + dx; view.xmax = drag.v.xmax + dx;
    view.ymin = drag.v.ymin + dy; view.ymax = drag.v.ymax + dy;
    draw();
  });
  cv.addEventListener('pointerup', () => { drag = null; panel._recapture = null; });
  cv.addEventListener('pointercancel', () => { drag = null; panel._recapture = null; });
  cv.addEventListener('wheel', (e) => {
    e.preventDefault();
    panel._zoom(e.deltaY < 0 ? 1.15 : 1 / 1.15);
  }, { passive: false });
  draw();
}

function mountUiPanels(root, opts) {
  if (!root) return;
  const inert = !!(opts && opts.inert);
  $$('.ui-panel', root).forEach((box) => {
    if (box.dataset.live === '1') return;
    const items = parseUiSpec(box.dataset.ui || '');
    if (!items.length || !hasMeaningfulUiItems(items)) { box.remove(); return; }
    stripMirroredChoiceList(box, items);
    box.dataset.live = '1';
    if (inert) box.classList.add('ui-inert');   // AD: законсервированная панель истории
    box.innerHTML = '';

    // AA: ПАНЕЛЬ ПРИНАДЛЕЖИТ СВОЕМУ СООБЩЕНИЮ. Ответ на неё — продолжение
    // ТОГО ЖЕ ответа, и это должно работать даже после перерисовки ленты:
    // живая нода умирает при переключении диалога, а msgId — нет.
    const panelMsg = box.closest('.msg');
    const panelMsgId = (panelMsg && panelMsg.dataset.msgId) || '';

    const HUES = ['c1', 'c2', 'c3', 'c4', 'c5'];
    // AB: ЛЮБОЙ ВЫБОР РОВНО ИЗ ДВУХ ВАРИАНТОВ — ТА ЖЕ СНОСКА ДА/НЕТ:
    // маленькая строка и две кнопки (первая зелёная, вторая красная), клик
    // и есть ответ. Пара больше не растягивается в большую панель с плитками.
    const pair = (items.length === 1 && items[0].t === 'tiles' &&
                  (items[0].opts || []).length === 2) ? items[0] : null;
    if (pair) { pair.t = 'confirm'; pair.val = null; }
    // ЧИСТЫЕ ПЛИТКИ НЕ ЖДУТ КНОПКУ: один клик = выбор = отправка. Единственный
    // формат, где подтверждение лишено смысла — нечего докручивать, нечему
    // передумать: одно касание уже и есть весь ответ.
    const tilesOnly = items.length > 0 && items.every((x) => x.t === 'tiles');
    // AA: ЧИСТЫЙ confirm — то же самое: клик по «Да»/«Нет» и есть весь ответ,
    // никакой кнопки «Отправить» и «Своего варианта» — да/нет не пишут словами
    const confirmOnly = items.length > 0 && items.every((x) => x.t === 'confirm');
    // ЛЮБАЯ панель отправляется ТОЛЬКО кнопкой «Отправить». Раньше чистые
    // плитки улетали по первому касанию (таймер на 900 мс), а тумблеры и
    // звёзды — сразу; передумать было нельзя, промах стоил отправки. Теперь
    // клик всегда означает только выбор с подсветкой — как в живом диалоге.
    const ANALOG = { slider: 1, number: 1, text: 1, area: 1, date: 1,
                     color: 1, rank: 1, multi: 1, rate: 1 };
    let touched = false;
    let go = null;

    let ownVal = '';                       // «свой вариант» — вне списка items
    const summary = () => items.filter((x) => x.t !== 'button').map((x) => {
      if (x.t === 'confirm') return x.label + ': ' + (x.val || '—');
      if (x.t === 'toggle') return x.label + ': ' + (x.val ? 'да' : 'нет');
      if (x.t === 'multi') return x.label + ': ' + (x.val.length ? x.val.join(', ') : '—');
      // порядок и есть ответ — нумеруем, иначе смысл расстановки теряется
      if (x.t === 'rank') return x.label + ': ' + x.opts.map((o, i) => (i + 1) + ') ' + o).join(', ');
      if (x.t === 'rate') return x.label + ': ' + (x.val ? x.val + ' из ' + x.max : '—');
      return x.label + ': ' + (x.val == null || x.val === '' ? '—' : x.val);
    }).concat(ownVal.trim() ? ['Свой вариант: ' + ownVal.trim()] : []);

    /* Один источник истины для готовности всей панели.
       Раньше каждый control сам пытался включить Send. В mixed-панели плитка
       звала armSend(), та видела slider и выходила раньше обновления disabled —
       выбранный обязательный вариант оставлял кнопку серой. Date/rate/multi
       вдобавок считались заполненными ещё до ответа. Теперь обязательны все
       controls без начального осмысленного значения, а непустой «Свой вариант»
       является полноценной альтернативой всей форме. */
    const ready = () => items.every((x) => {
      if (x.t === 'tiles') return x.val != null;
      if (x.t === 'confirm') return x.val != null;
      if (x.t === 'text' || x.t === 'area' || x.t === 'date') {
        return String(x.val || '').trim().length > 0;
      }
      if (x.t === 'rate') return Number(x.val) > 0;
      if (x.t === 'multi') return Array.isArray(x.val) && x.val.length > 0;
      return true;
    });
    const canSend = () => ready() || Boolean(ownVal.trim());
    const syncSendState = () => {
      const valid = canSend();
      if (go) go.disabled = !valid;
      return valid;
    };

    const fire = () => {
      if (box.dataset.sent === '1' || !syncSendState()) return;
      box.dataset.sent = '1';
      box.classList.add('ui-sent');
      $$('input,button,textarea', box).forEach((c) => { c.disabled = true; });
      $('#input').value = summary().join('\n'); autoGrow();
      send({ silent: true, continue: true, continueOf: panelMsgId });
      sfx('send');
    };

    // Выбор меняет только состояние кнопки: нет таймера, нет автопосылки.
    // Подтверждение — всегда осознанное нажатие «Отправить».
    const armSend = () => { syncSendState(); };
    const controlChanged = () => {
      touched = true;
      syncSendState();
    };

    items.forEach((it, idx) => {
      // AA: confirm — ВОПРОС ДА/НЕТ отдельной сноской: не большая панель,
      // а тихая строка с зелёной «Да» и красной «Нет». В чистой панели клик
      // сразу отправляет ответ; в смешанной — работает как выбор (ждёт
      // общей кнопки «Отправить»).
      if (it.t === 'confirm') {
        const row = el('div', 'ui-row ui-confirm');
        row.style.animationDelay = (idx * 55) + 'ms';
        // подписи — сами варианты; у настоящего да/нет это «Да» и «Нет»
        const labels = (it.opts && it.opts.length === 2) ? it.opts : ['Да', 'Нет'];
        row.innerHTML =
          '<div class="cn-q">' + esc(it.label) + '</div>' +
          '<div class="cn-btns">' +
            '<button class="cn-btn cn-yes">' + esc(labels[0]) + '</button>' +
            '<button class="cn-btn cn-no">' + esc(labels[1]) + '</button>' +
          '</div>';
        const btns = row.querySelector('.cn-btns');
        row.querySelectorAll('.cn-btn').forEach((b) => {
          b.addEventListener('click', () => {
            if (box.dataset.sent === '1') return;
            const val = b.textContent.trim();
            it.val = val;
            sfx('select');
            if (confirmOnly) {
              btns.innerHTML = '<span class="cn-picked">✓ ' + esc(val) + '</span>';
              setTimeout(fire, 120);
            } else {
              row.querySelectorAll('.cn-btn').forEach((x) => x.classList.remove('sel'));
              b.classList.add('sel');
              controlChanged();
            }
          });
        });
        box.appendChild(row);
        return;
      }

      const row = el('div', 'ui-row ui-' + it.t + ' ' + HUES[idx % HUES.length]);
      row.style.animationDelay = (idx * 55) + 'ms';

      if (it.t === 'slider') {
        row.innerHTML = '<div class="ui-lab"><span>' + esc(it.label) +
          '</span><b class="ui-val">' + it.val + (it.unit ? ' ' + esc(it.unit) : '') + '</b></div>';
        const inp = el('input', 'ui-range');
        inp.type = 'range'; inp.min = it.min; inp.max = it.max;
        inp.step = it.step; inp.value = it.val;
        const out = row.querySelector('.ui-val');
        const paint = () => {
          const pct = ((inp.value - it.min) / (it.max - it.min || 1)) * 100;
          inp.style.setProperty('--fill', pct + '%');
        };
        inp.addEventListener('input', () => {
          it.val = parseFloat(inp.value);
          out.textContent = inp.value + (it.unit ? ' ' + it.unit : ''); paint();
          out.classList.remove('bump'); void out.offsetWidth; out.classList.add('bump');
          controlChanged();
        });
        paint();
        row.appendChild(inp);

      } else if (it.t === 'number') {
        // счётчик: то же число, но щёлкается кнопками — удобно для «сколько штук»
        row.innerHTML = '<div class="ui-lab"><span>' + esc(it.label) + '</span></div>';
        const st = el('div', 'ui-step');
        const minus = el('button', 'ui-stepb', '−');
        const val = el('b', 'ui-val ui-num', String(it.val));
        const plus = el('button', 'ui-stepb', '+');
        const setv = (v) => {
          it.val = Math.max(it.min, Math.min(it.max, Math.round(v / it.step) * it.step));
          it.val = parseFloat(it.val.toFixed(4));
          val.textContent = it.val + (it.unit ? ' ' + it.unit : '');
          val.classList.remove('bump'); void val.offsetWidth; val.classList.add('bump');
          controlChanged(); sfx('select');
        };
        minus.addEventListener('click', () => setv(it.val - it.step));
        plus.addEventListener('click', () => setv(it.val + it.step));
        st.appendChild(minus); st.appendChild(val); st.appendChild(plus);
        row.appendChild(st);

      } else if (it.t === 'text') {
        // строка ввода: когда вариантов не перечислить
        row.innerHTML = '<div class="ui-lab"><span>' + esc(it.label) + '</span></div>';
        const inp = el('input', 'ui-text');
        inp.type = 'text'; inp.value = it.val || '';
        inp.placeholder = it.hint || 'впиши ответ…';
        inp.addEventListener('input', () => {
          it.val = inp.value; controlChanged();
        });
        inp.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') { e.preventDefault(); if (touched) fire(); }
        });
        row.appendChild(inp);

      } else if (it.t === 'area') {
        // длинный ответ: когда одной строки заведомо мало
        row.innerHTML = '<div class="ui-lab"><span>' + esc(it.label) + '</span></div>';
        const ta = el('textarea', 'ui-area');
        ta.rows = 3;
        ta.placeholder = it.hint || 'можно подробно…';
        ta.addEventListener('input', () => {
          it.val = ta.value; controlChanged();
        });
        row.appendChild(ta);

      } else if (it.t === 'date') {
        row.innerHTML = '<div class="ui-lab"><span>' + esc(it.label) + '</span></div>';
        const inp = el('input', 'ui-text ui-date');
        inp.type = 'date'; inp.value = it.val || '';
        inp.addEventListener('input', () => { it.val = inp.value; controlChanged(); sfx('select'); });
        row.appendChild(inp);

      } else if (it.t === 'color') {
        row.innerHTML = '<div class="ui-lab"><span>' + esc(it.label) +
          '</span><b class="ui-val">' + esc(it.val) + '</b></div>';
        const inp = el('input', 'ui-color');
        inp.type = 'color'; inp.value = it.val;
        const out = row.querySelector('.ui-val');
        inp.addEventListener('input', () => {
          it.val = inp.value; out.textContent = inp.value; controlChanged();
        });
        row.appendChild(inp);

      } else if (it.t === 'rate') {
        // оценка: быстрый способ ответить «насколько», не набирая цифру
        row.innerHTML = '<div class="ui-lab"><span>' + esc(it.label) +
          '</span><b class="ui-val">' + (it.val || '—') + '</b></div>';
        const st = el('div', 'ui-stars');
        const out = row.querySelector('.ui-val');
        const paint = () => {
          $$('.ui-star', st).forEach((b, i) => b.classList.toggle('on', i < it.val));
          out.textContent = it.val ? it.val + ' / ' + it.max : '—';
        };
        for (let n = 1; n <= it.max; n++) {
          const b = el('button', 'ui-star', '★');
          b.addEventListener('click', () => { it.val = n; paint(); controlChanged(); sfx('select'); });
          st.appendChild(b);
        }
        paint();
        row.appendChild(st);

      } else if (it.t === 'multi') {
        // несколько вариантов сразу — плитки не подходят, там выбор один
        row.innerHTML = '<div class="ui-lab"><span>' + esc(it.label) + '</span></div>';
        const grid = el('div', 'ui-tiles');
        it.opts.forEach((o, oi) => {
          const t = el('button', 'ui-tile ui-multi-t ' + HUES[(oi + idx) % HUES.length], o);
          t.addEventListener('click', () => {
            const at = it.val.indexOf(o);
            if (at >= 0) it.val.splice(at, 1); else it.val.push(o);
            t.classList.toggle('on', at < 0);
            controlChanged(); sfx('select');
          });
          grid.appendChild(t);
        });
        row.appendChild(grid);

      } else if (it.t === 'rank') {
        // расстановка по важности: ответ — сам порядок строк
        row.innerHTML = '<div class="ui-lab"><span>' + esc(it.label) +
          '</span><b class="ui-hint">по важности</b></div>';
        const list = el('div', 'ui-rank');
        const paint = () => {
          $$('.ui-rk', list).forEach((r, i) => {
            r.querySelector('.rk-n').textContent = i + 1;
            r.querySelector('.rk-up').disabled = i === 0;
            r.querySelector('.rk-dn').disabled = i === it.opts.length - 1;
          });
          it.val = it.opts.slice();
        };
        const move = (from, to) => {
          if (to < 0 || to >= it.opts.length) return;
          it.opts.splice(to, 0, it.opts.splice(from, 1)[0]);
          const rows = $$('.ui-rk', list);
          list.insertBefore(rows[from], to < from ? rows[to] : rows[to].nextSibling);
          paint(); controlChanged(); sfx('select');
        };
        it.opts.forEach((o) => {
          const r = el('div', 'ui-rk');
          r.innerHTML = '<span class="rk-n"></span><span class="rk-t">' + esc(o) + '</span>';
          const up = el('button', 'rk-up', '↑');
          const dn = el('button', 'rk-dn', '↓');
          up.addEventListener('click', () => move($$('.ui-rk', list).indexOf(r), $$('.ui-rk', list).indexOf(r) - 1));
          dn.addEventListener('click', () => move($$('.ui-rk', list).indexOf(r), $$('.ui-rk', list).indexOf(r) + 1));
          r.appendChild(up); r.appendChild(dn);
          list.appendChild(r);
        });
        paint();
        row.appendChild(list);

      } else if (it.t === 'toggle') {
        row.innerHTML = '<span class="ui-lab-t">' + esc(it.label) + '</span>';
        const sw = el('button', 'ui-sw' + (it.val ? ' on' : ''));
        sw.innerHTML = '<i></i>';
        sw.addEventListener('click', () => {
          it.val = !it.val; sw.classList.toggle('on', it.val);
          // ЗНАЧЕНИЕ СНАЧАЛА, звук потом: любой сбой звука (контекст
          // аудио уснул) не имеет права съесть регистрацию значения —
          // иначе переключатель «иногда не работает»
          controlChanged();
          try { blip(it.val); } catch (err) { /* звук не важен */ }
        });
        // клик по ПОДПИСИ тоже переключает: цель крупнее, мимо не промахнуться
        row.addEventListener('click', (e) => {
          if (sw.contains(e.target)) return;
          sw.click();
        });
        row.appendChild(sw);

      } else if (it.t === 'tiles') {
        row.innerHTML = '<div class="ui-lab"><span>' + esc(it.label) + '</span></div>';
        const grid = el('div', 'ui-tiles');
        it.opts.forEach((o, oi) => {
          const t = el('button', 'ui-tile ' + HUES[(oi + idx) % HUES.length], o);
          t.addEventListener('click', () => {
            it.val = o;
            $$('.ui-tile', grid).forEach((x) => x.classList.remove('on'));
            t.classList.add('on'); sfx('select');
            if (tilesOnly) { setTimeout(fire, 140); return; }
            controlChanged();
          });
          grid.appendChild(t);
        });
        row.appendChild(grid);

        } else {
          const b = el('button', 'ui-btn ' + HUES[idx % HUES.length], it.label);
          b.addEventListener('click', () => {
            if (box.dataset.sent === '1') return;
            box.dataset.sent = '1';
            box.classList.add('ui-sent');
            $$('input,button,textarea', box).forEach((c) => { c.disabled = true; });
            $('#input').value = it.label; autoGrow();
            send({ silent: true, continue: true, continueOf: panelMsgId });
            sfx('send');
          });
          row.appendChild(b);
        }
      box.appendChild(row);
    });

    // Свой вариант. Любой заранее собранный список конечен, а ответ человека —
    // нет: если ни одна плитка не подходит, панель не должна загонять в угол.
    // Поэтому в конце всегда есть строка, куда можно вписать своё.
    // Панелям из одних кнопок-действий и чистым да/нет-сноскам она не нужна.
    const askable = items.some((x) => x.t !== 'button') && !confirmOnly;
    // text/area уже И ЕСТЬ свободный «свой вариант»; второе одинаковое поле
    // только путало бы человека в deterministic fallback-вопросах.
    const hasFreeEntry = items.some((x) => x.t === 'text' || x.t === 'area');
    let own = null;
    if (askable && !hasFreeEntry) {
      const row = el('div', 'ui-row ui-own');
      row.style.animationDelay = (items.length * 55) + 'ms';
      row.innerHTML = '<div class="ui-lab"><span>Свой вариант</span></div>';
      own = el('input', 'ui-text ui-own-i');
      own.type = 'text';
      own.placeholder = 'если ничего не подходит — впиши своё…';
      row.appendChild(own);
      box.appendChild(row);
    }

    // Кнопка «Отправить» нужна везде, КРОМЕ чистых плиток и чистых да/нет:
    // там клик сам является подтверждением. Выглядит и ведёт себя как кнопка
    // отправки под полем ввода — та же стрелка, тот же смысл.
    if (askable && !tilesOnly && !confirmOnly) {
      go = el('button', 'ui-go', ICO.send + '<span>Отправить</span>');
      go.addEventListener('click', fire);
      // Плиткам кнопка не нужна: выбор уходит сам. Но как только человек начал
      // писать свой вариант, автоотправку надо отменить и дать ему кнопку —
      // иначе панель улетит на полуслове.
      box.appendChild(go);
      syncSendState();
    }
    if (own) {
      own.addEventListener('input', () => {
        ownVal = own.value;
        touched = true;
        syncSendState();
      });
      own.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); if (touched) fire(); }
      });
    }
  });
}

function foldCodeBlocks(root, animate) {
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
    const fold = () => collapseToThumb(wrap, {
      instant: true, cls: 'th-code inline-thumb', icon: ICO.code,
      title: lang ? 'Код · ' + lang : 'Код',
      sub: lines + ' стр. · ' + fmtSize(code.length),
      // ярлык «развернуть» — агентская эстетика; тихий режим чище без него
      tag: S.agentMode ? 'развернуть' : '',
    });
    if (!animate) { fold(); return; }
    // БАГ «перед сворачиванием разворачивается во весь рост»: раньше у pre
    // снимали класс live-code (его max-height держал высоту), и на кадр код
    // распахивался полностью. Сворачиваемся ровно от ВИДИМОЙ высоты: замер
    // до любых изменений, фиксация на обёртке и плавное съёживание в ноль.
    const visible = wrap.getBoundingClientRect().height;
    pre.classList.remove('live-code');
    wrap.style.overflow = 'hidden';
    wrap.style.height = Math.max(visible, 24) + 'px';
    requestAnimationFrame(() => {
      wrap.style.transition = 'height .34s cubic-bezier(.3,.7,.3,1), opacity .3s ease';
      wrap.style.height = '0px';
      wrap.style.opacity = '.25';
    });
    setTimeout(() => { wrap.style.transition = ''; fold(); scrollDown(false); }, 360);
  });
}

/* ============================ отправка ============================ */
function autoGrow() {
  const t = $('#input');
  const MAX = 190;
  // Измерять textarea через height:auto ненадёжно: браузер одновременно
  // применяет rows, max-height и нативный overflow:auto, поэтому у пустой
  // строки иногда остаётся лишний пиксель прокрутки. Сначала снимаем высоту
  // до нуля, измеряем ПОЛНЫЙ контент, затем одним решением задаём и высоту,
  // и режим overflow. Скролл появляется только когда контент реально выше MAX.
  t.style.height = '0px';
  const full = t.scrollHeight;
  const cs = getComputedStyle(t);
  const oneLine = Math.ceil((parseFloat(cs.lineHeight) || 22) +
    (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0));
  const wanted = t.value === '' ? oneLine : Math.max(oneLine, full);
  t.style.height = Math.min(wanted, MAX) + 'px';
  t.style.overflowY = wanted > MAX ? 'auto' : 'hidden';
  updateSendBtn();
}
$('#input').addEventListener('input', autoGrow);
$('#input').addEventListener('focus', () => $('#composer').classList.add('focus'));
$('#input').addEventListener('blur', () => $('#composer').classList.remove('focus'));
$('#input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});

/* ============================ лимит ₽ ============================ */
S.budgetRub = null;
const budgetBtn = $('#tgBudget');
const budgetPop = $('#budgetPop');

/* Суммы пользователя — его привычки, а не наши догадки. Каждая введённая
   сумма (кнопкой или вручную) запоминается; варианты предлагают ПОСЛЕДНИЕ
   4 РАЗНЫХ суммы. Живёт в localStorage — переживает перезагрузку. */
function budgetHistory() {
  try { return (JSON.parse(localStorage.getItem('jarvis.budgetHistory') || '[]') || [])
    .filter((x) => typeof x === 'number' && x > 0); } catch (e) { return []; }
}
function rememberBudget(value) {
  const v = parseFloat(value);
  if (!(v > 0)) return;
  const rest = budgetHistory().filter((x) => Math.abs(x - v) > 0.001);
  try { localStorage.setItem('jarvis.budgetHistory', JSON.stringify([v].concat(rest).slice(0, 8))); } catch (e) {}
  renderBudgetVariants();
}
function budgetSuggestions() {
  const hist = budgetHistory();
  return (hist.length ? hist : [10, 25, 50, 100]).slice(0, 4);
}
function renderBudgetVariants() {
  const box = $('#budgetVariants');
  if (!box) return;
  const cur = S.budgetRub;
  box.replaceChildren();
  budgetSuggestions().forEach((v) => {
    const b = el('button', 'bp-v' + (cur && Math.abs(cur - v) < 0.001 ? ' on' : ''), v + ' ₽');
    b.addEventListener('click', () => setBudget(v));
    box.appendChild(b);
  });
  const off = $('#budgetOff');
  if (off) off.hidden = !S.budgetRub;
  const hint = $('#budgetHint');
  if (hint) {
    hint.textContent = S.budgetRub
      ? 'Сейчас: ' + S.budgetRub + ' ₽ на один ответ'
      : (budgetHistory().length ? 'Твои обычные суммы' : 'Сколько можно потратить на один ответ');
  }
}

/* Открытие/закрытие панели лимита — РОВНО как у панели уведомлений:
   вылет npInUp, возврат npOutUp. Прежний пружинистый scale(.6) заменён:
   обе панели теперь прилетают одним и тем же характером движения. */
function hideBudgetPop() {
  if (budgetPop.hidden || budgetPop.classList.contains('bp-closing')) return;
  budgetPop.classList.add('bp-closing');
  setTimeout(() => {
    budgetPop.classList.remove('bp-closing');
    budgetPop.hidden = true;
  }, 150);                              // = длительность npOutUp (закрытие чуть быстрее)
}
function openBudgetPop() {
  budgetPop.classList.remove('bp-closing');
  if (budgetPop.hidden) renderBudgetVariants();
  budgetPop.hidden = false;
}

function setBudget(value) {
  S.budgetRub = value;
  budgetBtn.classList.toggle('on', !!value);
  hideBudgetPop();
  rememberBudget(value);
  // звук — как у агента: монета «завелась» высоким тоном, ручное снятие — низким
  beep(value ? 760 : 420, 0.1);
  // Идёт ответ — лимит меняется НА ЛЕТУ: сервер применит его к текущему
  // прогону с учётом уже потраченного. «Отключить» тоже работает сразу.
  if (S.streaming && S.chatId) {
    api('/api/budget', { chat_id: S.chatId, budget_rub: value || 0 });
    if (value) toast('Лимит ' + value + ' ₽ применён к текущему ответу', 'success', 'Лимит');
    else toast('Лимит снят — текущий ответ идёт без ограничений', 'info', 'Лимит');
    return;
  }
  if (value) toast('Лимит ' + value + ' ₽ на ответ включён', 'success', 'Лимит');
  else toast('Лимит снят', 'info', 'Лимит');
}

budgetBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  // Во время ответа клик по активной монетке НЕ выключает лимит молча —
  // открываем окошко: можно поднять, можно отключить осознанно.
  if (S.budgetRub && !S.streaming) { setBudget(null); return; }
  if (budgetPop.hidden) openBudgetPop(); else hideBudgetPop();
});
document.addEventListener('click', (e) => {
  if (budgetPop && !budgetPop.hidden && !budgetPop.contains(e.target) && e.target !== budgetBtn) {
    hideBudgetPop();
  }
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && budgetPop && !budgetPop.hidden) hideBudgetPop();
});
const budgetOffBtn = $('#budgetOff');
if (budgetOffBtn) budgetOffBtn.addEventListener('click', () => setBudget(null));
/* Свои стрелки суммы вместо браузерных: − и + в стиле Джарвиса. Нативный
   спиннер number-инпута выглядел чужеродно на тёмной панели. */
const budgetInput = $('#budgetInput');
function budgetStepValue(delta) {
  const cur = parseFloat(String(budgetInput.value).replace(',', '.')) || 0;
  const next = Math.max(1, Math.round(cur + delta));
  budgetInput.value = String(next);
}
if (budgetInput) {
  budgetInput.addEventListener('input', () => {
    budgetInput.value = budgetInput.value.replace(/[^0-9]/g, '').slice(0, 6);
  });
  budgetInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); $('#budgetApply').click(); }
    if (e.key === 'ArrowUp') { e.preventDefault(); budgetStepValue(1); }
    if (e.key === 'ArrowDown') { e.preventDefault(); budgetStepValue(-1); }
  });
}
const bpMinus = $('#bpMinus'), bpPlus = $('#bpPlus');
if (bpMinus) bpMinus.addEventListener('click', () => budgetStepValue(-1));
if (bpPlus) bpPlus.addEventListener('click', () => budgetStepValue(1));
$('#budgetApply').addEventListener('click', () => {
  const v = parseFloat($('#budgetInput').value);
  if (v > 0) setBudget(v); else toast('Введите сумму больше нуля', 'warn');
});

/* Черновик живёт в localStorage: случайная перезагрузка страницы больше не
   съедает недописанное сообщение. Отправка и очистка поля стирают черновик. */
function saveDraft() {
  try { localStorage.setItem('jarvis.draft', $('#input').value); } catch (e) {}
}
function loadDraft() {
  try {
    const d = localStorage.getItem('jarvis.draft');
    if (d) { $('#input').value = d; autoGrow(); updateSendBtn(); }
  } catch (e) {}
}
$('#input').addEventListener('input', saveDraft);
loadDraft();
/* BC: ПРОГРЕВ УБРАН С КНОПКИ ОТПРАВКИ — текст в поле меняется с каждым
   символом, и каждый hover превращался в РЕАЛЬНЫЙ LLM-вызов на той же
   машине: локальный сервер грузил CPU, ввод тормозил. Греем только
   фиксированные тексты — плитки и чипы */
$('#sendBtn').addEventListener('click', () => {
  // стоп — только когда поле пустое; если текст набран, отправляем (прервав старый поток)
  if (S.streaming && !$('#input').value.trim() && !S.attachments.length) {
    // СТОП ВО ВРЕМЯ СЦЕНАРИЯ РВЁТ ВЕСЬ СЦЕНАРИЙ: кнопка шла мимо stopStream,
    // флаг не ставился — и после остановки этапа следующий отправлялся снова
    if (S.scenarioActive) S.abortedScenario = true;
    stopRunForReal();
    if (S.abort) S.abort.abort();
    setStreaming(false);
    // печать обрывается МГНЕННО: буфер приравнивается к показанному,
    // тайпер больше нечего дописывать
    if (S.followUi) {
      typerStop(S.followUi);
      S.followUi.buffer = S.followUi.shown || '';
      markStopped(S.followUi);
    }
    // ОСТАНОВКА — НЕ ОБРЫВ: несвернувшиеся инструменты и группы сворачиваются
    // своими анимациями, кухня не остаётся раскрытой
    if (S.followUi) flushTools(S.followUi);
    return;
  }
  send();
});

/* Полная остановка прогона: сначала серверная отмена работы (инструменты,
   санкции, computer-use), потом обрыв SSE. Только abort оставлял агент
   работать по-настоящему: он продолжал двигать мышью и спрашивать
   разрешения уже «в никуда». keepalive переживает закрытие страницы. */
function stopRunForReal() {
  const token = S.runToken;
  if (!token) return;
  S.runToken = '';
  try {
    fetch('/api/chat/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_token: token }),
      keepalive: true,
    }).catch(() => {});
  } catch (e) { /* уже остановлен */ }
}

/* Прервать текущий поток и дождаться, пока интерфейс освободится. */
function stopStream() {
  return new Promise((resolve) => {
    if (!S.streaming) { resolve(); return; }
    // СТОП ВО ВРЕМЯ СЦЕНАРИЯ РВЁТ ВЕСЬ СЦЕНАРИЙ: следующий шаг не отправляется
    if (S.scenarioActive) S.abortedScenario = true;
    stopRunForReal();
    try { if (S.abort) S.abort.abort(); } catch (e) { /* уже закрыт */ }
    let waited = 0;
    const t = setInterval(() => {
      waited += 60;
      if (!S.streaming || waited > 1800) {
        clearInterval(t);
        setStreaming(false);
        // печать обрывается мгновенно, затем кухня красиво сворачивается
        if (S.followUi) {
          typerStop(S.followUi);
          S.followUi.buffer = S.followUi.shown || '';
          markStopped(S.followUi);
          flushTools(S.followUi);
        }
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
  // сценарий присылает готовый текст: поле ввода пользователя не трогаем
  const overrideText = typeof opts.text === 'string' ? opts.text.trim() : '';
  if (!overrideText && !input.value.trim() && !S.attachments.length) return;
  // Предыдущий ответ ещё идёт — аккуратно прерываем и только ПОСЛЕ ожидания
  // снимаем новый текст/вложения. Иначе символы, набранные за эти миллисекунды,
  // стирались, а запрос уходил со старой копией поля.
  if (S.streaming) { await stopStream(); }
  if (S.streaming) return;
  const text = overrideText || input.value.trim();
  if (!text && !S.attachments.length) return;
  S.lastPrompt = text;
  // старые варианты ответа относились к прошлой реплике — убираем сразу
  const rb = $('#replyBar');
  if (rb) { rb.hidden = true; rb.innerHTML = ''; }
  S.replyTicket = (S.replyTicket || 0) + 1;   // аннулируем незавершённый заказ подсказок
  foldAllNotes();   // диалог ожил — уведомления уплывают наверх, в ленту

  // Снимок владельца запроса. Пока грузится кадр или идёт SSE, камеру можно
  // закрыть и открыть заново. Динамический msgHost()/activeChatId() тогда уже
  // укажет на НОВУЮ карточку, и поздний ответ старого сеанса способен записать
  // ей чужой chat_id. Каждый запрос навсегда привязан к DOM/context поколения,
  // в котором был отправлен; новый сеанс получает только свои новые сообщения.
  // AC: РАЗГОВОР ВСЕГДА ЖИВЁТ В СВОЁМ ДИАЛОГЕ (kind='voice', вне списка):
  // после закрытия вкладки беседа не остаётся ни в основном диалоге, ни
  // где-либо ещё — перечитать её можно, снова открыв вкладку. Тумблер
  // «Контекст диалога» теперь ТОЛЬКО показывает модели историю текущего
  // диалога (voice_context), не меняя место хранения.
  const requestVoice = !!opts.voice;
  const voiceIsolated = requestVoice;
  const requestCamNode = (!requestVoice && camLive()) ? S.camNode : null;
  const requestHost = requestVoice
    ? ((S.voiceBox && S.voiceBox.querySelector('.voice-transcript')) || stream())
    : ((requestCamNode && requestCamNode.querySelector('.cam-chat')) || stream());
  const requestIsolatedCam = !!(requestCamNode && !S.camLink);
  const requestChatId = requestVoice ? (VOICE.chatId || '')
    : requestIsolatedCam ? (S.camChatId || '') : (S.chatId || '');
  const requestKind = requestVoice ? 'voice' : (requestIsolatedCam ? 'cam' : '');
  // Режимы принадлежат запросу: смена switch во время загрузки кадра не
  // меняет уже начатую задачу задним числом.
  const requestAgentMode = !!S.agentMode;
  const requestComputerUse = !!S.computerUse;

  // Вложения, правка и текст тоже принадлежат этому запросу. Раньше снимок
  // делался ПОСЛЕ await загрузки автокадра: за это время повторное открытие
  // камеры или второй клик Send могли подменить глобальные S.attachments,
  // S.editing и даже стереть уже новый текст из input. Забираем всё синхронно
  // до первой точки ожидания и сразу переводим интерфейс в streaming.
  const atts = S.attachments.slice();
  S.attachments = [];
  renderAttachments();
  const editing = S.editing && S.editing.id ? S.editing : null;
  S.editing = null;
  if (!overrideText) {
    input.value = '';
    autoGrow();
    saveDraft();
    // ручная отправка: копим повторы — на третий предложим сценарий
    maybeOfferScenario(text);
  }

  /* AS: ПЕРВЫЙ ЗАПРОС ДИАЛОГА — пока приветствие на экране, запоминаем,
     где стоят большое ядро и надпись JARVIS: при рождении ответа они
     перелетят на свои места (flyWelcomeInto) */
  const wlReactor = document.querySelector('.welcome .reactor.xl');
  const wlTitle = document.querySelector('.welcome .hello span');
  let welcomeFlight = null;
  if (wlReactor && wlTitle) {
    const cr = wlReactor.getBoundingClientRect();
    const tr = wlTitle.getBoundingClientRect();
    /* AX: ПРИЗРАК-РЕАКТОР — летит ВЕСЬ реактор с кольцами и сбрасывает
       их В ПОЛЁТЕ, превращаясь в круглешок. Надпись — ДВА СЛОЯ: нижний
       градиентный (как в приветствии), верхний цветной; цвет меняется
       В ПОЛЁТЕ, не кадром */
    const gc = el('div', 'fly-ghost ghost-reactor',
      '<i class="gr gr1"></i><i class="gr gr2"></i><i class="gcore"></i>');
    gc.style.cssText = 'left:' + cr.left + 'px;top:' + cr.top + 'px;width:' +
      cr.width + 'px;height:' + cr.height + 'px';
    const gt = el('div', 'fly-ghost ghost-title',
      '<span class="gt-grad">JARVIS</span><span class="gt-solid">JARVIS</span>');
    gt.style.cssText = 'left:' + tr.left + 'px;top:' + tr.top + 'px';
    document.body.appendChild(gc);
    document.body.appendChild(gt);
    wlReactor.style.visibility = 'hidden';
    wlTitle.style.visibility = 'hidden';
    welcomeFlight = { core: cr, title: tr, ghostCore: gc, ghostTitle: gt };
    /* AX: УХОД ПРИВЕТСТВИЯ — плитки разъезжаются в стороны с растворением,
       страница мягко тает. Всё — одной длительностью с полётом (WELCOME_MS) */
    welcomeExit();
    /* страховка: отправка сорвалась до рождения ответа — призраки
       не должны висеть вечно */
    setTimeout(() => {
      if (gc.isConnected && !gc.dataset.landed) gc.remove();
      if (gt.isConnected && !gt.dataset.landed) gt.remove();
    }, 4000);
  }

  // правка: подменяем текст на месте и убираем устаревший ответ ниже
  let userMsgNode = null;
  if (editing && editing.node && editing.node.isConnected) {
    userMsgNode = editing.node;
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
  } else if (opts.scenario && opts.scenarioStep) {
    // СЦЕНАРИЙ — РАЗГОВОР ДЖАРВИСА, а не переписка с ним. Шаги не рисуются
    // сообщениями пользователя: вместо реплики — тонкая строка этапа.
    // Ответы печатаются сплошным полотном, этапы отделяются друг от друга.
    const stage = el('div', 'sc-stage');
    stage.innerHTML =
      '<i>' + opts.scenarioStep + '</i>' +
      '<span class="sc-stage-line"></span>' +
      '<b class="sc-stage-text">' + esc(text) + '</b>';
    requestHost.appendChild(stage);
  } else if (!opts.silent) {
    userMsgNode = addUserMsg(text, atts, null, requestHost);
  }

  // ПРОДОЛЖЕНИЕ ОТВЕТА: ответ на интерактивную панель — не новый ответ, а
  // продолжение того же. Карточка, время и имя остаются прежними; печать
  // продолжается ниже тонкого разделителя. Пользователь читает ОДИН ответ.
  let node = null;
  const contUi = (opts.continue && S.lastUi && S.lastUi.node &&
                  S.lastUi.node.root && S.lastUi.node.root.isConnected &&
                  requestHost && requestHost.contains(S.lastUi.node.root) &&
                  !requestIsolatedCam) ? S.lastUi : null;
  if (contUi) {
    // полностью единый ответ: та же карточка, никакого разделителя
    node = contUi.node;
  } else if (opts.continueOf && requestHost && !requestIsolatedCam) {
    // AA: ПАНЕЛЬ ОТВЕЧЕНА ПОСЛЕ ПЕРЕРИСОВКИ ЛЕНТЫ (возврат в диалог, фоновый
    // ответ, перезагрузка) — живой ноды уже нет, но сообщение на месте.
    // Продолжаем ИМЕННО его: тот же ответ дописывается ниже, а не рождается
    // новая карточка «ответ на ответ».
    const root = $$('.msg', requestHost)
      .find((m) => m.dataset && m.dataset.msgId === opts.continueOf);
    // AD: НОВЫЙ ОТВЕТ ВСЕГДА ВНИЗУ. Продолжать можно ТОЛЬКО последнее
    // сообщение ленты: клик по старой панели (выше поздних ответов) раньше
    // дописывал СТАРОЕ сообщение — ответ рождался над свежими репликами.
    const allMsgs = $$('.msg', requestHost);
    if (root && root.querySelector('.ai-content') && allMsgs[allMsgs.length - 1] === root) {
      node = { root, body: root.querySelector('.ai-content'),
               modelEl: root.querySelector('.ai-model') };
    }
  }
  if (!node) {
    node = addAiMsg(null, requestHost);
  }
  // AB: ГОЛОСОВОЙ ОТВЕТ НЕВИДИМ ВСЮ БЕСЕДУ — мы слышим друг друга, текста
  // на экране нет. Проявится, когда разговор будет завершён вручную.
  if (requestVoice && node && node.root) {
    node.root.classList.add('voice-run');
    VOICE.nodes.push(node.root);
  }
  /* AS: рождение ответа — если это первый запрос диалога, ядро и имя
     прилетают из приветствия и становятся круглешком и заголовком */
  flyWelcomeInto(node, welcomeFlight);
  /* AY: круглешок живёт С ПЕРВОГО кадра ответа и до конца — мысли,
     инструменты, печать; в финале finishLiveDot вернёт ему кружок.
     AZ: между делом он изредка играет с формой */
  if (node && node.root) {
    node.root.classList.add('live');
    dotShapePlay(node.root);
  }
  const runId = ++S.streamRun;
  // Уникальный токен прогона: по нему сервер гасит РАБОТУ при Stop
  // (инструменты, санкции, computer-use), а не только SSE-соединение.
  const runToken = 'r_' + Date.now().toString(36) + '_' +
    Math.random().toString(36).slice(2, 10);
  S.runToken = runToken;
  setStreaming(true);
  sfx('send');

  // блоки, которые появляются по ходу
  const ui = {
    node,
    runId,
    userMsgNode,
    cameraNode: requestCamNode,
    chatId: requestChatId,
    isolatedCamera: requestIsolatedCam,
    voiceIsolated: voiceIsolated,
    statusEl: null,
    thinkCard: null,
    planCard: null,
    planItems: [],
    planList: null,
    planStep: 1,
    planTimers: [],
    planDock: null,
    planFinished: false,
    planGate: false,
    planDeferred: [],
    planIntroPromise: null,
    planPaintQ: [],
    planPainted: 0,
    planPaintPending: null,
    verbose: true,
    mdEl: null,
    buffer: '',
    shown: '',
    typer: null,
    onTyped: null,
    tools: {},
    silent: {},
    files: [],
    doneReceived: false,
    visualDone: false,
    routeEl: null,
    routeTier: '',
    routeReason: '',
    modelName: '',
    pendingReplyUi: '',
    agentMode: requestAgentMode,
    followOutput: true,
  };
  S.followUi = ui;
  /* BI: followUi гаснет каждый раз, когда печать осушается (пауза сети),
     и уведомление о режиме, включённом в этот момент, улетало в конец
     ленты. liveUi держит прогон до НАСТОЯЩЕГО конца ответа */
  S.liveUi = ui;
  watchRunFollow(ui);
  // AB: РЕЕСТР ЖИВЫХ ПРОГОНОВ. Уходя из диалога, пользователь НЕ отменяет
  // ответ — узел продолжает печататься в отцеплённом DOM. Раньше при
  // возврате лента перерисовывалась с нуля и живой ответ терялся (оставалась
  // заглушка). Теперь прогон зарегистрирован по диалогу: openChat вернёт
  // его узел на экран, и печать пойдёт как никуда и не уходила.
  if (!requestIsolatedCam && !voiceIsolated) {
    S.liveRuns = S.liveRuns || {};
    if (requestChatId) S.liveRuns[requestChatId] = ui;
  }
  // Сетевой SSE может закрыться раньше, чем локальный typer покажет последний
  // символ. finally ждёт именно эту границу, а не состояние сокета.
  ui.visualDonePromise = new Promise((resolve) => { ui.resolveVisualDone = resolve; });
  ui.statusEl = el('div', 'thinking-line');
  node.body.appendChild(ui.statusEl);
  thinkMode(ui, 'Соединяюсь');
  // Z: СТРАЖ СТРОКИ СОСТОЯНИЯ. Каким бы путём ни пошёл ответ (план, мысли,
  // инструменты, гонки таймеров), на экране ВСЕГДА есть живая строка с
  // курсором. Пропала — через 1.6с она возвращается сама с «думаю…».
  // AA: СТРАЖ БОЛЬШЕ НЕ СЛЕПНЕТ. Раньше он отключался на весь план
  // (ui.planDock) и на весь остаток ответа после первого текста (mdEl):
  // длинная генерация вызова инструмента или медленный инструмент без
  // событий оставляли экран БЕЗ курсора до следующего хода — «снова
  // пропал». Теперь план наверху — не замена строке в теле ответа, а
  // «текст уже есть» значит «текст ПЕЧАТАЕТСЯ прямо сейчас»: допечатан
  // и поток молчит — строка возвращается.
  const statusWatch = setInterval(() => {
    if (S.streamRun !== runId || !S.streaming) return;
    if (ui.doneReceived) return;                     // ответ уже дописан — не воскресать
    const sbody = ui.node && ui.node.body;
    if (!sbody) return;
    // AC: принадлежность телу ответа, а не isConnected — узел может быть
    // честно отцеплен от экрана, пока пользователь смотрит другой диалог
    if (ui.mdEl && ui.typer && sbody.contains(ui.mdEl)) return;   // печать идёт — курсор в тексте
    if (ui.statusEl && sbody.contains(ui.statusEl)) return;
    ui.statusEl = ensureStatus(ui);
    if (ui.statusEl) {
      ui.statusEl._watchLine = true;
      busyMode(ui, ['думаю…', 'готовлю ответ', 'ещё секунду'], 1500);
    }
  }, 1600);

  const controller = new AbortController();
  S.abort = controller;
  try {
    // Камера включена — молча прикладываем снимок именно к локальному atts.
    // Upload слушает тот же AbortController, что и SSE: Stop во время медленной
    // загрузки не может через секунду самовольно запустить уже отменённый ответ.
    if (S.camStream && !atts.some((a) => a.fromCam)) {
      const frame = await camAttachFrame(requestChatId, controller.signal);
      if (frame) { frame.fromCam = true; atts.push(frame); }
    }
    if (controller.signal.aborted || S.streamRun !== runId) {
      const aborted = new Error('Запрос остановлен');
      aborted.name = 'AbortError';
      throw aborted;
    }

    const res = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: controller.signal,
      body: JSON.stringify({
        chat_id: requestChatId, kind: requestKind, text,
        run_token: runToken,
        budget_rub: S.budgetRub || 0,
        edit_of: editing ? editing.id : '',
        continue_of: opts.continueOf || '',
        voice: requestVoice,
        voice_context: (requestVoice && VOICE.ctxOn && S.chatId) || '',
        camera_on: camLive(),
        agent_mode: requestAgentMode,
        computer_use: requestComputerUse,
        silent: !!opts.silent,
        attachments: atts,
      }),
    });
    if (!res.ok) throw new Error('Сервер ответил HTTP ' + res.status);
    if (!res.body) throw new Error('Сервер не открыл поток ответа');
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    const dispatchSse = (part) => {
      const line = part.split(/\r?\n/).find((l) => l.startsWith('data:'));
      if (!line) return;
      let ev; try { ev = JSON.parse(line.slice(5).trim()); } catch (e) { return; }
      // Z: ГОЛОСОВОЙ РЕЖИМ слушает поток ответа — Джарвис говорит
      // предложениями, не дожидаясь конца генерации
      if (opts.onDelta && ev.type === 'delta') opts.onDelta(ev.text || '');
      if (opts.onDone && ev.type === 'done') opts.onDone(ev.content || '');
      // ОДНО БИТОЕ СОБЫТИЕ НЕ УБИВАЕТ ПОТОК (X): раньше исключение в любом
      // обработчике рвало весь цикл чтения — курсор замирал, ответ
      // «зависал». Плохое событие уходит в консоль, печать живёт дальше.
      try { dispatchStreamEvent(ev, ui); }
      catch (e) { try { console.error('SSE handler:', e); } catch (e2) {} }
    };
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split(/\r?\n\r?\n/);
      buf = parts.pop();
      parts.forEach(dispatchSse);
    }
    // TextDecoder и последний SSE-record могут иметь хвост без завершающей
    // пустой строки. Потеря именно этого record раньше оставляла done/end за
    // бортом и заставляла UI завершать удачный ответ как сетевой обрыв.
    buf += dec.decode();
    if (buf.trim()) buf.split(/\r?\n\r?\n/).forEach(dispatchSse);
  } catch (e) {
    if (e.name === 'AbortError') cancelPlanGate(ui);
    else await waitForPlanGate(ui);
    if (e.name !== 'AbortError') {
      // Если полный done уже пришёл, последующий сетевой EOF не отменяет факт:
      // спокойно даём локальной печати закончиться и не рисуем ложную ошибку.
      if (!ui.doneReceived) {
        showError(ui, String(e.message || e));
        queueResponseFinish(ui, ui.buffer, false);
      }
    } else {
      // Явная остановка — единственный случай, когда пользователь сам просит
      // оборвать анимацию. Здесь обещание обязательно разрешаем, иначе finally
      // навсегда останется ждать callback остановленного тайпера.
      typerStop(ui);
      if (ui.mdEl) {
        ui.mdEl.classList.remove('typing');
        const full = String(ui.shown || ui.buffer || '');
        if (ui.marksEl && ui.frozen && ui.freezeLocked) {
          /* BK: метка стоит МЕЖДУ замороженной головой и хвостом —
             пересборка ответа не имеет права её терять */
          ui.mdEl.innerHTML = '';
          const fr = el('div', 'md-frozen');
          fr.innerHTML = ui.frozen.html;
          const tl = el('div', 'md-tail');
          tl.innerHTML = MD.render(stripSteps(full.slice(ui.frozen.src.length)));
          ui.mdEl.appendChild(fr);
          ui.mdEl.appendChild(ui.marksEl);
          ui.mdEl.appendChild(tl);
          ui.marksEl.style.display = '';
        } else {
          ui.mdEl.innerHTML = MD.render(stripSteps(full));
          if (ui.marksEl && !ui.mdEl.contains(ui.marksEl) &&
              ui.marksEl.parentNode !== ui.node.body) ui.mdEl.appendChild(ui.marksEl);
          if (ui.marksEl) ui.marksEl.style.display = '';
        }
        fixTables(ui.mdEl);
      }
      dropStatus(ui);
      discardPlan(ui);
      // ПРЕРВАННЫЙ ОТВЕТ НЕ ОСТАВЛЯЕТ ОТКРЫТЫХ ПАНЕЛЕЙ: код-блоки сворачиваются,
      // ход мыслей складывается в строку — иначе после Stop ответ выглядел
      // «недозакрытым»: развёрнутый код и раскрытые карточки висели открытыми.
      foldCodeBlocks(ui.mdEl, true);
      if (ui.thinkCard && ui.thinkCard.isConnected) {
        if (ui.thinkCard.classList.contains('qt-think')) {
          const mk = ui.thinkCard.querySelector('.qt-mark');
          if (mk) mk.textContent = '✓';
          qtMiniaturize(ui.thinkCard);
        } else {
          const ts = thinkFlush(ui.thinkCard);
          collapseSoon(ui.thinkCard, {
            cls: 'th-think', icon: ICO.think, title: 'Ход мыслей',
            sub: ts ? fmtSize((ts.textContent || '').length) : '',
            tag: 'развернуть',
          });
        }
      }
      markStopped(ui);
      settleVisualDone(ui);
    }
  } finally {
    clearInterval(statusWatch);
    // AB: прогон завершён — убираем из реестра живых, чтобы возврат в диалог
    // больше не пытался прикрепить дохлый узел
    if (S.liveRuns) {
      Object.keys(S.liveRuns).forEach((k) => {
        if (S.liveRuns[k] === ui) delete S.liveRuns[k];
      });
    }
    // SSE часто успевается закрыться, пока вступительный план ещё летит. Ждём gate,
    // иначе fallback-finish сам начал бы ответ раньше завершения перелёта.
    await waitForPlanGate(ui);
    // Сокет — не источник истины для кнопки Stop и звука. Если сервер закрылся
    // без end/done, всё равно сначала допечатываем уже полученный хвост.
    if (!ui.doneReceived && !ui.visualDone) queueResponseFinish(ui, ui.buffer, false);
    await ui.visualDonePromise;
    dropStatus(ui);
    // Ушли в другой диалог и уже запустили новый ответ: поздний finally старого
    // прогона не должен выключать его Stop и обнулять его AbortController.
    if (S.streamRun === runId) {
      setStreaming(false);
      if (S.abort === controller) S.abort = null;
      if (S.runToken === runToken) S.runToken = '';
      // Лимит ₽ живёт ровно один ответ: ответ дописан — монетка гаснет сама
      if (S.budgetRub) {
        S.budgetRub = null;
        if (budgetBtn) budgetBtn.classList.remove('on');
      }
      // недавний ответ можно продолжить после ui-панели (opts.continue)
      if (!requestIsolatedCam && node.root && node.root.isConnected) {
        S.lastUi = { node };
      }
    }
  }
  loadChats();
  // Поздний background-прогон не заказывает подсказки для чужого активного
  // диалога и не обновляет его состояние посреди нового ответа.
  /* BF/BG: подсказки пишутся после КАЖДОГО ответа. Заказ живёт, пока
     это последний прогон и пользователь в ЭТОМ диалоге — живая нода
     не обязательна (лента могла перерисоваться), пустой chat_id нового
     диалога подхватывается из S.chatId (SSE-событие chat уже прилетело).
     НОВЫЙ ответ уже печатается — заказ не нужен, его сделает свой finally */
  const chat = ui.chatId || S.chatId;
  if (!(S.streaming && S.streamRun !== runId) && chat && chat === activeChatId()) {
    refreshState();
    fetchReplies();   // подсказки — уже после того, как ответ закрыт
  }
}

/* Варианты продолжения тянем отдельным запросом. Пока их считают, полоса
   показывает мерцающие заглушки: пусто было бы похоже на «ничего не будет». */
async function fetchReplies() {
  const box = $('#replyBar');
  /* BG: во время ответа подсказок не показываем — только после его
     конца (поздний заказ прошлого прогона не всплывёт поверх печати) */
  if (S.streaming) return;
  const chat = activeChatId();
  if (!box || !chat) return;
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
    if (S.replyTicket !== ticket) return;
    if (activeChatId() !== chat) { showReplies([]); return; }
    showReplies(r.items || []);
  } catch (e) {
    if (S.replyTicket === ticket) showReplies([]);
  } finally {
    clearTimeout(bail);
  }
}

/* ============ состояние «думаю» и остальные статусы ============
   Пока JARVIS молчит перед первым словом, мигает тот же курсор, который затем
   поедет вместе с текстом. Инструменты, шаги плана и ожидание используют его
   же: единый индикатор не меняет форму и не может «застыть кружком» между фазами.
   Справа живёт мелкая подпись; если фраз несколько, они сменяют друг друга. */
const THINK_QUIPS = [
  'думаю', 'взвешиваю', 'соображаю', 'подбираю слова', 'листаю память',
  'связываю мысли', 'проверяю себя', 'ищу формулировку', 'считаю варианты',
  'собираю ответ', 'сверяюсь с фактами', 'почти нашёл', 'ещё секунду',
  'кручу шестерёнки', 'раскладываю по полочкам', 'прикидываю', 'уточняю детали',
];

/* ПОЧЕМУ БЕГУЩИЙ ТЕКСТ БЫЛ НЕ ВЕЗДЕ. Строк состояния было две разных:
   thinkMode — живая, с подменой подписей, и busyMode — одна застывшая
   надпись рядом с крутилкой. Всё, что не «думаю» (инструмент, ожидание,
   шаг плана), попадало во вторую и замирало. Дело не в оформлении, а в
   том, что механизм смены текста существовал только у одного состояния.
   Теперь механизм ОДИН: runStatus крутит любой набор строк, а слева всегда
   живёт мигающий курсор. Круглый spinner исключён из жизненного цикла ответа:
   при долгом ожидании и при «уменьшить движение» он выглядел зависшим. */
function runStatus(ui, lines, opts) {
  // Y: СТРОКА СОСТОЯНИЯ ВОЗРОЖДАЕТСЯ САМА. В многошаговом ответе её
  // разбирали после текста шага, а следующий ход модели («status»/
  // «thinking») приходил к ПУСТОМУ ui.statusEl — runStatus молча
  // выходил, и экран оставался без курсора. Вот он, «исчезающий
  // курсор в агенте». Теперь строка создаётся здесь же, на месте.
  if (!ui) return;
  // isConnected === false (не undefined): в живом DOM оторванная строка
  // обязана возродиться сразу здесь, на месте
  if (!ui.statusEl || ui.statusEl.isConnected === false) {
    if (!ui.node || !ui.node.body) return;   // некуда ставить — тихо выходим
    ui.statusEl = ensureStatus(ui);
  }
  const box = ui.statusEl;
  if (!box) return;
  const o = opts || {};
  const list = (Array.isArray(lines) ? lines : [lines]).filter(Boolean);
  if (!list.length) return;
  const mode = o.caret ? 'think-wait' : 'work-wait';
  const every = o.every || 2100;
  const signature = mode + '|' + every + '|' + list.join('\u241f');

  // tool_partial может прислать одну и ту же подсказку десятки раз. Раньше
  // каждый chunk заново создавал caret, сбрасывал его animation и толкал
  // строку — отсюда дёрганье «Готовлю инструмент». Одинаковое состояние
  // теперь идемпотентно, а сам caret живёт одним DOM-узлом до конца фазы.
  if (box._statusSignature === signature &&
      (list.length < 2 || ui.quipTimer)) return;
  box._statusSignature = signature;
  stopQuips(ui);
  box.className = 'thinking-line ' + mode;
  let caret = box.querySelector('.tw-caret');
  let q = box.querySelector('.tw-quip');
  if (!caret || !q) {
    box.innerHTML = '<span class="tw-caret"></span><span class="tw-quip"></span>';
    caret = box.querySelector('.tw-caret');
    q = box.querySelector('.tw-quip');
    syncCursorPhase(caret);
  }
  const swap = (txt) => {
    if (q.textContent === txt) return;
    q.textContent = txt;
    // Только мягкий compositor fade; геометрия caret не меняется.
    if (q.animate) {
      q.getAnimations().forEach((a) => a.cancel());
      q.animate([
        { opacity: .62, transform: 'translate3d(-2px,0,0)' },
        { opacity: 1, transform: 'translate3d(0,0,0)' },
      ], { duration: 360, easing: 'cubic-bezier(.16,1,.3,1)' });
    }
  };
  swap(list[0]);
  if (list.length < 2) return;
  let i = 0;
  ui.quipTimer = setInterval(() => {
    i = o.shuffle ? (i + 1 + Math.floor(Math.random() * (list.length - 1))) % list.length
                  : (i + 1) % list.length;
    swap(list[i]);
  }, every);
}

/* Фразы по ТЕМЕ инструмента. Ключ — группа из реестра (web, sandbox,
   computer, media, memory, auto, base): она приходит с сервера и её набор
   закрытый. Списка имён инструментов здесь по-прежнему нет — он открытый
   и устарел бы с первым же новым инструментом, а группа у нового найдётся
   всегда. Если группа незнакомая, берём base — строка не пропадёт. */
const GROUP_QUIPS = {
  web:      ['выхожу в сеть', 'открываю страницы', 'читаю источники',
             'сверяю факты', 'отбираю главное'],
  sandbox:  ['работаю с файлами', 'открываю песочницу', 'считаю',
             'проверяю результат', 'складываю в папку'],
  computer: ['беру управление', 'смотрю на экран', 'веду курсор',
             'нажимаю', 'проверяю, что вышло'],
  media:    ['работаю с медиа', 'разглядываю кадр', 'рисую',
             'слушаю дорожку', 'собираю результат'],
  memory:   ['лезу в память', 'вспоминаю', 'сверяюсь с записями',
             'запоминаю на будущее'],
  auto:     ['ставлю в расписание', 'завожу будильник', 'проверяю время'],
  base:     ['работаю', 'уточняю', 'собираю данные', 'ещё секунду'],
};
function groupQuips(group) {
  return GROUP_QUIPS[group] || GROUP_QUIPS.base;
}

/* Строки состояния для конкретного вызова инструмента. Источник правды —
   само событие: человеческий label приходит с сервера, вторая строка — то,
   с чем инструмент реально работает (запрос, путь, адрес), а дальше идут
   фразы по теме, чтобы строка жила, пока инструмент думает. */
function toolTicker(ev) {
  const label = ev.label || ev.name || 'работаю';
  const lines = [label];
  const args = ev.args || {};
  let best = '';
  Object.keys(args).forEach((k) => {
    const v = args[k];
    if (typeof v !== 'string' && typeof v !== 'number') return;
    const t = String(v).trim();
    if (t && t.length <= 90 && t.length > best.length) best = t;
  });
  if (best) lines.push('· ' + best);
  return lines.concat(groupQuips(ev.group));
}

/* Семейства инструментов: череда однотипных действий — ОДНА история.
   Пять поисков подряд это одна работа «исследую интернет», а не пять
   одинаковых строк. Источник правды о теме — группа из реестра. */
const QT_FAMILY = {
  web: { label: 'Интернет', ico: 'globe' },
  sandbox: { label: 'Код и файлы', ico: 'term' },
  memory: { label: 'Память', ico: 'brain' },
  computer: { label: 'Компьютер', ico: 'cursor' },
  media: { label: 'Медиа', ico: 'eye' },
  auto: { label: 'Задачи', ico: 'cloud' },
  base: { label: 'Работа', ico: 'spark' },
};

/* Конкретный инструмент — своя смысловая иконка: по ней виден ТИП действия
   (глобус — искал, окно — открыл страницу, глаз — посмотрел на экран). */
const QT_TOOL_ICO = {
  web_search: 'globe', open_url: 'win', http_request: 'cloud',
  read_file: 'doc', write_file: 'docpen', list_files: 'doc',
  run_python: 'term', run_command: 'term',
  screenshot: 'eye', ui_tree: 'eye', screen_info: 'eye',
  mouse_click: 'cursor', mouse_move: 'cursor', scroll: 'cursor',
  drag: 'cursor', type_text: 'cursor', press_key: 'cursor',
  open_app: 'cursor',
  remember: 'brain', recall: 'brain', forget: 'brain',
  generate_image: 'spark',
};

function qtFamilyIcon(group, name) {
  return ICO[QT_TOOL_ICO[name] || (QT_FAMILY[group] || {}).ico || 'gear'] || ICO.gear;
}

/* Компактная расшифровка вызова для раскрываемой строки следа: аргументы и
   результат — то, что интересно при желании заглянуть, но не нужно на бегу. */
function qtDetail(args, result) {
  const parts = [];
  const a = args || {};
  Object.keys(a).slice(0, 4).forEach((k) => {
    const v = String(a[k] == null ? '' : a[k]).trim();
    if (v) parts.push(k + ': ' + (v.length > 160 ? v.slice(0, 160) + '…' : v));
  });
  let out = parts.join('\n');
  const text = toolResultText(result || {});
  if (text) out += (out ? '\n' : '') + (text.length > 600 ? text.slice(0, 600) + '…' : text);
  return out || '(пусто)';
}

/* ======================= КУХНЯ ИНСТРУМЕНТОВ =======================
   Один спокойный серый цвет на всё — чуть ярче подписи «обычный запрос».
   РАБОТАЮЩИЙ инструмент: имя, от него вниз тонкая полоса, справа от неё —
   поток строк: вальяжно плывут вверх, растворяясь в темноте сверху и снизу.
   Закончил — галочка и время встают У ИМЕНИ, и инструмент залезает в папку
   своего семейства, растворяясь в ней. Папка открывается — от иконки вниз
   идёт полоса, инструменты выплывают по одному; клик по инструменту
   разворачивает его текст (полоса от иконки, без затемнений, прокрутка
   только вниз). Обычный режим и AGENT живут на одной кухне. */
/* Строка состояния обязана жить, когда приходит инструмент: после начала
   текста ответа её разбирали (delta → dropStatus), и поздний tool_start
   падал на statusEl.parentNode — «Cannot read properties of null». */
function ensureStatus(ui) {
  // AC: КОРЕНЬ «МНОЖЕСТВО ДУМАЮЩИХ КУРСОРОВ». Проверка была по isConnected —
  // а пока узел ответа живёт в отцепленном DOM (пользователь в другом
  // диалоге), isConnected ЛОЖНО и для живой строки: каждый статусный вызов
  // прилетавшего события создавал НОВУЮ «думаю…» строку, старые оставались
  // детьми узла. Вернулся — стопка курсоров. Существование = быть ребёнком
  // тела ответа, подключён узел к экрану или нет.
  if (ui && ui.statusEl && ui.node && ui.node.body &&
      ui.node.body.contains(ui.statusEl)) return ui.statusEl;
  if (!ui || !ui.node || !ui.node.body) return null;
  ui.statusEl = el('div', 'thinking-line');
  ui.node.body.appendChild(ui.statusEl);
  return ui.statusEl;
}

/* AC: ПОТОК МЫСЛЕЙ — НЕПРЕРЫВНЫЙ ТЕКСТ. Прежний путь кормил каждый delta
   отдельной строкой потока: рассуждающие модели шлют мысль мелкими кусками,
   и живой ответ выглядел «слово — перенос — слово». Теперь куски копятся в
   буфер и укладываются строками по границам слов (как в истории): мысль
   читается связным текстом, «бегущий» вид потока сохраняется. */
function qtThinkFeed(flow, text) {
  const inner = flow.querySelector('.qt-flowin');
  if (!inner) return;
  flow._buf = (flow._buf || '') + String(text || '');
  // AD: модель шлёт мысль кусками и СКЛЕИВАЕТ предложения без пробела
  // («web_search.Need news») — читалось как недописанный текст. Ставим
  // пробел после точки/вопроса, если дальше идёт заглавная буква.
  flow._buf = flow._buf.replace(/([.!\u2026!?])(?=[A-Z\u0410-\u042f\u0401])/g, '$1 ');
  // AN: цепочки многоточий — ГРАНИЦА мысли, сами точки убираем совсем
  flow._buf = flow._buf.replace(/(?:\s*\.\s*){2,}/g, ' ').replace(/\s*\u2026\s*/g, ' ');
  const born = (line) => setTimeout(() => line.classList.remove('qt-wait'), 230);
  for (;;) {
    const rest = flow._buf;
    if (rest.length <= 96 && rest.indexOf('\n') < 0) break;
    let cut = rest.indexOf('\n');
    if (cut < 0 || cut > 96) {
      // AD: ЗАВЕРШЁННОЕ ПРЕДЛОЖЕНИЕ — лучшая граница строки: мысль
      // читается по предложениям, а не произвольными кусками по 96 знаков
      let se = -1;
      const upto = Math.min(96, rest.length - 1);
      for (let i = 0; i <= upto; i++) {
        const c = rest[i];
        if (c === '.' || c === '!' || c === '?' || c === '\u2026') {
          const nx = rest[i + 1];
          if (nx === undefined || nx === ' ' || nx === '\n') se = i + 1;
        }
      }
      if (se > 20) cut = se;
      else {
        const sp = rest.lastIndexOf(' ', 96);
        cut = sp > 40 ? sp : 96;
      }
    }
    let line = inner.lastElementChild;
    if (!line || line._sealed) {
      line = el('div', 'qt-flowline qt-wait', '');
      inner.appendChild(line);
      born(line);
    }
    line.textContent = rest.slice(0, cut).trim();
    line._sealed = true;
    flow._buf = rest.slice(cut + 1).replace(/^\s+/, '');
  }
  if (flow._buf) {
    let live = inner.lastElementChild;
    if (!live || live._sealed) {
      live = el('div', 'qt-flowline qt-wait', '');
      inner.appendChild(live);
      born(live);
    }
    live.textContent = flow._buf;
  }
  if (!flow.classList.contains('full')) {
    const h = Math.min(inner.scrollHeight, 88);
    if (h > (flow._h || 0)) { flow._h = h; flow.style.height = h + 'px'; }
    if (inner.scrollHeight > 92) {
      flow.classList.add('full');
      flow._h = 88;
      flow.style.height = '88px';
      glideFlow(flow, inner);
    }
  }
  if (flow._ui) scrollSoon(flow._ui);
}

function qtFeed(flow, text) {
  // строки живут на ВНУТРЕННЕМ слое: он и сдвигается при полёте, а внешнее
  // окно просто обрезает — поток никогда не наезжает на заголовок
  const inner = flow.querySelector('.qt-flowin');
  if (!inner) return;
  const line = el('div', 'qt-flowline qt-wait', esc(String(text || '')));
  inner.appendChild(line);
  // ФАЗА 1 — НАПОЛНЕНИЕ: высота окна едет плавным переходом (полоса под
  // текстом тянется без рывков), ничего не движется. ФАЗА 2 — ПОЛЁТ:
  // окно заполнилось, включаем затемнение краёв и текст пролетает вверх
  if (!flow.classList.contains('full')) {
    const h = Math.min(inner.scrollHeight, 88);
    if (h > (flow._h || 0)) {
      flow._h = h;
      flow.style.height = h + 'px';
    }
    if (inner.scrollHeight > 88 + 4) {
      flow.classList.add('full');
      flow._h = 88;
      flow.style.height = '88px';
      glideFlow(flow, inner);
    }
  }
  // страница прилипает к растущему инструменту: текст не пишется за экраном
  if (flow._ui) scrollSoon(flow._ui);
  // ПОЛОСА УДЛИНЯЕТСЯ ПЕРВОЙ, СТРОКА ПИШЕТСЯ ПОСЛЕ: полоса тянется под
  // ещё невидимой строкой, и лишь доехав — отпускает строку наружу.
  // Так окно растёт строго по мере текста, без прыжка «полная длина сразу»
  setTimeout(() => line.classList.remove('qt-wait'), 230);
}

/* ПОЛЁТ ПОТОКА. Внутренний слой плавно уезжает вверх ровно на высоту
   первой строки, затем она удаляется и transform сбрасывается в тот же
   кадр — сдвига не видно, а верхняя строка растворилась в затемнении. */
function glideFlow(flow, inner) {
  if (flow._glide) return;
  flow._glide = true;
  const step = () => {
    const first = inner.firstElementChild;
    if (!first) { flow._glide = false; return; }
    const h = first.offsetHeight + 6;
    inner.style.transition = 'transform .5s cubic-bezier(.3,.7,.3,1)';
    void inner.offsetHeight;
    inner.style.transform = 'translateY(' + (-h) + 'px)';
    setTimeout(() => {
      inner.style.transition = 'none';
      if (first.parentNode === inner) first.remove();
      inner.style.transform = 'none';
      // поток всё ещё переполнен — продолжаем лететь без паузы
      // (сравнение с ПОТОЛКОМ окна, а не с clientHeight: высота едет
      // переходом и на старте отстаёт — было бы ложное «не переполнен»)
      if (inner.scrollHeight > 88 + 4) step();
      else { flow._glide = false; flow.classList.remove('full'); }
    }, 520);
  };
  step();
}

function qtOpen(ui, ev) {
  const node = el('div', 'qt-node');
  node.innerHTML =
    '<div class="qt-head">' +
      '<span class="qt-ico">' + qtFamilyIcon(ev.group, ev.name) + '</span>' +
      '<span class="qt-name">' + esc(ev.label || ev.name) + '</span>' +
      '<span class="qt-mark"></span>' +
    '</div>' +
    '<div class="qt-body"><span class="qt-rail"></span><div class="qt-flow">' +
      '<div class="qt-flowin"></div></div></div>';
  node._tool = { id: ev.id || ev.name, name: ev.name, label: ev.label || ev.name,
                 group: ev.group || 'base', args: ev.args || {} };
  const st = ensureStatus(ui);
  (st && st.parentNode ? st.parentNode : ui.node.body).insertBefore(node, st || null);
  ui.tools[ev.id || ev.name] = node;
  // ПОТОК РАБОТЫ: первым делом — главный аргумент (запрос/путь/адрес):
  // имя инструмента и так стоит заголовком, дублировать его в потоке
  // бессмысленно. Дальше — фразы темы. Строки приходят чаще: поток живой.
  const flow = node.querySelector('.qt-flow');
  flow._ui = ui;                    // поток растёт — страница едет за ним
  const lines = toolTicker(ev).slice(1);
  const quips = groupQuips(ev.group);
  let li = 0;
  qtFeed(flow, lines[li++]);
  const tick = () => {
    if (!node.isConnected || node._done) return;
    qtFeed(flow, li < lines.length ? lines[li++] : quips[(li++) % quips.length]);
    node._t = setTimeout(tick, 1150);
  };
  node._t = setTimeout(tick, 1150);
  scrollSoon(ui);
  return node;
}

/* МАССА РЕЗУЛЬТАТА — В ПОТОКЕ СРАЗУ. Раньше во время работы в инструменте
   было видно пару строк, а «масса информации» всплывала только при клике
   после ответа. Теперь результат вываливается в поток тем же живым полётом:
   первая строка с галочкой, затем до шести строк настоящего вывода. */
function qtResultLine(node, ev) {
  const flow = node && node.querySelector('.qt-flow');
  if (!flow) return;
  const ok = !(ev && ev.result && ev.result.ok === false);
  const raw = String(toolResultText((ev && ev.result) || '') || '');
  const lines = raw.split('\n').map((x) => x.trim()).filter(Boolean);
  qtFeed(flow, (ok ? '✓ ' : '✕ ') + (lines[0] || 'готово'));
  const extra = lines.slice(1, 7);
  extra.forEach((x, i) => {
    setTimeout(() => {
      if (node.isConnected && !node._done) return;
      qtFeed(flow, x.length > 96 ? x.slice(0, 96) + '…' : x);
    }, 120 + i * 170);
  });
  // сколько будет литься масса — миниатюра ждёт ЭТО плюс ~1 секунду
  return 120 + extra.length * 170;
}

/* МИНИАТЮРА: инструмент закончил — тело (поток) прячется, остаётся строка
   «иконка + имя + галочка + время». В папку семейство уезжает ПОЗЖЕ —
   когда череда инструментов этого типа закончилась. */
function qtMiniaturize(node) {
  if (!node || !node.isConnected || node.dataset.mini === '1' || node.dataset.folded === '1') return;
  node.dataset.mini = '1';
  node._thinkOpen = false;   // AA: свернули — состояние клика тоже сбросили
  const body = node.querySelector('.qt-body');
  if (!body) return;
  // AC: НИКАКОГО display:none. Прежний финальный кадр гасил элемент целиком —
  // и qtToggleThink при повторном открытии читал высоту 0 («мысль не
  // открывается»), а в момент гашения геометрия прыгала. Закрытое состояние —
  // всегда просто height:0 + opacity:0, тело остаётся в потоке.
  body.style.overflow = 'hidden';
  const h = body.getBoundingClientRect().height;
  if (h > 0) {
    body.style.transition = 'none';
    body.style.height = h + 'px';
    void body.offsetHeight;
    body.style.transition = 'height .5s cubic-bezier(.25,.6,.3,1), opacity .36s ease';
  } else {
    body.style.transition = 'none';
  }
  body.style.height = '0px';
  body.style.opacity = '0';
}

/* AA: ТИХИЙ ХОД МЫСЛЕЙ ОТКРЫВАЕТСЯ КЛИКОМ. Когда пошёл текст ответа, поток
   мыслей сворачивается в одну строку «Ход мыслей ✓» — и раньше это было
   навсегда: у строки был курсор-указатель, но клик ничего не делал. Теперь
   клик раскрывает поток обратно, повторный — закрывает. История ведёт себя
   так же: строки мыслей hydrated в поток заранее и спрятаны.
   AB: КОРЕНЬ ПОДЛАГИВАНИЯ ПОСЛЕДНЕГО КАДРА — display-переключение.
   Прежний код в конце анимации гасил элемент целиком, и вместе с ним
   исчезали вертикальные поля: всё снизу прыгало на 6px в один кадр —
   тот же класс бага, что в папках (X). Теперь тело НИКОГДА не выключается
   display-ом: закрытое состояние — это просто height:0 (+overflow hidden),
   геометрия вокруг не меняется ни в одном кадре. */
function qtToggleThink(row) {
  const body = row.querySelector('.qt-body');
  if (!body) return;
  if (row._thinkOpen) {
    row._thinkOpen = false;
    const h = body.getBoundingClientRect().height;
    body.style.transition = 'none';
    body.style.height = Math.max(h, 0) + 'px';
    void body.offsetHeight;
    body.style.transition = 'height .44s cubic-bezier(.3,.6,.3,1), opacity .3s ease';
    body.style.height = '0px';
    body.style.opacity = '0';
  } else {
    row._thinkOpen = true;
    // высоту окну даёт сам поток: живой qtFeed уже вырастил его, история
    // гидрируется заранее; страховка — на случай совсем пустого потока
    const flow = row.querySelector('.qt-flow');
    if (flow && !flow.style.height) {
      const inner = flow.querySelector('.qt-flowin');
      const fh = Math.min(inner ? inner.scrollHeight : 0, 88);
      flow.style.height = Math.max(fh, 0) + 'px';
      if (inner && inner.scrollHeight > 92) flow.classList.add('full');
    }
    body.style.transition = 'none';
    body.style.height = 'auto';
    const h = body.getBoundingClientRect().height;
    body.style.height = '0px';
    body.style.opacity = '0';
    void body.offsetHeight;
    body.style.transition = 'height .44s cubic-bezier(.22,.8,.3,1), opacity .34s ease';
    body.style.height = Math.max(h, 0) + 'px';
    body.style.opacity = '1';
    // вернулись к естественной высоте: поздние мысли смогут дописываться
    setTimeout(() => {
      if (row._thinkOpen && body.style.height !== '0px') body.style.height = 'auto';
    }, 470);
  }
}

/* ЧЕРЕДА ЗАКОНЧИЛАСЬ: инструменты другого типа или текст ответа означают,
   что прошлое семейство своё отработало — его миниатюры складываются в
   папку, одна за другой, той же классной анимацией. */
function qtSweep(ui, keepGroup) {
  const st = ui && ui.statusEl;
  const host = (st && st.parentNode) || (ui && ui.node ? ui.node.body : null);
  if (!host) return 0;
  let i = 0;
  let folded = 0;
  const pending = [];
  $$('.qt-node', host).forEach((nn) => {
    if (nn.dataset.folded === '1') return;
    // Z: СТРОКА ХОДА МЫСЛЕЙ — НЕ ИНСТРУМЕНТ: в папку не собирается.
    // Раньше она попадала в семейство «Работа» как безымянный инструмент
    // с непонятной иконкой.
    if (nn.classList.contains('qt-think')) return;
    const t = nn._tool || {};
    if (keepGroup && t.group === keepGroup) return;
    if (!nn.dataset.mini) {
      if (!nn._done) return;          // ещё работает — не трогаем
      qtMark(nn, t.ok, t.elapsed);
      // БЕЗ предварительной миниатюры: раньше полоса закрывалась (.5с),
      // и только потом инструмент летел в папку — два такта вместо одного.
      // Теперь qtFold закрывает полосу И летит ОДНОВРЕМЕННО.
    }
    folded++;
    pending.push([nn, i++ * 170]);
  });
  if (pending.length === 1) {
    // ОДИНОЧКЕ ПАПКА НЕ НУЖНА (X): папка из одного инструмента — лишний
    // клик без смысла. Он остаётся своей строкой; тело уже закрыла
    // миниатюра, а если череда оборвалась раньше — закрываем сейчас.
    const solo = pending[0][0];
    if (!solo.dataset.mini) qtMiniaturize(solo);
    return 0;
  }
  pending.forEach(([nn, delay], idx) => {
    setTimeout(() => qtFold(ui, nn, idx === pending.length - 1), delay);
  });
  return folded;
}

function qtMark(node, ok, sec) {
  const m = node && node.querySelector('.qt-mark');
  if (m) m.textContent = (ok === false ? '✕' : '✓') + (sec != null ? ' ' + sec + 'с' : '');
}

function qtFolder(ui, group) {
  if (!ui.qtFolders) ui.qtFolders = {};
  let f = ui.qtFolders[group];
  if (f && f.isConnected) return f;
  f = el('div', 'qt-folder');
  f.dataset.group = group;
  f._items = [];
  f.innerHTML =
    '<div class="qt-head">' +
      '<span class="qt-ico">' + qtFamilyIcon(group, '') + '</span>' +
      '<span class="qt-name"></span>' +
      '<span class="qt-mark"></span>' +
    '</div>' +
    '<div class="qt-kids"><span class="qt-rail"></span><div class="qt-rows"></div></div>';
  f.querySelector('.qt-head').addEventListener('click', () => qtToggleFolder(f));
  ui.qtFolders[group] = f;
  return f;
}

function qtFolderSync(folder) {
  const items = folder._items || [];
  const fam = QT_FAMILY[folder.dataset.group] || QT_FAMILY.base;
  const total = items.reduce((acc, x) => acc + (x.elapsed || 0), 0);
  const anyFail = items.some((x) => x.ok === false);
  folder.querySelector('.qt-name').textContent =
    fam.label + (items.length > 1 ? ' × ' + items.length : '');
  folder.querySelector('.qt-mark').textContent =
    (anyFail ? '✕' : '✓') + (total ? ' ' + total.toFixed(1).replace(/\.0$/, '') + 'с' : '');
}

function qtFold(ui, node, isLast) {
  if (!node || !node.isConnected || node.dataset.folded === '1') return;
  node.dataset.folded = '1';
  node._done = true;
  clearTimeout(node._t);
  const t = node._tool || {};
  const folder = qtFolder(ui, t.group || 'base');
  const fresh = !folder.isConnected;
  if (fresh && node.parentNode) node.parentNode.insertBefore(folder, node);
  folder._items.push({ name: t.name, label: t.label, group: t.group, args: t.args,
                       ok: t.ok, elapsed: t.elapsed, result: t.result });
  qtFolderSync(folder);
  // СТАРАЯ АНИМАЦИЯ, НО С ТОЧНОЙ ПОСАДКОЙ: узел остаётся в потоке и сжимается
  // по высоте (как раньше), а СМЕЩЕНИЕ ПОЛЁТА пересчитывается КАЖДЫЙ КАДР —
  // вальс сжимает соседей сверху, и заранее рассчитанный пролёт перелетал
  // группу. Прицеливание по живому низу головы папки: посадка ВСЕГДА под ней.
  const h0 = node.getBoundingClientRect().height;
  // Y: ПОТОК СКЛАДЫВАЕТСЯ БЫСТРО И ПЕРВЫМ. Раньше высота узла резала
  // подпись ещё в полёте — текст обрезался задолго до названия группы
  // и «растворялся» ниже него. Поток строк уходит за .3с, а строка-
  // заголовок остаётся читаемой почти до самой посадки в название.
  const bodyEl = node.querySelector('.qt-body');
  if (bodyEl && !node.dataset.mini) {
    const bh = bodyEl.getBoundingClientRect().height;
    bodyEl.style.overflow = 'hidden';
    bodyEl.style.transition = 'none';
    bodyEl.style.height = bh + 'px';
    void bodyEl.offsetHeight;
    bodyEl.style.transition = 'height .3s ease, opacity .22s ease';
    bodyEl.style.height = '0px';
    bodyEl.style.opacity = '0';
  }
  node.style.overflow = 'hidden';
  node.style.transition = 'none';
  node.style.height = h0 + 'px';
  void node.offsetHeight;
  // Z: ВЫСОТА СХЛОПЫВАЕТСЯ ПОЗДНО И БЫСТРО. Раньше она резала подпись с
  // самого начала полёта — текст обрезался на полпути и «таял ниже
  // названия». Теперь подпись целиком доживает до самой папки.
  // AG: ЗАТЕМНЕНИЕ ЖИВЁТ В САМОМ ПОЛЁТЕ. Раньше это был CSS-переход с
  // задержкой ВДОГОНКУ WAAPI-полёту — и в живом ответе (лента едет, поток
  // дышит) переход терялся: инструменты залетали в группу читаемыми и
  // сливались с названием. Ключи opacity/filter теперь ВНУТРИ той же
  // WAAPI-анимации, что и transform: затемнение физически не может
  // потеряться — оно и есть полёт. С 45% пути строка гаснет и к посадке
  // почти чёрная (brightness .08).
  node.style.transition = 'height .42s cubic-bezier(.4,.6,.3,1) .58s';
  node.style.height = '0px';
  const t0 = performance.now();
  const DUR = 1050;
  const ease = (k) => (k < .5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2);
  // ПОЛЁТ ЧЕРЕЗ WAAPI: у анимации Web Animations приоритет выше ЛЮБЫХ
  // CSS-переходов и анимаций — ничто больше не может заглушить трансформ
  // (inline-transform в прошлых версиях местами перекрывался стилями, и
  // инструмент таял на месте, ниже группы). ЦЕЛЬ — ЦЕНТР строки приходит
  // В ЦЕНТР головы папки: инструмент визуально входит В саму группу.
  // Лента вальса поднимается (соседи сверху сжимаются) — конец ключа
  // ПЕРЕЦЕЛИВАЕТСЯ каждый кадр: посадка всегда в голову, что бы ни делал
  // макет вокруг.
  const fr0 = folder.getBoundingClientRect();
  const base0 = node.getBoundingClientRect().top;
  // Z: ПОСАДКА ТОЧНАЯ ПО ПОСТРОЕНИЮ — без поправочных констант. Прицел:
  // ЦЕНТР ПОДПИСИ узла в ЦЕНТР НАЗВАНИЯ папки. Полёт — чистый сдвиг:
  // прежнее лёгкое сжатие карточки (до 93%) тащило подпись к центру узла
  // на ПЕРЕМЕННУЮ величину (высота-то анимируется) — вот почему текст
  // годами «таял ниже названия», сколько его ни поднимали.
  const titleEl = folder.querySelector('.qt-head .qt-name') || folder.querySelector('.qt-head') || folder;
  const labEl = node.querySelector('.qt-head .qt-name');
  const lRect0 = labEl ? labEl.getBoundingClientRect() : node.getBoundingClientRect();
  const labC = (lRect0.top + lRect0.height / 2) - base0;   // подпись живёт на фикс. смещении от верха узла
  const titleC = () => {
    const r = titleEl.getBoundingClientRect();
    return r.top + r.height / 2;
  };
  let aim = titleC() - labC - base0;
  const flyKeys = (target) => [
    { transform: 'translateY(0px)', opacity: 1, filter: 'blur(0px) brightness(1)' },
    // AI: ЧИТАЕМАЯ ДО САМОГО ПОДЛЁТА. У промежуточного кадра СвОЙ easing —
    // медленный старт и крутой финиш: весь спад яркости умещается в
    // последние ~25% пути. Раньше спад растягивался на весь хвост полёта
    // с быстрым началом — строка терялась ещё в воздухе и выглядела как
    // «летит мимо» группы. Теперь она полная почти весь путь и ныряет
    // под название уже чёрным силуэтом — в последние мгновения.
    { opacity: 1, filter: 'blur(.5px) brightness(.94)', offset: .55,
      easing: 'cubic-bezier(.62,.04,.6,.55)' },
    { transform: 'translateY(' + target + 'px)', opacity: 0,
      filter: 'blur(4px) brightness(.08)' }];
  const flight = node.animate(
    flyKeys(aim),
    { duration: DUR, easing: 'cubic-bezier(.2,.5,.2,1)', fill: 'forwards' });
  const homing = () => {
    // прогресс — у самой анимации; уравнение решается каждый кадр по
    // живым координатам: aim = название − подпись − база узла
    let pr = 0;
    try { pr = flight.effect.getComputedTiming().progress || 0; }
    catch (err) { pr = Math.min(1, (performance.now() - t0) / DUR); }
    const nr = node.getBoundingClientRect();
    const baseTop = nr.top - pr * aim;      // где узел стоит без трансформа
    const need = titleC() - labC - baseTop;
    if (Math.abs(need - aim) > 0.5) {
      aim = need;
      // перенацеливание несёт ТЕ ЖЕ ключи затемнения — иначе setKeyframes
      // стёр бы их и полёт продолжился бы без затемнения
      flight.effect.setKeyframes(flyKeys(aim));
    }
    if (pr < 1) requestAnimationFrame(homing);
  };
  requestAnimationFrame(homing);
  folder._pend = (folder._pend || 0) + 1;
  const folderBlink = () => {
    if (!folder.isConnected) return;
    folder.classList.remove('blink');
    void folder.offsetWidth;
    folder.classList.add('blink');
  };
  setTimeout(() => {
    node.remove();
    folder._pend -= 1;
  }, 1200);   // Y: растворение доигрывает до конца (fade .84с+.3с)
  // ПОСЛЕДНИЙ ИНСТРУМЕНТ ЕЩЁ ЛЕТИТ — папка уже «охнула»: мигание начинается
  // чуть раньше прилёта, впихивание последнего читается живым
  if (isLast) setTimeout(folderBlink, 820);
}

/* РУЧНОЕ ОТКРЫТИЕ/ЗАКРЫТИЕ ПАПКИ — ОДНА АНИМАЦИЯ В ДВЕ СТОРОНЫ.
   Строки выплывают из-под головы группы одна за другой. Вертикальная
   линия НЕ живёт своей жизнью: её высота на каждом кадре = СРЕДНИЙ
   прогресс прилёта строк — линия движется строго с инструментами, не
   раньше и не позже. Закрытие проигрывает ТЕ ЖЕ анимации задом наперёд
   (reverse): нижняя строка уходит первой, линия задвигается зеркально.
   Отличается только направление, не рисунок. */
function qtToggleFolder(f) {
  const kids = f.querySelector('.qt-kids');
  if (!kids || f._anim) return;      // анимация играет — клики не рвут её
  const wasOpen = f.classList.contains('open');
  const head = f.querySelector('.qt-head');
  if (!wasOpen) {
    const box = f.querySelector('.qt-rows');
    box.replaceChildren();
    (f._items || []).forEach((t) => box.appendChild(qtRow(t)));
    kids.classList.add('open');
    f.classList.add('open');
  }
  const rows = Array.from(f.querySelectorAll('.qt-row'));
  if (!rows.length) return;
  f._anim = true;
  // геометрия снимается при открытом теле: путь каждой строки до головы
  const headBottom = head.getBoundingClientRect().bottom;
  const H = kids.getBoundingClientRect().height;
  // ВЫСОТА ТЕЛА — СИНХРОННО С КЛИКОМ, до первого кадра: раньше она ставилась
  // только в первом rAF-тике, и браузер успевал показать тело полной высоты
  // на один кадр — тот самый «микролюфт» в начале открытия (и в конце
  // закрытия, когда авто-высота мигала перед уборкой).
  kids.style.overflow = 'hidden';
  kids.style.height = (wasOpen ? H : 0) + 'px';
  const STEP = 55, FLY = 500, LEAD = 70;   // X: было 120 — клик и первый инструмент разделяла заметная пауза
  // ЗАКРЫТИЕ = ОТКРЫТИЕ НАОБОРОТ, кадр в кадр: те же кривые и стаггер,
  // только кадры развёрнуты (строка уезжает ПОД голову, растворяясь) и
  // очередь обращена — первой уходит НИЖНЯЯ строка. Никакого «проиграть
  // задом наперёд»: только те же длительности, только зеркальный рисунок.
  const anims = rows.map((r, i) => {
    const dist = Math.max(4, r.getBoundingClientRect().top - headBottom);
    const wait = wasOpen ? (rows.length - 1 - i) : i;   // закрытие: снизу вверх
    // ТОЛЬКО transform и opacity: анимация blur заставляла браузер
    // ПЕРЕРИСОВЫВАТЬ размытие текста каждый кадр — отсюда подлагивание
    // закрытия. Композиторные свойства идут на GPU без единой перерисовки.
    const frames = wasOpen
      ? [{ transform: 'translateY(0) scale(1)', opacity: '1' },
         { transform: 'translateY(' + (-dist) + 'px) scale(.93)', opacity: '0' }]
      : [{ transform: 'translateY(' + (-dist) + 'px) scale(.93)', opacity: '0' },
         { transform: 'translateY(0) scale(1)', opacity: '1' }];
    return r.animate(frames,
      { duration: FLY, delay: LEAD + wait * STEP,
        easing: 'cubic-bezier(.2,.5,.2,1)', fill: 'both' });
  });
  // ЛИНИЯ = СРЕДНИЙ ПРОГРЕСС СТРОК: при открытии растёт от нуля к полной
  // ровно с прилётом строк; при закрытии тем же темпом съёживается к нулю.
  // Не раньше инструментов и не позже — всегда их средний такт.
  const dir = wasOpen ? -1 : 1;    // закрытие: прогресс 1 = строка ушла
  let raf = 0;
  const tick = () => {
    let sum = 0;
    anims.forEach((a) => {
      const pr = a.effect.getComputedTiming().progress;
      sum += pr == null ? 0 : pr;
    });
    const avg = sum / anims.length;
    kids.style.height = Math.max(0, Math.min(H, H * (dir < 0 ? 1 - avg : avg))) + 'px';
    if (anims.some((a) => a.playState === 'running')) {
      raf = requestAnimationFrame(tick);
    } else {
      kids.style.height = (dir < 0 ? 0 : H) + 'px';
    }
  };
  raf = requestAnimationFrame(tick);
  Promise.all(anims.map((a) => a.finished)).then(() => {
    cancelAnimationFrame(raf);
    // СНАЧАЛА прячем тело (display:none), и только потом cancel: cancel
    // снимает fill-конец анимаций, и строки успевали вспыхнуть видимыми
    // на последний кадр — «закрытие подлагивает».
    if (wasOpen) {
      f.classList.remove('open');
      kids.classList.remove('open');
    }
    anims.forEach((a) => { try { a.cancel(); } catch (err) { /* уже мертва */ } });
    kids.style.cssText = '';
    rows.forEach((r) => { r.style.cssText = ''; });
    f._anim = false;
  }).catch(() => {
    cancelAnimationFrame(raf);
    if (wasOpen) {
      f.classList.remove('open');
      kids.classList.remove('open');
    }
    anims.forEach((a) => { try { a.cancel(); } catch (err) { /* уже мертва */ } });
    kids.style.cssText = '';
    rows.forEach((r) => { r.style.cssText = ''; });
    f._anim = false;
  });
}

function qtRow(t) {
  const row = el('div', 'qt-row');
  row.innerHTML =
    '<div class="qt-head">' +
      '<span class="qt-ico">' + qtFamilyIcon(t.group, t.name) + '</span>' +
      '<span class="qt-name">' + esc(t.label || t.name) + '</span>' +
      '<span class="qt-mark">' + (t.ok === false ? '✕' : '✓') + (t.elapsed != null ? ' ' + t.elapsed + 'с' : '') + '</span>' +
    '</div>' +
    '<div class="qt-detail-wrap"><span class="qt-rail"></span>' +
      '<pre class="qt-detail">' + esc(qtDetail(t.args, t.result)) + '</pre></div>';
  row.querySelector('.qt-head').addEventListener('click', (e) => {
    e.stopPropagation();
    qtToggleDetail(row);
  });
  return row;
}

function qtToggleDetail(row) {
  const w = row.querySelector('.qt-detail-wrap');
  if (!w) return;
  if (w.classList.contains('open')) {
    const h = w.getBoundingClientRect().height;
    w.style.overflow = 'hidden';
    w.style.transition = 'none';
    w.style.height = h + 'px';
    void w.offsetHeight;
    w.style.transition = 'height .44s cubic-bezier(.3,.6,.3,1), opacity .3s ease';
    w.style.height = '0px';
    w.style.opacity = '0';
    setTimeout(() => { w.classList.remove('open'); w.style.cssText = ''; }, 460);
  } else {
    w.classList.add('open');
    const h = w.getBoundingClientRect().height;
    w.style.overflow = 'hidden';
    w.style.transition = 'none';
    w.style.height = '0px';
    w.style.opacity = '0';
    void w.offsetHeight;
    w.style.transition = 'height .44s cubic-bezier(.22,.8,.3,1), opacity .34s ease';
    w.style.height = h + 'px';
    w.style.opacity = '1';
    setTimeout(() => { w.style.cssText = ''; }, 470);
  }
}

/* Конец работы/ответа: всё открытое немедленно прячется в папки */
function flushQt(ui) {
  const st = ui && ui.statusEl;
  const host = (st && st.parentNode) || (ui && ui.node ? ui.node.body : null);
  if (!host) return;
  const pending = [];
  $$('.qt-node', host).forEach((n) => {
    if (n.dataset.folded === '1') return;
    // Z: ход мыслей — не инструмент, в папку не собирается
    if (n.classList.contains('qt-think')) return;
    const t = n._tool || {};
    if (!n.querySelector('.qt-mark').textContent) qtMark(n, t.ok, t.elapsed);
    pending.push(n);
  });
  pending.forEach((n, idx) => {
    setTimeout(() => qtFold(ui, n, idx === pending.length - 1), idx * 170);
  });
}

/* Единая точка финала: сворачивается и серая кухня, и агентская группа —
   режим мог переключиться посреди ответа, открытым не должно остаться ничто */
function flushTools(ui) {
  flushQt(ui);
  flushAgentGroup(ui);
}

/* registry-marked ожидание держит контур человечески различимый срок даже
   при мгновенном cache hit: считаем от первого paint, а не от результата */
const TOOL_WAIT_AFTER_PAINT_MS = 800;

function finishToolWait(card) {
  if (!card || !card.classList.contains('tool-wait')) return;
  const finish = () => {
    if (!card.isConnected) return;
    const painted = Number(card.dataset.waitPainted || 0);
    if (!painted) {
      requestAnimationFrame(finish);
      return;
    }
    const left = TOOL_WAIT_AFTER_PAINT_MS - (performance.now() - painted);
    if (left > 0) setTimeout(finish, left);
    else card.classList.remove('tool-wait');
  };
  requestAnimationFrame(finish);
}

/* ЧЕРЕДА ОДНОТИПНЫХ КАРТОЧЕК АГЕНТА СКЛАДЫВАЕТСЯ В ОДНУ ГРУППОВУЮ:
   другой тип работы или конец ответа закрывают семью. Одиночная карточка
   живёт сама и сворачивается в миниатюру, как обычная инструментальная. */
function flushAgentGroup(ui) {
  const g = ui && ui.agentGroup;
  ui.agentGroup = null;
  if (!g || !g.cards || !g.cards.length) return;
  const cards = g.cards.filter((c) => c.isConnected);
  if (!cards.length) return;
  const fam = QT_FAMILY[g.group] || QT_FAMILY.base;
  if (cards.length === 1) {
    const only = cards[0];
    const t = only._tool || {};
    collapseSoon(only, {
      cls: t.ok === false ? 'th-no' : 'th-ok', icon: ICO.code,
      title: t.label || '', sub: t.elapsed != null ? t.elapsed + 'с' : '',
      tag: t.ok === false ? 'ошибка' : 'готово',
    });
    return;
  }
  const anchor = cards[0];
  const secs = cards.map((c) => (c._tool && c._tool.elapsed) || 0);
  const totalSec = secs.reduce((a, b) => a + b, 0);
  const anyFail = cards.some((c) => c._tool && c._tool.ok === false);
  const card = makeCard(qtFamilyIcon(g.group, ''), fam.label, 'tool-card ag-group', true);
  card.querySelector('.card-head').insertBefore(el('span', 'tool-run'), card.querySelector('.chev'));
  const head = card.querySelector('.k');
  if (head) head.innerHTML = esc(fam.label) + '<span class="ag-count"> × ' + cards.length + '</span>';
  const rows = el('div', 'ag-rows');
  cards.forEach((c) => {
    const t = c._tool || {};
    const row = el('div', 'ql-row');
    row.innerHTML =
      '<span class="ql-head">' +
        '<span class="qs-ico">' + qtFamilyIcon(t.group, t.name) + '</span>' +
        '<span class="ql-label">' + esc(t.label || t.name) + '</span>' +
        '<span class="qs-state">' + (t.ok === false ? '✕' : '✓') +
          (t.elapsed != null ? ' ' + t.elapsed + 'с' : '') + '</span>' +
      '</span>' +
      '<pre class="ql-detail">' + esc(qtDetail(t.args, t.result)) + '</pre>';
    row.addEventListener('click', () => {
      // пользователь разбирает группу — авто-сворачивание отменяется,
      // группа не «проглатывает» раскрытый инструмент через секунду
      cancelFoldSoon(card);
      // РАСКРЫТИЕ С АНИМАЦИЕЙ — той же, что у обычных инструментов кухни:
      // деталь вырастает по высоте от нуля, а не щёлкает мгновенно
      const det = row.querySelector('.ql-detail');
      const willOpen = !row.classList.contains('open');
      let h = 0;
      if (det) {
        if (willOpen) {
          row.classList.add('open');
          h = det.scrollHeight;
        } else {
          h = det.scrollHeight;
          row.classList.remove('open');
        }
      } else {
        row.classList.toggle('open');
      }
      if (det && h > 0) {
        det.style.overflow = 'hidden';
        const anim = det.animate(
          willOpen
            ? [{ height: '0px', opacity: '0' }, { height: h + 'px', opacity: '1' }]
            : [{ height: h + 'px', opacity: '1' }, { height: '0px', opacity: '0' }],
          { duration: willOpen ? 440 : 380,
            easing: willOpen ? 'cubic-bezier(.22,.8,.3,1)' : 'cubic-bezier(.3,.6,.3,1)' });
        anim.finished.then(() => { det.style.overflow = ''; }).catch(() => {
          det.style.overflow = '';
        });
      }
    });
    rows.appendChild(row);
  });
  card.inner.appendChild(rows);
  if (anchor.parentNode) anchor.parentNode.insertBefore(card, anchor);
  cards.forEach((c) => {
    const t = c._tool || {};
    if (t.id && ui.tools[t.id] === c) delete ui.tools[t.id];
    c.remove();
  });
  markBorn(card);
  const run = card.querySelector('.tool-run');
  if (run) { run.style.animation = 'none'; run.style.background = anyFail ? 'var(--red)' : 'var(--green)'; }
  const k = card.querySelector('.k');
  if (k) k.className = 'k ' + (anyFail ? 'tool-err' : 'tool-ok');
  const tEl = card.querySelector('.t');
  if (tEl && totalSec) tEl.innerHTML += ' <span class="muted" style="font-size:10.5px">· ' +
    totalSec.toFixed(1).replace(/\.0$/, '') + 'с</span>';
  // групповая карточка задерживается на виду, затем сворачивается в миниатюру
  collapseSoon(card, {
    cls: anyFail ? 'th-no' : 'th-ok', icon: qtFamilyIcon(g.group, ''),
    title: fam.label + ' × ' + cards.length,
    sub: totalSec ? totalSec.toFixed(1).replace(/\.0$/, '') + 'с' : '',
    tag: anyFail ? 'есть ошибки' : 'готово',
  });
  scrollSoon(ui);
}

/* Печать «хода мыслей». Раньше текст вставлялся кусками как есть: модель
   отдаёт reasoning пачками по 20-200 символов, и блок дёргался скачками,
   а не печатался. Здесь тот же приём, что и в основном ответе, но БЕЗ пауз
   и крупным шагом — мысли должны пролетать, их не читают вдумчиво.
   Скорость подстраивается под очередь: чем больше не показано, тем крупнее
   шаг, поэтому поток никогда не отстаёт от модели. */
/* Досказать всё немедленно и погасить таймер. Нужно в момент сворачивания
   карточки: иначе таймер тикает на скрытом узле, а в подпись миниатюры
   попадает длина недопечатанного текста. */
/* AN/AO: ЖИВОЙ ФОРМАТ ХОДА МЫСЛЕЙ. Рассуждающая модель думает вслух
   цепочками многоточий («так... значит... делаю...»). По первой правке
   цепочки сжимались в «…» — но сами многоточия ЮЗЕРУ НЕ НУЖНЫ: текст
   обязан читаться нормально. Теперь цепочки точек — просто ГРАНИЦА
   мысли: точки исчезают, каждая мысль и каждое предложение — своя
   строка. Никаких троеточий на экране вообще. Сырой текст модели
   не трогаем: _raw копится как есть, показ всегда строится заново. */
function thinkFormat(raw) {
  const t = String(raw == null ? '' : raw)
    .replace(/\r/g, '')
    .replace(/\u2026/g, '...')
    .replace(/(?:\s*\.\s*){2,}/g, '\n')
    .replace(/([.!?]) +(?=[\u0410-\u042f\u0401A-Z0-9\u00ab\u201c(\u2013\u2014])/g, '$1\n')
    .replace(/[ \t]+/g, ' ')
    .replace(/\n[ \t]+|[ \t]+\n/g, '\n')
    .replace(/\n{2,}/g, '\n')
    .trim();
  /* AP: строки БЕЗ ЕДИНОЙ БУКВЫ — мусор рассуждающей модели
     (", .,.:,. 2 2026.") — не показываем вообще: без слов нет мысли.
     Второй рубеж поверх серверного фильтра: даже если мусор прорвётся,
     на экран он не попадёт */
  return t.split('\n')
    .filter((ln) => /[\u0410-\u042f\u0401\u0430-\u044f\u0451a-zA-Z]/.test(ln))
    .join('\n');
}

function thinkFlush(card) {
  const el = card && card.querySelector('.think-stream');
  if (!el) return null;
  if (el._t) { clearInterval(el._t); el._t = null; }
  if (el._raw != null) el.textContent = thinkFormat(el._raw);
  else if (el._buf != null) el.textContent = el._buf;
  return el;
}

/* Ход мыслей больше НЕ печатается курсором.
   Печать с курсором — это способ подать текст, который читают. Мысли не
   читают: по ним скользят взглядом, чтобы понять, чем занят агент. Поэтому
   здесь текст просто проматывается снизу вверх, а края блока затемнены —
   в фокусе середина. Никакого курсора, никакой посимвольной печати. */
function thinkType(el, chunk) {
  if (!el) return;
  el._raw = (el._raw != null ? el._raw : el.textContent || '') + chunk;
  // AN: текст ставим сразу ЦЕЛИКОМ, но отформатированным (thinkFormat):
  // мысль по строкам, без кашы из точек — поток живой и читаемый.
  // AW: перерисовка коагулируется в кадр (rAF): сотня кусков мыслей
  // между кадрами больше не перформатирует весь текст сотню раз
  if (el._thinkRaf) return;
  el._thinkRaf = requestAnimationFrame(() => {
    el._thinkRaf = 0;
    el.textContent = thinkFormat(el._raw);
    const atEnd = el.scrollHeight - el.scrollTop - el.clientHeight < 90;
    if (atEnd) el.scrollTop = el.scrollHeight;
  });
}


function thinkMode(ui, first) {
  const lines = first ? [first].concat(THINK_QUIPS) : THINK_QUIPS.slice();
  runStatus(ui, lines, { caret: true, shuffle: true });
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

/* Обычный статус: тот же мигающий курсор + текст. Принимает и одну строку,
   и набор — тогда строки сменяют друг друга, как у «думаю». */
function busyMode(ui, text, every) {
  runStatus(ui, text, { caret: false, every: every || 2100 });
}

function setStreaming(on) {
  S.streaming = on;
  updateSendBtn();
  $('#composer').classList.toggle('busy', on);
  reactor(on ? 'busy' : 'idle');
  // Ускорение живёт ровно один ответ: новый ответ начинается своим темпом.
  if (!on) setBoost(false);
  const bb = $('#boostBtn');
  if (bb) { bb.disabled = !on; bb.classList.toggle('live', on); }
}

/* ТУРБО ×3.5: пока идёт печать, рядом с Send доступна кнопка «». Один клик —
   оставшийся текст печатается в три с половиной раза быстрее; при этом
   страница ОБЯЗАНА успевать прокручиваться (шаг печатки остаётся конечным),
   а кнопка светится золотым, как план: внутренний свет + быстрые струйки. */
function setBoost(on) {
  S.turbo = !!on;
  const bb = $('#boostBtn');
  if (bb) bb.classList.toggle('on', S.turbo);
}
if ($('#boostBtn')) {
  $('#boostBtn').addEventListener('click', () => {
    if (!S.streaming) return;
    setBoost(!S.turbo);
    beep(S.turbo ? 920 : 620, 0.07);
  });
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
/* Печать учитывает и вид содержимого, и реальную длину очереди. Фиксированные
   95 зн/с превращали 1000 знаков уже готового ответа в искусственные 10,5 с.
   Но и ступенчатый backlog-режим был плох: текст внезапно «выстреливал».
   Поэтому backlog даёт только плавную ограниченную прибавку, а текущий CPS
   догоняет цель экспоненциально — без смены темпа за один кадр. */
const TYPE_MS = 20;              // не чаще 50 DOM-render/с: кадры остаются анимациям
/* ПОЧЕМУ ЗДЕСЬ СКОРОСТИ В ЗНАКАХ/СЕК, А НЕ «ЗНАКОВ ЗА ТАКТ».
   Раньше шаг был целым числом за такт: 1 в разговоре, 2 после 900 знаков,
   4 после 2000, 13 в коде. Целый шаг — это лестница: минимальная добавка
   уже удваивает скорость, а переход между ветками происходит за один кадр.
   Внутри одного ответа темп прыгал с 90 до 1180 зн/с (в тринадцать раз) на
   каждом ``` и на каждой строке таблицы. Это и есть «то слишком медленно,
   то невероятно быстро» — не два неверных числа, а сама лестница.
   Теперь скорость задаётся в знаках в секунду, накапливается дробно и
   сглаживается, поэтому переходы не видны, а темп ровный. */
const CPS_TALK = 125;            // естественный разговор при короткой очереди
const CPS_TALK_MAX = 245;        // длинный готовый хвост не держит интерфейс
const CPS_CODE = 470;            // код: быстрее прежнего, но страница успевает ехать
const CPS_SMOOTH_MS = 340;        // заметно мягче старых ступеней скорости

function talkTargetCps(left) {
  // До 120 символов темп базовый. Затем непрерывная насыщаемая кривая: даже
  // огромная очередь не пересекает CPS_TALK_MAX и не создаёт «залп» текста.
  const queued = Math.max(0, Number(left || 0) - 120);
  const blend = 1 - Math.exp(-queued / 520);
  return CPS_TALK + (CPS_TALK_MAX - CPS_TALK) * blend;
}

function deferMountedReplyUi(ui) {
  if (!ui || !ui.replyLive || !ui.replyLive.isConnected) return;
  // Поздний delta означает, что прежняя граница ответа была лишь сетевой
  // паузой. Controls снова становятся pending и не обгоняют новый текст.
  ui.pendingReplyUi = ui.replyUiSpec || ui.pendingReplyUi || '';
  ui.replyLive.remove();
  ui.replyLive = null;
}

function flushPendingReplyUi(ui) {
  if (!ui || !ui.pendingReplyUi || ui.shown !== ui.buffer) return false;
  if (!hasMeaningfulUiItems(parseUiSpec(ui.pendingReplyUi))) {
    ui.pendingReplyUi = '';
    ui.replyUiSpec = '';
    return false;
  }
  if (ui.replyLive && ui.replyLive.isConnected) ui.replyLive.remove();
  const live = el('div', 'reply-ui-live');
  const panel = el('div', 'ui-panel');
  panel.dataset.ui = ui.pendingReplyUi;
  live.appendChild(panel);
  if (ui.statusEl && ui.statusEl.parentNode === ui.node.body) {
    ui.node.body.insertBefore(live, ui.statusEl);
  } else {
    ui.node.body.appendChild(live);
  }
  ui.replyLive = live;
  ui.pendingReplyUi = '';
  mountUiPanels(live);
  mountPlotPanels(live);
  scrollDown(false, ui);
  followGrowingPanel(live, 900, ui);
  return true;
}

function typeInto(ui, chunk) {
  const text = String(chunk || '');
  if (!text) return;
  deferMountedReplyUi(ui);
  ui.buffer += text;
  if (ui.shown == null) ui.shown = '';
  typerStart(ui);
}

/* B. Печать с характером: паузы заданы в миллисекундах, а не в ticks.
   Поэтому облегчение кадрового цикла не меняет темп и не создаёт новую
   «медленную» ветку. Заголовки получают ровно те же паузы, что обычный текст. */
const PAUSE_AFTER = { '.': 100, '!': 100, '?': 100, ',': 45, ';': 55,
                      ':': 55, '\n': 65, '—': 45 };

/* Внутри блока кода? Считаем незакрытые ``` в уже показанном тексте.
   Источник истины — сам текст, а не догадка по длине. */
function inCodeBlock(text) {
  let fences = 0, i = 0;
  while ((i = text.indexOf('```', i)) !== -1) { fences++; i += 3; }
  return fences % 2 === 1;
}

/* Строка похожа на таблицу или технический блок? Смотрим не только уже
   показанный prefix, а полную текущую строку в buffer. Иначе каждый новый ряд
   таблицы начинался на разговорной скорости, после первого `|` резко ускорялся
   и снова тормозил на переводе строки. СПИСКИ ЗДЕСЬ БОЛЬШЕ НЕ УЧИТЫВАЮТСЯ:
   пункты «- …» — это живая речь, Джарвис «проговаривает» их вслух, с обычной
   скоростью и микропаузой между пунктами. Быстрыми остаются только таблицы и
   отступ в 4 пробела (код). */
function fastLine(text, at) {
  const pos = at == null ? text.length : Math.max(0, Math.min(text.length, at));
  const start = text.lastIndexOf('\n', Math.max(0, pos - 1)) + 1;
  const foundEnd = text.indexOf('\n', pos);
  const end = foundEnd < 0 ? text.length : foundEnd;
  const line = text.slice(start, end);
  return /^\s*\|/.test(line) || /^ {4}/.test(line);
}

/* У заголовков больше нет отдельного класса скорости: markdown влияет только
   на оформление. Для печати это тот же разговорный текст с тем же CPS. */

/* Инкрементальный рендер печати. Раньше каждый такт (70 раз в секунду)
   перестраивался markdown ВСЕГО ответа: на длинном тексте это O(n²) работы
   в главном потоке — браузеру не оставалось времени на кадры, и анимации
   (крутилка статуса, мигающий курсор, живое ядро) буквально замирали. Это и
   был «виснущий кружочек», а вовсе не ошибка в самой анимации.
   Теперь всё, что дальше последнего завершённого абзаца, уже не изменится:
   его HTML считаем один раз и запоминаем, а пересобираем только хвост. */
/* Отметки [ШАГ N] — служебные: по ним подсвечивается план. Пользователю их
   показывать незачем. */
// Служебные отметки шагов плана: [ШАГ 2] и [ШАГ ГОТОВ]. Их шлёт модель,
// по ним двигается панель плана, но в тексте ответа их быть не должно.
const STEP_MARK = /\[\s*ШАГ\s*(?:\d+|ГОТОВ)\s*\]\s*/g;
function stripSteps(t) { return t.replace(STEP_MARK, ''); }

function renderTyped(ui) {
  if (!ui.mdEl) return;
  const text = ui.shown;
  if (ui.frozen && !text.startsWith(ui.frozen.src)) ui.frozen = null;
  let src = ui.frozen ? ui.frozen.src : '';
  let html = ui.frozen ? ui.frozen.html : '';
  const idx = text.lastIndexOf('\n\n');
  /* BK: метка режима ЖДЁТ конца предложения: как только в печати
     появилась точка после текущей границы — замораживаем ровно на ней */
  if (ui.freezePending && !ui.freezeLocked) {
    const k = lastSentenceEnd(text, src.length);
    if (k > src.length) {
      const head = text.slice(0, k);
      const mathOk = (head.match(/\\\[/g) || []).length ===
                     (head.match(/\\\]/g) || []).length;
      if (!inCodeBlock(head) && mathOk) {
        ui.frozen = { src: head, html: MD.render(stripSteps(head)) };
        ui._frozenSrc = null;
        ui.freezeLocked = true;
        ui.freezePending = false;
        if (ui.marksEl) ui.marksEl.style.display = '';
      }
    }
  }
  /* BI: после уведомления о режиме граница ЗАКРЕПЛЕНА: весь текст после
     метки остаётся хвостом и печатается ПОД ней (метка на месте включения) */
  if (ui.freezeLocked) {
    if (ui.frozen) { src = ui.frozen.src; html = ui.frozen.html; }
  } else if (idx >= 0 && idx + 2 > src.length) {
    const cand = text.slice(0, idx + 2);
    // границу нельзя ставить внутри блока кода — он рендерится целиком
    // BF: и внутри ОТКРЫТОЙ формулы \[ ... \] — математическая панель
    // живёт в хвосте и растёт по мере печати, заморозка её бы порвала
    const mathOpen = (cand.match(/\\\[/g) || []).length !== (cand.match(/\\\]/g) || []).length;
    if (!inCodeBlock(cand) && !mathOpen) {
      src = cand;
      html = MD.render(stripSteps(cand));
      ui.frozen = { src, html };
    }
  }

  // ПОЧЕМУ ЛЕНТА «ЕЗДИЛА» ВВЕРХ-ВНИЗ ВО ВРЕМЯ ПЕЧАТИ.
  // Хвост ответа пересобирается из markdown на каждом такте, а markdown по
  // полтексту разбирается ИНАЧЕ, чем по целому. Пока строка «| Источник |»
  // не дописана, это обычный абзац; допечатали вторую строку — абзац
  // превратился в таблицу. «- пункт» сначала абзац, потом список; три
  // обратные кавычки — сначала текст, потом блок кода. На тестовом ответе я
  // насчитал 10 таких превращений, и каждое меняет высоту скачком, иногда
  // В МЕНЬШУЮ сторону: текст на миг становится короче, лента дёргается вниз,
  // автопрокрутка возвращает её обратно. Отсюда качели.
  // Лечение: во время печати ответ не имеет права становиться ниже, чем
  // только что был. Растёт — пожалуйста, это естественно.
  // Высоту читаем редко. offsetHeight сразу после innerHTML принудительно
  // запускает layout; два таких чтения на каждом кадре и отнимали кадры у
  // cursor/gradient. Раз в 100 мс достаточно, чтобы пресечь markdown-качели.
  const now = performance.now();
  const measure = now - (ui.lastHeightCheck || 0) >= 100;
  const before = measure ? ui.mdEl.offsetHeight : 0;
  // ГОЛОВА И ХВОСТ В РАЗНЫХ УЗЛАХ: замороженная часть не перестраивается —
  // её HTML фиксируется один раз, а свёрнутые вкладки кода (слоты с кнопками
  // и слушателями) стоят в ней неподвижно. Переписывается только хвост.
  let frozenEl = ui.mdEl.firstElementChild;
  if (!frozenEl || !frozenEl.classList.contains('md-frozen')) {
    ui.mdEl.replaceChildren();
    frozenEl = el('div', 'md-frozen');
    ui.mdEl.appendChild(frozenEl);
    ui._frozenSrc = null;
    ui._tailKeys = [];
  }
  /* BH: между головой и хвостом живёт слот меток (.md-marks — уведомления
     о режимах, включённых посреди ответа). Порядок нормализуем только
     когда он реально нарушен — лишний appendChild перезапускал бы анимацию
     входа метки на каждом такте печати.
     BJ: метка, поставленная ДО начала печати, живёт в ТЕЛЕ ответа (перед
     строкой статуса) — её НЕ переносим внутрь печати: текст обязан
     печататься ПОД меткой, а не наоборот */
  if (ui.marksEl && !ui.mdEl.contains(ui.marksEl) &&
      ui.marksEl.parentNode !== ui.node.body) ui.mdEl.appendChild(ui.marksEl);
  let tailEl = frozenEl.nextElementSibling;
  if (tailEl && tailEl.classList.contains('md-marks')) tailEl = tailEl.nextElementSibling;
  if (!tailEl || !tailEl.classList.contains('md-tail')) {
    tailEl = el('div', 'md-tail');
    ui.mdEl.appendChild(tailEl);
  }
  {
    const want = [frozenEl];
    if (ui.marksEl && ui.mdEl.contains(ui.marksEl)) want.push(ui.marksEl);
    want.push(tailEl);
    let ordered = ui.mdEl.children.length === want.length;
    if (ordered) for (let k = 0; k < want.length; k++) {
      if (ui.mdEl.children[k] !== want[k]) { ordered = false; break; }
    }
    if (!ordered) want.forEach((n) => ui.mdEl.appendChild(n));
  }
  if (ui._frozenSrc !== src) {
    frozenEl.innerHTML = html;
    ui._frozenSrc = src;
    // свернуть код головы один раз — и больше не трогать до конца ответа
    refoldCodeBlocks(ui, frozenEl, null, '');
    ui._tailKeys = [];
    /* BI: график в замороженной части оживает СРАЗУ при заморозке —
       раньше панель монтировалась только в конце ответа */
    mountPlotPanels(frozenEl);
  }
  /* BI: хвост перерисовывается каждый такт — смонтированные панели
     (canvas + слушатели) вынимаем ДО innerHTML и ставим на место ПОСЛЕ:
     иначе график то исчезал, то появлялся заново */
  const savedPlots = $$('.plot-panel', tailEl).filter((n) => n._jarvisPlot);
  tailEl.innerHTML = MD.render(stripSteps(text.slice(src.length)));
  const freshPlots = $$('.plot-panel', tailEl);
  freshPlots.forEach((n, i) => {
    if (savedPlots[i]) n.replaceWith(savedPlots[i]);
  });
  mountPlotPanels(tailEl);
  /* BK: график ДЕРЖАЛИ пальцем, пока печать пересобирала хвост — панель
     вернулась та же, но пересборка рвала pointer capture и пан СБРАСЫВАЛСЯ.
     Возвращаем захват тому же указателю — жест живёт дальше */
  savedPlots.forEach((p) => { if (p._recapture) p._recapture(); });
  fixTables(ui.mdEl);
  markImportantThought(ui.mdEl);
  placeCaret(ui.mdEl);
  // Незаконченный длинный code fence живёт в ограниченном окне и следует за
  // собственной нижней строкой. Общую ленту при этом двигает только run intent.
  const typedPres = $$('pre', ui.mdEl);
  const livePre = typedPres.length ? typedPres[typedPres.length - 1] : null;
  typedPres.forEach((pre) => pre.classList.remove('live-code'));
  if (livePre && inCodeBlock(text)) {
    livePre.classList.add('live-code');
    livePre.scrollTop = livePre.scrollHeight;
  }
  // ЗАКРЫВШИЙСЯ FENCE СРАЗУ СТАНОВИТСЯ МИНИАТЮРОЙ-ВКЛАДКОЙ: пишется блок —
  // живое окно live-code; закрылся — вкладка в тот же тик. Перерисовка
  // печати не рождает вкладку заново: готовые слоты встают из кэша.
  refoldCodeBlocks(ui, tailEl, livePre, text);
  // АКТИВНЫЙ fence остаётся живым окном (скроллится сам), закрытые — уже
  // свёрнуты выше. Развёрнутый пользователем блок (миниатюра среди печати)
  // не сжимается снова: индекс блока запоминается в _codePeek.
  if (measure) {
    ui.lastHeightCheck = now;
    const after = ui.mdEl.offsetHeight;
    if (after < before) {
      ui.floor = Math.max(ui.floor || 0, before);
      ui.mdEl.style.minHeight = ui.floor + 'px';
    } else if (ui.floor && after > ui.floor) {
      // ответ перерос прежний пол — подпорка больше не нужна
      ui.floor = 0;
      ui.mdEl.style.minHeight = '';
    }
  }
}

/* Курсор набора.
   Раньше он рисовался через CSS ::after у последнего блока. Пока ответ —
   обычный абзац, всё хорошо. Но стоит последнему блоку оказаться таблицей,
   списком или картинкой, и ::after у блочного элемента встаёт ОТДЕЛЬНОЙ
   строкой под ним: курсор «просто внизу», а текст пишется где-то выше без
   него. Именно это и выглядело как «печатает медленно и без курсора».
   Ставим курсор настоящим узлом внутрь последнего текстового элемента —
   тогда он всегда там же, где последняя буква. */
function clearTypingDecorations(mdEl) {
  if (!mdEl) return;
  const caret = mdEl.querySelector('.caret');
  if (caret) caret.remove();
  const trail = mdEl.querySelector('.typing-trail');
  if (trail && trail.parentNode) {
    trail.parentNode.insertBefore(document.createTextNode(trail.textContent || ''), trail);
    trail.remove();
  }
}

/* Жёлтая искра включается локально, без подсказок модели и токенов. Важность
   определяется структурой ответа: заголовки, явное выделение, рекомендации,
   выводы и риски. У любого достаточно длинного ответа есть ещё один стабильный
   смысловой акцент — текущий блок в момент пересечения порога. Индекс хранится
   на md-контейнере, поэтому он не скачет при каждом markdown-render. */
function markImportantThought(mdEl) {
  if (!mdEl) return;
  const parts = $$('h1,h2,h3,h4,h5,h6,p,li,blockquote', mdEl);
  const last = parts[parts.length - 1];
  if (!last) return;
  const text = (last.textContent || '').trim();
  const index = parts.indexOf(last);
  const fullLength = (mdEl.textContent || '').trim().length;
  const heading = /^H[1-6]$/.test(last.tagName || '');
  const emphasized = !!(last.querySelector && (last.querySelector('strong') || last.querySelector('mark')));
  const semantic = /^(?:⚠\ufe0f?\s*)?(?:важно|главное|критично|обязательно|внимание|осторожно|итог|вывод|результат|рекомендация|совет|что делать|следующий шаг|important|critical)\s*[:—.!]/i.test(text) ||
    /\b(?:необратим\w*|нельзя отменить|без резервной копии|риск\s+(?:потери|утечки|блокировки)|потребуется подтверждение|обратите внимание|не забудьте|лучше всего|рекомендую|стоит\s+(?:сделать|проверить|сохранить)|нужно\s+(?:сделать|проверить|сохранить)|следует\s+(?:сделать|проверить))\b/i.test(text);

  if (!heading && !emphasized && !semantic && fullLength >= 280 && text.length >= 28 &&
      mdEl.dataset.importantBlock == null) {
    mdEl.dataset.importantBlock = String(index);
  }
  const longAnswerAccent = Number(mdEl.dataset.importantBlock) === index;
  last.classList.toggle('action-important', heading || emphasized || semantic || longAnswerAccent);
}

function placeCaret(mdEl) {
  clearTypingDecorations(mdEl);

  // ПОЧЕМУ КУРСОР «ЗАДЕРЖИВАЛСЯ» ПОЗАДИ ТЕКСТА.
  // Спуск шёл по .children, а это ТОЛЬКО элементы — текстовые узлы в список
  // не попадают. В строке «**жирный** и дальше текст» последним элементом
  // абзаца остаётся <strong>, хотя после него идёт ещё полстроки обычного
  // текста. Курсор уезжал внутрь жирного куска и замирал там, пока печаталось
  // продолжение, — со стороны это и выглядит как «отстал». То же самое с
  // ссылками, кодом в строке и курсивом.
  // Правильный ориентир — ПОСЛЕДНИЙ УЗЕЛ (lastChild), а не последний элемент:
  // если строка кончается текстом, курсор place прямо здесь, в конце.
  // PRE имеет собственную зелёную code-caret, IMG/UI не содержат текста.
  // TABLE намеренно НЕ opaque: спускаемся table→tbody→tr→td→text и ставим
  // живую каретку ровно в последнюю печатаемую ячейку.
  const OPAQUE = (n) => n && n.nodeType === 1 && (
    n.tagName === 'PRE' || n.tagName === 'IMG' ||
    n.classList.contains('code-block') || n.classList.contains('ui-panel'));

  let host = mdEl;
  for (;;) {
    const last = host.lastChild;
    if (!last) break;                       // пусто — ставим сюда
    if (last.nodeType === 3) break;         // строка кончается текстом — курсор в конец
    if (last.nodeType !== 1) { break; }
    if (OPAQUE(last)) return;               // код/таблица/картинка рисуют курсор сами
    host = last;                            // спускаемся в последний элемент
  }
  if (OPAQUE(host)) return;
  const c = document.createElement('span');
  c.className = 'caret';

  // Обычный Markdown, заголовки и таблицы всегда используют синий режим.
  // Золото локально доступно только явному <mark>/.action-important фрагменту;
  // факт вызова инструмента больше не перекрашивает весь последующий ответ.
  let p = host;
  let important = false;
  while (p && p !== mdEl) {
    if (p.tagName === 'MARK' || (p.classList && p.classList.contains('action-important'))) {
      important = true; break;
    }
    p = p.parentNode;
  }
  const tail = host.lastChild;
  if (tail && tail.nodeType === 3 && tail.nodeValue) {
    const chars = Array.from(tail.nodeValue);
    const cut = Math.max(0, chars.length - 7);
    tail.nodeValue = chars.slice(0, cut).join('');
    const trail = document.createElement('span');
    trail.className = 'typing-trail ' + (important ? 'important-trail' : 'normal-trail');
    trail.textContent = chars.slice(cut).join('');
    host.appendChild(trail);
  }
  if (important) c.classList.add('caret-important');
  syncCursorPhase(c);
  host.appendChild(c);
}

/* Прокрутка читает геометрию страницы, а чтение сразу после записи HTML
   заставляет браузер пересчитывать разметку внеочередно. Каждый такт это
   недопустимо дорого, поэтому догоняем ленту не чаще ~12 раз в секунду —
   глазу этого хватает с запасом. */
function scrollSoon(ui) {
  const now = performance.now();
  if (now - (ui.lastScroll || 0) < 80) return;
  ui.lastScroll = now;
  scrollDown(false, ui);
}

function typerStart(ui) {
  if (ui.typer) return;
  // печать может быть придержана вальсом свёртки инструментов (конец
  // череды): hold живёт в _qtHold и подхватывается здесь при старте
  ui.holdUntil = ui._qtHold || 0;
  ui._qtHold = 0;
  ui.acc = ui.acc || 0;
  let lastTick = performance.now();
  ui.typer = setInterval(() => {
    const now = performance.now();
    // Пропущенный браузером кадр не превращаем в долг, который затем выдаётся
    // пачкой. Реальное время всё равно прошло; после stall продолжаем тем же
    // ровным темпом вместо визуального «выстрела» на 100–250 мс текста.
    const elapsed = Math.max(1, Math.min(250, now - lastTick));
    lastTick = now;
    const left = ui.buffer.length - ui.shown.length;
    if (left <= 0) {
      clearInterval(ui.typer); ui.typer = null;
      if (ui.mdEl) {
        ui.mdEl.classList.remove('typing');
        /* AQ: осушение потока — НЕ конец: сеть может принести ещё текст,
           и реактор не должен мигать на каждой паузе. Эстафета гасит
           ответный реактор только на НАСТОЯЩЕМ конце (onTyped/typerStop) */
        // Сетевой поток может ненадолго осушиться до следующего chunk. Каретка
        // уже скрылась, значит и цветной trail обязан немедленно стать обычным
        // текстом — последнее слово не остаётся синим/жёлтым в паузе.
        clearTypingDecorations(ui.mdEl);
      }
      flushPendingReplyUi(ui);
      // Поток мог временно осушиться до следующего SSE-chunk. Не переносим
      // скорость предыдущей (например, табличной) строки в новый кусок.
      ui.cps = 0;
      ui.acc = 0;
      if (ui.onTyped) { const cb = ui.onTyped; ui.onTyped = null; cb(); }
      return;
    }
    if (now < (ui.holdUntil || 0)) return;

    const code = inCodeBlock(ui.shown) || fastLine(ui.buffer, ui.shown.length);
    // Markdown-заголовок здесь намеренно не проверяется: у него разговорный
    // темп. Размер очереди меняет только мягкую целевую скорость, не размер
    // очередного DOM-шага и не скорость скачком.
    let want = code ? CPS_CODE : talkTargetCps(left);
    // ТУРБО: кнопка ×2 у поля ввода. Ускоряется всё честно — целевая
    // скорость, предел кадра и паузы препинания. Ответ не «прыгает», а
    // печатается тем же характером, только вдвое быстрее.
    const turbo = S.turbo ? 16 : 1;
    want *= turbo;
    // После done ускоряется ТОЛЬКО плотный контент (код, таблицы, списки):
    // его хвост не должен «досматриваться» минуту. Разговорный текст держит
    // живой темп и паузы препинания до самого конца — скорость печати
    // отвечает за характер ответа, а не за пропускную способность.
    // Большой хвост кода добивается быстрее: чем длиннее очередь, тем выше
    // темп (до 2000 симв/с). Выше нельзя: автопрокрутка перестаёт поспевать,
    // и ответ уезжает вниз без читателя.
    if (code && ui.fastFinish) want = Math.max(want, Math.min(2000, 700 + left * 0.18));
    if (!ui.cps) ui.cps = want;
    const blend = 1 - Math.exp(-elapsed / CPS_SMOOTH_MS);
    ui.cps += (want - ui.cps) * blend;

    ui.acc = (ui.acc || 0) + (ui.cps * elapsed) / 1000;
    let step = Math.floor(ui.acc);
    if (step < 1) return;
    /* AV: ПРЕДЕЛ КАДРА СВЯЗАН СО ВРЕМЕНЕМ КАДРА. Раньше тик давал не
       больше 4 знаков НЕЗАВИСИМО от паузы: дорогой рендер длинного ответа
       или зажатый браузером таймер растягивали тик до 100-250мс — и темп
       падал до 10-20 зн/с («полслова в секунду»). Пропавшее время теперь
       отрабатывается тем же темпом: предел растёт вместе с паузой, а
       накопитель acc не даёт выстрелить быстрее положенного. */
    const baseCap = (code ? (ui.fastFinish ? 26 : 10) : 4) * turbo;
    const frameCap = Math.max(baseCap, Math.ceil((ui.cps * elapsed) / 1000));
    step = Math.min(step, left, frameCap);

    // Не перепрыгиваем через знак препинания пачкой: заканчиваем этот render
    // прямо на нём, а остаток времени переносим на следующий кадр.
    if (!code) {
      const piece = ui.buffer.slice(ui.shown.length, ui.shown.length + step);
      for (let i = 0; i < piece.length; i++) {
        if (PAUSE_AFTER[piece[i]]) { step = i + 1; break; }
      }
    }
    ui.acc = Math.max(0, ui.acc - step);
    ui.shown = ui.buffer.slice(0, ui.shown.length + step);
    if (ui.mdEl) {
      ui.mdEl.classList.add('typing');
      renderTyped(ui);
    }
    if (!code) {
      const pause = PAUSE_AFTER[ui.shown[ui.shown.length - 1]] || 0;
      if (pause) ui.holdUntil = now + (pause / turbo) *
        (CPS_TALK / Math.max(CPS_TALK, ui.cps));
    }
    scrollSoon(ui);
  }, TYPE_MS);
}

/* дописать всё, что осталось (в конце ответа) */
function typerFlush(ui) {
  if (ui.shown == null) ui.shown = '';
  typerStart(ui);
}

/* МИНИАТЮРА КОДА — СРАЗУ ПОСЛЕ ЗАКРЫТИЯ FENCE: вкладка (иконка, язык,
   размер) строится в тот же тик, когда fence закрылся, — не в конце ответа.
   Блок живёт в СЛОТЕ (code-slot): слот кэшируется по содержимому, и когда
   печать перерисовывает ленту, готовый слот просто встаёт на место —
   вкладка не рождается заново десять раз в секунду. */
function foldOneCodeBlock(pre, ui, key) {
  const code = pre.textContent || '';
  if (code.split('\n').length < 4 && code.length < 200) return;   // короткие сниппеты живут как есть
  if (pre.closest('.code-block')) return;
  const lang = pre.getAttribute('data-lang') || '';
  const slot = el('div', 'code-slot');
  const wrap = el('div', 'code-block');
  pre.replaceWith(slot);
  slot.appendChild(wrap);
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
  if (ui && key) ui._codeCache.set(key, slot);
  // раскрытие помнится по СОДЕРЖИМОМУ блока: когда хвост доедет до границы
  // заморозки и ключ сменит позицию, раскрытая вкладка не сожмётся снова
  if (ui && ui.mdEl && ui.mdEl._codePeek &&
      ui.mdEl._codePeek.has(key.split('\u0001')[0])) return;
  const thumb = collapseToThumb(wrap, {
    instant: false, cls: 'th-code inline-thumb', icon: ICO.code,
    title: lang ? 'Код · ' + lang : 'Код',
    sub: code.split('\n').length + ' стр. · ' + fmtSize(code.length),
    tag: S.agentMode ? 'развернуть' : '',
  });
  // раскрытие среди печати запоминаем: следующий тик не сожмёт блок снова
  if (thumb) thumb.addEventListener('click', () => {
    if (!ui.mdEl) return;
    if (!ui.mdEl._codePeek) ui.mdEl._codePeek = new Set();
    ui.mdEl._codePeek.add(key.split('\u0001')[0]);
  });
}

/* Проход по ленте: готовые слоты встают на место своих pre, новые блоки
   закрываются один раз. Ключ = содержимое + позиция блока (порядок блоков
   в ответе стабилен). В ХВОСТЕ ключ позиции известен заранее: слот ставится
   БЕЗ чтения текста блока — большой код не перечитывается на каждом тике.
   Голова обрабатывается один раз на границе заморозки. */
function refoldCodeBlocks(ui, root, livePre, text) {
  if (!ui._codeCache) ui._codeCache = new Map();
  if (!ui._tailKeys) ui._tailKeys = [];
  const inTail = root.classList.contains('md-tail');
  const pres = $$('pre', root);
  for (let i = 0; i < pres.length; i++) {
    const pre = pres[i];
    if (pre.closest('.code-block')) continue;            // уже в слоте
    if (pre === livePre && inCodeBlock(text)) continue;  // пишется сейчас
    // быстрый путь: позиция уже была свернута — слот из кэша, без чтения
    if (inTail && i < ui._tailKeys.length && ui._tailKeys[i]) {
      const key = ui._tailKeys[i];
      const slot = ui._codeCache.get(key);
      const peeked = ui.mdEl._codePeek && ui.mdEl._codePeek.has(key.split('\u0001')[0]);
      if (slot && !peeked) { pre.replaceWith(slot); continue; }
      if (slot && peeked) continue;   // пользователь раскрыл — блок развёрнут
    }
    const code = pre.textContent || '';
    if (code.split('\n').length < 4 && code.length < 200) {
      if (inTail) ui._tailKeys[i] = null;    // короткий сниппет — не блок
      continue;
    }
    const key = (pre.getAttribute('data-lang') || '') + '\u0000' + code +
      '\u0001' + i + (inTail ? 't' : 'f');
    const slot = ui._codeCache.get(key);
    if (slot) {
      const peeked = ui.mdEl._codePeek && ui.mdEl._codePeek.has(key.split('\u0001')[0]);
      if (!peeked) pre.replaceWith(slot);
      if (inTail) ui._tailKeys[i] = key;
      continue;
    }
    foldOneCodeBlock(pre, ui, key);
    if (inTail) ui._tailKeys[i] = key;
  }
}

/* МЕТКА «ОСТАНОВЛЕНО»: единая точка. Раньше её рисовал только обработчик
   AbortError — а если обрыв приходил другим путём (стоп из сценария, из
   follow-ожидания), метка пропадала. Теперь её ставит каждый путь стопа,
   и никогда дважды. */
function markStopped(ui) {
  if (!ui || ui._stoppedMark) return;
  if (!ui.node || !ui.node.body) return;
  ui._stoppedMark = true;
  ui.node.body.appendChild(el('div', 'muted', 'Остановлено.'));
}

function typerStop(ui) {
  if (!ui) return;
  if (ui.typer) { clearInterval(ui.typer); ui.typer = null; }
  if (ui.mdEl) {
    ui.mdEl.classList.remove('typing');
    clearTypingDecorations(ui.mdEl);
  }
  ui.cps = 0;
  ui.acc = 0;
  ui.onTyped = null;
}

/* ================== голос Джарвиса ================== */
function voiceOn() { return localStorage.getItem('jarvisVoice') === '1'; }

/* Единственная точка переключения голоса: и кнопка в шапке, и тумблер в
   настройках зовут её — иначе два источника истины разъезжаются. */
function setVoice(on) {
  localStorage.setItem('jarvisVoice', on ? '1' : '0');
  if (on) {
    toast('Озвучивание ответов включено', 'success', 'Голос');
  } else {
    try { speechSynthesis.cancel(); } catch (e) {}
    toast('Озвучивание ответов выключено', 'info', 'Голос');
  }
  api('/api/config/update', { patch: { ui: { voice_reply: on } } });
}

/* AD: КНОПКА В УГЛУ — «ЗВУК», ВЕСЬ ЗВУК. Прежняя «говорилка» гасила только
   озвучку ответов; теперь одна кнопка выключает ВСЁ: сигналы интерфейса
   (beep/blip/sfx уже слушают soundOn) и чтение ответов вслух. */
function syncSoundBtn() {
  const b = $('#voiceBtn');
  if (!b) return;
  const on = soundOn();
  b.classList.toggle('on', on);
  b.classList.toggle('off', !on);
  b.title = on ? 'Звук включён' : 'Звук выключен — полная тишина';
}

function setSound(on) {
  S.config.ui = S.config.ui || {};
  S.config.ui.sound = on;
  syncSoundBtn();
  // AE: живой отклик — короткая вспышка иконки, никаких постоянных пульсаций
  const sb = $('#voiceBtn');
  if (sb) {
    sb.classList.remove('tick');
    void sb.offsetWidth;
    sb.classList.add('tick');
    setTimeout(() => sb.classList.remove('tick'), 380);
  }
  if (!on) {
    try { speechSynthesis.cancel(); } catch (e) { /* синтеза нет */ }
    toast('Звук выключен — полная тишина', 'info', 'Звук');
  } else {
    blip(true);
    toast('Звук включён', 'info', 'Звук');
  }
  api('/api/config/update', { patch: { ui: { sound: on } } });
}

function speakReply(text) {
  // чтение вслух — тоже звук: главная кнопка «Звук» глушит и его
  if (!soundOn() || !voiceOn() || !text) return;
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
  $('#voiceBtn').addEventListener('click', () => setSound(!soundOn()));
  syncSoundBtn();
}

/* колокольчик в углу: открыть/закрыть центр уведомлений */
if ($('#bellBtn')) {
  $('#bellBtn').addEventListener('click', (e) => { e.stopPropagation(); toggleNotePanel(); });
  const npc = $('#npClear');
  if (npc) {
    npc.addEventListener('click', async () => {
      // Список не должен опустошаться мгновенно: строки улетают волной,
      // сверху вниз, и только потом появляется «пусто».
      const rows = $$('#npList .np-item');
      // Волна короче: 45 мс на строку при десятке уведомлений — это почти
      // полсекунды ожидания сверх самой анимации. И высота каждой строки
      // измеряется по факту, чтобы схлопывание начиналось с движения,
      // а не с неподвижной паузы (см. npGone).
      const STEP = 26;
      rows.forEach((r, i) => {
        r.style.setProperty('--h', r.offsetHeight + 'px');
        r.style.animationDelay = (i * STEP) + 'ms';
        r.classList.add('np-sweep');
      });
      sfx('pop');
      S.notifications = [];
      S.unread = 0;
      const wait = rows.length ? 220 + (rows.length - 1) * STEP : 0;
      setTimeout(() => renderNotePanel(), wait);
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

/* Локальный конец ответа. Серверный done сразу завершает план, но звук,
   actions и кнопка Stop ждут, пока ui.buffer действительно дойдёт до ui.shown.
   Так сетевой EOF не выдаёт недопечатанный ответ за визуально готовый. */
function clearRunRoute(ui) {
  // Имя оставлено для совместимости жизненного цикла: теперь visual-done не
  // очищает meta. Сценарий и модель — паспорт ответа и должны переживать как
  // окончание печати, так и повторное открытие диалога.
  updateResponseMeta(ui);
}

/* AZ/BA: ВСПЛЕСК НА ДЕЙСТВИИ. База круглешка спокойная; оживает он только
   когда пользовательу есть что увидеть: открылся новый инструмент, шаг
   плана, готов файл. Класс вешается на САМО ядро (.ai-core): прежний
   вариант с классом 'act' на карточке ответа попадал в CSS-правило кнопок
   .act{display:inline-flex;border:...} — ответ на секунду обрастал рамкой
   и уезжал влево. Имя dot-act коллизий не имеет */
function dotAction(root) {
  const core = root && root.querySelector ? root.querySelector('.ai-core') : null;
  if (!core || !root.classList.contains('live')) return;
  /* BG: пока играет SVG-фигура — не вспыхиваем поверх */
  if (core.classList.contains('shape-on')) return;
  core.classList.add('dot-act');
  clearTimeout(core._actTimer);
  core._actTimer = setTimeout(() => {
    if (core.classList) core.classList.remove('dot-act');
  }, 1150);
}

/* BG: ФИГУРЫ — ПОЛНОЦЕННАЯ ВЕКТОРНАЯ ГРАФИКА (SVG). Каждая фигура —
   подробный чертёж: градиентная заливка со светом (блик сверху-слева),
   яркие передние рёбра, видимые ЗАДНИЕ рёбра сквозь полупрозрачные
   грани. 4D — тессеракт, пентахорон; 3D — куб, тетраэдр, кристалл;
   2D — звезда, шестиугольник, крест; 1D — линия, волна, зигзаг */
/* BI: НАСТОЯЩАЯ 3D/4D ГЕОМЕТРИЯ. Прежние фигуры были плоскими
   проекциями с фальшивым затенением — юзер справедливо увидел «2D».
   Теперь честный конвейер: вершины → вращение (3D: ось-угол Родрига;
   4D: плоскости XW/ZW — тессеракт вращается как настоящий 4D-куб) →
   перспектива → стеклянные грани (полупрозрачные, художник по глубине,
   свет по нормали) → рёбра с приглушёнными задними. 2D- и 1D-фигур
   больше нет: 4 объёмных тела + 2 четырёхмерных. */
const DOT_S3 = 1 / Math.sqrt(3);
const DOT_SHAPES = {
  tess: {
    name: 'тессеракт', d: 4, V: [], E: [], F: [],
    init() {
      for (let i = 0; i < 16; i++) {
        this.V.push([(i & 1 ? .5 : -.5), (i & 2 ? .5 : -.5),
                     (i & 4 ? .5 : -.5), (i & 8 ? .5 : -.5)]);
      }
      for (let i = 0; i < 16; i++) for (let j = i + 1; j < 16; j++) {
        const x = i ^ j;
        if (x === 1 || x === 2 || x === 4 || x === 8) this.E.push([i, j]);
      }
      /* стеклянные кубы: вершины с w=+.5 и w=-.5 — после 4D-вращения
         внутренний куб выворачивается наружу, как у настоящего тессеракта */
      const CF = [[0, 1, 3, 2], [4, 6, 7, 5], [0, 2, 6, 4],
                  [1, 5, 7, 3], [0, 4, 5, 1], [2, 3, 7, 6]];
      [[0, 8], [8, 16]].forEach(([a, b]) => {
        const sub = [];
        for (let i = a; i < b; i++) sub.push(i);
        CF.forEach((f) => this.F.push(f.map((k) => sub[k])));
      });
    },
  },
  penta: {
    name: 'пентахорон', d: 4, E: [], F: [],
    V: [[.45, .45, .45, -.22], [.45, -.45, -.45, -.22],
        [-.45, .45, -.45, -.22], [-.45, -.45, .45, -.22],
        [0, 0, 0, .99]],
    init() {
      for (let a = 0; a < 5; a++) for (let b = a + 1; b < 5; b++) {
        this.E.push([a, b]);
        for (let c = b + 1; c < 5; c++) this.F.push([a, b, c]);
      }
    },
  },
  cube: {
    name: 'куб', d: 3, AX: [.5, .8, .33],
    V: [], E: [], F: [],
    init() {
      for (let i = 0; i < 8; i++)
        this.V.push([(i & 1 ? DOT_S3 : -DOT_S3), (i & 2 ? DOT_S3 : -DOT_S3),
                     (i & 4 ? DOT_S3 : -DOT_S3)]);
      for (let i = 0; i < 8; i++) for (let j = i + 1; j < 8; j++) {
        const x = i ^ j;
        if (x === 1 || x === 2 || x === 4) this.E.push([i, j]);
      }
      this.F = [[0, 1, 3, 2], [4, 6, 7, 5], [0, 2, 6, 4],
                [1, 5, 7, 3], [0, 4, 5, 1], [2, 3, 7, 6]];
    },
  },
  octa: {
    name: 'октаэдр', d: 3, AX: [.7, .5, .5],
    V: [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]],
    E: [], F: [],
    init() {
      /* противоположные пары: 0-1, 2-3, 4-5 (xor = 1) — не рёбра */
      for (let i = 0; i < 6; i++) for (let j = i + 1; j < 6; j++) {
        if ((i ^ j) !== 1) this.E.push([i, j]);
      }
      this.F = [[0, 2, 4], [2, 1, 4], [1, 3, 4], [3, 0, 4],
                [2, 0, 5], [1, 2, 5], [3, 1, 5], [0, 3, 5]];
    },
  },
  tetra: {
    name: 'тетраэдр', d: 3, AX: [.3, .9, .3],
    V: [[DOT_S3, DOT_S3, DOT_S3], [DOT_S3, -DOT_S3, -DOT_S3],
        [-DOT_S3, DOT_S3, -DOT_S3], [-DOT_S3, -DOT_S3, DOT_S3]],
    E: [[0, 1], [0, 2], [0, 3], [1, 2], [1, 3], [2, 3]],
    F: [[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]],
    init() {},
  },
  crystal: {
    name: 'кристалл', d: 3, AX: [.2, .95, .25],
    V: [], E: [], F: [],
    init() {
      /* шестигранная бипирамида — честный кристалл с 12 гранями */
      for (let i = 0; i < 6; i++) {
        const a = i * Math.PI / 3;
        this.V.push([Math.cos(a) * .82, Math.sin(a) * .82, 0]);
      }
      this.V.push([0, 0, 1.15], [0, 0, -1.15]);
      for (let i = 0; i < 6; i++) {
        this.E.push([i, (i + 1) % 6]);
        this.E.push([i, 6]); this.E.push([i, 7]);
        this.F.push([i, (i + 1) % 6, 6]);
        this.F.push([(i + 1) % 6, i, 7]);
      }
    },
  },
};
Object.keys(DOT_SHAPES).forEach((k) => DOT_SHAPES[k].init());
const DOT_SHAPE_KEYS = Object.keys(DOT_SHAPES);
const DOT_MORPH_MS = 700;      // BK: круглешок ВЫРАСТАЕТ в фигуру (морф)
const DOT_MORPH_OUT_MS = 650;  // BK: фигура ПЛАВНО СЖИМАЕТСЯ обратно в круг
const DOT_HOLD_MS = 4200;      // BK: фигура держится долго — успеваешь рассмотреть
const DOT_CIRCLE_R = 5.2;      // радиус круглешка в единицах viewBox (13px)
let DOT_SVG_N = 0;

/* вращение точки вокруг оси (формула Родрига) */
function dotRotAxis(v, axis, ang) {
  const k = axis, c = Math.cos(ang), s = Math.sin(ang), d = k[0] * v[0] + k[1] * v[1] + k[2] * v[2];
  return [
    v[0] * c + (k[1] * v[2] - k[2] * v[1]) * s + k[0] * d * (1 - c),
    v[1] * c + (k[2] * v[0] - k[0] * v[2]) * s + k[1] * d * (1 - c),
    v[2] * c + (k[0] * v[1] - k[1] * v[0]) * s + k[2] * d * (1 - c),
  ];
}

/* нормаль грани */
function dotFaceNormal(pts) {
  const ax = pts[1][0] - pts[0][0], ay = pts[1][1] - pts[0][1], az = pts[1][2] - pts[0][2];
  const bx = pts[2][0] - pts[0][0], by = pts[2][1] - pts[0][1], bz = pts[2][2] - pts[0][2];
  const nx = ay * bz - az * by, ny = az * bx - ax * bz, nz = ax * by - ay * bx;
  const L = Math.hypot(nx, ny, nz) || 1;
  return [nx / L, ny / L, nz / L];
}

/* один кадр фигуры: t — секунды с начала показа.
   BK: НАСТОЯЩАЯ ТРАНСФОРМАЦИЯ, а не подмена. m — фаза морфа (0 = круг,
   1 = фигура): вершины ВЫРАСТАЮТ радиально из обода круглешка в свои
   3D-позиции, грани наливаются стеклом, а сам круг тает — и в обратную
   сторону при уходе. Круг и фигура — одно тело, ничего не «появляется
   поверх» */
function dotShapeFrame(key, t, L) {
  const sh = DOT_SHAPES[key];
  const SC = sh.d === 4 ? 8.6 : 10.4;
  const smooth = (k) => k * k * (3 - 2 * k);
  const mt = t * 1000;
  let m;
  if (mt < DOT_MORPH_MS) m = smooth(mt / DOT_MORPH_MS);
  else if (mt < DOT_MORPH_MS + DOT_HOLD_MS) m = 1;
  else m = 1 - smooth(Math.min(1, (mt - DOT_MORPH_MS - DOT_HOLD_MS) / DOT_MORPH_OUT_MS));
  let pts3;
  if (sh.d === 4) {
    /* ВРАЩЕНИЕ В 4D: две плоскости (XW и ZW) с разными скоростями —
       тессеракт выворачивается, как настоящее 4D-вращение (внутренний
       куб становится внешним и обратно) */
    const a = t * 0.85, b = t * 0.5;
    const ca = Math.cos(a), sa = Math.sin(a), cb = Math.cos(b), sb = Math.sin(b);
    pts3 = sh.V.map((p) => {
      const x1 = p[0] * ca - p[3] * sa;
      const w1 = p[0] * sa + p[3] * ca;
      const z1 = p[2] * cb - w1 * sb;
      const w2 = p[2] * sb + w1 * cb;
      const k = 3.1 / (3.1 - w2 * 1.5);      // перспектива 4D → 3D
      return [x1 * k, p[1] * k, z1 * k];
    });
  } else {
    const ang = t * 0.62;
    pts3 = sh.V.map((p) => dotRotAxis(p, sh.AX, ang));
  }
  /* перспектива 3D → 2D */
  const P = pts3.map((p) => {
    const k = 5.6 / (5.6 - p[2] * 1.1);
    return [p[0] * k * SC, p[1] * k * SC, p[2]];
  });
  /* МОРФ: каждая вершина едет по своему лучу из обода круга (r = R0)
     в её проекцию. В нуле все вершины на ободе — фигура и ЕСТЬ круг */
  const Q = P.map((p) => {
    const r = Math.hypot(p[0], p[1]);
    if (r < 1e-9) return [p[0] * m, p[1] * m, p[2]];
    const rr = DOT_CIRCLE_R + (r - DOT_CIRCLE_R) * m;
    const k = rr / r;
    return [p[0] * k, p[1] * k, p[2]];
  });
  const px = (i) => Q[i][0].toFixed(2) + ',' + Q[i][1].toFixed(2);
  /* грани-стекло: художник по глубине, свет по нормали.
     BE: стекло видимое (0.18+0.34·свет), обводка запаивает швы.
     BK: грани НАЛИВАЮТСЯ по мере морфа (opacity × m) */
  const faces = sh.F.map((f) => {
    const ps = f.map((i) => Q[i]);
    let depth = 0;
    ps.forEach((q) => { depth += q[2]; });
    depth /= ps.length;
    const n = dotFaceNormal(ps);
    const bright = Math.abs(n[2] * .62 - n[1] * .5 + n[0] * .36);
    const op = (0.18 + 0.34 * bright) * m;
    return {
      depth,
      html: '<polygon points="' + f.map((i) => px(i)).join(' ') +
        '" fill="url(#gF' + L + ')" fill-opacity="' + op.toFixed(3) +
        '" stroke="url(#gF' + L + ')" stroke-width=".4" stroke-opacity="' + op.toFixed(3) + '"/>',
    };
  }).sort((a, b) => a.depth - b.depth);
  /* рёбра: передние яркие, задние приглушённые; вырастают вместе с морфом */
  let edges = '';
  sh.E.forEach((e) => {
    const back = (Q[e[0]][2] + Q[e[1]][2]) / 2 < 0;
    edges += '<line x1="' + Q[e[0]][0].toFixed(2) + '" y1="' + Q[e[0]][1].toFixed(2) +
      '" x2="' + Q[e[1]][0].toFixed(2) + '" y2="' + Q[e[1]][1].toFixed(2) +
      '" stroke="' + (back ? 'url(#gB' + L + ')"' : 'url(#gE' + L + ')"') +
      ' stroke-width="' + (back ? '.75' : '1.05') + '" opacity="' +
      ((back ? .55 : .95) * m).toFixed(3) + '"/>';
  });
  /* ТЕЛО КРУГЛЕШКА: тает по мере роста фигуры (и возвращается при сжатии) */
  const core = '<circle cx="0" cy="0" r="' + (DOT_CIRCLE_R + 2.4 * m).toFixed(2) +
    '" fill="url(#gF' + L + ')" opacity="' + ((1 - m) * .96).toFixed(3) + '"/>';
  return core + faces.map((f) => f.html).join('') +
    '<g>' + edges + '</g>';
}

function dotShapeSvg(core) {
  if (core._shapeSvg && core._shapeSvg.isConnected) return core._shapeSvg;
  const L = ++DOT_SVG_N;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'dot-shape-svg');
  svg.setAttribute('viewBox', '-16 -16 32 32');
  svg.innerHTML =
    '<defs>' +
      '<radialGradient id="gF' + L + '" cx="34%" cy="28%" r="82%">' +
        '<stop offset="0%" stop-color="#ffffff"/><stop offset="38%" stop-color="#c8eeff"/>' +
        '<stop offset="72%" stop-color="#48b6ea"/><stop offset="100%" stop-color="#0e6ea8"/>' +
      '</radialGradient>' +
      '<linearGradient id="gE' + L + '" x1="0" y1="0" x2="1" y2="1">' +
        '<stop offset="0%" stop-color="#ffffff"/><stop offset="55%" stop-color="#d4f1ff"/>' +
        '<stop offset="100%" stop-color="#8ed4f4"/>' +
      '</linearGradient>' +
      '<linearGradient id="gB' + L + '" x1="0" y1="0" x2="1" y2="1">' +
        '<stop offset="0%" stop-color="#7cc2e6"/><stop offset="100%" stop-color="#2a76a0"/>' +
      '</linearGradient>' +
    '</defs><g class="rot-g"></g>';
  svg._gradL = L;
  core.parentNode.appendChild(svg);
  core._shapeSvg = svg;
  return svg;
}

function dotShapePlay(root) {
  const core = root && root.querySelector ? root.querySelector('.ai-core') : null;
  if (!core) return;
  clearTimeout(core._shapeTimer);
  const schedule = () => {
    /* BE: ЧАСТО — каждые 3.5–8 секунд: круглешок забавляется,
       чтобы читатель не скучал */
    core._shapeTimer = setTimeout(play, 3500 + Math.random() * 4500);
  };
  const stopRot = () => {
    if (core._shapeRaf) {
      cancelAnimationFrame(core._shapeRaf);
      core._shapeRaf = null;
    }
  };
  const play = () => {
    if (!core.isConnected || !root.classList.contains('live') ||
        core.classList.contains('dot-settle')) { return; }
    const key = DOT_SHAPE_KEYS[Math.floor(Math.random() * DOT_SHAPE_KEYS.length)];
    const svg = dotShapeSvg(core);
    const g = svg.querySelector('.rot-g');
    g.setAttribute('class', 'rot-g');
    /* BK: вращает САМ ДВИЖОК (JS) и НЕ ПРЕРЫВАЕТСЯ до конца показа:
       кадр рисуется на каждом rAF (60fps — фигура маленькая, это дёшево),
       морф туда и обратно считается внутри dotShapeFrame по t. Прежний
       пропуск кадров (33мс) давал рывки «одним кадром» */
    const t0 = performance.now();
    let finished = false;
    const rot = () => {
      core._shapeRaf = null;
      if (!svg.isConnected || !g) return;
      const t = (performance.now() - t0) / 1000;
      g.innerHTML = dotShapeFrame(key, t, svg._gradL);
      if (svg.classList.contains('sh-out')) return;   // сворачиваемся — стоп
      if (!finished && t * 1000 >= DOT_MORPH_MS + DOT_HOLD_MS + DOT_MORPH_OUT_MS) {
        /* морф завершился: фигура снова СТАЛА кругом — мягко уступить
           место настоящему круглешку (перекрёстное растворение) */
        finished = true;
        stopRot();
        svg.classList.remove('sh-in');
        svg.classList.add('sh-out');
        core.classList.remove('shape-on');
        setTimeout(() => {
          if (!svg.classList) return;
          svg.classList.remove('sh-out');
          g.setAttribute('class', 'rot-g');
          g.innerHTML = '';
          schedule();
        }, 420);
        return;
      }
      if (!finished) core._shapeRaf = requestAnimationFrame(rot);
    };
    g.innerHTML = dotShapeFrame(key, 0, svg._gradL);
    core._shapeRaf = requestAnimationFrame(rot);
    svg.classList.remove('sh-out');
    svg.classList.add('sh-in');
    core.classList.add('shape-on');
  };
  schedule();
}
function finishLiveDot(root) {
  if (!root || !root.classList || !root.classList.contains('live')) return;
  const core = root.querySelector('.ai-core');
  if (core) {
    clearTimeout(core._actTimer);
    clearTimeout(core._shapeTimer);
    if (core._shapeRaf) { cancelAnimationFrame(core._shapeRaf); core._shapeRaf = null; }
    core.classList.remove('dot-act', 'shape-on', 'dot-settle');
    /* BJ: ФИГУРА УХОДИТ ПЛАВНО, а не исчезает мгновенно: раньше в момент
       конца ответа svg вырывался из DOM среди вращения — фигура «рвалась».
       Теперь играем мягкий sh-out (анимация больше не привязана к классу
       .live) и убираем svg только после её конца; кружок в это время
       плавно проявляется перекрёстным растворением */
    if (core._shapeSvg) {
      const sg = core._shapeSvg;
      core._shapeSvg = null;
      sg.classList.remove('sh-in');
      sg.classList.add('sh-out');
      const g = sg.querySelector('.rot-g');
      if (g) { g.setAttribute('class', 'rot-g'); g.innerHTML = ''; }
      setTimeout(() => sg.remove(), 800);
    }
    core.classList.add('dot-settle');
    setTimeout(() => {
      if (core.classList) core.classList.remove('dot-settle');
    }, 640);
  }
  root.classList.remove('live');
}

function settleVisualDone(ui) {
  if (!ui || ui.visualDone) return;
  if (S.liveUi === ui) S.liveUi = null;
  finishLiveDot(ui.node && ui.node.root);
  clearRunRoute(ui);
  if (S.followUi === ui) S.followUi = null;
  if (ui.stopFollowWatch) ui.stopFollowWatch();
  ui.visualDone = true;
  if (ui.resolveVisualDone) {
    ui.resolveVisualDone();
    ui.resolveVisualDone = null;
  }
}

function queueResponseFinish(ui, content, success) {
  if (ui.doneReceived) return;
  ui.doneReceived = true;
  // Ответ уже ПОЛУЧИСТ целиком. Хвост из кода, таблиц и списков не должен
  // «досматриваться» в медленном темпе — плотный контент ускоряется.
  // Разговорный текст после done печатается как живой: с прежним темпом
  // и паузами, иначе ответ «выстреливает» и теряет характер.
  ui.fastFinish = true;
  const doneContent = String(content || '');
  if (!ui.mdEl) {
    ui.mdEl = el('div', 'md');
    ui.node.body.appendChild(ui.mdEl);
  }

  // У ответа один владелец: delta-buffer, уже увиденный браузером. Раньше
  // несовпадающий done.content стирал весь DOM и запускал печать заново — в
  // многошаговом AGENT это как раз заменяло полный ответ последним пунктом.
  // Сервер теперь присылает каноническую склейку, но фронт всё равно не имеет
  // права откатывать показанное. doneContent используется лишь для действительно
  // непроточного ответа, когда ни одного delta не было.
  if (!ui.buffer) ui.buffer = doneContent;
  // ГОНКА ПЛАНА. Дельты могли прийти не все: стрим сетевого события done —
  // канонический текст ответа, и он бывает ДЛИННЕЕ накопленного buffer
  // (последний сегмент не стримился дельтами). Раньше печать завершалась на
  // обрыве, план зеленел и уезжал — «выполнен до конца печати». Теперь:
  // если done-текст НАЧИНАЕТСЯ с напечатанного буфера, допечатываем хвост
  // (план завершится только по-настоящему последнего символа). Если текст
  // иной природы — показанное не откатываем, как и раньше.
  if (doneContent.length > ui.buffer.length &&
      doneContent.startsWith(ui.buffer)) {
    ui.buffer = doneContent;
  }
  content = ui.buffer;
  ui.onTyped = () => {
    if (ui.visualDone) return;
    try {
      ui.mdEl.classList.remove('typing');
      clearTypingDecorations(ui.mdEl);
      ui.floor = 0;
      ui.mdEl.style.minHeight = '';

      // Последний такт renderTyped уже построил полный markdown. Ничего не
      // пересобираем и не присваиваем повторно: callback достигается только при
      // ui.shown === ui.buffer, а buffer и есть канонический ответ.
      // `reply_ui` уже владеет отдельной live-панелью рядом с Markdown. Если
      // событие не пришло (старый сохранённый ответ), fence остаётся fallback.
      const embeddedPanels = $$('.ui-panel', ui.mdEl);
      if (ui.replyLive && ui.replyLive.isConnected) {
        // Не показываем одну спецификацию второй раз из echoed markdown-fence.
        embeddedPanels.forEach((panel) => panel.remove());
      } else if (ui.replyUiSpec && embeddedPanels.length === 0) {
        const fallbackPanel = el('div', 'ui-panel');
        fallbackPanel.dataset.ui = ui.replyUiSpec;
        ui.mdEl.appendChild(fallbackPanel);
      }
      const panels = [
        ...$$('.ui-panel', ui.mdEl),
        ...(ui.replyLive && ui.replyLive.isConnected ? $$('.ui-panel', ui.replyLive) : []),
      ];
      // ПЕРВОПРИЧИНА «видно только после повторного открытия»: пустой ui-panel
      // во время печати имеет display:none и затем вырастает за нижней кромкой.
      // Геометрия ПОСЛЕ роста уже ничего не говорит о намерении пользователя,
      // поэтому решение хранится на run с момента wheel/touch/scroll-away.
      // live-code здесь НЕ снимается: именно это распахивало блок на кадр
      // перед сворачиванием. foldCodeBlocks съёживает от видимой высоты.
      foldCodeBlocks(ui.mdEl, true);
      mountUiPanels(ui.mdEl);
      mountPlotPanels(ui.mdEl);
      fixTables(ui.mdEl);
      if (ui.replyLive && ui.replyLive.isConnected) mountPlotPanels(ui.replyLive);
      $$('.img-out', ui.mdEl).forEach((im) => im.addEventListener('click',
        () => openPreview({ name: im.alt || 'изображение', url: im.src })));

      if (ui.thinkCard && ui.thinkCard.isConnected) {
        if (ui.thinkCard.classList.contains('qt-think')) {
          const mk = ui.thinkCard.querySelector('.qt-mark');
          if (mk) mk.textContent = '✓';
          qtMiniaturize(ui.thinkCard);
        } else {
          const ts = thinkFlush(ui.thinkCard);
          collapseSoon(ui.thinkCard, {
            cls: 'th-think', icon: ICO.think, title: 'Ход мыслей',
            sub: ts ? fmtSize((ts.textContent || '').length) : '',
            tag: 'развернуть',
          });
        }
      }

      if (success) {
        ui.planItems.forEach((li) => {
          li.classList.remove('now');
          li.classList.add('done');
        });
        undockPlan(ui);
      } else if (!ui.planFinished) {
        discardPlan(ui);
      }
      if (success) {
        // Кнопки «Копировать/Озвучить» — только на ПОЛНОСТЬЮ законченном
        // ответе. Если в ответе ждёт выбора интерактивная панель — это
        // пауза, а не финал: продолжение допишет ответ, и кнопки встанут
        // один раз, в самом конце.
        const pendingPanel = $$('.ui-panel:not(.ui-sent)', ui.mdEl).length ||
          (ui.replyLive && ui.replyLive.isConnected &&
           $$('.ui-panel:not(.ui-sent)', ui.replyLive).length);
        if (!pendingPanel) addMsgActions(ui.node, content);
        if (S.streamRun === ui.runId && ui.node.isConnected && !pendingPanel) {
          speakReply(content);
          sfx('done');
        }
      }
      // Route принадлежит этому ответу и исчезнет в settleVisualDone — вместе
      // с фактическим завершением визуальной печати, а не по сетевому done.

      // Если человек сам ушёл вверх, не перетягиваем его. Иначе controls
      // появляются в кадре в том же lifecycle, без закрытия диалога.
      if (ui.node.root.isConnected) {
        scrollDown(false, ui);
        if (panels.length) followGrowingPanel(ui.replyLive || ui.mdEl, 900, ui);
      }
    } finally {
      settleVisualDone(ui);
    }
  };
  typerFlush(ui);
}

const TIER_LABEL = {
  nano: 'простой запрос', base: 'обычный запрос', smart: 'сложный запрос',
  coder: 'работа с кодом', vision: 'работа со зрением',
};

/* ЧЕЛОВЕЧЕСКИЙ ВИД РЕЗУЛЬТАТА ИНСТРУМЕНТА. Раньше всё, что не подходило
   под известные ключи, печаталось сырым JSON.stringify — пользователь видел
   `{"ok": true, "status": 200, "body": …}` вместо «HTTP 200 · получено
   1 842 символа». JSON — служебный формат для модели, человеку — выжимка. */
function toolResultText(r) {
  if (!r) return '';
  if (r.error) return '⚠ ' + r.error;
  if (r.stdout != null || r.stderr != null) {
    return ((r.stdout || '') + (r.stderr ? '\n' + r.stderr : '')).slice(0, 4000);
  }
  if (r.results) {
    return (r.results || []).map((x) => '• ' + (x.title || '') + '\n  ' + (x.url || '') + '\n  ' + (x.snippet || '')).join('\n');
  }
  if (r.status != null && r.body != null) {
    let body = '';
    try {
      const parsed = JSON.parse(r.body);
      body = Object.keys(parsed).slice(0, 6).map((k) => {
        const v = parsed[k];
        return k + ': ' + (typeof v === 'object' ? '…' : String(v).slice(0, 60));
      }).join(' · ');
    } catch (e) {
      body = String(r.body).replace(/\s+/g, ' ').slice(0, 140);
    }
    return 'HTTP ' + r.status + ' · получено ' + String(r.body || '').length.toLocaleString('ru-RU') +
      ' симв.' + (body ? '\n' + body : '');
  }
  if (r.content) return String(r.content).slice(0, 4000);
  if (r.text) return String(r.text).slice(0, 4000);
  if (r.path) return (r.title ? r.title + '\n' : '') + r.path;
  if (r.url) return (r.title || r.url) + '\n' + r.url;
  // общий случай: короткая выжимка скалярных полей, БЕЗ сырого JSON
  const keys = Object.keys(r).filter((k) => k !== 'ok');
  const lines = keys.slice(0, 8).map((k) => {
    const v = r[k];
    if (v == null) return null;
    if (typeof v === 'string') return k + ': ' + v.slice(0, 200);
    if (typeof v === 'number' || typeof v === 'boolean') return k + ': ' + v;
    if (Array.isArray(v)) return k + ': ' + v.length + ' шт.';
    return null;
  }).filter(Boolean);
  return (r.ok ? '' : '') + (lines.join('\n') || 'готово');
}

function dispatchStreamEvent(ev, ui) {
  if (ui.planGate) {
    ui.planDeferred.push(ev);
    return;
  }
  handleEvent(ev, ui);
}

function handleEvent(ev, ui) {
  const node = ui.node;
  switch (ev.type) {
    case 'chat':
      // Ответ знает владельца с момента send(). Поздний chat-event старой
      // камеры не имеет права присвоить свой id уже повторно открытой карточке.
      if (ui.isolatedCamera) {
        if (ui.cameraNode && ui.cameraNode === S.camNode) S.camChatId = ev.chat_id;
      } else if (ui.voiceIsolated) {
        // AB: изолированный разговор — свой служебный диалог, С.chatId не трогаем
        VOICE.chatId = ev.chat_id;
      } else {
        S.chatId = ev.chat_id;
        // AB: прогон только что узнал свой диалог — регистрируем для возврата
        if (S.liveRuns) S.liveRuns[ev.chat_id] = ui;
        // НОВЫЙ ДИАЛОГ: имя придумывается фоном, параллельно ответу. Пока
        // его нет — в списке живое «…» вместо казённого «Новый диалог».
        loadChats().then(() => {
          const it = $$('.chat-item').find((x) => x.classList.contains('active'));
          const t = it && it.querySelector('.chat-title');
          if (t && /^(новый диалог|диалог)$/i.test((t.textContent || '').trim())) {
            t.textContent = '…';
            t.classList.add('title-pending');
          }
        });
      }
      break;

    case 'chat_title': {
      // фоновое название приехало прямо в живой поток — обновляем список
      if (ev.title) loadChats();
      break;
    }

    case 'user_msg': {
      // id относится к пузырю ЭТОГО запроса. Поиск «последнего .msg-user во
      // всей ленте» ломался при двух поколениях camera card и гонке ответов.
      if (ui.userMsgNode && !ui.userMsgNode.dataset.msgId) ui.userMsgNode.dataset.msgId = ev.id;
      break;
    }

    case 'ai_msg': {
      // AA: id сохранённого ответа приезжает до закрытия потока — панель в
      // этом сообщении продолжает ИМЕННО его, даже если ленту перерисовали
      if (ui.node && ui.node.root && !ui.node.root.dataset.msgId) {
        ui.node.root.dataset.msgId = ev.id;
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
      ui.routeTier = ev.tier || '';
      ui.routeReason = ev.reason || '';
      updateResponseMeta(ui);
      // Кухню показываем только на сложных задачах: на «привет» и короткий
      // вопрос пользователь ждёт ответ, а не ход мыслей и терминал.
      ui.verbose = ev.verbose !== false;
      break;
    }

    case 'model':
      ui.modelName = ev.model || '';
      updateResponseMeta(ui);
      $('#footModel').textContent = ev.model || '—';
      break;

    case 'status':
      // Состояние приходит с сервера полем phase, а не угадывается по тексту.
      // Во всех фазах — живой курсор; think дополнительно перебирает реплики.
      if (ev.phase === 'think') thinkMode(ui);
      else busyMode(ui, ev.text);
      break;

    case 'thinking': {
      // Y: ХОД МЫСЛЕЙ — КАЖДОМУ РЕЖИМУ СВОЙ ДИЗАЙН. Тихому — та же кухня,
      // что и инструментам: серая строка с потоком строк; агенту — его
      // карточка. А показываются мысли в тихом режиме только у рабочих
      // ответов (первый инструмент / второй ход) — это решает сервер.
      if (!(ui.agentMode || S.agentMode)) {
        if (!ui.thinkCard || !ui.thinkCard.classList.contains('qt-think')) {
          const qn = el('div', 'qt-node qt-think');
          qn.innerHTML =
            '<div class="qt-head">' +
              '<span class="qt-ico">◇</span>' +
              '<span class="qt-name">Ход мыслей</span>' +
              '<span class="qt-mark"></span>' +
            '</div>' +
            '<div class="qt-body"><span class="qt-rail"></span><div class="qt-flow">' +
              '<div class="qt-flowin"></div></div></div>';
          // AA: строку можно раскрыть обратно после сворачивания
          qn.querySelector('.qt-head').addEventListener('click', () => qtToggleThink(qn));
          ui.thinkCard = qn;
          markBorn(qn);
          /* AQ: и тихая строка мыслей открывает ответ, а не замыкает */
          node.body.insertBefore(qn, node.body.firstChild);
        }
        const flow = ui.thinkCard.querySelector('.qt-flow');
        if (flow && ev.text) qtThinkFeed(flow, ev.text);
        scrollSoon(ui);
        break;
      }
      if (!ui.thinkCard) {
        // карточка раскрыта сразу: мысли должны бежать на глазах, как в терминале
        ui.thinkCard = makeCard('◇', 'Ход мыслей', 'think-card', true);
        markBorn(ui.thinkCard);
        ui.thinkCard.inner.appendChild(el('div', 'think-stream'));
        node.body.appendChild(ui.thinkCard);
      }
      /* AQ: ХОД МЫСЛЕЙ — РАНЬШЕ ВСЕХ. Мысли могут прийти к нам позже
         инструментов (модель думает между вызовами) — карточка при
         каждом событии поднимается в САМЫЙ ВЕРХ ответа: если уж мысль
         показывается, она открывает ответ, а не прячется под низом */
      if (node.body.firstChild !== ui.thinkCard) {
        node.body.insertBefore(ui.thinkCard, node.body.firstChild);
      }
      const ts = ui.thinkCard.querySelector('.think-stream');
      thinkType(ts, ev.text);
      ui.thinkCard.setTitle('Ход мыслей <span class="muted" style="font-size:10.5px">· думаю…</span>');
      scrollSoon(ui);
      break;
    }

    case 'plan': {
      // Вторая граница после backend: plan виден только реальному AGENT-run.
      // Режим могли включить ПОСЛЕ отправки (карточка-разрешение, mode_changed
      // на лету) — тогда ui.agentMode ещё false, но S.agentMode уже true.
      // Старая проверка только по ui молча выбрасывала план: агент работал
      // «без плана», хотя сервер его честно присылал.
      if (!(ui.agentMode || S.agentMode) || !(ev.steps || []).length) break;
      beginPlanGate(ui);
      // Агент может составить план дважды за прогон (уточнил задачу — сделал
      // новый). Прежнюю панель и прежнюю карточку убираем, иначе первая так и
      // останется висеть наверху: undockPlan знает только про последнюю.
      if (ui.planDock || ui.planCard) {
        clearPlanTimers(ui);
        dropStrayDocks(null);
        ui.planDock = null;
        if (ui.planCard && ui.planCard.isConnected) ui.planCard.remove();
        if (ui.planHome && ui.planHome.isConnected) ui.planHome.remove();
        ui.planHome = null;
        ui.planItems = [];
        ui.planPaintQ = [];
        ui.planPainted = 0;
      }
      ui.planFinished = false;
      // Новый план (и первый, и заменивший старый) начинает покраску с нуля:
      // значения предыдущего плана не должны блокировать шаги нового.
      ui.planPaintQ = [];
      ui.planPainted = 0;
      ui.planPaintPending = null;
      ui.planCard = makeCard('☰', 'План · ' + ev.steps.length + ' шаг(ов)', 'plan-card', true);
      markBorn(ui.planCard);
      const list = el('ul', 'plan-list');
      ui.planList = list;
      ui.planStep = 1;
      ev.steps.forEach((s, i) => {
        const li = el('li', 'plan-pending', '<span class="plan-num">' + (i + 1) + '</span><span class="plan-copy"></span>');
        // Модель иногда помечает шаги markdown-зачёркиванием (~~step~~) и
        // жирным: в карточке плана это выглядит мусором. Шаг — чистый текст.
        li._planText = String(s || '').replace(/~~/g, '').replace(/\*\*/g, '')
          .replace(/\x60/g, '').trim();
        // Пункты уже существуют как состояние (plan_step может прийти сразу),
        // но в DOM входят и быстро печатаются по одному. Общий typer ответа
        // начнётся только после releasePlanGate по завершении перелёта.
        ui.planItems.push(li);
      });
      ui.planCard.inner.appendChild(list);
      node.body.insertBefore(ui.planCard, ui.statusEl);
      // Flying dock сам является визуальной копией. Якорь-распорка здесь не
      // нужен: именно он оставлял пустую дыру перед началом ответа.
      ui.planHome = null;
      sfx('pop');
      // ПОЧЕМУ ЭКРАН НЕ ЕХАЛ ВНИЗ ЗА ПЛАНОМ.
      // Одного scrollDown() мало: в этот момент карточка только вставлена, её
      // высота ещё не посчитана (пункты появляются с анимацией, шрифт может
      // дорисовываться). Но и мгновенный pinToBottom не годится — это и был
      // резкий «скачок одним кадром». ChaseBottom сам держит низ столько
      // кадров, сколько нужно: он каждый кадр замеряет остаток и доедает его
      // плавным ходом — карточка растёт, лента догоняет как при тихой печати.
      chaseBottom(stream(), ui);
      // Пункты идут строго последовательно: быстрая печать и короткие 300 мс.
      // Сеть читается дальше, но события ответа лежат в gate-очереди.
      planLater(ui, () => revealPlanItems(ui, 0), 100);
      break;
    }

    case 'tool_hint':
      // AA: имя инструмента приезжает, когда строка состояния уже разобрана
      // (после текста шага). Раньше подсказка молчала в пустоту и не могла
      // вернуть строку — теперь она её воскрешает сама
      ensureStatus(ui);
      busyMode(ui, [ev.label || 'Готовлю инструмент'].concat(groupQuips(ev.group)), 2300);
      break;

    case 'tool_start': {
      // Вызов инструмента не перекрашивает обычный последующий ответ: gradient
      // живёт только на самой tool-карточке, а Markdown сохраняет синюю каретку.
      // раз дошло до инструментов — задача не «простая», кухню открываем
      ui.verbose = true;
      // «Глаза» агента (снимок экрана, параметры экрана) — служебные шаги.
      // Пользователю их видеть незачем: он просил результат, а не отчёт
      // о каждом кадре. Тихо запоминаем и показываем только в терминале.
      if (SILENT_TOOLS[ev.name]) {
        ui.silent[ev.id || ev.name] = true;
        if (ui.statusEl) {
          busyMode(ui, ['смотрю на экран', 'разбираю, что вижу'], 1400);
        }
        termLine('$ ' + ev.name, 'cmd');
        break;
      }
      // строка состояния рассказывает, чем занят прямо сейчас; она же
      // гарантирует, что инструменту есть куда встать (после начала текста
      // строку разбирали — ensureStatus возвращает её на место)
      ensureStatus(ui);
      busyMode(ui, toolTicker(ev), 2200);
      termLine('$ ' + ev.name + ' ' + JSON.stringify(ev.args || {}).slice(0, 300), 'cmd');
      dotAction(ui.node && ui.node.root);
      ui._qtSwept = false;
      // РЕЖИМ РЕШАЕТСЯ «СЕЙЧАС» (X): включили AGENT после старта ответа —
      // инструменты этого диалога рисуются в агентском дизайне, а не в
      // тихом (и наоборот). Снимок на момент отправки врал о режиме.
      if (!(ui.agentMode || S.agentMode)) {
        // ОБЫЧНЫЙ РЕЖИМ — СЕРАЯ КУХНЯ: имя, полоса, поток строк. Инструмент
        // другого типа закрывает прежнее семейство — его миниатюры уезжают
        // в папку только теперь, чередой, а не по одному.
        qtSweep(ui, ev.group);
        qtOpen(ui, ev);
        break;
      }
      // AGENT — СВОЯ ПОДАЧА: карточка с рамкой, аргументами и спиннером,
      // как в агентской рубке. Череда однотипных карточек складывается
      // в одну групповую (другой тип работы или конец ответа).
      if (ui.agentGroup && ui.agentGroup.group !== ev.group) flushAgentGroup(ui);
      const waitVisual = ev.wait_visual === true;
      const card = makeCard('⚙', ev.label || ev.name,
        'tool-card' + (waitVisual ? ' tool-wait' : ''), true);
      card.querySelector('.card-head').insertBefore(el('span', 'tool-run'), card.querySelector('.chev'));
      const kv = el('div', 'kv');
      Object.keys(ev.args || {}).forEach((k) => {
        const v = String(ev.args[k]);
        kv.innerHTML += '<i>' + esc(k) + '</i><span>' + esc(v.length > 300 ? v.slice(0, 300) + '…' : v) + '</span>';
      });
      card.inner.appendChild(kv);
      node.body.insertBefore(card, ensureStatus(ui) || null);
      markBorn(card);
      card._tool = { id: ev.id || ev.name, name: ev.name, label: ev.label || ev.name,
                     group: ev.group || 'base', args: ev.args || {} };
      if (!ui.agentGroup) ui.agentGroup = { group: ev.group || 'base', cards: [] };
      ui.agentGroup.cards.push(card);
      ui.tools[ev.id || ev.name] = card;
      beep(520, 0.05);
      scrollDown();
      break;
    }

    case 'plan_step': {
      dotAction(ui.node && ui.node.root);
      // Шаг НАЧАЛСЯ: предыдущие отмечаем выполненными, текущий подсвечиваем.
      // Раньше фронт считал вызовы инструментов и в конце разом вычёркивал
      // весь список — теперь это факт от самой модели.
      const n = ev.step | 0;
      const total = ui.planItems.length || 1;
      ui.planStep = Math.max(1, Math.min(n || 1, total));
      // Покраска — с ритмом: пачка фактов не должна проскочить экран
      // за один кадр и изобразить «мгновенное» выполнение плана.
      if (ui.planStep > (ui.planPainted || 0)) {
        (ui.planPaintQ || (ui.planPaintQ = [])).push(ui.planStep);
        paintPlanStepSoon(ui);
      }
      break;
    }

    case 'budget_wait': {
      // Лимит исчерпан: тихая карточка в каноне вопросов Джарвиса — та же
      // типографика, те же кнопки-варианты, только жёлтый акцент.
      reactor('wait');
      busyMode(ui, ['Лимит исчерпан', 'жду решения'], 1500);
      const card = el('div', 'panel-card ask-card budget-card');
      card.innerHTML =
        '<div class="ask-h"><span class="ask-i rub">₽</span>Лимит ' +
        esc(String(ev.limit || '?')) + ' ₽ исчерпан</div>' +
        '<div class="budget-spent">потрачено ' + esc(String(ev.spent || '?')) +
        ' ₽ — продолжаем?</div>' +
        '<div class="ask-opts"></div>';
      const box = card.querySelector('.ask-opts');
      // Суммы докладываем из ПРИВЫЧЕК пользователя (последние разные суммы,
      // которые он сам вводил), а не из фиксированного списка сервера.
      const pick = (answer) => {
        if (card.dataset.done === '1') return;
        card.dataset.done = '1';
        api('/api/questions/answer', { id: ev.id, answer });
        // Решение принято — карточка сворачивается в миниатюру чуть крупнее
        // обычных строк инструментов: деньги видны и после сворачивания.
        collapseToThumb(card, {
          cls: 'th-budget', icon: '₽',
          title: 'Лимит ' + ev.limit + ' ₽',
          sub: answer.replace(/^Увеличить на /, '+').replace(/^Отключить лимит$/, 'лимит снят')
            .replace(/^Остановить$/, 'остановлено'),
          tag: 'решено',
        });
        reactor('busy');
      };
      budgetSuggestions().forEach((v) => {
        const b = el('button', 'ask-opt good', '+' + v + ' ₽');
        b.addEventListener('click', () => pick('Увеличить на ' + v + ' ₽'));
        box.appendChild(b);
      });
      const off = el('button', 'ask-opt', 'Отключить лимит');
      off.addEventListener('click', () => pick('Отключить лимит'));
      const stop = el('button', 'ask-opt bad', 'Остановить');
      stop.addEventListener('click', () => pick('Остановить'));
      box.appendChild(off);
      box.appendChild(stop);
      node.body.insertBefore(card, ui.statusEl);
      sfx('notify');
      scrollDown(true);
      break;
    }

    case 'budget_off': {
      // лимит отключён из ответа агента: гасим кнопку и в поле ввода
      S.budgetRub = null;
      if (budgetBtn) budgetBtn.classList.remove('on');
      toast('Лимит отключён — работаю без ограничений', 'info', 'Лимит');
      break;
    }

    case 'budget_update': {
      S.budgetRub = ev.limit || S.budgetRub;
      toast('Лимит увеличен до ' + ev.limit + ' ₽', 'success', 'Лимит');
      break;
    }

    case 'mode_request': {
      // Джарвис просит включить режим: короткое «зачем» сверху, ПОД текстом —
      // ИМЕНОВАННЫЙ тумблер: иконка + название + выключенный круглешок.
      // Сразу видно, что это за функция и что она отключена — тумблер читается
      // как «можно активировать», в отличие от безымянной кнопки. Каждый режим
      // — свой цвет. Мелкая «Пропустить» рядом.
      reactor('wait');
      flushTools(ui);
      busyMode(ui, ['Жду разрешения', 'режим «' + ev.label + '»'], 1500);
      const MODE_META = {
        agent: { name: 'AGENT', ico: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="8" width="14" height="11" rx="3"/><path d="M12 8V5.4"/><circle cx="12" cy="3.6" r="1.3"/><circle cx="9.2" cy="12.6" r=".9" fill="currentColor" stroke="none"/><circle cx="14.8" cy="12.6" r=".9" fill="currentColor" stroke="none"/><path d="M9.5 16h5"/></svg>',
                 hint: 'автономная работа по плану' },
        camera: { name: 'Камера', ico: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2"/></svg>',
                  hint: 'живое распознавание кадра' },
        computer: { name: 'Компьютер', ico: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><rect x="6.5" y="3" width="11" height="18" rx="5.5"/><path d="M12 7v3.4"/></svg>',
                    hint: 'управление мышью и клавиатурой' },
        budget: { name: 'Лимит ₽', ico: '₽',
                  hint: 'потолок расходов на ответ' },
      };
      const meta = MODE_META[ev.mode] || { name: ev.label || 'режим', ico: '⚡', hint: '' };
      const card = el('div', 'panel-card mode-card m-' + (ev.mode || 'agent'));
      card.innerHTML =
        '<div class="mc-row">' +
          '<div class="mc-text">' +
            '<div class="mc-title">Включить «' + esc(ev.label || 'режим') + '»?</div>' +
            '<div class="mc-reason">' + esc(ev.reason ||
              'Для этой задачи режим сильно упростит работу.') + '</div>' +
          '</div>' +
          '<div class="mc-actions">' +
            '<button class="mc-switch" title="Нажми, чтобы включить">' +
              '<span class="mc-sw-ico">' + meta.ico + '</span>' +
              '<span class="mc-sw-text"><b>' + esc(meta.name) + '</b>' +
                '<small>выключен · ' + esc(meta.hint) + '</small></span>' +
              '<span class="mc-sw-track"><i></i></span>' +
            '</button>' +
            '<button class="mc-skip">Пропустить</button>' +
          '</div>' +
        '</div>';
      const syncSwitchState = () => {
        // тумблер всегда показывает ТЕКУЩЕЕ состояние режима: включили его
        // или нет — при разворачивании карточки из миниатюры это видно сразу
        const on = !!{ agent: S.agentMode, camera: S.cameraOn,
                       computer: S.computerUse, budget: !!S.budgetRub }[ev.mode];
        const track = card.querySelector('.mc-sw-track');
        if (track) track.classList.toggle('on', on);
        const small = card.querySelector('.mc-sw-text small');
        if (small) small.textContent = on ? 'включён' : 'выключен';
      };
      const done = (answer) => {
        if (card.dataset.done === '1') return;
        card.dataset.done = '1';
        api('/api/questions/answer', { id: ev.id, answer });
        // координаты тумблера снимаются ДО свёртывания карточки: после
        // свёртывания элемент скрыт, rect нулевой, а красной волне AGENT
        // нужно родиться именно отсюда — из точки разрешения
        const swEl = card.querySelector('.mc-switch');
        if (swEl && answer === 'Включить' && ev.mode === 'agent') {
          const rect = swEl.getBoundingClientRect();
          if (rect.width || rect.height) S.agentWaveRect = rect;
        }
        const thumb = collapseToThumb(card, {
          cls: 'th-ask', icon: '⚡', title: 'Разрешение: ' + (ev.label || ''),
          sub: ev.reason || '',
          tag: answer === 'Включить' ? 'включено' : 'пропущено',
        });
        // Повторное разворачивание: карточка уже сыграла свою роль. Метку
        // readonly вешаем на САМУ карточку: разворачивать её можно много раз
        // (каждый раз появляется новая миниатюра), и каждый раз карточка
        // обязана быть приглушённой, а тумблер — показывать живой статус.
        card._syncModeSwitch = syncSwitchState;
        card.dataset.readonly = '1';
      };
      card.querySelector('.mc-switch').addEventListener('click', () => done('Включить'));
      card.querySelector('.mc-skip').addEventListener('click', () => done('Не нужно'));
      node.body.insertBefore(card, ui.statusEl);
      sfx('notify');
      scrollDown(true);
      break;
    }

    case 'mode_changed': {
      // Разрешение получено: включаем настоящий тумблер у поля ввода —
      // тот самый, копия которого стояла в карточке.
      if (ev.mode === 'agent') {
        // Текущий run стартовал БЕЗ агентского режима: его объект ui ещё не
        // знает, что режим включили. Без этого план прогона молча
        // выбрасывался — «агент работает без плана».
        if (ui) ui.agentMode = true;
        const t = $('#tgAgent');
        if (t && !t.checked) {
          // волна расходится от тумблера в самой карточке-разрешении
          const origin = node.body.querySelector('.mode-card .mc-switch') || $('#swAgent');
          S.agentWaveOrigin = origin;
          t.checked = true; t.dispatchEvent(new Event('change'));
        }
      } else if (ev.mode === 'computer') {
        if (!S.computerUse) $('#tgComputer').click();
      } else if (ev.mode === 'camera') {
        if (!S.cameraOn) $('#tgCamera').click();
      } else if (ev.mode === 'budget') {
        if (budgetPop) openBudgetPop();
      }
      toast('Режим включён', 'success', 'Разрешение');
      break;
    }

    case 'approval_wait': {
      reactor('wait');
      busyMode(ui, ['Жду твоего решения', 'нужно подтверждение', '· ' + (ev.label || ev.tool || '')], 1500);
      const permission = ev.style === 'permission';
      const critical = !permission && /delete|shell|payment|pay|computer|click|type_text/.test(ev.tool || '');
      const card = el('div', 'panel-card approve-card' +
        (permission ? ' permission' : (critical ? ' critical' : '')));
      card.innerHTML =
        '<div class="ah">' + (permission ? '◇ Можно открыть приложение?' : '⛨ Требуется подтверждение') + '</div>' +
        '<div class="ab"><b>' + esc(ev.label || ev.tool) + '</b><br>' + esc(ev.reason || '') +
        '<div class="s-args" style="margin-top:8px">' + esc(JSON.stringify(ev.args || {}, null, 1)) + '</div></div>' +
        '<div class="approve-actions"><button class="btn primary sm ok">Разрешить</button>' +
        '<button class="btn ' + (permission ? '' : 'danger ') + 'sm no">Не открывать</button>' +
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
      sfx(permission ? 'pop' : 'error');
      toast((ev.label || ev.tool) + ' — нужно твоё разрешение', permission ? 'info' : 'warn',
        permission ? 'Разрешение' : 'Санкция');
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
      // вопрос — граница работы: кухонные инструменты прячутся в папки
      flushTools(ui);
      busyMode(ui, ['Жду твоего ответа', 'выбери вариант выше'], 1500);
      const card = questionCard(ev, (choice) => {
        api('/api/questions/answer', { id: ev.id, answer: choice });
      });
      node.body.insertBefore(card, ui.statusEl);
      sfx('warn');
      scrollDown(true);
      break;
    }

    case 'tool_result': {
      if (ui.silent[ev.id || ev.name]) {
        delete ui.silent[ev.id || ev.name];
        termLine((ev.result && ev.result.ok !== false ? '✓ ' : '✕ ') + ev.name, 'sys');
        break;
      }
      const node = ui.tools[ev.id || ev.name];
      const ok = ev.result && ev.result.ok !== false;
      if (node && node.classList.contains('qt-node')) {
        // ЗАКОНЧИЛ: галочка и время у имени, в поток падает короткая строка
        // результата (видно, ЧТО пришло), инструмент сворачивается в
        // миниатюру. В папку семейство уедет, когда череда этого типа
        // закончится — другой инструмент или текст ответа.
        delete ui.tools[ev.id || ev.name];
        node._tool.ok = ok;
        node._tool.elapsed = ev.elapsed != null ? ev.elapsed : null;
        node._tool.result = ev.result || {};
        node._done = true;
        clearTimeout(node._t);
        qtMark(node, ok, node._tool.elapsed);
        const pour = qtResultLine(node, ev);
        // масса результата дописывается в поток, и лишь через ~1 секунду
        // после её конца инструмент сворачивается в миниатюру
        setTimeout(() => qtMiniaturize(node), pour + 1000);
      } else if (node) {
        // AGENT: спиннер замирает цветом, ✓/✕ у имени, результат внутри
        // карточки; сворачивается чередой в одну групповую карточку
        const run = node.querySelector('.tool-run');
        if (run) { run.style.animation = 'none'; run.style.background = ok ? 'var(--green)' : 'var(--red)'; }
        const kEl = node.querySelector('.k');
        if (kEl) { kEl.className = 'k ' + (ok ? 'tool-ok' : 'tool-err'); kEl.textContent = ok ? '✓' : '✕'; }
        if (ev.elapsed != null) {
          const tEl = node.querySelector('.t');
          if (tEl) tEl.innerHTML += ' <span class="muted" style="font-size:10.5px">· ' + ev.elapsed + 'с</span>';
        }
        const pre = el('pre', 'out');
        pre.textContent = toolResultText(ev.result || {}) || '(пусто)';
        node.inner.appendChild(pre);
        finishToolWait(node);
        node._tool = Object.assign(node._tool || {}, {
          ok, elapsed: ev.elapsed != null ? ev.elapsed : null, result: ev.result || {} });
      }
      termLine((ok ? '✓ ' : '✕ ') + ev.name + (ev.result && ev.result.error ? ' — ' + ev.result.error : ' — ok'),
        ok ? '' : 'err');
      if (ok && ev.name === 'remember') pulseNav('memory', true);
      // инструмент отработал — строка состояния не должна остаться висеть на
      // прошлом действии: пока модель осмысляет результат, так и пишем
      busyMode(ui, [(ok ? 'Готово: ' : 'Не вышло: ') + (ev.label || ev.name),
                    'разбираю результат', 'думаю, что дальше'], 1400);
      break;
    }

    case 'memory_saved': {
      // Локальный writer — настоящий инструментальный факт, хотя сетевого tool
      // call больше нет. Без градиентов и «процесса»: сохранение уже завершилось.
      // Вместо этого — та же анимация, что у файла: карточка перелетает
      // во вкладку «Память», и вкладка коротко мерцает.
      const facts = ev.facts || [];
      const card = memoryTraceCard(facts);
      markBorn(card);
      node.body.insertBefore(card, ui.statusEl);
      const summary = facts.map((fact) => fact.value || '').filter(Boolean).join(', ').slice(0, 80);
      flyToNav(card, '🧠 ' + (summary || 'запомнено'), 'memory');
      collapseSoon(card, {
        cls: 'th-ok', icon: '✓', title: 'Запомнить',
        sub: summary,
        tag: 'готово',
      });
      pulseNav('memory', true);
      scrollDown();
      break;
    }

    case 'file': {
      dotAction(ui.node && ui.node.root);
      ui.files.push(ev);
      const wrap = ui.filesBox || (ui.filesBox = el('div', ''));
      if (!wrap.parentNode) node.body.insertBefore(wrap, ui.statusEl);
      attachFileChip(wrap, ev);
      busyMode(ui, ['Сохраняю файл', '· ' + (ev.name || '')], 1400);
      flyToFiles(wrap.lastElementChild, ev.name);
      pulseNav('files', false);
      sfx('ok');
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
      // ПОШЁЛ ТЕКСТ — прошлое семейство инструментов закончило работу:
      // миниатюры уезжают в папку вальсом, и ПЕЧАТЬ ЖДЁТ окончания этой
      // анимации + ещё пол-секунды: текст не должен затирать свёртку
      if (!ui.agentMode && !ui._qtSwept) {
        ui._qtSwept = true;
        const folded = qtSweep(ui, null);
        if (folded > 0) {
          // вальс должен успеть показаться, но не задерживать ответ:
          // хвост последней свёртки + короткая пауза — и текст пошёл
          ui._qtHold = performance.now() + (folded - 1) * 150 + 480;
        }
      }
      if (!ui.mdEl) {
        dropStatus(ui);
        if (ui.thinkCard) ui.thinkCard.classList.remove('live');
        // пошёл ответ — ход мыслей сразу убираем, чтобы не мешал читать
        if (ui.thinkCard && ui.thinkCard.isConnected) {
          if (ui.thinkCard.classList.contains('qt-think')) {
            // Y: тихий дизайн сворачивается как строка кухни
            const mk = ui.thinkCard.querySelector('.qt-mark');
            if (mk) mk.textContent = '✓';
            qtMiniaturize(ui.thinkCard);
          } else {
            const ts0 = thinkFlush(ui.thinkCard);
            collapseSoon(ui.thinkCard, {
              cls: 'th-think', icon: ICO.think, title: 'Ход мыслей',
              sub: ts0 ? fmtSize((ts0.textContent || '').length) : '',
            });
          }
        }
        ui.mdEl = el('div', 'md typing');
        node.body.appendChild(ui.mdEl);
      }
      typeInto(ui, ev.text);
      // AA: строку, воскресшую стражем во время сетевой паузы, убирает сам
      // текст: каретка вернулась в печать, дублирующее «думаю…» не нужно
      if (ui.statusEl && ui.statusEl._watchLine && ui.typer) dropStatus(ui);
      break;
    }

    case 'reply_ui': {
      // Событие может прийти между двумя delta. Поэтому сначала сохраняем его,
      // а монтируем только на визуальной границе shown === buffer. Если после
      // раннего mount придёт ещё текст, typeInto снова отложит ту же панель.
      const spec = String(ev.spec || '').trim();
      if (!hasMeaningfulUiItems(parseUiSpec(spec))) break;
      ui.replyUiSpec = spec;
      ui.pendingReplyUi = spec;
      if (ui.replyLive && ui.replyLive.isConnected) ui.replyLive.remove();
      ui.replyLive = null;
      if (spec) flushPendingReplyUi(ui);
      break;
    }

    case 'reset': {
      // сервер понял, что модель напечатала вызов инструмента текстом,
      // и просит стереть уже показанное — начинаем ответ заново
      typerStop(ui);
      ui.fastFinish = false;
      ui.buffer = ''; ui.shown = ''; ui.frozen = null;
      ui.freezeLocked = false;
      ui.replyUiSpec = '';
      ui.pendingReplyUi = '';
      if (ui.replyLive && ui.replyLive.isConnected) ui.replyLive.remove();
      ui.replyLive = null;
      /* BJ: метки режимов ПЕРЕЖИВАЮТ перезапуск ответа: уведомление стоит
         на месте включения и не имеет права исчезнуть вместе с текстом.
         До начала новой печати оно живёт в теле ответа — перед строкой
         статуса, куда вернётся и новая печать (после метки) */
      if (ui.marksEl && ui.mdEl && ui.mdEl.contains(ui.marksEl)) {
        const st = (ui.statusEl && ui.node.body.contains(ui.statusEl))
          ? ui.statusEl : null;
        if (st) ui.node.body.insertBefore(ui.marksEl, st);
        else ui.node.body.appendChild(ui.marksEl);
      }
      if (ui.mdEl) { ui.mdEl.remove(); ui.mdEl = null; }
      if (!ui.statusEl) {
        ui.statusEl = el('div', 'thinking-line');
        node.body.appendChild(ui.statusEl);
        busyMode(ui, ['Переигрываю', 'беру инструмент', 'делаю по-настоящему'], 1300);
      }
      break;
    }

    case 'done': {
      dropStatus(ui);
      // ответ завершён: всё открытое прячется в папки немедленно
      flushTools(ui);
      if (ev.tier) ui.routeTier = ev.tier;
      if (ev.model) ui.modelName = ev.model;
      updateResponseMeta(ui);
      // План НЕ завершается здесь: dock остаётся живым, пока ответ
      // допечатывается. Зеленеет и уезжает во вкладку только из финала
      // typer (queueResponseFinish → onTyped) — иначе план «выполнен»
      // раньше, чем прочитан сам ответ. undockPlan идемпотентен.
      queueResponseFinish(ui, ev.content || ui.buffer, true);
      break;
    }

    case 'error':
      showError(ui, ev.error || 'неизвестная ошибка');
      flushTools(ui);
      queueResponseFinish(ui, ui.buffer, false);
      break;

    case 'end':
      dropStatus(ui);
      // конец потока: что не закрыл done, закрываем здесь — кухня не должна
      // остаться раскрытой после ответа
      flushTools(ui);
      // имя диалога могло прийти фоном с опозданием (или не прийти в этот
      // поток вообще — обрыв): обновим список ещё пару раз, «…» не зависнет
      [1600, 4200].forEach((ms) => setTimeout(() => {
        const cur = (S.chats || []).find((c) => c.id === S.chatId);
        if (cur && (!cur.title || cur.title === 'Новый диалог')) loadChats();
      }, ms));
      // end означает только конец SSE. Если done потерялся, всё равно дренируем
      // локальный буфер; Stop → Send переключит finally после visualDonePromise.
      if (!ui.doneReceived) queueResponseFinish(ui, ui.buffer, false);
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
  // бросок в панель «Файлы» — это импорт в песочницу (его обрабатывает
  // сетка файлов), а не вложение к сообщению
  if (e.target && e.target.closest && e.target.closest('#view-files')) return;
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
    pulseNav('files', false);
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
/* Y: ЗАПИСЬ → WAV 16 кГц МОНО ПРЯМО В БРАУЗЕРЕ. decodeAudioData понимает
   и webm/opus (Chrome), и mp4/aac (Safari); OfflineAudioContext сам
   приводит к 16 кГц; дальше — честный RIFF-заголовок и 16-бит PCM.
   Никакого ffmpeg, никакого «формата, который не примет ни одна
   слуховая модель». */
async function blobToWav16k(blob) {
  const buf = await blob.arrayBuffer();
  const AC = window.AudioContext || window.webkitAudioContext;
  const actx = new AC();
  let decoded;
  try {
    decoded = await actx.decodeAudioData(buf);
  } finally {
    try { actx.close(); } catch (e) { /* уже закрыт */ }
  }
  const rate = 16000;
  const OAC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
  const off = new OAC(1, Math.max(1, Math.ceil(decoded.duration * rate)), rate);
  const src = off.createBufferSource();
  src.buffer = decoded;
  src.connect(off.destination);
  src.start();
  const rendered = await off.startRendering();
  const pcm = rendered.getChannelData(0);
  const bytes = new ArrayBuffer(44 + pcm.length * 2);
  const v = new DataView(bytes);
  const wstr = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  wstr(0, 'RIFF'); v.setUint32(4, 36 + pcm.length * 2, true); wstr(8, 'WAVE');
  wstr(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true);
  v.setUint16(34, 16, true); wstr(36, 'data'); v.setUint32(40, pcm.length * 2, true);
  let o = 44;
  for (let i = 0; i < pcm.length; i++, o += 2) {
    const s = Math.max(-1, Math.min(1, pcm[i]));
    v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
  }
  let bin = '';
  const u8 = new Uint8Array(bytes);
  for (let i = 0; i < u8.length; i += 0x8000) {
    bin += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
  }
  return 'data:audio/wav;base64,' + btoa(bin);
}


/* AA: КНОПКА МИКРОФОНА — ЭТО РЕЖИМ РАЗГОВОРА. Отдельная кнопка рядом
   была лишней (и из-за дубля id вообще не реагировала): диктовка в поле
   уступила место живому диалогу — говоришь, Джарвис отвечает голосом,
   разговор пишется в текущий диалог. */
$('#micBtn').addEventListener('click', () => {
  if (VOICE.open) closeVoiceMode(); else openVoiceMode();
});

/* ============================ ГОЛОСОВОЙ РЕЖИМ ============================
   AB: РАЗГОВОР — ОБЛАСТЬ, КАК КАМЕРА, а не окно на весь экран. Кнопка
   микрофона открывает карточку в ленте: орб (статус — ТОЛЬКО цвет и пульс),
   тумблер «Контекст диалога» и кнопка завершения. Во время разговора
   НИКАКОГО текста — мы просто слышим друг друга. Живая камера и разговор
   сливаются в ОДИН интерфейс: поле разговора переезжает в область ответов
   камеры. Разговор — классическое общение: инструментов, панелей, планов
   и режимов нет В ПРИНЦИПЕ (это решает сервер). Перебой — задача №1:
   пока Джарвис говорит, микрофон слушает человека.
   Схема: слушаю → тишина 1.4с = конец фразы → распознаю → думаю → говорю
   ПРЕДЛОЖЕНИЯМИ, не дожидаясь конца генерации → снова слушаю. */
const VOICE = { open: false, phase: 'idle', rec: null, chunks: [], stream: null,
                ctx: null, an: null, raf: 0, heard: false, lastVoice: 0,
                startedAt: 0, pending: '', barge: 0, ctxOn: true, chatId: '', nodes: [] };
let VOICE_RU = null;

/* Контекст диалога: включён — беседа пишется в текущий диалог; выключен —
   в изолированный служебный разговор (как у камеры, вне списка диалогов). */
/* AC: по умолчанию контекст ВЫКЛЮЧЕН — разговор не подхватывает переписку
   диалога сам; включается вручную и выбор запоминается. */
function voiceCtxOn() {
  try { return localStorage.getItem('jarvisVoiceCtx') === '1'; } catch (e) { return false; }
}

function voiceRu() {
  if (VOICE_RU) return VOICE_RU;
  try {
    const vs = window.speechSynthesis ? (window.speechSynthesis.getVoices() || []) : [];
    VOICE_RU = vs.find((v) => /^ru/i.test(v.lang || '')) || null;
  } catch (e) { /* голосов нет — скажет системным */ }
  return VOICE_RU;
}
if (window.speechSynthesis) {
  window.speechSynthesis.onvoiceschanged = () => { VOICE_RU = null; };
}

/* Статус — ТОЛЬКО ЦВЕТ И ПУЛЬС ОРБА: голубой дышит — слушает, золотой
   пульсирует — думает, зелёный частит — говорит. Никакого текста. */
function voiceSetPhase(p) {
  VOICE.phase = p;
  if (S.voiceBox) S.voiceBox.className = 'voice-box ' + (p === 'idle' ? 'listening' : p);
}

function buildVoiceCard() {
  const card = el('div', 'msg msg-ai voice-msg');
  card.innerHTML =
    '<div class="ai-avatar">' + AVATAR_CORE + '</div>' +
    '<div class="ai-body"><div class="ai-name">JARVIS<span class="ai-model"> · разговор</span></div>' +
    '<div class="ai-content"><div class="voice-live"></div></div></div>';
  return card;
}

function voiceField() {
  const f = el('div', 'voice-box listening');
  f.innerHTML =
    '<div class="v-orb"><i class="v-ring r1"></i><i class="v-ring r2"></i><b></b></div>' +
    '<div class="v-side">' +
      '<label class="cam-link voice-ctx"><input type="checkbox"><i></i>' +
        '<span>Контекст диалога</span></label>' +
      '<button class="voice-stop" aria-label="Завершить разговор" title="Завершить разговор">✕</button>' +
    '</div>' +
    '<div class="voice-transcript" hidden></div>';
  return f;
}

/* ПОЛЕ РАЗГОВОРА ЖИВЁТ в своей карточке, а при включённой камере — в её
   области ответов: один интерфейс, одна сцена. Камера выключилась —
   поле возвращается в собственную карточку. */
function voiceMount(forceOwn) {
  let host = null;
  const camChat = (!forceOwn && camLive() && S.camNode)
    ? S.camNode.querySelector('.cam-chat') : null;
  if (camChat) {
    host = camChat;          // единый интерфейс камеры и разговора
  } else {
    if (!S.voiceNode || !S.voiceNode.isConnected) {
      S.voiceNode = buildVoiceCard();
      stream().appendChild(S.voiceNode);
    }
    host = S.voiceNode.querySelector('.voice-live');
  }
  if (!S.voiceBox || !S.voiceBox.isConnected) {
    S.voiceBox = voiceField();
    S.voiceBox.querySelector('.voice-stop').addEventListener('click', closeVoiceMode);
    const ctx = S.voiceBox.querySelector('.voice-ctx input');
    ctx.checked = VOICE.ctxOn;
    ctx.addEventListener('change', () => {
      VOICE.ctxOn = ctx.checked;
      try { localStorage.setItem('jarvisVoiceCtx', VOICE.ctxOn ? '1' : '0'); } catch (e) {}
      blip(VOICE.ctxOn);
    });
  }
  host.appendChild(S.voiceBox);
  voiceSetPhase(VOICE.phase === 'idle' ? 'listening' : VOICE.phase);
  scrollDown(true);
}

/* AC: ИСТОРИЯ БЕСЕДЫ — текстом. Разговор всегда живёт в своём диалоге,
   поэтому транскрипт доступен и в открытой вкладке, и в свёрнутой карточке
   (тёмная миниатюра — клик, полистать диалог, как у камеры). Во время самой
   беседы текста нет: с первой новой фразы транскрипт прячется. */
async function voiceRenderTranscript(box) {
  if (!box || !VOICE.chatId) return;
  box.hidden = false;
  box.innerHTML = '';
  try {
    const r = await api('/api/messages?chat_id=' + encodeURIComponent(VOICE.chatId));
    if (!r.ok) return;
    const msgs = (r.messages || []).slice(-40);
    if (!msgs.length) { box.hidden = true; return; }
    box.innerHTML = msgs.map((m) =>
      '<div class="vt-line' + (m.role === 'user' ? ' vt-user' : '') + '">' +
      '<b>' + (m.role === 'user' ? 'Ты' : 'JARVIS') + '</b>' +
      esc(String(m.content || '').slice(0, 600)) + '</div>').join('');
  } catch (e) { /* истории нет — молча */ }
}

function voiceLoadTranscript() {
  const box = S.voiceBox && S.voiceBox.querySelector('.voice-transcript');
  if (box) voiceRenderTranscript(box);
}

async function openVoiceMode() {
  if (VOICE.open) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    toast('Браузер не даёт доступ к микрофону', 'error');
    return;
  }
  VOICE.open = true;
  VOICE.nodes = [];
  VOICE.ctxOn = voiceCtxOn();
  // AG: КАЖДЫЙ ЗВОНОК — НОВЫЙ РАЗГОВОР. Прежний код восстанавливал id
  // прошлого диалога разговора из localStorage, и новая вкладка показывала
  // СТАРУЮ беседу (даже в новом диалоге). Звонок положил — разговор закрыт;
  // следующий звонок начинается с чистого листа.
  VOICE.chatId = '';
  beep(760, 0.08);
  showView('chat');
  killWelcome();
  voiceMount();
  const mb = $('#micBtn');
  if (mb) mb.classList.add('rec');      // кнопка микрофона «дышит», пока идёт разговор
  try {
    // AC: КОРЕНЬ «недоговаривает и прерывается» — ЭХО. Голос Джарвиса из
    // колонок попадал в микрофон, детектор перебоя слышал «человека» и
    // обрывал синтез на полуслове. Просим у браузера честную обработку:
    // echoCancellation убирает собственный вывод, noiseSuppression — фон.
    VOICE.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (e) {
    closeVoiceMode();
    toast('Нет доступа к микрофону', 'error');
    return;
  }
  voiceListen();
}

function closeVoiceMode() {
  VOICE.open = false;
  cancelAnimationFrame(VOICE.raf);
  try { if (VOICE.rec && VOICE.rec.state === 'recording') VOICE.rec.stop(); } catch (e) { /* уже мёртв */ }
  VOICE.rec = null;
  if (VOICE.stream) { VOICE.stream.getTracks().forEach((t) => t.stop()); VOICE.stream = null; }
  if (VOICE.ctx) { try { VOICE.ctx.close(); } catch (e) { /* уже закрыт */ } VOICE.ctx = null; VOICE.an = null; }
  try { window.speechSynthesis.cancel(); } catch (e) { /* синтеза нет */ }
  VOICE.chunks = []; VOICE.pending = ''; VOICE.barge = 0;
  const mb = $('#micBtn');
  if (mb) mb.classList.remove('rec');
  voiceSetPhase('idle');
  // РАЗГОВОР ОКОНЧЕН — текст беседы возвращается на экран: спрятанные при
  // разговоре ответы проявляются в ленте; изолированную историю можно
  // перечитать, открыв область разговора снова
  VOICE.nodes.forEach((n) => { if (n && n.classList) n.classList.remove('voice-run'); });
  VOICE.nodes = [];
  // AC: КОРЕНЬ «микрофон не уходит» — поле жило в камере, а closeVoiceMode
  // обнулял только ссылки: элемент оставался в DOM. Удаляем САМ ЭЛЕМЕНТ,
  // где бы он ни был смонтирован (своя карточка или область камеры).
  if (S.voiceBox) { S.voiceBox.remove(); S.voiceBox = null; }
  const card = S.voiceNode;
  S.voiceNode = null;
  if (card && card.isConnected) {
    // тёмная неактивная миниатюра, как у камеры: клик разворачивает карточку,
    // и в ней можно полистать беседу текстом
    collapseToThumb(card, { cls: 'th-cam', icon: '🎤', title: 'Разговор', tag: 'завершён' });
    const host = card.querySelector('.voice-live');
    if (host) {
      const tb = el('div', 'voice-transcript');
      host.appendChild(tb);
      voiceRenderTranscript(tb);
    }
  }
}

/* СЛУШАЮ. Запись идёт кусками; уровень звука кормит орб (чуть громче —
   чуть больше) и решает, когда фраза закончилась (слова + 1.4с тишины). */
function voiceListen() {
  if (!VOICE.open || !VOICE.stream) return;
  VOICE.chunks = [];
  VOICE.heard = false;
  VOICE.lastVoice = 0;
  VOICE.startedAt = performance.now();
  const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus']
    .find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || '';
  try {
    VOICE.rec = new MediaRecorder(VOICE.stream, mime ? { mimeType: mime } : undefined);
  } catch (e) { closeVoiceMode(); return; }
  VOICE.rec.ondataavailable = (e) => { if (e.data && e.data.size) VOICE.chunks.push(e.data); };
  VOICE.rec.onstop = voiceTranscribe;
  VOICE.rec.start(250);
  if (!VOICE.ctx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    VOICE.ctx = new AC();
    const src = VOICE.ctx.createMediaStreamSource(VOICE.stream);
    VOICE.an = VOICE.ctx.createAnalyser();
    VOICE.an.fftSize = 1024;
    src.connect(VOICE.an);
  }
  voiceSetPhase('listening');
  const buf = new Uint8Array(VOICE.an.fftSize);
  const tick = () => {
    if (!VOICE.open || VOICE.phase !== 'listening') return;
    VOICE.an.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) { const d = (buf[i] - 128) / 128; sum += d * d; }
    const level = Math.sqrt(sum / buf.length);
    // живой уровень кормит сам орб — он дышит громче вместе с голосом
    if (S.voiceBox) S.voiceBox.style.setProperty('--vl', Math.min(1, level * 4).toFixed(3));
    const now = performance.now();
    if (level > 0.055) { VOICE.heard = true; VOICE.lastVoice = now; }
    if (VOICE.heard && now - VOICE.lastVoice > 1400) { voiceStopRec(); return; }
    if (now - VOICE.startedAt > 30000) { voiceStopRec(); return; }   // страховка от вечной записи
    VOICE.raf = requestAnimationFrame(tick);
  };
  VOICE.raf = requestAnimationFrame(tick);
}

function voiceStopRec() {
  cancelAnimationFrame(VOICE.raf);
  if (VOICE.rec && VOICE.rec.state === 'recording') {
    try { VOICE.rec.stop(); } catch (e) { voiceListen(); }
  } else if (VOICE.phase === 'listening') {
    voiceListen();
  }
}

async function voiceTranscribe() {
  if (!VOICE.open) return;
  voiceSetPhase('thinking');
  const blob = new Blob(VOICE.chunks, { type: (VOICE.rec && VOICE.rec.mimeType) || 'audio/webm' });
  VOICE.rec = null;
  if (!blob.size || blob.size < 1200) { voiceRetry(); return; }
  let payload = '';
  try {
    payload = await blobToWav16k(blob);
  } catch (e) {
    payload = await new Promise((res) => {
      const fr = new FileReader();
      fr.onload = () => res(fr.result);
      fr.readAsDataURL(blob);
    });
  }
  const r = await api('/api/transcribe', { audio: payload, language: 'ru' });
  if (!VOICE.open) return;
  if (r.ok && r.text && r.text.trim()) {
    voiceAsk(r.text.trim());
  } else {
    voiceRetry();
  }
}

/* Не расслышал — никаких надписей: орб просто возвращается к слушанию. */
function voiceRetry() {
  if (!VOICE.open) return;
  setTimeout(() => { if (VOICE.open && VOICE.phase === 'thinking') voiceListen(); }, 900);
}

/* Спрашиваю Джарвиса: разговор — КЛАССИЧЕСКОЕ ОБЩЕНИЕ (голосом), ответы
   прячутся до конца беседы, а поток идёт нам — говорим предложениями
   по мере генерации. */
async function voiceAsk(text) {
  voiceSetPhase('thinking');
  // пошла новая беседа — текст прошлой прячем: только голос
  const tr = S.voiceBox && S.voiceBox.querySelector('.voice-transcript');
  if (tr) tr.hidden = true;
  VOICE.pending = '';
  try {
    await send({
      text, voice: true, silent: true,
      onDelta: (chunk) => { if (chunk) voiceFeed(chunk); },
      onDone: (content) => {
        if (!content) voiceAfterSpeak();
      },
    });
  } catch (e) { voiceAfterSpeak(); }
  voiceAfterSpeak();
}

/* Готовые предложения уходят в речь сразу; длинный кусок без знаков
   режется по слову, чтобы Джарвис не молчал полминуты. */
function voiceFeed(chunk) {
  VOICE.pending += chunk;
  let out = '';
  let idx;
  while ((idx = VOICE.pending.search(/[.!?…](\s|$)/)) >= 0) {
    out += VOICE.pending.slice(0, idx + 1) + ' ';
    VOICE.pending = VOICE.pending.slice(idx + 1).replace(/^\s+/, '');
  }
  const flat = VOICE.pending.replace(/\s+/g, ' ').trim();
  if (flat.length >= 170) {
    const sp = flat.lastIndexOf(' ', 150);
    if (sp > 30) {
      out += flat.slice(0, sp) + '… ';
      VOICE.pending = flat.slice(sp + 1);
    }
  }
  if (out) voiceSpeak(out.trim());
}

function voiceSpeak(text) {
  if (!VOICE.open || !text) return;
  try {
    const u = new SpeechSynthesisUtterance(text);
    u.lang = 'ru-RU';
    const v = voiceRu();
    if (v) u.voice = v;
    u.rate = 1.04;
    u.pitch = 1;
    u.onstart = () => {
      if (VOICE.phase !== 'speaking') {
        voiceSetPhase('speaking');
        cancelAnimationFrame(VOICE.raf);
        VOICE.raf = requestAnimationFrame(voiceBargeLoop);
      }
    };
    u.onend = () => { voiceAfterSpeak(); };
    u.onerror = () => { voiceAfterSpeak(); };
    window.speechSynthesis.speak(u);
  } catch (e) { voiceAfterSpeak(); }
}

/* ПЕРЕБОЙ — ЗАДАЧА №1: пока Джарвис говорит, микрофон слушает. Услышал
   человека (~250мс речи) — замолкает и начинает слушать фразу. */
function voiceBargeLoop() {
  if (!VOICE.open || VOICE.phase !== 'speaking') return;
  let level = 0;
  if (VOICE.an) {
    const buf = new Uint8Array(VOICE.an.fftSize);
    VOICE.an.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) { const d = (buf[i] - 128) / 128; sum += d * d; }
    level = Math.sqrt(sum / buf.length);
    if (S.voiceBox) S.voiceBox.style.setProperty('--vl', Math.min(1, level * 4).toFixed(3));
  }
  // AC: порог поднят и требует УСТОЙЧИВЫЙ звук (~110мс): колоночное эхо
  // после echoCancellation — тихое и короткое, живой голос у микрофона —
  // громкий и продолжительный. Перебий остаётся мгновенным для человека.
  if (level > 0.16) {
    VOICE.barge += 1;
    if (VOICE.barge >= 7) {
      VOICE.barge = 0;
      try { window.speechSynthesis.cancel(); } catch (e) { /* синтеза нет */ }
      voiceListen();
      return;
    }
  } else {
    VOICE.barge = 0;
  }
  VOICE.raf = requestAnimationFrame(voiceBargeLoop);
}

/* Договорил? Проверяем: генерация закончена, хвост текста озвучен,
   синтезатор свободен — тогда снова слушаем. */
function voiceAfterSpeak() {
  if (!VOICE.open) return;
  if (S.streaming) return;                       // ответ ещё пишется
  const flat = VOICE.pending.trim();
  if (flat) { VOICE.pending = ''; voiceSpeak(flat); return; }   // озвучить хвост
  setTimeout(() => {
    if (!VOICE.open || S.streaming) return;
    if (window.speechSynthesis && window.speechSynthesis.speaking) return;
    if (VOICE.phase !== 'listening') voiceListen();
  }, 550);
}

/* AA: у режима разговора ровно одна кнопка — микрофон в композере. */
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && VOICE.open) closeVoiceMode();
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
    '<div class="ai-avatar">' + AVATAR_CORE + '</div>' +
    '<div class="ai-body"><div class="ai-name">JARVIS<span class="ai-model"> · зрение</span></div>' +
    '<div class="ai-content">' +
      '<div class="cam-live">' +
        '<div class="cam-col-left">' +
          '<div class="cam-wrap">' +
            '<video class="cam-video" autoplay playsinline muted></video>' +
            '<div class="cam-scan"></div>' +
            '<div class="cam-corners"><i></i><i></i><i></i><i></i></div>' +
            '<div class="cam-hud"><span class="cam-rec"></span><span class="cam-state">включаю камеру…</span></div>' +
          '</div>' +
          '<div class="cam-note muted">Смотрю трансляцию и комментирую справа. ' +
          'Спроси прямо в чате — «что это?», «где купить» — отвечу по тому, что сейчас в кадре.</div>' +
          // По умолчанию камера — отдельный разговор: болтовня «вижу кружку»
          // не должна засорять основной диалог. Но иногда кадр нужен именно
          // как продолжение беседы — тогда этот тумблер подцепляет контекст.
          '<label class="cam-link"><input type="checkbox"><i></i>' +
          '<span>Контекст диалога</span></label>' +
        '</div>' +
        '<div class="cam-col-right">' +
          '<div class="cam-feed"></div>' +
          '<div class="cam-chat"></div>' +
        '</div>' +
      '</div>' +
    '</div></div>';
  return card;
}

async function startCam() {
  // Уже ИДЁТ трансляция — повторный клик ничего не дублирует. Одного наличия
  // camNode недостаточно: после отказа permission карточка остаётся с ошибкой,
  // и именно старый `if (S.camNode) return` навсегда блокировал повторную попытку.
  if (S.camStream) return;
  showView('chat');
  killWelcome();

  // Потерянный/disconnected узел не является живым сеансом.
  if (S.camNode && !S.camNode.isConnected) S.camNode = null;
  if (!S.camNode) {
    // Каждое новое окно камеры — чистый изолированный разговор.
    S.camChatId = null;
    S.camLink = false;
    S.camLast = '';
    S.camNode = buildCamCard();
    stream().appendChild(S.camNode);
    const link = S.camNode.querySelector('.cam-link input');
    if (link) {
      link.checked = false;
      link.addEventListener('change', () => {
        S.camLink = link.checked;
        blip(link.checked);
        camSay(link.checked
          ? 'Теперь вижу текущий диалог — отвечаю с учётом того, о чём мы говорили.'
          : 'Отвязался от диалога: снова смотрю только на кадр.', 'sys');
      });
    }
    // окно камеры сворачивается кликом по любому пустому месту (не по видео)
    addFoldButton(S.camNode, { cls: 'th-cam', icon: ICO.cam, title: 'Камера', tag: 'свёрнута' });
    scrollDown(true);
  }
  // AB: ЕДИНЫЙ ИНТЕРФЕЙС — при живом разговоре его поле переезжает
  // в область ответов камеры: одна сцена, один разговор
  if (VOICE.open) voiceMount();
  // Идёт ответ — он продолжится В камере, а не позади её карточки
  if (S.streaming && S.followUi) adoptRunIntoCam();

  const run = ++S.camRun;
  camState('включаю камеру…', false);
  try {
    // Держим stream локально до проверки поколения. Если пользователь успел
    // выключить камеру или сменить чат, поздний Promise обязан сразу отпустить
    // hardware, а не воскресить невидимый старый сеанс.
    const media = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: 'environment', width: { ideal: 1280 } }, audio: false,
    });
    if (run !== S.camRun || !S.cameraOn || !S.camNode || !S.camNode.isConnected) {
      media.getTracks().forEach((t) => t.stop());
      return;
    }
    S.camStream = media;
    const video = camPart('.cam-video');
    if (video) video.srcObject = media;
    // macOS/Safari может завершить track сам (смена устройства, системная
    // блокировка). Сбрасываем именно активное поколение — иначе camStream
    // остаётся truthy и следующий startCam ошибочно считает камеру включённой.
    media.getVideoTracks().forEach((track) => track.addEventListener('ended', () => {
      if (run !== S.camRun || S.camStream !== media) return;
      if (S.camTimer) { clearInterval(S.camTimer); S.camTimer = null; }
      S.camStream = null;
      if (video) video.srcObject = null;
      S.camPrevPix = null;
      S.cameraOn = false;
      const toggle = $('#tgCamera');
      if (toggle) toggle.classList.remove('on');
      camState('трансляция остановлена · включи снова', false);
    }));
    camState('трансляция · смотрю', true);
    camSay('Камера включена. Смотрю, что происходит.', 'sys');
    sfx('start');
    S.camPrevPix = null;
    if (S.camTimer) clearInterval(S.camTimer);
    S.camTimer = setInterval(camTick, CAM_TICK);
  } catch (e) {
    // Ошибка уже отменённого запроса не имеет права портить новый сеанс.
    if (run !== S.camRun) return;
    S.camStream = null;
    S.cameraOn = false;
    const toggle = $('#tgCamera');
    if (toggle) toggle.classList.remove('on');
    camState('нет доступа к камере · можно повторить', false);
    camSay('Не получилось включить камеру: браузер не дал доступ. Разреши камеру для этого сайта и включи её снова.', 'err');
    toast('Нет доступа к камере', 'error');
  }
}

function stopCam() {
  // Отменяет и уже работающую трансляцию, и ещё не завершившийся getUserMedia.
  ++S.camRun;
  // AB: разговор переживает выключение камеры — поле уезжает обратно
  // в собственную карточку прежде, чем камера свернётся
  if (VOICE.open) voiceMount(true);
  if (S.camTimer) { clearInterval(S.camTimer); S.camTimer = null; }
  if (S.camStream) { S.camStream.getTracks().forEach((t) => t.stop()); S.camStream = null; }
  // Ответы камеры живут В её карточке: свернулись вместе с ней, развернул —
  // увидел разговор. В основную ленту они выходят только когда камера была
  // ПРИВЯЗАНА к текущему диалогу (те ответы и так сохранены в нём).
  if (S.camLink) releaseRunFromCam();
  const node = S.camNode;
  const v = node && node.querySelector('.cam-video');
  if (v) v.srcObject = null;
  if (node) {
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
  S.camBusy = false;
  S.cameraOn = false;
  const toggle = $('#tgCamera');
  if (toggle) toggle.classList.remove('on');
}

function camState(text, live) {
  const st = camPart('.cam-state');
  if (st) st.textContent = text;
  const wrap = camPart('.cam-live');
  if (wrap) wrap.classList.toggle('live', !!live);
}

function camSay(text, kind) {
  const feed = camPart('.cam-feed');
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

/* ОТВЕТ ПРОДОЛЖАЕТСЯ В КАМЕРЕ. Джарвис предложил включить камеру (или
   пользователь включил её сам), и ответ печатается дальше — но в основной
   ленте, которую закрыла карточка камеры: человек физически не видит, что
   агент пишет. Берём ЖИВОЙ ответ (и его вопрос) и переносим в диалог
   камеры: печать, инструменты и статус продолжаются там, где их видно. */
function adoptRunIntoCam() {
  try {
    const chat = camPart('.cam-chat');
    const run = S.followUi;
    if (!chat || !run || !run.node || !run.node.root || !run.node.root.isConnected) return;
    const root = run.node.root;
    if (root.closest('.cam-chat')) return;            // уже живёт в камере
    if (!stream() || !stream().contains(root)) return; // чужой host не трогаем
    const userNode = root.previousElementSibling;
    if (userNode && userNode.classList.contains('msg') && userNode.classList.contains('msg-user')) {
      chat.appendChild(userNode);
    }
    chat.appendChild(root);
    // follow-слушатель колёсика был повешен на старый box: в камере свой
    // скролл-контейнер, вешаем и на него (дубль по box не случится — guard)
    watchRunFollow(run);
    scrollDown(true, run);
  } catch (e) { /* перенос ответа не должен ломать камеру */ }
}

/* Камера выключается: принятые в неё сообщения возвращаются в основную
   ленту — карточка камеры сворачивается в строку, и ответ не должен
   исчезнуть вместе с ней. */
function releaseRunFromCam() {
  try {
    const card = S.camNode && S.camNode.isConnected ? S.camNode : null;
    const chat = card ? card.querySelector('.cam-chat') : null;
    if (!chat || !stream()) return;
    let anchor = card.parentNode ? card.nextSibling : null;
    Array.from(chat.children).forEach((child) => {
      if (!child.classList || !child.classList.contains('msg')) return; // cam-строки остаются
      stream().insertBefore(child, anchor);
      anchor = child.nextSibling;
    });
  } catch (e) { /* возврат не должен ломать сворачивание камеры */ }
}

/* текущий кадр как data-url (для отправки модели) */
function camFrame(maxW) {
  const v = camPart('.cam-video');
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
  const v = camPart('.cam-video');
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
  const run = S.camRun;
  const media = S.camStream;
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
    // Ответ старого vision-запроса может прийти уже после закрытия и нового
    // открытия камеры. Поколение и MediaStream обязаны совпасть, иначе старая
    // подпись снова попадёт в свежую карточку.
    if (run !== S.camRun || media !== S.camStream || !camLive()) return;
    const txt = (r.answer || r.content || r.text || '').trim();
    if (r.ok && txt && txt !== S.camLast) { S.camLast = txt; camSay(txt); }
    else if (!r.ok) camState('трансляция · ' + (r.error || 'модель молчит'), true);
    if (r.ok) camState('трансляция · смотрю', true);
  } finally {
    if (run === S.camRun) S.camBusy = false;
  }
}

/* если камера включена — к сообщению в чат автоматически прикладывается текущий кадр */
async function camAttachFrame(chatId, signal) {
  if (!S.camStream) return null;
  const data = camFrame();
  if (!data) return null;
  const r = await api('/api/upload', {
    name: 'camera_' + Date.now() + '.jpg', data, chat_id: chatId || '',
    transient: true,   // AE: живой кадр — не файл диалога
  }, signal ? { signal } : null);
  if (!r.ok || (signal && signal.aborted)) return null;
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
    sfx('stop');
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
/* Заголовок и варианты чистыми: модель пишет «## Что дальше» и вешает
   двоеточия в конец пунктов — человеку нужны слова, а не разметка */
function cleanAskText(s) {
  return String(s || '')
    .replace(/^\s*#{1,6}\s*/gm, '')
    .replace(/\*\*/g, '')
    .replace(/^\s*[-*•]\s+/, '')
    .replace(/[:：]\s*$/, '')
    .trim();
}

function questionCard(ev, onPick) {
  const opts = (ev.options || []).map(cleanAskText).filter(Boolean);
  const card = el('div', 'panel-card ask-card');
  const yesNo = opts.length === 2;
  card.innerHTML =
    '<div class="ask-h"><span class="ask-i">?</span>' + esc(cleanAskText(ev.question)) + '</div>' +
    '<div class="ask-opts"></div>' +
    '<div class="ask-send"><button class="ask-go" disabled>' + ICO.send + '<span>Отправить</span></button></div>';
  const box = card.querySelector('.ask-opts');
  const go = card.querySelector('.ask-go');
  let chosen = '';                    // выбранный вариант: клик лишь подсвечивает
  let ownText = '';                   // свой вариант — полноценная альтернатива
  const pick = (value) => {
    const answer = String(value || '').trim();
    if (!answer || card.dataset.done === '1') return;
    card.dataset.done = '1';
    box.innerHTML = '<span class="ask-picked">✓ ' + esc(answer) + '</span>';
    go.parentNode.remove();
    reactor('busy');            // ответ получен — Джарвис снова за работой
    if (onPick) onPick(answer);
    collapseToThumb(card, { cls: 'th-ask', icon: '?', title: 'Вопрос',
                            sub: ev.question || '', tag: answer });
  };
  const syncGo = () => { go.disabled = !(chosen || ownText.trim()); };
  // ВЫБОР — ЭТО ПОДСВЕТКА, ОТПРАВЛЯЕТ КНОПКА. Клик по варианту больше не
  // отправляет его сразу: человек может передумать и выбрать другой. Каждый
  // тип выбора подсвечивает выбранный элемент классом .sel.
  const choose = (value, btn) => {
    if (card.dataset.done === '1') return;
    chosen = value;
    ownText = '';
    $$('.ask-opt', box).forEach((b) => b.classList.remove('sel'));
    if (btn) btn.classList.add('sel');
    syncGo();
    sfx('select');
  };
  // РАЗНООБРАЗИЕ ВЫБОРА. Один и тот же список кнопок от вопроса к вопросу
  // приучает глаз и делает диалог механическим. Теперь каждый уточняющий
  // вопрос получает СВОЙ тип выбора — как живой собеседник, который каждый
  // раз подаёт варианты иначе: то таблетками, то списком, то сегментом.
  // Смысл (первый — зелёный, последний — красный у пар да/нет) сохраняется
  // в любом оформлении.
  const ASK_STYLES = ['pills', 'stack', 'cloud', 'seg', 'grid', 'dial'];
  S.askStyle = ((S.askStyle || 0) + 1) % ASK_STYLES.length;
  const style = ASK_STYLES[S.askStyle];
  box.classList.add('ask-s-' + style);
  card.classList.add('ask-c-' + style);
  const tone = (i) => yesNo ? (i === 0 ? ' good' : ' bad') : '';
  if (style === 'stack') {
    // вертикальный список: номер + текст + стрелка, во всю ширину
    opts.forEach((o, i) => {
      const b = el('button', 'ask-opt as-stack' + tone(i));
      b.innerHTML = '<i>' + (i + 1) + '</i><span>' + esc(o) + '</span><b>›</b>';
      b.addEventListener('click', () => choose(o, b));
      box.appendChild(b);
    });
  } else if (style === 'cloud') {
    // облако чипов: компактные капсулы без порядка
    opts.forEach((o, i) => {
      const b = el('button', 'ask-opt as-cloud' + tone(i), esc(o));
      b.addEventListener('click', () => choose(o, b));
      box.appendChild(b);
    });
  } else if (style === 'seg') {
    // слитный сегмент: кнопки склеены в одну полосу
    opts.forEach((o, i) => {
      const b = el('button', 'ask-opt as-seg' + tone(i), esc(o));
      b.addEventListener('click', () => choose(o, b));
      box.appendChild(b);
    });
  } else if (style === 'grid') {
    // сетка карточек: у каждого варианта своя ячейка с угловой меткой
    opts.forEach((o, i) => {
      const b = el('button', 'ask-opt as-grid' + tone(i));
      b.innerHTML = '<em>' + String.fromCharCode(65 + i) + '</em><span>' + esc(o) + '</span>';
      b.addEventListener('click', () => choose(o, b));
      box.appendChild(b);
    });
  } else if (style === 'dial') {
    // диск: круглые клавиши с первой буквой, подпись рядом
    opts.forEach((o, i) => {
      const b = el('button', 'ask-opt as-dial' + tone(i));
      b.innerHTML = '<i>' + esc(String(o).trim().charAt(0).toUpperCase() || '?') + '</i>' +
        '<span>' + esc(o) + '</span>';
      b.addEventListener('click', () => choose(o, b));
      box.appendChild(b);
    });
  } else {
    // классические таблетки (pills)
    opts.forEach((o, i) => {
      const b = el('button', 'ask-opt' + tone(i), esc(o));
      b.addEventListener('click', () => choose(o, b));
      box.appendChild(b);
    });
  }
  // У любого живого выбора есть выход из конечного списка. Раньше ```ui уже
  // добавлял «Свой вариант», а блокирующий ask_user — нет; один и тот же
  // контракт интерфейса зависел от того, каким путём пошла модель.
  if (onPick && !ev.answer) {
    const own = el('div', 'ask-own');
    const input = el('input', 'ask-own-i');
    input.type = 'text'; input.placeholder = 'Свой вариант…';
    input.addEventListener('input', () => {
      ownText = input.value;
      if (ownText.trim()) chosen = '';
      $$('.ask-opt', box).forEach((b) => b.classList.remove('sel'));
      syncGo();
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); pick(ownText || chosen); }
    });
    own.appendChild(input); box.appendChild(own);
  }
  // ОТПРАВЛЯЕТ ТОЛЬКО КНОПКА — и выбор из списка, и свой вариант
  go.addEventListener('click', () => pick(ownText.trim() || chosen));
  // если ответ уже был дан раньше (перечитываем историю) — показываем выбор
  if (ev.answer) {
    card.dataset.done = '1';
    box.innerHTML = '<span class="ask-picked">✓ ' + esc(ev.answer) + '</span>';
    go.parentNode.remove();
  }
  return card;
}

/* Варианты продолжения над полем ввода.
   Обычный клик отправляет реплику сразу, клик по «Enter» кладёт её в поле
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
    /* BE: значок Enter вместо карандашика — карандаш читался как
       «редактирование подсказки»; Enter честно говорит «в поле ввода» */
    const ed = el('button', 'rc-ed', '↵');
    ed.title = 'Вставить в поле ввода и дописать';
    go.addEventListener('click', () => {
      box.hidden = true;
      $('#input').value = t;
      autoGrow();
      send();
    });
    ed.addEventListener('click', (e) => {
      /* BF: подсказки НЕ исчезают — реплика легла в поле ввода, а полоса
         ждёт следующего решения (отправить самому или взять другой чип) */
      e.stopPropagation();
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
  // Chips входят по очереди и на каждом animation-delay отнимают немного
  // высоты у ленты. Следуем за нижней границей всё время их роста, а не только
  // один раз до первого chip; wheel/touch внутри helper сразу отменяет follow.
  followGrowingPanel(box, 520 + items.length * 60);
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
/* Общий перелёт «из ответа во вкладку»: файлы летят в «Файлы», запомненные
   факты — в «Память». Одна механика, один визуальный язык. */
function flyToNav(chip, label, view, fallbackToast) {
  const target = document.querySelector('.nav-item[data-view="' + view + '"]');
  if (!chip || !target || !chip.getBoundingClientRect) { if (fallbackToast) toast(fallbackToast, 'success', 'Файл'); return; }
  const a = chip.getBoundingClientRect();
  const b = target.getBoundingClientRect();
  if (!a.width || !b.width) { if (fallbackToast) toast(fallbackToast, 'success', 'Файл'); return; }

  sfx('fly');
  const fly = el('div', 'file-fly');
  fly.textContent = label;
  fly.style.left = a.left + 'px';
  fly.style.top = a.top + 'px';
  fly.style.width = Math.min(a.width, 260) + 'px';
  document.body.appendChild(fly);

  // дуга: сначала вверх и вбок, потом к цели — прямой перелёт выглядит мёртвым
  const dx = (b.left + b.width / 2) - (a.left + Math.min(a.width, 260) / 2);
  const dy = (b.top + b.height / 2) - (a.top + a.height / 2);

  // ДВЕ ОТДЕЛЬНЫЕ ФАЗЫ, а не одна длинная анимация. В прошлый раз пауза была
  // вставлена кадрами внутрь общего полёта — из-за этого растянулся весь
  // перелёт (стал вялым), а сама задержка занимала долю времени и глазом не
  // читалась. Теперь отрыв с зависанием живёт своей короткой жизнью, а полёт
  // остался ровно таким же быстрым, каким нравился.
  const LIFT = 260;    // отрыв от диалога + короткое зависание
  const FLIGHT = 880;  // сам перелёт — прежняя быстрая анимация

  fly.animate([
    { transform: 'translate(0,0) scale(1)', filter: 'brightness(1)', offset: 0 },
    // отделился: приподнялся, чуть подрос и подсветился — видно, что оторвался
    { transform: 'translate(0,-10px) scale(1.05)', filter: 'brightness(1.18)', offset: .55,
      easing: 'cubic-bezier(.2,.9,.3,1)' },
    // висит: тот же кадр — неподвижная пауза перед броском
    { transform: 'translate(0,-10px) scale(1.05)', filter: 'brightness(1.18)', offset: 1 }
  ], { duration: LIFT, fill: 'forwards' }).onfinish = () => {
    fly.animate([
      { transform: 'translate(0,-10px) scale(1.05)', opacity: 1, offset: 0 },
      { transform: 'translate(' + (dx * 0.45) + 'px,' + (dy * 0.35 - 46) + 'px) scale(.78)',
        opacity: .95, offset: .5 },
      { transform: 'translate(' + (dx * 0.92) + 'px,' + (dy * 0.92) + 'px) scale(.42)',
        opacity: .5, offset: .82 },
      { transform: 'translate(' + dx + 'px,' + dy + 'px) scale(.2)', opacity: 0, offset: 1 }
    ], { duration: FLIGHT, easing: 'cubic-bezier(.3,.7,.3,1)' }).onfinish = () => {
      fly.remove();
      target.classList.add('nav-lit');
      setTimeout(() => target.classList.remove('nav-lit'), 900);
    };
  };
}

function flyToFiles(chip, name) {
  flyToNav(chip, '📄 ' + (name || 'файл'), 'files', (name || 'файл') + ' готов');
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

/* Одна строка уведомления. Удаление — кнопкой-крестиком: на компьютере это
   удобнее свайпа. Сама анимация ухода осталась «смахивающей»: строка уезжает
   влево и схлопывается, как это делал бы палец на телефоне. */
function noteRow(n) {
  const kind = n.level === 'error' ? 'error' : (n.level === 'success' ? 'success' : 'info');
  const row = el('div', 'np-item ' + kind + (n.read ? '' : ' fresh'));
  row.innerHTML =
    '<div class="np-face">' +
      '<div class="np-body">' +
        '<div class="np-t">' + esc(n.title || 'Уведомление') + '</div>' +
        '<div class="np-b">' + esc((n.body || '').slice(0, 260)) + '</div>' +
        '<div class="np-time">' + fmtTime(n.created_at) + '</div>' +
      '</div>' +
      '<button class="np-x" title="Удалить">✕</button>' +
    '</div>';
  row.querySelector('.np-x').addEventListener('click', async (e) => {
    e.stopPropagation();
    if (row.dataset.gone === '1') return;
    row.dataset.gone = '1';
    row.style.maxHeight = row.offsetHeight + 'px';   // фиксируем высоту для схлопывания
    row.classList.add('np-gone');
    setTimeout(() => {
      row.remove();
      const list = $('#npList');
      if (list && !list.querySelector('.np-item')) {
        list.appendChild(el('div', 'np-empty', 'Пока пусто'));
      }
    }, 260);
    S.notifications = (S.notifications || []).filter((x) => String(x.id) !== String(n.id));
    await api('/api/notifications/delete', { id: n.id });
  });
  return row;
}

function toggleNotePanel(force) {
  const panel = $('#notePanel');
  if (!panel) return;
  const open = force != null ? force : panel.hidden;
  if (open) {
    panel.classList.remove('np-closing');
    panel.hidden = false;
    renderNotePanel();
    // открыл — значит увидел: гасим счётчик непрочитанного
    api('/api/notifications/read', {}).then(() => { S.unread = 0; renderNotePanel(); });
    return;
  }
  // Закрываем зеркально: панель уезжает тем же движением, каким приехала.
  // Раньше она просто пропадала — открытие было плавным, закрытие рывком.
  if (panel.hidden || panel.dataset.closing === '1') return;
  panel.dataset.closing = '1';
  panel.classList.add('np-closing');
  setTimeout(() => {
    panel.hidden = true;
    panel.classList.remove('np-closing');
    panel.dataset.closing = '';
  }, 200);                              // = длительность npOut в CSS
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
/* ============================ сценарии ============================ */
/* Запуск сценария: новый диалог и последовательная отправка шагов.
   Каждый шаг ждёт полного завершения предыдущего (и печати) — сценарий
   это запись разговора, а не пачка параллельных вопросов. */
async function runScenario(sc) {
  showView('chat');
  const r = await api('/api/chats/new', { title: sc.title || 'Сценарий' });
  if (r.ok && r.chat && r.chat.id) {
    S.chatId = r.chat.id;
    loadChats();
  }
  const steps = sc.steps || [];
  S.scenarioActive = true;
  try {
    for (let i = 0; i < steps.length; i++) {
      if (S.abortedScenario) break;
      await sendScenarioStep(steps[i], i + 1, steps.length);
      if (S.abortedScenario) break;
      if (i < steps.length - 1) await sleep(700);
    }
  } finally {
    // флаг гаснет ВСЕГДА — даже при ошибке шага: иначе «сегодня» молчит
    // до перезагрузки страницы
    S.scenarioActive = false;
    S.abortedScenario = false;
  }
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

/* send() резолвится в finally, когда ответ УЖЕ допечатан (visualDonePromise),
   поэтому шагу достаточно дождаться самого send — здесь был undefined
   wasStreaming, из-за которого логика ожидания не работала вовсе.
   Поллинг ниже — страховка на случай, если контракт send изменится: увидели
   стрим — ждём его конца; не увидели за 3 с — идём дальше. Stop прерывает
   сценарий целиком (S.abortedScenario). */
async function sendScenarioStep(text, step, total) {
  try {
    await send({ text, scenario: true, scenarioStep: step, scenarioTotal: total });
  } catch (e) { /* шаг не прошёл — идём дальше */ }
  await new Promise((resolve) => {
    const t0 = Date.now();
    let saw = false;
    const poll = setInterval(() => {
      if (S.streaming) { saw = true; return; }
      if (!saw || Date.now() - t0 > 3000) { clearInterval(poll); resolve(); }
    }, 200);
  });
}

async function loadScenarios() {
  const grid = $('#scenarioGrid');
  if (!grid) return;
  const r = await api('/api/scenarios');
  const list = (r.ok && r.scenarios) || [];
  // Служебная полоса — ровно как в AUTO: слева статистика, справа действия.
  const stats = $('#scenarioStats');
  if (stats) {
    const stepsTotal = list.reduce((acc, sc) => acc + (sc.steps || []).length, 0);
    const statHtml = [
      ['всего', list.length, ''],
      ['шагов', stepsTotal, 'active'],
    ].map(([label, value, cls]) => '<span class="auto-stat ' + cls + '"><i>' +
      label + '</i><b>' + value + '</b></span>').join('');
    if (stats.innerHTML !== statHtml) stats.innerHTML = statHtml;
  }
  if (!list.length) {
    const empty = el('div', 'empty task-empty',
      '<span class="e-ico">⚡️</span>Сценариев пока нет.<br>' +
      'Нажмите «+ Новый сценарий» — или сохраните частый запрос из диалога.');
    grid.replaceChildren(empty);
    empty.style.gridColumn = '1/-1';
    return;
  }
  grid.replaceChildren();
  list.forEach((sc) => {
    // карточка сценария = карточка задачи AUTO: та же структура .tc-*,
    // те же кнопки .btn.sm. Один язык интерфейса, без самодеятельности.
    const steps = sc.steps || [];
    const card = el('div', 'task-card scenario-card done');
    card.innerHTML =
      '<div class="tc-head">' +
        '<div class="tc-title">' + esc((sc.emoji ? sc.emoji + ' ' : '') + (sc.title || 'Сценарий')) + '</div>' +
        '<div class="tc-state done">' + steps.length + ' шаг' + (steps.length === 1 ? '' : 'ов') + '</div>' +
      '</div>' +
      '<div class="tc-steps">' + steps.map((x) => '• ' + esc(String(x).slice(0, 110))).join('<br>') + '</div>' +
      '<div class="tc-actions"></div>';
    // клик по карточке = полный просмотр: шаги целиком, без обрезки в 110 знаков
    card.addEventListener('click', () => openScenario(sc));
    const acts = card.querySelector('.tc-actions');
    acts.style.cssText = 'display:flex;gap:8px;margin-top:4px';
    const run = el('button', 'btn sm primary', 'Запустить');
    run.addEventListener('click', (e) => { e.stopPropagation(); runScenario(sc); });
    const edit = el('button', 'btn sm', 'Редактировать');
    edit.addEventListener('click', (e) => { e.stopPropagation(); editScenario(sc); });
    const del = el('button', 'btn sm danger', 'Удалить');
    del.addEventListener('click', async (e) => {
      e.stopPropagation();
      await api('/api/scenarios/delete', { id: sc.id });
      loadScenarios();
    });
    acts.appendChild(run);
    acts.appendChild(edit);
    acts.appendChild(del);
    grid.appendChild(card);
  });
}

/* Полный просмотр сценария: все шаги целиком. То же окно Джарвиса, что и у
   предложения сохранить — сценарий это запись разговора, её видно целиком. */
function openScenario(sc) {
  const steps = sc.steps || [];
  modal(
    '<div class="jw-ico">' + esc(sc.emoji || '⚡') + '</div>' +
    '<h3>' + esc(sc.title || 'Сценарий') + '</h3>' +
    '<div class="md-sub">' + steps.length + ' шаг' + (steps.length === 1 ? '' : 'ов') +
      ' — выполняются по очереди в новом диалоге</div>' +
    '<div class="sc-steps-full">' +
      steps.map((x, i) => '<div class="sc-step-full"><i>' + (i + 1) + '</i>' +
        esc(String(x)) + '</div>').join('') +
    '</div>' +
    '<div class="modal-acts">' +
      '<button class="btn" id="scClose">Закрыть</button>' +
      '<button class="btn" id="scEdit">Редактировать</button>' +
      '<button class="btn primary" id="scRun">Запустить</button>' +
    '</div>', null, { soft: true });
  $('#scClose').addEventListener('click', closeModal);
  $('#scEdit').addEventListener('click', () => editScenario(sc));
  $('#scRun').addEventListener('click', () => { closeModal(); runScenario(sc); });
}

/* Редактирование сценария: то же окно создания, но с уже заполненными
   полями — правка привычной формы, а не новый интерфейс. */
function editScenario(sc) {
  modal(
    '<h3>Редактировать сценарий</h3>' +
    '<div class="sd" style="margin-bottom:10px">Каждая строка — отдельное сообщение Джарвису.</div>' +
    '<input id="scTitle" class="bp-input" style="width:100%;margin-bottom:10px" placeholder="Название">' +
    '<input id="scEmoji" class="bp-input" style="width:100%;margin-bottom:10px" placeholder="Эмодзи (необязательно)">' +
    '<textarea id="scSteps" class="bp-input" style="width:100%;min-height:120px;resize:vertical" ' +
    'placeholder="Каждая строка — шаг сценария"></textarea>' +
    '<div class="modal-acts"><button class="btn" id="scCancel">Отмена</button>' +
    '<button class="btn primary" id="scSave">Сохранить</button></div>', null, { soft: true });
  $('#scTitle').value = sc.title || '';
  $('#scEmoji').value = sc.emoji || '';
  $('#scSteps').value = (sc.steps || []).join('\n');
  $('#scCancel').addEventListener('click', closeModal);
  $('#scSave').addEventListener('click', async () => {
    const steps = $('#scSteps').value.split('\n').map((x) => x.trim()).filter(Boolean);
    if (!steps.length) { toast('Нужен хотя бы один шаг', 'warn'); return; }
    const r = await api('/api/scenarios/update', {
      id: sc.id,
      title: $('#scTitle').value.trim() || 'Сценарий',
      emoji: $('#scEmoji').value.trim(),
      steps,
    });
    closeModal();
    if (r.ok) { toast('Сценарий обновлён', 'success'); loadScenarios(); }
  });
}

$('#addScenarioBtn').addEventListener('click', () => {
  modal(
    '<h3>Новый сценарий</h3>' +
    '<div class="sd" style="margin-bottom:10px">Каждая строка — отдельное сообщение Джарвису. ' +
    'Шаги выполняются по очереди в новом диалоге.</div>' +
    '<input id="scTitle" class="bp-input" style="width:100%;margin-bottom:10px" placeholder="Название (например: Дайджест утра)">' +
    '<input id="scEmoji" class="bp-input" style="width:100%;margin-bottom:10px" placeholder="Эмодзи (необязательно)">' +
    '<textarea id="scSteps" class="bp-input" style="width:100%;min-height:120px;resize:vertical" ' +
    'placeholder="Новости технологий за вчера\nПогода в Стокгольме\nСобери всё в короткий дайджест"></textarea>' +
    '<div class="modal-acts"><button class="btn primary" id="scSave">Сохранить</button></div>');
  $('#scSave').addEventListener('click', async () => {
    const steps = $('#scSteps').value.split('\n').map((x) => x.trim()).filter(Boolean);
    if (!steps.length) { toast('Нужен хотя бы один шаг', 'warn'); return; }
    const r = await api('/api/scenarios/new', {
      title: $('#scTitle').value.trim() || 'Сценарий',
      emoji: $('#scEmoji').value.trim(),
      steps,
    });
    closeModal();
    if (r.ok) { toast('Сценарий сохранён', 'success'); loadScenarios(); }
  });
});

/* Предложение сохранить сценарий: третий раз тот же запрос — значит это рутина.
   Ключ — первые слова сообщения: точного совпадения достаточно. */
function maybeOfferScenario(text) {
  try {
    const key = 'sc:' + String(text || '').toLowerCase().split(/\s+/).slice(0, 4).join(' ');
    if (key.length < 8) return;
    const counts = JSON.parse(localStorage.getItem('jarvis.scenarioHints') || '{}');
    counts[key] = (counts[key] || 0) + 1;
    Object.keys(counts).forEach((k) => { if (counts[k] === 0) delete counts[k]; });
    const keys = Object.keys(counts);
    if (keys.length > 40) delete counts[keys[0]];
    localStorage.setItem('jarvis.scenarioHints', JSON.stringify(counts));
    if (counts[key] === 3) {
      toast('Частый запрос — сохранить как сценарий?', 'info', 'Сценарии');
      setTimeout(() => {
        modal(
          '<div class="jw-ico">⚡</div>' +
          '<h3>Сделать сценарий?</h3>' +
          '<div class="sd" style="margin-bottom:10px">Вы уже третий раз отправляете похожий запрос. ' +
          'Сохранить его как сценарий — потом один клик, и Джарвис всё сделает.</div>' +
          '<div class="modal-acts">' +
          '<button class="btn primary" id="scYes">Сохранить</button>' +
          '<button class="btn" id="scNo">Не надо</button></div>', null, { soft: true });
        $('#scNo').addEventListener('click', closeModal);
        $('#scYes').addEventListener('click', async () => {
          const r = await api('/api/scenarios/new', {
            title: String(text || '').split(/\s+/).slice(0, 5).join(' '), emoji: '⚡️', steps: [text],
          });
          closeModal();
          if (r.ok) {
            counts[key] = -999; // больше не предлагаем
            localStorage.setItem('jarvis.scenarioHints', JSON.stringify(counts));
            toast('Сценарий сохранён — вкладка «Сценарии»', 'success');
          }
        });
      }, 900);
    }
  } catch (e) { /* localStorage недоступен — молча пропускаем */ }
}

async function loadTasks() {
  const ticket = ++S.taskLoadRun;
  const r = await api('/api/tasks');
  if (ticket !== S.taskLoadRun) return;
  S.tasks = r.tasks || [];
  S.autoPaused = !!r.paused;
  renderTasks();
}

function taskFingerprint(t) {
  // updated_at намеренно входит в fingerprint: backend меняет его только при
  // реальном status/event/result transition. Сам polling DOM не трогает.
  return JSON.stringify([
    t.title, t.prompt, t.status, t.progress, t.result, t.schedule, t.next_run,
    t.updated_at, t.resume_status, t.events || [],
  ]);
}

function paintTaskCard(card, t) {
  const st = t.status || 'queued';
  const wasOpen = !!card.querySelector('.tc-events.open');
  card.className = 'task-card ' + st;
  card.dataset.taskId = String(t.id);
  const stateRu = {
    queued: 'в очереди', running: 'выполняется', done: 'готово', error: 'ошибка',
    scheduled: 'по расписанию', paused: 'на паузе', cancelled: 'отменена',
  }[st] || st;
  card.innerHTML =
    '<div class="tc-head"><div class="tc-title">' + esc(t.title) + '</div>' +
    '<div class="tc-state ' + st + '">' + stateRu + '</div></div>' +
    '<div class="tc-prompt">' + esc(t.prompt) + '</div>' +
    '<div class="tc-bar"><i style="width:' + Math.round((t.progress || 0) * 100) + '%"></i></div>' +
    (t.schedule ? '<div class="muted" style="font-size:11px;margin-bottom:6px">⟳ ' + esc(t.schedule) +
      (t.next_run ? ' · следующий запуск ' + fmtTime(t.next_run) : '') + '</div>' : '') +
    '<div class="tc-events' + (wasOpen ? ' open' : '') + '"></div>' +
    (t.result ? '<div class="tc-result md">' + MD.render(String(t.result).slice(0, 2500)) + '</div>' : '') +
    '<div class="tc-actions"></div>';

  const evBox = card.querySelector('.tc-events');
  (t.events || []).slice(-40).forEach((e) => {
    evBox.appendChild(el('div', 'tc-ev', esc(typeof e === 'string' ? e : (e.text || JSON.stringify(e)))));
  });
  const acts = card.querySelector('.tc-actions');
  /* BG: клик по кнопке — ТОЛЬКО её функция: карточку не открываем */
  const mk = (label, cls, fn, disabled) => {
    const b = el('button', 'btn sm ' + (cls || ''), label);
    b.disabled = !!disabled;
    b.addEventListener('click', (e) => { e.stopPropagation(); fn(e); });
    acts.appendChild(b);
  };
  if (st !== 'running') {
    mk('Запустить', 'primary', async () => {
      const r = await api('/api/tasks/run', { task_id: t.id });
      if (!r.ok) toast(r.error || 'Задача уже занята', 'warn');
      else toast('Задача запущена', 'info');
      loadTasks();
    }, S.autoPaused);
  } else {
    mk('Отменить', 'ghost', async () => {
      await api('/api/tasks/cancel', { task_id: t.id }); loadTasks();
    });
  }
  // BF: правка задачи — как в сценариях (кроме живого прогона)
  mk('Редактировать', '', () => { editTask(t); }, st === 'running');
  mk('Удалить', 'danger', async () => {
    await api('/api/tasks/delete', { task_id: t.id }); loadTasks();
  });
  // BF: клик по карточке — полный просмотр задачи (как у сценария)
  card.onclick = () => openTask(t);
  card.dataset.fingerprint = taskFingerprint(t);
}

/* BF: полный просмотр задачи — то же окно Джарвиса, что у сценария:
   задание целиком, расписание, статус и последний результат. */
function openTask(t) {
  const stRu = {
    queued: 'в очереди', running: 'выполняется', done: 'готово', error: 'ошибка',
    scheduled: 'по расписанию', paused: 'на паузе', cancelled: 'отменена',
  }[t.status] || t.status;
  modal(
    '<div class="jw-ico">◎</div>' +
    '<h3>' + esc(t.title) + '</h3>' +
    '<div class="md-sub">' + esc(stRu) +
      (t.schedule ? ' · ⟳ ' + esc(t.schedule) : '') +
      (t.next_run ? ' · следующий запуск ' + fmtTime(t.next_run) : '') + '</div>' +
    '<div class="sc-steps-full"><div class="sc-step-full"><i>?</i>' +
      esc(t.prompt) + '</div></div>' +
    (t.result ? '<div class="sd" style="margin:4px 0 10px">Последний результат:</div>' +
      '<div class="tc-result md">' + MD.render(String(t.result).slice(0, 2500)) + '</div>' : '') +
    ((t.events || []).length
      ? '<details class="sd" style="margin-top:10px"><summary style="cursor:pointer">Лог (' +
        t.events.length + ')</summary><div class="tc-events open" style="max-height:220px;overflow:auto">' +
        t.events.slice(-40).map((e) => '<div class="tc-ev">' +
          esc(typeof e === 'string' ? e : (e.text || JSON.stringify(e))) + '</div>').join('') +
        '</div></details>'
      : '') +
    '<div class="modal-acts">' +
      '<button class="btn" id="tkClose">Закрыть</button>' +
      '<button class="btn" id="tkEdit">Редактировать</button>' +
      (t.status !== 'running'
        ? '<button class="btn primary" id="tkRun">Запустить</button>'
        : '<button class="btn primary" id="tkCancel">Отменить</button>') +
    '</div>', null, { soft: true });
  $('#tkClose').addEventListener('click', closeModal);
  $('#tkEdit').addEventListener('click', () => editTask(t));
  const run = $('#tkRun');
  if (run) run.addEventListener('click', () => {
    closeModal();
    api('/api/tasks/run', { task_id: t.id }).then((r) => {
      if (!r.ok) toast(r.error || 'Задача уже занята', 'warn');
      else toast('Задача запущена', 'info');
      loadTasks();
    });
  });
  const cancel = $('#tkCancel');
  if (cancel) cancel.addEventListener('click', () => {
    closeModal();
    api('/api/tasks/cancel', { task_id: t.id }).then(() => loadTasks());
  });
}

/* BF: редактирование задачи — та же форма, что и создание, но с уже
   заполненными полями. Расписание пересчитает сервер. */
function editTask(t) {
  modal(
    '<h3>Редактировать задачу</h3>' +
    '<div class="md-sub">JARVIS выполнит её сам и пришлёт результат — в уведомления и в Telegram.</div>' +
    '<div class="field"><label>Название</label><input id="mTitle" placeholder="Утренняя сводка"></div>' +
    '<div class="field"><label>Что сделать</label><textarea id="mPrompt" rows="4" placeholder="Собери главные новости про ИИ и сделай короткую сводку"></textarea></div>' +
    '<div class="field"><label>Расписание (необязательно)</label><input id="mSched" placeholder="daily 09:00  ·  every 2h  ·  every 30m"></div>' +
    '<div class="modal-acts"><button class="btn ghost" id="mCancel">Отмена</button>' +
    '<button class="btn primary" id="mOk">Сохранить</button></div>',
    (m) => {
      $('#mTitle', m).value = t.title || '';
      $('#mPrompt', m).value = t.prompt || '';
      $('#mSched', m).value = t.schedule || '';
      $('#mCancel', m).addEventListener('click', closeModal);
      $('#mOk', m).addEventListener('click', async () => {
        const title = $('#mTitle', m).value.trim();
        const prompt = $('#mPrompt', m).value.trim();
        if (!prompt) { toast('Опиши задачу', 'warn'); return; }
        const r = await api('/api/tasks/update', {
          task_id: t.id,
          title: title || prompt.slice(0, 40),
          prompt,
          schedule: $('#mSched', m).value.trim(),
        });
        closeModal();
        if (r.ok) { toast('Задача обновлена', 'success'); loadTasks(); }
        else toast(r.error || 'Не удалось обновить задачу', 'warn');
      });
    }
  );
}

function renderTasks() {
  const grid = $('#taskGrid');
  const running = S.tasks.filter((task) => task.status === 'running').length;
  const active = S.tasks.filter((task) =>
    ['queued', 'running', 'scheduled', 'paused'].includes(task.status)).length;
  const done = S.tasks.filter((task) => task.status === 'done').length;
  const stats = $('#autoStats');
  if (stats) {
    const statHtml = [
      ['всего', S.tasks.length, ''],
      ['актуальных', active, 'active'],
      ['в работе', running, 'running'],
      ['выполнено', done, 'done'],
    ].map(([label, value, cls]) => '<span class="auto-stat ' + cls + '"><i>' +
      label + '</i><b>' + value + '</b></span>').join('');
    if (stats.innerHTML !== statHtml) stats.innerHTML = statHtml;
  }
  const bar = $('#autoBar');
  if (bar) bar.classList.toggle('running', running > 0);
  const autoNav = $('.nav-item[data-view="auto"]');
  if (autoNav) autoNav.classList.toggle('auto-running', running > 0);
  const pause = $('#autoPauseBtn');
  if (pause) {
    pause.classList.toggle('play', S.autoPaused);
    pause.innerHTML = S.autoPaused ? ICO.play : ICO.pause;
    // Задач нет — паузе нечего ставить: кнопка мутнеет и не нажимается
    const idle = S.tasks.length === 0;
    pause.disabled = idle;
    pause.classList.toggle('idle', idle);
    const label = idle ? 'Задач нет'
      : S.autoPaused ? 'Продолжить все актуальные задачи' : 'Поставить все актуальные задачи на паузу';
    pause.title = label;
    pause.setAttribute('aria-label', label);
  }
  const clear = $('#clearDoneBtn');
  if (clear) clear.disabled = done === 0;

  if (!S.tasks.length) {
    if (!grid.querySelector(':scope > .task-empty')) {
      grid.replaceChildren(el('div', 'empty task-empty',
        '<span class="e-ico">◎</span>Фоновых задач нет.<br>' +
        'Напиши в чат «каждый день в 9:00 присылай сводку новостей» — ' +
        'я сам заведу задачу и буду присылать результат.'));
      grid.firstElementChild.style.gridColumn = '1/-1';
    }
    return;
  }

  const byId = new Map($$('.task-card', grid).map((card) => [card.dataset.taskId, card]));
  $$('.task-empty', grid).forEach((node) => node.remove());
  let cursor = grid.firstElementChild;
  S.tasks.forEach((t) => {
    const key = String(t.id);
    let card = byId.get(key);
    if (!card) {
      card = el('div', 'task-card');
      paintTaskCard(card, t);
    } else {
      byId.delete(key);
      if (card.dataset.fingerprint !== taskFingerprint(t)) paintTaskCard(card, t);
    }
    // Если порядок не изменился, это ноль DOM mutations. insertBefore нужен
    // только новому элементу или реальной смене updated_at-порядка.
    if (card !== cursor) grid.insertBefore(card, cursor);
    cursor = card.nextElementSibling;
  });
  byId.forEach((card) => card.remove());
}

$('#clearDoneBtn').addEventListener('click', async () => {
  const r = await api('/api/tasks/clear-completed', {});
  toast('Выполненные задачи очищены: ' + Number(r.deleted || 0), 'success');
  loadTasks();
});

$('#autoPauseBtn').addEventListener('click', async () => {
  const path = S.autoPaused ? '/api/tasks/resume-all' : '/api/tasks/pause-all';
  const r = await api(path, {});
  if (!r.ok) { toast(r.error || 'Не удалось изменить AUTO', 'warn'); return; }
  S.autoPaused = !!r.paused;
  toast(S.autoPaused ? 'AUTO поставлен на паузу' : 'AUTO продолжает задачи', 'info');
  loadTasks();
});

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
  // стрелки обновления совершают оборот: локальный список прилетает быстрее
  // кадра, поэтому оборот держим минимум 650мс — движение всегда видно
  const spin = $('#refreshFiles svg');
  if (spin) spin.classList.add('spin');
  const started = performance.now();
  try {
    await loadFilesInner(dir);
  } finally {
    const left = 650 - (performance.now() - started);
    setTimeout(() => { if (spin) spin.classList.remove('spin'); }, Math.max(0, left));
  }
}

async function loadFilesInner(dir) {
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
      'нажми «Импорт». Здесь же появятся файлы, которые я создам.</div>';
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

let fileDragEndedAt = 0;

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
    // Нативный drag-and-drop после отпускания иногда синтезирует click по
    // исходной карточке. Это не прямой клик и не должно открывать терминал.
    if (Date.now() - fileDragEndedAt < 260) { e.preventDefault(); return; }
    // Cmd/Ctrl и Shift — только выделение, без открытия: в Finder так же.
    // Прямой клик, наоборот, только открывает: старая selectOnly() включала
    // заодно синюю галочку и панель массовых операций, хотя человек ничего
    // не выбирал.
    if (e.metaKey || e.ctrlKey) { e.preventDefault(); selectToggle(f.path); return; }
    if (e.shiftKey) { e.preventDefault(); selectRange(f.path); return; }
    clearSelection();
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
    fileDragEndedAt = Date.now();
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
// ШЛЕЙФ ГАСНЕТ В САМОМ БРОСКЕ: после drop сетка перерисовывается, карточка-
// источник уходит из DOM и dragend до неё не доходит — миниатюры висели
// на плашке до перезагрузки. Capture-слушатель на документе покрывает все
// пути броска, что бы ни случилось с источником.
document.addEventListener('dragend', () => stopDragGhosts(), true);
document.addEventListener('drop', () => stopDragGhosts(), true);
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
/* Ввоз файла в песочницу ПО ССЫЛКЕ: чип из переписки ссылается на файл,
   которого может уже не быть в песочнице диалога (диалог пересоздан, файл
   переехал). Тогда перемещение по пути отвечает «не найдено» — вместо
   ошибки качаем содержимое по ссылке и заливаем его в песочницу заново. */
async function importByUrl(url, name, destDir) {
  try {
    const blob = await (await fetch(url)).blob();
    const data = await new Promise((res) => {
      const fr = new FileReader();
      fr.onload = () => res(fr.result);
      fr.onerror = () => res(null);
      fr.readAsDataURL(blob);
    });
    if (!data) return false;
    const r = await api('/api/upload', { name, data, chat_id: S.chatId || '' });
    if (!r.ok) { toast(r.error || 'не удалось перенести', 'error'); return false; }
    const target = destDir != null ? destDir : (S.fdir || '');
    if (target) await api('/api/sandbox/move', { path: r.name, dest: target, chat_id: S.chatId || '' });
    toast(name + ' — в песочнице', 'success');
    return true;
  } catch (err) {
    toast('не удалось перенести', 'error');
    return false;
  }
}

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
    const missing = (r.errors || []).some((x) => /не найдено|not found/i.test(x.error || ''));
    if (missing) {
      // файла нет в песочнице диалога — ввозим содержимое по ссылке чипа
      const url = e.dataTransfer.getData('text/jarvis-url');
      const name = e.dataTransfer.getData('text/jarvis-name');
      if (url && name) {
        const okImp = await importByUrl(url, name, destDir);
        if (okImp) { clearSelection(); pulseNav('files', false); loadFiles(); }
        return;
      }
    }
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
  if (done) {
    pulseNav('files', false);
    toast('Загружено файлов: ' + done, 'success');
  }
  loadFiles();
}

function renderSbxBar(info, entries) {
  const box = $('#sbxStats');
  if (!box) return;
  S.sandbox = info || {};
  // в шапке остаются только характеристики: сколько файлов, сколько места,
  // сколько объектов в текущей папке. Ярлык с названием убран.
  // «Очистить» живёт только когда чистить есть что: без файлов кнопка
  // замьючена, а не притворяется рабочей
  const wipe = $('#sbxWipe');
  if (wipe) wipe.disabled = !((info.files || 0) > 0);
  const stats = [
    ['файлов', String(info.files || 0)],
    ['занято', fmtSize(info.size || 0)],
  ];
  if (S.fdir) stats.push(['в этой папке', String((entries || []).length)]);
  box.innerHTML = stats.map(([k, v]) =>
    '<span class="sbx-stat"><i>' + esc(k) + '</i><b>' + esc(v) + '</b></span>').join('');
}

async function viewFile(f, card) {
  const token = S.fileViewToken = (S.fileViewToken || 0) + 1;
  const terminal = $('#fileTerminal');
  const work = $('#filesWork');
  if (terminal) terminal.hidden = false;
  if (work) work.classList.add('terminal-open');
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
  // Быстро нажали другой файл: медленный ответ первого не имеет права
  // заменить уже открытый второй предпросмотр.
  if (token !== S.fileViewToken) return;
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
  S.fileViewToken = (S.fileViewToken || 0) + 1;
  const feed = $('#termFeed');
  const terminal = $('#fileTerminal');
  const work = $('#filesWork');
  if (terminal) terminal.hidden = true;
  if (work) work.classList.remove('terminal-open');
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

  // предпросмотр: закрыть и скачать
  $('#fprevClose').addEventListener('click', closePreview);
  $('#fprevDl').addEventListener('click', () => {
    if (!PREV_FILE) return;
    const a = el('a'); a.href = PREV_FILE.url; a.download = PREV_FILE.name || '';
    document.body.appendChild(a); a.click(); a.remove();
    sfx('ok');
  });
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
    const inner = e.dataTransfer.getData('text/jarvis-path');
    if (!inner && (e.dataTransfer.files || []).length) {
      // в песочницу можно закинуть файлы и руками: бросили прямо в сетку —
      // импортируем, вкладка «Файлы» мигает (pulseNav внутри uploadToSandbox)
      await uploadToSandbox(Array.from(e.dataTransfer.files), S.fdir || '');
      return;
    }
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
        pulseNav('memory', true);
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
        const r = await api('/api/memory/add', { kind: 'fact', key: k, value: v });
        if (!r.ok) { toast(r.error || 'Не удалось запомнить', 'error'); return; }
        pulseNav('memory', true);
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

  // Изображения идут через российский release gateway. В ZIP лежит только
  // ограниченный revocable token; настоящий provider credential остаётся на
  // сервере. Пользователь ничего не регистрирует и не настраивает.
  const mediaCfg = c.media || {};
  const mediaSet = el('div', 'sset');
  if (mediaCfg.has_image_gateway) {
    mediaSet.innerHTML = '<h3>Генерация изображений</h3>' +
      '<div class="sd">Облачная генерация уже включена в установщик. Работает из России ' +
      'без VPN, создаёт одно изображение без watermark и не требует ваших ключей.</div>' +
      '<div class="prov-state"><span class="dot" style="width:7px;height:7px;border-radius:50%;background:var(--green)"></span> ' +
      'JARVIS Image Cloud: подключено</div>';
  } else {
    // Персональный GigaChat остаётся только dev/fallback режимом для сборок без
    // gateway. Production installer никогда не просит пользователя об этом.
    mediaSet.innerHTML = '<h3>Генерация изображений</h3>' +
      '<div class="sd">В этой сборке облачный канал не подключён. Для персонального dev-режима ' +
      'можно использовать свой Authorization Key GigaChat.</div>' +
      '<div class="prov-state"><span class="dot" style="width:7px;height:7px;border-radius:50%;background:' +
      (mediaCfg.has_gigachat_key ? 'var(--green)' : 'var(--red)') + '"></span> GigaChat fallback: ' +
      (mediaCfg.has_gigachat_key ? 'ключ установлен' : 'не подключён') + '</div>' +
      '<div class="field"><label>Authorization Key GigaChat</label><input id="kGiga" type="password" autocomplete="off" placeholder="' +
      esc(mediaCfg.gigachat_auth_key || 'вставь ключ без слова Basic') + '"></div>' +
      '<button class="btn primary" id="saveGiga">Сохранить fallback-ключ</button>';
  }
  grid.appendChild(mediaSet);
  const saveGiga = $('#saveGiga', mediaSet);
  if (saveGiga) saveGiga.addEventListener('click', async () => {
    const key = $('#kGiga', mediaSet).value.trim().replace(/^Basic\s+/i, '');
    if (!key) { toast('Вставь Authorization Key GigaChat', 'warn'); return; }
    const r = await api('/api/config/update', {
      patch: { media: { image_provider: 'gigachat', gigachat_auth_key: key,
        gigachat_scope: 'GIGACHAT_API_PERS', gigachat_model: 'GigaChat' } },
    });
    if (!r.ok) { toast(r.error || 'Не удалось сохранить ключ', 'error'); return; }
    S.config = r.config || S.config;
    toast('Персональная генерация подключена', 'success');
    renderSettings();
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
  if (e.key === 'Escape') { closeModal(); closePreview(); }
});

(async function init() {
  syncSoundBtn();
  setupScrollDate($('#stream'), $('#scrollDate'));
  /* BB: ПЛИТКИ ГОТОВЫ ДО ЭКРАНА — экран собирается один раз с уже
     загруженными плитками, никакой подмены на глазах */
  await loadIdeas();
  $('#stream').appendChild(buildWelcome());
  requestAnimationFrame(fitSuggTexts);
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => fitSuggTexts());
  }
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
  autoGrow();
  $('#input').focus();
})();
