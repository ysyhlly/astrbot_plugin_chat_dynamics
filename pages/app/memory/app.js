import {
  apiGet,
  apiPost,
  escapeHtml,
  formatTs,
  friendlyError,
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
  btnUnmute: document.getElementById("btnUnmute"),
  btnLoad: document.getElementById("btnLoad"),
  btnRefresh: document.getElementById("btnRefresh"),
  narrativePanel: document.getElementById("narrativePanel"),
  narrativeStatus: document.getElementById("narrativeStatus"),
  narrativeContent: document.getElementById("narrativeContent"),
  narrativeSnapshot: document.getElementById("narrativeSnapshot"),
  narrativeMaterials: document.getElementById("narrativeMaterials"),
  narrativeDiary: document.getElementById("narrativeDiary"),
  narrativeCommitments: document.getElementById("narrativeCommitments"),
  narrativeIntents: document.getElementById("narrativeIntents"),
  narrativeReceipts: document.getElementById("narrativeReceipts"),
  narrativeStats: document.getElementById("narrativeStats"),
  narrativeCorrectForm: document.getElementById("narrativeCorrectForm"),
  btnNarrativeRefresh: document.getElementById("btnNarrativeRefresh"),
  btnNarrativeCorrect: document.getElementById("btnNarrativeCorrect"),
};

let selectedUmo = storageGet(UMO_KEY, "");
let tab = "anniversaries";
let notebook = { anniversaries: [], reminders: [], slang_trials: [], mute_until: 0 };
let online = false;
let busy = false;
let loadFailed = false;
let notebookRevision = 0;
let narrativeRevision = 0;
let narrativeData = null;
let narrativeLoading = false;

const AVAILABILITY_LABELS = {
  available: "可以交互", busy: "忙碌", asleep: "睡眠", unknown: "未知",
};

function narrativeValue(value) {
  if (value == null || value === "") return "—";
  return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
}

function narrativeRows(value) {
  if (Array.isArray(value)) return value;
  if (!value || typeof value !== "object") return [];
  return Object.entries(value).map(([provider, row]) =>
    row && typeof row === "object" && !Array.isArray(row)
      ? { provider, ...row } : { provider, text: narrativeValue(row) });
}

function narrativeCanWrite() {
  const life = narrativeData?.life;
  return Boolean(selectedUmo && narrativeData && narrativeData.enabled !== false
    && narrativeData.can_write !== false && life && life.managed !== false
    && life.available !== false && life.snapshot && life.snapshot.managed !== false);
}

function setNarrativeEnabled() {
  if (!els.narrativePanel) return;
  const ok = narrativeCanWrite() && !busy && !narrativeLoading;
  els.btnNarrativeRefresh.disabled = !selectedUmo || busy || narrativeLoading;
  els.btnNarrativeCorrect.disabled = !ok;
  els.narrativeCorrectForm.querySelectorAll("input,select").forEach((node) => { node.disabled = !ok; });
  const life = narrativeData?.life || {};
  const paused = life.paused ?? life.snapshot?.paused;
  const outreachPaused = life.outreach_paused ?? life.proactive_paused ?? life.snapshot?.outreach_paused;
  els.narrativePanel.querySelectorAll("[data-narrative-action]").forEach((node) => {
    const action = node.dataset.narrativeAction;
    const alreadySet = (action === "pause" && paused === true)
      || (action === "resume" && paused === false)
      || (action === "pause_outreach" && outreachPaused === true)
      || (action === "resume_outreach" && outreachPaused === false);
    node.disabled = !ok || alreadySet;
  });
  els.narrativeIntents.querySelectorAll("button").forEach((node) => { node.disabled = !ok; });
}

function clearNarrative(message, error = false) {
  narrativeData = null;
  els.narrativeContent.classList.add("hidden");
  for (const name of ["narrativeSnapshot", "narrativeMaterials", "narrativeDiary", "narrativeCommitments", "narrativeIntents", "narrativeReceipts", "narrativeStats"]) {
    els[name].replaceChildren();
  }
  els.narrativeStatus.textContent = message;
  els.narrativeStatus.classList.toggle("error", error);
  setNarrativeEnabled();
}

function addNarrativeText(parent, tag, text, className = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = narrativeValue(text);
  parent.append(node);
  return node;
}

