import { msg, tr, setText } from "../shared/series-ui.js";
import { friendlyError } from "./errors.js";

const FALLBACK_PLUGINS = [
  { id: "astrbot_plugin_chat_dynamics", label: "对话主控", mark: "CD" },
  { id: "astrbot_plugin_dynamics_memory", label: "群间记忆", mark: "DM" },
  { id: "astrbot_plugin_dynamics_learning", label: "群间学习", mark: "DL" },
  { id: "astrbot_plugin_dynamics_world", label: "群间世界", mark: "DW" },
  { id: "astrbot_plugin_dynamics_horizons", label: "群间见闻", mark: "DH" },
  { id: "astrbot_plugin_dynamics_games", label: "群间游戏", mark: "DG" },
  { id: "astrbot_plugin_group_chronicle", label: "群间史记", mark: "GC" },
  { id: "astrbot_plugin_visual_media", label: "视觉工坊", mark: "VM" },
];

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function isConflictError(err) {
  const message = String(err?.message || err || "");
  return message.includes("冲突") || message.includes("409") || message.toLowerCase().includes("version");
}

export function mountGroupMatrix({ apiGet, apiPost, onOpenScope }) {
  const root = document.getElementById("view-matrix");
  if (!root) return { load: async () => {}, isLoaded: () => false };

  const headEl = document.getElementById("matrixTableHead");
  const bodyEl = document.getElementById("matrixTableBody");
  const statusEl = document.getElementById("matrixStatus");
  const feedbackEl = document.getElementById("matrixBatchFeedback");
  const emptyEl = document.getElementById("matrixEmpty");
  const tableWrap = document.getElementById("matrixTableWrap");
  const filterInput = document.getElementById("matrixFilter");
  const personaSelect = document.getElementById("matrixPersonaFilter");
  const btnRefresh = document.getElementById("btnMatrixRefresh");
  const btnTemplates = document.getElementById("btnMatrixTemplates");
  const countBadge = document.getElementById("matrixGroupCount");

  // Template manager dialog elements
  const tplDialog = document.getElementById("matrixTemplatesDialog");
  const btnCloseTplDialog = document.getElementById("btnCloseTplDialog");
  const tplListEl = document.getElementById("tplList");
  const tplForm = document.getElementById("tplForm");
  const tplFormTitle = document.getElementById("tplFormTitle");
  const btnNewTemplate = document.getElementById("btnNewTemplate");
  const tplNameInput = document.getElementById("tplName");
  const tplDescInput = document.getElementById("tplDesc");
  const tplHintInput = document.getElementById("tplStrategyHint");
  const tplSwitchesEl = document.getElementById("tplPluginSwitches");
  const btnSaveTemplate = document.getElementById("btnSaveTemplate");
  const btnResetTemplate = document.getElementById("btnResetTemplate");
  const tplFormStatus = document.getElementById("tplFormStatus");

  let data = null;
  let loading = false;
  let loadedOnce = false;
  const toggling = new Set();
  const batchBusyUmos = new Set();

  let templates = [];
  let templatePlugins = [];
  let templatesLoaded = false;
  let tplSaving = false;
  let editingTemplateId = ""; // empty => create new custom template
  let viewingBuiltinId = ""; // non-empty => readonly preview in form

  function setStatus(text, isError = false) {
    if (!statusEl) return;
    setText(statusEl, text || "");
    statusEl.classList.toggle("error", Boolean(isError));
  }

  function setTplStatus(text, isError = false) {
    if (!tplFormStatus) return;
    setText(tplFormStatus, text || "");
    tplFormStatus.classList.toggle("error", Boolean(isError));
  }

  function cellKey(umo, pluginId) {
    return `${umo}::${pluginId}`;
  }

  function getCatalogPlugins() {
    if (templatePlugins.length) return templatePlugins;
    if (Array.isArray(data?.plugins) && data.plugins.length) return data.plugins;
    return FALLBACK_PLUGINS;
  }

  function pluginLabel(pluginId) {
    const found =
      (Array.isArray(data?.plugins) ? data.plugins : []).find((p) => p.id === pluginId) ||
      templatePlugins.find((p) => p.id === pluginId) ||
      FALLBACK_PLUGINS.find((p) => p.id === pluginId);
    return found?.label || pluginId;
  }

  function clearBatchFeedback() {
    if (!feedbackEl) return;
    feedbackEl.classList.add("hidden");
    feedbackEl.innerHTML = "";
  }

  function showBatchFeedback({ groupLabel, actionLabel, res }) {
    if (!feedbackEl) return;
    const applied = Array.isArray(res?.applied) ? res.applied : [];
    const skipped = Array.isArray(res?.skipped) ? res.skipped : [];
    const failed = Array.isArray(res?.failed) ? res.failed : [];
    const summaryText = tr(
      "{0}（{1}）：已应用 {2} / 跳过 {3} / 失败 {4}",
      groupLabel,
      actionLabel,
      applied.length,
      skipped.length,
      failed.length
    );
    setStatus(summaryText, failed.length > 0 && applied.length === 0);

    const pills = [
      ...applied.map(
        (item) =>
          `<span class="mbf-pill is-applied">✓ ${escapeHtml(pluginLabel(item.plugin_id))} · ${escapeHtml(item.enabled ? tr("开启") : tr("暂停"))}</span>`
      ),
      ...skipped.map(
        (item) =>
          `<span class="mbf-pill is-skipped" title="${escapeHtml(item.message || item.reason || "")}">↷ ${escapeHtml(pluginLabel(item.plugin_id))}${item.message || item.reason ? ` (${escapeHtml(item.message || item.reason)})` : ""}</span>`
      ),
      ...failed.map(
        (item) =>
          `<span class="mbf-pill is-failed" title="${escapeHtml(item.message || item.reason || "")}">✕ ${escapeHtml(pluginLabel(item.plugin_id))}${item.message || item.reason ? ` (${escapeHtml(item.message || item.reason)})` : ""}</span>`
      ),
    ].join("");

    feedbackEl.classList.remove("hidden");
    feedbackEl.classList.toggle("has-error", failed.length > 0);
    feedbackEl.innerHTML = `<div class="mbf-head">
        <strong>${escapeHtml(summaryText)}</strong>
        <button type="button" class="button button-quiet mbf-close" data-mbf-dismiss="1" aria-label="${escapeHtml(tr("关闭提示"))}">✕</button>
      </div>
      ${pills ? `<div class="mbf-pills">${pills}</div>` : ""}`;
  }

  function renderPersonaOptions(groups) {
    if (!personaSelect) return;
    const prev = personaSelect.value || "";
    const personas = [...new Set(groups.map((g) => String(g.persona_id || "").trim()).filter(Boolean))].sort();
    const options = [`<option value="">${escapeHtml(tr("全部角色"))}</option>`].concat(
      personas.map((p) => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`)
    );
    personaSelect.innerHTML = options.join("");
    if (prev && personas.includes(prev)) personaSelect.value = prev;
  }

  function buildTemplateSelectOptions() {
    const builtins = templates.filter((t) => t && (t.builtin || t.readonly));
    const customs = templates.filter((t) => t && !t.builtin && !t.readonly);
    const parts = [`<option value="">${escapeHtml(tr("应用模板…"))}</option>`];
    if (builtins.length) {
      parts.push(
        `<optgroup label="${escapeHtml(tr("内置模板"))}">${builtins
          .map((t) => `<option value="${escapeHtml(t.id)}">${escapeHtml(t.name || t.id)}</option>`)
          .join("")}</optgroup>`
      );
    }
    if (customs.length) {
      parts.push(
        `<optgroup label="${escapeHtml(tr("自定义模板"))}">${customs
          .map((t) => `<option value="${escapeHtml(t.id)}">${escapeHtml(t.name || t.id)}</option>`)
          .join("")}</optgroup>`
      );
    }
    return parts.join("");
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
      countBadge.textContent = `${groups.length} / ${allGroups.length} ${tr("群")}`;
    }

    headEl.innerHTML = `<tr>
      <th scope="col" class="matrix-col-group">${escapeHtml(tr("群聊与绑定角色"))}</th>
      ${plugins
        .map(
          (p) => `<th scope="col" class="matrix-col-plugin" title="${escapeHtml(p.label)} (${escapeHtml(p.state)})">
            <span class="matrix-plugin-mark">${escapeHtml(p.mark || "")}</span>
            <span class="matrix-plugin-name">${escapeHtml(p.label || p.id)}</span>
          </th>`
        )
        .join("")}
      <th scope="col" class="matrix-col-actions">${escapeHtml(tr("快捷批量 / 模板"))}</th>
    </tr>`;

    if (!groups.length) {
      bodyEl.innerHTML = "";
      if (tableWrap) tableWrap.classList.add("hidden");
      if (emptyEl) {
        emptyEl.classList.remove("hidden");
        const emptyCopy = emptyEl.querySelector("p");
        if (emptyCopy) {
          emptyCopy.textContent = allGroups.length
            ? tr("当前筛选条件下没有匹配的群聊，试试清空搜索或切换角色。")
            : tr("暂未发现已接管或活跃的群聊，请先在「快速引导」或「参数配置」中启用群聊。");
        }
      }
      return;
    }

    if (tableWrap) tableWrap.classList.remove("hidden");
    if (emptyEl) emptyEl.classList.add("hidden");

    const tplOptionsHtml = buildTemplateSelectOptions();

    bodyEl.innerHTML = groups
      .map((group) => {
        const umo = String(group.umo || "");
        const persona = String(group.persona_id || "").trim();
        const cells = group.cells || {};
        const rowBusy = batchBusyUmos.has(umo);
        const rowVersion = group.version || data?.version || "";

        const cellsHtml = plugins
          .map((plugin) => {
            const cell = cells[plugin.id] || {};
            const key = cellKey(umo, plugin.id);
            const busy = rowBusy || toggling.has(key);
            const enabled = cell.enabled;
            const supported = Boolean(cell.supported);
            const state = String(cell.state || plugin.state || "missing");
            const summary = String(cell.summary || "");
            const excludedByCore = Boolean(cell.excluded_by_core) || summary.includes("主控排除");
            const act = typeof cell.activity_today === "number" ? ` · ${tr("今日")} ${cell.activity_today}` : "";
            const excludedNote = excludedByCore
              ? ` · ${tr("该群位于主控排除名单（exclude_groups）中，运行时由主控统一拦截跳过")}`
              : "";
            const title = `${plugin.label}：${summary || state}${act}${excludedNote}`;

            const excludedBadge = excludedByCore
              ? `<span class="matrix-excluded-badge" title="${escapeHtml(tr("该群位于主控排除名单（exclude_groups）中，运行时由主控统一拦截跳过"))}">${escapeHtml(tr("主控排除"))}</span>`
              : "";

            if (supported) {
              const isOn = enabled === true;
              const pillCls = isOn ? "is-on" : "is-off";
              const label = busy ? tr("切换中…") : isOn ? `● ${tr("开启")}` : `○ ${tr("暂停")}`;
              return `<td class="matrix-cell${excludedByCore ? " is-excluded-cell" : ""}">
                <div class="matrix-cell-stack">
                  <button type="button"
                    class="matrix-cell-btn ${pillCls}"
                    data-matrix-toggle="1"
                    data-umo="${escapeHtml(umo)}"
                    data-plugin-id="${escapeHtml(plugin.id)}"
                    data-next-enabled="${isOn ? "false" : "true"}"
                    data-version="${escapeHtml(cell.version || rowVersion)}"
                    aria-pressed="${isOn ? "true" : "false"}"
                    title="${escapeHtml(title)}（${escapeHtml(tr("点击切换"))}）"
                    ${busy ? "disabled" : ""}>${escapeHtml(label)}</button>
                  ${excludedBadge}
                </div>
              </td>`;
            }

            if (excludedByCore) {
              if (cell.can_open && plugin.id !== "astrbot_plugin_chat_dynamics") {
                return `<td class="matrix-cell is-excluded-cell">
                  <button type="button"
                    class="matrix-cell-pill is-excluded"
                    data-matrix-open-plugin="${escapeHtml(plugin.id)}"
                    data-umo="${escapeHtml(umo)}"
                    title="${escapeHtml(title)}（${escapeHtml(tr("点击前往面板"))}）">${escapeHtml(tr("主控排除"))}</button>
                </td>`;
              }
              return `<td class="matrix-cell is-excluded-cell">
                <span class="matrix-cell-pill is-excluded" title="${escapeHtml(title)}">${escapeHtml(tr("主控排除"))}</span>
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
                ? tr("未安装")
                : state === "disabled"
                  ? tr("已停用")
                  : enabled === true
                    ? `● ${tr("开启")}`
                    : enabled === false
                      ? `○ ${tr("暂停")}`
                      : tr("只读"));
            if (cell.can_open && plugin.id !== "astrbot_plugin_chat_dynamics") {
              return `<td class="matrix-cell">
                <button type="button"
                  class="matrix-cell-pill ${readonlyTone}"
                  data-matrix-open-plugin="${escapeHtml(plugin.id)}"
                  data-umo="${escapeHtml(umo)}"
                  title="${escapeHtml(title)}（${escapeHtml(tr("点击前往面板"))}）">${escapeHtml(text)}</button>
              </td>`;
            }
            return `<td class="matrix-cell">
              <span class="matrix-cell-pill ${readonlyTone}" title="${escapeHtml(title)}">${escapeHtml(text)}</span>
            </td>`;
          })
          .join("");

        return `<tr data-umo="${escapeHtml(umo)}">
          <td class="matrix-group-cell">
            <button type="button" class="matrix-group-link" data-matrix-focus-umo="${escapeHtml(umo)}" title="${escapeHtml(tr("查看该群会话详情"))}：${escapeHtml(umo)}">
              <span class="matrix-group-name">${escapeHtml(group.label || umo)}</span>
              <span class="matrix-group-meta">${persona ? `${tr("角色")}：${escapeHtml(persona)}` : tr("默认角色")} · <code>${escapeHtml(group.group_id || umo)}</code></span>
            </button>
          </td>
          ${cellsHtml}
          <td class="matrix-actions-cell">
            <div class="matrix-row-actions">
              <button type="button"
                class="matrix-quick-btn"
                data-matrix-batch-mode="all_on"
                data-umo="${escapeHtml(umo)}"
                data-version="${escapeHtml(rowVersion)}"
                title="${escapeHtml(tr("一键全开该群所有可切换插件"))}"
                ${rowBusy ? "disabled" : ""}>${escapeHtml(tr("全开"))}</button>
              <button type="button"
                class="matrix-quick-btn is-pause"
                data-matrix-batch-mode="all_off"
                data-umo="${escapeHtml(umo)}"
                data-version="${escapeHtml(rowVersion)}"
                title="${escapeHtml(tr("一键暂停该群所有可切换插件"))}"
                ${rowBusy ? "disabled" : ""}>${escapeHtml(tr("全停"))}</button>
              <select class="matrix-tpl-select"
                data-matrix-apply-template="1"
                data-umo="${escapeHtml(umo)}"
                data-version="${escapeHtml(rowVersion)}"
                aria-label="${escapeHtml(tr("为 {0} 应用模板", group.label || umo))}"
                ${rowBusy || !templates.length ? "disabled" : ""}>
                ${tplOptionsHtml}
              </select>
            </div>
          </td>
        </tr>`;
      })
      .join("");
  }

  async function loadTemplates({ quiet = false } = {}) {
    try {
      const res = await apiGet("series_templates", {});
      if (res && typeof res === "object") {
        if (Array.isArray(res.templates)) {
          templates = res.templates;
        }
        if (Array.isArray(res.plugins) && res.plugins.length) {
          templatePlugins = res.plugins;
        }
        templatesLoaded = true;
      }
    } catch (err) {
      if (!quiet) {
        setTplStatus(friendlyError(err, "读取配置模板失败，请稍后重试。"), true);
      }
    }
    if (loadedOnce) render();
    if (tplDialog?.open) renderTemplateModal();
  }

  async function load({ quiet = false } = {}) {
    if (loading) return;
    loading = true;
    if (btnRefresh) btnRefresh.disabled = true;
    if (!quiet) setStatus(msg("正在聚合全系列 8 插件群聊状态…"));
    try {
      const [matrixRes] = await Promise.all([
        apiGet("series_matrix", {}),
        loadTemplates({ quiet: true }),
      ]);
      data = matrixRes && typeof matrixRes === "object" ? matrixRes : { plugins: [], groups: [], version: "" };
      loadedOnce = true;
      renderPersonaOptions(Array.isArray(data.groups) ? data.groups : []);
      render();
      const total = Array.isArray(data.groups) ? data.groups.length : 0;
      const readyPlugins = (data.plugins || []).filter((p) => p.state === "ready" || p.state === "enabled").length;
      setStatus(
        tr("已同步 {0} 个群聊 · {1}/{2} 个系列插件在线", total, readyPlugins, (data.plugins || []).length)
      );
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
    if (toggling.has(key) || batchBusyUmos.has(umo)) return;
    toggling.add(key);
    render();
    setStatus(nextEnabled ? msg("正在恢复接管…") : msg("正在暂停接管…"));
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
      const pLabel = pluginLabel(pluginId);
      const groupLabel = (data?.groups || []).find((g) => g.umo === umo)?.label || umo;
      setStatus(`${groupLabel} · ${pLabel} ${(res?.enabled ?? nextEnabled) ? tr("已恢复开启") : tr("已暂停接管")}`);
    } catch (err) {
      const text = friendlyError(err, "切换失败，请重试。");
      setStatus(text, true);
      if (isConflictError(err)) {
        await load({ quiet: true });
      }
    } finally {
      toggling.delete(key);
      render();
    }
  }

  async function handleRowBatch({ umo, expectedVersion, mode, templateId }) {
    if (!umo || batchBusyUmos.has(umo)) return;
    const group = (data?.groups || []).find((g) => g.umo === umo);
    const groupLabel = group?.label || umo;
    const body = { umo };
    const ver = expectedVersion || group?.version || data?.version || "";
    if (ver) body.expected_version = ver;

    let actionLabel = "";
    if (templateId) {
      body.template_id = templateId;
      const tpl = templates.find((t) => t.id === templateId);
      actionLabel = tpl?.name || templateId;
    } else if (mode === "all_on" || mode === "all_off") {
      const target = mode === "all_on";
      const states = {};
      for (const p of data?.plugins || []) {
        if (p?.id) states[p.id] = target;
      }
      body.states = states;
      actionLabel = target ? tr("一键全开") : tr("一键暂停");
    } else {
      return;
    }

    batchBusyUmos.add(umo);
    render();
    setStatus(tr("正在为 {0} 执行「{1}」…", groupLabel, actionLabel));

    try {
      const res = await apiPost("series_matrix_batch", body);
      if (data && Array.isArray(data.groups)) {
        if (res?.matrix_version) data.version = res.matrix_version;
        const idx = data.groups.findIndex((g) => g.umo === umo);
        if (idx >= 0) {
          if (res?.row && typeof res.row === "object") {
            data.groups[idx] = {
              ...data.groups[idx],
              ...res.row,
              cells: res.row.cells || data.groups[idx].cells || {},
            };
          } else {
            const targetRow = data.groups[idx];
            if (res?.matrix_version) targetRow.version = res.matrix_version;
            for (const item of Array.isArray(res?.applied) ? res.applied : []) {
              if (targetRow.cells && targetRow.cells[item.plugin_id]) {
                targetRow.cells[item.plugin_id].enabled = item.enabled;
                if (item.version) targetRow.cells[item.plugin_id].version = item.version;
                if (item.summary) targetRow.cells[item.plugin_id].summary = item.summary;
              }
            }
          }
        }
      }
      showBatchFeedback({ groupLabel, actionLabel, res });
    } catch (err) {
      const text = friendlyError(err, "批量操作失败，请刷新重试。");
      setStatus(text, true);
      if (isConflictError(err)) {
        await load({ quiet: true });
      }
    } finally {
      batchBusyUmos.delete(umo);
      render();
    }
  }

  /* ---------- Template Manager Modal ---------- */

  function renderPluginSwitches(statesMap = {}, readonly = false) {
    if (!tplSwitchesEl) return;
    const plugins = getCatalogPlugins();
    tplSwitchesEl.innerHTML = plugins
      .map((p) => {
        const checked = Boolean(statesMap[p.id]);
        return `<label class="tpl-switch-item${checked ? " is-on" : ""}${readonly ? " is-readonly" : ""}">
          <span class="tpl-switch-meta">
            <span class="matrix-plugin-mark">${escapeHtml(p.mark || "")}</span>
            <span class="tpl-switch-label">${escapeHtml(p.label || p.id)}</span>
          </span>
          <input type="checkbox"
            data-tpl-plugin="${escapeHtml(p.id)}"
            ${checked ? "checked" : ""}
            ${readonly ? "disabled" : ""} />
        </label>`;
      })
      .join("");
  }

  function resetTemplateForm({ defaultAllOn = false } = {}) {
    editingTemplateId = "";
    viewingBuiltinId = "";
    if (tplFormTitle) setText(tplFormTitle, msg("新建自定义模板"));
    if (tplNameInput) {
      tplNameInput.value = "";
      tplNameInput.readOnly = false;
    }
    if (tplDescInput) {
      tplDescInput.value = "";
      tplDescInput.readOnly = false;
    }
    if (tplHintInput) {
      tplHintInput.value = "";
      tplHintInput.readOnly = false;
    }
    const initStates = {};
    for (const p of getCatalogPlugins()) {
      initStates[p.id] = defaultAllOn || p.id === "astrbot_plugin_chat_dynamics";
    }
    renderPluginSwitches(initStates, false);
    if (btnSaveTemplate) {
      btnSaveTemplate.disabled = tplSaving;
      setText(btnSaveTemplate, msg("保存模板"));
    }
  }

  function loadTemplateIntoForm(tpl, { asClone = false } = {}) {
    if (!tpl) return;
    const isReadonly = Boolean(tpl.builtin || tpl.readonly) && !asClone;
    if (asClone) {
      editingTemplateId = "";
      viewingBuiltinId = "";
      if (tplFormTitle) setText(tplFormTitle, msg("基于内置模板新建"));
    } else if (isReadonly) {
      editingTemplateId = "";
      viewingBuiltinId = tpl.id;
      if (tplFormTitle) setText(tplFormTitle, msg("查看内置模板（只读）"));
    } else {
      editingTemplateId = tpl.id;
      viewingBuiltinId = "";
      if (tplFormTitle) setText(tplFormTitle, msg("编辑自定义模板"));
    }
    if (tplNameInput) {
      tplNameInput.value = asClone ? `${tpl.name || tpl.id} 副本` : tpl.name || "";
      tplNameInput.readOnly = isReadonly;
    }
    if (tplDescInput) {
      tplDescInput.value = tpl.description || "";
      tplDescInput.readOnly = isReadonly;
    }
    if (tplHintInput) {
      tplHintInput.value = tpl.strategy_hint || "";
      tplHintInput.readOnly = isReadonly;
    }
    renderPluginSwitches(tpl.states || {}, isReadonly);
    if (btnSaveTemplate) {
      btnSaveTemplate.disabled = tplSaving || isReadonly;
      setText(btnSaveTemplate, editingTemplateId ? msg("保存修改") : msg("保存模板"));
    }
  }

  function renderTemplateModal() {
    if (!tplListEl) return;
    const plugins = getCatalogPlugins();
    const totalCount = plugins.length || 8;
    if (!templates.length) {
      tplListEl.innerHTML = `<p class="ops-note">${escapeHtml(tr("暂未读取到模板，请新建自定义模板或稍后重试。"))}</p>`;
      return;
    }

    tplListEl.innerHTML = templates
      .map((tpl) => {
        const isBuiltin = Boolean(tpl.builtin || tpl.readonly);
        const states = tpl.states && typeof tpl.states === "object" ? tpl.states : {};
        const onCount = Object.values(states).filter(Boolean).length;
        const active = tpl.id === editingTemplateId || tpl.id === viewingBuiltinId;
        const actionsHtml = isBuiltin
          ? `<button type="button" class="button button-quiet tpl-item-btn" data-tpl-view="${escapeHtml(tpl.id)}">${escapeHtml(tr("查看"))}</button>
             <button type="button" class="button button-quiet tpl-item-btn" data-tpl-clone="${escapeHtml(tpl.id)}">${escapeHtml(tr("复制新建"))}</button>`
          : `<button type="button" class="button button-quiet tpl-item-btn" data-tpl-edit="${escapeHtml(tpl.id)}">${escapeHtml(tr("编辑"))}</button>
             <button type="button" class="button button-danger tpl-item-btn" data-tpl-delete="${escapeHtml(tpl.id)}">${escapeHtml(tr("删除"))}</button>`;
        return `<article class="tpl-list-item${active ? " is-active" : ""}" data-tpl-id="${escapeHtml(tpl.id)}">
          <div class="tpl-item-main">
            <div class="tpl-item-head">
              <strong>${escapeHtml(tpl.name || tpl.id)}</strong>
              <span class="tpl-badge ${isBuiltin ? "is-builtin" : "is-custom"}">${escapeHtml(isBuiltin ? tr("内置 · 只读") : tr("自定义"))}</span>
              <span class="tpl-count">${onCount}/${totalCount} ${escapeHtml(tr("开启"))}</span>
            </div>
            ${tpl.description ? `<p class="tpl-item-desc">${escapeHtml(tpl.description)}</p>` : ""}
            ${tpl.strategy_hint ? `<p class="tpl-item-hint">${escapeHtml(tpl.strategy_hint)}</p>` : ""}
          </div>
          <div class="tpl-item-actions">${actionsHtml}</div>
        </article>`;
      })
      .join("");
  }

  async function openTemplatesDialog() {
    if (!tplDialog) return;
    setTplStatus("");
    resetTemplateForm();
    renderTemplateModal();
    if (typeof tplDialog.showModal === "function") {
      tplDialog.showModal();
    } else {
      tplDialog.setAttribute("open", "");
    }
    await loadTemplates({ quiet: false });
    renderTemplateModal();
    if (!editingTemplateId && !viewingBuiltinId) {
      resetTemplateForm();
    }
  }

  async function saveTemplateForm(event) {
    event?.preventDefault();
    if (tplSaving || viewingBuiltinId) return;
    const name = String(tplNameInput?.value || "").trim();
    if (!name) {
      setTplStatus(msg("请填写模板名称。"), true);
      tplNameInput?.focus();
      return;
    }
    const description = String(tplDescInput?.value || "").trim();
    const strategy_hint = String(tplHintInput?.value || "").trim();
    const states = {};
    for (const input of tplSwitchesEl?.querySelectorAll("[data-tpl-plugin]") || []) {
      const pid = input.dataset.tplPlugin;
      if (pid) states[pid] = Boolean(input.checked);
    }

    tplSaving = true;
    if (btnSaveTemplate) btnSaveTemplate.disabled = true;
    setTplStatus(msg("正在保存模板…"));
    try {
      const payload = { name, description, strategy_hint, states };
      if (editingTemplateId) payload.id = editingTemplateId;
      const res = await apiPost("series_templates", payload);
      if (Array.isArray(res?.templates)) {
        templates = res.templates;
      } else if (res?.template) {
        const idx = templates.findIndex((t) => t.id === res.template.id);
        if (idx >= 0) templates[idx] = res.template;
        else templates.push(res.template);
      }
      const savedTpl = res?.template;
      if (savedTpl?.id) {
        editingTemplateId = savedTpl.id;
      }
      setTplStatus(editingTemplateId ? msg("自定义模板已保存。") : msg("新模板已创建。"));
      renderTemplateModal();
      render();
    } catch (err) {
      setTplStatus(friendlyError(err, "保存模板失败，请重试。"), true);
    } finally {
      tplSaving = false;
      if (btnSaveTemplate) btnSaveTemplate.disabled = Boolean(viewingBuiltinId);
    }
  }

  async function deleteCustomTemplate(tplId) {
    if (!tplId || tplSaving) return;
    const tpl = templates.find((t) => t.id === tplId);
    if (!tpl || tpl.builtin || tpl.readonly) return;
    const confirmed =
      typeof window.DynamicsUI?.confirm === "function"
        ? await window.DynamicsUI.confirm(msg("删除自定义模板「{0}」？此操作不可撤销。", tpl.name || tplId), {
            danger: true,
          })
        : true;
    if (!confirmed) return;

    tplSaving = true;
    setTplStatus(msg("正在删除模板…"));
    try {
      const res = await apiPost("series_templates", { action: "delete", id: tplId });
      if (Array.isArray(res?.templates)) {
        templates = res.templates;
      } else {
        templates = templates.filter((t) => t.id !== tplId);
      }
      if (editingTemplateId === tplId) {
        resetTemplateForm();
      }
      setTplStatus(msg("已删除自定义模板。"));
      renderTemplateModal();
      render();
    } catch (err) {
      setTplStatus(friendlyError(err, "删除模板失败，请重试。"), true);
    } finally {
      tplSaving = false;
    }
  }

  /* ---------- Event Wiring ---------- */

  bodyEl?.addEventListener("click", (event) => {
    const toggleBtn = event.target.closest?.("[data-matrix-toggle]");
    if (toggleBtn) {
      void handleToggle(toggleBtn);
      return;
    }
    const batchBtn = event.target.closest?.("[data-matrix-batch-mode]");
    if (batchBtn) {
      const umo = batchBtn.dataset.umo || "";
      const mode = batchBtn.dataset.matrixBatchMode || "";
      const expectedVersion = batchBtn.dataset.version || "";
      void handleRowBatch({ umo, expectedVersion, mode });
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

  bodyEl?.addEventListener("change", (event) => {
    const select = event.target.closest?.("[data-matrix-apply-template]");
    if (!select) return;
    const templateId = String(select.value || "").trim();
    if (!templateId) return;
    const umo = select.dataset.umo || "";
    const expectedVersion = select.dataset.version || "";
    select.value = "";
    void handleRowBatch({ umo, expectedVersion, templateId });
  });

  feedbackEl?.addEventListener("click", (event) => {
    if (event.target.closest?.("[data-mbf-dismiss]")) {
      clearBatchFeedback();
    }
  });

  btnTemplates?.addEventListener("click", () => {
    void openTemplatesDialog();
  });

  btnCloseTplDialog?.addEventListener("click", () => {
    tplDialog?.close?.();
  });

  tplDialog?.addEventListener("click", (event) => {
    if (event.target === tplDialog) {
      tplDialog.close?.();
    }
  });

  btnNewTemplate?.addEventListener("click", () => {
    setTplStatus("");
    resetTemplateForm();
    renderTemplateModal();
    tplNameInput?.focus();
  });

  btnResetTemplate?.addEventListener("click", () => {
    setTplStatus("");
    if (editingTemplateId) {
      const tpl = templates.find((t) => t.id === editingTemplateId);
      if (tpl) {
        loadTemplateIntoForm(tpl);
        return;
      }
    }
    resetTemplateForm();
    renderTemplateModal();
  });

  tplForm?.addEventListener("submit", (event) => {
    void saveTemplateForm(event);
  });

  tplSwitchesEl?.addEventListener("change", (event) => {
    const input = event.target.closest?.("[data-tpl-plugin]");
    if (!input) return;
    input.closest(".tpl-switch-item")?.classList.toggle("is-on", Boolean(input.checked));
  });

  tplListEl?.addEventListener("click", (event) => {
    const viewBtn = event.target.closest?.("[data-tpl-view]");
    if (viewBtn) {
      const tpl = templates.find((t) => t.id === viewBtn.dataset.tplView);
      if (tpl) {
        setTplStatus("");
        loadTemplateIntoForm(tpl, { asClone: false });
        renderTemplateModal();
      }
      return;
    }
    const cloneBtn = event.target.closest?.("[data-tpl-clone]");
    if (cloneBtn) {
      const tpl = templates.find((t) => t.id === cloneBtn.dataset.tplClone);
      if (tpl) {
        setTplStatus(msg("已载入副本，可修改名称和开关后保存为自定义模板。"));
        loadTemplateIntoForm(tpl, { asClone: true });
        renderTemplateModal();
        tplNameInput?.focus();
      }
      return;
    }
    const editBtn = event.target.closest?.("[data-tpl-edit]");
    if (editBtn) {
      const tpl = templates.find((t) => t.id === editBtn.dataset.tplEdit);
      if (tpl) {
        setTplStatus("");
        loadTemplateIntoForm(tpl, { asClone: false });
        renderTemplateModal();
        tplNameInput?.focus();
      }
      return;
    }
    const delBtn = event.target.closest?.("[data-tpl-delete]");
    if (delBtn) {
      void deleteCustomTemplate(delBtn.dataset.tplDelete || "");
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

  window.addEventListener("series-languagechange", () => {
    if (loadedOnce) render();
    if (tplDialog?.open) renderTemplateModal();
  });

  return {
    load,
    loadTemplates,
    isLoaded: () => loadedOnce,
  };
}
