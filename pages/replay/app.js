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
let replayData = null;
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

const RECIPIENT_ERRORS = { correct: "判断正确", missed_bot: "漏判 Bot", false_bot: "误判为对 Bot 说", wrong_recipient: "收件人错误", missing_recipient: "遗漏收件人", subject_confusion: "混淆提及与称呼", unknown: "无法判断" };
const RECIPIENT_BOOLS = { recipient_correct: "收件人判断正确", bot_targeted: "在对 Bot 说话", expected_reply: "Bot 应该回复" };
function traceView(trace) {
  if (!trace || trace.routing_schema_version !== 2) return '<p class="ops-note">暂无决策记录</p>';
  const yesNo = value => value === true ? "是" : value === false ? "否" : "待决";
  const recipient = trace.recipient || {}, topic = trace.topic || {}, participation = trace.participation || {};
  const ids = trace.identifiers_redacted ? "ID 已隐藏" : (recipient.ids || []).join(", ") || "未确定";
  const summary = `收件人：${ids} · 对 Bot：${yesNo(recipient.bot_targeted)} · 话题歧义：${yesNo(topic.ambiguous)} · 收件人歧义：${yesNo(recipient.ambiguous)} · 参与：${participation.level || "待决"} · 应回复：${yesNo(participation.should_reply)}`;
  const parent = trace.parent || {};
  const routingRows = [["话题", topic.topic_id, topic.confidence], ["父消息", parent.parent_id || parent.message_id, parent.confidence], ["收件人", ids, recipient.confidence]];
  const routingHtml = routingRows.map(([name, id, score]) => `<p class="ops-note">${escapeHtml(name)}：${escapeHtml(trace.identifiers_redacted ? "ID 已隐藏" : id || "未确定")} · 分数 ${escapeHtml(score ?? "—")}</p>`).join("");
  const evidenceHtml = (trace.ledger?.entries || []).map(entry => `<li>${escapeHtml(entry.domain)} · ${escapeHtml(entry.code)}：${escapeHtml(entry.raw_value)}${entry.contribution == null ? "" : ` · 贡献 ${escapeHtml(entry.contribution)}`}</li>`).join("");
  return `${routingHtml}<details><summary>判断依据（启发式分数，未经概率校准）</summary><ul>${evidenceHtml || "<li>暂无证据明细</li>"}</ul></details><p class="ops-note" data-trace-summary>${escapeHtml(summary)}</p><details><summary>查看决策记录</summary><pre class="decision-trace">${escapeHtml(JSON.stringify(trace, null, 2))}</pre></details>`;
}

function recipientEditor(index) {
  const select = (key, label, options) => `<label for="recipient-${key}-${index}">${label}</label><select id="recipient-${key}-${index}" data-recipient="${key}"><option value="">未标注</option>${options}</select>`;
  return `<details class="recipient-editor"><summary>收件人与回复纠错（可选）</summary>${Object.entries(RECIPIENT_BOOLS).map(([key, label]) => select(key, label, '<option value="true">是</option><option value="false">否</option>')).join("")}
    ${["recipient_ids", "subject_ids"].map(key => `<label for="recipient-${key}-${index}">${key === "recipient_ids" ? "收件人 ID" : "被讨论对象 ID"}</label><input id="recipient-${key}-${index}" data-recipient="${key}" type="text" aria-describedby="recipient-help-${index}" placeholder="多个 ID 用逗号分隔">`).join("")}
    <p id="recipient-help-${index}" class="ops-note">留空表示未标注；输入 [] 表示没有对象。每项最多 256 字符，最多 64 项。</p>
    ${select("recipient_error_type", "收件人错误类型", Object.entries(RECIPIENT_ERRORS).map(([key, label]) => `<option value="${key}">${label}</option>`).join(""))}</details><p data-saved-annotation class="ops-note"></p>`;
}
function draftNote(draft) {
  if (!draft || typeof draft !== "object") return "";
  const parts = [];
  if (typeof draft.expected_reply === "boolean") parts.push(`该回复：${draft.expected_reply ? "是" : "否"}`);
  if (typeof draft.bot_targeted === "boolean") parts.push(`对 Bot 说话：${draft.bot_targeted ? "是" : "否"}`);
  const confidence = Number(draft.confidence);
  const confidenceText = Number.isFinite(confidence) ? ` · 置信度 ${confidence}` : "";
  const reason = draft.reason ? ` · ${escapeHtml(draft.reason)}` : "";
  return `<p class="ops-note" data-draft-note>AI 草稿：${escapeHtml(parts.join(" · "))}${confidenceText}${reason} <button type="button" class="button" data-apply-draft>按草稿填写</button></p>`;
}
function applyDraftToRow(row, draft) {
  if (!draft) return false;
  row.querySelectorAll("[data-recipient]").forEach(input => {
    const value = draft[input.dataset.recipient];
    if (value === undefined || value === null) return;
    input.value = typeof value === "boolean" ? String(value) : Array.isArray(value) ? value.join(", ") : String(value);
  });
  row.dataset.dirty = "1";
  return true;
}
function draftFor(message) {
  if (!message || !annotationData) return null;
  return (annotationData.drafts || {})[message.msg_id] || null;
}

