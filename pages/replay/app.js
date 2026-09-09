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
let selectedIndex = -1;
let refreshRevision = 0;
const TOPIC_COLORS = ["speak", "media", "rhythm", "arbiter", "proactive", "manners"];

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
  blocks = Array.isArray(data.topic_blocks) ? data.topic_blocks : [];
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
