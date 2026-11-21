"""Playwright browser tests for v2.0 Group Control Matrix tab and legacy learning/memory sunset banners."""

from __future__ import annotations

import pytest

from .test_ui_theme_browser import browser as browser, page_server as page_server, setup


@pytest.mark.parametrize("width,theme", [(1440, "day"), (390, "night")])
def test_console_group_matrix_tab_renders_and_toggles_cleanly(browser, page_server, width, theme):
    with browser.new_context(viewport={"width": width, "height": 900}) as context:
        setup(context, {"ui": theme})
        context.add_init_script("""(() => {
          const api = window.AstrBotPluginPage;
          const origGet = api.apiGet;
          const origPost = api.apiPost;
          window.__toggleCalls = [];
          let cdEnabled = true;
          let cdVersion = "v1";
          api.apiGet = async (endpoint, params = {}) => {
            if (endpoint === "series_matrix") {
              return {
                ok: true,
                data: {
                  version: "m-" + cdVersion,
                  plugins: [
                    { id: "astrbot_plugin_chat_dynamics", label: "对话主控", mark: "CD", page: "app", state: "ready", can_open: true, supports_toggle: true },
                    { id: "astrbot_plugin_dynamics_world", label: "群间世界", mark: "DW", page: "world", state: "ready", can_open: true, supports_toggle: false },
                    { id: "astrbot_plugin_dynamics_learning", label: "群间学习", mark: "DL", page: "review", state: "ready", can_open: true, supports_toggle: false },
                    { id: "astrbot_plugin_dynamics_memory", label: "群间记忆", mark: "DM", page: "dashboard", state: "missing", can_open: false, supports_toggle: false }
                  ],
                  groups: [
                    {
                      umo: "qq:GroupMessage:10001",
                      group_id: "10001",
                      label: "星海茶话会",
                      persona_id: "夏目安安",
                      version: "row-" + cdVersion,
                      cells: {
                        astrbot_plugin_chat_dynamics: { enabled: cdEnabled, supported: true, state: "ready", summary: cdEnabled ? "开启" : "暂停", version: cdVersion, can_open: true, page: "app" },
                        astrbot_plugin_dynamics_world: { enabled: true, supported: false, state: "ready", summary: "运行中", version: "", can_open: true, page: "world" },
                        astrbot_plugin_dynamics_learning: { enabled: true, supported: false, state: "ready", summary: "3 项待审", version: "", can_open: true, page: "review" },
                        astrbot_plugin_dynamics_memory: { enabled: null, supported: false, state: "missing", summary: "未安装", version: "", can_open: false, page: "dashboard" }
                      }
                    }
                  ]
                }
              };
            }
            return origGet(endpoint, params);
          };
          api.apiPost = async (endpoint, body = {}) => {
            if (endpoint === "series_matrix_toggle") {
              window.__toggleCalls.push(body);
              cdEnabled = Boolean(body.enabled);
              cdVersion = "v2";
              return {
                ok: true,
                data: {
                  ok: true,
                  umo: body.umo,
                  plugin_id: body.plugin_id,
                  enabled: cdEnabled,
                  version: cdVersion,
                  summary: cdEnabled ? "开启" : "暂停",
                  matrix_version: "m-" + cdVersion
                }
              };
            }
            return origPost(endpoint, body);
          };
        })();""")

        page = context.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(f"{page_server}/console/index.html?ui={theme}")
        assert page.locator("#clock").count() == 0
        page.locator("#tab-matrix").click()
        page.locator("#matrixTableBody tr").first.wait_for(state="visible")
        assert "星海茶话会" in page.locator("#matrixTableBody").inner_text()

        toggle = page.locator('[data-matrix-toggle="1"]').first
        assert "开启" in toggle.inner_text()
        toggle.click()
        page.wait_for_function("window.__toggleCalls.length === 1")
        assert page.evaluate("window.__toggleCalls[0]") == {
            "umo": "qq:GroupMessage:10001",
            "plugin_id": "astrbot_plugin_chat_dynamics",
            "enabled": False,
            "expected_version": "v1",
        }
        assert "暂停" in page.locator('[data-matrix-toggle="1"]').first.inner_text()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        assert errors == []


@pytest.mark.parametrize(
    ("subpage", "banner_id", "button_id"),
    [
        ("learning", "#legacyLearningNotice", "#btnOpenDynamicsLearning"),
        ("memory", "#legacyMemoryNotice", "#btnOpenDynamicsMemory"),
    ],
)
def test_legacy_learning_and_memory_show_sunset_redirect_banners(browser, page_server, subpage, banner_id, button_id):
    with browser.new_context(viewport={"width": 1280, "height": 900}) as context:
        setup(context, {"ui": "day"})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(f"{page_server}/{subpage}/index.html")
        page.locator(banner_id).wait_for(state="visible")
        page.locator(button_id).wait_for(state="visible")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        assert errors == []