function applyAnnotationData(data, block, prefill = false) {
  annotationData = data;
  annotationMetrics.textContent = `已标注 ${data.metrics.total} 条 · 收件人标注 ${data.recipient_metrics?.total || 0} 条 · ${Object.entries(data.metrics.error_counts).map(([key, count]) => `${ERROR_LABELS[key] || key} ${count}`).join(" · ")}。${data.metrics.sample_note}`;
  (block.messages || []).forEach((message, index) => {
    const row = annotationMessages.children[index];
    if (!row) return;
    const record = (data.records || []).find(item => item.msg_id === message.msg_id);
    // The draft is shown whether or not a label exists yet — an unlabelled
    // message is the one a draft is for.
    const note = row.querySelector("[data-draft-note]");
    if (note) note.remove();
    const draft = draftFor(message);
    if (draft) row.querySelector("[data-saved-annotation]").insertAdjacentHTML("afterend", draftNote(draft));
    if (!record) return;
    const descriptions = Object.entries(RECIPIENT_BOOLS).filter(([key]) => typeof record[key] === "boolean").map(([key, label]) => `${label}：${record[key] ? "是" : "否"}`);
    for (const [key, label] of [["recipient_ids", "收件人"], ["subject_ids", "讨论对象"]]) {
      if (Array.isArray(record[key])) descriptions.push(`${label}：${record[key].join(", ") || "无"}`);
    }
    if (record.recipient_error_type) descriptions.push(RECIPIENT_ERRORS[record.recipient_error_type] || record.recipient_error_type);
    row.querySelector("[data-saved-annotation]").textContent = `已保存：话题 ${record.expected_topic}${descriptions.length ? " · " + descriptions.join(" · ") : ""}`;
    if (prefill && !row.dataset.dirty) {
      const target = row.querySelector("[data-target]");
      target.value = record.error_type === "correct" ? "CORRECT" : record.expected_topic;
      if (!target.value) target.value = "CORRECT";
      if (record.error_type !== "correct") row.querySelector("[data-error]").value = record.error_type;
      row.querySelectorAll("[data-recipient]").forEach(input => {
        const value = record[input.dataset.recipient];
        input.value = Array.isArray(value) ? value.length ? value.join(", ") : "[]" : value === undefined ? "" : String(value);
      });
    }
  });
}
function recipientValues(row) {
  const result = {};
  row.querySelectorAll("[data-recipient]").forEach(input => {
    const key = input.dataset.recipient, value = input.value.trim();
    if (!value) return;
    if (key.endsWith("_ids")) {
      const ids = value === "[]" ? [] : [...new Set(value.split(/[,，]/).map(id => id.trim()).filter(Boolean))];
      if (ids.length > 64 || ids.some(id => id.length > 256)) throw new Error("ID 最多 64 项，每项最多 256 字符。");
      result[key] = ids;
    } else result[key] = key in RECIPIENT_BOOLS ? value === "true" : value;
  });
  return result;
}

