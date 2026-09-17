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
  assert(/const CPS_CODE\s*=\s*420/.test(js) && /const CPS_SMOOTH_MS\s*=\s*340/.test(js));
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
  assert(/Math\.min\(32,\s*now\s*-\s*lastTick\)/.test(typer),
    'a delayed browser frame must not be paid back as a visible character burst');
  assert(/step\s*=\s*Math\.min\(step,\s*left,\s*code\s*\?\s*10\s*:\s*4\)/.test(typer));
  assert(!/left\s*>|boost|mult/i.test(typer),
    'typing acceleration must be smooth rather than a discrete backlog branch');
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
  const gateCtx = loadFunctions(
    ['beginPlanGate', 'dispatchStreamEvent', 'releasePlanGate', 'cancelPlanGate'],
    { Promise, handleEvent(ev) { handled.push(ev.type); } },
  );
  const planStatus = new MiniNode('div');
  const gated = { statusEl: planStatus };
  gateCtx.beginPlanGate(gated);
  assert.strictEqual(planStatus.style.display, 'none',
    'only the plan is visible during its typing and flight; the stable status node is held, not rebuilt');
  gateCtx.dispatchStreamEvent({ type: 'delta' }, gated);
  gateCtx.dispatchStreamEvent({ type: 'tool_start' }, gated);
  assert.deepStrictEqual(handled, [], 'no answer/tool content may appear while the plan is writing or flying');
  gateCtx.releasePlanGate(gated);
  assert.deepStrictEqual(handled, ['delta', 'tool_start'], 'the original SSE order must resume after arrival');
  assert.strictEqual(planStatus.style.display, '', 'the same status node returns only after the plan arrives');
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
    beginPlanGate(ui) { ui.planGate = true; },
    clearPlanTimers() { clears += 1; },
    dropStrayDocks(keep) { removedDocks.push(keep); oldDock.remove(); },
    makeCard, markBorn() {}, el: miniEl,
    pinToBottom() {}, stream() { return body; },
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
    watchRunFollow() {},
    camFrame() { return 'data:image/jpeg;base64,frame'; },
    typerStop() {}, dropStatus() {},
    cancelPlanGate() { stoppedPlans += 1; },
    async waitForPlanGate() {},
    discardPlan() {},
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
    ['deferMountedReplyUi', 'flushPendingReplyUi', 'typeInto', 'clearRunRoute', 'settleVisualDone'],
    {
      String, el: miniEl, S: { followUi: null },
      parseUiSpec() { return [{ t: 'tiles' }]; },
      hasMeaningfulUiItems(items) { return items.some((item) => item.t !== 'text' && item.t !== 'area'); },
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
  assert(/<div class="agent-switch"[^>]*>[\s\S]{0,120}<span class="agent-switch-label">AGENT<\/span>\s*<label class="agent-switch-track">\s*<input id="tgAgent" type="checkbox" role="switch"/.test(html),
    'AGENT caption must sit outside the only clickable checkbox track');
  assert(!/<button[^>]+id="tgAgent"/.test(html));
  assert(/\.toggle\s*\{[^}]*height:28px[^}]*padding:0 12px/s.test(css));
  assert(/\.agent-switch\s*\{[^}]*height:28px[^}]*display:flex[^}]*border:0[^}]*background:transparent/s.test(css) &&
    /\.agent-switch-track\s*\{[^}]*width:48px[^}]*height:28px/s.test(css) &&
    /\.send-btn\s*\{[^}]*width:48px[^}]*height:38px/s.test(css),
    'AGENT track stays 28px high and its 48px width aligns with the widened Send control');
  assert(/#tgAgent'\)\.addEventListener\('change'[\s\S]*S\.agentMode\s*=\s*this\.checked[\s\S]*tip-dismissed/.test(js));
  assert(/\.agent-switch'\)\.addEventListener\('mouseleave'[\s\S]*tip-dismissed/.test(js));
  const tipRule = css.match(/\.agent-switch\[data-tip\]:not\(\.tip-dismissed\):hover::after\s*\{([^}]*)\}/s);
  assert(/\.composer\s*\{[^}]*overflow:visible/s.test(css) && tipRule &&
    /z-index:60/.test(tipRule[1]) && /white-space:nowrap/.test(tipRule[1]),
  'switch tooltip must render above the composer, then stay hidden until mouseleave');

  const refresh = extractFunction(js, 'refreshState');
  assert(/st\.running_tasks/.test(refresh) && /auto-running', running > 0/.test(refresh),
    'AUTO shimmer is derived only from tasks actually running now');
  assert(!/auto-running', active > 0/.test(refresh),
    'queued or scheduled tasks must not animate AUTO');
  assert(/\.nav-item\[data-view="auto"\]\.auto-running \.nav-outline/.test(css) &&
    /animation:autoOutlineFlow/.test(css));
  assert(!/auto-running::after/.test(css), 'AUTO gradient must not fill the tab area');
  assert((html.match(/class="nav-outline"/g) || []).length === 5,
    'every navigation tab must own the same outline-only effect layer');

  const events = extractFunction(js, 'handleEvent');
  assert(/case 'memory_saved':[\s\S]*pulseNav\('memory', true\)/.test(events),
    'deterministic memory writes must trigger the visible memory outline');
  assert(/ev\.name === 'remember'\) pulseNav\('memory', true\)/.test(events),
    'model-driven memory writes get the same outline pulse');
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
  assert(/routeTier:\s*meta\.tier \|\| ''/.test(extractFunction(js, 'renderMessages')),
    'history restores the persisted scenario together with the model');

  const metaRoot = new MiniNode('div');
  const metaHead = new MiniNode('div'); metaHead.className = 'ai-name'; metaRoot.appendChild(metaHead);
  const legacyModel = new MiniNode('span'); legacyModel.textContent = 'old';
  const metaCtx = loadFunctions(['updateResponseMeta'], {
    TIER_LABEL: { quality: 'качество' },
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
  const followCtx = loadFunctions(['runScrollBox', 'watchRunFollow'], {
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
  assert(/pre\.live-code/.test(finish) && /classList\.remove\('live-code'\)/.test(finish) &&
    /foldCodeBlocks\(ui\.mdEl\)/.test(finish),
    'completed code leaves live mode and enters the existing fold/large-tab lifecycle');
  assert(/\.md pre\.live-code\s*\{[^}]*max-height:min\(42vh,360px\)[^}]*overflow:auto/s.test(css),
    'typing code remains in a bounded internally scrolling viewport');
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
  const task = {
    id: 'stable', title: 'Task', prompt: 'Work', status: 'running', progress: 0.4,
    result: '', schedule: '', next_run: 0, updated_at: 1, resume_status: '', events: [],
  };
  let paints = 0;
  const reconcileState = { tasks: [task], autoPaused: false };
  const reconcileCtx = loadFunctions(['taskFingerprint', 'renderTasks'], {
    S: reconcileState, Map, JSON, String,
    $$(selector, node) { return node.querySelectorAll(selector); },
    $(selector) {
      if (selector === '#taskGrid') return grid;
      if (selector === '.nav-item[data-view="auto"]') return autoNav;
      if (selector === '#autoPauseBtn') return pause;
      if (selector === '#clearDoneBtn') return clear;
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

  reconcileState.tasks = [{ ...task, progress: 0.8, updated_at: 2 }];
  reconcileCtx.renderTasks();
  assert.strictEqual(grid.firstElementChild, originalCard,
    'a changed task is patched in its keyed card instead of replacing the node');
  assert.strictEqual(paints, 1);

  assert(/id="clearDoneBtn"/.test(html) && /id="autoPauseBtn"/.test(html),
    'AUTO exposes clear-completed and global pause/play controls');
  assert(/\/api\/tasks\/clear-completed/.test(js) &&
    /S\.autoPaused \? '\/api\/tasks\/resume-all' : '\/api\/tasks\/pause-all'/.test(js));
  assert(/\.task-card\.paused::before\s*\{[^}]*rgba\(154,202,219,\.42\)/s.test(css),
    'paused cards retain a distinct calm state');
}

function testThinkingGradientContract() {
  const field = css.match(/\.panel-card\.live::before\s*\{([^}]*)\}/s);
  assert(field && /display\s*:\s*none/.test(field[1]),
    'live cards must have no animated interior field');

  const frame = css.match(/\.panel-card\.live::after\s*\{([^}]*)\}/s);
  assert(frame && /inset\s*:\s*-1px/.test(frame[1]) && /padding\s*:\s*1px/.test(frame[1]),
    'the animated gradient must cover the complete border ring');
  assert(/mask-composite\s*:\s*exclude/.test(frame[1]));
  assert(/opacity\s*:\s*\.58/.test(frame[1]) && /wholeFrameFlow 1\.25s/.test(frame[1]) &&
    /background-position:85% 0/.test(frame[1]),
  'reasoning outline must be colourful on the first frame and move immediately');
  assert(/@keyframes wholeFrameFlow\s*\{[\s\S]*0%\{opacity:\.58/.test(css));

  const toolFrame = css.match(/\.tool-card\.live::after\s*\{([^}]*)\}/s);
  assert(toolFrame && /rgba\(66,224,242,\.18\)/.test(toolFrame[1]) &&
    /rgba\(168,120,237,\.68\)/.test(toolFrame[1]) &&
    /rgba\(239,169,83,\.66\)/.test(toolFrame[1]) &&
    /background-size:290% 100%/.test(toolFrame[1]) && /toolFrameFlow 1\.18s/.test(toolFrame[1]),
  'Working contour must be wide, feathered, and restrained without filling the card');
  const textBand = css.match(/\.think-card\.live \.card-head,\.think-card\.live \.think-stream,[\s\S]*?\.tool-card\.live \.kv span\s*\{([^}]*)\}/s);
  assert(textBand && /color:transparent/.test(textBand[1]) && /background-clip:text/.test(textBand[1]) &&
    /#61e7ff 45\.2%/.test(textBand[1]) && /#718fff 46\.8%/.test(textBand[1]) &&
    /#ef72d0 50%/.test(textBand[1]) && /#65edc4 54\.8%/.test(textBand[1]) &&
    /liveTextFlow 8\.4s/.test(textBand[1]),
    'thinking and tool text receive a narrow, calm Apple-style colour band on the glyphs');
  assert(/@keyframes liveTextFlow/.test(css));
  assert(!/\.think-stream::(?:before|after)[^{]*\{[^}]*caret/s.test(css),
    'thinking stays a masked scrolling stream, not a cursor animation');

  const autoOutline = css.match(/\.nav-item\[data-view="auto"\]\.auto-running \.nav-outline\s*\{([^}]*)\}/s);
  assert(autoOutline && /background-size:390% 100%/.test(autoOutline[1]) &&
    /autoOutlineFlow 3\.8s cubic-bezier\(\.45,0,\.55,1\) infinite alternate/.test(autoOutline[1]) &&
    /drop-shadow\(0 0 8px/.test(autoOutline[1]) && /drop-shadow\(0 0 17px/.test(autoOutline[1]) &&
    (autoOutline[1].match(/rgba\(/g) || []).length >= 12,
    'running AUTO keeps a restrained feathered border gradient with a wider layered glow');
  const memoryOutline = css.match(/\.nav-item\.save-glint-strong \.nav-outline\s*\{([^}]*)\}/s);
  assert(memoryOutline && /#b477ff/.test(memoryOutline[1]) && /#ef79cf/.test(memoryOutline[1]) &&
    /memorySaveOutline 1\.35s/.test(memoryOutline[1]),
    'Memory save gets one conspicuous Apple-Intelligence-style border pass');

  const agentSwitch = css.match(/\.agent-switch\s*\{([^}]*)\}/s);
  const agentTrack = css.match(/\.agent-switch-track\s*\{([^}]*)\}/s);
  assert(agentSwitch && /border:0/.test(agentSwitch[1]) && /background:transparent/.test(agentSwitch[1]),
    'AGENT remains a real switch without an outer capsule');
  assert(agentTrack && /width:48px/.test(agentTrack[1]) && /height:28px/.test(agentTrack[1]) &&
    /border:1px solid var\(--line\)/.test(agentTrack[1]) && /background:transparent/.test(agentTrack[1]),
    'the off AGENT track matches the neutral neighbouring controls at 28px high');
  assert(/\.agent-switch-track:has\(input:checked\)\s*\{[^}]*rgba\(143,134,207,\.18\)/s.test(css),
    'the AGENT tint appears only on its inner track after the switch is enabled');
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
  console.log('package28_frontend_runtime: 13 regression groups passed');
})().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
