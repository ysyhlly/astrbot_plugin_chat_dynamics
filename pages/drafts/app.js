import {
  apiGet,
  apiPost,
  escapeHtml,
  formatTs,
  friendlyError,
  readyBridge,
  redactId,
  setLink,
} from "./api.js";
import { renderNav } from "./shell.js";

const els = {
  linkLamp: document.getElementById("linkLamp"),
  linkLabel: document.getElementById("linkLabel"),
  btnRefresh: document.getElementById("btnRefresh"),
  sessionFilter: document.getElementById("sessionFilter"),
  acceptTopic: document.getElementById("acceptTopic"),
  btnSelectAll: document.getElementById("btnSelectAll"),
  btnSelectNone: document.getElementById("btnSelectNone"),
  btnAccept: document.getElementById("btnAccept"),
  btnDismiss: document.getElementById("btnDismiss"),
  summaryLine: document.getElementById("summaryLine"),
  reviewStatus: document.getElementById("reviewStatus"),
  listHost: document.getElementById("listHost"),
  listEmpty: document.getElementById("listEmpty"),
  draftCount: document.getElementById("draftCount"),
  hiddenNote: document.getElementById("hiddenNote"),
};

let payload = null;
let selected = new Set();
let sessionFilter = "";
let busy = false;
let loadRevision = 0;
let confirming = false;

async function confirmReview(message) {
  // Native confirm is denied by the host sandbox (no allow-modals).
  if (confirming) return false;
  confirming = true;
  const dialog = document.createElement("dialog");
  dialog.setAttribute("aria-label", "确认审核操作");
  dialog.style.cssText = "max-width:min(560px,90vw);padding:24px;border:1px solid #888;border-radius:12px;";
  const text = document.createElement("p");
  text.textContent = message;
  text.style.whiteSpace = "pre-wrap";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "button";
  cancel.textContent = "取消";
  const accept = document.createElement("button");
  accept.type = "button";
  accept.className = "button button-primary";
  accept.textContent = "确认继续";
  dialog.append(text, cancel, accept);
  document.body.append(dialog);
  return new Promise(resolve => {
    const finish = value => { dialog.close(); dialog.remove(); confirming = false; resolve(value); };
    cancel.addEventListener("click", () => finish(false));
    accept.addEventListener("click", () => finish(true));
    dialog.addEventListener("cancel", event => { event.preventDefault(); finish(false); });
    dialog.showModal();
    cancel.focus();
  });
}

const pendingKey = "chat-dynamics:draft-review:pending:v1";
let pendingRequests = [];
let pendingStorageAvailable = true;
try {
  const stored = JSON.parse(sessionStorage.getItem(pendingKey) || "[]");
  if (Array.isArray(stored)) pendingRequests = stored.filter(body => body?.request_id && body?.session_key);
} catch { /* A corrupt record must not cause a new write. */ }
const checkPending = document.createElement("button");
checkPending.type = "button";
checkPending.className = "button button-primary";
checkPending.id = "btnCheckPending";
checkPending.textContent = "核对待确认请求";
els.reviewStatus.insertAdjacentElement("afterend", checkPending);
function persistPending(next = pendingRequests) {
  // AstrBot's opaque-origin sandbox denies sessionStorage. The server still
  // deduplicates requests by ID; keep that ID in memory for query/retry instead
  // of making browser storage a prerequisite for every review write.
  try {
    sessionStorage.setItem(pendingKey, JSON.stringify(next));
    pendingStorageAvailable = true;
  } catch {
    pendingStorageAvailable = false;
  }
  pendingRequests = next;
}
function requestId() {
  if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
  // LAN HTTP pages lack randomUUID, but getRandomValues is available there.
  // This is an idempotency key, not an authentication credential.
  if (typeof globalThis.crypto?.getRandomValues !== "function") {
    throw new Error("浏览器无法生成请求标识，请使用支持 Web Crypto 的浏览器。");
  }
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
}
function pendingNotice() {
  if (pendingRequests.length) {
    els.reviewStatus.textContent = `请求结果待确认：${pendingRequests.length} 个请求可能已生效，请核对待确认请求。`;
    if (!pendingStorageAvailable) {
      els.reviewStatus.textContent += "请先完成核对再刷新或离开页面；当前环境无法保存待确认记录。";
    }
    els.reviewStatus.classList.add("error");
  }
}
async function confirmRequest(body, initial = false) {
  let result;
  if (!initial) {
    try {
      const status = await apiGet("annotation_drafts", {session_key: body.session_key, request_id: body.request_id});
      if (status.state === "complete") result = status.result;
    } catch { /* Query failure is not evidence of write failure. */ }
  }
  if (result === undefined) {
    try { result = await apiPost("annotation_drafts", body); }
    catch {
      if (initial) return confirmRequest(body, false);
      const error = new Error("请求结果待确认");
      error.pending = true;
      throw error;
    }
  }
  if (!result || result.state === "pending") {
    const error = new Error("请求结果待确认");
    error.pending = true;
    throw error;
  }
  persistPending(pendingRequests.filter(item => item.request_id !== body.request_id));
  return result;
}
async function reviewPost(body) {
  body.request_id = requestId();
  persistPending([...pendingRequests, body]);
  return confirmRequest(body, true);
}
async function reconcilePending() {
  if (busy) return;
  busy = true;
  setEnabled();
  let confirmed = 0;
  try {
    for (const body of [...pendingRequests]) {
      try { await confirmRequest(body); confirmed += 1; }
      catch { /* Retain the original body and request id for another check. */ }
    }
    els.reviewStatus.textContent = `已核对 ${confirmed} 个请求。`;
    await load();
  } finally {
    busy = false;
    pendingNotice();
    setEnabled();
  }
}
checkPending.addEventListener("click", () => void reconcilePending());

