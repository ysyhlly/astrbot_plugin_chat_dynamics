import {
  apiGet,
  apiPost,
  readyBridge,
  friendlyError,
  escapeHtml,
  setLink,
  storageSet,
  withTimeout,
} from "./api.js";
import { mountPluginSideNav, navigateToPluginPage } from "./plugin_nav.js";
import { msg, tr, setText, apply } from "../shared/series-ui.js";

const DISMISSED_KEY = "chat_dynamics_setup_dismissed";

const FALLBACK_DEFAULTS = {
  enable: true,
  takeover_all: false,
  takeover_groups: [],
  exclude_groups: [],
  bot_names: [],
  participation_backend: "jev",
  decision_provider: "",
  decision_llm_provider: "",
  reply_provider: "",
  discussion_judgment_mode: "rerank",
  discussion_reranker_provider: "",
  discussion_llm_provider: "",
  shadow_mode: false,
  presence_knob: "sensible",
  reply_probability_threshold: 70,
  debounce_base_cooldown: 0.25,
  debounce_extended_cooldown: 1.0,
  debounce_max_cap: 4.0,
  deep_cooling_minutes: 15.0,
  private_chat_enabled: false,
  daily_rhythm_enabled: true,
  media_understand_reply_enabled: false,
  rhythm_timezone: "Asia/Shanghai",
};

const STYLE_KEYS = [
  "shadow_mode",
  "presence_knob",
  "reply_probability_threshold",
  "debounce_base_cooldown",
  "debounce_extended_cooldown",
  "debounce_max_cap",
  "deep_cooling_minutes",
];

const MANAGED_KEYS = [
  "enable",
  "takeover_all",
  "takeover_groups",
  "exclude_groups",
  "bot_names",
  "participation_backend",
  "decision_provider",
  "decision_llm_provider",
  "reply_provider",
  "discussion_judgment_mode",
  "discussion_reranker_provider",
  "discussion_llm_provider",
  ...STYLE_KEYS,
  "private_chat_enabled",
  "daily_rhythm_enabled",
  "media_understand_reply_enabled",
];

const LIST_KEYS = new Set(["takeover_groups", "exclude_groups", "bot_names"]);
const BOOL_KEYS = new Set([
  "enable",
  "takeover_all",
  "shadow_mode",
  "private_chat_enabled",
  "daily_rhythm_enabled",
  "media_understand_reply_enabled",
]);
const NUM_KEYS = new Set([
  "reply_probability_threshold",
  "debounce_base_cooldown",
  "debounce_extended_cooldown",
  "debounce_max_cap",
  "deep_cooling_minutes",
]);

const KEY_LABELS = {
  enable: "启用插件",
  takeover_all: "接管全部群聊",
  takeover_groups: "指定生效群聊",
  exclude_groups: "排除群聊名单",
  bot_names: "机器人昵称",
  participation_backend: "参与决策方式",
  decision_provider: "Jev / 抉择模型",
  decision_llm_provider: "LLM 决策模型",
  reply_provider: "回复正文模型",
  discussion_judgment_mode: "话题归属判断方式",
  discussion_reranker_provider: "话题重排序模型",
  discussion_llm_provider: "话题归属 LLM",
  shadow_mode: "观察模式（只看不发）",
  presence_knob: "分寸旋钮",
  reply_probability_threshold: "普通群聊开口门槛",
  debounce_base_cooldown: "基础防抖秒数",
  debounce_extended_cooldown: "续说防抖秒数",
  debounce_max_cap: "防抖上限秒数",
  deep_cooling_minutes: "深度冷却分钟",
  private_chat_enabled: "接管私聊",
  daily_rhythm_enabled: "今日作息早晚安",
  media_understand_reply_enabled: "媒体理解",
  rhythm_timezone: "作息时区",
};

const STYLE_PRESETS = [
  {
    id: "observe",
    label: "先观察试跑",
    badge: "只看不发",
    desc: "开启观察模式，只记录判断结果、不发送实际回复；适合首次接管群聊先确认效果。",
    chips: ["观察模式开", "分寸：懂事", "门槛 70%", "冷却 15 分"],
    values: {
      shadow_mode: true,
      presence_knob: "sensible",
      reply_probability_threshold: 70,
      debounce_base_cooldown: 0.25,
      debounce_extended_cooldown: 1.0,
      debounce_max_cap: 4.0,
      deep_cooling_minutes: 15.0,
    },
  },
  {
    id: "quiet",
    label: "安静克制",
    badge: "少插话",
    desc: "平时多旁听、少打扰，点名或高把握时再接话，冷却间隔更长。",
    chips: ["正常发送", "分寸：隐身", "门槛 80%", "冷却 20 分"],
    values: {
      shadow_mode: false,
      presence_knob: "ghost",
      reply_probability_threshold: 80,
      debounce_base_cooldown: 0.25,
      debounce_extended_cooldown: 1.2,
      debounce_max_cap: 5.0,
      deep_cooling_minutes: 20.0,
    },
  },
  {
    id: "sensible",
    label: "懂事自然",
    badge: "推荐",
    desc: "读懂场合再接话，兼顾自然互动与社交分寸，与插件默认调校保持一致。",
    chips: ["正常发送", "分寸：懂事", "门槛 70%", "冷却 15 分"],
    values: {
      shadow_mode: false,
      presence_knob: "sensible",
      reply_probability_threshold: 70,
      debounce_base_cooldown: 0.25,
      debounce_extended_cooldown: 1.0,
      debounce_max_cap: 4.0,
      deep_cooling_minutes: 15.0,
    },
  },
  {
    id: "lively",
    label: "活跃爱聊",
    badge: "更主动",
    desc: "更积极参与群内闲聊与接梗，防抖聚合更灵敏，冷却时间更短。",
    chips: ["正常发送", "分寸：活跃", "门槛 55%", "冷却 10 分"],
    values: {
      shadow_mode: false,
      presence_knob: "lively",
      reply_probability_threshold: 55,
      debounce_base_cooldown: 0.15,
      debounce_extended_cooldown: 0.75,
      debounce_max_cap: 3.0,
      deep_cooling_minutes: 10.0,
    },
  },
];

const COMPANION_STATES = {
  ready: "已就绪",
  enabled: "已启用",
  partial: "兼容接入",
  missing: "未安装",
  disabled: "已停用",
  unloaded: "未加载",
  inactive: "未接入",
  not_ready: "未就绪",
  conflict: "同名冲突",
  error: "状态异常",
  loading: "等待就绪",
};

const SOURCE_LABELS = {
  platform: "平台列表",
  session: "活跃会话",
  config: "已配置",
};

const SHARED_DEFAULT_FIELDS = [
  { key: "takeover_groups", label: "生效群聊" },
  { key: "exclude_groups", label: "排除群聊" },
  { key: "bot_names", label: "机器人昵称" },
  { key: "reply_provider", label: "回复模型" },
  { key: "decision_provider", label: "决策模型" },
  { key: "rhythm_timezone", label: "作息时区" },
];

const els = {
  linkLamp: document.getElementById("linkLamp"),
  linkLabel: document.getElementById("linkLabel"),
  btnDismissSetup: document.getElementById("btnDismissSetup"),
  setupReentryBanner: document.getElementById("setupReentryBanner"),
  setupStatus: document.getElementById("setupStatus"),
  setupStepper: document.getElementById("setupStepper"),
  stepBtns: [1, 2, 3, 4].map((n) => document.getElementById(`stepBtn${n}`)),
  stepMetas: [1, 2, 3, 4].map((n) => document.getElementById(`stepMeta${n}`)),
  stepPanels: [1, 2, 3, 4].map((n) => document.getElementById(`stepPanel${n}`)),
  btnPrevStep: document.getElementById("btnPrevStep"),
  btnNextStep: document.getElementById("btnNextStep"),
  footerProgress: document.getElementById("footerProgress"),
  // Step 1
  pluginDisabledAlert: document.getElementById("pluginDisabledAlert"),
  enablePluginToggle: document.getElementById("enablePluginToggle"),
  scopeCardSpecific: document.getElementById("scopeCardSpecific"),
  scopeCardAll: document.getElementById("scopeCardAll"),
  modeTakeoverSpecific: document.getElementById("modeTakeoverSpecific"),
  modeTakeoverAll: document.getElementById("modeTakeoverAll"),
  groupPickerTitle: document.getElementById("groupPickerTitle"),
  groupPickerHint: document.getElementById("groupPickerHint"),
  btnRefreshGroups: document.getElementById("btnRefreshGroups"),
  groupSearch: document.getElementById("groupSearch"),
  groupSelectionSummary: document.getElementById("groupSelectionSummary"),
  groupDiscoveryNote: document.getElementById("groupDiscoveryNote"),
  groupList: document.getElementById("groupList"),
  takeoverManualWrap: document.getElementById("takeoverManualWrap"),
  takeoverGroupsInput: document.getElementById("takeoverGroupsInput"),
  excludeGroupsDetails: document.getElementById("excludeGroupsDetails"),
  excludeCountBadge: document.getElementById("excludeCountBadge"),
  excludeGroupsInput: document.getElementById("excludeGroupsInput"),
  setupTemplateSelect: document.getElementById("setupTemplateSelect"),
  setupTemplatePreview: document.getElementById("setupTemplatePreview"),
  botNamesInput: document.getElementById("botNamesInput"),
  botNamesPreview: document.getElementById("botNamesPreview"),
  // Step 2
  btnRefreshProviders: document.getElementById("btnRefreshProviders"),
  modelRecommendBanner: document.getElementById("modelRecommendBanner"),
  modelRecommendTitle: document.getElementById("modelRecommendTitle"),
  modelRecommendDesc: document.getElementById("modelRecommendDesc"),
  btnApplyModelRecommend: document.getElementById("btnApplyModelRecommend"),
  backendCardJev: document.getElementById("backendCardJev"),
  backendCardLlm: document.getElementById("backendCardLlm"),
  backendJev: document.getElementById("backendJev"),
  backendLlm: document.getElementById("backendLlm"),
  jevAvailBadge: document.getElementById("jevAvailBadge"),
  llmAvailBadge: document.getElementById("llmAvailBadge"),
  decisionJevWrap: document.getElementById("decisionJevWrap"),
  decisionProviderSelect: document.getElementById("decisionProviderSelect"),
  decisionJevHint: document.getElementById("decisionJevHint"),
  decisionLlmWrap: document.getElementById("decisionLlmWrap"),
  decisionLlmProviderSelect: document.getElementById("decisionLlmProviderSelect"),
  replyProviderSelect: document.getElementById("replyProviderSelect"),
  discussionSettingsDetails: document.getElementById("discussionSettingsDetails"),
  discussionModeBadge: document.getElementById("discussionModeBadge"),
  discussionModeSelect: document.getElementById("discussionModeSelect"),
  discussionRerankerWrap: document.getElementById("discussionRerankerWrap"),
  discussionRerankerSelect: document.getElementById("discussionRerankerSelect"),
  discussionLlmWrap: document.getElementById("discussionLlmWrap"),
  discussionLlmSelect: document.getElementById("discussionLlmSelect"),
  probeIncludeDiscussion: document.getElementById("probeIncludeDiscussion"),
  btnProbeModels: document.getElementById("btnProbeModels"),
  probeNote: document.getElementById("probeNote"),
  probeResults: document.getElementById("probeResults"),
  // Step 3
  styleCards: document.getElementById("styleCards"),
  togglePrivateChat: document.getElementById("togglePrivateChat"),
  toggleDailyRhythm: document.getElementById("toggleDailyRhythm"),
  toggleMediaUnderstand: document.getElementById("toggleMediaUnderstand"),
  // Step 4
  reviewPathGrid: document.getElementById("reviewPathGrid"),
  reviewWarnings: document.getElementById("reviewWarnings"),
  reviewWarningTitle: document.getElementById("reviewWarningTitle"),
  reviewWarningCount: document.getElementById("reviewWarningCount"),
  reviewWarningList: document.getElementById("reviewWarningList"),
  reviewDiffCount: document.getElementById("reviewDiffCount"),
  reviewDiffList: document.getElementById("reviewDiffList"),
  btnSaveSetup: document.getElementById("btnSaveSetup"),
  btnGoConsole: document.getElementById("btnGoConsole"),
  btnGoConfig: document.getElementById("btnGoConfig"),
  saveNote: document.getElementById("saveNote"),
  saveSuccessBanner: document.getElementById("saveSuccessBanner"),
  setupTemplateReceipt: document.getElementById("setupTemplateReceipt"),
  btnActivateCompanions: document.getElementById("btnActivateCompanions"),
  btnRefreshCompanions: document.getElementById("btnRefreshCompanions"),
  sharedDefaultsBox: document.getElementById("sharedDefaultsBox"),
  companionNote: document.getElementById("companionNote"),
  companionCards: document.getElementById("companionCards"),
};

