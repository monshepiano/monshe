// Снимок LIVE-модуля BM28 (beta.100-101) — сохранить по просьбе юзера.

/* ============================================================ */
/* ============ BM28: LIVE — СОЗВОН С ДЖАРВИСОМ ================= */
/* Полноэкранный звонок: вода и стекло, живой орб, инструменты   */
/* мимолётом. Движок разговора (VAD, перебой, транскрипция,      */
/* синтез) — прежний, battle-tested; LIVE — его новое лицо.      */
/* ============================================================ */
const LIVE = { on: false, mic: false, cam: false, root: null, video: null,
  dreamIn: null, qEl: null, aEl: null, toolsEl: null, sideEl: null,
  askEl: null, run: 0 };

/* Аккорд входа/выхода — фирменный: восходящий арпеджио на вход,
   нисходящий на выход. Тот же синтезатор, что у всех звуков */
function liveChord(inn) {
  if (inn) chord([329.63, 493.88, 659.25, 987.77], { dur: .95, gain: .09, gap: .08, glide: 1.02 });
  else chord([987.77, 659.25, 493.88, 329.63], { dur: .8, gain: .075, gap: .07, glide: .98 });
}

/* --- СЦЕНА --- */
function liveBuild() {
  const root = el('div', '');
  root.id = 'liveRoot';
  root.innerHTML =
    '<div class="live-bg">' +
      '<i class="la a1"></i><i class="la a2"></i><i class="la a3"></i>' +
      '<div class="live-stars"></div>' +
      '<div class="live-sheen"></div>' +
      '<div class="live-redwave"></div>' +
    '</div>' +
    '<div class="live-veil"></div>' +
    '<div class="live-stage">' +
      '<div class="live-flow">' +
        '<div class="live-dream"><div class="live-dream-in">' +
          '<div class="live-q"></div>' +
          '<div class="live-a"></div>' +
        '</div></div>' +
        '<div class="live-tools"></div>' +
        '<div class="live-camwrap"><video class="live-video" autoplay playsinline muted></video></div>' +
        '<div class="live-core-wrap"><div class="live-core">' +
          '<div class="live-orb"><i class="lo-ring r1"></i><i class="lo-ring r2"></i>' +
            '<div class="lo-core"></div></div>' +
          '<div class="live-line">' +
            '<input id="liveInput" placeholder="Спроси Джарвиса…" autocomplete="off">' +
            '<button class="live-send" aria-label="Отправить">' +
              '<svg viewBox="0 0 24 24"><path d="M3 20l18-8L3 4v6l12 2-12 2z" fill="currentColor"/></svg>' +
            '</button>' +
          '</div>' +
        '</div></div>' +
      '</div>' +
      '<div class="live-side"><div class="ls-title">ПЛАН АГЕНТА</div><div class="live-side-in"></div></div>' +
    '</div>' +
    '<div class="live-bar">' +
      '<button class="lb" id="lbCam" title="Камера" aria-label="Камера">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M15 8.4v7.2a2 2 0 0 1-2 2H4.8a2 2 0 0 1-2-2V8.4a2 2 0 0 1 2-2H13a2 2 0 0 1 2 2z"/><path d="M15 11.2l5-2.8v7.2l-5-2.8"/></svg></button>' +
      '<button class="lb" id="lbMic" title="Микрофон" aria-label="Микрофон">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2.6" width="6" height="11.2" rx="3"/><path d="M5.5 11.2a6.5 6.5 0 0 0 13 0"/><path d="M12 17.7V21"/><path d="M8.8 21h6.4"/></svg></button>' +
      '<button class="lb" id="lbComp" title="Компьютер" aria-label="Компьютер">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3.4" y="4.6" width="17.2" height="12.4" rx="2.2"/><path d="M9.5 20.5h5M12 17v3.5"/></svg></button>' +
      '<button class="lb lb-exit" id="lbExit" title="Выйти" aria-label="Выйти">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M15 4.5H8a3 3 0 0 0-3 3v9a3 3 0 0 0 3 3h7"/><path d="M14 8.5l4 3.5-4 3.5"/><path d="M18 12H9.5"/></svg></button>' +
    '</div>';
  document.body.appendChild(root);
  LIVE.root = root;
  LIVE.video = root.querySelector('.live-video');
  LIVE.dreamIn = root.querySelector('.live-dream-in');
  LIVE.qEl = root.querySelector('.live-q');
  LIVE.aEl = root.querySelector('.live-a');
  LIVE.toolsEl = root.querySelector('.live-tools');
  LIVE.sideEl = root.querySelector('.live-side-in');
  /* звёзды — случайные, еле заметные */
  const stars = root.querySelector('.live-stars');
  for (let i = 0; i < 16; i++) {
    const st = el('i', '');
    st.style.left = (Math.random() * 100).toFixed(1) + '%';
    st.style.top = (Math.random() * 100).toFixed(1) + '%';
    st.style.setProperty('--d', (10 + Math.random() * 16).toFixed(1) + 's');
    st.style.setProperty('--dl', (-Math.random() * 14).toFixed(1) + 's');
    const sz = 1.5 + Math.random() * 2.6;
    st.style.width = sz + 'px'; st.style.height = sz + 'px';
    stars.appendChild(st);
  }
  /* строка ввода: Enter — спросить */
  const inp = root.querySelector('#liveInput');
  inp.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      const t = inp.value.trim();
      if (!t || S.streaming) return;
      inp.value = '';
      const core = root.querySelector('.live-core');
      if (core) { core.classList.remove('flash'); void core.offsetWidth; core.classList.add('flash'); }
      liveAskText(t);
    }
  });
  root.querySelector('.live-send').addEventListener('click', () => {
    const t = inp.value.trim();
    if (!t || S.streaming) return;
    inp.value = '';
    liveAskText(t);
  });
  /* панель управления */
  root.querySelector('#lbCam').addEventListener('click', () => liveSetCam(!LIVE.cam));
  root.querySelector('#lbMic').addEventListener('click', () => liveSetMic(!LIVE.mic));
  root.querySelector('#lbComp').addEventListener('click', () => liveSetComp(!S.computerUse));
  root.querySelector('#lbExit').addEventListener('click', liveConfirmExit);
}

