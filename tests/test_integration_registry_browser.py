"""Capability display distinguishes discovery, readiness, and selection."""
from .test_ui_theme_browser import browser as browser, page_server as page_server, setup


def test_capability_registry_escapes_provider_details(browser, page_server):
    with browser.new_context(viewport={"width": 390, "height": 844}) as context:
        setup(context, {"ui": "day"})
        page = context.new_page()
        page.goto(f"{page_server}/console/index.html")
        page.wait_for_selector("#integrationPanel", state="attached")
        page.evaluate("""async () => {
          const {renderIntegrations} = await import('./integrations.js');
          renderIntegrations({enabled:true,providers:[],capability_registry:[
            {name:'selflearning.native_hook',detected:true,ready:true,selected:true,detail:'host_hook_owned'},
            {name:'livingmemory.embedding_api',detected:false,ready:false,selected:false,detail:'<img src=x onerror=alert(1)>'}
          ]});
        }""")
        registry = page.locator("#integrationRegistry")
        assert "selflearning.native_hook · 已选择" in registry.text_content()
        assert "livingmemory.embedding_api · 未发现" in registry.text_content()
        assert registry.locator("img").count() == 0


def test_decision_layer_card_reports_the_backend_without_markup(browser, page_server):
    with browser.new_context(viewport={"width": 390, "height": 844}) as context:
        setup(context, {"ui": "day"})
        page = context.new_page()
        page.goto(f"{page_server}/console/index.html")
        page.wait_for_selector("#integrationPanel", state="attached")
        page.evaluate("""async () => {
          const {renderDecisionLayer} = await import('./integrations.js');
          renderDecisionLayer({backend:'jev', enabled:true, status:'available', detail:'ready',
            error_code:'', model:'jev-latest', served_model:'jev-1.13.0',
            endpoint_host:'api.typesafe.ai', calls:3, failures:1,
            request_id:'req_42', min_confidence:0.6});
        }""")
        card = page.locator("#decisionLayer")
        text = card.text_content()
        assert "Jev 决策模型" in text and "api.typesafe.ai" in text
        assert "实答 jev-1.13.0" in text and "req_42" in text
        assert "3 次 · 失败 1 次" in text and "0.6" in text
        page.evaluate("""async () => {
          const {renderDecisionLayer} = await import('./integrations.js');
          renderDecisionLayer({backend:'jev', status:'degraded',
            error_code:'<img src=x onerror=alert(1)>', model:'<b>model</b>'});
        }""")
        assert card.locator("img").count() == 0 and card.locator("b").count() == 0
        assert "<img" in card.text_content()
