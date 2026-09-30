import { friendlyError } from "./api.js";

const PLUGIN = "astrbot_plugin_chat_dynamics";
const REQUEST_TIMEOUT_MS = 8000;
const CHAT_PROVIDER_KEYS = new Set([
  "provider",
  "reply_provider",
  "vibe_provider",
  "decision_provider",
  "topic_reranker_provider",
  // The draft provider is a model choice like the others; without it here the
  // field rendered as a free-text box while every sibling offered the list.
  "annotation_draft_provider",
]);
const EMBEDDING_PROVIDER_KEYS = new Set(["embedding_provider"]);
const ROUTE_KEYS = new Set([
  "enable", "shadow_mode", "takeover_all", "takeover_groups", "exclude_groups",
  "decision_mode", "decision_learning_mode", "decision_learning_student_backend",
  "decision_backend", "kev_base_url", "kev_checkpoint_id", "reply_provider",
  "topic_reranker_enabled", "vibe_llm_enabled",
]);
const BASE_BASIC_KEYS = new Set([
  "enable", "shadow_mode", "takeover_all", "takeover_groups", "exclude_groups",
  "decision_mode", "decision_backend", "decision_learning_mode",
  "decision_learning_student_backend", "kev_base_url", "kev_checkpoint_id",
  "kev_timeout", "reply_provider", "reply_timeout", "bot_names",
]);
const BASIC_HINTS = {
  enable: "关闭后，本插件不会处理群聊消息。",
  shadow_mode: "观察模式只记录判断，不发送插件回复。",
  takeover_all: "选择指定群或全部群。排除名单始终优先。",
  takeover_groups: "仅在“指定群”模式生效；多个群号用逗号或换行分隔。",
  exclude_groups: "这里的群始终不接管，即使选择全部群。",
  bot_names: "只有希望通过昵称点名时才需要填写；直接 @ 无需设置。",
  decision_mode: "Kev 独占决策需要选择“人设与模型决策”。",
  decision_backend: "选择 Kev 后，整轮决策由独立决策模型完成；服务失败时保持沉默。",
  decision_learning_mode: "Kev 独占决策需要“实际接管”。",
  decision_learning_student_backend: "Kev 独占决策需要学生后端也设为 Kev。",
  kev_base_url: "填写 AstrBot 容器可访问的 Kev 服务地址。",
  kev_checkpoint_id: "锁定发布版检查点；与服务返回的 ID 不一致时拒绝采用。",
  kev_timeout: "单次 Kev 请求最长等待时间；超时后本轮保持沉默。",
  reply_provider: "只生成回复正文，不决定是否开口。留空则使用 AstrBot 默认回复模型。",
  reply_timeout: "回复正文生成超时，与 Kev 决策超时分别计算。",
};
const OPTION_LABELS = {
  decision_mode: { legacy: "规则判断（兼容）", persona_model: "人设与模型决策" },
  decision_backend: { kev: "Kev 决策模型", model: "聊天模型（旧配置；请切换）", jev: "Jev 决策模型", laya: "Laya 决策模型" },
  decision_learning_mode: { off: "关闭", collect: "教师采集（兼容）", shadow: "旁路观察（兼容）", active: "实际接管" },
  decision_learning_student_backend: { kev: "Kev", agentjev: "AgentJev（兼容）", laya: "Laya（兼容）" },
  agentjev_input_format: { legacy: "旧格式", cmdcode_full_input_soft_v1: "Command Code 完整输入" },
  // Keep the wording identical to the schema hint and to 今日读空气/分寸台/控制台.
  presence_knob: { ghost: "隐身", sensible: "懂事（默认）", lively: "活跃" },
  pipeline_mode: { filter: "过滤（默认，不影响其它插件）", exclusive: "独占（会吞掉后续插件）" },
  learning_policy_mode: { off: "关闭（默认，不读取学习层）", shadow: "观察（只对比不应用）", active: "应用（需通过三项检查）" },
};

