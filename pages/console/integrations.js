/**
 * Integrations / Self-Learning Companion Diagnostic Module
 * AstrBot Chat Dynamics Console
 */

const CAPABILITIES = [
  [
    "memories",
    "01",
    "批准记忆",
    "把记忆正文与偏好交给主模型，在回复前注入提示词",
    ["get_approved_memories", "list_approved_memories", "fetch_memories", "query_memories"],
  ],
  [
    "relationships",
    "02",
    "关系提示",
    "理解参与者之间的亲疏度、交互历史与语境",
    ["get_relationship_hints", "list_relationships", "relationship_snapshot"],
  ],
  [
    "slang",
    "03",
    "群聊黑话",
    "保留群聊特殊词汇、含义定义和适用社交场景",
    ["get_slang_candidates", "list_slang", "approved_slang", "list_approved_slang"],
  ],
];

const CAPABILITY_NAMES = {
  memories: "批准记忆",
  relationships: "关系提示",
  slang: "群聊黑话",
  delivered_messages: "发送反馈",
  delivery_hooks: "发送反馈",
  input_hooks: "输入采集",
  native_hooks: "原生请求钩子",
  direct_methods: "直连读取方法",
  native_commands: "管理命令入口",
};

const CATEGORIES = [
  {
    key: "native_hooks",
    title: "原生请求钩子",
    desc: "AstrBot LLM 请求上下文注入",
    icon: "⚡",
    getMethods: (p) => list(p.native_hooks),
    emptyText: "未挂载请求钩子",
  },
  {
    key: "direct_methods",
    title: "直连读取方法",
    desc: "记忆、关系与黑话直查接口",
    icon: "📖",
    getMethods: (p) => Array.from(new Set(getDirectMethods(p))),
    emptyText: "未发现直连方法",
  },
  {
    key: "input_hooks",
    title: "输入采集",
    desc: "消息感知与会话事件监听",
    icon: "📥",
    getMethods: (p) => list(p.input_hooks),
    emptyText: "未配置输入监听",
  },
  {
    key: "delivery_hooks",
    title: "发送反馈",
    desc: "回复投递与发送结果通知通道",
    icon: "📤",
    getMethods: (p) => list(p.delivery_hooks),
    emptyText: "未配置反馈通道",
  },
  {
    key: "native_commands",
    title: "管理命令入口",
    desc: "搭档控制指令与自学习管理",
    icon: "🛠️",
    getMethods: (p) => list(p.native_commands),
    emptyText: "未暴露管理指令",
  },
];

const $ = (id) => document.getElementById(id);
const list = (value) => (Array.isArray(value) ? value : []);

function getDirectMethods(p) {
  if (!p || !p.direct_methods) return [];
  if (Array.isArray(p.direct_methods)) {
    return p.direct_methods
      .filter((item) => item != null && String(item).trim() !== "")
      .map((item) => String(item).trim());
  }
  if (typeof p.direct_methods === "object") {
    return Object.values(p.direct_methods).flatMap((val) => {
      if (Array.isArray(val)) {
        return val
          .filter((item) => item != null && String(item).trim() !== "")
          .map((item) => String(item).trim());
      }
      if (typeof val === "string" && val.trim()) return [val.trim()];
      return [];
    });
  }
  return [];
}

function formatErrorDetail(v) {
  if (v == null) return "";
  if (v instanceof Error) return v.message || String(v);
  if (typeof v === "object") {
    if (Object.keys(v).length === 0 && !(v instanceof Error)) return "";
    try {
      const seen = new WeakSet();
      const str = JSON.stringify(v, (key, value) => {
        if (typeof value === "object" && value !== null) {
          if (seen.has(value)) return "[Circular]";
          seen.add(value);
        }
        return value;
      });
      return str === "{}" ? (v.message || v.detail || v.error || "") : str;
    } catch {
      return v.message || v.detail || v.error || "";
    }
  }
  return String(v).trim();
}