const state = {
  step: 1,
  busy: false,
  probing: false,
  dismissing: false,
  hasEffectiveScope: false,
  showKeepCustom: false,
  selectedStyle: "sensible",
  configState: { schema: {}, stored: {}, effective: {} },
  initialDraft: {},
  draft: {},
  groups: [],
  groupPlatforms: [],
  groupsLoaded: false,
  groupsError: "",
  providers: {
    chat: [],
    decision: [],
    rerank: [],
    routerReady: false,
    loaded: false,
  },
  recommendation: null,
  seriesPresence: null,
  integrations: null,
  templates: [],
  templatePlugins: [],
  templatesLoaded: false,
  selectedTemplateId: "",
};

function parseList(raw) {
  const items = Array.isArray(raw)
    ? raw
    : String(raw ?? "").split(/[\n,，、;；]+/);
  const seen = new Set();
  const out = [];
  for (const item of items) {
    const clean = String(item ?? "").trim();
    if (!clean || seen.has(clean)) continue;
    seen.add(clean);
    out.push(clean);
  }
  return out;
}

function normalizeValue(key, raw) {
  if (LIST_KEYS.has(key)) return parseList(raw);
  if (BOOL_KEYS.has(key)) return Boolean(raw);
  if (NUM_KEYS.has(key)) {
    const n = Number(raw);
    return Number.isFinite(n) ? n : FALLBACK_DEFAULTS[key];
  }
  return String(raw ?? "").trim();
}

function valuesEqual(key, a, b) {
  if (LIST_KEYS.has(key)) {
    const la = parseList(a);
    const lb = parseList(b);
    return la.length === lb.length && la.every((v, i) => v === lb[i]);
  }
  if (NUM_KEYS.has(key)) {
    return Math.abs(Number(a) - Number(b)) < 1e-6;
  }
  if (BOOL_KEYS.has(key)) {
    return Boolean(a) === Boolean(b);
  }
  return String(a ?? "").trim() === String(b ?? "").trim();
}

function readEffectiveValue(panel, key) {
  const eff = panel?.effective || {};
  const stored = panel?.stored || panel?.values || {};
  const schemaDef = panel?.schema?.[key]?.default;
  const raw =
    eff[key] !== undefined
      ? eff[key]
      : stored[key] !== undefined
        ? stored[key]
        : schemaDef !== undefined
          ? schemaDef
          : FALLBACK_DEFAULTS[key];
  return normalizeValue(key, raw);
}

function detectStylePreset(values) {
  for (const preset of STYLE_PRESETS) {
    const allMatch = STYLE_KEYS.every((key) =>
      valuesEqual(key, values[key], preset.values[key])
    );
    if (allMatch) return preset.id;
  }
  return null;
}

function buildInitialDraft(panel) {
  const out = {};
  for (const key of MANAGED_KEYS) {
    out[key] = readEffectiveValue(panel, key);
  }
  out.rhythm_timezone = readEffectiveValue(panel, "rhythm_timezone");
  return out;
}

function cloneDraft(src) {
  const copy = {};
  for (const [k, v] of Object.entries(src)) {
    copy[k] = Array.isArray(v) ? [...v] : v;
  }
  return copy;
}

function computeDiff() {
  const changes = [];
  for (const key of MANAGED_KEYS) {
    if (state.selectedStyle === "keep_custom" && STYLE_KEYS.includes(key)) {
      continue;
    }
    const before = state.initialDraft[key];
    const after = state.draft[key];
    if (!valuesEqual(key, before, after)) {
      changes.push({ key, before, after });
    }
  }
  return changes;
}

function formatDiffValue(key, value) {
  if (LIST_KEYS.has(key)) {
    const arr = parseList(value);
    if (!arr.length) return "空";
    if (arr.length <= 4) return arr.join("、");
    return `${arr.slice(0, 3).join("、")} 等 ${arr.length} 项`;
  }
  if (BOOL_KEYS.has(key)) return value ? "开启" : "关闭";
  if (key === "participation_backend") return value === "llm" ? "普通 LLM" : "Jev 原生抉择";
  if (key === "discussion_judgment_mode") return value === "llm" ? "LLM 判断" : "规则＋重排序";
  if (key === "presence_knob") {
    return { ghost: "隐身", sensible: "懂事", lively: "活跃" }[value] || String(value);
  }
  if (key === "reply_probability_threshold") return `${value}%`;
  if (key === "deep_cooling_minutes") return `${value} 分钟`;
  if (NUM_KEYS.has(key)) return `${value} 秒`;
  const text = String(value ?? "").trim();
  if (!text) {
    if (key === "reply_provider") return "AstrBot 默认";
    if (key === "decision_llm_provider") return "跟随回复 / 默认";
    return "未选择";
  }
  return text;
}

function setStatus(node, text, isError = false) {
  if (!node) return;
  node.textContent = text || "";
  node.classList.toggle("error", Boolean(isError));
}

function setStep(stepNum, { focusPanel = false } = {}) {
  const clamped = Math.max(1, Math.min(4, Number(stepNum) || 1));
  state.step = clamped;
  els.stepBtns.forEach((btn, idx) => {
    const active = idx + 1 === clamped;
    if (active) btn.setAttribute("aria-current", "step");
    else btn.removeAttribute("aria-current");
  });
  els.stepPanels.forEach((panel, idx) => {
    panel.hidden = idx + 1 !== clamped;
  });
  els.btnPrevStep.disabled = clamped === 1;
  els.btnNextStep.hidden = clamped === 4;
  els.footerProgress.textContent = `第 ${clamped} / 4 步`;
  if (clamped === 4) {
    renderReview();
    renderSharedDefaults();
  }
  if (focusPanel) {
    const target = els.stepPanels[clamped - 1];
    target?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }
}

/* ---------- Step 1: Scope, Discovered Groups & Bot Names ---------- */

function syncStep1InputsFromDraft() {
  const isAll = Boolean(state.draft.takeover_all);
  els.modeTakeoverSpecific.checked = !isAll;
  els.modeTakeoverAll.checked = isAll;
  els.scopeCardSpecific.classList.toggle("is-selected", !isAll);
  els.scopeCardAll.classList.toggle("is-selected", isAll);

  els.pluginDisabledAlert.classList.toggle(
    "hidden",
    Boolean(state.initialDraft.enable) && Boolean(state.draft.enable)
  );
  els.enablePluginToggle.checked = Boolean(state.draft.enable);

  setText(
    els.groupPickerTitle,
    msg(isAll ? "勾选需要排除的群聊（可选）" : "选择生效群聊")
  );
  setText(
    els.groupPickerHint,
    msg(
      isAll
        ? "已选择全部群聊；下方勾选的群将加入排除名单（始终不接管）。"
        : "自动汇集已连接平台群列表、近期活跃会话与已配置群号，支持多选。"
    )
  );

  els.takeoverManualWrap.classList.toggle("hidden", isAll);
  if (isAll || state.draft.exclude_groups.length > 0) {
    els.excludeGroupsDetails.open = true;
  }

  if (document.activeElement !== els.takeoverGroupsInput) {
    els.takeoverGroupsInput.value = state.draft.takeover_groups.join("\n");
  }
  if (document.activeElement !== els.excludeGroupsInput) {
    els.excludeGroupsInput.value = state.draft.exclude_groups.join("\n");
  }
  if (document.activeElement !== els.botNamesInput) {
    els.botNamesInput.value = state.draft.bot_names.join(", ");
  }
  els.excludeCountBadge.textContent = String(state.draft.exclude_groups.length);
  renderBotNamesPreview();
  renderGroupList();
}

function renderBotNamesPreview() {
  const names = state.draft.bot_names || [];
  els.botNamesPreview.replaceChildren();
  for (const name of names) {
    const chip = document.createElement("span");
    chip.className = "name-chip";
    chip.textContent = name;
    els.botNamesPreview.append(chip);
  }
}

