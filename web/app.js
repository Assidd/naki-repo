/* CQA — интерфейс на чистом JavaScript. Все данные и расчёты — на сервере (/api). */
(() => {
  "use strict";

  const $view = document.getElementById("view");
  const state = { filters: { q: "", language: "", channel: "", status: "", critical: false, group: "" }, pollTimer: null };

  const LANG = { RU: "Русский", KZ: "Казахский", EN: "Английский" };
  const CHANNEL = { "agent chat": "Чат", "voice call": "Звонок" };
  const STATUS = {
    none: ["Не оценён", ""],
    queued: ["В очереди", "info"],
    running: ["Оценивается…", "info"],
    done: ["Оценён", "ok"],
    needs_review: ["Требует проверки", "warn"],
    failed: ["Ошибка", "bad"],
  };
  const SPEAKER = { client: "Клиент", operator: "Оператор", bot: "Бот", system: "Система" };
  const TYPE = { yes_no: "Да / Нет", scale: "Шкала", flag: "Грубая ошибка (без баллов)" };

  // ---------- утилиты ----------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

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

  function answerLabel(criterion, answer) {
    if (answer == null) return "—";
    if (answer === "na") return "N/A";
    if (criterion && criterion.type === "flag") return answer === "yes" ? "Нарушение найдено" : "Нет нарушений";
    if (answer === "yes") return "Да";
    if (answer === "no") return "Нет";
    return answer;
  }

  function answerOptions(c) {
    const opts = c.type === "scale" ? c.scale.map((s) => String(s.value)) : ["yes", "no"];
    if (c.na_allowed) opts.push("na");
    return opts;
  }

  function statusBadge(status) {
    const [label, cls] = STATUS[status] || STATUS.none;
    return `<span class="badge ${cls}">${esc(label)}</span>`;
  }

  function stopPolling() { clearTimeout(state.pollTimer); state.pollTimer = null; }
  function poll(fn, ms = 2500) { stopPolling(); state.pollTimer = setTimeout(fn, ms); }

  // ---------- шапка ----------
  async function refreshHeader() {
    try {
      const s = await api("/status");
      const el = document.getElementById("model-status");
      el.innerHTML = s.llm.configured
        ? "Модель: " + s.llm.providers.map((p, i) => `${esc(p.model)}${i ? " (запасная)" : ""}`).join(", ")
        : '<span class="badge bad">Модель не настроена — укажите OPENAI_API_KEY в .env</span>';
      const pill = document.getElementById("appeal-count");
      pill.hidden = !s.open_appeals;
      pill.textContent = s.open_appeals;
      return s;
    } catch { return null; }
  }

  // ---------- список диалогов ----------
  async function viewList() {
    const [rows, status, checklists] = await Promise.all([api("/conversations"), refreshHeader(), api("/checklists")]);
    const f = state.filters;
    const evaluated = rows.filter((r) => r.evaluation && r.evaluation.score != null);
    const avg = evaluated.length ? (evaluated.reduce((a, r) => a + Number(r.evaluation.score), 0) / evaluated.length).toFixed(2) : "—";
    const criticalCount = rows.filter((r) => r.evaluation && r.evaluation.critical).length;
    const groups = [...new Set(rows.map((r) => r.group_id))];

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

    $view.innerHTML = `
      <h1>Диалоги</h1>
      <p class="sub">Загрузите набор, выберите версию чек-листа и запустите оценку. Итог и баллы считает сервер по правилам чек-листа.</p>
      <div class="stats">
        <div class="stat"><b>${rows.length}</b><span>диалогов (${rows.filter((r) => r.channel === "voice call").length} звонков)</span></div>
        <div class="stat"><b>${evaluated.length}</b><span>оценено</span></div>
        <div class="stat"><b>${avg}</b><span>средний итоговый балл</span></div>
        <div class="stat bad"><b>${criticalCount}</b><span>с грубыми ошибками</span></div>
        <div class="stat"><b>${status ? status.open_appeals : 0}</b><span>апелляций ждут решения</span></div>
      </div>

      <div class="panel">
        <h2>Данные и запуск оценки</h2>
        <div class="row">
          <label>group_id <input id="group-id" type="text" value="DEV" size="10"></label>
          <button class="btn" id="fetch-btn" ${status && status.organizer_configured ? "" : 'title="Укажите ORGANIZER_API_URL в .env"'}>Загрузить из API организаторов</button>
          <label class="btn">Импорт JSON… <input id="import-file" type="file" accept=".json,application/json" hidden></label>
          <span class="grow"></span>
          <label>Чек-лист <select id="checklist-select">${checklists.map((c) => `<option value="${c.id}">v${c.version} — ${esc(c.comment)}</option>`).join("")}</select></label>
          <button class="btn primary" id="batch-btn">Оценить неоценённые${f.group ? " в " + esc(f.group) : ""}</button>
        </div>
        ${status && !status.organizer_configured ? '<p class="small muted">API организаторов не настроен: впишите ссылку GET в ORGANIZER_API_URL в файле .env. Пока можно загрузить JSON вручную.</p>' : ""}
      </div>

      <div class="panel">
        <div class="row" style="margin-bottom:10px">
          <input id="f-q" type="text" placeholder="Поиск по ID или тексту" value="${esc(f.q)}" class="grow">
          <select id="f-group"><option value="">Все наборы</option>${groups.map((g) => `<option ${f.group === g ? "selected" : ""}>${esc(g)}</option>`).join("")}</select>
          <select id="f-language"><option value="">Все языки</option>${Object.entries(LANG).map(([k, v]) => `<option value="${k}" ${f.language === k ? "selected" : ""}>${v}</option>`).join("")}</select>
          <select id="f-channel"><option value="">Все каналы</option>${Object.entries(CHANNEL).map(([k, v]) => `<option value="${k}" ${f.channel === k ? "selected" : ""}>${v}</option>`).join("")}</select>
          <select id="f-status"><option value="">Любой статус</option>${Object.entries(STATUS).map(([k, v]) => `<option value="${k}" ${f.status === k ? "selected" : ""}>${v[0]}</option>`).join("")}</select>
          <label class="chk"><input id="f-critical" type="checkbox" ${f.critical ? "checked" : ""}> Только с грубыми ошибками</label>
        </div>
        <div class="table-wrap">
        <table>
          <thead><tr><th>ID</th><th>Набор</th><th>Канал / язык</th><th>Первая реплика клиента</th><th>Статус</th><th>Итог</th><th>Грубые ошибки</th></tr></thead>
          <tbody>
            ${filtered.map((r) => {
              const e = r.evaluation;
              return `<tr class="click" data-id="${esc(r.id)}">
                <td><b>${esc(r.id)}</b></td>
                <td>${esc(r.group_id)}</td>
                <td>${CHANNEL[r.channel] || esc(r.channel)} · ${esc(r.language)}</td>
                <td class="muted small">${esc(r.preview)}</td>
                <td>${statusBadge(e ? e.status : "none")}${e && e.open_appeals ? ' <span class="badge warn">апелляция</span>' : ""}</td>
                <td class="score">${e && e.score != null ? esc(e.score) : "—"}${e && e.score !== e.original_score && e.original_score != null ? `<div class="small muted">исходно ${esc(e.original_score)}</div>` : ""}</td>
                <td>${e && e.critical ? '<span class="badge bad">Есть</span>' : e && e.critical === false ? '<span class="muted">нет</span>' : ""}</td>
              </tr>`;
            }).join("") || `<tr><td colspan="7" class="empty">${rows.length ? "Ничего не найдено — измените фильтры." : "Диалогов пока нет. Загрузите набор из API организаторов или импортируйте JSON."}</td></tr>`}
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

    document.getElementById("fetch-btn").addEventListener("click", async (e) => {
      e.target.disabled = true;
      try {
        const r = await api("/dataset/fetch", { method: "POST", body: { group_id: document.getElementById("group-id").value } });
        toast(`Загружено ${r.total} диалогов (${r.group_id}): новых ${r.created}, обновлено ${r.updated}`);
        render();
      } catch (err) { toast(err.message, true); e.target.disabled = false; }
    });

    document.getElementById("import-file").addEventListener("change", async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      try {
        const payload = JSON.parse(await file.text());
        const group = prompt("Название набора (group_id) для импортированных диалогов:", "MANUAL");
        if (group === null) return;
        const r = await api("/dataset/import", { method: "POST", body: { group_id: group, payload } });
        toast(`Импортировано ${r.total} диалогов в «${r.group_id}»`);
        render();
      } catch (err) { toast("Импорт не выполнен: " + err.message, true); }
    });

    document.getElementById("batch-btn").addEventListener("click", async (e) => {
      e.target.disabled = true;
      try {
        const body = { checklist_id: Number(document.getElementById("checklist-select").value), only_unevaluated: true };
        if (f.group) body.group_id = f.group;
        const r = await api("/evaluations/batch", { method: "POST", body });
        toast(r.started.length ? `Запущено оценок: ${r.started.length}` : "Все диалоги уже оценены этой версией чек-листа");
        render();
      } catch (err) { toast(err.message, true); e.target.disabled = false; }
    });

    if (state.refocus) {
      const el = document.getElementById(state.refocus);
      state.refocus = null;
      if (el) { el.focus(); if (el.type === "text") el.setSelectionRange(el.value.length, el.value.length); }
    }
    if (rows.some((r) => r.evaluation && ["queued", "running"].includes(r.evaluation.status))) poll(render);
  }

  // Позиция реплики в записи звонка: время от начала, без интервалов, когда запись стояла на паузе
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
    const conv = await api("/conversations/" + encodeURIComponent(cid));
    const checklists = await api("/checklists");
    const eid = evalId || conv.latest_evaluation_id;
    const ev = eid ? await api("/evaluations/" + eid) : null;
    const isCall = conv.channel === "voice call";
    const offsets = isCall ? audioOffsets(conv.transcript) : {};
    const firstTs = NaN;

    $view.innerHTML = `
      <p><a href="#/">← Все диалоги</a></p>
      <h1>${esc(conv.id)}</h1>
      <p class="sub">${CHANNEL[conv.channel] || esc(conv.channel)} · ${LANG[conv.language] || esc(conv.language)} · набор ${esc(conv.group_id)} · ${conv.transcript.length} реплик</p>
      <div class="card-grid">
        <section class="panel transcript" id="transcript">
          <h2>Транскрипт</h2>
          ${isCall && conv.audio_url ? `
            <div class="panel" style="padding:10px">
              <audio id="audio" controls preload="none" src="/api/conversations/${encodeURIComponent(conv.id)}/audio" style="width:100%"></audio>
              <div class="small muted">Если плеер не воспроизводит запись — <a href="${esc(conv.audio_url)}" target="_blank" rel="noopener">откройте её в плеере организаторов</a>. Клик по цитате перематывает запись к реплике.</div>
            </div>` : ""}
          ${conv.transcript.map((m) => `
            <div class="msg ${esc(m.speaker)}" id="msg-${esc(m.id)}" data-ts="${esc(m.timestamp)}" data-offset="${offsets[m.id] ?? ""}">
              <div class="id">${esc(m.id)}</div>
              <div><span class="who">${SPEAKER[m.speaker] || esc(m.speaker)}</span><span class="ts">${esc(m.timestamp)}</span><div class="text">${esc(m.text)}</div></div>
            </div>`).join("")}
        </section>
        <section id="result"></section>
      </div>`;

    const $result = document.getElementById("result");
    const runBar = `
      <div class="panel row">
        <label>Чек-лист <select id="run-checklist">${checklists.map((c) => `<option value="${c.id}" ${ev && ev.checklist_id === c.id ? "selected" : ""}>v${c.version} — ${esc(c.comment)}</option>`).join("")}</select></label>
        <button class="btn primary" id="run-btn" ${ev && ["queued", "running"].includes(ev.status) ? "disabled" : ""}>${ev ? "Оценить заново" : "Запустить оценку"}</button>
        ${conv.evaluations.length > 1 ? `<label class="grow" style="text-align:right">История <select id="history">${conv.evaluations.map((e) => `<option value="${e.id}" ${ev && e.id === ev.id ? "selected" : ""}>#${e.id} · ${esc(e.created_at.slice(0, 16).replace("T", " "))} · ${STATUS[e.status]?.[0] || e.status}${e.score ? " · " + e.score : ""}</option>`).join("")}</select></label>` : ""}
      </div>`;

    if (!ev) {
      $result.innerHTML = runBar + '<div class="panel empty">Диалог ещё не оценён.</div>';
    } else if (["queued", "running"].includes(ev.status)) {
      $result.innerHTML = runBar + `<div class="panel empty">${statusBadge(ev.status)}<p>Модель оценивает диалог. Страница обновится автоматически.</p></div>`;
      poll(() => viewCard(cid, ev.id));
    } else if (ev.status === "failed") {
      $result.innerHTML = runBar + `<div class="panel">${statusBadge("failed")}<p>${esc(ev.error)}</p><p class="small muted">Ошибка модели не превращается в нулевую оценку. Исправьте настройку и запустите оценку заново.</p></div>`;
    } else {
      $result.innerHTML = runBar + renderEvaluation(ev);
      bindEvaluation(ev, cid, firstTs);
    }

    document.getElementById("run-btn").addEventListener("click", async (e) => {
      e.target.disabled = true;
      try {
        const r = await api("/evaluations", { method: "POST", body: { conversation_id: cid, checklist_id: Number(document.getElementById("run-checklist").value) } });
        viewCard(cid, r.id);
      } catch (err) { toast(err.message, true); e.target.disabled = false; }
    });
    const hist = document.getElementById("history");
    if (hist) hist.addEventListener("change", () => viewCard(cid, Number(hist.value)));
  }

  function renderEvaluation(ev) {
    const cl = ev.checklist.data;
    const final = ev.final || {};
    const sections = Object.fromEntries((cl.sections || []).map((s) => [s.id, s.title]));
    const appealsBy = Object.fromEntries(ev.appeals.map((a) => [a.criterion_id, a]));
    const scoreChanged = final.score !== ev.score;
    return `
      <div class="panel">
        <div class="row">
          <div><div class="small muted">Итоговый балл</div><div class="score-big">${final.score ?? "—"}<span class="small muted"> / 100</span></div></div>
          ${scoreChanged ? `<div><div class="small muted">Исходная оценка модели</div><div class="score">${ev.score ?? "—"}</div></div>` : ""}
          <span class="grow"></span>
          <div style="text-align:right">${statusBadge(ev.status)}<div class="small muted">Чек-лист v${ev.checklist.version} · ${esc(ev.model || "")}</div>
          <div class="small muted">Токены ${ev.tokens_in ?? 0} / ${ev.tokens_out ?? 0}${ev.cost_usd != null ? ` · ≈ $${Number(ev.cost_usd).toFixed(5)}` : ""}</div></div>
        </div>
        ${final.critical ? '<div class="critical-banner">⚠ Обнаружена грубая ошибка — см. критерий «Грубые ошибки»</div>' : ""}
        ${ev.status === "needs_review" ? `<div class="issues"><b>Требует проверки:</b> часть доказательств модели не подтвердилась в тексте диалога. Баллы показаны, но проверьте отмеченные критерии.<ul>${ev.issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul></div>` : ""}
        ${Object.keys(final.sections || {}).length ? `<div class="row small">${Object.entries(final.sections).map(([k, v]) => `<span class="badge info">${esc(sections[k] || k)}: ${v}%</span>`).join("")}</div>` : ""}
      </div>
      ${cl.criteria.map((c) => {
        const r = ev.results[c.id];
        if (!r) return "";
        const finalAnswer = final.answers ? final.answers[c.id] : r.answer;
        const fc = (final.criteria || {})[c.id] || {};
        const appeal = appealsBy[c.id];
        const changed = finalAnswer !== r.answer;
        return `
        <div class="crit ${changed ? "changed" : ""}" id="crit-${esc(c.id)}">
          <div class="crit-head">
            <h3>${esc(c.title)} <span class="small muted">${TYPE[c.type]}${c.type !== "flag" ? " · вес " + esc(c.weight) : ""}</span></h3>
            <div>
              ${changed ? `<s class="muted">${esc(answerLabel(c, r.answer))}</s> → ` : ""}
              <span class="badge ${c.type === "flag" ? (finalAnswer === "yes" ? "bad" : "ok") : finalAnswer === "na" ? "" : finalAnswer === "no" || finalAnswer === "0" ? "bad" : "ok"}">${esc(answerLabel(c, finalAnswer))}</span>
              ${c.type !== "flag" ? `<span class="small muted"> ${fc.share != null ? `${fc.share} из ${fc.max_share} б.` : "не учитывается (N/A)"}</span>` : ""}
            </div>
          </div>
          <p>${esc(r.explanation)}</p>
          ${r.evidence.length ? `<ul class="evidence">${r.evidence.map((e) => `<li><span class="quote ${e.verified ? "" : "unverified"}" data-msg="${esc(e.message_id)}" data-quote="${esc(e.quote)}" title="${e.verified ? "Цитата найдена в реплике" : "Цитата НЕ найдена в реплике"}">${esc(e.message_id)}: «${esc(e.quote)}»</span>${e.verified ? "" : ' <span class="badge bad">не подтверждено</span>'}</li>`).join("")}</ul>` : ""}
          ${r.checked_range ? `<p class="small">Проверенный участок: <button class="btn link range" data-range="${esc(r.checked_range)}">${esc(r.checked_range)}</button></p>` : ""}
          ${appeal ? renderAppealInfo(c, appeal) : `<button class="btn small appeal-open" data-crit="${esc(c.id)}">Оспорить</button>
            <div class="appeal-box" id="appeal-form-${esc(c.id)}" hidden>
              <textarea placeholder="Почему вы не согласны с оценкой?"></textarea>
              <div class="row" style="margin-top:6px"><button class="btn primary appeal-send" data-crit="${esc(c.id)}">Подать апелляцию</button></div>
            </div>`}
        </div>`;
      }).join("")}`;
  }

  function renderAppealInfo(c, a) {
    if (a.status === "open") {
      return `<div class="appeal-box"><span class="badge warn">Апелляция на рассмотрении</span> <span class="small">${esc(a.comment)}</span>
        <div class="small"><a href="#/appeals/${a.id}">Открыть рассмотрение →</a></div></div>`;
    }
    return `<div class="appeal-box"><span class="badge ${a.decision === "change" ? "warn" : "ok"}">${a.decision === "change" ? "Оценка изменена проверяющим" : "Оценка подтверждена проверяющим"}</span>
      <div class="small"><b>Апелляция:</b> ${esc(a.comment)}</div>
      <div class="small"><b>Решение:</b> ${a.decision === "change" ? `новый ответ «${esc(answerLabel(c, a.new_answer))}». ` : ""}${esc(a.reason)}</div></div>`;
  }

  function highlightMessage(id, quote, firstTs) {
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

  function bindEvaluation(ev, cid, firstTs) {
    $view.querySelectorAll(".quote").forEach((q) => q.addEventListener("click", () => highlightMessage(q.dataset.msg, q.dataset.quote, firstTs)));
    $view.querySelectorAll(".range").forEach((b) => b.addEventListener("click", () => {
      document.querySelectorAll(".msg.range").forEach((m) => m.classList.remove("range"));
      const ids = (b.dataset.range.match(/m\d+/g) || []).map((s) => Number(s.slice(1)));
      const [from, to] = [Math.min(...ids), Math.max(...ids)];
      for (let i = from; i <= to; i++) document.getElementById("msg-m" + i)?.classList.add("range");
      highlightMessage("m" + from, null, firstTs);
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

  // ---------- апелляции ----------
  async function viewAppeals(appealId) {
    const list = await api("/appeals");
    refreshHeader();
    if (appealId) return viewAppealReview(list.find((a) => a.id === appealId));
    const open = list.filter((a) => a.status === "open");
    const done = list.filter((a) => a.status !== "open");
    const rowsHtml = (arr) => arr.map((a) => `<tr class="click" data-id="${a.id}"><td>#${a.id}</td><td>${esc(a.conversation_id)}</td><td>${esc(a.criterion_id)}</td><td class="small">${esc(a.comment)}</td><td>${a.status === "open" ? '<span class="badge warn">Ждёт решения</span>' : a.decision === "change" ? '<span class="badge warn">Изменена</span>' : '<span class="badge ok">Подтверждена</span>'}</td></tr>`).join("");
    $view.innerHTML = `
      <h1>Апелляции</h1>
      <p class="sub">Проверяющий изучает спорную оценку, комментарий и фрагмент диалога, затем подтверждает или меняет оценку. Исходная оценка модели сохраняется.</p>
      <div class="panel"><h2>Ждут решения (${open.length})</h2><div class="table-wrap"><table><thead><tr><th>№</th><th>Диалог</th><th>Критерий</th><th>Комментарий</th><th>Статус</th></tr></thead><tbody>${rowsHtml(open) || '<tr><td colspan="5" class="empty">Открытых апелляций нет</td></tr>'}</tbody></table></div></div>
      <div class="panel"><h2>Рассмотренные (${done.length})</h2><div class="table-wrap"><table><thead><tr><th>№</th><th>Диалог</th><th>Критерий</th><th>Комментарий</th><th>Решение</th></tr></thead><tbody>${rowsHtml(done) || '<tr><td colspan="5" class="empty">Пока нет</td></tr>'}</tbody></table></div></div>`;
    $view.querySelectorAll("tr[data-id]").forEach((tr) => tr.addEventListener("click", () => (location.hash = "#/appeals/" + tr.dataset.id)));
  }

  async function viewAppealReview(appeal) {
    if (!appeal) { $view.innerHTML = '<div class="panel empty">Апелляция не найдена. <a href="#/appeals">К списку</a></div>'; return; }
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
      <p><a href="#/appeals">← Все апелляции</a></p>
      <h1>Апелляция #${appeal.id}: ${esc(c.title)}</h1>
      <p class="sub">Диалог <a href="#/c/${encodeURIComponent(conv.id)}">${esc(conv.id)}</a> · оценка #${ev.id} · чек-лист v${ev.checklist.version}</p>
      <div class="card-grid">
        <div>
          <div class="panel">
            <h2>Спорная оценка</h2>
            <p><span class="badge">${esc(answerLabel(c, r.answer))}</span> <span class="small muted">${TYPE[c.type]}${c.type !== "flag" ? " · вес " + c.weight : ""}</span></p>
            <p class="small muted">${esc(c.description)}${c.na_allowed ? `<br>N/A: ${esc(c.na_condition)}` : ""}</p>
            <p><b>Объяснение модели:</b> ${esc(r.explanation)}</p>
            ${r.evidence.map((e) => `<div class="quote ${e.verified ? "" : "unverified"}">${esc(e.message_id)}: «${esc(e.quote)}»</div>`).join("<br>")}
            <div class="appeal-box"><b>Комментарий апелляции:</b> ${esc(appeal.comment)}</div>
          </div>
          <div class="panel">
            <h2>Фрагмент диалога</h2>
            ${fragment.length ? fragment.map((m) => `<div class="msg ${esc(m.speaker)}"><div class="id">${esc(m.id)}</div><div><span class="who">${SPEAKER[m.speaker]}</span><span class="ts">${esc(m.timestamp)}</span><div>${esc(m.text)}</div></div></div>`).join("") : '<p class="muted">Модель не указала реплик — смотрите полный диалог.</p>'}
            <a href="#/c/${encodeURIComponent(conv.id)}">Открыть полный диалог →</a>
          </div>
        </div>
        <div class="panel">
          <h2>Решение проверяющего</h2>
          ${decided ? `
            <p><span class="badge ${appeal.decision === "change" ? "warn" : "ok"}">${appeal.decision === "change" ? "Оценка изменена" : "Оценка подтверждена"}</span></p>
            ${appeal.decision === "change" ? `<p>Новый ответ: <b>${esc(answerLabel(c, appeal.new_answer))}</b></p>` : ""}
            <p><b>Причина:</b> ${esc(appeal.reason)}</p>
            <p>Исходный итог: <b>${ev.score}</b> → финальный итог: <b>${ev.final.score}</b></p>` : `
            <div class="row"><label class="chk"><input type="radio" name="decision" value="confirm" checked> Подтвердить оценку</label>
            <label class="chk"><input type="radio" name="decision" value="change"> Изменить оценку</label></div>
            <p id="new-answer-wrap" hidden>Новый ответ:
              <select id="new-answer">${answerOptions(c).filter((o) => o !== r.answer).map((o) => `<option value="${o}">${esc(answerLabel(c, o))}</option>`).join("")}</select></p>
            <p><textarea id="reason" placeholder="Причина решения (обязательно)"></textarea></p>
            <p>Исходный итог: <b>${ev.score}</b> → после решения: <b id="preview-score">${ev.final.score}</b></p>
            <button class="btn primary" id="decide-btn">Сохранить решение</button>`}
        </div>
      </div>`;

    if (decided) return;
    const decision = () => document.querySelector("input[name=decision]:checked").value;
    const body = () => ({ decision: decision(), new_answer: decision() === "change" ? document.getElementById("new-answer").value : null, reason: document.getElementById("reason").value });
    const preview = async () => {
      document.getElementById("new-answer-wrap").hidden = decision() !== "change";
      try { const p = await api(`/appeals/${appeal.id}/preview`, { method: "POST", body: body() }); document.getElementById("preview-score").textContent = p.score ?? "—"; }
      catch (err) { document.getElementById("preview-score").textContent = "—"; }
    };
    document.querySelectorAll("input[name=decision]").forEach((i) => i.addEventListener("change", preview));
    document.getElementById("new-answer").addEventListener("change", preview);
    document.getElementById("decide-btn").addEventListener("click", async () => {
      try {
        const res = await api(`/appeals/${appeal.id}/decide`, { method: "POST", body: body() });
        toast(`Решение сохранено. Итог: ${res.final.score}`);
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
        <h1>Чек-лист</h1>
        <p class="sub">Изменения сохраняются как новая версия. Старые оценки остаются привязаны к своей версии; новую версию выберите при запуске оценки.</p>
        <div class="panel row">
          <label>Версия <select id="version">${versions.map((v) => `<option value="${v.id}" ${v.id === current.id ? "selected" : ""}>v${v.version} — ${esc(v.comment)} (${esc(v.created_at.slice(0, 16).replace("T", " "))})</option>`).join("")}</select></label>
          <span class="grow"></span>
          <span class="small muted">Сумма весов: ${totalWeight}. При N/A вес перераспределяется, максимум всегда 100.</span>
        </div>
        <div class="editor">
          ${data.criteria.map((c, i) => `
            <div class="crit" data-i="${i}">
              <div class="row"><b class="grow">${i + 1}. <input type="text" data-f="title" value="${esc(c.title)}" style="width:70%"></b>
                <button class="btn small" data-act="up" ${i === 0 ? "disabled" : ""}>↑</button>
                <button class="btn small" data-act="down" ${i === data.criteria.length - 1 ? "disabled" : ""}>↓</button>
                <button class="btn small danger" data-act="del">Удалить</button></div>
              <div class="grid">
                <label>ID<input type="text" data-f="id" value="${esc(c.id)}"></label>
                <label>Раздел<select data-f="section">${sectionOpts(c.section)}</select></label>
                <label>Тип ответа<select data-f="type">${Object.entries(TYPE).map(([k, v]) => `<option value="${k}" ${c.type === k ? "selected" : ""}>${v}</option>`).join("")}</select></label>
                <label>Вес<input type="number" min="0" step="1" data-f="weight" value="${esc(c.weight)}" ${c.type === "flag" ? "disabled" : ""}></label>
                <label>N/A<span><input type="checkbox" data-f="na_allowed" ${c.na_allowed ? "checked" : ""}> разрешён</span></label>
              </div>
              <label class="small muted">Формулировка (что считать «Да» / как оценивать)<textarea data-f="description">${esc(c.description)}</textarea></label>
              ${c.na_allowed ? `<label class="small muted">Условие N/A<textarea data-f="na_condition">${esc(c.na_condition)}</textarea></label>` : ""}
              ${c.type === "scale" ? `<div class="small muted" style="margin-top:6px">Шкала (верхняя ступень даёт полный вес):</div>
                ${(c.scale || []).map((s, j) => `<div class="scale-row"><input type="number" data-s="${j}" data-sf="value" value="${esc(s.value)}"><input type="text" data-s="${j}" data-sf="label" value="${esc(s.label)}"><button class="btn small" data-act="sdel" data-s="${j}">×</button></div>`).join("")}
                <button class="btn small" data-act="sadd" style="margin-top:4px">+ ступень</button>` : ""}
            </div>`).join("")}
        </div>
        <div class="panel row">
          <button class="btn" id="add">+ Добавить критерий</button>
          <span class="grow"></span>
          <input type="text" id="comment" placeholder="Комментарий к версии (что изменено)" size="40">
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
    const hash = location.hash || "#/";
    const parts = hash.slice(2).split("/").map(decodeURIComponent);
    document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active",
      (parts[0] === "" || parts[0] === "c") ? a.dataset.nav === "list" : a.dataset.nav === parts[0]));
    try {
      if (parts[0] === "c" && parts[1]) await viewCard(parts[1]);
      else if (parts[0] === "checklist") await viewChecklist(parts[1] ? Number(parts[1]) : null);
      else if (parts[0] === "appeals") await viewAppeals(parts[1] ? Number(parts[1]) : null);
      else await viewList();
    } catch (err) {
      $view.innerHTML = `<div class="panel"><b>Не удалось загрузить данные.</b><p>${esc(err.message)}</p><button class="btn" onclick="location.reload()">Повторить</button></div>`;
    }
  }

  window.addEventListener("hashchange", render);
  refreshHeader();
  render();
})();
