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
  const box = {
    className: '', markup: '',
    set innerHTML(value) { this.markup = value; },
    get innerHTML() { return this.markup; },
    querySelector(selector) { return selector === '.tw-quip' ? q : statusCaret; },
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
    ['clearTypingDecorations', 'syncCursorPhase', 'placeCaret'],
    {
      document: miniDocument, CURSOR_BREATHE_MS: 1050,
      performance: { now: () => (cursorClock += 20) },
    },
  );
  assert(/const TYPE_MS\s*=\s*20/.test(js), 'DOM typing is capped at 50 renders per second');
  assert(/const CPS_TALK\s*=\s*95/.test(js));
  assert(!/CPS_IMPORTANT/.test(js), 'headings must not have a separate speed');
  assert(!/function importantLine|function headingEndedSince/.test(js),
    'headings must not add a hidden rate or pause branch');
  const typer = extractFunction(js, 'typerStart');
  assert(/let want\s*=\s*code\s*\?\s*CPS_CODE\s*:\s*CPS_TALK/.test(typer));
  assert(!/heading|important/i.test(typer), 'typer must not inspect markdown headings');
  assert(/performance\.now\(\)/.test(typer) && /CPS_SMOOTH_MS/.test(typer),
    'elapsed-time CPS must survive delayed timer frames');
  assert(/Math\.min\(32,\s*now\s*-\s*lastTick\)/.test(typer),
    'a delayed browser frame must not be paid back as a visible character burst');
  assert(/step\s*=\s*Math\.min\(step,\s*left,\s*code\s*\?\s*10\s*:\s*4\)/.test(typer));
  assert(!/left\s*>|backlog|boost|mult/i.test(typer),
    'typing speed must depend on content, never SSE chunk/backlog size');
  assert(/ui\.cps\s*=\s*0[\s\S]*ui\.acc\s*=\s*0/.test(typer),
    'a temporarily drained stream must not leak its previous speed into the next chunk');

  const fast = loadFunctions(['fastLine'], { Math });
  const tableSource = 'Обычный текст\n| Ячейка | Значение |\nОбычный итог';
  assert.strictEqual(fast.fastLine(tableSource, tableSource.indexOf('Ячейка')), true,
    'the complete current row is classified before the first pipe is typed');
  assert.strictEqual(fast.fastLine(tableSource, 3), false);

  const md = new MiniNode('div');
  const heading = new MiniNode('h2');
  const original = 'Пуск 🚀 системы';
  heading.appendChild(miniDocument.createTextNode(original));
  md.appendChild(heading);
  ctx.placeCaret(md);
  assert.strictEqual(md.querySelectorAll('.caret').length, 1);
  assert(md.querySelector('.caret').classList.contains('caret-important'));
  const firstCursorPhase = md.querySelector('.caret').style.animationDelay;
  assert(/^-[\d.]+ms$/.test(firstCursorPhase),
    'a recreated markdown caret must rejoin the global animation phase');
  assert.strictEqual(md.querySelectorAll('.important-trail').length, 1);
  assert.strictEqual(Array.from(md.querySelector('.important-trail').textContent).length, 12,
    'trail must be bounded by twelve Unicode characters');
  assert.strictEqual(heading.textContent, original, 'decorations must not change visible heading text');

  ctx.placeCaret(md);
  assert.strictEqual(md.querySelectorAll('.caret').length, 1, 'each tick owns exactly one caret');
  assert.notStrictEqual(md.querySelector('.caret').style.animationDelay, firstCursorPhase,
    'DOM replacement must advance rather than restart the cursor breath');
  assert.strictEqual(md.querySelectorAll('.important-trail').length, 1, 'trail nodes must not accumulate');
  assert.strictEqual(heading.textContent, original);
  ctx.clearTypingDecorations(md);
  assert.strictEqual(md.querySelector('.caret'), null);
  assert.strictEqual(md.querySelector('.important-trail'), null);
  assert.strictEqual(heading.textContent, original, 'completion must restore a plain text node');

  const plain = new MiniNode('div');
  const p = new MiniNode('p');
  p.appendChild(miniDocument.createTextNode('Обычный ответ'));
  plain.appendChild(p);
  ctx.placeCaret(plain);
  assert(!plain.querySelector('.caret').classList.contains('caret-important'));
  assert.strictEqual(plain.querySelector('.important-trail'), null);
  assert.strictEqual(Array.from(plain.querySelector('.normal-trail').textContent).length, 12,
    'ordinary typing has the same bounded twelve-character blue trail');
  ctx.placeCaret(plain, true);
  assert(plain.querySelector('.caret').classList.contains('caret-important'),
    'a real action can enter important mode without changing typer speed');
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
  assert(goldCaret && /opacity\s*:\s*\.58/.test(goldCaret[1]));
  assert(!/#fff(?:fff)?\b/i.test(goldCaret[1]), 'gold caret must not flare to pure white');
  assert(/\.normal-trail\s*\{[^}]*linear-gradient\(90deg,#9ab0ba[^}]*#74c7d8[^}]*#b6edf3[^}]*text-shadow/s.test(css),
    'ordinary cursor trail must remain delicately but visibly blue');
  assert(/\.important-trail\s*\{[^}]*linear-gradient\(90deg,var\(--tx\)\s+0%[^}]*#c2b278[^}]*#c6b163[^}]*#c8ba7b[^}]*text-shadow/s.test(css),
    'gold trail must use the deliberately dimmed warm palette');
  assert(/\.caret\s*\{[^}]*animation\s*:\s*cursorBreathe[^}]*\}/s.test(css));
  assert(!/@keyframes (?:spark|twBlink)[^{]*\{[^}]*box-shadow/s.test(css),
    'cursor animation must stay on compositor opacity/transform');
  const events = extractFunction(js, 'handleEvent');
  ['plan', 'tool_start', 'approval_wait', 'question', 'file'].forEach((type) => {
    const at = events.indexOf("case '" + type + "'");
    assert(at >= 0 && /ui\.actionAccent\s*=\s*true/.test(events.slice(at, at + 700)),
      type + ' must enable the closed important-action visual mode');
  });
  const question = extractFunction(js, 'questionCard');
  assert(/ask-own/.test(question) && /Свой вариант/.test(question),
    'blocking ask_user controls must offer the same custom answer escape hatch');
  const panels = extractFunction(js, 'mountUiPanels');
  assert(/hasFreeEntry/.test(panels) && /askable\s*&&\s*!hasFreeEntry/.test(panels),
    'a free-text fallback control must not render a duplicate custom-answer field');
  assert(/go\.disabled\s*=\s*!ready\(\)/.test(panels),
    'empty fallback text cannot be submitted accidentally');
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
  let nextTimer = 1;
  const intervals = new Map();
  const timeouts = [];
  const cancelled = new Set();
  const setIntervalFake = (fn, ms) => {
    const id = nextTimer++; intervals.set(id, { fn, ms }); return id;
  };
  const clearFake = (id) => { intervals.delete(id); cancelled.add(id); };
  const setTimeoutFake = (fn, ms) => { const id = nextTimer++; timeouts.push({ id, fn, ms }); return id; };
  const S = { streamRun: 28 };
  let clock = 0;
  const ctx = loadFunctions(
    ['clearPlanTimers', 'finishPlanItems', 'syncCursorPhase', 'typePlanItem', 'undockPlan'],
    {
      S, PLAN_FRAME_MS: 20, CPS_TALK: 95, CURSOR_BREATHE_MS: 1050,
      performance: { now() { clock += 20; return clock; } },
      document: miniDocument, el: miniEl,
      setInterval: setIntervalFake, clearInterval: clearFake,
      setTimeout: setTimeoutFake, clearTimeout: clearFake,
      $$: (selector, node) => node ? node.querySelectorAll(selector) : [],
    },
  );

  // Completion while an item is still typing: interval is cancelled, all
  // text is restored, the plan turns green immediately, then disappears at 2s.
  const list = new MiniNode('ul');
  const { li, copy } = makePlanItem('Проверить жизненный цикл');
  list.appendChild(li);
  const card = new MiniNode('div');
  card.className = 'plan-card live';
  card.appendChild(list);
  card.setTitle = (text) => { card.titleText = text; };
  const ui = {
    runId: 28, planTimers: [], planFinished: false, planItems: [li],
    planList: list, planCard: card, planDock: null,
  };
  ctx.typePlanItem(ui, li, () => { throw new Error('cancelled typer must not finish later'); });
  const typingTimer = ui.planTimers[0];
  intervals.get(typingTimer).fn();
  intervals.get(typingTimer).fn();
  assert.strictEqual(copy.childNodes.length, 3, 'typing uses a bounded lead/trail/caret trio');
  ctx.undockPlan(ui);
  assert(cancelled.has(typingTimer), 'completion must cancel the in-flight item typer');
  assert.strictEqual(copy.textContent, li._planText);
  assert(card.classList.contains('plan-complete'), 'pre-dock completion turns the card green immediately');
  assert.strictEqual(card.titleText, 'План выполнен');
  assert(timeouts.some((timer) => timer.ms === 1700));
  const removeCard = timeouts.find((timer) => timer.ms === 2000);
  assert(removeCard, 'completed pre-dock plan must be removed at exactly 2 seconds');
  removeCard.fn();
  assert.strictEqual(card.isConnected, false);

  // Completion during the dock flight owns the flying clone. The source card
  // leaves normal flow immediately; no thumbnail/collapse placeholder returns.
  const dock = new MiniNode('div');
  dock.className = 'plan-dock fly live';
  dock.dataset.runId = '29';
  const title = new MiniNode('span'); title.className = 'pd-t';
  const step = new MiniNode('span'); step.className = 'pd-step';
  const fill = new MiniNode('i'); fill.className = 'pd-fill';
  const dockStep = new MiniNode('div'); dockStep.className = 'pd-s now';
  dock.appendChild(title); dock.appendChild(step); dock.appendChild(fill); dock.appendChild(dockStep);
  const card2 = new MiniNode('div');
  const plan2 = makePlanItem('Завершить полёт');
  const list2 = new MiniNode('ul'); list2.appendChild(plan2.li); card2.appendChild(list2);
  const ui2 = {
    runId: 29, planTimers: [], planFinished: false, planItems: [plan2.li],
    planList: list2, planCard: card2, planDock: dock,
  };
  const ctx2 = loadFunctions(['clearPlanTimers', 'finishPlanItems', 'undockPlan'], {
    setTimeout: setTimeoutFake, clearTimeout: clearFake,
    $$: (selector, node) => node ? node.querySelectorAll(selector) : (selector === '.plan-dock' ? [dock] : []),
  });
  ctx2.undockPlan(ui2);
  assert.strictEqual(card2.isConnected, false, 'flying plan source must not reserve response space');
  assert(dock.classList.contains('done'));
  assert(!dock.classList.contains('live'));
  assert.strictEqual(title.textContent, 'План выполнен');
  assert.strictEqual(step.textContent, 'готово');
  assert.strictEqual(fill.style.width, '100%');
  assert(dockStep.classList.contains('done'));
  assert(!dockStep.classList.contains('now'));
  const dockRemove = timeouts.filter((timer) => timer.ms === 2000).pop();
  dockRemove.fn();
  assert.strictEqual(dock.isConnected, false, 'flying plan must disappear completely after 2 seconds');

  assert(/const PLAN_FRAME_MS\s*=\s*20/.test(js));
  assert(/carry\s*\+=\s*CPS_TALK\s*\*\s*elapsed\s*\/\s*1000/.test(extractFunction(js, 'typePlanItem')),
    'plan items must use the exact conversational CPS');
  assert(/at\s*-\s*12/.test(extractFunction(js, 'typePlanItem')));
  assert(/const PLAN_ITEM_PAUSE\s*=\s*760/.test(js));
  assert(/const PLAN_LOOK_MS\s*=\s*1200/.test(js));
  assert(!/\.pd-s\.now::after\s*\{/.test(css), 'current dock step must have no underline pseudo-element');
  const current = css.match(/\.pd-s\.now \.pd-cap\s*\{([^}]*)\}/s);
  assert(current && /color\s*:\s*#ffd98a/.test(current[1]), 'current step uses a nearby brighter gold');
  assert(/\.plan-card\.plan-complete/.test(css) && /\.plan-dock\.done/.test(css));
  const doneRule = css.match(/\.plan-dock\.done\s*\{([^}]*)\}/s);
  assert(doneRule && /background\s*:\s*linear-gradient\([^}]*rgba\(7,30,23/.test(doneRule[1]),
    'the complete dock itself, not just its labels, turns green immediately');
  assert(!/collapseSoon\s*\(\s*(?:ui\.)?planCard/.test(extractFunction(js, 'undockPlan')),
    'completion must never return a plan thumbnail');
  const finish = extractFunction(js, 'queueResponseFinish');
  assert(/if\s*\(!ui\.buffer\)\s*ui\.buffer\s*=\s*doneContent/.test(finish));
  assert(/content\s*=\s*ui\.buffer/.test(finish));
  assert(!/ui\.shown\s*=\s*['"]{2}/.test(finish),
    'done must never erase a fully streamed multi-step answer and retype the last step');
  assert(!/ui\.mdEl\.innerHTML\s*=\s*['"]{2}/.test(finish));
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
    clearPlanTimers() { clears += 1; },
    dropStrayDocks(keep) { removedDocks.push(keep); oldDock.remove(); },
    makeCard, markBorn() {}, el: miniEl,
    pinToBottom() {}, stream() { return body; },
    planLater(_ui, fn, ms) { scheduled.push({ fn, ms }); },
    revealPlanItems() {}, sfx() {},
    dropStatus() { completionOrder.push('status'); },
    undockPlan() { completionOrder.push('plan'); },
    queueResponseFinish() { completionOrder.push('typing'); },
  });
  const ui = {
    node: { body }, statusEl: status, planDock: oldDock, planCard: oldCard,
    planItems: [], planList: null, planHome: null, planFinished: false,
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
  assert.deepStrictEqual(completionOrder, ['status', 'plan', 'typing'],
    'server completion must paint the plan before waiting for local response typing');
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
  const ctx = loadFunctions(['camPart', 'startCam', 'stopCam'], {
    S, CAM_TICK: 2500, ICO: { cam: '' },
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
  const eventCtx = loadFunctions(['handleEvent'], { S });
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
  assert(/const requestCamNode\s*=\s*camLive\(\)\s*\?\s*S\.camNode/.test(sendSource));
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
    setInterval, clearInterval,
    $: (selector) => selector === '#input' ? input : (selector === '#replyBar' ? reply : null),
    foldAllNotes() {}, camLive() { return true; }, stream() { return mainHost; },
    renderAttachments() { renderedAttachments = requestState.attachments.length; },
    autoGrow() {}, addUserMsg() { return new MiniNode('div'); },
    addAiMsg() { return { root: aiRoot, body: aiBody, modelEl: new MiniNode('span') }; },
    setStreaming(value) { requestState.streaming = value; }, sfx() {}, thinkMode() {},
    camFrame() { return 'data:image/jpeg;base64,frame'; },
    typerStop() {}, dropStatus() {}, undockPlan() { stoppedPlans += 1; },
    el: miniEl,
    settleVisualDone(ui) {
      ui.visualDone = true;
      if (ui.resolveVisualDone) ui.resolveVisualDone();
    },
    queueResponseFinish() { throw new Error('aborted upload must not finalize as a response'); },
    loadChats() {}, refreshState() {}, fetchReplies() {},
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

function testLiveInteractivePanelMountsBeforeStreamSettlement() {
  const scroller = new MiniNode('div'); scroller.className = 'cam-chat';
  scroller.scrollHeight = 1000; scroller.scrollTop = 500; scroller.clientHeight = 400;
  const node = new MiniNode('article');
  const body = new MiniNode('div'); node.body = body; node.appendChild(body); scroller.appendChild(node);
  const md = new MiniNode('div'); md.className = 'md'; body.appendChild(md);
  const panel = new MiniNode('div'); panel.className = 'ui-panel';
  panel.dataset.ui = 'tiles Формат: PDF | Word | Markdown'; md.appendChild(panel);
  const routeHint = new MiniNode('div'); routeHint.className = 'show';
  const frames = [];
  let settled = 0;
  const ctx = loadFunctions(['queueResponseFinish'], {
    String,
    S: { streamRun: 'run-live' },
    el: miniEl,
    $$: (selector, root) => root.querySelectorAll(selector),
    $: (selector) => selector === '#routeHint' ? routeHint : null,
    clearTypingDecorations() {}, foldCodeBlocks() {},
    mountUiPanels(root) {
      const live = root.querySelector('.ui-panel');
      live.dataset.live = '1';
      live.appendChild(new MiniNode('button'));
      scroller.scrollHeight = 1300;
    },
    addMsgActions() {}, speakReply() {}, sfx() {},
    thinkFlush() {}, collapseSoon() {}, undockPlan() {}, scrollDown() {},
    requestAnimationFrame(fn) { frames.push(fn); return frames.length; },
    settleVisualDone(ui) { ui.visualDone = true; settled += 1; },
    typerFlush(ui) { const done = ui.onTyped; ui.onTyped = null; done(); },
  });
  const ui = {
    runId: 'run-live', node, mdEl: md, buffer: 'Выбери формат', shown: 'Выбери формат',
    doneReceived: false, visualDone: false, replyUiSpec: '', thinkCard: null,
    planItems: [],
  };
  ctx.queueResponseFinish(ui, 'Выбери формат', false);
  assert.strictEqual(panel.dataset.live, '1',
    'the live response lifecycle itself must mount controls without reopening the chat');
  assert(panel.querySelector('button'), 'mounted controls must already exist before response settlement');
  assert.strictEqual(settled, 1);
  assert.strictEqual(frames.length, 1, 'panel growth must schedule one follow-to-bottom frame');
  frames[0]();
  assert.strictEqual(scroller.scrollTop, 1300,
    'a user following the answer must see the newly grown controls immediately');

  const finish = extractFunction(js, 'queueResponseFinish');
  assert(/const followPanel[\s\S]*mountUiPanels[\s\S]*requestAnimationFrame/.test(finish));
  assert(!/savedScroll|oldScroll|scrollTop\s*=\s*(?:before|old|saved)/i.test(finish),
    'mounting must preserve follow-at-bottom intent, not restore a stale scrollTop coordinate');
}

function testInteractiveFenceReachesFrontendPanel() {
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
  assert(/ui\.replyUiSpec\s*&&\s*!ui\.mdEl\.querySelector\('\.ui-panel'\)/.test(finish));
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

function testBrowserImageHandoffContract() {
  assert(!/js\.puter\.com\/v2\//.test(html),
    'Puter.js must be lazy-loaded only when image generation is actually requested');
  const loader = extractFunction(js, 'loadPuter');
  assert(/script\.src\s*=\s*['"]https:\/\/js\.puter\.com\/v2\/['"]/.test(loader));
  assert(/setTimeout\([\s\S]*20000/.test(loader), 'provider loading must have a finite timeout');
  assert(/puterLoadPromise/.test(js), 'parallel image events must share one SDK load');

  const handoff = extractFunction(js, 'handleBrowserImageRequest');
  assert(/S\.imageRequests\.has\(ev\.id\)/.test(handoff) && /S\.imageRequests\.add\(ev\.id\)/.test(handoff),
    'one in-flight browser request id must execute at most once');
  assert(/await\s+waitForPuterSignIn\(puter,\s*ui\)/.test(handoff));
  const signIn = extractFunction(js, 'waitForPuterSignIn');
  const click = signIn.slice(signIn.indexOf("addEventListener('click'"));
  assert(/await\s+puter\.auth\.signIn\(\)/.test(click),
    'Puter authentication must execute directly in the explicit user click callback');
  assert(/использует ресурсы[\s\S]{0,100}твоего аккаунта/.test(signIn),
    'the authorization card must explain the user-pays boundary honestly');

  assert(/puter\.ai\.txt2img/.test(handoff));
  assert(/model\s*:\s*String\(ev\.model\s*\|\|\s*['"]gpt-image-2['"]\)/.test(handoff));
  assert(/quality\s*:\s*String\(ev\.quality\s*\|\|\s*['"]medium['"]\)/.test(handoff));
  assert(/ratio\s*:\s*\{\s*w:[^}]*h:/.test(handoff));
  assert(/api\(['"]\/api\/images\/complete['"]/.test(handoff));
  assert(/\bid\s*:\s*ev\.id/.test(handoff));
  const events = extractFunction(js, 'handleEvent');
  assert(/case 'image_request':[\s\S]*handleBrowserImageRequest\(ev,\s*ui\)/.test(events),
    'SSE must dispatch browser generation immediately without blocking stream parsing');
}

function testThinkingGradientContract() {
  assert(!/\.think-card\.live \.think-stream::after\s*\{/.test(css),
    'the rejected narrow thinking beam must not return');
  assert(!/\.panel-card\.live \.card-head::after\s*\{/.test(css),
    'the rejected narrow tool-header beam must not return');

  const field = css.match(/\.panel-card\.live::before\s*\{([^}]*)\}/s);
  assert(field, 'one full-card layer must tint the complete thinking/tool body');
  assert(/inset\s*:\s*-2%/.test(field[1]) && !/width\s*:/.test(field[1]));
  assert(/rgba\(0,200,240,\.018\)/.test(field[1]) && /rgba\(136,105,211,\.032\)/.test(field[1]),
    'the background field must be almost imperceptible');
  assert(/will-change\s*:\s*transform,opacity/.test(field[1]));
  assert(/animation\s*:\s*wholeCardFlow\s+2s/.test(field[1]));

  const frame = css.match(/\.panel-card\.live::after\s*\{([^}]*)\}/s);
  assert(frame && /inset\s*:\s*0/.test(frame[1]) && /padding\s*:\s*1px/.test(frame[1]),
    'the animated gradient must cover the entire border ring');
  assert(/mask-composite\s*:\s*exclude/.test(frame[1]));
  assert(/animation\s*:\s*wholeFrameFlow\s+2s/.test(frame[1]));
  assert(/\.panel-card\.live \.think-stream\s*\{animation:wholeTextFlow 2s/.test(css),
    'text gets a separate, still delicate tint over the nearly invisible field');
  assert(/@keyframes wholeCardFlow[\s\S]*0%,4%[\s\S]*16%[\s\S]*54%[\s\S]*72%,100%\{opacity:0/s.test(css),
    'the pass must fade in, traverse quickly, fade out, then pause until the next cycle');

  const executableCss = css.replace(/\/\*[\s\S]*?\*\//g, '');
  assert(!/background-position\s*:/.test(executableCss),
    'no animated gradient may trigger background repaint frames');
  assert(!/stShimmer/.test(css), 'status text must not repaint a clipped gradient forever');
  assert(/function runStatus[\s\S]*q\.animate\(\[/.test(js),
    'status phrase transition uses compositor Web Animations instead of forced layout');
  assert(!/\.think-stream::(?:before|after)[^{]*\{[^}]*caret/s.test(css),
    'thinking stays a masked scrolling stream, not a cursor animation');
}

(async () => {
  testLiveStatusHasNoSpinner();
  testTelegramDateHudAndTimeOnlyMeta();
  testFileViewportAndCloseButton();
  testImportantHeadingCaretAndTrail();
  testPlanTypingCompletionAndDockRaces();
  testRepeatedPlanEventReplacesOwnership();
  await testCameraLifecycleOwnershipAndLateResults();
  testLiveInteractivePanelMountsBeforeStreamSettlement();
  testInteractiveFenceReachesFrontendPanel();
  testBrowserImageHandoffContract();
  testThinkingGradientContract();
  console.log('package28_frontend_runtime: 11 regression groups passed');
})().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