const CONFIG_GROUPS = [
  {
    "id": "basics",
    "title": "启用与接管范围",
    "blurb": "先确定在哪些群生效，以及使用哪种决策方式",
    "open": true,
    "keys": [
      "enable",
      "shadow_mode",
      "pipeline_mode",
      "ambient_intervention",
      "takeover_all",
      "takeover_groups",
      "exclude_groups",
      "bot_names",
      "command_prefix"
    ]
  },
  {
    "id": "decision",
    "title": "决策模型 Kev",
    "blurb": "决定是否开口、动作、目标和回复长度；服务失败时保持沉默",
    "open": true,
    "keys": [
      "decision_mode",
      "decision_backend",
      "decision_learning_mode",
      "decision_learning_student_backend",
      "kev_base_url",
      "kev_checkpoint_id",
      "kev_timeout",
      "kev_internal_hosts",
      "decision_timeout"
    ]
  },
  {
    "id": "providers",
    "title": "回复正文",
    "blurb": "选择生成回复文字的模型；不参与开口判断",
    "open": false,
    "keys": [
      "reply_provider",
      "reply_timeout",
      "tool_agent_timeout",
      "provider"
    ]
  },
  {
    "id": "collection",
    "title": "决策记录",
    "blurb": "选择哪些会话留存样本，以及保留期限",
    "open": false,
    "keys": [
      "decision_learning_sessions",
      "decision_learning_retention_days",
      "decision_learning_sample_rate",
      "decision_learning_labels_per_hour"
    ]
  },
  {
    "id": "routing",
    "title": "话题识别与归属",
    "blurb": "话题识别、模型复判、上下文窗口与父消息判断",
    "open": true,
    "keys": [
      "conversation_router_enabled",
      "topic_reranker_enabled",
      "topic_reranker_provider",
      "topic_reranker_timeout",
      "topic_window_seconds",
      "replay_message_limit",
      "topic_join_threshold",
      "topic_commit_threshold",
      "topic_ambiguity_threshold",
      "topic_margin_threshold",
      "parent_window_seconds",
      "parent_accept_threshold",
      "routing_neural_timeout"
    ]
  },
  {
    "id": "embedding",
    "title": "语义理解",
    "blurb": "向量模型、关联阈值、缓存容量与有效期",
    "open": false,
    "keys": [
      "neural_embedding_enabled",
      "embedding_provider",
      "neural_link_threshold",
      "embedding_cache_size",
      "embedding_cache_ttl_seconds"
    ]
  },
  {
    "id": "addressivity",
    "title": "点名与冷却",
    "blurb": "明确点名、等待接话与深度冷却",
    "open": false,
    "keys": [
      "strong_addressivity_threshold",
      "safe_hover_threshold",
      "deep_cooling_minutes"
    ]
  },
  {
    "id": "manners",
    "title": "社交分寸",
    "blurb": "参与程度、接话礼仪与私密话题边界",
    "open": false,
    "keys": [
      "presence_knob",
      "social_manners_enabled",
      "relay_baton_enabled",
      "private_field_enabled",
      "deciding_detect_enabled"
    ]
  },
  {
    "id": "proactive",
    "title": "主动参与与配额",
    "blurb": "主动找话题、新人保护与发言次数限制",
    "open": false,
    "keys": [
      "gap_fill_proactive_enabled",
      "cold_memory_nudge_enabled",
      "newcomer_caution_enabled",
      "hyped_quota_enabled",
      "proactive_quota_enabled",
      "proactive_quota_per_hour",
      "proactive_quota_per_topic"
    ]
  },
  {
    "id": "media",
    "title": "图片与语音",
    "blurb": "媒体理解、回复门槛与隐私保护",
    "open": false,
    "keys": [
      "media_image_gate_enabled",
      "media_voice_gate_enabled",
      "media_understand_reply_enabled",
      "media_privacy_strict"
    ]
  },
  {
    "id": "vibe",
    "title": "群聊氛围",
    "blurb": "低频氛围校准仅在规则模式启用；人设模式使用本地遥测",
    "open": false,
    "keys": [
      "vibe_llm_enabled",
      "telemetrics_window_seconds",
      "fast_banter_enter_mpm",
      "chill_fade_enter_mpm"
    ]
  },
  {
    "id": "wts",
    "title": "发言意愿权重",
    "blurb": "调整话题、专业性、问题价值、参与度与疲劳的影响",
    "open": false,
    "keys": [
      "wts_topic_weight",
      "wts_professionalism_weight",
      "wts_question_weight",
      "wts_participation_weight",
      "wts_fatigue_weight"
    ]
  },
  {
    "id": "debounce",
    "title": "消息合并",
    "blurb": "等待碎片补充，控制一轮消息的最长合并时间",
    "open": false,
    "keys": [
      "debounce_base_cooldown",
      "debounce_extended_cooldown",
      "debounce_max_cap"
    ]
  },
  {
    "id": "pacing",
    "title": "回复节奏与格式",
    "blurb": "打字速度、分段间隔、长度与文字风格",
    "open": false,
    "keys": [
      "chars_per_second",
      "base_thinking_delay",
      "max_fragments",
      "max_fragment_chars",
      "inter_burst_interval",
      "pace_align_enabled",
      "casual_emoji_enabled",
      "strip_markdown_in_banter"
    ]
  },
  {
    "id": "daily_rhythm",
    "title": "每日作息",
    "blurb": "早晚问候、入睡、唤醒与当晚安排",
    "open": false,
    "keys": [
      "daily_rhythm_enabled",
      "rhythm_timezone",
      "rhythm_morning_hi_enabled",
      "rhythm_day_share_slots",
      "rhythm_goodnight_text_quota",
      "rhythm_sleep_after_winddown",
      "rhythm_allow_self_sleep",
      "rhythm_allow_wake",
      "rhythm_insomnia_enabled",
      "rhythm_force_sleep",
      "rhythm_skip_morning_hi_tonight"
    ]
  },
  {
    "id": "memory",
    "title": "记忆与联动",
    "blurb": "群记忆、情绪记忆、口头禅与自学习插件联动",
    "open": false,
    "keys": [
      "group_memory_enabled",
      "mood_memory_enabled",
      "slang_trial_enabled",
      "selflearning_integration",
      "selflearning_hub_url",
      "selflearning_hub_key_env"
    ]
  },
  {
    "id": "learning",
    "title": "发布策略联动",
    "blurb": "读取 Dynamics Learning 的已发布策略；与上方决策学习模式独立",
    "open": false,
    "keys": [
      "learning_policy_mode",
      "learning_policy_source_id",
      "learning_policy_expected_policy_id",
      "learning_policy_expected_dataset_fingerprint",
      "learning_policy_refresh_seconds"
    ]
  },
  {
    "id": "drafting",
    "title": "离线预标注",
    "blurb": "人工审批用的标注草稿；与在线决策无关",
    "open": false,
    "keys": [
      "annotation_draft_enabled",
      "annotation_draft_auto_enabled",
      "annotation_draft_interval_minutes",
      "annotation_draft_provider",
      "annotation_draft_limit",
      "annotation_draft_timeout"
    ]
  },
  {
    "id": "compatibility",
    "title": "旧后端兼容设置",
    "blurb": "仅供已有配置迁移与故障排查；Kev 独占模式不使用",
    "open": false,
    "keys": [
      "agentjev_all_tasks_active",
      "agentjev_all_tasks_shadow",
      "agentjev_active_checkpoint_sha256",
      "agentjev_input_format",
      "agentjev_base_url",
      "agentjev_internal_hosts",
      "agentjev_timeout",
      "kev_canary_percent",
      "decision_learning_jev_fallback",
      "decision_provider",
      "vibe_provider",
      "vibe_backend",
      "laya_base_url",
      "laya_timeout",
      "laya_min_confidence",
      "laya_max_uncertainty",
      "laya_internal_hosts"
    ]
  },
  {
    "id": "console",
    "title": "面板隐私",
    "blurb": "控制面板是否显示消息正文",
    "open": false,
    "keys": [
      "console_show_message_content"
    ]
  }
];

const SPAN2_KEYS = new Set([
  "takeover_groups",
  "exclude_groups",
  "bot_names",
  "provider",
  "agentjev_active_checkpoint_sha256",
]);

