import {
  apiGet,
  apiPost,
  escapeHtml,
  formatTs,
  PRESENCE_LABEL,
  readyBridge,
  redactId,
  setLink,
  storageGet,
  storageSet,
} from "./api.js";
import { fillSessionSelect, renderNav } from "./shell.js";

const UMO_KEY = "cd_wire_umo";
const LANE_ZH = {
  speak: "开口",
  manners: "分寸安静",
  media: "媒体安静",
  rhythm: "作息安静",
  arbiter: "仲裁安静",
  proactive: "主动配额",
};

const els = {
  linkLamp: document.getElementById("linkLamp"),
  linkLabel: document.getElementById("linkLabel"),
  sessionSelect: document.getElementById("sessionSelect"),
  trackHint: document.getElementById("trackHint"),
  railCounts: document.getElementById("railCounts"),
  replayRail: document.getElementById("replayRail"),
  railEmpty: document.getElementById("railEmpty"),
  detailDialog: document.getElementById("detailDialog"),
  detailTitle: document.getElementById("detailTitle"),
  btnCloseDetail: document.getElementById("btnCloseDetail"),
  blockDetail: document.getElementById("blockDetail"),
  blockMeta: document.getElementById("blockMeta"),
  blockEvents: document.getElementById("blockEvents"),
  tonightNote: document.getElementById("tonightNote"),
  btnRefresh: document.getElementById("btnRefresh"),
  btnGhostTonight: document.getElementById("btnGhostTonight"),
  btnSensibleTonight: document.getElementById("btnSensibleTonight"),
  btnLivelyTonight: document.getElementById("btnLivelyTonight"),
};

let online = false;
let selectedUmo = storageGet(UMO_KEY, "");
let busy = false;
let blocks = [];
let archivedTopics = [];
let selectedIndex = -1;
let refreshRevision = 0;
const TOPIC_COLORS = ["speak", "media", "rhythm", "arbiter", "proactive", "manners"];
const annotationMessages = document.getElementById("annotationMessages");
const annotationStatus = document.getElementById("annotationStatus");
const annotationMetrics = document.getElementById("annotationMetrics");
let annotationRevision = 0;
let annotationData = null;
const ERROR_LABELS = { correct: "判断正确", topic_merge: "不同话题被合并", topic_split: "同话题被拆分", wrong_assignment: "选错已有话题", premature_assignment: "过早归类", reopen_miss: "遗漏历史话题", unknown: "无法判断" };

async function renderAnnotations(block) {
  const revision = ++annotationRevision;
  annotationData = null;
  annotationMessages.replaceChildren();
  annotationStatus.textContent = "";
  annotationMetrics.textContent = "";
  if (!block) return;
  const topics = [...blocks, ...archivedTopics].filter(b => b.session_id === block.session_id && b.topic_id && b.topic_id !== "UNKNOWN");
  annotationMessages.innerHTML = (block.messages || []).map((message, index) => {
    const targetId = `annotation-target-${index}`;
    const errorId = `annotation-error-${index}`;
    return `<div class="annotation-row"><p>${escapeHtml(message.text)}</p><p class="ops-note">系统判断：${escapeHtml(message.topic_id)} · ${escapeHtml(message.confidence)}${message.ambiguous ? " · 待确认" : ""}</p><label for="${targetId}">应归属</label><select id="${targetId}" data-target><option value="CORRECT">判断正确</option><option value="NEW">新话题</option><option value="UNKNOWN">无法判断</option>${topics.map(t => `<option value="${escapeHtml(t.topic_id)}">${escapeHtml(t.topic_title)} (${escapeHtml(t.topic_id)})</option>`).join("")}</select><label for="${errorId}">错误类型</label><select id="${errorId}" data-error>${Object.entries(ERROR_LABELS).filter(([key]) => key !== "correct").map(([key, label]) => `<option value="${key}">${label}</option>`).join("")}</select><button type="button" class="button" data-annotate="${index}">保存标注</button></div>`;
  }).join("");
  (block.messages || []).forEach((message, index) => {
    if (!Array.isArray(message.candidates) || !message.candidates.length) return;
    const hint = document.createElement("p");
    hint.className = "ops-note";
    hint.textContent = `候选证据（非概率）：${message.candidates.filter(Array.isArray).map(c => `${c[1]}: ${Number(c[0]).toFixed(2)}`).join(" · ")}`;
    annotationMessages.children[index].append(hint);
  });
  try {
    const data = await apiGet("topic_annotations", { session_key: block.session_id });
    if (revision !== annotationRevision) return;
    annotationData = data;
    annotationMetrics.textContent = `已标注 ${data.metrics.total} 条 · ${Object.entries(data.metrics.error_counts).map(([key, count]) => `${ERROR_LABELS[key] || key} ${count}`).join(" · ")}。${data.metrics.sample_note}`;
  } catch (err) {
    if (revision === annotationRevision) annotationStatus.textContent = err.message || "标注读取失败";
  }
}