async function renderAnnotations(block) {
  const revision = ++annotationRevision;
  annotationData = null;
  annotationMessages.replaceChildren();
  annotationStatus.textContent = "";
  annotationMetrics.textContent = "";
  if (!block) return;
  const topics = [...(replayData?.topic_blocks || blocks), ...archivedTopics].filter(b => b.session_id === block.session_id && b.topic_id && b.topic_id !== "UNKNOWN");
  annotationMessages.innerHTML = (block.messages || []).map((message, index) => {
    const targetId = `annotation-target-${index}`;
    const errorId = `annotation-error-${index}`;
    return `<div class="annotation-row"><p>${escapeHtml(message.text)}</p><p class="ops-note">系统判断：${escapeHtml(message.topic_id)} · ${escapeHtml(message.confidence)}${message.ambiguous ? " · 待确认" : ""}</p><label for="${targetId}">应归属</label><select id="${targetId}" data-target><option value="CORRECT">判断正确</option><option value="NEW">新话题</option><option value="UNKNOWN">无法判断</option>${topics.map(t => `<option value="${escapeHtml(t.topic_id)}">${escapeHtml(t.topic_title)} (${escapeHtml(t.topic_id)})</option>`).join("")}</select><label for="${errorId}">错误类型</label><select id="${errorId}" data-error>${Object.entries(ERROR_LABELS).filter(([key]) => key !== "correct").map(([key, label]) => `<option value="${key}">${label}</option>`).join("")}</select>${traceView(message.decision_trace)}${recipientEditor(index)}<button type="button" class="button" data-annotate="${index}">保存标注</button></div>`;
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
    applyAnnotationData(data, block, true);
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
    els.tonightNote.textContent = "这段主题没有开口记录。可按需要调整全局参与程度。";
  } else {
    els.tonightNote.textContent = "参与程度会应用于全局会话，可按需要调整。";
  }
}

function renderRail(data) {
  replayData = data;
  archivedTopics = Array.isArray(data.archived_topics) ? data.archived_topics : [];
  blocks = (Array.isArray(data.topic_blocks) ? data.topic_blocks : []).filter(block => block.topic_id && block.topic_id !== "UNKNOWN" && (!block.topic_status || block.topic_status === "committed"));
  const allBlocks = blocks;
  const query = document.getElementById("topicSearch").value.trim().toLocaleLowerCase();
  const decision = document.getElementById("decisionFilter").value;
  const speaks = block => (block.events || []).some(event => event.action === "speak");
  const events = allBlocks.flatMap(block => block.events || []);
  document.getElementById("summaryTopics").textContent = allBlocks.length;
  document.getElementById("summaryMessages").textContent = allBlocks.reduce((sum, block) => sum + (Number(block.message_count) || 0), 0);
  document.getElementById("summaryDecisions").textContent = `${events.filter(event => event.action === "speak").length} / ${events.filter(event => event.action !== "speak").length}`;
  blocks = allBlocks.filter(block => (!query || (block.topic_title || "").toLocaleLowerCase().includes(query)) && (decision === "all" || (decision === "speak" ? speaks(block) : !speaks(block))));
  els.railEmpty.textContent = allBlocks.length ? "没有符合筛选条件的主题。试试其他关键词或参与情况。" : "目前没有形成话题，留白是正常状态。出现持续、集中的讨论后才会显示话题。";
  const unassigned = Math.max(0, Number(data.unassigned_message_count) || 0);
  document.getElementById("unassignedNote").textContent = unassigned
    ? `最近保留的消息中有 ${unassigned} 条尚未形成话题，留白展示。零散聊天、图片和表情包不会自动合成一个话题。`
    : "尚未形成连续讨论的消息留白展示。群聊不需要时时刻刻都有话题。";
  els.railCounts.textContent = `${blocks.length} 个主题场景`;
  els.trackHint.textContent = selectedUmo ? `会话 ${redactId(selectedUmo)}` : "总览最近判断";
  if (Number.isInteger(data.message_limit) && Number.isInteger(data.retained_message_count)) {
    els.trackHint.textContent += ` · 当前回看 ${data.retained_message_count} 条 · 每会话最多 ${data.message_limit} 条`;
  }
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
    replayData = null;
    for (const id of ["summaryTopics", "summaryMessages", "summaryDecisions"]) document.getElementById(id).textContent = "—";
    els.railEmpty.textContent = "回放暂时不可用，请检查连接后刷新。";
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
  document.getElementById("topicSearch").addEventListener("input", () => { if (replayData) renderRail(replayData); });
  document.getElementById("decisionFilter").addEventListener("change", () => { if (replayData) renderRail(replayData); });
  const markDirty = event => {
    const row = event.target.closest(".annotation-row");
    if (row) row.dataset.dirty = "true";
  };
  annotationMessages.addEventListener("input", markDirty);
  annotationMessages.addEventListener("change", event => {
    markDirty(event);
    if (!event.target.matches("[data-target]")) return;
    const row = event.target.closest(".annotation-row");
    row.querySelector("[data-error]").value = event.target.value === "UNKNOWN" ? "premature_assignment" : event.target.value === "NEW" ? "topic_merge" : "wrong_assignment";
  });
  annotationMessages.addEventListener("click", async event => {
    const applyButton = event.target.closest("[data-apply-draft]");
    if (applyButton) {
      const row = applyButton.closest(".annotation-row");
      const index = [...annotationMessages.children].indexOf(row);
      const message = blocks[selectedIndex]?.messages?.[index];
      if (applyDraftToRow(row, draftFor(message))) {
        annotationStatus.textContent = ("已按草稿填写收件人与回复标签；话题仍由你选择，确认后按保存标注。"
          + "保存后记录里会写明这条采纳了草稿。");
      }
      return;
    }
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
      await apiPost("topic_annotations", { session_key: block.session_id, msg_id: message.msg_id, expected_topic: expected, error_type: error, ...recipientValues(row) });
      if (revision !== annotationRevision) return;
      const data = await apiGet("topic_annotations", { session_key: block.session_id });
      if (revision !== annotationRevision) return;
      applyAnnotationData(data, block);
      annotationStatus.textContent = "已保存标注。";

    } catch (err) {
      if (revision === annotationRevision) annotationStatus.textContent = err.message || "标注保存失败";
    } finally { button.disabled = false; }
  });
  document.getElementById("btnDraftAnnotations").addEventListener("click", async () => {
    const block = blocks[selectedIndex];
    const button = document.getElementById("btnDraftAnnotations");
    if (!block) { annotationStatus.textContent = "先选一个会话。"; return; }
    button.disabled = true;
    annotationStatus.textContent = "正在生成 AI 草稿…";
    try {
      // The model call is not a panel read: it needs its own budget.
      const data = await apiPost("annotation_draft",
        { session_key: block.session_id, refresh: true }, { timeoutMs: 180000 });
      const asked = data?.stats?.asked ?? 0;
      const drafts = data?.drafts || {};
      const drafted = Object.keys(drafts).length;
      // Drafts cover the session's recent window, not this page: messages
      // without a committed topic never render here, so say how many of the
      // drafts actually land on the block the user is looking at.
      const onPage = (block.messages || []).filter(m => drafts[m.msg_id]).length;
      annotationStatus.textContent = data?.state === "fresh"
        ? `已生成 ${drafted} 条草稿（窗口内可起草 ${asked} 条），其中 ${onPage} 条对应本页消息。草稿不是标注：确认后按「保存标注」才生效。`
        : (data?.reason || "本次没有生成草稿。");
      const refreshed = await apiGet("topic_annotations", { session_key: block.session_id });
      applyAnnotationData(refreshed, block);
    } catch (err) {
      annotationStatus.textContent = err.message || "生成草稿失败";
    } finally { button.disabled = false; }
  });
  document.getElementById("btnApplyDrafts").addEventListener("click", () => {
    const block = blocks[selectedIndex];
    let filled = 0;
    annotationMessages.querySelectorAll(".annotation-row").forEach((row, index) => {
      const message = block?.messages?.[index];
      if (applyDraftToRow(row, draftFor(message))) filled += 1;
    });
    const stored = Object.keys(annotationData?.drafts || {}).length;
    annotationStatus.textContent = filled
      ? `已按草稿填写 ${filled} 条；话题仍由你选择，逐条确认后按保存标注。`
      : stored
        ? `本会话存有 ${stored} 条草稿，但都对应其他消息：草稿覆盖该会话最近窗口里未标注的消息，包含尚未形成话题、不在任何回放块里的那些。`
        : "本页没有可用的草稿，先点「生成 AI 草稿」。";
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