const els = {
  pageTitle: document.getElementById("pageTitle"),
  pageDesc: document.getElementById("pageDesc"),
  linkLamp: document.getElementById("linkLamp"),
  linkLabel: document.getElementById("linkLabel"),
  actionTitle: document.getElementById("actionTitle"),
  configForm: document.getElementById("configForm"),
  configNote: document.getElementById("configNote"),
  configMismatch: document.getElementById("configMismatch"),
  configWarnings: document.getElementById("configWarnings"),
  btnConfigReload: document.getElementById("btnConfigReload"),
  btnConfigApply: document.getElementById("btnConfigApply"),
  btnConfigSave: document.getElementById("btnConfigSave"),
  configSearch: document.getElementById("configSearch"),
  configCategory: document.getElementById("configCategory"),
  configResultCount: document.getElementById("configResultCount"),
  configStartTitle: document.getElementById("configStartTitle"),
  configOverviewState: document.getElementById("configOverviewState"),
  configConflicts: document.getElementById("configConflicts"),
  configConflictTitle: document.getElementById("configConflictTitle"),
  configConflictCount: document.getElementById("configConflictCount"),
  configConflictList: document.getElementById("configConflictList"),
  configScopeStatus: document.getElementById("configScopeStatus"),
  configScopeHeadline: document.getElementById("configScopeHeadline"),
  configDecisionHeadline: document.getElementById("configDecisionHeadline"),
  configDecisionStatus: document.getElementById("configDecisionStatus"),
  configModelHeadline: document.getElementById("configModelHeadline"),
  configModelStatus: document.getElementById("configModelStatus"),
  decisionOverview: document.getElementById("decisionOverview"),
  btnBasicConfig: document.getElementById("btnBasicConfig"),
  btnAdvancedConfig: document.getElementById("btnAdvancedConfig"),
};

let bridge = window.AstrBotPluginPage || null;

async function wirePageNav(currentPage) {
  const mod = await import("./plugin_nav.js");
  mod.mountPluginSideNav(currentPage);
}


let configState = { schema: {}, stored: {}, effective: {}, mismatches: [] };
let providerOptions = { chat: [], embedding: [] };
let configDirty = false;
let configBusy = false;
let navigationApproved = false;
let configMode = "basic";
let openGroups = new Set(CONFIG_GROUPS.filter((group) => group.open).map((group) => group.id));
const VIEW_STORAGE_KEY = `${PLUGIN}:config-view:v1`;
try {
  const view = JSON.parse(localStorage.getItem(VIEW_STORAGE_KEY) || "null");
  if (view && Array.isArray(view.openGroups)) openGroups = new Set(view.openGroups.filter((id) => typeof id === "string"));
  if (view?.mode === "advanced") configMode = "advanced";
} catch (_) { /* Storage may be unavailable in embedded pages. */ }

function persistView() {
  try { localStorage.setItem(VIEW_STORAGE_KEY, JSON.stringify({ openGroups: [...openGroups], mode: configMode })); } catch (_) { /* Keep controls usable without storage. */ }
}

function settingValue(key) {
  const input = els.configForm.querySelector(`[data-config-key="${key}"]`);
  if (configDirty && input) return input.dataset.booleanSelect === "true" ? input.value === "true"
    : input.type === "checkbox" ? input.checked : input.value;
  return configState.effective[key] ?? configState.stored[key] ?? configState.schema[key]?.default;
}

function listSetting(raw) {
  return Array.isArray(raw) ? raw.map(String).filter(Boolean)
    : String(raw || "").split(/[\n,，]/).map(item => item.trim()).filter(Boolean);
}

function routeInfo() {
  const value = settingValue;
  const excluded = new Set(listSetting(value("exclude_groups")));
  const whitelist = [...new Set(listSetting(value("takeover_groups")))];
  const groups = whitelist.filter(id => !excluded.has(id));
  const enabled = Boolean(value("enable"));
  const takeoverAll = Boolean(value("takeover_all"));
  const persona = value("decision_mode") === "persona_model";
  const backend = value("decision_backend");
  const learningMode = value("decision_learning_mode");
  const student = value("decision_learning_student_backend");
  const modelOnly = persona && backend === "kev" && learningMode === "active" && student === "kev";
  const kevUrl = String(value("kev_base_url") || "").trim();
  const checkpoint = String(value("kev_checkpoint_id") || "").trim();
  const observing = Boolean(value("shadow_mode"));
  const issues = [];
  if (takeoverAll && whitelist.length) issues.push({key: "takeover_groups", kind: "未使用", text: "已选择全部群聊，白名单不会缩小生效范围；排除名单仍有效。"});
  if (!modelOnly)
    issues.push({key: !persona ? "decision_mode" : backend !== "kev" ? "decision_backend" :
      learningMode !== "active" ? "decision_learning_mode" : "decision_learning_student_backend",
      kind: "未启用 Kev 独占", text: "主回合还未由 Kev 完整接管。依次选择人设与模型决策、Kev 后端、实际接管和 Kev 学生模型。"});
  if (modelOnly && !kevUrl)
    issues.push({key: "kev_base_url", kind: "服务地址缺失", text: "Kev 服务地址为空，模型请求无法发出，本轮会保持沉默。"});
  if (modelOnly && !checkpoint)
    issues.push({key: "kev_checkpoint_id", kind: "检查点未锁定", text: "填写已核对的发布版检查点 ID；身份不匹配时会保持沉默。"});
  if (!modelOnly && Boolean(value("topic_reranker_enabled")))
    issues.push({key: "topic_reranker_enabled", kind: "额外模型调用", text: "LLM 话题重排已开启。若只允许 Kev 做在线判断，请关闭它。"});
  if (!modelOnly && Boolean(value("vibe_llm_enabled")))
    issues.push({key: "vibe_llm_enabled", kind: "额外模型调用", text: "LLM 氛围校准已开启。若只允许 Kev 做在线判断，请关闭它。"});
  if (observing)
    issues.push({key: "shadow_mode", kind: "不会发送", text: "插件总观察模式已开启，判断结果只记录，不发送插件回复。"});
  if (learningMode !== "off" && !listSetting(value("decision_learning_sessions")).length)
    issues.push({key: "decision_learning_sessions", kind: "不留样本", text: "未指定采集会话；在线决策仍运行，但不会保存逐题样本。"});
  return {value, excluded, whitelist, groups, enabled, takeoverAll, persona, learningMode,
    student, backend, modelOnly, kevUrl, checkpoint, observing, issues};
}

