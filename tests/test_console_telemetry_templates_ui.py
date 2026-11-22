"""Playwright browser tests for Console Telemetry, Matrix Batch Actions & Template Manager, and Setup Template Onboarding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .test_ui_theme_browser import browser as browser, page_server as page_server, setup

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("width,theme", [(1440, "day"), (390, "night")])
def test_console_telemetry_section_windows_charts_empty_and_error(browser, page_server, width, theme):
    with browser.new_context(viewport={"width": width, "height": 900}) as context:
        setup(context, {"ui": theme})
        context.add_init_script("""(() => {
          const api = window.AstrBotPluginPage;
          const origGet = api.apiGet;
          window.__telemetryCalls = [];
          window.__telemetryMode = "normal";
          api.apiGet = async (endpoint, params = {}) => {
            if (endpoint === "series_telemetry") {
              window.__telemetryCalls.push({ window: params.window || "24h" });
              if (window.__telemetryMode === "error") {
                throw new Error("遥测存储暂时不可用");
              }
              if (window.__telemetryMode === "empty") {
                return {
                  ok: true,
                  data: {
                    window: params.window || "24h",
                    generated_at: 1732000000,
                    retention_days: 30,
                    summary: {
                      calls: 0, ok_calls: 0, error_calls: 0, error_rate: 0,
                      usage_missing_calls: 0, input_tokens: 0, output_tokens: 0,
                      total_tokens: 0, cached_tokens: 0, cache_creation_tokens: 0,
                      cache_hit_rate: 0, avg_queue_ms: 0, avg_generation_ms: 0,
                      p50_queue_ms: 0, p95_queue_ms: 0, p50_generation_ms: 0, p95_generation_ms: 0
                    },
                    by_plugin: [],
                    by_provider: [],
                    timeline: [],
                    live_queue: { capacity: 4, background_capacity: 3, max_queue: 128, active: 0, queued: 0, by_provider: {}, by_plugin: {} }
                  }
                };
              }
              const mult = params.window === "7d" ? 7 : params.window === "30d" ? 30 : params.window === "1h" ? 1 : 2;
              return {
                ok: true,
                data: {
                  window: params.window || "24h",
                  generated_at: 1732000000,
                  retention_days: 30,
                  summary: {
                    calls: 60 * mult,
                    ok_calls: 59 * mult,
                    error_calls: 1 * mult,
                    error_rate: 1 / 60,
                    usage_missing_calls: 2,
                    input_tokens: 40000 * mult,
                    output_tokens: 10000 * mult,
                    total_tokens: 50000 * mult,
                    cached_tokens: 18000 * mult,
                    cache_creation_tokens: 1200,
                    cache_hit_rate: 0.45,
                    avg_queue_ms: 14,
                    avg_generation_ms: 620,
                    p50_queue_ms: 6,
                    p95_queue_ms: 42,
                    p50_generation_ms: 510,
                    p95_generation_ms: 1480
                  },
                  by_plugin: [
                    {
                      plugin_id: "astrbot_plugin_chat_dynamics",
                      label: "对话主控",
                      calls: 40 * mult, ok_calls: 40 * mult, error_calls: 0, error_rate: 0,
                      input_tokens: 28000 * mult, output_tokens: 7000 * mult, total_tokens: 35000 * mult,
                      cached_tokens: 14000 * mult, cache_hit_rate: 0.5,
                      p50_generation_ms: 480, p95_generation_ms: 1250, p95_queue_ms: 20
                    },
                    {
                      plugin_id: "astrbot_plugin_dynamics_world",
                      label: "群间世界",
                      calls: 20 * mult, ok_calls: 19 * mult, error_calls: 1 * mult, error_rate: 0.05,
                      input_tokens: 12000 * mult, output_tokens: 3000 * mult, total_tokens: 15000 * mult,
                      cached_tokens: 4000 * mult, cache_hit_rate: 0.333,
                      p50_generation_ms: 720, p95_generation_ms: 1800, p95_queue_ms: 55
                    }
                  ],
                  by_provider: [
                    {
                      provider_id: "openai/gpt-4o-mini",
                      calls: 60 * mult, ok_calls: 59 * mult, error_calls: 1 * mult, error_rate: 1 / 60,
                      input_tokens: 40000 * mult, output_tokens: 10000 * mult, total_tokens: 50000 * mult,
                      cached_tokens: 18000 * mult, cache_hit_rate: 0.45,
                      p50_generation_ms: 510, p95_generation_ms: 1480, p95_queue_ms: 42
                    }
                  ],
                  timeline: [
                    { bucket: "2026-11-22 13:00", granularity: "hour", calls: 15 * mult, error_calls: 0, input_tokens: 10000 * mult, output_tokens: 2500 * mult, cached_tokens: 4500 * mult, cache_hit_rate: 0.45, avg_generation_ms: 510 },
                    { bucket: "2026-11-22 14:00", granularity: "hour", calls: 25 * mult, error_calls: 1, input_tokens: 18000 * mult, output_tokens: 4500 * mult, cached_tokens: 8000 * mult, cache_hit_rate: 0.44, avg_generation_ms: 680 },
                    { bucket: "2026-11-22 15:00", granularity: "hour", calls: 20 * mult, error_calls: 0, input_tokens: 12000 * mult, output_tokens: 3000 * mult, cached_tokens: 5500 * mult, cache_hit_rate: 0.46, avg_generation_ms: 530 }
                  ],
                  live_queue: {
                    capacity: 4, background_capacity: 3, max_queue: 128, active: 2, queued: 1,
                    by_provider: { "openai/gpt-4o-mini": { active: 2, background_active: 1, queued: 1 } },
                    by_plugin: { "astrbot_plugin_chat_dynamics": { active: 1, queued: 0 }, "astrbot_plugin_dynamics_world": { active: 1, queued: 1 } }
                  }
                }
              };
            }
            return origGet(endpoint, params);
          };
        })();""")

        page = context.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(f"{page_server}/console/index.html?ui={theme}")

        # Lazy load only when tab clicked
        assert page.evaluate("window.__telemetryCalls.length") == 0
        page.locator("#tab-telemetry").click()
        page.locator("#telemetrySection").wait_for(state="visible")
        page.wait_for_function("window.__telemetryCalls.length === 1")
        assert page.evaluate("window.__telemetryCalls[0].window") == "24h"

        assert page.locator("#telemetryStatCalls").inner_text() == "120"
        assert page.locator("#telemetryStatTokens").inner_text() == "100.0K"
        assert page.locator("#telemetryStatCache").inner_text() == "45.0%"
        assert page.locator("#telemetryStatQueue").inner_text() == "2 / 1"
        assert page.locator("#telemetryTrendChart svg").count() == 1
        assert "对话主控" in page.locator("#telemetryPluginTableBody").inner_text()
        assert "群间世界" in page.locator("#telemetryPluginTableBody").inner_text()
        assert "openai/gpt-4o-mini" in page.locator("#telemetryProviderTableBody").inner_text()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")

        # Switch chart metric to calls
        page.locator('[data-telemetry-metric="calls"]').click()
        assert page.locator('[data-telemetry-metric="calls"]').get_attribute("aria-pressed") == "true"
        assert page.locator("#telemetryTrendChart svg").count() == 1

        # Switch window to 7d
        page.locator('[data-telemetry-window="7d"]').click()
        page.wait_for_function("window.__telemetryCalls.length === 2")
        assert page.evaluate("window.__telemetryCalls[1].window") == "7d"
        assert page.locator("#telemetryStatCalls").inner_text() == "420"

        # Empty state
        page.evaluate('window.__telemetryMode = "empty"')
        page.locator("#btnTelemetryRefresh").click()
        page.wait_for_function("window.__telemetryCalls.length === 3")
        page.locator("#telemetryEmpty").wait_for(state="visible")
        assert page.locator("#telemetryStatCalls").inner_text() == "0"

        # Error state
        page.evaluate('window.__telemetryMode = "error"')
        page.locator("#btnTelemetryRefresh").click()
        page.wait_for_function("window.__telemetryCalls.length === 4")
        assert "遥测存储暂时不可用" in page.locator("#telemetryStatus").inner_text()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        assert errors == []


@pytest.mark.parametrize("width,theme", [(1440, "day"), (390, "night")])
def test_console_matrix_batch_excluded_badge_and_template_crud(browser, page_server, width, theme):
    with browser.new_context(viewport={"width": width, "height": 900}) as context:
        setup(context, {"ui": theme})
        context.add_init_script("""(() => {
          const api = window.AstrBotPluginPage;
          const origGet = api.apiGet;
          const origPost = api.apiPost;
          window.__batchCalls = [];
          window.__templatePosts = [];
          window.__forceConflictOnce = false;
          window.DynamicsUI_confirm_override = true;

          let versionNum = 1;
          let cdEnabled = true;
          let dmEnabled = false;
          const plugins = [
            { id: "astrbot_plugin_chat_dynamics", label: "对话主控", mark: "CD", page: "app", state: "ready", can_open: true, supports_toggle: true },
            { id: "astrbot_plugin_dynamics_memory", label: "群间记忆", mark: "DM", page: "dashboard", state: "ready", can_open: true, supports_toggle: true },
            { id: "astrbot_plugin_dynamics_world", label: "群间世界", mark: "DW", page: "world", state: "ready", can_open: true, supports_toggle: false },
            { id: "astrbot_plugin_visual_media", label: "视觉工坊", mark: "VM", page: "workshop", state: "missing", can_open: false, supports_toggle: false }
          ];
          let templates = [
            {
              id: "full_companion",
              name: "全能陪伴群",
              description: "8 插件全开",
              builtin: true,
              readonly: true,
              strategy_hint: "推荐常用熟人群",
              states: {
                astrbot_plugin_chat_dynamics: true,
                astrbot_plugin_dynamics_memory: true,
                astrbot_plugin_dynamics_world: true,
                astrbot_plugin_visual_media: true
              }
            },
            {
              id: "core_only",
              name: "仅主控对话",
              description: "仅开主控",
              builtin: true,
              readonly: true,
              strategy_hint: "",
              states: {
                astrbot_plugin_chat_dynamics: true,
                astrbot_plugin_dynamics_memory: false,
                astrbot_plugin_dynamics_world: false,
                astrbot_plugin_visual_media: false
              }
            }
          ];

          function makeMatrixRow() {
            const v = "v" + versionNum;
            return {
              umo: "qq:GroupMessage:10001",
              group_id: "10001",
              label: "星海茶话会",
              persona_id: "夏目安安",
              version: "row-" + v,
              cells: {
                astrbot_plugin_chat_dynamics: { enabled: cdEnabled, supported: true, state: "ready", summary: cdEnabled ? "开启" : "暂停", version: v, can_open: true, excluded_by_core: false },
                astrbot_plugin_dynamics_memory: { enabled: dmEnabled, supported: true, state: "ready", summary: dmEnabled ? "开启" : "暂停", version: v, can_open: true, excluded_by_core: false },
                astrbot_plugin_dynamics_world: { enabled: false, supported: false, state: "ready", summary: "主控排除", version: "", can_open: true, excluded_by_core: true },
                astrbot_plugin_visual_media: { enabled: null, supported: false, state: "missing", summary: "未安装", version: "", can_open: false, excluded_by_core: false }
              }
            };
          }

          api.apiGet = async (endpoint, params = {}) => {
            if (endpoint === "series_matrix") {
              return { ok: true, data: { version: "m-v" + versionNum, plugins, groups: [makeMatrixRow()] } };
            }
            if (endpoint === "series_templates") {
              return { ok: true, data: { templates: JSON.parse(JSON.stringify(templates)), plugins: plugins.map(({id, label, mark}) => ({id, label, mark})) } };
            }
            return origGet(endpoint, params);
          };

          api.apiPost = async (endpoint, body = {}) => {
            if (endpoint === "series_matrix_batch") {
              window.__batchCalls.push(body);
              if (window.__forceConflictOnce) {
                window.__forceConflictOnce = false;
                throw new Error("409 版本冲突，请刷新");
              }
              let desired = body.states || {};
              if (body.template_id) {
                const tpl = templates.find(t => t.id === body.template_id);
                desired = tpl ? tpl.states : {};
              }
              if ("astrbot_plugin_chat_dynamics" in desired) cdEnabled = Boolean(desired.astrbot_plugin_chat_dynamics);
              if ("astrbot_plugin_dynamics_memory" in desired) dmEnabled = Boolean(desired.astrbot_plugin_dynamics_memory);
              versionNum += 1;
              return {
                ok: true,
                data: {
                  ok: true,
                  umo: body.umo,
                  template_id: body.template_id || "",
                  applied: [
                    { plugin_id: "astrbot_plugin_chat_dynamics", enabled: cdEnabled, version: "v" + versionNum, summary: cdEnabled ? "开启" : "暂停" },
                    { plugin_id: "astrbot_plugin_dynamics_memory", enabled: dmEnabled, version: "v" + versionNum, summary: dmEnabled ? "开启" : "暂停" }
                  ],
                  skipped: [
                    { plugin_id: "astrbot_plugin_visual_media", reason: "missing", message: "未安装" }
                  ],
                  failed: [],
                  matrix_version: "m-v" + versionNum,
                  row: makeMatrixRow()
                }
              };
            }
            if (endpoint === "series_templates") {
              window.__templatePosts.push(body);
              if (body.action === "delete") {
                templates = templates.filter(t => t.id !== body.id);
                return { ok: true, data: { deleted: true, id: body.id, templates: JSON.parse(JSON.stringify(templates)) } };
              }
              const id = body.id || ("custom_" + (templates.length + 1));
              const rec = {
                id,
                name: body.name,
                description: body.description || "",
                strategy_hint: body.strategy_hint || "",
                builtin: false,
                readonly: false,
                states: body.states || {}
              };
              const idx = templates.findIndex(t => t.id === id);
              if (idx >= 0) templates[idx] = rec;
              else templates.push(rec);
              return { ok: true, data: { template: rec, templates: JSON.parse(JSON.stringify(templates)) } };
            }
            return origPost(endpoint, body);
          };
        })();""")

        page = context.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(f"{page_server}/console/index.html?ui={theme}")
        page.locator("#tab-matrix").click()
        page.locator("#matrixTableBody tr").first.wait_for(state="visible")

        # 1. Verify "主控排除" badge/pill rendered clearly
        assert "主控排除" in page.locator("#matrixTableBody").inner_text()
        excluded_pill = page.locator(".matrix-cell-pill.is-excluded").first
        assert excluded_pill.is_visible()
        assert "exclude_groups" in (excluded_pill.get_attribute("title") or "")

        # 2. Row quick action: 一键全开
        page.locator('[data-matrix-batch-mode="all_on"]').first.click()
        page.wait_for_function("window.__batchCalls.length === 1")
        call0 = page.evaluate("window.__batchCalls[0]")
        assert call0["umo"] == "qq:GroupMessage:10001"
        assert call0["expected_version"] == "row-v1"
        assert call0["states"]["astrbot_plugin_dynamics_memory"] is True
        page.locator("#matrixBatchFeedback").wait_for(state="visible")
        assert "已应用 2 / 跳过 1 / 失败 0" in page.locator("#matrixBatchFeedback").inner_text()

        # 3. Row apply template dropdown (uses updated optimistic lock version row-v2!)
        page.locator('[data-matrix-apply-template="1"]').first.select_option("core_only")
        page.wait_for_function("window.__batchCalls.length === 2")
        call1 = page.evaluate("window.__batchCalls[1]")
        assert call1["template_id"] == "core_only"
        assert call1["expected_version"] == "row-v2"

        # 4. Template Manager Modal: list built-in readonly, create, edit, delete custom template
        page.locator("#btnMatrixTemplates").click()
        page.locator("#matrixTemplatesDialog").wait_for(state="visible")
        assert "内置 · 只读" in page.locator("#tplList").inner_text()

        # View built-in -> save button disabled
        page.locator('[data-tpl-view="full_companion"]').click()
        assert page.locator("#btnSaveTemplate").is_disabled()

        # Create custom template
        page.locator("#btnNewTemplate").click()
        page.locator("#tplName").fill("夜间安静群")
        page.locator("#tplDesc").fill("只开主控和记忆")
        page.locator('[data-tpl-plugin="astrbot_plugin_dynamics_memory"]').check()
        page.locator("#btnSaveTemplate").click()
        page.wait_for_function("window.__templatePosts.length === 1")
        assert "夜间安静群" in page.locator("#tplList").inner_text()

        # Edit custom template
        created_id = page.locator('[data-tpl-edit]').first.get_attribute("data-tpl-edit")
        page.locator(f'[data-tpl-edit="{created_id}"]').click()
        page.locator("#tplName").fill("夜间安静群 Pro")
        page.locator("#btnSaveTemplate").click()
        page.wait_for_function("window.__templatePosts.length === 2")
        assert "夜间安静群 Pro" in page.locator("#tplList").inner_text()
        assert page.evaluate("window.__templatePosts[1].id") == created_id

        # Delete custom template (confirm modal bypassed via DynamicsUI.confirm stub)
        page.evaluate("window.DynamicsUI.confirm = async () => true;")
        page.locator(f'[data-tpl-delete="{created_id}"]').click()
        page.wait_for_function("window.__templatePosts.length === 3")
        assert page.evaluate("window.__templatePosts[2]") == {"action": "delete", "id": created_id}
        assert "夜间安静群 Pro" not in page.locator("#tplList").inner_text()

        page.locator("#btnCloseTplDialog").click()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        assert errors == []


@pytest.mark.parametrize("width,theme", [(1440, "day"), (375, "night")])
def test_setup_wizard_template_onboarding_applies_batch_and_shows_receipt(browser, page_server, width, theme):
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    state = {
        "ui": theme,
        "stored": {
            "enable": True,
            "takeover_all": False,
            "takeover_groups": [],
            "private_chat_enabled": False,
            "participation_backend": "llm",
            "decision_provider": "",
        },
        "effective": {
            "enable": True,
            "takeover_all": False,
            "takeover_groups": [],
            "exclude_groups": [],
            "bot_names": [],
            "participation_backend": "llm",
            "decision_provider": "",
            "decision_llm_provider": "",
            "reply_provider": "",
            "discussion_judgment_mode": "rerank",
            "discussion_reranker_provider": "",
            "discussion_llm_provider": "",
            "shadow_mode": False,
            "presence_knob": "sensible",
            "reply_probability_threshold": 70,
            "debounce_base_cooldown": 0.25,
            "debounce_extended_cooldown": 1.0,
            "debounce_max_cap": 4.0,
            "deep_cooling_minutes": 15.0,
            "private_chat_enabled": False,
            "daily_rhythm_enabled": True,
            "media_understand_reply_enabled": False,
            "rhythm_timezone": "Asia/Shanghai",
        },
        "batch_calls": [],
    }

    def handle_api(_source, method: str, endpoint: str, payload: dict):
        if method == "get":
            if endpoint == "ui_preferences":
                return {"ok": True, "data": {"ui": state["ui"], "setup_dismissed": False}}
            if endpoint == "config":
                return {"ok": True, "data": {"schema": schema, "stored": dict(state["stored"]), "effective": dict(state["effective"])}}
            if endpoint == "providers":
                return {"ok": True, "data": {"chat": [{"id": "openai/gpt-4o-mini", "model": "gpt-4o-mini"}], "decision": [], "rerank": [], "systemone_router_ready": True}}
            if endpoint == "setup/groups":
                return {
                    "ok": True,
                    "data": {
                        "groups": [
                            {"group_id": "10001", "group_name": "测试新群", "platform_id": "qq-main", "platform_type": "aiocqhttp", "umo": "qq-main:GroupMessage:10001", "member_count": 30, "sources": ["platform"]},
                        ],
                        "platforms": [{"platform_id": "qq-main", "platform_type": "aiocqhttp", "status": "ok", "group_count": 1}],
                        "discovered_count": 1,
                        "session_count": 0,
                        "partial_failure": False,
                    },
                }
            if endpoint == "series_templates":
                return {
                    "ok": True,
                    "data": {
                        "plugins": [
                            {"id": "astrbot_plugin_chat_dynamics", "label": "对话主控", "mark": "CD"},
                            {"id": "astrbot_plugin_dynamics_memory", "label": "群间记忆", "mark": "DM"},
                            {"id": "astrbot_plugin_visual_media", "label": "视觉工坊", "mark": "VM"},
                        ],
                        "templates": [
                            {
                                "id": "light_chat",
                                "name": "轻量陪聊群",
                                "description": "主控 + 记忆 + 视觉 开启",
                                "builtin": True,
                                "readonly": True,
                                "strategy_hint": "适合日常轻度陪聊",
                                "states": {
                                    "astrbot_plugin_chat_dynamics": True,
                                    "astrbot_plugin_dynamics_memory": True,
                                    "astrbot_plugin_visual_media": True,
                                },
                            },
                            {
                                "id": "custom_club",
                                "name": "社团专属模板",
                                "description": "自定义社团方案",
                                "builtin": False,
                                "readonly": False,
                                "strategy_hint": "",
                                "states": {
                                    "astrbot_plugin_chat_dynamics": True,
                                    "astrbot_plugin_dynamics_memory": True,
                                    "astrbot_plugin_visual_media": False,
                                },
                            },
                        ],
                    },
                }
            if endpoint == "series_presence":
                return {"ok": True, "data": {"plugins": [], "series_defaults": {"rhythm_timezone": "Asia/Shanghai"}}}
            if endpoint == "integrations":
                return {"ok": True, "data": {"companions": []}}
            return {"ok": True, "data": {}}
        if method == "post":
            if endpoint == "config":
                updates = (payload or {}).get("config", {})
                state["stored"].update(updates)
                state["effective"].update(updates)
                return {"ok": True, "data": {"schema": schema, "stored": dict(state["stored"]), "effective": dict(state["effective"])}}
            if endpoint == "series_matrix_batch":
                state["batch_calls"].append(payload)
                return {
                    "ok": True,
                    "data": {
                        "ok": True,
                        "umo": payload["umo"],
                        "template_id": payload["template_id"],
                        "applied": [
                            {"plugin_id": "astrbot_plugin_chat_dynamics", "enabled": True, "version": "v2", "summary": "开启"},
                            {"plugin_id": "astrbot_plugin_dynamics_memory", "enabled": True, "version": "v2", "summary": "开启"},
                        ],
                        "skipped": [
                            {"plugin_id": "astrbot_plugin_visual_media", "reason": "missing", "message": "未安装"},
                        ],
                        "failed": [],
                        "matrix_version": "m-v2",
                    },
                }
            return {"ok": True, "data": {"saved": True}}
        return {"ok": True, "data": {}}

    with browser.new_context(viewport={"width": width, "height": 900}) as context:
        context.expose_binding("__setupTplApi", handle_api)
        context.add_init_script("""
          window.AstrBotPluginPage = {
            ready: async () => {},
            t: (_key, fallback) => fallback,
            apiGet: (endpoint, params = {}) => window.__setupTplApi('get', endpoint, params),
            apiPost: (endpoint, body = {}) => window.__setupTplApi('post', endpoint, body),
          };
        """)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(f"{page_server}/setup/index.html?ui={theme}")
        page.locator('[data-group-check="10001"]').wait_for()

        # Select group 10001 and choose custom template
        page.locator('[data-group-check="10001"]').check()
        page.locator("#setupTemplateSelect").select_option("custom_club")
        page.locator("#setupTemplatePreview").wait_for(state="visible")
        assert "社团专属模板" in page.locator("#setupTemplatePreview").inner_text()
        assert "群间记忆" in page.locator("#setupTemplatePreview").inner_text()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")

        # Jump to Step 4 -> verify template noted in Review -> Save
        page.locator("#stepBtn4").click()
        page.locator("#stepPanel4").wait_for(state="visible")
        assert "社团专属模板" in page.locator("#reviewPathGrid").inner_text()

        page.locator("#btnSaveSetup").click()
        page.locator("#saveSuccessBanner").wait_for(state="visible")
        page.locator("#setupTemplateReceipt").wait_for(state="visible")

        assert state["batch_calls"] == [
            {"umo": "qq-main:GroupMessage:10001", "template_id": "custom_club"}
        ]
        receipt_text = page.locator("#setupTemplateReceipt").inner_text()
        assert "社团专属模板" in receipt_text
        assert "已应用 2 / 跳过 1 / 失败 0" in receipt_text
        assert "群间记忆" in receipt_text
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        assert errors == []


@pytest.mark.parametrize("lang", ["en", "ja", "ru"])
def test_console_telemetry_and_matrix_lazy_locale_switching(browser, page_server, lang):
    with browser.new_context(viewport={"width": 1280, "height": 900}) as context:
        setup(context, {"ui": "day"})
        context.add_init_script("""(() => {
          const api = window.AstrBotPluginPage;
          const origGet = api.apiGet;
          api.apiGet = async (endpoint, params = {}) => {
            if (endpoint === "series_telemetry") {
              return {
                ok: true,
                data: {
                  window: params.window || "24h",
                  retention_days: 30,
                  summary: { calls: 5, ok_calls: 5, error_calls: 0, error_rate: 0, input_tokens: 1000, output_tokens: 200, total_tokens: 1200, cached_tokens: 400, cache_hit_rate: 0.4 },
                  by_plugin: [{ plugin_id: "astrbot_plugin_chat_dynamics", label: "对话主控", calls: 5, ok_calls: 5, error_calls: 0, error_rate: 0, input_tokens: 1000, output_tokens: 200, cached_tokens: 400, cache_hit_rate: 0.4, p50_generation_ms: 300, p95_generation_ms: 500 }],
                  by_provider: [{ provider_id: "openai/gpt-4o-mini", calls: 5, ok_calls: 5, error_calls: 0, error_rate: 0, input_tokens: 1000, output_tokens: 200, cached_tokens: 400, cache_hit_rate: 0.4, p50_generation_ms: 300, p95_generation_ms: 500 }],
                  timeline: [{ bucket: "2026-11-22 15:00", granularity: "hour", calls: 5, error_calls: 0, input_tokens: 1000, output_tokens: 200, cached_tokens: 400, cache_hit_rate: 0.4, avg_generation_ms: 300 }],
                  live_queue: { capacity: 4, max_queue: 128, active: 0, queued: 0, by_plugin: {}, by_provider: {} }
                }
              };
            }
            return origGet(endpoint, params);
          };
        })();""")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(f"{page_server}/console/index.html?lang={lang}")
        page.locator("#tab-telemetry").click()
        page.locator("#telemetrySection").wait_for(state="visible")
        page.wait_for_function(
            "document.getElementById('telemetryTitle')?.textContent !== '系列运行与 Token 遥测'"
        )
        title_text = page.locator("#telemetryTitle").inner_text().strip()
        assert title_text and title_text != "系列运行与 Token 遥测"
        btn_tpl_text = page.locator("#btnMatrixTemplates").text_content() or ""
        assert btn_tpl_text.strip() != "模板管理"
        assert errors == []

