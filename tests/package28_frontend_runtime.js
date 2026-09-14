'use strict';

/* Browser-free runtime contracts for the package 28 regressions.
   Run with: node tests/package28_frontend_runtime.js */

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..');
const js = fs.readFileSync(path.join(root, 'app/jarvis/web/js/app.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'app/jarvis/web/css/app.css'), 'utf8');
const html = fs.readFileSync(path.join(root, 'app/jarvis/web/index.html'), 'utf8');

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

function testLiveStatusHasNoSpinner() {
  const timers = [];
  const q = {
    textContent: '', offsetWidth: 10,
    classList: { remove() {}, add() {} },
  };
  const box = {
    className: '', markup: '',
    set innerHTML(value) { this.markup = value; },
    get innerHTML() { return this.markup; },
    querySelector(selector) { return selector === '.tw-quip' ? q : null; },
  };
  const ctx = loadFunctions(['runStatus'], {
    Math,
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

function testTimeAndFilesCssContracts() {
  assert(/\.msg-time\.two\.in-bubble\s*\{[^}]*display\s*:\s*flex/s.test(css),
    'old message date and time must keep separate flex rows');
  assert(/\.files-work\s*\{[^}]*flex-direction\s*:\s*column/s.test(css),
    'file preview terminal must be below the grid');
  assert(/\.files-work>\.term-block\[hidden\]\s*\{[^}]*display\s*:\s*none/s.test(css),
    'file terminal must remain absent until a file is opened');
  const close = html.match(/<button[^>]*id="termClose"[^>]*>([^<]*)<\/button>/);
  assert(close && close[1].trim() === '×', 'file close control must be a compact cross');

  const fileCard = extractFunction(js, 'fileCard');
  const click = fileCard.slice(fileCard.indexOf("c.addEventListener('click'"),
    fileCard.indexOf("c.addEventListener('dblclick'"));
  const executable = click.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
  assert(/\bclearSelection\s*\(\s*\)/.test(executable), 'direct open must clear selection');
  assert(!/\bselectOnly\s*\(/.test(executable), 'direct open must not select the card');
}

function testCompletedPlanAlwaysLeavesDock() {
  const timers = [];
  const title = { textContent: '' };
  const fill = { style: {} };
  const classes = new Set(['plan-dock', 'live']);
  const dock = {
    dataset: { runId: '28' }, isConnected: true, style: {}, listeners: {},
    classList: {
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
    },
    querySelector(selector) { return selector === '.pd-t' ? title : selector === '.pd-fill' ? fill : null; },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    remove() { this.isConnected = false; },
  };
  const li = { parentNode: {}, classList: { remove() {} } };
  const card = { isConnected: true, style: {} };
  let collapsed = false;
  const ctx = loadFunctions(['undockPlan'], {
    clearPlanTimers() {},
    $$(selector) { return selector === '.plan-dock' ? [dock] : []; },
    setTimeout(fn, ms) { timers.push({ fn, ms }); return timers.length; },
    collapseSoon() { collapsed = true; },
  });
  const ui = {
    runId: 28, planItems: [li], planList: {}, planDock: null,
    planCard: card, planHome: null,
  };
  ctx.undockPlan(ui);
  assert(classes.has('done'));
  assert.strictEqual(title.textContent, 'План выполнен');
  assert.strictEqual(fill.style.width, '100%');
  assert(collapsed, 'completed plan card must return as a thumbnail');
  assert(timers.some((timer) => timer.ms === 700),
    'completed caption must remain briefly readable before fly-out');
  while (timers.length) timers.shift().fn();
  assert.strictEqual(dock.isConnected, false, 'owned dock must be removed by fallback timer');

  const currentRule = css.match(/\.pd-s\.now::after\s*\{([^}]*)\}/s);
  assert(currentRule && /height\s*:\s*1px/.test(currentRule[1]));
  assert(currentRule && /border\s*:\s*0/.test(currentRule[1]),
    'current plan step uses a thin underline, not a frame over its caption');
  assert(/dock\.dataset\.runId\s*=\s*String\(ui\.runId\)/.test(js),
    'plan dock must carry per-response ownership');
}

function makeNode(video) {
  return {
    isConnected: true,
    classList: { add() {} },
    querySelector(selector) { return selector === '#cam' ? video : null; },
    querySelectorAll() { return []; },
  };
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

async function testCameraCanRestartAndCancelsLateMedia() {
  const toggle = { classList: { remove() {} } };
  const video = { srcObject: null };
  let nextMedia = makeMedia();
  let requests = 0;
  const S = {
    camRun: 0, camStream: null, camNode: makeNode(video), cameraOn: true,
    camTimer: null, camPrevPix: null, camBusy: false,
  };
  const ctx = loadFunctions(['startCam', 'stopCam'], {
    S, CAM_TICK: 2500, ICO: { cam: '' },
    navigator: { mediaDevices: { async getUserMedia() { requests += 1; return nextMedia; } } },
    showView() {}, killWelcome() {}, buildCamCard() { throw new Error('unexpected rebuild'); },
    stream() { throw new Error('unexpected stream lookup'); },
    addFoldButton() {}, scrollDown() {}, blip() {}, camState() {}, camSay() {},
    sfx() {}, toast() {}, camTick() {}, collapseToThumb() {},
    setInterval() { return 71; }, clearInterval() {},
    $(selector) { return selector === '#tgCamera' ? toggle : null; },
  });

  // A connected error/ended card is reusable. The old `if (S.camNode) return`
  // failed this exact case and never asked the browser for a stream again.
  await ctx.startCam();
  assert.strictEqual(requests, 1);
  assert.strictEqual(S.camStream, nextMedia);
  nextMedia.listeners.ended();
  assert.strictEqual(S.camStream, null);
  assert.strictEqual(S.cameraOn, false);

  S.cameraOn = true;
  nextMedia = makeMedia();
  await ctx.startCam();
  assert.strictEqual(requests, 2, 'camera must request media again after the first session ended');
  assert.strictEqual(S.camStream, nextMedia);
  ctx.stopCam();
  assert.strictEqual(nextMedia.track.stopped, 1);
  assert.strictEqual(S.camNode, null);
  assert.strictEqual(S.camBusy, false);

  // A getUserMedia result that resolves after stopCam must release its track.
  const lateVideo = { srcObject: null };
  S.camNode = makeNode(lateVideo);
  S.cameraOn = true;
  let resolveLate;
  const lateMedia = makeMedia();
  ctx.navigator.mediaDevices.getUserMedia = () => new Promise((resolve) => { resolveLate = resolve; });
  const pending = ctx.startCam();
  ctx.stopCam();
  resolveLate(lateMedia);
  await pending;
  assert.strictEqual(lateMedia.track.stopped, 1, 'late media stream must not leak after close');
  assert.strictEqual(S.camStream, null);
}

(async () => {
  testLiveStatusHasNoSpinner();
  testTimeAndFilesCssContracts();
  testCompletedPlanAlwaysLeavesDock();
  await testCameraCanRestartAndCancelsLateMedia();
  console.log('package28_frontend_runtime: 6 regression groups passed');
})().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