function setTonightEnabled(ok) {
  for (const id of ["btnGhostTonight", "btnSensibleTonight", "btnLivelyTonight"]) {
    if (els[id]) els[id].disabled = !ok;
  }
}

function selectBlock(index) {
  selectedIndex = index;
  const nodes = els.replayRail.querySelectorAll(".replay-block");
  nodes.forEach(node => {
    const selected = Number(node.dataset.index) === index;
    node.classList.toggle("is-selected", selected);
    node.setAttribute("aria-pressed", String(selected));
  });
  els.blockEvents.replaceChildren();
  const block = blocks[index];
  void renderAnnotations(block);
  if (!block) {
    els.detailDialog.close();
    els.blockDetail.textContent = "点时间轨上的色块。";
    els.blockMeta.textContent = "";
    els.tonightNote.textContent = "";
    return;
  }
  els.detailTitle.textContent = block.topic_title || "未关联主题";
  const speak = (block.events || []).filter(event => event.action === "speak").length;
  els.blockDetail.textContent = `开口 ${speak} 次 · 安静 ${(block.events || []).length - speak} 次`;
  els.blockMeta.textContent = `${formatTs(block.start_ts)} — ${formatTs(block.end_ts)} · ${block.message_count || 0} 条消息`;
  const events = Array.isArray(block.events) && block.events.length ? block.events : [];
  els.blockEvents.innerHTML = events.map(event => `<li><time>${escapeHtml(formatTs(event.ts))}</time><div><strong>${escapeHtml(event.reason_zh || "未记录具体原因")}</strong><span>${escapeHtml([event.action === "speak" ? "开口" : "安静", LANE_ZH[event.lane] || "", event.association || "", event.session_id ? `会话 ${redactId(event.session_id)}` : "", event.reason_code || ""].filter(Boolean).join(" · "))}</span></div></li>`).join("");
  if (!speak) {
    els.tonightNote.textContent = "若觉得这段太安静，可用下面的分寸旋钮松一点。";
  } else {
    els.tonightNote.textContent = "若觉得这段太吵，可调到懂事或隐身。";
  }
}

function renderRail(data) {
  archivedTopics = Array.isArray(data.archived_topics) ? data.archived_topics : [];
  blocks = (Array.isArray(data.topic_blocks) ? data.topic_blocks : []).filter(block => block.topic_id && block.topic_id !== "UNKNOWN" && (!block.topic_status || block.topic_status === "committed"));
  const unassigned = Math.max(0, Number(data.unassigned_message_count) || 0);
  document.getElementById("unassignedNote").textContent = unassigned
    ? `最近保留的消息中有 ${unassigned} 条尚未形成话题，留白展示。零散聊天、图片和表情包不会自动合成一个话题。`
    : "尚未形成连续讨论的消息留白展示。群聊不需要时时刻刻都有话题。";
  els.railCounts.textContent = `${blocks.length} 个主题场景`;
  els.trackHint.textContent = selectedUmo ? `会话 ${redactId(selectedUmo)}` : "总览最近判断";
  if (!blocks.length) {
    els.replayRail.innerHTML = "";
    els.railEmpty.classList.remove("hidden");
    selectBlock(-1);
    return;
  }
  els.railEmpty.classList.add("hidden");
  const timestamp = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const first = Math.min(...blocks.map(block => timestamp(block.start_ts)));
  const last = Math.max(first + 1, ...blocks.map(block => timestamp(block.end_ts)));
  const span = last - first;
  const axis = Array.from({ length: 5 }, (_, i) => `<time>${escapeHtml(formatTs(first + span * i / 4))}</time>`).join("");
  els.replayRail.innerHTML = `<div class="gantt-chart"><div class="gantt-heading">聊天主题 / 时间</div><div class="gantt-axis">${axis}</div>${blocks.map((block, index) => {
    const lane = TOPIC_COLORS[index % TOPIC_COLORS.length];
    const left = Math.max(0, Math.min(99.5, (timestamp(block.start_ts) - first) / span * 100));
    const width = Math.max(0.5, Math.min(100 - left, (timestamp(block.end_ts) - timestamp(block.start_ts)) / span * 100));
    const title = block.topic_title || "未关联主题";
    const label = escapeHtml(title);
    const session = !selectedUmo && block.session_id ? `<small>会话 ${escapeHtml(redactId(block.session_id))}</small>` : "";
    return `<div class="gantt-label lane-${lane}"><span>${label}${session}</span></div><div class="gantt-track"><button type="button" class="replay-block lane-${lane}" data-index="${index}" style="left:min(${left}%, calc(100% - 84px));width:clamp(84px, ${width}%, 100%)" title="${label}" aria-label="${escapeHtml(`${title} · ${formatTs(block.start_ts)} · 查看详情`)}" aria-haspopup="dialog" aria-pressed="false"><span class="scene-reason">${label}</span></button></div>`;
  }).join("")}</div>`;
  selectBlock(-1);

}

