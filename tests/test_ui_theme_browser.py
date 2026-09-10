"""Browser behavior for six-page themes, sandbox storage and account restoration."""

import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from .test_console_redesign_verification import _launch_browser, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PAGES = ("console", "config", "today", "manners", "memory", "replay")


@pytest.fixture(scope="module")
def page_server():
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT / "pages"), **kwargs)

        def log_message(self, *_args):
            pass

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            super().end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=2)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        yield browser
        browser.close()


def setup(context, state):
    def preferences(_source, method, body):
        if method == "post":
            if state.get("fail"):
                return {"ok": False}
            state["ui"] = body["ui"]
            state.setdefault("writes", []).append(body["ui"])
            return {"ok": True, "data": {"ui": body["ui"], "saved": True}}
        return {"ok": True, "data": {"ui": state.get("ui")}}

    context.expose_binding("__themeApi", preferences)
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    context.add_init_script("window.__schema = " + json.dumps(schema, ensure_ascii=False))
    context.add_init_script("""
      window.AstrBotPluginPage = {
        ready: async () => {}, t: (_key, fallback) => fallback,
        apiGet: async (endpoint, params = {}) => {
          if (endpoint === 'ui_preferences') return window.__themeApi('get', {});
          if (endpoint === 'page_nav') return {ok:true,data:{content_path:'/' + params.page + '/index.html?asset_token=fresh'}};
          if (endpoint === 'config') return {ok:true,data:{schema:window.__schema,values:{},effective:{}}};
          if (endpoint === 'sessions') return {ok:true,data:{sessions:[]}};
          if (endpoint === 'presets') return {ok:true,data:{presets:{}}};
          return {ok:true,data:{enabled:true,pipeline_mode:'filter',takeover_all:true,sessions:[],
            read_air:{},empty:true,occasion:{kind:'neutral'},one_liner:'暂无活跃会话，开始聊天后将在这里呈现。',
            blocks:[],items:[],providers:[],presence_knob:'sensible',config:{},effective:{}}};
        },
        apiPost: async (endpoint, body) => endpoint === 'ui_preferences' ? window.__themeApi('post', body) : {ok:true,data:{}}
      };
    """)


@pytest.mark.parametrize("name", PAGES)
@pytest.mark.parametrize("source", ["query", "storage", "day_override"])
def test_theme_canvas_without_external_resources(browser, page_server, name, source):
    """The navigation canvas must have its theme even before JS/CSS arrive."""
    with browser.new_context() as context:
        if source == "query":
            context.add_init_script("Object.defineProperty(window, 'localStorage', {get() {throw new DOMException('sandbox', 'SecurityError')}})")
        else:
            context.add_init_script("localStorage.setItem('chat_dynamics_ui', 'night')")
        page = context.new_page()
        page.route("**/*.js", lambda route: route.abort())
        page.route("**/*.css", lambda route: route.abort())
        query = "?ui=night" if source == "query" else "?ui=day" if source == "day_override" else ""
        page.goto(f"{page_server}/{name}/index.html{query}")
        night = source != "day_override"
        assert page.locator("html").get_attribute("data-theme") == ("night" if night else "day")
        assert page.locator("html").evaluate("node => getComputedStyle(node).backgroundColor") == (
            "rgb(11, 20, 25)" if night else "rgb(245, 246, 244)"
        )
        assert page.locator("html").evaluate("node => getComputedStyle(node).colorScheme") == (
            "dark" if night else "light"
        )


@pytest.mark.parametrize("name", PAGES)
@pytest.mark.parametrize("theme", ["day", "night"])
def test_all_pages_share_theme_and_fit_mobile(browser, page_server, name, theme):
    with browser.new_context(viewport={"width": 1366, "height": 940}) as context:
        setup(context, {"ui": theme})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{page_server}/{name}/index.html")
        page.wait_for_function("document.querySelector('#uiThemeStatus')?.textContent === '已恢复账号偏好'")
        expected = "rgb(234, 243, 240)" if theme == "night" else "rgb(23, 42, 43)"
        assert page.locator("body").evaluate("node => getComputedStyle(node).color") == expected
        for width in (1366, 390):
            page.set_viewport_size({"width": width, "height": 940})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name
            assert page.locator("#btnUiTheme").bounding_box()["height"] >= 44
            screenshot_dir = os.environ.get("THEME_SCREENSHOT_DIR")
            if screenshot_dir:
                Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(Path(screenshot_dir) / f"{name}-{theme}-{width}.png"))
        assert errors == []