function basicKeysForRoute(route) {
  const keys = new Set(BASE_BASIC_KEYS);
  if (route.learningMode !== "off") keys.add("decision_learning_sessions");
  return keys;
}

function filterConfigFields() {
  const query = els.configSearch.value.trim().toLocaleLowerCase();
  const basic = configMode === "basic" && !query;
  const basicKeys = basic ? basicKeysForRoute(routeInfo()) : null;
  els.configForm.dataset.view = basic ? "basic" : "advanced";
  els.btnBasicConfig.setAttribute("aria-pressed", String(configMode === "basic"));
  els.btnAdvancedConfig.setAttribute("aria-pressed", String(configMode === "advanced"));
  els.btnConfigApply.classList.toggle("hidden", !configState.mismatches.length);
  let count = 0;
  els.configForm.querySelectorAll("details.config-group").forEach((group) => {
    let matches = 0;
    group.querySelectorAll(".config-field").forEach((field) => {
      const visible = query ? group.dataset.search.includes(query) || field.dataset.search.includes(query)
        : !basic || basicKeys.has(field.querySelector("[data-config-key]")?.dataset.configKey);
      field.hidden = !visible;
      if (visible) matches += 1;
    });
    group.hidden = !matches;
    group.open = query || basic ? Boolean(matches) : openGroups.has(group.dataset.groupId);
    if (!group.querySelector(".mismatched")) group.querySelector(".group-badge").textContent = `${matches} 项`;
    const nav = [...document.querySelectorAll("#configCategoryNav [data-category]")]
      .find(button => button.dataset.category === group.dataset.groupId);
    if (nav) {
      nav.hidden = !matches;
      nav.querySelector("span").textContent = basic ? `${matches}/${group.querySelectorAll(".config-field").length}` : String(matches);
      nav.title = basic ? `当前显示 ${matches} 项，本类共 ${group.querySelectorAll(".config-field").length} 项` : "";
    }
    count += matches;
  });
  document.getElementById("configEmpty").classList.toggle("hidden", count > 0);
  els.configResultCount.textContent = query ? `在全部设置中找到 ${count} 项` : basic
    ? `${count} 项常用设置 · 其他参数可搜索或切换到全部`
    : `共 ${count} 项设置 · 分组展开状态自动记住`;
}

function setConfigMode(mode) {
  rememberOpenGroups();
  configMode = mode;
  els.configSearch.value = "";
  persistView();
  filterConfigFields();
}

function updateScopeStatus() {
  if (!Object.keys(configState.schema).length) return;
  const route = routeInfo();
  const {value, excluded, groups, observing} = route;
  let message = !route.enabled ? "插件已关闭。开启后再保存即可生效。"
    : route.takeoverAll ? `对全部未排除群生效${excluded.size ? `，排除 ${excluded.size} 个群` : ""}。指定群列表当前不限制范围。`
    : groups.length ? `对 ${groups.length} 个指定群生效。`
    : "尚未选择有效群聊，请填写群号或将范围改为“全部群”。";
  if (observing) message += " 插件总观察已开启，不会发送插件回复。";
  if (configState.mismatches.length && !configDirty) message += " 有已存设置尚未应用，以下表单可能与运行状态不同。";
  els.configScopeStatus.textContent = `${configDirty ? "保存后" : "当前"}：${message}`;
  els.configScopeHeadline.textContent = !route.enabled ? "插件已关闭"
    : route.takeoverAll ? "全部未排除群"
    : groups.length ? `${groups.length} 个指定群聊` : "尚未选择群聊";
  const impact = document.getElementById("configActionImpact");
  if (impact) impact.textContent = configDirty
    ? route.enabled && (route.takeoverAll || groups.length)
      ? `保存后立即影响${route.takeoverAll ? "全部未排除群" : `${groups.length} 个指定群`}；保存前的修改不会生效。`
      : "保存后仍不会接管群聊，直到启用插件并选择生效范围。"
    : "修改设置后，保存会立即应用到当前生效的群聊。";
  const backendName = OPTION_LABELS.decision_backend[route.backend] || route.backend || "未选择";
  const decisionTitle = route.modelOnly ? "Kev 独占决策" : route.persona ? backendName : "本地规则判断";
  const decisionText = route.modelOnly
    ? "整轮问题由 Kev 决策模型回答；旧教师、LLM 话题重排和 LLM 氛围校准在此路径中停用。"
    : route.persona ? "当前仍在兼容决策路径。若要求只有 Kev 判断，请核对下方四项模型设置。"
      : "当前由本地规则处理主回合；下方 Kev 设置不会接管主回合。";
  const modelTitle = route.modelOnly ? "失败即静默" : "检查实际回退路径";
  const modelText = route.modelOnly
    ? "Kev 超时、缺题或检查点不符时，本轮保持沉默。回复模型只在决策通过后生成正文。"
    : "兼容路径可能使用旧模型或教师回退；回复正文仍由独立的回复 Provider 生成。";
  els.decisionOverview.dataset.route = route.modelOnly ? "kev" : "default";
  els.configDecisionHeadline.textContent = decisionTitle;
  els.configDecisionStatus.textContent = decisionText;
  els.configModelHeadline.textContent = modelTitle;
  els.configModelStatus.textContent = modelText;
  els.configStartTitle.textContent = configDirty ? "保存后路径预览" : "当前运行路径";
  els.configOverviewState.textContent = configDirty ? "尚未保存" : configState.mismatches.length ? "已保存未应用" : "已应用";
  els.configOverviewState.dataset.state = configDirty ? "draft" : configState.mismatches.length ? "pending" : "applied";
  els.configConflicts.classList.toggle("hidden", !route.issues.length);
  els.configConflictTitle.textContent = configDirty ? "保存前请核对" : "配置路径需要核对";
  els.configConflictCount.textContent = `${route.issues.length} 项`;
  els.configConflictList.innerHTML = route.issues.map(issue => `<li><span>${escapeHtml(issue.kind)}</span><p>${escapeHtml(issue.text)}</p>${issue.key
    ? `<button type="button" data-focus-key="${escapeHtml(issue.key)}">定位参数</button>` : ""}</li>`).join("");
  updateFieldContexts(route);
}

