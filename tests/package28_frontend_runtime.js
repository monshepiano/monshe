'use strict';

/* Browser-free deterministic contracts for package 28 and its UI follow-up.
   Run with: node tests/package28_frontend_runtime.js */

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..');
const js = fs.readFileSync(path.join(root, 'app/jarvis/web/js/app.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'app/jarvis/web/css/app.css'), 'utf8');
const html = fs.readFileSync(path.join(root, 'app/jarvis/web/index.html'), 'utf8');
const pyAgent = fs.readFileSync(path.join(root, 'app/jarvis/agent.py'), 'utf8');
const pyLlm = fs.readFileSync(path.join(root, 'app/jarvis/llm.py'), 'utf8');
const pyServer = fs.readFileSync(path.join(root, 'app/jarvis/server.py'), 'utf8');
const markdown = fs.readFileSync(path.join(root, 'app/jarvis/web/js/markdown.js'), 'utf8');

function extractFunction(source, name) {
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const match = re.exec(source);
  assert(match, 'function not found: ' + name);
  const start = match.index;
  const open = source.indexOf('{', start);
  let depth = 0;
  let quote = '';
  let lineComment = false;
  let blockComment = false;
  let escaped = false;
  for (let i = open; i < source.length; i += 1) {
    const ch = source[i];
    const next = source[i + 1];
    if (lineComment) {
      if (ch === '\n') lineComment = false;
      continue;
    }
    if (blockComment) {
      if (ch === '*' && next === '/') { blockComment = false; i += 1; }
      continue;
    }
    if (quote) {
      if (escaped) { escaped = false; continue; }
      if (ch === '\\') { escaped = true; continue; }
      if (ch === quote) quote = '';
      continue;
    }
    if (ch === '/' && next === '/') { lineComment = true; i += 1; continue; }
    if (ch === '/' && next === '*') { blockComment = true; i += 1; continue; }
    if (ch === '"' || ch === "'" || ch === '`') { quote = ch; continue; }
    if (ch === '{') depth += 1;
    if (ch === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error('unterminated function: ' + name);
}

function loadFunctions(names, context) {
  const source = names.map((name) => extractFunction(js, name)).join('\n');
  vm.createContext(context);
  vm.runInContext(source + '\n' + names.map((name) => 'this.' + name + '=' + name).join(';'), context);
  return context;
}

class MiniClassList {
  constructor(node) { this.node = node; }
  _set() { return new Set((this.node._className || '').split(/\s+/).filter(Boolean)); }
  _save(set) { this.node._className = Array.from(set).join(' '); }
  add(...names) { const set = this._set(); names.forEach((name) => set.add(name)); this._save(set); }
  remove(...names) { const set = this._set(); names.forEach((name) => set.delete(name)); this._save(set); }
  contains(name) { return this._set().has(name); }
  toggle(name, force) {
    const set = this._set();
    const on = force === undefined ? !set.has(name) : !!force;
    if (on) set.add(name); else set.delete(name);
    this._save(set);
    return on;
  }
}

function simpleSelector(selector) {
  let clean = selector.trim();
  if (clean.includes(',')) clean = clean.split(',')[0].trim();
  if (clean.includes('>')) clean = clean.split('>').pop().trim();
  if (clean.includes(' ')) clean = clean.split(/\s+/).pop();
  clean = clean.replace(/^:scope/, '').trim();
  return clean;
}

function selectorMatches(node, selector) {
  if (!node || node.nodeType !== 1) return false;
  if (selector.includes(',')) {
    return selector.split(',').some((part) => selectorMatches(node, part.trim()));
  }
  const clean = simpleSelector(selector);
  if (!clean) return false;
  if (clean.startsWith('.')) {
    return clean.slice(1).split('.').every((name) => node.classList.contains(name));
  }
  if (clean.startsWith('#')) return node.id === clean.slice(1);
  const attr = clean.match(/^\[data-([\w-]+)="([^"]*)"\]$/);
  if (attr) {
    const key = attr[1].replace(/-([a-z])/g, (_m, ch) => ch.toUpperCase());
    return String(node.dataset[key] || '') === attr[2];
  }
  return node.tagName === clean.toUpperCase();
}

class MiniNode {
  constructor(tag, text) {
    this.nodeType = tag ? 1 : 3;
    this.tagName = tag ? String(tag).toUpperCase() : undefined;
    this.nodeValue = tag ? null : String(text || '');
    this.childNodes = [];
    this.parentNode = null;
    this.dataset = {};
    this.style = {};
    this.attributes = {};
    this.listeners = {};
    this.id = '';
    this._className = '';
    this._connected = true;
    this._innerHTML = '';
    this.classList = new MiniClassList(this);
  }
  get className() { return this._className; }
  set className(value) { this._className = String(value || ''); }
  get isConnected() { return this._connected; }
  set isConnected(value) { this._connected = !!value; }
  get children() { return this.childNodes.filter((node) => node.nodeType === 1); }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() {
    const items = this.children;
    return items[items.length - 1] || null;
  }
  get previousElementSibling() {
    if (!this.parentNode) return null;
    const items = this.parentNode.children;
    return items[items.indexOf(this) - 1] || null;
  }
  get nextElementSibling() {
    if (!this.parentNode) return null;
    const items = this.parentNode.children;
    return items[items.indexOf(this) + 1] || null;
  }
  get textContent() {
    if (this.nodeType === 3) return this.nodeValue;
    return this.childNodes.map((node) => node.textContent).join('');
  }
  set textContent(value) {
    if (this.nodeType === 3) { this.nodeValue = String(value || ''); return; }
    this.childNodes.forEach((node) => { node.parentNode = null; node._connected = false; });
    this.childNodes = [];
    const text = String(value || '');
    if (text) this.appendChild(new MiniNode(null, text));
  }
  get innerHTML() { return this._innerHTML; }
  set innerHTML(value) { this._innerHTML = String(value || ''); this.childNodes = []; }
  appendChild(node) {
    if (node.parentNode) node.parentNode._detach(node);
    this.childNodes.push(node);
    node.parentNode = this;
    node._connected = this._connected;
    return node;
  }
  insertBefore(node, ref) {
    if (!ref || !this.childNodes.includes(ref)) return this.appendChild(node);
    if (node.parentNode) node.parentNode._detach(node);
    const at = this.childNodes.indexOf(ref);
    this.childNodes.splice(at, 0, node);
    node.parentNode = this;
    node._connected = this._connected;
    return node;
  }
  replaceChild(node, old) {
    const at = this.childNodes.indexOf(old);
    assert(at >= 0, 'replaceChild target must exist');
    if (node.parentNode) node.parentNode._detach(node);
    this.childNodes[at] = node;
    node.parentNode = this;
    node._connected = this._connected;
    old.parentNode = null;
    old._connected = false;
    return old;
  }
  _detach(node) {
    const at = this.childNodes.indexOf(node);
    if (at >= 0) this.childNodes.splice(at, 1);
    node.parentNode = null;
  }
  remove() {
    if (this.parentNode) this.parentNode._detach(this);
    this._connected = false;
  }
  querySelectorAll(selector) {
    const out = [];
    const walk = (node) => {
      node.childNodes.forEach((child) => {
        if (selectorMatches(child, selector)) out.push(child);
        if (child.nodeType === 1) walk(child);
      });
    };
    walk(this);
    return out;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  removeEventListener(name) { delete this.listeners[name]; }
  closest(selector) {
    let node = this;
    while (node) {
      if (selectorMatches(node, selector)) return node;
      node = node.parentNode;
    }
    return null;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] == null ? null : this.attributes[name]; }
  getBoundingClientRect() {
    return this._rect || { top: 100, bottom: 220, left: 30, width: 500, height: 120 };
  }
}

const miniDocument = {
  createElement(tag) { return new MiniNode(tag); },
  createTextNode(text) { return new MiniNode(null, text); },
};
function miniEl(tag, cls, markup) {
  const node = miniDocument.createElement(tag);
  node.className = cls || '';
  if (markup != null) node.innerHTML = markup;
  return node;
}

function testLiveStatusHasNoSpinner() {
  const timers = [];
  const q = { textContent: '', offsetWidth: 10, classList: { remove() {}, add() {} } };
  const statusCaret = { style: {} };
  let mounted = false;
  let mounts = 0;
  const box = {
    className: '', markup: '', _statusSignature: '',
    set innerHTML(value) { this.markup = value; mounted = true; mounts += 1; },
    get innerHTML() { return this.markup; },
    querySelector(selector) {
      if (!mounted) return null;
      return selector === '.tw-quip' ? q : statusCaret;
    },
  };
  const ctx = loadFunctions(['syncCursorPhase', 'runStatus'], {
    Math, CURSOR_BREATHE_MS: 1050, performance: { now: () => 200 },
    stopQuips(ui) { ui.quipTimer = null; },
    setInterval(fn, ms) { timers.push({ fn, ms }); return timers.length; },
  });
  const ui = { statusEl: box, quipTimer: null };
  ctx.runStatus(ui, ['работаю', 'проверяю'], { caret: false, every: 900 });
  assert(box.innerHTML.includes('tw-caret'), 'work status must use the live caret');
  assert(!box.innerHTML.includes('spinner'), 'response lifecycle must not render a round spinner');
  assert(box.className.includes('work-wait'));
  assert.strictEqual(q.textContent, 'работаю');
  ctx.runStatus(ui, ['работаю', 'проверяю'], { caret: false, every: 900 });
  assert.strictEqual(mounts, 1, 'repeated tool_partial must not recreate the status caret');
  assert.strictEqual(timers.length, 1, 'identical status must not restart its ticker');
  timers[0].fn();
  assert.strictEqual(q.textContent, 'проверяю', 'work labels must stay alive');
}

function testTelegramDateHudAndTimeOnlyMeta() {
  const host = new MiniNode('div');
  host._rect = { top: 50, bottom: 450, left: 0, width: 700, height: 400 };
  host.scrollHeight = 1200; host.scrollTop = 300; host.clientHeight = 400;
  const old = new MiniNode('div'); old.className = 'msg';
  old.dataset.ts = String(Date.parse('2026-09-14T10:00:00Z') / 1000);
  old._rect = { top: 0, bottom: 40, left: 0, width: 500, height: 40 };
  const visible = new MiniNode('div'); visible.className = 'msg';
  visible.dataset.ts = String(Date.parse('2026-09-15T10:00:00Z') / 1000);
  visible._rect = { top: 65, bottom: 125, left: 0, width: 500, height: 60 };
  host.appendChild(old); host.appendChild(visible);
  const badge = new MiniNode('div');
  const timers = [];
  const frames = [];
  const ctx = loadFunctions(
    ['dayKey', 'dayLabel', 'scrollDayMessage', 'setupScrollDate', 'stampTime', 'placeDaySeparator'],
    {
      Date, Number, String, Math, document: miniDocument, el: miniEl,
      esc: (text) => String(text),
      $$: (selector, node) => node.querySelectorAll(selector),
      S: { scenarioActive: false },
      requestAnimationFrame(fn) { frames.push(fn); return frames.length; },
      setTimeout(fn, ms) { timers.push({ fn, ms }); return timers.length; },
      clearTimeout() {},
    },
  );
  ctx.setupScrollDate(host, badge);
  assert(host.listeners.scroll, 'date HUD must subscribe to the transcript scroll');
  host.listeners.scroll(); frames.shift()();
  assert.strictEqual(badge.textContent, ctx.dayLabel(Number(visible.dataset.ts)),
    'first visible message owns the transient calendar label');
  assert(badge.classList.contains('show'));
  assert.strictEqual(badge.getAttribute('aria-hidden'), 'false');
  const hide = timers.find((timer) => timer.ms === 1150);
  assert(hide, 'date HUD must schedule disappearance');
  hide.fn();
  assert(!badge.classList.contains('show'));
  assert.strictEqual(badge.getAttribute('aria-hidden'), 'true');

  host.scrollTop = host.scrollHeight - host.clientHeight;
  host.listeners.scroll(); frames.shift()();
  assert(!badge.classList.contains('show'), 'date stays hidden at transcript bottom');

  const msg = new MiniNode('div');
  const bubble = new MiniNode('div');
  bubble.className = 'bubble-user';
  msg.appendChild(bubble);
  const stamp = ctx.stampTime(msg, Number(visible.dataset.ts));
  assert(stamp.classList.contains('msg-time'));
  assert(stamp.classList.contains('in-bubble'));
  assert(!stamp.classList.contains('two'), 'live message meta must contain time only');
  assert(!/\d{2}\.\d{2}/.test(stamp.textContent), 'date must not be repeated beside a message');
  assert.strictEqual(msg.dataset.day, ctx.dayKey(Number(visible.dataset.ts)));

  const dayHost = new MiniNode('div');
  const first = new MiniNode('div'); first.className = 'msg';
  ctx.stampTime(first, Date.parse('2026-09-14T08:00:00Z') / 1000);
  dayHost.appendChild(first);
  const firstSep = ctx.placeDaySeparator(first);
  assert(firstSep && firstSep.classList.contains('day-separator'),
    'conversation must begin with a permanent calendar separator');
  assert.strictEqual(first.previousElementSibling, firstSep);
  const same = new MiniNode('div'); same.className = 'msg';
  ctx.stampTime(same, Date.parse('2026-09-14T09:00:00Z') / 1000);
  dayHost.appendChild(same);
  assert.strictEqual(ctx.placeDaySeparator(same), null,
    'one calendar row is enough for consecutive messages on the same day');
  const tomorrow = new MiniNode('div'); tomorrow.className = 'msg';
  ctx.stampTime(tomorrow, Date.parse('2026-09-15T09:00:00Z') / 1000);
  dayHost.appendChild(tomorrow);
  assert(ctx.placeDaySeparator(tomorrow), 'a changed day must create the next separator');

  assert(/class="scroll-date"\s+id="scrollDate"/.test(html));
  assert(/\.scroll-date\s*\{[^}]*position\s*:\s*absolute[^}]*top\s*:\s*24px[^}]*opacity\s*:\s*0/s.test(css),
    'date stays fixed at the desktop transcript start while scrolling');
  const shownDate = css.match(/\.scroll-date\.show\s*\{([^}]*)\}/s);
  assert(shownDate && /opacity\s*:\s*\.42/.test(shownDate[1]),
    'the transient scroll date must remain deliberately dim and transparent');
  assert(/@media[^}]*[\s\S]*\.scroll-date\s*\{\s*top\s*:\s*16px/s.test(css),
    'mobile date aligns with the mobile transcript start');
  assert(/\.day-separator\s*\{[^}]*display\s*:\s*flex[^}]*margin/s.test(css),
    'a permanent day separator must remain at the beginning of each date group');
  assert(/\.msg-time\.two\.in-bubble\s*\{[^}]*display\s*:\s*flex/s.test(css),
    'legacy saved two-line stamps must remain readable');
}

function testFileViewportAndCloseButton() {
  assert(/\.files-work\s*\{[^}]*flex\s*:\s*1 1 auto[^}]*min-height\s*:\s*0[^}]*overflow\s*:\s*hidden/s.test(css),
    'file workspace must be bounded by the visible view');
  assert(/\.files-work>\.file-grid\s*\{[^}]*flex\s*:\s*1 1 0[^}]*overflow-y\s*:\s*auto/s.test(css),
    'only the file grid must consume and scroll through remaining height');
  assert(/\.files-work>\.term-block\s*\{[^}]*flex\s*:\s*0 0 auto[^}]*max-height\s*:/s.test(css),
    'visible preview must reserve the viewport bottom instead of following all files');
  assert(/\.files-work>\.term-block\[hidden\]\s*\{[^}]*display\s*:\s*none/s.test(css));
  const close = html.match(/<button[^>]*id="termClose"[^>]*>([^<]*)<\/button>/);
  assert(close && close[1].trim() === '×', 'file close control must be a cross');
  assert(/\.term-act\.term-close\s*\{[^}]*width\s*:\s*25px[^}]*height\s*:\s*25px[^}]*display\s*:\s*grid/s.test(css),
    'file close control must be a square dialog button');

  const fileCard = extractFunction(js, 'fileCard');
  const click = fileCard.slice(fileCard.indexOf("c.addEventListener('click'"),
    fileCard.indexOf("c.addEventListener('dblclick'"));
  const executable = click.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
  assert(/\bclearSelection\s*\(\s*\)/.test(executable));
  assert(!/\bselectOnly\s*\(/.test(executable));
}