const keyOf = (session, mid) => `${session}::${mid}`;

function pairOf(key) {
  const idx = key.indexOf("::");
  return { session: key.slice(0, idx), mid: key.slice(idx + 2) };
}

function yesNo(value) {
  return value === true ? "是" : value === false ? "否" : "未判";
}

function visibleSessions() {
  const sessions = payload?.sessions || [];
  return sessionFilter ? sessions.filter(s => s.session_key === sessionFilter) : sessions;
}

function itemBy(pair) {
  const group = (payload?.sessions || []).find(s => s.session_key === pair.session);
  return (group?.items || []).find(item => item.msg_id === pair.mid) || null;
}

function setEnabled() {
  const count = selected.size;
  const blocked = busy || pendingRequests.length > 0;
  checkPending.hidden = !pendingRequests.length;
  checkPending.disabled = busy;
  els.btnAccept.disabled = blocked || !count;
  els.btnDismiss.disabled = blocked || !count;
  els.btnSelectAll.disabled = busy;
  els.btnSelectNone.disabled = busy || !count;
  els.btnRefresh.disabled = busy;
  els.sessionFilter.disabled = busy;
  els.acceptTopic.disabled = busy;
  // `data-expired` marks controls that stay disabled regardless of busy: an
  // expired draft has no label to write, so re-enabling it here would break it.
  els.listHost.querySelectorAll("button, input").forEach(node => {
    node.disabled = blocked || node.hasAttribute("data-expired");
  });
}

function cardHtml(session, item) {
  const key = keyOf(session, item.msg_id);
  const checked = selected.has(key) ? " checked" : "";
  const disabled = busy ? " disabled" : "";
  // Every card renders the same control names, so spell out which draft each
  // one belongs to for screen readers.
  const shortId = String(item.msg_id || "").slice(0, 8) || "未知";
  const cardName = `草稿 ${shortId}`;
  const chips = [
    `该回复：${yesNo(item.expected_reply)}`,
    `对 Bot 说话：${yesNo(item.bot_targeted)}`,
  ];
  const confidence = Number(item.confidence);
  if (Number.isFinite(confidence)) chips.push(`置信度 ${confidence}`);
  const meta = [
    item.topic_id ? `话题 ${item.topic_id}` : "未形成话题",
    item.ts ? formatTs(item.ts) : "",
    item.annotated ? "已有人工标注（采纳会更新建议字段）" : "",
    item.saveable ? "" : (item.stale_reason === "session_gone"
      ? "已失效：会话的消息图已不在内存里（插件重启过），只能忽略"
      : "已失效：消息已超出保留窗口，只能忽略"),
  ].filter(Boolean).join(" · ");
  const acceptDisabled = busy || !item.saveable;
  const acceptTitle = item.saveable ? "" : " title=\"失效草稿无法采纳，只能忽略\"";
  const text = item.text || "（正文已隐藏；打开 console_show_message_content 后可显示）";
  return `<article class="draft-card${item.saveable ? "" : " is-stale"}">
    <label class="draft-check" title="选择这条"><input type="checkbox" aria-label="选择${escapeHtml(cardName)}" data-select data-session="${escapeHtml(session)}" data-mid="${escapeHtml(item.msg_id)}"${checked}${disabled}></label>
    <div class="draft-body">
      <p class="draft-text">${escapeHtml(text)}</p>
      <p class="ops-note">${escapeHtml(meta)}</p>
      <p class="draft-suggest">AI 建议：${chips.map(chip => `<span class="draft-chip">${escapeHtml(chip)}</span>`).join("")}</p>
      ${item.reason ? `<p class="ops-note">理由：${escapeHtml(item.reason)}</p>` : ""}
    </div>
    <div class="draft-actions">
      <button type="button" class="button button-primary" aria-label="采纳${escapeHtml(cardName)}" data-accept data-session="${escapeHtml(session)}" data-mid="${escapeHtml(item.msg_id)}"${acceptDisabled ? " disabled" : ""}${item.saveable ? "" : " data-expired"}${acceptTitle}>采纳</button>
      <button type="button" class="button button-quiet" aria-label="忽略${escapeHtml(cardName)}" data-dismiss data-session="${escapeHtml(session)}" data-mid="${escapeHtml(item.msg_id)}"${disabled}>忽略</button>
    </div>
  </article>`;
}

