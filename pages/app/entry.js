// All sections share the host's single app scope. Keep its signed URL, shared UMO and theme.
(async () => {
  "use strict";
  const TIMEOUT_MS = 2500;
  const DISMISSED_KEY = "chat_dynamics_setup_dismissed";
  let redirected = false;

  function go(relPath) {
    if (redirected) return;
    redirected = true;
    const target = new URL(relPath, window.location.href);
    target.search = window.location.search;
    try {
      if (!target.searchParams.get("umo")) {
        const sesUmo = (sessionStorage.getItem("dynamics_shared_umo") || "").trim();
        if (sesUmo) target.searchParams.set("umo", sesUmo);
      }
    } catch {}
    target.hash = window.location.hash;
    window.location.replace(target.href);
  }

  function unwrap(raw) {
    if (!raw || typeof raw !== "object") return null;
    if (raw.status === "error" || raw.ok === false) return null;
    return raw.data !== undefined ? raw.data : raw;
  }

  function localDismissed() {
    try {
      return localStorage.getItem(DISMISSED_KEY) === "1";
    } catch {
      return false;
    }
  }

  function hasEffectiveScope(effective) {
    if (!effective || typeof effective !== "object") return true;
    const groups = Array.isArray(effective.takeover_groups)
      ? effective.takeover_groups.map((v) => String(v || "").trim()).filter(Boolean)
      : [];
    return Boolean(effective.takeover_all) || groups.length > 0 || Boolean(effective.private_chat_enabled);
  }

  const bridge = window.DynamicsPanelBridge || window.AstrBotPluginPage || null;
  if (!bridge || typeof bridge.apiGet !== "function") {
    go("./console/index.html");
    return;
  }

  const timer = window.setTimeout(() => go("./console/index.html"), TIMEOUT_MS);
  try {
    if (typeof bridge.ready === "function") {
      try {
        await bridge.ready();
      } catch {}
    }
    if (redirected) return;
    const [cfgRes, prefRes] = await Promise.allSettled([
      bridge.apiGet("config", {}),
      bridge.apiGet("ui_preferences", {}),
    ]);
    if (redirected) return;
    const cfg = cfgRes.status === "fulfilled" ? unwrap(cfgRes.value) : null;
    const effective = cfg && typeof cfg.effective === "object" && cfg.effective !== null ? cfg.effective : null;
    if (!effective) {
      go("./console/index.html");
      return;
    }
    const prefs = prefRes.status === "fulfilled" ? unwrap(prefRes.value) : null;
    const dismissed =
      prefs && typeof prefs.setup_dismissed === "boolean"
        ? prefs.setup_dismissed === true
        : localDismissed();
    if (!hasEffectiveScope(effective) && !dismissed) {
      go("./setup/index.html");
    } else {
      go("./console/index.html");
    }
  } catch {
    go("./console/index.html");
  } finally {
    window.clearTimeout(timer);
  }
})();