function renderGroupList() {
  const isAll = Boolean(state.draft.takeover_all);
  const takeoverSet = new Set(state.draft.takeover_groups || []);
  const excludeSet = new Set(state.draft.exclude_groups || []);

  els.groupSelectionSummary.textContent = isAll
    ? `全部群聊（排除 ${excludeSet.size} 个）`
    : `已选 ${takeoverSet.size} 个群聊`;

  const query = String(els.groupSearch.value || "").trim().toLowerCase();
  const rows = state.groups.filter((g) => {
    if (!query) return true;
    const hay = `${g.group_name || ""} ${g.group_id || ""} ${g.platform_type || ""} ${g.platform_id || ""}`.toLowerCase();
    return hay.includes(query);
  });

  els.groupList.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = state.groups.length
      ? "没有匹配的群聊，请缩短搜索关键词，或在下方输入框手动粘贴群号。"
      : "未自动发现群聊列表（部分平台不支持拉取群列表）。你可以直接在下方输入框手动粘贴群号（支持逗号或换行批量粘贴）。";
    els.groupList.append(empty);
    return;
  }

  for (const row of rows) {
    const gid = String(row.group_id || "").trim();
    if (!gid) continue;
    const checked = isAll ? excludeSet.has(gid) : takeoverSet.has(gid);
    const excluded = excludeSet.has(gid);

    const item = document.createElement("div");
    item.className = "group-row";
    if (!isAll && checked) item.classList.add("is-checked");
    if (excluded) item.classList.add("is-excluded");
    item.dataset.groupId = gid;

    const label = document.createElement("label");
    label.className = "group-check-label";

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = checked;
    checkbox.dataset.groupCheck = gid;
    checkbox.setAttribute(
      "aria-label",
      isAll
        ? `排除群聊 ${row.group_name || gid}`
        : `选择群聊 ${row.group_name || gid}`
    );
    checkbox.addEventListener("change", () => {
      if (state.draft.takeover_all) {
        toggleGroupInList("exclude_groups", gid, checkbox.checked);
      } else {
        toggleGroupInList("takeover_groups", gid, checkbox.checked);
      }
    });

    const info = document.createElement("div");
    info.className = "group-info";

    const titleLine = document.createElement("div");
    titleLine.className = "group-title-line";

    const nameEl = document.createElement("span");
    nameEl.className = "group-name";
    nameEl.textContent = row.group_name ? String(row.group_name) : `群 ${gid}`;

    const idEl = document.createElement("code");
    idEl.className = "group-id-code";
    idEl.textContent = gid;

    titleLine.append(nameEl, idEl);

    const metaLine = document.createElement("div");
    metaLine.className = "group-meta-line";

    if (row.platform_type || row.platform_id) {
      const pBadge = document.createElement("span");
      pBadge.className = "platform-badge";
      pBadge.textContent = [row.platform_type, row.platform_id]
        .filter(Boolean)
        .join(" · ");
      metaLine.append(pBadge);
    }

    if (Number.isFinite(Number(row.member_count)) && Number(row.member_count) > 0) {
      const mSpan = document.createElement("span");
      mSpan.textContent = `${row.member_count} 人`;
      metaLine.append(mSpan);
    }

    const srcs = Array.isArray(row.sources)
      ? row.sources.map((s) => SOURCE_LABELS[s] || s).filter(Boolean)
      : [];
    if (srcs.length) {
      const sSpan = document.createElement("span");
      sSpan.textContent = srcs.join(" / ");
      metaLine.append(sSpan);
    }

    info.append(titleLine, metaLine);
    label.append(checkbox, info);
    item.append(label);

    if (!isAll) {
      const exBtn = document.createElement("button");
      exBtn.type = "button";
      exBtn.className = "button button-quiet group-exclude-btn";
      exBtn.setAttribute("aria-pressed", String(excluded));
      exBtn.textContent = excluded ? "已排除" : "排除";
      exBtn.addEventListener("click", () => {
        toggleGroupInList("exclude_groups", gid, !excludeSet.has(gid));
      });
      item.append(exBtn);
    }

    els.groupList.append(item);
  }
}

function toggleGroupInList(key, gid, include) {
  const next = parseList(state.draft[key]);
  const exists = next.includes(gid);
  if (include && !exists) next.push(gid);
  if (!include && exists) next.splice(next.indexOf(gid), 1);
  state.draft[key] = next;
  syncStep1InputsFromDraft();
  updateStepperSummary();
  if (state.step === 4) renderReview();
}

async function loadSetupGroups({ manual = false } = {}) {
  els.btnRefreshGroups.disabled = true;
  setStatus(els.groupDiscoveryNote, "正在读取群列表…");
  try {
    const data = await apiGet("setup/groups", {}, { timeoutMs: 8000 });
    state.groups = Array.isArray(data?.groups) ? data.groups : [];
    state.groupPlatforms = Array.isArray(data?.platforms) ? data.platforms : [];
    state.groupsLoaded = true;
    state.groupsError = "";

    const discovered = Number(data?.discovered_count || 0);
    const sessions = Number(data?.session_count || 0);
    const unsupported = state.groupPlatforms.filter((p) => p.status === "unsupported").length;
    const failed = state.groupPlatforms.filter(
      (p) => p.status === "timeout" || p.status === "error"
    ).length;

    const parts = [`共找到 ${state.groups.length} 个群聊`];
    if (discovered > 0) parts.push(`平台拉取 ${discovered} 个`);
    if (sessions > 0) parts.push(`活跃会话 ${sessions} 个`);
    if (unsupported > 0) parts.push(`${unsupported} 个平台不支持自动拉取列表，可手动粘贴群号`);
    if (failed > 0 || data?.partial_failure) parts.push("部分平台拉取超时或失败，可点刷新重试");
    setStatus(els.groupDiscoveryNote, parts.join(" · "));
  } catch (err) {
    state.groupsLoaded = false;
    state.groupsError = friendlyError(err, "群列表暂时不可用，可直接手动粘贴群号。");
    setStatus(
      els.groupDiscoveryNote,
      manual
        ? state.groupsError
        : "暂未读取到自动群列表，可在下方直接粘贴群号或稍后点刷新重试。",
      manual
    );
  } finally {
    els.btnRefreshGroups.disabled = false;
    renderGroupList();
  }
}

/* ---------- Step 1b: Series Templates for New Group Onboarding ---------- */

function templatePluginLabel(pluginId) {
  const found = (state.templatePlugins || []).find((p) => p && p.id === pluginId);
  return found?.label || pluginId;
}

function renderTemplateSelector() {
  if (!els.setupTemplateSelect) return;
  const current = String(state.selectedTemplateId || "");
  const builtins = state.templates.filter((t) => t && (t.builtin || t.readonly));
  const customs = state.templates.filter((t) => t && !t.builtin && !t.readonly);

  const parts = [
    `<option value="">${escapeHtml(tr("暂不下发模板（仅保存主控配置）"))}</option>`,
  ];
  if (builtins.length) {
    parts.push(
      `<optgroup label="${escapeHtml(tr("内置模板"))}">${builtins
        .map(
          (t) =>
            `<option value="${escapeHtml(t.id)}"${t.id === current ? " selected" : ""}>${escapeHtml(t.name || t.id)}</option>`
        )
        .join("")}</optgroup>`
    );
  }
  if (customs.length) {
    parts.push(
      `<optgroup label="${escapeHtml(tr("自定义模板"))}">${customs
        .map(
          (t) =>
            `<option value="${escapeHtml(t.id)}"${t.id === current ? " selected" : ""}>${escapeHtml(t.name || t.id)}</option>`
        )
        .join("")}</optgroup>`
    );
  }
  els.setupTemplateSelect.innerHTML = parts.join("");
  if (current && state.templates.some((t) => t.id === current)) {
    els.setupTemplateSelect.value = current;
  } else {
    state.selectedTemplateId = "";
    els.setupTemplateSelect.value = "";
  }
  renderTemplatePreview();
}

function renderTemplatePreview() {
  if (!els.setupTemplatePreview) return;
  const tpl = state.templates.find((t) => t && t.id === state.selectedTemplateId);
  if (!tpl) {
    els.setupTemplatePreview.classList.add("hidden");
    els.setupTemplatePreview.replaceChildren();
    return;
  }
  const isBuiltin = Boolean(tpl.builtin || tpl.readonly);
  const states = tpl.states && typeof tpl.states === "object" ? tpl.states : {};
  const orderedPlugins =
    state.templatePlugins.length > 0
      ? state.templatePlugins
      : Object.keys(states).map((id) => ({ id, label: id, mark: "" }));

  const chips = orderedPlugins
    .map((p) => {
      const on = Boolean(states[p.id]);
      return `<span class="tpl-preview-chip ${on ? "is-on" : "is-off"}">${on ? "●" : "○"} ${escapeHtml(p.label || p.id)}</span>`;
    })
    .join("");

  els.setupTemplatePreview.classList.remove("hidden");
  els.setupTemplatePreview.innerHTML = `<div class="tpl-preview-head">
      <strong>${escapeHtml(tpl.name || tpl.id)}</strong>
      <span class="pill-tag ${isBuiltin ? "pill-muted" : ""}">${escapeHtml(isBuiltin ? tr("内置 · 只读") : tr("自定义"))}</span>
    </div>
    ${tpl.description ? `<p class="tpl-preview-desc">${escapeHtml(tpl.description)}</p>` : ""}
    ${tpl.strategy_hint ? `<p class="tpl-preview-hint">${escapeHtml(tpl.strategy_hint)}</p>` : ""}
    <div class="tpl-preview-chips">${chips}</div>`;
}

async function loadSetupTemplates() {
  try {
    const data = await apiGet("series_templates", {}, { timeoutMs: 8000 });
    if (data && typeof data === "object") {
      state.templates = Array.isArray(data.templates) ? data.templates : [];
      state.templatePlugins = Array.isArray(data.plugins) ? data.plugins : [];
      state.templatesLoaded = true;
    }
  } catch {
    state.templates = [];
    state.templatesLoaded = false;
  }
  renderTemplateSelector();
}

