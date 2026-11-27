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
  if (query.has("persona")) preferences.persona = query.get("persona");
  if (query.has("ui")) preferences.ui = query.get("ui");
  if (query.has("series_deeplink")) {
    const rawDeepLink = String(query.get("series_deeplink") || "").trim();
    if (rawDeepLink) {
      try { preferences.deep_link = JSON.parse(rawDeepLink); }
      catch { preferences.deep_link = rawDeepLink.slice(0, 256); }
    }
  }

  const RESERVED_QUERY_KEYS = new Set([
    "series_entry", "series_deeplink", "lang", "umo", "persona", "ui", "theme", "scope", "locale", "deep_link", "plugin",
  ]);
  const SAFE_QUERY_KEY = /^[a-zA-Z][a-zA-Z0-9_]{0,31}$/;

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
    if (!entry) return name;
    const clean = String(name || "").replace(/^\/+/, "");
    if (/^series_workspace\/(?:panel|open)\//.test(clean)) return clean;
    return "series_workspace/panel/" + plugin + "/" + clean;
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
      if (typeof options.persona === "string") preferences.persona = options.persona;
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
  async function withTimeout(operation) {
    let timer;
    try {
      return await Promise.race([operation, new Promise((_, reject) => {
        timer = setTimeout(() => reject(Error(window.DynamicsUI?.tr("请求超时，请刷新重试") || "请求超时，请刷新重试")), 8000);
      })]);
    } finally { clearTimeout(timer); }
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
  function applyDeepLinkParams(url, options) {
    if (!options || typeof options !== "object") return;
    const rawDeepLink = options.deep_link;
    if (rawDeepLink && typeof rawDeepLink === "object" && !Array.isArray(rawDeepLink)) {
      const clean = {};
      for (const [k, v] of Object.entries(rawDeepLink)) {
        if (!SAFE_QUERY_KEY.test(k) || v == null || v === "") continue;
        if (typeof v !== "string" && typeof v !== "number" && typeof v !== "boolean") continue;
        const text = String(v).trim().slice(0, 256);
        if (!text) continue;
        clean[k] = text;
        if (!RESERVED_QUERY_KEYS.has(k)) url.searchParams.set(k, text);
      }
      if (!url.searchParams.has("umo") && (clean.umo || clean.scope)) {
        url.searchParams.set("umo", clean.umo || clean.scope);
      }
      if (!url.searchParams.has("persona") && clean.persona) {
        url.searchParams.set("persona", clean.persona);
      }
      if (Object.keys(clean).length) {
        const encoded = JSON.stringify(clean);
        if (encoded.length <= 1024) url.searchParams.set("series_deeplink", encoded);
      }
    } else if (typeof rawDeepLink === "string" && rawDeepLink.trim()) {
      url.searchParams.set("series_deeplink", rawDeepLink.trim().slice(0, 256));
    }
    for (const [k, v] of Object.entries(options)) {
      if (RESERVED_QUERY_KEYS.has(k) || !SAFE_QUERY_KEY.test(k) || v == null || v === "") continue;
      if (typeof v !== "string" && typeof v !== "number" && typeof v !== "boolean") continue;
      if (!url.searchParams.has(k)) {
        const text = String(v).trim().slice(0, 256);
        if (text) url.searchParams.set(k, text);
      }
    }
  }
  async function open(id, page, options = {}, transition) {
    // This is always the entry plugin's normal authenticated API. Only a
    // participating destination can offer its own view and its panel operations.
    const opts = options && typeof options === "object" ? options : {};
    const response = await withTimeout(native.apiGet("series_workspace/open/" + encodeURIComponent(id), { lang: opts.locale }));
    if (response?.status === "error") throw Error(response.message);
    const url = destinationPath(response?.data ?? response, id, page);
    url.searchParams.set("series_entry", entry || plugin);
    for (const [key, value] of Object.entries({ lang: opts.locale, umo: opts.scope, persona: opts.persona, ui: opts.ui })) {
      if (typeof value === "string" && value) url.searchParams.set(key, value);
    }
    if (opts.ui) url.searchParams.set("theme", opts.ui === "night" ? "dark" : "light");
    applyDeepLinkParams(url, opts);
    if (transition) await transition();
    try { await withTimeout(stopStreams()); window.location.assign(url.href); }
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
