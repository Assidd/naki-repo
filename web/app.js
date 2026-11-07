/* CQA — интерфейс на чистом JavaScript. Все данные и расчёты — на сервере (/api). */
(() => {
  "use strict";

  const $view = document.getElementById("view");
  const state = { filters: { q: "", language: "", channel: "", status: "", critical: false, group: "" }, analyticsGroup: "", pollTimer: null, refocus: null, models: [] };
  const store = {
    get(k) { try { return localStorage.getItem("cqa:" + k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem("cqa:" + k, v); } catch { /* хранилище недоступно */ } },
  };
  state.model = store.get("model") || "";

  const LANG = { RU: "Русский", KZ: "Казахский", EN: "Английский" };
  const CHANNEL = { "agent chat": "Чат", "voice call": "Звонок" };
  const STATUS = {
    none: ["circle", "Не оценён", "", "Оценка ещё не запускалась"],
    queued: ["clock", "В очереди", "info", "Ждёт своей очереди на оценку"],
    running: ["refresh", "Оценивается", "info", "Модель сейчас оценивает диалог"],
    done: ["check", "Оценён", "good", "Оценка готова, все цитаты подтверждены"],
    needs_review: ["alert", "Проверить", "warn", "Часть цитат модели не найдена в диалоге — нужна проверка человеком"],
    failed: ["x", "Ошибка", "bad", "Модель не ответила; оценка не выставлена"],
  };
  const SPEAKER = { client: "Клиент", operator: "Оператор", bot: "Бот", system: "Система" };
  const TYPE = { yes_no: "Да / Нет", scale: "Шкала", flag: "Грубая ошибка" };

  // ---------- иконки (линейные SVG, 24×24) ----------
  const ICON_PATHS = {
    dialogs: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    analytics: '<path d="M3 3v18h18"/><path d="M7 15v3M12 10v8M17 6v12"/>',
    appeals: '<path d="M12 3v18M7 21h10M5 7h14"/><path d="M5 7l-3 7a3 3 0 0 0 6 0zM19 7l-3 7a3 3 0 0 0 6 0z"/>',
    checklist: '<path d="M10 6h11M10 12h11M10 18h11"/><path d="M3 6l1.5 1.5L7 5M3 12l1.5 1.5L7 11M3 18l1.5 1.5L7 17"/>',
    phone: '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 2.7a2 2 0 0 1-.5 2.1L8 9.8a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.5c.9.3 1.8.6 2.7.7a2 2 0 0 1 1.7 2z"/>',
    chat: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    download: '<path d="M12 3v12M7 10l5 5 5-5M5 21h14"/>',
    upload: '<path d="M12 21V9M7 14l5-5 5 5M5 3h14"/>',
    play: '<path d="M7 4l13 8-13 8z"/>',
    refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
    alert: '<path d="M12 3l10 18H2z"/><path d="M12 10v4M12 17.5v.5"/>',
    check: '<path d="M5 12l5 5 9-10"/>',
    x: '<path d="M6 6l12 12M18 6L6 18"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    circle: '<circle cx="12" cy="12" r="8"/>',
    back: '<path d="M15 6l-6 6 6 6"/>',
    trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 14h10l1-14M9 7V4h6v3"/>',
    send: '<path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4z"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 7.5v.5"/>',
    up: '<path d="M6 15l6-6 6 6"/>',
    down: '<path d="M6 9l6 6 6-6"/>',
  };
  const icon = (name) => `<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${ICON_PATHS[name] || ""}</svg>`;
  const NAV = [
    { id: "list", href: "#/", icon: "dialogs", title: "Диалоги", sub: "Загрузка и оценка" },
    { id: "analytics", href: "#/analytics", icon: "analytics", title: "Аналитика", sub: "Сводка по качеству" },
    { id: "appeals", href: "#/appeals", icon: "appeals", title: "Апелляции", sub: "Спорные оценки" },
    { id: "checklist", href: "#/checklist", icon: "checklist", title: "Чек-лист", sub: "Критерии и веса" },
  ];
  document.getElementById("nav").innerHTML = NAV.map((n) => `<a href="${n.href}" data-nav="${n.id}">${icon(n.icon)}<span><b>${n.title}</b><small>${n.sub}</small></span>${n.id === "appeals" ? '<span id="appeal-count" class="count" hidden></span>' : ""}</a>`).join("");
  const chan = (c) => (c === "voice call" ? `<span class="chan">${icon("phone")} Звонок</span>` : `<span class="chan">${icon("chat")} Чат</span>`);

  // ---------- утилиты ----------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (n, d = 2) => (n == null || n === "" ? "—" : Number(n).toLocaleString("ru-RU", { minimumFractionDigits: d, maximumFractionDigits: d }));

  async function api(path, opts = {}) {
    const init = { headers: { "Content-Type": "application/json" }, ...opts };
    if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
    const res = await fetch("/api" + path, init);
    let data = null;
    try { data = await res.json(); } catch { /* пустой ответ */ }
    if (!res.ok) {
      let msg = data && data.detail ? data.detail : `Ошибка ${res.status}`;
      if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join("; ");
      throw new Error(msg);
    }
    return data;
  }

  function toast(text, error = false) {
    const t = document.getElementById("toast");
    t.textContent = text;
    t.className = "toast" + (error ? " error" : "");
    t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (t.hidden = true), error ? 7000 : 3500);
  }

  // Подсказка при наведении на любой элемент с data-tip
  const $tip = document.getElementById("tooltip");
  document.addEventListener("mousemove", (e) => {
    const el = e.target.closest("[data-tip]");
    if (!el) { $tip.hidden = true; return; }
    $tip.textContent = el.dataset.tip;
    $tip.hidden = false;
    const x = Math.min(e.clientX + 14, window.innerWidth - $tip.offsetWidth - 8);
    const y = Math.min(e.clientY + 16, window.innerHeight - $tip.offsetHeight - 8);
    $tip.style.left = x + "px";
    $tip.style.top = y + "px";
  });

  function band(score) {
    if (score == null) return ["none", "нет итога"];
    const s = Number(score);
    if (s >= 80) return ["good", "хорошо"];
    if (s >= 50) return ["warn", "средне"];
    return ["bad", "плохо"];
  }
  function scorePill(score) {
    if (score == null) return '<span class="muted">—</span>';
    const [cls, label] = band(score);
    return `<span class="score-pill ${cls}"><b>${fmt(score)}</b><small>${label}</small></span>`;
  }
  function statusChip(status) {
    const [ic, label, cls, tip] = STATUS[status] || STATUS.none;
    return `<span class="chip ${cls}" data-tip="${esc(tip)}">${icon(ic)} ${esc(label)}</span>`;
  }
  function answerLabel(c, a) {
    if (a == null) return "—";
    if (a === "na") return "N/A";
    if (c && c.type === "flag") return a === "yes" ? "Нарушение" : "Нет нарушений";
    if (a === "yes") return "Да";
    if (a === "no") return "Нет";
    const top = c && c.scale ? Math.max(...c.scale.map((s) => Number(s.value))) : null;
    return top ? `${a} из ${top}` : a;
  }
  function answerClass(c, a) {
    if (a === "na") return "";
    if (c.type === "flag") return a === "yes" ? "bad" : "good";
    if (a === "yes") return "good";
    if (a === "no") return "bad";
    const top = Math.max(...c.scale.map((s) => Number(s.value)));
    const r = Number(a) / top;
    return r >= 0.99 ? "good" : r >= 0.5 ? "warn" : "bad";
  }
  function answerOptions(c) {
    const opts = c.type === "scale" ? c.scale.map((s) => String(s.value)) : ["yes", "no"];
    if (c.na_allowed) opts.push("na");
    return opts;
  }

  function stopPolling() { clearTimeout(state.pollTimer); state.pollTimer = null; }
  function poll(fn, ms = 2500) { stopPolling(); state.pollTimer = setTimeout(fn, ms); }

  // ---------- боковая панель ----------
  async function refreshHeader() {
    try {
      const s = await api("/status");
      state.models = s.llm.models || [];
      if (state.model && !state.models.some((m) => m.id === state.model)) state.model = "";
      const box = document.getElementById("model-status");
      if (!s.llm.configured) {
        box.innerHTML = '<span class="chip bad">Модель не настроена</span><br>Укажите OPENAI_API_KEY в .env';
      } else if (!box.querySelector("#sidebar-model")) {
        // Общий выбор модели для всех новых оценок; синхронизирован с выбором на страницах
        const fallback = s.llm.providers.slice(1).map((p) => esc(p.model)).join(", ");
        box.innerHTML = `<label for="sidebar-model">Модель оценки</label>${modelOptions("sidebar-model")}
          ${fallback ? `<div>Запасная: <b>${fallback}</b></div>` : ""}`;
        bindModelSelect("sidebar-model");
      } else {
        box.querySelector("#sidebar-model").value = state.model;
      }
      document.getElementById("env").innerHTML = `
        <span class="chip ${s.organizer_configured ? "good" : ""}" data-tip="${s.organizer_configured ? "Источник данных организаторов подключён" : "Ссылка на API организаторов не задана"}">${icon(s.organizer_configured ? "check" : "info")} API организаторов</span>
        <span class="chip ${s.llm.configured ? "good" : "bad"}">${icon(s.llm.configured ? "check" : "x")} Модель ИИ</span>`;
      const pill = document.getElementById("appeal-count");
      pill.hidden = !s.open_appeals;
      pill.textContent = s.open_appeals;
      return s;
    } catch { return null; }
  }

  // ---------- выбор модели ----------
  function modelOptions(id) {
    const price = (m) => (m.price ? ` · $${m.price.input_per_1m} / $${m.price.output_per_1m} за 1M` : "");
    const def = state.models.find((m) => m.default) || state.models[0];
    return `<select id="${id}" class="model-select"><option value="">По умолчанию (${esc(def.model)})</option>
      ${state.models.map((m) => `<option value="${esc(m.id)}" ${state.model === m.id ? "selected" : ""}>${esc(m.model)}${m.provider !== "openai" ? " (" + esc(m.provider) + ")" : ""}${price(m)}</option>`).join("")}</select>`;
  }
  function modelSelect(id) {
    if (!state.models.length) return "";
    return `<label class="field" data-tip="Модель для новых оценок (то же, что в левой панели). Цена ориентировочная: вход / выход за 1 млн токенов">Модель
      ${modelOptions(id)}</label>`;
  }
  function bindModelSelect(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("change", () => {
      state.model = el.value;
      store.set("model", el.value);
      // Все переключатели модели на экране показывают одно значение
      document.querySelectorAll(".model-select").forEach((s) => { if (s !== el) s.value = el.value; });
      const m = state.models.find((x) => x.id === el.value);
      toast("Новые оценки будут выполняться моделью " + (m ? m.model : "по умолчанию"));
    });
  }

  // ---------- сдача результатов ----------
  function resultsPanel(groups) {
    const g = state.resultsGroup || (groups.includes("DEV") ? "DEV" : groups[0] || "");
    const target = state.resultsTarget || (g === "DEV" ? "dev" : "prod");
    return `
      <div class="panel" id="results-panel">
        <div class="panel-head"><h2>Сдача результатов организаторам</h2><span class="small muted">формат JSON 1.0 из приложения к гайду</span></div>
        <p class="small muted" style="margin-top:-6px">Файл записывается в репозиторий: <span class="mono">results/dev/results.json</span> — DEV, <span class="mono">results/prod/results.json</span> — контрольный набор. После сохранения сделайте commit и push.</p>
        <div class="row">
          <label class="field">Набор<select id="res-group">${groups.map((x) => `<option ${x === g ? "selected" : ""}>${esc(x)}</option>`).join("")}</select></label>
          <label class="field">Куда<select id="res-target"><option value="dev" ${target === "dev" ? "selected" : ""}>results/dev</option><option value="prod" ${target === "prod" ? "selected" : ""}>results/prod</option></select></label>
          <label class="chk" style="align-self:flex-end;padding-bottom:8px" data-tip="Выключено: выгружается оценка модели. Включено: с учётом решений проверяющих по апелляциям.">
            <input type="checkbox" id="res-appeals" ${state.resultsAppeals ? "checked" : ""}> С учётом апелляций</label>
          <span class="grow"></span>
          <button class="btn" id="res-check" style="align-self:flex-end">Проверить</button>
          <button class="btn" id="res-download" style="align-self:flex-end">${icon("download")} Скачать JSON</button>
          <button class="btn primary" id="res-save" style="align-self:flex-end">${icon("send")} Сохранить в репозиторий</button>
        </div>
        <div id="res-out" class="small" style="margin-top:10px"></div>
      </div>`;
  }

  function bindResults(groups) {
    if (!groups.length) return;
    const val = () => ({ group: document.getElementById("res-group").value, target: document.getElementById("res-target").value, appeals: document.getElementById("res-appeals").checked });
    const remember = () => { const v = val(); state.resultsGroup = v.group; state.resultsTarget = v.target; state.resultsAppeals = v.appeals; };
    document.getElementById("res-group").addEventListener("change", (e) => {
      document.getElementById("res-target").value = e.target.value === "DEV" ? "dev" : "prod";
      remember();
    });
    ["res-target", "res-appeals"].forEach((id) => document.getElementById(id).addEventListener("change", remember));
    const out = document.getElementById("res-out");
    const report = (r, saved) => {
      const failed = r.problems.filter((p) => p.includes("status=error"));
      const errors = r.problems.filter((p) => !p.includes("status=error"));
      out.innerHTML = `
        <div class="banner ${r.problems.length ? "warn" : "info"}" style="margin:0">${icon(r.problems.length ? "alert" : "check")}<div>
          ${saved ? `<b>Сохранено: ${esc(saved)}</b><br>` : ""}Записей: <b>${r.total}</b>, оценено: <b>${r.ok}</b>, с ошибкой: <b>${r.total - r.ok}</b>.
          ${r.problems.length ? "" : " Проверка формата пройдена."}
          ${failed.length ? `<br>Не оценены (попадут в файл со status = "error"): ${failed.length}. Запустите их оценку и выгрузите заново.` : ""}
          ${errors.length ? `<ul>${errors.slice(0, 8).map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}
          ${saved ? `<div style="margin-top:6px">Дальше: <span class="mono">git add results &amp;&amp; git commit -m "Results" &amp;&amp; git push</span></div>` : ""}
        </div></div>`;
    };
    const fetchResults = (v) => api(`/results?group_id=${encodeURIComponent(v.group)}&use_appeals=${v.appeals}`);
    document.getElementById("res-check").addEventListener("click", async () => {
      try { report(await fetchResults(val())); } catch (err) { toast(err.message, true); }
    });
    document.getElementById("res-download").addEventListener("click", async () => {
      try {
        const r = await fetchResults(val());
        report(r);
        const blob = new Blob([JSON.stringify(r.payload, null, 2) + "\n"], { type: "application/json" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "results.json";
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 1000);
      } catch (err) { toast(err.message, true); }
    });
    document.getElementById("res-save").addEventListener("click", async () => {
      const v = val();
      try {
        const r = await api("/results/save", { method: "POST", body: { group_id: v.group, target: v.target, use_appeals: v.appeals } });
        report(r, r.path);
        toast("Файл " + r.path + " сохранён");
      } catch (err) { toast(err.message, true); }
    });
  }


  // ---------- список диалогов ----------
  async function viewList() {
    const [rows, status, checklists] = await Promise.all([api("/conversations"), refreshHeader(), api("/checklists")]);
    const f = state.filters;
    const groups = [...new Set(rows.map((r) => r.group_id))];
    const evaluated = rows.filter((r) => r.evaluation && ["done", "needs_review"].includes(r.evaluation.status)).length;
    const review = rows.filter((r) => r.evaluation && r.evaluation.status === "needs_review").length;
    const openAppeals = status ? status.open_appeals : 0;

    const filtered = rows.filter((r) => {
      const st = r.evaluation ? r.evaluation.status : "none";
      if (f.q && !(r.id + " " + r.preview).toLowerCase().includes(f.q.toLowerCase())) return false;
      if (f.language && r.language !== f.language) return false;
      if (f.channel && r.channel !== f.channel) return false;
      if (f.status && st !== f.status) return false;
      if (f.group && r.group_id !== f.group) return false;
      if (f.critical && !(r.evaluation && r.evaluation.critical)) return false;
      return true;
    });

    const step = (n, done, title, text, action) => `
      <div class="step ${done ? "done" : "todo"}"><span class="num">${done ? icon("check") : n}</span>
        <div><b>${title}</b><p>${text}</p>${action || ""}</div></div>`;

    $view.innerHTML = `
      <div class="page-head">
        <div><h1>Диалоги</h1><p class="lead">Загрузите разговоры, запустите оценку по чек-листу и откройте любой диалог, чтобы увидеть баллы, объяснения и реплики-доказательства.</p></div>
      </div>

      <div class="process">
        ${step(1, rows.length > 0, "Загрузите диалоги", rows.length ? `Загружено ${rows.length} (${groups.join(", ")})` : "Из API организаторов или JSON-файлом")}
        ${step(2, rows.length > 0 && evaluated === rows.length, "Оцените их", rows.length ? `Оценено ${evaluated} из ${rows.length}` : "Модель проверит каждый критерий")}
        ${step(3, rows.length > 0 && evaluated === rows.length && !review && !openAppeals, "Разберите спорные", `Проверить: ${review} · апелляций ждут решения: ${openAppeals}`,
          review ? `<button class="btn sm" id="show-review">Показать требующие проверки</button>` : openAppeals ? `<a class="btn sm" href="#/appeals">К апелляциям</a>` : "")}
      </div>

      <div class="panel">
        <div class="panel-head"><h2>Загрузка и запуск оценки</h2></div>
        <div class="row">
          <label class="field">Набор (group_id)<input id="group-id" type="text" value="DEV" size="10"></label>
          <button class="btn" id="fetch-btn" style="align-self:flex-end">${icon("download")} Загрузить из API организаторов</button>
          <button class="btn" id="import-toggle" style="align-self:flex-end" aria-expanded="false">${icon("upload")} Импорт из файлов</button>
          <span class="grow"></span>
          <label class="field">Версия чек-листа<select id="checklist-select">${checklists.map((c) => `<option value="${c.id}">v${c.version} — ${esc(c.comment)}</option>`).join("")}</select></label>
          ${modelSelect("model-select")}
          <button class="btn primary" id="batch-btn" style="align-self:flex-end">${icon("play")} ${state.reevaluate ? "Оценить все" : "Оценить неоценённые"}${f.group ? " в " + esc(f.group) : ""}</button>
        </div>
        <div class="row small" style="margin-top:8px;justify-content:flex-end">
          <label class="chk" data-tip="Запустить новую оценку и для уже оценённых диалогов — например, чтобы сравнить модели. Старые оценки сохраняются в истории.">
            <input type="checkbox" id="reevaluate" ${state.reevaluate ? "checked" : ""}> Переоценить и уже оценённые</label>
        </div>
        ${groups.length ? `<div class="datasets"><span class="small muted">Загруженные наборы:</span>
          ${groups.map((g) => `<span class="dataset"><b>${esc(g)}</b><span class="muted">${rows.filter((r) => r.group_id === g).length}</span>
            <button data-del="${esc(g)}" aria-label="Удалить набор ${esc(g)}" data-tip="Удалить набор ${esc(g)} вместе с оценками и апелляциями">${icon("trash")}</button></span>`).join("")}</div>` : ""}
        ${status && !status.organizer_configured ? '<p class="small muted">API организаторов не настроен: впишите ссылку GET в ORGANIZER_API_URL в файле .env. Пока можно загрузить диалоги из файлов.</p>' : ""}
        <div class="import-box" id="import-box" hidden>
          <h3>Импорт из файлов</h3>
          <p class="small muted">Форматы: <b>JSON</b> (как в API организаторов), <b>CSV / TSV</b> и <b>Excel (.xlsx)</b> — одна строка на реплику, <b>TXT</b> — строки вида «Оператор: текст». Можно выбрать несколько файлов сразу.
            <a href="/api/dataset/template.csv">Скачать шаблон CSV</a></p>
          <div class="row">
            <label class="field grow">Файлы<input id="import-files" type="file" multiple accept=".json,.csv,.tsv,.xlsx,.txt"></label>
            <label class="field">Набор<input id="import-group" type="text" value="MANUAL" size="10"></label>
            <label class="field" data-tip="Используется, если язык не указан в файле">Язык, если не указан
              <select id="import-lang"><option value="AUTO">Определить автоматически</option><option value="RU">Русский</option><option value="KZ">Казахский</option><option value="EN">Английский</option></select></label>
            <label class="field" data-tip="Используется, если канал не указан в файле">Канал, если не указан
              <select id="import-channel"><option value="agent chat">Чат</option><option value="voice call">Звонок</option></select></label>
            <button class="btn primary" id="import-btn" style="align-self:flex-end" disabled>Импортировать</button>
          </div>
          <details class="small" style="margin-top:8px"><summary>Как оформить файл</summary>
            <ul>
              <li><b>CSV / Excel:</b> первая строка — заголовки. Обязательны <span class="mono">speaker</span> (роль) и <span class="mono">text</span>; по желанию <span class="mono">conversation_id</span>, <span class="mono">timestamp</span>, <span class="mono">channel</span>, <span class="mono">language</span>, <span class="mono">audio_url</span>. Русские названия тоже подходят: «диалог», «роль», «текст», «время», «канал», «язык».</li>
              <li><b>Роли:</b> оператор / клиент / бот / система (или operator / client / bot / system, agent, customer).</li>
              <li><b>TXT:</b> каждая реплика с новой строки: «[00:12] Оператор: Здравствуйте!». Несколько диалогов в одном файле разделяйте строкой «### id-диалога». Без заголовка ID диалога = имя файла.</li>
              <li>Если в файле есть диалог с уже существующим ID, он обновится.</li>
            </ul>
          </details>
          <div id="import-result" class="small" style="margin-top:8px"></div>
        </div>
      </div>

      ${groups.length ? resultsPanel(groups) : ""}

      <div class="panel">
        <div class="filters">
          <input id="f-q" type="text" placeholder="Поиск по ID или тексту клиента" value="${esc(f.q)}">
          <select id="f-group" aria-label="Набор"><option value="">Все наборы</option>${groups.map((g) => `<option ${f.group === g ? "selected" : ""}>${esc(g)}</option>`).join("")}</select>
          <select id="f-language" aria-label="Язык"><option value="">Все языки</option>${Object.entries(LANG).map(([k, v]) => `<option value="${k}" ${f.language === k ? "selected" : ""}>${v}</option>`).join("")}</select>
          <select id="f-channel" aria-label="Канал"><option value="">Чаты и звонки</option>${Object.entries(CHANNEL).map(([k, v]) => `<option value="${k}" ${f.channel === k ? "selected" : ""}>${v}</option>`).join("")}</select>
          <select id="f-status" aria-label="Статус"><option value="">Любой статус</option>${Object.entries(STATUS).map(([k, v]) => `<option value="${k}" ${f.status === k ? "selected" : ""}>${v[1]}</option>`).join("")}</select>
          <label class="chk"><input id="f-critical" type="checkbox" ${f.critical ? "checked" : ""}> Только с грубыми ошибками</label>
        </div>
        <p class="small muted" style="margin:0 0 8px">Показано ${filtered.length} из ${rows.length}. Итог: <span style="color:var(--good)">●</span> 80–100 хорошо · <span style="color:var(--warn)">●</span> 50–79 средне · <span style="color:var(--bad)">●</span> ниже 50 плохо.</p>
        <div class="table-wrap">
        <table>
          <thead><tr><th>Диалог</th><th>Канал</th><th>Язык</th><th>Статус</th><th class="num">Итог</th><th>Грубая ошибка</th></tr></thead>
          <tbody>
            ${filtered.map((r) => {
              const e = r.evaluation;
              const changed = e && e.score !== e.original_score && e.original_score != null;
              return `<tr class="click" data-id="${esc(r.id)}">
                <td class="cell-title"><b>${esc(r.id)}</b><span>${esc(r.preview)}</span></td>
                <td>${chan(r.channel)}</td>
                <td>${esc(r.language)}</td>
                <td>${statusChip(e ? e.status : "none")}${e && e.open_appeals ? ' <span class="chip warn" data-tip="Подана апелляция, ждёт решения">${icon("appeals")} апелляция</span>' : ""}</td>
                <td class="num">${e ? scorePill(e.score) : '<span class="muted">—</span>'}${changed ? `<div class="small muted" data-tip="Итог изменён решением по апелляции">было ${fmt(e.original_score)}</div>` : ""}</td>
                <td>${e && e.critical ? `<span class="chip bad">${icon("alert")} Есть</span>` : e && e.critical === false ? '<span class="muted small">нет</span>' : ""}</td>
              </tr>`;
            }).join("") || `<tr><td colspan="6"><div class="empty">${rows.length ? "<b>Ничего не найдено</b>Измените или сбросьте фильтры." : "<b>Диалогов пока нет</b>Загрузите набор из API организаторов или импортируйте JSON-файл."}</div></td></tr>`}
          </tbody>
        </table>
        </div>
      </div>`;

    const bind = (id, key, prop = "value") => {
      document.getElementById(id).addEventListener(prop === "checked" ? "change" : "input", (e) => {
        state.filters[key] = e.target[prop];
        state.refocus = id;
        if (id === "f-q") { clearTimeout(bind._t); bind._t = setTimeout(render, 250); } else render();
      });
    };
    bind("f-q", "q"); bind("f-language", "language"); bind("f-channel", "channel"); bind("f-status", "status"); bind("f-group", "group"); bind("f-critical", "critical", "checked");

    $view.querySelectorAll("tr[data-id]").forEach((tr) => tr.addEventListener("click", () => (location.hash = "#/c/" + encodeURIComponent(tr.dataset.id))));
    document.getElementById("show-review")?.addEventListener("click", () => { state.filters.status = "needs_review"; render(); });

    document.getElementById("fetch-btn").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      btn.textContent = "Загрузка…";
      try {
        const r = await api("/dataset/fetch", { method: "POST", body: { group_id: document.getElementById("group-id").value } });
        toast(`Загружено ${r.total} диалогов (${r.group_id}): новых ${r.created}, обновлено ${r.updated}`);
        render();
      } catch (err) { toast(err.message, true); render(); }
    });

    const importBox = document.getElementById("import-box");
    const importToggle = document.getElementById("import-toggle");
    importBox.hidden = !state.importOpen;
    importToggle.setAttribute("aria-expanded", String(!!state.importOpen));
    importToggle.addEventListener("click", () => {
      state.importOpen = importBox.hidden;
      importBox.hidden = !importBox.hidden;
      importToggle.setAttribute("aria-expanded", String(!importBox.hidden));
    });
    if (state.importOpts) {
      document.getElementById("import-group").value = state.importOpts.group;
      document.getElementById("import-lang").value = state.importOpts.lang;
      document.getElementById("import-channel").value = state.importOpts.channel;
    }
    const filesInput = document.getElementById("import-files");
    const importBtn = document.getElementById("import-btn");
    filesInput.addEventListener("change", () => {
      importBtn.disabled = !filesInput.files.length;
      importBtn.textContent = filesInput.files.length > 1 ? `Импортировать ${filesInput.files.length} файла(ов)` : "Импортировать";
    });
    importBtn.addEventListener("click", async () => {
      const files = [...filesInput.files];
      if (!files.length) return;
      importBtn.disabled = true;
      const out = document.getElementById("import-result");
      const results = [];
      for (const file of files) {
        const q = new URLSearchParams({
          filename: file.name,
          group_id: document.getElementById("import-group").value || "MANUAL",
          language: document.getElementById("import-lang").value,
          channel: document.getElementById("import-channel").value,
        });
        try {
          const res = await fetch("/api/dataset/upload?" + q, { method: "POST", body: file, headers: { "Content-Type": "application/octet-stream" } });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error(data.detail || `Ошибка ${res.status}`);
          results.push({ ok: true, text: `${file.name}: ${data.total} диал. (новых ${data.created}, обновлено ${data.updated})` });
        } catch (err) {
          results.push({ ok: false, text: err.message });
        }
      }
      const okCount = results.filter((r) => r.ok).length;
      state.importOpen = true;
      state.importOpts = {
        group: document.getElementById("import-group").value,
        lang: document.getElementById("import-lang").value,
        channel: document.getElementById("import-channel").value,
      };
      state.lastImport = results;
      toast(okCount === files.length ? "Импорт завершён" : `Импортировано файлов: ${okCount} из ${files.length}`, okCount !== files.length);
      await render();
    });
    if (state.lastImport) {
      document.getElementById("import-result").innerHTML = state.lastImport.map((r) => `<div class="${r.ok ? "" : "error-text"}">${icon(r.ok ? "check" : "x")} ${esc(r.text)}</div>`).join("");
      state.lastImport = null;
    }

    bindModelSelect("model-select");
    document.getElementById("reevaluate").addEventListener("change", (e) => { state.reevaluate = e.target.checked; render(); });
    bindResults(groups);
    $view.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      const g = b.dataset.del;
      const n = rows.filter((r) => r.group_id === g).length;
      if (!confirm(`Удалить набор «${g}» (${n} диалогов) вместе с его оценками и апелляциями? Это действие нельзя отменить.`)) return;
      try {
        const r = await api("/datasets/" + encodeURIComponent(g), { method: "DELETE" });
        toast(`Набор «${r.group_id}» удалён: ${r.conversations} диалогов, ${r.evaluations} оценок`);
        if (state.filters.group === g) state.filters.group = "";
        if (state.resultsGroup === g) state.resultsGroup = "";
        render();
      } catch (err) { toast(err.message, true); }
    }));
    document.getElementById("batch-btn").addEventListener("click", async (e) => {
      e.currentTarget.disabled = true;
      try {
        const body = { checklist_id: Number(document.getElementById("checklist-select").value), only_unevaluated: !state.reevaluate, model: state.model || null };
        if (f.group) body.group_id = f.group;
        const r = await api("/evaluations/batch", { method: "POST", body });
        toast(r.started.length ? `Запущено оценок: ${r.started.length}` : "Все диалоги уже оценены этой версией чек-листа");
        render();
      } catch (err) { toast(err.message, true); render(); }
    });

    if (state.refocus) {
      const el = document.getElementById(state.refocus);
      state.refocus = null;
      if (el) { el.focus(); if (el.type === "text") el.setSelectionRange(el.value.length, el.value.length); }
    }
    if (rows.some((r) => r.evaluation && ["queued", "running"].includes(r.evaluation.status))) poll(render);
  }

  // Позиция реплики в записи звонка: время от начала без интервалов, когда запись стояла на паузе
  // (системные сообщения «Запись … приостановлены» / «… возобновлены»). Это оценка, а не точная синхронизация.
  function audioOffsets(transcript) {
    const out = {};
    let prev = null, total = 0, paused = false;
    for (const m of transcript) {
      const ts = Date.parse(m.timestamp);
      if (isNaN(ts)) continue;
      if (prev !== null && !paused) total += Math.max(0, (ts - prev) / 1000);
      prev = ts;
      out[m.id] = Math.round(total * 10) / 10;
      if (m.speaker === "system") {
        const t = m.text.toLowerCase();
        if (/приостанов|paused|тоқтат/.test(t)) paused = true;
        if (/возобнов|resumed|жалғас/.test(t)) paused = false;
      }
    }
    return out;
  }

  // ---------- карточка диалога ----------
  async function viewCard(cid, evalId) {
    const [conv, checklists] = await Promise.all([api("/conversations/" + encodeURIComponent(cid)), api("/checklists")]);
    const eid = evalId || conv.latest_evaluation_id;
    const ev = eid ? await api("/evaluations/" + eid) : null;
    const isCall = conv.channel === "voice call";
    const offsets = isCall ? audioOffsets(conv.transcript) : {};

    $view.innerHTML = `
      <a class="back" href="#/">${icon("back")} Все диалоги</a>
      <div class="page-head">
        <div><h1>${esc(conv.id)}</h1>
          <div class="row"><span class="chip">${chan(conv.channel)}</span><span class="chip">${LANG[conv.language] || esc(conv.language)}</span>
          <span class="chip">Набор ${esc(conv.group_id)}</span><span class="chip">${conv.transcript.length} реплик</span></div></div>
        <div class="row">
          <label class="field">Версия чек-листа<select id="run-checklist">${checklists.map((c) => `<option value="${c.id}" ${ev && ev.checklist_id === c.id ? "selected" : ""}>v${c.version} — ${esc(c.comment)}</option>`).join("")}</select></label>
          ${modelSelect("card-model")}
          <button class="btn primary" id="run-btn" style="align-self:flex-end" ${ev && ["queued", "running"].includes(ev.status) ? "disabled" : ""}>${ev ? icon("refresh") + " Оценить заново" : icon("play") + " Запустить оценку"}</button>
        </div>
      </div>
      ${conv.evaluations.length > 1 ? `<div class="row small" style="margin:-6px 0 12px"><span class="muted">История оценок:</span>
        <select id="history">${conv.evaluations.map((e) => `<option value="${e.id}" ${ev && e.id === ev.id ? "selected" : ""}>#${e.id} · ${esc(e.created_at.slice(0, 16).replace("T", " "))} · ${STATUS[e.status]?.[1] || e.status}${e.score ? " · " + e.score : ""}</option>`).join("")}</select></div>` : ""}
      <div class="card-grid">
        <section id="result"></section>
        <section class="panel transcript" id="transcript">
          <div class="panel-head"><h2>Транскрипт</h2><span class="small muted">клик по цитате слева — переход к реплике</span></div>
          <div class="who-legend"><span><i style="background:var(--brand-soft)"></i>Оператор</span><span><i style="background:var(--surface-2)"></i>Клиент</span><span><i></i>Бот и система — контекст</span></div>
          ${isCall && conv.audio_url ? `
            <div style="margin-bottom:10px">
              <audio id="audio" controls preload="none" src="/api/conversations/${encodeURIComponent(conv.id)}/audio" style="width:100%"></audio>
              <div class="small muted">Клик по цитате перематывает запись к реплике (приблизительно). <a href="${esc(conv.audio_url)}" target="_blank" rel="noopener">Плеер организаторов</a></div>
            </div>` : ""}
          ${conv.transcript.map((m) => `
            <div class="msg ${esc(m.speaker)}" id="msg-${esc(m.id)}" data-offset="${offsets[m.id] ?? ""}">
              <div class="id">${esc(m.id)}</div>
              <div><span class="who">${SPEAKER[m.speaker] || esc(m.speaker)}</span><span class="ts">${esc(isCall ? m.timestamp.slice(11, 19) : m.timestamp)}</span><div class="text">${esc(m.text)}</div></div>
            </div>`).join("")}
        </section>
      </div>`;

    const $result = document.getElementById("result");
    if (!ev) {
      $result.innerHTML = '<div class="panel empty"><b>Диалог ещё не оценён</b>Выберите версию чек-листа и нажмите «Запустить оценку».</div>';
    } else if (["queued", "running"].includes(ev.status)) {
      $result.innerHTML = `<div class="panel empty">${statusChip(ev.status)}<p>Модель оценивает диалог. Страница обновится сама.</p></div>`;
      poll(() => viewCard(cid, ev.id));
    } else if (ev.status === "failed") {
      $result.innerHTML = `<div class="panel">${statusChip("failed")}<p>${esc(ev.error)}</p><p class="small muted">Ошибка модели не превращается в нулевую оценку. Исправьте настройку и запустите оценку заново.</p></div>`;
    } else {
      $result.innerHTML = renderEvaluation(ev);
      bindEvaluation(ev, cid);
    }

    bindModelSelect("card-model");
    document.getElementById("run-btn").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      try {
        const r = await api("/evaluations", { method: "POST", body: { conversation_id: cid, checklist_id: Number(document.getElementById("run-checklist").value), model: state.model || null } });
        viewCard(cid, r.id);
      } catch (err) { toast(err.message, true); btn.disabled = false; }
    });
    const hist = document.getElementById("history");
    if (hist) hist.addEventListener("change", () => viewCard(cid, Number(hist.value)));
  }

  function renderEvaluation(ev) {
    const cl = ev.checklist.data;
    const final = ev.final || {};
    const sectionTitles = Object.fromEntries((cl.sections || []).map((s) => [s.id, s.title]));
    const appealsBy = Object.fromEntries(ev.appeals.map((a) => [a.criterion_id, a]));
    const scoreChanged = final.score !== ev.score;
    const [bcls, blabel] = band(final.score);

    // Критерии группируем по разделам чек-листа, в порядке чек-листа
    const order = [];
    for (const c of cl.criteria) {
      const sid = c.section || "";
      let g = order.find((x) => x.id === sid);
      if (!g) order.push((g = { id: sid, items: [] }));
      g.items.push(c);
    }

    return `
      <div class="panel">
        <div class="summary">
          <div class="score-ring ${bcls}" style="--p:${final.score ?? 0}" role="img" aria-label="Итог ${final.score ?? "нет"} из 100">
            <div><span><b>${final.score != null ? fmt(final.score) : "—"}</b><small>из 100 · ${blabel}</small></span></div>
          </div>
          <div class="facts">
            <div class="row">${statusChip(ev.status)}${final.critical ? `<span class="chip bad">${icon("alert")} Грубая ошибка</span>` : '<span class="chip good">Грубых ошибок нет</span>'}</div>
            ${scoreChanged ? `<div class="small">Исходная оценка модели: <b>${fmt(ev.score)}</b> → после апелляций: <b>${fmt(final.score)}</b></div>` : ""}
            <div class="small muted">Чек-лист v${ev.checklist.version} · ${esc(ev.model || "")}</div>
            <div class="small muted">Токены ${ev.tokens_in ?? 0} / ${ev.tokens_out ?? 0}${ev.cost_usd != null ? ` · стоимость ≈ $${Number(ev.cost_usd).toFixed(4)}` : ""}</div>
            ${Object.keys(final.sections || {}).length ? `<div class="sections">${Object.entries(final.sections).map(([k, v]) => `
              <div class="hbar" data-tip="${esc(sectionTitles[k] || k)}: ${v}% от максимума раздела"><div class="name small">${esc(sectionTitles[k] || k)}</div>
              <div class="track"><div class="fill" style="width:${v}%"></div></div><div class="val">${fmt(v, 0)}%</div></div>`).join("")}</div>` : ""}
          </div>
        </div>
        ${final.critical ? `<div class="banner bad">${icon("alert")}<div><b>Обнаружена грубая ошибка.</b> Она отмечается отдельно и не входит в баллы — см. раздел «Грубые нарушения».</div></div>` : ""}
        ${ev.status === "needs_review" ? `<div class="banner warn">${icon("alert")}<div><b>Нужна проверка человеком.</b> Часть доказательств модели не нашлась в тексте диалога. Баллы показаны, но проверьте отмеченные критерии.<ul>${ev.issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul></div></div>` : ""}
      </div>

      <details class="help"><summary>Как читать оценку</summary><ul>
        <li><b>Баллы критерия</b> — сколько он принёс в итог из 100 с учётом перераспределения веса.</li>
        <li><b>N/A</b> — критерий неприменим к этому диалогу; его вес распределён между остальными.</li>
        <li><b>Цитата</b> — реплика, на которой основан вывод. Нажмите на неё, чтобы перейти к ней в транскрипте. Красная полоса — цитата не найдена в диалоге.</li>
        <li><b>Проверенный участок</b> — где ожидалось пропущенное действие.</li>
        <li>Не согласны с оценкой — нажмите «Оспорить» у критерия.</li>
      </ul></details>

      <div class="panel">
        <h2>Оценка по критериям</h2>
        ${order.map((g) => `
          ${g.id ? `<div class="section-title">${esc(sectionTitles[g.id] || g.id)}${final.sections && final.sections[g.id] != null ? ` · ${fmt(final.sections[g.id], 0)}%` : ""}</div>` : ""}
          ${g.items.map((c) => renderCriterion(c, ev.results[c.id], final, appealsBy[c.id])).join("")}`).join("")}
      </div>`;
  }

  function renderCriterion(c, r, final, appeal) {
    if (!r) return "";
    const fa = final.answers ? final.answers[c.id] : r.answer;
    const fc = (final.criteria || {})[c.id] || {};
    const changed = fa !== r.answer;
    const pct = fc.share != null && Number(fc.max_share) ? (Number(fc.share) / Number(fc.max_share)) * 100 : null;
    return `
      <div class="crit ${changed ? "changed" : ""} ${c.type === "flag" && fa === "yes" ? "flagged" : ""}" id="crit-${esc(c.id)}">
        <div class="crit-head">
          <div><h3>${esc(c.title)}</h3><span class="meta">${TYPE[c.type]}${c.type !== "flag" ? " · вес " + esc(c.weight) : ""}</span></div>
          <div class="crit-score">
            ${changed ? `<s class="small muted">${esc(answerLabel(c, r.answer))}</s> ` : ""}<span class="chip ${answerClass(c, fa)}">${esc(answerLabel(c, fa))}</span>
            ${c.type !== "flag" ? `<span class="pts">${fc.share != null ? `${fmt(fc.share)} из ${fmt(fc.max_share)} б.` : "не учитывается (N/A)"}</span>` : ""}
          </div>
        </div>
        ${pct != null ? `<div class="track" data-tip="Выполнено на ${fmt(pct, 0)}%"><div class="fill" style="width:${pct}%"></div></div>` : ""}
        <p>${esc(r.explanation)}</p>
        ${r.evidence.length ? `<ul class="evidence">${r.evidence.map((e) => `<li><button class="quote ${e.verified ? "" : "unverified"}" data-msg="${esc(e.message_id)}" data-quote="${esc(e.quote)}" data-tip="${e.verified ? "Цитата найдена в реплике — нажмите, чтобы перейти" : "Цитата НЕ найдена в реплике"}"><span class="mono">${esc(e.message_id)}</span>«${esc(e.quote)}»</button></li>`).join("")}</ul>` : ""}
        ${r.checked_range ? `<div class="small" style="margin-top:6px">Проверенный участок: <button class="btn link range" data-range="${esc(r.checked_range)}">${esc(r.checked_range)}</button></div>` : ""}
        ${appeal ? renderAppealInfo(c, appeal) : `<div class="crit-actions"><button class="btn sm ghost appeal-open" data-crit="${esc(c.id)}">${icon("appeals")} Оспорить</button></div>
          <div class="appeal-box" id="appeal-form-${esc(c.id)}" hidden>
            <textarea placeholder="Почему вы не согласны с оценкой? Проверяющий увидит этот комментарий."></textarea>
            <div class="row" style="margin-top:6px"><button class="btn primary sm appeal-send" data-crit="${esc(c.id)}">Подать апелляцию</button></div>
          </div>`}
      </div>`;
  }

  function renderAppealInfo(c, a) {
    if (a.status === "open") {
      return `<div class="appeal-box"><span class="chip warn">${icon("appeals")} Апелляция на рассмотрении</span> <span class="small">${esc(a.comment)}</span>
        <div class="small" style="margin-top:4px"><a href="#/appeals/${a.id}">Открыть рассмотрение →</a></div></div>`;
    }
    return `<div class="appeal-box"><span class="chip ${a.decision === "change" ? "warn" : "good"}">${a.decision === "change" ? "Оценка изменена проверяющим" : "Оценка подтверждена проверяющим"}</span>
      <div class="small" style="margin-top:4px"><b>Апелляция:</b> ${esc(a.comment)}</div>
      <div class="small"><b>Решение:</b> ${a.decision === "change" ? `новый ответ «${esc(answerLabel(c, a.new_answer))}». ` : ""}${esc(a.reason)}</div></div>`;
  }

  function highlightMessage(id, quote) {
    document.querySelectorAll(".msg.hl").forEach((m) => m.classList.remove("hl"));
    document.querySelectorAll(".msg mark").forEach((mk) => mk.replaceWith(mk.textContent));
    const el = document.getElementById("msg-" + id);
    if (!el) return toast("Реплика " + id + " не найдена", true);
    el.classList.add("hl");
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    const textEl = el.querySelector(".text");
    if (quote) {
      const idx = textEl.textContent.toLowerCase().indexOf(quote.toLowerCase());
      if (idx >= 0) {
        const t = textEl.textContent;
        textEl.innerHTML = esc(t.slice(0, idx)) + "<mark>" + esc(t.slice(idx, idx + quote.length)) + "</mark>" + esc(t.slice(idx + quote.length));
      }
    }
    const audio = document.getElementById("audio");
    const offset = Number(el.dataset.offset);
    if (audio && el.dataset.offset !== "" && !isNaN(offset)) {
      try {
        const max = isFinite(audio.duration) ? audio.duration - 1 : Infinity;
        audio.currentTime = Math.max(0, Math.min(offset - 1, max));
        audio.play().catch(() => {});
      } catch { /* плеер недоступен */ }
    }
  }

  function bindEvaluation(ev, cid) {
    $view.querySelectorAll(".quote").forEach((q) => q.addEventListener("click", () => highlightMessage(q.dataset.msg, q.dataset.quote)));
    $view.querySelectorAll(".range").forEach((b) => b.addEventListener("click", () => {
      document.querySelectorAll(".msg.range").forEach((m) => m.classList.remove("range"));
      const ids = (b.dataset.range.match(/m\d+/g) || []).map((s) => Number(s.slice(1)));
      const [from, to] = [Math.min(...ids), Math.max(...ids)];
      for (let i = from; i <= to; i++) document.getElementById("msg-m" + i)?.classList.add("range");
      highlightMessage("m" + from, null);
    }));
    $view.querySelectorAll(".appeal-open").forEach((b) => b.addEventListener("click", () => {
      const form = document.getElementById("appeal-form-" + b.dataset.crit);
      form.hidden = !form.hidden;
      if (!form.hidden) form.querySelector("textarea").focus();
    }));
    $view.querySelectorAll(".appeal-send").forEach((b) => b.addEventListener("click", async () => {
      const comment = document.querySelector(`#appeal-form-${b.dataset.crit} textarea`).value;
      try {
        await api("/appeals", { method: "POST", body: { evaluation_id: ev.id, criterion_id: b.dataset.crit, comment } });
        toast("Апелляция подана и ждёт проверяющего");
        refreshHeader();
        viewCard(cid, ev.id);
      } catch (err) { toast(err.message, true); }
    }));
  }

  // ---------- аналитика ----------
  function hbars(items, { max = 100, unit = "%", digits = 0 } = {}) {
    return `<div class="hbars">${items.map((it) => {
      const na = it.value == null;
      const w = na ? 0 : Math.max(0, Math.min(100, (it.value / max) * 100));
      return `<div class="hbar ${na ? "na" : ""}" data-tip="${esc(it.tip || "")}">
        <div class="name">${esc(it.name)}${it.sub ? `<small>${esc(it.sub)}</small>` : ""}</div>
        <div class="track"><div class="fill" style="width:${w}%"></div></div>
        <div class="val">${na ? "—" : fmt(it.value, digits) + unit}${it.note ? `<small>${esc(it.note)}</small>` : ""}</div>
      </div>`;
    }).join("")}</div>`;
  }

  async function viewAnalytics() {
    refreshHeader();
    const g = state.analyticsGroup;
    const a = await api("/analytics" + (g ? "?group_id=" + encodeURIComponent(g) : ""));
    const st = a.statuses;
    const evaluated = st.done + st.needs_review;
    const maxBucket = Math.max(1, ...a.distribution.map((d) => d.count));
    const scored = a.distribution.reduce((s, d) => s + d.count, 0);
    const weighted = a.criteria.filter((c) => c.type !== "flag");
    const flags = a.criteria.filter((c) => c.type === "flag");

    $view.innerHTML = `
      <div class="page-head">
        <div><h1>Аналитика</h1><p class="lead">Сводка по последней оценке каждого диалога с учётом решений по апелляциям. Помогает увидеть, где операторы теряют баллы чаще всего.</p></div>
        <label class="field">Набор<select id="a-group"><option value="">Все наборы</option>${a.groups.map((x) => `<option ${g === x ? "selected" : ""}>${esc(x)}</option>`).join("")}</select></label>
      </div>

      ${evaluated === 0 ? `<div class="panel empty"><b>Пока нет оценённых диалогов</b>Запустите оценку на странице «Диалоги» — здесь появится сводка.</div>` : `
      <div class="kpis">
        <div class="kpi"><div class="label">Оценено диалогов</div><div class="value">${evaluated}<small> из ${a.total}</small></div><div class="note">${st.none ? `${st.none} ещё не оценены` : "все диалоги оценены"}</div></div>
        <div class="kpi"><div class="label">Средний итоговый балл</div><div class="value">${fmt(a.avg_score)}</div><div class="note">медиана ${fmt(a.median_score)}</div></div>
        <div class="kpi ${a.critical.length ? "alert" : ""}"><div class="label">С грубыми ошибками</div><div class="value">${a.critical.length}</div><div class="note">${evaluated ? fmt((a.critical.length / evaluated) * 100, 1) + "% диалогов" : ""}</div></div>
        <div class="kpi"><div class="label">Требуют проверки</div><div class="value">${st.needs_review}</div><div class="note">${st.failed ? `ошибок модели: ${st.failed}` : "цитаты не подтвердились"}</div></div>
        <div class="kpi"><div class="label">Апелляции</div><div class="value">${a.appeals.total}</div><div class="note">ждут: ${a.appeals.open} · изменено: ${a.appeals.changed} · подтверждено: ${a.appeals.confirmed}</div></div>
        <div class="kpi"><div class="label">Стоимость оценки</div><div class="value">$${fmt(a.cost.total_usd, 3)}</div><div class="note">≈ $${fmt(a.cost.per_evaluation_usd, 4)} за диалог</div></div>
      </div>

      <div class="charts">
        <div class="panel">
          <div class="panel-head"><h2>Где теряются баллы</h2><span class="small muted">выполнение критерия, % от максимума</span></div>
          ${hbars(weighted.map((c) => ({
            name: c.title, sub: `вес ${c.weight}`, value: c.fulfilment,
            note: c.na ? `N/A: ${c.na}` : "",
            tip: `${c.title}: в среднем ${fmt(c.fulfilment, 1)}% от максимума · оценено ${c.evaluated - c.na}, N/A ${c.na}`,
          })))}
          <p class="legend-note">Чем короче полоса, тем чаще операторы не выполняют критерий. N/A (неприменим) в процент не входит.</p>
        </div>

        <div class="panel">
          <div class="panel-head"><h2>Распределение итоговых баллов</h2><span class="small muted">число диалогов</span></div>
          <div class="vbars">${a.distribution.map((d) => `
            <div class="vbar" data-tip="Итог ${d.label}: ${d.count} диал. (${scored ? fmt((d.count / scored) * 100, 0) : 0}%)">
              <span class="v">${d.count}</span><div class="col" style="height:${(d.count / maxBucket) * 100}%"></div></div>`).join("")}</div>
          <div class="vlabels">${a.distribution.map((d) => `<span>${d.label}</span>`).join("")}</div>
          <p class="legend-note">Диалоги без итога (все критерии N/A) сюда не входят.</p>
        </div>

        <div class="panel">
          <div class="panel-head"><h2>Средний балл по языкам</h2></div>
          ${hbars(a.by_language.map((x) => ({ name: LANG[x.key] || x.key, sub: `${x.count} диал.`, value: x.avg, tip: `${LANG[x.key] || x.key}: средний итог ${fmt(x.avg)} по ${x.count} диалогам` })), { unit: "", digits: 1 })}
          <div class="section-title">По каналам</div>
          ${hbars(a.by_channel.map((x) => ({ name: CHANNEL[x.key] || x.key, sub: `${x.count} диал.`, value: x.avg, tip: `${CHANNEL[x.key] || x.key}: средний итог ${fmt(x.avg)} по ${x.count} диалогам` })), { unit: "", digits: 1 })}
        </div>

        <div class="panel">
          <div class="panel-head"><h2>Грубые ошибки</h2><span class="small muted">${flags.map((f) => esc(f.title)).join(", ")}</span></div>
          ${a.critical.length ? `<div class="table-wrap"><table><thead><tr><th>Диалог</th><th>Что найдено</th><th class="num">Итог</th></tr></thead><tbody>
            ${a.critical.map((c) => `<tr class="click" data-id="${esc(c.conversation_id)}"><td><b>${esc(c.conversation_id)}</b><div class="small muted">${CHANNEL[c.channel] || ""} · ${esc(c.language)}${c.status === "needs_review" ? " · требует проверки" : ""}</div></td><td class="small">${esc(c.reason)}</td><td class="num">${scorePill(c.score)}</td></tr>`).join("")}
          </tbody></table></div>` : '<div class="empty"><b>Грубых ошибок не найдено</b></div>'}
        </div>
      </div>

      <div class="panel">
        <div class="panel-head"><h2>10 диалогов с самым низким итогом</h2><span class="small muted">кандидаты для разбора с операторами</span></div>
        <div class="table-wrap"><table><thead><tr><th>Диалог</th><th>Канал</th><th>Язык</th><th>Статус</th><th class="num">Итог</th></tr></thead><tbody>
          ${a.lowest.map((r) => `<tr class="click" data-id="${esc(r.conversation_id)}"><td><b>${esc(r.conversation_id)}</b>${r.critical ? ` <span class="chip bad" data-tip="Грубая ошибка">${icon("alert")}</span>` : ""}</td><td>${CHANNEL[r.channel] || ""}</td><td>${esc(r.language)}</td><td>${statusChip(r.status)}</td><td class="num">${scorePill(r.score)}</td></tr>`).join("")}
        </tbody></table></div>
      </div>

      <details class="panel"><summary><b>Таблица по критериям</b> <span class="small muted">точные значения</span></summary>
        <div class="table-wrap" style="margin-top:10px"><table><thead><tr><th>Критерий</th><th>Тип</th><th class="num">Вес</th><th class="num">Оценено</th><th class="num">N/A</th><th class="num">Выполнение</th><th class="num">Нарушений</th></tr></thead><tbody>
          ${a.criteria.map((c) => `<tr><td>${esc(c.title)}</td><td>${TYPE[c.type]}</td><td class="num">${c.weight}</td><td class="num">${c.evaluated}</td><td class="num">${c.na}</td><td class="num">${c.type === "flag" ? "—" : fmt(c.fulfilment, 1) + "%"}</td><td class="num">${c.type === "flag" ? c.violations : "—"}</td></tr>`).join("")}
        </tbody></table></div>
        <p class="legend-note">Токенов использовано: ${a.cost.tokens_in.toLocaleString("ru-RU")} входных, ${a.cost.tokens_out.toLocaleString("ru-RU")} выходных за ${a.cost.evaluations} оценок.</p>
      </details>`}`;

    document.getElementById("a-group").addEventListener("change", (e) => { state.analyticsGroup = e.target.value; render(); });
    $view.querySelectorAll("tr[data-id]").forEach((tr) => tr.addEventListener("click", () => (location.hash = "#/c/" + encodeURIComponent(tr.dataset.id))));
    if (st.queued || st.running) poll(render, 4000);
  }

  // ---------- апелляции ----------
  async function viewAppeals(appealId) {
    const list = await api("/appeals");
    refreshHeader();
    if (appealId) return viewAppealReview(list.find((a) => a.id === appealId));
    const open = list.filter((a) => a.status === "open");
    const done = list.filter((a) => a.status !== "open");
    const rowsHtml = (arr) => arr.map((a) => `<tr class="click" data-id="${a.id}"><td>#${a.id}</td><td><b>${esc(a.conversation_id)}</b></td><td>${esc(a.criterion_id)}</td><td class="small">${esc(a.comment)}</td><td>${a.status === "open" ? '<span class="chip warn">Ждёт решения</span>' : a.decision === "change" ? '<span class="chip warn">Изменена</span>' : '<span class="chip good">Подтверждена</span>'}</td></tr>`).join("");
    $view.innerHTML = `
      <div class="page-head"><div><h1>Апелляции</h1><p class="lead">Проверяющий изучает спорную оценку, объяснение модели, комментарий и фрагмент диалога, затем подтверждает или меняет оценку. Исходная оценка модели всегда сохраняется.</p></div></div>
      <div class="panel"><h2>Ждут решения (${open.length})</h2><div class="table-wrap"><table><thead><tr><th>№</th><th>Диалог</th><th>Критерий</th><th>Комментарий</th><th>Статус</th></tr></thead><tbody>${rowsHtml(open) || '<tr><td colspan="5"><div class="empty"><b>Открытых апелляций нет</b>Подать апелляцию можно в карточке диалога кнопкой «Оспорить».</div></td></tr>'}</tbody></table></div></div>
      <div class="panel"><h2>Рассмотренные (${done.length})</h2><div class="table-wrap"><table><thead><tr><th>№</th><th>Диалог</th><th>Критерий</th><th>Комментарий</th><th>Решение</th></tr></thead><tbody>${rowsHtml(done) || '<tr><td colspan="5"><div class="empty">Пока нет</div></td></tr>'}</tbody></table></div></div>`;
    $view.querySelectorAll("tr[data-id]").forEach((tr) => tr.addEventListener("click", () => (location.hash = "#/appeals/" + tr.dataset.id)));
  }

  async function viewAppealReview(appeal) {
    if (!appeal) { $view.innerHTML = '<div class="panel empty"><b>Апелляция не найдена</b><a href="#/appeals">К списку</a></div>'; return; }
    const ev = await api("/evaluations/" + appeal.evaluation_id);
    const conv = await api("/conversations/" + encodeURIComponent(appeal.conversation_id));
    const c = ev.checklist.data.criteria.find((x) => x.id === appeal.criterion_id);
    const r = ev.results[c.id];
    const msgIds = new Set(r.evidence.map((e) => e.message_id));
    const rng = (r.checked_range || "").match(/m\d+/g) || [];
    if (rng.length) { const n = rng.map((s) => +s.slice(1)); for (let i = Math.min(...n); i <= Math.max(...n); i++) msgIds.add("m" + i); }
    const fragment = conv.transcript.filter((m) => msgIds.has(m.id));
    const decided = appeal.status === "decided";

    $view.innerHTML = `
      <a class="back" href="#/appeals">${icon("back")} Все апелляции</a>
      <div class="page-head"><div><h1>Апелляция #${appeal.id}: ${esc(c.title)}</h1>
        <p class="lead">Диалог <a href="#/c/${encodeURIComponent(conv.id)}">${esc(conv.id)}</a> · оценка #${ev.id} · чек-лист v${ev.checklist.version}</p></div></div>
      <div class="card-grid">
        <div>
          <div class="panel">
            <h2>Спорная оценка</h2>
            <div class="row"><span class="chip ${answerClass(c, r.answer)}">${esc(answerLabel(c, r.answer))}</span><span class="small muted">${TYPE[c.type]}${c.type !== "flag" ? " · вес " + c.weight : ""}</span></div>
            <p class="small muted">${esc(c.description)}${c.na_allowed ? `<br>N/A: ${esc(c.na_condition)}` : ""}</p>
            <p><b>Объяснение модели:</b> ${esc(r.explanation)}</p>
            <ul class="evidence">${r.evidence.map((e) => `<li><div class="quote ${e.verified ? "" : "unverified"}"><span class="mono">${esc(e.message_id)}</span>«${esc(e.quote)}»</div></li>`).join("")}</ul>
            <div class="appeal-box"><b>Комментарий апелляции:</b> ${esc(appeal.comment)}</div>
          </div>
          <div class="panel">
            <h2>Фрагмент диалога</h2>
            ${fragment.length ? fragment.map((m) => `<div class="msg ${esc(m.speaker)}"><div class="id">${esc(m.id)}</div><div><span class="who">${SPEAKER[m.speaker]}</span><span class="ts">${esc(m.timestamp)}</span><div class="text">${esc(m.text)}</div></div></div>`).join("") : '<p class="muted">Модель не указала реплик — смотрите полный диалог.</p>'}
            <a href="#/c/${encodeURIComponent(conv.id)}">Открыть полный диалог →</a>
          </div>
        </div>
        <div class="panel">
          <h2>Решение проверяющего</h2>
          ${decided ? `
            <p><span class="chip ${appeal.decision === "change" ? "warn" : "good"}">${appeal.decision === "change" ? "Оценка изменена" : "Оценка подтверждена"}</span></p>
            ${appeal.decision === "change" ? `<p>Новый ответ: <b>${esc(answerLabel(c, appeal.new_answer))}</b></p>` : ""}
            <p><b>Причина:</b> ${esc(appeal.reason)}</p>
            <p>Итог: <b>${fmt(ev.score)}</b> → <b>${fmt(ev.final.score)}</b></p>` : `
            <div class="row"><label class="chk"><input type="radio" name="decision" value="confirm" checked> Подтвердить оценку</label>
            <label class="chk"><input type="radio" name="decision" value="change"> Изменить оценку</label></div>
            <p id="new-answer-wrap" hidden><label class="field">Новый ответ
              <select id="new-answer">${answerOptions(c).filter((o) => o !== r.answer).map((o) => `<option value="${o}">${esc(answerLabel(c, o))}</option>`).join("")}</select></label></p>
            <p><label class="field">Причина решения (обязательно)<textarea id="reason"></textarea></label></p>
            <div class="kpi" style="margin-bottom:12px"><div class="label">Итог после решения</div><div class="value"><span id="preview-score">${fmt(ev.final.score)}</span><small> было ${fmt(ev.score)}</small></div></div>
            <button class="btn primary" id="decide-btn">Сохранить решение</button>`}
        </div>
      </div>`;

    if (decided) return;
    const decision = () => document.querySelector("input[name=decision]:checked").value;
    const body = () => ({ decision: decision(), new_answer: decision() === "change" ? document.getElementById("new-answer").value : null, reason: document.getElementById("reason").value });
    const preview = async () => {
      document.getElementById("new-answer-wrap").hidden = decision() !== "change";
      try { const p = await api(`/appeals/${appeal.id}/preview`, { method: "POST", body: body() }); document.getElementById("preview-score").textContent = fmt(p.score); }
      catch { document.getElementById("preview-score").textContent = "—"; }
    };
    document.querySelectorAll("input[name=decision]").forEach((i) => i.addEventListener("change", preview));
    document.getElementById("new-answer").addEventListener("change", preview);
    document.getElementById("decide-btn").addEventListener("click", async () => {
      try {
        const res = await api(`/appeals/${appeal.id}/decide`, { method: "POST", body: body() });
        toast(`Решение сохранено. Итог: ${fmt(res.final.score)}`);
        refreshHeader();
        viewAppeals(appeal.id);
      } catch (err) { toast(err.message, true); }
    });
  }

  // ---------- редактор чек-листа ----------
  async function viewChecklist(versionId) {
    const versions = await api("/checklists");
    const current = await api(versionId ? "/checklists/" + versionId : "/checklists/latest");
    const data = structuredClone(current.data);
    const sectionOpts = (sel) => `<option value="">— без раздела —</option>` + (data.sections || []).map((s) => `<option value="${esc(s.id)}" ${sel === s.id ? "selected" : ""}>${esc(s.title)}</option>`).join("");

    function draw() {
      const totalWeight = data.criteria.filter((c) => c.type !== "flag").reduce((a, c) => a + Number(c.weight || 0), 0);
      $view.innerHTML = `
        <div class="page-head">
          <div><h1>Чек-лист</h1><p class="lead">Критерии, по которым модель оценивает диалоги. Изменения сохраняются как новая версия: старые оценки остаются привязаны к своей версии, новую выберите при запуске оценки.</p></div>
          <label class="field">Версия<select id="version">${versions.map((v) => `<option value="${v.id}" ${v.id === current.id ? "selected" : ""}>v${v.version} — ${esc(v.comment)} (${esc(v.created_at.slice(0, 16).replace("T", " "))})</option>`).join("")}</select></label>
        </div>
        <details class="help"><summary>Как устроен расчёт</summary><ul>
          <li><b>Да / Нет</b>: «Да» даёт полный вес, «Нет» — ноль.</li>
          <li><b>Шкала</b>: баллы пропорциональны весу, верхняя ступень даёт полный вес.</li>
          <li><b>N/A</b>: критерий исключается, его вес делится между остальными — максимум всегда 100.</li>
          <li><b>Грубая ошибка</b>: отмечается отдельно и в баллы не входит.</li>
          <li>Сейчас сумма весов: <b>${totalWeight}</b>. Она может быть любой — итог всё равно приводится к 100.</li>
        </ul></details>
        <div class="editor">
          ${data.criteria.map((c, i) => `
            <div class="crit" data-i="${i}">
              <div class="row"><span class="chip">${i + 1}</span><input type="text" data-f="title" value="${esc(c.title)}" class="grow" aria-label="Название критерия">
                <button class="btn sm" data-act="up" ${i === 0 ? "disabled" : ""} aria-label="Выше">${icon("up")}</button>
                <button class="btn sm" data-act="down" ${i === data.criteria.length - 1 ? "disabled" : ""} aria-label="Ниже">${icon("down")}</button>
                <button class="btn sm danger" data-act="del">Удалить</button></div>
              <div class="grid">
                <label class="field">ID<input type="text" data-f="id" value="${esc(c.id)}"></label>
                <label class="field">Раздел<select data-f="section">${sectionOpts(c.section)}</select></label>
                <label class="field">Тип ответа<select data-f="type">${Object.entries(TYPE).map(([k, v]) => `<option value="${k}" ${c.type === k ? "selected" : ""}>${v}</option>`).join("")}</select></label>
                <label class="field">Вес<input type="number" min="0" step="1" data-f="weight" value="${esc(c.weight)}" ${c.type === "flag" ? "disabled" : ""}></label>
                <label class="field">N/A<span class="chk"><input type="checkbox" data-f="na_allowed" ${c.na_allowed ? "checked" : ""}> разрешён</span></label>
              </div>
              <label class="field">Формулировка — что считать «Да» / как оценивать<textarea data-f="description">${esc(c.description)}</textarea></label>
              ${c.na_allowed ? `<label class="field" style="margin-top:8px">Когда критерий неприменим (N/A)<textarea data-f="na_condition">${esc(c.na_condition)}</textarea></label>` : ""}
              ${c.type === "scale" ? `<div class="small muted" style="margin-top:8px">Ступени шкалы (верхняя даёт полный вес):</div>
                ${(c.scale || []).map((s, j) => `<div class="scale-row"><input type="number" data-s="${j}" data-sf="value" value="${esc(s.value)}" aria-label="Значение"><input type="text" data-s="${j}" data-sf="label" value="${esc(s.label)}" aria-label="Описание ступени"><button class="btn sm" data-act="sdel" data-s="${j}" aria-label="Удалить ступень">×</button></div>`).join("")}
                <button class="btn sm ghost" data-act="sadd" style="margin-top:4px">+ ступень</button>` : ""}
            </div>`).join("")}
        </div>
        <div class="panel row">
          <button class="btn" id="add">+ Добавить критерий</button>
          <span class="grow"></span>
          <input type="text" id="comment" placeholder="Что изменено (комментарий к версии)" size="36">
          <button class="btn primary" id="save">Сохранить новую версию</button>
        </div>`;

      document.getElementById("version").addEventListener("change", (e) => (location.hash = "#/checklist/" + e.target.value));
      $view.querySelectorAll(".crit[data-i]").forEach((box) => {
        const c = data.criteria[Number(box.dataset.i)];
        box.querySelectorAll("[data-f]").forEach((inp) => inp.addEventListener("change", () => {
          const f = inp.dataset.f;
          if (f === "na_allowed") { c.na_allowed = inp.checked; if (!c.na_allowed) c.na_condition = ""; draw(); return; }
          if (f === "weight") c.weight = Number(inp.value);
          else c[f] = inp.value;
          if (f === "type") {
            if (c.type === "flag") { c.weight = 0; c.na_allowed = false; }
            if (c.type === "scale" && !(c.scale || []).length) c.scale = [{ value: 10, label: "Полностью выполнено" }, { value: 5, label: "Частично" }, { value: 0, label: "Не выполнено" }];
            if (c.type !== "scale") c.scale = [];
            if (c.type !== "flag" && !c.weight) c.weight = 10;
            draw();
          }
          if (f === "weight") draw();
        }));
        box.querySelectorAll("[data-sf]").forEach((inp) => inp.addEventListener("change", () => {
          const s = c.scale[Number(inp.dataset.s)];
          s[inp.dataset.sf] = inp.dataset.sf === "value" ? Number(inp.value) : inp.value;
        }));
        box.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => {
          const i = Number(box.dataset.i);
          const act = b.dataset.act;
          if (act === "del" && confirm(`Удалить критерий «${c.title}»?`)) data.criteria.splice(i, 1);
          if (act === "up") [data.criteria[i - 1], data.criteria[i]] = [data.criteria[i], data.criteria[i - 1]];
          if (act === "down") [data.criteria[i + 1], data.criteria[i]] = [data.criteria[i], data.criteria[i + 1]];
          if (act === "sadd") c.scale.push({ value: 0, label: "" });
          if (act === "sdel") c.scale.splice(Number(b.dataset.s), 1);
          draw();
        }));
      });
      document.getElementById("add").addEventListener("click", () => {
        let n = data.criteria.length + 1;
        while (data.criteria.some((c) => c.id === "c" + n)) n++;
        data.criteria.push({ id: "c" + n, section: "", title: "Новый критерий", type: "yes_no", weight: 10, na_allowed: false, description: "Да — …", na_condition: "", scale: [] });
        draw();
        window.scrollTo(0, document.body.scrollHeight);
      });
      document.getElementById("save").addEventListener("click", async () => {
        try {
          const saved = await api("/checklists", { method: "POST", body: { data, comment: document.getElementById("comment").value } });
          toast(`Сохранена версия v${saved.version}. Выберите её при запуске оценки.`);
          location.hash = "#/checklist/" + saved.id;
        } catch (err) { toast("Не сохранено: " + err.message, true); }
      });
    }
    draw();
  }

  // ---------- роутер ----------
  async function render() {
    stopPolling();
    $tip.hidden = true;
    const hash = location.hash || "#/";
    const parts = hash.slice(2).split("/").map(decodeURIComponent);
    const section = parts[0] === "" || parts[0] === "c" ? "list" : parts[0];
    document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === section));
    const navItem = NAV.find((n) => n.id === section) || NAV[0];
    const sub = parts[0] === "c" && parts[1] ? parts[1] : parts[0] === "appeals" && parts[1] ? "Апелляция #" + parts[1] : parts[0] === "checklist" && parts[1] ? "Версия" : "";
    document.getElementById("crumbs").innerHTML = `Контроль качества / ${sub ? `<a href="${navItem.href}">${navItem.title}</a> / <b>${esc(sub)}</b>` : `<b>${navItem.title}</b>`}`;
    document.title = `${sub || navItem.title} · Контроль качества · Freedom Broker`;
    try {
      if (parts[0] === "c" && parts[1]) await viewCard(parts[1]);
      else if (parts[0] === "analytics") await viewAnalytics();
      else if (parts[0] === "checklist") await viewChecklist(parts[1] ? Number(parts[1]) : null);
      else if (parts[0] === "appeals") await viewAppeals(parts[1] ? Number(parts[1]) : null);
      else await viewList();
    } catch (err) {
      $view.innerHTML = `<div class="panel"><b>Не удалось загрузить данные.</b><p>${esc(err.message)}</p><button class="btn" onclick="location.reload()">Повторить</button></div>`;
    }
  }

  window.addEventListener("hashchange", () => { render(); window.scrollTo(0, 0); });
  refreshHeader();
  render();
})();