function renderNarrativeList(host, rows, kind, empty) {
  host.replaceChildren();
  const entries = narrativeRows(rows).slice(0, 100);
  if (!entries.length) {
    addNarrativeText(host, "p", empty, "narrative-empty");
    return;
  }
  for (const raw of entries) {
    const row = raw && typeof raw === "object" ? raw : { text: raw };
    const item = document.createElement("article");
    item.className = "narrative-item";
    const title = kind === "material" ? row.provider || row.source_id || row.name || "素材"
      : kind === "receipt" ? row.status || row.result || "发送结果"
        : kind === "intent" ? row.reason || row.description || "联系意图"
          : kind === "commitment" ? row.description || row.text || "约定"
            : row.fact_kind || "生活记录";
    addNarrativeText(item, "strong", title);
    const text = row.text ?? row.content ?? row.actual_content ?? row.actual_text ?? row.summary;
    if (text != null) addNarrativeText(item, "p", text);
    const time = row.occurred_at ?? row.occurred ?? row.sent_at ?? row.created_at ?? row.updated_at;
    const meta = [
      time ? formatTs(time) : "",
      row.status ?? row.availability ?? (row.available === false ? "不可用" : ""),
      row.source_id ?? (row.source_ids ? narrativeValue(row.source_ids) : ""),
    ].filter(Boolean).join(" · ");
    if (meta) addNarrativeText(item, "p", meta, "meta");
    const details = document.createElement("details");
    details.className = "narrative-details";
    addNarrativeText(details, "summary", "查看来源与记录详情");
    addNarrativeText(details, "pre", row);
    item.append(details);
    const id = row.intent_id ?? row.id;
    const finalStatus = ["sent", "completed", "cancelled", "expired", "failed", "uncertain", "rejected"].includes(row.status);
    if (kind === "intent" && id && !finalStatus) {
      const button = addNarrativeText(item, "button", "取消此联系", "button button-quiet");
      button.type = "button";
      button.dataset.intentId = String(id);
      button.setAttribute("aria-label", `取消联系：${narrativeValue(title)}`);
    }
    host.append(item);
  }
}

function renderNarrative(result) {
  const life = result?.life || {};
  const snapshot = life.snapshot;
  if (result?.enabled === false || life.managed === false || life.available === false
      || !snapshot || snapshot.managed === false) {
    clearNarrative(life.reason || result?.reason || "当前会话尚未启用持续叙事生活；请在设置中配置会话范围。请确认生活插件与世界书已连接。");
    return;
  }
  narrativeData = result;
  els.narrativeContent.classList.remove("hidden");
  els.narrativeStatus.classList.remove("error");
  const paused = life.paused ?? snapshot.paused;
  const outreachPaused = life.outreach_paused ?? life.proactive_paused ?? snapshot.outreach_paused;
  els.narrativeStatus.textContent = [
    `已加载 · 会话 ${redactId(selectedUmo)}`,
    paused === true ? "生活推进已暂停" : "",
    outreachPaused === true ? "主动联系已暂停" : "",
    snapshot.stale ? "状态已过期，等待推进" : "",
    result.can_write === false ? "仅可查看" : "",
  ].filter(Boolean).join(" · ");
  els.narrativeSnapshot.replaceChildren();
  const fields = [
    ["角色", snapshot.scope?.persona_id ?? snapshot.persona_id],
    ["状态修订", snapshot.revision], ["活动", snapshot.activity],
    ["可交互状态", AVAILABILITY_LABELS[snapshot.availability] || snapshot.availability],
    ["主观状态", snapshot.subjective_state], ["近期摘要", snapshot.summary],
    ["有效时间", formatTs(snapshot.valid_from ?? snapshot.valid_at)],
    ["状态来源", snapshot.source_ids],
  ];
  const storage = result.storage;
  if (storage) {
    const gib = (bytes) => `${(Number(bytes || 0) / 1024 ** 3).toFixed(2)} GiB`;
    fields.push(["插件存储（所有会话）", `叙事库 ${gib(storage.database_bytes)} · 写入日志 ${gib(storage.wal_bytes)} · 旧决策库 ${gib(storage.legacy_database_bytes)}`],
      ["磁盘剩余", `${gib(storage.free_bytes)}${storage.low_space ? " · 空间不足 5 GiB，请安排归档" : ""}`],
      ["历史保留", "到期精简在线摘要；完整压缩上下文、回执及旧决策历史保留。归档需校验后另存，不自动删库。"]);
  }
  for (const [label, value] of fields) {
    addNarrativeText(els.narrativeSnapshot, "dt", label);
    addNarrativeText(els.narrativeSnapshot, "dd", value);
  }
  renderNarrativeList(els.narrativeMaterials, result.materials, "material", "尚无本轮素材记录。");
  renderNarrativeList(els.narrativeDiary, life.diary, "diary", "尚无生活记录。");
  renderNarrativeList(els.narrativeCommitments, life.commitments ?? snapshot.commitments, "commitment", "当前会话没有约定。");
  const intents = [...narrativeRows(life.intents), ...narrativeRows(result.intents)];
  const seen = new Set();
  const uniqueIntents = intents.filter((row) => {
    const id = row.intent_id ?? row.id;
    if (!id) return true;
    if (seen.has(id)) return false;
    seen.add(id);
    return true;
  });
  renderNarrativeList(els.narrativeIntents, uniqueIntents, "intent", "当前会话没有联系意图。");
  renderNarrativeList(els.narrativeReceipts, result.receipts, "receipt", "尚无发送回执。");
  els.narrativeStats.textContent = JSON.stringify({
    life: life.stats ?? { calls: life.calls ?? [], pending_count: life.pending_count ?? 0 },
    coordinator: result.stats ?? {}, pending_notifications: result.pending_notifications ?? [],
    daily_plan: life.daily_plan ?? [], plan_date: life.plan_date ?? "",
  }, null, 2);
  setNarrativeEnabled();
}

