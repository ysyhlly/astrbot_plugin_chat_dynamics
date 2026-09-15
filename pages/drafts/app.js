import {
  apiGet,
  apiPost,
  escapeHtml,
  formatTs,
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

const keyOf = (session, mid) => `${session}::${mid}`;

function yesNo(value) {
  return value === true ? "是" : value === false ? "否" : "未判";
}

function visibleSessions() {
  const sessions = payload?.sessions || [];
  return sessionFilter ? sessions.filter(s => s.session_key === sessionFilter) : sessions;
}

function setEnabled() {
  const count = selected.size;
  els.btnAccept.disabled = busy || !count;
  els.btnDismiss.disabled = busy || !count;
  els.btnSelectAll.disabled = busy;
  els.btnSelectNone.disabled = busy || !count;
  els.btnRefresh.disabled = busy;
  els.sessionFilter.disabled = busy;
  els.acceptTopic.disabled = busy;
  els.listHost.querySelectorAll("button, input").forEach(node => { node.disabled = busy; });
}

function cardHtml(session, item) {
  const key = keyOf(session, item.msg_id);
  const checked = selected.has(key) ? " checked" : "";
  const disabled = busy ? " disabled" : "";
  const chips = [
    `该回复：${yesNo(item.expected_reply)}`,
    `对 Bot 说话：${yesNo(item.bot_targeted)}`,
  ];
  const confidence = Number(item.confidence);
  if (Number.isFinite(confidence)) chips.push(`置信度 ${confidence}`);
  const meta = [
    item.topic_id ? `话题 ${item.topic_id}` : "未形成话题",
    item.ts ? formatTs(item.ts) : "",
    item.annotated ? "已有人工标注（采纳会覆盖）" : "",
    item.saveable ? "" : "消息已滚出窗口，无法保存",
  ].filter(Boolean).join(" · ");
  const text = item.text || "（正文已隐藏；打开 console_show_message_content 后可显示）";
  return `<article class="draft-card${item.saveable ? "" : " is-stale"}">
    <label class="draft-check" title="选择这条"><input type="checkbox" data-select data-session="${escapeHtml(session)}" data-mid="${escapeHtml(item.msg_id)}"${checked}${disabled}></label>
    <div class="draft-body">
      <p class="draft-text">${escapeHtml(text)}</p>
      <p class="ops-note">${escapeHtml(meta)}</p>
      <p class="draft-suggest">AI 建议：${chips.map(chip => `<span class="draft-chip">${escapeHtml(chip)}</span>`).join("")}</p>
      ${item.reason ? `<p class="ops-note">理由：${escapeHtml(item.reason)}</p>` : ""}
    </div>
    <div class="draft-actions">
      <button type="button" class="button button-primary" data-accept data-session="${escapeHtml(session)}" data-mid="${escapeHtml(item.msg_id)}"${disabled}>采纳</button>
      <button type="button" class="button button-quiet" data-dismiss data-session="${escapeHtml(session)}" data-mid="${escapeHtml(item.msg_id)}"${disabled}>忽略</button>
    </div>
  </article>`;
}

function render() {
  const sessions = payload?.sessions || [];
  els.sessionFilter.innerHTML = ['<option value="">全部会话</option>'].concat(
    sessions.map(s => `<option value="${escapeHtml(s.session_key)}"${s.session_key === sessionFilter ? " selected" : ""}>${escapeHtml(redactId(s.session_key))}（${(s.items || []).length} 条）</option>`),
  ).join("");
  const total = payload?.total_drafts ?? 0;
  els.draftCount.textContent = `${total} 条`;
  els.summaryLine.textContent = total
    ? `待审 ${total} 条 · ${sessions.length} 个会话 · 已选 ${selected.size} 条`
    : "";
  els.hiddenNote.textContent = payload?.content_hidden
    ? "正文按默认脱敏隐藏；审批依据是模型给出的结论、置信度与理由。"
    : "";
  els.listEmpty.classList.toggle("hidden", total > 0);
  els.listHost.innerHTML = visibleSessions().map(group => {
    const head = `<div class="draft-session-head">
      <strong>会话 ${escapeHtml(redactId(group.session_key))}</strong>
      <span class="ops-note">${group.generated_at ? `生成于 ${escapeHtml(formatTs(group.generated_at))}` : ""}${group.provider_id ? ` · 模型 ${escapeHtml(group.provider_id)}` : ""}</span>
      <button type="button" class="button button-quiet" data-clear-session="${escapeHtml(group.session_key)}"${busy ? " disabled" : ""}>清空该会话草稿</button>
    </div>`;
    return head + (group.items || []).map(item => cardHtml(group.session_key, item)).join("");
  }).join("");
  setEnabled();
}

async function load() {
  const revision = ++loadRevision;
  try {
    const data = await apiGet("annotation_drafts");
    if (revision !== loadRevision) return;
    payload = data;
    selected = new Set([...selected].filter(key => {
      const [session, mid] = key.split("::");
      const group = (data.sessions || []).find(s => s.session_key === session);
      return Boolean(group && (group.items || []).some(item => item.msg_id === mid));
    }));
    setLink(els, true, "已连接");
    render();
  } catch (err) {
    if (revision !== loadRevision) return;
    setLink(els, false, (err && err.message) || "离线");
    els.reviewStatus.textContent = err.message || "草稿列表读取失败";
  }
}

async function apply(action, pairs) {
  if (!pairs.length || busy) return;
  busy = true;
  setEnabled();
  const bySession = new Map();
  for (const pair of pairs) {
    if (!bySession.has(pair.session)) bySession.set(pair.session, []);
    bySession.get(pair.session).push(pair.mid);
  }
  let saved = 0;
  let removed = 0;
  const failed = [];
  try {
    for (const [session, msgIds] of bySession) {
      const body = { action, session_key: session, msg_ids: msgIds };
      if (action === "accept") body.expected_topic = els.acceptTopic.value;
      const result = await apiPost("annotation_drafts", body);
      saved += Number(result.saved) || 0;
      removed += Number(result.removed) || 0;
      for (const row of result.failed || []) failed.push(row);
    }
    if (action === "accept") {
      const topicLabel = els.acceptTopic.selectedOptions[0]?.textContent || "";
      els.reviewStatus.textContent = `已采纳 ${saved} 条，按「${topicLabel}」写入标注（记录会注明采纳自草稿）。`
        + (failed.length ? `另有 ${failed.length} 条失败：${failed[0].error}` : "");
    } else {
      els.reviewStatus.textContent = `已忽略 ${removed} 条草稿，不会写入标注。`;
    }
    selected.clear();
    await load();
  } catch (err) {
    els.reviewStatus.textContent = err.message || "操作失败";
  } finally {
    busy = false;
    render();
  }
}

async function boot() {
  renderNav("drafts");
  els.btnRefresh.addEventListener("click", () => void load());
  els.sessionFilter.addEventListener("change", () => { sessionFilter = els.sessionFilter.value; render(); });
  els.btnSelectAll.addEventListener("click", () => {
    visibleSessions().forEach(group => (group.items || []).forEach(item => selected.add(keyOf(group.session_key, item.msg_id))));
    render();
  });
  els.btnSelectNone.addEventListener("click", () => { selected.clear(); render(); });
  els.btnAccept.addEventListener("click", () => {
    const pairs = [...selected].map(key => {
      const idx = key.indexOf("::");
      return { session: key.slice(0, idx), mid: key.slice(idx + 2) };
    });
    void apply("accept", pairs);
  });
  els.btnDismiss.addEventListener("click", () => {
    const pairs = [...selected].map(key => {
      const idx = key.indexOf("::");
      return { session: key.slice(0, idx), mid: key.slice(idx + 2) };
    });
    void apply("dismiss", pairs);
  });
  els.listHost.addEventListener("change", event => {
    const box = event.target.closest("[data-select]");
    if (!box) return;
    const key = keyOf(box.dataset.session, box.dataset.mid);
    if (box.checked) selected.add(key); else selected.delete(key);
    render();
  });
  els.listHost.addEventListener("click", event => {
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
    if (clearBtn && !busy) {
      void (async () => {
        busy = true;
        setEnabled();
        try {
          await apiPost("annotation_drafts", { action: "clear_session", session_key: clearBtn.dataset.clearSession });
          els.reviewStatus.textContent = "已清空该会话的全部待审草稿。";
          selected.clear();
          await load();
        } catch (err) {
          els.reviewStatus.textContent = err.message || "清空失败";
        } finally {
          busy = false;
          render();
        }
      })();
    }
  });
  try {
    await readyBridge();
  } catch {
    /* bridge may still work for api calls */
  }
  await load();
}

void boot();
