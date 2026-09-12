"""Small real-browser contract test for the plugin page.

The suite uses the host's Dashboard Bridge contract, so it does not need a
running AstrBot server. CI installs Chromium and executes this module; local
unit runs skip it when Playwright/browser binaries are unavailable.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
sync_playwright = playwright.sync_playwright


ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "pages" / "console"
DEFAULT_SCREENSHOT_DIR = Path(
    r"C:\Users\31283\.codex\visualizations\2026\09\06\chat-dynamics-redesign"
)


def _chromium_executable() -> str | None:
    """Prefer an already-installed browser; never download a Playwright bundle."""

    configured = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    candidates = [Path(configured)] if configured else []
    if os.name == "nt":
        candidates.extend(
            [
                Path(os.environ.get("PROGRAMFILES", ""))
                / "Google/Chrome/Application/chrome.exe",
                Path(os.environ.get("PROGRAMFILES(X86)", ""))
                / "Google/Chrome/Application/chrome.exe",
                Path(os.environ.get("LOCALAPPDATA", ""))
                / "Google/Chrome/Application/chrome.exe",
            ]
        )
    for candidate in candidates:
        if candidate and candidate.is_file():
            return str(candidate)
    return None


def _launch_browser(pw):
    launch_args = {"headless": True}
    executable = _chromium_executable()
    if executable:
        launch_args["executable_path"] = executable
    try:
        return pw.chromium.launch(**launch_args)
    except Exception as exc:
        pytest.skip(f"Chromium unavailable: {exc}")


def _screenshot_dir() -> Path | None:
    configured = os.environ.get("BROWSER_SCREENSHOT_DIR")
    if configured:
        return Path(configured)
    if os.name == "nt":
        return DEFAULT_SCREENSHOT_DIR
    return None


def _capture_screenshot(page, name: str) -> None:
    destination = _screenshot_dir()
    if destination is None:
        return
    destination.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(destination / f"{name}.png"), full_page=True)


def _assert_no_horizontal_overflow(page) -> None:
    dimensions = page.evaluate(
        """
        () => ({
          viewport: window.innerWidth,
          document: document.documentElement.scrollWidth,
          body: document.body ? document.body.scrollWidth : 0,
        })
        """
    )
    assert dimensions["document"] <= dimensions["viewport"] + 1, dimensions
    assert dimensions["body"] <= dimensions["viewport"] + 1, dimensions


def _assert_touch_target(page, selector: str, minimum: int = 44) -> None:
    control = page.locator(selector)
    control.wait_for(state="visible")
    assert control.is_enabled(), f"{selector} is disabled"
    box = control.bounding_box()
    assert box is not None, selector
    assert box["width"] >= minimum and box["height"] >= minimum, (
        selector,
        box,
    )


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        return


@pytest.fixture()
def console_server():
    def handler(*args, **kwargs):
        return _QuietHandler(*args, directory=str(CONSOLE), **kwargs)

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/index.html"
    finally:
        server.shutdown()
        thread.join(timeout=2)


@pytest.mark.parametrize("persona_mode", [False, True])
def test_console_loads_redacted_state_and_applies_preset(console_server, persona_mode):
    calls = []
    logs = []
    with sync_playwright() as pw:
        try:
            browser = _launch_browser(pw)
        except Exception as exc:  # missing local browser binary; CI installs it
            pytest.skip(f"Chromium is unavailable: {type(exc).__name__}")
        page = browser.new_page()
        page.on("console", lambda message: logs.append(message.text))
        page.add_init_script(
            """
            window.confirm = () => true;
            window.__calls = [];
            window.AstrBotPluginPage = {
              t: (_key, fallback) => fallback,
              apiGet: async (endpoint) => {
                if (endpoint === "presets") return {status:"ok", ok:true, data:{presets:{active:{changes:{shadow_mode:false}}}}};
                if (endpoint === "session") return {status:"ok", ok:true, data:{session_key:"mock:GroupMessage:g1",session_id:"g1",group_id:"g1",content_redacted:true,dag_nodes:1,mode:"chill_fade",mpm:0,pending:0,cooling:false,cooling_remaining:0,turn_count:1,rate_series:[],scene_tags:[],emotion_tags:[],last_arbitration:null}};
                return {status:"ok", ok:true, data:{enabled:true,pipeline_mode:"filter",takeover_all:true,takeover_groups:[],exclude_groups:[],live_count:1,cooling_count:0,pending_count:0,shadow_mode:true,console_show_message_content:false,sessions:[{session_key:"mock:GroupMessage:g1",session_id:"g1",group_id:"g1",takeover:true,mode:"chill_fade",mpm:0,token_density:0,emoji_ratio:0,media_ratio:0,cooling:false,cooling_remaining:0,pending:0,dag_nodes:1}]}};
              },
              apiPost: async (endpoint, body) => { window.__calls.push({endpoint, body}); return {status:"ok", ok:true, data:{saved:true}}; }
            };
            """
        )
        if persona_mode:
            page.add_init_script("""
                window.addEventListener('DOMContentLoaded', () => {
                    const original = window.AstrBotPluginPage.apiGet;
                    window.AstrBotPluginPage.apiGet = async (endpoint) => {
                        const result = await original(endpoint);
                        Object.assign(result.data, {decision_mode:'persona_model', agent_bridge:'ready',
                            interaction_state:'focused', model_queue_depth:2,
                            model_decision:{action:'reply',state:'focused',reason_code:'relevant_request',
                                target_message_ids:['m1'],latency_ms:125,shadow:true}});
                        return result;
                    };
                });
            """)
        page.goto(console_server)
        page.locator("#tab-policy").click()
        page.get_by_text("观察模式 · 不产生副作用").wait_for()
        page.get_by_text("控制台正文：已脱敏").wait_for()
        if persona_mode:
            page.get_by_text("人设模型决策", exact=False).wait_for()
            page.locator("#tab-sessions").click()
            page.locator("#traceMeta").filter(has_text="relevant_request").wait_for()
            page.locator("#tab-policy").click()
            assert "125ms" in page.locator("#traceMeta").inner_text().lower()
        screenshot_dir = os.environ.get("BROWSER_SCREENSHOT_DIR")
        if screenshot_dir:
            Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(screenshot_dir) / ("persona.png" if persona_mode else "legacy.png")), full_page=True)
        page.locator("#presetSelect option[value='active']").wait_for(state="attached")
        select = page.locator("#presetSelect")
        select.select_option("active")
        page.locator("#btnPreset").click()
        page.wait_for_function("window.__calls && window.__calls.some((item) => item.endpoint === 'preset/apply')")
        calls = page.evaluate("window.__calls")
        assert calls[-1] == {"endpoint": "preset/apply", "body": {"name": "active", "confirm": True}}
        browser.close()

    log_path = os.environ.get("BROWSER_LOG_PATH")
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(json.dumps(logs, ensure_ascii=False), encoding="utf-8")


def test_console_detail_race_does_not_overwrite_new_selection(console_server):
    with sync_playwright() as pw:
        try:
            browser = _launch_browser(pw)
        except Exception as exc:
            pytest.skip(f"Chromium is unavailable: {type(exc).__name__}")
        page = browser.new_page()
        page.add_init_script(
            """
            window.__detailResolvers = {};
            window.__detailCalls = [];
            window.AstrBotPluginPage = {
              t: (_key, fallback) => fallback,
              apiGet: async (endpoint, params = {}) => {
                if (endpoint === "presets") return {status:"ok", ok:true, data:{presets:{active:{changes:{shadow_mode:false}}}}};
                if (endpoint === "overview") return {status:"ok", ok:true, data:{enabled:true,pipeline_mode:"filter",takeover_all:true,takeover_groups:[],exclude_groups:[],live_count:2,cooling_count:0,pending_count:0,shadow_mode:true,console_show_message_content:false,sessions:[
                  {session_key:"umo:A",session_id:"a",group_id:"g1",takeover:true,mode:"chill_fade",mpm:1},
                  {session_key:"umo:B",session_id:"b",group_id:"g2",takeover:true,mode:"chill_fade",mpm:2}
                ]}};
                if (endpoint === "session") {
                  window.__detailCalls.push(params.session_key);
                  return new Promise((resolve) => { window.__detailResolvers[params.session_key] = resolve; });
                }
                throw new Error("unexpected endpoint");
              },
              apiPost: async () => ({status:"ok", ok:true, data:{saved:true}})
            };
            """
        )
        page.goto(console_server)
        channels = page.locator("#channelList .channel")
        channels.nth(0).click()
        page.wait_for_function("window.__detailResolvers['umo:A'] !== undefined")
        channels.nth(1).click()
        page.wait_for_function("window.__detailResolvers['umo:B'] !== undefined")
        page.evaluate(
            """
            window.__detailResolvers['umo:B']({status:'ok', ok:true, data:{session_key:'umo:B',session_id:'b',group_id:'g2',mode:'chill_fade',content_redacted:true}})
            """
        )
        page.locator("#roomId").filter(has_text="g2").wait_for()
        page.evaluate(
            """
            window.__detailResolvers['umo:A']({status:'ok', ok:true, data:{session_key:'umo:A',session_id:'a',group_id:'g1',mode:'fast_banter',content_redacted:true}})
            """
        )
        page.wait_for_timeout(100)
        assert "g2" in page.locator("#roomId").inner_text().lower()
        page.locator("#sessionFilter").fill("not-loaded-search")
        page.locator("#sessionFilter").press("Enter")
        page.locator("#sessionFilterNote").filter(has_text="未在已加载会话中找到").wait_for()
        assert page.evaluate("window.__detailCalls") == ["umo:A", "umo:B"]
        browser.close()


def test_console_detail_error_is_visible_and_retryable(console_server):
    with sync_playwright() as pw:
        try:
            browser = _launch_browser(pw)
        except Exception as exc:
            pytest.skip(f"Chromium is unavailable: {type(exc).__name__}")
        page = browser.new_page()
        page.add_init_script(
            """
            window.__sessionAttempts = 0;
            window.AstrBotPluginPage = {
              t: (_key, fallback) => fallback,
              apiGet: async (endpoint) => {
                if (endpoint === "presets") return {status:"ok", ok:true, data:{presets:{active:{changes:{}}}}};
                if (endpoint === "overview") return {status:"ok", ok:true, data:{enabled:true,pipeline_mode:"filter",takeover_all:true,takeover_groups:[],live_count:2,cooling_count:0,pending_count:0,shadow_mode:false,console_show_message_content:false,sessions:[
                  {session_key:"umo:other",session_id:"other",group_id:"g-other",takeover:true,mode:"chill_fade",mpm:1},
                  {session_key:"umo:retry",session_id:"retry",group_id:"g-retry",takeover:true,mode:"chill_fade",mpm:1}
                ]}};
                if (endpoint === "session") {
                  window.__sessionAttempts += 1;
                  if (window.__sessionAttempts === 1) return {status:"error", ok:false, message:"detail unavailable"};
                  return {status:"ok", ok:true, data:{session_key:"umo:retry",session_id:"retry",group_id:"g-retry",mode:"chill_fade",content_redacted:true}};
                }
                throw new Error("unexpected endpoint");
              },
              apiPost: async () => ({status:"ok", ok:true, data:{saved:true}})
            };
            """
        )
        page.goto(console_server)
        page.locator("#channelList .channel").filter(has_text="g-retry").click()
        page.locator("#detailStatus").filter(has_text="详情加载失败").wait_for()
        retry = page.locator("#btnRetryDetail")
        retry.wait_for(state="visible")
        retry.click()
        page.locator("#roomId").filter(has_text="g-retry").wait_for()
        assert page.evaluate("window.__sessionAttempts") == 2
        assert retry.is_hidden()
        browser.close()


@pytest.mark.parametrize(
    ("viewport_name", "viewport"),
    [
        ("desktop", {"width": 1440, "height": 1000}),
        ("tablet", {"width": 768, "height": 1024}),
        ("mobile", {"width": 390, "height": 844}),
    ],
)
def test_console_responsive_layout_wraps_long_umo_and_keeps_controls_usable(
    console_server, viewport_name, viewport
):
    """Exercise rendered layout contracts against long UMO/provider values."""

    with sync_playwright() as pw:
        try:
            browser = _launch_browser(pw)
        except Exception as exc:
            pytest.skip(f"Chromium is unavailable: {type(exc).__name__}")
        try:
            page = browser.new_page(viewport=viewport)
            page.add_init_script(
                """
                window.confirm = () => true;
                window.__overviewCalls = 0;
                const longSessionKey = "umo:GroupMessage:" + "session-key-".repeat(18);
                const longGroupId = "group-" + "g".repeat(96);
                const longProvider = "provider-" + "p".repeat(180);
                const sessions = [
                  {session_key:longSessionKey,session_id:"long-session",group_id:longGroupId,
                   takeover:true,mode:"chill_fade",mpm:3.5,token_density:9.2,
                   provider_resolution:{reply:longProvider,vibe:longProvider}},
                  {session_key:"umo:GroupMessage:short",session_id:"short-session",group_id:"g-short",
                   takeover:true,mode:"fast_banter",mpm:1.2}
                ];
                const overview = {
                  enabled:true,pipeline_mode:"filter",takeover_all:true,takeover_groups:[],
                  exclude_groups:[],live_count:2,cooling_count:0,pending_count:0,
                  shadow_mode:false,console_show_message_content:false,
                  provider_resolution:{reply:longProvider,vibe:longProvider},
                  sessions
                };
                window.__responsiveFixture = {longSessionKey, longProvider};
                window.AstrBotPluginPage = {
                  t: (_key, fallback) => fallback,
                  apiGet: async (endpoint, params = {}) => {
                    if (endpoint === "presets") {
                      return {status:"ok",ok:true,data:{presets:{active:{changes:{}}}}};
                    }
                    if (endpoint === "overview") {
                      window.__overviewCalls += 1;
                      return {status:"ok",ok:true,data:overview};
                    }
                    if (endpoint === "session") {
                      const row = sessions.find((item) => item.session_key === params.session_key);
                      return {status:"ok",ok:true,data:{...(row || sessions[0]),content_redacted:true,
                        nodes:[],dag_nodes:0,unicode_emoji_ratio:0,media_ratio:0,
                        unique_speakers:2,cooling:false,cooling_remaining:0}};
                    }
                    throw new Error("unexpected endpoint: " + endpoint);
                  },
                  apiPost: async (_endpoint, _body) => ({status:"ok",ok:true,data:{saved:true}})
                };
                """
            )
            page.goto(console_server)
            page.wait_for_function("window.__overviewCalls > 0")
            first_channel = page.locator("#channelList .channel").first
            first_channel.wait_for(state="visible")
            first_channel.click()
            page.locator("#roomView").wait_for(state="visible")
            _capture_screenshot(page, viewport_name)
            _assert_no_horizontal_overflow(page)
            _assert_touch_target(page, "#btnRefresh")
            _assert_touch_target(page, "#btnCool")
            _assert_touch_target(page, "#btnReset")
            assert page.locator("#roomId").inner_text().count("session-key-") >= 1
            first_channel_box = first_channel.bounding_box()
            assert first_channel_box is not None
            assert first_channel_box["height"] <= 180
            session_key = first_channel.locator(".session-key")
            assert session_key.evaluate("element => getComputedStyle(element).webkitLineClamp") == "2"
            assert session_key.bounding_box()["height"] <= 28
            assert first_channel.get_attribute("title") == page.evaluate("window.__responsiveFixture.longSessionKey")
            assert page.evaluate("window.__responsiveFixture.longSessionKey") in (first_channel.get_attribute("aria-label") or "")
            stage_states = page.locator("#pipeline .stage-state")
            assert stage_states.count() == 5
            assert all(stage_states.nth(index).is_visible() for index in range(stage_states.count()))
            assert all(stage_states.nth(index).inner_text() in {"活跃", "待命"} for index in range(stage_states.count()))
            assert all("：" in (stage_states.nth(index).get_attribute("aria-label") or "") for index in range(stage_states.count()))
            page.locator("#tab-policy").click()
            page.locator("#providerState").wait_for(state="visible")
            assert page.locator("#providerState").inner_text().count("provider-") >= 1
        finally:
            browser.close()


def test_console_offline_state_does_not_show_fake_enabled_or_counts(console_server):
    """A failed overview must clear stale-looking status and management state."""

    with sync_playwright() as pw:
        try:
            browser = _launch_browser(pw)
        except Exception as exc:
            pytest.skip(f"Chromium is unavailable: {type(exc).__name__}")
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.add_init_script(
                """
                window.AstrBotPluginPage = {
                  t: (_key, fallback) => fallback,
                  apiGet: async () => { throw new Error("offline"); },
                  apiPost: async () => { throw new Error("offline"); }
                };
                """
            )
            page.goto(console_server)
            page.wait_for_function(
                "() => document.querySelector('#linkLamp')?.classList.contains('warn')"
            )
            assert page.locator("#statEnabled").inner_text() == "—"
            assert page.locator("#statLive").inner_text() == "—"
            assert page.locator("#statCooling").inner_text() == "—"
            assert page.locator("#statPending").inner_text() == "—"
            assert "未知" in page.locator("#providerState").inner_text()
            assert page.locator("#btnCool").is_disabled()
            assert page.locator("#btnReset").is_disabled()
            assert page.locator("#emptyBoard").is_visible()
        finally:
            browser.close()


def test_console_side_nav_assigns_signed_content_path(console_server):
    """Side nav must use page_nav's asset_token; tokenless sibling URLs are 401."""

    with sync_playwright() as pw:
        try:
            browser = _launch_browser(pw)
        except Exception as exc:
            pytest.skip(f"Chromium is unavailable: {type(exc).__name__}")
        try:
            page = browser.new_page()
            page.add_init_script(
                """
                window.AstrBotPluginPage = {
                  ready: async () => ({}),
                  t: (_key, fallback) => fallback,
                  apiGet: async (endpoint, params = {}) => {
                    if (endpoint === "page_nav") {
                      return {
                        content_path:
                          "/api/plugin/page/content/astrbot_plugin_chat_dynamics/" +
                          params.page +
                          "/?asset_token=fresh-token"
                      };
                    }
                    if (endpoint === "presets") {
                      return {status:"ok", ok:true, data:{presets:{}}};
                    }
                    return {status:"ok", ok:true, data:{
                      enabled:true, pipeline_mode:"filter", takeover_all:true,
                      takeover_groups:[], exclude_groups:[], live_count:0,
                      cooling_count:0, pending_count:0, shadow_mode:true,
                      console_show_message_content:false, sessions:[]
                    }};
                  },
                  apiPost: async () => ({status:"ok", ok:true, data:{}})
                };
                """
            )
            page.goto(console_server)
            page.locator('[data-nav-page="today"]').wait_for()
            page.locator('[data-nav-page="today"]').click()
            page.wait_for_url("**/today/**")
            assert "asset_token=fresh-token" in page.url
            assert "/astrbot_plugin_chat_dynamics/today/" in page.url
        finally:
            browser.close()