async function loadNarrative() {
  if (!els.narrativePanel) return;
  const revision = ++narrativeRevision;
  const umo = selectedUmo;
  narrativeLoading = Boolean(umo);
  clearNarrative(umo ? "正在加载角色生活…" : "请选择会话。");
  els.narrativePanel.setAttribute("aria-busy", String(narrativeLoading));
  if (!umo) return;
  try {
    const result = await apiGet("narrative", { umo });
    if (revision !== narrativeRevision || umo !== selectedUmo) return;
    renderNarrative(result);
  } catch (err) {
    if (revision !== narrativeRevision || umo !== selectedUmo) return;
    clearNarrative(friendlyError(err, "角色生活读取失败；请刷新重试。此面板仅对管理员开放。"), true);
  } finally {
    if (revision === narrativeRevision && umo === selectedUmo) {
      narrativeLoading = false;
      els.narrativePanel.setAttribute("aria-busy", "false");
      setNarrativeEnabled();
    }
  }
}

async function mutateNarrative(action, payload = {}) {
  if (!narrativeCanWrite() || busy || narrativeLoading) return;
  const umo = selectedUmo;
  busy = true;
  setEnabled();
  els.narrativeStatus.classList.remove("error");
  els.narrativeStatus.textContent = "正在记录操作…";
  try {
    await apiPost("narrative", { umo, action, ...payload });
    if (umo !== selectedUmo) return;
    if (action === "correct") els.narrativeCorrectForm.reset();
    await loadNarrative();
  } catch (err) {
    if (umo !== selectedUmo) return;
    els.narrativeStatus.textContent = friendlyError(err, "操作失败；原状态未确认更改，请刷新检查。");
    els.narrativeStatus.classList.add("error");
  } finally {
    busy = false;
    setEnabled();
  }
}

function showListLoading() {
  els.listEmpty.classList.add("hidden");
  els.listHost.setAttribute("aria-busy", "true");
  els.listHost.innerHTML = '<p class="ops-note loading-note">正在加载记忆小本…</p>';
}

function addFormHasInput() {
  return [...els.addFields.querySelectorAll("input")].some((input) =>
    input.type === "checkbox" ? input.checked : Boolean(input.value.trim()));
}

