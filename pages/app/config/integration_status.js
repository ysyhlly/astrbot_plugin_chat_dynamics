/** Read-only companion status; configuration remains owned by each plugin. */
const LABELS = {
  ready: "接口可用", partial: "需兼容接口", missing: "未安装",
  disabled: "已停用", loading: "等待就绪",
};

function text(tag, value, className = "") {
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  return node;
}

export function mountIntegrationStatus({apiGet, focusGroup}) {
  const root = document.getElementById("integrationCards");
  const note = document.getElementById("integrationNote");
  const summary = document.getElementById("integrationSummary");
  const button = document.getElementById("btnIntegrationRefresh");
  let sequence = 0;

  async function refresh() {
    if (!root || !note || !summary || !button) return;
    const request = ++sequence;
    button.disabled = true;
    root.setAttribute("aria-busy", "true");
    note.textContent = "正在核对插件与接口…";
    try {
      const result = await apiGet("integrations");
      if (request !== sequence) return;
      if (result?.schema_version !== 1 || !Array.isArray(result.companions)) throw new Error("unsupported snapshot");
      const rows = result.companions;
      const blocking = Array.isArray(result.blocking) ? result.blocking : [];
      root.replaceChildren();
      for (const row of rows) {
        const card = document.createElement("article");
        card.className = "integration-card";
        card.dataset.integration = String(row.id || "");
        const header = document.createElement("div");
        header.className = "integration-card-heading";
        header.append(text("h3", String(row.name || "联动插件")));
        const state = Object.hasOwn(LABELS, row.state) ? row.state : "loading";
        header.append(text("span", LABELS[state], "integration-state integration-state-" + state));
        card.append(header, text("p", String(row.role || ""), "integration-role"));
        if (row.version) card.append(text("p", String(row.version), "integration-version"));
        card.append(text("p", String(row.settings || "")));
        if (row.required && state !== "ready") {
          card.append(text("p", row.id === "autonomous_blocking"
            ? "已要求此服务可用：当前状态会阻止角色回复。"
            : "持续生活已启用：这项能力尚未接通。", "integration-required"));
        }
        if (row.config_group) {
          const action = text("button", "查看本插件相关设置", "button button-quiet");
          action.type = "button";
          action.addEventListener("click", () => focusGroup(String(row.config_group)));
          card.append(action);
        }
        root.append(card);
      }
      summary.textContent = `${rows.filter(row => row.state === "ready").length} / ${rows.length} 接口可用`;
      note.textContent = blocking.length
        ? "当前启用的功能存在未接通的依赖，请查看对应卡片。"
        : "联动均按需接入；未安装的可选插件不影响基础聊天。接口可用不代表已为所有会话启用。";
      note.dataset.error = String(blocking.length > 0);
    } catch (_error) {
      if (request !== sequence) return;
      summary.textContent = "状态暂不可用";
      note.textContent = "联动状态读取失败，可重试；当前设置和未保存修改不受影响。";
      note.dataset.error = "true";
    } finally {
      if (request === sequence) {
        root.setAttribute("aria-busy", "false");
        button.disabled = false;
      }
    }
  }
  button?.addEventListener("click", () => { void refresh(); });
  return {refresh};
}
