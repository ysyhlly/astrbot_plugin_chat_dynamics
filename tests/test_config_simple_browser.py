"""Simple configuration is a view over the complete, preserved configuration."""
import pytest

from .test_ui_theme_browser import ROOT, browser as browser, page_server as page_server, setup


def setup_config(context, theme="day"):
    setup(context, {"ui": theme})
    context.add_init_script("""(() => {
      const api = window.AstrBotPluginPage;
      const get = api.apiGet;
      const post = api.apiPost;
      window.__stored = Object.fromEntries(Object.entries(window.__schema).map(([k,v]) => [k,v.default]));
      window.__stored.decision_timeout = 11;
      window.__stored.reply_provider = 'existing-model';
      api.apiGet = async (endpoint, params) => {
        if (endpoint === 'config') return {ok:true,data:{schema:window.__schema,stored:window.__stored,effective:window.__stored}};
        if (endpoint === 'providers' && window.__failProviders) throw new Error('offline');
        return get(endpoint, params);
      };
      api.apiPost = async (endpoint, body) => {
        if (endpoint !== 'config') return post(endpoint, body);
        if (window.__failSave) throw new Error('save failed');
        window.__savedConfig = body.config;
        window.__stored = body.config;
        return {ok:true,data:{schema:window.__schema,stored:body.config,effective:body.config}};
      };
    })();""")


@pytest.mark.parametrize("width,theme", [(1366, "day"), (390, "night")])
def test_simple_config_preserves_advanced_edits_and_saves(browser, page_server, width, theme):
    with browser.new_context(viewport={"width": width, "height": 940}) as context:
        setup_config(context, theme)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{page_server}/config/index.html?ui={theme}")
        page.wait_for_load_state("networkidle")
        assert page.locator('html').get_attribute('data-ui-theme') == theme
        assert page.locator('.config-field:visible').count() == 7
        assert page.locator('#btnConfigApply').is_hidden()
        assert "尚未选择有效群聊" in page.locator('#configScopeStatus').inner_text()
        assert page.locator('[data-config-key="enable"]').is_checked()
        assert page.locator('[data-config-key="decision_mode"] option:checked').inner_text() == "规则模式（默认）"
        (ROOT / "output").mkdir(exist_ok=True)
        page.screenshot(path=str(ROOT / "output" / f"config-simple-{width}-{theme}.png"), full_page=True)
        page.locator('[data-config-key="takeover_groups"]').fill("123，456\n123")
        page.locator('[data-config-key="exclude_groups"]').fill("456")
        assert "保存后：对 1 个指定群生效" in page.locator('#configScopeStatus').inner_text()
        page.locator('#configSearch').fill('decision_timeout')
        field = page.locator('[data-config-key="decision_timeout"]')
        assert field.input_value() == "11"
        field.fill("12")
        page.locator('#btnAdvancedConfig').click()
        page.locator('#btnBasicConfig').click()
        assert field.input_value() == "12"
        assert page.locator('.config-field:visible').count() == 7
        page.evaluate("window.__failSave = true")
        page.locator('#btnConfigSave').click()
        page.wait_for_function("document.querySelector('#configNote').textContent === 'save failed'")
        assert not page.locator('#btnConfigSave').is_disabled()
        assert field.input_value() == "12"
        page.evaluate("window.__failSave = false")
        page.locator('#btnConfigSave').click()
        page.wait_for_function("window.__savedConfig?.decision_timeout === 12")
        assert page.evaluate("window.__savedConfig.reply_provider") == "existing-model"
        assert page.evaluate("window.__savedConfig.takeover_groups") == ["123", "456", "123"]
        assert "当前：对 1 个指定群生效" in page.locator('#configScopeStatus').inner_text()
        assert page.evaluate("Object.keys(window.__savedConfig).length === Object.keys(window.__schema).length")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert not errors


def test_config_loads_without_provider_list_and_storage(browser, page_server):
    with browser.new_context() as context:
        setup_config(context)
        context.add_init_script("window.__failProviders = true; Object.defineProperty(window, 'localStorage', {get() {throw new DOMException('blocked', 'SecurityError')}})")
        page = context.new_page()
        page.goto(f"{page_server}/config/index.html")
        page.wait_for_load_state("networkidle")
        assert page.locator('.config-field:visible').count() == 7
        assert "模型列表暂时不可用" in page.locator('#configNote').inner_text()
        page.locator('#btnAdvancedConfig').click()
        page.locator('#configSearch').fill('shadow_mode')
        page.locator('[data-config-key="shadow_mode"]').check()
        page.locator('#btnBasicConfig').click()
        assert "观察模式" in page.locator('#configScopeStatus').inner_text()
        page.locator('#btnConfigSave').click()
        page.wait_for_function("window.__savedConfig?.shadow_mode === true")
        assert page.evaluate("window.__savedConfig.reply_provider") == "existing-model"


def test_config_view_choice_survives_reload(browser, page_server):
    with browser.new_context() as context:
        setup_config(context)
        page = context.new_page()
        page.goto(f"{page_server}/config/index.html")
        page.wait_for_load_state("networkidle")
        page.locator('#btnAdvancedConfig').click()
        page.reload()
        page.wait_for_load_state("networkidle")
        assert page.locator('#btnAdvancedConfig').get_attribute('aria-pressed') == 'true'
        page.locator('#btnBasicConfig').click()
        page.reload()
        page.wait_for_load_state("networkidle")
        assert page.locator('.config-field:visible').count() == 7
