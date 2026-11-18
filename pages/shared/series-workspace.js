/* Plugin-local navigation and data access inside a stock AstrBot view. */
(() => {
  const script = document.currentScript;
  const plugin = script.dataset.plugin;
  const view = script.dataset.view;
  const native = window.AstrBotPluginView || window.AstrBotPluginPage;
  if (!native || !plugin || !view) return;
  const query = new URLSearchParams(location.search);
  const entry = query.get("series_entry");
  if (!entry && typeof native.navigateToPlugin === "function") return;

  const initial = native.getContext?.() || {};
  const preferences = {};
  const subscribers = new Set();
  const streams = new Set();
  const pendingStreams = new Set();
  let stopping = false;
  if (query.has("lang")) preferences.localeOverride = query.get("lang");
  if (query.has("umo")) preferences.scope = query.get("umo");
  if (query.has("ui")) preferences.ui = query.get("ui");

  function context() {
    const host = native.getContext?.() || {};
    if (!entry) return { ...host, ...preferences };
    // The stock parent still describes the entry. The served page supplied
    // its own localized metadata when AstrBot initialized its native SDK.
    return { ...initial, isDark: host.isDark ?? initial.isDark,
      ...preferences, pluginName: plugin, pageName: view };
  }
  function notify() {
    for (const callback of subscribers) callback(context());
  }
  function endpoint(name) {
    return entry
      ? "series_workspace/panel/" + plugin + "/" + name.replace(/^\/+/, "") : name;
  }
  const bridge = {
    async ready() { await native.ready(); return context(); },
    getContext: context,
    getLocale() { const c = context(); return c.localeOverride || c.locale || "zh-CN"; },
    getI18n() { return context().i18n || {}; },
    t(key, fallback) {
      const c = context();
      for (const locale of [c.localeOverride, c.locale, "zh-CN", "en-US"]) {
        let value = c.i18n?.[locale];
        for (const part of String(key).split(".")) value = value?.[part];
        if (value != null) return String(value);
      }
      return fallback || "";
    },
    onContext(callback) {
      subscribers.add(callback);
      callback(context());
      return () => subscribers.delete(callback);
    },
    async setNavigationContext(options) {
      if (typeof options.locale === "string") preferences.localeOverride = options.locale;
      if (typeof options.ui === "string") preferences.ui = options.ui;
      if (typeof options.scope === "string") preferences.scope = options.scope;
      notify();
    },
    apiGet(name, params) { return native.apiGet(endpoint(name), params); },
    apiPost(name, body) { return native.apiPost(endpoint(name), body); },
    upload(name, file) { return native.upload(endpoint(name), file); },
    download(name, params, filename) { return native.download(endpoint(name), params, filename); },
    subscribeSSE(name, handlers, params) {
      if (stopping) return Promise.reject(new Error("面板已离开"));
      const task = native.subscribeSSE(endpoint(name), handlers, params).then(async id => {
        if (stopping) await native.unsubscribeSSE(id);
        else streams.add(id);
        return id;
      });
      pendingStreams.add(task);
      return task.finally(() => pendingStreams.delete(task));
    },
    unsubscribeSSE(id) {
      streams.delete(id);
      return native.unsubscribeSSE(id);
    },
  };
  async function stopStreams() {
    stopping = true;
    const tasks = [...pendingStreams, ...Array.from(streams, id => bridge.unsubscribeSSE(id))];
    await Promise.allSettled(tasks);
  }
  function destinationPath(data, id, page) {
    if (data?.plugin !== id || data?.view !== page || typeof data.path !== "string"
        || !data.path.startsWith("/") || data.path.startsWith("//")) throw Error("目标面板地址无效");
    const url = new URL(data.path, location.href);
    const prefix = "/api/v1/plugins/" + encodeURIComponent(id) + "/views/" + encodeURIComponent(page) + "/_t/";
    if (url.origin !== new URL(location.href).origin || !url.pathname.startsWith(prefix)
        || !url.pathname.slice(prefix.length).split("/")[0]) throw Error("目标面板地址无效");
    return url;
  }
  async function open(id, page, options, transition) {
    // This is always the entry plugin's normal authenticated API. Only a
    // participating destination can offer its own view and its panel operations.
    const response = await native.apiGet("series_workspace/open/" + encodeURIComponent(id), { lang: options.locale });
    if (response?.status === "error") throw Error(response.message);
    const url = destinationPath(response?.data ?? response, id, page);
    url.searchParams.set("series_entry", entry || plugin);
    for (const [key, value] of Object.entries({ lang: options.locale, umo: options.scope, ui: options.ui })) {
      if (typeof value === "string" && value) url.searchParams.set(key, value);
    }
    if (options.ui) url.searchParams.set("theme", options.ui === "night" ? "dark" : "light");
    if (transition) await transition();
    await stopStreams();
    try { window.location.assign(url.href); }
    catch (error) { stopping = false; throw error; }
    return { navigating: true };
  }
  native.onContext?.(notify);
  if (entry) {
    // Keep the workspace binding on the target's own section links, including
    // links created later by its task list. Never add it to external URLs.
    const pageRoot = new URL(script.src).pathname.replace(/\/series-workspace\.js$/, "/");
    function carryEntry(event) {
      const link = event.target.closest?.("a[href]");
      if (!link || link.hasAttribute("download")) return;
      const url = new URL(link.href, location.href);
      if (url.origin === new URL(location.href).origin && url.pathname.startsWith(pageRoot)
          && url.pathname.endsWith(".html") && !url.searchParams.has("series_entry")) {
        url.searchParams.set("series_entry", entry);
        link.href = url.href;
      }
    }
    for (const event of ["click", "auxclick", "contextmenu"]) document.addEventListener(event, carryEntry, true);
  }
  window.addEventListener("pagehide", () => { void stopStreams(); });
  window.addEventListener("pageshow", () => { stopping = false; });
  window.DynamicsPanelBridge = bridge;
  window.DynamicsWorkspace = { open };
})();
