/** Shared AstrBot Plugin Page bridge helpers for Chat Dynamics wireframe pages. */
export const PLUGIN = "astrbot_plugin_chat_dynamics";
export const REQUEST_TIMEOUT_MS = 8000;

export const PRESENCE_LABEL = {
  ghost: "隐身",
  sensible: "懂事",
  lively: "活跃",
};

export const PRESENCE_PREVIEW = {
  ghost: "尽量少开口，没点名就旁听。",
  sensible: "懂场合再接话，默认推荐。",
  lively: "更愿意轻接整活与闲聊。",
};

export const OCCASION_LABEL = {
  neutral: "普通闲聊",
  banter: "整活",
  serious_help: "认真求助",
  vent: "倾诉",
  conflict: "冲突/降温",
  deciding: "决策中",
  chill_fade: "冷场衰退",
  fast_banter: "快速碎梗",
  serious_inquiry: "严肃探讨",
};

let bridge = window.AstrBotPluginPage || null;

export function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function redactId(value) {
  const text = String(value || "");
  if (!text) return "—";
  if (text.length <= 6) return "*".repeat(text.length);
  return `${text.slice(0, 3)}…${text.slice(-2)}`;
}

export function unwrap(payload) {
  if (!payload || typeof payload !== "object") {
    throw new Error("空响应");
  }
  if (payload.status === "error" || payload.ok === false) {
    throw new Error(payload.error || payload.message || "请求失败");
  }
  return payload.data !== undefined ? payload.data : payload;
}

export function withTimeout(promise, timeoutMs = REQUEST_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("请求超时")), timeoutMs);
    Promise.resolve(promise).then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err) => {
        clearTimeout(timer);
        reject(err);
      }
    );
  });
}

export async function apiGet(endpoint, params = {}) {
  if (!bridge && window.AstrBotPluginPage) bridge = window.AstrBotPluginPage;
  if (bridge && typeof bridge.apiGet === "function") {
    return unwrap(await withTimeout(bridge.apiGet(endpoint, params)));
  }
  throw new Error("Plugin Page bridge 不可用");
}

export async function apiPost(endpoint, body = {}) {
  if (!bridge && window.AstrBotPluginPage) bridge = window.AstrBotPluginPage;
  if (bridge && typeof bridge.apiPost === "function") {
    return unwrap(await withTimeout(bridge.apiPost(endpoint, body)));
  }
  throw new Error("Plugin Page bridge 不可用");
}

export async function readyBridge() {
  if (!bridge && window.AstrBotPluginPage) bridge = window.AstrBotPluginPage;
  if (bridge && typeof bridge.ready === "function") {
    await withTimeout(bridge.ready());
  }
  return bridge;
}

export function setLink(els, ok, label) {
  if (!els || !els.linkLamp || !els.linkLabel) return;
  els.linkLamp.classList.toggle("on", Boolean(ok));
  els.linkLamp.classList.toggle("warn", !ok);
  els.linkLabel.textContent = label;
}

export function formatTs(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return "";
  try {
    return new Date(n * (n > 1e12 ? 1 : 1000)).toLocaleString("zh-CN", {
      hour12: false,
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}

export function sessionKey(row) {
  if (!row || typeof row !== "object") return "";
  return String(row.session_key || row.umo || row.session_id || "");
}

export function storageGet(key, fallback = "") {
  try {
    return localStorage.getItem(key) || fallback;
  } catch {
    return fallback;
  }
}

export function storageSet(key, value) {
  try {
    localStorage.setItem(key, String(value ?? ""));
  } catch {
    /* ignore */
  }
}