function updateFieldContexts(route) {
  const contexts = {
    takeover_groups: route.takeoverAll ? "当前选择全部群，此列表不会限制范围" : "仅这些群会被接管",
    kev_base_url: route.modelOnly ? "当前在线决策服务" : "仅在 Kev 独占路径使用",
    kev_checkpoint_id: route.modelOnly ? "每次预测前核对服务检查点 ID" : "仅在 Kev 独占路径使用",
    reply_provider: "仅生成回复正文，不决定是否开口",
    decision_provider: route.modelOnly ? "Kev 独占路径不使用" : "兼容路径可能使用",
    topic_reranker_enabled: route.modelOnly ? "只允许 Kev 判断时应关闭" : "",
    vibe_llm_enabled: route.modelOnly ? "只允许 Kev 判断时应关闭" : "",
    learning_policy_mode: "发布策略联动独立于决策模型设置",
  };
  els.configForm.querySelectorAll("[data-context-key]").forEach(node => {
    const message = contexts[node.dataset.contextKey] || "";
    node.textContent = message;
    node.hidden = !message;
    node.closest(".config-field")?.classList.toggle("context-inactive", message.includes("不使用") || message.includes("不会限制") || message.includes("仅在"));
  });
}

function t(key, fallback) {
  if (bridge && typeof bridge.t === "function") {
    const value = bridge.t(key, fallback);
    if (value) return value;
  }
  return fallback;
}

function unwrap(payload) {
  if (!payload) return null;
  if (payload.status === "error" || payload.ok === false) {
    throw new Error(payload.message || payload.error || "请求失败");
  }
  if (payload.status === "ok" && payload.data !== undefined) return payload.data;
  if (payload.ok === true && payload.data !== undefined) return payload.data;
  return payload;
}

function withTimeout(promise, timeoutMs = REQUEST_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const timeoutId = window.setTimeout(() => reject(new Error("请求超时")), timeoutMs);
    Promise.resolve(promise).then(
      (value) => {
        window.clearTimeout(timeoutId);
        resolve(value);
      },
      (error) => {
        window.clearTimeout(timeoutId);
        reject(error);
      },
    );
  });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function setLink(ok, label) {
  els.linkLamp.classList.toggle("on", ok);
  els.linkLamp.classList.toggle("warn", !ok);
  els.linkLabel.textContent = label;
}

async function apiGet(endpoint, params = {}) {
  if (!bridge && window.AstrBotPluginPage) bridge = window.AstrBotPluginPage;
  if (bridge && typeof bridge.apiGet === "function") {
    return unwrap(await withTimeout(bridge.apiGet(endpoint, params)));
  }
  throw new Error("Plugin Page bridge 不可用");
}

async function apiPost(endpoint, body = {}) {
  if (!bridge && window.AstrBotPluginPage) bridge = window.AstrBotPluginPage;
  if (bridge && typeof bridge.apiPost === "function") {
    return unwrap(await withTimeout(bridge.apiPost(endpoint, body)));
  }
  throw new Error("Plugin Page bridge 不可用");
}

function setConfigNote(message, isError = false) {
  if (!els.configNote) return;
  els.configNote.textContent = message || "";
  els.configNote.classList.toggle("error", Boolean(isError));
}

function setConfigDirty(dirty) {
  configDirty = Boolean(dirty);
  if (els.actionTitle) {
    els.actionTitle.textContent = configDirty ? "有未保存修改" : "所有修改已保存";
  }
  if (els.btnConfigSave) {
    els.btnConfigSave.disabled = configBusy || !configDirty;
    els.btnConfigSave.title = configDirty ? "保存并应用到运行时" : "没有未保存的修改";
  }
  document.querySelector(".action-bar").dataset.dirty = String(configDirty);
  updateScopeStatus();
}

function setConfigBusy(busy) {
  configBusy = Boolean(busy);
  els.configForm.setAttribute("aria-busy", String(configBusy));
  els.configForm.querySelectorAll("[data-config-key]").forEach(input => { input.disabled = configBusy; });
  els.btnConfigReload.disabled = configBusy;
  els.btnConfigApply.disabled = configBusy;
  setConfigDirty(configDirty);
}

function updateEditedFields() {
  let count = 0;
  els.configForm.querySelectorAll("[data-config-key]").forEach(input => {
    const initial = configFieldValue(input.dataset.configKey);
    const current = input.type === "checkbox" ? input.checked : input.value;
    const changed = String(initial) !== String(current);
    input.closest(".config-field").dataset.dirty = String(changed);
    if (changed) count += 1;
  });
  setConfigDirty(count > 0);
  if (count) els.actionTitle.textContent = `${count} 项未保存修改`;
}

function isProviderField(key, schema) {
  if (CHAT_PROVIDER_KEYS.has(key) || EMBEDDING_PROVIDER_KEYS.has(key)) return true;
  const special = String((schema && schema._special) || "");
  return special === "select_provider" || special.startsWith("select_provider");
}

function providerListForKey(key) {
  if (EMBEDDING_PROVIDER_KEYS.has(key)) return providerOptions.embedding || [];
  return providerOptions.chat || [];
}

function configFieldValue(key) {
  const schema = configState.schema[key] || {};
  const type = schema.type || "string";
  const raw = configState.stored[key] ?? schema.default;
  if (type === "bool") return Boolean(raw);
  if (type === "int") return Number.isFinite(Number(raw)) ? Number(raw) : Number(schema.default || 0);
  if (type === "float") return Number.isFinite(Number(raw)) ? Number(raw) : Number(schema.default || 0);
  if (type === "list") {
    if (Array.isArray(raw)) return raw.join(", ");
    return raw == null ? "" : String(raw);
  }
  return raw == null ? "" : String(raw);
}

function fieldError(key, message) {
  const error = new Error(message);
  error.configKey = key;
  return error;
}