function resolveTemplateTargetUmos({ prevTakeover, nextTakeover, isAll, excludeGroups }) {
  const excludeSet = new Set(parseList(excludeGroups));
  const byGroupId = new Map();
  for (const row of state.groups || []) {
    const gid = String(row?.group_id || "").trim();
    const umo = String(row?.umo || "").trim();
    if (gid && umo) byGroupId.set(gid, umo);
  }
  let rawTargets = [];
  if (isAll) {
    rawTargets = (state.groups || [])
      .map((r) => String(r?.group_id || "").trim())
      .filter((gid) => gid && !excludeSet.has(gid));
  } else {
    const prevSet = new Set(parseList(prevTakeover));
    const added = parseList(nextTakeover).filter((gid) => !prevSet.has(gid) && !excludeSet.has(gid));
    rawTargets = added.length
      ? added
      : parseList(nextTakeover).filter((gid) => !excludeSet.has(gid));
  }
  const seen = new Set();
  const out = [];
  for (const gid of rawTargets) {
    const umo = byGroupId.get(gid) || gid;
    if (!umo || seen.has(umo)) continue;
    seen.add(umo);
    const matched = (state.groups || []).find(
      (r) => String(r?.group_id || "") === gid || String(r?.umo || "") === umo
    );
    out.push({
      gid,
      umo,
      label: matched?.group_name ? `${matched.group_name} (${gid})` : gid,
    });
  }
  return out;
}

function renderTemplateReceipt({ tplName, results, emptyReason = "" }) {
  if (!els.setupTemplateReceipt) return;
  if (emptyReason) {
    els.setupTemplateReceipt.classList.remove("hidden");
    els.setupTemplateReceipt.classList.remove("has-error");
    els.setupTemplateReceipt.innerHTML = `<strong>${escapeHtml(tr("系列模板回执"))}</strong><p>${escapeHtml(emptyReason)}</p>`;
    return;
  }
  let totalApplied = 0;
  let totalSkipped = 0;
  let totalFailed = 0;
  for (const item of results) {
    totalApplied += item.applied.length;
    totalSkipped += item.skipped.length;
    totalFailed += item.failed.length + (item.error ? 1 : 0);
  }
  const headline = tr(
    "模板「{0}」已下发至 {1} 个群：已应用 {2} / 跳过 {3} / 失败 {4}",
    tplName,
    results.length,
    totalApplied,
    totalSkipped,
    totalFailed
  );
  const rowsHtml = results
    .map((item) => {
      if (item.error) {
        return `<div class="receipt-group-row is-error">
          <div class="receipt-group-head"><strong>${escapeHtml(item.label)}</strong><code>${escapeHtml(item.umo)}</code></div>
          <p class="receipt-error-text">${escapeHtml(item.error)}</p>
        </div>`;
      }
      const pills = [
        ...item.applied.map(
          (p) =>
            `<span class="receipt-pill is-applied">✓ ${escapeHtml(templatePluginLabel(p.plugin_id))} · ${escapeHtml(p.enabled ? tr("开启") : tr("暂停"))}</span>`
        ),
        ...item.skipped.map(
          (p) =>
            `<span class="receipt-pill is-skipped" title="${escapeHtml(p.message || p.reason || "")}">↷ ${escapeHtml(templatePluginLabel(p.plugin_id))}${p.message || p.reason ? ` (${escapeHtml(p.message || p.reason)})` : ""}</span>`
        ),
        ...item.failed.map(
          (p) =>
            `<span class="receipt-pill is-failed" title="${escapeHtml(p.message || p.reason || "")}">✕ ${escapeHtml(templatePluginLabel(p.plugin_id))}${p.message || p.reason ? ` (${escapeHtml(p.message || p.reason)})` : ""}</span>`
        ),
      ].join("");
      return `<div class="receipt-group-row">
        <div class="receipt-group-head">
          <strong>${escapeHtml(item.label)}</strong>
          <span class="receipt-group-counts">${escapeHtml(tr("已应用 {0} / 跳过 {1} / 失败 {2}", item.applied.length, item.skipped.length, item.failed.length))}</span>
        </div>
        <div class="receipt-pills">${pills}</div>
      </div>`;
    })
    .join("");

  els.setupTemplateReceipt.classList.remove("hidden");
  els.setupTemplateReceipt.classList.toggle("has-error", totalFailed > 0);
  els.setupTemplateReceipt.innerHTML = `<div class="receipt-head"><strong>${escapeHtml(headline)}</strong></div><div class="receipt-groups">${rowsHtml}</div>`;
}

/* ---------- Step 2: Models, Smart Recommendation & Probe ---------- */

function nonDecisionChatProviders() {
  return state.providers.chat.filter((row) => !row.systemone && !row.decision);
}

function populateSelect(selectEl, items, currentValue, { blankLabel, missingSuffix = "（当前已保存，未在启用列表）" }) {
  if (!selectEl) return;
  const current = String(currentValue || "").trim();
  const options = [];
  if (blankLabel !== null && blankLabel !== undefined) {
    options.push(`<option value="">${escapeHtml(blankLabel)}</option>`);
  }
  let found = !current;
  for (const item of items) {
    const id = String(item.id || "").trim();
    if (!id) continue;
    if (id === current) found = true;
    const label = item.model && item.model !== id ? `${id} · ${item.model}` : id;
    options.push(
      `<option value="${escapeHtml(id)}"${id === current ? " selected" : ""}>${escapeHtml(label)}</option>`
    );
  }
  if (current && !found) {
    options.push(
      `<option value="${escapeHtml(current)}" selected>${escapeHtml(current + missingSuffix)}</option>`
    );
  }
  selectEl.innerHTML = options.join("");
  selectEl.value = current;
}

function computeModelRecommendation() {
  if (!state.providers.loaded) return null;
  if (state.providers.decision.length === 0) {
    return {
      backend: "llm",
      decisionProvider: "",
      isWarn: state.draft.participation_backend === "jev",
      title: "未检测到已启用的 Jev / 抉择模型",
      desc:
        state.draft.participation_backend === "llm"
          ? "已使用「普通 LLM 参与决策」，无需额外部署 Jev 服务；可直接选填回复模型或留空沿用 AstrBot 默认。"
          : "当前 AstrBot 没有可用的 Jev / 抉择模型，推荐切换为「普通 LLM 参与决策」，无需额外部署即可开始使用。",
      actionLabel: "一键切换为普通 LLM 决策",
      needsAction: state.draft.participation_backend !== "llm",
    };
  }
  const firstId = String(state.providers.decision[0]?.id || "");
  const isJevWithProvider =
    state.draft.participation_backend === "jev" && Boolean(state.draft.decision_provider);
  return {
    backend: "jev",
    decisionProvider: firstId,
    isWarn: false,
    title: `已检测到 ${state.providers.decision.length} 个 Jev / 抉择模型`,
    desc: isJevWithProvider
      ? `推荐使用 Jev 原生抉择（当前已选 ${state.draft.decision_provider}），响应快且开口分寸更稳。`
      : `推荐选择 Jev 原生抉择并接入 ${firstId}；如需免 Jev 运行也可保持普通 LLM。`,
    actionLabel: `采用推荐：Jev (${firstId})`,
    needsAction: !isJevWithProvider,
  };
}

function applyInitialModelRecommendation() {
  if (!state.providers.loaded) return;
  const stored = state.configState.stored || {};
  const storedBackend = String(stored.participation_backend || "").trim();
  const schemaDefaultBackend = String(
    state.configState.schema?.participation_backend?.default || "jev"
  ).trim();
  const userPickedDecision = Boolean(String(stored.decision_provider || "").trim());
  const explicitNonDefaultBackend =
    Boolean(storedBackend) && storedBackend !== schemaDefaultBackend;

  if (state.hasEffectiveScope && (explicitNonDefaultBackend || userPickedDecision)) {
    return;
  }

  if (state.providers.decision.length === 0) {
    if (!state.hasEffectiveScope && !userPickedDecision && (!storedBackend || storedBackend === "jev")) {
      state.draft.participation_backend = "llm";
    }
  } else {
    if (!state.hasEffectiveScope && !explicitNonDefaultBackend) {
      state.draft.participation_backend = "jev";
    }
    if (
      state.draft.participation_backend === "jev" &&
      !state.draft.decision_provider &&
      !userPickedDecision
    ) {
      state.draft.decision_provider = String(state.providers.decision[0]?.id || "");
    }
  }
}

function syncStep2InputsFromDraft() {
  const isLlm = state.draft.participation_backend === "llm";
  els.backendJev.checked = !isLlm;
  els.backendLlm.checked = isLlm;
  els.backendCardJev.classList.toggle("is-selected", !isLlm);
  els.backendCardLlm.classList.toggle("is-selected", isLlm);

  els.decisionJevWrap.classList.toggle("hidden", isLlm);
  els.decisionLlmWrap.classList.toggle("hidden", !isLlm);

  const decCount = state.providers.decision.length;
  const chatProviders = nonDecisionChatProviders();

  if (state.providers.loaded) {
    els.jevAvailBadge.textContent = decCount > 0 ? `${decCount} 个可用` : "暂无可用";
    els.jevAvailBadge.classList.toggle("pill-warn", decCount === 0);
    els.llmAvailBadge.textContent = `${chatProviders.length} 个聊天模型`;
  } else {
    els.jevAvailBadge.textContent = "未加载";
    els.llmAvailBadge.textContent = "无需 Jev";
  }

  populateSelect(els.decisionProviderSelect, state.providers.decision, state.draft.decision_provider, {
    blankLabel: decCount > 0 ? "请选择 Jev / 抉择模型…" : "暂无可用抉择模型（建议切到普通 LLM）",
  });
  populateSelect(els.decisionLlmProviderSelect, chatProviders, state.draft.decision_llm_provider, {
    blankLabel: "跟随回复模型 / AstrBot 默认聊天模型",
  });
  populateSelect(els.replyProviderSelect, chatProviders, state.draft.reply_provider, {
    blankLabel: "留空（使用 AstrBot 默认回复模型）",
  });

  if (state.providers.loaded && !isLlm && !state.providers.routerReady && decCount > 0) {
    els.decisionJevHint.textContent = "提示：内置决策路由尚未完全就绪，若测通失败请重载插件。";
  } else {
    els.decisionJevHint.textContent =
      "选择已在 AstrBot 启用的 System One / Jev 或 OpenAI Decisions 服务。";
  }

  // Discussion judgment collapsible
  const discMode = state.draft.discussion_judgment_mode === "llm" ? "llm" : "rerank";
  els.discussionModeSelect.value = discMode;
  els.discussionModeBadge.textContent = discMode === "llm" ? "LLM 判断" : "规则＋重排序";
  els.discussionRerankerWrap.classList.toggle("hidden", discMode === "llm");
  els.discussionLlmWrap.classList.toggle("hidden", discMode !== "llm");
  populateSelect(
    els.discussionRerankerSelect,
    state.providers.rerank,
    state.draft.discussion_reranker_provider,
    { blankLabel: "留空（仅规则或自动选择）" }
  );
  populateSelect(
    els.discussionLlmSelect,
    chatProviders,
    state.draft.discussion_llm_provider,
    { blankLabel: "留空（跟随默认聊天模型）" }
  );

  // Smart recommendation banner
  const rec = computeModelRecommendation();
  state.recommendation = rec;
  if (rec) {
    els.modelRecommendBanner.classList.remove("hidden");
    els.modelRecommendBanner.classList.toggle("is-warn", Boolean(rec.isWarn));
    els.modelRecommendTitle.textContent = rec.title;
    els.modelRecommendDesc.textContent = rec.desc;
    els.btnApplyModelRecommend.classList.toggle("hidden", !rec.needsAction);
    els.btnApplyModelRecommend.textContent = rec.actionLabel;
  } else {
    els.modelRecommendBanner.classList.add("hidden");
  }
}

