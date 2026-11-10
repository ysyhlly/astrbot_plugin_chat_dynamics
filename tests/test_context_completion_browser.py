"""The optional lookup setting is visible, scoped and persistent in the panel."""

from pathlib import Path

import pytest

from .test_config_simple_browser import setup_config
from .test_ui_theme_browser import browser as browser, page_server as page_server


@pytest.mark.parametrize("width,theme", [(1366, "day"), (390, "night")])
def test_context_completion_switch_saves_only_itself(browser, page_server, width, theme):
    with browser.new_context(viewport={"width": width, "height": 900}) as context:
        setup_config(context, theme)
        context.add_init_script("""(() => {
          const get = window.AstrBotPluginPage.apiGet;
          window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
            if (endpoint === 'config') window.__stored.decision_provider = 'test-jev';
            if (endpoint === 'providers') return {ok:true,data:{chat:[],embedding:[],
              systemone:[{id:'test-jev',model:'Jev',systemone:true}],systemone_router_ready:true}};
            return get(endpoint, params);
          };
        })();""")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{page_server}/config/index.html?ui={theme}")
        field = page.locator('[data-config-key="context_completion_enabled"]:visible')
        field.wait_for()
        assert not field.is_checked()
        field.check()
        page.locator("#btnConfigSave").click()
        page.wait_for_function("window.__savedConfig?.context_completion_enabled === true", timeout=5000)
        assert page.evaluate("Object.keys(window.__savedConfig)") == ["context_completion_enabled"]
        page.locator("#btnConfigReload").click()
        page.wait_for_function("document.querySelector('[data-config-key=context_completion_enabled]').checked")
        assert field.is_checked()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        output = Path(__file__).parents[2] / f"context-setting-{width}.png"
        field.locator('xpath=ancestor::*[contains(@class,"config-field")][1]').screenshot(path=str(output))
        assert not errors