function parseNumericField(key, type, raw, schema = {}) {
  // Speak in the field's own Chinese label: the raw key is shown next to it,
  // but "防抖硬上限时间" is what the user just typed into.
  const label = schema.description || key;
  const text = String(raw ?? "").trim();
  if (!text) {
    throw fieldError(key, `「${label}」不能为空`);
  }
  const number = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(text) ? Number(text) : NaN;
  if (!Number.isFinite(number)) {
    throw fieldError(key, `「${label}」必须是数字`);
  }
  if (type === "int" && !Number.isInteger(number)) {
    throw fieldError(key, `「${label}」必须是整数`);
  }
  // The schema publishes the usable range as `slider`; without this the panel
  // accepted any number and left the bound to fail silently at runtime.
  const slider = schema.slider && typeof schema.slider === "object" ? schema.slider : null;
  const min = slider ? Number(slider.min) : NaN;
  const max = slider ? Number(slider.max) : NaN;
  if (Number.isFinite(min) && number < min) {
    throw fieldError(key, `「${label}」不能小于 ${min}`);
  }
  if (Number.isFinite(max) && number > max) {
    throw fieldError(key, `「${label}」不能大于 ${max}`);
  }
  return number;
}

function collectConfigUpdates() {
  const updates = {};
  if (!els.configForm) return updates;
  const inputs = els.configForm.querySelectorAll("[data-config-key]");
  for (const input of inputs) {
    const key = input.dataset.configKey;
    const current = input.type === "checkbox" ? input.checked : input.value;
    if (String(current) === String(configFieldValue(key))) continue;
    const schema = configState.schema[key] || {};
    const type = schema.type || "string";
    if (type === "bool") {
      updates[key] = input.dataset.booleanSelect === "true" ? input.value === "true" : Boolean(input.checked);
      continue;
    }
    if (type === "int" || type === "float") {
      updates[key] = parseNumericField(key, type, input.value, schema);
      continue;
    }
    if (type === "list") {
      updates[key] = String(input.value || "")
        .split(/[\n,，]/)
        .map((part) => part.trim())
        .filter(Boolean);
      continue;
    }
    updates[key] = String(input.value ?? "");
  }
  return Object.fromEntries(Object.entries(updates).filter(([key, value]) =>
    JSON.stringify(value) !== JSON.stringify(configState.stored[key] ?? configState.schema[key]?.default)));
}

function renderProviderSelect(key, value) {
  const options = providerListForKey(key);
  const current = String(value ?? "");
  const seen = new Set();
  const parts = [`<option value="">（空 / 沿用默认）</option>`];
  for (const row of options) {
    const id = String((row && row.id) || "");
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const label = String((row && row.label) || id);
    parts.push(
      `<option value="${escapeHtml(id)}" ${current === id ? "selected" : ""}>${escapeHtml(label)}</option>`,
    );
  }
  if (current && !seen.has(current)) {
    parts.push(
      `<option value="${escapeHtml(current)}" selected>${escapeHtml(current)}（未在列表中）</option>`,
    );
  }
  return `<select data-config-key="${escapeHtml(key)}">${parts.join("")}</select>`;
}

function renderField(key, mismatchSet) {
  const schema = configState.schema[key] || {};
  const type = schema.type || "string";
  const title = schema.description || key;
  const hint = schema.hint || "";
  const eff = configState.effective[key];
  const mismatched = mismatchSet.has(key) ? " mismatched" : "";
  const span = SPAN2_KEYS.has(key) || type === "list" ? " span-2" : "";
  const value = configFieldValue(key);
  let control = "";
  if (key === "takeover_all" && type === "bool") {
    control = `<select data-config-key="takeover_all" data-boolean-select="true" aria-label="群聊生效范围">
      <option value="false" ${value ? "" : "selected"}>仅指定群</option>
      <option value="true" ${value ? "selected" : ""}>全部群（排除名单除外）</option>
    </select>`;
  } else if (type === "bool") {
    control = `<span class="config-check"><input type="checkbox" role="switch" aria-label="${escapeHtml(title)}" data-config-key="${escapeHtml(key)}" ${value ? "checked" : ""}/><span class="check-state" aria-hidden="true"><span class="check-on">已开启</span><span class="check-off">已关闭</span></span></span>`;
  } else if (isProviderField(key, schema) && type === "string") {
    control = renderProviderSelect(key, value);
  } else if (type === "string" && Array.isArray(schema.options) && schema.options.length) {
    control = `<select data-config-key="${escapeHtml(key)}">${schema.options
      .filter((opt) => key !== "decision_backend" || opt !== "model" || String(value) === "model")
      .map(
        (opt) =>
          `<option value="${escapeHtml(opt)}" ${String(value) === String(opt) ? "selected" : ""}>${escapeHtml(OPTION_LABELS[key]?.[opt] || opt)}</option>`,
      )
      .join("")}</select>`;
  } else if (type === "list") {
    control = `<textarea data-config-key="${escapeHtml(key)}" rows="2" placeholder="逗号或换行分隔">${escapeHtml(value)}</textarea>`;
  } else if (type === "int" || type === "float") {
    const slider = schema.slider && typeof schema.slider === "object" ? schema.slider : {};
    const bounds = [
      Number.isFinite(Number(slider.min)) ? ` min="${escapeHtml(String(slider.min))}"` : "",
      Number.isFinite(Number(slider.max)) ? ` max="${escapeHtml(String(slider.max))}"` : "",
    ].join("");
    const step = slider.step ?? (type === "int" ? 1 : "any");
    control = `<input type="number" data-config-key="${escapeHtml(key)}" value="${escapeHtml(value)}" step="${escapeHtml(String(step))}"${bounds}/>`;
  } else {
    control = `<input type="text" data-config-key="${escapeHtml(key)}" value="${escapeHtml(value)}"/>`;
  }
  const effectiveText =
    eff === undefined ? "—" : escapeHtml(typeof eff === "object" ? JSON.stringify(eff) : String(eff));
  return `<label class="config-field${mismatched}${span}" data-basic="${Object.hasOwn(BASIC_HINTS, key)}" data-search="${escapeHtml(`${title} ${key} ${hint} ${BASIC_HINTS[key] || ""}`.toLocaleLowerCase())}">
    <span class="config-title">${escapeHtml(title)}</span>
    <span class="config-key">${escapeHtml(key)}</span>
    ${control}
    <span class="config-context" data-context-key="${escapeHtml(key)}" hidden></span>
    <span class="config-effective">生效：${effectiveText}${configState.learning_policy_overridden?.includes(key) ? ` · 学习策略覆盖（${escapeHtml(configState.learning_policy?.policy_id || configState.learning_policy?.source_id || "当前策略")}），保存基础值后仍可能被覆盖` : ""}</span>
    ${hint ? `<span class="config-hint config-detail-hint">${escapeHtml(hint)}</span>` : ""}
    ${BASIC_HINTS[key] ? `<span class="config-hint config-basic-hint">${escapeHtml(BASIC_HINTS[key])}</span>` : ""}
  </label>`;
}