async function loadProviders({ applyRecommend = false } = {}) {
  els.btnRefreshProviders.disabled = true;
  try {
    const data = await apiGet("providers", {}, { timeoutMs: 8000 });
    const chat = Array.isArray(data?.chat) ? data.chat : [];
    const decision = Array.isArray(data?.decision)
      ? data.decision
      : Array.isArray(data?.systemone)
        ? data.systemone
        : chat.filter((r) => r.systemone || r.decision);
    const rerank = Array.isArray(data?.rerank) ? data.rerank : [];
    state.providers = {
      chat,
      decision,
      rerank,
      routerReady: data?.systemone_router_ready === true,
      loaded: true,
    };
    if (applyRecommend) {
      applyInitialModelRecommendation();
    }
  } catch {
    state.providers = { ...state.providers, loaded: false };
  } finally {
    els.btnRefreshProviders.disabled = false;
    syncStep2InputsFromDraft();
    updateStepperSummary();
  }
}

function renderProbeSlot(title, slot) {
  const card = document.createElement("article");
  const status = slot?.status || "skipped";
  card.className = `probe-card ${
    status === "ok" ? "is-ok" : status === "error" ? "is-error" : ""
  }`;

  const top = document.createElement("div");
  top.className = "probe-card-top";
  const h = document.createElement("strong");
  h.textContent = title;
  const badge = document.createElement("span");
  badge.className = `pill-tag ${
    status === "ok" ? "" : status === "error" ? "pill-danger" : "pill-muted"
  }`;
  badge.textContent =
    status === "ok" ? "连接正常" : status === "error" ? "连接失败" : "已跳过";
  top.append(h, badge);

  const metaParts = [];
  if (slot?.provider_id) {
    metaParts.push(slot.using_default ? `${slot.provider_id} (AstrBot 默认)` : slot.provider_id);
  } else if (slot?.using_default) {
    metaParts.push("AstrBot 默认");
  }
  if (slot?.model && slot.model !== slot.provider_id) {
    metaParts.push(slot.model);
  }
  if (Number.isFinite(Number(slot?.latency_ms)) && Number(slot.latency_ms) >= 0) {
    metaParts.push(`${Math.round(Number(slot.latency_ms))} ms`);
  }

  const meta = document.createElement("div");
  meta.className = "probe-meta";
  meta.textContent = metaParts.join(" · ") || "—";

  const msgEl = document.createElement("p");
  msgEl.className = "probe-msg";
  msgEl.textContent = String(slot?.message || (status === "ok" ? "连通测试通过" : "未执行测试"));

  card.append(top, meta, msgEl);
  return card;
}

async function runConnectivityProbe() {
  if (state.probing) return;
  state.probing = true;
  els.btnProbeModels.disabled = true;
  els.btnProbeModels.setAttribute("aria-busy", "true");
  setStatus(els.probeNote, "正在发送轻量探针测试模型连通性（最长约 12 秒）…");
  els.probeResults.replaceChildren();

  const payload = {
    participation_backend: state.draft.participation_backend,
    decision_provider: state.draft.decision_provider,
    decision_llm_provider: state.draft.decision_llm_provider,
    reply_provider: state.draft.reply_provider,
    discussion_judgment_mode: state.draft.discussion_judgment_mode,
    discussion_reranker_provider: state.draft.discussion_reranker_provider,
    discussion_llm_provider: state.draft.discussion_llm_provider,
    probe_discussion: Boolean(els.probeIncludeDiscussion.checked),
  };

  try {
    const bridge = window.DynamicsPanelBridge || window.AstrBotPluginPage || null;
    let data;
    if (bridge && typeof bridge.apiPost === "function") {
      const raw = await withTimeout(bridge.apiPost("setup/probe", payload), 12000);
      if (!raw || typeof raw !== "object") throw new Error("空响应");
      const inner = raw.data !== undefined ? raw.data : raw;
      if (inner && typeof inner === "object" && inner.results && typeof inner.results === "object") {
        data = inner;
      } else if (raw.status === "error" || raw.ok === false) {
        throw new Error(raw.error || raw.message || "请求失败");
      } else {
        data = inner;
      }
    } else {
      data = await apiPost("setup/probe", payload, { timeoutMs: 12000 });
    }
    const results = data?.results || {};
    const slots = [
      ["决策模型", results.decision],
      ["回复模型", results.reply],
    ];
    if (results.discussion) {
      slots.push(["话题归属模型", results.discussion]);
    }
    let hasError = false;
    for (const [label, slot] of slots) {
      if (!slot) continue;
      if (slot.status === "error") hasError = true;
      els.probeResults.append(renderProbeSlot(label, slot));
    }
    setStatus(
      els.probeNote,
      hasError
        ? "部分模型连接未通过，请查看下方原因提示或切换其他模型。"
        : "所测模型连接均正常。",
      hasError
    );
  } catch (err) {
    setStatus(els.probeNote, friendlyError(err, "连通测试请求失败，请稍后重试。"), true);
  } finally {
    state.probing = false;
    els.btnProbeModels.disabled = false;
    els.btnProbeModels.removeAttribute("aria-busy");
  }
}

/* ---------- Step 3: Scenario Cards & High-Frequency Toggles ---------- */

function selectStyleCard(styleId) {
  state.selectedStyle = styleId;
  if (styleId === "keep_custom") {
    for (const key of STYLE_KEYS) {
      state.draft[key] = state.initialDraft[key];
    }
  } else {
    const preset = STYLE_PRESETS.find((p) => p.id === styleId);
    if (preset) {
      for (const key of STYLE_KEYS) {
        state.draft[key] = preset.values[key];
      }
    }
  }
  renderStyleCards();
  updateStepperSummary();
  if (state.step === 4) renderReview();
}

function renderStyleCards() {
  els.styleCards.replaceChildren();
  const cards = [];
  if (state.showKeepCustom) {
    const curShadow = state.initialDraft.shadow_mode ? "观察模式开" : "正常发送";
    const curPresence =
      { ghost: "隐身", sensible: "懂事", lively: "活跃" }[state.initialDraft.presence_knob] ||
      String(state.initialDraft.presence_knob);
    cards.push({
      id: "keep_custom",
      label: "保持当前自定义参数",
      badge: "不改动已有调校",
      desc: "保留你之前调好的观察模式、分寸、开口门槛、防抖与深度冷却参数，不被引导预设覆盖。",
      chips: [
        curShadow,
        `分寸：${curPresence}`,
        `门槛 ${state.initialDraft.reply_probability_threshold}%`,
        `冷却 ${state.initialDraft.deep_cooling_minutes} 分`,
      ],
      isKeepCustom: true,
    });
  }
  for (const preset of STYLE_PRESETS) {
    cards.push(preset);
  }

  for (const card of cards) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `style-card${card.isKeepCustom ? " is-keep-custom" : ""}`;
    btn.setAttribute("role", "radio");
    btn.setAttribute("aria-checked", String(state.selectedStyle === card.id));
    btn.dataset.styleId = card.id;

    const head = document.createElement("div");
    head.className = "style-card-head";
    const title = document.createElement("strong");
    title.textContent = card.label;
    const badge = document.createElement("span");
    badge.className = `pill-tag${card.id === "sensible" ? "" : " pill-muted"}`;
    badge.textContent = card.badge;
    head.append(title, badge);

    const desc = document.createElement("p");
    desc.className = "style-card-desc";
    desc.textContent = card.desc;

    const params = document.createElement("div");
    params.className = "style-card-params";
    for (const c of card.chips) {
      const span = document.createElement("span");
      span.className = "param-chip";
      span.textContent = c;
      params.append(span);
    }

    btn.append(head, desc, params);
    if (!card.isKeepCustom && card.values && state.initialDraft) {
      const diffs = [];
      for (const key of STYLE_KEYS) {
        const before = state.initialDraft[key];
        const after = card.values[key];
        if (!valuesEqual(key, before, after)) {
          const label = FIELD_LABELS[key] || key;
          diffs.push(`${label}: ${formatSettingValue(key, before)} → ${formatSettingValue(key, after)}`);
        }
      }
      const diffEl = document.createElement("p");
      diffEl.className = "ops-note style-card-diff";
      diffEl.textContent = diffs.length ? `变更预览：${diffs.join(" · ")}` : "与当前已保存参数一致";
      btn.append(diffEl);
    }
    btn.addEventListener("click", () => selectStyleCard(card.id));
    els.styleCards.append(btn);
  }
}

function syncStep3InputsFromDraft() {
  renderStyleCards();
  els.togglePrivateChat.checked = Boolean(state.draft.private_chat_enabled);
  els.toggleDailyRhythm.checked = Boolean(state.draft.daily_rhythm_enabled);
  els.toggleMediaUnderstand.checked = Boolean(state.draft.media_understand_reply_enabled);
}

/* ---------- Step 4: Review, Validation, Partial Save & Companions ---------- */