function updateSelection() {
  els.listHost.querySelectorAll("[data-select]").forEach(box => { box.checked = selected.has(keyOf(box.dataset.session, box.dataset.mid)); });
  els.summaryLine.textContent = `待审 ${payload?.total_drafts ?? 0} 条 · 已选 ${selected.size} 条`;
  setEnabled();
}

function render() {
  const sessions = payload?.sessions || [];
  // The option list is rebuilt from this payload, so a filter whose session is gone
  // would leave the control reading "全部会话" while the card area stayed empty.
  if (sessionFilter && !sessions.some(s => s.session_key === sessionFilter)) {
    sessionFilter = "";
  }
  els.sessionFilter.innerHTML = ['<option value="">全部会话</option>'].concat(
    sessions.map(s => `<option value="${escapeHtml(s.session_key)}"${s.session_key === sessionFilter ? " selected" : ""}>${escapeHtml(redactId(s.session_key))}（${(s.items || []).length} 条）</option>`),
  ).join("");
  const total = payload?.total_drafts ?? 0;
  const liveCount = sessions.reduce((sum, group) => sum + (group.items || []).filter(item => item.saveable).length, 0);
  const selectedExpired = [...selected].filter(key => itemBy(pairOf(key))?.saveable === false).length;
  els.summaryLine.textContent = total
    ? `待审 ${total} 条 · 可采纳 ${liveCount} 条 · ${sessions.length} 个会话 · 已选 ${selected.size} 条`
      + (selectedExpired ? `（其中 ${selectedExpired} 条已失效，采纳时会跳过）` : "")
    : "";
  els.hiddenNote.textContent = payload?.content_hidden
    ? "正文按默认脱敏隐藏；审批依据是模型给出的结论、置信度与理由。"
    : "";
  els.listEmpty.classList.toggle("hidden", total > 0);
  els.listHost.innerHTML = visibleSessions().map(group => {
    const items = group.items || [];
    const live = items.filter(item => item.saveable);
    const expired = items.filter(item => !item.saveable);
    const head = `<div class="draft-session-head">
      <strong>会话 ${escapeHtml(redactId(group.session_key))}</strong>
      <span class="ops-note">${group.generated_at ? `最近生成于 ${escapeHtml(formatTs(group.generated_at))}` : ""}${group.provider_id ? ` · 最近使用模型 ${escapeHtml(group.provider_id)}` : ""}</span>
      <button type="button" class="button button-danger" data-clear-session="${escapeHtml(group.session_key)}"${busy ? " disabled" : ""} data-clear-count="${items.length}">清空该会话草稿</button>
    </div>`;
    // Expired drafts are kept visible — silently dropping them would hide the
    // fact that a restart ate them — but they are never offered as savable.
    const sessionGone = expired.length > 0 && live.length === 0
      && expired.every(item => item.stale_reason === "session_gone");
    const expiredNote = expired.length
      ? `<p class="ops-note draft-expired-note">以下 ${expired.length} 条已失效（${sessionGone ? "插件重启后这条会话的消息图还没重建" : "消息超出保留窗口"}），无法采纳，只能忽略或清空：</p>`
      : "";
    return head
      + live.map(item => cardHtml(group.session_key, item)).join("")
      + expiredNote
      + expired.map(item => cardHtml(group.session_key, item)).join("");
  }).join("");
  const liveTotal = visibleSessions().reduce((sum, group) => sum + (group.items || []).filter(item => item.saveable).length, 0);
  els.draftCount.textContent = total ? `${total} 条（可采纳 ${liveTotal} 条）` : "0 条";
  setEnabled();
}

function showLoading() {
  els.listEmpty.classList.add("hidden");
  els.listHost.setAttribute("aria-busy", "true");
  els.listHost.innerHTML = '<p class="ops-note loading-note">正在读取待审草稿…</p>';
}