function rememberOpenGroups() {
  if (configMode === "basic") return;
  if (!els.configForm || els.configSearch.value.trim() || !els.configForm.querySelector("details.config-group")) return;
  openGroups = new Set();
  els.configForm.querySelectorAll("details.config-group").forEach((node) => {
    if (node.open && node.dataset.groupId) openGroups.add(node.dataset.groupId);
  });
}

function groupedKeys(schemaKeys) {
  const remaining = new Set(schemaKeys);
  const groups = CONFIG_GROUPS.map((group) => {
    const keys = group.keys.filter((key) => remaining.has(key));
    keys.forEach((key) => remaining.delete(key));
    return { ...group, keys };
  }).filter((group) => group.keys.length);
  if (remaining.size) {
    groups.push({
      id: "other",
      title: "新增设置",
      blurb: "当前版本新增的参数，仍可在这里查看和修改",
      open: false,
      keys: Array.from(remaining),
    });
  }
  return groups;
}

function renderConfigForm(panel) {
  if (!els.configForm) return;
  rememberOpenGroups();
  configState = {
    schema: panel && panel.schema ? panel.schema : {},
    stored: panel && panel.stored ? panel.stored : {},
    effective: panel && panel.effective ? panel.effective : {},
    mismatches: Array.isArray(panel && panel.mismatches) ? panel.mismatches : [],
    learning_policy: panel?.learning_policy || {},
    learning_policy_overridden: Array.isArray(panel?.learning_policy_overridden) ? panel.learning_policy_overridden : [],
    warnings: Array.isArray(panel?.warnings) ? panel.warnings : [],
  };
  if (els.configWarnings) {
    els.configWarnings.classList.toggle("hidden", !configState.warnings.length);
    els.configWarnings.innerHTML = configState.warnings.length
      ? `<strong>运行配置提示</strong><ul>${configState.warnings.map(warning => `<li>${escapeHtml(warning)}</li>`).join("")}</ul>`
      : "";
  }
  const keys = Object.keys(configState.schema).filter(key => configState.schema[key]?.invisible !== true);
  els.configForm.setAttribute("aria-busy", "false");
  if (!keys.length) {
    els.configForm.innerHTML = '<p class="empty">暂无配置 schema。</p>';
    return;
  }
  const mismatchSet = new Set(configState.mismatches);
  if (els.configMismatch) {
    if (mismatchSet.size) {
      els.configMismatch.classList.remove("hidden");
      els.configMismatch.textContent = `这些参数已保存但与运行时不一致：${Array.from(mismatchSet).join("、")}。可点「强制应用」。`;
    } else {
      els.configMismatch.classList.add("hidden");
      els.configMismatch.textContent = "";
    }
  }

  const groups = groupedKeys(keys);
  const selectedCategory = els.configCategory.value;
  els.configCategory.innerHTML = '<option value="">选择设置分类…</option>' + groups.map((group) =>
    `<option value="${escapeHtml(group.id)}">${escapeHtml(group.title)} · ${group.keys.length} 项</option>`).join("");
  els.configCategory.value = groups.some((group) => group.id === selectedCategory) ? selectedCategory : "";
  document.getElementById("configCategoryNav").innerHTML = groups.map(group =>
    `<button type="button" class="category-link" data-category="${escapeHtml(group.id)}">${escapeHtml(group.title)}<span>${group.keys.length}</span></button>`).join("");
  els.configForm.innerHTML = groups
    .map((group) => {
      const isOpen = openGroups.has(group.id);
      const mismatchCount = group.keys.filter((key) => mismatchSet.has(key)).length;
      const badge = mismatchCount
        ? `<span class="group-badge">${mismatchCount} 项不一致</span>`
        : `<span class="group-badge">${group.keys.length} 项</span>`;
      return `<details class="config-group" data-group-id="${escapeHtml(group.id)}" data-search="${escapeHtml(`${group.title} ${group.blurb}`.toLocaleLowerCase())}" ${isOpen ? "open" : ""}>
        <summary>
          <div class="group-title">
            <strong>${escapeHtml(group.title)}</strong>
            <span>${escapeHtml(group.blurb)}</span>
          </div>
          ${badge}
        </summary>
        <div class="group-body">
          <div class="config-grid">${group.keys.map((key) => renderField(key, mismatchSet)).join("")}</div>
        </div>
      </details>`;
    })
    .join("");

  setConfigDirty(false);
  els.configForm.querySelectorAll("details.config-group").forEach((group) => {
    group.addEventListener("toggle", () => {
      if (els.configSearch.value.trim()) return;
      rememberOpenGroups();
      persistView();
    });
  });
  filterConfigFields();
  els.configForm.querySelectorAll("[data-config-key]").forEach((input) => {
    const clearFieldError = () => {
      if (!input.hasAttribute("aria-invalid")) return;
      input.removeAttribute("aria-invalid");
      input.closest(".config-field")?.classList.remove("has-error");
    };
    const onEdit = () => {
      clearFieldError();
      updateEditedFields();
      if (ROUTE_KEYS.has(input.dataset.configKey)) filterConfigFields();
    };
    input.addEventListener("change", onEdit);
    input.addEventListener("input", onEdit);
  });
}

async function loadProviders() {
  try {
    const data = await apiGet("providers");
    providerOptions = {
      chat: Array.isArray(data && data.chat) ? data.chat : [],
      embedding: Array.isArray(data && data.embedding) ? data.embedding : [],
    };
  } catch (_err) {
    providerOptions = { chat: [], embedding: [] };
    throw _err;
  }
}