@pytest.mark.parametrize("detail,lamp", [("native_hooks", "原生钩子"), ("call_error:TimeoutError", "降级中")])
def test_companion_status_shows_native_mode_and_call_failures(console_server, detail, lamp):
    with sync_playwright() as pw:
        try:
            browser = _launch_browser(pw)
        except Exception as exc:
            pytest.skip(f"Chromium is unavailable: {type(exc).__name__}")
        page = browser.new_page()
        partner = {"status": "connected" if detail == "native_hooks" else "degraded", "detail": detail,
                   "lamp": lamp, "providers": [{"name": "LivingMemory", "mode": "native_hooks", "errors": {}}]}
        page.add_init_script("window.__partner = " + json.dumps(partner, ensure_ascii=False) + ";")
        page.add_init_script("""
            window.AstrBotPluginPage = {
                t: (_key, fallback) => fallback,
                apiGet: async () => ({ok:true, data:{enabled:true, sessions:[], selflearning:window.__partner}}),
                apiPost: async () => ({ok:true, data:{}})
            };
        """)
        page.goto(console_server)
        page.locator("#tab-policy").click()
        page.locator("#statPartner").get_by_text(lamp, exact=True).wait_for()
        assert "LivingMemory" in page.locator("#statPartnerHint").inner_text()
        if detail == "native_hooks":
            assert "原生钩子" in page.locator("#statPartnerHint").inner_text()
        else:
            assert "TimeoutError" in page.locator("#statPartnerHint").inner_text()
        browser.close()


def test_persona_fallback_is_visible_and_updates_on_diagnostic_change(console_server):
    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        page = browser.new_page()
        page.add_init_script("""
            window.__overview = {enabled:true, decision_mode:'legacy', shadow_mode:false,
                persona_fallback:'CD_AGENT_BRIDGE_UNAVAILABLE:missing_host_managers', sessions:[]};
            window.AstrBotPluginPage = {t: (_key, fallback) => fallback,
                apiGet: async endpoint => ({ok:true,data:endpoint === 'presets' ? {presets:{}} : window.__overview})};
        """)
        page.goto(console_server)
        page.locator('#tab-policy').click()
        page.locator('#shadowState').filter(has_text='人设不可用，已切换规则模式').wait_for()
        assert 'missing_host_managers' in page.locator('#shadowState').get_attribute('title')
        page.evaluate("window.__overview.persona_fallback = ''")
        page.locator('#btnRefresh').click()
        page.wait_for_function("!document.querySelector('#shadowState').textContent.includes('人设不可用')")
        assert page.locator('#shadowState').get_attribute('title') == ''
        browser.close()
