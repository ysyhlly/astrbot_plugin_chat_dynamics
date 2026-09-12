import { renderIntegrations } from "./integrations.js";
import { mountWorkspace } from "./workspace.js";

const PLUGIN = "astrbot_plugin_chat_dynamics";
const MODE_LABEL = {
  fast_banter: "快速碎梗",
  serious_inquiry: "严肃探讨",
  chill_fade: "能量衰退",
};

const els = {
  pageTitle: document.getElementById("pageTitle"),
  pageDesc: document.getElementById("pageDesc"),
  clock: document.getElementById("clock"),
  linkLamp: document.getElementById("linkLamp"),
  linkLabel: document.getElementById("linkLabel"),
  statEnabled: document.getElementById("statEnabled"),
  statTakeover: document.getElementById("statTakeover"),
  statLive: document.getElementById("statLive"),
  statCooling: document.getElementById("statCooling"),
  statPending: document.getElementById("statPending"),
  channelList: document.getElementById("channelList"),
  sessionFilter: document.getElementById("sessionFilter"),
  sessionFilterNote: document.getElementById("sessionFilterNote"),
  emptyBoard: document.getElementById("emptyBoard"),
  emptyTitle: document.getElementById("emptyTitle"),
  emptyCopy: document.getElementById("emptyCopy"),
  detailStatus: document.getElementById("detailStatus"),
  btnRetryDetail: document.getElementById("btnRetryDetail"),
  board: document.getElementById("board"),
  roomView: document.getElementById("roomView"),
  roomId: document.getElementById("roomId"),
  roomMode: document.getElementById("roomMode"),
  badgeTakeover: document.getElementById("badgeTakeover"),
  badgeCool: document.getElementById("badgeCool"),
  badgeVibe: document.getElementById("badgeVibe"),
  meters: document.getElementById("meters"),
  rateBars: document.getElementById("rateBars"),
  traceMeta: document.getElementById("traceMeta"),
  pipeline: document.getElementById("pipeline"),
  nodeList: document.getElementById("nodeList"),
  dagCount: document.getElementById("dagCount"),
  coolMinutes: document.getElementById("coolMinutes"),
  btnCool: document.getElementById("btnCool"),
  btnReset: document.getElementById("btnReset"),
  btnRefresh: document.getElementById("btnRefresh"),
  opsPanel: document.getElementById("opsPanel"),
  opsNote: document.getElementById("opsNote"),
  shadowState: document.getElementById("shadowState"),
  privacyState: document.getElementById("privacyState"),
  providerState: document.getElementById("providerState"),
  metricState: document.getElementById("metricState"),
  statOccasion: document.getElementById("statOccasion"),
  statOccasionHint: document.getElementById("statOccasionHint"),
  statIntervene: document.getElementById("statIntervene"),
  statQuiet: document.getElementById("statQuiet"),
  statPresence: document.getElementById("statPresence"),
  statPartner: document.getElementById("statPartner"),
  statPartnerHint: document.getElementById("statPartnerHint"),
  whySilentState: document.getElementById("whySilentState"),
  mannersState: document.getElementById("mannersState"),
  mediaGateState: document.getElementById("mediaGateState"),
  mediaGateNote: document.getElementById("mediaGateNote"),
  presenceSelect: document.getElementById("presenceSelect"),
  btnPresence: document.getElementById("btnPresence"),
  presenceNote: document.getElementById("presenceNote"),
  notebookUmo: document.getElementById("notebookUmo"),
  btnNotebookLoad: document.getElementById("btnNotebookLoad"),
  notebookNote: document.getElementById("notebookNote"),
  notebookPreview: document.getElementById("notebookPreview"),
  onboarding: document.getElementById("onboarding"),
  shadowDecisions: document.getElementById("shadowDecisions"),
  shadowDecisionList: document.getElementById("shadowDecisionList"),
  presetSelect: document.getElementById("presetSelect"),
  btnPreset: document.getElementById("btnPreset"),
  presetNote: document.getElementById("presetNote"),
};

let bridge = window.AstrBotPluginPage || null;

async function wirePageNav(currentPage) {
  const mod = await import("./plugin_nav.js");
  mod.mountPluginSideNav(currentPage);
}


let overview = null;
let overviewOnline = false;
let selectedId = "";
let filterText = "";
let timer = null;
let lastDetail = null;
let pollTick = 0;
let quietPoll = false;
let lastOverviewFp = "";
let refreshInFlight = false;
let overviewRequestToken = 0;
let detailRequestToken = 0;
let operationRequestToken = 0;
let detailLoading = false;
let detailError = false;
let operationInFlight = false;
let operationKind = "";
let operationSessionId = "";
let presetsLoaded = false;
let presetsLoadInFlight = null;
let presetRetryPending = false;
const REQUEST_TIMEOUT_MS = 8000;

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