async function loadConfigPanel() {
  if (!els.configForm || configBusy) return;
  setConfigBusy(true);
  try {
    const [providers, panel] = await Promise.allSettled([loadProviders(), apiGet("config")]);
    if (panel.status === "rejected") throw panel.reason;
    renderConfigForm(panel.value || {});
    const chatN = providerOptions.chat.length;
    const embN = providerOptions.embedding.length;
    setConfigNote(providers.status === "rejected"
      ? "配置已读取，模型列表暂时不可用；已保存的模型选择仍会保留。"
      : `已读取 · ${chatN} 个聊天模型 · ${embN} 个向量模型（均可选）`);
  } catch (err) {
    setConfigNote(friendlyError(err, "读取配置失败，请稍后重试。"), true);
  } finally {
    setConfigBusy(false);
  }
}

function focusConfigField(key) {
  if (!key) return;
  const input = els.configForm.querySelector(`[data-config-key="${key}"]`);
  if (!input) return;
  els.configSearch.value = "";
  configMode = "advanced";
  const group = input.closest("details.config-group");
  if (group) openGroups.add(group.dataset.groupId);
  filterConfigFields();
  persistView();
  input.setAttribute("aria-invalid", "true");
  input.closest(".config-field")?.classList.add("has-error");
  input.focus({ preventScroll: true });
  input.scrollIntoView({ block: "center" });
}

async function saveConfigPanel() {
  if (!els.configForm || configBusy) return;
  let updates;
  try {
    updates = collectConfigUpdates();
  } catch (err) {
    setConfigNote((err && err.message) || "表单校验失败", true);
    focusConfigField(err && err.configKey);
    return;
  }
  setConfigNote("正在保存并应用…");
  const baseline = Object.fromEntries(Object.keys(updates).map(key => [key, configState.stored[key] ?? null]));
  setConfigBusy(true);
  try {
    const panel = await apiPost("config", { config: updates, baseline });
    renderConfigForm(panel || {});
    setConfigNote("已保存并应用到运行时");
  } catch (err) {
    setConfigDirty(true);
    setConfigNote(friendlyError(err, "保存失败，请稍后重试。"), true);
  } finally {
    setConfigBusy(false);
  }
}

async function applyConfigPanel() {
  if (configBusy) return;
  if (configDirty && !window.confirm("有未保存的修改，强制应用已存配置会丢弃它们。继续吗？")) return;
  setConfigBusy(true);
  setConfigNote("正在强制应用已存配置…");
  try {
    const panel = await apiPost("config/apply", {});
    renderConfigForm(panel || {});
    setConfigNote("已强制同步到运行时");
  } catch (err) {
    setConfigNote(friendlyError(err, "应用失败，请稍后重试。"), true);
  } finally {
    setConfigBusy(false);
  }
}

async function boot() {
  els.configConflictList.addEventListener("click", event => {
    const button = event.target.closest("[data-focus-key]");
    if (button) focusConfigField(button.dataset.focusKey);
  });
  document.getElementById("btnClearSearch").addEventListener("click", () => {
    els.configSearch.value = "";
    filterConfigFields();
    els.configSearch.focus();
  });
  document.getElementById("configCategoryNav").addEventListener("click", event => {
    const button = event.target.closest("[data-category]");
    if (!button) return;
    els.configCategory.value = button.dataset.category;
    els.configCategory.dispatchEvent(new Event("change"));
  });
  els.btnBasicConfig.addEventListener("click", () => setConfigMode("basic"));
  els.btnAdvancedConfig.addEventListener("click", () => setConfigMode("advanced"));
  els.configSearch.addEventListener("input", filterConfigFields);
  els.configCategory.addEventListener("change", () => {
    const id = els.configCategory.value;
    const group = [...els.configForm.querySelectorAll("details.config-group")].find((item) => item.dataset.groupId === id);
    if (!group) return;
    document.querySelectorAll("[data-category]").forEach(button => button.setAttribute("aria-current", String(button.dataset.category === id)));
    setConfigMode("advanced");
    openGroups.add(id);
    persistView();
    filterConfigFields();
    group.querySelector("summary").focus({preventScroll: true});
    group.scrollIntoView({block: "start"});
  });
  for (const [id, expand] of [["btnExpandGroups", true], ["btnCollapseGroups", false]]) {
    document.getElementById(id).addEventListener("click", () => {
      setConfigMode("advanced");
      openGroups = new Set(expand ? [...els.configForm.querySelectorAll("details.config-group")].map((group) => group.dataset.groupId) : []);
      persistView();
      filterConfigFields();
    });
  }
  els.pageTitle.textContent = t("pages.config.title", "插件设置");
  await wirePageNav("config");
  els.pageDesc.textContent = t(
    "pages.config.desc",
    "核对生效群聊、Kev 决策和回复生成，再保存到运行时。",
  );
  if (bridge && typeof bridge.ready === "function") {
    try {
      await withTimeout(bridge.ready());
      setLink(true, "已连接");
    } catch (_err) {
      setLink(false, "连接超时");
    }
  } else {
    setLink(false, "本地预览");
  }
  els.btnConfigReload.addEventListener("click", () => {
    if (configDirty && !window.confirm("有未保存的修改，重新读取会丢弃它们。继续吗？")) return;
    void loadConfigPanel();
  });
  window.addEventListener("beforeunload", (event) => {
    if (navigationApproved) { navigationApproved = false; return; }
    if (!configDirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
  window.ChatDynamicsBeforeNavigate = () => {
    if (configBusy) return false;
    if (configDirty && !window.confirm("有未保存的修改，离开会丢弃它们。继续吗？")) return false;
    navigationApproved = true;
    window.setTimeout(() => { navigationApproved = false; }, 0);
    return true;
  };
  els.btnConfigApply.addEventListener("click", () => {
    void applyConfigPanel();
  });
  els.btnConfigSave.addEventListener("click", () => {
    void saveConfigPanel();
  });
  setConfigDirty(false);
  await loadConfigPanel();
}

boot();