async function refresh() {
  const revision = ++refreshRevision;
  try {
    const params = selectedUmo ? { umo: selectedUmo } : {};
    const data = await apiGet("replay", params);
    if (revision !== refreshRevision) return;
    online = true;
    setLink(els, true, "已连接");
    fillSessionSelect(els.sessionSelect, data.sessions || [], selectedUmo);
    renderRail(data);
    setTonightEnabled(true);
  } catch (err) {
    if (revision !== refreshRevision) return;
    online = false;
    setLink(els, false, (err && err.message) || "离线");
    setTonightEnabled(false);
    els.replayRail.innerHTML = "";
    blocks = [];
    selectBlock(-1);
    els.railCounts.textContent = "主题 —";
    els.railEmpty.classList.remove("hidden");
    els.blockDetail.textContent = "回放暂时不可用，请稍后刷新。";
  }
}

async function savePresence(value) {
  if (!online || busy) return;
  busy = true;
  els.tonightNote.textContent = "正在保存今晚分寸…";
  try {
    await apiPost("config", { config: { presence_knob: value } });
    els.tonightNote.textContent = `已设为「${PRESENCE_LABEL[value] || value}」。`;
    await refresh();
  } catch (err) {
    els.tonightNote.textContent = (err && err.message) || "保存失败";
  } finally {
    busy = false;
  }
}

async function boot() {
  renderNav("replay");
  annotationMessages.addEventListener("change", event => {
    if (!event.target.matches("[data-target]")) return;
    const row = event.target.closest(".annotation-row");
    row.querySelector("[data-error]").value = event.target.value === "UNKNOWN" ? "premature_assignment" : event.target.value === "NEW" ? "topic_merge" : "wrong_assignment";
  });
  annotationMessages.addEventListener("click", async event => {
    const button = event.target.closest("[data-annotate]");
    if (!button || button.disabled) return;
    const block = blocks[selectedIndex];
    const message = block?.messages?.[Number(button.dataset.annotate)];
    if (!message) return;
    const row = button.closest(".annotation-row");
    const expected = row.querySelector("[data-target]").value;
    const error = row.querySelector("[data-error]").value;
    const revision = annotationRevision;
    button.disabled = true;
    try {
      await apiPost("topic_annotations", { session_key: block.session_id, msg_id: message.msg_id, expected_topic: expected, error_type: error });
      if (revision !== annotationRevision) return;
      const data = await apiGet("topic_annotations", { session_key: block.session_id });
      if (revision !== annotationRevision) return;
      annotationData = data;
      annotationStatus.textContent = "已保存标注。";
      annotationMetrics.textContent = `已标注 ${data.metrics.total} 条 · ${Object.entries(data.metrics.error_counts).map(([key, count]) => `${ERROR_LABELS[key] || key} ${count}`).join(" · ")}。${data.metrics.sample_note}`;
    } catch (err) {
      if (revision === annotationRevision) annotationStatus.textContent = err.message || "标注保存失败";
    } finally { button.disabled = false; }
  });
  document.getElementById("btnExportAnnotations").addEventListener("click", () => {
    if (!annotationData) { annotationStatus.textContent = "请先等待标注加载完成。"; return; }
    const blob = new Blob([JSON.stringify(annotationData, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url; link.download = "topic-annotations.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  try {
    await readyBridge();
  } catch {
    /* bridge may still work for api calls */
  }
  els.sessionSelect.addEventListener("change", () => {
    selectedUmo = els.sessionSelect.value || "";
    storageSet(UMO_KEY, selectedUmo);
    void refresh();
  });
  els.replayRail.addEventListener("click", (event) => {
    const btn = event.target.closest(".replay-block");
    if (!btn) return;
    selectBlock(Number(btn.getAttribute("data-index")));
    els.detailDialog.showModal();
  });
  els.btnCloseDetail.addEventListener("click", () => els.detailDialog.close());
  els.btnRefresh.addEventListener("click", () => void refresh());
  els.btnGhostTonight.addEventListener("click", () => void savePresence("ghost"));
  els.btnSensibleTonight.addEventListener("click", () => void savePresence("sensible"));
  els.btnLivelyTonight.addEventListener("click", () => void savePresence("lively"));
  await refresh();
}

boot();
