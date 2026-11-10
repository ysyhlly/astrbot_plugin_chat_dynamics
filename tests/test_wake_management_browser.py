"""Operator-facing wake rules, conditional rhythm settings and storage status."""
import pytest
from .test_ui_theme_browser import ROOT, browser as browser, page_server as page_server, setup


@pytest.mark.parametrize("width", [390, 1366])
def test_config_explains_effective_rhythm_and_wake_policy(browser, page_server, width):
    with browser.new_context(viewport={"width": width, "height": 940}) as context:
        setup(context, {"ui": "day"})
        context.add_init_script("""(() => {
          const api = window.AstrBotPluginPage, get = api.apiGet;
          api.apiGet = async (endpoint, params) => endpoint === 'config'
            ? {ok:true,data:{schema:window.__schema,stored:{narrative_enabled:true,narrative_all_groups:true},effective:{narrative_enabled:true,narrative_all_groups:true}}}
            : get(endpoint, params);
        })();""")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{page_server}/config/index.html")
        page.wait_for_function("document.querySelector('#configModelStatus').textContent.includes('不会把')")
        assert "忽略" in page.locator("#configModelStatus").inner_text()
        assert "仅用于未接入" in page.locator('[data-context-key="daily_rhythm_enabled"]').text_content()
        assert "不会自动删除历史" in page.locator('[data-context-key="narrative_context_retention_days"]').text_content()
        assert not errors
        (ROOT / "output").mkdir(exist_ok=True)
        page.screenshot(path=str(ROOT / "output" / f"wake-config-{width}.png"), full_page=True)


def test_memory_shows_capacity_and_retention(browser, page_server):
    with browser.new_context() as context:
        setup(context, {"ui": "day"})
        context.add_init_script("""(() => {
          localStorage.setItem('cd_wire_umo','group-a');
          const api=window.AstrBotPluginPage,get=api.apiGet;
          api.apiGet=async(endpoint,params)=>{
            if(endpoint==='narrative') return {ok:true,data:{enabled:true,
              life:{managed:true,snapshot:{managed:true,availability:'awake',activity:'看书',revision:1}},
              storage:{database_bytes:1073741824,wal_bytes:0,legacy_database_bytes:2147483648,free_bytes:10737418240,low_space:false}}};
            if(endpoint==='sessions'||endpoint==='read_air') return {ok:true,data:{sessions:[{session_key:'group-a'}]}};
            return get(endpoint,params);
          };
        })();""")
        page=context.new_page()
        errors=[]
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{page_server}/memory/index.html")
        page.wait_for_function("document.querySelector('#narrativeSnapshot').textContent.includes('1.00 GiB')")
        assert "不自动删库" in page.locator("#narrativeSnapshot").inner_text()
        assert "10.00 GiB" in page.locator("#narrativeSnapshot").inner_text()
        assert not errors
        (ROOT / "output").mkdir(exist_ok=True)
        page.screenshot(path=str(ROOT / "output" / "wake-storage.png"), full_page=True)
