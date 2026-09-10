/* Shared, synchronous first-paint bootstrap. Page-local copies also work in sandboxed iframes. */
(() => {
  "use strict";
  const KEY = "chat_dynamics_ui";
  const valid = (value) => value === "day" || value === "night";
  let revision = 0;
  let started = false;
  let writes = Promise.resolve();
  let status = "日夜模式随时切换";
  let retry = false;

  function stored() {
    try { return localStorage.getItem(KEY); } catch { return null; }
  }

  function current() {
    // AstrBot owns data-theme (dark/light) and rewrites it during bridge context updates.
    // Keep the plugin preference independent so host initialization cannot reset its palette.
    return document.documentElement.dataset.uiTheme === "night" ? "night" : "day";
  }

  function renderStatus(message = status, failed = false) {
    status = message;
    retry = failed;
    const node = document.getElementById("uiThemeStatus");
    if (node) node.textContent = status;
    const button = document.getElementById("btnUiRetry");
    if (button) button.hidden = !retry;
  }

  function paint(value) {
    const next = valid(value) ? value : "day";
    document.documentElement.dataset.uiTheme = next;
    document.documentElement.style.colorScheme = next === "night" ? "dark" : "light";
    const meta = document.querySelector('meta[name="color-scheme"]');
    if (meta) meta.content = next === "night" ? "dark light" : "light dark";
    const button = document.getElementById("btnUiTheme");
    if (button) {
      button.setAttribute("aria-pressed", String(next === "night"));
      button.setAttribute("aria-label", next === "night" ? "切换到日间模式" : "切换到夜间模式");
      const label = button.querySelector(".ui-theme-label");
      if (label) label.textContent = next === "night" ? "夜间模式" : "日间模式";
    }
    return next;
  }

  function cache(next) {
    try { localStorage.setItem(KEY, next); return true; } catch { return false; }
  }

  function updateUrl(next) {
    try {
      const url = new URL(location.href);
      url.searchParams.set("ui", next);
      history.replaceState(history.state, "", url);
    } catch { /* Sandboxed navigation still carries ui in its signed destination. */ }
  }

  function timeout(promise) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("theme request timeout")), 5000);
      Promise.resolve(promise).then(
        (value) => { clearTimeout(timer); resolve(value); },
        (error) => { clearTimeout(timer); reject(error); }
      );
    });
  }

  async function request(method, body) {
    const bridge = window.AstrBotPluginPage;
    if (!bridge || typeof bridge[method] !== "function") throw new Error("bridge unavailable");
    if (typeof bridge.ready === "function") await timeout(bridge.ready());
    const result = await timeout(bridge[method]("ui_preferences", body));
    if (!result || result.ok === false || result.status === "error") throw new Error("theme unavailable");
    return result.data || result;
  }

  function apply(value) {
    const next = paint(value);
    const localSaved = cache(next);
    updateUrl(next);
    const ownRevision = ++revision;
    renderStatus("正在保存…");
    // Serialize writes so a slow first click cannot overwrite a later choice.
    writes = writes.then(async () => {
      try {
        const result = await request("apiPost", { ui: next });
        if (result.saved !== true || result.ui !== next) throw new Error("theme not saved");
        if (revision === ownRevision) renderStatus("已保存到账号");
      } catch {
        if (revision === ownRevision) renderStatus(localSaved ? "已记住本机选择，账号同步失败" : "暂未保存，当前页面已生效", true);
      }
    });
    return writes;
  }

  async function start() {
    paint(current());
    renderStatus(status, retry);
    if (started) return;
    started = true;
    const ownRevision = revision;
    try {
      const result = await request("apiGet", {});
      if (revision !== ownRevision) return; // An in-flight restore must never undo a click.
      if (valid(result.ui)) {
        paint(result.ui);
        cache(result.ui);
        updateUrl(result.ui);
        renderStatus("已恢复账号偏好");
      }
    } catch { /* Local first paint remains usable when the host is unavailable. */ }
  }

  const query = new URLSearchParams(location.search).get("ui");
  paint(valid(query) ? query : stored());
  window.addEventListener("storage", (event) => {
    if (event.key !== KEY || !valid(event.newValue)) return;
    ++revision;
    paint(event.newValue);
    updateUrl(event.newValue);
    renderStatus("已同步其他页面的选择");
  });
  window.ChatDynamicsTheme = { current, apply, start, flush: () => writes };
})();
