import { msg, tr, setText } from "../shared/series-ui.js";
import { friendlyError } from "./errors.js";

const VALID_WINDOWS = ["1h", "24h", "7d", "30d"];

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function num(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

function formatCompact(value) {
  const n = num(value);
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 10_000) return `${(n / 1_000).toFixed(1)}K`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(2)}K`;
  return String(Math.round(n));
}

function formatPct(rate, hasDenominator = true) {
  if (!hasDenominator) return "—";
  const r = Math.max(0, Math.min(1, num(rate)));
  return `${(r * 100).toFixed(1)}%`;
}

function formatMs(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return "—";
  if (n >= 1000) return `${(n / 1000).toFixed(2)}s`;
  return `${Math.round(n)}ms`;
}

function formatBucketShort(bucket, granularity) {
  const text = String(bucket || "").trim();
  if (!text) return "";
  if (granularity === "day") {
    return text.length >= 10 ? text.slice(5, 10) : text;
  }
  // e.g. "2026-11-22 15:00" or ISO "2026-11-22T15:00:00Z"
  const m = text.match(/(\d{2}-\d{2})[ T](\d{2}:\d{2})/);
  if (m) return `${m[1]} ${m[2]}`;
  return text.length > 11 ? text.slice(-11) : text;
}

function formatGeneratedAt(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return "";
  try {
    return new Date(n * (n > 1e12 ? 1 : 1000)).toLocaleTimeString("zh-CN", {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return "";
  }
}

export function mountSeriesTelemetry({ apiGet }) {
  const root = document.getElementById("view-telemetry");
  if (!root) return { load: async () => {}, isLoaded: () => false };

  const statusEl = document.getElementById("telemetryStatus");
  const btnRefresh = document.getElementById("btnTelemetryRefresh");
  const windowBtns = [...root.querySelectorAll("[data-telemetry-window]")];
  const metricBtns = [...root.querySelectorAll("[data-telemetry-metric]")];

  const statCalls = document.getElementById("telemetryStatCalls");
  const statCallsHint = document.getElementById("telemetryStatCallsHint");
  const statTokens = document.getElementById("telemetryStatTokens");
  const statTokensHint = document.getElementById("telemetryStatTokensHint");
  const statCache = document.getElementById("telemetryStatCache");
  const statCacheHint = document.getElementById("telemetryStatCacheHint");
  const statQueue = document.getElementById("telemetryStatQueue");
  const statQueueHint = document.getElementById("telemetryStatQueueHint");

  const emptyEl = document.getElementById("telemetryEmpty");
  const trendCard = document.getElementById("telemetryTrendCard");
  const trendChart = document.getElementById("telemetryTrendChart");
  const trendMeta = document.getElementById("telemetryTrendMeta");
  const trendAxis = document.getElementById("telemetryTrendAxis");

  const pluginBody = document.getElementById("telemetryPluginTableBody");
  const providerBody = document.getElementById("telemetryProviderTableBody");
  const turnLatencyCard = document.getElementById("telemetryTurnLatencyCard");
  const turnLatencyMeta = document.getElementById("telemetryTurnLatencyMeta");
  const turnLatencyDegraded = document.getElementById("telemetryTurnLatencyDegraded");
  const turnLatencyBody = document.getElementById("telemetryTurnLatencyBody");

  let activeWindow = "24h";
  let chartMetric = "tokens"; // "tokens" | "calls"
  let data = null;
  let loading = false;
  let loadedOnce = false;
  let lastError = "";
  let requestSeq = 0;

  function setStatus(text, isError = false) {
    if (!statusEl) return;
    setText(statusEl, text || "");
    statusEl.classList.toggle("error", Boolean(isError));
  }

  function syncToolbarButtons() {
    for (const btn of windowBtns) {
      const active = btn.dataset.telemetryWindow === activeWindow;
      btn.setAttribute("aria-pressed", String(active));
      btn.classList.toggle("is-active", active);
      btn.disabled = loading;
    }
    for (const btn of metricBtns) {
      const active = btn.dataset.telemetryMetric === chartMetric;
      btn.setAttribute("aria-pressed", String(active));
      btn.classList.toggle("is-active", active);
    }
    if (btnRefresh) {
      btnRefresh.disabled = loading;
      btnRefresh.setAttribute("aria-busy", loading ? "true" : "false");
    }
    root.setAttribute("aria-busy", loading ? "true" : "false");
  }

  function renderTrend(timeline) {
    if (!trendChart || !trendMeta || !trendAxis) return;
    const rows = Array.isArray(timeline) ? timeline : [];
    if (!rows.length) {
      trendMeta.textContent = tr("暂无趋势数据");
      trendChart.innerHTML = `<div class="telemetry-chart-empty">${escapeHtml(tr("当前时间窗口内暂无调用趋势记录。"))}</div>`;
      trendAxis.innerHTML = "";
      return;
    }

    const totalTokensAll = rows.reduce((acc, r) => acc + num(r.input_tokens) + num(r.output_tokens), 0);
    const effectiveMetric = chartMetric === "tokens" && totalTokensAll > 0 ? "tokens" : "calls";

    const values = rows.map((r) =>
      effectiveMetric === "tokens" ? num(r.input_tokens) + num(r.output_tokens) : num(r.calls)
    );
    const maxVal = Math.max(1, ...values);
    const width = 640;
    const height = 148;
    const padTop = 14;
    const padBottom = 6;
    const usableH = height - padTop - padBottom;
    const slotW = width / rows.length;
    const barW = Math.max(4, Math.min(28, slotW * 0.68));

    const barsSvg = rows
      .map((row, idx) => {
        const val = values[idx];
        const barH = val > 0 ? Math.max(3, (val / maxVal) * usableH) : 1.5;
        const x = idx * slotW + (slotW - barW) / 2;
        const y = height - padBottom - barH;

        const inTok = num(row.input_tokens);
        const outTok = num(row.output_tokens);
        const cachedTok = num(row.cached_tokens);
        const calls = num(row.calls);
        const errs = num(row.error_calls);
        const cachePct = inTok > 0 ? formatPct(row.cache_hit_rate, true) : "—";
        const tooltip = [
          row.bucket || "",
          `${tr("调用")} ${calls} (${tr("错误")} ${errs})`,
          `Token ${formatCompact(inTok + outTok)} (${tr("输入")} ${formatCompact(inTok)} / ${tr("输出")} ${formatCompact(outTok)})`,
          `${tr("缓存命中")} ${formatCompact(cachedTok)} (${cachePct})`,
          `${tr("平均生成")} ${formatMs(row.avg_generation_ms)}`,
        ]
          .filter(Boolean)
          .join(" · ");

        let cachedOverlay = "";
        if (effectiveMetric === "tokens" && val > 0 && cachedTok > 0) {
          const cachedH = Math.min(barH, Math.max(2, (cachedTok / maxVal) * usableH));
          const cy = height - padBottom - cachedH;
          cachedOverlay = `<rect class="tt-bar-cached" x="${x.toFixed(1)}" y="${cy.toFixed(1)}" width="${barW.toFixed(1)}" height="${cachedH.toFixed(1)}" rx="2"></rect>`;
        }

        const errDot =
          errs > 0
            ? `<circle class="tt-bar-err" cx="${(x + barW / 2).toFixed(1)}" cy="${Math.max(5, y - 5).toFixed(1)}" r="3"></circle>`
            : "";

        return `<g class="tt-bar-group">
          <title>${escapeHtml(tooltip)}</title>
          <rect class="tt-bar-main${val === 0 ? " is-zero" : ""}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${barH.toFixed(1)}" rx="2"></rect>
          ${cachedOverlay}
          ${errDot}
        </g>`;
      })
      .join("");

    const gridLines = [0.25, 0.5, 0.75]
      .map((ratio) => {
        const y = (padTop + usableH * (1 - ratio)).toFixed(1);
        return `<line class="tt-grid-line" x1="0" y1="${y}" x2="${width}" y2="${y}"></line>`;
      })
      .join("");

    const ariaLabel =
      effectiveMetric === "tokens"
        ? tr("最近 {0} 窗口的 Token 用量趋势（峰值 {1}）", activeWindow, formatCompact(maxVal))
        : tr("最近 {0} 窗口的调用次数趋势（峰值 {1}）", activeWindow, formatCompact(maxVal));

    trendMeta.textContent =
      effectiveMetric === "tokens"
        ? `${tr("峰值")} ${formatCompact(maxVal)} Token · ${rows.length} ${tr("个时段")}`
        : `${tr("峰值")} ${formatCompact(maxVal)} ${tr("次")} · ${rows.length} ${tr("个时段")}`;

    trendChart.innerHTML = `<svg class="telemetry-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="${escapeHtml(ariaLabel)}">
      ${gridLines}
      ${barsSvg}
    </svg>`;

    const first = rows[0];
    const mid = rows.length >= 3 ? rows[Math.floor(rows.length / 2)] : null;
    const last = rows[rows.length - 1];
    trendAxis.innerHTML = [
      `<span>${escapeHtml(formatBucketShort(first?.bucket, first?.granularity))}</span>`,
      mid ? `<span>${escapeHtml(formatBucketShort(mid?.bucket, mid?.granularity))}</span>` : "",
      `<span>${escapeHtml(formatBucketShort(last?.bucket, last?.granularity))}</span>`,
    ]
      .filter(Boolean)
      .join("");
  }

  function renderBreakdownTable(tbody, rows, { idKey, labelKey, liveMap }) {
    if (!tbody) return;
    const list = Array.isArray(rows) ? rows : [];
    if (!list.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="telemetry-td-empty">${escapeHtml(tr("暂无明细记录"))}</td></tr>`;
      return;
    }

    tbody.innerHTML = list
      .map((row) => {
        const rawId = String(row?.[idKey] || "unknown");
        const display = labelKey && row?.[labelKey] ? String(row[labelKey]) : rawId;
        const calls = num(row?.calls);
        const errCalls = num(row?.error_calls);
        const inTok = num(row?.input_tokens);
        const outTok = num(row?.output_tokens);
        const cachedTok = num(row?.cached_tokens);
        const cacheRate = formatPct(row?.cache_hit_rate, inTok > 0);
        const p50 = formatMs(row?.p50_generation_ms || row?.avg_generation_ms);
        const p95 = formatMs(row?.p95_generation_ms);
        const qP95 = formatMs(row?.p95_queue_ms || row?.avg_queue_ms);
        const live = liveMap && typeof liveMap === "object" ? liveMap[rawId] : null;
        const liveActive = num(live?.active);
        const liveQueued = num(live?.queued);
        const liveBadge =
          liveActive > 0 || liveQueued > 0
            ? `<span class="telemetry-live-pill" title="${escapeHtml(tr("实时活跃 {0} / 排队 {1}", liveActive, liveQueued))}">${liveActive}/${liveQueued}</span>`
            : "";

        return `<tr>
          <td class="telemetry-td-name">
            <div class="telemetry-name-row">
              <strong>${escapeHtml(display)}</strong>
              ${liveBadge}
            </div>
            ${display !== rawId ? `<span class="telemetry-sub-id">${escapeHtml(rawId)}</span>` : ""}
          </td>
          <td class="tabular-nums">${escapeHtml(formatCompact(calls))}</td>
          <td class="tabular-nums" title="${escapeHtml(`${inTok.toLocaleString()} / ${outTok.toLocaleString()}`)}">
            ${escapeHtml(formatCompact(inTok))} <span class="telemetry-sep">/</span> ${escapeHtml(formatCompact(outTok))}
          </td>
          <td class="tabular-nums" title="${escapeHtml(`${tr("缓存命中")} ${cachedTok.toLocaleString()}`)}">
            <strong>${escapeHtml(cacheRate)}</strong>
            <span class="telemetry-sub-metric">${escapeHtml(formatCompact(cachedTok))}</span>
          </td>
          <td class="tabular-nums" title="${escapeHtml(`${tr("排队 P95")}: ${qP95}`)}">
            ${escapeHtml(p50)} <span class="telemetry-sep">/</span> ${escapeHtml(p95)}
          </td>
          <td class="tabular-nums${errCalls > 0 ? " is-error-cell" : ""}">
            ${escapeHtml(String(errCalls))}${calls > 0 && errCalls > 0 ? ` <span class="telemetry-sub-metric">(${escapeHtml(formatPct(row?.error_rate, true))})</span>` : ""}
          </td>
        </tr>`;
      })
      .join("");
  }

  const TURN_STAGE_LABELS = {
    debounce: "防抖聚合",
    graph: "图谱与路由",
    decision: "参与抉择 (Jev/LLM)",
    vibe: "氛围校准",
    context: "上下文拼装",
    reply: "回复生成",
    first_send: "首句发出（端到端）",
    send: "消息发送",
  };

  function renderTurnLatency(turnLatency) {
    if (!turnLatencyBody) return;
    const tl = turnLatency && typeof turnLatency === "object" ? turnLatency : {};
    const stages = tl.stages && typeof tl.stages === "object" ? tl.stages : {};
    const entries = Object.entries(stages);
    const sampledNodes = num(tl.sampled_nodes);
    const degradedMap = tl.degraded && typeof tl.degraded === "object" ? tl.degraded : {};
    const degradedEntries = Object.entries(degradedMap).filter(([, c]) => num(c) > 0);

    if (turnLatencyMeta) {
      turnLatencyMeta.textContent = sampledNodes > 0
        ? `${tr("最近采样")} ${sampledNodes} ${tr("条节点")} · ${entries.length} ${tr("个阶段")}`
        : tr("暂无近期回合采样");
    }
    if (turnLatencyDegraded) {
      turnLatencyDegraded.textContent = degradedEntries.length
        ? `${tr("降级记录")}：${degradedEntries.map(([k, v]) => `${k} (${v})`).join(" · ")}`
        : "";
    }
    if (turnLatencyCard) {
      turnLatencyCard.classList.toggle("hidden", false);
    }
    if (!entries.length) {
      turnLatencyBody.innerHTML = `<tr><td colspan="4" class="telemetry-td-empty">${escapeHtml(tr("暂无分阶段耗时样本，产生群聊回复后自动汇总。"))}</td></tr>`;
      return;
    }
    turnLatencyBody.innerHTML = entries
      .map(([stageKey, stats]) => {
        const label = TURN_STAGE_LABELS[stageKey] || stageKey;
        const count = num(stats?.count);
        const p50 = formatMs(num(stats?.p50_seconds) * 1000);
        const p95 = formatMs(num(stats?.p95_seconds) * 1000);
        return `<tr>
          <td class="telemetry-td-name"><strong>${escapeHtml(label)}</strong>${label !== stageKey ? `<span class="telemetry-sub-id">${escapeHtml(stageKey)}</span>` : ""}</td>
          <td class="tabular-nums">${escapeHtml(String(count))}</td>
          <td class="tabular-nums">${escapeHtml(p50)}</td>
          <td class="tabular-nums">${escapeHtml(p95)}</td>
        </tr>`;
      })
      .join("");
  }

  function render() {
    syncToolbarButtons();
    const safe = data && typeof data === "object" ? data : null;
    const summary = safe?.summary && typeof safe.summary === "object" ? safe.summary : {};
    const live = safe?.live_queue && typeof safe.live_queue === "object" ? safe.live_queue : {};
    const byPlugin = Array.isArray(safe?.by_plugin) ? safe.by_plugin : [];
    const byProvider = Array.isArray(safe?.by_provider) ? safe.by_provider : [];
    const timeline = Array.isArray(safe?.timeline) ? safe.timeline : [];

    const calls = num(summary.calls);
    const okCalls = num(summary.ok_calls);
    const errCalls = num(summary.error_calls);
    const missingUsage = num(summary.usage_missing_calls);
    const inTok = num(summary.input_tokens);
    const outTok = num(summary.output_tokens);
    const totalTok = num(summary.total_tokens) || inTok + outTok;
    const cachedTok = num(summary.cached_tokens);
    const cacheCreateTok = num(summary.cache_creation_tokens);

    const active = num(live.active);
    const queued = num(live.queued);
    const capacity = num(live.capacity);
    const maxQueue = num(live.max_queue);

    if (statCalls) {
      statCalls.textContent = safe ? formatCompact(calls) : "—";
      statCalls.title = safe ? calls.toLocaleString() : "";
    }
    if (statCallsHint) {
      if (!safe) {
        setText(statCallsHint, msg("等待遥测数据"));
      } else {
        const parts = [`${tr("成功")} ${okCalls}`, `${tr("失败")} ${errCalls}`];
        if (missingUsage > 0) parts.push(`${tr("无 usage")} ${missingUsage}`);
        statCallsHint.textContent = parts.join(" · ");
      }
    }

    if (statTokens) {
      statTokens.textContent = safe ? formatCompact(totalTok) : "—";
      statTokens.title = safe ? `${tr("输入")} ${inTok.toLocaleString()} / ${tr("输出")} ${outTok.toLocaleString()}` : "";
    }
    if (statTokensHint) {
      statTokensHint.textContent = safe
        ? `${tr("输入")} ${formatCompact(inTok)} · ${tr("输出")} ${formatCompact(outTok)}`
        : tr("输入 / 输出 Token");
    }

    if (statCache) {
      statCache.textContent = safe ? formatPct(summary.cache_hit_rate, inTok > 0) : "—";
    }
    if (statCacheHint) {
      if (!safe) {
        setText(statCacheHint, msg("缓存命中 / 输入"));
      } else if (inTok === 0) {
        setText(statCacheHint, msg("暂无输入 Token 样本"));
      } else {
        const extra = cacheCreateTok > 0 ? ` · ${tr("写入")} ${formatCompact(cacheCreateTok)}` : "";
        statCacheHint.textContent = `${tr("命中")} ${formatCompact(cachedTok)} / ${formatCompact(inTok)}${extra}`;
      }
    }

    if (statQueue) {
      statQueue.textContent = safe ? `${active} / ${queued}` : "—";
    }
    if (statQueueHint) {
      if (!safe) {
        setText(statQueueHint, msg("实时活跃 / 排队"));
      } else {
        const errPct = formatPct(summary.error_rate, calls > 0);
        const capPart = capacity > 0 ? `${tr("并发池")} ${capacity}${maxQueue > 0 ? `/${maxQueue}` : ""}` : tr("实时队列");
        statQueueHint.textContent = `${capPart} · ${tr("失败率")} ${errPct}`;
      }
    }

    const isEmpty = Boolean(safe) && calls === 0 && byPlugin.length === 0 && byProvider.length === 0;
    if (emptyEl) emptyEl.classList.toggle("hidden", !isEmpty);
    if (trendCard) trendCard.classList.toggle("hidden", !safe || isEmpty);

    renderTrend(timeline);
    renderBreakdownTable(pluginBody, byPlugin, {
      idKey: "plugin_id",
      labelKey: "label",
      liveMap: live.by_plugin,
    });
    renderBreakdownTable(providerBody, byProvider, {
      idKey: "provider_id",
      labelKey: null,
      liveMap: live.by_provider,
    });
    renderTurnLatency(safe?.turn_latency);
  }

  async function load({ quiet = false, windowOverride = null } = {}) {
    if (windowOverride && VALID_WINDOWS.includes(windowOverride)) {
      activeWindow = windowOverride;
    }
    const seq = ++requestSeq;
    loading = true;
    lastError = "";
    syncToolbarButtons();
    if (!quiet) {
      setStatus(msg("正在读取系列运行与 Token 遥测…"));
    }
    try {
      const res = await apiGet("series_telemetry", { window: activeWindow });
      if (seq !== requestSeq) return;
      data = res && typeof res === "object" && ("summary" in res || "by_plugin" in res || "live_queue" in res)
        ? res
        : {
            window: activeWindow,
            retention_days: 30,
            summary: { calls: 0, ok_calls: 0, error_calls: 0, error_rate: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0, cached_tokens: 0, cache_hit_rate: 0 },
            by_plugin: [],
            by_provider: [],
            timeline: [],
            live_queue: { capacity: 0, active: 0, queued: 0, by_plugin: {}, by_provider: {} },
          };
      loadedOnce = true;
      const genAt = formatGeneratedAt(data.generated_at);
      const retention = num(data.retention_days) || 30;
      setStatus(
        genAt
          ? `${tr("窗口")} ${activeWindow} · ${tr("更新于")} ${genAt} · ${tr("保留近 {0} 天聚合桶", retention)}`
          : `${tr("窗口")} ${activeWindow} · ${tr("保留近 {0} 天聚合桶", retention)}`
      );
    } catch (err) {
      if (seq !== requestSeq) return;
      lastError = friendlyError(err, "读取运行遥测失败，请点击刷新重试。");
      setStatus(lastError, true);
    } finally {
      if (seq === requestSeq) {
        loading = false;
        render();
      }
    }
  }

  for (const btn of windowBtns) {
    btn.addEventListener("click", () => {
      const next = btn.dataset.telemetryWindow || "24h";
      if (!VALID_WINDOWS.includes(next)) return;
      if (next === activeWindow && loadedOnce && !lastError) return;
      void load({ windowOverride: next });
    });
  }

  for (const btn of metricBtns) {
    btn.addEventListener("click", () => {
      const next = btn.dataset.telemetryMetric === "calls" ? "calls" : "tokens";
      if (next === chartMetric) return;
      chartMetric = next;
      render();
    });
  }

  btnRefresh?.addEventListener("click", () => {
    void load();
  });

  window.addEventListener("console:viewchange", (event) => {
    if (event?.detail?.view === "telemetry" && !loadedOnce) {
      void load();
    }
  });

  window.addEventListener("series-languagechange", () => {
    if (loadedOnce) render();
  });

  render();

  return {
    load,
    isLoaded: () => loadedOnce,
    getWindow: () => activeWindow,
  };
}
