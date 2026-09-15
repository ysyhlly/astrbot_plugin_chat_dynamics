"""Actual page behavior for delayed config writes and validation recovery."""
from .test_config_simple_browser import setup_config
from .test_ui_theme_browser import browser as browser, page_server as page_server


def test_slow_save_locks_edits_and_sends_only_changed_fields(browser, page_server):
    with browser.new_context() as context:
        setup_config(context)
        page = context.new_page()
        page.goto(f"{page_server}/config/index.html")
        page.wait_for_load_state("networkidle")
        page.evaluate("""() => {
          const api = window.AstrBotPluginPage;
          const post = api.apiPost;
          api.apiPost = async (endpoint, body) => {
            window.__request = body;
            await new Promise(resolve => window.__finishSave = resolve);
            return post(endpoint, body);
          };
        }""")
        field = page.locator('[data-config-key="bot_names"]')
        field.fill("new name")
        page.locator('#btnConfigSave').click()
        page.wait_for_function("Boolean(window.__finishSave)")
        assert field.is_disabled()
        assert page.locator('#btnConfigSave').is_disabled()
        assert page.locator('#btnConfigReload').is_disabled()
        assert page.locator('#btnConfigApply').is_disabled()
        assert page.evaluate("Object.keys(window.__request.config)") == ["bot_names"]
        assert page.evaluate("window.__request.baseline.bot_names") == page.evaluate("window.__schema.bot_names.default")
        page.evaluate("window.__finishSave()")
        page.wait_for_function("document.querySelector('#configNote').textContent === '已保存并应用到运行时'")
        assert not field.is_disabled()
        assert field.input_value() == "new name"


def test_apply_cancel_preserves_dirty_and_invalid_hidden_field_is_revealed(browser, page_server):
    with browser.new_context() as context:
        setup_config(context)
        page = context.new_page()
        page.goto(f"{page_server}/config/index.html")
        page.wait_for_load_state("networkidle")
        page.locator('#configSearch').fill('replay_message_limit')
        field = page.locator('[data-config-key="replay_message_limit"]')
        field.fill("1.5")
        page.locator('#btnBasicConfig').click()
        assert not field.is_visible()
        page.locator('#btnConfigSave').click()
        assert field.is_visible()
        assert field.get_attribute('aria-invalid') == 'true'
        assert field.evaluate('node => node === document.activeElement')
        page.on('dialog', lambda dialog: dialog.dismiss())
        page.locator('#btnConfigApply').click()
        assert field.input_value() == '1.5'
        assert not page.locator('#btnConfigSave').is_disabled()


def test_learning_override_source_is_visible(browser, page_server):
    with browser.new_context() as context:
        setup_config(context)
        context.add_init_script("""(() => {
          const get = window.AstrBotPluginPage.apiGet;
          window.AstrBotPluginPage.apiGet = async (...args) => {
            const response = await get(...args);
            if (args[0] === 'config') {
              response.data.learning_policy = {applied: true, policy_id: 'policy-test'};
              response.data.learning_policy_overridden = ['decision_timeout'];
              response.data.warnings = ['参数已回退到默认值 <script>'];
            }
            return response;
          };
          const post = window.AstrBotPluginPage.apiPost;
          window.AstrBotPluginPage.apiPost = async (...args) => {
            const response = await post(...args);
            if (args[0] === 'config') response.data.warnings = ['参数已回退到默认值 <script>'];
            return response;
          };
        })();""")
        page = context.new_page()
        page.goto(f"{page_server}/config/index.html")
        page.wait_for_load_state("networkidle")
        page.locator('#configSearch').fill('decision_timeout')
        assert '学习策略覆盖（policy-test）' in page.locator('[data-config-key="decision_timeout"]').locator('..').inner_text()
        assert page.locator('#configWarnings').is_visible()
        assert '参数已回退到默认值 <script>' in page.locator('#configWarnings').inner_text()
        assert page.locator('#configWarnings script').count() == 0
        page.locator('[data-config-key="decision_timeout"]').fill('12')
        page.locator('#btnConfigSave').click()
        page.wait_for_function("document.querySelector('#configNote').textContent === '已保存并应用到运行时'")
        assert page.locator('#configWarnings').is_visible()
        assert '参数已回退到默认值' in page.locator('#configWarnings').inner_text()