function fmtTime(date) {
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

function pct(value) {
  return `${Math.round((Number(value) || 0) * 100)}%`;
}

function setLink(ok, label) {
  els.linkLamp.classList.toggle("on", ok);
  els.linkLamp.classList.toggle("warn", !ok);
  els.linkLabel.textContent = label;
}

function setBusy(element, busy) {
  if (element) element.setAttribute("aria-busy", busy ? "true" : "false");
}

function sessionKey(row) {
  return String((row && (row.session_key || row.session_id)) || "");
}

function sessionIdentities(row) {
  return [row && row.session_key, row && row.session_id, row && row.group_id]
    .filter((value) => value !== undefined && value !== null && String(value) !== "")
    .map((value) => String(value));
}

function setFilterNote(message, isError = false) {
  els.sessionFilterNote.textContent = message || "";
  els.sessionFilterNote.classList.toggle("error", isError);
}

function setDetailStatus(message, isError = false, retry = false) {
  els.detailStatus.textContent = message || "";
  els.detailStatus.classList.toggle("error", isError);
  els.btnRetryDetail.classList.toggle("hidden", !retry);
  els.btnRetryDetail.disabled = !retry || detailLoading || refreshInFlight || operationInFlight;
}

function updateBusyState() {
  // Quiet polling must not mark the whole board busy — hosts often style aria-busy as a full flash.
  const blockUi = operationInFlight || (!quietPoll && detailLoading);
  setBusy(els.board, blockUi);
  setBusy(els.channelList, false);
  setBusy(els.opsPanel, operationInFlight);
}

function updateManagementControls() {
  const detailAvailable = overviewOnline && Boolean(selectedId) && Boolean(lastDetail) && !detailLoading && !detailError;
  const busy = refreshInFlight || operationInFlight;
  const canManage = detailAvailable && !busy;
  els.btnCool.disabled = !canManage;
  els.btnReset.disabled = !canManage;
  els.coolMinutes.disabled = !canManage;
  els.btnPreset.disabled = !overviewOnline || !presetsLoaded || Boolean(presetsLoadInFlight) || busy;
  els.presetSelect.disabled = !overviewOnline || !presetsLoaded || Boolean(presetsLoadInFlight) || busy;
  els.btnRetryDetail.disabled = !selectedId || detailLoading || busy;
  if (els.btnPresence) els.btnPresence.disabled = !overviewOnline || busy;
  if (els.presenceSelect) els.presenceSelect.disabled = !overviewOnline || busy;
  if (els.btnNotebookLoad) els.btnNotebookLoad.disabled = !overviewOnline || busy;
  if (els.notebookUmo) els.notebookUmo.disabled = !overviewOnline || busy;
  updateBusyState();
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


function overviewRowFingerprint(row) {
  if (!row || typeof row !== "object") return "";
  return [
    sessionKey(row),
    row.mode,
    row.mpm,
    row.pending,
    row.cooling,
    row.cooling_remaining,
    row.dag_nodes,
    row.sample_size,
    row.takeover,
    row.interaction_state,
    row.model_queue_depth,
    row.vibe_llm_in_flight,
    row.mode_source,
  ].map((value) => String(value ?? "")).join("|");
}


function overviewSnapshotFingerprint(data) {
  const safe = data && typeof data === "object" ? data : {};
  const sessions = Array.isArray(safe.sessions) ? safe.sessions : [];
  const sessionPart = sessions
    .map((row) => overviewRowFingerprint(row))
    .sort()
    .join("||");
  return [
    safe.enabled,
    safe.pipeline_mode,
    safe.takeover_all,
    safe.live_count,
    safe.cooling_count,
    safe.pending_count,
    safe.shadow_mode,
    safe.decision_mode,
    safe.persona_fallback,
    safe.agent_bridge,
    safe.console_show_message_content,
    safe.presence_knob,
    JSON.stringify(safe.provider_resolution || {}),
    JSON.stringify(safe.metrics || {}),
    JSON.stringify((safe.shadow_decisions || []).slice(-5)),
    JSON.stringify(safe.read_air || {}),
    JSON.stringify(safe.selflearning || {}),
    JSON.stringify(safe.social_manners || {}),
    JSON.stringify(safe.media_gate || {}),
    sessionPart,
  ].map((value) => String(value ?? "")).join("#");
}

function detailNeedsNetworkRefresh(prev, row) {
  if (!prev || !row) return true;
  // Metric-only churn (mpm/sample_size) should not blank+reload the room.
  const identityChanged =
    String(prev.session_key || prev.session_id || "") !== String(row.session_key || row.session_id || "") ||
    String(prev.group_id || "") !== String(row.group_id || "");
  if (identityChanged) return true;
  const heavy = [
    "cooling",
    "takeover",
    "mode",
    "mode_source",
    "interaction_state",
    "model_queue_depth",
    "vibe_llm_in_flight",
    "pending",
    "dag_nodes",
  ];
  return heavy.some((key) => String(prev[key] ?? "") !== String(row[key] ?? ""));
}

function patchRoomFromOverview(detail, row) {
  if (!detail || !row) return detail;
  return {
    ...detail,
    ...row,
    nodes: detail.nodes || [],
  };
}

function setHtmlIfChanged(element, html) {
  if (!element) return false;
  if (element.dataset.renderHtml === html) return false;
  element.innerHTML = html;
  element.dataset.renderHtml = html;
  return true;
}

function renderOverview(data) {
  const safeData = data && typeof data === "object" ? data : {};
  const groups = Array.isArray(safeData.takeover_groups) ? safeData.takeover_groups : [];
  const sessions = Array.isArray(safeData.sessions) ? safeData.sessions : [];
  overview = { ...safeData, takeover_groups: groups, sessions };
  overviewOnline = true;
  els.statEnabled.textContent = safeData.enabled === true ? "启用" : safeData.enabled === false ? "停用" : "—";
  const modeLabel = safeData.pipeline_mode === "exclusive" ? "独占" : safeData.pipeline_mode === "filter" ? "过滤" : "—";
  if (typeof safeData.takeover_all === "boolean") {
    els.statTakeover.textContent = safeData.takeover_all
      ? `${modeLabel} · 全部群`
      : groups.length
        ? `${modeLabel} · ${groups.length} 个白名单`
        : "未生效";
  } else {
    els.statTakeover.textContent = "—";
  }
  els.statLive.textContent = safeData.live_count === undefined || safeData.live_count === null ? "—" : String(safeData.live_count);
  els.statCooling.textContent = safeData.cooling_count === undefined || safeData.cooling_count === null ? "—" : String(safeData.cooling_count);
  els.statPending.textContent = safeData.pending_count === undefined || safeData.pending_count === null ? "—" : String(safeData.pending_count);
  if (typeof safeData.shadow_mode === "boolean") {
    els.shadowState.textContent = safeData.shadow_mode ? "观察模式 · 不产生副作用" : "正常运行";
  } else {
    els.shadowState.textContent = "—";
  }
  if (safeData.decision_mode === "persona_model") {
    els.shadowState.textContent += " · 人设模型决策";
    els.shadowState.title = `Agent: ${safeData.agent_bridge || "未知"}；不参与决策的旧选项：${(safeData.inactive_options || []).join("、")}`;
  } else if (safeData.persona_fallback) {
    els.shadowState.textContent += " · 人设不可用，已切换规则模式";
    els.shadowState.title = String(safeData.persona_fallback);
  } else {
    els.shadowState.title = "";
  }
  els.privacyState.textContent = typeof safeData.console_show_message_content === "boolean"
    ? safeData.console_show_message_content ? "控制台正文：显示" : "控制台正文：已脱敏"
    : "控制台正文：未知";
  const provider = safeData.provider_resolution || {};
  if (safeData.decision_mode === "persona_model") {
    els.providerState.textContent = `回复 Provider：${provider.reply || "当前 UMO"} · 决策：${provider.decision || provider.reply || "当前 UMO"}`;
  } else {
    els.providerState.textContent = `回复 Provider：${provider.reply || "当前 UMO"} · 氛围：${provider.vibe || "当前 UMO"}`;
  }
  const metrics = safeData.metrics || {};
  const warnings = Array.isArray(safeData.config_warnings) ? safeData.config_warnings : [];
  const shadowCount = Array.isArray(safeData.shadow_decisions) ? safeData.shadow_decisions.length : 0;
  const failureCount = Number(metrics.llm_reply_failed || 0) + Number(metrics.llm_vibe_failed || 0) + Number(metrics.send_failed || 0);
    els.metricState.textContent = `${warnings.length ? `配置提示 ${warnings.length} 条：${warnings[0]} · ` : ""}观察决策 ${shadowCount} 条 · 失败 ${failureCount} 次`;
  const readAir = safeData.read_air || {};
  const occasion = readAir.occasion || {};
  if (els.statOccasion) els.statOccasion.textContent = occasion.kind || "—";
  if (els.statOccasionHint) els.statOccasionHint.textContent = occasion.reason_zh || "读空气";
  if (els.statIntervene) els.statIntervene.textContent = readAir.intervene_count != null ? String(readAir.intervene_count) : "—";
  if (els.statQuiet) els.statQuiet.textContent = readAir.quiet_count != null ? String(readAir.quiet_count) : "—";
  const presenceMap = { ghost: "隐身", sensible: "懂事", lively: "活跃" };
  const presence = safeData.presence_knob || "sensible";
  if (els.statPresence) els.statPresence.textContent = presenceMap[presence] || presence;
  if (els.presenceSelect && document.activeElement !== els.presenceSelect) {
    els.presenceSelect.value = presenceMap[presence] ? presence : "sensible";
  }
  const partner = safeData.selflearning || {};
  renderIntegrations(safeData.selflearning);
  if (els.statPartner) els.statPartner.textContent = partner.lamp || partner.status || "—";
  if (els.statPartnerHint) {
    const details = {
      native_hooks: "由 AstrBot 原生钩子注入，召回取决于搭档配置",
      api_detected: "已发现直连接口，按能力调用",
      plugin_initializing: "搭档仍在初始化",
      plugin_found_no_api: "搭档已发现，但没有支持的接口或原生钩子",
      plugin_not_found: "未发现启用的记忆搭档",
      integration_disabled: "本插件桥接已关闭；不影响搭档自身钩子",
      native_hooks_bypassed: "当前宿主缺少请求钩子 API，请检查 AstrBot 版本",
    };
    const providers = Array.isArray(partner.providers) ? partner.providers : [];
    const names = providers.map(p => p.name).filter(Boolean).join(" / ");
    els.statPartnerHint.textContent = [names, details[partner.detail] || partner.detail || "selflearning"].filter(Boolean).join("：");
    els.statPartnerHint.title = providers.map(p => `${p.name}: ${p.detail || p.mode}; ${Object.values(p.errors || {}).join(", ")}`).join("\n");
  }
  const manners = safeData.social_manners || {};
  if (els.mannersState) {
    const flags = [
      manners.relay_baton === false ? "接力关" : "接力开",
      manners.private_field === false ? "私场关" : "私场开",
      manners.hyped_quota === false ? "捧场关" : "捧场开",
    ];
    els.mannersState.textContent = `社交分寸：${manners.enabled === false ? "总关" : flags.join(" · ")}`;
  }
  const mediaGate = safeData.media_gate || {};
  if (els.mediaGateState) {
    const mflags = [
      mediaGate.image === false ? "看图关" : "看图开",
      mediaGate.voice === false ? "听语音关" : "听语音开",
      mediaGate.understand_reply ? "理解接话开" : "理解接话关",
      mediaGate.privacy_strict === false ? "隐私松" : "隐私严",
    ];
    els.mediaGateState.textContent = `媒体门闩：${mflags.join(" · ")}`;
  }
  if (els.mediaGateNote) {
    const mm = mediaGate.multimodal || {};
    els.mediaGateNote.textContent = mm.detail
      ? `多模态：${mm.lamp || "—"} · ${mm.detail}`
      : "多模态：启发式门闩（无视觉/听写时仍可用）";
  }
  if (els.whySilentState) {
    const why = Array.isArray(readAir.why_silent) ? readAir.why_silent : [];
    els.whySilentState.textContent = why.length
      ? `最近为什么没回：${why.slice(0, 5).map((item) => item.reason_zh || item.reason_code || "").filter(Boolean).join("；")}`
      : "最近为什么没回：暂无记录";
  }
  if (els.btnPresence) els.btnPresence.disabled = !overviewOnline || Boolean(operationInFlight);
  if (els.presenceSelect) els.presenceSelect.disabled = !overviewOnline || Boolean(operationInFlight);
  if (els.btnNotebookLoad) els.btnNotebookLoad.disabled = !overviewOnline || Boolean(operationInFlight);
  const decisions = Array.isArray(safeData.shadow_decisions) ? safeData.shadow_decisions.slice(-10).reverse() : [];
  els.shadowDecisions.classList.toggle("hidden", decisions.length === 0);
  const shadowHtml = decisions
    .map((item) => {
      const state = item.state ? ` · ${escapeHtml(item.state)}` : "";
      return `<li><b>${escapeHtml(item.action || "未知")}</b>${state} · ${escapeHtml(item.reason || "")}</li>`;
    })
    .join("");
  setHtmlIfChanged(els.shadowDecisionList, shadowHtml);
  const hasScope = Boolean(safeData.takeover_all) || groups.length > 0;
  els.onboarding.classList.toggle("hidden", sessions.length > 0 || safeData.enabled !== true || hasScope);
  setFilterNote("");
  if (!selectedId) setDetailStatus("");
  renderChannels(sessions);
  updateManagementControls();
}

function renderOverviewUnavailable(message = "后端未响应") {
  renderIntegrations(null);
  overview = null;
  overviewOnline = false;
  detailRequestToken += 1;
  selectedId = "";
  detailLoading = false;
  detailError = false;
  lastDetail = null;
  [els.statEnabled, els.statTakeover, els.statLive, els.statCooling, els.statPending].forEach((element) => {
    element.textContent = "—";
  });
  els.shadowState.textContent = "连接中断";
  els.shadowState.title = "";
  els.privacyState.textContent = "控制台正文：未知";
  els.providerState.textContent = "Provider：未知";
  els.metricState.textContent = `${message} · 总览数据未知`;
  [els.statOccasion, els.statIntervene, els.statQuiet, els.statPresence, els.statPartner].forEach((el) => { if (el) el.textContent = "—"; });
  if (els.statOccasionHint) els.statOccasionHint.textContent = "读空气";
  if (els.statPartnerHint) els.statPartnerHint.textContent = "selflearning";
  if (els.whySilentState) els.whySilentState.textContent = "";
  if (els.mannersState) els.mannersState.textContent = "社交分寸：—";
  if (els.mediaGateState) els.mediaGateState.textContent = "媒体门闩：—";
  if (els.mediaGateNote) els.mediaGateNote.textContent = "";

  els.shadowDecisions.classList.add("hidden");
  els.onboarding.classList.add("hidden");
  renderChannels([]);
  const empty = els.channelList.querySelector(":scope > .empty");
  if (empty) empty.textContent = "连接中断，无法读取会话。";
  renderRoom(null, "连接已中断", "当前详情已清空，连接恢复后会自动重试。");
  setDetailStatus(message, true, false);
  setFilterNote("连接中断，暂时无法查找会话。", true);
  updateManagementControls();
}

function operationProgress(kind) {
  if (kind === "cool") return "正在开启深度冷却…";
  if (kind === "reset") return "正在重置会话状态…";
  return "正在应用预设…";
}

function setOperationLabel(kind, busy) {
  if (kind === "cool") {
    els.btnCool.textContent = busy ? "冷却中…" : "开启深度冷却";
    setBusy(els.btnCool, busy);
  }
  if (kind === "reset") {
    els.btnReset.textContent = busy ? "重置中…" : "重置本会话状态";
    setBusy(els.btnReset, busy);
  }
  if (kind === "preset") {
    els.btnPreset.textContent = busy ? "应用中…" : "应用";
    setBusy(els.btnPreset, busy);
    setBusy(els.presetSelect, busy);
  }
}

function beginOperation(kind, sessionId = "") {
  const token = ++operationRequestToken;
  overviewRequestToken += 1;
  detailRequestToken += 1;
  operationInFlight = true;
  operationKind = kind;
  operationSessionId = sessionId;
  if (sessionId) {
    detailLoading = true;
    detailError = false;
    lastDetail = null;
    renderRoom(null, "正在更新会话详情", operationProgress(kind));
    setDetailStatus(operationProgress(kind), false, false);
  }
  setOperationLabel(kind, true);
  updateManagementControls();
  return token;
}

function showOperationDetailError(message, err) {
  detailLoading = false;
  detailError = true;
  lastDetail = null;
  renderRoom(null, "操作未完成", "详情没有被旧数据覆盖。修复连接后可以重试读取。");
  const reason = err && err.message ? ` ${err.message}` : "";
  setDetailStatus(`${message}${reason}`, true, true);
  updateManagementControls();
}

function endOperation(token, kind) {
  if (token !== operationRequestToken) return;
  if (operationSessionId && selectedId === operationSessionId && detailLoading) {
    showOperationDetailError("操作完成但详情刷新未完成，请重试。", null);
  }
  operationInFlight = false;
  operationKind = "";
  operationSessionId = "";
  setOperationLabel(kind, false);
  updateManagementControls();
}

async function loadPresets() {
  if (presetsLoaded) return true;
  if (presetsLoadInFlight) return presetsLoadInFlight;
  presetsLoadInFlight = (async () => {
    try {
      const data = await apiGet("presets");
      const presets = data && data.presets ? data.presets : {};
      Array.from(els.presetSelect.options)
        .filter((option) => option.value)
        .forEach((option) => option.remove());
      Object.entries(presets).forEach(([name, info]) => {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = { observe: "观察模式", balanced: "平衡模式", active: "活跃群聊" }[name] || name;
        option.title = Object.keys((info && info.changes) || {}).join(", ");
        els.presetSelect.appendChild(option);
      });
      presetsLoaded = true;
      presetRetryPending = false;
      els.presetNote.textContent = "";
      return true;
    } catch (err) {
      presetRetryPending = true;
      els.presetNote.textContent = (err && err.message) || "预设读取失败";
      return false;
    } finally {
      presetsLoadInFlight = null;
      updateManagementControls();
    }
  })();
  return presetsLoadInFlight;
}

async function applyPreset() {
  const name = els.presetSelect.value;
  if (!name || !overviewOnline || !presetsLoaded || operationInFlight) return;
  if (!window.confirm("应用此预设并覆盖对应运行参数？")) return;
  const sessionId = selectedId;
  const token = beginOperation("preset", sessionId);
  setOps("正在应用预设…");
  try {
    const result = await apiPost("preset/apply", { name, confirm: true });
    if (token !== operationRequestToken) return;
    els.presetNote.textContent = result && result.saved === false ? "预设已应用（配置对象未提供持久化接口）" : "预设已应用";
    await refresh({ refreshDetail: true, operationToken: token });
    if (token === operationRequestToken && overviewOnline) setOps("预设已应用。");
  } catch (err) {
    if (token === operationRequestToken) {
      els.presetNote.textContent = (err && err.message) || "预设应用失败";
      setOps((err && err.message) || "预设应用失败。", true);
      if (sessionId && selectedId === sessionId) showOperationDetailError("预设应用失败，详情可重试。", err);
    }
  } finally {
    if (token === operationRequestToken) endOperation(token, "preset");
  }
}

function renderChannels(sessions) {
  const q = filterText.trim().toLowerCase();
  const rows = sessions.filter((row) => {
    if (!q) return true;
    const haystack = [row.session_key, row.session_id, row.group_id]
      .map((value) => String(value || "").toLowerCase())
      .join(" ");
    return haystack.includes(q);
  });
  if (!rows.length) {
    const message = q ? "没有匹配的会话。" : "尚无会话。请先在配置中让过滤器对群聊生效。";
    const existingEmpty = els.channelList.querySelector(":scope > .empty");
    if (els.channelList.children.length !== 1 || !existingEmpty) {
      const item = document.createElement("li");
      item.className = "empty";
      item.textContent = message;
      els.channelList.replaceChildren(item);
    } else {
      existingEmpty.textContent = message;
    }
    return;
  }

  const existing = new Map(
    Array.from(els.channelList.querySelectorAll(":scope > li > .channel[data-session-key]")).map((button) => [
      button.dataset.sessionKey,
      button.parentElement,
    ]),
  );
  rows.forEach((row, index) => {
    const sessionKey = String(row.session_key || row.session_id || "");
    let li = existing.get(sessionKey);
    let btn;
    if (!li) {
      li = document.createElement("li");
      btn = document.createElement("button");
      btn.type = "button";
      btn.dataset.sessionKey = sessionKey;
      btn.addEventListener("click", () => selectSession(btn.dataset.sessionKey));
      li.appendChild(btn);
    } else {
      btn = li.querySelector(".channel");
    }
    btn.className = `channel${sessionKey === selectedId ? " active" : ""}`;
    btn.setAttribute("aria-pressed", sessionKey === selectedId ? "true" : "false");
    btn.title = sessionKey;
    btn.setAttribute("aria-label", `会话 ${String(row.group_id || row.session_id || "未知")}, UMO ${sessionKey}`);
    const channelHtml = `
      <span class="id">${escapeHtml(row.group_id || row.session_id)}</span>
      <span class="session-key">${escapeHtml(sessionKey)}</span>
      <span class="meta">
        <span class="mode-${cssMode(row.mode)}">${escapeHtml(MODE_LABEL[row.mode] || row.mode || "未知")}</span>
        <span>${Number(row.mpm || 0).toFixed(1)} MPM</span>
      </span>
    `;
    setHtmlIfChanged(btn, channelHtml);
    const current = els.channelList.children[index];
    if (current !== li) els.channelList.insertBefore(li, current || null);
    existing.delete(sessionKey);
  });
  existing.forEach((li) => li.remove());
  els.channelList.querySelectorAll(":scope > .empty").forEach((item) => item.remove());
}

function cssMode(mode) {
  if (mode === "fast_banter") return "fast";
  if (mode === "serious_inquiry") return "serious";
  return "chill";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function meter(label, value, ratio) {
  const width = Math.max(0, Math.min(100, Math.round((ratio || 0) * 100)));
  return `<article class="meter" role="meter" aria-label="${escapeHtml(label)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${width}" aria-valuetext="${escapeHtml(value)}"><span class="stat-label">${escapeHtml(label)}</span><b>${escapeHtml(value)}</b><div class="gauge" aria-hidden="true"><i style="width:${width}%"></i></div></article>`;
}

function renderRoom(
  detail,
  emptyTitle = "没有选中的会话",
  emptyCopy = "从左侧点选一个 UMO 会话，或确认已开启「对全部群聊生效」/填写白名单。",
) {
  if (!detail) {
    els.traceMeta.title = "";
    els.emptyTitle.textContent = emptyTitle;
    els.emptyCopy.textContent = emptyCopy;
    els.emptyBoard.classList.remove("hidden");
    els.roomView.classList.add("hidden");
    return;
  }
  els.emptyBoard.classList.add("hidden");
  els.roomView.classList.remove("hidden");
  els.traceMeta.title = "";
  const groupId = String(detail.group_id || detail.session_id || "");
  const sessionKey = String(detail.session_key || detail.session_id || "");
  els.roomId.textContent = groupId && groupId !== sessionKey ? `${groupId} · ${sessionKey}` : sessionKey;
  els.roomMode.textContent = MODE_LABEL[detail.mode] || String(detail.mode || "未知");
  els.roomMode.className = `mode-${cssMode(detail.mode)}`;
  els.badgeTakeover.textContent = detail.takeover ? "已生效" : "未生效";
  els.badgeTakeover.classList.toggle("hot", Boolean(detail.takeover));
  els.badgeCool.textContent = detail.cooling
    ? `冷却 ${Math.ceil((detail.cooling_remaining || 0) / 60)} 分`
    : "冷却关";
  els.badgeCool.classList.toggle("cool", Boolean(detail.cooling));
  const llmCount = Number(detail.vibe_llm_snapshot_count || 0);
  const llmAge = Number(detail.vibe_llm_snapshot_age);
  if (detail.vibe_llm_in_flight) {
    els.badgeVibe.textContent = "LLM 校准中";
  } else if (detail.mode_source === "llm") {
    els.badgeVibe.textContent = `LLM 校准 · ${llmCount} 次`;
  } else {
    els.badgeVibe.textContent = llmCount ? `本地遥测 · 已校准 ${llmCount} 次` : "本地遥测";
  }
  els.badgeVibe.title = Number.isFinite(llmAge) ? `最近一次 LLM 校准：${Math.ceil(llmAge)} 秒前` : "尚未调用 LLM 校准";

  const metersHtml = [
    meter("MPM", Number(detail.mpm || 0).toFixed(1), Math.min(1, (detail.mpm || 0) / 20)),
    meter("平均字符", Number(detail.token_density || 0).toFixed(1), Math.min(1, (detail.token_density || 0) / 40)),
    meter("发言人数", String(detail.unique_speakers || 0), Math.min(1, (detail.unique_speakers || 0) / 6)),
    meter("Emoji 消息", pct(detail.unicode_emoji_ratio), detail.unicode_emoji_ratio),
    meter("媒体消息", pct(detail.media_ratio), detail.media_ratio),
    meter("标点规范", pct(detail.punctuation_formality), detail.punctuation_formality),
  ].join("");
  setHtmlIfChanged(els.meters, metersHtml);

  const series = (Array.isArray(detail.rate_series) ? detail.rate_series : [])
    .slice(-12)
    .map((value) => Number(value))
    .map((value) => (Number.isFinite(value) ? Math.max(0, value) : 0));
  const max = Math.max(1, ...series);
  els.rateBars.setAttribute("aria-label", `最近 60 秒消息速率：${series.map((value) => value.toFixed(1)).join("，")} MPM`);
  const barsHtml = series
    .map((n) => `<span aria-hidden="true" style="height:${Math.min(100, Math.max(6, (n / max) * 100))}%"></span>`)
    .join("");
  if (setHtmlIfChanged(els.rateBars, barsHtml)) {
    els.rateBars.classList.add("bars--animate");
    window.setTimeout(() => els.rateBars.classList.remove("bars--animate"), 450);
  }
  const labels = [...(detail.scene_tags || []), ...(detail.emotion_tags || [])];
  els.traceMeta.textContent = `${Number(detail.mpm || 0).toFixed(1)} MPM · ${detail.sample_size || 0} 条${labels.length ? ` · ${labels.join(" / ")}` : ""}`;
  if (detail.decision_mode === "persona_model") {
    const decision = detail.model_decision || {};
    els.traceMeta.textContent = `人设状态 ${decision.state || detail.interaction_state} · ${decision.action || "等待决策"} · ${decision.reason_code || ""} · ${decision.latency_ms || 0}ms · 排队 ${detail.model_queue_depth || 0}${decision.shadow ? " · 仅观察" : ""}`;
    els.traceMeta.title = `回应消息：${(decision.target_message_ids || []).join("、")}`;
  }
  els.dagCount.textContent = `${detail.dag_nodes || 0} 节点`;

  els.pipeline.querySelectorAll("li").forEach((item) => {
    const stage = item.getAttribute("data-stage");
    const live =
      (stage === "vibe" && detail.sample_size > 0) ||
      (stage === "graph" && detail.dag_nodes > 0) ||
      (stage === "debounce" && detail.pending > 0) ||
      (stage === "arbiter" && detail.cooling) ||
      (stage === "pacer" && Boolean(detail.last_bot_text));
    item.classList.toggle("live", live);
    const stateLabel = live ? "活跃" : "待命";
    const stageName = item.querySelector(".stage-name")?.textContent || stage || "阶段";
    item.setAttribute("aria-label", `${stageName}：${stateLabel}`);
    const state = item.querySelector(".stage-state");
    if (state) {
      state.textContent = stateLabel;
      state.setAttribute("aria-label", `${stageName}：${stateLabel}`);
      state.title = `${stageName}：${stateLabel}`;
    }
  });

  if (detail.content_redacted) {
    setHtmlIfChanged(els.nodeList, `<li class="empty">正文已脱敏。可在插件配置中临时开启“控制台显示消息正文”。</li>`);
    return;
  }
  const nodes = detail.nodes || [];
  if (!nodes.length) {
    setHtmlIfChanged(els.nodeList, `<li class="empty">图谱还是空的。群消息流入并完成防抖后会出现在这里。</li>`);
    return;
  }
  const nodesHtml = nodes
    .slice()
    .reverse()
    .map((node) => {
      const who = node.is_bot ? "BOT" : `USER ${escapeHtml(node.user_id)}`;
      const parents = Array.isArray(node.parent_ids) ? node.parent_ids : [];
      const reply = parents.length ? ` · parent ${parents.map(escapeHtml).join(", ")}` : "";
      const thread = node.thread_id ? ` · thread ${escapeHtml(String(node.thread_id).slice(0, 12))}` : "";
      return `<li class="${node.is_bot ? "bot" : ""}"><div class="who">${who}${reply}${thread}</div><div>${escapeHtml(node.text || "")}</div></li>`;
    })
    .join("");
  setHtmlIfChanged(els.nodeList, nodesHtml);
}

function detailBelongsToSession(detail, requestedId) {
  if (!detail || typeof detail !== "object") return false;
  const row = (overview && Array.isArray(overview.sessions) ? overview.sessions : [])
    .find((item) => sessionKey(item) === requestedId);
  const expected = new Set([requestedId, ...sessionIdentities(row)]);
  if (detail.session_key !== undefined && detail.session_key !== null && String(detail.session_key) !== "") {
    return expected.has(String(detail.session_key));
  }
  if (detail.session_id !== undefined && detail.session_id !== null && String(detail.session_id) !== "") {
    return expected.has(String(detail.session_id));
  }
  if (detail.group_id !== undefined && detail.group_id !== null && String(detail.group_id) !== "") {
    return expected.has(String(detail.group_id));
  }
  return false;
}

async function selectSession(sessionId, { silent = false } = {}) {
  const requestedId = String(sessionId || "");
  if (!requestedId || !overviewOnline) return;
  const sessions = overview && Array.isArray(overview.sessions) ? overview.sessions : [];
  if (!sessions.some((row) => sessionKey(row) === requestedId)) return;
  const sameSelection = selectedId === requestedId && Boolean(lastDetail) && !detailError;
  selectedId = requestedId;
  const requestToken = ++detailRequestToken;
  detailLoading = true;
  detailError = false;
  if (!silent || !sameSelection) {
    lastDetail = null;
    renderChannels(sessions);
    renderRoom(null, "正在加载会话详情", `正在加载 ${requestedId}…`);
    setDetailStatus("正在加载会话详情…", false, false);
  } else {
    renderChannels(sessions);
    setDetailStatus(silent ? "" : "正在更新会话详情…", false, false);
  }
  updateManagementControls();
  try {
    const detail = await apiGet("session", { session_key: requestedId });
    if (requestToken !== detailRequestToken || selectedId !== requestedId || !overviewOnline) return;
    if (!detailBelongsToSession(detail, requestedId)) throw new Error("会话详情与当前选择不匹配");
    detailLoading = false;
    detailError = false;
    lastDetail = detail;
    renderRoom(detail);
    setDetailStatus("");
    if (els.opsNote.dataset.source === "detail") setOps("");
    updateManagementControls();
  } catch (err) {
    if (requestToken !== detailRequestToken || selectedId !== requestedId || !overviewOnline) return;
    console.warn("[ChatDynamics] session detail failed", err);
    detailLoading = false;
    detailError = true;
    if (!silent || !sameSelection) {
      lastDetail = null;
      renderRoom(null, "无法加载会话详情", "当前详情已清空，请检查 session_key 或后端连接后重试。");
    }
    setDetailStatus(`详情加载失败：${(err && err.message) || "会话详情请求失败"}`, true, true);
    setOps((err && err.message) || "会话详情请求失败", true, "detail");
    updateManagementControls();
  }
}

async function refresh({ refreshDetail = true, operationToken = null, quiet = false } = {}) {
  if (document.hidden || refreshInFlight || (operationInFlight && operationToken === null)) return;
  quietPoll = Boolean(quiet) && operationToken === null;
  refreshInFlight = true;
  if (!quietPoll) {
    setBusy(els.btnRefresh, true);
    els.btnRefresh.disabled = true;
    els.btnRefresh.textContent = "刷新中…";
  }
  updateManagementControls();
  const requestToken = ++overviewRequestToken;
  try {
    const data = await apiGet("overview");
    if (requestToken !== overviewRequestToken) return;
    if (operationToken !== null && operationToken !== operationRequestToken) return;
    if (!data) {
      setLink(false, "后端未响应");
      renderOverviewUnavailable("后端未响应");
      lastOverviewFp = "";
      return;
    }
    setLink(true, "基站在线");
    const nextFp = overviewSnapshotFingerprint(data);
    const overviewChanged = nextFp !== lastOverviewFp;
    lastOverviewFp = nextFp;
    if (overviewChanged || !overview) {
      renderOverview(data);
    } else {
      overview = { ...overview, ...(data && typeof data === "object" ? data : {}), sessions: Array.isArray(data.sessions) ? data.sessions : overview.sessions };
    }
    if (presetRetryPending) void loadPresets();
    if (selectedId) {
      const sessions = Array.isArray(data.sessions) ? data.sessions : [];
      const still = sessions.some((row) => sessionKey(row) === selectedId);
      if (still) {
        const row = sessions.find((item) => sessionKey(item) === selectedId);
        if (refreshDetail || !lastDetail) {
          await selectSession(selectedId, { silent: quietPoll && Boolean(lastDetail) });
          if (requestToken !== overviewRequestToken || (operationToken !== null && operationToken !== operationRequestToken)) return;
        } else if (row && detailNeedsNetworkRefresh(lastDetail, row)) {
          // Structural change only — keep the current room painted while fetching.
          await selectSession(selectedId, { silent: true });
          if (requestToken !== overviewRequestToken || (operationToken !== null && operationToken !== operationRequestToken)) return;
        } else {
          lastDetail = patchRoomFromOverview(lastDetail, row || {});
          renderRoom(lastDetail);
          if (overviewChanged) renderChannels(sessions);
        }
      } else {
        selectedId = "";
        detailRequestToken += 1;
        detailLoading = false;
        detailError = false;
        lastDetail = null;
        renderRoom(null);
        setDetailStatus("");
        updateManagementControls();
      }
    } else if (Array.isArray(data.sessions) && data.sessions.length === 1) {
      await selectSession(sessionKey(data.sessions[0]), { silent: false });
    }
  } catch (err) {
    if (requestToken !== overviewRequestToken || (operationToken !== null && operationToken !== operationRequestToken)) return;
    console.warn("[ChatDynamics] overview failed", err);
    setLink(false, (err && err.message) || "后端未响应");
    renderOverviewUnavailable((err && err.message) || "后端未响应");
    lastOverviewFp = "";
  } finally {
    refreshInFlight = false;
    quietPoll = false;
    if (!document.hidden) {
      setBusy(els.btnRefresh, false);
      els.btnRefresh.disabled = false;
      els.btnRefresh.textContent = "刷新";
    }
    updateManagementControls();
  }
}

function stopPolling() {
  if (timer) {
    window.clearTimeout(timer);
    timer = null;
  }
}

function startPolling() {
  stopPolling();
  if (document.hidden) return;
  timer = window.setTimeout(async () => {
    timer = null;
    pollTick += 1;
    // Quiet overview every 5s; full detail fetch at most every 30s (and only silently).
    await refresh({ refreshDetail: pollTick % 6 === 0, quiet: true });
    if (!document.hidden) startPolling();
  }, 5000);
}

function setOps(message, isError = false, source = "operation") {
  els.opsNote.textContent = message;
  els.opsNote.classList.toggle("error", isError);
  els.opsNote.dataset.source = message ? source : "";
}

async function coolSelected() {
  if (!selectedId || !overviewOnline || !lastDetail || detailLoading || operationInFlight) return;
  const minutes = Number(els.coolMinutes.value || 15);
  if (!Number.isFinite(minutes) || minutes < 1 || minutes > 180) {
    els.coolMinutes.setAttribute("aria-invalid", "true");
    setOps("冷却分钟必须在 1 到 180 之间。", true);
    els.coolMinutes.focus();
    return;
  }
  els.coolMinutes.removeAttribute("aria-invalid");
  const sessionId = selectedId;
  const token = beginOperation("cool", sessionId);
  setOps("正在开启深度冷却…");
  try {
    await apiPost("cool", { session_key: sessionId, minutes });
    if (token !== operationRequestToken) return;
    if (selectedId === sessionId) {
      setOps(`已开启 ${minutes} 分钟深度冷却。`);
    }
    await refresh({ refreshDetail: true, operationToken: token });
  } catch (err) {
    if (token === operationRequestToken && selectedId === sessionId) {
      setOps((err && err.message) || "冷却请求失败。", true);
      showOperationDetailError("冷却失败，详情可重试。", err);
    }
  } finally {
    endOperation(token, "cool");
  }
}

async function resetSelected() {
  if (!selectedId || !overviewOnline || !lastDetail || detailLoading || operationInFlight) return;
  const sessionId = selectedId;
  if (!window.confirm(`重置会话 ${sessionId} 的图谱、遥测与冷却？`)) return;
  const token = beginOperation("reset", sessionId);
  setOps("正在重置会话状态…");
  try {
    await apiPost("reset", { session_key: sessionId });
    if (token !== operationRequestToken) return;
    if (selectedId === sessionId) setOps("本会话状态已清空。");
    await refresh({ refreshDetail: true, operationToken: token });
  } catch (err) {
    if (token === operationRequestToken && selectedId === sessionId) {
      setOps((err && err.message) || "重置失败。", true);
      showOperationDetailError("重置失败，详情可重试。", err);
    }
  } finally {
    endOperation(token, "reset");
  }
}


async function boot() {
  mountWorkspace();
  els.pageTitle.textContent = t("pages.console.title", "群聊动态控制台");
  els.pageDesc.textContent = t("pages.console.desc", "看见每一场对话，掌握机器人的参与节奏。");
  await wirePageNav("console");
  if (bridge && typeof bridge.ready === "function") {
    try {
      await withTimeout(bridge.ready());
      setLink(true, "Bridge 已连接");
    } catch (_err) {
      setLink(false, "Bridge 超时");
    }
  } else {
    setLink(false, "直连 / 本地预览");
  }
  els.clock.textContent = fmtTime(new Date());
  setInterval(() => {
    els.clock.textContent = fmtTime(new Date());
  }, 1000);

  els.btnRefresh.addEventListener("click", async () => {
    await refresh();
    startPolling();
  });
  els.btnCool.addEventListener("click", coolSelected);
  els.btnReset.addEventListener("click", resetSelected);
  els.btnPreset.addEventListener("click", applyPreset);

if (els.btnPresence) els.btnPresence.addEventListener("click", () => { applyPresenceKnob(); });
if (els.btnNotebookLoad) els.btnNotebookLoad.addEventListener("click", () => { loadNotebookLite(); });
  els.btnRetryDetail.addEventListener("click", () => {
    if (selectedId) void selectSession(selectedId);
  });
  els.sessionFilter.addEventListener("input", () => {
    filterText = els.sessionFilter.value || "";
    els.sessionFilter.removeAttribute("aria-invalid");
    setFilterNote("");
    renderChannels((overview && overview.sessions) || []);
  });
  els.sessionFilter.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      const typed = (els.sessionFilter.value || "").trim();
      if (!typed) return;
      const sessions = overview && Array.isArray(overview.sessions) ? overview.sessions : [];
      const exactUmo = sessions.filter((row) => String(row.session_key || "") === typed);
      if (exactUmo.length === 1) {
        setFilterNote("");
        void selectSession(sessionKey(exactUmo[0]));
        return;
      }
      if (exactUmo.length > 1) {
        setFilterNote("完整 UMO 匹配多个会话，请从列表中选择。", true);
        els.sessionFilter.setAttribute("aria-invalid", "true");
        return;
      }
      const exactGroup = sessions.filter((row) => String(row.group_id || "") === typed);
      if (exactGroup.length === 1) {
        setFilterNote("");
        void selectSession(sessionKey(exactGroup[0]));
      } else if (exactGroup.length > 1) {
        setFilterNote("群 ID 匹配多个会话，请输入完整 UMO。", true);
        els.sessionFilter.setAttribute("aria-invalid", "true");
      } else {
        setFilterNote("未在已加载会话中找到匹配项，请从列表选择或输入完整 UMO。", true);
        els.sessionFilter.setAttribute("aria-invalid", "true");
      }
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopPolling();
    } else {
      refresh({ quiet: true });
      startPolling();
    }
  });

  updateManagementControls();
  await refresh();
  await loadPresets();
  startPolling();
}

