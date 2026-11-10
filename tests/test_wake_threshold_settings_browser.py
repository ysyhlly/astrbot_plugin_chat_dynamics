import pytest
from .test_systemone_settings_browser import native_models
from .test_ui_theme_browser import browser as browser, page_server as page_server


@pytest.mark.parametrize('width', [390, 1366])
def test_threshold_controls_save_and_read_back(browser, page_server, width):
    with browser.new_context(viewport={'width': width, 'height': 1000}) as context:
        native_models(context)
        context.add_init_script("window.__stored.decision_provider = 'commandcode/jev'")
        page = context.new_page()
        page.goto(f'{page_server}/config/index.html')
        page.wait_for_load_state('networkidle')
        page.locator('#configSearch').fill('对象门槛')
        quote = page.locator('[data-config-key="quote_reply_threshold"]')
        name = page.locator('[data-config-key="name_reply_threshold"]')
        assert quote.is_visible() and name.is_visible()
        assert quote.get_attribute('min') == '0' and quote.get_attribute('max') == '100'
        quote.fill('45')
        name.fill('55')
        page.locator('#btnConfigSave').click()
        page.wait_for_function('window.__savedConfig?.quote_reply_threshold === 45 && window.__savedConfig?.name_reply_threshold === 55')
        assert page.evaluate('Object.keys(window.__savedConfig).sort()') == ['name_reply_threshold', 'quote_reply_threshold']
        page.locator('#btnConfigReload').click()
        page.wait_for_function('document.querySelector("[data-config-key=quote_reply_threshold]").value === "45"')
        assert name.input_value() == '55'
        assert '45%' in page.locator('#configModelHeadline').inner_text()
        assert '55%' in page.locator('#configModelHeadline').inner_text()
