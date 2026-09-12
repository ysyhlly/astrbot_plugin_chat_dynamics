const PLUGIN = "astrbot_plugin_chat_dynamics";
const REQUEST_TIMEOUT_MS = 8000;
const CHAT_PROVIDER_KEYS = new Set([
  "provider",
  "reply_provider",
  "vibe_provider",
  "decision_provider",
  "topic_reranker_provider",
]);
const EMBEDDING_PROVIDER_KEYS = new Set(["embedding_provider"]);
const BASIC_HINTS = {
  enable: "保持开启即可。关闭后，本插件不参与群聊处理。",
  takeover_all: "通常保持关闭，先在下方填写一个群号。开启后对所有未排除的群生效。",
  takeover_groups: "填写要启用的群号，多个群可用逗号或换行分隔。不填且未开启全部群时，插件不会介入。",
  exclude_groups: "可选。这里的群始终排除，即使开启了全部群。",
  bot_names: "可选。直接 @ 机器人无需填写；想用昵称点名时，再填写完整昵称。",
  decision_mode: "通常使用规则模式即可。人设模式沿用当前人格，先调用模型判断是否参与，再生成回复，会增加一次模型调用。",
  presence_knob: "保持默认即可。需要时调整参与分寸；是否主动发言仍由当前决策模式决定。",
};
const OPTION_LABELS = {
  decision_mode: { legacy: "规则模式（默认）", persona_model: "人设模式（模型判断）" },
  presence_knob: { ghost: "安静", sensible: "适度（默认）", lively: "活跃" },
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
      "decision_mode",
      "command_prefix"
    ]
  },
  {
    "id": "providers",
    "title": "回复与决策模型",
    "blurb": "选择回复、决策和氛围模型，设置决策等待时间",
    "open": true,
    "keys": [
      "reply_provider",
      "decision_provider",
      "decision_timeout",
      "reply_timeout",
      "tool_agent_timeout",
      "vibe_provider",
      "provider"
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
    "blurb": "向量模型、关联阈值与缓存容量",
    "open": false,
    "keys": [
      "neural_embedding_enabled",
      "embedding_provider",
      "neural_link_threshold",
      "embedding_cache_size"
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
    "blurb": "氛围分析、统计窗口与模式切换阈值",
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
      "selflearning_integration"
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
  btnConfigReload: document.getElementById("btnConfigReload"),
  btnConfigApply: document.getElementById("btnConfigApply"),
  btnConfigSave: document.getElementById("btnConfigSave"),
  configSearch: document.getElementById("configSearch"),
  configCategory: document.getElementById("configCategory"),
  configResultCount: document.getElementById("configResultCount"),
  configScopeStatus: document.getElementById("configScopeStatus"),
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

function filterConfigFields() {
  const query = els.configSearch.value.trim().toLocaleLowerCase();
  const basic = configMode === "basic" && !query;
  els.configForm.dataset.view = basic ? "basic" : "advanced";
  els.btnBasicConfig.setAttribute("aria-pressed", String(configMode === "basic"));
  els.btnAdvancedConfig.setAttribute("aria-pressed", String(configMode === "advanced"));
  els.btnConfigApply.classList.toggle("hidden", basic && !configState.mismatches.length);
  let count = 0;
  els.configForm.querySelectorAll("details.config-group").forEach((group) => {
    let matches = 0;
    group.querySelectorAll(".config-field").forEach((field) => {
      const visible = query ? group.dataset.search.includes(query) || field.dataset.search.includes(query)
        : !basic || field.dataset.basic === "true";
      field.hidden = !visible;
      if (visible) matches += 1;
    });
    group.hidden = !matches;
    group.open = query || basic ? Boolean(matches) : openGroups.has(group.dataset.groupId);
    if (!group.querySelector(".mismatched")) group.querySelector(".group-badge").textContent = `${matches} 项`;
    count += matches;
  });
  document.getElementById("configEmpty").classList.toggle("hidden", count > 0);
  els.configResultCount.textContent = query ? `在全部设置中找到 ${count} 项` : basic
    ? `${count} 项常用设置 · 更多选项可搜索或切换到全部设置`
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
  const value = (key) => {
    const input = els.configForm.querySelector(`[data-config-key="${key}"]`);
    if (configDirty && input) return input.type === "checkbox" ? input.checked : input.value;
    return configState.effective[key] ?? configState.stored[key] ?? configState.schema[key]?.default;
  };
  const list = (raw) => Array.isArray(raw) ? raw : String(raw || "").split(/[\n,，]/).map(s => s.trim()).filter(Boolean);
  const excluded = new Set(list(value("exclude_groups")).map(String));
  const groups = [...new Set(list(value("takeover_groups")).map(String))].filter(id => !excluded.has(id));
  let message = !value("enable") ? "插件已关闭。开启后再保存即可生效。"
    : value("takeover_all") ? `对全部群生效${excluded.size ? `，排除 ${excluded.size} 个群` : ""}。`
    : groups.length ? `对 ${groups.length} 个指定群生效。`
    : "尚未选择有效群聊，请填写群号或开启「对全部群聊生效」。";
  if (value("shadow_mode")) message += " 当前为观察模式，插件不会接管回复；可在全部设置中关闭。";
  if (configState.mismatches.length && !configDirty) message += " 有已存设置尚未应用，以下表单可能与运行状态不同。";
  els.configScopeStatus.textContent = `${configDirty ? "保存后" : "当前"}：${message}`;
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
    els.btnConfigSave.disabled = !configDirty;
    els.btnConfigSave.title = configDirty ? "保存并应用到运行时" : "没有未保存的修改";
  }
  document.querySelector(".action-bar").dataset.dirty = String(configDirty);
  updateScopeStatus();
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

function parseNumericField(key, type, raw) {
  const text = String(raw ?? "").trim();
  if (!text) {
    throw new Error(`${key} 不能为空`);
  }
  const number = type === "int" ? Number.parseInt(text, 10) : Number.parseFloat(text);
  if (!Number.isFinite(number)) {
    throw new Error(`${key} 不是有效数字`);
  }
  if (type === "int" && !Number.isInteger(number)) {
    throw new Error(`${key} 必须是整数`);
  }
  return number;
}

function collectConfigUpdates() {
  const updates = {};
  if (!els.configForm) return updates;
  const inputs = els.configForm.querySelectorAll("[data-config-key]");
  for (const input of inputs) {
    const key = input.dataset.configKey;
    const schema = configState.schema[key] || {};
    const type = schema.type || "string";
    if (type === "bool") {
      updates[key] = Boolean(input.checked);
      continue;
    }
    if (type === "int" || type === "float") {
      updates[key] = parseNumericField(key, type, input.value);
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
  return updates;
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
  if (type === "bool") {
    control = `<span class="config-check"><input type="checkbox" role="switch" aria-label="${escapeHtml(title)}" data-config-key="${escapeHtml(key)}" ${value ? "checked" : ""}/><span class="check-state" aria-hidden="true"><span class="check-on">已开启</span><span class="check-off">已关闭</span></span></span>`;
  } else if (isProviderField(key, schema) && type === "string") {
    control = renderProviderSelect(key, value);
  } else if (type === "string" && Array.isArray(schema.options) && schema.options.length) {
    control = `<select data-config-key="${escapeHtml(key)}">${schema.options
      .map(
        (opt) =>
          `<option value="${escapeHtml(opt)}" ${String(value) === String(opt) ? "selected" : ""}>${escapeHtml(OPTION_LABELS[key]?.[opt] || opt)}</option>`,
      )
      .join("")}</select>`;
  } else if (type === "list") {
    control = `<textarea data-config-key="${escapeHtml(key)}" rows="2" placeholder="逗号或换行分隔">${escapeHtml(value)}</textarea>`;
  } else if (type === "int" || type === "float") {
    control = `<input type="number" data-config-key="${escapeHtml(key)}" value="${escapeHtml(value)}" step="${type === "int" ? "1" : "any"}"/>`;
  } else {
    control = `<input type="text" data-config-key="${escapeHtml(key)}" value="${escapeHtml(value)}"/>`;
  }
  const effectiveText =
    eff === undefined ? "—" : escapeHtml(typeof eff === "object" ? JSON.stringify(eff) : String(eff));
  return `<label class="config-field${mismatched}${span}" data-basic="${Object.hasOwn(BASIC_HINTS, key)}" data-search="${escapeHtml(`${title} ${key} ${hint} ${BASIC_HINTS[key] || ""}`.toLocaleLowerCase())}">
    <span class="config-title">${escapeHtml(title)}</span>
    <span class="config-key">${escapeHtml(key)}</span>
    ${control}
    <span class="config-effective">生效：${effectiveText}</span>
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
  };
  const keys = Object.keys(configState.schema);
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
    input.addEventListener("change", updateEditedFields);
    input.addEventListener("input", updateEditedFields);
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
  if (!els.configForm) return;
  els.configForm.setAttribute("aria-busy", "true");
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
    setConfigNote((err && err.message) || "读取配置失败", true);
    els.configForm.setAttribute("aria-busy", "false");
  }
}

async function saveConfigPanel() {
  if (!els.configForm) return;
  let updates;
  try {
    updates = collectConfigUpdates();
  } catch (err) {
    setConfigNote((err && err.message) || "表单校验失败", true);
    return;
  }
  setConfigNote("正在保存并应用…");
  if (els.btnConfigSave) els.btnConfigSave.disabled = true;
  try {
    const panel = await apiPost("config", { config: updates });
    renderConfigForm(panel || {});
    setConfigNote("已保存并应用到运行时");
  } catch (err) {
    setConfigDirty(true);
    setConfigNote((err && err.message) || "保存失败", true);
  }
}

async function applyConfigPanel() {
  setConfigNote("正在强制应用已存配置…");
  try {
    const panel = await apiPost("config/apply", {});
    renderConfigForm(panel || {});
    setConfigNote("已强制同步到运行时");
  } catch (err) {
    setConfigNote((err && err.message) || "应用失败", true);
  }
}

async function boot() {
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
  els.pageTitle.textContent = t("pages.config.title", "参数配置");
  await wirePageNav("config");
  els.pageDesc.textContent = t(
    "pages.config.desc",
    "定义参与范围，调整每一次回应。",
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
    void loadConfigPanel();
  });
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
