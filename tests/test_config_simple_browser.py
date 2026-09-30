"""Simple configuration is a view over the complete, preserved configuration."""
import pytest

from .test_ui_theme_browser import ROOT, browser as browser, page_server as page_server, setup


BASIC_KEYS = {
    "enable", "shadow_mode", "takeover_all", "takeover_groups", "exclude_groups",
    "decision_mode", "decision_backend", "decision_learning_mode",
    "decision_learning_student_backend", "kev_base_url", "kev_checkpoint_id",
    "kev_timeout", "reply_provider", "reply_timeout", "bot_names",
}


def visible_config_keys(page):
    return set(page.locator('.config-field:visible [data-config-key]').evaluate_all(
        "nodes => nodes.map(node => node.dataset.configKey)"))


def setup_config(context, theme="day"):
    setup(context, {"ui": theme})
    context.add_init_script("""(() => {
      const api = window.AstrBotPluginPage;
      const get = api.apiGet;
      const post = api.apiPost;
      window.__stored = Object.fromEntries(Object.entries(window.__schema).map(([k,v]) => [k,v.default]));
      window.__stored.decision_timeout = 11;
      window.__stored.reply_provider = 'existing-model';
      window.__stored.jev_base_url = 'https://legacy-jev.example';
      window.__stored.jev_model = 'jev-pinned';
      window.__stored.laya_base_url = 'http://127.0.0.1:18080';
      window.__stored.laya_timeout = 3.5;
      window.__legacyStored = Object.fromEntries(['jev_base_url', 'jev_model', 'laya_base_url', 'laya_timeout']
        .map(key => [key, window.__stored[key]]));
      api.apiGet = async (endpoint, params) => {
        if (endpoint === 'config') return {ok:true,data:{schema:window.__schema,stored:window.__stored,effective:window.__stored}};
        if (endpoint === 'providers' && window.__failProviders) throw new Error('offline');
        return get(endpoint, params);
      };
      api.apiPost = async (endpoint, body) => {
        if (endpoint !== 'config') return post(endpoint, body);
        if (window.__failSave) throw new Error('save failed');
        window.__savedConfig = body.config;
        window.__stored = {...window.__stored, ...body.config};
        return {ok:true,data:{schema:window.__schema,stored:window.__stored,effective:window.__stored}};
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
        assert visible_config_keys(page) == BASIC_KEYS
        assert page.locator('#btnConfigApply').is_hidden()
        assert "尚未选择有效群聊" in page.locator('#configScopeStatus').inner_text()
        assert page.locator('[data-config-key="enable"]').is_checked()
        assert page.locator('[data-config-key="decision_mode"] option:checked').inner_text() == "规则判断（兼容）"
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
        assert visible_config_keys(page) == BASIC_KEYS
        page.evaluate("window.__failSave = true")
        page.locator('#btnConfigSave').click()
        page.wait_for_function("document.querySelector('#configNote').textContent === 'save failed'")
        assert not page.locator('#btnConfigSave').is_disabled()
        assert field.input_value() == "12"
        page.evaluate("window.__failSave = false")
        page.locator('#btnConfigSave').click()
        page.wait_for_function("window.__savedConfig?.decision_timeout === 12")
        assert page.evaluate("window.__stored.reply_provider") == "existing-model"
        assert page.evaluate("!('reply_provider' in window.__savedConfig)")
        assert page.evaluate("Object.entries(window.__legacyStored).every(([key, value]) => "
                             "window.__stored[key] === value && !(key in window.__savedConfig))")
        for key in ("jev_base_url", "jev_model", "laya_base_url", "laya_timeout"):
            assert page.locator(f'[data-config-key="{key}"]').count() == 0
        assert page.evaluate("window.__savedConfig.takeover_groups") == ["123", "456", "123"]
        assert "当前：对 1 个指定群生效" in page.locator('#configScopeStatus').inner_text()
        assert set(page.evaluate("Object.keys(window.__savedConfig)")) == {"takeover_groups", "exclude_groups", "decision_timeout"}
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert not errors


def test_config_loads_without_provider_list_and_storage(browser, page_server):
    with browser.new_context() as context:
        setup_config(context)
        context.add_init_script("window.__failProviders = true; Object.defineProperty(window, 'localStorage', {get() {throw new DOMException('blocked', 'SecurityError')}})")
        page = context.new_page()
        page.goto(f"{page_server}/config/index.html")
        page.wait_for_load_state("networkidle")
        assert visible_config_keys(page) == BASIC_KEYS
        assert "模型列表暂时不可用" in page.locator('#configNote').inner_text()
        page.locator('#btnAdvancedConfig').click()
        page.locator('#configSearch').fill('shadow_mode')
        page.locator('[data-config-key="shadow_mode"]').check()
        page.locator('#btnBasicConfig').click()
        assert "插件总观察已开启，不会发送插件回复" in page.locator('#configScopeStatus').inner_text()
        page.locator('#btnConfigSave').click()
        page.wait_for_function("window.__savedConfig?.shadow_mode === true")
        assert page.evaluate("window.__stored.reply_provider") == "existing-model"
        assert page.evaluate("!('reply_provider' in window.__savedConfig)")


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
        assert visible_config_keys(page) == BASIC_KEYS


@pytest.mark.parametrize("width,theme", [(1366, "day"), (390, "night")])
def test_kev_route_preview_missing_service_and_checkpoint(browser, page_server, width, theme):
    with browser.new_context(viewport={"width": width, "height": 940}) as context:
        setup_config(context, theme)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{page_server}/config/index.html?ui={theme}")
        page.wait_for_load_state("networkidle")
        assert page.locator('#configDecisionHeadline').inner_text() == "本地规则判断"
        assert "未启用 Kev 独占" in page.locator('#configConflictList').inner_text()
        assert page.locator('[data-config-key="decision_backend"] option[value="model"]').count() == 1

        page.locator('[data-config-key="decision_mode"]').select_option("persona_model")
        assert "聊天模型（旧配置；请切换）" in page.locator('#configDecisionHeadline').inner_text()
        assert "当前仍在兼容决策路径" in page.locator('#configDecisionStatus').inner_text()
        assert page.locator('[data-focus-key="decision_backend"]').is_visible()
        page.locator('[data-config-key="decision_backend"]').select_option("kev")
        assert page.locator('[data-focus-key="decision_learning_mode"]').is_visible()
        page.locator('[data-config-key="decision_learning_mode"]').select_option("active")
        assert visible_config_keys(page) == BASIC_KEYS | {"decision_learning_sessions"}
        assert page.locator('#configDecisionHeadline').inner_text() == "Kev 独占决策"
        assert page.locator('#configModelHeadline').inner_text() == "失败即静默"
        assert "检查点不符时，本轮保持沉默" in page.locator('#configModelStatus').inner_text()
        assert "检查点未锁定" in page.locator('#configConflictList').inner_text()
        assert "不留样本" in page.locator('#configConflictList').inner_text()

        page.locator('[data-config-key="decision_learning_student_backend"]').select_option("agentjev")
        assert page.locator('[data-focus-key="decision_learning_student_backend"]').is_visible()
        assert page.locator('#configDecisionHeadline').inner_text() != "Kev 独占决策"
        page.locator('[data-config-key="decision_learning_student_backend"]').select_option("kev")
        page.locator('[data-config-key="kev_base_url"]').fill("")
        assert "服务地址缺失" in page.locator('#configConflictList').inner_text()
        page.locator('[data-config-key="kev_base_url"]').fill("http://kev-server:18766")
        assert "服务地址缺失" not in page.locator('#configConflictList').inner_text()
        page.locator('[data-config-key="kev_checkpoint_id"]').fill("kev-release-test")
        page.locator('[data-config-key="decision_learning_sessions"]').fill("test-session")
        assert page.locator('#configConflicts').is_hidden()
        assert page.locator('#configStartTitle').inner_text() == "保存后路径预览"
        assert page.locator('#configOverviewState').inner_text() == "尚未保存"
        assert page.evaluate("window.__stored.decision_backend") == "model"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        (ROOT / "output").mkdir(exist_ok=True)
        page.screenshot(path=str(ROOT / "output" / f"config-kev-route-{width}-{theme}.png"), full_page=True)

        page.locator('#btnConfigSave').click()
        page.wait_for_function("window.__stored.kev_checkpoint_id === 'kev-release-test'")
        assert page.locator('#configOverviewState').inner_text() == "已应用"
        assert page.locator('#configStartTitle').inner_text() == "当前运行路径"
        assert page.locator('[data-config-key="decision_backend"] option[value="model"]').count() == 0
        assert set(page.evaluate("Object.keys(window.__savedConfig)")) == {
            "decision_mode", "decision_backend", "decision_learning_mode",
            "kev_base_url", "kev_checkpoint_id", "decision_learning_sessions",
        }
        assert page.evaluate("Object.entries(window.__legacyStored).every(([key, value]) => "
                             "window.__stored[key] === value && !(key in window.__savedConfig))")
        assert not errors