function evaluatePathWarnings() {
  const warnings = [];
  const d = state.draft;
  const hasGroupScope = Boolean(d.takeover_all) || parseList(d.takeover_groups).length > 0;
  const hasAnyScope = hasGroupScope || Boolean(d.private_chat_enabled);

  if (!d.enable) {
    warnings.push({
      blocking: false,
      step: 1,
      text: "插件总开关（enable）当前处于关闭状态，开启前不会处理群聊消息。",
      action: "返回第 1 步开启",
    });
  }
  if (!hasAnyScope) {
    warnings.push({
      blocking: false,
      step: 1,
      text: "尚未选择任何生效群聊，也未开启私聊接管；保存后插件不会对任何会话生效。",
      action: "返回第 1 步选群",
    });
  }
  if (d.participation_backend === "jev" && !String(d.decision_provider || "").trim()) {
    warnings.push({
      blocking: true,
      step: 2,
      text: "当前选择了 Jev 原生抉择，但尚未选择决策模型（decision_provider），保存校验无法通过。请选择一个 Jev 模型，或切换为普通 LLM 参与决策。",
      action: "返回第 2 步选择",
    });
  } else if (
    d.participation_backend === "jev" &&
    state.providers.loaded &&
    !state.providers.routerReady
  ) {
    warnings.push({
      blocking: false,
      step: 2,
      text: "内置决策路由当前尚未就绪，如保存后未响应请尝试重新加载插件。",
      action: "查看第 2 步",
    });
  }
  if (d.shadow_mode) {
    warnings.push({
      blocking: false,
      step: 3,
      text: "当前处于「先观察试跑」观察模式（shadow_mode = true），只记录判断结果，不会实际发送回复。",
      action: "前往第 3 步调整",
    });
  }
  return warnings;
}

function renderReview() {
  const d = state.draft;
  const groups = parseList(d.takeover_groups);
  const excluded = parseList(d.exclude_groups);
  const hasScope = Boolean(d.takeover_all) || groups.length > 0 || Boolean(d.private_chat_enabled);

  const scopeHeadline = !d.enable
    ? "插件已停用"
    : d.takeover_all
      ? excluded.length
        ? `全部群聊（排除 ${excluded.length} 个）`
        : "全部群聊"
      : groups.length
        ? `指定 ${groups.length} 个群聊`
        : d.private_chat_enabled
          ? "仅私聊生效"
          : "未配置生效范围";
  const selectedTpl = state.templates.find((t) => t && t.id === state.selectedTemplateId);
  const scopeDescParts = [
    d.bot_names.length > 0
      ? `昵称：${d.bot_names.slice(0, 4).join("、")}${d.bot_names.length > 4 ? "…" : ""}`
      : "未设置额外昵称（仍支持 @ 与引用触发）",
  ];
  if (selectedTpl) {
    scopeDescParts.push(`${tr("保存后下发模板")}：${selectedTpl.name || selectedTpl.id}`);
  }
  const scopeDesc = scopeDescParts.join(" · ");

  const isLlm = d.participation_backend === "llm";
  const decisionOk = isLlm || Boolean(String(d.decision_provider || "").trim());
  const decisionHeadline = isLlm ? "普通 LLM 参与决策" : "Jev / 原生抉择";
  const decisionDesc = isLlm
    ? d.decision_llm_provider
      ? `模型：${d.decision_llm_provider}`
      : d.reply_provider
        ? `跟随回复模型：${d.reply_provider}`
        : "使用 AstrBot 默认聊天模型"
    : d.decision_provider
      ? `模型：${d.decision_provider}`
      : "未选择决策模型（需先补充或切换为 LLM）";

  const styleMeta =
    state.selectedStyle === "keep_custom"
      ? "保持当前自定义参数"
      : STYLE_PRESETS.find((p) => p.id === state.selectedStyle)?.label || "自定义";
  const replyHeadline = d.reply_provider || "AstrBot 默认回复模型";
  const replyDesc = `${styleMeta} · 开口门槛 ${d.reply_probability_threshold}%${
    d.shadow_mode ? " · 观察模式" : ""
  }`;

  const toggleHeadline = [
    `私聊 ${d.private_chat_enabled ? "开" : "关"}`,
    `作息 ${d.daily_rhythm_enabled ? "开" : "关"}`,
    `媒体 ${d.media_understand_reply_enabled ? "开" : "关"}`,
  ].join(" / ");
  const toggleDesc = `话题归属：${
    d.discussion_judgment_mode === "llm" ? "LLM 判断" : "规则＋重排序"
  }`;

  const tiles = [
    {
      label: "01 生效范围",
      headline: scopeHeadline,
      desc: scopeDesc,
      cls: !hasScope || !d.enable ? "is-warn" : "",
    },
    {
      label: "02 参与决策",
      headline: decisionHeadline,
      desc: decisionDesc,
      cls: !decisionOk ? "is-error" : "",
    },
    {
      label: "03 回复与风格",
      headline: replyHeadline,
      desc: replyDesc,
      cls: d.shadow_mode ? "is-warn" : "",
    },
    {
      label: "04 高频开关",
      headline: toggleHeadline,
      desc: toggleDesc,
      cls: "",
    },
  ];

  els.reviewPathGrid.replaceChildren();
  for (const tile of tiles) {
    const art = document.createElement("article");
    art.className = `path-tile${tile.cls ? ` ${tile.cls}` : ""}`;
    const lbl = document.createElement("span");
    lbl.className = "path-label";
    lbl.textContent = tile.label;
    const h = document.createElement("strong");
    h.textContent = tile.headline;
    const p = document.createElement("p");
    p.textContent = tile.desc;
    art.append(lbl, h, p);
    els.reviewPathGrid.append(art);
  }

  // Warnings
  const warnings = evaluatePathWarnings();
  const hasBlocking = warnings.some((w) => w.blocking);
  els.reviewWarnings.classList.toggle("hidden", warnings.length === 0);
  els.reviewWarnings.classList.toggle("has-blocking", hasBlocking);
  els.reviewWarningCount.textContent = `${warnings.length} 条提示`;
  els.reviewWarningList.replaceChildren();
  for (const w of warnings) {
    const li = document.createElement("li");
    const span = document.createElement("span");
    span.textContent = w.text;
    const jump = document.createElement("button");
    jump.type = "button";
    jump.className = "button button-quiet";
    jump.textContent = w.action;
    jump.addEventListener("click", () => setStep(w.step, { focusPanel: true }));
    li.append(span, jump);
    els.reviewWarningList.append(li);
  }

  // Diff summary
  const diff = computeDiff();
  els.reviewDiffCount.textContent = `${diff.length} 项变更`;
  els.reviewDiffList.replaceChildren();
  if (!diff.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent =
      "目前没有待保存的参数改动。你可以直接点击“保存并立即应用”确认完成引导，或返回前几步调整。";
    els.reviewDiffList.append(empty);
  } else {
    for (const item of diff) {
      const row = document.createElement("div");
      row.className = "diff-item";
      row.dataset.diffKey = item.key;

      const keyBlock = document.createElement("div");
      keyBlock.className = "diff-key-block";
      const labelEl = document.createElement("strong");
      labelEl.textContent = KEY_LABELS[item.key] || item.key;
      const codeEl = document.createElement("span");
      codeEl.className = "config-key";
      codeEl.textContent = item.key;
      keyBlock.append(labelEl, codeEl);

      const vals = document.createElement("div");
      vals.className = "diff-values";
      const oldEl = document.createElement("span");
      oldEl.className = "diff-old";
      oldEl.textContent = formatDiffValue(item.key, item.before);
      const arrow = document.createElement("span");
      arrow.textContent = "→";
      arrow.setAttribute("aria-hidden", "true");
      const newEl = document.createElement("span");
      newEl.className = "diff-new";
      newEl.textContent = formatDiffValue(item.key, item.after);
      vals.append(oldEl, arrow, newEl);

      row.append(keyBlock, vals);
      els.reviewDiffList.append(row);
    }
  }
}

function renderSharedDefaults() {
  els.sharedDefaultsBox.replaceChildren();
  const seriesDefaults = state.seriesPresence?.series_defaults || {};
  for (const field of SHARED_DEFAULT_FIELDS) {
    const cell = document.createElement("div");
    cell.className = "shared-default-item";
    const strong = document.createElement("strong");
    strong.textContent = field.label;
    const code = document.createElement("span");
    code.className = "config-key";
    code.textContent = field.key;
    const valEl = document.createElement("span");
    valEl.className = "shared-default-val";
    let rawVal =
      state.draft[field.key] !== undefined
        ? state.draft[field.key]
        : seriesDefaults[field.key];
    if (field.key === "rhythm_timezone" && !String(rawVal || "").trim()) {
      rawVal = String(seriesDefaults.rhythm_timezone || "").trim() || "Asia/Shanghai";
    }
    valEl.textContent = formatDiffValue(field.key, rawVal);
    cell.append(strong, code, valEl);
    els.sharedDefaultsBox.append(cell);
  }
}

function renderCompanions() {
  renderSharedDefaults();
  els.companionCards.replaceChildren();

  const presencePlugins = Array.isArray(state.seriesPresence?.plugins)
    ? state.seriesPresence.plugins
    : [];
  const integrationRows = Array.isArray(state.integrations?.companions)
    ? state.integrations.companions
    : [];

  const merged = [];
  const byId = new Map();

  for (const p of presencePlugins) {
    if (!p || !p.id || p.current || p.id === "astrbot_plugin_chat_dynamics") continue;
    const entry = {
      id: String(p.id),
      label: String(p.label || p.id),
      role: String(p.role || ""),
      state: String(p.state || "missing"),
      version: String(p.version || ""),
      canOpen: Boolean(p.can_open && p.page),
      settingsNote: "",
    };
    byId.set(entry.id, entry);
    merged.push(entry);
  }

  for (const row of integrationRows) {
    if (!row || !row.id) continue;
    const id = String(row.id);
    const existing = byId.get(id);
    if (existing) {
      if (!existing.role && row.role) existing.role = String(row.role);
      if (!existing.version && row.version) existing.version = String(row.version);
      if (row.settings) existing.settingsNote = String(row.settings);
    } else if (id !== "astrbot_plugin_chat_dynamics") {
      merged.push({
        id,
        label: String(row.name || id),
        role: String(row.role || ""),
        state: String(row.state || "missing"),
        version: String(row.version || ""),
        canOpen: false,
        settingsNote: String(row.settings || ""),
      });
    }
  }

  if (!merged.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent =
      "当前仅检测到对话主控插件。后续如安装群间世界、群间记忆、群间学习、视觉工坊等系列伙伴，会自动继承上方共享默认项。";
    els.companionCards.append(empty);
    setStatus(els.companionNote, "未检测到其他群间系列伙伴插件（可选安装，不影响基础聊天）。");
    return;
  }

  const readyCount = merged.filter(
    (c) => c.state === "ready" || c.state === "enabled"
  ).length;
  setStatus(
    els.companionNote,
    `已发现 ${merged.length} 个伙伴项 · ${readyCount} 个已就绪；可点击直接同帧跳转配置。`
  );

  for (const plugin of merged) {
    const card = document.createElement("article");
    card.className = "companion-card";
    card.dataset.companionId = plugin.id;

    const top = document.createElement("div");
    top.className = "companion-card-top";
    const h4 = document.createElement("h4");
    h4.textContent = plugin.label;

    const badge = document.createElement("span");
    const isGood = plugin.state === "ready" || plugin.state === "enabled";
    const isWarn = plugin.state === "conflict" || plugin.state === "error";
    badge.className = `pill-tag ${isGood ? "" : isWarn ? "pill-danger" : "pill-muted"}`;
    badge.textContent = COMPANION_STATES[plugin.state] || plugin.state;
    top.append(h4, badge);

    const role = document.createElement("p");
    role.className = "companion-role";
    role.textContent = plugin.role || plugin.settingsNote || "群间系列伙伴插件";

    const body = document.createElement("div");
    body.append(top, role);
    if (plugin.version) {
      const ver = document.createElement("p");
      ver.className = "companion-meta";
      ver.textContent = plugin.version;
      body.append(ver);
    }
    card.append(body);

    const actions = document.createElement("div");
    actions.className = "companion-card-actions";
    if (plugin.canOpen) {
      const openBtn = document.createElement("button");
      openBtn.type = "button";
      openBtn.className = "button button-quiet";
      openBtn.dataset.openCompanion = plugin.id;
      openBtn.textContent = `前往 ${plugin.label}`;
      openBtn.addEventListener("click", async () => {
        openBtn.disabled = true;
        try {
          if (typeof window.DynamicsSeriesNavigation?.open === "function") {
            await window.DynamicsSeriesNavigation.open(plugin.id);
          } else {
            throw new Error("跨面板跳转接口暂不可用，请点击顶部互通插件栏打开。");
          }
        } catch (err) {
          setStatus(
            els.companionNote,
            friendlyError(err, `无法打开 ${plugin.label}，请稍后重试。`),
            true
          );
        } finally {
          openBtn.disabled = false;
        }
      });
      actions.append(openBtn);
    } else {
      const hint = document.createElement("span");
      hint.className = "companion-meta";
      hint.textContent =
        plugin.state === "missing"
          ? "未安装 · 可按需在插件市场获取"
          : "当前无独立可跳转面板";
      actions.append(hint);
    }
    card.append(actions);
    els.companionCards.append(card);
  }
}

