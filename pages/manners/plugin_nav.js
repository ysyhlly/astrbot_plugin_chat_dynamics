/** Unified plugin side nav — byte-copied into each pages/<name>/ (page-local copy only; never import parent shared). */

export const PLUGIN_PAGES = [
  { id: "console", label: "群聊动态控制台" },
  { id: "config", label: "插件参数配置" },
  { id: "today", label: "今日读空气" },
  { id: "manners", label: "分寸台" },
  { id: "memory", label: "记忆小本" },
  { id: "replay", label: "场景回放" },
];

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function currentTheme() {
  return new URLSearchParams(window.location.search || "").get("theme") || "";
}

export function resolvedUi() {
  // theme.js owns chat_dynamics_ui and account persistence on all six pages.
  return window.ChatDynamicsTheme?.current() || "day";
}

export function applyUi(ui) {
  return window.ChatDynamicsTheme?.apply(ui);
}

function mountThemeToggle() {
  const side = document.querySelector(".plugin-side");
  if (!side || document.getElementById("btnUiTheme")) {
    void window.ChatDynamicsTheme?.start();
    return;
  }
  const btn = document.createElement("button");
  btn.type = "button";
  btn.id = "btnUiTheme";
  btn.className = "plugin-theme-toggle";
  btn.setAttribute("aria-label", "切换日间或夜间界面");
  btn.innerHTML = '<span class="ui-theme-icon" aria-hidden="true">◐</span><span class="ui-theme-label">日间模式</span><span class="ui-theme-switch" aria-hidden="true"></span>';
  btn.addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-ui-theme") === "night" ? "night" : "day";
    applyUi(current === "night" ? "day" : "night");
  });
  side.appendChild(btn);
  const status = document.createElement("p");
  status.id = "uiThemeStatus";
  status.className = "ui-theme-status";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  side.appendChild(status);
  const retry = document.createElement("button");
  retry.id = "btnUiRetry";
  retry.type = "button";
  retry.className = "ui-theme-retry";
  retry.textContent = "重试保存";
  retry.hidden = true;
  retry.addEventListener("click", () => { void applyUi(resolvedUi()); });
  side.appendChild(retry);
  void window.ChatDynamicsTheme?.start();
}

export function extractContentPath(raw) {
  if (typeof raw === "string") return raw.trim();
  const payload = raw && typeof raw === "object" ? raw : null;
  if (!payload) return "";
  const nested =
    payload.data && typeof payload.data === "object" ? payload.data : null;
  return String(
    (nested && nested.content_path) || payload.content_path || ""
  ).trim();
}

export function signedContentHref(contentPath, theme, baseHref) {
  const raw = String(contentPath || "").trim();
  if (!raw) return "";

  const applyTheme = (params) => {
    if (theme) params.set("theme", theme);
    params.set("ui", resolvedUi());
    return params;
  };

  let href = "";
  try {
    // Sandboxed plugin iframes have an opaque origin, so location.origin is
    // the string "null" and cannot be used as a URL base. location.href still
    // holds the real content URL.
    const base = String(baseHref || window.location.href || "").trim();
    const url = base && base !== "null" ? new URL(raw, base) : new URL(raw);
    applyTheme(url.searchParams);
    href = url.pathname + url.search + url.hash;
  } catch {
    if (raw.startsWith("/") && !raw.startsWith("//")) {
      const hashIdx = raw.indexOf("#");
      const withoutHash = hashIdx >= 0 ? raw.slice(0, hashIdx) : raw;
      const hash = hashIdx >= 0 ? raw.slice(hashIdx) : "";
      const qIdx = withoutHash.indexOf("?");
      const path = qIdx >= 0 ? withoutHash.slice(0, qIdx) : withoutHash;
      const query = qIdx >= 0 ? withoutHash.slice(qIdx + 1) : "";
      const params = new URLSearchParams(query);
      applyTheme(params);
      const q = params.toString();
      href = path + (q ? `?${q}` : "") + hash;
    }
  }
  if (!href || !/[?&]asset_token=/.test(href)) return "";
  return href;
}

export function siblingContentUrl(pageName) {
  // Diagnostic helper only. Live navigation must not reuse this URL:
  // page-scoped asset_token cannot be copied, and a tokenless path is 401.
  const path = String(window.location.pathname || "");
  const parts = path.replace(/\/+$/, "").split("/");
  const contentIdx = parts.indexOf("content");
  let nextPath;
  if (contentIdx >= 0 && parts.length >= contentIdx + 3) {
    parts[contentIdx + 2] = pageName;
    nextPath = `${parts.join("/")}/`;
  } else {
    nextPath = `../${encodeURIComponent(pageName)}/`;
  }
  const theme = currentTheme();
  const qs = new URLSearchParams();
  if (theme) qs.set("theme", theme);
  const q = qs.toString();
  return nextPath + (q ? `?${q}` : "");
}

export async function navigateToPluginPage(pageName) {
  const theme = currentTheme();
  try {
    let bridge = window.AstrBotPluginPage || null;
    if (bridge && typeof bridge.ready === "function") {
      try {
        await bridge.ready();
      } catch {
        /* ready is optional; apiGet may still work */
      }
    }
    if (!bridge && window.AstrBotPluginPage) bridge = window.AstrBotPluginPage;
    if (bridge && typeof bridge.apiGet === "function") {
      const params = { page: pageName };
      if (theme) params.theme = theme;
      const raw = await bridge.apiGet("page_nav", params);
      await window.ChatDynamicsTheme?.flush();
      const signed = signedContentHref(extractContentPath(raw), theme);
      if (signed) {
        window.location.assign(signed);
        return;
      }
    }
  } catch (err) {
    console.warn("[chat_dynamics] page_nav failed", err);
  }
  // Never assign a tokenless sibling URL. AstrBot answers that with 未授权.
  console.warn("[chat_dynamics] page_nav missing signed content_path; stay on current page");
}

export function mountPluginSideNav(currentId) {
  const root = document.getElementById("pluginSideNav");
  if (!root) return;

  const current = String(currentId || "");
  root.innerHTML = PLUGIN_PAGES.map((page) => {
    const label = escapeHtml(page.label);
    if (page.id === current) {
      return `<span class="plugin-side-item is-current" aria-current="page">${label}</span>`;
    }
    return `<button type="button" class="plugin-side-item" data-nav-page="${escapeHtml(page.id)}">${label}</button>`;
  }).join("");

  root.querySelectorAll("[data-nav-page]").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.preventDefault();
      void navigateToPluginPage(btn.getAttribute("data-nav-page"));
    });
  });

  // Back-compat: leave #pageNav empty so CSS can hide it.
  const legacy = document.getElementById("pageNav");
  if (legacy) legacy.innerHTML = "";
  mountThemeToggle();
}

export default {
  PLUGIN_PAGES,
  extractContentPath,
  signedContentHref,
  siblingContentUrl,
  navigateToPluginPage,
  mountPluginSideNav,
  resolvedUi,
  applyUi,
};
