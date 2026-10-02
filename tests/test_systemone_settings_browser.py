"""Selecting native Jev models preserves draft edits and independent reply models."""
import pytest

from .test_config_simple_browser import setup_config
from .test_ui_theme_browser import ROOT, browser as browser, page_server as page_server


def native_models(context, theme="day"):
    setup_config(context, theme)
    context.add_init_script("""(() => {
      const api = window.AstrBotPluginPage, get = api.apiGet;
      window.__stored.decision_backend = 'kev';
      window.__stored.decision_learning_mode = 'active';
      api.apiGet = async (endpoint, params) => {
        if (endpoint !== 'providers') return get(endpoint, params);
        if (window.__failModels) throw new Error('offline');
        const models = [{id:'commandcode/jev',model:'typesafe/jev',label:'CommandCode · typesafe/jev',systemone:true},
          {id:'zen/jev',model:'jev-1.13',label:'Zen · jev-1.13',systemone:true}];
        return {ok:true,data:{chat:[...models,{id:'existing-model',label:'回复模型'}],systemone:models,
          embedding:[],systemone_router_ready:!window.__routerMissing}};
      };
    })();""")


@pytest.mark.parametrize("width,theme", [(1366, "day"), (390, "night")])
def test_native_jev_selection_refresh_and_save(browser, page_server, width, theme):
    with browser.new_context(viewport={"width": width, "height": 940}) as context:
        native_models(context, theme)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{page_server}/config/index.html?ui={theme}")
        page.wait_for_load_state("networkidle")
        page.locator('[data-config-key="takeover_groups"]').fill("123")
        page.locator('#btnUseJev').click()
        decision = page.locator('[data-config-key="decision_provider"]')
        assert decision.is_visible()
        assert page.locator('[data-config-key="kev_base_url"]').count() == 0
        assert page.locator('[data-config-key="jev_timeout"]').is_visible()
        assert decision.locator('option[value="existing-model"]').count() == 0
        assert page.locator('[data-config-key="reply_provider"] option[value="commandcode/jev"]').count() == 0
        decision.select_option('commandcode/jev')
        assert "typesafe/jev" in page.locator('#configDecisionHeadline').inner_text()
        assert "@必回" in page.locator('#configModelHeadline').inner_text()
        assert "确认对象是机器人即可接话" in page.locator('#configModelStatus').inner_text()
        assert page.evaluate("window.__stored.decision_backend") == "kev"
        assert page.locator('#configConflicts').is_hidden()
        page.locator('#btnRefreshModels').click()
        page.wait_for_function("document.querySelector('#configNote').textContent.includes('已刷新')")
        assert decision.input_value() == 'commandcode/jev'
        assert page.locator('[data-config-key="takeover_groups"]').input_value() == '123'
        page.evaluate("window.__failModels = true")
        page.locator('#btnRefreshModels').click()
        page.wait_for_function("document.querySelector('#configNote').textContent.includes('读取失败')")
        assert decision.input_value() == 'commandcode/jev'
        page.evaluate("window.__failModels = false")
        page.locator('#btnRefreshModels').click()
        page.wait_for_function("document.querySelector('#configNote').textContent.includes('已刷新')")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        (ROOT / "output").mkdir(exist_ok=True)
        page.screenshot(path=str(ROOT / "output" / f"settings-jev-{width}-{theme}.png"), full_page=True)
        page.locator('#btnConfigSave').click()
        page.wait_for_function("window.__savedConfig?.decision_provider === 'commandcode/jev'")
        assert page.evaluate("!('decision_backend' in window.__savedConfig)")
        assert page.evaluate("!('decision_learning_mode' in window.__savedConfig)")
        assert page.evaluate("window.__stored.reply_provider") == "existing-model"
        assert page.evaluate("!('reply_provider' in window.__savedConfig) && !('jev_model' in window.__savedConfig)")
        assert not errors


def test_missing_router_blocks_save_and_keeps_draft(browser, page_server):
    with browser.new_context() as context:
        native_models(context)
        context.add_init_script("window.__routerMissing = true")
        page = context.new_page()
        page.goto(f"{page_server}/config/index.html")
        page.wait_for_load_state("networkidle")
        page.locator('#btnUseJev').click()
        page.locator('[data-config-key="decision_provider"]').select_option('zen/jev')
        assert "模型连接未就绪" in page.locator('#configConflictList').inner_text()
        page.locator('#btnConfigSave').click()
        assert "尚未就绪" in page.locator('#configNote').inner_text()
        assert page.evaluate("window.__savedConfig === undefined")
        assert page.locator('[data-config-key="decision_provider"]').input_value() == 'zen/jev'


@pytest.mark.parametrize("width,theme", [(1366, "day"), (390, "night")])
def test_multiline_participation_prompts_edit_save_and_restore(browser, page_server, width, theme):
    with browser.new_context(viewport={"width": width, "height": 940}) as context:
        native_models(context, theme)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{page_server}/config/index.html?ui={theme}")
        page.wait_for_load_state("networkidle")
        page.locator('[data-config-key="takeover_groups"]').fill("123")
        page.locator('[data-config-key="decision_provider"]').select_option('zen/jev')
        custom = {"decision_prompt": "主动聊游戏\n话题不用与自己有关", "reply_prompt": "先接住话题\n用简短自然的语气 <ok>"}
        for key, value in custom.items():
            page.locator('#configSearch').fill(key)
            field = page.locator(f'[data-config-key="{key}"]')
            assert field.is_visible() and field.evaluate('node => node.tagName') == 'TEXTAREA'
            assert field.get_attribute('maxlength') == '4000'
            assert "\n" in field.input_value()
            field.fill(value)
        page.locator('#configSearch').fill('')
        page.locator('#btnBasicConfig').click()
        page.locator('#btnRefreshModels').click()
        page.wait_for_function("document.querySelector('#configNote').textContent.includes('已刷新')")
        for key, value in custom.items():
            assert page.locator(f'[data-config-key="{key}"]').input_value() == value
        page.locator('#btnConfigSave').click()
        page.wait_for_function("window.__savedConfig?.decision_prompt?.includes('主动聊游戏')")
        for key, value in custom.items():
            assert page.evaluate('(key) => window.__stored[key]', key) == value
            assert page.locator(f'[data-config-key="{key}"]').input_value() == value
        page.locator('#btnAdvancedConfig').click()
        page.locator('details[data-group-id="providers"]').evaluate('node => node.open = true')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        (ROOT / "output").mkdir(exist_ok=True)
        page.screenshot(path=str(ROOT / "output" / f"settings-prompts-{width}-{theme}.png"), full_page=True)
        for key in custom:
            page.locator('#configSearch').fill(key)
            page.locator(f'[data-config-key="{key}"]').fill('')
        page.locator('#btnConfigSave').click()
        page.wait_for_function("window.__savedConfig?.decision_prompt === '' && window.__savedConfig?.reply_prompt === ''")
        assert not errors