async function loadCompanions() {
  els.btnRefreshCompanions.disabled = true;
  if (els.btnActivateCompanions) els.btnActivateCompanions.disabled = true;
  setStatus(els.companionNote, "正在读取伙伴插件状态…");
  try {
    const [presRes, intRes] = await Promise.allSettled([
      apiGet("series_presence", {}, { timeoutMs: 8000 }),
      apiGet("integrations", {}, { timeoutMs: 8000 }),
    ]);
    state.seriesPresence = presRes.status === "fulfilled" ? presRes.value : null;
    state.integrations = intRes.status === "fulfilled" ? intRes.value : null;
    renderCompanions();
  } catch (err) {
    setStatus(els.companionNote, friendlyError(err, "伙伴插件状态读取失败。"), true);
  } finally {
    els.btnRefreshCompanions.disabled = false;
    if (els.btnActivateCompanions) els.btnActivateCompanions.disabled = false;
  }
}

async function activateInstalledCompanions() {
  if (!els.btnActivateCompanions || state.busy) return;
  els.btnActivateCompanions.disabled = true;
  els.btnRefreshCompanions.disabled = true;
  setStatus(els.companionNote, "正在一键激活已安装的群间系列伙伴并同步群聊作用域…");
  try {
    const targetUmos = [];
    const seen = new Set();
    const excludeSet = new Set(parseList(state.draft.exclude_groups));
    const takeoverList = parseList(state.draft.takeover_groups);
    const byGid = new Map();
    for (const g of state.groups || []) {
      const gid = String(g?.group_id || "").trim();
      const umo = String(g?.umo || g?.session_key || "").trim();
      if (gid && umo) byGid.set(gid, umo);
    }
    const candidates = state.draft.takeover_all
      ? (state.groups || []).map((g) => String(g?.umo || g?.group_id || "").trim())
      : takeoverList.map((gid) => byGid.get(gid) || gid);
    for (const item of candidates) {
      if (!item || excludeSet.has(item) || seen.has(item)) continue;
      seen.add(item);
      targetUmos.push(item);
    }
    const res = await apiPost(
      "setup/activate_companions",
      { umos: targetUmos },
      { timeoutMs: 15000 }
    );
    const activated = Array.isArray(res?.activated) ? res.activated : [];
    const skipped = Array.isArray(res?.skipped) ? res.skipped : [];
    const failed = Array.isArray(res?.failed) ? res.failed : [];
    const summaryParts = [
      `已激活 ${activated.length} 个伙伴`,
      skipped.length ? `跳过 ${skipped.length} 个` : "",
      failed.length ? `失败 ${failed.length} 个` : "",
    ].filter(Boolean);
    setStatus(els.companionNote, summaryParts.join(" · "), failed.length > 0 && activated.length === 0);
    await loadCompanions();
  } catch (err) {
    setStatus(els.companionNote, friendlyError(err, "一键激活伙伴失败，请稍后重试。"), true);
  } finally {
    els.btnActivateCompanions.disabled = false;
    els.btnRefreshCompanions.disabled = false;
  }
}

function updateStepperSummary() {
  const d = state.draft;
  const groups = parseList(d.takeover_groups);
  const scopeMeta = d.takeover_all
    ? "全部群聊"
    : groups.length > 0
      ? `已选 ${groups.length} 群`
      : d.private_chat_enabled
        ? "仅私聊"
        : "待选群聊";
  els.stepMetas[0].textContent = scopeMeta;
  const step1Ready = Boolean(d.enable) && (Boolean(d.takeover_all) || groups.length > 0 || Boolean(d.private_chat_enabled));
  els.stepBtns[0].classList.toggle("is-ready", step1Ready);
  els.stepBtns[0].classList.toggle("has-warn", !step1Ready);

  const isLlm = d.participation_backend === "llm";
  const modelReady = isLlm || Boolean(String(d.decision_provider || "").trim());
  els.stepMetas[1].textContent = isLlm
    ? "普通 LLM"
    : d.decision_provider
      ? `Jev · ${d.decision_provider}`
      : "待选 Jev 模型";
  els.stepBtns[1].classList.toggle("is-ready", modelReady);
  els.stepBtns[1].classList.toggle("has-warn", !modelReady);

  const styleLabel =
    state.selectedStyle === "keep_custom"
      ? "保持自定义"
      : STYLE_PRESETS.find((p) => p.id === state.selectedStyle)?.label || "懂事自然";
  els.stepMetas[2].textContent = styleLabel;
  els.stepBtns[2].classList.add("is-ready");

  const diffCount = computeDiff().length;
  els.stepMetas[3].textContent = `${diffCount} 项待保存`;
}

async function persistSetupDismissed() {
  storageSet(DISMISSED_KEY, "1");
  try {
    await apiPost("ui_preferences", { setup_dismissed: true }, { timeoutMs: 5000 });
  } catch {
    /* localStorage fallback already set */
  }
}

async function saveSetupConfig() {
  if (state.busy) return;
  const warnings = evaluatePathWarnings();
  const blocking = warnings.find((w) => w.blocking);
  if (blocking) {
    setStatus(els.saveNote, blocking.text, true);
    renderReview();
    return;
  }

  const nonBlockingScopeWarn = warnings.find((w) => w.step === 1);
  if (nonBlockingScopeWarn) {
    const confirmFn = window.DynamicsUI?.confirm;
    const confirmed = confirmFn
      ? await confirmFn(
          msg("当前运行路径尚未完整（{0}）。仍要继续保存吗？", nonBlockingScopeWarn.text),
          { confirmLabel: msg("继续保存"), cancelLabel: msg("先回去补充") }
        )
      : true;
    if (!confirmed) {
      setStep(1, { focusPanel: true });
      return;
    }
  }

  const diff = computeDiff();
  const prevTakeover = parseList(state.initialDraft.takeover_groups);
  const nextTakeover = parseList(state.draft.takeover_groups);
  const isAllScope = Boolean(state.draft.takeover_all);
  const excludeGroups = parseList(state.draft.exclude_groups);
  const chosenTemplateId = String(state.selectedTemplateId || "").trim();
  const chosenTemplate = state.templates.find((t) => t && t.id === chosenTemplateId) || null;

  state.busy = true;
  els.btnSaveSetup.disabled = true;
  els.saveSuccessBanner.classList.add("hidden");
  if (els.setupTemplateReceipt) {
    els.setupTemplateReceipt.classList.add("hidden");
    els.setupTemplateReceipt.replaceChildren();
  }

  try {
    if (diff.length > 0) {
      setStatus(els.saveNote, "正在保存并应用配置…");
      const updates = {};
      const baseline = {};
      const stored = state.configState.stored || {};
      for (const item of diff) {
        updates[item.key] = item.after;
        baseline[item.key] = Object.hasOwn(stored, item.key) ? stored[item.key] : null;
      }
      const savedPanel = await apiPost("config", { config: updates, baseline }, { timeoutMs: 10000 });
      if (savedPanel && (savedPanel.effective || savedPanel.stored)) {
        applyLoadedConfig(savedPanel, { preserveStyleSelection: true });
      } else {
        // Fallback when mock returns empty object
        const mergedStored = { ...stored, ...updates };
        const mergedEffective = { ...(state.configState.effective || {}), ...updates };
        applyLoadedConfig(
          { schema: state.configState.schema, stored: mergedStored, effective: mergedEffective },
          { preserveStyleSelection: true }
        );
      }
    }
    await persistSetupDismissed();
    els.saveSuccessBanner.classList.remove("hidden");

    if (chosenTemplateId) {
      const targets = resolveTemplateTargetUmos({
        prevTakeover,
        nextTakeover,
        isAll: isAllScope,
        excludeGroups,
      });
      const tplName = chosenTemplate?.name || chosenTemplateId;
      if (!targets.length) {
        renderTemplateReceipt({
          tplName,
          results: [],
          emptyReason: tr("主控配置已保存；当前未选中具体群聊，未执行模板批量下发。"),
        });
      } else {
        setStatus(els.saveNote, tr("配置已保存，正在向 {0} 个群下发模板「{1}」…", targets.length, tplName));
        const batchResults = [];
        for (const target of targets) {
          try {
            const res = await apiPost(
              "series_matrix_batch",
              { umo: target.umo, template_id: chosenTemplateId },
              { timeoutMs: 12000 }
            );
            batchResults.push({
              ...target,
              applied: Array.isArray(res?.applied) ? res.applied : [],
              skipped: Array.isArray(res?.skipped) ? res.skipped : [],
              failed: Array.isArray(res?.failed) ? res.failed : [],
              error: "",
            });
          } catch (err) {
            batchResults.push({
              ...target,
              applied: [],
              skipped: [],
              failed: [],
              error: friendlyError(err, "模板下发失败"),
            });
          }
        }
        renderTemplateReceipt({ tplName, results: batchResults });
      }
    }

    setStatus(
      els.saveNote,
      diff.length > 0
        ? `已保存 ${diff.length} 项配置并立即生效，其余高级参数保持不变。`
        : "已确认当前配置，引导完成。"
    );
    renderReview();
    renderSharedDefaults();
    updateStepperSummary();
  } catch (err) {
    setStatus(els.saveNote, friendlyError(err, "保存失败，请检查上方提示后重试。"), true);
  } finally {
    state.busy = false;
    els.btnSaveSetup.disabled = false;
  }
}

