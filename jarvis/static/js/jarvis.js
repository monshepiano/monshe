(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];

  const state = {
    view: "chat",
    agent: false,
    background: false,
    thinking: false,
    notifs: [],
    thoughts: [],
    files: [],
    missions: [],
    attachments: [],
    pendingConfirm: null,
    settings: {},
    unread: 0,
  };

  const feed = $("#feed");
  const idle = $("#idleCore");

  function pad(n) { return String(n).padStart(2, "0"); }
  function tickClock() {
    const d = new Date();
    $("#clock").textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
  setInterval(tickClock, 1000);
  tickClock();

  /* ---------- boot ---------- */
  const bootLines = [
    "INITIALIZING ARC CORE…",
    "NEURAL ORCHESTRATOR · GigaChat / DeepSeek",
    "SANDBOX ONLINE",
    "COMPUTER-USE STANDBY",
    "SAFETY LATCH ARMED",
    "WELCOME BACK",
  ];
  const bootLog = $("#bootLog");
  bootLines.forEach((t, i) => {
    setTimeout(() => {
      const d = document.createElement("div");
      d.textContent = "> " + t;
      bootLog.appendChild(d);
    }, 280 * i);
  });
  setTimeout(() => {
    $("#boot").classList.add("go");
    $("#app").classList.remove("hidden");
    greet();
    notify({
      level: "ok",
      title: "Контур онлайн",
      body: "Джарвис готов. Уведомления будут всплывать здесь сами.",
      auto_open: true,
    });
  }, 2200);

  function greet() {
    const h = new Date().getHours();
    const part = h < 6 ? "Доброй ночи" : h < 12 ? "Доброе утро" : h < 18 ? "Добрый день" : "Добрый вечер";
    $("#idleHello").textContent = `${part}. Чем помочь?`;
  }

  /* ---------- particles ---------- */
  const canvas = $("#particles");
  const ctx = canvas.getContext("2d");
  let dots = [];
  function resize() {
    canvas.width = innerWidth;
    canvas.height = innerHeight;
    dots = Array.from({ length: 48 }, () => ({
      x: Math.random() * canvas.width,
      y: Math.random() * canvas.height,
      v: 0.15 + Math.random() * 0.45,
      r: Math.random() * 1.4 + 0.3,
    }));
  }
  addEventListener("resize", resize);
  resize();
  (function loop() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "rgba(0,232,255,.55)";
    dots.forEach((p) => {
      p.y -= p.v;
      if (p.y < 0) p.y = canvas.height;
      ctx.globalAlpha = 0.35 + Math.sin(p.y / 40) * 0.2;
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fill();
    });
    requestAnimationFrame(loop);
  })();

  /* ---------- nav / panels ---------- */
  $$(".rail-btn").forEach((b) => {
    b.onclick = () => {
      $$(".rail-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.view = b.dataset.view;
      $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + state.view));
      if (state.view === "agent") renderMissions();
      if (state.view === "bg") renderBg();
      if (state.view === "sandbox") renderFiles();
    };
  });

  function openPanel(id) {
    $$(".panel").forEach((p) => p.classList.toggle("open", p.id === id));
  }
  function closePanels() { $$(".panel").forEach((p) => p.classList.remove("open")); }
  $("#btnThoughts").onclick = () => $("#panelThoughts").classList.toggle("open");
  $("#btnTerm").onclick = () => $("#panelTerm").classList.toggle("open");
  $("#btnBell").onclick = () => {
    $("#panelNotif").classList.toggle("open");
    state.unread = 0;
    updateBell();
  };
  $$("[data-close]").forEach((b) => (b.onclick = () => $("#" + b.dataset.close).classList.remove("open")));

  $("#modeSw").onclick = () => {
    state.agent = !state.agent;
    $("#modeSw").classList.toggle("on", state.agent);
    $("#modeSw").querySelector("span").textContent = state.agent ? "AGENT" : "CHAT";
    $("#pillMode").textContent = state.agent ? "AGENT" : "CHAT";
    if (state.agent) {
      $$(".rail-btn").find((b) => b.dataset.view === "agent")?.click();
      pushThought("Агентский контур включён. Задача уйдёт в шаги.");
    }
  };
  $("#bgCheck").onchange = (e) => (state.background = e.target.checked);

  /* ---------- render helpers ---------- */
  function showIdle(on) { idle.classList.toggle("hide", !on); }

  function md(text) {
    const esc = String(text || "")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    return esc
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/!\[(.*?)\]\((.*?)\)/g, '<img alt="$1" src="$2" />')
      .replace(/\[(.*?)\]\((https?:.*?)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  }

  function addMsg(role, html, extra) {
    showIdle(false);
    const el = document.createElement("div");
    el.className = "msg " + role;
    const who = role === "user" ? "YOU" : "JARVIS";
    el.innerHTML = `<div class="avatar">${role === "user" ? "YOU" : "J"}</div>
      <div class="bubble"><div class="who">${who}</div><div class="body">${html}</div>${extra || ""}</div>`;
    feed.appendChild(el);
    el.scrollIntoView({ behavior: "smooth", block: "end" });
    return el;
  }

  function setThinking(on) {
    state.thinking = on;
    $("#thinkRow")?.remove();
    if (!on) return;
    showIdle(false);
    const el = document.createElement("div");
    el.id = "thinkRow";
    el.className = "msg";
    el.innerHTML = `<div class="avatar">J</div><div class="bubble thinking"><div class="think-orb"></div> анализирую контур…</div>`;
    feed.appendChild(el);
    el.scrollIntoView({ behavior: "smooth", block: "end" });
  }

  function pushThought(text) {
    if (!text) return;
    state.thoughts.push(text);
    const d = document.createElement("div");
    d.className = "thought-line";
    d.textContent = text;
    $("#thoughtStream").prepend(d);
  }
  function pushTerm(text) {
    const d = document.createElement("div");
    d.className = "term-line";
    d.textContent = text;
    $("#termStream").prepend(d);
    $("#panelTerm").classList.add("open");
  }

  function notify(n) {
    const rec = {
      id: n.id || "n" + Date.now(),
      level: n.level || "info",
      title: n.title || "Джарвис",
      body: n.body || "",
      ts: n.ts || Date.now() / 1000,
      confirm_id: n.confirm_id,
    };
    state.notifs.unshift(rec);
    state.unread++;
    updateBell();
    renderNotifs();
    if (n.auto_open !== false) {
      $("#panelNotif").classList.add("open");
    }
  }
  function updateBell() {
    const b = $("#bellCount");
    b.textContent = String(state.unread);
    b.classList.toggle("on", state.unread > 0);
  }
  function renderNotifs() {
    const box = $("#notifList");
    box.innerHTML = state.notifs.slice(0, 40).map((n) => `
      <article class="n-card ${n.level || ""}">
        <h4>${escapeHtml(n.title)}</h4>
        <p>${escapeHtml(n.body)}</p>
      </article>`).join("") || `<p class="meta">тихо</p>`;
  }

  function escapeHtml(s) {
    return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function renderMissions() {
    const box = $("#missionGrid");
    if (!state.missions.length) {
      box.innerHTML = `<div class="block"><h3>Нет миссий</h3><p>Включите AGENT у поля ввода и отправьте задачу.</p></div>`;
      return;
    }
    box.innerHTML = state.missions.map((m) => `
      <article class="block">
        <h3>${escapeHtml(m.title || "миссия")}</h3>
        <div class="st ${m.status === "running" ? "run" : ""}">${m.status || "done"}</div>
        <div class="steps">${(m.steps || []).map((s) => "→ " + escapeHtml(s.name || s)).join("<br>")}</div>
      </article>`).join("");
  }
  function renderBg() {
    const box = $("#bgBlocks");
    const items = state.missions.filter((m) => m.background);
    box.innerHTML = items.length
      ? items.map((m) => `<article class="block"><h3>${escapeHtml(m.title)}</h3><p class="st">${m.status}</p></article>`).join("")
      : `<div class="block"><h3>Фон пуст</h3><p>Отметьте «в фон» под полем ввода.</p></div>`;
  }
  function renderFiles() {
    const box = $("#fileGrid");
    if (!state.files.length) {
      box.innerHTML = `<div class="block"><h3>Пусто</h3><p>Попросите Джарвиса создать файл или прикрепите свой.</p></div>`;
      return;
    }
    box.innerHTML = state.files.map((f) => `
      <a class="block file-card" href="/api/sandbox/${encodeURIComponent(f.path)}" target="_blank">
        <h3>${escapeHtml(f.path)}</h3>
        <p class="meta">${f.size} байт</p>
      </a>`).join("");
  }

  function suggest(items) {
    const box = $("#suggest");
    box.innerHTML = "";
    items.forEach((t) => {
      const b = document.createElement("button");
      b.className = "chip";
      b.textContent = t;
      b.onclick = () => { $("#input").value = t; send(); };
      box.appendChild(b);
    });
  }
  suggest(["Что ты умеешь?", "Найди новости про ИИ", "Нарисуй арк-реактор", "Запомни: я люблю краткий стиль"]);

  /* ---------- chat ---------- */
  const input = $("#input");
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(72, input.scrollHeight) + "px";
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });
  $("#btnSend").onclick = send;

  async function send(forced) {
    const text = (forced || input.value || "").trim();
    if (!text && !state.attachments.length) return;
    input.value = "";
    input.style.height = "auto";
    addMsg("user", escapeHtml(text).replace(/\n/g, "<br>"));
    setThinking(true);
    const payload = {
      text,
      agent: state.agent || state.view === "agent",
      background: state.background,
      attachments: state.attachments,
    };
    state.attachments = [];
    renderAtt();
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      setThinking(false);
      if (data.background) {
        addMsg("assistant", "Миссия ушла в фон. Панель уведомлений откроется, когда будет готово.");
        return;
      }
      paintAnswer(data);
    } catch (err) {
      setThinking(false);
      addMsg("assistant", "Канал оборвался. Я на месте — повторите приказ.");
    }
  }

  function paintAnswer(data) {
    let extra = "";
    if (data.steps && data.steps.length) {
      extra = `<div class="chips">${data.steps.map((s) => `<span class="chip">${escapeHtml(s.name)}</span>`).join("")}</div>`;
      state.missions.unshift({
        id: data.mission_id,
        title: (data.text || "").slice(0, 60),
        status: "done",
        steps: data.steps,
        background: data.background,
      });
    }
    const html = md(data.text || "…");
    addMsg("assistant", html, extra);
    if (data.model) $("#pillModel").textContent = "CORE · " + String(data.model).toUpperCase();
    if (state.settings.voice_out) speak(data.text || "");
    // if image path mentioned, try show
    const m = String(data.text || "").match(/([\w./-]+\.(?:png|jpg|jpeg|svg|webp))/i);
    if (m) {
      const url = "/api/sandbox/" + m[1].replace(/^\/+/, "");
      const last = feed.querySelector(".msg:last-child .body");
      if (last && !last.querySelector("img")) {
        const img = document.createElement("img");
        img.src = url;
        last.appendChild(img);
      }
    }
  }

  function speak(text) {
    if (!("speechSynthesis" in window)) return;
    const u = new SpeechSynthesisUtterance(String(text).slice(0, 400));
    u.lang = "ru-RU";
    u.rate = 1.04;
    speechSynthesis.cancel();
    speechSynthesis.speak(u);
  }

  /* ---------- files / voice / camera ---------- */
  $("#btnAttach").onclick = () => $("#filePick").click();
  $("#filePick").onchange = async (e) => {
    const fd = new FormData();
    [...e.target.files].forEach((f) => fd.append("file", f));
    const res = await fetch("/api/upload", { method: "POST", body: fd });
    const data = await res.json();
    (data.files || []).forEach((f) => state.attachments.push(f));
    renderAtt();
    refreshState();
    e.target.value = "";
  };
  function renderAtt() {
    $("#attachRow").innerHTML = state.attachments.map((a) => `<span class="att">${escapeHtml(a.name)}</span>`).join("");
  }

  const Speech = window.SpeechRecognition || window.webkitSpeechRecognition;
  let rec = null;
  if (Speech) {
    rec = new Speech();
    rec.lang = "ru-RU";
    rec.interimResults = true;
    rec.onresult = (ev) => {
      const t = [...ev.results].map((r) => r[0].transcript).join(" ");
      input.value = t;
      $("#listenHint").textContent = ev.results[0].isFinal ? "" : "слушаю…";
      if (ev.results[0].isFinal) send();
    };
    rec.onend = () => { $("#btnMic").classList.remove("gold"); $("#listenHint").textContent = ""; };
  }
  $("#btnMic").onclick = () => {
    if (!rec) { $("#listenHint").textContent = "Голос недоступен в этом браузере — возьмите Chrome / Safari."; return; }
    try { rec.start(); $("#btnMic").classList.add("gold"); $("#listenHint").textContent = "говорите"; } catch {}
  };

  $("#btnCamStart").onclick = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
      $("#cam").srcObject = stream;
    } catch {
      notify({ level: "warn", title: "Камера", body: "Нет доступа к камере", auto_open: true });
    }
  };
  $("#btnCamShot").onclick = () => {
    const v = $("#cam");
    const c = $("#camCanvas");
    if (!v.videoWidth) return;
    c.width = v.videoWidth; c.height = v.videoHeight;
    c.getContext("2d").drawImage(v, 0, 0);
    const data_url = c.toDataURL("image/jpeg", 0.85);
    state.attachments.push({ name: "camera.jpg", kind: "image", data_url });
    $$(".rail-btn").find((b) => b.dataset.view === "chat")?.click();
    send("Что на этом кадре? Если это товар или картина — опиши и предложи, где купить.");
  };

  /* ---------- confirm ---------- */
  function showConfirm(ev) {
    state.pendingConfirm = ev.confirm_id;
    $("#confirmTitle").textContent = "Подтвердите: " + (ev.tool || "действие");
    $("#confirmBody").textContent = ev.reason || "";
    $("#confirmArgs").textContent = JSON.stringify(ev.args || {}, null, 2);
    $("#confirmModal").classList.remove("hidden");
  }
  async function resolveConfirm(allow) {
    const id = state.pendingConfirm;
    $("#confirmModal").classList.add("hidden");
    if (!id) return;
    await fetch("/api/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, allow }),
    });
    state.pendingConfirm = null;
  }
  $("#confirmYes").onclick = () => resolveConfirm(true);
  $("#confirmNo").onclick = () => resolveConfirm(false);

  /* ---------- SSE ---------- */
  function connect() {
    const es = new EventSource("/api/events");
    es.onopen = () => {
      $("#pillLink").classList.remove("warn");
      $("#pillLink").innerHTML = "<i></i> LINK";
    };
    es.onerror = () => {
      $("#pillLink").classList.add("warn");
      $("#pillLink").innerHTML = "<i></i> HOLD";
    };
    es.onmessage = (m) => {
      let ev;
      try { ev = JSON.parse(m.data); } catch { return; }
      handle(ev);
    };
  }
  function handle(ev) {
    switch (ev.kind) {
      case "thought":
        pushThought(ev.text);
        if (!$("#panelThoughts").classList.contains("open") && state.thinking) {
          $("#panelThoughts").classList.add("open");
        }
        break;
      case "terminal":
        pushTerm(ev.text);
        break;
      case "notify":
        notify(ev);
        break;
      case "confirm":
        showConfirm(ev);
        break;
      case "status":
        if (ev.state === "thinking") setThinking(true);
        if (ev.state === "idle") setThinking(false);
        break;
      case "sandbox":
        refreshState();
        if (ev.action === "write" || ev.action === "upload") {
          notify({ level: "ok", title: "Песочница", body: ev.path || "файл", auto_open: false });
        }
        break;
      case "computer":
        if (ev.url) {
          $("#screenImg").src = ev.url + "?t=" + Date.now();
          $("#screenEmpty").style.display = "none";
          $$(".rail-btn").find((b) => b.dataset.view === "computer")?.classList.add("active");
        }
        break;
      case "image":
        if (ev.path) {
          const last = feed.querySelector(".msg:last-child .body") || addMsg("assistant", "").querySelector(".body");
          const img = document.createElement("img");
          img.src = "/api/sandbox/" + ev.path;
          last.appendChild(img);
        }
        break;
      case "mission":
        state.missions = [{ id: ev.mission_id, title: ev.title, status: ev.status, steps: ev.steps, background: ev.background }, ...state.missions.filter((x) => x.id !== ev.mission_id)];
        if (state.view === "agent") renderMissions();
        if (state.view === "bg") renderBg();
        break;
      case "orchestrator":
        $("#pillModel").textContent = "CORE · " + (ev.model || "AUTO").toUpperCase();
        pushThought(ev.text);
        break;
      case "assistant":
        // already painted in send() for foreground; for background paint now
        if (ev.background) paintAnswer(ev);
        break;
    }
  }
  connect();

  /* ---------- settings / state ---------- */
  async function refreshState() {
    try {
      const r = await fetch("/api/state");
      const d = await r.json();
      state.settings = d.raw_settings || d.settings || {};
      state.files = d.files || [];
      if (d.missions) state.missions = d.missions.concat(state.missions).slice(0, 40);
      if (state.settings.owner_name) {
        $("#ownerLabel").textContent = state.settings.owner_name;
        greet();
      }
      const form = $("#settingsForm");
      for (const [k, v] of Object.entries(state.settings)) {
        const el = form.elements[k];
        if (!el) continue;
        if (el.type === "checkbox") el.checked = !!v;
        else if (!el.value) el.value = v;
      }
      if (state.view === "sandbox") renderFiles();
    } catch {}
  }
  $("#settingsForm").onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const body = {};
    fd.forEach((v, k) => (body[k] = v));
    $$("#settingsForm input[type=checkbox]").forEach((c) => (body[c.name] = c.checked));
    await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    notify({ level: "ok", title: "Конфиг", body: "Сохранено", auto_open: true });
    refreshState();
  };
  refreshState();

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
})();
