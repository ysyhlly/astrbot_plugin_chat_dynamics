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
