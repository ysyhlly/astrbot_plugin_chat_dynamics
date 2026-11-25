/* Unified null-safe companion theme bootstrap synced from chat_dynamics/pages/shared/series-theme.js. */
(() => {
  "use strict";
  if (typeof document === "undefined" || typeof window === "undefined") return;
  const KEY = "chat_dynamics_ui";
  const valid = (value) => value === "day" || value === "night";

  function normalizeQuery(params) {
    if (!params || typeof params.get !== "function") return null;
    const ui = params.get("ui");
    if (valid(ui)) return ui;
    const theme = params.get("theme");
    if (theme === "dark") return "night";
    if (theme === "light") return "day";
    return null;
  }

  function currentMode() {
    const root = document.documentElement;
    return root && root.dataset && root.dataset.uiTheme === "night" ? "night" : "day";
  }

  function getById(id) {
    return typeof document.getElementById === "function" ? document.getElementById(id) : null;
  }

  function paint(value) {
    const mode = valid(value) ? value : "day";
    const night = mode === "night";
    const root = document.documentElement;
    if (root && root.dataset) {
      root.dataset.uiTheme = mode;
    }
    if (root && root.style) {
      root.style.colorScheme = night ? "dark" : "light";
    }

    const standardToggle = getById("theme-toggle");
    if (standardToggle) {
      if (typeof standardToggle.setAttribute === "function") {
        standardToggle.setAttribute("aria-pressed", String(night));
        standardToggle.setAttribute("aria-label", night ? "切换到日间模式" : "切换到夜间模式");
      }
      const standardLabel =
        getById("theme-label") ||
        (typeof standardToggle.querySelector === "function"
          ? standardToggle.querySelector(".ui-theme-label")
          : null);
      if (standardLabel) standardLabel.textContent = night ? "夜间模式" : "日间模式";
    }

    const worldToggle = getById("dwTheme");
    if (worldToggle) {
      if (typeof worldToggle.setAttribute === "function") {
        worldToggle.setAttribute("aria-pressed", String(night));
        worldToggle.setAttribute("aria-label", night ? "切换到日间模式" : "切换到夜间模式");
      }
      const worldLabel =
        typeof worldToggle.querySelector === "function"
          ? worldToggle.querySelector(".dw-theme-label")
          : null;
      if (worldLabel) worldLabel.textContent = night ? "夜间模式" : "日间模式";
    }
    return mode;
  }

  function rememberUrl(mode) {
    try {
      if (typeof location === "undefined" || !location || !location.href) return;
      const url = new URL(location.href);
      url.searchParams.set("ui", mode);
      if (typeof history !== "undefined" && history && typeof history.replaceState === "function") {
        history.replaceState(history.state, "", url);
      }
    } catch {
      /* Sandboxed history can be unavailable. */
    }
  }

  function toggleTheme() {
    const next = currentMode() === "night" ? "day" : "night";
    paint(next);
    let stored = true;
    try {
      localStorage.setItem(KEY, next);
    } catch {
      stored = false;
    }
    rememberUrl(next);
    const statusNode = getById("dwThemeStatus");
    if (statusNode) {
      statusNode.textContent = stored ? "" : "当前页已切换，浏览器未允许保存偏好。";
    }
  }

  let saved = null;
  try {
    saved = localStorage.getItem(KEY);
  } catch {
    /* Sandboxed storage can be unavailable. */
  }
  let query = null;
  try {
    const search = typeof location !== "undefined" && location ? location.search || "" : "";
    query = normalizeQuery(new URLSearchParams(search));
  } catch {
    /* Invalid search query. */
  }
  paint(valid(query) ? query : valid(saved) ? saved : "day");

  function bindToggles() {
    paint(currentMode());
    const standardToggle = getById("theme-toggle");
    if (standardToggle) {
      standardToggle.onclick = toggleTheme;
    }
    const worldToggle = getById("dwTheme");
    if (worldToggle) {
      if (typeof worldToggle.addEventListener === "function") {
        worldToggle.addEventListener("click", toggleTheme);
      } else {
        worldToggle.onclick = toggleTheme;
      }
    }
  }

  if (document.readyState === "loading") {
    if (typeof document.addEventListener === "function") {
      document.addEventListener("DOMContentLoaded", bindToggles, { once: true });
    }
  } else {
    bindToggles();
  }

  if (typeof window.addEventListener === "function") {
    window.addEventListener("storage", (event) => {
      if (event && event.key === KEY && valid(event.newValue)) {
        paint(event.newValue);
        rememberUrl(event.newValue);
      }
    });
  }
})();
