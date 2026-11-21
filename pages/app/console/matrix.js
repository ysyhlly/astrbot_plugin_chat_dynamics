import { msg, setText } from "../shared/series-ui.js";
import { friendlyError } from "./errors.js";

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function mountGroupMatrix({ apiGet, apiPost, onOpenScope }) {
  const root = document.getElementById("view-matrix");
  if (!root) return { load: async () => {} };

  const headEl = document.getElementById("matrixTableHead");
  const bodyEl = document.getElementById("matrixTableBody");
  const statusEl = document.getElementById("matrixStatus");
  const emptyEl = document.getElementById("matrixEmpty");
  const tableWrap = document.getElementById("matrixTableWrap");
  const filterInput = document.getElementById("matrixFilter");
  const personaSelect = document.getElementById("matrixPersonaFilter");
  const btnRefresh = document.getElementById("btnMatrixRefresh");
  const countBadge = document.getElementById("matrixGroupCount");

  let data = null;
  let loading = false;
  let loadedOnce = false;
  const toggling = new Set();

  function setStatus(text, isError = false) {
    if (!statusEl) return;
    setText(statusEl, text || "");
    statusEl.classList.toggle("error", Boolean(isError));
  }

  function cellKey(umo, pluginId) {
    return `${umo}::${pluginId}`;
  }

  function renderPersonaOptions(groups) {
    if (!personaSelect) return;
    const prev = personaSelect.value || "";
    const personas = [...new Set(groups.map((g) => String(g.persona_id || "").trim()).filter(Boolean))].sort();
    const options = ['<option value="">全部角色</option>'].concat(
      personas.map((p) => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`)
    );
    personaSelect.innerHTML = options.join("");
    if (prev && personas.includes(prev)) personaSelect.value = prev;
  }

  function render() {
    if (!headEl || !bodyEl) return;
    const plugins = Array.isArray(data?.plugins) ? data.plugins : [];
    const allGroups = Array.isArray(data?.groups) ? data.groups : [];
    const term = String(filterInput?.value || "").trim().toLowerCase();
    const personaFilter = String(personaSelect?.value || "").trim();

    const groups = allGroups.filter((group) => {
      if (personaFilter && String(group.persona_id || "").trim() !== personaFilter) return false;
      if (!term) return true;
      const hay = `${group.label || ""} ${group.umo || ""} ${group.group_id || ""} ${group.persona_id || ""}`.toLowerCase();
      return hay.includes(term);
    });

    if (countBadge) {
      countBadge.textContent = `${groups.length} / ${allGroups.length} 群`;
    }

    headEl.innerHTML = `<tr>
      <th scope="col" class="matrix-col-group">群聊与绑定角色</th>
      ${plugins
        .map(
          (p) => `<th scope="col" class="matrix-col-plugin" title="${escapeHtml(p.label)} (${escapeHtml(p.state)})">
            <span class="matrix-plugin-mark">${escapeHtml(p.mark || "")}</span>
            <span class="matrix-plugin-name">${escapeHtml(p.label || p.id)}</span>
          </th>`
        )
        .join("")}
    </tr>`;

    if (!groups.length) {
      bodyEl.innerHTML = "";
      if (tableWrap) tableWrap.classList.add("hidden");
      if (emptyEl) {
        emptyEl.classList.remove("hidden");
        const emptyCopy = emptyEl.querySelector("p");
        if (emptyCopy) {
          emptyCopy.textContent = allGroups.length
            ? "当前筛选条件下没有匹配的群聊，试试清空搜索或切换角色。"
            : "暂未发现已接管或活跃的群聊，请先在「快速引导」或「参数配置」中启用群聊。";
        }
      }
      return;
    }

    if (tableWrap) tableWrap.classList.remove("hidden");
    if (emptyEl) emptyEl.classList.add("hidden");

    bodyEl.innerHTML = groups
      .map((group) => {
        const umo = String(group.umo || "");
        const persona = String(group.persona_id || "").trim();
        const cells = group.cells || {};
        const cellsHtml = plugins
          .map((plugin) => {
            const cell = cells[plugin.id] || {};
            const key = cellKey(umo, plugin.id);
            const busy = toggling.has(key);
            const enabled = cell.enabled;
            const supported = Boolean(cell.supported);
            const state = String(cell.state || plugin.state || "missing");
            const summary = String(cell.summary || "");
            const act = typeof cell.activity_today === "number" ? ` · 今日 ${cell.activity_today}` : "";
            const title = `${plugin.label}：${summary || state}${act}`;

            if (supported) {
              const isOn = enabled === true;
              const pillCls = isOn ? "is-on" : "is-off";
              const label = busy ? "切换中…" : isOn ? "● 开启" : "○ 暂停";
              return `<td class="matrix-cell">
                <button type="button"
                  class="matrix-cell-btn ${pillCls}"
                  data-matrix-toggle="1"
                  data-umo="${escapeHtml(umo)}"
                  data-plugin-id="${escapeHtml(plugin.id)}"
                  data-next-enabled="${isOn ? "false" : "true"}"
                  data-version="${escapeHtml(cell.version || group.version || data?.version || "")}"
                  aria-pressed="${isOn ? "true" : "false"}"
                  title="${escapeHtml(title)}（点击切换）"
                  ${busy ? "disabled" : ""}>${escapeHtml(label)}</button>
              </td>`;
            }

            const readonlyTone =
              state === "missing" || state === "disabled" || state === "unloaded"
                ? "is-offline"
                : enabled === true
                  ? "is-readonly-on"
                  : enabled === false
                    ? "is-readonly-off"
                    : "is-readonly";
            const text =
              summary ||
              (state === "missing"
                ? "未安装"
                : state === "disabled"
                  ? "已停用"
                  : enabled === true
                    ? "● 开启"
                    : enabled === false
                      ? "○ 暂停"
                      : "只读");
            if (cell.can_open && plugin.id !== "astrbot_plugin_chat_dynamics") {
              return `<td class="matrix-cell">
                <button type="button"
                  class="matrix-cell-pill ${readonlyTone}"
                  data-matrix-open-plugin="${escapeHtml(plugin.id)}"
                  data-umo="${escapeHtml(umo)}"
                  title="${escapeHtml(title)}（点击前往面板）">${escapeHtml(text)}</button>
              </td>`;
            }
            return `<td class="matrix-cell">
              <span class="matrix-cell-pill ${readonlyTone}" title="${escapeHtml(title)}">${escapeHtml(text)}</span>
            </td>`;
          })
          .join("");

        return `<tr data-umo="${escapeHtml(umo)}">
          <td class="matrix-group-cell">
            <button type="button" class="matrix-group-link" data-matrix-focus-umo="${escapeHtml(umo)}" title="查看该群会话详情：${escapeHtml(umo)}">
              <span class="matrix-group-name">${escapeHtml(group.label || umo)}</span>
              <span class="matrix-group-meta">${persona ? `角色：${escapeHtml(persona)}` : "默认角色"} · <code>${escapeHtml(group.group_id || umo)}</code></span>
            </button>
          </td>
          ${cellsHtml}
        </tr>`;
      })
      .join("");
  }

  async function load({ quiet = false } = {}) {
    if (loading) return;
    loading = true;
    if (btnRefresh) btnRefresh.disabled = true;
    if (!quiet) setStatus(msg("正在聚合全系列 8 插件群聊状态…"));
    try {
      const result = await apiGet("series_matrix", {});
      data = result && typeof result === "object" ? result : { plugins: [], groups: [], version: "" };
      loadedOnce = true;
      renderPersonaOptions(Array.isArray(data.groups) ? data.groups : []);
      render();
      const total = Array.isArray(data.groups) ? data.groups.length : 0;
      const readyPlugins = (data.plugins || []).filter((p) => p.state === "ready" || p.state === "enabled").length;
      setStatus(`已同步 ${total} 个群聊 · ${readyPlugins}/${(data.plugins || []).length} 个系列插件在线`);
    } catch (err) {
      setStatus(friendlyError(err, "读取群聊总控矩阵失败，请刷新重试。"), true);
    } finally {
      loading = false;
      if (btnRefresh) btnRefresh.disabled = false;
    }
  }

  async function handleToggle(button) {
    const umo = button.dataset.umo || "";
    const pluginId = button.dataset.pluginId || "";
    const nextEnabled = button.dataset.nextEnabled === "true";
    const expectedVersion = button.dataset.version || data?.version || "";
    if (!umo || !pluginId) return;
    const key = cellKey(umo, pluginId);
    if (toggling.has(key)) return;
    toggling.add(key);
    render();
    setStatus(`正在${nextEnabled ? "恢复" : "暂停"}接管…`);
    try {
      const res = await apiPost("series_matrix_toggle", {
        umo,
        plugin_id: pluginId,
        enabled: nextEnabled,
        expected_version: expectedVersion,
      });
      if (data && Array.isArray(data.groups)) {
        const group = data.groups.find((g) => g.umo === umo);
        if (group && group.cells && group.cells[pluginId]) {
          group.cells[pluginId].enabled = res?.enabled ?? nextEnabled;
          if (res?.version) group.cells[pluginId].version = res.version;
          if (res?.summary) group.cells[pluginId].summary = res.summary;
        }
        if (res?.matrix_version) data.version = res.matrix_version;
      }
      const pluginLabel = (data?.plugins || []).find((p) => p.id === pluginId)?.label || pluginId;
      const groupLabel = (data?.groups || []).find((g) => g.umo === umo)?.label || umo;
      setStatus(`${groupLabel} · ${pluginLabel} 已${(res?.enabled ?? nextEnabled) ? "恢复开启" : "暂停接管"}`);
    } catch (err) {
      const text = friendlyError(err, "切换失败，请重试。");
      setStatus(text, true);
      if (String(err?.message || "").includes("冲突") || String(err?.message || "").includes("409")) {
        await load({ quiet: true });
      }
    } finally {
      toggling.delete(key);
      render();
    }
  }

  bodyEl?.addEventListener("click", (event) => {
    const toggleBtn = event.target.closest?.("[data-matrix-toggle]");
    if (toggleBtn) {
      void handleToggle(toggleBtn);
      return;
    }
    const openBtn = event.target.closest?.("[data-matrix-open-plugin]");
    if (openBtn) {
      const pluginId = openBtn.dataset.matrixOpenPlugin || "";
      const umo = openBtn.dataset.umo || "";
      if (umo) window.DynamicsUI?.setSharedScope?.(umo, { emit: true, syncUrl: false });
      if (pluginId && window.DynamicsSeriesNavigation?.open) {
        void window.DynamicsSeriesNavigation.open(pluginId).catch((err) => {
          setStatus(friendlyError(err, "无法打开目标面板"), true);
        });
      }
      return;
    }
    const focusBtn = event.target.closest?.("[data-matrix-focus-umo]");
    if (focusBtn) {
      const umo = focusBtn.dataset.matrixFocusUmo || "";
      if (umo && typeof onOpenScope === "function") {
        onOpenScope(umo);
      }
    }
  });

  filterInput?.addEventListener("input", () => render());
  personaSelect?.addEventListener("change", () => render());
  btnRefresh?.addEventListener("click", () => void load());

  window.addEventListener("console:viewchange", (event) => {
    if (event?.detail?.view === "matrix" && !loadedOnce) {
      void load();
    }
  });

  return {
    load,
    isLoaded: () => loadedOnce,
  };
}
