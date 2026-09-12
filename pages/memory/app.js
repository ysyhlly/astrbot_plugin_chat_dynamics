import {
  apiGet,
  apiPost,
  escapeHtml,
  formatTs,
  readyBridge,
  redactId,
  setLink,
  storageGet,
  storageSet,
} from "./api.js";
import { fillSessionSelect, renderNav } from "./shell.js";

const UMO_KEY = "cd_wire_umo";

const els = {
  linkLamp: document.getElementById("linkLamp"),
  linkLabel: document.getElementById("linkLabel"),
  sessionSelect: document.getElementById("sessionSelect"),
  sessionNote: document.getElementById("sessionNote"),
  listHost: document.getElementById("listHost"),
  listEmpty: document.getElementById("listEmpty"),
  addFields: document.getElementById("addFields"),
  addForm: document.getElementById("addForm"),
  addTitle: document.getElementById("addTitle"),
  addNote: document.getElementById("addNote"),
  btnAdd: document.getElementById("btnAdd"),
  btnMuteTonight: document.getElementById("btnMuteTonight"),
  btnLoad: document.getElementById("btnLoad"),
  btnRefresh: document.getElementById("btnRefresh"),
};

let selectedUmo = storageGet(UMO_KEY, "");
let tab = "anniversaries";
let notebook = { anniversaries: [], reminders: [], slang_trials: [], mute_until: 0 };
let online = false;
let busy = false;
let notebookRevision = 0;

function setEnabled() {
  const ok = online && Boolean(selectedUmo) && !busy;
  els.btnAdd.disabled = !ok;
  els.btnMuteTonight.disabled = !ok;
  els.btnLoad.disabled = !ok;
  els.sessionSelect.disabled = busy;
  els.btnRefresh.disabled = busy;
  document.querySelectorAll(".tab").forEach((node) => { node.disabled = busy; });
}

function paintTabs() {
  document.querySelectorAll(".tab").forEach((node) => {
    const selected = node.getAttribute("data-tab") === tab;
    node.setAttribute("aria-selected", String(selected));
    node.tabIndex = selected ? 0 : -1;
  });
}

function paintAddForm() {
  if (tab === "anniversaries") {
    els.addTitle.textContent = "记一个纪念日";
    els.addFields.innerHTML = `
      <label>标题<input name="title" maxlength="40" required placeholder="例如：群庆" /></label>
      <label>月<input name="month" type="number" min="1" max="12" required /></label>
      <label>日<input name="day" type="number" min="1" max="31" required /></label>
      <label>备注<input name="note" maxlength="80" placeholder="可选，勿写隐私" /></label>
    `;
  } else if (tab === "reminders") {
    els.addTitle.textContent = "记一条约定";
    els.addFields.innerHTML = `
      <label>内容<input name="text" maxlength="80" required placeholder="到期提醒一句" /></label>
      <label>到期时间<input name="due_local" type="datetime-local" required /></label>
    `;
  } else {
    els.addTitle.textContent = "记一条试用梗";
    els.addFields.innerHTML = `
      <label>短语<input name="phrase" maxlength="24" required placeholder="需已批准；敏感词会被拒" /></label>
      <label class="chip-row" style="align-items:center">
        <input name="approved" type="checkbox" /> 我确认这是群里已允许的梗
      </label>
    `;
  }
}

function softText(value, fallback = "（已记一条）") {
  const text = String(value || "").trim();
  if (!text) return fallback;
  if (text.length <= 24) return text;
  return `${text.slice(0, 20)}…`;
}