def test_theme_survives_reload_navigation_and_new_context_without_storage(browser, page_server):
    account = {}
    with browser.new_context() as context:
        setup(context, account)
        context.add_init_script("Object.defineProperty(window, 'localStorage', {get() {throw new DOMException('sandbox', 'SecurityError')}})")
        page = context.new_page()
        page.goto(f"{page_server}/console/index.html?ui=day")
        page.locator("#btnUiTheme").click()
        page.wait_for_function("document.querySelector('#uiThemeStatus').textContent === '已保存到账号'")
        assert account["ui"] == "night"
        page.reload()
        page.wait_for_function("document.documentElement.dataset.theme === 'night'")
        page.locator('[data-nav-page="today"]').click()
        page.wait_for_url("**/today/index.html?**")
        assert "asset_token=fresh" in page.url and "ui=night" in page.url
        page.wait_for_function("document.querySelector('#uiThemeStatus')?.textContent === '已恢复账号偏好'")
        assert page.locator("#btnUiTheme").get_attribute("aria-pressed") == "true"
    with browser.new_context() as reopened:
        setup(reopened, account)
        page = reopened.new_page()
        page.goto(f"{page_server}/memory/index.html?ui=day")
        page.wait_for_function("document.querySelector('#uiThemeStatus')?.textContent === '已恢复账号偏好'")
        assert page.locator("html").get_attribute("data-theme") == "night"


def test_late_restore_cannot_undo_click(browser, page_server):
    with browser.new_context() as context:
        setup(context, {})
        context.add_init_script("""
          window.addEventListener('DOMContentLoaded', () => {
            const original = window.AstrBotPluginPage.apiGet;
            window.AstrBotPluginPage.apiGet = (endpoint, params) => endpoint === 'ui_preferences'
              ? new Promise(resolve => window.releaseTheme = () => resolve({ok:true,data:{ui:'day'}}))
              : original(endpoint, params);
          });
        """)
        page = context.new_page()
        page.goto(f"{page_server}/console/index.html")
        page.wait_for_function("typeof window.releaseTheme === 'function'")
        page.locator("#btnUiTheme").click()
        page.evaluate("window.releaseTheme()")
        page.wait_for_function("document.querySelector('#uiThemeStatus').textContent === '已保存到账号'")
        assert page.locator("html").get_attribute("data-theme") == "night"


def test_rapid_theme_changes_are_saved_in_order(browser, page_server):
    state = {}
    with browser.new_context() as context:
        setup(context, state)
        page = context.new_page()
        page.goto(f"{page_server}/console/index.html")
        page.locator("#btnUiTheme").wait_for()
        page.evaluate("""() => {
          const original = window.AstrBotPluginPage.apiPost;
          let first = true;
          window.AstrBotPluginPage.apiPost = (endpoint, body) => {
            if (first) {
              first = false;
              return new Promise(resolve => { window.releaseWrite = () => resolve(original(endpoint, body)); });
            }
            return original(endpoint, body);
          };
          window.ChatDynamicsTheme.apply('night');
          window.ChatDynamicsTheme.apply('day');
          window.ChatDynamicsTheme.apply('night');
        }""")
        page.wait_for_function("typeof window.releaseWrite === 'function'")
        assert not state.get("writes")
        page.evaluate("window.releaseWrite()")
        page.evaluate("window.ChatDynamicsTheme.flush()")
        assert state["writes"] == ["night", "day", "night"]
        assert state["ui"] == "night"


def test_real_opaque_iframe_restores_account_theme(browser, page_server):
    state = {"ui": "night"}
    with browser.new_context() as context:
        setup(context, state)
        page = context.new_page()
        page.goto(page_server)
        page.set_content(f'<iframe sandbox="allow-scripts" src="{page_server}/today/index.html"></iframe>')
        frame = page.frame_locator("iframe")
        frame.locator("#uiThemeStatus").filter(has_text="已恢复账号偏好").wait_for()
        assert frame.locator("html").get_attribute("data-theme") == "night"
        assert frame.locator("html").evaluate("() => {try {localStorage.getItem('x'); return false;} catch {return true;}}")
        frame.locator("#btnUiTheme").click()
        frame.locator("#uiThemeStatus").filter(has_text="已保存到账号").wait_for()
        assert state["ui"] == "day"


def test_save_failure_is_visible_and_retry_saves_current_theme(browser, page_server):
    state = {"fail": True}
    with browser.new_context() as context:
        setup(context, state)
        page = context.new_page()
        page.goto(f"{page_server}/console/index.html")
        page.locator("#btnUiTheme").click()
        page.locator("#btnUiRetry").wait_for(state="visible")
        state["fail"] = False
        page.locator("#btnUiRetry").click()
        page.wait_for_function("document.querySelector('#uiThemeStatus').textContent === '已保存到账号'")
        assert state["ui"] == "night"
