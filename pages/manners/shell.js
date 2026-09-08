import { escapeHtml } from "./api.js";
import { mountPluginSideNav } from "./plugin_nav.js";

export function renderNav(current) {
  mountPluginSideNav(current);
}

export function fillSessionSelect(selectEl, sessions, selected) {
  if (!selectEl) return;
  const rows = Array.isArray(sessions) ? sessions : [];
  const current = String(selected || "");
  const options = [`<option value="">全部会话（总览）</option>`];
  for (const row of rows) {
    const key = String(row.session_key || row.session_id || "");
    if (!key) continue;
    const label = row.group_id && row.group_id !== key
      ? `${row.group_id} · ${key.slice(0, 10)}…`
      : key.length > 28
        ? `${key.slice(0, 12)}…${key.slice(-6)}`
        : key;
    const sel = key === current ? " selected" : "";
    options.push(`<option value="${escapeHtml(key)}"${sel}>${escapeHtml(label)}</option>`);
  }
  selectEl.innerHTML = options.join("");
}