/* --- ВХОД: звонок начинается --- */
async function liveOpen() {
  if (LIVE.on || VOICE.open) return;
  LIVE.on = true;
  LIVE.run += 1;
  VOICE.open = true;
  VOICE.nodes = [];
  VOICE.chatId = '';          // каждый звонок — новый разговор
  VOICE.ctxOn = true;         // LIVE = прямой провод к единому мозгу
  killWelcome();
  showView('chat');
  liveBuild();
  document.body.classList.add('live-on');
  requestAnimationFrame(() => { if (LIVE.root) LIVE.root.classList.add('open'); });
  if (S.agentMode && LIVE.root) LIVE.root.classList.add('ag');
  liveChord(true);
  /* микрофон по умолчанию ВКЛЮЧЁН — это звонок. Нет доступа — тихо
     переходим в текстовый режим */
  try {
    VOICE.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (e) { VOICE.stream = null; }
  if (!LIVE.on) return;    // успели выйти, пока просили микрофон
  if (VOICE.stream) {
    LIVE.mic = true;
    LIVE.root.classList.add('mic-on');
    const b = LIVE.root.querySelector('#lbMic'); if (b) b.classList.add('on');
    voiceListen();
  } else {
    LIVE.mic = false;
    LIVE.root.classList.add('text-on');
    setTimeout(() => { const i = $('#liveInput'); if (i && LIVE.on) i.focus(); }, 650);
  }
}

/* --- ВЫХОД: подтверждение, аккорд, вода закрывается --- */
function liveConfirmExit() {
  if (!LIVE.on) return;
  confirmBox('Выйти из LIVE?', 'Звонок завершится. Разговор сохранится в списке диалогов.', liveClose);
}

function liveClose() {
  if (!LIVE.on) return;
  LIVE.on = false;
  liveChord(false);
  const root = LIVE.root;
  if (root) root.classList.remove('open');
  document.body.classList.remove('live-on');
  if (LIVE.cam && S.camStream) stopCam();   // камера, поднятая звонком, гаснет вместе с ним
  LIVE.cam = false;
  /* BM23: финализатор на transitionend — таймер лишь страховка. Раньше
     удаление через 520мс могло подрезать fade 450мс на загруженной машине */
  const run = LIVE.run;
  let settled = false;
  const settle = () => {
    if (settled || LIVE.run !== run) return;
    settled = true;
    if (LIVE.root) { LIVE.root.remove(); LIVE.root = null; }
    LIVE.video = LIVE.dreamIn = LIVE.qEl = LIVE.aEl = null;
    LIVE.toolsEl = LIVE.sideEl = LIVE.askEl = null;
  };
  if (root) {
    root.addEventListener('transitionend', (e) => {
      if (e.target === root && e.propertyName === 'opacity') settle();
    });
  }
  setTimeout(settle, 1150);
  /* движок закрывается прежним путём: снимет voice-run, покажет
     транскрипт звонка в ленте как свёрнутую карточку */
  closeVoiceMode();
}

/* --- ФАЗЫ и УРОВЕНЬ (выстреливает движок разговора) --- */
function livePhase(p) {
  if (!LIVE.root) return;
  LIVE.root.classList.remove('ph-listening', 'ph-thinking', 'ph-speaking', 'ph-idle');
  LIVE.root.classList.add('ph-' + (p === 'idle' ? 'idle' : p));
}

function liveLevel(v) {
  if (LIVE.root) LIVE.root.style.setProperty('--vl', (v || 0).toFixed(3));
}

/* --- ТУМБЛЕРЫ ПАНЕЛИ --- */
async function liveSetMic(on) {
  if (!LIVE.on || on === LIVE.mic) return;
  if (on) {
    if (!VOICE.stream || !VOICE.stream.active) {
      try {
        VOICE.stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
      } catch (e) { toast('Нет доступа к микрофону', 'error'); return; }
    }
    if (!LIVE.on) return;
    LIVE.mic = true;
    LIVE.root.classList.remove('text-on');
    LIVE.root.classList.add('mic-on');
    voiceListen();
  } else {
    LIVE.mic = false;
    LIVE.root.classList.remove('mic-on');
    LIVE.root.classList.add('text-on');
    /* стоп прослушивания БЕЗ транскрипции недосказанного */
    cancelAnimationFrame(VOICE.raf);
    if (VOICE.rec) {
      try { VOICE.rec.onstop = null; VOICE.rec.stop(); } catch (e) { /* уже мёртв */ }
      VOICE.rec = null;
    }
    VOICE.chunks = [];
    try { window.speechSynthesis.cancel(); } catch (e) { /* синтеза нет */ }
    voiceSetPhase('idle');
    setTimeout(() => { const i = $('#liveInput'); if (i && LIVE.on) i.focus(); }, 620);
  }
  blip(on);
  const b = LIVE.root.querySelector('#lbMic');
  if (b) { b.classList.toggle('on', on); b.classList.toggle('mute', !on); }
}

async function liveSetCam(on) {
  if (!LIVE.on || on === LIVE.cam) return;
  if (on) {
    try { await startCam(); } catch (e) { /* карточка не собралась */ }
    if (!S.camStream) { toast('Камера недоступна', 'error'); return; }
    if (!LIVE.on) return;
    if (LIVE.video) { try { LIVE.video.srcObject = S.camStream; } catch (e) {} }
    LIVE.cam = true;
    LIVE.root.classList.add('cam-on');
    sfx('start');
  } else {
    LIVE.cam = false;
    LIVE.root.classList.remove('cam-on');
    if (LIVE.video) { try { LIVE.video.srcObject = null; } catch (e) {} }
    if (S.camStream) stopCam();
    sfx('stop');
  }
  const b = LIVE.root.querySelector('#lbCam');
  if (b) b.classList.toggle('on', LIVE.cam);
}

async function liveSetComp(on) {
  if (!LIVE.on) return;
  if (!on) {
    S.computerUse = false;
    const b = LIVE.root.querySelector('#lbComp'); if (b) b.classList.remove('on');
    blip(false);
    return;
  }
  /* включение — с самопроверкой, как прежде в композере: тумблер
     сразу назовёт причину, если computer-use не готов */
  const r = await api('/api/computer/status');
  const st = (r && r.computer) || {};
  if (st.ok) {
    S.computerUse = true;
    const b = LIVE.root.querySelector('#lbComp'); if (b) b.classList.add('on');
    blip(true);
  } else {
    S.computerUse = false;
    sfx('error');
    modal('<h3>COMPUTER-USE не готов</h3>' +
      '<div class="sd" style="margin-bottom:10px">Проверка на этой машине не прошла:</div>' +
      '<div class="sd" style="color:var(--red)">' + esc(st.error || 'неизвестная причина') + '</div>');
  }
}

/* --- ТЕКСТОВЫЙ ВОПРОС (микрофон выключен) --- */
async function liveAskText(text) {
  if (!LIVE.on || S.streaming) return;
  liveShowQuestion(text);
  voiceSetPhase('thinking');
  try {
    await send({
      text, voice: true, silent: true,
      onDelta: (chunk) => { if (chunk) liveDelta(chunk); },
      onDone: () => {},
    });
  } catch (e) { /* сеть обязательно ответит ошибкой в поток */ }
  if (LIVE.on && !LIVE.mic) voiceSetPhase('idle');
}

/* --- СОH: вопрос/ответ из воды --- */
function liveShowQuestion(text) {
  if (!LIVE.dreamIn || !LIVE.qEl) return;
  const d = LIVE.dreamIn;
  d.classList.add('swap');
  setTimeout(() => {
    if (!LIVE.on || !LIVE.dreamIn) return;
    LIVE.qEl.textContent = text || '';
    LIVE.aEl.textContent = '';
    d.classList.remove('swap');
    d.classList.add('fresh');
    void d.offsetWidth;
    d.classList.remove('fresh');
  }, 400);
}

function liveDelta(chunk) {
  if (LIVE.aEl) LIVE.aEl.textContent += chunk;
}

/* --- СОБЫТИЯ ПОТОКА -> СЦЕНА --- */
function liveEvent(ev) {
  if (!LIVE.on || !ev || !ev.type) return;
  switch (ev.type) {
    case 'tool_start': return liveToolShow(ev);
    case 'tool_result': return liveToolDone(ev);
    case 'plan': return liveSideShow(ev);
    case 'plan_step': return liveSideStep(ev);
    case 'approval_wait': return liveAskShow(ev);
    case 'question': return liveAskShow(ev);
    case 'approval_done': return liveAskHide();
    case 'mode_changed': {
      if (S.agentMode) LIVE.root.classList.add('ag');
      else LIVE.root.classList.remove('ag');
      return;
    }
    case 'done': case 'end': return liveWorkDone();
  }
}

/* инструмент — мимолётная карточка из глубины; остальные уступают место */
function liveToolShow(ev) {
  if (!LIVE.toolsEl || SILENT_TOOLS[ev.name]) return;
  const card = el('div', 'live-tool');
  let args = '';
  try {
    args = JSON.stringify(ev.args || {});
    if (args.length > 90) args = args.slice(0, 90) + '…';
  } catch (e) { args = ''; }
  card.innerHTML = '<span class="lt-dot"></span><b>' + esc(ev.name || 'инструмент') + '</b>' +
    (args ? '<span class="lt-args">' + esc(args) + '</span>' : '');
  card.dataset.name = ev.name || '';
  LIVE.toolsEl.appendChild(card);
  /* не больше трёх мимолётных — старые уплывают */
  const cards = Array.from(LIVE.toolsEl.children);
  while (cards.length > 3) liveToolOut(cards.shift());
  LIVE.root.classList.add('tools-up');
  sfx('pop');
  /* страховка: зависший инструмент не занимает сцену навсегда */
  setTimeout(() => { if (card.isConnected) liveToolOut(card); }, 9000);
}

function liveToolDone(ev) {
  if (!LIVE.toolsEl) return;
  const cards = Array.from(LIVE.toolsEl.children).filter((c) => c.dataset.name === (ev.name || ''));
  const card = cards[cards.length - 1];
  if (!card) return;
  card.classList.add('done');
  setTimeout(() => liveToolOut(card), 900);
}

function liveToolOut(card) {
  if (!card || !card.isConnected || card.classList.contains('out')) return;
  card.classList.add('out');
  /* финализатор на animationend (BM23), таймер — страховка */
  let gone = false;
  const drop = () => {
    if (gone) return;
    gone = true;
    card.remove();
    if (LIVE.toolsEl && !LIVE.toolsEl.children.length) LIVE.root.classList.remove('tools-up');
  };
  card.addEventListener('animationend', drop);
  setTimeout(drop, 950);
}

/* большая работа: план агента — справа, элементы уступают влево */
function liveSideShow(ev) {
  if (!LIVE.sideEl || !(ev.steps || []).length) return;
  LIVE.sideEl.innerHTML = '';
  (ev.steps || []).forEach((st, i) => {
    const row = el('div', 'ls-step');
    row.innerHTML = '<i class="ls-i"></i><span>' + esc(typeof st === 'string' ? st : (st.text || st.title || JSON.stringify(st))) + '</span>';
    LIVE.sideEl.appendChild(row);
    setTimeout(() => row.classList.add('in'), 120 + i * 110);
  });
  LIVE.root.classList.add('side-on');
}

function liveSideStep() {
  if (!LIVE.sideEl) return;
  const next = Array.from(LIVE.sideEl.children)
    .find((c) => !c.classList.contains('done'));
  if (next) next.classList.add('done');
}

function liveWorkDone() {
  /* пауза — и все элементы возвращаются на места */
  setTimeout(() => {
    if (!LIVE.on || !LIVE.root) return;
    LIVE.root.classList.remove('side-on');
    if (LIVE.toolsEl) Array.from(LIVE.toolsEl.children).forEach(liveToolOut);
    liveAskHide();
  }, 1200);
}

/* интерактив (вопрос/подтверждение) всплывает из глубины; выбор
   делается нажатием настоящей кнопки в скрытой карточке ленты —
   источник решения один */
function liveAskShow(ev) {
  if (!LIVE.root || LIVE.askEl) return;
  const panel = el('div', 'live-ask');
  const isApproval = ev.type === 'approval_wait';
  let title = 'Джарвис спрашивает';
  let body = '';
  let acts = '';
  if (isApproval) {
    title = ev.style === 'permission' ? '◇ Можно открыть приложение?' : '⛨ Требуется подтверждение';
    body = '<b>' + esc(ev.label || ev.tool || '') + '</b>' + (ev.reason ? '<br>' + esc(ev.reason) : '');
    acts = '<button class="btn primary sm la-yes">Разрешить</button>' +
           '<button class="btn danger sm la-no">Не открывать</button>';
  } else {
    body = esc(ev.question || ev.text || '');
    acts = '<button class="btn la-no">Отмена</button>';
    const opts = ev.options || ev.variants || [];
    if (opts.length) {
      acts = opts.map((o, i) => '<button class="btn primary sm la-opt" data-i="' + i + '">' +
        esc(typeof o === 'string' ? o : (o.text || o.label || String(i))) + '</button>').join('') + acts;
    } else {
      acts = '<button class="btn primary sm la-free">Ответить текстом</button>' + acts;
    }
  }
  panel.innerHTML = '<div class="la-title">' + title + '</div>' +
    '<div class="la-body">' + body + '</div>' +
    '<div class="la-acts">' + acts + '</div>';
  LIVE.root.querySelector('.live-stage').appendChild(panel);
  LIVE.askEl = panel;
  sfx('warn');
  const decide = (choice) => {
    liveAskHide(500);
    /* нажимаем настоящую кнопку скрытой карточки — один источник решения */
    const cards = $$('.panel-card.approve-card, .panel-card.ask-card', stream());
    const last = cards[cards.length - 1];
    if (last) {
      if (isApproval) {
        const btn = last.querySelector(choice === 'approved' ? '.ok' : '.no');
        if (btn) { btn.click(); return; }
      } else {
        /* выбор — лишь подсветка, отправляет кнопка карточки: жмём обе */
        const opts = Array.from(last.querySelectorAll('.ask-opt'));
        const opt = (typeof choice === 'number' && opts[choice]) || opts[0];
        if (opt) {
          opt.click();
          const go = last.querySelector('.ask-go');
          if (go && !go.disabled) { go.click(); return; }
        }
      }
    }
    /* карточки нет — решаем напрямую */
    if (isApproval) {
      const pend = S.approvals[0];
      if (pend) api('/api/approvals/decide', { id: pend.id, decision: choice });
    } else if (typeof choice === 'string' && ev.id) {
      api('/api/questions/answer', { id: ev.id, answer: choice });
    }
  };
  if (isApproval) {
    panel.querySelector('.la-yes').addEventListener('click', () => decide('approved'));
    panel.querySelector('.la-no').addEventListener('click', () => decide('rejected'));
  } else {
    panel.querySelectorAll('.la-opt').forEach((b) => b.addEventListener('click', () => decide(+b.dataset.i)));
    const fr = panel.querySelector('.la-free');
    if (fr) fr.addEventListener('click', () => {
      liveAskHide(400);
      /* ответ текстом: строка ввода уже ждёт — следующий вопрос уйдёт
         в этот же диалог */
      const i = $('#liveInput'); if (i && LIVE.on) i.focus();
    });
    const no = panel.querySelector('.la-no');
    if (no) no.addEventListener('click', () => liveAskHide(300));
  }
}

function liveAskHide(delay) {
  const panel = LIVE.askEl;
  if (!panel) return;
  LIVE.askEl = null;
  setTimeout(() => {
    panel.classList.add('out');
    let gone = false;
    const drop = () => { if (gone) return; gone = true; panel.remove(); };
    panel.addEventListener('animationend', drop);
    setTimeout(drop, 950);
  }, delay || 0);
}