async function dismissSetupWizard() {
  if (state.dismissing) return;
  state.dismissing = true;
  els.btnDismissSetup.disabled = true;
  try {
    await persistSetupDismissed();
    await navigateToPluginPage("console", els.btnDismissSetup);
  } finally {
    state.dismissing = false;
    els.btnDismissSetup.disabled = false;
  }
}

function applyLoadedConfig(panel, { preserveStyleSelection = false } = {}) {
  state.configState = {
    schema: panel?.schema && typeof panel.schema === "object" ? panel.schema : {},
    stored:
      panel?.stored && typeof panel.stored === "object"
        ? panel.stored
        : panel?.values && typeof panel.values === "object"
          ? panel.values
          : {},
    effective: panel?.effective && typeof panel.effective === "object" ? panel.effective : {},
  };
  state.initialDraft = buildInitialDraft(state.configState);
  state.draft = cloneDraft(state.initialDraft);
  state.hasEffectiveScope =
    Boolean(state.initialDraft.takeover_all) ||
    state.initialDraft.takeover_groups.length > 0 ||
    Boolean(state.initialDraft.private_chat_enabled);

  els.setupReentryBanner.classList.toggle("hidden", !state.hasEffectiveScope);

  const matched = detectStylePreset(state.initialDraft);
  state.showKeepCustom = state.hasEffectiveScope || matched === null;
  if (!preserveStyleSelection) {
    state.selectedStyle = state.showKeepCustom ? "keep_custom" : matched;
  } else if (state.showKeepCustom && matched === null) {
    state.selectedStyle = "keep_custom";
  }

  syncStep1InputsFromDraft();
  syncStep2InputsFromDraft();
  syncStep3InputsFromDraft();
  updateStepperSummary();
  if (state.step === 4) {
    renderReview();
    renderSharedDefaults();
  }
}

function bindEvents() {
  // Stepper free-jump
  els.stepBtns.forEach((btn, idx) => {
    btn.addEventListener("click", () => setStep(idx + 1));
  });
  els.btnPrevStep.addEventListener("click", () => setStep(state.step - 1, { focusPanel: true }));
  els.btnNextStep.addEventListener("click", () => setStep(state.step + 1, { focusPanel: true }));

  els.btnDismissSetup.addEventListener("click", () => {
    void dismissSetupWizard();
  });

  // Step 1 events
  els.enablePluginToggle.addEventListener("change", () => {
    state.draft.enable = Boolean(els.enablePluginToggle.checked);
    updateStepperSummary();
  });
  els.modeTakeoverSpecific.addEventListener("change", () => {
    if (!els.modeTakeoverSpecific.checked) return;
    state.draft.takeover_all = false;
    syncStep1InputsFromDraft();
    updateStepperSummary();
  });
  els.modeTakeoverAll.addEventListener("change", () => {
    if (!els.modeTakeoverAll.checked) return;
    state.draft.takeover_all = true;
    syncStep1InputsFromDraft();
    updateStepperSummary();
  });
  els.groupSearch.addEventListener("input", () => renderGroupList());
  els.btnRefreshGroups.addEventListener("click", () => {
    void loadSetupGroups({ manual: true });
  });
  els.takeoverGroupsInput.addEventListener("input", () => {
    state.draft.takeover_groups = parseList(els.takeoverGroupsInput.value);
    renderGroupList();
    updateStepperSummary();
  });
  els.excludeGroupsInput.addEventListener("input", () => {
    state.draft.exclude_groups = parseList(els.excludeGroupsInput.value);
    els.excludeCountBadge.textContent = String(state.draft.exclude_groups.length);
    renderGroupList();
    updateStepperSummary();
  });
  els.botNamesInput.addEventListener("input", () => {
    state.draft.bot_names = parseList(els.botNamesInput.value);
    renderBotNamesPreview();
    updateStepperSummary();
  });
  els.setupTemplateSelect?.addEventListener("change", () => {
    state.selectedTemplateId = String(els.setupTemplateSelect.value || "").trim();
    renderTemplatePreview();
    if (state.step === 4) renderReview();
  });
  window.addEventListener("series-languagechange", () => {
    if (state.templatesLoaded) renderTemplateSelector();
  });

  // Step 2 events
  els.btnRefreshProviders.addEventListener("click", () => {
    void loadProviders({ applyRecommend: false });
  });
  els.btnApplyModelRecommend.addEventListener("click", () => {
    if (!state.recommendation) return;
    state.draft.participation_backend = state.recommendation.backend;
    if (state.recommendation.backend === "jev" && state.recommendation.decisionProvider) {
      state.draft.decision_provider = state.recommendation.decisionProvider;
    }
    syncStep2InputsFromDraft();
    updateStepperSummary();
  });
  els.backendJev.addEventListener("change", () => {
    if (!els.backendJev.checked) return;
    state.draft.participation_backend = "jev";
    if (!state.draft.decision_provider && state.providers.decision.length > 0) {
      state.draft.decision_provider = String(state.providers.decision[0]?.id || "");
    }
    syncStep2InputsFromDraft();
    updateStepperSummary();
  });
  els.backendLlm.addEventListener("change", () => {
    if (!els.backendLlm.checked) return;
    state.draft.participation_backend = "llm";
    syncStep2InputsFromDraft();
    updateStepperSummary();
  });
  els.decisionProviderSelect.addEventListener("change", () => {
    state.draft.decision_provider = String(els.decisionProviderSelect.value || "").trim();
    syncStep2InputsFromDraft();
    updateStepperSummary();
  });
  els.decisionLlmProviderSelect.addEventListener("change", () => {
    state.draft.decision_llm_provider = String(els.decisionLlmProviderSelect.value || "").trim();
    updateStepperSummary();
  });
  els.replyProviderSelect.addEventListener("change", () => {
    state.draft.reply_provider = String(els.replyProviderSelect.value || "").trim();
    updateStepperSummary();
  });
  els.discussionModeSelect.addEventListener("change", () => {
    state.draft.discussion_judgment_mode =
      els.discussionModeSelect.value === "llm" ? "llm" : "rerank";
    syncStep2InputsFromDraft();
    updateStepperSummary();
  });
  els.discussionRerankerSelect.addEventListener("change", () => {
    state.draft.discussion_reranker_provider = String(
      els.discussionRerankerSelect.value || ""
    ).trim();
    updateStepperSummary();
  });
  els.discussionLlmSelect.addEventListener("change", () => {
    state.draft.discussion_llm_provider = String(els.discussionLlmSelect.value || "").trim();
    updateStepperSummary();
  });
  els.btnProbeModels.addEventListener("click", () => {
    void runConnectivityProbe();
  });

  // Step 3 events
  els.togglePrivateChat.addEventListener("change", () => {
    state.draft.private_chat_enabled = Boolean(els.togglePrivateChat.checked);
    updateStepperSummary();
  });
  els.toggleDailyRhythm.addEventListener("change", () => {
    state.draft.daily_rhythm_enabled = Boolean(els.toggleDailyRhythm.checked);
    updateStepperSummary();
  });
  els.toggleMediaUnderstand.addEventListener("change", () => {
    state.draft.media_understand_reply_enabled = Boolean(els.toggleMediaUnderstand.checked);
    updateStepperSummary();
  });

  // Step 4 events
  els.btnSaveSetup.addEventListener("click", () => {
    void saveSetupConfig();
  });
  els.btnGoConsole.addEventListener("click", () => {
    void navigateToPluginPage("console", els.btnGoConsole);
  });
  els.btnGoConfig.addEventListener("click", () => {
    void navigateToPluginPage("config", els.btnGoConfig);
  });
  if (els.btnActivateCompanions) {
    els.btnActivateCompanions.addEventListener("click", () => {
      void activateInstalledCompanions();
    });
  }
  els.btnRefreshCompanions.addEventListener("click", () => {
    void loadCompanions();
  });

  // Navigation guard for unsaved edits (bypassed when dismissing)
  const beforeNavigate = async () => {
    if (state.dismissing) return true;
    if (state.busy) return false;
    const diff = computeDiff();
    if (!diff.length) return true;
    if (typeof window.DynamicsUI?.confirm === "function") {
      return window.DynamicsUI.confirm(
        msg("引导页还有 {0} 项未保存的调整，离开将不会生效。继续离开吗？", diff.length),
        { danger: true }
      );
    }
    return true;
  };
  window.ChatDynamicsBeforeNavigate = beforeNavigate;
  window.DynamicsBeforeNavigate = beforeNavigate;
}

async function boot() {
  mountPluginSideNav("setup");
  bindEvents();
  setStep(1);
  apply(document);

  try {
    await readyBridge();
    setLink(els, true, tr("已连接"));
  } catch {
    setLink(els, false, tr("连接超时"));
  }

  try {
    const cfg = await apiGet("config", {}, { timeoutMs: 8000 });
    applyLoadedConfig(cfg || {});
    setStatus(els.setupStatus, "已读取当前配置，可自由点击上方步骤或在底部按步推进。");
  } catch (err) {
    applyLoadedConfig({});
    setStatus(els.setupStatus, friendlyError(err, "读取配置失败，请刷新页面重试。"), true);
  }

  // Load groups, providers, templates, and companion status in parallel (never auto-probe!)
  await Promise.allSettled([
    loadProviders({ applyRecommend: true }),
    loadSetupGroups({ manual: false }),
    loadSetupTemplates(),
    loadCompanions(),
  ]);
  updateStepperSummary();
  if (state.step === 4) {
    renderReview();
    renderSharedDefaults();
  }
}

void boot();