function renderList() {
  paintTabs();
  document.getElementById("memoryResults").setAttribute("aria-labelledby", `tab-${tab}`);
  let rows = [];
  if (tab === "anniversaries") rows = notebook.anniversaries || [];
  else if (tab === "reminders") rows = notebook.reminders || [];
  else rows = notebook.slang_trials || [];

  document.getElementById("memoryCount").textContent = `${rows.length} 条`;
  if (!selectedUmo) {
    els.listHost.innerHTML = "";
    els.listEmpty.classList.remove("hidden");
    els.listEmpty.innerHTML = `请先选择群会话。也可在侧栏「今日读空气」里挑一个。`;
    return;
  }
  if (!rows.length) {
    els.listHost.innerHTML = "";
    els.listEmpty.classList.remove("hidden");
    const copy =
      tab === "anniversaries"
        ? "还没有纪念日。群庆、固定活动日可以记在这里。"
        : tab === "reminders"
          ? "还没有约定。到期会提醒一次（one-shot）。"
          : "还没有试用梗。默认关闭；开启 slang 后才可写入。";
    els.listEmpty.textContent = copy;
    return;
  }
  els.listEmpty.classList.add("hidden");
  els.listHost.innerHTML = rows
    .map((item) => {
      const id = escapeHtml(item.id || "");
      if (tab === "anniversaries") {
        return `<article class="memory-card" data-id="${id}">
          <strong>${escapeHtml(softText(item.title))}</strong>
          <span class="meta">${escapeHtml(`${item.month || "?"}/${item.day || "?"}`)}${
            item.note ? ` · ${escapeHtml(softText(item.note, ""))}` : ""
          }</span>
          <div class="actions">
            <button type="button" class="button button-quiet" data-act="forget">忘掉</button>
          </div>
        </article>`;
      }
      if (tab === "reminders") {
        return `<article class="memory-card" data-id="${id}">
          <strong>${escapeHtml(softText(item.text))}</strong>
          <span class="meta">到期 ${escapeHtml(formatTs(item.due_at) || "—")}${
            item.nudged ? " · 已提醒" : ""
          }</span>
          <div class="actions">
            <button type="button" class="button button-quiet" data-act="done">标完成</button>
            <button type="button" class="button" data-act="forget">忘掉</button>
          </div>
        </article>`;
      }
      return `<article class="memory-card" data-id="${id}">
        <strong>${escapeHtml(softText(item.phrase))}</strong>
        <span class="meta">试用 ${escapeHtml(String(item.uses || 0))} 次${
          item.cold_retract ? " · 已冷却" : ""
        }</span>
        <div class="actions">
          <button type="button" class="button button-quiet" data-act="forget">忘掉</button>
        </div>
      </article>`;
    })
    .join("");
}

async function loadSessions() {
  try {
    const air = await apiGet("read_air", selectedUmo ? { umo: selectedUmo } : {});
    fillSessionSelect(els.sessionSelect, air.sessions || [], selectedUmo);
    online = true;
    setLink(els, true, "已连接");
  } catch (err) {
    online = false;
    setLink(els, false, (err && err.message) || "离线");
  }
  setEnabled();
}

async function loadNotebook() {
  const revision = ++notebookRevision;
  if (!selectedUmo) {
    notebook = { anniversaries: [], reminders: [], slang_trials: [], mute_until: 0 };
    renderList();
    els.sessionNote.textContent = "未选择会话。";
    return;
  }
  try {
    const result = await apiGet("notebook", { umo: selectedUmo });
    if (revision !== notebookRevision) return;
    notebook = result;
    online = true;
    setLink(els, true, "已连接");
    const mute = Number(notebook.mute_until || 0);
    els.sessionNote.textContent = mute > Date.now() / 1000
      ? `今晚别提生效中 · 至 ${formatTs(mute)} · 会话 ${redactId(selectedUmo)}`
      : `已加载 · 会话 ${redactId(selectedUmo)}（正文默认脱敏展示）`;
  } catch (err) {
    if (revision !== notebookRevision) return;
    online = false;
    notebook = { anniversaries: [], reminders: [], slang_trials: [] };
    setLink(els, false, "读取失败");
    els.sessionNote.textContent = (err && err.message) || "读取失败";
  }
  setEnabled();
  renderList();
}

