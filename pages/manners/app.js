import {
  apiGet,
  apiPost,
  escapeHtml,
  PRESENCE_LABEL,
  PRESENCE_PREVIEW,
  readyBridge,
  setLink,
} from "./api.js";
import { renderNav } from "./shell.js";

const PRESENCE_ORDER = ["ghost", "sensible", "lively"];

const SOCIAL_CHIPS = [
  { key: "relay_baton_enabled", label: "接力棒", blurb: "别人聊得热时少插话" },
  { key: "private_field_enabled", label: "私场", blurb: "两人互回时先旁听" },
  { key: "hyped_quota_enabled", label: "整活配额", blurb: "起哄别叠罗汉" },
  { key: "gap_fill_proactive_enabled", label: "缺口补全主动", blurb: "悬空问/约定缺口才主动" },
  {
    key: "cold_memory_nudge_enabled",
    label: "冷场公共记忆轻唤",
    blurb: "默认开，仅活跃档生效；无小本不编群史",
  },
  { key: "newcomer_caution_enabled", label: "新人更收", blurb: "新/低频提高门槛，不点名" },
  { key: "daily_rhythm_enabled", label: "今日作息", blurb: "氛围作息：收束≠已睡" },
  { key: "rhythm_morning_hi_enabled", label: "醒来早安", blurb: "隐身/决策/冲突不主动" },
  { key: "rhythm_allow_self_sleep", label: "允许自己睡", blurb: "冷场收束后可入睡" },
  { key: "rhythm_allow_wake", label: "允许被吵醒", blurb: "@/求助/命令可短醒" },
  { key: "rhythm_insomnia_enabled", label: "允许睡不着", blurb: "默认关，至多一句" },
  { key: "rhythm_force_sleep", label: "强制入睡", blurb: "环境主动关闭" },
  { key: "rhythm_skip_morning_hi_tonight", label: "今晚别早安", blurb: "跳过醒来问好" },
];

const MEDIA_CHIPS = [
  { key: "media_image_gate_enabled", label: "图片门闩", blurb: "不确定图文先听" },
  { key: "media_voice_gate_enabled", label: "语音门闩", blurb: "不确定语音先听" },
  {
    key: "media_understand_reply_enabled",
    label: "理解接话 L2",
    blurb: "多模态理解后接话（耗额度）",
    warn: true,
  },
];

const els = {
  linkLamp: document.getElementById("linkLamp"),
  linkLabel: document.getElementById("linkLabel"),
  presenceRange: document.getElementById("presenceRange"),
  presencePreview: document.getElementById("presencePreview"),
  presenceNote: document.getElementById("presenceNote"),
  btnSavePresence: document.getElementById("btnSavePresence"),
  socialChips: document.getElementById("socialChips"),
  mediaChips: document.getElementById("mediaChips"),
  chipNote: document.getElementById("chipNote"),
  dangerNote: document.getElementById("dangerNote"),
  btnRefresh: document.getElementById("btnRefresh"),
  btnClearMemory: document.getElementById("btnClearMemory"),
  btnResetManners: document.getElementById("btnResetManners"),
};

let stored = {};
let busy = false;
let online = false;

function currentPresence() {
  return PRESENCE_ORDER[Number(els.presenceRange.value)] || "sensible";
}

function paintPresenceMarks() {
  const value = currentPresence();
  els.presencePreview.textContent = PRESENCE_PREVIEW[value] || "";
  els.presenceRange.setAttribute("aria-valuetext", PRESENCE_LABEL[value] || value);
  document.querySelectorAll(".presence-marks span").forEach((node) => {
    node.classList.toggle("active", node.getAttribute("data-v") === value);
  });
}

function renderChips(host, defs) {
  host.innerHTML = defs
    .map((chip) => {
      const on = Boolean(stored[chip.key]);
      const warn = chip.warn ? " warn" : "";
      return `<button type="button" class="chip${warn}" data-key="${escapeHtml(chip.key)}" data-warn="${
        chip.warn ? "1" : "0"
      }" aria-pressed="${on ? "true" : "false"}" title="${escapeHtml(chip.blurb || "")}">${escapeHtml(
        chip.label
      )}</button>`;
    })
    .join("");
}

async function loadConfig() {
  try {
    const panel = await apiGet("config");
    stored = { ...(panel.stored || panel.effective || {}) };
    const knob = String(stored.presence_knob || "sensible");
    const idx = Math.max(0, PRESENCE_ORDER.indexOf(knob));
    els.presenceRange.value = String(idx);
    paintPresenceMarks();
    renderChips(els.socialChips, SOCIAL_CHIPS);
    renderChips(els.mediaChips, MEDIA_CHIPS);
    online = true;
    setLink(els, true, "已连接");
    els.btnSavePresence.disabled = false;
  } catch (err) {
    online = false;
    setLink(els, false, (err && err.message) || "离线");
    els.btnSavePresence.disabled = true;
  }
}

async function saveConfig(patch, noteEl) {
  if (!online || busy) return;
  busy = true;
  if (noteEl) noteEl.textContent = "保存中…";
  try {
    const panel = await apiPost("config", { config: patch });
    stored = { ...(panel.stored || panel.effective || stored), ...patch };
    if (noteEl) noteEl.textContent = "已保存并应用到运行时。";
    renderChips(els.socialChips, SOCIAL_CHIPS);
    renderChips(els.mediaChips, MEDIA_CHIPS);
  } catch (err) {
    if (noteEl) noteEl.textContent = (err && err.message) || "保存失败";
  } finally {
    busy = false;
  }
}

function onChipClick(event) {
  const btn = event.target.closest("button.chip");
  if (!btn || busy || !online) return;
  const key = btn.getAttribute("data-key");
  if (!key) return;
  const next = btn.getAttribute("aria-pressed") !== "true";
  if (btn.getAttribute("data-warn") === "1" && next) {
    const ok = window.confirm(
      "开启「理解接话 L2」会在相关媒体上尝试多模态理解并可能接话，消耗模型额度。确定开启？"
    );
    if (!ok) return;
  }
  void saveConfig({ [key]: next }, els.chipNote);
}

async function boot() {
  renderNav("manners");
  try {
    await readyBridge();
  } catch {
    /* ignore */
  }
  els.presenceRange.addEventListener("input", paintPresenceMarks);
  els.btnSavePresence.addEventListener("click", () =>
    void saveConfig({ presence_knob: currentPresence() }, els.presenceNote)
  );
  els.socialChips.addEventListener("click", onChipClick);
  els.mediaChips.addEventListener("click", onChipClick);
  els.btnRefresh.addEventListener("click", () => void loadConfig());
  els.btnClearMemory.addEventListener("click", () => {
    window.confirm("后端尚未提供一键清空；请到记忆页逐条忘掉。");
  });
  els.btnResetManners.addEventListener("click", () => {
    window.confirm("后端尚未提供一键重置分寸；请手动把芯片调回默认。");
  });
  await loadConfig();
}

boot();
