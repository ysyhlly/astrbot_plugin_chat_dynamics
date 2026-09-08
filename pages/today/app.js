import {
  apiGet,
  apiPost,
  escapeHtml,
  formatTs,
  OCCASION_LABEL,
  PRESENCE_LABEL,
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
  partnerSheet: document.getElementById("partnerSheet"),
  occasionCapsule: document.getElementById("occasionCapsule"),
  oneLiner: document.getElementById("oneLiner"),
  emptyHint: document.getElementById("emptyHint"),
  knobTarget: document.getElementById("knobTarget"),
  thermoFill: document.getElementById("thermoFill"),
  thermoQuiet: document.getElementById("thermoQuiet"),
  thermoIntervene: document.getElementById("thermoIntervene"),
  decisionList: document.getElementById("decisionList"),
  decisionEmpty: document.getElementById("decisionEmpty"),
  tonightNote: document.getElementById("tonightNote"),
  btnRefresh: document.getElementById("btnRefresh"),
  btnGhostTonight: document.getElementById("btnGhostTonight"),
  btnSensibleTonight: document.getElementById("btnSensibleTonight"),
  btnLivelyTonight: document.getElementById("btnLivelyTonight"),
  btnMuteTonight: document.getElementById("btnMuteTonight"),
};

let online = false;
let selectedUmo = storageGet(UMO_KEY, "");
let busy = false;

function setTonightEnabled(ok) {
  for (const id of ["btnGhostTonight", "btnSensibleTonight", "btnLivelyTonight"]) {
    if (els[id]) els[id].disabled = !ok;
  }
  if (els.btnMuteTonight) els.btnMuteTonight.disabled = !ok || !selectedUmo;
}

function renderPartner(air) {
  const sl = air.selflearning || {};
  const mg = air.media_gate || {};
  const mm = mg.multimodal || {};
  const rhythm = air.daily_rhythm || {};
  const rows = [
    ["selflearning", sl.lamp || sl.status || "未知", sl.detail || ""],
    ["图片门闩", mg.image ? "开" : "关", "不确定图文倾向旁听"],
    ["语音门闩", mg.voice ? "开" : "关", "不确定语音倾向旁听"],
    ["理解接话 L2", mg.understand_reply ? "开" : "关", "默认关，耗额度"],
    ["多模态", mm.lamp || "启发式", mm.detail || ""],
    [
      "今日作息",
      air.rhythm_state_zh || rhythm.state_zh || "还醒着",
      air.rhythm_summary || rhythm.last_reason_zh || "氛围道具·非真日历",
    ],
  ];
  els.partnerSheet.innerHTML = rows
    .map(
      ([k, v, d]) =>
        `<div class="partner-row"><span>${escapeHtml(k)}</span><span><strong>${escapeHtml(v)}</strong>${
          d ? `<span class="meta"> · ${escapeHtml(d)}</span>` : ""
        }</span></div>`
    )
    .join("");
}

function renderOccasion(air) {
  const occ = air.occasion || {};
  const kind = occ.kind || "neutral";
  const label = OCCASION_LABEL[kind] || kind;
  const conf = Math.round(Number(air.confidence ?? occ.confidence ?? 0) * 100);
  const rhythmZh = air.rhythm_state_zh || (air.daily_rhythm || {}).state_zh || "";
  const rhythmBit = rhythmZh ? ` · 作息 ${escapeHtml(rhythmZh)}` : "";
  els.occasionCapsule.innerHTML = `${escapeHtml(label)} <span class="conf">把握 ${conf || "—"}%</span>${rhythmBit}`;
  els.oneLiner.textContent =
    air.rhythm_summary || air.one_liner || occ.reason_zh || "暂无白话摘要";
  if (air.empty) {
    els.emptyHint.innerHTML = `暂无活跃群 · 可去侧栏「分寸台」`;
  } else {
    els.emptyHint.textContent = selectedUmo ? `会话 ${redactId(selectedUmo)}` : "总览";
  }
}

function renderThermo(air) {
  const t = air.thermometer || {};
  const intervene = Number(t.intervene_count || air.intervene_count || 0);
  const quiet = Number(t.quiet_count || air.quiet_count || 0);
  const ratio = Number(t.intervene_ratio);
  const pct = Number.isFinite(ratio) ? Math.round(ratio * 100) : intervene + quiet ? Math.round((intervene / (intervene + quiet)) * 100) : 0;
  els.thermoFill.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  els.thermoQuiet.textContent = `安静 ${quiet}`;
  els.thermoIntervene.textContent = `插话 ${intervene}`;
  const knob = air.presence_knob || t.target || "sensible";
  const used = Number(air.proactive_used ?? t.proactive_used ?? 0);
  const cap = Number(air.proactive_cap ?? t.proactive_cap ?? 0);
  const quota =
    Number.isFinite(cap) && cap > 0 ? ` · 主动 ${used}/${cap}` : "";
  els.knobTarget.textContent = `目标：${PRESENCE_LABEL[knob] || knob}${quota}`;
}

function renderDecisions(air) {
  const rows = (air.decisions || air.why_silent || []).slice(0, 5);
  if (!rows.length) {
    els.decisionList.innerHTML = "";
    els.decisionEmpty.classList.remove("hidden");
    return;
  }
  els.decisionEmpty.classList.add("hidden");
  els.decisionList.innerHTML = rows
    .map((item) => {
      const reason = item.reason_zh || item.reason_code || "安静旁听";
      const meta = [formatTs(item.ts), item.reason_code ? String(item.reason_code) : ""]
        .filter(Boolean)
        .join(" · ");
      return `<li><strong>${escapeHtml(reason)}</strong>${
        meta ? `<span class="meta">${escapeHtml(meta)}</span>` : ""
      }</li>`;
    })
    .join("");
}

async function refresh() {
  try {
    const params = selectedUmo ? { umo: selectedUmo } : {};
    const air = await apiGet("read_air", params);
    online = true;
    setLink(els, true, "已连接");
    fillSessionSelect(els.sessionSelect, air.sessions || [], selectedUmo);
    renderPartner(air);
    renderOccasion(air);
    renderThermo(air);
    renderDecisions(air);
    setTonightEnabled(true);
  } catch (err) {
    online = false;
    setLink(els, false, (err && err.message) || "离线");
    setTonightEnabled(false);
    els.oneLiner.textContent = "读空气暂时不可用，请稍后刷新。";
    els.decisionEmpty.classList.remove("hidden");
    els.decisionList.innerHTML = "";
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

async function muteTonight() {
  if (!online || busy || !selectedUmo) {
    els.tonightNote.textContent = "请先选择具体群会话，再静音记忆。";
    return;
  }
  busy = true;
  els.tonightNote.textContent = "正在设置今晚别提…";
  try {
    await apiPost("notebook", { action: "mute_tonight", umo: selectedUmo, hours: 10 });
    els.tonightNote.textContent = "今晚先不提记忆小本内容。";
  } catch (err) {
    els.tonightNote.textContent = (err && err.message) || "设置失败";
  } finally {
    busy = false;
  }
}

async function boot() {
  renderNav("today");
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
  els.btnRefresh.addEventListener("click", () => void refresh());
  els.btnGhostTonight.addEventListener("click", () => void savePresence("ghost"));
  els.btnSensibleTonight.addEventListener("click", () => void savePresence("sensible"));
  els.btnLivelyTonight.addEventListener("click", () => void savePresence("lively"));
  els.btnMuteTonight.addEventListener("click", () => void muteTonight());
  await refresh();
}

boot();