function setEnabled() {
  const ok = online && Boolean(selectedUmo) && !busy;
  els.btnAdd.disabled = !ok;
  els.btnMuteTonight.disabled = !ok;
  if (els.btnUnmute) els.btnUnmute.disabled = !ok;
  els.btnLoad.disabled = !ok;
  els.sessionSelect.disabled = busy;
  els.btnRefresh.disabled = busy;
  document.querySelectorAll(".tab").forEach((node) => { node.disabled = busy; });
  setNarrativeEnabled();
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

function forgetBlock(actionsHtml) {
  return `<div class="actions">${actionsHtml}</div>
    <div class="confirm-row hidden" data-confirm>
      <span>忘掉后无法恢复，确定吗？</span>
      <button type="button" class="button button-danger" data-act="forget-confirm">确认忘掉</button>
      <button type="button" class="button button-quiet" data-act="forget-cancel">取消</button>
    </div>
    <p class="ops-note error hidden" data-card-note role="status"></p>`;
}

function renderList() {
  paintTabs();
  els.listHost.removeAttribute("aria-busy");
  document.getElementById("memoryResults").setAttribute("aria-labelledby", `tab-${tab}`);
  let rows = [];
  if (tab === "anniversaries") rows = notebook.anniversaries || [];
  else if (tab === "reminders") rows = notebook.reminders || [];
  else rows = notebook.slang_trials || [];

  if (loadFailed) {
    document.getElementById("memoryCount").textContent = "读取失败";
    els.listHost.innerHTML = "";
    els.listEmpty.classList.remove("hidden");
    els.listEmpty.classList.add("is-error");
    els.listEmpty.textContent = "记忆小本暂时读不到，请点右上角「刷新」重试。";
    return;
  }
  document.getElementById("memoryCount").textContent = `${rows.length} 条`;
  els.listEmpty.classList.remove("is-error");
  if (!selectedUmo) {
    els.listHost.innerHTML = "";
    els.listEmpty.classList.remove("hidden");
    els.listEmpty.textContent = "请先选择群聊会话。也可从顶部导航打开「今日读空气」。";
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
          ${forgetBlock('<button type="button" class="button button-quiet" data-act="forget">忘掉</button>')}
        </article>`;
      }
      if (tab === "reminders") {
        return `<article class="memory-card" data-id="${id}">
          <strong>${escapeHtml(softText(item.text))}</strong>
          <span class="meta">到期 ${escapeHtml(formatTs(item.due_at) || "—")}${
            item.nudged ? " · 已提醒" : ["sending", "uncertain"].includes(item.delivery_state) ? " · 送达待确认，不会自动重发" : ""
          }</span>
          ${forgetBlock('<button type="button" class="button button-quiet" data-act="done">标完成</button><button type="button" class="button button-quiet" data-act="forget">忘掉</button>')}
        </article>`;
      }
      return `<article class="memory-card" data-id="${id}">
        <strong>${escapeHtml(softText(item.phrase))}</strong>
        <span class="meta">试用 ${escapeHtml(String(item.uses || 0))} 次${
          item.cold_retract ? " · 已冷却" : ""
        }</span>
        ${forgetBlock('<button type="button" class="button button-quiet" data-act="forget">忘掉</button>')}
      </article>`;
    })
    .join("");
  els.listHost.querySelectorAll(".memory-card").forEach((card) => {
    const title = (card.querySelector("strong")?.textContent || "").trim();
    if (!title) return;
    const forget = card.querySelector('[data-act="forget"]');
    if (forget) forget.setAttribute("aria-label", `忘掉：${title}`);
    const confirm = card.querySelector('[data-act="forget-confirm"]');
    if (confirm) confirm.setAttribute("aria-label", `确认忘掉：${title}`);
  });
}

function openForgetConfirm(card) {
  const actions = card.querySelector(".actions");
  const row = card.querySelector("[data-confirm]");
  if (!actions || !row) return;
  actions.classList.add("hidden");
  row.classList.remove("hidden");
  row.querySelector('[data-act="forget-confirm"]')?.focus();
}

function closeForgetConfirm(card) {
  card.querySelector("[data-confirm]")?.classList.add("hidden");
  card.querySelector(".actions")?.classList.remove("hidden");
  card.querySelector('[data-act="forget"]')?.focus();
}

async function loadSessions() {
  try {
    const air = await apiGet("read_air", selectedUmo ? { umo: selectedUmo } : {});
    fillSessionSelect(els.sessionSelect, air.sessions || [], selectedUmo);
    online = true;
    setLink(els, true, "已连接");
  } catch (err) {
    online = false;
    setLink(els, false, friendlyError(err, "离线"));
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
    loadFailed = false;
    setLink(els, true, "已连接");
    const mute = Number(notebook.mute_until || 0);
    const muted = mute > Date.now() / 1000;
    els.sessionNote.textContent = muted
      ? `今晚别提生效中 · 至 ${formatTs(mute)} · 会话 ${redactId(selectedUmo)}`
      : `已加载 · 会话 ${redactId(selectedUmo)}（正文默认脱敏展示）`;
    // A ten-hour silence with no way back was the only irreversible switch on
    // this page; offer the cancel path whenever a mute is running.
    if (els.btnUnmute) els.btnUnmute.classList.toggle("hidden", !muted);
  } catch (err) {
    if (revision !== notebookRevision) return;
    online = false;
    loadFailed = true;
    notebook = { anniversaries: [], reminders: [], slang_trials: [] };
    setLink(els, false, "读取失败");
    els.sessionNote.textContent = friendlyError(err, "读取失败，请稍后重试。");
  }
  setEnabled();
  renderList();
}

async function mutate(action, payload = {}, card = null) {
  if (!selectedUmo || busy) return;
  busy = true;
  setEnabled();
  const note = card ? card.querySelector("[data-card-note]") : null;
  if (note) {
    note.textContent = "处理中…";
    note.classList.remove("hidden", "error");
  }
  try {
    await apiPost("notebook", { action, umo: selectedUmo, ...payload });
    await loadNotebook();
  } catch (err) {
    // Report next to the record that was clicked, not in the add-record card.
    const message = friendlyError(err, "操作失败，请稍后重试。");
    if (note) {
      note.textContent = message;
      note.classList.add("error");
      note.classList.remove("hidden");
    } else {
      els.addNote.textContent = message;
      els.addNote.classList.add("error");
    }
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
    els.addNote.classList.remove("error");
    els.addNote.textContent = "已记下。";
    els.addForm.reset();
    await loadNotebook();
  } catch (err) {
    els.addNote.classList.add("error");
    els.addNote.textContent = friendlyError(err, "写入失败，请稍后重试。");
  } finally {
    busy = false;
    setEnabled();
  }
}

function removeActionForTab() {
  if (tab === "anniversaries") return "remove_anniversary";
  if (tab === "reminders") return "remove_reminder";
  return "remove_slang";
}

function onListClick(event) {
  const btn = event.target.closest("button[data-act]");
  if (!btn) return;
  const card = btn.closest(".memory-card");
  const id = card && card.getAttribute("data-id");
  if (!id) return;
  const act = btn.getAttribute("data-act");
  // Forgetting is irreversible and has no undo server-side, so it takes two
  // deliberate clicks; the first one only asks.
  if (act === "forget") {
    openForgetConfirm(card);
    return;
  }
  if (act === "forget-cancel") {
    closeForgetConfirm(card);
    return;
  }
  if (act === "forget-confirm") {
    void mutate(removeActionForTab(), { id }, card);
    return;
  }
  if (act === "done" && tab === "reminders") {
    void mutate("mark_done", { id }, card);
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
      const next = node.getAttribute("data-tab") || "anniversaries";
      if (next === tab) return;
      if (addFormHasInput() && !window.confirm("切换分类会清空「新增记录」里填写的内容，继续吗？")) return;
      tab = next;
      els.addNote.textContent = "";
      els.addNote.classList.remove("error");
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
    const next = els.sessionSelect.value || "";
    const narrativeDraft = Boolean(document.getElementById("narrativeActivity")?.value.trim()
      || document.getElementById("narrativeAvailability")?.value);
    if (next !== selectedUmo && (addFormHasInput() || narrativeDraft)
        && !window.confirm("切换会话会清空未保存的新增记录或状态纠正，继续吗？")) {
      els.sessionSelect.value = selectedUmo;
      return;
    }
    selectedUmo = next;
    storageSet(UMO_KEY, selectedUmo);
    notebook = { anniversaries: [], reminders: [], slang_trials: [] };
    loadFailed = false;
    paintAddForm();
    els.narrativeCorrectForm?.reset();
    showListLoading();
    setEnabled();
    void loadNotebook();
    void loadNarrative();
  });
  els.btnRefresh.addEventListener("click", async () => {
    showListLoading();
    await loadSessions();
    await Promise.all([loadNotebook(), loadNarrative()]);
  });
  els.btnLoad.addEventListener("click", () => {
    showListLoading();
    void loadNotebook();
    void loadNarrative();
  });
  els.btnMuteTonight.addEventListener("click", () => void mutate("mute_tonight", { hours: 10 }));
  els.btnUnmute?.addEventListener("click", () => void mutate("mute_tonight", { hours: 0 }));
  els.addForm.addEventListener("submit", onAdd);
  els.listHost.addEventListener("click", onListClick);
  els.btnNarrativeRefresh?.addEventListener("click", () => void loadNarrative());
  els.narrativePanel?.querySelectorAll("[data-narrative-action]").forEach((node) => {
    node.addEventListener("click", () => void mutateNarrative(node.dataset.narrativeAction));
  });
  els.narrativeIntents?.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-intent-id]");
    if (button) void mutateNarrative("cancel_intent", { intent_id: button.dataset.intentId });
  });
  els.narrativeCorrectForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    const patch = {};
    const activity = document.getElementById("narrativeActivity").value.trim();
    const availability = document.getElementById("narrativeAvailability").value;
    if (activity) patch.activity = activity;
    if (availability) patch.availability = availability;
    if (!Object.keys(patch).length) {
      els.narrativeStatus.textContent = "请填写活动或选择可交互状态。";
      els.narrativeStatus.classList.add("error");
      return;
    }
    void mutateNarrative("correct", { patch });
  });
  paintAddForm();
  showListLoading();
  await loadSessions();
  await Promise.all([loadNotebook(), loadNarrative()]);
}

boot();