boot();


async function applyPresenceKnob() {
  if (!els.presenceSelect || !overviewOnline || operationInFlight) return;
  const value = els.presenceSelect.value;
  if (!["ghost", "sensible", "lively"].includes(value)) return;
  operationInFlight = true;
  updateManagementControls();
  if (els.presenceNote) els.presenceNote.textContent = "正在应用分寸旋钮…";
  try {
    await apiPost("config", { config: { presence_knob: value } });
    if (els.presenceNote) els.presenceNote.textContent = "分寸旋钮已保存。";
    await refresh({ quiet: false });
  } catch (err) {
    if (els.presenceNote) els.presenceNote.textContent = (err && err.message) || "保存失败";
  } finally {
    operationInFlight = false;
    updateManagementControls();
  }
}

async function loadNotebookLite() {
  if (!els.notebookUmo || !overviewOnline || operationInFlight) return;
  const umo = String(els.notebookUmo.value || "").trim();
  if (!umo) {
    if (els.notebookNote) els.notebookNote.textContent = "请填写 UMO / session_key";
    return;
  }
  operationInFlight = true;
  updateManagementControls();
  if (els.notebookPreview) els.notebookPreview.textContent = "";
  if (els.notebookNote) els.notebookNote.textContent = "正在读取小本…";
  try {
    const data = await apiGet("notebook", { umo });
    if (els.notebookPreview) {
      const ann = (data && data.anniversaries) || [];
      const rem = (data && data.reminders) || [];
      const slang = (data && data.slang_trials) || [];
      els.notebookPreview.textContent = [
        `纪念日 ${ann.length} 条`,
        ...ann.slice(0, 5).map((a) => `· ${a.month}/${a.day} ${a.title || ""}`),
        `约定 ${rem.length} 条`,
        ...rem.slice(0, 5).map((r) => `· ${r.text || ""}`),
        `黑话试用 ${slang.length} 条`,
        ...slang.slice(0, 5).map((s) => `· ${s.phrase || ""}`),
      ].join("\n");
    }
    if (els.notebookNote) els.notebookNote.textContent = "已加载（默认不展示隐私原文）。";
  } catch (err) {
    if (els.notebookNote) els.notebookNote.textContent = (err && err.message) || "读取失败";
    if (els.notebookPreview) els.notebookPreview.textContent = "";
  } finally {
    operationInFlight = false;
    updateManagementControls();
  }
}