function getErrorsList(raw) {
  if (!raw) return [];
  if (raw instanceof Error) {
    const msg = formatErrorDetail(raw);
    return msg ? [["调用异常", msg]] : [];
  }
  if (typeof raw === "string") {
    const trimmed = raw.trim();
    return trimmed ? [["调用异常", trimmed]] : [];
  }
  if (Array.isArray(raw)) {
    return raw
      .map((item, idx) => [`异常 ${idx + 1}`, formatErrorDetail(item)])
      .filter(([_, err]) => err !== "" && err !== "{}");
  }
  if (typeof raw === "object") {
    return Object.entries(raw)
      .map(([k, v]) => [k, formatErrorDetail(v)])
      .filter(([_, err]) => err !== "" && err !== "{}");
  }
  return [["异常", String(raw)]];
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

const providersByKey = new Map();
let fingerprint;

export function renderIntegrations(partner) {
  if (!$("integrationPanel")) return;
  let next;
  try {
    next = JSON.stringify(partner ?? null);
  } catch {
    next = String(Date.now());
  }
  if (next === fingerprint) return;
  fingerprint = next;

  const providers = list(partner?.providers);
  const native = providers.some((p) => list(p.native_hooks).length > 0);
  const disabled = partner?.enabled === false;
  const partnerErrors = getErrorsList(partner?.errors);
  const hasErrors =
    partnerErrors.length > 0 || providers.some((p) => getErrorsList(p.errors).length > 0);
  const isInitializing =
    providers.some((p) => p.ready === false) || partner?.detail === "plugin_initializing";

  // Status computation
  let statusText = "等待诊断";
  let state = "unknown";
  if (!partner) {
    statusText = "状态未知";
    state = "unknown";
  } else if (disabled) {
    statusText = "互联已关闭";
    state = "missing";
  } else if (hasErrors) {
    statusText = partner.lamp || "降级中";
    state = "degraded";
  } else if (isInitializing) {
    statusText = partner.lamp || "初始化中";
    state = "unknown";
  } else if (providers.length > 0) {
    statusText = partner.lamp || (native ? "原生钩子" : "接口已发现");
    state = "connected";
  } else {
    statusText = partner.lamp || "未发现搭档";
    state = "missing";
  }

  const statusEl = $("integrationStatus");
  if (statusEl) {
    statusEl.textContent = statusText;
    statusEl.dataset.state = state;
  }

  // Metric counts
  const discovered = new Set(providers.flatMap(getDirectMethods));
  if ($("integrationCount")) {
    $("integrationCount").textContent = partner ? String(discovered.size) : "—";
  }
  if ($("integrationProviderCount")) {
    $("integrationProviderCount").textContent = partner ? String(providers.length) : "—";
  }

  // Diagnostic Note
  if ($("integrationNote")) {
    let noteText = "这里显示接口接入方式与诊断状态，不展示记忆正文。";
    if (!partner) {
      noteText = "尚未取得接口诊断；连接恢复后自动更新。";
    } else if (disabled) {
      noteText =
        "本插件的直连读取与发送通知已关闭；搭档自己的原生钩子仍由其配置控制。可在插件参数配置中开启 selflearning_integration。";
    } else if (hasErrors) {
      noteText =
        "检测到部分接口调用异常，已自动跳过并降级，不影响主流程回复。搭档恢复后将自动重新接入。";
      if (partnerErrors.length > 0) {
        noteText += `（全局诊断：${partnerErrors.map(([k, v]) => `${k}：${v}`).join("；")}）`;
      }
    } else if (isInitializing) {
      noteText = "搭档正在初始化内部知识库或模型，准备完成后将自动加载对应接口。";
    } else if (!providers.length) {
      noteText =
        "支持与 SelfLearning 或 LivingMemory 搭档互联。若已安装，请确保在 AstrBot 中已启用该插件。";
    } else if (native) {
      noteText = "搭档已挂载 AstrBot 原生请求钩子，记忆将在请求发生时由宿主钩子自动注入主模型。";
    } else {
      noteText = "已识别直连调用方法，将在生成回复前按需检索批准记忆与关系提示。";
    }
    if (partner?.weakened && partner.weakened.length > 0 && !native) {
      noteText += `（诊断提示：${partner.weakened.join("，")}）`;
    }
    $("integrationNote").textContent = noteText;
  }

  // Capabilities cards
  const cardsContainer = $("integrationCapabilities");
  if (cardsContainer) {
    for (const [key, number, title, description, supported] of CAPABILITIES) {
      let card = document.getElementById(`capability-${key}`);
      if (!card) {
        card = el("article", "capability-card");
        card.id = `capability-${key}`;
        const head = el("div", "capability-heading");
        head.append(el("span", "capability-number", number), el("h3", "", title));
        const desc = el("p", "capability-description", description);
        const stateEl = el("p", "capability-state");
        const details = el("details", "capability-methods");
        const summary = el("summary", "capability-summary");
        summary.setAttribute("aria-expanded", "false");
        const titleSpan = el("span", "summary-title", `查看 ${supported.length} 个兼容方法名`);
        const iconSpan = el("span", "summary-icon", "▾");
        summary.append(titleSpan, iconSpan);
        details.addEventListener("toggle", () => {
          summary.setAttribute("aria-expanded", details.open ? "true" : "false");
          titleSpan.textContent = details.open
            ? `收起 ${supported.length} 个兼容方法名`
            : `查看 ${supported.length} 个兼容方法名`;
        });
        const methodsList = el("ul", "method-list");
        details.append(summary, methodsList);
        card.append(head, desc, stateEl, details);
        cardsContainer.append(card);
      } else if (card.parentElement !== cardsContainer) {
        cardsContainer.append(card);
      }

      const capMethods = new Set(
        providers.flatMap((p) => {
          const direct = getDirectMethods(p);
          const directMatching = direct.filter((m) => supported.includes(m));
          if (directMatching.length > 0) {
            return directMatching;
          }
          const val = p.direct_methods?.[key];
          if (Array.isArray(val)) return val.filter(Boolean);
          if (typeof val === "string" && val.trim()) return [val.trim()];
          return [];
        })
      );
      const capErrors = providers.flatMap((p) => {
        const errList = getErrorsList(p.errors);
        const match = errList.find(([k]) => k === key);
        return match ? [`${p.name || "搭档"}: ${match[1]}`] : [];
      });

      // Capability state text
      let stateLabel = "未发现直连接口";
      if (!partner) {
        stateLabel = "等待诊断";
      } else if (disabled) {
        stateLabel = "直连已关闭";
      } else if (capErrors.length > 0) {
        stateLabel = `⚠️ 异常：${capErrors[0]}`;
      } else if (capMethods.size > 0) {
        stateLabel = `已发现 ${capMethods.size} 个直连方法`;
      } else if (native) {
        stateLabel = "原生注入路径 · 由搭档配置决定";
      }
      const stateEl = card.querySelector(".capability-state");
      if (stateEl) stateEl.textContent = stateLabel;

      // Update method list items
      const methodsList = card.querySelector(".method-list");
      if (methodsList) {
        methodsList.replaceChildren();
        supported.forEach((method) => {
          const isFound = capMethods.has(method);
          const item = el("li", `method-item ${isFound ? "is-active" : ""}`);
          const code = el("code", "method-name", method);
          const tag = el(
            "span",
            `method-tag ${isFound ? "tag-found" : "tag-compat"}`,
            isFound ? "● 已匹配" : "○ 兼容"
          );
          item.append(code, tag);
          methodsList.append(item);
        });
      }
    }
  }

  // Provider cards
  const container = $("integrationProviders");
  if (container) {
    const keys = new Set();
    providers.forEach((p, index) => {
      let key = p.name ? String(p.name) : `provider-${index}`;
      if (keys.has(key)) {
        key = `${key}__${index}`;
      }
      keys.add(key);
      let row = providersByKey.get(key);
      if (!row) {
        row = el("details", "provider-card");
        row.open = true;
        const summary = el("summary", "provider-summary");
        summary.setAttribute("aria-expanded", "true");
        row.addEventListener("toggle", () => {
          summary.setAttribute("aria-expanded", row.open ? "true" : "false");
        });
        const leftWrap = el("div", "provider-header-left");
        const nameEl = el("span", "provider-name");
        const modeBadge = el("span", "provider-mode-badge");
        leftWrap.append(nameEl, modeBadge);
        const stateEl = el("span", "provider-state");
        summary.append(leftWrap, stateEl);
        const body = el("div", "provider-content");
        row.append(summary, body);
        providersByKey.set(key, row);
      }
      const current = container.children[index];
      if (current !== row) {
        container.insertBefore(row, current || null);
      }

      // Header info
      const nameEl = row.querySelector(".provider-name");
      if (nameEl) nameEl.textContent = p.name || "未命名搭档";
      const modeBadge = row.querySelector(".provider-mode-badge");
      if (modeBadge) {
        modeBadge.textContent =
          p.mode === "native_hooks" || list(p.native_hooks).length > 0
            ? "原生钩子注入"
            : "直连 API 模式";
      }

      const errors = getErrorsList(p.errors);
      const stateEl = row.querySelector(".provider-state");
      if (stateEl) {
        stateEl.textContent = errors.length
          ? `${errors.length} 项异常`
          : p.ready === false
          ? "初始化中"
          : list(p.native_hooks).length > 0
          ? "原生钩子"
          : "直连适配";
      }
      row.dataset.state = errors.length
        ? "degraded"
        : p.ready === false
        ? "unknown"
        : "connected";

      // Body content
      const body = row.querySelector(".provider-content");
      if (body) {
        body.replaceChildren();

        // 1. Error Banner if any
        if (errors.length > 0) {
          const errorBanner = el("div", "integration-diagnostic-banner error-banner");
          const errorTitle = el(
            "strong",
            "banner-title",
            `⚠️ 接口调用异常（${errors.length} 项）`
          );
          errorBanner.append(errorTitle);
          errors.forEach(([cap, err]) => {
            const rowItem = el("div", "error-item");
            const capName = CAPABILITY_NAMES[cap] || cap;
            rowItem.append(
              el("span", "error-cap-name", `${capName}：`),
              el("span", "error-detail", String(err))
            );
            errorBanner.append(rowItem);
          });
          body.append(errorBanner);
        }

        // 2. Initializing Banner if ready === false
        if (p.ready === false) {
          const initBanner = el("div", "integration-diagnostic-banner init-banner");
          initBanner.append(
            el(
              "p",
              "",
              "ℹ️ 搭档正在初始化中：内部知识库或模型仍在载入，就绪后将自动加载对应接口。"
            )
          );
          body.append(initBanner);
        }

        // 3. Category cards grid
        const categoryGrid = el("div", "provider-category-grid");
        CATEGORIES.forEach((cat) => {
          const methods = cat.getMethods(p);
          const catCard = el("div", "provider-category-card");
          const catHeader = el("div", "category-card-header");
          const titleGroup = el("div", "category-title-group");
          titleGroup.append(
            el("span", "category-icon", cat.icon),
            el("span", "category-title", cat.title)
          );
          const badge = el(
            "span",
            `category-badge ${methods.length ? "badge-active" : ""}`,
            methods.length ? `${methods.length} 个入口` : "未发现"
          );
          catHeader.append(titleGroup, badge);
          const catDesc = el("p", "category-desc", cat.desc);
          const chipsContainer = el("div", "category-chips");
          if (methods.length > 0) {
            methods.forEach((m) => {
              const chip = el("code", "method-chip");
              String(m).split(/(?<=_)/).forEach((part, index) => {
                if (index) chip.append(document.createElement("wbr"));
                chip.append(document.createTextNode(part));
              });
              chipsContainer.append(chip);
            });
          } else {
            const nativeAlternative = cat.key === "direct_methods" && list(p.native_hooks).length > 0;
            chipsContainer.append(el("span", "method-chip-empty", nativeAlternative
              ? "使用原生请求钩子，不需要另挂直连接口。" : cat.emptyText));
          }
          catCard.append(catHeader, catDesc, chipsContainer);
          categoryGrid.append(catCard);
        });
        body.append(categoryGrid);
      }
    });

    // Remove deleted providers
    for (const [key, row] of providersByKey) {
      if (!keys.has(key)) {
        row.remove();
        providersByKey.delete(key);
      }
    }
  }

  // Empty state handling
  const emptyEl = $("integrationEmpty");
  if (emptyEl) {
    emptyEl.hidden = providers.length > 0;
    if (providers.length === 0) {
      let emptyMsg =
        "尚未发现启用的记忆搭档。请检查 SelfLearning / LivingMemory 是否已安装并启用。";
      if (!partner) {
        emptyMsg = "尚未取得接口诊断；连接恢复后自动更新。";
      } else if (disabled) {
        emptyMsg =
          "自学习互联已在配置中停用。可在插件参数配置中开启 selflearning_integration。";
      } else if (partnerErrors.length > 0) {
        emptyMsg = `⚠️ 互联异常：${partnerErrors.map(([k, v]) => `${k}：${v}`).join("；")}`;
      } else if (isInitializing) {
        emptyMsg = "记忆搭档正在初始化内部知识库或模型，准备完成后将自动加载接口…";
      }
      emptyEl.textContent = emptyMsg;
    }
  }
}