function testImportantHeadingCaretAndTrail() {
  let cursorClock = 100;
  const ctx = loadFunctions(
    ['clearTypingDecorations', 'markImportantThought', 'syncCursorPhase', 'placeCaret'],
    {
      document: miniDocument, CURSOR_BREATHE_MS: 1050,
      performance: { now: () => (cursorClock += 20) },
      $$: (selector, node) => node.querySelectorAll(selector),
    },
  );
  assert(/const TYPE_MS\s*=\s*20/.test(js), 'DOM typing is capped at 50 renders per second');
  assert(/const CPS_TALK\s*=\s*125/.test(js) && /const CPS_TALK_MAX\s*=\s*245/.test(js));
  // код: 470 базово (быстрее старых 420, медленнее спорных 560), хвост —
  // до 2000 симв/с; BE: у дна — догоняющий скролл, зона без run-а — 160px
  assert(/const CPS_CODE\s*=\s*470/.test(js) && /const CPS_SMOOTH_MS\s*=\s*340/.test(js) &&
    /Math\.min\(2000, 700 \+ left \* 0\.18\)/.test(js) &&
    /box\.scrollHeight - box\.scrollTop - box\.clientHeight < 160/.test(js));
  assert(!/CPS_IMPORTANT/.test(js), 'headings must not have a separate speed');
  assert(!/function importantLine|function headingEndedSince/.test(js),
    'headings must not add a hidden rate or pause branch');
  const target = loadFunctions(['talkTargetCps'], {
    Math, Number, CPS_TALK: 125, CPS_TALK_MAX: 245,
  });
  assert.strictEqual(target.talkTargetCps(120), 125);
  assert(target.talkTargetCps(800) > 200 && target.talkTargetCps(800) < 245,
    'a long ready tail accelerates continuously without crossing the visual-speed ceiling');
  const typer = extractFunction(js, 'typerStart');
  assert(/let want\s*=\s*code\s*\?\s*CPS_CODE\s*:\s*talkTargetCps\(left\)/.test(typer));
  assert(!/heading|important/i.test(typer), 'typer must not inspect markdown headings');
  assert(/performance\.now\(\)/.test(typer) && /CPS_SMOOTH_MS/.test(typer),
    'elapsed-time CPS must survive delayed timer frames');
  assert(/Math\.min\(250,\s*now\s*-\s*lastTick\)/.test(typer),
    'a delayed browser frame is credited HONESTLY (up to 250ms): heavy frames must never slow the text pace — the step cap already prevents bursts');
  /* AV: старая формула «4 знака на тик ВСЕГДА» и была багом «полслова в
     секунду»: тяжёлый кадр тянулся 250мс, а тик всё равно давал 4 знака.
     Теперь предел привязан ко времени кадра — темп честный при любых
     лагах, а накопитель acc по-прежнему не даёт залпов */
  assert(/const baseCap = \(code \? \(ui\.fastFinish \? 26 : 10\) : 4\) \* turbo;/.test(typer) &&
    /const frameCap = Math\.max\(baseCap, Math\.ceil\(\(ui\.cps \* elapsed\) \/ 1000\)\);/.test(typer) &&
    /step = Math\.min\(step, left, frameCap\);/.test(typer),
    'AV: step cap follows real frame time — heavy frames never slow the pace');
  assert(/const turbo = S\.turbo \? 16 : 1;/.test(typer) &&
    /want \*= turbo;/.test(typer) && /pause \/ turbo/.test(typer),
    'the ×3.5 boost button speeds target rate, frame cap and shrinks punctuation pauses');
  assert(/if\s*\(code\s*&&\s*ui\.fastFinish\)\s*want\s*=/.test(typer),
    'the after-done speed boost must apply to dense content only, never to prose');
  assert(!/left\s*>|boost|mult/i.test(typer),
    'typing acceleration must be smooth rather than a discrete backlog branch');
  assert(/ui\.cps\s*=\s*0[\s\S]*ui\.acc\s*=\s*0/.test(typer),
    'a temporarily drained stream must not leak its previous speed into the next chunk');

  const fast = loadFunctions(['fastLine'], { Math });
  const tableSource = 'Обычный текст\n| Ячейка | Значение |\nОбычный итог';
  assert.strictEqual(fast.fastLine(tableSource, tableSource.indexOf('Ячейка')), true,
    'the complete current row is classified before the first pipe is typed');
  assert.strictEqual(fast.fastLine(tableSource, 3), false);
  // списки — живая речь: маркеры больше не включают быструю печать,
  // а между пунктами ставится микропауза (перевод дыхания)
  assert.strictEqual(fast.fastLine('- первый пункт списка', 1), false,
    'list items are spoken at conversational speed, not code speed');
  assert.strictEqual(fast.fastLine('2. нумерованный пункт', 1), false,
    'numbered list items are spoken at conversational speed too');
  assert(!/pause = Math\.max\(pause, 300\)/.test(js),
    'list items print with the usual conversational rhythm, no extra pause');

  const md = new MiniNode('div');
  const heading = new MiniNode('h2');
  const original = 'Пуск 🚀 системы';
  heading.appendChild(miniDocument.createTextNode(original));
  md.appendChild(heading);
  ctx.markImportantThought(md);
  ctx.placeCaret(md);
  assert.strictEqual(md.querySelectorAll('.caret').length, 1);
  assert(md.querySelector('.caret').classList.contains('caret-important'),
    'markdown headings deterministically use the yellow important caret');
  const firstCursorPhase = md.querySelector('.caret').style.animationDelay;
  assert(/^-[\d.]+ms$/.test(firstCursorPhase),
    'a recreated markdown caret must rejoin the global animation phase');
  assert.strictEqual(md.querySelector('.normal-trail'), null);
  assert.strictEqual(Array.from(md.querySelector('.important-trail').textContent).length, 7,
    'heading accent must remain bounded to the seven-character typing trail');
  assert.strictEqual(heading.textContent, original, 'decorations must not change visible heading text');

  ctx.placeCaret(md);
  assert.strictEqual(md.querySelectorAll('.caret').length, 1, 'each tick owns exactly one caret');
  assert.notStrictEqual(md.querySelector('.caret').style.animationDelay, firstCursorPhase,
    'DOM replacement must advance rather than restart the cursor breath');
  assert.strictEqual(md.querySelectorAll('.important-trail').length, 1, 'heading trails must not accumulate');
  assert.strictEqual(heading.textContent, original);
  ctx.clearTypingDecorations(md);
  assert.strictEqual(md.querySelector('.caret'), null);
  assert.strictEqual(md.querySelector('.normal-trail'), null);
  assert.strictEqual(heading.textContent, original, 'completion must restore a plain text node');

  const plain = new MiniNode('div');
  const p = new MiniNode('p');
  p.appendChild(miniDocument.createTextNode('Обычный ответ'));
  plain.appendChild(p);
  ctx.placeCaret(plain);
  assert(!plain.querySelector('.caret').classList.contains('caret-important'));
  assert.strictEqual(plain.querySelector('.important-trail'), null);
  assert.strictEqual(Array.from(plain.querySelector('.normal-trail').textContent).length, 7,
    'ordinary typing has the shorter bounded blue trail');
  ctx.clearTypingDecorations(plain);
  p.textContent = 'Важно: перед удалением нужна резервная копия';
  ctx.markImportantThought(plain);
  ctx.placeCaret(plain);
  assert(p.classList.contains('action-important') && plain.querySelector('.important-trail'),
    'explicitly important thoughts receive the rare gold trail without model markup');
  ctx.clearTypingDecorations(plain);
  assert.strictEqual(plain.querySelector('.typing-trail'), null,
    'when typing pauses, the last word must immediately return to plain text');
  p.textContent = 'Главное: проверьте результат перед публикацией';
  ctx.markImportantThought(plain);
  ctx.placeCaret(plain);
  assert(plain.querySelector('.caret').classList.contains('caret-important'),
    'a key recommendation must receive the gold semantic marker without extra model tokens');
  ctx.clearTypingDecorations(plain);
  p.textContent = 'Обычный ответ без риска';
  ctx.markImportantThought(plain);
  assert(!p.classList.contains('action-important'),
    'semantic highlighting must clear when the current thought is ordinary');

  const longMd = new MiniNode('div');
  const longP = new MiniNode('p');
  longP.textContent = 'Подробное объяснение без служебной разметки. '.repeat(9);
  longMd.appendChild(longP);
  ctx.markImportantThought(longMd);
  ctx.placeCaret(longMd);
  assert(longMd.querySelector('.caret').classList.contains('caret-important'),
    'every sufficiently long response receives a deterministic important moment');

  const marked = new MiniNode('mark');
  marked.appendChild(miniDocument.createTextNode('Подтвердить действие'));
  plain.appendChild(marked);
  ctx.placeCaret(plain);
  assert(plain.querySelector('.caret').classList.contains('caret-important'),
    'only an explicitly marked action enters important mode');
  assert(plain.querySelector('.important-trail'));

  const table = new MiniNode('table');
  const tbody = new MiniNode('tbody');
  const row = new MiniNode('tr');
  const cell = new MiniNode('td');
  cell.appendChild(miniDocument.createTextNode('Последняя ячейка'));
  row.appendChild(cell); tbody.appendChild(row); table.appendChild(tbody);
  const tableMd = new MiniNode('div'); tableMd.appendChild(table);
  ctx.placeCaret(tableMd);
  assert.strictEqual(tableMd.querySelectorAll('.caret').length, 1,
    'a markdown table must retain the live caret while its last cell is typing');
  assert.strictEqual(tableMd.querySelector('.caret').parentNode, cell,
    'the caret belongs beside the last table-cell text, never below the table');
  assert(!/tagName\s*===\s*['"]TABLE['"]/.test(extractFunction(js, 'placeCaret')),
    'TABLE must not be treated as an opaque cursor owner');

  assert(/\.caret\s*\{[^}]*#00bff3[^}]*box-shadow/s.test(css), 'normal cursor is visibly bright blue');
  const goldCaret = css.match(/\.caret\.caret-important\s*\{([^}]*)\}/s);
  assert(goldCaret && /opacity\s*:\s*1/.test(goldCaret[1]) && /#ffd34f/.test(goldCaret[1]),
    'important caret must be a clearly visible saturated yellow marker');
  assert(!/#fff(?:fff)?\b/i.test(goldCaret[1]), 'gold caret must not flare to pure white');
  assert(/\.normal-trail\s*\{[^}]*linear-gradient\(90deg,#9ab0ba[^}]*#74c7d8[^}]*#b6edf3[^}]*text-shadow/s.test(css),
    'ordinary cursor trail must remain delicately but visibly blue');
  assert(/\.important-trail\s*\{[^}]*linear-gradient\(90deg,var\(--tx\)\s+0%[^}]*#ffd34f[^}]*#ffc83f[^}]*#ffe88f[^}]*text-shadow/s.test(css),
    'gold trail must visibly mark important thought without changing typing speed');
  assert(/\.caret\s*\{[^}]*animation\s*:\s*cursorBreathe[^}]*\}/s.test(css));
  assert(!/@keyframes (?:spark|twBlink)[^{]*\{[^}]*box-shadow/s.test(css),
    'cursor animation must stay on compositor opacity/transform');
  const events = extractFunction(js, 'handleEvent');
  assert(!/actionAccent/.test(js),
    'tool and plan events must not recolor the whole following markdown answer');
  assert(/tagName\s*===\s*['"]MARK['"]/.test(extractFunction(js, 'placeCaret')) &&
    /action-important/.test(extractFunction(js, 'placeCaret')),
  'gold mode follows deterministic locally marked important fragments');
  const question = extractFunction(js, 'questionCard');
  assert(/ask-own/.test(question) && /Свой вариант/.test(question),
    'blocking ask_user controls must offer the same custom answer escape hatch');
  const panels = extractFunction(js, 'mountUiPanels');
  assert(/!hasMeaningfulUiItems\(items\)/.test(panels),
    'a panel made only of free text must be removed because the composer already exists');
  assert(/hasFreeEntry/.test(panels) && /askable\s*&&\s*!hasFreeEntry/.test(panels),
    'free entry alongside a real choice must not create a second custom-answer field');
}

function makePlanItem(text) {
  const li = new MiniNode('li');
  li._planText = text;
  const copy = new MiniNode('span');
  copy.className = 'plan-copy';
  li.appendChild(copy);
  return { li, copy };
}

function testPlanTypingCompletionAndDockRaces() {
  assert(/const PLAN_CPS\s*=\s*220/.test(js),
    'plan intro must use its own fast typing lane');
  assert(/const PLAN_ITEM_PAUSE\s*=\s*300/.test(js) && /const PLAN_LOOK_MS\s*=\s*520/.test(js),
    'plan items stay quick while the completed intro remains readable before ascent');
  assert(/const PLAN_FLY_MS\s*=\s*640/.test(js),
    'the event gate must match the expressive dock flight transition');
  const typer = extractFunction(js, 'typePlanItem');
  assert(/PLAN_CPS \* elapsed/.test(typer) && /Math\.min\(250, now - last\)/.test(typer),
    'plan typing must be elapsed-time based and recover from ordinary Safari stalls');
  assert(/at - 7/.test(typer), 'plan action trail must stay short');

  // Structural root cause regression: incoming SSE events are not allowed to
  // race the visual plan intro. They remain in source order until the flight
  // callback releases the gate.
  const handled = [];
  const gateTimers = [];
  const gateCtx = loadFunctions(
    ['beginPlanGate', 'dispatchStreamEvent', 'releasePlanGate', 'cancelPlanGate'],
    { Promise, handleEvent(ev) { handled.push(ev.type); },
      setTimeout(fn, ms) { gateTimers.push({ fn, ms }); return gateTimers.length; },
      clearTimeout() {} },
  );
  const planStatus = new MiniNode('div');
  const gated = { statusEl: planStatus };
  gateCtx.beginPlanGate(gated);
  assert(planStatus.classList.contains('gate-hold'),
    'X: during the plan intro the caret line stays ALIVE — only its caption hides, so the user never sees a dead screen');
  assert(gated.planWatchdog, 'plan gate must arm a liveness watchdog');
  gateCtx.dispatchStreamEvent({ type: 'delta' }, gated);
  gateCtx.dispatchStreamEvent({ type: 'tool_start' }, gated);
  assert.deepStrictEqual(handled, [], 'no answer/tool content may appear while the plan is writing or flying');
  gateCtx.releasePlanGate(gated);
  assert.deepStrictEqual(handled, ['delta', 'tool_start'], 'the original SSE order must resume after arrival');
  assert(!planStatus.classList.contains('gate-hold'),
    'the caption returns together with the answer after the plan arrives');
  assert.strictEqual(gated.planGate, false);
  assert.strictEqual(gated.planIntroPromise, null);

  const reveal = extractFunction(js, 'revealPlanItems');
  assert(/dockPlan\(ui, \(\) => releasePlanGate\(ui\)\)/.test(reveal),
    'only the completed dock flight may release answer events');
  assert(/PLAN_ITEM_PAUSE/.test(reveal) && /PLAN_LOOK_MS/.test(reveal));
  assert(/await waitForPlanGate\(ui\)/.test(extractFunction(js, 'send')),
    'stream completion must also wait for the visual plan gate');
  assert(!/doneCount\s*=\s*Object\.keys\(ui\.tools\)/.test(js),
    'tool count must never guess or prematurely complete plan steps');

  // Completion first turns the real dock wholly green, then leaves an
  // accessible archive in the message. It never restores the old card.
  const timers = [];
  const trace = [];
  const dock = new MiniNode('div'); dock.className = 'plan-dock live expanded'; dock.dataset.runId = '7';
  dock._rect = { top: 10, left: 20, width: 600, height: 90, bottom: 100 };
  const title = new MiniNode('span'); title.className = 'pd-t'; dock.appendChild(title);
  const step = new MiniNode('span'); step.className = 'pd-step'; dock.appendChild(step);
  const segment = new MiniNode('span'); segment.className = 'pd-seg now'; dock.appendChild(segment);
  const dot = new MiniNode('span'); dot.className = 'pd-s now'; dock.appendChild(dot);
  const card = new MiniNode('div');
  const archiveThumb = new MiniNode('button');
  archiveThumb._rect = { top: 420, left: 60, width: 420, height: 44, bottom: 464 };
  const finishCtx = loadFunctions(['undockPlan'], {
    PLAN_DONE_HOLD_MS: 2100,
    PLAN_FOLD_MS: 680,
    clearPlanTimers() { trace.push('clear'); },
    finishPlanItems() { trace.push('finish'); },
    releasePlanGate() { trace.push('release'); },
    archiveCompletedPlan() { trace.push('archive'); return archiveThumb; },
    requestAnimationFrame(fn) { fn(); },
    $$(selector, rootNode) {
      if (rootNode) return rootNode.querySelectorAll(selector);
      return selector === '.plan-dock' ? [dock] : [];
    },
    setTimeout(fn, ms) { timers.push({ fn, ms }); return timers.length; },
  });
  const ui = {
    runId: 7, planFinished: false, planDock: dock, planCard: card,
    agentMode: true, planItems: [makePlanItem('Шаг').li],
  };
  finishCtx.undockPlan(ui);
  assert.deepStrictEqual(trace, ['clear', 'finish', 'release']);
  assert(ui.planFinished && !card.isConnected, 'completion removes the obsolete flying source card');
  assert(dock.classList.contains('done') && !dock.classList.contains('live'));
  assert.strictEqual(title.textContent, 'План выполнен');
  assert.strictEqual(step.textContent, 'готово');
  assert(segment.classList.contains('done') && !segment.classList.contains('now'));
  assert(dot.classList.contains('done') && !dot.classList.contains('now'));
  assert.deepStrictEqual(timers.map((timer) => timer.ms), [2100],
    'the completed green dock remains readable before it starts folding');
  timers[0].fn();
  assert.deepStrictEqual(trace, ['clear', 'finish', 'release', 'archive']);
  assert(dock.classList.contains('plan-folding') && dock.isConnected,
    'the dock must animate toward the real archive row instead of vanishing');
  assert(archiveThumb.classList.contains('plan-archive-reveal'));
  assert(/translate3d\(/.test(dock.style.transform) && /scale\(/.test(dock.style.transform));
  assert.strictEqual(timers[1].ms, 760, 'the source dock survives for the full fold transition');
  timers[1].fn();
  assert(!dock.isConnected);

  const archive = extractFunction(js, 'archiveCompletedPlan');
  assert(/!ui\.agentMode/.test(archive) && /!\(ui\.planItems \|\| \[\]\)\.length/.test(archive),
    'normal chat and empty plans must never create a completed-plan tab');
  assert(/collapseToThumb\(card/.test(archive) && /th-plan/.test(archive) && /instant: true/.test(archive),
    'the full completed plan must persist as an immediately readable collapsed tab');
  const collapse = extractFunction(js, 'collapseToThumb');
  assert(/reopenOpts\.cls[\s\S]*replace\(\/\\bplan-archive-target\\b\/g/.test(collapse) &&
    /Object\.assign\(\{\}, opts, \{ instant: false \}\)/.test(collapse),
    'after first expansion, later plan thumbnails must not reuse the invisible one-shot FLIP target');

  // Execute two full open/fold cycles. The second thumbnail used to be an
  // invisible pointer-events:none FLIP target and could never receive a click.
  const planRoot = new MiniNode('div');
  const planCard = new MiniNode('div'); planCard.className = 'panel-card plan-card';
  const planHead = new MiniNode('div'); planHead.className = 'card-head open';
  const planBody = new MiniNode('div'); planBody.className = 'card-body open';
  planCard.appendChild(planHead); planCard.appendChild(planBody); planRoot.appendChild(planCard);
  const foldTimers = [];
  const foldCtx = loadFunctions(['setCardOpen', 'growHeight', 'collapseToThumb', 'addFoldButton'], {
    Date, Object, String, Math, document: miniDocument, el: miniEl,
    esc: (value) => String(value), ICO: { bell: 'bell' },
    window: { getSelection: () => '' },
    setTimeout(fn, ms) { foldTimers.push({ fn, ms, cancelled: false }); return foldTimers.length; },
    clearTimeout(id) { if (foldTimers[id - 1]) foldTimers[id - 1].cancelled = true; },
  });
  const thumb1 = foldCtx.collapseToThumb(planCard, {
    cls: 'th-plan plan-archive-target', icon: '☰', title: 'План', instant: true,
  });
  thumb1.classList.add('plan-archive-reveal');
  thumb1.listeners.click();
  let foldButton = planCard.querySelector('.th-fold');
  assert(foldButton, 'first expansion must expose a fold control');
  foldButton.listeners.click({ stopPropagation() {} });
  while (foldTimers.length) {
    const timer = foldTimers.shift();
    if (!timer.cancelled) timer.fn();
  }
  const thumb2 = planRoot.querySelector('.thumb');
  assert(thumb2 && !thumb2.classList.contains('plan-archive-target'),
    'second thumbnail must be visible and clickable');
  thumb2.listeners.click();
  foldButton = planCard.querySelector('.th-fold');
  assert(foldButton && planCard.style.display === '',
    'the same completed plan must open for the second time');

  assert(!/archiveCompletedPlan/.test(extractFunction(js, 'discardPlan')),
    'stopped or failed plans must not be misrepresented as completed archives');
  assert(!/\.pd-s\.now::after\s*\{/.test(css), 'current dock step must have no underline pseudo-element');
  assert(/Array\.from\(\{ length: n \}[\s\S]*pd-seg/.test(extractFunction(js, 'dockPlan')),
    'the top progress bar must create exactly one equal segment per plan item');
  assert(/\.pd-bar\s*\{[^}]*display:flex[^}]*gap/s.test(css) &&
    /\.pd-seg\s*\{[^}]*flex:1 1 0/s.test(css),
    'plan progress segments must share the available width equally');
  assert(!/\.pd-seg\.now::after\s*\{/.test(css));
  assert(/\.pd-seg\.now\s*\{[^}]*animation:segmentBeat 1\.05s/s.test(css) &&
    /@keyframes segmentBeat\s*\{50%\{[^}]*box-shadow:0 0 17px/s.test(css),
    'the whole current segment must pulse in sync with the current plan dot');
  assert(/\.plan-card\.plan-complete/.test(css) && /\.plan-dock\.done/.test(css));
  assert(/const PLAN_FLY_MS\s*=\s*640/.test(js) &&
    /const PLAN_DONE_HOLD_MS\s*=\s*2100/.test(js) &&
    /const PLAN_FOLD_MS\s*=\s*680/.test(js),
    'plan ascent, completion hold, and archive morph must remain deliberately expressive');
  assert(/\.plan-card\.plan-complete\s*\{[^}]*background:var\(--panel2\)/s.test(css) &&
    /\.plan-card\.plan-complete \.card-head\s*\{[^}]*background:rgba\(63,191,149,\.09\)/s.test(css) &&
    /\.plan-card\.plan-complete \.plan-list li\s*\{[^}]*color:var\(--tx2\)/s.test(css),
    'completed archive keeps only its frame and header green while its interior remains neutral');

  const finish = extractFunction(js, 'queueResponseFinish');
  assert(/if\s*\(!ui\.buffer\)\s*ui\.buffer\s*=\s*doneContent/.test(finish));
  assert(/content\s*=\s*ui\.buffer/.test(finish));
  assert(!/ui\.shown\s*=\s*['"]{2}/.test(finish),
    'done must never erase a fully streamed multi-step answer and retype the last step');
}

function testRepeatedPlanEventReplacesOwnership() {
  const removedDocks = [];
  const scheduled = [];
  const completionOrder = [];
  const status = new MiniNode('div');
  const body = new MiniNode('div');
  body.appendChild(status);
  const oldCard = new MiniNode('div');
  body.insertBefore(oldCard, status);
  const oldDock = new MiniNode('div');
  let clears = 0;
  const makeCard = () => {
    const card = new MiniNode('div');
    card.inner = new MiniNode('div');
    card.appendChild(card.inner);
    return card;
  };
  const ctx = loadFunctions(['handleEvent'], {
    S: {},
    flushQt() {}, flushTools() {}, flushAgentGroup() {}, finishToolWait() {},
    beginPlanGate(ui) { ui.planGate = true; },
    clearPlanTimers() { clears += 1; },
    dropStrayDocks(keep) { removedDocks.push(keep); oldDock.remove(); },
    makeCard, markBorn() {}, el: miniEl,
    pinToBottom() {}, chaseBottom() {}, stream() { return body; },
    planLater(_ui, fn, ms) { scheduled.push({ fn, ms }); },
    revealPlanItems() {}, sfx() {},
    dropStatus() { completionOrder.push('status'); },
    updateResponseMeta() {},
    undockPlan() { completionOrder.push('plan'); },
    queueResponseFinish() { completionOrder.push('typing'); },
  });
  const ui = {
    node: { body }, statusEl: status, planDock: oldDock, planCard: oldCard,
    planItems: [], planList: null, planHome: null, planFinished: false,
    agentMode: true,
  };
  ctx.handleEvent({ type: 'plan', steps: ['Один', 'Два'] }, ui);
  const firstReplacement = ui.planCard;
  assert.strictEqual(oldCard.isConnected, false);
  assert.strictEqual(ui.planItems.length, 2);
  assert.strictEqual(scheduled[0].ms, 100);
  ctx.handleEvent({ type: 'plan', steps: ['Новый'] }, ui);
  assert.strictEqual(firstReplacement.isConnected, false, 'a repeated plan event must remove its predecessor');
  assert.strictEqual(ui.planItems.length, 1);
  assert.strictEqual(clears, 2);
  assert.strictEqual(removedDocks.length, 2);

  ctx.handleEvent({ type: 'done', content: 'готово' }, ui);
  // План НЕ завершается по сетевому done: dock остаётся живым, пока ответ
  // допечатывается. undockPlan вызывается из финала typer (onTyped), а не здесь.
  assert.deepStrictEqual(completionOrder, ['status', 'typing'],
    'server done must NOT complete the plan; typing finish does');
  const finish = extractFunction(js, 'queueResponseFinish');
  assert(/if\s*\(success\)\s*\{[\s\S]*?undockPlan\(ui\)/.test(finish),
    'undockPlan lives inside the onTyped callback, after the response is fully typed');
}

function makeMedia() {
  const listeners = {};
  const track = {
    stopped: 0,
    addEventListener(name, fn) { listeners[name] = fn; },
    stop() { this.stopped += 1; },
  };
  return {
    track, listeners,
    getTracks() { return [track]; },
    getVideoTracks() { return [track]; },
  };
}

function makeCameraNode(video, feed, chat) {
  const node = new MiniNode('div');
  node.parts = { '.cam-video': video, '.cam-feed': feed, '.cam-chat': chat };
  node.querySelector = function query(selector) { return this.parts[selector] || null; };
  node.querySelectorAll = function queryAll(selector) {
    if (selector === '.cam-line' && feed) return feed.querySelectorAll(selector);
    return [];
  };
  return node;
}

async function testCameraLifecycleOwnershipAndLateResults() {
  const toggle = { classList: { remove() {} } };
  const video = { srcObject: null };
  let nextMedia = makeMedia();
  let requests = 0;
  const card = makeCameraNode(video, new MiniNode('div'), new MiniNode('div'));
  const S = {
    camRun: 0, camStream: null, camNode: card, cameraOn: true,
    camTimer: null, camPrevPix: null, camBusy: false,
  };
  const ctx = loadFunctions(['camPart', 'startCam', 'stopCam',
    'adoptRunIntoCam', 'releaseRunFromCam'], {
    S, CAM_TICK: 2500, ICO: { cam: '' },
    // AB: камера общается с голосовым режимом (единый интерфейс) — в песочнице
    // теста разговора нет, поэтому старт/стоп камеры его просто не трогает
    VOICE: { open: false },
    navigator: { mediaDevices: { async getUserMedia() { requests += 1; return nextMedia; } } },
    showView() {}, killWelcome() {}, buildCamCard() { throw new Error('unexpected rebuild'); },
    stream() { throw new Error('unexpected stream lookup'); },
    addFoldButton() {}, scrollDown() {}, blip() {}, camState() {}, camSay() {},
    sfx() {}, toast() {}, camTick() {}, collapseToThumb() {},
    setInterval() { return 71; }, clearInterval() {},
    $(selector) { return selector === '#tgCamera' ? toggle : null; },
  });

  await ctx.startCam();
  assert.strictEqual(requests, 1);
  assert.strictEqual(S.camStream, nextMedia);
  assert.strictEqual(video.srcObject, nextMedia, 'live video lookup is scoped to the owned camera card');
  nextMedia.listeners.ended();
  assert.strictEqual(S.camStream, null);
  assert.strictEqual(S.cameraOn, false);

  S.cameraOn = true;
  nextMedia = makeMedia();
  await ctx.startCam();
  assert.strictEqual(requests, 2, 'camera must request hardware again after track ended');
  ctx.stopCam();
  assert.strictEqual(nextMedia.track.stopped, 1);
  assert.strictEqual(S.camNode, null);
  assert.strictEqual(S.camBusy, false);

  // getUserMedia resolving after close must release hardware and must not
  // resurrect a stale generation.
  const lateVideo = { srcObject: null };
  S.camNode = makeCameraNode(lateVideo, new MiniNode('div'), new MiniNode('div'));
  S.cameraOn = true;
  let resolveLate;
  const lateMedia = makeMedia();
  ctx.navigator.mediaDevices.getUserMedia = () => new Promise((resolve) => { resolveLate = resolve; });
  const pending = ctx.startCam();
  ctx.stopCam();
  resolveLate(lateMedia);
  await pending;
  assert.strictEqual(lateMedia.track.stopped, 1);
  assert.strictEqual(S.camStream, null);

  // Reopened cards own their feed. Duplicate old camera markup can remain in
  // history without receiving current comments or messages.
  const oldFeed = new MiniNode('div');
  const newFeed = new MiniNode('div');
  const oldCard = makeCameraNode({}, oldFeed, new MiniNode('div'));
  const newCard = makeCameraNode({}, newFeed, new MiniNode('div'));
  S.camNode = newCard;
  const sayCtx = loadFunctions(['camPart', 'camSay'], {
    S, Date, el: miniEl, esc: String, MD: { render: String }, scrollDown() {},
  });
  sayCtx.camSay('новый сеанс');
  assert.strictEqual(oldFeed.childNodes.length, 0);
  assert.strictEqual(newFeed.childNodes.length, 1);
  sayCtx.camSay('обновлённый кадр');
  assert.strictEqual(newFeed.childNodes.length, 1, 'live scene description replaces itself instead of growing');

  // A late `chat` event from oldCard cannot overwrite the chat id of newCard;
  // user message ids are assigned to the exact node owned by the request.
  const userNode = new MiniNode('div'); userNode.dataset.msgId = '';
  S.camNode = newCard; S.camChatId = 'new-session'; S.chatId = 'main';
  const eventCtx = loadFunctions(['handleEvent'], { S, flushQt() {}, flushTools() {},
    flushAgentGroup() {}, finishToolWait() {}, ensureStatus: () => null });
  eventCtx.handleEvent({ type: 'chat', chat_id: 'stale-id' }, {
    node: {}, isolatedCamera: true, cameraNode: oldCard, userMsgNode: userNode,
  });
  assert.strictEqual(S.camChatId, 'new-session');
  eventCtx.handleEvent({ type: 'chat', chat_id: 'active-id' }, {
    node: {}, isolatedCamera: true, cameraNode: newCard, userMsgNode: userNode,
  });
  assert.strictEqual(S.camChatId, 'active-id');
  eventCtx.handleEvent({ type: 'user_msg', id: 'message-28' }, {
    node: {}, isolatedCamera: true, cameraNode: newCard, userMsgNode: userNode,
  });
  assert.strictEqual(userNode.dataset.msgId, 'message-28');

  const sendSource = extractFunction(js, 'send');
  assert(/const requestCamNode\s*=\s*\(!requestVoice && camLive\(\)\)\s*\?\s*S\.camNode/.test(sendSource));
  assert(/const atts\s*=\s*S\.attachments\.slice\(\)/.test(sendSource));
  assert(sendSource.indexOf('const atts = S.attachments.slice()') < sendSource.indexOf('await camAttachFrame'),
    'request attachments must be captured before the camera upload yields');
  assert(/camAttachFrame\(requestChatId, controller\.signal\)/.test(sendSource));
  assert(/frame\.fromCam\s*=\s*true;\s*atts\.push\(frame\)/.test(sendSource));
  assert(!/S\.attachments\.push\(frame\)/.test(sendSource),
    'a late frame must never leak into the next request attachment tray');
  assert(/addAiMsg\(null, requestHost\)/.test(sendSource));
  assert(/chat_id:\s*requestChatId,\s*kind:\s*requestKind/.test(sendSource));
  assert(!/querySelector\('#cam/.test(js), 'camera runtime must not use duplicate global ids');

  // Deterministic slow-upload race: compose state is already request-owned,
  // Stop aborts the upload itself, and no cancelled request can start its SSE.
  const input = { value: 'сделай мем' };
  const reply = { hidden: false, innerHTML: 'old' };
  const requestChat = new MiniNode('div');
  const requestCard = makeCameraNode({}, new MiniNode('div'), requestChat);
  const mainHost = new MiniNode('div');
  const requestState = {
    attachments: [{ name: 'notes.txt' }], editing: null, streaming: false,
    streamRun: 0, abort: null, camStream: {}, camNode: requestCard,
    camLink: false, camChatId: 'camera-owned', chatId: 'main',
    agentMode: true, computerUse: false, replyTicket: 0,
  };
  let markUploadStarted;
  const uploadStarted = new Promise((resolve) => { markUploadStarted = resolve; });
  let streamRequests = 0;
  const fetchFake = (url, options) => {
    if (url === '/api/upload') {
      markUploadStarted();
      return new Promise((_resolve, reject) => {
        options.signal.addEventListener('abort', () => {
          const error = new Error('aborted'); error.name = 'AbortError'; reject(error);
        }, { once: true });
      });
    }
    streamRequests += 1;
    throw new Error('cancelled camera upload reached the SSE endpoint');
  };
  let renderedAttachments = -1;
  let stoppedPlans = 0;
  const aiRoot = new MiniNode('div');
  const aiBody = new MiniNode('div');
  aiRoot.appendChild(aiBody);
  const sendCtx = loadFunctions(['api', 'camAttachFrame', 'stopStream', 'send'], {
    S: requestState, AbortController, fetch: fetchFake,
    flushTools() {},
    setInterval, clearInterval,
    $: (selector) => selector === '#input' ? input : (selector === '#replyBar' ? reply : null),
    foldAllNotes() {}, camLive() { return true; }, stream() { return mainHost; },
    renderAttachments() { renderedAttachments = requestState.attachments.length; },
    autoGrow() {}, addUserMsg() { return new MiniNode('div'); },
    addAiMsg() { return { root: aiRoot, body: aiBody, modelEl: new MiniNode('span') }; },
    setStreaming(value) { requestState.streaming = value; }, sfx() {}, thinkMode() {},
    watchRunFollow() {},
    camFrame() { return 'data:image/jpeg;base64,frame'; },
    typerStop() {}, dropStatus() {},
    markStopped() {},
    stopRunForReal() {},
    cancelPlanGate() { stoppedPlans += 1; },
    async waitForPlanGate() {},
    discardPlan() {},
    el: miniEl,
    settleVisualDone(ui) {
      ui.visualDone = true;
      if (ui.resolveVisualDone) ui.resolveVisualDone();
    },
    saveDraft() {},
    maybeOfferScenario() {},
    foldCodeBlocks() {}, thinkFlush() { return null; }, collapseSoon() {},
    ICO: {}, fmtSize() { return ''; },
    queueResponseFinish() { throw new Error('aborted upload must not finalize as a response'); },
    loadChats() {}, refreshState() {}, fetchReplies() {},
    activeChatId() { return requestState.chatId || ''; },
    flyWelcomeInto() {}, dotShapePlay() {}, dotAction() {},
    document: { querySelector: () => null },
  });
  const pendingSend = sendCtx.send();
  await uploadStarted;
  assert.strictEqual(input.value, '', 'input must be detached before camera upload waits');
  assert.strictEqual(renderedAttachments, 0, 'global tray belongs to the next compose immediately');
  assert.strictEqual(requestState.streaming, true, 'Stop must be available during upload');
  const pendingStop = sendCtx.stopStream();
  await Promise.all([pendingSend, pendingStop]);
  assert.strictEqual(streamRequests, 0);
  assert.strictEqual(requestState.attachments.length, 0);
  assert.strictEqual(requestState.streaming, false);
  assert.strictEqual(stoppedPlans, 1);
}

function testPendingInteractivePanelAndRouteLifecycle() {
  const scroller = new MiniNode('div'); scroller.className = 'cam-chat';
  scroller.scrollHeight = 1000; scroller.scrollTop = 500; scroller.clientHeight = 400;
  const node = new MiniNode('article');
  const body = new MiniNode('div'); node.body = body; node.appendChild(body); scroller.appendChild(node);
  const md = new MiniNode('div'); md.className = 'md'; body.appendChild(md);
  const status = new MiniNode('div'); body.appendChild(status);
  let mounted = 0;
  let followed = 0;
  let typerStarts = 0;
  let metaRefreshes = 0;
  const ctx = loadFunctions(
    ['deferMountedReplyUi', 'flushPendingReplyUi', 'typeInto', 'clearRunRoute', 'finishLiveDot', 'settleVisualDone'],
    {
      String, el: miniEl, S: { followUi: null },
      parseUiSpec() { return [{ t: 'tiles' }]; },
      hasMeaningfulUiItems(items) { return items.some((item) => item.t !== 'text' && item.t !== 'area'); },
      mountPlotPanels() {},
      mountUiPanels(root) {
        mounted += 1;
        const panel = root.querySelector('.ui-panel');
        panel.dataset.live = '1';
        panel.appendChild(new MiniNode('button'));
      },
      scrollDown() {},
      followGrowingPanel() { followed += 1; },
      typerStart() { typerStarts += 1; },
      updateResponseMeta() { metaRefreshes += 1; },
    },
  );
  const spec = 'tiles Формат: PDF | Word | Markdown';
  const ui = {
    node, mdEl: md, statusEl: status,
    buffer: 'Текст ещё печатается', shown: 'Текст ещё',
    replyUiSpec: spec, pendingReplyUi: spec, replyLive: null,
    visualDone: false,
  };

  assert.strictEqual(ctx.flushPendingReplyUi(ui), false);
  assert.strictEqual(mounted, 0,
    'controls must remain pending until every preceding character is visible');
  ui.shown = ui.buffer;
  assert.strictEqual(ctx.flushPendingReplyUi(ui), true);
  const firstLive = ui.replyLive;
  assert(firstLive && firstLive.querySelector('button'));
  assert.strictEqual(mounted, 1);
  assert.strictEqual(followed, 1);
  assert.strictEqual(body.children.indexOf(firstLive), body.children.indexOf(status) - 1,
    'the visual-boundary controls belong immediately after the completed response');

  ctx.typeInto(ui, ' и это поздний delta');
  assert.strictEqual(firstLive.isConnected, false,
    'a late text delta must unmount controls that would otherwise overtake it');
  assert.strictEqual(ui.replyLive, null);
  assert.strictEqual(ui.pendingReplyUi, spec);
  assert.strictEqual(typerStarts, 1);
  ui.shown = ui.buffer;
  assert.strictEqual(ctx.flushPendingReplyUi(ui), true);
  assert.notStrictEqual(ui.replyLive, firstLive,
    'the same controls remount only at the next real visual boundary');
  assert.strictEqual(mounted, 2);

  const route = new MiniNode('span'); body.appendChild(route);
  let resolved = 0;
  ui.routeEl = route;
  ui.resolveVisualDone = () => { resolved += 1; };
  ctx.settleVisualDone(ui);
  assert.strictEqual(route.isConnected, true);
  assert.strictEqual(ui.routeEl, route, 'visual completion keeps response metadata attached');
  assert.strictEqual(metaRefreshes, 1);
  assert.strictEqual(resolved, 1);
  ctx.settleVisualDone(ui);
  assert.strictEqual(resolved, 1, 'visual settlement and persistent metadata refresh are idempotent');

  const events = extractFunction(js, 'handleEvent');
  assert(/case 'reply_ui':[\s\S]*pendingReplyUi\s*=\s*spec[\s\S]*flushPendingReplyUi\(ui\)/.test(events));
  assert(/case 'delta':[\s\S]*typeInto\(ui, ev\.text\)/.test(events));
  const finish = extractFunction(js, 'queueResponseFinish');
  assert(!/savedScroll|oldScroll|scrollTop\s*=\s*(?:before|old|saved)/i.test(finish),
    'panel mounting must preserve follow intent, not restore a stale scroll coordinate');
}

function testInteractiveFenceReachesFrontendPanel() {
  const meaningful = loadFunctions(['hasMeaningfulUiItems'], {});
  assert.strictEqual(meaningful.hasMeaningfulUiItems([{ t: 'text' }, { t: 'area' }]), false,
    'free-only controls must fall back to the one main composer');
  assert.strictEqual(meaningful.hasMeaningfulUiItems([{ t: 'tiles' }, { t: 'text' }]), true,
    'a custom answer remains allowed next to a real predefined choice');
  const context = { window: {} };
  vm.createContext(context);
  vm.runInContext(markdown, context);
  const rendered = context.window.MD.render(
    'Какой формат выбираем?\n\n```ui\ntiles Формат: PDF | Word | Markdown\n```',
  );
  assert(/<div class="ui-panel" data-ui="tiles Формат: PDF \| Word \| Markdown"><\/div>/.test(rendered),
    'canonical backend UI fence must arrive as a live frontend panel, not a code/plain list');
  const panels = extractFunction(js, 'mountUiPanels');
  assert(/parseUiSpec\(box\.dataset\.ui/.test(panels));
  assert(/addEventListener\(['"]click/.test(panels),
    'rendered choice tiles must have a real click handler');
  const events = extractFunction(js, 'handleEvent');
  assert(/case 'reply_ui':[\s\S]*ui\.replyUiSpec\s*=/.test(events),
    'frontend must consume the parser-independent reply_ui SSE event');
  const finish = extractFunction(js, 'queueResponseFinish');
  assert(/ui\.replyUiSpec\s*&&\s*embeddedPanels\.length\s*===\s*0/.test(finish));
  assert(/fallbackPanel\.dataset\.ui\s*=\s*ui\.replyUiSpec/.test(finish),
    'a missing markdown panel must be reconstructed directly in the final DOM');

  const root = new MiniNode('div');
  const list = new MiniNode('ol');
  ['Минимализм — светлый кадр', 'Ретро — плёнка', 'Кино — контраст'].forEach((text) => {
    const li = new MiniNode('li'); li.textContent = text; list.appendChild(li);
  });
  const question = new MiniNode('p'); question.textContent = 'Какой вариант выбираем?';
  const panel = new MiniNode('div'); panel.className = 'ui-panel';
  root.appendChild(list); root.appendChild(question); root.appendChild(panel);
  const helpers = loadFunctions(['choiceKey', 'stripMirroredChoiceList'], {
    String, $$: (selector, node) => node.querySelectorAll(selector),
  });
  helpers.stripMirroredChoiceList(panel, [{
    t: 'tiles', opts: ['Минимализм', 'Ретро', 'Кино'],
  }]);
  assert.strictEqual(list.parentNode, null,
    'plain mirrored options must be replaced by the styled tile controls');
  assert.strictEqual(question.parentNode, root, 'the actual question must stay visible');
}

function testRussianImageAndHudFollowupContract() {
  assert(!/js\.puter\.com|puter\.ai|loadPuter|handleBrowserImageRequest|case 'image_request'/.test(js + html),
    'the rejected Puter browser handoff must not remain in production UI');
  assert(/has_image_gateway/.test(js) && /Image Cloud/.test(js) && /не требует ваших ключей/.test(js),
    'production settings must expose zero-setup Russian image gateway status');

  const togglesAt = html.indexOf('<div class="toggles">');
  const togglesEnd = html.indexOf('<!-- ---------- VIEW: AUTO', togglesAt);
  const cameraAt = html.indexOf('id="tgCamera"', togglesAt);
  const computerAt = html.indexOf('id="tgComputer"', togglesAt);
  const spacerAt = html.indexOf('class="spacer"', togglesAt);
  const agentAt = html.indexOf('id="tgAgent"', togglesAt);
  assert(togglesAt >= 0 && cameraAt < computerAt && computerAt < spacerAt &&
    spacerAt < agentAt && agentAt < togglesEnd,
  'AGENT must occupy the right edge of the footer directly below Send');
  // AGENT: буква «A» стоит НА САМОМ круглешке тумблера (внутри <i>), рядом нет подписей
  assert(/<label class="agent-switch-track">\s*<input id="tgAgent" type="checkbox" role="switch"[^>]*>\s*<i aria-hidden="true">A<\/i>\s*<\/label>/.test(html),
    'the A letter must live ON the switch knob itself');
  assert(!/agent-switch-label/.test(html), 'no caption floats beside the AGENT track anymore');
  const budgetAt = html.indexOf('id="tgBudget"', togglesAt);
  assert(budgetAt > 0 && budgetAt > agentAt,
    'BM11: the ruble-limit button is the rightmost control (after AGENT) so its right edge lands exactly beneath the boost button');
  assert(!/<button[^>]+id="tgAgent"/.test(html));
  assert(/\.toggle\s*\{[^}]*height:28px[^}]*padding:0 12px/s.test(css));
  assert(/\.agent-switch\s*\{[^}]*height:28px[^}]*display:flex[^}]*border:0[^}]*background:transparent/s.test(css) &&
    /\.agent-switch-track\s*\{[^}]*width:42px[^}]*height:28px/s.test(css) &&
    /\.agent-switch-track input:checked \+ i\{[^}]*translateX\(10px\)/s.test(css) &&
    /\.send-btn\s*\{[^}]*width:42px[^}]*height:38px/s.test(css),
    'AGENT track is compact (36px, 12px travel); Send keeps its width');
  assert(/#tgAgent'\)\.addEventListener\('change'[\s\S]*S\.agentMode\s*=\s*this\.checked[\s\S]*tip-dismissed/.test(js));
  assert(/\$\$\('\.agent-switch'\)\.forEach\(\(sw\) => sw\.addEventListener\('mouseleave'[\s\S]*tip-dismissed/.test(js));
  const tipRule = css.match(/\.agent-switch\[data-tip\]:not\(\.tip-dismissed\):hover::after\s*\{([^}]*)\}/s);
  assert(/\.composer\s*\{[^}]*overflow:visible/s.test(css) && tipRule &&
    /z-index:60/.test(tipRule[1]) && /white-space:normal/.test(tipRule[1]) &&
    /animation:tipIn \.16s 1\.45s both/.test(tipRule[1]),
  'switch tooltip renders above the composer, wraps, and waits ~1s like an OS hint');

  const refresh = extractFunction(js, 'refreshState');
  assert(/st\.running_tasks/.test(refresh) && /auto-running', running > 0/.test(refresh),
    'AUTO shimmer is derived only from tasks actually running now');
  assert(!/auto-running', active > 0/.test(refresh),
    'queued or scheduled tasks must not animate AUTO');
  assert(/\.nav-item\[data-view="auto"\]\.auto-running \.nav-outline/.test(css) &&
    /animation:autoOutlineFlow/.test(css));
  assert(!/auto-running::after/.test(css), 'AUTO gradient must not fill the tab area');
  assert((html.match(/class="nav-outline"/g) || []).length === 6,
    'every navigation tab (включая Сценарии) must own the same outline-only effect layer');

  const events = extractFunction(js, 'handleEvent');
  assert(/case 'memory_saved':[\s\S]*memoryTraceCard\(facts\)[\s\S]*collapseSoon\(card[\s\S]*pulseNav\('memory', true\)/.test(events),
    'deterministic memory writes must create a visible tool trace and pulse Memory');
  assert(/case 'file':[\s\S]*pulseNav\('files', false\)/.test(events),
    'actual file saves trigger the Files outline pulse');
  assert(/\.nav-item\.save-glint \.nav-outline\s*\{[^}]*fileSaveOutline \.72s/s.test(css) &&
    /@keyframes fileSaveOutline/.test(css) && /@keyframes memorySaveOutline/.test(css),
    'Files and Memory use quick, clearly visible, outline-only colour passes');

  const autoType = extractFunction(js, 'typeAutoReply');
  assert(/typeInto\(ui, text\)/.test(autoType) && /md typing/.test(autoType),
    'delayed AUTO messages must use the normal cursor typing lane');
  const syncTail = extractFunction(js, 'syncChatTail');
  assert(/typeAutoReply\(node, m\.content/.test(syncTail));
  assert(!/node\.body\.innerHTML\s*=/.test(syncTail),
    'AUTO replies may not appear as an already completed HTML block');

  assert(/case 'reply_ui':[\s\S]*hasMeaningfulUiItems\(parseUiSpec\(spec\)\)/.test(events),
    'free-only reply UI must be rejected before mounting');
  const replies = extractFunction(js, 'showReplies');
  assert(/followGrowingPanel\(box, 520 \+ items\.length \* 60\)/.test(replies));
  const follow = extractFunction(js, 'followGrowingPanel');
  assert(/wheel/.test(follow) && /touchstart/.test(follow));

  assert(/\.composer-wrap\s*\{[^}]*linear-gradient\(180deg,rgba\(4,7,13,0\) 0%/s.test(css));
  assert(/ui\.routeTier\s*=\s*ev\.tier[\s\S]*updateResponseMeta\(ui\)[\s\S]*ui\.modelName\s*=\s*ev\.model[\s\S]*updateResponseMeta\(ui\)/.test(events),
    'scenario and model update one response-owned metadata line');
  const clearMeta = extractFunction(js, 'clearRunRoute');
  assert(/updateResponseMeta\(ui\)/.test(clearMeta) && !/\.remove\(\)/.test(clearMeta),
    'scenario and model persist above their response after visual completion');
  assert(/routeTier:\s*meta\.tier \|\| ''/.test(extractFunction(js, 'renderMessageInto')),
    'history restores the persisted scenario together with the model (X: per-message renderer extracted, fresh arrivals reuse it)');

  const metaRoot = new MiniNode('div');
  const metaHead = new MiniNode('div'); metaHead.className = 'ai-name'; metaRoot.appendChild(metaHead);
  const legacyModel = new MiniNode('span'); legacyModel.textContent = 'old';
  const metaCtx = loadFunctions(['updateResponseMeta'], {
    TIER_LABEL: { quality: 'качество' },
    PROV_SHORT: { cloudru: 'cloud.ru' },   // BM9: провайдер в паспорте ответа
    el: miniEl,
  });
  const metaUi = {
    node: { root: metaRoot, modelEl: legacyModel }, routeTier: 'quality',
    modelName: 'gpt-test', routeReason: 'reason', routeEl: null,
  };
  metaCtx.updateResponseMeta(metaUi);
  assert.strictEqual(metaUi.routeEl.textContent, 'качество · gpt-test');
  assert.strictEqual(metaUi.routeEl.parentNode, metaHead);
  assert.strictEqual(legacyModel.textContent, '', 'legacy model badge must not duplicate persistent metadata');
  const metaStyle = css.match(/\.ai-route\s*\{([^}]*)\}/s);
  assert(metaStyle && !/border|background|border-radius/.test(metaStyle[1]) &&
    /font:8\.5px/.test(metaStyle[1]) && /rgba\(154,202,219,\.43\)/.test(metaStyle[1]),
    'response metadata is tiny readable low-contrast text, never a framed pill');

  assert(/const permission = ev\.style === 'permission'/.test(events) &&
    /permission \? '◇ Можно открыть приложение\?'/.test(events) &&
    /permission \? '' : 'danger '/.test(events) && /sfx\(permission \? 'pop' : 'error'\)/.test(events),
    'Terminal window permission uses softer copy, buttons, and sound than a dangerous sanction');
  assert(/\.approve-card\.permission\s*\{[^}]*rgba\(112,151,255,\.4\)[^}]*rgba\(164,119,255,\.065\)/s.test(css),
    'permission cards use the calm blue-violet sanction variant');
}

function testReadinessFollowHistoryAndLiveCodeContracts() {
  const panels = extractFunction(js, 'mountUiPanels');
  assert(/const ready = \(\) => items\.every/.test(panels) &&
    /if \(x\.t === 'tiles'\) return x\.val != null/.test(panels) &&
    /const syncSendState = \(\) =>/.test(panels) && /go\.disabled = !valid/.test(panels),
    'one readiness synchronizer must own Send state for every required control');
  assert(/it\.t === 'slider'[\s\S]*controlChanged\(\)/.test(panels) &&
    /it\.t === 'tiles'[\s\S]*controlChanged\(\)/.test(panels) &&
    /ownVal = own\.value[\s\S]*syncSendState\(\)/.test(panels),
    'slider, selected tile, and custom answer all re-evaluate the same Send state');

  const scrollBox = new MiniNode('div'); scrollBox.className = 'cam-chat';
  scrollBox.scrollHeight = 1200; scrollBox.scrollTop = 800; scrollBox.clientHeight = 400;
  const root = new MiniNode('article'); scrollBox.appendChild(root);
  const followUi = { node: { root }, followOutput: true };
  const followCtx = loadFunctions(['runScrollBox', 'watchRunFollow', 'followLiveStream'], {
    Number, S: { followUi }, stream() { return scrollBox; },
  });
  followCtx.watchRunFollow(followUi);
  scrollBox.listeners.wheel({ deltaY: -20 });
  assert.strictEqual(followUi.followOutput, false,
    'an explicit upward wheel gesture releases output follow immediately');
  scrollBox.scrollTop = 500;
  scrollBox.listeners.scroll();
  assert.strictEqual(followUi.followOutput, false,
    'layout growth cannot silently re-enable follow while far from the bottom');
  scrollBox.scrollTop = 1190;
  scrollBox.listeners.scroll();
  assert.strictEqual(followUi.followOutput, true,
    'returning to the transcript tail explicitly resumes follow');

  const history = new MiniNode('div'); history.scrollHeight = 1700; history.scrollTop = 0;
  history.clientHeight = 500;
  const historyCtx = loadFunctions(['pinToBottom'], {
    $$: (selector, node) => node.querySelectorAll(selector),
  });
  historyCtx.pinToBottom(history);
  assert.strictEqual(history.scrollTop, 1700,
    'history is pinned to the final position synchronously on opening');
  const open = extractFunction(js, 'openChat');
  assert(open.includes('renderMessages(stream, r.messages || [])') &&
    open.indexOf('pinToBottom(stream)') > open.indexOf('renderMessages(stream, r.messages || [])') &&
    !/requestAnimationFrame|setTimeout/.test(open),
    'history opening must not visibly walk through staged scroll corrections');

  const typer = extractFunction(js, 'renderTyped');
  assert(/livePre\.classList\.add\('live-code'\)/.test(typer) &&
    /livePre\.scrollTop = livePre\.scrollHeight/.test(typer));
  const finish = extractFunction(js, 'queueResponseFinish');
  // live-code больше не снимается заранее (это распахивало блок на кадр);
  // foldCodeBlocks съёживает блок ОТ ВИДИМОЙ высоты и лишь потом снимает класс
  assert(/foldCodeBlocks\(ui\.mdEl,\s*true\)/.test(finish),
    'completed code folds with the animate path, never expanding to full height first');
  // S: при ОТКРЫТИИ диалога код сворачивается в стройные строки;
  // окно кода в развёрнутом виде — КОМПАКТНОЕ по высоте (треть экрана)
  assert(!/keepCodeOpen/.test(js) && !/code-open/.test(css) &&
    (js.match(/foldCodeBlocks\(node\.body\);/g) || []).length >= 2 &&
    /\.md pre\{[^}]*max-height:min\(30vh,260px\)/s.test(css) &&
    /\.md pre\.live-code\{max-height:min\(30vh,260px\)/.test(css) &&
    /\.code-block pre\{[^}]*max-height:min\(30vh,260px\)/.test(css),
    'opening a chat folds code back into slim rows; the expanded code window is COMPACT (a third of the screen)');
  // U: тик печати кредитует ЧЕСТНОЕ время (до 250мс), а не максимум 32мс:
  // тяжёлый кадр больше не режет темп текста — от пачек защищает предел шага
  assert(/Math\.min\(250, now - lastTick\)/.test(extractFunction(js, 'typerStart')) &&
    !/Math\.min\(32, now - lastTick\)/.test(js),
    'typer credits HONEST elapsed time (cap 250ms): heavy frames cut DOM frequency, never the text pace');
  // U: миниатюры в слотах кода не перезапускают анимацию при каждой перерисовке
  assert(/\.code-slot \.thumb\{animation:none\}/.test(css),
    'thumbs inside code slots never replay their pop-in animation on typer repaints');
  // S: печать больше не перестраивает ГОЛОВУ целиком — корень лага после
  // кода: innerHTML всей ленты на каждом тике парсил сотни килобайт заново
  const rt = extractFunction(js, 'renderTyped');
  assert(/el\('div', 'md-frozen'\)/.test(rt) && /el\('div', 'md-tail'\)/.test(rt) &&
    /if \(ui\._frozenSrc !== frozenKey\) \{/.test(rt) &&
    /frozenEl\.innerHTML = sg\.html;/.test(rt) &&
    /tailEl\.innerHTML = MD\.render\(stripSteps\(text\.slice\(src\.length\)\)\);/.test(rt) &&
    !/ui\.mdEl\.innerHTML = html \+ MD\.render/.test(js) &&
    /refoldCodeBlocks\(ui, active, null, ''\);/.test(rt) &&
    /refoldCodeBlocks\(ui, sg\.frozenEl, null, ''\);/.test(rt),
    'renderTyped splits the FROZEN HEAD from the live TAIL: the head (with its code slots) is parsed ONCE, only the tail repaints — no lag after big code');
  assert(/i < ui\._tailKeys\.length/.test(extractFunction(js, 'refoldCodeBlocks')) &&
    /ui\._tailKeys\[i\] = key;/.test(extractFunction(js, 'refoldCodeBlocks')) &&
    /_codePeek\.has\(key\.split\('\\u0001'\)\[0\]\)/.test(extractFunction(js, 'refoldCodeBlocks')),
    'tail slots are placed by POSITION key — big code text is not re-read on every typer tick');
  const fold = extractFunction(js, 'foldCodeBlocks');
  assert(/classList\.remove\('live-code'\)/.test(fold) &&
    /getBoundingClientRect\(\)\.height/.test(fold),
    'folding measures the visible height before removing the live-code cap');
  assert(/\.md pre\.live-code\s*\{[^}]*max-height:min\(30vh,260px\)[^}]*overflow:auto/s.test(css),
    'typing code remains in a bounded, COMPACT internally scrolling viewport');
}


async function testAutoPollingReconciliationAndBulkControls() {
  // A slower poll must never replace a newer task snapshot.
  const pending = [];
  const state = { taskLoadRun: 0, tasks: [], autoPaused: false };
  let renders = 0;
  const loadCtx = loadFunctions(['loadTasks'], {
    S: state,
    api() { return new Promise((resolve) => pending.push(resolve)); },
    renderTasks() { renders += 1; },
  });
  const oldPoll = loadCtx.loadTasks();
  const newPoll = loadCtx.loadTasks();
  pending[1]({ tasks: [{ id: 'new' }], paused: true });
  await newPoll;
  pending[0]({ tasks: [{ id: 'stale' }], paused: false });
  await oldPoll;
  assert.deepStrictEqual(state.tasks.map((task) => task.id), ['new']);
  assert.strictEqual(state.autoPaused, true);
  assert.strictEqual(renders, 1, 'a stale poll response is discarded without a DOM pass');

  // Identical polling data keeps the exact card node and performs no repaint.
  const grid = new MiniNode('div');
  const autoNav = new MiniNode('div');
  const pause = new MiniNode('button');
  const clear = new MiniNode('button');
  const stats = new MiniNode('div');
  const autoBar = new MiniNode('div');
  const task = {
    id: 'stable', title: 'Task', prompt: 'Work', status: 'running', progress: 0.4,
    result: '', schedule: '', next_run: 0, updated_at: 1, resume_status: '', events: [],
  };
  let paints = 0;
  const reconcileState = { tasks: [task], autoPaused: false };
  const reconcileCtx = loadFunctions(['taskFingerprint', 'renderTasks'], {
    S: reconcileState, Map, JSON, String,
    ICO: {
      pause: '<svg><path d="M8 5v14M16 5v14"/></svg>',
      play: '<svg><path d="M8 5.5l10 6.5-10 6.5z"/></svg>',
    },
    $$(selector, node) { return node.querySelectorAll(selector); },
    $(selector) {
      if (selector === '#taskGrid') return grid;
      if (selector === '.nav-item[data-view="auto"]') return autoNav;
      if (selector === '#autoPauseBtn') return pause;
      if (selector === '#clearDoneBtn') return clear;
      if (selector === '#autoStats') return stats;
      if (selector === '#autoBar') return autoBar;
      return null;
    },
    el: miniEl,
    paintTaskCard(card, current) {
      paints += 1;
      card.className = 'task-card ' + current.status;
      card.dataset.taskId = String(current.id);
      card.dataset.fingerprint = reconcileCtx.taskFingerprint(current);
    },
  });
  const originalCard = new MiniNode('article');
  originalCard.className = 'task-card running';
  originalCard.dataset.taskId = 'stable';
  originalCard.dataset.fingerprint = reconcileCtx.taskFingerprint(task);
  grid.appendChild(originalCard);
  reconcileCtx.renderTasks();
  assert.strictEqual(grid.firstElementChild, originalCard);
  assert.strictEqual(paints, 0, 'unchanged task polling must produce zero card repaint');
  assert(/<i>всего<\/i><b>1<\/b>/.test(stats.innerHTML) &&
    /<i>актуальных<\/i><b>1<\/b>/.test(stats.innerHTML) &&
    /<i>в работе<\/i><b>1<\/b>/.test(stats.innerHTML),
  'AUTO summary reports total, current and running task counts');
  assert(/<svg[\s\S]*M8 5v14M16 5v14/.test(pause.innerHTML),
    'global pause control uses a real stroke SVG, not typographic bars');
  assert(autoBar.classList.contains('running'));

  reconcileState.tasks = [{ ...task, progress: 0.8, updated_at: 2 }];
  reconcileCtx.renderTasks();
  assert.strictEqual(grid.firstElementChild, originalCard,
    'a changed task is patched in its keyed card instead of replacing the node');
  assert.strictEqual(paints, 1);

  const autoBarAt = html.indexOf('class="auto-bar"');
  const pauseAt = html.indexOf('id="autoPauseBtn"', autoBarAt);
  const clearAt = html.indexOf('id="clearDoneBtn"', autoBarAt);
  assert(autoBarAt >= 0 && pauseAt > autoBarAt && clearAt > pauseAt &&
    /id="autoStats"/.test(html),
    'AUTO mirrors the Files summary bar with pause left of clear');
  assert(/id="clearDoneBtn"[^>]*>[\s\n]*Очистить<\/button>/.test(html) &&
    !/Очистить выполненные/.test(html) &&
    /pause\.innerHTML = S\.autoPaused \? ICO\.play : ICO\.pause/.test(js) &&
    !/>Ⅱ<|>▶</.test(html),
    'bulk controls use real SVG icons and the requested compact clear label');
  assert(/\/api\/tasks\/clear-completed/.test(js) &&
    /S\.autoPaused \? '\/api\/tasks\/resume-all' : '\/api\/tasks\/pause-all'/.test(js));
  assert(/\.task-card\.paused::before\s*\{[^}]*rgba\(154,202,219,\.42\)/s.test(css),
    'paused cards retain a distinct calm state');
}

function testThinkingGradientContract() {
  // Дизайн обновлён по просьбе: ход мыслей — тоже процесс, у него тот же
  // мягкий перелив по контуру. Запрещено лишь второе яркое усиление.
  assert(!/\.think-card\.live|\.panel-card\.live/.test(css + js),
    'thinking must not grow a loud second gradient class');
  assert(/\.think-card::after\s*\{[^}]*toolSweep 4\.4s/s.test(css),
    'thinking card carries the same sweep-stripe as tools, much calmer');

  // Инструмент работает: ОДНА вертикальная полоса света — слой ПОВЕРХ всего
  // содержимого карточки, поэтому заголовок/аргументы/тело подсвечиваются
  // ровно и в одной фазе. Рамка — та же полоса; фон дышит медленнее (3с).
  const toolFrame = css.match(/\.tool-card\.tool-wait::after\s*\{([^}]*)\}/s);
  assert(toolFrame && /background-size:250% 100%/.test(toolFrame[1]) &&
    /toolSweep 2\.2s/.test(toolFrame[1]) &&
    /rgba\(45,212,228,0\) 33%/.test(toolFrame[1]),
  'a saturated color stripe sweeps the contour (2.2s)');
  // БУКВЫ несут насыщенный цвет: градиент клипается по тексту всех блоков
  assert(/\.tool-card\.tool-wait \.card-head \.k,\s*\n?\s*\.tool-card\.tool-wait \.card-head \.t,\s*\n?\s*\.tool-card\.tool-wait \.kv,\s*\n?\s*\.tool-card\.tool-wait \.card-inner\s*\{/.test(css) &&
    /rgba\(45,212,228,1\) 47%,rgba\(64,196,255,1\) 50%,rgba\(158,124,255,1\) 53%/.test(css) &&
    /-webkit-text-fill-color:transparent/.test(css),
  'saturated color lives ON the letters of every text block');
  // ВЛОЖЕННЫЙ КОД ЗАЩИЩЁН: pre/code/ссылки не наследуют прозрачную заливку —
  // именно это раньше делало открытый код тёмным
  assert(/\.tool-card\.tool-wait \.card-inner pre,\s*\n?\s*\.tool-card\.tool-wait \.card-inner code/.test(css) &&
    /-webkit-text-fill-color:initial/.test(css) && /color:#a9e6ff/.test(css),
  'nested code blocks keep solid color (no dark/invisible code)');
  // ФОН: еле видно, обесцвеченно, дышит медленнее букв
  const toolBg = css.match(/\.tool-card\.tool-wait::before\s*\{([^}]*)\}/s);
  assert(toolBg && /opacity:\.09/.test(toolBg[1]) &&
    /rgba\(126,158,172,\.72\)/.test(toolBg[1]) &&
    /toolSweep 3\.6s linear infinite/.test(toolBg[1]) &&
    !/rgba\(45,212,228/.test(toolBg[1]),
  'the background wash is subtle-but-visible and desaturated, slower than letters');
  // рамка заметна: полоса полной непрозрачности на пике + базовый цвет ярче
  assert(/rgba\(45,212,228,0\) 33%/.test(css) && /rgba\(45,212,228,0\) 67%/.test(css) &&
    /\.tool-card\.tool-wait\{border-color:rgba\(120,210,225,\.42\)\}/.test(css),
  'the contour stripe is wider and fully opaque at its peak');
  const glowKf2 = css.match(/@keyframes toolSweepGlow\s*\{([^@]*)\}/s);
  assert(glowKf2 && /inset 0 0 26px rgba\(64,196,255,\.24\)/.test(glowKf2[1]) &&
    /inset 0 0 52px rgba\(158,124,255,\.13\)/.test(glowKf2[1]),
  'a bit more colored inner glow in phase with the stripe');
  // фон дышит через opacity-слой ::before; отдельные keyframes дыхания
  // больше не нужны — фон и так в 6 раз медленнее букв (3.6с против 2.2с)
  // СВЕЧЕНИЕ — ТОЛЬКО ВНУТРИ карточки: inset-подсветка в фазе полосы,
  // наружного ореола нет
  const glowKf = css.match(/@keyframes toolSweepGlow\s*\{([^@]*)\}/s);
  assert(glowKf && /toolSweepGlow 2\.2s/.test(css) &&
    /inset 0 0 26px rgba\(64,196,255,\.24\)/.test(glowKf[1]),
  'an inner glow pulses in phase with the stripe');
  // убираем все inset-сегменты: после этого ЦВЕТНЫХ теней остаться не должно —
  // значит, наружного ореола нет вообще
  const outerShadows = glowKf[1].replace(/inset [^,}]*/g, '');
  assert(!/rgba\((?!0,0,0)[0-9]+,[0-9]+,[0-9]+/.test(outerShadows),
  'the glow lives INSIDE the card only (inset), no outer halo');
  // AUTO-вкладка: ореол — box-shadow на самой вкладке (drop-shadow на
  // замаскированном кольце маска срезала — свечения не было видно)
  assert(/@keyframes autoTabHalo/.test(css) &&
    /0 0 13px rgba\(151,132,255,\.48\)/.test(css),
  'AUTO tab glows via an unmasked box-shadow halo');
  assert(!/toolWaitBurst|toolWaitFlow/.test(css),
  'old full-contour gradient is gone');
  // ИНСТРУМЕНТЫ — ЕДИНАЯ СЕРАЯ КУХНЯ (qt): имя, полоса вниз, поток строк;
  // карточек-инструментов с контуром больше нет ни в обычном, ни в AGENT
  const qtOpenFn = extractFunction(js, 'qtOpen');
  assert(/const st = ensureStatus\(ui\);/.test(qtOpenFn) &&
    /qtFeed\(flow/.test(qtOpenFn) && /qt-flow/.test(qtOpenFn),
    'tools open as one gray kitchen: name, rail down, flowing status lines');
  // Дизайн по просьбе пользователя: градиент живёт ИМЕННО на буквах заголовка
  // (gradient text) и на контуре. Запрещён лишь второй яркий «live»-класс.
  assert(!/\.tool-card\.live/.test(css) && !/liveTextFlow/.test(css),
    'no loud second live-gradient class on top of tool-wait');
  assert(!/\.think-stream::(?:before|after)[^{]*\{[^}]*caret/s.test(css),
    'thinking stays a masked scrolling stream, not a cursor animation');

  // AUTO: свечение исходит ОТ градиента — тень-ореол окрашивается цветом
  // бегущей полосы в её фазе (autoOutlineFlow), отдельного box-shadow нет
  const autoOutline = css.match(/\.nav-item\[data-view="auto"\]\.auto-running \.nav-outline\s*\{([^}]*)\}/s);
  assert(autoOutline && /background-size:390% 100%/.test(autoOutline[1]) &&
    /autoOutlineFlow 3\.2s linear infinite/.test(autoOutline[1]) &&
    /rgba\(151,132,255,\.70\)/.test(autoOutline[1]),
    'running AUTO keeps its border-only gradient, brighter than before');
  const flowAt = css.indexOf('@keyframes autoOutlineFlow');
  const flowBlock = css.slice(flowAt, css.indexOf('@keyframes', flowAt + 10));
  assert(flowAt >= 0 && /background-position/.test(flowBlock) &&
    /drop-shadow\(0 0 1[01]px/.test(flowBlock) && /rgba\(151,132,255/.test(flowBlock),
    'the outline glow is coloured by the stripe phase and clearly visible');
  // сама область вкладки чуть подсвечена изнутри
  const autoTabArea = css.match(/\.nav-item\[data-view="auto"\]\.auto-running\s*\{([^}]*)\}/s);
  assert(autoTabArea && /inset 0 0 16px/.test(autoTabArea[1]) &&
    /linear-gradient\(90deg,rgba\(47,156,146,\.13\)/.test(autoTabArea[1]),
  'the AUTO tab area itself is faintly lit from inside');
  assert(!/autoTabGlow/.test(css),
    'no independent tab glow: light comes from the moving gradient');
  assert(/@keyframes autoIcoGlow/.test(css),
    'the AUTO icon glow follows the same stripe phase');
  const memoryOutline = css.match(/\.nav-item\.save-glint-strong \.nav-outline\s*\{([^}]*)\}/s);
  assert(memoryOutline && /#b477ff/.test(memoryOutline[1]) && /#ef79cf/.test(memoryOutline[1]) &&
    /memorySaveOutline 1\.35s/.test(memoryOutline[1]),
    'Memory save gets one conspicuous Apple-Intelligence-style border pass');

  const agentSwitch = css.match(/\.agent-switch\s*\{([^}]*)\}/s);
  const agentTrack = css.match(/\.agent-switch-track\s*\{([^}]*)\}/s);
  assert(agentSwitch && /border:0/.test(agentSwitch[1]) && /background:transparent/.test(agentSwitch[1]),
    'AGENT remains a real switch without an outer capsule');
  assert(agentTrack && /width:42px/.test(agentTrack[1]) && /height:28px/.test(agentTrack[1]) &&
    /border:1px solid var\(--line\)/.test(agentTrack[1]) && /background:transparent/.test(agentTrack[1]),
    'the off AGENT track matches the neutral neighbouring controls at 28px high');
  // включённый AGENT — БОРДО и горит ЯРЧЕ с пульсацией («я активен!»)
  assert(/\.agent-switch-track:has\(input:checked\)\s*\{[^}]*rgba\(74,13,24,\.5\)/s.test(css) &&
    /\.agent-switch-track input:checked \+ i\{[^}]*background:linear-gradient\(180deg,#d4526c,#a83248\)/s.test(css) &&
    /\.agent-switch-track input:checked \+ i\{[^}]*color:#25070d/s.test(css) &&
    /\.agent-switch-track i\{[^}]*background:var\(--tx3\)/s.test(css) &&
    !/\.agent-switch-track i\{[^}]*#2fa8d8/s.test(css) &&
    /\.agent-switch-track:has\(input:checked\)\{\s*animation:agGlow 2\.2s ease-in-out infinite\}/.test(css) &&
    /@keyframes agGlow\{\s*0%,100%\{box-shadow:0 0 10px 1px rgba\(255,110,135,\.16\),0 0 22px 4px rgba\(170,40,60,\.09\)\}\s*50%\{box-shadow:0 0 20px 4px rgba\(255,120,140,\.34\),0 0 42px 10px rgba\(178,48,68,\.2\)\}\}/.test(css),
    'the WHOLE track pulses: colored glow spreads beyond the toggle borders, letter A stays crisp bordo');
}

function testBudgetScenariosDraftsAndTailRaceContracts() {
  // ₽-лимит: кнопка слева от AGENT, активная — жёлтая; панель ожидания —
  // в каноне ask-card (те же кнопки-варианты), с жёлтой полосой на контуре
  assert(/id="tgBudget"/.test(html) && /\.budget-btn\.on\{[^}]*#f0be46/s.test(css) &&
    /case 'budget_wait'/.test(js) && /budget-card/.test(js) &&
    /budget_rub: S\.budgetRub \|\| 0/.test(js) &&
    /\.budget-card\s*\{[^}]*rgba\(240,190,70/s.test(css) &&
    /\.budget-card \.ask-opt\.good/.test(css),
  'the ruble limit ships end-to-end: button, yellow state, ask-card panel, body field');
  // ₽-панель = стиль панели уведомлений: плотный фон, шапка-полоса,
  // «Снять»; варианты — ИСТОРИЯ сумм пользователя (последние 4 разных)
  assert(/\.budget-pop\{[^}]*background:#0a1420/s.test(css) &&
    /\.budget-pop\{[^}]*border:1px solid var\(--line2\)/s.test(css) &&
    /id="budgetOff"/.test(html) && /id="budgetVariants"/.test(html) &&
    /function budgetHistory/.test(js) && /function budgetSuggestions/.test(js) &&
    /function rememberBudget/.test(js) && /jarvis\.budgetHistory/.test(js) &&
    /budgetSuggestions\(\)\.forEach/.test(js),
  'budget popup uses the notifications-panel style and the users own amounts');
  // ПАНЕЛЬ ЗАКРЫВАЕТСЯ: display:flex обязан уступать атрибуту hidden —
  // без этого правила лимит «висел всегда» и не снимался
  // наведение на ВЫКЛЮЧЕННЫЙ прибор: СПОКОЙНОЕ свечение из центра,
  // разгорается ОДИН РАЗ (1.3с, без пульсации) + тусклый цвет кнопки.
  // Включённый — тих. Тумблер AGENT здесь нейтрален: красный только на
  // наведении и во включённом состоянии.
  assert(/#tgCamera\{--sp:/.test(css) && /#tgComputer\{--sp:178,168,246\}/.test(css) &&
    /#tgBudget\{--sp:240,190,70\}/.test(css),
    'camera stays teal, computer is violet, budget gold');
  assert(!/qtGlowIn/.test(css) &&
    !/\.toggle:not\(\.on\):hover::before/.test(css) &&
    /\.toggle:not\(\.on\):hover,\.budget-btn:not\(\.on\):hover,\.comp-btn:not\(\.on\):hover\{[^}]*box-shadow:0 0 10px 1px rgba\(var\(--sp\),\.15\),0 0 26px 6px rgba\(var\(--sp\),\.07\)/s.test(css) &&
    /\.toggle:not\(\.on\):hover,\.budget-btn:not\(\.on\):hover,\.comp-btn:not\(\.on\):hover\{[^}]*background:rgba\(var\(--sp\),\.1\)/s.test(css) &&
    /text-shadow:0 0 9px rgba\(var\(--sp\),\.35\)/.test(css) &&
    /\.comp-btn\{--sp:130,190,215\}/.test(css) &&
    !/@keyframes ibBreath/.test(css),
    'the glow is pure BLURRED-SHADOW LIGHT (no disc, no rim): swells instantly on hover, the button fills with juice');
  assert(!/\.toggle:not\(\.on\):hover\{transform:rotate/.test(css) &&
    !/\.budget-btn:not\(\.on\):hover\{transform:rotate/.test(css) &&
    !/\[data-tip\]::after\{transform:translateX\(-50%\) rotate\(/.test(css),
    'no tilt anywhere and tooltips need no rotation compensation');
  assert(/\.toggle:not\(\.on\):hover,\.budget-btn:not\(\.on\):hover,\.comp-btn:not\(\.on\):hover\{[^}]*rgba\(var\(--sp\),\.42\)/s.test(css) &&
    /\.toggle:not\(\.on\):hover \.tg-dot\{background:rgba\(var\(--sp\),\.7\)/.test(css),
    'the button, label and contour glow a bit DIMMER in their own color');
  // тумблер AGENT — РЕЗИНКА: круглёшек тянется вправо и РЫВКОМ (плавно)
  // возвращается, как под действием резинки; насыщенный огонёк уходит
  // за правый край (остаётся свечение справа), по контуру бежит ЯВНАЯ
  // искра насыщенного красного, в конце замедляется и гаснет.
  assert(/animation:agKnobRubber 1s cubic-bezier\(\.3,\.7,\.3,1\) both/.test(css) &&
    /15%\{background:#a03c50;color:#1d060b\}/.test(css) &&
    /@keyframes agKnobRubber\{[\s\S]*?44%\{transform:translateX\(6px\);background:#c4475e;color:#1d060b\}/.test(css) &&
    /52%\{transform:translateX\(6px\)/.test(css) &&
    /68%\{transform:translateX\(-1\.8px\)/.test(css) &&
    /82%\{transform:translateX\(\.7px\)/.test(css) &&
    /@keyframes agKnobRubber\{[\s\S]*?100%\{transform:translateX\(0\);background:#8d4d5e;color:#1d060b\}\}/.test(css) &&
    !/agKnobRubber\{[\s\S]*?100%\{transform:translateX\(0\);background:var\(--tx3\)\}\}/.test(css),
    'the knob is ITS OLD GREY self, painting DARK bordo on the way right and unpainting on the way back, synced to the motion');
  assert(/animation:agEmberRun \.9s cubic-bezier\(\.3,\.5,\.35,1\) both/.test(css) &&
    /rgba\(255,150,168,\.7\),rgba\(255,86,112,\.38\) 48%/.test(css) &&
    /@keyframes agEmberRun\{[\s\S]*?40%\{transform:translateX\(14px\)\}[\s\S]*?70%\{transform:translateX\(21px\)\}[\s\S]*?100%\{opacity:\.85;transform:translateX\(26px\)\}\}/.test(css) &&
    /filter:blur\(7px\)/.test(css) &&
    /width:26px;height:26px/.test(css) &&
    /\.agent-switch-track\{overflow:hidden\}/.test(css),
    'the ember is DIMMED to the background: bigger, blurrier, freezes at the right edge INSIDE the track');
  assert(/@keyframes agRestGlow\{to\{box-shadow:inset 0 1px 5px rgba\(0,0,0,\.42\),[\s\S]*?inset 14px 0 26px -8px rgba\(255,86,112,\.4\)\}\}/.test(css) &&
    /animation:agRestGlow \.5s ease \.6s both/.test(css),
    'the ember glow is GENEROUS and stays outside at the right edge');
  assert(/animation:agSparkRun 1\.1s linear both/.test(css) &&
    /rgba\(255,84,112,\.55\) 50%/.test(css) &&
    /background-repeat:no-repeat/.test(css) &&
    /@keyframes agSparkRun\{[\s\S]*?0%\{background-position:135% 0;opacity:0\}[\s\S]*?100%\{background-position:-11% 0;opacity:0\}\}/.test(css) &&
    /92%\{background-position:-8% 0;opacity:1\}/.test(css) &&
    /40%\{background-position:40% 0\}/.test(css),
    'ONE spark pass, brisk then slowing, fading exactly at the END of its path');
  // по умолчанию тумблер НЕЙТРАЛЕН: базовая рамка var(--line), никаких
  // красных приманок до наведения
  assert(!/\.agent-switch\{--sp:255,107,122\}/.test(css) &&
    !/\.agent-switch-track\{border-color:rgba\(255,107,122,\.26\)/.test(css) &&
    !/\.agent-switch-track i\{color:#2a1216;background:#a4898f\}/.test(css),
    'the OFF agent switch is neutral: red appears only on hover or when ON');
  assert(/\.agent-switch-track:has\(input:checked\)\{\s*animation:agGlow 2\.2s ease-in-out infinite\}/.test(css) &&
    /@keyframes agGlow\{\s*0%,100%\{box-shadow:0 0 10px 1px rgba\(255,110,135,\.16\),0 0 22px 4px rgba\(170,40,60,\.09\)\}\s*50%\{box-shadow:0 0 20px 4px rgba\(255,120,140,\.34\),0 0 42px 10px rgba\(178,48,68,\.2\)\}\}/.test(css),
    'the WHOLE track pulses with a colored glow that spreads beyond its borders');
  assert(/\.budget-pop\[hidden\]\{display:none\}/.test(css),
  'the budget popup actually closes ([hidden] beats display:flex)');
  // закрытие чуть быстрее открытия; панель чуть жёлтая; стрелки свои
  assert(/\.budget-pop\.bp-closing\{animation:npOutUp \.15s/.test(css) &&
    /}, 150\);/.test(extractFunction(js, 'hideBudgetPop')),
    'closing runs faster than opening (.15s CSS, 150ms JS)');
  assert(/background:#0a1420;border:1px solid var\(--line2\)/.test(css) &&
    /animation:npInUp \.34s/.test(css) &&
    /\.bp-step\{[^}]*rgba\(240,190,70/s.test(css),
    'the budget panel is CLASSIC like notifications: neutral shell, gold only in accents, calmer .34s opening');
  assert(/\.bp-step\{/.test(css) && /id="bpMinus"/.test(html) && /id="bpPlus"/.test(html) &&
    /type="text" inputmode="numeric"/.test(html) &&
    !/budgetInput" type="number/.test(html),
    'Jarvis-style steppers replace the native number spinners');
  // звук лимита — тот же тон, что у агента: включение и ручное снятие
  const setBudgetFn = extractFunction(js, 'setBudget');
  assert(/beep\(value \? 760 : 420, 0\.1\)/.test(setBudgetFn),
  'the ruble coin beeps like the agent switch (on=760, off=420)');
  // кнопка ₽ имеет подпись при наведении — как остальные кнопки поля ввода
  assert(/\.budget-btn\[data-tip\]:is\(:hover,:focus-visible\)::after/.test(css),
  'the ruble button carries the same hover tooltip as the other toggles');
  // решение по лимиту сворачивается в миниатюру чуть крупнее инструментов
  assert(/cls: 'th-budget'/.test(js) && /\.thumb\.th-budget\{[^}]*rgba\(240,190,70/s.test(css),
  'a picked budget decision collapses into a slightly larger golden thumb');
  // сценарии: вкладка, сетка, запуск шагами, подсказка на третий повтор
  assert(/data-view="scenarios"/.test(html) && /loadScenarios\(\)/.test(js) &&
    /function runScenario/.test(js) && /api\/scenarios\/new/.test(js) &&
    /maybeOfferScenario/.test(js),
  'scenarios tab ships with step runner, storage API and repeat suggestion');
  // запуск сценария обязан работать: showView (switchView не существует),
  // шаг ждёт конца печати, кнопки «Пример» больше нет
  assert(/showView\('chat'\)/.test(extractFunction(js, 'runScenario')) &&
    !/switchView/.test(js) && !/scDemoBtn/.test(js) && !/scDemoBtn/.test(html),
  'runScenario navigates with the real showView and the demo button is gone');
  const step = extractFunction(js, 'sendScenarioStep');
  assert(!/wasStreaming/.test(step) && /await\s*$/.test(step) ||
    (!/wasStreaming/.test(step) && /send\(\{ text, scenario: true, scenarioStep: step, scenarioTotal: total \}\)/.test(step)),
    'scenario steps await the real send and never reference wasStreaming');
  // сценарий — монолог Джарвиса: сообщения пользователя не рисуются,
  // между ответами — тихая строка этапа
  assert(/opts\.scenario && opts\.scenarioStep/.test(js) && !/addUserMsg\(text, atts, null, requestHost\);[\s\S]{0,40}scenario/.test('') ||
    /sc-stage/.test(js) && /sc-stage\{/.test(css) && /scenarioStep: step/.test(js),
    'scenario steps draw a quiet stage divider instead of a user message');
  assert(!/toast\('Шаг '/.test(extractFunction(js, 'runScenario')),
    'the stage divider replaces the per-step toast');
  // карточка открывается целиком + редактирование на месте
  assert(/function openScenario/.test(js) && /function editScenario/.test(js) &&
    /api\/scenarios\/update/.test(js) && /sc-steps-full/.test(css) &&
    /Редактировать/.test(js),
  'scenario card opens full and edits in place via /api/scenarios/update');
  // предложение сценария: мягкий фон + окно Джарвиса с градиентной рамкой
  assert(/\.modal-back\.soft\{background:rgba\(2,5,10,\.42\)/.test(css) &&
    /\.modal\.jarvis-win/.test(css) && /jw-ico/.test(css) &&
    /\{ soft: true \}/.test(js) && /jw-ico/.test(js),
  'the scenario offer uses a soft backdrop and a Jarvis-styled window');
  // панель лимита летит движением панели уведомлений (npIn/npOut, зеркально)
  assert(/animation:npInUp \.34s cubic-bezier\(\.2,\.9,\.3,1\) both/.test(css) &&
    /\.budget-pop\.bp-closing\{animation:npOutUp \.15s/.test(css) &&
    /@keyframes npInUp\{from\{opacity:0;transform:translateY\(10px\) scale\(\.97\)\}\}/.test(css) &&
    !/budgetPopIn/.test(css) &&
    /function hideBudgetPop/.test(js) && /function openBudgetPop/.test(js),
    'the budget panel flies with the exact note-panel motion, spring bounce removed');
  // черновик не теряется: сохранение на input, восстановление на старте
  assert(/function saveDraft/.test(js) && /function loadDraft/.test(js) &&
    /jarvis\.draft/.test(js),
  'unsent input survives reload via localStorage draft');
  // ГОНКА ПЛАНА: хвост канонического done допечатывается, а не обрывается
  const finish = extractFunction(js, 'queueResponseFinish');
  assert(/doneContent\.startsWith\(ui\.buffer\)/.test(finish) &&
    /ui\.buffer = doneContent/.test(finish),
  'a longer canonical done extends the buffer instead of finishing early');
}

function testQuietToolsBoostAskStylesAndAgentTheme() {
  // КУХНЯ ИНСТРУМЕНТОВ — СЕРАЯ И ЖИВАЯ: имя, от него вниз полоса, справа —
  // поток строк (плывут вверх, маски сверху/снизу). Один цвет на всё —
  // чуть ярче подписи «обычный запрос»; других оттенков нет.
  assert(/function qtOpen/.test(js) && /function qtFeed/.test(js) &&
    /function qtFold/.test(js) && /function qtFolder\b/.test(js) &&
    /function qtToggleFolder/.test(js) && /function qtToggleDetail/.test(js) &&
    /function flushTools/.test(js) && /QT_FAMILY/.test(js) &&
    /QT_TOOL_ICO/.test(js) && /function qtDetail/.test(js) &&
    /function qtSweep/.test(js) && /function qtMiniaturize/.test(js) &&
    /function qtResultLine/.test(js) && /function ensureStatus/.test(js),
    'one gray kitchen: open/feed/fold/folders/details + sweep/miniature/result-line/ensureStatus');
  assert(/\.qt-node,\.qt-folder\{/.test(css) && /\.qt-head\{/.test(css) &&
    /\.qt-rail\{/.test(css) && /\.qt-flow\{/.test(css) &&
    /\.qt-flowline\{/.test(css) && /@keyframes qtRise/.test(css),
    'a tool is a name with a thin rail down and a flowing masked area to the right');
  assert(/\.qt-flow\{[^}]*max-height:88px/s.test(css) &&
    /\.qt-flowin\{[^}]*will-change:transform/s.test(css) &&
    /\.qt-flow\.full\{[\s\S]*?mask-image:linear-gradient\(180deg,transparent,#000 22%,#000 82%,transparent\)/s.test(css) &&
    /function glideFlow/.test(js) &&
    /inner\.scrollHeight > 88 \+ 4/.test(extractFunction(js, 'qtFeed')) &&
    !/flow\.clientHeight \+ 4/.test(extractFunction(js, 'qtFeed')) &&
    js.includes('<div class="qt-flowin"></div>') &&
    /transition:height \.32s/.test(css) &&
    /flow\._h = h;/.test(extractFunction(js, 'qtFeed')) &&
    /flow\.style\.height = h \+ 'px';/.test(extractFunction(js, 'qtFeed')) &&
    /if \(flow\._ui\) scrollSoon\(flow\._ui\)/.test(extractFunction(js, 'qtFeed')) &&
    /flow\._ui = ui;/.test(extractFunction(js, 'qtOpen')),
    'the flow grows SMOOTHLY (height transition), fills line by line, glides inside — and the page STICKS to the growing tool');
  assert(/rgba\(154,202,219,\.72\)/.test(css) &&
    !/rgba\(128,156,142\)/.test(css),
    'ALL kitchen colors are the plain-request gray, a bit brighter; no other tints');
  assert(/\.qt-mark\{[^}]*margin-left:4px/s.test(css),
    'checkmark and time sit right next to the tool name, not at the far edge');
  // отработал — галочка у имени, строка результата в поток, СВЁРТКА В
  // МИНИАТЮРУ; в папку семейство уезжает ТОЛЬКО когда череда закончилась
  // (другой тип инструмента или текст ответа)
  assert(/setTimeout\(\(\) => qtMiniaturize\(node\), pour \+ 1000\);/.test(js) &&
    /lines\.slice\(1, 7\)/.test(js) &&
    /return 120 \+ extra\.length \* 170;/.test(extractFunction(js, 'qtResultLine')),
    'the RESULT MASS pours into the flow right away (up to 6 real lines); the miniature waits out the mass + ~1s');
  assert(/function qtSweep\(ui, keepGroup\)/.test(js) &&
    /qtSweep\(ui, ev\.group\);/.test(js) &&
    /if \(!ui\.agentMode && !ui\._qtSwept\)/.test(js) &&
    /qtResultLine\(node, ev\);/.test(js),
    'a finished tool becomes a miniature and shows its result line; the family folder closes the WHOLE streak at once');
  // КОНЕЦ ОТВЕТА — ТОТ ЖЕ ВАЛЬС: инструменты улетают под группу ОДИН ЗА
  // ДРУГИМ (150мс), каждый со своей полосой одновременно (внутри qtFold)
  const fq = extractFunction(js, 'flushQt');
  assert(/setTimeout\(\(\) => qtFold\(ui, n, idx === pending\.length - 1\), idx \* 170\);/.test(fq) &&
    /const pending = \[\];/.test(fq),
    'answer end folds tools ONE BY ONE into the group (170ms), strip compressing in the same beat');

  const rf = extractFunction(js, 'qtFold');
  assert(/'height \.42s cubic-bezier\(\.4,\.6,\.3,1\) \.58s'/.test(rf) &&
    !/position = 'fixed'/.test(rf) && !/ghost/.test(rf) &&
    /requestAnimationFrame\(homing\)/.test(rf) &&
    /const flight = node\.animate\(/.test(rf) &&
    /flight\.effect\.setKeyframes\(flyKeys\(aim\)\)/.test(rf) &&
    /const labC = \(lRect0\.top \+ lRect0\.height \/ 2\) - base0;/.test(rf) &&
    /let aim = titleC\(\) - labC - base0;/.test(rf) &&
    /const need = titleC\(\) - labC - baseTop;/.test(rf) &&
    !/scale\(\.93\)/.test(rf) &&
    /getComputedTiming\(\)\.progress/.test(rf) &&
    /easing: 'cubic-bezier\(\.2,\.5,\.2,1\)', fill: 'forwards'/.test(rf) &&
    /const flyKeys = \(target\) => \[/.test(rf) &&
    /function qtFold\(ui, node, isLast\)/.test(js) &&
    /if \(isLast\) setTimeout\(folderBlink, 820\);/.test(rf) &&
    /filter: 'blur\(4px\) brightness\(\.08\)'/.test(rf) &&
    /folder\._pend = \(folder\._pend \|\| 0\) \+ 1;/.test(extractFunction(js, 'qtFold')) &&
    /folder\.classList\.add\('blink'\)/.test(extractFunction(js, 'qtFold')) &&
    /\.qt-folder\.blink \.qt-name\{animation:qtBlink \.5s ease-out both\}/.test(css) &&
    /0%\{filter:brightness\(1\.15\)\}/.test(css) &&
    /30%\{filter:brightness\(1\.8\)\}/.test(css) &&
    !/qtBlink[\s\S]*?text-shadow/.test(css.match(/@keyframes qtBlink\{[\s\S]*?\}\}/)[0]) &&
    'the fold is slow and gentle, lines DISSOLVE (blur) as they fly to the folder, which BLINKS its glow right as the streak lands');
  assert(/_qtHold = performance\.now\(\) \+ \(folded - 1\) \* 150 \+ 480/.test(js) &&
    /ui\.holdUntil = ui\._qtHold \|\| 0;/.test(js),
    'typing waits for the waltz but the pause is SHORT now');
  assert(/\.qt-detail\{[^}]*overflow-y:auto;overflow-x:hidden/s.test(css) &&
    /white-space:pre-wrap;word-break:break-word/.test(css),
    'tool text scrolls VERTICALLY ONLY and wraps — no horizontal scrolling');
  assert(/\.qt-folder \.qt-kids\{display:none\}/.test(css) &&
    /\.qt-folder\.open \.qt-kids\{display:flex/.test(css) &&
    /\.qt-kids>\.qt-rail\{/.test(css) && /\.qt-rows\{/.test(css),
    'the folder has no rail when closed; opened, a rail runs down from the icon');
  const tf = extractFunction(js, 'qtToggleFolder');
  assert(/r\.animate\(frames,/.test(tf) &&
    /kids\.style\.height = \(wasOpen \? H : 0\) \+ 'px';/.test(tf) &&
    !/filter: 'blur/.test(tf) &&
    /const wait = wasOpen \? \(rows\.length - 1 - i\) : i;/.test(tf) &&
    /duration: FLY, delay: LEAD \+ wait \* STEP,/.test(tf) &&
    /easing: 'cubic-bezier\(\.2,\.5,\.2,1\)', fill: 'both'/.test(tf) &&
    !/\.reverse\(\)/.test(tf) &&
    /const pr = a\.effect\.getComputedTiming\(\)\.progress;/.test(tf) &&
    /sum \+= pr == null \? 0 : pr;/.test(tf) &&
    /const dir = wasOpen \? -1 : 1;/.test(tf) &&
    /H \* \(dir < 0 \? 1 - avg : avg\)/.test(tf) &&
    /Promise\.all\(anims\.map\(\(a\) => a\.finished\)\)/.test(tf) &&
    !/setTimeout\(/.test(tf) &&
    tf.indexOf("f.classList.remove('open')") < tf.indexOf('a.cancel()'),
    'MANUAL open/close is ONE animation: closing plays the SAME frames inverted (bottom row leaves first), and the rail height tracks the rows AVERAGE PROGRESS every frame — perfectly in sync, never ahead');
  assert(tf.indexOf("kids.style.height = '0px'") < tf.indexOf("f.classList.remove('open')"),
    'CLOSING keeps the folder open until the reverse animation finishes — it never snaps to display:none');
  assert(/'height \.44s cubic-bezier\(\.3,\.6,\.3,1\), opacity \.3s ease'/.test(extractFunction(js, 'qtToggleDetail')) &&
    /'height \.44s cubic-bezier\(\.22,\.8,\.3,1\), opacity \.34s ease'/.test(extractFunction(js, 'qtToggleDetail')),
    'a single tool expands/collapses slower and smoother too');
  assert(js.includes("web: { label: 'Интернет', ico: 'globe' }") &&
    /web_search: 'globe', open_url: 'win', http_request: 'cloud'/.test(js),
    'the internet family unites search+open+http with meaningful icons');
  assert(/\.qt-ico svg\{width:13px;height:13px;display:block\}/.test(css),
    'tool icons are slightly smaller');
  // УСКОРЕНИЕ: после нажатия кнопка СТАНОВИТСЯ ЗОЛОТОЙ и пульсирует, как план;
  // по контуру бегут яркие золотые струйки
  assert(/id="boostBtn"/.test(html) && /Ускорить печать/.test(html) &&
    /function setBoost/.test(js) && /S\.turbo = !!on;/.test(js),
    'the boost button wakes with streaming');
  assert(/\.boost-btn\.on\{[^}]*color:#f6c95a[^}]*background:rgba\(217,164,65,\.16\)/s.test(css) &&
    /animation:boostPulse 1\.5s ease-in-out infinite/.test(css) &&
    /@keyframes boostPulse\{[\s\S]*?50%\{box-shadow:inset 0 0 24px rgba\(246,204,110,\.45\),0 0 26px rgba\(240,190,70,\.42\)\}\}/.test(css) &&
    /animation:boostRun \.42s linear infinite/.test(css) &&
    /rgba\(255,214,110,\.98\) 50%/.test(css) &&
    /background-repeat:no-repeat/.test(css) &&
    /\.boost-btn\.live:not\(\.on\):hover/.test(css) &&
    !/\.boost-btn\.live:hover\{/.test(css),
    'boost turns GOLD with a STRONGER pulse and brighter sparks; hover never un-golds it');
  // ШЕСТЬ ТИПОВ ВЫБОРА + подтверждение кнопкой (кроме чистых плиток)
  const qcard = extractFunction(js, 'questionCard');
  assert(/function cleanAskText/.test(js) &&
    js.includes('.replace(/^\\s*#{1,6}\\s*/gm') &&
    js.includes(".replace(/[:：]\\s*$/, '')") &&
    /esc\(cleanAskText\(ev\.question\)\)/.test(qcard) &&
    /\.map\(cleanAskText\)\.filter\(Boolean\)/.test(qcard),
    'ask titles lose markdown hashes, options lose trailing colons');
  assert(/ASK_STYLES = \['pills', 'stack', 'cloud', 'seg', 'grid', 'dial'\]/.test(qcard) &&
    /classList\.add\('sel'\)/.test(qcard) &&
    /go\.addEventListener\('click', \(\) => pick\(ownText\.trim\(\) \|\| chosen\)\);/.test(qcard) &&
    /\.ask-opt\.sel\{/.test(css) && /\.ask-go\{/.test(css),
    'question cards highlight the choice and send by button');
  const mount = extractFunction(js, 'mountUiPanels');
  assert(/const tilesOnly = items\.length > 0 && items\.every\(\(x\) => x\.t === 'tiles'\);/.test(mount) &&
    /if \(tilesOnly\) \{ setTimeout\(fire, 140\); return; \}/.test(mount) &&
    /if \(askable && !tilesOnly && !confirmOnly\) \{/.test(mount) &&
    !/sendTimer/.test(mount),
    'panels with ONLY colored tiles send on click — every other format waits for the button');
  ['ask-s-stack', 'ask-s-cloud', 'ask-s-seg', 'ask-s-grid', 'ask-s-dial'].forEach((cls) => {
    assert(new RegExp('\\.' + cls.replace('-', '\\-') + '\\{').test(css),
      'choice style ' + cls + ' has its own CSS');
  });
  // ТЕМА AGENT — БОРДО НА НОЧНОЙ СИНИ (человек-паук), и её ПРИВОЗИТ волна
  assert(/@property --cy\{syntax:'<color>';inherits:true;initial-value:#00c8f0\}/.test(css) &&
    /transition:--cy \.3s ease,--cy2 \.3s ease,--line \.3s ease/.test(css),
    'theme colors are registered properties and TRAVEL with transitions');
  assert(/body\.agent-on\{[\s\S]*?--cy:#9e2c42; --cy2:#cf8a9b;[\s\S]*?--line:rgba\(168,50,72,\.24\)[\s\S]*?--panel:rgba\(24,10,15,\.8\)/.test(css),
    'WINE NIGHT, DARKER WINE: a genuinely NEW world — wine-black sky, wine panels, wine lines, deep dark rose energy');
  assert(/function agentWave/.test(js) && /S\.agentWaveRect/.test(js),
    'the wave can be born from the permission-card toggle rect');
  const waveCss = css.match(/\.agent-wave\{([^}]*)\}/s)[1];
  assert(!/border:3px/.test(waveCss) && !/filter:blur/.test(waveCss) &&
    /will-change:transform,opacity;/.test(waveCss) && /backface-visibility:hidden;/.test(waveCss) &&
    /transparent 0%/.test(waveCss) && /rgba\(168,50,72,\.42\) 44%/.test(waveCss) &&
    /transparent 68%/.test(waveCss),
    'the wave is a RING: hollow center, bright front, transparent edge — nothing trails behind it');
  assert(/animation:agentWave \.72s linear both/.test(css) &&
    /@keyframes agentWave\{[\s\S]*?88%\{opacity:\.97\}[\s\S]*?100%\{transform:translate\(-50%,-50%\) scale\(3\.2\);opacity:0\}\}/.test(css) &&
    /wave\.style\.setProperty\('--aw', \(radius \* 2\.6\) \+ 'px'\)/.test(js) &&
    /function agentPaint\(/.test(js) &&
    /document\.body\.classList\.add\('agent-on'\);[\s\S]*?requestAnimationFrame\(\(\) => \{/.test(js.slice(js.indexOf('function agentPaint'), js.indexOf('function agentPaint') + 2600)) &&
    !/setTimeout\(\(\) => \{\s*\n\s*document\.body\.classList\.add\('agent-on'\);\s*\n\s*\}, 260\);/.test(js),
    'the wave runs LINEAR (no slowdown), exits FAR beyond any screen, and PAINTS the UI behind its front like a brush');
  assert(!/agentWave\(S\.agentWaveOrigin \|\| \$\('#swAgent'\), true\)/.test(js),
    'NO reverse wave on disable: plain base transitions take the theme back');
  assert(/@keyframes agentWaveOut\{[\s\S]*?100%\{opacity:0;transform:translate\(-50%,-50%\) scale\(\.03\)\}\}/.test(css),
    'on disable the color wave shrinks back INTO the toggle');
  assert(/document\.body\.classList\.add\('agent-on'\);/.test(js) &&
    /\}, 260\);/.test(js) &&
    /transition:--cy \.3s ease,--cy2 \.3s ease,--line \.3s ease/.test(css) &&
    /document\.body\.classList\.remove\('agent-on'\);/.test(js) &&
    /ag-switching/.test(js),
    'colors flip at 260ms and settle in 0.3s — the paint lands while the wave is still rolling over it');
  assert(/body\.agent-on \.bg-layer\{/.test(css) && /body\.agent-on \.grid-plane\{/.test(css) &&
    /body\.agent-on \.bubble-user\{/.test(css) && /body\.agent-on \.note-panel\{background:#1a0c12\}/.test(css) &&
    /body\.agent-on \.send-btn\{background:linear-gradient\(135deg,#a83248,#6e1c2c\)/.test(css) &&
    /body\.agent-on \.reactor \.core\{background:radial-gradient\(circle,#fff,#f0c4cf 44%,#7c1f30\)/.test(css) &&
    /body\.agent-on \.jw-ico\{/.test(css) &&
    /body\.agent-on ::-webkit-scrollbar-thumb\{background:rgba\(196,84,104,\.28\)\}/.test(css),
    'WINE NIGHT everywhere: wine sky, wine panels, wine scroll, bright-rose energy — plan stays GOLD');
  assert(/body\.agent-on \.md pre\{background:rgba\(20,9,13,\.92\)/.test(css) &&
    /body\.agent-on \.md pre code\{color:#d8dde6\}/.test(css) &&
    /body\.agent-on \.md code\{background:rgba\(20,9,13,\.85\);color:#d8dde6\}/.test(css) &&
    /body\.agent-on \.md th\{background:rgba\(74,20,34,\.7\)/.test(css) &&
    /body\.agent-on \.md tr:nth-child\(even\)\{background:rgba\(46,19,27,\.35\)\}/.test(css),
    /body\.agent-on \.att\{background:rgba\(168,50,72,\.12\)/.test(css) &&
    /body\.agent-on \.thumb\{background:rgba\(30,12,18,\.5\)\}/.test(css) &&
    /body\.agent-on \.ui-panel\{border-color:rgba\(196,84,104,\.26\)/.test(css) &&
    /body\.agent-on \.day-separator\{color:#d9b3bd/.test(css) &&
    /body\.agent-on \.chat-item:hover\{background:rgba\(168,50,72,\.14\)/.test(css) &&
    /body\.agent-on \.brand-name\{text-shadow:0 0 18px rgba\(196,84,104,\.45\)\}/.test(css) &&
    /body\.agent-on \.nav-item\.drop-hot\{background:rgba\(168,50,72,\.2\)/.test(css),
    'CODE stays NEUTRAL in agent mode (no red code lines); final sweep: attachments, thumbs, ui-panels, day separators, chat list, brand, drop glow — all wine');
  assert(/body\.agent-on \.hello span\{background:linear-gradient\(90deg,#eec2ce,#d39aa9 42%,#e8c4cf 74%,#eec2ce\);[\s\S]*?-webkit-background-clip:text;background-clip:text;color:transparent;[\s\S]*?filter:drop-shadow\(0 0 22px rgba\(196,84,104,\.28\)\)\}/.test(css),
    'the AGENT greeting paints ONLY the letters (clip:text) — no square gradient slab behind JARVIS');
  // ИСТОРИЯ ИНСТРУМЕНТОВ — ВСЕГДА ТИХАЯ КУХНЯ: restoreTrace собирает папки
  // семейств, а не агентские tool-card; текущий режим не перекрашивает прошлое
  assert(/function renderToolKitchen\(node, traces\)/.test(js) &&
    /const toolTraces = \[\];/.test(extractFunction(js, 'restoreTrace')) &&
    /renderToolKitchen\(node, toolTraces\);/.test(extractFunction(js, 'restoreTrace')) &&
    !/tool-card/.test(extractFunction(js, 'renderToolKitchen')) &&
    /qtFolderSync\(f\);/.test(extractFunction(js, 'renderToolKitchen')),
    'past tools are restored as the QUIET kitchen (family folders with rows): switching to AGENT never repaints history');
  const agBlock = css.slice(css.indexOf('body.agent-on{'));
  assert(!/168,159,242/.test(agBlock) && !/47,156,146/.test(agBlock) &&
    !/c3a9f0/.test(agBlock) && !/94,42,92/.test(agBlock) &&
    !/152,124,150/.test(agBlock) && !/c99aa3/.test(agBlock) &&
    !/e290a2/.test(agBlock) && !/2fa8d8/.test(agBlock),
    'THEME PURITY: the wine-night block has no violet, teal, pink or blue leftovers');
  assert(!/body\.agent-on \.plan-dock \{/.test(css) &&
    !/body\.agent-on \.plan-dock ./.test(css),
    'the plan dock is NEVER touched by the agent theme: gold frame, gold segments, green done — as it was');
  assert(/body\.agent-on \.nav-item:hover\{background:rgba\(168,50,72,\.14\)\}/.test(css) &&
    /body\.agent-on \.chat-item\.active\{background:rgba\(168,50,72,\.2\)/.test(css) &&
    /body\.agent-on \.ask-opt\.sel, ?body\.agent-on \.ask-opt\.sel:hover\{background:rgba\(168,50,72,\.22\)/.test(css) &&
    /body\.agent-on \.sugg:hover\{background:rgba\(168,50,72,\.14\)/.test(css) &&
    /body\.agent-on \.btn\.primary\{background:linear-gradient\(135deg,#a83248,#6e1c2c\)/.test(css) &&
    /body\.agent-on \.fcard\.sel\{border-color:#a83248/.test(css) &&
    /body\.agent-on \.toggle\.on\{background:rgba\(168,50,72,\.18\)/.test(css),
    /body\.agent-on #newChatBtn\{background:linear-gradient\(135deg,#a83248,#6e1c2c\)/.test(css) &&
    /body\.agent-on \.topbar\{background:rgba\(18,8,12,\.72\)/.test(css) &&
    /body\.agent-on \.term-head\{border-bottom:1px solid rgba\(168,50,72,\.24\)/.test(css) &&
    /body\.agent-on \.fprev-head\{background:rgba\(24,10,15,\.95\)\}/.test(css) &&
    /body\.agent-on \.sel-bar\{background:rgba\(24,10,15,\.92\)/.test(css) &&
    /body\.agent-on \.boot-core\{background:radial-gradient\(circle,#fff,#e8c4cf 40%,#6e1c2c\)\}/.test(css) &&
    /body\.agent-on \.nav-item\.active\{[\s\S]*?border-left:2px solid #a83248\}/.test(css),
    'EVERY hover/selected state is harmonized wine light — and Q passes over topbar, new-chat button, terminal, preview, selection bar, boot, tabs');
  assert(!/body\.agent-on \.boost-btn\.on\{color:#ff8f9c/.test(css),
    'boost stays GOLD inside the agent theme');
  // СЦЕНАРИЙ: «сегодня» молчит, полоса недолгая, название без рамки, стоп рвёт всё
  assert(/if \(S\.scenarioActive\) return null;/.test(extractFunction(js, 'placeDaySeparator')) &&
    /if \(S\.scenarioActive\) \{ clearTimeout\(hideTimer\); hide\(\); return; \}/.test(extractFunction(js, 'setupScrollDate')),
    'the "today" label and day separators stay silent while a scenario runs');
  assert(/\.sc-stage-text\{[^}]*font-size:13\.5px/s.test(css) &&
    !/\.sc-stage-text\{[^}]*border/s.test(css) &&
    /\.sc-stage-line\{flex:0 0 64px/.test(css),
    'stage = framed digit, MUCH longer connector, bigger frameless title');
  assert(/if \(S\.scenarioActive\) S\.abortedScenario = true;/.test(extractFunction(js, 'stopStream')) &&
    /S\.abortedScenario/.test(extractFunction(js, 'runScenario')),
    'STOP during a scenario kills the WHOLE scenario — no next step is sent');
  // стоп-КНОПКА тоже рвёт сценарий: раньше шла мимо stopStream и флаг не ставился
  assert(/\$\('#sendBtn'\)\.addEventListener[\s\S]*?if \(S\.scenarioActive\) S\.abortedScenario = true;[\s\S]*?stopRunForReal\(\);/.test(js),
    'the STOP BUTTON itself flags the whole scenario (it used to bypass stopStream)');
  // BA: перед клик-обработчиком появились hover-прогревы — срезаем от КЛИКА
  const stopBtn = js.slice(js.indexOf("$('#sendBtn').addEventListener('click'"));
  assert(/if \(S\.followUi\) flushTools\(S\.followUi\);/.test(stopBtn.slice(0, 1100)) &&
    /flushTools\(S\.followUi\);/.test(extractFunction(js, 'stopStream')) &&
    /typerStop\(S\.followUi\);/.test(stopBtn.slice(0, 1100)) &&
    /S\.followUi\.buffer = S\.followUi\.shown \|\| '';/.test(stopBtn.slice(0, 1100)) &&
    /typerStop\(S\.followUi\);/.test(extractFunction(js, 'stopStream')) &&
    /markStopped\(S\.followUi\);/.test(stopBtn.slice(0, 1100)) &&
    /markStopped\(S\.followUi\);/.test(extractFunction(js, 'stopStream')) &&
    /function markStopped\(ui\)/.test(js) && /'Остановлено\.'/.test(js),
    'STOP cuts the typer INSTANTLY (buffer = shown), shows «Остановлено.» in EVERY path and folds tools');
  // прокрутка открытой карточки сценария: без системного скроллбара и дрожания
  assert(/\.sc-steps-full\{[^}]*scrollbar-width:none/s.test(css) &&
    /\.sc-steps-full::-webkit-scrollbar\{width:0;height:0;display:none\}/.test(css) &&
    /transform:translateZ\(0\)/.test(css.match(/\.sc-steps-full\{[^}]*\}/s)[0]),
    'the scenario card scrolls without a system scrollbar or edge shimmer');
  // окно Джарвиса входит без scale — рамка не мерцает
  assert(/@keyframes jarvisWinIn\{from\{opacity:0;transform:translateY\(14px\)\}\}/.test(css),
    'the Jarvis window fades up without scaling (no border shimmer)');
  assert(/\.modal\.jarvis-win\{[^}]*overflow:hidden/s.test(css) &&
    /\.jw-pane\{[^}]*overflow-y:auto/s.test(css) &&
    /jw-pane/.test(js.match(/m\.innerHTML = \(opts && opts\.soft\)[^;]+;/)[0]),
    'soft windows scroll an INNER pane: the gradient frame never scrolls away (no harsh card edges)');
  assert(/\.sc-stage-line\{flex:0 0 64px/.test(css) &&
    /\.sc-stage-text\{[^}]*font-size:13\.5px/s.test(css),
    'the stage connector is MUCH longer and the stage title is bigger');
  // РЕЗИНКА ТУМБЛЕРА: играет РОВНО ОДИН РАЗ за наведение (JS-класс ag-play),
  // огонёк живёт ВНУТРИ трека (overflow:hidden), свечение остаётся СНАРУЖИ
  // справа, искра — один проход с замедлением в конце
  assert(/\.agent-switch-track\{overflow:hidden\}/.test(css) &&
    /\.agent-switch\.ag-play \.agent-switch-track:not\(:has\(input:checked\)\) i\{[\s\S]*?agKnobRubber 1s/.test(css) &&
    /sw\.classList\.remove\('ag-play'\);[\s\S]*?void sw\.offsetWidth;[\s\S]*?sw\.classList\.add\('ag-play'\);/.test(js) &&
    !/cooling/.test(js) &&
    !/:has\(input:checked\)\):hover i\{/.test(css),
    'the rubber RESTARTS on every mouseenter (remove + reflow + add) — quick re-hovers always play');
  const spark = css.match(/\.agent-switch\.ag-play \.agent-switch-track:not\(:has\(input:checked\)\)\:\:before\s*\{([^}]*)\}/s);
  assert(spark && /background-repeat:no-repeat/.test(spark[1]) &&
    /rgba\(255,84,112,\.55\) 50%/.test(spark[1]),
    'the contour spark is a SINGLE pass (no-repeat), bright');
  assert(/animation:agSparkRun 1\.1s linear both/.test(css) &&
    /@keyframes agSparkRun\{[\s\S]*?100%\{background-position:-11% 0;opacity:0\}\}/.test(css) &&
    /92%\{background-position:-8% 0;opacity:1\}/.test(css) &&
    /40%\{background-position:40% 0\}/.test(css),
    'ONE spark pass, brisk then slowing, fading exactly at the END of its path');
  const ember = css.match(/\.agent-switch\.ag-play \.agent-switch-track:not\(:has\(input:checked\)\)\:\:after\s*\{([^}]*)\}/s);
  assert(ember && /width:26px;height:26px/.test(ember[1]) && /blur\(7px\)/.test(ember[1]) &&
    /rgba\(255,150,168,\.7\)/.test(ember[1]) && /rgba\(255,86,112,\.38\)/.test(ember[1]),
    'the ember is big and blurry but DIMMED — pushed to the background');
  const emberKf = css.match(/@keyframes agEmberRun\s*\{([\s\S]*?)\}\}/)[1];
  assert(/40%\{transform:translateX\(14px\)\}/.test(emberKf) &&
    /70%\{transform:translateX\(21px\)\}/.test(emberKf) &&
    /100%\{opacity:\.85;transform:translateX\(26px\)/.test(emberKf),
    'the ember freezes at the right edge of the track: its glow stays slightly visible INSIDE');
  assert(/@keyframes agEmberRun\{[\s\S]*?100%\{opacity:\.85;transform:translateX\(26px\)\}\}/.test(css) &&
    /sw\.addEventListener\('mouseleave', \(\) => \{[\s\S]*?classList\.remove\('ag-play'\)/.test(js),
    'the ember STOPS at the right blind zone and STAYS alive while hovered — it never escapes or dies');
  assert(/@keyframes agRestGlow\{to\{box-shadow:inset 0 1px 5px rgba\(0,0,0,\.42\),\s*\n?\s*inset 14px 0 26px -8px rgba\(255,86,112,\.4\)\}\}/.test(css),
    'the ember glow lives ONLY INSIDE the track: nothing bleeds outside the toggle');
  // свет приборов: свечение = размытые тени, включается СРАЗУ, набухает и замирает
  assert(!/qtGlowIn/.test(css) && !/:hover::before\{content:''/.test(css) &&
    /\.toggle:not\(\.on\):hover,\.budget-btn:not\(\.on\):hover,\.comp-btn:not\(\.on\):hover\{[^}]*transition:color \.55s ease,background-color \.55s ease,border-color \.55s ease/s.test(css) &&
    !/inset 0 0 22px/.test(css),
    'buttons are BACK to the loved N glow, only dimmer at the edges — no extra brightness added');
  // РАЗВЁРТЫВАНИЕ КОДА С ПЕРВОГО РАЗА: следы анимации сворачивания стираются
  assert(/node\.style\.height = '';/.test(extractFunction(js, 'collapseToThumb')) &&
    /node\.style\.opacity = '';/.test(extractFunction(js, 'collapseToThumb')),
    'expanding a thumb clears leftover inline styles: code opens bright on the FIRST click');
  // AGENT: свои инструментальные карточки,厨房 только в обычном режиме
  assert(/if \(!\(ui\.agentMode \|\| S\.agentMode\)\) \{[\s\S]*?qtSweep\(ui, ev\.group\);[\s\S]*?qtOpen\(ui, ev\);[\s\S]*?break;[\s\S]*?\}/.test(js) &&
    /'tool-card' \+ \(waitVisual \? ' tool-wait' : ''\)/.test(js) &&
    /function flushAgentGroup/.test(js) && /finishToolWait\(node\);/.test(js) &&
    /TOOL_WAIT_AFTER_PAINT_MS = 800/.test(js) &&
    /if \(e\.target\.closest\('\.ql-row'\)\) return;/.test(extractFunction(js, 'makeCard')) &&
    /function cancelFoldSoon\(card\)/.test(js) &&
    /card\._foldT = setTimeout\(\(\) => \{/.test(js) &&
    /collapseSoon\(card, \{/.test(extractFunction(js, 'flushAgentGroup')) &&
    /cancelFoldSoon\(card\);/.test(extractFunction(js, 'flushAgentGroup')) &&
    /\.ql-row,\.qt-row,\.ag-rows,\.qt-kids'\);/.test(extractFunction(js, 'addFoldButton')) &&
    /node\.addEventListener\('click', bgFold\);/.test(extractFunction(js, 'addFoldButton')),
    'AGENT keeps its OWN tool cards with groups; opening a tool inside a group CANCELS the pending group collapse — behaviorally');
}

function testIterationXContracts() {
  // ================= ИТЕРАЦИЯ X (beta.28) — 10 пунктов отзыва =================
  // П.1: вальс целится ПОДПИСЬЮ инструмента в НАЗВАНИЕ группы (текст тает
  // в названии, не ниже) — прицел по верху узла опускал его на полстроки
  const fold = extractFunction(js, 'qtFold');
  assert(/titleEl/.test(fold) && /labEl/.test(fold) &&
    /const labC = \(lRect0\.top \+ lRect0\.height \/ 2\) - base0;/.test(fold) &&
    /const need = titleC\(\) - labC - baseTop;/.test(fold) &&
    !/scale\(\.93\)/.test(fold),
    'X1: the flying tool aims its LABEL at the group TITLE line — dissolves in it, never below');
  // П.2: папки — внешний вертикальный margin тела убран (скачок 6px в первый
  // кадр открытия и в последний кадр закрытия), зазор живёт внутри
  assert(/\.qt-folder\.open \.qt-kids\{display:flex\}/.test(css) &&
    !/\.qt-folder\.open \.qt-kids\{display:flex;margin/.test(css) &&
    /\.qt-kids>\.qt-rows\{[^}]*padding:2px 0 3px/.test(css) &&
    /LEAD = 70/.test(js),
    'X2: folder body keeps its outer geometry calm — no 6px jump at open start / close end');
  // П.4/П.8: дизайн инструментов решает ЖИВОЙ режим; одиночка не сворачивается
  // в папку; ход мыслей показывается в любом режиме
  assert(/if \(!\(ui\.agentMode \|\| S\.agentMode\)\) \{/.test(js) &&
    !/if \(!ui\.agentMode\) break;/.test(extractFunction(js, 'handleEvent')) &&
    /if \(pending\.length === 1\) \{/.test(extractFunction(js, 'qtSweep')),
    'X4/X8: live mode picks the tool design; a lone tool stays its own line — never a one-item folder');
  // П.6: после переключения тумблера резинка молчит до СЛЕДУЮЩЕГО наведения
  assert(/shell\.dataset\.agHold = '1';/.test(js) &&
    /sw\.dataset\.agHold/.test(js) &&
    /delete this\.dataset\.agHold;/.test(js),
    'X6: the knob rubber plays after a toggle only on the NEXT hover');
  // П.7: одно битое событие не убивает поток; план-gate со стражем; курсор
  // дышит под планом и в reduced-motion; вход в диалог во время генерации
  // показывает живую заглушку, ответ доезжает плавно
  assert(/try \{ dispatchStreamEvent\(ev, ui\); \}/.test(js) &&
    /ui\.planWatchdog = setTimeout/.test(js) &&
    /gate-hold/.test(js) && /\.thinking-line\.gate-hold \.tw-quip\{display:none\}/.test(css) &&
    /caretDim 1\.6s ease-in-out infinite!important/.test(css) &&
    /function appendLivePlaceholder/.test(js) &&
    /function appendFreshMessages/.test(js) &&
    /r\.generating \|\| S\.detached === id/.test(js),
    'X7: a broken event cannot kill the stream; the plan gate has a watchdog; the caret never dies; an in-flight answer greets you with a live line and arrives smoothly');
  // П.3: сервер узнаёт вызов-конверт {"open_url": {...}} (python-тест
  // проверяет исполнение; здесь — фронтовая часть: стрим придерживается)
  // П.9/П.10 — серверные, их контракты живут в python-тестах.
}

function testIterationYContracts() {
  // ================= ИТЕРАЦИЯ Y (beta.29) — 7 пунктов =================
  const ev = extractFunction(js, 'handleEvent');
  const fold = extractFunction(js, 'qtFold');
  // П.1: ход мыслей — тихому режиму СВОЙ дизайн (кухня), не агентская карточка
  assert(/qt-node qt-think/.test(ev) && /qtThinkFeed\(flow, ev\.text\)/.test(ev) &&
    /\.qt-think \.qt-ico\{font-size:12px/.test(css) &&
    /qtMiniaturize\(ui\.thinkCard\);/.test(js),
    'Y1: quiet mode renders thinking in KITCHEN design (gray stream line), never an agent card');
  // П.1: показывается только у рабочих ответов (решает сервер, но свёртка тихая)
  // П.3: история восстанавливает ДИЗАЙН ТОГО ОТВЕТА — meta.agent решает
  assert(/const agentAnswer = !!meta\.agent;/.test(extractFunction(js, 'restoreTrace')) &&
    /renderAgentTraceGroups\(node, toolTraces\)/.test(extractFunction(js, 'restoreTrace')) &&
    /renderToolKitchen\(node, toolTraces\)/.test(extractFunction(js, 'restoreTrace')) &&
    /function renderAgentTraceGroups/.test(js),
    'Y3: an answer saved in quiet mode restores in QUIET design, agent answers in AGENT design — never mixed');
  // П.2: подпись доживает до названия: поток складывается первым, фейд в конце
  // AD: затемнение теперь стартует в середине полёта — инструмент тонет в темноте
  assert(/bodyEl\.style\.transition = 'height \.3s ease, opacity \.22s ease';/.test(fold) &&
    /const flyKeys = \(target\) => \[/.test(fold) &&
    /\}, 1200\);/.test(fold),
    'Y2: the flying label survives until the group TITLE: stream collapses first, dimming sinks it');
  // П.5: строка статуса возрождается сама — курсор не умирает между ходами
  assert(/isConnected === false/.test(extractFunction(js, 'runStatus')) &&
    /ui\.statusEl = ensureStatus\(ui\);/.test(extractFunction(js, 'runStatus')),
    'Y5: runStatus resurrects the status line itself — the caret can never vanish between model turns');
  // П.4: подсветка выключенного тумблера молчит до нового наведения
  assert(/\.agent-switch:not\(\[data-ag-hold\]\) \.agent-switch-track:not\(:has\(input:checked\)\):hover/.test(css),
    'Y4: the off-toggle glow stays silent while the cursor never left; plays again only on the NEXT hover');
  // П.6: запись превращается в WAV 16кГц прямо в браузере — ffmpeg больше не нужен
  // (AA: диктовку сменил режим разговора — WAV теперь готовит voiceTranscribe)
  const wav = extractFunction(js, 'blobToWav16k');
  assert(/function blobToWav16k/.test(js) && /decodeAudioData/.test(wav) &&
    /v\.setUint32\(24, rate, true\)/.test(wav) && /data:audio\/wav;base64,/.test(wav) &&
    /blobToWav16k\(blob\)/.test(extractFunction(js, 'voiceTranscribe')),
    'Y6: the browser itself converts the recording to 16kHz mono WAV — webm/opus is rejected by every ASR model and ffmpeg may be absent');
}

function testIterationZContracts() {
  // ================= ИТЕРАЦИЯ Z (beta.30) — 8 пунктов =================
  const sweep = extractFunction(js, 'qtSweep');
  const fq = extractFunction(js, 'flushQt');
  const sendFn = extractFunction(js, 'send');
  // П.2/3: строка хода мыслей — НЕ инструмент: в папки не собирается
  assert(/if \(nn\.classList\.contains\('qt-think'\)\) return;/.test(sweep) &&
    /if \(n\.classList\.contains\('qt-think'\)\) return;/.test(fq),
    'Z2/3: the thinking line never lands in a family folder — folders collect TOOLS only');
  // П.5: страж строки состояния — «думаю…» возвращается само
  assert(/const statusWatch = setInterval/.test(sendFn) &&
    /busyMode\(ui, \['думаю…', 'готовлю ответ', 'ещё секунду'\], 1500\);/.test(sendFn) &&
    /clearInterval\(statusWatch\);/.test(sendFn),
    'Z5: a watchdog resurrects the status line — the caret can never stay gone');
  // П.5: готовый ответ приезжает мягко
  assert(/animation = 'freshIn \.32s ease both'/.test(extractFunction(js, 'appendFreshMessages')) &&
    /@keyframes freshIn/.test(css),
    'Z5: a finished background answer fades in softly instead of popping');
  // П.6: план привязан к диалогу
  assert(/dock\.dataset\.chatId = ui\.chatId \|\| S\.chatId \|\| '';/.test(js) &&
    /d\.style\.display = \(d\.dataset\.chatId === id\) \? '' : 'none';/.test(extractFunction(js, 'openChat')),
    'Z6: the plan dock belongs to its chat: hidden in others, restored on return');
  // П.7: голосовой режим — окно, орб, распознавание, синтез, перебой
  assert(/function openVoiceMode/.test(js) && /function closeVoiceMode/.test(js) &&
    /function voiceListen/.test(js) && /function voiceTranscribe/.test(js) &&
    /function voiceBargeLoop/.test(js) && /blobToWav16k\(blob\)/.test(extractFunction(js, 'voiceTranscribe')) &&
    /SpeechSynthesisUtterance/.test(js) &&
    /window\.speechSynthesis\.cancel\(\);/.test(extractFunction(js, 'voiceBargeLoop')) &&
    /opts\.onDelta && ev\.type === 'delta'/.test(sendFn) &&
    /id="voiceBtn"/.test(html) &&
    /\.voice-box\{display:flex/.test(css) && /\.voice-box\.speaking \.v-orb b\{/.test(css),
    'Z7: full voice mode — orb (listen/think/speak), VAD end-of-phrase, browser WAV, streaming TTS by sentences, barge-in');
  // П.8: негативный промпт — в python-тестах
}

function testProactiveModesBudgetAndAbortContracts() {
  // проактивные режимы: «зачем» сверху, ПОД ним — ИМЕНОВАННЫЙ тумблер
  // (иконка + название + выключенный круглешок, свой цвет на режим) и «Пропустить»
  assert(/case 'mode_request'/.test(js) && /mc-switch/.test(js) &&
    /mc-skip/.test(js) && /mc-sw-track/.test(js) && /MODE_META/.test(js) &&
    /case 'mode_changed'/.test(js) &&
    js.includes("api('/api/questions/answer'"),
  'mode requests render a NAMED toggle (icon+title+off knob) under the text');
  assert(/<svg viewBox="0 0 24 24"[^>]*><rect x="5" y="8" width="14" height="11" rx="3"\/>/.test(js) &&
    /const syncSwitchState = \(\) =>/.test(js) &&
    /mc-readonly/.test(js) &&
    /\.mode-card\.mc-readonly\{opacity:\.55\}/.test(css) &&
    /\.mode-card\.mc-readonly \.mc-switch,\.mode-card\.mc-readonly \.mc-skip\{pointer-events:none\}/.test(css) &&
    !/\.mode-card\.mc-readonly\{[^}]*pointer-events/.test(css) &&
    /\.mc-sw-track\.on i\{[^}]*translateX\(14px\)/s.test(css),
  'agent gets a robot icon; reopening syncs the knob to the live state, dims the card yet keeps it closable');
  assert(/\.mc-switch\{[^}]*display:flex[^}]*cursor:pointer/s.test(css) &&
    /\.mc-sw-track\{[^}]*width:38px/s.test(css) &&
    /\.mode-card\.m-agent\{--mc:#8f2739\}/.test(css) &&
    /\.mode-card\.m-camera\{--mc:var\(--teal\)\}/.test(css) &&
    /\.mode-card\.m-computer\{--mc:var\(--violet\)\}/.test(css),
  'agent blush is deep BORDO now, computer shines VIOLET, camera keeps teal');
  assert(/\.mc-row\{display:flex;flex-direction:column/.test(css) &&
    !/transform:scale\(1\.55\)/.test(css),
  'mode card: text on top, toggle below at natural size');
  // КАМЕРА и КОМПЬЮТЕР — прежние кнопки-тумблеры со СВОИМИ цветами и звуком
  assert(/<button class="toggle" id="tgCamera"/.test(html) &&
    /<button class="toggle" id="tgComputer"/.test(html) &&
    /#tgCamera\.on\{[^}]*rgba\(47,156,146/s.test(css) &&
    /\.toggle#tgComputer\.on\{[^}]*rgba\(143,134,207/s.test(css),
  'camera keeps teal, the computer toggle is violet now');
  assert(/\$\('#tgCamera'\)\.addEventListener\('click'/.test(js) &&
    /\$\('#tgComputer'\)\.addEventListener\('click'/.test(js) &&
    /beep\(S\.cameraOn \? 760 : 420, 0\.1\)/.test(js) &&
    /beep\(S\.computerUse \? 760 : 420, 0\.1\)/.test(js),
  'camera/computer buttons beep exactly like the agent switch');
  // РЕЖИМЫ ЖИВУТ ПО-РАЗНОМУ: обычный — серая qt-кухня, AGENT — свои
  // карточки с группами. X: дизайн решает ЖИВОЙ режим, а не снимок на
  // момент отправки; ход мыслей показывается в ЛЮБОМ режиме (тихому тоже,
  // своим дизайном) — иначе история «воскрешала» невиденные карточки
  assert(/termLine\('\$ ' \+ ev\.name \+ ' ' \+ JSON\.stringify/.test(js) &&
    /qtOpen\(ui, ev\);/.test(js) &&
    /'tool-card' \+ \(waitVisual \? ' tool-wait' : ''\)/.test(js) &&
    /if \(!\(ui\.agentMode \|\| S\.agentMode\)\) \{/.test(js) &&
    !/if \(!ui\.agentMode\) break;/.test(extractFunction(js, 'handleEvent')),
    'normal mode runs the gray kitchen; AGENT runs its own cards; the LIVE mode decides the design and thinking shows in every mode');
  // подписи: пауза ~1с, компактные, у микрофона и вложения
  assert(/animation:tipIn \.16s 1\.45s both/.test(css) &&
    /@keyframes tipIn/.test(css) && /max-width:180px/.test(css) &&
    /white-space:normal/.test(css) &&
    /id="attachBtn" data-tip="Вложить файл"/.test(html) &&
    /id="micBtn" data-tip="Голосовой режим"/.test(html) &&
    /data-tip="AGENT — план и самостоятельная работа"/.test(html) &&
    /data-tip="Лимит ₽ на ответ"/.test(html),
  'tooltips wait ~1.5s, stay compact, mic and attach included');
  // монета лимита пульсирует как точка колокольчика (тот же bellPing)
  assert(/\.budget-btn\.on \.budget-coin\{animation:bellPing 1\.9s ease-in-out infinite\}/.test(css) &&
    !/budgetPulse/.test(css),
  'the budget coin pulses exactly like the notification bell dot');
  // шаги плана не пролетают: каждый шаг живёт на экране минимум 950мс
  assert(/const PLAN_STEP_MS = 950/.test(js),
  'plan steps hold on screen long enough not to flash by');
  assert(/\.replace\(\/~~\/g, ''\)/.test(js) && /li\._planText = String/.test(js),
    'plan steps arrive as CLEAN text: model markdown like ~~step~~ is stripped');
  // дописанный код сворачивается в строку СРАЗУ, не дожидаясь конца ответа
  assert(!/pre-folded/.test(js) && !/pre-folded/.test(css) && !/ui\.codeExpanded/.test(js),
    'code NEVER collapses into a slim «развернуть» row: opening a tool shows the full code (scrollable)');
  // дописанный код: окно ограничено по высоте, нейтральный цвет строк,
  // стройная строка СРАЗУ после закрытия fence, клик раскрывает обратно
  assert(/\.md pre\{[^}]*max-height:min\(30vh,260px\)/s.test(css) &&
    /\.md pre code\{[^}]*color:#d4d9e0\}/s.test(css) &&
    /\.md pre\.live-code\{max-height:min\(30vh,260px\)/.test(css) &&
    /function foldOneCodeBlock\(pre, ui, key\)/.test(js) &&
    /function refoldCodeBlocks\(ui, root, livePre, text\)/.test(js) &&
    /refoldCodeBlocks\(ui, tailEl, livePre, text\);/.test(js) &&
    /ui\._codeCache\.set\(key, slot\);/.test(js) &&
    /pre\.replaceWith\(slot\);/.test(extractFunction(js, 'refoldCodeBlocks')) &&
    /\.code-slot\{display:contents\}/.test(css) &&
    /_codePeek\.add\(key\.split\('\\u0001'\)\[0\]\);/.test(js) &&
    /_codePeek\.has\(key\.split\('\\u0001'\)\[0\]\)/.test(js) &&
    /instant: false, cls: 'th-code inline-thumb'/.test(extractFunction(js, 'foldOneCodeBlock')) &&
    !/code-compact/.test(js) && !/code-compact/.test(css) &&
    /tag: S\.agentMode \? 'развернуть' : ''/.test(js) &&
    /tag: 'развернуть',/.test(js),
    'closed fence becomes a slim thumbnail tab THE MOMENT it closes, WITH its shrink animation (instant:false), once — the cached slot survives the typer repaint');
  // панель ФАЙЛЫ: «Импорт», иконка обновления (две круговые стрелки),
  // «Очистить» замьючена, когда чистить нечего
  assert(/id="uploadHere" title="Импорт"/.test(html) &&
    /M12 16V5/.test(html) && /M4 19h16/.test(html) &&
    /id="refreshFiles"[^>]*title="Обновить"/.test(html) &&
    /const spin = \$\('#refreshFiles svg'\);/.test(js) &&
    /spin\.classList\.add\('spin'\);/.test(js) &&
    /const left = 650 - \(performance\.now\(\) - started\);/.test(js) &&
    /@keyframes btnSpin\{to\{transform:rotate\(360deg\)\}\}/.test(css) &&
    /\.btn:disabled\{opacity:\.52;cursor:default[^}]*pointer-events:none\}/.test(css) &&
    /wipe\.disabled = !\(\(info\.files \|\| 0\) > 0\);/.test(js),
    'files panel: UPLOAD icon (arrow up over a bar), spinning refresh arrows, «Очистить» visibly muted');
  // Q-ФАЙЛЫ: чип в диалоге тянется в песочницу с тем же шлейфом; бросок
  // файлов в сетку «Файлов» — импорт в песочницу, а не вложение в чат
  assert(/a\.draggable = true;/.test(extractFunction(js, 'attachFileChip')) &&
    /startDragGhosts\(e, \[a\], a\);/.test(extractFunction(js, 'attachFileChip')) &&
    /await uploadToSandbox\(Array\.from\(e\.dataTransfer\.files\), S\.fdir \|\| ''\);/.test(js) &&
    /e\.target\.closest\('#view-files'\)\) return;/.test(js),
    'a file chip in chat drags into the sandbox with the SAME ghost trail; dropping OS files onto the files grid imports them into the sandbox');
  // R: шлейф гаснет в САМОМ броске — drop на документе (capture) и dragend,
  // иначе перерисовка сетки съедает источник и миниатюры висят вечно
  assert(/document\.addEventListener\('drop', \(\) => stopDragGhosts\(\), true\);/.test(js) &&
    /document\.addEventListener\('dragend', \(\) => stopDragGhosts\(\), true\);/.test(js),
    'the drag ghost layer is killed on EVERY drop/dragend path — it can never freeze on screen');
  // W: название диалога — «…» вместо «Новый диалог», приезд из фона
  assert(/case 'chat_title':/.test(js) &&
    /title-pending/.test(js) &&
    /\.chat-title\.title-pending\{animation:titlePulse 1\.15s ease-in-out infinite\}/.test(css) &&
    /@keyframes titlePulse/.test(css) &&
    /\[1600, 4200\]\.forEach/.test(js),
    'a fresh chat shows a live «…» instead of «Новый диалог»; the background title arrives via chat_title or a delayed list refresh — it can NEVER stay unnamed');
  // W: инструменты в группе агента раскрываются С анимацией высоты
  assert(/det\.animate\(/.test(extractFunction(js, 'qlRowToggle')) &&
    /duration: 440/.test(extractFunction(js, 'qlRowToggle')) &&
    /duration: 380/.test(extractFunction(js, 'qlRowToggle')) &&
    extractFunction(js, 'flushAgentGroup').includes('qlRowToggle(row);') &&
    extractFunction(js, 'renderAgentTraceGroups').includes('qlRowToggle(row)') &&
    extractFunction(js, 'qlRowToggle').includes("then(() => { row.classList.remove('open'); det.style.overflow = ''; })"),
    'tools inside an agent group expand/collapse with the SAME height animation — and closing (BL) mirrors opening: the class leaves only AFTER the collapse animates');
  // W: курсор думания не замирает при системном «уменьшить движение»
  assert(/\.tw-caret,\.caret\{animation:caretDim 1\.6s ease-in-out infinite!important\}/.test(css) &&
    /@keyframes caretDim/.test(css),
    'the thinking caret keeps a gentle opacity breathe even under prefers-reduced-motion — never looks frozen');
  // U: тумблер — значение регистрируется ДО звука, звук в try-catch,
  // клик по подписи тоже переключает
  assert(/controlChanged\(\);\n          try \{ blip\(it\.val\); \} catch/.test(js) &&
    /if \(sw\.contains\(e\.target\)\) return;/.test(js) &&
    /sw\.click\(\);/.test(js),
    'ui toggle registers its value BEFORE the sound (sound is try-caught) and the whole row is clickable');
  // S: «не найдено» при переносе в песочницу больше не тупик: чип несёт
  // ссылку, содержимое ввезётся заново
  assert(/function importByUrl\(url, name, destDir\)/.test(js) &&
    /setData\('text\/jarvis-url', f\.url \|\| ''\);/.test(extractFunction(js, 'attachFileChip')) &&
    /не найдено\|not found/.test(extractFunction(js, 'dropOnto')) &&
    /await importByUrl\(url, name, destDir\);/.test(extractFunction(js, 'dropOnto')) &&
    /await dropOnto\(e, ''\);/.test(js),
    'dragging a chat chip into the sandbox NEVER dead-ends: a missing path falls back to importing the file by its URL');
  // поток: полоса удлиняется ПЕРВОЙ, строка пишется после неё
  assert(/qt-flowline qt-wait/.test(js) &&
    /classList\.remove\('qt-wait'\), 230\)/.test(js) &&
    /\.qt-flowline\.qt-wait\{opacity:0;animation:none\}/.test(css),
    'the strip lengthens FIRST, the line writes after — growth tracks the text, no full-length jump');
  // режим включили ПОСЛЕ отправки — текущий run обязан увидеть план
  assert(/if \(ui\) ui\.agentMode = true;/.test(js) &&
    /\(ui\.agentMode \|\| S\.agentMode\)/.test(js),
  'a mode_changed mid-run unblocks plan rendering for the live stream');
  // сценарии: та же служебная полоса и сетка, что в AUTO; карточки = task-card
  assert(/task-card scenario-card/.test(js) && /tc-head/.test(js) &&
    /scenarioStats/.test(js) && /auto-bar scenario-bar/.test(html) &&
    /class="task-grid scenario-grid"/.test(html) &&
    /badge\.style\.display = active > 0 \? '' : 'none'/.test(js) &&
    !/cont-sep/.test(js) && !/cont-sep/.test(css),
  'scenarios reuse the AUTO bar/grid/task-card canon; zero badge hidden; no separator');
  // ответ продолжается В камере, когда её включили во время стрима
  assert(/function adoptRunIntoCam/.test(js) && /function releaseRunFromCam/.test(js) &&
    /if \(S\.streaming && S\.followUi\) adoptRunIntoCam\(\);/.test(js) &&
    /if \(S\.camLink\) releaseRunFromCam\(\);/.test(js),
  'opening the camera mid-answer adopts the live reply into the cam chat');
  // request_mode доступен модели, но не параллелится и не заменяет разрешение
  // лимит на лету: смена во время стрима уходит на сервер и гаснет после ответа
  assert(/S\.streaming && S\.chatId/.test(js) &&
    js.includes("api('/api/budget'") &&
    js.includes('budget_rub: value || 0'),
  'budget changes during a live run are pushed to the server');
  assert(/if \(S\.budgetRub\) \{/.test(js) && /budgetBtn\.classList\.remove\('on'\)/.test(js),
    'the ruble button goes dark as soon as the answer finishes');
  // продолжение ответа после интерактивной панели: та же карточка, БЕЗ
  // разделителя, кнопки действий — только на полностью законченном ответе.
  // AA: продолжение переживает перерисовку ленты — панель знает msgId своего
  // сообщения, а сервер дописывает текст в ТО ЖЕ сообщение, а не плодит новое
  assert(js.includes("send({ silent: true, continue: true, continueOf: panelMsgId })") &&
    /S\.lastUi/.test(js) && /requestHost\.contains\(S\.lastUi\.node\.root\)/.test(js) &&
    /const panelMsg = box\.closest\('\.msg'\);/.test(js) &&
    /m\.dataset && m\.dataset\.msgId === opts\.continueOf/.test(js) &&
    /continue_of: opts\.continueOf \|\| ''/.test(js) &&
    /case 'ai_msg':/.test(js) &&
    /ui-panel:not\(\.ui-sent\)/.test(js) &&
    /if \(!pendingPanel\) addMsgActions/.test(js),
  'ui-panel answers continue the same message card instead of a new reply');
  // результат инструмента — человеческая выжимка, а не сырой JSON
  // computer-use: при провале самопроверки — кнопки открыть нужные панели прав
  assert(js.includes("api('/api/computer/permissions'") &&
    /permAcc/.test(js) && /permScr/.test(js) &&
    /Открыть «Универсальный доступ»/.test(js) && /Открыть «Запись экрана»/.test(js),
  'a failed self-check offers direct links to the macOS permission panes');
  assert(/function toolResultText/.test(js) &&
    !/else txt = JSON\.stringify\(r, null, 1\)/.test(js) &&
    /'HTTP ' \+ r\.status \+ ' · получено '/.test(js),
  'tool cards render a human digest, never a raw JSON envelope');
  // экран успевает за печатью: BE — догоняющий скролл без smooth-интерполяции
  // (pin-instant на весь догон, записи точные, каждый кадр — четверть остатка)
  const scrollFn = extractFunction(js, 'scrollDown');
  const chaseFn = extractFunction(js, 'chaseBottom');
  assert(/chaseBottom\(box, run\)/.test(scrollFn) && /pin-instant/.test(chaseFn),
    'chaseBottom keeps up with fast code — instant writes, eased distance');
  // ГЛАВНОЕ: программная прокрутка не снимает follow-интент. Раньше scroll-
  // событие от НАШЕГО ЖЕ pin-а при подросшем контенте (>150px) гасило
  // followOutput — ответ «улетал вниз», страница не скроллилась следом.
  assert(!/else if \(distance > 150\) run\.followOutput = false/.test(js),
    'programmatic scroll no longer cancels follow intent (wheel/touch only)');
  // прерванный ответ не оставляет открытых панелей
  const stopBranch = extractFunction(js, 'stopStream');
  const abortClose = /foldCodeBlocks\(ui\.mdEl, true\)/.test(extractFunction(js, 'send')) ||
    /foldCodeBlocks\(ui\.mdEl, true\)/.test(js);
  assert(abortClose && /collapseSoon\(ui\.thinkCard/.test(js),
    'an aborted answer folds code blocks and collapses the thinking card');
}

function testIterationADContracts() {
  // AD1: мысль — предложения с пробелами, не склейка
  const feed = extractFunction(js, 'qtThinkFeed');
  assert(/flow\._buf = flow\._buf\.replace\(/.test(feed) &&
    /'\$1 '/.test(feed),
    'AD1: glued sentences get a space after the period');
  assert(/if \(se > 20\) cut = se;/.test(feed),
    'AD1: a finished sentence is the preferred line boundary');
  // AD2: инструмент тонет в темноте под группой
  const fold = extractFunction(js, 'qtFold');
  assert(/const flyKeys = \(target\) => \[/.test(fold) &&
    /offset: \.55/.test(fold) &&
    /easing: 'cubic-bezier\(\.62,\.04,\.6,\.55\)'/.test(fold) &&
    /blur\(4px\) brightness\(\.08\)/.test(fold) &&
    /setKeyframes\(flyKeys\(aim\)\)/.test(fold),
    'AD2: dimming lives INSIDE the WAAPI flight keyframes — cannot be lost');
  // AD3: ховер — только сам инструмент
  assert(/\.qt-head:hover \.qt-name,\.qt-row:hover \.qt-name/.test(css) &&
    !/\.qt-folder:hover \.qt-name/.test(css) &&
    !/\.qt-rows \.qt-row:hover/.test(css),
    'AD3: hovering highlights the NAME only — no area glow, never the group');
  // AD4: новый ответ ВСЕГДА внизу; старые панели законсервированы
  const sendFn = extractFunction(js, 'send');
  assert(/allMsgs\[allMsgs\.length - 1\] === root/.test(sendFn) &&
    /mountUiPanels\(node\.body, \{ inert: !activePanel \}\);/.test(js) &&
    /if \(inert \|\| sent\) box\.classList\.add\('ui-inert'\);/.test(extractFunction(js, 'mountUiPanels')) &&
    /\.ui-panel\.ui-inert\{pointer-events:none;opacity:\.5/.test(css),
    'AD4: only the last message continues; historical panels are inert');
  // AD5: плавная кромка ленты у подсказок
  assert(/mask-image:linear-gradient\(180deg,#000 0,#000 calc\(100% - 30px\),transparent\)/.test(css),
    'AD5: the stream dissolves at the bottom boundary instead of a hard cut');
  // AD6: тёмная неактивная развёрнутая камера/разговор
  assert(/node\.classList\.contains\('cam-msg'\) && !S\.camStream\) node\.classList\.add\('offline'\);/.test(js) &&
    /\.cam-msg\.offline \.cam-col-left,\.voice-msg\.offline \.v-side\{opacity:\.5;pointer-events:none\}/.test(css) &&
    !/\.cam-msg\.offline,\.voice-msg\.offline\{opacity/.test(css) &&
    /\.cam-msg\.offline \.cam-chat\{opacity:\.85\}/.test(css) &&
    /\.cam-msg\.offline \.cam-feed\{opacity:\.4\}/.test(css),
    'AD6: a finished card is hinted inert; dialog slightly dimmed, live scene feed dimmed hard');
  // AD7: иконка сценариев — в общем стиле, без смайлика в заголовке
  assert(!/⚡️/.test(html) && /<h2>Сценарии<\/h2>/.test(html) &&
    /data-view="scenarios"/.test(html) && /\.nav-ico svg\{display:block/.test(css),
    'AD7: scenarios tab uses the same line-icon design; page title has no emoji');
  // AD8: кнопка в углу — ВЕСЬ звук
  assert(/function setSound\(on\)/.test(js) && /setSound\(!soundOn\(\)\)/.test(js) &&
    /if \(!soundOn\(\) \|\| !voiceOn\(\) \|\| !text\) return;/.test(js) &&
    /title="Звук"/.test(html) && !/Голос Джарвиса: озвучивать/.test(html),
    'AD8: the corner button is the master sound switch (UI sounds + spoken replies)');
  // AD9: кружок AGENT чуть медленнее
  assert(/transition:transform \.45s cubic-bezier\(\.3,\.6,\.3,1\)/.test(css) &&
    /\.agent-switch-track input:checked \+ i\{transform:translateX\(10px\)/.test(css) &&
    /left:5px;top:3px/.test(css) &&
    /animation:agKnobRubber 1s/.test(css),
    'AD9: agent toggle — 10px travel, 1.3s glide both ways, hover 1.5s');
}

function testIterationAJContracts() {
  // AS: у ответов КРУГЛЕШОК (ядро без колец). Эстафета отменена целиком:
  // ни relay-функций в JS, ни relay-правил в CSS — док живёт всегда
  assert(js.includes('const AVATAR_CORE =') &&
    js.includes('class="ai-core"') &&
    !js.includes('relayTyping') && !js.includes('relayFlick') &&
    !css.includes('relay') &&
    (js.match(/<animate/g) || []).length === 0 &&
    !/sigil/i.test(js) && !/sigil/i.test(css),
    'AS: answer avatar is a live core dot; the relay saga is gone for good');
  assert(css.includes('.ai-core{position:absolute;left:50%;top:8px;width:15px;height:15px;') &&
    !css.includes('coreBreathe') &&
    css.includes('animation:coreLive 4.6s ease-in-out infinite}'),
    'AS/AY/BC/BD: history cores are STATIC and cheap; only the live answer breathes');
}

function testIterationAKContracts() {
  // AK: стрелка — SVG-шеврон по центру; ничего не выпирает из дока
  assert(/<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"/.test(html) &&
    !/>‹<\/button>/.test(html),
    'AK: collapse arrow is an SVG chevron, perfectly centered');
  assert(/_with_reasoning_lang\(convo\)/.test(pyAgent),
    'AK: reasoning-language system note rides next to the last message');
}

function testIterationBIContracts() {
  // BK: ДЖАРВИС ВСЕГДА ОТКРЫВАЕТСЯ С ДОКОМ — состояние панели не переживает
  // перезапуск: сохранённого 'open' больше не читаем и не восстанавливаем
  assert(js.includes("localStorage.removeItem('jarvis.sidebar2')") &&
    !js.includes("localStorage.getItem('jarvis.sidebar2')") &&
    !js.includes("localStorage.getItem('jarvis.sidebar')") &&
    html.includes('b70</span>'),
    'BK: the dock opens with the doc on EVERY launch; the stored open state is retired');
  // BI: чипы — только хвост чата, без протухших сообщений
  const pyAgent = fs.readFileSync(path.join(root, 'app/jarvis/agent.py'), 'utf8');
  const pyDb = fs.readFileSync(path.join(root, 'app/jarvis/db.py'), 'utf8');
  assert(pyDb.includes('def get_recent_messages(') &&
    pyDb.includes('ORDER BY created_at DESC LIMIT ?) ORDER BY created_at ASC') &&
    !pyAgent.includes('get_messages(') &&
    pyAgent.includes('_SUGGEST_VERB') &&
    pyAgent.includes('(?:ить|ать|ять|еть)(?:ся|сь)?'),
    'BI: reply chips come from the freshest messages; bare names never pass');
  // BI: 6 объёмных фигур; 4D выворачивается в плоскостях XW/ZW
  assert(js.includes('function dotShapeFrame(') &&
    js.includes('function dotRotAxis(') &&
    js.includes('const a = t * 0.85, b = t * 0.5;') &&
    js.includes('k = 3.1 / (3.1 - w2 * 1.5)') &&
    js.includes('core._shapeRaf = requestAnimationFrame(rot);') &&
    !css.includes('@keyframes spinZ') && !css.includes('.rot-g.rot-z') &&
    !js.includes('DOT_SPINS'),
    'BI: 6 volumetric shapes; the tesseract turns inside out like a real 4D body');
  // BI: математика — дроби не топят переменные, корень честный, стрелки дышат
  assert(markdown.includes('function mathRender(src, inline)') &&
    markdown.includes('class="msq-svg"') &&
    css.includes('.mrel{') && css.includes('.math-inline .mfrac{'),
    'BI: inline math stays in the line; the radical grows its own stroke');
  // BI: графики — мат-соотношения, засечки с подписями, пан везде
  assert(js.includes('const ASPECTS = [') &&
    js.includes("{ r: Math.SQRT2, label: '1:√2' }") &&
    js.includes('const niceStep = (span, target) => {') &&
    js.includes('const aimDomain = () => {') &&
    js.includes('const featureXs = () => {') &&
    // BL: жесты 3D — колесо = масштаб, левая кнопка = вращение, средняя = пан
    js.includes('zoomBy(e.deltaY < 0 ? 1.12 : 1 / 1.12);') &&
    js.includes("mode = (e.button === 0 || e.button === 1) ? 'rot' : (e.button === 2 ? 'pan' : null);") &&
    // BL: ось больше не обрезается пополам (граница цикла — тоже значение)
    js.includes('const x0t = Math.ceil(x0 / sx) * sx, x1t = Math.floor(x1 / sx) * sx;'),
    'BI: plots use mathematicians\' aspect ratios; axes carry real ticks on the WHOLE axis');
  // BI: агент всегда пишет поверхности в ключе z — правило 11
  assert(pyAgent.includes('ОБЯЗАТЕЛЬНО в ключе "z"'),
    'BI: rule 11 forces surfaces through the z key');
}

function testIterationBJContracts() {
  // BJ: СКРОЛЛ — ровный ход без разгона: скорость пропорциональна остатку
  const chase = js.split('function chaseBottom(')[1].split('\nfunction ')[0];
  assert(chase.includes('const target = Math.min(13, Math.max(0.9, gap * 0.13));') && chase.includes('const v = Math.min(target, (st.chaseV || 0) + 1.4);') &&
    !js.includes('st.v') &&
    js.split('function followLiveStream(')[1].split('\nfunction ')[0]
      .includes('chaseBottom(box, run);'),
    'BJ: one smooth ease-out chase curve — no acceleration bursts anywhere');
  // BJ: уведомление ДО начала печати — в теле ответа, перед строкой статуса
  const tl = js.split('function toolLine(')[1].split('\nfunction ')[0];
  const rt = js.split('function renderTyped(')[1].split('\nfunction ')[0];
  assert(tl.includes('let preText = false;') &&
    tl.includes('!cand.mdEl') &&
    tl.includes('body.insertBefore(liveUi.marksEl, st);') &&
    rt.includes('ui.marksEl.parentNode !== ui.node.body') &&
    js.split("case 'reset':")[1].split('\n    case ')[0]
      .includes('ui.node.body.insertBefore(slot, st);') &&
    js.split("case 'reset':")[1].split('\n    case ')[0]
      .includes('(ui.segs || []).forEach((sg) => { if (sg.marksEl) slots.push(sg.marksEl); });'),
    'BJ: a mode enabled before any text lands in the answer body, above the future text');
  // BJ: круглешок — крупнее, мягче, грани видны, конец не рвёт фигуру
  const play = js.split('function dotShapePlay(')[1].split('\nfunction ')[0];
  const fd = js.split('function finishLiveDot(')[1].split('\nfunction ')[0];
  assert(css.includes('width:46px;height:46px;') &&
    css.includes('.dot-shape-svg.sh-fly{animation:dotSvgFly .5s') &&
    js.includes('(0.18 + 0.34 * bright)') &&
    js.includes('stroke-width=".4"') &&
    fd.includes("svg.classList.add('sh-fly');") &&
    fd.includes('svg._closeRaf = setTimeout(() => svg.remove(), 560);') &&
    fd.includes('dotShapeFrame(key, t, L, Math.min(1, p));') &&
    play.includes('core._shapeRaf = requestAnimationFrame(rot);'),
    'BJ/BM: bigger shapes, glass faces, and an honest shrink-hold-fly exit');
  // BJ: ЗОЛОТО без щелчков — единая структура фильтров в каждом кадре
  for (const kf of ['coreLive', 'coreBurst', 'coreSettle', 'dotSvgOut', 'dotSvgFly']) {
    const block = css.split('@keyframes ' + kf + '{')[1].split('}}')[0];
    const frames = block.split('{').length - 1;
    const hasFn = (fn) => block.split(fn).length - 1;
    if (kf.indexOf('core') === 0) {
      assert(frames === hasFn('hue-rotate') && frames === hasFn('saturate') &&
        frames === hasFn('brightness') && frames === hasFn('drop-shadow'),
        'BJ: ' + kf + ' keeps one filter list shape in every frame — gold never snaps');
    } else {
      assert(frames === hasFn('brightness') && frames === hasFn('drop-shadow'),
        'BJ: ' + kf + ' keeps one filter list shape in every frame');
    }
  }
  // BJ: математика — обычный корень с носиком, пределы над/под и в строке
  assert(markdown.includes('M.8 13.9 L3.3 16 L5.9 0" fill="none') && !markdown.includes('H11') &&
    markdown.includes("out += (inline ? '<span class=\"mbi-in\">' : '')") &&
    markdown.includes('(?![a-zA-Z])') &&
    css.includes('vertical-align:middle;margin:0 2px;line-height:1.15') &&
    css.includes('.math-inline .mfrac{font-size:.82em;vertical-align:middle') &&
    css.includes('.mbi-in{display:inline-block;vertical-align:middle'),
    'BJ: a clean typographic radical; inline big operators carry limits over/under');
  // BK: таблицы — естественные пропорции закрепляет colgroup (fixTables)
  assert(css.includes('overflow:auto;max-height:62vh') &&
    css.includes('overflow-wrap:anywhere;word-break:break-word') &&
    js.includes('function fixTables(') &&
    js.includes("t.style.tableLayout = 'fixed';"),
    'BJ: tables stop jumping while streaming; long ones scroll in their own window');
  // BJ: график-интерпретатор понимает любую разумную запись
  assert(js.includes('function plotParseSpec(') &&
    js.includes(`.replace(/'/g, '"')`) &&
    js.includes(`replace(/([{,]\\s*)([A-Za-z_]\\w*)\\s*:/g, '$1"$2":')`) &&
    js.includes('const dx = (drag.x - e.clientX) * ux;') &&
    js.includes('const zSrc = Array.isArray(spec.z) ?'),
    'BJ: the plot interpreter forgives single quotes, bare keys and bare formulas');
  // BJ: промпт требует строгий JSON
  assert(pyAgent.includes('СТРОГО в ДВОЙНЫХ') &&
    pyAgent.includes("график-интерпретатор читает только чистый JSON"),
    'BJ: the prompt demands strict double-quoted JSON for plot blocks');
}

function testIterationBLContracts() {
  // BL: УВЕДОМЛЕНИЕ — граница сразу после знака; таблицу не режет
  const lse = js.split('function lastSentenceEnd(')[1].split('\nfunction ')[0];
  assert(lse.includes('(?![0-9A-Za-zА-Яа-яЁё])') &&
    js.includes('function endsInOpenTable(') &&
    !js.split('function toolLine(')[1].split('\nfunction ')[0].includes('endsInOpenTable') &&
    js.split('function renderTyped(')[1].split('\nfunction ')[0].includes('!endsInOpenTable(head)') &&
    js.split('function queueResponseFinish(')[1].split('\nfunction ')[0]
      .includes("ui.marksEl.style.display = '';"),
    'BL: the boundary sits right after the punctuation; an open table blocks the cut and the note lands after it');
  // BL: КРУГЛЕШОК — морф из ЦЕНТРА точки, 1.5× крупнее, 4с, рёбра без щелчков
  const frame = js.split('function dotShapeFrame(')[1].split('\nfunction ')[0];
  assert(css.includes('.dot-shape-svg{position:absolute;left:50%;top:8px;width:46px;height:46px;') &&
    css.includes('.ai-core{position:absolute;left:50%;top:8px;width:15px;height:15px;') &&
    js.includes('const DOT_HOLD_MS = 4000;') &&
    frame.includes('(0.5 + 0.45 * t) * m') &&
    frame.includes('(0.7 + 0.4 * t)') &&
    !frame.includes("back ? 'url(#gB'") &&
    js.split('function dotShapePlay(')[1].split('\nfunction ')[0]
      .includes('if (core._shapeRaf) { schedule(); return; }'),
    'BL: the morph grows from the dot\'s own center, everything 1.5×, 4s hold, edges styled continuously by depth');
  // BL: ТАБЛИЦЫ — колонки доехают плавно, состояние переживает пересборку
  assert(js.includes('const TABLE_COL_ANIM = new Map();') &&
    js.split('function fixTables(')[1].split('\nfunction ')[0].includes('v + d * 0.22') &&
    js.includes('host._tblStamp = ++TABLE_HOST_STAMP;'),
    'BL: column widths chase their new proportions smoothly — like the scroll');
  // BL: СКРОЛЛ — потолок ниже: карточки входят одним куском высоты
  assert(js.split('function chaseBottom(')[1].split('\nfunction ')[0]
      .includes('const target = Math.min(13, Math.max(0.9, gap * 0.13));') &&
    !js.includes('Math.min(24,'),
    'BL: the chase ceiling is gentle (11px/frame) — agent cards no longer glide-jerk');
  // BL: ГРАФИКИ — умный старт, ноль всегда в кадре, плавный ±
  const p2 = js.split('function buildPlot2Panel(')[1].split('\nfunction ')[0];
  assert(p2.includes('const smartInit = () => {') &&
    p2.includes('const featureXs = () => {') &&
    p2.includes('if (a > 0) a = 0;') &&
    p2.includes('const need = (yb - ya) * 1.16 + 0.5;') &&   // BM9: окно растёт под размах кривых
    p2.includes('const xspan = span0 * w * aspect.r / h;') &&
    (js.match(/const ease = \(p\) => p \* p \* \(3 - 2 \* p\);/g) || []).length === 3 &&
    js.includes("mkBtn('+', () => { panel._zoom(1.3); });"),
    'BL: 2D opens on its important points with the origin always in frame; ± zooms animate smoothly (3 panels)');
  // BL: 3D — вращение вокруг центра данных, старт по размеру, жесты по кнопкам
  const p3 = js.split('function buildPlot3Panel(')[1].split('\nfunction ')[0];
  assert(p3.includes('const fitZoom = (w, h) => {') &&
    p3.includes('project(x1 - mcx, y1 - mcy, c00 - mcz)') &&
    p3.includes('lastDist') &&
    p3.includes("mode = 'pan';") && p3.includes("if (e.pointerType === 'touch') {") &&
    p3.includes('zoomBy(dist / lastDist);') &&
    p3.includes('zoomBy(e.deltaY < 0 ? 1.12 : 1 / 1.12);'),
    'BL: 3D rotates around its data center and fits itself into frame; left=rotate, two fingers=pan, wheel/pinch=zoom');
}

function testIterationBKContracts() {
  // BK: ДЖАРВИС ВСЕГДА ОТКРЫВАЕТСЯ С ДОКОМ — сохранённое 'open' не читается
  assert(js.includes("localStorage.removeItem('jarvis.sidebar2');") &&
    !js.includes("localStorage.getItem('jarvis.sidebar2')") &&
    !js.includes("localStorage.setItem('jarvis.sidebar2'"),
    'BK: every launch starts with the dock; the open state never survives a reload');
  // BK: ПЛАН ЕДЕТ ПЛАВНО — вот была разница режимов: пункты плана дёргали
  // ленту мгновенным pinToBottom, теперь тот же разгоняющийся догон
  const reveal = js.split('function revealPlanItems(')[1].split('\nfunction ')[0];
  assert(reveal.includes('chaseBottom(msgHost(), ui);') &&
    !reveal.includes('pinToBottom') &&
    js.split("case 'plan':")[1].split('\n    case ')[0]
      .includes('chaseBottom(stream(), ui);'),
    'BK: the plan scrolls with the same smooth chase as quiet typing — no one-frame jumps');
  // BK: НАСТОЯЩАЯ ТРАНСФОРМАЦИЯ круглешка: вершины вырастают из обода
  const frame = js.split('function dotShapeFrame(')[1].split('\nfunction ')[0];
  assert(js.includes('const DOT_CIRCLE_R = 5.2;') &&
    frame.includes('DOT_CIRCLE_R + (r - DOT_CIRCLE_R) * m') &&
    frame.includes('(1 - m) * .96') &&
    js.includes('const DOT_HOLD_MS = 4000;') &&
    frame.includes('m = 1 - smooth(') &&
    !frame.includes('lastDraw'),
    'BK: the dot truly morphs — vertices grow out of the rim and melt back; 60fps, long hold');
  // BK: УВЕДОМЛЕНИЕ МЕЖДУ ПРЕДЛОЖЕНИЯМИ — граница ждёт точку
  const tl2 = js.split('function toolLine(')[1].split('\nfunction ')[0];
  const rt2 = js.split('function renderTyped(')[1].split('\nfunction ')[0];
  assert(js.includes('function lastSentenceEnd(') &&
    tl2.includes('liveUi.freezePending = true;') &&
    tl2.includes("liveUi.marksEl.style.display = liveUi.freezePending ? 'none' : '';") &&
    rt2.includes('closeMarkSegment(ui);') &&
    rt2.includes('const from = Math.max(src.length, ui.markFrom || 0);') &&
    rt2.includes('const k = lastSentenceEnd(text, from);'),
    'BK: a mid-sentence mode note waits for the sentence to end — never cuts words');
  // BK: КОРЕНЬ — черта и носик совпадают (одна линия 1.4px тем же цветом)
  assert(markdown.includes('M.8 13.9 L3.3 16 L5.9 0" fill="none') && !markdown.includes('H11') &&
    markdown.includes('stroke="rgba(190,235,255,.85)"') &&
    markdown.includes('<span class="msq-r">') &&
    markdown.includes('vector-effect="non-scaling-stroke"') &&
    markdown.includes('viewBox="0 0 6.6 24"') &&
    css.includes('padding-left:.40em') &&
    css.includes('.msqrt .msq-r{display:inline-block;line-height:1.5;min-height:1em;') &&
    css.includes('padding:0 .18em 0 .05em') &&
    css.includes('.msqrt::after{content:\'\';position:absolute;left:.35em;right:0;top:-.7px;') &&
    css.includes('height:1.4px;background:rgba(190,235,255,.85)') &&
    !css.includes('.msq-b') && !css.includes('aspect-ratio:11/24') && !css.includes('border-top:1.4px') &&
    css.includes('.msqrt .msq-i{position:absolute;left:-.24em;width:.34em;text-align:right;'),
    'BM3: the svg is cropped at the tip (no dead width — the radicand sits right behind the nose), the bar starts at the tip, and a wide root index grows LEFT, never onto the stroke');
  // BK: ГРАФИК-ИНТЕРПРЕТАТОР — z(x,y)=, юникод-математика, спасение формул
  assert(js.includes("src.replace(/(^|[^\\w])([a-zA-Z])\\s*\\(([^)]*)\\)\\s*=/g,") &&
    js.includes(".replace(/[·×]/g, '*')") &&
    js.includes('.replace(/−|–|—/g, '-')') &&
    js.includes("if (tk.length === 1) { out.push('x');") &&
    js.includes('const cand = Array.isArray(v) ? v.find((s) => typeof s === \'string\') : v;'),
    'BK: the plot interpreter reads f(x,y)= definitions, unicode math, and salvages formulas');
  // BK: ПАН ПЕРЕЖИВАЕТ ПЕЧАТЬ — пересборка хвоста возвращает pointer capture
  assert((js.match(/panel\._recapture = \(\) =>/g) || []).length === 3 &&
    js.includes('savedPlots.forEach((p) => { if (p._recapture) p._recapture(); });'),
    'BK: dragging a plot survives the streaming tail rebuild — capture is restored');
}

function testIterationBMContracts() {
  // BM: ПОДСКАЗКИ — предзагрузка до того, как юзер их увидит
  const fr = js.split('async function fetchReplies(')[1].split('\nfunction ')[0];
  assert(js.includes('function prefetchReplies()') &&
    js.split('case \'ai_msg\':')[1].split('\n    case ')[0].includes('prefetchReplies();') &&
    !js.split('function queueResponseFinish(')[1].split('\nfunction ')[0].includes('prefetchReplies();') &&
    fr.includes('S.replyPrefetch') &&
    fr.includes("if (S.streaming) return;") &&
    pyServer.includes('_reply_job_result('),
    'BM3: replies are prefetched at ai_msg (AFTER the save, not the racy done) and the server never runs two suggestion jobs for one answer');
  // BM: ГРАФИК ПО-ЧЕЛОВЕЧЕСКИ — данные это данные, окно по точкам, аспект «авто»
  const p2 = js.split('function buildPlot2Panel(')[1].split('\nfunction ')[0];
  assert(js.includes('function plotParseSeries(') &&
    p2.includes("let aspect = (!fns.length && series.length) ? ASPECTS[5] : ASPECTS[0];") &&
    p2.includes('series.forEach((s) => s.pts.forEach((p) => {') &&
    p2.includes('let da = Infinity, db = -Infinity;'),
    'BM: «график температуры и ветра» works — data series drive the window and the aspect');
  // BM: КАЖДАЯ МЕТКА НА СВОЁМ МЕСТЕ — сегменты печати
  const rt = js.split('function renderTyped(')[1].split('\nfunction ')[0];
  assert(js.includes('function closeMarkSegment(ui)') &&
    rt.includes('closeMarkSegment(ui);') &&
    rt.includes('segs.forEach((sg) => { want.push(sg.frozenEl, sg.marksEl); });') &&
    js.split('function toolLine(')[1].split('\nfunction ')[0]
      .includes("liveUi.marksEl.style.display = liveUi.freezePending ? 'none' : '';"),
    'BM: each mode mark keeps its own segment — a later enable/disable never drags it away');
  // BM2: отвеченность живёт В ТЕКСТЕ сообщения (```ui-sent), а не в наборах id
  const rm = js.split('function renderMessages(')[1].split('\nfunction ')[0];
  assert(rm.includes("m.role === 'assistant' && m.id === lastAiId") && !rm.includes('answered.has') &&
    js.includes("const sent = box.classList.contains('ui-sent');") &&
    js.includes("if (inert || sent) box.classList.add('ui-inert');") &&
    markdown.includes('ui-sent') &&
    pyAgent.includes('def mark_answered_fences(') &&
    pyAgent.includes('def suppress_repeated_panels(') &&
    pyServer.includes('agent.mark_answered_fences(') &&
    pyServer.includes('db.update_message_content(target["id"], marked)') &&
    pyServer.includes('suppress_repeated_panels('),
    'BM2: an answered interactive stays dead across re-renders and restarts — the answer marks the ```ui-sent fence right in the message text');
  // BM: \ge/\le → ≥/≤, степени не теряются
  const bctx = { window: {} };
  vm.createContext(bctx);
  vm.runInContext(markdown, bctx);
  const bmr = bctx.window.MD.render;
  const geOut = bmr('\\[ b^2 \\ge 0 \\]');
  assert(geOut.includes('≥') && geOut.includes('msup') && geOut.includes('>2<') &&
    bmr('\\[ a \\le b \\]').includes('≤') &&
    markdown.includes("le:'\\u2264', ge:'\\u2265',"),
    'BM: ge/le render as real ≥/≤ symbols; superscripts survive');
  // BM3: КОРЕНЬ — svg-оверлей фиксированной ширины .40em, содержимое в потоке
  assert(css.includes('.msqrt{position:relative;display:inline-block;line-height:0;') &&
    css.includes('.msqrt .msq-svg{position:absolute;left:0;top:0;width:.40em;height:100%;'),
    'BM3: the radical sign is a fixed .40em overlay (cropped at the tip) — stable at any content size');
  // BM3: отношения и операции — с воздухом; «>=» — один знак
  assert(bmr('\\[ a >= n \\]').includes('≥') && !bmr('\\[ a >= n \\]').includes('&gt;') &&
    bmr('\\[ x <= y \\]').includes('≤') &&
    bmr('\\[ a \\times b \\]').includes('<span class="mop">\u00d7</span>'),
    'BM3: bare >=/<= fuse into one ≥/≤; operators like × get real air');
}

function testIterationAOContracts() {
  // AO: РАБОЧАЯ ОБЛАСТЬ ЖИВЁТ В ТАКТ ДОКУ — та же кривая дока в каждую
  // сторону, отступ вида скользит, а не прыгает
  assert(css.includes('.main{grid-column:1;margin-left:262px;') &&
    css.includes('transition:margin-left .6s cubic-bezier(.22,.68,.18,1)}') &&
    css.includes('.app.collapsed .main{margin-left:0;') &&
    css.includes('transition:margin-left .6s cubic-bezier(.5,.35,.15,1)}') &&
    css.includes('.main .view{transition:padding-left .6s cubic-bezier(.22,.68,.18,1)}') &&
    css.includes('transition:width .6s cubic-bezier(.22,.68,.18,1)') &&
    css.includes('transition:width .6s cubic-bezier(.5,.35,.15,1)'),
    'AO/AP: work area slides with the DOCK curve in both directions (margin, Safari-proof)');
  // AO: ВОЛНА СИГИЛА — печать развязывает фигуру в линию волн (жёлтая,
  // яркий пульс), конец печати медленно замеряет её в последней позе
  assert(css.includes('transition:opacity .18s ease}') &&
    !css.includes('arriveCore') && !css.includes('arriveName') &&
    js.includes('fade.onfinish = () => g.remove();') &&
    css.includes('.fly-ghost{position:fixed;z-index:400;pointer-events:none;margin:0'),
    'AO/AV: ghosts land with a soft crossfade — no flash, no jerk');
  const typingTick = js.split("ui.mdEl.classList.add('typing');")[1].slice(0, 300);
  assert(!typingTick.includes('flyWelcomeInto') && !js.includes('relayTyping'),
    'AO/AS: typing drives nothing — the dot livenes is pure CSS');
  // AO: ТУМБЛЕР — клики не блокируются, двойная пересадка защищена
  assert(!css.includes('.agent-switch.ag-switching .agent-switch-track{pointer-events:none}') &&
    extractFunction(js, 'agentPaint').includes('agentPaint._busy') &&
    js.includes('agentPaint._busy = false'),
    'AO: no click blackout on the toggle, repaint guarded against double-fire');
}

function testIterationBM6Contracts() {
  // BM6: НАСТРОЙКИ ПРОВАЙДЕРОВ ДИНАМИЧЕСКИЕ — любой провайдер из конфига
  // получает поле ключа сам; Яндекс ещё и folder_id; роль задаёт порядок
  const rs = js.split('function renderSettings(')[1].split('\nfunction ')[0];
  assert(rs.includes('const provs = Object.entries(p).sort((a, b) =>') &&
    rs.includes("Number((a[1] || {}).priority)") &&
    rs.includes("id=\"k_' + name") &&
    rs.includes("'folder_id' in pc") &&
    rs.includes("id=\"fid_' + name") &&
    rs.includes("id=\"r_' + name") &&
    rs.includes('ROLE_OPTS') &&
    !rs.includes('kCloud'),
    'BM6: provider settings come from the config itself — key, folder_id and role per provider, nothing hardcoded');
  // BM6: «ПРОВЕРИТЬ ПРОВАЙДЕРОВ» — точный снимок зонда, а не только каталог
  // BM7: снимок вынесен в showProvidersState — им же живёт чип в верхней панели
  const sps = extractFunction(js, 'showProvidersState');
  assert(js.includes("api('/api/providers')") &&
    sps.includes("'жив — зонд '") &&
    sps.includes("'НЕ ОТВЕЧАЕТ — обхожу запасным'") &&
    sps.includes("'зонд падает…'") &&
    sps.includes('med_ttft_s') && sps.includes('med_cps') &&
    sps.includes('gen_ttft_s'),
    'BM6: the probe button shows the exact state — alive with latency, dead and bypassed, or failing probes');
  // BM7: КОМПАКТНЫЙ АККОРДЕОН — строка на провайдера, клик разворачивает
  assert(rs.includes('prov-row') && rs.includes('prov-head') &&
    rs.includes('prov-fields') && rs.includes("row.classList.toggle('open')") &&
    rs.includes('refreshProvRows(prov)'),
    'BM7: provider settings collapsed to one row each — click expands keys, live probe state in the row');
  // BM7: провайдер в шапке ответа — между режимом и моделью
  const urm = extractFunction(js, 'updateResponseMeta');
  assert(urm.includes('ui.providerName') && urm.includes('if (prov) parts.push(prov);'),
    'BM7: the answer passport shows WHO answered — provider between mode and model');
  // BM7: чип в верхней панели — кто отвечает, клик открывает снимок
  assert(html.includes('id="chipProv"') &&
    js.includes("function setProvChip(name)") &&
    js.includes("$('#chipProv').addEventListener('click', () => showProvidersState());") &&
    css.includes('#chipProv'),
    'BM7: top bar carries a live provider chip — click opens the exact state of everyone');
  // BM6: онбординг больше не привязан к cloudru
  assert(js.includes("Object.values(S.config.providers || {}).some((x) => (x || {}).has_key)"),
    'BM6: the «insert a key» toast fires only when NO provider has a key');
  // BM6: сервер отдаёт снимок и запускает дозор
  assert(pyServer.includes('"/api/providers"') &&
    pyServer.includes('llm.providers_status()') &&
    pyServer.includes('llm.start_prober()'),
    'BM6: /api/providers endpoint + background prober started with the server');
  // BM6: распознавание — зонд, мёртвый обходит очередь, приоритеты
  assert(pyLlm.includes('def probe_provider(') &&
    pyLlm.includes('def _probe_dead(') &&
    pyLlm.includes('def start_prober(') &&
    pyLlm.includes('def providers_status(') &&
    pyLlm.includes('def provider_headers(') &&
    pyLlm.includes('def _gigachat_token(') &&
    pyLlm.includes('penalty += 10') &&
    pyLlm.includes('def active_providers()'),
    'BM6: probes mark a provider dead (2 fails) and it is skipped at once; any provider joins by config priority');
  // BM6: картинки через Яндекс + статус-строка с именем провайдера из конфига
  assert(pyLlm.includes('def provider_display(') &&
    pyAgent.includes('llm.provider_display('),
    'BM6: the switch status line names the provider from its config label');
}

function testIterationBM8Contracts() {
  // BM8: ЧИП-ОГОНЁК — роль ответившего, имя в подсказке, «никто» мигает
  const spc = extractFunction(js, 'setProvChip');
  assert(spc.includes("? (PROV_SHORT[S.lastProvider] || S.lastProvider) : '—';"),
    'BM8/BM11: the chip carries the provider NAME alone («cloud.ru») — the light carries the role');
  assert(spc.includes("cur.order === 0 ? 'ok'") &&
    spc.includes("cur.order === 1 ? 'warn'") &&
    spc.includes("'err live'"),
    'BM8: green = primary, yellow = first backup, red = far backup, blinking red = nobody alive');
  assert(spc.includes('title = ') && spc.includes('PROV_SHORT'),
    'BM8: the exact provider name lives in the chip tooltip');
  // BM8: провайдер в паспорте ответа — доменной меткой через точку
  assert(js.includes("cloudru: 'cloud.ru'"),
    'BM8: the answer passport names the provider as cloud.ru');
  // BM8: ГЛУБОКАЯ ПРОВЕРКА — ген-зонд всех по кнопке
  assert(js.includes("api('/api/providers?deep=1')") &&
    js.includes('showProvidersState(true)'),
    'BM8: the button runs a generation probe of every provider at once');
  // BM8: ГРАНИЦЫ ИНСТРУМЕНТА НЕ ГАСНУТ после первого переполнения
  const glide = extractFunction(js, 'glideFlow');
  assert(!glide.includes("classList.remove('full')"),
    'BM8: the flow-edge darkening never lifts once a line has overflowed');
  const feed = extractFunction(js, 'qtFeed');
  assert(feed.includes("flow.classList.contains('full') && inner.scrollHeight"),
    'BM8: new lines restart the glide while the mask stays on');
  // BM8: РАСХОД ЗА СЕГОДНЯ — от местной полуночи устройства
  assert(html.includes('Расход сегодня') && !html.includes('Расход 24ч') &&
    js.includes("day_start=' + dayStart") &&
    js.includes('getFullYear(), d.getMonth(), d.getDate()'),
    'BM8: sidebar shows today\'s spend from the device\'s local midnight');
  // BM8: ген-зонд меряет РАБОЧУЮ модель, темп бережливый
  assert(pyLlm.includes('pick_model("base", prov)') &&
    pyLlm.includes('_GEN_PROBE_GAP_S = 300.0') &&
    pyLlm.includes('_GEN_PROBE_GAP_HOT_S = 120.0') &&
    pyLlm.includes('def deep_probe_all('),
    'BM8: the generation probe measures the base (workhorse) model; 5 min when healthy, 2 min under suspicion');
  // BM8: улучшатель промпта картинки — жёсткие 12 секунд
  const pyMedia = fs.readFileSync(path.join(root, 'app/jarvis/tools/media.py'), 'utf8');
  assert(pyMedia.includes('temperature=0.45, timeout=12'),
    'BM8: prompt enhancement cannot stall image generation (12s hard cap)');
}

function testIterationBM9Contracts() {
  // BM9: провайдер в паспорте — доменной меткой (cloudru → cloud.ru)
  const urm = extractFunction(js, 'updateResponseMeta');
  assert(urm.includes('PROV_SHORT[provRaw] || provRaw'),
    'BM9: the answer passport maps the provider id to its domain label');
  // BM11: чип — только имя провайдера (слово «провайдер» убрано)
  assert(!html.includes('id="chipModel"') &&
    js.includes("? (PROV_SHORT[S.lastProvider] || S.lastProvider) : '—';") &&
    !js.includes("'провайдер: '"),
    'BM11: the top bar chip carries the provider name alone — the word «провайдер» is gone');
  // BM9: агент не перекрашивает огоньки панели
  assert(!css.includes('body.agent-on .status-chip .dot{background:#c4788b}'),
    'BM9: agent mode no longer recolors the status lights');
  // BM9: скролл плывёт — мягкий разгон и пологая доля остатка
  const chase = extractFunction(js, 'chaseBottom');
  assert(chase.includes('Math.min(13, Math.max(0.9, gap * 0.13))') &&
    chase.includes('(st.chaseV || 0) + 1.4'),
    'BM10: glide scroll — soft ramp, ceiling 13px/frame, always faster than the printer');
  // BM10: папки анимирует JS-вальс (Web Animations + rAF высота);
  // CSS-переход(grid) конкурировал с ним и давал резкий кадр в конце
  assert(css.includes('.qt-folder .qt-kids{display:none}') &&
    !css.includes('grid-template-rows:0fr') &&
    js.includes("kids.style.height = (wasOpen ? H : 0) + 'px';"),
    'BM10: folders animate via the JS waltz alone — no CSS transition fighting it at the end');
  // BM10: степень корня — ниже и левее: не залезает за черту корня;
  // корень в ЗНАМЕНАТЕЛЕ дроби опущен (не наезжает на знак дроби)
  assert(css.includes('.msqrt .msq-i{position:absolute;left:-.24em;width:.34em') &&
    css.includes('top:-.44em') && css.includes('.mfr-d .msqrt{margin-top:.22em}'),
    'BM11: the root degree sits lower still and slightly more left — never touching the bar; a root in the denominator drops below the fraction bar');
  // BM9: 3D — ЛКМ вращает, ПКМ пан; короткий ПКМ — обычное меню
  const p3 = js.split('function buildPlot3Panel(')[1].split('\nfunction ')[0];
  assert(p3.includes("(e.button === 0 || e.button === 1) ? 'rot' : (e.button === 2 ? 'pan' : null)"),
    'BM9: LMB-drag rotates, RMB-drag pans — as the person asked');
  const shell = extractFunction(js, 'plotShell');
  assert(/contextmenu[\s\S]*?e\.preventDefault\(\);[\s\S]*?\}\);/.test(shell),
    'BM10: the plot surface never shows the native menu — pan and menu can never collide (macOS fires the menu on mousedown, deferring it is impossible)');
  // BM9: график при печати — загрузка, ошибка только в финале
  assert(js.includes("panel.closest('.msg-ai.live')") &&
    js.includes('plot-load') && js.includes('строю график…'),
    'BM9: a plot being typed shows a spinner — the error only appears in the final render');
  // BM9: окно графика растёт под весь размах кривых
  assert(js.includes('const need = (yb - ya) * 1.16 + 0.5;') &&
    js.includes('const xspan = span0 * w * aspect.r / h;'),
    'BM9: both curves and their intersection fit the first frame — the window grows, aspect stays honest');
  // BM9: подсказки — починенный промпт (без дубля-мусора) и запрет голых оценок
  assert(!pyAgent.includes('с глаголом. "\n                        "примере»'),
    'BM9: the suggestion prompt no longer carries the duplicated garbage fragment');
  assert(pyAgent.includes('стоит посмотреть'),
    'BM9: bare evaluative chips like «стоит посмотреть» are banned in the prompt');
}

function testIterationBM10Contracts() {
  // BM10/BM11: КАРКАС ПРОСТРАНСТВ (идея из Arc) — LIVE + мини-иконки
  assert(html.includes('id="spacesBar"') &&
    !html.includes('id="spModeChat"') && html.includes('id="spModeLive"') &&
    html.includes('data-space="chat"') && html.includes('data-space="math"') &&
    html.includes('data-space="music"') && html.includes('id="view-space"'),
    'BM11: the spaces bar keeps LIVE alone — the big CHAT button is gone, chat lives among the mini-icons');
  // переключение кликом И свайпом двумя пальцами; старт всегда в CHAT
  assert(js.includes("const SPACES = ['chat', 'math', 'music'];") &&
    js.includes('function setSpace(name, dir)') &&
    js.includes('function initSpaces()') &&
    js.includes("window.addEventListener('wheel'") &&
    js.includes('Math.abs(e.deltaX) < 38') &&
    js.includes("spaceApply('chat');"),
    'BM11: spaces switch by tap or a fast two-finger swipe; the app always boots into CHAT');
  // под чертой — вкладки пространства; чат — базовое: заполнен и синий
  assert(js.includes("$$('.nav, .chats-block').forEach((n) => n.classList.toggle('space-off', !isChat));") &&
    css.includes('.sp-ico.base{color:var(--cy2)}') &&
    !css.includes('.sp-ico.sel::after') &&
    css.includes('.sp-mode.active{'),
    'BM11: below the line the tabs belong to the space; the chat icon is the filled bright-blue base, the selected one glows with no underline');
  // BM10: МЕДИА ИЗ ИНТЕРНЕТА — нативные плееры в ответе
  const pyMedia = fs.readFileSync(path.join(root, 'app/jarvis/tools/media.py'), 'utf8');
  assert(pyMedia.includes('def show_media(url: str)') &&
    pyMedia.includes('_YOUTUBE_RE') &&
    pyMedia.includes('RuTube/VK Видео'),
    'BM10: the show_media tool fetches direct media and honestly refuses YouTube (blocked in RF)');
  const afc = extractFunction(js, 'attachFileChip');
  assert(afc.includes("v.autoplay = true; v.muted = true;") &&
    afc.includes('f.kind === \'audio\'') &&
    css.includes('.video-out{'),
    'BM10: video plays inline at once (muted by default), audio gets a native player');
  // BM10: подсказки — обращения к JARVIS, бюджет 14с
  assert(pyAgent.includes('обращение ') &&
    pyAgent.includes('могу выбрать ') &&
    pyAgent.includes('timeout=14, operation="reply_suggestions_ai"'),
    'BM10: every suggestion chip addresses JARVIS (ask/request); a slow provider no longer kills them at 9s');
  // BM10: интерактивы не запрещены — только повтор уже отвеченной панели
  assert(!pyAgent.includes('НЕ показывай ui для') &&
    pyAgent.includes('НЕ выставляй панель'),
    'BM10: interactives are never banned — the only ban is repeating an already-answered panel');
  // BM11: кнопка лимита — под ускорением, радиус как у тумблера
  assert(css.includes('.budget-btn{position:relative;width:30px;height:28px;border-radius:20px;') &&
    css.includes('margin-right:51px}') &&
    html.indexOf('id="swAgent"') < html.indexOf('id="tgBudget"'),
    'BM11: the budget button is the rightmost control (after AGENT), pill-radius like the toggles, right edge exactly beneath the boost button');
}

function testIterationBM11Contracts() {
  // ДОК: пространств в доке нет — вкладки как раньше + LIVE + текущее
  assert(html.includes('id="spDock"') && html.includes('id="spdLive"') &&
    html.includes('id="spdCur"') && html.includes('id="spdFly"') &&
    css.includes('.app.collapsed .spaces{display:none}') &&
    css.includes('.app.collapsed .sp-dock{display:flex;flex-direction:column;align-items:center;gap:5px;'),
    'BM11: the dock stays as it was — tabs plus a LIVE icon and the current-space icon with a flyout');
  // выплывающая панель: наведение плавно выплывает вправо
  assert(css.includes('.spd-cur-wrap:hover .spd-fly,.spd-fly:hover{opacity:1;pointer-events:auto;') &&
    css.includes('left:calc(100% + 14px)'),
    'BM11: hovering the current-space icon slides a small panel out to the right');
  // настройки — с правого края иконок (в ряду и в выпадающей панели)
  assert(html.includes('id="spSettings"') &&
    css.includes('.sp-ico.sp-set{margin-left:auto') &&
    html.includes('data-view="settings" data-tip="настройки"'),
    'BM11: a settings gear sits at the right edge of the space icons — in the bar and in the flyout');
  // ГЛАЙДЕР: подсветка морфом перетекает на выбранную иконку
  assert(html.includes('id="spGlider"') &&
    js.includes('function spaceGlider()') &&
    css.includes('.sp-glider{position:absolute;top:0;height:34px;border-radius:10px;') &&
    css.includes('transition:left .34s cubic-bezier(.3,1.35,.42,1),width .34s cubic-bezier(.3,1.35,.42,1),'),
    'BM11: the highlight glider liquidly morphs onto the newly selected space icon');
  // анимация направленная: старое уплывает в сторону движения, новое — с противоположной
  assert(js.includes("translateX(' + (-56 * way) + 'px)'") &&
    js.includes("translateX(' + (56 * way) + 'px)'"),
    'BM11: old space exits toward the swipe direction, the new one enters from the opposite side');
  // свайп быстрее: порог 38, пауза 450
  assert(js.includes('Math.abs(e.deltaX) < 38 || Math.abs(e.deltaX) < Math.abs(e.deltaY) * 1.35') &&
    js.includes('if (now - lastSwipe < 450) return;'),
    'BM11: the swipe reacts faster — lower threshold, shorter pause between swipes');
  // ЧИПЫ: универсальный запас вместо шаблона; ожидание покрывает бюджет
  assert(pyAgent.includes('Уточни главное') &&
    pyAgent.includes('Предложи варианты развития') &&
    pyAgent.includes('Как это применить?') &&
    !pyAgent.includes('Расскажи подробнее'),
    'BM11: the local chip fallback is universal (clarify / develop / apply) — fit for any answer, no dead template');
  const fr = extractFunction(js, 'fetchReplies');
  assert(fr.includes('}, 18000);') && !fr.includes('}, 15000);'),
    'BM11: the chip skeleton outlives the whole nano budget (14s) — chips never vanish on a slow provider');
  // МЕДИА: модель знает про show_media и не отказывается
  assert(pyAgent.includes('show_media — картинка, аудио или видео из интернета') &&
    pyAgent.includes('НИКОГДА не говори «не могу передать') &&
    pyAgent.includes('RuTube,\n   VK Видео или прямую ссылку'),
    'BM11: the tool list carries show_media and the prompt forbids the old «cannot transfer audio/video» excuse');
  // ГРАФИК: пустая плоскость запрещена — окно по фактическим точкам
  assert(js.includes('panel._emptyGuard') &&
    js.includes('ДАННЫЕ ВАЖНЕЕ диапазона из спеки') &&
    js.includes('ПУСТАЯ ПЛОСКОСТЬ ЗАПРЕЩЕНА'),
    'BM11: a data plot aims its window at the real points, and an all-NaN formula says so honestly instead of an empty plane');
  // МИНИ-ВКЛАДКИ: фенс + карточка с мини-интерфейсом
  assert(markdown.includes("lang === 'embed'") &&
    markdown.includes('class="embed-panel" data-embed=') &&
    js.includes('function mountEmbedPanels(') &&
    js.includes('const EMBED_VIEWS = {') &&
    js.includes('function buildEmbedPanel(panel, spec)') &&
    css.includes('.embed-card{margin:14px 0;max-width:440px;border-radius:13px;'),
    'BM11: a ```embed fence renders a thin-frame mini-tab card (AUTO/Файлы/Память/Сценарии) with limited interaction');
  assert(pyAgent.includes('МИНИ-ВКЛАДКИ') &&
    pyAgent.includes('```embed'),
    'BM11: the agent prompt knows the embed block — mini-tabs appear when the answer discusses tasks/files/memory');
}

function testIterationAXContracts() {
  // AX: скоординированный уход — всё одной длительности 620мс
  assert(js.includes('function welcomeExit(') &&
    js.split('function welcomeExit(')[1].split('\nfunction ')[0]
      .includes('transform .62s cubic-bezier(.65,0,.35,1), opacity .62s cubic-bezier(.65,0,.35,1)') &&
    js.split('function flyGhost(')[1].split('\nfunction ')[0].includes('const dur = 620;'),
    'AX: tiles scatter, welcome fades and ghosts fly — all in one 620ms beat');
  // AX: морф В ПОЛЁТЕ — кольца тают, цвет надписи перетекает
  assert(js.includes('rings.forEach((r) => { r.style.opacity = String(Math.max(0, 1 - e)); });') &&
    js.includes("'<span class=\"gt-grad\">JARVIS</span><span class=\"gt-solid\">JARVIS</span>'") &&
    js.includes('(p - .35) / .45'),
    'AX/AY: the core sheds its rings mid-flight (at flight speed); the title morphs mid-flight');
  // AX/BD: круглешок — такт БЕЗ размера (цвет/мерцание), золото во всплеске
  const live = css.split('@keyframes coreLive{')[1].split('}}')[0];
  const burst = css.split('@keyframes coreBurst{')[1].split('}}')[0];
  assert(!live.includes('transform') && live.includes('#ffd8a8') &&
    burst.includes('#ffd489') &&
    js.includes('function dotShapeFrame('),
    'AX/BD: the dot bursts gold; the idle beat changes color, never size');
  // AX: плитки фиксированной высоты, описание — три строки с троеточием
  const sp = css.split('.sugg .sp{')[1].split('}')[0];
  assert(sp.includes('-webkit-line-clamp:5') &&
    js.includes("'</span></b><span class=\"sp\">' + esc(s.desc || s.prompt) + '</span>'"),
    'AX/AZ: fixed-height tiles show title+description; overflow ends with an ellipsis');
}

function testIterationAYContracts() {
  // AY: ЕДИНАЯ анимация ухода — killWelcome не режет уходящее приветствие,
  // входная popIn снята, страница тает и складывается по высоте
  assert(js.includes("w.dataset.exit = '1';") &&
    js.includes("w.dataset.exit !== '1'") &&
    js.split('function welcomeExit(')[1].split('\nfunction ')[0]
      .includes("t.style.animation = 'none';") &&
    js.split('function welcomeExit(')[1].split('\nfunction ')[0]
      .includes("w.style.height = '0px';"),
    'AY: the welcome leaves as ONE motion — tiles scatter, page fades and folds');
  // AY/BB: киношное размытие — ступенька через класс, transition ведёт браузер
  const fly = js.split('function flyGhost(')[1].split('\nfunction ')[0];
  assert(fly.includes("g.classList.toggle('motion', moving);") &&
    css.includes('.fly-ghost.motion{filter:blur(1.4px)}'),
    'AY/BB: cinematic motion blur as a stepped class — no per-frame filter writes');
  // AY: круглешок живёт весь ответ и в финале оседает кружком
  assert(js.includes('function finishLiveDot(') &&
    js.includes("node.root.classList.add('live');") &&
    css.includes('animation:coreLive 4.6s ease-in-out infinite}') &&
    css.includes('.ai-core.dot-settle{animation:coreSettle .55s cubic-bezier(.65,0,.35,1) forwards}') &&
    !css.includes(':has(.typing) .ai-core'),
    'AY: the dot lives through thinking, tools and typing, then settles into a circle');
  // AY: плитки компактнее, троеточие — по-человечески (после знака — пробел)
  const fit = js.split('function fitSuggText(')[1].split('\nfunction ')[0];
  assert(css.split('.sugg{')[1].split('}')[0].includes('font-size:12.5px') &&
    fit.includes('sp.dataset.full') && fit.includes("'\\u00A0…'"),
    'AY: compact text-forward tiles; ellipsis after punctuation goes after a space');
  // AY: ИИ ещё придумывает плитки — клиент заберёт живые повторным заходом
  assert(js.includes('if (r.refreshing && !again) setTimeout(() => { loadIdeas(true); }, 2600);'),
    'AY: welcome tiles: local set instantly, AI set swaps in when ready');
}

function testIterationAZContracts() {
  // AZ: подсказки принадлежат СВОЕМУ диалогу — смена диалога гасит полосу
  const newChat = js.split('function newChat(')[1].split('\nfunction ')[0];
  const openChat = js.split('async function openChat(')[1].split('\nfunction ')[0];
  assert(newChat.includes("S.replyTicket = (S.replyTicket || 0) + 1;") &&
    openChat.includes("rb.hidden = true; rb.innerHTML = '';") &&
    js.split('async function fetchReplies(')[1].split('\nfunction ')[0]
      .includes('if (activeChatId() !== chat) { showReplies([]); return; }'),
    'AZ: reply chips are per-chat — switching chats clears them at once');
  // AZ/BA/BD: спокойная база 4.6s, всплеск только на видимых действиях
  assert(css.includes('.msg-ai.live .ai-core.dot-act{animation:coreBurst 1.15s') &&
    css.includes('@keyframes coreBurst{') &&
    js.includes('function dotAction(') &&
    js.split("case 'tool_start': {")[1].split("case '")[0]
      .includes('dotAction(ui.node && ui.node.root);'),
    'AZ: calm dot at rest; energized burst only on visible actions (tool, plan step)');
  // AZ/BE/BG: игры с формой — 11 понятных фигур, качественные SVG
  assert(js.includes('function dotShapePlay(') &&
    js.includes('const DOT_SHAPE_KEYS = Object.keys(DOT_SHAPES);') &&
    js.includes('<radialGradient id="gF') &&
    ['tess', 'penta', 'cube', 'octa', 'tetra', 'crystal']
      .every((k) => js.includes('  ' + k + ': {')) &&
    ['star', 'hex', 'cross', 'line', 'wave', 'zig']
      .every((k) => !js.includes('  ' + k + ': {')) &&
    css.includes('.dot-shape-svg{') && css.includes('width:46px;height:46px;') &&
    !css.includes('clip-path:polygon('),
    'AZ: the dot occasionally plays with its shape — tesseract, cube, crystal, star');
  // AZ: плитка = название + ОПИСАНИЕ, промпт — только по клику
  assert(js.includes('desc: s[1], prompt: s[2]') &&
    js.includes('esc(s.desc || s.prompt)'),
    'AZ: tiles show title and description; the prompt itself waits for the click');
}

function testIterationBAContracts() {
  // BA: классы круглешка — на САМОМ .ai-core; коллизия .act устранена
  const act = js.split('function dotAction(')[1].split('\nfunction ')[0];
  assert(act.includes("root.querySelector('.ai-core')") &&
    act.includes("core.classList.add('dot-act');") &&
    !js.includes("root.classList.add('act')"),
    'BA: dot classes live on the core itself; the .act collision is gone for good');
  // BD: предпрогрев УДАЛЁН — hover больше не делает скрытых LLM-вызовов
  assert(!js.includes('warmRequest') && !js.includes('warmHover') &&
    !js.includes('pointerenter') && !pyServer.includes('/api/warm'),
    'BA/BD: hover warmup is gone for good — no silent LLM calls on hover');
}

function testIterationBBContracts() {
  // BB: ИИ-плитки готовы ДО первого экрана; сервер греет их при старте
  const init = js.split('(async function init()')[1].split('\n})();')[0];
  assert(init.indexOf('await loadIdeas();') > -1 &&
    init.indexOf('await loadIdeas();') < init.indexOf('buildWelcome()'),
    'BB: tiles are loaded BEFORE the welcome screen is built');
  // BC/BD: круглешок — фигуры ×2.1, пружина с золотом, спокойный ритм
  const play = js.split('function dotShapePlay(')[1].split('\nfunction ')[0];
  assert(js.includes('const DOT_MORPH_MS = 700;') &&
    js.includes('const DOT_HOLD_MS = 4000;') &&
    play.includes("g.innerHTML = dotShapeFrame(key, t, svg._gradL);") &&
    play.includes("core.classList.add('shape-on');") &&
    play.includes('3500 + Math.random() * 4500') &&
    css.includes('.dot-shape-svg.sh-out{animation:dotSvgOut .4s') &&
    css.includes('.dot-shape-svg.sh-fly{animation:dotSvgFly .5s') &&
    css.includes('@keyframes dotSvgOut{') && css.includes('@keyframes dotSvgFly{') &&
    css.includes('width:46px;height:46px;') &&
    css.split('@keyframes dotSvgFly{')[1].split('}}')[0]
      .includes('translateY(-42px) scale(.22)'),
    'BC/BD/BM: the dot morphs into a real SVG shape — honest geometry, no fade-in entrance');
  // BC: рамка-вспышка УДАЛЕНА
  assert(!css.includes('flash-done') && !js.includes('flash-done') &&
    !css.includes('doneFlash'),
    'BC: the done-frame flash is gone for good');
}

function testIterationBCContracts() {
  // BC: рамка удалена; история статична; прогрев не на кнопке отправки
  assert(!css.includes('flash-done') && !css.includes('doneFlash') &&
    !css.includes('coreBreathe') &&
    !js.includes('pointerenter'),
    'BC/BD: no done frame; history cores static; hover warmup gone entirely');
  // BC/BD/BG: фигуры ×2.1 — SVG с пружиной; золото ПЛАВНО в кадрах морфа
  assert(css.includes('.dot-shape-svg.sh-fly{animation:dotSvgFly .5s') &&
    css.includes('width:46px;height:46px;') &&
    css.includes('100%{opacity:0;transform:translateY(-42px) scale(.22);') &&
    !css.includes('.dot-shape-svg.sh-in') &&
    css.includes('transition:opacity .45s ease') &&
    js.includes('<radialGradient id="gF') && js.includes('<linearGradient id="gE'),
    'BC/BD: shapes are big 40px SVGs with a soft gold-lit morph and a dot crossfade');
}

function testIterationBDContracts() {
  // BD: прогрев убран ПОЛНОСТЬЮ — ни /api/warm на сервере, ни hover-прогрева
  assert(!pyServer.includes('/api/warm') && !pyServer.includes('_WARM_SEEN') &&
    !js.includes('warmRequest') && !js.includes('pointerenter'),
    'BD: warmup is completely gone — server and client');
  // BE: пустой [] — СБОЙ, а не «чипов нет»: подсказки всегда три
  const sra = pyAgent.split('def suggest_replies_ai(')[1].split('\ndef ')[0];
  assert(!sra.includes('span.finish("empty")') && !sra.includes('return []') &&
    sra.includes('верни ровно ТРИ ') && sra.includes('живые реплики ВСЕГДА'),
    'BE: an empty [] is a FAILURE — chips are always three (local fallback)');
  // BD: шаблонная тройка не кэшируется в meta
  assert((pyServer.match(/items != agent\.suggest_replies\(/g) || []).length === 2,
    'BD: template replies are never cached into chat meta');
  // BD: спокойный круглешок — в покое БЕЗ размера, фигура ×2.1 на 1.5с
  const coreLive = css.split('@keyframes coreLive{')[1].split('}}')[0];
  assert(!coreLive.includes('transform') && coreLive.includes('#ffd8a8') &&
    !coreLive.includes('background') && coreLive.includes('hue-rotate') &&
    css.includes('animation:coreLive 4.6s ease-in-out infinite}') &&
    js.includes('const DOT_MORPH_MS = 700;') &&
    js.includes('const DOT_MORPH_OUT_MS = 650;') &&
    js.includes('const DOT_HOLD_MS = 4000;') &&
    js.includes('const DOT_CIRCLE_R = 5.2;'),
    'BD: calm dot at rest (no size change) — gold shimmer only');
  // BD: история дешёвая — базовое ядро без полигонов и фильтров
  const baseCore = css.split('.ai-core{')[1].split('}')[0];
  assert(!baseCore.includes('animation:') && !baseCore.includes('clip-path') &&
    !baseCore.includes('filter') && baseCore.includes('box-shadow:') &&
    css.split('.msg-ai.live .ai-core{')[1].split('}')[0].includes('filter:drop-shadow(') &&
    css.includes('.msg-ai.live .ai-core.shape-on{opacity:0}'),
    'BD: history cores are cheap — all morph magic lives on .live only');
  // BD: круглешок реагирует и на файлы
  assert(js.split("case 'file': {")[1].split("case '")[0]
      .includes('dotAction(ui.node && ui.node.root);'),
    'BD: the dot pulses on file events too');
}

function testIterationBEContracts() {
  // BE: подсказки ВСЕГДА — приветствие тоже получает три (локальный запас)
  const sra2 = pyAgent.split('def suggest_replies_ai(')[1].split('\ndef ')[0];
  assert(sra2.includes('верни ровно ТРИ ') && sra2.includes('живые реплики ВСЕГДА') &&
    !sra2.includes('return []'),
    'BE: suggestions are an integral part of every answer — never empty');
  // BE: значок Enter в чипах (карандаш читался как «редактирование»)
  const sr = js.split('function showReplies(')[1].split('\nfunction ')[0];
  assert(sr.includes("el('button', 'rc-ed', '↵');") && !sr.includes('✎'),
    'BE: chips carry an Enter glyph, not a pencil');
  // BI: 6 объёмных фигур — стекло, свет по нормали, задние рёбра приглушены
  assert(js.includes('<radialGradient id="gF') &&
    js.includes("url(#gE' + L") && !js.includes("url(#gB' + L") &&
    js.includes('(0.18 + 0.34 * bright)') &&
    ['tess', 'penta', 'cube', 'octa', 'tetra', 'crystal']
      .every((k) => js.includes('  ' + k + ': {')) &&
    ['star', 'hex', 'cross', 'line', 'wave', 'zig']
      .every((k) => !js.includes('  ' + k + ': {')),
    'BE: shapes are few but readable — edges, glass surface, back edges');
  // BE: вращение во время показа — 4 плоскости, часто (3.5–8с)
  assert(!js.includes('DOT_SPINS') &&
    !css.includes('@keyframes spinZ{') && !css.includes('.rot-g.rot-z') &&
    js.includes('core._shapeRaf = requestAnimationFrame(rot);') &&
    js.split('function dotShapePlay(')[1].split('\nfunction ')[0]
      .includes('3500 + Math.random() * 4500') &&
    js.includes('const a = t * 0.85, b = t * 0.5;'),
    'BE: the JS engine rotates shapes — 4D figures turn inside out in XW/ZW planes');
  // BE/BG: скролл — плавный догон; прилипание у дна — ПЕРВЫМ делом
  assert(js.includes('function chaseBottom(') &&
    js.includes('const target = Math.min(13, Math.max(0.9, gap * 0.13));') &&
    js.includes('function followGrowingPanel(') &&
    js.includes('if (top < st.lastTop - 2) { leave(); st.lastTop = top; return; }') &&
    js.includes('if (run && h - top - box.clientHeight < 48) {') &&
    js.includes('st.autoPend = 0;'),
    'BE: smooth chase-scroll; stickiness only at the very bottom');
  // BE: агент и компьютер — строка в чате, не всплывашка
  assert(js.includes('function toolLine(') &&
    js.includes("toolLine('agent', S.agentMode);") &&
    js.includes("toolLine('computer', false);") &&
    js.includes("toolLine('computer', true);") &&
    !js.includes('Агентский режим включён') && !js.includes('Готов управлять') &&
    css.includes('.tool-mark{'),
    'BE: tool toggles leave a stylish line in the chat, not a toast');
}

function testIterationBFContracts() {
  // BF: задачи AUTO открываются и редактируются — как сценарии
  assert(js.includes('function openTask(') && js.includes('function editTask(') &&
    js.includes("card.onclick = () => openTask(t);") &&
    js.includes("api('/api/tasks/update'") &&
    js.split('function paintTaskCard(')[1].split('\nfunction ')[0]
      .includes("mk('Редактировать', '', () => { editTask(t); }, st === 'running');") &&
    pyServer.includes('if path == "/api/tasks/update":'),
    'BF: AUTO tasks open and edit — same affordances as scenarios');
  // BF/BG: подсказки после КАЖДОГО ответа; Enter не прячет чипы;
  // во время печати чипы не заказываются (гейт streaming)
  assert(js.includes('const chat = ui.chatId || S.chatId;') &&
    js.includes('chat === activeChatId()') &&
    js.split('function fetchReplies(')[1].split('\nfunction ')[0]
      .includes('if (S.streaming) return;') &&
    !js.split("ed.addEventListener('click'")[1].split('chip.appendChild')[0]
      .includes('box.hidden = true') &&
    pyAgent.split('def suggest_replies_ai(')[1].split('\ndef ')[0]
      .includes('items = themed or parsed'),
    'BF: chips refresh after every answer; Enter keeps them on screen');
  // BF/BG: круглешок выше (уровень JARVIS), sticky в длинном ответе,
  // тело — качественный SVG со светом и градиентами
  assert(css.includes('.ai-core{position:absolute;left:50%;top:8px;width:15px;height:15px;') &&
    css.includes('.msg-ai.live .ai-avatar{position:sticky;top:8px;z-index:2}') &&
    js.includes('<radialGradient id="gF') &&
    js.includes('function dotShapeFrame(') &&
    js.includes('const a = t * 0.85, b = t * 0.5;'),
    'BF: the dot sits level with the JARVIS label, sticks while reading, shapes are solid bodies');
  // BF: строка инструмента — робот с предлагашек, меньше и тише
  const tl = js.split('function toolLine(')[1].split('\nfunction ')[0];
  assert(tl.includes('<rect x="5" y="8" width="14" height="11" rx="3"/>') &&
    tl.includes('<circle cx="12" cy="3.6" r="1.3"/>') &&
    css.includes('.tool-mark .tm-ico{display:grid;place-items:center;width:19px;height:19px;') &&
    js.split('function placeDaySeparator(')[1].split('\nfunction ')[0]
      .includes("prev.classList.contains('tool-mark')"),
    'BF: tool lines wear the robot glyph, quieter design, no stray date separator');
  // BF: агентский скролл честный — clamp свёртки и force
  assert(js.includes('if (h < st.lastH - 2) {') &&
    js.includes('const gone = !!(run && run.followOutput === false);') &&
    js.includes('if (!gone && (force || run || near)) {'),
    'BF: folding clamp is not a user escape; force never drags a reader who left');
  // BF: математическая панель — с открывающей скобки, живьём
  const markdown = fs.readFileSync(path.join(root, 'app/jarvis/web/js/markdown.js'), 'utf8');
  assert(markdown.includes('math-block') && markdown.includes('math-live') &&
    js.split('function renderTyped(')[1].split('\nfunction ')[0].includes('mathOpen') &&
    css.includes(".math-block{font-family:'STIX Two Math','Cambria Math'"),
    'BF: \\[ ... \\] prints as a math panel the moment it starts');
}

function testIterationAWContracts() {
  // AW: мысли не штормят прокрутки (английский теперь течёт — раньше
  // каждый кусок дёргал scrollDown и валил кадры)
  const thinkCase = js.split("case 'thinking':")[1].split("case 'plan':")[0];
  assert(thinkCase.includes('scrollSoon(ui);') &&
    !thinkCase.includes('\n      scrollDown();\n      break;'),
    'AW: thinking events never force a scroll storm');
  // AW: thinkType коагулирует перерисовку в кадр
  assert(js.split('function thinkType(')[1].split('\n}\n')[0].includes('el._thinkRaf'),
    'AW: think rendering is coalesced into animation frames');
  // AW: llm-дедуп не глотает content/tool_calls дельты (continue убран)
  const dedupBlock = pyLlm.split('think = _reasoning_increment(')[1]
    .split('for tc in delta.get("tool_calls")')[0];
  assert(dedupBlock.includes('if think:') && !dedupBlock.includes('\n                                    continue'),
    'AW: a deduped reasoning chunk never swallows the rest of the delta');
}

function testIterationAUContracts() {
  // AV: скорости возвращены + КОРЕНЬ медленности закрыт (time-aware cap)
  assert(js.includes('const CPS_TALK = 125;') &&
    js.includes('const CPS_TALK_MAX = 245;') &&
    js.includes('const CPS_CODE = 470;') &&
    js.split('function typerStart(')[1].split('\nfunction ')[0]
      .includes('const frameCap = Math.max(baseCap, Math.ceil((ui.cps * elapsed) / 1000));'),
    'AV: classic speeds are back; the per-frame cap now follows real frame time');
  // AU: видимый номер сборки — всегда ясно, какой билд на экране
  assert(html.includes('<span class="ver-chip">b70</span>') &&
    css.includes('.ver-chip{align-self:center;'),
    'AU: the build number is visible in the top bar');
  // AU: призрак-надпись физически не может растянуться на весь экран
  assert(css.includes('.ghost-title{display:grid;width:max-content}'),
    'AU: the title ghost is clamped to its content');
  // AW: призраки рождаются В МОМЕНТ ОТПРАВКИ, оригиналы прячутся в тот же кадр
  assert(js.includes("const gc = el('div', 'fly-ghost ghost-reactor',") &&
    js.includes("wlReactor.style.visibility = 'hidden';") &&
    js.includes('welcomeFlight = { core: cr, title: tr, ghostCore: gc, ghostTitle: gt };') &&
    !js.includes('}), 320);'),
    'AW: ghosts are born at send time — the welcome never disappears');
}

function testIterationATContracts() {
  // AT: ФПС — blur-стекло и gap больше не анимируются (пересчёт размытия
  // рвал кадры в Safari); геометрия по-прежнему едет кривыми дока
  assert(!css.includes('backdrop-filter .5s') &&
    !css.includes('gap .6s ease') &&
    css.includes('transition:width .6s cubic-bezier(.22,.68,.18,1),padding'),
    'AT: dock FPS — no blur/gap transitions, geometry only');
  // AT: перелёт v2 — места пусты до прилёта, призраки по своим классам
  assert(js.includes("core.classList.add('pre-flight')") &&
    js.includes("name.classList.add('pre-flight')") &&
    css.includes('.ai-core.pre-flight{opacity:0}') &&
    css.includes('.ai-name.pre-flight{opacity:0}') &&
    js.includes("'fly-ghost ghost-reactor'") && js.includes("'fly-ghost ghost-title'") &&
    css.includes('.ghost-title{display:grid;width:max-content}') &&
    !js.includes("'hello fly-ghost'") && !js.includes("'reactor fly-ghost'"),
    'AT: destinations empty until landing; ghosts styled as themselves');
  // AT: цель меряется в момент старта — призрак не летит мимо
  /* AW: полёт стартует мгновенно, наведение ЖИВОЕ — цель перемеряется
     каждый кадр, устареть не может */
  assert(js.split('function flyGhost(')[1].split('\nfunction ')[0].includes('const to = targetRect();') &&
    js.includes('requestAnimationFrame(tick);') && !js.includes('}), 320);'),
    'AT/AW: live homing — the target is re-measured every frame');
  // AU: перевод снесён — мысли любого языка идут в ленту живьём
  assert(pyAgent.includes('_HAS_LETTERS_RE') &&
    !pyAgent.includes('_translate_think') &&
    !pyAgent.includes('think_translated_events'),
    'AU: thinking streams live in any language, no translation at all');
}

function testIterationARContracts() {
  // AS: рождение ответа — ПЕРЕЛЁТ из приветствия (ядро + надпись JARVIS)
  assert(js.includes('flyWelcomeInto(node, welcomeFlight);') &&
    js.includes('welcomeFlight = { core: cr, title: tr, ghostCore: gc, ghostTitle: gt };') &&
    js.includes("'.welcome .reactor.xl'") &&
    js.includes('.welcome .hello span'),
    'AS: first request — the big core and the JARVIS title fly into the reply');
  // AS: эстафеты нет — никаких relay- следов ни в JS, ни в CSS
  assert(!js.includes('relay') && !css.includes('relay'),
    'AS: the relay is cancelled — the dock reactor lives forever');
  // AR: статика больше не кэшируется браузером
  assert(pyServer.includes('"Cache-Control", "no-store"') &&
    html.includes('/static/css/app.css?v=1.2.0-beta.81'),
    'AR: statics are always fresh — no more week-old CSS in the browser');
  // AR: повтор потока reasoning склеивается обратно в чистый текст
  assert(pyLlm.includes('def _reasoning_increment(') &&
    pyLlm.includes('think = _reasoning_increment('),
    'AR: degenerate reasoning repeats collapse into clean text at the source');
}

function testIterationAQContracts() {
  // AQ: ВЕРХНЯЯ ПАНЕЛЬ — НЕПРЕРЫВНАЯ ПОЛОСА на всю ширину, всегда за
  // меню (z-55 против z-60): меню уезжает в док — полоса открывается
  // за ним БЕЗ анимации самой панели. Никакого скольжения верха.
  const topbar = css.split('.topbar{')[1].split('}')[0];
  assert(/position:fixed/.test(topbar) &&
    /top:0;left:0;right:0/.test(topbar) &&
    /z-index:55/.test(topbar) &&
    /padding:11px 18px 11px 280px/.test(topbar),
    'AQ: topbar is a fixed full-width band under the sidebar — revealed, never animated');
  assert(css.includes('padding-top:56px}') &&
    css.includes('.topbar{padding-left:80px}'),
    'AQ: content starts below the band; mobile band clears the icon strip');
  // AR: элементы панели едут на новую площадь кривыми дока
  assert(css.includes('transition:padding-left .6s cubic-bezier(.22,.68,.18,1)}') &&
    css.includes('.app.collapsed .topbar{padding-left:18px;') &&
    css.includes('transition:padding-left .6s cubic-bezier(.5,.35,.15,1)}'),
    'AR: band content slides with the dock and redistributes');
  // AQ: ХОД МЫСЛЕЙ — целые предложения, дубли и огрызки умирают в буфере
  assert(pyAgent.includes('class _ThinkFilter:') &&
    pyAgent.includes('_THINK_REPEAT_RE') &&
    pyAgent.includes('think_filter.feed(') &&
    pyAgent.includes('think_filter.close()'),
    'AQ: backend buffers reasoning into whole sentences, kills glued doubles');
  // AQ: карточка мыслей — РАНЬШЕ ВСЕХ в ответе
  const thinkCase = js.split("case 'thinking':")[1].split("case 'plan':")[0];
  assert(thinkCase.includes('node.body.firstChild !== ui.thinkCard') &&
    thinkCase.includes('node.body.insertBefore(ui.thinkCard, node.body.firstChild)') &&
    thinkCase.includes('node.body.insertBefore(qn, node.body.firstChild)'),
    'AQ: whenever the thought appears, it opens the answer — never hides at the bottom');
  // AS: покой — живое дыхание круглешка (эстафеты и стали больше нет)
  assert(css.includes('.ai-core{position:absolute;left:50%;top:8px;width:15px;height:15px;') &&
    css.includes('@keyframes coreLive{') &&
    !css.includes('grayscale(.88) brightness(1.32)'),
    'AS: the core dot is alive — breathe at rest, pulse while printing');
}

function testIterationAPContracts() {
  // AP: ТУМБЛЕР — детерминированный старт: ВЫКЛ под курсором больше не
  // телепортируется (резинка перезапускалась с 0% и врала нулём)
  const ch = js.split("$('#tgAgent').addEventListener('change'")[1].split('\n});')[0];
  assert(/let from;/.test(ch) &&
    /if \(this\.checked\) \{[\s\S]*?getComputedStyle\(knob\)\.transform/.test(ch) &&
    ch.includes("from = 'translateX(10px)'") &&
    ch.includes('if (from !== to)') &&
    /knob\.animate\(/.test(ch),
    'AP: toggle always animates — deterministic from/to, no teleport on hover+click');
  // AP: ХОД МЫСЛЕЙ — серверный фильтр требует живую кириллицу
  assert(pyAgent.includes('cyr >= 4 and cyr >= lat'),
    'AP: backend reasoning filter demands real Cyrillic — garbage never shown');
  // AP: ВЕРХНЯЯ ПАНЕЛЬ — margin вместо grid-колонок (Safari их не анимирует)
  assert(css.includes('.app.collapsed .main{margin-left:0;') &&
    !css.includes('grid-template-columns .6s'),
    'AP: no more left-edge snapping — margin slides everywhere');
}

function testIterationANContracts() {
  // AN/AS: СИГИЛ ЗАМЕНЁН КРУГЛЕШКОМ (ядро без колец) — вся SMIL-сага
  // и эстафета удалены; перелёт из приветствия делает flyWelcomeInto
  assert(js.includes('const AVATAR_CORE =') &&
    js.includes('class="ai-core"') &&
    (js.match(/AVATAR_CORE \+/g) || []).length === 4 &&
    (js.match(/<animate/g) || []).length === 0 &&
    !/sigil/i.test(js) && !/sigil/i.test(css) &&
    typeof extractFunction(js, 'flyWelcomeInto') === 'string' &&
    typeof extractFunction(js, 'flyGhost') === 'string',
    'AN/AS: avatar is the live core dot, zero SMIL tags, flight engine present');
  // AN: ТУМБЛЕР ИГРАЕТ ВСЕГДА — даже если клик пришёлся на живую резинку.
  // Круглёшок ведёт WAAPI от его текущего положения, той же кривой .45с.
  const ch = js.split("$('#tgAgent').addEventListener('change'")[1].split('\n});')[0];
  assert(/getComputedStyle\(knob\)\.transform/.test(ch) &&
    /knob\.getAnimations\(\)\.forEach/.test(ch) &&
    /knob\.animate\(/.test(ch) &&
    /duration: 450/.test(ch) &&
    /easing: 'cubic-bezier\(\.3,\.6,\.3,1\)'/u.test(ch) &&
    /go\.onfinish/.test(ch),
    'AN: toggle on/off always animates — WAAPI leads the knob over the rubber');
  // AN: ХОД МЫСЛЕЙ — ЖИВОЙ ФОРМАТ: исполняем thinkFormat и сверяем строки
  const fmt = loadFunctions(['thinkFormat'], {});
  assert(fmt.thinkFormat('Хм... так... надо сделать. Проверю.') === 'Хм\nтак\nнадо сделать.\nПроверю.' &&
    fmt.thinkFormat('Ищу новости. Читаю источники.') === 'Ищу новости.\nЧитаю источники.' &&
    fmt.thinkFormat('т. д. и т. п. без изменений') === 'т. д. и т. п. без изменений' &&
    fmt.thinkFormat('...........') === '' &&
    fmt.thinkFormat('') === '' &&
    fmt.thinkFormat(', .,.:,. 2 2026.') === '' &&
    !fmt.thinkFormat('думаю... ищу... читаю').includes('…') &&
    !fmt.thinkFormat('думаю... ищу... читаю').includes('..'),
    'AN/AP: reasoning — one thought per line, NO ellipses, letterless garbage dropped');
  const thinkType = extractFunction(js, 'thinkType');
  assert(/el\._raw/.test(thinkType) && /thinkFormat\(el\._raw\)/.test(thinkType) &&
    /thinkFormat\(el\._raw\)/.test(extractFunction(js, 'thinkFlush')) &&
    /thinkFormat\(think\)/.test(extractFunction(js, 'restoreTrace')) &&
    extractFunction(js, 'qtThinkFeed').includes('(?:\\s*\\.\\s*){2,}'),
    'AN: thinkType/thinkFlush/restoreTrace/qtThinkFeed all render through the live format');
  // AN: ПЛИТКИ — ИЗНАЧАЛЬНЫЙ ВИД: строки не принудительно равны (не было
  // полей сверху/снизу у коротких), заголовок — одна строка с троеточием
  const suggGrid = css.split('.suggestions{')[1].split('}')[0];
  assert(/repeat\(3,minmax\(0,1fr\)\)/.test(suggGrid) &&
    /grid-auto-rows:142px/.test(suggGrid),
    'AN/AX/AY: equal columns AND a fixed compact height — even, text-forward tiles');
  const suggB = css.split('.sugg b .st{')[1].split('}')[0];
  assert(/white-space:nowrap/.test(suggB) &&
    /overflow:hidden/.test(suggB) &&
    /text-overflow:ellipsis/.test(suggB) &&
    /min-width:0/.test(suggB),
    'AQ: title lives in an inner span (min-width:0) — the canonical ellipsis pattern');
  assert(css.includes('.sugg b{display:flex;min-width:0;') &&
    js.includes("'</span></b><span class=\"sp\">' + esc(s.desc || s.prompt) + '</span>'") &&
    !js.includes('fitSuggTitle') && !js.includes('watchSuggTitle'),
    'AQ: JS measuring is gone — pure CSS ellipsis inside the span');
  // AN: НАВЕДЕНИЕ ЧУТЬ МЕДЛЕННЕЕ, вкл/выкл прежние .45с одной кривой
  assert(/animation:agKnobRubber 1s cubic-bezier\(\.3,\.7,\.3,1\) both/.test(css) &&
    /animation:agEmberRun \.9s cubic-bezier\(\.3,\.5,\.35,1\) both/.test(css) &&
    /animation:agRestGlow \.5s ease \.6s both/.test(css) &&
    /animation:agSparkRun 1\.1s linear both/.test(css),
    'AN: hover slightly slower — 1s rubber, .9s ember, 1.1s spark, .5s glow');
}

function testIterationAHContracts() {
  // AG2: каждый звонок — новый разговор; голосовые не в песочнице
  assert(!/jarvisVoiceChat/.test(js) &&
    !/voiceLoadTranscript\(\);/.test(extractFunction(js, 'openVoiceMode')),
    'AG2: each call starts a fresh conversation; no old transcript on open');
  // AG3: затемнение — ключи WAAPI-полёта
  const foldFn = extractFunction(js, 'qtFold');
  assert(/const flyKeys = \(target\) => \[/.test(foldFn) &&
    /setKeyframes\(flyKeys\(aim\)\)/.test(foldFn),
    'AG3: dimming keys travel inside the flight, homing keeps them');
  // AH: свёрнутое меню — плавающий док, контент на всю ширину
  assert(/\.app\.collapsed \.main\{margin-left:0;/.test(css) &&
    /\.main\{grid-column:1;margin-left:262px;/.test(css),
    'AH: content spans full width when collapsed; topbar reaches the left edge');
  const dock = css.split('/* ---- свёрнутый режим: панель превращается в плавающий DOCK')[1].split('/* подпись иконки')[0];
  assert(/\.app\.collapsed \.dock\{pointer-events:auto/.test(dock) &&
    /width:68px/.test(dock) &&
    /backdrop-filter:blur\(18px\) saturate\(1\.2\)/.test(dock) &&
    /border-radius:20px/.test(dock) &&
    /background:rgba\(15,27,44,\.38\)/.test(dock) &&
    /\.app\.collapsed \.nav\{margin:0\}/.test(dock) &&
    /\.app\.collapsed \.nav-item\{gap:0;width:auto;justify-content:center;padding:10px 0;margin:0 6px;transform:none;/.test(dock) &&
    /\.app\.collapsed \.nav-item\.active::before\{display:none\}/.test(dock) &&
    /padding-left:76px/.test(dock) &&
    !/transform:scale/.test(dock),
    'AH: dock is matte glass, nothing protrudes beyond the pill');
  assert(/--dock-y/.test(js) && /function dockY\(on\)/.test(js) &&
    /side-folding/.test(js) && /side-folding/.test(css) &&
    /app\.classList\.toggle\('collapsed', collapsing\);/.test(js) &&
    /app\.classList\.toggle\('side-folding', collapsing\);/.test(js) &&
    !/SIDE_FADE/.test(js) && !/SIDE_MORPH/.test(js) &&
    /cubic-bezier\(\.5,\.35,\.15,1\)/.test(dock) &&
    /localStorage\.removeItem\('jarvis\.sidebar2'\)/.test(js),
    'AJ: simultaneous mirrored choreography, default opens with dock');
}

function testIterationAFContracts() {
  // AF5: включённая камера не предлагается повторно
  assert(/camera_on: camLive\(\),/.test(extractFunction(js, 'send')),
    'AF5: the client reports camera state so an active mode is never re-offered');
  // AF6: звук по умолчанию ВКЛЮЧЁН; кнопка пересинхронизируется с конфигом
  assert(/function soundOn\(\) \{ const ui = S\.config && S\.config\.ui; return !\(ui && ui\.sound === false\); \}/.test(js) &&
    /S\.config = st\.config \|\| \{\};\n  syncSoundBtn\(\);/.test(js),
    'AF6: sound defaults ON (missing setting = on) and the button re-syncs when config arrives');
}

function testIterationAEContracts() {
  // AE1: сценам — красноватый в общей палитре
  assert(/\.nav-item\[data-view="scenarios"\] \.nav-ico\{color:#a84a5b\}/.test(css) &&
    /\.view-scenarios \.panel-head h2\{color:#f5ccd3\}/.test(css) &&
    !/\.view-scenarios \.panel-head h2\{color:var\(--red\)\}/.test(css),
    'AE1: scenarios tab is muted dark red; the page title is pale like other tabs');
  // AE2: живые вложения не становятся файлами
  assert(/transient: true,/.test(extractFunction(js, 'camAttachFrame')),
    'AE2: camera frames upload as transient — never stored as dialog files');
  // AE3: нет CSS-троеточия у строк мысли (в заголовках ellipsis уместен и остаётся)
  const flowCss = css.split('.qt-flowline{font-size:11px')[1].split('}')[0];
  assert(/white-space:normal/.test(flowCss) && !/text-overflow/.test(flowCss),
    'AE3: think lines wrap instead of being cut with a CSS ellipsis');
  // AE8: иконка звука спокойная
  assert(!/\.voice-btn\.on \.w1\{animation/.test(css) &&
    !/\.voice-btn\.off \.mute\{opacity:1;color:var\(--red\)\}/.test(css) &&
    /\.voice-btn\.tick svg\{animation:vsndTick/.test(css),
    'AE8: sound icon does not pulse when on, is monochrome when off, ticks on toggle');
}

function testIterationACContracts() {
  // AC1: мысль — непрерывный текст (не слово-на-строку) и открытие без display
  const mini = extractFunction(js, 'qtMiniaturize');
  assert(/function qtThinkFeed\(flow, text\)/.test(js) &&
    /qtThinkFeed\(flow, ev\.text\)/.test(js) &&
    !/\.style\.display/.test(mini) &&
    /body\.style\.height = '0px';/.test(mini) &&
    /body\.style\.opacity = '0';/.test(mini),
    'AC1: think stream flows as continuous text; collapsed body is height:0, never display:none');
  // AC2: корень стопки курсоров — isConnected в отцепленном DOM
  const ensure = extractFunction(js, 'ensureStatus');
  const openChatFn = extractFunction(js, 'openChat');
  const watchDet = extractFunction(js, 'watchDetached');
  assert(/ui\.node\.body\.contains\(ui\.statusEl\)/.test(ensure) &&
    !/ui\.statusEl\.isConnected/.test(ensure) &&
    /sbody\.contains\(ui\.mdEl\)/.test(extractFunction(js, 'send')) &&
    /\(S\.liveRuns \|\| \{\}\)\[id\]\) return;/.test(watchDet) &&
    /if \(!liveAttached && \(r\.generating \|\| S\.detached === id\)\) watchDetached\(id\);/.test(openChatFn),
    'AC2: status existence = child of run body (works detached); poller yields to a live run');
  // AC4: голос — эхо-подавление, устойчивый перебой, всегда изолирован
  const openVoice = extractFunction(js, 'openVoiceMode');
  const closeVoice = extractFunction(js, 'closeVoiceMode');
  const barge = extractFunction(js, 'voiceBargeLoop');
  assert(/echoCancellation: true, noiseSuppression: true, autoGainControl: true/.test(openVoice) &&
    /level > 0\.16/.test(barge) && /VOICE\.barge >= 7/.test(barge) &&
    /localStorage\.getItem\('jarvisVoiceCtx'\) === '1'/.test(js) &&
    /S\.voiceBox\.remove\(\); S\.voiceBox = null;/.test(closeVoice) &&
    /voice_context: \(requestVoice && VOICE\.ctxOn && S\.chatId\) \|\| '',/.test(extractFunction(js, 'send')) &&
    /voiceRenderTranscript\(tb\);/.test(closeVoice) &&
    /flex-direction:column/.test(css),
    'AC4: echo cancellation + sturdier barge; ctx off by default; voice always isolated; the field really leaves');
}

function testIterationABContracts() {
  // AB1: КОРЕНЬ подлагивания последнего кадра мысли — display-переключение
  // гасило вертикальные поля. Теперь полей нет вовсе, тело не выключается.
  const toggle = extractFunction(js, 'qtToggleThink');
  assert(/\.qt-think \.qt-body\{margin:0 0 0 6px\}/.test(css) &&
    /\.qt-think \.qt-flow\{margin-top:3px\}/.test(css) &&
    !/display\s*=\s*'none'/.test(toggle) &&
    !/display:none/.test(js.split('function restoreTrace')[1].split('\nfunction ')[0]),
    'AB1: think body has no toggling vertical margins — no last-frame jump, ever');
  // AB2: живой ответ переживает переключение диалога — реестр прогонов
  const sendFn = extractFunction(js, 'send');
  const openChatFn = extractFunction(js, 'openChat');
  assert(/S\.liveRuns\[requestChatId\] = ui;/.test(sendFn) &&
    /S\.liveRuns\[ev\.chat_id\] = ui;/.test(js) &&
    /stream\.appendChild\(live\.node\.root\);/.test(openChatFn) &&
    /if \(S\.followUi === live && !S\.streaming\) setStreaming\(true\);/.test(openChatFn) &&
    /if \(S\.liveRuns\[k\] === ui\) delete S\.liveRuns\[k\];/.test(sendFn),
    'AB2: a live run is registered per chat and re-attached on return — the answer keeps printing, Stop stays Stop');
  // AB3: разговор — ОБЛАСТЬ, как камера: без полноэкранного окна и текста
  assert(/function voiceMount\(forceOwn\)/.test(js) &&
    /function buildVoiceCard\(\)/.test(js) &&
    !/voice-veil/.test(css) &&
    /\.voice-run\{display:none!important\}/.test(css) &&
    /\.voice-box\.thinking \.v-orb b\{/.test(css) &&
    /voiceIsolated = requestVoice;/.test(sendFn) &&
    /voice: requestVoice,/.test(sendFn) &&
    /node\.root\.classList\.add\('voice-run'\);/.test(sendFn) &&
    /if \(VOICE\.open\) closeVoiceMode\(\);/.test(extractFunction(js, 'newChat')) &&
    /if \(VOICE\.open\) voiceMount\(true\);/.test(extractFunction(js, 'stopCam')) &&
    /if \(VOICE\.open\) voiceMount\(\);/.test(extractFunction(js, 'startCam')) &&
    /Контекст диалога/.test(js) && /voiceLoadTranscript/.test(js),
    'AB3: voice is an inline area like the camera — orb-only status, context toggle, hidden text, unified cam UI');
  // AB5: пара вариантов = та же сноска
  const mount = extractFunction(js, 'mountUiPanels');
  assert(/pair\.t = 'confirm'; pair\.val = null;/.test(mount) &&
    /const labels = \(it\.opts && it\.opts\.length === 2\) \? it\.opts : \['Да', 'Нет'\];/.test(mount),
    'AB5: any 2-option choice renders as the same yes/no footnote with two buttons');
}

function testIterationBGContracts() {
  // BG: АВТО — кнопка работает СВОЕЙ функцией, карточку не открывает;
  // «Лог» с карточки ушёл в свёрнутый <details> окна задачи
  const card = js.split('function paintTaskCard(')[1].split('\nfunction ')[0];
  const task = js.split('function openTask(')[1].split('\nfunction ')[0];
  assert(card.includes("b.addEventListener('click', (e) => { e.stopPropagation(); fn(e); });") &&
    !card.includes("mk('Лог'") &&
    task.includes('<details class="sd"') && task.includes('Лог ('),
    'BG: task-card buttons do only their job; the log lives inside the task window');
  // BH: карточки разрешений ВЕРНУТЫ как было; по центру — только
  // строка-уведомление (.tool-mark: flex + margin auto)
  assert(css.includes('.mc-actions{display:flex;align-items:center;gap:16px}') &&
    !css.includes('.mc-actions>.mc-switch') &&
    css.split('.tool-mark{')[1].split('}')[0].includes('margin:7px auto'),
    'BH: permission cards are back to the original; the centered one is the tool-mark notification');
  // BG: чипы уходят СРАЗУ при отправке; во время печати не заказываются
  const send = js.split('async function send(')[1].split('\nfunction ')[0];
  assert(send.includes("const rb = $('#replyBar');") &&
    send.includes("if (rb) { rb.hidden = true; rb.innerHTML = ''; }") &&
    js.split('async function fetchReplies(')[1].split('\nfunction ')[0]
      .includes('if (S.streaming) return;') &&
    js.includes('const chat = ui.chatId || S.chatId;') &&
    js.includes('if (!(S.streaming && S.streamRun !== runId) && chat && chat === activeChatId())') &&
    js.includes('if (!cachedReplies.length && !S.streaming) fetchReplies();'),
    'BG: chips clear instantly on send; never ordered mid-stream; refetched on return');
  // BG: круглешок — полноценный SVG со светом, без clip-path-полигонов
  assert(js.includes('<radialGradient id="gF') &&
    js.includes('<linearGradient id="gE') && js.includes('<linearGradient id="gB') &&
    js.includes("url(#gE' + L") && !js.includes("url(#gB' + L") && js.includes('(0.18 + 0.34 * bright)') &&
    !css.includes('clip-path:polygon(') &&
    css.includes('.msg-ai.live .ai-core.shape-on{opacity:0}') &&
    css.includes('@keyframes dotSvgOut{') && css.includes('@keyframes dotSvgFly{') &&
    !css.includes('dotSvgIn') &&
    css.includes('width:46px;height:46px;'),
    'BG/BM: the dot is a real SVG with light and edges; the exit flies honestly');
  // BG: скролл — дно первым, свёртка панели догоняется плавно
  assert(js.includes('if (run && h - top - box.clientHeight < 48) {') &&
    js.includes('st.autoPend = 0;') &&
    js.includes('function followGrowingPanel(') &&
    js.includes('const target = Math.min(13, Math.max(0.9, gap * 0.13));'),
    'BG: bottom-stick check comes first; collapsing panels are chased smoothly');
  // BG: математика — правило 11, мини-LaTeX и живые панели plot/geo
  assert(pyAgent.includes('11. МАТЕМАТИКА') && pyAgent.includes('```plot') &&
    pyAgent.includes('```geo') &&
    markdown.includes('function mathRender(') &&
    markdown.includes('function mathIsBlock(') &&
    markdown.includes('data-kind="plot"') && markdown.includes('data-kind="geo"') &&
    js.includes('function mathParseExpr(') && js.includes('function mathCompile(') &&
    js.includes('function mountPlotPanels(') &&
    js.includes('function buildPlot2Panel(') && js.includes('function buildPlot3Panel(') &&
    js.includes('function buildGeoPanel(') &&
    css.includes('.plot-panel{') && css.includes('.mfrac{') && css.includes('.msqrt{') &&
    (js.match(/mountPlotPanels\(/g) || []).length >= 4,
    'BG: real LaTeX math with live 2D/3D plots and geometry drawings in the chat');
}

function testIterationBHContracts() {
  // BH: агентский скролл — непрерывный живой догон + разгон догона
  assert(js.includes('function followLiveStream(') &&
    js.includes("box.querySelector('.msg-ai.live')") &&
    js.includes('followLiveStream(box);') &&
    !js.includes('Math.max(2, Math.ceil(gap * 0.16))') &&
    js.split('function followLiveStream(')[1].split('\nfunction ')[0]
      .includes('chaseBottom(box, run);') &&
    js.split('function followGrowingPanel(')[1].split('\nfunction ')[0]
      .includes('chaseBottom(box, run);') &&
    js.split('function chaseBottom(')[1].split('\nfunction ')[0]
      .includes('const target = Math.min(13, Math.max(0.9, gap * 0.13));') &&
    !js.includes('st.v ='),
    'BI: agent and panel scroll share one chaseBottom engine — no separate steps');
  // BH: уведомление режима остаётся ПОСЕРЕДИНЕ ответа
  const tl = js.split('function toolLine(')[1].split('\nfunction ')[0];
  assert(tl.includes('let liveUi = (S.followUi') &&
    tl.includes('const cand = (S.liveUi && S.liveUi !== S.followUi)') &&
    tl.includes('liveUi.freezePending = true;') &&
    tl.includes("liveUi.marksEl = el('div', 'md-marks');") &&
    tl.includes('if (markHost) markHost.appendChild(row);') &&
    js.split('function renderTyped(')[1].split('\nfunction ')[0].includes('closeMarkSegment(ui);') &&
    css.includes('.md-marks{width:100%}') &&
    !css.includes('.mc-actions>.mc-switch'),
    'BH: a mode note enabled mid-answer stays at its place in the stream');
  // BH: круглешок — только объёмные фигуры; 1D не крутятся; свет виден
  assert(['tess', 'penta', 'cube', 'octa', 'tetra', 'crystal']
      .every((k) => js.includes('  ' + k + ': {')) &&
    ['star', 'hex', 'cross', 'line', 'wave', 'zig']
      .every((k) => !js.includes('  ' + k + ': {')) &&
    js.includes('core._shapeRaf = requestAnimationFrame(rot);') &&
    js.includes('const a = t * 0.85, b = t * 0.5;') &&
    css.includes('width:46px;height:46px;') &&
    css.split('@keyframes coreLive{')[1].split('}}')[0].indexOf('background') < 0 &&
    css.includes('hue-rotate(-138deg)') &&
    css.includes('transition:opacity .45s ease'),
    'BH: volumetric shapes only; 1D rests; gold interpolates; the dot crossfades');
  // BH: математика — живой рендер пределов, boxed, aligned, стрелок
  const mctx = { window: {} };
  vm.createContext(mctx);
  vm.runInContext(markdown, mctx);
  const mr = mctx.window.MD.render;
  const integ = mr('\\[ \\int_0^1 f(x)\\,dx = F(1) \\]');
  assert(integ.includes('mb-t') && integ.includes('mb-b') &&
    integ.indexOf('mb-t') < integ.indexOf('mb-b'),
    'BH: integral limits stack over and under the sign');
  const boxed = mr('\\[ \\boxed{\\int_0^\\infty e^{-x}dx = 1} \\]');
  assert(boxed.includes('mboxed') && !boxed.includes('boxed{'),
    'BH: \\boxed renders as a frame, not literal text');
  const aligned = mr('\\[\\begin{aligned} x &= 2y + 1 \\\\ y &= 3 \\end{aligned}\\]');
  assert(aligned.includes('mtable m-al') && !aligned.includes('&amp;') &&
    !aligned.includes('aligned}') && aligned.split('mrow').length - 1 === 2,
    'BH: aligned environment lays out rows without leaking & or \\end');
  const arrow = mr('\\( a \\Longrightarrow b \\Longleftrightarrow c \\)');
  assert(arrow.includes('\u27f9') && arrow.includes('\u27fa') &&
    !arrow.includes('Longrightarrow'),
    'BH: long arrows render as glyphs');
  const lim = mr('\\[ \\lim_{x \\to 0} \\frac{\\sin x}{x} = 1 \\]');
  assert(lim.includes('mb-b') && lim.includes('lim') && lim.includes('mfrac'),
    'BH: \\lim carries its condition under the sign');
  // BH: графики — пан по всей площади, ховер, соотношение осей, 3D-фикс
  const p2 = js.split('function buildPlot2Panel(')[1].split('\nfunction ')[0];
  assert(p2.includes('drag = { x: e.clientX, y: e.clientY, x0, x1, y0, y1 };') &&
    p2.includes('const dx = (drag.x - e.clientX) * ux;') &&
    p2.includes('y0 = drag.y0 + dy;') &&
    p2.includes('hover = { px: e.clientX - r.left, py: e.clientY - r.top };') &&
    p2.includes("const ASPECTS = [") && p2.includes("{ r: 1, label: '1:1' }") &&
    p2.includes('let aspect = (!fns.length && series.length) ? ASPECTS[5] : ASPECTS[0];') &&
    css.includes('.plot-pop{') && css.includes('background:rgba(13,29,47,.55)'),
    'BH: the 2D plot pans freely, shows the hovered curve; math keeps 1:1, data plots open in auto');
  const p3 = js.split('function buildPlot3Panel(')[1].split('\nfunction ')[0];
  assert(p3.includes('const sampleGrid = () => {') &&
    p3.includes('for (const R of [2, 5, 10, 25, 60]) {') &&
    p3.includes('поверхность пуста: функция нигде не определена') &&
    p3.includes('if (!isFinite(c00) || !isFinite(c10) || !isFinite(c11) || !isFinite(c01)) continue;'),
    'BH: an empty 3D surface aims at its domain instead of giving up');
}

(async () => {
  testLiveStatusHasNoSpinner();
  testTelegramDateHudAndTimeOnlyMeta();
  testFileViewportAndCloseButton();
  testImportantHeadingCaretAndTrail();
  testPlanTypingCompletionAndDockRaces();
  testRepeatedPlanEventReplacesOwnership();
  await testCameraLifecycleOwnershipAndLateResults();
  testPendingInteractivePanelAndRouteLifecycle();
  testInteractiveFenceReachesFrontendPanel();
  testRussianImageAndHudFollowupContract();
  testReadinessFollowHistoryAndLiveCodeContracts();
  await testAutoPollingReconciliationAndBulkControls();
  testThinkingGradientContract();
  testBudgetScenariosDraftsAndTailRaceContracts();
  testProactiveModesBudgetAndAbortContracts();
  testQuietToolsBoostAskStylesAndAgentTheme();
  testIterationXContracts();
  testIterationYContracts();
  testIterationZContracts();
  testIterationABContracts();
  testIterationACContracts();
  testIterationADContracts();
  testIterationAEContracts();
  testIterationAFContracts();
  testIterationAHContracts();
  testIterationAJContracts();
  testIterationAKContracts();
  testIterationANContracts();
  testIterationAPContracts();
  testIterationAQContracts();
  testIterationARContracts();
  testIterationATContracts();
  testIterationAUContracts();
  testIterationAWContracts();
  testIterationAXContracts();
  testIterationAYContracts();
  testIterationAZContracts();
  testIterationBAContracts();
  testIterationBBContracts();
  testIterationBCContracts();
  testIterationBDContracts();
  testIterationBEContracts();
  testIterationBFContracts();
  testIterationBGContracts();
  testIterationBHContracts();
  testIterationBIContracts();
  testIterationBJContracts();
  testIterationBKContracts();
  testIterationBLContracts();
  testIterationBMContracts();
  testIterationBM6Contracts();
  testIterationBM8Contracts();
  testIterationBM9Contracts();
  testIterationBM10Contracts();
  testIterationBM11Contracts();
  testIterationAOContracts();
  console.log('package28_frontend_runtime: 55 regression groups passed');
})().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