function showLoadError(message) {
  payload = null;
  els.listHost.removeAttribute("aria-busy");
  els.listHost.innerHTML = "";
  els.listEmpty.classList.remove("hidden");
  els.listEmpty.classList.add("is-error");
  els.listEmpty.textContent = message;
}

async function load() {
  const revision = ++loadRevision;
  try {
    const data = await apiGet("annotation_drafts");
    if (revision !== loadRevision) return;
    payload = data;
    els.listEmpty.classList.remove("is-error");
    selected = new Set([...selected].filter(key => {
      const { session, mid } = pairOf(key);
      const group = (data.sessions || []).find(s => s.session_key === session);
      return Boolean(group && (group.items || []).some(item => item.msg_id === mid));
    }));
    setLink(els, true, "已连接");
    render();
  } catch (err) {
    if (revision !== loadRevision) return;
    const message = friendlyError(err, "草稿列表读取失败。");
    setLink(els, false, message);
    els.reviewStatus.classList.add("error");
    els.reviewStatus.textContent = message;
    showLoadError("草稿列表暂时读不到，请点上方「刷新」重试。");
  }
}

async function apply(action, pairs, expiredSkipped = 0) {
  if (!pairs.length || busy || pendingRequests.length) return;
  if (action === "accept") {
    const overwritten = pairs.filter(pair => itemBy(pair)?.annotated).length;
    const changes = pairs.filter(pair => itemBy(pair)?.annotated).slice(0, 12).map(pair => {
      const item = itemBy(pair);
      return `${pair.mid}: 该回复 ${yesNo(item.annotation?.expected_reply)} → ${yesNo(item.expected_reply)}；对 Bot 说话 ${yesNo(item.annotation?.bot_targeted)} → ${yesNo(item.bot_targeted)}；话题 ${item.annotation?.expected_topic || "未审核"} → ${els.acceptTopic.value === "KEEP" ? "保留" : els.acceptTopic.selectedOptions[0]?.textContent}`;
    }).join("\n");
    if (overwritten && !await confirmReview(`这 ${pairs.length} 条里有 ${overwritten} 条已有人工标注，采纳会更新建议字段。\n${changes}${overwritten > 12 ? "\n其余条目同样更新上述字段。" : ""}\n继续吗？`)) return;
  }
  const topicChoice = els.acceptTopic.value;
  busy = true;
  setEnabled();
  const bySession = new Map();
  for (const pair of pairs) {
    if (!bySession.has(pair.session)) bySession.set(pair.session, []);
    bySession.get(pair.session).push(pair.mid);
  }
  let saved = 0;
  let removed = 0;
  let skipped = expiredSkipped;
  const failed = [];
  const totalSessions = bySession.size;
  let doneSessions = 0;
  els.reviewStatus.classList.remove("error");
  try {
    for (const [session, msgIds] of bySession) {
      doneSessions += 1;
      // Cross-session batches run one request per session; say where we are.
      if (totalSessions > 1) {
        els.reviewStatus.textContent = `正在处理第 ${doneSessions}/${totalSessions} 个会话…`;
      }
      for (let offset = 0; offset < msgIds.length; offset += 200) {
      const chunk = msgIds.slice(offset, offset + 200);
      const body = { action, session_key: session, msg_ids: chunk };
      if (action === "accept") {
        body.expected_topic = topicChoice;
        body.revisions = Object.fromEntries(chunk.map(mid => {
          const item = itemBy({session, mid});
          return [mid, { annotation_revision: item?.annotation_revision, draft_revision: item?.draft_revision }];
        }));
      }
      let result;
      try { result = await reviewPost(body); }
      catch (err) {
        chunk.forEach(mid => selected.add(keyOf(session, mid)));
        if (!err.pending) {
          chunk.forEach(mid => failed.push({msg_id: mid, error: err.message || String(err)}));
        }
        continue;
      }
      const unresolved = new Set([...(result.failed || []), ...(result.skipped || [])].map(row => row.msg_id));
      const succeeded = result.saved_ids || chunk.filter(mid => !unresolved.has(mid));
      succeeded.forEach(mid => selected.delete(keyOf(session, mid)));
      unresolved.forEach(mid => selected.add(keyOf(session, mid)));
      saved += Number(result.saved) || 0;
      removed += Number(result.removed) || 0;
      skipped += (result.skipped || []).length;
      for (const row of result.failed || []) failed.push(row);
      }
    }
    if (pendingRequests.length) {
      pendingNotice();
    } else if (action === "accept") {
      const topicLabel = els.acceptTopic.selectedOptions[0]?.textContent || "";
      const notes = [];
      if (skipped) notes.push(`${skipped} 条已失效的草稿被跳过（插件重启或消息超出保留窗口，只能忽略）`);
      if (failed.length) notes.push(`${failed.length} 条失败：${friendlyError(failed[0].error, "原因未知")}`);
      els.reviewStatus.textContent = saved
        ? `已采纳 ${saved} 条，按「${topicLabel}」写入标注（记录会注明采纳自草稿）。` + (notes.length ? `另有 ${notes.join("；")}。` : "")
        : `没有写入任何标注：${notes.join("；") || "这些草稿已经不能采纳了"}。`;
    } else {
      els.reviewStatus.textContent = `已忽略 ${removed} 条草稿，不会写入标注。` + (failed.length ? `${failed.length} 条失败，保留选择以便重试。` : "");
    }
    els.reviewStatus.classList.remove("error");
    els.reviewStatus.classList.toggle("error", failed.length > 0);
    await load();
  } catch (err) {
    els.reviewStatus.classList.add("error");
    els.reviewStatus.textContent = friendlyError(err, "操作失败，请稍后重试。");
  } finally {
    busy = false;
    render();
    pendingNotice();
  }
}