async function mutate(action, payload = {}) {
  if (!selectedUmo || busy) return;
  busy = true;
  setEnabled();
  try {
    await apiPost("notebook", { action, umo: selectedUmo, ...payload });
    await loadNotebook();
  } catch (err) {
    els.addNote.textContent = (err && err.message) || "操作失败";
  } finally {
    busy = false;
    setEnabled();
  }
}

async function onAdd(event) {
  event.preventDefault();
  if (!selectedUmo || busy) return;
  const fd = new FormData(els.addForm);
  busy = true;
  setEnabled();
  els.addNote.textContent = "写入中…";
  try {
    if (tab === "anniversaries") {
      await apiPost("notebook", {
        action: "add_anniversary",
        umo: selectedUmo,
        title: String(fd.get("title") || ""),
        month: Number(fd.get("month") || 0),
        day: Number(fd.get("day") || 0),
        note: String(fd.get("note") || ""),
      });
    } else if (tab === "reminders") {
      const local = String(fd.get("due_local") || "");
      const due = local ? new Date(local).getTime() / 1000 : NaN;
      if (!Number.isFinite(due)) throw new Error("请填写有效到期时间");
      await apiPost("notebook", {
        action: "add_reminder",
        umo: selectedUmo,
        text: String(fd.get("text") || ""),
        due_at: due,
      });
    } else {
      await apiPost("notebook", {
        action: "add_slang",
        umo: selectedUmo,
        phrase: String(fd.get("phrase") || ""),
        approved: Boolean(fd.get("approved")),
      });
    }
    els.addNote.textContent = "已记下。";
    els.addForm.reset();
    await loadNotebook();
  } catch (err) {
    els.addNote.textContent = (err && err.message) || "写入失败";
  } finally {
    busy = false;
    setEnabled();
  }
}

function onListClick(event) {
  const btn = event.target.closest("button[data-act]");
  if (!btn) return;
  const card = btn.closest(".memory-card");
  const id = card && card.getAttribute("data-id");
  if (!id) return;
  const act = btn.getAttribute("data-act");
  if (tab === "anniversaries" && act === "forget") {
    void mutate("remove_anniversary", { id });
  } else if (tab === "reminders" && (act === "forget" || act === "done")) {
    void mutate(act === "done" ? "mark_done" : "remove_reminder", { id });
  } else if (tab === "slang" && act === "forget") {
    void mutate("remove_slang", { id });
  }
}

async function boot() {
  renderNav("memory");
  try {
    await readyBridge();
  } catch {
    /* ignore */
  }
  document.querySelectorAll(".tab").forEach((node) => {
    node.addEventListener("click", () => {
      tab = node.getAttribute("data-tab") || "anniversaries";
      els.addNote.textContent = "";
      paintAddForm();
      renderList();
    });
    node.addEventListener("keydown", (event) => {
      const tabs = [...document.querySelectorAll(".tab")];
      const index = tabs.indexOf(node);
      const next = event.key === "ArrowRight" ? (index + 1) % tabs.length : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : -1;
      if (next < 0) return;
      event.preventDefault();
      tabs[next].click();
      tabs[next].focus();
    });
  });
  els.sessionSelect.addEventListener("change", () => {
    selectedUmo = els.sessionSelect.value || "";
    storageSet(UMO_KEY, selectedUmo);
    notebook = { anniversaries: [], reminders: [], slang_trials: [] };
    paintAddForm();
    renderList();
    setEnabled();
    void loadNotebook();
  });
  els.btnRefresh.addEventListener("click", async () => {
    await loadSessions();
    await loadNotebook();
  });
  els.btnLoad.addEventListener("click", () => void loadNotebook());
  els.btnMuteTonight.addEventListener("click", () => void mutate("mute_tonight", { hours: 10 }));
  els.addForm.addEventListener("submit", onAdd);
  els.listHost.addEventListener("click", onListClick);
  paintAddForm();
  await loadSessions();
  await loadNotebook();
}

boot();
