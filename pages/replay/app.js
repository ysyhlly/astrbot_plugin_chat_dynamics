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
  blockDetail: document.getElementById("blockDetail"),
  blockMeta: document.getElementById("blockMeta"),
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

function setTonightEnabled(ok) {
  for (const id of ["btnGhostTonight", "btnSensibleTonight", "btnLivelyTonight"]) {
    if (els[id]) els[id].disabled = !ok;
  }
}

function blockWidth(block, span) {
  const start = Number(block.start_ts || 0);
  const end = Number(block.end_ts || start);
  const dur = Math.max(8, end - start);
  const pct = span > 0 ? dur / span : 1;
  return Math.max(18, Math.min(120, Math.round(pct * 220)));
}

function selectBlock(index) {
  selectedIndex = index;
  const nodes = els.replayRail.querySelectorAll(".replay-block");
  nodes.forEach((node, i) => node.classList.toggle("is-selected", i === index));
  const block = blocks[index];
  if (!block) {
    els.blockDetail.textContent = "点时间轨上的色块。";
    els.blockMeta.textContent = "";
    return;
  }
  const lane = LANE_ZH[block.lane] || block.lane || "";
  const action = block.action === "speak" ? "开口" : "安静";
  els.blockDetail.textContent = block.reason_zh || `${action} · ${lane}`;
  const meta = [
    formatTs(block.start_ts),
    lane,
    block.count > 1 ? `${block.count} 次相近判断` : "",
    block.reason_code || "",
  ].filter(Boolean);
  els.blockMeta.textContent = meta.join(" · ");
  if (block.action === "silent") {
    els.tonightNote.textContent = "若觉得这段太安静，可用下面的分寸旋钮松一点。";
  } else {
    els.tonightNote.textContent = "若觉得这段太吵，可调到懂事或隐身。";
  }
}

function renderRail(data) {
  blocks = Array.isArray(data.blocks) ? data.blocks : [];
  const speak = Number(data.speak_count || 0);
  const silent = Number(data.silent_count || 0);
  els.railCounts.textContent = `开口 ${speak} · 安静 ${silent}`;
  els.trackHint.textContent = selectedUmo ? `会话 ${redactId(selectedUmo)}` : "总览最近判断";
  if (!blocks.length) {
    els.replayRail.innerHTML = "";
    els.railEmpty.classList.remove("hidden");
    selectBlock(-1);
    return;
  }
  els.railEmpty.classList.add("hidden");
  const first = Number(blocks[0].start_ts || 0);
  const last = Number(blocks[blocks.length - 1].end_ts || first);
  const span = Math.max(1, last - first);
  els.replayRail.innerHTML = blocks
    .map((block, index) => {
      const lane = block.lane || (block.action === "speak" ? "speak" : "manners");
      const label = escapeHtml(block.reason_zh || LANE_ZH[lane] || lane);
      return `<button type="button" class="replay-block lane-${escapeHtml(lane)}" data-action="${escapeHtml(
        block.action || "silent"
      )}" data-index="${index}" style="flex-basis:${blockWidth(block, span)}px" title="${label}" aria-label="${label}"></button>`;
    })
    .join("");
  selectBlock(blocks.length - 1);
}

async function refresh() {
  try {
    const params = selectedUmo ? { umo: selectedUmo } : {};
    const data = await apiGet("replay", params);
    online = true;
    setLink(els, true, "已连接");
    fillSessionSelect(els.sessionSelect, data.sessions || [], selectedUmo);
    renderRail(data);
    setTonightEnabled(true);
  } catch (err) {
    online = false;
    setLink(els, false, (err && err.message) || "离线");
    setTonightEnabled(false);
    els.replayRail.innerHTML = "";
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
  });
  els.btnRefresh.addEventListener("click", () => void refresh());
  els.btnGhostTonight.addEventListener("click", () => void savePresence("ghost"));
  els.btnSensibleTonight.addEventListener("click", () => void savePresence("sensible"));
  els.btnLivelyTonight.addEventListener("click", () => void savePresence("lively"));
  await refresh();
}

boot();