async function boot() {
  renderNav("drafts");
  els.btnRefresh.addEventListener("click", () => {
    showLoading();
    void load();
  });
  els.sessionFilter.addEventListener("change", () => { sessionFilter = els.sessionFilter.value; render(); });
  els.btnSelectAll.addEventListener("click", () => {
    visibleSessions().forEach(group => (group.items || []).forEach(item => selected.add(keyOf(group.session_key, item.msg_id))));
    updateSelection();
  });
  els.btnSelectNone.addEventListener("click", () => { selected.clear(); updateSelection(); });
  const selectedPairs = () => [...selected].map(pairOf);
  els.btnAccept.addEventListener("click", () => {
    // Expired drafts cannot become labels; filter them here so one restart
    // does not turn a batch accept into a wall of identical failures.
    const chosen = selectedPairs();
    const live = chosen.filter(pair => itemBy(pair)?.saveable);
    const expired = chosen.length - live.length;
    if (!live.length) {
      els.reviewStatus.classList.add("error");
      els.reviewStatus.textContent = `选中的 ${chosen.length} 条都已失效（插件重启或消息超出保留窗口），无法采纳；可以直接「忽略选中」把它们清掉。`;
      return;
    }
    void apply("accept", live, expired);
  });
  els.btnDismiss.addEventListener("click", async () => {
    const count = selected.size;
    if (!count) return;
    if (!await confirmReview(`忽略后这 ${count} 条草稿会被丢弃（不会写入标注），且无法恢复。继续吗？`)) return;
    void apply("dismiss", selectedPairs());
  });
  els.listHost.addEventListener("change", event => {
    const box = event.target.closest("[data-select]");
    if (!box) return;
    const key = keyOf(box.dataset.session, box.dataset.mid);
    if (box.checked) selected.add(key); else selected.delete(key);
    updateSelection();
  });
  els.listHost.addEventListener("click", async event => {
    const acceptBtn = event.target.closest("[data-accept]");
    if (acceptBtn) {
      void apply("accept", [{ session: acceptBtn.dataset.session, mid: acceptBtn.dataset.mid }]);
      return;
    }
    const dismissBtn = event.target.closest("[data-dismiss]");
    if (dismissBtn) {
      void apply("dismiss", [{ session: dismissBtn.dataset.session, mid: dismissBtn.dataset.mid }]);
      return;
    }
    const clearBtn = event.target.closest("[data-clear-session]");
    if (clearBtn && !busy && !pendingRequests.length) {
      const count = Number(clearBtn.dataset.clearCount || 0);
      if (!await confirmReview(`确定清空这个会话的 ${count} 条待审草稿吗？清空后无法恢复。`)) return;
      void (async () => {
        busy = true;
        setEnabled();
        try {
          await reviewPost({ action: "clear_session", session_key: clearBtn.dataset.clearSession });
          els.reviewStatus.classList.remove("error");
          els.reviewStatus.textContent = "已清空该会话的全部待审草稿。";
          selected.clear();
          await load();
        } catch (err) {
          els.reviewStatus.classList.add("error");
          els.reviewStatus.textContent = "请求结果待确认，请核对后再操作。";
        } finally {
          busy = false;
          render();
          pendingNotice();
        }
      })();
    }
  });
  try {
    await readyBridge();
  } catch {
    /* bridge may still work for api calls */
  }
  showLoading();
  await load();
  pendingNotice();
}

void boot();
